"""interceptor/deploy.py: the unit, the fragment and the wait for the socket.

Standard library only, so the main suite runs it. Nothing here starts a
server or systemd: a test binds its own unix socket, or renders text.
"""

import contextlib
import os
import re
import shutil
import socket
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

import policy
import router_link
import verbs
from interceptor import deploy, wire

REPO = Path(__file__).absolute().parents[1]
DEPLOY = REPO / "interceptor" / "deploy.py"
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"


@contextlib.contextmanager
def short_home():
    """A real, resolved home whose socket path fits a unix socket."""
    base = Path(tempfile.mkdtemp(prefix="dp-", dir="/tmp")).resolve()
    try:
        yield str(base / "home")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def bound(home, listen):
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(wire.socket_path(home))
    if listen:
        s.listen(4)
    return s


def test_listener_problem_names_an_absent_socket():
    with short_home() as home:
        problem = deploy.listener_problem(home)
        assert wire.socket_path(home) in problem and "is absent" in problem


def test_listener_problem_refuses_a_non_socket():
    with short_home() as home:
        os.makedirs(os.path.join(home, "run"))
        Path(wire.socket_path(home)).write_text("x")
        assert "is not a socket" in deploy.listener_problem(home)


def test_listener_problem_refuses_a_socket_nobody_listens_on():
    with short_home() as home:
        bound(home, listen=False).close()
        assert os.path.exists(wire.socket_path(home))
        assert deploy.listener_problem(home).startswith(
            "nothing accepts connections")


def test_listener_problem_is_empty_when_something_accepts():
    with short_home() as home:
        s = bound(home, listen=True)
        try:
            assert deploy.listener_problem(home) == ""
        finally:
            s.close()


def test_wait_gives_up_at_the_deadline_and_returns_at_once_when_listening():
    with short_home() as home:
        now = [0.0]
        slept = []

        def sleep(n):
            slept.append(n)
            now[0] += n
        problem = deploy.wait_for_listener(
            home, seconds=1.0, interval=0.25, clock=lambda: now[0],
            sleep=sleep)
        assert problem and slept and now[0] >= 1.0
        assert deploy.main(["wait", "--home", home, "--seconds", "0"]) == 1
        s = bound(home, listen=True)
        try:
            slept.clear()
            assert deploy.wait_for_listener(
                home, seconds=1.0, clock=lambda: now[0], sleep=sleep) == ""
            assert slept == []
            assert deploy.main(["wait", "--home", home]) == 0
        finally:
            s.close()


def _sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            cur = out.setdefault(m.group(1), [])
        else:
            key, _, value = line.partition("=")
            cur.append((key, value))
    return out


def _unit():
    repo, home, venv = "/opt/r", "/srv/h", "/srv/v"
    router, sandy = "/opt/router", "/opt/sandy"
    return (deploy.unit_text(repo, home, venv, router, sandy),
            repo, home, venv, router, sandy)


def test_the_unit_orders_the_interceptor_before_the_gateway():
    text, repo, home, venv, router, sandy = _unit()
    sec = _sections(text)
    unit = dict(sec["Unit"])
    assert unit["Before"] == "openshell-gateway.service"
    keys = [k for k, _ in sec["Unit"]]
    for forbidden in ("After", "Requires", "PartOf"):
        assert forbidden not in keys
    wanted = dict(sec["Install"])["WantedBy"].split()
    assert set(wanted) == {"default.target", "openshell-gateway.service"}
    svc = dict(sec["Service"])
    assert svc["ExecStart"] == f"{venv}/bin/python {repo}/interceptor/server.py --home {home}"
    assert svc["ExecStartPost"] == f"{venv}/bin/python {repo}/interceptor/deploy.py wait --home {home}"
    env = [v for k, v in sec["Service"] if k == "Environment"]
    assert env == ["PYTHONDONTWRITEBYTECODE=1", f"AMAP_ROUTER_REPO={router}",
                   f"AMAP_SANDY_REPO={sandy}"]
    assert svc["Restart"] == "on-failure"
    assert text.endswith("\n")


@pytest.mark.parametrize("which", ["repo", "home", "venv"])
@pytest.mark.parametrize("bad", ["relative/x", "/opt/a b", "/opt/%h",
                                 "/opt/$X", '/opt/"q"'])
def test_the_unit_refuses_a_path_systemd_would_misread(which, bad):
    args = {"repo": "/opt/r", "home": "/srv/h", "venv": "/srv/v",
            "router": "/opt/router", "sandy": "/opt/sandy"}
    args[which] = bad
    with pytest.raises(deploy.DeployError):
        deploy.unit_text(**args)


def test_unit_for_pins_the_siblings_this_process_found():
    with short_home() as home:
        text = deploy.unit_for(home, "/srv/v")
    assert f"AMAP_ROUTER_REPO={router_link.find_router()}" in text
    assert f"AMAP_SANDY_REPO={policy.find_sandy()}" in text


def test_the_fragment_command_prints_gateway_configs_fragment(capsys, tmp_path):
    with short_home() as home:
        assert deploy.main(["fragment", "--home", home]) == 0
        printed = capsys.readouterr().out
        assert printed == wire.gateway_fragment(home)
        verbs.run_gateway_config(
            Namespace(home=home, gateway_toml=str(tmp_path / "g.toml")))
        assert printed in capsys.readouterr().out


def test_home_problem_refuses_what_install_and_the_server_refuse(tmp_path):
    with short_home() as home:
        assert deploy.home_problem(home) is None
        real = Path(home).parent / "real"
        real.mkdir()
        link = Path(home).parent / "link"
        link.symlink_to(real)
        problem = deploy.home_problem(str(link))
        assert str(real) in problem and "D19" in problem
        assert "overlaps" in deploy.home_problem(str(REPO / "x"))
        long = str(Path(home).parent / ("d" * 100))
        assert "107" in deploy.home_problem(long)
        assert deploy.home_problem("relative/home")
        assert deploy.home_problem(str(Path(home).parent / "no" / "home"))


def test_the_ci_job_runs_the_grpc_tests_in_the_hash_locked_virtualenv():
    text = WORKFLOW.read_text()
    parts = re.split(r"^  ([a-z-]+):$", text, flags=re.M)
    jobs = dict(zip(parts[1::2], parts[2::2]))
    assert "interceptor" in jobs
    job = jobs["interceptor"]
    pip = ('install --require-hashes --only-binary=:all: '
           '-r interceptor/requirements.txt')
    for needle in ("siblings --apply", "venv", pip, "-m pytest interceptor/tests"):
        assert needle in job, needle
    pip_line = next(ln for ln in job.splitlines() if pip in ln)
    pytest_line = next(ln for ln in job.splitlines()
                       if "-m pytest interceptor/tests" in ln)
    assert "$RUNNER_TEMP/ivenv/bin/pip" in pip_line
    assert "$RUNNER_TEMP/ivenv/bin/" in pytest_line
    assert '"3.9"' in job
    assert "pytest tests" in jobs["suite"]


def test_deploy_text_is_identifier_clean():
    text = DEPLOY.read_text()
    for needle in ("/ho" + "me/", "/Us" + "ers/"):
        assert needle not in text, needle
    assert not re.search(r"\b[a-z0-9-]+\.(com|net|org|io|dev)\b", text)
