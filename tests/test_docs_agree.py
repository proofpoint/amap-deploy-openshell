"""The operator's docs, checked against what the repository really does.

Every `amap-openshell.py` line in `README.md`, `docs/TUTORIAL.md` and
`docs/L1-RUNBOOK.md` parses against the real argparse. Every `openshell`,
`openshell-gateway` and `docker` line is one `docs/L1-RUNBOOK.md` already shows
with its OpenShell citation. Every path the docs name is one the tooling writes
or one in this repository. Nothing shipped carries a host path or a personal
identifier. Nothing here runs OpenShell, Docker or the router.
"""

import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import _workspace
import amap_openshell
import l1_kit
import policy
import render
import router_config
import home_record
import siblings
import test_l1_runbook as rb

REPO = Path(__file__).absolute().parents[1]
README = REPO / "README.md"
TUTORIAL = REPO / "docs" / "TUTORIAL.md"
RUNBOOK = REPO / "docs" / "L1-RUNBOOK.md"
DOCS = (README, TUTORIAL)
ALL_DOCS = DOCS + (RUNBOOK,)

# The AMAP repos and OpenShell are the only names shipped text may carry.
# proofpoint.github.io is the GitHub Pages host of this repository's own site
# (docs/index.html), which the README links.
ALLOWED_HOSTS = ("github.com", "proofpoint.github.io")

# The programs whose flags docs/L1-RUNBOOK.md cites at OpenShell main@acbac9c.
CHECKED_PROGRAMS = ("openshell", "openshell-gateway", "docker",
                    "run.sh", "build.sh")

# Lines the operator runbook introduces that the L1 runbook has no use for.
# Each one is a break-it experiment, and its program is one the runbook runs.
NEW_IN_THE_TUTORIAL = ("docker stop amap-router-local",)

# What docs/POC-REPORT.md settled and the tutorial therefore predicts.
REQUIRED_EVIDENCE = ("EROFS", "read_only", "read_write",
                     "queued_for_human", "UNKNOWN", "FAIL")

# The README's status line: verified live, and only under the stated scope.
# A change to what the status claims must change this test too.
STATUS_WORDING = ("**Status: verified live on one host, under the scope below.**",
                  "verified live on one host", "under the scope")

OUTCOME_WORDS = ("FAIL", "UNKNOWN", "Read-only file system", "held",
                 "queued_for_human", "DENIED")


def text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _drop_comment(line: str) -> str:
    return re.sub(r"(^|\s)#\s.*$", "", line).strip()


def commands(md: str) -> List[str]:
    out = []
    for _, body in rb.fenced_blocks(md):
        for line in rb.command_lines(body):
            line = rb.normalise(_drop_comment(line))
            if line:
                out.append(line)
    return out


def program_of(line: str) -> str:
    try:
        first = shlex.split(line)[0]
    except (ValueError, IndexError):
        return ""
    return os.path.basename(first)


def checked_commands(md: str) -> List[str]:
    return [c for c in commands(md) if program_of(c) in CHECKED_PROGRAMS]


def numbered_steps(md: str) -> List[Tuple[int, str]]:
    return [(int(m.group(1)), m.group(2).strip())
            for line in rb.outside_fences(md)
            for m in [re.match(r"## (\d+)\. (.+)\Z", line)] if m]


def section(md: str, heading: str) -> str:
    lines, out, level, on = md.splitlines(), [], None, False
    fence = False
    for line in lines:
        if line.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#+) ", line)
        if on and m and len(m.group(1)) <= level:
            break
        if on:
            out.append(line)
        elif m and line.strip() == heading:
            on, level = True, len(m.group(1))
    return "\n".join(out)


def experiments(md: str) -> List[Tuple[str, str]]:
    body = section(md, "## 11. Break it on purpose")
    parts = re.split(r"^### (.+)$", body, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts), 2)]


def markdown_links(md: str) -> List[str]:
    lines = [l for l in rb.outside_fences(md)]
    return re.findall(r"\]\(([^)\s]+)\)", "\n".join(lines))


# --- AC1: every amap-openshell.py line parses ----------------------------------

def parsed_calls(path):
    calls = rb.amap_openshell_calls(text(path))
    out = []
    for line, rest in calls:
        try:
            out.append(amap_openshell.build_parser().parse_args(rest))
        except SystemExit:
            raise AssertionError(
                f"the parser refuses this line in {Path(path).name}: {line}")
    return out


