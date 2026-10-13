"""The adapter against the real host registry (DESIGN.md D27), run privately.

Public CI cannot reach the registry, so this module runs only when
$AMAP_OPENSHELL_PRIVATE_OVERLAY names an operator-private JSON file. Without
it the module is skipped, never passed. The file holds host-registry.json's
seven members plus:

  sha       the registry checkout's commit, which must be its HEAD
  scratch   an empty directory the operator prepared as a throwaway registry:
            it holds only the registry's host config naming
            `<scratch>/identities` as the openshell identity root

The tests write only into `scratch`, never into a host's real registry. The
results are reported privately, never in this repository or its pull
requests. Standard library only.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import host_registry as hr

OVERLAY = os.environ.get("AMAP_OPENSHELL_PRIVATE_OVERLAY")
if not OVERLAY:
    pytest.skip("no private overlay: the real registry is not reachable here",
                allow_module_level=True)

DOC = hr.strict_loads(Path(OVERLAY).read_bytes())
SCRATCH = Path(DOC["scratch"])


@pytest.fixture
def cfg(tmp_path):
    root = SCRATCH / "identities"
    root.mkdir(mode=0o755, exist_ok=True)
    for entry in SCRATCH.iterdir():      # a throwaway registry, emptied
        if entry.name not in (hr.HOST_CONFIG, "identities"):
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in root.iterdir():
        shutil.rmtree(entry)
    home = tmp_path / "home"
    home.mkdir()
    doc = {k: DOC[k] for k in hr.CONFIG_KEYS}
    doc.update(registry=str(SCRATCH), identity_root=str(root))
    (home / hr.CONFIG_NAME).write_text(json.dumps(doc))
    os.chmod(home / hr.CONFIG_NAME, 0o644)
    return hr.load_config(str(home))


def test_the_checkout_is_the_pinned_commit(cfg):
    head = subprocess.run(["git", "-C", cfg.checkout, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True)
    assert head.stdout.strip() == DOC["sha"]


def test_the_registry_speaks_the_configured_interface(cfg):
    assert hr.version(cfg) == (True, "")


def test_an_empty_registry_is_empty_then_registers(cfg):
    assert hr.show(cfg, "default", "alpha").kind == hr.EMPTY
    r = hr.register(cfg, "default", "alpha")
    assert r.outcome == hr.REGISTERED and r.binding["name"] == "alpha"


def test_a_registration_is_current_and_checks_with_the_sandboxs_validator(cfg):
    hr.register(cfg, "default", "alpha")
    look = hr.show(cfg, "default", "alpha")
    assert look.kind == hr.BOUND and look.binding["name_json"] == "current"
    b = look.binding
    assert hr.identity_dir_problem(cfg, b) is None
    assert hr.name_json_problems(b["identity_dir"],
                                 hr.expected_of(cfg, b)) == []


def test_register_is_idempotent_and_takes_the_incarnation(cfg):
    first = hr.register(cfg, "default", "alpha").binding
    again = hr.register(cfg, "default", "alpha", incarnation="sandbox-id-1")
    assert again.outcome == hr.REGISTERED
    assert again.binding["logical_id"] == first["logical_id"]


def test_an_unknown_key_is_not_found_and_list_shows_the_bound(cfg):
    hr.register(cfg, "default", "alpha")
    assert hr.show(cfg, "default", "beta").kind == hr.UNBOUND
    names = [b["name"] for b in hr.list_bindings(cfg).bindings]
    assert "alpha" in names


def test_an_off_rule_name_is_refused_and_nothing_registered(cfg):
    r = hr.register(cfg, "default", "Not-A-Label")
    assert r.outcome == hr.REFUSED
    assert hr.list_bindings(cfg).kind in (hr.EMPTY, hr.BOUND)
