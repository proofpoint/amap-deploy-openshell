"""`verify`, against a fake `openshell`, a fake `docker` and a fake router.

Nothing here runs OpenShell, Docker or the router. The fakes are written into
`fake_bin` (registered with the binary guard) and the router checkout is a
directory of the test's own (`_fake_router`): the real sibling is never run.

Facts relied on, at OpenShell main@acbac9c: `sandbox get --output json` has
`id`, `name`, `workspace` and `phase` (crates/openshell-cli/src/run.rs:
2789-2791); `sandbox exec` takes `--no-tty`, `--no-login-shell` and `--timeout`
(crates/openshell-cli/src/main.rs:1681-1727); a sandbox's container carries the
sandbox-id label (crates/openshell-core/src/driver_utils.rs:16-31). The errno
of a denied write is EROFS (docs/POC-REPORT.md, Unknown 2). OpenShell logs no
filesystem denial (FINDINGS.md), so no test looks for one.
"""

import argparse
import ast
import datetime
import errno
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _fake_openshell
import _fake_router
import _l1_world
import amap_openshell
import l1_kit
import membership
import openshell_cli
import render
import router_config
import verify
from interceptor import wire
from test_verbs import IMAGE, RUN_AS, Op, tree

REPO = Path(__file__).absolute().parents[1]
SECTION_IDS = ("install", "router-config", "gateway", "members",
               "router-container", "router-health")
MEMBERS = ("alpha", "beta")


class World:
    """A provisioned home, the fakes, and `verify` run against them."""

    def __init__(self, op, fake, router, gateway_toml, capsys, tmp):
        self.op, self.fake, self.router = op, fake, router
        self.gateway_toml, self.capsys, self.tmp = gateway_toml, capsys, tmp
        self.home = op.home

    def verify(self, *extra, docker=None, openshell=None, toml=None):
        self.capsys.readouterr()
        code = amap_openshell.main([
            "--home", str(self.home), "--openshell",
            openshell or self.fake.binary(), "--run-as", RUN_AS, "--image",
            IMAGE, "--gateway-toml", str(toml or self.gateway_toml), "verify",
            "--docker", docker or self.fake.docker(), *extra])
        c = self.capsys.readouterr()
        return code, c.out, c.err

    def lines(self, out, claim_part):
        return [ln for ln in out.splitlines() if claim_part in ln]

    def result_of(self, out, claim_part):
        """The one outcome the report gives the claim containing the text."""
        found = [ln.split()[0] for ln in out.splitlines()
                 if ln.startswith("  ") and claim_part in ln]
        assert len(found) == 1, (claim_part, found)
        return found[0]


@pytest.fixture
def world(tmp_path, fake_bin, monkeypatch, capsys):
    """A home installed and provisioned by the verbs, a fake openshell and
    docker, a fake router checkout, a gateway.toml with every setting, and the
    first-sight markers the router would have written."""
    fake = _fake_openshell.Fake(tmp_path, fake_bin)
    for k, v in fake.env().items():
        monkeypatch.setenv(k, v)
    op = Op(tmp_path, fake, capsys)
    op.install()
    op.provision(*MEMBERS)
    toml = tmp_path / "gateway.toml"
    toml.write_text(_l1_world.GATEWAY_TOML + "\n"
                    + wire.gateway_fragment(str(op.home)))
    router = _fake_router.FakeRouter(tmp_path, str(op.home))
    monkeypatch.setenv("AMAP_ROUTER_REPO", str(router.root()))
    fake.set_router_config(str(router_config.router_json_path(str(op.home))))
    for name in MEMBERS:
        marker = op.home / "router-state" / name / "first-seen.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "instance": name,
            "first_seen_ts": datetime.datetime.now(
                datetime.timezone.utc).isoformat()}))
    return World(op, fake, router, toml, capsys, tmp_path)


def claims_of(out):
    return [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]


# --- the healthy world -------------------------------------------------------

def test_a_healthy_fleet_passes(world):
    before = tree(world.home)
    code, out, err = world.verify()
    assert code == 0, out + err
    for section in SECTION_IDS:
        assert any(ln.startswith(section + ":") for ln in out.splitlines()), \
            section
    assert "FAIL" not in out and "UNKNOWN" not in out
    assert claims_of(out) and all(ln.startswith("PASS")
                                  for ln in claims_of(out))
    assert tree(world.home) == before


