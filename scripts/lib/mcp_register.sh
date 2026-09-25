# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# _register_mcp — register a code-intelligence MCP server with Claude Code.
# Single source of truth, sourced by scripts/install.sh (fresh install) and
# scripts/bootstrap.sh (every update via update.sh), so both paths register
# identically.
#
# THE TWO TRANSPORTS DIFFER ON WHAT THEY DO ABOUT A MISMATCH, deliberately:
#
#   _register_mcp (stdio)  HEALS a drifted command. `claude mcp list` merges
#                          scopes, so a same-named project-scope entry can mask
#                          a STALE user-scope command behind "already
#                          registered" (real case: codebase-memory-mcp stayed
#                          pointed at the bare binary, bypassing the memory-cap
#                          launcher). User scope is checked against
#                          ~/.claude.json directly and re-registered.
#
#   _register_mcp_http     NEVER heals. It does not remove or replace an entry
#                          already under the name — it preserves it and tells
#                          the operator how to adopt ours. See that function
#                          for why the asymmetry is correct rather than an
#                          inconsistency.
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

# _mcp_entry_exists — is there a user-scope entry under this name with a
# TRUTHY value? Prints "1" or "". Deliberately transport-blind: the stdio
# heal path needs to know a NAME is taken, which is what `claude mcp add`
# refuses on, and the per-transport identity checks answer a different
# question (is the stored value the one we intend).
#
# Truthiness, not membership — so `{}` and `null` read as absent, and a
# python3 failure is swallowed to "". Kept as-is because _register_mcp's
# behaviour is out of scope here; _register_mcp_http uses the stricter
# _mcp_entry_present below. Closing the same gap for the stdio caller is
# tracked separately.
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

# _mcp_entry_present — is the NAME taken at user scope, at all? Prints "1"
# (taken), "" (free), or "unknown" (could not read the config).
#
# MEMBERSHIP, not truthiness, and it fails CLOSED. _register_mcp_http promises
# never to replace an existing entry, and a detector that answers "absent" for
# `{}`, for `null`, or because python3 fell over would send the caller to
# `claude mcp add` against a name the operator owns. MEASURED (CC 2.1.246) that
# `add` refuses a taken name — exit 1, "already exists in user config", entry
# byte-identical — so nothing is destroyed today. That is a THIRD PARTY's
# behaviour holding the guarantee up, and this whole function exists because
# one predicate about ownership was wrong three revisions running. Genesis's
# own layer answers for it.
_mcp_entry_present() {
    python3 - "$1" <<'PYEOF' 2>/dev/null || echo "unknown"
import json, os, sys
try:
    cfg = json.load(open(os.path.expanduser("~/.claude.json")))
except FileNotFoundError:
    print("")          # no config at all is a genuine "free", not a failure
except Exception:
    print("unknown")   # unreadable/corrupt — do not claim the name is free
else:
    print("1" if sys.argv[1] in cfg.get("mcpServers", {}) else "")
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
        if [ "$scope" = "user" ] && [ -n "$(_mcp_entry_present "$name")" ]; then
            echo "  $name: declined (URL empty), but an EXISTING $scope registration remains ACTIVE"
            echo "    remove it with: claude mcp remove $name -s $scope"
            # "remains ACTIVE" is a claim about what a session will REACH, and
            # a local-scope entry outranks user scope — so removing the user
            # one would not be the whole job. Every exit that says what is live
            # owes this check.
            _warn_local_scope_shadow "$name" "$url"
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
        # server, so anything that is not equal is a different configuration.
        if [ "$registered" = "$url" ]; then
            echo "  $name: already registered"
            _warn_local_scope_shadow "$name" "$url"
            return 0
        fi
        #
        # NEVER HEAL — preserve and warn. Owner decision, 2026-09-24.
        #
        # Three revisions tried to decide OWNERSHIP from what was stored, and
        # each produced one defect: healing every mismatch destroyed an
        # operator's own server; preserving every mismatch made
        # GENESIS_GREP_MCP_URL inert on a box Genesis had already set up;
        # renaming to a Genesis-owned name and healing again destroyed an
        # operator's entry, one name over. The pattern is the tell — no
        # predicate over ~/.claude.json can distinguish an entry Genesis wrote
        # from an identical one an operator wrote, because nothing in the file
        # records who wrote it.
        #
        # So the predicate is deleted rather than narrowed a fourth time. An
        # existing entry is ALWAYS the operator's to change. This CANNOT
        # destroy configuration, needs no ownership state to maintain, and
        # removes the remove-then-add window entirely: nothing is removed, so a
        # failing `claude mcp add` can never leave the name unregistered.
        #
        # The accepted cost, stated so it is not discovered: changing
        # GENESIS_GREP_MCP_URL on an already-registered box does NOT take
        # effect by itself. The operator removes the entry first, which the
        # warning below spells out. That is one manual step against a class of
        # silent data loss.
        #
        # Deliberately NOT applied to _register_mcp above: that function heals
        # STDIO entries, which Genesis has written under those names since long
        # before this file grew an HTTP path, and its heal fixes a measured
        # real failure. The asymmetry is the point — an established heal with a
        # known provenance is not the same claim as a new one.
        local present
        present="$(_mcp_entry_present "$name")"
        if [ -n "$present" ]; then
            if [ "$present" = "unknown" ]; then
                echo "  WARNING: could not read ~/.claude.json — cannot tell whether $name is"
                echo "    already registered, so NOTHING was changed. Fix the file and re-run."
                return 0
            fi
            if [ -n "$registered" ]; then
                echo "  WARNING: $name already exists at $scope scope with a DIFFERENT URL."
                echo "    stored: $registered"
                echo "    ours:   $url"
                # Exact-match identity, so a trailing slash reads as different
                # and this warns on every run until the operator settles on one
                # spelling. Noisy in the safe direction; not worth a normaliser
                # that would have to model URL equivalence.
            else
                echo "  WARNING: $name already exists at $scope scope with a different value."
            fi
            echo "    Genesis has NOT modified it — an existing entry is never replaced."
            echo "    To adopt the Genesis-managed server:"
            echo "      claude mcp remove $name -s $scope   # then re-run scripts/bootstrap.sh"
            # BOTH lines above are the only remedy this branch offers, and a
            # local-scope entry outranks user scope — so where one exists, that
            # remedy silently does nothing. Under the old heal behaviour this
            # case fell THROUGH to the shared warning at the bottom; converting
            # it to an early return took the warning with it, invisibly, since
            # that call is untouched context in the diff.
            #
            # Compared against OURS, not against the stored value: the question
            # this branch has to answer is "will a local entry still shadow the
            # server after they adopt ours?". MEASURED — passing the stored URL
            # goes silent exactly when a local entry matches it, which is the
            # case where the remedy above does not work.
            _warn_local_scope_shadow "$name" "$url"
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
