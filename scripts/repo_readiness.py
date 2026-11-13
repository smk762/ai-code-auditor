#!/usr/bin/env python3
"""Scan-readiness + README-drift audit per ecosystem repo, at the tip of origin/dev.

    python scripts/repo_readiness.py --snapshot-dir /tmp/snap            # deterministic checks only
    python scripts/repo_readiness.py --snapshot-dir /tmp/snap --llm      # + Sonnet README-vs-code review
    python scripts/repo_readiness.py --repos argus-cortex,argus-lens --llm

Never touches working trees: each repo is `git fetch`ed (remote refs only) and origin/<branch>
is exported with `git archive` into --snapshot-dir. Writes reports/repo_readiness/<run>/:
<repo>.json (all findings), issues/<repo>.md (draft issue body), summary.md.

Deterministic checks: README age vs code churn, docs present, opsec hits in published docs
(private/link-local IPs, home/server paths, emails), env vars / HTTP routes / CLI entry points
the code defines but no doc mentions, `__all__` and docstring coverage, oversize or vendored files
inside the scanned tree, agent/planning files at the root.
LLM check (default Sonnet 5.5, effort high, full source): README claims that are stale, wrong or
missing. --model / --effort swap the reviewer; add new models' prices to auditor/spend_ledger.py.
The 2026-11-13 run over 22 repos cost $10.47 (~3M input tokens).
Spend is recorded on ledger lane "wiki" with note prefix "baseline:".

Draft issues quote opsec hits with their values; strip the values before posting to a public
repo (the 2026-11-13 filing kept only file:line and type).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from auditor.config import load_ecosystem_config  # noqa: E402
from auditor.repo_resolver import resolve_repo_path  # noqa: E402
from auditor.repo_scanner import IGNORE_DIRS, SOURCE_EXTENSIONS  # noqa: E402
from auditor.spend_ledger import get_ledger  # noqa: E402

LANE, NOTE = "wiki", "baseline: repo readiness audit"
MODEL, EFFORT = "claude-sonnet-5-5", "high"
MAX_FILE_CHARS = 200_000
SKIP_DIRS = {"vendor", "vendored", "third_party"}
DOC_NAMES = re.compile(r"^(README|CHANGELOG|CONTRIBUTING|DESIGN|spec)[^/]*\.md$|^docs/.+\.md$", re.I)
AGENT_FILES = {"CLAUDE.md", "AGENTS.md", "TODO.md", ".cursorrules"}

PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|169\.254)\.\d{1,3}\.\d{1,3}\b")
HOME_PATH = re.compile(r"(?<![\w.])(?:/home/[\w.-]+|/Users/[\w.-]+|/root/|/srv/|/mnt/[\w.-]+)")
EMAIL = re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(r"@(?:example\.(?:com|org)|users\.noreply\.github\.com)$|^noreply@", re.I)
PY_ENV = re.compile(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Z][A-Z0-9_]+)['"]|os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]\s*\]""")
JS_ENV = re.compile(r"process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[\s*['\"]([A-Z][A-Z0-9_]+)['\"]\s*\]")
PY_ROUTE = re.compile(r"""@\w+\.(get|post|put|patch|delete|websocket|route)\(\s*['"]([^'"]+)['"]""")
STARLETTE_ROUTE = re.compile(r"""\b(?:Route|WebSocketRoute|Mount)\(\s*['"]([^'"]+)['"]""")
CLI_CMD = re.compile(r"""(?:add_parser|\.command)\(\s*(?:name\s*=\s*)?['"]([\w-]+)['"]""")

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "readme_freshness": {"type": "integer"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["incorrect", "stale", "missing", "unclear"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "readme_says": {"type": "string"},
                    "code_shows": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["kind", "severity", "readme_says", "code_shows", "evidence", "fix"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["readme_freshness", "summary", "findings"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """You are reviewing whether a repository's README (and docs) still match its \
code at the tip of `dev`. The docs and the full source are below.

Report only concrete, checkable problems:
- incorrect: the docs state something the code contradicts (wrong flag, env var, default, route, \
command, function name, install step, version, behaviour).
- stale: the docs describe something that no longer exists or has been renamed.
- missing: an important user-facing capability, command, route, env var or config the code has \
and the docs never mention. Only things a user or integrator needs; not internals.
- unclear: the docs are ambiguous in a way that would lead a user to do the wrong thing.

For each: quote what the docs say (or "(not mentioned)"), state what the code shows, give \
evidence as path:Lx-Ly from the line-numbered source, and a one-line fix. Severity high = a user \
following the docs would fail or be misled; medium = notable gap; low = minor. Do not report \
style, tone, typos or formatting. Do not pad: if the docs are accurate, return few or no findings.
readme_freshness: 1 (badly out of date) to 5 (accurate and complete). summary: 2-3 sentences.

Deterministic pre-checks already found (you may confirm, refine or ignore them):
{prechecks}

{docs}

{source}"""


def sh(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def snapshot(repo, dest: Path) -> dict:
    """fetch + export origin/<branch> without touching the working tree."""
    src = resolve_repo_path(repo)
    branch = repo.branch or "dev"
    sh(["git", "fetch", "--quiet", "origin", branch], cwd=src)
    ref = f"origin/{branch}"
    sha = sh(["git", "rev-parse", ref], cwd=src)
    out = dest / repo.name
    if out.exists():
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True)
    data = subprocess.run(["git", "archive", "--format=tar", ref], cwd=src, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(out, filter="data")
    return {"path": out, "sha": sha, "ref": ref, "git": src}


def git_info(snap: dict) -> dict:
    g, ref = snap["git"], snap["ref"]
    readme = next((p.name for p in sorted(snap["path"].glob("README*"))), None)
    info: dict[str, Any] = {"readme": readme}
    if not readme:
        return info
    last = sh(["git", "log", "-1", "--format=%H %cI", ref, "--", readme], cwd=g)
    if last:
        rsha, rdate = last.split()
        info["readme_last_commit"] = rsha[:10]
        info["readme_last_date"] = rdate[:10]
        info["days_since_readme"] = (datetime.now(timezone.utc) - datetime.fromisoformat(rdate)).days
        info["code_commits_since_readme"] = int(sh(
            ["git", "rev-list", "--count", f"{rsha}..{ref}", "--", ".", f":!{readme}", ":!docs", ":!*.md"],
            cwd=g) or 0)
    return info


def walk(root: Path) -> tuple[list[Path], list[Path], list[str]]:
    src, docs, oversize = [], [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(root).parts
        if any(x in IGNORE_DIRS for x in parts):
            continue
        rel = p.relative_to(root).as_posix()
        if DOC_NAMES.search(rel) and len(parts) <= 2:
            docs.append(p)
        if p.suffix in SOURCE_EXTENSIONS:
            if SKIP_DIRS.intersection(parts):
                continue
            size = p.stat().st_size
            if size > MAX_FILE_CHARS:
                oversize.append(f"{rel} ({size // 1000} kB)")
                continue
            src.append(p)
    return src, docs, oversize


def prechecks(root: Path) -> dict:
    src, docs, oversize = walk(root)
    doc_text = "\n".join(d.read_text(encoding="utf-8", errors="replace") for d in docs)
    out: dict[str, Any] = {
        "docs": [d.relative_to(root).as_posix() for d in docs],
        "oversize_in_tree": oversize,
        "agent_files": sorted(f for f in AGENT_FILES if (root / f).exists()),
        "source_files": len(src),
    }
    opsec = []
    for d in docs:
        for i, line in enumerate(d.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rel = d.relative_to(root).as_posix()
            for rx, what in ((PRIVATE_IP, "private IP"), (HOME_PATH, "home/server path")):
                for m in rx.finditer(line):
                    opsec.append(f"{rel}:L{i} {what}: {m.group(0)}")
            for m in EMAIL.finditer(line):
                if not EMAIL_OK.search(m.group(0)):
                    opsec.append(f"{rel}:L{i} email: {m.group(0)}")
    out["opsec"] = opsec

    env, routes, cmds = {}, {}, {}
    py_files = mod_doc = 0
    pub_defs = pub_doc = 0
    init_all: dict[str, bool] = {}
    for f in src:
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = f.relative_to(root).as_posix()
        is_test = bool(re.search(r"(^|/)(tests?|__tests__)/|(^|/)test_|_test\.|\.(test|spec)\.", rel))
        if is_test:
            continue
        for m in PY_ENV.finditer(text) if f.suffix == ".py" else JS_ENV.finditer(text):
            name = m.group(1) or m.group(2)
            env.setdefault(name, f"{rel}:L{text.count(chr(10), 0, m.start()) + 1}")
        if f.suffix == ".py":
            for m in PY_ROUTE.finditer(text):
                routes.setdefault(f"{m.group(1).upper()} {m.group(2)}", f"{rel}:L{text.count(chr(10), 0, m.start()) + 1}")
            for m in STARLETTE_ROUTE.finditer(text):
                routes.setdefault(m.group(1), f"{rel}:L{text.count(chr(10), 0, m.start()) + 1}")
            for m in CLI_CMD.finditer(text):
                cmds.setdefault(m.group(1), f"{rel}:L{text.count(chr(10), 0, m.start()) + 1}")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            py_files += 1
            mod_doc += bool(ast.get_docstring(tree))
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("_"):
                    pub_defs += 1
                    pub_doc += bool(ast.get_docstring(n))
            if f.name == "__init__.py" and "/" in rel and rel.count("/") <= 2:
                init_all[rel] = any(isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets) for n in tree.body)

    def undocumented(items: dict[str, str], key=lambda k: k) -> dict[str, str]:
        return {k: v for k, v in sorted(items.items()) if key(k) not in doc_text}

    out["env_vars"] = len(env)
    out["undocumented_env"] = undocumented(env)
    out["routes"] = len(routes)
    out["undocumented_routes"] = undocumented(routes, key=lambda k: k.split()[-1])
    pyproject = root / "pyproject.toml"
    scripts = {}
    if pyproject.exists():
        import tomllib
        try:
            scripts = tomllib.loads(pyproject.read_text()).get("project", {}).get("scripts", {}) or {}
        except tomllib.TOMLDecodeError:
            pass
    out["entry_points"] = sorted(scripts)
    out["undocumented_entry_points"] = [s for s in sorted(scripts) if s not in doc_text]
    out["cli_subcommands"] = len(cmds)
    out["undocumented_subcommands"] = undocumented(cmds, key=lambda k: f" {k}")
    out["python_files"] = py_files
    out["module_docstring_pct"] = round(100 * mod_doc / py_files) if py_files else None
    out["public_def_docstring_pct"] = round(100 * pub_doc / pub_defs) if pub_defs else None
    out["package_init_without_all"] = sorted(k for k, v in init_all.items() if not v)
    return out


def numbered(text: str) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))


def llm_review(cl: anthropic.Anthropic, root: Path, pre: dict) -> dict:
    src, docs, _ = walk(root)
    docs_txt = "\n".join(f"===== DOC {d.relative_to(root).as_posix()} =====\n{numbered(d.read_text(encoding='utf-8', errors='replace'))}"
                         for d in docs) or "(no README or docs)"
    src_txt = "\n".join(f"===== {f.relative_to(root).as_posix()} =====\n{numbered(f.read_text(encoding='utf-8', errors='replace'))}"
                        for f in src)
    pre_txt = json.dumps({k: pre[k] for k in ("undocumented_env", "undocumented_routes",
                                             "undocumented_entry_points", "undocumented_subcommands")}, indent=1)
    t0 = time.time()
    try:
        with cl.messages.stream(
            model=MODEL, max_tokens=32000,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
            messages=[{"role": "user", "content": REVIEW_PROMPT.format(prechecks=pre_txt, docs=docs_txt, source=src_txt)}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    usd = get_ledger().record_usage(LANE, MODEL, msg.usage, note=NOTE)
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        review = json.loads(text)
    except json.JSONDecodeError:
        return {"error": "parse", "cost": usd}
    return {**review, "cost": usd, "input_tokens": msg.usage.input_tokens, "latency_s": round(time.time() - t0, 1)}


def issue_body(name: str, snap_sha: str, gi: dict, pre: dict, rev: dict | None, public: bool) -> str:
    L = [f"Audited at `dev` @ `{snap_sha[:10]}` by ai-code-auditor's scan-readiness check "
         f"(smk762/ai-code-auditor#8). The suite DeepWiki"
         + (" and the docs imported into the demo UIs (smk762/argus-studio#169) read" if public else " reads")
         + " this repo's README, docs and code, so drift here shows up there.", ""]
    if gi.get("readme"):
        L.append(f"README last changed {gi.get('readme_last_date', '?')} (`{gi.get('readme_last_commit', '?')}`); "
                 f"{gi.get('code_commits_since_readme', '?')} code commits on `dev` since.")
    else:
        L.append("**No README.**")
    if rev and "findings" in rev:
        L.append(f"README accuracy: {rev['readme_freshness']}/5. {rev['summary']}")
    L.append("")
    if rev and rev.get("findings"):
        L += ["## README / docs vs code", ""]
        order = {"high": 0, "medium": 1, "low": 2}
        for f in sorted(rev["findings"], key=lambda f: order[f["severity"]]):
            L.append(f"- [ ] **{f['severity']} · {f['kind']}**: {f['fix']}  \n"
                     f"  Docs: {f['readme_says']}  \n  Code: {f['code_shows']} (`{f['evidence']}`)")
        L.append("")
    if pre["opsec"]:
        L += ["## Opsec (published docs must pass, see argus-studio#169 Approach 6)", ""]
        L += [f"- [ ] `{x}` → replace with a placeholder (`<GPU_HOST>`, `<LAN_HOST>`, `<AGENT_HOST>`)" for x in pre["opsec"]]
        L.append("")
    undoc = [("env vars", pre["undocumented_env"]), ("HTTP routes", pre["undocumented_routes"]),
             ("CLI subcommands", pre["undocumented_subcommands"])]
    if any(v for _, v in undoc) or pre["undocumented_entry_points"]:
        L += ["## Defined in code, not mentioned in any doc", "",
              "Document the user-facing ones; internal ones can stay undocumented (tick them off).", ""]
        for label, items in undoc:
            if items:
                L.append(f"- {label}: " + ", ".join(f"`{k}` ({v})" for k, v in list(items.items())[:25])
                         + (f" … and {len(items) - 25} more" if len(items) > 25 else ""))
        if pre["undocumented_entry_points"]:
            L.append("- console scripts: " + ", ".join(f"`{s}`" for s in pre["undocumented_entry_points"]))
        L.append("")
    struct = []
    if pre["package_init_without_all"]:
        struct.append("Add `__all__` to " + ", ".join(f"`{p}`" for p in pre["package_init_without_all"])
                      + " so the public API is explicit (the wiki reads it to say what is re-exported).")
    if pre["module_docstring_pct"] is not None and pre["module_docstring_pct"] < 60:
        struct.append(f"Module docstrings: {pre['module_docstring_pct']}% of Python files. A one-line purpose "
                      "per module makes the per-file notes much more accurate.")
    if pre["oversize_in_tree"]:
        struct.append("Move vendored/generated files out of the source tree (into `vendor/`) or exclude them: "
                      + ", ".join(f"`{x}`" for x in pre["oversize_in_tree"]))
    if not pre["docs"] or pre["docs"] == [gi.get("readme")]:
        struct.append("Consider a `docs/` folder for reference material (CLI, config, API) so the README "
                      "stays an overview; #169 imports an explicit allowlist of README sections + `docs/*.md`.")
    if struct:
        L += ["## Make it easier to scan", ""] + [f"- [ ] {s}" for s in struct] + [""]
    L.append("Agent/planning files (`CLAUDE.md`, `AGENTS.md`, `TODO.md`) are never published or used as wiki input; "
             "nothing to do for them.")
    return "\n".join(L)


def main() -> int:
    global MODEL, EFFORT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", default="all")
    ap.add_argument("--snapshot-dir", required=True)
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--visibility", help="JSON file {repo: PUBLIC|PRIVATE}")
    args = ap.parse_args()
    MODEL, EFFORT = args.model, args.effort

    repos = [r for r in load_ecosystem_config().repos if r.enabled
             and (args.repos == "all" or r.name in args.repos.split(","))]
    vis = json.loads(Path(args.visibility).read_text()) if args.visibility else {}
    run = ROOT / "reports" / "repo_readiness" / datetime.now().strftime("%Y%m%d-%H%M%S")
    (run / "issues").mkdir(parents=True)
    snapdir = Path(args.snapshot_dir)
    cl = anthropic.Anthropic(max_retries=4) if args.llm else None

    def work(repo):
        snap = snapshot(repo, snapdir)
        gi = git_info(snap)
        pre = prechecks(snap["path"])
        rev = llm_review(cl, snap["path"], pre) if (cl and pre["source_files"]) else None
        return repo.name, snap["sha"], gi, pre, rev

    rows = []
    with ThreadPoolExecutor(args.concurrency) as pool:
        for fut in as_completed([pool.submit(work, r) for r in repos]):
            try:
                name, sha, gi, pre, rev = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {exc}", flush=True)
                continue
            public = vis.get(name) == "PUBLIC" and name.startswith("argus-")
            (run / f"{name}.json").write_text(json.dumps({"sha": sha, "git": gi, "prechecks": pre, "review": rev}, indent=1))
            (run / "issues" / f"{name}.md").write_text(issue_body(name, sha, gi, pre, rev, public))
            nf = len(rev.get("findings", [])) if rev else 0
            hi = sum(f["severity"] == "high" for f in rev.get("findings", [])) if rev else 0
            rows.append((name, sha, gi, pre, rev, nf, hi))
            print(f"  {name:20} files={pre['source_files']:4} opsec={len(pre['opsec'])} "
                  f"readme={gi.get('readme') or '-'} findings={nf} (high {hi}) "
                  f"{('$%.2f' % rev.get('cost', 0)) if rev else ''} {rev.get('error', '') if rev else ''}", flush=True)

    rows.sort()
    L = ["# Repo scan-readiness audit\n",
         "| Repo | dev sha | README age | Code commits since | Accuracy | Findings (high) | Opsec | "
         "Undoc env | Undoc routes | Mod docstrings | Oversize |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    total = 0.0
    for name, sha, gi, pre, rev, nf, hi in rows:
        total += rev.get("cost", 0) if rev else 0
        L.append(f"| {name} | `{sha[:8]}` | {gi.get('days_since_readme', '–')}d | {gi.get('code_commits_since_readme', '–')} | "
                 f"{rev.get('readme_freshness', '–') if rev else '–'} | {nf} ({hi}) | {len(pre['opsec'])} | "
                 f"{len(pre['undocumented_env'])}/{pre['env_vars']} | {len(pre['undocumented_routes'])}/{pre['routes']} | "
                 f"{pre['module_docstring_pct'] if pre['module_docstring_pct'] is not None else '–'}% | {len(pre['oversize_in_tree'])} |")
    L.append(f"\nLLM review: {MODEL} @ {EFFORT}, full source at origin/dev. Cost ${total:.2f}.")
    (run / "summary.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nrun dir: {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
