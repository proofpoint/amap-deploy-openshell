"""The validate rule for CreateSandbox (docs/INTERCEPTOR.md section 4).

The input is `proposed_operation` as a dict. The output is a `Decision`. The
rule gathers every reason and never raises on JSON-shaped input. It reuses
`render.member_rows`, so it cannot drift from the mounts `provision` renders.

OpenShell facts, cited at OpenShell v0.1.2:

- The gateway serialises with prost-reflect's JSON names (lowerCamelCase), so
  both spellings of every inspected field are read
  (crates/openshell-gateway-interceptors/src/proto_json.rs:83-96,116-124,168).
- CreateSandboxRequest fields: proto/openshell.proto:1242-1265. SandboxSpec:
  :1017-1048. SandboxTemplate: :1068-1096.
- The docker driver config is `mounts` and `cdi_devices`, with unknown fields
  denied (crates/openshell-driver-docker/src/lib.rs:701-710); a mount holds
  `type`, `source`, `target`, `read_only` and `selinux_label`, and `read_only`
  defaults to true (lib.rs:731-741,769-771).
- A stored template's driver config is merged after interception
  (crates/openshell-server/src/grpc/sandbox.rs:434-457,788-812).

Standard library plus this repository's `render`, `policy`, `membership` and
`l1_kit`. Python 3.9 compatible.
"""

from __future__ import annotations

import os
import re
from typing import (Any, Callable, Dict, List, Mapping, NamedTuple, Optional,
                    Tuple)

import host_registry
import l1_kit
import membership
import policy
import render

REASON_PREFIX = "amap-openshell interceptor: "

REQUEST_FIELDS = ("workspaceScope", "spec", "name", "labels", "annotations",
                  "awaitMainProcessAttachment", "workloadTemplate",
                  "requestId", "serviceExposures")
SPEC_FIELDS = ("logLevel", "environment", "template", "policy", "providers",
               "resourceRequirements", "command", "tty",
               "providerAttachmentEpoch")
TEMPLATE_FIELDS = ("image", "runtimeClassName", "agentSocket", "labels",
                   "annotations", "environment", "resources",
                   "userNamespaces", "driverConfig")
MOUNT_KEYS = ("type", "source", "target", "read_only")
MOUNTS_KEY = "mounts"

_MISSING = object()


def snake(camel: str) -> str:
    """`workloadTemplate` becomes `workload_template`."""
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), camel)


class Decision(NamedTuple):
    allowed: bool
    reasons: Tuple[str, ...]


def allowed_binds(home: str, name: str) -> Dict[str, bool]:
    """`{source: read_only}` for the rows rendered for `name`. With the host
    registry (D27) the identity directory is added by `_registry_checks`, so
    the expected set is these rows plus one registry read."""
    return {row.source: row.read_only for row in render.member_rows(home, name)}


# The variables a create may not carry when the fleet uses the host registry:
# payload/amap-main sets them itself, from name.json (D27).
SELF_ADDRESS_VARIABLES = (render.SELF_ENV, render.PEER_SELF_ENV)


def _environments(spec: Any, reasons: List[str]
                  ) -> List[Tuple[str, Mapping[str, Any]]]:
    """Every environment a create carries: spec.environment and
    spec.template.environment, in either spelling."""
    out: List[Tuple[str, Mapping[str, Any]]] = []
    if not isinstance(spec, dict):
        return out
    present, env = _field(spec, "environment", reasons)
    if present:
        if isinstance(env, dict):
            out.append(("spec.environment", env))
        else:
            reasons.append("spec.environment must be an object")
    t_present, template = _field(spec, "template", reasons)
    if t_present and isinstance(template, dict):
        present, env = _field(template, "environment", reasons)
        if present:
            if isinstance(env, dict):
                out.append(("spec.template.environment", env))
            else:
                reasons.append("spec.template.environment must be an object")
    return out


