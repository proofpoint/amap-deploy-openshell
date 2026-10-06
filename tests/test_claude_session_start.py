"""The SessionStart hook, payload/claude-session-start.

Context: Claude Code exports CLAUDE_CODE_MESSAGING_SOCKET to its hooks (Claude
Code's cross-session-messaging page). The hook records the socket, the pid and
the start time, and never reads CLAUDE_CODE_MESSAGING_TOKEN, the proof that a
connection comes from the session's own child (amap-connector-claude CLAUDE.md
at 37875a5). HOME under a numeric `run_as_user` is OpenShell
main@acbac9c:crates/openshell-sandbox/src/process.rs:217-241.

Every test runs a copy of the hook beside a copy of the lister whose PROC is a
staged tree, never the real /proc.
"""

import ast
import json
import os
import stat
import subprocess
import sys

import pytest

import _sessions as s
from _sessions import BASE, FakeSandbox, copy_payload, rewritten_lister, run_lister, parse_rows

HOOK = s.REPO / "payload" / "claude-session-start"
SOCK = "/tmp/hook-test/1.sock"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
MESSAGING = "e" * 32  # a fake messaging token, distinct from TOKEN
CLAUDE = BASE + 20
OUTER_CLAUDE = BASE + 21
SHELL = BASE + 22


@pytest.fixture
def staged(tmp_path):
    """A staged /proc: init, an outer and an inner claude, a shell, and this
    test process (as the hook's parent)."""
    with s.short_tmp() as sock_dir:
        fake = FakeSandbox(tmp_path, sock_dir, os.getpid())
        fake.add(OUTER_CLAUDE, 1, "claude", ["claude"])
        fake.add(CLAUDE, OUTER_CLAUDE, "claude", ["claude"], start_time=777)
        fake.add(SHELL, CLAUDE, "sh", ["/bin/sh", "-c", "x"])
        fake.add(os.getpid(), SHELL, "python3", ["python3", "x"])
        payload = tmp_path / "payload"
        payload.mkdir()
        copy_payload(payload, "claude-session-start")
        rewritten_lister(payload / "openshell-sessions", fake.proc)
        (tmp_path / "home").mkdir()
        try:
            yield fake, payload / "claude-session-start"
        finally:
            fake.close()


