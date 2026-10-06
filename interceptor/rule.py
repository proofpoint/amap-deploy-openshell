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
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

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
    """`{source: read_only}` for the rows rendered for `name`."""
    return {row.source: row.read_only for row in render.member_rows(home, name)}


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
                 fleet: Mapping[str, Any]) -> Decision:
    """The decision for a proposed CreateSandbox. Pure but for the symlink
    check (R7d), which reads the host."""
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
