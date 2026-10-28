# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# The card vault (src/genesis/payments/vault_store.py) in the encrypted
# credentials section: backup.sh §8 writes it, restore.sh §8 stages it. The
# vault is three files, and only all three together, from one moment, are
# usable:
#   identity.key   the age secret key (0600, no passphrase), or, in a vault
#                  made before the passphrase was dropped and not yet migrated,
#                  identity.age: the same key sealed under that passphrase
#   recipient.txt  its public key (adding a card encrypts to this)
#   cards.db       SQLite, one row per card, each card's details age-encrypted
#
# So the three travel as ONE archive, creds/payment_vault.tar.gpg: one file
# for every reader (git, the off-site upload, the host mirror, the restore
# pull), so no reader can ever hold two files from different backups.
#
#   payment_vault_check <dir>
#       Return 0 when <dir> holds a complete set vault_store accepts (its own
#       verify_set runs last); otherwise print the reason (fixed text, never a
#       value) and return 1. The shell checks before it may refuse more, never less.
#       Shared by both sides, so backup only ever ships a set restore accepts.
#
#   backup_payment_vault <vault_dir> <creds_dir> <temp_root>
#       Write <creds_dir>/payment_vault.tar.gpg with backup.sh's encrypt_file
#       and print ONE line: "<state> [reason]".
#         ok       a fresh archive was written
#         absent   no vault, and no earlier archive
#         missing  no vault, but an earlier archive exists (the caller fails the
#                  run: a vault that vanished is a loss; retiring one on purpose
#                  is `git rm creds/payment_vault.tar.gpg` in the backups repo)
#         failed   the vault exists but no fresh archive could be written; the
#                  earlier archive, if any, is kept, because an older vault
#                  beats none
#       With PAYMENT_VAULT_RECORD set to a path, an ok run also writes
#       "<key file> <recipient>" of the set it archived there (the recipient is
#       the vault's public key), so the caller can act on exactly that set.
#       Always exits 0: the caller runs it as `out=$(…)` under `set -e`.
#       Runs in a subshell, so its EXIT trap never replaces the caller's.
#
#   payment_vault_unpack <archive.tar> <dest_parent>
#       Restore's side: check that <archive.tar> holds exactly the three files,
#       each a regular file, and unpack them into a NEW directory under
#       <dest_parent>, checked with payment_vault_check. Prints that directory on
#       success; prints a reason and returns 1 otherwise (nothing left behind).
#
#   payment_vault_drop_copies <vault_dir> <recipient>
#       Delete the copies `vault migrate` kept beside the vault
#       (payment_vault.pre-migrate-<UTC stamp>). backup.sh calls it only once the
#       archive of the migrated vault with that recipient has reached the backups
#       remote: a copy holds every card the vault had, and the vault's unsealed
#       key opens them. Only a real directory holding nothing but vault files,
#       whose recipient.txt is that recipient (migration keeps the key pair), is
#       deleted; anything else is left. Prints "<deleted> <left>"; always exits 0.
#
#   payment_vault_count_copies <vault_dir>
#       Print how many payment_vault.pre-migrate-* entries sit beside the vault.
#
# cards.db is copied with SQLite's online backup (a plain copy can tear on a
# concurrent `vault add`), from a read-only connection (`-readonly` never
# creates a missing file), while holding the vault directory's flock, the same
# lock `vault init`, `add` and `retire` take, so a backup never sees one of
# them half-done. The archive is built under <temp_root>, outside the backups
# clone, and moved in by one rename from a name no `*.gpg` reader matches.

