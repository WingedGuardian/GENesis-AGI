"""The separately approved single-turn list_changed qualification probe."""

import os
from pathlib import Path

import pytest

from tests.test_hooks.test_codex_mcp_turn_refresh_native import run_refresh_probe

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NOTIFICATION_TURN_TESTS") != "1",
    reason="list_changed mocked turn needs its own explicit opt-in",
)


def test_list_changed_refreshes_both_existing_thread_catalogs(tmp_path):
    run_refresh_probe(tmp_path, notification_fixture=Path(__file__).with_name("mcp_notification_fixture.py"))
