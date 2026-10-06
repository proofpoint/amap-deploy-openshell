"""The fleet policy: `fleet.json`, read through amap-deploy-sandy's `fleet_policy`.

This is the adapter over amap-deploy-sandy's `fleet_policy` module. That module
is imported from the sandy checkout, never copied, so the two deployments cannot
disagree about who may mail or task whom, or about what an address is.

Only the agreed core is used (`CORE_NAMES`). `policy_checks` is deliberately not
imported: it belongs to sandy's write path and reaches for sandy's manifest.

`fleet.json` is a bare policy, in `fleet_policy`'s model (`fleet_domain`,
`groups`, `default_peers`, `peers`, `task_graph`, `task_deny`), plus one key of
this deployment's own, `workspace`: the OpenShell workspace every member's sandbox
lives in. `fleet.json` is deployment config that the router never reads, so
`workspace` is not a wire field. Sandy's manifest wrapper and its selection rule
are refused, not ignored: a control that is silently ignored looks like one that
works.

Instance names are OpenShell sandbox names. `membership.py` holds the rule that
they must satisfy, and every name the policy mentions is checked against it.

The fleet domain of a new fleet comes from `fleet_policy.derived_fleet_domain`
(D21), called through `domain_for`, never copied.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import sys
import types
from pathlib import Path
from typing import (Any, Dict, Iterable, Iterator, List, Mapping, NamedTuple,
                    Optional, Tuple, Union)

import membership

SANDY_VARIABLE = "AMAP_SANDY_REPO"
SANDY_DIR_NAME = "amap-deploy-sandy"
SANDY_CONFIRM = "fleet_policy.py"

# The names this adapter uses from `fleet_policy`, and no others.
CORE_NAMES = ("PolicyError", "load_policy", "default_policy", "resolve_peers",
              "one_sided", "resolve_task_graph", "resolve_task_deny",
              "transpose_task_graph", "overlapping_pairs", "address_for",
              "router_address", "FLEET_DOMAIN_KEY", "TASK_GRAPH_KEY",
              "TASK_GRAPH_ALL", "TASK_DENY_KEY", "ALLOW_ANY", "GROUP_SIGIL",
              "ALL_GROUP", "SCHEMA_VERSION",
              # D21: the per-host fleet domain, pinned by sandy's
              # test_shared_policy_surface
              "derived_fleet_domain", "DEFAULT_DOMAIN_BASE")

WORKSPACE_KEY = "workspace"

# Keys only a sandy feature manifest carries.
MANIFEST_KEYS = ("feature", "schema", "create", "mounts", "entry", "expose",
                 "agent_args", "receives")
# Keys of sandy's policy that this deployment does not evaluate.
SANDY_ONLY_KEYS = ("sandboxes", "agents", "container_recreate_interval_hours")
# Added by `load_policy` when absent; removed from the loaded policy.
_INJECTED_KEYS = ("sandboxes", "agents")


class SandyNotFound(ImportError):
    """The amap-deploy-sandy checkout, or a usable `fleet_policy`, is missing."""


def find_sandy(env: Optional[Mapping[str, str]] = None,
               start: Optional[Path] = None) -> Path:
    """The amap-deploy-sandy checkout.

    When `$AMAP_SANDY_REPO` is set and not empty it is the only place searched.
    Otherwise the search walks up from this file's directory and takes the
    nearest `<ancestor>/amap-deploy-sandy` that contains `fleet_policy.py`. A
    directory with the right name and no `fleet_policy.py` is skipped. Paths use
    `.absolute()`, never `.resolve()`, so a symlinked checkout stays where it is.
    """
    env = os.environ if env is None else env
    value = env.get(SANDY_VARIABLE)
    if value:
        cand = Path(value).absolute()
        if (cand / SANDY_CONFIRM).is_file():
            return cand
        raise SandyNotFound(
            f"${SANDY_VARIABLE}={value} does not contain {SANDY_CONFIRM}; it is "
            f"the only place searched because the variable names it. Point it "
            f"at the {SANDY_DIR_NAME} checkout, or unset it to search beside "
            f"this repository.")
    here = Path(__file__).absolute().parent if start is None \
        else Path(start).absolute()
    for ancestor in [here, *here.parents]:
        cand = ancestor / SANDY_DIR_NAME
        if (cand / SANDY_CONFIRM).is_file():
            return cand
    raise SandyNotFound(
        f"cannot find the {SANDY_DIR_NAME} checkout (confirmed by "
        f"{SANDY_CONFIRM}) beside any ancestor of {here}. Check it out beside "
        f"this repository, or set ${SANDY_VARIABLE}.")


def _load_core(root: Path) -> types.ModuleType:
    """`fleet_policy` from `root`, loaded from its file. It is not put on
    `sys.path` (so sandy's other modules stay unimportable from here) and not
    registered in `sys.modules`. Bytecode is not written, so the checkout is
    never modified."""
    path = root / SANDY_CONFIRM
    spec = importlib.util.spec_from_file_location("fleet_policy", str(path))
    if spec is None or spec.loader is None:
        raise SandyNotFound(f"cannot load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    for name in CORE_NAMES:
        if not hasattr(module, name):
            raise SandyNotFound(
                f"{path} lacks {name}: the agreed interface changed")
    return module


_fp = _load_core(find_sandy())

PolicyError = _fp.PolicyError
FLEET_DOMAIN_KEY = _fp.FLEET_DOMAIN_KEY
TASK_GRAPH_KEY = _fp.TASK_GRAPH_KEY
TASK_GRAPH_ALL = _fp.TASK_GRAPH_ALL
TASK_DENY_KEY = _fp.TASK_DENY_KEY
ALLOW_ANY = _fp.ALLOW_ANY
GROUP_SIGIL = _fp.GROUP_SIGIL
router_address = _fp.router_address
DEFAULT_DOMAIN_BASE = _fp.DEFAULT_DOMAIN_BASE


class Resolved(NamedTuple):
    peers: Dict[str, List[str]]        # instance -> its resolved mail peers
    task_graph: Dict[str, List[str]]   # recipient -> the senders that may task it
    one_sided: List[str]               # mail entries that look present and do nothing
    overlaps: List[str]                # pairs declared on both lanes


# `fleet_policy`'s messages point at sandy's manifest and at its commands.
# Its wording is not part of the agreed interface, so a test catches drift.
_REWORDS: "Tuple[Tuple[re.Pattern[str], str], ...]" = tuple(
    (re.compile(pattern), replacement) for pattern, replacement in (
        (r"the manifest's `feature` section", "fleet.json"),
        (r"\(Instance names are the sandy slugs, verbatim — "
         r"`amap-sandy\.py list` prints them[^)]*\)",
         "(Instance names are OpenShell sandbox names, as membership.json "
         "records them.)"),
        (r"widen the selection rule", "provision the sandbox"),
        (r"Enroll the instance", "Provision the sandbox"),
        (r"see the module docstring's 'THE DECISION' section: ", ""),
    ))


def _reword(message: str) -> str:
    for pattern, replacement in _REWORDS:
        message = pattern.sub(replacement, message)
    return message


@contextlib.contextmanager
def _sandy_errors() -> Iterator[None]:
    """Re-raise `fleet_policy`'s errors in this deployment's words."""
    try:
        yield
    except _fp.PolicyError as e:
        raise PolicyError(_reword(str(e))) from e


def _instance_mentions(policy: Dict[str, Any]) -> List[Tuple[str, str]]:
    """`(where, name)` for every instance name the policy mentions."""
    found: List[Tuple[str, str]] = []
    for group, members in (policy.get("groups") or {}).items():
        found += [(f"groups.{group}", m) for m in members]
    found += [("default_peers", p) for p in policy.get("default_peers") or []
              if p != ALLOW_ANY and not p.startswith(GROUP_SIGIL)]
    for key, values in (policy.get("peers") or {}).items():
        found.append(("peers (key)", key))
        found += [(f"peers.{key}", v) for v in values
                  if v != ALLOW_ANY and not v.startswith(GROUP_SIGIL)]
    graph = policy.get(TASK_GRAPH_KEY)
    if isinstance(graph, dict):
        for recipient, senders in graph.items():
            found.append((f"{TASK_GRAPH_KEY} (key)", recipient))
            found += [(f"{TASK_GRAPH_KEY}.{recipient}", s) for s in senders]
    for i, pair in enumerate(policy.get(TASK_DENY_KEY) or []):
        found += [(f"{TASK_DENY_KEY}[{i}]", name) for name in pair]
    return found


def domain_for(runtime: str, hostname: str, base: str = DEFAULT_DOMAIN_BASE) -> str:
    """`<runtime>.<host>.<base>`, or `<runtime>.<base>` when the hostname
    leaves no label: amap-deploy-sandy's `fleet_policy.derived_fleet_domain`,
    called, never copied (D1, D21). Pure: the caller passes the hostname."""
    with _sandy_errors():
        return _fp.derived_fleet_domain(runtime, hostname, base)


def load_fleet(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    """The validated policy in `path`, with `workspace` in it.

    There is no default policy: `fleet_domain` and `workspace` are the
    operator's to write, and neither has a defensible default."""
    p = Path(path)
    if not p.is_file():
        raise PolicyError(
            f"{p}: no fleet policy here. This deployment has no default: "
            f"{FLEET_DOMAIN_KEY} and {WORKSPACE_KEY} are the operator's to "
            f"write. Start from examples/fleet.json.")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise PolicyError(f"cannot read {p}: {e}") from e
    except json.JSONDecodeError as e:
        raise PolicyError(f"{p} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise PolicyError(f"{p}: expected a JSON object at the top level")

    manifest = [k for k in MANIFEST_KEYS if k in raw]
    if manifest:
        raise PolicyError(
            f"{p}: this is a sandy feature manifest (has {', '.join(manifest)}); "
            f"fleet.json is a bare policy: move the `feature` section's keys to "
            f"the top level and drop the rest")
    for key in SANDY_ONLY_KEYS:
        if key in raw:
            raise PolicyError(
                f"{p}: {key!r} is amap-deploy-sandy's; this deployment does not "
                f"evaluate it (membership is membership.json). Remove it.")

    if WORKSPACE_KEY not in raw:
        raise PolicyError(
            f"{p}: {WORKSPACE_KEY!r} is required: the one OpenShell workspace "
            f"every member's sandbox lives in")
    workspace = raw[WORKSPACE_KEY]
    # Names are unique only within a workspace:
    #   OpenShell main@acbac9c:crates/openshell-server/src/persistence/tests.rs:1027
    # so a fleet has one workspace and its members are told apart by name alone.
    if not isinstance(workspace, str):
        raise PolicyError(
            f"{p}: a fleet spans one OpenShell workspace; {WORKSPACE_KEY!r} "
            f"must be one name, got {workspace!r}. Names are unique only within "
            f"a workspace.")
    reason = membership.workspace_problem(workspace)
    if reason:
        raise PolicyError(f"{p}: {WORKSPACE_KEY}: {reason}")

    with _sandy_errors():
        policy = _fp.load_policy(p)
    if FLEET_DOMAIN_KEY not in policy:
        raise PolicyError(
            f"{p}: {FLEET_DOMAIN_KEY!r} is required: every member's address is "
            f"<name>@{FLEET_DOMAIN_KEY}, for example agents.example.org")

    for where, name in _instance_mentions(policy):
        reason = membership.name_problem(name)
        if reason:
            raise PolicyError(f"{p}: {where}: {reason}")

    for key in _INJECTED_KEYS:
        policy.pop(key, None)
    policy[WORKSPACE_KEY] = workspace
    return policy


def workspace_of(policy: Dict[str, Any]) -> str:
    return policy[WORKSPACE_KEY]


def named_instances(policy: Dict[str, Any]) -> List[str]:
    """Every instance name the policy mentions, sorted, once each."""
    return sorted({name for _, name in _instance_mentions(policy)})


def _checked_names(names: Iterable[str]) -> List[str]:
    checked = sorted(set(names))
    for name in checked:
        reason = membership.name_problem(name)
        if reason:
            raise PolicyError(reason)
    return checked


def addresses(policy: Dict[str, Any], names: Iterable[str]) -> Dict[str, str]:
    """`{name: address}`, each address made by `fleet_policy.address_for`."""
    checked = _checked_names(names)
    domain = policy[FLEET_DOMAIN_KEY]
    return {name: _fp.address_for(name, domain) for name in checked}


def resolve(policy: Dict[str, Any], names: Iterable[str]) -> Resolved:
    """Who may mail and who may task whom, for the members `names`. Overlaps
    between the two lanes are returned, not raised: the caller decides."""
    checked = _checked_names(names)
    with _sandy_errors():
        peers = _fp.resolve_peers(policy, checked)
        graph = _fp.resolve_task_graph(policy, checked)
        return Resolved(peers, graph, _fp.one_sided(peers),
                        _fp.overlapping_pairs(policy, peers, graph))
