"""Renders the router's host-side configuration: `router.json`, the verdict
file, and the lane directories one member needs.

What this module renders, for the router that amap-router-local provides and
that this repository uses unmodified:

- `router.json`, from `fleet.json` and `membership.json`, in **discovery mode**
  (D2): `state_dir`, `instances_dir`, `selected_json`, `fleet_domain`, the task
  graph and, when the fleet declares mail peers, the mail graph. It never has an
  `instances` object: the instance set is the directories under `instances_dir`,
  admitted by the verdict (amap-router-local `router/config.py:817-857`).
- The verdict file at `selected_json`, from `membership.json`. The router reads
  only `selected[].slug` and `not_selected[].slug` from an object
  (`router/config.py:538-588`), and a sandbox name is its slug. The verdict
  carries nothing else. The OpenShell ID stays in `membership.json`, where the
  decision "this sandbox, and not merely one of this name" is recorded.
- The lane directories of one member, `instances_dir/<name>/<lane>/<leaf>`,
  from the router's own `LANES` and `LANE_LEAVES` (`router/config.py:217-220`,
  `232-236`).
- A drift comparison between a rendering and the file on disk.

Layout, from `render`, with `$AMAP_OPENSHELL_HOME` for the home directory:

    $AMAP_OPENSHELL_HOME/router.json          this module's rendering
    $AMAP_OPENSHELL_HOME/selected.json        the verdict (`render.selected_json`)
    $AMAP_OPENSHELL_HOME/instances/<name>/    one member's lanes (`render.instances_dir`)
    $AMAP_OPENSHELL_HOME/payload/             mounted read-only into every sandbox
    $AMAP_OPENSHELL_HOME/roster/              the router publishes here; mounted read-only
    $AMAP_OPENSHELL_HOME/router-state/        router-private (`state_dir`)

The router derives the roster directory as `dirname(selected_json)/roster`
(`router/roster.py:112,125-132`), so `roster/` sits beside the verdict and
outside every lane.

**Lanes are created before the sandbox exists.** In discovery mode the router's
`provision` creates nothing inside an instance tree and refuses when a leaf is
missing (amap-router-local `router/provision.py:154-200`): a directory it made
would not be one the sandbox mounts, and the connector's daemon refuses to start
when a leaf is missing. OpenShell refuses a bind mount whose source does not
exist (OpenShell main@acbac9c:`crates/openshell-driver-docker/src/lib.rs:3753-3761`,
`tests.rs:2052-2075`). So `create_lanes` runs first, for every member. It makes
real directories only: the router scans with `follow_symlinks=False`
(`router/config.py:648`), so a link is not an instance root.

**The verdict is replaced atomically.** A verdict written in place can tear, and
two renders can splice into a document that parses and names a membership
neither computed (`router/config.py:549-567`). `write_atomic` writes a temporary
file in the same directory and renames it over the target. The verdict's
*directory* is what the router container binds, so the router follows the
rename (amap-router-local `docker/derive-mounts.py:69-79`).

**Two choices that differ from amap-deploy-sandy.**

- *The task graph.* The router's `task_graph: "all"` expands to every member
  tasking every other and knows nothing about `task_deny`. This module emits the
  token only when the resolved graph really is the full expansion. When a
  `task_deny` removes a pair it emits the explicit `peer_senders` map, so the deny
  stays in force.
- *A pair on both lanes.* `policy.resolve` returns pairs declared on both the
  task lane and the mail lane instead of refusing them. This module refuses to
  render them, so the router's own refusal (`router/config.py:1232-1241`) cannot
  be reached from a rendering. One-sided mail entries are returned as warnings.

`router.json` is checked by the router's own loader, `router.config.load_obj`,
before it is returned: `loader_check`. Reading the disk is all that loader does
in discovery mode, and it never refuses because of what is on disk. Shipped
code uses only the router's public `router.config`, through `router_link`.

Nothing here talks to OpenShell, Docker or the router. `render_router_json`,
`render_verdict`, `lane_paths` and `drift` read nothing and write nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import stat
import sys
import tempfile
from typing import (Any, Dict, Iterator, List, Mapping, NamedTuple, Optional,
                    Sequence, Tuple)

import membership
import policy
import render
import router_link


class RouterConfigError(ValueError):
    """The router's configuration cannot be rendered, written or created."""


# --- the router's facts ------------------------------------------------------

