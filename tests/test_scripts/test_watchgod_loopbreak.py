"""tmp_watchgod ORANGE loop-break + durable OOM capture.

Regression cover for the 2026-08-19 runaway: cc-tmp went ORANGE (a ~382MB pytest
tree the cache-evict never touches), and because the tier that dispatched the
handler was measured BEFORE cleanup, the daemon re-entered ORANGE every poll and
re-ran the idle-session kill loop for ~4.5h. The loop-break re-measures AFTER
cleanup and kills ONLY if still over the line; when nothing is reclaimable and
nothing is killable it pages once instead of looping silently.

Plus the OOM sampler: on a NEW cgroup oom_kill it writes a durable snapshot and
pages once (the death that started all this left no diagnosable trace).

tmux is a configurable STUB (never a real session): it reports whatever sessions
the test injects and records every kill-session call to a file, so we can assert
the loop-break did or did NOT kill. queue_alert is overridden to a call-log so we
can assert page-once. The script's sourcing guard loads its functions without
starting the daemon.
"""

import os
import stat
import subprocess
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# A configurable tmux stub. STUB_SESSIONS is echoed for list-sessions (one
# "name:attached" line), STUB_ACTIVITY is the session_activity epoch, and every
# kill-session target is appended to STUB_KILLLOG. Any other verb is a no-op.
_TMUX_STUB = r"""#!/usr/bin/env bash
case "$1" in
  list-sessions)   [[ -n "${STUB_SESSIONS:-}" ]] && printf '%s\n' "$STUB_SESSIONS" ;;
  display-message) echo "${STUB_ACTIVITY:-0}" ;;
  kill-session)    echo "$3" >> "${STUB_KILLLOG:?}"; exit "${STUB_KILL_RC:-0}" ;;
esac
exit 0
"""


# journalctl stub — the OOM attribution path queries the user journal, and a
# test must never read the REAL one (host-dependent: a live install's journal
# could carry a genuine contained kill and flip a paging assertion). Installed
# in every sandbox: STUB_JOURNAL is the `-o cat` output (default empty =
# journal readable, nothing attributable → unattributed page, the pre-#1775
# behaviour every older test asserts), STUB_JOURNAL_RC simulates an
# unavailable journal.
_JOURNALCTL_STUB = r"""#!/usr/bin/env bash
[[ -n "${STUB_JOURNAL_ARGLOG:-}" ]] && echo "$*" >> "$STUB_JOURNAL_ARGLOG"
# STUB_JOURNAL_RC_FILE (read per call) lets a multi-call snippet model the
# journal going down and recovering; STUB_JOURNAL_RC is the static form.
if [[ -n "${STUB_JOURNAL_RC_FILE:-}" && -f "$STUB_JOURNAL_RC_FILE" ]]; then
  _rc=$(cat "$STUB_JOURNAL_RC_FILE")
else
  _rc="${STUB_JOURNAL_RC:-0}"
fi
[[ "$_rc" != 0 ]] && exit "$_rc"
# Cursor semantics, enough to exercise the real thing: the guard queries with
# --after-cursor (a POSITION filter, strictly-after) once it holds a cursor, and
# falls back to a relative --since window on the first run. STUB_JOURNAL_STALE
# marks the stubbed line as belonging to an EARLIER position, so any query that
# carries --after-cursor must not return it — the stale-line case.
_after_cursor=0
for a in "$@"; do [[ "$a" == "--after-cursor" ]] && _after_cursor=1; done
if [[ -n "${STUB_JOURNAL_STALE:-}" && "$_after_cursor" == 1 ]]; then
  # Out of window: emit only the cursor line, no records.
  printf -- '-- cursor: s=stub;i=%s\n' "$(date +%s%N)"
  exit 0
fi
# STUB_JOURNAL_FILE: records, one per line, re-read per call — a multi-call
# snippet can land a record LATE (after the tick whose kill it belongs to).
if [[ -n "${STUB_JOURNAL_FILE:-}" && -f "${STUB_JOURNAL_FILE}" ]]; then
  cat "$STUB_JOURNAL_FILE"
elif [[ -n "${STUB_JOURNAL:-}" ]]; then
  printf '%s\n' "${STUB_JOURNAL}"
fi
# --show-cursor appends this trailing line; the guard parses it to advance.
printf -- '-- cursor: s=stub;i=%s\n' "$(date +%s%N)"
exit 0
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"
    cctmp.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    _make_exec(bind / "journalctl", _JOURNALCTL_STUB)
    return home, cctmp, bind


def _run(home, bind, snippet, extra_env=None):
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


# The snippet prefix: shrink the budget so a few MB crosses ORANGE, and override
# queue_alert to a call-log. threshold_orange = budget*75/100; budget=4 → 3MB.
_PRELUDE = (
    'CC_TMP_BUDGET_MB=4; queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
)


def _mb(path: Path, mb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (mb * 1024 * 1024))


# ── Fix 3: ORANGE loop-break ─────────────────────────────────────────────


def test_orange_resolved_by_cleanup_does_not_kill(tmp_path):
    """The core regression: when cache cleanup drops cc-tmp back under the ORANGE
    line, NO session is killed — even though a killable (idle>2h, unattached)
    session exists. Before the loop-break this killed a session every poll."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "claude-skills" / "blob", 6)  # >3MB ORANGE; evicted by cleanup
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-99:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), (
        "loop-break failed: a session was killed after cleanup resolved ORANGE"
    )
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "resolved by cache cleanup" in log, log


