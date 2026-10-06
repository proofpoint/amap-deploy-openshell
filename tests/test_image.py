"""image/Dockerfile, the sandbox image recipe (decision D4).

It is parsed statically; no docker, podman or openshell runs. The one place
the recipe's shell text is executed is the RUN that creates the account, under
/bin/sh with fakes for the user tools, to see what it refuses. The model is
OpenShell main@acbac9c:examples/bring-your-own-container/Dockerfile, and the
identity rules are docs/how-it-works/sandboxes/runtimes.mdx:265-275.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest

import _amap_main
import _harness

DOCKERFILE = _amap_main.REPO / "image" / "Dockerfile"


def instructions(text: str) -> List[Tuple[str, str]]:
    joined: List[str] = []
    buf = ""
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        joined.append(buf + line)
        buf = ""
    if buf:
        joined.append(buf)
    out = []
    for ln in joined:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        keyword, _, rest = ln.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def dockerfile():
    return instructions(DOCKERFILE.read_text(encoding="utf-8"))


def run_texts() -> List[str]:
    return [rest for kw, rest in dockerfile() if kw == "RUN"]


def commands(run: str) -> List[str]:
    return [c.strip() for c in re.split(r"&&|;|\n", run) if c.strip()]


def words(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def all_commands() -> List[str]:
    return [c for run in run_texts() for c in commands(run)]


def useradd_command() -> str:
    hits = [c for c in all_commands() if c.startswith("useradd ")]
    assert len(hits) == 1, hits
    return hits[0]


def _user_run() -> str:
    hits = [r for r in run_texts() if "useradd" in r]
    assert len(hits) == 1
    return hits[0]


def _fakes(fake_bin, log: Path):
    for name in ("userdel", "groupdel", "groupadd", "useradd", "install", "npm"):
        _harness.write_fake(
            fake_bin, name,
            f'#!/bin/sh\nprintf \'%s %s\\n\' "$(basename "$0")" "$*" >> \'{log}\'\n')
    _harness.write_fake(fake_bin, "getent", "#!/bin/sh\nexit 2\n")


def _sh(text, env, fake_bin):
    env = dict(env)
    env["PATH"] = f"{fake_bin}:{os.defpath}"
    return subprocess.run(["/bin/sh", "-c", text], env=env, text=True,
                          capture_output=True, timeout=30)


def test_the_final_user_is_not_root():
    users = [rest for kw, rest in dockerfile() if kw == "USER"]
    assert users
    user = users[-1].split(":")[0]
    assert user not in {"root", "0"}
    cmd = useradd_command()
    assert words(cmd)[-1] == user
    assert '--uid "$SANDBOX_UID"' in cmd


@pytest.mark.parametrize("uid,gid", [
    ("0", "1000"), ("1000", "0"), ("", "1000"), ("abc", "1000"), ("1000", "")])
def test_the_build_refuses_a_root_or_missing_uid(tmp_path, fake_bin, uid, gid):
    log = tmp_path / "calls.log"
    _fakes(fake_bin, log)
    r = _sh(_user_run(), {"SANDBOX_UID": uid, "SANDBOX_GID": gid}, fake_bin)
    assert r.returncode != 0, r.stdout
    assert "SANDBOX_" in r.stderr or "root" in r.stderr
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "useradd" not in calls


def test_the_build_makes_the_one_account_for_a_numeric_uid_and_gid(
        tmp_path, fake_bin):
    log = tmp_path / "calls.log"
    _fakes(fake_bin, log)
    r = _sh(_user_run(), {"SANDBOX_UID": "1000", "SANDBOX_GID": "1000"},
            fake_bin)
    assert r.returncode == 0, r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    added = [c for c in calls if c.startswith("useradd ")]
    assert len(added) == 1
    assert "--uid 1000 --gid 1000" in added[0]
    assert added[0].endswith(" agent")


def test_it_installs_only_python3_bash_and_claude_code():
    cmds = all_commands()
    apt = [words(c) for c in cmds if c.startswith("apt-get install")]
    assert len(apt) == 1
    assert {w for w in apt[0][2:] if not w.startswith("-")} == {"python3", "bash"}
    npm = [words(c) for c in cmds if c.startswith("npm install")]
    assert len(npm) == 1
    pkgs = [w for w in npm[0][2:] if not w.startswith("-")]
    assert pkgs == ["@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"]
    used = {w for c in cmds for w in words(c)}
    banned = {"pip", "pip3", "apk", "yum", "dnf", "curl", "wget", "gem",
              "cargo", "npx"}
    assert not (used & banned)
    assert not [kw for kw, _ in dockerfile() if kw in ("COPY", "ADD")]


def test_nothing_names_a_registry_credential():
    ins = dockerfile()
    text = "\n".join(f"{kw} {rest}" for kw, rest in ins).lower()
    for needle in ("token", "password", "passwd", "secret", "credential",
                   "auth", "api_key", "apikey", "npmrc", "docker login",
                   "type=secret", "--secret"):
        assert needle not in text, needle
    assert not re.search(r"://[^/\s]*@", text)
    for kw, rest in ins:
        if kw in ("ARG", "ENV"):
            name = re.split(r"[=\s]", rest, maxsplit=1)[0]
            assert not re.search(r"(?i)(key|token|secret|pass|auth)", name)
    image = [rest for kw, rest in ins if kw == "FROM"][0].split()[0]
    host, sep, _ = image.partition("/")
    if sep:
        assert "." not in host and ":" not in host


def test_the_claude_code_version_is_required(tmp_path, fake_bin):
    args = [rest for kw, rest in dockerfile() if kw == "ARG"]
    assert "CLAUDE_CODE_VERSION" in args  # no default value
    log = tmp_path / "calls.log"
    _fakes(fake_bin, log)
    run = [r for r in run_texts() if "npm install" in r][0]
    r = _sh(run, {}, fake_bin)
    assert r.returncode != 0
    assert "CLAUDE_CODE_VERSION" in r.stderr
    assert not log.exists()
    ok = _sh(run, {"CLAUDE_CODE_VERSION": "1.2.3"}, fake_bin)
    assert ok.returncode == 0, ok.stderr
    assert "npm install -g @anthropic-ai/claude-code@1.2.3" in log.read_text(
        encoding="utf-8")


def test_the_workspace_is_sandbox_and_there_is_no_cmd():
    ins = dockerfile()
    assert [rest for kw, rest in ins if kw == "WORKDIR"][-1] == "/sandbox"
    assert not [kw for kw, _ in ins if kw in ("CMD", "ENTRYPOINT")]


def test_it_cites_openshell_and_names_no_host():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert ("OpenShell main@acbac9c:examples/bring-your-own-container/"
            "Dockerfile") in text
    assert "/ho" + "me/" not in text
    assert "/Us" + "ers/" not in text
