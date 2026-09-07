"""Tests for the capability_grants table, migration 0030, and its CRUD (WS-8).

Covers the fresh-install path (create_all_tables / _tables.py), the versioned
migration (up/down/idempotency), CHECK constraints, and CRUD semantics — the
4-state machine persisted, success/correction counters, and the granted→ask
regression below the competence floor.  DARK substrate: no runtime caller yet.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.autonomy.capabilities import InvalidTransition
from genesis.autonomy.types import CellEvent, CellState
from genesis.db.crud import capability_grants as cg

MIGRATION = importlib.import_module("genesis.db.migrations.0030_capability_grants")
MIGRATION_PRD = importlib.import_module("genesis.db.migrations.0033_autonomy_earn_lose")

_EMAIL = {"domain": "email", "verb": "send", "risk_class": "standard"}
#: A synthetic domain outside PROMOTABLE_DOMAINS. Deliberately not a real
#: capability name — this suite tests the CLASS, not one member of it.
_WIDGET = {"domain": "widget", "verb": "poke", "risk_class": "standard"}
_TS = "2026-06-21T00:00:00"


@pytest.fixture
async def db(tmp_path):
    """Fresh DB via the real migration up()s (0030 + the PR-D 0033 columns the
    CRUD now reads/writes)."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await MIGRATION.up(conn)  # up() must not commit — runner owns the txn
        await MIGRATION_PRD.up(conn)
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_table_and_index_exist(self, db):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_grants'"
        )
        assert await cur.fetchone() is not None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_capability_grants_domain'"
        )
        assert await cur.fetchone() is not None

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)  # IF NOT EXISTS → must not raise
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capability_grants'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_down_drops_table(self, tmp_path):
        path = str(tmp_path / "down.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capability_grants'"
            )
            assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_fresh_install_creates_table(self, tmp_path):
        """create_all_tables (the fresh-install / test path) creates it too."""
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='capability_grants'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_rejects_bad_state_and_risk_class(self, db):
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO capability_grants (id, domain, verb, risk_class, state) "
                "VALUES ('x', 'email', 'send', 'standard', 'BOGUS')"
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO capability_grants (id, domain, verb, risk_class) "
                "VALUES ('y', 'email', 'send', 'BOGUS')"
            )


