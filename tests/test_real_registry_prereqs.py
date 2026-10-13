"""The private suite's prerequisites (tests/test_real_registry.py) fail
closed: whatever goes wrong before the first registry command, the run ends
with exit status 2 and a named reason, and nothing is asked of a registry.

These run in public CI: they need no registry. Standard library only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_real_registry as trr

REPO = Path(__file__).absolute().parents[1]


def _overlay(tmp_path, text):
    p = tmp_path / "overlay.json"
    p.write_text(text)
    return p


def test_an_unset_overlay_is_a_prerequisite_failure():
    with pytest.raises(trr.PrerequisiteError, match="not set"):
        trr.prerequisites(None)


def test_an_unreadable_overlay_is_a_prerequisite_failure(tmp_path):
    with pytest.raises(trr.PrerequisiteError, match="FileNotFoundError"):
        trr.prerequisites(tmp_path / "missing.json")
    with pytest.raises(trr.PrerequisiteError):
        trr.prerequisites(tmp_path)               # a directory


@pytest.mark.parametrize("text", ["{", "[]", '{"sha": 1, "sha": 2}', "NaN"])
def test_a_malformed_overlay_is_a_prerequisite_failure(tmp_path, text):
    with pytest.raises(trr.PrerequisiteError):
        trr.prerequisites(_overlay(tmp_path, text))


def _full(**change):
    import _fake_registry
    doc = {"python": _fake_registry.real_python(),
           "checkout": "/nonexistent/code",
           "registry": "/unused", "identity_root": "/unused",
           "interface": "example-registry.adapter/1",
           "module": "example_registry", "name_schema": "example.name/1",
           "sha": "0" * 40, "scratch": "/nonexistent/scratch",
           "host_config": {"root": trr.IDENTITY_ROOT_TOKEN}}
    doc.update(change)
    return json.dumps(doc)


def test_a_bad_config_is_a_prerequisite_failure(tmp_path):
    with pytest.raises(trr.PrerequisiteError, match="module"):
        trr.prerequisites(_overlay(tmp_path, _full(module="has space")))


def test_an_unusable_checkout_is_a_prerequisite_failure(tmp_path):
    with pytest.raises(trr.PrerequisiteError, match="checkout"):
        trr.prerequisites(_overlay(tmp_path, _full()))


def test_a_launch_error_is_a_prerequisite_failure(tmp_path, monkeypatch):
    code = tmp_path / "code"
    code.mkdir()
    os.chmod(code, 0o755)
    monkeypatch.setattr(trr.hr, "owner_problem", lambda *a, **k: None)

    def boom(*a, **k):
        raise OSError("cannot launch git")
    monkeypatch.setattr(trr.subprocess, "run", boom)
    with pytest.raises(trr.PrerequisiteError, match="OSError"):
        trr.prerequisites(_overlay(tmp_path, _full(checkout=str(code))))


def test_the_session_exits_2_before_any_registry_command(tmp_path):
    """End to end: a malformed overlay ends the private run with status 2,
    and no test runs."""
    env = dict(os.environ)
    env[trr.OVERLAY_VARIABLE] = str(_overlay(tmp_path, "{"))
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
                        "no:cacheprovider", "tests/test_real_registry.py"],
                       cwd=str(REPO), env=env, capture_output=True, text=True,
                       timeout=300)
    assert p.returncode == 2, p.stdout + p.stderr
    assert "prerequisites fail" in p.stdout + p.stderr
    assert " passed" not in p.stdout
