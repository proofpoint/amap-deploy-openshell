"""The literal budget, enforced mechanically over the ASTs of verify.py and
router_sections.py.

Ported from amap-deploy-sandy's test of the same name, which walks its own
`router_health.py`. No `3/3`, no `15`, no `17 mount(s)`: each is an expected
value typed in rather than derived, and each goes stale the day the fleet
changes.

Two rules, both scoped to `verify_*` functions (the sections' entry points) and
every helper that builds a Check, which are the only places an expected value
can appear:

  1. NO INT LITERAL other than 0, 1 and 2. Those are exit codes and small
     structural counts, and anything bigger is a fleet fact that must come from
     a Fact.
  2. EVERY STRING LITERAL must be a key of a WIRE_NAMES table (a name another
     program emits or reads), a key of FACT_SOURCES (a handle), argv (which is
     asked, never asserted), or prose (`claim`, `remedy`, `do_not`, `reason`).

Both are mutation-provable in one line. Paste `assert n == 3` into any section
and rule 1 fires (`test_no_int_literal_other_than_exit_codes`); compare a check's
actual value against a bare `"EROFS"` and rule 2 fires
(`test_every_string_literal_is_a_wire_name_a_fact_or_prose`). A rule that cannot
be made to fire is not a rule, and `test_the_rules_fire_on_a_planted_literal`
plants both mutations and proves it.

Rule 2's exemptions are chosen so that no exempt position can carry an expected
value: argv, a Fact's name and provenance (a handle and a sentence; its VALUE,
the middle argument, is not exempt), and the keys of `ctx.fact`/`ctx.value`/
`ctx.of`.
"""

import ast
from pathlib import Path

import router_sections as rs
import verify

_SRC_PATH = Path(verify.__file__)
_TREE = ast.parse(_SRC_PATH.read_text(encoding="utf-8"))
_RS_TREE = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))

ALLOWED_INTS = {0, 1, 2}

# Calls whose entire subtree is argv or a handle, never an expected value. They
# are this module's runner methods: a flag or a subcommand is what we ask.
ARGV_BUILDERS = {"run", "openshell", "docker", "exec_in", "base", "_inspect",
                 "oneshot_name", "runsh_argv"}

# Keyword arguments that carry PROSE, not values.
PROSE_KWARGS = {"claim", "remedy", "do_not", "why", "look_at", "reason",
                "provenance"}

# Calls whose every argument is prose.
PROSE_CALLS = {"_unresolved", "Unresolved", "warn", "note"}

# Calls whose string arguments are selectors: they ask a question.
SELECTOR_CALLS = {"glob", "rglob", "startswith", "endswith"}

# Handles into the fact cache.
HANDLE_CALLS = {"fact", "value", "known", "get", "of"}


def _builds_a_check(fn):
    """A function that calls `check()` or `unknown()` is an assertion site
    whatever it is called: factoring a check into a helper must not move its
    expected value somewhere nothing walks."""
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id in ("check", "unknown")
               for n in ast.walk(fn))


def _verify_functions(tree=_TREE):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("verify_")]


def _assertion_sites(tree=_TREE):
    named = _verify_functions(tree)
    seen = {id(f) for f in named}
    return named + [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and id(n) not in seen
                    and _builds_a_check(n)]


def _ids(node):
    return {id(c) for c in ast.walk(node) if isinstance(c, ast.Constant)}


def _exempt_nodes(fn):
    """Every Constant node id that a rule-2 exemption covers."""
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            fname = node.func.id if isinstance(node.func, ast.Name) else \
                getattr(node.func, "attr", "")
            if fname in ARGV_BUILDERS | PROSE_CALLS | SELECTOR_CALLS:
                out |= _ids(node)
            if fname == "Fact":
                for i, a in enumerate(node.args):
                    if i != 1:
                        out |= _ids(a)
                for kw in node.keywords:
                    if kw.arg in ("name", "provenance"):
                        out |= _ids(kw.value)
            if fname in HANDLE_CALLS:
                out |= {id(c) for c in node.args if isinstance(c, ast.Constant)}
            for kw in node.keywords:
                if kw.arg == "timeout" or kw.arg in PROSE_KWARGS:
                    out |= _ids(kw.value)
        if isinstance(node, ast.Subscript):
            out |= _ids(node.slice)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.add(id(node.value))              # a docstring
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.add(id(v))               # the literal parts of an f-string
                if isinstance(v, ast.FormattedValue) and v.format_spec is not None:
                    out |= _ids(v.format_spec)
    return out


