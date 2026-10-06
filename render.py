"""Renders one member's sandbox: the `openshell sandbox create` argument vector
and the sandbox policy YAML.

Everything here is pure. Nothing is read from or written to the filesystem, and
no process identity is read (no uid or gid is looked up): the uid:gid is a value
the caller passes in `Host.run_as`. Nothing is run. Placing the files these
renderings name is a later step (S5, S6).

**One table.** `member_rows` (which `mount_table` calls) is the only place a mount's `read_only` value is
decided. The driver config's `read_only` flags and the policy's `read_only` and
`read_write` lists are both generated from its output, so they cannot disagree.
`rendering_problems` re-checks a finished rendering, and `render_member` fails
closed on any problem it finds.

**Lane names come from the router.** `LANES`, `LANE_OUTBOX` and the other lane
constants are read from amap-router-local's `router.config` through
`router_link` (`router/config.py:217-220`). No lane name is written as a
literal in this module. `ROSTER_DIRNAME` is pinned here, because `roster` is an
internal router module: the router derives `dirname(selected_json)/roster`
(`router/roster.py:112,125-132`), and a test cross-checks the pin.

**Docker only (D3).** The driver config is keyed `docker`. Podman is untested.

**One uid:gid (D7).** Every policy carries the same `process.run_as_user` and
`run_as_group`, from one configured `"UID:GID"` value, the format the router
container gets as `--user "$(id -u):$(id -g)"` (amap-router-local
`docker/run.sh:81`). Root is refused.

OpenShell facts this module relies on, cited at OpenShell `main@acbac9c`:

- Bind mounts are `--driver-config-json`, `{"docker": {"mounts": [...]}}`, with
  `type`, `source`, `target` and `read_only`; unknown fields are refused, and
  targets may not replace the workspace root or the container root
  (docs/how-it-works/sandboxes/runtimes.mdx:113-133).
- `filesystem_policy.read_only` and `read_write` take absolute paths;
  `landlock.compatibility` and `process.run_as_user` and `run_as_group` are in
  docs/how-it-works/policies/schema.mdx:39-100.
- Reserved container roots: crates/openshell-core/src/container_paths.rs:11-17,
  26 and 32-41, checked by crates/openshell-driver-docker/src/driver_mounts.rs:86-99.
- With both lists empty OpenShell applies no Landlock layer
  (crates/openshell-sandbox/src/sandbox/linux/landlock.rs:241-242).
- The default policy's system paths and read-write paths
  (docs/how-it-works/policies/default-policy.mdx:26-40, 58-75).
- The CLI flags (crates/openshell-cli/src/main.rs, lines cited at each use).
- The flags that let a script create a sandbox with no terminal: `--detach`,
  `--tty` and `--auto-providers` (see `UNATTENDED_FLAGS`).

The limit of `hard_requirement`: it makes an unusable Landlock ruleset fatal, but
it still skips a single missing path (schema.mdx:69-76). That is why the lane
directories must exist before the sandbox does (S5), and why `verify` (S7)
checks them. Whether Claude Code runs under exactly the system path lists below
is for the live check (L1) to confirm.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import (Any, Dict, List, Mapping, NamedTuple, Optional, Sequence,
                    Tuple)

import membership
import policy
import provider_profile
import router_link


class RenderError(ValueError):
    """A rendering cannot be made, or a finished rendering is not sound."""


# --- the router's facts ------------------------------------------------------

_rc = router_link.router_config()
LANES: Tuple[str, ...] = tuple(_rc.LANES)  # amap-router-local router/config.py:217-220
LANE_OUTBOX: str = _rc.LANE_OUTBOX          # the one agent-written lane (config.py:28-38)
# The variable payload/amap-main reads for each lane.
LANE_ENV: Dict[str, str] = {_rc.LANE_INBOX: "AMAP_INBOX_DIR",
                            _rc.LANE_PEER: "AMAP_PEER_DIR",
                            _rc.LANE_OUTBOX: "AMAP_OUTBOX_DIR"}
SELF_ENV = "AMAP_SELF_ADDRESS"
# What inbox-submit's `peers` tool reads (amap-connector-claude
# bin/inbox-submit:83-84 at 37875a5): the roster directory and this agent's
# own address. AMAP_SELF_ADDRESS stays for payload/amap-main.
ROSTER_ENV = "AMAP_ROSTER_DIR"
PEER_SELF_ENV = "AMAP_SELF"

if set(LANES) != set(LANE_ENV):
    raise RenderError(
        f"the router has lanes {sorted(LANES)} but payload/amap-main has a "
        f"variable for {sorted(LANE_ENV)}: the router has a lane that "
        f"payload/amap-main has no variable for")

# --- host layout (DESIGN section 2) -----------------------------------------

PAYLOAD_DIRNAME = "payload"
INSTANCES_DIRNAME = "instances"
SELECTED_JSON_NAME = "selected.json"
# amap-router-local router/roster.py:112,125-132: dirname(selected_json)/roster
ROSTER_DIRNAME = "roster"

# --- sandbox side (DESIGN section 3) ----------------------------------------

MOUNT_ROOT = "/opt/amap"
PAYLOAD_TARGET = MOUNT_ROOT + "/payload"
ROSTER_TARGET = MOUNT_ROOT + "/roster"
LANES_TARGET = MOUNT_ROOT + "/lanes"

# The image's WORKDIR is the workspace (OpenShell
# main@acbac9c:docs/how-it-works/sandboxes/runtimes.mdx:275; image/Dockerfile).
# HOME is the workdir under a numeric run_as_user (OpenShell
# main@acbac9c:crates/openshell-sandbox/src/process.rs:217-241).
WORKDIR = "/sandbox"

# OpenShell main@acbac9c:docs/how-it-works/policies/default-policy.mdx:28-37
SYSTEM_READ_ONLY = ("/bin", "/usr", "/lib", "/proc", "/dev/urandom", "/etc",
                    "/var/log")
# The workdir is listed explicitly, with include_workdir false, so the rendered
# lists show every writable path. /tmp and /dev/null are the default policy's
# and the baseline's read-write paths (OpenShell
# main@acbac9c:docs/how-it-works/policies/default-policy.mdx:39,65-68). /tmp
# may hold Claude Code's socket directory: the session hook records wherever
# Claude Code put it (payload/claude-session-start), and L1 checks that it is
# under a read-write path.
SYSTEM_READ_WRITE = (WORKDIR, "/tmp", "/dev/null")

# OpenShell main@acbac9c:crates/openshell-core/src/container_paths.rs:11-17,
# 32-41 (checked by crates/openshell-driver-docker/src/driver_mounts.rs:86-99),
# and 26 for the last three.
RESERVED_ROOTS = ("/opt/openshell", "/etc/openshell", "/etc/openshell-tls",
                  "/run/openshell", "/run/openshell-sidecar", "/run/netns",
                  "/var/run/netns", "/proc", "/sys", "/dev")

# OpenShell main@acbac9c:crates/openshell-driver-docker/src/lib.rs:731-741 (the
# mount is tagged by `type` and refuses unknown fields), docs runtimes.mdx:113-131.
DRIVER = "docker"
MOUNT_TYPE = "bind"
# The ID of this deployment's own provider profile, providers/amap-claude-code.json,
# which the runner imports in step 3. `--provider <profile id> --auto-providers`
# creates the provider from it: OpenShell
# main@acbac9c:docs/how-it-works/providers/overview.mdx:344-356 and
# crates/openshell-cli/src/commands/provider.rs:413-447, 508-521 (identical at
# v0.1.2).
PROVIDER = provider_profile.PROFILE_ID
# OpenShell main@acbac9c:docs/how-it-works/sandboxes/overview.mdx:36-48 and
# crates/openshell-cli/src/main.rs:1538-1544
RESTART_POLICY = "on-failure"
# OpenShell main@acbac9c:docs/how-it-works/policies/schema.mdx:63-80
COMPATIBILITY = "hard_requirement"
POLICY_VERSION = 1
# OpenShell main@acbac9c:docs/how-it-works/policies/schema.mdx:89
MAX_ID = 4294967294

# The wrapper and the arguments amap-deploy-sandy gives Claude Code (DESIGN
# section 3). A test pins them against sandy's constants.
MAIN_PROCESS_NAME = "amap-main"
MCP_CONFIG_NAME = "mcp-servers.json"
POLICY_PROMPT_NAME = "INBOX-POLICY.md"
MCP_CONFIG_FLAG = "--mcp-config"
SYSTEM_PROMPT_FILE_FLAG = "--append-system-prompt-file"
# The settings file's `crossSessionInbound` is from Claude Code's
# cross-session-messaging page (read 2026-09-29), and its
# `skipDangerousModePermissionPrompt` is from D9, observed on 2.1.284.
SETTINGS_NAME = "claude-settings.json"
# `claude --help`, Claude Code 2.1.284: `--settings <file-or-json>` and
# `--dangerously-skip-permissions` (D8).
SETTINGS_FLAG = "--settings"
BYPASS_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# A script creates the sandbox with no terminal. OpenShell main@acbac9c:
# crates/openshell-cli/src/main.rs:1523-1555 (also v0.1.2, main.rs:1523-1547);
# --detach returns without attaching (run.rs:649-653, 1165-1172;
# docs/how-it-works/sandboxes/overview.mdx:29-33); --tty gives the main process
# a terminal regardless of the caller's (run.rs:641-642, 675); --auto-providers
# creates the provider without asking (commands/provider.rs:481-497).
UNATTENDED_FLAGS = ("--detach", "--tty", "--auto-providers")
UNATTENDED_NEGATIONS = ("--no-tty", "--no-auto-providers")

POLICY_KEYS = ("version", "filesystem_policy", "landlock", "process")
POLICY_HEADER = ("# Sandbox policy rendered by amap-openshell. "
                 "Re-render it; do not edit it.")


# --- types -------------------------------------------------------------------

class RunAs(NamedTuple):
    uid: int
    gid: int


class Host(NamedTuple):
    home: str             # $AMAP_OPENSHELL_HOME on the host
    image: str            # --from
    run_as: RunAs         # the one uid:gid for the router and every sandbox (D7)
    restart_policy: bool  # the installed OpenShell accepts --restart-policy


class Mount(NamedTuple):
    role: str             # PAYLOAD_DIRNAME, ROSTER_DIRNAME, or a router lane name
    source: str
    target: str
    read_only: bool


class Rendering(NamedTuple):
    name: str
    workspace: str
    address: str
    mounts: Tuple[Mount, ...]
    policy: Dict[str, Any]
    policy_yaml: str
    argv: List[str]


# --- the configured identity and the host -----------------------------------

def parse_run_as(value: str) -> RunAs:
    """`"UID:GID"`, the value the router container gets as
    `--user "$(id -u):$(id -g)"` (amap-router-local `docker/run.sh:81`). Root,
    signs, spaces and out-of-range ids are refused."""
    if not isinstance(value, str):
        raise RenderError(
            f"run_as must be a string 'UID:GID', got {type(value).__name__}")
    parts = value.split(":")
    if len(parts) != 2:
        raise RenderError(f"run_as {value!r} must be exactly 'UID:GID'")
    ids = []
    for part in parts:
        if not (part.isascii() and part.isdigit()):
            raise RenderError(
                f"run_as {value!r}: {part!r} is not a plain decimal number")
        n = int(part)
        if n == 0:
            raise RenderError(
                f"run_as {value!r}: uid and gid must not be 0 (root)")
        if n > MAX_ID:
            raise RenderError(
                f"run_as {value!r}: {n} is above the largest id, {MAX_ID}")
        ids.append(n)
    return RunAs(ids[0], ids[1])


def _run_as_problem(run_as: object) -> Optional[str]:
    if not isinstance(run_as, RunAs):
        return "run_as must be a RunAs"
    for what, n in (("uid", run_as.uid), ("gid", run_as.gid)):
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_ID:
            return f"run_as {what} {n!r} must be a number from 1 to {MAX_ID}"
    return None


def host_path_problem(what: str, path: object) -> Optional[str]:
    """A reason `path` cannot be a host directory that is bound by identity, or
    None. `what` names it in the message. The rule is one place so that every
    host path this deployment renders obeys it: `home` and the router's
    `state_dir` (the router's container binds its paths by identity, as
    `-v "$path:$path:$mode"`, amap-router-local `docker/run.sh:69`)."""
    if not isinstance(path, str):
        return f"{what} must be a string, got {type(path).__name__}"
    if not path.startswith("/"):
        return f"{what} {path!r} must be an absolute path"
    if posixpath.normpath(path) != path:
        return (f"{what} {path!r} must be normalized: no trailing '/', no '.' "
                f"or '..' components, no doubled '/'")
    if ":" in path:
        # A bind becomes a Docker `-v source:target:ro` string (OpenShell
        # main@acbac9c:crates/openshell-driver-docker/src/lib.rs:3745-3781),
        # and the router's own run script builds `-v "$path:$path:$mode"`
        # (amap-router-local `docker/run.sh:69`).
        return f"{what} {path!r} must not contain ':'"
    if any(c.isspace() or not c.isprintable() for c in path):
        # OpenShell main@acbac9c:crates/openshell-driver-docker/src/driver_mounts.rs:35-55
        return (f"{what} {path!r} must not contain whitespace or control "
                f"characters")
    return None


def home_problem(home: object) -> Optional[str]:
    """A reason `home` cannot be $AMAP_OPENSHELL_HOME, or None."""
    return host_path_problem("home", home) or (
        "home must not be the filesystem root" if home == "/" else None)


def host_problem(host: Host) -> Optional[str]:
    """A reason the host description cannot be rendered, or None."""
    reason = home_problem(host.home)
    if reason:
        return reason
    if not isinstance(host.image, str) or not host.image:
        return "image is required"
    if any(c.isspace() for c in host.image):
        return f"image {host.image!r} must not contain whitespace"
    if not isinstance(host.restart_policy, bool):
        return "restart_policy must be a bool"
    return _run_as_problem(host.run_as)


# --- layout ------------------------------------------------------------------

def payload_dir(home: str) -> str:
    return posixpath.join(home, PAYLOAD_DIRNAME)


def instances_dir(home: str) -> str:
    return posixpath.join(home, INSTANCES_DIRNAME)


def selected_json(home: str) -> str:
    return posixpath.join(home, SELECTED_JSON_NAME)


def roster_dir(home: str) -> str:
    return posixpath.join(posixpath.dirname(selected_json(home)), ROSTER_DIRNAME)


def lane_dir(home: str, name: str, lane: str) -> str:
    return posixpath.join(instances_dir(home), name, lane)


def lane_target(lane: str) -> str:
    return f"{LANES_TARGET}/{lane}"


# --- the one table -----------------------------------------------------------

def member_rows(home: str, name: str) -> Tuple[Mount, ...]:
    """THE table. The payload and the roster are read-only, and so is every
    lane except the one the agent writes. This is the only place in the module
    where a `read_only` value is decided. It needs only the home and the name,
    so the interceptor computes the same rows."""
    reason = membership.name_problem(name) or home_problem(home)
    if reason:
        raise RenderError(reason)
    rows = [Mount(PAYLOAD_DIRNAME, payload_dir(home), PAYLOAD_TARGET, True),
            Mount(ROSTER_DIRNAME, roster_dir(home), ROSTER_TARGET, True)]
    for lane in LANES:
        rows.append(Mount(lane, lane_dir(home, name, lane), lane_target(lane),
                          read_only=(lane != LANE_OUTBOX)))
    return tuple(rows)


def mount_table(host: Host, name: str) -> Tuple[Mount, ...]:
    """The table for a host description: the name, then the host, are checked
    first, and the rows are `member_rows`."""
    reason = membership.name_problem(name)
    if reason:
        raise RenderError(reason)
    reason = host_problem(host)
    if reason:
        raise RenderError(reason)
    return member_rows(host.home, name)


# --- renderers ---------------------------------------------------------------

def driver_config(mounts: Sequence[Mount]) -> Dict[str, Any]:
    """The `--driver-config-json` document. `read_only` is always explicit."""
    return {DRIVER: {"mounts": [
        {"type": MOUNT_TYPE, "source": m.source, "target": m.target,
         "read_only": m.read_only} for m in mounts]}}


def driver_config_json(mounts: Sequence[Mount]) -> str:
    return json.dumps(driver_config(mounts), sort_keys=True,
                      separators=(",", ":"))


def filesystem_policy(mounts: Sequence[Mount]) -> Dict[str, Any]:
    return {
        "include_workdir": False,
        "read_only": [*SYSTEM_READ_ONLY,
                      *(m.target for m in mounts if m.read_only)],
        "read_write": [*SYSTEM_READ_WRITE,
                       *(m.target for m in mounts if not m.read_only)],
    }


def policy_document(mounts: Sequence[Mount], run_as: RunAs) -> Dict[str, Any]:
    """The policy. It has no network rules: egress comes only from the provider
    (DESIGN section 4; OpenShell
    main@acbac9c:docs/how-it-works/policies/default-policy.mdx:42-47). The ids
    are strings because OpenShell's `ProcessPolicy` fields are strings
    (crates/openshell-policy-schema/src/lib.rs:237-244; schema.mdx:96-100)."""
    return {
        "version": POLICY_VERSION,
        "filesystem_policy": filesystem_policy(mounts),
        "landlock": {"compatibility": COMPATIBILITY},
        "process": {"run_as_user": str(run_as.uid),
                    "run_as_group": str(run_as.gid)},
    }


_KEY = re.compile(r"[a-z_]+\Z")


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    raise RenderError(f"cannot render a {type(value).__name__} as a YAML scalar")


def _emit(mapping: Mapping[str, Any], depth: int, out: List[str]) -> None:
    pad = "  " * depth
    for key, value in mapping.items():
        if not isinstance(key, str) or not _KEY.match(key):
            raise RenderError(f"policy key {key!r} must match [a-z_]+")
        if isinstance(value, Mapping):
            if not value:
                raise RenderError(f"policy key {key!r} is an empty mapping")
            out.append(f"{pad}{key}:")
            _emit(value, depth + 1, out)
        elif isinstance(value, (list, tuple)):
            if not value:
                out.append(f"{pad}{key}: []")
            else:
                out.append(f"{pad}{key}:")
                out.extend(f"{pad}  - {_scalar(item)}" for item in value)
        else:
            out.append(f"{pad}{key}: {_scalar(value)}")


def policy_yaml(doc: Mapping[str, Any]) -> str:
    """Block-style YAML for this policy's shape only. Block style is the form of
    OpenShell's own documented examples (OpenShell
    main@acbac9c:docs/how-it-works/policies/schema.mdx:56-61, 96-100); whether
    its YAML parser (noyalib, Cargo.toml:88) accepts flow style has not been
    checked. OpenShell rejects unknown fields and duplicate keys
    (schema.mdx:11-13; crates/openshell-policy-schema/src/lib.rs:568-599), and
    this emitter produces neither."""
    lines = [POLICY_HEADER]
    _emit(doc, 0, lines)
    return "\n".join(lines) + "\n"


def main_process() -> List[str]:
    """What follows `--`. `amap-main` runs claude itself, so its arguments are
    claude's arguments and no `claude` word appears here."""
    return [f"{PAYLOAD_TARGET}/{MAIN_PROCESS_NAME}",
            MCP_CONFIG_FLAG, f"{PAYLOAD_TARGET}/{MCP_CONFIG_NAME}",
            SYSTEM_PROMPT_FILE_FLAG, f"{PAYLOAD_TARGET}/{POLICY_PROMPT_NAME}",
            SETTINGS_FLAG, f"{PAYLOAD_TARGET}/{SETTINGS_NAME}",
            BYPASS_PERMISSIONS_FLAG]


