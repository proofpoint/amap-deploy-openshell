"""`provision`, `install`, `deprovision` and `teardown` with the host registry
(DESIGN.md D27), against a fake `openshell` and the fake registry.

What is pinned: the status table before and after the first register,
the fresh-host rule, the pre-check that keeps a name from being minted,
the four register outcomes, the gate before `sandbox create`,
the interim name, address and domain rule, the commit boundary, and
that the registry is never asked to retire. Standard library only.
"""

import json
import os
from pathlib import Path

import pytest

import _fake_registry as fr
import host_registry as hr
import render
from test_verbs import Op, fake  # noqa: F401  (fixtures)

REPO = Path(__file__).absolute().parents[1]
IC = hr.identity_check


@pytest.fixture
def op(safe_tmp, fake, capsys):
    """An Op whose home has no group- or world-writable ancestor."""
    return Op(safe_tmp, fake, capsys)


@pytest.fixture
def reg(tmp_path, op):
    """A host whose fleet uses the registry, installed, nothing provisioned."""
    template = json.loads((REPO / "examples" / "fleet.json").read_text())
    template["identity"] = "registry"
    path = tmp_path / "fleet-template.json"
    path.write_text(json.dumps(template))
    op.home.mkdir()
    world = fr.World(op.home.parent / "reghost", op.home.resolve(),
                     domain="placeholder.example.org")
    op.world = world
    op.template = str(path)
    code, out, err = op.run("install", "--apply", host=("--fleet", str(path)))
    assert code == 0, (out, err)
    # install derived the fleet domain; the registry's domain is set to it.
    world.host_json({"domain": op.fleet()["fleet_domain"],
                     "identity_root": str(world.identity_root)})
    return op


def ops(world):
    return [c["op"] for c in world.calls()]


def created_env(op):
    argv = op.creates()[-1]["argv"]
    return dict(argv[i + 1].split("=", 1) for i, w in enumerate(argv)
                if w == "--env")


def created_mounts(op):
    argv = op.creates()[-1]["argv"]
    doc = json.loads(argv[argv.index("--driver-config-json") + 1])
    return doc["docker"]["mounts"]


# --- a fresh host ------------------------------------------------------------

def test_a_fresh_host_registers_then_creates(reg):
    out = reg.ok("provision", "alpha", "--apply")
    assert "the host registry is empty: a fresh host" in out
    assert ops(reg.world)[:3] == ["version", "show", "register"]
    b = reg.world.state()["bindings"][0]
    env = created_env(reg)
    assert env[IC.ENV_DIR] == render.IDENTITY_TARGET
    assert env[IC.ENV_LOGICAL_ID] == b["logical_id"]
    assert env[IC.ENV_EPOCH] == "1"
    assert render.SELF_ENV not in env and render.PEER_SELF_ENV not in env
    ident = [m for m in created_mounts(reg)
             if m["target"] == render.IDENTITY_TARGET]
    assert ident == [{"type": "bind", "read_only": True, "target":
                      render.IDENTITY_TARGET, "source": str(
                          reg.world.identity_root / b["logical_id"])}]


def test_the_incarnation_is_registered_after_create(reg):
    reg.ok("provision", "alpha", "--apply")
    regs = [c for c in reg.world.calls() if c["op"] == "register"]
    assert "incarnation" not in regs[0]
    assert regs[-1]["incarnation"] == reg.members()[0].id


def test_a_dry_run_registers_nothing(reg):
    out = reg.ok("provision", "alpha")
    assert "would register alpha" in out
    assert "register" not in ops(reg.world) and not reg.creates()


def test_a_second_provision_reuses_the_binding(reg):
    reg.ok("provision", "alpha", "--apply")
    reg.ok("provision", "alpha", "--apply")
    assert len(reg.world.state()["bindings"]) == 1


# --- a lost registry never mints ---------------------------------------------