def test_verify_writes_nothing_on_the_host(world):
    before, calls = tree(world.home), len(world.fake.calls())
    world.verify()
    assert tree(world.home) == before
    new = world.fake.calls()[calls:]
    assert new
    # V3's probe is the one create verify makes, and it never deletes.
    creates = [c for c in new if "create" in c["argv"]]
    assert len(creates) == 1
    name = creates[0]["argv"][creates[0]["argv"].index("--name") + 1]
    assert name.startswith(wire.PROBE_PREFIX)
    assert not [c for c in new if "delete" in c["argv"]]
    assert set(world.fake.sandboxes()) == set(MEMBERS)


def test_verify_has_no_apply():
    with pytest.raises(SystemExit) as e:
        amap_openshell.build_parser().parse_args(["verify", "--apply"])
    assert e.value.code == 2


# Each numbered check of the plan, found in a healthy run by the words its
# claim has, so a removed check fails here rather than passing silently.
CHECKS = (
    ("install", "the payload on disk is byte for byte"),
    ("install", "the layout install writes is present"),
    ("install", "every member the fleet names is recorded"),
    ("install", "no sandbox in the workspace is outside the fleet"),
    ("router-config", "router.json is what this fleet renders"),
    ("router-config", "selected.json is what this fleet renders"),
    ("gateway", "the gateway sets"),
    ("gateway", "gateway.toml registers the mounts interceptor as rendered"),
    ("gateway", "the running gateway negotiated the mounts interceptor"),
    ("gateway", "a create that mounts another member's outbox is refused"),
    ("members", "alpha: the sandbox exists and its (workspace, name, id)"),
    ("members", "alpha: the effective policy lists every lane"),
    ("members", "alpha: the effective policy grants no egress"),
    ("members", "alpha: the container mounts the rendered table"),
    ("members", "alpha: no mount source is shared"),
    ("members", "alpha: a write into the inbox lane fails"),
    ("members", "alpha: the delivery daemon is running"),
)


def test_every_check_s7_names_is_present(world):
    assert tuple(s.id for s in verify.SECTIONS) == SECTION_IDS
    code, out, err = world.verify()
    assert code == 0, out + err
    section = None
    seen = []
    for ln in out.splitlines():
        if not ln.startswith(" "):
            section = ln.split(":")[0]
        else:
            seen.append((section, ln.strip()))
    for where, words in CHECKS:
        assert any(s == where and words in c for s, c in seen), (where, words)
    # one group per member, and a check per required gateway setting
    assert sum("the gateway sets" in c for _, c in seen) > 1
    for member in MEMBERS:
        assert sum(c.split(" ", 1)[1].startswith(member + ":")
                   for _, c in seen) == sum(
            c.split(" ", 1)[1].startswith("alpha:") for _, c in seen)


def test_nothing_stops_early(world):
    world.fake.set_flag(rw_inbox="alpha", drop_policy_path="beta",
                        fail_exec="alpha")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "alpha: the container mounts") == "FAIL"
    assert world.result_of(out, "beta: the effective policy lists") == "FAIL"
    assert world.result_of(out, "alpha: a write into the inbox") == "UNKNOWN"
    assert world.result_of(out, "the router's own view") == "PASS"


# --- install -----------------------------------------------------------------

def test_a_payload_file_edited_on_disk_fails(world):
    path = world.home / "payload" / "amap-main"
    path.write_bytes(path.read_bytes() + b"# edited\n")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the payload on disk") == "FAIL"
    (line,) = [ln for ln in out.splitlines() if ln.startswith("NOT the payload")]
    assert "amap-main" in line and "(do not edit a file under payload/" in line \
        and "do not: do not" not in line


def test_a_payload_file_removed_fails(world):
    (world.home / "payload" / "claude-seed").unlink()
    code, out, err = world.verify()
    assert code == 1
    (line,) = [ln for ln in out.splitlines() if ln.startswith("NOT the payload")]
    assert "claude-seed" in line and "missing" in line


def test_a_missing_connector_makes_the_payload_check_unknown(world, tmp_path,
                                                             monkeypatch):
    empty = tmp_path / "no-connector"
    empty.mkdir()
    monkeypatch.setenv("AMAP_CONNECTOR_REPO", str(empty))
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the payload on disk") == "UNKNOWN"
    assert "UNKNOWN whether the payload on disk" in out
    assert "NOT the payload" not in out


