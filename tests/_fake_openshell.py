"""A fake `openshell` and a fake `docker` for the verbs' and `verify`'s tests.

Nothing here runs OpenShell or Docker. Each fake is a small script that calls
`fake_main`, which reads and updates one JSON state file and records each call in
`calls.jsonl`. `openshell` models `sandbox get` (with `--policy-only`), `list`,
`create`, `delete` and `exec` of `verify`'s two probes, `gateway info -o json`
(its `gateway_info` flag: ok, none, fail or bad_json) and the interceptor probe
(a `create` whose docker block holds the poison key; its `interceptor` flag:
refuse, refuse_wrapped, refuse_other, gateway, failed_closed or allow, the ways V3's create can end). `docker` models `ps` and
`inspect` of a sandbox's containers and of the router container. It models no
router: `_fake_router` does.

A sandbox has two containers, as a real one does (docs/POC-REPORT.md, Pass
criterion 3): `c-<name>` holds the bind mounts and `v-<name>` holds only a
volume. The effective policy is the rendered one, unquoted, plus the provider's network
section, which OpenShell adds.

`create` mints `id-<name>-<n>` from a counter, so a second create of a name
gets a new ID, as OpenShell does. `create` needs `ANTHROPIC_API_KEY` in its
environment (D10). Standard library only, and it imports nothing from the
repository.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROGRAM = "openshell"
ENV_VAR = "FAKE_OPENSHELL_STATE"
KEY_VARIABLE = "ANTHROPIC_API_KEY"
FAKE_KEY = "fake-KEY-not-a-secret-0123456789abcdefghijKLMNOP"
# Pinned to interceptor/wire.py by a test: this file imports nothing from the
# repository.
INTERCEPTOR_PREFIX = "amap-openshell interceptor: "
POISON_KEY = "amap_verify_probe"
# A flag that names a member holds that member's name, or "" for none.
DEFAULT_FLAGS: Dict[str, Any] = {
    "fail_create": False, "fail_get": False, "fail_list": False, "extra": [],
    "other_workspace": [],
    "rw_inbox": "", "drop_policy_path": "", "share_source": "",
    "write_opens": "", "no_delivery": "", "fail_exec": "",
    "fail_docker_ps": False, "fail_inspect": False, "extra_mount": "",
    "router_absent": False, "router_stopped": False, "router_posture": [],
    "policy_append": {}, "gateway_info": "ok", "interceptor": "refuse"}
PROGRAMS = ("openshell", "docker")
PROBE_MARK = "# amap-openshell verify: "
PROVIDER_HOST = "api.anthropic.com"
STUB = """#!{python}
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {tests!r})
import _fake_openshell
sys.exit(_fake_openshell.fake_main({prog!r}, sys.argv[1:]))
"""
TESTS_DIR = Path(__file__).absolute().parent


class Fake:
    def __init__(self, tmp: Path, fake_bin: Path, workspace: str = "default",
                 programs: Tuple[str, ...] = PROGRAMS, **flags: Any) -> None:
        unknown = set(flags) - set(DEFAULT_FLAGS)
        assert not unknown, f"unknown fake flag(s): {sorted(unknown)}"
        self.workspace = workspace
        self.state_path = Path(tmp) / "fake-openshell.json"
        self.calls_path = Path(tmp) / "fake-openshell-calls.jsonl"
        self.state_path.write_text(json.dumps({
            "counter": 0, "workspace": workspace, "sandboxes": {},
            "calls": str(self.calls_path), "router_config": "",
            "flags": {**DEFAULT_FLAGS, **flags}}), encoding="utf-8")
        self._binaries = {}
        for prog in programs:
            path = Path(fake_bin) / prog
            path.write_text(STUB.format(python=sys.executable,
                                        tests=str(TESTS_DIR), prog=prog))
            path.chmod(0o755)
            self._binaries[prog] = path
        self._binary = self._binaries.get(PROGRAM)

    def binary(self) -> str:
        return str(self._binary)

    def docker(self) -> str:
        return str(self._binaries["docker"])

    def set_router_config(self, path: str) -> None:
        """The router.json the fake router container was started from."""
        st = self._state()
        st["router_config"] = str(path)
        self._save(st)

    def set_phase(self, name: str, phase: str) -> None:
        st = self._state()
        st["sandboxes"][name]["phase"] = phase
        self._save(st)

    def env(self) -> Dict[str, str]:
        return {ENV_VAR: str(self.state_path), KEY_VARIABLE: FAKE_KEY,
                "PATH": os.environ.get("PATH", "")}

    def _state(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state: Dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> List[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(ln) for ln in
                self.calls_path.read_text(encoding="utf-8").splitlines() if ln]

    def probes(self) -> List[dict]:
        """What `exec` of a probe did: one record per probe run."""
        path = Path(str(self.calls_path) + ".probes")
        if not path.exists():
            return []
        return [json.loads(ln) for ln in
                path.read_text(encoding="utf-8").splitlines() if ln]

    def sandboxes(self) -> Dict[str, dict]:
        return self._state()["sandboxes"]

    def put(self, name: str, sandbox_id: str) -> None:
        st = self._state()
        st["sandboxes"][name] = {"id": sandbox_id, "name": name,
                                 "workspace": self.workspace,
                                 "phase": "Ready"}
        self._save(st)

    def forget(self, name: str) -> None:
        st = self._state()
        st["sandboxes"].pop(name, None)
        self._save(st)

    def set_flag(self, **kv: Any) -> None:
        st = self._state()
        unknown = set(kv) - set(DEFAULT_FLAGS)
        assert not unknown, f"unknown fake flag(s): {sorted(unknown)}"
        st["flags"].update(kv)
        self._save(st)


def _option(argv: List[str], flag: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else ""


def realistic_policy(text: str) -> str:
    """The rendered policy as OpenShell prints it back: list items unquoted, and
    the attached provider's network section after the rendered keys (docs/
    POC-REPORT.md, Pass criterion 3)."""
    lines = [re.sub(r'^(\s+- )"(.*)"$', r"\1\2", ln)
             for ln in text.splitlines()]
    return ("\n".join(lines) + "\nnetwork_policies:\n"
            "  _provider_amap_claude_code:\n"
            "    name: _provider_amap_claude_code\n    endpoints:\n"
            f"      - host: {PROVIDER_HOST}\n        port: 443\n"
            "    binaries:\n      - path: /usr/local/bin/node\n")


def fake_main(prog: str, argv: List[str]) -> int:
    path = os.environ.get(ENV_VAR)
    if not path:
        print("fake: no state file", file=sys.stderr)
        return 70
    st = json.loads(Path(path).read_text(encoding="utf-8"))
    with open(st["calls"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"prog": prog, "argv": argv,
                             "key": bool(os.environ.get(KEY_VARIABLE))}) + "\n")
    if prog == "docker":
        return _docker(st, argv)
    return _openshell(st, path, argv)


def _complain(text: str) -> int:
    print(text, file=sys.stderr)
    return 1


def _openshell(st: Dict[str, Any], path: str, argv: List[str]) -> int:
    flags = st["flags"]

    def save() -> None:
        Path(path).write_text(json.dumps(st), encoding="utf-8")

    rest = list(argv)
    workspace = st["workspace"]
    if rest[:1] == ["--workspace"]:
        workspace = rest[1]
        rest = rest[2:]
    if rest[:2] == ["gateway", "info"]:
        mode = flags["gateway_info"]
        if mode == "fail":
            return _complain("status: Unavailable, message: gateway down")
        if mode == "bad_json":
            print("not json")
            return 0
        exts = [] if mode == "none" else [{
            "kind": "gateway-interceptor",
            "configured_name": "amap-openshell-mounts",
            "name": "amap-openshell-mounts"}]
        print(json.dumps({"extensions": exts}))
        return 0
    if rest[:1] != ["sandbox"] or len(rest) < 2:
        return _complain("fake openshell: unhandled command")
    verb, rest = rest[1], rest[2:]
    boxes = st["sandboxes"]
    complain = _complain

    if verb == "list":
        if flags["fail_list"]:
            return complain("status: Unavailable, message: gateway down")
        for name in sorted(boxes):
            print(f"{boxes[name]['workspace']}/{name}")
        for name in flags["extra"]:
            print(f"{st['workspace']}/{name}")
        for name in flags["other_workspace"]:
            print(f"elsewhere/{name}")
        return 0
    if verb == "get":
        if flags["fail_get"]:
            return complain("status: Unavailable, message: gateway down")
        box = boxes.get(rest[0])
        if box is None or box["workspace"] != workspace:
            return complain("status: NotFound, message: sandbox not found")
        if "--policy-only" in rest:
            if "policy_text" not in box:
                return complain("status: NotFound, message: no policy")
            print(_effective_policy(box, flags), end="")
            return 0
        print(json.dumps({k: box[k] for k in
                          ("id", "name", "workspace", "phase")}))
        return 0
    if verb == "delete":
        box = boxes.get(rest[0])
        if box is None or box["workspace"] != workspace:
            return complain("status: NotFound, message: sandbox not found")
        del boxes[rest[0]]
        save()
        print(f"deleted {rest[0]}")
        return 0
    if verb == "create":
        name = _option(rest, "--name")
        config_text = _option(rest, "--driver-config-json")
        docker_block = json.loads(config_text)["docker"] if config_text else {}
        if POISON_KEY in docker_block:
            mode = flags["interceptor"]
            source = docker_block["mounts"][0]["source"]
            if mode == "refuse":
                return complain(
                    f'status: PermissionDenied, message: "{INTERCEPTOR_PREFIX}'
                    f'bind source {source} is not one {name} may mount"')
            if mode == "refuse_wrapped":
                # The CLI's error renderer as seen live on the test host (2026-10-05):
                # wrapped to a narrow width when not on a terminal, breaking
                # at spaces and after hyphens, each continuation line under a
                # box-drawing bar.
                message = (f"{INTERCEPTOR_PREFIX}bind source {source} is not "
                           f"one {name} may mount")
                wrapped = message.replace("amap-openshell ", "amap-\n  \u2502 openshell\n  \u2502 ")
                wrapped = wrapped.replace("/amap-home/", "/amap-\n  \u2502 home/")
                return complain(
                    "Error:   \u00d7 code: 'The caller does not have permission "
                    "to execute the specified\n  \u2502 operation', message: "
                    f'"{wrapped}"')
            if mode == "refuse_other":
                return complain(
                    f'status: PermissionDenied, message: "{INTERCEPTOR_PREFIX}'
                    'unknown field spec.bogus"')
            if mode == "gateway":
                return complain(
                    "status: InvalidArgument, message: \"invalid docker "
                    f"driver_config: unknown field `{POISON_KEY}`, expected "
                    "`cdi_devices` or `mounts`\"")
            if mode == "failed_closed":
                return complain(
                    "status: PermissionDenied, message: \"gateway interceptor "
                    "'amap-openshell-mounts' failed closed: transport error\"")
        elif flags["fail_create"]:
            return complain("status: FailedPrecondition, message: provider "
                            "profile 'amap-claude-code' not found")
        if not os.environ.get(KEY_VARIABLE) and POISON_KEY not in docker_block:
            return complain(f"status: FailedPrecondition, message: {KEY_VARIABLE} "
                            f"is not set")
        if name in boxes:
            return complain("status: AlreadyExists, message: sandbox exists")
        st["counter"] += 1
        box = {"id": f"id-{name}-{st['counter']}", "name": name,
               "workspace": workspace, "phase": "Ready"}
        policy_file, config = _option(rest, "--policy"), _option(
            rest, "--driver-config-json")
        if policy_file:
            with open(policy_file, encoding="utf-8") as fh:
                box["policy_text"] = realistic_policy(fh.read())
        if config:
            box["mounts"] = json.loads(config)["docker"]["mounts"]
        boxes[name] = box
        save()
        print(f"created {name}")
        return 0
    if verb == "exec":
        return _exec(st, rest)
    return complain("fake openshell: unhandled sandbox verb")


def _effective_policy(box: Dict[str, Any], flags: Dict[str, Any]) -> str:
    text = box["policy_text"]
    if flags["drop_policy_path"] == box["name"]:
        text = "".join(ln + "\n" for ln in text.splitlines()
                       if ln.strip() != "- /opt/amap/lanes/inbox")
    return text + flags["policy_append"].get(box["name"], "")


def _mount_of(box: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    best = None
    for m in box.get("mounts", []):
        t = m["target"].rstrip("/") + "/"
        if path.startswith(t) and (best is None
                                   or len(t) > len(best["target"])):
            best = m
    return best


def _exec(st: Dict[str, Any], argv: List[str]) -> int:
    flags = st["flags"]
    name = _option(argv, "--name")
    command = argv[argv.index("--") + 1:]
    box = st["sandboxes"].get(name)
    if box is None:
        return _complain("status: NotFound, message: sandbox not found")
    if box["phase"] != "Ready" or flags["fail_exec"] == name:
        return _complain(f"sandbox {name} is not running")
    if command[:2] != ["python3", "-c"]:
        return _complain("fake openshell: only verify's probes are modelled")
    snippet, args = command[2], command[3:]
    marker = snippet.split("\n", 1)[0]
    assert marker.startswith(PROBE_MARK), marker
    kind = marker[len(PROBE_MARK):]
    record = {"sandbox": name, "kind": kind, "args": args}
    code = 0
    if kind == "write-probe":
        target = args[0]
        mount = _mount_of(box, target)
        if flags["write_opens"] == name and mount is not None:
            code = _run_really(snippet, args, mount, target, record)
        else:
            print("EROFS" if mount is None or mount["read_only"] else "OPENED")
    elif kind == "delivery-probe":
        print(0 if flags["no_delivery"] == name else 1)
    else:
        return _complain(f"fake openshell: unknown probe {kind!r}")
    with open(st["calls"] + ".probes", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return code


def _run_really(snippet: str, args: List[str], mount: Dict[str, Any],
                target: str, record: Dict[str, Any]) -> int:
    """Runs the write probe for real against the host directory the mount's
    source names, so a test can see that it cleans up after itself."""
    import contextlib
    import io
    host = os.path.join(mount["source"],
                        target[len(mount["target"].rstrip("/")) + 1:])
    out = io.StringIO()
    argv, sys.argv = sys.argv, ["-c", host, *args[1:]]
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(snippet, "<probe>", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = argv
    record["scratch"] = os.path.basename(host)
    record["left_behind"] = os.path.exists(host)
    sys.stdout.write(out.getvalue())
    return 0


# --- docker ------------------------------------------------------------------

ROUTER_CONTAINER = "amap-router-local"
VOLUME_ROW = {"Type": "volume", "Name": "openshell-channel",
              "Source": "/var/lib/docker/volumes/openshell-channel/_data",
              "Destination": "/.openshell/channel", "RW": True}


def _sandbox_rows(box: Dict[str, Any], flags: Dict[str, Any],
                  peers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for m in box.get("mounts", []):
        row = {"Type": "bind", "Source": m["source"],
               "Destination": m["target"], "RW": not m["read_only"]}
        if flags["rw_inbox"] == box["name"] and m["target"].endswith("/inbox"):
            row["RW"] = True
        if (flags["share_source"] == box["name"]
                and m["target"].endswith("/outbox") and peers):
            other = [p for p in peers[0].get("mounts", [])
                     if p["target"] == m["target"]]
            if other:
                row["Source"] = other[0]["source"]
        rows.append(row)
    if flags["extra_mount"] == box["name"]:
        rows.append({"Type": "bind", "Source": "/srv/elsewhere",
                     "Destination": "/srv/elsewhere", "RW": True})
    return rows


def _docker(st: Dict[str, Any], argv: List[str]) -> int:
    flags = st["flags"]
    boxes = st["sandboxes"]
    verb = argv[0] if argv else ""
    if verb == "ps":
        label = next((w for w in argv if w.startswith("label=")), "")
        if label:
            if flags["fail_docker_ps"]:
                return _complain("Cannot connect to the Docker daemon")
            want = label.split("=", 2)[2]
            for name in sorted(boxes):
                box = boxes[name]
                if box["id"] == want and box["phase"] == "Ready":
                    print(f"v-{name}")
                    print(f"c-{name}")
            return 0
        if any(w.startswith("name=^") for w in argv):
            if flags["fail_docker_ps"]:
                return _complain("Cannot connect to the Docker daemon")
            if not flags["router_absent"]:
                print(ROUTER_CONTAINER)
            return 0
        return _complain("fake docker: unhandled ps")
    if verb == "inspect":
        target = argv[-1]
        fmt = _option(argv, "--format") or _option(argv, "-f")
        if target == ROUTER_CONTAINER and not flags["router_absent"]:
            return _inspect_router(st, fmt)
        if flags["fail_inspect"]:
            return _complain("Error: fake inspect failure")
        if fmt == "{{json .Mounts}}" and target[:2] in ("c-", "v-"):
            name = target[2:]
            box = boxes.get(name)
            if box is None:
                return _complain(f"Error: No such object: {target}")
            if target.startswith("v-"):
                print(json.dumps([VOLUME_ROW]))
            else:
                peers = [boxes[n] for n in sorted(boxes) if n != name]
                print(json.dumps(_sandbox_rows(box, flags, peers)))
            return 0
        return _complain(f"Error: No such object: {target}")
    return _complain("fake docker: unhandled command")


def _inspect_router(st: Dict[str, Any], fmt: str) -> int:
    flags = st["flags"]
    if fmt == "{{.State.Running}}":
        print("false" if flags["router_stopped"] else "true")
        return 0
    if fmt == "{{.HostConfig.NetworkMode}} {{.HostConfig.RestartPolicy.Name}}":
        print(" ".join(flags["router_posture"] or ["none", "unless-stopped"]))
        return 0
    if fmt == "{{json .Mounts}}":
        import _fake_router
        rows = _fake_router.container_rows(st["router_config"])
        print(json.dumps([{"Type": "bind", "Source": p, "Destination": p,
                           "RW": rw} for p, rw in rows]))
        return 0
    return _complain("fake docker: unhandled router inspect")
