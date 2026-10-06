"""The gRPC server for the mounts interceptor (docs/INTERCEPTOR.md section 9).

Run it before the gateway, in a virtualenv built from requirements.txt:

    python3 interceptor/server.py --home "$AMAP_OPENSHELL_HOME"

This is the only module outside `_gen/` that imports grpc or protobuf (D5). It
converts messages to dicts, calls `wire`, and converts the result back. The
decision is in `rule.py`.

The boundary is a `unix://` socket (mode 0600) in `$AMAP_OPENSHELL_HOME/run`
(mode 0700), outside every mount source. The gateway does not sign its calls to
this interceptor, and `wire.describe` refuses a Describe that carries
authorization metadata (docs/INTERCEPTOR.md section 2).

OpenShell facts, cited at OpenShell v0.1.2: unix:// endpoints are supported
(crates/openshell-core/src/config.rs:410-412); a Describe refusal stops the
gateway (crates/openshell-gateway-interceptors/src/plan.rs:218-243);
`SnapshotProviderProfiles` is called only when the manifest sets
`provider_profiles` (proto/gateway_interceptor.proto:124-130), which it does
not.
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), os.path.join(_HERE, "_gen")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import grpc  # noqa: E402
from google.protobuf import json_format  # noqa: E402

import gateway_interceptor_pb2 as pb  # noqa: E402
import gateway_interceptor_pb2_grpc as pb_grpc  # noqa: E402

import render  # noqa: E402
from interceptor import wire  # noqa: E402

HOME_VARIABLE = "AMAP_OPENSHELL_HOME"
MAX_SOCKET_PATH = wire.MAX_SOCKET_PATH
EXIT_USAGE = 2


def _to_dict(message) -> dict:
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


class Servicer(pb_grpc.GatewayInterceptorServicer):
    def __init__(self, home: str) -> None:
        self.home = home

    def Describe(self, request, context):
        gateway = _to_dict(request.gateway) if request.HasField("gateway") \
            else None
        metadata = {k.lower(): v for k, v in context.invocation_metadata()}
        try:
            manifest = wire.describe(gateway, metadata)
        except wire.Refused as e:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(e))
        return json_format.ParseDict(manifest, pb.InterceptorManifest())

    def Evaluate(self, request, context):
        try:
            result = wire.evaluate(_to_dict(request), self.home)
        except Exception:
            result = wire.denial(["internal error"])
        verdict = "allow" if result.get("allowed") else \
            "deny: " + str(result.get("reason", ""))
        print(f"evaluate {verdict}", file=sys.stderr, flush=True)
        return json_format.ParseDict(result, pb.InterceptorResult())

    def SnapshotProviderProfiles(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED,
                      "this interceptor serves no provider profiles")


def prepare_run_dir(home: str) -> str:
    """Makes `home/run` (0700) and clears a stale socket. Returns the socket's
    path. Refuses a run directory or socket path that is not what it expects."""
    path = wire.socket_path(home)
    run = os.path.dirname(path)
    # On a fresh host provision-guest.sh starts this service before bring-up's
    # install has made the home. Make it as install does (a plain 0755
    # directory) so the service, and the gateway that waits on it, can start.
    if not os.path.lexists(home):
        if not os.path.isdir(os.path.dirname(home)):
            raise SystemExit(f"cannot create {home}: its parent is not a "
                             f"directory")
        os.mkdir(home, 0o755)
    elif os.path.islink(home) or not os.path.isdir(home):
        raise SystemExit(f"{home} must be a real directory")
    if os.path.lexists(run):
        if os.path.islink(run) or not os.path.isdir(run):
            raise SystemExit(f"{run} must be a real directory")
    else:
        os.mkdir(run, 0o700)
    os.chmod(run, 0o700)
    if len(path.encode()) > MAX_SOCKET_PATH:
        raise SystemExit(f"the socket path is {len(path.encode())} bytes; a "
                         f"unix socket path holds at most {MAX_SOCKET_PATH}. "
                         f"Use a shorter {HOME_VARIABLE}")
    if os.path.lexists(path):
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
        else:
            raise SystemExit(f"{path} exists and is not a socket")
    return path


def serve(home: str, max_workers: int = 4) -> Tuple["grpc.Server", str]:
    """Starts the server on the unix socket under `home`. The socket is created
    under a umask that leaves it 0600."""
    path = prepare_run_dir(home)
    old = os.umask(0o177)
    try:
        server = grpc.server(ThreadPoolExecutor(max_workers))
        pb_grpc.add_GatewayInterceptorServicer_to_server(Servicer(home), server)
        server.add_insecure_port("unix:" + path)
        server.start()
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return server, path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.py", description="The mounts interceptor's gRPC server.")
    parser.add_argument("--home", default=os.environ.get(HOME_VARIABLE),
                        help=f"the deployment's home (default: ${HOME_VARIABLE})")
    args = parser.parse_args(argv)
    home = args.home
    reason = render.home_problem(home)
    if reason:
        print(f"server.py: {reason}", file=sys.stderr)
        return EXIT_USAGE
    real = os.path.realpath(home)
    if real != home:
        print(f"server.py: home {home!r} is not its own real path: it "
              f"resolves to {real!r}. Use that (D19)", file=sys.stderr)
        return EXIT_USAGE
    server, path = serve(home)
    print(f"serving on unix:{path}", file=sys.stderr, flush=True)

    def stop(signum, frame):
        server.stop(1)
        try:
            os.unlink(path)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait_for_termination()
    return 0


if __name__ == "__main__":
    sys.exit(main())
