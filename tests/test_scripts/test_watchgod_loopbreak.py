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


def _oom_file(tmp_path, kills: int) -> Path:
    f = tmp_path / "memory.events"
    f.write_text(f"low 0\nhigh 0\nmax 0\noom 3\noom_kill {kills}\noom_group_kill 0\n")
    return f


def test_oom_increment_logs_and_pages(tmp_path):
    home, _cc, bind = _sandbox(tmp_path)
    oom = _oom_file(tmp_path, 5)
    out = _run(
        home,
        bind,
        _PRELUDE + 'result=$(check_oom_events 3); echo "BASELINE=$result"',
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
        _PRELUDE + 'result=$(check_oom_events 5); echo "BASELINE=$result"',
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
        _PRELUDE + 'result=$(check_oom_events 3); echo "BASELINE=$result"',
        {"OOM_EVENTS_FILE": str(tmp_path / "does-not-exist")},
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert "BASELINE=3" in out.stdout, out.stdout  # baseline preserved, no crash
