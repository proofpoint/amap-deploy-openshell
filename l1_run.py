"""The `l1-run` verb: docs/L1-RUNBOOK.md steps 0-9 as one command.

Host side only. Unlike `l1-kit`, which writes files and runs nothing, this
runs `openshell`, `docker` and the router's scripts, so it is its own verb. It is
a dry run without `--apply`: it prints each command and runs nothing. With
`--apply` every external command goes through one `Executor`, which records the
argv, the output, the exit status and the time under
`$AMAP_OPENSHELL_HOME/evidence/step-<N>/`, with the provider key and the
messaging tokens redacted.

What it never does: delete a provider profile or a provider, delete a sandbox other than the throwaway probe of Unknown 3
that it created (its ID is re-read just before the delete and must equal the ID
it recorded at creation), remove a lane, remove or restart the router container, edit or
restart the gateway, or print `ANTHROPIC_API_KEY`.

What it decides, and from what. Every verdict comes from evidence the agents in
the sandboxes cannot write: the router's private state directory (never mounted
into a sandbox), read-only lanes and the roster listed on the host, `docker
inspect`, `sandbox get --policy-only`, and probes run with `sandbox exec` from
the image's read-only system paths or the read-only payload. The requests and
the router's copies of each result that the protocol itself places in an
outbox are checked only for their format (Pass criterion 1) or as the
connector's own view (`submit_result`, Pass criterion 2). Neither is a trust
verdict.

OpenShell facts, cited at OpenShell main@acbac9c (and v0.1.2 where noted):

- `sandbox exec` has `--no-tty`, `--no-login-shell`, `--timeout` and `--env`:
  crates/openshell-cli/src/main.rs:1681-1727 (v0.1.2 main.rs:1673-1719).
- The exec argv is shell-escaped one word at a time:
  crates/openshell-server/src/grpc/sandbox.rs:3213-3229.
- An exec'd process is spawned by the supervisor with a cleared environment, so
  it is never a descendant of the main process
  (crates/openshell-sandbox/src/boundary_exec.rs:123-190, 422-444). The runner
  therefore does not run the lister as a command. It loads the lister as a
  module and calls its `rows` (`SESSION_PROBE`).
- `sandbox get --output json` has `id`, `name`, `workspace` and `phase`
  (crates/openshell-cli/src/run.rs:2789-2797); the phase names are
  crates/openshell-cli/src/commands/common.rs:58-71.
- `sandbox get --policy-only`: main.rs:1598-1601 and
  docs/how-it-works/sandboxes/overview.mdx:612.
- `sandbox list --names --all-workspaces` prints `<workspace>/<name>`:
  main.rs:1610-1640 and run.rs:2598-2620.
- `sandbox delete`, `stop` and `start`: main.rs:1642, 1654 and 1662 (v0.1.2:
  1634, 1646 and 1654).
- `logs`: main.rs:531-556. `status`: main.rs:612-614. `--version`:
  main.rs:497-499.
- A sandbox's container carries the labels `sandbox-id` and
  `isolation-role=sandbox`, in OpenShell's own label namespace:
  crates/openshell-core/src/driver_utils.rs:16-31 and
  crates/openshell-driver-docker/src/lib.rs:5638-5745 (the sandbox id at 5723,
  the role at 5741-5744).
- `openshell-gateway config preflight --path <toml>` validates a gateway
  configuration without changing it or starting the gateway; the runner treats
  exit 0 as one of D6's posture findings: crates/openshell-server/src/cli.rs:56
  (`Preflight`), 60-68 (`--path`), 274 (`bin_name` `openshell-gateway`) and 289.
- The provider profile commands, at main@acbac9c and at v0.1.2 (the lines are
  the same at both, except where noted), in crates/openshell-cli/src/main.rs:
- `provider`: main.rs:583-585. `provider profile`: main.rs:929-931.
- `provider list-profiles -o`: main.rs:917-927 (dispatch 3948-3957; v0.1.2
  3938-3947). The `json` output format: main.rs:766-771.
- `provider profile lint -f`: main.rs:1143-1161, the same at v0.1.2.
- `provider profile import -f`: main.rs:1108-1126, the same at v0.1.2.
- `provider profile update <id> -f`: main.rs:1128-1141, the same at v0.1.2.
- The implementations are crates/openshell-cli/src/commands/provider.rs:
  1590-1615 and 1701-1830 (both revisions).
- An update must carry a non-zero `resource_version`:
  crates/openshell-server/src/grpc/provider.rs:2894-2908. Import is create-only:
  docs/how-it-works/providers/profiles.mdx:308, 317.
- A network rule's binaries match the real path of the executable that opens the
  connection: docs/how-it-works/policies/network-rules.mdx:57-77.
- The gateway's built-in listener is loopback, 127.0.0.1:17670:
  docs/how-it-works/gateways/configuration.mdx:36-38.

amap-router-local (found by discovery; paths are relative to its checkout):

- The audit log is `state_dir/<instance>/audit/log.jsonl`, with the events
  `peer_notice_placed` and `outcome_consumed`: router/audit.py:1-45, 62-71,
  86-91, deliver.py:482-498 and outcomes.py:400-417. The router unlinks each
  outcome file it consumes (outcomes.py), so the audit log is where the outcome
  is read. A test cross-checks each pinned name against the router's own module.
- A held request is `state_dir/<name>/held/req-<id>.json`: outbound.py:196-197.
- A reverse task with no edge gets `queued_for_human` with the reason
  `recipient_not_allowlisted`: outbound.py:801-803.
- The roster is `roster/roster.json`: roster.py:113.
- The container is named `amap-router-local`: docker/run.sh:30.
- The router writes its log to stderr: router/__main__.py:188-190.

amap-connector-claude:

- `inbox-submit submit` prints `Submitted request <id> to the relay drop-box`:
  bin/inbox-submit:833-849. `submit-result` is bin/inbox-submit:855-860, and it
  reads the drop-box named by `OUTBOX_DIR` (bin/inbox-submit:109-120).

Lane names and leaves come from the router (`router_config`). A leaf is spelled
only as the second argument of `_leaf`, which refuses at import a name the
router does not list for that lane.

Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.util
import json
import os
import platform
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Mapping, NamedTuple,
                    Optional, Sequence, Tuple, TypeVar)

from gateway import gateway_toml_path, setting_lines  # noqa: F401

import l1_kit
import membership
import policy
import provider_profile
import render
import router_config
import router_link

T = TypeVar("T")

PROG = l1_kit.PROG
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CHECKS = 3

REPO = Path(__file__).absolute().parent
FLEET_FILE = REPO / "examples" / "fleet.json"
RUNBOOK = REPO / "docs" / "L1-RUNBOOK.md"
IMAGE = "amap-openshell-agent"
IMAGE_LABEL_VERSION = "amap-openshell.claude-code-version"
IMAGE_LABEL_RUN_AS = "amap-openshell.run-as"
ROUTER_CONTAINER = "amap-router-local"
CRASHED_STATES = ("restarting", "exited", "dead")   # docker's State names
LOG_TAIL_LINES = 50
PROBE_NAME = "amap-l1-probe"
# The container label OpenShell puts on a sandbox's container
# (crates/openshell-core/src/driver_utils.rs:16-31). It is spelled in two parts
# so that no text of this file reads as a host name.
SANDBOX_ID_LABEL = "openshell" ".ai/sandbox-id"
GATEWAY_PORT = 17670
GATEWAY_HOST = "127.0.0.1"

STEPS = tuple(range(10))
GATE_STEPS = (0, 2)
POSTURE_GATED = (3, 4, 5, 6, 7, 8)
STEP_TITLES = {
    0: "prerequisites and D6's posture",
    1: "build the image",
    2: "check the gateway",
    3: "prepare the host and import the provider profile",
    4: "create each sandbox",
    5: "record each sandbox's ID",
    6: "build and start the router",
    7: "delegate from the sender to the receiver",
    8: "check the pass criteria and settle the unknowns",
    9: "draft the report",
}

REQUIRED_VARIABLES = ("AMAP_OPENSHELL_HOME", "CLAUDE_CODE_VERSION")
SPEC_VARIABLE = "AMAP_SPEC_DIR"
SPEC_DIR_NAMES = ("amap-spec", ".amap-spec")
SPEC_CONFIRM = "fixtures/validate.py"
# Where the sibling search starts; None is this repository. Tests point it at
# a layout of their own.
SIBLING_SEARCH_START: Optional[Path] = None
KEY_VARIABLE = "ANTHROPIC_API_KEY"
TOKEN_VARIABLE = "CLAUDE_CODE_MESSAGING_TOKEN"
REDACTED = "[REDACTED]"

EVIDENCE_DIRNAME = "evidence"
RUNNER_LOG = "runner.log"
REPORT_NAME = "POC-REPORT.md"
DELEGATION_NAME = "delegation.json"
CHECKS_NAME = "checks.json"
FACTS_NAME = "facts.json"
PROBE_RECORD_NAME = "probe.json"
PROBE_POLICY_NAME = "amap-l1-probe.yaml"
EVIDENCE_DIR_MODE = 0o700
EVIDENCE_FILE_MODE = 0o600
OUTPUT_CAP = 1024 * 1024

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
# interceptor/rule.py REASON_PREFIX; a test keeps the two equal.
INTERCEPTOR_PREFIX = "amap-openshell interceptor: "
SKIPPED = "SKIPPED"
DONE = "DONE"
DRY = "DRY RUN"

_rc = router_link.router_config()
LANE_INBOX, LANE_PEER, LANE_OUTBOX = _rc.LANE_INBOX, _rc.LANE_PEER, _rc.LANE_OUTBOX


class RunnerError(Exception):
    """The runner refuses, or is misused."""


def _leaf(lane: str, name: str) -> str:
    """A leaf of `lane`, spelled here only as this function's second argument.
    The router exports the leaves as tuples, not as names."""
    if name not in router_config.LANE_LEAVES[lane]:
        raise RunnerError(f"the router has no leaf {name!r} in the {lane} lane")
    return name


NOTICES = _leaf(LANE_PEER, "notices")
MESSAGES = _leaf(LANE_PEER, "messages")
RESULTS = _leaf(LANE_OUTBOX, "results")
PROCESSED = _leaf(LANE_OUTBOX, "processed")

# Router-private layout, pinned: tests cross-check each against router.audit,
# router.roster and router.outbound.
AUDIT_DIRNAME = "audit"
AUDIT_FILENAME = "log.jsonl"
HELD_DIRNAME = "held"
ROSTER_FILENAME = "roster.json"
EVENT_NOTICE_PLACED = "peer_notice_placed"
EVENT_OUTCOME = "outcome_consumed"
REASON_NOT_ALLOWLISTED = "recipient_not_allowlisted"

if "inbox-submit" not in l1_kit.CONNECTOR_MCP_BINARIES:
    raise RunnerError("the payload no longer carries inbox-submit")
INBOX_SUBMIT = posixpath.join(render.PAYLOAD_TARGET, l1_kit.PAYLOAD_BIN_DIRNAME,
                              "inbox-submit")
LISTER = posixpath.join(render.PAYLOAD_TARGET, l1_kit.SESSION_LISTER_NAME)
PAYLOAD_SETTINGS = posixpath.join(render.PAYLOAD_TARGET, render.SETTINGS_NAME)
CLAUDE_DIR = posixpath.join(render.WORKDIR, ".claude")
REFUSE_FILE = posixpath.join(CLAUDE_DIR, "settings.local.json")
REFUSE_TEXT = json.dumps({"crossSessionInbound": "refuse"})
DELEGATION_SUBJECT = "L1 delegation: list your workspace"
NEGATIVE_SUBJECT = "L1 negative case: refusing receiver"
REVERSE_SUBJECT = "L1 reverse task: never granted"
DELEGATION_BODY = (
    "Please list the files in your workspace and send the list back as a reply "
    "to this message: use inbox-submit's submit tool with reply_to_message_id "
    "set to this message's id.")
NEGATIVE_BODY = ("Please list the files in your workspace and reply. This "
                 "message tests a receiver that refuses cross-session messages.")
REVERSE_BODY = ("Please list the files in your workspace. This message tests a "
                "task that was never granted.")
MAIL_PROBE_URL = "https://mail.example.org"
MAIL_PROBE_HOST = "mail.example.org"
SUBMITTED_RE = re.compile(r"^Submitted request (\S+) to the relay drop-box",
                          re.M)

DELEGATION_STATE = f"step-7/{DELEGATION_NAME}"
CHECKS_STATE = f"step-8/{CHECKS_NAME}"
FACTS_STATE = f"step-0/{FACTS_NAME}"
PROBE_STATE = f"step-8/{PROBE_RECORD_NAME}"
REPORT_RELPATH = REPORT_NAME

# --- probes run inside a sandbox ---------------------------------------------

SNIPPET_MARK = "# amap-openshell l1-run: "

SESSION_PROBE = '''# amap-openshell l1-run: session-probe
import importlib.machinery, importlib.util, os, sys
sys.dont_write_bytecode = True
lister_path, home = sys.argv[1], sys.argv[2]
loader = importlib.machinery.SourceFileLoader("openshell_sessions", lister_path)
spec = importlib.util.spec_from_loader("openshell_sessions", loader)
lister = importlib.util.module_from_spec(spec)
loader.exec_module(lister)
table = lister.process_table(lister.PROC)
start = None
for pid in sorted(table):
    if lister.runs(lister.PROC, table, pid, lister.MAIN):
        start = pid
        break
if start is None:
    sys.stderr.write("no process runs %s\\n" % lister.MAIN)
    sys.exit(3)
for row in lister.rows(lister.PROC, home, start):
    sys.stdout.write("\\t".join(row) + "\\n")
'''

WRITE_PROBE = '''# amap-openshell l1-run: write-probe
import errno, os, sys
path, mode = sys.argv[1], sys.argv[2]
flags = os.O_WRONLY | (os.O_CREAT if mode == "create" else os.O_APPEND)
try:
    fd = os.open(path, flags, 0o600)
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
else:
    os.close(fd)
    print("OPENED")
'''

NETWORK_PROBE = '''# amap-openshell l1-run: network-probe
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=15)
except Exception as e:
    print("DENIED " + type(e).__name__)
    sys.exit(1)
print("REACHED")
'''

IDS_PROBE = '''# amap-openshell l1-run: ids-probe
import os
print("%d:%d" % (os.getuid(), os.getgid()))
'''

REFUSE_STATE = '''# amap-openshell l1-run: refuse-state
import sys
path, text = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        got = fh.read()
except FileNotFoundError:
    print("absent")
else:
    print("ours" if got == text else "other")
'''

WRITE_REFUSE = '''# amap-openshell l1-run: write-refuse
import os, sys
path, text = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("exists")
    sys.exit(1)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
print("written")
'''

REMOVE_REFUSE = '''# amap-openshell l1-run: remove-refuse
import os, sys
path, text = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        got = fh.read()
except FileNotFoundError:
    print("absent")
else:
    if got != text:
        print("other")
        sys.exit(1)
    os.unlink(path)
    print("removed")
'''

HOME_WRITE_PROBE = '''# amap-openshell l1-run: home-write-probe
import errno, os, sys
path = os.path.join(sys.argv[1], ".amap-l1-probe")
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as e:
    print(errno.errorcode.get(e.errno, str(e.errno)))
else:
    os.close(fd)
    os.unlink(path)
    print("writable")
'''


# --- types -------------------------------------------------------------------

class Settings(NamedTuple):
    poll_interval: float = 5.0
    command_timeout: float = 300.0
    build_timeout: float = 3600.0
    create_timeout: float = 900.0
    exec_timeout: int = 120
    ready_timeout: float = 600.0
    stop_timeout: float = 300.0
    router_timeout: float = 180.0
    delivery_timeout: float = 900.0
    reply_timeout: float = 1800.0
    log_timeout: float = 60.0


class Result(NamedTuple):
    argv: Tuple[str, ...]
    code: Optional[int]
    stdout: str
    stderr: str
    started: str
    seconds: float
    timed_out: bool
    record: str   # the evidence file, relative to evidence/


class StepResult(NamedTuple):
    outcome: str   # PASS | FAIL | SKIPPED (steps 0-7); DONE | FAIL | SKIPPED (8, 9)
    notes: Tuple[str, ...]


class CheckOutcome(NamedTuple):
    outcome: str                # PASS | FAIL | UNKNOWN
    reason: str                 # one line; required for FAIL and UNKNOWN
    facts: Tuple[str, ...]      # report evidence lines
    evidence: Tuple[str, ...]   # evidence files, relative to evidence/


class Check(NamedTuple):
    heading: str    # "Pass criterion 1" ... "Unknown 7"
    title: str      # the report template's title
    run: Optional[Callable[["Ctx", "CheckRun"], CheckOutcome]]
    unknown_reason: Optional[str]   # exactly one of run / unknown_reason is set


class Delegation(NamedTuple):
    subject: str
    sender: str
    receiver: str
    req_id: str
    notice_id: Optional[str]
    outcome: Optional[str]
    reply_notice_id: Optional[str]
    state: str   # planted | placed | delivered | complete


Cmd = Tuple[str, List[str]]   # (label, argv)


def ph(text: str) -> str:
    """A placeholder for a value a dry run cannot know."""
    return f"<{text}>"


# --- redaction, evidence and the executor ------------------------------------

_PATTERNS = (
    re.compile(r"(" + TOKEN_VARIABLE + r"[\"']?\s*[=:]\s*[\"']?)[^\s\"',}]+"),
    re.compile(r"(\"peerToken\"\s*:\s*\")[^\"]*"),
    re.compile(r"(" + KEY_VARIABLE + r"[\"']?\s*[=:]\s*[\"']?)[^\s\"',}]+"),
)


def secret_values(env: Mapping[str, str]) -> List[str]:
    """The values the runner must never write: the provider key, its last 20
    characters (a suffix is enough to recognise it), and a messaging token if
    the environment carries one."""
    out: List[str] = []
    key = env.get(KEY_VARIABLE) or ""
    if key:
        out.append(key)
        if len(key) > 20:
            out.append(key[-20:])
    token = env.get(TOKEN_VARIABLE) or ""
    if token:
        out.append(token)
    return out


class Redactor:
    def __init__(self, secrets: Iterable[str]) -> None:
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def exact(self, s: str) -> str:
        for secret in self.secrets:
            s = s.replace(secret, REDACTED)
        return s

    def text(self, s: str) -> str:
        s = self.exact(s)
        for pattern in _PATTERNS:
            s = pattern.sub(lambda m: m.group(1) + REDACTED, s)
        return s

    def doc(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {self.text(str(k)): self.doc(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.doc(v) for v in obj]
        return obj


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s[:60].strip("-") or "x"


def _mkdir(path: str, mode: int) -> None:
    """A real directory at `path`, with exactly `mode`. A symlink is refused."""
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RunnerError(f"{path} is not a real directory")
    os.chmod(path, mode)


class Evidence:
    """`$AMAP_OPENSHELL_HOME/evidence/`. Command records are append-only: each
    is created with O_EXCL and never rewritten. Four state files
    (`delegation.json`, `checks.json`, `probe.json`, `facts.json`) and the
    probe's policy are replaced atomically while their step runs. The log is
    appended to. Every string written passes through the redactor."""

    def __init__(self, home: str, run_id: str, redactor: Redactor) -> None:
        self.home = home
        self.run_id = run_id
        self.redactor = redactor
        self.root = posixpath.join(home, EVIDENCE_DIRNAME)
        self._seq: Dict[int, int] = {}

    def ensure(self) -> None:
        if not os.path.isdir(self.home):
            os.mkdir(self.home)
            os.chmod(self.home, l1_kit.DIR_MODE)
        _mkdir(self.root, EVIDENCE_DIR_MODE)

    def path(self, relpath: str) -> str:
        return posixpath.join(self.root, relpath)

    def step_dir(self, n: int) -> str:
        d = self.path(f"step-{n}")
        _mkdir(d, EVIDENCE_DIR_MODE)
        return d

    def _clean(self, text: str) -> str:
        return self.redactor.exact(text)

    def record(self, step: int, label: str, doc: Mapping[str, Any],
               check: Optional[str] = None) -> str:
        directory = self.step_dir(step)
        body = dict(doc)
        if check is not None:
            body.setdefault("check", check)
        text = self._clean(json.dumps(self.redactor.doc(body), indent=2) + "\n")
        slug = _slug(label)
        while True:
            self._seq[step] = self._seq.get(step, 0) + 1
            name = f"{self.run_id}-{self._seq[step]:03d}-{slug}.json"
            path = posixpath.join(directory, name)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             EVIDENCE_FILE_MODE)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            return f"step-{step}/{name}"

    def step_result(self, n: int, title: str, result: StepResult) -> None:
        directory = self.step_dir(n)
        doc = {"step": n, "title": title, "run": self.run_id,
               "outcome": result.outcome, "notes": list(result.notes)}
        text = self._clean(json.dumps(self.redactor.doc(doc), indent=2) + "\n")
        self._replace(posixpath.join(directory, f"{self.run_id}-result.json"),
                      text)

    def write_state(self, relpath: str, doc: Any) -> None:
        self.write_text(relpath, json.dumps(self.redactor.doc(doc), indent=2)
                        + "\n")

    def write_text(self, relpath: str, text: str) -> str:
        """Replace `relpath` with `text` all at once; an identical file is left
        alone. Returns the file's path."""
        step = re.match(r"step-(\d+)/", relpath)
        if step:
            self.step_dir(int(step.group(1)))
        path = self.path(relpath)
        text = self._clean(text)
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == text:
                    return path
        except FileNotFoundError:
            pass
        self._replace(path, text)
        return path

    def _replace(self, path: str, text: str) -> None:
        parent = posixpath.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, EVIDENCE_FILE_MODE)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def read_state(self, relpath: str) -> Optional[dict]:
        try:
            with open(self.path(relpath), encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise RunnerError(
                f"evidence/{relpath} cannot be read ({e}); remove it to "
                f"go on") from e
        if not isinstance(doc, dict):
            raise RunnerError(f"evidence/{relpath} is not a JSON object; "
                              f"remove it to go on")
        return doc

    def write_new(self, relpath: str, text: str) -> bool:
        """Create `relpath` with O_EXCL. False when it already exists."""
        path = self.path(relpath)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         EVIDENCE_FILE_MODE)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self._clean(text))
        return True

    def exists(self, relpath: str) -> bool:
        return os.path.exists(self.path(relpath))

    def log(self, line: str) -> None:
        fd = os.open(self.path(RUNNER_LOG),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     EVIDENCE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(self._clean(line) + "\n")


def unique_run_id(home: str, base: str) -> str:
    """`base`, or `base-2`, `base-3`, ... when an earlier run of the same second
    left its result in `step-0`."""
    directory = posixpath.join(home, EVIDENCE_DIRNAME, "step-0")
    run_id, n = base, 1
    while os.path.exists(posixpath.join(directory, f"{run_id}-result.json")):
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def display_argv(argv: Sequence[str]) -> str:
    """The command as one line. A Python snippet is shown by name."""
    words = []
    for word in argv:
        if word.startswith(SNIPPET_MARK):
            words.append("snippet:" + word.split("\n", 1)[0][len(SNIPPET_MARK):])
        else:
            words.append(word)
    return shlex.join(words)


def _cap(text: str) -> str:
    if len(text) > OUTPUT_CAP:
        return "[truncated]\n" + text[-OUTPUT_CAP:]
    return text


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _stamp(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Executor:
    """The one place a command runs. It records each command's argv, output,
    exit status and time. A dry run has nothing to execute, so it refuses."""

    def __init__(self, env: Mapping[str, str], apply: bool,
                 evidence: Optional[Evidence], redactor: Redactor, out: Any,
                 clock: Callable[[], float],
                 now: Callable[[], datetime.datetime] = _utc_now) -> None:
        self.env = dict(env)
        self.apply = apply
        self.evidence = evidence
        self.redactor = redactor
        self.out = out
        self.clock = clock
        self.now = now

    def _say(self, line: str) -> None:
        text = self.redactor.text(line)
        print(text, file=self.out)
        if self.evidence is not None:
            self.evidence.log(text)

    def run(self, step: int, label: str, argv: Sequence[str], *,
            timeout: float, input_text: Optional[str] = None,
            cwd: Optional[str] = None, check: Optional[str] = None,
            with_key: bool = False) -> Result:
        if not self.apply:
            raise RunnerError("a dry run executes nothing")
        if self.evidence is None:
            raise RunnerError("there is no evidence directory to record into")
        argv = list(argv)
        # The provider key reaches only the commands that create a sandbox.
        child_env = {k: v for k, v in self.env.items()
                     if with_key or k != KEY_VARIABLE}
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._say("+ " + display_argv(argv))
        started = self.now()
        t0 = self.clock()
        code: Optional[int]
        timed_out = False
        try:
            proc = subprocess.run(
                argv, input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                capture_output=True, text=True, errors="replace",
                timeout=timeout, cwd=cwd, env=child_env)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            code, timed_out = None, True
            out, err = _as_text(e.stdout), _as_text(e.stderr)
        except OSError as e:
            code, out, err = None, "", str(e)
        seconds = round(self.clock() - t0, 3)
        out, err = _cap(out), _cap(err)
        record = self.evidence.record(step, label, {
            "kind": "command", "step": step, "check": check, "label": label,
            "argv": argv, "cwd": cwd, "started": _stamp(started),
            "ended": _stamp(self.now()), "seconds": seconds, "exit": code,
            "timed_out": timed_out, "stdout": out, "stderr": err})
        return Result(tuple(argv), code, out, err, _stamp(started), seconds,
                      timed_out, record)


# --- context and inputs ------------------------------------------------------

class SpecNotFound(RunnerError):
    """The amap-spec directory is missing."""


def find_spec(env: Optional[Mapping[str, str]] = None,
              start: Optional[Path] = None) -> Path:
    """The amap-spec directory, found as `router_link.find_router` finds the
    router. When `$AMAP_SPEC_DIR` is set and not empty it is the only place
    searched. Otherwise the nearest `<ancestor>/amap-spec` or
    `<ancestor>/.amap-spec` that contains `fixtures/validate.py`."""
    env = os.environ if env is None else env
    value = env.get(SPEC_VARIABLE)
    if value:
        cand = Path(value).absolute()
        if (cand / SPEC_CONFIRM).is_file():
            return cand
        raise SpecNotFound(
            f"${SPEC_VARIABLE}={value} does not contain {SPEC_CONFIRM}; it is "
            f"the only place searched because the variable names it. Point it "
            f"at the amap-spec directory, or unset it to search beside this "
            f"repository.")
    here = REPO if start is None else Path(start).absolute()
    for ancestor in [here, *here.parents]:
        for name in SPEC_DIR_NAMES:
            cand = ancestor / name
            if (cand / SPEC_CONFIRM).is_file():
                return cand
    raise SpecNotFound(
        f"cannot find the amap-spec directory (amap-spec or .amap-spec, "
        f"confirmed by {SPEC_CONFIRM}) beside any ancestor of {here}. Check it "
        f"out beside this repository, or set ${SPEC_VARIABLE}.")


class SiblingSource(NamedTuple):
    label: str
    variable: str
    find: Callable[..., Path]
    error: type


SIBLING_SOURCES = (
    SiblingSource("amap-router-local", router_link.ROUTER_VARIABLE,
                  router_link.find_router, router_link.RouterNotFound),
    SiblingSource(l1_kit.CONNECTOR_DIR_NAME, l1_kit.CONNECTOR_VARIABLE,
                  l1_kit.find_connector, l1_kit.KitError),
    SiblingSource("amap-spec", SPEC_VARIABLE, find_spec, SpecNotFound),
)


Finding = Tuple[bool, str]


def find_siblings(env: Mapping[str, str]
                  ) -> Tuple[Dict[str, Path], List[Finding]]:
    """The three checkouts l1-run reads, found as the other verbs and the
    tests find them, and one finding for each."""
    found: Dict[str, Path] = {}
    findings: List[Finding] = []
    for src in SIBLING_SOURCES:
        try:
            path = src.find(env, SIBLING_SEARCH_START)
        except src.error as e:
            findings.append((False, f"{src.label} is not found: {e}"))
            continue
        found[src.label] = path
        how = (f"named by ${src.variable}" if env.get(src.variable)
               else "found beside an ancestor of this repository")
        findings.append((True, f"{src.label} found at {path} ({how})"))
    return found, findings


def known_repos(env: Mapping[str, str]) -> Dict[str, Path]:
    """The checkouts `$AMAP_OPENSHELL_HOME` must not overlap."""
    return {
        "amap-deploy-openshell": REPO,
        "amap-router-local": router_link.find_router(
            env, SIBLING_SEARCH_START),
        l1_kit.CONNECTOR_DIR_NAME: l1_kit.find_connector(
            env, SIBLING_SEARCH_START),
        "amap-deploy-sandy": policy.find_sandy(env),
    }


def gateway_toml_findings(path: Optional[str]) -> List[Finding]:
    """The static part of D6's posture, read from the gateway's configuration
    file (docs/L1-RUNBOOK.md step 2)."""
    if not path:
        return [(False, "the gateway configuration file cannot be located: "
                        "neither XDG_CONFIG_HOME nor HOME is set (D6)")]
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return [(False, f"the gateway configuration file cannot be read ({e}) "
                        f"(D6)")]
    have = setting_lines(text)
    findings: List[Finding] = [(True, "the gateway configuration file exists")]
    missing = [line for line in setting_lines(l1_kit.GATEWAY_FRAGMENT)
               if line not in have]
    if missing:
        findings.append((False, "gateway.toml lacks the fragment's setting(s): "
                                + "; ".join(missing) + " (D6, DESIGN.md "
                                "section 6)"))
    else:
        findings.append((True, "gateway.toml has every setting of the "
                               "gateway fragment (D6, DESIGN.md section 6)"))
    binds = [line for line in have if "bind_address" in line]
    findings.append((not binds, "gateway.toml has no bind_address: the gateway "
                                "keeps its built-in loopback listener (D6)"
                     if not binds else "gateway.toml sets bind_address, so the "
                                       "listener is not the built-in loopback "
                                       "one (D6)"))
    oidc = [line for line in have if "oidc" in line.lower()]
    findings.append((not oidc, "gateway.toml has no OIDC setting (D6)"
                     if not oidc else "gateway.toml mentions OIDC, which the "
                                      "accepted posture excludes (D6)"))
    return findings


def load_inputs(env: Mapping[str, str], ids: Tuple[int, int]
                ) -> Tuple[Optional[dict], List[Finding]]:
    """Step 0's in-process checks. Every finding is `(ok, text)`, and no text
    holds a secret value. The dict is None unless every finding is ok."""
    findings: List[Finding] = []

    def add(ok: bool, text: str) -> bool:
        findings.append((ok, text))
        return ok

    for var in REQUIRED_VARIABLES:
        value = env.get(var)
        add(bool(value), f"{var} is set" if value else f"{var} is not set, "
                                                        f"or is empty")
    siblings, sib_findings = find_siblings(env)
    findings.extend(sib_findings)
    add(bool(env.get(KEY_VARIABLE)),
        f"{KEY_VARIABLE} is set" if env.get(KEY_VARIABLE)
        else f"{KEY_VARIABLE} is not set, or is empty")
    version = env.get("CLAUDE_CODE_VERSION") or ""
    if version:
        add(not any(c.isspace() for c in version),
            "CLAUDE_CODE_VERSION has no whitespace"
            if not any(c.isspace() for c in version)
            else "CLAUDE_CODE_VERSION has whitespace")

    home = env.get("AMAP_OPENSHELL_HOME") or ""
    if home:
        reason = render.home_problem(home)
        add(reason is None, "AMAP_OPENSHELL_HOME is a usable host path"
            if reason is None else reason)
        try:
            overlap = l1_kit.home_overlap(home, known_repos(env))
        except (l1_kit.KitError, router_link.RouterNotFound,
                policy.SandyNotFound) as e:
            add(False, f"the checkouts cannot be located: {e}")
        else:
            add(overlap is None, "AMAP_OPENSHELL_HOME overlaps no checkout"
                if overlap is None else overlap)
        add(os.path.isdir(posixpath.dirname(home)),
            "the parent of AMAP_OPENSHELL_HOME is a directory"
            if os.path.isdir(posixpath.dirname(home))
            else "the parent of AMAP_OPENSHELL_HOME is not a directory")

    run_as: Optional[render.RunAs] = None
    try:
        run_as = render.parse_run_as(f"{ids[0]}:{ids[1]}")
        add(True, "the runner runs as a non-root uid:gid (D7)")
    except render.RenderError:
        add(False, "the runner refuses root (D7): run it as the operator's "
                   "own uid:gid")

    fleet: Optional[dict] = None
    members: List[str] = []
    sender = receiver = None
    addresses: Dict[str, str] = {}
    # The installed fleet.json once `prepare` or `install` has seeded it: since
    # D21 its fleet_domain is derived per host, so the template's addresses
    # are not the fleet's. The template only before anything is installed.
    installed = l1_kit.fleet_json(home) if home else ""
    fleet_file = installed if installed and os.path.isfile(installed) \
        else str(FLEET_FILE)
    try:
        fleet = policy.load_fleet(fleet_file)
        members = policy.named_instances(fleet)
        edges: List[Tuple[str, str]] = []
        if len(members) == 2:
            graph = policy.resolve(fleet, members).task_graph
            edges = [(s, r) for r, senders in sorted(graph.items())
                     for s in senders]
        fine = len(members) == 2 and len(edges) == 1
        add(fine, "the fleet has two members and one edge: one may task the "
                  "other, and not the reverse" if fine
            else "the fleet must name exactly two members and grant exactly "
                 "one edge")
        if fine:
            sender, receiver = edges[0]
            addresses = policy.addresses(fleet, members)
    except (policy.PolicyError, membership.MembershipError, OSError) as e:
        add(False, f"the fleet cannot be loaded: {e}")

    toml = gateway_toml_path(env)
    findings.extend(gateway_toml_findings(toml))

    if not all(ok for ok, _ in findings):
        return None, findings
    assert run_as is not None and fleet is not None and toml is not None
    inputs = {
        "home": home,
        "router_repo": siblings["amap-router-local"],
        "connector_repo": siblings[l1_kit.CONNECTOR_DIR_NAME],
        "spec_dir": str(siblings["amap-spec"]),
        "claude_code_version": version,
        "run_as": run_as,
        "run_as_text": f"{run_as.uid}:{run_as.gid}",
        "fleet": fleet,
        "workspace": policy.workspace_of(fleet),
        "members": tuple(members),
        "sender": sender,
        "receiver": receiver,
        "addresses": addresses,
        "gateway_toml": toml,
    }
    return inputs, findings


class Ctx:
    """Everything a step needs. Built by the runner; step 0 fills in the
    inputs."""

    def __init__(self, env: Mapping[str, str], apply: bool, settings: Settings,
                 out: Any, clock: Callable[[], float],
                 sleep: Callable[[float], None],
                 now: Callable[[], datetime.datetime],
                 ids: Callable[[], Tuple[int, int]]) -> None:
        self.env = dict(env)
        self.apply = apply
        self.settings = settings
        self.out = out
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.uid_gid = ids
        self.redactor = Redactor(secret_values(self.env))
        self.evidence: Optional[Evidence] = None
        self.executor = Executor(self.env, False, None, self.redactor, out,
                                 clock, now)
        self.ids: Dict[str, str] = {}
        self.checks: List[dict] = []
        self.home = ""
        self.router_repo = Path("/")
        self.connector_repo = Path("/")
        self.spec_dir = ""
        self.claude_code_version = ""
        self.run_as = render.RunAs(1, 1)
        self.run_as_text = ""
        self.fleet: dict = {}
        self.workspace = ""
        self.members: Tuple[str, ...] = ()
        self.sender = ""
        self.receiver = ""
        self.addresses: Dict[str, str] = {}
        self.gateway_toml = ""
        self.image = IMAGE
        self.host = render.Host("/", IMAGE, render.RunAs(1, 1), False)

    def adopt(self, inputs: Mapping[str, Any]) -> None:
        for key, value in inputs.items():
            setattr(self, key, value)
        self.host = render.Host(self.home, self.image, self.run_as, False)

    def attach_evidence(self) -> None:
        base = self.now().strftime("%Y%m%dT%H%M%SZ")
        self.evidence = Evidence(self.home, unique_run_id(self.home, base),
                                 self.redactor)
        self.evidence.ensure()
        self.executor = Executor(self.env, self.apply, self.evidence,
                                 self.redactor, self.out, self.clock, self.now)

    def say(self, line: str = "") -> None:
        text = self.redactor.text(line)
        print(text, file=self.out)
        if self.apply and self.evidence is not None:
            self.evidence.log(text)

    def stamp(self) -> str:
        return _stamp(self.now())


Go = Callable[..., Result]


def stepgo(ctx: Ctx, step: int) -> Go:
    """`go(cmd, timeout=None, ...)`: run a `(label, argv)` in `step`."""
    def go(cmd: Cmd, timeout: Optional[float] = None, **kw: Any) -> Result:
        return ctx.executor.run(
            step, cmd[0], cmd[1],
            timeout=ctx.settings.command_timeout if timeout is None else timeout,
            **kw)
    return go


def observe(ctx: Ctx, step: int, label: str, source: str, observed: Any,
            check: Optional[str] = None) -> str:
    """Record a host-side read (an audit match, a host listing, an `os.stat`)."""
    assert ctx.evidence is not None
    return ctx.evidence.record(step, label, {
        "kind": "observation", "step": step, "check": check, "label": label,
        "source": source, "observed": observed, "time": ctx.stamp()}, check)


# --- command builders --------------------------------------------------------

def openshell(ctx: Ctx, *words: str) -> List[str]:
    return ["openshell", "--workspace", ctx.workspace, *words]


def exec_argv(ctx: Ctx, name: str, command: Sequence[str],
              env_pairs: Sequence[str] = ()) -> List[str]:
    """`sandbox exec` (OpenShell main@acbac9c:crates/openshell-cli/src/main.rs:
    1681-1727): no terminal, no login shell, and a timeout."""
    argv = openshell(ctx, "sandbox", "exec", "--name", name, "--no-tty",
                     "--no-login-shell", "--timeout",
                     str(ctx.settings.exec_timeout))
    for pair in env_pairs:
        argv += ["--env", pair]
    return argv + ["--", *command]


def py(ctx: Ctx, name: str, snippet: str, *args: str) -> List[str]:
    return exec_argv(ctx, name, ["python3", "-c", snippet, *args])


def get_json_argv(ctx: Ctx, name: str) -> List[str]:
    return openshell(ctx, "sandbox", "get", name, "--output", "json")


def policy_only_argv(ctx: Ctx, name: str) -> List[str]:
    return openshell(ctx, "sandbox", "get", name, "--policy-only")


def list_argv(ctx: Ctx) -> List[str]:
    return ["openshell", "sandbox", "list", "--all-workspaces", "--names"]


def create_argv_from_file(ctx: Ctx, name: str) -> List[str]:
    """The kit's create command for `name`, as the kit wrote it."""
    path = l1_kit.command_file(ctx.home, name)
    try:
        with open(path, encoding="utf-8") as fh:
            argv = shlex.split(fh.read())
    except (OSError, ValueError) as e:
        raise RunnerError(f"cannot read the create command {path}: {e}") from e
    missing = [f for f in render.UNATTENDED_FLAGS if f not in argv]
    if not argv or argv[0] != "openshell" or missing:
        raise RunnerError(f"{path} is not an unattended `openshell` create "
                          f"command" + (f" (missing {', '.join(missing)})"
                                        if missing else ""))
    return argv


def kit_argv(ctx: Ctx, *words: str, apply: bool) -> List[str]:
    return [sys.executable, str(REPO / "amap-openshell.py"), "l1-kit", *words,
            *(["--apply"] if apply else [])]


def build_argv(ctx: Ctx) -> List[str]:
    return ["docker", "build",
            "--build-arg", f"CLAUDE_CODE_VERSION={ctx.claude_code_version}",
            "--build-arg", f"SANDBOX_UID={ctx.run_as.uid}",
            "--build-arg", f"SANDBOX_GID={ctx.run_as.gid}",
            "--label", f"{IMAGE_LABEL_VERSION}={ctx.claude_code_version}",
            "--label", f"{IMAGE_LABEL_RUN_AS}={ctx.run_as_text}",
            "-t", ctx.image, "image/"]


def _drop_box_env() -> List[str]:
    return [f"OUTBOX_DIR={render.lane_target(LANE_OUTBOX)}"]


def submit_argv(ctx: Ctx, frm: str, to: str, subject: str, body: str
                ) -> List[str]:
    """The connector's own CLI, in the sandbox, against the drop-box the agent's
    tool writes."""
    return exec_argv(ctx, frm, ["python3", INBOX_SUBMIT, "submit", "--to",
                                ctx.addresses[to], "--subject", subject,
                                "--body", body], _drop_box_env())


def submit_result_argv(ctx: Ctx, member: str, req_id: str) -> List[str]:
    return exec_argv(ctx, member, ["python3", INBOX_SUBMIT, "submit-result",
                                   req_id], _drop_box_env())


def router_script(ctx: Ctx, name: str) -> str:
    return str(ctx.router_repo / "docker" / name)


def router_json(ctx: Ctx) -> str:
    return router_config.router_json_path(ctx.home)


# Each `cmd_*` is a `(label, argv)`. The dry run prints these, and `--apply`
# runs the same ones, so the two cannot name a command differently.

def cmd_openshell_version() -> Cmd:
    return "openshell-version", ["openshell", "--version"]


def cmd_docker_version() -> Cmd:
    return "docker-version", ["docker", "version", "--format",
                              "{{.Server.Version}}"]


def cmd_image_inspect(image: str = IMAGE) -> Cmd:
    return "image-inspect", ["docker", "image", "inspect", "--format",
                             "{{json .Config.Labels}}", image]


def cmd_build(ctx: Ctx) -> Cmd:
    return "docker-build", build_argv(ctx)


def cmd_status() -> Cmd:
    return "status", ["openshell", "status"]


def cmd_preflight(ctx: Ctx) -> Cmd:
    return "gateway-preflight", ["openshell-gateway", "config", "preflight",
                                 "--path", ctx.gateway_toml]


def cmd_listeners() -> Cmd:
    return "listeners", ["ss", "-ltnH"]


def cmd_list(ctx: Ctx) -> Cmd:
    return "sandbox-list", list_argv(ctx)


def cmd_prepare(ctx: Ctx, apply: bool) -> Cmd:
    return ("prepare-apply" if apply else "prepare-dry"), kit_argv(
        ctx, "prepare", "--home", ctx.home, "--fleet", str(FLEET_FILE),
        "--run-as", ctx.run_as_text, "--image", IMAGE, apply=apply)


def cmd_image_paths(image: str = IMAGE) -> Cmd:
    """Read `node`'s and `claude`'s real paths from the built image. The
    entrypoint is bypassed, and the probe needs no network."""
    return "image-paths", ["docker", "run", "--rm", "--network", "none",
                           "--entrypoint", "sh", image, "-c",
                           provider_profile.IMAGE_PATHS_SCRIPT]


def cmd_profile_kit(ctx: Ctx, binaries: Sequence[str], apply: bool) -> Cmd:
    return ("profile-apply" if apply else "profile-dry"), kit_argv(
        ctx, "profile", "--home", ctx.home,
        *[w for b in binaries for w in ("--binary", b)], apply=apply)


def profile_path(ctx: Ctx) -> str:
    return l1_kit.profile_file(ctx.home)


def cmd_profile_lint(ctx: Ctx) -> Cmd:
    return "profile-lint", openshell(ctx, "provider", "profile", "lint", "-f",
                                     profile_path(ctx))


def cmd_profile_list(ctx: Ctx, after: bool = False) -> Cmd:
    return ("profile-list-after" if after else "profile-list"), openshell(
        ctx, "provider", "list-profiles", "-o", "json")


def cmd_profile_import(ctx: Ctx) -> Cmd:
    return "profile-import", openshell(ctx, "provider", "profile", "import",
                                       "-f", profile_path(ctx))


def cmd_profile_update(ctx: Ctx, document_path: str) -> Cmd:
    return "profile-update", openshell(ctx, "provider", "profile", "update",
                                       provider_profile.PROFILE_ID, "-f",
                                       document_path)


def cmd_get(ctx: Ctx, name: str) -> Cmd:
    return f"get-{name}", get_json_argv(ctx, name)


def cmd_confirm_absent(ctx: Ctx, name: str) -> Cmd:
    return f"confirm-absent-{name}", list_argv(ctx)


def cmd_create(ctx: Ctx, name: str) -> Cmd:
    """The create command the kit wrote. Before `prepare` has run there is no
    file, so a dry run shows the rendering the kit would write."""
    path = l1_kit.command_file(ctx.home, name)
    if os.path.isfile(path):
        return f"create-{name}", create_argv_from_file(ctx, name)
    rendered = render.render_member(ctx.fleet, name, ctx.host,
                                    l1_kit.policy_file(ctx.home, name))
    return f"create-{name}", list(rendered.argv)


def cmd_ready(ctx: Ctx, name: str) -> Cmd:
    return f"ready-{name}", py(ctx, name, SESSION_PROBE, LISTER, render.WORKDIR)


def cmd_record(ctx: Ctx, name: str, sandbox_id: str, apply: bool) -> Cmd:
    return (f"record-apply-{name}" if apply else f"record-dry-{name}"), \
        kit_argv(ctx, "record", "--home", ctx.home, name, sandbox_id,
                 apply=apply)


def cmd_ps_router() -> Cmd:
    return "docker-ps", ["docker", "ps", "-a", "--filter",
                         f"name=^{ROUTER_CONTAINER}$", "--format",
                         "{{.Names}} {{.State}}"]


def cmd_ps_router_wait() -> Cmd:
    return "docker-ps-wait", cmd_ps_router()[1]


def cmd_router_exit_code() -> Cmd:
    return "router-exit-code", ["docker", "inspect", "--format",
                                "{{.State.ExitCode}}", ROUTER_CONTAINER]


def cmd_router_log_tail() -> Cmd:
    return "router-log-tail", ["docker", "logs", "--tail",
                               str(LOG_TAIL_LINES), ROUTER_CONTAINER]


def cmd_ps_router_after() -> Cmd:
    return "docker-ps-after", cmd_ps_router()[1]


def cmd_router_build(ctx: Ctx) -> Cmd:
    return "router-build", [router_script(ctx, "build.sh")]


def cmd_router_run(ctx: Ctx) -> Cmd:
    return "router-run", [router_script(ctx, "run.sh"), "--config",
                          router_json(ctx), "--detach"]


def cmd_router_logs() -> Cmd:
    return "router-logs", ["docker", "logs", ROUTER_CONTAINER]


def cmd_submit(ctx: Ctx, label: str, frm: str, to: str, subject: str,
               body: str) -> Cmd:
    return label, submit_argv(ctx, frm, to, subject, body)


def cmd_submit_result(ctx: Ctx, member: str, req_id: str) -> Cmd:
    return f"submit-result-{member}", submit_result_argv(ctx, member, req_id)


def cmd_stop(ctx: Ctx, name: str) -> Cmd:
    return f"stop-{name}", openshell(ctx, "sandbox", "stop", name)


def cmd_start(ctx: Ctx, name: str) -> Cmd:
    return f"start-{name}", openshell(ctx, "sandbox", "start", name)


def cmd_phase(ctx: Ctx, name: str) -> Cmd:
    return f"phase-{name}", get_json_argv(ctx, name)


def cmd_exec(ctx: Ctx, label: str, name: str, *command: str) -> Cmd:
    return label, exec_argv(ctx, name, list(command))


def cmd_py(ctx: Ctx, label: str, name: str, snippet: str, *args: str) -> Cmd:
    return label, py(ctx, name, snippet, *args)


# --- host readers ------------------------------------------------------------

def state_dir(ctx: Ctx) -> str:
    return router_config.default_state_dir(ctx.home)


def audit_path(ctx: Ctx, instance: str) -> str:
    return posixpath.join(state_dir(ctx), instance, AUDIT_DIRNAME,
                          AUDIT_FILENAME)


def audit_events(ctx: Ctx, instance: str) -> List[dict]:
    """The router's audit log for `instance`, one line at a time. A line that
    does not parse is kept as `{"_unparsed": line}`."""
    try:
        with open(audit_path(ctx, instance), encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    events: List[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            events.append({"_unparsed": line})
            continue
        events.append(doc if isinstance(doc, dict) else {"_unparsed": line})
    return events


def find_event(events: Sequence[dict], event: str, **fields: Any
               ) -> Optional[dict]:
    for doc in events:
        if doc.get("event") == event and all(doc.get(k) == v
                                             for k, v in fields.items()):
            return doc
    return None


def lane_host(ctx: Ctx, member: str, lane: str, *leaf: str) -> str:
    return posixpath.join(render.lane_dir(ctx.home, member, lane), *leaf)


def _regular_files(directory: str) -> List[str]:
    """The regular files of a directory, without a leading dot, sorted."""
    try:
        with os.scandir(directory) as it:
            return sorted(e.name for e in it if not e.name.startswith(".")
                          and e.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return []


def _rejected(ctx: Ctx, member: str, req_id: str) -> bool:
    """Whether the router's result for `req_id` is `rejected`."""
    path = lane_host(ctx, member, LANE_OUTBOX, RESULTS, f"{req_id}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("outcome") == "rejected"


def find_planted(ctx: Ctx, member: str, subject: str) -> Optional[str]:
    """The `req_id` of a request `member` already planted with `subject`, in its
    outbox or in the router's `processed` directory, that can still be
    resumed. One addressed to no current member's address, or one the router
    rejected, is not: live on the test host (2026-10-05) a request sent to the
    template's old domain was rejected `recipient_unknown`, and resuming it
    waited for a notice that could never come."""
    current = set(ctx.addresses.values())
    for directory in (lane_host(ctx, member, LANE_OUTBOX),
                      lane_host(ctx, member, LANE_OUTBOX, PROCESSED)):
        for name in _regular_files(directory):
            if not (name.startswith("req-") and name.endswith(".json")):
                continue
            try:
                with open(posixpath.join(directory, name),
                          encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            draft = doc.get("draft") if isinstance(doc, dict) else None
            if isinstance(draft, dict) and draft.get("subject") == subject:
                req_id = doc.get("req_id")
                to = draft.get("to")
                to = [to] if isinstance(to, str) else to
                if not (isinstance(to, list) and current & set(to)):
                    continue
                if isinstance(req_id, str) and req_id \
                        and not _rejected(ctx, member, req_id):
                    return req_id
    return None


def held_path(ctx: Ctx, member: str, req_id: str) -> str:
    return posixpath.join(state_dir(ctx), member, HELD_DIRNAME,
                          f"req-{req_id}.json")


def roster_path(ctx: Ctx) -> str:
    return posixpath.join(render.roster_dir(ctx.home), ROSTER_FILENAME)


def wait_until(ctx: Ctx, probe: Callable[[], Optional[T]],
               timeout: float) -> Optional[T]:
    """`probe()` until it returns something other than None, or `timeout`
    seconds pass on `ctx.clock`. It always probes once."""
    start = ctx.clock()
    while True:
        value = probe()
        if value is not None:
            return value
        if ctx.clock() - start >= timeout:
            return None
        ctx.sleep(ctx.settings.poll_interval)


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def recorded_ids(ctx: Ctx) -> Optional[Dict[str, str]]:
    """Every member's recorded ID, or None unless all of them are recorded."""
    members = membership.load(l1_kit.membership_json(ctx.home))
    if members is None:
        return None
    found = {m.name: m.id for m in members}
    if any(name not in found for name in ctx.members):
        return None
    return {name: found[name] for name in ctx.members}


def format_finding(ok: bool, text: str) -> str:
    return f"{'ok   ' if ok else 'FAIL '} {text}"


# --- sandboxes ---------------------------------------------------------------

def find_sandbox(ctx: Ctx, go: Go, name: str) -> Tuple[str, Optional[dict], str]:
    """`("present", <sandbox get json>, "")`, `("absent", None, "")` or
    `("unknown", None, <why>)`. A failed `get` is confirmed against the list
    before the sandbox is called absent."""
    r = go(cmd_get(ctx, name))
    if r.code == 0:
        doc = parse_json(r.stdout)
        if isinstance(doc, dict):
            return "present", doc, ""
        return "unknown", None, f"`sandbox get {name}` printed no JSON object"
    listing = go(cmd_confirm_absent(ctx, name))
    if listing.code != 0:
        return "unknown", None, (f"`sandbox get {name}` failed and the list "
                                 f"could not confirm it: "
                                 f"{_first_line(listing.stderr)}")
    names = {ln.strip() for ln in listing.stdout.splitlines() if ln.strip()}
    if f"{ctx.workspace}/{name}" in names:
        return "unknown", None, (f"{name} is listed but `sandbox get` failed: "
                                 f"{_first_line(r.stderr)}")
    return "absent", None, ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _all_lines(text: str) -> str:
    """Every non-blank line, joined with "; ": a diagnostic's detail follows
    its first line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "; ".join(lines) if lines else "no output"


def _last_line(text: str) -> Optional[str]:
    """The last non-blank line, stripped, or None."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return None


def identity_problem(ctx: Ctx, name: str, doc: Mapping[str, Any]
                     ) -> Optional[str]:
    """Why `doc` (a `sandbox get` document) is not the sandbox `name` of the
    fleet's workspace, or is not the one that was recorded."""
    if doc.get("name") != name or doc.get("workspace") != ctx.workspace:
        return (f"`sandbox get {name}` reports name {doc.get('name')!r} in "
                f"workspace {doc.get('workspace')!r}, not {name!r} in "
                f"{ctx.workspace!r}")
    sandbox_id = doc.get("id")
    reason = membership.id_problem(sandbox_id)
    if reason:
        return reason
    recorded = membership.load(l1_kit.membership_json(ctx.home)) or []
    known = membership.find(recorded, name)
    if known is not None and known.id != sandbox_id:
        return str(membership.IdMismatch(
            known, membership.Member(ctx.workspace, name, str(sandbox_id))))
    return None


def probe_rows(stdout: str) -> List[List[str]]:
    return [line.split("\t") for line in stdout.splitlines() if line.strip()]


def ready_row(rows: Sequence[Sequence[str]]) -> Optional[List[str]]:
    """The one `claude` row whose socket and record are both real."""
    if len(rows) == 1 and len(rows[0]) >= 6 and rows[0][4] != "-" \
            and rows[0][5] != "-":
        return list(rows[0])
    return None


def wait_ready(ctx: Ctx, go: Go, name: str, timeout: float
               ) -> Tuple[Optional[List[str]], str]:
    """Waits until the session probe shows exactly one ready `claude`."""
    last = [""]

    def probe() -> Optional[List[str]]:
        r = go(cmd_ready(ctx, name))
        last[0] = (f"exit {r.code}: " + (r.stdout.strip() or r.stderr.strip()
                                         or "no output"))
        if r.code == 0:
            return ready_row(probe_rows(r.stdout))
        return None

    return wait_until(ctx, probe, timeout), last[0]


def wait_phase(ctx: Ctx, go: Go, name: str, phase: str, timeout: float) -> bool:
    def probe() -> Optional[bool]:
        r = go(cmd_phase(ctx, name))
        doc = parse_json(r.stdout) if r.code == 0 else None
        return True if isinstance(doc, dict) and doc.get("phase") == phase \
            else None

    return bool(wait_until(ctx, probe, timeout))


def bounce(ctx: Ctx, go: Go, name: str) -> Optional[str]:
    """Stops and starts `name`, and waits until it is ready. None on success,
    otherwise why not."""
    r = go(cmd_stop(ctx, name))
    if r.code != 0:
        return f"`sandbox stop {name}` failed: {_first_line(r.stderr)}"
    if not wait_phase(ctx, go, name, "Stopped", ctx.settings.stop_timeout):
        return (f"{name} did not reach Stopped within "
                f"{ctx.settings.stop_timeout:g} s")
    r = go(cmd_start(ctx, name))
    if r.code != 0:
        return f"`sandbox start {name}` failed: {_first_line(r.stderr)}"
    row, last = wait_ready(ctx, go, name, ctx.settings.ready_timeout)
    if row is None:
        return (f"{name} did not show one ready session within "
                f"{ctx.settings.ready_timeout:g} s (last probe: {last})")
    return None


# --- the gateway's posture ---------------------------------------------------

def posture_findings(ctx: Ctx, go: Go, allowed: Sequence[str]
                     ) -> List[Finding]:
    """D6's posture, read only: the gateway file, `openshell status`, the
    gateway's own preflight, the listener, and the sandboxes that exist."""
    findings = gateway_toml_findings(ctx.gateway_toml)

    r = go(cmd_status())
    findings.append((r.code == 0, "openshell status reports a gateway"
                     if r.code == 0 else
                     f"openshell status failed: {_first_line(r.stderr)}"))

    r = go(cmd_preflight(ctx))
    findings.append((r.code == 0, "openshell-gateway config preflight accepts "
                     "the gateway configuration file" if r.code == 0 else
                     f"openshell-gateway config preflight failed: "
                     f"{_first_line(r.stderr or r.stdout)}"))

    r = go(cmd_listeners())
    if r.code != 0:
        findings.append((False, f"the listeners cannot be listed: "
                                f"{_first_line(r.stderr)} (D6)"))
    else:
        addrs = []
        for line in r.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[3].endswith(f":{GATEWAY_PORT}"):
                addrs.append(fields[3])
        want = f"{GATEWAY_HOST}:{GATEWAY_PORT}"
        good = bool(addrs) and all(a == want for a in addrs)
        findings.append((good,
                         f"the gateway listens on {want} and nowhere else "
                         f"(D6, loopback only)" if good else
                         f"port {GATEWAY_PORT} is not held by {want} alone "
                         f"(D6, loopback only): {', '.join(addrs) or 'no listener'}"))

    r = go(cmd_list(ctx))
    if r.code != 0:
        findings.append((False, f"the sandbox list failed: "
                                f"{_first_line(r.stderr)} (D6)"))
    else:
        ok_names = {f"{ctx.workspace}/{m}" for m in allowed}
        extra = sorted({ln.strip() for ln in r.stdout.splitlines()
                        if ln.strip()} - ok_names)
        findings.append((not extra,
                         "no sandbox exists but the fleet's (D6)" if not extra
                         else f"other sandbox(es) exist: {', '.join(extra)} "
                              f"(D6)"))
    return findings


def yaml_block_list(text: str, key: str) -> Optional[List[str]]:
    """The `- item` lines under `<key>:` in block style, at the key's indent or
    deeper, or None when the key is absent or written in flow style."""
    lines = text.splitlines()
    pattern = re.compile(r"^(\s*)" + re.escape(key) + r":\s*$")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m is None:
            continue
        indent = len(m.group(1))
        items: List[str] = []
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            body = nxt.strip()
            if len(nxt) - len(nxt.lstrip()) >= indent and body.startswith("- "):
                items.append(_unquote(body[2:].strip()))
            else:
                break
        return items
    return None


def _unquote(item: str) -> str:
    if len(item) >= 2 and item[0] == item[-1] == '"':
        try:
            value = json.loads(item)
            if isinstance(value, str):
                return value
        except ValueError:
            pass
    if len(item) >= 2 and item[0] == item[-1] == "'":
        return item[1:-1]
    return item


# --- steps 0-7 and 9 ---------------------------------------------------------

def step_0(ctx: Ctx) -> StepResult:
    inputs, findings = load_inputs(ctx.env, ctx.uid_gid())
    notes = [format_finding(ok, text) for ok, text in findings]
    if inputs is None:
        return StepResult(FAIL, tuple(notes))
    ctx.adopt(inputs)
    ctx.attach_evidence()
    assert ctx.evidence is not None
    observe(ctx, 0, "prerequisites", "load_inputs",
            [{"ok": ok, "text": text} for ok, text in findings])
    go = stepgo(ctx, 0)
    version = go(cmd_openshell_version())
    docker = go(cmd_docker_version())
    for what, r in (("openshell --version", version),
                    ("docker version", docker)):
        if r.code != 0:
            notes.append(f"{what} failed: {_first_line(r.stderr)}")
            return StepResult(FAIL, tuple(notes))
    graph = policy.resolve(ctx.fleet, ctx.members).task_graph
    ctx.evidence.write_state(FACTS_STATE, {
        "openshell": version.stdout.strip(),
        "docker": docker.stdout.strip(),
        "kernel": platform.release(),
        "claude_code": ctx.claude_code_version,
        "run_as": ctx.run_as_text,
        "workspace": ctx.workspace,
        "members": list(ctx.members),
        "sender": ctx.sender,
        "receiver": ctx.receiver,
        "task_graph": {k: sorted(v) for k, v in sorted(graph.items())},
    })
    return StepResult(PASS, tuple(notes))


def image_is_current(ctx: Ctx, inspect: Any) -> bool:
    """Whether `docker image inspect` (a result with `.code` and `.stdout`)
    shows `ctx.image` was built with this Claude Code version and run-as."""
    if inspect.code != 0:
        return False
    labels = parse_json(inspect.stdout)
    return (isinstance(labels, dict)
            and labels.get(IMAGE_LABEL_VERSION) == ctx.claude_code_version
            and labels.get(IMAGE_LABEL_RUN_AS) == ctx.run_as_text)


def step_1(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 1)
    r = go(cmd_image_inspect(ctx.image))
    if image_is_current(ctx, r):
        return StepResult(SKIPPED, ("image built with these arguments",))
    built = go(cmd_build(ctx), timeout=ctx.settings.build_timeout,
               cwd=str(REPO))
    if built.code != 0:
        return StepResult(FAIL, (f"docker build failed: "
                                 f"{_first_line(built.stderr)}",))
    return StepResult(PASS, ("image built",))


def step_2(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 2)
    findings = posture_findings(ctx, go, ctx.members)
    observe(ctx, 2, "findings", "posture_findings",
            [{"ok": ok, "text": text} for ok, text in findings])
    notes = [format_finding(ok, text) for ok, text in findings]
    if not all(ok for ok, _ in findings):
        notes.append("refusing to go on: decision D6's posture does not hold")
        return StepResult(FAIL, tuple(notes))
    return StepResult(PASS, tuple(notes))


PATH_STATE = re.compile(r"^(would create|would update|present) ", re.M)


def _all_present(stdout: str) -> bool:
    states = PATH_STATE.findall(stdout)
    return bool(states) and all(s == "present" for s in states)


def import_profile(ctx: Ctx, go: Go) -> Tuple[str, Tuple[str, ...]]:
    """Render this deployment's provider profile from the image's real paths,
    lint it, and import it into the fleet's workspace, or update the gateway's
    copy when it differs. Returns `(PASS | SKIPPED | FAIL, notes)`. It never
    deletes a profile."""
    pid = provider_profile.PROFILE_ID
    r = go(cmd_image_paths(ctx.image))
    if r.code != 0:
        return FAIL, (f"reading the image's paths failed: "
                      f"{_first_line(r.stderr)}; run step 1 first",)
    try:
        paths = provider_profile.parse_image_paths(r.stdout)
    except provider_profile.ProfileError as e:
        return FAIL, (f"{e}; no default is used",)
    binaries = provider_profile.image_binaries(paths)
    observe(ctx, 3, "image-binaries", "docker run readlink -f", paths)

    dry = go(cmd_profile_kit(ctx, binaries, False))
    if dry.code != 0:
        return FAIL, (f"l1-kit profile (dry run) failed: "
                      f"{_first_line(dry.stderr)}",)
    wrote = not _all_present(dry.stdout)
    if wrote:
        done = go(cmd_profile_kit(ctx, binaries, True))
        if done.code != 0:
            return FAIL, (f"l1-kit profile failed: "
                          f"{_first_line(done.stderr)}",)

    def listed(after: bool) -> Tuple[Optional[List[dict]], str]:
        lst = go(cmd_profile_list(ctx, after))
        if lst.code != 0:
            return None, (f"listing the provider profiles failed: "
                          f"{_first_line(lst.stderr)}")
        try:
            return provider_profile.gateway_copies(parse_json(lst.stdout)), ""
        except provider_profile.ProfileError as e:
            return None, str(e)

    try:
        rendered = provider_profile.render_profile(
            provider_profile.load_template(), binaries)
        copies, why = listed(False)
        if copies is None:
            return FAIL, (why,)
        if len(copies) > 1:
            return FAIL, (f"the gateway lists {len(copies)} profiles with the "
                          f"ID {pid}: ambiguous",)
        action: Optional[str] = None
        if not copies:
            # Lint only what is about to be imported: OpenShell 0.1.2's lint
            # rejects a profile whose ID the gateway already has ("custom
            # provider profile ... already exists"; seen on L2's host).
            lint = go(cmd_profile_lint(ctx))
            if lint.code != 0:
                return FAIL, (f"the provider profile does not lint: "
                              f"{_all_lines(lint.stderr or lint.stdout)}",)
            imp = go(cmd_profile_import(ctx))
            if imp.code != 0:
                return FAIL, (f"the profile import failed: "
                              f"{_first_line(imp.stderr)}",)
            action = "imported"
        elif not provider_profile.same_profile(rendered, copies[0]):
            doc = provider_profile.update_document(
                rendered, copies[0].get("resource_version"))
            assert ctx.evidence is not None
            rel = ctx.evidence.record(3, "profile-update-document", doc)
            upd = go(cmd_profile_update(ctx, ctx.evidence.path(rel)))
            if upd.code != 0:
                return FAIL, (f"the profile update failed: "
                              f"{_first_line(upd.stderr)}",)
            action = "updated"
        if action is not None:
            after, why = listed(True)
            if after is None:
                return FAIL, (why,)
            if len(after) != 1 \
                    or not provider_profile.same_profile(rendered, after[0]):
                return FAIL, ("the gateway's copy does not match what was "
                              "imported",)
    except provider_profile.ProfileError as e:
        return FAIL, (str(e),)

    if not wrote and action is None:
        return SKIPPED, (f"the provider profile {pid} is imported and "
                         f"identical",)
    what = action or "rendered"
    return PASS, (f"the provider profile {pid} is {what}",)


def step_3(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 3)
    dry = go(cmd_prepare(ctx, False))
    if dry.code != 0:
        return StepResult(FAIL, (f"l1-kit prepare (dry run) failed: "
                                 f"{_first_line(dry.stderr)}",))
    prepared_skipped = _all_present(dry.stdout)
    notes: List[str] = []
    if prepared_skipped:
        notes.append("the host is already prepared")
    else:
        done = go(cmd_prepare(ctx, True))
        if done.code != 0:
            return StepResult(FAIL, (f"l1-kit prepare failed: "
                                     f"{_first_line(done.stderr)}",))
        notes.append("the host is prepared")
    outcome, more = import_profile(ctx, go)
    if outcome == FAIL:
        return StepResult(FAIL, tuple(notes) + more)
    both = prepared_skipped and outcome == SKIPPED
    return StepResult(SKIPPED if both else PASS, tuple(notes) + more)


def step_4(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 4)
    notes: List[str] = []
    created = False
    for name in ctx.members:
        state, doc, why = find_sandbox(ctx, go, name)
        if state == "unknown":
            return StepResult(FAIL, (why,))
        if state == "present":
            assert doc is not None
            problem = identity_problem(ctx, name, doc)
            if problem:
                return StepResult(FAIL, (problem,))
            notes.append(f"{name}: exists; not re-created")
            continue
        r = go(cmd_create(ctx, name), timeout=ctx.settings.create_timeout,
               with_key=True)
        if r.code != 0:
            return StepResult(FAIL, notes + [
                f"the create command for {name} failed: "
                f"{_first_line(r.stderr)}; do not go on with a sandbox "
                f"missing"])
        created = True
        notes.append(f"{name}: created")
    for name in ctx.members:
        row, last = wait_ready(ctx, go, name, ctx.settings.ready_timeout)
        if row is None:
            return StepResult(FAIL, tuple(notes) + (
                f"{name} did not show one ready session within "
                f"{ctx.settings.ready_timeout:g} s (last probe: {last})",))
    return StepResult(PASS if created else SKIPPED, tuple(notes))


def step_5(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 5)
    notes: List[str] = []
    wrote = False
    ids: Dict[str, str] = {}
    for name in ctx.members:
        r = go(cmd_get(ctx, name))
        doc = parse_json(r.stdout) if r.code == 0 else None
        if not isinstance(doc, dict):
            return StepResult(FAIL, (f"`sandbox get {name}` failed: "
                                     f"{_first_line(r.stderr)}",))
        if doc.get("name") != name or doc.get("workspace") != ctx.workspace:
            return StepResult(FAIL, (
                f"`sandbox get {name}` reports name {doc.get('name')!r} in "
                f"workspace {doc.get('workspace')!r}",))
        sandbox_id = doc.get("id")
        reason = membership.id_problem(sandbox_id)
        if reason:
            return StepResult(FAIL, (reason,))
        ids[name] = str(sandbox_id)
        dry = go(cmd_record(ctx, name, ids[name], False))
        if dry.code != 0:
            return StepResult(FAIL, (f"l1-kit record {name} failed: "
                                     f"{_first_line(dry.stderr)}",))
        if _all_present(dry.stdout):
            notes.append(f"{name}: already recorded")
            continue
        done = go(cmd_record(ctx, name, ids[name], True))
        if done.code != 0:
            return StepResult(FAIL, (f"l1-kit record {name} failed: "
                                     f"{_first_line(done.stderr)}",))
        wrote = True
        notes.append(f"{name}: recorded")
    for path in (router_json(ctx), render.selected_json(ctx.home)):
        if not os.path.isfile(path):
            return StepResult(FAIL, (f"{os.path.basename(path)} does not exist "
                                     f"after recording every member",))
    ctx.ids = ids
    return StepResult(PASS if wrote else SKIPPED, tuple(notes))


def router_state(ctx: Ctx, go: Go, cmd: Optional[Cmd] = None
                 ) -> Tuple[Optional[str], Optional[Result]]:
    """`"running"`, another docker state, or None when there is no container."""
    r = go(cmd or cmd_ps_router())
    if r.code != 0:
        return None, r
    for line in r.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == ROUTER_CONTAINER:
            return fields[1], r
    return None, r


def crash_note(ctx: Ctx, go: Go, state: str) -> str:
    """Why step 6 stopped at once: the container's state, its exit code and
    the last non-blank line of its log. Every call reads; the container is
    never removed, started or restarted."""
    ec = go(cmd_router_exit_code())
    if ec.code == 0 and ec.stdout.strip():
        exit_code = ec.stdout.strip()
    else:
        exit_code = f"unreadable ({_first_line(ec.stderr)})"
    lg = go(cmd_router_log_tail())
    if lg.code != 0:
        last = f"unreadable ({_first_line(lg.stderr)})"
    else:
        # The router logs to stderr (router/__main__.py:188-190), and `docker
        # logs` keeps the two streams apart, so stderr's last line comes first.
        last = (_last_line(lg.stderr) or _last_line(lg.stdout)
                or "the log is empty")
    return (f"the router container is {state} (exit code {exit_code}), so it "
            f"will publish no roster; its last log line: {last}. The runner "
            f"never removes or restarts a container: fix the cause, remove "
            f"the container yourself and rerun")


def wait_for_roster(ctx: Ctx, go: Go) -> Tuple[str, str]:
    """("roster", "") when the roster is published; ("crashed", <state>) as
    soon as the container is restarting, exited or dead; ("timeout", "")
    after router_timeout. The roster is looked for before docker is asked."""
    def probe() -> Optional[Tuple[str, str]]:
        if os.path.isfile(roster_path(ctx)):
            return "roster", ""
        state, r = router_state(ctx, go, cmd_ps_router_wait())
        if r is not None and r.code == 0 and state in CRASHED_STATES:
            return "crashed", str(state)
        return None
    found = wait_until(ctx, probe, ctx.settings.router_timeout)
    return found if found is not None else ("timeout", "")


def step_6(ctx: Ctx) -> StepResult:
    go = stepgo(ctx, 6)
    if not os.path.isfile(router_json(ctx)):
        return StepResult(FAIL, ("router.json does not exist: run step 5 "
                                 "first",))
    state, ps = router_state(ctx, go)
    assert ps is not None
    if ps.code != 0:
        return StepResult(FAIL, (f"docker ps failed: "
                                 f"{_first_line(ps.stderr)}",))
    started = False
    if state is not None and state != "running":
        return StepResult(FAIL, (
            f"the router container exists and is {state}; the runner never "
            f"removes a container: remove it yourself and rerun",))
    if state is None:
        for cmd, timeout in ((cmd_router_build(ctx),
                              ctx.settings.build_timeout),
                             (cmd_router_run(ctx), ctx.settings.command_timeout)):
            r = go(cmd, timeout=timeout)
            if r.code != 0:
                return StepResult(FAIL, (
                    f"{cmd[0]} failed: "
                    f"{_first_line(r.stderr or r.stdout)}",))
        after, ps2 = router_state(ctx, go)
        if after in CRASHED_STATES:
            return StepResult(FAIL, (crash_note(ctx, go, after),))
        if after != "running":
            return StepResult(FAIL, (f"the router container is "
                                     f"{after or 'absent'} after it was "
                                     f"started",))
        started = True
    how, crashed = wait_for_roster(ctx, go)
    if how == "crashed":
        return StepResult(FAIL, (crash_note(ctx, go, crashed),))
    if how == "timeout":
        return StepResult(FAIL, (f"the router published no roster within "
                                 f"{ctx.settings.router_timeout:g} s",))
    observe(ctx, 6, "roster", "host listing", {"roster": "present"})
    if not started:
        return StepResult(SKIPPED, ("the router is already running",))
    go(cmd_router_logs())
    return StepResult(PASS, ("the router is running and has published its "
                             "roster",))


def _delegation(doc: Optional[Mapping[str, Any]]) -> Optional[Delegation]:
    if not doc:
        return None
    try:
        return Delegation(**{k: doc.get(k) for k in Delegation._fields})
    except TypeError:
        return None


def delegation_of(ctx: Ctx) -> Optional[Delegation]:
    assert ctx.evidence is not None
    return _delegation(ctx.evidence.read_state(DELEGATION_STATE))


def _submitted_id(r: Result) -> Optional[str]:
    m = SUBMITTED_RE.search(r.stdout)
    return m.group(1) if m else None


def step_7(ctx: Ctx) -> StepResult:
    assert ctx.evidence is not None
    go = stepgo(ctx, 7)
    state, ps = router_state(ctx, go)
    ids = recorded_ids(ctx)
    if state != "running" or ids is None:
        return StepResult(FAIL, ("the router is not running or membership "
                                 "does not record both members: run steps "
                                 "4-6 first",))
    sender, receiver = ctx.sender, ctx.receiver
    prior = delegation_of(ctx)
    if prior is not None and prior.state == "complete":
        return StepResult(SKIPPED, ("the delegation is complete",))
    d = prior if prior is not None else Delegation(
        DELEGATION_SUBJECT, sender, receiver, "", None, None, None, "planted")

    def save(**kw: Any) -> None:
        nonlocal d
        d = d._replace(**kw)
        ctx.evidence.write_state(DELEGATION_STATE, d._asdict())  # type: ignore

    def missing(what: str) -> StepResult:
        note = [f"{what}"]
        result = lane_host(ctx, sender, LANE_OUTBOX, RESULTS,
                           f"{d.req_id}.json")
        if os.path.isfile(result):
            observe(ctx, 7, "router-result-file", "host file", {
                "path": os.path.relpath(result, ctx.home),
                "document": parse_json(_read(result))})
            note.append("the router's result for the request exists on the "
                        "host: see the evidence")
        return StepResult(FAIL, tuple(note))

    req_id = d.req_id or find_planted(ctx, sender, DELEGATION_SUBJECT)
    if not req_id:
        r = go(cmd_submit(ctx, "submit-delegation", sender, receiver,
                          DELEGATION_SUBJECT, DELEGATION_BODY))
        req_id = _submitted_id(r) if r.code == 0 else None
        if not req_id:
            return StepResult(FAIL, (f"the delegation was not planted: "
                                     f"{_first_line(r.stderr or r.stdout)}",))
    if not d.req_id:
        save(req_id=req_id, state="planted")

    if not d.notice_id:
        def placed() -> Optional[dict]:
            return find_event(audit_events(ctx, receiver), EVENT_NOTICE_PLACED,
                              from_instance=sender, req_id=req_id)
        event = wait_until(ctx, placed, ctx.settings.delivery_timeout)
        if event is None:
            return missing(f"the router placed no notice for request "
                           f"{req_id} in {receiver}'s peer lane within "
                           f"{ctx.settings.delivery_timeout:g} s")
        observe(ctx, 7, "audit-placed", audit_path(ctx, receiver), event)
        save(notice_id=event.get("notice_id"), state="placed")

    if d.outcome != "delivered":
        def consumed() -> Optional[dict]:
            return find_event(audit_events(ctx, receiver), EVENT_OUTCOME,
                              notice_id=d.notice_id)
        event = wait_until(ctx, consumed, ctx.settings.delivery_timeout)
        if event is None:
            return missing(f"{receiver}'s delivery daemon reported no outcome "
                           f"for notice {d.notice_id} within "
                           f"{ctx.settings.delivery_timeout:g} s")
        observe(ctx, 7, "audit-outcome", audit_path(ctx, receiver), event)
        if event.get("outcome") != "delivered":
            save(outcome=event.get("outcome"))
            return missing(f"the outcome for notice {d.notice_id} is "
                           f"{event.get('outcome')!r}, not 'delivered'")
        save(outcome="delivered", state="delivered")

    def replied() -> Optional[dict]:
        event = find_event(audit_events(ctx, sender), EVENT_NOTICE_PLACED,
                           from_instance=receiver, in_reply_to=d.notice_id)
        if event is None:
            return None
        notice = lane_host(ctx, sender, LANE_PEER, NOTICES,
                           f"notice-{event.get('notice_id')}.json")
        return event if os.path.isfile(notice) else None
    event = wait_until(ctx, replied, ctx.settings.reply_timeout)
    if event is None:
        return missing(f"{receiver}'s reply to notice {d.notice_id} did not "
                       f"reach {sender}'s peer lane within "
                       f"{ctx.settings.reply_timeout:g} s")
    observe(ctx, 7, "audit-reply", audit_path(ctx, sender), event)
    save(reply_notice_id=event.get("notice_id"), state="complete")
    return StepResult(PASS, (f"{sender} delegated to {receiver}, the outcome "
                             f"was delivered and {receiver}'s reply reached "
                             f"{sender}",))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# --- the checks ---------------------------------------------------------------

Sub = Tuple[str, str]   # (outcome, reason)


def combine(subs: Sequence[Sub]) -> Sub:
    """Any FAIL gives FAIL; otherwise any UNKNOWN gives UNKNOWN; otherwise
    PASS. The reason is the reasons of the deciding outcome."""
    for outcome in (FAIL, UNKNOWN):
        reasons = [r for o, r in subs if o == outcome]
        if reasons:
            return outcome, "; ".join(reasons)
    return PASS, "; ".join(r for _, r in subs if r)


class CheckRun:
    """One check's window on the executor. It records which evidence files the
    check produced. `shared` is the step's scratch space, so a later check can
    read an earlier one's sub-result."""

    def __init__(self, ctx: Ctx, heading: Optional[str],
                 shared: Dict[str, Any]) -> None:
        self.ctx = ctx
        self.heading = heading
        self.shared = shared
        self.records: List[str] = []

    def run(self, cmd: Cmd, timeout: Optional[float] = None, **kw: Any
            ) -> Result:
        r = self.ctx.executor.run(
            8, cmd[0], cmd[1],
            timeout=self.ctx.settings.command_timeout if timeout is None
            else timeout, check=self.heading, **kw)
        self.records.append(r.record)
        return r

    def observe(self, label: str, source: str, obj: Any) -> str:
        rec = observe(self.ctx, 8, label, source, obj, self.heading)
        self.records.append(rec)
        return rec

    def finish(self, subs: Sequence[Sub], facts: Sequence[str] = ()
               ) -> CheckOutcome:
        outcome, reason = combine(subs)
        return CheckOutcome(outcome, reason, tuple(facts),
                            tuple(dict.fromkeys(self.records)))


def _need_delegation(ctx: Ctx) -> Tuple[Optional[Delegation], Sub]:
    d = delegation_of(ctx)
    if d is None or not d.notice_id:
        return None, (UNKNOWN, "step 7 has no recorded delegation")
    return d, (PASS, "")


def _exec_sub(r: Result, what: str) -> Optional[Sub]:
    """UNKNOWN when an `exec` did not run at all."""
    if r.code is None:
        return UNKNOWN, (f"{what} did not run: "
                         f"{'timed out' if r.timed_out else _first_line(r.stderr)}")
    return None


def load_validate(spec_dir: str) -> Any:
    """`fixtures/validate.py` from the amap-spec directory, without writing
    bytecode."""
    path = posixpath.join(spec_dir, "fixtures", "validate.py")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("amap_spec_validate", path)
        if spec is None or spec.loader is None:
            raise RunnerError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class LiveDoc(NamedTuple):
    path: str     # relative to home
    name: str     # the fixture name that picks the schema
    prefix: str


def live_documents(ctx: Ctx, validate: Any) -> List[LiveDoc]:
    """The regular files the run produced, each with the fixture name that picks
    its schema."""
    def prefix_of(name: str) -> str:
        for prefix in validate.SCHEMA_FOR_PREFIX:
            if name.startswith(prefix):
                return prefix
        raise RunnerError(f"no schema for {name}")

    found: List[Tuple[str, str]] = []
    roster = roster_path(ctx)
    if os.path.isfile(roster):
        found.append((roster, "roster-live.json"))
    for m in ctx.members:
        plan = (
            (lane_host(ctx, m, LANE_INBOX, NOTICES), None, "notice-live.json"),
            (lane_host(ctx, m, LANE_INBOX, MESSAGES), None,
             "message-live.json"),
            (lane_host(ctx, m, LANE_PEER, NOTICES), None, "peer-live.json"),
            (lane_host(ctx, m, LANE_PEER, MESSAGES), None, "message-live.json"),
            (lane_host(ctx, m, LANE_OUTBOX), "req-", "request-live.json"),
            (lane_host(ctx, m, LANE_OUTBOX, PROCESSED), "req-",
             "request-live.json"),
            (lane_host(ctx, m, LANE_OUTBOX, RESULTS), None, "result-live.json"),
        )
        for directory, start, name in plan:
            for entry in _regular_files(directory):
                if start is None or entry.startswith(start):
                    found.append((posixpath.join(directory, entry), name))
    return [LiveDoc(os.path.relpath(p, ctx.home), name, prefix_of(name))
            for p, name in found]


def check_documents(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    validate = load_validate(ctx.spec_dir)
    docs = live_documents(ctx, validate)
    listing: List[dict] = []
    failed: List[str] = []
    for doc in docs:
        try:
            with open(posixpath.join(ctx.home, doc.path),
                      encoding="utf-8") as fh:
                parsed = json.load(fh)
            errors = list(validate.check_document(doc.name, parsed))
        except (OSError, ValueError) as e:
            errors = [f"cannot be read as JSON: {e}"]
        listing.append({"path": doc.path, "schema":
                        validate.SCHEMA_FOR_PREFIX[doc.prefix],
                        "errors": errors})
        if errors:
            failed.append(f"{doc.path}: {errors[0]}")
    run.observe("documents", "host listing", listing)
    covered = sorted({d.prefix for d in docs})
    absent = [p for p in validate.SCHEMA_FOR_PREFIX if p not in covered]
    facts = [f"check_document (schema plus post-checks) on {p} documents, "
             f"schema {validate.SCHEMA_FOR_PREFIX[p]}" for p in covered]
    if absent:
        facts.append("not produced by this run: " + ", ".join(absent))
    if failed:
        return run.finish([(FAIL, "; ".join(failed))], facts)
    if absent:
        return run.finish([(UNKNOWN, "no live document of the class(es) "
                            + ", ".join(absent) + "; the operator says in the "
                            "report which are not applicable")], facts)
    return run.finish([(PASS, "every live document passes check_document")],
                      facts)


def check_connector_operational(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    d, missing = _need_delegation(ctx)
    if d is None:
        return run.finish([missing])
    S, B = ctx.sender, ctx.receiver
    subs: List[Sub] = []
    facts: List[str] = []

    consumed = find_event(audit_events(ctx, B), EVENT_OUTCOME,
                          notice_id=d.notice_id)
    run.observe("audit-outcome", audit_path(ctx, B), consumed)
    if consumed is None:
        subs.append((UNKNOWN, f"{B}'s audit log has no outcome for the "
                              f"notice"))
    elif consumed.get("outcome") == "delivered":
        subs.append((PASS, f"{B}'s daemon reported delivered"))
        facts.append(f"{B}'s outcome for the delegation: delivered")
    else:
        subs.append((FAIL, f"{B}'s outcome is {consumed.get('outcome')!r}"))

    reply = find_event(audit_events(ctx, S), EVENT_NOTICE_PLACED,
                       from_instance=B, in_reply_to=d.notice_id)
    on_host = bool(reply) and os.path.isfile(lane_host(
        ctx, S, LANE_PEER, NOTICES, f"notice-{reply.get('notice_id')}.json"))
    run.observe("audit-reply", audit_path(ctx, S), reply)
    if reply is not None and on_host:
        subs.append((PASS, f"{B}'s reply reached {S}"))
        facts.append(f"{B} replied, and the notice is in {S}'s peer lane")
    else:
        subs.append((FAIL, f"no reply from {B} reached {S}'s peer lane"))

    r = run.run(cmd_submit_result(ctx, S, d.req_id))
    result = parse_json(r.stdout) if r.code == 0 else None
    bad = _exec_sub(r, "submit-result")
    if bad:
        subs.append(bad)
    elif isinstance(result, dict) and result.get("outcome") == "accepted":
        subs.append((PASS, f"{S}'s submit_result says accepted"))
        facts.append(f"{S}'s submit_result outcome: accepted")
    elif isinstance(result, dict):
        subs.append((FAIL, f"{S}'s submit_result says "
                           f"{result.get('outcome')!r}, not accepted"))
    elif r.code == 0 and r.stdout.strip() == "pending":
        subs.append((UNKNOWN, f"{S}'s submit_result is still pending"))
    else:
        subs.append((UNKNOWN, f"{S}'s submit_result printed no result"))

    for name in (S, B):
        r = run.run(cmd_ready(ctx, name))
        bad = _exec_sub(r, f"the session probe in {name}")
        if bad:
            subs.append(bad)
        elif r.code != 0:
            subs.append((UNKNOWN, f"the session probe in {name} failed: "
                                  f"{_first_line(r.stderr)}"))
        else:
            rows = probe_rows(r.stdout)
            if len(rows) == 1:
                subs.append((PASS, f"one claude session is visible in {name}"))
                facts.append(f"the lister shows one claude session in {name}")
            else:
                subs.append((FAIL, f"the lister shows {len(rows)} claude "
                                   f"session(s) in {name}, not one"))
    return run.finish(subs, facts)


def check_inbox_write_denied(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    S = ctx.sender
    inbox = render.lane_target(LANE_INBOX)
    outbox = render.lane_target(LANE_OUTBOX)
    subs: List[Sub] = []
    facts: List[str] = []

    r = run.run(cmd_exec(ctx, "touch-inbox", S, "touch", f"{inbox}/probe"))
    bad = _exec_sub(r, "the write")
    if bad:
        subs.append(bad)
    elif r.code == 0:
        subs.append((FAIL, f"a write into {S}'s inbox lane succeeded"))
    else:
        subs.append((PASS, "the write into the inbox lane was denied"))
        facts.append("the write into the inbox lane failed: "
                     + _first_line(r.stderr or r.stdout))

    sandbox_id = ctx.ids.get(S) or (recorded_ids(ctx) or {}).get(S)
    if not sandbox_id:
        subs.append((UNKNOWN, f"{S}'s sandbox ID is not recorded"))
    else:
        ps = run.run(("docker-ps-sandbox",
                      ["docker", "ps", "-q", "--filter",
                       f"label={SANDBOX_ID_LABEL}={sandbox_id}"]))
        containers = [ln.strip() for ln in ps.stdout.splitlines() if ln.strip()]
        if ps.code != 0 or not containers:
            subs.append((UNKNOWN, f"docker shows no container for {S}'s "
                                  f"sandbox ID"))
        else:
            matching: List[Tuple[str, List[dict]]] = []
            broken = False
            for n, cid in enumerate(containers, 1):
                ins = run.run((f"inspect-mounts-{n}",
                               ["docker", "inspect", "--format",
                                "{{json .Mounts}}", cid]))
                mounts = parse_json(ins.stdout) if ins.code == 0 else None
                if not isinstance(mounts, list):
                    broken = True
                    continue
                if any(isinstance(m, dict) and m.get("Destination") == inbox
                       for m in mounts):
                    matching.append((cid, mounts))
            if broken:
                subs.append((UNKNOWN, "docker inspect gave no mount list"))
            elif len(matching) > 1:
                subs.append((UNKNOWN, f"several containers mount the inbox "
                                      f"lane of {S}"))
            elif not matching:
                subs.append((FAIL, f"no container of {S}'s sandbox mounts the "
                                   f"inbox lane"))
            else:
                mounts = matching[0][1]
                wrong = []
                for row in render.mount_table(ctx.host, S):
                    hit = [m for m in mounts if isinstance(m, dict)
                           and m.get("Destination") == row.target]
                    want_rw = not row.read_only
                    if not hit or hit[0].get("RW") is not want_rw:
                        wrong.append(f"{row.target} should be "
                                     f"{'writable' if want_rw else 'read-only'}")
                if wrong:
                    subs.append((FAIL, "mount flags differ: " + "; ".join(wrong)))
                else:
                    subs.append((PASS, "the mount flags match the mount table"))
                    facts.append(f"the container mounts the inbox lane "
                                 f"read-only and the outbox lane read-write")

    r = run.run(("policy-only", policy_only_argv(ctx, S)))
    if r.code != 0:
        subs.append((UNKNOWN, f"`sandbox get --policy-only` failed: "
                              f"{_first_line(r.stderr)}"))
    else:
        ro = yaml_block_list(r.stdout, "read_only")
        rw = yaml_block_list(r.stdout, "read_write")
        if ro is None or rw is None:
            subs.append((UNKNOWN, "the policy text has no block-style "
                                  "read_only and read_write lists"))
        elif inbox in ro and inbox not in rw and outbox in rw:
            subs.append((PASS, "the policy lists the inbox lane under "
                               "read_only and the outbox lane under "
                               "read_write"))
            facts.append("Landlock layer: the inbox lane is under read_only, "
                         "the outbox lane under read_write")
        else:
            subs.append((FAIL, "the policy does not put the inbox lane under "
                               "read_only alone and the outbox lane under "
                               "read_write"))
    return run.finish(subs, facts)


def check_network_denied(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    S = ctx.sender
    r = run.run(cmd_py(ctx, "network-probe", S, NETWORK_PROBE, MAIL_PROBE_URL))
    bad = _exec_sub(r, "the network probe")
    if bad:
        return run.finish([bad])
    if r.code == 0 or "REACHED" in r.stdout:
        return run.finish([(FAIL, f"the call to {MAIL_PROBE_HOST} succeeded")])
    if not r.stdout.startswith("DENIED"):
        return run.finish([(UNKNOWN, f"the probe did not report a denial: "
                                     f"{_first_line(r.stderr or r.stdout)}")])
    denial = r.stdout.strip()

    def logged() -> Optional[str]:
        got = run.run(("sandbox-logs", openshell(
            ctx, "logs", S, "--source", "sandbox", "--level", "info", "-n",
            "100")))
        return got.stdout if got.code == 0 and MAIL_PROBE_HOST in got.stdout \
            else None

    line = wait_until(ctx, logged, ctx.settings.log_timeout)
    if line is None:
        return run.finish([(FAIL, f"the call was denied but not logged: the "
                                  f"sandbox log names no {MAIL_PROBE_HOST}")],
                          [f"the call failed: {denial}"])
    shown = [ln.strip() for ln in line.splitlines() if MAIL_PROBE_HOST in ln]
    return run.finish([(PASS, "the call was denied and the denial is logged")],
                      [f"the call failed: {denial}",
                       "the sandbox log names the host: " + shown[0]])


def check_reverse_task_held(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    S, B = ctx.sender, ctx.receiver
    req_id = find_planted(ctx, B, REVERSE_SUBJECT)
    if not req_id:
        r = run.run(cmd_submit(ctx, "submit-reverse", B, S, REVERSE_SUBJECT,
                               REVERSE_BODY))
        req_id = _submitted_id(r) if r.code == 0 else None
        if not req_id:
            return run.finish([(UNKNOWN, f"the reverse task was not "
                                         f"planted: "
                                         f"{_first_line(r.stderr or r.stdout)}")])
    held = wait_until(ctx, lambda: True if os.path.isfile(
        held_path(ctx, B, req_id)) else None, ctx.settings.delivery_timeout)
    run.observe("held-copy", "router state", {
        "held": bool(held), "path": os.path.relpath(held_path(ctx, B, req_id),
                                                    ctx.home)})
    if not held:
        return run.finish([(UNKNOWN, f"the router held no copy of the request "
                                     f"within "
                                     f"{ctx.settings.delivery_timeout:g} s")])
    subs: List[Sub] = [(PASS, "the router holds a copy of the request")]
    facts = ["the router holds the request under its private state directory"]
    r = run.run(cmd_submit_result(ctx, B, req_id))
    result = parse_json(r.stdout) if r.code == 0 else None
    if isinstance(result, dict):
        if result.get("outcome") == "queued_for_human":
            subs.append((PASS, "submit_result says queued_for_human"))
            facts.append(f"submit_result: queued_for_human, reason "
                         f"{result.get('reason_code')}")
        else:
            subs.append((FAIL, f"submit_result says "
                               f"{result.get('outcome')!r}, not "
                               f"queued_for_human"))
    else:
        subs.append((UNKNOWN, "submit_result printed no result"))
    placed = find_event(audit_events(ctx, S), EVENT_NOTICE_PLACED,
                        from_instance=B, req_id=req_id)
    run.observe("audit-placement", audit_path(ctx, S), placed)
    if placed is not None:
        subs.append((FAIL, f"the router placed a notice for the request in "
                           f"{S}'s peer lane"))
    else:
        subs.append((PASS, f"no notice was placed in {S}'s peer lane"))
        facts.append(f"the router's audit log for {S} has no placement for "
                     f"the request")
    return run.finish(subs, facts)


def check_router_writes_visible(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    d, missing = _need_delegation(ctx)
    if d is None:
        return run.finish([missing])
    B = ctx.receiver
    name = f"notice-{d.notice_id}.json"
    on_host = name in _regular_files(lane_host(ctx, B, LANE_PEER, NOTICES))
    run.observe("host-listing", "host listing", {"present": on_host,
                                                 "name": name})
    if not on_host:
        return run.finish([(UNKNOWN, f"the notice is not in {B}'s peer lane "
                                     f"on the host")])
    r = run.run(cmd_exec(ctx, "ls-peer-notices", B, "ls", "-1",
                         posixpath.join(render.lane_target(LANE_PEER),
                                        NOTICES)))
    bad = _exec_sub(r, "the listing")
    if bad:
        return run.finish([bad])
    if r.code != 0:
        return run.finish([(UNKNOWN, f"the listing inside {B} failed: "
                                     f"{_first_line(r.stderr)}")])
    if name in r.stdout.split():
        return run.finish([(PASS, f"the notice the router wrote is listed "
                                  f"inside the running {B}")],
                          [f"the notice is listed on the host and inside {B}"])
    return run.finish([(FAIL, f"the notice is on the host but {B} does not "
                              f"list it")])


ERRNO_RE = re.compile(r"^E[A-Z0-9]+$")


def check_inbox_errno(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    S = ctx.sender
    r = run.run(cmd_py(ctx, "write-probe-inbox", S, WRITE_PROBE,
                       f"{render.lane_target(LANE_INBOX)}/probe", "create"))
    bad = _exec_sub(r, "the write probe")
    if bad:
        return run.finish([bad])
    seen = r.stdout.strip()
    if r.code != 0 or not seen:
        return run.finish([(UNKNOWN, f"the write probe printed no errno: "
                                     f"{_first_line(r.stderr)}")])
    if seen == "EROFS":
        return run.finish([(PASS, "the write failed with EROFS")],
                          ["errno: EROFS"])
    if seen == "OPENED":
        return run.finish([(FAIL, "the write into the inbox lane succeeded")])
    if ERRNO_RE.match(seen):
        return run.finish([(FAIL, f"errno was {seen}, not EROFS")],
                          [f"errno: {seen}"])
    return run.finish([(UNKNOWN, f"the write probe printed {seen!r}")])


def probe_policy_text(ctx: Ctx) -> str:
    """The sender's policy with the peer lane's `read_only` line removed: the
    lane is still mounted, and the policy no longer lists it."""
    doc = render.policy_document(render.mount_table(ctx.host, ctx.sender),
                                 ctx.run_as)
    read_only = doc["filesystem_policy"]["read_only"]
    doc["filesystem_policy"]["read_only"] = [
        p for p in read_only if p != render.lane_target(LANE_PEER)]
    return render.policy_yaml(doc)


def probe_create_argv(ctx: Ctx, policy_path: str) -> List[str]:
    argv = render.create_argv(
        ctx.workspace, PROBE_NAME, ctx.host, policy_path,
        render.mount_table(ctx.host, ctx.sender), ctx.addresses[ctx.sender])
    return argv[:argv.index("--") + 1] + ["sleep", "3600"]


def probe_policy_path(ctx: Ctx) -> str:
    return posixpath.join(ctx.home, EVIDENCE_DIRNAME, "step-8",
                          PROBE_POLICY_NAME)


def _probe_id(ctx: Ctx, run: CheckRun) -> Tuple[str, Optional[str]]:
    """`(state, id)` of the probe sandbox, as `find_sandbox` reports it."""
    state, doc, why = find_sandbox(ctx, run.run, PROBE_NAME)
    if state == "present" and doc is not None:
        return state, str(doc.get("id"))
    return state, None


def check_unlisted_lane(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    assert ctx.evidence is not None
    S = ctx.sender
    recorded = ctx.evidence.read_state(PROBE_STATE) or {}
    state, existing = _probe_id(ctx, run)
    if state == "unknown":
        return run.finish([(UNKNOWN, f"whether {PROBE_NAME} exists could not "
                                     f"be read")])
    if state == "present":
        if not existing or existing != recorded.get("id"):
            return run.finish([(UNKNOWN, f"{PROBE_NAME} exists and the runner "
                                         f"did not create it")])
        # Ours, from an interrupted run: remove it, and start again.
        run.run(("delete-" + PROBE_NAME + "-stale",
                 openshell(ctx, "sandbox", "delete", PROBE_NAME)))

    policy_path = ctx.evidence.write_text(f"step-8/{PROBE_POLICY_NAME}",
                                          probe_policy_text(ctx))
    inbox = render.lane_target(LANE_INBOX)
    peer = render.lane_target(LANE_PEER)
    subs: List[Sub] = []
    facts: List[str] = []
    first = second = None
    try:
        r = run.run(("create-" + PROBE_NAME,
                     probe_create_argv(ctx, policy_path)),
                    timeout=ctx.settings.create_timeout, with_key=True)
        flat = re.sub("[\\s\u2502\u00d7]+", "", r.stdout + r.stderr)
        if r.code != 0 and re.sub(r"\s+", "", INTERCEPTOR_PREFIX) in flat:
            # With the mounts interceptor on, this probe (a non-member mounting
            # alpha's lanes) is refused before it exists, which is the
            # interceptor's job. The Landlock property it tests was settled
            # with the interceptor off (POC-REPORT, L1 and L2), so this stays
            # UNKNOWN here, never a pass.
            subs.append((UNKNOWN, "refused by the mounts interceptor, as it "
                                  "should be: a probe that is not a fleet "
                                  "member cannot mount alpha's lanes, so this "
                                  "experiment needs the interceptor off; see "
                                  "POC-REPORT for the run that settled it"))
        elif r.code != 0:
            subs.append((UNKNOWN, f"the probe sandbox could not be created: "
                                  f"{_first_line(r.stderr)}"))
        else:
            state, made = _probe_id(ctx, run)
            if state != "present" or not made:
                subs.append((UNKNOWN, "the probe sandbox has no readable ID"))
            else:
                ctx.evidence.write_state(PROBE_STATE, {
                    "name": PROBE_NAME, "id": made,
                    "workspace": ctx.workspace})
                first = run.run(cmd_exec(ctx, "ls-probe-peer", PROBE_NAME,
                                         "ls", peer))
                second = run.run(cmd_exec(ctx, "ls-probe-inbox", PROBE_NAME,
                                          "ls", inbox))
    finally:
        cleanup = ctx.evidence.read_state(PROBE_STATE) or {}
        state, seen = _probe_id(ctx, run)
        if state == "present" and seen and seen == cleanup.get("id"):
            run.run(cmd_delete_probe(ctx))
    if first is not None and second is not None:
        if first.code is None or second.code is None:
            subs.append((UNKNOWN, "a listing in the probe did not run"))
        elif first.code == 0:
            subs.append((FAIL, "the lane that is missing from the policy is "
                               "readable"))
        elif second.code == 0:
            subs.append((PASS, "the unlisted lane is unreadable and the "
                               "listed lane is readable"))
            facts.append("the listing of the unlisted lane failed: "
                         + _first_line(first.stderr or first.stdout))
            facts.append("the listing of the listed lane worked")
        else:
            subs.append((UNKNOWN, "both listings failed, so the probe cannot "
                                  "tell the two lanes apart"))
    return run.finish(subs, facts)


def cmd_delete_probe(ctx: Ctx) -> Cmd:
    return "delete-" + PROBE_NAME, openshell(ctx, "sandbox", "delete",
                                             PROBE_NAME)


def restore_receiver(ctx: Ctx, run: CheckRun) -> Optional[str]:
    """Removes the refusing setting and restarts the receiver. None on success,
    otherwise why not."""
    B = ctx.receiver
    r = run.run(cmd_py(ctx, "remove-refuse", B, REMOVE_REFUSE, REFUSE_FILE,
                       REFUSE_TEXT))
    if r.code != 0 or r.stdout.strip() not in ("removed", "absent"):
        return (f"the setting could not be removed from {B}: "
                f"{_first_line(r.stderr or r.stdout)}")
    return bounce(ctx, run.run, B)


def at_or_under(path: str, parent: str) -> bool:
    p = [c for c in path.split("/") if c]
    q = [c for c in parent.split("/") if c]
    return p[:len(q)] == q


def _negative_case(ctx: Ctx, run: CheckRun) -> Tuple[Sub, List[str]]:
    """The refusing receiver. The setting is written with O_EXCL, and beta is
    put back in a `finally`."""
    S, B = ctx.sender, ctx.receiver
    facts: List[str] = []
    state = run.run(cmd_py(ctx, "refuse-state", B, REFUSE_STATE, REFUSE_FILE,
                           REFUSE_TEXT))
    seen = state.stdout.strip()
    if state.code != 0 or seen != "absent":
        return (UNKNOWN, f"{B} already has a settings.local.json that the "
                         f"runner did not leave ({seen or 'unreadable'})"), facts
    wrote = run.run(cmd_py(ctx, "write-refuse", B, WRITE_REFUSE, REFUSE_FILE,
                           REFUSE_TEXT))
    if wrote.code != 0 or wrote.stdout.strip() != "written":
        return (UNKNOWN, f"the refusing setting could not be written in {B}: "
                         f"{_first_line(wrote.stderr or wrote.stdout)}"), facts
    sub: Sub = (UNKNOWN, "the negative case did not finish")
    try:
        problem = bounce(ctx, run.run, B)
        if problem:
            sub = (UNKNOWN, f"{B} did not come back with the setting: "
                            f"{problem}")
        else:
            req_id = find_planted(ctx, S, NEGATIVE_SUBJECT)
            if req_id and _already_answered(ctx, req_id):
                # An earlier round's request has its outcome: it says nothing
                # about the receiver as it is now.
                req_id = None
            if not req_id:
                r = run.run(cmd_submit(ctx, "submit-negative", S, B,
                                       NEGATIVE_SUBJECT, NEGATIVE_BODY))
                req_id = _submitted_id(r) if r.code == 0 else None
            if not req_id:
                sub = (UNKNOWN, "the negative delegation was not planted")
            else:
                sub = _await_negative(ctx, run, req_id, facts)
    finally:
        problem = restore_receiver(ctx, run)
        if problem:
            note = f"the receiver was not restored: {problem}"
            facts.append(note)
            run.shared.setdefault("notes", []).append(note)
    return sub, facts


def _already_answered(ctx: Ctx, req_id: str) -> bool:
    """Whether the receiver's daemon has already reported an outcome for the
    notice the router placed for `req_id`."""
    events = audit_events(ctx, ctx.receiver)
    placed = find_event(events, EVENT_NOTICE_PLACED,
                        from_instance=ctx.sender, req_id=req_id)
    return placed is not None and find_event(
        events, EVENT_OUTCOME, notice_id=placed.get("notice_id")) is not None


def _await_negative(ctx: Ctx, run: CheckRun, req_id: str, facts: List[str]
                    ) -> Sub:
    S, B = ctx.sender, ctx.receiver
    limit = ctx.settings.delivery_timeout
    placed = wait_until(ctx, lambda: find_event(
        audit_events(ctx, B), EVENT_NOTICE_PLACED, from_instance=S,
        req_id=req_id), limit)
    if placed is None:
        return UNKNOWN, f"no outcome recorded within {limit:g} s (no notice)"
    run.observe("audit-negative-placed", audit_path(ctx, B), placed)
    notice = placed.get("notice_id")
    outcome = wait_until(ctx, lambda: find_event(
        audit_events(ctx, B), EVENT_OUTCOME, notice_id=notice), limit)
    if outcome is None:
        return UNKNOWN, f"no outcome recorded within {limit:g} s"
    run.observe("audit-negative-outcome", audit_path(ctx, B), outcome)
    got = outcome.get("outcome")
    facts.append(f"negative case outcome: {got}")
    if got in ("refused", "held"):
        return PASS, f"with the receiver set to refuse the outcome is {got}"
    if got == "delivered":
        return FAIL, ("the outcome is delivered with the receiver set to "
                      "refuse: the daemon counted silence as receipt")
    return UNKNOWN, f"the outcome {got!r} is neither refused, held nor delivered"


def check_injection(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    d, missing = _need_delegation(ctx)
    B = ctx.receiver
    subs: List[Sub] = []
    facts: List[str] = []
    if d is None:
        subs.append(missing)
    else:
        consumed = find_event(audit_events(ctx, B), EVENT_OUTCOME,
                              notice_id=d.notice_id)
        run.observe("audit-outcome", audit_path(ctx, B), consumed)
        if consumed is None:
            subs.append((UNKNOWN, f"{B}'s audit log has no outcome for the "
                                  f"delegation"))
        elif consumed.get("outcome") != "delivered":
            subs.append((FAIL, f"the delegation's outcome is "
                               f"{consumed.get('outcome')!r}"))
        else:
            facts.append("positive case outcome: delivered")
            r = run.run(cmd_ready(ctx, B))
            row = ready_row(probe_rows(r.stdout)) if r.code == 0 else None
            if row is None:
                subs.append((UNKNOWN, f"{B} shows no single session with a "
                                      f"socket"))
            else:
                directory = posixpath.dirname(row[4])
                under = [p for p in render.SYSTEM_READ_WRITE
                         if not p.startswith("/dev/")
                         and at_or_under(directory, p)]
                if under:
                    subs.append((PASS, "the delegation was delivered and the "
                                       "socket directory is under a "
                                       "read-write path"))
                    facts.append(f"the socket directory is under {under[0]}")
                else:
                    subs.append((FAIL, "the socket directory is not under a "
                                       "read-write path of the policy"))
    negative, more = _negative_case(ctx, run)
    facts.extend(more)
    run.shared["negative"] = negative
    subs.append(negative)
    return run.finish(subs, facts)


def check_receiver_setting(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    B = ctx.receiver
    subs: List[Sub] = []
    facts: List[str] = []
    r = run.run(cmd_py(ctx, "write-probe-payload", B, WRITE_PROBE,
                       PAYLOAD_SETTINGS, "append"))
    seen = r.stdout.strip()
    bad = _exec_sub(r, "the write probe")
    if bad:
        subs.append(bad)
    elif seen == "OPENED":
        subs.append((FAIL, "the agent can open the payload's settings file "
                           "for writing"))
    elif ERRNO_RE.match(seen):
        subs.append((PASS, "the payload's settings file is not writable from "
                           "inside"))
        facts.append(f"a write to the payload's settings file fails: {seen}")
    else:
        subs.append((UNKNOWN, f"the write probe printed {seen!r}"))

    d, missing = _need_delegation(ctx)
    if d is None:
        subs.append(missing)
    elif d.outcome == "delivered":
        subs.append((PASS, "step 7 was delivered under accept"))
    else:
        subs.append((UNKNOWN, "step 7 has no delivered outcome"))

    negative = run.shared.get("negative")
    if negative is None:
        subs.append((UNKNOWN, "Unknown 4's negative case did not run"))
    elif negative[0] == PASS:
        subs.append((PASS, "tightening through settings.local.json gave "
                           "refused or held"))
    else:
        subs.append((negative[0], "Unknown 4's negative case: " + negative[1]))

    listing = run.run(cmd_exec(ctx, "ls-claude-dir", B, "ls", "-la", CLAUDE_DIR))
    facts.append("listing of the agent's settings directory: "
                 + (_first_line(listing.stdout) if listing.code == 0
                    else f"failed ({_first_line(listing.stderr)})"))
    writable = run.run(cmd_py(ctx, "home-write-probe", B, HOME_WRITE_PROBE,
                              CLAUDE_DIR))
    facts.append(f"the agent's user creating a file in {CLAUDE_DIR}: "
                 f"{writable.stdout.strip() or 'no answer'}")
    facts.append(f"whether {posixpath.join(CLAUDE_DIR, 'settings.json')} "
                 f"changes the value: UNKNOWN (not exercised by the runner)")
    return run.finish(subs, facts)


def check_posture(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    findings = posture_findings(ctx, run.run, ctx.members)
    facts = [f"{'ok' if ok else 'FAIL'}: {text}" for ok, text in findings]
    if all(ok for ok, _ in findings):
        return run.finish([(PASS, "the gateway file, the listener and the "
                                  "sandbox list all match D6")], facts)
    bad = "; ".join(text for ok, text in findings if not ok)
    return run.finish([(FAIL, bad)], facts)


def _policy_run_as(ctx: Ctx, name: str) -> Optional[str]:
    text = _read(l1_kit.policy_file(ctx.home, name))
    user = re.search(r'^\s*run_as_user:\s*"?(\d+)"?\s*$', text, re.M)
    group = re.search(r'^\s*run_as_group:\s*"?(\d+)"?\s*$', text, re.M)
    if not user or not group:
        return None
    return f"{user.group(1)}:{group.group(1)}"


def check_shared_uid(ctx: Ctx, run: CheckRun) -> CheckOutcome:
    d, missing = _need_delegation(ctx)
    S, B = ctx.sender, ctx.receiver
    want = ctx.run_as_text
    subs: List[Sub] = []
    facts: List[str] = [f"the operator's uid:gid: {want}"]

    def compare(what: str, got: Optional[str]) -> None:
        if got is None:
            subs.append((UNKNOWN, f"{what} could not be read"))
        elif got == want:
            subs.append((PASS, f"{what} is {want}"))
            facts.append(f"{what}: {got}")
        else:
            subs.append((FAIL, f"{what} is {got}, not {want}"))
            facts.append(f"{what}: {got}")

    def owner(path: str) -> Optional[str]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{st.st_uid}:{st.st_gid}"

    seen = {
        f"the host owner of {S}'s outbox": owner(lane_host(ctx, S, LANE_OUTBOX)),
        f"the host owner of {B}'s peer notices": owner(
            lane_host(ctx, B, LANE_PEER, NOTICES)),
    }
    for m in ctx.members:
        seen[f"{m}'s policy run_as"] = _policy_run_as(ctx, m)
    run.observe("host-ids", "os.stat and the rendered policies", seen)
    for what, got in seen.items():
        compare(what, got)

    r = run.run(("inspect-router-user", ["docker", "inspect", "--format",
                                         "{{.Config.User}}", ROUTER_CONTAINER]))
    compare("the router container's user",
            r.stdout.strip() if r.code == 0 and r.stdout.strip() else None)
    for m in (S, B):
        r = run.run(cmd_py(ctx, f"ids-{m}", m, IDS_PROBE))
        compare(f"the uid:gid inside {m}",
                r.stdout.strip() if r.code == 0 and r.stdout.strip() else None)

    if d is None:
        subs.append(missing)
    else:
        peer = render.lane_target(LANE_PEER)
        name = f"notice-{d.notice_id}.json"
        r = run.run(cmd_exec(ctx, "stat-notice", B, "stat", "-c", "%u:%g %a",
                             posixpath.join(peer, NOTICES, name)))
        got = r.stdout.strip() if r.code == 0 else None
        if got is None:
            subs.append((FAIL, f"{B} cannot stat the notice the router wrote"))
        elif got == f"{want} 600":
            subs.append((PASS, f"the notice is {want} with mode 600"))
            facts.append(f"the notice inside {B}: {got}")
        else:
            subs.append((FAIL, f"the notice inside {B} is {got}, not "
                               f"{want} 600"))
        r = run.run(cmd_exec(ctx, "cat-message", B, "cat",
                             posixpath.join(peer, MESSAGES, name)))
        if r.code == 0 and isinstance(parse_json(r.stdout), dict):
            subs.append((PASS, f"{B} reads the message the router wrote"))
            facts.append(f"{B} read the delivered message")
        else:
            subs.append((FAIL, f"{B} cannot read the message the router "
                               f"wrote: {_first_line(r.stderr or r.stdout)}"))
    return run.finish(subs, facts)


CHECKS = (
    Check("Pass criterion 1", "validate.py gates every document",
          check_documents, None),
    Check("Pass criterion 2", "the connector's operational checks",
          check_connector_operational, None),
    Check("Pass criterion 3",
          "a write to the inbox lane is denied at both layers",
          check_inbox_write_denied, None),
    Check("Pass criterion 4",
          "a network call to a mail API is denied and logged",
          check_network_denied, None),
    Check("Pass criterion 5", "beta tasking alpha is held at the router",
          check_reverse_task_held, None),
    Check("Unknown 1", "router writes appear inside a running sandbox",
          check_router_writes_visible, None),
    Check("Unknown 2", "the errno of a write to the inbox lane",
          check_inbox_errno, None),
    Check("Unknown 3", "a lane mounted but missing from the policy",
          check_unlisted_lane, None),
    Check("Unknown 4", "injection over AF_UNIX, and the refusing receiver",
          check_injection, None),
    Check("Unknown 5", "where the cross-session receiver setting is fixed",
          check_receiver_setting, None),
    Check("Unknown 6", "the accepted gateway posture", check_posture, None),
    Check("Unknown 7", "the shared uid holds end to end", check_shared_uid,
          None),
)

EXECUTION_ORDER = ("Unknown 6", "Pass criterion 3", "Unknown 2",
                   "Pass criterion 4", "Unknown 1", "Unknown 7",
                   "Pass criterion 2", "Pass criterion 5", "Unknown 3",
                   "Unknown 4", "Unknown 5", "Pass criterion 1")


def _cmds_get(ctx: Ctx, name: str) -> List[Cmd]:
    return [cmd_get(ctx, name), cmd_confirm_absent(ctx, name)]


def _cc_documents(ctx: Ctx) -> List[Cmd]:
    return []


def _cc_operational(ctx: Ctx) -> List[Cmd]:
    S, B = ctx.sender, ctx.receiver
    return [cmd_submit_result(ctx, S, ph("req-id")), cmd_ready(ctx, S),
            cmd_ready(ctx, B)]


def _cc_inbox_write(ctx: Ctx) -> List[Cmd]:
    S = ctx.sender
    return [cmd_exec(ctx, "touch-inbox", S, "touch",
                     f"{render.lane_target(LANE_INBOX)}/probe"),
            ("docker-ps-sandbox", ["docker", "ps", "-q", "--filter",
                                   f"label={SANDBOX_ID_LABEL}="
                                   + ph("sandbox-id")]),
            ("inspect-mounts-1", ["docker", "inspect", "--format",
                                  "{{json .Mounts}}", ph("container")]),
            ("policy-only", policy_only_argv(ctx, S))]


def _cc_network(ctx: Ctx) -> List[Cmd]:
    S = ctx.sender
    return [cmd_py(ctx, "network-probe", S, NETWORK_PROBE, MAIL_PROBE_URL),
            ("sandbox-logs", openshell(ctx, "logs", S, "--source", "sandbox",
                                       "--level", "info", "-n", "100"))]


def _cc_reverse(ctx: Ctx) -> List[Cmd]:
    S, B = ctx.sender, ctx.receiver
    return [cmd_submit(ctx, "submit-reverse", B, S, REVERSE_SUBJECT,
                       REVERSE_BODY),
            cmd_submit_result(ctx, B, ph("req-id"))]


def _cc_visible(ctx: Ctx) -> List[Cmd]:
    return [cmd_exec(ctx, "ls-peer-notices", ctx.receiver, "ls", "-1",
                     posixpath.join(render.lane_target(LANE_PEER), NOTICES))]


def _cc_errno(ctx: Ctx) -> List[Cmd]:
    return [cmd_py(ctx, "write-probe-inbox", ctx.sender, WRITE_PROBE,
                   f"{render.lane_target(LANE_INBOX)}/probe", "create")]


def _cc_unlisted(ctx: Ctx) -> List[Cmd]:
    peer = render.lane_target(LANE_PEER)
    inbox = render.lane_target(LANE_INBOX)
    return [*_cmds_get(ctx, PROBE_NAME),
            ("delete-" + PROBE_NAME + "-stale",
             openshell(ctx, "sandbox", "delete", PROBE_NAME)),
            ("create-" + PROBE_NAME,
             probe_create_argv(ctx, probe_policy_path(ctx))),
            cmd_get(ctx, PROBE_NAME),
            cmd_exec(ctx, "ls-probe-peer", PROBE_NAME, "ls", peer),
            cmd_exec(ctx, "ls-probe-inbox", PROBE_NAME, "ls", inbox),
            cmd_delete_probe(ctx)]


def _cc_injection(ctx: Ctx) -> List[Cmd]:
    B = ctx.receiver
    return [cmd_ready(ctx, B),
            cmd_py(ctx, "refuse-state", B, REFUSE_STATE, REFUSE_FILE,
                   REFUSE_TEXT),
            cmd_py(ctx, "write-refuse", B, WRITE_REFUSE, REFUSE_FILE,
                   REFUSE_TEXT),
            cmd_stop(ctx, B), cmd_phase(ctx, B), cmd_start(ctx, B),
            cmd_ready(ctx, B),
            cmd_submit(ctx, "submit-negative", ctx.sender, B, NEGATIVE_SUBJECT,
                       NEGATIVE_BODY),
            cmd_py(ctx, "remove-refuse", B, REMOVE_REFUSE, REFUSE_FILE,
                   REFUSE_TEXT)]


def _cc_setting(ctx: Ctx) -> List[Cmd]:
    B = ctx.receiver
    return [cmd_py(ctx, "write-probe-payload", B, WRITE_PROBE,
                   PAYLOAD_SETTINGS, "append"),
            cmd_exec(ctx, "ls-claude-dir", B, "ls", "-la", CLAUDE_DIR),
            cmd_py(ctx, "home-write-probe", B, HOME_WRITE_PROBE, CLAUDE_DIR)]


def _cc_posture(ctx: Ctx) -> List[Cmd]:
    return [cmd_status(), cmd_preflight(ctx), cmd_listeners(), cmd_list(ctx)]


def _cc_uid(ctx: Ctx) -> List[Cmd]:
    S, B = ctx.sender, ctx.receiver
    peer = render.lane_target(LANE_PEER)
    name = "notice-" + ph("notice-id") + ".json"
    return [("inspect-router-user", ["docker", "inspect", "--format",
                                     "{{.Config.User}}", ROUTER_CONTAINER]),
            cmd_py(ctx, f"ids-{S}", S, IDS_PROBE),
            cmd_py(ctx, f"ids-{B}", B, IDS_PROBE),
            cmd_exec(ctx, "stat-notice", B, "stat", "-c", "%u:%g %a",
                     posixpath.join(peer, NOTICES, name)),
            cmd_exec(ctx, "cat-message", B, "cat",
                     posixpath.join(peer, MESSAGES, name))]


CHECK_COMMANDS: Dict[str, Callable[[Ctx], List[Cmd]]] = {
    "Pass criterion 1": _cc_documents,
    "Pass criterion 2": _cc_operational,
    "Pass criterion 3": _cc_inbox_write,
    "Pass criterion 4": _cc_network,
    "Pass criterion 5": _cc_reverse,
    "Unknown 1": _cc_visible,
    "Unknown 2": _cc_errno,
    "Unknown 3": _cc_unlisted,
    "Unknown 4": _cc_injection,
    "Unknown 5": _cc_setting,
    "Unknown 6": _cc_posture,
    "Unknown 7": _cc_uid,
}


def run_check(ctx: Ctx, check: Check, shared: Dict[str, Any]) -> CheckOutcome:
    run = CheckRun(ctx, check.heading, shared)
    if check.run is None:
        return CheckOutcome(UNKNOWN, check.unknown_reason or "not checked", (),
                            ())
    try:
        return check.run(ctx, run)
    except Exception as e:   # a check never stops the loop
        return CheckOutcome(UNKNOWN, f"the check could not run: "
                                     f"{type(e).__name__}: {e}", (),
                            tuple(dict.fromkeys(run.records)))


def step_8(ctx: Ctx) -> StepResult:
    assert ctx.evidence is not None
    ev = ctx.evidence
    existing = ev.read_state(CHECKS_STATE)
    if existing is not None:
        ctx.checks = list(existing.get("checks", []))
        return StepResult(SKIPPED, (
            "remove $AMAP_OPENSHELL_HOME/evidence/step-8/checks.json to run "
            "it again",))
    shared: Dict[str, Any] = {}
    notes: List[str] = []
    ids = recorded_ids(ctx)
    if ids is not None:
        ctx.ids = ids

    # A refusing setting left by an interrupted run is put back first.
    pre = CheckRun(ctx, None, shared)
    left = pre.run(cmd_py(ctx, "refuse-state-precheck", ctx.receiver,
                          REFUSE_STATE, REFUSE_FILE, REFUSE_TEXT))
    if left.stdout.strip() == "ours":
        problem = restore_receiver(ctx, pre)
        notes.append(f"{ctx.receiver} was left refusing by an interrupted "
                     f"run: " + ("restored" if problem is None
                                 else f"not restored: {problem}"))

    by_heading = {c.heading: c for c in CHECKS}
    outcomes: Dict[str, CheckOutcome] = {}
    for heading in EXECUTION_ORDER:
        outcomes[heading] = run_check(ctx, by_heading[heading], shared)
    doc = []
    for check in CHECKS:
        o = outcomes[check.heading]
        doc.append({"heading": check.heading, "title": check.title,
                    "outcome": o.outcome, "reason": o.reason,
                    "facts": list(o.facts), "evidence": list(o.evidence)})
        notes.append(f"{check.heading}: {o.outcome} ({o.reason})")
    notes.extend(shared.get("notes", []))
    ev.write_state(CHECKS_STATE, {"run": ev.run_id, "checks": doc})
    ctx.checks = doc
    return StepResult(DONE, tuple(notes))


# --- the report --------------------------------------------------------------

_TEMPLATE_FENCE = re.compile(r"^```markdown[ \t]*\n(.*?)^```[ \t]*$",
                             re.M | re.S)


def report_template() -> str:
    """The one fenced markdown block of the runbook."""
    text = RUNBOOK.read_text(encoding="utf-8")
    blocks = _TEMPLATE_FENCE.findall(text)
    if len(blocks) != 1:
        raise RunnerError("the runbook must hold exactly one markdown "
                          "template block")
    return blocks[0]


def not_checked_items(spec_dir: str) -> List[str]:
    """The items of validate.py's NOT CHECKED HERE list: each `- ` bullet with
    its continuation lines, whitespace-normalized. This is the runbook's rule
    for the NOT-CHECKED table."""
    with open(posixpath.join(spec_dir, "fixtures", "validate.py"),
              encoding="utf-8") as fh:
        text = fh.read()
    block = text[text.index("NOT CHECKED HERE"):].split("\n\n")[0]
    items: List[List[str]] = []
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append([stripped[2:]])
        elif items and stripped:
            items[-1].append(stripped)
    return [" ".join(" ".join(parts).split()) for parts in items]


class Sanitizer:
    """Replaces host paths with the variables the runbook names."""

    def __init__(self, ctx: Ctx) -> None:
        pairs = [(ctx.home, "$AMAP_OPENSHELL_HOME"),
                 (str(ctx.router_repo), "$AMAP_ROUTER_REPO"),
                 (str(ctx.connector_repo), "$AMAP_CONNECTOR_REPO"),
                 (ctx.spec_dir, "$AMAP_SPEC_DIR"),
                 (str(REPO), "this repository"),
                 (ctx.gateway_toml, "$XDG_CONFIG_HOME/openshell/gateway.toml"),
                 (ctx.env.get("HOME", ""), "$HOME")]
        self.pairs = sorted((p for p in pairs if len(p[0]) > 1),
                            key=lambda p: len(p[0]), reverse=True)

    def __call__(self, text: str) -> str:
        for path, name in self.pairs:
            text = text.replace(path, name)
        return text


def _plain(text: str) -> str:
    """One line, with no angle brackets: the generated text of the draft."""
    return " ".join(text.replace("<", "(").replace(">", ")").split())


def _outcome_of(checks: Mapping[str, Mapping[str, Any]], heading: str) -> str:
    return str(checks.get(heading, {}).get("outcome", UNKNOWN))


def _write_up(facts: Mapping[str, Any], checks: Mapping[str, Mapping[str, Any]],
              delegation: Optional[Delegation]) -> str:
    sender, receiver = facts.get("sender", "the sender"), \
        facts.get("receiver", "the receiver")
    parts = []
    if delegation is None:
        parts.append("No delegation is recorded, so nothing was delivered or "
                     "answered.")
    else:
        parts.append(f"{sender} delegated a task to {receiver} through the "
                     f"connector's inbox-submit CLI, the drop-box the agent's "
                     f"tool writes.")
        parts.append(f"The router placed a notice in {receiver}'s peer lane, "
                     f"and {receiver}'s daemon reported the outcome "
                     f"{delegation.outcome or 'none'}.")
        if delegation.state == "complete":
            parts.append(f"{receiver}'s reply reached {sender}'s peer lane.")
        else:
            parts.append(f"The delegation stopped at {delegation.state}: no "
                         f"reply reached {sender}.")
    not_pass = [h for h in (c.heading for c in CHECKS)
                if _outcome_of(checks, h) != PASS]
    if not_pass:
        parts.append("Not PASS: " + ", ".join(
            f"{h} ({_outcome_of(checks, h)})" for h in not_pass) + ".")
    else:
        parts.append("Every check the runner made is PASS.")
    parts.append("The outcomes below are the runner's, from the evidence "
                 "directory; the operator reviews each before this draft "
                 "becomes the report.")
    return _plain(" ".join(parts))


POSTURE_SENTENCE = ("a dedicated gateway, loopback only, no OIDC, one "
                    "operator, no other sandboxes. The risk accepted: any "
                    "caller that may create a sandbox on it can bind-mount "
                    "any host path the container runtime can see.")


def draft_report(template: str, facts: Mapping[str, Any],
                 checks: Sequence[Mapping[str, Any]], items: Sequence[str],
                 sanitize: Callable[[str], str],
                 redact: Callable[[str], str]) -> str:
    """The template with every placeholder filled from the evidence. Nothing
    is a count. The operator's own columns and tags are left for the
    operator."""
    by_heading = {str(c["heading"]): c for c in checks}
    delegation = _delegation(facts.get("delegation"))
    lines = template.splitlines()
    out: List[str] = []
    current: Optional[str] = None
    i = 0
    headers = {
        "Date: ": facts.get("date", ""),
        "Host: ": f"Linux {facts.get('kernel', '')}, Docker "
                  f"{facts.get('docker', '')}",
        "OpenShell: ": facts.get("openshell", ""),
        "Claude Code: ": facts.get("claude_code", ""),
        "Fleet: ": f"examples/fleet.json: {facts.get('sender')} may task "
                   f"{facts.get('receiver')}; {facts.get('receiver')} may "
                   f"not task {facts.get('sender')}",
    }
    while i < len(lines):
        line = lines[i]
        if i == 0:
            out += [line, "", "Draft written by amap-openshell.py l1-run from "
                              "$AMAP_OPENSHELL_HOME/evidence. Review every "
                              "line before moving it to docs/POC-REPORT.md."]
            i += 1
            continue
        header = next((h for h in headers if line.startswith(h)), None)
        if header is not None:
            out.append(header + _plain(str(headers[header])))
        elif line.startswith("Accepted gateway posture (D6, Decision 1): <"):
            while not lines[i].rstrip().endswith(">"):
                i += 1
            posture = _outcome_of(by_heading, "Unknown 6")
            out.append("Accepted gateway posture (D6, Decision 1): "
                       + POSTURE_SENTENCE + " Checked by l1-run as Unknown 6: "
                       + posture + ".")
        elif line.startswith("<A short write-up"):
            out.append(_write_up(facts, by_heading, delegation))
        elif line.startswith("<Each tag moved"):
            while not lines[i].rstrip().endswith(">"):
                i += 1
            out.append("Not filled in by the runner. Move a tag only with "
                       "evidence from this run, and cite it.")
        elif line.startswith("### "):
            heading = line[4:].split(":", 1)[0]
            current = heading if heading in by_heading else None
            out.append(line)
        elif line.startswith("Outcome: <") and current:
            out.append("Outcome: " + _plain(str(by_heading[current]["outcome"])))
        elif line.startswith("Evidence: <") and current:
            c = by_heading[current]
            pieces = [str(c.get("reason") or "")] + [str(f) for f in
                                                     c.get("facts", [])]
            files = c.get("evidence", [])
            if files:
                pieces.append("evidence files: " + ", ".join(files))
            out.append("Evidence: " + "; ".join(
                _plain(p) for p in pieces if p.strip()))
        elif line.startswith("| <item> |"):
            for item in items:
                out.append("| " + item.replace("|", "\\|") + " | | | | "
                           "UNKNOWN: for the operator to fill in from the "
                           "evidence |")
        else:
            out.append(line)
        i += 1
    return sanitize(redact("\n".join(out) + "\n"))


def step_9(ctx: Ctx) -> StepResult:
    assert ctx.evidence is not None
    ev = ctx.evidence
    if ev.exists(REPORT_RELPATH):
        return StepResult(SKIPPED, ("evidence/POC-REPORT.md exists: remove it "
                                    "to redraft",))
    stored = ev.read_state(CHECKS_STATE)
    if stored is None:
        return StepResult(FAIL, ("step 8 has not run: evidence/step-8/"
                                 "checks.json is missing",))
    facts = dict(ev.read_state(FACTS_STATE) or {})
    facts["date"] = ctx.now().strftime("%Y-%m-%d")
    facts["delegation"] = ev.read_state(DELEGATION_STATE)
    template = report_template()
    text = draft_report(template, facts, stored.get("checks", []),
                        not_checked_items(ctx.spec_dir), Sanitizer(ctx),
                        ctx.redactor.text)
    leaks = [s for s in ctx.redactor.secrets if s in text]
    stale = [p for p in re.findall(r"<[^<>]+>", template, re.S) if p in text]
    if leaks or stale:
        return StepResult(FAIL, ("the draft holds a secret or an unfilled "
                                 "placeholder, so it was not written",))
    if not ev.write_new(REPORT_RELPATH, text):
        return StepResult(SKIPPED, ("evidence/POC-REPORT.md exists: remove it "
                                    "to redraft",))
    return StepResult(DONE, ("wrote $AMAP_OPENSHELL_HOME/evidence/"
                             "POC-REPORT.md; review it, then move it to "
                             "docs/POC-REPORT.md",))


STEP_FUNCS: Dict[int, Callable[[Ctx], StepResult]] = {
    0: step_0, 1: step_1, 2: step_2, 3: step_3, 4: step_4, 5: step_5,
    6: step_6, 7: step_7, 8: step_8, 9: step_9,
}


# --- the dry run -------------------------------------------------------------

def show(ctx: Ctx, cmd: Cmd) -> None:
    ctx.say(f"+ {display_argv(cmd[1])}  # {cmd[0]}")


def would_wait(ctx: Ctx, seconds: float, what: str) -> None:
    ctx.say(f"  would wait up to {seconds:g} s for {what}")


def describe_step(n: int, ctx: Ctx) -> StepResult:
    """A dry run's view of step `n`: its commands, with a placeholder for every
    value that is not known yet. Nothing is run and nothing is written."""
    s = ctx.settings
    S, B = ctx.sender, ctx.receiver
    if n == 0:
        inputs, findings = load_inputs(ctx.env, ctx.uid_gid())
        notes = tuple(format_finding(ok, text) for ok, text in findings)
        if inputs is None:
            return StepResult(FAIL, notes)
        ctx.adopt(inputs)
        show(ctx, cmd_openshell_version())
        show(ctx, cmd_docker_version())
        return StepResult(PASS, notes)
    if n == 1:
        show(ctx, cmd_image_inspect(ctx.image))
        ctx.say("  skipped when the image already carries these labels")
        show(ctx, cmd_build(ctx))
    elif n == 2:
        for cmd in _cc_posture(ctx):
            show(ctx, cmd)
    elif n == 3:
        show(ctx, cmd_prepare(ctx, False))
        ctx.say("  then, unless every path is present:")
        show(ctx, cmd_prepare(ctx, True))
        show(ctx, cmd_image_paths(ctx.image))
        show(ctx, cmd_profile_kit(ctx, (ph("node path"), ph("claude path")),
                                  False))
        ctx.say("  then, unless the profile file is present:")
        show(ctx, cmd_profile_kit(ctx, (ph("node path"), ph("claude path")),
                                  True))
        show(ctx, cmd_profile_list(ctx))
        ctx.say("  then, when the gateway has no profile with this ID:")
        show(ctx, cmd_profile_lint(ctx))
        show(ctx, cmd_profile_import(ctx))
        ctx.say("  or, when its copy differs, with the copy's "
                "resource_version:")
        show(ctx, cmd_profile_update(ctx, ph("update document")))
        show(ctx, cmd_profile_list(ctx, True))
    elif n == 4:
        for name in ctx.members:
            for cmd in _cmds_get(ctx, name):
                show(ctx, cmd)
            show(ctx, cmd_create(ctx, name))
        for name in ctx.members:
            show(ctx, cmd_ready(ctx, name))
            would_wait(ctx, s.ready_timeout, f"{name} to show one ready "
                                             f"session")
    elif n == 5:
        for name in ctx.members:
            show(ctx, cmd_get(ctx, name))
            show(ctx, cmd_record(ctx, name, ph(f"{name} id"), False))
            show(ctx, cmd_record(ctx, name, ph(f"{name} id"), True))
    elif n == 6:
        show(ctx, cmd_ps_router())
        show(ctx, cmd_router_build(ctx))
        show(ctx, cmd_router_run(ctx))
        show(ctx, cmd_ps_router_after())
        would_wait(ctx, s.router_timeout, "the router to publish its roster")
        ctx.say("  while it waits, it reads the container's state, and stops "
                "at once if it is restarting, exited or dead, after reading:")
        show(ctx, cmd_ps_router_wait())
        show(ctx, cmd_router_exit_code())
        show(ctx, cmd_router_log_tail())
        show(ctx, cmd_router_logs())
    elif n == 7:
        show(ctx, cmd_ps_router())
        show(ctx, cmd_submit(ctx, "submit-delegation", S, B, DELEGATION_SUBJECT,
                             DELEGATION_BODY))
        would_wait(ctx, s.delivery_timeout, f"the router's placement in {B}'s "
                                            f"peer lane")
        would_wait(ctx, s.delivery_timeout, f"{B}'s outcome, delivered")
        would_wait(ctx, s.reply_timeout, f"{B}'s reply in {S}'s peer lane")
    elif n == 8:
        show(ctx, cmd_py(ctx, "refuse-state-precheck", B, REFUSE_STATE,
                         REFUSE_FILE, REFUSE_TEXT))
        titles = {c.heading: c.title for c in CHECKS}
        for heading in EXECUTION_ORDER:
            ctx.say(f"  {heading}: {titles[heading]}")
            for cmd in CHECK_COMMANDS[heading](ctx):
                show(ctx, cmd)
            if heading == "Pass criterion 4":
                would_wait(ctx, s.log_timeout, f"the sandbox log to name "
                                               f"{MAIL_PROBE_HOST}")
            if heading == "Pass criterion 5":
                would_wait(ctx, s.delivery_timeout, "the router's held copy")
            if heading == "Unknown 4":
                would_wait(ctx, s.delivery_timeout, f"{B}'s outcome, refused "
                                                    f"or held")
                ctx.say(f"  then, in a finally, {B} is put back")
                show(ctx, cmd_stop(ctx, B))
                show(ctx, cmd_start(ctx, B))
    elif n == 9:
        ctx.say("  would write $AMAP_OPENSHELL_HOME/evidence/POC-REPORT.md")
    return StepResult(DRY, ())


# --- selection and the runner ------------------------------------------------

def selected_steps(from_step: Optional[int], only_step: Optional[int]
                   ) -> Tuple[int, ...]:
    if only_step is not None:
        return (only_step,)
    if from_step is not None:
        return tuple(range(from_step, len(STEPS)))
    return STEPS


def steps_to_run(selected: Sequence[int]) -> Tuple[int, ...]:
    """Step 0 always runs. Step 2 runs whenever a step from 3 to 8 does."""
    wanted = {0} | set(selected)
    if any(s in POSTURE_GATED for s in selected):
        wanted.add(2)
    return tuple(sorted(wanted))


def execute(ctx: Ctx, n: int) -> StepResult:
    """Runs step `n` in apply mode. An unexpected exception is a FAIL, with its
    type and message redacted. The result is written to the evidence."""
    try:
        result = STEP_FUNCS[n](ctx)
    except Exception as e:
        result = StepResult(FAIL, (f"unexpected {type(e).__name__}: {e}",))
    if ctx.evidence is not None:
        ctx.evidence.step_result(n, STEP_TITLES[n], result)
    return result


def refusal(n: int, result: StepResult) -> str:
    bad = [note[len("FAIL "):].strip() for note in result.notes
           if note.startswith("FAIL ")]
    if n == 2:
        return ("refusing to go on: decision D6's posture does not hold"
                + (": " + "; ".join(bad) if bad else ""))
    if bad:
        return "refusing to go on: " + "; ".join(bad)
    return f"refusing to go on: step {n} failed"


class Runner:
    def __init__(self, env: Mapping[str, str], apply: bool,
                 selected: Sequence[int], settings: Settings = Settings(),
                 out: Any = sys.stdout,
                 now: Callable[[], datetime.datetime] = _utc_now,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 ids: Callable[[], Tuple[int, int]] = lambda: (os.getuid(),
                                                               os.getgid())
                 ) -> None:
        self.apply = apply
        self.selected = tuple(selected)
        self.ctx = Ctx(env, apply, settings, out, clock, sleep, now, ids)

    def run(self) -> int:
        ctx = self.ctx
        ran: Dict[int, StepResult] = {}
        for n in steps_to_run(self.selected):
            ctx.say(f"step {n}: {STEP_TITLES[n]}")
            result = execute(ctx, n) if self.apply else describe_step(n, ctx)
            ran[n] = result
            ctx.say(f"step {n}: {result.outcome}")
            for note in result.notes:
                ctx.say(f"  {note}")
            if result.outcome == FAIL:
                ctx.say(refusal(n, result) if n in GATE_STEPS
                        else f"stopping: step {n} failed")
                return EXIT_FAILED
        if not self.apply:
            ctx.say("dry run: nothing was run or written; add --apply to run")
            return EXIT_OK
        if 8 in ran and any(c.get("outcome") != PASS for c in ctx.checks):
            return EXIT_CHECKS
        return EXIT_OK


def evidence_dirs() -> List[str]:
    return [EVIDENCE_DIRNAME] + [f"{EVIDENCE_DIRNAME}/step-{n}" for n in STEPS]


def evidence_files() -> List[str]:
    return [f"{EVIDENCE_DIRNAME}/{RUNNER_LOG}",
            f"{EVIDENCE_DIRNAME}/{REPORT_NAME}",
            f"{EVIDENCE_DIRNAME}/{FACTS_STATE}",
            f"{EVIDENCE_DIRNAME}/{DELEGATION_STATE}",
            f"{EVIDENCE_DIRNAME}/{CHECKS_STATE}",
            f"{EVIDENCE_DIRNAME}/{PROBE_STATE}"]


def main(args: argparse.Namespace) -> int:
    try:
        return Runner(os.environ, args.apply,
                      selected_steps(args.from_step, args.only_step)).run()
    except (RunnerError, l1_kit.KitError, render.RenderError,
            router_config.RouterConfigError, policy.PolicyError,
            membership.MembershipError, router_link.RouterNotFound,
            policy.SandyNotFound, OSError) as e:
        print(f"{PROG} l1-run: {e}", file=sys.stderr)
        return EXIT_FAILED
