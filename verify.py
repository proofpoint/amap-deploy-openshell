"""The `verify` verb: what is installed, what OpenShell runs, and what the agent
can do, reported as PASS, FAIL or UNKNOWN.

Host side only. It reads files, runs `openshell` and `docker`, and runs one
probe inside each sandbox. It writes nothing on the host, has no `--apply`, and
starts no other program (the router's own one-shots, `docker/run.sh` and
`docker/derive-mounts.py`, are run by the router sections in
`router_sections.py`, unchanged).

The discipline's core (the three outcomes, `Unresolved`, `Fact` and `Check`) is
amap-deploy-sandy's agreed core, loaded through `router_health_link`. The
sections, the context and the runner are this repository's copy in
`router_sections`:

- Three outcomes, never two. `Unresolved` is the value of a fact that could not
  be computed: not `None`, and not an empty collection. A comparison that
  involves one is UNKNOWN. `verify` exits 1 for any FAIL or any UNKNOWN, and 0
  only when every check passed.
- Every expected value is a `Fact` with a provenance, built through
  `Ctx.fact`. `tests/test_no_hardcoded_expectations.py` walks this file and
  refuses an int literal above 2 and a string literal that is not a wire name,
  a fact handle, argv or prose.
- Absence is decided by a command that succeeded and listed nothing. A failed
  `sandbox list` is UNKNOWN, never "no stray sandboxes".
- A non-passing check owes the operator a remedy, and `do_not` names the tempting
  wrong fix.

What each member section asks, and how:

- the sandbox exists and its `(workspace, name, id)` is the record in
  `membership.json` (`openshell_cli.find_sandbox`);
- the effective policy (`sandbox get --policy-only`) lists every lane the mount
  table renders on the side it was rendered on, and grants no egress beyond the
  provider's own endpoints. OpenShell adds paths and a `network_policies`
  section of its own (docs/POC-REPORT.md, Pass criterion 3), so the policy is
  compared by inclusion, not by equality;
- the sandbox's container mounts, through `docker inspect`, the rows `render`
  made, with `ro` and `rw` as rendered, and no other bind mount; and no mount
  source but the fleet-wide payload and roster is shared with another member;
- a write into the inbox lane from inside the sandbox fails with the errno L1
  recorded (docs/POC-REPORT.md, Unknown 2: EROFS). The probe writes a scratch
  name and removes it whatever the outcome;
- `inbox-delivery` is running.

No OpenShell log line is expected for a filesystem denial: OpenShell logs none
(FINDINGS.md, "OpenShell does not log filesystem denials"). The second layer is
evidenced by the policy and mount checks, never by a log.

The `inbox-delivery` check reads the sandbox's `/proc` through the payload's own
read-only lister, because the daemon has no host-visible signal: its claims and
state live under the agent's own home. A FAIL (no such process) is decisive. A
PASS is not proof the agent did not stage a process of that name: this check
exists to catch a daemon that has died, not an agent that lies.

The router's one-shot containers are named `amap-openshell-verify-<section>`
(`router_sections.oneshot_name`). They are `--rm` one-shots. The router sections
call the program `--docker` names, through `Ctx.docker`.

The gateway section also holds the mounts interceptor's three checks
(docs/INTERCEPTOR.md section 7). V1 reads `gateway.toml` against the fragment
`gateway-config` prints. V2 asks the running gateway, through `gateway info -o
json`, whether it negotiated the interceptor. V3 asks OpenShell for a create that
every guard should refuse: it binds another member's outbox and carries a key
the Docker driver rejects. If the interceptor is in force it is refused with the
interceptor's own prefix. If one is created, the check FAILs and names the
sandbox: verify never deletes, because it has no `--apply`. V3 is the only
`create` verify ever makes.

Standard library only (plus `interceptor.wire`), and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import posixpath
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import (Any, Callable, Dict, Iterator, List, Mapping, NamedTuple,
                    Optional, Sequence, Tuple)

import gateway
import l1_kit
import l1_run
import membership
import openshell_cli
import policy
import provider_profile
import render
import router_config
import router_link
import router_sections
import verbs
from interceptor import wire
from router_sections import (PASS, FAIL, UNKNOWN, Unresolved, Fact, Check,
                             check, unknown, same_set, Section, difference,
                             report_lines, problem_lines)

PROG = l1_kit.PROG
EXIT_OK, EXIT_PROBLEM = 0, 1
PROBE_MARK = "# amap-openshell verify: "
DEFAULT_DOCKER = "docker"
EXEC_TIMEOUT_S = 60
SECTION_INSTALL, SECTION_ROUTER_CONFIG = "install", "router-config"
SECTION_GATEWAY, SECTION_MEMBERS = "gateway", "members"
SCRATCH_PREFIX = ".amap-verify-"      # the write probe's scratch filename
OPENED = "OPENED"                     # what the write probe prints on success
NETWORK_KEY = "network_policies"
RO_KEY, RW_KEY = "read_only", "read_write"

# Every string another program emits or reads, which this module does not get
# to choose, with the reason. The literal budget admits exactly these inside an
# assertion site, together with the router sections' table.
WIRE_NAMES: Dict[str, str] = {
    "read_only": "a key of the policy's filesystem_policy, as render emits it and "
                 "`sandbox get --policy-only` prints it",
    "read_write": "a key of the policy's filesystem_policy, as render emits it and "
                  "`sandbox get --policy-only` prints it",
    "id": "`sandbox get --output json` key (OpenShell "
          "main@acbac9c:crates/openshell-cli/src/run.rs:2789-2791)",
    "name": "`sandbox get --output json` key (run.rs:2789-2791)",
    "workspace": "`sandbox get --output json` key (run.rs:2789-2791)",
    "phase": "`sandbox get --output json` key (run.rs:2789-2791)",
    "Type": "docker inspect .Mounts row key: the kind of mount",
    "bind": "docker inspect's spelling of a bind mount in .Mounts",
    NETWORK_KEY: "the top-level key of the effective policy that holds network "
                 "rules, as L1 observed it (docs/POC-REPORT.md, Pass criterion 3, "
                 "the `sandbox get --policy-only` evidence)",
    OPENED: "what this repo's write probe prints when the open succeeded: a "
            "name this module chooses and its reader must know",
}
for _name in WIRE_NAMES:
    if _name in router_sections.WIRE_NAMES:
        raise ImportError(f"WIRE_NAMES restates router_sections' {_name!r}: one "
                          f"home for each name")
ALL_WIRE_NAMES: Dict[str, str] = {**router_sections.WIRE_NAMES, **WIRE_NAMES}


# --- the probes run inside a sandbox -----------------------------------------

WRITE_PROBE = PROBE_MARK + "write-probe\n" + '''import errno, os, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
else:
    try:
        os.close(fd)
        print("OPENED")
    finally:
        os.unlink(path)
'''

DELIVERY_PROBE = PROBE_MARK + "delivery-probe\n" + '''import importlib.machinery, importlib.util, sys
sys.dont_write_bytecode = True
lister_path, name = sys.argv[1], sys.argv[2]
loader = importlib.machinery.SourceFileLoader("openshell_sessions", lister_path)
spec = importlib.util.spec_from_loader("openshell_sessions", loader)
lister = importlib.util.module_from_spec(spec)
loader.exec_module(lister)
table = lister.process_table(lister.PROC)
print(sum(1 for pid in sorted(table)
          if lister.runs(lister.PROC, table, pid, name)))
'''


# --- the runner --------------------------------------------------------------

class Probe:
    """The only place `verify` starts a program: `openshell` and `docker`."""

    def __init__(self, client: openshell_cli.Client, docker_binary: str,
                 env: Mapping[str, str], timeout: float = 120.0) -> None:
        self.client = client
        self._docker = openshell_cli.Client(docker_binary, "", env, timeout)

    def openshell(self, *words: str) -> openshell_cli.Call:
        return self.client.run(self.client.base(*words))

    def exec_in(self, name: str, command: Sequence[str]) -> openshell_cli.Call:
        """`sandbox exec`: no terminal, no login shell, and a timeout (OpenShell
        main@acbac9c:crates/openshell-cli/src/main.rs:1681-1727)."""
        return self.openshell("sandbox", "exec", "--name", name, "--no-tty",
                              "--no-login-shell", "--timeout",
                              str(EXEC_TIMEOUT_S), "--", *command)

    def gateway_info(self) -> openshell_cli.Call:
        """`gateway info -o json` (OpenShell
        v0.1.2:crates/openshell-cli/src/main.rs:1376-1381)."""
        return self.client.run([self.client.binary, "gateway", "info", "-o",
                                "json"])

    def docker(self, *words: str) -> openshell_cli.Call:
        return self._docker.run([self._docker.binary, *words])


class ProbeOutcome(NamedTuple):
    """What V3's create did: a verdict (one of `wire.PROBE_*`), the probe
    sandbox's name, and the first line of what OpenShell said."""
    verdict: str
    name: str
    detail: str


