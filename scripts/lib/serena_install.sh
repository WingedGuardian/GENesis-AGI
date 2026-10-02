# shellcheck shell=bash
# Keep automatic tool updates from invalidating an enabled shared-service pin.
_install_serena() (
    local sharing serena_lock_fd
    # Same per-user lock as configure; keep check and tool mutation together.
    if ! mkdir -p "$HOME/.config/systemd/user"; then
        echo "  WARNING: Serena lock directory unavailable; skipping install/upgrade"
        return 0
    fi
    if ! exec {serena_lock_fd}>>"$HOME/.config/systemd/user/.genesis-serena.lock"; then
        echo "  WARNING: Serena lock unavailable; skipping install/upgrade"
        return 0
    fi
    if ! flock -xn "$serena_lock_fd"; then
        echo "  WARNING: Serena clients or configuration active; skipping install/upgrade"
        return 0
    fi
    sharing="$(python3 - <<'PY'
import json, os, shutil, subprocess
from pathlib import Path
try:
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis").expanduser()
    if not home.is_absolute():
        raise ValueError("Serena sharing requires an absolute GENESIS_HOME")
    path = home / "config/serena-shared.json"
    config = json.loads(path.read_text())
    enabled = config["enabled"]
    if type(enabled) is not bool:
        raise ValueError("invalid enabled setting")
    sharing = "enabled" if enabled else "disabled"
except FileNotFoundError:
    sharing = "disabled"
except (ValueError, KeyError, TypeError, OSError):
    sharing = "unknown"
# A first install needs no user manager, but PATH absence alone can hide a pinned tool.
if sharing == "disabled" and shutil.which("serena") is None:
    try:
        root = Path(os.fsdecode(subprocess.check_output(["uv", "tool", "dir"]).removesuffix(b"\n")))
        if not root.is_absolute():
            raise ValueError("invalid uv tool directory")
        provider = root / "serena-agent"
        if not (provider.exists() or provider.is_symlink()):
            sharing = "fresh"
    except (OSError, ValueError, subprocess.CalledProcessError):
        sharing = "unknown tool installation"
# Unit names and the installed provider are per-user, independent of GENESIS_HOME.
if sharing == "disabled":
    try:
        for context in ("claude-code", "codex"):
            output = subprocess.check_output([
                "systemctl", "--user", "show", f"genesis-serena-{context}.service",
                "--property=LoadState", "--property=ActiveState", "--property=UnitFileState",
            ], text=True)
            state = dict(line.split("=", 1) for line in output.splitlines())
            absent = state == {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": ""}
            stopped = (state.get("ActiveState") in ("inactive", "failed")
                       and (state.get("LoadState"), state.get("UnitFileState"))
                       in (("loaded", "disabled"), ("masked", "masked")))
            if not (absent or stopped):
                sharing = "enabled or unknown in user services"
                break
    except (OSError, ValueError, subprocess.CalledProcessError):
        sharing = "unknown in user services"
print(sharing)
PY
)" || sharing=unknown
    if [ "$sharing" = fresh ]; then
        uv tool install serena-agent 2>/dev/null || echo "  WARNING: Serena install failed (non-critical)"
    elif [ "$sharing" != disabled ]; then
        echo "  Serena: automatic install/upgrade skipped (sharing $sharing); disable sharing before upgrading and revalidate before enabling."
    elif command -v serena >/dev/null 2>&1; then
        uv tool upgrade serena-agent 2>/dev/null || echo "  WARNING: Serena upgrade failed (non-critical)"
    else
        uv tool install serena-agent 2>/dev/null || echo "  WARNING: Serena install failed (non-critical)"
    fi
)
