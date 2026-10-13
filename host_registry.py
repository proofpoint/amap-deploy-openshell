"""The OpenShell adapter's client for the host registry (DESIGN.md D27).

The host registry is the host's single registry writer: a separate program
that this repository neither ships nor names. Its identifiers (the interface
token, the module the CLI runs as, and name.json's schema id) are not written
here. They come from `host-registry.json` in `$AMAP_OPENSHELL_HOME`, an
operator-owned file that is never committed (`load_config`). This module never
imports the registry. It runs `python -E -s -m <module> --registry DIR` from
the registry's checkout, writes one JSON request to its stdin, and reads one
JSON line from its stdout.

Four things live here, and nothing else:

- **The strict reader.** Every response, the config file and name.json are
  read by one reader, which refuses a duplicate member name anywhere and the
  non-finite numbers. It is `payload/identity-check`'s, loaded by path, so the
  sandbox and the host apply one validator.
- **The config** and its ownership checks.
- **The runner and the classifiers.** A lookup (`show`, `list`) is classified
  by the status table in DESIGN.md D27, and a `register` into one of four
  outcomes. Both branch on the response's `ok` and `committed` members, never
  on the exit status.
- **Overlap by filesystem identity.** Whether one path is at, under or above
  another is decided by comparing (st_dev, st_ino) along the ancestor chains,
  never by comparing strings.

Standard library only. It imports no sibling repository and none of this
repository's modules.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import posixpath
import stat
import subprocess
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, NamedTuple, Optional,
                    Sequence, Tuple)

REPO = Path(__file__).absolute().parent
IDENTITY_CHECK = REPO / "payload" / "identity-check"


def _load_identity_check():
    loader = importlib.machinery.SourceFileLoader("identity_check",
                                                  str(IDENTITY_CHECK))
    spec = importlib.util.spec_from_loader("identity_check", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


identity_check = _load_identity_check()
Refused = identity_check.Refused
strict_loads = identity_check.strict_loads
check_document = identity_check.check_document
IDENTITY_TARGET = identity_check.IDENTITY_TARGET
NAME_JSON = identity_check.NAME_JSON

ADAPTER = "openshell"
CONFIG_NAME = "host-registry.json"
MAX_CONFIG = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
PATH_KEYS = ("python", "checkout", "registry", "identity_root")
TOKEN_KEYS = ("interface", "module", "name_schema")
CONFIG_KEYS = PATH_KEYS + TOKEN_KEYS
# The registry's own files that its writer uid owns. overlay.json is not one:
# it is root-owned by design. host.json is the operator's data, checked for
# being readable, never for its owner.
REGISTRY_OWNED = ("journal.jsonl", "registry.json", "writer.lock")
HOST_CONFIG = "host.json"

# The runner's isolation. `-I` would also drop the working directory from
# sys.path, which `-m` needs, so `-E -s` is used with the checkout as cwd.
INTERPRETER_FLAGS = ("-E", "-s")
MINIMAL_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
SHOW_TIMEOUT_S = 10.0
REGISTER_TIMEOUT_S = 60.0

# Lookup kinds (DESIGN.md D27, the status table).
BOUND, UNBOUND, EMPTY = "bound", "unbound", "empty"
UNKNOWN, BROKEN, DEFECT = "unknown", "broken-config", "defect"
# register outcomes.
REGISTERED, UNDELIVERED, REFUSED = "registered", "undelivered", "refused"

UNBOUND_CODES = ("not_found",)
EMPTY_CODES = ("registry_absent",)
UNKNOWN_CODES = ("unreadable", "invalid", "internal")
BROKEN_CODES = ("config_absent", "instances_dir_absent",
                "adapter_not_configured", "interface")
DEFECT_CODES = ("bad_request",)

BINDING_REQUIRED = ("logical_id", "name", "address", "adapter",
                    "first_registered_ts", "epoch", "state")
BINDING_OPTIONAL = ("identity_dir", "name_json", "created")
BINDING_STATES = ("active", "retired")
NAME_JSON_STATUSES = ("current", "stale", "absent", "unreadable")


class ConfigError(ValueError):
    """host-registry.json, or a path it names, cannot be used. Fail closed."""


class SymlinkInPath(OSError):
    """A path has a symlink among its components."""


# --- the config --------------------------------------------------------------

class Config(NamedTuple):
    python: str
    checkout: str
    registry: str
    identity_root: str
    interface: str
    module: str
    name_schema: str


def config_path(home: str) -> str:
    return posixpath.join(home, CONFIG_NAME)


def _abs_normal(value: Any) -> bool:
    return (isinstance(value, str) and value.startswith("/")
            and posixpath.normpath(value) == value)


def _token(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)
            and not any(c.isspace() for c in value))


def ancestors(path: str) -> List[str]:
    """`path`'s proper ancestors, `/` first."""
    parts = [p for p in path.split("/") if p]
    return ["/" + "/".join(parts[:i]) for i in range(len(parts))]


