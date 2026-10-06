"""`docs/L1-RUNBOOK.md`: the L1 procedure as exact commands.

The runbook is checked against what the repository really does, so it cannot
drift from it: every `amap-openshell.py` line parses against the real argparse,
every file path it names is one the kit or the runner writes (from a real run of
the kit into a temporary home, and the runner's evidence layout) or one in this
repository, it names every PLAN.md phase-1 pass criterion
and every L1 unknown in IMPLEMENTATION-PLAN.md, and its steps run in order.

What is a path. The scan covers the text of fenced blocks and inline code, minus
blockquote lines (which quote another document), with URLs blanked. Host paths
are `$AMAP_OPENSHELL_HOME/...`, `$<sibling variable>/...`,
`$XDG_CONFIG_HOME/openshell/gateway.toml` (OpenShell's own file, OpenShell
main@acbac9c:docs/how-it-works/gateways/configuration.mdx:11, also spelled with
its usual fallback to .config in the home directory), or a path
relative to this repository. Absolute paths are in-sandbox paths, and must be at
or under a rendered mount target or one of the policy's system paths. A
placeholder is `<file>`. Nothing runs OpenShell, Docker or the router.
"""

import fnmatch
import os
import re
import shlex
from pathlib import Path

import _workspace
import amap_openshell
import l1_kit
import l1_run
import policy
import render

REPO = Path(__file__).absolute().parents[1]
RUNBOOK = REPO / "docs" / "L1-RUNBOOK.md"
FLEET_FILE = REPO / "examples" / "fleet.json"



# The gateway's own file, with the fallback Ubuntu needs when XDG_CONFIG_HOME
# is unset (configuration.mdx:11 and 32).
XDG_GATEWAY_TOML = "${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateway.toml"

def runbook():
    return RUNBOOK.read_text(encoding="utf-8")


# --- parsing ------------------------------------------------------------------

def fenced_blocks(md):
    """`(lang, text)` for every fenced block."""
    blocks, lang, body = [], None, []
    for line in md.splitlines():
        if lang is None:
            m = re.match(r"```(\S*)\s*\Z", line)
            if m:
                lang, body = m.group(1), []
        elif line.strip() == "```":
            blocks.append((lang, "\n".join(body)))
            lang = None
        else:
            body.append(line)
    assert lang is None, "an unclosed fence"
    return blocks


def outside_fences(md):
    out, inside = [], False
    for line in md.splitlines():
        if re.match(r"```", line):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return out


def command_lines(text):
    """Joins `\\` continuations and drops comments and blank lines."""
    lines, cur = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        line = (cur + line).strip()
        cur = ""
        if line and not line.startswith("#"):
            lines.append(line)
    if cur.strip():
        lines.append(cur.strip())
    return lines


def inline_code(md):
    spans = []
    for line in outside_fences(md):
        if line.lstrip().startswith(">"):
            continue
        spans.extend(re.findall(r"`([^`\n]+)`", line))
    return spans


def normalise(text):
    return " ".join(text.split())


def headings(md, pattern):
    return [(i, line) for i, line in enumerate(outside_fences(md))
            if re.match(pattern, line)]


def blockquote_after(md, heading_regex):
    """The first blockquote under the heading, whitespace-normalised."""
    lines = outside_fences(md)
    start = next(i for i, l in enumerate(lines) if re.match(heading_regex, l))
    quote = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if line.startswith(">"):
            quote.append(line[1:].strip())
        elif quote:
            break
    assert quote, f"no blockquote under {heading_regex}"
    return normalise(" ".join(quote))


def bullets_after(md, marker, indent):
    """The bullets, at `indent` spaces, after the line that holds `marker`.
    Continuation lines are indented deeper. The list ends at the first other
    line."""
    lines = md.splitlines()
    start = next(i for i, l in enumerate(lines) if marker in l)
    bullets, pad = [], " " * indent
    for line in lines[start + 1:]:
        if line.startswith(pad + "- "):
            bullets.append([line[indent + 2:]])
        elif not line.strip():
            continue
        elif bullets and line.startswith(pad + " "):
            bullets[-1].append(line.strip())
        else:
            break
    return [normalise(" ".join(b)) for b in bullets]


