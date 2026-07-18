# shellcheck shell=bash
# Live-system guard for bootstrap.sh — sourced, not executed.
#
# bootstrap_refuse_if_server_live [args...]
#
# bootstrap.sh is a setup/repair tool, NOT a deploy path. Running it on a
# machine with a live genesis-server re-runs installers (curl|bash, npm -g,
# pip), re-registers MCP servers + systemd units, and edits dotfiles on top
# of a running system — the direct trigger of the 2026-07-13 eth0/load
# incident. Code changes deploy via scripts/update.sh (or git pull +
# pip install -e . + systemctl --user restart genesis-server).
#
# This function makes that rule mechanical: refuse to proceed while
# genesis-server is live, unless one of two overrides applies:
#
#   GENESIS_BOOTSTRAP_ALLOW_LIVE=1  — sanctioned-caller opt-out. update.sh
#       sets it on its bootstrap invocation: its ERR trap is armed there, so
#       a guard refusal inside update.sh would trigger a FULL update rollback
#       (git reset to the rollback tag) — the guard must never gate the
#       sanctioned deploy path, which stops the server itself beforehand.
#   --force  — human override for deliberate live repair. Proceeds with a
#       loud warning.
#
# Server-live detection mirrors update.sh's _ensure_server_down check so the
# two can't drift: live iff `systemctl --user is-active` reports active OR a
# `python -m genesis serve` process exists. Detection is FAIL-OPEN: any other
# outcome (unknown unit rc=4, no session D-Bus, pgrep miss/absent) reads as
# not-running and bootstrap proceeds — a broken detector must never block a
# fresh-install bootstrap (where no server exists yet).
#
# Return codes (caller decides severity, cf. venv_setup.sh):
#   0 — proceed (not live, opt-out set, or --force given; warning printed
#       when forcing past a live server)
#   1 — refused: server is live, no override (message already printed)

_genesis_server_is_live() {
    if systemctl --user is-active --quiet genesis-server.service 2>/dev/null; then
        return 0
    fi
    if pgrep -f "python -m genesis serve" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

bootstrap_refuse_if_server_live() {
    # Sanctioned-caller opt-out (update.sh deploy path) — silent, unconditional.
    if [ -n "${GENESIS_BOOTSTRAP_ALLOW_LIVE:-}" ]; then
        return 0
    fi

    local force=false arg
    for arg in "$@"; do
        [ "$arg" = "--force" ] && force=true
    done

    if ! _genesis_server_is_live; then
        return 0
    fi

    if [ "$force" = "true" ]; then
        echo "WARNING: proceeding on a LIVE system by --force (genesis-server is running)."
        return 0
    fi

    cat >&2 <<'EOF'
REFUSED: genesis-server is RUNNING — bootstrap.sh is a setup/repair tool,
not a deploy path. Running it on a live system re-runs installers and
re-registers services on top of a running server (this triggered the
2026-07-13 eth0/load incident).

To deploy code changes, use the sanctioned path instead:
    scripts/update.sh
  or, for a code-only change:
    git pull && pip install -e . && systemctl --user restart genesis-server

To run bootstrap for deliberate live repair anyway:
    ./scripts/bootstrap.sh --force
EOF
    return 1
}
