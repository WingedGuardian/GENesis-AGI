# shellcheck shell=bash
# shellcheck disable=SC2154  # _git_dir and _SERVING_COMMIT_PY are the caller's (below)
# deploy_status.sh — what genesis-server runs, read without changing anything.
#
# Sourced by scripts/deploy_code_only.sh, for its status mode (the validation
# bracket's reading) and for its deploy decisions. Every git call here passes
# --no-optional-locks: status runs beside other sessions' deploys and must not
# take the index lock a merge needs.
#
# The caller sets:
#   GENESIS_ROOT         the checkout
#   _git_dir             its git directory (the reflog is <that>/logs/HEAD)
#   _SERVING_COMMIT_PY   the source of scripts/lib/serving_commit.py, read at
#                        the caller's start so a merge cannot swap it mid-run
#
# The paths the server loads: the editable install imports src/ from disk,
# startup reads config/, and pyproject.toml is the install's own metadata.
_RUNTIME_PATHS=(src config pyproject.toml)

_git_ro() { git --no-optional-locks -C "$GENESIS_ROOT" "$@"; }

# Does genesis-server run in THIS checkout? The unit fixes its working directory
# (and the secrets.env beside it) separately from its python, so a unit left
# behind by a moved checkout can run this venv from another tree. Unreadable is
# "no". Sets _UNIT_DIR.
_unit_dir_is_ours() {
    _UNIT_DIR="$(systemctl --user show genesis-server -p WorkingDirectory --value 2>/dev/null || true)"
    [ -n "$_UNIT_DIR" ] && [ "$(realpath -m "$_UNIT_DIR")" = "$(realpath -m "$GENESIS_ROOT")" ]
}

# Sets SERVING to the commit the server booted from, or empty with SERVING_WHY
# saying why it is unknown, and SERVING_HELD to that commit and every commit HEAD
# has held since, one per line. It is read from HEAD's reflog at the unit's start
# time (ActiveState and the start come from `systemctl show`, never `is-active`,
# and no date is parsed: --timestamp=unix). It is the tree at boot, as far as
# git records it: an uncommitted edit present at boot and reverted since is
# invisible to it.
_read_serving() {
    local state boot cutoff out rc=0 crc=0
    SERVING=""
    SERVING_WHY=""
    SERVING_HELD=""
    state="$(systemctl --user show genesis-server -p ActiveState --value 2>/dev/null || true)"
    if [ "$state" != active ]; then
        SERVING_WHY="genesis-server is not running (${state:-state unreadable})"
        return 0
    fi
    # This checkout's reflog says what THIS checkout held; a unit running in
    # another directory booted from that one's.
    if ! _unit_dir_is_ours; then
        SERVING_WHY="genesis-server runs in ${_UNIT_DIR:-an unreadable working directory}, not $GENESIS_ROOT"
        return 0
    fi
    boot="$(systemctl --user show genesis-server -p ActiveEnterTimestamp --timestamp=unix --value 2>/dev/null || true)"
    boot="${boot#@}"
    case "$boot" in
        ''|*[!0-9]*)
            SERVING_WHY="cannot read genesis-server's start time in unix seconds (--timestamp=unix needs systemd 248 or newer)"
            return 0 ;;
    esac
    # Before this cutoff git may have expired unreachable reflog entries (a
    # detour, as a pair). git resolves the setting itself; unset means its
    # 30-day default (git exits 1), and a value it cannot read (git exits 128)
    # leaves the cutoff empty, which the reader answers as unknown.
    cutoff="$(_git_ro config --type=expiry-date gc.reflogExpireUnreachable 2>/dev/null)" || crc=$?
    case "$crc" in
        0) ;;
        1) cutoff=$(( $(date +%s) - 30 * 86400 )) ;;
        *) cutoff="" ;;
    esac
    out="$(python3 -c "$_SERVING_COMMIT_PY" --held "$_git_dir/logs/HEAD" "$boot" \
        "$(_git_ro rev-parse HEAD)" "$cutoff" 2>/dev/null)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
        SERVING="${out%%$'\n'*}"
        SERVING_HELD="$out"
    else
        SERVING_WHY="${out#unknown: }"
        [ -n "$SERVING_WHY" ] || SERVING_WHY="the reflog reader failed"
    fi
}

