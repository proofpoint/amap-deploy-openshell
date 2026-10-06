"""Staging helpers for the session lister tests.

A fake /proc is a directory tree with a `stat` and a `cmdline` file per pid.
The lister under test is a copy of payload/openshell-sessions whose root
constant (PROC) is rewritten to a staged path, so nothing else in the file
differs from the shipped one. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import importlib.machinery
import importlib.util
import os
import socket as _socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import _amap_main
import _workspace

REPO = _amap_main.REPO
LISTER = REPO / "payload" / "openshell-sessions"
ROOT_LINES = ('PROC = "/proc"',)
# Staged pids sit above Linux's largest pid_max (4194304), so they never
# collide with a real parent pid.
BASE = 4200000
MAIN_PID = BASE + 10
SUBSHELL_PID = BASE + 11
TOKEN = "a" * 32

MAIN_ARGV = ["/bin/sh", "/opt/amap/payload/amap-main", "claude",
             "--mcp-config", "/opt/amap/payload/mcp-servers.json"]


@contextlib.contextmanager
def short_tmp() -> Iterator[Path]:
    """A short directory under /tmp: an AF_UNIX path has a length limit."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ocs-") as d:
        yield Path(d)


class FakeSandbox:
    def __init__(self, root: Path, sock_dir: Path, daemon_pid: int):
        self.root = Path(root)
        self.sock_dir = Path(sock_dir)
        self.proc = self.root / "proc"
        self.home = self.root / "home"
        self.records = self.home / ".claude" / "amap-sessions"
        self.keys = self.home / ".claude" / "sessions"
        self.daemon_pid = daemon_pid
        self._sockets: List[_socket.socket] = []
        self.proc.mkdir(parents=True)
        self.sock_dir.mkdir(parents=True, exist_ok=True)
        self.add(1, 0, "openshell-sandb", ["/opt/openshell/bin/openshell-sandbox"])
        self.add(MAIN_PID, 1, "amap-main", MAIN_ARGV)
        self.add(SUBSHELL_PID, MAIN_PID, "amap-main", MAIN_ARGV)
        self.add(daemon_pid, SUBSHELL_PID, "python3",
                 ["python3", "/opt/amap/payload/inbox-delivery"])

    def use_home(self, home: Path) -> None:
        self.home = Path(home)
        self.records = self.home / ".claude" / "amap-sessions"
        self.keys = self.home / ".claude" / "sessions"

    def add(self, pid: int, ppid: int, comm: str, argv,
            start_time=None) -> None:
        """Stages a process. `stat` holds a full line, so that the 20th field
        after the comm (proc(5) field 22) is the start time."""
        st = 1000 + (pid - BASE) if start_time is None else start_time
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194304 0 0 0 0 0 0 0 "
            f"0 20 0 1 0 {st} 0 0\n", encoding="utf-8")
        (d / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))

    def start_time_of(self, pid: int) -> str:
        fields = (self.proc / str(pid) / "stat").read_text(
            encoding="utf-8").rsplit(")", 1)[1].split()
        return fields[19]

    def add_claude(self, pid: int, ppid: int = MAIN_PID, *, comm: str = "claude",
                   argv=("claude",), socket: bool = True, record: bool = True,
                   record_pid=None, record_start=None,
                   record_text: Optional[str] = None, peer_key: bool = True,
                   peer_token: str = TOKEN, key_id: str = "k1",
                   peer_key_text: Optional[str] = None) -> Tuple[str, str]:
        """Stages a `claude` process, its socket, its record and its peer key
        (the file Claude Code writes, `<pid>.<id>.key`). Returns the socket path
        and the peer key path, each `-` when it was not made."""
        self.add(pid, ppid, comm, list(argv))
        sock_path = self.sock_dir / f"{pid}.sock"
        if socket:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.bind(str(sock_path))
            self._sockets.append(s)
        if record:
            if record_text is None:
                record_text = json.dumps({
                    "socket": str(sock_path),
                    "pid": pid if record_pid is None else record_pid,
                    "start_time": (self.start_time_of(pid)
                                   if record_start is None else record_start)})
            self.records.mkdir(parents=True, exist_ok=True)
            rec_path = self.records / f"{pid}.json"
            rec_path.write_text(record_text, encoding="utf-8")
            rec_path.chmod(0o600)
        key_path = "-"
        if peer_key:
            text = (json.dumps({"peerToken": peer_token})
                    if peer_key_text is None else peer_key_text)
            key_path = self.add_peer_key(pid, key_id, text)
        return (str(sock_path) if socket else "-", key_path)

    def add_peer_key(self, pid: int, key_id: str, text: str) -> str:
        """Stages `<pid>.<key_id>.key` under the sessions directory."""
        self.keys.mkdir(parents=True, exist_ok=True)
        path = self.keys / f"{pid}.{key_id}.key"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return str(path)

    def close(self) -> None:
        for s in self._sockets:
            s.close()
        self._sockets = []


