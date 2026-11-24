"""Module cycles and private imports inside a repo (#154): dodges still report, real fixes don't."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from auditor import structure

BASE = {
    "pkg/__init__.py": "",
    "pkg/kinds.py": "from pkg.socket import BACKLOG\n\n\ndef stream():\n    return BACKLOG\n",
    "pkg/socket.py": "from pkg.kinds import stream\n\nBACKLOG = 5\n\n\ndef listen():\n    return stream()\n",
}
PAIR = ["pkg/kinds.py", "pkg/socket.py"]


def _cycles(files: dict[str, str]) -> list[list[str]]:
    return [c["members"] for c in structure.scan_sources({**BASE, **files})["cycles"]]


DODGES = {
    "lazy": {"pkg/kinds.py": "def stream():\n    from pkg.socket import BACKLOG\n    return BACKLOG\n"},
    "relative": {"pkg/kinds.py": "from .socket import BACKLOG\n"},
    "module": {"pkg/kinds.py": "import pkg.socket as s\n"},
    "from_package": {"pkg/kinds.py": "from . import socket\n"},
    "re_exported": {"pkg/__init__.py": "from pkg.socket import BACKLOG\n", "pkg/kinds.py": "from pkg import BACKLOG\n"},
    "re_exported_renamed": {"pkg/__init__.py": "from .socket import BACKLOG as LIMIT\n",
                            "pkg/kinds.py": "def f():\n    from . import LIMIT\n    return LIMIT\n"},
    "import_module": {"pkg/kinds.py": "import importlib\n\n\ndef f():\n    return importlib.import_module('pkg.socket')\n"},
    "try_except": {"pkg/kinds.py": "try:\n    from pkg.socket import BACKLOG\nexcept ImportError:\n    BACKLOG = 0\n"},
    "type_checking_else": {"pkg/kinds.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n"
                                           "    from pkg.socket import BACKLOG\n"},
}


@pytest.mark.parametrize("files", [{}, *DODGES.values()], ids=["base", *DODGES])
def test_a_cycle_stays_one_however_it_is_dressed(files: dict[str, str]) -> None:
    assert _cycles(files) == [PAIR]


def test_a_relay_module_joins_the_cycle() -> None:
    files = {"pkg/limits.py": "from pkg.socket import BACKLOG\n", "pkg/kinds.py": "from pkg.limits import BACKLOG\n"}
    assert _cycles(files) == [["pkg/kinds.py", "pkg/limits.py", "pkg/socket.py"]]


FIXES = {
    "moved": {"pkg/kinds.py": "BACKLOG = 5\n\n\ndef stream():\n    return BACKLOG\n"},
    "third_module": {"pkg/limits.py": "BACKLOG = 5\n", "pkg/kinds.py": "from pkg.limits import BACKLOG\n",
                     "pkg/socket.py": "from pkg.kinds import stream\nfrom pkg.limits import BACKLOG\n"},
    "types_only": {"pkg/kinds.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.socket import X\n"},
    "stdlib_namesake": {"pkg/kinds.py": "import socket\n"},
    "re_exports_itself": {"pkg/__init__.py": "from pkg.kinds import stream\nfrom pkg.socket import listen\n",
                          "pkg/kinds.py": "stream = None\n"},
    "package_constant": {"pkg/__init__.py": "VERSION = 1\nfrom pkg.kinds import stream\n",
                         "pkg/kinds.py": "from pkg import VERSION\nstream = VERSION\n"},
}


@pytest.mark.parametrize("files", FIXES.values(), ids=FIXES)
def test_a_real_fix_is_silent(files: dict[str, str]) -> None:
    assert _cycles(files) == []


def test_tests_do_not_make_a_cycle() -> None:
    assert _cycles({"pkg/kinds.py": "stream = 1\n", "tests/test_x.py": "import pkg.socket\n"}) == []


def test_the_key_names_members_never_lines_and_the_cut_is_the_thin_edge() -> None:
    files = {"pkg/socket.py": "from pkg.kinds import a, b, stream\nBACKLOG = 1\n"}
    one = structure.scan_sources({**BASE, **files})["cycles"][0]
    moved = {**BASE, **files, "pkg/kinds.py": "\n\n\n" + BASE["pkg/kinds.py"]}
    assert one["key"] == structure.scan_sources(moved)["cycles"][0]["key"] == "module-cycle:pkg/kinds.py,pkg/socket.py"
    assert one["cuts"] == [{"from": "pkg/kinds.py", "to": "pkg/socket.py", "names": ["BACKLOG"]}]
    assert one["cuts_smallest"] is True
    assert one["edges"][0]["sites"] == ["pkg/kinds.py:1"] and one["lazy_edges"] == 0


def test_lazy_edges_are_marked_only_when_every_site_is_lazy() -> None:
    lazy = structure.scan_sources({**BASE, **DODGES["lazy"]})["cycles"][0]
    assert lazy["lazy_edges"] == 1
    both = "from pkg.socket import BACKLOG\n" + DODGES["lazy"]["pkg/kinds.py"]
    assert structure.scan_sources({**BASE, "pkg/kinds.py": both})["cycles"][0]["lazy_edges"] == 0


def test_src_layouts_and_same_named_packages_resolve_to_the_nearest() -> None:
    svc = {f"{s}/src/app/{n}": t for s in ("a", "b") for n, t in {
        "__init__.py": "", "x.py": "from app.y import f\n", "y.py": "from app.x import g\n" if s == "a" else "f = 1\n",
    }.items()}
    assert [c["members"] for c in structure.scan_sources(svc)["cycles"]] == [["a/src/app/x.py", "a/src/app/y.py"]]


# ── private imports ──────────────────────────────────────────────────────────

def _private(user: str, **files: str) -> list[tuple[str, str]]:
    src = {"pkg/__init__.py": "", "pkg/core.py": "_secret = 1\nopen_ = 2\n", "pkg/_impl.py": "f = 1\n",
           "pkg/user.py": user, **{f"pkg/{k}.py": v for k, v in files.items()}}
    return [(p["defined_in"], p["name"]) for p in structure.scan_sources(src)["private_imports"]
            if p["path"] == "pkg/user.py"]


@pytest.mark.parametrize("user", [
    "from pkg.core import _secret\n",
    "from pkg.core import _secret as secret\n",
    "import pkg.core\n\n\ndef f():\n    return pkg.core._secret\n",
    "from pkg import core\n\nx = core._secret\n",
    "from pkg import core\n\nx = getattr(core, '_secret')\n",
    "def f():\n    from . import core as c\n    return lambda: c._secret\n",
])
def test_a_private_name_is_reported_however_it_is_reached(user: str) -> None:
    assert _private(user) == [("pkg/core.py", "_secret")]


def test_a_re_exported_private_name_belongs_to_its_definer() -> None:
    assert _private("from pkg import _secret\n", __init__="from pkg.core import _secret\n") == [("pkg/core.py", "_secret")]


@pytest.mark.parametrize("user", [
    "from pkg.core import open_\n", "from pkg import _impl\n", "from pkg._impl import f\n",
    "import pkg._impl\n\nx = pkg._impl.f\n", "from pkg.core import __doc__\n", "import os\n\nos._exit\n",
    "class A:\n    def f(self):\n        return self._secret\n",
    "def f():\n    from pkg import core\n    return core.open_\n\n\ndef g(core):\n    return core._secret\n",
])
def test_public_names_private_modules_and_other_objects_are_not_reported(user: str) -> None:
    assert _private(user) == []


# ── mirrors ──────────────────────────────────────────────────────────────────

def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _mirror(tmp: Path, files: dict[str, str]) -> tuple[Path, Path]:
    repo = tmp / "demo"
    for p, t in files.items():
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(t)
    _git(tmp, "init", "-q", "-b", "dev", str(repo))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "x")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    cfg = tmp / "mirrors.yaml"
    cfg.write_text(f"mirrors:\n  - name: demo\n    path: {repo}\n  - name: gone\n    path: {tmp}/nope\n")
    return repo, cfg


def test_run_reads_origin_dev_and_never_writes_the_mirror(tmp_path: Path) -> None:
    repo, cfg = _mirror(tmp_path, {**BASE, "pkg/bytes.py": "x = 'é'\n"})
    (repo / "pkg/kinds.py").write_text("stream = 1\n")  # the working tree is not what is scanned
    before = (_git(repo, "status", "--porcelain"), _git(repo, "for-each-ref"))
    r = structure.run(tmp_path / "out", mirrors_path=cfg)
    assert [c["members"] for c in r["repos"]["demo"]["cycles"]] == [PAIR]
    assert (_git(repo, "status", "--porcelain"), _git(repo, "for-each-ref")) == before
    assert structure.load_report(tmp_path / "out") == r
    assert r["summary"]["repos"] == 2 and r["summary"]["unreadable"] == 1


def test_an_unreadable_repo_or_file_is_never_clean(tmp_path: Path) -> None:
    repo, cfg = _mirror(tmp_path, {"pkg/a.py": "def (:\n", "pkg/b.py": "x = 1\n", "pkg/big.py": "x = 1\n" * 200_000})
    r = structure.run(tmp_path / "out", mirrors_path=cfg)["repos"]
    assert r["demo"]["unparsed"] == ["pkg/a.py", "pkg/big.py"] and r["demo"]["complete"] is False
    assert "error" in r["gone"] and "cycles" not in r["gone"] and r["gone"]["complete"] is False
    _git(repo, "update-ref", "-d", "refs/remotes/origin/dev")
    r = structure.run(tmp_path / "out", mirrors_path=cfg)["repos"]
    assert "origin/dev" in r["demo"]["error"]


def test_one_repo_keeps_the_others_results_and_a_typo_is_refused(tmp_path: Path) -> None:
    _, cfg = _mirror(tmp_path, BASE)
    structure.run(tmp_path / "out", mirrors_path=cfg)
    assert set(structure.run(tmp_path / "out", mirrors_path=cfg, repos=["demo"])["repos"]) == {"demo", "gone"}
    with pytest.raises(ValueError, match="typo"):
        structure.run(tmp_path / "out", mirrors_path=cfg, repos=["typo"])


def _edges(spec: dict[str, int]) -> dict[tuple[str, str], dict]:
    return {(e[0], e[1]): {"names": {str(i) for i in range(n)}, "lazy": False} for e, n in spec.items()}


def _clears(edges: dict, cuts: list) -> bool:
    graph: dict[str, set[str]] = {}
    for a, b in set(edges) - set(cuts):
        graph.setdefault(a, set()).add(b)
    return structure._components(graph) == []


def test_the_cuts_clear_every_circle_not_just_the_first() -> None:
    """#154's pilot: the one best edge of a 12-module cycle left a circle of 5 behind."""
    two_loops = _edges({"ab": 1, "ba": 2, "bc": 1, "cb": 2})
    assert structure._cuts(two_loops) == ([("a", "b"), ("b", "c")], True)
    shared = _edges({"ab": 5, "bc": 1, "ca": 1, "bd": 1, "da": 1})  # one edge in both: fewest edges before fewest names
    assert structure._cuts(shared) == ([("a", "b")], True)


