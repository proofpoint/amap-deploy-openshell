"""The interceptor over a real unix socket (docs/INTERCEPTOR.md tests 30-33).

Runs only in the interceptor virtualenv. The home is a short directory under
the system temp directory: a unix socket path holds at most 107 bytes.
"""

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import grpc
import pytest
from google.protobuf import json_format

REPO = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from interceptor import locks, server, wire  # noqa: E402
from test_interceptor_rule import (make_home, rendered_operation,  # noqa: E402
                                   section5_operation)

import gateway_interceptor_pb2 as pb  # noqa: E402
import gateway_interceptor_pb2_grpc as pb_grpc  # noqa: E402

import render  # noqa: E402

PROTO = REPO / "interceptor" / "proto"
GEN = REPO / "interceptor" / "_gen"


@pytest.fixture
def served():
    root = Path(tempfile.mkdtemp(prefix="ai-", dir="/tmp")).resolve()
    home = make_home(root)
    srv, path = server.serve(home)
    channel = grpc.insecure_channel("unix:" + path)
    try:
        yield home, path, pb_grpc.GatewayInterceptorStub(channel)
    finally:
        channel.close()
        srv.stop(0).wait()
        shutil.rmtree(root, ignore_errors=True)


def test_stubs_match_vendored_protos():
    _, _, digests = locks.read_source(str(PROTO / locks.SOURCE_NAME))
    want = [f"# sha256 {digests[f]}  {f}" for f in sorted(digests)]
    names = {f"{f[:-len('.proto')]}{suffix}.py" for f in digests
             for suffix in ("_pb2", "_pb2_grpc")}
    assert {p.name for p in GEN.glob("*.py")} == names
    for p in GEN.glob("*.py"):
        lines = p.read_text().splitlines()
        assert lines[0] == locks.GEN_MARK, p.name
        assert [ln for ln in lines if ln.startswith("# sha256 ")] == want, p.name


def _describe_request():
    return json_format.ParseDict({"gateway": {
        "protocol_version": {"major": 1, "minor": 0},
        "supported_capabilities": [wire.CONTRACT],
        "required_capabilities": [wire.CONTRACT]}}, pb.DescribeRequest())


def test_describe_over_unix_socket(served):
    _, _, stub = served
    got = stub.Describe(_describe_request())
    assert got == json_format.ParseDict(
        wire.manifest(wire.IMPLEMENTATION_VERSION), pb.InterceptorManifest())


def test_describe_refuses_a_missing_gateway(served):
    _, _, stub = served
    with pytest.raises(grpc.RpcError) as e:
        stub.Describe(pb.DescribeRequest())
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION


def _evaluation(operation):
    return json_format.ParseDict({
        "interceptor_name": wire.INTERCEPTOR_NAME,
        "binding_id": wire.BINDING_ID, "service": wire.SERVICE,
        "method": wire.METHOD,
        "validate": {"proposed_operation": operation}}, pb.InterceptorEvaluation())


def test_evaluate_refuses_another_members_outbox_over_unix_socket(served):
    home, _, stub = served
    got = stub.Evaluate(_evaluation(section5_operation(home)))
    assert got.allowed is False
    assert got.status_code == wire.STATUS_DENIED
    assert got.reason.startswith(wire.REASON_PREFIX)
    assert "instances/beta/" + render.LANE_OUTBOX in got.reason


def test_evaluate_allows_the_rendered_create_over_unix_socket(served):
    home, _, stub = served
    got = stub.Evaluate(_evaluation(rendered_operation(home, "alpha")))
    assert got.allowed is True


def test_socket_and_directory_modes(served):
    home, path, _ = served
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(os.path.dirname(path)).st_mode) == 0o700


def test_a_missing_home_is_created_as_install_would(tmp_path):
    """On a fresh host provision-guest.sh starts the server before install
    has made the home. The server makes it, 0755 like install, so the
    gateway waiting on the socket can start."""
    import stat
    home = str(tmp_path / "amap-home")
    path = server.prepare_run_dir(home)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_a_home_that_is_a_file_is_refused(tmp_path):
    home = tmp_path / "amap-home"
    home.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SystemExit):
        server.prepare_run_dir(str(home))
