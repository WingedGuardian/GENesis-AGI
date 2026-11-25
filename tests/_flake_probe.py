"""THROWAWAY CI proof for #5024 (reverted before review): fail one untouched test once."""

import os
import tempfile

import pytest

TARGET = "tests/test_scripts/test_check_skip_ceiling.py::TestRefusesRatherThanGuesses::test_missing_report_refuses"


def pytest_runtest_setup(item):
    if item.nodeid != TARGET:
        return
    marker = os.path.join(tempfile.gettempdir(), "genesis-5024-flake-probe")
    if not os.path.exists(marker):
        open(marker, "w").close()
        pytest.fail("flake probe: first attempt fails on purpose (#5024 CI proof)")