# The key file of the set in <dir>: identity.key when one is there (in any
# form: a symlink is then refused as the key, never passed over for the
# sealed copy), else identity.age. A vault holding both (a migration killed
# part-way) archives the one cards.db's digest records: identity.age while the
# migration has not moved the digest, identity.key once it has.
payment_vault_key_file() {
    local digest="" sum=""
    if [ -e "$1/identity.key" ] || [ -L "$1/identity.key" ]; then
        if [ -f "$1/identity.age" ] && [ ! -L "$1/identity.age" ] && [ ! -L "$1/cards.db" ]; then
            digest="$(sqlite3 -readonly "$1/cards.db" \
                "SELECT value FROM vault_meta WHERE key = 'identity_sha256'" 2>/dev/null)" || digest=""
            sum="$(sha256sum < "$1/identity.age" 2>/dev/null)" || sum=""
            if [ -n "$digest" ] && [ "$digest" = "${sum%% *}" ]; then
                printf 'identity.age\n'
                return 0
            fi
        fi
        printf 'identity.key\n'
    else
        printf 'identity.age\n'
    fi
}
# The three files of a set whose key file is $1, in the order tar writes them.
payment_vault_files() { PAYMENT_VAULT_FILES=("$1" recipient.txt cards.db); }
# The repository's src/, for payment_vault_check's vault_store call.
_PV_SRC="$(unset CDPATH; cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../src" 2>/dev/null && pwd)" || _PV_SRC=""
PAYMENT_VAULT_ARCHIVE=payment_vault.tar.gpg
# The name a snapshot carries the clone's kept archive under when that run's
# vault step did not succeed: restore never stages it on its own.
PAYMENT_VAULT_LAST_GOOD=payment_vault.last-good.tar.gpg
# vault_store.SCHEMA_VERSION and the cards table's columns, in order (a parity
# test builds a real vault with vault_store and checks it here).
PAYMENT_VAULT_SCHEMA_VERSION=1
PAYMENT_VAULT_COLUMNS="card_ref label brand last4 merchant single_use status created_at retired_at blob"

