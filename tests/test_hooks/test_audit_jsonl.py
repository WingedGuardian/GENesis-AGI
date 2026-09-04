"""The shared audit writer: one file per flush, and retention that cannot destroy.

This module replaced a single-shared-file design whose maintenance ran on the hook
path. These tests pin the two properties that made the replacement worth doing —
the writer never resolves a name it does not create, and the pruner never deletes
the file a live writer is producing — plus the cost bound that keeps a cosmetic
helper from becoming a fail-open on a verdict path.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
import audit_jsonl as aj  # noqa: E402


def _files(d: Path) -> list[Path]:
    return sorted(f for f in d.glob("*.jsonl") if f.is_file() and not f.is_symlink())


def _rows(d: Path) -> list[dict]:
    return [json.loads(x) for f in _files(d) for x in f.read_text().splitlines() if x.strip()]


# ── writing ──────────────────────────────────────────────────────────────────


def test_a_batch_is_one_file_with_every_row(tmp_path):
    """All rows land together or not at all — a multi-sigil merge is one record."""
    path = aj.write_batch(str(tmp_path / "store"), [{"a": 1}, {"a": 2}, {"a": 3}])
    assert path is not None
    assert len(_files(tmp_path / "store")) == 1
    assert [r["a"] for r in _rows(tmp_path / "store")] == [1, 2, 3]


def test_the_store_and_its_files_are_own_user_only(tmp_path):
    d = tmp_path / "store"
    aj.write_batch(str(d), [{"a": 1}])
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(_files(d)[0].stat().st_mode) == 0o600


def test_a_planted_name_is_stepped_over_never_written_through(tmp_path):
    """``O_CREAT|O_EXCL|O_NOFOLLOW`` cannot open a name that already exists.

    This is the whole reason for the design: the previous writer opened one
    well-known name, so a symlink there redirected the write and a FIFO there
    blocked the caller in the kernel. Here both simply fail with EEXIST.
    """
    d = tmp_path / "store"
    d.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    stamp, pid = aj._stamp(), os.getpid()
    mp = pytest.MonkeyPatch()
    mp.setattr(aj, "_stamp", lambda: stamp)
    try:
        (d / f"{stamp}Z-{pid}-0.jsonl").symlink_to(victim)
        os.mkfifo(d / f"{stamp}Z-{pid}-1.jsonl")
        started = time.monotonic()
        path = aj.write_batch(str(d), [{"a": 1}])
        elapsed = time.monotonic() - started
    finally:
        mp.undo()
    assert elapsed < 2.0, "a planted FIFO blocked the writer"
    assert victim.read_text() == "untouched", "the write was redirected through a symlink"
    assert path is not None and path.endswith("-2.jsonl"), path


def test_a_short_write_still_produces_a_complete_record(tmp_path, monkeypatch):
    """``os.write`` may write fewer bytes than asked; a single call would truncate."""
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, buf: real_write(fd, buf[:1]))
    aj.write_batch(str(tmp_path / "store"), [{"a": "x" * 200}])
    monkeypatch.undo()
    assert _rows(tmp_path / "store") == [{"a": "x" * 200}]


def test_a_non_serialisable_row_costs_nothing_on_disk(tmp_path):
    """Serialisation happens BEFORE any filesystem call, so a bad row leaves no
    partial file behind — the failure mode is silence, not corruption."""
    d = tmp_path / "store"
    assert aj.write_batch(str(d), [{"a": object()}]) is None
    assert not d.exists() or _files(d) == []


def test_a_batch_past_the_row_cap_is_refused(tmp_path, capsys):
    """The cost bound. A sibling helper on this hook path once did work proportional
    to the command and exceeded a hook's registration timeout, which is a fail-open."""
    assert (
        aj.write_batch(str(tmp_path / "store"), [{"a": i} for i in range(aj._MAX_ROWS + 1)]) is None
    )
    assert "[audit-log]" in capsys.readouterr().err


def test_a_full_batch_is_written_well_inside_a_hook_budget(tmp_path):
    """The cost assertion, not just the cap: a maximum batch must be cheap."""
    started = time.monotonic()
    assert aj.write_batch(str(tmp_path / "store"), [{"a": i} for i in range(aj._MAX_ROWS)])
    assert time.monotonic() - started < 0.5


def test_an_empty_batch_writes_nothing(tmp_path):
    assert aj.write_batch(str(tmp_path / "store"), []) is None


