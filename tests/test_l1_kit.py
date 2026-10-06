"""The `l1-kit` verb: `prepare` and `record`.

Facts relied on, at OpenShell main@acbac9c: a bind mount whose source does not
exist is refused (crates/openshell-driver-docker/src/lib.rs:3753-3761), so
`prepare` must have made every mount source; and `sandbox get --output json` has
`id`, `name` and `workspace` (crates/openshell-cli/src/run.rs:2789-2791,
docs/how-it-works/sandboxes/overview.mdx:603-607), which is where an ID comes from.

Facts relied on in amap-router-local: `router/config.py:538-588` (the verdict the
loader reads), `router/config.py:817-857` (discovery), and `docker/derive-mounts.py`
(a `state_dir` that does not exist is refused, so `record` makes `router-state/`
with `router.json`).

The router's own loader is the oracle for `router.json` and `selected.json`
(`test_router_config.router_view`, in a subprocess). Lane names come from the
router through `_amap_main.router_facts`, not from a literal. Nothing here runs
OpenShell, Docker or the router.
"""

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
from pathlib import Path

import pytest

import _amap_main
import _workspace
import amap_openshell
import l1_kit
import membership
import policy
import provider_profile
import render
import router_config
import test_render
import test_router_config
from membership import Member

REPO = Path(__file__).absolute().parents[1]
FLEET_FILE = REPO / "examples" / "fleet.json"
RUN_AS = "1234:5678"
IMAGE = "amap-openshell-agent:test"
IDS = {"alpha": "id-alpha-0001", "beta": "id-beta-0002"}
WS = "default"