def test_every_amap_openshell_invocation_in_the_docs_parses():
    for path in ALL_DOCS:
        parsed = parsed_calls(path)
        if path in DOCS:
            assert parsed, f"{path.name} shows no amap-openshell.py call"


def test_an_invented_flag_is_caught():
    calls = rb.amap_openshell_calls(
        "```sh\npython3 amap-openshell.py verify --only alpha\n```")
    assert len(calls) == 1
    try:
        amap_openshell.build_parser().parse_args(calls[0][1])
    except SystemExit:
        return
    raise AssertionError("the parser accepted an invented flag")


def test_the_readme_lists_every_verb():
    md = text(README)
    cells = set(re.findall(r"^\|\s*`([^`|]+)`", md, flags=re.M))
    for name in amap_openshell.VERBS + (amap_openshell.VERIFY,
                                        amap_openshell.SIBLINGS):
        assert any(c == name or c.split()[0] == name for c in cells), \
            f"no verb row for {name}"
    assert amap_openshell.L1_KIT in md and amap_openshell.L1_RUN in md


# --- AC5: every pin the docs quote is siblings.json's ---------------------------

# IMPLEMENTATION-PLAN.md is a dated record that quotes past pins.
# Records of what a past run used: they quote the pins of their day.
PIN_HISTORY = ("IMPLEMENTATION-PLAN.md", "docs/POC-REPORT.md")
# `main@acbac9c`, OpenShell's citation form, is excluded by the `@`.
PIN_TOKEN = re.compile(r"(?<![@\w])[0-9a-f]{7,40}(?!\w)")
SKIPPED_DIRS = (".git", ".claude", "__pycache__")


def pin_problems(md: str, pins: Dict[str, Optional[str]]
                 ) -> Tuple[List[str], List[Tuple[str, str]]]:
    """The problems, and the quotes found as (sibling name, token). A quote is
    a hex token with a letter a-f on a line that names a sibling. It must be a
    prefix of the pin of a sibling that line names, and that sibling must be
    pinned."""
    problems: List[str] = []
    quotes: List[Tuple[str, str]] = []
    for line in md.splitlines():
        named = [n for n in pins if n in line]
        if not named:
            continue
        for tok in PIN_TOKEN.findall(line):
            if not re.search(r"[a-f]", tok):
                continue
            hit = [n for n in named
                   if pins[n] is not None and pins[n].startswith(tok)]
            if hit:
                quotes.append((hit[0], tok))
            else:
                problems.append(f"{tok!r} on a line naming {named} is not "
                                f"the pin of any of them: {line.strip()}")
    return problems, quotes


def scanned_markdown() -> List[Path]:
    out = []
    for p in sorted(REPO.rglob("*.md")):
        rel = p.relative_to(REPO)
        if any(part in SKIPPED_DIRS for part in rel.parts):
            continue
        if str(rel) in PIN_HISTORY:
            continue
        out.append(p)
    return out


def _pins() -> Dict[str, Optional[str]]:
    return {s.name: s.commit for s in
            siblings.load_pins(REPO / siblings.PINS_NAME)}


def test_every_pin_the_docs_quote_is_the_pins_file():
    pins = _pins()
    problems = []
    for p in scanned_markdown():
        found, _ = pin_problems(text(p), pins)
        problems += [f"{p.relative_to(REPO)}: {x}" for x in found]
    assert problems == []
    _, quotes = pin_problems(text(TUTORIAL), pins)
    for name, commit in pins.items():
        if commit is not None:
            assert any(n == name for n, _ in quotes), name
    for name in PIN_HISTORY:
        assert (REPO / name).is_file(), name


def test_the_pin_check_fires_on_a_stale_pin():
    pins = _pins()
    stale = pin_problems("git -C amap-connector-claude checkout d34ccbf", pins)
    assert stale[0]
    assert pin_problems("amap-spec at abc1234", pins)[0]
    # A token that differs from the router's pin in its first character.
    pin = pins["amap-router-local"]
    other = ("b" if pin[0] == "a" else "a") + pin[1:7]
    assert pin_problems(f"amap-router-local `{other}`", pins)[0]
    assert pin_problems("OpenShell main@acbac9c in amap-router-local",
                        pins) == ([], [])
    router = pins["amap-router-local"][:7]
    assert pin_problems(f"amap-router-local `{router}`", pins) == (
        [], [("amap-router-local", router)])


