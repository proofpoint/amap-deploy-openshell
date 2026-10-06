"""`router_health_link`: the core of sandy's `router_health`, behind a checked surface.

`verify` loads only the three-outcome core that amap-deploy-sandy agreed to keep
stable (their PR #9): `USED_NAMES`, eleven names. The router sections, the
context, the runner and the fact derivations are this repository's own, in
`router_sections`. The link refuses a checkout that lacks the core, and is
pinned here; sandy's own modules are never importable from this process; and
no path of `verify` reaches a repo-discovery helper of sandy's.
"""

import ast
import importlib.util
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

import _workspace
import policy
import router_health_link as link
import router_sections as rs
import verify
from test_verify import world  # noqa: F401  (the fixture)

SANDY_ONLY = {"sandy_bin", "sandy_home", "sandboxes", "selection_states",
              "not_enrolled"}


def test_every_name_this_deployment_uses_exists():
    rh = link.load()
    for name in link.USED_NAMES:
        assert hasattr(rh, name), name


def test_used_names_are_exactly_the_agreed_core():
    assert link.USED_NAMES == ("PASS", "FAIL", "UNKNOWN", "Verdict",
                               "Unresolved", "Fact", "Check", "check",
                               "unknown", "same_set", "CannotRun")
    assert not hasattr(link, "GENERIC_FACTS")


def test_a_module_missing_a_name_is_refused(tmp_path):
    source = (_workspace.SANDY_ROOT / link.HEALTH_FILE).read_text(
        encoding="utf-8")
    (tmp_path / link.HEALTH_FILE).write_text(
        source + "\ndel same_set\n", encoding="utf-8")
    with pytest.raises(link.RouterHealthNotFound) as e:
        link.load(tmp_path)
    assert "same_set" in str(e.value)
    assert isinstance(e.value, ImportError)


def test_a_module_missing_names_outside_the_core_still_loads(tmp_path):
    """sandy may change its internals without notice."""
    source = (_workspace.SANDY_ROOT / link.HEALTH_FILE).read_text(
        encoding="utf-8")
    (tmp_path / link.HEALTH_FILE).write_text(
        source + "\ndel verify_health\ndel Ctx\ndel FACT_SOURCES\n"
                 "del run_sections\n", encoding="utf-8")
    module = link.load(tmp_path)
    for name in link.USED_NAMES:
        assert hasattr(module, name), name


def test_a_missing_sandy_checkout_is_the_policy_error(tmp_path, monkeypatch):
    """The command line turns this one into a message (by class name)."""
    monkeypatch.setenv(policy.SANDY_VARIABLE, str(tmp_path))
    with pytest.raises(policy.SandyNotFound):
        link.load()


def test_the_checkout_is_not_put_on_sys_path():
    root = str(_workspace.SANDY_ROOT)
    assert root not in sys.path
    assert "amap_sandy" not in sys.modules
    assert link.MODULE_NAME not in sys.modules
    before = list(sys.path)
    link.load()
    assert sys.path == before
    assert link.MODULE_NAME not in sys.modules


def test_a_full_verify_run_never_imports_amap_sandy(world):  # noqa: F811
    code, out, err = world.verify()
    assert code == 0, out + err
    assert "amap_sandy" not in sys.modules
    assert "fleet_policy" not in sys.modules
    assert str(_workspace.SANDY_ROOT) not in sys.path


def test_a_missing_router_checkout_never_reaches_sandys_fleet_policy(
        world, tmp_path, monkeypatch):  # noqa: F811
    before = list(sys.path)
    empty = tmp_path / "no-router"
    empty.mkdir()
    monkeypatch.setenv("AMAP_ROUTER_REPO", str(empty))
    code, out, err = world.verify()
    assert code == 1
    assert out.count("UNKNOWN whether the router") == 2
    assert "Traceback" not in out + err
    assert "fleet_policy" not in sys.modules
    assert "amap_sandy" not in sys.modules
    assert sys.path == before


def _called_names(tree, known):
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in known}


def _fact_names(tree):
    """The string argument of every `x.fact("...")` / `x.value("...")`."""
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("fact", "value") and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            out.add(n.args[0].value)
    return out


