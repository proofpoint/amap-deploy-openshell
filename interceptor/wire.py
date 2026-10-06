"""The interceptor's wire form on plain dicts, and the gateway.toml fragment.

`server.py` converts protobuf messages to and from the dicts used here, so
nothing in this module imports grpc or protobuf. Dict keys are the proto field
names (snake_case), which `json_format.ParseDict` reads back into messages.

OpenShell facts, cited at OpenShell v0.1.2:

- The manifest's fields: proto/gateway_interceptor.proto:97-115,124-144. The
  validate phase is 3 (:36-41). Interceptors must reject unmet requirements
  (:30-32).
- The gateway's Describe metadata carries protocol 1.0 and the contract as both
  supported and required capability
  (crates/openshell-core/src/extension_protocol.rs:12-13,104-116).
- A Describe refusal stops the gateway
  (crates/openshell-gateway-interceptors/src/plan.rs:218-243). With gateway
  signing, `authorization` metadata arrives (plan.rs:193-205); this deployment
  refuses it rather than verify a token (docs/INTERCEPTOR.md section 2).
- `gateway info -o json` lists `extensions[]` with `kind` and `configured_name`
  (crates/openshell-cli/src/commands/gateway.rs:456-464,520-547).
- A fail-closed denial reads `gateway interceptor '<name>' failed closed: <err>`
  (crates/openshell-gateway-interceptors/src/runtime.rs:385-390).

Standard library plus `interceptor.rule` and `render`.
"""

from __future__ import annotations

import posixpath
from typing import Any, Dict, Mapping, Optional, Sequence

import render
from interceptor import rule

INTERCEPTOR_NAME = "amap-openshell-mounts"
BINDING_ID = "create-sandbox-mounts"
SERVICE = "openshell.v1.OpenShell"
METHOD = "CreateSandbox"
CREATE_SANDBOX_RPC = SERVICE + "/" + METHOD
PHASE_VALIDATE = 3
PHASE_CONFIG_NAME = "validate"
CONTRACT = "openshell.gateway-interceptor.contract"
PROTOCOL_MAJOR, PROTOCOL_MINOR = 1, 0
IMPLEMENTATION_NAME = "amap-openshell/interceptor"
# Diagnostics only (extension.proto:24-25). Bump it when the rule's decision
# changes.
IMPLEMENTATION_VERSION = "1"

FAIL_CLOSED = "fail_closed"
BINDING_POLICY = "exact"
TIMEOUT = "2s"
STATUS_DENIED = "PERMISSION_DENIED"
ANNOTATION_RULE, ANNOTATION_DECISION = "amap.rule", "amap.decision"
RULE_ID = "create-mounts"
REASON_PREFIX = rule.REASON_PREFIX

# sockaddr_un.sun_path holds 108 bytes, with the terminating NUL.
MAX_SOCKET_PATH = 107
RUN_DIRNAME = "run"
SOCKET_NAME = "interceptor.sock"
EXTENSION_KIND = "gateway-interceptor"
FAILED_CLOSED_MARK = "failed closed"

PROBE_PREFIX = "amap-verify-"
PROBE_TARGET = "/opt/amap/probe"
PROBE_POISON_KEY = "amap_verify_probe"
PROBE_COMMAND = ("true",)
PROBE_REFUSED = "refused by the interceptor"
PROBE_REFUSED_OTHER = "refused by the interceptor, but not for the outbox"
PROBE_FAILED_CLOSED = "the interceptor failed closed"
PROBE_BY_GATEWAY = "refused by the gateway, not the interceptor"
PROBE_CREATED = "created: nothing refused it"

_PHASES = ("modify_operation", "validate", "post_commit")


class Refused(Exception):
    """Describe refuses: the gateway then does not start."""


def manifest(version: str) -> Dict[str, Any]:
    return {
        "name": INTERCEPTOR_NAME,
        "bindings": [{
            "id": BINDING_ID,
            "selector": {"rpc": CREATE_SANDBOX_RPC},
            "phases": [PHASE_VALIDATE],
            "failure_policy": FAIL_CLOSED,
        }],
        "failure_policy": FAIL_CLOSED,
        "provider_profiles": False,
        "expected_audience": "",
        "extension": {
            "protocol_version": {"major": PROTOCOL_MAJOR,
                                 "minor": PROTOCOL_MINOR},
            "implementation_name": IMPLEMENTATION_NAME,
            "implementation_version": version,
            "supported_capabilities": [CONTRACT],
            "required_capabilities": [CONTRACT],
        },
    }


def _pick(d: Mapping[str, Any], snake: str) -> Any:
    camel = snake.split("_")[0] + "".join(
        p.title() for p in snake.split("_")[1:])
    return d.get(snake, d.get(camel))


