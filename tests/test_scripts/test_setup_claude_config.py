"""scripts/setup_claude_config.py — the .mcp.json render interactive sessions load.

config/mcp.json.template is the single source for two consumers: this render
(interactive sessions, and a dispatch on the "full" MCP profile) and
genesis.cc.session_config.render_mcp_servers (dispatch profiles, which filter it
down and can never add a server). A server only some dispatch profile needs
therefore stays in the template and is left out of the interactive render (#2319).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "setup_claude_config.py"
TEMPLATE = REPO_ROOT / "config" / "mcp.json.template"
INSTALL = REPO_ROOT / "scripts" / "install.sh"


def _module():
    spec = importlib.util.spec_from_file_location("setup_claude_config", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _template_servers() -> dict:
    return json.loads(TEMPLATE.read_text())["mcpServers"]


def test_the_interactive_render_leaves_out_dispatch_only_servers(tmp_path):
    mod = _module()
    rendered = json.loads(mod.render_mcp_text(TEMPLATE.read_text(), tmp_path))
    servers = rendered["mcpServers"]
    assert "discord-bot" not in servers
    expected = set(_template_servers()) - set(mod.INTERACTIVE_EXCLUDED_SERVERS)
    assert set(servers) == expected and expected, servers
    for spec in servers.values():
        assert "{{GENESIS_ROOT}}" not in json.dumps(spec)
    assert servers["genesis-health"]["command"].startswith(str(tmp_path))


def test_every_excluded_server_still_reaches_the_profiles_that_need_it():
    """Leaving a server out of .mcp.json is only safe while the template still
    carries it AND a dispatch profile still names it: the profile render is the
    only way left to reach it, and it can only remove servers, never add them."""
    from genesis.cc.direct_session import _PROFILE_TO_MCP
    from genesis.cc.session_config import _MCP_PROFILES

    mod = _module()
    # Only MCP profiles a DirectSession profile actually maps to are reachable:
    # a server named by an orphaned _MCP_PROFILES entry is still unreachable.
    reachable = {s for p in set(_PROFILE_TO_MCP.values()) for s in _MCP_PROFILES.get(p, ())}
    template = _template_servers()
    for server in mod.INTERACTIVE_EXCLUDED_SERVERS:
        assert server in template, f"{server!r} left the template: dispatch loses it too"
        assert server in reachable, (
            f"{server!r} is reached by no dispatch profile, so excluding it from "
            ".mcp.json removes it everywhere — drop it from the template instead"
        )


def test_render_writes_once_then_reports_no_change(tmp_path):
    mod = _module()
    (tmp_path / "config").mkdir()
    shutil.copyfile(TEMPLATE, tmp_path / "config" / "mcp.json.template")
    assert mod.render_mcp_config(tmp_path, dry_run=False) is True
    written = (tmp_path / ".mcp.json").read_text()
    assert written.endswith("\n")
    assert "discord-bot" not in json.loads(written)["mcpServers"]
    assert mod.render_mcp_config(tmp_path, dry_run=False) is False


def test_an_existing_mcp_json_listing_the_server_is_re_rendered_without_it(tmp_path):
    """Installs that already have a .mcp.json from the old render (which copied
    every template server) lose discord-bot on their next bootstrap, which runs
    this script."""
    mod = _module()
    (tmp_path / "config").mkdir()
    shutil.copyfile(TEMPLATE, tmp_path / "config" / "mcp.json.template")
    old = TEMPLATE.read_text().replace("{{GENESIS_ROOT}}", str(tmp_path))
    (tmp_path / ".mcp.json").write_text(old)
    assert mod.render_mcp_config(tmp_path, dry_run=False) is True
    assert "discord-bot" not in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]


def test_mcp_only_renders_just_the_mcp_json(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copyfile(TEMPLATE, tmp_path / "config" / "mcp.json.template")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--mcp-only", "--genesis-root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == [".mcp.json", "config"]
    assert "discord-bot" not in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]


@pytest.mark.parametrize("stale_output", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_mcp_only_fails_when_nothing_could_be_rendered(tmp_path, stale_output, dry_run):
    """No template means nothing was rendered, whatever is already on disk: an
    earlier .mcp.json, or --dry-run, must not report that as success."""
    stale = '{"mcpServers": {}}\n'
    if stale_output:
        (tmp_path / ".mcp.json").write_text(stale)
    cmd = [sys.executable, str(SCRIPT), "--mcp-only", "--genesis-root", str(tmp_path)]
    if dry_run:
        cmd.append("--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, (r.returncode, r.stderr)
    if stale_output:
        assert (tmp_path / ".mcp.json").read_text() == stale
    else:
        assert not (tmp_path / ".mcp.json").exists()


def test_install_renders_mcp_json_through_the_same_renderer():
    """install.sh used to sed the template into .mcp.json, which copies every
    server; it must call the renderer that knows which ones to leave out."""
    text = INSTALL.read_text()
    block = text[text.index('MCP_TARGET="$REPO_DIR/.mcp.json"') :]
    block = block[: block.index("\nfi\n")]
    assert "setup_claude_config.py" in block and "--mcp-only" in block, block
    assert "sed " not in block, block