def _tree_of(fn):
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _reached_facts():
    """Every fact the router sections reach: through the section functions,
    the helpers they call, and the derivation of each fact."""
    module_functions = {n for n, v in vars(rs).items()
                        if inspect.isfunction(v) and v.__module__ == rs.__name__}
    todo, seen_fns, facts = ["verify_container", "verify_health"], set(), set()
    while todo:
        name = todo.pop()
        if name in seen_fns:
            continue
        seen_fns.add(name)
        tree = _tree_of(getattr(rs, name))
        facts |= _fact_names(tree)
        todo += sorted(_called_names(tree, module_functions) - seen_fns)
    done = set()
    while facts - done:
        fact = sorted(facts - done)[0]
        done.add(fact)
        if fact in rs.FACT_SOURCES:
            facts |= _fact_names(_tree_of(rs.FACT_SOURCES[fact]))
    return facts


def test_our_fact_table_covers_every_fact_sandys_router_sections_reach():
    reached = _reached_facts()
    assert {"router_repo", "router_config", "status_doc", "container"} <= reached
    missing = reached - set(verify.FACT_SOURCES)
    assert missing == set(), ("the router sections reach facts this "
                              f"deployment does not define: {sorted(missing)}")
    assert reached <= set(rs.FACT_SOURCES) | set(rs.SUPPLIED_FACTS)


def test_no_sandy_only_fact_is_registered():
    assert set(verify.FACT_SOURCES) & SANDY_ONLY == set()
    assert set(rs.FACT_SOURCES) & SANDY_ONLY == set()
    assert not set(_reached_facts()) & SANDY_ONLY


def _rh_attributes(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "_rh"}


def test_verify_uses_only_the_named_surface():
    """Every `_rh.<name>` in verify.py and router_sections.py is in USED_NAMES,
    so a new dependency on sandy's module has to be named (and so pinned)
    here first. Only router_sections loads the module."""
    assert _rh_attributes(verify.__file__) == set()
    used = _rh_attributes(rs.__file__)
    assert used and used <= set(link.USED_NAMES), sorted(
        used - set(link.USED_NAMES))


class Recorder:
    """Stands in for sandy's module and records every attribute read."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "seen", set())

    def __getattr__(self, name):
        if not (name.startswith("__") and name.endswith("__")):
            self.seen.add(name)
        return getattr(object.__getattribute__(self, "_real"), name)


def _fresh(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _scenario_healthy(world, tmp_path, monkeypatch):
    return 0


def _scenario_router_absent(world, tmp_path, monkeypatch):
    world.fake.set_flag(router_absent=True)
    return 1


def _scenario_status_fails(world, tmp_path, monkeypatch):
    world.router.set(status_fails=True)
    return 1


def _scenario_docker_ps_fails(world, tmp_path, monkeypatch):
    world.fake.set_flag(fail_docker_ps=True)
    return 1


def _scenario_no_checkout(world, tmp_path, monkeypatch):
    empty = tmp_path / "no-router"
    empty.mkdir()
    monkeypatch.setenv("AMAP_ROUTER_REPO", str(empty))
    return 1


@pytest.mark.parametrize("scenario", [
    _scenario_healthy, _scenario_router_absent, _scenario_status_fails,
    _scenario_docker_ps_fails, _scenario_no_checkout])
def test_verify_reads_no_other_attribute_of_sandys_module_across_every_section(
        scenario, world, tmp_path, monkeypatch):  # noqa: F811
    """The proxy: fresh copies of router_sections and verify load through a
    recorder, and every attribute they read of sandy's module is in the core."""
    expected = scenario(world, tmp_path, monkeypatch)
    rec = Recorder(rs._rh)
    monkeypatch.setattr(link, "load", lambda root=None: rec)
    _fresh("router_sections", rs.__file__, monkeypatch)
    _fresh("verify", verify.__file__, monkeypatch)
    code, out, err = world.verify()
    assert code == expected, out + err
    assert rec.seen <= set(link.USED_NAMES), sorted(rec.seen - set(link.USED_NAMES))
    assert {"check", "unknown", "Unresolved", "Fact"} <= rec.seen
    for section in ("install", "router-config", "gateway", "members",
                    "router-container", "router-health"):
        assert any(ln.startswith(section + ":") for ln in out.splitlines())
    if expected == 0:
        assert all(ln.strip().startswith("PASS") for ln in out.splitlines()
                   if ln.startswith("  "))