payment_vault_check() {
    local dir="$1" f ic uv cols meta digest sum rec why key
    local PAYMENT_VAULT_FILES
    key="$(payment_vault_key_file "$dir")"
    payment_vault_files "$key"
    for f in "${PAYMENT_VAULT_FILES[@]}"; do
        if [ -L "$dir/$f" ] || [ ! -f "$dir/$f" ]; then
            printf '%s is missing or not a regular file\n' "$f"
            return 1
        fi
    done
    if [ "$key" = identity.key ]; then
        # One line: "AGE-SECRET-KEY-1" plus 58 uppercase Bech32 characters
        # (vault_store also verifies its checksum).
        if [ "$(wc -l < "$dir/identity.key" 2>/dev/null)" != 1 ] || ! head -n 1 "$dir/identity.key" 2>/dev/null \
            | grep -qE '^AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}$'; then  # pragma: allowlist secret (public BIP-173 alphabet, not a secret)
            printf 'identity.key is not an age secret key\n'
            return 1
        fi
    elif [ "$(head -c 21 "$dir/identity.age" 2>/dev/null)" != "age-encryption.org/v1" ]; then
        printf 'identity.age is not an age file\n'
        return 1
    fi
    # "age1" plus 58 Bech32 characters: an X25519 public key. (vault_store also
    # verifies its checksum; the shape is enough to refuse a truncated file.)
    if ! head -n 1 "$dir/recipient.txt" 2>/dev/null \
        | grep -qE '^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$'; then  # pragma: allowlist secret (public BIP-173 alphabet, not a secret)
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
    if [ "$uv" != "$PAYMENT_VAULT_SCHEMA_VERSION" ]; then
        printf 'cards.db is not a vault database of format version %s\n' "$PAYMENT_VAULT_SCHEMA_VERSION"
        return 1
    fi
    cols="$(sqlite3 -readonly "$dir/cards.db" \
        "SELECT group_concat(name, ' ') FROM (SELECT name FROM pragma_table_info('cards') ORDER BY cid)" \
        2>/dev/null)" || cols=""
    if [ "$cols" != "$PAYMENT_VAULT_COLUMNS" ]; then
        printf 'cards.db does not have the vault card table\n'
        return 1
    fi
    # The key pair init recorded (vault_store's vault_meta): a damaged or
    # swapped key file is refused here too, so backup never ships one over the
    # last good archive.
    meta="$(sqlite3 -readonly "$dir/cards.db" \
        "SELECT group_concat(name, ' ') FROM (SELECT name FROM pragma_table_info('vault_meta') ORDER BY cid)" \
        2>/dev/null)" || meta=""
    if [ "$meta" != "key value" ]; then
        printf 'cards.db does not record the vault keys\n'
        return 1
    fi
    digest="$(sqlite3 -readonly "$dir/cards.db" \
        "SELECT value FROM vault_meta WHERE key = 'identity_sha256'" 2>/dev/null)" || digest=""
    sum="$(sha256sum < "$dir/$key" 2>/dev/null)" || sum=""
    if [ -z "$digest" ] || [ "$digest" != "${sum%% *}" ]; then
        printf '%s is not the key this vault was created with\n' "$key"
        return 1
    fi
    rec="$(sqlite3 -readonly "$dir/cards.db" \
        "SELECT value FROM vault_meta WHERE key = 'recipient'" 2>/dev/null)" || rec=""
    if [ -z "$rec" ] || [ "$rec" != "$(head -n 1 "$dir/recipient.txt")" ]; then
        printf 'recipient.txt is not the public key this vault was created with\n'
        return 1
    fi
    # The checks above give fast, specific reasons; this one is authoritative.
    # The vault's own acceptance (vault_store.verify_set): the whole recipient
    # file with its checksum, the exact table DDL, no other schema objects. A
    # set the vault would refuse is never published over the last good archive.
    # The src/ path must hold vault_store: an empty sys.path entry is the
    # current directory, which -I exists to keep out.
    if [ -z "$_PV_SRC" ] || [ ! -f "$_PV_SRC/genesis/payments/vault_store.py" ]; then
        printf 'the vault check cannot find vault_store under src/\n'
        return 1
    fi
    # The venv's interpreter first (the version the code is written for), then
    # a 3.12, then python3. -I: no user site, no cwd on sys.path.
    local py="$_PV_SRC/../.venv/bin/python" rc=0
    [ -x "$py" ] || py="$(command -v python3.12 || command -v python3 || true)"
    if [ -z "$py" ]; then
        printf 'the vault check needs python3, which is not installed\n'
        return 1
    fi
    why="$("$py" -I - "$_PV_SRC" "$dir" 2>/dev/null <<'PYVERIFY'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from genesis.payments import vault_store
try:
    vault_store.verify_set(Path(sys.argv[2]))
except vault_store.VaultError as exc:
    print(exc)
    sys.exit(1)
PYVERIFY
)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        # A VaultError prints its fixed reason; anything else (an interpreter
        # too old for the code, an import failure, a crash) prints nothing, and
        # its stderr is dropped so no value can reach the output.
        printf '%s\n' "${why:-vault_store could not be loaded or crashed under $py (needs Python 3.11+)}"
        return 1
    fi
    return 0
}

