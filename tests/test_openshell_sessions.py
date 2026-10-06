"""The session lister, payload/openshell-sessions.

Context, at OpenShell main@acbac9c: the main process is the command after `--`
(docs/how-it-works/sandboxes/overview.mdx:23); `/proc` is read-only and `/tmp`
is read-write in the baseline (docs/how-it-works/policies/default-policy.mdx:
34,39,67-68); HOME under a numeric `run_as_user`
(crates/openshell-sandbox/src/process.rs:217-241); and the orphan reaping that
makes the sandbox's workload PID 1
(crates/openshell-sandbox/src/boundary_server.rs:3316).

The row contract is the connector's `find_claude_targets`, `_read_token` and
`one_target` in bin/inbox-delivery at 37875a5. Most tests run a copy of the
lister against a staged /proc tree whose root constant is rewritten. The
socket comes from the record that payload/claude-session-start writes, so a
staged record stands in for it. The key file is the session's own peer key,
which Claude Code writes, so a staged `<pid>.<id>.key` stands in for that. Only the end-to-end test reads a
real /proc tree, the one the test itself creates; no other real process tree is
read for an assertion.
"""

import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import _amap_main
import _harness
import _sessions as s
import _workspace
import policy
import render
from _sessions import (BASE, MAIN_PID, TOKEN, FakeSandbox, connector_daemon,
                       copy_payload, parse_rows, rewritten_lister, run_lister)


FLEET = policy.load_fleet(s.REPO / "examples" / "fleet.json")
HOST = render.Host("/srv/amap", "amap-openshell-agent:test",
                   render.parse_run_as("1000:1000"), False)


@pytest.fixture
def fake(tmp_path):
    with s.short_tmp() as sock_dir:
        sb = FakeSandbox(tmp_path, sock_dir, os.getpid())
        try:
            yield sb
        finally:
            sb.close()


def _copy(fake, tmp_path):
    return rewritten_lister(tmp_path / "openshell-sessions", fake.proc)


def _ok(fake, tmp_path, **kw):
    r = run_lister(fake, tmp_path, **kw)
    assert r.returncode == 0, r.stderr
    return r