# --------------------------------------------------------------------------- #
# CRUD + state machine
# --------------------------------------------------------------------------- #
class TestCrud:
    @pytest.mark.asyncio
    async def test_ensure_creates_not_determined(self, db):
        row = await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        assert row["state"] == CellState.NOT_DETERMINED.value
        assert row["id"] == "email:send:standard"
        assert row["successes"] == 0 and row["corrections"] == 0

    @pytest.mark.asyncio
    async def test_ensure_is_idempotent(self, db):
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        rows = await cg.list_all(db)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_classify_then_approve_grants_and_stamps(self, db):
        s1 = await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        assert s1 == CellState.ASK
        s2 = await cg.apply_event(
            db,
            origin_class="first_party",
            event=CellEvent.APPROVE,
            updated_at="2026-06-21T01:00:00",
            **_EMAIL,
        )
        assert s2 == CellState.GRANTED
        row = await cg.get_cell(db, **_EMAIL)
        assert row["state"] == CellState.GRANTED.value
        assert row["granted_at"] == "2026-06-21T01:00:00"

    @pytest.mark.asyncio
    async def test_illegal_event_raises(self, db):
        # APPROVE from NOT_DETERMINED is illegal.
        with pytest.raises(InvalidTransition):
            await cg.apply_event(
                db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
            )

    @pytest.mark.asyncio
    async def test_record_success_increments(self, db):
        await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        row = await cg.get_cell(db, **_EMAIL)
        assert row["successes"] == 2
        assert row["last_used_at"] == _TS

    @pytest.mark.asyncio
    async def test_correction_regresses_granted_cell_below_floor(self, db):
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        # 0 successes, 1 correction → posterior 1/3 < 0.50 → regress to ASK.
        state = await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK
        row = await cg.get_cell(db, **_EMAIL)
        assert row["state"] == CellState.ASK.value
        assert row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_correction_demotes_any_granted_cell(self, db):
        # WS-8 PR-D "easy to lose": even a heavily-supported GRANTED cell
        # regresses to ASK on a SINGLE correction (deterministic, NOT posterior-
        # gated).  The well-supported counters only make it cheaper to re-earn.
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        for _ in range(5):
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        state = await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK
        row = await cg.get_cell(db, **_EMAIL)
        assert row["state"] == CellState.ASK.value
        assert row["granted_at"] is None  # decay-clock origin cleared on demotion

    @pytest.mark.asyncio
    async def test_correction_on_non_granted_is_inert(self, db):
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        state = await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK  # unchanged; only counter moved
        row = await cg.get_cell(db, **_EMAIL)
        assert row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_regrant_restamps_granted_at(self, db):
        # grant → correction-regress → re-approve must refresh granted_at to the
        # most recent grant (the decay clock must not see it as stale-old).
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        assert (await cg.get_cell(db, **_EMAIL))["granted_at"] == _TS
        # 0 successes + 1 correction → regress to ASK.
        assert (
            await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
            == CellState.ASK
        )
        later = "2026-06-22T12:00:00"
        s = await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=later, **_EMAIL
        )
        assert s == CellState.GRANTED
        assert (await cg.get_cell(db, **_EMAIL))["granted_at"] == later

    @pytest.mark.asyncio
    async def test_correction_atomic_single_state(self, db):
        # The counter increment and regression land together (one UPDATE).
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        state = await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        row = await cg.get_cell(db, **_EMAIL)
        assert state == CellState.ASK
        assert row["state"] == CellState.ASK.value and row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_list_all_orders_by_key(self, db):
        await cg.ensure_cell(db, domain="email", verb="send", risk_class="bulk", updated_at=_TS)
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        rows = await cg.list_all(db)
        assert [r["risk_class"] for r in rows] == ["bulk", "standard"]


