"""The `l1-run` verb: docs/L1-RUNBOOK.md steps 0-9 as one command.

Every test runs the real runner against fakes for `openshell`, `docker`, `ss`
and `openshell-gateway` (tests/_l1_world.py), under the suite's binary guard.
Nothing runs OpenShell, Docker or the router. The fakes share one JSON world and
simulate what the router would do on the host: they write the router's audit
lines with the router's own `router.audit.build_line`, and documents that are
copies of amap-spec's valid fixtures.

Facts relied on, at OpenShell main@acbac9c: `sandbox exec` is
crates/openshell-cli/src/main.rs:1681-1727 and its process is not a descendant
of the main process (crates/openshell-sandbox/src/boundary_exec.rs:422-444),
which is why the runner loads the lister as a module (`SESSION_PROBE`);
`sandbox get --output json` is crates/openshell-cli/src/run.rs:2789-2797.

Facts relied on in amap-router-local: the audit log layout and events
(`router/audit.py`), the held copy (`router/outbound.py:196-197`) and the roster
(`router/roster.py:113`). A test cross-checks each name the runner pins against
the router itself.

Nothing here states a total of passes or failures.
"""

import ast
import datetime
import io
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _amap_main
import _harness
import _l1_world
import _sessions
import _workspace
import amap_openshell
import l1_kit
import l1_run
import membership
import provider_profile
import render
import router_config
import test_l1_kit
import test_l1_runbook
from l1_run import (CHECKS, EXIT_CHECKS, EXIT_FAILED, EXIT_OK, Settings,
                    StepResult)

REPO = Path(__file__).absolute().parents[1]
RUNBOOK = REPO / "docs" / "L1-RUNBOOK.md"
FAST = Settings(poll_interval=1, command_timeout=60, build_timeout=60,
                create_timeout=60, exec_timeout=30, ready_timeout=10,
                stop_timeout=10, router_timeout=10, delivery_timeout=10,
                reply_timeout=10, log_timeout=5)
DRY_LAST_LINE = "dry run: nothing was run or written; add --apply to run"