def _writable_by_others(mode: int) -> bool:
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def writable_ancestor(path: str,
                      lstat: Callable = os.lstat) -> Optional[str]:
    """The first ancestor of `path` that is group- or world-writable without
    the sticky bit, or None. A sticky world-writable directory (/tmp) cannot
    have another user's entry renamed or removed, so it is not counted."""
    for a in ancestors(path):
        st = lstat(a)
        if _writable_by_others(st.st_mode) and not st.st_mode & stat.S_ISVTX:
            return a
    return None


def owner_problem(path: str, uids: Sequence[int], what: str,
                  want_dir: Optional[bool] = None,
                  lstat: Callable = os.lstat) -> Optional[str]:
    """Why `path` is not a real file or directory owned by one of `uids`,
    not group- or world-writable, with no such ancestor. None when sound."""
    try:
        st = lstat(path)
    except OSError as e:
        return f"{what} {path} cannot be read: {e.strerror or e}"
    if stat.S_ISLNK(st.st_mode):
        return f"{what} {path} is a symlink; name its real path"
    if want_dir is True and not stat.S_ISDIR(st.st_mode):
        return f"{what} {path} is not a directory"
    if want_dir is False and not stat.S_ISREG(st.st_mode):
        return f"{what} {path} is not a regular file"
    if st.st_uid not in uids:
        return (f"{what} {path} is owned by uid {st.st_uid}, not "
                f"{' or '.join(str(u) for u in uids)}")
    if _writable_by_others(st.st_mode):
        return (f"{what} {path} is group- or world-writable "
                f"(mode {stat.S_IMODE(st.st_mode):04o}); "
                f"run: chmod go-w {path}")
    try:
        bad = writable_ancestor(path, lstat)
    except OSError as e:
        return f"{what} {path}: an ancestor cannot be read: {e.strerror or e}"
    if bad:
        return (f"{what} {path} has a group- or world-writable ancestor "
                f"{bad}; run: chmod go-w {bad}")
    return None


