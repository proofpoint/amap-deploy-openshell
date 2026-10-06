"""Fakes for the L1 runner's tests: `openshell`, `docker`, `ss` and
`openshell-gateway`, and the one JSON "world" they share.

Nothing here runs OpenShell, Docker or the router. Each fake is a small script
that calls `fake_main`, which reads and updates the world and records its call
in `calls.jsonl`. The world simulates what the real programs' side effects would
be as far as the runner can see them:

- `sandbox create` records a sandbox with an ID, its policy text and its mounts,
  and prints the provider line that carries the API key from the environment;
  the session probe prints a ready `claude` row after `ready_polls` empty
  answers, and its stderr carries a messaging token in two forms (so the
  runner's redaction has something to redact);
- `docker run` of the image (the probe of `node`'s and `claude`'s real paths)
  prints the paths from the `image_paths` flag, and refuses before the build;
- the `provider` commands keep the gateway's profile catalog: `profile lint`
  checks a file, `list-profiles -o json` prints the catalog, `profile import`
  is create-only and `profile update` needs the stored `resource_version`;
  `sandbox create --provider X --auto-providers` needs an imported profile named
  X and a key in the environment, as OpenShell's ensure_required_providers does;
- `docker run` for the router marks it running and writes the roster. With
  `router_crash` the started router falls into that docker state after
  `router_crash_polls` more `docker ps` answers of `running`, never publishes a
  roster, and reports `router_exit_code` and a log that ends in
  `router_crash_log` on stderr. With `roster_after_polls` the roster appears
  only on that many router `docker ps` calls that find it missing;
- `inbox-submit submit` writes a request into the sender's drop-box. When the
  router runs it also does the router's work: it places a notice and a message
  in the recipient's peer lane, writes the router's audit lines (built with the
  router's own `router.audit.build_line`), and answers with a result. A task on
  the fleet's edge is `delivered` (or `refused` when the receiver's setting is
  active), and the reply appears in the sender's peer lane. A task with no edge
  is `queued_for_human`, and the router keeps a held copy. With `silent_refuse`
  a receiver that refuses answers nothing and the outcome is still `delivered`.

The documents are copies of amap-spec's valid fixtures with only their id
fields changed, so they pass the validator. Standard library only, and it
imports nothing from the repository.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

FAKE_KEY = "fake-KEY-not-a-secret-0123456789abcdefghijKLMNOP"
FAKE_TOKEN = "fake-token-not-a-secret-9f8e7d6c5b4a"
ENV_VAR = "FAKE_L1_WORLD"
PROGRAMS = ("openshell", "docker", "ss", "openshell-gateway")
GATEWAY_TOML = """\
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
"""
DEFAULT_FLAGS: Dict[str, Any] = {
    "ready_polls": 1,
    "reply": True,
    "errno": "EROFS",
    "payload_errno": "EROFS",
    "network_reachable": False,
    "fail_create": False,
    "fail_build": False,
    "fail_router_once": False,
    "listen": None,
    "extra_sandboxes": [],
    "silent_refuse": False,   # a receiver that says nothing counts as delivered
    "image_paths": {"node": "/usr/local/bin/node",
                    "claude": "/usr/local/lib/node_modules/@anthropic-ai/"
                              "claude-code/cli.js"},
    "fail_lint": False,
    "interceptor_refuses_probe": False,
    # a router container that exists in this docker state
    "router_container_state": "",
    "router_crash": "",          # the docker state a started router falls into
    "router_crash_polls": 1,     # router `docker ps` calls after the start that still say running
    "router_exit_code": 0,
    "router_crash_log": "",
    "roster_after_polls": 0,     # >0: the roster appears on the Nth router `docker ps` that finds it missing
}
STUB = """#!{python}
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {tests!r})
import _l1_world
sys.exit(_l1_world.fake_main({prog!r}, sys.argv[1:]))
"""
TESTS_DIR = Path(__file__).absolute().parent


class World:
    """One temporary world: the fakes, the shared JSON, and the environment a
    runner needs."""

    def __init__(self, tmp: Path, fake_bin: Path, **flags: Any) -> None:
        import pytest
        if os.getuid() == 0 or os.getgid() == 0:
            pytest.fail("the L1 runner refuses root (D7): run the tests as an "
                        "ordinary uid:gid, not as root")
        import _workspace
        sys.dont_write_bytecode = True
        from router import config as rc
        self.tmp = Path(tmp)
        self.fake_bin = Path(fake_bin)
        self.home = self.tmp / "home"
        self.xdg = self.tmp / "xdg"
        self.path = self.tmp / "world.json"
        self.state_path = self.tmp / "state.json"
        self.calls_path = self.tmp / "calls.jsonl"
        unknown = set(flags) - set(DEFAULT_FLAGS)
        assert not unknown, f"unknown world flag(s): {sorted(unknown)}"
        (self.xdg / "openshell").mkdir(parents=True)
        self.gateway_toml = self.xdg / "openshell" / "gateway.toml"
        self.gateway_toml.write_text(GATEWAY_TOML, encoding="utf-8")
        cfg = {
            "home": str(self.home),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "workspace": "default",
            "fixtures": str(Path(_workspace.SPEC_DIR) / "fixtures" / "valid"),
            "router_root": str(_workspace.ROUTER_ROOT),
            "key": FAKE_KEY,
            "token": FAKE_TOKEN,
            "calls": str(self.calls_path),
            "edges": [["alpha", "beta"]],
            "lane_inbox": rc.LANE_INBOX,
            "lane_peer": rc.LANE_PEER,
            "lane_outbox": rc.LANE_OUTBOX,
            "flags": {**DEFAULT_FLAGS, **flags},
        }
        self.path.write_text(json.dumps(cfg), encoding="utf-8")
        self.state_path.write_text(json.dumps({
            "counter": 0, "req": 0, "notice": 0, "sandboxes": {}, "labels": None,
            "router": False, "router_failed": False,
            "router_ps": 0, "router_config": None, "probed": False,
            "deleted": [], "builds": [], "profiles": {}, "providers": []}),
            encoding="utf-8")
        for prog in PROGRAMS:
            (self.fake_bin / prog).write_text(STUB.format(
                python=sys.executable, tests=str(TESTS_DIR), prog=prog),
                encoding="utf-8")
            os.chmod(self.fake_bin / prog, 0o755)

    def env(self) -> Dict[str, str]:
        import _workspace
        env = dict(os.environ)
        env.update({
            "AMAP_OPENSHELL_HOME": str(self.home),
            "AMAP_ROUTER_REPO": str(_workspace.ROUTER_ROOT),
            "AMAP_CONNECTOR_REPO": str(_workspace.CONNECTOR_ROOT),
            "AMAP_SPEC_DIR": str(_workspace.SPEC_DIR),
            "CLAUDE_CODE_VERSION": "2.1.284",
            "ANTHROPIC_API_KEY": FAKE_KEY,
            "XDG_CONFIG_HOME": str(self.xdg),
            ENV_VAR: str(self.path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        })
        return env

    def calls(self) -> List[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in
                self.calls_path.read_text(encoding="utf-8").splitlines() if line]

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def put_profile(self, doc: dict, resource_version: int) -> None:
        """Put a copy of a profile into the gateway's catalog."""
        state = self.state()
        state["profiles"][doc["id"]] = {**doc, "resource_version":
                                        resource_version, "source": "custom",
                                        "scope": "workspace"}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def set_flag(self, **kv: Any) -> None:
        cfg = json.loads(self.path.read_text(encoding="utf-8"))
        unknown = set(kv) - set(DEFAULT_FLAGS)
        assert not unknown, f"unknown world flag(s): {sorted(unknown)}"
        cfg["flags"].update(kv)
        self.path.write_text(json.dumps(cfg), encoding="utf-8")


