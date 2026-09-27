"""tmp_watchgod ORANGE loop-break + durable OOM capture.

Regression cover for the 2026-08-19 runaway: cc-tmp went ORANGE (a ~382MB pytest
tree the cache-evict never touches), and because the tier that dispatched the
handler was measured BEFORE cleanup, the daemon re-entered ORANGE every poll and
re-ran this tail every poll for ~4.5h. The loop-break re-measures AFTER cleanup
and only proceeds if still over the line; when nothing is reclaimable it records
the stuck state ONCE in the log rather than re-logging it forever. It does not
page: per design D2 only RED pages, which this file asserts twice.

The idle-session kill this tier used to perform was REMOVED in 2026-09 — it
never fired and could not have reclaimed cc-tmp if it had. RED's kill is a
separate site and is unchanged.

Plus the OOM sampler: on a NEW cgroup oom_kill it writes a durable snapshot and
pages once (the death that started all this left no diagnosable trace).

tmux is a configurable STUB (never a real session): it reports whatever sessions
the test injects and records every kill-session call to a file, so we can assert
no kill happens at ORANGE and that RED's still does. STUB_TMUX_ARGLOG records
every invocation, which is what separates a DELETED call site from one whose
predicate merely did not fire. queue_alert is overridden to a call-log so we
can assert page-once. The script's sourcing guard loads its functions without
starting the daemon.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# A configurable tmux stub. STUB_SESSIONS is echoed for list-sessions (one
# "name:attached" line), STUB_ACTIVITY is the session_activity epoch, and every
# kill-session target is appended to STUB_KILLLOG. Any other verb is a no-op.
# STUB_TMUX_ARGLOG, when set, records EVERY invocation (verb and all) — the
# only way to tell a deleted call site from one whose predicate did not fire.
_TMUX_STUB = r"""#!/usr/bin/env bash
[[ -n "${STUB_TMUX_ARGLOG:-}" ]] && echo "$*" >> "$STUB_TMUX_ARGLOG"
case "$1" in
  list-sessions)   [[ -n "${STUB_SESSIONS:-}" ]] && printf '%s\n' "$STUB_SESSIONS" ;;
  display-message) echo "${STUB_ACTIVITY:-0}" ;;
  # STUB_KILL_RC is retained for RED's kill path; ORANGE no longer kills, so
  # the arm that used to exercise a FAILED kill is gone.
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


def test_orange_resolved_by_cleanup_returns_early(tmp_path):
    """When cache cleanup drops cc-tmp back under the ORANGE line the handler
    returns before the stuck-state record and clears any stale flag.

    The kill-log assertion is kept deliberately. This arm predates the removal
    of the ORANGE kill loop (2026-09) and is retained as a standing guard: if
    the loop is ever restored, it must go red here too."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "claude-skills" / "blob", 6)  # >3MB ORANGE; evicted by cleanup
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    stuck.touch()  # a stale flag from a prior episode
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-99:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "no session may be killed at ORANGE"
    assert not stuck.exists(), "resolving ORANGE must clear the stuck flag"
    log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "resolved by cache cleanup" in log, log


def test_orange_persists_logs_stuck_once_and_never_pages(tmp_path):
    """Non-reclaimable data keeps cc-tmp ORANGE → per design D2 this does NOT
    page (only RED pages); it records the stuck state once (dedupe flag + a
    single STUCK log line), not silently every poll."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # NOT a cache/session dir → cleanup can't evict
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "", "STUB_KILLLOG": str(killlog)}  # no sessions
    # Call twice — the second must NOT re-log the stuck state (flag dedupe).
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange; clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "no session may be killed at ORANGE"
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
    # The ORANGE tail's two operator-facing lines are the only place a human
    # learns what this tier did, and nothing else in this file reads them. Both
    # made a claim about killing sessions that stopped being true in 2026-09;
    # pin the absence, or the prose can silently regress while every arm above
    # stays green.
    for lie in ("evaluating idle sessions", "session is killable", "before any session kill"):
        assert lie not in logtext, (
            f"the ORANGE log still claims {lie!r}, but this tier no longer kills"
        )


