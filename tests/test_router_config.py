"""The router's configuration: `router.json`, the verdict file, lane creation and
drift.

The acceptance checks run the router's own loader on the written files, in a
subprocess (`_ORACLE`), and read what its discovery report and its presence check
say. `router.provision` and `router.roster` are INTERNAL router modules
(amap-router-local `router/__init__.py:182`): they are used here only as those
oracles, in a subprocess, and never by the shipped code. Nothing here runs the
router (`python -m router`), OpenShell or Docker.

Facts relied on:

- OpenShell main@acbac9c:`crates/openshell-driver-docker/src/lib.rs:3753-3761`
  and `tests.rs:2052-2075`: a bind mount whose source does not exist is refused,
  which is why the lanes exist before the sandbox does.
- amap-router-local at 9854a5e: `router/config.py:538-588` (the verdict format
  the loader reads), `router/config.py:817-857` (discovery), `router/config.py:
  835-844` (state_dir must not be nested with instances_dir or selected_json),
  `router/config.py:1232-1241` (a pair on both lanes is refused),
  `router/provision.py:154-200` (the discovery presence check) and
  `router/roster.py:112,125-132` (the roster directory).

The lane names and leaves in the expectations come from the router through a
subprocess, independent of `router_config`'s own import.
"""

import ast
import itertools
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

import _amap_main
import _workspace
import membership
import policy
import render
import router_config
import router_link
from membership import Member
from router_config import RouterConfigError

REPO = Path(__file__).absolute().parents[1]
FLEET = policy.load_fleet(REPO / "examples" / "fleet.json")
WS = policy.workspace_of(FLEET)
F = _amap_main.router_facts()
RC = router_link.router_config()
M = [Member(WS, "alpha", "id-alpha-0001"), Member(WS, "beta", "id-beta-0002")]

_variant_counter = itertools.count()


def make_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


def fleet_variant(tmp_path, **over):
    doc = json.loads((REPO / "examples" / "fleet.json").read_text())
    doc.update(over)
    d = tmp_path / "variants"
    d.mkdir(exist_ok=True)
    path = d / f"fleet-{next(_variant_counter)}.json"
    path.write_text(json.dumps(doc))
    return policy.load_fleet(path)


def recorded(home, members):
    path = os.path.join(home, "membership.json")
    membership.write(path, members)
    return membership.load(path)


def bring_up(home, members, fleet=FLEET):
    for m in members:
        router_config.create_lanes(home, m.name)
    loaded = recorded(home, members)
    router_config.write_verdict(
        home, router_config.render_verdict(loaded, policy.workspace_of(fleet)))
    rendered = router_config.render_router_json(fleet, loaded, home)
    Path(router_config.router_json_path(home)).write_text(rendered.text)
    return rendered


def snapshot(root):
    out = []
    for d, dirs, files in os.walk(str(root)):
        for n in dirs + files:
            p = os.path.join(d, n)
            link = os.path.islink(p)
            out.append((os.path.relpath(p, str(root)),
                        os.path.isdir(p) and not link, link))
    return sorted(out)


def is_or_under(a, b):
    a, b = PurePosixPath(a), PurePosixPath(b)
    return a == b or b in a.parents


def overlap(a, b):
    return is_or_under(a, b) or is_or_under(b, a)


_ORACLE = """
import json, sys
from router import config, provision, roster

try:
    cfg = config.load(sys.argv[1])
except config.ConfigError as e:
    print(json.dumps({"config_error": str(e)}))
    sys.exit(0)

d = cfg.discovery
presence = {}
for name in sorted(cfg.instances):
    try:
        provision.provision(cfg, name)
        presence[name] = "ok"
    except provision.ProvisionError as e:
        presence[name] = "refused: " + str(e)
rd = roster.roster_dir(cfg)
print(json.dumps({
    "instances": sorted(cfg.instances),
    "skipped": dict(d.skipped),
    "no_verdict": sorted(d.no_verdict),
    "verdict_without_directory": sorted(d.verdict_without_directory),
    "verdict_unavailable": d.verdict_unavailable,
    "inert_edges": dict(d.inert_edges),
    "clean": d.is_clean(),
    "roots": {n: str(i.root) for n, i in cfg.instances.items()},
    "peer_senders": {n: sorted(i.peer_senders) for n, i in cfg.instances.items()},
    "peers": {n: sorted(i.peers) for n, i in cfg.instances.items()},
    "fleet_domain": cfg.fleet_domain,
    "selected_json": str(cfg.selected_json),
    "state_dir": str(cfg.state_dir),
    "roster_dir": str(rd) if rd is not None else None,
    "presence": presence,
}))
"""


