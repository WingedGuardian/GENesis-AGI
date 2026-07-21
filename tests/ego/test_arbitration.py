"""WS-2 P4 arbitration discount — annotation, sort, digest, isolation.

All fixtures synthetic. The discount is annotate-only: shadow renders
badges/escalation notes without touching sort order; enforce lets the
calibrated track record drive the digest sort; a proposal is never
suppressed (sovereignty invariant, design §5).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.schema import TABLES
from genesis.ego import proposals as proposals_mod
from genesis.ego.proposals import (
    ProposalWorkflow,
    _format_digest,
    _sort_proposals,
    annotate_calibration,
    arbitration_failure_counts,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego + calibration tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_state"])
        await conn.execute(TABLES["calibration_cells"])
        yield conn


@pytest.fixture
def workflow(db):
    return ProposalWorkflow(db=db, topic_manager=AsyncMock(), memory_store=AsyncMock())


async def _insert_cell(
    db,
    *,
    domain: str,
    status: str = "ok",
    n: int = 41,
    mean_confidence: float | None = 0.85,
    shrunk_estimate: float | None = 0.62,
    provenance: str = "stated",
    window_days: int = 90,
    action_class: str = "ego_proposal",
    metric: str = "approved_and_executes",
) -> None:
    await db.execute(
        "INSERT INTO calibration_cells (domain, action_class, metric, provenance,"
        " window_days, n, n_mechanical, base_rate, mean_confidence, brier,"
        " reliability, resolution, uncertainty, ece, shrunk_estimate, status,"
        " computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL,"
        " NULL, NULL, ?, ?, '2026-01-01T00:00:00+00:00')",
        (
            domain,
            action_class,
            metric,
            provenance,
            window_days,
            n,
            n,
            mean_confidence,
            shrunk_estimate,
            status,
        ),
    )
    await db.commit()


def _proposal(action_type: str = "dispatch", confidence: float = 0.9, **extra) -> dict:
    return {
        "action_type": action_type,
        "content": f"do the {action_type} thing",
        "rationale": "because the synthetic fixture says so",
        "confidence": confidence,
        "urgency": "normal",
        **extra,
    }


# ---------------------------------------------------------------------------
# annotate_calibration
# ---------------------------------------------------------------------------


class TestAnnotateCalibration:
    async def test_no_cell_no_annotation(self, db):
        p = _proposal()
        await annotate_calibration(db, p)
        assert "_calibration_note" not in p
        assert "_calibrated_confidence" not in p
        assert "_calibration_badge" not in p

    @pytest.mark.parametrize("status", ["thin", "unknown"])
    async def test_thin_unknown_escalation_note_only(self, db, status):
        await _insert_cell(db, domain="ego.dispatch", status=status, n=7)
        p = _proposal("dispatch")
        await annotate_calibration(db, p)
        assert "escalate" in p["_calibration_note"]
        assert status in p["_calibration_note"]
        assert "(n=7)" in p["_calibration_note"]
        # Never a discount on ignorance, never a bare percentage.
        assert "_calibrated_confidence" not in p
        assert "_calibration_badge" not in p
        assert "%" not in p["_calibration_note"]

    async def test_ok_gap_discounts_and_badges(self, db):
        await _insert_cell(
            db, domain="ego.dispatch", mean_confidence=0.85, shrunk_estimate=0.62, n=41
        )
        p = _proposal("dispatch", confidence=0.9)
        await annotate_calibration(db, p)
        assert p["_calibrated_confidence"] == pytest.approx(0.62)
        assert p["_calibration_badge"] == "⚖ stated 0.90 → track record 0.62 (n=41)"
        assert "_calibration_note" not in p

    async def test_ok_no_gap_untouched(self, db):
        await _insert_cell(db, domain="ego.dispatch", mean_confidence=0.70, shrunk_estimate=0.65)
        p = _proposal("dispatch")
        await annotate_calibration(db, p)
        assert "_calibrated_confidence" not in p
        assert "_calibration_badge" not in p

    async def test_ok_null_scoring_untouched(self, db):
        # A base-rate-style ok cell with NULL mean_confidence must never
        # produce a discount (the tool-lane shape).
        await _insert_cell(db, domain="ego.dispatch", mean_confidence=None, shrunk_estimate=None)
        p = _proposal("dispatch")
        await annotate_calibration(db, p)
        assert "_calibrated_confidence" not in p

    async def test_wrong_domain_metric_ignored(self, db):
        # A gap cell for a DIFFERENT action_type or metric never annotates.
        await _insert_cell(db, domain="ego.other")
        await _insert_cell(db, domain="ego.dispatch", metric="something_else")
        p = _proposal("dispatch")
        await annotate_calibration(db, p)
        assert "_calibrated_confidence" not in p

    async def test_policy_prior_lane_never_consulted(self, db):
        # A prior can never launder into the stated discount (design §4.2).
        await _insert_cell(db, domain="ego.dispatch", provenance="policy_prior")
        p = _proposal("dispatch")
        await annotate_calibration(db, p)
        assert "_calibrated_confidence" not in p

    async def test_lookup_failure_isolated_and_counted(self, db):
        await db.execute("DROP TABLE calibration_cells")
        await db.commit()
        p = _proposal("dispatch")
        await annotate_calibration(db, p)  # must not raise
        assert "_calibrated_confidence" not in p
        assert arbitration_failure_counts().get("dispatch") == 1


# ---------------------------------------------------------------------------
# create_batch wiring
# ---------------------------------------------------------------------------


class TestCreateBatchWiring:
    async def test_shadow_annotates_created_proposals(self, db, workflow, monkeypatch):
        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", lambda: "shadow")
        await _insert_cell(db, domain="ego.dispatch", mean_confidence=0.85, shrunk_estimate=0.62)
        _, ids, created = await workflow.create_batch([_proposal("dispatch", 0.9)])
        assert len(ids) == 1
        assert created[0]["_calibrated_confidence"] == pytest.approx(0.62)

    async def test_off_skips_lookup_entirely(self, db, workflow, monkeypatch):
        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", lambda: "off")
        called = False

        async def _spy(*a, **k):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr("genesis.db.crud.calibration_cells.list_cells", _spy)
        _, ids, created = await workflow.create_batch([_proposal("dispatch")])
        assert len(ids) == 1
        assert called is False
        assert "_calibrated_confidence" not in created[0]

    async def test_annotation_failure_never_blocks_batch(self, db, workflow, monkeypatch):
        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", lambda: "shadow")

        async def _boom(*a, **k):
            raise RuntimeError("synthetic lookup failure")

        monkeypatch.setattr("genesis.db.crud.calibration_cells.list_cells", _boom)
        _, ids, _created = await workflow.create_batch([_proposal("dispatch")])
        assert len(ids) == 1
        assert arbitration_failure_counts().get("dispatch") == 1


# ---------------------------------------------------------------------------
# Sort + digest
# ---------------------------------------------------------------------------


class TestSortAndDigest:
    def test_shadow_sort_uses_stated(self):
        a = _proposal("a", confidence=0.9, _calibrated_confidence=0.1)
        b = _proposal("b", confidence=0.5)
        assert _sort_proposals([b, a])[0] is a  # stated 0.9 still wins

    def test_enforce_sort_uses_calibrated(self):
        a = _proposal("a", confidence=0.9, _calibrated_confidence=0.1)
        b = _proposal("b", confidence=0.5)
        assert _sort_proposals([a, b], enforce_calibration=True)[0] is b

    def test_enforce_without_calibration_falls_back_to_stated(self):
        a = _proposal("a", confidence=0.9)
        b = _proposal("b", confidence=0.5)
        assert _sort_proposals([b, a], enforce_calibration=True)[0] is a

    def test_digest_renders_badge_and_note(self, monkeypatch):
        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", lambda: "shadow")
        digest = _format_digest(
            [
                _proposal(
                    "dispatch",
                    confidence=0.9,
                    _calibration_badge="⚖ stated 0.90 → track record 0.62 (n=41)",
                ),
                _proposal(
                    "research",
                    confidence=0.4,
                    _calibration_note="calibration: thin (n=7) — escalate; track record not yet trustworthy",
                ),
            ],
            "batch123",
        )
        assert "⚖ stated 0.90 → track record 0.62 (n=41)" in digest
        assert "escalate" in digest
        # Sort untouched in shadow: dispatch (0.9) numbered before research.
        assert digest.index("dispatch") < digest.index("research")

    def test_digest_enforce_reorders(self, monkeypatch):
        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", lambda: "enforce")
        digest = _format_digest(
            [
                _proposal("overconf", confidence=0.9, _calibrated_confidence=0.2),
                _proposal("modest", confidence=0.5),
            ],
            "batch123",
        )
        assert digest.index("modest") < digest.index("overconf")

    def test_digest_mode_read_failure_degrades_to_shadow(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("genesis.ledger.ws2_ledger_config.arbitration_mode", _boom)
        digest = _format_digest(
            [
                _proposal("overconf", confidence=0.9, _calibrated_confidence=0.1),
                _proposal("modest", confidence=0.5),
            ],
            "batch123",
        )
        # Fail-safe = shadow: stated order preserved, digest still renders.
        assert digest.index("overconf") < digest.index("modest")


# ---------------------------------------------------------------------------
# Failure-counter surface
# ---------------------------------------------------------------------------


class TestFailureCounter:
    def test_counts_snapshot_and_reset(self):
        proposals_mod._arbitration_failures["dispatch"] = 2
        assert arbitration_failure_counts() == {"dispatch": 2}
        proposals_mod._reset_arbitration_failures_for_tests()
        assert arbitration_failure_counts() == {}


# ---------------------------------------------------------------------------
# Health surfacing
# ---------------------------------------------------------------------------


async def test_compute_alerts_surfaces_arbitration_failures():
    """_compute_alerts emits ledger:arbitration_failed while the counter is
    nonzero — silently missing calibration annotations must not be silent."""
    from genesis.mcp.health import errors as health_errors

    proposals_mod._arbitration_failures["dispatch"] = 3
    alerts, current_ids = await health_errors._compute_alerts()
    assert "ledger:arbitration_failed:dispatch" in current_ids
    (alert,) = [a for a in alerts if a["id"] == "ledger:arbitration_failed:dispatch"]
    assert alert["severity"] == "WARNING"
    assert "without calibration annotations" in alert["message"]

    proposals_mod._reset_arbitration_failures_for_tests()
    _alerts, current_ids = await health_errors._compute_alerts()
    assert not any(i.startswith("ledger:arbitration_failed") for i in current_ids)