backup_payment_vault() (
    vault="$1" out="$2" tmproot="$3" stage="" fd="" why="" n="" key=""
    archive="$out/$PAYMENT_VAULT_ARCHIVE"
    tmpname="$out/.$PAYMENT_VAULT_ARCHIVE.tmp"
    trap '[ -n "$stage" ] && rm -rf -- "$stage"' EXIT
    say() { printf '%s\n' "$*"; exit 0; }
    prior() { [ -e "$archive" ]; }
    # A symlink here (or at its parent) would send the archive outside the
    # backups repo while the run reported ok.
    if [ -L "$out" ] || [ -L "$(dirname -- "$out")" ]; then
        say failed "the backup directory is a symlink"
    fi
    # The staging leftovers of a killed earlier run: the encrypted temp name in
    # the clone, and the plaintext stage under the temp root (SIGKILL skips the
    # EXIT trap). backup.sh holds its run lock, so no live run owns either.
    rm -f -- "$tmpname" 2>/dev/null
    if [ -e "$tmpname" ] || [ -L "$tmpname" ]; then
        say failed "something other than a file is at $tmpname"
    fi
    for f in "${tmproot%/}"/payment_vault.??????; do
        if [ -d "$f" ] && [ ! -L "$f" ]; then rm -rf -- "$f" 2>/dev/null; fi
    done
    if [ ! -e "$vault" ] && [ ! -L "$vault" ]; then
        prior && say missing "the vault directory is gone"
        say absent
    fi
    if [ -L "$vault" ] || [ ! -d "$vault" ]; then
        say failed "the vault path is not a directory"
    fi
    declare -F encrypt_file >/dev/null || say failed "encrypt_file is not defined"
    exec {fd}<"$vault" || say failed "the vault directory cannot be opened"
    flock -w 30 "$fd" || say failed "the vault stayed locked for 30s (a vault command running?)"
    key="$(payment_vault_key_file "$vault")"
    payment_vault_files "$key"
    if [ ! -e "$vault/$key" ] && [ ! -L "$vault/$key" ]; then
        # Never initialised, or an init killed before its key landed (vault_store
        # treats a key file as the mark of an initialised vault). Cards without
        # a key are a loss, not an empty vault (vault_store refuses that state too).
        if [ -e "$vault/cards.db" ] || [ -L "$vault/cards.db" ]; then
            if [ -L "$vault/cards.db" ] || [ ! -f "$vault/cards.db" ]; then
                say failed "cards.db is not a regular file in a vault with no key"
            fi
            if [ -s "$vault/cards.db" ]; then
                n="$(sqlite3 -readonly "$vault/cards.db" 'SELECT count(*) FROM cards' 2>/dev/null)" \
                    || say failed "cards.db cannot be read in a vault with no key"
                case "$n" in
                    0 | '') ;;
                    *) say failed "cards remain in a vault with no key" ;;
                esac
            fi
        fi
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
    mkdir -m 0700 "$stage/set" 2>/dev/null || say failed "cannot create a temp directory"
    cp -- "$vault/$key" "$vault/recipient.txt" "$stage/set/" 2>/dev/null \
        || say failed "cannot copy the key files"
    # cd keeps the destination a fixed relative name: .backup takes it quoted,
    # and a path holding a quote would break that.
    (cd "$stage/set" && sqlite3 -readonly "$vault/cards.db" ".timeout 10000" ".backup cards.db") \
        >/dev/null 2>&1 || say failed "the cards.db snapshot failed (after an interrupted vault command, run python -m genesis vault status once to roll it back)"
    why="$(payment_vault_check "$stage/set")" || say failed "$why"
    tar -C "$stage/set" --format=ustar -cf "$stage/payment_vault.tar" -- "${PAYMENT_VAULT_FILES[@]}" \
        2>/dev/null || say failed "packing the vault failed"
    encrypt_file "$stage/payment_vault.tar" "$stage/$PAYMENT_VAULT_ARCHIVE" || say failed "encrypting the vault failed"
    mkdir -p "$out" 2>/dev/null || say failed "cannot create the output directory"
    cp -T -- "$stage/$PAYMENT_VAULT_ARCHIVE" "$tmpname" 2>/dev/null || {
        rm -f -- "$tmpname" 2>/dev/null
        say failed "writing the backup copy failed"
    }
    # -T: a directory at the archive path is an error, never a destination to
    # move the archive into (which would report ok with no archive in place).
    mv -f -T -- "$tmpname" "$archive" 2>/dev/null || {
        rm -f -- "$tmpname" 2>/dev/null
        say failed "renaming the archive into place failed"
    }
    if [ -n "${PAYMENT_VAULT_RECORD:-}" ]; then
        printf '%s %s\n' "$key" "$(head -n 1 "$stage/set/recipient.txt" 2>/dev/null)" \
            > "$PAYMENT_VAULT_RECORD" 2>/dev/null || rm -f -- "$PAYMENT_VAULT_RECORD" 2>/dev/null
    fi
    say ok
)