def test_a_member_the_fleet_names_but_nothing_recorded_fails(world):
    path = world.home / "membership.json"
    recorded = membership.load(str(path))
    membership.write(str(path), [m for m in recorded if m.name != "beta"])
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "every member the fleet names is recorded") \
        == "FAIL"
    assert world.result_of(out, "beta: the sandbox exists") == "FAIL"
    assert world.result_of(out, "alpha: the sandbox exists") == "PASS"


def test_a_missing_layout_directory_fails(world):
    import shutil
    shutil.rmtree(world.home / "commands")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the layout install writes") == "FAIL"


def test_an_absent_membership_file_is_unknown(world):
    (world.home / "membership.json").unlink()
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "every member the fleet names is recorded") \
        == "UNKNOWN"


# --- router-config -----------------------------------------------------------

def test_router_json_drift_fails(world):
    path = world.home / "router.json"
    doc = json.loads(path.read_text())
    doc["fleet_domain"] = "other.example.org"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    rendered = router_config.render_router_json(
        world.op.fleet(), world.op.members(), str(world.home))
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "router.json is what this fleet renders") \
        == "FAIL"
    for line in router_config.drift_on_disk(str(path), rendered):
        assert line in out


def test_an_absent_router_json_fails(world):
    (world.home / "router.json").unlink()
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "router.json is what this fleet renders") \
        == "FAIL"
    assert "is absent" in out


def test_selected_json_drift_fails(world):
    path = world.home / "selected.json"
    doc = json.loads(path.read_text())
    doc["selected"] = doc["selected"][:1]
    path.write_text(json.dumps(doc))
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "selected.json is what this fleet renders") \
        == "FAIL"


# --- gateway -----------------------------------------------------------------

def test_a_gateway_setting_absent_fails(world, tmp_path):
    toml = tmp_path / "partial.toml"
    toml.write_text(_l1_world.GATEWAY_TOML.replace(
        "enable_bind_mounts = true\n", ""))
    code, out, err = world.verify(toml=toml)
    assert code == 1
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT the gateway sets")]
    assert "enable_bind_mounts" in line


def test_an_unreadable_gateway_file_is_unknown(world, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    toml = tmp_path / "locked.toml"
    toml.write_text(_l1_world.GATEWAY_TOML)
    toml.chmod(0)
    try:
        code, out, err = world.verify(toml=toml)
    finally:
        toml.chmod(0o600)
    assert code == 1
    assert "UNKNOWN whether the gateway sets" in out
    assert "Permission denied" in out
    assert "NOT the gateway sets" not in out


def test_an_absent_gateway_file_is_absent_not_unknown(world, tmp_path):
    code, out, err = world.verify(toml=tmp_path / "nowhere.toml")
    assert code == 1
    assert "NOT the gateway sets" in out
    assert "UNKNOWN whether the gateway sets" not in out


# --- members: the five the acceptance criteria name --------------------------

@pytest.mark.parametrize("member", MEMBERS)
def test_a_writable_inbox_mount_fails(world, member):
    other = [m for m in MEMBERS if m != member][0]
    world.fake.set_flag(rw_inbox=member)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, f"{member}: the container mounts") == "FAIL"
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith(f"NOT {member}: the container mounts")]
    assert "/opt/amap/lanes/inbox" in line
    assert "UNKNOWN whether " + member + ": the container mounts" not in out
    assert world.result_of(out, f"{other}: the container mounts") == "PASS"


def test_a_lane_missing_from_the_policy_fails(world):
    world.fake.set_flag(drop_policy_path="alpha")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "alpha: the effective policy lists") == "FAIL"
    assert "NOT alpha: the effective policy lists" in out
    assert world.result_of(out, "beta: the effective policy lists") == "PASS"


def test_an_id_mismatch_fails(world):
    recorded = membership.find(world.op.members(), "alpha").id
    world.fake.forget("alpha")
    world.fake.put("alpha", "id-other-9")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "alpha: the sandbox exists") == "FAIL"
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT alpha: the sandbox exists")]
    assert recorded in line and "id-other-9" in line
    assert world.result_of(out, "beta: the sandbox exists") == "PASS"


