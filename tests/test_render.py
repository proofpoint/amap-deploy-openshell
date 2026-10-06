"""Sandbox rendering: the `openshell sandbox create` argument vector and the
policy YAML for one member.

OpenShell facts the checks rely on, at OpenShell main@acbac9c:
docs/how-it-works/sandboxes/runtimes.mdx:113-133 (Docker bind mounts, and
targets may not replace the workspace root or the container root),
docs/how-it-works/policies/schema.mdx:39-100 (filesystem_policy,
landlock.compatibility, process.run_as_*),
crates/openshell-core/src/container_paths.rs:11-41 (reserved roots),
crates/openshell-sandbox/src/sandbox/linux/landlock.rs:241-242 (both lists empty
means no Landlock layer), docs/how-it-works/policies/default-policy.mdx:26-40
and 58-75 (system paths and the baseline read-write paths), and
crates/openshell-cli/src/main.rs (the create flags, at the lines render.py
cites).

The acceptance checks read the *rendered outputs*, the driver-config JSON in the
argv and the policy YAML text, not the intermediate `Rendering.mounts` and
`Rendering.policy`. The lane names come from the router through a subprocess,
independent of render.py's own import.
"""

import ast
import copy
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

import _amap_main
import _workspace
import membership
import policy
import provider_profile
import render
from render import Host, RenderError, RunAs

REPO = Path(__file__).absolute().parents[1]
FLEET = policy.load_fleet(REPO / "examples" / "fleet.json")
# The last name has 19 characters, and alpha-2 is a prefix trap for alpha.
MEMBERS = ("alpha", "beta", "gamma-1", "a", "alpha-2", "abcdefghijklmnopqrs")
F = _amap_main.router_facts()

# OpenShell main@acbac9c:crates/openshell-core/src/container_paths.rs:11-17,26,32-41 (checked by driver_mounts.rs:86-99)
OPENSHELL_RESERVED_ROOTS = (
    "/opt/openshell", "/etc/openshell", "/etc/openshell-tls", "/run/openshell",
    "/run/openshell-sidecar", "/run/netns", "/var/run/netns", "/proc", "/sys",
    "/dev")


def make_host(tmp_path, restart=False, run_as="1234:5678"):
    return Host(str(tmp_path / "home"), "amap-openshell-agent:test",
                render.parse_run_as(run_as), restart)


def policy_path(tmp_path, name):
    return str(tmp_path / "policies" / f"{name}.yaml")


def render_all(tmp_path, h):
    return {n: render.render_member(FLEET, n, h, policy_path(tmp_path, n))
            for n in MEMBERS}


def is_or_under(a, b):
    a, b = PurePosixPath(a), PurePosixPath(b)
    return a == b or b in a.parents


def overlap(a, b):
    return is_or_under(a, b) or is_or_under(b, a)


def argv_values(argv, flag):
    cut = argv.index("--")
    return [argv[i + 1] for i, w in enumerate(argv[:cut]) if w == flag]


def argv_value(argv, flag):
    got = argv_values(argv, flag)
    assert len(got) == 1, f"{flag} occurs {len(got)} times before --"
    return got[0]


_KEY_LINE = re.compile(r"^( *)([a-z_]+):(?: (.+))?$")
_ITEM_LINE = re.compile(r"^( *)- (.+)$")


def _scalar(text):
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "[]":
        return []
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        value = json.loads(text)
        assert isinstance(value, str), text
        return value
    raise AssertionError(f"not a scalar this grammar has: {text!r}")


