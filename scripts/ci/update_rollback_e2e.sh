#!/usr/bin/env bash
# Forced update-rollback end-to-end check, for a DISPOSABLE CI runner only.
#
# Used by .github/workflows/update-rollback-test.yml after scripts/install.sh has
# installed Genesis at ~/genesis. It runs scripts/update.sh for real, several
# times, against a local bare "origin", and checks what the rollback promises:
# the exact pre-update packages come back (update.sh "Restored the pre-update
# packages."), the database snapshot is restored through SQLite while a Genesis
# MCP server holds it open, and the server starts and writes again.
#
# NEVER run this on a real install. update.sh stops and restarts the user's
# services, rewrites the venv and restores the database; this script also edits
# ~/genesis/secrets.env and repoints ~/genesis's origin. It refuses to start
# unless GITHUB_ACTIONS=true and GENESIS_CI_DISPOSABLE=1 are both set.
#
# Usage: update_rollback_e2e.sh {setup|noop|clean|fault|mutant}
# Each phase appends "RESULT <phase> <name> <PASS|FAIL|INFO> <detail>" lines to
# $CI_EV/summary.txt and exits 1 on any FAIL. Evidence lands in $CI_EV.
set -euo pipefail

if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${GENESIS_CI_DISPOSABLE:-}" != "1" ]; then
    echo "refusing: this runs update.sh for real; disposable CI runners only" >&2
    exit 64
fi

G="$HOME/genesis"
CI_ROOT="$HOME/ci"
CI_EV="${CI_EV:-$CI_ROOT/evidence}"
PY="$G/.venv/bin/python"
DB="$G/data/genesis.db"
DEPS_DIR="$HOME/.genesis/update-deps"
PROBE="ci_rollback_probe"
mkdir -p "$CI_ROOT" "$CI_EV"
FAILS=0
# An aborted phase must not leave the reader or sampler running into the next step.
trap 'touch "$CI_ROOT/reader-stop" "$CI_ROOT/sampler-stop" 2>/dev/null || true' EXIT

result() { # phase name verdict detail...
    local phase="$1" name="$2" verdict="$3"
    shift 3
    echo "RESULT $phase $name $verdict $*" | tee -a "$CI_EV/summary.txt"
    case "$verdict $*" in
        "INFO LEAK"* | "INFO INCONCLUSIVE"* | "INFO INVALID"* | "INFO NO:"*) echo "::warning title=update-rollback $phase::$name: $*" ;;
        "INFO "*) echo "::notice title=update-rollback $phase::$name: $*" ;;
        FAIL*) echo "::error title=update-rollback $phase::$name: $*" ;;
    esac
    [ "$verdict" != FAIL ] || FAILS=$((FAILS + 1))
}
check() { # phase name detail -- command...
    local phase="$1" name="$2" detail="$3"
    shift 4
    if "$@" >/dev/null 2>&1; then result "$phase" "$name" PASS "$detail"; else result "$phase" "$name" FAIL "$detail"; fi
}
finish() {
    echo "--- $1: $FAILS failure(s)"
    df -h "$HOME" | tail -1
    [ "$FAILS" -eq 0 ]
}

db_ro() { # SQL -> rows (read-only; never writes the database under test)
    "$PY" -I - "$DB" "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
for row in con.execute(sys.argv[2]):
    print("|".join("" if v is None else str(v) for v in row))
PY
}

freeze() { "$G/.venv/bin/pip" freeze --all --exclude-editable 2>/dev/null | LC_ALL=C sort; }

wait_browser_stack() {
    # The rollback waits at most 600 s on the browser-stack lock and then
    # reports the packages NOT restored: never let install's provisioning run
    # overlap an update phase.
    local _
    for _ in $(seq 1 180); do
        case "$(systemctl --user is-active genesis-browser-stack.service 2>/dev/null || true)" in
            active | activating | reloading) sleep 10 ;;
            *) break ;;
        esac
    done
    if flock -n "$HOME/.genesis/locks/browser-stack.lock" true 2>/dev/null; then
        echo "browser-stack lock free"
    else
        echo "browser-stack lock still held after waiting"
        echo "::warning title=update-rollback::the browser-stack lock was still held when an update phase started"
    fi
}

