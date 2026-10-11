# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# The card vault (src/genesis/payments/vault_store.py) in the encrypted
# credentials section: backup.sh §8 writes it, restore.sh §8 checks what it
# staged. The vault is three files, and only all three together are usable:
#   identity.age   the age secret key, sealed under the vault passphrase
#   recipient.txt  its public key (adding a card encrypts to this)
#   cards.db       SQLite, one row per card, each card's details age-encrypted
#
#   payment_vault_check <dir>
#       Return 0 when <dir> holds a complete, readable set; otherwise print the
#       reason (fixed text, never a value) and return 1. Shared by both sides,
#       so backup only ever ships a set restore will accept.
#
#   backup_payment_vault <vault_dir> <out_dir> <temp_root>
#       Write <out_dir>/{identity.age,recipient.txt,cards.db}.gpg with
#       backup.sh's encrypt_file, and print ONE line: "<state> [reason]".
#         ok       a fresh set was written
#         absent   no vault, and no earlier set in <out_dir>
#         missing  no vault, but an earlier set exists (the caller fails the run:
#                  a vault that vanished is a loss, and retiring one on purpose
#                  is `git rm -r creds/payment_vault` in the backups repo)
#         failed   the vault exists but no fresh set could be written; the
#                  earlier set, if any, is left in place, because an older
#                  vault beats none
#       Always exits 0: the caller runs it as `out=$(…)` under `set -e`.
#       Runs in a subshell, so its EXIT trap never replaces the caller's.
#
# cards.db is copied with SQLite's online backup (a plain copy can tear on a
# concurrent `vault add`), from a read-only connection (`-readonly` never
# creates a missing file), while holding the vault directory's flock, the same
# lock `vault init` takes, so a backup never sees an init half-done. The set
# is built under <temp_root>, outside the backups clone, and moved in only once
# all three files are encrypted, under names no `*.gpg` reader matches.

PAYMENT_VAULT_FILES=(identity.age recipient.txt cards.db)

payment_vault_check() {
    local dir="$1" f ic uv
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        if [ -L "$dir/$f" ] || [ ! -f "$dir/$f" ]; then
            printf '%s is missing or not a regular file\n' "$f"
            return 1
        fi
    done
    if [ "$(head -c 21 "$dir/identity.age" 2>/dev/null)" != "age-encryption.org/v1" ]; then
        printf 'identity.age is not an age file\n'
        return 1
    fi
    if ! head -n 1 "$dir/recipient.txt" 2>/dev/null | grep -qE '^age1[0-9a-z]+$'; then
        printf 'recipient.txt does not hold an age public key\n'
        return 1
    fi
    if ! command -v sqlite3 >/dev/null 2>&1; then
        printf 'sqlite3 is not installed, so cards.db cannot be checked\n'
        return 1
    fi
    ic="$(sqlite3 -readonly "$dir/cards.db" 'PRAGMA integrity_check' 2>/dev/null)" || ic=""
    if [ "$ic" != "ok" ]; then
        printf 'cards.db failed its integrity check\n'
        return 1
    fi
    uv="$(sqlite3 -readonly "$dir/cards.db" 'PRAGMA user_version' 2>/dev/null)" || uv=""
    case "$uv" in
        '' | 0 | *[!0-9]*)
            printf 'cards.db is not a vault database (format version unset)\n'
            return 1
            ;;
    esac
    return 0
}

backup_payment_vault() (
    vault="$1" out="$2" tmproot="$3" stage="" fd="" why="" f=""
    trap '[ -n "$stage" ] && rm -rf -- "$stage"' EXIT
    say() { printf '%s\n' "$*"; exit 0; }
    prior() {
        for f in "${PAYMENT_VAULT_FILES[@]}"; do
            [ -e "$out/$f.gpg" ] && return 0
        done
        return 1
    }
    # Staging leftovers of a killed earlier run.
    rm -f -- "$out"/.*.gpg.tmp 2>/dev/null
    if [ ! -e "$vault" ] && [ ! -L "$vault" ]; then
        prior && say missing "the vault directory is gone"
        say absent
    fi
    if [ -L "$vault" ] || [ ! -d "$vault" ]; then
        say failed "the vault path is not a directory"
    fi
    declare -F encrypt_file >/dev/null || say failed "encrypt_file is not defined"
    exec {fd}<"$vault" || say failed "the vault directory cannot be opened"
    flock -w 30 "$fd" || say failed "the vault stayed locked for 30s (a vault init running?)"
    if [ ! -e "$vault/identity.age" ] && [ ! -L "$vault/identity.age" ]; then
        # Never initialised, or an init killed before its key landed (vault_store
        # treats identity.age as the mark of an initialised vault): no key to keep.
        prior && say missing "the vault holds no key"
        say absent
    fi
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        if [ -L "$vault/$f" ] || [ ! -f "$vault/$f" ]; then
            say failed "the vault is incomplete ($f is missing or not a regular file)"
        fi
    done
    stage="$(mktemp -d -p "$tmproot" payment_vault.XXXXXX 2>/dev/null)" || {
        stage=""
        say failed "cannot create a temp directory"
    }
    cp -- "$vault/identity.age" "$vault/recipient.txt" "$stage/" 2>/dev/null \
        || say failed "cannot copy the key files"
    # cd keeps the destination a fixed relative name: .backup takes it quoted,
    # and a path holding a quote would break that.
    (cd "$stage" && sqlite3 -readonly "$vault/cards.db" ".timeout 10000" ".backup cards.db") \
        >/dev/null 2>&1 || say failed "the cards.db snapshot failed (after an interrupted vault command, run python -m genesis vault status once to roll it back)"
    why="$(payment_vault_check "$stage")" || say failed "$why"
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        encrypt_file "$stage/$f" "$stage/$f.gpg" || say failed "encrypting $f failed"
    done
    # A symlink here (or at its parent) would send the set outside the backups
    # repo while the run reported ok.
    if [ -L "$out" ] || [ -L "$(dirname -- "$out")" ]; then
        say failed "the backup directory is a symlink"
    fi
    mkdir -p "$out" 2>/dev/null || say failed "cannot create the output directory"
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        cp -- "$stage/$f.gpg" "$out/.$f.gpg.tmp" 2>/dev/null || {
            rm -f -- "$out"/.*.gpg.tmp 2>/dev/null
            say failed "writing the backup copy failed"
        }
    done
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        mv -f -- "$out/.$f.gpg.tmp" "$out/$f.gpg" 2>/dev/null \
            || say failed "renaming $f.gpg into place failed"
    done
    say ok
)
