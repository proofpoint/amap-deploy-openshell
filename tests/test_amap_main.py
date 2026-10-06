"""The sandbox's main process, payload/amap-main.

OpenShell facts the wrapper relies on, at OpenShell main@acbac9c:
docs/how-it-works/sandboxes/overview.mdx:23 (the sandbox supervises the one
command after `--`), :36-48 (the restart policy acts on that command's exit)
and :366-374 (`--env` reaches every process in the sandbox).

The daemon's contract is amap-connector-claude `bin/inbox-delivery`
(`ENV_KEYS`, `SELF_ENV`, `run`, at d34ccbf). It is parsed from that source at
test time, and the lane names come from the router. The wrapper runs under
/bin/sh against fakes, so no daemon, `claude`, OpenShell or router runs.
"""

import json
import os
import re
import signal
import subprocess
from pathlib import Path

import pytest

import _amap_main as m
from _amap_main import (Lab, WRAPPER, connector_contract, parse_contract,
                        pid_alive, router_facts, wrapper_constant)

SELF = "alpha@agents.example.org"


@pytest.fixture
def lab(tmp_path):
    the_lab = Lab(tmp_path)
    try:
        yield the_lab
    finally:
        the_lab.close()


def _daemon_env(lab):
    return lab.starts()[0]["env"]


def test_the_daemon_receives_exactly_the_connectors_required_set_plus_self(lab):
    c = connector_contract()
    assert lab.run_once() == 0
    assert set(_daemon_env(lab)) == set(c.required) | {c.self_env}
    assert len(c.required) == 8
    assert c.self_env not in c.required


def test_inherited_delivery_variables_do_not_reach_the_daemon(lab):
    c = connector_contract()
    extra = {k: "/stale" for k in c.required}
    extra[c.receipt_env] = "1"
    extra["AMAP_DELIVERY_EXTRA"] = "x"
    assert lab.run_once(env=lab.env(**extra)) == 0
    got = _daemon_env(lab)
    assert set(got) == set(c.required) | {c.self_env}
    assert "/stale" not in got.values()
    assert c.receipt_env not in got


def test_without_a_self_address_the_daemon_gets_the_required_set_alone(lab):
    c = connector_contract()
    env = lab.env(**{m.SELF_INPUT: None, c.self_env: "forged@example.org"})
    assert lab.run_once(env=env) == 0
    assert set(_daemon_env(lab)) == set(c.required)


def test_an_empty_self_address_is_refused_by_name(lab):
    proc = lab.start(env=lab.env(**{m.SELF_INPUT: ""}))
    rc = proc.wait(10)
    assert rc != 0
    assert m.SELF_INPUT in lab.stderr()
    assert lab.daemon_events() == []
    assert lab.claude_events() == []


def test_each_derived_path(lab):
    f = router_facts()
    c = connector_contract()
    home = lab.home
    inbox = lab.lanes / "inbox"
    peer = lab.lanes / "peer"
    outbox = lab.lanes / "outbox"

    def leaf(lane, prefix):
        hits = [x for x in f["leaves"][lane] if x.startswith(prefix)]
        assert len(hits) == 1, (lane, prefix, hits)
        return hits[0]

    expected = {
        "AMAP_DELIVERY_MAIL_NOTICE_DIR":
            str(inbox / leaf(f["inbox"], "notice")),
        "AMAP_DELIVERY_MAIL_CLAIM":
            str(home / ".claude/connector/claims/mail.amap-consumer.json"),
        "AMAP_DELIVERY_PEER_NOTICE_DIR":
            str(peer / leaf(f["peer"], "notice")),
        "AMAP_DELIVERY_PEER_MESSAGE_DIR":
            str(peer / leaf(f["peer"], "message")),
        "AMAP_DELIVERY_PEER_CLAIM":
            str(home / ".claude/connector/claims/peer.amap-consumer.json"),
        "AMAP_DELIVERY_STATE_DIR":
            str(home / ".claude/connector/delivery-state"),
        "AMAP_DELIVERY_OUTCOME_DIR": str(outbox / f["outcomes_rel"]),
        "AMAP_DELIVERY_SESSION_SOURCE":
            str(lab.payload / "openshell-sessions"),
        c.self_env: SELF,
    }
    assert set(expected) == set(c.required) | {c.self_env}
    assert lab.run_once() == 0
    got = _daemon_env(lab)
    for name, value in expected.items():
        assert got[name] == value, name
    # the lab's lane directories carry the router's lane names
    assert inbox.name == f["inbox"]
    assert peer.name == f["peer"]
    assert outbox.name == f["outbox"]