run_update() { # label -> sets UPD_RC; log in $CI_EV/update-<label>.log
    local label="$1"
    wait_browser_stack | tee -a "$CI_EV/update-$label.log"
    UPD_RC=0
    (cd "$G" && GENESIS_DEPLOY_HEALTH_WINDOW_SECS=180 ./scripts/update.sh) \
        >>"$CI_EV/update-$label.log" 2>&1 || UPD_RC=$?
    echo "update.sh ($label) exit $UPD_RC" | tee -a "$CI_EV/update-$label.log"
    cp -f "$HOME/.genesis/last_rollback_source.json" "$CI_EV/last_rollback_source-$label.json" 2>/dev/null || true
}

push_commit() { # message (commits whatever is staged in $CI_ROOT/work, pushes to origin main)
    git -C "$CI_ROOT/work" -c user.name=ci -c user.email=ci@example.invalid commit -q -m "$1"
    git -C "$CI_ROOT/work" push -q origin HEAD:main
}

case "${1:-}" in
setup)
    P=setup
    check $P cc_context_flag "the MCP servers serve tools only with this flag" -- test -e "$HOME/.genesis/cc_context_enabled"
    check $P tree_clean "install left no tracked change (update.sh refuses a dirty tree)" -- \
        test -z "$(git -C "$G" status --porcelain --untracked-files=no)"
    # Local bare origin; update.sh fetches refs/heads/main from the pinned remote.
    git clone -q --bare "$G" "$CI_ROOT/origin.git"
    git -C "$G" remote set-url origin "file://$CI_ROOT/origin.git"
    git -C "$G" fetch -q origin
    git -C "$G" branch -q --set-upstream-to=origin/main main
    git clone -q "$CI_ROOT/origin.git" "$CI_ROOT/work"
    # A backups remote of its own: the pre-update backup gate refuses an update
    # without a verified backup. secrets.env re-exports its own (empty) values
    # over the environment, so the values go INTO the file.
    git init -q --bare "$CI_ROOT/backups.git"
    pass="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
    echo "::add-mask::$pass"
    "$PY" -I - "$G/secrets.env" "$pass" "file://$CI_ROOT/backups.git" <<'PY'
import sys
path, passphrase, repo = sys.argv[1:4]
lines = open(path).read().splitlines() if __import__("os").path.exists(path) else []
want = {"GENESIS_BACKUP_PASSPHRASE": passphrase, "GENESIS_BACKUP_REPO": repo}
out = [l for l in lines if l.split("=", 1)[0] not in want]
out += [f"{k}={v}" for k, v in want.items()]
open(path, "w").write("\n".join(out) + "\n")
PY
    rc=0
    (cd "$G" && ./scripts/backup.sh) >"$CI_EV/backup-setup.log" 2>&1 || rc=$?
    check $P backup_runs "a manual backup succeeds against the local backups remote (exit $rc)" -- test "$rc" -eq 0
    check $P backup_verified "the backup gate's two fields: healthy + sqlite_backup_verified" -- \
        "$PY" -c "import json,sys; s=json.load(open(sys.argv[1])); sys.exit(0 if s.get('db_integrity_status')=='healthy' and s.get('sqlite_backup_verified') is True else 1)" "$HOME/.genesis/backup_status.json"
    check $P server_active "genesis-server running after install" -- systemctl --user is-active --quiet genesis-server
    wait_browser_stack
    finish setup
    ;;

