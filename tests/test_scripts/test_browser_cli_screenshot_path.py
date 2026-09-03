"""Drift guard for the CLI half of the unique-screenshot-path fix.

`scripts/browser.py` deliberately imports no `genesis` modules — it is a
standalone one-off CLI — so it carries its own copy of the naming scheme used by
the MCP tool's `_impl_browser_screenshot`. Prose comments link the two copies;
this test is what actually catches them drifting apart.

The defect being guarded: a module-level `DEFAULT_SCREENSHOT` constant bound one
filename for the life of the process, so every capture without an explicit path
silently overwrote the previous one.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "browser.py"

# Must stay in step with genesis/mcp/health/browser.py's stamp format:
#   %Y%m%dT%H%M%S%fZ  ->  20260902T190142123456Z
_STAMP = re.compile(r"^\d{8}T\d{12}Z$")


def _load_cli():
    spec = importlib.util.spec_from_file_location("_genesis_browser_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not importlib.util.find_spec("playwright"),
    reason="playwright not installed",
)
class TestCliDefaultScreenshotPath:
    def test_each_call_returns_a_distinct_path(self):
        cli = _load_cli()
        paths = [cli._default_screenshot_path() for _ in range(5)]
        assert len(set(paths)) == 5, paths

    def test_stamp_shape_matches_the_mcp_tool(self):
        """Microsecond resolution + trailing Z — a second-resolution stamp ties
        a rapid burst, which is the ordering half of the original bug."""
        cli = _load_cli()
        name = Path(cli._default_screenshot_path()).name
        stamp = name.split("_")[-2]
        assert _STAMP.match(stamp), name

    def test_no_fixed_default_constant_remains(self):
        """The module-level constant IS the bug — a name bound once per process
        cannot be unique per call."""
        cli = _load_cli()
        assert not hasattr(cli, "DEFAULT_SCREENSHOT"), (
            "a module-level default screenshot path reintroduces the "
            "overwrite bug — it binds one name for the whole process"
        )
