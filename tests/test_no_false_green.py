"""A green run that tested nothing must not be reportable as green.

The principle, borrowed from amap-deploy-sandy's own suite: skipping is correct
when nothing was intended, and a false green when something was. Only the
operator can say which, so `verify` never decides for them.

This repo's instance is: **a listing that failed must not read as "nothing is
there."** A `sandbox list` that could not be asked is not an empty workspace,
a `docker inspect` that failed is not a container with no shared mounts, and a
`membership.json` that is absent is not one that records nobody. Each is
`Unresolved`, which makes the check UNKNOWN and `verify` exit 1.

Each test names the one-line mutation that makes it fire.
"""

import membership
import pytest

import verify
from test_verify import World, world  # noqa: F401  (the fixture)
from verify import Unresolved


class StubCtx:
    """Just enough of `Ctx` for a derivation: `fact` and `value`, over values
    the test planted. The derivation under test is the real one."""

    def __init__(self, **values):
        self._values = values

    def fact(self, name):
        return verify.Fact(name, self._values[name], f"stub:{name}")

    def value(self, name):
        return self._values[name]


def test_a_failed_listing_yields_Unresolved_not_an_empty_list():
    """Mutation: make `_f_strays` return `[]` when `listed_names` is
    Unresolved (for instance by dropping the `_inherit` call)."""
    ctx = StubCtx(listed_names=Unresolved("`sandbox list` failed: gateway down"),
                  fleet_members=["alpha", "beta"])
    value, provenance = verify._f_strays(ctx)
    assert isinstance(value, Unresolved)
    assert "gateway down" in value.reason
    assert value != []


def test_a_genuinely_empty_stray_set_IS_an_empty_list():
    """The inverse, so the rule cannot be met by never answering.
    Mutation: return Unresolved whenever the list is empty."""
    ctx = StubCtx(listed_names=["alpha", "beta"],
                  fleet_members=["alpha", "beta"])
    value, _ = verify._f_strays(ctx)
    assert value == [] and not isinstance(value, Unresolved)
    ctx = StubCtx(listed_names=["alpha", "beta", "stray"],
                  fleet_members=["alpha", "beta"])
    assert verify._f_strays(ctx)[0] == ["stray"]


def test_the_check_reports_UNKNOWN_and_never_PASS_when_the_listing_failed(world):  # noqa: F811
    """End to end. Mutation: as above; the stray check would read PASS."""
    world.fake.set_flag(fail_list=True)
    code, out, err = world.verify()
    assert code == 1
    assert world.result_of(out, "no sandbox in the workspace is outside") \
        == "UNKNOWN"
    assert "UNKNOWN whether no sandbox in the workspace is outside the fleet" \
        in out
    assert "gateway down" in out


def test_the_same_holds_for_shared_mount_sources():
    """Mutation: make `_f_shared_sources` skip a member whose mounts are
    Unresolved; the unseen member might be the one sharing."""
    ctx = StubCtx(
        actual_mounts={"alpha": {"/x": ("/src/a", False)},
                       "beta": Unresolved("`docker ps` failed")},
        host=None, fleet_members=["alpha", "beta"])
    value, _ = verify._f_shared_sources(ctx)
    assert isinstance(value, Unresolved) and "beta" in value.reason


def test_a_member_whose_mounts_are_unknown_is_never_reported_as_unshared(world):  # noqa: F811
    world.fake.set_flag(fail_docker_ps=True)
    code, out, err = world.verify()
    assert code == 1
    for member in ("alpha", "beta"):
        assert world.result_of(out, f"{member}: no mount source is shared") \
            == "UNKNOWN"


def test_absent_and_empty_membership_are_different_answers(world):  # noqa: F811
    """Mutation: make `_f_members` return `{}` for an absent file."""
    path = world.home / "membership.json"
    ctx = verify.Ctx(verify.options_from(_args(world)), None)
    path.unlink()
    value, _ = verify._f_members(ctx)
    assert isinstance(value, Unresolved) and "absent" in value.reason
    membership.write(str(path), [])
    ctx = verify.Ctx(verify.options_from(_args(world)), None)
    value, _ = verify._f_members(ctx)
    assert value == {} and not isinstance(value, Unresolved)


def _args(world):
    import argparse
    return argparse.Namespace(home=str(world.home), image="img", run_as=None,
                              restart_policy=False, openshell="openshell",
                              gateway_toml=None)


def test_an_unanswered_fact_compares_to_nothing():
    """`Unresolved` is equal to nothing, itself included, and cannot be put in
    a set: a comparison cannot quietly succeed."""
    u = Unresolved("why")
    assert u != u and u != [] and u != 0 and u != ""
    with pytest.raises(TypeError):
        {u}
    check = verify.check(claim="c", expected=verify.Fact("n", [], "p"),
                         actual=u, remedy="r")
    assert check.result == verify.UNKNOWN