def read_yaml_subset(text):
    """A strict reader of exactly the emitter's grammar."""
    assert text.endswith("\n"), "the text must end with a newline"
    assert "\t" not in text, "tabs are not part of the grammar"
    lines = text[:-1].split("\n")
    pos = [0]

    def parse_block(indent):
        out = {}
        while pos[0] < len(lines):
            line = lines[pos[0]]
            if line.startswith("#"):
                pos[0] += 1
                continue
            m = _KEY_LINE.match(line)
            assert m, f"not a line this grammar has: {line!r}"
            got = len(m.group(1))
            assert got % 2 == 0, f"odd indentation: {line!r}"
            if got < indent:
                return out
            assert got == indent, f"unexpected indentation: {line!r}"
            key, value = m.group(2), m.group(3)
            assert key not in out, f"duplicate key {key!r}"
            pos[0] += 1
            if value is not None:
                out[key] = _scalar(value)
                continue
            assert pos[0] < len(lines), f"{key!r} has no value"
            nxt = lines[pos[0]]
            item = _ITEM_LINE.match(nxt)
            if item and len(item.group(1)) == indent + 2:
                items = []
                while pos[0] < len(lines):
                    item = _ITEM_LINE.match(lines[pos[0]])
                    if not item or len(item.group(1)) != indent + 2:
                        break
                    items.append(_scalar(item.group(2)))
                    pos[0] += 1
                out[key] = items
            else:
                out[key] = parse_block(indent + 2)
                assert out[key], f"{key!r} has an empty mapping"
        return out

    result = parse_block(0)
    assert pos[0] == len(lines), f"unread lines from {lines[pos[0]]!r}"
    return result


def rendered_mounts(r):
    doc = json.loads(argv_value(r.argv, "--driver-config-json"))
    assert set(doc) == {"docker"}
    assert set(doc["docker"]) == {"mounts"}
    return doc["docker"]["mounts"]


def rendered_policy(r):
    return read_yaml_subset(r.policy_yaml)


def lane_role_targets():
    return {lane: f"/opt/amap/lanes/{lane}" for lane in F["lanes"]}


# --- acceptance criteria -----------------------------------------------------

def test_inbound_lanes_payload_and_roster_are_read_only_twice_and_the_outbox_read_write_twice(tmp_path):
    h = make_host(tmp_path)
    home = h.home
    for name, r in render_all(tmp_path, h).items():
        mounts = rendered_mounts(r)
        fs = rendered_policy(r)["filesystem_policy"]
        by_source = {m["source"]: m for m in mounts}
        assert len(by_source) == len(mounts)
        for m in mounts:
            assert set(m) == {"type", "source", "target", "read_only"}
            assert m["type"] == "bind"
        for lane in F["lanes"]:
            m = by_source[f"{home}/instances/{name}/{lane}"]
            if lane != F["outbox"]:
                assert m["read_only"] is True
                assert m["target"] in fs["read_only"]
                assert m["target"] not in fs["read_write"]
            else:
                assert m["read_only"] is False
                assert m["target"] in fs["read_write"]
                assert m["target"] not in fs["read_only"]
        for shared in (f"{home}/payload", f"{home}/roster"):
            m = by_source[shared]
            assert m["read_only"] is True
            assert m["target"] in fs["read_only"]
            assert m["target"] not in fs["read_write"]


def test_flags_and_lists_come_from_one_table(tmp_path):
    h = make_host(tmp_path)
    table = render.mount_table(h, "alpha")
    flipped = tuple(m._replace(read_only=False) if m.role == F["inbox"] else m
                    for m in table)
    r = render.render_from_table(FLEET, "alpha", h,
                                 policy_path(tmp_path, "alpha"), flipped)
    inbox_target = f"/opt/amap/lanes/{F['inbox']}"
    got = [m for m in rendered_mounts(r) if m["target"] == inbox_target]
    assert [m["read_only"] for m in got] == [False]
    fs = rendered_policy(r)["filesystem_policy"]
    assert inbox_target in fs["read_write"]
    assert inbox_target not in fs["read_only"]
    probs = render.rendering_problems(r, h)
    assert any(F["inbox"] in p and "read-only" in p for p in probs), probs
    good = render.render_member(FLEET, "alpha", h, policy_path(tmp_path, "alpha"))
    assert render.rendering_problems(good, h) == []


def test_policy_compatibility_is_hard_requirement(tmp_path):
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        doc = rendered_policy(r)
        assert doc["landlock"] == {"compatibility": "hard_requirement"}
        assert doc["version"] == 1


def test_the_yaml_is_the_policy_document(tmp_path):
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        assert read_yaml_subset(r.policy_yaml) == r.policy
        lines = r.policy_yaml.split("\n")
        assert lines[0].startswith("#")
        items = [ln for ln in lines if ln.lstrip().startswith("- ")]
        assert items
        for ln in items:
            body = ln.lstrip()[2:]
            assert body.startswith('"') and body.endswith('"'), ln
        ids = [ln for ln in lines if "run_as_" in ln]
        assert len(ids) == 2
        for ln in ids:
            value = ln.split(": ", 1)[1]
            assert value.startswith('"') and value.endswith('"'), ln


