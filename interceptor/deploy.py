"""Deploys the mounts interceptor on a host: the systemd unit, the gateway.toml
fragment and the wait for its socket (docs/INTERCEPTOR.md).

`examples/vms/proxmox/provision-guest.sh --home DIR` calls this module, so the
script never retypes TOML or unit text. Commands:

    deploy.py fragment --home DIR           print the gateway.toml block
    deploy.py unit --home DIR --venv DIR    print the systemd user unit
    deploy.py wait --home DIR [--seconds N] exit 0 once the socket accepts

OpenShell facts, cited at OpenShell v0.1.2, read and not run: the gateway is a
systemd user unit, `openshell-gateway.service`, with `After=default.target` and
`WantedBy=default.target` [docs: deploy/deb/openshell-gateway.service:4-23;
deploy/man/openshell-gateway.8.md:146-160]. Our own unit orders itself ahead of
it with `Before=` and is pulled in by `WantedBy=openshell-gateway.service`, so
OpenShell's unit file is never edited. A Describe refusal stops the gateway
[docs: crates/openshell-gateway-interceptors/src/plan.rs:218-243], and an
interceptor that is down turns every create into PERMISSION_DENIED under
fail_closed [docs: crates/openshell-gateway-interceptors/src/runtime.rs:373-404].

Standard library plus this repository's modules. It never imports grpc,
protobuf or `server` (D5), so the main suite can test it.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
import time
from typing import Callable, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import l1_kit  # noqa: E402
import policy  # noqa: E402
import render  # noqa: E402
import router_link  # noqa: E402
from interceptor import wire  # noqa: E402

UNIT_NAME = "amap-openshell-interceptor.service"
GATEWAY_UNIT = "openshell-gateway.service"
CONNECT_TIMEOUT = 2.0
WAIT_SECONDS = 10.0
# systemd specifiers, expansion and quoting.
UNSAFE_IN_UNIT = "%$\\\"';"
EXIT_USAGE = 2


class DeployError(ValueError):
    pass


def home_problem(home: str) -> Optional[str]:
    """Why `home` cannot be the deployment's home, or None. The checks are the
    ones `install` and `server.py` make, so a refusal comes before anything is
    installed."""
    reason = render.home_problem(home)
    if reason:
        return reason
    try:
        l1_kit.home_checks(home)
    except l1_kit.KitError as e:
        return str(e)
    real = os.path.realpath(home)
    if real != home:
        return (f"home {home!r} is not its own real path: it resolves to "
                f"{real!r}. Use that (D19)")
    try:
        l1_kit.require_home_directory(home, must_exist=False)
    except l1_kit.KitError as e:
        return str(e)
    path = wire.socket_path(home)
    if len(path.encode()) > wire.MAX_SOCKET_PATH:
        return (f"the socket path is {len(path.encode())} bytes; a unix socket "
                f"path holds at most {wire.MAX_SOCKET_PATH}. Use a shorter home")
    return None


def unit_path_problem(label: str, path: str) -> Optional[str]:
    reason = render.host_path_problem(label, path)
    if reason:
        return reason
    if any(c in UNSAFE_IN_UNIT for c in path):
        return (f"{label} {path!r} must not contain any of "
                f"{UNSAFE_IN_UNIT!r}: systemd would read it as a specifier, "
                f"an expansion or quoting")
    return None


def unit_text(repo: str, home: str, venv: str, router: str, sandy: str) -> str:
    for label, path in (("repo", repo), ("home", home), ("venv", venv),
                        ("router checkout", router), ("sandy checkout", sandy)):
        reason = unit_path_problem(label, path)
        if reason:
            raise DeployError(reason)
    py = f"{venv}/bin/python"
    return f"""\
# Written by examples/vms/proxmox/provision-guest.sh (interceptor/deploy.py).
# The amap-openshell mounts interceptor (docs/INTERCEPTOR.md). The gateway
# registers it fail_closed and does not start without it, so it starts first:
# Before= orders it ahead of the gateway, and WantedBy= pulls it in whenever
# the gateway starts. OpenShell's own unit is not edited. ExecStartPost holds
# the gateway back until the socket accepts connections.
[Unit]
Description=amap-openshell mounts interceptor
Before={GATEWAY_UNIT}

[Service]
Type=simple
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=AMAP_ROUTER_REPO={router}
Environment=AMAP_SANDY_REPO={sandy}
ExecStart={py} {repo}/interceptor/server.py --home {home}
ExecStartPost={py} {repo}/interceptor/deploy.py wait --home {home}
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=default.target {GATEWAY_UNIT}
"""


def unit_for(home: str, venv: str) -> str:
    reason = home_problem(home)
    if reason:
        raise DeployError(reason)
    return unit_text(str(l1_kit.REPO), home, venv,
                     str(router_link.find_router()), str(policy.find_sandy()))


def listener_problem(home: str, timeout: float = CONNECT_TIMEOUT) -> str:
    """'' when something accepts a connection on the interceptor's socket,
    otherwise why not."""
    try:
        path = wire.socket_path(home)
    except ValueError as e:
        return str(e)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} is absent"
    except OSError as e:
        return f"cannot inspect {path}: {e}"
    if not stat.S_ISSOCK(st.st_mode):
        return f"{path} is not a socket"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
    except OSError as e:
        return f"nothing accepts connections on {path}: {e.strerror or e}"
    finally:
        s.close()
    return ""


def wait_for_listener(home: str, seconds: float = WAIT_SECONDS,
                      interval: float = 0.1,
                      clock: Callable[[], float] = time.monotonic,
                      sleep: Callable[[float], None] = time.sleep) -> str:
    """Polls `listener_problem` until it is empty or `seconds` pass. Returns
    the last problem."""
    deadline = clock() + seconds
    while True:
        problem = listener_problem(home)
        if not problem or clock() >= deadline:
            return problem
        sleep(interval)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="Deploys the mounts interceptor (docs/INTERCEPTOR.md).")
    sub = parser.add_subparsers(dest="command", required=True)
    frag = sub.add_parser("fragment", help="print the gateway.toml block")
    frag.add_argument("--home", required=True)
    unit = sub.add_parser("unit", help="print the systemd user unit")
    unit.add_argument("--home", required=True)
    unit.add_argument("--venv", required=True)
    wait = sub.add_parser("wait", help="exit 0 once the socket accepts")
    wait.add_argument("--home", required=True)
    wait.add_argument("--seconds", type=float, default=WAIT_SECONDS)
    args = parser.parse_args(argv)
    try:
        if args.command == "fragment":
            reason = home_problem(args.home)
            if reason:
                raise DeployError(reason)
            print(wire.gateway_fragment(args.home), end="")
        elif args.command == "unit":
            print(unit_for(args.home, args.venv))
        else:
            problem = wait_for_listener(args.home, args.seconds)
            if problem:
                print(f"deploy.py: {problem}", file=sys.stderr)
                return 1
    except (DeployError, l1_kit.KitError, policy.PolicyError,
            policy.SandyNotFound, router_link.RouterNotFound, ValueError,
            OSError) as e:
        print(f"deploy.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
