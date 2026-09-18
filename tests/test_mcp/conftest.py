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
