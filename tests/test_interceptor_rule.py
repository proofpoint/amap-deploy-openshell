"""The CreateSandbox validate rule (docs/INTERCEPTOR.md section 4, tests 1-16).

Standard library only. The home is a tmp directory holding `examples/fleet.json`
and the real lane directories, so the symlink check reads a real tree. Lane
names come from `render` and the router, never from a literal.
"""

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

import policy
import render
import router_config
import router_link
from interceptor import rule
from interceptor.rule import Decision, check_create, evaluate_create

REPO = Path(__file__).absolute().parents[1]
MEMBERS = ("alpha", "beta")
OUTBOX = render.LANE_OUTBOX


def make_home(tmp) -> str:
    """A real, realpath'd home with a fleet, payload, roster and lanes."""
    home = Path(tmp).resolve() / "home"
    home.mkdir(parents=True)
    shutil.copy(REPO / "examples" / "fleet.json", home / "fleet.json")
    (home / render.PAYLOAD_DIRNAME).mkdir()
    (home / render.ROSTER_DIRNAME).mkdir()
    fleet = policy.load_fleet(str(home / "fleet.json"))
    for name in policy.named_instances(fleet):
        router_config.create_lanes(str(home), name)
    return str(home)


def rendered_operation(home, name, workspace=None) -> dict:
    ws = workspace or policy.workspace_of(
        policy.load_fleet(os.path.join(home, "fleet.json")))
    return {"workspaceScope": {"workspace": ws}, "name": name,
            "spec": {"template": {
                "image": "img",
                "driverConfig": render.driver_config(
                    render.member_rows(home, name))}}}


def _mount(source, read_only):
    return {"type": render.MOUNT_TYPE, "source": source,
            "target": "/opt/amap/other", "read_only": read_only}


def mounts_of(op) -> list:
    return op["spec"]["template"]["driverConfig"]["docker"]["mounts"]


def section5_operation(home) -> dict:
    """INTERCEPTOR.md section 5: alpha asks for the payload and beta's outbox,
    read-write."""
    op = rendered_operation(home, "alpha")
    mounts_of(op)[:] = [
        _mount(render.payload_dir(home), True),
        _mount(render.lane_dir(home, "beta", OUTBOX), False)]
    return op


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path)


def denied(home, op) -> Decision:
    d = evaluate_create(op, home)
    assert d.allowed is False and d.reasons, op
    return d


def test_the_rendered_create_is_allowed(home):
    for name in MEMBERS:
        assert evaluate_create(rendered_operation(home, name), home) == \
            Decision(True, ())


def test_another_members_outbox_is_refused(home):
    d = denied(home, section5_operation(home))
    assert any("instances/beta/" + OUTBOX in r for r in d.reasons)


def test_another_members_lane_read_only_is_refused(home):
    for lane in render.LANES:
        op = rendered_operation(home, "alpha")
        mounts_of(op).append(
            _mount(render.lane_dir(home, "beta", lane), True))
        denied(home, op)


def test_paths_outside_the_rendered_rows_are_refused(home):
    outbox = render.lane_dir(home, "alpha", OUTBOX)
    for src in ("/", home, render.instances_dir(home),
                os.path.join(render.instances_dir(home), "alpha"),
                outbox + "/sub", "/var/run/docker.sock"):
        op = rendered_operation(home, "alpha")
        mounts_of(op).append(_mount(src, True))
        denied(home, op)


def test_unnormalized_sources_are_refused(home):
    good = render.lane_dir(home, "alpha", OUTBOX)
    inst = render.instances_dir(home)
    for src in (inst + "/alpha/../alpha/" + OUTBOX,
                inst + "/alpha/./" + OUTBOX,
                inst + "//alpha/" + OUTBOX,
                good + "/", "instances/alpha/" + OUTBOX, good + ":x",
                good + " x", good + "\x01"):
        op = rendered_operation(home, "alpha")
        mounts_of(op)[-1]["source"] = src
        d = denied(home, op)
        assert any("bind source" in r for r in d.reasons), src


def test_a_symlinked_source_is_refused(home):
    mine = render.lane_dir(home, "alpha", OUTBOX)
    shutil.rmtree(mine)
    os.symlink(render.lane_dir(home, "beta", OUTBOX), mine)
    d = denied(home, rendered_operation(home, "alpha"))
    assert any("symlink" in r for r in d.reasons)


def _rows(op):
    return {m["source"]: m for m in mounts_of(op)}


@pytest.mark.parametrize("which,value,allowed", [
    (render.PAYLOAD_DIRNAME, False, False),
    (router_link.router_config().LANE_INBOX, False, False),
    (OUTBOX, True, False),
    (render.PAYLOAD_DIRNAME, None, True),
    (OUTBOX, None, False),
    (render.PAYLOAD_DIRNAME, "true", False),
    (render.PAYLOAD_DIRNAME, 1, False),
    (render.PAYLOAD_DIRNAME, 1.0, False),
    (OUTBOX, 0, False),
    (OUTBOX, 0.0, False),
])
def test_read_only_must_match_the_rendering(home, which, value, allowed):
    op = rendered_operation(home, "alpha")
    rows = render.member_rows(home, "alpha")
    row = [r for r in rows if r.role == which][0]
    mount = _rows(op)[row.source]
    if value is None:
        del mount["read_only"]
    else:
        mount["read_only"] = value
    assert evaluate_create(op, home).allowed is allowed


def test_a_templated_create_is_refused(home):
    for key in ("workloadTemplate", "workload_template"):
        op = rendered_operation(home, "alpha")
        op[key] = "t"
        denied(home, op)
        bare = {"name": "alpha", key: "t",
                "workspaceScope": {"workspace": "default"}}
        denied(home, bare)