class Options(NamedTuple):
    home: str
    image: str
    run_as: str
    restart_policy: bool
    openshell: str
    docker: str
    gateway_toml: Optional[str]
    router_container: str
    router_image: str
    env: Mapping[str, str]


def options_from(args: argparse.Namespace,
                 env: Optional[Mapping[str, str]] = None) -> Options:
    h = verbs.host_args(args, env)
    toml = getattr(args, "gateway_toml", None) or gateway.gateway_toml_path(h.env)
    return Options(
        h.home, h.image, h.run_as, h.restart_policy, h.openshell,
        getattr(args, "docker", None) or DEFAULT_DOCKER, toml,
        getattr(args, "router_container", None) or h.env.get("CONTAINER")
        or l1_run.ROUTER_CONTAINER,
        getattr(args, "router_image", None) or h.env.get("IMAGE")
        or l1_run.ROUTER_CONTAINER,
        dict(h.env))


class Absent:
    """A sandbox OpenShell listed no entry for, answered by a command that
    succeeded. Not `Unresolved`: that is a sandbox nobody could ask about."""

    def __repr__(self) -> str:
        return "ABSENT"


# --- facts -------------------------------------------------------------------
#
# Every derivation is a function of the Ctx. A source that cannot answer returns
# `Unresolved(reason)`: never None, and never an empty collection.

def _unresolved(reason: str) -> Any:
    return Unresolved(reason)


def _inherit(ctx: "Ctx", *names: str) -> Optional[Tuple[Any, str]]:
    """The first of `names` that is Unresolved, as a fact's own answer."""
    for name in names:
        fact = ctx.fact(name)
        if not fact.known:
            return fact.value, fact.provenance
    return None


def _f_home(ctx):
    return ctx.opts.home, "--home (or $AMAP_OPENSHELL_HOME)"


def _f_fleet(ctx):
    path = l1_kit.fleet_json(ctx.opts.home)
    try:
        return policy.load_fleet(path), path
    except (policy.PolicyError, OSError, ValueError) as e:
        return _unresolved(f"{path} cannot be loaded: {e}"), path


def _f_workspace(ctx):
    gone = _inherit(ctx, "fleet")
    if gone:
        return gone
    return (policy.workspace_of(ctx.value("fleet")),
            "the workspace key of fleet.json")


def _f_fleet_members(ctx):
    gone = _inherit(ctx, "fleet")
    if gone:
        return gone
    try:
        names = l1_kit.fleet_members(ctx.value("fleet"))
    except l1_kit.KitError as e:
        return _unresolved(str(e)), ctx.fact("fleet").provenance
    return sorted(names), "the instances fleet.json names"


def _f_members(ctx):
    path = l1_kit.membership_json(ctx.opts.home)
    try:
        listed = membership.load(path)
    except (membership.MembershipError, OSError) as e:
        return _unresolved(f"{path} cannot be read: {e}"), path
    if listed is None:
        return (_unresolved(f"{path} is absent: nothing has been recorded"),
                path)
    return {m.name: m for m in listed}, path