def test_a_mount_source_shared_by_two_members_fails(world):
    world.fake.set_flag(share_source="beta")
    code, out, err = world.verify()
    assert code == 1
    for member in MEMBERS:
        assert world.result_of(
            out, f"{member}: no mount source is shared") == "FAIL"
        (line,) = [ln for ln in out.splitlines() if ln.startswith(
            f"NOT {member}: no mount source is shared")]
        assert "alpha" in line and "beta" in line


def test_a_successful_in_sandbox_write_fails(world):
    world.fake.set_flag(write_opens="alpha")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "alpha: a write into the inbox") == "FAIL"
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT alpha: a write into the inbox")]
    assert verify.OPENED in line
    assert world.result_of(out, "beta: a write into the inbox") == "PASS"
    (probe,) = [p for p in world.fake.probes()
                if p["sandbox"] == "alpha" and p["kind"] == "write-probe"]
    assert probe["args"][0].startswith("/opt/amap/lanes/inbox/"
                                       + verify.SCRATCH_PREFIX)
    assert probe["scratch"].startswith(verify.SCRATCH_PREFIX)
    assert probe["left_behind"] is False
    inbox = world.home / "instances" / "alpha" / "inbox"
    assert [p for p in inbox.iterdir()
            if p.name.startswith(verify.SCRATCH_PREFIX)] == []


# --- members: the rest -------------------------------------------------------

def test_a_stopped_sandbox_is_unknown_not_a_pass(world):
    world.fake.set_phase("alpha", "Stopped")
    code, out, err = world.verify()
    assert code == 1
    for part in ("a write into the inbox", "the delivery daemon",
                 "the container mounts"):
        assert world.result_of(out, f"alpha: {part}") == "UNKNOWN", part
    assert world.result_of(out, "alpha: the sandbox exists") == "PASS"


def test_the_daemon_not_running_fails(world):
    world.fake.set_flag(no_delivery="alpha")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "alpha: the delivery daemon") == "FAIL"
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT alpha: the delivery daemon")]
    assert "do not read this PASS as proof" in line
    assert world.result_of(out, "beta: the delivery daemon") == "PASS"


def test_an_extra_mount_in_the_container_fails(world):
    world.fake.set_flag(extra_mount="alpha")
    code, out, err = world.verify()
    assert code == 1
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT alpha: the container mounts")]
    assert "/srv/elsewhere" in line


def test_a_network_key_in_the_effective_policy_fails(world):
    world.fake.set_flag(policy_append={
        "alpha": "network:\n  mail:\n    endpoints: []\n"})
    code, out, err = world.verify()
    assert code == 1
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT alpha: the effective policy grants no "
                                "egress")]
    assert "key network" in line
    assert world.result_of(out, "beta: the effective policy grants no "
                                "egress") == "PASS"


def test_a_host_beyond_the_providers_in_the_policy_fails(world):
    world.fake.set_flag(policy_append={
        "beta": "  mail:\n    name: mail\n    endpoints:\n"
                "      - host: mail.example.org\n        port: 443\n"})
    code, out, err = world.verify()
    assert code == 1
    (line,) = [ln for ln in out.splitlines()
               if ln.startswith("NOT beta: the effective policy grants no "
                                "egress")]
    assert "host mail.example.org" in line


def test_a_member_recorded_but_absent_from_openshell_fails(world):
    world.fake.forget("beta")
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "beta: the sandbox exists") == "FAIL"
    # the rest are properties of a sandbox that is not there: unknown, not four
    # more failures
    assert world.result_of(out, "beta: the delivery daemon") == "UNKNOWN"
    assert world.result_of(out, "beta: a write into the inbox") == "UNKNOWN"


def test_the_probes_run_only_openshell_and_docker(world, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, *a, **k):
        seen.append(list(argv)[0])
        return real(argv, *a, **k)

    monkeypatch.setattr(openshell_cli.subprocess, "run", spy)
    import router_sections as rh
    rh_real = subprocess.run
    monkeypatch.setattr(rh.subprocess, "run",
                        lambda argv, *a, **k: (seen.append(list(argv)[0]),
                                               rh_real(argv, *a, **k))[1])
    code, out, err = world.verify()
    assert code == 0, out + err
    allowed = {world.fake.binary(), world.fake.docker(), sys.executable,
               str(world.router.root() / "docker" / "run.sh")}
    assert set(seen) <= allowed, set(seen) - allowed
    assert world.fake.binary() in seen and world.fake.docker() in seen