def _read_regular(path: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | \
        getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{path} is not a regular file")
        if st.st_size > limit:
            raise ConfigError(f"{path} is larger than {limit} bytes")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ConfigError(f"{path} is larger than {limit} bytes")
    return data


def parse_config(doc: Any, where: str = CONFIG_NAME) -> Config:
    """A closed document: exactly CONFIG_KEYS, absolute normalised paths and
    whitespace-free tokens. Every identifier is opaque: compared, never
    parsed."""
    if not isinstance(doc, dict):
        raise ConfigError(f"{where} is not a JSON object")
    keys = set(doc)
    if keys != set(CONFIG_KEYS):
        missing = sorted(set(CONFIG_KEYS) - keys)
        extra = sorted(keys - set(CONFIG_KEYS))
        raise ConfigError(f"{where} must have exactly {', '.join(CONFIG_KEYS)}"
                          + (f"; missing {', '.join(missing)}" if missing else "")
                          + (f"; extra {', '.join(extra)}" if extra else ""))
    for k in PATH_KEYS:
        if not _abs_normal(doc[k]):
            raise ConfigError(f"{where}: {k} is not an absolute, normalised "
                              f"path")
    for k in TOKEN_KEYS:
        if not _token(doc[k]):
            raise ConfigError(f"{where}: {k} is not a non-empty token without "
                              f"whitespace")
    return Config(**{k: doc[k] for k in CONFIG_KEYS})


def load_config(home: str, uid: Optional[int] = None,
                keep_out_of: Sequence[str] = (),
                lstat: Callable = os.lstat) -> Config:
    """`host-registry.json` from the home, with every check D27 makes:

    - the file: a regular file opened without following a symlink, at most
      MAX_CONFIG bytes, owned by `uid` (the operator's, by default the
      caller's), not group- or world-writable, with no such ancestor, read by
      the strict reader and closed;
    - `python` (a regular file) and `checkout` (a directory): owned by `uid`
      or root, with the same writability checks, given by their real paths;
    - `identity_root`: no group- or world-writable ancestor;
    - none of the file, `python` or `checkout` at or under any of
      `keep_out_of` (the bound sources' roots).

    Raises ConfigError on the first failure. Nothing is created."""
    uid = os.getuid() if uid is None else uid
    path = config_path(home)
    problem = owner_problem(path, (uid,), CONFIG_NAME, want_dir=False,
                            lstat=lstat)
    if problem:
        raise ConfigError(problem)
    try:
        data = _read_regular(path, MAX_CONFIG)
    except OSError as e:
        raise ConfigError(f"{path} cannot be read: {e.strerror or e}") from None
    try:
        cfg = parse_config(strict_loads(data), path)
    except Refused as e:
        raise ConfigError(f"{path}: {e}") from None
    for what, p, want_dir in (("python", cfg.python, False),
                              ("checkout", cfg.checkout, True)):
        problem = owner_problem(p, (uid, 0), f"{CONFIG_NAME} {what}",
                                want_dir=want_dir, lstat=lstat)
        if problem:
            raise ConfigError(problem)
    try:
        bad = writable_ancestor(cfg.identity_root, lstat)
    except OSError as e:
        raise ConfigError(f"identity_root {cfg.identity_root}: an ancestor "
                          f"cannot be read: {e.strerror or e}") from None
    if bad:
        raise ConfigError(f"identity_root {cfg.identity_root} has a group- or "
                          f"world-writable ancestor {bad}; run: chmod go-w {bad}")
    for root in keep_out_of:
        try:
            lstat(root)
        except FileNotFoundError:
            continue   # nothing is under it yet; the check runs at every use
        for what, p in (("the config file", path), ("python", cfg.python),
                        ("checkout", cfg.checkout)):
            try:
                rel = relation(p, root, lstat)
            except OSError as e:
                raise ConfigError(f"cannot compare {what} {p} with {root}: "
                                  f"{e.strerror or e}") from None
            if rel in ("equal", "under"):
                raise ConfigError(f"{what} {p} is at or under {root}, which is "
                                  f"mounted into sandboxes")
    return cfg


def ownership_problems(cfg: Config, uid: Optional[int] = None,
                       lstat: Callable = os.lstat) -> List[str]:
    """D27's owner checks: the registry directory and its writer's own files
    (when present) and the identity root, against the operator uid; host.json
    only for being a readable regular file. overlay.json is never checked:
    it is root-owned by design."""
    uid = os.getuid() if uid is None else uid
    probs: List[str] = []
    for what, p, want_dir in (("the registry directory", cfg.registry, True),
                              ("identity_root", cfg.identity_root, True)):
        problem = owner_problem(p, (uid,), what, want_dir=want_dir, lstat=lstat)
        if problem:
            probs.append(problem)
    for name in REGISTRY_OWNED:
        p = posixpath.join(cfg.registry, name)
        try:
            st = lstat(p)
        except FileNotFoundError:
            continue       # an empty registry has none of them yet
        except OSError as e:
            probs.append(f"{p} cannot be read: {e.strerror or e}")
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            probs.append(f"{p} is not a regular file")
        elif st.st_uid != uid:
            probs.append(f"{p} is owned by uid {st.st_uid}, not {uid}: the "
                         f"registry's writer must be the operator")
    hj = posixpath.join(cfg.registry, HOST_CONFIG)
    try:
        st = lstat(hj)
    except OSError as e:
        probs.append(f"{hj} cannot be read: {e.strerror or e}")
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            probs.append(f"{hj} is not a regular file")
        elif not os.access(hj, os.R_OK):
            probs.append(f"{hj} is not readable")
    try:
        st = lstat(cfg.identity_root)
    except OSError:
        pass                 # reported above
    else:
        if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) != 0o755:
            probs.append(f"identity_root {cfg.identity_root} has mode "
                         f"{stat.S_IMODE(st.st_mode):04o}, not 0755; run: "
                         f"chmod 0755 {cfg.identity_root}")
    return probs


