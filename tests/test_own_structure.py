"""This repo's own module cycles may only shrink (#154)."""
from __future__ import annotations

from pathlib import Path

from auditor import structure

KNOWN: list[set[str]] = []  # none left; it stays empty


def test_no_new_module_cycle_in_this_repo() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = {str(p.relative_to(root)): p.read_text(encoding="utf-8")
               for d in ("auditor", "conductor", "ecosystem", "patterns", "pipelines", "semantic", "wiki")
               for p in sorted((root / d).rglob("*.py"))}
    r = structure.scan_sources({k: v for k, v in sources.items() if structure.in_scope(k)})
    assert not r["unparsed"]
    new = [c["members"] for c in r["cycles"] if not any(set(c["members"]) <= k for k in KNOWN)]
    assert not new, f"new module cycle (imports inside functions count): {new}"
