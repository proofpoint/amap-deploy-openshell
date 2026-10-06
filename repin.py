"""repin: move a sibling's pin to a newer commit, for the repin workflow.

`check` asks each pinned sibling's remote for its `main` and prints, as a JSON
list, the siblings whose `main` is not their pin: `{"name", "pin", "head"}`.
`apply NAME COMMIT` rewrites that sibling's pin in siblings.json and every pin
quote of it in the shipped Markdown, so the docs test keeps agreeing. It never
touches the records of past runs (PIN_HISTORY), which quote the pins of their
day.

`openshell-check` follows OpenShell's release tags: it prints, as JSON, the
newest release tag later than the one `interceptor/proto/SOURCE` vendors
(`{"current", "tag"}`), or `null`. The release workflow regenerates the
interceptor's stubs from that tag and opens a pull request marked as needing a
live run.

A repin is a proposal. The workflow opens a pull request and never merges it:
each pin is an operator's decision (D13, D16), and the suite proves the offline
properties only, not the live ones.

Standard library only (plus `interceptor.locks`, which is too). Python 3.9
compatible. Like `siblings`, it imports nothing that loads sandy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import siblings
from interceptor import locks

REPO = Path(__file__).absolute().parent
BRANCH = "main"
# Records of what a past run used. tests/test_docs_agree.py keeps the same list.
PIN_HISTORY = ("IMPLEMENTATION-PLAN.md", "docs/POC-REPORT.md")
SKIPPED_DIRS = (".git", ".claude", "__pycache__")
PIN_TOKEN = re.compile(r"(?<![@\w])[0-9a-f]{7,40}(?!\w)")
EXIT_OK, EXIT_USAGE = 0, 2
OPENSHELL_URL = "https://github.com/NVIDIA/OpenShell.git"
OPENSHELL_SOURCE = REPO / "interceptor" / "proto" / locks.SOURCE_NAME
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)\Z")


class RepinError(Exception):
    """A refusal: nothing was written."""


def remote_head(url: str, branch: str = BRANCH) -> str:
    """The commit `branch` points at on `url`, by `git ls-remote`."""
    r = subprocess.run(
        [siblings.GIT, "ls-remote", url, f"refs/heads/{branch}"],
        capture_output=True, text=True, timeout=siblings.GIT_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    fields = r.stdout.split()
    if r.returncode != 0 or not fields \
            or not siblings.COMMIT_RE.match(fields[0]):
        raise RepinError(f"cannot read {branch} of {url}: "
                         f"{(r.stderr or r.stdout).strip() or 'no output'}")
    return fields[0]


def remote_tags(url: str) -> List[str]:
    """The tag names on `url`, by `git ls-remote --tags --refs`."""
    r = subprocess.run(
        [siblings.GIT, "ls-remote", "--tags", "--refs", url],
        capture_output=True, text=True, timeout=siblings.GIT_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise RepinError(f"cannot read the tags of {url}: "
                         f"{(r.stderr or r.stdout).strip() or 'no output'}")
    prefix = "refs/tags/"
    return [f.split(prefix, 1)[1] for ln in r.stdout.splitlines()
            for f in ln.split()[1:] if f.startswith(prefix)]


def release_key(tag: str) -> Optional[Tuple[int, int, int]]:
    """`(major, minor, patch)` of a release tag such as `v0.1.2`, or None for a
    pre-release or any other tag."""
    m = RELEASE_TAG.match(tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def newer_release(current: str, tags: Iterable[str]) -> Optional[str]:
    """The newest release tag later than `current`, or None."""
    have = release_key(current)
    if have is None:
        raise RepinError(f"{current!r} is not a release tag")
    later = [(k, t) for t in tags for k in [release_key(t)]
             if k is not None and k > have]
    return max(later)[1] if later else None


def openshell_check(source: Path = OPENSHELL_SOURCE,
                    tags: Optional[Callable[[str], List[str]]] = None
                    ) -> Optional[Dict[str, str]]:
    """`{"current", "tag"}` when OpenShell has a release later than the vendored
    tag, else None."""
    try:
        current = locks.read_source(str(source))[0]
    except (OSError, ValueError) as e:
        raise RepinError(f"cannot read {source.name}: {e}") from e
    newest = newer_release(current, (tags or remote_tags)(OPENSHELL_URL))
    return {"current": current, "tag": newest} if newest else None


def stale(pins: Path, head: Callable[[str], str] = remote_head
          ) -> List[Dict[str, str]]:
    """The pinned siblings whose remote `main` is not their pin."""
    out = []
    for s in siblings.load_pins(pins):
        if s.commit is None:
            continue
        now = head(s.url)
        if now != s.commit:
            out.append({"name": s.name, "pin": s.commit, "head": now})
    return out


def shipped_markdown(repo: Path) -> List[Path]:
    out = []
    for p in sorted(repo.rglob("*.md")):
        rel = p.relative_to(repo)
        if any(part in SKIPPED_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in PIN_HISTORY:
            continue
        out.append(p)
    return out


def requote(text: str, name: str, old: str, new: str) -> str:
    """`text` with each quote of `old` on a line naming `name` replaced by the
    same-length prefix of `new`."""
    def line(ln: str) -> str:
        if name not in ln:
            return ln
        return PIN_TOKEN.sub(
            lambda m: new[:len(m.group())] if old.startswith(m.group())
            else m.group(), ln)
    return "".join(line(ln) for ln in text.splitlines(keepends=True))


def apply(repo: Path, name: str, new: str) -> List[Path]:
    """Move `name`'s pin to `new`. Returns the files changed."""
    if not siblings.COMMIT_RE.match(new):
        raise RepinError(f"{new!r} is not a full 40-character commit")
    pins_path = repo / siblings.PINS_NAME
    found = [s for s in siblings.load_pins(pins_path) if s.name == name]
    if not found:
        raise RepinError(f"siblings.json has no sibling {name!r}")
    old = found[0].commit
    if old is None:
        raise RepinError(f"{name} is not pinned")
    if old == new:
        return []
    changed = []
    # The pin is a full commit, so it occurs once; replacing it in place
    # keeps the file's one-line-per-sibling layout.
    text = pins_path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RepinError(f"{name}'s pin {old} is not quoted exactly once in "
                         f"siblings.json")
    pins_path.write_text(text.replace(old, new), encoding="utf-8")
    if [s.commit for s in siblings.load_pins(pins_path)
            if s.name == name] != [new]:
        raise RepinError(f"siblings.json does not pin {name} at {new} after "
                         f"the rewrite")
    changed.append(pins_path)
    for p in shipped_markdown(repo):
        before = p.read_text(encoding="utf-8")
        after = requote(before, name, old, new)
        if after != before:
            p.write_text(after, encoding="utf-8")
            changed.append(p)
    return changed


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv == ["check"]:
            print(json.dumps(stale(REPO / siblings.PINS_NAME)))
            return EXIT_OK
        if argv == ["openshell-check"]:
            print(json.dumps(openshell_check()))
            return EXIT_OK
        if len(argv) == 3 and argv[0] == "apply":
            for p in apply(REPO, argv[1], argv[2]):
                print(f"updated {p.relative_to(REPO)}")
            return EXIT_OK
    except (RepinError, siblings.SiblingsError) as e:
        print(f"repin: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("usage: repin.py check | repin.py openshell-check | "
          "repin.py apply NAME COMMIT", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