def test_an_absent_snapshot_with_a_used_identity_root_is_unknown(reg):
    (reg.world.identity_root / ("f" * 32)).mkdir()
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "UNKNOWN" in out + err
    assert "register" not in ops(reg.world) and not reg.creates()


def test_an_absent_snapshot_after_a_registration_is_unknown(reg):
    reg.ok("provision", "alpha", "--apply")
    (reg.world.registry / "registry.json").unlink()
    for d in reg.world.identity_root.iterdir():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    before = len([c for c in reg.world.calls() if c["op"] == "register"])
    code, out, err = reg.run("provision", "beta", "--apply")
    assert code == 1 and "lost registry" in out + err
    assert len([c for c in reg.world.calls()
                if c["op"] == "register"]) == before


# --- the pre-check -----------------------------------------------------------

def test_a_retired_key_is_refused_before_any_register(reg):
    reg.ok("provision", "alpha", "--apply")
    reg.ok("deprovision", "alpha", "--apply")
    reg.world.retire("alpha")
    before = len(reg.world.calls())
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "retired identity" in out + err
    after = reg.world.calls()[before:]
    assert [c["op"] for c in after] == ["version", "show", "list"]


def test_a_name_held_by_another_key_is_refused_before_any_register(reg):
    reg.world.bind("elsewhere", "alpha")
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "held by the host registry" in out + err
    assert "register" not in ops(reg.world)


def test_a_suffixed_name_is_refused_after_register(reg):
    """The residual: a name the pre-check cannot see (an earlier name of an
    overridden binding) is minted, and the interim name rule refuses the create."""
    s = {"bindings": [], "earlier": ["alpha"]}
    (reg.world.registry / "registry.json").write_text(json.dumps(s))
    (reg.home / "registry-members.json").write_text('{"names": ["x"]}')
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "names must equal sandbox names" in out + err
    assert not reg.creates()


# --- register outcomes -------------------------------------------------------

def test_undelivered_is_retried_once_then_proceeds(reg):
    reg.world.faults({"register": {"error": "delivery_failed",
                                   "committed": True, "once": True}})
    reg.ok("provision", "alpha", "--apply")
    assert reg.creates()


def test_undelivered_twice_creates_nothing(reg):
    reg.world.faults({"register": {"error": "delivery_failed",
                                   "committed": True}})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "not delivered" in out + err
    assert not reg.creates()
    assert [c["op"] for c in reg.world.calls()].count("register") == 2


def test_unknown_twice_creates_nothing(reg):
    reg.world.faults({"register": {"error": "internal",
                                   "committed": "unknown", "exit": 3}})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "UNKNOWN" in out + err
    assert not reg.creates()


@pytest.mark.parametrize("code_", ["unreadable", "invalid"])
def test_an_unreadable_registry_is_never_empty(reg, code_):
    reg.world.faults({"show": {"error": code_}})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "UNKNOWN" in out + err
    assert "register" not in ops(reg.world)


@pytest.mark.parametrize("code_", ["config_absent", "adapter_not_configured"])
def test_a_broken_registry_config_fails_closed(reg, code_):
    reg.world.faults({"show": {"error": code_}})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and code_ in out + err
    assert not reg.creates()


def test_a_registry_that_does_not_speak_the_interface_is_refused(reg):
    reg.world.host_json({"domain": reg.fleet()["fleet_domain"],
                         "identity_root": str(reg.world.identity_root),
                         "interfaces": ["another/1"]})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "does not list the interface" in out + err


# --- fail closed, no silent fallback -----------------------------------------

def test_removing_the_config_never_falls_back_to_legacy(reg):
    (reg.home / hr.CONFIG_NAME).unlink()
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and hr.CONFIG_NAME in out + err
    assert not reg.creates()


def test_a_registry_domain_that_is_not_the_fleet_domain_is_refused(reg):
    reg.world.host_json({"domain": "other.example.org",
                         "identity_root": str(reg.world.identity_root)})
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "registry domain" in out + err
    assert not reg.creates()


