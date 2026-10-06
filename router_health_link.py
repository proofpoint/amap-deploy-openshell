"""Loads the core of amap-deploy-sandy's `router_health` for `verify`, and
refuses a change.

Only the three-outcome core that amap-deploy-sandy agreed to keep stable is
loaded from their checkout: `PASS`, `FAIL`, `UNKNOWN`, `Unresolved`, `Fact`,
`Check` and the helpers around them. Their PR #9 pins it on their side, with a
promise to send an advance message before it changes. Everything else of
`router_health` (the sections, the context, the runner, the fact derivations)
is this repository's own copy in `router_sections.py`. The module is loaded
from the sandy checkout's file, as `policy` loads `fleet_policy` (decision D1):

- the checkout is not put on `sys.path`, so sandy's other modules stay
  unimportable from here;
- the module is not left in `sys.modules`;
- bytecode is not written, so the checkout is never modified.

`USED_NAMES` is the agreed core. A checkout whose module lacks one is refused,
and a test pins the list.

A missing checkout raises `policy.SandyNotFound`, which the command line turns
into a message. A checkout whose module no longer fits raises
`RouterHealthNotFound`, which is a defect to report, not a setup step.

Standard library only, and Python 3.9 compatible.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Optional

import policy

HEALTH_FILE = "router_health.py"
MODULE_NAME = "sandy_router_health"

# The core amap-deploy-sandy agreed to keep stable, and nothing else.
USED_NAMES = ("PASS", "FAIL", "UNKNOWN", "Verdict", "Unresolved", "Fact",
              "Check", "check", "unknown", "same_set", "CannotRun")


class RouterHealthNotFound(ImportError):
    """The sandy checkout exists, but its `router_health` does not fit."""


def load(root: Optional[Path] = None) -> types.ModuleType:
    """`router_health` from the sandy checkout at `root` (default: the checkout
    `policy.find_sandy` finds, which raises `policy.SandyNotFound` when there is
    none)."""
    root = policy.find_sandy() if root is None else Path(root)
    path = Path(root) / HEALTH_FILE
    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(path))
    if spec is None or spec.loader is None or not path.is_file():
        raise RouterHealthNotFound(
            f"{path} is not a loadable Python module: the interface this "
            f"deployment reuses changed")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    # `dataclasses` looks its class's module up in sys.modules while the class
    # body runs, so the module is registered for the load and removed after.
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
        sys.modules.pop(MODULE_NAME, None)
    for name in USED_NAMES:
        if not hasattr(module, name):
            raise RouterHealthNotFound(
                f"{path} lacks {name}: the core amap-deploy-sandy agreed to "
                f"keep stable changed")
    return module
