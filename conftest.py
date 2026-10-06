"""Test harness.

The four sources (the router, the connector, the sandy checkout and the
amap-spec directory) are read, never written. Their fingerprints are taken
before and after the session. `openshell`, `docker` and `podman` cannot be
spawned unless a test stubbed them. A missing source ends the run before
collection as a usage error (exit 4) naming every missing source, so the run
never passes as a smaller set of tests.
"""

import sys
from pathlib import Path

REPO = Path(__file__).absolute().parent
for _p in (REPO, REPO / "tests"):  # insert if absent; tests/ ends up first
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
sys.dont_write_bytecode = True  # the suite writes no bytecode into a source

import pytest  # noqa: E402

import _harness  # noqa: E402
import _workspace  # noqa: E402


def pytest_configure(config):
    try:
        _workspace.require_all()
    except _workspace.SiblingMissing as e:
        raise pytest.UsageError(str(e))
    # Installed here, not in a fixture, so it is in place while test modules
    # are imported (including `from subprocess import Popen`).
    _harness.install()


def pytest_unconfigure(config):
    _harness.uninstall()
    # pytest rewrites this file before it can set dont_write_bytecode, so its
    # bytecode is removed here, leaving no stray files in the tree.
    cache = REPO / "__pycache__"
    for pyc in cache.glob("conftest.*.pyc"):
        try:
            pyc.unlink()
        except OSError:
            pass
    try:
        cache.rmdir()  # only succeeds when empty
    except OSError:
        pass


@pytest.fixture(scope="session", autouse=True)
def sibling_fingerprints():
    roots = _workspace.require_all()
    before = {k: _workspace.fingerprint(p) for k, p in roots.items()}
    yield before
    problems = []
    for k, p in roots.items():
        after = _workspace.fingerprint(p)
        if after != before[k]:
            problems.append(
                f"the test session CHANGED the {k} sibling at {p}: a test wrote "
                f"into a read-only source.\n--- before ---\n{before[k]}\n"
                f"--- after ---\n{after}")
    if problems:
        raise AssertionError("\n\n".join(problems))


@pytest.fixture
def fake_bin(tmp_path):
    """A directory whose fakes the binary guard allows to run."""
    d = tmp_path / "fakebin"
    d.mkdir()
    with _harness.stubbed(d):
        yield d


@pytest.fixture(autouse=True)
def _private_config_home(tmp_path_factory, monkeypatch):
    """Every test gets its own XDG_CONFIG_HOME, so nothing writes the real
    ~/.config (the home record, home_record.py). AMAP_OPENSHELL_HOME starts
    unset and is restored afterwards, because amap_openshell.main fills it
    from the record."""
    monkeypatch.setenv("XDG_CONFIG_HOME",
                       str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.delenv("AMAP_OPENSHELL_HOME", raising=False)
