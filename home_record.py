"""The recorded home: where `amap-openshell.py` finds `$AMAP_OPENSHELL_HOME`
when neither `--home` nor the variable gives it.

`install --apply` and `provision-guest.sh --home DIR --apply` write the home
to `$XDG_CONFIG_HOME/amap-openshell/home`, or `$HOME/.config/...` when
`XDG_CONFIG_HOME` is unset, the same base `gateway.toml` uses. One line, the
absolute path. `teardown --apply` removes it when it names the home torn
down. A shell's rc files are not touched: they belong to the operator's
dotfiles, and the record works for any caller, interactive or not.

Order, highest first: `--home`, `$AMAP_OPENSHELL_HOME`, this record.

Standard library only. It imports nothing from this repository.
"""

from __future__ import annotations

import os
import posixpath
from typing import Mapping, Optional

HOME_VARIABLE = "AMAP_OPENSHELL_HOME"
DIRNAME = "amap-openshell"
FILENAME = "home"


class RecordError(ValueError):
    """The record exists but cannot be used."""


def record_path(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The record's path, or None when neither XDG_CONFIG_HOME nor HOME is
    set."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or (
        posixpath.join(env["HOME"], ".config") if env.get("HOME") else None)
    return posixpath.join(base, DIRNAME, FILENAME) if base else None


def read(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The recorded home, or None when there is no record."""
    path = record_path(env)
    if path is None or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        home = fh.read().strip()
    if not home or not posixpath.isabs(home) or "\n" in home:
        raise RecordError(f"{path} does not hold one absolute path; fix it, "
                          f"or pass --home or set ${HOME_VARIABLE}")
    return home


def write(home: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Record `home`. Returns the record's path, or None when there is no
    config base to write under."""
    path = record_path(env)
    if path is None:
        return None
    os.makedirs(posixpath.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(home + "\n")
    os.replace(tmp, path)
    return path


def forget(home: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Remove the record if it names `home`. True when it was removed."""
    path = record_path(env)
    try:
        recorded = read(env)
    except RecordError:
        return False
    if path is None or recorded != home:
        return False
    os.remove(path)
    return True