def test_the_daemons_private_state_lives_under_home(lab):
    c = connector_contract()
    assert lab.run_once() == 0
    got = _daemon_env(lab)
    private = [k for k in c.required
               if k.endswith("_CLAIM") or k.endswith("_STATE_DIR")]
    assert private
    for name in private:
        Path(got[name]).relative_to(lab.home)


def test_a_killed_daemon_is_restarted(lab):
    proc = lab.start()
    lab.wait_for(lambda: len(lab.starts()) >= 1, "the first daemon start")
    os.kill(lab.starts()[0]["pid"], signal.SIGKILL)
    lab.wait_for(lambda: len(lab.starts()) >= 2, "the second daemon start")
    starts = lab.starts()
    assert starts[0]["pid"] != starts[1]["pid"]
    assert lab.sleeps() == [wrapper_constant("BACKOFF_FIRST_SECONDS")]
    assert proc.poll() is None
    assert "restarting in" in lab.stderr()
    assert lab.finish(proc) == 0


def test_a_daemon_that_always_exits_gets_growing_delays(lab):
    env = lab.env(FAKE_DAEMON_MODE="exit", FAKE_SLEEP_SCALE="0.1")
    lab.start(env=env)
    lab.wait_for(lambda: len(lab.starts()) >= 4, "four daemon starts")
    s = lab.sleeps()[:3]
    assert len(s) == 3
    assert s[0] >= 1
    assert s[0] < s[1] < s[2]
    t = [e["t"] for e in lab.starts()]
    gaps = [t[i + 1] - t[i] for i in range(3)]
    for i in range(3):
        assert gaps[i] >= 0.1 * s[i], (gaps, s)
    assert gaps[2] > gaps[0]
    assert len(lab.starts()) <= 5


def test_the_backoff_doubles_to_its_cap(lab):
    first = wrapper_constant("BACKOFF_FIRST_SECONDS")
    cap = wrapper_constant("BACKOFF_MAX_SECONDS")
    assert 1 <= first < cap
    env = lab.env(FAKE_DAEMON_MODE="exit", FAKE_SLEEP_SCALE="0.001")
    lab.start(env=env)
    lab.wait_for(lambda: len(lab.sleeps()) >= 9, "nine backoff sleeps")
    want = [min(first * 2 ** i, cap) for i in range(9)]
    assert lab.sleeps()[:9] == want
    assert want.count(cap) >= 2


def test_a_daemon_that_ran_long_enough_restarts_from_the_first_delay(tmp_path):
    the_lab = Lab(tmp_path,
                  edits={"HEALTHY_AFTER_SECONDS=60": "HEALTHY_AFTER_SECONDS=1"})
    try:
        first = wrapper_constant("BACKOFF_FIRST_SECONDS")
        env = the_lab.env(FAKE_DAEMON_MODE="run-then-exit",
                          FAKE_DAEMON_RUN_SECONDS="1.5")
        the_lab.start(env=env)
        the_lab.wait_for(lambda: len(the_lab.sleeps()) >= 2, "two sleeps")
        # without the reset the second delay would be 2 * first
        assert the_lab.sleeps()[:2] == [first, first]
    finally:
        the_lab.close()


@pytest.mark.parametrize(
    "var", ["AMAP_INBOX_DIR", "AMAP_PEER_DIR", "AMAP_OUTBOX_DIR", "HOME"],
    ids=lambda v: v)
def test_a_missing_input_exits_nonzero_and_names_it(lab, var):
    proc = lab.start(env=lab.env(**{var: None}))
    rc = proc.wait(10)
    assert rc != 0
    assert var in lab.stderr()
    assert lab.daemon_events() == []
    assert lab.claude_events() == []


def test_an_empty_lane_variable_is_missing_too(lab):
    proc = lab.start(env=lab.env(AMAP_PEER_DIR=""))
    rc = proc.wait(10)
    assert rc != 0
    assert "AMAP_PEER_DIR" in lab.stderr()
    assert lab.daemon_events() == []
    assert lab.claude_events() == []


@pytest.mark.parametrize("code", [0, 1, 3, 42])
def test_the_wrapper_exits_with_claudes_status(lab, code):
    assert lab.run_once(env=lab.env(FAKE_CLAUDE_EXIT=str(code))) == code


def test_claude_exiting_stops_the_daemon_with_sigterm(lab):
    proc = lab.start()
    lab.wait_for(lambda: len(lab.starts()) >= 1, "a daemon start")
    lab.wait_for(lambda: len(lab.claude_events()) >= 1, "a claude start")
    assert lab.finish(proc) == 0
    events = lab.daemon_events()
    assert [e["event"] for e in events] == ["start", "term"]
    assert events[0]["pid"] == events[1]["pid"]
    assert not pid_alive(events[0]["pid"])
    assert not lab.group_alive(proc)