def _f_host(ctx):
    opts = ctx.opts
    try:
        host = render.Host(opts.home, opts.image,
                           render.parse_run_as(opts.run_as),
                           opts.restart_policy)
        reason = render.host_problem(host)
    except render.RenderError as e:
        return _unresolved(str(e)), "--home, --image, --run-as"
    if reason:
        return _unresolved(reason), "--home, --image, --run-as"
    return host, "--home, --image, --run-as (the host-wide options)"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mode_text(mode: int) -> str:
    return f"{mode:04o}"


def _f_payload_sources(ctx):
    try:
        connector = l1_kit.find_connector(ctx.opts.env)
        rows = l1_kit.read_payload(connector)
    except l1_kit.KitError as e:
        return _unresolved(str(e)), "the connector checkout and this repo's payload/"
    return ({rel: (_digest(data), _mode_text(mode)) for rel, data, mode in rows},
            "l1_kit.payload_sources: the connector's bin/ and this repo's payload/")


def _f_installed_payload(ctx):
    root = render.payload_dir(ctx.opts.home)
    where = f"the files under {root}"
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return {}, where + " (the directory is absent)"
    except OSError as e:
        return _unresolved(f"cannot look at {root}: {e}"), where
    if not stat.S_ISDIR(st.st_mode):
        return {}, where + " (it is not a real directory)"
    errors: List[OSError] = []
    found: Dict[str, Tuple[str, str]] = {}
    for base, _dirs, names in os.walk(root, onerror=errors.append):
        for n in names:
            path = os.path.join(base, n)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    found[rel] = ("symlink", os.readlink(path))
                else:
                    with open(path, "rb") as fh:
                        found[rel] = (_digest(fh.read()),
                                      _mode_text(stat.S_IMODE(info.st_mode)))
            except OSError as e:
                return _unresolved(f"cannot read {path}: {e}"), where
    if errors:
        return (_unresolved(f"cannot list {errors[0].filename}: "
                            f"{errors[0].strerror}"), where)
    return found, where


def _f_install_dirs(ctx):
    home = ctx.opts.home
    payload = render.payload_dir(home)
    return ([payload, posixpath.join(payload, l1_kit.PAYLOAD_BIN_DIRNAME),
             render.roster_dir(home), render.instances_dir(home),
             posixpath.join(home, l1_kit.POLICIES_DIRNAME),
             posixpath.join(home, l1_kit.COMMANDS_DIRNAME),
             router_config.default_state_dir(home)],
            "the directories `install` and `provision` write under --home")


def _f_rendered_router(ctx):
    gone = _inherit(ctx, "fleet", "members")
    if gone:
        return gone
    try:
        rendered = router_config.render_router_json(
            ctx.value("fleet"), list(ctx.value("members").values()),
            ctx.opts.home)
    except (router_config.RouterConfigError, render.RenderError) as e:
        return (_unresolved(f"router.json cannot be rendered: {e}"),
                "router_config.render_router_json")
    return rendered, "router_config.render_router_json over fleet.json and membership.json"


def _f_rendered_verdict(ctx):
    gone = _inherit(ctx, "members", "workspace")
    if gone:
        return gone
    try:
        doc = router_config.render_verdict(list(ctx.value("members").values()),
                                           ctx.value("workspace"))
    except router_config.RouterConfigError as e:
        return _unresolved(f"selected.json cannot be rendered: {e}"), "router_config.render_verdict"
    return doc, "router_config.render_verdict over membership.json"


