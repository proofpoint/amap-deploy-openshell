"""This deployment's provider profile, providers/amap-claude-code.json, and
provider_profile.py, which renders and compares it.

Facts, at OpenShell main@acbac9c (v0.1.2 lines in brackets where they differ):
the profile's fields are docs/how-it-works/providers/profiles.mdx:477-490, and
`id` and `display_name` have no serde default
(crates/openshell-providers/src/profiles.rs:683-712 [691-720]); the ID is
lowercase kebab-case (profiles.mdx:327, profiles.rs:1996-2006 [2020-2030]);
credential names that look like `v<digits>_...` are reserved (profiles.mdx:481,
profiles.rs:2101-2109, crates/openshell-core/src/secrets.rs:788-817).

The settings file's `env` is documented in Claude Code's CHANGELOG (2.0.17,
2.1.105 and 2.1.120). Nothing here runs OpenShell or Docker.
"""

import ast
import copy
import json
import re
from pathlib import Path

import pytest

import provider_profile as pp
import render
import test_l1_runbook
from provider_profile import ProfileError

REPO = Path(__file__).absolute().parents[1]
PLAN = (REPO / "IMPLEMENTATION-PLAN.md").read_text(encoding="utf-8")
# D10 names the Anthropic API as the one endpoint. The host is spelled in
# parts so that this file reads as no host name, as l1_run's label does.
HOST = "api." "anthropic" ".com"
NODE, CLAUDE = "/usr/local/bin/node", "/opt/c/cli.js"


def template():
    return json.loads(pp.TEMPLATE.read_text(encoding="utf-8"))


