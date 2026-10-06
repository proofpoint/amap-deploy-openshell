"""The fleet policy adapter: fleet.json, the addresses it yields, and the proof
that amap-deploy-sandy's `fleet_policy` is imported and not copied."""

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _workspace
import policy
from policy import PolicyError, SandyNotFound

REPO = Path(__file__).absolute().parents[1]
EXAMPLE = REPO / "examples" / "fleet.json"

# The names the adapter may use from `fleet_policy`, written out here so a change
# to the adapter's list has to change this one too.
D1_CORE = {
    "PolicyError", "load_policy", "default_policy", "resolve_peers",
    "one_sided", "resolve_task_graph", "resolve_task_deny",
    "transpose_task_graph", "overlapping_pairs", "address_for",
    "router_address", "FLEET_DOMAIN_KEY", "TASK_GRAPH_KEY", "TASK_GRAPH_ALL",
    "TASK_DENY_KEY", "ALLOW_ANY", "GROUP_SIGIL", "ALL_GROUP", "SCHEMA_VERSION",
    "derived_fleet_domain", "DEFAULT_DOMAIN_BASE",   # D21
}

# Built in two parts so this file does not contain the name it looks for.
_BANNED_MODULE = "policy" + "_checks"

_DROP = object()


@pytest.fixture(scope="module", name="v")
def _spec_validate():
    path = _workspace.SPEC_DIR / "fixtures" / "validate.py"
    spec = importlib.util.spec_from_file_location("amap_spec_validate", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo_py_files():
    for dirpath, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs
                         if d not in (".git", ".claude", "__pycache__"))
        for f in sorted(files):
            if f.endswith(".py"):
                yield Path(dirpath) / f


def _fleet(tmp_path, **overrides):
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if value is _DROP:
            doc.pop(key, None)
        else:
            doc[key] = value
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _sandy_tree():
    return ast.parse((_workspace.SANDY_ROOT / "fleet_policy.py")
                     .read_text(encoding="utf-8"))