def test_an_identity_root_under_the_instances_dir_is_refused(reg):
    inner = reg.home / "instances" / "ids"
    inner.mkdir()
    os.chmod(inner, 0o755)
    reg.world.write_config(dict(reg.world.config, identity_root=str(inner)))
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "separate trees" in out + err


# --- the commit boundary and the operator verbs ------------------------------

def test_a_failed_create_keeps_the_registration_and_a_retry_reuses_it(reg):
    reg.fake.set_flag(fail_create=True)
    code, _out, _err = reg.run("provision", "alpha", "--apply")
    assert code == 1
    assert len(reg.world.state()["bindings"]) == 1
    reg.fake.set_flag(fail_create=False)
    reg.ok("provision", "alpha", "--apply")
    assert len(reg.world.state()["bindings"]) == 1


def test_deprovision_and_teardown_never_retire(reg):
    reg.ok("provision", "alpha", "--apply")
    out = reg.ok("deprovision", "alpha", "--apply")
    assert "never retires" in out
    out = reg.ok("teardown", "--apply")
    assert "Teardown does not retire it" in out
    assert "permanently unprovisionable" in out
    assert set(ops(reg.world)) <= {"version", "show", "list", "register"}
    assert reg.world.state()["bindings"][0]["state"] == "active"


def test_install_creates_the_identity_root(tmp_path, op):
    template = json.loads((REPO / "examples" / "fleet.json").read_text())
    template["identity"] = "registry"
    path = tmp_path / "t.json"
    path.write_text(json.dumps(template))
    op.home.mkdir()
    world = fr.World(op.home.parent / "r", op.home.resolve(),
                     make_identity_root=False)
    code, out, err = op.run("install", "--apply", host=("--fleet", str(path)))
    assert code == 0, (out, err)
    assert world.identity_root.is_dir()
    assert oct(world.identity_root.stat().st_mode & 0o777) == "0o755"


# --- redelivery, the attempt marker and the record ----------------------------

def test_an_undelivered_registration_is_redelivered_on_rerun(reg):
    """Two undelivered attempts leave a committed binding with no name.json.
    Only register redelivers, so a rerun must register again rather than
    refuse "not current" forever."""
    reg.world.faults({"register": {"error": "delivery_failed",
                                   "committed": True}})
    code, _out, _err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and not reg.creates()
    reg.world.faults({})
    out = reg.ok("provision", "alpha", "--apply")
    assert "name.json redelivered" in out
    assert reg.creates()
    assert len(reg.world.state()["bindings"]) == 1


def test_the_attempt_is_recorded_before_the_registry_is_asked(reg):
    reg.world.faults({"register": {"error": "internal",
                                   "committed": "unknown", "exit": 3}})
    code, _out, _err = reg.run("provision", "alpha", "--apply")
    assert code == 1
    record = json.loads((reg.home / "registry-members.json").read_text())
    assert record == {"names": ["alpha"]}


@pytest.mark.parametrize("what", ["symlink", "group-writable", "extra member",
                                  "duplicate name", "unsorted", "bad name"])
def test_an_untrusted_record_fails_closed(reg, what):
    path = reg.home / "registry-members.json"
    text = {"extra member": '{"names": [], "more": 1}',
            "duplicate name": '{"names": ["alpha", "alpha"]}',
            "unsorted": '{"names": ["beta", "alpha"]}',
            "bad name": '{"names": ["Not_A_Name"]}'}.get(what,
                                                         '{"names": []}')
    if what == "symlink":
        real = reg.home.parent / "elsewhere.json"
        real.write_text(text)
        os.symlink(real, path)
    else:
        path.write_text(text)
        os.chmod(path, 0o664 if what == "group-writable" else 0o644)
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "register" not in ops(reg.world)


def test_a_journal_without_a_snapshot_is_not_a_fresh_host(reg):
    (reg.world.registry / "journal.jsonl").write_text("")
    code, out, err = reg.run("provision", "alpha", "--apply")
    assert code == 1 and "journal but no snapshot" in out + err
    assert "register" not in ops(reg.world)
