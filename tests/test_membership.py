"""membership.json: the name rule, the (workspace, name, id) record, and the
refusals that keep it trustworthy."""

import json
import os

import pytest

import membership
from membership import IdMismatch, Member, MembershipError

UUID = "3f2b8c1e-5a4d-4e0f-9b7a-6c1d2e3f4a5b"


def M(name, id_="id-1", workspace="default"):
    return Member(workspace, name, id_)


def _write_doc(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _doc(*entries, version=1):
    return {"version": version,
            "members": [{"workspace": w, "name": n, "id": i}
                        for w, n, i in entries]}


# ---------------------------------------------------------------- the name rule

@pytest.mark.parametrize("name", ["a", "alpha", "beta2", "0abc", "a-b-c",
                                  "a" * 19])
def test_name_problem_accepts_valid_names(name):
    assert membership.name_problem(name) is None


@pytest.mark.parametrize("name, fragment", [
    pytest.param("a" * 20, "at most 19", id="twenty-chars"),
    pytest.param("Alpha", "lowercase", id="uppercase"),
    pytest.param("al_pha", "lowercase", id="underscore"),
    pytest.param("-alpha", "start or end", id="leading-hyphen"),
    pytest.param("alpha-", "start or end", id="trailing-hyphen"),
    pytest.param("al--pha", "'--'", id="double-hyphen"),
    pytest.param("alpha.beta", "lowercase", id="dot"),
    pytest.param("alphá", "lowercase", id="non-ascii"),
    pytest.param("", "is required", id="empty"),
    pytest.param(None, "must be a string", id="not-a-string"),
])
def test_name_problem_refuses_with_reason(name, fragment):
    reason = membership.name_problem(name)
    assert reason is not None
    assert fragment in reason


def test_no_member_name_can_be_the_routers_local_part():
    """The router's own address has a dot in its local part, and a sandbox name
    cannot, so no member can collide with it."""
    assert membership.name_problem("amap.router") is not None


def test_workspace_problem_uses_the_same_rule():
    assert membership.workspace_problem("default") is None
    assert membership.workspace_problem("Team_A") is not None
    assert "at most 19" in membership.workspace_problem("w" * 20)


def test_id_problem():
    assert membership.id_problem(UUID) is None
    for bad in ("", "a b", "a\nb", 5):
        assert membership.id_problem(bad) is not None


# --------------------------------------------------------------- record/check

def test_record_adds_a_new_member_sorted():
    start = []
    one = membership.record(start, M("beta", "id-b"))
    two = membership.record(one, M("alpha", "id-a"))
    assert two == [M("alpha", "id-a"), M("beta", "id-b")]
    assert start == []
    assert one == [M("beta", "id-b")]


def test_record_the_same_triple_is_unchanged():
    members = [M("alpha"), M("beta", "id-b")]
    assert membership.record(members, M("alpha")) == members


def test_record_refuses_an_invalid_name():
    with pytest.raises(MembershipError, match="at most 19"):
        membership.record([], M("a" * 20))
    with pytest.raises(MembershipError, match="lowercase"):
        membership.record([], M("Alpha"))


def test_record_same_name_with_a_different_id_raises_id_mismatch():
    members = [M("alpha", "id-1")]
    with pytest.raises(IdMismatch) as excinfo:
        membership.record(members, M("alpha", "id-2"))
    e = excinfo.value
    assert e.recorded.id == "id-1"
    assert e.observed.id == "id-2"
    text = str(e)
    for fragment in ("mismatch", "alpha", "id-1", "id-2"):
        assert fragment in text
    assert members == [M("alpha", "id-1")]


def test_record_refuses_one_id_under_two_names():
    with pytest.raises(MembershipError):
        membership.record([M("alpha", "id-1")], M("beta", "id-1"))


def test_record_refuses_a_second_workspace():
    with pytest.raises(MembershipError, match="more than one OpenShell workspace"):
        membership.record([M("alpha")], M("beta", "id-2", workspace="team"))


def test_check_reports_an_id_mismatch():
    members = [M("alpha", "id-1")]
    reason = membership.check(members, Member("default", "alpha", "id-2"))
    assert reason is not None
    for fragment in ("mismatch", "id-1", "id-2"):
        assert fragment in reason
    assert membership.check(members, Member("default", "alpha", "id-1")) is None
    assert "not recorded" in membership.check(members, M("beta", "id-3"))
    assert "workspace mismatch" in membership.check(
        members, Member("team", "alpha", "id-1"))


def test_remove():
    members = [M("alpha", "id-a"), M("beta", "id-b")]
    assert membership.remove(members, "alpha") == [M("beta", "id-b")]
    assert len(members) == 2
    with pytest.raises(MembershipError, match="not recorded"):
        membership.remove(members, "gamma")


def test_require_workspace_refuses_a_member_outside_the_fleet_workspace():
    members = [M("alpha"), M("beta", "id-b", workspace="team")]
    with pytest.raises(MembershipError, match="beta"):
        membership.require_workspace(members, "default")
    membership.require_workspace([M("alpha")], "default")


# ------------------------------------------------------------------ the file

def test_load_absent_is_none_and_empty_members_is_an_empty_list(tmp_path):
    assert membership.load(tmp_path / "membership.json") is None
    p = _write_doc(tmp_path / "membership.json", {"version": 1, "members": []})
    assert membership.load(p) == []


def test_load_refuses_an_empty_file(tmp_path):
    p = tmp_path / "membership.json"
    p.write_bytes(b"")
    with pytest.raises(MembershipError, match="not valid JSON"):
        membership.load(p)


def test_load_refuses_a_directory(tmp_path):
    with pytest.raises(MembershipError):
        membership.load(tmp_path)


def test_load_refuses_one_name_recorded_twice(tmp_path):
    p = _write_doc(tmp_path / "membership.json", _doc(
        ("default", "alpha", "id-1"), ("default", "alpha", "id-2")))
    with pytest.raises(MembershipError) as excinfo:
        membership.load(p)
    assert "alpha" in str(excinfo.value)
    assert "recorded twice" in str(excinfo.value)


def test_load_refuses_one_id_under_two_names(tmp_path):
    p = _write_doc(tmp_path / "membership.json", _doc(
        ("default", "alpha", "id-1"), ("default", "beta", "id-1")))
    with pytest.raises(MembershipError, match="id-1"):
        membership.load(p)


def test_load_refuses_members_in_two_workspaces(tmp_path):
    p = _write_doc(tmp_path / "membership.json", _doc(
        ("default", "alpha", "id-1"), ("team", "beta", "id-2")))
    with pytest.raises(MembershipError, match="more than one OpenShell workspace"):
        membership.load(p)


def test_load_refuses_an_invalid_name_in_the_file(tmp_path):
    p = _write_doc(tmp_path / "membership.json",
                   _doc(("default", "Alpha", "id-1")))
    with pytest.raises(MembershipError, match="lowercase"):
        membership.load(p)


def _top_extra():
    d = _doc(("default", "alpha", "id-1"))
    d["extra"] = 1
    return d


def _entry_extra():
    d = _doc(("default", "alpha", "id-1"))
    d["members"][0]["extra"] = 1
    return d


def _version_absent():
    d = _doc(("default", "alpha", "id-1"))
    del d["version"]
    return d


@pytest.mark.parametrize("doc", [
    pytest.param(_top_extra(), id="top-extra"),
    pytest.param(_entry_extra(), id="entry-extra"),
    pytest.param(_doc(("default", "alpha", "id-1"), version=2), id="version-2"),
    pytest.param(_doc(("default", "alpha", "id-1"), version=True),
                 id="version-true"),
    pytest.param(_version_absent(), id="version-absent"),
])
def test_load_refuses_unknown_keys_and_versions(tmp_path, doc):
    p = _write_doc(tmp_path / "membership.json", doc)
    with pytest.raises(MembershipError):
        membership.load(p)


def test_write_then_load_round_trips(tmp_path):
    p = tmp_path / "membership.json"
    members = [M("alpha", "id-a"), M("beta", "id-b")]
    membership.write(p, members)
    assert membership.load(p) == members


def test_dump_is_sorted_and_stable():
    text = membership.dump([M("beta", "id-b"), M("alpha", "id-a")])
    assert text == (
        '{\n'
        '  "version": 1,\n'
        '  "members": [\n'
        '    {\n'
        '      "workspace": "default",\n'
        '      "name": "alpha",\n'
        '      "id": "id-a"\n'
        '    },\n'
        '    {\n'
        '      "workspace": "default",\n'
        '      "name": "beta",\n'
        '      "id": "id-b"\n'
        '    }\n'
        '  ]\n'
        '}\n')


def test_write_refuses_invalid_members_and_writes_nothing(tmp_path):
    p = tmp_path / "membership.json"
    with pytest.raises(MembershipError):
        membership.write(p, [M("Alpha")])
    assert not p.exists()
    assert os.listdir(tmp_path) == []


def test_write_refuses_a_missing_directory(tmp_path):
    d = tmp_path / "absent"
    with pytest.raises(MembershipError):
        membership.write(d / "membership.json", [M("alpha")])
    assert not d.exists()


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path,
                                                             monkeypatch):
    p = tmp_path / "membership.json"
    membership.write(p, [M("alpha", "id-a")])
    before = p.read_text(encoding="utf-8")

    def fake_replace(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(membership.os, "replace", fake_replace)
    with pytest.raises(OSError):
        membership.write(p, [M("alpha", "id-a"), M("beta", "id-b")])
    assert p.read_text(encoding="utf-8") == before
    assert os.listdir(tmp_path) == ["membership.json"]


def test_write_leaves_only_the_file(tmp_path):
    p = tmp_path / "membership.json"
    membership.write(p, [M("alpha")])
    assert sorted(os.listdir(tmp_path)) == ["membership.json"]
    assert os.stat(p).st_mode & 0o777 == 0o644