# --- the fakes ---------------------------------------------------------------

class Sim:
    def __init__(self) -> None:
        self.path = Path(os.environ[ENV_VAR])
        self.cfg = json.loads(self.path.read_text(encoding="utf-8"))
        self.state_path = self.path.parent / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.flags = self.cfg["flags"]
        self.home = Path(self.cfg["home"])
        self.out: List[str] = []
        self.err: List[str] = []

    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.state_path)

    def sandbox(self, name: str) -> Optional[dict]:
        return self.state["sandboxes"].get(name)

    def emit(self, code: int = 0) -> int:
        sys.stdout.write("".join(self.out))
        sys.stderr.write("".join(self.err))
        return code

    def say(self, text: str = "") -> None:
        self.out.append(text + "\n")

    def complain(self, text: str) -> None:
        self.err.append(text + "\n")

    def fixture(self, name: str) -> dict:
        with open(Path(self.cfg["fixtures"]) / f"{name}.json",
                  encoding="utf-8") as fh:
            return json.load(fh)

    def write_json(self, path: Path, doc: Any, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")

    def next_notice_id(self) -> str:
        self.state["notice"] += 1
        return "%032x" % (0xa11ce0000 + self.state["notice"])

    def lane(self, member: str, lane: str, *rest: str) -> Path:
        return self.home / "instances" / member / lane / Path(*rest) \
            if rest else self.home / "instances" / member / lane

    def audit(self, instance: str, event: str, **fields: Any) -> None:
        sys.dont_write_bytecode = True
        root = self.cfg["router_root"]
        if root not in sys.path:
            sys.path.insert(0, root)
        from router import audit
        path = self.home / "router-state" / instance / "audit" / "log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as fh:
            fh.write(audit.build_line(event, instance, fields))


def fake_main(prog: str, argv: List[str]) -> int:
    sim = Sim()
    with open(sim.cfg["calls"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"prog": prog, "argv": list(argv),
                             "key": bool(os.environ.get("ANTHROPIC_API_KEY"))})
                 + "\n")
    handler = {"openshell": _openshell, "docker": _docker, "ss": _ss,
               "openshell-gateway": _gateway}[prog]
    code = handler(sim, list(argv))
    sim.save()
    return sim.emit(code)


