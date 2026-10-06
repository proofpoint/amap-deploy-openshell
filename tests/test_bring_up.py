"""The `bring-up` verb (D15): README "Bring-up" lines 1-8 as one command.

Nothing here runs OpenShell, Docker or the router. `openshell`, `docker` and the
gateway preflight are the `_l1_world` fakes. The router's `build.sh` and
`run.sh` are fake scripts substituted through `l1_run.router_script`.
`verify.main` is replaced. stdin is a `StringIO`. The only key is
`_l1_world.FAKE_KEY`. Nothing here states a total of passes or failures.
"""

import ast
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import _harness
import _l1_world
import amap_openshell
import bring_up
import l1_run
import router_config
import test_docs_agree
import test_l1_kit
import test_l1_run
import test_l1_runbook
import verify
from interceptor import wire

REPO = Path(__file__).absolute().parents[1]
README = REPO / "README.md"
TUTORIAL = REPO / "docs" / "TUTORIAL.md"
KEY = _l1_world.FAKE_KEY
KEY_VARIABLE = "ANTHROPIC_API_KEY"
REAL_INTERCEPTOR_PROBLEM = bring_up.interceptor_problem
# The order D15 fixes, written out so that a reordering of `bring_up.STAGES`
# fails here instead of being copied.
STAGE_NAMES = ["image", "gateway-config", "install", "profile", "provision",
               "router", "verify"]


def test_the_stages_are_in_the_order_d15_fixes():
    assert [name for name, _ in bring_up.STAGES] == STAGE_NAMES

UP = "AMAP is up"


