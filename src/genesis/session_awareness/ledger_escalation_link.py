"""The dedup key that links a `session_ledger` row to its escalation follow-up.

One formula, one owner. A ledger row that goes undisposed long enough is
escalated into a `follow_ups` row (the escalation sweep); the link between the
two is the follow-up's `dedup_key`, because `follow_ups` has no column pointing
at another table's row and adding one for this would be a schema change to a
table two other subsystems are actively editing.

The formula lives here rather than in the sweep because THREE places need it and
two of them cannot import Genesis at all: `scripts/genesis_session_context.py`
and `scripts/genesis_urgent_alerts.py` are import-free hooks (stdlib only, so a
broken venv can never wedge a session), and they must render `-> escalated:
follow_up <id>` beside an open row so a revived session can see and close its own
escalations. Those two inline the same three lines; a parity test asserts the
inlined form equals this one, so a change here that they do not follow fails
loudly instead of silently unlinking every row.

`source='ledger_escalation'` is part of the key rather than implied, matching the
`"<source>|<stable identity>"` convention used by the other programmatic
follow-up creators (see `surplus/jobs/gates.py`).
"""

from __future__ import annotations

import hashlib

# GROUNDWORK(ledger-escalation) — SATISFIED 2026-09-06. The WRITER this note
# was waiting for now exists: `session_awareness/ledger_escalation.py`, an
# hourly learning-scheduler sweep that promotes an undisposed ledger row into a
# follow_ups row and completes it again when the row is disposed. The bet paid
# off exactly as intended — landing the formula here first made that sweep a
# pure add, with no change to either import-free hook script.
#
# The other three consumers still hold and still constrain any edit here: the
# two READ-side hooks (`scripts/genesis_session_context.py`,
# `scripts/genesis_urgent_alerts.py`) inline this same formula because they
# cannot import Genesis, and a parity test asserts the inlined form equals this
# one. Change the formula and every existing link silently breaks — the sweep
# would re-escalate rows that already have a follow-up, and the hooks would stop
# rendering the link. Do not delete as dead code.
ESCALATION_SOURCE = "ledger_escalation"


def escalation_dedup_key(ledger_id: str) -> str:
    """The `follow_ups.dedup_key` for the escalation of ledger row ``ledger_id``.

    Stable across runs and across the row's whole life: the dedup precheck spans
    ALL follow-up statuses, so a completed escalation is never re-created for the
    same row.
    """
    return hashlib.sha256(f"{ESCALATION_SOURCE}|{ledger_id}".encode()).hexdigest()