def _gateway(sim: Sim, argv: List[str]) -> int:
    return 0 if argv[:2] == ["config", "preflight"] else 2


def _ss(sim: Sim, argv: List[str]) -> int:
    listen = sim.flags.get("listen") or "127.0.0.1:17670"
    sim.say(f"LISTEN 0 4096 {listen} 0.0.0.0:*")
    return 0


def _flag_value(argv: List[str], flag: str) -> Optional[str]:
    for i, w in enumerate(argv):
        if w == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _flag_values(argv: List[str], flag: str) -> List[str]:
    return [argv[i + 1] for i, w in enumerate(argv)
            if w == flag and i + 1 < len(argv)]


# --- openshell ---------------------------------------------------------------

def _openshell(sim: Sim, argv: List[str]) -> int:
    if argv[:1] == ["--workspace"]:
        argv = argv[2:]
    if argv == ["--version"]:
        sim.say("openshell 0.1.2")
        return 0
    if argv == ["status"]:
        sim.say("Gateway: openshell (connected)")
        return 0
    if argv[:1] == ["logs"]:
        return _logs(sim, argv[1:])
    if argv[:1] == ["provider"]:
        return _provider(sim, argv[1:])
    if argv[:1] != ["sandbox"]:
        sim.complain("fake openshell: unhandled command")
        return 2
    verb, rest = argv[1], argv[2:]
    if verb == "list":
        for name in sorted(sim.state["sandboxes"]):
            sim.say(f"{sim.cfg['workspace']}/{name}")
        for name in sim.flags.get("extra_sandboxes") or []:
            sim.say(f"{sim.cfg['workspace']}/{name}")
        return 0
    if verb == "get":
        sb = sim.sandbox(rest[0])
        if sb is None:
            sim.complain("status: NotFound, message: sandbox not found")
            return 1
        if "--policy-only" in rest:
            sim.out.append(sb["policy_text"])
        else:
            sim.say(json.dumps({"id": sb["id"], "name": sb["name"],
                                "workspace": sim.cfg["workspace"],
                                "phase": sb["phase"]}))
        return 0
    if verb == "create":
        return _create(sim, rest)
    if verb in ("stop", "start", "delete"):
        return _lifecycle(sim, verb, rest[0])
    if verb == "exec":
        return _exec(sim, rest)
    sim.complain("fake openshell: unhandled sandbox verb")
    return 2


def _load_profile_file(sim: Sim, path: Optional[str]) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, TypeError, ValueError) as e:
        sim.complain(f"cannot read the profile file: {e}")
        return None
    if not isinstance(doc, dict):
        sim.complain("the profile file is not an object")
        return None
    return doc