def rewritten_lister(dest: Path, proc: Path) -> Path:
    lines = LISTER.read_text(encoding="utf-8").split("\n")
    new = {ROOT_LINES[0]: f'PROC = "{proc}"'}
    for old, repl in new.items():
        hits = [i for i, ln in enumerate(lines) if ln == old]
        assert len(hits) == 1, (
            f"expected {old!r} once as a whole line, found {len(hits)}")
        lines[hits[0]] = repl
    dest = Path(dest)
    dest.write_text("\n".join(lines), encoding="utf-8")
    dest.chmod(0o755)
    return dest


def copy_payload(dest_dir: Path, *names: str) -> None:
    """Copies payload files, byte for byte and with their modes."""
    for name in names:
        src = REPO / "payload" / name
        dst = Path(dest_dir) / name
        shutil.copyfile(src, dst)
        dst.chmod(src.stat().st_mode & 0o777)


def run_lister(fake: FakeSandbox, tmp: Path, *, home=None,
               path_prefix=None) -> subprocess.CompletedProcess:
    """Run a rewritten copy of the lister. Its parent is this process, so the
    fake's daemon pid must be os.getpid()."""
    copy = rewritten_lister(Path(tmp) / "openshell-sessions", fake.proc)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("HOME", None)
    h = fake.home if home is None else home
    if h is not None:
        env["HOME"] = str(h)
    if path_prefix is not None:
        env["PATH"] = str(path_prefix) + os.pathsep + os.environ["PATH"]
    return subprocess.run([sys.executable, str(copy)], env=env, text=True,
                          capture_output=True, timeout=30)


def parse_rows(stdout: str) -> List[List[str]]:
    return [ln.split("\t") for ln in stdout.splitlines()]


_CONNECTOR = None


def connector_daemon():
    """The connector's inbox-delivery, loaded as a module and never run."""
    global _CONNECTOR
    if _CONNECTOR is None:
        path = str(_workspace.CONNECTOR_ROOT / "bin" / "inbox-delivery")
        loader = importlib.machinery.SourceFileLoader(
            "connector_inbox_delivery", path)
        spec = importlib.util.spec_from_loader("connector_inbox_delivery", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _CONNECTOR = mod
    return _CONNECTOR


LISTING_DAEMON = '''#!/usr/bin/env python3
import importlib.machinery, importlib.util, json, os, signal, subprocess, sys, time

sys.dont_write_bytecode = True
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

path = os.environ["FAKE_CONNECTOR_DAEMON"]
loader = importlib.machinery.SourceFileLoader("connector_inbox_delivery", path)
spec = importlib.util.spec_from_loader("connector_inbox_delivery", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

source = os.environ["AMAP_DELIVERY_SESSION_SOURCE"]
log = os.environ["FAKE_LISTING_LOG"]
while True:
    p = subprocess.run([source], capture_output=True, text=True)
    found = mod.find_claude_targets(source)
    targets = None if found is None else [[t.socket_path, t.key_file] for t in found]
    with open(log, "a") as f:
        f.write(json.dumps({"rc": p.returncode, "stdout": p.stdout,
                            "targets": targets}) + "\\n")
    time.sleep(0.1)
'''
