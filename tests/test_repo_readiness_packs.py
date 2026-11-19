"""scripts/repo_readiness.py: snapshot exports its recorded sha; typed `__all__` counts."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch):
    spec = importlib.util.spec_from_file_location("repo_readiness_script", ROOT / "scripts" / "repo_readiness.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, text: str) -> None:
    (repo / "file.txt").write_text(text)
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", text)


def test_snapshot_exports_the_sha_it_records(tmp_path: Path, monkeypatch) -> None:
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    mod = _load(monkeypatch)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "dev")
    _commit(upstream, "first")
    src = tmp_path / "src"
    _git(tmp_path, "clone", "-q", str(upstream), str(src))
    monkeypatch.setattr(mod, "resolve_repo_path", lambda repo: src)

    real_sh = mod.sh

    def racing_sh(args, cwd=None, check=True):
        out = real_sh(args, cwd=cwd, check=check)
        if args[:2] == ["git", "rev-parse"]:
            # Another snapshot fetches a newer dev between rev-parse and archive.
            _commit(upstream, "second")
            _git(src, "fetch", "-q", "origin", "dev")
        return out

    monkeypatch.setattr(mod, "sh", racing_sh)
    snap = mod.snapshot(SimpleNamespace(name="r", branch="dev"), tmp_path / "snaps")
    assert snap["sha"] == _git(upstream, "rev-parse", "dev~1")
    assert (snap["path"] / "file.txt").read_text() == "first"


def test_typed_and_augmented_all_count_as_defined(tmp_path: Path, monkeypatch) -> None:
    mod = _load(monkeypatch)
    for pkg, body in {
        "plain": "__all__ = ['a']\n",
        "typed": "__all__: list[str] = ['a']\n",
        "augmented": "from .x import __all__\n__all__ += ['b']\n",
        "none": "x = 1\n",
    }.items():
        (tmp_path / "src" / pkg).mkdir(parents=True)
        (tmp_path / "src" / pkg / "__init__.py").write_text(body)
    pre = mod.prechecks(tmp_path)
    assert pre["package_init_without_all"] == ["src/none/__init__.py"]
