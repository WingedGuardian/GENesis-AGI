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
import json
import os
import sys
from pathlib import Path

# Distinguishes "key absent" from "key present and falsy" (0, "", False).
_MISSING = object()


def find_genesis_root() -> Path:
    """Find genesis root from this script's location."""
    return Path(__file__).resolve().parent.parent


def render_mcp_config(genesis_root: Path, dry_run: bool) -> bool:
    """Render .mcp.json from template. Returns True if changes were made."""
    template_path = genesis_root / "config" / "mcp.json.template"
    output_path = genesis_root / ".mcp.json"

    if not template_path.exists():
        print(f"ERROR: MCP template not found at {template_path}", file=sys.stderr)
        return False

    template = template_path.read_text()
    rendered = template.replace("{{GENESIS_ROOT}}", str(genesis_root))

    if output_path.exists():
        current = output_path.read_text()
        if current == rendered:
            print(f".mcp.json: already correct ({genesis_root})")
            return False

    print(f".mcp.json: rendering with GENESIS_ROOT={genesis_root}")
    if not dry_run:
        output_path.write_text(rendered)
        print("  Written.")
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


# Key in cc-global-settings.yaml holding the settings that configure the CC
# CLIENT rather than a project, with a `policy` per entry. See that file's own
# header — it is the contract, this is just the applier.
_USER_LEVEL_SECTION = "user_level_defaults"


def _write_json(path: Path, data: dict) -> None:
    """Write JSON by truncating the file IN PLACE. Deliberately not atomic.

    An earlier revision of this used temp-file + rename. Don't reintroduce it
    without reading this: `os.replace` installs a NEW inode, and that silently
    discards three properties of the target that matter more here than
    atomicity does.

      * mode — this file routinely holds an `env` block with API keys, and the
        rename reset 0600 to the umask default on every run;
      * ownership — under sudo the new inode is root's, leaving the operator's
        CC unable to write its own settings;
      * symlink-ness — a dotfiles-managed settings.json is commonly a symlink.
        The rename replaced the LINK with a regular file, so the managed copy
        silently kept the old value and the next `stow`/`chezmoi` restored it,
        reintroducing whatever the setting was fixing. No signal, no error.

    An in-place truncate keeps all three for free, because the inode never
    changes. What it gives up is a torn file if the process is killed between
    truncate and write — a narrow window on a small file, and the behaviour
    this script had for years. That trade is the right way round: a torn file
    is loud and fixable, a silently-forked source of truth is neither.
    """
    path.write_text(json.dumps(data, indent=2) + "\n")