_rc = router_link.router_config()
LANES: Tuple[str, ...] = tuple(_rc.LANES)  # amap-router-local router/config.py:217-220
LANE_LEAVES: Dict[str, Tuple[str, ...]] = {
    k: tuple(v) for k, v in _rc.LANE_LEAVES.items()}  # config.py:232-236
ROUTER_TASK_GRAPH_ALL: str = _rc.TASK_GRAPH_ALL  # config.py:162
SELECTED_SCHEMA: int = _rc.SELECTED_SCHEMA  # config.py:242

if set(LANE_LEAVES) != set(LANES):
    raise RouterConfigError(
        f"the router has lanes {sorted(LANES)} but leaves for "
        f"{sorted(LANE_LEAVES)}: they must name the same lanes")
if policy.ALLOW_ANY != _rc.ALLOW_ANY:
    raise RouterConfigError(
        f"the router's mail wildcard is {_rc.ALLOW_ANY!r} but fleet_policy's is "
        f"{policy.ALLOW_ANY!r}: the two no longer agree")

ROUTER_JSON_NAME = "router.json"
STATE_DIRNAME = "router-state"

# router.json's keys are the router's names (config.py:149-156). It exports no
# constants for them.
KEY_STATE_DIR = "state_dir"
KEY_INSTANCES_DIR = "instances_dir"
KEY_SELECTED_JSON = "selected_json"
KEY_FLEET_DOMAIN = "fleet_domain"
KEY_TASK_GRAPH = "task_graph"
KEY_PEER_SENDERS = "peer_senders"
KEY_PEERS = "peers"
ROUTER_KEYS = (KEY_STATE_DIR, KEY_INSTANCES_DIR, KEY_SELECTED_JSON,
               KEY_FLEET_DOMAIN, KEY_TASK_GRAPH, KEY_PEER_SENDERS, KEY_PEERS)

# The verdict's keys are what `_read_selected` reads (config.py:538-588).
VERDICT_SCHEMA = "schema"
VERDICT_SELECTED = "selected"
VERDICT_NOT_SELECTED = "not_selected"
VERDICT_SLUG = "slug"


class Rendered(NamedTuple):
    doc: Dict[str, Any]
    text: str
    warnings: Tuple[str, ...]


# --- paths -------------------------------------------------------------------