def test_no_claude_prints_nothing_and_exits_0(fake, tmp_path):
    r = run_lister(fake, tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_one_claude_with_its_socket_is_one_row(fake, tmp_path):
    sock, key = fake.add_claude(BASE + 20)
    r = _ok(fake, tmp_path)
    assert parse_rows(r.stdout) == [
        ["claude", "-", "-", str(BASE + 20), sock, key]]
    assert sock.startswith(str(fake.sock_dir) + "/")
    assert os.path.dirname(sock) == str(fake.sock_dir)
    assert key.startswith(str(fake.keys))


def test_one_claude_without_its_socket_has_socket_dash(fake, tmp_path):
    sock, key = fake.add_claude(BASE + 20, socket=False)
    assert sock == "-"
    r = _ok(fake, tmp_path)
    assert parse_rows(r.stdout) == [
        ["claude", "-", "-", str(BASE + 20), "-", key]]
    assert key != "-"


def test_two_claude_processes_are_two_rows(fake, tmp_path, monkeypatch):
    a = fake.add_claude(BASE + 21)
    b = fake.add_claude(BASE + 20)
    r = _ok(fake, tmp_path)
    assert parse_rows(r.stdout) == [
        ["claude", "-", "-", str(BASE + 20), *b],
        ["claude", "-", "-", str(BASE + 21), *a]]
    monkeypatch.setenv("HOME", str(fake.home))
    targets = connector_daemon().find_claude_targets(str(_copy(fake, tmp_path)))
    assert [(t.socket_path, t.key_file) for t in targets] == [b, a]


def test_a_claude_under_claude_is_a_second_row(fake, tmp_path):
    fake.add_claude(BASE + 20)
    fake.add_claude(BASE + 21, ppid=BASE + 20)
    r = _ok(fake, tmp_path)
    assert [row[3] for row in parse_rows(r.stdout)] == [
        str(BASE + 20), str(BASE + 21)]


def test_the_wrappers_own_processes_are_not_claude(fake, tmp_path):
    assert "claude" in s.MAIN_ARGV  # amap-main's argument, not a process
    r = _ok(fake, tmp_path)
    assert r.stdout == ""


def test_a_claude_outside_the_main_process_gets_no_row(fake, tmp_path):
    fake.add_claude(BASE + 20, ppid=1)
    fake.add(BASE + 21, 1, "bash", ["bash"])
    fake.add_claude(BASE + 22, ppid=BASE + 21)
    assert _ok(fake, tmp_path).stdout == ""
    inside = fake.add_claude(BASE + 23, ppid=MAIN_PID)
    assert parse_rows(_ok(fake, tmp_path).stdout) == [
        ["claude", "-", "-", str(BASE + 23), *inside]]


@pytest.mark.parametrize("comm,argv,row", [
    ("claude", ["claude"], True),
    ("node", ["node", "/usr/local/bin/claude"], True),
    ("python3", ["python3", "/x/claude"], True),
    ("node", ["node", "/x/cli.js", "claude"], False),
    ("claude-helper", ["claude-helper"], False),
])
def test_claude_is_recognised_by_comm_or_by_argv0_or_argv1(
        fake, tmp_path, comm, argv, row):
    fake.add_claude(BASE + 20, comm=comm, argv=argv)
    r = _ok(fake, tmp_path)
    assert len(parse_rows(r.stdout)) == (1 if row else 0)


@pytest.mark.parametrize("body", ["not json", "[]", '{"pid": "x"}'])
def test_a_garbled_record_gives_no_socket(fake, tmp_path, body):
    _, key = fake.add_claude(BASE + 20, record_text=body)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1 and rows[0][4:] == ["-", key]


def test_a_record_of_an_earlier_process_with_the_same_pid_is_ignored(
        fake, tmp_path):
    _, key = fake.add_claude(BASE + 20, record_start="1")
    assert fake.start_time_of(BASE + 20) != "1"
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1 and rows[0][4:] == ["-", key]


@pytest.mark.parametrize("record_pid", [BASE + 99, True])
def test_a_record_naming_another_pid_is_ignored(fake, tmp_path, record_pid):
    _, key = fake.add_claude(BASE + 20, record_pid=record_pid)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1 and rows[0][4:] == ["-", key]


def test_a_relative_socket_in_the_record_is_dash(fake, tmp_path):
    pid = BASE + 20
    _, key = fake.add_claude(pid, record=False)
    fake.records.mkdir(parents=True)
    (fake.records / f"{pid}.json").write_text(json.dumps({
        "socket": f"{pid}.sock", "pid": pid,
        "start_time": fake.start_time_of(pid)}), encoding="utf-8")
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1
    assert rows[0][4] == "-"
    assert rows[0][5] == key


def test_a_symlinked_record_is_ignored(fake, tmp_path):
    pid = BASE + 20
    sock, key = fake.add_claude(pid)
    rec = fake.records / f"{pid}.json"
    real = tmp_path / "real.json"
    rec.replace(real)
    rec.symlink_to(real)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1 and rows[0][4:] == ["-", key]


@pytest.mark.parametrize("body", [
    "not json", "{}", '{"peerToken": ""}', '{"peerToken": 5}', "[]"])
def test_a_peer_key_without_a_peer_token_is_dash(fake, tmp_path, body):
    sock, _ = fake.add_claude(BASE + 20, peer_key_text=body)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert len(rows) == 1 and rows[0][4] == sock and rows[0][5] == "-"


def test_the_keyfile_column_is_the_sessions_own_peer_key(
        fake, tmp_path, monkeypatch):
    """AC2."""
    pid = BASE + 20
    sock, _ = fake.add_claude(pid, peer_token="p" * 32)
    row = parse_rows(_ok(fake, tmp_path).stdout)[0]
    key = str(fake.keys / f"{pid}.k1.key")
    assert row[5] == key
    assert not row[5].startswith(str(fake.records))
    assert connector_daemon()._read_token(row[5]) == "p" * 32
    monkeypatch.setenv("HOME", str(fake.home))
    copy = str(_copy(fake, tmp_path))
    found = connector_daemon().find_claude_targets(copy)
    assert [(t.socket_path, t.key_file) for t in found] == [(sock, key)]
    Path(key).unlink()
    assert parse_rows(_ok(fake, tmp_path).stdout)[0][5] == "-"


def test_two_valid_peer_keys_for_one_pid_are_dash(fake, tmp_path):
    pid = BASE + 20
    fake.add_claude(pid)
    fake.add_peer_key(pid, "k2", json.dumps({"peerToken": "q" * 32}))
    assert parse_rows(_ok(fake, tmp_path).stdout)[0][5] == "-"


def test_an_invalid_candidate_beside_a_valid_key_is_skipped(fake, tmp_path):
    pid = BASE + 20
    fake.add_claude(pid)
    fake.add_peer_key(pid, "k2", "not json")
    assert parse_rows(_ok(fake, tmp_path).stdout)[0][5] == str(
        fake.keys / f"{pid}.k1.key")


def test_a_symlinked_peer_key_is_dash(fake, tmp_path):
    pid = BASE + 20
    _, key = fake.add_claude(pid)
    real = tmp_path / "real.key"
    Path(key).replace(real)
    Path(key).symlink_to(real)
    assert parse_rows(_ok(fake, tmp_path).stdout)[0][5] == "-"


def test_only_this_pids_keys_are_candidates(fake, tmp_path):
    pid = BASE + 20
    fake.add_claude(pid, peer_key=False)
    valid = json.dumps({"peerToken": TOKEN})
    for name in (f"{pid}0.k1.key", f"{pid + 1}.k1.key", f"{pid}.key"):
        fake.keys.mkdir(parents=True, exist_ok=True)
        (fake.keys / name).write_text(valid, encoding="utf-8")
    assert parse_rows(_ok(fake, tmp_path).stdout)[0][5] == "-"


def test_a_path_with_whitespace_is_dash(fake, tmp_path):
    fake.use_home(tmp_path / "a b")
    fake.add_claude(BASE + 20)
    assert (fake.records / f"{BASE + 20}.json").is_file()
    r = _ok(fake, tmp_path)
    rows = parse_rows(r.stdout)
    assert len(rows) == 1 and len(rows[0]) == 6
    assert rows[0][5] == "-"
    assert rows[0][4] != "-"


def test_a_socket_path_that_is_not_a_socket_is_dash(fake, tmp_path):
    (fake.sock_dir / f"{BASE + 20}.sock").write_text("", encoding="utf-8")
    _, key = fake.add_claude(BASE + 20, socket=False)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert rows[0][4] == "-"
    assert rows[0][5] == key
    fake.add_claude(BASE + 21, socket=False)  # the record names a missing path
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert [r[4] for r in rows] == ["-", "-"]


def _scenario_socket(fake):
    fake.add_claude(BASE + 20)


def _scenario_no_socket(fake):
    fake.add_claude(BASE + 20, socket=False)


def _scenario_two(fake):
    fake.add_claude(BASE + 20)
    fake.add_claude(BASE + 21)


def _scenario_bad_key(fake):
    fake.add_claude(BASE + 20, peer_key_text="not json")


def _scenario_whitespace(fake):
    fake.use_home(fake.root / "a b")
    fake.add_claude(BASE + 20)


@pytest.mark.parametrize("scenario", [
    _scenario_socket, _scenario_no_socket, _scenario_two, _scenario_bad_key,
    _scenario_whitespace])
def test_every_row_has_exactly_six_fields(fake, tmp_path, scenario):
    scenario(fake)
    r = _ok(fake, tmp_path)
    lines = r.stdout.splitlines()
    assert lines
    for line in lines:
        assert len(line.split()) == 6
        items = line.split("\t")
        assert len(items) == 6 and all(items)
        assert items[1] == "-" and items[2] == "-"


def test_the_connector_reads_our_rows(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(fake.home))
    copy = str(_copy(fake, tmp_path))
    assert connector_daemon().find_claude_targets(copy) == []
    sock, key = fake.add_claude(BASE + 20)
    targets = connector_daemon().find_claude_targets(copy)
    assert len(targets) == 1
    assert (targets[0].socket_path, targets[0].key_file) == (sock, key)
    assert connector_daemon()._read_token(key) == TOKEN


def test_without_an_amap_main_ancestor_the_lister_fails(
        fake, tmp_path, monkeypatch):
    fake.add(os.getpid(), 1, "python3", ["python3", "/opt/amap/payload/x"])
    r = run_lister(fake, tmp_path)
    assert r.returncode == 2
    assert "amap-main" in r.stderr
    assert r.stdout == ""
    monkeypatch.setenv("HOME", str(fake.home))
    assert connector_daemon().find_claude_targets(
        str(_copy(fake, tmp_path))) is None


def test_without_home_the_lister_fails(fake, tmp_path):
    copy = _copy(fake, tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    r = subprocess.run([sys.executable, str(copy)], env=env, text=True,
                       capture_output=True, timeout=30)
    assert r.returncode != 0
    assert "HOME" in r.stderr
    assert r.stdout == ""


def test_a_vanishing_process_is_skipped(fake, tmp_path):
    (fake.proc / str(BASE + 30)).mkdir()
    (fake.proc / str(BASE + 31)).mkdir()
    (fake.proc / str(BASE + 31) / "stat").write_text("garbage", encoding="utf-8")
    fake.add_claude(BASE + 20)
    r = _ok(fake, tmp_path)
    assert len(parse_rows(r.stdout)) == 1


def test_a_comm_with_a_paren_and_spaces_is_read(fake, tmp_path):
    fake.add(BASE + 20, MAIN_PID, "a) b (c", ["x"])
    fake.add_claude(BASE + 21, ppid=BASE + 20)
    rows = parse_rows(_ok(fake, tmp_path).stdout)
    assert [r[3] for r in rows] == [str(BASE + 21)]


@pytest.mark.parametrize("with_claude", [False, True])
def test_the_lister_needs_no_tmux(fake, tmp_path, fake_bin, with_claude):
    marker = tmp_path / "tmux-ran"
    _harness.write_fake(fake_bin, "tmux", f"#!/bin/sh\ntouch '{marker}'\n")
    if with_claude:
        sock, key = fake.add_claude(BASE + 20)
    r = _ok(fake, tmp_path, path_prefix=fake_bin)
    assert not marker.exists()
    if with_claude:
        assert parse_rows(r.stdout) == [
            ["claude", "-", "-", str(BASE + 20), sock, key]]
    else:
        assert r.stdout == ""
    source = s.LISTER.read_text(encoding="utf-8")
    assert "subprocess" not in source and "os.system" not in source


def _module_of_lister():
    loader = importlib.machinery.SourceFileLoader(
        "openshell_sessions_under_test", str(s.LISTER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_the_lister_never_prints_a_token(fake, tmp_path):
    fake.add_claude(BASE + 20)
    fake.add_claude(BASE + 21, peer_key_text='{"peerToken": "%s"}'
                    % ("c" * 32))
    r = _ok(fake, tmp_path)
    assert len(parse_rows(r.stdout)) == 2
    for token in (TOKEN, "c" * 32):
        assert token not in r.stdout and token not in r.stderr


def test_the_lister_takes_the_socket_from_the_record_and_the_key_from_claude_code():
    mod = _module_of_lister()
    assert mod.RECORD_SUBDIR == ".claude/amap-sessions"
    assert mod.RECORD_NAME == "{pid}.json"
    assert mod.KEY_SUBDIR == ".claude/sessions"
    assert mod.PEER_TOKEN == "peerToken"
    assert not hasattr(mod, "RECORD_TOKEN") and not hasattr(mod, "SOCK_DIR")
    text = s.LISTER.read_text(encoding="utf-8")
    assert "cc-socks" not in text
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value != doc:
            assert "CLAUDE_CODE_MESSAGING_TOKEN" not in n.value


def test_under_amap_main_the_hook_records_the_session_the_lister_reports(
        tmp_path):
    """AC2. The real /proc tree is the one this test creates: the wrapper, its
    fake claude, the hook that claude runs, and the daemon that runs the
    unmodified lister."""
    token2 = "b" * 32
    with s.short_tmp() as sock_dir:
        lab = _amap_main.Lab(tmp_path)
        outsider = None
        try:
            daemon = lab.payload / "inbox-delivery"
            daemon.write_text(s.LISTING_DAEMON, encoding="utf-8")
            daemon.chmod(0o755)
            copy_payload(lab.payload, "openshell-sessions",
                         "claude-session-start", render.SETTINGS_NAME)
            log = tmp_path / "listing.log"
            records = lab.home / ".claude" / "amap-sessions"

            outsider_env = dict(os.environ)
            outsider_env["FAKE_CLAUDE_LOG"] = str(tmp_path / "outsider.log")
            outsider_env["FAKE_CLAUDE_RELEASE"] = str(tmp_path / "outsider.go")
            outsider = subprocess.Popen(
                [sys.executable, str(lab.bin / "claude")], env=outsider_env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            with open(f"/proc/{outsider.pid}/stat", "rb") as fh:
                text = fh.read().decode()
            start = text.rsplit(")", 1)[1].split()[19]
            records.mkdir(parents=True)
            (records / f"{outsider.pid}.json").write_text(json.dumps({
                "socket": str(sock_dir / "x.sock"),
                "pid": outsider.pid, "start_time": start}), encoding="utf-8")

            args = render.render_member(
                FLEET, "alpha", HOST, "/srv/amap/policies/alpha.yaml").argv
            args = args[args.index("--") + 2:]
            env = lab.env(
                FAKE_CLAUDE_HOOKS="1",
                FAKE_PAYLOAD_TARGET=render.PAYLOAD_TARGET,
                FAKE_PAYLOAD_DIR=str(lab.payload),
                FAKE_SOCK_DIR=str(sock_dir),
                FAKE_MESSAGING_TOKEN=token2, FAKE_PEER_TOKEN="p" * 32,
                FAKE_CONNECTOR_DAEMON=str(
                    _workspace.CONNECTOR_ROOT / "bin" / "inbox-delivery"),
                FAKE_LISTING_LOG=str(log), PYTHONDONTWRITEBYTECODE="1")
            proc = lab.start(*args, env=env)
            lab.wait_for(lambda: any(e["event"] == "hook"
                                     for e in lab.claude_events()),
                         "the SessionStart hook")
            started = [e for e in lab.claude_events()
                       if e["event"] == "start"][0]
            hook = [e for e in lab.claude_events() if e["event"] == "hook"][0]
            c = started["pid"]

            def entries():
                try:
                    lines = log.read_text(encoding="utf-8").splitlines()
                except FileNotFoundError:
                    return []
                out = []
                for ln in lines:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        pass
                return out

            def hit():
                return [e for e in entries() if f"{c}.sock" in e["stdout"]]

            lab.wait_for(lambda: hit(), "a listing that names the socket")
            e = hit()[0]
            sock = str(sock_dir / f"{c}.sock")
            record = records / f"{c}.json"
            peer_key = f"{lab.home}/.claude/sessions/{c}.s1.key"
            assert e["rc"] == 0
            assert e["stdout"].splitlines() == [
                f"claude\t-\t-\t{c}\t{sock}\t{peer_key}"]
            assert e["targets"] == [[sock, peer_key]]
            assert connector_daemon()._read_token(peer_key) == "p" * 32
            assert set(json.loads(record.read_text(encoding="utf-8"))) == {
                "socket", "pid", "start_time"}
            assert stat.S_IMODE(os.stat(record).st_mode) == 0o600
            for f in lab.home.rglob("*"):
                if f.is_file():
                    assert token2.encode() not in f.read_bytes(), f
            assert (hook["rc"], hook["stdout"], hook["stderr"]) == (0, "", "")
            assert hook["socket"] == sock
            for x in entries():
                assert token2 not in x["stdout"]
                assert str(outsider.pid) not in x["stdout"]
            assert lab.finish(proc) == 0
            assert token2 not in lab.stderr()
            assert token2 not in lab.stdout_path.read_text(encoding="utf-8")
            assert token2 not in hook["stdout"] + hook["stderr"]
        finally:
            lab.close()
            if outsider is not None:
                outsider.kill()
                outsider.wait()


def test_the_lister_is_what_amap_main_points_the_daemon_at():
    assert s.LISTER.is_file() and os.access(s.LISTER, os.X_OK)
    text = s.LISTER.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "#!/usr/bin/env python3"
    wrapper = _amap_main.WRAPPER.read_text(encoding="utf-8").splitlines()
    assert ('AMAP_DELIVERY_SESSION_SOURCE="$SELF_DIR/openshell-sessions"'
            in wrapper)
    compile(text, str(s.LISTER), "exec")


def test_the_lister_cites_openshell_and_names_no_host():
    text = s.LISTER.read_text(encoding="utf-8")
    assert "OpenShell main@acbac9c:" in text
    assert "docs/how-it-works/sandboxes/overview.mdx" in text
    assert "/ho" + "me/" not in text
    assert "/Us" + "ers/" not in text
