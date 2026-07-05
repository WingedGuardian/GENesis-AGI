# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# _register_mcp — register a code-intelligence MCP server with Claude Code,
# healing DRIFTED user-scope registrations. Single source of truth, sourced by
# scripts/install.sh (fresh install) and scripts/bootstrap.sh (every update via
# update.sh), so both paths register identically and existing installs heal.
#
# Why drift-healing exists: `claude mcp list` merges scopes, so a same-named
# project-scope entry can mask a STALE user-scope command behind "already
# registered" (real case: codebase-memory-mcp stayed pointed at the bare
# binary, bypassing the memory-cap launcher). User scope is therefore checked
# against ~/.claude.json directly and re-registered on mismatch.
#
# Usage: _register_mcp <name> <scope> <command> [args...]

_register_mcp() {
    local name="$1" scope="$2"
    shift 2
    local cmd_args=("$@")
    if ! command -v claude &>/dev/null; then
        echo "  WARNING: 'claude' CLI not found — skipping $name registration"
        return 0
    fi
    if [ "$scope" = "user" ]; then
        local registered
        registered="$(python3 - "$name" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(os.path.expanduser("~/.claude.json")))
    print(cfg.get("mcpServers", {}).get(sys.argv[1], {}).get("command", ""))
except Exception:
    print("")
PYEOF
)"
        # Match rule: an ABSOLUTE intended command must match exactly (a stale
        # path with the right basename — e.g. a reaped worktree's launcher —
        # is drift and must heal). A BARE intended name matches any stored
        # resolution with that basename (`claude mcp add gitnexus` may store
        # "~/.local/bin/gitnexus", which is not drift).
        local intended="${cmd_args[0]}" matched=false
        if [ -n "$registered" ]; then
            case "$intended" in
                /*) [ "$registered" = "$intended" ] && matched=true ;;
                *)  [ "$(basename "$registered")" = "$intended" ] && matched=true ;;
            esac
        fi
        if [ "$matched" = true ]; then
            echo "  $name: already registered"
            _warn_local_scope_shadow "$name" "$intended"
            return 0
        fi
        if [ -n "$registered" ]; then
            echo "  $name: registered command drifted ($registered) — re-registering"
            claude mcp remove "$name" -s "$scope" 2>/dev/null || true
        fi
    else
        if claude mcp list 2>/dev/null | grep -q "^$name:"; then
            echo "  $name: already registered"
            return 0
        fi
    fi
    claude mcp add "$name" -s "$scope" -- "${cmd_args[@]}" 2>/dev/null \
        && echo "  $name: registered ($scope)" \
        || echo "  WARNING: Failed to register $name"
    [ "$scope" = "user" ] && _warn_local_scope_shadow "$name" "${cmd_args[0]}"
    return 0
}

# Local-scope (per-project) entries take PRECEDENCE over user scope, so a
# stale local entry silently shadows a healed user registration. Never
# auto-remove a user's local config — surface it loudly instead.
_warn_local_scope_shadow() {
    local name="$1" intended="$2" shadows
    shadows="$(python3 - "$name" "$intended" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(os.path.expanduser("~/.claude.json")))
    for proj, p in cfg.get("projects", {}).items():
        cmd = p.get("mcpServers", {}).get(sys.argv[1], {}).get("command", "")
        if cmd and cmd != sys.argv[2]:
            print(f"{proj} -> {cmd}")
except Exception:
    pass
PYEOF
)"
    if [ -n "$shadows" ]; then
        echo "  WARNING: $name has LOCAL-scope registrations that shadow the user-scope one:"
        while IFS= read -r line; do
            echo "    $line"
        done <<< "$shadows"
        echo "    Remove with: claude mcp remove $name -s local (run inside that project)"
    fi
}
