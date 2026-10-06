"""`gateway-config`: the section 6 fragment, compared read-only with the
operator's `gateway.toml`. The settings are OpenShell
main@acbac9c:docs/how-it-works/sandboxes/runtimes.mdx:120-131. Each is PRESENT,
ABSENT or UNKNOWN, and UNKNOWN is never a pass."""

import pytest

import _l1_world
import amap_openshell
import gateway
import l1_kit
from interceptor import wire
from test_l1_kit import tree

SETTINGS = ["allow_driver_config = true", "enable_bind_mounts = true",
            "enabled = false"]


def gateway_config(capsys, toml, *extra):
    capsys.readouterr()
    code = amap_openshell.main(["--gateway-toml", str(toml), *extra,
                                "gateway-config"])
    c = capsys.readouterr()
    return code, c.out, c.err


def verdicts(out):
    return {line.split(None, 1)[1].split(" (")[0]: line.split()[0]
            for line in out.splitlines()
            if line.split()[:1] in (["PRESENT"], ["ABSENT"], ["UNKNOWN"])}


def test_gateway_config_prints_the_fragment(tmp_path, capsys):
    code, out, err = gateway_config(capsys, tmp_path / "missing.toml")
    assert l1_kit.GATEWAY_FRAGMENT in out


def test_each_of_the_three_settings_is_present_absent_or_unknown(tmp_path,
                                                                capsys):
    toml = tmp_path / "gateway.toml"
    toml.write_text(_l1_world.GATEWAY_TOML)
    code, out, err = gateway_config(capsys, toml)
    assert code == 0 and verdicts(out) == {s: "PRESENT" for s in SETTINGS}

    toml.write_text(_l1_world.GATEWAY_TOML.replace(
        "enable_bind_mounts = true\n", ""))
    code, out, err = gateway_config(capsys, toml)
    assert code == 1
    assert verdicts(out) == {**{s: "PRESENT" for s in SETTINGS},
                             "enable_bind_mounts = true": "ABSENT"}

    # a directory in its place cannot be read, as root or not
    bad = tmp_path / "dir" / "gateway.toml"
    bad.mkdir(parents=True)
    code, out, err = gateway_config(capsys, bad)
    assert code == 1 and verdicts(out) == {s: "UNKNOWN" for s in SETTINGS}
    assert "ABSENT" not in out


def test_a_setting_under_the_wrong_table_is_absent(tmp_path, capsys):
    toml = tmp_path / "gateway.toml"
    toml.write_text("[openshell.drivers.docker]\nallow_driver_config=true\n"
                    "enable_bind_mounts = true\nenabled = false\n")
    code, out, err = gateway_config(capsys, toml)
    assert code == 1
    assert verdicts(out) == {"allow_driver_config = true": "PRESENT",
                             "enable_bind_mounts = true": "PRESENT",
                             "enabled = false": "ABSENT"}


def test_an_absent_gateway_toml_is_absent_not_unknown(tmp_path, capsys):
    code, out, err = gateway_config(capsys, tmp_path / "nothing.toml")
    assert code == 1
    assert verdicts(out) == {s: "ABSENT" for s in SETTINGS}
    assert "is absent" in out and "UNKNOWN" not in out


def test_a_located_nowhere_gateway_toml_is_unknown(monkeypatch, capsys):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    capsys.readouterr()
    code = amap_openshell.main(["gateway-config"])
    out = capsys.readouterr().out
    assert code == 1 and verdicts(out) == {s: "UNKNOWN" for s in SETTINGS}


def test_gateway_config_writes_nothing(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "gateway.toml").write_text(_l1_world.GATEWAY_TOML)
    before = (tree(home), tree(cfg))
    gateway_config(capsys, cfg / "gateway.toml", "--home", str(home))
    assert (tree(home), tree(cfg)) == before
    with pytest.raises(SystemExit) as e:
        amap_openshell.build_parser().parse_args(["gateway-config", "--apply"])
    assert e.value.code == 2


def test_the_three_settings_are_design_section_6s():
    assert gateway.required_settings() == tuple(
        line for line in gateway.setting_lines(l1_kit.GATEWAY_FRAGMENT)
        if "=" in line)
    assert len(gateway.required_settings()) == 3
    assert gateway.FRAGMENT is l1_kit.GATEWAY_FRAGMENT


def test_gateway_config_prints_the_interceptor_fragment_after_the_drivers(
        tmp_path, capsys):
    toml = tmp_path / "gateway.toml"
    toml.write_text(_l1_world.GATEWAY_TOML)
    home = str(tmp_path / "home")
    code, out, err = gateway_config(capsys, toml, "--home", home)
    assert code == 0 and verdicts(out) == {s: "PRESENT" for s in SETTINGS}
    assert l1_kit.GATEWAY_FRAGMENT + "\n" + wire.gateway_fragment(home) in out


def test_gateway_config_without_home_names_what_the_fragment_needs(
        tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AMAP_OPENSHELL_HOME", raising=False)
    toml = tmp_path / "gateway.toml"
    toml.write_text(_l1_world.GATEWAY_TOML)
    code, out, err = gateway_config(capsys, toml)
    assert "[[openshell.gateway.interceptors]]" not in out
    assert "needs --home" in out