def test_orange_with_an_idle_killable_session_kills_nothing(tmp_path):
    """THE ACCEPTANCE BAR for the 2026-09 removal, and it inverts what this
    file used to assert.

    Until 2026-09 an unattached CC session idle >2h was reaped at ORANGE, and
    the stuck state was then suppressed (`killed_any == 1`). Both halves are
    now inverted: the session survives, and the stuck state IS recorded —
    because reaping a session was never going to reclaim cc-tmp. MEASURED
    across two independent log windows (2026-08-19 → 09-07 and 2026-09-22 →
    09-25): 1,385 ORANGE polls, zero kills."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)
    killlog = home / "killed.log"
    # STUB_ACTIVITY=100 is epoch 1970 → idle far past the old 2h threshold, so
    # this session is exactly the one the removed loop would have killed.
    env = {"STUB_SESSIONS": "cc-77:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "ORANGE killed an idle session — the removed kill loop is back"
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    assert stuck.exists(), (
        "a killable session must no longer suppress the stuck record: there is "
        "no kill, so there is nothing to suppress it"
    )
    # This fixture is the ONE the old loop would have killed under, so it is the
    # only arm where the old kill log line could ever have appeared. Pin its
    # absence here rather than in an arm with no sessions, where it is dead.
    logtext = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "Killing idle" not in logtext, "the ORANGE kill log line is back\n" + logtext


def test_orange_invokes_tmux_zero_times(tmp_path):
    """Stronger than 'no kill was logged', and the reason this arm exists.

    A no-kill assertion passes against a loop whose predicate merely failed to
    fire — MEASURED: three of this file's pre-existing no-kill arms stayed
    green against the unmodified script. Asserting that tmux is never invoked
    at all (no list-sessions, no display-message, no kill-session) is what
    distinguishes a deleted loop from a quiet one."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # persists ORANGE → reaches the tail
    arglog = home / "tmux-calls.log"
    env = {
        "STUB_SESSIONS": "cc-55:0",
        "STUB_ACTIVITY": "100",
        "STUB_KILLLOG": str(home / "killed.log"),
        "STUB_TMUX_ARGLOG": str(arglog),
    }
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # PROOF OF TAIL. Without this the arm passes when ORANGE resolves early and
    # never reaches the region the kill loop was in — a fixture whose blob lands
    # in an EVICTABLE cache dir does exactly that, and the RED control below
    # cannot tell the two apart.
    orange_log = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "ORANGE persists after cleanup" in orange_log, (
        "ORANGE returned before the tail — the zero-tmux assertion is vacuous"
    )
    # Negative control: the arglog machinery itself must work, or this arm is
    # vacuous. RED does invoke tmux, so a second call proves the log records.
    assert not arglog.exists(), (
        f"ORANGE invoked tmux: {arglog.read_text() if arglog.exists() else ''}"
    )
    proc2 = _run(home, bind, _PRELUDE + "clean_cc_red", env)
    assert proc2.returncode == 0, f"{proc2.stdout}\n{proc2.stderr}"
    assert arglog.exists() and "list-sessions" in arglog.read_text(), (
        "the tmux arglog never records anything — the ORANGE assertion above was vacuous"
    )


