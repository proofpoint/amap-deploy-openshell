"""The host registry client (DESIGN.md D27): the strict reader, the config and
its ownership checks, overlap by filesystem identity, the status table, the
register outcomes, and the shared name.json validator.

The registry is `_fake_registry`, a stand-in written from the interface's
documented behaviour with invented identifiers. Standard library only.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _fake_registry as fr
import host_registry as hr

REPO = Path(__file__).absolute().parents[1]


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()
    home = root / "home"
    home.mkdir()
    return fr.World(root, home)


def cfg_of(world):
    return hr.load_config(str(world.home))


# --- the strict reader -------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"a": 1, "a": 2}',
    '{"a": {"b": 1, "b": 1}}',
    '[{"x": 1, "x": 1}]',
])
def test_a_duplicate_member_anywhere_is_refused(text):
    with pytest.raises(hr.Refused, match="duplicate member"):
        hr.strict_loads(text)


@pytest.mark.parametrize("text", ["NaN", '{"a": Infinity}', '[-Infinity]'])
def test_non_finite_numbers_are_refused(text):
    with pytest.raises(hr.Refused, match="non-finite"):
        hr.strict_loads(text)


def test_the_reader_is_the_sandboxs_own():
    assert hr.strict_loads is hr.identity_check.strict_loads
    assert hr.IDENTITY_TARGET == "/opt/amap/identity"


# --- the config --------------------------------------------------------------

def test_a_sound_config_loads(world):
    cfg = cfg_of(world)
    assert cfg.module == fr.MODULE and cfg.interface == fr.INTERFACE


def test_the_config_is_closed(world):
    world.write_config(dict(world.config, extra="x"))
    with pytest.raises(hr.ConfigError, match="extra extra"):
        cfg_of(world)
    doc = dict(world.config)
    del doc["module"]
    world.write_config(doc)
    with pytest.raises(hr.ConfigError, match="missing module"):
        cfg_of(world)


def test_a_duplicate_member_in_the_config_is_refused(world):
    text = json.dumps(world.config)[:-1] + ', "module": "other"}'
    world.write_config(text=text)
    with pytest.raises(hr.ConfigError, match="duplicate member 'module'"):
        cfg_of(world)


@pytest.mark.parametrize("key,value", [
    ("python", "relative/python"), ("registry", "/a/../b"),
    ("interface", "has space"), ("name_schema", ""), ("module", 3)])
def test_paths_and_tokens_are_checked(world, key, value):
    world.write_config(dict(world.config, **{key: value}))
    with pytest.raises(hr.ConfigError):
        cfg_of(world)


def test_a_group_writable_config_is_refused(world):
    os.chmod(world.write_config(), 0o664)
    with pytest.raises(hr.ConfigError, match="group- or world-writable"):
        cfg_of(world)


def test_a_symlinked_config_is_refused(world):
    real = world.root / "elsewhere.json"
    real.write_text(json.dumps(world.config))
    (world.home / hr.CONFIG_NAME).unlink()
    os.symlink(real, world.home / hr.CONFIG_NAME)
    with pytest.raises(hr.ConfigError, match="symlink"):
        cfg_of(world)


def test_a_config_owned_by_another_uid_is_refused(world):
    with pytest.raises(hr.ConfigError, match="owned by uid"):
        hr.load_config(str(world.home), uid=os.getuid() + 1)


def test_a_symlinked_python_is_refused(world):
    link = world.root / "python-link"
    os.symlink(fr.real_python(), link)
    world.write_config(dict(world.config, python=str(link)))
    with pytest.raises(hr.ConfigError, match="symlink"):
        cfg_of(world)


def test_a_writable_checkout_is_refused(world):
    os.chmod(world.checkout, 0o777)
    with pytest.raises(hr.ConfigError, match="checkout"):
        cfg_of(world)


def test_a_checkout_with_a_writable_ancestor_is_refused(world):
    parent = world.root / "open"
    parent.mkdir()
    code = fr.write_checkout(parent / "code")
    os.chmod(parent, 0o777)
    world.write_config(dict(world.config, checkout=str(code)))
    with pytest.raises(hr.ConfigError, match="writable ancestor"):
        cfg_of(world)


def test_an_identity_root_with_a_writable_ancestor_is_refused(world):
    parent = world.root / "open"
    parent.mkdir()
    os.chmod(parent, 0o777)
    world.write_config(dict(world.config,
                            identity_root=str(parent / "identities")))
    with pytest.raises(hr.ConfigError, match="identity_root"):
        cfg_of(world)


def test_a_config_inside_a_bound_source_is_refused(world):
    with pytest.raises(hr.ConfigError, match="mounted into sandboxes"):
        hr.load_config(str(world.home), keep_out_of=(str(world.home),))
    with pytest.raises(hr.ConfigError, match="checkout"):
        hr.load_config(str(world.home), keep_out_of=(str(world.checkout),))


# --- ownership and layout ----------------------------------------------------

def test_a_sound_layout_has_no_problems(world, tmp_path):
    inst = tmp_path / "instances"
    inst.mkdir()
    cfg = cfg_of(world)
    assert hr.ownership_problems(cfg) == []
    assert hr.layout_problems(cfg, str(inst)) == []


def test_the_overlay_is_never_owner_checked(world):
    (world.registry / "overlay.json").write_text("{}")
    os.chmod(world.registry / "overlay.json", 0o444)
    assert hr.ownership_problems(cfg_of(world)) == []


def test_registry_files_owned_by_another_uid_are_refused(world):
    (world.registry / "journal.jsonl").write_text("")
    probs = hr.ownership_problems(cfg_of(world), uid=os.getuid() + 1)
    assert any("journal.jsonl" in p for p in probs)


def test_an_identity_root_with_the_wrong_mode_names_its_remedy(world):
    os.chmod(world.identity_root, 0o700)
    probs = hr.ownership_problems(cfg_of(world))
    assert any("chmod 0755" in p for p in probs)


@pytest.mark.parametrize("where", ["equal", "under", "above"])
def test_an_identity_root_overlapping_the_instances_dir_is_refused(world, where):
    cfg = cfg_of(world)
    root = Path(cfg.identity_root)
    other = {"equal": root, "under": root.parent,
             "above": root / "inner"}[where]
    other.mkdir(exist_ok=True)
    probs = hr.layout_problems(cfg, str(other))
    assert probs and "separate trees" in probs[0]


def test_overlap_is_by_inode_not_by_spelling(tmp_path):
    a = tmp_path.resolve() / "a"
    (a / "b").mkdir(parents=True)
    assert hr.relation(str(a / "b"), str(a)) == "under"
    assert hr.relation(str(a), str(a / "b")) == "above"
    assert hr.relation(str(a), str(a)) == "equal"
    (tmp_path / "d").mkdir()
    assert hr.relation(str(a), str(tmp_path.resolve() / "d")) == "disjoint"


def test_a_symlink_anywhere_in_a_compared_path_refuses(tmp_path):
    real = tmp_path.resolve() / "real"
    real.mkdir()
    os.symlink(real, tmp_path.resolve() / "alias")
    with pytest.raises(hr.SymlinkInPath):
        hr.relation(str(tmp_path.resolve() / "alias"), str(real))


def test_a_path_that_cannot_be_inspected_raises(tmp_path):
    with pytest.raises(OSError):
        hr.relation(str(tmp_path.resolve() / "missing"), str(tmp_path.resolve()))


# --- the status table --------------------------------------------------------

def test_an_absent_snapshot_is_an_empty_registry(world):
    assert hr.show(cfg_of(world), "default", "alpha").kind == hr.EMPTY


def test_not_found_is_unbound(world):
    cfg = cfg_of(world)
    hr.register(cfg, "default", "alpha")
    look = hr.show(cfg, "default", "beta")
    assert look.kind == hr.UNBOUND and look.code == "not_found"


@pytest.mark.parametrize("code,kind", [
    ("unreadable", hr.UNKNOWN), ("invalid", hr.UNKNOWN),
    ("config_absent", hr.BROKEN), ("instances_dir_absent", hr.BROKEN),
    ("adapter_not_configured", hr.BROKEN), ("interface", hr.BROKEN),
    ("bad_request", hr.DEFECT), ("something_new", hr.UNKNOWN)])
def test_every_refusal_code_is_mapped(world, code, kind):
    world.faults({"show": {"error": code}})
    assert hr.show(cfg_of(world), "default", "alpha").kind == kind


def test_unreadable_and_invalid_are_never_empty(world):
    for code in ("unreadable", "invalid"):
        world.faults({"list": {"error": code}})
        assert hr.list_bindings(cfg_of(world)).kind != hr.EMPTY


@pytest.mark.parametrize("raw", [
    "", "\n", '{"ok": true}\n{"ok": true}\n', '{"ok": true, "ok": false}\n',
    "[1]\n", "not json\n", '{"ok": "yes"}\n'])
def test_an_unusable_response_is_unknown(world, raw):
    world.faults({"show": {"raw": raw}})
    assert hr.show(cfg_of(world), "default", "alpha").kind == hr.UNKNOWN


def test_a_duplicate_member_in_a_response_is_never_read_last_wins(world):
    world.faults({"register": {"raw": '{"ok": false, "ok": true, '
                                      '"binding": {}}\n'}})
    assert hr.register(cfg_of(world), "default", "alpha").outcome == hr.UNKNOWN


def test_a_late_answer_is_unknown(world, monkeypatch):
    monkeypatch.setattr(hr, "SHOW_TIMEOUT_S", 0.5)
    world.faults({"show": {"sleep": 3}})
    look = hr.show(cfg_of(world), "default", "alpha")
    assert look.kind == hr.UNKNOWN and "did not answer" in look.detail


def test_an_unrunnable_registry_is_unknown(world):
    cfg = cfg_of(world)._replace(python=str(world.root / "no-such-python"))
    assert hr.show(cfg, "default", "alpha").kind == hr.UNKNOWN


# --- register outcomes -------------------------------------------------------

def test_a_registration_delivers_name_json(world):
    cfg = cfg_of(world)
    r = hr.register(cfg, "default", "alpha")
    assert r.outcome == hr.REGISTERED
    b = hr.show(cfg, "default", "alpha").binding
    assert b["name_json"] == "current"
    assert hr.identity_dir_problem(cfg, b) is None
    assert hr.name_json_problems(b["identity_dir"], hr.expected_of(cfg, b)) == []


def test_committed_true_is_undelivered_whatever_the_exit_status(world):
    world.faults({"register": {"error": "delivery_failed", "committed": True,
                               "exit": 1}})
    r = hr.register(cfg_of(world), "default", "alpha")
    assert r.outcome == hr.UNDELIVERED and r.binding["name"] == "alpha"


def test_committed_unknown_is_unknown(world):
    world.faults({"register": {"error": "internal", "committed": "unknown",
                               "exit": 3}})
    assert hr.register(cfg_of(world), "default", "alpha").outcome == hr.UNKNOWN


def test_a_refusal_without_committed_is_refused(world):
    world.faults({"register": {"error": "adapter_not_configured"}})
    r = hr.register(cfg_of(world), "default", "alpha")
    assert r.outcome == hr.REFUSED and r.code == "adapter_not_configured"


def test_the_version_says_whether_the_interface_is_spoken(world):
    cfg = cfg_of(world)
    assert hr.version(cfg) == (True, "")
    world.host_json({"domain": fr.DOMAIN,
                     "identity_root": str(world.identity_root),
                     "interfaces": ["another/1"]})
    assert hr.version(cfg)[0] is False


def test_the_command_is_absolute_isolated_and_has_no_shell(world):
    cfg = cfg_of(world)
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, b'{"ok": true, '
                                           b'"interfaces": []}\n', b"")
    hr.version(cfg, run=run)
    assert seen["argv"][:3] == [cfg.python, "-E", "-s"]
    assert seen["argv"][3:5] == ["-m", cfg.module]
    assert seen["cwd"] == cfg.checkout
    assert seen["env"] == hr.MINIMAL_ENV
    assert "shell" not in seen and seen["timeout"] > 0


# --- the identity directory --------------------------------------------------

def test_an_identity_dir_not_under_the_root_is_refused(world):
    cfg = cfg_of(world)
    hr.register(cfg, "default", "alpha")
    b = dict(hr.show(cfg, "default", "alpha").binding)
    b["identity_dir"] = str(world.root / "elsewhere")
    assert "is not" in hr.identity_dir_problem(cfg, b)


def test_an_identity_dir_with_the_wrong_mode_names_chmod(world):
    cfg = cfg_of(world)
    hr.register(cfg, "default", "alpha")
    b = hr.show(cfg, "default", "alpha").binding
    os.chmod(b["identity_dir"], 0o775)
    assert "chmod 0755" in hr.identity_dir_problem(cfg, b)


def test_a_symlinked_identity_dir_is_refused(world):
    cfg = cfg_of(world)
    hr.register(cfg, "default", "alpha")
    b = hr.show(cfg, "default", "alpha").binding
    real = world.root / "real-ident"
    os.rename(b["identity_dir"], real)
    os.symlink(real, b["identity_dir"])
    assert hr.identity_dir_problem(cfg, b) is not None


# --- no sibling, no sandy ----------------------------------------------------

def test_host_registry_imports_the_standard_library_only():
    tree = ast.parse((REPO / "host_registry.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    stdlib = set(getattr(sys, "stdlib_module_names", ())) or {
        "__future__", "importlib", "json", "os", "posixpath", "stat",
        "subprocess", "pathlib", "typing"}
    assert names <= stdlib | {"__future__"}, names


def test_registration_runs_with_no_sandy_on_path_and_ignores_its_overlay(
        world, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("AMAP_DEPLOY_OVERLAY", str(world.root / "theirs.json"))
    cfg = cfg_of(world)
    assert hr.register(cfg, "default", "alpha").outcome == hr.REGISTERED


def test_no_public_file_names_a_private_identifier():
    """The fake's invented identifiers are the only ones in the tree."""
    for path in ("host_registry.py", "payload/identity-check", "render.py",
                 "verbs.py", "verify.py", "interceptor/rule.py",
                 "payload/amap-main"):
        text = (REPO / path).read_text(encoding="utf-8")
        assert "example-registry" not in text, path
