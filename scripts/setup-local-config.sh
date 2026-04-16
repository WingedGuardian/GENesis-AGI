#!/usr/bin/env bash
# Genesis — Local config setup
#
# Creates ~/.genesis/config/genesis.yaml from user input (or auto-detects
# where possible). Also creates the local module and profile overlay dirs.
#
# Idempotent: safe to re-run. Existing values are shown as defaults.
#
# Usage:
#   ./scripts/setup-local-config.sh
#   ./scripts/setup-local-config.sh --non-interactive   # use all defaults

set -euo pipefail

GENESIS_HOME="${HOME}/.genesis"
CONFIG_DIR="${GENESIS_HOME}/config"
LOCAL_CONFIG="${CONFIG_DIR}/genesis.yaml"
REPO_EXAMPLE="$(dirname "$(readlink -f "$0")")/../config/genesis.yaml.example"

NON_INTERACTIVE=false
if [[ "${1:-}" == "--non-interactive" ]]; then
    NON_INTERACTIVE=true
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

_print() { printf '\033[0;36m%s\033[0m\n' "$*"; }
_ok()    { printf '\033[0;32m✓ %s\033[0m\n' "$*"; }
_warn()  { printf '\033[0;33m! %s\033[0m\n' "$*" >&2; }

_ask() {
    local prompt="$1" default="$2" result
    if [[ "$NON_INTERACTIVE" == true ]]; then
        echo "$default"
        return
    fi
    read -rp "  ${prompt} [${default}]: " result
    echo "${result:-$default}"
}

_read_yaml_value() {
    # Read a top-level or nested value from genesis.yaml.
    # Usage: _read_yaml_value timezone (returns "UTC" or existing value)
    #        _read_yaml_value network.ollama_url
    local key="$1"
    [[ -f "$LOCAL_CONFIG" ]] || { echo ""; return; }
    python3 - "$LOCAL_CONFIG" "$key" <<'PYEOF'
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f) or {}
    parts = sys.argv[2].split(".")
    val = data
    for p in parts:
        if not isinstance(val, dict):
            print(""); sys.exit(0)
        val = val.get(p, "")
    print(val if val is not None else "")
except Exception:
    print("")
PYEOF
}

# ── Banner ────────────────────────────────────────────────────────────────────

echo ""
_print "  Genesis — Local Config Setup"
_print "  ────────────────────────────────────────────"
echo ""
echo "  This creates ~/.genesis/config/genesis.yaml with your machine-specific"
echo "  settings (timezone, service URLs, GitHub identity)."
echo "  The file is NEVER committed to git."
echo ""

# ── Migrate from repo YAML if this is an existing install ────────────────────

if [[ -f "$LOCAL_CONFIG" ]]; then
    _ok "Existing config found at $LOCAL_CONFIG — showing current values as defaults."
else
    _print "  Creating new config at $LOCAL_CONFIG"
    mkdir -p "$CONFIG_DIR"
fi

# ── Gather values ─────────────────────────────────────────────────────────────

echo ""
_print "  [1/4] Timezone"
echo "  Your local IANA timezone (e.g. UTC, Europe/London, UTC)."

# Auto-detect from /etc/timezone or timedatectl
_auto_tz=""
if [[ -f /etc/timezone ]]; then
    _auto_tz="$(cat /etc/timezone | tr -d '[:space:]')"
elif command -v timedatectl &>/dev/null; then
    _auto_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
fi
_current_tz="$(_read_yaml_value timezone)"
_default_tz="${_current_tz:-${_auto_tz:-UTC}}"

USER_TIMEZONE="$(_ask "Timezone" "$_default_tz")"

echo ""
_print "  [2/4] Ollama (local inference)"
echo "  Leave as default if you don't have a local Ollama instance."

_current_ollama_url="$(_read_yaml_value network.ollama_url)"
_current_ollama_enabled="$(_read_yaml_value network.ollama_enabled)"
_default_ollama_url="${_current_ollama_url:-http://localhost:11434}"
_default_ollama_enabled="${_current_ollama_enabled:-false}"

