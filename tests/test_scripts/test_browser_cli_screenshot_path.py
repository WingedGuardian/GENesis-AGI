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
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "browser.py"

# Must stay in step with genesis/mcp/health/browser.py's stamp format:
#   %Y%m%dT%H%M%S%fZ  ->  20260902T190142123456Z
_STAMP = re.compile(r"^\d{8}T\d{12}Z$")


def _load_cli():
    """Import the standalone CLI WITHOUT requiring playwright.

    `scripts/browser.py` imports `playwright.sync_api` at module scope, and CI
    does not install playwright. Guarding this class with a `skipif` therefore
    meant the CLI half of the fix had ZERO coverage on the one machine that
    gates merges — it was only ever verified on a developer box that happened
    to have playwright, while CI reported a green run that had asserted
    nothing. Nothing under test here touches playwright (`_default_screenshot_path`
    is pure stdlib), so stub the import instead of skipping the test.

    The stub raises if anything actually calls it, so a future test that DOES
    need a real browser fails loudly rather than passing against a mock.
    """

    def _stub_sync_playwright(*_args, **_kwargs):
        raise RuntimeError(
            "playwright is stubbed in this test module — it exists only to "
            "satisfy the CLI's module-level import"
        )

    pw = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _stub_sync_playwright
    pw.sync_api = sync_api

    # Stub UNCONDITIONALLY, even where playwright is installed. A
    # `find_spec`-guarded stub would take one path on a developer box and the
    # other in CI — so the path that matters would be the one never exercised
    # here, which is the same shape of hole this fix exists to close.
    _MISSING = object()
    saved = {
        name: sys.modules.get(name, _MISSING) for name in ("playwright", "playwright.sync_api")
    }
    sys.modules["playwright"] = pw
    sys.modules["playwright.sync_api"] = sync_api
    try:
        spec = importlib.util.spec_from_file_location("_genesis_browser_cli", CLI_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore exactly what was there, so a real playwright installed by
        # another test module survives and the stub never leaks.
        for name, prior in saved.items():
            if prior is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior


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
