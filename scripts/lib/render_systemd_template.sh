# render_systemd_template <template-file> — print the rendered unit on stdout.
#
# Single source for the template→unit render: bootstrap's install loop (which
# writes every scripts/systemd/*.template into ~/.config/systemd/user and calls
# daemon-reload, never restart) AND update.sh's resident-unit heal (which must
# re-render a changed template before restarting, or the daemon bounces onto
# the previously installed directives while its fresh start time masks the
# drift). Callers must set GENESIS_ROOT; substitutions are the four bootstrap
# placeholders (__HOME__, __VENV__, __REPO_DIR__, __CC_BIN_DIR__).
#
# CC_BIN_DIR resolution mirrors the original bootstrap block: the npm prefix's
# bin is where `npm install -g` (gitnexus, and claude via cc_ensure_local)
# actually lands — nvm's bin after an nvm fallback, ~/.npm-global, or a system
# prefix — while a pinned claude already on PATH (e.g. /usr/local/bin)
# resolves `command -v` directly. Rendering only claude's dir would leave the
# npm bin invisible to the units; hardcoding ~/.npm-global would miss nvm.
render_systemd_template() {
    local template="$1"
    local _cc_path _cc_prefix CC_BIN_DIR
    _cc_path="$(command -v claude 2>/dev/null || true)"
    _cc_prefix="$(npm config get prefix 2>/dev/null || true)"
    [ -n "$_cc_prefix" ] || _cc_prefix="/usr/local"
    [ "$_cc_prefix" = "/usr" ] && _cc_prefix="/usr/local"
    if [[ -n "$_cc_path" ]]; then
        CC_BIN_DIR="$(dirname "$_cc_path")"
    else
        CC_BIN_DIR="$_cc_prefix/bin"
    fi
    if [[ "$_cc_prefix/bin" != "$CC_BIN_DIR" ]]; then
        CC_BIN_DIR="$CC_BIN_DIR:$_cc_prefix/bin"
    fi
    sed -e "s|__HOME__|$HOME|g" \
        -e "s|__VENV__|$GENESIS_ROOT/.venv|g" \
        -e "s|__REPO_DIR__|$GENESIS_ROOT|g" \
        -e "s|__CC_BIN_DIR__|$CC_BIN_DIR|g" \
        "$template"
}
