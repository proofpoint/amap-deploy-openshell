"""The four read-only sources are fingerprinted before and after the session;
a run that writes into one fails."""

import os
import subprocess
from pathlib import Path

import pytest

import _harness
import _workspace


def _git(root, *args):
    env = dict(os.environ, HOME=str(root), GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")
    return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_a_clean_checkout_fingerprints_the_same_twice(tmp_path):
    root = _repo(tmp_path)
    assert _workspace.git_fingerprint(root) == _workspace.git_fingerprint(root)


def test_overwriting_a_tracked_file_changes_the_fingerprint(tmp_path):
    root = _repo(tmp_path)
    before = _workspace.git_fingerprint(root)
    (root / "a.txt").write_text("two\n")
    after = _workspace.git_fingerprint(root)
    assert after != before
    assert "a.txt" in after


def test_an_untracked_file_changes_the_fingerprint(tmp_path):
    root = _repo(tmp_path)
    before = _workspace.git_fingerprint(root)
    (root / "sub").mkdir()
    (root / "sub" / "new.txt").write_text("x")
    after = _workspace.git_fingerprint(root)
    assert after != before
    assert "sub/new.txt" in after  # --untracked-files=all lists the file


def test_editing_an_untracked_file_changes_the_fingerprint(tmp_path):
    root = _repo(tmp_path)
    (root / "new.txt").write_text("x")
    before = _workspace.git_fingerprint(root)
    (root / "new.txt").write_text("y")
    assert _workspace.git_fingerprint(root) != before


def test_a_staged_change_changes_the_fingerprint(tmp_path):
    root = _repo(tmp_path)
    (root / "a.txt").write_text("two\n")
    _git(root, "add", "a.txt")
    staged = _workspace.git_fingerprint(root)
    _git(root, "reset", "-q", "--hard")
    assert _workspace.git_fingerprint(root) != staged


def test_fingerprinting_does_not_write_the_index(tmp_path):
    root = _repo(tmp_path)
    index = root / ".git" / "index"
    # A tracked file whose stat data is stale makes git want to refresh the
    # index (`git diff` does so even under --no-optional-locks); the
    # fingerprint must not let that write reach the source.
    (root / "a.txt").write_text("one\n")
    os.utime(root / "a.txt", (1, 1))
    mtime_before = index.stat().st_mtime_ns
    bytes_before = index.read_bytes()
    _workspace.git_fingerprint(root)
    assert index.stat().st_mtime_ns == mtime_before
    assert index.read_bytes() == bytes_before


def test_a_git_failure_is_an_error_not_a_match(tmp_path):
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    with pytest.raises(_workspace.FingerprintError):
        _workspace.git_fingerprint(broken)


def test_a_plain_tree_edit_and_addition_change_its_fingerprint(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("one")
    base = _workspace.tree_fingerprint(root)
    (root / "a.txt").write_text("two")
    edited = _workspace.tree_fingerprint(root)
    assert edited != base
    (root / "b.txt").write_text("x")
    added = _workspace.tree_fingerprint(root)
    assert added != edited
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (root / "y.pyc").write_bytes(b"\0")
    assert _workspace.tree_fingerprint(root) == added


def test_fingerprint_dispatches_on_git(tmp_path):
    repo = _repo(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f").write_text("x")
    assert _workspace.fingerprint(repo) == _workspace.git_fingerprint(repo)
    assert _workspace.fingerprint(plain) == _workspace.tree_fingerprint(plain)


def test_all_four_real_siblings_are_guarded_this_session(sibling_fingerprints):
    assert set(sibling_fingerprints) == set(_workspace.SIBLINGS)
    for value in sibling_fingerprints.values():
        assert isinstance(value, str) and value


WRITING_PROBE = (
    "import os, pathlib\n"
    "def test_writes():\n"
    "    (pathlib.Path(os.environ['AMAP_ROUTER_REPO']) / 'stray').write_text('x')\n")


def _real_paths():
    return {s.variable: str(_workspace.require_all()[k])
            for k, s in _workspace.SIBLINGS.items()}


def test_the_session_guard_fails_a_run_that_writes_into_a_sibling(tmp_path):
    router = tmp_path / "fake-router"
    (router / "router").mkdir(parents=True)
    (router / "router" / "reset.py").write_text("")
    _git(router, "init", "-q")
    _git(router, "add", "router/reset.py")
    _git(router, "commit", "-q", "-m", "init")
    env = _real_paths()
    env["AMAP_ROUTER_REPO"] = str(router)

    bad = _harness.run_copy(tmp_path / "bad", env, WRITING_PROBE)
    out = bad.stdout + bad.stderr
    assert bad.returncode == 1, out
    assert "CHANGED" in out
    assert "router" in out
    assert "stray" in out

    (router / "stray").unlink()
    ok = _harness.run_copy(tmp_path / "ok", env, _harness.PASSING_PROBE)
    assert ok.returncode == 0, ok.stdout + ok.stderr