def _int_violations(tree):
    bad = []
    for fn in _assertion_sites(tree):
        exempt = _exempt_nodes(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                    and not isinstance(node.value, bool) \
                    and node.value not in ALLOWED_INTS and id(node) not in exempt:
                bad.append(f"{fn.name}:{node.lineno} -> {node.value!r}")
    return bad


def _known():
    return (set(verify.WIRE_NAMES) | set(rs.WIRE_NAMES)
            | set(verify.FACT_SOURCES))


def _string_violations(tree):
    known, bad = _known(), []
    for fn in _assertion_sites(tree):
        exempt = _exempt_nodes(fn)
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            if id(node) in exempt or node.value in known or node.value == "":
                continue
            bad.append(f"{fn.name}:{node.lineno} -> {node.value!r}")
    return bad


def test_there_are_verify_functions_to_check():
    """A walker that inspects nothing passes trivially. The section table says
    how many entry points there must be: the two router wrappers count."""
    fns = _verify_functions()
    assert fns, "the AST walk found no verify_* functions"
    assert len(fns) == len(verify.SECTIONS)
    assert len(_assertion_sites()) > len(fns), \
        "the helpers that build checks must be walked too"


def test_no_int_literal_other_than_exit_codes():
    """Mutation: add `assert n == 3`, or `actual == 3`, to any check."""
    bad = _int_violations(_TREE)
    assert bad == [], ("int literal in a verify: every count must come from a "
                       "Fact with a provenance.\n  " + "\n  ".join(bad))


def test_every_string_literal_is_a_wire_name_a_fact_or_prose():
    """Mutation: compare a check's actual value against a bare `"EROFS"`."""
    bad = _string_violations(_TREE)
    assert bad == [], ("string literal in a verify that is not a wire name, a "
                       "fact handle, argv or prose. Either it is a name another "
                       "program chose (add it to WIRE_NAMES with the reason) or "
                       "it is an expected value (derive it).\n  "
                       + "\n  ".join(bad))


def test_the_rules_fire_on_a_planted_literal():
    """Each rule is proved to fire: the same walk over a source with a typed-in
    count and a typed-in expected string reports both."""
    planted = ast.parse(
        "def verify_planted(ctx):\n"
        "    yield check(claim='c', expected=ctx.fact('true'),\n"
        "                actual=ctx.value('x') == 17, remedy='r')\n"
        "    yield check(claim='c', expected=ctx.fact('true'),\n"
        "                actual=ctx.value('x') == 'EROFS', remedy='r')\n"
        "def _helper(ctx):\n"
        "    return check(claim='c', expected=ctx.fact('true'), actual=3,\n"
        "                 remedy='r')\n")
    assert len(_int_violations(planted)) == 2          # 17 and 3
    assert [v for v in _string_violations(planted) if "EROFS" in v]


def test_wire_names_all_carry_a_reason():
    for name, why in verify.WIRE_NAMES.items():
        assert len(why) > 12, f"WIRE_NAMES[{name!r}] must say why"


def test_wire_names_holds_no_number():
    for name in verify.WIRE_NAMES:
        assert not name.strip().isdigit(), f"{name!r} is a number"


def test_our_wire_names_do_not_restate_the_router_sections():
    assert set(verify.WIRE_NAMES) & set(rs.WIRE_NAMES) == set()


def test_the_router_sections_have_verify_functions_to_check():
    """The copy is walked as `verify.py` is."""
    assert {f.name for f in _verify_functions(_RS_TREE)} == {
        "verify_container", "verify_health"}
    assert len(_assertion_sites(_RS_TREE)) > 2


def test_no_int_literal_in_the_router_sections():
    bad = _int_violations(_RS_TREE)
    assert bad == [], "\n  ".join(bad)


def test_every_string_literal_in_the_router_sections_is_a_wire_name_a_fact_or_prose():
    bad = _string_violations(_RS_TREE)
    assert bad == [], "\n  ".join(bad)


def test_router_wire_names_carry_a_reason_and_no_number():
    for name, why in rs.WIRE_NAMES.items():
        assert len(why) > 12, f"WIRE_NAMES[{name!r}] must say why"
        assert not name.strip().isdigit(), f"{name!r} is a number"


def test_no_expected_value_is_spelled_as_a_literal_errno():
    """The expected errno comes from the standard library's table, with L1 as
    its provenance: no errno name appears as a string in verify.py."""
    import errno
    text = _SRC_PATH.read_text(encoding="utf-8")
    consts = {n.value for n in ast.walk(_TREE)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not (consts & set(errno.errorcode.values()))
    assert verify.FACT_SOURCES["denied_errno"](None)[0] == \
        errno.errorcode[errno.EROFS]
    assert "EROFS" not in [c for c in consts]
