"""Tests for the durable alert queue (F.3) — genesis.guardian.alert.queue.

The queue is deliberately dependency-free, so these exercise it in isolation:
enqueue atomicity/permissions/dedup, oldest-first draining, the
terminal-vs-transient send contract, corrupt-entry quarantine, and prune caps.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from genesis.guardian.alert import queue as q


def _entries(root: Path) -> list[dict]:
    return [e for _, e in q.list_queued(root)]


def test_enqueue_writes_readable_entry(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    assert q.enqueue_alert(
        root,
        severity="emergency",
        source="backup",
        title="T",
        body="B",
        dedupe_key="k1",
        meta={"x": 1},
    )
    items = _entries(root)
    assert len(items) == 1
    e = items[0]
    assert e["schema"] == q.SCHEMA_VERSION
    assert (e["severity"], e["source"], e["title"], e["body"]) == ("emergency", "backup", "T", "B")
    assert e["dedupe_key"] == "k1"
    assert e["meta"] == {"x": 1}


def test_enqueue_entry_is_0600(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    q.enqueue_alert(root, severity="warning", source="s", title="t", body="b")
    path = next((root).glob("*.json"))
    assert (path.stat().st_mode & 0o777) == 0o600


def test_enqueue_never_raises_returns_false(tmp_path: Path) -> None:
    # root path is a *file* → mkdir fails → best-effort returns False, no raise.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    assert q.enqueue_alert(blocker, severity="warning", source="s", title="t", body="b") is False


def test_dedupe_key_collapses_live_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    assert (
        q.enqueue_alert(root, severity="warning", source="s", title="t", body="b", dedupe_key="dup")
        is True
    )
    assert (
        q.enqueue_alert(
            root, severity="warning", source="s", title="t2", body="b2", dedupe_key="dup"
        )
        is False
    )
    assert len(_entries(root)) == 1


def test_no_dedupe_key_allows_multiple(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    q.enqueue_alert(root, severity="warning", source="s", title="a", body="b")
    q.enqueue_alert(root, severity="warning", source="s", title="c", body="d")
    assert len(_entries(root)) == 2


def test_list_queued_oldest_first(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    for i, ts in enumerate([100.0, 50.0, 75.0]):
        (root / f"{ts:.6f}-{i}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "ts": ts,
                    "severity": "info",
                    "source": "s",
                    "title": f"t{i}",
                    "body": "",
                    "dedupe_key": None,
                    "meta": {},
                }
            )
        )
    order = [e["ts"] for e in _entries(root)]
    assert order == [50.0, 75.0, 100.0]


def test_malformed_entry_quarantined_not_wedging(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "bad.json").write_text("{not valid json")
    q.enqueue_alert(root, severity="info", source="s", title="ok", body="b")
    items = _entries(root)
    assert len(items) == 1 and items[0]["title"] == "ok"
    assert (root / "bad.json.corrupt").exists()
    assert not (root / "bad.json").exists()


@pytest.mark.asyncio
async def test_drain_delivers_and_unlinks(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    for i in range(3):
        q.enqueue_alert(root, severity="info", source="s", title=f"t{i}", body="b")
    seen: list[str] = []

    async def send(entry: dict) -> bool:
        seen.append(entry["title"])
        return True  # terminal → unlink

    removed = await q.drain(root, send)
    assert removed == 3
    assert sorted(seen) == ["t0", "t1", "t2"]
    assert _entries(root) == []


@pytest.mark.asyncio
async def test_drain_stops_on_transient_failure(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    for i, ts in enumerate([1.0, 2.0, 3.0]):
        (root / f"{ts:.6f}-{i}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "ts": ts,
                    "severity": "info",
                    "source": "s",
                    "title": f"t{i}",
                    "body": "",
                    "dedupe_key": None,
                    "meta": {},
                }
            )
        )
    calls: list[str] = []

    async def send(entry: dict) -> bool:
        calls.append(entry["title"])
        return entry["title"] == "t0"  # first terminal, second transient-fails

    removed = await q.drain(root, send)
    assert removed == 1  # only t0 unlinked
    assert calls == ["t0", "t1"]  # stopped after the failure, never tried t2
    remaining = sorted(e["title"] for e in _entries(root))
    assert remaining == ["t1", "t2"]


@pytest.mark.asyncio
async def test_drain_rejected_is_terminal_unlinks(tmp_path: Path) -> None:
    # A caller mapping REJECTED→True must see the entry removed, not stuck.
    root = tmp_path / "queue"
    q.enqueue_alert(root, severity="info", source="s", title="dup", body="b")

    async def send(entry: dict) -> bool:
        return True  # caller decided terminal (delivered OR rejected)

    assert await q.drain(root, send) == 1
    assert _entries(root) == []


@pytest.mark.asyncio
async def test_drain_send_raising_keeps_entry(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    q.enqueue_alert(root, severity="info", source="s", title="t", body="b")

    async def send(entry: dict) -> bool:
        raise RuntimeError("boom")

    removed = await q.drain(root, send)
    assert removed == 0
    assert len(_entries(root)) == 1  # kept, not lost


@pytest.mark.asyncio
async def test_drain_respects_max_per_run(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    for i in range(5):
        q.enqueue_alert(root, severity="info", source="s", title=f"t{i}", body="b")

    async def send(entry: dict) -> bool:
        return True

    removed = await q.drain(root, send, max_per_run=2)
    assert removed == 2
    assert len(_entries(root)) == 3


@pytest.mark.asyncio
async def test_drain_missing_root_is_noop(tmp_path: Path) -> None:
    async def send(entry: dict) -> bool:
        return True

    assert await q.drain(tmp_path / "nope", send) == 0


def test_prune_drops_aged_entries(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    now = time.time()
    old = now - (20 * 24 * 3600)
    (root / f"{old:.6f}-old.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "ts": old,
                "severity": "info",
                "source": "s",
                "title": "old",
                "body": "",
                "dedupe_key": None,
                "meta": {},
            }
        )
    )
    q.enqueue_alert(root, severity="info", source="s", title="fresh", body="b")
    removed = q.prune(root, max_age_s=14 * 24 * 3600)
    assert removed == 1
    titles = [e["title"] for e in _entries(root)]
    assert titles == ["fresh"]


def test_prune_caps_file_count_oldest_first(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    now = time.time()
    for i in range(5):
        ts = now - (5 - i)  # i=0 oldest
        (root / f"{ts:.6f}-{i}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "ts": ts,
                    "severity": "info",
                    "source": "s",
                    "title": f"t{i}",
                    "body": "",
                    "dedupe_key": None,
                    "meta": {},
                }
            )
        )
    removed = q.prune(root, max_files=2)
    assert removed == 3
    survivors = sorted(e["title"] for e in _entries(root))
    assert survivors == ["t3", "t4"]  # newest kept