def run_hook(hook, home, **overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HOME", SOCKET_ENV, TOKEN_ENV)}
    env.update({"HOME": str(home), SOCKET_ENV: SOCK, TOKEN_ENV: MESSAGING,
                "PYTHONDONTWRITEBYTECODE": "1"})
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run([sys.executable, str(hook)], env=env, text=True,
                          capture_output=True, timeout=30,
                          stdin=subprocess.DEVNULL)


def test_the_hook_records_its_nearest_claude_socket_pid_and_start_time(staged):
    fake, hook = staged
    r = run_hook(hook, fake.home)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    path = fake.records / f"{CLAUDE}.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "socket": SOCK, "pid": CLAUDE, "start_time": "777"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(fake.records.stat().st_mode) == 0o700
    assert not (fake.records / f"{OUTER_CLAUDE}.json").exists()
    assert MESSAGING.encode() not in path.read_bytes()


def test_a_rerun_replaces_the_record(staged):
    fake, hook = staged
    assert run_hook(hook, fake.home).returncode == 0
    r = run_hook(hook, fake.home, **{SOCKET_ENV: "/tmp/hook-test/2.sock"})
    assert r.returncode == 0, r.stderr
    assert [p.name for p in fake.records.iterdir()] == [f"{CLAUDE}.json"]
    path = fake.records / f"{CLAUDE}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["socket"] == "/tmp/hook-test/2.sock"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "empty"])
@pytest.mark.parametrize("var", [SOCKET_ENV, "HOME"])
def test_a_missing_or_empty_variable_is_named_and_nothing_is_written(
        staged, var, value):
    fake, hook = staged
    r = run_hook(hook, fake.home, **{var: value})
    assert r.returncode == 1
    assert var in r.stderr
    assert r.stdout == ""
    assert not fake.records.exists()
    assert MESSAGING not in r.stderr


def test_the_hook_does_not_need_the_messaging_token(staged):
    fake, hook = staged
    r = run_hook(hook, fake.home, **{TOKEN_ENV: None})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert (fake.records / f"{CLAUDE}.json").exists()


def test_the_messaging_token_is_written_nowhere(tmp_path):
    """AC1: the real hook runs with a fake messaging token, and the value then
    appears in no file under the fake home and in no output."""
    with s.short_tmp() as sock_dir:
        fake = FakeSandbox(tmp_path, sock_dir, os.getpid())
        try:
            sock, key = fake.add_claude(BASE + 20)
            fake.add(os.getpid(), BASE + 20, "python3", ["python3", "x"])
            payload = tmp_path / "payload"
            payload.mkdir()
            copy_payload(payload, "claude-session-start")
            rewritten_lister(payload / "openshell-sessions", fake.proc)
            hook = run_hook(payload / "claude-session-start", fake.home,
                            **{SOCKET_ENV: sock})
            assert hook.returncode == 0, hook.stderr
            lister = run_lister(fake, tmp_path)
            assert lister.returncode == 0, lister.stderr
            needle = MESSAGING.encode()
            files = [p for p in fake.home.rglob("*") if p.is_file()]
            assert files
            assert any(p.parent == fake.records for p in files)
            assert any(p.parent == fake.keys for p in files)
            for p in files:
                assert needle not in p.read_bytes(), p
            for out in (hook.stdout, hook.stderr, lister.stdout, lister.stderr):
                assert MESSAGING not in out
            assert parse_rows(lister.stdout) == [
                ["claude", "-", "-", str(BASE + 20), sock, key]]
        finally:
            fake.close()


def test_without_a_claude_ancestor_the_hook_fails_and_writes_nothing(staged):
    fake, hook = staged
    fake.add(os.getpid(), 1, "python3", ["python3", "x"])
    r = run_hook(hook, fake.home)
    assert r.returncode == 1
    assert "claude" in r.stderr
    assert r.stdout == ""
    assert not fake.records.exists()
    assert MESSAGING not in r.stderr


def test_the_hook_never_prints_the_token(staged):
    fake, hook = staged
    ok = run_hook(hook, fake.home)
    (fake.records / f"{CLAUDE}.json").unlink()
    fake.records.rmdir()
    fake.records.parent.mkdir(exist_ok=True)
    fake.records.write_text("not a directory", encoding="utf-8")
    failed = run_hook(hook, fake.home)
    assert ok.returncode == 0 and failed.returncode == 1
    for r in (ok, failed):
        assert r.stdout == ""
        assert MESSAGING not in r.stdout and MESSAGING not in r.stderr
    assert "claude-session-start:" in failed.stderr


def test_the_hook_never_reads_the_messaging_token():
    text = HOOK.read_text(encoding="utf-8")
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value != doc:
            assert TOKEN_ENV not in n.value
        if isinstance(n, ast.Name):
            assert n.id != "TOKEN_ENV"
    assert "peerToken" not in text


def test_the_hook_reuses_the_listers_process_rules():
    text = HOOK.read_text(encoding="utf-8")
    tree = ast.parse(text)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)}
    assert not defined & {"read_stat", "read_argv", "runs", "process_table",
                          "ancestors", "read_start_time"}
    strings = {n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "claude" not in strings
    assert ".claude/amap-sessions" not in strings
    assert "openshell-sessions" in text and "SourceFileLoader" in text


def test_the_hook_is_executable_python_names_no_host_and_cites_its_sources():
    text = HOOK.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "#!/usr/bin/env python3"
    assert os.access(HOOK, os.X_OK)
    compile(text, str(HOOK), "exec")
    ast.parse(text, feature_version=(3, 9))
    assert "CLAUDE_CODE_MESSAGING_SOCKET" in text
    assert "OpenShell main@acbac9c:" in text
    assert "/ho" + "me/" not in text
    assert "/Us" + "ers/" not in text
