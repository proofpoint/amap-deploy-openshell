"""payload/mcp-servers.json, the MCP registration.

It is the connector's registration (amap-connector-claude `.mcp.json.example`)
in the form amap-deploy-sandy ships (`payload/mcp-servers.json`), with the
payload root replaced by this deployment's payload mount target, which is read
from DESIGN.md section 3. The lane directories are separate mounts, so the file
names them only by the variables `payload/amap-main` reads, the lane names and
leaves of which come from the router. One difference from sandy's registration
is named, D23: `inbox-submit` runs the connector's own `bin/inbox-submit`, not
sandy's `submit-server` wrapper.
"""

import copy
import json
import os
import posixpath
import re
from pathlib import PurePosixPath

import _amap_main
import _workspace

REPO = _amap_main.REPO
CONFIG = REPO / "payload" / "mcp-servers.json"


def payload_target():
    lines = (REPO / "DESIGN.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## 3."))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    hits = []
    for ln in lines[start:end]:
        m = re.match(r"^\| `(/[^`]+)` \|", ln)
        if m and "MCP config" in ln.strip().strip("|").split("|")[-1]:
            hits.append(m.group(1))
    assert len(hits) == 1, hits
    return hits[0]


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def ours():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def example():
    doc = json.loads((_workspace.CONNECTOR_ROOT / ".mcp.json.example").read_text(
        encoding="utf-8"))
    doc.pop("_comment", None)
    return doc


def sandys():
    return json.loads((_workspace.SANDY_ROOT / "payload" / "mcp-servers.json")
                      .read_text(encoding="utf-8"))


def test_it_registers_the_connectors_servers_the_connectors_way():
    mine, ex = ours(), example()
    assert set(mine) == {"mcpServers"}
    assert set(mine["mcpServers"]) == set(ex["mcpServers"])
    for name, server in mine["mcpServers"].items():
        theirs = ex["mcpServers"][name]
        assert server.get("args") == theirs.get("args"), name
        assert set(server["env"]) == set(theirs["env"]), name
        assert server["env"].get("INBOX_LANE") == theirs["env"].get(
            "INBOX_LANE"), name
        assert posixpath.basename(server["command"]) == posixpath.basename(
            theirs["command"]), name


def test_every_path_resolves_under_the_payload_mount_target():
    target = payload_target()
    assert target == "/opt/amap/payload"
    paths = [v for v in strings(ours()) if v.startswith("/")]
    assert paths
    for v in paths:
        assert posixpath.normpath(v) == v
        assert ".." not in v.split("/")
        PurePosixPath(v).relative_to(target)
    for name, server in ours()["mcpServers"].items():
        base = posixpath.basename(server["command"])
        assert server["command"] == f"{target}/bin/{base}", name
        binary = _workspace.CONNECTOR_ROOT / "bin" / base
        assert binary.is_file() and os.access(binary, os.X_OK), binary


def test_lane_locations_come_only_from_amap_mains_lane_variables():
    f = _amap_main.router_facts()
    lane_of = dict(zip(_amap_main.INPUT_LANE_VARS,
                       (f["inbox"], f["peer"], f["outbox"])))
    for v in strings(ours()):
        if v.startswith("/") or not ("$" in v or "/" in v):
            continue
        m = re.match(r"^\$\{(\w+)\}(?:/(.+))?$", v)
        assert m, v
        assert m.group(1) in lane_of, v
        if m.group(2) is not None:
            assert m.group(2) in f["leaves"][lane_of[m.group(1)]], v
    servers = ours()["mcpServers"]
    inbox_leaf = next(x for x in f["leaves"][f["inbox"]]
                      if x.startswith("message"))
    peer_leaf = next(x for x in f["leaves"][f["peer"]]
                     if x.startswith("message"))
    by_lane = {srv["env"]["INBOX_LANE"]: srv for srv in servers.values()
               if "INBOX_LANE" in srv["env"]}
    assert by_lane["mail"]["env"]["INBOX_MESSAGE_DIR"] == \
        "${AMAP_INBOX_DIR}/" + inbox_leaf
    assert by_lane["peer"]["env"]["INBOX_MESSAGE_DIR"] == \
        "${AMAP_PEER_DIR}/" + peer_leaf
    assert servers["inbox-submit"]["env"]["OUTBOX_DIR"] == "${AMAP_OUTBOX_DIR}"


