#!/usr/bin/env python3
"""Evaluate DeepWiki-style generation: per-file notes (Haiku) + wiki pages (Haiku/Sonnet/Opus).

    python scripts/wiki_eval.py --stage notes                # notes for every enabled repo (timed)
    python scripts/wiki_eval.py --stage pages --repos voluptas,argus-cortex,argus-curator
    python scripts/wiki_eval.py --stage judge --run <dir>    # blind Opus judge vs full repo source
    python scripts/wiki_eval.py --stage all --repos ...      # all three

Stage 1 writes one JSON note per source file (summary, key symbols with line ranges, deps),
cached by content hash under reports/wiki_eval/notes/ so reruns only pay for changed files.
Stage 2 asks each arm to write four pages per repo from those notes, citing [path:Lx-Ly].
Citations are checked mechanically (file exists, range in bounds, overlaps a real AST unit).
Stage 3 shows the judge the repo's full source plus the arms' pages (anonymised, shuffled)
and scores accuracy / coverage / usefulness / citations 1-5 and lists factual errors.

All spend is recorded in the ledger on lane "wiki" with note prefix "baseline:" (one-off).

Arms: plain model names (haiku/sonnet/opus) draft from notes only; "-v2" adds structure facts
(imports, __all__/exports, top-level defs read from the AST); "-v2v" adds the check-and-fix pass
over the v2 draft. The production setup chosen on 2026-11-13 is opus-v2v (ai-code-auditor#8).

Re-running when new models or prices arrive:
  1. add the model's price to PRICES in auditor/spend_ledger.py;
  2. add arms to ARMS (and STRUCTURE_ARMS / VERIFY_BASE for -v2 / -v2v variants);
  3. compare against the incumbent, reusing cached notes and an earlier run as the baseline:
       python scripts/wiki_eval.py --stage all --arms opus-v2v,<new>-v2v \\
           --baseline-run reports/wiki_eval/<earlier run> --judge-arms opus-v1,opus-v2v,<new>-v2v
  Compare versions within one run only: the judge scores pages relative to each other.
  Round 2 (4 arms x 12 pages + judge) cost ~$14.50; the judge is ~40% of that.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from auditor.chunker import extract_code_units  # noqa: E402
from auditor.config import load_ecosystem_config  # noqa: E402
from auditor.repo_resolver import resolve_repo_path, resolve_scan_root  # noqa: E402
from auditor.repo_scanner import scan_repo  # noqa: E402
from auditor.spend_ledger import get_ledger  # noqa: E402

LANE = "wiki"
NOTE = "baseline: wiki eval"
OUT_BASE = ROOT / "reports" / "wiki_eval"
NOTES_DIR = OUT_BASE / "notes"
NOTES_MODEL = "claude-haiku-5-5"
MAX_FILE_CHARS = 200_000  # larger files are reported, not truncated

ARMS = {
    "haiku": ("claude-haiku-5-5", "medium"),
    "sonnet": ("claude-sonnet-5-5", "medium"),
    "opus": ("claude-opus-5-5", "medium"),
    # round 2: v2 = notes + deterministic structure facts; v2v = v2 page + check-and-fix pass
    "opus-v1": ("claude-opus-5-5", "medium"),  # copied from a round-1 run as the baseline
    "sonnet-v2": ("claude-sonnet-5-5", "medium"),
    "sonnet-v2v": ("claude-sonnet-5-5", "medium"),
    "opus-v2": ("claude-opus-5-5", "medium"),
    "opus-v2v": ("claude-opus-5-5", "medium"),
}
STRUCTURE_ARMS = {"sonnet-v2", "sonnet-v2v", "opus-v2", "opus-v2v"}
VERIFY_BASE = {"sonnet-v2v": "sonnet-v2", "opus-v2v": "opus-v2"}
SKIP_DIRS = {"vendor", "vendored", "third_party"}
JUDGE = ("claude-opus-5-5", "high")

PAGES = {
    "overview": "Overview: what this repository is for, who/what uses it, its main capabilities, "
                "and how it fits into the wider Argus ecosystem. Orientation for a newcomer.",
    "architecture": "Architecture: the main modules/packages, their responsibilities, and how data "
                    "and control flow between them. Include a mermaid diagram if it genuinely helps.",
    "interfaces": "Interfaces and entry points: HTTP routes, CLI commands, workers/jobs, public "
                  "functions or classes other code calls, config and env vars that change behaviour.",
    "integration": "Integration: what this repo depends on and what depends on it — other ecosystem "
                   "repos, external services, databases, queues, model providers — and the contracts "
                   "(payloads, schemas, endpoints) at each boundary.",
}

NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "role": {"type": "string"},
        "key_symbols": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "purpose": {"type": "string"},
                },
                "required": ["name", "kind", "start_line", "end_line", "purpose"],
                "additionalProperties": False,
            },
        },
        "interfaces": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "role", "key_symbols", "interfaces", "dependencies"],
    "additionalProperties": False,
}

NOTE_PROMPT = """You are indexing a codebase to build a developer wiki. Read the file below \
(line numbers are prefixed) and return a compact note:
- summary: 2-4 sentences on what the file does.
- role: one of entrypoint, api, service, model, config, util, test, script, ui, other — plus a few words.
- key_symbols: the functions/classes/routes that matter to someone understanding the repo, with \
exact start_line/end_line from the prefixes. Skip trivial helpers. At most 12.
- interfaces: externally visible surface defined here (HTTP routes with method+path, CLI commands, \
env vars read, queues/topics, DB tables).
- dependencies: other ecosystem repos, external services or notable libraries this file talks to.
Be factual; only state what the code shows.