# _runtime_same <a> <b>: do two commits hold the same files under what the
# server loads? Exit 0 same, 1 different, 2 cannot tell. A commit that changes
# only docs or hooks needs no restart, and does not change what a validation
# runs against.
_runtime_same() {
    local rc=0
    _git_ro diff --quiet "$1" "$2" -- "${_RUNTIME_PATHS[@]}" 2>/dev/null || rc=$?
    case "$rc" in
        0) return 0 ;;
        1) return 1 ;;
        *) return 2 ;;
    esac
}

# _runtime_held <commit>: are the files the server loads still the ones it booted
# from, at <commit> AND at every commit HEAD has held since the boot? The server
# imports src/ lazily, so a module can load from any tree HEAD held, and a later
# commit that restores the boot's files does not unload it: the two ends alone
# cannot say. Exit 0 yes, 1 no (_HELD_OTHER names the commit that differs, and
# _HELD_DETOUR is set when it is one HEAD held on the way, not <commit>), 2 cannot
# tell. Expects _read_serving to have found SERVING.
_runtime_held() {
    local c rc unsure=""
    _HELD_OTHER=""
    _HELD_DETOUR=""
    rc=0
    _runtime_same "$SERVING" "$1" || rc=$?
    case "$rc" in
        0) ;;
        1) _HELD_OTHER="$1"; return 1 ;;
        *) unsure=1 ;;
    esac
    while IFS= read -r c; do
        [ -n "$c" ] || continue
        rc=0
        _runtime_same "$SERVING" "$c" || rc=$?
        case "$rc" in
            0) ;;
            1) _HELD_OTHER="$c"; _HELD_DETOUR=1; return 1 ;;
            *) unsure=1 ;;
        esac
    done <<< "$SERVING_HELD"
    [ -z "$unsure" ] || return 2
}

# The server-loaded changes the running server has not loaded, between the
# commit it booted from and HEAD. When that commit is unknown, <fallback-from>
# (this run's pull range) is used instead, and said so. Addressed to the session
# that ran the command: it names the next step and pages nobody.
# shellcheck disable=SC2120  # the fallback is optional: status passes none
_report_pending() {
    local fallback_from="${1:-}" head from changed
    head="$(_git_ro rev-parse HEAD)"
    _read_serving
    if [ -n "$SERVING" ]; then
        echo "  The server booted from $SERVING; the tree is at $head."
        from="$SERVING"
    else
        echo "  The commit the server booted from is unknown: $SERVING_WHY."
        if [ -n "$fallback_from" ] && [ "$fallback_from" != "$head" ]; then
            echo "  Showing what this pull changed instead ($fallback_from..$head)."
            from="$fallback_from"
        else
            return 0
        fi
    fi
    changed="$(_git_ro diff --no-renames --name-only "$from" "$head" -- "${_RUNTIME_PATHS[@]}")" \
        || { echo "  NOTE: cannot list the changes since $from."; return 0; }
    if [ -z "$changed" ]; then
        if [ -n "$SERVING" ]; then
            local rc=0
            _runtime_held "$head" || rc=$?
            if [ "$rc" -eq 1 ]; then
                echo "  The files the server loads match its boot commit again, but since the boot"
                echo "  HEAD has held other runtime files (at ${_HELD_OTHER:0:12}). The server imports src/"
                echo "  lazily, so it may run some of them until it restarts."
                echo "  Next step, once no validation holds the lock: scripts/deploy_code_only.sh restart"
                return 0
            elif [ "$rc" -ne 0 ]; then
                echo "  NOTE: cannot compare every commit HEAD has held since the boot with the boot commit."
                return 0
            fi
        fi
        echo "  Nothing the server loads (src/, config/, pyproject.toml) has changed since then."
        return 0
    fi
    echo "  PENDING: the running server has not loaded these changes. It imports src/ lazily,"
    echo "  so until it restarts it can run a mix of old and new code:"
    echo "$changed" | sed 's/^/          /'
    echo "  Next step, once no validation holds the lock: scripts/deploy_code_only.sh restart"
    echo "  (launch it detached; the header of the script has the command)."
}

