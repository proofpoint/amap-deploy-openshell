"""Finds the four read-only sources the suite checks against: the router, the
connector, the sandy checkout and the amap-spec directory.

For each one, the environment variable is the only place searched when it is
set. Otherwise the search walks up from this file and takes the nearest match
beside an ancestor, confirmed by a named file. A directory with the right name
and no confirming file is not a match. A set-but-empty variable counts as
unset. Paths use `.absolute()`, never `.resolve()`, so a symlinked sibling is
still the sibling.

Importing this module never raises: a missing source is `path=None`, and
`require_all()` is where that becomes an error.

The fingerprints answer one question: did the session change a source? Nothing
here writes into one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple

_HERE = Path(__file__).absolute()


class Sibling(NamedTuple):
    key: str                    # "router" | "connector" | "sandy" | "spec"
    dir_names: Tuple[str, ...]  # names looked for beside each ancestor, in order
    variable: str               # the override
    confirm: str                # relative file that confirms a candidate


SIBLINGS: Dict[str, Sibling] = {
    "router": Sibling("router", ("amap-router-local",),
                      "AMAP_ROUTER_REPO", "router/reset.py"),
    "connector": Sibling("connector", ("amap-connector-claude",),
                         "AMAP_CONNECTOR_REPO", "bin/inbox-delivery"),
    "sandy": Sibling("sandy", ("amap-deploy-sandy",),
                     "AMAP_SANDY_REPO", "fleet_policy.py"),
    "spec": Sibling("spec", ("amap-spec", ".amap-spec"),
                    "AMAP_SPEC_DIR", "fixtures/validate.py"),
}
ENV_NAMES = tuple(s.variable for s in SIBLINGS.values())

_LABEL = {"router": "the router", "connector": "the connector",
          "sandy": "the sandy checkout", "spec": "the amap-spec directory"}


class Found(NamedTuple):
    key: str
    path: Optional[Path]        # None when missing
    variable: Optional[str]     # the variable that supplied the search, else None
    searched: Tuple[Path, ...]  # every candidate looked at


class SiblingMissing(RuntimeError):
    pass


class FingerprintError(RuntimeError):
    pass


def find(key: str, env: Optional[Mapping[str, str]] = None,
         start: Optional[Path] = None) -> Found:
    sib = SIBLINGS[key]
    env = os.environ if env is None else env
    start = _HERE.parent if start is None else Path(start).absolute()
    value = env.get(sib.variable)
    if value:
        cand = Path(value).absolute()
        ok = (cand / sib.confirm).is_file()
        return Found(key, cand if ok else None, sib.variable, (cand,))
    searched: List[Path] = []
    for d in [start, *start.parents]:
        for name in sib.dir_names:
            cand = d / name
            searched.append(cand)
            if (cand / sib.confirm).is_file():
                return Found(key, cand, None, tuple(searched))
    return Found(key, None, None, tuple(searched))


def message(found: Found) -> str:
    """The text for a missing source."""
    sib = SIBLINGS[found.key]
    if found.variable:
        value = found.searched[0] if found.searched else ""
        return (f"${sib.variable}={value} does not contain {sib.confirm}; it is "
                f"the only place searched because the variable names it. Point "
                f"it at the checkout, or unset it to search beside this "
                f"repository.")
    return (f"cannot find {_LABEL[found.key]} ({' or '.join(sib.dir_names)}, "
            f"confirmed by {sib.confirm}) beside any ancestor of this checkout. "
            f"Check it out beside this repository, or set ${sib.variable}.")


def require_all(env: Optional[Mapping[str, str]] = None,
                start: Optional[Path] = None) -> Dict[str, Path]:
    founds = {k: find(k, env, start) for k in SIBLINGS}
    missing = [f for f in founds.values() if f.path is None]
    if missing:
        lines = ["missing sibling(s); the suite does not run without all four:"]
        lines += [f"  {f.key}: {message(f)}" for f in missing]
        raise SiblingMissing("\n".join(lines))
    return {k: f.path for k, f in founds.items()}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fingerprints.

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_symlink():
        return "-> " + os.readlink(path)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: Path, *args: str, index: Optional[Path] = None) -> bytes:
    cmd = ["git", "--no-optional-locks", "-C", str(root), *args]
    env = dict(os.environ)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env)
    except OSError as e:
        raise FingerprintError(f"cannot run git in {root}: {e}") from e
    if r.returncode != 0:
        raise FingerprintError(
            f"git {' '.join(args)} failed in {root} (exit {r.returncode}): "
            f"{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def git_fingerprint(root: Path) -> str:
    """The working tree's state: status (untracked files listed), the unstaged
    and staged diffs, and a hash of every untracked file. Nothing is written into
    the source, not even the index refresh git does on its own. HEAD is left out on purpose: a commit made by the source's
    owner during the run leaves a clean tree clean."""
    root = Path(root)
    # `git diff` refreshes a stale index and writes it back even under
    # --no-optional-locks, and that write would be into the source. So every
    # command runs against a private copy of the index (mtime kept, so git's
    # racy-timestamp check behaves the same).
    real = Path(os.fsdecode(_git(root, "rev-parse", "--git-path", "index")).strip())
    if not real.is_absolute():
        real = root / real
    with tempfile.TemporaryDirectory(prefix="amap-fp-") as tmp:
        index = Path(tmp) / "index"
        if real.is_file():
            shutil.copy2(real, index)
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all",
                      index=index)
        diff = _git(root, "diff", "--binary", index=index)
        cached = _git(root, "diff", "--cached", "--binary", index=index)
        # NUL-separated so a path with special characters is not quoted.
        raw = _git(root, "status", "--porcelain=v1", "--untracked-files=all",
                   "-z", index=index)
    text = lambda b: b.decode("utf-8", "replace")  # noqa: E731
    untracked: List[str] = []
    entries = raw.split(b"\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        code, path = e[:2], e[3:]
        if code[:1] in (b"R", b"C") or code[1:2] in (b"R", b"C"):
            i += 1  # a rename or copy is followed by its origin path
        if code == b"??":
            untracked.append(os.fsdecode(path))
    lines = []
    for rel in sorted(untracked):
        p = root / rel
        if p.is_dir() and not p.is_symlink():
            digest = tree_fingerprint(p)
            digest = hashlib.sha256(digest.encode()).hexdigest()
        else:
            try:
                digest = _sha256_file(p)
            except OSError as e:
                raise FingerprintError(f"cannot read {p}: {e}") from e
        lines.append(f"?? {rel} {digest}")
    return "\n".join([
        "== status ==", text(status),
        "== diff ==", text(diff),
        "== diff --cached ==", text(cached),
        "== untracked ==", "\n".join(lines), ""])


def tree_fingerprint(root: Path) -> str:
    """A content hash for a directory that is not a git tree. `__pycache__` and
    `.git` are pruned and `*.pyc` skipped, as the siblings' `.gitignore` files
    already ignore them."""
    root = Path(root)
    lines = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        # os.walk does not descend into a symlinked directory; record the link.
        files = files + [d for d in dirs if (Path(dirpath) / d).is_symlink()]
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            try:
                lines.append(f"{rel}\t{_sha256_file(p)}")
            except OSError as e:
                raise FingerprintError(f"cannot read {p}: {e}") from e
    return "\n".join(lines) + "\n"


def fingerprint(root: Path) -> str:
    root = Path(root)
    if (root / ".git").exists():
        return git_fingerprint(root)
    return tree_fingerprint(root)


FOUND = {k: find(k) for k in SIBLINGS}  # never raises at import
ROUTER_ROOT = FOUND["router"].path
CONNECTOR_ROOT = FOUND["connector"].path
SANDY_ROOT = FOUND["sandy"].path
SPEC_DIR = FOUND["spec"].path