def _read_text(path: str) -> Any:
    """The file's text, None when it is absent, or an Unresolved."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        return _unresolved(f"cannot read {path}: {e}")


def _f_router_drift(ctx):
    gone = _inherit(ctx, "rendered_router")
    if gone:
        return gone
    path = router_config.router_json_path(ctx.opts.home)
    have = _read_text(path)
    if isinstance(have, Unresolved):
        return have, path
    return (router_config.drift_on_disk(path, ctx.value("rendered_router")),
            f"router_config.drift_on_disk of {path} against the rendering")


def _f_verdict_drift(ctx):
    gone = _inherit(ctx, "rendered_verdict")
    if gone:
        return gone
    path = render.selected_json(ctx.opts.home)
    want = ctx.value("rendered_verdict")
    have = _read_text(path)
    where = f"{path} against router_config.render_verdict"
    if isinstance(have, Unresolved):
        return have, where
    if have is None:
        return [f"{path} is absent: render it to create it"], where
    if have == router_config.json_text(want):
        return [], where
    try:
        doc = json.loads(have)
    except ValueError as e:
        return [f"{path} is not valid JSON: {e}"], where
    if doc == want:
        return ["same content, different bytes (whitespace or key order)"], where
    return [f"{path} differs from the rendering: it holds {doc!r}, the "
            f"rendering is {want!r}"], where


def _f_gateway_reading(ctx):
    path = ctx.opts.gateway_toml
    return gateway.read_gateway_toml(path), f"gateway.toml: {path or 'not located'}"


def _rendered_mount_rows(ctx, name):
    return render.mount_table(ctx.value("host"), name)


def _real(path: str) -> str:
    return os.path.realpath(path)


def _f_rendered_mounts(ctx):
    gone = _inherit(ctx, "host", "fleet_members")
    if gone:
        return gone
    try:
        return ({n: {row.target: (_real(row.source), not row.read_only)
                     for row in _rendered_mount_rows(ctx, n)}
                 for n in ctx.value("fleet_members")},
                "render.mount_table: the one table the create command is built from")
    except render.RenderError as e:
        return _unresolved(str(e)), "render.mount_table"


def _f_rendered_policy(ctx):
    """Each lane, the payload and the roster, on the side render put it on. The
    system paths are not listed: OpenShell adds to them."""
    gone = _inherit(ctx, "host", "fleet_members")
    if gone:
        return gone
    out: Dict[str, Dict[str, List[str]]] = {}
    try:
        for n in ctx.value("fleet_members"):
            rows = _rendered_mount_rows(ctx, n)
            targets = {row.target for row in rows}
            fs = render.filesystem_policy(rows)
            out[n] = {side: [t for t in fs[side] if t in targets]
                      for side in (RO_KEY, RW_KEY)}
    except render.RenderError as e:
        return _unresolved(str(e)), "render.filesystem_policy"
    return out, "render.filesystem_policy over render.mount_table"





def _f_policy_keys(ctx):
    return (list(render.POLICY_KEYS) + [NETWORK_KEY],
            "render.POLICY_KEYS, and the network section OpenShell adds for "
            "the attached provider (docs/POC-REPORT.md, Pass criterion 3)")


def _f_provider_hosts(ctx):
    try:
        template = provider_profile.load_template()
    except provider_profile.ProfileError as e:
        return _unresolved(str(e)), "providers/amap-claude-code.json"
    hosts = sorted({str(e.get("host")) for e in template.get("endpoints") or []
                    if isinstance(e, dict) and e.get("host")})
    if not hosts:
        return (_unresolved("the provider profile names no endpoint"),
                "providers/amap-claude-code.json")
    return hosts, "the endpoints of providers/amap-claude-code.json"


def _f_sandbox_docs(ctx):
    gone = _inherit(ctx, "fleet_members", "workspace")
    if gone:
        return gone
    out: Dict[str, Any] = {}
    for n in ctx.value("fleet_members"):
        state, doc, why = openshell_cli.find_sandbox(ctx.probe.client, n)
        if state == "present":
            out[n] = doc
        elif state == "absent":
            out[n] = Absent()
        else:
            out[n] = _unresolved(why)
    return out, "`sandbox get NAME --output json`, confirmed by `sandbox list`"


def _f_listed_names(ctx):
    gone = _inherit(ctx, "workspace")
    if gone:
        return gone
    names, why = ctx.probe.client.list_names()
    where = "`sandbox list --all-workspaces --names`"
    if names is None:
        return _unresolved(why), where
    prefix = ctx.value("workspace") + "/"
    return sorted(n[len(prefix):] for n in names if n.startswith(prefix)), where


def _f_strays(ctx):
    gone = _inherit(ctx, "listed_names", "fleet_members")
    if gone:
        return gone
    have = set(ctx.value("fleet_members"))
    return ([n for n in ctx.value("listed_names") if n not in have],
            "the workspace's listed sandboxes that fleet.json does not name")


def _member_doc(ctx, name: str) -> Any:
    """The member's `sandbox get` document, or an Unresolved that says why no
    per-sandbox question can be asked."""
    base = ctx.fact("sandbox_docs")
    if not base.known:
        return base.value
    doc = base.value.get(name)
    if isinstance(doc, Absent):
        return _unresolved(f"OpenShell has no sandbox named {name}")
    if doc is None:
        return _unresolved(f"{name} is not a member the fleet names")
    return doc


def _bind_rows(text: str) -> Any:
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _member_mounts(ctx, name: str) -> Any:
    doc = _member_doc(ctx, name)
    if isinstance(doc, Unresolved):
        return doc
    ps = ctx.probe.docker("ps", "-q", "--filter",
                          f"label={l1_run.SANDBOX_ID_LABEL}={doc.get('id')}")
    if ps.code != 0:
        return _unresolved(f"`docker ps` failed: "
                           f"{openshell_cli.first_line(ps.stderr)}")
    containers = [ln.strip() for ln in ps.stdout.splitlines() if ln.strip()]
    if not containers:
        return _unresolved(f"docker lists no running container for {name}'s "
                           f"sandbox ID")
    found: Dict[str, Tuple[str, bool]] = {}
    for cid in containers:
        ins = ctx.probe.docker("inspect", "--format", "{{json .Mounts}}", cid)
        rows = _bind_rows(ins.stdout) if ins.code == 0 else None
        if rows is None:
            return _unresolved(f"`docker inspect` of {name}'s container gave "
                               f"no mount list")
        for row in rows:
            if not isinstance(row, dict) or row.get("Type") != "bind":
                continue
            dest = str(row.get("Destination") or "")
            if dest in found:
                return _unresolved(f"several containers of {name}'s sandbox "
                                   f"mount {dest}")
            found[dest] = (_real(str(row.get("Source") or "")),
                           bool(row.get("RW")))
    return found


def _f_actual_mounts(ctx):
    gone = _inherit(ctx, "fleet_members", "sandbox_docs")
    if gone:
        return gone
    return ({n: _member_mounts(ctx, n) for n in ctx.value("fleet_members")},
            "`docker ps` by the sandbox-id label, then `docker inspect` of each "
            "container's bind mounts")


def _f_shared_sources(ctx):
    """`[(source, [members...])]` for every source more than one member mounts,
    other than the fleet-wide read-only rows. Unresolved when any member's mounts
    are unknown: the sources that could be shared are among the ones not seen."""
    gone = _inherit(ctx, "actual_mounts", "host", "fleet_members")
    if gone:
        return gone
    mounts = ctx.value("actual_mounts")
    for n, rows in mounts.items():
        if isinstance(rows, Unresolved):
            return (_unresolved(f"{n}'s mounts are unknown: {rows.reason}"),
                    ctx.fact("actual_mounts").provenance)
    first = ctx.value("fleet_members")[0]
    wide = {row.target for row in _rendered_mount_rows(ctx, first)
            if row.role in (render.PAYLOAD_DIRNAME, render.ROSTER_DIRNAME)}
    by_source: Dict[str, List[str]] = {}
    for n in sorted(mounts):
        for dest, (source, _rw) in mounts[n].items():
            if dest not in wide:
                by_source.setdefault(source, []).append(n)
    return ([(s, sorted(set(ms))) for s, ms in sorted(by_source.items())
             if len(set(ms)) > 1],
            "the bind mounts docker reports, minus the fleet-wide payload and roster")


_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):")
_HOST_LINE = re.compile(r"^\s*(?:-\s+)?host:\s*[\"']?([^\"'\s]+)[\"']?\s*$")


def _policy_view(text: str) -> Any:
    ro = l1_run.yaml_block_list(text, RO_KEY)
    rw = l1_run.yaml_block_list(text, RW_KEY)
    if ro is None or rw is None:
        return None
    keys = [m.group(1) for m in map(_TOP_KEY.match, text.splitlines()) if m]
    hosts = [m.group(1) for m in map(_HOST_LINE.match, text.splitlines()) if m]
    return {RO_KEY: ro, RW_KEY: rw, "top_keys": keys, "hosts": sorted(set(hosts))}


def _member_policy(ctx, name: str) -> Any:
    doc = _member_doc(ctx, name)
    if isinstance(doc, Unresolved):
        return doc
    r = ctx.probe.openshell("sandbox", "get", name, "--policy-only")
    if r.code != 0:
        return _unresolved(f"`sandbox get {name} --policy-only` failed: "
                           f"{openshell_cli.first_line(r.stderr)}")
    view = _policy_view(r.stdout)
    if view is None:
        return _unresolved("the policy text has no block-style read_only and "
                           "read_write lists")
    return view


def _f_effective_policy(ctx):
    gone = _inherit(ctx, "fleet_members", "sandbox_docs")
    if gone:
        return gone
    return ({n: _member_policy(ctx, n) for n in ctx.value("fleet_members")},
            "`sandbox get NAME --policy-only`")


def _f_egress_excess(ctx):
    """What each member's effective policy grants beyond this deployment's
    rendering and the provider's own endpoints."""
    gone = _inherit(ctx, "effective_policy", "policy_keys", "provider_hosts")
    if gone:
        return gone
    keys, hosts = ctx.value("policy_keys"), ctx.value("provider_hosts")
    out: Dict[str, Any] = {}
    for n, view in ctx.value("effective_policy").items():
        if isinstance(view, Unresolved):
            out[n] = view
            continue
        out[n] = ([f"key {k}" for k in view["top_keys"] if k not in keys]
                  + [f"host {h}" for h in view["hosts"] if h not in hosts])
    return out, ("the effective policy's top-level keys and endpoint hosts, "
                 "against policy_keys and provider_hosts")


