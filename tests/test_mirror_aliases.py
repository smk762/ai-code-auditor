from __future__ import annotations

import os
from pathlib import Path

from conductor.mirrors import ensure_checkout_aliases


def _cfg(tmp_path: Path) -> dict:
    home, mirrors = tmp_path / "home", tmp_path / "home" / "mirrors"
    (mirrors / "argus-cortex").mkdir(parents=True)
    (home / "argus-lens").mkdir()
    return {
        "mirror_root": str(mirrors),
        "aliases": {"argus-core": "argus-cortex"},
        "mirrors": [
            {"name": "argus-cortex", "path": str(mirrors / "argus-cortex")},
            {"name": "argus-lens", "path": str(home / "argus-lens")},
        ],
    }


def test_alias_created_next_to_every_mirror_dir_and_idempotent(tmp_path: Path):
    cfg = _cfg(tmp_path)
    (tmp_path / "home/mirrors/argus-cortex/SUITE.md").write_text("map")
    results = ensure_checkout_aliases(cfg)
    assert all(r.ok for r in results) and len(results) == 2
    # Both a mirrors/ sibling and a ~/ sibling resolve the suite import.
    assert (tmp_path / "home/argus-lens/../argus-core/SUITE.md").read_text() == "map"
    assert (tmp_path / "home/mirrors/argus-core/SUITE.md").read_text() == "map"
    assert os.readlink(tmp_path / "home/mirrors/argus-core") == "argus-cortex"  # relative
    assert {r.action for r in ensure_checkout_aliases(cfg)} == {"alias-ok"}


def test_alias_repoints_stale_symlink_but_never_touches_real_dir(tmp_path: Path):
    cfg = _cfg(tmp_path)
    (tmp_path / "home/mirrors/argus-core").symlink_to("nowhere")
    real = tmp_path / "home/argus-core"
    real.mkdir()
    (real / "keep.txt").write_text("mine")
    by_path = {r.path: r for r in ensure_checkout_aliases(cfg)}
    assert os.readlink(tmp_path / "home/mirrors/argus-core") == "argus-cortex"
    assert by_path[str(real)].action == "alias-kept-existing"
    assert (real / "keep.txt").read_text() == "mine"


def test_unknown_alias_target_is_reported(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg["aliases"] = {"argus-core": "argus-nope"}
    (r,) = ensure_checkout_aliases(cfg)
    assert not r.ok and "argus-nope" in r.error


def test_no_aliases_no_op(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.pop("aliases")
    assert ensure_checkout_aliases(cfg) == []
