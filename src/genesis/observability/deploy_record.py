"""Per-row deploy facts for ``update_history`` readers.

A success row in ``update_history`` proves the checkout moved, migrations ran,
and the host Guardian was redeployed — it does not by itself prove the
in-container ``genesis-server`` restarted onto the new commit. ``update.sh``
records that distinction in ``degraded_subsystems`` via the
``genesis-server-not-restarted`` marker (emitted by its post-update health
probe).

This module turns a stored ``(status, degraded_subsystems)`` pair into three
explicit facts so readers don't have to re-derive them:

- ``code_applied`` — the row claims the deploy ran (``status == 'success'``).
- ``activation_applied`` — the row counts as a full activation baseline
  (checkouts/migrations/host redeploy; deliberately the same condition).
- ``server_restarted`` — ``False`` when ``update.sh`` recorded the
  not-restarted marker, ``None`` otherwise: a row without the marker carries
  no positive restart evidence (pre-#2625 writers could leave the server
  down without recording it), so no claim is made either way.

``server_restarted`` is evidence from ``update.sh``'s health probe — a history
row never proves which commit the server is running; that comes from the
server's own boot identity (a follow-up). Stdlib-only leaf: no genesis imports.
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_RESTARTED_MARKER = "genesis-server-not-restarted"


def degraded_markers(degraded: str | None) -> tuple[str, ...]:
    """Split a stored ``degraded_subsystems`` value into exact tokens."""
    if not degraded:
        return ()
    return tuple(t for t in (part.strip() for part in degraded.split(",")) if t)


@dataclass(frozen=True)
class RowFacts:
    code_applied: bool
    activation_applied: bool
    server_restarted: bool | None

    def as_dict(self) -> dict[str, bool | None]:
        return {
            "code_applied": self.code_applied,
            "activation_applied": self.activation_applied,
            "server_restarted": self.server_restarted,
        }


def row_facts(status: str | None, degraded: str | None) -> RowFacts:
    """Derive the three deploy facts from a stored ``update_history`` row."""
    if status == "success":
        return RowFacts(
            code_applied=True,
            activation_applied=True,
            server_restarted=(
                False
                if NOT_RESTARTED_MARKER in degraded_markers(degraded)
                else None
            ),
        )
    return RowFacts(code_applied=False, activation_applied=False, server_restarted=None)