Repository: {repo}
File: {path}

{body}"""

PAGE_SYSTEM = """You write pages for a DeepWiki-style developer wiki of one repository in the \
Argus ecosystem. Readers are engineers and coding agents new to the repo.

Rules:
- Every factual claim about the code must cite its source as [path:Lstart-Lend] using the \
repo-relative paths and line numbers from the file notes. Cite the narrowest range that supports \
the claim. Never invent a path or line number.
- Only state what the notes and context support. If something is unclear, say so briefly rather \
than guessing.
- Write GitHub-flavoured markdown. Start with a level-1 heading. Prefer short sections, tables \
for lists of routes/commands/env vars, and concrete names over generic prose.
- Length: as long as the content warrants, typically 400-1200 words."""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "accuracy": {"type": "integer"},
                    "coverage": {"type": "integer"},
                    "usefulness": {"type": "integer"},
                    "citation_quality": {"type": "integer"},
                    "factual_errors": {"type": "array", "items": {"type": "string"}},
                    "notable_omissions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "accuracy", "coverage", "usefulness", "citation_quality",
                             "factual_errors", "notable_omissions"],
                "additionalProperties": False,
            },
        },
        "ranking": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["pages", "ranking", "rationale"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTIONS = """You are grading candidate wiki pages for the repository whose full source is \
above. The pages were written by different anonymous writers from per-file notes; you have the \
real code, so check claims against it.

Page brief: {brief}

Score each page 1-5 (5 best) on:
- accuracy: are its claims true of the code? One clear factual error caps this at 3; several at 2.
- coverage: does it cover what matters for this page type in THIS repo?
- usefulness: would an engineer or coding agent new to the repo get oriented faster from it?
- citation_quality: do [path:Lx-Ly] citations point at code that supports the claim?
List concrete factual_errors (quote the claim, say what the code actually does) and \
notable_omissions. Then rank labels best to worst. Do not reward length or confident tone.

