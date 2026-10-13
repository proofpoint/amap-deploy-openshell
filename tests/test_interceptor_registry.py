"""The CreateSandbox rule with the host registry (DESIGN.md D27).

The identity row must be present whenever a member's rows are, exact (source,
target, read-only, a real directory), with no other target overlapping it; no
source may touch the registry directory; and the identity variables must equal
the registry's binding in spec.environment and spec.template.environment
alike, with no self-address variable in either. The interceptor only
ever sends `show`. Standard library only.
"""

import ast
import copy
import json
import os
import shutil
from pathlib import Path

import pytest

import _fake_registry as fr
import host_registry as hr
import policy
import render
import router_config
from interceptor import rule
from interceptor.rule import check_create

REPO = Path(__file__).absolute().parents[1]
IC = hr.identity_check


class Host:
    def __init__(self, tmp):
        root = Path(tmp).resolve()
        self.home = root / "home"
        self.home.mkdir()
        fleet = json.loads((REPO / "examples" / "fleet.json").read_text())
        fleet["identity"] = "registry"
        (self.home / "fleet.json").write_text(json.dumps(fleet))
        (self.home / render.PAYLOAD_DIRNAME).mkdir()
        (self.home / render.ROSTER_DIRNAME).mkdir()
        self.fleet = policy.load_fleet(str(self.home / "fleet.json"))
        for name in policy.named_instances(self.fleet):
            router_config.create_lanes(str(self.home), name)
        self.world = fr.World(root / "reghost", self.home,
                              domain=self.fleet["fleet_domain"])
        self.cfg = hr.load_config(str(self.home))
        hr.register(self.cfg, "default", "alpha")
        self.binding = hr.show(self.cfg, "default", "alpha").binding
        self.expected = hr.expected_of(self.cfg, self.binding)

    def operation(self, name="alpha", env_at="spec"):
        rows = render.member_rows(str(self.home), name) + (
            render.identity_row(self.expected.identity_dir),)
        env = dict(p.split("=", 1) for p in render.identity_env(self.expected))
        spec = {"template": {"image": "img",
                             "driverConfig": render.driver_config(rows)}}
        if env_at == "spec":
            spec["environment"] = env
        else:
            spec["template"]["environment"] = env
        return {"workspaceScope": {"workspace": "default"}, "name": name,
                "spec": spec}

    def decide(self, op, **kw):
        return check_create(op, str(self.home), self.fleet, **kw)


def mounts_of(op):
    return op["spec"]["template"]["driverConfig"]["docker"]["mounts"]


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def refused(host, op, needle):
    d = host.decide(op)
    assert d.allowed is False, op
    assert any(needle in r for r in d.reasons), d.reasons
    return d


def test_the_rendered_create_is_allowed(host):
    for at in ("spec", "template"):
        d = host.decide(host.operation(env_at=at))
        assert d.allowed, d.reasons


def test_a_create_without_the_identity_row_is_refused(host):
    op = host.operation()
    mounts_of(op)[:] = [m for m in mounts_of(op)
                        if m["target"] != render.IDENTITY_TARGET]
    refused(host, op, "exactly once")


def test_the_identity_row_at_another_target_is_refused(host):
    op = host.operation()
    for m in mounts_of(op):
        if m["source"] == host.expected.identity_dir:
            m["target"] = "/opt/amap/elsewhere"
    refused(host, op, "must be bound at")


def test_a_writable_identity_row_is_refused(host):
    op = host.operation()
    for m in mounts_of(op):
        if m["source"] == host.expected.identity_dir:
            m["read_only"] = False
    refused(host, op, "read-only")


def test_name_json_alone_is_never_bound(host):
    op = host.operation()
    for m in mounts_of(op):
        if m["source"] == host.expected.identity_dir:
            m["source"] = host.expected.identity_dir + "/name.json"
    refused(host, op, "exactly once")


def test_a_writable_lane_at_the_identity_target_is_refused(host):
    op = host.operation()
    for m in mounts_of(op):
        if m["target"] == render.lane_target(render.LANE_OUTBOX):
            m["target"] = render.IDENTITY_TARGET + "/x"
    refused(host, op, "overlaps the identity target")