def _f_denied_errno(ctx):
    return (errno.errorcode[errno.EROFS],
            "the errno L1 recorded for a write into the inbox lane "
            "(docs/POC-REPORT.md, Unknown 2 and Pass criterion 3), by the "
            "standard library's own errno table")


def _answer_line(out: str) -> str:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _member_write(ctx, name: str) -> Any:
    doc = _member_doc(ctx, name)
    if isinstance(doc, Unresolved):
        return doc
    target = posixpath.join(render.lane_target(l1_run.LANE_INBOX),
                            SCRATCH_PREFIX + secrets.token_hex(8))
    r = ctx.probe.exec_in(name, ["python3", "-c", WRITE_PROBE, target])
    word = _answer_line(r.stdout)
    if r.code != 0 and word != OPENED:
        return _unresolved(f"`sandbox exec` in {name} failed: "
                           f"{openshell_cli.first_line(r.stderr or r.stdout)}")
    if not word:
        return _unresolved(f"the write probe in {name} printed nothing")
    return word


def _f_write_probe(ctx):
    gone = _inherit(ctx, "fleet_members", "sandbox_docs")
    if gone:
        return gone
    return ({n: _member_write(ctx, n) for n in ctx.value("fleet_members")},
            "an open for writing of a scratch name in the inbox lane, inside "
            "the sandbox")


def _member_deliveries(ctx, name: str) -> Any:
    doc = _member_doc(ctx, name)
    if isinstance(doc, Unresolved):
        return doc
    r = ctx.probe.exec_in(name, ["python3", "-c", DELIVERY_PROBE, l1_run.LISTER,
                                 l1_kit.DELIVERY_NAME])
    if r.code != 0:
        return _unresolved(f"`sandbox exec` in {name} failed: "
                           f"{openshell_cli.first_line(r.stderr or r.stdout)}")
    word = _answer_line(r.stdout)
    if not word.isdigit():
        return _unresolved(f"the delivery probe in {name} printed {word!r}, "
                           f"not a count")
    return int(word)


def _f_delivery_counts(ctx):
    gone = _inherit(ctx, "fleet_members", "sandbox_docs")
    if gone:
        return gone
    return ({n: _member_deliveries(ctx, n) for n in ctx.value("fleet_members")},
            "the payload's own lister, run inside each sandbox: processes "
            "running inbox-delivery")


def _f_router_repo(ctx):
    try:
        return router_link.find_router(ctx.opts.env), "router_link.find_router"
    except router_link.RouterNotFound as e:
        return _unresolved(str(e)), "router_link.find_router"


def _f_router_config(ctx):
    return (router_config.router_json_path(ctx.opts.home),
            "router.json under --home: the file `router-config` renders")


def _f_container(ctx):
    return (ctx.opts.router_container,
            "--router-container (default: $CONTAINER, as run.sh reads it)")


def _f_image(ctx):
    return (ctx.opts.router_image,
            "--router-image (default: $IMAGE, as run.sh reads it)")


# --- the mounts interceptor (docs/INTERCEPTOR.md section 7) ------------------

def _f_interceptor_name(ctx):
    return (wire.INTERCEPTOR_NAME,
            "interceptor/wire.py INTERCEPTOR_NAME: the name the manifest and "
            "the fragment declare")


def _f_interceptor_registration(ctx):
    rows = gateway.interceptor_report(ctx.value("gateway_reading"),
                                      ctx.opts.home)
    for claim, verdict, why in rows:
        if verdict == gateway.UNKNOWN:
            return _unresolved(why), "gateway.toml"
    return ([claim for claim, verdict, _ in rows
             if verdict != gateway.PRESENT],
            "gateway.toml against wire.gateway_fragment(--home)")


def _f_negotiated_interceptors(ctx):
    where = "`gateway info -o json`: the extensions the running gateway loaded"
    r = ctx.probe.gateway_info()
    if r.code != 0:
        return (_unresolved(f"`gateway info` failed: "
                            f"{openshell_cli.first_line(r.stderr)}"), where)
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return _unresolved("`gateway info` printed no JSON"), where
    exts = doc.get("extensions") if isinstance(doc, dict) else None
    if not isinstance(exts, list):
        return _unresolved("`gateway info` has no extensions list"), where
    return (sorted(str(e.get("configured_name")) for e in exts
                   if isinstance(e, dict)
                   and e.get("kind") == wire.EXTENSION_KIND), where)


def _f_probe_refused(ctx):
    return (wire.PROBE_REFUSED,
            "a create that binds another member's outbox, refused with "
            "wire.REASON_PREFIX and that outbox's path in the reason")


