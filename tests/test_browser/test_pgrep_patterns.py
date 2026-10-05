"""BROWSER_PGREP_PATTERNS are POSIX ERE strings consumed by `pgrep -f`.

Every consumer (health probe, awareness signal, process reaper) treats a non-zero
pgrep exit as "no browser processes", so a pattern pgrep cannot compile blinds all
of them silently. These tests compile each pattern with the real tools.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from genesis.browser.types import BROWSER_PGREP_PATTERNS

DRIVER = "(playwright|patchright)/driver/node"


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep not available")
@pytest.mark.parametrize("pattern", BROWSER_PGREP_PATTERNS)
def test_pgrep_accepts_pattern(pattern):
    """Exit 0 (match) or 1 (no match) are both fine; 2 means pgrep rejected the regex."""
    rc = subprocess.run(["pgrep", "-fc", pattern], capture_output=True).returncode
    assert rc in (0, 1), f"pgrep rejected {pattern!r} (exit {rc})"


def test_python_style_group_would_be_rejected():
    """Guard the guard: the test above can tell a bad pattern from a good one."""
    if shutil.which("pgrep") is None:
        pytest.skip("pgrep not available")
    rc = subprocess.run(["pgrep", "-fc", "(?:play|patch)wright"], capture_output=True).returncode
    assert rc == 2


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        (
            "/venv/lib/python3.12/site-packages/playwright/driver/node /venv/.../cli.js run-driver",
            True,
        ),
        (
            "/venv/lib/python3.12/site-packages/patchright/driver/node /venv/.../cli.js run-driver",
            True,
        ),
        ("/home/u/genesis/.venv/bin/python scripts/genesis_mcp_server.py --server health", False),
    ],
)
def test_driver_pattern_matches_both_drivers(cmdline, expected):
    assert DRIVER in BROWSER_PGREP_PATTERNS
    matched = (
        subprocess.run(
            ["grep", "-Eq", DRIVER],
            input=cmdline.encode(),
            capture_output=True,
        ).returncode
        == 0
    )
    assert matched is expected