def test_the_identity_dir_at_a_lane_target_is_refused(host):
    op = host.operation()
    for m in mounts_of(op):
        if m["source"] == host.expected.identity_dir:
            m["target"] = render.lane_target(render.LANE_OUTBOX)
    refused(host, op, "must be bound at")


def test_the_registry_directory_is_never_mounted(host):
    op = host.operation()
    mounts_of(op).append({"type": "bind", "source": str(host.world.registry),
                          "target": "/opt/amap/reg", "read_only": True})
    refused(host, op, "registry's directory")


def test_a_symlinked_identity_dir_is_refused(host):
    real = host.world.root / "moved"
    os.rename(host.expected.identity_dir, real)
    os.symlink(real, host.expected.identity_dir)
    refused(host, host.operation(), "identity_dir")


@pytest.mark.parametrize("key", [k for k in IC.ENV_KEYS])
def test_each_identity_variable_is_required(host, key):
    op = host.operation()
    del op["spec"]["environment"][key]
    refused(host, op, key)


@pytest.mark.parametrize("key", [IC.ENV_NAME, IC.ENV_LOGICAL_ID, IC.ENV_EPOCH,
                                 IC.ENV_DIR])
def test_a_different_identity_value_is_refused(host, key):
    op = host.operation()
    op["spec"]["environment"][key] = "different"
    refused(host, op, key)


def test_conflicting_values_across_the_two_environments_are_refused(host):
    """a correct spec.environment does not cover a wrong
    spec.template.environment, however the gateway merges them."""
    op = host.operation()
    env = dict(op["spec"]["environment"])
    env[IC.ENV_LOGICAL_ID] = "b" * 32
    op["spec"]["template"]["environment"] = env
    refused(host, op, "spec.template.environment." + IC.ENV_LOGICAL_ID)


@pytest.mark.parametrize("key", rule.SELF_ADDRESS_VARIABLES)
@pytest.mark.parametrize("at", ["spec", "template"])
def test_a_self_address_variable_is_refused_in_either_environment(host, key, at):
    op = host.operation()
    target = op["spec"] if at == "spec" else op["spec"]["template"]
    target.setdefault("environment", {})[key] = "alpha@agents.example.org"
    refused(host, op, key)


def test_an_unregistered_member_is_refused(host):
    op = host.operation(name="beta")
    refused(host, op, "show for beta")


@pytest.mark.parametrize("code,kind", [("unreadable", hr.UNKNOWN),
                                       ("registry_absent", hr.EMPTY),
                                       ("config_absent", hr.BROKEN)])
def test_any_show_but_a_bound_answer_refuses(host, code, kind):
    host.world.faults({"show": {"error": code}})
    refused(host, host.operation(), kind)


def test_a_stale_name_json_refuses(host):
    p = Path(host.expected.identity_dir) / "name.json"
    doc = json.loads(p.read_text())
    doc["epoch"] = 9
    p.write_text(json.dumps(doc))
    refused(host, host.operation(), "name.json stale")


def test_a_missing_config_refuses_and_never_falls_back(host):
    (host.home / hr.CONFIG_NAME).unlink()
    refused(host, host.operation(), "cannot be used")


# --- show only ---------------------------------------------------------------

def test_the_interceptor_sends_only_show(host):
    before = len(host.world.calls())
    host.decide(host.operation())
    assert [c["op"] for c in host.world.calls()[before:]] == ["show"]


def test_no_registry_writer_is_reachable_from_the_interceptor():
    allowed = {"show", "load_config", "layout_problems", "relation",
               "identity_dir_problem", "expected_of", "ConfigError", "BOUND",
               "Expected", "identity_check"}
    for path in (REPO / "interceptor").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "host_registry"}
        assert used <= allowed, (path.name, used - allowed)


def test_the_legacy_fleet_never_asks_the_registry(host):
    fleet = dict(host.fleet)
    del fleet["identity"]
    asked = []
    op = host.operation()
    check_create(op, str(host.home), fleet,
                 show=lambda *a: asked.append(a) or None)
    assert asked == []