@pytest.mark.parametrize("text", [
    'a:\n  - foo\n',              # an unquoted string item
    'a:\n\t- "x"\n',              # a tab indent
    '{a: 1}\n',                   # a flow mapping
    'a: {b: 1}\n',                # a flow mapping as a value
    'a:\n   - "x"\n',             # an unknown indentation step
    'a: word\n',                  # a bare word
    'a: "x"',                     # no final newline
])
def test_the_yaml_reader_refuses_what_it_does_not_understand(text):
    with pytest.raises(AssertionError):
        read_yaml_subset(text)


def test_no_two_members_share_a_member_mount_source(tmp_path):
    """Acceptance 2, in the reading of the plan (R2): the payload and the
    roster are shared and read-only; no member-owned source is shared or
    nested."""
    h = make_host(tmp_path)
    renders = render_all(tmp_path, h)
    shared = {f"{h.home}/payload", f"{h.home}/roster"}
    seen = {}
    for name, r in renders.items():
        for m in rendered_mounts(r):
            seen.setdefault(m["source"], []).append((name, m))
    for source, uses in seen.items():
        if len(uses) > 1:
            assert source in shared, source
            assert all(m["read_only"] is True for _, m in uses)
    assert shared <= set(seen)
    lane_sources = {name: [m["source"] for m in rendered_mounts(r)
                           if m["source"] not in shared]
                    for name, r in renders.items()}
    names = list(lane_sources)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for sa in lane_sources[a]:
                for sb in lane_sources[b]:
                    assert not overlap(sa, sb), (sa, sb)
    writable = {name: [m["source"] for m in rendered_mounts(r)
                       if m["read_only"] is False]
                for name, r in renders.items()}
    assert all(len(v) == 1 for v in writable.values())
    flat = [s for v in writable.values() for s in v]
    assert len(set(flat)) == len(flat)


def test_sources_are_only_this_members_lanes_the_payload_and_the_roster(tmp_path):
    h = make_host(tmp_path)
    for name, r in render_all(tmp_path, h).items():
        want = {f"{h.home}/payload", f"{h.home}/roster"}
        want |= {f"{h.home}/instances/{name}/{lane}" for lane in F["lanes"]}
        assert {m["source"] for m in rendered_mounts(r)} == want


def test_every_mount_target_avoids_openshells_reserved_roots(tmp_path):
    assert set(render.RESERVED_ROOTS) >= set(OPENSHELL_RESERVED_ROOTS)
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        targets = [m["target"] for m in rendered_mounts(r)]
        for t in targets:
            assert t != "/"
            assert posixpath.normpath(t) == t
            for root in OPENSHELL_RESERVED_ROOTS:
                assert not overlap(t, root), (t, root)
            # runtimes.mdx:133: targets cannot replace the workspace root
            assert not overlap(t, "/sandbox"), t
        for i, a in enumerate(targets):
            for b in targets[i + 1:]:
                assert not overlap(a, b), (a, b)


def test_the_policy_has_no_network_rules_and_egress_comes_only_from_the_provider(tmp_path):
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        assert set(rendered_policy(r)) == {"version", "filesystem_policy",
                                           "landlock", "process"}
        assert "network_policies" not in r.policy_yaml
        assert "network_middlewares" not in r.policy_yaml
        assert argv_values(r.argv, "--provider") == [render.PROVIDER]


def test_the_create_command_names_this_deployments_profile(tmp_path):
    """AC4: `--provider` is the ID of providers/amap-claude-code.json, which
    the runner imports in step 3 (OpenShell
    main@acbac9c:crates/openshell-cli/src/commands/provider.rs:413-447)."""
    h = make_host(tmp_path)
    assert render.PROVIDER == "amap-claude-code"
    for r in render_all(tmp_path, h).values():
        assert argv_values(r.argv, "--provider") == [provider_profile.PROFILE_ID]
        assert render.rendering_problems(r, h) == []


