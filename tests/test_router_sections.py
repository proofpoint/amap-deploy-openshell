"""S7b: the router sections are this repository's own.

`router_sections.py` is a copy of amap-deploy-sandy's `router_health.py` at
cd03903, reduced to the two router sections and the machinery they need. These
tests pin its header, what is defined here and what is sandy's agreed core, the
supported way to supply facts, and the missing-router path that must give this
repository's remedy and reach none of sandy's modules.

Nothing here runs docker, OpenShell or the router: the subprocess wrapper is
stubbed where a call would otherwise be made.
"""

import argparse
import ast
import os
import re
import sys
from pathlib import Path

import pytest

import _workspace  # noqa: F401  (the sources the suite checks against)
import amap_openshell  # noqa: F401  (imports verify lazily, in main)
import router_health_link as link
import router_link
import router_sections as rs
import verify
from test_verify import world  # noqa: F401  (the fixture)

REPO = Path(__file__).absolute().parents[1]
SANDY_NAMES = {"fleet_policy_mod", "provisioner", "router_not_found_message",
               "amap_sandy", "fleet_policy", "host_facts_doc",
               "write_host_facts"}


def _supplied(**over):
    sup = {n: (lambda c: (1, "p")) for n in rs.SUPPLIED_FACTS}
    sup.update(over)
    return sup


def test_the_header_names_its_origin_and_every_copied_function():
    doc = rs.__doc__
    for token in ("amap-deploy-sandy", "router_health.py", "cd03903", "Apache"):
        assert token in doc, token
    for name in rs.COPIED_FROM_SANDY:
        assert f"`{name}`" in doc, name
        assert hasattr(rs, name), name
    assert {"verify_container", "verify_health"} <= set(rs.COPIED_FROM_SANDY)


def test_the_owned_code_is_defined_here_and_only_the_core_is_sandys():
    owned = [rs.run, rs.Proc, rs.Ctx, rs.Section, rs.Outcome, rs.run_sections,
             rs.verify_container, rs.verify_health, rs.problem_lines,
             *rs.FACT_SOURCES.values()]
    for obj in owned:
        assert obj.__module__ == rs.__name__, obj
    core = [rs.Unresolved, rs.Fact, rs.Check, rs.check, rs.unknown,
            rs.same_set, rs.CannotRun]
    for obj in core:
        assert obj.__module__ == link.MODULE_NAME, obj


def test_the_fact_derivations_are_the_eleven_s7_took_by_reference():
    assert set(rs.FACT_SOURCES) == {
        "router_doc", "config_instances", "state_dir", "selected_json",
        "status_doc", "announced_interval", "admitted", "max_poll_age",
        "true", "zero", "empty"}
    assert set(rs.SUPPLIED_FACTS) <= set(verify._OURS)


def test_ctx_refuses_a_missing_supplied_fact_and_a_restated_derivation():
    with pytest.raises(KeyError) as e:
        rs.Ctx({})
    for name in rs.SUPPLIED_FACTS:
        assert name in str(e.value)
    with pytest.raises(ValueError) as e:
        rs.Ctx(_supplied(true=lambda c: (False, "x")))
    assert "true" in str(e.value)
    ctx = rs.Ctx(_supplied())
    with pytest.raises(KeyError):
        ctx.fact("nope")

    def broken(c):
        raise RuntimeError("no")

    fact = rs.Ctx(_supplied(container=broken)).fact("container")
    assert fact.known is False
    assert "(derivation failed)" in fact.provenance


def _args(world):
    return argparse.Namespace(home=str(world.home), image="img", run_as=None,
                              restart_policy=False, openshell="openshell",
                              gateway_toml=None)


def test_verify_ctx_reads_the_whole_table(world):  # noqa: F811
    ctx = verify.Ctx(verify.options_from(_args(world)), None)
    assert ctx.sources.keys() == verify.FACT_SOURCES.keys()
    assert ctx.docker == "docker"


def test_a_missing_router_with_unreadable_status_is_unknown_with_this_repos_remedy(
        world, tmp_path, monkeypatch):  # noqa: F811
    world.router.set(status_fails=True)
    empty = tmp_path / "no-router"
    empty.mkdir()
    monkeypatch.setenv("AMAP_ROUTER_REPO", str(empty))
    before = list(sys.path)
    assert "fleet_policy" not in sys.modules
    assert "amap_sandy" not in sys.modules
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "the router reports itself healthy") == "UNKNOWN"
    lines = [ln for ln in out.splitlines() if ln.startswith(
        "UNKNOWN whether the router reports itself healthy")]
    assert len(lines) == 1
    with pytest.raises(router_link.RouterNotFound) as e:
        router_link.find_router(dict(os.environ))
    assert router_link.not_found_remedy() in lines[0]
    assert str(e.value) in lines[0]
    assert "that is the only checkout that was searched" not in out
    assert "cannot locate the router checkout" not in out
    assert sys.path == before
    assert "fleet_policy" not in sys.modules
    assert "amap_sandy" not in sys.modules
    assert "Traceback" not in out + err