# Uncommitted edits to what the server loads: a tracked edit or an untracked
# module there runs without moving HEAD. Prints "none", the first paths, or
# "unreadable".
_runtime_edits() {
    local st
    st="$(_git_ro status --porcelain --no-renames --untracked-files=all \
        -- "${_RUNTIME_PATHS[@]}" 2>/dev/null)" || { echo unreadable; return 0; }
    if [ -z "$st" ]; then
        echo none
    else
        printf '%s paths: %s\n' "$(printf '%s\n' "$st" | wc -l)" \
            "$(printf '%s\n' "$st" | cut -c4- | head -n 5 | paste -sd ' ' -)"
    fi
}

# The IGNORED files under what the server loads — a config/*.local.yaml override,
# a local module — which git status never lists, so runtime-edits cannot see an
# edit to one. Prints "none", "<n> files, <fingerprint of paths and contents>", or
# "unreadable". Left out: bytecode, and the files the running server rewrites on
# its own schedule (.gitignore marks them generated: the procedure trigger cache
# hourly, the two identity syntheses, the generated MCP configs). With them in,
# every validation longer than an hour would read as changed. A file the server
# starts rewriting later fails safe: validations read invalid, never valid.
# Also in: the user-directory overlays (<name>.local.yaml under ~/.genesis/config),
# which the loaders PREFER to the checkout's (src/genesis/_config_overlay.py,
# _resolve_overlay_path), where the dashboard's settings writes land, and which
# some consumers reread live. Only those: other files the server reads from that
# directory (a user outreach.yaml, genesis.yaml, modules/) are NOT fingerprinted,
# and the header of deploy_code_only.sh lists them among what the bracket cannot
# see. A dangling overlay link is skipped, as the loader's is_file() skips it.
_RUNTIME_OVERRIDES_SKIP='(^|/)__pycache__/|\.py[co]$|^config/\.generated/|^config/procedure_triggers\.(yaml|json)$|^src/genesis/identity/(USER_KNOWLEDGE|TRIAGE_CALIBRATION)\.md$'
_USER_OVERLAY_DIR="$HOME/.genesis/config"
_runtime_overrides() {
    local raw list hashes overlays=""
    raw="$(_git_ro ls-files -z --others --ignored --exclude-standard \
        -- "${_RUNTIME_PATHS[@]}" 2>/dev/null | tr '\0' '\n')" || { echo unreadable; return 0; }
    if [ -d "$_USER_OVERLAY_DIR" ]; then
        overlays="$(find -L "$_USER_OVERLAY_DIR" -maxdepth 1 -name '*.local.yaml' -type f \
            2>/dev/null)" || { echo unreadable; return 0; }
    fi
    list="$( { printf '%s\n' "$raw" | grep -vE "$_RUNTIME_OVERRIDES_SKIP"; printf '%s\n' "$overlays"; } \
        | grep -v '^$' | LC_ALL=C sort || true)"
    if [ -z "$list" ]; then
        echo none
        return 0
    fi
    hashes="$(printf '%s\n' "$list" | _git_ro hash-object --stdin-paths 2>/dev/null)" \
        || { echo unreadable; return 0; }
    printf '%s files, %s\n' "$(printf '%s\n' "$list" | wc -l)" \
        "$(paste -d ' ' <(printf '%s\n' "$hashes") <(printf '%s\n' "$list") | sha256sum | cut -c1-16)"
}