def _provider(sim: Sim, argv: List[str]) -> int:
    catalog = sim.state["profiles"]
    if argv == ["list-profiles", "-o", "json"]:
        sim.say(json.dumps(list(catalog.values())))
        return 0
    if argv[:1] != ["profile"] or len(argv) < 2:
        sim.complain("fake openshell: unhandled provider command")
        return 2
    verb, rest = argv[1], argv[2:]
    if verb == "lint":
        doc = _load_profile_file(sim, _flag_value(rest, "-f"))
        if doc is None:
            return 1
        pid = doc.get("id")
        ok = isinstance(pid, str) and bool(re.match(
            r"\A[a-z0-9]+(-[a-z0-9]+)*\Z", pid))
        ok = ok and isinstance(doc.get("display_name"), str) \
            and bool(doc["display_name"])
        ok = ok and all(isinstance(b, (str, dict)) and b
                        for b in doc.get("binaries", []))
        if isinstance(pid, str) and pid in catalog:
            # OpenShell 0.1.2's lint, seen on L2's host.
            sim.complain("Provider profile diagnostics:")
            sim.complain(f"error profile={pid} field=id custom provider "
                         f"profile '{pid}' already exists")
            return 1
        if sim.flags.get("fail_lint") or not ok:
            sim.complain("Provider profile diagnostics:")
            sim.complain("profile lint failed")
            return 1
        sim.say("Provider profile lint passed.")
        return 0
    if verb == "import":
        doc = _load_profile_file(sim, _flag_value(rest, "-f"))
        if doc is None:
            return 1
        if doc["id"] in catalog:
            sim.complain(f"provider profile '{doc['id']}' already exists")
            return 1
        catalog[doc["id"]] = {**doc, "resource_version": 1,
                              "source": "custom", "scope": "workspace"}
        sim.say(f"Imported provider profile {doc['id']}")
        return 0
    if verb == "update":
        doc = _load_profile_file(sim, _flag_value(rest, "-f"))
        if doc is None:
            return 1
        stored = catalog.get(rest[0] if rest else "")
        if stored is None or doc.get("id") != rest[0]:
            sim.complain("no such provider profile, or the ID differs")
            return 1
        if doc.get("resource_version") != stored["resource_version"]:
            sim.complain("custom provider profile update requires a "
                         "non-zero resource_version that matches")
            return 1
        version = stored["resource_version"] + 1
        catalog[doc["id"]] = {**doc, "resource_version": version,
                              "source": "custom", "scope": "workspace"}
        sim.say(f"Updated provider profile {doc['id']}")
        return 0
    sim.complain("fake openshell: unhandled provider profile command")
    return 2


def _create(sim: Sim, argv: List[str]) -> int:
    if "--" in argv:
        head, tail = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    else:
        head, tail = argv, []
    name = _flag_value(head, "--name")
    if sim.flags.get("fail_create"):
        sim.complain("fake openshell: create failed")
        return 1
    if name in sim.state["sandboxes"]:
        sim.complain(f"sandbox {name} already exists")
        return 1
    if sim.flags.get("interceptor_refuses_probe") and tail[:1] == ["sleep"]:
        # As on the test host with the mounts interceptor on (2026-10-05): wrapped, with
        # a bar before each continuation line.
        sim.complain("Error:   \u00d7 code: 'The caller does not have permission "
                     "to execute the specified\n  \u2502 operation', message: "
                     "\"amap-\n  \u2502 openshell interceptor: name "
                     f"'{name}' is not a member of the fleet\"")
        return 1
    provider = _flag_value(head, "--provider")
    if provider not in sim.state["providers"]:
        if provider not in sim.state["profiles"]:
            sim.complain(f"provider '{provider}' not found and no provider "
                         f"profile named '{provider}' is available")
            return 1
        if "--auto-providers" not in head \
                or not os.environ.get("ANTHROPIC_API_KEY"):
            sim.complain(f"provider '{provider}' cannot be created: no "
                         f"--auto-providers or no key in the environment")
            return 1
        sim.state["providers"].append(provider)
        sim.say(f"provider {provider} created from ANTHROPIC_API_KEY="
                f"{os.environ.get('ANTHROPIC_API_KEY', '')}")
    policy_path = _flag_value(head, "--policy")
    with open(policy_path, encoding="utf-8") as fh:
        policy_text = fh.read()
    config = json.loads(_flag_value(head, "--driver-config-json"))
    sim.state["counter"] += 1
    sim.state["sandboxes"][name] = {
        "id": f"id-{name}-{sim.state['counter']}", "name": name,
        "phase": "Ready", "ready_polls": int(sim.flags["ready_polls"]),
        "policy_text": policy_text, "mounts": config["docker"]["mounts"],
        "refuse_file": False, "refuse_active": False, "stops": 0, "starts": 0,
        "probe": tail[:1] == ["sleep"], "env": _flag_values(head, "--env")}
    sim.say(f"sandbox {name} is ready")
    return 0