{pages}"""


# ---------------------------------------------------------------- helpers

def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)


def call(cl: anthropic.Anthropic, model: str, effort: str, *, system: Any = None,
         content: Any, max_tokens: int, schema: dict | None = None) -> dict:
    output_config: dict[str, Any] = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens, output_config=output_config,
                                  messages=[{"role": "user", "content": content}])
    if system is not None:
        kwargs["system"] = system
    t0 = time.time()
    try:
        with cl.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "latency_s": time.time() - t0, "cost": 0.0}
    usd = get_ledger().record_usage(LANE, model, msg.usage, note=NOTE)
    text = "".join(b.text for b in msg.content if b.type == "text")
    u = msg.usage
    return {
        "text": text, "stop_reason": msg.stop_reason, "latency_s": round(time.time() - t0, 2),
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "cache_write": u.cache_creation_input_tokens or 0, "cache_read": u.cache_read_input_tokens or 0,
        "cost": usd,
    }


def repos_by_name() -> dict[str, Any]:
    return {r.name: r for r in load_ecosystem_config().repos if r.enabled}


def numbered(text: str) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def source_files(repo) -> list[Path]:
    root = resolve_scan_root(repo)
    return [f for f in sorted(scan_repo(repo))
            if not SKIP_DIRS.intersection(f.relative_to(root).parts)]


# ---------------------------------------------------------------- structure facts (no LLM)

_JS_IMPORT = re.compile(r"""^\s*(?:import\b[^'"]*?from\s*|import\s*|export\b[^'"]*?from\s*)['"]([^'"]+)['"]""", re.M)
_JS_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_EXPORT = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|type|interface|enum)\s+(\w+)", re.M)


def file_structure(path: Path, text: str) -> dict:
    """Imports, exports and top-level names, read from the code itself."""
    import ast

    out: dict[str, Any] = {"imports": [], "exports": None, "top_level": []}
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        imps: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imps += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = "." * node.level + (node.module or "")
                imps.append(f"{mod}: {', '.join(a.name for a in node.names)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out["top_level"].append(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        try:
                            out["exports"] = list(ast.literal_eval(node.value))
                        except (ValueError, SyntaxError):
                            out["exports"] = ["<dynamic __all__>"]
        # lazy imports inside functions matter for "optional dependency" claims
        lazy = sorted({a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                       and n not in tree.body for a in n.names}
                      | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                         and n not in tree.body and n.level == 0})
        out["imports"] = imps
        if lazy:
            out["lazy_imports"] = lazy
    elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
        out["imports"] = sorted(set(_JS_IMPORT.findall(text)) | set(_JS_REQUIRE.findall(text)))
        out["exports"] = _JS_EXPORT.findall(text) or None
    return out


def structure_block(repo) -> str:
    root = resolve_scan_root(repo)
    lines = ["## Structure facts (read from the code, authoritative)\n",
             "Imports are module-level unless marked lazy (imported inside a function). "
             "`exports` is `__all__` (Python) or `export` declarations (JS/TS); "
             "null means the file declares none.\n"]
    for f in source_files(repo):
        text = f.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_FILE_CHARS:
            continue
        s = file_structure(f, text)
        if not (s["imports"] or s["exports"] or s["top_level"]):
            continue
        lines.append(f"\n### {rel(f, root)}")
        if s["imports"]:
            lines.append("- imports: " + "; ".join(s["imports"]))
        if s.get("lazy_imports"):
            lines.append("- lazy imports: " + ", ".join(s["lazy_imports"]))
        lines.append(f"- exports: {s['exports']}")
        if s["top_level"]:
            lines.append("- top-level defs: " + ", ".join(s["top_level"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- stage 1: notes

def note_path(repo: str, relpath: str, text: str) -> Path:
    h = hashlib.sha1(f"{relpath}\0{text}".encode()).hexdigest()[:16]
    return NOTES_DIR / repo / f"{h}.json"


def stage_notes(repo_names: list[str], concurrency: int) -> dict:
    cl = client()
    repos = repos_by_name()
    jobs, cached, oversize = [], 0, []
    for name in repo_names:
        repo = repos[name]
        root = resolve_scan_root(repo)
        for f in source_files(repo):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rp = rel(f, root)
            if len(text) > MAX_FILE_CHARS:
                oversize.append(f"{name}/{rp} ({len(text)//1000}k chars)")
                continue
            if not text.strip():
                continue
            out = note_path(name, rp, text)
            if out.exists():
                cached += 1
                continue
            jobs.append((name, rp, text, out))

    print(f"notes: {len(jobs)} to write, {cached} cached, {len(oversize)} oversize skipped", flush=True)
    stats = {"files": len(jobs), "cached": cached, "oversize": oversize, "cost": 0.0,
             "input_tokens": 0, "output_tokens": 0, "errors": 0, "latencies": []}

    def work(job):
        name, rp, text, out = job
        prompt = NOTE_PROMPT.format(repo=name, path=rp, body=numbered(text))
        r = call(cl, NOTES_MODEL, "low", content=prompt, max_tokens=8000, schema=NOTE_SCHEMA)
        if "error" in r:
            return job, r
        try:
            note = json.loads(r["text"])
        except json.JSONDecodeError:
            r["error"] = "parse"
            return job, r
        note.update(path=rp, lines=text.count("\n") + 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(note, indent=1))
        return job, r

    t0 = time.time()
    with ThreadPoolExecutor(concurrency) as pool:
        for i, fut in enumerate(as_completed([pool.submit(work, j) for j in jobs]), 1):
            job, r = fut.result()
            stats["cost"] += r.get("cost", 0.0)
            stats["input_tokens"] += r.get("input_tokens", 0)
            stats["output_tokens"] += r.get("output_tokens", 0)
            if "error" in r:
                stats["errors"] += 1
                print(f"  ! {job[0]}/{job[1]}: {r['error'][:160]}", flush=True)
            else:
                stats["latencies"].append(r["latency_s"])
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)}  ${stats['cost']:.3f}  {time.time()-t0:.0f}s", flush=True)
    stats["wall_s"] = round(time.time() - t0, 1)
    stats["batch_equiv_cost"] = stats["cost"] / 2
    lat = sorted(stats.pop("latencies")) or [0]
    stats["latency_p50"], stats["latency_p95"] = lat[len(lat) // 2], lat[int(len(lat) * 0.95)]
    return stats


def load_notes(repo) -> list[dict]:
    root = resolve_scan_root(repo)
    notes = []
    for f in source_files(repo):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        p = note_path(repo.name, rel(f, root), text)
        if p.exists():
            notes.append(json.loads(p.read_text()))
    return notes


# ---------------------------------------------------------------- stage 2: pages

def repo_context(repo, notes: list[dict]) -> str:
    base = resolve_repo_path(repo)
    readme = next(iter(sorted(base.glob("README*"))), None)
    eco = (ROOT / "ecosystem" / "ecosystem.md").read_text(encoding="utf-8")
    parts = [f"# Repository: {repo.name}\n",
             "## Ecosystem topology (shared doc)\n", eco]
    if readme:
        parts += [f"\n## {readme.name}\n", readme.read_text(encoding="utf-8", errors="replace")[:20000]]
    parts.append(f"\n## File notes ({len(notes)} files)\n")
    for n in notes:
        parts.append(f"\n### {n['path']} ({n['lines']} lines) — {n['role']}\n{n['summary']}")
        for s in n["key_symbols"]:
            parts.append(f"- {s['kind']} `{s['name']}` L{s['start_line']}-L{s['end_line']}: {s['purpose']}")
        if n["interfaces"]:
            parts.append("  interfaces: " + "; ".join(n["interfaces"]))
        if n["dependencies"]:
            parts.append("  deps: " + "; ".join(n["dependencies"]))
    return "\n".join(parts)


CITE = re.compile(r"\[([\w./@+-]+?\.\w+):L(\d+)(?:-L?(\d+))?\]")
_NAMED = re.compile(r"`([A-Za-z_][\w.]*)(?:\(\))?`")


def check_citations(md: str, repo) -> dict:
    """valid: file exists + range in bounds. anchored: overlaps an AST unit.
    named/matched: when a backticked identifier precedes the citation on the same line
    (nearest one), does that identifier actually occur in the cited lines?"""
    root = resolve_scan_root(repo)
    text_cache: dict[str, list[str] | None] = {}
    units_cache: dict[str, list[tuple[int, int]]] = {}
    total = valid = anchored = named = matched = 0
    bad: list[str] = []
    mismatched: list[str] = []
    for m in CITE.finditer(md):
        total += 1
        path, a = m.group(1), int(m.group(2))
        b = int(m.group(3) or a)
        if path not in text_cache:
            f = root / path
            if f.is_file():
                text_cache[path] = f.read_text(encoding="utf-8", errors="replace").splitlines()
                units_cache[path] = [(u.start_line, u.end_line)
                                     for u in extract_code_units(repo.name, f)]
            else:
                text_cache[path] = None
        lines = text_cache[path]
        if lines is None or not (1 <= a <= b <= len(lines) + 1):
            bad.append(m.group(0))
            continue
        valid += 1
        units = units_cache.get(path) or []
        if not units or any(s <= b and a <= e for s, e in units):
            anchored += 1
        line_start = md.rfind("\n", 0, m.start()) + 1
        prev_cite = max((c.end() for c in CITE.finditer(md, line_start, m.start())), default=line_start)
        names = [x for x in _NAMED.findall(md, prev_cite, m.start())
                 if x.rsplit(".", 1)[-1] not in {"py", "ts", "tsx", "js", "jsx", "md", "json", "yaml", "toml"}]
        if names:
            named += 1
            ident = names[-1].split(".")[-1]
            if re.search(rf"\b{re.escape(ident)}\b", "\n".join(lines[a - 1:b])):
                matched += 1
            else:
                mismatched.append(f"`{names[-1]}` {m.group(0)}")
    return {"total": total, "valid": valid, "anchored": anchored, "named": named,
            "matched": matched, "invalid_examples": bad[:8], "mismatch_examples": mismatched[:8]}


VERIFY_SYSTEM = """You are fact-checking a draft wiki page against the repository's actual code. \
You are given the structure facts (imports/exports read from the code), the exact code for \
every range the draft cites (line-numbered), and the draft.

For every claim in the draft:
- If the cited code supports it, keep it.
- If the cited code contradicts it, or the claim over-generalises (\"all\", \"every\", \
\"everything\", \"only\"), rewrite it to match the code exactly, or delete it.
- If the citation points at the wrong lines, fix the line numbers using the code shown; if \
you cannot find supporting lines in what is shown, remove the claim rather than guess.
- Claims about imports, re-exports, dependency direction and optional dependencies must match \
the structure facts.
Do not add new sections or new claims. Keep the draft's structure, headings and tone. \
Output only the corrected page markdown, nothing else."""


def cited_code(md: str, repo, pad: int = 3, cap_chars: int = 120_000) -> str:
    root = resolve_scan_root(repo)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for m in CITE.finditer(md):
        a = int(m.group(2))
        ranges.setdefault(m.group(1), []).append((a, int(m.group(3) or a)))
    parts, size = [], 0
    for path, rs in sorted(ranges.items()):
        f = root / path
        if not f.is_file():
            parts.append(f"\n===== {path} (FILE DOES NOT EXIST) =====")
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        merged: list[list[int]] = []
        for a, b in sorted(rs):
            a, b = max(1, a - pad), min(len(lines), b + pad)
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            chunk = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))
            if size + len(chunk) > cap_chars:
                parts.append(f"\n===== {path} L{a}-L{b} (not shown: size cap) =====")
                continue
            size += len(chunk)
            parts.append(f"\n===== {path} L{a}-L{b} =====\n{chunk}")
    return "\n".join(parts)


def copy_baseline(baseline_run: Path, run: Path, repo_names: list[str], src_arm: str = "opus",
                  dst_arm: str = "opus-v1") -> list[dict]:
    """Reuse round-1 pages as a judged baseline (no new spend). Citations re-checked with the
    current checker so the columns are comparable."""
    import shutil

    repos = repos_by_name()
    old = [json.loads(l) for l in (baseline_run / "pages.jsonl").read_text().splitlines() if l.strip()]
    recs = []
    for r in old:
        if r["arm"] != src_arm or r["repo"] not in repo_names or "error" in r:
            continue
        src = baseline_run / "pages" / r["repo"] / src_arm / f"{r['page']}.md"
        dst = run / "pages" / r["repo"] / dst_arm / f"{r['page']}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        recs.append({**r, "arm": dst_arm, "citations": check_citations(src.read_text(), repos[r["repo"]])})
    return recs


def stage_pages(repo_names: list[str], arm_names: list[str], run: Path, concurrency: int,
                baseline_run: Path | None = None) -> list[dict]:
    cl = client()
    repos = repos_by_name()
    results: list[dict] = []
    if baseline_run:
        results += copy_baseline(baseline_run, run, repo_names)
    gen_arms = [a for a in arm_names if a not in VERIFY_BASE and a != "opus-v1"]
    for a in arm_names:  # a verify arm needs its base generated too
        if a in VERIFY_BASE and VERIFY_BASE[a] not in gen_arms:
            gen_arms.append(VERIFY_BASE[a])
    verify_arms = [a for a in arm_names if a in VERIFY_BASE]

    jobs = []
    for name in repo_names:
        repo = repos[name]
        notes = load_notes(repo)
        if not notes:
            print(f"skip {name}: no notes (run --stage notes first)")
            continue
        ctx = repo_context(repo, notes)
        ctx_struct = ctx + "\n\n" + structure_block(repo)
        for arm in gen_arms:
            for page in PAGES:
                jobs.append((repo, arm, page, ctx_struct if arm in STRUCTURE_ARMS else ctx))
    print(f"pages: {len(jobs)} draft calls + {len(verify_arms) * len(PAGES) * len(repo_names)} "
          f"check-and-fix calls", flush=True)

    def log(rec):
        results.append(rec)
        if "error" in rec:
            info = rec["error"][:60]
        else:
            c = rec["citations"]
            info = (f"{rec['words']}w cites {c['valid']}/{c['total']} "
                    f"symbol {c['matched']}/{c['named']}")
        print(f"  {rec['repo']:14} {rec['arm']:10} {rec['page']:12} {info}"
              f"  ${rec['cost']:.3f} {rec['latency_s']:.0f}s", flush=True)

    def save(repo, arm, page, r, extra_cost=0.0):
        rec = {"repo": repo.name, "arm": arm, "page": page, **{k: v for k, v in r.items() if k != "text"}}
        rec["cost"] = rec.get("cost", 0.0) + extra_cost
        if "text" in r:
            d = run / "pages" / repo.name / arm
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{page}.md").write_text(r["text"])
            rec["words"] = len(r["text"].split())
            rec["citations"] = check_citations(r["text"], repo)
        return rec

    def draft(job):
        repo, arm, page, ctx = job
        model, effort = ARMS[arm]
        system = [{"type": "text", "text": PAGE_SYSTEM},
                  {"type": "text", "text": ctx, "cache_control": {"type": "ephemeral"}}]
        content = f"Write the **{page}** page.\n\nBrief: {PAGES[page]}"
        r = call(cl, model, effort, system=system, content=content, max_tokens=32000)
        return save(repo, arm, page, r)

    def verify(job):
        repo, arm, page, base_rec, struct = job
        model, effort = ARMS[arm]
        md = (run / "pages" / repo.name / VERIFY_BASE[arm] / f"{page}.md").read_text()
        system = [{"type": "text", "text": VERIFY_SYSTEM},
                  {"type": "text", "text": struct, "cache_control": {"type": "ephemeral"}}]
        content = (f"## Cited code\n{cited_code(md, repo)}\n\n## Page brief\n{PAGES[page]}\n\n"
                   f"## Draft\n{md}")
        r = call(cl, model, effort, system=system, content=content, max_tokens=32000)
        # cost of a v2v page = its draft + the check-and-fix pass
        rec = save(repo, arm, page, r, extra_cost=base_rec["cost"])
        rec["verify_cost"] = r.get("cost", 0.0)
        return rec

    # Warm each (repo, arm) cache with one page first, then fan out the rest.
    first: dict = {}
    for j in jobs:
        first.setdefault((j[0].name, j[1]), j)
    rest = [j for j in jobs if first[(j[0].name, j[1])] is not j]
    for phase in (list(first.values()), rest):
        with ThreadPoolExecutor(concurrency) as pool:
            for fut in as_completed([pool.submit(draft, j) for j in phase]):
                log(fut.result())

    if verify_arms:
        by_key = {(r["repo"], r["arm"], r["page"]): r for r in results}
        structs = {n: structure_block(repos[n]) for n in repo_names}
        vjobs = [(repos[n], a, p, by_key[(n, VERIFY_BASE[a], p)], structs[n])
                 for n in repo_names for a in verify_arms for p in PAGES
                 if "error" not in by_key.get((n, VERIFY_BASE[a], p), {"error": 1})]
        with ThreadPoolExecutor(concurrency) as pool:
            for fut in as_completed([pool.submit(verify, j) for j in vjobs]):
                log(fut.result())

    (run / "pages.jsonl").write_text("\n".join(json.dumps(r) for r in results))
    return results


# ---------------------------------------------------------------- stage 3: judge

def full_source(repo) -> str:
    root = resolve_scan_root(repo)
    parts = [f"# Full source of {repo.name}\n"]
    for f in source_files(repo):
        text = f.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_FILE_CHARS:
            parts.append(f"\n===== {rel(f, root)} (omitted: {len(text)//1000}k chars) =====")
            continue
        parts.append(f"\n===== {rel(f, root)} =====\n{numbered(text)}")
    return "\n".join(parts)


def stage_judge(run: Path, concurrency: int, seed: int = 7, judge_arms: list[str] | None = None) -> list[dict]:
    cl = client()
    repos = repos_by_name()
    rng = random.Random(seed)
    jobs = []
    for repo_dir in sorted((run / "pages").iterdir()):
        repo = repos[repo_dir.name]
        src = full_source(repo)
        arms = sorted(p.name for p in repo_dir.iterdir() if not judge_arms or p.name in judge_arms)
        for page in PAGES:
            present = [a for a in arms if (repo_dir / a / f"{page}.md").exists()]
            rng.shuffle(present)
            labels = {chr(65 + i): a for i, a in enumerate(present)}
            jobs.append((repo, page, src, labels, repo_dir))
    print(f"judge: {len(jobs)} calls", flush=True)

    def work(job):
        repo, page, src, labels, repo_dir = job
        pages_txt = "\n\n".join(
            f"===== PAGE {lab} =====\n{(repo_dir / arm / f'{page}.md').read_text()}"
            for lab, arm in labels.items())
        system = [{"type": "text", "text": src, "cache_control": {"type": "ephemeral"}}]
        content = JUDGE_INSTRUCTIONS.format(brief=PAGES[page], pages=pages_txt)
        r = call(cl, *JUDGE, system=system, content=content, max_tokens=32000, schema=JUDGE_SCHEMA)
        rec = {"repo": repo.name, "page": page, "labels": labels,
               **{k: v for k, v in r.items() if k != "text"}}
        if "text" in r:
            try:
                rec["verdict"] = json.loads(r["text"])
            except json.JSONDecodeError:
                rec["error"] = "parse"
        return rec

    results = []
    first = {}
    for j in jobs:
        first.setdefault(j[0].name, j)
    rest = [j for j in jobs if first[j[0].name] is not j]
    for phase in (list(first.values()), rest):
        with ThreadPoolExecutor(concurrency) as pool:
            for fut in as_completed([pool.submit(work, j) for j in phase]):
                rec = fut.result()
                results.append(rec)
                print(f"  judged {rec['repo']:14} {rec['page']:12} ${rec['cost']:.3f} "
                      f"{rec.get('error', '')}", flush=True)
    (run / "judgements.jsonl").write_text("\n".join(json.dumps(r) for r in results))
    return results


# ---------------------------------------------------------------- summary

def summarise(run: Path, notes_stats: dict | None) -> str:
    pages = [json.loads(l) for l in (run / "pages.jsonl").read_text().splitlines() if l.strip()]
    judg = [json.loads(l) for l in (run / "judgements.jsonl").read_text().splitlines()
            if l.strip()] if (run / "judgements.jsonl").exists() else []
    arms = sorted({p["arm"] for p in pages}, key=list(ARMS).index)
    agg: dict[str, dict[str, Any]] = {a: {"cost": 0.0, "words": 0, "n": 0, "cites": 0, "valid": 0,
                                          "anch": 0, "named": 0, "matched": 0, "lat": 0.0, "err": 0}
                                      for a in arms}
    for p in pages:
        a = agg[p["arm"]]
        a["cost"] += p["cost"]
        a["lat"] += p["latency_s"]
        if "error" in p:
            a["err"] += 1
            continue
        a["n"] += 1
        a["words"] += p["words"]
        a["cites"] += p["citations"]["total"]
        a["valid"] += p["citations"]["valid"]
        a["anch"] += p["citations"]["anchored"]
        a["named"] += p["citations"].get("named", 0)
        a["matched"] += p["citations"].get("matched", 0)
    scores: dict[str, dict[str, list]] = {a: {"accuracy": [], "coverage": [], "usefulness": [],
                                              "citation_quality": [], "errors": [], "rank1": []}
                                          for a in arms}
    judge_cost = 0.0
    for j in judg:
        judge_cost += j["cost"]
        v = j.get("verdict")
        if not v:
            continue
        for pg in v["pages"]:
            arm = j["labels"].get(pg["label"])
            if not arm:
                continue
            for k in ("accuracy", "coverage", "usefulness", "citation_quality"):
                scores[arm][k].append(pg[k])
            scores[arm]["errors"].append(len(pg["factual_errors"]))
        if v["ranking"]:
            top = j["labels"].get(v["ranking"][0])
            if top:
                scores[top]["rank1"].append(1)

    def avg(xs):
        return f"{sum(xs)/len(xs):.2f}" if xs else "–"

    n_repos = len({p["repo"] for p in pages})
    n_judged = {a: len(scores[a]["accuracy"]) for a in arms}
    lines = [f"# Wiki eval — {run.name}\n"]
    if notes_stats:
        lines += [
            "## Stage 1 — per-file notes (Haiku 5.5, effort low, direct API)\n",
            f"- Files written: {notes_stats['files']} (cached: {notes_stats['cached']}), "
            f"errors: {notes_stats['errors']}",
            f"- Tokens: {notes_stats['input_tokens']:,} in / {notes_stats['output_tokens']:,} out",
            f"- Cost: ${notes_stats['cost']:.2f} direct (≈ ${notes_stats['batch_equiv_cost']:.2f} via batch)",
            f"- Wall time: {notes_stats['wall_s']/60:.1f} min; per-call latency "
            f"p50 {notes_stats['latency_p50']:.1f}s, p95 {notes_stats['latency_p95']:.1f}s",
            f"- Oversize files skipped (> {MAX_FILE_CHARS//1000}k chars): "
            + (", ".join(notes_stats["oversize"]) or "none"), "",
        ]
    lines += [
        f"## Stage 2 — pages ({n_repos} repos × {len(PAGES)} pages per arm, effort medium)\n",
        "| Arm | Pages | Avg words | Citations/page | Valid cites | Anchored cites | Symbol match | "
        "Accuracy | Coverage | Usefulness | Citation quality | Factual errors/page | Ranked #1 | "
        "Cost (direct) | Cost/repo via batch | Avg latency |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in arms:
        a, s = agg[arm], scores[arm]
        n = max(a["n"], 1)
        rank = f"{len(s['rank1'])}/{len(judg)}" if n_judged[arm] else "not judged"
        lines.append(
            f"| {arm} | {a['n']} (+{a['err']} err) | {a['words']//n} | {a['cites']/n:.1f} | "
            f"{a['valid']/max(a['cites'],1):.0%} | {a['anch']/max(a['cites'],1):.0%} | "
            f"{a['matched']/max(a['named'],1):.0%} ({a['named']}) | "
            f"{avg(s['accuracy'])} | {avg(s['coverage'])} | {avg(s['usefulness'])} | "
            f"{avg(s['citation_quality'])} | {avg(s['errors'])} | "
            f"{rank} | ${a['cost']:.2f} | ${a['cost']/n_repos/2:.3f} | "
            f"{a['lat']/max(a['n']+a['err'],1):.0f}s |")
    lines += [
        "",
        f"Judge: {JUDGE[0]} @ {JUDGE[1]}, sees each repo's full line-numbered source; pages "
        f"anonymised and shuffled. Judge cost: ${judge_cost:.2f}.",
        "Valid = path exists and range in bounds. Anchored = valid and overlaps an AST unit "
        "(or file has none). Symbol match = when a `backticked` identifier precedes the "
        "citation on the same line, it occurs in the cited lines (count in brackets).",
        "Costs for v2v arms include the draft they verified. opus-v1 is copied from round 1 "
        "(its cost is round 1's).",
        "Caveat: one Opus judge may favour Opus-style prose; factual-error counts are the "
        "least style-sensitive column.",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=["notes", "pages", "judge", "all"], default="all")
    ap.add_argument("--repos", default="voluptas,argus-cortex,argus-curator",
                    help="repos for pages/judge (notes stage: 'all' = every enabled repo)")
    ap.add_argument("--notes-repos", default="all")
    ap.add_argument("--arms", default="sonnet-v2v,opus-v2v",
                    help=f"comma list from: {', '.join(ARMS)}")
    ap.add_argument("--run", help="existing run dir (for --stage judge)")
    ap.add_argument("--baseline-run", help="copy this run's opus pages in as arm opus-v1")
    ap.add_argument("--judge-arms", help="comma list; default: every arm with pages")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    run = Path(args.run) if args.run else OUT_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    page_repos = [r for r in args.repos.split(",") if r]
    notes_stats = None
    if args.stage in ("notes", "all"):
        names = list(repos_by_name()) if args.notes_repos == "all" else args.notes_repos.split(",")
        notes_stats = stage_notes(names, args.concurrency)
        (run / "notes_stats.json").write_text(json.dumps(notes_stats, indent=1))
        print(json.dumps(notes_stats, indent=1))
    elif (run / "notes_stats.json").exists():
        notes_stats = json.loads((run / "notes_stats.json").read_text())
    if args.stage in ("pages", "all"):
        stage_pages(page_repos, args.arms.split(","), run, args.concurrency,
                    Path(args.baseline_run) if args.baseline_run else None)
    if args.stage in ("judge", "all"):
        stage_judge(run, min(args.concurrency, 4),
                    judge_arms=args.judge_arms.split(",") if args.judge_arms else None)
    if (run / "pages.jsonl").exists():
        summary = summarise(run, notes_stats)
        (run / "summary.md").write_text(summary + "\n")
        print("\n" + summary)
    print(f"\nrun dir: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