# ── The validation bracket ────────────────────────────────────────────
# A validation against the live server takes a token from `status` at its start
# and runs `status --verify <token>` at its end. The token names what the server
# runs, and exists only when a validation can run against it: the server is up,
# its boot commit is known, HEAD's runtime files are the ones it booted from (and
# were at every commit HEAD held since the boot: the server imports lazily, so a
# detour's modules can still be loaded after a commit restores the files), no
# uncommitted edit sits under what it loads, and every field is readable.
# Otherwise it is "unknown (<why>)", which no token equals. The same token at the
# end means nothing the server runs changed: no restart (boot commit, MainPID,
# systemd invocation, which a reused pid cannot fake), no change to the runtime
# files (HEAD may move over docs or hooks), no edit to an ignored override or a
# user overlay. A TRIPWIRE, not a certificate: what the header of
# deploy_code_only.sh lists as unseen (the venv's packages, the non-overlay
# files in ~/.genesis/config, an edit undone without moving HEAD, a rewritten or
# backdated reflog) reads valid.
# Sets BRACKET, or empty with BRACKET_WHY. Expects _read_serving to have run.
_bracket() {
    local mainpid inv edits overrides rc=0
    BRACKET=""
    BRACKET_WHY=""
    if [ -z "$SERVING" ]; then
        BRACKET_WHY="the boot commit is unknown: $SERVING_WHY"
        return 0
    fi
    mainpid="$(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || true)"
    inv="$(systemctl --user show genesis-server -p InvocationID --value 2>/dev/null || true)"
    case "$mainpid" in ''|0|*[!0-9]*) BRACKET_WHY="the server's MainPID is unreadable"; return 0 ;; esac
    [ -n "$inv" ] || { BRACKET_WHY="the server's invocation id is unreadable"; return 0; }
    edits="$(_runtime_edits)"
    [ "$edits" = none ] || { BRACKET_WHY="uncommitted runtime edits: $edits"; return 0; }
    overrides="$(_runtime_overrides)"
    [ "$overrides" != unreadable ] || { BRACKET_WHY="the ignored runtime overrides are unreadable"; return 0; }
    _runtime_held HEAD || rc=$?
    case "$rc" in
        0) ;;
        1)
            if [ -n "$_HELD_DETOUR" ]; then
                BRACKET_WHY="since the boot, HEAD has held other runtime files (at ${_HELD_OTHER:0:12}), which the server imports lazily and may run (restart first)"
            else
                BRACKET_WHY="HEAD's runtime files differ from the ones the server booted from (restart first)"
            fi
            return 0 ;;
        *) BRACKET_WHY="cannot compare the runtime files HEAD has held since the boot with the boot commit's"; return 0 ;;
    esac
    BRACKET="b1-$(printf '%s\n' "$SERVING" "$mainpid" "$inv" "$overrides" | sha256sum | cut -c1-24)"
}

# `status [--verify <token>]`. Prints the fields a human reads and the bracket;
# with --verify, exits 0 when the token still holds and 1 when it does not.
_status_main() {
    local verify="$1" inv
    _read_serving
    echo "serving: ${SERVING:-unknown ($SERVING_WHY)}"
    echo "head: $(_git_ro rev-parse HEAD)"
    echo "mainpid: $(systemctl --user show genesis-server -p MainPID --value 2>/dev/null || echo unknown)"
    inv="$(systemctl --user show genesis-server -p InvocationID --value 2>/dev/null || true)"
    echo "invocation: ${inv:-unknown}"
    echo "runtime-edits: $(_runtime_edits)"
    echo "runtime-overrides: $(_runtime_overrides)"
    _bracket
    if [ -z "$verify" ]; then
        echo "bracket: ${BRACKET:-unknown ($BRACKET_WHY)}"
        _report_pending
        return 0
    fi
    if [ -z "$BRACKET" ]; then
        echo "bracket: INVALID — now $BRACKET_WHY"
        return 1
    fi
    if [ "$BRACKET" != "$verify" ]; then
        echo "bracket: INVALID — since that token the server restarted, or what it runs changed (now $BRACKET)"
        return 1
    fi
    echo "bracket: valid — nothing the server runs changed since the token"
}
