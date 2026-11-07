"""Explicit verification, outside hourly collection.

A reader takes no lock: per-source records publish through atomic ``current``
swaps, and the snapshot's own ``derived/current`` link swaps atomically. The
snapshot is a disposable cache. Verification requires the configured reader to
load a current cache, then independently compares its rows with forced live
views over the same captured source records. Fallback reports unavailable.
"""

import contextlib
import hashlib
import json

from . import query, store


def _digest(con, view):
    digest, count = hashlib.sha256(), 0
    cursor = con.execute(f"SELECT * FROM {view} ORDER BY ALL")  # noqa: S608 — internal fixed view names
    schema = [(column[0], str(column[1])) for column in cursor.description]
    digest.update(json.dumps(schema, ensure_ascii=True).encode() + b"\n")
    while rows := cursor.fetchmany(1000):
        for row in rows:
            digest.update(json.dumps(row, default=str, ensure_ascii=True).encode() + b"\n")
            count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def verify(data):
    selection = store.select_sources(data)
    records, excluded = selection
    coverage = {"included": len(records), "excluded": excluded}
    # Reuse the configured, eager cache reader. A fallback is unavailable for
    # verification: comparing two live readers would certify no cached bytes.
    # snapshot_only, so a missing cache is not first computed live in full.
    try:
        snap = query.connect(data, selection=selection, snapshot_only=True)
    except query.NoSnapshot:
        return {
            "ok": False,
            "unavailable": "no readable current snapshot; run derive",
            "coverage": coverage,
            "snapshot_current": False,
        }
    with contextlib.closing(snap):
        comparisons = {v: {"snapshot": _digest(snap, v)} for v in query._DERIVED}
    # Immutable captured source records remain usable after cache closure. The
    # connections run sequentially so they do not each retain half the RAM cap.
    with contextlib.closing(query.connect(data, live=True, selection=selection)) as live:
        for view in query._DERIVED:
            comparisons[view]["live"] = _digest(live, view)
    return {
        "ok": all(v["snapshot"] == v["live"] for v in comparisons.values()) and not excluded,
        "coverage": coverage,
        "snapshot_current": True,
        "views": comparisons,
    }
