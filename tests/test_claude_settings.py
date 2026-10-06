"""The Claude Code settings file, payload/claude-settings.json, and the flags
that carry it.

The flag names are from `claude --help` on Claude Code 2.1.284:
`--settings <file-or-json>` and `--dangerously-skip-permissions` (decision D8
of IMPLEMENTATION-PLAN.md). The undocumented names in the settings and in the
seeder were observed on that version (decision D9). `crossSessionInbound` and
the hook are from Claude Code's cross-session-messaging page, read 2026-09-29.
The settings' `env` sets CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, which Claude
Code's CHANGELOG documents (2.0.17, 2.1.105 and 2.1.120), all before 2.1.284.
"""

import ast
import json
import os
import posixpath
import re
import shlex
from pathlib import Path

import pytest

import _workspace
import amap_openshell
import l1_kit
import policy
import provider_profile
import render

REPO = Path(__file__).absolute().parents[1]
SETTINGS = REPO / "payload" / render.SETTINGS_NAME
SEED = REPO / "payload" / "claude-seed"
PLAN = (REPO / "IMPLEMENTATION-PLAN.md").read_text(encoding="utf-8")
FLEET_FILE = REPO / "examples" / "fleet.json"
FLEET = policy.load_fleet(FLEET_FILE)
HOST = render.Host("/srv/amap", "amap-openshell-agent:test",
                   render.parse_run_as("1000:1000"), False)


def load():
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def hook_commands():
    return [h["command"] for group in load()["hooks"]["SessionStart"]
            for h in group["hooks"]]


def _sandy_constants():
    tree = ast.parse((_workspace.SANDY_ROOT / "amap_sandy.py").read_text(
        encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant):
            found[node.targets[0].id] = node.value.value
    return found


def _plan_row(prefix):
    rows = [ln for ln in PLAN.splitlines() if ln.startswith(prefix)]
    assert len(rows) == 1, prefix
    return rows[0]


def test_the_env_switches_off_non_essential_traffic():
    """Claude Code CHANGELOG: 2.0.17 ("now disables release notes fetching"),
    2.1.105 (it can be set in one project's settings) and 2.1.120 (it
    suppresses telemetry for API users). The profile allows one endpoint either
    way."""
    env = load()["env"]
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert all(isinstance(v, str) for v in env.values())
    profile = json.loads(provider_profile.TEMPLATE.read_text(encoding="utf-8"))
    assert len(profile["endpoints"]) == 1


def test_the_settings_file_has_exactly_the_listed_keys():
    doc = load()
    assert set(doc) == {"crossSessionInbound", "skipDangerousModePermissionPrompt",
                        "hooks", "env"}
    assert doc["env"] == {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    assert doc["crossSessionInbound"] == "accept"
    assert doc["skipDangerousModePermissionPrompt"] is True
    assert set(doc["hooks"]) == {"SessionStart"}
    groups = doc["hooks"]["SessionStart"]
    assert isinstance(groups, list) and groups
    for group in groups:
        assert set(group) == {"hooks"} and isinstance(group["hooks"], list)
        for hook in group["hooks"]:
            assert set(hook) == {"type", "command"}
            assert hook["type"] == "command"
            assert isinstance(hook["command"], str)
    assert len(hook_commands()) == 1


def test_cross_session_inbound_is_sandys_accept():
    c = _sandy_constants()
    doc = load()
    assert "crossSessionInbound" == c["CROSS_SESSION_KEY"]
    assert doc[c["CROSS_SESSION_KEY"]] == c["CROSS_SESSION_ACCEPT"]


def test_the_hook_command_resolves_under_the_payload_mount_target():
    sources = {s.rel: s for s in l1_kit.payload_sources(
        _workspace.CONNECTOR_ROOT)}
    for cmd in hook_commands():
        assert shlex.split(cmd) == [cmd]
        assert posixpath.normpath(cmd) == cmd
        assert render._at_or_under(cmd, render.PAYLOAD_TARGET)
        rel = cmd[len(render.PAYLOAD_TARGET) + 1:]
        assert rel in sources and sources[rel].executable
        assert sources[rel].path.is_file()
        assert os.access(sources[rel].path, os.X_OK)
    rows = [m for m in render.mount_table(HOST, "alpha")
            if m.role == render.PAYLOAD_DIRNAME]
    assert len(rows) == 1
    assert rows[0].target == render.PAYLOAD_TARGET and rows[0].read_only


def _check_claude_command(argv, what):
    cut = argv.index("--")
    tail = argv[cut + 1:]
    assert tail[0] == f"{render.PAYLOAD_TARGET}/amap-main", what
    assert tail.count(render.SETTINGS_FLAG) == 1, what
    assert tail[tail.index(render.SETTINGS_FLAG) + 1] == \
        f"{render.PAYLOAD_TARGET}/{render.SETTINGS_NAME}", what
    assert tail.count(render.BYPASS_PERMISSIONS_FLAG) == 1, what
    assert "--allow-dangerously-skip-permissions" not in tail, what
    assert "--permission-mode" not in tail, what


def test_the_rendered_claude_command_carries_the_settings_and_the_bypass_flag(
        tmp_path):
    home = tmp_path / "home"
    assert amap_openshell.main([
        "l1-kit", "prepare", "--home", str(home), "--fleet", str(FLEET_FILE),
        "--run-as", "1234:5678", "--image", "amap-openshell-agent:test",
        "--apply"]) == 0
    assert (home / "payload" / render.SETTINGS_NAME).read_bytes() == \
        SETTINGS.read_bytes()
    names = policy.named_instances(FLEET)
    assert names
    for n in names:
        r = render.render_member(FLEET, n, HOST, f"/srv/amap/policies/{n}.yaml")
        _check_claude_command(r.argv, n)
        written = shlex.split(
            (home / "commands" / f"create-{n}.sh").read_text(encoding="utf-8"))
        _check_claude_command(written, n)


def test_the_bypass_flag_is_d8s():
    assert "`--dangerously-skip-permissions`" in _plan_row("| D8 |")
    assert render.BYPASS_PERMISSIONS_FLAG == "--dangerously-skip-permissions"
    assert render.SETTINGS_FLAG == "--settings"
    assert "2.1.284" in (REPO / "render.py").read_text(encoding="utf-8")


def test_the_undocumented_names_are_d9s():
    ticked = re.findall(r"`([^`]+)`", _plan_row("| D9 |"))
    names = set()
    for token in ticked:
        names |= {p for p in re.split(r"\.|\[<[^>]*>\]", token) if p}
    tree = ast.parse(SEED.read_text(encoding="utf-8"))
    keys = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.endswith("_KEY") \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            keys[node.targets[0].id] = node.value.value
    assert set(keys.values()) == {
        "hasCompletedOnboarding", "lastOnboardingVersion", "projects",
        "hasTrustDialogAccepted", "customApiKeyResponses", "approved"}
    assert set(keys.values()) <= names
    assert "skipDangerousModePermissionPrompt" in names
    assert "D9" in SEED.read_text(encoding="utf-8")


def test_design_section_3_names_the_settings_and_the_bypass_flag():
    text = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    start, end = text.index("## 3."), text.index("## 4.")
    section = text[start:end]
    for needle in ("--settings", "claude-settings.json",
                   "--dangerously-skip-permissions"):
        assert needle in section, needle