def describe(gateway: Optional[Mapping[str, Any]],
             metadata: Mapping[str, str]) -> Dict[str, Any]:
    """The manifest, or `Refused` when the gateway's metadata is unmet."""
    if any(str(k).lower() == "authorization" for k in metadata):
        raise Refused("the gateway sent authorization metadata: this "
                      "interceptor does not verify tokens (disable gateway "
                      "signing for it)")
    if not isinstance(gateway, Mapping):
        raise Refused("the gateway metadata is missing")
    version = _pick(gateway, "protocol_version")
    major = version.get("major") if isinstance(version, Mapping) else None
    if major != PROTOCOL_MAJOR:
        raise Refused(f"the gateway's protocol major version {major!r} is "
                      f"not {PROTOCOL_MAJOR}")
    supported = _pick(gateway, "supported_capabilities") or []
    if CONTRACT not in supported:
        raise Refused(f"the gateway does not support {CONTRACT}")
    required = _pick(gateway, "required_capabilities") or []
    extra = [c for c in required if c != CONTRACT]
    if extra:
        raise Refused(f"the gateway requires capabilities this interceptor "
                      f"does not have: {', '.join(map(str, extra))}")
    return manifest(IMPLEMENTATION_VERSION)


def allowance() -> Dict[str, Any]:
    return {"allowed": True,
            "log_annotations": {ANNOTATION_RULE: RULE_ID,
                                ANNOTATION_DECISION: "allow"}}


def denial(reasons: Sequence[str]) -> Dict[str, Any]:
    return {"allowed": False, "status_code": STATUS_DENIED,
            "reason": REASON_PREFIX + "; ".join(reasons),
            "log_annotations": {ANNOTATION_RULE: RULE_ID,
                                ANNOTATION_DECISION: "deny"}}


def evaluate(evaluation: Mapping[str, Any], home: str) -> Dict[str, Any]:
    """The result for an Evaluate request: a denial unless it is a CreateSandbox
    validate phase the rule allows."""
    if not isinstance(evaluation, Mapping):
        return denial(["the evaluation is not an object"])
    if evaluation.get("service") != SERVICE or \
            evaluation.get("method") != METHOD:
        return denial([f"this interceptor binds only {CREATE_SANDBOX_RPC}"])
    if _pick(evaluation, "binding_id") != BINDING_ID:
        return denial(["the evaluation is for another binding"])
    present = [p for p in _PHASES if _pick(evaluation, p) is not None]
    if present != ["validate"]:
        return denial(["this interceptor binds only the validate phase"])
    validate = _pick(evaluation, "validate")
    if not isinstance(validate, Mapping):
        return denial(["the validate payload is not an object"])
    try:
        d = rule.evaluate_create(_pick(validate, "proposed_operation"), home)
    except Exception as e:
        return denial([f"internal error: {type(e).__name__}"])
    return allowance() if d.allowed else denial(d.reasons)


def socket_path(home: str) -> str:
    reason = render.home_problem(home)
    if reason:
        raise ValueError(reason)
    return posixpath.join(home, RUN_DIRNAME, SOCKET_NAME)


def gateway_fragment(home: str) -> str:
    """The gateway.toml block that registers the interceptor (section 6)."""
    return f"""\
# The amap-openshell mounts interceptor (docs/INTERCEPTOR.md section 6).
# Start interceptor/server.py first, then restart the gateway.
# Registration is static, so a restart is needed after a change
# (OpenShell v0.1.2:docs/extensibility/gateway-interceptors.mdx:102).
# `exact` fails startup when the configured and declared bindings differ
# (OpenShell v0.1.2:crates/openshell-gateway-interceptors/src/plan.rs:249-289),
# and under it only the configured failure_policy counts (plan.rs:258-261).
# A unix:// endpoint is supported
# (OpenShell v0.1.2:crates/openshell-core/src/config.rs:410-412). The timeout
# default is 500 ms (plan.rs:207-210).
# allow_insecure_transport opts this registration out of the gateway's
# extension JWT (D24). The gateway logs a warning at every start
# (OpenShell v0.1.2:crates/openshell-server/src/lib.rs:113-140). This
# interceptor does not verify tokens, and it refuses a Describe that carries
# one, so removing this line stops the gateway starting rather than quietly
# trusting an unchecked token. The boundary is the socket: 0600 in a 0700
# directory (docs/INTERCEPTOR.md section 2).
[[openshell.gateway.interceptors]]
name           = "{INTERCEPTOR_NAME}"
grpc_endpoint  = "unix://{socket_path(home)}"
allow_insecure_transport = true
failure_policy = "{FAIL_CLOSED}"
binding_policy = "{BINDING_POLICY}"
timeout        = "{TIMEOUT}"

[[openshell.gateway.interceptors.bindings]]
rpc    = "{CREATE_SANDBOX_RPC}"
phases = ["{PHASE_CONFIG_NAME}"]
"""