# --- AC4: commands and paths --------------------------------------------------

def amap_openshell_calls(md):
    calls = []
    for _, text in fenced_blocks(md):
        for line in command_lines(text):
            if "amap-openshell.py" not in line:
                continue
            tokens = shlex.split(line)
            at = next(i for i, t in enumerate(tokens)
                      if os.path.basename(t) == "amap-openshell.py")
            calls.append((line, tokens[at + 1:]))
    return calls


def test_every_amap_openshell_invocation_in_the_runbook_parses():
    calls = amap_openshell_calls(runbook())
    assert calls
    parsed = []
    for line, rest in calls:
        try:
            parsed.append(amap_openshell.build_parser().parse_args(rest))
        except SystemExit:
            raise AssertionError(f"the parser refuses this runbook line: {line}")
    kit = [a for a in parsed if a.command == "l1-kit"]
    assert any(a.action == "prepare" and a.apply for a in kit)
    members = set(policy.named_instances(policy.load_fleet(FLEET_FILE)))
    assert members
    recorded = {a.name for a in kit if a.action == "record" and a.apply}
    assert recorded == members


def written_by_the_kit(tmp_path):
    home = str(tmp_path / "home")
    args = ["prepare", "--home", home, "--fleet", str(FLEET_FILE),
            "--run-as", "1234:5678", "--image", "img:test", "--apply"]
    assert amap_openshell.main(["l1-kit", *args]) == 0
    for i, name in enumerate(sorted(
            policy.named_instances(policy.load_fleet(FLEET_FILE)))):
        assert amap_openshell.main(
            ["l1-kit", "record", "--home", home, name, f"id-{i}-{name}",
             "--apply"]) == 0
    assert amap_openshell.main(
        ["l1-kit", "profile", "--home", home, "--binary", "/usr/local/bin/node",
         "--binary", "/usr/local/bin/claude", "--apply"]) == 0
    files, dirs = set(), set()
    for d, subdirs, names in os.walk(home):
        for n in subdirs:
            dirs.add(os.path.relpath(os.path.join(d, n), home))
        for n in names:
            files.add(os.path.relpath(os.path.join(d, n), home))
    # What the runner writes beside the kit's files: the evidence directory.
    files |= set(l1_run.evidence_files())
    dirs |= set(l1_run.evidence_dirs())
    return files | dirs, dirs


URL = re.compile(r"\w+://\S+")
SEPARATORS = re.compile(r"[\s\"'`;|&()=,]+")


def scanned_text(md):
    fenced = "\n".join(t for _, t in fenced_blocks(md))
    fenced = "\n".join(l for l in fenced.splitlines()
                       if not l.lstrip().startswith(">"))
    return URL.sub(" ", fenced + "\n" + "\n".join(inline_code(md)))


def candidate_paths(md):
    out = []
    for line in scanned_text(md).splitlines():
        for tok in SEPARATORS.split(line):
            if "/" in tok:
                out.append(tok)
    return out


def at_or_under(path, parent):
    p, q = path.strip("/").split("/"), parent.strip("/").split("/")
    return p[:len(q)] == q


def home_path_ok(rest, written, dirs):
    comps = [c for c in rest.split("/") if c]
    if not comps:
        return True
    for w in written:
        wc = w.split("/")
        if len(wc) == len(comps) and all(
                fnmatch.fnmatchcase(a, re.sub(r"<[^>]*>", "*", b))
                for a, b in zip(wc, comps)):
            return True
    fixed = []
    for c in comps:
        if "<" in c:
            break
        fixed.append(c)
    rest_comps = comps[len(fixed):]
    return bool(rest_comps) and bool(fixed) and all("<" in c for c in rest_comps) \
        and "/".join(fixed) in dirs


