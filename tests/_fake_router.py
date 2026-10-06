"""A throwaway router checkout for `verify`'s tests.

Sandy's router sections run the router's own tools by path: `docker/run.sh` for
`status`, and `docker/derive-mounts.py` for the mount set. Those are executed by
path, which the binary guard does not cover, so a test must never point at the
real sibling. `FakeRouter` writes a checkout into a directory of the test's own
and answers the two tools from a flags file. It runs nothing: `run.sh` reads the
router's config, and prints.

`router/reset.py` is empty: it is only the file `router_link.find_router`
confirms a checkout by.

The mount rows follow the real emitter's shape, parents first: the directory of
the verdict read-only, then the instance tree and the state directory
read-write. The container's own mounts are those rows plus the config, which
`run.sh` adds (the emitter does not print it).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

FLAGS = {"polls": 3, "stale": False, "no_interval": False,
         "no_admitted": False, "admitted": None, "discovery_flagged": False,
         "status_fails": False}
INTERVAL_S = 5.0
STALE_AGE_S = 3600

RUN_SH = """#!{python}
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {tests!r})
import _fake_router
sys.exit(_fake_router.run_sh({flags!r}, sys.argv[1:]))
"""
DERIVE = """#!{python}
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {tests!r})
import _fake_router
sys.exit(_fake_router.derive_mounts(sys.argv[1:]))
"""


def derive_rows(config: str) -> List[Tuple[str, bool]]:
    with open(config, encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = [(os.path.dirname(os.path.abspath(doc["selected_json"])), False),
            (os.path.abspath(doc["instances_dir"]), True),
            (os.path.abspath(doc["state_dir"]), True)]
    return sorted(rows, key=lambda r: len([p for p in r[0].split("/") if p]))


def container_rows(config: str) -> List[Tuple[str, bool]]:
    """What the router container mounts: the emitter's rows and the config."""
    return derive_rows(config) + [(os.path.abspath(config), False)]


def derive_mounts(argv: List[str]) -> int:
    for path, rw in derive_rows(argv[0]):
        print(f"{path}\t{'rw' if rw else 'ro'}")
    return 0


def _admitted(doc: Dict[str, Any]) -> List[str]:
    with open(doc["selected_json"], encoding="utf-8") as fh:
        selected = {e["slug"] for e in json.load(fh)["selected"]}
    dirs = {d for d in os.listdir(doc["instances_dir"])
            if os.path.isdir(os.path.join(doc["instances_dir"], d))}
    return sorted(dirs & selected)


def run_sh(flags_path: str, argv: List[str]) -> int:
    import datetime
    with open(flags_path, encoding="utf-8") as fh:
        flags = {**FLAGS, **json.load(fh)}
    config = argv[argv.index("--config") + 1]
    rest = argv[argv.index("--") + 1:]
    if flags["status_fails"]:
        print("router has not written status yet", file=sys.stderr)
        return 1
    with open(config, encoding="utf-8") as fh:
        doc = json.load(fh)
    if rest == ["status", "--json"]:
        admitted = (flags["admitted"] if flags["admitted"] is not None
                    else _admitted(doc))
        now = datetime.datetime.now(datetime.timezone.utc)
        if flags["stale"]:
            now -= datetime.timedelta(seconds=STALE_AGE_S)
        status: Dict[str, Any] = {
            "polls": flags["polls"], "last_poll_ts": now.isoformat(),
            "instances": {n: {} for n in admitted},
            "totals": {"outbound_errored": 0, "instance_errored": 0}}
        if not flags["no_interval"]:
            status["interval_s"] = INTERVAL_S
        if not flags["no_admitted"]:
            status["admitted"] = admitted
        print(json.dumps(status))
        return 0
    if rest == ["status"]:
        print("router status")
        if flags["discovery_flagged"]:
            print("discovery:")
            print("  ** NO DIRECTORY  ghost: selected but its tree does not "
                  "exist")
        return 0
    print("fake run.sh: unhandled command", file=sys.stderr)
    return 2


class FakeRouter:
    def __init__(self, tmp: Path, home: str) -> None:
        self.home = home
        self._root = Path(tmp) / "router-checkout"
        self._flags = Path(tmp) / "fake-router-flags.json"
        self._flags.write_text(json.dumps({}), encoding="utf-8")
        tests = str(Path(__file__).absolute().parent)
        (self._root / "router").mkdir(parents=True)
        (self._root / "router" / "reset.py").write_text("")
        docker = self._root / "docker"
        docker.mkdir()
        for name, body in (("run.sh", RUN_SH), ("derive-mounts.py", DERIVE)):
            path = docker / name
            path.write_text(body.format(python=sys.executable, tests=tests,
                                        flags=str(self._flags)))
            path.chmod(0o755)

    def root(self) -> Path:
        return self._root

    def set(self, **flags: Any) -> None:
        unknown = set(flags) - set(FLAGS)
        assert not unknown, f"unknown fake router flag(s): {sorted(unknown)}"
        have = json.loads(self._flags.read_text(encoding="utf-8"))
        have.update(flags)
        self._flags.write_text(json.dumps(have), encoding="utf-8")
