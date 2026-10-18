# shellcheck shell=bash
# Publication uses the caller's DR lock, backend, passphrase and EXIT trap.

_publication_python() {
    local rc=0
    printf '%s' "$_BACKUP_PASSPHRASE" | python3 "$_SCRIPT_DIR/lib/transcript_publication.py" "$@" \
        --spool "$_PUBLICATION_SPOOL" --scratch "$GENESIS_BIG_TMP" \
        --session "$_PUBLICATION_WORK/session.json" --binding "$_PUBLICATION_WORK/binding.json" \
        --deadline "$_BACKUP_DEADLINE" || rc=$?
    # A failed cryptographic/durability boundary stops authority updates here,
    # including the caller's EXIT checkpoint; retry authenticates visible state.
    if [ "$rc" -ne 0 ]; then _PUBLICATION_ACTIVE=false; fi
    return "$rc"
}

_publication_summary() {
    _publication_python summary >"$_PUBLICATION_WORK/summary.json" || return 1
    _PUBLICATION_SUMMARY=$(cat "$_PUBLICATION_WORK/summary.json")
    _T2_STAMP=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["snapshot"].rsplit("/",1)[1])' "$_PUBLICATION_WORK/summary.json")
    log "Off-site original snapshot (newer inputs deferred): $_PUBLICATION_SUMMARY"
}

publication_checkpoint() {
    [ "${_PUBLICATION_ACTIVE:-false}" = true ] || return 0
    _publication_python checkpoint || return 1
    _publication_summary
}

publication_apply_gaps() {
    local gaps=() i
    _publication_python gaps >"$_PUBLICATION_WORK/gaps" || return 1
    mapfile -d '' -t gaps <"$_PUBLICATION_WORK/gaps"
    _EXTRA_SKIP_LABELS=(); _EXTRA_PARTIAL_LABELS=()
    _EXTRA_UPLOAD_FAILED=0; _EXTRA_SKIPPED=0; _EXTRA_PARTIAL=0
    for ((i=0; i<${#gaps[@]}; i+=2)); do
        case "${gaps[i]}" in
            skipped) _EXTRA_SKIP_LABELS+=("${gaps[i+1]}"); _EXTRA_SKIPPED=$((_EXTRA_SKIPPED+1)) ;;
            partial) _EXTRA_PARTIAL_LABELS+=("${gaps[i+1]}"); _EXTRA_PARTIAL=$((_EXTRA_PARTIAL+1)) ;;
            upload-failed) _EXTRA_SKIP_LABELS+=("${gaps[i+1]}"); _EXTRA_UPLOAD_FAILED=$((_EXTRA_UPLOAD_FAILED+1)) ;;
            *) return 1 ;;
        esac
    done
}

_publication_namespace() {
    local directory rc
    _publication_python directories >"$_PUBLICATION_WORK/directories" || return 1
    while IFS= read -r -d '' directory; do
        rc=0
        backend_list_strict "$directory" >"$_PUBLICATION_WORK/listing" || rc=$?
        case "$rc" in
            3) continue ;;
            0) _publication_python check-list --directory "$directory" --listing "$_PUBLICATION_WORK/listing" || return 1 ;;
            *) return 1 ;;
        esac
    done <"$_PUBLICATION_WORK/directories"
}

_publication_acknowledge() {
    local snapshot="$1"
    _publication_python marker || return 1
    backend_get "$snapshot/COMPLETE" "$_PUBLICATION_WORK/ack" || return 1
    _publication_python retire --received "$_PUBLICATION_WORK/ack" || return 1
    _publication_summary || return 1
    _PUBLICATION_ACTIVE=false
    return 0
}

