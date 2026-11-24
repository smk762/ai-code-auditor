"""The scan set (config/ecosystem.yaml) and the sync set (config/repo_mirrors.yaml) list the same repos.

A repo in only one of them is either scanned from a mirror nothing syncs, or synced and never scanned
(which is how repos went missing from the findings page).

Every scanned repo is also described wherever the auditor lists repos: a row in the ecosystem.md table
(the scan prompt's repo context and the topology page read it), a line in its flow diagram, a
``service_roles`` entry (role contracts and the service topology) and, for argus repos, a brand stage
(the topology node's colour; a repo without one is drawn grey as "external")."""

import re
from pathlib import Path

import yaml

from auditor import brand
from auditor.ecosystem_briefing import _load_repo_roles
from auditor.ui_topology import parse_ecosystem_doc

ROOT = Path(__file__).resolve().parents[1]


def _names(path: str, key: str) -> dict[str, str]:
    data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return {r["name"]: r["path"] for r in data[key]}


def test_scan_set_and_mirror_set_match() -> None:
    scan = _names("config/ecosystem.yaml", "repos")
    mirrors = _names("config/repo_mirrors.yaml", "mirrors")
    assert scan == mirrors  # same names and the same paths


def test_this_repo_is_scanned_from_a_mirror_not_the_working_checkout() -> None:
    assert _names("config/ecosystem.yaml", "repos")["ai-code-auditor"] == "~/mirrors/ai-code-auditor"


def test_generated_docs_bundle_is_not_scanned() -> None:
    assert "argus-docs" not in _names("config/ecosystem.yaml", "repos")


# Scanned repos that are not argus packages: shared deps and this repo. They have no brand stage by design.
NOT_ARGUS = {"agent-composer", "voluptas", "ai-code-auditor"}


def _scanned() -> list[str]:
    return list(_names("config/ecosystem.yaml", "repos"))


def test_every_scanned_repo_has_an_ecosystem_table_row() -> None:
    doc = (ROOT / "ecosystem" / "ecosystem.md").read_text(encoding="utf-8")
    topology_rows, _ = parse_ecosystem_doc(doc)
    prompt_rows = _load_repo_roles()
    assert [r for r in _scanned() if r not in topology_rows] == []
    assert [r for r in _scanned() if r not in prompt_rows] == []


def test_every_scanned_repo_is_in_the_flow_diagram() -> None:
    doc = (ROOT / "ecosystem" / "ecosystem.md").read_text(encoding="utf-8")
    flow = re.search(r"^## Ecosystem flow\b.*?```(.*?)```", doc, re.S | re.M).group(1)
    words = set(re.findall(r"[a-z][a-z0-9-]*", flow))
    assert [r for r in _scanned() if r not in words and r.removeprefix("argus-") not in words] == []


def test_every_scanned_repo_has_a_service_role() -> None:
    rules = yaml.safe_load((ROOT / "config" / "architecture_rules.yaml").read_text(encoding="utf-8"))
    roles = {e["name"] for e in rules["service_roles"]}
    assert [r for r in _scanned() if r not in roles] == []


def test_the_flow_draws_exactly_the_allowed_calls_into_retina() -> None:
    # A "a / b ──► c" line links every member of the group, so the diagram's arrows into retina
    # must name the repos allowed_calls lets call it: no more (a phantom edge), no fewer.
    doc = (ROOT / "ecosystem" / "ecosystem.md").read_text(encoding="utf-8")
    _, flow = parse_ecosystem_doc(doc)
    drawn = {f"argus-{a}" for a, b in flow if b == "retina"}
    rules = yaml.safe_load((ROOT / "config" / "architecture_rules.yaml").read_text(encoding="utf-8"))
    allowed = {c["from"] for c in rules["call_graph_rules"]["allowed_calls"] if c["to"] == "argus-retina"}
    assert drawn == allowed
    assert drawn  # the diagram does draw them


def test_the_flow_draws_exactly_the_allowed_calls_out_of_iris() -> None:
    # iris's resident targets are deployment config; the diagram must draw the ones allowed_calls
    # records (those deploy/iris.example.yaml wires) and no others.
    doc = (ROOT / "ecosystem" / "ecosystem.md").read_text(encoding="utf-8")
    _, flow = parse_ecosystem_doc(doc)
    scanned = set(_scanned())
    drawn = {b if b in scanned else f"argus-{b}" for a, b in flow if a == "iris"}
    rules = yaml.safe_load((ROOT / "config" / "architecture_rules.yaml").read_text(encoding="utf-8"))
    allowed = {c["to"] for c in rules["call_graph_rules"]["allowed_calls"] if c["from"] == "argus-iris"}
    assert drawn == allowed
    assert drawn


def test_every_scanned_argus_repo_has_a_brand_stage() -> None:
    scanned = _scanned()
    assert {r for r in scanned if not r.startswith("argus-")} == NOT_ARGUS
    assert [r for r in scanned if r not in NOT_ARGUS and brand.stage_of(r) is None] == []
    # no stale entries, and a repo is either marked or placed, never both
    listed = set(brand.MARKS) | set(brand.STAGE_OF_UNMARKED)
    assert listed == {r.removeprefix("argus-") for r in scanned if r not in NOT_ARGUS}
    assert not set(brand.MARKS) & set(brand.STAGE_OF_UNMARKED)
