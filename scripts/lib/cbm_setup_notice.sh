# shellcheck shell=bash
# ONE site for the installers' codebase-memory-mcp setup line, shared by
# bootstrap.sh, install.sh and update.sh.
#
# It prints nothing of its own judgement: it runs
# `python -m genesis.infra_profile.codebase_mcp`, the same facts and verdict the
# infrastructure-profile collector feeds to the
# codebase_mcp_registered_unconfigured health alert, so the deploy line and the
# alert cannot disagree. Output:
#   - registered but not set up, not opted out: one line naming what is missing
#     and the doc section with the configure/enable commands;
#   - configured and enabled: "Codebase: OK (configured and enabled)";
#   - not registered, or opted out with the kill-switch file: nothing;
#   - the check itself could not run: a "could not be checked" line, never
#     silence, so an unverified state never reads as fine.
#
# Usage: genesis_cbm_setup_notice <python> <checkout> [indent]
# Always returns 0: an advisory must never fail a set -e installer.
genesis_cbm_setup_notice() {
    local _py="${1:-}" _root="${2:-}" _indent="${3:-  }" _out=""
    if [ -z "$_py" ] || [ ! -x "$_py" ] || [ ! -d "$_root/src/genesis" ]; then
        printf '%sCodebase: setup state could not be checked (no interpreter or checkout)\n' "$_indent"
        return 0
    fi
    # PYTHONPATH pins the module to THIS checkout's source rather than whatever
    # checkout the venv's editable install happens to point at.
    if _out="$(cd "$_root" && PYTHONPATH="$_root/src" "$_py" -m genesis.infra_profile.codebase_mcp \
            --repo "$_root" 2>/dev/null)"; then
        if [ -n "$_out" ]; then
            printf '%s%s\n' "$_indent" "$_out"
        fi
    else
        printf '%sCodebase: setup state could not be checked (the check failed); run %s -I scripts/codebase_managed.py status\n' \
            "$_indent" "$_py"
    fi
    return 0
}
