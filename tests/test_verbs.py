"""The operator verbs, against a fake `openshell`.

Facts relied on, at OpenShell main@acbac9c: `sandbox get --output json` has
`id`, `name` and `workspace` (crates/openshell-cli/src/run.rs:2789-2791), which
is where `provision` reads the ID it records; OpenShell reuses a deleted
sandbox's name under a new ID (docs/how-it-works/sandboxes/operations.mdx:27),
which the fake reproduces; a bind mount whose source does not exist is refused
(crates/openshell-driver-docker/src/lib.rs:3753-3761), so `provision` writes
the lanes before it creates.

Facts relied on in amap-router-local: `router/firstsight.py` (a recreated name
adopts an old first-sight marker unless it is deleted) and `router/reset.py:
96-112` (lane roots and skeleton leaves are never removed, and `audit/` is
kept). Nothing here runs OpenShell, Docker or the router.
"""

import argparse
import ast
import json
import os
import re
import shlex
import shutil
import socket
import stat
from pathlib import Path

import pytest

import _fake_openshell
import _workspace
import amap_openshell
import l1_kit
import membership
import openshell_cli
import policy
import render
import router_config
import verbs
from membership import Member
from test_l1_kit import tree

REPO = Path(__file__).absolute().parents[1]
RUN_AS = "1234:5678"
IMAGE = "amap-openshell-agent:test"
WRITING = ("install", "provision alpha", "deprovision alpha", "router-config",
           "teardown")


@pytest.fixture
def fake(tmp_path, fake_bin, monkeypatch):
    f = _fake_openshell.Fake(tmp_path, fake_bin)
    for k, v in f.env().items():
        monkeypatch.setenv(k, v)
    return f


class Op:
    """One host home, and the verbs run against it."""

    def __init__(self, tmp_path, fake, capsys):
        self.home = tmp_path / "home"
        self.fake = fake
        self.capsys = capsys

    def run(self, *argv, home=None, host=()):
        self.capsys.readouterr()
        code = amap_openshell.main([
            "--home", str(home or self.home), "--openshell",
            self.fake.binary(), "--run-as", RUN_AS, "--image", IMAGE, *host,
            *argv])
        c = self.capsys.readouterr()
        return code, c.out, c.err

    def ok(self, *argv):
        code, out, err = self.run(*argv)
        assert code == 0, (argv, out, err)
        return out

    def install(self):
        return self.ok("install", "--apply")

    def provision(self, *names):
        for n in names:
            self.ok("provision", n, "--apply")

    def creates(self):
        return [c for c in self.fake.calls() if "create" in c["argv"]]

    def deletes(self):
        return [c for c in self.fake.calls() if "delete" in c["argv"]]

    def fleet(self):
        return policy.load_fleet(str(self.home / "fleet.json"))

    def members(self):
        return membership.load(str(self.home / "membership.json"))


@pytest.fixture
def op(tmp_path, fake, capsys):
    return Op(tmp_path, fake, capsys)


@pytest.fixture
def full(op):
    """Installed, with alpha and beta provisioned."""
    op.install()
    op.provision("alpha", "beta")
    return op


# --- acceptance criterion 1 ----------------------------------------------------

def test_no_verb_writes_without_apply(full, tmp_path):
    toml = tmp_path / "gw.toml"
    toml.write_text(_l1_gateway())
    before, calls = tree(tmp_path / "home"), len(full.fake.calls())
    for verb in (*WRITING, "list", "gateway-config"):
        code, out, err = full.run("--gateway-toml", str(toml), *verb.split())
        assert code in (0, 1), (verb, out, err)
        assert tree(tmp_path / "home") == before, verb
    new = full.fake.calls()[calls:]
    assert not [c for c in new if "create" in c["argv"]
                or "delete" in c["argv"]]


def _l1_gateway():
    import _l1_world
    return _l1_world.GATEWAY_TOML


# --- install -------------------------------------------------------------------

def test_install_writes_the_layout_and_nothing_else(op):
    op.install()
    found = {p.relative_to(op.home).as_posix() for p in op.home.rglob("*")}
    sources = l1_kit.payload_sources(_workspace.CONNECTOR_ROOT)
    assert found == ({"fleet.json", "roster", "instances", "policies",
                      "commands", "payload", "payload/bin"}
                     | {"payload/" + s.rel for s in sources})