def test_no_mount_target_is_at_or_under_a_read_write_path(tmp_path):
    """Acceptance 5, in the reading of the plan (R3). The workdir is the
    image's WORKDIR (runtimes.mdx:275), and /tmp and /dev/null are the baseline
    read-write paths (default-policy.mdx:65-68)."""
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        fs = rendered_policy(r)["filesystem_policy"]
        assert {"/sandbox", "/tmp", "/dev/null"} <= set(fs["read_write"])
        assert fs["include_workdir"] is False
        for m in rendered_mounts(r):
            for p in fs["read_write"]:
                if is_or_under(m["target"], p):
                    assert p == m["target"] and m["read_only"] is False, (m, p)


def test_the_policy_lists_are_never_empty(tmp_path):
    for r in render_all(tmp_path, make_host(tmp_path)).values():
        fs = rendered_policy(r)["filesystem_policy"]
        assert fs["read_only"] and fs["read_write"]


def test_the_checker_catches_each_broken_rule(tmp_path, monkeypatch):
    h = make_host(tmp_path)
    pp = policy_path(tmp_path, "alpha")
    base = render.render_member(FLEET, "alpha", h, pp)
    assert render.rendering_problems(base, h) == []

    def with_policy(edit):
        doc = copy.deepcopy(base.policy)
        edit(doc)
        return base._replace(policy=doc)

    def from_table(edit):
        table = tuple(edit(m) for m in render.mount_table(h, "alpha"))
        return render.render_from_table(FLEET, "alpha", h, pp, table)

    def emptied(doc):
        doc["filesystem_policy"]["read_only"] = []
        doc["filesystem_policy"]["read_write"] = []

    def rw_add(path):
        return lambda doc: doc["filesystem_policy"]["read_write"].append(path)

    def user_zero(doc):
        doc["process"]["run_as_user"] = "0"

    def add_network(doc):
        doc["network_policies"] = {}

    def retarget(m):
        return m._replace(target="/opt/openshell/x") if m.role == F["inbox"] else m

    def reroute(m):
        if m.role == F["inbox"]:
            return m._replace(source=render.lane_dir(h.home, "beta", F["inbox"]))
        return m

    cases = [
        (with_policy(emptied), "Landlock"),
        (with_policy(rw_add("/opt/amap")), "read_write"),
        (with_policy(rw_add(render.PAYLOAD_TARGET)), "read_write"),
        (from_table(retarget), "reserved"),
        (with_policy(user_zero), "run_as"),
        (with_policy(add_network), "network"),
        (from_table(reroute), "source"),
        (base._replace(policy_yaml=base.policy_yaml + "# edited\n"), "yaml"),
    ]
    for broken, word in cases:
        probs = render.rendering_problems(broken, h)
        assert any(word in p for p in probs), (word, probs)

    table = render.mount_table(h, "alpha")
    flipped = tuple(m._replace(read_only=False) if m.role == F["inbox"] else m
                    for m in table)
    monkeypatch.setattr(render, "mount_table", lambda host, name: flipped)
    with pytest.raises(RenderError):
        render.render_member(FLEET, "alpha", h, pp)


def test_design_section_3_uses_the_routers_lane_names(tmp_path):
    text = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    start = text.index("\n## 3.")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    rows = dict(re.findall(r"^\| `(/[^`]+)` \| ([^|]+) \|", section, re.M))
    rows = {k: v.strip() for k, v in rows.items()}
    want = {render.PAYLOAD_TARGET: "read-only", render.ROSTER_TARGET: "read-only"}
    for lane in F["lanes"]:
        want[f"{render.LANES_TARGET}/{lane}"] = (
            "read-write" if lane == F["outbox"] else "read-only")
    assert render.PAYLOAD_TARGET == "/opt/amap/payload"
    assert rows == want
    assert not any("inbound" in p or "outbound" in p for p in rows)
    r = render.render_member(FLEET, "alpha", make_host(tmp_path),
                             policy_path(tmp_path, "alpha"))
    assert set(rows) == {m["target"] for m in rendered_mounts(r)}


def test_every_policy_runs_as_the_one_configured_uid_and_gid(tmp_path, monkeypatch):
    h = make_host(tmp_path, run_as="1234:5678")
    first = render_all(tmp_path, h)
    pairs = set()
    for r in first.values():
        proc = rendered_policy(r)["process"]
        assert proc == {"run_as_user": "1234", "run_as_group": "5678"}
        pairs.add((proc["run_as_user"], proc["run_as_group"]))
    assert len(pairs) == 1 and "0" not in pairs.pop()
    for fn in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(os, fn, lambda: 0)
    second = render_all(tmp_path, h)
    for n in MEMBERS:
        assert second[n].argv == first[n].argv
        assert second[n].policy_yaml == first[n].policy_yaml