def test_every_file_path_the_runbook_names_is_written_by_the_kit_or_in_this_repo(
        tmp_path):
    written, dirs = written_by_the_kit(tmp_path)
    fleet = policy.load_fleet(FLEET_FILE)
    h = render.Host("/srv/amap-home", "img:test", render.parse_run_as("1000:1000"),
                    False)
    targets = [m.target for m in render.mount_table(h, "alpha")]
    system = list(render.SYSTEM_READ_ONLY) + list(render.SYSTEM_READ_WRITE)
    sibling_vars = {s.variable: _workspace.FOUND[k].path
                    for k, s in _workspace.SIBLINGS.items()}
    assert policy.named_instances(fleet)

    classified, failures = [], []
    for tok in candidate_paths(runbook()):
        tok = tok.rstrip(".,:;")
        if tok == XDG_GATEWAY_TOML:
            ok = True
        elif tok.startswith("$"):
            m = re.match(r"\$\{?(\w+)\}?(/.*)?\Z", tok)
            var, rest = (m.group(1), m.group(2) or "") if m else (None, "")
            if var == "AMAP_OPENSHELL_HOME":
                ok = home_path_ok(rest, written, dirs)
            elif var in sibling_vars:
                ok = (sibling_vars[var] / rest.lstrip("/")).exists()
            elif var == "XDG_CONFIG_HOME":
                ok = rest == "/openshell/gateway.toml"
            else:
                ok = False
        elif tok.startswith("/"):
            ok = any(at_or_under(tok, t) for t in targets + system)
        else:
            ok = (REPO / tok).exists() or tok == "docs/POC-REPORT.md"
        classified.append(tok)
        if not ok:
            failures.append(tok)
    assert classified, "the scan found no path at all"
    assert failures == [], f"paths the runbook names that nothing writes: {failures}"
    # The scan is live: it does refuse a path nothing writes.
    assert not home_path_ok("/instances/alpha/nothing", written, dirs)
    assert not home_path_ok("/nothing", written, dirs)
    assert home_path_ok("/instances/<name>/peer", written, dirs)
    assert home_path_ok("/roster/<file>", written, dirs)


# --- AC5: the criteria and the unknowns ---------------------------------------

def test_the_runbook_names_every_phase_1_pass_criterion():
    plan = (REPO / "PLAN.md").read_text(encoding="utf-8")
    phase = plan.split("## Phase 1", 1)[1].split("## Phase 2", 1)[0]
    want = bullets_after(phase, "**Pass criteria:**", 0)
    assert want
    md = runbook()
    found = headings(md, r"#### Pass criterion \d+")
    assert [int(re.search(r"\d+", l).group()) for _, l in found] == \
        list(range(1, len(want) + 1))
    have = [blockquote_after(md, re.escape(l)) for _, l in found]
    assert have == want


def test_the_runbook_names_every_l1_unknown():
    plan = (REPO / "IMPLEMENTATION-PLAN.md").read_text(encoding="utf-8")
    section = plan.split("### L1:", 1)[1].split("\n### ", 1)[0]
    want = bullets_after(
        section, "- **Settle these unknowns, and record the evidence for each:**",
        2)
    assert want
    md = runbook()
    found = headings(md, r"#### Unknown \d+")
    assert [int(re.search(r"\d+", l).group()) for _, l in found] == \
        list(range(1, len(want) + 1))
    have = [blockquote_after(md, re.escape(l)) for _, l in found]
    assert have == want


def test_the_helpers_read_what_they_are_given():
    md = ("intro\n\n- a b\n  c\n- d\n\nafter\n\n### H\n\n> one\n> two\n\n> three\n"
          "```sh\nx\n```\n`a` and `b`\n> `c`\n")
    assert bullets_after(md, "intro", 0) == ["a b c", "d"]
    assert blockquote_after(md, "### H") == "one two"
    assert fenced_blocks(md) == [("sh", "x")]
    assert inline_code(md) == ["a", "b"]
    assert [normalise(x) for x in command_lines("a \\\n b\n# c\n\nd")] == \
        ["a b", "d"]


# --- order and content --------------------------------------------------------