class Clock:
    """A fake monotonic clock that only `sleep` advances."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def fixed_utc():
    return datetime.datetime(2026, 9, 29, 12, 0, 0,
                             tzinfo=datetime.timezone.utc)


def go(world, *, apply=True, from_step=None, only_step=None, settings=FAST,
       env=None):
    """Runs the runner against `world`. Returns `(exit code, printed text)`."""
    clock = Clock()
    out = io.StringIO()
    runner = l1_run.Runner(
        env if env is not None else world.env(), apply,
        l1_run.selected_steps(from_step, only_step), settings=settings,
        out=out, now=fixed_utc, sleep=clock.advance, clock=clock.now)
    return runner.run(), out.getvalue()


@pytest.fixture
def world(tmp_path, fake_bin):
    return _l1_world.World(tmp_path, fake_bin)


@pytest.fixture
def runner():
    return go


class Walk:
    def __init__(self, world, code, out):
        self.world, self.code, self.out = world, code, out

    @property
    def evidence(self):
        return self.world.home / "evidence"


@pytest.fixture(scope="module")
def walked(tmp_path_factory):
    """One full `--apply` walk, shared by the tests that only read it."""
    tmp = tmp_path_factory.mktemp("walk")
    fake_bin = tmp / "fakebin"
    fake_bin.mkdir()
    with _harness.stubbed(fake_bin):
        w = _l1_world.World(tmp, fake_bin)
        code, out = go(w)
    return Walk(w, code, out)


def walk_fresh(tmp_path, fake_bin, **flags):
    w = _l1_world.World(tmp_path, fake_bin, **flags)
    code, out = go(w)
    return Walk(w, code, out)


def make_ctx(world):
    """A context with the inputs adopted, for the pure builders."""
    ctx = l1_run.Runner(world.env(), False, l1_run.STEPS, settings=FAST).ctx
    inputs, findings = l1_run.load_inputs(ctx.env, ctx.uid_gid())
    assert inputs is not None, findings
    ctx.adopt(inputs)
    return ctx


def evidence_docs(evidence, step=None):
    """Every JSON record under `evidence/step-*`, as `(path, doc)`."""
    out = []
    for d, _, files in os.walk(str(evidence)):
        if step is not None and os.path.basename(d) != f"step-{step}":
            continue
        for f in sorted(files):
            if f.endswith(".json"):
                p = Path(d) / f
                out.append((p, json.loads(p.read_text(encoding="utf-8"))))
    return out


def commands(evidence):
    return [d for _, d in evidence_docs(evidence) if d.get("kind") == "command"]


def result_of(evidence, step, run_id=None):
    """The `*-result.json` of a step, for the run named or the only one."""
    found = [p for p in sorted((evidence / f"step-{step}").glob("*-result.json"))
             if re.match(r"^\d{8}T\d{6}Z(-\d+)?-result\.json$", p.name)]
    if run_id is not None:
        found = [p for p in found if p.name == f"{run_id}-result.json"]
    assert len(found) == 1, (step, [p.name for p in found])
    return json.loads(found[0].read_text(encoding="utf-8"))


def checks_of(evidence):
    doc = json.loads((evidence / "step-8" / "checks.json").read_text(
        encoding="utf-8"))
    return {c["heading"]: c for c in doc["checks"]}, doc["checks"]


def is_create(call, name=None):
    a = call["argv"]
    return (call["prog"] == "openshell" and a[2:4] == ["sandbox", "create"]
            and (name is None or a[a.index("--name") + 1] == name))


def is_image_probe(call):
    a = call["argv"]
    return (call["prog"] == "docker" and a[:1] == ["run"] and "--rm" in a
            and "amap-openshell-agent" in a)


def is_profile_call(call, verb=None):
    a = call["argv"]
    return (call["prog"] == "openshell" and a[2:3] == ["provider"]
            and (verb is None or a[3:5] == ["profile", verb]))


def is_mutating(call):
    a = call["argv"]
    if call["prog"] == "docker":
        if is_image_probe(call):   # a read of the image
            return False
        return a[0] in ("build", "run", "rm", "stop", "start", "kill")
    if call["prog"] == "openshell":
        if a[2:3] == ["sandbox"] and a[3] in ("create", "stop", "start",
                                              "delete"):
            return True
        if a[2:3] == ["provider"] and (
                a[3:4] == ["create"]
                or a[3:5] in (["profile", "import"], ["profile", "update"],
                              ["profile", "delete"])):
            return True
        return (any(w.endswith("inbox-submit") for w in a) and "submit" in a)
    return False


def gateway_mutating(call):
    """A mutating call that touches the gateway or the router (a build of the
    image is not one)."""
    return is_mutating(call) and not (call["prog"] == "docker"
                                      and call["argv"][0] == "build")


# --- AC1 ----------------------------------------------------------------------

def test_a_dry_run_executes_nothing_and_writes_nothing(tmp_path, fake_bin,
                                                       world, runner):
    """AC1: a dry run runs no command and writes no file."""
    before = test_l1_kit.tree(tmp_path)
    code, out = runner(world, apply=False)
    assert code == EXIT_OK, out
    assert test_l1_kit.tree(tmp_path) == before
    assert not world.calls_path.exists()
    assert not world.home.exists()
    assert out.rstrip("\n").splitlines()[-1] == DRY_LAST_LINE
    for text in ("docker build", "l1-kit prepare", "provider profile lint",
                 "list-profiles", "provider profile import", "l1-kit profile",
                 "readlink -f", "sandbox create",
                 "sandbox get", "l1-kit record", "run.sh", "inbox-submit"):
        assert text in out, text
    for c in CHECKS:
        assert c.heading in out, c.heading


def dry_labels(out):
    return set(re.findall(r"  # (\S+)$", out, re.M))


def test_the_dry_run_prints_every_command_the_apply_run_issues(
        walked, tmp_path, fake_bin):
    """AC1: `describe_step` and apply mode share the label names."""
    world = _l1_world.World(tmp_path, fake_bin)
    _, out = go(world, apply=False)
    printed = dry_labels(out)
    used = {c["label"] for c in commands(walked.evidence)}
    assert used
    assert used <= printed, sorted(used - printed)
    for c in CHECKS:
        assert c.heading in out


def test_the_executor_refuses_to_run_in_a_dry_run():
    ex = l1_run.Executor({}, False, None, l1_run.Redactor([]), io.StringIO(),
                         time.monotonic)
    with pytest.raises(l1_run.RunnerError):
        ex.run(0, "x", ["true"], timeout=5)


# --- AC2 ----------------------------------------------------------------------

def test_apply_against_fakes_walks_every_step_and_drafts_the_report(walked):
    """AC2: steps 0-9 leave evidence, and the report is drafted."""
    assert walked.code == EXIT_CHECKS, walked.out
    ev = walked.evidence
    for n in range(10):
        r = result_of(ev, n)
        want = "DONE" if n in (8, 9) else "PASS"
        assert r["outcome"] == want, (n, r)
    recs = commands(ev)
    assert recs
    for rec in recs:
        assert isinstance(rec["argv"], list) and rec["argv"]
        assert isinstance(rec["exit"], int)
        for key in ("started", "seconds", "stdout", "stderr"):
            assert key in rec
    by, ordered = checks_of(ev)
    assert [c["heading"] for c in ordered] == [c.heading for c in CHECKS]
    pc1 = by["Pass criterion 1"]
    assert pc1["outcome"] == "UNKNOWN"
    assert "directory-" in pc1["reason"] and "identity-" in pc1["reason"]
    listing = None
    for rel in pc1["evidence"]:
        doc = json.loads((ev / rel).read_text(encoding="utf-8"))
        if doc.get("label") == "documents":
            listing = doc["observed"]
    assert listing, "Pass criterion 1 recorded no document listing"
    assert all(item["errors"] == [] for item in listing)
    assert all(item["schema"].endswith(".schema.json") for item in listing)
    for c in ordered:
        if c["heading"] != "Pass criterion 1":
            assert c["outcome"] == "PASS", c
    assert (ev / "POC-REPORT.md").is_file()


def test_the_calls_follow_the_runbook_order(walked):
    calls = walked.world.calls()

    def first(pred):
        return next(i for i, c in enumerate(calls) if pred(c))

    build = first(lambda c: c["prog"] == "docker" and c["argv"][:1] == ["build"]
                  and "amap-openshell-agent" in c["argv"])
    create = first(is_create)
    run = first(lambda c: c["prog"] == "docker" and c["argv"][:1] == ["run"]
                and not is_image_probe(c))
    probe = first(is_image_probe)
    imp = first(lambda c: is_profile_call(c, "import"))
    submit = first(lambda c: any(w.endswith("inbox-submit")
                                 for w in c["argv"]) and "submit" in c["argv"])
    assert build < probe < imp < create < run < submit
    state = walked.world.state()
    recs = {c["label"]: c for c in commands(walked.evidence)}
    for name in ("alpha", "beta"):
        created = first(lambda c, n=name: is_create(c, n))
        later_get = max(i for i, c in enumerate(calls)
                        if c["prog"] == "openshell"
                        and c["argv"][2:5] == ["sandbox", "get", name])
        assert created < later_get
        # The ID that `record` was given is the one the create made.
        argv = recs[f"record-apply-{name}"]["argv"]
        assert argv[argv.index(name, 5) + 1] == state["sandboxes"][name]["id"]


# --- stopping and gating ------------------------------------------------------

def test_steps_0_to_7_stop_at_the_first_fail(tmp_path, fake_bin):
    w = _l1_world.World(tmp_path, fake_bin, fail_build=True)
    code, out = go(w)
    assert code == EXIT_FAILED
    ev = w.home / "evidence"
    assert result_of(ev, 1)["outcome"] == "FAIL"
    assert not any(is_create(c) for c in w.calls())
    assert not (ev / "step-3").exists()


def write_toml(world, extra=""):
    world.gateway_toml.write_text(_l1_world.GATEWAY_TOML + extra,
                                  encoding="utf-8")


D6_CASES = {
    "bind-address": lambda w: write_toml(
        w, '\n[openshell.gateway.network]\nbind_address = "0.0.0.0:17670"\n'),
    "non-loopback-listener": lambda w: w.set_flag(listen="0.0.0.0:17670"),
    "another-sandbox": lambda w: w.set_flag(extra_sandboxes=["stranger"]),
}


@pytest.mark.parametrize("case", sorted(D6_CASES))
def test_the_runner_refuses_to_go_on_when_d6_fails(case, tmp_path, fake_bin):
    """The gates hold, and they hold again when a run resumes."""
    w = _l1_world.World(tmp_path, fake_bin)
    D6_CASES[case](w)
    code, out = go(w)
    assert code == EXIT_FAILED
    assert "refusing to go on" in out and "D6" in out, out
    failed_step = 0 if case == "bind-address" else 2
    assert f"step {failed_step}: FAIL" in out
    assert not any(gateway_mutating(c) for c in w.calls()), w.calls()
    before = len(w.calls())
    code, out = go(w, from_step=7)
    assert code == EXIT_FAILED
    assert "refusing to go on" in out and "D6" in out, out
    assert not any(gateway_mutating(c) for c in w.calls())
    assert not any(is_create(c) for c in w.calls())
    assert not any(c["argv"][:1] == ["run"] for c in w.calls()
                   if c["prog"] == "docker")
    assert len(w.calls()) >= before


def test_a_missing_prerequisite_refuses_in_a_dry_run_too(world):
    env = world.env()
    del env["ANTHROPIC_API_KEY"]
    for apply in (False, True):
        code, out = go(world, apply=apply, env=env)
        assert code == EXIT_FAILED
        assert "ANTHROPIC_API_KEY is not set" in out
        assert "step 0: FAIL" in out
    assert not world.calls_path.exists()
    assert not world.home.exists()


# --- step 8 -------------------------------------------------------------------

def outcome_returning(outcome, reason="patched"):
    def run(ctx, r):
        return l1_run.CheckOutcome(outcome, reason, (), ())
    return run


def test_step_8_runs_every_check_and_unknown_is_never_a_pass(
        tmp_path, fake_bin, monkeypatch):
    w = _l1_world.World(tmp_path, fake_bin, network_reachable=True,
                        errno="EACCES")
    code, out = go(w)
    assert code == EXIT_CHECKS, out
    by, ordered = checks_of(w.home / "evidence")
    assert len(ordered) == len(CHECKS)
    assert by["Pass criterion 4"]["outcome"] == "FAIL"
    assert by["Unknown 2"]["outcome"] == "FAIL"
    assert "EACCES" in by["Unknown 2"]["reason"]

    def patched(unknown_at):
        return tuple(
            c._replace(run=outcome_returning(
                "UNKNOWN" if c.heading == unknown_at else "PASS"))
            for c in CHECKS)

    # With every check patched, one UNKNOWN is enough to refuse an exit of 0,
    # and with none of them the same run exits 0.
    for name, unknown_at, want in (("second", "Unknown 5", EXIT_CHECKS),
                                   ("third", "nothing", EXIT_OK)):
        monkeypatch.setattr(l1_run, "CHECKS", patched(unknown_at))
        (tmp_path / name).mkdir()
        code, out = go(_l1_world.World(tmp_path / name, fake_bin))
        assert code == want, (name, out)


def test_a_check_that_raises_is_unknown(tmp_path, fake_bin, monkeypatch):
    def boom(ctx, r):
        raise ValueError("boom")

    patched = tuple(c._replace(run=boom) if c.heading == "Pass criterion 3"
                    else c for c in CHECKS)
    monkeypatch.setattr(l1_run, "CHECKS", patched)
    w = _l1_world.World(tmp_path, fake_bin)
    code, out = go(w)
    assert code == EXIT_CHECKS
    by, ordered = checks_of(w.home / "evidence")
    assert by["Pass criterion 3"]["outcome"] == "UNKNOWN"
    assert "ValueError" in by["Pass criterion 3"]["reason"]
    assert len(ordered) == len(CHECKS)
    for heading, c in by.items():
        if heading not in ("Pass criterion 3", "Pass criterion 1"):
            assert c["outcome"] == "PASS", heading


def test_the_negative_case_restores_the_receiver(walked):
    sb = walked.world.state()["sandboxes"]["beta"]
    assert sb["refuse_file"] is False and sb["refuse_active"] is False
    assert sb["phase"] == "Ready"
    assert (sb["stops"], sb["starts"]) == (2, 2)
    by, _ = checks_of(walked.evidence)
    assert any("refused" in f for f in by["Unknown 4"]["facts"])


def test_a_receiver_that_counts_silence_as_delivered_fails(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin, silent_refuse=True)
    assert walk.code == EXIT_CHECKS
    by, _ = checks_of(walk.evidence)
    assert by["Unknown 4"]["outcome"] == "FAIL"
    assert "silence" in by["Unknown 4"]["reason"]
    assert by["Unknown 5"]["outcome"] == "FAIL"
    sb = walk.world.state()["sandboxes"]["beta"]
    assert sb["refuse_file"] is False and sb["phase"] == "Ready"


def test_a_missing_reply_fails_step_7(tmp_path, fake_bin):
    w = _l1_world.World(tmp_path, fake_bin, reply=False)
    code, out = go(w)
    assert code == EXIT_FAILED
    ev = w.home / "evidence"
    r = result_of(ev, 7)
    assert r["outcome"] == "FAIL" and "reply" in " ".join(r["notes"])
    assert not (ev / "step-8").exists() and not (ev / "step-9").exists()
    state = json.loads((ev / "step-7" / "delegation.json").read_text())
    assert state["state"] == "delivered"


def test_a_session_that_never_comes_ready_fails_step_4(tmp_path, fake_bin):
    w = _l1_world.World(tmp_path, fake_bin, ready_polls=1000)
    code, out = go(w)
    assert code == EXIT_FAILED
    r = result_of(w.home / "evidence", 4)
    assert r["outcome"] == "FAIL"
    assert "did not show one ready session" in " ".join(r["notes"])
    assert not (w.home / "evidence" / "step-5").exists()


def test_a_writable_payload_settings_file_fails_unknown_5(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin, payload_errno=None)
    by, _ = checks_of(walk.evidence)
    assert by["Unknown 5"]["outcome"] == "FAIL"
    assert by["Unknown 4"]["outcome"] == "PASS"


def test_a_stale_refusing_setting_is_put_back_first(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin)
    w, ev = walk.world, walk.evidence
    state = w.state()
    state["sandboxes"]["beta"]["refuse_file"] = True
    state["sandboxes"]["beta"]["refuse_active"] = True
    w.state_path.write_text(json.dumps(state), encoding="utf-8")
    (ev / "step-8" / "checks.json").unlink()
    code, out = go(w, only_step=8)
    assert code == EXIT_CHECKS, out
    sb = w.state()["sandboxes"]["beta"]
    assert sb["refuse_file"] is False and sb["refuse_active"] is False
    assert sb["phase"] == "Ready"
    assert "was left refusing by an interrupted run: restored" in out
    by, _ = checks_of(ev)
    # The negative case ran again, against a request of its own.
    assert by["Unknown 4"]["outcome"] == "PASS"
    submits = [c for c in commands(ev) if c["label"] == "submit-negative"]
    assert len(submits) == 2


def test_only_the_probe_sandbox_is_ever_deleted(walked):
    calls = walked.world.calls()
    deletes = [c for c in calls if c["prog"] == "openshell"
               and c["argv"][2:4] == ["sandbox", "delete"]]
    assert len(deletes) == 1
    assert deletes[0]["argv"][4:] == [l1_run.PROBE_NAME]
    assert not any(c["prog"] == "docker" and c["argv"][:1] in (["rm"], ["kill"])
                   for c in calls)
    assert walked.world.state()["deleted"] == [l1_run.PROBE_NAME]
    for m in ("alpha", "beta"):
        for path in router_config.lane_paths(str(walked.world.home), m):
            assert os.path.isdir(path), path


def test_the_probe_policy_drops_only_the_peer_lane(walked):
    home = walked.world.home
    probe = (walked.evidence / "step-8" / "amap-l1-probe.yaml").read_text(
        encoding="utf-8").splitlines()
    sender = (home / "policies" / "alpha.yaml").read_text(
        encoding="utf-8").splitlines()
    dropped = f'- "{render.lane_target(l1_run.LANE_PEER)}"'
    assert [ln.strip() for ln in sender if ln not in probe] == [dropped]
    assert [ln for ln in probe if ln not in sender] == []
    assert len(sender) == len(probe) + 1
    create = [c for c in walked.world.calls()
              if is_create(c, l1_run.PROBE_NAME)]
    assert len(create) == 1
    argv = create[0]["argv"]
    assert argv[argv.index("--") + 1:] == ["sleep", "3600"]
    assert argv[argv.index("--policy") + 1] == str(
        walked.evidence / "step-8" / "amap-l1-probe.yaml")


# --- AC3 ----------------------------------------------------------------------

def test_every_runbook_criterion_and_unknown_maps_to_exactly_one_check():
    md = RUNBOOK.read_text(encoding="utf-8")
    found = [line[len("#### "):] for _, line in test_l1_runbook.headings(
        md, r"#### (Pass criterion|Unknown) \d+")]
    assert found == [c.heading for c in CHECKS]
    assert len(set(found)) == len(found)
    for c in CHECKS:
        assert (c.run is None) != (c.unknown_reason is None), c.heading
    template = l1_run.report_template()
    titled = re.findall(r"^### (.+)$", template, re.M)
    assert titled == [f"{c.heading}: {c.title}" for c in CHECKS]
    assert sorted(l1_run.EXECUTION_ORDER) == sorted(found)
    assert len(set(l1_run.EXECUTION_ORDER)) == len(l1_run.EXECUTION_ORDER)
    assert sorted(l1_run.CHECK_COMMANDS) == sorted(found)


def test_the_report_follows_the_template(walked):
    ev = walked.evidence
    draft = (ev / "POC-REPORT.md").read_text(encoding="utf-8")
    template = l1_run.report_template()
    heads = lambda text: [ln for ln in text.splitlines()  # noqa: E731
                          if ln.startswith("#")]
    assert heads(draft) == heads(template)
    for placeholder in re.findall(r"<[^<>]+>", template, re.S):
        assert placeholder not in draft, placeholder
    items = l1_run.not_checked_items(str(_workspace.SPEC_DIR))
    assert items
    rows = []
    for line in draft.splitlines():
        m = re.match(r"^\| (.*) \| \| \| \| UNKNOWN: for the operator to fill "
                     r"in from the evidence \|$", line)
        if m:
            rows.append(m.group(1).replace("\\|", "|"))
    assert rows == items
    assert not re.search(r"\b\d+ (passed|failed)\b", draft)
    assert str(walked.world.home) not in draft
    assert "/home/" not in draft
    assert "Draft written by amap-openshell.py l1-run" in draft


# --- AC4 ----------------------------------------------------------------------

def test_no_evidence_or_report_contains_the_key_or_the_token(walked):
    secrets = [_l1_world.FAKE_KEY, _l1_world.FAKE_KEY[-20:],
               _l1_world.FAKE_TOKEN]
    marked = False
    for d, _, files in os.walk(str(walked.evidence)):
        for f in files:
            data = (Path(d) / f).read_bytes()
            for s in secrets:
                assert s.encode() not in data, (f, s)
            marked = marked or l1_run.REDACTED.encode() in data
    assert marked, "no evidence file shows a redaction: the path never ran"
    for s in secrets:
        assert s not in walked.out


def test_the_redactor():
    key = _l1_world.FAKE_KEY
    r = l1_run.Redactor(l1_run.secret_values({"ANTHROPIC_API_KEY": key}))
    assert r.text(key) == l1_run.REDACTED
    assert r.text("value " + key[-20:] + " end") == "value [REDACTED] end"
    assert r.text("CLAUDE_CODE_MESSAGING_TOKEN=abc") == \
        "CLAUDE_CODE_MESSAGING_TOKEN=[REDACTED]"
    assert r.text('{"peerToken": "abc"}') == '{"peerToken": "[REDACTED]"}'
    assert r.text("ANTHROPIC_API_KEY=abc def") == \
        "ANTHROPIC_API_KEY=[REDACTED] def"
    assert r.text("ANTHROPIC_API_KEY is set") == "ANTHROPIC_API_KEY is set"
    assert r.doc({"a": [key, {"b": "CLAUDE_CODE_MESSAGING_TOKEN: xyz"}],
                  "n": 3}) == {
        "a": ["[REDACTED]", {"b": "CLAUDE_CODE_MESSAGING_TOKEN: [REDACTED]"}],
        "n": 3}
    short = l1_run.Redactor(l1_run.secret_values({"ANTHROPIC_API_KEY": "abc"}))
    assert short.secrets == ["abc"]


def test_the_key_reaches_only_the_create_commands(walked):
    for call in walked.world.calls():
        if call["key"]:
            assert is_create(call), call["argv"]
    assert any(c["key"] for c in walked.world.calls())


# --- AC5 ----------------------------------------------------------------------

def test_from_n_resumes(tmp_path, fake_bin):
    w = _l1_world.World(tmp_path, fake_bin, fail_router_once=True)
    code, out = go(w)
    assert code == EXIT_FAILED, out
    ev = w.home / "evidence"
    first = "20260929T120000Z"
    assert result_of(ev, 6, first)["outcome"] == "FAIL"
    w.set_flag(fail_router_once=False)
    code, out = go(w, from_step=6)
    assert code == EXIT_CHECKS, out
    calls = w.calls()
    builds = [c for c in calls if c["prog"] == "docker"
              and c["argv"][:1] == ["build"]
              and c["argv"][c["argv"].index("-t") + 1] == "amap-openshell-agent"]
    assert len(builds) == 1
    for name in ("alpha", "beta"):
        assert len([c for c in calls if is_create(c, name)]) == 1
    prepares = [c for c in commands(ev) if c["label"] == "prepare-apply"]
    assert len(prepares) == 1
    second = first + "-2"
    ran = sorted(n for n in range(10)
                 if (ev / f"step-{n}" / f"{second}-result.json").exists())
    assert ran == [0, 2, 6, 7, 8, 9]


def tree_outside_evidence(home):
    return [row for row in test_l1_kit.tree(home)
            if row[0] != "evidence" and not row[0].startswith("evidence/")]


def evidence_bytes(ev):
    return {str(p.relative_to(ev)): p.read_bytes()
            for p in sorted(ev.rglob("*")) if p.is_file()}


def test_a_rerun_after_success_changes_nothing(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin)
    w, ev = walk.world, walk.evidence
    assert walk.code == EXIT_CHECKS
    calls_before = len(w.calls())
    tree_before = tree_outside_evidence(w.home)
    files_before = evidence_bytes(ev)
    state_before = w.state()
    code, out = go(w)
    assert code == EXIT_CHECKS, out
    new = w.calls()[calls_before:]
    assert new, "a rerun still reads the gateway and the host"
    assert not [c for c in new if is_mutating(c)], new
    assert tree_outside_evidence(w.home) == tree_before
    assert w.state() == state_before
    after = evidence_bytes(ev)
    for rel, data in files_before.items():
        if rel == "runner.log":
            assert after[rel].startswith(data)
        else:
            assert after[rel] == data, rel
    added = [json.loads(after[rel]) for rel in after if rel not in files_before
             and rel.endswith(".json") and "-result" not in rel]
    for doc in added:
        if doc.get("kind") == "command":
            argv = doc["argv"]
            assert not ("l1-kit" in argv and "--apply" in argv), argv
    second = "20260929T120000Z-2"
    want = {0: "PASS", 2: "PASS", 1: "SKIPPED", 3: "SKIPPED", 4: "SKIPPED",
            5: "SKIPPED", 6: "SKIPPED", 7: "SKIPPED", 8: "SKIPPED",
            9: "SKIPPED"}
    for n, outcome in want.items():
        assert result_of(ev, n, second)["outcome"] == outcome, n


def test_selection_and_gates():
    sel, run = l1_run.selected_steps, l1_run.steps_to_run
    assert run(sel(None, None)) == tuple(range(10))
    assert run(sel(7, None)) == (0, 2, 7, 8, 9)
    assert run(sel(None, 1)) == (0, 1)
    assert run(sel(None, 9)) == (0, 9)
    assert run(sel(None, 8)) == (0, 2, 8)
    assert run(sel(None, 0)) == (0,)
    assert run(sel(3, None)) == (0, 2, 3, 4, 5, 6, 7, 8, 9)


# --- the session probe and the plant ------------------------------------------

def run_probe(fake, tmp_path):
    lister = _sessions.rewritten_lister(tmp_path / "openshell-sessions",
                                        fake.proc)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-c", l1_run.SESSION_PROBE, str(lister),
         str(fake.home)], env=env, capture_output=True, text=True, timeout=30)


def test_the_session_probe_finds_one_claude_row(tmp_path):
    pid = _sessions.BASE + 20
    with _sessions.short_tmp() as sock_dir:
        fake = _sessions.FakeSandbox(tmp_path, sock_dir, _sessions.BASE + 12)
        try:
            sock, key = fake.add_claude(pid)
            r = run_probe(fake, tmp_path)
            assert r.returncode == 0, r.stderr
            rows = _sessions.parse_rows(r.stdout)
            assert rows == [["claude", "-", "-", str(pid), sock, key]]
            fake.add_claude(pid + 1)
            r = run_probe(fake, tmp_path)
            assert r.returncode == 0, r.stderr
            assert len(_sessions.parse_rows(r.stdout)) == 2
        finally:
            fake.close()
    with _sessions.short_tmp() as sock_dir:
        fake = _sessions.FakeSandbox(tmp_path / "none", sock_dir,
                                     _sessions.BASE + 12)
        try:
            r = run_probe(fake, tmp_path / "none")
            assert (r.returncode, r.stdout) == (0, "")
        finally:
            fake.close()
    with _sessions.short_tmp() as sock_dir:
        fake = _sessions.FakeSandbox(tmp_path / "bare", sock_dir,
                                     _sessions.BASE + 12)
        try:
            import shutil
            for pid_ in (_sessions.MAIN_PID, _sessions.SUBSHELL_PID):
                shutil.rmtree(fake.proc / str(pid_))
            r = run_probe(fake, tmp_path / "bare")
            assert r.returncode == 3 and r.stderr.strip()
        finally:
            fake.close()


def test_the_plant_uses_the_agents_drop_box(world):
    ctx = make_ctx(world)
    mcp = json.loads((REPO / "payload" / "mcp-servers.json").read_text(
        encoding="utf-8"))["mcpServers"]["inbox-submit"]
    mounts = render.mount_table(ctx.host, ctx.sender)
    pairs = dict(p.split("=", 1) for p in render.env_pairs(
        mounts, ctx.addresses[ctx.sender]))
    want = mcp["env"]["OUTBOX_DIR"].replace("${AMAP_OUTBOX_DIR}",
                                            pairs["AMAP_OUTBOX_DIR"])
    argv = l1_run.submit_argv(ctx, ctx.sender, ctx.receiver, "s", "b")
    envs = [argv[i + 1] for i, w in enumerate(argv) if w == "--env"]
    assert envs == [f"OUTBOX_DIR={want}"]
    assert l1_run.INBOX_SUBMIT == mcp["command"]
    assert l1_run.LISTER == render.PAYLOAD_TARGET + "/" + \
        l1_kit.SESSION_LISTER_NAME
    assert (REPO / "payload" / l1_kit.SESSION_LISTER_NAME).is_file()
    assert argv[argv.index("--") + 1:][:2] == ["python3", mcp["command"]]
    assert ctx.addresses[ctx.receiver] in argv


def test_the_build_matches_the_dockerfile_and_the_runbook(world):
    ctx = make_ctx(world)
    argv = l1_run.build_argv(ctx)
    keys = {argv[i + 1].split("=", 1)[0] for i, w in enumerate(argv)
            if w == "--build-arg"}
    dockerfile = (REPO / "image" / "Dockerfile").read_text(encoding="utf-8")
    assert keys == set(re.findall(r"^ARG (\w+)", dockerfile, re.M))
    build = None
    for _, text in test_l1_runbook.fenced_blocks(RUNBOOK.read_text(
            encoding="utf-8")):
        for line in test_l1_runbook.command_lines(text):
            if line.startswith("docker build"):
                build = shlex.split(line)
    assert build
    assert argv[argv.index("-t") + 1] == build[build.index("-t") + 1] == \
        l1_run.IMAGE
    prepare = l1_run.kit_argv(ctx, "prepare", "--home", ctx.home, "--fleet",
                              str(l1_run.FLEET_FILE), "--run-as",
                              ctx.run_as_text, "--image", l1_run.IMAGE,
                              apply=False)
    assert re.fullmatch(r"\d+:\d+", prepare[prepare.index("--run-as") + 1])
    assert prepare[prepare.index("--image") + 1] == build[build.index("-t") + 1]


def test_pinned_router_facts_match_the_router():
    snippet = """
