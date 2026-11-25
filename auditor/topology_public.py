"""Public export of the ecosystem topology: one gated JSON file for dragonhound.dev/topology/.

The internal topology (``auditor/ui_topology.py``) names private repos, ops hosts and policy violations,
so it is never copied. This builds the public topology from the same scan, with a tighter rule at each step:

* Nodes are only repos GitHub reports public (a failed lookup counts as private) and known external
  services. A repo's text is its GitHub description, which is already public, or our ecosystem doc's role when it has none (the gate still reads it).
* A service a private repo provides (ComfyUI) is drawn as that software; ``mithrandir`` is left out.
* No violations, facets, findings or pack ids.
* Evidence skips tests, fixtures and this repo's own service catalogue, whose patterns match themselves,
  and links pin the commit that was scanned. A mirror with local changes, or whose HEAD isn't on
  ``origin/dev``, stops the export: its line numbers would point at code nobody else can see.

The opsec gate (``wiki.public.gate``) runs over the finished file and refuses it whole.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auditor import ui_topology as ut
from auditor.brand import STAGES, stage_of
from wiki.public import OWNER, gate, load_denylist, private_repo_names

GENERATOR = "ai-code-auditor topology export"
PRIVATE_SERVICES = frozenset({"mithrandir"})
_NOT_PRODUCT = re.compile(r"(^|/)(tests?|fixtures?|__tests__|e2e|examples?)/|\.(test|spec)\.[jt]sx?$|(^|/)(test_[^/]*|conftest)\.py$")
# files here that list service names as data, so every service pattern matches them
CATALOGUE = {"ai-code-auditor": frozenset({"auditor/ui_topology.py", "auditor/topology_public.py", "auditor/ecosystem_briefing.py",
                                           "auditor/agent_pack.py", "scripts/build_service_icons.py", "wiki/citations.py",
                                           "wiki/pages.py", "wiki/public.py"})}

Info = Callable[[str], "dict[str, Any] | None"]


def github_info(repo: str, token: str = "") -> dict[str, Any] | None:
    """{visibility, description} from the GitHub API; None if the lookup fails (treated as private)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{OWNER}/{repo}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-code-auditor",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return {"visibility": d.get("visibility"), "description": d.get("description") or ""}
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None


def _git(path: str, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout.strip()


def published_head(path: str) -> str | None:
    """The scanned commit, or None when its files aren't what ``origin/dev`` holds."""
    try:
        rc, sha = _git(path, "rev-parse", "--verify", "HEAD")
        if rc or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        if _git(path, "merge-base", "--is-ancestor", sha, "origin/dev")[0]:
            return None
        rc, dirty = _git(path, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if rc or dirty else sha


def _evidence(repo: str, hits: list[dict[str, Any]], ok: Callable[[str], bool]) -> list[dict[str, Any]]:
    skip = CATALOGUE.get(repo, frozenset())
    return [h for h in hits if h["path"] not in skip and not _NOT_PRODUCT.search(h["path"]) and ok(h["path"])]


def build(repos: list[Any], info: dict[str, dict[str, Any] | None], doc: str,
          ok: Callable[[str], bool] = lambda _t: True) -> dict[str, Any]:
    """The public topology for *repos* (every configured repo), given GitHub's answer for each."""
    public = [r for r in repos if r.enabled and (info.get(r.name) or {}).get("visibility") == "public"]
    heads = {r.name: published_head(r.path) for r in public}
    if bad := sorted(n for n, h in heads.items() if h is None):
        raise RuntimeError(f"not exporting: no clean commit on origin/dev in the mirror of {', '.join(bad)}")
    names = set(heads)
    meta, flow = ut.parse_ecosystem_doc(doc)
    imports, uses, files = ut._scan(public)

    nodes: list[dict[str, Any]] = [
        {"id": r.name, "label": r.name.removeprefix("argus-"), "kind": "repo", "stage": stage_of(r.name) or "external",
         "area": meta.get(r.name, {}).get("area", ""), "description": (info[r.name] or {}).get("description") or meta.get(r.name, {}).get("role", ""),
         "files": files.get(r.name, 0), "github": f"https://github.com/{OWNER}/{r.name}", "head": heads[r.name]}
        for r in public]
    edges: list[dict[str, Any]] = []
    for a, b in flow:
        src, dst = ut._resolve(a, names), ut._resolve(b, names)
        if src and dst and src != dst:
            edges.append({"source": src, "target": dst, "kind": "flow"})
    for a, targets in imports.items():
        for b, hits in targets.items():
            if ev := _evidence(a, hits, ok):
                edges.append({"source": a, "target": b, "kind": "imports", "files": len(ev), "evidence": ev[:ut.MAX_EVIDENCE]})
    services: set[str] = set()
    for a, svcs in uses.items():
        for sid, hits in svcs.items():
            provider = ut.SERVICES[sid][4]
            target = provider if provider in names else f"svc:{sid}"
            ev = _evidence(a, hits, ok)
            if sid in PRIVATE_SERVICES or target == a or len(ev) < ut.MIN_FILES:
                continue
            if target.startswith("svc:"):
                services.add(sid)
            edges.append({"source": a, "target": target, "kind": "uses", "service": ut.SERVICES[sid][0],
                          "files": len(ev), "evidence": ev[:ut.MAX_EVIDENCE]})
    nodes += [{"id": f"svc:{s}", "label": ut.SERVICES[s][0], "kind": "service", "category": ut.SERVICES[s][1]}
              for s in sorted(services)]
    nodes.sort(key=lambda n: n["id"])
    edges.sort(key=lambda e: (e["kind"], e["source"], e["target"], e.get("service", "")))
    return {"generator": GENERATOR, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "min_files": ut.MIN_FILES, "stages": {k: v[0] for k, v in STAGES.items()},
            "counts": dict(Counter(e["kind"] for e in edges)), "nodes": nodes, "edges": edges}


def export(out: str | Path, *, denylist_path: str | Path | None = None, info: Info | None = None) -> dict[str, Any]:
    """Write the public topology to *out* and return it. Raises, writing nothing, when the gate refuses it."""
    from auditor.config import load_ecosystem_config
    from auditor.merge_poll import github_token

    denylist = load_denylist(denylist_path)  # fail closed before any lookup
    dest = Path(out)
    if dest.exists() and GENERATOR not in dest.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"refusing to replace {dest}: it is not a previous topology export")
    token = github_token()
    lookup = info or (lambda r: github_info(r, token))
    repos = load_ecosystem_config().repos
    found = {r.name: lookup(r.name) for r in repos}
    private = private_repo_names({n: (d or {}).get("visibility") for n, d in found.items()}) + sorted(PRIVATE_SERVICES)
    doc = ut.ECOSYSTEM_DOC.read_text(encoding="utf-8") if ut.ECOSYSTEM_DOC.is_file() else ""
    data = build(repos, found, doc, lambda text: gate(text, denylist, private) is None)
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if hit := gate(text, denylist, private):
        raise RuntimeError(f"opsec gate refused the topology ({hit[0]}: {hit[1]}); nothing written")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    return data