def _lifecycle(sim: Sim, verb: str, name: str) -> int:
    sb = sim.sandbox(name)
    if sb is None:
        sim.complain("status: NotFound, message: sandbox not found")
        return 1
    if verb == "stop":
        sb["phase"], sb["stops"] = "Stopped", sb["stops"] + 1
    elif verb == "start":
        sb["phase"], sb["starts"] = "Ready", sb["starts"] + 1
        sb["refuse_active"] = sb["refuse_file"]
        sb["ready_polls"] = 1
    else:
        del sim.state["sandboxes"][name]
        sim.state["deleted"].append(name)
    return 0


def _logs(sim: Sim, argv: List[str]) -> int:
    sim.say("INFO sandbox supervisor started")
    if sim.state["probed"]:
        sim.say("WARN network policy denied CONNECT mail.example.org:443")
    return 0


# --- inside a sandbox --------------------------------------------------------

def _host_path(sb: dict, path: str) -> Optional[Path]:
    for m in sorted(sb["mounts"], key=lambda m: -len(m["target"])):
        if path == m["target"] or path.startswith(m["target"] + "/"):
            return Path(m["source"]) / path[len(m["target"]):].lstrip("/")
    return None


def _mount_of(sb: dict, path: str) -> Optional[dict]:
    for m in sorted(sb["mounts"], key=lambda m: -len(m["target"])):
        if path == m["target"] or path.startswith(m["target"] + "/"):
            return m
    return None


def _policy_paths(sb: dict) -> List[str]:
    return [line.strip()[2:].strip().strip('"')
            for line in sb["policy_text"].splitlines()
            if line.strip().startswith("- ")]


def _allowed(sb: dict, path: str) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/")
               for p in _policy_paths(sb))


def _exec(sim: Sim, argv: List[str]) -> int:
    name = _flag_value(argv, "--name")
    envs = _flag_values(argv, "--env")
    command = argv[argv.index("--") + 1:]
    sb = sim.sandbox(name)
    if sb is None:
        sim.complain("status: NotFound, message: sandbox not found")
        return 1
    if sb["phase"] != "Ready":
        sim.complain(f"sandbox {name} is not running")
        return 1
    if command[:2] == ["python3", "-c"]:
        marker = command[2].split("\n", 1)[0]
        prefix = "# amap-openshell l1-run: "
        assert marker.startswith(prefix), marker
        return _snippet(sim, sb, marker[len(prefix):], command[3:])
    if command[:1] == ["python3"] and command[1].endswith("inbox-submit"):
        return _inbox_submit(sim, sb, envs, command[2:])
    return _plain(sim, sb, command)


def _snippet(sim: Sim, sb: dict, kind: str, args: List[str]) -> int:
    cfg = sim.cfg
    if kind == "session-probe":
        sim.complain(f"CLAUDE_CODE_MESSAGING_TOKEN={cfg['token']}")
        sim.complain(json.dumps({"peerToken": cfg["token"]}))
        if sb["ready_polls"] > 0:
            sb["ready_polls"] -= 1
            return 0
        sim.say("\t".join(["claude", "-", "-", "4242",
                           f"/tmp/cc-socks-{cfg['uid']}/s.sock",
                           "/sandbox/.claude/sessions/4242.s1.key"]))
        return 0
    if kind == "write-probe":
        path = args[0]
        if path.startswith("/opt/amap/payload"):
            sim.say(sim.flags["payload_errno"] if sim.flags["payload_errno"]
                    else "OPENED")
            return 0
        mount = _mount_of(sb, path)
        if mount is not None and mount["read_only"]:
            sim.say(sim.flags["errno"] or "OPENED")
        else:
            sim.say("OPENED")
        return 0
    if kind == "network-probe":
        sim.state["probed"] = True
        if sim.flags["network_reachable"]:
            sim.say("REACHED")
            return 0
        sim.say("DENIED URLError")
        return 1
    if kind == "ids-probe":
        sim.say(f"{cfg['uid']}:{cfg['gid']}")
        return 0
    if kind == "refuse-state":
        sim.say("ours" if sb["refuse_file"] else "absent")
        return 0
    if kind == "write-refuse":
        if sb["refuse_file"]:
            sim.say("exists")
            return 1
        sb["refuse_file"] = True
        sim.say("written")
        return 0
    if kind == "remove-refuse":
        sim.say("removed" if sb["refuse_file"] else "absent")
        sb["refuse_file"] = False
        return 0
    if kind == "home-write-probe":
        sim.say("writable")
        return 0
    sim.complain("fake openshell: unhandled snippet " + kind)
    return 127


