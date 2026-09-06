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

# GROUNDWORK(ledger-escalation): the WRITER — the sweep that promotes an
# undisposed ledger row into a follow_ups row — is not built yet. Today the only
# consumers are the two READ-side hooks, which render "-> escalated: follow_up
# <id>" beside an open row and correctly degrade to nothing while no such row
# exists. Landing the formula here first means that sweep is a pure add rather
# than a change to two import-free hook scripts. Do not delete as dead code.
ESCALATION_SOURCE = "ledger_escalation"


def escalation_dedup_key(ledger_id: str) -> str:
    """The `follow_ups.dedup_key` for the escalation of ledger row ``ledger_id``.

    Stable across runs and across the row's whole life: the dedup precheck spans
    ALL follow-up statuses, so a completed escalation is never re-created for the
    same row.
    """
    return hashlib.sha256(f"{ESCALATION_SOURCE}|{ledger_id}".encode()).hexdigest()
