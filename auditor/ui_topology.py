"""Ecosystem map: how the argus packages and the services around them connect.

Edges come from four sources, each kept as its own kind so the page can toggle them:

* ``flow``: the documented pipeline, parsed from the diagram in ``ecosystem/ecosystem.md``
  (horizontal ``──►`` arrows and vertical ``│ … ▼`` drops).
* ``imports``: repo A's source imports repo B's Python package (``import argus_cortex``).
* ``uses``: repo A's source mentions a known external service (catalogue below), with up to
  three file:line examples as evidence. Services one repo of the suite provides (ComfyUI →
  voluptas, rag-chat/rag-ingest → agent-composer) point at that repo instead.
* ``violation``: an architecture-policy violation from ``service_topology.json``.

Scanning every mirror takes a few seconds, so the result is cached until a mirror's HEAD,
the ecosystem doc or the topology sidecar changes.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ECOSYSTEM_DOC = Path(__file__).resolve().parents[1] / "ecosystem" / "ecosystem.md"
MIN_FILES = 2  # a service mentioned in a single file is shown in details, not drawn
MAX_EVIDENCE = 3

# Areas in the ecosystem doc → colour group on the map.
GROUPS = {
    "pipeline": ("Image pipeline", {"acquisition", "curation", "curation demo", "captioning", "gallery", "studio ui",
                                    "guarded gen", "gen demo", "training bridge", "eval"}),
    "verticals": ("Verticals", {"maps", "survey", "survey ui", "provenance", "review ui", "face / tts face"}),
    "platform": ("Platform", {"taxonomy / ai contract", "infra", "ci", "package template"}),
    "shared": ("Shared services", {"chat / rag", "comfyui"}),
}

# id → (label, category, side, regex, provider repo or None)
SERVICES: dict[str, tuple[str, str, str, str, str | None]] = {
    "comfyui": ("ComfyUI", "AI inference", "left", r"(?i)comfy", "voluptas"),
    "rag": ("rag-chat / rag-ingest", "AI inference", "left", r"(?i)rag[-_]?(chat|ingest)|:9150\b|:9050\b", "agent-composer"),
    "ollama": ("Ollama", "AI inference", "left", r"(?i)ollama|:11434\b", None),
    "openai": ("OpenAI-compatible API", "AI inference", "left", r"(?i)api\.openai|\bopenai\b", None),
    "anthropic": ("Anthropic API", "AI inference", "left", r"(?i)\banthropic\b", None),
    "nudenet": ("NudeNet", "Moderation", "left", r"(?i)nudenet", None),
    "singguard": ("SingGuard", "Moderation", "left", r"(?i)sing_?guard", None),
    "qdrant": ("Qdrant", "Data stores", "right", r"(?i)qdrant|:6333\b", None),
    "postgres": ("Postgres", "Data stores", "right", r"(?i)psycopg|postgres(ql)?://|asyncpg", None),
    "redis": ("Redis", "Data stores", "right", r"(?i)\bredis\b", None),
    "s3": ("S3 / MinIO", "Data stores", "right", r"(?i)minio|boto3", None),
    "ipfs": ("IPFS", "Chain", "right", r"(?i)\bipfs\b|pinata", None),
    "ethereum": ("Ethereum (ERC-721)", "Chain", "right", r"(?i)\bweb3\b|erc-?721|\bethers\b", None),
    "mithrandir": ("mithrandir", "Ops", "right", r"(?i)mithrandir", None),
    "huggingface": ("Hugging Face", "ML tooling", "top", r"(?i)huggingface|hf_hub", None),
    "diffusers": ("diffusers", "ML tooling", "top", r"\bdiffusers\b", None),
    "trainers": ("kohya / OneTrainer", "ML tooling", "top", r"(?i)kohya|onetrainer", None),
    "geo": ("GDAL / PDAL", "Geo tooling", "top", r"(?i)\bpdal\b|\bgdal\b|osgeo|rasterio", None),
}
_SERVICE_RE = {k: re.compile(v[3]) for k, v in SERVICES.items()}

_cache: dict[str, Any] = {"key": None, "data": None}
_lock = threading.Lock()


# ── ecosystem doc ────────────────────────────────────────────────────────────

def parse_ecosystem_doc(text: str) -> tuple[dict[str, dict[str, str]], list[tuple[str, str]]]:
    """(repo → {area, role, url}, documented flow edges as (from, to) short names)."""
    repos: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"^\|\s*([^|]+?)\s*\|\s*\[([\w.-]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*$", text, re.M):
        repos[m.group(2)] = {"area": m.group(1), "url": m.group(3), "role": m.group(4)}
    flow: list[tuple[str, str]] = []
    block = re.search(r"## Ecosystem flow\s*```\n(.*?)```", text, re.S)
    if not block:
        return repos, flow
    grid = block.group(1).splitlines()

    def names_in(segment: str) -> list[str]:
        segment = re.sub(r"\([^)]*\)", " ", segment)
        return [w for w in re.findall(r"[a-z][a-z0-9-]*", segment)]

    for line in grid:
        if "──►" not in line or ":" in line.split("──►")[0]:
            continue  # skip "Shared AI / RAG:" style legend lines
        parts = [names_in(p) for p in line.split("──►")]
        parts = [p for p in parts if p]  # "(train)" drops out, chaining forge → proof
        for left, right in zip(parts, parts[1:]):
            flow += [(a, b) for a in left for b in right]

    def word_at(row: int, col: int) -> str | None:
        if not (0 <= row < len(grid)) or col >= len(grid[row]):
            return None
        line = grid[row]
        if not re.match(r"[a-z0-9-]", line[col]):
            return None
        a = col
        while a > 0 and re.match(r"[a-z0-9-]", line[a - 1]):
            a -= 1
        b = col
        while b < len(line) and re.match(r"[a-z0-9-]", line[b]):
            b += 1
        return line[a:b]

    for r, line in enumerate(grid):
        for c, ch in enumerate(line):
            if ch != "▼":
                continue
            up = r - 1
            while up >= 0 and c < len(grid[up]) and grid[up][c] == "│":
                up -= 1
            src, dst = word_at(up, c), word_at(r + 1, c)
            if src and dst:
                flow.append((src, dst))
    return repos, list(dict.fromkeys(flow))


def _resolve(short: str, names: set[str]) -> str | None:
    for cand in (short, f"argus-{short}"):
        if cand in names:
            return cand
    return None


# ── code scan ────────────────────────────────────────────────────────────────

def _scan(repos: list[Any]) -> tuple[dict[str, dict[str, list]], dict[str, dict[str, list]], dict[str, int]]:
    """(imports[a][b] = evidence, uses[a][service] = evidence, files scanned per repo)."""
    from auditor.repo_resolver import resolve_scan_root
    from wiki.sources import MAX_FILE_CHARS, read_text, source_files

    modules = {r.name: r.name.replace("-", "_") for r in repos}
    import_re = {n: re.compile(rf"^\s*(?:from|import)\s+{m}\b", re.M) for n, m in modules.items()}
    imports: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    uses: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    files: Counter = Counter()
    for repo in repos:
        try:
            root = resolve_scan_root(repo)
            paths = source_files(repo)
        except Exception:  # noqa: BLE001 — a missing mirror just has no edges
            continue
        for f in paths:
            rel = f.relative_to(root).as_posix()
            if rel.startswith("tests/") or "/tests/" in rel or Path(rel).name.startswith("test_"):
                continue
            text = read_text(f)
            if text is None or len(text) > MAX_FILE_CHARS:
                continue
            files[repo.name] += 1
            for other, rx in import_re.items():
                if other != repo.name and (m := rx.search(text)):
                    imports[repo.name][other].append({"path": rel, "line": text.count("\n", 0, m.start()) + 1})
            for sid, rx in _SERVICE_RE.items():
                if m := rx.search(text):
                    uses[repo.name][sid].append({"path": rel, "line": text.count("\n", 0, m.start()) + 1})
    return imports, uses, dict(files)


def _head(path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ── assembly ─────────────────────────────────────────────────────────────────

def _group(area: str) -> str:
    a = area.strip().lower()
    return next((g for g, (_, areas) in GROUPS.items() if a in areas), "other")


def topology_data(output_dir: str | Path) -> dict[str, Any]:
    from auditor.config import load_ecosystem_config

    out = Path(output_dir)
    repos = [r for r in load_ecosystem_config().repos if r.enabled]
    side = out / "service_topology.json"
    key = (tuple((r.name, _head(r.path)) for r in repos),
           ECOSYSTEM_DOC.stat().st_mtime if ECOSYSTEM_DOC.is_file() else 0,
           side.stat().st_mtime if side.is_file() else 0)
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
    data = _build(out, repos, side)
    with _lock:
        _cache.update(key=key, data=data)
    return data


def _build(out: Path, repos: list[Any], side: Path) -> dict[str, Any]:
    from auditor.merge_poll import load_state, merge_poll_state_path

    names = {r.name for r in repos}
    doc = ECOSYSTEM_DOC.read_text(encoding="utf-8") if ECOSYSTEM_DOC.is_file() else ""
    meta, flow_short = parse_ecosystem_doc(doc)
    topo = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    node_info = {n.get("name"): n for n in topo.get("nodes") or []}
    try:
        poll = load_state(merge_poll_state_path(out)).get("repos") or {}
    except Exception:  # noqa: BLE001
        poll = {}
    imports, uses, files = _scan(repos)

    nodes: list[dict[str, Any]] = []
    for r in repos:
        m = meta.get(r.name, {})
        slug = (poll.get(r.name) or {}).get("slug")
        nodes.append({
            "id": r.name, "label": r.name.removeprefix("argus-"), "kind": "repo",
            "group": _group(m.get("area", "")), "area": m.get("area", ""), "role": m.get("role", ""),
            "service_role": (node_info.get(r.name) or {}).get("role"),
            "violations": int((node_info.get(r.name) or {}).get("violation_count") or 0),
            "files": files.get(r.name, 0),
            "wiki": (out / "wiki" / r.name / "INDEX.json").is_file(),
            "github": f"https://github.com/{slug}" if slug else m.get("url"),
            "head": (poll.get(r.name) or {}).get("sha"),
        })

    edges: list[dict[str, Any]] = []
    for a, b in flow_short:
        src, dst = _resolve(a, names), _resolve(b, names)
        if src and dst and src != dst:
            edges.append({"source": src, "target": dst, "kind": "flow", "weight": 1, "label": "documented flow"})
    for a, targets in imports.items():
        for b, ev in targets.items():
            edges.append({"source": a, "target": b, "kind": "imports", "weight": len(ev),
                          "label": f"imports {b.replace('-', '_')}", "evidence": ev[:MAX_EVIDENCE]})

    used_services: set[str] = set()
    mentions: list[dict[str, Any]] = []
    for a, svcs in uses.items():
        for sid, ev in svcs.items():
            label, category, _side, _rx, provider = SERVICES[sid]
            target = provider if provider in names else f"svc:{sid}"
            if target == a:
                continue  # a repo "using" the service it provides
            rec = {"source": a, "target": target, "kind": "uses", "weight": len(ev), "service": sid,
                   "label": f"uses {label}", "evidence": ev[:MAX_EVIDENCE]}
            if len(ev) >= MIN_FILES:
                edges.append(rec)
                used_services.add(sid)
            else:
                mentions.append(rec)
    for sid in sorted(used_services):
        label, category, side_, _rx, provider = SERVICES[sid]
        if provider in names:
            continue
        nodes.append({"id": f"svc:{sid}", "label": label, "kind": "service", "category": category, "side": side_})

    for e in topo.get("edges") or []:
        if "violation" in str(e.get("kind")) and e.get("from") in names and e.get("to") in names:
            edges.append({"source": e["from"], "target": e["to"], "kind": "violation", "weight": 1,
                          "label": str(e.get("kind")).replace("_", " ")})

    groups = {g: label for g, (label, _) in GROUPS.items()} | {"other": "Other"}
    return {
        "nodes": nodes, "edges": edges, "mentions": mentions, "groups": groups,
        "counts": dict(Counter(e["kind"] for e in edges)),
        "topology_generated_at": topo.get("generated_at"),
        "min_files": MIN_FILES,
    }