def test_the_installed_payload_is_byte_for_byte(op):
    op.install()
    sources = l1_kit.payload_sources(_workspace.CONNECTOR_ROOT)
    for src in sources:
        written = op.home / "payload" / src.rel
        assert written.read_bytes() == src.path.read_bytes(), src.rel
        assert stat.S_IMODE(written.stat().st_mode) == \
            (l1_kit.EXEC_MODE if src.executable else l1_kit.FILE_MODE)
    on_disk = {p.relative_to(op.home / "payload").as_posix()
               for p in (op.home / "payload").rglob("*") if p.is_file()}
    assert on_disk == {s.rel for s in sources}


def test_install_never_overwrites_the_operators_fleet_json(op):
    op.install()
    path = op.home / "fleet.json"
    path.write_text(json.dumps(json.loads(path.read_text()), indent=8) + "\n")
    mine = path.read_bytes()
    out = op.ok("install", "--apply")
    assert path.read_bytes() == mine
    assert any(ln.startswith("present fleet.json") for ln in out.splitlines())


def test_install_refuses_a_fleet_it_cannot_load(op, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    before = tree(tmp_path)
    code, out, err = op.run("install", "--apply", host=("--fleet", str(bad)))
    assert code == 1 and err.strip()
    assert tree(tmp_path) == before and not op.home.exists()


def test_install_renders_the_router_files_only_when_every_member_is_recorded(op):
    op.install()
    op.provision("alpha")
    out = op.ok("install", "--apply")
    assert "still to provision: beta" in out
    for n in ("router.json", "selected.json", "router-state"):
        assert not (op.home / n).exists()

    op.provision("beta")
    for n in ("router.json", "selected.json"):
        (op.home / n).unlink()
    shutil.rmtree(op.home / "router-state")
    op.ok("install", "--apply")
    fleet, members = op.fleet(), op.members()
    want = router_config.render_router_json(fleet, members, str(op.home))
    assert (op.home / "router.json").read_text() == want.text
    assert (op.home / "selected.json").read_text() == router_config.json_text(
        router_config.render_verdict(members, policy.workspace_of(fleet)))
    assert stat.S_IMODE((op.home / "router-state").stat().st_mode) == 0o700


def test_absent_and_empty_membership_are_different_answers(op):
    absent = op.install()
    assert "membership.json is absent: nothing has been recorded" in absent
    (op.home / "membership.json").write_text(membership.dump([]))
    empty = op.ok("install", "--apply")
    assert "membership.json lists no member" in empty
    # The home record's line names a tmp path that carries this test's name.
    said = "\n".join(ln for ln in empty.splitlines()
                     if not ln.startswith("recorded home "))
    assert "absent" not in said.replace("absent fleet", "")
    for n in ("router.json", "selected.json", "router-state"):
        assert not (op.home / n).exists()


# --- provision -----------------------------------------------------------------

def test_provision_creates_the_lanes_the_policy_and_the_command(full):
    home = str(full.home)
    for p in router_config.lane_paths(home, "alpha"):
        assert os.path.isdir(p), p
    host = render.Host(home, IMAGE, render.parse_run_as(RUN_AS), False)
    r = render.render_member(full.fleet(), "alpha", host,
                             l1_kit.policy_file(home, "alpha"))
    assert Path(l1_kit.policy_file(home, "alpha")).read_text() == r.policy_yaml
    assert Path(l1_kit.command_file(home, "alpha")).read_text() == \
        l1_kit.command_text(r.argv)


def test_provision_runs_the_rendered_create_command(full):
    written = shlex.split((full.home / "commands" / "create-alpha.sh")
                          .read_text())
    (call,) = [c for c in full.creates() if "alpha" in c["argv"]]
    assert call["argv"] == written[1:]


def test_the_written_create_command_names_openshell_not_the_fake(full):
    text = (full.home / "commands" / "create-alpha.sh").read_text()
    assert text.startswith("openshell ")
    assert full.fake.binary() not in text


def test_provision_records_the_id_the_fake_returns(full):
    box = full.fake.sandboxes()["alpha"]
    got = membership.find(full.members(), "alpha")
    assert got == Member(box["workspace"], box["name"], box["id"])
    assert got.workspace == policy.workspace_of(full.fleet())


def test_provision_is_idempotent(full):
    before = (full.home / "membership.json").read_bytes()
    n = len(full.creates())
    out = full.ok("provision", "alpha", "--apply")
    assert len(full.creates()) == n
    assert "present sandbox alpha" in out
    assert "present membership.json" in out
    assert (full.home / "membership.json").read_bytes() == before


def test_reprovision_with_a_new_id_refuses_until_deprovision(full):
    old = membership.find(full.members(), "alpha").id
    before = (full.home / "membership.json").read_bytes()
    n = len(full.creates())
    full.fake.forget("alpha")
    code, out, err = full.run("provision", "alpha", "--apply")
    assert code == 1 and old in err and "deprovision" in err
    assert len(full.creates()) == n
    assert (full.home / "membership.json").read_bytes() == before
    full.ok("deprovision", "alpha", "--apply")
    full.ok("provision", "alpha", "--apply")
    new = membership.find(full.members(), "alpha").id
    assert new != old and new == full.fake.sandboxes()["alpha"]["id"]


def test_a_sandbox_under_this_name_with_another_id_is_refused(full):
    full.fake.forget("alpha")
    full.fake.put("alpha", "id-other-9")
    recorded = membership.find(full.members(), "alpha")
    before, calls = tree(full.home), len(full.fake.calls())
    code, out, err = full.run("provision", "alpha", "--apply")
    assert code == 1
    assert str(membership.IdMismatch(recorded, Member(
        recorded.workspace, "alpha", "id-other-9"))) in err
    assert tree(full.home) == before
    assert not [c for c in full.fake.calls()[calls:]
                if "create" in c["argv"] or "delete" in c["argv"]]


def test_provision_refuses_a_name_the_fleet_does_not_name(op):
    op.install()
    code, out, err = op.run("provision", "gamma", "--apply")
    assert code == 1 and "alpha" in err and "beta" in err
    assert not op.creates()


def test_provision_refuses_before_install(op):
    code, out, err = op.run("provision", "alpha", "--apply")
    assert code == 1 and "install --apply" in err
    for gone in ("payload", "roster"):
        op.install()
        shutil.rmtree(op.home / gone)
        code, out, err = op.run("provision", "alpha", "--apply")
        assert code == 1 and "install --apply" in err and gone in err
        shutil.rmtree(op.home)
    assert not op.creates()


def test_provision_refuses_to_create_without_the_api_key(op, monkeypatch):
    op.install()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    before = tree(op.home)
    code, out, err = op.run("provision", "alpha", "--apply")
    assert code == 1 and "ANTHROPIC_API_KEY" in err
    assert not op.creates() and tree(op.home) == before


def test_only_the_create_call_carries_the_provider_key(full):
    full.ok("list")
    full.ok("deprovision", "beta", "--apply")
    calls = full.fake.calls()
    assert calls
    for c in calls:
        assert c["key"] is ("create" in c["argv"]), c


def test_an_unknown_sandbox_state_is_unknown_not_a_pass(op):
    op.install()
    op.fake.set_flag(fail_get=True, fail_list=True)
    before = tree(op.home)
    code, out, err = op.run("provision", "alpha", "--apply")
    assert code == 1 and "UNKNOWN" in out and "PASS" not in out
    assert tree(op.home) == before and not op.creates()


# --- deprovision ---------------------------------------------------------------

def test_deprovision_empties_the_lanes_and_clears_the_first_sight_marker(full):
    home = str(full.home)
    roots = {render.lane_dir(home, "alpha", lane)
             for lane in router_config.LANES}
    leaves = [p for p in router_config.lane_paths(home, "alpha")
              if p not in roots]
    for leaf in leaves:
        Path(leaf, "x.json").write_text("{}")
        os.mkdir(os.path.join(leaf, "sub"))
        Path(leaf, "sub", "y").write_text("y")
    state = full.home / "router-state" / "alpha"
    (state / "results").mkdir(parents=True)
    (state / "audit").mkdir()
    (state / "first-seen.json").write_text("{}")
    (state / "results" / "00000000.json").write_text("{}")
    (state / "audit" / "log.jsonl").write_text("line\n")
    router_json = (full.home / "router.json").read_bytes()

    out = full.ok("deprovision", "alpha", "--apply")
    assert [c for c in full.deletes() if c["argv"][-1] == "alpha"]
    assert "alpha" not in full.fake.sandboxes()
    for p in router_config.lane_paths(home, "alpha"):
        assert os.path.isdir(p), p
    for leaf in leaves:
        assert os.listdir(leaf) == [], leaf
    assert not (state / "first-seen.json").exists()
    assert not (state / "results").exists()
    assert (state / "audit" / "log.jsonl").read_bytes() == b"line\n"
    assert [m.name for m in full.members()] == ["beta"]
    selected = json.loads((full.home / "selected.json").read_text())
    assert [s["slug"] for s in selected["selected"]] == ["beta"]
    assert (full.home / "router.json").read_bytes() == router_json
    assert "fleet.json" in out and "router-config --apply" in out


def test_deprovision_leaves_a_sandbox_that_is_not_the_recorded_member_alone(
        full):
    full.fake.forget("alpha")
    full.fake.put("alpha", "id-other-9")
    out = full.ok("deprovision", "alpha", "--apply")
    assert not full.deletes()
    assert full.fake.sandboxes()["alpha"]["id"] == "id-other-9"
    assert "left sandbox alpha alone" in out
    assert [m.name for m in full.members()] == ["beta"]


def test_deprovision_of_an_unrecorded_name_is_a_refusal(op):
    op.install()
    before = tree(op.home)
    code, out, err = op.run("deprovision", "alpha", "--apply")
    assert code == 1 and "alpha" in err
    assert not op.deletes() and tree(op.home) == before


# --- nothing inside a sandbox, nothing but openshell ----------------------------

def test_nothing_is_written_inside_a_sandbox(full):
    full.ok("deprovision", "alpha", "--apply")
    assert not [c for c in full.fake.calls() if "exec" in c["argv"]]
    for name in ("verbs.py", "openshell_cli.py"):
        tree_ = ast.parse((REPO / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree_):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in ("exec", "docker"), (name, node.value)


def test_the_verbs_run_only_openshell(full, monkeypatch):
    seen = []
    real = openshell_cli.subprocess.run

    def spy(argv, *a, **k):
        seen.append(argv[0])
        return real(argv, *a, **k)

    monkeypatch.setattr(openshell_cli.subprocess, "run", spy)
    full.ok("list")
    full.ok("deprovision", "beta", "--apply")
    assert seen and set(seen) == {full.fake.binary()}
    assert os.path.basename(full.fake.binary()) == "openshell"
    assert {c["argv"][0] for c in full.fake.calls()} <= {"--workspace", "sandbox"}


# --- list ----------------------------------------------------------------------

def test_list_names_each_member_with_its_verdict(full):
    out = full.ok("list")
    assert "alpha: member" in out and "beta: member" in out
    full.fake.set_flag(extra=["stray"], other_workspace=["elsewhere-one"])
    full.fake.forget("beta")
    code, out, err = full.run("list")
    assert code == 1
    assert "beta: recorded but absent" in out
    assert "stray: not a member" in out
    assert "elsewhere-one" not in out
    full.fake.put("beta", "id-other-7")
    code, out, err = full.run("list")
    assert code == 1 and "beta: ID MISMATCH" in out and "id-other-7" in out


def test_list_reports_unknown_when_the_listing_fails(full):
    full.fake.set_flag(fail_list=True)
    code, out, err = full.run("list")
    assert code == 1 and "UNKNOWN" in out and "PASS" not in out


# --- router-config -------------------------------------------------------------

def test_router_config_reports_drift_and_writes_with_apply(full):
    path = full.home / "router.json"
    want = router_config.render_router_json(full.fleet(), full.members(),
                                            str(full.home))
    assert full.ok("router-config").strip() == "in sync"
    doc = json.loads(path.read_text())
    doc["fleet_domain"] = "drifted.example.org"
    path.write_text(json.dumps(doc))
    code, out, err = full.run("router-config")
    assert code == 1
    for line in router_config.drift_on_disk(str(path), want):
        assert line in out
    assert json.loads(path.read_text())["fleet_domain"] == "drifted.example.org"
    full.ok("router-config", "--apply")
    assert path.read_text() == want.text
    assert full.ok("router-config").strip() == "in sync"


def test_router_config_before_every_member_is_recorded_is_a_refusal(op):
    op.install()
    op.provision("alpha")
    before = tree(op.home)
    for extra in ((), ("--apply",)):
        code, out, err = op.run("router-config", *extra)
        assert code == 1 and "beta" in err
    assert tree(op.home) == before


# --- teardown ------------------------------------------------------------------

def test_teardown_removes_installs_outputs_and_keeps_the_rest(full):
    home = str(full.home)
    code, out, err = full.run("teardown")
    assert code == 0
    for rel in ("payload", "roster", "router.json", "selected.json",
                "router-state"):
        assert f"would remove {rel}" in out.splitlines(), rel
    kept = tree(full.home)
    full.ok("teardown", "--apply")
    for rel in ("payload", "roster", "router.json", "selected.json",
                "router-state"):
        assert not (full.home / rel).exists(), rel
    for rel in ("fleet.json", "membership.json", "policies", "commands"):
        assert (full.home / rel).exists(), rel
    for name in ("alpha", "beta"):
        for p in router_config.lane_paths(home, name):
            assert os.path.isdir(p)
    assert [r for r in kept if r[0].startswith("instances")] == \
        [r for r in tree(full.home) if r[0].startswith("instances")]


def test_teardown_names_held_requests_before_removing_them(full):
    held = full.home / "router-state" / "alpha" / "held"
    held.mkdir(parents=True)
    (held / "x.json").write_text("{}")
    out = full.run("teardown")[1]
    (line,) = [ln for ln in out.splitlines() if "held/x.json" in ln]
    assert "lost" in line
    assert out.index(line) < out.index("would remove")


# --- confinement ---------------------------------------------------------------

def test_every_path_a_verb_writes_or_removes_is_under_home(full):
    ns = argparse.Namespace(home=str(full.home), run_as=RUN_AS, image=IMAGE,
                            restart_policy=False, openshell=full.fake.binary(),
                            fleet=None)
    h = verbs.host_args(ns, env=os.environ)
    fleet = verbs.loaded_fleet(h)
    alpha = membership.find(full.members(), "alpha")
    plans = [verbs.plan_install(h),
             verbs.plan_provision_files(h, fleet, "alpha")[0],
             verbs.plan_record(h, alpha),
             verbs.plan_deprovision(h, fleet, "alpha", alpha),
             verbs.plan_teardown(h)]
    home = str(full.home)
    for plan in plans:
        assert plan.home == home
        paths = ([d for d, _ in plan.dirs] + [f for f, _, _ in plan.files]
                 + [r.path for r in plan.removals])
        assert paths
        for p in paths:
            assert p == home or p.startswith(home + os.sep), p


def test_a_home_overlapping_a_repo_is_refused(op, tmp_path):
    for argv in (("install", "--apply"), ("provision", "alpha", "--apply"),
                 ("deprovision", "alpha", "--apply"), ("list",),
                 ("router-config",), ("teardown",)):
        before = tree(REPO / "payload")
        code, out, err = op.run(*argv, home=REPO)
        assert code == 1 and "overlaps" in err, argv
        assert tree(REPO / "payload") == before


def test_the_verbs_text_is_identifier_clean():
    domain = re.compile(r"\b[\w-]+\.(com|net|io|dev|co)\b")
    lanes = set(router_config.LANES)
    for name in ("verbs.py", "openshell_cli.py", "gateway.py"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "/home/" not in text and "/Users/" not in text, name
        assert not domain.search(text), name
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in lanes, (name, node.value)


# --- deprovision warns about held requests ---------------------------------------

def test_deprovision_warns_about_held_requests_and_removes_what_it_did_before(
        full):
    home = str(full.home)
    roots = {render.lane_dir(home, "alpha", lane)
             for lane in router_config.LANES}
    leaves = [p for p in router_config.lane_paths(home, "alpha")
              if p not in roots]
    for leaf in leaves:
        Path(leaf, "x.json").write_text("{}")
    rs = full.home / "router-state"
    for rel, text in (("alpha/held/req-1.json", "{}"),
                      ("alpha/results/0.json", "{}"),
                      ("alpha/audit/log.jsonl", "line\n"),
                      ("alpha/first-seen.json", "{}"),
                      ("beta/held/req-2.json", "{}")):
        (rs / rel).parent.mkdir(parents=True, exist_ok=True)
        (rs / rel).write_text(text)
    line = "held request router-state/alpha/held/req-1.json will be lost"
    expected = {"router-state/alpha/first-seen.json",
                "router-state/alpha/held", "router-state/alpha/results",
                *(os.path.relpath(p, home) for p in leaves)}

    def removals(out, verb):
        return {ln[len(verb) + 1:] for ln in out.splitlines()
                if ln.startswith(verb + " ")}

    before = tree(full.home)
    code, out, err = full.run("deprovision", "alpha")
    assert code == 0, err
    assert line in out.splitlines() and "beta/held" not in out
    assert out.index(line) < out.index("would remove")
    assert tree(full.home) == before
    wr = removals(out, "would remove")
    assert wr == expected

    code, out, err = full.run("deprovision", "alpha", "--apply")
    assert code == 0, err
    assert line in out.splitlines()
    assert out.index(line) < out.index("removed ")
    assert removals(out, "removed") == wr
    assert not (rs / "alpha" / "held").exists()
    assert not (rs / "alpha" / "results").exists()
    assert (rs / "alpha" / "audit" / "log.jsonl").read_text() == "line\n"
    assert (rs / "beta" / "held" / "req-2.json").is_file()
    for p in router_config.lane_paths(home, "alpha"):
        assert os.path.isdir(p), p


def test_teardown_and_deprovision_share_the_held_helper():
    source = (REPO / "verbs.py").read_text(encoding="utf-8")
    tree_ = ast.parse(source)
    users = set()
    for node in ast.walk(tree_):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) \
                        and getattr(sub.func, "id", "") == "held_warnings":
                    users.add(node.name)
    assert {"run_teardown", "run_deprovision"} <= users
    assert source.count('will be lost"') == 1


def test_a_held_directory_above_the_root_does_not_make_files_held(tmp_path):
    home = tmp_path / "held" / "home"
    root = home / "router-state" / "beta"
    (root / "audit").mkdir(parents=True)
    (root / "audit" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "held").mkdir()
    (root / "held" / "00000001.json").write_text("{}", encoding="utf-8")
    assert verbs.held_requests(str(home), [str(root)]) == [
        str(root / "held" / "00000001.json")]


def test_install_refuses_a_home_that_is_not_its_own_real_path(op, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    before = tree(tmp_path / "real")
    code, out, err = op.run("install", "--apply",
                            home=tmp_path / "link" / "home")
    assert code == 1
    assert str(tmp_path / "real" / "home") in err
    assert tree(tmp_path / "real") == before


# --- the fleet domain (S11, D21) ---------------------------------------------

def _host(monkeypatch, name):
    monkeypatch.setattr(socket, "gethostname", lambda: name)


def _template():
    return json.loads(Path(amap_openshell.REPO, "examples", "fleet.json")
                      .read_text(encoding="utf-8"))


def test_a_new_fleet_gets_the_derived_domain(op, monkeypatch):
    _host(monkeypatch, "Host-1.local")
    out = op.ok("install", "--apply")
    doc = json.loads((op.home / "fleet.json").read_text())
    assert doc["fleet_domain"] == "openshell.host-1.internal"
    assert policy._fp.FLEET_DOMAIN_RE.match(doc["fleet_domain"])
    assert op.fleet()
    assert policy.addresses(op.fleet(), ["alpha"]) == {
        "alpha": "alpha@openshell.host-1.internal"}
    tpl = _template()
    assert list(doc) == list(tpl)
    assert ({k: v for k, v in doc.items() if k != "fleet_domain"}
            == {k: v for k, v in tpl.items() if k != "fleet_domain"})
    line = [ln for ln in out.splitlines() if ln.startswith(
        "new fleet.json: fleet_domain 'openshell.host-1.internal'")]
    assert line and "'agents.example.org'" in line[0]


@pytest.mark.parametrize("name", ["___", "", ".local", "--"])
def test_a_hostname_with_nothing_usable_gives_the_bare_runtime_domain(
        op, monkeypatch, name):
    _host(monkeypatch, name)
    op.ok("install", "--apply")
    domain = json.loads((op.home / "fleet.json").read_text())["fleet_domain"]
    assert domain == "openshell.internal"
    assert policy._fp.FLEET_DOMAIN_RE.match(domain)


def test_fleet_domain_base_sets_the_base(op, monkeypatch):
    _host(monkeypatch, "My_Box")
    op.ok("install", "--apply", "--fleet-domain-base", "agents.example.org")
    assert (json.loads((op.home / "fleet.json").read_text())["fleet_domain"]
            == "openshell.my-box.agents.example.org")


def test_a_bad_base_is_refused_and_nothing_is_written(op, monkeypatch, tmp_path):
    _host(monkeypatch, "Host-1")
    before = tree(tmp_path)
    code, out, err = op.run("install", "--apply", "--fleet-domain-base",
                            "Not_A.Domain")
    assert code == 1
    assert "fleet domain base" in err
    assert tree(tmp_path) == before and not op.home.exists()


def test_an_existing_fleet_json_is_byte_identical_after_install(op, monkeypatch):
    _host(monkeypatch, "Host-1")
    op.install()
    path = op.home / "fleet.json"
    doc = json.loads(path.read_text())
    doc["fleet_domain"] = "agents.example.org"
    path.write_text(json.dumps(doc, indent=8) + "\n")
    mine, mtime = path.read_bytes(), path.stat().st_mtime_ns
    _host(monkeypatch, "Other-Host")
    out = ""
    for argv in (("install",), ("install", "--apply"),
                 ("install", "--apply", "--fleet-domain-base", "b.example.org")):
        out = op.ok(*argv)
        assert path.read_bytes() == mine
        assert path.stat().st_mtime_ns == mtime
        assert any(ln.startswith("present fleet.json") for ln in out.splitlines())
    note = [ln for ln in out.splitlines() if ln.startswith(
        "note: --fleet-domain-base applies only to a new fleet.json")]
    assert note and "stays 'agents.example.org'" in note[0]


def test_install_dry_run_shows_the_domain_and_writes_nothing(
        op, monkeypatch, tmp_path):
    _host(monkeypatch, "Host-1")
    before = tree(tmp_path)
    code, out, err = op.run("install")
    assert code == 0
    assert "'openshell.host-1.internal'" in out
    assert tree(tmp_path) == before


def test_install_takes_the_derivation_from_sandys_fleet_policy(
        op, monkeypatch, tmp_path):
    calls = []

    def record(runtime, hostname, base):
        calls.append((runtime, hostname, base))
        return "x.example.org"

    monkeypatch.setattr(policy._fp, "derived_fleet_domain", record)
    _host(monkeypatch, "Host-1.local")
    op.ok("install", "--apply", "--fleet-domain-base", "b.example.org")
    assert calls == [("openshell", "Host-1.local", "b.example.org")]
    assert (json.loads((op.home / "fleet.json").read_text())["fleet_domain"]
            == "x.example.org")
    other = tmp_path / "home2"
    code, out, err = op.run("install", "--apply", home=other)
    assert code == 0, err
    assert calls[-1] == ("openshell", "Host-1.local",
                         policy._fp.DEFAULT_DOMAIN_BASE)


def test_plan_install_takes_the_hostname_it_is_given(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("the hostname must be passed in")

    monkeypatch.setattr(socket, "gethostname", boom)
    h = verbs.host_args(argparse.Namespace(
        home=str(tmp_path / "home"), run_as=RUN_AS, image=IMAGE,
        restart_policy=False, openshell="openshell", fleet=None),
        env=os.environ)
    plan = verbs.plan_install(h, hostname="Host-3")
    (data,) = [d for p, d, m in plan.files if p.endswith("/fleet.json")]
    assert json.loads(data)["fleet_domain"] == "openshell.host-3.internal"


def test_a_new_fleet_json_beside_recorded_members_renders_router_json_with_its_domain(
        full, monkeypatch):
    (full.home / "fleet.json").unlink()
    _host(monkeypatch, "Host-2")
    full.ok("install", "--apply")
    router = json.loads((full.home / "router.json").read_text())
    fleet = json.loads((full.home / "fleet.json").read_text())
    assert router["fleet_domain"] == fleet["fleet_domain"] \
        == "openshell.host-2.internal"


def test_install_records_the_home_and_teardown_forgets_it(op):
    import home_record
    dry = op.ok("install")
    assert "would record home" in dry and home_record.read() is None
    op.install()
    assert home_record.read() == str(op.home)
    op.ok("teardown", "--apply")
    assert home_record.read() is None


def test_teardown_leaves_a_record_of_another_home(op):
    import home_record
    op.install()
    home_record.write("/srv/another-home")
    op.ok("teardown", "--apply")
    assert home_record.read() == "/srv/another-home"