noop)
    # Nothing new on origin. #3830 says the package record is retired when "the
    # update or its rollback completes"; a no-op takes the record before its
    # branch and retires it only on a failure marker or a degradation.
    P=noop
    rm -rf "$DEPS_DIR"
    pre_failure=no
    [ -e "$HOME/.genesis/last_update_failure.json" ] && pre_failure=yes
    run_update noop
    check $P exit0 "a no-op update succeeds" -- test "$UPD_RC" -eq 0
    degraded=no
    grep -qE 'UPDATE CONTINUING IN DEGRADED MODE|recording degraded subsystem' "$CI_EV/update-noop.log" && degraded=yes
    left="$(find "$DEPS_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)" || left=0
    if [ "$pre_failure" = no ] && [ "$degraded" = no ]; then
        if [ "$left" -eq 0 ]; then
            result $P record_retired PASS "no package record left after a clean no-op update"
        else
            # Measured, not asserted: reported for the owner's decision.
            result $P record_retired INFO "LEAK: $left file(s) left in update-deps after a clean no-op update"
        fi
    else
        result $P record_retired INFO "INCONCLUSIVE: failure marker=$pre_failure degraded=$degraded (either retires the record)"
    fi
    mkdir -p "$CI_EV/noop-leftover"
    find "$DEPS_DIR" -maxdepth 1 -type f -exec mv {} "$CI_EV/noop-leftover/" \; 2>/dev/null || true
    finish noop
    ;;

clean)
    # A real, harmless update. Control arm: a successful update must leave no record.
    P=clean
    rm -rf "$DEPS_DIR"
    echo "harmless change $(date -u +%FT%TZ)" >"$CI_ROOT/work/scripts/ci/.update-rollback-probe"
    git -C "$CI_ROOT/work" add scripts/ci/.update-rollback-probe
    push_commit "ci: harmless update"
    run_update clean
    check $P exit0 "a clean update succeeds" -- test "$UPD_RC" -eq 0
    check $P updated_line "update.sh reports the move" -- grep -qE '^  Updated: .+ → ' "$CI_EV/update-clean.log"
    check $P head_moved "HEAD is the pushed commit" -- test "$(git -C "$G" rev-parse HEAD)" = "$(git -C "$CI_ROOT/work" rev-parse HEAD)"
    check $P record_retired "no package record left after a successful update" -- \
        test "$(find "$DEPS_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)" -eq 0
    finish clean
    ;;

fault | mutant)
    P="$1"
    W="$CI_ROOT/work"
    if [ "$P" = fault ]; then
        # (a) a migration: a schema change the rollback must undo
        cat >"$W/src/genesis/db/migrations/29991231000000_ci_rollback_probe.py" <<'PY'
"""CI only: a schema change the forced rollback must undo."""


async def up(db):
    await db.execute("CREATE TABLE IF NOT EXISTS ci_rollback_probe (id INTEGER PRIMARY KEY, note TEXT)")
    await db.execute("INSERT INTO ci_rollback_probe (note) VALUES ('written by the failing update')")
PY
        # (b) packages: one added (the restore must remove it), one changed (it must put the old one back)
        "$PY" -I - "$W/pyproject.toml" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s, n = re.subn(r'"diskcache>=5\.6",', '"diskcache==5.6.1",\n    "tomli-w==1.0.0",', s, count=1)
if n != 1:
    sys.exit("pyproject: diskcache line not found")
open(p, "w").write(s)
PY
        # (c) a health check that fails deterministically once the new code runs
        "$PY" -I - "$W/src/genesis/dashboard/routes/health.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
anchor = 'async def health_snapshot():\n    """Return system health snapshot with bridge status from status.json."""\n'
if s.count(anchor) != 1:
    sys.exit("health.py: anchor not found")
s = s.replace(anchor, anchor + '    return jsonify({"status": "unhealthy", "ci": "forced update failure"}), 503\n', 1)
open(p, "w").write(s)
PY
        git -C "$W" add -A
        push_commit "ci: an update that must roll back"
    else
        # The same failing update, with the rollback's database restore swapped
        # back to the pre-#3829 cp (the staged merged rollback runs it). It must
        # be told apart from the real one, or #3829's CI result is
        # integration-only.
        "$PY" -I - "$W/scripts/lib/update_rollback.sh" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