def _sandy_function_names():
    return {n.name for n in ast.walk(_sandy_tree())
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _without_docstring(fn):
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _body_key(body):
    return ast.dump(ast.Module(body=body, type_ignores=[]))


def _sandy_function_bodies():
    return {n.name: _without_docstring(n) for n in ast.walk(_sandy_tree())
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# ------------------------------------------------------------------ addresses

def test_every_example_member_address_passes_the_roster_address_check(v):
    """Checked with amap-spec fixtures/validate.py (check_document under the
    roster- prefix, schemas/roster.schema.json): schema validation only; its two
    post-checks are gated to result-/notice-/message-/peer- and do not run for a
    roster."""
    pol = policy.load_fleet(EXAMPLE)
    names = policy.named_instances(pol)
    assert names == ["alpha", "beta"]
    addrs = policy.addresses(pol, names)

    schema = v.schema_for("roster-openshell.json")
    assert schema["title"] == "roster"
    address_schema = schema["properties"]["members"]["items"]["properties"]["address"]
    for a in addrs.values():
        assert v.validate(a, address_schema) == []

    doc = {
        "contract_version": "2",
        "router": policy.router_address(pol["fleet_domain"]),
        "written_at": "2026-01-01T00:00:00Z",
        "members": [{"address": a, "state": "admitted"}
                    for a in sorted(addrs.values())],
    }
    assert v.check_document("roster-openshell.json", doc) == []


def test_the_roster_address_check_rejects_a_bare_name(v):
    """Proves the check is live: a name with no domain is refused."""
    doc = {
        "contract_version": "2",
        "router": "amap.router@agents.example.org",
        "written_at": "2026-01-01T00:00:00Z",
        "members": [{"address": "alpha", "state": "admitted"}],
    }
    errs = v.check_document("roster-openshell.json", doc)
    assert errs
    assert any("address" in e for e in errs)


@pytest.mark.parametrize("name", ["a", "z" * 19, "0-9", "a-b-c-d"])
def test_any_valid_member_name_makes_a_conforming_address(v, name):
    pol = policy.load_fleet(EXAMPLE)
    addr = policy.addresses(pol, [name])[name]
    assert addr == f"{name}@agents.example.org"
    schema = v.schema_for("roster-openshell.json")
    address_schema = schema["properties"]["members"]["items"]["properties"]["address"]
    assert v.validate(addr, address_schema) == []


def test_addresses_come_from_fleet_policy_address_for(monkeypatch):
    pol = policy.load_fleet(EXAMPLE)
    names = ["alpha", "beta"]
    expected = {n: policy._fp.address_for(n, "agents.example.org")
                for n in names}
    assert policy.addresses(pol, names) == expected

    def fake_addr(name, domain):
        return "X"

    monkeypatch.setattr(policy._fp, "address_for", fake_addr)
    assert set(policy.addresses(pol, names).values()) == {"X"}


def test_addresses_refuse_an_invalid_name():
    pol = policy.load_fleet(EXAMPLE)
    with pytest.raises(PolicyError, match="lowercase"):
        policy.addresses(pol, ["Alpha"])


# --------------------------------------------------------------------- loading

def test_load_fleet_loads_the_example():
    pol = policy.load_fleet(EXAMPLE)
    assert policy.workspace_of(pol) == "default"
    assert pol["fleet_domain"] == "agents.example.org"
    assert pol["task_graph"] == {"beta": ["alpha"]}
    assert "sandboxes" not in pol
    assert "agents" not in pol


def test_load_fleet_refuses_an_absent_file(tmp_path):
    with pytest.raises(PolicyError, match="no fleet policy"):
        policy.load_fleet(tmp_path / "fleet.json")


def test_load_fleet_refuses_an_empty_file(tmp_path):
    p = tmp_path / "fleet.json"
    p.write_bytes(b"")
    with pytest.raises(PolicyError, match="not valid JSON"):
        policy.load_fleet(p)


def test_load_fleet_refuses_a_sandy_manifest():
    manifest = _workspace.SANDY_ROOT / "examples" / "feature.json"
    with pytest.raises(PolicyError, match="manifest"):
        policy.load_fleet(manifest)


@pytest.mark.parametrize("key, value", [
    ("sandboxes", {"include": ["*"], "exclude": []}),
    ("agents", {"include": ["claude"], "exclude": []}),
    ("container_recreate_interval_hours", 24),
])
def test_load_fleet_refuses_sandy_only_keys(tmp_path, key, value):
    p = _fleet(tmp_path, **{key: value})
    with pytest.raises(PolicyError) as excinfo:
        policy.load_fleet(p)
    assert key in str(excinfo.value)


def test_load_fleet_requires_a_workspace(tmp_path):
    p = _fleet(tmp_path, workspace=_DROP)
    with pytest.raises(PolicyError, match="'workspace' is required"):
        policy.load_fleet(p)


def test_load_fleet_refuses_more_than_one_workspace(tmp_path):
    p = _fleet(tmp_path, workspace=["default", "team"])
    with pytest.raises(PolicyError, match="one OpenShell workspace"):
        policy.load_fleet(p)


def test_load_fleet_refuses_an_invalid_workspace_name(tmp_path):
    p = _fleet(tmp_path, workspace="Team_A")
    with pytest.raises(PolicyError, match="lowercase"):
        policy.load_fleet(p)


def test_load_fleet_requires_a_fleet_domain(tmp_path):
    p = _fleet(tmp_path, fleet_domain=_DROP)
    with pytest.raises(PolicyError, match="fleet_domain"):
        policy.load_fleet(p)


@pytest.mark.parametrize("overrides, where, fragment", [
    pytest.param({"groups": {"g": ["Alpha_1"]}}, "groups.g", "lowercase",
                 id="groups"),
    pytest.param({"peers": {"Alpha_1": []}}, "peers (key)", "lowercase",
                 id="peers-key"),
    pytest.param({"peers": {"alpha": ["Alpha_1"]}}, "peers.alpha", "lowercase",
                 id="peers-value"),
    pytest.param({"default_peers": ["Alpha_1"]}, "default_peers", "lowercase",
                 id="default_peers"),
    pytest.param({"task_graph": {"Alpha_1": ["alpha"]}}, "task_graph (key)",
                 "lowercase", id="task_graph-key"),
    pytest.param({"task_graph": {"beta": ["a" * 20]}}, "task_graph.beta",
                 "at most 19", id="task_graph-sender"),
    pytest.param({"task_deny": [["alpha", "Alpha_1"]]}, "task_deny[0]",
                 "lowercase", id="task_deny"),
])
def test_load_fleet_refuses_an_invalid_instance_name(tmp_path, overrides,
                                                     where, fragment):
    p = _fleet(tmp_path, **overrides)
    with pytest.raises(PolicyError) as excinfo:
        policy.load_fleet(p)
    assert where in str(excinfo.value)
    assert fragment in str(excinfo.value)


# ------------------------------------------------------------------- resolving

def test_resolve_the_example():
    pol = policy.load_fleet(EXAMPLE)
    r = policy.resolve(pol, ["alpha", "beta"])
    assert r.peers == {"alpha": [], "beta": []}
    assert r.task_graph == {"alpha": [], "beta": ["alpha"]}
    assert r.one_sided == []
    assert r.overlaps == []


def test_resolve_reports_an_overlap(tmp_path):
    pol = policy.load_fleet(_fleet(tmp_path, default_peers=["@all"]))
    assert policy.resolve(pol, ["alpha", "beta"]).overlaps


def _load_and_resolve(path):
    pol = policy.load_fleet(path)
    policy.resolve(pol, ["alpha", "beta"])


@pytest.mark.parametrize("overrides, fleet_json", [
    pytest.param({"peers": {"ghost": []}}, False, id="stray-peers-key"),
    pytest.param({"groups": {"g": ["ghost"]}}, True, id="stray-group-member"),
    pytest.param({"task_graph": {"ghost": ["alpha"]}}, False,
                 id="stray-task-graph-recipient"),
    pytest.param({"task_graph": {"beta": ["ghost"]}}, True,
                 id="stray-task-graph-sender"),
    pytest.param({"task_deny": [["alpha", "ghost"]]}, False,
                 id="stray-task-deny"),
    pytest.param({"default_peers": ["ALLOW_ANY", "alpha"]}, False,
                 id="allow-any-mixed"),
])
def test_sandys_errors_are_reworded(tmp_path, overrides, fleet_json):
    p = _fleet(tmp_path, **overrides)
    with pytest.raises(PolicyError) as excinfo:
        _load_and_resolve(p)
    message = str(excinfo.value)
    if fleet_json:
        assert "fleet.json" in message
    # The temporary path may carry the user's name or the test's name.
    text = message.replace(str(p), "").lower()
    for word in ("manifest", "sandy", "selection rule", "module docstring"):
        assert word not in text, message


# ----------------------------------------------------------- finding the source

def _fake_sandy(root):
    d = root / "amap-deploy-sandy"
    d.mkdir(parents=True)
    (d / "fleet_policy.py").write_text("", encoding="utf-8")
    return d


def test_find_sandy_matches_the_harness():
    found = policy.find_sandy()
    assert os.path.samefile(found / "fleet_policy.py",
                            _workspace.SANDY_ROOT / "fleet_policy.py")


def test_find_sandy_the_variable_is_the_only_place_searched(tmp_path):
    ws = tmp_path / "ws"
    _fake_sandy(ws)
    (ws / "repo").mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SandyNotFound) as excinfo:
        policy.find_sandy(env={"AMAP_SANDY_REPO": str(empty)},
                          start=ws / "repo")
    assert "AMAP_SANDY_REPO" in str(excinfo.value)
    assert "fleet_policy.py" in str(excinfo.value)


def test_find_sandy_an_empty_variable_counts_as_unset(tmp_path):
    ws = tmp_path / "ws"
    sandy = _fake_sandy(ws)
    (ws / "repo").mkdir()
    found = policy.find_sandy(env={"AMAP_SANDY_REPO": ""}, start=ws / "repo")
    assert found == sandy.absolute()


def test_find_sandy_takes_the_nearest_confirmed_directory(tmp_path):
    far = _fake_sandy(tmp_path / "a")
    (tmp_path / "a" / "b" / "amap-deploy-sandy").mkdir(parents=True)
    start = tmp_path / "a" / "b" / "c" / "repo"
    start.mkdir(parents=True)
    # b's directory has the right name and no fleet_policy.py: skipped.
    assert policy.find_sandy(env={}, start=start) == far.absolute()
    near = _fake_sandy(tmp_path / "a" / "b" / "c")
    assert policy.find_sandy(env={}, start=start) == near.absolute()


def _python(code, cwd, env):
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=120)