_WRAPPING = re.compile("[\\s\u2502\u00d7]+")


def _squash(text: str) -> str:
    """`text` without whitespace or the CLI's box-drawing characters."""
    return _WRAPPING.sub("", text)


def _f_probe_outcome(ctx):
    where = ("`sandbox create` of a probe that binds another member's outbox "
             "and carries a key the Docker driver refuses")
    gone = _inherit(ctx, "host", "fleet_members", "workspace")
    if gone:
        return gone
    member = ctx.value("fleet_members")[0]
    host = ctx.value("host")
    outbox = render.lane_dir(ctx.opts.home, member, render.LANE_OUTBOX)
    name = wire.PROBE_PREFIX + secrets.token_hex(3)
    cfg = render.driver_config([render.Mount(
        render.LANE_OUTBOX, outbox, wire.PROBE_TARGET, False)])
    cfg[render.DRIVER][wire.PROBE_POISON_KEY] = True
    r = ctx.probe.openshell(
        "sandbox", "create", "--name", name, "--from", host.image,
        "--driver-config-json",
        json.dumps(cfg, sort_keys=True, separators=(",", ":")),
        "--detach", "--no-tty", "--no-auto-providers", "--",
        *wire.PROBE_COMMAND)
    if r.code is None:
        return _unresolved(f"`openshell` cannot run: "
                           f"{openshell_cli.first_line(r.stderr)}"), where
    text = r.stdout + r.stderr
    # The CLI wraps an error to the terminal's width, or narrower off a
    # terminal, breaking at spaces and after hyphens and drawing a bar before
    # each continuation line (seen live on the test host, 2026-10-05). So match with
    # whitespace and the box-drawing characters removed from both sides.
    flat = _squash(text)
    if r.code == 0:
        verdict = wire.PROBE_CREATED
    elif _squash(wire.REASON_PREFIX) in flat and _squash(outbox) in flat:
        verdict = wire.PROBE_REFUSED
    elif _squash(wire.REASON_PREFIX) in flat:
        verdict = wire.PROBE_REFUSED_OTHER
    elif _squash(wire.FAILED_CLOSED_MARK) in flat:
        verdict = wire.PROBE_FAILED_CLOSED
    else:
        verdict = wire.PROBE_BY_GATEWAY
    return ProbeOutcome(verdict, name, openshell_cli.first_line(text)), where


_OURS: Dict[str, Callable[["Ctx"], Tuple[Any, str]]] = {
    "home": _f_home, "fleet": _f_fleet, "workspace": _f_workspace,
    "fleet_members": _f_fleet_members, "members": _f_members, "host": _f_host,
    "payload_sources": _f_payload_sources,
    "installed_payload": _f_installed_payload, "install_dirs": _f_install_dirs,
    "rendered_router": _f_rendered_router,
    "rendered_verdict": _f_rendered_verdict, "router_drift": _f_router_drift,
    "verdict_drift": _f_verdict_drift, "gateway_reading": _f_gateway_reading,
    "rendered_mounts": _f_rendered_mounts, "rendered_policy": _f_rendered_policy,
    "policy_keys": _f_policy_keys, "provider_hosts": _f_provider_hosts,
    "sandbox_docs": _f_sandbox_docs, "listed_names": _f_listed_names,
    "strays": _f_strays, "actual_mounts": _f_actual_mounts,
    "shared_sources": _f_shared_sources,
    "effective_policy": _f_effective_policy, "egress_excess": _f_egress_excess,
    "denied_errno": _f_denied_errno, "write_probe": _f_write_probe,
    "delivery_counts": _f_delivery_counts, "router_repo": _f_router_repo,
    "router_config": _f_router_config, "container": _f_container,
    "image": _f_image, "interceptor_name": _f_interceptor_name,
    "interceptor_registration": _f_interceptor_registration,
    "negotiated_interceptors": _f_negotiated_interceptors,
    "probe_refused": _f_probe_refused, "probe_outcome": _f_probe_outcome,
}

for _name in _OURS:
    if _name in router_sections.FACT_SOURCES:
        raise ImportError(f"_OURS restates router_sections' derivation "
                          f"{_name!r}: one home for each derivation")
FACT_SOURCES: Dict[str, Callable[["Ctx"], Tuple[Any, str]]] = {
    **router_sections.FACT_SOURCES,
    **_OURS,
}


class Ctx(router_sections.Ctx):
    """router_sections' Ctx, with this deployment's facts supplied."""

    def __init__(self, opts: Options, probe: Probe) -> None:
        super().__init__(_OURS, docker=opts.docker)
        self.opts = opts
        self.probe = probe

    def of(self, name: str, member: str) -> "Fact":
        """The part of a per-member fact that is `member`'s."""
        base = self.fact(name)
        label = f"{name}[{member}]"
        if not base.known:
            return Fact(label, base.value, base.provenance)
        if member not in base.value:
            return Fact(label, _unresolved(f"{name} has no entry for {member}"),
                        base.provenance)
        return Fact(label, base.value[member], base.provenance)


# --- sections ----------------------------------------------------------------

def _map(value: Any, fn: Callable[[Any], Any]) -> Any:
    """`fn(value)`, or the Unresolved itself."""
    return value if isinstance(value, Unresolved) else fn(value)


def verify_install(ctx: Ctx) -> Iterator[Check]:
    yield check(
        claim="the payload on disk is byte for byte the connector's and this "
              "repo's",
        expected=ctx.fact("payload_sources"),
        actual=ctx.value("installed_payload"),
        remedy="run `install --apply`: it writes the payload again from its "
               "sources",
        do_not="do not edit a file under payload/: it is a copy, and "
               "`install --apply` overwrites it")
    dirs = ctx.fact("install_dirs")
    yield check(
        claim="the layout install writes is present",
        expected=dirs,
        actual=[d for d in dirs.value
                if os.path.isdir(d) and not os.path.islink(d)],
        ok=same_set,
        remedy="run `install --apply`, and `provision NAME --apply` for any "
               "member not yet provisioned")
    recorded = ctx.value("members")
    yield check(
        claim="every member the fleet names is recorded",
        expected=ctx.fact("fleet_members"),
        actual=_map(recorded, sorted),
        ok=same_set,
        remedy="run `provision NAME --apply` for each member that is not "
               "recorded; `list` shows the difference",
        do_not="do not write membership.json by hand: the ID in it must be "
               "the one OpenShell reports")
    yield check(
        claim="no sandbox in the workspace is outside the fleet",
        expected=ctx.fact("empty"),
        actual=ctx.value("strays"),
        remedy="a sandbox the fleet does not name is either a member missing "
               "from fleet.json or one to delete; `list` names it",
        do_not="do not treat a failed `sandbox list` as an empty workspace")


