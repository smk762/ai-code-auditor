"""phased_migration_plan schedules each repo once."""

from __future__ import annotations

from patterns.extraction_migration_planner import phased_migration_plan


def _migrations(plan: dict) -> list[str]:
    tasks = plan["phase_2"]["tasks"] + plan["phase_3"]["tasks"]
    return [t for t in tasks if t.startswith("Migrate") and "svc-" in t]


def test_single_repo_is_migrated_once() -> None:
    plan = phased_migration_plan("shared-x", ["svc-a"])
    assert plan["phase_2"]["tasks"] == ["Migrate imports in svc-a to shared-x."]
    assert len(_migrations(plan)) == 1


def test_multi_repo_split_is_unchanged() -> None:
    plan = phased_migration_plan("shared-x", ["svc-a", "svc-b", "svc-c"])
    assert plan["phase_2"]["tasks"] == ["Migrate imports in svc-a to shared-x."]
    assert plan["phase_3"]["tasks"][:2] == ["Migrate svc-b to shared-x.", "Migrate svc-c to shared-x."]
    assert len(_migrations(plan)) == 3
