"""scripts/setup_claude_config.py — the .mcp.json render interactive sessions load.

config/mcp.json.template is the single source for two consumers: this render
(interactive and conversation-channel sessions, which run in the checkout;
dispatched sessions, "full" included, run outside it and never read .mcp.json) and
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


# ── .mcp.json is merged by server name (#2413) ─────────────────────────────
# Genesis owns the servers its template defines; every other server, and every
# other top-level key, belongs to the user (or to bootstrap's serena
# registration) and survives the render.


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path_factory, monkeypatch):
    """Backups go under GENESIS_HOME; keep every test (and the subprocess runs,
    which inherit the environment) away from the real ~/.genesis. Outside
    tmp_path, which some tests list."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(home / ".genesis"))
    # A deploy's own variables must not leak in from the running environment.
    monkeypatch.delenv("GENESIS_MU_BACKUP_DIR", raising=False)
    monkeypatch.delenv("GENESIS_DEPLOY_SUMMARY", raising=False)
    return home / ".genesis"


def _root(tmp_path) -> Path:
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(TEMPLATE, root / "config" / "mcp.json.template")
    return root


def _render(mod, root) -> dict:
    return json.loads(mod.render_mcp_text(TEMPLATE.read_text(), root))


def _backups(genesis_home: Path) -> list[Path]:
    return sorted((genesis_home / "deploy-backups").glob("*/.mcp.json"))


_SERENA = {"command": "/x/.claude/mcp/run-serena", "args": ["--context", "claude-code"]}


