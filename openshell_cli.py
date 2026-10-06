"""The one place the operator verbs run a program: `openshell`.

It never runs `docker`, never runs `sandbox exec` and never writes a file. It
reads a sandbox's identity, lists sandboxes, creates a sandbox from the argv
`render` made, and deletes one. Nothing is written inside a sandbox.

The calls, at OpenShell main@acbac9c (the citations are `l1_run`'s):

- the global `--workspace` (crates/openshell-cli/src/main.rs:466-475) pins the
  workspace, so `$OPENSHELL_WORKSPACE` cannot move a call to another one;
- `sandbox get NAME --output json` has `id`, `name` and `workspace`
  (crates/openshell-cli/src/run.rs:2789-2791);
- `sandbox list --all-workspaces --names` prints `<workspace>/<name>`
  (main.rs:1610-1640, run.rs:2598-2620);
- `sandbox delete NAME` (main.rs:1642).

The provider key (D10) reaches the `sandbox create` call only: `run` strips it
from every other child's environment. It is never an argument, so there is
nothing to redact.

Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import membership
import provider_profile

DEFAULT_BINARY = "openshell"
DEFAULT_TIMEOUT = 120.0
KEY_VARIABLE = provider_profile.KEY_VARIABLE


class OpenShellError(Exception):
    """A call could not be made or its answer could not be used."""


class Call(NamedTuple):
    argv: Tuple[str, ...]
    code: Optional[int]      # None: the program did not run, or timed out
    stdout: str
    stderr: str


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


class Client:
    def __init__(self, binary: str = DEFAULT_BINARY, workspace: str = "",
                 env: Optional[Mapping[str, str]] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.binary = binary
        self.workspace = workspace
        self.env = dict(os.environ if env is None else env)
        self.timeout = timeout

    def run(self, argv: Sequence[str], *, with_key: bool = False) -> Call:
        child_env = {k: v for k, v in self.env.items()
                     if with_key or k != KEY_VARIABLE}
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = tuple(argv)
        try:
            proc = subprocess.run(
                list(argv), stdin=subprocess.DEVNULL, capture_output=True,
                text=True, errors="replace", timeout=self.timeout,
                env=child_env)
        except subprocess.TimeoutExpired:
            return Call(argv, None, "", f"timed out after {self.timeout:g}s")
        except OSError as e:
            return Call(argv, None, "", str(e))
        return Call(argv, proc.returncode, proc.stdout, proc.stderr)

    def base(self, *words: str) -> List[str]:
        return [self.binary, "--workspace", self.workspace, *words]

    def get(self, name: str) -> Call:
        return self.run(self.base("sandbox", "get", name, "--output", "json"))

    def list_names(self) -> Tuple[Optional[List[str]], str]:
        """The `<workspace>/<name>` lines, or None and why the list failed."""
        r = self.run([self.binary, "sandbox", "list", "--all-workspaces",
                      "--names"])
        if r.code != 0:
            return None, (f"`sandbox list` failed: {first_line(r.stderr)}")
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()], ""

    def create(self, argv: Sequence[str]) -> Call:
        """Runs the rendered create argv with this client's binary in place of
        its first word. The only call that carries the provider key."""
        return self.run([self.binary, *list(argv)[1:]], with_key=True)

    def delete(self, name: str) -> Call:
        return self.run(self.base("sandbox", "delete", name))


def find_sandbox(client: Client, name: str) -> Tuple[str, Optional[dict], str]:
    """`("present", <sandbox get json>, "")`, `("absent", None, "")` or
    `("unknown", None, <why>)`. A failed `get` is confirmed against the list
    before the sandbox is called absent."""
    r = client.get(name)
    if r.code == 0:
        try:
            doc = json.loads(r.stdout)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            return "present", doc, ""
        return "unknown", None, f"`sandbox get {name}` printed no JSON object"
    names, why = client.list_names()
    if names is None:
        return "unknown", None, (f"`sandbox get {name}` failed "
                                 f"({first_line(r.stderr)}) and the list could "
                                 f"not confirm it: {why}")
    if f"{client.workspace}/{name}" in names:
        return "unknown", None, (f"{name} is listed but `sandbox get` failed: "
                                 f"{first_line(r.stderr)}")
    return "absent", None, ""


def identity_problem(doc: Mapping[str, Any], name: str,
                     workspace: str) -> Optional[str]:
    """Why `doc` (a `sandbox get` document) is not the sandbox `name` of
    `workspace` with a usable ID, or None."""
    if doc.get("name") != name or doc.get("workspace") != workspace:
        return (f"`sandbox get {name}` reports name {doc.get('name')!r} in "
                f"workspace {doc.get('workspace')!r}, not {name!r} in "
                f"{workspace!r}")
    return membership.id_problem(doc.get("id"))
