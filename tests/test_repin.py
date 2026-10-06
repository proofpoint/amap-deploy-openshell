"""repin.py: the repin workflow's helper. No network: `stale` is given a fake
remote, and `apply` runs on a copy of the repository's pins and docs."""

import json
import shutil
from pathlib import Path

import pytest

import repin
import siblings
import test_docs_agree

REPO = Path(__file__).absolute().parents[1]
NEW = "f" * 40


@pytest.fixture
def copy(tmp_path):
    shutil.copy(REPO / siblings.PINS_NAME, tmp_path / siblings.PINS_NAME)
    for p in repin.shipped_markdown(REPO):
        dest = tmp_path / p.relative_to(REPO)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(p, dest)
    for name in repin.PIN_HISTORY:
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, dest)
    return tmp_path


def pins(root):
    return {s.name: s.commit for s in
            siblings.load_pins(root / siblings.PINS_NAME)}


def test_stale_lists_only_pinned_siblings_whose_main_moved():
    current = pins(REPO)
    router = "amap-router-local"
    heads = {f"https://github.com/proofpoint/{n}": c
             for n, c in current.items()}
    heads[f"https://github.com/proofpoint/{router}"] = NEW
    out = repin.stale(REPO / siblings.PINS_NAME, heads.__getitem__)
    assert out == [{"name": router, "pin": current[router], "head": NEW}]


def test_stale_never_asks_about_the_unpinned_spec():
    asked = []
    repin.stale(REPO / siblings.PINS_NAME,
                lambda url: asked.append(url) or pins(REPO)[url.rsplit("/", 1)[1]])
    assert not any(u.endswith("/amap-spec") for u in asked)


def test_apply_moves_the_pin_and_every_shipped_quote(copy):
    name = "amap-router-local"
    before = (copy / siblings.PINS_NAME).read_text(encoding="utf-8")
    changed = repin.apply(copy, name, NEW)
    assert pins(copy)[name] == NEW
    # Only that sibling's line of siblings.json changed.
    after = (copy / siblings.PINS_NAME).read_text(encoding="utf-8")
    assert [a for a, b in zip(after.splitlines(), before.splitlines())
            if a != b] == [ln for ln in after.splitlines() if NEW in ln]
    assert copy / "docs" / "TUTORIAL.md" in changed
    for p in repin.shipped_markdown(copy):
        problems, _ = test_docs_agree.pin_problems(
            p.read_text(encoding="utf-8"), pins(copy))
        assert problems == [], p
    for name_ in repin.PIN_HISTORY:
        assert (copy / name_).read_text(encoding="utf-8") == \
            (REPO / name_).read_text(encoding="utf-8")


def test_apply_to_the_current_pin_changes_nothing(copy):
    name = "amap-connector-claude"
    assert repin.apply(copy, name, pins(copy)[name]) == []


@pytest.mark.parametrize("name, commit", [
    ("amap-router-local", "e43dbba"),
    ("amap-spec", NEW),
    ("no-such-sibling", NEW),
])
def test_apply_refuses_and_writes_nothing(copy, name, commit):
    before = (copy / siblings.PINS_NAME).read_bytes()
    with pytest.raises(repin.RepinError):
        repin.apply(copy, name, commit)
    assert (copy / siblings.PINS_NAME).read_bytes() == before


def test_requote_touches_only_lines_naming_the_sibling():
    old, new = "a" * 40, "b" * 40
    text = ("amap-router-local `aaaaaaa`\n"
            "amap-connector-claude `aaaaaaa`\n"
            "amap-router-local OpenShell main@aaaaaaa\n")
    assert repin.requote(text, "amap-router-local", old, new) == (
        "amap-router-local `bbbbbbb`\n"
        "amap-connector-claude `aaaaaaa`\n"
        "amap-router-local OpenShell main@aaaaaaa\n")


def test_the_history_list_is_the_docs_tests():
    assert tuple(repin.PIN_HISTORY) == tuple(test_docs_agree.PIN_HISTORY)


def test_main_check_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(repin, "remote_head",
                        lambda url: pins(REPO)[url.rsplit("/", 1)[1]])
    monkeypatch.setattr(repin, "stale",
                        lambda p, head=repin.remote_head: [])
    assert repin.main(["check"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_refuses_bad_usage(capsys):
    assert repin.main(["apply", "amap-router-local"]) == repin.EXIT_USAGE


# --- the OpenShell release watcher ---------------------------------------------

def test_openshell_check_reports_the_newest_release_after_the_vendored_tag():
    tags = ["v0.1.1", "v0.1.2", "v0.1.10", "v0.2.0", "v0.1.3", "nightly"]
    assert repin.openshell_check(tags=lambda url: tags) == {
        "current": "v0.1.2", "tag": "v0.2.0"}


def test_openshell_check_ignores_prereleases_and_older_tags():
    tags = ["v0.1.1", "v0.1.2", "v0.2.0-rc1", "v0.2.0-beta.2", "dev", "0.3.0"]
    assert repin.openshell_check(tags=lambda url: tags) is None


def test_openshell_check_reads_the_vendored_tag():
    seen = []
    repin.openshell_check(tags=lambda url: seen.append(url) or [])
    assert seen == [repin.OPENSHELL_URL]
    assert repin.locks.read_source(str(repin.OPENSHELL_SOURCE))[0] == "v0.1.2"


def test_main_openshell_check_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(repin, "remote_tags", lambda url: ["v0.1.2"])
    assert repin.main(["openshell-check"]) == 0
    assert capsys.readouterr().out.strip() == "null"


def test_the_release_workflow_never_merges_and_asks_for_a_live_run():
    text = (REPO / ".github" / "workflows" / "openshell-release.yml"
            ).read_text(encoding="utf-8")
    for needle in ("repin.py openshell-check", "regen.sh", "--apply",
                   "pytest interceptor/tests", "pytest tests"):
        assert needle in text, needle
    assert "needs a live run" in text.lower()
    assert "gh pr merge" not in text
