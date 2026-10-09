# shellcheck shell=bash
# Immutable transcript objects with authenticated dated inventories.
# Caller holds the host backup/restore lock. Host IDs must be unique.
_pool_python() {
    printf '%s' "$_BACKUP_PASSPHRASE" | python3 "$_SCRIPT_DIR/lib/transcript_archive.py" "$@"
}
_pool_host_safe() { [[ "$1" =~ ^Genesis/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }

transcript_pool_backup() (
    local directory="$1" host="$2" snapshot="$3" work names rc=0 name object size
    _pool_host_safe "$host" || return 1
    work=$(mktemp -d -p "$GENESIS_BIG_TMP" transcript-pool.XXXXXX) || return 1
    trap 'rm -rf -- "$work"' EXIT
    _pool_python pool-manifest "$directory" --destination "$work/manifest.gpg" --snapshot "$snapshot" --scratch "$GENESIS_BIG_TMP" >"$work/rows" || return 1
    backend_mkdir "$host/transcript-objects" || return 1
    # Format marker precedes manifest and objects: incomplete inventory blocks GC.
    printf 'genesis-transcript-pool 1\n' >"$work/POOLED"
    backend_put_atomic "$work/POOLED" "$snapshot/TRANSCRIPT_POOL" || return 1
    backend_put_atomic "$work/manifest.gpg" "$snapshot/transcripts/manifest-v1.json.gpg" || return 1
    names=$(backend_list_strict "$host/transcript-objects") || return 1
    printf '%s\n' "$names" | sed '/^$/d' >"$work/objects"
    python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-missing "$work/rows" --selected "$work/objects" >"$work/missing" || return 1
    while IFS=$'\t' read -r name object size; do
        [ -n "$name" ] || continue
        if python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$directory/$name" --object-id "$object" --size "$size" && backend_put_atomic "$directory/$name" "$host/transcript-objects/$object"; then
            printf 'Transcript pool: uploaded %s\n' "$name" >&2
        else
            printf 'Transcript pool: upload failed for %s\n' "$name" >&2
            rc=1
        fi
    done <"$work/missing"
    # Presence is only an upload optimization. Read every referenced object back
    # before authorizing COMPLETE, including successful new uploads. Never use a
    # cached remote verification as authority for this run.
    while IFS=$'\t' read -r name object size; do
        [ -n "$name" ] || continue
        rm -f -- "$work/remote.gpg" || return 1
        if ! backend_get "$host/transcript-objects/$object" "$work/remote.gpg"; then
            printf 'Transcript pool: remote read failed for %s\n' "$name" >&2
            rc=1
            continue
        fi
        if ! python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$work/remote.gpg" --object-id "$object" --size "$size"; then
            # Repair only from the already validated exact ciphertext; this does
            # not recapture an out-of-scope source or change retained identities.
            if python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$directory/$name" --object-id "$object" --size "$size" \
                && backend_put_atomic "$directory/$name" "$host/transcript-objects/$object" \
                && rm -f -- "$work/remote.gpg" \
                && backend_get "$host/transcript-objects/$object" "$work/remote.gpg" \
                && python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$work/remote.gpg" --object-id "$object" --size "$size"; then
                printf 'Transcript pool: repaired and verified %s\n' "$name" >&2
            else
                printf 'Transcript pool: remote verification failed for %s\n' "$name" >&2
                rc=1
            fi
        fi
    done <"$work/rows"
    # Snapshot metadata must also survive its transfer unchanged.
    backend_get "$snapshot/TRANSCRIPT_POOL" "$work/remote-marker" \
        && cmp -s -- "$work/POOLED" "$work/remote-marker" || rc=1
    backend_get "$snapshot/transcripts/manifest-v1.json.gpg" "$work/remote-manifest.gpg" \
        && cmp -s -- "$work/manifest.gpg" "$work/remote-manifest.gpg" || rc=1
    return "$rc"
)

transcript_pool_pull() (
    local host="$1" snapshot="$2" destination="$3" cache="${4:-}" work name object size stage payload manifest_cache="" cached=false rc=0
    _pool_host_safe "$host" || return 1
    work=$(mktemp -d -p "$GENESIS_BIG_TMP" transcript-pool.XXXXXX) || return 1
    trap 'rm -rf -- "$work"' EXIT
    if [ -n "$cache" ]; then
        # The caller binds this private cache to the backend and selected snapshot
        # and holds the restore lock. Retire only unpublished tool-owned stages.
        [ ! -L "$cache" ] && mkdir -p -- "$cache" || return 1
        rm -f -- "$cache"/.pool-download.* "$cache"/.manifest-cache.* || return 1
        manifest_cache="$cache/manifest-$(printf '%s' "$snapshot" | sha256sum | cut -d' ' -f1).enc"
    fi
    # A prior authenticated snapshot manifest can survive a transport outage;
    # every use still authenticates its current key and exact snapshot binding.
    if backend_get "$snapshot/transcripts/manifest-v1.json.gpg" "$work/manifest.gpg"; then
        _pool_python pool-read "$work/manifest.gpg" --snapshot "$snapshot" --scratch "$GENESIS_BIG_TMP" >"$work/rows" || return 1
        if [ -n "$manifest_cache" ]; then
            stage=$(mktemp -p "$cache" .manifest-cache.XXXXXX) || return 1
            if cp -- "$work/manifest.gpg" "$stage" && cmp -s -- "$work/manifest.gpg" "$stage" && mv -f -- "$stage" "$manifest_cache"; then
                :
            else
                rm -f -- "$stage"
                return 1
            fi
        fi
    elif [ -n "$manifest_cache" ] && [ ! -L "$manifest_cache" ] && [ -f "$manifest_cache" ]; then
        _pool_python pool-read "$manifest_cache" --snapshot "$snapshot" --scratch "$GENESIS_BIG_TMP" >"$work/rows" || return 1
        cached=true
    else
        return 1
    fi
    if $cached; then printf 'Transcript pool: using authenticated cached snapshot manifest\n' >&2; fi
    mkdir -p -- "$destination" || return 1
    while IFS=$'\t' read -r name object size; do
        [ -n "$name" ] || continue
        payload="$work/object.gpg"
        if [ -n "$cache" ]; then payload="$cache/$object"; fi
        if [ -z "$cache" ] || ! python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$payload" --object-id "$object" --size "$size" >/dev/null 2>&1; then
            stage=$(mktemp -p "${cache:-$work}" .pool-download.XXXXXX) || return 1
            if backend_get "$host/transcript-objects/$object" "$stage" && python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$stage" --object-id "$object" --size "$size" && mv -f -- "$stage" "$payload"; then
                :
            else
                printf 'Transcript pool: missing or corrupt object for %s\n' "$name" >&2
                rm -f -- "$stage"
                rc=1
                continue
            fi
        fi
        stage=$(mktemp -p "$destination" .pool-download.XXXXXX) || return 1
        if cp -- "$payload" "$stage" && python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-verify "$stage" --object-id "$object" --size "$size" && mv -f -- "$stage" "$destination/$name"; then
            printf '%s\n' "$name"
        else
            rc=1
        fi
        rm -f -- "$stage"
    done <"$work/rows"
    return "$rc"
)

transcript_pool_gc() (
    local host="$1" current="$2" work directories children payloads object snapshot pooled rc=0
    _pool_host_safe "$host" || return 1
    work=$(mktemp -d -p "$GENESIS_BIG_TMP" transcript-pool-gc.XXXXXX) || return 1
    trap 'rm -rf -- "$work"' EXIT
    directories=$(backend_list_dirs_strict "$host") || return 1
    grep -Fxq -- "$current" <<<"$directories" || return 1
    : >"$work/references"
    for snapshot in $directories; do
        [[ "$snapshot" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
        children=$(backend_list_strict "$host/$snapshot") || return 1
        pooled=false
        if grep -Fxq TRANSCRIPT_POOL <<<"$children"; then pooled=true; fi
        if grep -Fxq COMPLETE <<<"$children"; then
            rm -f "$work/complete"
            backend_get "$host/$snapshot/COMPLETE" "$work/complete" || return 1
            [ -f "$work/complete" ] || return 1
            if grep -Fxq 'transcript-pool 1' "$work/complete"; then pooled=true; fi
        fi
        [ "$snapshot" != "$current" ] || pooled=true
        rc=0
        payloads=$(backend_list_strict "$host/$snapshot/transcripts") || rc=$?
        if [ "$rc" -eq 3 ]; then
            $pooled && return 1
            continue # legacy snapshot with no transcript payload
        fi
        [ "$rc" -eq 0 ] || return 1
        if grep -Fxq manifest-v1.json.gpg <<<"$payloads"; then
            backend_get "$host/$snapshot/transcripts/manifest-v1.json.gpg" "$work/manifest.gpg" || return 1
            _pool_python pool-read "$work/manifest.gpg" --snapshot "$host/$snapshot" --scratch "$GENESIS_BIG_TMP" >>"$work/references" || return 1
        elif $pooled || grep -Fxq POOLED <<<"$payloads"; then
            return 1 # incomplete pooled snapshot: unknown references, no deletion
        fi
    done
    backend_list_strict "$host/transcript-objects" >"$work/objects" || return 1
    python3 "$_SCRIPT_DIR/lib/transcript_archive.py" pool-gc "$work/references" --selected "$work/objects" >"$work/delete" || return 1
    while read -r object; do
        [ -n "$object" ] || continue
        backend_delete "$host/transcript-objects/$object" || return 1
    done <"$work/delete"
)
