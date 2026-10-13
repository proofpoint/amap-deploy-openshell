"""The adapter against the real host registry (DESIGN.md D27), run privately.

Public CI cannot reach the registry, so these tests carry the `real_registry`
marker. They are deselected (tests/conftest.py) unless
$AMAP_OPENSHELL_PRIVATE_OVERLAY names an operator-private JSON file holding
host-registry.json's seven members, plus:

  sha          the registry checkout's commit, which must be its HEAD
  scratch      a directory the operator made for these tests: absolute,
               normalised, not a symlink, the operator's, not group- or
               world-writable, holding the marker file SCRATCH_MARKER and no
               registry of its own
  host_config  the registry's host config as a JSON object, with the string
               IDENTITY_ROOT_TOKEN where the openshell identity root goes

Before any registry command, a session fixture checks the overlay, the
checkout's commit and the scratch root. A failure there ends the session.
Each test then works in a fresh child of the scratch root that the test
creates: nothing that was already there is read, cleared or written. The
results are reported privately, never in this repository or its pull
requests. Standard library only.
"""

import json
import os
import posixpath
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

import host_registry as hr

pytestmark = pytest.mark.real_registry

OVERLAY_VARIABLE = "AMAP_OPENSHELL_PRIVATE_OVERLAY"
SCRATCH_MARKER = ".amap-openshell-registry-scratch"
IDENTITY_ROOT_TOKEN = "@IDENTITY_ROOT@"
OVERLAY_KEYS = set(hr.CONFIG_KEYS) | {"sha", "scratch", "host_config"}


def _scratch_problem(scratch) -> str:
    if not isinstance(scratch, str) or not scratch.startswith("/") or \
            posixpath.normpath(scratch) != scratch:
        return "scratch is not an absolute, normalised path"
    try:
        hr.chain(scratch)
        st = os.lstat(scratch)
    except (OSError, ValueError) as e:
        return f"scratch cannot be used: {e}"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or \
            st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "scratch is not the operator's own, unshared directory"
    if not os.path.isfile(os.path.join(scratch, SCRATCH_MARKER)):
        return f"scratch has no {SCRATCH_MARKER}: it was not made for this"
    for owned in (*hr.REGISTRY_OWNED, hr.HOST_CONFIG):
        if os.path.lexists(os.path.join(scratch, owned)):
            return f"scratch holds a registry's {owned}: refused"
    return ""


class PrerequisiteError(Exception):
    """The private run cannot start. Nothing has been asked of a registry."""


def _check(doc) -> None:
    if not isinstance(doc, dict) or set(doc) != OVERLAY_KEYS:
        raise PrerequisiteError("the private overlay is not the documented "
                                "shape")
    hr.parse_config({k: doc[k] for k in hr.CONFIG_KEYS})
    for what, path, want_dir in (("python", doc["python"], False),
                                 ("checkout", doc["checkout"], True)):
        problem = hr.owner_problem(path, (os.getuid(), 0), what,
                                   want_dir=want_dir)
        if problem:
            raise PrerequisiteError(problem)
    head = subprocess.run(["git", "-C", doc["checkout"], "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=30)
    if head.returncode != 0 or head.stdout.strip() != doc["sha"]:
        raise PrerequisiteError(f"the registry checkout is not at the pinned "
                                f"commit {doc['sha']}")
    problem = _scratch_problem(doc["scratch"])
    if problem:
        raise PrerequisiteError(problem)
    if not isinstance(doc["host_config"], dict) or \
            IDENTITY_ROOT_TOKEN not in json.dumps(doc["host_config"]):
        raise PrerequisiteError("host_config has no identity-root placeholder")


def prerequisites(path) -> dict:
    """The private overlay, once every prerequisite holds: its shape, its
    config, the interpreter and checkout, the pinned commit, and the scratch
    root. ANY failure, whatever raised it, is a PrerequisiteError naming the
    cause. Nothing here runs a registry command or writes anything."""
    try:
        if not path:
            raise PrerequisiteError(f"${OVERLAY_VARIABLE} is not set")
        doc = hr.strict_loads(Path(path).read_bytes())
        _check(doc)
        return doc
    except PrerequisiteError:
        raise
    except Exception as e:  # noqa: BLE001 - every failure must stop the run
        raise PrerequisiteError(f"{type(e).__name__}: {e}") from None


@pytest.fixture(scope="session")
def overlay():
    """The session's prerequisites, before any registry command. A failure
    ends the session with exit status 2."""
    try:
        return prerequisites(os.environ.get(OVERLAY_VARIABLE))
    except PrerequisiteError as e:
        pytest.exit(f"the private run's prerequisites fail: {e}", 2)


@pytest.fixture
def cfg(overlay):
    """A fresh registry in a new child of the scratch root, removed after
    the test. Nothing already in the scratch root is touched."""
    child = Path(tempfile.mkdtemp(prefix="run-", dir=overlay["scratch"]))
    os.chmod(child, 0o755)
    try:
        registry, root = child / "registry", child / "identities"
        registry.mkdir(mode=0o755)
        root.mkdir(mode=0o755)
        text = json.dumps(overlay["host_config"]).replace(
            IDENTITY_ROOT_TOKEN, str(root))
        (registry / hr.HOST_CONFIG).write_text(text)
        home = child / "home"
        home.mkdir(mode=0o755)
        doc = {k: overlay[k] for k in hr.CONFIG_KEYS}
        doc.update(registry=str(registry), identity_root=str(root))
        (home / hr.CONFIG_NAME).write_text(json.dumps(doc))
        os.chmod(home / hr.CONFIG_NAME, 0o644)
        yield hr.load_config(str(home))
    finally:
        shutil.rmtree(child)


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
    assert names == ["alpha"]


def test_an_off_rule_name_is_refused_and_nothing_is_registered(cfg):
    r = hr.register(cfg, "default", "Not-A-Label")
    assert r.outcome == hr.REFUSED
    assert hr.list_bindings(cfg).kind == hr.EMPTY


def test_a_wrong_interface_is_broken_config(cfg):
    assert hr.show(cfg._replace(interface="another/1"), "default",
                   "alpha").kind == hr.BROKEN
