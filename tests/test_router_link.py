"""router_link: finding the router checkout and importing its `router.config`
without writing into it."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _harness
import _workspace
import router_link
from router_link import RouterNotFound

REPO = Path(__file__).absolute().parents[1]


def test_it_finds_the_router_the_suite_found():
    assert router_link.find_router() == _workspace.ROUTER_ROOT


def test_the_variable_is_the_only_place_searched(tmp_path):
    with pytest.raises(RouterNotFound) as e:
        router_link.find_router(env={"AMAP_ROUTER_REPO": str(tmp_path)})
    assert "AMAP_ROUTER_REPO" in str(e.value)


def test_an_empty_variable_counts_as_unset():
    assert router_link.find_router(env={"AMAP_ROUTER_REPO": ""}) == \
        _workspace.ROUTER_ROOT


def test_a_directory_without_the_confirming_file_is_skipped(tmp_path):
    (tmp_path / "amap-router-local").mkdir()
    assert _harness.nothing_beside(tmp_path) == [tmp_path / "amap-router-local"]
    with pytest.raises(RouterNotFound):
        router_link.find_router(env={}, start=tmp_path / "x" / "y")


def _copies(tmp_path):
    t, r = tmp_path / "T", tmp_path / "R"
    t.mkdir()
    shutil.copy(REPO / "router_link.py", t / "router_link.py")
    shutil.copytree(_workspace.ROUTER_ROOT / "router", r / "router",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return t, r


def _run(code, t, r):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH")}
    env["PYTHONPATH"] = str(t)
    env["AMAP_ROUTER_REPO"] = str(r)
    return subprocess.run([sys.executable, "-c", code], cwd=str(t), env=env,
                          capture_output=True, text=True, timeout=120)


def test_loading_the_router_writes_no_bytecode_into_it(tmp_path):
    t, r = _copies(tmp_path)
    p = _run("import router_link; m = router_link.router_config(); "
             "print(m.LANES)", t, r)
    assert p.returncode == 0, p.stderr
    assert not list(r.rglob("__pycache__"))


def test_a_different_router_already_imported_is_refused(tmp_path):
    t, r = _copies(tmp_path)
    d = tmp_path / "D" / "router"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "config.py").write_text("")
    p = _run("import sys; sys.path.insert(0, %r); import router.config; "
             "import router_link; router_link.router_config()"
             % str(tmp_path / "D"), t, r)
    assert p.returncode != 0
    assert "different `router` package" in p.stderr


def test_the_not_found_remedy_is_this_repos_sentence():
    assert router_link.not_found_remedy() == (
        "check out amap-router-local beside this repository, or set "
        "$AMAP_ROUTER_REPO")
