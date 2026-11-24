"""README.md matches the code and holds nothing private (#162)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts import update_readme_cli as gen

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEPLOYMENT = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")


def test_readme_commands_match_the_cli() -> None:
    assert gen.render(README) == README, "run python scripts/update_readme_cli.py"


def test_a_missing_or_stale_command_is_noticed() -> None:
    row = next(line for line in README.splitlines() if line.startswith("| `ai-audit trust`"))
    for changed in (README.replace(row + "\n", ""), README.replace(row, row + "\n| `ai-audit gone` | removed |")):
        assert changed != README
        assert gen.render(changed) != changed


def test_the_table_lists_nested_commands() -> None:
    assert "| `ai-audit eval trust` |" in gen.table()


def test_missing_markers_are_an_error() -> None:
    with pytest.raises(ValueError):
        gen.render("no markers")


@pytest.mark.parametrize("text", [README, DEPLOYMENT], ids=["README", "deployment"])
def test_no_private_addresses_or_home_paths(text: str) -> None:
    private = r"\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d+\.\d+\b"
    assert not re.findall(private, text)
    assert not re.findall(r"/home/\w+", text)
    assert not re.findall(r"[\w.+-]+@[\w-]+\.(?!invalid\b)\w+", text)  # an email address


def test_readme_names_no_scanned_repo_or_internal_host() -> None:
    mirrors = yaml.safe_load((ROOT / "config" / "repo_mirrors.yaml").read_text(encoding="utf-8"))["mirrors"]
    names = {m["name"] for m in mirrors} - {"ai-code-auditor"}
    names |= {"mithrandir", "jarrah", "gothmog", "mimiri"}
    found = [n for n in sorted(names) if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", README, re.IGNORECASE)]
    assert not found


def test_local_links_and_images_exist() -> None:
    for text, base in ((README, ROOT), (DEPLOYMENT, ROOT / "docs")):
        for target in re.findall(r"\]\(([^)#\s]+)", text):
            if "://" not in target:
                assert (base / target).exists(), target