@pytest.mark.parametrize("value", [
    "0:1000", "1000:0", "0:0", "", "1000", "1000:", "a:b", "-1:1000",
    "+1:1000", "1000:1000:1", "4294967295:1", " 1000:1000", "1000:1000\n",
    "\u0661:1"])
def test_run_as_refuses_root_and_malformed_values(value):
    with pytest.raises(RenderError):
        render.parse_run_as(value)


def test_run_as_accepts_the_largest_id():
    assert render.parse_run_as("1:4294967294") == RunAs(1, 4294967294)
    with pytest.raises(RenderError):
        render.parse_run_as(1000)


def test_run_as_is_the_format_the_router_container_gets():
    text = (_workspace.ROUTER_ROOT / "docker" / "run.sh").read_text(
        encoding="utf-8")
    assert '--user "$(id -u):$(id -g)"' in text
    assert render.parse_run_as("1000:1000") == RunAs(1000, 1000)


# --- the argument vector -----------------------------------------------------

@pytest.mark.parametrize("restart", [False, True])
def test_the_argv_has_its_flags_once_and_in_place(tmp_path, restart):
    h = make_host(tmp_path, restart=restart)
    for name, r in render_all(tmp_path, h).items():
        argv = r.argv
        assert argv[:5] == ["openshell", "--workspace", "default", "sandbox",
                            "create"]
        assert argv.count("--") == 1
        assert argv_value(argv, "--name") == name
        assert argv_value(argv, "--from") == h.image
        assert argv_value(argv, "--provider") == render.PROVIDER
        assert argv_value(argv, "--policy") == policy_path(tmp_path, name)
        argv_value(argv, "--driver-config-json")
        env_of = dict(zip((F["inbox"], F["peer"], F["outbox"]),
                          _amap_main.INPUT_LANE_VARS))
        want = [f"{env_of[lane]}=/opt/amap/lanes/{lane}" for lane in F["lanes"]]
        addr = policy.addresses(FLEET, [name])[name]
        want += ["AMAP_SELF_ADDRESS=" + addr, "AMAP_SELF=" + addr,
                 "AMAP_ROSTER_DIR=/opt/amap/roster"]
        assert argv_values(argv, "--env") == want
        cut = argv.index("--")
        for flag in render.UNATTENDED_FLAGS:
            assert argv[:cut].count(flag) == 1, flag
        for flag in render.UNATTENDED_NEGATIONS:
            assert flag not in argv[:cut], flag


@pytest.mark.parametrize("restart", [False, True])
def test_the_create_command_is_unattended_and_passes_s4s_checks(
        tmp_path, restart):
    """AC4. OpenShell main@acbac9c:crates/openshell-cli/src/main.rs:1523-1555:
    the negations override the flags, so a rendering that has one is unsound."""
    h = make_host(tmp_path, restart=restart)
    for name in MEMBERS:
        r = render.render_member(FLEET, name, h, policy_path(tmp_path, name))
        assert render.rendering_problems(r, h) == [], name
        cut = r.argv.index("--")
        for flag in render.UNATTENDED_FLAGS:
            assert flag in r.argv[:cut]
            without = [w for w in r.argv if w != flag]
            problems = render.rendering_problems(r._replace(argv=without), h)
            assert any(flag in p for p in problems), (name, flag)
        for flag in render.UNATTENDED_NEGATIONS:
            withit = r.argv[:cut] + [flag] + r.argv[cut:]
            problems = render.rendering_problems(r._replace(argv=withit), h)
            assert any(flag in p for p in problems), (name, flag)


def _connector_constants(*names):
    tree = ast.parse((_workspace.CONNECTOR_ROOT / "bin" / "inbox-submit")
                     .read_text(encoding="utf-8"))
    found = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id in names:
            found[n.targets[0].id] = ast.literal_eval(n.value)
    return found