import json
from pathlib import Path
from types import SimpleNamespace
from router import audit, outbound, roster
print(json.dumps({
    "audit_dir": audit.AUDIT_DIR,
    "audit_file": audit.AUDIT_FILENAME,
    "placed": audit.EVENT_PEER_NOTICE_PLACED,
    "consumed": audit.EVENT_OUTCOME_CONSUMED,
    "roster": roster.ROSTER_FILENAME,
    "reason": outbound.REASON_RECIPIENT_NOT_ALLOWLISTED,
    "held": str(outbound._held_path(SimpleNamespace(state_dir=Path("/s")),
                                    "b", "7")),
}))
"""
    env = dict(os.environ, PYTHONPATH=str(_workspace.ROUTER_ROOT),
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", snippet], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    facts = json.loads(r.stdout)
    assert facts["audit_dir"] == l1_run.AUDIT_DIRNAME
    assert facts["audit_file"] == l1_run.AUDIT_FILENAME
    assert facts["placed"] == l1_run.EVENT_NOTICE_PLACED
    assert facts["consumed"] == l1_run.EVENT_OUTCOME
    assert facts["roster"] == l1_run.ROSTER_FILENAME
    assert facts["reason"] == l1_run.REASON_NOT_ALLOWLISTED
    ctx = l1_run.Ctx({}, False, FAST, io.StringIO(), time.monotonic,
                     time.sleep, fixed_utc, lambda: (1, 1))
    ctx.home = "/h"
    assert facts["held"] == l1_run.held_path(ctx, "b", "7").replace(
        "/h/router-state", "/s")
    run_sh = (_workspace.ROUTER_ROOT / "docker" / "run.sh").read_text(
        encoding="utf-8")
    assert f'CONTAINER="${{CONTAINER:-{l1_run.ROUTER_CONTAINER}}}"' in run_sh
    assert l1_run.ROUTER_CONTAINER == "amap-router-local"


# --- structure ----------------------------------------------------------------

def test_one_executor_and_no_lane_literals():
    tree_ = ast.parse((REPO / "l1_run.py").read_text(encoding="utf-8"))
    runs = []
    for cls in [n for n in ast.walk(tree_) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _dotted(node.func) == \
                        "subprocess.run":
                    runs.append((cls.name, fn.name))
    every = [n for n in ast.walk(tree_) if isinstance(n, ast.Call)
             and _dotted(n.func) == "subprocess.run"]
    assert len(every) == 1 and runs == [("Executor", "run")]
    for node in ast.walk(tree_):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                assert not re.match(r"(system|popen|exec|spawn|posix_spawn)",
                                    node.attr), node.attr
            assert node.attr != "Popen", "Popen"
    facts = _amap_main.router_facts()
    lanes = set(facts["lanes"])
    leaves = {leaf for v in facts["leaves"].values() for leaf in v}
    allowed = set()
    for node in ast.walk(tree_):
        if isinstance(node, ast.Call) and _dotted(node.func) == "_leaf" \
                and len(node.args) == 2 \
                and isinstance(node.args[1], ast.Constant):
            allowed.add(id(node.args[1]))
    for node in ast.walk(tree_):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in lanes, node.value
            if node.value in leaves:
                assert id(node) in allowed, node.value


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def test_the_runner_is_identifier_clean_and_states_no_totals():
    for path in (REPO / "l1_run.py", REPO / "tests" / "_l1_world.py"):
        assert test_l1_runbook.identifier_problems(path) == [], path.name
        assert not re.search(r"\b\d+ (passed|failed)\b",
                             path.read_text(encoding="utf-8")), path.name


def test_evidence_is_private_and_append_only(walked):
    ev = walked.evidence
    assert stat.S_IMODE(os.stat(ev).st_mode) == 0o700
    name = re.compile(r"^\d{8}T\d{6}Z(-\d+)?-\d{3}-[a-z0-9-]+\.json$")
    state = {"facts.json", "delegation.json", "checks.json", "probe.json",
             "amap-l1-probe.yaml"}
    for d, dirs, files in os.walk(str(ev)):
        for sub in dirs:
            assert stat.S_IMODE(os.stat(Path(d) / sub).st_mode) == 0o700, sub
        for f in files:
            assert stat.S_IMODE(os.stat(Path(d) / f).st_mode) == 0o600, f
            if f in state or f.endswith("-result.json") or \
                    f in ("runner.log", "POC-REPORT.md"):
                continue
            assert name.match(f), f
    commands_seen = [p for p, _ in evidence_docs(ev, 4)]
    assert commands_seen


# --- the parsers --------------------------------------------------------------

def test_the_posture_parsers(tmp_path):
    good = tmp_path / "gateway.toml"
    good.write_text(_l1_world.GATEWAY_TOML, encoding="utf-8")
    findings = l1_run.gateway_toml_findings(str(good))
    assert findings and all(ok for ok, _ in findings), findings

    def bad(text):
        path = tmp_path / "bad.toml"
        path.write_text(text, encoding="utf-8")
        found = l1_run.gateway_toml_findings(str(path))
        assert any(not ok for ok, _ in found), text
        return [t for ok, t in found if not ok]

    assert "bind_address" in " ".join(bad(
        _l1_world.GATEWAY_TOML + '\n[openshell.gateway.x]\n'
        'bind_address = "0.0.0.0:17670"\n'))
    assert "OIDC" in " ".join(bad(
        _l1_world.GATEWAY_TOML + "\n[openshell.gateway.oidc]\n"))
    assert "enable_bind_mounts" in " ".join(bad(
        _l1_world.GATEWAY_TOML.replace("enable_bind_mounts = true\n", "")))
    absent = l1_run.gateway_toml_findings(str(tmp_path / "absent.toml"))
    assert not all(ok for ok, _ in absent)
    commented = _l1_world.GATEWAY_TOML + "# bind_address = 1\n# oidc is off\n"
    path = tmp_path / "commented.toml"
    path.write_text(commented, encoding="utf-8")
    assert all(ok for ok, _ in l1_run.gateway_toml_findings(str(path)))

    block = ('filesystem_policy:\n  read_only:\n    - "/usr"\n    - \'/etc\'\n'
             '    - /bin\n  read_write:\n    - "/tmp"\nother: 1\n')
    assert l1_run.yaml_block_list(block, "read_only") == ["/usr", "/etc",
                                                          "/bin"]
    assert l1_run.yaml_block_list(block, "read_write") == ["/tmp"]
    assert l1_run.yaml_block_list(block, "missing") is None
    assert l1_run.yaml_block_list("read_only: [/usr, /etc]\n",
                                  "read_only") is None
    assert l1_run.yaml_block_list("a:\n  read_only:\n  - /usr\nb: 1\n",
                                  "read_only") == ["/usr"]


# --- S5e: this deployment's provider profile ----------------------------------
#
# Facts, at OpenShell main@acbac9c (identical at v0.1.2 unless noted): the
# profile commands are crates/openshell-cli/src/main.rs:1108-1161 and
# commands/provider.rs:1590-1615, 1701-1830; an update needs a non-zero
# resource_version (crates/openshell-server/src/grpc/provider.rs:2894-2908);
# `--provider X --auto-providers` creates a provider from an imported profile
# (commands/provider.rs:413-447, 508-521).

def step3_labels(evidence):
    return [d["label"] for d in commands(evidence) if d.get("step") == 3]


def profile_calls(world, verb=None):
    return [c for c in world.calls() if is_profile_call(c, verb)]


def is_lint(call):
    return call["prog"] == "openshell" and call["argv"][2:5] == [
        "provider", "profile", "lint"]


def test_step_3_lints_imports_and_a_rerun_imports_nothing(tmp_path, fake_bin):
    """AC3."""
    walk = walk_fresh(tmp_path, fake_bin)
    w, ev = walk.world, walk.evidence
    assert walk.code == EXIT_CHECKS
    calls = w.calls()
    assert len(profile_calls(w, "import")) == 1
    assert not profile_calls(w, "update")
    lints = [i for i, c in enumerate(calls) if is_lint(c)]
    imports = [i for i, c in enumerate(calls)
               if is_profile_call(c, "import")]
    assert lints and lints[0] < imports[0]
    labels = step3_labels(ev)
    for label in ("image-paths", "profile-dry", "profile-apply",
                  "profile-lint", "profile-list", "profile-import",
                  "profile-list-after"):
        assert label in labels, label
    recorded = [d["argv"] for d in commands(ev)]
    for c in calls:
        if "provider" in c["argv"]:
            assert ["openshell", *c["argv"]] in recorded, c["argv"]
    doc = w.state()["profiles"][provider_profile.PROFILE_ID]
    assert doc["binaries"] == list(
        _l1_world.DEFAULT_FLAGS["image_paths"].values())

    before = len(calls)
    code, out = go(w)
    assert code == EXIT_CHECKS, out
    new = w.calls()[before:]
    # An imported profile is not linted again: OpenShell's lint rejects an
    # ID the gateway already has.
    assert not any(is_lint(c) for c in new)
    assert any(c["argv"][2:3] == ["provider"] and "list-profiles" in c["argv"]
               for c in new)
    assert not [c for c in new if is_profile_call(c, "import")
                or is_profile_call(c, "update")]
    assert result_of(ev, 3, "20260929T120000Z-2")["outcome"] == "SKIPPED"
    for root in (w.home / "evidence", w.home / "providers"):
        for p in root.rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                assert _l1_world.FAKE_KEY not in text, p
                assert _l1_world.FAKE_KEY[-20:] not in text, p


def test_the_rendered_binaries_are_the_images_real_paths(tmp_path, fake_bin):
    """AC2."""
    walk = walk_fresh(tmp_path, fake_bin, image_paths={
        "node": "/opt/n/bin/node", "claude": "/opt/n/bin/node"})
    w = walk.world
    rendered = json.loads((w.home / "providers"
                           / provider_profile.PROFILE_NAME).read_text(
                               encoding="utf-8"))
    assert rendered["binaries"] == ["/opt/n/bin/node"]
    assert w.state()["profiles"][provider_profile.PROFILE_ID][
        "binaries"] == ["/opt/n/bin/node"]


def test_distinct_image_paths_are_both_listed(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin, image_paths={
        "node": "/opt/n/bin/node", "claude": "/opt/c/claude"})
    rendered = json.loads((walk.world.home / "providers"
                           / provider_profile.PROFILE_NAME).read_text(
                               encoding="utf-8"))
    assert rendered["binaries"] == ["/opt/n/bin/node", "/opt/c/claude"]


def test_no_reported_path_fails_step_3_with_no_default(tmp_path, fake_bin):
    """AC2: nothing is rendered, imported or created."""
    walk = walk_fresh(tmp_path, fake_bin, image_paths=None)
    w, ev = walk.world, walk.evidence
    assert walk.code == EXIT_FAILED
    res = result_of(ev, 3)
    assert res["outcome"] == "FAIL"
    assert any("node" in n for n in res["notes"]), res["notes"]
    assert not profile_calls(w)
    assert not any(is_create(c) for c in w.calls())
    assert not (w.home / "providers" / provider_profile.PROFILE_NAME).exists()


def test_a_differing_gateway_copy_is_updated_with_its_resource_version(
        tmp_path, fake_bin):
    w = _l1_world.World(tmp_path, fake_bin)
    old = provider_profile.render_profile(provider_profile.load_template(),
                                          ["/old/node"])
    w.put_profile(old, 7)
    code, out = go(w)
    assert code == EXIT_CHECKS, out
    assert not profile_calls(w, "import")
    updates = profile_calls(w, "update")
    assert len(updates) == 1
    argv = updates[0]["argv"]
    path = Path(argv[argv.index("-f") + 1])
    assert (w.home / "evidence" / "step-3") in path.parents
    assert json.loads(path.read_text(encoding="utf-8"))["resource_version"] == 7
    assert w.state()["profiles"][provider_profile.PROFILE_ID]["binaries"] \
        == list(_l1_world.DEFAULT_FLAGS["image_paths"].values())
    before = len(w.calls())
    code, out = go(w)
    assert code == EXIT_CHECKS, out
    assert not [c for c in w.calls()[before:] if is_profile_call(c, "update")]


def test_a_failing_lint_stops_before_the_import(tmp_path, fake_bin):
    walk = walk_fresh(tmp_path, fake_bin, fail_lint=True)
    w = walk.world
    assert walk.code == EXIT_FAILED
    assert result_of(walk.evidence, 3)["outcome"] == "FAIL"
    assert not profile_calls(w, "import")
    assert not any(is_create(c) for c in w.calls())


def test_the_profile_commands_are_cited_at_both_revisions(world):
    """AC5: the runner's docstring names each subcommand and flag with its
    main.rs lines and v0.1.2."""
    ctx = make_ctx(world)
    doc = l1_run.__doc__.splitlines()
    for cmd in (l1_run.cmd_profile_lint(ctx), l1_run.cmd_profile_list(ctx),
                l1_run.cmd_profile_import(ctx),
                l1_run.cmd_profile_update(ctx, "x")):
        argv = cmd[1]
        words = argv[3:]                      # after `--workspace W`
        assert argv[:3] == ["openshell", "--workspace", ctx.workspace]
        sub = " ".join(w for w in words if not w.startswith("-"))
        sub = " ".join(sub.split()[:3] if words[1] == "profile"
                       else sub.split()[:2])
        flags = [w for w in words if w.startswith("-")]
        lines = [ln for ln in doc if sub in ln]
        assert lines, sub
        assert any("main.rs:" in ln and "v0.1.2" in ln
                   and all(f in ln for f in flags) for ln in lines), (sub, flags)


# --- S8d: the router stage fails fast, and l1-run finds the siblings ---------

SLOW_ROUTER = FAST._replace(router_timeout=600)
DOCKER_MUTATORS = ("rm", "rmi", "stop", "kill", "restart", "start", "update")


def run_steps(world, steps, settings=FAST, env=None):
    """`(code, out, clock)`: the runner on exactly `steps`, with its own clock."""
    clock = Clock()
    out = io.StringIO()
    r = l1_run.Runner(env if env is not None else world.env(), True,
                      tuple(steps), settings=settings, out=out, now=fixed_utc,
                      sleep=clock.advance, clock=clock.now)
    return r.run(), out.getvalue(), clock


def docker_calls(world):
    return [c["argv"] for c in world.calls() if c["prog"] == "docker"]


def is_router_run(argv):
    return argv[:1] == ["run"] and "amap-openshell-agent" not in argv


@pytest.mark.parametrize("state", ["restarting", "exited", "dead"])
def test_step_6_stops_at_once_when_the_router_crash_loops(world, state):
    code, out, _ = run_steps(world, (0, 1, 2, 3, 4, 5))
    assert code == EXIT_OK, out
    world.set_flag(router_crash=state, router_exit_code=126,
                   router_crash_log="entrypoint.sh: permission denied")
    before = len(docker_calls(world))
    code, out, clock = run_steps(world, (6,), SLOW_ROUTER)
    assert code == EXIT_FAILED
    assert "step 6: FAIL" in out
    assert clock.t <= SLOW_ROUTER.poll_interval < 600
    assert state in out and "exit code 126" in out
    assert "entrypoint.sh: permission denied" in out
    assert "router: banner" not in out
    mine = docker_calls(world)[before:]
    assert len([a for a in mine if is_router_run(a)]) == 1
    assert not [a for a in mine if a[0] in DOCKER_MUTATORS]
    after_run = mine[[i for i, a in enumerate(mine) if is_router_run(a)][0] + 1:]
    assert {a[0] for a in after_run} <= {"ps", "inspect", "logs"}
    assert world.state()["router"] is True


def test_step_6_reports_a_router_that_crashes_before_the_first_check(world):
    run_steps(world, (0, 1, 2, 3, 4, 5))
    world.set_flag(router_crash="restarting", router_crash_polls=0,
                   router_exit_code=3, router_crash_log="boom")
    code, out, _ = run_steps(world, (6,), SLOW_ROUTER)
    assert code == EXIT_FAILED
    assert "step 6: FAIL" in out and "restarting" in out
    assert "exit code 3" in out and "boom" in out


def test_step_6_with_a_healthy_router_still_waits_for_the_roster_and_passes(
        world):
    world.set_flag(roster_after_polls=3)
    code, out, _ = run_steps(world, (0, 1, 2, 3, 4, 5))
    assert code == EXIT_OK, out
    code, out, clock = run_steps(world, (6,))
    assert code == EXIT_OK, out
    assert "step 6: PASS" in out
    recs = [c for c in commands(world.home / "evidence")
            if c.get("label") == "docker-ps-wait" and c.get("step") == 6]
    assert len(recs) == 2
    assert clock.t > 0
    assert state_roster_exists(world)


def state_roster_exists(world):
    cfg = world.state()["router_config"]
    return os.path.isfile(os.path.join(os.path.dirname(cfg), "roster",
                                       "roster.json"))


def test_step_6_leaves_a_running_router_alone_while_it_waits(world):
    code, out = go(world)
    assert code in (EXIT_OK, EXIT_CHECKS), out
    cfg = world.state()["router_config"]
    roster = Path(cfg).parent / "roster" / "roster.json"
    roster.unlink()
    world.set_flag(roster_after_polls=2)
    before = len(docker_calls(world))
    code, out, _ = run_steps(world, (6,))
    assert code == EXIT_OK, out
    assert "step 6: SKIPPED" in out and "already running" in out
    mine = docker_calls(world)[before:]
    assert not [a for a in mine if is_router_run(a) or a[0] in DOCKER_MUTATORS]
    assert roster.is_file()


SIBLING_VARS = [
    ("AMAP_ROUTER_REPO", "amap-router-local", "router/reset.py"),
    ("AMAP_CONNECTOR_REPO", "amap-connector-claude", "bin/inbox-delivery"),
    ("AMAP_SPEC_DIR", "amap-spec", "fixtures/validate.py"),
]


def make_checkout(path, confirm):
    f = Path(path) / confirm
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("", encoding="utf-8")
    return Path(path)


def make_layout(tmp_path, skip=None, spec_name="amap-spec"):
    lay = tmp_path / "lay"
    this = lay / "this-repo"
    this.mkdir(parents=True)
    for _, name, confirm in SIBLING_VARS:
        if name == skip:
            continue
        make_checkout(lay / (spec_name if name == "amap-spec" else name),
                      confirm)
    return lay, this


def env_without_siblings(world, **extra):
    env = world.env()
    for var, _, _ in SIBLING_VARS:
        env.pop(var, None)
    env.update(extra)
    return env


def test_step_0_finds_the_siblings_beside_the_repository_without_their_variables(
        world, tmp_path, monkeypatch):
    lay, this = make_layout(tmp_path)
    monkeypatch.setattr(l1_run, "SIBLING_SEARCH_START", this)
    code, out, _ = run_steps(world, (0,), env=env_without_siblings(world))
    assert code == EXIT_OK, out
    assert "step 0: PASS" in out
    for name in ("amap-router-local", "amap-connector-claude", "amap-spec"):
        assert f"{name} found at {lay / name} (found beside an ancestor" in out
    (lay / "amap-spec").rename(lay / ".amap-spec")
    code, out, _ = run_steps(world, (0,), env=env_without_siblings(world))
    assert code == EXIT_OK, out
    assert f"amap-spec found at {lay / '.amap-spec'} (found beside" in out
    env = env_without_siblings(world, **{v: "" for v, _, _ in SIBLING_VARS})
    code, out, _ = run_steps(world, (0,), env=env)
    assert code == EXIT_OK, out
    assert "found beside an ancestor" in out


@pytest.mark.parametrize("var,name,confirm", SIBLING_VARS)
def test_step_0_a_sibling_variable_is_the_only_place_searched(
        world, tmp_path, monkeypatch, var, name, confirm):
    lay, this = make_layout(tmp_path)
    monkeypatch.setattr(l1_run, "SIBLING_SEARCH_START", this)
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, _ = run_steps(world, (0,),
                             env=env_without_siblings(world, **{var: str(empty)}))
    assert code == EXIT_FAILED
    assert f"${var}=" in out and "the only place searched" in out
    other = make_checkout(tmp_path / "other", confirm)
    code, out, _ = run_steps(world, (0,),
                             env=env_without_siblings(world, **{var: str(other)}))
    assert code == EXIT_OK, out
    assert f"{name} found at {other} (named by ${var})" in out


@pytest.mark.parametrize("var,name,confirm", SIBLING_VARS)
def test_step_0_a_missing_sibling_fails_naming_its_variable(
        world, tmp_path, monkeypatch, var, name, confirm):
    lay, this = make_layout(tmp_path, skip=name)
    monkeypatch.setattr(l1_run, "SIBLING_SEARCH_START", this)
    code, out, _ = run_steps(world, (0,), env=env_without_siblings(world))
    assert code == EXIT_FAILED
    assert f"{name} is not found" in out and f"${var}" in out


def test_find_spec_prefers_amap_spec_and_refuses_a_directory_without_validate_py(
        tmp_path):
    lay = tmp_path / "lay"
    this = lay / "repo"
    this.mkdir(parents=True)
    make_checkout(lay / "amap-spec", "fixtures/validate.py")
    make_checkout(lay / ".amap-spec", "fixtures/validate.py")
    assert l1_run.find_spec({}, this) == lay / "amap-spec"
    (lay / "amap-spec" / "fixtures" / "validate.py").unlink()
    assert l1_run.find_spec({}, this) == lay / ".amap-spec"
    (lay / ".amap-spec" / "fixtures" / "validate.py").unlink()
    with pytest.raises(l1_run.SpecNotFound):
        l1_run.find_spec({}, this)
    with pytest.raises(l1_run.SpecNotFound):
        l1_run.find_spec({"AMAP_SPEC_DIR": str(lay)}, this)


def test_l1_run_needs_no_sibling_variable():
    assert l1_run.REQUIRED_VARIABLES == ("AMAP_OPENSHELL_HOME",
                                         "CLAUDE_CODE_VERSION")


def test_addresses_come_from_the_installed_fleet_json(world):
    """Since D21 an installed fleet.json carries a per-host fleet_domain, so the
    template's addresses are not the fleet's. Live on the test host (2026-10-05), step 7
    sent to the template's beta@agents.example.org and the router answered
    recipient_unknown."""
    template = json.loads(l1_run.FLEET_FILE.read_text(encoding="utf-8"))
    home = Path(world.env()["AMAP_OPENSHELL_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    installed = dict(template, fleet_domain="openshell.testhost.internal")
    Path(l1_kit.fleet_json(str(home))).write_text(json.dumps(installed),
                                                 encoding="utf-8")
    ctx = make_ctx(world)
    assert set(ctx.addresses.values()) == {
        f"{m}@openshell.testhost.internal" for m in ctx.members}


def test_before_install_the_template_gives_the_addresses(world):
    template = json.loads(l1_run.FLEET_FILE.read_text(encoding="utf-8"))
    home = Path(world.env()["AMAP_OPENSHELL_HOME"])
    assert not Path(l1_kit.fleet_json(str(home))).exists()
    ctx = make_ctx(world)
    assert all(a.endswith("@" + template["fleet_domain"])
               for a in ctx.addresses.values())


def _plant(ctx, member, req_id, to, outcome=None):
    processed = l1_run.lane_host(ctx, member, l1_run.LANE_OUTBOX, l1_run.PROCESSED)
    os.makedirs(processed, exist_ok=True)
    with open(os.path.join(processed, f"req-{req_id}.json"), "w") as fh:
        json.dump({"req_id": req_id, "draft": {
            "to": [to], "subject": l1_run.DELEGATION_SUBJECT, "body_text": "b"}}, fh)
    if outcome:
        results = l1_run.lane_host(ctx, member, l1_run.LANE_OUTBOX, l1_run.RESULTS)
        os.makedirs(results, exist_ok=True)
        with open(os.path.join(results, f"{req_id}.json"), "w") as fh:
            json.dump({"req_id": req_id, "outcome": outcome}, fh)


def test_a_request_to_a_stale_address_or_rejected_is_not_resumed(world):
    """Live on the test host (2026-10-05): after the address fix, step 7 found the earlier
    request by its subject, sent to the old domain and rejected, and waited
    for a notice that could not come."""
    ctx = make_ctx(world)
    s, r = ctx.sender, ctx.receiver
    _plant(ctx, s, "00000001", f"{r}@agents.stale.example", "rejected")
    assert l1_run.find_planted(ctx, s, l1_run.DELEGATION_SUBJECT) is None
    _plant(ctx, s, "00000002", ctx.addresses[r], "rejected")
    assert l1_run.find_planted(ctx, s, l1_run.DELEGATION_SUBJECT) is None
    _plant(ctx, s, "00000003", ctx.addresses[r])
    assert l1_run.find_planted(ctx, s, l1_run.DELEGATION_SUBJECT) == "00000003"


def test_a_request_to_a_stale_address_is_not_resumed_even_unanswered(world):
    ctx = make_ctx(world)
    _plant(ctx, ctx.sender, "00000001", f"{ctx.receiver}@agents.stale.example")
    assert l1_run.find_planted(ctx, ctx.sender, l1_run.DELEGATION_SUBJECT) is None


def test_unknown_3_names_the_interceptor_when_it_refuses_the_probe(tmp_path, fake_bin):
    """With the mounts interceptor on, Unknown 3's probe cannot be created.
    The note says why. It stays UNKNOWN, never a pass."""
    walk = walk_fresh(tmp_path, fake_bin, interceptor_refuses_probe=True)
    by_heading, _ = checks_of(walk.evidence)
    u3 = by_heading["Unknown 3"]
    assert u3["outcome"] == l1_run.UNKNOWN
    assert "refused by the mounts interceptor" in json.dumps(u3)


def test_the_interceptor_prefix_is_the_rules():
    from interceptor import rule
    assert l1_run.INTERCEPTOR_PREFIX == rule.REASON_PREFIX