def test_verify_never_asks_openshell_for_logs(world):
    tree_ = ast.parse((REPO / "verify.py").read_text(encoding="utf-8"))
    consts = {n.value for n in ast.walk(tree_)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "logs" not in consts
    world.verify()
    assert not [c for c in world.fake.calls() if "logs" in c["argv"]]


def test_the_write_probe_is_scratch_and_cleans_up(tmp_path):
    source = verify.WRITE_PROBE
    assert source.startswith(verify.PROBE_MARK)
    code = compile(source, "<probe>", "exec")
    scratch = str(tmp_path / (verify.SCRATCH_PREFIX + "0123"))

    def run_probe(path):
        import contextlib
        import io
        out, argv = io.StringIO(), sys.argv
        sys.argv = ["-c", path]
        try:
            with contextlib.redirect_stdout(out):
                exec(code, {"__name__": "__main__"})
        finally:
            sys.argv = argv
        return out.getvalue().strip()

    assert run_probe(scratch) == verify.OPENED
    assert not os.path.exists(scratch)          # writable: cleaned up
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        if os.geteuid() != 0:
            word = run_probe(str(ro / (verify.SCRATCH_PREFIX + "0123")))
            assert word == errno.errorcode[errno.EACCES]
            assert list(ro.iterdir()) == []        # nothing left behind
    finally:
        ro.chmod(0o700)
    missing = str(tmp_path / "no-such-dir" / "x")
    assert run_probe(missing) == errno.errorcode[errno.ENOENT]


# --- the router's two sections, reused from sandy ----------------------------

def test_the_router_container_checks_pass_in_the_healthy_world(world):
    code, out, err = world.verify()
    in_section = False
    rows = []
    for ln in out.splitlines():
        if not ln.startswith(" "):
            in_section = ln.startswith("router-container:")
        elif in_section:
            rows.append(ln.split()[0])
    assert rows and set(rows) == {"PASS"}


def test_an_absent_router_container_is_one_failure_not_four(world):
    world.fake.set_flag(router_absent=True)
    code, out, err = world.verify()
    assert code == 1
    rows = []
    in_section = False
    for ln in out.splitlines():
        if not ln.startswith(" "):
            in_section = ln.startswith("router-container:")
        elif in_section:
            rows.append(ln.split()[0])
    assert rows.count("FAIL") == 1 and rows[0] == "FAIL"
    assert set(rows[1:]) == {"UNKNOWN"}
    assert world.result_of(out, "the router container is running") == "FAIL"


def test_a_stopped_router_container_fails_running(world):
    world.fake.set_flag(router_stopped=True)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the router container is running") == "FAIL"


def test_a_router_container_with_a_network_fails(world):
    world.fake.set_flag(router_posture=["bridge", "no"])
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "it runs with no network") == "FAIL"


def test_a_stale_status_document_fails(world):
    world.router.set(stale=True)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the last poll is recent") == "FAIL"


def test_no_interval_makes_freshness_unknown(world):
    world.router.set(no_interval=True)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the last poll is recent") == "UNKNOWN"


def test_a_missing_first_sight_marker_fails(world):
    (world.home / "router-state" / "beta" / "first-seen.json").unlink()
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "every configured instance has been "
                                "first-seen") == "FAIL"


def test_a_flagged_discovery_line_fails(world):
    world.router.set(discovery_flagged=True)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the router's discovery report flags "
                                "nothing") == "FAIL"


def test_a_missing_router_checkout_is_unknown_for_both_router_sections(
        world, tmp_path, monkeypatch):
    empty = tmp_path / "no-router"
    empty.mkdir()
    monkeypatch.setenv("AMAP_ROUTER_REPO", str(empty))
    code, out, err = world.verify()
    assert code == 1
    assert out.count("UNKNOWN whether the router container is running") == 1
    assert out.count("UNKNOWN whether the router reports itself healthy") == 1
    assert "NOT the router" not in out


# --- a missing or failing openshell or docker --------------------------------

def _affected(out, parts):
    for part in parts:
        assert world_result(out, part) == "UNKNOWN", part