publication_run() {
    local mode="$1" snapshot="$2" remaining rc action row source remote download failure operation=()
    local _BACKEND_DEADLINE="$_BACKUP_DEADLINE"
    local -A uploaded=()
    _PUBLICATION_ACTIVE=false
    _PUBLICATION_WORK=$(mktemp -d -p "$_BACKUP_READBACK_DIR" publication.XXXXXX) || return 1
    python3 - "$_PUBLICATION_WORK/binding.json" "$_BACKEND" \
        "${_BACKEND_LOCAL_ROOT:-}${_BACKEND_NAS:-}" "$_T2_HOST_DIR" \
        "${GENESIS_BACKUP_TRANSCRIPT_SCOPE:-main}" "$BACKUP_DIR" <<'PY' || return 1
import json,sys
from pathlib import Path
path,backend,root,host,scope,installation=sys.argv[1:]
if backend == "local": root=str(Path(root).resolve())
Path(path).write_text(json.dumps(dict(backend=backend,root=root,host=host,scope=scope,installation=installation)))
PY
    if [ "$mode" = open ]; then
        _publication_python open --backup "$BACKUP_DIR" || return 1
    else
        printf '%s\0' "${_EXTRA_BUILT[@]+"${_EXTRA_BUILT[@]}"}" >"$_PUBLICATION_WORK/extras"
        _publication_python create --backup "$BACKUP_DIR" --snapshot "$snapshot" \
            --qdrant "$_QDRANT_FRESH" --extras "$_PUBLICATION_WORK/extras" \
            --vault-status "$_PV_STATUS" --vault-archive "$PAYMENT_VAULT_ARCHIVE" \
            --vault-last-good "$PAYMENT_VAULT_LAST_GOOD" || return 1
    fi
    _PUBLICATION_ACTIVE=true
    _publication_summary || return 1
    snapshot="$_T2_HOST_DIR/$_T2_STAMP"
    _publication_namespace || return 1
    # A published recovery point is immutable, including after a lost ack.
    rc=0
    backend_list_strict "$snapshot" >"$_PUBLICATION_WORK/root-listing" || rc=$?
    if [ "$rc" -eq 0 ] && grep -Fxq COMPLETE "$_PUBLICATION_WORK/root-listing"; then
        _publication_acknowledge "$snapshot"
        return "$?"
    elif [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
        return 1
    fi
    while :; do
        remaining=$(python3 -c 'import sys,time;print(max(0,int(float(sys.argv[1])-time.monotonic()-25)))' "$_BACKUP_DEADLINE") || return 1
        [ "$remaining" -gt 0 ] || { publication_checkpoint; return 1; }
        _publication_python next >"$_PUBLICATION_WORK/operation" || return 1
        mapfile -d '' -t operation <"$_PUBLICATION_WORK/operation"
        action="${operation[0]}"
        case "$action" in
            claim)
                rc=0
                backend_list_strict "$snapshot" >/dev/null || rc=$?
                [ "$rc" -eq 3 ] || return 1
                # Intent becomes durable BEFORE the first remote mutation.
                _publication_python claim || return 1
                for directory in data qdrant transcripts; do
                    backend_mkdir "$snapshot/$directory" || return 1
                done
                ;;
            put|get|compare)
                row="${operation[1]}"; source="${operation[2]}"
                remote="${operation[3]}"; download="${operation[4]}"
                if [ "$action" = put ]; then
                    # One whole-file attempt per row/run; persistent corruption
                    # or a misacknowledging backend cannot spin for two hours.
                    [ -z "${uploaded[$row]:-}" ] || return 1
                    uploaded[$row]=true
                    backend_mkdir "$(dirname "$remote")" || return 1
                    # Metadata present after a killed initialization must match
                    # the frozen bytes; never adopt a conflicting inventory.
                    case "$remote" in
                        "$snapshot/TRANSCRIPT_POOL"|"$snapshot/transcripts/manifest-v1.json.gpg")
                            backend_list_strict "$(dirname "$remote")" >"$_PUBLICATION_WORK/listing" || return 1
                            if grep -Fxq -- "$(basename "$remote")" "$_PUBLICATION_WORK/listing"; then
                                backend_get "$remote" "$download" && cmp -s -- "$source" "$download" || return 1
                                _publication_python put --row "$row" --ok || return 1
                                _publication_python compare --row "$row" --ok || return 1
                                continue
                            fi
                            ;;
                    esac
                    if backend_put_atomic "$source" "$remote"; then
                        _publication_python put --row "$row" --ok || return 1
                    else
                        failure=$(_publication_python put --row "$row") || return 1
                        [ "$failure" != optional ] || continue
                        publication_checkpoint || true
                        return 1
                    fi
                else
                    rc=0
                    if [ "$action" = get ]; then
                        backend_get_resume "$remote" "$download" || rc=$?
                    fi
                    if [ "$rc" -eq 0 ]; then
                        _publication_python compare --row "$row" --ok || return 1
                    else
                        _publication_python compare --row "$row" || return 1
                    fi
                fi
                ;;
            marker)
                # next durably checkpointed every receipt and exact marker intent.
                backend_list_strict "$snapshot" >"$_PUBLICATION_WORK/root-listing" || return 1
                if grep -Fxq COMPLETE "$_PUBLICATION_WORK/root-listing"; then
                    _publication_acknowledge "$snapshot"
                    return "$?"
                fi
                backend_put_atomic "${operation[1]}" "${operation[2]}" "$_BACKUP_READBACK_DIR" || return 1
                _publication_acknowledge "$snapshot"
                return "$?"
                ;;
            *) return 1 ;;
        esac
    done
}
