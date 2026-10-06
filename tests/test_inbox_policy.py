"""`payload/INBOX-POLICY.md` is amap-deploy-sandy's text with exactly four
operator-approved substitutions (three in use), and nothing else.

The substitutions, compared as whitespace-normalised text:

1. the roster path: sandy's `$AMAP_ROSTER_DIR/roster.json` is
   `/opt/amap/roster/roster.json` here;
2. the address noun: `<sandbox slug>` is `<sandbox name>`;
3. *Retired.* `find a workspace by` -> `find an agent by`. amap-deploy-sandy's
   #18 replaced that sentence with one about `inbox-submit`'s `peers`, so its
   source text is gone. If the source text comes back, the test fails.
4. identity: sandy's `/etc/sandy-session.json` is `$AMAP_SELF_ADDRESS`, the
   address the operator gave the sandbox when creating it. Once that file is
   gone, "the file" in the same sentence has no antecedent, so "If the file is
   missing" is "If the roster file is missing". The span below covers both.

The comparison is by paragraph. A paragraph in which no substitution applies
must be byte-identical to sandy's, so a change to sandy's text, or a fifth edit
here, fails instead of drifting. Two more facts are checked against the
rendering: every `$AMAP_*` name the text uses is a name a rendered `--env`
sets, and every `/opt/amap/...` path it names is at or under a read-only mount
target (the text tells the agent where its read-only tools and roster are).
"""

import posixpath
import re
from pathlib import Path

import _workspace
import policy
import render

REPO = Path(__file__).absolute().parents[1]
OURS = REPO / "payload" / "INBOX-POLICY.md"

SUBSTITUTIONS = (
    ("`$AMAP_ROSTER_DIR/roster.json`", "`/opt/amap/roster/roster.json`"),
    ("`<sandbox slug>@<fleet domain>`", "`<sandbox name>@<fleet domain>`"),
    ("which comes from your sandbox's session (`/etc/sandy-session.json`). "
     "If the file is missing",
     "which is the address the operator gave this sandbox when creating it "
     "(`$AMAP_SELF_ADDRESS`). If the roster file is missing"),
)

# Retired substitutions' source text (amap-deploy-sandy #18 removed it). It must
# not reappear in sandy's text.
RETIRED = ("find a workspace by",)


def normalise(text):
    return " ".join(text.split())


def paragraphs(text):
    return re.split(r"\n[ \t]*\n", text)


def policy_text_problems(sandy, ours):
    """One message per way `ours` is not `sandy` with the approved substitutions."""
    problems = []
    whole_sandy, whole_ours = normalise(sandy), normalise(ours)
    for old, new in SUBSTITUTIONS:
        if whole_sandy.count(old) != 1:
            problems.append(f"{old!r} occurs {whole_sandy.count(old)} times in "
                            f"sandy's text, not once")
        if whole_ours.count(new) != 1:
            problems.append(f"{new!r} occurs {whole_ours.count(new)} times in "
                            f"this repository's text, not once")
    for old in RETIRED:
        if old in whole_sandy:
            problems.append(f"{old!r}, a retired substitution's source, is back "
                            f"in sandy's text")
    theirs, mine = paragraphs(sandy), paragraphs(ours)
    if len(theirs) != len(mine):
        problems.append(f"sandy's text has {len(theirs)} paragraphs and this "
                        f"one has {len(mine)}")
        return problems
    for i, (a, b) in enumerate(zip(theirs, mine)):
        expected = normalise(a)
        applied = [(o, n) for o, n in SUBSTITUTIONS if o in expected]
        if applied:
            for old, new in applied:
                expected = expected.replace(old, new)
            if normalise(b) != expected:
                problems.append(f"paragraph {i} is not sandy's paragraph with "
                                f"only the approved substitutions")
        elif a != b:
            problems.append(f"paragraph {i} differs from sandy's, and no "
                            f"substitution applies to it")
    return problems


def sandy_text():
    return (_workspace.SANDY_ROOT / "payload" /
            "INBOX-POLICY.md").read_text(encoding="utf-8")


def our_text():
    return OURS.read_text(encoding="utf-8")


def test_ours_is_sandys_with_exactly_the_approved_substitutions():
    assert policy_text_problems(sandy_text(), our_text()) == []


def test_a_change_to_sandys_text_fails_instead_of_drifting():
    sandy, ours = sandy_text(), our_text()
    assert policy_text_problems(sandy, ours) == []

    # A word changed in a paragraph no substitution touches.
    other = sandy.replace("arrives", "comes", 1)
    assert other != sandy
    assert policy_text_problems(other, ours)

    # A word changed inside the "Who is who" paragraph, outside the
    # substitutions.
    inside = sandy.replace("The router rewrites it", "The router rewrites this", 1)
    assert inside != sandy
    assert policy_text_problems(inside, ours)

    # A fifth edit in ours.
    fifth = ours.replace("shape as your own", "shape as yours", 1)
    assert fifth != ours
    assert policy_text_problems(sandy, fifth)

    # An edit in ours to a paragraph with no substitution.
    sixth = ours.replace("arrives", "comes", 1)
    assert sixth != ours
    assert policy_text_problems(sandy, sixth)

    # A substitution undone in ours.
    undone = ours.replace("<sandbox name>", "<sandbox slug>", 1)
    assert undone != ours
    assert policy_text_problems(sandy, undone)

    # A retired substitution's source text comes back in sandy's text.
    retired_back = sandy.replace("and match the start",
                                 "and find a workspace by the start", 1)
    assert retired_back != sandy
    assert policy_text_problems(retired_back, ours)

    # An edit inside the substituted "Who is who" paragraph.
    peers_edit = sandy.replace("never pick", "never guess", 1)
    assert peers_edit != sandy
    assert policy_text_problems(peers_edit, ours)

    # An edit to the reply-id paragraph, which no substitution touches.
    reply_edit = ours.replace("Never guess it.", "Never guess.", 1)
    assert reply_edit != ours
    assert policy_text_problems(sandy, reply_edit)


def rendered_argvs():
    fleet = policy.load_fleet(REPO / "examples" / "fleet.json")
    h = render.Host("/srv/amap-home", "image:test", render.parse_run_as("1000:1000"),
                    False)
    return [render.render_member(fleet, n, h, f"/srv/amap-home/policies/{n}.yaml")
            for n in policy.named_instances(fleet)]


def test_every_amap_variable_the_text_uses_is_a_rendered_env_name():
    used = set(re.findall(r"\$\{?(AMAP_[A-Z0-9_]+)", our_text()))
    assert used
    rendered = rendered_argvs()
    assert rendered
    for r in rendered:
        names = {r.argv[i + 1].split("=", 1)[0]
                 for i, w in enumerate(r.argv) if w == "--env"}
        assert used <= names, sorted(used - names)


def test_every_opt_amap_path_the_text_names_is_under_a_read_only_mount_target():
    import json
    paths = re.findall(r"/opt/amap(?:/[\w.-]+)*", our_text())
    assert paths
    for r in rendered_argvs():
        argv = r.argv
        config = json.loads(argv[argv.index("--driver-config-json") + 1])
        targets = [m["target"] for m in config["docker"]["mounts"]
                   if m["read_only"] is True]
        assert targets
        for p in paths:
            assert any(p == t or p.startswith(t + "/") for t in targets), \
                (p, targets)
            assert posixpath.normpath(p) == p
