"""revalidate: a pack with no work_item_id is never a candidate (batch 5)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auditor import revalidate as rv
from tests.test_revalidate import _pack


@pytest.mark.parametrize("pid", [None, ""], ids=["null", "empty"])
def test_a_pack_without_an_id_is_skipped(tmp_path: Path, pid) -> None:
    out = tmp_path / "reports"
    _pack(out, "pack-good", "unverified")
    (out / "agent_packs" / "pack-noid.yaml").write_text(yaml.safe_dump({
        "work_item_id": pid, "kind": "nightly", "status": "unverified", "repo": {"name": "r"},
        "location": {"file_path": "/m/r/app/calc.py"}}))
    got = [c.pack_id for c in rv.candidates(out)]
    assert got == ["pack-good"]
    assert "None" not in got