def test_tutorial_step_2_runs_siblings():
    body = section(text(TUTORIAL), "## 2. Get the pieces")
    assert body
    calls = [a for _, rest in rb.amap_openshell_calls(body)
             for a in [amap_openshell.build_parser().parse_args(rest)]]
    assert any(a.command == "siblings" and a.apply is False for a in calls)
    assert any(a.command == "siblings" and a.apply is True for a in calls)
    names = _pins()
    for c in commands(body):
        assert not (program_of(c) == "git" and any(n in c for n in names)), c
    assert "examples/vms/proxmox/README.md" in body and "push" in body


def test_the_tutorial_runs_the_bring_up_and_the_break_it_verbs():
    members = set(policy.named_instances(
        policy.load_fleet(REPO / "examples" / "fleet.json")))
    assert members
    calls = parsed_calls(TUTORIAL)

    def has(command, apply=None, name=None, action=None):
        return any(a.command == command
                   and (apply is None or bool(getattr(a, "apply", False)) == apply)
                   and (name is None or getattr(a, "name", None) == name)
                   and (action is None or getattr(a, "action", None) == action)
                   for a in calls)

    assert has("install", apply=False) and has("install", apply=True)
    for m in members:
        assert has("provision", apply=True, name=m), m
    assert has("verify") and has("list") and has("gateway-config")
    assert has("router-config", apply=False) and has("router-config", apply=True)
    assert has("l1-kit", apply=True, action="profile")


# --- the other programs: one line per runbook line -----------------------------

def test_every_openshell_and_docker_command_is_one_the_runbook_shows():
    mine = set(checked_commands(text(README)) + checked_commands(text(TUTORIAL)))
    assert mine, "no openshell or docker command was found in the docs"
    bad = mine - set(commands(text(RUNBOOK))) - set(NEW_IN_THE_TUTORIAL)
    assert bad == set(), f"lines docs/L1-RUNBOOK.md does not show: {sorted(bad)}"


def test_every_exemption_is_used_and_its_program_is_one_the_runbook_runs():
    used = commands(text(TUTORIAL))
    programs = {program_of(c) for c in commands(text(RUNBOOK))}
    for line in NEW_IN_THE_TUTORIAL:
        assert line in used, f"a stale exemption: {line}"
        assert program_of(line) in programs, line


def test_a_command_the_runbook_does_not_show_is_caught():
    line = "openshell --workspace default sandbox destroy alpha"
    assert line not in commands(text(RUNBOOK))
    assert line not in NEW_IN_THE_TUTORIAL
    assert program_of(line) in CHECKED_PROGRAMS


# --- AC2: paths and identifiers -------------------------------------------------

def test_every_file_path_the_docs_name_is_written_by_the_tooling_or_is_in_this_repo(
        tmp_path):
    written, dirs = rb.written_by_the_kit(tmp_path)
    fleet = policy.load_fleet(REPO / "examples" / "fleet.json")
    h = render.Host("/srv/amap-home", "img:test", render.parse_run_as("1000:1000"),
                    False)
    targets = [m.target for m in render.mount_table(h, "alpha")]
    system = list(render.SYSTEM_READ_ONLY) + list(render.SYSTEM_READ_WRITE)
    sibling_vars = {s.variable: _workspace.FOUND[k].path
                    for k, s in _workspace.SIBLINGS.items()}
    assert policy.named_instances(fleet)
    seen, failures = [], []
    for path in DOCS:
        for tok in rb.candidate_paths(text(path)):
            tok = tok.rstrip(".,:;")
            if tok == rb.XDG_GATEWAY_TOML:
                ok = True
            elif tok == ("${XDG_CONFIG_HOME:-$HOME/.config}/"
                         f"{home_record.DIRNAME}/{home_record.FILENAME}"):
                ok = True   # the home record (D25), written by install
            elif tok.startswith("$"):
                m = re.match(r"\$\{?(\w+)\}?(/.*)?\Z", tok)
                var, rest = (m.group(1), m.group(2) or "") if m else (None, "")
                if var == "AMAP_OPENSHELL_HOME":
                    ok = rb.home_path_ok(rest, written, dirs)
                elif var in sibling_vars:
                    ok = (sibling_vars[var] / rest.lstrip("/")).exists()
                elif var == "XDG_CONFIG_HOME":
                    ok = rest == "/openshell/gateway.toml"
                else:
                    ok = False
            elif tok.startswith("/"):
                ok = any(rb.at_or_under(tok, t) for t in targets + system)
            else:
                ok = (REPO / tok).exists()
            seen.append(tok)
            if not ok:
                failures.append((path.name, tok))
    assert seen, "the scan found no path at all"
    assert failures == [], f"paths nothing writes: {failures}"


