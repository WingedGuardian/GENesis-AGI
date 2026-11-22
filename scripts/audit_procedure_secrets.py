#!/usr/bin/env python3
"""Report procedures whose stored text the credential scrub would change (#4859).

Read-only: opens the database ``mode=ro`` and writes one JSON report (default
``~/.genesis/output/procedure-secrets-audit.json``). It never prints or stores a
value: each finding is the procedure id, its tier and deprecation, the pattern
names that matched, and per span whether it looks like a real value or a
placeholder (``$VAR``, ``<...>``, ``{{...}}``, ``***``, ``xxx``). A span is judged on
the characters the scrub removed, so ``value_spans`` is an UPPER bound: a ``$`` or
``<`` just outside the removed span is missed. Redacting what it finds is a
separate, owner-approved step. Fields are scanned exactly as the write path scrubs
them (a JSON field decoded first). A row with a field the scrub could not process
is reported, and the run exits 2: the scan is incomplete. ``--out`` naming the
database or its -wal/-shm sidecar is refused (exit 1).

Usage: audit_procedure_secrets.py [--db PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.db.crud import procedural  # noqa: E402 - the write path's own field handling

_FIELDS = procedural._SCRUBBED_FIELDS
json_field_document, scrub_field = procedural.json_field_document, procedural.scrub_field

_PLACEHOLDER = re.compile(r"[$<{]|\*{3}|xxx|redacted|example|changeme", re.IGNORECASE)


def _spans(original: str, cleaned: str) -> list[str]:
    """For each redacted span: 'placeholder' or 'value', judged on the removed text."""
    kinds = []
    matcher = difflib.SequenceMatcher(None, original, cleaned, autojunk=False)
    for op, i1, i2, _, _ in matcher.get_opcodes():
        if op != "equal" and i2 > i1:
            kinds.append("placeholder" if _PLACEHOLDER.search(original[i1:i2]) else "value")
    return kinds


def audit(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(procedural_memory)")}
    fields = [f for f in _FIELDS if f in cols]
    rows = con.execute(
        f"SELECT id, activation_tier, deprecated, {', '.join(fields)} FROM procedural_memory"  # noqa: S608 — column names from a fixed list
    ).fetchall()
    findings = []
    for row in rows:
        names: set[str] = set()
        withheld: set[str] = set()
        kinds: list[str] = []
        for field in fields:
            text = row[field]
            if not text:
                continue
            # The write path's own handling: a JSON field is decoded and scrubbed
            # value by value (escaped quotes and keys included), prose as text.
            found: list[str] = []
            try:
                cleaned = scrub_field(field, text, found)
            except Exception:  # noqa: BLE001 - the write path withholds on any failure
                withheld.add(field)
                continue
            if "withheld" in found:  # a string in it could not be processed
                withheld.add(field)
                found = [name for name in found if name != "withheld"]
            if found:
                names.update(found)
                document = json_field_document(field, text)
                base = text if document is None else json.dumps(document, ensure_ascii=False)
                kinds += _spans(base, cleaned)
        if names or withheld:
            findings.append(
                {
                    "id": row["id"],
                    "tier": row["activation_tier"],
                    "deprecated": bool(row["deprecated"]),
                    "patterns": sorted(names),
                    "value_spans": kinds.count("value"),
                    "placeholder_spans": kinds.count("placeholder"),
                    "withheld_fields": sorted(withheld),
                }
            )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "procedures": len(rows),
        "with_findings": len(findings),
        "with_value_spans": sum(1 for f in findings if f["value_spans"]),
        "with_withheld_fields": sum(1 for f in findings if f["withheld_fields"]),
        "note": "value_spans is an upper bound (judged on removed characters only)",
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=Path.home() / "genesis/data/genesis.db")
    parser.add_argument(
        "--out", type=Path, default=Path.home() / ".genesis/output/procedure-secrets-audit.json"
    )
    args = parser.parse_args(argv)
    # The database and its WAL sidecars: overwriting any of them corrupts it. SQLite
    # opens the sidecars beside the file a symlink points to, so they are derived
    # from the resolved path as well as the name given.
    names = {args.db, args.db.resolve()}
    guarded = [*names, *(n.with_name(n.name + s) for n in names for s in ("-wal", "-shm"))]
    if any(
        args.out.resolve() == path.resolve()
        or (args.out.exists() and path.exists() and os.path.samefile(args.out, path))
        for path in guarded
    ):
        print(f"refusing to write the report over the database or a sidecar: {args.out}", file=sys.stderr)
        return 1
    report = audit(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{report['with_findings']} of {report['procedures']} procedures would change "
        f"({report['with_value_spans']} with value-like spans); report: {args.out}"
    )
    if report["with_withheld_fields"]:
        print(
            f"INCOMPLETE: {report['with_withheld_fields']} procedures had fields the scrub "
            "could not process; they were not scanned",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
