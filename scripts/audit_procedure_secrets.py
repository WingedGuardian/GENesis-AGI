#!/usr/bin/env python3
"""Report procedures whose stored text the credential scrub would change (#4859).

Read-only: opens the database ``mode=ro`` and writes one JSON report (default
``~/.genesis/output/procedure-secrets-audit.json``). It never prints or stores a
value: each finding is the procedure id, its tier and deprecation, the pattern
names that matched, and per span whether it looks like a real value or a
placeholder (``$VAR``, ``<...>``, ``{{...}}``, ``***``, ``xxx``). A span is judged on
the characters the scrub removed, so ``value_spans`` is an UPPER bound: a ``$`` or
``<`` just outside the removed span is missed. Redacting what it finds is a
separate, owner-approved step.

Usage: audit_procedure_secrets.py [--db PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.learning.procedural.credential_scrub import ScrubFailed, scrub_text  # noqa: E402

_FIELDS = (
    "principle",
    "scenario",
    "steps",
    "attempted_workarounds",
    "extraction_context",
    "lesson",
    "failure_modes",
)
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
            try:
                cleaned, found = scrub_text(text)
            except ScrubFailed:
                withheld.add(field)
                continue
            if found:
                names.update(found)
                kinds += _spans(text, cleaned)
        if names:
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
    report = audit(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{report['with_findings']} of {report['procedures']} procedures would change "
        f"({report['with_value_spans']} with value-like spans); report: {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