def _load_user_settings(path: Path) -> dict | None:
    """Existing user settings, or None if unreadable/not an object."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    # ValueError covers JSONDecodeError AND UnicodeDecodeError — a settings file
    # that is binary-corrupt must warn, not traceback out of a deploy step.
    except (OSError, ValueError) as exc:
        print(f"WARNING: could not read {path}: {exc}")
        return None
    if not isinstance(data, dict):
        print(f"WARNING: {path} is not a JSON object — leaving it alone.")
        return None
    return data


def _target_for(key: str, entry: dict) -> tuple[int | None, bool]:
    """`(value, operator_forced)` for a `policy: floor` entry.

    When the entry's `env_override` is set to a usable integer, the operator has
    named an exact value and it is AUTHORITATIVE — it sets rather than floors, so
    `GENESIS_CC_RETENTION_DAYS=30` genuinely yields 30 even against a higher
    existing value. That is what the docs promise; treating it as merely a lower
    floor would leave a documented lever that silently does nothing.

    Values below 1 are refused: CC's own schema rejects `cleanupPeriodDays < 1`,
    and a negative would put the sweep's cutoff in the FUTURE — i.e. older-than-
    everything. Refusing to write it is the fail-closed direction on the one key
    this whole module exists to keep safe.
    """
    value = entry.get("value")
    default = value if isinstance(value, int) and not isinstance(value, bool) else None
    name = entry.get("env_override")
    raw = os.environ.get(name) if name else None
    if raw is None or raw == "":
        return default, False
    try:
        parsed = int(raw)
    except ValueError:
        print(f"WARNING: {name}={raw!r} is not an integer — using {default}.")
        return default, False
    if parsed < 1:
        print(f"WARNING: {name}={raw!r} is below 1 — refusing it, using {default}.")
        return default, False
    return parsed, True


def _apply_user_level_defaults(settings: dict, section: dict) -> list[str]:
    """Apply each manifest entry to `settings` in place. Returns change lines."""
    changes: list[str] = []
    for key, entry in sorted(section.items()):
        if not isinstance(entry, dict) or "policy" not in entry:
            print(f"WARNING: {key} in the manifest has no policy — skipped.")
            continue
        policy, have = entry["policy"], settings.get(key, _MISSING)

        if policy == "set_if_absent":
            # An entry with no `value` would otherwise write a null into every
            # install's CC settings; refuse rather than propagate a manifest typo.
            if "value" not in entry:
                print(f"WARNING: {key} is set_if_absent but declares no value — skipped.")
            elif have is _MISSING:
                settings[key] = entry["value"]
                changes.append(f"  {key}: <unset> -> {entry['value']}")

        elif policy == "floor":
            target, forced = _target_for(key, entry)
            if target is None:
                print(f"WARNING: {key} has no usable integer value — skipped.")
            elif forced:
                if have != target:
                    settings[key] = target
                    changes.append(f"  {key}: {'<unset>' if have is _MISSING else have}"
                                   f" -> {target} (operator override)")
            # An explicit `null` must heal exactly like an absent key. CC resolves
            # the value with `?? Y` (nullish-coalescing, Y=30), so `null` IS the
            # 30-day default — the precise state this module exists to prevent.
            # Leaving it alone would fail OPEN on the one key that matters, and
            # would disagree with the two shell writers, which use `.get()` and
            # heal it. MEASURED before the fix: shells -> 180, this path -> null.
            elif have is _MISSING or have is None:
                settings[key] = target
                changes.append(
                    f"  {key}: {'<unset>' if have is _MISSING else 'null'} -> {target}"
                )
            elif not isinstance(have, int) or isinstance(have, bool):
                print(f"WARNING: {key} is {have!r}, not an integer — left alone.")
            elif have < target:
                settings[key] = target
                changes.append(f"  {key}: {have} -> {target} (raised to Genesis floor)")

        else:
            print(f"WARNING: {key} has unknown policy {policy!r} — skipped.")
    return changes


def ensure_user_cc_defaults(genesis_root: Path, dry_run: bool) -> None:
    """Apply the manifest's user_level_defaults to ~/.claude/settings.json.

    Runs unconditionally, NOT behind --global: --global force-sets `model` and
    `effortLevel`, so running it on every update would stomp an operator's model
    choice. This path only ever raises a floor or fills an absent key, which is
    safe on every bootstrap — and bootstrap is what update.sh re-runs, so it is
    the route that reaches installs that already exist.

    NEVER raises. bootstrap.sh calls this script bare under `set -euo pipefail`
    from update.sh, which runs `set -Eeuo pipefail` with an armed ERR trap — so
    an exception here would escalate into a full update rollback, reported as a
    JSON error rather than a settings problem.
    """
    try:
        _ensure_user_cc_defaults(genesis_root, dry_run)
    except Exception as exc:  # noqa: BLE001 — never fail a deploy over settings
        print(f"WARNING: could not apply user-level CC defaults: {exc}")
        print("  Continuing; re-run scripts/setup_claude_config.py after fixing.")


def _ensure_user_cc_defaults(genesis_root: Path, dry_run: bool) -> None:
    try:
        import yaml
    except ImportError:
        print("WARNING: pyyaml unavailable — user-level CC defaults NOT applied.")
        return

    manifest_path = genesis_root / "config" / "cc-global-settings.yaml"
    settings_path = Path.home() / ".claude" / "settings.json"

    if not manifest_path.exists():
        print(f"WARNING: settings manifest not found: {manifest_path}")
        print("  User-level CC defaults NOT applied.")
        return

    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    section = manifest.get(_USER_LEVEL_SECTION) or {}
    if not isinstance(section, dict) or not section:
        print(f"WARNING: {manifest_path.name} has no '{_USER_LEVEL_SECTION}' section.")
        return

    current = _load_user_settings(settings_path)
    if current is None:
        print("  User-level CC defaults NOT applied (fix the file, then re-run).")
        return

    changes = _apply_user_level_defaults(current, section)
    if not changes:
        # Print the live values, not just "ok". This runs on every update, so it
        # is the one recurring receipt that the settings are actually in force —
        # the original bug was a value that looked right and did nothing, and a
        # bare "already satisfied" would have printed happily throughout.
        live = {k: current.get(k) for k in sorted(section)}
        print(f"~/.claude/settings.json: user-level CC defaults in force: {live}")
        print("  (user-level only — CC exposes no resolver for the effective value)")
        return

    print("~/.claude/settings.json: applying user-level CC defaults:")
    for line in changes:
        print(line)
    if dry_run:
        print("  (dry run — not written)")
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(settings_path, current)
    print("  Written.")


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
    current = _load_user_settings(global_settings_path)
    if current is None:
        print("  Global settings NOT applied (fix the file, then re-run).")
        return

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
            global_settings_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(global_settings_path, current)
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
    parser.add_argument("--global", dest="do_global", action="store_true",
                        help="Also configure ~/.claude/settings.json from manifest")
    args = parser.parse_args()

    genesis_root = (args.genesis_root or find_genesis_root()).resolve()

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

    # User-level CC defaults that cannot live in the repo's project settings.
    # Unconditional (not behind --global) so update.sh -> bootstrap.sh carries
    # them to installs that already exist; set-if-absent, so it never overwrites.
    print()
    ensure_user_cc_defaults(genesis_root, args.dry_run)

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