def test_write_never_raises_on_an_unusable_store(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    assert aj.write_batch(str(blocker / "sub"), [{"a": 1}]) is None
    assert "[audit-log]" in capsys.readouterr().err


# ── retention ────────────────────────────────────────────────────────────────


def _seed(d: Path, n: int, size: int = 100) -> list[Path]:
    """Seed ``n`` records of roughly ``size`` bytes each, oldest first.

    Real JSONL rather than padding, so ``_rows`` can read the store afterwards —
    a seeded file that cannot be parsed would fail the readers, not the code.
    """
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        f = d / f"20200101T000000_{i:06d}Z-1-0.jsonl"
        body = json.dumps({"seeded": i, "pad": "x" * max(size - 30, 1)}) + "\n"
        f.write_text(body)
        out.append(f)
    return out


def test_trim_deletes_the_OLDEST_until_it_fits(tmp_path):
    d = tmp_path / "store"
    seeded = _seed(d, 10, size=100)
    aj.trim_dir_by_size(str(d), 450)
    survivors = _files(d)
    assert sum(f.stat().st_size for f in survivors) <= 450
    assert survivors[-1] == seeded[-1], "the newest record must survive"
    assert seeded[0] not in survivors, "the oldest must be the first to go"


def test_trim_never_deletes_the_newest_even_when_it_alone_exceeds_the_bound(tmp_path):
    """The property that makes this safe to run beside a live writer: a flush in
    progress is always the newest name, so it can never be the deletion candidate."""
    d = tmp_path / "store"
    _seed(d, 3, size=1000)
    aj.trim_dir_by_size(str(d), 10)
    assert len(_files(d)) == 1, "the newest record was deleted"


def test_a_failed_unlink_cannot_cost_the_newest_record(tmp_path, monkeypatch):
    """The survivor invariant must be STRUCTURAL, not arithmetic.

    An earlier version tracked survivors in a counter decremented inside a
    suppressed-OSError block, so one failed unlink stopped the count tracking
    reality: the loop kept deleting, walked past the last element, and took the
    newest record — the file a live writer may be mid-write on. Realistic triggers
    are all ordinary OSErrors: a second pruner racing the timer, EPERM on a sticky
    directory, EACCES on a partially-restored store.
    """
    d = tmp_path / "store"
    seeded = _seed(d, 3, size=1000)
    real_unlink = os.unlink
    monkeypatch.setattr(
        os,
        "unlink",
        lambda p: (
            (_ for _ in ()).throw(PermissionError(13, "nope"))
            if str(p) == str(seeded[0])
            else real_unlink(p)
        ),
    )
    aj.trim_dir_by_size(str(d), 10)
    monkeypatch.undo()
    survivors = _files(d)
    assert seeded[-1] in survivors, "the newest record was deleted after a failed unlink"


def test_a_partial_write_leaves_no_truncated_record(tmp_path, monkeypatch):
    """A truncated final line makes the WHOLE store unreadable to `cat …/*.jsonl`.

    The record is its own file, so removing it on a mid-write failure contains the
    damage to itself — which is what makes "all rows land or none do" true rather
    than aspirational.
    """
    d = tmp_path / "store"
    real_write = os.write
    calls = {"n": 0}

    def fail_midway(fd, buf):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, buf[:10])  # a genuine short write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", fail_midway)
    assert aj.write_batch(str(d), [{"a": "x" * 500}]) is None
    monkeypatch.undo()
    assert _files(d) == [], "a truncated record was left on disk"


def test_trim_is_a_no_op_under_the_bound(tmp_path):
    d = tmp_path / "store"
    seeded = _seed(d, 3, size=10)
    before = {f: f.stat().st_mtime_ns for f in seeded}
    assert aj.trim_dir_by_size(str(d), 10_000) == 0
    assert {f: f.stat().st_mtime_ns for f in _files(d)} == before


def test_trim_ignores_entries_it_must_not_follow_or_delete(tmp_path):
    """A symlink in the store is neither read through nor unlinked through."""
    d = tmp_path / "store"
    _seed(d, 2, size=100)
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (d / "20990101T000000_000000Z-1-0.jsonl").symlink_to(victim)
    aj.trim_dir_by_size(str(d), 10)
    assert victim.read_text() == "untouched"
    assert victim.exists()


def test_trim_on_a_missing_store_is_zero_and_creates_nothing(tmp_path):
    d = tmp_path / "never-used"
    assert aj.trim_dir_by_size(str(d), 100) == 0
    assert not d.exists()


def test_trim_and_a_live_writer_do_not_lose_a_record(tmp_path):
    """The concurrency claim, asserted rather than argued: the writer only ever
    creates new names and the pruner only ever deletes old whole files, so they
    cannot contend. No lock exists, and this is why none is needed."""
    d = tmp_path / "store"
    _seed(d, 20, size=100)
    written = aj.write_batch(str(d), [{"live": True}])
    aj.trim_dir_by_size(str(d), 300)
    assert written is not None
    assert Path(written).exists(), "the pruner deleted the record just written"
    assert {"live": True} in _rows(d)