def test_orange_persists_no_killable_logs_once_no_page(tmp_path):
    """Non-reclaimable data keeps cc-tmp ORANGE and no idle session is killable →
    per design D2 this does NOT page (only RED pages); it records the stuck state
    once (dedupe flag + a single STUCK log line), not silently every poll."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # NOT a cache/session dir → cleanup can't evict
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "", "STUB_KILLLOG": str(killlog)}  # no sessions
    # Call twice — the second must NOT re-log the stuck state (flag dedupe).
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange; clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "nothing was killable; no kill should occur"
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    assert stuck.exists(), "stuck-ORANGE dedupe flag not set"
    # D2: no page — the alert queue is never touched for a stuck ORANGE.
    assert not (home / ".genesis" / "alerts" / "calls.log").exists(), (
        "stuck ORANGE must NOT page (D2: only RED pages)"
    )
    logtext = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert logtext.count("STUCK ORANGE") == 1, (
        f"expected exactly one STUCK log line, got {logtext.count('STUCK ORANGE')}"
    )


def test_orange_persists_with_killable_reaps_and_no_stuck_flag(tmp_path):
    """When ORANGE persists AND an idle>2h unattached session exists, it IS
    reaped (unchanged behavior) and the stuck flag is not raised."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-77:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert killlog.exists() and "cc-77" in killlog.read_text(), "idle session should be reaped"
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    assert not stuck.exists(), "stuck flag must not be set when a session was killed"


def test_kill_does_not_clear_stuck_flag_no_double_page(tmp_path):
    """Regression for the double-page edge: once stuck-ORANGE has paged, a later
    poll that happens to reap a newly-idle session must NOT clear the flag (a kill
    doesn't reduce cc-tmp), else the next still-ORANGE poll re-arms and re-pages
    the same episode. The flag clears only on the green transition."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # persists ORANGE
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    stuck.touch()  # a prior poll already paged
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-88:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert killlog.exists(), "idle session should still be reaped"
    assert stuck.exists(), "a kill must NOT clear the stuck flag (would re-page next poll)"


def test_failed_kill_does_not_count_as_reaped(tmp_path):
    """If tmux kill-session FAILS (session vanished between listing and killing,
    or any error), killed_any must stay 0 — nothing was reclaimed — so the stuck
    marker is still recorded rather than silently skipped on a phantom reap."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # persists ORANGE
    killlog = home / "killed.log"
    env = {
        "STUB_SESSIONS": "cc-66:0",
        "STUB_ACTIVITY": "100",
        "STUB_KILLLOG": str(killlog),
        "STUB_KILL_RC": "1",  # kill-session fails
    }
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert killlog.exists(), "kill was attempted"
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    assert stuck.exists(), "a FAILED kill must not suppress the stuck marker"