def test_a_users_own_server_survives_the_render(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["mine"] = {"command": "/opt/mine", "args": ["--x"]}
    (root / ".mcp.json").write_text(json.dumps(current, indent=2) + "\n")
    before = (root / ".mcp.json").read_bytes()
    assert mod.render_mcp_config(root, dry_run=False) is False
    assert (root / ".mcp.json").read_bytes() == before


def test_serena_registered_by_bootstrap_is_not_dropped_and_re_added(tmp_path):
    """Every bootstrap used to drop serena (not in the template) and then
    re-register it ~120 lines later; a crash in between left it out."""
    mod = _module()
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["serena"] = _SERENA
    # _register_serena's json.dump writes without a trailing newline; a text
    # comparison would call that a change.
    (root / ".mcp.json").write_text(json.dumps(current, indent=2))
    mtime = (root / ".mcp.json").stat().st_mtime_ns
    assert mod.render_mcp_config(root, dry_run=False) is False
    assert (root / ".mcp.json").stat().st_mtime_ns == mtime
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["serena"] == _SERENA


def test_a_drifted_genesis_server_is_corrected_backed_up_and_named(tmp_path, _scratch_home, capsys):
    mod = _module()
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["genesis-health"]["command"] = "/old/root/scripts/genesis_mcp_server.sh"
    current["mcpServers"]["serena"] = _SERENA
    old_text = json.dumps(current, indent=2) + "\n"
    (root / ".mcp.json").write_text(old_text)
    assert mod.render_mcp_config(root, dry_run=False) is True
    after = json.loads((root / ".mcp.json").read_text())
    assert (
        after["mcpServers"]["genesis-health"] == _render(mod, root)["mcpServers"]["genesis-health"]
    )
    assert after["mcpServers"]["serena"] == _SERENA
    backups = _backups(_scratch_home)
    assert len(backups) == 1 and backups[0].read_text() == old_text
    # A user's own entry may carry an env block with a key: owner-only.
    assert backups[0].stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "genesis-health" in out and "serena" in out and str(backups[0].parent) in out


def test_the_merge_keeps_template_order_then_the_users_servers(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    current = {"mcpServers": {"mine": {"command": "/opt/mine"}}}
    (root / ".mcp.json").write_text(json.dumps(current))
    assert mod.render_mcp_config(root, dry_run=False) is True
    names = list(json.loads((root / ".mcp.json").read_text())["mcpServers"])
    assert names == list(_render(mod, root)["mcpServers"]) + ["mine"]


def test_other_top_level_keys_are_kept(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    current = {"mcpServers": {"mine": {"command": "/opt/mine"}}, "extra": {"k": 1}}
    (root / ".mcp.json").write_text(json.dumps(current))
    mod.render_mcp_config(root, dry_run=False)
    assert json.loads((root / ".mcp.json").read_text())["extra"] == {"k": 1}


def test_a_dispatch_only_server_in_an_existing_file_is_still_removed(tmp_path):
    """discord-bot is Genesis's (it is in the template) even though the
    interactive render leaves it out: it must not survive as a 'user' server."""
    mod = _module()
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["discord-bot"] = {"command": "/x/discord"}
    (root / ".mcp.json").write_text(json.dumps(current))
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert "discord-bot" not in json.loads((root / ".mcp.json").read_text())["mcpServers"]


def test_a_retired_genesis_server_is_removed(tmp_path, monkeypatch):
    mod = _module()
    monkeypatch.setattr(mod, "RETIRED_SERVERS", frozenset({"old-genesis"}))
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["old-genesis"] = {"command": "/x/old"}
    (root / ".mcp.json").write_text(json.dumps(current))
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert "old-genesis" not in json.loads((root / ".mcp.json").read_text())["mcpServers"]


def test_retired_servers_share_no_name_with_the_template():
    mod = _module()
    assert not set(mod.RETIRED_SERVERS) & set(_template_servers())


@pytest.mark.parametrize(
    "bad",
    ["{not json", "[1, 2]", '{"mcpServers": []}', '{"mcpServers": "x"}', "\udcff"],
    ids=["invalid", "array", "servers-array", "servers-string", "undecodable"],
)
def test_an_unusable_file_is_backed_up_and_replaced(tmp_path, _scratch_home, bad):
    mod = _module()
    root = _root(tmp_path)
    raw = bad.encode("utf-8", "surrogateescape")
    (root / ".mcp.json").write_bytes(raw)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert json.loads((root / ".mcp.json").read_text()) == _render(mod, root)
    backups = _backups(_scratch_home)
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_the_files_mode_is_kept(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    (root / ".mcp.json").write_text('{"mcpServers": {}}')
    (root / ".mcp.json").chmod(0o600)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert (root / ".mcp.json").stat().st_mode & 0o777 == 0o600


def test_a_symlinked_mcp_json_is_written_through_and_stays_a_link(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    real = tmp_path / "dotfiles-mcp.json"
    real.write_text('{"mcpServers": {"mine": {"command": "/opt/mine"}}}')
    (root / ".mcp.json").symlink_to(real)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert (root / ".mcp.json").is_symlink()
    servers = json.loads(real.read_text())["mcpServers"]
    assert "mine" in servers and "genesis-health" in servers


def test_a_dry_run_writes_nothing(tmp_path, _scratch_home):
    mod = _module()
    root = _root(tmp_path)
    (root / ".mcp.json").write_text('{"mcpServers": {"mine": {"command": "/opt/mine"}}}')
    before = (root / ".mcp.json").read_bytes()
    assert mod.render_mcp_config(root, dry_run=True) is True
    assert (root / ".mcp.json").read_bytes() == before
    assert not (_scratch_home / "deploy-backups").exists()


def test_a_first_render_needs_no_backup(tmp_path, _scratch_home):
    mod = _module()
    root = _root(tmp_path)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert not (_scratch_home / "deploy-backups").exists()


def test_the_run_summary_names_the_change_when_a_deploy_asks_for_one(tmp_path, monkeypatch):
    mod = _module()
    root = _root(tmp_path)
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GENESIS_DEPLOY_SUMMARY", str(summary))
    current = _render(mod, root)
    current["mcpServers"]["genesis-health"]["command"] = "/old/x"
    (root / ".mcp.json").write_text(json.dumps(current))
    mod.render_mcp_config(root, dry_run=False)
    line = summary.read_text()
    assert line.startswith("mcp.json ") and "genesis-health" in line
    assert not line.startswith("kept ")  # bootstrap counts "^kept " lines as units


def test_a_null_server_map_keeps_the_files_other_keys(tmp_path):
    mod = _module()
    root = _root(tmp_path)
    (root / ".mcp.json").write_text('{"mcpServers": null, "extra": 1}')
    assert mod.render_mcp_config(root, dry_run=False) is True
    after = json.loads((root / ".mcp.json").read_text())
    assert after["extra"] == 1 and after["mcpServers"] == _render(mod, root)["mcpServers"]


def test_two_changes_into_one_backup_directory_keep_both_backups(tmp_path, monkeypatch):
    """Two checkouts, or two runs in the same second, share a directory name."""
    mod = _module()
    shared = tmp_path / "one-dir"
    monkeypatch.setenv("GENESIS_MU_BACKUP_DIR", str(shared))
    root = _root(tmp_path)
    for marker in ("first", "second"):
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {}, "m": marker}))
        assert mod.render_mcp_config(root, dry_run=False) is True
    saved = sorted(p.read_text() for p in shared.glob(".mcp.json*"))
    assert (
        len(saved) == 2 and any("first" in s for s in saved) and any("second" in s for s in saved)
    )


def test_a_failed_backup_leaves_the_file_unchanged_and_does_not_abort(tmp_path, monkeypatch):
    mod = _module()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GENESIS_MU_BACKUP_DIR", str(blocker / "sub"))
    root = _root(tmp_path)
    (root / ".mcp.json").write_text('{"mcpServers": {"mine": {"command": "/m"}}}')
    before = (root / ".mcp.json").read_bytes()
    assert mod.render_mcp_config(root, dry_run=False) is False
    assert (root / ".mcp.json").read_bytes() == before


def test_the_notice_does_not_call_serena_the_users(tmp_path, capsys):
    mod = _module()
    root = _root(tmp_path)
    current = _render(mod, root)
    current["mcpServers"]["genesis-health"]["command"] = "/old/x"
    current["mcpServers"]["serena"] = _SERENA
    (root / ".mcp.json").write_text(json.dumps(current))
    mod.render_mcp_config(root, dry_run=False)
    out = capsys.readouterr().out
    assert "kept serena" in out and "your" not in out