def test_the_docs_are_identifier_clean():
    for path in DOCS:
        assert rb.identifier_problems(path, allowed_hosts=ALLOWED_HOSTS) == []


def test_the_identifier_check_fires_on_a_planted_identifier(tmp_path):
    f = tmp_path / "planted.md"
    f.write_text("/home/someone/x\nsomeone@agents.example.org\n"
                 "https://real.example.com/x\nhttps://github.com/a\n",
                 encoding="utf-8")
    problems = {p for _, p in rb.identifier_problems(f, ALLOWED_HOSTS)}
    assert "/home/" in problems
    assert any("@" in p for p in problems)
    assert "real.example.com" in problems
    assert "github.com" not in problems
    assert any(p == "github.com" for _, p in rb.identifier_problems(f))


# --- the status, the totals, the README, the tutorial --------------------------

def test_the_docs_keep_the_status_wording():
    for phrase in STATUS_WORDING:
        assert phrase in text(README), phrase
    assert "verified live on one host" in text(TUTORIAL)


def test_the_docs_state_no_totals():
    for path in DOCS:
        assert not re.search(r"\b\d+ (passed|failed)\b", text(path)), path.name


def test_the_readme_names_every_path_the_tooling_writes():
    md = text(README)
    names = {l1_kit.FLEET_NAME, l1_kit.MEMBERSHIP_NAME, l1_kit.POLICIES_DIRNAME,
             l1_kit.COMMANDS_DIRNAME, l1_kit.PROVIDERS_DIRNAME,
             l1_kit.GATEWAY_FRAGMENT_NAME, render.PAYLOAD_DIRNAME,
             render.ROSTER_DIRNAME, render.INSTANCES_DIRNAME,
             render.SELECTED_JSON_NAME, router_config.ROUTER_JSON_NAME,
             os.path.basename(router_config.default_state_dir("/x"))}
    for n in names | set(render.LANES):
        assert n in md, f"the README does not name {n}"


def test_the_tutorial_has_the_runbooks_shape():
    md = text(TUTORIAL)
    steps = numbered_steps(md)
    assert steps
    assert [n for n, _ in steps] == list(range(1, len(steps) + 1))
    assert steps[-1][1] == "Break it on purpose"
    assert "need" in steps[0][1].lower()
    toc = re.findall(r"^\d+\. \[([^\]]+)\]\(#", md, flags=re.M)
    assert toc == [t for _, t in steps]


def test_break_it_on_purpose_predicts_an_outcome_for_every_experiment():
    md = text(TUTORIAL)
    exps = experiments(md)
    assert exps
    for title, body in exps:
        assert any(w in body for w in OUTCOME_WORDS), title
        assert any(commands(body)), f"{title} shows no command"
    whole = section(md, "## 11. Break it on purpose")
    for word in REQUIRED_EVIDENCE:
        assert word in whole, word


def test_the_tutorial_cites_the_poc_report_and_the_runbook():
    md = text(TUTORIAL)
    assert "docs/POC-REPORT.md" in md and "docs/L1-RUNBOOK.md" in md
    assert "docs/TUTORIAL.md" in text(README)


def test_every_relative_link_in_the_docs_resolves():
    checked = 0
    for path in DOCS:
        base = path.parent
        for target in markdown_links(text(path)):
            if re.match(r"\w+:", target) or target.startswith("#"):
                continue
            file = target.split("#", 1)[0]
            assert (base / file).exists(), f"{path.name}: {target}"
            checked += 1
    assert checked


def test_the_tutorial_needs_the_sibling_variables_only_when_a_checkout_is_not_beside():
    body = section(text(TUTORIAL), "## 1. What you need")
    holding = [t for _, t in rb.fenced_blocks(body)
               if "export AMAP_OPENSHELL_HOME=" in t]
    assert len(holding) == 1
    for variable in ("AMAP_ROUTER_REPO", "AMAP_CONNECTOR_REPO",
                     "AMAP_SANDY_REPO"):
        assert f"export {variable}=" not in holding[0]
    assert ("needed only when a checkout is not beside this one"
            in rb.normalise(body))
