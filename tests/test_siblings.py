"""`amap-openshell.py siblings` (siblings.py) and siblings.json.

`git` is faked on PATH (tests/_fake_git.py), so nothing reaches a network. The
command runs from a copy of the four files it needs, in a scratch workspace
with no sandy checkout beside it.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _fake_git as fg
import _harness
import _workspace
import siblings
from siblings import SiblingsError
import test_l1_runbook

REPO = Path(__file__).absolute().parents[1]
PINS = REPO / "siblings.json"
COPIED = ("amap-openshell.py", "amap_openshell.py", "siblings.py",
          "siblings.json")
OLD = "1" * 40
NEWER = "f" * 40
S1 = "a1" * 20
S2 = "b2" * 20
ROUTER, CONNECTOR, SANDY, SPEC = (
    "amap-router-local", "amap-connector-claude", "amap-deploy-sandy",
    "amap-spec")


class World:
    def __init__(self, tmp_path, fake_bin):
        fg.install(fake_bin)
        self.ws = tmp_path / "ws"
        self.repo = self.ws / "amap-deploy-openshell"
        self.repo.mkdir(parents=True)
        for name in COPIED:
            shutil.copyfile(REPO / name, self.repo / name)
        self.log = tmp_path / "git.log"
        self.remotes_file = tmp_path / "remotes.json"
        self.sibs = {s.name: s for s in siblings.load_pins(PINS)}
        self.remote = {}
        for s in self.sibs.values():
            if s.commit is None:
                self.remote[s.url] = {"commits": [S1, S2], "head": S2}
            else:
                self.remote[s.url] = {"commits": [OLD, s.commit, NEWER],
                                      "head": NEWER}
        self.write_remotes()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("AMAP_")}
        self.env.update(PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                        PYTHONDONTWRITEBYTECODE="1",
                        FAKE_GIT_LOG=str(self.log),
                        FAKE_GIT_REMOTES=str(self.remotes_file))
        assert shutil.which("git", path=self.env["PATH"]) == str(
            Path(fake_bin) / "git")

    def write_remotes(self):
        fg.remotes(self.remotes_file, self.remote)

    def place(self, name, head, dirty=False):
        s = self.sibs[name]
        fg.checkout(self.ws / name, s.url, head, self.remote[s.url]["commits"]
                    if head in self.remote[s.url]["commits"] else [head],
                    dirty=dirty)

    def dest(self, name):
        return self.ws / name

    def pin(self, name):
        return self.sibs[name].commit

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "amap-openshell.py"), *args],
            env=self.env, capture_output=True, text=True, timeout=60)

    def snapshot(self):
        files, dirs = {}, set()
        for p in sorted(self.ws.rglob("*")):
            rel = str(p.relative_to(self.ws))
            if p.is_dir():
                dirs.add(rel)
            else:
                files[rel] = p.read_bytes()
        return files, dirs

    def calls(self):
        return fg.calls(self.log)


@pytest.fixture
def world(tmp_path, fake_bin):
    return World(tmp_path, fake_bin)


def mixed(w):
    """Router absent; connector behind; sandy at its pin; amap-spec behind."""
    w.place(CONNECTOR, OLD)
    w.place(SANDY, w.pin(SANDY))
    w.place(SPEC, S1)


def test_the_pins_file_names_each_sibling_at_its_pin():
    sibs = siblings.load_pins(PINS)
    assert [s.name for s in sibs] == [ROUTER, CONNECTOR, SANDY, SPEC]
    # The values are the operator's decisions (D13, D16) and move with every
    # repin, so the test checks their form, not their value. The docs test
    # checks that every quote of a pin is the pins file's.
    *pinned, spec = [s.commit for s in sibs]
    assert spec is None
    assert all(c is not None and siblings.COMMIT_RE.match(c) for c in pinned)
    for s in sibs:
        assert s.url == f"https://github.com/proofpoint/{s.name}"
    assert {s.name for s in sibs} == {
        s.dir_names[0] for s in _workspace.SIBLINGS.values()}


def _broken(mutate):
    doc = json.loads(PINS.read_text(encoding="utf-8"))
    return mutate(doc)


BROKEN = {
    "short commit": lambda d: d["siblings"][0].update(commit="9854a5e"),
    "upper-case commit": lambda d: d["siblings"][0].update(
        commit=d["siblings"][0]["commit"].upper()),
    "missing key": lambda d: d["siblings"][0].pop("url"),
    "extra key": lambda d: d["siblings"][0].update(extra=1),
    "duplicate name": lambda d: d["siblings"][1].update(
        name=d["siblings"][0]["name"]),
    "slash in name": lambda d: d["siblings"][0].update(name="a/b"),
    "dotdot name": lambda d: d["siblings"][0].update(name=".."),
    "http url": lambda d: d["siblings"][0].update(
        url="http://github.com/proofpoint/x"),
    "empty commit": lambda d: d["siblings"][0].update(commit=""),
    "top-level list": lambda d: d.clear(),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_a_broken_pins_file_is_refused(tmp_path, case):
    doc = json.loads(PINS.read_text(encoding="utf-8"))
    BROKEN[case](doc)
    if case == "top-level list":
        doc = [doc]
    path = tmp_path / "siblings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SiblingsError):
        siblings.load_pins(path)


def test_without_apply_nothing_changes(world):
    mixed(world)
    before = world.snapshot()
    r = world.cli("siblings")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert any(ln.startswith(ROUTER) and "would clone" in ln for ln in lines)
    assert any(ln.startswith(CONNECTOR) and "would fetch" in ln
               for ln in lines)
    assert any(ln.startswith(SANDY) and "nothing to do" in ln for ln in lines)
    assert any(ln.startswith(SPEC) and "fast-forward" in ln for ln in lines)
    assert "DRY RUN" in r.stdout
    calls = world.calls()
    assert calls
    for c in calls:
        assert fg.subcommand(c["argv"]) in siblings.READ_ONLY_GIT, c
        assert c["argv"][0] == "--no-optional-locks", c
    assert world.snapshot() == before
    assert not world.dest(ROUTER).exists()


def test_with_apply_every_sibling_is_left_at_its_pin(world):
    mixed(world)
    r = world.cli("siblings", "--apply")
    assert r.returncode == 0, r.stderr
    for name in (ROUTER, CONNECTOR, SANDY):
        assert fg.state(world.dest(name))["head"] == world.pin(name), name
    assert fg.state(world.dest(SPEC))["head"] == S2
    assert S2 in r.stdout
    seen = len(world.calls())
    again = world.cli("siblings", "--apply")
    assert again.returncode == 0, again.stderr
    new = world.calls()[seen:]
    subs = [fg.subcommand(c["argv"]) for c in new]
    assert not {"clone", "fetch", "checkout"} & set(subs)
    assert [x for x in subs if x not in siblings.READ_ONLY_GIT] == ["pull"]
    assert all(c["prompt"] == "0" for c in world.calls())


@pytest.mark.parametrize("apply", [False, True], ids=["dry", "apply"])
@pytest.mark.parametrize("dirty", [CONNECTOR, SPEC])
def test_a_dirty_checkout_is_refused_by_name(world, apply, dirty):
    mixed(world)
    state = fg.state(world.dest(dirty))
    state["dirty"] = True
    (world.dest(dirty) / ".git" / "fake.json").write_text(json.dumps(state))
    before = world.snapshot()
    r = world.cli("siblings", *(["--apply"] if apply else []))
    assert r.returncode == 1
    assert str(world.dest(dirty)) in r.stderr
    assert "local changes" in r.stderr
    assert world.snapshot() == before
    for c in world.calls():
        assert fg.subcommand(c["argv"]) in siblings.READ_ONLY_GIT, c


def test_a_directory_that_is_not_a_checkout_is_refused(world):
    mixed(world)
    world.dest(SANDY).joinpath(".git").rename(world.dest(SANDY) / "x")
    before = world.snapshot()
    r = world.cli("siblings", "--apply")
    assert r.returncode == 1
    assert str(world.dest(SANDY)) in r.stderr
    assert world.snapshot() == before
    shutil.rmtree(world.dest(SANDY))
    world.dest(SANDY).mkdir()
    r = world.cli("siblings", "--apply")
    assert r.returncode == 1
    assert str(world.dest(SANDY)) in r.stderr
    assert not world.dest(ROUTER).exists()


def test_a_pin_the_remote_lacks_fails_naming_it(world):
    url = world.sibs[ROUTER].url
    world.remote[url]["commits"] = [OLD, NEWER]
    world.write_remotes()
    r = world.cli("siblings", "--apply")
    assert r.returncode == 1
    assert ROUTER in r.stderr and "checkout" in r.stderr


def test_siblings_runs_with_no_sandy_checkout(world):
    assert _harness.nothing_beside(world.repo) == []
    assert not (world.repo / "policy.py").exists()
    r = world.cli("siblings", "--apply")
    assert r.returncode == 0, r.stderr
    for name in (ROUTER, CONNECTOR, SANDY):
        assert fg.state(world.dest(name))["head"] == world.pin(name)
    code = ("import sys; sys.path.insert(0, %r); import amap_openshell, "
            "siblings; print(sorted(m for m in ('policy','fleet_policy',"
            "'render','router_link','verbs') if m in sys.modules))"
            % str(world.repo))
    p = subprocess.run([sys.executable, "-c", code], env=world.env,
                       capture_output=True, text=True, timeout=60)
    assert (p.returncode, p.stdout.strip()) == (0, "[]"), p.stderr


def test_siblings_is_standard_library_python_3_9_without_a_shell():
    text = (REPO / "siblings.py").read_text(encoding="utf-8")
    tree = ast.parse(text, feature_version=(3, 9))
    mods = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module.split(".")[0])
    assert mods <= {"__future__", "json", "os", "re", "subprocess", "sys",
                    "pathlib", "typing"}
    assert "shell=True" not in text


def test_siblings_files_are_identifier_clean():
    for name in ("siblings.py", "siblings.json"):
        assert test_l1_runbook.identifier_problems(
            REPO / name, allowed_hosts=("github.com",)) == [], name