def layout_problems(cfg: Config, router_instances_dir: str,
                    lstat: Callable = os.lstat) -> List[str]:
    """the identity root is neither at, under nor above the router's
    instances directory or the registry directory, by filesystem identity.
    A path that cannot be inspected is a problem, never a pass."""
    probs: List[str] = []
    for what, other in (("the router's instances_dir", router_instances_dir),
                        ("the registry directory", cfg.registry)):
        try:
            rel = relation(cfg.identity_root, other, lstat)
        except (OSError, ValueError) as e:
            probs.append(f"cannot compare identity_root {cfg.identity_root} "
                         f"with {what} {other}: {e}")
            continue
        if rel != "disjoint":
            how = {"equal": "the same directory as"}.get(rel, rel)
            probs.append(f"identity_root {cfg.identity_root} is {how} {what} "
                         f"{other}; they must be separate trees")
    return probs


def entries(path: str) -> Optional[List[str]]:
    """A directory's entry names, or None when it cannot be listed."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


# --- overlap by filesystem identity ------------------------------------------

def chain(path: str, lstat: Callable = os.lstat) -> List[Tuple[int, int]]:
    """(st_dev, st_ino) for `/` and each component of `path`, in order.
    A symlink anywhere refuses (SymlinkInPath). Any stat failure raises."""
    if not _abs_normal(path):
        raise ValueError(f"{path!r} is not an absolute, normalised path")
    out = []
    for p in ancestors(path) + ([path] if path != "/" else []):
        st = lstat(p)
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkInPath(f"{p} is a symlink")
        out.append((st.st_dev, st.st_ino))
    return out


def relation(a: str, b: str, lstat: Callable = os.lstat) -> str:
    """How `a` stands to `b`: "equal", "under" (a is below b), "above" (a
    contains b) or "disjoint". Raises when either cannot be inspected, so a
    caller refuses rather than guesses."""
    ca, cb = chain(a, lstat), chain(b, lstat)
    if ca[-1] == cb[-1]:
        return "equal"
    if cb[-1] in ca:
        return "under"
    if ca[-1] in cb:
        return "above"
    return "disjoint"


# --- the runner --------------------------------------------------------------

class Answer(NamedTuple):
    exit: Optional[int]       # None when the process could not finish
    doc: Optional[Dict[str, Any]]
    problem: Optional[str]    # why there is no usable document


def command(cfg: Config) -> List[str]:
    return [cfg.python, *INTERPRETER_FLAGS, "-m", cfg.module,
            "--registry", cfg.registry]


def request(cfg: Config, op: str, **fields: Any) -> Dict[str, Any]:
    req: Dict[str, Any] = {"op": op}
    if op != "version":
        req["interface"] = cfg.interface
    req.update(fields)
    return req


def call(cfg: Config, req: Mapping[str, Any], timeout: float,
         run: Callable = subprocess.run) -> Answer:
    """Runs the CLI once: no shell, the checkout as cwd, a minimal fixed
    environment, a timeout. The answer must be exactly one line holding one
    JSON object, read by the strict reader."""
    try:
        proc = run(command(cfg), input=json.dumps(dict(req)).encode("utf-8"),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   cwd=cfg.checkout, env=dict(MINIMAL_ENV), timeout=timeout,
                   check=False)
    except subprocess.TimeoutExpired:
        return Answer(None, None, f"the host registry did not answer within "
                                  f"{timeout:g}s")
    except OSError as e:
        return Answer(None, None, f"the host registry could not be run: "
                                  f"{e.strerror or e}")
    out = proc.stdout or b""
    if len(out) > MAX_RESPONSE:
        return Answer(proc.returncode, None, "the response is too large")
    lines = out.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != 1:
        return Answer(proc.returncode, None,
                      f"the response is {len(lines)} lines, not one")
    try:
        doc = strict_loads(lines[0])
    except Refused as e:
        return Answer(proc.returncode, None, f"the response is refused: {e}")
    if not isinstance(doc, dict):
        return Answer(proc.returncode, None, "the response is not an object")
    return Answer(proc.returncode, doc, None)


# --- classifying -------------------------------------------------------------

def binding_problem(b: Any, want_identity_dir: bool = True) -> Optional[str]:
    """Why `b` is not a well-formed openshell binding. None when it is."""
    if not isinstance(b, dict):
        return "the binding is not an object"
    keys = set(b)
    missing = [k for k in BINDING_REQUIRED if k not in keys]
    extra = sorted(keys - set(BINDING_REQUIRED) - set(BINDING_OPTIONAL))
    if missing:
        return f"the binding lacks {', '.join(missing)}"
    if extra:
        return f"the binding has unknown member(s) {', '.join(extra)}"
    if not isinstance(b["logical_id"], str) or \
            not identity_check.LOGICAL_ID.match(b["logical_id"]):
        return "the binding's logical_id is not 32 lower-case hex"
    for k in ("name", "address", "first_registered_ts"):
        if not isinstance(b[k], str) or not b[k]:
            return f"the binding's {k} is not a non-empty string"
    if b["address"].partition("@")[0] != b["name"]:
        return "the binding's address is not name@domain"
    if b["adapter"] != ADAPTER:
        return f"the binding's adapter is {b['adapter']!r}, not {ADAPTER!r}"
    if not identity_check.is_epoch(b["epoch"]):
        return "the binding's epoch is not an integer of at least 1"
    if b["state"] not in BINDING_STATES:
        return f"the binding's state {b['state']!r} is unknown"
    if "identity_dir" in b and not _abs_normal(b["identity_dir"]):
        return "the binding's identity_dir is not an absolute, normalised path"
    if want_identity_dir and b["state"] == "active" and "identity_dir" not in b:
        return "the binding has no identity_dir: the adapter is not configured"
    if "name_json" in b and b["name_json"] not in NAME_JSON_STATUSES:
        return f"the binding's name_json {b['name_json']!r} is unknown"
    if "created" in b and not isinstance(b["created"], bool):
        return "the binding's created is not a boolean"
    return None


class Lookup(NamedTuple):
    kind: str                         # BOUND, UNBOUND, EMPTY, UNKNOWN, BROKEN, DEFECT
    binding: Optional[Dict[str, Any]] = None
    bindings: Tuple[Dict[str, Any], ...] = ()
    code: Optional[str] = None
    detail: str = ""


def _error(doc: Mapping[str, Any]) -> Tuple[Optional[str], str]:
    err = doc.get("error")
    if not isinstance(err, dict) or not isinstance(err.get("code"), str):
        return None, "the refusal has no error code"
    detail = err.get("detail")
    return err["code"], detail if isinstance(detail, str) else ""


def classify_code(code: Optional[str]) -> str:
    if code in UNBOUND_CODES:
        return UNBOUND
    if code in EMPTY_CODES:
        return EMPTY
    if code in BROKEN_CODES:
        return BROKEN
    if code in DEFECT_CODES:
        return DEFECT
    return UNKNOWN   # UNKNOWN_CODES, and any code this table does not list


def classify_lookup(answer: Answer, op: str) -> Lookup:
    """A `show` or `list` answer, by the status table. Never raises."""
    if answer.problem or answer.doc is None:
        return Lookup(UNKNOWN, detail=answer.problem or "no answer")
    doc = answer.doc
    if answer.exit == 3:
        return Lookup(UNKNOWN, code="internal", detail="exit status 3")
    if doc.get("ok") is True:
        if op == "show":
            if set(doc) != {"ok", "binding"}:
                return Lookup(UNKNOWN, detail="show's answer is not {ok, binding}")
            problem = binding_problem(doc["binding"])
            if problem:
                return Lookup(UNKNOWN, detail=problem)
            if "name_json" not in doc["binding"] and \
                    doc["binding"]["state"] == "active":
                return Lookup(UNKNOWN, detail="show's binding has no name_json")
            return Lookup(BOUND, binding=doc["binding"])
        if set(doc) != {"ok", "bindings"} or not isinstance(doc["bindings"], list):
            return Lookup(UNKNOWN, detail="list's answer is not {ok, bindings}")
        out = []
        for b in doc["bindings"]:
            if isinstance(b, dict) and b.get("adapter") != ADAPTER:
                out.append(b)        # another adapter's: kept, not validated
                continue
            problem = binding_problem(b, want_identity_dir=False)
            if problem:
                return Lookup(UNKNOWN, detail=f"list: {problem}")
            out.append(b)
        return Lookup(BOUND, bindings=tuple(out))
    if doc.get("ok") is False:
        if set(doc) - {"ok", "error"}:
            return Lookup(UNKNOWN, detail="a refusal with unexpected members")
        code, detail = _error(doc)
        return Lookup(classify_code(code), code=code, detail=detail)
    return Lookup(UNKNOWN, detail="the answer has no boolean ok")


class Registration(NamedTuple):
    outcome: str                      # REGISTERED, UNDELIVERED, UNKNOWN, REFUSED
    binding: Optional[Dict[str, Any]] = None
    code: Optional[str] = None
    detail: str = ""


def classify_register(answer: Answer) -> Registration:
    """A `register` answer, branching on `ok` and `committed`, never on the
    exit status. Never raises."""
    if answer.problem or answer.doc is None:
        return Registration(UNKNOWN, detail=answer.problem or "no answer")
    doc = answer.doc
    if doc.get("ok") is True:
        if set(doc) != {"ok", "binding"}:
            return Registration(UNKNOWN, detail="register's answer is not "
                                                "{ok, binding}")
        problem = binding_problem(doc["binding"])
        if problem:
            return Registration(UNKNOWN, detail=problem)
        return Registration(REGISTERED, binding=doc["binding"])
    if doc.get("ok") is not False:
        return Registration(UNKNOWN, detail="the answer has no boolean ok")
    if set(doc) - {"ok", "error", "committed", "binding"}:
        return Registration(UNKNOWN, detail="a refusal with unexpected members")
    code, detail = _error(doc)
    committed = doc.get("committed", None)
    if committed is True:
        b = doc.get("binding")
        problem = binding_problem(b) if b is not None else \
            "a committed refusal without its binding"
        if problem:
            return Registration(UNKNOWN, code=code, detail=problem)
        return Registration(UNDELIVERED, binding=b, code=code, detail=detail)
    if committed == "unknown" or answer.exit == 3:
        return Registration(UNKNOWN, code=code, detail=detail)
    if "committed" in doc:
        return Registration(UNKNOWN, code=code,
                            detail=f"committed is {committed!r}")
    return Registration(REFUSED, code=code, detail=detail)


# --- the operations ----------------------------------------------------------

def show(cfg: Config, workspace: str, sandbox: str,
         run: Callable = subprocess.run) -> Lookup:
    """The read-only lookup the interceptor makes. It never writes."""
    return classify_lookup(call(cfg, request(cfg, "show", adapter=ADAPTER,
                                             workspace=workspace,
                                             sandbox=sandbox),
                                SHOW_TIMEOUT_S, run), "show")


def list_bindings(cfg: Config, run: Callable = subprocess.run) -> Lookup:
    return classify_lookup(call(cfg, request(cfg, "list"), SHOW_TIMEOUT_S, run),
                           "list")


def version(cfg: Config, run: Callable = subprocess.run) -> Tuple[Optional[bool], str]:
    """Whether the registry speaks the configured interface: True, False, or
    None when that cannot be told."""
    a = call(cfg, request(cfg, "version"), SHOW_TIMEOUT_S, run)
    if a.problem or a.doc is None:
        return None, a.problem or "no answer"
    if a.doc.get("ok") is not True or not isinstance(a.doc.get("interfaces"), list):
        return None, "version's answer is not {ok, interfaces}"
    return cfg.interface in a.doc["interfaces"], ""


def register(cfg: Config, workspace: str, sandbox: str,
             incarnation: Optional[str] = None,
             run: Callable = subprocess.run) -> Registration:
    fields: Dict[str, Any] = {"adapter": ADAPTER, "workspace": workspace,
                              "sandbox": sandbox}
    if incarnation is not None:
        fields["incarnation"] = incarnation
    return classify_register(call(cfg, request(cfg, "register", **fields),
                                  REGISTER_TIMEOUT_S, run))


# --- the expected identity ---------------------------------------------------

class Expected(NamedTuple):
    """What name.json must hold, as rendered into the sandbox (D27)."""
    schema: str
    name: str
    address: str
    logical_id: str
    epoch: int
    identity_dir: str

    def document(self) -> Dict[str, Any]:
        return {"schema": self.schema, "name": self.name,
                "address": self.address, "logical_id": self.logical_id,
                "epoch": self.epoch}


def expected_of(cfg: Config, binding: Mapping[str, Any]) -> Expected:
    return Expected(cfg.name_schema, binding["name"], binding["address"],
                    binding["logical_id"], binding["epoch"],
                    binding["identity_dir"])


def identity_dir_problem(cfg: Config, binding: Mapping[str, Any],
                         uid: Optional[int] = None,
                         lstat: Callable = os.lstat) -> Optional[str]:
    """Why the binding's identity_dir may not be mounted: it must be
    `identity_root/<logical_id>` by filesystem identity, a real directory owned
    by the operator, mode 0755, holding name.json."""
    uid = os.getuid() if uid is None else uid
    want = posixpath.join(cfg.identity_root, binding["logical_id"])
    got = binding.get("identity_dir")
    if got != want:
        return f"identity_dir {got} is not {want}"
    try:
        if relation(got, want, lstat) != "equal":
            return f"identity_dir {got} is not {want} on disk"
        st = lstat(got)
    except OSError as e:
        return f"identity_dir {got} cannot be inspected: {e.strerror or e}"
    if not stat.S_ISDIR(st.st_mode):
        return f"identity_dir {got} is not a directory"
    if st.st_uid != uid:
        return f"identity_dir {got} is owned by uid {st.st_uid}, not {uid}"
    if stat.S_IMODE(st.st_mode) != 0o755:
        return (f"identity_dir {got} has mode {stat.S_IMODE(st.st_mode):04o}, "
                f"not 0755; run: chmod 0755 {got}")
    return None


def read_name_json(identity_dir: str) -> bytes:
    """The host-side read of name.json, by the sandbox's own reader."""
    return identity_check.read_name_json(identity_dir)


def name_json_problems(identity_dir: str, expected: Expected) -> List[str]:
    """Every reason the delivered name.json is not `expected`."""
    try:
        doc = strict_loads(read_name_json(identity_dir))
    except Refused as e:
        return [str(e)]
    return check_document(doc, expected.document())