def _plain(sim: Sim, sb: dict, command: List[str]) -> int:
    cfg = sim.cfg
    prog, args = command[0], command[1:]
    if prog == "touch":
        mount = _mount_of(sb, args[-1])
        if mount is not None and mount["read_only"]:
            sim.complain(f"touch: cannot touch '{args[-1]}': "
                         f"Read-only file system")
            return 1
        return 0
    if prog == "ls":
        if args[:1] == ["-la"]:
            sim.say("total 0")
            sim.say("drwxr-xr-x 2 agent agent 40 Jan  1 00:00 .")
            return 0
        path = args[-1]
        host = _host_path(sb, path)
        if host is None or not host.is_dir():
            sim.complain(f"ls: cannot access '{path}': No such file or "
                         f"directory")
            return 2
        if not _allowed(sb, path):
            sim.complain(f"ls: cannot open directory '{path}': "
                         f"Permission denied")
            return 2
        for entry in sorted(os.listdir(host)):
            sim.say(entry)
        return 0
    if prog == "stat":
        host = _host_path(sb, args[-1])
        if host is None or not host.is_file():
            sim.complain(f"stat: cannot statx '{args[-1]}': No such file or "
                         f"directory")
            return 1
        sim.say(f"{cfg['uid']}:{cfg['gid']} 600")
        return 0
    if prog == "cat":
        host = _host_path(sb, args[-1])
        if host is None or not host.is_file():
            sim.complain(f"cat: {args[-1]}: No such file or directory")
            return 1
        sim.out.append(host.read_text(encoding="utf-8"))
        return 0
    sim.complain("fake openshell: unhandled exec")
    return 127


# --- the connector's CLI, and the router's work ------------------------------

def _inbox_submit(sim: Sim, sb: dict, envs: List[str], args: List[str]) -> int:
    outbox_env = dict(e.split("=", 1) for e in envs).get("OUTBOX_DIR", "")
    host = _host_path(sb, outbox_env)
    if host is None:
        sim.complain("inbox-submit: OUTBOX_DIR is not a mounted path")
        return 1
    if args[0] == "submit-result":
        result = host / "results" / f"{args[1]}.json"
        if result.is_file():
            sim.out.append(result.read_text(encoding="utf-8"))
        else:
            sim.say("pending")
        return 0
    if args[0] != "submit":
        sim.complain("inbox-submit: unhandled command")
        return 2
    to = _flag_value(args, "--to")
    subject = _flag_value(args, "--subject") or ""
    body = _flag_value(args, "--body") or ""
    sim.state["req"] += 1
    req_id = "%08x" % sim.state["req"]
    request = sim.fixture("request-minimal")
    request["req_id"] = req_id
    request["draft"]["to"] = [to]
    request["draft"]["subject"] = subject
    request["draft"]["body_text"] = body
    sim.write_json(host / f"req-{req_id}.json", request)
    sim.say(f"Submitted request {req_id} to the relay drop-box "
            f"({outbox_env}/req-{req_id}.json).")
    sim.say(f"Result will appear at: {outbox_env}/results/{req_id}.json")
    if sim.state["router"]:
        _route(sim, sb["name"], to, req_id, subject, body, host)
    return 0


def _peer_notice(sim: Sim, notice_id: str, subject: str, sender: str,
                 in_reply_to: Optional[str]) -> dict:
    doc = sim.fixture("peer-result" if in_reply_to else "peer-minimal")
    doc["notice_id"] = notice_id
    doc["message"]["id"] = notice_id
    doc["message"]["subject"] = subject
    if in_reply_to:
        doc["message"]["in_reply_to"] = in_reply_to
        doc["message"]["references"] = [in_reply_to]
    return doc


