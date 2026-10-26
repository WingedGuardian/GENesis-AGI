"""Explicit verification, outside hourly collection."""

import contextlib
import hashlib
import json

from . import catalog, query, store
from .locks import publication


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
    with store._locked(store.DEFAULT_LOCK), publication():
        selected = catalog.load(data)
        if not query.snapshot_compatible(data):
            return {"ok": False, "unavailable": "no compatible snapshot; run derive"}
        with contextlib.closing(query.connect(data, selected=selected)) as snap:
            snapshots = {v: _digest(snap, v) for v in query._DERIVED}
        with contextlib.closing(query.connect(data, live=True, selected=selected)) as live:
            comparisons = {
                v: {"snapshot": snapshots[v], "live": _digest(live, v)} for v in query._DERIVED
            }
        keys, excluded = store.compatible_sources(data, selected)
        return {
            "ok": all(v["snapshot"] == v["live"] for v in comparisons.values()) and not excluded,
            "coverage": {"included": len(keys), "excluded": excluded},
            "snapshot_current": query.derived_current(data, selected),
            "catalog_revision": selected.revision if selected is not None else None,
            "views": comparisons,
        }
