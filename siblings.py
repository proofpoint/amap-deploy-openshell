"""amap-openshell siblings: set up the sibling checkouts beside this repository.

It reads siblings.json (decision D14), and for each sibling clones it beside
this repository or fetches it if it is already there, then checks out its
pinned commit (decision D13). amap-spec is not pinned: it is cloned, or
fast-forwarded, and its commit is printed. Every checkout is read before
anything is done. A refusal (a path that is not a directory, a directory that is
not a git checkout of its own, or local changes) means nothing is run, even with
`--apply`.

This module imports neither `policy` nor anything that loads sandy, because it
runs before sandy exists. It imports nothing from this repository.

Without `--apply` it makes only `rev-parse` and `status` calls (READ_ONLY_GIT),
each as `git --no-optional-locks -C <dir> ...`, and changes nothing. Every git
call runs with GIT_TERMINAL_PROMPT=0, as a list argv with no shell, and with a
timeout, so a private or wrong URL fails instead of hanging.

Standard library only. Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

REPO = Path(__file__).absolute().parent
PINS_NAME = "siblings.json"
GIT = "git"
GIT_TIMEOUT = 600
READ_ONLY_GIT = ("rev-parse", "status")
EXIT_OK, EXIT_REFUSED = 0, 1
PROG = "amap-openshell.py siblings"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
KEYS = ("name", "url", "commit")


class SiblingsError(Exception):
    """A refusal: the operator's to fix."""


class Sibling(NamedTuple):
    name: str
    url: str
    commit: Optional[str]   # None: unpinned


class Plan(NamedTuple):
    sibling: Sibling
    dest: Path
    head: Optional[str]                     # None: absent
    actions: Tuple[Tuple[str, ...], ...]    # git argvs (without "git") for --apply
    says: str                               # the dry-run line


def load_pins(path: Path) -> List[Sibling]:
    where = str(path.name)
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SiblingsError(f"{where}: cannot be read as JSON: {e}")
    if not isinstance(doc, dict) or set(doc) != {"siblings"}:
        raise SiblingsError(f"{where}: must be an object with exactly the "
                            f"key 'siblings'")
    entries = doc["siblings"]
    if not isinstance(entries, list) or not entries:
        raise SiblingsError(f"{where}: 'siblings' must be a non-empty list")
    out: List[Sibling] = []
    seen = set()
    for i, e in enumerate(entries):
        what = f"{where}: entry {i}"
        if not isinstance(e, dict) or set(e) != set(KEYS):
            raise SiblingsError(f"{what} must be an object with exactly the "
                                f"keys {list(KEYS)}")
        name, url, commit = e["name"], e["url"], e["commit"]
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise SiblingsError(f"{what}: bad name {name!r}")
        what = f"{where}: entry {name!r}"
        if name in seen:
            raise SiblingsError(f"{what} is a duplicate name")
        seen.add(name)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise SiblingsError(f"{what}: url must be a string starting "
                                f"https://")
        if commit is not None and (not isinstance(commit, str)
                                   or not COMMIT_RE.match(commit)):
            raise SiblingsError(f"{what}: commit must be null or 40 lower-case "
                                f"hex characters")
        out.append(Sibling(name, url, commit))
    return out


def git(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
            [GIT, *args], env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
            capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SiblingsError(f"git {' '.join(args)}: {e}")


def read_git(dest: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return git(["--no-optional-locks", "-C", str(dest), *args])


def inspect(sib: Sibling, base: Path) -> Plan:
    """Read-only calls only."""
    dest = base / sib.name
    if not os.path.lexists(dest):
        actions: Tuple[Tuple[str, ...], ...] = (
            ("clone", "--quiet", sib.url, str(dest)),)
        if sib.commit is not None:
            actions += (("-C", str(dest), "checkout", "--quiet", "--detach",
                         sib.commit),)
            says = (f"{sib.name}: absent; would clone {sib.url} and check out "
                    f"{sib.commit}")
        else:
            says = f"{sib.name}: absent; would clone {sib.url} (not pinned)"
        return Plan(sib, dest, None, actions, says)
    if not dest.is_dir():
        raise SiblingsError(f"{dest} exists and is not a directory")
    top = read_git(dest, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) \
            != os.path.realpath(dest):
        raise SiblingsError(f"{dest} is not a git checkout of its own")
    status = read_git(dest, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        raise SiblingsError(f"{dest}: git status failed")
    if status.stdout.strip():
        raise SiblingsError(f"{dest} has local changes; commit, stash or "
                            f"remove them, then run siblings again")
    head_run = read_git(dest, "rev-parse", "HEAD")
    if head_run.returncode != 0:
        raise SiblingsError(f"{dest}: cannot read HEAD")
    head = head_run.stdout.strip()
    if sib.commit is not None:
        if head == sib.commit:
            return Plan(sib, dest, head, (),
                        f"{sib.name}: at its pin {sib.commit}; nothing to do")
        return Plan(sib, dest, head,
                    (("-C", str(dest), "fetch", "--quiet", "origin"),
                     ("-C", str(dest), "checkout", "--quiet", "--detach",
                      sib.commit)),
                    f"{sib.name}: at {head}; would fetch origin and check out "
                    f"{sib.commit}")
    return Plan(sib, dest, head,
                (("-C", str(dest), "pull", "--ff-only", "--quiet"),),
                f"{sib.name}: at {head}; would fast-forward (not pinned)")


def plan_all(sibs: Sequence[Sibling], base: Path) -> Tuple[List[Plan], List[str]]:
    plans: List[Plan] = []
    refusals: List[str] = []
    for sib in sibs:
        try:
            plans.append(inspect(sib, base))
        except SiblingsError as e:
            refusals.append(str(e))
    return plans, refusals


def carry_out(plan: Plan) -> str:
    name = plan.sibling.name
    for argv in plan.actions:
        r = git(argv)
        if r.returncode != 0:
            lines = (r.stderr or "").strip().splitlines()
            first = lines[0] if lines else f"exit {r.returncode}"
            raise SiblingsError(f"{name}: git {' '.join(argv)} failed: {first}")
    r = read_git(plan.dest, "rev-parse", "HEAD")
    if r.returncode != 0:
        raise SiblingsError(f"{name}: cannot read HEAD after the update")
    head = r.stdout.strip()
    if plan.sibling.commit is not None and head != plan.sibling.commit:
        raise SiblingsError(f"{name}: is at {head}, not its pin "
                            f"{plan.sibling.commit}")
    return head


def run(base: Path, pins: Path, apply: bool, out=sys.stdout,
        err=sys.stderr) -> int:
    try:
        sibs = load_pins(pins)
        plans, refusals = plan_all(sibs, base)
        if refusals:
            for r in refusals:
                print(f"{PROG}: {r}", file=err)
            return EXIT_REFUSED
        if not apply:
            for p in plans:
                print(p.says, file=out)
            print("DRY RUN: nothing changed. Run again with --apply.", file=out)
            return EXIT_OK
        for p in plans:
            if not p.actions:
                print(f"{p.sibling.name}: at its pin {p.sibling.commit}; "
                      f"nothing to do", file=out)
                continue
            head = carry_out(p)
            tail = " (its pin)" if p.sibling.commit is not None \
                else " (not pinned)"
            print(f"{p.sibling.name}: at {head}{tail}", file=out)
        return EXIT_OK
    except SiblingsError as e:
        print(f"{PROG}: {e}", file=err)
        return EXIT_REFUSED


def main(args) -> int:
    return run(REPO.parent, REPO / PINS_NAME, bool(args.apply))
