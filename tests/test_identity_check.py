"""payload/identity-check and payload/amap-main's hold (DESIGN.md D27).

name.json must be exactly the five-member document the host rendered;
amap-main starts nothing until it is, never falls back to another address,
and holds unless AMAP_IDENTITY_DIR is exactly the identity target. The wrapper runs under /bin/sh against the fakes `_amap_main.Lab`
provides. Standard library only.
"""

import json
import os
import time
from pathlib import Path

import pytest

import _amap_main
import host_registry as hr

REPO = Path(__file__).absolute().parents[1]
IC = hr.identity_check
EXPECTED = {"schema": "example-registry.name/1", "name": "alpha",
            "address": "alpha@agents.example.org", "logical_id": "a" * 32,
            "epoch": 2}


def env_for(expected=EXPECTED, **overrides):
    e = {IC.ENV_DIR: IC.IDENTITY_TARGET, IC.ENV_SCHEMA: expected["schema"],
         IC.ENV_NAME: expected["name"], IC.ENV_ADDRESS: expected["address"],
         IC.ENV_LOGICAL_ID: expected["logical_id"],
         IC.ENV_EPOCH: str(expected["epoch"])}
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return e


def write(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / IC.NAME_JSON
    p.write_text(text)
    return p


# --- the document ------------------------------------------------------------

def test_the_exact_document_passes(tmp_path):
    write(tmp_path / "id", json.dumps(EXPECTED))
    assert IC.address_for(env_for(), str(tmp_path / "id")) == EXPECTED["address"]


def _mutated(**change):
    doc = dict(EXPECTED)
    for k, v in change.items():
        if v is _DROP:
            del doc[k]
        else:
            doc[k] = v
    return json.dumps(doc)


_DROP = object()

WITNESSES = {
    "missing member": _mutated(epoch=_DROP),
    "extra member": _mutated(label="x"),
    "duplicate member": json.dumps(EXPECTED)[:-1] + ', "name": "alpha"}',
    "epoch as a string": _mutated(epoch="2"),
    "epoch as true": _mutated(epoch=True),
    "epoch zero": _mutated(epoch=0),
    "wrong schema": _mutated(schema="example-registry.name/2"),
    "wrong name": _mutated(name="beta", address="beta@agents.example.org"),
    "wrong address": _mutated(address="alpha@agents.example.com"),
    "wrong domain only": _mutated(address="alpha@other.example.org"),
    "wrong logical_id": _mutated(logical_id="b" * 32),
    "wrong epoch": _mutated(epoch=3),
    "not an object": "[]",
    "not json": "{",
}


@pytest.mark.parametrize("what", sorted(WITNESSES))
def test_every_mismatch_is_refused(tmp_path, what):
    write(tmp_path / "id", WITNESSES[what])
    with pytest.raises(IC.Refused):
        IC.address_for(env_for(), str(tmp_path / "id"))


def test_a_symlinked_name_json_is_refused(tmp_path):
    real = write(tmp_path / "real", json.dumps(EXPECTED))
    (tmp_path / "id").mkdir()
    os.symlink(real, tmp_path / "id" / IC.NAME_JSON)
    with pytest.raises(IC.Refused):
        IC.address_for(env_for(), str(tmp_path / "id"))


def test_an_oversized_name_json_is_refused(tmp_path):
    write(tmp_path / "id", " " * (IC.MAX_NAME_JSON + 1) + json.dumps(EXPECTED))
    with pytest.raises(IC.Refused, match="too large"):
        IC.address_for(env_for(), str(tmp_path / "id"))


@pytest.mark.parametrize("key", IC.ENV_KEYS)
def test_each_expected_variable_is_required(tmp_path, key):
    write(tmp_path / "id", json.dumps(EXPECTED))
    with pytest.raises(IC.Refused, match=key):
        IC.address_for(env_for(**{key: None}), str(tmp_path / "id"))


def test_the_identity_dir_must_be_exactly_the_target(tmp_path):
    """a crafted create cannot point the check at a writable path
    holding its own name.json."""
    forged = write(tmp_path / "writable", json.dumps(EXPECTED)).parent
    with pytest.raises(IC.Refused, match="is not /opt/amap/identity"):
        IC.address_for(env_for(**{IC.ENV_DIR: str(forged)}))


def test_the_shipped_target_is_the_rendered_one():
    import render
    assert IC.IDENTITY_TARGET == render.IDENTITY_TARGET == "/opt/amap/identity"


# --- amap-main holds ---------------------------------------------------------

class IdentityLab(_amap_main.Lab):
    """The Lab, with identity-check copied into its payload and its target
    moved under the lab: the one test seam. The shipped value is pinned by
    test_the_shipped_target_is_the_rendered_one."""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.identity = self.tmp / "identity"
        text = (REPO / "payload" / "identity-check").read_text()
        old = 'IDENTITY_TARGET = "/opt/amap/identity"'
        assert text.count(old) == 1
        _amap_main._write_exec(self.payload / "identity-check",
                               text.replace(old, f"IDENTITY_TARGET = "
                                                 f"{str(self.identity)!r}"))

    def identity_env(self, **overrides):
        e = self.env(**{_amap_main.SELF_INPUT: None})
        e.update(env_for(**{IC.ENV_DIR: str(self.identity)}))
        for k, v in overrides.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e


@pytest.fixture
def ilab(tmp_path):
    lab = IdentityLab(tmp_path)
    yield lab
    lab.close()


def _held(lab, env, seconds=1.5):
    proc = lab.start(env=env)
    time.sleep(seconds)
    started = lab.starts() or lab.claude_events()
    lab.close()
    return proc, started


def test_a_matching_identity_sets_both_self_variables(ilab):
    write(ilab.identity, json.dumps(EXPECTED))
    ilab.run_once(env=ilab.identity_env())
    env = ilab.starts()[0]["env"]
    assert env["AMAP_DELIVERY_SELF"] == EXPECTED["address"]


@pytest.mark.parametrize("what", sorted(WITNESSES))
def test_a_mismatched_name_json_starts_nothing(ilab, what):
    write(ilab.identity, WITNESSES[what])
    _proc, started = _held(ilab, ilab.identity_env())
    assert not started
    assert "holding" in ilab.stderr()


def test_an_absent_name_json_starts_nothing(ilab):
    _proc, started = _held(ilab, ilab.identity_env())
    assert not started


@pytest.mark.parametrize("key", [k for k in IC.ENV_KEYS if k != IC.ENV_DIR])
def test_a_missing_expected_variable_starts_nothing(ilab, key):
    write(ilab.identity, json.dumps(EXPECTED))
    _proc, started = _held(ilab, ilab.identity_env(**{key: None}))
    assert not started


def test_a_forged_identity_dir_starts_nothing(ilab):
    forged = write(ilab.tmp / "writable", json.dumps(EXPECTED)).parent
    _proc, started = _held(ilab, ilab.identity_env(
        **{IC.ENV_DIR: str(forged)}))
    assert not started


def test_there_is_no_fallback_to_the_rendered_address(ilab):
    _proc, started = _held(ilab, ilab.identity_env(
        **{_amap_main.SELF_INPUT: EXPECTED["address"]}))
    assert not started