def test_the_owned_health_section_gives_this_repos_remedy_when_the_router_is_missing(
        tmp_path):
    empty = tmp_path / "no-router"
    empty.mkdir()
    with pytest.raises(router_link.RouterNotFound) as e:
        router_link.find_router(env={"AMAP_ROUTER_REPO": str(empty)})
    message = str(e.value)
    ctx = rs.Ctx(_supplied(
        router_repo=lambda c: (rs.Unresolved(message), "p"),
        router_config=lambda c: (tmp_path / "router.json", "p"),
        container=lambda c: ("c", "p"), image=lambda c: ("i", "p")))
    before = list(sys.path)
    assert rs.runsh_argv(ctx, rs.SECTION_HEALTH, "status") == ()
    checks = list(rs.verify_health(ctx))
    assert len(checks) == 1
    assert checks[0].claim == rs.HEALTH_STATUS
    assert checks[0].result == rs.UNKNOWN
    assert checks[0].remedy == router_link.not_found_remedy()
    assert sys.path == before
    assert "fleet_policy" not in sys.modules
    assert "amap_sandy" not in sys.modules


def test_the_docker_program_is_ctx_docker(tmp_path, monkeypatch):
    seen = []

    def recorder(argv, **kwargs):
        seen.append(list(argv))
        return rs.Proc(tuple(argv), 0, "", "")

    monkeypatch.setattr(rs, "run", recorder)
    ctx = rs.Ctx(_supplied(), docker="/fake/docker")
    rs._inspect(ctx, "c", "{{x}}")
    assert seen[-1][0] == "/fake/docker"
    assert rs._container_presence(ctx, "c") is False
    assert seen[-1][0] == "/fake/docker"


def test_the_one_shot_names_are_this_deployments(tmp_path):
    name = "amap-openshell-verify-router-health"
    assert rs.oneshot_name(rs.SECTION_HEALTH) == name
    ctx = rs.Ctx(_supplied(router_repo=lambda c: (tmp_path, "p"),
                           router_config=lambda c: (tmp_path / "r.json", "p")))
    argv = rs.runsh_argv(ctx, rs.SECTION_HEALTH, "status")
    assert argv[argv.index("--name") + 1] == name


def _names(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.name.split(".")[-1])
            out.add(n.asname or "")
    return out


def _on_sys_path(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in ("insert", "append")
            and isinstance(n.value, ast.Attribute) and n.value.attr == "path"
            and isinstance(n.value.value, ast.Name)
            and n.value.value.id == "sys"]


def test_nothing_reaches_sandys_modules_or_sys_path():
    for path in (rs.__file__, verify.__file__, link.__file__):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        assert not (_names(tree) & SANDY_NAMES), path
        assert path == link.__file__ or not _on_sys_path(tree), path
    vtree = ast.parse(Path(verify.__file__).read_text(encoding="utf-8"))
    assert "_rh" not in _names(vtree)
    for n in ast.walk(vtree):
        if isinstance(n, ast.Import):
            assert "router_health_link" not in [a.name for a in n.names]
        if isinstance(n, ast.ImportFrom):
            assert n.module != "router_health_link"
    rtree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(rtree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "_rh"}
    assert used and used <= set(link.USED_NAMES)


def test_router_sections_text_is_identifier_clean():
    import router_config
    domain = re.compile(r"\b[\w-]+\.(com|net|io|dev|co)\b")
    lanes = set(router_config.LANES)
    text = (REPO / "router_sections.py").read_text(encoding="utf-8")
    assert "/home/" not in text and "/Users/" not in text
    assert not domain.search(text)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in lanes, node.value


# --- S8d: the text L2 found wrong ---------------------------------------------

def _do_not_texts():
    texts = []
    for name in ("verify.py", "router_sections.py"):
        tree_ = ast.parse((REPO / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree_):
            if isinstance(node, ast.keyword) and node.arg == "do_not" \
                    and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                texts.append(node.value.value)
    return texts


def _line_for(text):
    chk = rs.check(claim="c", expected=rs.Fact("c", True, "p"), actual=False,
                   remedy="r", do_not=text)
    out = rs.Outcome(rs.Section("s", "t", lambda ctx: iter(())), [chk])
    (line,) = rs.problem_lines([out])
    return line


def test_problem_lines_never_says_do_not_twice():
    texts = _do_not_texts()
    assert texts
    for text in texts:
        line = _line_for(text)
        assert "do not: do not" not in line
        assert line.endswith(f"({text})")
    assert _line_for("never X").endswith("(do not: never X)")


def test_a_stopped_router_containers_remedy_is_docker_start(world):
    world.fake.set_flag(router_stopped=True)
    code, out, err = world.verify()
    (line,) = [ln for ln in out.splitlines()
               if "NOT the router container is running" in ln]
    assert "docker start amap-router-local" in line
    assert line.count("docker/run.sh --detach") == 1
    assert "docker/run.sh --detach from the router checkout only when there " \
           "is no container" in line
    world.fake.set_flag(router_stopped=False, router_absent=True)
    code, out, err = world.verify()
    (line,) = [ln for ln in out.splitlines()
               if "NOT the router container is running" in ln]
    assert "docker/run.sh" in line and "--detach" in line
    assert "docker start" not in line


def test_the_header_records_where_the_text_differs_from_sandys():
    doc = rs.__doc__
    for word in ("differs from sandy's", "do not", "docker start", "2026-10-04"):
        assert word in doc


def test_the_header_records_sandys_own_fix_and_where_this_copy_still_differs():
    doc = rs.__doc__
    for word in ("2b1dcf4", "eeecad0", "`verify_container`"):
        assert word in doc
    sandy = (_workspace.SANDY_ROOT / "router_health.py").read_text(
        encoding="utf-8")
    for fact in ("docker start {name}", 'do_not="do not docker rm',
                 'line += f" ({c.do_not})"'):
        assert fact in sandy, fact
    assert "do not docker rm" not in Path(rs.__file__).read_text(encoding="utf-8")