def test_the_runbook_runs_the_steps_in_order():
    md = runbook()
    at = [md.index(f"\n## {n}. ") for n in range(1, 10)]
    assert at == sorted(at)
    assert "\n## 0. " in md and md.index("\n## 0. ") < at[0]
    markers = ["docker build", "gateway-fragment.toml", "l1-kit prepare",
               "commands/create-", "sandbox get", "l1-kit record",
               "docker/run.sh", "Pass criterion 1", "```markdown"]
    first = [md.index(m) for m in markers]
    assert first == sorted(first) and len(set(first)) == len(first), \
        dict(zip(markers, first))
    top = md[:md.index("\n## 0. ")]
    assert "unsupported until verified" in top


def test_step_0_names_the_variables_and_the_posture():
    md = runbook()
    step0 = md[md.index("\n## 0. "):md.index("\n## 1. ")]
    for variable in ("AMAP_OPENSHELL_HOME", "AMAP_ROUTER_REPO",
                     "AMAP_CONNECTOR_REPO", "AMAP_SPEC_DIR",
                     "CLAUDE_CODE_VERSION"):
        assert f"export {variable}=" in step0, variable
    assert "D6" in step0 and "G3" in step0
    assert "repository's root" in step0


def test_the_image_build_uses_the_dockerfiles_args_and_the_rendered_identity():
    md = runbook()
    build = None
    for _, text in fenced_blocks(md):
        for line in command_lines(text):
            if line.startswith("docker build"):
                build = shlex.split(line)
                break
    assert build, "no docker build line"
    args = {}
    for i, t in enumerate(build):
        if t == "--build-arg":
            k, v = build[i + 1].split("=", 1)
            args[k] = v
    dockerfile = (REPO / "image" / "Dockerfile").read_text(encoding="utf-8")
    assert set(args) == set(re.findall(r"^ARG (\w+)", dockerfile, re.M))
    tag = build[build.index("-t") + 1]
    prepare = [a for line, rest in amap_openshell_calls(md)
               for a in [amap_openshell.build_parser().parse_args(rest)]
               if a.command == "l1-kit" and a.action == "prepare"]
    assert prepare
    for a in prepare:
        assert a.image == tag
        assert a.run_as == f"{args['SANDBOX_UID']}:{args['SANDBOX_GID']}"


def test_the_runbook_includes_the_poc_report_template():
    md = runbook()
    blocks = [t for lang, t in fenced_blocks(md) if lang == "markdown"]
    assert len(blocks) == 1
    template = blocks[0]
    assert "D6" in template
    assert "accepted posture" in template.lower()
    header = [l for l in template.splitlines()
              if l.startswith("|") and re.search(r"NOT[- ]CHECKED", l)]
    assert header
    for column in ("enforced by OpenShell", "still operational",
                   "not applicable", "evidence"):
        assert column in header[0]
    for word in ("PASS", "FAIL", "UNKNOWN"):
        assert word in template
    for n in range(1, 6):
        assert f"Pass criterion {n}" in template
    for n in range(1, 8):
        assert f"Unknown {n}" in template


def test_the_runbook_states_no_totals():
    for text in (runbook(), (REPO / "l1_kit.py").read_text(encoding="utf-8")):
        assert not re.search(r"\b\d+ (passed|failed)\b", text)


# --- the scripted run (S5d) ---------------------------------------------------

def test_the_scripted_run_section():
    md = runbook()
    assert "\n## Scripted run" in md
    assert md.index("\n## Scripted run") < md.index("\n## 0. ")
    section = md[md.index("\n## Scripted run"):md.index("\n## 0. ")]
    fenced = [line for _, text in fenced_blocks(section)
              for line in command_lines(text)]
    for needle in ("l1-run", "l1-run --apply", "--from", "--only"):
        assert any(needle in line for line in fenced), needle
    for line in fenced:
        tokens = shlex.split(line)
        at = next(i for i, t in enumerate(tokens)
                  if os.path.basename(t) == "amap-openshell.py")
        args = amap_openshell.build_parser().parse_args(tokens[at + 1:])
        assert args.command == "l1-run", line
    # The markers of the manual steps must first appear in those steps.
    for marker in ("docker build", "gateway-fragment.toml", "l1-kit prepare",
                   "commands/create-", "sandbox get", "l1-kit record",
                   "docker/run.sh", "Pass criterion 1", "```markdown"):
        assert marker not in section, marker
    assert render.PROVIDER in section
    assert "evidence/step-" in section
    assert "POC-REPORT.md" in section
    assert "docs/POC-REPORT.md" in section