class RouterScripts:
    """Fake `build.sh` and `run.sh`, with a log of how each was called."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "scripts.log"
        _harness.write_fake(self.dir, "build.sh", (
            '#!/bin/sh\n'
            f'echo "build.sh $* key=${{{KEY_VARIABLE}:+set}}" >> "{self.log}"\n'
            f'exec docker build -t amap-router-local "{self.dir}"\n'))
        _harness.write_fake(self.dir, "run.sh", (
            '#!/bin/sh\n'
            f'echo "run.sh $* key=${{{KEY_VARIABLE}:+set}}" >> "{self.log}"\n'
            '[ "$1" = "--config" ] || exit 64\n'
            'exec docker run --detach --name amap-router-local '
            '-e "ROUTER_CONFIG=$2" amap-router-local\n'))

    def lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()


class Host:
    def __init__(self, world, scripts, monkeypatch, capsys):
        self.world, self.scripts = world, scripts
        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.verify_code = 0
        self.verify_calls = []

    def run(self, *extra, stdin="", version="2.1.284", host_opts=()):
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        self.capsys.readouterr()
        argv = ["--home", str(self.world.home), *host_opts, "bring-up",
                *(["--claude-code-version", version] if version else []),
                *extra]
        code = amap_openshell.main(argv)
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture
def host(tmp_path, fake_bin, monkeypatch, capsys):
    world = _l1_world.World(tmp_path, fake_bin)
    for k, v in world.env().items():
        monkeypatch.setenv(k, v)
    scripts = RouterScripts(tmp_path / "router-scripts")
    monkeypatch.setattr(l1_run, "router_script",
                        lambda ctx, name: str(scripts.dir / name))
    monkeypatch.setattr(bring_up, "SETTINGS",
                        l1_run.Settings(poll_interval=0.01, router_timeout=2))
    h = Host(world, scripts, monkeypatch, capsys)

    def fake_verify(args):
        h.verify_calls.append({
            "calls_before": len(world.calls()),
            "scripts_before": len(scripts.lines()),
            "key_in_env": KEY_VARIABLE in os.environ})
        print("verify: faked")
        return h.verify_code

    monkeypatch.setattr(verify, "main", fake_verify)
    monkeypatch.setattr(bring_up, "interceptor_problem", lambda home: "")
    return h


def last(out):
    return [ln for ln in out.splitlines() if ln.strip()][-1]


def headers(out):
    seen = []
    for m in re.finditer(r"^stage \d+/\d+ ([a-z-]+):", out, re.M):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def destructive(call):
    a = call["argv"]
    if call["prog"] == "openshell":
        rest = a[2:] if a[:1] == ["--workspace"] else a
        return (rest[:1] == ["sandbox"] and rest[1] in ("delete", "stop",
                                                        "start")) \
            or rest[:3] == ["provider", "profile", "delete"]
    if call["prog"] == "docker":
        return a[0] in ("rm", "rmi", "stop", "kill", "restart")
    return False


def files_under(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def creates(world):
    return [c for c in world.calls() if c["prog"] == "openshell"
            and "create" in c["argv"] and "sandbox" in c["argv"]]


def create_name(call):
    a = call["argv"]
    return a[a.index("--name") + 1]


# --- AC1: the interface --------------------------------------------------------

def test_help_lists_bring_up_as_a_word():
    r = subprocess.run([sys.executable, str(REPO / "amap-openshell.py"),
                        "--help"], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0
    assert re.search(r"(?<!\w)bring-up(?!\w)", r.stdout)


def test_bring_up_accepts_exactly_the_bootstrap_interface():
    parser = amap_openshell.build_parser()
    args = parser.parse_args([
        "--home", "/h", "--fleet", "examples/fleet.json", "bring-up",
        "--claude-code-version", "2.1.284", "--api-key-stdin", "--apply"])
    assert args.command == "bring-up"
    assert args.claude_code_version == "2.1.284"
    assert args.api_key_stdin is True and args.apply is True
    bare = parser.parse_args(["bring-up"])
    assert (bare.claude_code_version, bare.api_key_stdin, bare.apply) == (
        None, False, False)
    import argparse
    (action,) = [a for a in parser._actions
                 if isinstance(a, argparse._SubParsersAction)]
    sub = action.choices["bring-up"]
    flags = {o for a in sub._actions for o in a.option_strings}
    assert flags == {"-h", "--help", "--claude-code-version",
                     "--api-key-stdin", "--apply"}
    assert not [a for a in sub._actions if not a.option_strings]
    for bad in (["bring-up", "--only", "3"],
                ["bring-up", "--claude-code-version"]):
        with pytest.raises(SystemExit) as e:
            parser.parse_args(bad)
        assert e.value.code == 2


def test_bring_up_needs_a_home(monkeypatch, capsys):
    monkeypatch.delenv("AMAP_OPENSHELL_HOME", raising=False)
    assert amap_openshell.main(["bring-up"]) == 2
    assert "--home is required" in capsys.readouterr().err


def test_the_bootstrap_command_line_runs(host, monkeypatch):
    monkeypatch.chdir(REPO)
    monkeypatch.delenv(KEY_VARIABLE)
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    code = amap_openshell.main([
        "--home", str(host.world.home), "--fleet", "examples/fleet.json",
        "bring-up", "--claude-code-version", "2.1.284", "--api-key-stdin",
        "--apply"])
    out = host.capsys.readouterr().out
    assert code == 0, out
    assert last(out) == UP


# --- the version ---------------------------------------------------------------

def test_no_version_is_a_refusal(host, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_VERSION")
    code, out, _ = host.run(version=None)
    assert code == 2
    final = last(out)
    assert final.startswith("bring-up stopped at preflight:")
    assert "--claude-code-version" in final and "CLAUDE_CODE_VERSION" in final
    assert host.world.calls() == []
    assert not host.world.home.exists()


def test_the_version_flag_wins_over_the_environment(host):
    code, out, _ = host.run()
    assert code == 0, out
    assert "CLAUDE_CODE_VERSION=2.1.284" in out
    code, out, _ = host.run(version="9.9.9")
    assert code == 0, out
    assert "CLAUDE_CODE_VERSION=9.9.9" in out
    assert "CLAUDE_CODE_VERSION=2.1.284" not in out


# --- AC2: the dry run ----------------------------------------------------------

def test_the_dry_run_on_a_fresh_host_changes_nothing(host):
    code, out, err = host.run()
    assert code == 0, out + err
    assert not host.world.home.exists()
    calls = host.world.calls()
    assert calls
    assert not [c for c in calls if test_l1_run.is_mutating(c)]
    assert host.scripts.lines() == []
    assert host.verify_calls == []
    for n, name in enumerate(STAGE_NAMES, 1):
        assert re.search(rf"^stage {n}/7 {name}: would", out, re.M), name
    assert last(out) == bring_up.DRY_LINE
    assert UP not in out


def test_the_dry_run_on_a_brought_up_host_changes_nothing(host):
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    home = host.world.home
    tree, files = test_l1_kit.tree(home), files_under(home)
    calls = len(host.world.calls())
    scripts, verifies = host.scripts.lines(), len(host.verify_calls)
    code, out, err = host.run()
    assert code == 0, out + err
    assert test_l1_kit.tree(home) == tree
    assert files_under(home) == files
    assert host.scripts.lines() == scripts
    assert len(host.verify_calls) == verifies
    new = host.world.calls()[calls:]
    assert new
    assert not [c for c in new if test_l1_run.is_mutating(c)], new
    for name in ("image", "provision", "router"):
        n = STAGE_NAMES.index(name) + 1
        assert re.search(rf"^stage {n}/7 {name}: would skip", out, re.M), \
            (name, out)


def test_the_dry_run_reads_the_key_but_never_shows_it(host):
    code, out, err = host.run("--api-key-stdin", stdin=KEY + "\n")
    assert code == 0, out + err
    assert "key: given on stdin" in out
    for secret in (KEY, KEY[-20:]):
        assert secret not in out and secret not in err


# --- AC3: the seven stages -----------------------------------------------------

def is_agent_build(call):
    return (call["prog"] == "docker" and call["argv"][:1] == ["build"]
            and "amap-openshell-agent" in call["argv"])


def test_apply_on_a_fresh_host_runs_the_seven_stages_in_order(host):
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert last(out) == UP
    assert headers(out) == STAGE_NAMES
    calls = host.world.calls()

    def first(pred):
        return next(i for i, c in enumerate(calls) if pred(c))
    build = first(is_agent_build)
    imported = first(lambda c: test_l1_run.is_profile_call(c, "import"))
    created = first(lambda c: c in creates(host.world))
    router = first(lambda c: c["prog"] == "docker"
                   and c["argv"][:1] == ["run"]
                   and "amap-openshell-agent" not in c["argv"])
    assert build < imported < created < router
    lines = host.scripts.lines()
    assert lines[0].startswith("build.sh")
    assert lines[1].startswith(
        f"run.sh --config {host.world.home}/router.json --detach")
    assert len(host.verify_calls) == 1
    assert host.verify_calls[0]["calls_before"] == len(calls)
    assert host.verify_calls[0]["scripts_before"] == len(lines)
    home = host.world.home
    recorded = json.loads((home / "membership.json").read_text())
    assert "alpha" in json.dumps(recorded) and "beta" in json.dumps(recorded)
    assert (home / "router.json").is_file()
    assert (home / "roster" / "roster.json").is_file()


@pytest.mark.parametrize("stage", ["image", "gateway-config", "profile",
                                   "provision", "router"])
def test_a_failing_stage_stops_there(host, tmp_path, stage):
    opts = ()
    if stage == "image":
        host.world.set_flag(fail_build=True)
    elif stage == "gateway-config":
        toml = tmp_path / "partial.toml"
        toml.write_text("[openshell]\nversion = 2\n", encoding="utf-8")
        opts = ("--gateway-toml", str(toml))
    elif stage == "profile":
        host.world.set_flag(fail_lint=True)
    elif stage == "provision":
        host.world.set_flag(fail_create=True)
    else:
        host.world.set_flag(fail_router_once=True)
    code, out, err = host.run("--apply", host_opts=opts)
    assert code != 0 and UP not in out
    assert last(out).startswith(f"bring-up stopped at {stage}:"), out
    n = STAGE_NAMES.index(stage) + 1
    assert f"stage {n}/7 {stage}: stopped" in out
    later = STAGE_NAMES[n:]
    assert not [x for x in headers(out) if x in later]
    assert host.verify_calls == []
    if STAGE_NAMES.index(stage) < STAGE_NAMES.index("provision"):
        assert not [c for c in host.world.calls() if "create" in c["argv"]
                    and c["prog"] == "openshell"]
    if STAGE_NAMES.index(stage) < STAGE_NAMES.index("router"):
        assert host.scripts.lines() == []
    if stage == "gateway-config":
        assert not (host.world.home / "payload").exists()


def test_a_router_container_that_exists_but_is_not_running_is_refused_not_removed(
        host):
    host.world.set_flag(router_container_state="exited")
    code, out, err = host.run("--apply")
    assert code != 0
    final = last(out)
    assert final.startswith("bring-up stopped at router:") and "exited" in final
    assert host.scripts.lines() == []
    assert not [c for c in host.world.calls() if destructive(c)]
    assert host.verify_calls == []


# --- AC4: a rerun --------------------------------------------------------------

def test_a_rerun_builds_creates_imports_and_starts_nothing(host):
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    w = host.world
    calls = len(w.calls())
    scripts = host.scripts.lines()
    builds = w.state()["builds"]
    outside = test_l1_run.tree_outside_evidence(w.home)
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert last(out) == UP
    new = w.calls()[calls:]
    assert new
    for c in new:
        a = c["argv"]
        assert not (c["prog"] == "docker" and a[0] == "build"), c
        assert not (c["prog"] == "docker" and a[0] == "run"
                    and not test_l1_run.is_image_probe(c)), c
        assert c not in creates(w)
        assert not (test_l1_run.is_profile_call(c)
                    and a[3:5] in (["profile", "import"],
                                   ["profile", "update"])), c
    assert host.scripts.lines() == scripts
    assert w.state()["builds"] == builds
    assert test_l1_run.tree_outside_evidence(w.home) == outside
    assert len(host.verify_calls) == 2
    for name in ("image", "profile", "provision", "router"):
        assert f"{name}: skipped" in out, (name, out)


# --- AC5: the key --------------------------------------------------------------

def assert_key_only_in_creates(host):
    w = host.world
    made = creates(w)
    assert {create_name(c) for c in made} == {"alpha", "beta"}
    assert all(c["key"] for c in made)
    assert not [c for c in w.calls() if c["key"] and c not in made]
    assert not [ln for ln in host.scripts.lines() if "key=set" in ln]
    assert host.verify_calls and not host.verify_calls[0]["key_in_env"]


def test_the_stdin_key_reaches_only_the_create_calls(host, monkeypatch):
    monkeypatch.delenv(KEY_VARIABLE)
    code, out, err = host.run("--api-key-stdin", "--apply", stdin=KEY + "\n")
    assert code == 0, out + err
    assert_key_only_in_creates(host)
    assert KEY_VARIABLE not in os.environ


def test_the_environment_key_reaches_only_the_create_calls(host):
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert_key_only_in_creates(host)
    assert os.environ[KEY_VARIABLE] == KEY


def test_a_missing_key_with_a_member_to_create_refuses_before_any_change(
        host, monkeypatch):
    monkeypatch.delenv(KEY_VARIABLE)
    code, out, err = host.run("--apply")
    assert code == 1
    final = last(out)
    assert final.startswith("bring-up stopped at preflight:")
    assert "--api-key-stdin" in final and KEY_VARIABLE in final
    assert not host.world.home.exists()
    calls = host.world.calls()
    assert not [c for c in calls if test_l1_run.is_mutating(c)]
    assert not [c for c in calls if c["argv"][:1] == ["build"]]
    assert host.scripts.lines() == [] and host.verify_calls == []


def test_a_missing_key_with_nothing_to_create_carries_on(host, monkeypatch):
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    monkeypatch.delenv(KEY_VARIABLE)
    before = len(creates(host.world))
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert last(out) == UP
    assert len(creates(host.world)) == before


def test_an_empty_stdin_key_is_a_refusal(host):
    code, out, err = host.run("--api-key-stdin", "--apply", stdin="\n")
    assert code == 1
    assert last(out).startswith("bring-up stopped at preflight:")
    assert not host.world.home.exists()
    assert not [c for c in host.world.calls() if test_l1_run.is_mutating(c)]


@pytest.mark.parametrize("source", ["stdin", "env"])
def test_the_fake_key_appears_in_no_file_no_output_and_no_argv(
        host, monkeypatch, source):
    if source == "stdin":
        monkeypatch.delenv(KEY_VARIABLE)
        code, out, err = host.run("--api-key-stdin", "--apply",
                                  stdin=KEY + "\n")
    else:
        code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert creates(host.world)
    for secret in (KEY, KEY[-20:]):
        s = secret.encode()
        assert not [p for p, data in files_under(host.world.home).items()
                    if s in data]
        assert secret not in out and secret not in err
        assert not [c for c in host.world.calls()
                    if secret in json.dumps(c["argv"])]
        assert not [ln for ln in host.scripts.lines() if secret in ln]


# --- AC6: verify decides -------------------------------------------------------

@pytest.mark.parametrize("n", [1, 3])
def test_a_failing_verify_makes_bring_up_fail(host, n):
    host.verify_code = n
    code, out, err = host.run("--apply")
    assert code == n
    assert last(out) == (f"bring-up stopped at verify: verify exited {n}; "
                         f"see its report above")
    assert UP not in out


# --- constraints ---------------------------------------------------------------

def test_bring_up_is_never_destructive(host, tmp_path):
    w = host.world
    toml = w.gateway_toml.read_bytes()
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert not [c for c in w.calls() if destructive(c)]
    assert w.gateway_toml.read_bytes() == toml
    banned = {"delete", "rm", "rmi", "kill", "restart", "stop"}
    tree = ast.parse((REPO / "bring_up.py").read_text(encoding="utf-8"))
    assert not [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value in banned]


def test_a_failed_create_is_not_destructive(host):
    host.world.set_flag(fail_create=True)
    code, out, err = host.run("--apply")
    assert code != 0
    assert not [c for c in host.world.calls() if destructive(c)]


FORBIDDEN_L1_RUN = {
    "step_0", "step_2", "step_4", "step_5", "step_7", "step_8", "step_9",
    "STEP_FUNCS", "Runner", "execute", "describe_step", "CHECKS", "run_check",
    "cmd_submit", "submit_argv", "bounce", "cmd_stop", "cmd_start"}


def test_bring_up_runs_no_l1_run_step_and_no_experiment(host):
    tree = ast.parse((REPO / "bring_up.py").read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "l1_run"}
    assert used and not used & FORBIDDEN_L1_RUN
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    for c in host.world.calls():
        assert not (c["prog"] == "openshell" and "exec" in c["argv"]), c
        assert not [w for w in c["argv"] if w.endswith("inbox-submit")], c


def test_the_openshell_option_is_honoured(host, fake_bin):
    alt = fake_bin / "alt-openshell"
    alt.write_text(_l1_world.STUB.format(
        python=sys.executable, tests=str(_l1_world.TESTS_DIR),
        prog="openshell"), encoding="utf-8")
    alt.chmod(0o755)
    (fake_bin / "openshell").unlink()
    code, out, err = host.run("--apply", host_opts=("--openshell", str(alt)))
    assert code == 0, out + err
    assert last(out) == UP
    assert [c for c in host.world.calls() if c["prog"] == "openshell"]


def test_the_image_and_run_as_options_are_honoured(host):
    code, out, err = host.run(host_opts=(
        "--image", "amap-openshell-agent:alt", "--run-as", "1234:5678"))
    assert code == 0, out + err
    for text in ("-t amap-openshell-agent:alt", "SANDBOX_UID=1234",
                 "SANDBOX_GID=5678", "amap-openshell.run-as=1234:5678"):
        assert text in out, text
    inspects = [c for c in host.world.calls() if c["prog"] == "docker"
                and c["argv"][:2] == ["image", "inspect"]]
    assert inspects
    assert inspects[0]["argv"][-1] == "amap-openshell-agent:alt"


def test_bring_up_text_is_identifier_clean():
    path = REPO / "bring_up.py"
    src = path.read_text(encoding="utf-8")
    assert "/home/" not in src and "/Users/" not in src
    assert not re.search(r"\b[\w-]+\.(com|net|io|dev|co)\b", src)
    tree = ast.parse(src)
    lanes = set(router_config.LANES)
    assert not [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value in lanes]


# --- the l1_run factoring ------------------------------------------------------

def test_the_l1_run_image_builders_take_the_image(host):
    ctx = test_l1_run.make_ctx(host.world)
    assert ctx.image == l1_run.IMAGE
    assert l1_run.cmd_image_inspect()[1][-1] == l1_run.IMAGE
    assert l1_run.IMAGE in l1_run.cmd_image_paths()[1]
    ctx.image = "x"
    argv = l1_run.build_argv(ctx)
    assert argv[argv.index("-t") + 1] == "x"
    assert l1_run.cmd_image_inspect("x")[1][-1] == "x"
    assert "x" in l1_run.cmd_image_paths("x")[1]


def test_image_is_current(host):
    ctx = test_l1_run.make_ctx(host.world)
    good = {l1_run.IMAGE_LABEL_VERSION: ctx.claude_code_version,
            l1_run.IMAGE_LABEL_RUN_AS: ctx.run_as_text}

    def result(code, stdout):
        return l1_run.Result(("docker",), code, stdout, "", "", 0.0, False, "")
    assert l1_run.image_is_current(ctx, result(0, json.dumps(good)))
    assert not l1_run.image_is_current(ctx, result(0, json.dumps(
        {**good, l1_run.IMAGE_LABEL_VERSION: "0.0.0"})))
    assert not l1_run.image_is_current(ctx, result(0, json.dumps(
        {**good, l1_run.IMAGE_LABEL_RUN_AS: "1:1x"})))
    assert not l1_run.image_is_current(ctx, result(1, json.dumps(good)))
    assert not l1_run.image_is_current(ctx, result(0, "not json"))
    assert not l1_run.image_is_current(ctx, result(0, json.dumps([good])))


# --- the docs ------------------------------------------------------------------

def test_the_readme_bring_up_leads_with_the_one_command():
    body = test_docs_agree.section(README.read_text(encoding="utf-8"),
                                   "## Bring-up")
    calls = test_l1_runbook.amap_openshell_calls(body)
    parser = amap_openshell.build_parser()
    parsed = [parser.parse_args(rest) for _, rest in calls]
    assert parsed[0].command == "bring-up"
    ups = [a for a in parsed if a.command == "bring-up"]
    assert any(not a.apply for a in ups) and any(a.apply for a in ups)

    def has(command, apply, name=None):
        return any(a.command == command and a.apply is apply
                   and getattr(a, "name", None) == name for a in parsed)
    assert has("install", True)
    assert has("provision", True, "alpha") and has("provision", True, "beta")
    assert any(a.command == "verify" for a in parsed)
    assert "docker/build.sh" in body and "docker/run.sh" in body


def test_the_readme_verbs_table_lists_bring_up():
    assert re.search(r"^\|\s*`bring-up`", README.read_text(encoding="utf-8"),
                     re.M)


def test_the_tutorial_says_what_bring_up_replaces():
    parsed = [a for a in test_docs_agree.parsed_calls(TUTORIAL)
              if a.command == "bring-up"]
    assert any(not a.apply for a in parsed) and any(a.apply for a in parsed)
    prose = "\n".join(test_l1_runbook.outside_fences(
        TUTORIAL.read_text(encoding="utf-8")))
    for phrase in ("steps 3 and 5 to 9", "step 4's `gateway-config` check",
                   "Steps 10 and 11", "stay by hand"):
        assert phrase in prose, phrase


def test_a_keyless_rerun_after_a_router_stop_passes_the_profile_stage(
        host, monkeypatch):
    """L2's host: the first run imported the profile and stopped at router.
    The rerun, with no key, must not lint the imported profile again, since
    OpenShell's lint rejects an ID the gateway already has."""
    host.world.set_flag(fail_router_once=True)
    code, out, err = host.run("--apply")
    assert last(out).startswith("bring-up stopped at router:"), out
    monkeypatch.delenv(KEY_VARIABLE)
    code, out, err = host.run("--apply")
    assert code == 0, out + err
    assert "stage 4/7 profile: skipped" in out, out
    assert last(out) == UP