def _environment_checks(spec: Any, expected: host_registry.Expected,
                        reasons: List[str]) -> None:
    """the identity variables are present and equal the
    registry's binding, wherever they are given and however the two
    environments merge; the self-address variables are absent from both."""
    want = dict(pair.split("=", 1) for pair in render.identity_env(expected))
    envs = _environments(spec, reasons)
    for where, env in envs:
        for key in SELF_ADDRESS_VARIABLES:
            if key in env:
                reasons.append(f"{where}.{key} is refused: with the host "
                               f"registry the address comes only from "
                               f"name.json")
    for key, value in want.items():
        given = [(where, env[key]) for where, env in envs if key in env]
        if not given:
            reasons.append(f"the create does not set {key}: the sandbox could "
                           f"not check its identity")
            continue
        for where, got in given:
            if got != value:
                reasons.append(f"{where}.{key} is not the host registry's "
                               f"value")


def _registry_checks(spec: Any, mounts: List[Any], name: str, home: str,
                     fleet: Mapping[str, Any], allowed: Dict[str, bool],
                     reasons: List[str],
                     show: Callable = host_registry.show) -> None:
    """D27, for a member's create when the fleet uses the host registry.
    The registry is asked one read-only question, `show`; anything but an
    active binding whose name.json is current refuses. Then the identity
    directory's row must be present and exact, no other target may overlap
    its target, no source may touch the registry directory, and the
    identity variables must equal the binding."""
    try:
        cfg = host_registry.load_config(home, keep_out_of=(
            render.payload_dir(home), render.roster_dir(home),
            render.instances_dir(home)))
    except host_registry.ConfigError as e:
        reasons.append(f"the host registry cannot be used: {e}")
        return
    for problem in host_registry.layout_problems(cfg, render.instances_dir(home)):
        reasons.append(problem)
    look = show(cfg, policy.workspace_of(dict(fleet)), name)
    if look.kind != host_registry.BOUND:
        reasons.append(f"the host registry's show for {name} answers "
                       f"{look.kind} ({look.code or ''} {look.detail}): only a "
                       f"registered member may be created".replace(" )", ")"))
        return
    b = look.binding
    domain = fleet[policy.FLEET_DOMAIN_KEY]
    if b["state"] != "active" or b.get("name_json") != "current":
        reasons.append(f"the host registry's binding for {name} is "
                       f"{b['state']} with name.json {b.get('name_json')}")
        return
    if b["name"] != name or b["address"] != f"{name}@{domain}":
        reasons.append(f"the host registry's name and address for {name} are "
                       f"{b['name']!r}, {b['address']!r}: until the router "
                       f"keys by logical_id they must be {name!r}, "
                       f"{name + '@' + domain!r}")
        return
    problem = host_registry.identity_dir_problem(cfg, b)
    if problem:
        reasons.append(problem)
        return
    expected = host_registry.expected_of(cfg, b)
    allowed[expected.identity_dir] = True
    rows = [m for m in mounts if isinstance(m, dict)]
    ident = [m for m in rows if m.get("source") == expected.identity_dir]
    if len(ident) != 1:
        reasons.append(f"the create must bind {name}'s identity directory "
                       f"{expected.identity_dir} exactly once (it has "
                       f"{len(ident)})")
    else:
        m = ident[0]
        if m.get("target") != render.IDENTITY_TARGET:
            reasons.append(f"the identity directory must be bound at "
                           f"{render.IDENTITY_TARGET}, not {m.get('target')}")
        if m.get("read_only", True) is not True:
            reasons.append("the identity directory must be bound read-only")
    # Targets are compared as text below, so each must be absolute and its
    # own normal form: /opt/amap/./identity and lanes/../identity land on the
    # identity target without spelling it.
    for i, m in enumerate(rows):
        problem = render._path_problem(f"mounts[{i}].target", m.get("target"))
        if problem:
            reasons.append(problem)
    for i, m in enumerate(rows):
        target = m.get("target")
        if m.get("source") == expected.identity_dir or \
                not isinstance(target, str):
            continue
        if render._overlap(target, render.IDENTITY_TARGET):
            reasons.append(f"mounts[{i}].target {target} overlaps the identity "
                           f"target {render.IDENTITY_TARGET}")
    for i, m in enumerate(rows):
        src = m.get("source")
        if not isinstance(src, str) or render.host_path_problem("s", src):
            continue
        try:
            rel = host_registry.relation(src, cfg.registry)
        except (OSError, ValueError) as e:
            reasons.append(f"mounts[{i}].source {src} cannot be compared with "
                           f"the registry directory: {e}")
            continue
        if rel != "disjoint":
            reasons.append(f"mounts[{i}].source {src} touches the host "
                           f"registry's directory: it is never mounted")
    _environment_checks(spec, expected, reasons)


