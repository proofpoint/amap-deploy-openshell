"""Helper for the payload/amap-main tests.

It runs the shipped wrapper under /bin/sh against fakes, so no daemon, no
`claude`, no OpenShell and no router runs. The daemon's variable names are
parsed from the connector's source and the lane names come from the router;
neither list is restated here. Standard library only.
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import _workspace

REPO = Path(__file__).absolute().parents[1]
WRAPPER = REPO / "payload" / "amap-main"
DELIVERY_PREFIX = "AMAP_DELIVERY_"
# This deployment's own inputs, not the daemon's.
INPUT_LANE_VARS = ("AMAP_INBOX_DIR", "AMAP_PEER_DIR", "AMAP_OUTBOX_DIR")
SELF_INPUT = "AMAP_SELF_ADDRESS"
# 60 characters, so the seeder's 20-character suffix is a proper part of it.
FAKE_API_KEY = "fake-api-key" + "0123456789abcdef" * 3
SEED = REPO / "payload" / "claude-seed"


class Contract(NamedTuple):
    required: Tuple[str, ...]
    self_env: str
    receipt_env: str


def daemon_source() -> str:
    return (_workspace.CONNECTOR_ROOT / "bin" / "inbox-delivery").read_text(
        encoding="utf-8")


def parse_contract(source: str) -> Contract:
    """Reads ENV_KEYS, SELF_ENV and RECEIPT_WINDOW_ENV from the daemon's
    source. A name that is missing is an error, so a comparison against
    nothing cannot pass."""
    wanted = ("ENV_KEYS", "SELF_ENV", "RECEIPT_WINDOW_ENV")
    found: Dict[str, object] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[target.id] = ast.literal_eval(node.value)
    missing = [n for n in wanted if n not in found]
    if missing:
        raise AssertionError(
            "the connector's daemon source has no module-level assignment "
            "for: " + ", ".join(missing))
    return Contract(tuple(found["ENV_KEYS"]), str(found["SELF_ENV"]),
                    str(found["RECEIPT_WINDOW_ENV"]))


def connector_contract() -> Contract:
    return parse_contract(daemon_source())


_ROUTER_SNIPPET = """
import json
from router import config, outcomes
print(json.dumps({
    "lanes": list(config.LANES),
    "inbox": config.LANE_INBOX,
    "peer": config.LANE_PEER,
    "outbox": config.LANE_OUTBOX,
    "leaves": {k: list(v) for k, v in config.LANE_LEAVES.items()},
    "outcomes_rel": outcomes.OUTCOMES_REL.as_posix(),
}))
"""


def router_facts() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_workspace.ROUTER_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, "-c", _ROUTER_SNIPPET], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# --- fakes -------------------------------------------------------------------

FAKE_DAEMON = '''#!/usr/bin/env python3
import json, os, signal, sys, time

LOG = os.environ["FAKE_DAEMON_LOG"]
MODE = os.environ.get("FAKE_DAEMON_MODE", "run")


def w(event, **extra):
    rec = dict(event=event, pid=os.getpid(), t=time.time(), **extra)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\\n")


def on_term(signum, frame):
    w("term")
    sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
w("start", env={k: v for k, v in os.environ.items()
                if k.startswith("AMAP_DELIVERY_")})

if MODE == "exit":
    sys.exit(3)
if MODE == "run-then-exit":
    time.sleep(float(os.environ["FAKE_DAEMON_RUN_SECONDS"]))
    sys.exit(3)
if MODE == "keep-claims":
    for name in %(claim_names)r:
        path = os.environ[name]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{}")
while True:
    time.sleep(0.02)
'''

FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, signal, socket, subprocess, sys, time

if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.284 (Claude Code)")
    sys.exit(int(os.environ.get("FAKE_CLAUDE_VERSION_EXIT") or 0))

LOG = os.environ["FAKE_CLAUDE_LOG"]


def w(event, **extra):
    with open(LOG, "a") as f:
        f.write(json.dumps(dict(event=event, **extra)) + "\\n")


def on_term(signum, frame):
    w("term")
    sys.exit(7)


def claude_json():
    try:
        with open(os.path.join(os.environ["HOME"], ".claude.json")) as f:
            return f.read()
    except OSError:
        return None


def run_hooks():
    target = os.environ["FAKE_PAYLOAD_TARGET"] + "/"
    payload = os.environ["FAKE_PAYLOAD_DIR"] + "/"
    args = sys.argv[1:]
    settings = args[args.index("--settings") + 1].replace(target, payload)
    with open(settings) as f:
        doc = json.load(f)
    sock = os.path.join(os.environ["FAKE_SOCK_DIR"], "%d.sock" % os.getpid())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(1)
    peer = os.environ.get("FAKE_PEER_TOKEN")
    if peer:
        # Claude Code writes its own peer key; the fake stands in for it.
        keys = os.path.join(os.environ["HOME"], ".claude", "sessions")
        os.makedirs(keys, mode=0o700, exist_ok=True)
        kp = os.path.join(keys, "%d.s1.key" % os.getpid())
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as kf:
            json.dump({"peerToken": peer}, kf)
    for group in doc["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            env = dict(os.environ)
            env["CLAUDE_CODE_MESSAGING_SOCKET"] = sock
            env["CLAUDE_CODE_MESSAGING_TOKEN"] = os.environ["FAKE_MESSAGING_TOKEN"]
            p = subprocess.run(
                ["/bin/sh", "-c", hook["command"].replace(target, payload)],
                env=env, capture_output=True, text=True, timeout=30,
                input='{"hook_event_name": "SessionStart", "source": "startup"}')
            w("hook", rc=p.returncode, stdout=p.stdout, stderr=p.stderr,
              socket=sock)
    return srv


signal.signal(signal.SIGTERM, on_term)
data = sys.stdin.read() if os.environ.get("FAKE_CLAUDE_READ_STDIN") else ""
w("start", pid=os.getpid(), argv=sys.argv[1:], stdin=data,
  claude_json=claude_json())
srv = run_hooks() if os.environ.get("FAKE_CLAUDE_HOOKS") == "1" else None
release = os.environ["FAKE_CLAUDE_RELEASE"]
deadline = time.time() + 30
while not os.path.exists(release):
    if time.time() > deadline:
        w("timeout")
        sys.exit(99)
    time.sleep(0.02)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT") or 0))
'''

FAKE_SLEEP = '''#!/usr/bin/env python3
import os, sys, time

with open(os.environ["FAKE_SLEEP_LOG"], "a") as f:
    f.write(sys.argv[1] + "\\n")
time.sleep(float(sys.argv[1]) * float(os.environ.get("FAKE_SLEEP_SCALE") or "0.01"))
'''


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class Lab:
    def __init__(self, tmp: Path, *, edits: Optional[Dict[str, str]] = None):
        self.tmp = Path(tmp)
        self.payload = self.tmp / "payload"
        self.bin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.lanes = self.tmp / "lanes"
        self.procs: List[subprocess.Popen] = []
        self.payload.mkdir()
        self.bin.mkdir()
        self.home.mkdir()
        for lane in ("inbox", "peer", "outbox"):
            (self.lanes / lane).mkdir(parents=True)

        text = WRAPPER.read_bytes().decode("utf-8")
        if edits:
            lines = text.split("\n")
            for old, new in edits.items():
                hits = [i for i, ln in enumerate(lines) if ln == old]
                assert len(hits) == 1, (
                    "expected %r once as a whole line, found %d"
                    % (old, len(hits)))
                lines[hits[0]] = new
            text = "\n".join(lines)
        self.wrapper = self.payload / "amap-main"
        _write_exec(self.wrapper, text)
        _write_exec(self.payload / "claude-seed",
                    SEED.read_text(encoding="utf-8"))
        self.work = self.tmp / "work"
        self.work.mkdir()

        claims = [k for k in connector_contract().required
                  if k.endswith("_CLAIM")]
        assert claims, "the connector's contract names no claim variables"
        _write_exec(self.payload / "inbox-delivery",
                    FAKE_DAEMON % {"claim_names": tuple(claims)})
        _write_exec(self.bin / "claude", FAKE_CLAUDE)
        _write_exec(self.bin / "sleep", FAKE_SLEEP)

        self.daemon_log = self.tmp / "daemon.log"
        self.claude_log = self.tmp / "claude.log"
        self.sleep_log = self.tmp / "sleep.log"
        self.release_file = self.tmp / "release"
        self.stdout_path = self.tmp / "stdout"
        self.stderr_path = self.tmp / "stderr"

    def env(self, **overrides) -> Dict[str, str]:
        e = {
            "PATH": "%s%s%s" % (self.bin, os.pathsep, os.environ["PATH"]),
            "HOME": str(self.home),
            "ANTHROPIC_API_KEY": FAKE_API_KEY,
            "AMAP_INBOX_DIR": str(self.lanes / "inbox"),
            "AMAP_PEER_DIR": str(self.lanes / "peer"),
            "AMAP_OUTBOX_DIR": str(self.lanes / "outbox"),
            SELF_INPUT: "alpha@agents.example.org",
            "FAKE_DAEMON_LOG": str(self.daemon_log),
            "FAKE_CLAUDE_LOG": str(self.claude_log),
            "FAKE_SLEEP_LOG": str(self.sleep_log),
            "FAKE_CLAUDE_RELEASE": str(self.release_file),
        }
        for k, v in overrides.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def start(self, *args: str, env: Optional[Dict[str, str]] = None,
              stdin=subprocess.DEVNULL) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["/bin/sh", str(self.wrapper), *args],
            env=self.env() if env is None else env,
            stdin=stdin,
            cwd=str(self.work),
            stdout=open(self.stdout_path, "ab"),
            stderr=open(self.stderr_path, "ab"),
            start_new_session=True)
        self.procs.append(proc)
        return proc

    def stderr(self) -> str:
        try:
            return self.stderr_path.read_text(encoding="utf-8",
                                              errors="replace")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _events(path: Path) -> List[dict]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass  # a line still being written
        return out

    def daemon_events(self) -> List[dict]:
        return self._events(self.daemon_log)

    def claude_events(self) -> List[dict]:
        return self._events(self.claude_log)

    def sleeps(self) -> List[int]:
        try:
            lines = self.sleep_log.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return []
        return [int(x) for x in lines]

    def starts(self) -> List[dict]:
        return [e for e in self.daemon_events() if e["event"] == "start"]

    def release(self) -> None:
        self.release_file.touch()

    def wait_for(self, pred: Callable[[], bool], what: str,
                 timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError("timed out waiting for %s; stderr: %s"
                             % (what, self.stderr()))

    def finish(self, proc: subprocess.Popen, timeout: float = 30.0) -> int:
        self.release()
        return proc.wait(timeout)

    def run_once(self, *args: str, env: Optional[Dict[str, str]] = None,
                 input_text: Optional[str] = None) -> int:
        if input_text is None:
            proc = self.start(*args, env=env)
        else:
            proc = self.start(*args, env=env, stdin=subprocess.PIPE)
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        self.wait_for(lambda: len(self.starts()) >= 1, "a daemon start")
        self.wait_for(lambda: any(e["event"] == "start"
                                  for e in self.claude_events()),
                      "a claude start")
        return self.finish(proc)

    def close(self) -> None:
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()

    def group_alive(self, proc: subprocess.Popen) -> bool:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return False
        return True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wrapper_constant(name: str) -> int:
    m = re.search(r"^%s=(\d+)$" % re.escape(name),
                  WRAPPER.read_text(encoding="utf-8"), re.M)
    assert m, "the wrapper has no constant %s" % name
    return int(m.group(1))
