"""Canonical bitemporal timestamp handling for SQLite storage.

Every bitemporal comparison in Genesis is a raw SQLite TEXT comparison
(``invalid_at > ?`` against ``datetime.now(UTC).isoformat()``), so the
stored format IS the correctness contract. Canonical forms:

- Full timestamps: ``YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`` (UTC offset,
  never ``Z``, never naive, never space-separated — all three sort
  incorrectly against the canonical form in TEXT comparisons).
- Date-only ``YYYY-MM-DD`` (valid_at only): canonical by design —
  "valid from that date"; prefix-compares correctly against full ISO
  cutoffs. See the WS-H design (2026-07-03).

``canonical_iso`` is the single write-path gate; migration 0050
retrofits the legacy rows.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def canonical_iso(ts: str | None) -> str | None:
    """Normalize a timestamp string to canonical storage form.

    Returns the canonical string, the input unchanged for date-only
    values, or ``None`` for empty/unparseable input (callers decide the
    fallback — ``create_metadata`` falls back to ``created_at`` for
    ``valid_at``; ``invalidate_memory`` refuses to write garbage).
    """
    if not ts or not isinstance(ts, str):
        return None
    ts = ts.strip()
    if _DATE_ONLY_RE.match(ts):
        return ts
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # All Genesis writers stamp UTC; naive values are UTC by contract.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()
