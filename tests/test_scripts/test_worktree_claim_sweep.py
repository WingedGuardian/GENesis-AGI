"""Tests for the daily ownership sweep (scripts/worktree_claim_sweep.py).

The load-bearing case here is a HANDOFF, not a release, and it exists because of
a defect a green unit suite did not catch. A `claim` goes releasable when its
session exits or the worktree goes idle -- and neither of those says anything
about whether uncommitted work is still sitting in it. Plainly unlocking would
leave that work bare for the reaper, which runs from the same `disk_hygiene.sh`
invocation MINUTES later, and the reaper's restore path reconstructs untracked
files only: tracked modifications do not come back.

So the window is not "until tomorrow's sweep". It is the gap between two
consecutive steps of one script, and anything falling into it is unrecoverable.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))

_spec = importlib.util.spec_from_file_location(
    "worktree_claim_sweep", _ROOT / "scripts" / "worktree_claim_sweep.py"
)
sweep = importlib.util.module_from_spec(_spec)
sys.modules["worktree_claim_sweep"] = sweep
_spec.loader.exec_module(sweep)

wc = sweep.wc


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def P(rule: str, **extra) -> dict:
    return {"ns": wc.PAYLOAD_NAMESPACE, "v": 1, "rule": rule, **extra}


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "seed")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--quiet", "-b", "feature/x", str(wt))
    return wt


def _dead_claim(worktree: Path) -> None:
    """A claim whose process is gone -- i.e. releasable."""
    proc = subprocess.Popen(["sleep", "30"])
    pid, start = proc.pid, wc.proc_starttime(proc.pid)
    proc.kill()
    proc.wait(timeout=10)
    wc.lock_worktree(worktree, P("claim", pid=pid, start=start))


def test_a_releasable_claim_over_dirty_work_hands_off_instead_of_releasing(
    worktree: Path,
) -> None:
    """The defect this file exists for. Both halves are asserted.

    Releasing alone would be a silent data-loss window: the reaper runs next in
    the same script, and it does not restore tracked modifications.
    """
    (worktree / "README.md").write_text("uncommitted work from the departed session\n")
    _dead_claim(worktree)

    action, why = sweep._decide(worktree, wc.read_lock(worktree), now=time.time())
    assert action == "retake-dirty", why
    assert "uncommitted tracked changes remain" in why


def test_a_releasable_claim_over_a_clean_worktree_simply_releases(worktree: Path) -> None:
    """The control. Without this, 'retake-dirty' could be returned unconditionally
    and the test above would still pass -- proving nothing about the condition."""
    _dead_claim(worktree)
    action, why = sweep._decide(worktree, wc.read_lock(worktree), now=time.time())
    assert action == "release", why


def test_a_live_claim_is_kept_whether_or_not_the_worktree_is_dirty(
    worktree: Path, monkeypatch
) -> None:
    """A handoff must not fire while the owner is still working. The claim is the
    stronger statement; downgrading it to `dirty` would let the sweep release it
    as soon as the session committed."""
    monkeypatch.setattr(wc, "pid_is_live_session", lambda *a, **k: True)
    wc.lock_worktree(worktree, P("claim", pid=4242, start=1))
    (worktree / "README.md").write_text("still being worked on\n")

    action, _ = sweep._decide(worktree, wc.read_lock(worktree), now=time.time())
    assert action == "keep"


def test_the_handoff_leaves_a_dirty_lock_in_place_end_to_end(
    worktree: Path, monkeypatch, capsys
) -> None:
    """Runs the real handoff branch, not just the decision.

    Asserting the ACTION alone would pass against a main loop that computed the
    right answer and then did nothing with it.
    """
    (worktree / "README.md").write_text("uncommitted\n")
    _dead_claim(worktree)
    assert wc.read_lock(worktree).rule == "claim"

    monkeypatch.setattr(
        sweep, "_list_worktrees", lambda root: [{"path": str(worktree), "branch": "feature/x"}]
    )
    monkeypatch.setattr(sys, "argv", ["worktree_claim_sweep.py"])
    assert sweep.main() == 0

    lock = wc.read_lock(worktree)
    assert lock is not None, "the worktree must never be left unlocked with dirty work"
    assert lock.rule == "dirty"
    assert "HANDED OFF" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--release-only", "--take-only"])
def test_a_single_sided_sweep_keeps_the_claim_rather_than_half_handing_off(
    worktree: Path, monkeypatch, capsys, flag
) -> None:
    """A handoff is an unlock AND a lock. Doing only the first leaves the work
    bare; doing only the second is impossible while the claim stands. So in
    either single-sided mode the safe answer is to leave the claim alone."""
    (worktree / "README.md").write_text("uncommitted\n")
    _dead_claim(worktree)

    monkeypatch.setattr(
        sweep, "_list_worktrees", lambda root: [{"path": str(worktree), "branch": "feature/x"}]
    )
    monkeypatch.setattr(sys, "argv", ["worktree_claim_sweep.py", flag])
    assert sweep.main() == 0

    assert wc.read_lock(worktree).rule == "claim"
    assert "KEPT" in capsys.readouterr().out


def test_a_dry_run_changes_nothing(worktree: Path, monkeypatch, capsys) -> None:
    (worktree / "README.md").write_text("uncommitted\n")
    _dead_claim(worktree)

    monkeypatch.setattr(
        sweep, "_list_worktrees", lambda root: [{"path": str(worktree), "branch": "feature/x"}]
    )
    monkeypatch.setattr(sys, "argv", ["worktree_claim_sweep.py", "--dry-run"])
    assert sweep.main() == 0

    assert wc.read_lock(worktree).rule == "claim", "dry-run must not mutate the lock"
    assert "WOULD HAND OFF" in capsys.readouterr().out


def test_mode_off_takes_and_releases_nothing(worktree: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GENESIS_WORKTREE_OWNERSHIP", "1")
    (worktree / "README.md").write_text("uncommitted\n")
    _dead_claim(worktree)

    monkeypatch.setattr(sys, "argv", ["worktree_claim_sweep.py"])
    assert sweep.main() == 0
    assert wc.read_lock(worktree).rule == "claim"
    assert "disabled" in capsys.readouterr().out


def test_a_foreign_lock_on_a_long_idle_worktree_is_reported_not_released(
    worktree: Path, monkeypatch
) -> None:
    """A leaked third-party lock keeps the reaper away from that worktree
    permanently. It is still not ours to release, so the sweep says so loudly
    instead of acting -- silence is how the leak becomes permanent."""
    _git(worktree, "worktree", "lock", "--reason", "held by some other tool", str(worktree))
    monkeypatch.setattr(sweep, "_last_activity_time", lambda p: time.time() - 40 * 86400)

    action, why = sweep._decide(worktree, wc.read_lock(worktree), now=time.time())
    assert action == "foreign-stale"
    assert "release by hand" in why
    assert wc.read_lock(worktree) is not None


def test_a_recently_touched_foreign_lock_is_not_reported_as_a_leak(
    worktree: Path, monkeypatch
) -> None:
    """The control for the case above: without it, 'foreign-stale' could be
    returned for every foreign lock and the leak report would be noise."""
    _git(worktree, "worktree", "lock", "--reason", "held by some other tool", str(worktree))
    monkeypatch.setattr(sweep, "_last_activity_time", lambda p: time.time())

    action, _ = sweep._decide(worktree, wc.read_lock(worktree), now=time.time())
    assert action == "foreign"
