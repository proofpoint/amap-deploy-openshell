"""The interceptor's wire form (docs/INTERCEPTOR.md section 10, tests 17-22).

Standard library only. Dicts here carry the proto field names, as `server.py`
converts them.
"""

import posixpath

import pytest

import gateway
import render
from interceptor import rule, wire
from test_interceptor_rule import (make_home, section5_operation,
                                   rendered_operation)


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path)


def evaluation(operation, **over):
    ev = {"interceptor_name": wire.INTERCEPTOR_NAME,
          "binding_id": wire.BINDING_ID, "service": wire.SERVICE,
          "method": wire.METHOD,
          "validate": {"proposed_operation": operation}}
    ev.update(over)
    return ev


def good_gateway():
    return {"protocol_version": {"major": 1, "minor": 0},
            "supported_capabilities": [wire.CONTRACT],
            "required_capabilities": [wire.CONTRACT]}


def test_the_manifest_binds_create_sandbox_validate_only():
    v = "some-version"
    assert wire.manifest(v) == {
        "name": "amap-openshell-mounts",
        "bindings": [{
            "id": "create-sandbox-mounts",
            "selector": {"rpc": "openshell.v1.OpenShell/CreateSandbox"},
            "phases": [3],
            "failure_policy": "fail_closed"}],
        "failure_policy": "fail_closed",
        "provider_profiles": False,
        "expected_audience": "",
        "extension": {
            "protocol_version": {"major": 1, "minor": 0},
            "implementation_name": "amap-openshell/interceptor",
            "implementation_version": v,
            "supported_capabilities": [
                "openshell.gateway-interceptor.contract"],
            "required_capabilities": [
                "openshell.gateway-interceptor.contract"]}}


def test_describe_refuses_an_incompatible_gateway():
    for gw, md in (
        (None, {}),
        ({**good_gateway(), "protocol_version": {"major": 2, "minor": 0}}, {}),
        ({**good_gateway(), "supported_capabilities": []}, {}),
        (good_gateway(), {"authorization": "Bearer x"}),
    ):
        with pytest.raises(wire.Refused):
            wire.describe(gw, md)
    assert wire.describe(good_gateway(), {}) == \
        wire.manifest(wire.IMPLEMENTATION_VERSION)


def test_describe_refuses_unmet_required_capabilities():
    gw = {**good_gateway(),
          "required_capabilities": [wire.CONTRACT, "other"]}
    with pytest.raises(wire.Refused):
        wire.describe(gw, {})


def test_evaluate_refuses_any_other_rpc_or_phase(home):
    op = rendered_operation(home, "alpha")
    cases = (
        evaluation(op, method="DeleteSandbox"),
        evaluation(op, service="openshell.v1.Other"),
        {k: v for k, v in evaluation(op).items() if k != "validate"}
        | {"modify_operation": {"proposed_operation": op}},
        {k: v for k, v in evaluation(op).items() if k != "validate"}
        | {"post_commit": {"committed_response": {}}},
        evaluation(op, binding_id="other"),
    )
    for ev in cases:
        assert wire.evaluate(ev, home)["allowed"] is False
    assert wire.evaluate(evaluation(op), home)["allowed"] is True


def test_a_denial_result(home):
    result = wire.evaluate(evaluation(section5_operation(home)), home)
    assert result["allowed"] is False
    assert result["status_code"] == "PERMISSION_DENIED"
    assert result["reason"].startswith(wire.REASON_PREFIX)
    assert result.get("patches", []) == []
    assert set(result["log_annotations"]) == {"amap.rule", "amap.decision"}


def test_an_internal_error_is_a_denial(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(rule, "evaluate_create", boom)
    result = wire.evaluate(evaluation(rendered_operation(home, "alpha")), home)
    assert result["allowed"] is False
    assert result["reason"].startswith(wire.REASON_PREFIX)


def test_the_fragment_matches_the_manifest(home):
    lines = gateway.setting_lines(wire.gateway_fragment(home))
    got = dict(ln.split(" = ", 1) for ln in
               (gateway._normal(l) for l in lines) if " = " in ln)
    binding = wire.manifest("v")["bindings"][0]
    assert got["rpc"] == f'"{binding["selector"]["rpc"]}"'
    assert got["phases"] == '["validate"]'
    assert binding["phases"] == [wire.PHASE_VALIDATE]
    assert got["binding_policy"] == '"exact"'
    assert got["failure_policy"] == '"fail_closed"'
    # D24: the gateway signs extension calls by default (seen live on the test host), and
    # this interceptor refuses a signed Describe, so the registration opts out.
    assert got["allow_insecure_transport"] == "true"
    endpoint = "unix://" + wire.socket_path(home)
    assert got["grpc_endpoint"] == f'"{endpoint}"'
    sock = wire.socket_path(home)
    for d in (render.payload_dir(home), render.roster_dir(home),
              render.instances_dir(home)):
        assert not sock.startswith(d + posixpath.sep)


def test_the_socket_path_refuses_a_bad_home():
    with pytest.raises(ValueError):
        wire.socket_path("/")
