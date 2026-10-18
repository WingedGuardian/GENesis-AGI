"""scripts/setup_claude_config.py — the .mcp.json render interactive sessions load.

config/mcp.json.template is the single source for two consumers: this render
(interactive and conversation-channel sessions, which run in the checkout;
dispatched sessions, "full" included, run outside it and never read .mcp.json) and
genesis.cc.session_config.render_mcp_servers (dispatch profiles, which filter it
down and can never add a server). A server only some dispatch profile needs
therefore stays in the template and is left out of the interactive render (#2319).
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
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
    [
        "{not json",
        "[1, 2]",
        '{"mcpServers": []}',
        '{"mcpServers": "x"}',
        "\udcff",
        '{"mcpServers": {"mine": {"command": "/m"}}, "x": NaN}',
        '{"mcpServers": {}, "x": -Infinity}',
    ],
    ids=["invalid", "array", "servers-array", "servers-string", "undecodable", "nan", "infinity"],
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
    assert mod.render_mcp_config(root, dry_run=False) is None
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


def _drifted(mod, root: Path, extra: dict) -> Path:
    current = _render(mod, root)
    current["mcpServers"]["genesis-health"]["command"] = "/old/path"
    current["mcpServers"].update(extra)
    path = root / ".mcp.json"
    path.write_text(json.dumps(current, indent=2) + "\n")
    return path


def test_a_server_name_with_a_lone_surrogate_does_not_abort_the_render(
    tmp_path, _scratch_home, capsys, monkeypatch
):
    """JSON (and Node's JSON.parse) accept "\\ud800"; printing it raw raised
    UnicodeEncodeError before the backup, aborting bootstrap (#3284 review)."""
    mod = _module()
    root = _root(tmp_path)
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GENESIS_DEPLOY_SUMMARY", str(summary))
    path = _drifted(mod, root, {"\ud800odd": {"command": "/m"}})
    assert mod.render_mcp_config(root, dry_run=False) is True
    servers = json.loads(path.read_text())["mcpServers"]
    assert "\ud800odd" in servers
    assert servers["genesis-health"]["command"] != "/old/path"
    out = capsys.readouterr().out
    assert "\\ud800odd" in out
    assert "\\ud800odd" in summary.read_text()


def test_mcp_only_fails_when_the_render_could_not_be_written(tmp_path):
    """A stale file left in place because no backup could be made is not a
    successful render (#3284 review)."""
    (tmp_path / "config").mkdir()
    shutil.copyfile(TEMPLATE, tmp_path / "config" / "mcp.json.template")
    stale = '{"mcpServers": {"genesis-health": {"command": "/old"}}}\n'
    (tmp_path / ".mcp.json").write_text(stale)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--mcp-only", "--genesis-root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GENESIS_MU_BACKUP_DIR": str(blocker / "sub")},
    )
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert (tmp_path / ".mcp.json").read_text() == stale


def _secondary_group() -> int | None:
    others = [g for g in os.getgroups() if g != os.getgid()]
    return others[0] if others else None


def test_the_files_group_is_kept(tmp_path, _scratch_home):
    gid = _secondary_group()
    if gid is None:
        pytest.skip("this user belongs to no second group")
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    os.chown(path, -1, gid)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert path.stat().st_gid == gid


def test_a_group_that_cannot_be_kept_leaves_the_file_unchanged(
    tmp_path, _scratch_home, monkeypatch
):
    gid = _secondary_group()
    if gid is None:
        pytest.skip("this user belongs to no second group")
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    os.chown(path, -1, gid)
    before = path.read_bytes()
    monkeypatch.setattr(
        mod.os, "chown", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no"))
    )
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == before and path.stat().st_gid == gid
    assert not list(root.glob(".mcp.json.*")), "temp file left behind"


def _acl_bytes(named_uid: int = 65534) -> bytes:
    import struct

    u = 0xFFFFFFFF
    entries = [(1, 6, u), (2, 4, named_uid), (4, 4, u), (0x10, 4, u), (0x20, 4, u)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)


def _with_acl(path: Path) -> None:
    try:
        os.setxattr(path, "system.posix_acl_access", _acl_bytes())
    except (OSError, AttributeError):
        pytest.skip("no POSIX ACL support here")


def test_a_posix_acl_is_kept(tmp_path, _scratch_home):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    _with_acl(path)
    acl = os.getxattr(path, "system.posix_acl_access")
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert os.getxattr(path, "system.posix_acl_access") == acl


def test_an_acl_that_cannot_be_carried_leaves_the_file_unchanged(
    tmp_path, _scratch_home, monkeypatch
):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    _with_acl(path)
    before = path.read_bytes()
    monkeypatch.setattr(mod.shutil, "copystat", lambda src, dst: os.chmod(dst, 0o600))
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == before
    assert not list(root.glob(".mcp.json.*")), "temp file left behind"


def _bootstrap_run(
    root: Path, tmp_path: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """The script on the command line, bounded so a hang fails, not blocks."""
    env = {
        **os.environ,
        "GENESIS_DEPLOY_SUMMARY": str(tmp_path / "summary.txt"),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--mcp-only", "--genesis-root", str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize("kind", ["fifo", "directory", "dangling-symlink", "unreadable"])
def test_a_file_it_cannot_use_is_reported_never_a_crash_or_a_hang(tmp_path, _scratch_home, kind):
    """bootstrap runs this under set -e: a traceback would abort it, and a FIFO
    used to block the read forever (#3284 class audit)."""
    root = _root(tmp_path)
    path = root / ".mcp.json"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "dangling-symlink":
        path.symlink_to(tmp_path / "missing-dir" / "mcp.json")
    else:
        path.write_text('{"mcpServers": {}}')
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
    try:
        r = _bootstrap_run(root, tmp_path)
    finally:
        if kind == "unreadable":
            path.chmod(0o600)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "left unchanged" in r.stdout
    assert "mcp.json NOT updated" in (tmp_path / "summary.txt").read_text()
    reason = {
        "fifo": "not a regular file",
        "directory": "not a regular file",
        "dangling-symlink": "symlink to a missing file",
        "unreadable": "cannot read it",
    }[kind]
    assert reason in r.stdout, r.stdout


def test_a_directory_it_cannot_write_into_is_reported(tmp_path, _scratch_home):
    mod = _module()
    root = _root(tmp_path)
    path = root / ".mcp.json"
    path.write_text('{"mcpServers": {"mine": {"command": "/m"}}}')
    before = path.read_bytes()
    root.chmod(0o555)
    try:
        if os.access(root, os.W_OK):
            pytest.skip("running with privileges that ignore directory modes")
        assert mod.render_mcp_config(root, dry_run=False) is None
    finally:
        root.chmod(0o755)
    assert path.read_bytes() == before


def test_a_write_landing_after_the_read_is_not_overwritten(tmp_path, _scratch_home, monkeypatch):
    """Compare-and-swap: a `claude mcp add` landing between the read and the
    replace would otherwise be lost (#3284 class audit)."""
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    theirs = '{"mcpServers": {"added-meanwhile": {"command": "/x"}}}\n'
    real_copystat = mod.shutil.copystat

    def copystat_then_someone_writes(src, dst):
        real_copystat(src, dst)
        Path(src).write_text(theirs)

    monkeypatch.setattr(mod.shutil, "copystat", copystat_then_someone_writes)
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_text() == theirs
    assert not list(root.glob(".mcp.json.*")), "temp file left behind"


def test_absurdly_deep_json_is_treated_as_unreadable_not_a_crash(tmp_path, _scratch_home):
    mod = _module()
    root = _root(tmp_path)
    (root / ".mcp.json").write_text("[" * 100_000 + "]" * 100_000)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert "genesis-health" in json.loads((root / ".mcp.json").read_text())["mcpServers"]


@pytest.mark.parametrize("control", [*range(32), 127])
def test_summary_names_escape_all_ascii_controls(
    tmp_path, _scratch_home, monkeypatch, capsys, control
):
    mod = _module()
    root = _root(tmp_path)
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GENESIS_DEPLOY_SUMMARY", str(summary))
    name = "custom" + chr(control) + "server"
    path = _drifted(mod, root, {name: {"command": "/custom"}})
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert name in json.loads(path.read_text())["mcpServers"]
    line = summary.read_text()
    assert line.endswith("\n") and line.count("\n") == 1
    assert all(c.isprintable() for c in line[:-1])
    assert chr(control) not in capsys.readouterr().out.replace("\n", "")


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unrepresentable_json_numbers_decline_before_backup(
    tmp_path, _scratch_home, monkeypatch, number, dry_run
):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {"mine": {"command": "/custom"}})
    data = json.loads(path.read_text())
    data["custom-number"] = "overflow-placeholder"
    before = json.dumps(data).replace('"overflow-placeholder"', number).encode()
    path.write_bytes(before)
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GENESIS_DEPLOY_SUMMARY", str(summary))
    assert mod.render_mcp_config(root, dry_run=dry_run) is None
    assert path.read_bytes() == before
    assert not (_scratch_home / "deploy-backups").exists()
    assert "mcp.json NOT updated" in summary.read_text()


@pytest.mark.parametrize("dry_run", [False, True])
def test_first_creation_summary_matches_whether_it_wrote(
    tmp_path, _scratch_home, monkeypatch, dry_run
):
    mod = _module()
    root = _root(tmp_path)
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GENESIS_DEPLOY_SUMMARY", str(summary))
    assert mod.render_mcp_config(root, dry_run=dry_run) is True
    assert (root / ".mcp.json").exists() is not dry_run
    if dry_run:
        assert not summary.exists()
    else:
        assert summary.read_text() == "mcp.json created\n"
    assert not (_scratch_home / "deploy-backups").exists()


@pytest.mark.parametrize("stage", ["before-writer", "during-staging"])
def test_initial_absence_does_not_overwrite_a_concurrent_creation(
    tmp_path, _scratch_home, monkeypatch, stage
):
    mod = _module()
    root = _root(tmp_path)
    path = root / ".mcp.json"
    theirs = b'{"mcpServers":{"custom":{"command":"/custom"}}}\n'
    if stage == "before-writer":
        writer = mod._write_keeping_mode

        def create_then_write(*args, **kwargs):
            path.write_bytes(theirs)
            return writer(*args, **kwargs)

        monkeypatch.setattr(mod, "_write_keeping_mode", create_then_write)
    else:
        mkstemp = mod.tempfile.mkstemp

        def create_during_staging(*args, **kwargs):
            path.write_bytes(theirs)
            return mkstemp(*args, **kwargs)

        monkeypatch.setattr(mod.tempfile, "mkstemp", create_during_staging)
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == theirs
    assert not (_scratch_home / "deploy-backups").exists()
    assert not list(root.glob(".mcp.json.*"))


def test_a_present_but_different_inherited_acl_does_not_count_as_preserved(
    tmp_path, _scratch_home, monkeypatch
):
    mod = _module()
    root = _root(tmp_path)
    try:
        os.setxattr(root, "system.posix_acl_default", _acl_bytes(65533))
    except OSError:
        pytest.skip("no POSIX default ACL support")
    path = _drifted(mod, root, {})
    _with_acl(path)
    before, acl = path.read_bytes(), os.getxattr(path, mod._ACL)
    setxattr = os.setxattr

    def denied_copy(destination, name, value, *args, **kwargs):
        if name == mod._ACL and Path(destination) != path:
            raise PermissionError(errno.EPERM, "synthetic ACL copy refusal")
        return setxattr(destination, name, value, *args, **kwargs)

    monkeypatch.setattr(mod.os, "setxattr", denied_copy)
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == before and os.getxattr(path, mod._ACL) == acl
    assert not list(root.glob(".mcp.json.*"))


def test_acl_verification_checks_the_final_metadata(tmp_path, _scratch_home, monkeypatch):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    _with_acl(path)
    before, acl = path.read_bytes(), os.getxattr(path, mod._ACL)
    chown = os.chown

    def changed_after_chown(destination, *args, **kwargs):
        chown(destination, *args, **kwargs)
        os.setxattr(destination, mod._ACL, _acl_bytes(65533))

    monkeypatch.setattr(mod.os, "chown", changed_after_chown)
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == before and os.getxattr(path, mod._ACL) == acl
    assert not list(root.glob(".mcp.json.*"))


@pytest.mark.parametrize("target", ["original", "temporary"])
def test_unreadable_acl_metadata_declines_instead_of_claiming_success(
    tmp_path, _scratch_home, monkeypatch, target
):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    _with_acl(path)
    before = path.read_bytes()
    getxattr = os.getxattr

    def denied_read(filename, name, *args, **kwargs):
        if name == mod._ACL and (Path(filename) == path) == (target == "original"):
            raise PermissionError(errno.EACCES, "synthetic ACL read refusal")
        return getxattr(filename, name, *args, **kwargs)

    monkeypatch.setattr(mod.os, "getxattr", denied_read)
    assert mod.render_mcp_config(root, dry_run=False) is None
    assert path.read_bytes() == before
    assert not list(root.glob(".mcp.json.*"))


@pytest.mark.parametrize("original_acl", [False, True])
@pytest.mark.parametrize("default_acl", [False, True])
@pytest.mark.parametrize("mode", [0o440, 0o640])
def test_existing_metadata_is_qualified_before_staging_payload(
    tmp_path, _scratch_home, monkeypatch, original_acl, default_acl, mode
):
    mod = _module()
    root = _root(tmp_path)
    path = _drifted(mod, root, {})
    if original_acl:
        _with_acl(path)
    path.chmod(mode)
    wanted_acl = mod._access_acl(path)
    if default_acl:
        try:
            os.setxattr(root, "system.posix_acl_default", _acl_bytes(65533))
        except OSError:
            pytest.skip("no POSIX default ACL support")
    copied = []
    copystat = mod.shutil.copystat

    def observe_metadata(source, destination):
        # Metadata copying can enable inherited readers before its final check.
        # There must be no configuration payload at that transition.
        copied.append(Path(destination).read_bytes())
        assert copied[-1] == b""
        return copystat(source, destination)

    monkeypatch.setattr(mod.shutil, "copystat", observe_metadata)
    assert mod.render_mcp_config(root, dry_run=False) is True
    assert copied == [b""]
    assert mod._access_acl(path) == wanted_acl
    assert path.stat().st_mode & 0o777 == mode
    assert json.loads(path.read_text())["mcpServers"]["genesis-health"]["command"] != "/old/path"
    assert not list(root.glob(".mcp.json.*"))


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_hard_linked_configs_keep_every_alias(tmp_path, _scratch_home, capsys, symlink, changed):
    mod = _module()
    root = _root(tmp_path)
    path = root / ".mcp.json"
    if changed:
        _drifted(mod, root, {})
    else:
        path.write_text(json.dumps(_render(mod, root)))
    target = path
    if symlink:
        target = root / "config-target.json"
        path.rename(target)
        path.symlink_to(target.name)
    alias = root / "linked-config.json"
    os.link(target, alias)
    before, inode = target.read_bytes(), target.stat().st_ino
    assert mod.render_mcp_config(root, dry_run=False) is (None if changed else False)
    assert target.read_bytes() == alias.read_bytes() == before
    assert target.stat().st_ino == alias.stat().st_ino == inode
    assert target.stat().st_nlink == 2
    assert path.is_symlink() == symlink
    if changed:
        assert "multiple hard links" in capsys.readouterr().out
    assert not list(root.glob(".mcp.json.*"))
