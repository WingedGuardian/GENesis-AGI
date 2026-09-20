# shellcheck shell=bash
# ONE verified site for resolving the codebase-memory-mcp machine kill-switch
# path — the same semantics .claude/mcp/run-codebase-memory enforces on its own
# (that launcher stays self-contained: an MCP launcher must not gain a file
# dependency to stay fail-closed).
#
# Every consumer resolves through here so the sentinel cannot drift between
# sites: the installers honour the override, the indexer refuses when the
# override cannot be made absolute, and a relative or unexpanded ~/ override
# can never silently read as "not disabled".
#
# Prints the resolved ABSOLUTE path on stdout and returns 0; returns 1 when no
# absolute path can be produced (no home anywhere and no absolute override, or
# an override that is relative / begins with an unexpandable ~/). An empty
# override means unset — the same convention the launcher and installer use —
# so it falls back to the HOME-based default rather than refusing.
genesis_cbm_disable_file() {
    # No passwd fallback here: the installer's documented contract is that an
    # unset HOME is UNRESOLVABLE, not silently another account's home — callers
    # that want passwd resolution (the indexer, bootstrap) do it themselves
    # before this runs.
    local _home="${HOME:-}"
    local _path="${CODEBASE_MEMORY_MCP_DISABLE_FILE:-${_home:+${_home}/.genesis/codebase-memory-mcp.disabled}}"
    # shellcheck disable=SC2088  # matching a LITERAL ~/ prefix is the point:
    # the override arrived unexpanded, and this is where it gets expanded.
    if [[ "$_path" == "~/"* ]]; then
        # With no resolvable home this collapses to a relative path and the
        # absolute-path check below fails closed.
        _path="${_home:+${_home}/}${_path#\~/}"
    fi
    [[ "$_path" == /* ]] || return 1
    printf '%s\n' "$_path"
}
