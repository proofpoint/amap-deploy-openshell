"""A fake `git` for the `siblings` tests, and helpers to stage it.

It is a python3 script that goes on PATH ahead of any real git, so no test
reaches a network. It logs every call to $FAKE_GIT_LOG as a JSON line, keeps a
checkout's state in `<checkout>/.git/fake.json` (`origin`, `head`, `commits`,
`dirty`), and reads the remotes from $FAKE_GIT_REMOTES, a JSON object
`{url: {"commits": [...], "head": sha}}`. Standard library only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Sequence

FAKE_GIT = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["FAKE_GIT_LOG"], "a") as f:
    f.write(json.dumps({"argv": argv,
                        "prompt": os.environ.get("GIT_TERMINAL_PROMPT")}) + "\\n")

cdir = "."
args = []
i = 0
while i < len(argv):
    if argv[i] == "--no-optional-locks":
        i += 1
    elif argv[i] == "-C":
        cdir = argv[i + 1]
        i += 2
    else:
        args = argv[i:]
        break


def fail(code, msg):
    sys.stderr.write(msg + "\\n")
    sys.exit(code)


def remotes():
    with open(os.environ["FAKE_GIT_REMOTES"]) as f:
        return json.load(f)


def toplevel(start):
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, ".git", "fake.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load(top):
    with open(os.path.join(top, ".git", "fake.json")) as f:
        return json.load(f)


def save(top, st):
    with open(os.path.join(top, ".git", "fake.json"), "w") as f:
        json.dump(st, f)


def need_checkout():
    top = toplevel(cdir)
    if top is None:
        fail(128, "fatal: not a git repository")
    return top, load(top)


def positional(rest):
    return [a for a in rest if not a.startswith("--")]


def fetch(top, st):
    remote = remotes().get(st["origin"])
    if remote is None:
        fail(128, "fatal: unable to access remote")
    st["commits"] = sorted(set(st["commits"]) | set(remote["commits"]))
    return remote


if not args:
    fail(99, "fake git: no subcommand")
sub, rest = args[0], args[1:]
if sub == "clone":
    url, dest = positional(rest)
    remote = remotes().get(url)
    if remote is None:
        fail(128, "fatal: repository '%s' not found" % url)
    if os.path.isdir(dest) and os.listdir(dest):
        fail(128, "fatal: destination path '%s' already exists" % dest)
    os.makedirs(os.path.join(dest, ".git"), exist_ok=True)
    save(dest, {"origin": url, "head": remote["head"],
                "commits": sorted(remote["commits"]), "dirty": False})
elif sub == "rev-parse":
    top, st = need_checkout()
    if rest == ["--show-toplevel"]:
        print(top)
    elif rest == ["HEAD"]:
        print(st["head"])
    else:
        fail(99, "fake git: unsupported rev-parse")
elif sub == "status":
    top, st = need_checkout()
    if st["dirty"]:
        print("?? local-change")
elif sub == "fetch":
    top, st = need_checkout()
    fetch(top, st)
    save(top, st)
elif sub == "checkout":
    top, st = need_checkout()
    sha = positional(rest)[-1]
    if sha not in st["commits"]:
        fail(1, "error: pathspec '%s' did not match any file(s) known to git" % sha)
    st["head"] = sha
    save(top, st)
elif sub == "pull":
    top, st = need_checkout()
    remote = fetch(top, st)
    st["head"] = remote["head"]
    save(top, st)
else:
    fail(99, "fake git: unsupported " + sub)
'''


def install(fake_bin: Path) -> Path:
    path = Path(fake_bin) / "git"
    path.write_text(FAKE_GIT)
    path.chmod(0o755)
    return path


def remotes(path: Path, mapping: Dict[str, dict]) -> None:
    Path(path).write_text(json.dumps(mapping))


def checkout(dest: Path, url: str, head: str, commits: Sequence[str],
             dirty: bool = False) -> None:
    git_dir = Path(dest) / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "fake.json").write_text(json.dumps(
        {"origin": url, "head": head, "commits": list(commits),
         "dirty": dirty}))


def state(dest: Path) -> dict:
    return json.loads((Path(dest) / ".git" / "fake.json").read_text())


def calls(log: Path) -> List[dict]:
    p = Path(log)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln]


def subcommand(argv: Sequence[str]) -> str:
    i = 0
    while i < len(argv):
        if argv[i] == "--no-optional-locks":
            i += 1
        elif argv[i] == "-C":
            i += 2
        else:
            return argv[i]
    return ""