def _get(obj: Mapping[str, Any], camel: str) -> Tuple[bool, Any, Optional[str]]:
    """`(present, value, reason)` for a field in either spelling."""
    sn = snake(camel)
    in_c, in_s = camel in obj, sn in obj
    if in_c and in_s and sn != camel:
        return True, obj[camel], f"both {camel} and {sn} are given"
    if in_c:
        return True, obj[camel], None
    if in_s:
        return True, obj[sn], None
    return False, None, None


def _unknown(level: str, obj: Mapping[str, Any], fields: Tuple[str, ...],
             reasons: List[str]) -> None:
    known = set(fields) | {snake(f) for f in fields}
    for key in obj:
        if key not in known:
            reasons.append(f"unknown field {level}.{key}")


def _field(obj: Mapping[str, Any], camel: str, reasons: List[str]
           ) -> Tuple[bool, Any]:
    present, value, why = _get(obj, camel)
    if why:
        reasons.append(why)
    return present, value


def _driver_mounts(config: Any, reasons: List[str]) -> Any:
    """The mounts list under a driverConfig (R3), or None when there is none."""
    if not isinstance(config, dict):
        reasons.append("driverConfig must be an object")
        return None
    for key in config:
        if key != render.DRIVER:
            reasons.append(f"driverConfig.{key} is refused: only "
                           f"{render.DRIVER} is accepted")
    if render.DRIVER not in config:
        return None
    docker = config[render.DRIVER]
    if not isinstance(docker, dict):
        reasons.append(f"driverConfig.{render.DRIVER} must be an object")
        return None
    for key in docker:
        if key != MOUNTS_KEY:
            reasons.append(f"driverConfig.{render.DRIVER}.{key} is refused: "
                           f"only {MOUNTS_KEY} is accepted")
    if MOUNTS_KEY not in docker:
        return None
    mounts = docker[MOUNTS_KEY]
    if not isinstance(mounts, list):
        reasons.append(f"driverConfig.{render.DRIVER}.{MOUNTS_KEY} must be "
                       f"a list")
        return None
    return mounts


def _identity(operation: Mapping[str, Any], fleet: Mapping[str, Any],
              reasons: List[str]) -> Tuple[Any, bool]:
    """R5. Returns the name and whether it is a valid fleet member."""
    _, name = _field(operation, "name", reasons)
    member = False
    if not isinstance(name, str) or not name:
        reasons.append("name is required when mounts are given")
    else:
        problem = membership.name_problem(name)
        if problem:
            reasons.append(f"name: {problem}")
        elif name not in policy.named_instances(dict(fleet)):
            reasons.append(f"name {name!r} is not a member of the fleet")
        else:
            member = True
    present, scope = _field(operation, "workspaceScope", reasons)
    want = policy.workspace_of(dict(fleet))
    if not present or not isinstance(scope, dict):
        reasons.append("workspaceScope must name the fleet's workspace")
    else:
        keys = set(scope)
        if keys != {"workspace"}:
            reasons.append("workspaceScope must hold only workspace "
                           "(allWorkspaces is refused)")
        elif scope["workspace"] != want:
            reasons.append(f"workspaceScope.workspace {scope['workspace']!r} "
                           f"is not the fleet's workspace {want!r}")
    return name, member