def test_a_lint_failure_reports_every_diagnostic_line(host):
    host.world.set_flag(fail_lint=True)
    code, out, err = host.run("--apply")
    assert code != 0
    final = last(out)
    assert "Provider profile diagnostics:" in final, out
    assert "profile lint failed" in final, out


# --- S8d: the router stage stops at once on a crash --------------------------

@pytest.mark.parametrize("state", ["restarting", "exited"])
def test_the_router_stage_stops_at_once_on_a_crash_looping_container(
        host, monkeypatch, state):
    import time
    monkeypatch.setattr(bring_up, "SETTINGS",
                        l1_run.Settings(poll_interval=0.01, router_timeout=60))
    host.world.set_flag(router_crash=state, router_exit_code=126,
                        router_crash_log="entrypoint.sh: permission denied")
    started = time.monotonic()
    code, out, err = host.run("--apply")
    elapsed = time.monotonic() - started
    assert code != 0 and elapsed < 30
    final = last(out)
    assert final.startswith("bring-up stopped at router:"), final
    assert state in final and "exit code 126" in final
    assert "entrypoint.sh: permission denied" in final
    assert UP not in out
    assert host.verify_calls == []
    calls = host.world.calls()
    assert not [c for c in calls if destructive(c)]
    assert not [c for c in calls if c["prog"] == "docker"
                and c["argv"][0] in ("start", "update")]
    assert len([ln for ln in host.scripts.lines()
                if ln.startswith("run.sh")]) == 1


