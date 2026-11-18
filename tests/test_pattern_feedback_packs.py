from __future__ import annotations

from pathlib import Path

import yaml

from patterns.pattern_feedback import append_architecture_rule_for_extraction

CANDIDATE = {
    "id": "ext-retr-001", "repos_involved": ["argus-cortex", "argus-studio"],
    "source_units": [{"repo": "argus-cortex", "path": "a.py", "symbols": ["retry"]}],
    "proposed_library": {"name": "retry-core", "owner_repo": "argus-cortex"},
}


def test_rule_append_to_empty_rules_key(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("# starting point\nrules:\n")  # parses to {"rules": None}
    append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    text = rules.read_text()
    assert text.startswith("# starting point\n")
    assert [r["name"] for r in yaml.safe_load(text)["rules"]] == ["use_retry_core"]
    # idempotent, and a non-dict entry in the list does not crash the name check
    rules.write_text(text + "  - just-a-string\n")
    append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    assert next(r for r in yaml.safe_load(rules.read_text())["rules"] if isinstance(r, dict))["name"] == "use_retry_core"


def test_rule_append_crash_mid_write_leaves_the_rules_file_intact(tmp_path: Path, monkeypatch) -> None:
    rules = tmp_path / "rules.yaml"
    original = "# keep me\nrules:\n  - name: existing\n"
    rules.write_text(original)
    real_write = Path.write_text

    def crash_halfway(self, data, *a, **k):
        real_write(self, data[: len(data) // 2], *a, **k)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", crash_halfway)
    try:
        append_architecture_rule_for_extraction(CANDIDATE, str(rules))
    except OSError:
        pass
    monkeypatch.setattr(Path, "write_text", real_write)
    assert rules.read_text() == original
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(".lock")) == ["rules.yaml"]


def test_concurrent_rule_appends_keep_every_rule(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n")
    cands = [{**CANDIDATE, "proposed_library": {"name": f"lib-{i}"}} for i in range(24)]
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda c: append_architecture_rule_for_extraction(c, str(rules)), cands))
    names = sorted(r["name"] for r in yaml.safe_load(rules.read_text())["rules"])
    assert names == sorted(f"use_lib_{i}" for i in range(24))