def _check_mount(i: int, mount: Any, name: Any, allowed: Mapping[str, bool],
                 reasons: List[str]) -> None:
    where = f"mounts[{i}]"
    if not isinstance(mount, dict):
        reasons.append(f"{where} must be an object")
        return
    bad = False
    if mount.get("type") != render.MOUNT_TYPE:
        reasons.append(f"{where}.type {mount.get('type')!r} is refused: only "
                       f"{render.MOUNT_TYPE!r} is accepted")
        bad = True
    for key in mount:
        if key not in MOUNT_KEYS:
            reasons.append(f"{where}.{key} is refused: only "
                           f"{', '.join(MOUNT_KEYS)} are accepted")
            bad = True
    if "read_only" in mount and not isinstance(mount["read_only"], bool):
        reasons.append(f"{where}.read_only must be a boolean")
        bad = True
    if bad:
        return
    src = mount.get("source")
    problem = render.host_path_problem("bind source", src)
    if problem:
        reasons.append(f"{where}: {problem}")
        return
    if src not in allowed:
        who = name if isinstance(name, str) and name else "an unnamed sandbox"
        reasons.append(
            f"bind source {src} is not one {who} may mount (only the "
            f"payload, the roster and its own instances/<name>/<lane>)")
        return
    if mount.get("read_only", True) != allowed[src]:
        reasons.append(f"bind source {src} must have read_only = "
                       f"{allowed[src]}")
    real = os.path.realpath(src)
    if real != src:
        reasons.append(f"bind source {src} resolves to {real}: a component "
                       f"is a symlink")


def check_create(operation: object, home: str,
                 fleet: Mapping[str, Any],
                 show: Callable = host_registry.show) -> Decision:
    """The decision for a proposed CreateSandbox. Pure but for the symlink
    check (R7d), which reads the host, and, when the fleet uses the host
    registry (D27), one read-only `show` and the identity checks."""
    reasons: List[str] = []
    problem = render.home_problem(home)
    if problem:
        reasons.append(problem)
    if not isinstance(operation, dict):
        reasons.append("the operation must be an object")
        return Decision(False, tuple(reasons))

    # R1
    present, wt = _field(operation, "workloadTemplate", reasons)
    if present and wt not in (None, "", {}, []):
        reasons.append("workloadTemplate is refused: a stored template's "
                       "driver config is merged after interception")
    # R2
    _unknown("request", operation, REQUEST_FIELDS, reasons)
    config: Any = _MISSING
    present, spec = _field(operation, "spec", reasons)
    if present and not isinstance(spec, dict):
        reasons.append("spec must be an object")
    elif present:
        _unknown("spec", spec, SPEC_FIELDS, reasons)
        t_present, template = _field(spec, "template", reasons)
        if t_present and not isinstance(template, dict):
            reasons.append("spec.template must be an object")
        elif t_present:
            _unknown("spec.template", template, TEMPLATE_FIELDS, reasons)
            c_present, value = _field(template, "driverConfig", reasons)
            if c_present:
                config = value
    # R3
    mounts = None
    if config is not _MISSING:
        mounts = _driver_mounts(config, reasons)
    # R4
    if isinstance(mounts, list) and mounts:
        # R5
        name, member = _identity(operation, fleet, reasons)
        allowed: Dict[str, bool] = {}
        if member and not render.home_problem(home):
            allowed = allowed_binds(home, name)
            # D27
            if policy.uses_registry(fleet):
                _registry_checks(spec, mounts, name, home, fleet, allowed,
                                 reasons, show)
        # R6, R7
        for i, mount in enumerate(mounts):
            _check_mount(i, mount, name, allowed, reasons)
    return Decision(not reasons, tuple(reasons))


def evaluate_create(operation: object, home: str) -> Decision:
    """`check_create` with `fleet.json` read now (R9). Never raises."""
    try:
        try:
            fleet = policy.load_fleet(l1_kit.fleet_json(home))
        except (policy.PolicyError, OSError, ValueError) as e:
            return Decision(False, (f"fleet.json cannot be loaded: {e}",))
        return check_create(operation, home, fleet)
    except Exception as e:  # a decision is always returned
        return Decision(False, (f"internal error: {type(e).__name__}: {e}",))
