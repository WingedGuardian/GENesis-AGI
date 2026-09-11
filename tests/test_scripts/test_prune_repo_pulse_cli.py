"""The retention CLI refuses a window that would empty the table it bounds.

Every prune in `scripts/prune_repo_pulse.py` computes its cutoff as
``now - timedelta(days=N)``. A bare ``type=int`` accepts a NEGATIVE N, which
subtracts a negative and pushes the cutoff into the FUTURE — at which point
"older than the cutoff" matches EVERY row and the retention pass deletes the
whole table instead of trimming it.

BOTH flags take the validated type, not just the one a review named: they share
the arithmetic, so they share the bug. `--days` was never reported and was
equally exposed.

The CRUD layer carries its own guard (`prune_closed` raises on a sub-1 window),
because the caller that gets this wrong is the one that never thought about it.
This file covers the CLI surface: an argparse type gives the operator a readable
refusal instead of a traceback, and refuses BEFORE any connection is opened.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_repo_pulse.py"


def _load():
    spec = importlib.util.spec_from_file_location("prune_repo_pulse", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prune_repo_pulse"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("bad", ["-1", "0", "-180"])
def test_retention_days_rejects_a_sub_one_day_window(bad):
    mod = _load()
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="must be >= 1 day"):
        mod._retention_days(bad)


@pytest.mark.parametrize("good", ["1", "45", "180"])
def test_retention_days_accepts_valid_windows(good):
    """The boundary stays open at 1 — the guard must not over-refuse."""
    mod = _load()
    assert mod._retention_days(good) == int(good)


def test_retention_days_rejects_a_non_integer():
    mod = _load()
    import argparse

    with pytest.raises(argparse.ArgumentTypeError, match="whole number of days"):
        mod._retention_days("7.5")


@pytest.mark.parametrize("flag", ["--verification-days", "--days"])
def test_cli_refuses_a_negative_window_before_touching_the_database(flag):
    """End to end through argparse: exit 2, a usable message, and NO prune.

    Run as a subprocess so the assertion covers the real argv path — the flag
    wiring is the thing that would silently regress if someone restored
    ``type=int``, and an in-process call to the type function alone would not
    notice that.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), flag, "-1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, f"expected argparse's usage exit, got {result.returncode}"
    assert "must be >= 1 day" in result.stderr
    # argparse refuses during parsing, so the prune coroutine never runs and no
    # database is opened — the failure cannot half-delete anything.
    assert "deleted" not in result.stdout