start = s.index("_restore_db_snapshot() {\n")
end = s.index("\n}\n", start) + 3
old_cp = (
    "_restore_db_snapshot() {\n"
    "    _DB_RESTORE_LANDED=0\n"
    '    if cp "$DB_FILE.pre-update.peer-restore" "$DB_FILE" && rm -f "$DB_FILE-wal" "$DB_FILE-shm"; then\n'
    "        _DB_RESTORE_LANDED=1\n"
    '        echo "  Restored pre-migration database snapshot (stale WAL cleared). [CI mutant: pre-#3829 cp]"\n'
    "        return 0\n"
    "    fi\n"
    "    return 1\n"
    "}\n"
)
open(p, "w").write(s[:start] + old_cp + s[end:])
PY
        git -C "$W" add -A
        push_commit "ci: the same failing update with the old cp restore"
    fi
    rm -rf "$DEPS_DIR"
    check $P no_prior_state "no update in progress blocking a new run" -- test ! -e "$HOME/.genesis/update_state.json"
    freeze >"$CI_EV/$P-freeze-before.txt"
    check $P tomli_w_absent_before "tomli-w is not installed before (so its removal is a real change)" -- \
        bash -c "! grep -qi '^tomli[-_]w==' '$CI_EV/$P-freeze-before.txt'"
    check $P diskcache_not_target_before "diskcache is not already 5.6.1" -- \
        bash -c "! grep -qix 'diskcache==5.6.1' '$CI_EV/$P-freeze-before.txt'"
    "$PY" -P "$G/scripts/lib/venv_state.py" capture "$CI_EV/$P-oracle.json"
    before_head="$(git -C "$G" rev-parse HEAD)"

    # The MCP reader: one session, opened before the update, never restarted.
    rm -f "$CI_ROOT/reader-stop" "$CI_ROOT/sampler-stop"
    (cd "$G" && "$PY" scripts/ci/mcp_rollback_reader.py --log "$CI_EV/$P-reader.log" --stop "$CI_ROOT/reader-stop") \
        >"$CI_EV/$P-reader.out" 2>&1 &
    reader_pid=$!
    for _ in $(seq 1 60); do grep -q '"event": "ready"' "$CI_EV/$P-reader.log" 2>/dev/null && break; sleep 2; done
    reader_ready=no
    grep -q '"event": "ready"' "$CI_EV/$P-reader.log" 2>/dev/null && reader_ready=yes
    check $P reader_ready "the MCP session was up, with its tools, before the update" -- test "$reader_ready" = yes

    # Samplers while update.sh runs: did packages change, did the migration land?
    (
        while [ ! -e "$CI_ROOT/sampler-stop" ]; do
            freeze | grep -iE '^(tomli[-_]w|diskcache)==' >>"$CI_EV/$P-freeze-during.txt" 2>/dev/null || true
            db_ro "SELECT count(*) FROM sqlite_master WHERE name='$PROBE'" >>"$CI_EV/$P-probe-during.txt" 2>/dev/null || true
            sleep 5
        done
    ) &
    sampler_pid=$!
    t_update_start="$(date -u +%s)"
    run_update "$P"
    t_update_end="$(date -u +%s)"
    touch "$CI_ROOT/sampler-stop"
    wait "$sampler_pid" 2>/dev/null || true
    L="$CI_EV/update-$P.log"
    touch "$CI_EV/$P-freeze-during.txt" "$CI_EV/$P-probe-during.txt"
    migrated_during=no
    grep -qx 1 "$CI_EV/$P-probe-during.txt" && migrated_during=yes

    # Infrastructure, not the claims: a provisioning run holding the browser lock.
    if grep -q 'browser provisioning run held its lock' "$L"; then
        result $P infra_browser_lock FAIL "INFRA: the browser-stack lock was held through the rollback; the package restore did not run (re-run the job)"
    fi

    # The failure landed where intended: after packages and the migration, at the health check.
    check $P update_failed "the update failed (non-zero exit)" -- test "$UPD_RC" -ne 0
    check $P failed_at_health "it failed at the health check, not earlier" -- grep -q 'health endpoint did not respond after' "$L"
    check $P packages_changed_during "the new packages were really installed: tomli-w 1.0.0 added (control)" -- grep -qix 'tomli[-_]w==1\.0\.0' "$CI_EV/$P-freeze-during.txt"
    check $P pin_changed_during "the changed pin was really installed: diskcache 5.6.1 (control)" -- grep -qix 'diskcache==5\.6\.1' "$CI_EV/$P-freeze-during.txt"
    check $P migration_landed_during "the migration really ran (control)" -- test "$migrated_during" = yes
    check $P merged_rollback "the merged (staged) rollback ran, as on a live install" -- grep -q 'Using the rollback from' "$L"

    # #3830: exact packages.
    check $P pkg_restore_line "rollback says it restored the packages" -- grep -q 'Restored the pre-update packages\.' "$L"
    freeze >"$CI_EV/$P-freeze-after.txt"
    check $P freeze_equal "pip freeze after == before" -- cmp -s "$CI_EV/$P-freeze-before.txt" "$CI_EV/$P-freeze-after.txt"
    check $P oracle_verify "venv_state verify against the job's own pre-update record" -- \
        "$PY" -P "$G/scripts/lib/venv_state.py" verify "$CI_EV/$P-oracle.json"
    check $P code_restored "HEAD is back at the pre-update commit" -- test "$(git -C "$G" rev-parse HEAD)" = "$before_head"

    # #3829: the database.
    if [ "$P" = fault ]; then
        check $P db_restore_line "rollback says it restored the DB through SQLite" -- \
            grep -qE 'Restored (and verified )?the pre-update database snapshot through SQLite' "$L"
    fi
    probe_now="$(db_ro "SELECT count(*) FROM sqlite_master WHERE name='$PROBE'" 2>/dev/null || echo ERR)"
    mig_now="$(db_ro "SELECT count(*) FROM schema_migrations WHERE id LIKE '29991231000000%'" 2>/dev/null || echo ERR)"
    integrity="$(db_ro 'PRAGMA integrity_check' 2>/dev/null || true)"
    integrity="${integrity%%$'\n'*}"
    check $P probe_gone "the migration's table is gone from the live DB" -- test "$probe_now" = 0
    check $P migration_row_gone "schema_migrations no longer records the probe migration" -- test "$mig_now" = 0
    check $P integrity "PRAGMA integrity_check is ok ($integrity)" -- test "$integrity" = ok
    check $P server_active "genesis-server is running after the rollback" -- systemctl --user is-active --quiet genesis-server

    # The server writes again: an awareness tick that STARTED after the rollback
    # restarted the server (a tick's created_at is its start time; ticks run
    # every few minutes, so wait long enough for the second one).
    started_raw="$(systemctl --user show genesis-server -p ExecMainStartTimestamp --value 2>/dev/null || true)"
    # date -d "" means today's midnight, which would pass on old ticks: an empty
    # start time falls back to the update's end instead.
    if [ -n "$started_raw" ] && t_restart="$(date -u -d "$started_raw" +%s 2>/dev/null)"; then
        :
    else
        t_restart="$t_update_end"
        echo "::warning title=update-rollback $P::no server start time; using the update's end as the write boundary"
    fi
    wrote=no
    for _ in $(seq 1 75); do
        last="$(db_ro "SELECT max(created_at) FROM awareness_ticks" 2>/dev/null || true)"
        if [ -n "$last" ] && [ "$("$PY" -c "import datetime,sys; print(int(datetime.datetime.fromisoformat(sys.argv[1]).timestamp()))" "$last" 2>/dev/null || echo 0)" -ge "$t_restart" ]; then
            wrote=yes
            break
        fi
        sleep 5
    done
    check $P server_writes "the server wrote a new awareness tick after its post-rollback restart" -- test "$wrote" = yes
    journalctl --user -u genesis-server --since "@$t_update_start" --no-pager >"$CI_EV/$P-server-journal.txt" 2>/dev/null || true
    journal_ok=no
    [ -s "$CI_EV/$P-server-journal.txt" ] && grep -qiE 'Started|Starting' "$CI_EV/$P-server-journal.txt" && journal_ok=yes
    locked="$(grep -c 'database is locked' "$CI_EV/$P-server-journal.txt" 2>/dev/null || true)"
    locked="${locked:-0}"

    # The same MCP session, never restarted, through the restore.
    sleep 6
    touch "$CI_ROOT/reader-stop"
    for _ in $(seq 1 60); do kill -0 "$reader_pid" 2>/dev/null || break; sleep 2; done
    kill "$reader_pid" 2>/dev/null || true
    wait "$reader_pid" 2>/dev/null || true
    reader_saw="$("$PY" -I - "$CI_EV/$P-reader.log" "$t_update_end" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
