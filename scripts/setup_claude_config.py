#!/usr/bin/env python3
"""
Genesis Claude Code config setup script.

Renders .mcp.json from the template with machine-specific paths.
.claude/settings.json is now tracked in git directly (uses $CLAUDE_PROJECT_DIR,
no machine-specific content), so it no longer needs rendering.

Usage:
    python scripts/setup_claude_config.py              # Set up .mcp.json
    python scripts/setup_claude_config.py --global     # Also configure ~/.claude/settings.json
    python scripts/setup_claude_config.py --dry-run    # Preview changes
"""

import argparse
import contextlib
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def find_genesis_root() -> Path:
    """Find genesis root from this script's location."""
    return Path(__file__).resolve().parent.parent


# Servers in config/mcp.json.template that INTERACTIVE sessions do not get.
# The template is the single source for two consumers: this render, which
# writes .mcp.json (what interactive sessions and the server's conversation-
# channel sessions load: both run in the checkout; dispatched sessions run
# outside it and never read it), and src/genesis/cc/session_config.py, which renders it per
# dispatch profile and keeps only the servers that profile names. That filter
# can only remove servers, never add them, so a server only some dispatch
# profile needs must stay in the template and be left out HERE.
#   discord-bot: only the community-responder profile uses it (#2319). Listed in
#   .mcp.json, every interactive session spawned it for nothing: its tools are
#   on-demand HTTP, so the cost was one idle process per session (measured: 8 of
#   40 MCP processes on one install).
# tests/test_scripts/test_setup_claude_config.py checks each entry is still in
# the template and still named by a dispatch profile.
INTERACTIVE_EXCLUDED_SERVERS = frozenset({"discord-bot"})


def render_mcp_text(template: str, genesis_root: Path) -> str:
    """The .mcp.json content for <genesis_root>: the template with its root
    substituted and the interactive-excluded servers removed."""
    config = json.loads(template.replace("{{GENESIS_ROOT}}", str(genesis_root)))
    servers = config.get("mcpServers", {})
    config["mcpServers"] = {
        name: spec for name, spec in servers.items() if name not in INTERACTIVE_EXCLUDED_SERVERS
    }
    return json.dumps(config, indent=2) + "\n"


# Servers an earlier template defined and the current one does not. Genesis owns
# them, so the merge below removes them from an existing .mcp.json; without this
# list they would read as the user's own and stay forever. When you remove a
# server from config/mcp.json.template, add its name here.
RETIRED_SERVERS: frozenset[str] = frozenset()


