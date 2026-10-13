"""Tests for the durable alert queue (F.3) — genesis.guardian.alert.queue.

The queue is deliberately dependency-free, so these exercise it in isolation:
enqueue atomicity/permissions/dedup, oldest-first draining, the
terminal-vs-transient send contract, corrupt-entry quarantine, and prune caps.
"""

from __future__ import annotations

import json
import os
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


# --- schema-v1 validation (#3025) -------------------------------------------
# One file the readers cannot use must be quarantined, never raised: a raise
# from list_queued wedges both drains every tick, and makes every keyed
# enqueue_alert return False (the alert is dropped).


def _valid(**overrides) -> dict:
    entry = {
        "schema": 1,
        "ts": 1000.0,
        "severity": "warning",
        "source": "s",
        "title": "t",
        "body": "b",
        "dedupe_key": "k",
        "meta": {},
    }
    entry.update(overrides)
    return entry


# (id, raw JSON text) — each is NOT a deliverable v1 entry.
INVALID_ENTRIES = [
    ("ts-string", json.dumps(_valid(ts="soon"))),
    ("ts-null", json.dumps(_valid(ts=None))),
    ("ts-list", json.dumps(_valid(ts=[1]))),
    ("ts-bool", json.dumps(_valid(ts=True))),
    ("ts-nan", json.dumps(_valid()).replace('"ts": 1000.0', '"ts": NaN')),
    ("ts-infinity", json.dumps(_valid()).replace('"ts": 1000.0', '"ts": Infinity')),
    ("ts-missing", json.dumps({k: v for k, v in _valid().items() if k != "ts"})),
    ("schema-2", json.dumps(_valid(schema=2))),
    ("schema-missing", json.dumps({k: v for k, v in _valid().items() if k != "schema"})),
    ("schema-true", json.dumps(_valid(schema=True))),
    ("title-int", json.dumps(_valid(title=5))),
    ("body-list", json.dumps(_valid(body=["x"]))),
    ("severity-int", json.dumps(_valid(severity=3))),
    ("source-null", json.dumps(_valid(source=None))),
    ("title-and-body-empty", json.dumps(_valid(title="", body=""))),
    ("dedupe-key-int", json.dumps(_valid(dedupe_key=7))),
    ("meta-list", json.dumps(_valid(meta=["x"]))),
    ("meta-cause-int", json.dumps(_valid(meta={"likely_cause": 1}))),
    ("meta-url-dict", json.dumps(_valid(meta={"approval_url": {}}))),
    ("not-an-object", json.dumps([1, 2])),
    ("bad-json", "{not valid json"),
    ("ts-huge-int", json.dumps(_valid()).replace('"ts": 1000.0', '"ts": 1' + "0" * 400)),
]
# Every looped field, generated from the canonical tuples so dropping one from
# the check fails a test.
INVALID_ENTRIES += [(f"{f}-int", json.dumps(_valid(**{f: 5}))) for f in q._TEXT_FIELDS]
INVALID_ENTRIES += [
    (f"meta-{f}-int", json.dumps(_valid(meta={f: 5}))) for f in q._HOST_META_KEYS
]

VALID_ENTRIES = [
    ("full", json.dumps(_valid())),
    ("int-ts", json.dumps(_valid(ts=1000))),
    ("body-only", json.dumps(_valid(title="", body="b"))),
    ("title-only", json.dumps(_valid(title="t", body=""))),
    ("no-dedupe-key", json.dumps(_valid(dedupe_key=None))),
    ("meta-absent", json.dumps({k: v for k, v in _valid().items() if k != "meta"})),
    ("meta-null", json.dumps(_valid(meta=None))),
    ("meta-host-keys", json.dumps(_valid(meta={"likely_cause": "c", "proposed_action": None}))),
    ("meta-other-key-any-type", json.dumps(_valid(meta={"x": 1}))),
]


@pytest.mark.parametrize(("name", "raw"), INVALID_ENTRIES, ids=[n for n, _ in INVALID_ENTRIES])
def test_invalid_entry_is_quarantined_not_raised(tmp_path: Path, name: str, raw: str, caplog) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "bad.json").write_text(raw)
    q.enqueue_alert(root, severity="info", source="s", title="ok", body="b")
    with caplog.at_level("WARNING", logger="genesis.guardian.alert.queue"):
        items = _entries(root)
    assert [e["title"] for e in items] == ["ok"]
    assert (root / "bad.json.corrupt").exists()
    assert not (root / "bad.json").exists()
    assert any("bad.json" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(("name", "raw"), VALID_ENTRIES, ids=[n for n, _ in VALID_ENTRIES])
def test_valid_entry_shapes_are_kept(tmp_path: Path, name: str, raw: str) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "good.json").write_text(raw)
    assert len(_entries(root)) == 1
    assert (root / "good.json").exists()


def test_problem_names_the_failed_field() -> None:
    assert q.v1_entry_problem(_valid()) is None
    assert q.v1_entry_problem(_valid(ts="soon")) == "ts"
    assert q.v1_entry_problem(_valid(title=5)) == "title"
    assert q.v1_entry_problem(_valid(meta={"approval_url": 1})) == "meta.approval_url"
    assert q.v1_entry_problem(_valid(meta=["x"])) == "meta"  # names it, never raises
    assert q.v1_entry_problem([1]) == "entry"
    assert q.v1_entry_problem(_valid(ts=10**400)) == "ts"  # overflow, never raises