def router_view(config_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_workspace.ROUTER_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    p = subprocess.run([sys.executable, "-c", _ORACLE, str(config_path)],
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# --- acceptance: the router's loader ------------------------------------------

def test_the_routers_loader_accepts_the_example_and_admits_exactly_the_members(
        tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    view = router_view(router_config.router_json_path(home))
    assert "config_error" not in view, view
    loaded = membership.load(os.path.join(home, "membership.json"))
    assert view["instances"] == [m.name for m in loaded] == ["alpha", "beta"]
    assert view["no_verdict"] == []
    assert view["verdict_without_directory"] == []
    assert view["verdict_unavailable"] is False
    assert view["skipped"] == {}
    assert view["inert_edges"] == {}
    assert view["clean"] is True
    assert view["peer_senders"] == {"alpha": [], "beta": ["alpha"]}
    assert view["fleet_domain"] == FLEET["fleet_domain"]
    assert view["state_dir"] == router_config.default_state_dir(home)


def test_the_discovery_report_is_live_not_vacuous(tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    path = router_config.router_json_path(home)
    router_config.create_lanes(home, "gamma")  # a directory nobody admitted
    assert router_view(path)["no_verdict"] == ["gamma"]
    more = [*M, Member(WS, "delta", "id-delta")]  # admitted, with no directory
    router_config.write_verdict(home, router_config.render_verdict(more, WS))
    view = router_view(path)
    assert view["verdict_without_directory"] == ["delta"]
    assert view["clean"] is False


def test_the_rendering_is_checked_by_the_routers_own_loader(tmp_path,
                                                            monkeypatch):
    home = make_home(tmp_path)
    doc = router_config.render_router_json(FLEET, M, home).doc

    calls = []
    with monkeypatch.context() as mp:
        mp.setattr(router_config, "loader_check",
                   lambda d: calls.append(d))
        rendered = router_config.render_router_json(FLEET, M, home)
    assert calls == [rendered.doc]

    def refuse(d):
        raise RouterConfigError("refused by the spy")
    with monkeypatch.context() as mp:
        mp.setattr(router_config, "loader_check", refuse)
        with pytest.raises(RouterConfigError, match="spy"):
            router_config.render_router_json(FLEET, M, home)

    with pytest.raises(RouterConfigError) as e:
        router_config.loader_check({**doc, "bogus": 1})
    assert "bogus" in str(e.value)
    assert router_config.loader_check(doc).selected_json == \
        Path(render.selected_json(home))


def test_the_loader_check_writes_no_bytecode_into_the_router(tmp_path):
    t, r = tmp_path / "T", tmp_path / "R"
    t.mkdir()
    for name in ("router_config", "render", "policy", "membership",
                 "router_link", "provider_profile"):
        shutil.copy(REPO / f"{name}.py", t / f"{name}.py")
    shutil.copytree(_workspace.ROUTER_ROOT / "router", r / "router",
                    ignore=shutil.ignore_patterns("__pycache__"))
    doc = {"state_dir": str(tmp_path / "s"), "instances_dir": str(tmp_path / "i"),
           "selected_json": str(tmp_path / "sel.json"),
           "fleet_domain": "agents.example.org"}
    code = ("import json, router_config; "
            "router_config.loader_check(json.loads(%r))" % json.dumps(doc))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPATH")}
    env["PYTHONPATH"] = str(t)
    env["AMAP_ROUTER_REPO"] = str(r)
    env["AMAP_SANDY_REPO"] = str(_workspace.SANDY_ROOT)
    p = subprocess.run([sys.executable, "-c", code], cwd=str(t), env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert not list(r.rglob("__pycache__"))


# --- rendering ---------------------------------------------------------------

def test_router_json_is_discovery_mode_with_the_fleets_domain_and_graph(
        tmp_path):
    home = make_home(tmp_path)
    rendered = router_config.render_router_json(FLEET, M, home)
    doc = rendered.doc
    assert list(doc) == ["state_dir", "instances_dir", "selected_json",
                         "fleet_domain", "peer_senders"]
    assert "instances" not in doc
    assert doc["instances_dir"] == render.instances_dir(home)
    assert doc["selected_json"] == render.selected_json(home)
    assert doc["fleet_domain"] == FLEET["fleet_domain"]
    assert doc["peer_senders"] == {"alpha": [], "beta": ["alpha"]}
    assert "peers" not in doc
    assert rendered.text == json.dumps(doc, indent=2) + "\n"
    assert rendered.warnings == ()


def test_task_graph_all_passes_through_as_the_routers_word(tmp_path):
    home = make_home(tmp_path)
    fleet = fleet_variant(tmp_path, task_graph="ALL")
    rendered = bring_up(home, M, fleet)
    assert rendered.doc["task_graph"] == RC.TASK_GRAPH_ALL
    assert "peer_senders" not in rendered.doc
    view = router_view(router_config.router_json_path(home))
    assert view["peer_senders"] == {"alpha": ["beta"], "beta": ["alpha"]}


def test_task_deny_under_all_renders_an_explicit_graph(tmp_path):
    home = make_home(tmp_path)
    fleet = fleet_variant(tmp_path, task_graph="ALL",
                          task_deny=[["alpha", "beta"]])
    rendered = bring_up(home, M, fleet)
    assert "task_graph" not in rendered.doc
    assert rendered.doc["peer_senders"] == {"alpha": ["beta"], "beta": []}
    view = router_view(router_config.router_json_path(home))
    assert view["peer_senders"] == {"alpha": ["beta"], "beta": []}


def test_mail_peers_are_rendered_for_the_router(tmp_path):
    home = make_home(tmp_path)
    fleet = fleet_variant(tmp_path, task_graph={},
                          peers={"alpha": ["beta"], "beta": ["alpha"]})
    rendered = bring_up(home, M, fleet)
    assert rendered.doc["peers"] == {"alpha": ["beta"], "beta": ["alpha"]}
    view = router_view(router_config.router_json_path(home))
    assert view["peers"] == {"alpha": ["beta"], "beta": ["alpha"]}


def test_a_pair_on_both_lanes_is_refused(tmp_path):
    home = make_home(tmp_path)
    fleet = fleet_variant(tmp_path, task_graph={"beta": ["alpha"]},
                          peers={"alpha": ["beta"], "beta": ["alpha"]})
    with pytest.raises(RouterConfigError, match="BOTH lanes"):
        router_config.render_router_json(fleet, M, home)


def test_one_sided_mail_entries_are_warnings(tmp_path):
    home = make_home(tmp_path)
    fleet = fleet_variant(tmp_path, task_graph={}, peers={"alpha": ["beta"]})
    rendered = bring_up(home, M, fleet)
    assert rendered.warnings
    assert all("one-sided" in w for w in rendered.warnings)
    view = router_view(router_config.router_json_path(home))
    assert "config_error" not in view, view


def test_absent_membership_is_not_an_empty_one(tmp_path):
    home = make_home(tmp_path)
    with pytest.raises(RouterConfigError, match="absent"):
        router_config.render_router_json(FLEET, None, home)
    with pytest.raises(RouterConfigError, match="absent"):
        router_config.render_verdict(None, WS)
    assert router_config.render_verdict([], WS) == \
        {"schema": RC.SELECTED_SCHEMA, "selected": [], "not_selected": []}

    fleet = fleet_variant(tmp_path, task_graph={})
    rendered = router_config.render_router_json(fleet, [], home)
    Path(router_config.router_json_path(home)).write_text(rendered.text)
    path = router_config.router_json_path(home)
    assert router_view(path)["verdict_unavailable"] is True
    router_config.write_verdict(home, router_config.render_verdict([], WS))
    view = router_view(path)
    assert view["verdict_unavailable"] is False
    assert view["instances"] == []


def test_members_outside_the_fleets_workspace_are_refused(tmp_path):
    home = make_home(tmp_path)
    stray = [Member("other", "alpha", "x")]
    with pytest.raises(RouterConfigError):
        router_config.render_router_json(FLEET, stray, home)
    with pytest.raises(RouterConfigError):
        router_config.render_verdict(stray, WS)


def test_a_fleet_naming_a_non_member_is_refused_in_this_deployments_words(
        tmp_path):
    home = make_home(tmp_path)
    with pytest.raises(RouterConfigError) as e:
        router_config.render_router_json(FLEET, [M[0]], home)
    assert "amap-sandy" not in str(e.value)
    assert "manifest" not in str(e.value)


# --- the verdict -------------------------------------------------------------

def test_the_verdict_carries_only_what_the_router_reads():
    v = router_config.render_verdict(M, WS)
    assert set(v) == {"schema", "selected", "not_selected"}
    assert v["schema"] == RC.SELECTED_SCHEMA
    assert all(set(e) == {"slug"} for e in v["selected"])
    assert [e["slug"] for e in v["selected"]] == ["alpha", "beta"]
    assert v["not_selected"] == []
    text = router_config.json_text(v)
    for m in M:
        assert m.id not in text
    assert '"id"' not in text


def test_write_verdict_replaces_the_file_atomically(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    v1 = router_config.render_verdict(M, WS)
    path = router_config.write_verdict(home, v1)
    assert path == render.selected_json(home)
    ino = os.stat(path).st_ino

    v2 = router_config.render_verdict([M[0]], WS)
    router_config.write_verdict(home, v2)
    assert Path(path).read_text() == router_config.json_text(v2)
    assert os.stat(path).st_ino != ino
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert list(Path(home).glob(".*.tmp")) == []

    def boom(src, dst):
        raise OSError("no")
    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            router_config.write_verdict(home, v1)
    assert Path(path).read_text() == router_config.json_text(v2)
    assert list(Path(home).glob(".*.tmp")) == []


def test_write_verdict_never_creates_home(tmp_path):
    home = str(tmp_path / "nohome")
    with pytest.raises(RouterConfigError):
        router_config.write_verdict(home, router_config.render_verdict(M, WS))
    assert not os.path.exists(home)


# --- lanes -------------------------------------------------------------------

def test_lane_paths_are_the_routers_roots_and_leaves(tmp_path):
    home = make_home(tmp_path)
    want = []
    for lane in F["lanes"]:
        root = f"{home}/instances/alpha/{lane}"
        want.append(root)
        want.extend(f"{root}/{leaf}" for leaf in F["leaves"][lane])
    assert router_config.lane_paths(home, "alpha") == want


def test_after_lane_creation_every_root_and_leaf_exists_and_the_routers_presence_check_passes(
        tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    for m in M:
        for lane in F["lanes"]:
            root = f"{home}/instances/{m.name}/{lane}"
            assert os.path.isdir(root) and not os.path.islink(root)
            for leaf in F["leaves"][lane]:
                p = f"{root}/{leaf}"
                assert os.path.isdir(p) and not os.path.islink(p)
    before = snapshot(tmp_path)
    view = router_view(router_config.router_json_path(home))
    assert view["presence"] == {"alpha": "ok", "beta": "ok"}
    assert snapshot(tmp_path) == before  # the check created nothing


def test_the_presence_check_refuses_a_missing_leaf(tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    lane = F["lanes"][1]
    leaf = f"{home}/instances/alpha/{lane}/{F['leaves'][lane][0]}"
    os.rmdir(leaf)
    view = router_view(router_config.router_json_path(home))
    assert view["presence"]["alpha"].startswith("refused")
    assert leaf in view["presence"]["alpha"]
    assert view["presence"]["beta"] == "ok"


def test_lanes_exist_before_the_sandbox_as_every_rendered_lane_mount_source(
        tmp_path):
    """OpenShell main@acbac9c:crates/openshell-driver-docker/src/lib.rs:3753-3761
    refuses a bind mount whose source is missing, so with no sandbox and no
    membership yet, `create_lanes` has made every source the create command
    names for a lane."""
    home = make_home(tmp_path)
    router_config.create_lanes(home, "alpha")
    host = render.Host(home, "img:test", render.parse_run_as("1234:5678"), False)
    lanes = [m for m in render.mount_table(host, "alpha")
             if m.role in F["lanes"]]
    assert [m.role for m in lanes] == F["lanes"]
    for m in lanes:
        assert os.path.isdir(m.source), m.source


def test_create_lanes_is_idempotent_and_reports_what_it_made(tmp_path):
    home = make_home(tmp_path)
    inst = render.instances_dir(home)
    want = [inst, f"{inst}/alpha", *router_config.lane_paths(home, "alpha")]
    before = snapshot(tmp_path)
    assert router_config.create_lanes(home, "alpha", dry_run=True) == want
    assert snapshot(tmp_path) == before
    assert router_config.create_lanes(home, "alpha") == want
    assert router_config.create_lanes(home, "alpha") == []
    assert router_config.create_lanes(home, "alpha", dry_run=True) == []
    for p in want:
        assert p == inst or is_or_under(p, f"{inst}/alpha")
    # a second member adds only its own tree
    assert router_config.create_lanes(home, "beta")[0] == f"{inst}/beta"


def test_create_lanes_refuses_symlinks_and_files(tmp_path):
    home = make_home(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(render.instances_dir(home))
    os.symlink(str(elsewhere), f"{render.instances_dir(home)}/alpha")
    with pytest.raises(RouterConfigError, match="symlink"):
        router_config.create_lanes(home, "alpha")
    with pytest.raises(RouterConfigError, match="symlink"):
        router_config.create_lanes(home, "alpha", dry_run=True)
    assert list(elsewhere.iterdir()) == []

    root = f"{render.instances_dir(home)}/gamma/{F['lanes'][0]}"
    os.makedirs(os.path.dirname(root))
    Path(root).write_text("")
    with pytest.raises(RouterConfigError, match="not a directory"):
        router_config.create_lanes(home, "gamma")

    lane = F["lanes"][2]
    leaf = f"{render.instances_dir(home)}/delta/{lane}/{F['leaves'][lane][0]}"
    os.makedirs(os.path.dirname(leaf))
    os.symlink(str(elsewhere), leaf)
    with pytest.raises(RouterConfigError, match="symlink"):
        router_config.create_lanes(home, "delta")
    assert list(elsewhere.iterdir()) == []


def test_create_lanes_refuses_bad_names_and_homes(tmp_path):
    home = make_home(tmp_path)
    before = snapshot(tmp_path)
    for bad_home, name in ((home, "Alpha"), (home, "a" * 20),
                           ("relative/home", "alpha"),
                           (str(tmp_path / "nohome"), "alpha")):
        with pytest.raises(RouterConfigError):
            router_config.create_lanes(bad_home, name)
    assert snapshot(tmp_path) == before


def test_the_roster_dir_is_derived_and_outside_every_members_lanes(tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    view = router_view(router_config.router_json_path(home))
    assert view["roster_dir"] is not None
    assert view["roster_dir"] == render.roster_dir(home)
    assert view["roots"]
    for root in [*view["roots"].values(), render.instances_dir(home)]:
        assert not overlap(view["roster_dir"], root)


# --- state_dir ---------------------------------------------------------------

def test_state_dir_defaults_beside_the_tree_and_is_not_nested(tmp_path):
    home = make_home(tmp_path)
    doc = router_config.render_router_json(FLEET, M, home).doc
    assert doc["state_dir"] == f"{home}/router-state"
    assert not overlap(doc["state_dir"], render.instances_dir(home))
    assert not overlap(doc["state_dir"], render.selected_json(home))


_BAD_STATE_DIRS = {
    "home": lambda h: h,
    "instances_dir": render.instances_dir,
    "instances_child": lambda h: render.instances_dir(h) + "/x",
    "selected_json": render.selected_json,
    "parent_of_home": posixpath.dirname,
    "payload_child": lambda h: render.payload_dir(h) + "/x",
    "roster_dir": render.roster_dir,
    "roster_child": lambda h: render.roster_dir(h) + "/x",
    "relative": lambda h: "relative",
    "colon": lambda h: h + "/a:b",
    "dotdot": lambda h: h + "/x/../y",
}
_ROUTER_REFUSES = ("home", "instances_dir", "instances_child", "selected_json",
                   "parent_of_home")


@pytest.mark.parametrize("case", list(_BAD_STATE_DIRS))
def test_a_nested_state_dir_is_refused_and_the_router_agrees(tmp_path, case):
    home = make_home(tmp_path)
    bad = _BAD_STATE_DIRS[case](home)
    with pytest.raises(RouterConfigError):
        router_config.render_router_json(FLEET, M, home, state_dir=bad)
    if case in _ROUTER_REFUSES:
        good = router_config.render_router_json(FLEET, M, home).doc
        with pytest.raises(RouterConfigError, match="must not be nested"):
            router_config.loader_check({**good, "state_dir": bad})


# --- drift -------------------------------------------------------------------

def test_an_unchanged_rendering_has_no_drift(tmp_path):
    home = make_home(tmp_path)
    rendered = bring_up(home, M)
    assert router_config.drift_on_disk(
        router_config.router_json_path(home), rendered) == []


def test_a_changed_task_graph_is_drift_naming_the_key(tmp_path):
    home = make_home(tmp_path)
    bring_up(home, M)
    path = router_config.router_json_path(home)

    changed = router_config.render_router_json(
        fleet_variant(tmp_path, task_graph={"alpha": ["beta"]}), M, home)
    lines = router_config.drift_on_disk(path, changed)
    assert lines
    assert all("peer_senders" in ln for ln in lines)
    assert any("peer_senders[alpha]" in ln for ln in lines)
    assert any("peer_senders[beta]" in ln for ln in lines)

    everyone = router_config.render_router_json(
        fleet_variant(tmp_path, task_graph="ALL"), M, home)
    lines = router_config.drift_on_disk(path, everyone)
    assert any(ln.startswith("+ task_graph") for ln in lines)
    assert any(ln.startswith("- peer_senders") for ln in lines)


def test_drift_reports_absent_invalid_foreign_keys_and_bytes(tmp_path):
    home = make_home(tmp_path)
    rendered = router_config.render_router_json(FLEET, M, home)
    path = router_config.router_json_path(home)

    lines = router_config.drift_on_disk(path, rendered)
    assert len(lines) == 1 and "absent" in lines[0]

    Path(path).write_text("{not json")
    lines = router_config.drift_on_disk(path, rendered)
    assert len(lines) == 1 and "not valid JSON" in lines[0]

    Path(path).write_text(router_config.json_text(
        {**rendered.doc, "attachment_max_bytes": 1}))
    lines = router_config.drift_on_disk(path, rendered)
    assert len(lines) == 1 and lines[0].startswith("- attachment_max_bytes")

    Path(path).write_text(json.dumps(rendered.doc, indent=4) + "\n")
    assert router_config.drift_on_disk(path, rendered) == [
        "same content, different bytes (whitespace or key order)"]

    Path(path).write_text(router_config.json_text(
        {**rendered.doc, "state_dir": rendered.doc["state_dir"] + "-old"}))
    lines = router_config.drift_on_disk(path, rendered)
    assert len(lines) == 1 and lines[0].startswith("~ state_dir")

    Path(path).write_text("[1]")
    assert router_config.drift_on_disk(path, rendered) == [
        "the file on disk is not a JSON object"]


def test_drift_compares_mail_peers_as_sets_and_task_senders_as_lists():
    have = {"peers": {"a": ["b", "c"]}, "peer_senders": {"a": ["b", "c"]}}
    want = {"peers": {"a": ["c", "b"]}, "peer_senders": {"a": ["c", "b"]}}
    lines = router_config.drift(have, want)
    assert len(lines) == 1 and lines[0].startswith("~ peer_senders[a]")
    assert router_config.drift({"peers": {"a": ["b", "c"]}},
                               {"peers": {"a": ["c", "b"]}}) == []
    # a name on one side only is a difference, not a skip
    assert router_config.drift({"peers": {}}, {"peers": {"a": ["b"]}}) == [
        "~ peers[a] None -> ['b']"]


def test_rendering_writes_nothing(tmp_path):
    home = make_home(tmp_path)
    before = snapshot(tmp_path)
    rendered = router_config.render_router_json(FLEET, M, home)
    router_config.render_verdict(M, WS)
    router_config.lane_paths(home, "alpha")
    router_config.drift({}, rendered.doc)
    router_config.json_text(rendered.doc)
    assert snapshot(tmp_path) == before


# --- the source --------------------------------------------------------------

def _tree():
    return ast.parse((REPO / "router_config.py").read_text(encoding="utf-8"))


def test_lane_names_are_not_literals_in_router_config():
    names = set(F["lanes"]) | {leaf for v in F["leaves"].values() for leaf in v}
    assert names
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in names, node.value


def test_router_config_uses_only_the_public_router_config():
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "router_link" in imported
    assert not [m for m in imported if m.split(".")[0] == "router"]
