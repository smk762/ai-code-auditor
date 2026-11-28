"""pr_model: "Depends on: #12" and friends parse like the bare forms."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(("body", "verb"), [
    ("Depends on: #9", "depends"),
    ("Blocked by: smk762/argus-halo#9", "depends"),
    ("Stacked on: #9", "stacked"),
    ("Stacked on top of: #9", "stacked"),
    ("Requires - #9", "depends"),
], ids=["depends_colon", "blocked_colon", "stacked_colon", "stacked_top_colon", "requires_dash"])
def test_colon_forms_parse(body: str, verb: str) -> None:
    from auditor.pr_model import parse_dependencies

    deps = parse_dependencies(body, owner="smk762", repo="argus-halo")
    assert [(d["slug"], d["number"], d["verb"]) for d in deps] == [("smk762/argus-halo", 9, verb)]


@pytest.mark.parametrize("body", ["Blocks: argus-lens#96", "Required by: argus-lens#96"],
                         ids=["blocks_colon", "required_by_colon"])
def test_colon_forms_parse_for_dependants(body: str) -> None:
    from auditor.pr_model import parse_dependants

    deps = parse_dependants(body, owner="smk762", repo="argus-halo")
    assert [(d["slug"], int(d["number"])) for d in deps] == [("smk762/argus-lens", 96)]