def test_import_fails_loudly_without_the_sandy_checkout(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {k: val for k, val in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["AMAP_SANDY_REPO"] = str(empty)
    r = _python("import policy", REPO, env)
    assert r.returncode != 0
    for fragment in ("SandyNotFound", "AMAP_SANDY_REPO", "fleet_policy.py"):
        assert fragment in r.stderr


def test_loading_the_core_writes_no_bytecode_into_the_sandy_checkout(tmp_path):
    repo = tmp_path / "repo"
    sandy = tmp_path / "sandy"
    repo.mkdir()
    sandy.mkdir()
    shutil.copy(REPO / "policy.py", repo / "policy.py")
    shutil.copy(REPO / "membership.py", repo / "membership.py")
    shutil.copy(_workspace.SANDY_ROOT / "fleet_policy.py",
                sandy / "fleet_policy.py")
    env = {k: val for k, val in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE")}
    env["AMAP_SANDY_REPO"] = str(sandy)
    r = _python("import policy; print(policy._fp.__file__)", repo, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(sandy / "fleet_policy.py")
    assert not (sandy / "__pycache__").exists()


# ------------------------------------------------- imported, not copied (AC4)

def test_the_core_is_sandys_module_not_a_copy():
    assert os.path.samefile(policy._fp.__file__,
                            _workspace.SANDY_ROOT / "fleet_policy.py")
    assert policy.PolicyError is policy._fp.PolicyError
    assert policy.router_address is policy._fp.router_address


def test_the_agreed_core_is_d1_and_exists():
    assert set(policy.CORE_NAMES) == D1_CORE
    for name in D1_CORE:
        assert hasattr(policy._fp, name), name


def test_the_adapter_uses_only_the_agreed_core():
    tree = ast.parse((REPO / "policy.py").read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "_fp"}
    assert used
    assert used <= D1_CORE, sorted(used - D1_CORE)


def test_policy_checks_is_not_imported():
    banned = {_BANNED_MODULE, _BANNED_MODULE + ".py"}
    for path in _repo_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == _BANNED_MODULE
                               for a in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != _BANNED_MODULE, path
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.strip() not in banned, path


def test_no_function_of_fleet_policy_is_defined_in_this_repo():
    theirs = _sandy_function_names()
    assert theirs
    for path in _repo_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        ours = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not (ours & theirs), (path.name, sorted(ours & theirs))


def test_no_function_body_is_copied_from_fleet_policy():
    theirs = {name: _body_key(body)
              for name, body in _sandy_function_bodies().items()
              if len(body) >= 2}
    assert theirs
    for path in _repo_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = _body_key(_without_docstring(node))
                copied = [n for n, k in theirs.items() if k == key]
                assert not copied, (path.name, node.name, copied)


# --- the derived fleet domain (S11, D21) -------------------------------------

def test_domain_for_is_sandys_derivation():
    got = policy.domain_for("openshell", "Host-1.local", "internal")
    theirs = policy._fp.derived_fleet_domain
    assert got == theirs("openshell", "Host-1.local", "internal") \
        == "openshell.host-1.internal"
    assert policy.DEFAULT_DOMAIN_BASE is policy._fp.DEFAULT_DOMAIN_BASE


def test_domain_for_refuses_a_bad_base():
    with pytest.raises(PolicyError, match="fleet domain base"):
        policy.domain_for("openshell", "h", "Not_A.Domain")


def test_domain_for_calls_fleet_policy(monkeypatch):
    monkeypatch.setattr(policy._fp, "derived_fleet_domain",
                        lambda runtime, hostname, base: "X")
    assert policy.domain_for("openshell", "h", "internal") == "X"


def test_the_derivation_is_not_copied():
    assert {"host_label", "derived_fleet_domain"} <= _sandy_function_names()
    wanted = {"host_label", "derived_fleet_domain"}
    theirs = set()
    for fn in ast.walk(_sandy_tree()):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in wanted):
            continue
        skip = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.JoinedStr):
                skip.update(id(c) for c in ast.walk(n))
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and len(n.value) >= 4 and id(n) not in skip):
                theirs.add(n.value)
    assert theirs and "[^a-z0-9" + "-]" in theirs
    call = "derived_fleet_domain" + "("
    for path in _repo_py_files():
        text = path.read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                assert n.value not in theirs, (str(path), n.value)
        if path.name != "policy.py":
            assert call not in text, str(path)


def test_sandys_interface_test_pins_the_derivation():
    src = (_workspace.SANDY_ROOT / "tests" / "test_shared_policy_surface.py")
    found = {}
    for n in ast.parse(src.read_text(encoding="utf-8")).body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            if n.targets[0].id in ("SHARED_FUNCTIONS", "SHARED_CONSTANTS"):
                found[n.targets[0].id] = ast.literal_eval(n.value)
    assert found["SHARED_FUNCTIONS"]["derived_fleet_domain"] == [
        "runtime", "hostname", "base"]
    assert found["SHARED_CONSTANTS"]["DEFAULT_DOMAIN_BASE"] == "internal"
