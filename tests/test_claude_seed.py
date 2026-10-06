"""The first-run seeder, payload/claude-seed.

Decision D9 of IMPLEMENTATION-PLAN.md: the key names it writes were observed on
Claude Code 2.1.284, not documented. OpenShell gives the agent a placeholder for
the provider's credential (OpenShell main@acbac9c:
docs/how-it-works/providers/overview.mdx:366-369), so the suffix is computed
inside the sandbox.

A fake `claude` first on PATH answers `--version`; no test runs the real one.
"""

import ast
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

import _amap_main
from _amap_main import FAKE_API_KEY

SEED = _amap_main.SEED
SUFFIX = FAKE_API_KEY[-20:]


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "fakebin"
        self.home = tmp_path / "home"
        self.work = tmp_path / "work"
        for d in (self.bin, self.home, self.work):
            d.mkdir()
        fake = self.bin / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            'echo "${FAKE_VERSION:-2.1.284 (Claude Code)}"\n'
            'exit "${FAKE_RC:-0}"\n', encoding="utf-8")
        fake.chmod(0o755)
        self.path = str(self.bin) + os.pathsep + os.environ["PATH"]
        assert shutil.which("claude", path=self.path) == str(fake)
        self.config = self.home / ".claude.json"

    def run(self, **overrides):
        env = {"PATH": self.path, "HOME": str(self.home),
               "ANTHROPIC_API_KEY": FAKE_API_KEY,
               "PYTHONDONTWRITEBYTECODE": "1"}
        for k, v in overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        assert shutil.which("claude", path=env["PATH"]) == str(self.bin / "claude")
        return subprocess.run([sys.executable, str(SEED)], env=env, text=True,
                              capture_output=True, timeout=60,
                              cwd=str(self.work), stdin=subprocess.DEVNULL)

    def doc(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def workdir(self):
        return os.path.realpath(self.work)

    def home_names(self):
        return sorted(p.name for p in self.home.iterdir())


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_seeding_an_absent_file_writes_exactly_the_listed_answers(env):
    r = env.run()
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert env.doc() == {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "2.1.284",
        "projects": {env.workdir(): {"hasTrustDialogAccepted": True}},
        "customApiKeyResponses": {"approved": [SUFFIX]}}
    assert stat.S_IMODE(env.config.stat().st_mode) == 0o600
    assert env.home_names() == [".claude.json"]


def _leaves(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, path + (k,))
    else:
        yield path, node


def test_seeding_keeps_every_key_and_value_it_had(env):
    """AC3."""
    before = {
        "hasCompletedOnboarding": False,
        "lastOnboardingVersion": "1.0.0",
        "numStartups": 7,
        "userID": "u",
        "projects": {"/elsewhere": {"x": 1},
                     env.workdir(): {"hasTrustDialogAccepted": False,
                                     "allowedTools": []}},
        "customApiKeyResponses": {"approved": ["x" * 20],
                                  "rejected": ["y" * 20]}}
    env.config.write_text(json.dumps(before), encoding="utf-8")
    r = env.run()
    assert r.returncode == 0, r.stderr
    after = env.doc()
    new = dict(_leaves(after))
    for path, value in _leaves(before):
        assert path in new, path
        if isinstance(value, list):
            assert new[path][:len(value)] == value, path
        else:
            assert new[path] == value, path
    approved = ("customApiKeyResponses", "approved")
    assert new[approved] == ["x" * 20, SUFFIX]
    grown = [p for p, v in _leaves(before) if isinstance(v, list)
             and new[p] != v]
    assert grown == [approved]
    assert after["hasCompletedOnboarding"] is False
    assert after["lastOnboardingVersion"] == "1.0.0"
    assert after["projects"][env.workdir()]["hasTrustDialogAccepted"] is False


def test_only_a_20_character_suffix_of_the_key_is_written(env):
    """AC3."""
    r = env.run()
    data = env.config.read_bytes()
    assert FAKE_API_KEY.encode() not in data
    assert FAKE_API_KEY[-21:].encode() not in data
    assert len(SUFFIX) == 20 and SUFFIX.encode() in data
    for text in (r.stdout, r.stderr):
        assert FAKE_API_KEY not in text and SUFFIX not in text


def test_seeding_is_idempotent(env):
    assert env.run().returncode == 0
    first = env.config.read_bytes()
    st = env.config.stat()
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.config.read_bytes() == first
    st2 = env.config.stat()
    assert (st2.st_ino, st2.st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    assert env.doc()["customApiKeyResponses"]["approved"].count(SUFFIX) == 1


def test_the_mode_of_an_existing_file_is_kept(env):
    env.config.write_text('{"keep": 1}', encoding="utf-8")
    env.config.chmod(0o640)
    assert env.run().returncode == 0
    assert stat.S_IMODE(env.config.stat().st_mode) == 0o640
    assert env.doc()["keep"] == 1


def test_the_version_is_the_running_claude_codes(env):
    assert env.run(FAKE_VERSION="9.8.7 (Claude Code)").returncode == 0
    assert env.doc()["lastOnboardingVersion"] == "9.8.7"


def _write(text):
    return lambda e: e.config.write_text(text, encoding="utf-8")


def _symlink(e):
    real = e.tmp / "real.json"
    real.write_text('{"a": 1}', encoding="utf-8")
    e.config.symlink_to(real)


REFUSALS = {
    "key absent": (None, {"ANTHROPIC_API_KEY": None}),
    "key empty": (None, {"ANTHROPIC_API_KEY": ""}),
    "key of 20 characters": (None, {"ANTHROPIC_API_KEY": "k" * 20}),
    "home unset": (None, {"HOME": None}),
    "not json": (_write("not json"), {}),
    "an array": (_write("[]"), {}),
    "an empty file": (_write(""), {}),
    "projects is a list": (_write('{"projects": []}'), {}),
    "approved is a string": (
        _write('{"customApiKeyResponses": {"approved": "x"}}'), {}),
    "a symlink": (_symlink, {}),
    "version exits 1": (None, {"FAKE_RC": "1"}),
    "version is garbage": (None, {"FAKE_VERSION": "garbage"}),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_refusals_write_nothing(env, name):
    setup, overrides = REFUSALS[name]
    if setup:
        setup(env)
    existed = os.path.lexists(env.config)
    link = os.readlink(env.config) if env.config.is_symlink() else None
    before = env.config.read_bytes() if env.config.exists() else None
    r = env.run(**overrides)
    assert r.returncode == 1, r.stdout
    assert "claude-seed:" in r.stderr
    assert os.path.lexists(env.config) == existed
    if link is not None:
        assert os.readlink(env.config) == link
    if before is not None:
        assert env.config.read_bytes() == before
    assert not [n for n in env.home_names() if n != ".claude.json"]
    assert FAKE_API_KEY not in r.stderr
    assert "k" * 20 not in r.stderr


def test_the_seeder_is_executable_python_and_names_no_host():
    text = SEED.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "#!/usr/bin/env python3"
    assert os.access(SEED, os.X_OK)
    compile(text, str(SEED), "exec")
    ast.parse(text, feature_version=(3, 9))
    assert "D9" in text
    assert "OpenShell main@acbac9c:" in text
    assert "/ho" + "me/" not in text
    assert "/Us" + "ers/" not in text
