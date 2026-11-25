from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from auditor import topology_public as tp
from auditor.config import RepoConfig

DOC = """| Area | Repo | Role |
|---|---|---|
| Captioning | [argus-lens](https://x) | Captions |
| Broker | [argus-iris](https://x) | Secret broker |

## Ecosystem flow

```
quarry ──► lens ──► iris
```
"""


def _repo(root: Path, name: str, files: dict[str, str], *, pushed: bool = True) -> RepoConfig:
    d = root / name
    d.mkdir()

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                       check=True, capture_output=True)

    git("init", "-q", "-b", "dev")
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A")
    git("commit", "-qm", "x")
    if pushed:
        git("update-ref", "refs/remotes/origin/dev", "HEAD")
    return RepoConfig(name, str(d))


OLLAMA = "url = 'http://ollama'\n"


@pytest.fixture
def repos(tmp_path: Path, monkeypatch) -> list[RepoConfig]:
    rs = [
        _repo(tmp_path, "argus-lens", {
            "src/a.py": OLLAMA + "import argus_iris\n", "src/b.py": OLLAMA + "c = 'comfyui mithrandir'\n",
            "src/c.py": "c = 'comfyui mithrandir'\n",
            "tests/test_a.py": "r = 'redis'\n", "web/x.test.ts": "const r = 'redis'\n", "src/fixtures/f.py": "r = 'redis'\n"}),
        _repo(tmp_path, "argus-quarry", {"src/q.py": "from argus_lens import x\n"}),
        _repo(tmp_path, "argus-iris", {"src/i.py": "import argus_lens\n"}),
        _repo(tmp_path, "voluptas", {"v.py": "import argus_lens\n"}),
        _repo(tmp_path, "ai-code-auditor", {"auditor/ui_topology.py": OLLAMA, "wiki/pages.py": OLLAMA, "auditor/c.py": OLLAMA}),
    ]
    class _Cfg:
        repos = rs
    monkeypatch.setattr("auditor.config.load_ecosystem_config", lambda: _Cfg())
    doc = tmp_path / "eco.md"
    doc.write_text(DOC)
    monkeypatch.setattr(tp.ut, "ECOSYSTEM_DOC", doc)
    (tmp_path / "deny.txt").write_text("Jarrah\n")
    return rs


PRIVATE = {"argus-iris", "voluptas"}


def _info(name: str, description: str = "d"):
    return None if name == "voluptas" else {"visibility": "private" if name in PRIVATE else "public",
                                            "description": description}


def _export(tmp_path: Path, info=_info):
    out = tmp_path / "map.json"
    return tp.export(out, denylist_path=tmp_path / "deny.txt", info=info), out


def test_private_repos_and_ops_hosts_never_appear(tmp_path: Path, repos) -> None:
    data, out = _export(tmp_path)
    text = out.read_text().lower()
    for word in ("iris", "voluptas", "mithrandir", "violation", str(tmp_path).lower()):
        assert word not in text
    assert {"argus-lens", "argus-quarry", "svc:comfyui", "svc:ollama"} <= {n["id"] for n in data["nodes"]}
    ids = {n["id"] for n in data["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])
    assert [(e["source"], e["target"]) for e in data["edges"] if e["kind"] == "flow"] == [("argus-quarry", "argus-lens")]


def test_evidence_skips_tests_fixtures_and_our_own_catalogue(tmp_path: Path, repos) -> None:
    data, _ = _export(tmp_path)
    assert "svc:redis" not in {n["id"] for n in data["nodes"]}
    assert not [e for e in data["edges"] if e["source"] == "ai-code-auditor"]
    lens = next(e for e in data["edges"] if e["source"] == "argus-lens" and e["target"] == "svc:ollama")
    assert [h["path"] for h in lens["evidence"]] == ["src/a.py", "src/b.py"]


def test_links_pin_the_scanned_commit(tmp_path: Path, repos) -> None:
    data, _ = _export(tmp_path)
    head = subprocess.run(["git", "-C", repos[0].path, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert next(n for n in data["nodes"] if n["id"] == "argus-lens")["head"] == head


@pytest.mark.parametrize("how", ["dirty", "unpushed"])
def test_a_mirror_off_origin_dev_stops_the_export(tmp_path: Path, repos, how: str) -> None:
    p = repos[0].path
    (Path(p) / "src/a.py").write_text("changed\n")
    if how == "unpushed":
        subprocess.run(["git", "-C", p, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "y"], check=True)
    with pytest.raises(RuntimeError, match="argus-lens"):
        _export(tmp_path)
    assert not (tmp_path / "map.json").exists()


@pytest.mark.parametrize("text", ["runs on Jarrah", "talks to voluptas", "at 192.168.200.9"])
def test_gate_refusal_writes_nothing(tmp_path: Path, repos, text: str) -> None:
    with pytest.raises(RuntimeError, match="opsec gate"):
        _export(tmp_path, lambda n: _info(n, text))
    assert not (tmp_path / "map.json").exists()


def test_never_runs_without_a_denylist_or_over_another_file(tmp_path: Path, repos) -> None:
    with pytest.raises(FileNotFoundError):
        tp.export(tmp_path / "map.json", denylist_path=tmp_path / "none.txt", info=_info)
    (tmp_path / "map.json").write_text(json.dumps({"mine": 1}))
    with pytest.raises(RuntimeError, match="refusing to replace"):
        _export(tmp_path)