def json_text(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def router_json_path(home: str) -> str:
    return posixpath.join(home, ROUTER_JSON_NAME)


def default_state_dir(home: str) -> str:
    return posixpath.join(home, STATE_DIRNAME)


def state_dir_problem(home: str, state_dir: object) -> Optional[str]:
    """A reason `state_dir` cannot be the router's private state directory, or
    None."""
    reason = render.host_path_problem("state_dir", state_dir)
    if reason:
        return reason
    assert isinstance(state_dir, str)
    if state_dir == "/":
        return "state_dir must not be the filesystem root"
    for label, other in (("instances_dir", render.instances_dir(home)),
                         ("selected_json", render.selected_json(home))):
        if render._overlap(state_dir, other):
            return (f"state_dir {state_dir!r} and {label} {other!r} must not be "
                    f"nested inside one another (equal, or one at or under the "
                    f"other): the router refuses that, because state_dir is "
                    f"router-private and must stay unreachable from the tree "
                    f"the host writes (amap-router-local `router/config.py:"
                    f"835-844`)")
    for label, other in (("payload", render.payload_dir(home)),
                         ("roster", render.roster_dir(home))):
        if render._overlap(state_dir, other):
            return (f"state_dir {state_dir!r} overlaps the {label} directory "
                    f"{other!r}: it is a sandbox mount source, and "
                    f"router-private state must be out of every sandbox's "
                    f"reach")
    return None


# --- rendering ---------------------------------------------------------------

def _members_checked(members: Optional[Sequence[membership.Member]],
                     workspace: str) -> List[membership.Member]:
    """`members` as a list. None is refused: absent is not empty. The router
    reads a missing verdict as unavailable, and nothing is admitted."""
    if members is None:
        raise RouterConfigError(
            "membership.json is absent: nothing has been recorded, which is "
            "not the same as a record that lists no one")
    listed = list(members)
    try:
        membership.require_workspace(listed, workspace)
    except membership.MembershipError as e:
        raise RouterConfigError(str(e)) from e
    return listed


def render_router_json(fleet: Dict[str, Any],
                       members: Optional[Sequence[membership.Member]],
                       home: str,
                       state_dir: Optional[str] = None) -> Rendered:
    """The `router.json` for `fleet` and `members`, checked by the router's own
    loader. `state_dir` defaults to `default_state_dir(home)`."""
    reason = render.home_problem(home)
    if reason:
        raise RouterConfigError(reason)
    listed = _members_checked(members, policy.workspace_of(fleet))

    state_dir = state_dir or default_state_dir(home)
    reason = state_dir_problem(home, state_dir)
    if reason:
        raise RouterConfigError(reason)

    names = [m.name for m in listed]
    try:
        r = policy.resolve(fleet, names)
    except policy.PolicyError as e:
        raise RouterConfigError(str(e)) from e
    if r.overlaps:
        raise RouterConfigError("\n".join(r.overlaps))

    doc: Dict[str, Any] = {
        KEY_STATE_DIR: state_dir,
        KEY_INSTANCES_DIR: render.instances_dir(home),
        KEY_SELECTED_JSON: render.selected_json(home),
        KEY_FLEET_DOMAIN: fleet[policy.FLEET_DOMAIN_KEY],
    }
    full = {n: sorted(x for x in names if x != n) for n in names}
    resolved = {n: sorted(r.task_graph.get(n, [])) for n in names}
    if (fleet.get(policy.TASK_GRAPH_KEY) == policy.TASK_GRAPH_ALL
            and resolved == full):
        doc[KEY_TASK_GRAPH] = ROUTER_TASK_GRAPH_ALL
    else:
        doc[KEY_PEER_SENDERS] = {n: resolved[n] for n in sorted(names)}
    mail = {n: sorted(r.peers[n]) for n in sorted(names) if r.peers.get(n)}
    if mail:
        doc[KEY_PEERS] = mail

    loader_check(doc)
    return Rendered(doc, json_text(doc), tuple(r.one_sided))


@contextlib.contextmanager
def _no_bytecode() -> Iterator[None]:
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def loader_check(doc: Mapping[str, Any]) -> Any:
    """Loads `doc` with the router's own `router.config.load_obj` and returns
    its `RouterConfig`. The router refuses what it refuses: its refusal is
    re-raised as a `RouterConfigError`. Bytecode is not written: `load_obj`
    imports `router.binding` lazily (`router/config.py:1232`), and that would
    otherwise write into the router's checkout."""
    text = json.dumps(doc)
    with _no_bytecode():
        try:
            return _rc.load_obj(json.loads(text))
        except _rc.ConfigError as e:
            raise RouterConfigError(
                f"the router's own loader refuses the rendering: {e}") from e


def render_verdict(members: Optional[Sequence[membership.Member]],
                   workspace: str) -> Dict[str, Any]:
    """The verdict file's document: each member's name as a slug, and nothing
    else. No ID, no timestamp: it is what `_read_selected` reads."""
    listed = _members_checked(members, workspace)
    return {
        VERDICT_SCHEMA: SELECTED_SCHEMA,
        VERDICT_SELECTED: [{VERDICT_SLUG: m.name}
                           for m in sorted(listed, key=lambda m: m.name)],
        VERDICT_NOT_SELECTED: [],
    }


# --- writing -----------------------------------------------------------------

def write_atomic(path: str, text: str) -> None:
    """Replace `path` with `text` all at once: a reader sees the old file or the
    new one. The parent must be an existing directory. A failure leaves the old
    file and no temporary file."""
    parent = posixpath.dirname(path)
    if not os.path.isdir(parent):
        raise RouterConfigError(
            f"cannot write {path}: {parent} is not a directory")
    fd, tmp = tempfile.mkstemp(dir=parent,
                               prefix="." + posixpath.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_verdict(home: str, doc: Mapping[str, Any]) -> str:
    """Writes the verdict atomically to `render.selected_json(home)` and returns
    the path. `home` is never created."""
    reason = render.home_problem(home)
    if reason:
        raise RouterConfigError(reason)
    path = render.selected_json(home)
    write_atomic(path, json_text(doc))
    return path


# --- lanes -------------------------------------------------------------------

def _lane_problem(home: str, name: str) -> Optional[str]:
    return membership.name_problem(name) or render.home_problem(home)


def lane_paths(home: str, name: str) -> List[str]:
    """Every lane root and every leaf of member `name`, from the router's
    `LANES` and `LANE_LEAVES`. Each root comes before its leaves."""
    reason = _lane_problem(home, name)
    if reason:
        raise RouterConfigError(reason)
    out: List[str] = []
    for lane in LANES:
        root = render.lane_dir(home, name, lane)
        out.append(root)
        out.extend(posixpath.join(root, leaf) for leaf in LANE_LEAVES[lane])
    return out


def create_lanes(home: str, name: str, *, dry_run: bool = False) -> List[str]:
    """Creates `instances_dir/<name>/` and every lane root and leaf under it,
    before the member's sandbox exists. Returns the paths it created, or that it
    would create under `dry_run`. It is idempotent.

    Only real directories are accepted: a symlink or a file in the way is
    refused, because the router scans with `follow_symlinks=False`. `home` must
    already exist. Directories get the default mode and belong to whoever runs
    this: the operator's one uid (D7). `verify` checks ownership."""
    reason = _lane_problem(home, name)
    if reason:
        raise RouterConfigError(reason)
    if not os.path.isdir(home):
        raise RouterConfigError(f"home {home!r} is not an existing directory")
    inst = render.instances_dir(home)
    paths = [inst, posixpath.join(inst, name), *lane_paths(home, name)]
    made: List[str] = []
    absent = False
    for p in paths:
        if absent:
            # Under a directory that does not exist, nothing does.
            made.append(p)
            continue
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            made.append(p)
            if dry_run:
                absent = True
                continue
            try:
                os.mkdir(p)
            except FileExistsError:
                made.pop()
                _require_real_directory(p)
            except OSError as e:
                raise RouterConfigError(f"cannot create {p}: {e}") from e
            continue
        except OSError as e:
            raise RouterConfigError(f"cannot inspect {p}: {e}") from e
        _require_real_directory(p, st)
    return made


def _require_real_directory(path: str, st: Optional[os.stat_result] = None) -> None:
    try:
        st = st if st is not None else os.lstat(path)
    except OSError as e:
        raise RouterConfigError(f"cannot inspect {path}: {e}") from e
    if stat.S_ISLNK(st.st_mode):
        raise RouterConfigError(
            f"{path} is a symlink: the router scans with follow_symlinks=False "
            f"(amap-router-local `router/config.py:648`), so a link is not a "
            f"lane. Remove it, and this will create the directory")
    if not stat.S_ISDIR(st.st_mode):
        raise RouterConfigError(f"{path} exists and is not a directory")


# --- drift -------------------------------------------------------------------

_LIST_MAPS = (KEY_PEER_SENDERS, KEY_PEERS)


def _map_lines(key: str, have: Mapping[str, Any],
               want: Mapping[str, Any]) -> List[str]:
    """Per-name lines for a `{name: [names]}` map. `peer_senders` compares each
    list as it is, `peers` as a set."""
    lines: List[str] = []
    for n in sorted(set(have) | set(want), key=str):
        h = have.get(n) if n in have else None
        w = want.get(n) if n in want else None
        if key == KEY_PEERS and isinstance(h, list) and isinstance(w, list):
            same = set(map(str, h)) == set(map(str, w))
        else:
            same = h == w
        if not same:
            lines.append(f"~ {key}[{n}] {h!r} -> {w!r}")
    return lines


def drift(existing: Any, rendered: Mapping[str, Any]) -> List[str]:
    """One line per difference between a decoded router.json and a rendering.
    Empty means the same content. `+` is missing from the file, `-` is not in
    the rendering, and `~` differs."""
    if not isinstance(existing, dict):
        return ["the file on disk is not a JSON object"]
    lines = [f"- {key}: not a key this deployment renders (a hand edit, lost at "
             f"the next render)"
             for key in existing if key not in ROUTER_KEYS]
    for key in ROUTER_KEYS:
        in_have, in_want = key in existing, key in rendered
        if not in_have and not in_want:
            continue
        have, want = existing.get(key), rendered.get(key)
        if (key in _LIST_MAPS and isinstance(have, dict)
                and isinstance(want, dict)):
            lines.extend(_map_lines(key, have, want))
        elif not in_have:
            lines.append(f"+ {key}: {want!r}")
        elif not in_want:
            lines.append(f"- {key}: {have!r}")
        elif have != want:
            lines.append(f"~ {key}: {have!r} -> {want!r}")
    return lines


def drift_on_disk(path: str, rendered: Rendered) -> List[str]:
    """`drift` against the file at `path`."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return [f"{path} is absent: render it to create it"]
    except (OSError, UnicodeDecodeError) as e:
        return [f"cannot read {path}: {e}"]
    if text == rendered.text:
        return []
    try:
        obj = json.loads(text)
    except ValueError as e:
        return [f"{path} is not valid JSON: {e}"]
    return drift(obj, rendered.doc) or [
        "same content, different bytes (whitespace or key order)"]