# D23 (IMPLEMENTATION-PLAN.md): the one named difference from sandy's registration.
# Sandy's inbox-submit runs its wrapper `submit-server` (sandy #19), which rebuilds
# AMAP_SELF from /etc/sandy-session.json; an OpenShell sandbox has no such file, so
# this registration runs the connector's own bin/inbox-submit.
D23_SERVER = "inbox-submit"
D23_SANDY_WRAPPER = "submit-server"


def swap_root(obj, root, target):
    if isinstance(obj, str):
        return target + obj[len(root):] if obj.startswith(root + "/") else obj
    if isinstance(obj, dict):
        return {k: swap_root(v, root, target) for k, v in obj.items()}
    if isinstance(obj, list):
        return [swap_root(v, root, target) for v in obj]
    return obj


def registration_problems(theirs, mine, target):
    """One message per way `mine` is not sandy's registration `theirs` with its
    payload root swapped for `target` and D23's one difference."""
    servers = theirs["mcpServers"]
    roots = {posixpath.dirname(posixpath.dirname(s["command"]))
             for s in servers.values()
             if posixpath.basename(posixpath.dirname(s["command"])) == "bin"}
    if len(roots) != 1:
        return [f"sandy's commands under bin/ have {len(roots)} roots, not one"]
    root = next(iter(roots))
    problems = []
    if servers.get(D23_SERVER, {}).get("command") != f"{root}/{D23_SANDY_WRAPPER}":
        problems.append("D23's difference no longer describes sandy's registration")
        return problems
    expected = swap_root(copy.deepcopy(theirs), root, target)
    expected["mcpServers"][D23_SERVER]["command"] = f"{target}/bin/{D23_SERVER}"
    if expected != mine:
        problems.append("this registration is not sandy's with our payload root "
                        "and D23's one difference")
    return problems


def test_it_is_sandys_registration_with_our_payload_root_except_d23():
    assert registration_problems(sandys(), ours(), payload_target()) == []
    assert ours()["mcpServers"][D23_SERVER]["command"] == \
        payload_target() + "/bin/" + D23_SERVER


def test_a_change_to_sandys_registration_fails_instead_of_drifting():
    theirs, mine, target = sandys(), ours(), payload_target()
    assert registration_problems(theirs, mine, target) == []
    root = posixpath.dirname(theirs["mcpServers"][D23_SERVER]["command"])

    def sandy_side(change):
        t = copy.deepcopy(theirs)
        change(t["mcpServers"])
        assert t != theirs
        assert registration_problems(t, mine, target)

    def our_side(change):
        m = copy.deepcopy(mine)
        change(m["mcpServers"])
        assert m != mine
        assert registration_problems(theirs, m, target)

    sandy_side(lambda s: s["delegation"]["env"].update(INBOX_LANE="peers"))
    sandy_side(lambda s: s[D23_SERVER].setdefault("args", []).append("--x"))
    sandy_side(lambda s: s[D23_SERVER]["env"].update(EXTRA_KEY="1"))
    sandy_side(lambda s: s[D23_SERVER].update(
        command=f"{root}/bin/{D23_SERVER}"))
    sandy_side(lambda s: s[D23_SERVER].update(command=f"{root}/other"))
    sandy_side(lambda s: s.update(extra={"command": f"{root}/bin/extra"}))

    our_side(lambda s: s[D23_SERVER].update(
        command=f"{target}/{D23_SANDY_WRAPPER}"))
    our_side(lambda s: s["delegation"].update(command=f"{target}/bin/other"))
    our_side(lambda s: s.pop("delegation"))
    our_side(lambda s: s["delegation"].update(
        command=theirs["mcpServers"]["delegation"]["command"]))


def test_it_names_no_host():
    text = CONFIG.read_text(encoding="utf-8")
    assert "/ho" + "me/" not in text
    assert "/Us" + "ers/" not in text
