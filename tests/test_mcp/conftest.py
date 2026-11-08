"""Shared fixtures for MCP tool tests.

Settings-config isolation is autouse for the whole directory: any test that
reaches ``_impl_settings_update`` (directly or through a tool) writes to a
temp dir, never to the real ``~/.genesis/config/``. This guard exists because
the suite-wide overlay isolation in ``tests/conftest.py`` deliberately
excludes ``genesis.mcp.health.settings`` (import-heavy for an every-test
fixture), and a settings test that forgot its own patch once wrote a test
literal into the live inbox_monitor overlay.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# test_payment_context_js.py runs payment_context's page scripts in a real
# browser. Where Playwright is not installed (CI's test job installs no
# [browser] extra) it is not collected, rather than adding 13 skips to the
# suite's skip ceiling; the live card-fill L-tests are its coverage there.
# Same pattern as tests/test_ui/conftest.py.
try:
    import playwright  # noqa: F401
except ImportError:
    collect_ignore = ["test_payment_context_js.py"]


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory."""
    return tmp_path


@pytest.fixture(autouse=True)
def _patch_config_dir(config_dir: Path):
    """Redirect all settings config reads/writes to the temp dir."""
    with (
        patch("genesis.mcp.health.settings._CONFIG_DIR", config_dir),
        patch("genesis.mcp.health.settings._USER_CONFIG_DIR", config_dir),
    ):
        yield
