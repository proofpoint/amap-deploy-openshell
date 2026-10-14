"""With the host registry off (DESIGN.md D27), behaviour is today's, byte
for byte.

`golden/registry-off.json` was captured from amap-deploy-openshell 7694353,
before D27: each example member's rendered create command and policy, and the
interceptor's decision on a set of operations. A fleet with no `identity` key
and one with `"identity": "legacy"` must reproduce it exactly. Standard
library only.
"""

import json
import shutil
from pathlib import Path

import pytest

import policy
import render
import router_config
from interceptor.rule import check_create

REPO = Path(__file__).absolute().parents[1]
GOLDEN = json.loads((Path(__file__).parent / "golden" /
                     "registry-off.json").read_text(encoding="utf-8"))


def outputs(fleet, tmp):
    out = {"render": {}, "intercept": {}}
    host = render.Host("/srv/golden-home", "amap-openshell-agent:test",
                       render.parse_run_as("1234:5678"), True)
    for name in policy.named_instances(fleet):
        r = render.render_member(fleet, name, host,
                                 "/srv/golden-home/policies/%s.yaml" % name)
        out["render"][name] = {"argv": r.argv, "policy_yaml": r.policy_yaml,
                               "address": r.address}
    home = Path(tmp).resolve() / "home"
    home.mkdir()
    (home / "payload").mkdir()
    (home / "roster").mkdir()
    for n in policy.named_instances(fleet):
        router_config.create_lanes(str(home), n)
    h, ws = str(home), policy.workspace_of(fleet)

    def op(name, mounts):
        return {"workspaceScope": {"workspace": ws}, "name": name,
                "spec": {"template": {"image": "img", "driverConfig":
                                      {"docker": {"mounts": mounts}}}}}

    def rows(n):
        return render.driver_config(render.member_rows(h, n))["docker"]["mounts"]

    with_env = op("alpha", rows("alpha"))
    with_env["spec"] = {"environment": {"AMAP_SELF": "x@y.z"},
                        "template": {"image": "img",
                                     "environment": {"A": "b"},
                                     "driverConfig": {"docker": {
                                         "mounts": rows("alpha")}}}}
    other = op("alpha", rows("alpha") + [{
        "type": "bind", "source": render.lane_dir(h, "beta",
                                                  render.LANE_OUTBOX),
        "target": "/opt/amap/other", "read_only": False}])
    wrong_ws = op("alpha", rows("alpha"))
    wrong_ws["workspaceScope"] = {"workspace": "other"}
    cases = {"alpha_rendered": op("alpha", rows("alpha")),
             "beta_rendered": op("beta", rows("beta")),
             "alpha_with_env": with_env, "alpha_other_outbox": other,
             "no_mounts": {"workspaceScope": {"workspace": ws}, "name": "zzz",
                           "spec": {"template": {"image": "img"}}},
             "wrong_ws": wrong_ws}

    def no_show(*a):
        raise AssertionError("the registry was asked with the switch off")
    for k, o in cases.items():
        d = check_create(o, h, fleet, show=no_show)
        out["intercept"][k] = {"allowed": d.allowed,
                               "reasons": [r.replace(h, "<HOME>")
                                           for r in d.reasons]}
    return out


@pytest.mark.parametrize("switch", [None, "legacy"])
def test_the_switch_off_is_byte_for_byte_today(tmp_path, switch):
    doc = json.loads((REPO / "examples" / "fleet.json").read_text())
    if switch:
        doc["identity"] = switch
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(doc))
    fleet = policy.load_fleet(str(path))
    assert policy.uses_registry(fleet) is False
    assert outputs(fleet, tmp_path) == GOLDEN


def test_an_unknown_switch_value_is_refused(tmp_path):
    doc = json.loads((REPO / "examples" / "fleet.json").read_text())
    doc["identity"] = "registr"
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(policy.PolicyError, match="identity"):
        policy.load_fleet(str(path))