def test_attached_session_never_killed(tmp_path):
    """An ATTACHED session (activity recent, or simply not matched by the ':0$'
    unattached filter) is never a kill candidate — asserts the filter shape."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)
    killlog = home / "killed.log"
    # attached=1 → the ':0$' grep drops it, so the stub still lists it but the
    # loop never sees it. (list-sessions output is pre-filtered by the script.)
    env = {"STUB_SESSIONS": "cc-5:1", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "attached session must never be killed"


# ── Fix 4: durable OOM capture ───────────────────────────────────────────


def _oom_file(tmp_path, kills: int, local_oom: int | None = 0) -> Path:
    """The container's memory.events fixture, plus its memory.events.local
    (oom = <local_oom>) — the trigger discriminator. local_oom=None leaves the
    local file ABSENT: the trigger is then unverifiable and suppression must
    fail closed to a page."""
    f = tmp_path / "memory.events"
    f.write_text(f"low 0\nhigh 0\nmax 0\noom 3\noom_kill {kills}\noom_group_kill 0\n")
    if local_oom is not None:
        (tmp_path / "memory.events.local").write_text(
            f"low 0\nhigh 0\nmax 0\noom {local_oom}\noom_kill {kills}\noom_group_kill 0\n"
        )
    return f


def test_oom_increment_logs_and_pages(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "3:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "BASELINE=5" in out.stdout, out.stdout  # baseline advances to current
    oom_log = (home / ".genesis" / "logs" / "oom_events.log").read_text()
    assert "oom_kill 3 -> 5 (+2)" in oom_log, oom_log
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    # OOM pages at EMERGENCY tier (a discrete serious event), unlike stuck-ORANGE.
    assert "emergency watchgod:oom" in calls, calls


def test_oom_no_increment_is_silent(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "5:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom)},
    )
    assert "BASELINE=5" in out.stdout, out.stdout
    assert not (home / ".genesis" / "logs" / "oom_events.log").exists()
    assert not (home / ".genesis" / "alerts" / "calls.log").exists()


def test_oom_unavailable_is_noop(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "3:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(tmp_path / "does-not-exist")},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "BASELINE=3" in out.stdout, out.stdout  # baseline preserved, no crash


# ── Issue #1775: attribute the kill before paging ────────────────────────


_KILL_LINE = "code-intel-4408aa696643-cbm-4107466.scope: Failed with result 'oom-kill'."


def test_oom_contained_kill_snapshots_but_does_not_page(tmp_path):
    """The false-alarm class this closes: a kill inside a known resource-capped
    scope is containment working — durable snapshot + WARN log, no page."""
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "BASELINE=5" in out.stdout, out.stdout  # baseline still advances
    # The durable record survives — only the PAGE is downgraded.
    oom_log = (home / ".genesis" / "logs" / "oom_events.log").read_text()
    assert "oom_kill 4 -> 5 (+1)" in oom_log, oom_log
    assert not (home / ".genesis" / "alerts" / "calls.log").exists(), "must not page"
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [code-intel-4408aa696643-cbm-4107466.scope]" in wg_log, wg_log


def test_oom_noncontained_unit_pages_and_names_it(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": "run-u1234.scope: Failed with result 'oom-kill'.",
        },
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "run-u1234.scope" in calls, calls  # the page NAMES the killed unit


def test_oom_partially_attributed_batch_pages(tmp_path):
    """Two kills, ONE contained journal line → PAGE. The second kill is
    unexplained, and attribution may only downgrade a batch it fully accounts
    for.

    The pre-fix condition asked only "is every unit I FOUND contained?", which
    is trivially true of a single contained line — so `oom_kill 4 -> 6` was
    suppressed while a kill nothing accounted for went silent. That is the
    fail-open rule inverted in the one direction it exists to protect. The
    count of journal records must equal the counter delta.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 6)  # +2 kills
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE},  # only ONE line
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = home / ".genesis" / "alerts" / "calls.log"
    assert calls.exists(), "a partially attributed batch must PAGE, not suppress"
    assert "oom_kill 4->6" in calls.read_text(), calls.read_text()