def world_result(out, part):
    found = [ln.split()[0] for ln in out.splitlines()
             if ln.startswith("  ") and part in ln]
    assert found, part
    assert len(set(found)) == 1, (part, found)
    return found[0]


OPENSHELL_CLAIMS = ("no sandbox in the workspace is outside the fleet",
                    "alpha: the sandbox exists", "alpha: the effective policy",
                    "alpha: a write into the inbox",
                    "alpha: the delivery daemon", "beta: the sandbox exists")
DOCKER_CLAIMS = ("alpha: the container mounts", "beta: the container mounts",
                 "alpha: no mount source is shared",
                 "the router container is running")


def test_a_failing_openshell_is_unknown(world):
    world.fake.set_flag(fail_get=True, fail_list=True)
    code, out, err = world.verify()
    assert code == 1
    _affected(out, OPENSHELL_CLAIMS)
    assert "UNKNOWN whether no sandbox in the workspace" in out
    assert "gateway down" in out


def test_a_missing_openshell_is_unknown(world):
    code, out, err = world.verify(
        openshell=str(Path(world.fake.binary()).parent / "absent"))
    assert code == 1
    _affected(out, OPENSHELL_CLAIMS)
    assert "Traceback" not in out + err


def test_a_failing_docker_is_unknown(world):
    world.fake.set_flag(fail_docker_ps=True)
    code, out, err = world.verify()
    assert code == 1
    _affected(out, DOCKER_CLAIMS)
    assert "Docker daemon" in out


def test_a_missing_docker_is_unknown(world):
    code, out, err = world.verify(
        docker=str(Path(world.fake.docker()).parent / "absent"))
    assert code == 1
    _affected(out, DOCKER_CLAIMS)
    assert "Traceback" not in out + err


def test_an_unreadable_home_is_a_report_not_a_traceback(tmp_path, fake_bin,
                                                        monkeypatch, capsys):
    fake = _fake_openshell.Fake(tmp_path, fake_bin)
    for k, v in fake.env().items():
        monkeypatch.setenv(k, v)
    code = amap_openshell.main(["--home", str(tmp_path / "nothing"),
                                "--openshell", fake.binary(), "verify",
                                "--docker", fake.docker()])
    c = capsys.readouterr()
    assert code == 1
    assert "UNKNOWN" in c.out and "Traceback" not in c.out + c.err
    assert " PASS" not in "\n".join(ln for ln in c.out.splitlines()
                                    if ln.startswith("  ")
                                    and "members can be listed" in ln)


def test_the_fake_router_is_not_the_sibling(world):
    import _workspace
    real = Path(_workspace.ROUTER_ROOT).absolute()
    fake = Path(world.router.root()).absolute()
    assert fake != real and real not in fake.parents
    assert os.environ["AMAP_ROUTER_REPO"] == str(fake)


