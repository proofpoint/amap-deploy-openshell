"""The interceptor's own run: `python -m pytest interceptor/tests`, in the
virtualenv built from interceptor/requirements.txt.

A missing grpc or protobuf ends the run as a usage error, so the run cannot
pass as a smaller set of tests.
"""

import importlib.util

import pytest

NEEDED = ("grpc", "google.protobuf")
VENV = ("python3 -m venv --system-site-packages ivenv && "
        "ivenv/bin/pip install --require-hashes --only-binary=:all: "
        "-r interceptor/requirements.txt && "
        "ivenv/bin/python -m pytest interceptor/tests")


def _missing(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError:
        return True


def pytest_configure(config):
    gone = [n for n in NEEDED if _missing(n)]
    if gone:
        raise pytest.UsageError(
            f"the interceptor tests need {', '.join(gone)}, which this Python "
            f"does not have. Build the interceptor virtualenv and run them "
            f"there: {VENV}")