# --- the mounts interceptor must be running (S10b) -----------------------------

def test_apply_stops_before_any_change_when_the_interceptor_is_not_running(
        host, monkeypatch):
    monkeypatch.setattr(bring_up, "interceptor_problem",
                        REAL_INTERCEPTOR_PROBLEM)
    toml = host.world.gateway_toml.read_bytes()
    tree = files_under(host.world.home) if host.world.home.exists() else {}
    code, out, err = host.run("--apply", "--api-key-stdin", stdin=KEY + "\n")
    assert code == 1
    assert last(out).startswith(
        "bring-up stopped at preflight: the mounts interceptor is not running")
    assert "examples/vms/proxmox/provision-guest.sh" in last(out)
    assert "stage 1/7" not in out
    for c in host.world.calls():
        assert not is_agent_build(c) and c not in creates(host.world), c
        assert not destructive(c), c
    assert host.world.gateway_toml.read_bytes() == toml
    after = files_under(host.world.home) if host.world.home.exists() else {}
    assert after == tree


def test_the_dry_run_foresees_the_missing_interceptor(host, monkeypatch):
    monkeypatch.setattr(bring_up, "interceptor_problem",
                        REAL_INTERCEPTOR_PROBLEM)
    code, out, err = host.run()
    assert code == 1
    assert "preflight: the mounts interceptor is not running" in out
    assert headers(out) == STAGE_NAMES
    assert last(out).startswith("dry run: --apply would stop at preflight:")
    assert "provision-guest.sh" in last(out)


def test_interceptor_problem_is_empty_only_when_something_accepts():
    base = Path(tempfile.mkdtemp(prefix="bu-", dir="/tmp")).resolve()
    try:
        home = str(base / "home")
        problem = REAL_INTERCEPTOR_PROBLEM(home)
        assert "provision-guest.sh" in problem and f"--home {home}" in problem
        os.makedirs(os.path.join(home, "run"))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(wire.socket_path(home))
            s.listen(1)
            assert REAL_INTERCEPTOR_PROBLEM(home) == ""
        finally:
            s.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)