def verify_router_config(ctx: Ctx) -> Iterator[Check]:
    yield check(
        claim="router.json is what this fleet renders",
        expected=ctx.fact("empty"),
        actual=ctx.value("router_drift"),
        remedy="run `router-config --apply` to render it again",
        do_not="do not edit router.json by hand: the next render loses the edit")
    yield check(
        claim="selected.json is what this fleet renders",
        expected=ctx.fact("empty"),
        actual=ctx.value("verdict_drift"),
        remedy="run `router-config --apply` to render it again",
        do_not="do not edit selected.json by hand: it is the router's "
               "admission list, and the next render loses the edit")


def verify_gateway(ctx: Ctx) -> Iterator[Check]:
    reading = ctx.value("gateway_reading")
    for setting, verdict, why in gateway.settings_report(reading):
        claim = f"the gateway sets {setting}"
        if verdict == gateway.UNKNOWN:
            yield unknown(claim=claim, expected=ctx.fact("true"), reason=why,
                          remedy="make gateway.toml readable, or name it with "
                                 "--gateway-toml")
        else:
            yield check(claim=claim, expected=ctx.fact("true"),
                        actual=verdict == gateway.PRESENT,
                        remedy="add it to the gateway's gateway.toml as "
                               "`gateway-config` prints it, then restart the "
                               "gateway",
                        do_not="do not judge it from the gateway's behaviour: "
                               "this reads the file")
    yield from _interceptor_checks(ctx)


def _probe_remedy(o: Any) -> str:
    """What to do about V3's outcome. Prose only: not an assertion site."""
    if isinstance(o, Unresolved):
        return ("make `openshell` runnable and name a fleet member, then run "
                "verify again: no probe was made")
    if o.verdict == wire.PROBE_CREATED:
        return (f"the probe sandbox {o.name} was CREATED, so nothing refused "
                f"a mount of another member's outbox. Delete it with "
                f"`openshell sandbox delete {o.name}`, then register the "
                f"interceptor as `gateway-config --home ...` prints and "
                f"restart the gateway")
    return (f"the probe {o.name} was not refused by the interceptor ({o.verdict}): "
            f"{o.detail}. Start interceptor/server.py, register it as "
            f"`gateway-config --home ...` prints, and restart the gateway")


def _interceptor_checks(ctx: Ctx) -> Iterator[Check]:
    """V1 to V3. Not a `verify_*` function: it is part of the gateway section."""
    yield check(
        claim="gateway.toml registers the mounts interceptor as rendered",
        expected=ctx.fact("empty"),
        actual=ctx.value("interceptor_registration"),
        remedy="add the block `gateway-config --home ...` prints to "
               "gateway.toml, start interceptor/server.py, then restart the "
               "gateway",
        do_not="do not set fail_open or `disabled = true` to get a create "
               "through: that removes the guard")
    yield check(
        claim="the running gateway negotiated the mounts interceptor",
        expected=ctx.fact("interceptor_name"),
        actual=ctx.value("negotiated_interceptors"),
        ok=lambda want, got: want in got,
        remedy="start interceptor/server.py before the gateway, then restart "
               "the gateway",
        do_not="do not read a PASS here as proof the binding is in force: "
               "`gateway info` lists no bindings, and only the probe below "
               "proves that")
    o = ctx.value("probe_outcome")
    yield check(
        claim="a create that mounts another member's outbox is refused by "
              "the interceptor",
        expected=ctx.fact("probe_refused"),
        actual=_map(o, lambda x: x.verdict),
        remedy=_probe_remedy(o),
        do_not="do not expect verify to delete a probe it made: it has no "
               "--apply")


def _lanes_hold(expected: Mapping[str, Sequence[str]],
                actual: Mapping[str, Sequence[str]]) -> bool:
    """Every rendered path is on the side it was rendered on, and not on the
    other."""
    sides = (RO_KEY, RW_KEY)
    for side, other in (sides, sides[::-1]):
        for path in expected[side]:
            if path not in actual[side] or path in actual[other]:
                return False
    return True


def verify_members(ctx: Ctx) -> Iterator[Check]:
    names = ctx.value("fleet_members")
    if isinstance(names, Unresolved):
        yield unknown(claim="the members can be listed from fleet.json",
                      expected=ctx.fact("true"), reason=names.reason,
                      remedy="run `install --apply`, or fix fleet.json")
        return
    for name in names:
        yield from _verify_member(ctx, name)