def _peer_message(sim: Sim, notice_id: str, subject: str, body: str) -> dict:
    doc = sim.fixture("message-clean")
    doc["notice_id"] = notice_id
    doc["id"] = notice_id
    doc["subject"] = subject
    doc["body_text"] = body
    return doc


def _place(sim: Sim, member: str, lane: str, notice_id: str, notice: dict,
           message: dict) -> None:
    sim.write_json(sim.lane(member, lane, "notices",
                            f"notice-{notice_id}.json"), notice)
    sim.write_json(sim.lane(member, lane, "messages",
                            f"notice-{notice_id}.json"), message)


def _route(sim: Sim, sender: str, to: str, req_id: str, subject: str,
           body: str, outbox: Path) -> None:
    cfg = sim.cfg
    recipient = to.split("@")[0]
    on_edge = [sender, recipient] in cfg["edges"]
    request = outbox / f"req-{req_id}.json"
    processed = outbox / "processed" / f"req-{req_id}.json"
    processed.parent.mkdir(parents=True, exist_ok=True)
    if not on_edge:
        result = sim.fixture("result-queued")
        result["req_id"] = req_id
        sim.write_json(outbox / "results" / f"{req_id}.json", result)
        sim.write_json(sim.home / "router-state" / sender / "held"
                       / f"req-{req_id}.json",
                       json.loads(request.read_text(encoding="utf-8")))
        os.replace(request, processed)
        return
    notice_id = sim.next_notice_id()
    _place(sim, recipient, cfg["lane_peer"], notice_id,
           _peer_notice(sim, notice_id, subject, sender, None),
           _peer_message(sim, notice_id, subject, body))
    sim.audit(recipient, "peer_notice_placed", notice_id=notice_id,
              message_id=notice_id, tree=cfg["lane_peer"], to=to,
              **{"from": f"{sender}@{to.split('@')[1]}"},
              from_instance=sender, to_instance=recipient, req_id=req_id,
              in_reply_to=None, reply_basis=None, origin="local")
    refused = bool(sim.sandbox(recipient)["refuse_active"]) \
        and not sim.flags["silent_refuse"]
    outcome = "refused" if refused else "delivered"
    sim.audit(recipient, "outcome_consumed", tree=cfg["lane_peer"],
              notice_id=notice_id, outcome=outcome,
              outcome_ts="2026-09-29T12:00:00Z", detail=None,
              from_instance=sender, to_instance=recipient)
    result = sim.fixture("result-accepted")
    result["req_id"] = req_id
    sim.write_json(outbox / "results" / f"{req_id}.json", result)
    os.replace(request, processed)
    if refused:
        dsn = sim.next_notice_id()
        notice = sim.fixture("notice-minimal")
        notice["notice_id"] = dsn
        message = sim.fixture("message-clean")
        message["notice_id"] = dsn
        _place(sim, sender, cfg["lane_inbox"], dsn, notice, message)
        sim.audit(sender, "dsn_sent", notice_id=dsn, answers=notice_id,
                  outcome=outcome)
        return
    if sim.flags["reply"]:
        reply = sim.next_notice_id()
        _place(sim, sender, cfg["lane_peer"], reply,
               _peer_notice(sim, reply, "Re: " + subject, recipient,
                            notice_id),
               _peer_message(sim, reply, "Re: " + subject, "Done."))
        sim.audit(sender, "peer_notice_placed", notice_id=reply,
                  message_id=reply, tree=cfg["lane_peer"],
                  to=f"{sender}@{to.split('@')[1]}", **{"from": to},
                  from_instance=recipient, to_instance=sender,
                  req_id="%08x" % (0xf0000000 + sim.state["notice"]),
                  in_reply_to=notice_id, reply_basis="edge", origin="local")


# --- docker ------------------------------------------------------------------

def _roster_file(sim: Sim) -> Path:
    config = sim.state.get("router_config")
    return Path(config).parent / "roster" / "roster.json" if config \
        else Path(os.devnull + "-no-roster")


def _write_roster(sim: Sim, config: str) -> None:
    roster = Path(config).parent / "roster"
    roster.mkdir(parents=True, exist_ok=True)
    sim.write_json(roster / "roster.json", sim.fixture("roster-basic"),
                   mode=0o644)


