# shellcheck shell=bash
# Managed Codebase route selection, derived from state that already exists.
#
# Sourced by .claude/mcp/run-codebase-memory and scripts/lib/code_intel_index.sh.
# There is no selection marker of its own: evidence is the explicit override,
# the settings path, or a generated unit fragment that scripts/codebase_managed.py
# configure wrote. Those fragments survive loss of the settings mount, and
# `codebase_managed.py remove` (deleting them, then the settings) is the way back
# to the raw provider.

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

# codebase_managed_selected CONFIG HOME
#   0  managed route selected: a non-empty override, settings present (a broken
#      link included), or an owned fragment naming CONFIG
#   1  never configured: none of the above and no owned fragment at all
#   2  evidence could not be read
#   3  owned fragments exist, but name a different settings path
# Only status 1 permits the raw provider. Prints nothing on stdout, which is the
# MCP transport for the launcher.
codebase_managed_selected() {
    local config="${1:-}" home="${2:-}" directory resolved fragment first second other=0
    # Empty means unset, as in every caller's ${VAR:-default} expansion.
    if [[ -n "${CODEBASE_MEMORY_MCP_MANAGED_CONFIG:-}" ]]; then
        return 0
    fi
    if [[ -e "$config" || -L "$config" ]]; then
        return 0
    fi
    if [[ "$config" != /* || "$home" != /* ]]; then
        return 2
    fi
    # The settings can only be absent if every existing directory above them
    # is searchable; otherwise their presence cannot be read (refuse).
    if _codebase_managed_hidden "${config%/*}"; then
        return 2
    fi
    # Units record the settings path with its directory canonicalised and its
    # final component literal (codebase_managed.config_path). Either spelling
    # matches; a spelling neither produces still refuses, as status 3.
    resolved="$config"
    directory="${config%/*}"
    if command -v realpath >/dev/null 2>&1 \
        && directory="$(realpath -m -- "${directory:-/}" 2>/dev/null)"; then
        resolved="${directory%/}/${config##*/}"
    fi
    # A glob over a directory it cannot list matches nothing, which would read as
    # "never configured". Every existing component must be searchable and the
    # unit directory listable, or the evidence is unreadable.
    if _codebase_managed_hidden "$home/.config/systemd/user"; then
        return 2
    fi
    if [[ -e "$home/.config/systemd/user" ]] \
        && ! [[ -d "$home/.config/systemd/user" && -r "$home/.config/systemd/user" \
            && -x "$home/.config/systemd/user" ]]; then
        return 2
    fi
    for fragment in "$home"/.config/systemd/user/genesis-cbm-*; do
        [[ -f "$fragment" ]] || continue
        [[ -r "$fragment" ]] || return 2
        first=""
        second=""
        { IFS= read -r first || :; IFS= read -r second || :; } < "$fragment" || return 2
        [[ "$first" == "# Genesis managed Codebase v1" ]] || continue
        case "$second" in
            "# Genesis managed config: $config" | "# Genesis managed config: $resolved")
                return 0 ;;
        esac
        other=1
    done
    if [[ "$other" == 1 ]]; then
        return 3
    fi
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
