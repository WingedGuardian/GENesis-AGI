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
import json, os, subprocess
from pathlib import Path
path = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis").expanduser() / "config/serena-shared.json"
try:
    config = json.loads(path.read_text())
    enabled = config["enabled"]
    if type(enabled) is not bool:
        raise ValueError("invalid enabled setting")
    sharing = "enabled" if enabled else "disabled"
except FileNotFoundError:
    sharing = "disabled"
except (ValueError, KeyError, TypeError, OSError):
    sharing = "unknown"
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
    if [ "$sharing" != disabled ]; then
        echo "  Serena: automatic install/upgrade skipped (sharing $sharing); disable sharing before upgrading and revalidate before enabling."
    elif command -v serena >/dev/null 2>&1; then
        uv tool upgrade serena-agent 2>/dev/null || echo "  WARNING: Serena upgrade failed (non-critical)"
    else
        uv tool install serena-agent 2>/dev/null || echo "  WARNING: Serena install failed (non-critical)"
    fi
)
