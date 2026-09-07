"""tmp_watchgod ORANGE loop-break + durable OOM capture.

Regression cover for the 2026-08-19 runaway: cc-tmp went ORANGE (a ~382MB pytest
tree the cache-evict never touches), and because the tier that dispatched the
handler was measured BEFORE cleanup, the daemon re-entered ORANGE every poll and
re-ran the idle-session kill loop for ~4.5h. The loop-break re-measures AFTER
cleanup; when nothing is reclaimable it records the stuck state once instead of
looping silently.

The kill itself is GONE as of 2026-09-07 — ORANGE never reaps a session. The
tests that pinned the kill are now their inverse (both an unattached idle
session and an attached one must SURVIVE an ORANGE that cleanup cannot clear),
which is what keeps the removal from being quietly reintroduced.

Plus the OOM sampler: on a NEW cgroup oom_kill it writes a durable snapshot and
pages once (the death that started all this left no diagnosable trace).

tmux is a configurable STUB (never a real session): it reports whatever sessions
the test injects and records every kill-session call to a file, so we can assert
that nothing was killed. queue_alert is overridden to a call-log so we can assert
page-once. The script's sourcing guard loads its functions without starting the
daemon.
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


def test_orange_persists_logs_once_no_page(tmp_path):
    """Non-reclaimable data keeps cc-tmp ORANGE and there is nothing else safe to
    do → per design D2 this does NOT page (only RED pages); it records the stuck
    state once (dedupe flag + a single STUCK log line), not silently every poll."""
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


def test_orange_never_kills_a_session(tmp_path):
    """ORANGE does not kill sessions — the removal, pinned.

    The old handler reaped every unattached tmux session idle over 2h once
    cleanup failed to clear the line. The loop-break comment guarding it already
    made the case against it: sessions are not what fills cc-tmp, so the kill
    freed essentially nothing while destroying a session's whole context. Across
    the full log history at removal time (2026-08-19 → 2026-09-07, 1,029 ORANGE
    polls) it was reached ONCE and killed ZERO sessions.

    The setup is the exact shape that used to reap: non-reclaimable data holding
    cc-tmp over the line, plus an unattached session idle far past 2h. Both must
    survive, and because nothing was reclaimed the stuck marker IS raised — the
    pre-removal code took the kill branch and left the marker unset.

    Verify-RED: against the pre-removal script this fails on both assertions.
    """
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)  # not a cache dir → ORANGE persists
    killlog = home / "killed.log"
    env = {
        "STUB_SESSIONS": "cc-77:0",  # unattached
        "STUB_ACTIVITY": "100",  # epoch 100 → idle for decades
        "STUB_KILLLOG": str(killlog),
    }
    proc = _run(home, bind, _PRELUDE + "clean_cc_orange", env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not killlog.exists(), "ORANGE killed a session; the kill was removed"
    stuck = home / ".genesis" / "alerts" / "tmp_orange_stuck"
    assert stuck.exists(), "nothing was reclaimed, so the stuck marker must be raised"


def test_orange_never_kills_an_attached_session(tmp_path):
    """The same for an ATTACHED session — a live terminal someone is looking at.
    It was already excluded by the ':0$' unattached filter; asserted here so the
    guarantee survives the filter being gone."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mb(cctmp / "bigdata" / "blob", 6)
    killlog = home / "killed.log"
    env = {"STUB_SESSIONS": "cc-5:1", "STUB_ACTIVITY": "100", "STUB_KILLLOG": str(killlog)}
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
