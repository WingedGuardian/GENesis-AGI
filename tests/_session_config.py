"""Keep real MCP config generation inside each test's temporary directory."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_generated_mcp_configs(tmp_path):
    """Redirect output only; retain the real template, root and renderer.

    An independent patch survives a test calling its own monkeypatch.undo().
    This function-scoped fixture does not cover collection or child processes.
    """
    from genesis.cc import session_config

    with pytest.MonkeyPatch.context() as mp:
        generated = tmp_path / "generated-mcp"
        mp.setattr(session_config, "_generated_mcp_dir", lambda _config_dir: generated)
        yield generated
