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
- ``server_restarted`` — whether the health probe saw the server restart;
  ``False`` only on a success row carrying the not-restarted marker, ``None``
  on any non-success row (the row makes no claim either way).

``server_restarted`` is evidence from ``update.sh``'s health probe, not proof
of the commit currently running — a later restart clears the condition without
writing a new row. Stdlib-only leaf: no genesis imports.
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
            server_restarted=NOT_RESTARTED_MARKER not in degraded_markers(degraded),
        )
    return RowFacts(code_applied=False, activation_applied=False, server_restarted=None)