def test_a_second_stuck_poll_neither_relogs_nor_pages(tmp_path):
    """The dedupe survives the removal of the kill.

    This arm used to read: a later poll that reaps a newly-idle session must
    not clear the flag, else the next still-ORANGE poll re-arms and re-logs the
    same episode. There is no kill to clear it now, but the invariant it was
    protecting — one episode, one STUCK line — still has to hold."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # persists ORANGE
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    stuck.touch()  # a prior poll already recorded this episode
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-88:0", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "no session may be killed at ORANGE"
    assert stuck.exists(), "the stuck flag must survive a subsequent poll"
    logtext = (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert logtext.count("STUCK ORANGE") == 0, (
        "the flag failed to dedupe: the episode was logged a second time"
    )
    assert not (home / ".genesis" / "alerts" / "calls.log").exists(), (
        "stuck ORANGE must NOT page (D2: only RED pages)"
    )


def test_no_session_is_killed_whatever_its_attach_state(tmp_path):
    """Attached or unattached, idle or fresh: ORANGE kills nothing.

    Before 2026-09 only the unattached (`:0$`) sessions were candidates and the
    attach state was load-bearing. It no longer is, and this arm pins the
    generalisation rather than the old filter shape."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)
    killlog = home / "killed.log"
    for sessions in ("cc-5:1", "cc-5:0", "cc-5:0\ncc-6:0"):
        env = {
            "STUB_SESSIONS": sessions,
            "STUB_ACTIVITY": "100",
            "STUB_KILLLOG": str(killlog),
        }
        proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert not killlog.exists(), f"a session was killed at ORANGE ({sessions!r})"


