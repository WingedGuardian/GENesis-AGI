# shellcheck shell=bash
# Managed Codebase route selection, derived from state that already exists.
#
# Sourced by .claude/mcp/run-codebase-memory and scripts/lib/code_intel_index.sh.
# There is no selection marker of its own: evidence is the explicit override,
# the settings path, or a generated unit fragment that scripts/codebase_managed.py
# configure wrote. Those fragments survive loss of the settings mount, and
# removing them is the way back to the raw provider.
#
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
    local prefix="" part
    local -a parts
    IFS=/ read -r -a parts <<< "${config%/*}"
    for part in "${parts[@]}"; do
        [[ -n "$part" ]] || continue
        prefix="$prefix/$part"
        if [[ -d "$prefix" && ! -x "$prefix" ]]; then
            return 2
        fi
        # A dangling or looping link (a lost mount target, say) hides the settings.
        if [[ -L "$prefix" && ! -e "$prefix" ]]; then
            return 2
        fi
    done
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
    local component
    for component in "$home" "$home/.config" "$home/.config/systemd"; do
        if [[ -d "$component" && ! -x "$component" ]]; then
            return 2
        fi
    done
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