OLLAMA_URL="$(_ask "Ollama URL" "$_default_ollama_url")"
OLLAMA_ENABLED="$(_ask "Ollama enabled (true/false)" "$_default_ollama_enabled")"

echo ""
_print "  [3/4] LM Studio (optional GPU inference)"
echo "  Leave as default if you don't have LM Studio."

_current_lm_url="$(_read_yaml_value network.lm_studio_url)"
_default_lm_url="${_current_lm_url:-http://localhost:1234/v1}"
LM_STUDIO_URL="$(_ask "LM Studio URL" "$_default_lm_url")"

echo ""
_print "  [4/4] GitHub identity"
echo "  Used by the contribution pipeline and docs. Leave blank to skip."

_current_gh_user="$(_read_yaml_value github.user)"
_current_gh_public="$(_read_yaml_value github.public_repo)"
_default_gh_user="${_current_gh_user:-$(git config --global user.name 2>/dev/null | tr ' ' '-' || echo '')}"
_default_gh_public="${_current_gh_public:-GENesis-AGI}"

GH_USER="$(_ask "GitHub username" "${_default_gh_user:-YOUR_GITHUB_USER}")"
GH_PUBLIC_REPO="$(_ask "Public repo name" "$_default_gh_public")"

# ── Write config ──────────────────────────────────────────────────────────────

mkdir -p "$CONFIG_DIR"

# Use python3 yaml.dump to safely serialize all values — prevents YAML injection
# from crafted URL/timezone strings containing newlines or special characters.
python3 - "$LOCAL_CONFIG" \
    "$OLLAMA_URL" "$OLLAMA_ENABLED" "$LM_STUDIO_URL" \
    "$USER_TIMEZONE" "$GH_USER" "$GH_PUBLIC_REPO" << 'PYEOF'
import sys, yaml

out_path = sys.argv[1]
ollama_url, ollama_enabled_str, lm_studio_url = sys.argv[2], sys.argv[3], sys.argv[4]
timezone, gh_user, gh_public_repo = sys.argv[5], sys.argv[6], sys.argv[7]

# Parse ollama_enabled: accept "true"/"false"/1/0 strings
ollama_enabled = ollama_enabled_str.lower() in {"true", "1", "yes"}

data = {
    "network": {
        "ollama_url": ollama_url,
        "ollama_enabled": ollama_enabled,
        "lm_studio_url": lm_studio_url,
    },
    "timezone": timezone,
    "github": {
        "user": gh_user,
        "public_repo": gh_public_repo,
        "private_repo": "",
    },
}

header = (
    "# Genesis local configuration — machine-specific, never committed to git.\n"
    "# Regenerate: ./scripts/setup-local-config.sh\n"
    "# Env var overrides take precedence over values here.\n\n"
)
with open(out_path, "w") as f:
    f.write(header)
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
PYEOF

_ok "Config written to $LOCAL_CONFIG"

# ── Create local overlay dirs ─────────────────────────────────────────────────

echo ""
_print "  Creating local overlay directories..."

mkdir -p "${CONFIG_DIR}/modules"
_ok "Created ${CONFIG_DIR}/modules/"

mkdir -p "${CONFIG_DIR}/research-profiles"
_ok "Created ${CONFIG_DIR}/research-profiles/"

# ── Migrate user-specific module configs ──────────────────────────────────────

REPO_MODULES_DIR="$(dirname "$(readlink -f "$0")")/../config/modules"

for yaml_file in career-agent.yaml; do
    src="${REPO_MODULES_DIR}/${yaml_file}"
    dst="${CONFIG_DIR}/modules/${yaml_file}"
    if [[ -f "$src" ]] && [[ ! -f "$dst" ]]; then
        cp "$src" "$dst"
        _ok "Migrated ${yaml_file} to local config (original stays in repo for now)"
    fi
done

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
_print "  ────────────────────────────────────────────"
_print "  Setup complete!"
echo ""
echo "  Config: $LOCAL_CONFIG"
echo "  Local modules:   ${CONFIG_DIR}/modules/"
echo "  Local profiles:  ${CONFIG_DIR}/research-profiles/"
echo ""
echo "  Verify with: python -c \"from genesis.env import user_timezone; print(user_timezone())\""
echo ""