payment_vault_unpack() {
    local tarfile="$1" parent="$2" listing types names dest why key
    local PAYMENT_VAULT_FILES
    listing="$(tar -tvf "$tarfile" 2>/dev/null)" || {
        printf 'the archive cannot be read\n'
        return 1
    }
    # Exactly three entries, each a regular file, named exactly the three
    # names of one set (duplicates kept, so a repeated name cannot pass): the
    # key file identity.key, or identity.age in an archive made before
    # migration. A link or any other member type is refused before anything
    # is written.
    types="$(printf '%s\n' "$listing" | cut -c1 | sort | tr -d '\n')"
    names="$(tar -tf "$tarfile" 2>/dev/null | sort | tr '\n' ' ')"
    case "$names" in
        "cards.db identity.key recipient.txt ") key=identity.key ;;
        "cards.db identity.age recipient.txt ") key=identity.age ;;
        *) key="" ;;
    esac
    if [ "$types" != "---" ] || [ -z "$key" ]; then
        printf 'the archive does not hold exactly the three vault files\n'
        return 1
    fi
    payment_vault_files "$key"
    dest="$(umask 077 && mktemp -d -p "$parent" .payment_vault.XXXXXX 2>/dev/null)" || {
        printf 'cannot create a staging directory\n'
        return 1
    }
    if ! (umask 077 && tar -C "$dest" --no-same-owner --no-same-permissions \
        -xf "$tarfile" -- "${PAYMENT_VAULT_FILES[@]}") 2>/dev/null; then
        rm -rf -- "$dest"
        printf 'the archive could not be unpacked\n'
        return 1
    fi
    if ! why="$(payment_vault_check "$dest")"; then
        rm -rf -- "$dest"
        printf '%s\n' "$why"
        return 1
    fi
    printf '%s\n' "$dest"
}

payment_vault_drop_copies() (
    parent="$(dirname -- "$1")" recipient="${2:-}" removed=0 left=0 d="" f="" plain=""
    for d in "$parent"/payment_vault.pre-migrate-*; do
        [ -e "$d" ] || [ -L "$d" ] || continue  # the unmatched pattern: no copy
        plain=1
        # The name vault migrate writes, a real directory, and the archived
        # vault's key pair (a copy of another vault is never this one's to drop).
        if ! [[ "${d##*/payment_vault.pre-migrate-}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] \
            || [ -L "$d" ] || [ ! -d "$d" ] || [ -z "$recipient" ] \
            || [ -L "$d/recipient.txt" ] || [ ! -f "$d/recipient.txt" ] \
            || [ "$(head -n 1 "$d/recipient.txt" 2>/dev/null)" != "$recipient" ]; then
            plain=0
        else
            for f in "$d"/* "$d"/.[!.]* "$d"/..?*; do
                [ -e "$f" ] || [ -L "$f" ] || continue
                if [ -L "$f" ] || [ ! -f "$f" ]; then plain=0; fi
                case "${f##*/}" in
                    identity.age | recipient.txt | cards.db | cards.db-journal | cards.db-wal | cards.db-shm) ;;
                    *) plain=0 ;;
                esac
            done
        fi
        if [ "$plain" = 1 ] && rm -f -- "$d"/* "$d"/.[!.]* "$d"/..?* 2>/dev/null \
            && rmdir -- "$d" 2>/dev/null; then
            removed=$((removed + 1))
        else
            left=$((left + 1))
        fi
    done
    printf '%s %s\n' "$removed" "$left"
    exit 0
)

payment_vault_count_copies() (
    n=0
    for d in "$(dirname -- "$1")"/payment_vault.pre-migrate-*; do
        if [ -e "$d" ] || [ -L "$d" ]; then n=$((n + 1)); fi
    done
    printf '%s\n' "$n"
)