def test_RED_still_kills_an_unattached_session(tmp_path):
    """The control for the deletion — and it had no coverage before this change.

    ORANGE stopped killing; RED did not. Without this arm, removing the ORANGE
    loop is indistinguishable from removing the tier stack's ability to kill at
    all, and the residual argument in `clean_cc_orange` (RED is the remaining
    escape hatch for descriptor-pinned space) would rest on nothing. MEASURED
    2026-09-25: no watchgod suite asserted on the RED kill site."""
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)
    killlog = home / "killed.log"
    # No STUB_ACTIVITY: RED kills every unattached cc- session outright and
    # never consults session_activity. The production script has no
    # display-message call site left at all.
    env = {"STUB_SESSIONS": "cc-42:0", "STUB_KILLLOG": str(killlog)}
    proc = _run(home, bind, _PRELUDE + "clean_cc_red", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert killlog.exists() and "cc-42" in killlog.read_text(), (
        "RED must still kill an unattached CC session"
    )
    # Negative control: RED honours the same unattached filter ORANGE used to.
    killlog.unlink()
    env["STUB_SESSIONS"] = "cc-43:1"
    proc = _run(home, bind, _PRELUDE + "clean_cc_red", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "RED must not kill an ATTACHED session"


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
            "STUB_JOURNAL": _KILL_LINE + "\nrun-u1234.scope: Failed with result 'oom-kill'.",
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
        + "STUB_JOURNAL_STALE=1; export STUB_JOURNAL_STALE; "
        + 'r1=$(check_oom_events "4:0:0:0:0"); echo "B1=$r1"; '
        + f'printf \'%s\' "low 0\nhigh 0\nmax 0\noom 3\noom_kill 6\noom_group_kill 0\n" > "{oom}"; '
        + 'r2=$(check_oom_events "$r1"); echo "B2=$r2"'
    )
    # Deliberately NOT overriding OOM_EVENTS_LOCAL_FILE here. This cell is one
    # of the two that actually failed on CI, so it has to keep binding the
    # lazy-derivation fix: pinning the path per-test fixes it a second time and
    # removes it from the net that proves the general fix works.
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
        + "r1=$(check_oom_events 4); "
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


def test_the_local_counter_path_follows_a_reassigned_events_file(tmp_path):
    """The local path must resolve when it is USED, not when the script is
    sourced.

    MEASURED, and this was a live CI failure rather than a hypothetical: bound
    at source time, `OOM_EVENTS_LOCAL_FILE` still pointed at the real
    /sys/fs/cgroup for every test that aims `OOM_EVENTS_FILE` at its fixture
    INSIDE the snippet. Those tests then read the HOST's cgroup — the very
    thing this file stubs journalctl to avoid. On a machine that has the file
    they are green; on a runner that does not, the trigger reads unverifiable
    and every suppression cell pages instead. Two cells failed on CI while
    passing locally for exactly this reason.

    The fixture below sets a LOCAL counter the host could not coincidentally
    match, so a read of the wrong file cannot produce this answer.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5, local_oom=41)
    out = _run(
        home,
        bind,
        _PRELUDE + f'OOM_EVENTS_FILE="{oom}"; echo "LOC=$(_read_oom_local_trigger)"',
        # deliberately NOT passing OOM_EVENTS_FILE in the env: the snippet
        # reassigns it after sourcing, which is the shape that broke.
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "LOC=41" in out.stdout, (
        "the local counter must be read from the fixture beside the reassigned "
        f"events file, not from the host cgroup: {out.stdout}"
    )


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
        + f'printf \'%s\\n\' "{_KILL_LINE}" > "{jfile}"; '
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
        + f'printf \'%s\\n%s\\n\' "{_KILL_LINE}" "{second}" > "{jfile}"; '
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
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": _KILL_LINE,
            "STUB_JOURNAL_RC_FILE": str(rcfile),
        },
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "ARM=5:0:0:0:1" in out.stdout, out.stdout
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert "emergency watchgod:oom" in calls, calls
    assert "could not be armed" in calls, calls
    # re-anchored: the fallback query's --show-cursor landed in the cursor file
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert cursor.exists() and cursor.read_text().startswith("s=stub")


def test_oom_arm_refuses_to_report_armed_when_the_cursor_write_fails(tmp_path):
    """An unwritable cursor path must not report itself as armed.

    SCOPE, corrected after verify-RED, because the first version of this
    docstring named a mechanism the cell does not detect: putting a DIRECTORY
    at the cursor path fails the write AND the read-back, so this cell stays
    green when the write's exit-status check alone is reverted. It binds the
    OUTCOME — drain=1 — and nothing finer. The write check is isolated by
    `…_a_failed_write_leaves_a_STALE_cursor`; the read-back by
    `…_reads_back_empty`. Both are needed and neither subsumes the other.

    A directory is used rather than a permission bit because it behaves the
    same when the suite runs as root.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    (home / ".genesis" / "logs" / ".oom_journal_cursor").mkdir()
    out = _run(
        home,
        bind,
        _PRELUDE + 'b=$(_oom_arm_baseline); echo "B=$b"',
        {"OOM_EVENTS_FILE": str(oom)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B=5:0:0:0:1" in out.stdout, (
        f"an unpersisted cursor must carry drain=1, not drain=0: {out.stdout}"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only file")
def test_oom_drain_is_not_cleared_by_a_query_that_failed_to_reanchor(tmp_path):
    """A SUCCESSFUL QUERY IS NOT AN ANCHORED CURSOR.

    Found by adversarial audit and reproduced before fixing: `drain` was
    cleared on `_oom_query_ok == 1`, but the re-anchor that a successful query
    performs swallowed its own write failure — the very `|| true` this change
    exists to remove, one function away. So drain was set correctly and thrown
    away one tick later, and the kill after that was suppressed.

    The sequence, with the cursor path unwritable throughout:

      arm    → drain=1, nothing persisted
      kill 1 → pages (drain did its job); its journal record is consumed
      kill 2 → GENUINE, and wrote no record of its own (a non-main process
               dying inside a surviving scope writes no unit-failure line —
               exactly the case the cursor exists to catch). The fallback
               window still offers kill 1's contained record, which accounts
               for it, and the page is suppressed.

    MEASURED before the fix: two kills, ONE page. Both must page.
    """
    home, _cc, bind = _sandbox(tmp_path)
    # Unwritable for every writer, including the re-anchor inside the query.
    (home / ".genesis" / "logs" / ".oom_journal_cursor").mkdir()
    oom = _oom_file(tmp_path, 4)
    out = _run(
        home,
        bind,
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + 's=$(_oom_arm_baseline); echo "ARM=[$s]"; '
        + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 5\noom_group_kill 0\n' > \"{oom}\"; "
        + 's=$(check_oom_events "$s"); echo "T1=[$s]"; '
        + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 6\noom_group_kill 0\n' > \"{oom}\"; "
        + 's=$(check_oom_events "$s"); echo "T2=[$s]"',
        {"STUB_JOURNAL": _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "ARM=[4:0:0:0:1]" in out.stdout, f"the arm must report drain=1: {out.stdout}"
    assert "T1=[5:0:0:0:1]" in out.stdout, (
        f"a query that could not re-anchor must NOT clear drain: {out.stdout}"
    )
    calls = (home / ".genesis" / "alerts" / "calls.log").read_text()
    assert calls.count("emergency watchgod:oom") == 2, (
        "both kills must page — the second is genuine and wrote no record of "
        f"its own, so only a stale record could have explained it: {calls}"
    )


def test_a_failed_query_cannot_clear_drain_even_with_a_cursor_on_disk(tmp_path):
    """The anchored check is SYNTAX — it proves a file parses, not that its
    position is trustworthy. On a failed query nothing re-anchored, so clearing
    drain there trusts a position nobody verified this epoch.

    Found by review at this head, confirmed by construction before fixing: a
    stale cursor surviving a failed arm passed the syntax check, cleared drain
    on a tick whose query FAILED, and — once the deficit TTL lapsed — a query
    from that stale position returned a pre-baseline contained record that
    accounted for a real line-less kill. One kill, zero pages.

    Two halves close it: a failed arm now deletes a stale cursor (the cell
    above), and this one — clearing drain requires the query to have SUCCEEDED,
    because only a successful query re-anchors the file to a position minted
    this epoch.
    """
    home, _cc, bind = _sandbox(tmp_path)
    (home / ".genesis" / "logs" / ".oom_journal_cursor").write_text("s=genuine;i=5")
    oom = _oom_file(tmp_path, 4)
    out = _run(
        home,
        bind,
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 5\noom_group_kill 0\n' > \"{oom}\"; "
        + 'r=$(check_oom_events "4:0:0:0:1"); echo "R=[$r]"',
        {"STUB_JOURNAL_RC": "1"},  # the query fails; the cursor file still parses
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "R=[5:0:1:" in out.stdout and out.stdout.rstrip().endswith(":1]"), (
        f"a failed query must not clear drain, whatever is on disk: {out.stdout}"
    )


def test_drain_is_SET_when_a_query_cannot_save_its_cursor(tmp_path):
    """The inverse of the cell below, and it is reachable from the ORDINARY state.

    `drain` means "the cursor is not anchored". The first fix made a verified
    anchor the only thing that could CLEAR it — but nothing ever SET it, so from
    a healthy `drain=0` a re-anchor that cannot persist deleted the cursor and
    left the flag at 0. The next query then falls back to the relative window
    and re-reads the record the previous tick already counted.

    MEASURED before this fix, starting from `4:0:0:0:0` with the cursor path
    unwritable: a contained kill, then a real kill that wrote no record of its
    own — TWO kills, ZERO pages. The existing drain cells all start from
    `drain=1`, so none of them could see it.
    """
    home, _cc, bind = _sandbox(tmp_path)
    (home / ".genesis" / "logs" / ".oom_journal_cursor").mkdir()
    oom = _oom_file(tmp_path, 4)
    out = _run(
        home,
        bind,
        _PRELUDE
        + f'OOM_EVENTS_FILE="{oom}"; '
        + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 5\noom_group_kill 0\n' > \"{oom}\"; "
        + 'r=$(check_oom_events "4:0:0:0:0"); echo "R=[$r]"',
        {"STUB_JOURNAL": _KILL_LINE},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "R=[5:0:0:0:1]" in out.stdout, (
        "a query that could not save its cursor must SET drain, not leave it at "
        f"whatever it was: {out.stdout}"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only file")
def test_a_failed_reanchor_does_not_leave_a_stale_cursor_behind(tmp_path):
    """A stale cursor is indistinguishable from a fresh anchor to anything that
    only checks the file exists — and that check is what clears drain.

    So when the re-anchor cannot persist, the OLD value is removed rather than
    left to be mistaken for the new one.
    """
    home, _cc, bind = _sandbox(tmp_path)
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    cursor.write_text("s=stale;i=1")
    cursor.chmod(0o444)
    oom = _oom_file(tmp_path, 4)
    try:
        out = _run(
            home,
            bind,
            _PRELUDE
            + f'OOM_EVENTS_FILE="{oom}"; '
            + f"printf 'low 0\nhigh 0\nmax 0\noom 3\noom_kill 5\noom_group_kill 0\n' > \"{oom}\"; "
            + 'r=$(check_oom_events "4:0:0:0:1"); echo "R=[$r]"',
            {"STUB_JOURNAL": _KILL_LINE},
        )
    finally:
        if cursor.exists():
            cursor.chmod(0o644)
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "R=[5:0:0:0:1]" in out.stdout, (
        f"drain must survive a re-anchor that could not persist: {out.stdout}"
    )
    assert not cursor.exists() or not cursor.read_text().startswith("s=stale"), (
        "the stale cursor must not survive a failed re-anchor"
    )


def test_oom_arm_refuses_when_journalctl_is_not_installed(tmp_path):
    """The one arming branch no mutation could reach.

    Every other cell runs with journalctl on PATH, so `command -v journalctl ||
    return 1` never fires and a mutation flipping it to `return 0` turns
    nothing red — the branch is real but unconstructible from the default
    harness. An install without systemd's journal is not hypothetical, and the
    contract is "rc 0 only when the cursor is persisted", which such a host can
    never satisfy.

    PATH is rebuilt with only the externals the arm path needs, so journalctl
    is genuinely absent rather than merely stubbed to fail — those are
    different branches.
    """
    home, _cc, bind = _sandbox(tmp_path)
    minimal = tmp_path / "nojournal"
    minimal.mkdir()
    for tool in ("bash", "awk", "dirname", "date", "cat", "sed", "tail", "mkdir", "rm"):
        src = shutil.which(tool)
        if src:
            (minimal / tool).symlink_to(src)
    assert not shutil.which("journalctl", path=str(minimal)), "journalctl must be absent"
    oom = _oom_file(tmp_path, 5)
    env = dict(os.environ, HOME=str(home), PATH=str(minimal), OOM_EVENTS_FILE=str(oom))
    out = subprocess.run(
        [
            shutil.which("bash") or "/bin/bash",
            "-c",
            f"source '{_WATCHGOD}'\nb=$(_oom_arm_baseline); echo \"B=$b\"",
        ],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B=5:0:0:0:1" in out.stdout, (
        f"no journalctl means no anchorable cursor, so drain=1: {out.stdout}"
    )
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert not cursor.exists(), "nothing should have been written"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only file")
def test_oom_arm_refuses_when_a_failed_write_leaves_a_STALE_cursor(tmp_path):
    """The case the read-back cannot see, and the reason the write's exit
    status is checked on its own.

    MEASURED while verify-RED'ing this fix: restoring the original `|| true` on
    the write turned NOTHING red, because the other cell puts a DIRECTORY at
    the cursor path — where the write fails AND the read-back fails, so the
    read-back alone produces the right answer. That cell asserts the outcome
    but isolates nothing.

    Here a VALID cursor file already exists and the write fails. The read-back
    then succeeds, returning the OLD position, and arming would report drain=0
    while the cursor points somewhere before the baseline — which is precisely
    the state in which a pre-baseline record can account for a post-baseline
    kill and suppress a real page.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    cursor.write_text("s=stale;i=1")
    cursor.chmod(0o444)
    try:
        out = _run(
            home,
            bind,
            _PRELUDE + 'b=$(_oom_arm_baseline); echo "B=$b"',
            {"OOM_EVENTS_FILE": str(oom)},
        )
    finally:
        if cursor.exists():
            cursor.chmod(0o644)
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B=5:0:0:0:1" in out.stdout, (
        "a write that failed over a VALID stale cursor must carry drain=1 — "
        f"the read-back cannot distinguish it from a fresh anchor: {out.stdout}"
    )
    # The stale file must be GONE, not preserved. It passes the anchored check
    # on syntax while its POSITION predates the baseline, so leaving it behind
    # is what let a pre-baseline record account for a post-baseline kill once
    # drain was cleared and the deficit TTL had lapsed (found by review at this
    # head, confirmed by construction: one real line-less kill, zero pages).
    # The rm succeeds despite the 0444 mode because deletion is a DIRECTORY
    # permission, which is also why this cell needs no root guard for this half.
    assert not cursor.exists(), "a failed arm must remove a stale cursor, not keep it"


def test_oom_arm_refuses_when_the_cursor_reads_back_empty(tmp_path):
    """The write can report success and still leave nothing behind — a full
    filesystem surfaces the failure on close, not on the write.

    Exercised by stubbing `cat`, the same way this suite already stubs
    journalctl: at the point it matters, an unreadable cursor and an absent one
    are the same thing, so they must get the same answer.
    """
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    _make_exec(bind / "cat", "#!/usr/bin/env bash\nexit 0\n")  # succeeds, prints nothing
    out = _run(
        home,
        bind,
        _PRELUDE + 'b=$(_oom_arm_baseline); echo "B=$b"',
        {"OOM_EVENTS_FILE": str(oom)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B=5:0:0:0:1" in out.stdout, (
        f"a cursor that does not read back must carry drain=1: {out.stdout}"
    )


def test_oom_late_arm_anchors_the_cursor_main_only_arms_once(tmp_path):
    """Codex P1 (#1790): `main` arms ONCE. If `memory.events` is unreadable at
    that moment the spec is empty — monitoring unavailable — and arming returns
    before it ever reaches the journal, so the cursor is never anchored.

    The baseline is then really established on the first tick where the counter
    reads, and that path emitted drain=0 with an unanchored cursor. Two halves
    are asserted, because either alone passes for the wrong reason: the cursor
    file must now EXIST (the late arm ran), and the emitted spec must be a real
    baseline rather than the empty one.
    """
    home, _cc, bind = _sandbox(tmp_path)
    missing = tmp_path / "not-yet-there"
    out = _run(
        home,
        bind,
        _PRELUDE
        + f'OOM_EVENTS_FILE="{missing}"; b=$(_oom_arm_baseline); echo "ARM=[$b]"; '
        # the counter becomes readable only now, one tick later
        + f'OOM_EVENTS_FILE="{_oom_file(tmp_path, 7)}"; '
        + 'r=$(check_oom_events "$b"); echo "B=$r"',
        {"OOM_EVENTS_FILE": str(missing)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "ARM=[]" in out.stdout, f"the startup arm must report unavailable: {out.stdout}"
    assert "B=7:0:0:0:0" in out.stdout, f"the late arm must establish a baseline: {out.stdout}"
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    assert cursor.exists(), "the late arm must anchor the journal cursor"
    assert cursor.read_text().startswith("s=stub"), cursor.read_text()


def test_oom_late_arm_carries_drain_when_it_cannot_anchor(tmp_path):
    """The other half of the late arm. If the cursor still cannot be persisted
    on that tick, the emitted spec must say so — otherwise the baseline reads
    as trustworthy while the next query has nothing to anchor against."""
    home, _cc, bind = _sandbox(tmp_path)
    missing = tmp_path / "not-yet-there"
    (home / ".genesis" / "logs" / ".oom_journal_cursor").mkdir()
    out = _run(
        home,
        bind,
        _PRELUDE
        + f'OOM_EVENTS_FILE="{missing}"; b=$(_oom_arm_baseline); '
        + f'OOM_EVENTS_FILE="{_oom_file(tmp_path, 7)}"; '
        + 'r=$(check_oom_events "$b"); echo "B=$r"',
        {"OOM_EVENTS_FILE": str(missing)},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "B=7:0:0:0:1" in out.stdout, f"an unanchorable late arm must carry drain=1: {out.stdout}"


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
        + f"printf 'low 0\\nhigh 0\\nmax 0\\noom 3\\noom_kill 6\\noom_group_kill 0\\n' > \"{oom}\"; "
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
        {
            "OOM_EVENTS_FILE": str(oom),
            "STUB_JOURNAL": _KILL_LINE,
            "STUB_JOURNAL_RC_FILE": str(rcfile),
        },
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
