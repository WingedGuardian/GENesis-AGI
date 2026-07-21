# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Host-side backup-passphrase escrow lookup — shared by scripts/restore.sh
# (circular-trap fallback: decrypt a backup when secrets.env itself was lost)
# and scripts/backup.sh (SF4 round-trip: verify the freshly-encrypted SQL dump
# decrypts with the passphrase a DR box would actually use, so env-vs-escrow
# drift pages at backup time instead of surfacing at disaster time).
#
# Contract:
#   passphrase_escrow_lookup
#     Sets ESCROW_PASSPHRASE / ESCROW_SOURCE to the first candidate file that
#     yields a non-empty value (both empty when no escrow found). ALWAYS
#     returns 0 (set -e safe). No logging — callers own their log() idiom.
#
# Candidate order is load-bearing (explicit override, then shared mount, then
# host-side guardian state) and must stay in sync with the credential bridge's
# write locations. The bridge writes exactly `GENESIS_BACKUP_PASSPHRASE=<value>`
# (no quotes); an optional `export ` prefix is tolerated. Values are NOT
# quote-stripped — the passphrase may legitimately contain quotes.

passphrase_escrow_lookup() {
    ESCROW_PASSPHRASE=""
    ESCROW_SOURCE=""
    local _escrow _val
    for _escrow in \
        "${GENESIS_PASSPHRASE_ESCROW:-}" \
        "$HOME/.genesis/shared/guardian/backup_passphrase.env" \
        "$HOME/.local/state/genesis-guardian/shared/guardian/backup_passphrase.env" \
        "$HOME/.local/state/genesis-guardian/creds-archive/backup_passphrase.env"; do
        [ -n "$_escrow" ] && [ -f "$_escrow" ] || continue
        _val="$(sed -n 's/^\(export \)\{0,1\}GENESIS_BACKUP_PASSPHRASE=//p' "$_escrow" | head -n1)"
        if [ -n "$_val" ]; then
            ESCROW_PASSPHRASE="$_val"
            ESCROW_SOURCE="$_escrow"
            break
        fi
    done
    return 0
}