def kit(capsys, *argv):
    capsys.readouterr()  # drop what an earlier call left behind
    code = amap_openshell.main(["l1-kit", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree(root):
    """`(relpath, kind, mode, sha256 or link target)`, sorted. Content and mode
    are part of the answer."""
    out = []
    for d, dirs, files in os.walk(str(root)):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, str(root))
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                out.append((rel, "link", mode, os.readlink(p)))
            elif stat.S_ISDIR(st.st_mode):
                out.append((rel, "dir", mode, ""))
            else:
                with open(p, "rb") as fh:
                    out.append((rel, "file", mode,
                                hashlib.sha256(fh.read()).hexdigest()))
    return sorted(out)


def prepare_args(home, *extra, fleet=FLEET_FILE, run_as=RUN_AS, image=IMAGE):
    return ["prepare", "--home", str(home), "--fleet", str(fleet),
            "--run-as", run_as, "--image", image, *extra]


def prepared(tmp_path, *extra):
    home = tmp_path / "home"
    code = amap_openshell.main(["l1-kit", *prepare_args(home, "--apply", *extra)])
    assert code == 0
    return str(home)


def record_args(home, name, *extra):
    return ["record", "--home", str(home), name, IDS.get(name, "id-" + name),
            *extra]


PATHS = ["/usr/local/bin/node", "/opt/claude/cli.js"]
PROFILE_REL = "providers/" + provider_profile.PROFILE_NAME


def profile_args(home, *binaries, extra=()):
    return ["profile", "--home", str(home),
            *[w for b in binaries for w in ("--binary", b)], *extra]


def host(home):
    return render.Host(str(home), IMAGE, render.parse_run_as(RUN_AS), False)


def reported(out):
    """`(state, relpath)` for every path line."""
    rows = []
    for line in out.splitlines():
        m = re.match(r"(would create|would update|created|updated|present) "
                     r"(.+)\Z", line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


# --- AC1: no writes without --apply -------------------------------------------

def test_neither_action_writes_without_apply(tmp_path, capsys):
    absent = tmp_path / "absent"
    before = tree(tmp_path)
    code, out, err = kit(capsys, *prepare_args(absent))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert not absent.exists()
    assert "would create payload/amap-main" in out.splitlines()
    assert "would create commands/create-alpha.sh" in out.splitlines()
    assert out.splitlines()[-1] == \
        "dry run: nothing was written; add --apply to write"

    empty = tmp_path / "empty"
    empty.mkdir()
    before = tree(tmp_path)
    code, out, err = kit(capsys, *prepare_args(empty))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert "would create payload/amap-main" in out.splitlines()

    home = prepared(tmp_path)
    before = tree(tmp_path)
    code, out, err = kit(capsys, *prepare_args(home, image="another:image"))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert "would update commands/create-alpha.sh" in out.splitlines()

    code, out, err = kit(capsys, *record_args(home, "alpha"))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert "would create membership.json" in out.splitlines()

    assert kit(capsys, *record_args(home, "alpha", "--apply"))[0] == 0
    before = tree(tmp_path)
    code, out, err = kit(capsys, *record_args(home, "beta"))
    assert code == 0, err
    assert tree(tmp_path) == before
    lines = out.splitlines()
    assert "would create selected.json" in lines
    assert "would create router.json" in lines
    assert not os.path.exists(os.path.join(home, "selected.json"))
    assert not os.path.exists(os.path.join(home, "router.json"))


# --- AC2: prepare -------------------------------------------------------------

def test_prepare_copies_the_payload_byte_for_byte(tmp_path):
    home = prepared(tmp_path)
    sources = l1_kit.payload_sources(_workspace.CONNECTOR_ROOT)
    under = (REPO / "payload", _workspace.CONNECTOR_ROOT / "bin")
    for s in sources:
        written = Path(home, "payload", s.rel)
        assert written.read_bytes() == s.path.read_bytes(), s.rel
        assert any(u in s.path.parents for u in under), s.path
        assert s.executable == bool(s.path.stat().st_mode & 0o111), s.rel
        if s.executable:
            assert os.access(written, os.X_OK), s.rel
        else:
            assert not os.access(written, os.X_OK), s.rel
    on_disk = {p.relative_to(Path(home, "payload")).as_posix()
               for p in Path(home, "payload").rglob("*") if p.is_file()}
    assert on_disk == {s.rel for s in sources}
    assert Path(home, "payload", "INBOX-POLICY.md").read_bytes() == \
        (REPO / "payload" / "INBOX-POLICY.md").read_bytes()


def test_the_payload_holds_every_file_the_sandbox_names(tmp_path):
    home = prepared(tmp_path)
    expected = set()
    wrapper = (REPO / "payload" / "amap-main").read_text(encoding="utf-8")
    expected |= set(re.findall(r"\$SELF_DIR/([\w.-]+)", wrapper))
    servers = json.loads((REPO / "payload" / "mcp-servers.json").read_text())
    prefix = render.PAYLOAD_TARGET + "/"
    for server in servers["mcpServers"].values():
        assert server["command"].startswith(prefix), server
        expected.add(server["command"][len(prefix):])
    for word in render.main_process():
        if word.startswith(prefix):
            expected.add(word[len(prefix):])
    settings = json.loads((REPO / "payload" / render.SETTINGS_NAME).read_text())
    for group in settings["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            assert hook["command"].startswith(prefix), hook
            expected.add(hook["command"][len(prefix):])
    daemon = (_workspace.CONNECTOR_ROOT / "bin" /
              "inbox-delivery").read_text(encoding="utf-8")
    found = re.findall(r'os\.path\.join\(here, "([^"]+)"\)', daemon)
    assert found, "the daemon no longer loads a file from beside itself"
    expected |= set(found)
    assert expected
    for rel in sorted(expected):
        assert Path(home, "payload", rel).is_file(), rel


def members_of(home):
    return policy.named_instances(
        policy.load_fleet(os.path.join(home, "fleet.json")))


def test_every_mount_source_in_every_create_command_exists(tmp_path):
    home = prepared(tmp_path)
    assert members_of(home) == ["alpha", "beta"]
    for n in members_of(home):
        text = Path(home, "commands", f"create-{n}.sh").read_text()
        assert text.endswith("\n") and text.count("\n") == 1, n
        argv = shlex.split(text)
        assert argv[0] == "openshell"
        config = json.loads(argv[argv.index("--driver-config-json") + 1])
        mounts = config["docker"]["mounts"]
        assert mounts
        for m in mounts:
            assert os.path.isdir(m["source"]), m
            assert not os.path.islink(m["source"]), m
        policy_path = argv[argv.index("--policy") + 1]
        assert policy_path == os.path.join(home, "policies", f"{n}.yaml")
        assert os.path.isfile(policy_path)


def test_the_written_create_commands_are_unattended(tmp_path):
    """OpenShell main@acbac9c:crates/openshell-cli/src/main.rs:1523-1555."""
    home = prepared(tmp_path)
    names = members_of(home)
    assert names
    for n in names:
        argv = shlex.split(
            Path(home, "commands", f"create-{n}.sh").read_text())
        cut = argv.index("--")
        for flag in render.UNATTENDED_FLAGS:
            assert argv[:cut].count(flag) == 1, (n, flag)
        tail = argv[cut + 1:]
        assert tail[0] == f"{render.PAYLOAD_TARGET}/{render.MAIN_PROCESS_NAME}"
        assert tail[tail.index(render.SETTINGS_FLAG) + 1] == \
            f"{render.PAYLOAD_TARGET}/{render.SETTINGS_NAME}"
        assert render.BYPASS_PERMISSIONS_FLAG in tail[1:]


def test_every_written_policy_and_command_passes_s4s_checks(tmp_path):
    home = prepared(tmp_path)
    fleet = policy.load_fleet(FLEET_FILE)
    h = host(home)
    for n in members_of(home):
        policy_path = os.path.join(home, "policies", f"{n}.yaml")
        r = render.render_member(fleet, n, h, policy_path)
        file_text = Path(policy_path).read_text()
        cmd = Path(home, "commands", f"create-{n}.sh").read_text()
        assert file_text == r.policy_yaml
        assert cmd == shlex.join(r.argv) + "\n"
        it = render.Rendering(n, policy.workspace_of(fleet),
                              policy.addresses(fleet, [n])[n],
                              render.mount_table(h, n),
                              test_render.read_yaml_subset(file_text),
                              file_text, shlex.split(cmd))
        assert render.rendering_problems(it, h) == [], n
        assert test_render.argv_values(shlex.split(cmd), "--provider") == [
            provider_profile.PROFILE_ID]


# --- AC3: record --------------------------------------------------------------

def test_record_writes_the_verdict_and_router_json_only_when_every_member_is_recorded(
        tmp_path, capsys):
    home = prepared(tmp_path)
    for name in ("membership.json", "selected.json", "router.json"):
        assert not os.path.exists(os.path.join(home, name))

    assert kit(capsys, *record_args(home, "alpha", "--apply"))[0] == 0
    assert membership.load(os.path.join(home, "membership.json")) == \
        [Member(WS, "alpha", IDS["alpha"])]
    assert not os.path.exists(os.path.join(home, "selected.json"))
    assert not os.path.exists(os.path.join(home, "router.json"))

    assert kit(capsys, *record_args(home, "beta", "--apply"))[0] == 0
    router_json = os.path.join(home, "router.json")
    selected = os.path.join(home, "selected.json")
    assert os.path.isfile(router_json) and os.path.isfile(selected)
    assert os.path.isdir(os.path.join(home, "router-state"))
    assert stat.S_IMODE(os.stat(os.path.join(home, "router-state")).st_mode) \
        == 0o700

    view = test_router_config.router_view(router_json)
    assert "config_error" not in view, view
    assert view["instances"] == ["alpha", "beta"]
    assert view["no_verdict"] == []
    assert view["verdict_without_directory"] == []
    assert view["verdict_unavailable"] is False
    assert view["clean"] is True
    assert set(view["presence"].values()) == {"ok"}, view["presence"]
    with open(router_json, encoding="utf-8") as fh:
        router_config.loader_check(json.load(fh))

    recorded = membership.load(os.path.join(home, "membership.json"))
    assert {m.name: m.id for m in recorded} == IDS
    verdict_text = Path(selected).read_text()
    for sandbox_id in IDS.values():
        assert sandbox_id not in verdict_text


def test_record_is_idempotent_and_refuses_what_it_cannot_record(tmp_path, capsys):
    home = prepared(tmp_path)
    for n in ("alpha", "beta"):
        assert kit(capsys, *record_args(home, n, "--apply"))[0] == 0

    before = tree(tmp_path)
    code, out, err = kit(capsys, *record_args(home, "alpha", "--apply"))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert all(state == "present" for state, _ in reported(out)), out

    def refused(argv, reason):
        snapshot = tree(tmp_path)
        code, out, err = kit(capsys, *argv)
        assert code == 1, (argv, out, err)
        assert reason in err, (reason, err)
        assert "l1-kit record" in err
        assert tree(tmp_path) == snapshot

    refused(["record", "--home", home, "alpha", "id-a-different-one",
             "--apply"], "ID mismatch")
    refused(["record", "--home", home, "gamma", "id-gamma", "--apply"],
            "not a member")
    refused(["record", "--home", home, "beta", "has white space", "--apply"],
            "whitespace")

    bare = tmp_path / "bare"
    bare.mkdir()
    refused(["record", "--home", str(bare), "alpha", "id-x", "--apply"],
            "fleet.json")

    shutil.rmtree(os.path.join(home, "instances", "alpha", "outbox",
                               "processed"))
    refused(["record", "--home", home, "alpha", IDS["alpha"], "--apply"],
            "no lanes")


def test_record_refuses_a_membership_entry_the_fleet_does_not_name(tmp_path,
                                                                   capsys):
    home = prepared(tmp_path)
    Path(home, "membership.json").write_text(membership.dump(
        [Member(WS, "gamma", "id-gamma-0003")]))
    before = tree(tmp_path)
    code, _, err = kit(capsys, *record_args(home, "alpha", "--apply"))
    assert code == 1
    assert "gamma" in err
    assert tree(tmp_path) == before


def test_a_stale_verdict_is_warned_about_and_never_deleted(tmp_path, capsys):
    home = prepared(tmp_path)
    Path(home, "selected.json").write_text("{}\n")
    before = tree(tmp_path)
    code, out, err = kit(capsys, *record_args(home, "alpha"))
    assert code == 0, err
    assert any(line.startswith("warning: selected.json exists")
               for line in out.splitlines()), out
    assert tree(tmp_path) == before
    assert kit(capsys, *record_args(home, "alpha", "--apply"))[0] == 0
    assert os.path.exists(os.path.join(home, "selected.json"))


# --- prepare, the rest --------------------------------------------------------

def test_prepare_is_idempotent(tmp_path, capsys):
    home = prepared(tmp_path)
    before = tree(tmp_path)
    code, out, err = kit(capsys, *prepare_args(home, "--apply"))
    assert code == 0, err
    rows = reported(out)
    assert rows and all(state == "present" for state, _ in rows), out
    assert [line for line in out.splitlines()
            if not line.startswith("present ")] == []
    assert tree(tmp_path) == before


def test_a_changed_file_or_mode_is_updated_and_reported(tmp_path, capsys):
    home = prepared(tmp_path)
    wrapper = os.path.join(home, "payload", "amap-main")
    os.chmod(wrapper, 0o644)
    Path(home, "gateway-fragment.toml").write_text("stale\n")
    code, out, _ = kit(capsys, *prepare_args(home))
    assert code == 0
    assert ("would update", "payload/amap-main") in reported(out)
    assert ("would update", "gateway-fragment.toml") in reported(out)
    code, out, _ = kit(capsys, *prepare_args(home, "--apply"))
    assert code == 0
    assert ("updated", "payload/amap-main") in reported(out)
    assert stat.S_IMODE(os.stat(wrapper).st_mode) == 0o755
    assert Path(home, "gateway-fragment.toml").read_text() == \
        l1_kit.GATEWAY_FRAGMENT
    assert not [p for p in Path(home).rglob(".*.tmp")]


def test_restart_policy_only_when_asked(tmp_path):
    home = prepared(tmp_path)
    assert "--restart-policy" not in Path(
        home, "commands", "create-alpha.sh").read_text()
    other = tmp_path / "other"
    other.mkdir()
    home2 = prepared(other, "--restart-policy")
    assert "--restart-policy on-failure" in Path(
        home2, "commands", "create-alpha.sh").read_text()


def test_a_symlink_or_wrong_type_in_the_way_is_refused(tmp_path, capsys):
    home = prepared(tmp_path)
    target = os.path.join(home, "commands", "create-alpha.sh")
    os.unlink(target)
    os.symlink(os.path.join(home, "fleet.json"), target)
    before = tree(tmp_path)
    code, _, err = kit(capsys, *prepare_args(home, "--apply"))
    assert code == 1 and "symlink" in err
    assert tree(tmp_path) == before

    os.unlink(target)
    os.mkdir(target)
    before = tree(tmp_path)
    code, _, err = kit(capsys, *prepare_args(home, "--apply"))
    assert code == 1 and "not a regular file" in err
    assert tree(tmp_path) == before

    os.rmdir(target)
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    os.symlink(str(real), str(link))
    code, _, err = kit(capsys, *prepare_args(link, "--apply"))
    assert code == 1 and "symlink" in err


def test_a_missing_connector_file_fails_loudly_and_writes_nothing(
        tmp_path, capsys, monkeypatch):
    conn = tmp_path / "conn"
    shutil.copytree(_workspace.CONNECTOR_ROOT / "bin", conn / "bin",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (conn / "bin" / "_inboxlib.py").unlink()
    (conn / "bin" / "inbox-submit").unlink()
    monkeypatch.setenv("AMAP_CONNECTOR_REPO", str(conn))
    before = tree(tmp_path)
    code, out, err = kit(capsys, *prepare_args(tmp_path / "home", "--apply"))
    assert code == 1
    assert "_inboxlib.py" in err and "inbox-submit" in err, err
    assert out == ""
    assert tree(tmp_path) == before
    assert not (tmp_path / "home").exists()


def test_find_connector_follows_the_routers_rule(tmp_path):
    assert l1_kit.find_connector() == _workspace.CONNECTOR_ROOT
    good = tmp_path / "good"
    (good / "bin").mkdir(parents=True)
    (good / "bin" / "inbox-delivery").write_text("")
    env = {"AMAP_CONNECTOR_REPO": str(good)}
    assert l1_kit.find_connector(env) == good
    with pytest.raises(l1_kit.KitError, match="only place searched"):
        l1_kit.find_connector({"AMAP_CONNECTOR_REPO": str(tmp_path / "none")})
    # An empty variable counts as unset.
    assert l1_kit.find_connector({"AMAP_CONNECTOR_REPO": ""}) == \
        _workspace.CONNECTOR_ROOT
    # A directory with the right name and no confirming file is skipped.
    beside = tmp_path / "amap-connector-claude"
    beside.mkdir()
    with pytest.raises(l1_kit.KitError, match="cannot find"):
        l1_kit.find_connector({}, start=tmp_path)
    (beside / "bin").mkdir()
    (beside / "bin" / "inbox-delivery").write_text("")
    assert l1_kit.find_connector({}, start=tmp_path / "deeper") == beside


def test_a_newline_in_a_create_argument_is_refused():
    with pytest.raises(l1_kit.KitError, match="newline"):
        l1_kit.command_text(["openshell", "a\nb"])
    assert l1_kit.command_text(["openshell", "a b"]) == "openshell 'a b'\n"


# --- constraints --------------------------------------------------------------

def test_a_home_overlapping_a_repo_is_refused(tmp_path, capsys):
    link = tmp_path / "link"
    os.symlink(str(REPO), str(link))
    homes = [REPO, REPO / "sub", REPO.parent,
             _workspace.ROUTER_ROOT / "sub", _workspace.CONNECTOR_ROOT,
             _workspace.SANDY_ROOT, link, link / "sub"]
    for h in homes:
        before = tree(tmp_path)
        code, out, err = kit(capsys, *prepare_args(h))  # a dry run only
        assert code == 1, h
        assert "overlaps" in err, (h, err)
        assert out == ""
        assert tree(tmp_path) == before
    code, _, err = kit(capsys, "record", "--home", str(REPO), "alpha", "id-x")
    assert code == 1 and "overlaps" in err


def test_home_overlap_unit(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (tmp_path / "a2").mkdir()
    repos = {"fake": a}
    assert l1_kit.home_overlap(str(a), repos)
    assert l1_kit.home_overlap(str(a / "x" / "y"), repos)
    assert l1_kit.home_overlap(str(tmp_path), repos)
    assert "fake" in l1_kit.home_overlap(str(a), repos)
    assert l1_kit.home_overlap(str(tmp_path / "a2"), repos) is None
    assert l1_kit.home_overlap(str(tmp_path / "b"), repos) is None
    link = tmp_path / "link"
    os.symlink(str(a), str(link))
    assert l1_kit.home_overlap(str(link), repos)
    # a repo reached through a link is compared by its real path too
    assert l1_kit.home_overlap(str(a), {"fake": link})
    assert l1_kit.home_overlap(str(tmp_path / "a2"), {}) is None


def test_the_overlap_refusal_holds_with_apply(tmp_path, capsys, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir()
    monkeypatch.setattr(l1_kit, "known_repos", lambda: {"fake": fake})
    before = tree(tmp_path)
    code, _, err = kit(capsys, *prepare_args(fake / "home", "--apply"))
    assert code == 1 and "overlaps" in err
    assert tree(tmp_path) == before
    assert not (fake / "home").exists()


def test_bad_arguments_are_refused_and_write_nothing(tmp_path, capsys,
                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)
    nobody = tmp_path / "nobody.json"
    doc = json.loads(FLEET_FILE.read_text())
    doc["task_graph"] = {}
    nobody.write_text(json.dumps(doc))
    home = tmp_path / "home"
    cases = [
        prepare_args("rel/home", "--apply"),
        prepare_args(home, "--apply", run_as="0:0"),
        prepare_args(home, "--apply", run_as="abc"),
        prepare_args(home, "--apply", image=""),
        prepare_args(home, "--apply", image="an image"),
        prepare_args(home, "--apply", fleet=tmp_path / "absent.json"),
        prepare_args(home, "--apply", fleet=nobody),
    ]
    for argv in cases:
        before = tree(tmp_path)
        code, out, err = kit(capsys, *argv)
        assert code == 1, (argv, out, err)
        assert err.startswith("amap-openshell.py l1-kit prepare: "), err
        assert tree(tmp_path) == before, argv
    assert not home.exists()


def test_every_path_the_kit_writes_is_under_home(tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_text("m")
    before = tree(outside)
    home = tmp_path / "home"
    reports = []
    for argv in (prepare_args(home, "--apply"),
                 profile_args(home, *PATHS, extra=("--apply",)),
                 record_args(home, "alpha", "--apply"),
                 record_args(home, "beta", "--apply")):
        code, out, err = kit(capsys, *argv)
        assert code == 0, err
        reports.append(out)
    assert tree(outside) == before
    assert sorted(os.listdir(tmp_path)) == ["home", "outside"]
    for out in reports:
        for _, rel in reported(out):
            assert not os.path.isabs(rel), rel
            assert ".." not in Path(rel).parts, rel
            assert os.path.lexists(home / rel), rel


def test_profile_renders_the_profile_under_home(tmp_path, capsys):
    home = prepared(tmp_path)
    before = tree(tmp_path)
    code, out, err = kit(capsys, *profile_args(home, *PATHS))
    assert code == 0, err
    assert tree(tmp_path) == before
    assert f"would create {PROFILE_REL}" in out
    code, out, err = kit(capsys, *profile_args(home, *PATHS,
                                               extra=("--apply",)))
    assert code == 0, err
    target = Path(home, PROFILE_REL)
    want = provider_profile.profile_text(provider_profile.render_profile(
        provider_profile.load_template(), PATHS))
    assert target.read_text(encoding="utf-8") == want
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert json.loads(want)["binaries"] == PATHS
    code, out, err = kit(capsys, *profile_args(home, *PATHS,
                                               extra=("--apply",)))
    assert f"present {PROFILE_REL}" in out
    code, out, err = kit(capsys, *profile_args(home, "/other/node"))
    assert f"would update {PROFILE_REL}" in out


def test_profile_refuses_what_it_cannot_render(tmp_path, capsys):
    home = prepared(tmp_path)
    with pytest.raises(SystemExit):
        amap_openshell.main(["l1-kit", "profile", "--home", home])
    before = tree(tmp_path)
    code, out, err = kit(capsys, *profile_args(home, "relative/node",
                                               extra=("--apply",)))
    assert code == 1
    assert err.startswith("amap-openshell.py l1-kit profile: "), err
    assert tree(tmp_path) == before
    code, out, err = kit(capsys, *profile_args(tmp_path / "nohome", *PATHS,
                                               extra=("--apply",)))
    assert code == 1
    assert "run `l1-kit prepare --apply` first" in err
    assert not (tmp_path / "nohome").exists()


def test_the_kit_never_runs_a_program_or_invents_an_id():
    tree_ = ast.parse((REPO / "l1_kit.py").read_text(encoding="utf-8"))
    banned = {"subprocess", "uuid", "random", "secrets"}
    for node in ast.walk(tree_):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            top = name.split(".")[0]
            assert top not in banned, name
            assert top != "router", name
    for node in ast.walk(tree_):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "os":
            assert not re.match(r"(system|popen|exec|spawn|posix_spawn)",
                                node.attr), node.attr
    facts = _amap_main.router_facts()
    lane_words = set(facts["lanes"])
    for leaves in facts["leaves"].values():
        lane_words |= set(leaves)
    assert lane_words
    for node in ast.walk(tree_):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in lane_words, node.value


def test_the_gateway_fragment_is_design_section_6s(tmp_path):
    design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    section = design.split("## 6. Gateway configuration", 1)[1] \
        .split("\n## 7.", 1)[0]
    block = re.search(r"```toml\n(.*?)```", section, re.S).group(1)

    def lines(text):
        out = []
        for raw in text.splitlines():
            line = " ".join(raw.split("#", 1)[0].split())
            if line:
                out.append(line)
        return out

    want = lines(block)
    assert want and "enable_bind_mounts = true" in want
    home = prepared(tmp_path)
    written = Path(home, "gateway-fragment.toml").read_text()
    assert written == l1_kit.GATEWAY_FRAGMENT
    assert lines(written) == want
    assert lines(l1_kit.GATEWAY_FRAGMENT) == want


def test_the_cli_constants_agree():
    assert l1_kit.PROG == amap_openshell.PROG
    assert l1_kit.EXIT_REFUSED == amap_openshell.EXIT_REFUSED == 1


# --- removals and the router-file helpers --------------------------------------

def _removal_plan(home, *removals):
    return l1_kit.Plan(str(home), (), (), (), (), tuple(removals))


def test_a_plan_carries_removals(tmp_path):
    home = tmp_path / "home"
    (home / "tree" / "deep").mkdir(parents=True)
    (home / "tree" / "deep" / "f").write_text("x")
    (home / "keep" / "sub").mkdir(parents=True)
    (home / "keep" / "a").write_text("x")
    (home / "keep" / "sub" / "b").write_text("x")
    (home / "outside").mkdir()
    (home / "outside" / "precious").write_text("x")
    os.symlink(str(home / "outside"), str(home / "keep" / "link"))
    (home / "one").write_text("x")
    plan = _removal_plan(home,
                         l1_kit.Removal(str(home / "one"), "file"),
                         l1_kit.Removal(str(home / "tree"), "tree"),
                         l1_kit.Removal(str(home / "keep"), "contents"),
                         l1_kit.Removal(str(home / "gone"), "tree"))
    before = tree(tmp_path)
    assert l1_kit.carry_out(plan, apply=False) == [
        "would remove one", "would remove tree", "would remove keep",
        "absent gone"]
    assert tree(tmp_path) == before
    assert l1_kit.carry_out(plan, apply=True) == [
        "removed one", "removed tree", "removed keep", "absent gone"]
    assert not (home / "one").exists() and not (home / "tree").exists()
    assert (home / "keep").is_dir() and not list((home / "keep").iterdir())
    assert (home / "outside" / "precious").read_text() == "x"  # never followed

    os.symlink(str(home / "outside"), str(home / "linked"))
    bad = _removal_plan(home, l1_kit.Removal(str(home / "outside" / "precious"),
                                             "file"),
                        l1_kit.Removal(str(home / "linked"), "tree"))
    before = tree(tmp_path)
    for apply in (False, True):
        with pytest.raises(l1_kit.KitError, match="symlink"):
            l1_kit.carry_out(bad, apply=apply)
        assert tree(tmp_path) == before  # refused before anything was removed


def test_router_files_and_missing_members(tmp_path):
    fleet = policy.load_fleet(str(FLEET_FILE))
    alpha = Member(WS, "alpha", IDS["alpha"])
    beta = Member(WS, "beta", IDS["beta"])
    assert l1_kit.missing_members(fleet, []) == ["alpha", "beta"]
    assert l1_kit.missing_members(fleet, [beta]) == ["alpha"]
    assert l1_kit.missing_members(fleet, [alpha, beta]) == []
    home = str(tmp_path / "home")
    with pytest.raises(router_config.RouterConfigError):
        l1_kit.router_files(home, fleet, [alpha])
    dirs, files, warnings = l1_kit.router_files(home, fleet, [alpha, beta])
    rendered = router_config.render_router_json(fleet, [alpha, beta], home)
    assert dirs == [(router_config.default_state_dir(home),
                     l1_kit.STATE_DIR_MODE)]
    assert dict((p, d) for p, d, _ in files)[
        router_config.router_json_path(home)] == rendered.text.encode()
    assert warnings == list(rendered.warnings)