# --- rule 3 -------------------------------------------------------------------

def identifier_problems(path, allowed_hosts=()):
    """Rule 3 for one shipped file: no host path, no address, no real domain.
    A host in `allowed_hosts` is not reported."""
    text = Path(path).read_text(encoding="utf-8")
    name = Path(path).name
    problems = []
    if "/home/" in text:
        problems.append((name, "/home/"))
    if "/Users/" in text:
        problems.append((name, "/Users/"))
    if 'expanduser("~")' in text:
        problems.append((name, "expanduser"))
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        problems.append((name, email))
    for host in re.findall(r"[\w.-]+\.(?:com|net|io|org|ai|dev)\b", text):
        if host in allowed_hosts:
            continue
        if not (host == "example.org" or host.endswith(".example.org")):
            problems.append((name, host))
    if "~/" in text:
        problems.append((name, "~/"))
    return problems


def test_the_new_shipped_files_are_identifier_clean():
    for path in (RUNBOOK, REPO / "l1_kit.py"):
        assert identifier_problems(path) == []


# --- unattended sessions (S5c) --------------------------------------------------

def _between(md, start, end):
    a = md.index(start)
    return md[a:md.index(end, a)]


def test_step_4_creates_each_sandbox_unattended():
    """The flags are OpenShell main@acbac9c:
    crates/openshell-cli/src/main.rs:1523-1555."""
    step = _between(runbook(), "\n## 4. ", "\n## 5. ")
    for flag in render.UNATTENDED_FLAGS:
        assert flag in step, flag
    assert "D8" in step and "D9" in step
    assert render.PROVIDER in step
    assert "`claude-code` provider" not in step


def test_step_3_imports_this_deployments_profile():
    """The commands are OpenShell main@acbac9c and v0.1.2:
    crates/openshell-cli/src/main.rs:1108-1161; an update needs a resource
    version (crates/openshell-server/src/grpc/provider.rs:2894-2908)."""
    step = _between(runbook(), "\n## 3. ", "\n## 4. ")
    for needle in (render.PROVIDER, "readlink -f", "l1-kit profile",
                   "provider profile lint", "list-profiles",
                   "provider profile import", "provider profile update",
                   "resource_version", "v0.1.2"):
        assert needle in step, needle
    assert "--global" not in step


def test_unknown_5_records_what_settings_pins():
    section = _between(runbook(), "#### Unknown 5", "#### Unknown 6")
    for needle in (render.SETTINGS_FLAG, render.SETTINGS_NAME, "accept",
                   "What L1 must still confirm"):
        assert needle in section, needle


def test_l1_run_needs_the_sibling_variables_only_when_a_checkout_is_not_beside():
    md = runbook()
    step0 = md[md.index("\n## 0. "):md.index("\n## 1. ")]
    holding = [text for _, text in fenced_blocks(step0)
               if "export AMAP_OPENSHELL_HOME=" in text]
    assert len(holding) == 1
    assert "export CLAUDE_CODE_VERSION=" in holding[0]
    for variable in ("AMAP_ROUTER_REPO", "AMAP_CONNECTOR_REPO",
                     "AMAP_SPEC_DIR"):
        assert f"export {variable}=" not in holding[0]
    needed = "needed only when a checkout is not beside this one"
    assert needed in normalise(step0)
    scripted = md[md.index("\n## Scripted run"):md.index("\n## 0. ")]
    assert "set the variables of step 0" not in normalise(scripted)
    assert needed in normalise(scripted)
