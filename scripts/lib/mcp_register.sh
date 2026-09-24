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
#        _register_mcp_http <name> <scope> <url>
#
# Remote MCP endpoints, defined HERE so install.sh and bootstrap.sh cannot
# drift apart on the literal. Overridable by env for an install that proxies
# or pins a different host; the default is the vendor's public endpoint.
#
# `-` and NOT `:=` on purpose. The colon form treats an EMPTY value as unset
# and overwrites it, so `GENESIS_GREP_MCP_URL=""` — the spelling an operator
# reaches for to decline — would silently register the public endpoint anyway.
# With `-`, an explicitly empty value survives and _register_mcp_http skips.
# This matters more than for the other servers: the other two user-scope
# entries are local binaries, this is the first that sends a query off-box,
# and user scope means it is present in EVERY Claude Code project on the
# machine. Declining must actually work.
GENESIS_GREP_MCP_URL="${GENESIS_GREP_MCP_URL-https://mcp.grep.app}"
#
# _register_mcp registers a STDIO server (a local command). Remote servers
# speak HTTP and are stored with a "url" and no "command" at all, so they need
# _register_mcp_http below — the stdio drift check reads "command" and would
# see "" for every HTTP entry, re-registering it on every single run.

# _mcp_entry_exists — is there a user-scope entry under this name AT ALL,
# whatever its transport? Prints "1" or "". Deliberately transport-blind: the
# drift-heal paths need to know a NAME is taken, which is what `claude mcp add`
# refuses on, and the per-transport identity checks answer a different question
# (is the stored value the one we intend).
_mcp_entry_exists() {
    python3 - "$1" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(os.path.expanduser("~/.claude.json")))
    print("1" if cfg.get("mcpServers", {}).get(sys.argv[1]) else "")
except Exception:
    print("")
PYEOF
}

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
        # Two different situations, and only one of them is ours to heal.
        #
        # A non-empty `registered` means a COMMAND entry that does not match:
        # Genesis writes command entries under these names, so this is drift it
        # plausibly created, and re-registering is the long-standing behaviour
        # this helper exists for (see the header docblock).
        #
        # An EMPTY `registered` with an entry present means the stored entry has
        # no command at all — a different transport, which Genesis has never
        # written here. That is the operator's, so preserve it and say so
        # rather than removing it. Without this split, adding HTTP support
        # turned a helper that heals Genesis's own drift into one that deletes
        # configuration it did not create.
        if [ -n "$registered" ]; then
            echo "  $name: registered command drifted ($registered) — re-registering"
            claude mcp remove "$name" -s "$scope" 2>/dev/null || true
        elif [ -n "$(_mcp_entry_exists "$name")" ]; then
            echo "  WARNING: $name already exists at $scope scope with a different transport."
            echo "    Genesis has NOT modified it — it did not create that entry."
            echo "    To adopt the Genesis-managed server: claude mcp remove $name -s $scope   (then re-run)"
            return 0
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

# _register_mcp_http — same contract as _register_mcp, for a REMOTE server
# reached over HTTP. Kept as its own function rather than a transport flag on
# _register_mcp because every step differs: the add command takes --transport,
# the stored key is "url" not "command", and there is no argv to compare.
_register_mcp_http() {
    local name="$1" scope="$2" url="$3"
    # An empty URL is a deliberate decline, not a bug — see the `-` default
    # above. Say so rather than failing silently, so an operator who set it
    # can see the switch took effect, and one who blanked it by accident
    # learns why the server is absent.
    #
    # On an ALREADY-INSTALLED box the decline does not un-register anything:
    # the entry survives in ~/.claude.json and stays live in every Claude Code
    # project. Printing "declined" while it keeps running is the worst of both,
    # so check and say so. NOT auto-removed — this file's own doctrine (see
    # _warn_local_scope_shadow) is never to delete an operator's config
    # silently; surface it and give them the command.
    if [ -z "$url" ]; then
        if [ "$scope" = "user" ] && [ -n "$(_mcp_entry_exists "$name")" ]; then
            echo "  $name: declined (URL empty), but an EXISTING $scope registration remains ACTIVE"
            echo "    remove it with: claude mcp remove $name -s $scope"
        else
            echo "  $name: skipped (URL empty — registration declined)"
        fi
        return 0
    fi
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
    print(cfg.get("mcpServers", {}).get(sys.argv[1], {}).get("url", ""))
except Exception:
    print("")
PYEOF
)"
        # A URL is an exact-match identity: unlike a command path there is no
        # basename form that is legitimately a different spelling of the same
        # server, so any difference is drift and must heal.
        if [ "$registered" = "$url" ]; then
            echo "  $name: already registered"
            _warn_local_scope_shadow "$name" "$url"
            return 0
        fi
        # Ownership is decided by the NAME, which is why Genesis registers
        # `grep-app` rather than the generic `grep` that grep.app's own docs
        # use. Two earlier revisions tried to infer ownership from the stored
        # value and produced one defect each, in opposite directions: removing
        # a mismatch destroyed an operator's own server, and preserving every
        # mismatch made GENESIS_GREP_MCP_URL inert on any box Genesis had
        # already set up. No predicate over an ambiguous name can be right
        # both ways, so the ambiguity is removed instead of adjudicated.
        #
        # A URL mismatch under a Genesis-owned name is therefore OUR entry with
        # a changed endpoint — heal it, which is what makes the override work
        # on an existing install.
        if [ -n "$registered" ]; then
            echo "  $name: endpoint changed (stored: $registered) — re-registering"
            claude mcp remove "$name" -s "$scope" 2>/dev/null || true
        elif [ -n "$(_mcp_entry_exists "$name")" ]; then
            # An entry with no "url" is a different TRANSPORT, which Genesis
            # never writes here. Even under a name we own, that is something
            # we did not create — preserve it and say so rather than deleting
            # configuration whose origin we cannot account for.
            echo "  WARNING: $name already exists at $scope scope with a different transport."
            echo "    Genesis has NOT modified it — it did not create that entry."
            echo "    To adopt the Genesis-managed server: claude mcp remove $name -s $scope   (then re-run)"
            return 0
        fi
    else
        if claude mcp list 2>/dev/null | grep -q "^$name:"; then
            echo "  $name: already registered"
            return 0
        fi
    fi
    claude mcp add --transport http "$name" -s "$scope" "$url" 2>/dev/null \
        && echo "  $name: registered ($scope, http)" \
        || echo "  WARNING: Failed to register $name"
    [ "$scope" = "user" ] && _warn_local_scope_shadow "$name" "$url"
    return 0
}

# Local-scope (per-project) entries take PRECEDENCE over user scope, so a
# stale local entry silently shadows a healed user registration. Never
# auto-remove a user's local config — surface it loudly instead.
# Reads BOTH "command" (stdio) and "url" (http) so an HTTP server's local
# shadow is surfaced too; a stdio entry has no url and vice versa, so the
# `or` below picks whichever the stored entry actually carries.
_warn_local_scope_shadow() {
    local name="$1" intended="$2" shadows
    shadows="$(python3 - "$name" "$intended" <<'PYEOF' 2>/dev/null
import json, os, sys
try:
    cfg = json.load(open(os.path.expanduser("~/.claude.json")))
    for proj, p in cfg.get("projects", {}).items():
        entry = p.get("mcpServers", {}).get(sys.argv[1], {})
        cmd = entry.get("command", "") or entry.get("url", "")
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