def test_oom_fully_attributed_batch_of_two_does_not_page(tmp_path):
    """The control for the cell above, in the other direction.

    Without it, the count check would pass a guard that simply pages on every
    n>1 batch — which would re-open the false-alarm class #1775 closed, since
    two contained kills inside one poll window is the ordinary shape (measured
    2026-09-08: a gitnexus and a cbm scope died 39s apart).
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 6)  # +2 kills
    second = "code-intel-4408aa696643-gitnexus-4107467.scope: Failed with result 'oom-kill'."
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE + "\n" + second},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert not (home / ".genesis" / "alerts" / "calls.log").exists(), "both contained → no page"
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [" in wg_log, wg_log


def test_oom_same_unit_killed_twice_is_fully_attributed(tmp_path):
    """Cardinality, not distinctness: the SAME unit killed twice is two kills.

    This is the cell that makes preserving record cardinality load-bearing.
    De-duplicating the unit list (`sort -u`) collapses two records of one unit
    into a single line, so the count reads 1 against a delta of 2 and the batch
    pages as partially attributed — a FALSE page, in the direction #1775 exists
    to remove. It degrades safely (over-paging, never silence), which is exactly
    why no other cell catches it: every other case has distinct unit names, so
    dedup is a no-op there and the mutation survives them.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 6)  # +2 kills
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        # The identical contained unit, twice — one record per kill.
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE + "\n" + _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert not (home / ".genesis" / "alerts" / "calls.log").exists(), (
        "two records of one contained unit fully account for two kills — no page"
    )
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [" in wg_log, wg_log


def test_oom_mixed_units_page(tmp_path):
    # One contained + one not → page: attribution may only downgrade a kill
    # when EVERY killed unit is accounted for.
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 6)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": _KILL_LINE
            + "\nrun-u1234.scope: Failed with result 'oom-kill'.",
        },
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "emergency watchgod:oom" in (home / ".genesis" / "alerts" / "calls.log").read_text()


def test_oom_journal_unavailable_degrades_to_the_page(tmp_path):
    # No journal → the pre-attribution behaviour, unattributed page. Never
    # silence on missing evidence.
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL_RC": "1"},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "unattributed" in calls, calls


def test_oom_contained_prefixes_are_configurable(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": "myjob-heavy.scope: Failed with result 'oom-kill'.",
            "OOM_CONTAINED_UNIT_PREFIXES": "myjob-",
        },
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert not (home / ".genesis" / "alerts" / "calls.log").exists()


