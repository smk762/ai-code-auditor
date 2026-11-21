"""ui_repos._github_slugs: ssh clone URLs get a slug, malformed YAML degrades to no links."""
from __future__ import annotations

from pathlib import Path

from auditor import ui_repos


def _write(tmp_path: Path, text: str) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repo_mirrors.yaml").write_text(text, encoding="utf-8")


def test_https_and_ssh_clone_urls_both_give_a_slug(tmp_path, monkeypatch):
    _write(tmp_path, """mirrors:
  - {name: a, url: "https://github.com/smk762/argus-a.git"}
  - {name: b, url: "git@github.com:smk762/argus-b.git"}
  - {name: c, url: "ssh://git@github.com/smk762/argus-c.git/"}
  - {name: d, url: "https://gitlab.com/x/y.git"}
""")
    monkeypatch.chdir(tmp_path)
    assert ui_repos._github_slugs() == {"a": "smk762/argus-a", "b": "smk762/argus-b", "c": "smk762/argus-c"}


def test_malformed_yaml_returns_no_slugs(tmp_path, monkeypatch):
    _write(tmp_path, "mirrors: [\n  - name: a\n  url: :::\n")
    monkeypatch.chdir(tmp_path)
    assert ui_repos._github_slugs() == {}
