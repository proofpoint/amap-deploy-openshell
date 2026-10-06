"""The `l1-kit` verb: the host-side files for the L1 proof of concept, written
from S4's and S5's renderings.

Host side only, and it writes only under `--home`. It runs nothing: no
subprocess, no `os.system`, no `exec`. It never runs `openshell` or `docker`. It
writes the commands for the operator to run, and it takes every sandbox ID from
its arguments, so it never invents one.

Three actions. Each is a dry run without `--apply`.

- `prepare` writes the payload, every member's lanes, each member's policy
  YAML, each member's `openshell sandbox create` command as a one-line shell
  file, the fleet policy, and the gateway fragment.
- `record` records one created sandbox's ID in `membership.json`. When every
  member of the fleet is recorded it also writes `selected.json` and
  `router.json`.
- `profile` renders this deployment's provider profile, with `binaries` set to
  the real paths given by `--binary`, under `providers/`. It never imports it:
  the runner does.

Each action builds a complete `Plan` first: it reads every source, renders every
member, validates every argument and checks the type of every existing path.
Only then does `carry_out` write, so a refusal writes nothing.

This module adds no rendering logic. It reuses:

- `render`: `render_member`, `parse_run_as`, `Host`, `home_problem` and the
  layout helpers, which give the policy YAML, the create argv, the mount
  sources and the payload names;
- `router_config`: `create_lanes`, `lane_paths`, `render_verdict`,
  `render_router_json`, `json_text` and `default_state_dir`;
- `membership`: `record`, `dump`, `load` and `id_problem`;
- `policy`: `load_fleet`, `named_instances`, `workspace_of` and `find_sandy`;
- `router_link`: `find_router`.

Lane names come from the router, through `router_config` and `render`. None is
written here.

Host layout under `--home`, beyond what `render` and `router_config` fix:

    fleet.json                  a byte copy of `--fleet`; `record` reads it
    policies/<name>.yaml        the member's policy, passed as `--policy`
    commands/create-<name>.sh   the member's create command, one line
    gateway-fragment.toml       DESIGN.md section 6, for reference
    providers/amap-claude-code.json   written by `profile`
    membership.json             written by `record`

OpenShell facts, cited at OpenShell main@acbac9c:

- A bind mount whose source does not exist is refused
  (crates/openshell-driver-docker/src/lib.rs:3753-3761), so every mount source
  exists before the create command runs: the lanes, the payload and `roster/`.
- `sandbox get --output json` has `id`, `name` and `workspace`
  (crates/openshell-cli/src/run.rs:2789-2791,
  docs/how-it-works/sandboxes/overview.mdx:603-607): the operator reads the ID
  there and passes it to `record`.
- The gateway fragment is docs/how-it-works/sandboxes/runtimes.mdx:120-131.

The router's `docker/run.sh` goes through `docker/derive-mounts.py`, which
refuses a `state_dir` that does not exist. So `record` creates `router-state/`
(mode 0700, router-private) when it writes `router.json`.

Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import posixpath
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import (Any, Dict, List, Mapping, NamedTuple, Optional, Sequence,
                    Tuple)

import membership
import policy
import provider_profile
import render
import router_config
import router_link

PROG = "amap-openshell.py"
EXIT_REFUSED = 1


class KitError(ValueError):
    """The kit refuses: a source is missing, an argument is bad, or a path is
    not what the kit needs it to be."""


CONNECTOR_VARIABLE = "AMAP_CONNECTOR_REPO"
CONNECTOR_DIR_NAME = "amap-connector-claude"
CONNECTOR_CONFIRM = "bin/inbox-delivery"
REPO = Path(__file__).absolute().parent

FLEET_NAME = "fleet.json"
MEMBERSHIP_NAME = "membership.json"
POLICIES_DIRNAME = "policies"
COMMANDS_DIRNAME = "commands"
PROVIDERS_DIRNAME = "providers"
GATEWAY_FRAGMENT_NAME = "gateway-fragment.toml"

# DESIGN.md section 6, without its `# or podman` comment (Docker only).
GATEWAY_FRAGMENT = """\
# Gateway settings this deployment requires (OpenShell main@acbac9c:docs/how-it-works/sandboxes/runtimes.mdx:120-131).
# Merge them into the gateway's gateway.toml. DESIGN.md section 6 states the risk they accept.
[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
"""

FILE_MODE = 0o644
EXEC_MODE = 0o755
DIR_MODE = 0o755
STATE_DIR_MODE = 0o700

# The payload's file names beside `render`'s own (the wrapper, the MCP
# registration and the policy text). The layout is amap-deploy-sandy's
# `payload_sources` (amap_sandy.py), defined here because this repository does
# not import amap_sandy.
SESSION_LISTER_NAME = "openshell-sessions"
SESSION_HOOK_NAME = "claude-session-start"
SEED_NAME = "claude-seed"
DELIVERY_NAME = "inbox-delivery"
DELIVERY_SUPPORT_NAME = "_inboxlib.py"
PAYLOAD_BIN_DIRNAME = "bin"
CONNECTOR_BIN_DIRNAME = "bin"
CONNECTOR_MCP_BINARIES = ("inbox-mcp-vol", "inbox-submit")


class Source(NamedTuple):
    rel: str          # the path inside the payload directory
    path: Path        # the file it is copied from
    executable: bool


class Removal(NamedTuple):
    path: str
    kind: str   # "file": one file; "tree": a directory and all it holds;
    #             "contents": what a directory holds, and the directory stays


REMOVAL_KINDS = ("file", "tree", "contents")


class Plan(NamedTuple):
    home: str
    dirs: Tuple[Tuple[str, int], ...]           # (abs path, mode), parents first
    lanes: Tuple[str, ...]                      # member names, made by create_lanes
    files: Tuple[Tuple[str, bytes, int], ...]   # (abs path, data, mode)
    notes: Tuple[str, ...]                      # warnings and progress lines
    removals: Tuple[Removal, ...] = ()          # applied first, in order


# --- sources -----------------------------------------------------------------

def find_connector(env: Optional[Mapping[str, str]] = None,
                   start: Optional[Path] = None) -> Path:
    """The amap-connector-claude checkout, found as `router_link.find_router`
    finds the router. When `$AMAP_CONNECTOR_REPO` is set and not empty it is the
    only place searched. Otherwise the nearest `<ancestor>/amap-connector-claude`
    that contains `bin/inbox-delivery`. Paths use `.absolute()`."""
    env = os.environ if env is None else env
    value = env.get(CONNECTOR_VARIABLE)
    if value:
        cand = Path(value).absolute()
        if (cand / CONNECTOR_CONFIRM).is_file():
            return cand
        raise KitError(
            f"${CONNECTOR_VARIABLE}={value} does not contain "
            f"{CONNECTOR_CONFIRM}; it is the only place searched because the "
            f"variable names it. Point it at the {CONNECTOR_DIR_NAME} "
            f"checkout, or unset it to search beside this repository.")
    here = REPO if start is None else Path(start).absolute()
    for ancestor in [here, *here.parents]:
        cand = ancestor / CONNECTOR_DIR_NAME
        if (cand / CONNECTOR_CONFIRM).is_file():
            return cand
    raise KitError(
        f"cannot find the {CONNECTOR_DIR_NAME} checkout (confirmed by "
        f"{CONNECTOR_CONFIRM}) beside any ancestor of {here}. Check it out "
        f"beside this repository, or set ${CONNECTOR_VARIABLE}.")


def payload_sources(connector: Path) -> Tuple[Source, ...]:
    """Every file the payload holds: what to copy, and where it goes."""
    own = REPO / render.PAYLOAD_DIRNAME
    bin_dir = Path(connector) / CONNECTOR_BIN_DIRNAME
    return (
        Source(render.MAIN_PROCESS_NAME, own / render.MAIN_PROCESS_NAME, True),
        Source(SESSION_LISTER_NAME, own / SESSION_LISTER_NAME, True),
        Source(render.MCP_CONFIG_NAME, own / render.MCP_CONFIG_NAME, False),
        Source(render.POLICY_PROMPT_NAME, own / render.POLICY_PROMPT_NAME,
               False),
        Source(render.SETTINGS_NAME, own / render.SETTINGS_NAME, False),
        Source(SESSION_HOOK_NAME, own / SESSION_HOOK_NAME, True),
        Source(SEED_NAME, own / SEED_NAME, True),
        Source(DELIVERY_NAME, bin_dir / DELIVERY_NAME, True),
        Source(DELIVERY_SUPPORT_NAME, bin_dir / DELIVERY_SUPPORT_NAME, False),
    ) + tuple(Source(f"{PAYLOAD_BIN_DIRNAME}/{n}", bin_dir / n, True)
              for n in CONNECTOR_MCP_BINARIES)


def read_payload(connector: Path) -> List[Tuple[str, bytes, int]]:
    """`(rel, bytes, mode)` for every source. Every source that is missing or
    unreadable is named in one `KitError`, so nothing is copied from a partial
    set."""
    out: List[Tuple[str, bytes, int]] = []
    bad: List[str] = []
    for src in payload_sources(connector):
        try:
            if not src.path.is_file():
                raise OSError("not a file")
            data = src.path.read_bytes()
        except OSError as e:
            bad.append(f"{src.path} ({e})")
            continue
        out.append((src.rel, data, EXEC_MODE if src.executable else FILE_MODE))
    if bad:
        raise KitError("payload source file(s) missing or unreadable: "
                       + "; ".join(bad))
    return out


def known_repos() -> Dict[str, Path]:
    """The checkouts `--home` must not overlap."""
    return {
        "amap-deploy-openshell": REPO,
        "amap-router-local": router_link.find_router(),
        CONNECTOR_DIR_NAME: find_connector(),
        "amap-deploy-sandy": policy.find_sandy(),
    }


def home_overlap(home: str, repos: Mapping[str, Path]) -> Optional[str]:
    """A reason `home` overlaps a repo, or None. Overlap is at or under, in
    either direction. Both the literal paths and their real paths are compared,
    so a symlinked home is caught."""
    homes = {home, os.path.realpath(home)}
    for label in sorted(repos):
        repo = str(repos[label])
        for other in sorted({repo, os.path.realpath(repo)}):
            for h in sorted(homes):
                if render._overlap(h, other):
                    return (f"home {home!r} overlaps {label} ({other}): the "
                            f"kit writes under home, and never into a "
                            f"repository")
    return None


def fleet_members(fleet: Dict[str, Any]) -> List[str]:
    members = policy.named_instances(fleet)
    if not members:
        raise KitError("the fleet names no instance: task_graph, peers, groups "
                       "and task_deny mention nobody, so there is nothing to "
                       "prepare")
    return members


# --- paths -------------------------------------------------------------------

def fleet_json(home: str) -> str:
    return posixpath.join(home, FLEET_NAME)


def membership_json(home: str) -> str:
    return posixpath.join(home, MEMBERSHIP_NAME)


def policy_file(home: str, name: str) -> str:
    return posixpath.join(home, POLICIES_DIRNAME, f"{name}.yaml")


def command_file(home: str, name: str) -> str:
    return posixpath.join(home, COMMANDS_DIRNAME, f"create-{name}.sh")


def gateway_fragment_file(home: str) -> str:
    return posixpath.join(home, GATEWAY_FRAGMENT_NAME)


def providers_dir(home: str) -> str:
    return posixpath.join(home, PROVIDERS_DIRNAME)


def profile_file(home: str) -> str:
    return posixpath.join(providers_dir(home), provider_profile.PROFILE_NAME)


def command_text(argv: Sequence[str]) -> str:
    """The create command as one shell line. The argv is S4's, unchanged."""
    for word in argv:
        if "\n" in word:
            raise KitError(f"an argument of the create command contains a "
                           f"newline: {word!r}")
    return shlex.join(argv) + "\n"


# --- planning ----------------------------------------------------------------

def home_checks(home: str) -> None:
    reason = render.home_problem(home)
    if reason:
        raise KitError(reason)
    reason = home_overlap(home, known_repos())
    if reason:
        raise KitError(reason)


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KitError(f"cannot inspect {path}: {e}") from e


def _require_regular_file(path: str, st: os.stat_result) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise KitError(f"{path} is a symlink: the kit writes real files only")
    if not stat.S_ISREG(st.st_mode):
        raise KitError(f"{path} exists and is not a regular file")


def check_types(dirs: Sequence[Tuple[str, int]],
                 files: Sequence[Tuple[str, bytes, int]]) -> None:
    """Refuses a symlink or a wrong type at any existing target."""
    for path, _ in dirs:
        if _lstat(path) is not None:
            router_config._require_real_directory(path)
    for path, _, _ in files:
        st = _lstat(path)
        if st is not None:
            _require_regular_file(path, st)


def require_home_directory(home: str, must_exist: bool) -> None:
    st = _lstat(home)
    if st is None:
        if must_exist:
            raise KitError(f"home {home!r} does not exist: run "
                           f"`l1-kit prepare --apply` first")
        parent = posixpath.dirname(home)
        if not os.path.isdir(parent):
            raise KitError(f"cannot create home {home!r}: its parent {parent!r} "
                           f"is not a directory")
        return
    router_config._require_real_directory(home, st)


def plan_prepare(home: str, fleet_file: str, run_as: str, image: str,
                 restart_policy: bool = False) -> Plan:
    home_checks(home)
    require_home_directory(home, must_exist=False)
    fleet = policy.load_fleet(fleet_file)
    members = fleet_members(fleet)
    host = render.Host(home, image, render.parse_run_as(run_as), restart_policy)
    rendered = {n: render.render_member(fleet, n, host, policy_file(home, n))
                for n in members}
    fleet_bytes = Path(fleet_file).read_bytes()

    dirs = ((home, DIR_MODE),
            (render.payload_dir(home), DIR_MODE),
            (posixpath.join(render.payload_dir(home), PAYLOAD_BIN_DIRNAME),
             DIR_MODE),
            (render.roster_dir(home), DIR_MODE),
            (posixpath.join(home, POLICIES_DIRNAME), DIR_MODE),
            (posixpath.join(home, COMMANDS_DIRNAME), DIR_MODE))

    files: List[Tuple[str, bytes, int]] = []
    for rel, data, mode in read_payload(find_connector()):
        files.append((posixpath.join(render.payload_dir(home), rel), data, mode))
    files.append((fleet_json(home), fleet_bytes, FILE_MODE))
    for n in members:
        files.append((policy_file(home, n),
                      rendered[n].policy_yaml.encode("utf-8"), FILE_MODE))
        files.append((command_file(home, n),
                      command_text(rendered[n].argv).encode("utf-8"),
                      FILE_MODE))
    files.append((gateway_fragment_file(home),
                  GATEWAY_FRAGMENT.encode("utf-8"), FILE_MODE))

    if os.path.isdir(home):
        for n in members:  # refuses a symlink or a file in a lane's way
            router_config.create_lanes(home, n, dry_run=True)
    check_types(dirs, files)
    return Plan(home, dirs, tuple(members), tuple(files), ())


def missing_members(fleet: Dict[str, Any],
                    recorded: Sequence[membership.Member]) -> List[str]:
    """The fleet's members that `recorded` does not list, in fleet order."""
    have = {m.name for m in recorded}
    return [n for n in fleet_members(fleet) if n not in have]


def router_files(home: str, fleet: Dict[str, Any],
                 recorded: Sequence[membership.Member]
                 ) -> Tuple[List[Tuple[str, int]],
                            List[Tuple[str, bytes, int]], List[str]]:
    """The router's private state directory, `router.json` and `selected.json`
    for a record that is complete, and the render's warnings. Refused by
    `render_router_json` while a member is unrecorded."""
    workspace = policy.workspace_of(fleet)
    rendered = router_config.render_router_json(fleet, recorded, home)
    verdict = router_config.json_text(
        router_config.render_verdict(recorded, workspace))
    dirs = [(router_config.default_state_dir(home), STATE_DIR_MODE)]
    files = [(router_config.router_json_path(home),
              rendered.text.encode("utf-8"), FILE_MODE),
             (render.selected_json(home), verdict.encode("utf-8"), FILE_MODE)]
    return dirs, files, list(rendered.warnings)


def plan_record(home: str, name: str, sandbox_id: str) -> Plan:
    home_checks(home)
    require_home_directory(home, must_exist=True)
    fleet_path = fleet_json(home)
    st = _lstat(fleet_path)
    if st is None:
        raise KitError(f"{fleet_path} is absent: run `l1-kit prepare --apply` "
                       f"first")
    _require_regular_file(fleet_path, st)
    fleet = policy.load_fleet(fleet_path)
    members = fleet_members(fleet)

    if name not in members:
        raise KitError(f"{name!r} is not a member of the fleet "
                       f"({', '.join(members)})")
    reason = membership.id_problem(sandbox_id)
    if reason:
        raise KitError(reason)
    missing = router_config.create_lanes(home, name, dry_run=True)
    if missing:
        raise KitError(f"{name!r} has no lanes on this host (missing: "
                       f"{', '.join(missing)}): run `l1-kit prepare --apply` "
                       f"first")

    workspace = policy.workspace_of(fleet)
    existing = membership.load(membership_json(home)) or []
    stray = sorted(m.name for m in existing if m.name not in members)
    if stray:
        raise KitError(f"membership.json records {', '.join(stray)}, which the "
                       f"fleet does not name")
    recorded = membership.record(
        existing, membership.Member(workspace, name, sandbox_id))

    files: List[Tuple[str, bytes, int]] = [
        (membership_json(home), membership.dump(recorded).encode("utf-8"),
         FILE_MODE)]
    dirs: List[Tuple[str, int]] = []
    notes: List[str] = []
    have = sorted(m.name for m in recorded)
    left = missing_members(fleet, recorded)
    notes.append(f"recorded {', '.join(have)}")
    if left:
        notes.append(f"still to record: {', '.join(left)}; selected.json and "
                     f"router.json are written when every member is recorded")
        if _lstat(render.selected_json(home)) is not None:
            notes.append(
                f"warning: {render.SELECTED_JSON_NAME} exists but the record "
                f"is incomplete; the kit never deletes a file, so remove it if "
                f"it is stale")
    else:
        more_dirs, more_files, warnings = router_files(home, fleet, recorded)
        dirs.extend(more_dirs)
        files.extend(more_files)
        notes.extend(f"warning: {w}" for w in warnings)
    check_types(dirs, files)
    return Plan(home, tuple(dirs), (), tuple(files), tuple(notes))


def plan_profile(home: str, binaries: Sequence[str]) -> Plan:
    """The provider profile, rendered with `binaries` (the image's real paths).
    Nothing here reads the image: the caller supplies the paths."""
    home_checks(home)
    require_home_directory(home, must_exist=True)
    doc = provider_profile.render_profile(provider_profile.load_template(),
                                          binaries)
    dirs = ((providers_dir(home), DIR_MODE),)
    files = ((profile_file(home),
              provider_profile.profile_text(doc).encode("utf-8"), FILE_MODE),)
    check_types(dirs, files)
    return Plan(home, dirs, (), files,
                ("binaries: " + ", ".join(doc["binaries"]),))


# --- writing -----------------------------------------------------------------

def _write_bytes(path: str, data: bytes, mode: int) -> None:
    """Replace `path` with `data` all at once, with exactly `mode`. A temporary
    file in the same directory is synced, given its mode and renamed over the
    target, as `router_config.write_atomic` does for text."""
    parent = posixpath.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=parent,
                               prefix="." + posixpath.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _file_state(path: str, data: bytes, mode: int) -> str:
    """`present` when bytes and mode both match, else `update` or `create`."""
    st = _lstat(path)
    if st is None:
        return "create"
    _require_regular_file(path, st)
    try:
        same = Path(path).read_bytes() == data
    except OSError as e:
        raise KitError(f"cannot read {path}: {e}") from e
    return "present" if same and stat.S_IMODE(st.st_mode) == mode else "update"


def _line(apply: bool, state: str, home: str, path: str) -> str:
    shown = os.path.relpath(path, home)
    if state == "present":
        return f"present {shown}"
    if apply:
        return f"{'created' if state == 'create' else 'updated'} {shown}"
    return f"would {state} {shown}"


def _removal_state(rm: Removal) -> str:
    """`present` or `absent`. A symlink where a directory is expected is
    refused: the router scans with `follow_symlinks=False`, so a link is never
    a lane or a state directory, and is never followed here."""
    if rm.kind not in REMOVAL_KINDS:
        raise KitError(f"unknown removal kind {rm.kind!r} for {rm.path}")
    st = _lstat(rm.path)
    if st is None:
        return "absent"
    if rm.kind == "file":
        if stat.S_ISDIR(st.st_mode):
            raise KitError(f"{rm.path} is a directory, not a file")
        return "present"
    if stat.S_ISLNK(st.st_mode):
        raise KitError(f"{rm.path} is a symlink: the router scans with "
                       f"follow_symlinks=False, so the kit will not follow it")
    if not stat.S_ISDIR(st.st_mode):
        raise KitError(f"{rm.path} exists and is not a directory")
    return "present"


def _empty_directory(path: str) -> None:
    """Remove what `path` holds. A symlink inside is unlinked, never followed."""
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def _apply_removal(rm: Removal) -> None:
    if rm.kind == "file":
        os.unlink(rm.path)
    elif rm.kind == "tree":
        shutil.rmtree(rm.path)
    else:
        _empty_directory(rm.path)


def carry_out(plan: Plan, apply: bool) -> List[str]:
    """The only function that writes, and only with `apply`. Returns the report
    lines: one per path, relative to home, then the plan's notes. Removals come
    first, after every one has been checked."""
    home = plan.home
    lines: List[str] = []
    seen = set()

    states = [(rm, _removal_state(rm)) for rm in plan.removals]
    for rm, state in states:
        shown = os.path.relpath(rm.path, home)
        if state == "absent":
            lines.append(f"absent {shown}")
            continue
        if apply:
            _apply_removal(rm)
        lines.append(f"{'removed' if apply else 'would remove'} {shown}")

    def say(state: str, path: str) -> None:
        if path not in seen:
            seen.add(path)
            lines.append(_line(apply, state, home, path))

    for path, mode in plan.dirs:
        if _lstat(path) is not None:
            say("present", path)
            continue
        if apply:
            os.mkdir(path)
            os.chmod(path, mode)
        say("create", path)

    for name in plan.lanes:
        every = [render.instances_dir(home),
                 posixpath.join(render.instances_dir(home), name),
                 *router_config.lane_paths(home, name)]
        if os.path.isdir(home):
            made = set(router_config.create_lanes(home, name,
                                                  dry_run=not apply))
        else:  # a dry run into a home that does not exist yet
            made = set(every)
        for path in every:
            say("create" if path in made else "present", path)

    for path, data, mode in plan.files:
        state = _file_state(path, data, mode)
        if apply and state != "present":
            _write_bytes(path, data, mode)
        say(state, path)

    lines.extend(plan.notes)
    return lines


# --- command line ------------------------------------------------------------

def main(args: argparse.Namespace) -> int:
    action = args.action
    try:
        if action == "prepare":
            plan = plan_prepare(args.home, args.fleet, args.run_as, args.image,
                                args.restart_policy)
        elif action == "record":
            plan = plan_record(args.home, args.name, args.id)
        elif action == "profile":
            plan = plan_profile(args.home, args.binaries)
        else:
            raise KitError(f"unknown action {action!r}")
        lines = carry_out(plan, args.apply)
    except (KitError, render.RenderError, router_config.RouterConfigError,
            policy.PolicyError, membership.MembershipError,
            provider_profile.ProfileError,
            router_link.RouterNotFound, policy.SandyNotFound, OSError) as e:
        print(f"{PROG} l1-kit {action}: {e}", file=sys.stderr)
        return EXIT_REFUSED
    for line in lines:
        print(line)
    if not args.apply:
        print("dry run: nothing was written; add --apply to write")
    return 0