def _docker(sim: Sim, argv: List[str]) -> int:
    cfg = sim.cfg
    verb = argv[0]
    if verb == "version":
        sim.say("29.8.1")
        return 0
    if verb == "image":
        if argv[1] == "inspect" and argv[-1] == "amap-openshell-agent" \
                and sim.state["labels"] is not None:
            sim.say(json.dumps(sim.state["labels"]))
            return 0
        sim.complain("Error: No such image")
        return 1
    if verb == "build":
        tag = _flag_value(argv, "-t")
        if tag == "amap-openshell-agent":
            if sim.flags["fail_build"]:
                sim.complain("fake docker: build failed")
                return 1
            labels = dict(v.split("=", 1) for v in _flag_values(argv,
                                                                "--label"))
            sim.state["labels"] = labels
            sim.state["builds"].append(argv)
        sim.say("Successfully built")
        return 0
    if verb == "ps":
        for w in argv:
            if w.startswith("label=openshell" ".ai/sandbox-id="):
                want = w.split("=", 2)[2]
                for sb in sim.state["sandboxes"].values():
                    if sb["id"] == want:
                        sim.say(f"c-{sb['name']}")
                return 0
        if sim.flags.get("router_container_state"):
            sim.say(f"amap-router-local {sim.flags['router_container_state']}")
            return 0
        if sim.state["router"]:
            flags = sim.flags
            polls = sim.state.get("router_ps", 0)
            if flags["router_crash"]:
                sim.state["router_ps"] = polls = polls + 1
                if polls <= flags["router_crash_polls"]:
                    sim.say("amap-router-local running")
                else:
                    sim.say(f"amap-router-local {flags['router_crash']}")
                return 0
            if flags["roster_after_polls"] and not _roster_file(sim).is_file():
                sim.state["router_ps"] = polls = polls + 1
                if polls >= flags["roster_after_polls"]:
                    _write_roster(sim, sim.state["router_config"])
            sim.say("amap-router-local running")
        return 0
    if verb == "inspect":
        target = argv[-1]
        fmt = _flag_value(argv, "--format")
        if target == "amap-router-local" and fmt == "{{.State.ExitCode}}" \
                and (sim.state["router"] or sim.flags["router_container_state"]):
            sim.say(str(sim.flags["router_exit_code"]))
            return 0
        if target == "amap-router-local" and sim.state["router"]:
            sim.say(f"{cfg['uid']}:{cfg['gid']}")
            return 0
        if target.startswith("c-") and fmt == "{{json .Mounts}}":
            sb = sim.sandbox(target[2:])
            if sb is not None:
                sim.say(json.dumps([{"Type": "bind", "Source": m["source"],
                                     "Destination": m["target"],
                                     "RW": not m["read_only"]}
                                    for m in sb["mounts"]]))
                return 0
        sim.complain(f"Error: No such object: {target}")
        return 1
    if verb == "logs":
        if sim.flags["router_crash"]:
            sim.say("router: banner")
            sim.complain("router: starting")
            sim.complain(sim.flags["router_crash_log"])
            sim.complain("")
            return 0
        sim.say("router: started")
        sim.say("router: roster written")
        return 0
    if verb == "run" and "amap-openshell-agent" in argv:
        if sim.state["labels"] is None:
            sim.complain("Unable to find image 'amap-openshell-agent:latest'")
            return 125
        paths = sim.flags.get("image_paths") or {}
        sim.say(f"node={paths.get('node') or ''}")
        sim.say(f"claude={paths.get('claude') or ''}")
        return 0
    if verb == "run":
        if sim.state["router"]:
            sim.complain("docker: the container name is already in use")
            return 125
        if sim.flags["fail_router_once"] and not sim.state["router_failed"]:
            sim.state["router_failed"] = True
            sim.complain("docker: fake failure to start the router")
            return 1
        config = None
        for value in _flag_values(argv, "-e"):
            if value.startswith("ROUTER_CONFIG="):
                config = value.split("=", 1)[1]
        assert config, "no ROUTER_CONFIG"
        sim.state["router_config"] = config
        sim.state["router_ps"] = 0
        if not sim.flags["router_crash"] and not sim.flags["roster_after_polls"]:
            _write_roster(sim, config)
        sim.state["router"] = True
        sim.say("c-amap-router-local")
        return 0
    sim.complain("fake docker: unhandled command")
    return 2
