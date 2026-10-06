"""Finds the amap-router-local checkout and imports its public `router.config`.

`router.config` is one of the router's public modules (`PUBLIC_MODULES`,
amap-router-local `router/__init__.py:178`). This module never writes into the
checkout: bytecode writing is off while the import runs.

The search works as `policy.find_sandy` does. When `$AMAP_ROUTER_REPO` is set and
not empty it is the only place searched. Otherwise the search walks up from this
file's directory and takes the nearest `<ancestor>/amap-router-local` that
contains `router/reset.py`. A directory with the right name and no
`router/reset.py` is skipped. Paths use `.absolute()`, never `.resolve()`.

Standard library only, and no other module of this repository is imported.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from typing import Mapping, Optional

ROUTER_VARIABLE = "AMAP_ROUTER_REPO"
ROUTER_DIR_NAME = "amap-router-local"
ROUTER_CONFIRM = "router/reset.py"


class RouterNotFound(ImportError):
    """The amap-router-local checkout, or its `router.config`, is missing."""


def find_router(env: Optional[Mapping[str, str]] = None,
                start: Optional[Path] = None) -> Path:
    """The amap-router-local checkout."""
    env = os.environ if env is None else env
    value = env.get(ROUTER_VARIABLE)
    if value:
        cand = Path(value).absolute()
        if (cand / ROUTER_CONFIRM).is_file():
            return cand
        raise RouterNotFound(
            f"${ROUTER_VARIABLE}={value} does not contain {ROUTER_CONFIRM}; it "
            f"is the only place searched because the variable names it. Point "
            f"it at the {ROUTER_DIR_NAME} checkout, or unset it to search "
            f"beside this repository.")
    here = Path(__file__).absolute().parent if start is None \
        else Path(start).absolute()
    for ancestor in [here, *here.parents]:
        cand = ancestor / ROUTER_DIR_NAME
        if (cand / ROUTER_CONFIRM).is_file():
            return cand
    raise RouterNotFound(
        f"cannot find the {ROUTER_DIR_NAME} checkout (confirmed by "
        f"{ROUTER_CONFIRM}) beside any ancestor of {here}. Check it out beside "
        f"this repository, or set ${ROUTER_VARIABLE}.")


def not_found_remedy() -> str:
    """What the operator does when the router checkout is missing: this
    repository's sentence, built from the names `find_router` searches by."""
    return (f"check out {ROUTER_DIR_NAME} beside this repository, or set "
            f"${ROUTER_VARIABLE}")


def router_config(env: Optional[Mapping[str, str]] = None,
                  start: Optional[Path] = None) -> types.ModuleType:
    """The router's `router.config` module, imported from the checkout."""
    root = find_router(env, start)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        module = importlib.import_module("router.config")
    finally:
        sys.dont_write_bytecode = previous
    file = getattr(module, "__file__", None) or ""
    if not _is_under(Path(file).absolute(), root / "router"):
        raise RouterNotFound(
            f"a different `router` package is already imported, from {file}; "
            f"expected the checkout at {root}")
    return module


def _is_under(path: Path, parent: Path) -> bool:
    return parent == path or parent in path.parents