# --------------------------------------------------------------------------- #
# WS-8 PR-D — consequence-weighted demotion + re-earn + promotion detection
# --------------------------------------------------------------------------- #
class TestPRDCompetence:
    @pytest.mark.asyncio
    async def test_correction_accumulates_severity_weight(self, db):
        # A standard correction adds 1.0; a bulk correction adds 2.0.
        await cg.record_correction(
            db,
            origin_class="first_party",
            updated_at=_TS,
            domain="email",
            verb="send",
            risk_class="standard",
        )
        assert (await cg.get_cell(db, "email", "send", "standard"))[
            "weighted_corrections"
        ] == pytest.approx(1.0)
        await cg.record_correction(
            db,
            origin_class="first_party",
            updated_at=_TS,
            domain="email",
            verb="send",
            risk_class="bulk",
        )
        assert (await cg.get_cell(db, "email", "send", "bulk"))[
            "weighted_corrections"
        ] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_consequence_weight_override(self, db):
        await cg.record_correction(
            db, origin_class="first_party", updated_at=_TS, consequence_weight=3.5, **_EMAIL
        )
        row = await cg.get_cell(db, **_EMAIL)
        assert row["weighted_corrections"] == pytest.approx(3.5)
        assert row["corrections"] == 1

    @pytest.mark.asyncio
    async def test_correction_on_ask_accrues_crater_without_regress(self, db):
        # Demotion fires only on GRANTED; an ASK cell stays ASK but still
        # accumulates the weighted crater (a rejected held send is negative).
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        state = await cg.record_correction(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert state == CellState.ASK
        assert (await cg.get_cell(db, **_EMAIL))["weighted_corrections"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_touch_used_bumps_last_used_not_successes(self, db):
        await cg.ensure_cell(db, updated_at=_TS, **_EMAIL)
        assert await cg.touch_used(db, used_at="2026-06-22T00:00:00", **_EMAIL) is True
        row = await cg.get_cell(db, **_EMAIL)
        assert row["last_used_at"] == "2026-06-22T00:00:00"
        assert row["successes"] == 0  # autonomous use is not a competence signal

    @pytest.mark.asyncio
    async def test_detect_promotable_requires_min_n_and_threshold(self, db):
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        for _ in range(4):  # below MIN_PROMOTE_N
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert await cg.detect_promotable_cells(db) == []
        await cg.record_success(
            db, origin_class="first_party", updated_at=_TS, **_EMAIL
        )  # 5th → promotable
        cands = await cg.detect_promotable_cells(db)
        assert [c["id"] for c in cands] == ["email:send:standard"]
        assert cands[0]["posterior"] > 0.70

    @pytest.mark.asyncio
    async def test_detect_promotable_excludes_granted(self, db):
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        for _ in range(5):
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)
        assert await cg.detect_promotable_cells(db) == []  # already GRANTED

    @pytest.mark.asyncio
    async def test_heavier_harm_craters_reearn_deeper(self, db):
        # Same evidence, heavier past harm ⇒ lower re-earn posterior ⇒ harder
        # to climb back to the 0.70 promotion bar.
        assert cg.cell_posterior(5, 1, 2.0) < cg.cell_posterior(5, 1, 1.0)

    @pytest.mark.asyncio
    async def test_list_granted_returns_only_granted(self, db):
        # standard → GRANTED; bulk left at ASK.
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_EMAIL
        )
        await cg.apply_event(
            db,
            origin_class="first_party",
            domain="email",
            verb="send",
            risk_class="bulk",
            event=CellEvent.CLASSIFY,
            updated_at=_TS,
        )
        granted = await cg.list_granted(db)
        assert [g["id"] for g in granted] == ["email:send:standard"]

    @pytest.mark.asyncio
    async def test_decay_lapses_only_stale_grants(self, db):
        from datetime import UTC, datetime, timedelta

        now_dt = datetime(2026, 6, 21, tzinfo=UTC)
        now = now_dt.isoformat()
        # fresh grant (used today) — must NOT decay
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=now, **_EMAIL
        )
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=now, **_EMAIL
        )
        await cg.touch_used(db, used_at=now, **_EMAIL)
        # stale grant (granted 100d ago, never used) — must decay
        old = (now_dt - timedelta(days=100)).isoformat()
        for ev in (CellEvent.CLASSIFY, CellEvent.APPROVE):
            await cg.apply_event(
                db,
                origin_class="first_party",
                domain="email",
                verb="send",
                risk_class="bulk",
                event=ev,
                updated_at=old,
            )

        decayed = await cg.decay_stale_cells(db, now=now, half_life_days=90)

        assert decayed == ["email:send:bulk"]
        assert (await cg.get_cell(db, **_EMAIL))["state"] == CellState.GRANTED.value
        bulk = await cg.get_cell(db, "email", "send", "bulk")
        assert bulk["state"] == CellState.NOT_DETERMINED.value
        assert bulk["granted_at"] is None
        assert bulk["last_decayed_at"] == now