def test_the_wrapper_never_deletes_a_claim(lab):
    c = connector_contract()
    env = lab.env(FAKE_DAEMON_MODE="keep-claims")
    assert lab.run_once(env=env) == 0
    got = _daemon_env(lab)
    claims = [got[k] for k in c.required if k.endswith("_CLAIM")]
    assert claims
    for path in claims:
        assert Path(path).exists(), path
    # the text has no way to delete one either
    for line in WRAPPER.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"(^|[\s;&|(])(rm|unlink|rmdir)\s", code), line
        assert "-delete" not in code, line


def test_sigterm_to_the_wrapper_reaches_claude_then_the_daemon(lab):
    proc = lab.start()
    lab.wait_for(lambda: len(lab.starts()) >= 1, "a daemon start")
    lab.wait_for(lambda: len(lab.claude_events()) >= 1, "a claude start")
    os.kill(proc.pid, signal.SIGTERM)
    assert proc.wait(30) == 7
    assert any(e["event"] == "term" for e in lab.claude_events())
    assert any(e["event"] == "term" for e in lab.daemon_events())
    assert not lab.group_alive(proc)


def test_claude_gets_the_wrappers_arguments_and_stdin(lab):
    args = ("--mcp-config", "/opt/amap/payload/mcp-servers.json",
            "--append-system-prompt-file", "a b")
    env = lab.env(FAKE_CLAUDE_READ_STDIN="1")
    assert lab.run_once(*args, env=env, input_text="hello\n") == 0
    start = [e for e in lab.claude_events() if e["event"] == "start"][0]
    assert start["argv"] == list(args)
    assert start["stdin"] == "hello\n"


def _start_event(lab):
    return [e for e in lab.claude_events() if e["event"] == "start"][0]


def test_the_first_run_answers_are_seeded_before_claude_starts(lab):
    (lab.home / ".claude.json").write_text('{"keep": 1}', encoding="utf-8")
    assert lab.run_once() == 0
    text = _start_event(lab)["claude_json"]
    assert m.FAKE_API_KEY not in text
    doc = json.loads(text)
    assert doc["keep"] == 1
    assert doc["hasCompletedOnboarding"] is True
    assert doc["lastOnboardingVersion"] == "2.1.284"
    workdir = os.path.realpath(lab.tmp / "work")
    assert doc["projects"][workdir]["hasTrustDialogAccepted"] is True
    assert doc["customApiKeyResponses"]["approved"] == [m.FAKE_API_KEY[-20:]]


@pytest.mark.parametrize("failure", ["key absent", "key empty", "corrupt file"])
def test_a_seeding_failure_starts_neither_the_daemon_nor_claude(lab, failure):
    corrupt = b"{not json"
    env = lab.env()
    if failure == "key absent":
        env = lab.env(ANTHROPIC_API_KEY=None)
    elif failure == "key empty":
        env = lab.env(ANTHROPIC_API_KEY="")
    else:
        (lab.home / ".claude.json").write_bytes(corrupt)
    proc = lab.start(env=env)
    assert proc.wait(15) != 0
    assert "claude-seed" in lab.stderr()
    assert lab.daemon_events() == []
    assert not [e for e in lab.claude_events() if e["event"] == "start"]
    if failure == "corrupt file":
        assert (lab.home / ".claude.json").read_bytes() == corrupt
    assert m.FAKE_API_KEY not in lab.stderr()


def test_the_contract_parser_fails_loudly():
    with pytest.raises(AssertionError) as e:
        parse_contract("X = 1\n")
    for name in ("ENV_KEYS", "SELF_ENV", "RECEIPT_WINDOW_ENV"):
        assert name in str(e.value)
    with pytest.raises(AssertionError) as e:
        parse_contract('ENV_KEYS = ("A",)\n')
    assert "SELF_ENV" in str(e.value)


def test_the_wrapper_is_an_executable_posix_sh_script():
    text = WRAPPER.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "#!/bin/sh"
    assert os.access(WRAPPER, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(WRAPPER)]).returncode == 0


def test_the_wrapper_cites_openshell_and_names_no_host():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "OpenShell main@acbac9c:" in text
    assert "docs/how-it-works/sandboxes/overview.mdx" in text
    # spelled in pieces so this file is itself free of them
    for prefix in ("/ho" + "me/", "/Us" + "ers/"):
        assert prefix not in text