def test_a_search_cut_short_still_clears_the_cycle_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structure, "_MAX_CUT_STEPS", 1)
    edges = _edges({a + b: 1 for a in "abcde" for b in "abcde" if a != b})
    cuts, smallest = structure._cuts(edges)
    assert smallest is False and _clears(edges, cuts)


LOOP = {"app/__init__.py": "", "app/one/__init__.py": "from app.one.detect import go\n",
        "app/one/detect.py": "from app.two.metrics import m\n\n\ndef go():\n    return m\n",
        "app/one/leaf.py": "x = 1\n", "app/two/__init__.py": "",
        "app/two/metrics.py": "def m():\n    from app.one.leaf import x\n    return x\n"}


def test_a_cycle_through_a_package_init_is_found() -> None:
    [c] = structure.scan_sources(LOOP)["cycles"]
    assert c["members"] == ["app/one/__init__.py", "app/one/detect.py", "app/two/metrics.py"]
    [e] = [e for e in c["edges"] if e["implicit"]]
    assert (e["from"], e["to"], e["names"], e["lazy"]) == (
        "app/two/metrics.py", "app/one/__init__.py", ["via app/one/leaf.py"], True)


def test_the_loop_an_init_hides_breaks_at_run_time_once_the_import_is_top_level(tmp_path: Path) -> None:
    eager = {**LOOP, "app/two/metrics.py": "from app.one.leaf import x\n\n\ndef m():\n    return x\n"}
    for path, text in eager.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    p = subprocess.run([sys.executable, "-c", "import app.two.metrics"], cwd=tmp_path, capture_output=True, text=True,
                       check=False)
    assert "circular import" in p.stderr  # what the scan now reports is real


@pytest.mark.parametrize("change", [
    {"app/two/metrics.py": "def m():\n    if TYPE_CHECKING:\n        from app.one.leaf import x\n"},
    {"app/one/__init__.py": ""},  # the package no longer pulls its parts in
    {"app/one/detect.py": "def go():\n    from app.one import leaf\n    return leaf\n"},  # its own package only
])
def test_an_init_alone_is_no_cycle(change: dict[str, str]) -> None:
    assert structure.scan_sources({**LOOP, **change})["cycles"] == []


def test_a_package_that_re_exports_is_run_too() -> None:
    src = {"app/__init__.py": "", "app/one/__init__.py": "from app.core import x\nfrom app.one.detect import go\n",
           "app/one/detect.py": "from app.user import u\n\n\ndef go():\n    return u\n", "app/core.py": "x = 1\n",
           "app/user.py": "def u():\n    from app.one import x\n    return x\n"}
    [c] = structure.scan_sources(src)["cycles"]
    assert "app/one/__init__.py" in c["members"] and "app/core.py" not in c["members"]