def _deploy_backup_dir() -> Path:
    """One directory per run under ~/.genesis/deploy-backups (GENESIS_HOME-aware,
    like genesis.env.genesis_home; this script is stdlib-only), the directory a
    deploy's other backups go to (GENESIS_MU_BACKUP_DIR when a deploy set one).
    A ~1 KB file, written only when .mcp.json changes; disk_hygiene.sh prunes the
    directory once its deploy-backups retention lands (#3138). Not next to
    .mcp.json: that would leave a stray file in the checkout."""
    if os.environ.get("GENESIS_MU_BACKUP_DIR"):
        return Path(os.environ["GENESIS_MU_BACKUP_DIR"])
    home = os.environ.get("GENESIS_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".genesis"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base / "deploy-backups" / stamp


def _save_backup(data: bytes) -> Path:
    """Write <data> into the backup directory without replacing an earlier backup
    (two checkouts, or two runs in one second, share a directory name). 0600:
    a user's own server entry may carry an env block with a key."""
    backup_dir = _deploy_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in range(100):
        path = backup_dir / (".mcp.json" if n == 0 else f".mcp.json.{n}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    raise OSError(f"no free backup name in {backup_dir}")


def _shown(text: str) -> str:
    """Printable on any terminal: a lone surrogate or other non-ASCII text in a
    server name (or a path) is shown escaped instead of crashing the print."""
    return json.dumps(text, ensure_ascii=True)[1:-1]


def _summary(line: str) -> None:
    # update.sh hides bootstrap's output behind `tail -10`; a deploy that wants
    # the change named passes a summary file. update.sh sets it from #3138 on;
    # before that this is a no-op. No line may start "kept ": that bootstrap
    # counts those lines as kept systemd units.
    path = os.environ.get("GENESIS_DEPLOY_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except (OSError, UnicodeError):
            pass


_ACL = "system.posix_acl_access"
_NO_EXPECTATION = object()


def _access_acl(path: str | Path) -> bytes | None:
    """An absent/unsupported ACL is distinct from metadata we cannot read."""
    try:
        return os.getxattr(path, _ACL)
    except OSError as exc:
        if exc.errno in {errno.ENODATA, errno.ENOTSUP}:
            return None
        raise


def _write_keeping_mode(
    path: Path, text: str, expected: bytes | None | object = _NO_EXPECTATION
) -> str | None:
    """Write through a symlink to its target, atomically (same-dir temp file,
    then rename), keeping the old file's mode, owner, group, ACL and xattrs.
    Returns None once written, or why it declined (nothing replaced): a
    replacement that could not carry the old file's group or ACL would change
    who can read a file that may hold API keys. With ``expected``, it also
    declines when the file no longer holds those bytes: someone wrote it
    after it was read, and replacing it would lose that write. None means the
    caller observed absence; omitting expected makes no concurrency check.
    It also declines when the target's mode, owner, group, ACL, link count or
    identity changed after they were captured, since the replacement would
    publish the stale copy. These are best-effort checks, not atomic CAS: a
    change landing between the final re-stat and the rename still goes
    unnoticed."""
    target = Path(os.path.realpath(path))
    before = target.stat() if target.exists() else None
    if before is not None and before.st_nlink > 1:
        return "it has multiple hard links; atomic replacement would separate their contents"
    acl = _access_acl(target) if before is not None else None
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".mcp.json.")

    def metadata_refusal() -> str | None:
        if before is None:
            return None
        now = os.stat(tmp)
        if (now.st_uid, now.st_gid) != (before.st_uid, before.st_gid):
            return f"its owner/group ({before.st_uid}:{before.st_gid}) could not be kept"
        if _access_acl(tmp) != acl:
            return "its POSIX ACL could not be carried to the replacement"
        return None

    def target_metadata_refusal() -> str | None:
        # The temp carries a copy of the target's metadata as first captured; a
        # chmod, chown, setfacl or new hard link since then would be undone (or
        # the link split) by publishing it.
        try:
            now = os.stat(target)
        except FileNotFoundError:
            return "it changed while the merge ran (run bootstrap again)"
        fields = ("st_mode", "st_uid", "st_gid", "st_nlink", "st_ino", "st_dev")
        if any(getattr(now, f) != getattr(before, f) for f in fields) or (
            _access_acl(target) != acl
        ):
            return "its permissions, owner, ACL or links changed while the merge ran (run bootstrap again)"
        return None

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if before is not None:
                # Qualify readers while the temp is EMPTY. copystat/chown can
                # change effective ACL/mode access, so payload must come later.
                if acl is None and _access_acl(tmp) is not None:
                    os.removexattr(tmp, _ACL)
                try:
                    shutil.copystat(target, tmp)
                except OSError:
                    os.chmod(tmp, before.st_mode & 0o777)
                os.utime(tmp, None)
                with contextlib.suppress(OSError):  # judged on the result below
                    os.chown(tmp, before.st_uid, before.st_gid)
                refusal = metadata_refusal()
                if refusal:
                    return refusal
            # The FD was opened before metadata preparation and remains writable
            # even when the preserved target mode is read-only.
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if before is None:
            os.chmod(tmp, 0o666 & ~_umask())
        else:
            refusal = metadata_refusal()
            if refusal:
                return refusal
        changed = (
            target.exists() or target.is_symlink()
            if expected is None
            else expected is not _NO_EXPECTATION and target.read_bytes() != expected
        )
        if changed:
            return "it changed while the merge ran (run bootstrap again)"
        if before is not None:
            refusal = target_metadata_refusal()
            if refusal:
                return refusal
        os.replace(tmp, target)
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _reject_constant(name: str) -> object:
    # Python's json accepts NaN/Infinity; Claude Code's parser (JSON.parse) does
    # not. A file holding one is unreadable to its real reader, so treat it as
    # unusable (backed up and replaced) rather than writing the constant back.
    raise ValueError(f"non-standard JSON constant {name}")


def merge_mcp(existing: object, rendered: dict, template_servers: set[str]) -> dict | None:
    """The merged .mcp.json, or None when ``existing`` is not usable as one.

    Genesis owns every server its template defines (including the ones the
    interactive render leaves out) and every retired one. Those are replaced by
    the render; every other server, and every other top-level key, is kept."""
    if not isinstance(existing, dict):
        return None
    existing_servers = existing.get("mcpServers") or {}  # absent or null: none yet
    if not isinstance(existing_servers, dict):
        return None
    owned = template_servers | INTERACTIVE_EXCLUDED_SERVERS | RETIRED_SERVERS
    servers = dict(rendered["mcpServers"])
    for name, spec in existing_servers.items():
        if name not in owned:
            servers[name] = spec
    merged = {k: v for k, v in existing.items() if k != "mcpServers"}
    merged["mcpServers"] = servers
    return merged


def _not_updated(reason: str) -> None:
    # Failure is reported where update.sh shows it, not only on stdout,
    # which bootstrap's `tail -10` hides.
    print(f"  WARNING: .mcp.json left unchanged: {reason}.")
    _summary(f"mcp.json NOT updated: {reason}")


def render_mcp_config(genesis_root: Path, dry_run: bool) -> bool | None:
    """Render .mcp.json from the template, keeping servers the template does not
    own (#2413). Returns True if the file changed (or would, on a dry run),
    False if it was already correct, None if it should have changed and could
    not (no template, a .mcp.json that is not a regular file or cannot be read
    or written, no backup, or a replacement that could not keep the old
    file's owner or ACL). It never raises over the file: bootstrap runs it
    under set -e."""
    template_path = genesis_root / "config" / "mcp.json.template"
    output_path = genesis_root / ".mcp.json"

    if not template_path.exists():
        print(f"ERROR: MCP template not found at {_shown(str(template_path))}", file=sys.stderr)
        return None

    template_text = template_path.read_text()
    rendered = json.loads(render_mcp_text(template_text, genesis_root))
    template_servers = set(json.loads(template_text).get("mcpServers", {}))

    try:
        st = os.stat(output_path)  # follows a symlink to the file it names
    except FileNotFoundError:
        st = None
    except OSError as exc:
        _not_updated(f"cannot stat it ({_shown(str(exc))})")
        return None
    if st is None and output_path.is_symlink():
        _not_updated("it is a symlink to a missing file")
        return None
    if st is not None and not stat.S_ISREG(st.st_mode):
        # A FIFO would block the read forever; a directory cannot be parsed.
        _not_updated("it is not a regular file")
        return None
    try:
        old_bytes = output_path.read_bytes() if st is not None else None
    except OSError as exc:
        _not_updated(f"cannot read it ({_shown(str(exc))})")
        return None
    if old_bytes is None:
        new, why = rendered, "created"
    else:
        try:
            existing = json.loads(old_bytes.decode("utf-8"), parse_constant=_reject_constant)
        except (
            ValueError,
            RecursionError,
        ):  # JSONDecodeError, UnicodeDecodeError, NaN/Infinity, absurd nesting
            existing = None
        merged = merge_mcp(existing, rendered, template_servers)
        # Compared as objects: key order and formatting are not changes (serena's
        # registration writes the same JSON without a trailing newline).
        if merged is not None and merged == existing:
            print(f".mcp.json: already correct ({_shown(str(genesis_root))})")
            return False
        if merged is None:
            new, why = rendered, "replaced an unreadable file with the template's servers"
        else:
            new = merged
            old_servers = existing.get("mcpServers") or {}
            updated = sorted(
                n
                for n in set(old_servers) & set(rendered["mcpServers"])
                if old_servers[n] != rendered["mcpServers"][n]
            )
            added = sorted(set(rendered["mcpServers"]) - set(old_servers))
            removed = sorted(set(old_servers) - set(merged["mcpServers"]))
            kept = sorted(set(merged["mcpServers"]) - set(rendered["mcpServers"]))
            parts = [
                f"{label} {', '.join(_shown(n) for n in names)}"
                for label, names in (
                    ("updated", updated),
                    ("added", added),
                    ("removed", removed),
                    ("kept", kept),
                )
                if names
            ]
            why = "; ".join(parts) or "rewrote the file's other keys"

    try:
        serialized = json.dumps(new, indent=2, allow_nan=False) + "\n"
    except (ValueError, RecursionError):
        _not_updated("the merged configuration cannot be represented safely as JSON")
        return None
    print(f".mcp.json: rendering with GENESIS_ROOT={_shown(str(genesis_root))} ({why})")
    if dry_run:
        return True
    note = ""
    if old_bytes is not None:
        try:
            saved = _save_backup(old_bytes)
        except OSError as exc:
            # No rewrite without a backup, but never abort bootstrap over it.
            _not_updated(f"could not back it up ({_shown(str(exc))})")
            return None
        note = f"; previous file saved as {_shown(str(saved))}"
    try:
        declined = _write_keeping_mode(output_path, serialized, expected=old_bytes)
    except OSError as exc:
        declined = f"cannot write it ({_shown(str(exc))})"
    if declined is not None:
        _not_updated(f"{declined}{note}")
        return None
    print(f"  Written{note}.")
    _summary(f"mcp.json {why}{note}")
    return True


def check_launcher_executable(genesis_root: Path) -> None:
    """Verify hook and MCP launchers are executable."""
    for launcher in [
        genesis_root / ".claude" / "hooks" / "genesis-hook",
        genesis_root / ".claude" / "mcp" / "run-mcp-server",
        genesis_root / ".claude" / "mcp" / "run-codebase-memory",
    ]:
        if not launcher.exists():
            print(f"WARNING: Launcher not found: {launcher}")
        elif not launcher.stat().st_mode & 0o111:
            print(f"WARNING: Launcher not executable: {launcher}")
            print(f"  Fix: chmod +x {launcher}")
        else:
            print(f"  Launcher OK: {launcher.name}")


def check_venv(genesis_root: Path) -> None:
    """Verify Python venv exists."""
    python = genesis_root / ".venv" / "bin" / "python"
    if not python.exists():
        print(f"WARNING: Python venv not found at {python}")
        print(f"  Fix: cd {genesis_root} && python3 -m venv .venv && pip install -e .")
    else:
        print(f"  Venv OK: {python}")


def configure_global_settings(genesis_root: Path, dry_run: bool) -> None:
    """Configure ~/.claude/settings.json from the global settings manifest."""
    import yaml  # Only imported when --global is used

    manifest_path = genesis_root / "config" / "cc-global-settings.yaml"
    global_settings_path = Path.home() / ".claude" / "settings.json"

    if not manifest_path.exists():
        print(f"WARNING: Global settings manifest not found: {manifest_path}")
        print("  Skipping global settings configuration.")
        return

    manifest = yaml.safe_load(manifest_path.read_text())

    # Read existing global settings (preserve user overrides)
    current = json.loads(global_settings_path.read_text()) if global_settings_path.exists() else {}

    # Merge manifest values
    changes = []
    for key in ["model", "effortLevel"]:
        if key in manifest and current.get(key) != manifest[key]:
            changes.append(f"  {key}: {current.get(key, '<unset>')} -> {manifest[key]}")
            current[key] = manifest[key]

    for key in ["voiceEnabled", "autoDreamEnabled", "skipDangerousModePermissionPrompt"]:
        if key in manifest and current.get(key) != manifest[key]:
            changes.append(f"  {key}: {current.get(key, '<unset>')} -> {manifest[key]}")
            current[key] = manifest[key]

    if changes:
        print("~/.claude/settings.json: updating:")
        for c in changes:
            print(c)
        if not dry_run:
            global_settings_path.write_text(json.dumps(current, indent=2) + "\n")
            print("  Written.")
    else:
        print("~/.claude/settings.json: already matches manifest")

    # Check plugins
    strongly_recommended = manifest.get("plugins", {}).get("strongly_recommended", [])
    also_helpful = manifest.get("plugins", {}).get("also_helpful", [])

    skills_dir = Path.home() / ".claude" / "skills"
    plugins_dir = Path.home() / ".claude" / "plugins"

    def plugin_installed(name: str) -> bool:
        # Check skills and plugins directories
        for d in [skills_dir, plugins_dir]:
            if not d.exists():
                continue
            for item in d.rglob("*"):
                if item.is_dir() and item.name == name:
                    return True
        return False

    missing_critical = [p for p in strongly_recommended if not plugin_installed(p)]
    missing_helpful = [p for p in also_helpful if not plugin_installed(p)]

    if missing_critical:
        print(f"\n  Genesis strongly recommends these plugins: {', '.join(missing_critical)}")
        print("  Install via Claude Code plugin manager.")
    if missing_helpful:
        print(f"  These are also helpful to have: {', '.join(missing_helpful)}")


def trigger_indexing(genesis_root: Path, dry_run: bool) -> None:
    """Queue a code-intelligence index request (non-blocking, idle-gated).

    Writes an index-request marker (scripts/lib/index_marker.py); the
    genesis-code-intel.timer runner reindexes when the box is idle. We do NOT
    spawn an indexer here — fire-and-forget full-mode spawns at setup/commit
    helped storm the container (D-state I/O), and a guardrail test bans raw
    spawns. Worktrees are never indexed (Serena covers them live).
    """
    if dry_run:
        print("\nCode intelligence: (dry run — skipping index request)")
        return

    if (genesis_root / ".git").is_file():
        print("\nCode intelligence: worktree detected — no index request (use Serena here)")
        return

    marker_helper = genesis_root / "scripts" / "lib" / "index_marker.py"
    if not marker_helper.exists():
        print("\nCode intelligence: marker helper missing — skipping index request")
        return

    import sys as _sys

    lib = str((genesis_root / "scripts" / "lib").resolve())
    if lib not in _sys.path:
        _sys.path.insert(0, lib)
    try:
        import index_marker  # stdlib-only

        index_marker.write_marker(str(genesis_root), tools="both", mode="fast")
        print("\nCode intelligence: initial index queued (idle-gated runner)")
    except Exception as exc:  # noqa: BLE001 — never fail setup on a queue write
        print(f"\nCode intelligence: could not queue index request ({exc})")


def main():
    parser = argparse.ArgumentParser(description="Set up Claude Code config for this machine")
    parser.add_argument("--genesis-root", type=Path, help="Override genesis root path")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument(
        "--global",
        dest="do_global",
        action="store_true",
        help="Also configure ~/.claude/settings.json from manifest",
    )
    parser.add_argument(
        "--mcp-only", action="store_true", help="Only render .mcp.json (install.sh uses this)"
    )
    args = parser.parse_args()

    genesis_root = (args.genesis_root or find_genesis_root()).resolve()

    if args.mcp_only:
        if render_mcp_config(genesis_root, args.dry_run) is None:
            # It should have rendered and could not: a stale file on disk is
            # not a success.
            sys.exit(1)
        # A missing template renders nothing: say so in the exit status, not only
        # on stderr, so a caller can tell. Judge the template, not the output: a
        # .mcp.json left by an earlier render, or --dry-run, must not turn the
        # failure into a success.
        if not (genesis_root / "config" / "mcp.json.template").exists():
            sys.exit(1)
        sys.exit(0 if args.dry_run or (genesis_root / ".mcp.json").exists() else 1)

    print(f"Genesis root: {genesis_root}")
    print()

    # Verify prerequisites
    check_venv(genesis_root)
    check_launcher_executable(genesis_root)
    print()

    # Render .mcp.json from template
    render_mcp_config(genesis_root, args.dry_run)

    # settings.json is now tracked in git — no rendering needed
    settings_path = genesis_root / ".claude" / "settings.json"
    if settings_path.exists():
        content = settings_path.read_text()
        if "agent-zero" in content:
            print("\nWARNING: .claude/settings.json still references agent-zero!")
            print("  This file should use $CLAUDE_PROJECT_DIR. Check git status.")
        elif "/home/" in content and "CLAUDE_PROJECT_DIR" not in content:
            print("\nWARNING: .claude/settings.json has hardcoded paths.")
            print("  Pull latest from git to get the portable version.")
        else:
            print("\n.claude/settings.json: portable (uses $CLAUDE_PROJECT_DIR)")
    else:
        print("\nWARNING: .claude/settings.json not found!")
        print("  It should be tracked in git. Run: git checkout -- .claude/settings.json")

    # Copy settings.local.json template if missing
    local_settings = genesis_root / ".claude" / "settings.local.json"
    local_template = genesis_root / "config" / "settings.local.json.template"
    if not local_settings.exists() and local_template.exists():
        print("\nCopying settings.local.json template...")
        if not args.dry_run:
            local_settings.write_text(local_template.read_text())
            print("  Written.")
        else:
            print("  Would copy template to .claude/settings.local.json")

    # Global settings
    if args.do_global:
        print()
        configure_global_settings(genesis_root, args.dry_run)

    # Trigger code intelligence indexing (background, non-blocking)
    trigger_indexing(genesis_root, args.dry_run)

    if args.dry_run:
        print("\n(dry run — no files written)")
    else:
        print("\nSetup complete. Restart Claude Code to pick up changes.")


if __name__ == "__main__":
    main()