def _verify_member(ctx: Ctx, name: str) -> Iterator[Check]:
    doc = ctx.of("sandbox_docs", name).value
    recorded = ctx.value("members")
    claim = (f"{name}: the sandbox exists and its (workspace, name, id) is "
             f"the record")
    if isinstance(recorded, Unresolved):
        yield unknown(claim=claim, expected=ctx.fact("true"),
                      reason=recorded.reason,
                      remedy="run `provision NAME --apply`")
    elif name not in recorded:
        yield check(claim=claim, expected=ctx.fact("true"), actual=False,
                    remedy=f"{name} is not recorded in membership.json: run "
                           f"`provision {name} --apply`")
    elif isinstance(doc, Absent):
        yield check(claim=claim, expected=ctx.fact("true"), actual=False,
                    remedy=f"membership.json records {name} as "
                           f"{recorded[name].id}, but OpenShell has no sandbox "
                           f"of that name. That ID cannot come back: run "
                           f"`deprovision {name} --apply`, then provision it "
                           f"again")
    elif isinstance(doc, Unresolved):
        yield unknown(claim=claim, expected=ctx.fact("true"), reason=doc.reason,
                      remedy="make `openshell sandbox get` answer, then run "
                             "verify again")
    else:
        observed = membership.Member(str(doc.get("workspace")),
                                     str(doc.get("name")), str(doc.get("id")))
        problem = (openshell_cli.identity_problem(doc, name,
                                                  ctx.value("workspace"))
                   or membership.check(recorded.values(), observed))
        yield check(claim=claim, expected=ctx.of("members", name),
                    actual=observed, ok=lambda want, got: problem is None,
                    remedy=f"{problem}. Run `deprovision {name} --apply`, "
                           f"then provision it again",
                    do_not="do not edit the recorded ID to match: OpenShell "
                           "has no rename, so a different ID is a different "
                           "sandbox")

    yield check(
        claim=f"{name}: the effective policy lists every lane on the side it "
              f"was rendered on",
        expected=ctx.of("rendered_policy", name),
        actual=ctx.of("effective_policy", name).value,
        ok=_lanes_hold,
        remedy=f"re-create {name}: `deprovision {name} --apply`, then "
               f"`provision {name} --apply`. A policy is applied at create time",
        do_not="do not edit the policy file and expect the sandbox to follow: "
               "the file is what create reads")
    yield check(
        claim=f"{name}: the effective policy grants no egress beyond the "
              f"provider's endpoints",
        expected=ctx.fact("empty"),
        actual=ctx.of("egress_excess", name).value,
        remedy=f"re-create {name} from the rendered policy; nothing but the "
               f"provider may open a network path",
        do_not="do not allow a mail host in the policy: mail leaves only "
               "through the outbox")
    yield check(
        claim=f"{name}: the container mounts the rendered table, ro and rw as "
              f"rendered",
        expected=ctx.of("rendered_mounts", name),
        actual=ctx.of("actual_mounts", name).value,
        remedy=f"re-create {name}: the mount set is fixed at create time",
        do_not="do not change a mount's mode with docker: the gateway's "
               "record and the container would disagree")
    shared = ctx.fact("shared_sources")
    yield check(
        claim=f"{name}: no mount source is shared with another member",
        expected=ctx.fact("empty"),
        actual=_map(shared.value,
                    lambda rows: [r for r in rows if name in r[1]]),
        remedy="each member's lanes come from its own instances/NAME/: "
               "re-create the member whose mount names another's directory",
        do_not="do not make two members share a lane directory to 'save' a "
               "copy: a shared source lets one agent write what another reads")
    yield check(
        claim=f"{name}: a write into the inbox lane fails with the errno L1 "
              f"recorded",
        expected=ctx.fact("denied_errno"),
        actual=ctx.of("write_probe", name).value,
        remedy=f"the inbox lane is writable from inside {name}: check the "
               f"mount and the policy (the two checks above), then re-create "
               f"it",
        do_not="do not look for an OpenShell log line: OpenShell logs no "
               "filesystem denial, and the second layer is evidenced by the "
               "policy and mount checks")
    yield check(
        claim=f"{name}: the delivery daemon is running",
        expected=ctx.fact("true"),
        actual=_map(ctx.of("delivery_counts", name).value, lambda n: n > 0),
        remedy=f"start it in {name}: the main process starts inbox-delivery, "
               f"so read why amap-main stopped",
        do_not="do not read this PASS as proof the agent did not stage it; "
               "the daemon has no host-visible signal, and this check exists "
               "to catch a daemon that has died, not an agent that lies")


def _router_gate(ctx: Ctx, claim: str) -> Optional[Check]:
    repo = ctx.fact("router_repo")
    if repo.known:
        return None
    return unknown(claim=claim, expected=ctx.fact("true"),
                   reason=repo.value.reason,
                   remedy=router_link.not_found_remedy())


def verify_router_container(ctx: Ctx) -> Iterator[Check]:
    gate = _router_gate(ctx, router_sections.CONTAINER_RUNNING)
    if gate is not None:
        yield gate
        return
    yield from router_sections.verify_container(ctx)


def verify_router_health(ctx: Ctx) -> Iterator[Check]:
    gate = _router_gate(ctx, router_sections.HEALTH_STATUS)
    if gate is not None:
        yield gate
        return
    yield from router_sections.verify_health(ctx)


SECTIONS = (
    Section(SECTION_INSTALL, "the installed payload and layout",
            verify_install),
    Section(SECTION_ROUTER_CONFIG, "the router's rendered configuration",
            verify_router_config),
    Section(SECTION_GATEWAY, "the gateway's configuration", verify_gateway),
    Section(SECTION_MEMBERS, "each member", verify_members),
    Section(router_sections.SECTION_CONTAINER, "the router's container",
            verify_router_container),
    Section(router_sections.SECTION_HEALTH, "the router's health",
            verify_router_health),
)


def _workspace_hint(opts: Options) -> str:
    try:
        return policy.workspace_of(
            policy.load_fleet(l1_kit.fleet_json(opts.home)))
    except Exception:  # the `fleet` fact reports why; nothing asks OpenShell
        return ""


def run(opts: Options) -> int:
    client = openshell_cli.Client(opts.openshell, _workspace_hint(opts),
                                  opts.env)
    ctx = Ctx(opts, Probe(client, opts.docker, opts.env))
    outcomes = router_sections.run_sections(ctx, SECTIONS)
    for line in report_lines(outcomes):
        print(line)
    problems = problem_lines(outcomes)
    for line in problems:
        print(line)
    for line in ctx.warnings:
        print(f"warning: {line}")
    every = [c for o in outcomes for c in o.checks]
    return EXIT_OK if every and all(c.result == PASS for c in every) \
        else EXIT_PROBLEM


_REFUSALS = (verbs.VerbError, l1_kit.KitError, render.RenderError,
             router_config.RouterConfigError, policy.PolicyError,
             membership.MembershipError, openshell_cli.OpenShellError,
             router_link.RouterNotFound, policy.SandyNotFound, OSError)


def main(args: argparse.Namespace) -> int:
    try:
        return run(options_from(args))
    except _REFUSALS as e:
        print(f"{PROG} verify: {e}", file=sys.stderr)
        return EXIT_PROBLEM