def test_identity_is_required_with_mounts(home):
    def mutate(f):
        op = rendered_operation(home, "alpha")
        f(op)
        return op
    cases = (
        lambda op: op.update(name=""),
        lambda op: op.update(name="mallory"),
        lambda op: op.update(workspaceScope={"workspace": "other"}),
        lambda op: op.update(workspaceScope={"allWorkspaces": {}}),
        lambda op: op.pop("workspaceScope"),
    )
    for f in cases:
        denied(home, mutate(f))


def test_a_non_member_is_refused_by_the_membership_gate(home):
    # mallory's own rows, so only the membership gate can refuse the name
    router_config.create_lanes(home, "mallory")
    d = denied(home, rendered_operation(home, "mallory"))
    assert any("member of the fleet" in r for r in d.reasons), d.reasons


def test_all_workspaces_is_refused_by_name(home):
    for key in ("allWorkspaces", "all_workspaces"):
        op = rendered_operation(home, "alpha")
        op["workspaceScope"] = {key: {}}
        d = denied(home, op)
        assert any("allWorkspaces" in r for r in d.reasons), d.reasons
        assert not any(r.startswith("internal error") for r in d.reasons)


def test_a_create_without_mounts_is_allowed(home):
    ws = {"workspaceScope": {"workspace": "default"}}
    for op in (
        {**ws, "name": "alpha", "spec": {"template": {"image": "i"}}},
        {**ws, "name": "alpha", "spec": {"template": {
            "driverConfig": {"docker": {"mounts": []}}}}},
        {"name": "", "spec": {"template": {"image": "i"}}},
    ):
        assert evaluate_create(op, home) == Decision(True, ())


def test_only_the_docker_block_is_accepted(home):
    for extra in ({"podman": {"mounts": []}}, {"vm": {}}, {"zzz": 1}):
        op = rendered_operation(home, "alpha")
        op["spec"]["template"]["driverConfig"].update(extra)
        denied(home, op)
    op = rendered_operation(home, "alpha")
    op["spec"]["template"]["driverConfig"] = {"docker": "x"}
    denied(home, op)


def test_unknown_keys_are_refused(home):
    def at(f):
        op = rendered_operation(home, "alpha")
        f(op)
        denied(home, op)
    at(lambda op: op.update(bogus=1))
    at(lambda op: op["spec"].update(bogus=1))
    at(lambda op: op["spec"]["template"].update(bogus=1))
    at(lambda op: op["spec"]["template"]["driverConfig"]["docker"]
       .update(bogus=1))
    at(lambda op: mounts_of(op)[0].update(bogus=1))
    at(lambda op: op["spec"]["template"]["driverConfig"]["docker"]
       .update(cdi_devices=[]))
    at(lambda op: mounts_of(op)[0].update(selinux_label="z"))
    for t in ("volume", "image", "tmpfs"):
        at(lambda op, t=t: mounts_of(op)[0].update(type=t))


def _snake_keys(value, camels):
    if isinstance(value, dict):
        return {(rule.snake(k) if k in camels else k):
                _snake_keys(v, camels) for k, v in value.items()}
    if isinstance(value, list):
        return [_snake_keys(v, camels) for v in value]
    return value


def test_both_spellings_are_read_alike(home):
    camels = {"workspaceScope", "driverConfig", "workloadTemplate"}
    for op in (rendered_operation(home, "alpha"), section5_operation(home)):
        assert evaluate_create(_snake_keys(copy.deepcopy(op), camels), home) \
            == evaluate_create(op, home)
    op = rendered_operation(home, "alpha")
    op["workspace_scope"] = op["workspaceScope"]
    denied(home, op)
    op = rendered_operation(home, "alpha")
    t = op["spec"]["template"]
    t["driver_config"] = t["driverConfig"]
    denied(home, op)


def test_a_non_object_mount_is_refused(home):
    fleet = policy.load_fleet(os.path.join(home, "fleet.json"))
    for bad in (render.payload_dir(home), None, 42, True,
                [render.MOUNT_TYPE, render.payload_dir(home), "/opt/amap/x"]):
        op = rendered_operation(home, "alpha")
        i = len(mounts_of(op))
        mounts_of(op).append(bad)
        want = Decision(False, (f"mounts[{i}] must be an object",))
        assert check_create(op, home, fleet) == want, bad
        assert evaluate_create(op, home) == want, bad


def test_every_reason_is_reported(home):
    op = rendered_operation(home, "alpha", workspace="other")
    op["bogus"] = 1
    mounts_of(op).append(_mount(render.lane_dir(home, "beta", OUTBOX), False))
    assert len(evaluate_create(op, home).reasons) == 3


def test_malformed_input_is_a_denial(home):
    fleet = policy.load_fleet(os.path.join(home, "fleet.json"))
    assert check_create("x", home, fleet).allowed is False
    assert evaluate_create(None, home).allowed is False
    op = rendered_operation(home, "alpha")
    op["spec"]["template"]["driverConfig"]["docker"]["mounts"] = {}
    assert evaluate_create(op, home).allowed is False
    good = rendered_operation(home, "alpha")
    path = os.path.join(home, "fleet.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    assert evaluate_create(good, home).allowed is False
    os.remove(path)
    d = evaluate_create(good, home)
    assert d.allowed is False and "fleet.json" in d.reasons[0]


def test_the_rule_uses_renders_rows(home):
    for n in MEMBERS:
        assert rule.allowed_binds(home, n) == {
            r.source: r.read_only for r in render.member_rows(home, n)}
