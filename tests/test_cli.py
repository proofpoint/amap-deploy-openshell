"""The command-line skeleton: every verb is registered, the operator verbs are
implemented (verbs.py), and so is `verify` (verify.py)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import amap_openshell

REPO = Path(__file__).absolute().parents[1]

# PLAN.md phase 2's verbs. `gateway-config` is not among them.
PHASE_2_VERBS = {"install", "provision", "deprovision", "verify", "list",
                 "router-config", "teardown"}


def _subparser_choices():
    import argparse
    parser = amap_openshell.build_parser()
    (action,) = [a for a in parser._actions
                 if isinstance(a, argparse._SubParsersAction)]
    return set(action.choices)


def test_install_has_fleet_domain_base():
    parse = amap_openshell.build_parser().parse_args
    got = parse(["--home", "/h", "install", "--fleet-domain-base",
                 "agents.example.org"])
    assert got.fleet_domain_base == "agents.example.org"
    assert parse(["--home", "/h", "install"]).fleet_domain_base is None
    with pytest.raises(SystemExit):
        parse(["--home", "/h", "provision", "alpha", "--fleet-domain-base", "x"])


def test_every_verb_is_registered():
    """The phase-2 verbs are exactly the list in PLAN.md phase 2; `gateway-config`,
    `l1-kit`, `l1-run`, `siblings` and `bring-up` are the only other verbs."""
    assert _subparser_choices() == PHASE_2_VERBS | {
        "gateway-config", "l1-kit", "l1-run", "siblings", "bring-up"}
    assert amap_openshell.SIBLINGS == "siblings"
    assert set(amap_openshell.PHASE_2_VERBS) == PHASE_2_VERBS
    assert set(amap_openshell.VERBS) == (PHASE_2_VERBS - {"verify"}
                                         | {"gateway-config"})
    assert amap_openshell.VERIFY == "verify"


def test_siblings_has_apply_and_needs_no_home():
    parser = amap_openshell.build_parser()
    assert parser.parse_args(["siblings"]).apply is False
    assert parser.parse_args(["siblings", "--apply"]).apply is True
    assert "siblings" not in amap_openshell.VERBS


def test_host_wide_options_come_before_the_verb():
    parser = amap_openshell.build_parser()
    args = parser.parse_args(["--home", "/h", "install", "--apply"])
    assert (args.home, args.command, args.apply) == ("/h", "install", True)
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["install", "--home", "/h"])
    assert e.value.code == 2


def test_every_writing_verb_has_apply_and_the_read_only_verbs_do_not():
    parser = amap_openshell.build_parser()
    for verb in ("install", "provision alpha", "deprovision alpha",
                 "router-config", "teardown"):
        assert parser.parse_args([*verb.split(), "--apply"]).apply is True
        assert parser.parse_args(verb.split()).apply is False
    for verb in ("list", "verify", "gateway-config"):
        with pytest.raises(SystemExit) as e:
            parser.parse_args([verb, "--apply"])
        assert e.value.code == 2


def test_l1_kit_has_prepare_and_record():
    parser = amap_openshell.build_parser()
    base = ["l1-kit", "prepare", "--home", "/h", "--fleet", "f.json",
            "--run-as", "1:2", "--image", "img"]
    args = parser.parse_args(base)
    assert (args.command, args.action, args.apply) == ("l1-kit", "prepare",
                                                         False)
    assert args.restart_policy is False
    assert parser.parse_args([*base, "--apply", "--restart-policy"]).apply
    args = parser.parse_args(["l1-kit", "record", "--home", "/h", "alpha", "x"])
    assert (args.action, args.name, args.id, args.apply) == (
        "record", "alpha", "x", False)
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["l1-kit"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["l1-kit", "prepare", "--home", "/h"])
    assert e.value.code == 2
    assert "l1-kit" in parser.format_help()


def test_l1_kit_has_profile():
    parser = amap_openshell.build_parser()
    args = parser.parse_args(["l1-kit", "profile", "--home", "/h",
                              "--binary", "/a", "--binary", "/b"])
    assert (args.action, args.binaries, args.apply) == (
        "profile", ["/a", "/b"], False)
    with pytest.raises(SystemExit):
        parser.parse_args(["l1-kit", "profile", "--home", "/h"])


def test_l1_run_has_from_only_and_apply():
    parser = amap_openshell.build_parser()
    args = parser.parse_args(["l1-run"])
    assert (args.command, args.from_step, args.only_step, args.apply) == (
        "l1-run", None, None, False)
    args = parser.parse_args(["l1-run", "--from", "7", "--apply"])
    assert (args.from_step, args.only_step, args.apply) == (7, None, True)
    args = parser.parse_args(["l1-run", "--only", "8"])
    assert (args.from_step, args.only_step, args.apply) == (None, 8, False)
    for bad in (["--from", "1", "--only", "2"], ["--from", "10"],
                ["--only", "-1"], ["--from", "x"]):
        with pytest.raises(SystemExit) as e:
            parser.parse_args(["l1-run", *bad])
        assert e.value.code == 2, bad
    assert "l1-run" in parser.format_help()


def test_verify_is_implemented():
    assert amap_openshell.NOT_IMPLEMENTED == ()
    parser = amap_openshell.build_parser()
    assert parser.parse_args(["verify"]).command == "verify"


def test_verify_has_its_own_router_and_docker_options():
    parser = amap_openshell.build_parser()
    args = parser.parse_args(["verify", "--docker", "d", "--router-container",
                              "c", "--router-image", "i"])
    assert (args.docker, args.router_container, args.router_image) == (
        "d", "c", "i")
    bare = parser.parse_args(["verify"])
    assert (bare.docker, bare.router_container, bare.router_image) == (
        None, None, None)
    for flag in ("--docker", "--router-container", "--router-image"):
        with pytest.raises(SystemExit) as e:
            parser.parse_args([flag, "x", "verify"])
        assert e.value.code == 2, flag


def test_verify_needs_a_home(capsys):
    env = {k: v for k, v in os.environ.items() if k != "AMAP_OPENSHELL_HOME"}
    r = subprocess.run([sys.executable, str(REPO / "amap-openshell.py"),
                        "verify"], capture_output=True, text=True, timeout=60,
                       env=dict(env, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 2
    assert "--home" in r.stderr and "Traceback" not in r.stderr


def test_a_missing_home_is_a_message_not_a_traceback(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "AMAP_OPENSHELL_HOME"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, str(REPO / "amap-openshell.py"),
                        "install"], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "--home" in r.stderr


def test_the_launcher_runs_the_module(tmp_path):
    launcher = str(REPO / "amap-openshell.py")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    empty = tmp_path / "xdg"
    empty.mkdir()
    env["XDG_CONFIG_HOME"] = str(empty)
    r = subprocess.run([sys.executable, launcher, "gateway-config"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr and "ABSENT" in r.stdout
    h = subprocess.run([sys.executable, launcher, "--help"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert h.returncode == 0
    for verb in PHASE_2_VERBS:
        assert verb in h.stdout


@pytest.mark.parametrize("verb", ["l1-kit", "l1-run", "install", "verify"])
def test_a_missing_sandy_checkout_is_a_message_not_a_traceback(verb, tmp_path):
    """`policy` loads sandy's fleet_policy at import (D1), so a missing
    checkout surfaces when the CLI imports a verb's module. The operator gets
    the lookup's own message and exit 2, never a traceback."""
    empty = tmp_path / "not-sandy"
    empty.mkdir()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               AMAP_SANDY_REPO=str(empty))
    home = str(tmp_path / "home")
    argv = {"l1-run": [verb],
            "install": ["--home", home, verb],
            "verify": ["--home", home, verb],
            "l1-kit": [verb, "prepare", "--home", home,
                       "--fleet", str(REPO / "examples" / "fleet.json"),
                       "--run-as", "1000:1000", "--image", "img:test"]}[verb]
    r = subprocess.run([sys.executable, str(REPO / "amap-openshell.py"), *argv],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert "fleet_policy.py" in r.stderr and str(empty) in r.stderr
    assert verb in r.stderr


def test_only_the_sandy_lookup_is_turned_into_a_message():
    """Every other ImportError is a defect and must keep its traceback."""
    class SandyNotFound(ImportError):
        pass
    assert amap_openshell._missing_sandy(SandyNotFound("missing"))
    assert not amap_openshell._missing_sandy(ImportError("no module named x"))
    assert not amap_openshell._missing_sandy(ModuleNotFoundError("boom"))
