"""A stand-in for the host registry's CLI, for tests (DESIGN.md D27).

It is written from the registration interface's documented behaviour, with
invented identifiers, and it is not the registry: no code or text is copied
from it. It keeps its bindings in `<registry>/registry.json`, delivers
name.json into `<identity_root>/<logical_id>/`, and takes faults from
`<registry>/faults.json`, keyed by op:

  {"show": {"error": "unreadable"}}            refuse with that code
  {"register": {"error": "delivery_failed", "committed": true}}
  {"register": {"error": "internal", "committed": "unknown", "exit": 3}}
  {"show": {"raw": "text"}}                    print exactly this instead
  {"show": {"sleep": 5}}                       answer late
  {"list": {"once": true, ...}}                the fault applies once

Every call is appended to `<registry>/calls.jsonl`, so a test can tell which
ops were sent.

Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

INTERFACE = "example-registry.adapter/1"
MODULE = "example_registry"
NAME_SCHEMA = "example-registry.name/1"
DOMAIN = "agents.example.org"

FAKE_MAIN = r'''
import hashlib, json, os, sys, time

def out(doc, code=0):
    sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
    sys.exit(code)

def err(code, detail="", status=1, **extra):
    doc = {"ok": False, "error": {"code": code, "detail": detail}}
    doc.update(extra)
    out(doc, status)

args = sys.argv[1:]
reg = args[args.index("--registry") + 1]
req = json.loads(sys.stdin.read())
op = req.get("op")
with open(os.path.join(reg, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(req, sort_keys=True) + "\n")

faults_path = os.path.join(reg, "faults.json")
fault = None
if os.path.exists(faults_path):
    faults = json.load(open(faults_path))
    fault = faults.get(op)
    if fault and fault.get("once"):
        del faults[op]
        json.dump(faults, open(faults_path, "w"))
if fault:
    if "sleep" in fault:
        time.sleep(fault["sleep"])
    if "raw" in fault:
        sys.stdout.write(fault["raw"])
        sys.exit(fault.get("exit", 0))

host_json = os.path.join(reg, "host.json")
if op == "version":
    listed = json.load(open(host_json)) if os.path.exists(host_json) else {}
    out({"ok": True, "interfaces": listed.get("interfaces", [INTERFACE])})
if req.get("interface") != INTERFACE:
    err("interface", "unsupported interface", interfaces=[INTERFACE])
if not os.path.exists(host_json):
    err("config_absent", "no host.json")
host = json.load(open(host_json))
domain, root = host["domain"], host["identity_root"]

snap = os.path.join(reg, "registry.json")

def load():
    if not os.path.exists(snap):
        return None
    data = open(snap).read()
    if data == "UNREADABLE":
        err("unreadable", "snapshot unreadable")
    return json.loads(data)

def binding(b, show=False):
    d = {k: b[k] for k in ("logical_id", "name", "adapter",
                           "first_registered_ts", "epoch", "state")}
    d["address"] = b["name"] + "@" + domain
    d["identity_dir"] = os.path.join(root, b["logical_id"])
    if show and b["state"] == "active":
        p = os.path.join(d["identity_dir"], "name.json")
        want = name_json(b)
        if not os.path.exists(p):
            d["name_json"] = "absent"
        else:
            d["name_json"] = "current" if json.load(open(p)) == want else "stale"
    return d

def name_json(b):
    return {"schema": SCHEMA, "name": b["name"],
            "address": b["name"] + "@" + domain,
            "logical_id": b["logical_id"], "epoch": b["epoch"]}

def apply_fault():
    if fault and "error" in fault:
        extra = {}
        if "committed" in fault:
            extra["committed"] = fault["committed"]
        return fault["error"], extra, fault.get("exit", 1)
    return None

f = apply_fault() if op in ("show", "list") else None
if f:
    err(f[0], "injected", f[2], **f[1])

if op in ("show", "list"):
    state = load()
    if state is None:
        err("registry_absent", "no snapshot has been written")
    if op == "list":
        out({"ok": True, "bindings": [binding(b) for b in state["bindings"]]})
    if "logical_id" in req:
        hits = [b for b in state["bindings"] if b["logical_id"] == req["logical_id"]]
    else:
        key = req["workspace"] + "/" + req["sandbox"]
        hits = [b for b in state["bindings"]
                if b["key"] == key and b["state"] == "active"]
    if not hits:
        err("not_found", "no such binding")
    out({"ok": True, "binding": binding(hits[0], show=True)})

if op == "register":
    state = load() or {"bindings": []}
    key = req["workspace"] + "/" + req["sandbox"]
    hits = [b for b in state["bindings"] if b["key"] == key and b["state"] == "active"]
    created = not hits
    if hits:
        b = hits[0]
    else:
        lid = hashlib.sha256(("%s/%d" % (key, len(state["bindings"]))).encode()).hexdigest()[:32]
        taken = {x["name"] for x in state["bindings"]} | set(state.get("earlier", []))
        name = req["sandbox"] if req["sandbox"] not in taken else req["sandbox"] + "-" + lid[:4]
        b = {"logical_id": lid, "name": name, "adapter": "openshell", "key": key,
             "first_registered_ts": "2026-10-13T00:00:00Z", "epoch": 1,
             "state": "active"}
        state["bindings"].append(b)
    json.dump(state, open(snap, "w"))
    for f in ("journal.jsonl", "writer.lock"):
        open(os.path.join(reg, f), "a").close()
    d = binding(b)
    d["created"] = created
    f = apply_fault()
    if f:
        err(f[0], "injected", f[2], binding=d, **f[1]) if f[1].get("committed") is True \
            else err(f[0], "injected", f[2], **f[1])
    ident = os.path.join(root, b["logical_id"])
    if not os.path.isdir(root):
        err("instances_dir_absent", "no identity root", committed=True, binding=d)
    os.makedirs(ident, exist_ok=True)
    os.chmod(ident, 0o755)
    with open(os.path.join(ident, "name.json"), "w") as fh:
        json.dump(name_json(b), fh, sort_keys=True)
    out({"ok": True, "binding": d})

err("bad_request", "unknown op", 2)
'''


def write_checkout(checkout: Path) -> Path:
    """A checkout holding the fake as a runnable module."""
    pkg = checkout / MODULE
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        f"INTERFACE = {INTERFACE!r}\nSCHEMA = {NAME_SCHEMA!r}\n" + FAKE_MAIN)
    return checkout


def real_python() -> str:
    """An interpreter that passes host-registry.json's own checks: owned by
    root or the caller, with no group- or world-writable ancestor. The fake
    is standard library only, so any Python 3 runs it. A CI runner's own
    interpreter may sit under a world-writable directory, which the checks
    rightly refuse, so the system one is the fallback."""
    import host_registry
    tried = []
    for cand in (os.path.realpath(sys.executable),
                 os.path.realpath("/usr/bin/python3")):
        problem = host_registry.owner_problem(cand, (os.getuid(), 0), "python",
                                              want_dir=False)
        if problem is None:
            return cand
        tried.append(problem)
    raise RuntimeError("no interpreter passes the config's checks: "
                       + "; ".join(tried))


class World:
    """A host with the fake registry set up: its checkout, its registry
    directory with host.json, the identity root, and host-registry.json in
    the home."""

    def __init__(self, root: Path, home: Path, domain: str = DOMAIN,
                 write_config: bool = True, make_identity_root: bool = True):
        self.root = root
        self.home = home
        self.checkout = write_checkout(root / "registry-code")
        self.registry = root / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        os.chmod(self.registry, 0o755)
        self.identity_root = root / "identities"
        if make_identity_root:
            self.identity_root.mkdir(exist_ok=True)
            os.chmod(self.identity_root, 0o755)
        self.host_json({"domain": domain,
                        "identity_root": str(self.identity_root)})
        self.config = {
            "python": real_python(), "checkout": str(self.checkout),
            "registry": str(self.registry),
            "identity_root": str(self.identity_root),
            "interface": INTERFACE, "module": MODULE,
            "name_schema": NAME_SCHEMA}
        if write_config:
            self.write_config()

    def write_config(self, doc: Optional[Dict[str, Any]] = None,
                     text: Optional[str] = None) -> Path:
        path = self.home / "host-registry.json"
        path.write_text(text if text is not None else
                        json.dumps(doc if doc is not None else self.config))
        os.chmod(path, 0o644)
        return path

    def host_json(self, doc: Dict[str, Any]) -> None:
        (self.registry / "host.json").write_text(json.dumps(doc))

    def faults(self, doc: Dict[str, Any]) -> None:
        (self.registry / "faults.json").write_text(json.dumps(doc))

    def calls(self) -> list:
        p = self.registry / "calls.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines()]

    def state(self) -> Optional[Dict[str, Any]]:
        p = self.registry / "registry.json"
        return json.loads(p.read_text()) if p.exists() else None

    def retire(self, name: str) -> None:
        s = self.state()
        for b in s["bindings"]:
            if b["name"] == name:
                b["state"] = "retired"
        (self.registry / "registry.json").write_text(json.dumps(s))

    def bind(self, workspace: str, sandbox: str, name: Optional[str] = None,
             epoch: int = 1) -> Dict[str, Any]:
        """A binding made directly, as if registered earlier (no name.json)."""
        s = self.state() or {"bindings": []}
        lid = "%032x" % (len(s["bindings"]) + 1)
        b = {"logical_id": lid, "name": name or sandbox, "adapter": "openshell",
             "key": f"{workspace}/{sandbox}",
             "first_registered_ts": "2026-10-13T00:00:00Z", "epoch": epoch,
             "state": "active"}
        s["bindings"].append(b)
        (self.registry / "registry.json").write_text(json.dumps(s))
        return b
