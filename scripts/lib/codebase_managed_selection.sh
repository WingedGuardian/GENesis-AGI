# shellcheck shell=bash
# Managed Codebase route selection, derived from state that already exists.
#
# Sourced by .claude/mcp/run-codebase-memory and scripts/lib/code_intel_index.sh.
# Settings or native installation state select the route. Bootstrap renders
# these disabled units only after configuration; their disabled/static state is
# evidence too, so loss of settings after disable never re-enables raw bootstrap.
# No generated headers, fragment scan or separate selection marker.

# _codebase_managed_hidden DIR
#   0 when a lookup below DIR cannot be trusted: an existing component that is
#   not a searchable directory, or a dangling or looping link (a lost mount
#   target, say). 1 when every existing component is a searchable directory.
_codebase_managed_hidden() {
    local prefix="" part
    local -a parts
    IFS=/ read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do
        [[ -n "$part" ]] || continue
        prefix="$prefix/$part"
        if [[ -L "$prefix" && ! -e "$prefix" ]]; then
            return 0
        fi
        if [[ -e "$prefix" ]] && ! [[ -d "$prefix" && -x "$prefix" ]]; then
            return 0
        fi
    done
    return 1
}

# codebase_managed_sentinel_armed PATH
#   1 only when PATH is definitely absent: not present, not a link (a dangling
#   link is present), and no component above it hides the answer. 0 otherwise,
#   including a relative PATH. Mirrors codebase_managed.sentinel_armed.
codebase_managed_sentinel_armed() {
    [[ "${1:-}" == /* && ! -e "$1" && ! -L "$1" ]] || return 0
    _codebase_managed_hidden "${1%/*}"
}

# 0: configured/native units present; 1: settings absent and both units not-found;
# 2: uncertain evidence. Only 1 permits raw. No stdout on the MCP transport.
codebase_managed_selected() {
    local config="${1:-}" home="${2:-}" unit state rc
    [[ "$config" == /* && "$home" == /* ]] || return 2
    [[ -n "${CODEBASE_MEMORY_MCP_MANAGED_CONFIG:-}" ]] && return 0
    [[ -e "$config" || -L "$config" ]] && return 0
    _codebase_managed_hidden "${config%/*}" && return 2
    for unit in genesis-cbm-query.service genesis-cbm-query-clients.slice; do
        rc=0
        state="$(systemctl --user is-enabled "$unit" 2>/dev/null)" || rc=$?
        case "$state:$rc" in
            not-found:1 | not-found:4) ;;
            enabled:0 | enabled-runtime:0 | static:0 | indirect:0 | alias:0 | generated:0 | transient:0 | disabled:1 | masked:1 | masked-runtime:1) return 0 ;;
            *) return 2 ;;
        esac
    done
    return 1
}

# codebase_managed_batch_env CONFIG REPO HELPER
#   For a selected managed route, validates the batch settings through HELPER
#   (scripts/codebase_managed.py batch) and exports the verified worker, cache,
#   runtime, physical repository root and cap. On refusal returns 1 and leaves
#   the reason in CODEBASE_MANAGED_REFUSE. Fixed line fields, never eval.
codebase_managed_batch_env() {
    local config="$1" repo="$2" helper="$3" output=""
    local -a fields
    CODEBASE_MANAGED_REFUSE=""
    if [[ -n "$repo" ]]; then
        # One physical spelling for both the validation and the exported root.
        repo="$(unset CDPATH; cd -- "$repo" 2>/dev/null && pwd -P)" || repo=""
    fi
    if [[ -z "$repo" ]]; then
        CODEBASE_MANAGED_REFUSE="managed repository path unresolvable"
    elif ! output="$(/usr/bin/python3 -I "$helper" --config "$config" batch --repo "$repo")"; then
        CODEBASE_MANAGED_REFUSE="managed Codebase configuration refused"
    else
        mapfile -t fields <<< "$output"
        if [[ "${#fields[@]}" -ne 4 ]]; then
            CODEBASE_MANAGED_REFUSE="malformed managed batch settings"
        else
            export CODE_INTEL_CBM_WORKER_BINARY="${fields[0]}" CBM_CACHE_DIR="${fields[1]}" \
                CBM_RUNTIME_DIR="${fields[2]}" CBM_ALLOWED_ROOT="$repo" \
                CODE_INTEL_CBM_MEMORY_MAX="${fields[3]}"
        fi
    fi
    [[ -z "$CODEBASE_MANAGED_REFUSE" ]]
}
