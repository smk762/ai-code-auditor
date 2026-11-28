"""ecosystem.arch_contracts.validate: a bad name or a single repo where a list belongs is one clear
ValueError, never a TypeError or one error per character."""
from __future__ import annotations

import pytest

from ecosystem import arch_contracts

REPOS = {"argus-cortex", "argus-lens", "argus-lightbox"}
ROLES = [{"name": "argus-cortex", "role": "library"}, {"name": "argus-lens", "role": "worker"},
         {"name": "argus-lightbox", "role": "ui"}]
FORBID = [{"from_roles": ["ui"], "to_roles": ["worker"]}]


@pytest.mark.parametrize("data, needle", [
    ({"rules": [{"name": ["r"], "forbid": FORBID}], "service_roles": ROLES}, r"rules\[0\]: needs a name"),
    ({"rules": [{"name": {"a": 1}, "forbid": FORBID}], "service_roles": ROLES}, r"rules\[0\]: needs a name"),
    ({"rules": [], "service_roles": [*ROLES, {"name": ["argus-x"], "role": "ui"}]}, r"service_roles\[3\]: needs a name"),
], ids=["list_rule_name", "mapping_rule_name", "list_role_name"])
def test_an_unhashable_name_is_a_value_error(data: dict, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        arch_contracts.validate(data, REPOS)


@pytest.mark.parametrize("data", [
    {"rules": [{"name": "r", "forbid": FORBID, "applies_to": "argus-lens"}], "service_roles": ROLES},
    {"rules": [], "service_roles": ROLES,
     "domain_ownership": [{"domain": "d", "owner": "argus-cortex", "forbidden_in": "argus-lens"}]},
], ids=["applies_to", "forbidden_in"])
def test_a_single_repo_where_a_list_belongs_is_one_clear_error(data: dict) -> None:
    with pytest.raises(ValueError, match="must be a list of repos") as exc:
        arch_contracts.validate(data, REPOS)
    assert "unknown repo" not in str(exc.value)
