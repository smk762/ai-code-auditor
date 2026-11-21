"""Architecture contracts (#57): rules validated at load, and each rule proven on a seeded violation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auditor.agent_pack import architecture_findings
from auditor.config import load_architecture_rules
from ecosystem import arch_contracts
from ecosystem.arch_contracts import ImportSite, fixtures, repo_dependencies
from ecosystem.architecture_engine import ArchitectureEngine
from ecosystem.graph_builder import GraphBuilder

RULES = load_architecture_rules("config/architecture_rules.yaml")
REPOS = {"argus-cortex", "argus-lens", "argus-lightbox", "argus-ci"}
ROLES = [{"name": "argus-cortex", "role": "library"}, {"name": "argus-lens", "role": "worker"},
         {"name": "argus-lightbox", "role": "ui"}, {"name": "argus-ci", "role": "tooling"}]


def _load(tmp_path: Path, data: dict) -> dict:
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(data))
    return load_architecture_rules(p, approved=tmp_path / "none.json", repos=REPOS)


def _rule(**kw) -> dict:
    return {"name": "r", **kw}


# ── validation at load ───────────────────────────────────────────────────────

def test_the_checked_in_rules_load() -> None:
    assert {r["name"] for r in RULES["rules"]} >= {"service_boundary", "dependency_direction"}


@pytest.mark.parametrize("data, needle", [
    # the original rules: roles no repo has
    ({"rules": [_rule(forbid=[{"from_roles": ["gateway"], "to_roles": ["database"]}])], "service_roles": ROLES},
     "no repo in service_roles has"),
    ({"rules": [_rule(forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}])],
      "service_roles": [*ROLES, {"name": "argus-nope", "role": "ui"}]}, "unknown repo 'argus-nope'"),
    ({"rules": [_rule(forbidden_edges=["repo::a->repo::b:DEPENDS_ON"])], "service_roles": ROLES}, "never builds"),
    ({"rules": [_rule(allowed=["gateway->services"])], "service_roles": ROLES}, "never builds"),
    ({"rules": [_rule(description="d")], "service_roles": ROLES}, "no condition"),
    ({"rules": [_rule(forbid=[])], "service_roles": ROLES}, "non-empty list"),
    ({"rules": [_rule(forbid=[{"from_roles": ["ui"]}])], "service_roles": ROLES}, "to_roles must be"),
    # one repo holds the role, and a repo never violates a rule against itself
    ({"rules": [_rule(forbid=[{"from_roles": ["library"], "to_roles": ["library"]}])], "service_roles": ROLES},
     "can match nothing"),
    ({"rules": [_rule(forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}], exclude_paths="tests/*")],
      "service_roles": ROLES}, "exclude_paths"),
    ({"rules": [_rule(required_imports=["x"], applies_to=["argus-ghost"])], "service_roles": ROLES},
     "unknown repo 'argus-ghost'"),
    ({"rules": [], "service_roles": ROLES,
      "domain_ownership": [{"domain": "d", "owner": "argus-cortex", "forbidden_in": ["argus-ghost"]}]},
     "unknown repo 'argus-ghost'"),
    ({"rules": [], "service_roles": ROLES,
      "call_graph_rules": {"allowed_calls": [{"from": "*", "to": "argus-ghost"}]}}, "unknown repo 'argus-ghost'"),
])
def test_a_rule_that_cannot_resolve_or_match_is_a_load_error(tmp_path: Path, data: dict, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        _load(tmp_path, data)


def test_an_approved_rule_is_validated_too(tmp_path: Path) -> None:
    approved = tmp_path / "approved.yaml"
    approved.write_text(yaml.safe_dump({"rules": [_rule(name="use_x", required_imports=["x"],
                                                         applies_to=["argus-ghost"])]}))
    base = tmp_path / "rules.yaml"
    base.write_text(yaml.safe_dump({"rules": [], "service_roles": ROLES}))
    with pytest.raises(ValueError, match="argus-ghost"):
        load_architecture_rules(base, approved=approved, repos=REPOS)


def test_known_repos_come_from_ecosystem_and_mirrors() -> None:
    repos = arch_contracts.known_repos()
    assert repos is not None and {"argus-cortex", "agent-composer", "ai-code-auditor"} <= repos


# ── each checked-in rule on a seeded violation (real files, the pipeline's own path) ──

# rule -> (files for exactly one violation, files for a clean tree); each file is (repo, path, source)
SEEDED = {
    "service_boundary": (
        [("argus-lightbox", "src/argus_lightbox/view.py", "from argus_lens.assembly import composer\n")],
        [("argus-lightbox", "tests/test_view.py", "from argus_lens.assembly import composer\n"),
         ("argus-lightbox", "scripts/record.py", "import argus_lens\n"),
         ("argus-lightbox", "src/argus_lightbox/store.py", "from argus_cortex.store import Store\n"),
         ("argus-lens", "src/argus_lens/x.py", "import argus_lens.types\n")],
    ),
    "dependency_direction": (
        [("argus-cortex", "src/argus_cortex/gpu.py", "from argus_lens import types\n")],
        [("argus-lens", "src/argus_lens/x.py", "from argus_cortex import gpu\n"),
         ("argus-cortex", "src/argus_cortex/y.py", "from . import gpu\nimport argus_cortex.wire\n")],
    ),
}


def _run(tmp_path: Path, files: list[tuple[str, str, str]]) -> list:
    repo_files: dict[str, list[Path]] = {}
    roots: dict[str, str] = {}
    for repo, rel, src in files:
        p = tmp_path / repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
        repo_files.setdefault(repo, []).append(p)
        roots[repo] = str(tmp_path / repo)
    deps = repo_dependencies(repo_files, roots, [r["name"] for r in RULES["service_roles"]])
    graph = GraphBuilder(str(tmp_path / "graph.db"))
    try:
        return ArchitectureEngine(graph, RULES, dependencies=deps).evaluate()
    finally:
        graph.close()


def test_every_checked_in_rule_has_a_seeded_fixture() -> None:
    assert {r["name"] for r in RULES["rules"]} == set(SEEDED)


@pytest.mark.parametrize("rule", sorted(SEEDED))
def test_each_rule_fires_once_on_its_seeded_violation_and_not_on_a_clean_tree(tmp_path: Path, rule: str) -> None:
    bad, clean = SEEDED[rule]
    hits = _run(tmp_path / "bad", bad)
    assert [v.rule_name for v in hits] == [rule], hits
    assert ":1 (" in hits[0].evidence  # the importing file and line, so the pack points at code
    assert _run(tmp_path / "clean", clean) == []


def test_one_violation_per_repo_pair_with_a_stable_pack_id(tmp_path: Path) -> None:
    files = [("argus-lightbox", f"src/argus_lightbox/m{i}.py", "import argus_lens\n" * (i + 1)) for i in range(3)]
    [v] = _run(tmp_path / "a", files)
    assert v.violating_path == "argus-lightbox -> argus-lens" and v.entities == ["argus-lightbox", "argus-lens"]
    [moved] = _run(tmp_path / "b", [("argus-lightbox", "src/argus_lightbox/other.py", "\n\nimport argus_lens.x\n")])
    [f1], [f2] = architecture_findings([v]), architecture_findings([moved])
    assert f1.id == f2.id and f1.id.startswith("pack-argus-lightbox-arch-")
    assert f1.repo == "argus-lightbox" and f1.file_path.startswith("src/argus_lightbox/")


# ── the arch.contracts gate's measurement ────────────────────────────────────

def test_checked_in_rules_all_fire_on_their_generated_fixture() -> None:
    rep = fixtures(RULES)
    assert rep["n"] == len(RULES["rules"]) and rep["rate"] == 1.0, rep


def test_a_rule_that_cannot_fire_lowers_the_rate() -> None:
    rules = {"service_roles": ROLES, "rules": [
        _rule(name="ok", forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}]),
        _rule(name="blind", forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}], exclude_paths=["*"]),
        _rule(name="use_x", required_imports=["x"]),
    ]}
    rep = fixtures(rules)
    assert rep["n"] == 3 and rep["fired"] == 1
    assert {r["rule"]: r["fired"] for r in rep["rules"]} == {"ok": True, "blind": False, "use_x": False}


def test_no_rules_measures_nothing() -> None:
    assert fixtures({"rules": [], "service_roles": ROLES})["rate"] is None


def test_a_rule_forbidding_both_directions_gets_an_empty_clean_graph() -> None:
    rule = _rule(forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}, {"from_roles": ["worker"], "to_roles": ["ui"]}])
    bad, clean = arch_contracts.seeded_fixture(rule, ROLES)
    assert clean == {} and list(bad) == [("argus-lightbox", "argus-lens")]
    assert fixtures({"rules": [rule], "service_roles": ROLES})["rate"] == 1.0


def test_excluded_sites_alone_raise_nothing() -> None:
    rule = _rule(forbid=[{"from_roles": ["ui"], "to_roles": ["worker"]}], exclude_paths=["tests/*"])
    deps = {("argus-lightbox", "argus-lens"): [ImportSite("tests/test_a.py", 3, "argus_lens")]}
    assert arch_contracts.evaluate_rule(rule, ROLES, deps) == []
