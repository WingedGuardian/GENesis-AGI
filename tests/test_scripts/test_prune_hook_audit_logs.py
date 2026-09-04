"""The daily retention step for the hook audit stores.

Run as a subprocess, the way ``disk_hygiene.sh`` runs it, because the properties that
matter are process-level: it must never exit non-zero (a failing prune must not skip
the rest of the groom) and it must no-op cleanly on a store that has never been
written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "prune_hook_audit_logs.py"
_HYGIENE = _REPO / "scripts" / "disk_hygiene.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def _seed(d: Path, n: int, size: int = 1000, *, age_s: float = 3600) -> None:
    """Seed a store with n records, BACKDATED by default.

    The mtime is set deliberately, not left at "now": the pruner refuses to
    delete a file touched inside its active-writer grace window, because two
    overlapping flushes mean the earlier writer's file is not the newest NAME and
    was otherwise an ordinary deletion candidate (Codex P2, PR #1609). Fixtures
    that write everything in the same millisecond are the one shape real stores
    never have — a store being trimmed by the daily timer holds files hours old —
    so backdating is what makes these tests model the thing they test. Pass
    ``age_s=0`` to exercise the protection itself.
    """
    d.mkdir(parents=True, exist_ok=True)
    when = time.time() - age_s
    for i in range(n):
        body = json.dumps({"seeded": i, "pad": "x" * max(size - 30, 1)}) + "\n"
        f = d / f"20200101T000000_{i:06d}Z-1-0.jsonl"
        f.write_text(body)
        os.utime(f, (when, when))


def test_an_oversize_store_is_trimmed_to_the_bound(tmp_path):
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    before = sum(f.stat().st_size for f in store.glob("*.jsonl"))
    r = _run(str(store), "--max-bytes", "5000")
    assert r.returncode == 0, r.stderr
    after = sum(f.stat().st_size for f in store.glob("*.jsonl"))
    assert after <= 5000 < before
    assert "removed" in r.stdout


def test_the_newest_record_survives_a_trim(tmp_path):
    """Restating the writer's invariant at the process level: a live flush is always
    the newest name, so the pruner must never be able to take it."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000)
    newest = sorted(store.glob("*.jsonl"))[-1]
    assert _run(str(store), "--max-bytes", "10").returncode == 0
    assert newest.exists()


def test_a_store_under_the_bound_is_left_alone(tmp_path):
    store = tmp_path / "store"
    _seed(store, 3, size=100)
    before = {f.name: f.stat().st_mtime_ns for f in store.glob("*.jsonl")}
    assert _run(str(store), "--max-bytes", "1000000").returncode == 0
    assert {f.name: f.stat().st_mtime_ns for f in store.glob("*.jsonl")} == before


def test_a_missing_store_is_a_clean_no_op(tmp_path):
    r = _run(str(tmp_path / "never-used"))
    assert r.returncode == 0
    assert "absent" in r.stdout
    assert not (tmp_path / "never-used").exists()


def test_a_file_where_a_store_should_be_reports_and_still_exits_zero(tmp_path):
    """Best-effort is a process-level contract: a broken store must not abort the
    groom, so this reports and exits 0 rather than raising."""
    bogus = tmp_path / "a-file"
    bogus.write_text("not a directory")
    r = _run(str(bogus))
    assert r.returncode == 0
    assert "absent" in r.stdout or "error" in r.stderr


def test_several_stores_are_each_trimmed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _seed(a, 20, size=1000)
    _seed(b, 20, size=1000)
    assert _run(str(a), str(b), "--max-bytes", "5000").returncode == 0
    for store in (a, b):
        assert sum(f.stat().st_size for f in store.glob("*.jsonl")) <= 5000


def test_disk_hygiene_actually_invokes_the_pruner():
    """Wiring, not behaviour: the step is worthless if the groom never runs it.

    A text assertion because running ``main()`` live would reap worktrees.
    """
    body = _HYGIENE.read_text()
    assert "prune_hook_audit_logs.py" in body, "the groom does not invoke the pruner"
    main_body = body.split("\nmain() {", 1)[-1] if "\nmain() {" in body else body
    assert "prune_hook_audit_logs.py" in main_body, "the invocation is outside main()"


def test_a_recently_written_file_is_not_trimmed(tmp_path):
    """The protection itself. Two overlapping flushes mean the EARLIER writer's
    file is not the newest name — so excluding only `files[-1]` left it an
    ordinary deletion candidate, and unlinking it made that writer return a path
    that no longer exists as a successful write (Codex P2, PR #1609)."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000, age_s=0)  # everything written just now
    r = _run(str(store), "--max-bytes", "10")
    assert r.returncode == 0, r.stderr
    assert len(list(store.glob("*.jsonl"))) == 20, (
        "the pruner deleted files a concurrent flush could still be writing"
    )


def test_old_files_are_still_trimmed_when_a_new_one_exists(tmp_path):
    """CONTROL, and the one that stops the grace window becoming a no-op: a store
    with one fresh file and many old ones must still shrink."""
    store = tmp_path / "store"
    _seed(store, 20, size=1000)  # backdated
    fresh = store / "20990101T000000_000000Z-1-0.jsonl"
    fresh.write_text(json.dumps({"fresh": True}) + "\n")
    r = _run(str(store), "--max-bytes", "5000")
    assert r.returncode == 0, r.stderr
    assert fresh.exists(), "the newest file was taken"
    assert sum(f.stat().st_size for f in store.glob("*.jsonl")) <= 5100