def test_verify_text_is_identifier_clean():
    domain = re.compile(r"\b[\w-]+\.(com|net|io|dev|co)\b")
    lanes = set(router_config.LANES)
    for name in ("verify.py", "router_health_link.py"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "/home/" not in text and "/Users/" not in text, name
        assert not domain.search(text), name
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in lanes, (name, node.value)


# --- the mounts interceptor: V1, V2, V3 --------------------------------------

V1 = "gateway.toml registers the mounts interceptor"
V2 = "the running gateway negotiated the mounts interceptor"
V3 = "a create that mounts another member's outbox is refused"


def test_interceptor_checks_pass_when_registered_negotiated_and_refusing(world):
    code, out, err = world.verify()
    for claim in (V1, V2, V3):
        assert world.result_of(out, claim) == "PASS", out


def _toml_of(world):
    return world.gateway_toml.read_text()


def test_v1_fails_on_each_disabling_line(world):
    healthy = _toml_of(world)
    fragment = wire.gateway_fragment(str(world.home))
    sub = re.sub
    cases = {
        "no entry": lambda t: _l1_world.GATEWAY_TOML,
        "allowlist": lambda t: sub(r'binding_policy = "exact"',
                                   'binding_policy = "allowlist"', t),
        "dynamic and disabled": lambda t: sub(
            r'phases = \["validate"\]', 'disabled = true', sub(
                r'binding_policy = "exact"', 'binding_policy = "dynamic"', t)),
        "disabled alone": lambda t: t.replace(
            'phases = ["validate"]', 'phases = ["validate"]\ndisabled = true'),
        "two blocks": lambda t: t + "\n" + fragment,
        "service fail_open": lambda t: sub(
            r'failure_policy = "fail_closed"',
            'failure_policy = "fail_open"', t),
        "opt-out removed (D24)": lambda t: t.replace(
            "allow_insecure_transport = true\n", ""),
        "binding fail_open": lambda t: t.replace(
            'phases = ["validate"]',
            'phases = ["validate"]\nfailure_policy = "fail_open"'),
        "other phase": lambda t: sub(r'phases = \["validate"\]',
                                     'phases = ["modify_operation"]', t),
        "second binding": lambda t: t + (
            '\n[[openshell.gateway.interceptors.bindings]]\n'
            'rpc = "openshell.v1.OpenShell/DeleteSandbox"\n'
            'phases = ["validate"]\n'),
    }
    assert fragment in healthy
    for label, f in cases.items():
        text = f(healthy)
        assert text != healthy, label
        world.gateway_toml.write_text(text)
        code, out, err = world.verify()
        assert world.result_of(out, V1) == "FAIL", (label, out)
    bad = world.tmp / "dir" / "gateway.toml"
    bad.mkdir(parents=True)
    code, out, err = world.verify(toml=bad)
    assert world.result_of(out, V1) == "UNKNOWN"


def test_v1_fails_when_gateway_toml_is_absent(world):
    code, out, _ = world.verify(toml=world.tmp / "nowhere.toml")
    assert code == 1
    assert world.result_of(out, V1) == "FAIL"


def test_v2_fails_when_not_negotiated(world):
    world.fake.set_flag(gateway_info="none")
    assert world.result_of(world.verify()[1], V2) == "FAIL"
    for mode in ("fail", "bad_json"):
        world.fake.set_flag(gateway_info=mode)
        assert world.result_of(world.verify()[1], V2) == "UNKNOWN", mode


def test_v3_verdicts(world):
    notes = {"gateway": wire.PROBE_BY_GATEWAY,
             "failed_closed": wire.PROBE_FAILED_CLOSED,
             "refuse_other": wire.PROBE_REFUSED_OTHER}
    for mode in ("refuse", "refuse_wrapped", "gateway", "failed_closed",
                 "refuse_other", "allow"):
        world.fake.set_flag(interceptor=mode)
        code, out, err = world.verify()
        want = "PASS" if mode in ("refuse", "refuse_wrapped") else "FAIL"
        assert world.result_of(out, V3) == want, (mode, out)
        if mode in notes:
            assert notes[mode] in out
        if mode == "allow":
            made = [n for n in world.fake.sandboxes()
                    if n.startswith(wire.PROBE_PREFIX)]
            assert len(made) == 1 and made[0] in out
        assert not [c for c in world.fake.calls() if "delete" in c["argv"]]
    code, out, err = world.verify(openshell=str(world.tmp / "no-such-openshell"))
    assert world.result_of(out, V3) == "UNKNOWN"


def test_the_probe_argv(world):
    before = len(world.fake.calls())
    world.verify()
    creates = [c for c in world.fake.calls()[before:] if "create" in c["argv"]]
    (call,) = creates
    argv = call["argv"]
    name = argv[argv.index("--name") + 1]
    assert re.match(r"^amap-verify-[0-9a-f]{6}$", name)
    cfg = json.loads(argv[argv.index("--driver-config-json") + 1])
    assert cfg["docker"][wire.PROBE_POISON_KEY] is True
    assert cfg["docker"]["mounts"] == [{
        "type": render.MOUNT_TYPE,
        "source": render.lane_dir(str(world.home), "alpha", render.LANE_OUTBOX),
        "target": wire.PROBE_TARGET, "read_only": False}]
    assert argv[argv.index("--from") + 1] == IMAGE
    for flag in ("--detach", "--no-tty", "--no-auto-providers"):
        assert flag in argv
    assert argv[-2:] == ["--", *wire.PROBE_COMMAND]
    assert call["key"] is False
    assert not [c for c in world.fake.calls() if "delete" in c["argv"]]


def test_the_fake_refusal_uses_the_rules_prefix():
    assert _fake_openshell.INTERCEPTOR_PREFIX == wire.REASON_PREFIX
    assert _fake_openshell.POISON_KEY == wire.PROBE_POISON_KEY
