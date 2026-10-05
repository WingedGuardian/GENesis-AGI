# shellcheck shell=bash
# Standard unit rendering; no runtime fragment ownership or selection records.
# Install only after opt-in configuration, or keep an already installed pair.
# Returns 0 when the template should be rendered now; also read by the
# fresh-install CI check, which mirrors this rule rather than restating it.
genesis_cbm_template_selected() {
    local name="$1" unit_dir="$2" home="$3"
    case "$name" in genesis-cbm-query.service | genesis-cbm-query-clients.slice) ;;
        *) return 0 ;; esac
    # Selection evidence is never permission to write through a pathname. Both
    # renderers write with `> "$target"`, which follows a link (a `systemctl
    # link`ed unit, or a dangling one whose target it would create) and fails on
    # a directory. Preserve any non-regular artifact for operator review.
    if [[ -L "$unit_dir/$name" ]] || [[ -e "$unit_dir/$name" && ! -f "$unit_dir/$name" ]]; then
        echo "  Kept: $name (symlink or non-regular unit path preserved; not rendered)" >&2
        return 1
    fi
    [[ -e "$home/.genesis/config/codebase-managed.json" || -L "$home/.genesis/config/codebase-managed.json" \
       || -e "$unit_dir/genesis-cbm-query.service" || -L "$unit_dir/genesis-cbm-query.service" \
       || -e "$unit_dir/genesis-cbm-query-clients.slice" || -L "$unit_dir/genesis-cbm-query-clients.slice" ]]
}

# Inside a quoted ExecStart=: argument. ':' disables dollar expansion; '%' is
# still a specifier and must double. Sed replacement escaping happens afterwards.
genesis_cbm_exec_path() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'
}