@pytest.mark.parametrize("restart", [False, True])
def test_the_create_command_exports_the_roster_and_self_for_inbox_submit(
        tmp_path, restart):
    """AC4. inbox-submit's `peers` tool reads AMAP_ROSTER_DIR and AMAP_SELF
    (amap-connector-claude bin/inbox-submit at 37875a5)."""
    h = make_host(tmp_path, restart=restart)
    consts = _connector_constants("ROSTER_DIR_ENV", "SELF_ENV")
    assert render.ROSTER_ENV == consts["ROSTER_DIR_ENV"]
    assert render.PEER_SELF_ENV == consts["SELF_ENV"]
    assert "/opt/amap/roster" == render.ROSTER_TARGET
    for name in MEMBERS:
        r = render.render_member(FLEET, name, h, policy_path(tmp_path, name))
        assert render.rendering_problems(r, h) == [], name
        envs = argv_values(r.argv, "--env")
        for want in ("AMAP_ROSTER_DIR=/opt/amap/roster",
                     f"AMAP_SELF={r.address}",
                     f"AMAP_SELF_ADDRESS={r.address}"):
            assert envs.count(want) == 1, (name, want)
        roster = [m for m in r.mounts if m.role == render.ROSTER_DIRNAME]
        assert [m.target for m in roster] == ["/opt/amap/roster"]


def test_restart_policy_only_when_the_installed_openshell_supports_it(tmp_path):
    on = render.render_member(FLEET, "alpha", make_host(tmp_path, restart=True),
                              policy_path(tmp_path, "alpha"))
    assert argv_value(on.argv, "--restart-policy") == "on-failure"
    off = render.render_member(FLEET, "alpha", make_host(tmp_path),
                               policy_path(tmp_path, "alpha"))
    assert "--restart-policy" not in off.argv


def test_the_rendered_main_process_runs_under_the_real_wrapper(tmp_path):
    """The wrapper runs claude with its own arguments, so the rendered tail has
    no `claude` word."""
    h = make_host(tmp_path)
    r = render.render_member(FLEET, "alpha", h, policy_path(tmp_path, "alpha"))
    tail = r.argv[r.argv.index("--") + 1:]
    assert tail[0] == "/opt/amap/payload/amap-main"
    assert (REPO / "payload" / posixpath.basename(tail[0])).is_file()
    (tmp_path / "lab").mkdir()
    lab = _amap_main.Lab(tmp_path / "lab")
    try:
        pairs = dict(p.split("=", 1) for p in argv_values(r.argv, "--env"))
        assert lab.run_once(*tail[1:], env=lab.env(**pairs)) == 0
        start = [e for e in lab.claude_events() if e["event"] == "start"][0]
        assert start["argv"] == tail[1:]
        assert start["argv"][0] == "--mcp-config"
        assert lab.starts()[0]["env"]["AMAP_DELIVERY_SELF"] == r.address
    finally:
        lab.close()


def test_the_env_names_are_the_ones_amap_main_reads():
    text = (REPO / "payload" / "amap-main").read_text(encoding="utf-8")
    required = set(re.findall(r'^: "\$\{(AMAP_[A-Z_]+):\?', text, re.M))
    assert required == {"AMAP_INBOX_DIR", "AMAP_PEER_DIR", "AMAP_OUTBOX_DIR"}
    assert required == set(render.LANE_ENV.values())
    assert '"${AMAP_SELF_ADDRESS+x}"' in text
    assert render.SELF_ENV == "AMAP_SELF_ADDRESS"
    got = dict(zip(_amap_main.INPUT_LANE_VARS,
                   (F["inbox"], F["peer"], F["outbox"])))
    assert got == {v: k for k, v in render.LANE_ENV.items()}