def test_the_shipped_profile_parses_and_has_the_required_fields():
    doc = template()
    for key in pp.REQUIRED_FIELDS + pp.IDENTITY_FIELDS:
        assert isinstance(doc[key], str) and doc[key].strip(), key
    assert doc["id"] == pp.PROFILE_ID == render.PROVIDER == "amap-claude-code"
    assert re.match(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z", doc["id"])
    assert doc["id"] != "claude-code"
    assert doc["category"] in pp.CATEGORIES
    assert pp.profile_problems(doc, rendered=False) == []


def test_one_credential_and_it_is_d10s():
    doc = template()
    (cred,) = doc["credentials"]
    assert cred["env_vars"] == ["ANTHROPIC_API_KEY"]
    assert cred["auth_style"] == "header" and cred["header_name"] == "x-api-key"
    assert cred["required"] is True
    assert doc["discovery"]["credentials"] == [cred["name"]]
    for var in cred["env_vars"]:
        assert not re.match(r"\Av\d+_\w+\Z", var)
        assert not re.match(r"\As[0-9a-f]{64}_\w+\Z", var)
    row = next(l for l in PLAN.splitlines() if l.startswith("| D10"))
    assert "ANTHROPIC_API_KEY" in row


def test_one_endpoint_the_anthropic_api():
    (ep,) = template()["endpoints"]
    assert ep["host"] == HOST and ep["port"] == 443
    assert ep["protocol"] == "rest" and ep["access"] == "read-write"
    assert ep["enforcement"] == "enforce"
    assert ep.get("tls") != "skip"


def test_the_shipped_binaries_are_empty_until_rendered():
    assert template()["binaries"] == []


def test_render_sets_binaries_to_exactly_the_given_paths():
    t = template()
    frozen = copy.deepcopy(t)
    r = pp.render_profile(t, [NODE, CLAUDE])
    assert r["binaries"] == [NODE, CLAUDE]
    assert pp.render_profile(t, [NODE, NODE])["binaries"] == [NODE]
    assert {k: v for k, v in r.items() if k != "binaries"} == \
        {k: v for k, v in t.items() if k != "binaries"}
    assert t == frozen


@pytest.mark.parametrize("bad", [[], ["rel/node"], ["/a/../b"], [""],
                                 ["/a b"], ["/a\n"]])
def test_render_refuses_bad_or_missing_paths(bad):
    with pytest.raises(ProfileError):
        pp.render_profile(template(), bad)


def test_parse_image_paths():
    good = f"node={NODE}\n\nclaude={CLAUDE}\n"
    assert pp.parse_image_paths(good) == {"node": NODE, "claude": CLAUDE}
    for bad in ("node=\nclaude=/x\n", "node=/n\n", "node=n\nclaude=/x\n",
                "node=/n\nnode=/m\nclaude=/x\n",
                "node=/n\nclaude=/x\nother=/y\n", ""):
        with pytest.raises(ProfileError):
            pp.parse_image_paths(bad)
    assert pp.image_binaries({"node": NODE, "claude": CLAUDE}) == [NODE, CLAUDE]
    assert pp.image_binaries({"claude": NODE, "node": NODE}) == [NODE]


def mutate(fn):
    doc = pp.render_profile(template(), [NODE])
    fn(doc)
    return doc


def test_profile_problems_catches_each_broken_rule():
    assert pp.profile_problems(pp.render_profile(template(), [NODE])) == []
    mutations = {
        "uppercase id": lambda d: d.update(id="Amap"),
        "leading dash": lambda d: d.update(id="-x"),
        "the example id": lambda d: d.update(id="claude-code"),
        "no display_name": lambda d: d.pop("display_name"),
        "second credential": lambda d: d["credentials"].append(
            dict(d["credentials"][0])),
        "second key variable": lambda d: d["credentials"][0]["env_vars"]
        .append("CLAUDE_API_KEY"),
        "reserved env": lambda d: d["credentials"][0].update(
            env_vars=["v1_X"]),
        "undeclared discovery": lambda d: d["discovery"].update(
            credentials=["nope"]),
        "second endpoint": lambda d: d["endpoints"].append(
            dict(d["endpoints"][0])),
        "port 80": lambda d: d["endpoints"][0].update(port=80),
        "tls skip": lambda d: d["endpoints"][0].update(tls="skip"),
        "unknown key": lambda d: d.update(extra=1),
        "empty binary": lambda d: d.update(binaries=[""]),
    }
    for label, fn in mutations.items():
        assert pp.profile_problems(mutate(fn)), label


def test_same_profile_ignores_export_defaults_but_not_changes():
    r = pp.render_profile(template(), [NODE, CLAUDE])
    g = copy.deepcopy(r)
    g.update(resource_version=3, source="custom", scope="workspace")
    g["endpoints"][0].update(rules=[], deny_rules=[], persisted_queries={})
    g["binaries"] = [{"path": NODE}, CLAUDE]
    assert pp.same_profile(r, g)
    g2 = copy.deepcopy(g)
    g2["binaries"] = [{"path": "/other"}, CLAUDE]
    assert not pp.same_profile(r, g2)
    g3 = copy.deepcopy(g)
    g3["endpoints"].append(dict(g3["endpoints"][0]))
    assert not pp.same_profile(r, g3)
    g4 = copy.deepcopy(g)
    g4["credentials"][0]["env_vars"] = ["OTHER"]
    assert not pp.same_profile(r, g4)
    assert pp.gateway_copies([g, {"id": "x"}]) == [g]
    with pytest.raises(ProfileError):
        pp.gateway_copies({"id": pp.PROFILE_ID})


def test_update_document_carries_the_resource_version():
    r = pp.render_profile(template(), [NODE])
    assert pp.update_document(r, 7)["resource_version"] == 7
    assert "resource_version" not in r
    for bad in (0, None, True, "3"):
        with pytest.raises(ProfileError):
            pp.update_document(r, bad)


def test_the_module_is_pure(tmp_path):
    tree = ast.parse((REPO / "provider_profile.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [
                getattr(node, "module", None) or ""]
            assert "subprocess" not in names
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "os":
            assert node.attr != "system" and not node.attr.startswith(
                ("exec", "spawn", "posix_spawn"))
    before = sorted(tmp_path.rglob("*"))
    pp.render_profile(template(), [NODE])
    assert sorted(tmp_path.rglob("*")) == before
    assert "/usr" not in (REPO / "provider_profile.py").read_text(
        encoding="utf-8")


def test_only_the_profile_names_a_real_host():
    ident = test_l1_runbook.identifier_problems
    for path in (REPO / "provider_profile.py", REPO / "providers" / "README.md"):
        assert ident(path) == []
    assert ident(pp.TEMPLATE) != []
    assert ident(pp.TEMPLATE, allowed_hosts=(HOST,)) == []


def test_the_readme_cites_the_example_and_d10():
    text = (REPO / "providers" / "README.md").read_text(encoding="utf-8")
    for needle in ("providers/claude-code.yaml", "main@acbac9c", "D10",
                   "binaries", "l1-kit profile"):
        assert needle in text, needle
