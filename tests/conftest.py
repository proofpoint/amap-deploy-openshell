"""The host registry's test harness (DESIGN.md D27).

- **The `real_registry` marker.** Its tests run the adapter against the real
  host registry, which public CI cannot reach. They are DESELECTED, never
  skipped, unless $AMAP_OPENSHELL_PRIVATE_OVERLAY names an operator-private
  overlay (tests/test_real_registry.py).
- **`safe_tmp`.** A directory with no group- or world-writable ancestor.
  host-registry.json's checks refuse such an ancestor, so the registry's
  tests cannot live under /tmp. It is made under
  $AMAP_TEST_SAFE_ROOT, or ~/.cache/amap-openshell-tests, and removed after
  the test.
"""

import os
import shutil

import pytest

import _fake_registry

REAL_REGISTRY = "real_registry"
OVERLAY_VARIABLE = "AMAP_OPENSHELL_PRIVATE_OVERLAY"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{REAL_REGISTRY}: runs against the real host registry; "
                   f"deselected unless ${OVERLAY_VARIABLE} is set")


def pytest_collection_modifyitems(config, items):
    if os.environ.get(OVERLAY_VARIABLE):
        return
    keep, drop = [], []
    for item in items:
        (drop if item.get_closest_marker(REAL_REGISTRY) else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


@pytest.fixture
def safe_tmp():
    root = _fake_registry.safe_root()
    yield root
    shutil.rmtree(root, ignore_errors=True)