def test_oom_stale_contained_line_cannot_account_for_a_new_kill(tmp_path):
    """The cursor closes the one silence path attribution had.

    Scenario: a contained kill's journal line is consumed by increment N; a
    SECOND increment follows whose kill left no line of its own (a non-main
    process dying inside a surviving scope writes no unit-failure record).
    Under a fixed lookback the old line was still in window and silently
    accounted for the new kill; with the cursor advanced past it, the second
    increment reads an EMPTY journal and pages unattributed.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        # The stubbed line belongs to an EARLIER journal position: the first
        # call's fallback window (relative --since, no cursor yet) sees it, and
        # the cursor that call records sits after it — so the second call's
        # --after-cursor query, being strictly-after, does not.
        + 'STUB_JOURNAL_STALE=1; export STUB_JOURNAL_STALE; '
        + 'r1=$(check_oom_events "4:0:0:0:0"); echo "B1=$r1"; '
        + f'printf \'%s\' "low 0\nhigh 0\nmax 0\noom 3\noom_kill 6\noom_group_kill 0\n" > "{oom}"; '
        + 'r2=$(check_oom_events "$r1"); echo "B2=$r2"'
    )
    out = _run(home, bind, snippet, {"STUB_JOURNAL": _KILL_LINE})
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B1=5" in out.stdout and "B2=6" in out.stdout, out.stdout
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [" in wg_log, wg_log  # first increment: downgraded
    calls = home / ".genesis" / "alerts" / "calls.log"
    assert calls.exists(), "second increment must PAGE — its kill has no line"
    body = calls.read_text()
    assert "unattributed" in body and "oom_kill 5->6" in body, body


def test_oom_journal_query_uses_the_cursor_after_the_first_read(tmp_path):
    # Mechanism pin: a BARE legacy baseline (no local/deficit fields) → call 1
    # has no cursor file → relative fallback window; call 2 must query
    # --after-cursor with the cursor call 1 recorded.
    #
    # This used to pin `--since "@<epoch>"` alone. A timestamp filter is
    # INCLUSIVE at its boundary, so an entry landing exactly on the stored
    # second is read twice; --after-cursor is a position filter and is
    # strictly-after. The invariant ("the second read is bounded by what the
    # first recorded") is unchanged — the two bounds now compose.
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    arglog = tmp_path / "journal_args.log"
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 'r1=$(check_oom_events 4); '
        + f'printf \'%s\' "low 0\nhigh 0\nmax 0\noom 3\noom_kill 6\noom_group_kill 0\n" > "{oom}"; '
        + 'r2=$(check_oom_events "$r1"); true'
    )
    out = _run(home, bind, snippet, {"STUB_JOURNAL_ARGLOG": str(arglog)})
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    lines = arglog.read_text().splitlines()
    assert len(lines) == 2, lines
    assert " seconds" in lines[0], lines[0]  # bare baseline: relative fallback window
    assert "--after-cursor" not in lines[0], lines[0]  # ...and no cursor yet
    assert "--after-cursor" in lines[1], lines[1]  # second read: cursor used
    assert "s=stub" in lines[1], lines[1]  # ...and it is the one call 1 recorded
    # NOTE: --since must NOT appear on a cursor query — journalctl (systemd 255,
    # verified) refuses "--after-cursor + --since" outright. The late-record
    # problem is closed by deficit reconciliation instead.
    assert "--since" not in lines[1], lines[1]


def test_oom_cbm_wrapper_kill_is_contained_by_default(tmp_path):
    # Issue #1792: the codebase-memory MCP wrapper names its capped scope
    # cbm-mcp-<pid> precisely so this classification can exist. Both default
    # prefixes must classify — a second entry that silently broke the first
    # (or vice versa) would reopen the false pages.
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": "cbm-mcp-4107466.scope: Failed with result 'oom-kill'.",
        },
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert not (home / ".genesis" / "alerts" / "calls.log").exists()
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [cbm-mcp-4107466.scope]" in wg_log, wg_log


# ── #1790 review round: trigger verification + bounded attribution window ──


def test_oom_container_trigger_pages_despite_a_contained_record(tmp_path):
    """Codex P1 (#1790): the journal names the VICTIM unit, not the cgroup
    whose limit fired. An ancestor-limit OOM can victimise a contained child —
    the child's own cap never fired. The container root's LOCAL oom counter is
    the only trigger evidence: when it moved, the kill PAGES even with a
    fully-contained journal record."""
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5, local_oom=1)  # local oom 0 -> 1 across the window
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "container-level trigger" in calls, calls


def test_oom_unverifiable_trigger_pages_even_when_contained(tmp_path):
    """Fail direction: no readable memory.events.local = the trigger cannot be
    verified = NEVER suppress, however contained the journal record looks."""
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5, local_oom=None)  # no local fixture at all
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "trigger unverifiable" in calls, calls


def test_oom_late_record_from_an_earlier_kill_cannot_cover_a_new_kill(tmp_path):
    """Codex P1 + Devin (#1790): systemd can log the unit-failure line AFTER
    the poll that saw the counter move. The record's timestamp is PID 1's
    EMISSION time (measured on the live journal), so no time window can exclude
    it — reconciliation can: the late record retires the already-paged deficit
    instead of covering the new kill.

    Tick 1: contained kill, record NOT yet in the journal -> page, deficit 1.
    Tick 2: a new line-less kill (+1) while the late record lands -> the query
    returns ONE record against obligations 1+1=2 -> the new kill PAGES.
    Without reconciliation, count==delta==1 suppressed it.
    """
    home, _cc, bind = _sandbox(tmp_path)
    jfile = tmp_path / "journal.txt"
    jfile.write_text("")  # tick 1: the contained kill's record has NOT landed
    oom = _oom_file(tmp_path, 5)
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 'r1=$(check_oom_events "4:0:0:0:0"); echo "B1=$r1"; '
        # The tick-1 record lands LATE (simply: it appears in the file now).
        # (Double-quoted: the line itself contains single quotes.)
        + f"printf '%s\\n' \"{_KILL_LINE}\" > \"{jfile}\"; "
        + f"printf 'low 0\\nhigh 0\\nmax 0\\noom 3\\noom_kill 6\\noom_group_kill 0\\n' > \"{oom}\"; "
        + 'r2=$(check_oom_events "$r1"); echo "B2=$r2"'
    )
    out = _run(home, bind, snippet, {"STUB_JOURNAL_FILE": str(jfile)})
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert calls.count("emergency watchgod:oom") == 2, (
        "both increments must page — the late record belongs to the first, "
        f"already-paged kill: {calls}"
    )


def test_oom_late_record_retires_its_own_deficit_and_the_new_contained_kill_suppresses(tmp_path):
    """The other half of reconciliation: the late record and the new kill's
    OWN on-time record together account for BOTH obligations, so a contained
    kill is not paged just because the previous one's record was late.
    Tick 1: kill A, no record yet -> page, deficit 1.
    Tick 2: kill B (contained, on-time record) + A's late record both arrive:
    2 records == deficit 1 + delta 1, all contained -> no page."""
    home, _cc, bind = _sandbox(tmp_path)
    jfile = tmp_path / "journal.txt"
    jfile.write_text("")  # tick 1: A's record has not landed
    oom = _oom_file(tmp_path, 5)
    second = "code-intel-4408aa696643-gitnexus-4107467.scope: Failed with result 'oom-kill'."
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 'r1=$(check_oom_events "4:0:0:0:0"); echo "B1=$r1"; '
        + f"printf '%s\\n%s\\n' \"{_KILL_LINE}\" \"{second}\" > \"{jfile}\"; "
        + f"printf 'low 0\\nhigh 0\\nmax 0\\noom 3\\noom_kill 6\\noom_group_kill 0\\n' > \"{oom}\"; "
        + 'r2=$(check_oom_events "$r1"); echo "B2=$r2"'
    )
    out = _run(home, bind, snippet, {"STUB_JOURNAL_FILE": str(jfile)})
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert calls.count("emergency watchgod:oom") == 1, (
        f"only tick 1 (A unexplained) may page; tick 2 is fully accounted: {calls}"
    )
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [" in wg_log, wg_log


def test_oom_on_time_record_still_attributes(tmp_path):
    """The control: an on-time record still accounts for its kill — the
    reconciliation must not break the ordinary contained case."""
    home, _cc, bind = _sandbox(tmp_path)
    jfile = tmp_path / "journal.txt"
    jfile.write_text(_KILL_LINE + "\n")
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events "4:0:0:0:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL_FILE": str(jfile)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = home / ".genesis" / "alerts" / "calls.log"
    assert not calls.exists() or "emergency watchgod:oom" not in calls.read_text(), (
        calls.read_text() if calls.exists() else ""
    )
    wg_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "contained in [" in wg_log, wg_log


def test_oom_unretireable_deficit_expires(tmp_path):
    """A kill that never writes a record (a non-main process dying inside a
    surviving scope) leaves a deficit nothing can retire; without expiry every
    later contained kill would page spuriously forever. After the TTL the
    deficit is forgotten and ordinary attribution resumes. The second sandbox
    pins the other arm: a LIVE deficit still pages."""
    import time as _time

    home, _cc, bind = _sandbox(tmp_path)
    jfile = tmp_path / "journal.txt"
    jfile.write_text(_KILL_LINE + "\n")
    oom = _oom_file(tmp_path, 5)
    old_ts = int(_time.time()) - 100000  # far beyond the TTL
    out = _run(
        home,
        bind,
        _PRELUDE + f'result=$(check_oom_events "4:0:1:{old_ts}:0"); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL_FILE": str(jfile)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    calls = home / ".genesis" / "alerts" / "calls.log"
    assert not calls.exists() or "emergency watchgod:oom" not in calls.read_text(), (
        "an expired deficit must not page a fully-attributed contained kill: "
        + (calls.read_text() if calls.exists() else "")
    )
    home2, _cc2, bind2 = _sandbox(tmp_path / "second")
    jfile2 = tmp_path / "second" / "journal.txt"
    jfile2.write_text(_KILL_LINE + "\n")
    oom2 = _oom_file(tmp_path / "second", 5)
    out2 = _run(
        home2,
        bind2,
        _PRELUDE + 'result=$(check_oom_events "4:0:1:$(date +%s):0"); true',
        {"OOM_EVENTS_FILE": str(oom2), "STUB_JOURNAL_FILE": str(jfile2)},
    )
    assert out2.returncode == 0, f"{out2.stdout}\n{out2.stderr}"
    calls2 = (home2 / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls2, "a live deficit must still page"


def test_oom_unarmed_cursor_pages_and_reanchors(tmp_path):
    """drain=1 (the arm could not advance the cursor — journalctl down at
    startup): the first resolution must page even when the fallback window
    returns fully-contained records, and the successful query re-anchors the
    cursor for later ticks (Codex P1, #1790)."""
    home, _cc, bind = _sandbox(tmp_path)
    rcfile = tmp_path / "journal_rc"
    rcfile.write_text("1")  # journal down at arm time
    oom = _oom_file(tmp_path, 5)
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 'b=$(_oom_arm_baseline); echo "ARM=$b"; '
        # the journal recovers, and a kill lands, before the first tick
        + f'printf "0" > "{rcfile}"; '
        + f"printf 'low 0\\nhigh 0\\nmax 0\\noom 3\\noom_kill 6\\noom_group_kill 0\\n' > \"{oom}\"; "
        + 'r=$(check_oom_events "$b"); echo "B=$r"'
    )
    out = _run(
        home,
        bind,
        snippet,
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE, "STUB_JOURNAL_RC_FILE": str(rcfile)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "ARM=5:0:0:0:1" in out.stdout, out.stdout
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "could not be armed" in calls, calls
    # re-anchored: the fallback query's --show-cursor landed in the cursor file
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert cursor.exists() and cursor.read_text().startswith("s=stub")


def test_oom_startup_baseline_advances_cursor(tmp_path):
    """Codex P1 (#1790): baselining the counter without advancing the journal
    cursor leaves pre-startup records behind it, and the first post-startup
    kill could be 'accounted for' by a kill from before the baseline. Arming
    writes the tail cursor and stamps the window."""
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'b=$(_oom_arm_baseline); echo "B=$b"',
        {"OOM_EVENTS_FILE": str(oom)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    import re

    assert re.search(r"B=5:0:0:0:0", out.stdout), out.stdout  # counter:local:deficit:ts:drain
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert cursor.exists(), "arming must advance the journal cursor"
    assert cursor.read_text().startswith("s=stub")


def test_oom_drain_survives_a_failed_first_resolution(tmp_path):
    """Audit BLOCKER (#1790, round 2): drain=1 must clear only when a query
    SUCCEEDED. The journal was down at arm AND stays down through the first
    increment: that tick pages, but the cursor is still unanchored — clearing
    drain there would let the next tick's fallback window offer PRE-BASELINE
    records as attribution and suppress a real kill's page.

    Tick 1 (journal down): page, drain kept. Tick 2 (journal back, fallback
    window returns a contained record from before the baseline): must STILL
    page — with the drain reason — and the successful query re-anchors."""
    home, _cc, bind = _sandbox(tmp_path)
    rcfile = tmp_path / "journal_rc"
    rcfile.write_text("1")  # journal down at arm AND through tick 1
    oom = _oom_file(tmp_path, 5)
    snippet = (
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 'b=$(_oom_arm_baseline); echo "ARM=$b"; '
        # tick 1: a kill lands while the journal is STILL down
        + f'printf \'low 0\\nhigh 0\\nmax 0\\noom 3\\noom_kill 6\\noom_group_kill 0\\n\' > "{oom}"; '
        + 'r1=$(check_oom_events "$b"); echo "B1=$r1"; '
        # the journal recovers and a SECOND kill lands before tick 2
        + f'printf "0" > "{rcfile}"; '
        + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 7\noom_group_kill 0\n' > \"{oom}\"; "
        + 'r2=$(check_oom_events "$r1"); echo "B2=$r2"'
    )
    out = _run(
        home,
        bind,
        snippet,
        {"OOM_EVENTS_FILE": str(oom), "STUB_JOURNAL": _KILL_LINE, "STUB_JOURNAL_RC_FILE": str(rcfile)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "ARM=5:0:0:0:1" in out.stdout, out.stdout
    assert ":1" in out.stdout.split("B1=")[1], (
        f"drain must SURVIVE the failed first resolution: {out.stdout}"
    )
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert calls.count("emergency watchgod:oom") == 2, (
        f"both ticks must page — tick 2's record is pre-anchor and cannot count: {calls}"
    )
    assert calls.count("could not be armed") == 2, calls
    # ...and the successful tick-2 query re-anchored the cursor.
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert cursor.exists() and cursor.read_text().startswith("s=stub")