end = float(sys.argv[2])
polls = [r for r in rows if r.get("event") == "poll"]
errors = [r for r in rows if r.get("event") == "error" and r["t"] > end]
during = any(r.get("probe_visible") is True for r in polls if r["t"] <= end)
after = [r for r in polls if r["t"] > end]
gone_after = bool(after) and after[-1].get("probe_visible") is False
rolled = bool(after) and after[-1].get("newest_update_status") == "rolled_back"
print(f"{int(during)} {int(gone_after)} {int(rolled)} {len(errors)} {len(after)}")
PY
)" || reader_saw="0 0 0 0 0"
    read -r saw_during gone_after saw_rolled post_errors post_polls <<<"$reader_saw"
    fresh_rc=0
    (cd "$G" && "$PY" scripts/ci/mcp_rollback_reader.py --once --log "$CI_EV/$P-reader-fresh.log" --stop "$CI_ROOT/none") >/dev/null 2>&1 || fresh_rc=$?
    fresh_ok=no
    grep -q '"probe_visible": false' "$CI_EV/$P-reader-fresh.log" 2>/dev/null \
        && grep -q '"newest_update_status": "rolled_back"' "$CI_EV/$P-reader-fresh.log" && fresh_ok=yes

    if [ "$P" = fault ]; then
        check $P reader_saw_probe "the open session saw the migrated table during the failed update (control)" -- test "$saw_during" = 1
        check $P reader_sees_restore "the SAME open session (its held connection) sees the table gone after the restore" -- test "$gone_after" = 1
        check $P reader_sees_history "the reader process (a new connection per call) sees the rolled_back history row" -- test "$saw_rolled" = 1
        check $P reader_no_errors "the open session made $post_polls polls after the restore with no errors or timeouts" -- test "$post_errors" = 0
        check $P fresh_reader "a fresh MCP session sees the restored state (control, exit $fresh_rc)" -- test "$fresh_ok" = yes
        check $P journal_readable "the server journal was readable (so the next check is not vacuous)" -- test "$journal_ok" = yes
        check $P no_locked "no 'database is locked' in the server journal since the update ($locked)" -- test "$locked" = 0
    else
        # The mutant: report whether it was told apart; never fail the job on it.
        tells="locked=$locked reader_gone_after=$gone_after reader_rolled=$saw_rolled reader_errors=$post_errors server_writes=$wrote integrity=${integrity:-?} fresh=$fresh_ok"
        if [ "$reader_ready" != yes ] || [ "$saw_during" != 1 ] || [ "$migrated_during" != yes ] || [ "$journal_ok" != yes ] \
            || ! grep -q 'health endpoint did not respond after' "$L" || ! grep -q 'Using the rollback from' "$L" \
            || ! grep -q 'CI mutant: pre-#3829 cp' "$L"; then
            result $P distinguished INFO "INVALID: the mutant run did not reach a comparable state (reader_ready=$reader_ready saw_during=$saw_during migrated=$migrated_during journal=$journal_ok; $tells)"
        elif [ "$locked" != 0 ] || [ "$gone_after" != 1 ] || [ "$saw_rolled" != 1 ] || [ "$post_errors" != 0 ] \
            || [ "$wrote" != yes ] || [ "${integrity:-}" != ok ] || [ "$fresh_ok" != yes ]; then
            result $P distinguished INFO "YES: the pre-#3829 cp restore is told apart ($tells)"
        else
            result $P distinguished INFO "NO: the pre-#3829 cp restore passed the same checks ($tells); #3829's CI result is integration-only"
        fi
        FAILS=0  # the mutant's own checks above are expected to fail; only its verdict line counts
    fi
    finish "$P"
    ;;

*)
    echo "usage: $0 {setup|noop|clean|fault|mutant}" >&2
    exit 64
    ;;
esac
