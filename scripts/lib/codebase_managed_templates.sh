# shellcheck shell=bash
# Standard unit rendering; no runtime fragment ownership or selection records.
# Install only after opt-in configuration, or keep an already installed pair.
genesis_cbm_template_selected() {
    local name="$1" unit_dir="$2" home="$3"
    case "$name" in genesis-cbm-query.service | genesis-cbm-query-clients.slice) ;;
        *) return 0 ;; esac
    [[ -e "$home/.genesis/config/codebase-managed.json" || -L "$home/.genesis/config/codebase-managed.json" \
       || -e "$unit_dir/genesis-cbm-query.service" || -L "$unit_dir/genesis-cbm-query.service" \
       || -e "$unit_dir/genesis-cbm-query-clients.slice" || -L "$unit_dir/genesis-cbm-query-clients.slice" ]]
}

# Inside a quoted ExecStart=: argument. ':' disables dollar expansion; '%' is
# still a specifier and must double. Sed replacement escaping happens afterwards.
genesis_cbm_exec_path() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'
}
