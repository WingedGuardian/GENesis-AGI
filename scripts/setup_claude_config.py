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
import sys
from pathlib import Path


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
    """Trigger code intelligence indexing in background (non-blocking).

    Uses start_new_session=True + explicit log file to avoid inheriting
    open pipes from the caller (e.g. bootstrap's `| tail -10` pipe),
    which would cause SIGPIPE if the parent exits before the indexer finishes.
    """
    import shutil
    import subprocess

    if dry_run:
        print("\nCode intelligence: (dry run — skipping indexing)")
        return

    log_path = Path.home() / ".genesis" / "code-intelligence-setup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime

    launched = []
    if shutil.which("codebase-memory-mcp"):
        subprocess.Popen(
            ["codebase-memory-mcp", "cli", "index_repository",
             f'{{"repo_path": "{genesis_root}"}}'],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
        launched.append("codebase-memory-mcp")

    if shutil.which("npx"):
        subprocess.Popen(
            ["npx", "gitnexus", "analyze", "--quiet"],
            cwd=str(genesis_root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
        launched.append("GitNexus")

    if launched:
        print(f"\nCode intelligence: indexing in background ({', '.join(launched)})")
        print(f"  Log: {log_path}")
    else:
        print("\nCode intelligence: no indexers found (install codebase-memory-mcp to enable)")


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
