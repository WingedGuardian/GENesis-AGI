# shellcheck shell=bash
# Keep automatic tool updates from invalidating an enabled shared-service pin.
_install_serena() {
    local sharing
    sharing="$(python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ.get("GENESIS_HOME", str(Path.home() / ".genesis"))) / "config/serena-shared.json"
try:
    config = json.loads(path.read_text())
    enabled = config["enabled"]
    if type(enabled) is not bool:
        raise ValueError("invalid enabled setting")
    print("enabled" if enabled else "disabled")
except FileNotFoundError:
    print("disabled")
except (ValueError, KeyError, TypeError, OSError):
    print("unknown")
PY
)" || sharing=unknown
    if [ "$sharing" != disabled ]; then
        echo "  Serena: automatic install/upgrade skipped (sharing $sharing); disable sharing before upgrading and revalidate before enabling."
    elif command -v serena >/dev/null 2>&1; then
        uv tool upgrade serena-agent 2>/dev/null || echo "  WARNING: Serena upgrade failed (non-critical)"
    else
        uv tool install serena-agent 2>/dev/null || echo "  WARNING: Serena install failed (non-critical)"
    fi
}
