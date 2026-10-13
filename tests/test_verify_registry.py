"""`verify` with the host registry (DESIGN.md D27), against the fakes.

A fleet provisioned through the registry passes, and each D27 finding fires on
its own cause: the registration, the identity directory's extra entries, the
live identity mount (a member that predates its registration, a single-file
bind), the running daemon's identity, "registered, no sandbox" and "not
attributable to this workspace", and an interface the registry does not
speak, which turns the registry's checks UNKNOWN. Standard library only.
"""

import datetime
import json
from pathlib import Path

import pytest

import _fake_openshell
import _fake_registry as fr
import _fake_router
import _l1_world
import render
import router_config
from interceptor import wire
from test_verbs import Op
from test_verify import MEMBERS, World

REPO = Path(__file__).absolute().parents[1]

REGISTRY_CLAIMS = (
    "the host registry's config, owners, modes and layout are sound",
    "the host registry speaks the configured interface",
    "every registered identity of this workspace has a sandbox",
    "every openshell identity without a sandbox here is attributable",
    "its host registry identity is registered",
    "its identity directory holds name.json and nothing else",
    "the identity directory is mounted read-only",
    "the running delivery daemon has the binding's identity")


@pytest.fixture
def rworld(tmp_path, safe_tmp, fake_bin, monkeypatch, capsys):
    fake = _fake_openshell.Fake(tmp_path, fake_bin)
    for k, v in fake.env().items():
        monkeypatch.setenv(k, v)
    op = Op(safe_tmp, fake, capsys)
    template = json.loads((REPO / "examples" / "fleet.json").read_text())
    template["identity"] = "registry"
    path = tmp_path / "fleet-template.json"
    path.write_text(json.dumps(template))
    op.home.mkdir()
    registry = fr.World(safe_tmp / "reghost", op.home.resolve())
    code, out, err = op.run("install", "--apply", host=("--fleet", str(path)))
    assert code == 0, (out, err)
    registry.host_json({"domain": op.fleet()["fleet_domain"],
                        "identity_root": str(registry.identity_root)})
    op.provision(*MEMBERS)
    code, out, err = op.run("install", "--apply", host=("--fleet", str(path)))
    assert code == 0, (out, err)
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
    w = World(op, fake, router, toml, capsys, tmp_path)
    w.registry = registry
    return w


def result(w, out, claim):
    return w.result_of(out, claim)


def test_a_registered_fleet_passes(rworld):
    code, out, err = rworld.verify()
    assert code == 0, out + err
    for claim in REGISTRY_CLAIMS:
        assert claim in out, claim
    assert "FAIL" not in out and "UNKNOWN" not in out


def test_the_rendered_mounts_include_the_identity_row(rworld):
    code, out, _ = rworld.verify()
    assert result(rworld, out, "alpha: the container mounts the rendered "
                               "table") == "PASS"


def test_a_stale_running_identity_fails(rworld):
    rworld.fake.set_flag(stale_env="alpha")
    code, out, _ = rworld.verify()
    assert code == 1
    assert result(rworld, out, "alpha: the running delivery daemon") == "FAIL"
    assert "name.json changed since create" in out


def test_an_extra_entry_in_the_identity_dir_is_its_own_finding(rworld):
    b = rworld.registry.state()["bindings"][0]
    (rworld.registry.identity_root / b["logical_id"] / "stray").write_text("")
    code, out, _ = rworld.verify()
    assert result(rworld, out, f"{b['name']}: its identity directory holds "
                               f"name.json and nothing else") == "FAIL"
    assert result(rworld, out, f"{b['name']}: its host registry identity is "
                               f"registered") == "PASS"


def _edit_box(w, name, fn):
    state = w.fake._state()
    fn(state["sandboxes"][name])
    w.fake._save(state)


def test_a_member_without_its_identity_mount_fails_with_re_provision(rworld):
    _edit_box(rworld, "alpha", lambda box: box.update(mounts=[
        m for m in box["mounts"] if m["target"] != render.IDENTITY_TARGET]))
    code, out, _ = rworld.verify()
    assert result(rworld, out, "alpha: the identity directory is mounted") \
        == "FAIL"
    assert "predates its registration" in out
    assert "deprovision alpha --apply" in out


def test_a_single_file_bind_fails(rworld):
    def single(box):
        for m in box["mounts"]:
            if m["target"] == render.IDENTITY_TARGET:
                m["source"] = m["source"] + "/name.json"
    _edit_box(rworld, "alpha", single)
    code, out, _ = rworld.verify()
    assert result(rworld, out, "alpha: the identity directory is mounted") \
        == "FAIL"
    assert "single-file bind" in out


def test_a_registered_identity_without_a_sandbox_is_reported(rworld):
    """confirmed in this workspace by show, so it is an orphan."""
    rworld.registry.bind("default", "gamma")
    code, out, _ = rworld.verify()
    assert result(rworld, out, "every registered identity of this workspace "
                               "has a sandbox") == "FAIL"
    assert result(rworld, out, "every openshell identity without a sandbox "
                               "here is attributable") == "PASS"


def test_an_identity_of_another_workspace_is_not_attributed_or_dropped(rworld):
    rworld.registry.bind("other-workspace", "delta")
    code, out, _ = rworld.verify()
    assert result(rworld, out, "every openshell identity without a sandbox "
                               "here is attributable") == "FAIL"
    assert result(rworld, out, "every registered identity of this workspace "
                               "has a sandbox") == "PASS"
    assert "delta" in out


def test_an_unspoken_interface_makes_the_registry_checks_unknown(rworld):
    rworld.registry.host_json({
        "domain": rworld.op.fleet()["fleet_domain"],
        "identity_root": str(rworld.registry.identity_root),
        "interfaces": ["another/1"]})
    code, out, _ = rworld.verify()
    assert code == 1
    assert result(rworld, out, "the host registry speaks the configured "
                               "interface") == "FAIL"
    assert result(rworld, out, "alpha: its host registry identity is "
                               "registered") == "UNKNOWN"


def test_a_lost_snapshot_is_unknown_not_empty(rworld):
    (rworld.registry.registry / "registry.json").unlink()
    code, out, _ = rworld.verify()
    assert result(rworld, out, "alpha: its host registry identity is "
                               "registered") == "UNKNOWN"


def test_a_registry_domain_mismatch_is_named(rworld):
    rworld.registry.host_json({
        "domain": "other.example.org",
        "identity_root": str(rworld.registry.identity_root)})
    code, out, _ = rworld.verify()
    assert "registry domain" in out


def test_a_same_name_sandbox_does_not_hide_another_keys_binding(rworld):
    """A binding of another workspace named like one of this workspace's
    sandboxes is still asked about, and its logical_id differs from the one
    show(workspace, name) answers, so it is not attributed here."""
    rworld.registry.bind("other-workspace", "alpha")
    code, out, _ = rworld.verify()
    assert result(rworld, out, "every openshell identity without a sandbox "
                               "here is attributable") == "FAIL"
    assert result(rworld, out, "every registered identity of this workspace "
                               "has a sandbox") == "PASS"
