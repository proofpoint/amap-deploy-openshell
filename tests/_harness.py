"""Harness machinery for the suite: the binary guard, and a helper that copies
the harness into a scratch tree and runs pytest there.

THE BINARY GUARD. The suite never runs OpenShell, Docker or Podman. For the
whole session `subprocess.Popen` is replaced by `GuardedPopen`, which refuses,
before anything is executed, a command whose program is `openshell`, `docker`
or `podman` unless that program resolves to a file inside a directory a test
registered with `stubbed()` (the `fake_bin` fixture does this). A name that
does not resolve at all is refused too, so the guard behaves the same on a host
with no `openshell` installed.

What it covers: `subprocess.Popen` and everything built on it (`run`, `call`,
`check_call`, `check_output`, `getoutput`, `getstatusoutput`, `os.popen`).
What it does not cover: `os.system`, `os.exec*` and `os.posix_spawn`. For an
argv list only `argv[0]` (or `executable`) is checked, so a command such as
`openshell ... --driver docker` is not refused for the word `docker`. With
`shell=True` every word of the command line is checked.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

GUARDED = ("openshell", "docker", "podman")


class UnstubbedBinary(RuntimeError):
    pass


# Survive a second import of this module: never wrap the guard in the guard.
_REAL_POPEN = getattr(subprocess.Popen, "_amap_real", subprocess.Popen)
_SIG = inspect.signature(_REAL_POPEN.__init__)
_STUB_DIRS: set = set()  # resolved directories registered as holding fakes


def _words_of_command_line(line: str) -> List[str]:
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return line.split()


def command_words(bound: inspect.BoundArguments) -> List[str]:
    """The words of a Popen call that name a program."""
    a = bound.arguments
    args = a.get("args")
    if a.get("shell"):
        if isinstance(args, (str, bytes, os.PathLike)):
            line = os.fsdecode(args)
        else:
            line = os.fsdecode(args[0]) if args else ""
        return _words_of_command_line(line)
    if a.get("executable") is not None:
        return [os.fsdecode(a["executable"])]
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    if args:
        return [os.fsdecode(args[0])]
    return []


def check(bound: inspect.BoundArguments) -> None:
    a = bound.arguments
    env = a.get("env")
    for word in command_words(bound):
        if os.path.basename(word) not in GUARDED:
            continue
        if "/" in word:
            resolved: Optional[str] = word
            if not os.path.isabs(word) and a.get("cwd") is not None:
                resolved = os.path.join(os.fsdecode(a["cwd"]), word)
        else:
            resolved = shutil.which(
                word, path=os.pathsep.join(os.get_exec_path(env)))
        if resolved is not None and Path(resolved).resolve().parent in _STUB_DIRS:
            continue
        where = resolved if resolved is not None else "not on PATH"
        raise UnstubbedBinary(
            f"refusing to run {word!r} ({where}): the suite never runs "
            f"{', '.join(GUARDED)} unless a test stubbed it. Write a fake with "
            f"_harness.write_fake() in a directory given to _harness.stubbed(), "
            f"or use the fake_bin fixture.")


class GuardedPopen(_REAL_POPEN):  # type: ignore[valid-type,misc]
    _amap_real = _REAL_POPEN

    def __init__(self, *a, **k):
        try:
            bound = _SIG.bind(self, *a, **k)
        except TypeError:
            bound = None  # the real Popen reports the bad call itself
        if bound is not None:
            check(bound)
        super().__init__(*a, **k)


def install() -> None:
    subprocess.Popen = GuardedPopen  # type: ignore[misc]


def uninstall() -> None:
    subprocess.Popen = _REAL_POPEN  # type: ignore[misc]


@contextlib.contextmanager
def stubbed(directory: Path) -> Iterator[Path]:
    """Register `directory` as holding fakes, for the block."""
    d = Path(directory).resolve()
    added = d not in _STUB_DIRS
    _STUB_DIRS.add(d)
    try:
        yield Path(directory)
    finally:
        if added:
            _STUB_DIRS.discard(d)


def write_fake(directory: Path, name: str, script: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(script)
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Copy the harness into a scratch tree and run pytest there.

HARNESS_FILES = ("pytest.ini", "conftest.py", "tests/_workspace.py",
                 "tests/_harness.py")
REPO = Path(__file__).absolute().parents[1]
PASSING_PROBE = "def test_probe():\n    pass\n"


def nothing_beside(repo: Path) -> List[Path]:
    """Every `<ancestor>/<sibling dir name>` that exists, for `repo` and each of
    its ancestors. Empty means the discovery walk has nothing to find."""
    import _workspace
    repo = Path(repo).absolute()
    names = {n for s in _workspace.SIBLINGS.values() for n in s.dir_names}
    return [d / n for d in [repo, *repo.parents] for n in sorted(names)
            if (d / n).exists()]


def run_copy(tmp: Path, env: Dict[str, Optional[str]],
             probe: str = PASSING_PROBE) -> "subprocess.CompletedProcess":
    """Build `<tmp>/ws/repo` from the harness files plus one probe test, run
    pytest there and return the result. `env` overrides the child environment;
    a value of None leaves that variable unset."""
    import _workspace
    repo = Path(tmp) / "ws" / "repo"
    for rel in HARNESS_FILES:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO / rel).read_bytes())
    (repo / "tests" / "test_probe.py").write_text(probe)
    child = {k: v for k, v in os.environ.items()
             if k not in _workspace.ENV_NAMES and k != "PYTHONPATH"}
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    for k, v in env.items():
        if v is None:
            child.pop(k, None)
        else:
            child[k] = v
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"], cwd=str(repo),
        env=child, capture_output=True, text=True, timeout=180)