def test_unreadable_entry_is_kept_not_quarantined(tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file")
    root = tmp_path / "queue"
    root.mkdir()
    locked = root / "locked.json"
    locked.write_text(json.dumps(_valid()))
    locked.chmod(0)
    try:
        assert _entries(root) == []
        assert locked.exists()
        assert not (root / "locked.json.corrupt").exists()
    finally:
        locked.chmod(0o600)
    assert len(_entries(root)) == 1


@pytest.mark.asyncio
async def test_drain_proceeds_past_an_invalid_entry(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "0.000000-bad.json").write_text(json.dumps(_valid(ts="soon")))
    q.enqueue_alert(root, severity="info", source="s", title="real", body="b")
    seen: list[str] = []

    async def send(entry: dict) -> bool:
        seen.append(entry["title"])
        return True

    assert await q.drain(root, send) == 1
    assert seen == ["real"]


def test_keyed_enqueue_succeeds_with_an_invalid_file_present(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "bad.json").write_text(json.dumps(_valid(ts=None, dedupe_key="other")))
    assert q.enqueue_alert(root, severity="info", source="s", title="t", body="b", dedupe_key="k")
    assert [e["dedupe_key"] for e in _entries(root)] == ["k"]


def test_invalid_same_key_entry_does_not_suppress_enqueue(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    (root / "bad.json").write_text(json.dumps(_valid(title=5, dedupe_key="k")))
    assert q.enqueue_alert(root, severity="info", source="s", title="t", body="b", dedupe_key="k")
    assert [e["title"] for e in _entries(root)] == ["t"]


def test_prune_survives_an_invalid_entry_and_still_ages_out(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    old = time.time() - 20 * 24 * 3600
    (root / f"{old:.6f}-old.json").write_text(json.dumps(_valid(ts=old)))
    (root / "bad.json").write_text(json.dumps(_valid(ts="soon")))
    assert q.prune(root, max_age_s=14 * 24 * 3600) == 1
    assert _entries(root) == []


def test_prune_removes_old_corrupt_files_only(tmp_path: Path) -> None:
    import os

    root = tmp_path / "queue"
    root.mkdir()
    old_c = root / "old.json.corrupt"
    new_c = root / "new.json.corrupt"
    old_c.write_text("x")
    new_c.write_text("x")
    old = time.time() - 20 * 24 * 3600
    os.utime(old_c, (old, old))
    q.prune(root, max_age_s=14 * 24 * 3600)
    assert not old_c.exists()
    assert new_c.exists()


def test_host_meta_keys_match_what_the_host_drain_reads() -> None:
    """The check must cover every meta key the host drain reads, or a bad value
    for an unchecked key wedges the host drain again. Derived from the reader
    (guardian/check.py _alert_from_queue_entry), not restated."""
    import ast
    import inspect

    from genesis.guardian import check

    tree = ast.parse(inspect.getsource(check._alert_from_queue_entry))
    read = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "meta"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert read  # guard-the-guard: the reader really was parsed
    assert set(q._HOST_META_KEYS) == read



# --- round 2 (#3287): structural entries; a bound on quarantined files -------


def test_a_directory_named_like_an_entry_is_quarantined_once(tmp_path, caplog):
    """A directory called *.json can never be read: it is quarantined like bad
    content, not kept and warned about on every pass (review finding)."""
    root = tmp_path / "q"
    root.mkdir()
    (root / "odd.json").mkdir()
    (root / "ok.json").write_text(json.dumps(_valid()))
    with caplog.at_level("WARNING"):
        first = q.list_queued(root)
    assert [p.name for p, _ in first] == ["ok.json"]
    assert (root / "odd.json.corrupt").is_dir()
    caplog.clear()
    with caplog.at_level("WARNING"):
        q.list_queued(root)
    assert "odd" not in caplog.text, "no warning on the next pass"


def test_quarantined_files_are_bounded_by_count(tmp_path):
    """A producer writing bad entries every poll leaves at most max_files
    quarantined files: the oldest go first."""
    root = tmp_path / "q"
    root.mkdir()
    now = time.time()
    for i in range(12):
        p = root / f"bad{i:02d}.json.corrupt"
        p.write_text("x")
        os.utime(p, (now - 100 + i, now - 100 + i))
    removed = q.prune(root, max_files=5)
    left = sorted(p.name for p in root.glob("*.corrupt"))
    assert removed == 7
    assert left == [f"bad{i:02d}.json.corrupt" for i in range(7, 12)], "the newest kept"


def test_a_quarantined_directory_is_left_alone_by_prune(tmp_path):
    root = tmp_path / "q"
    root.mkdir()
    d = root / "odd.json.corrupt"
    d.mkdir()
    old = time.time() - 10 * q._PRUNE_MAX_AGE_S
    os.utime(d, (old, old))
    q.prune(root, max_files=0)
    assert d.is_dir()
