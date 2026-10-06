"""The interceptor's boundary and its locks (docs/INTERCEPTOR.md tests 23-24).

grpc and protobuf are confined to `interceptor/server.py` and the generated
`interceptor/_gen/` (D5). Everything else, here included, is the standard
library. Dependencies are hash-locked, and the locks and stubs are the output
of `interceptor/regen.sh --apply`.
"""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

import verbs
from interceptor import locks

REPO = Path(__file__).absolute().parents[1]
IDIR = REPO / "interceptor"
SKIP_DIRS = {".git", ".claude", "__pycache__", "amap-l1"}
FORBIDDEN_TOP = {"grpc", "google"}
LOCK_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            out.append(n.module or "")
            out += [f"{n.module}.{a.name}" for a in n.names]
    return out


def _python_files():
    for base, dirs, files in os.walk(REPO, followlinks=False):
        rel = Path(base).relative_to(REPO)
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS
            and (rel / d).as_posix() not in ("interceptor/_gen",
                                             "interceptor/tests"))
        for f in sorted(files):
            p = Path(base) / f
            if f.endswith(".py") and p != IDIR / "server.py":
                yield p


def _is_generated(name: str) -> bool:
    top = name.split(".")[0]
    return (top in FORBIDDEN_TOP or top == "_gen" or top.endswith("_pb2")
            or top.endswith("_pb2_grpc"))


def _stdlib(top: str) -> bool:
    if top in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return False
    origin = getattr(spec, "origin", None) if spec else None
    if not origin or origin in ("built-in", "frozen"):
        return spec is not None
    std = sysconfig.get_paths()["stdlib"]
    return origin.startswith(std) and "site-packages" not in origin


def test_only_server_imports_grpc():
    files = list(_python_files())
    assert files
    for p in files:
        for name in _imports(p):
            assert not _is_generated(name), (p.relative_to(REPO), name)
            if p.parent != IDIR:
                assert not name.startswith("interceptor.server"), p
                assert not name.startswith("interceptor._gen"), p
    repo_modules = {p.stem for p in REPO.glob("*.py")} | {"interceptor"}
    for name in ("rule.py", "wire.py", "locks.py", "__init__.py"):
        for imp in _imports(IDIR / name):
            top = imp.split(".")[0]
            if name == "locks.py":
                assert _stdlib(top), (name, imp)
            else:
                assert _stdlib(top) or top in repo_modules, (name, imp)


def _requirements(path: Path):
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    top, reqs = None, {}
    for ln in text.splitlines():
        if ln.startswith("# top-level:"):
            top = set(ln.split(":", 1)[1].split())
        if not ln.strip() or ln.startswith("#"):
            continue
        assert LOCK_LINE.match(ln), ln
        assert HASH.search(ln), ln
        name, _, rest = ln.partition("==")
        reqs[name] = rest.split()[0]
    return top, reqs


def test_requirements_are_hash_locked():
    top, run = _requirements(IDIR / "requirements.txt")
    assert top == {"grpcio", "protobuf"}
    assert top <= set(run)
    top, gen = _requirements(IDIR / "requirements-gen.txt")
    assert "grpcio-tools" in top and top <= set(gen)


def test_runtime_pins_match_the_generation_pins():
    _, run = _requirements(IDIR / "requirements.txt")
    _, gen = _requirements(IDIR / "requirements-gen.txt")
    assert run["grpcio"] == gen["grpcio-tools"] == gen["grpcio"]
    assert run["protobuf"] == gen["protobuf"]
    for p in (IDIR / "_gen").glob("*_pb2_grpc.py"):
        m = re.search(r"GRPC_GENERATED_VERSION = '([^']+)'", p.read_text())
        assert m and m.group(1) == run["grpcio"], p.name


def test_vendored_protos_match_their_source():
    import hashlib
    _, _, digests = locks.read_source(str(IDIR / "proto" / locks.SOURCE_NAME))
    have = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (IDIR / "proto").glob("*.proto")}
    assert have == digests
    closure = locks.import_closure(
        lambda f: (IDIR / "proto" / f).read_text(encoding="utf-8"))
    assert set(closure) == set(digests)


def test_wheel_pin_reads_wheel_filenames():
    assert locks.wheel_pin("typing_extensions-4.16.0-py3-none-any.whl") == \
        ("typing-extensions", "4.16.0")
    assert locks.wheel_pin("/x/grpcio-1.80.0-cp313-cp313-manylinux2014_"
                           "aarch64.manylinux_2_17_aarch64.whl") == \
        ("grpcio", "1.80.0")
    with pytest.raises(ValueError):
        locks.wheel_pin("grpcio-1.80.0.tar.gz")


def test_lock_text_refuses_a_pin_without_a_hash():
    with pytest.raises(ValueError):
        locks.lock_text(["a"], {"a": "1"}, {}, "t")
    with pytest.raises(ValueError):
        locks.lock_text(["b"], {"a": "1"}, {"a": ["0" * 64]}, "t")
    text = locks.lock_text(["b"], {"a": "1", "b": "2"},
                           {"a": ["f" * 64, "0" * 64], "b": ["1" * 64]}, "t")
    assert text.index("b==2") < text.index("a==1")


def test_regen_without_apply_writes_nothing(tmp_path):
    tool = tmp_path / "tool"
    tool.mkdir()
    for name in ("regen.sh", "locks.py"):
        shutil.copy(IDIR / name, tool / name)
    shutil.copytree(IDIR / "proto", tool / "proto")
    oshell = tmp_path / "openshell"
    (oshell / "proto").mkdir(parents=True)
    protos = {
        "gateway_interceptor.proto": 'import "google/protobuf/struct.proto";\n'
                                     'import "extension.proto";\n',
        "extension.proto": 'syntax = "proto3";\n'}
    for f, text in protos.items():
        (oshell / "proto" / f).write_text(text)
    git = ["git", "-C", str(oshell), "-c", "user.name=t", "-c",
           "user.email=t@example.org"]
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"],
                ["tag", "v0.1.2"]):
        subprocess.run([*git, *cmd], check=True, capture_output=True)

    def snapshot():
        return sorted((p.relative_to(tool).as_posix(), p.read_bytes())
                      for p in tool.rglob("*") if p.is_file())
    before = snapshot()
    env = {**os.environ, "PIP_NO_INDEX": "1",
           "PIP_INDEX_URL": "http://127.0.0.1:9/"}
    r = subprocess.run(["bash", str(tool / "regen.sh"), "--openshell",
                        str(oshell), "--tag", "v0.1.2"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[-1] == verbs.DRY_RUN_LINE
    for f in protos:
        assert f in r.stdout
    assert snapshot() == before
    bad = subprocess.run(["bash", str(tool / "regen.sh"), "--bogus"],
                         capture_output=True, text=True, env=env, timeout=60)
    assert bad.returncode == 2


def test_interceptor_files_are_identifier_clean():
    for p in sorted(IDIR.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for bad in ("/home/", "/Users/", "/tmp/"):
            assert bad not in text, (p.relative_to(REPO), bad)
        if p.suffix != ".proto":
            for domain in ("keycloak.example.com", "modelcontextprotocol.io"):
                assert domain not in text, p.relative_to(REPO)


def test_the_interceptor_suite_never_skips():
    text = (IDIR / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pytest.UsageError" in text
    for word in ("skip", "importorskip", "xfail"):
        assert word not in text
