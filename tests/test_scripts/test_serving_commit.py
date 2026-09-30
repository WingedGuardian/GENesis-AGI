"""scripts/lib/serving_commit.py: which commit the server booted from.

Real git repositories, with each move of HEAD timed by GIT_COMMITTER_DATE (the
reflog records the committer time of the ref update), then the reader run as the
deploy script runs it. The pruned-middle case is the reason for the continuity
check: without it, an expired entry makes an older move look like the answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import REPO

READER = REPO / "scripts" / "lib" / "serving_commit.py"


def _g(repo: Path, *args: str, at: int | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t")
    env.update(GIT_COMMITTER_EMAIL="t@e")
    if at is not None:
        env.update(GIT_COMMITTER_DATE=f"@{at} +0000", GIT_AUTHOR_DATE=f"@{at} +0000")
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    """Three commits at t=1000, 2000 and 3000 — A, B, C — each a move of HEAD."""
    r = tmp_path / "r"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")  # scrubbed: an inherited GIT_DIR would redirect it
    shas = {}
    for name, at in (("A", 1000), ("B", 2000), ("C", 3000)):
        (r / "f").write_text(name)
        _g(r, "add", "f")
        _g(r, "commit", "-qm", name, at=at)
        shas[name] = _g(r, "rev-parse", "HEAD")
    return r, shas


def _read(
    r: Path, boot: int | str, cutoff: int | str = 0, *, held: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(READER),
            *(["--held"] if held else []),
            str(r / ".git/logs/HEAD"),
            str(boot),
            _g(r, "rev-parse", "HEAD"),
            str(cutoff),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("boot", "want"),
    [(2500, "B"), (2001, "B"), (1999, "A"), (9999, "C")],
    ids=["between-moves", "a-second-after-a-move", "a-second-before-a-move", "after-the-last"],
)
def test_the_newest_move_before_the_boot(repo, boot, want):
    r, shas = repo
    out = _read(r, boot)
    assert (out.returncode, out.stdout.strip()) == (0, shas[want]), out


def test_a_move_in_the_boots_own_second_is_unknown(repo):
    """Both clocks count whole seconds: the move at 2000 may have come after a
    boot at 2000.4, so either answer could be wrong."""
    r, _ = repo
    out = _read(r, 2000)
    assert out.returncode == 1 and "same second" in out.stdout, out.stdout


def test_a_move_that_changed_nothing_in_that_second_is_harmless(repo):
    r, shas = repo
    _g(r, "checkout", "-q", "-b", "side", at=5000)  # old == new
    assert _read(r, 5000).stdout.strip() == shas["C"]


def test_a_detour_expired_as_a_pair_is_unknown_past_the_cutoff(tmp_path):
    """git gc expires a detour's entries (A->F and F->A) together once they are
    unreachable, and the chain left behind has no gap. Before the expiry the
    answer is F; after it the file alone would say A, so a boot older than the
    cutoff is unknown."""
    r = tmp_path / "d"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")  # scrubbed: an inherited GIT_DIR would redirect it
    (r / "f").write_text("a")
    _g(r, "add", "f")
    _g(r, "commit", "-qm", "A", at=1000)
    _g(r, "checkout", "-qb", "feat", at=1050)
    (r / "f").write_text("f")
    _g(r, "commit", "-qam", "F", at=1100)
    f_sha = _g(r, "rev-parse", "HEAD")
    _g(r, "checkout", "-q", "main", at=1600)
    _g(r, "branch", "-qD", "feat", at=1650)
    (r / "f").write_text("c")
    _g(r, "commit", "-qam", "C", at=1700)
    assert _read(r, 1500).stdout.strip() == f_sha, "control: before the expiry"
    _g(r, "reflog", "expire", "--expire=never", "--expire-unreachable=now", "--all")
    out = _read(r, 1500, cutoff=1501)
    assert out.returncode == 1 and "expiry cutoff" in out.stdout, out.stdout


def test_an_unreadable_cutoff_is_unknown(repo):
    r, _ = repo
    out = _read(r, 2500, cutoff="")
    assert out.returncode == 1 and "gc.reflogExpireUnreachable" in out.stdout, out.stdout


def test_a_reset_backwards_is_a_move_too(repo):
    r, shas = repo
    _g(r, "reset", "-q", "--hard", shas["A"], at=4000)
    assert _read(r, 4500).stdout.strip() == shas["A"]
    assert _read(r, 3500).stdout.strip() == shas["C"]


# --held: the boot commit, then every commit HEAD has held since. The server
# imports src/ lazily, so any of them can be loaded; the two ends alone cannot say
# (Devin, #2557 round 3).
@pytest.mark.parametrize(
    ("boot", "want"),
    [(1500, ["A", "B", "C"]), (2500, ["B", "C"]), (9999, ["C"])],
    ids=["two-moves-since", "one-move-since", "none-since"],
)
def test_held_is_the_boot_commit_then_every_commit_since(repo, boot, want):
    r, shas = repo
    out = _read(r, boot, held=True)
    assert (out.returncode, out.stdout.split()) == (0, [shas[w] for w in want]), out


def test_held_keeps_a_detour_the_tree_came_back_from(repo):
    """Boot at C, a move to A, a move back to C: both ends are C, and A is the
    commit only the history shows."""
    r, shas = repo
    _g(r, "reset", "-q", "--hard", shas["A"], at=4000)
    _g(r, "reset", "-q", "--hard", shas["C"], at=5000)
    out = _read(r, 3500, held=True)
    assert out.stdout.split() == [shas["C"], shas["A"], shas["C"]], out
    assert _read(r, 3500).stdout.strip() == shas["C"], "the default output is unchanged"


def test_held_is_unknown_when_the_boot_commit_is(repo):
    r, _ = repo
    out = _read(r, 500, held=True)
    assert out.returncode == 1 and out.stdout.startswith("unknown:"), out


@pytest.mark.parametrize(
    ("mutate", "boot", "reason"),
    [
        # The boot is older than the reflog's first entry.
        (None, 500, "starts after the server booted"),
        # An entry after the boot was pruned: B's move is gone, so the file shows
        # A then C with C's OLD not equal to A's NEW. Without the continuity check
        # the answer would be A, while the server booted from B.
        ("prune-middle", 2500, "gap"),
        # The newest entry was dropped: the reflog no longer reaches HEAD.
        ("drop-last", 2500, "does not end at HEAD"),
        # A move written earlier in the file but timed after the boot.
        ("clock-back", 2500, "backwards"),
        ("garbage", 2500, "documented shape"),
    ],
)
def test_every_way_the_reflog_cannot_prove_it_is_unknown(repo, mutate, boot, reason):
    r, _ = repo
    log = r / ".git" / "logs" / "HEAD"
    lines = log.read_text().splitlines()
    if mutate == "prune-middle":
        lines = [lines[0], lines[2]]
    elif mutate == "drop-last":
        lines = lines[:2]
    elif mutate == "clock-back":
        f = lines[0].split("\t", 1)
        head = f[0].split()
        head[-2] = "2600"
        lines[0] = " ".join(head) + "\t" + f[1]
    elif mutate == "garbage":
        lines.insert(1, "not a reflog line")
    log.write_text("\n".join(lines) + "\n")
    out = _read(r, boot)
    assert out.returncode == 1, out
    assert out.stdout.startswith("unknown:") and reason in out.stdout, out.stdout


@pytest.mark.parametrize("boot", ["0", "abc", ""])
def test_no_recorded_start_is_unknown(repo, boot):
    r, _ = repo
    out = _read(r, boot)
    assert out.returncode == 1 and out.stdout.startswith("unknown:"), out


def test_an_unreadable_reflog_is_unknown(tmp_path):
    out = subprocess.run(
        [sys.executable, str(READER), str(tmp_path / "absent"), "5", "deadbeef", "0"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 1 and "cannot read the reflog" in out.stdout, out
    assert "Traceback" not in out.stderr