def _sandy_constants():
    tree = ast.parse((_workspace.SANDY_ROOT / "amap_sandy.py").read_text(
        encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant):
            found[node.targets[0].id] = node.value.value
    return found


def test_claude_gets_sandys_arguments():
    c = _sandy_constants()
    assert render.MCP_CONFIG_FLAG == c["MCP_CONFIG_FLAG"]
    assert render.SYSTEM_PROMPT_FILE_FLAG == c["SYSTEM_PROMPT_FILE_FLAG"]
    assert render.MCP_CONFIG_NAME == c["MCP_SERVERS_PAYLOAD_NAME"]
    assert render.POLICY_PROMPT_NAME == c["POLICY_PAYLOAD_NAME"]
    assert render.main_process()[1:5] == [
        c["MCP_CONFIG_FLAG"], render.PAYLOAD_TARGET + "/" + c["MCP_SERVERS_PAYLOAD_NAME"],
        c["SYSTEM_PROMPT_FILE_FLAG"], render.PAYLOAD_TARGET + "/" + c["POLICY_PAYLOAD_NAME"]]
    assert render.main_process()[5:] == [
        render.SETTINGS_FLAG, render.PAYLOAD_TARGET + "/claude-settings.json",
        render.BYPASS_PERMISSIONS_FLAG]


# --- the router's facts and the layout ---------------------------------------

def test_lane_names_come_from_the_router():
    assert render.LANES == tuple(F["lanes"])
    assert render.LANE_OUTBOX == F["outbox"]
    tree = ast.parse((REPO / "render.py").read_text(encoding="utf-8"))
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not literals & set(F["lanes"])


_ROSTER_SNIPPET = "from router import roster; print(roster.ROSTER_DIRNAME)"


def test_the_roster_is_beside_selected_json_as_the_router_derives_it(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_workspace.ROUTER_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, "-c", _ROSTER_SNIPPET], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    h = make_host(tmp_path)
    assert render.roster_dir(h.home) == posixpath.join(
        posixpath.dirname(render.selected_json(h.home)), r.stdout.strip())


def test_rendering_writes_no_files(tmp_path):
    home = tmp_path / "home"
    assert not home.exists()
    before = sorted(tmp_path.rglob("*"))
    render_all(tmp_path, make_host(tmp_path))
    assert sorted(tmp_path.rglob("*")) == before
    assert not home.exists()
    banned = {"open", "write_text", "write_bytes", "mkdir", "makedirs", "touch",
              "replace", "rename", "unlink", "rmdir", "chmod", "mkstemp",
              "NamedTemporaryFile", "getuid", "getgid", "geteuid", "getegid"}
    for mod in ("render.py", "router_link.py"):
        tree = ast.parse((REPO / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else \
                    f.attr if isinstance(f, ast.Attribute) else None
                assert name not in banned, (mod, name)


def test_rendering_is_deterministic(tmp_path):
    h = make_host(tmp_path, restart=True)
    a = render.render_member(FLEET, "alpha", h, policy_path(tmp_path, "alpha"))
    b = render.render_member(FLEET, "alpha", h, policy_path(tmp_path, "alpha"))
    assert a.argv == b.argv and a.policy_yaml == b.policy_yaml


def test_bad_hosts_and_names_are_refused(tmp_path):
    good = make_host(tmp_path)
    pp = policy_path(tmp_path, "alpha")
    cases = [
        (good._replace(home="relative/home"), "alpha", pp, "absolute"),
        (good._replace(home="/"), "alpha", pp, "root"),
        (good._replace(home=str(tmp_path) + "/a:b"), "alpha", pp, "':'"),
        (good._replace(home=str(tmp_path) + "/"), "alpha", pp, "normalized"),
        (good._replace(home=str(tmp_path) + "/x/../y"), "alpha", pp, "normalized"),
        (good._replace(home=str(tmp_path) + "/a b"), "alpha", pp, "whitespace"),
        (good._replace(image=""), "alpha", pp, "image"),
        (good._replace(image="a b"), "alpha", pp, "image"),
        (good, "alpha", "policies/alpha.yaml", "absolute"),
        (good, "alpha", str(tmp_path) + "/p/../a.yaml", "normalized"),
        (good, "Alpha", pp, "lowercase"),
        (good, "a" * 20, pp, "20 characters"),
    ]
    for host, name, path, word in cases:
        with pytest.raises(RenderError) as e:
            render.render_member(FLEET, name, host, path)
        assert word in str(e.value), (word, str(e.value))
    assert membership.name_problem("Alpha")


def test_member_rows_are_the_mount_table(tmp_path):
    h = make_host(tmp_path)
    assert render.member_rows(h.home, "alpha") == render.mount_table(h, "alpha")
    with pytest.raises(RenderError):
        render.member_rows(h.home, "Not_A_Name")
    with pytest.raises(RenderError):
        render.member_rows("/", "alpha")
