"""The harness itself: sibling discovery, the importability of each source,
the missing-source failure (run in a copied tree) and the binary guard."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import _harness
import _workspace
from _workspace import SIBLINGS

FAKE_OPENSHELL = '#!/bin/sh\necho ran > "$MARK"\n'


def _make(root: Path, key: str, name: str = None) -> Path:
    """A directory that satisfies `key`'s confirming file."""
    sib = SIBLINGS[key]
    d = root / (name or sib.dir_names[0])
    (d / sib.confirm).parent.mkdir(parents=True, exist_ok=True)
    (d / sib.confirm).write_text("")
    return d


def _empty_env(**overrides):
    env = {v: "" for v in _workspace.ENV_NAMES}
    env.update(overrides)
    return env


# --- discovery -------------------------------------------------------------

def test_the_variable_is_the_only_place_searched(tmp_path):
    named = _make(tmp_path / "named", "router", "chosen")
    start = tmp_path / "ws" / "repo" / "tests"
    start.mkdir(parents=True)
    _make(tmp_path / "ws", "router")  # a confirmed sibling beside `start`
    found = _workspace.find("router", _empty_env(AMAP_ROUTER_REPO=str(named)),
                            start)
    assert found.path == named
    assert found.variable == "AMAP_ROUTER_REPO"


def test_a_variable_without_the_confirm_file_is_missing_and_the_walk_is_not_consulted(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    start = tmp_path / "ws" / "repo" / "tests"
    start.mkdir(parents=True)
    _make(tmp_path / "ws", "router")
    found = _workspace.find("router", _empty_env(AMAP_ROUTER_REPO=str(empty)),
                            start)
    assert found.path is None
    text = _workspace.message(found)
    assert "AMAP_ROUTER_REPO" in text
    assert str(empty) in text
    assert SIBLINGS["router"].confirm in text


def test_an_empty_variable_counts_as_unset(tmp_path):
    start = tmp_path / "ws" / "repo" / "tests"
    start.mkdir(parents=True)
    beside = _make(tmp_path / "ws", "router")
    found = _workspace.find("router", _empty_env(), start)
    assert found.path == beside
    assert found.variable is None


def test_the_walk_takes_the_nearest_confirmed_directory(tmp_path):
    start = tmp_path / "a" / "b" / "c"
    start.mkdir(parents=True)
    (tmp_path / "a" / "b" / "amap-router-local").mkdir()  # no confirm file
    far = _make(tmp_path, "router")
    assert _workspace.find("router", _empty_env(), start).path == far


def test_spec_is_found_under_either_name(tmp_path):
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)
    hidden = _make(tmp_path / "a", "spec", ".amap-spec")
    assert _workspace.find("spec", _empty_env(), start).path == hidden


def test_require_all_names_every_missing_sibling(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {v: str(empty) for v in _workspace.ENV_NAMES}
    with pytest.raises(_workspace.SiblingMissing) as e:
        _workspace.require_all(env)
    for v in _workspace.ENV_NAMES:
        assert v in str(e.value)


def test_this_checkout_finds_all_four():
    roots = _workspace.require_all()
    assert set(roots) == set(SIBLINGS)
    for key, path in roots.items():
        assert (path / SIBLINGS[key].confirm).is_file()


# --- importability ---------------------------------------------------------

LOAD_SNIPPET = """
import importlib.util, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

from router.config import LANES, LANE_LEAVES
assert LANES and set(LANES) <= set(LANE_LEAVES)

import fleet_policy
assert hasattr(fleet_policy, "address_for")

spec_path = Path(sys.argv[1]) / "fixtures" / "validate.py"
spec = importlib.util.spec_from_file_location("amap_spec_validate", spec_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "check_document")

loader = SourceFileLoader("inbox_delivery", str(Path(sys.argv[2]) / "bin" / "inbox-delivery"))
loader.load_module()
print("ok")
"""


def test_each_sibling_loads_unmodified():
    roots = _workspace.require_all()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(roots["router"]),
                                           str(roots["sandy"])]))
    r = subprocess.run([sys.executable, "-c", LOAD_SNIPPET, str(roots["spec"]),
                        str(roots["connector"])], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


# --- a missing source fails the run ----------------------------------------

def _real_env():
    roots = _workspace.require_all()
    return {s.variable: str(roots[k]) for k, s in SIBLINGS.items()}


def _repo_of(tmp: Path) -> Path:
    return tmp / "ws" / "repo"


def test_the_copied_harness_passes_with_every_sibling_named(tmp_path):
    r = _harness.run_copy(tmp_path, _real_env())
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in r.stdout
    assert _harness.nothing_beside(_repo_of(tmp_path)) == []


@pytest.mark.parametrize("key", list(SIBLINGS))
def test_a_missing_sibling_fails_the_run(key, tmp_path):
    sib = SIBLINGS[key]
    empty = tmp_path / "empty"
    empty.mkdir()
    env = _real_env()
    env[sib.variable] = str(empty)
    r = _harness.run_copy(tmp_path, env)
    out = r.stdout + r.stderr
    assert _harness.nothing_beside(_repo_of(tmp_path)) == []
    assert r.returncode == 4 == pytest.ExitCode.USAGE_ERROR, out
    assert "ERROR:" in out
    assert sib.variable in out
    assert str(empty) in out
    assert sib.confirm in out
    assert "passed" not in out


def test_an_unset_variable_with_nothing_beside_the_repo_fails_the_run(tmp_path):
    env = _real_env()
    env["AMAP_ROUTER_REPO"] = None
    r = _harness.run_copy(tmp_path, env)
    out = r.stdout + r.stderr
    assert r.returncode == 4, out
    assert "amap-router-local" in out
    assert "AMAP_ROUTER_REPO" in out


# --- the binary guard ------------------------------------------------------

def _env_with(path_dir, mark):
    return dict(os.environ, PATH=f"{path_dir}{os.pathsep}{os.environ['PATH']}",
                MARK=str(mark))


def test_the_guard_is_installed_for_the_session():
    assert subprocess.Popen is _harness.GuardedPopen


@pytest.mark.parametrize("how", ["run", "check_output", "popen", "shell"])
def test_calling_openshell_unstubbed_raises(how, tmp_path):
    notstub = tmp_path / "notstub"
    fake = _harness.write_fake(notstub, "openshell", FAKE_OPENSHELL)
    mark = tmp_path / "mark"
    env = _env_with(notstub, mark)
    with pytest.raises(_harness.UnstubbedBinary):
        if how == "run":
            subprocess.run(["openshell", "--version"], env=env)
        elif how == "check_output":
            subprocess.check_output([str(fake)], env=env)
        elif how == "popen":
            subprocess.Popen(["openshell"], env=env)
        else:
            subprocess.run("openshell --version", shell=True, env=env)
    assert not mark.exists()


@pytest.mark.parametrize("name", ["docker", "podman"])
def test_docker_and_podman_are_guarded_too(name):
    with pytest.raises(_harness.UnstubbedBinary):
        subprocess.run([name, "ps"])


def test_a_stubbed_fake_runs(fake_bin, tmp_path):
    _harness.write_fake(fake_bin, "openshell", FAKE_OPENSHELL)
    mark = tmp_path / "mark"
    r = subprocess.run(["openshell"], env=_env_with(fake_bin, mark))
    assert r.returncode == 0
    assert mark.exists()


def test_unrelated_programs_are_not_guarded():
    assert subprocess.run([sys.executable, "-c", "pass"]).returncode == 0