def env_pairs(mounts: Sequence[Mount], address: str) -> List[str]:
    """`--env` reaches every process in the sandbox (OpenShell
    main@acbac9c:docs/how-it-works/providers/overview.mdx:366-374).

    The lanes, then the member's address twice (SELF_ENV for payload/amap-main,
    PEER_SELF_ENV for inbox-submit), then the roster mount's target as
    ROSTER_ENV."""
    return ([f"{LANE_ENV[m.role]}={m.target}" for m in mounts
             if m.role in LANE_ENV]
            + [f"{SELF_ENV}={address}", f"{PEER_SELF_ENV}={address}"]
            + [f"{ROSTER_ENV}={m.target}" for m in mounts
               if m.role == ROSTER_DIRNAME])


def _path_problem(what: str, path: object) -> Optional[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        return f"{what} {path!r} must be an absolute path"
    if posixpath.normpath(path) != path:
        return f"{what} {path!r} must be a normalized path"
    return None


def create_argv(workspace: str, name: str, host: Host, policy_path: str,
                mounts: Sequence[Mount], address: str,
                openshell: str = "openshell") -> List[str]:
    """The `openshell sandbox create` argument vector.

    Flags, at OpenShell main@acbac9c:crates/openshell-cli/src/main.rs: the
    global `--workspace` (466-475; pinned so `$OPENSHELL_WORKSPACE` cannot move
    a member to another workspace), `--name` (1426), `--from` (1439),
    `--driver-config-json` (1495), `--provider` (1501), `--auto-providers`, `--detach` and `--tty` (1523-1555;
    also v0.1.2, main.rs:1523-1547; --detach returns without attaching,
    run.rs:649-653, 1165-1172, docs/how-it-works/sandboxes/overview.mdx:29-33;
    --tty gives the main process a terminal whatever the caller has,
    run.rs:641-642, 675; --auto-providers creates the provider without asking,
    commands/provider.rs:481-497; the provider is created from this
    deployment's imported profile, `PROVIDER`), `--policy` (1506; the
    Docker driver applies `process.run_as_*` only from the policy passed at
    create time, schema.mdx:89-94), `--restart-policy` (1538-1544), `--env`
    (1561-1565), and the command after `--` (1589)."""
    reason = _path_problem("policy_path", policy_path)
    if reason:
        raise RenderError(reason)
    argv = [openshell, "--workspace", workspace, "sandbox", "create",
            "--name", name, "--from", host.image, "--provider", PROVIDER,
            "--auto-providers", "--policy", policy_path,
            "--driver-config-json", driver_config_json(mounts)]
    for pair in env_pairs(mounts, address):
        argv += ["--env", pair]
    if host.restart_policy:
        argv += ["--restart-policy", RESTART_POLICY]
    argv += ["--detach", "--tty"]
    argv.append("--")
    argv += main_process()
    return argv


def render_from_table(fleet: Dict[str, Any], name: str, host: Host,
                      policy_path: str, mounts: Tuple[Mount, ...]) -> Rendering:
    """Renders `mounts` as given. It runs no checks: `render_member` does."""
    workspace = policy.workspace_of(fleet)
    address = policy.addresses(fleet, [name])[name]
    doc = policy_document(mounts, host.run_as)
    return Rendering(name, workspace, address, tuple(mounts), doc,
                     policy_yaml(doc),
                     create_argv(workspace, name, host, policy_path, mounts,
                                 address))


# --- the checker -------------------------------------------------------------

def _parts(path: str) -> List[str]:
    return [p for p in path.split("/") if p]


def _at_or_under(path: str, parent: str) -> bool:
    p, q = _parts(path), _parts(parent)
    return p[:len(q)] == q


def _overlap(a: str, b: str) -> bool:
    return _at_or_under(a, b) or _at_or_under(b, a)


def _expected_source(host: Host, name: str, role: str) -> Optional[str]:
    if role == PAYLOAD_DIRNAME:
        return payload_dir(host.home)
    if role == ROSTER_DIRNAME:
        return roster_dir(host.home)
    if role in LANES:
        return lane_dir(host.home, name, role)
    return None


def _expected_target(role: str) -> Optional[str]:
    if role == PAYLOAD_DIRNAME:
        return PAYLOAD_TARGET
    if role == ROSTER_DIRNAME:
        return ROSTER_TARGET
    if role in LANES:
        return lane_target(role)
    return None


def _argv_problems(r: Rendering) -> List[str]:
    probs: List[str] = []
    argv = r.argv
    if argv.count("--") != 1:
        probs.append(f"argv must have exactly one '--', has {argv.count('--')}")
        return probs
    cut = argv.index("--")
    head, tail = argv[:cut], argv[cut + 1:]
    providers = [i for i, w in enumerate(head) if w == "--provider"]
    if len(providers) != 1:
        probs.append(f"argv must have exactly one --provider, has "
                     f"{len(providers)}")
    elif providers[0] + 1 >= len(head) or head[providers[0] + 1] != PROVIDER:
        probs.append(f"--provider must be {PROVIDER}: egress comes only from "
                     f"the provider, not from network rules")
    for flag in UNATTENDED_FLAGS:
        if head.count(flag) != 1:
            probs.append(f"argv must have {flag} exactly once: a script "
                         f"creates the sandbox with no terminal")
    for flag in UNATTENDED_NEGATIONS:
        if flag in head:
            probs.append(f"argv must not have {flag}: it overrides "
                         f"{'--' + flag[len('--no-'):]}")
    configs = [i for i, w in enumerate(head) if w == "--driver-config-json"]
    if len(configs) != 1 or configs[0] + 1 >= len(head):
        probs.append("argv must have exactly one --driver-config-json with a "
                     "value")
    else:
        try:
            got = json.loads(head[configs[0] + 1])
        except ValueError as e:
            got = None
            probs.append(f"--driver-config-json is not JSON: {e}")
        if got is not None and got != driver_config(r.mounts):
            probs.append("--driver-config-json does not match the mount table")
    if tail != main_process():
        probs.append(f"the command after '--' must be {main_process()}")
    return probs


def rendering_problems(r: Rendering, host: Host) -> List[str]:
    """Every broken rule, one message each. An empty list means sound."""
    probs: List[str] = []
    doc = r.policy
    fs = doc.get("filesystem_policy", {}) if isinstance(doc, Mapping) else {}
    ro = list(fs.get("read_only", [])) if isinstance(fs, Mapping) else []
    rw = list(fs.get("read_write", [])) if isinstance(fs, Mapping) else []

    # a. OpenShell main@acbac9c:crates/openshell-sandbox/src/sandbox/linux/landlock.rs:241-242
    if not ro and not rw:
        probs.append("read_only and read_write are both empty, so OpenShell "
                     "applies no Landlock policy layer")

    # b. the flag and the lists agree
    for m in r.mounts:
        if (m.target in ro) != m.read_only:
            probs.append(f"the {m.role} mount is read_only is {m.read_only} but "
                         f"its target is {'not ' if m.target not in ro else ''}"
                         f"in the policy's read_only list")
        if (m.target in rw) != (not m.read_only):
            probs.append(f"the {m.role} mount is read_only is {m.read_only} but "
                         f"its target is {'not ' if m.target not in rw else ''}"
                         f"in the policy's read_write list")

    # c. the expected flag by role
    for m in r.mounts:
        want_ro = m.role != LANE_OUTBOX
        if m.read_only != want_ro:
            probs.append(f"the {m.role} mount must be "
                         f"{'read-only' if want_ro else 'read-write'}")

    # d. R3: no target at or under a read_write path, except its own entry
    for m in r.mounts:
        for p in rw:
            if _at_or_under(m.target, p) and \
                    not (p == m.target and not m.read_only):
                probs.append(f"the {m.role} mount target {m.target} is at or "
                             f"under the read_write path {p}, which grants "
                             f"write beneath it")

    # e. targets
    targets = [m.target for m in r.mounts]
    for m in r.mounts:
        reason = _path_problem(f"the {m.role} mount target", m.target)
        if reason:
            probs.append(reason)
            continue
        if m.target == "/":
            probs.append(f"the {m.role} mount target must not be '/'")
        for root in RESERVED_ROOTS:
            if _overlap(m.target, root):
                probs.append(f"the {m.role} mount target {m.target} overlaps "
                             f"the reserved root {root}")
        if _overlap(m.target, WORKDIR):
            probs.append(f"the {m.role} mount target {m.target} overlaps the "
                         f"workdir {WORKDIR}")
        want = _expected_target(m.role)
        if want is not None and m.target != want:
            probs.append(f"the {m.role} mount target is {m.target}, expected "
                         f"{want}")
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            if isinstance(a, str) and isinstance(b, str) and _overlap(a, b):
                probs.append(f"mount targets {a} and {b} overlap")

    # f. sources
    expected = {payload_dir(host.home), roster_dir(host.home)}
    expected |= {lane_dir(host.home, r.name, lane) for lane in LANES}
    actual = {m.source for m in r.mounts}
    if actual != expected:
        probs.append(f"the mount sources are {sorted(actual)}, expected "
                     f"exactly {sorted(expected)}: only this member's lanes, "
                     f"the payload and the roster may be mounted")
    for m in r.mounts:
        want = _expected_source(host, r.name, m.role)
        if want is None:
            probs.append(f"the mount role {m.role!r} is not one this member has")
        elif m.source != want:
            probs.append(f"the {m.role} mount source is {m.source}, expected "
                         f"{want}")
    if sorted(m.role for m in r.mounts) != \
            sorted([PAYLOAD_DIRNAME, ROSTER_DIRNAME, *LANES]):
        probs.append("the mounts are not exactly the payload, the roster and "
                     "each lane once (source check)")

    # g. identity
    want_proc = {"run_as_user": str(host.run_as.uid),
                 "run_as_group": str(host.run_as.gid)}
    proc = doc.get("process") if isinstance(doc, Mapping) else None
    if proc != want_proc:
        probs.append(f"process must be {want_proc}, got {proc!r} (run_as)")
    elif "0" in want_proc.values():
        probs.append("process run_as must not be root")

    # h. shape
    keys = set(doc) if isinstance(doc, Mapping) else set()
    if keys != set(POLICY_KEYS):
        probs.append(f"the policy's top-level keys are {sorted(keys)}, "
                     f"expected {sorted(POLICY_KEYS)}: no network rules, "
                     f"egress comes only from the provider")
    if doc.get("version") != POLICY_VERSION:
        probs.append(f"the policy version must be {POLICY_VERSION}")
    if doc.get("landlock") != {"compatibility": COMPATIBILITY}:
        probs.append(f"landlock must be compatibility {COMPATIBILITY}")

    # i. the text is the document
    try:
        text = policy_yaml(doc)
    except RenderError as e:
        probs.append(f"the policy cannot be rendered as yaml: {e}")
    else:
        if r.policy_yaml != text:
            probs.append("policy_yaml is not the yaml of the policy document")

    # j. argv
    probs.extend(_argv_problems(r))
    return probs


def render_member(fleet: Dict[str, Any], name: str, host: Host,
                  policy_path: str) -> Rendering:
    """The public entry point. It fails closed on any problem."""
    mounts = mount_table(host, name)
    r = render_from_table(fleet, name, host, policy_path, mounts)
    probs = rendering_problems(r, host)
    if probs:
        raise RenderError("\n".join(probs))
    return r
