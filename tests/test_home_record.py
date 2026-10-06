"""The recorded home (home_record.py): written by install --apply and by
provision-guest.sh --home, read by amap-openshell.py after --home and
$AMAP_OPENSHELL_HOME, removed by teardown. conftest gives every test its own
XDG_CONFIG_HOME, so the real ~/.config is never touched."""

import os

import pytest

import amap_openshell
import home_record


def test_the_record_lives_under_the_config_base(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "x"), "HOME": "/nowhere"}
    assert home_record.record_path(env) == str(tmp_path / "x" / "amap-openshell" / "home")
    env = {"XDG_CONFIG_HOME": "", "HOME": str(tmp_path)}
    assert home_record.record_path(env) == str(tmp_path / ".config" / "amap-openshell" / "home")
    assert home_record.record_path({}) is None


def test_write_read_and_forget(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert home_record.read(env) is None
    home_record.write("/srv/amap-home", env)
    assert home_record.read(env) == "/srv/amap-home"
    assert not home_record.forget("/srv/other", env)
    assert home_record.read(env) == "/srv/amap-home"
    assert home_record.forget("/srv/amap-home", env)
    assert home_record.read(env) is None


@pytest.mark.parametrize("text", ["", "relative/home\n", "/a\n/b\n"])
def test_a_bad_record_is_refused(tmp_path, text):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    path = home_record.record_path(env)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with pytest.raises(home_record.RecordError):
        home_record.read(env)


def _seen_home(monkeypatch, argv):
    seen = {}

    def fake(args):
        seen["home"] = getattr(args, "home", None)
        seen["env"] = os.environ.get(home_record.HOME_VARIABLE)
        return 0
    import verbs
    monkeypatch.setattr(verbs, "main", fake)
    assert amap_openshell.main(argv) == 0
    return seen


def test_the_cli_falls_back_to_the_record(monkeypatch):
    home_record.write("/srv/recorded")
    seen = _seen_home(monkeypatch, ["list"])
    assert seen == {"home": "/srv/recorded", "env": "/srv/recorded"}


def test_the_variable_and_the_flag_beat_the_record(monkeypatch):
    home_record.write("/srv/recorded")
    monkeypatch.setenv(home_record.HOME_VARIABLE, "/srv/variable")
    assert _seen_home(monkeypatch, ["list"])["home"] == "/srv/variable"
    assert _seen_home(monkeypatch, ["--home", "/srv/flag", "list"])["home"] == "/srv/flag"


def test_a_bad_record_is_a_usage_error(capsys):
    path = home_record.record_path()
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("relative\n")
    assert amap_openshell.main(["list"]) == amap_openshell.EXIT_USAGE
    assert path in capsys.readouterr().err


def test_without_a_record_the_home_is_still_required(capsys):
    assert amap_openshell.main(["list"]) == amap_openshell.EXIT_USAGE
    assert "--home is required" in capsys.readouterr().err