# --------------------------------------------------------------------------- #
# Promotable-domain allowlist — which capabilities may EVER hold standing       #
# autonomy.  Promotion is the one transition that converts per-action approval  #
# into a standing grant, so the set of domains allowed to make it is closed.    #
# --------------------------------------------------------------------------- #
class TestPromotableDomains:
    @pytest.mark.asyncio
    async def test_allowlist_contents_are_pinned(self):
        # Adding a domain here hands it standing autonomy — that must be a
        # deliberate act with a test to change, never a quiet import.
        assert sorted(cg.PROMOTABLE_DOMAINS) == ["email"]
        assert cg.is_promotable_cell("email", "standard") is True
        assert cg.is_promotable_cell("widget", "standard") is False
        # FINANCIAL is hardline in an ALLOWLISTED domain too — RiskClass calls
        # it "never trust-unlockable", and this is what makes that true.
        assert cg.is_promotable_cell("email", "financial") is False

    @pytest.mark.asyncio
    async def test_detect_promotable_excludes_non_promotable_domain(self, db):
        # Evidence far past BOTH bars — the only thing holding it back is the
        # domain.  Without the allowlist this cell is a promotion proposal.
        for ev in (CellEvent.CLASSIFY,):
            await cg.apply_event(
                db, origin_class="first_party", event=ev, updated_at=_TS, **_WIDGET
            )
        for _ in range(cg.MIN_PROMOTE_N * 2):
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_WIDGET)

        row = await cg.get_cell(db, **_WIDGET)
        assert row["state"] == CellState.ASK.value  # it IS an ASK cell with evidence
        assert row["successes"] >= cg.MIN_PROMOTE_N
        assert cg.cell_posterior(row["successes"], row["corrections"], 0.0) > cg.PROMOTE_THRESHOLD

        assert await cg.detect_promotable_cells(db) == []

    @pytest.mark.asyncio
    async def test_detect_promotable_still_returns_allowlisted_domain(self, db):
        # The behaviour-preserving control: the allowlist must not blind the
        # scan to the domain it exists to keep working.
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_EMAIL
        )
        for _ in range(cg.MIN_PROMOTE_N):
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_EMAIL)

        assert [c["id"] for c in await cg.detect_promotable_cells(db)] == ["email:send:standard"]

    @pytest.mark.asyncio
    async def test_approve_refused_for_non_promotable_domain(self, db):
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_WIDGET
        )
        with pytest.raises(InvalidTransition, match="not promotable"):
            await cg.apply_event(
                db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_WIDGET
            )
        # Refused, and the refusal did not mutate the cell.
        assert (await cg.get_cell(db, **_WIDGET))["state"] == CellState.ASK.value

    @pytest.mark.asyncio
    async def test_refused_approve_does_not_seed_a_cell(self, db):
        # No CLASSIFY first: a refusal must not create the row on its way out,
        # or a refused promotion would leave evidence that it was considered.
        with pytest.raises(InvalidTransition):
            await cg.apply_event(
                db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **_WIDGET
            )
        assert await cg.get_cell(db, **_WIDGET) is None

    @pytest.mark.asyncio
    async def test_non_promotable_domain_still_classifies_and_gates(self, db):
        # Non-promotable is NOT disabled: the cell still exists, still records
        # evidence, and still sits at ASK — i.e. every action keeps its approval.
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **_WIDGET
        )
        await cg.record_success(db, origin_class="first_party", updated_at=_TS, **_WIDGET)
        row = await cg.get_cell(db, **_WIDGET)
        assert row["state"] == CellState.ASK.value
        assert row["successes"] == 1

    @pytest.mark.asyncio
    async def test_allowlisted_domain_still_promotes(self, db):
        # The other half of the control: APPROVE is untouched where it is allowed.
        for ev in (CellEvent.CLASSIFY, CellEvent.APPROVE):
            await cg.apply_event(
                db, origin_class="first_party", event=ev, updated_at=_TS, **_EMAIL
            )
        assert (await cg.get_cell(db, **_EMAIL))["state"] == CellState.GRANTED.value

    def test_migrations_touching_granted_cells_are_a_reviewed_set(self):
        """A migration writes cell state in raw SQL, BELOW apply_event — so none
        of the three enforcement layers can reach it.

        Pinned as a closed SET of filenames rather than by parsing the SQL:
        a domain literal can be spelled a dozen ways, but a new migration file
        cannot hide.  Adding one that touches granted capability cells has to be
        looked at by a human, who then decides whether its domain belongs in
        PROMOTABLE_DOMAINS — instead of inheriting standing autonomy in silence.
        """
        from pathlib import Path

        import genesis

        mig_dir = Path(genesis.__file__).parent / "db" / "migrations"
        found = set()
        for path in sorted(mig_dir.glob("*.py")):
            src = path.read_text()
            # Case-INSENSITIVE: a migration writing the state symbolically
            # (`CellState.GRANTED.value` in an f-string) never contains the
            # lowercase literal, and would otherwise pass unseen.
            if "capability_grants" in src and "granted" in src.lower():
                found.add(path.name)

        reviewed = {
            # Creates the table; 'granted' appears only in the state CHECK list.
            "0030_capability_grants.py",
            # Prose only — its module docstring describes the GRANTED path.
            "0031_pending_email_sends.py",
            # Seeds email:send:standard GRANTED — email is in PROMOTABLE_DOMAINS.
            "0032_seed_email_standard_grant.py",
            # Deletes 0032's pristine seed (all-ASK); its down() restores it.
            "0033_autonomy_earn_lose.py",
            # Prose only — a column comment mentions a GRANTED cell.
            "0044_capability_shadow_events.py",
        }
        assert found == reviewed, (
            "a migration now touches GRANTED capability cells outside the reviewed "
            f"set: {found ^ reviewed}. Migrations write cell state directly and "
            "bypass apply_event's PROMOTABLE_DOMAINS guard — confirm the domain it "
            "grants is allowlisted, then add the file here."
        )

    @pytest.mark.asyncio
    async def test_financial_cell_is_never_promotable_even_in_email(self, db):
        """FINANCIAL is hardline by RiskClass's own docstring ("never
        trust-unlockable"). Until now that was true only because email_gate
        holds financial sends BEFORE the first CLASSIFY — one caller's
        ordering. A financial cell reached by any other path was promotable.
        """
        fin = {"domain": "email", "verb": "send", "risk_class": "financial"}
        await cg.apply_event(
            db, origin_class="first_party", event=CellEvent.CLASSIFY, updated_at=_TS, **fin
        )
        for _ in range(cg.MIN_PROMOTE_N * 2):
            await cg.record_success(db, origin_class="first_party", updated_at=_TS, **fin)

        row = await cg.get_cell(db, **fin)
        assert row["successes"] >= cg.MIN_PROMOTE_N  # evidence is NOT the reason
        assert await cg.detect_promotable_cells(db) == []
        with pytest.raises(InvalidTransition, match="not promotable"):
            await cg.apply_event(
                db, origin_class="first_party", event=CellEvent.APPROVE, updated_at=_TS, **fin
            )
        assert (await cg.get_cell(db, **fin))["state"] == CellState.ASK.value

    @pytest.mark.asyncio
    async def test_raw_string_event_cannot_bypass_the_promotion_guard(self, db):
        """CellEvent is a StrEnum: "approve" is NOT identical to
        CellEvent.APPROVE but IS equal to it, and _TRANSITIONS is a dict keyed
        on equality. An identity check here was skipped while transition() still
        returned GRANTED — committing the promotion, then failing on
        event.value AFTER the write. The guard must key on the VALUE.
        """
        await cg.apply_event(
            db,
            origin_class="first_party",
            event=CellEvent.CLASSIFY.value,  # raw string on the way in, too
            updated_at=_TS,
            **_WIDGET,
        )
        with pytest.raises(InvalidTransition, match="not promotable"):
            await cg.apply_event(
                db,
                origin_class="first_party",
                event=CellEvent.APPROVE.value,  # the bypass vector
                updated_at=_TS,
                **_WIDGET,
            )
        assert (await cg.get_cell(db, **_WIDGET))["state"] == CellState.ASK.value

    @pytest.mark.asyncio
    async def test_unknown_event_raises_invalid_transition(self, db):
        """Normalizing the event must not change the error type this function
        documents: an unrecognised event was already an InvalidTransition."""
        with pytest.raises(InvalidTransition, match="unknown cell event"):
            await cg.apply_event(
                db, origin_class="first_party", event="not_an_event",
                updated_at=_TS, **_WIDGET,
            )
