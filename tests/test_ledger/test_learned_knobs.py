"""WS-2 B5 knob substrate — registry, bounds, ledgered writes, applier, seam.

All fixtures synthetic (tmp_path files + in-memory DB). The trigger scan is
deliberately absent (substrate-only v1) — these tests pin the substrate
contracts the future trigger will call into.
"""

from __future__ import annotations

import aiosqlite
import pytest
import yaml

from genesis.db.schema import create_all_tables
from genesis.ledger import learned_knobs as lk


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.execute(
        "INSERT INTO signal_weights (signal_name, source_mcp, current_weight,"
        " initial_weight, min_weight, max_weight, feeds_depths)"
        " VALUES ('test_signal', 'test', 0.5, 0.5, 0.0, 1.0, '[\"Deep\"]')"
    )
    await conn.execute(
        "INSERT OR REPLACE INTO depth_thresholds (depth_name, threshold,"
        " floor_seconds, ceiling_count, ceiling_window_seconds)"
        " VALUES ('Deep', 0.45, 60, 10, 3600)"
    )
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def knob_paths(tmp_path, monkeypatch):
    """Isolate base file + overlay dir under tmp_path."""
    base_dir = tmp_path / "config"
    base_dir.mkdir()
    base = base_dir / "learned_knobs.yaml"
    base.write_text("knobs: {}\n")
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(lk, "_base_path", lambda: base)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return {"base": base, "overlay": user_dir / "learned_knobs.local.yaml"}


# ---------------------------------------------------------------------------
# Registry + loader
# ---------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("awareness.signal_weights.test_signal", ("signal_weights", "test_signal")),
            ("awareness.depth_thresholds.Deep", ("depth_thresholds", "Deep")),
            ("memory.activation_blend.base", ("activation_blend", "base")),
            ("memory.activation_blend.connectivity", ("activation_blend", "connectivity")),
        ],
    )
    def test_valid_keys(self, key, expected):
        assert lk.parse_knob_key(key) == expected

    @pytest.mark.parametrize(
        "key",
        [
            "awareness.depth_thresholds.deep",  # case-sensitive
            "awareness.depth_thresholds.Bogus",
            "memory.activation_blend.recency",  # not a blend component
            "routing.some_knob",  # not a group
            "awareness.signal_weights",  # missing name
            "",
        ],
    )
    def test_invalid_keys_rejected(self, key):
        assert lk.parse_knob_key(key) is None


class TestLoader:
    def test_empty_registry(self, knob_paths):
        assert lk.load_knobs() == {}

    def test_overlay_merges_over_base(self, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {"knobs": {"awareness.depth_thresholds.Deep": {"baseline": 0.45, "current": 0.44}}}
            )
        )
        knobs = lk.load_knobs()
        assert knobs["awareness.depth_thresholds.Deep"]["current"] == 0.44

    def test_unregistered_and_malformed_ignored(self, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {
                    "knobs": {
                        "routing.bogus": {"baseline": 1, "current": 2},
                        "memory.activation_blend.base": "not-a-dict",
                    }
                }
            )
        )
        assert lk.load_knobs() == {}


# ---------------------------------------------------------------------------
# Bounds validator
# ---------------------------------------------------------------------------


class TestValidateChange:
    def test_ok_within_bounds(self):
        assert lk.validate_change(baseline=0.5, current=0.5, new_value=0.52) == []

    def test_step_violation(self):
        errors = lk.validate_change(baseline=0.5, current=0.5, new_value=0.54)
        assert any("step" in e for e in errors)

    def test_cumulative_violation_across_steps(self):
        # each step small, but drift from baseline beyond 20%
        errors = lk.validate_change(baseline=0.5, current=0.6, new_value=0.62)
        assert any("cumulative" in e for e in errors)

    def test_nonpositive_baseline_rejected(self):
        assert lk.validate_change(baseline=0.0, current=0.1, new_value=0.1)


# ---------------------------------------------------------------------------
# activation_blend + seam
# ---------------------------------------------------------------------------


class TestActivationBlend:
    def test_defaults_without_file(self, knob_paths):
        assert lk.activation_blend() == lk.BLEND_DEFAULTS

    def test_override_within_bounds_applied(self, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {"knobs": {"memory.activation_blend.base": {"baseline": 0.6, "current": 0.58}}}
            )
        )
        assert lk.activation_blend()["base"] == 0.58

    def test_out_of_bounds_override_ignored(self, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {"knobs": {"memory.activation_blend.base": {"baseline": 0.6, "current": 0.9}}}
            )
        )
        assert lk.activation_blend()["base"] == 0.6

    def test_activation_module_seam_reload(self, knob_paths):
        from genesis.memory import activation

        try:
            knob_paths["overlay"].write_text(
                yaml.safe_dump(
                    {"knobs": {"memory.activation_blend.base": {"baseline": 0.6, "current": 0.62}}}
                )
            )
            activation.reload_blend()
            assert activation._BLEND_BASE == 0.62
            # scoring consumes the reloaded value (never-retrieved memory:
            # blend base is the floor coefficient)
            score = activation.compute_activation(1.0, "2026-01-01T00:00:00+00:00", 0, 0)
            assert score.final_score > 0
        finally:
            knob_paths["overlay"].unlink(missing_ok=True)
            activation.reload_blend()
            assert activation._BLEND_BASE == 0.6

    def test_seam_falls_back_on_loader_failure(self, monkeypatch):
        from genesis.memory import activation

        def _boom():
            raise RuntimeError("loader broken")

        monkeypatch.setattr("genesis.ledger.learned_knobs.activation_blend", _boom)
        try:
            activation.reload_blend()
            assert activation._BLEND_BASE == 0.6
            assert activation._BLEND_ACCESS == 0.25
            assert activation._BLEND_CONNECTIVITY == 0.15
        finally:
            monkeypatch.undo()
            activation.reload_blend()


# ---------------------------------------------------------------------------
# apply_knob_change (the ledgered write path)
# ---------------------------------------------------------------------------


class TestApplyKnobChange:
    async def test_signal_knob_end_to_end(self, db, knob_paths):
        mod_id = await lk.apply_knob_change(
            db, "awareness.signal_weights.test_signal", 0.52, reason="unit"
        )
        assert mod_id is not None
        # overlay written to the USER dir (never the repo tree), entry pinned
        overlay = yaml.safe_load(knob_paths["overlay"].read_text())
        entry = overlay["knobs"]["awareness.signal_weights.test_signal"]
        assert entry == {"baseline": 0.5, "current": 0.52}
        # DB synced through the clamped CRUD
        cursor = await db.execute(
            "SELECT current_weight FROM signal_weights WHERE signal_name='test_signal'"
        )
        assert (await cursor.fetchone())["current_weight"] == pytest.approx(0.52)
        # cognitive-ledger row recorded with the ws2_effector actor
        cursor = await db.execute(
            "SELECT actor FROM cognitive_file_modifications WHERE id = ?", (mod_id,)
        )
        assert (await cursor.fetchone())["actor"] == "ws2_effector"

    async def test_depth_knob_baseline_captured_from_live(self, db, knob_paths):
        await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        overlay = yaml.safe_load(knob_paths["overlay"].read_text())
        entry = overlay["knobs"]["awareness.depth_thresholds.Deep"]
        assert entry["baseline"] == 0.45  # live value at first apply, pinned
        cursor = await db.execute("SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'")
        assert (await cursor.fetchone())["threshold"] == pytest.approx(0.44)

    async def test_second_step_uses_pinned_baseline(self, db, knob_paths):
        await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        # next step measured against pinned baseline 0.45, current 0.44
        with pytest.raises(ValueError, match="step"):
            await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.48)

    async def test_unregistered_key_raises(self, db, knob_paths):
        with pytest.raises(ValueError, match="closed registry"):
            await lk.apply_knob_change(db, "routing.bogus", 0.5)

    async def test_unknown_signal_raises(self, db, knob_paths):
        with pytest.raises(ValueError, match="unknown signal"):
            await lk.apply_knob_change(db, "awareness.signal_weights.nope", 0.5)

    async def test_step_bound_enforced(self, db, knob_paths):
        with pytest.raises(ValueError, match="step"):
            await lk.apply_knob_change(db, "awareness.signal_weights.test_signal", 0.6)

    async def test_blend_knob_pokes_activation_reload(self, db, knob_paths):
        from genesis.memory import activation

        try:
            await lk.apply_knob_change(db, "memory.activation_blend.base", 0.61)
            assert activation._BLEND_BASE == 0.61
        finally:
            knob_paths["overlay"].unlink(missing_ok=True)
            activation.reload_blend()

    async def test_rollback_restores_prior_file(self, db, knob_paths):
        from genesis.learning.cognitive_ledger import rollback

        first = await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        assert first is not None
        second = await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.43)
        assert second is not None
        result = await rollback(db, second)
        assert result["ok"] is True
        overlay = yaml.safe_load(knob_paths["overlay"].read_text())
        assert overlay["knobs"]["awareness.depth_thresholds.Deep"]["current"] == 0.44
        # re-sync converges DB back to the rolled-back file
        applied = await lk.apply_learned_knobs_to_db(db)
        assert applied == 1
        cursor = await db.execute("SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'")
        assert (await cursor.fetchone())["threshold"] == pytest.approx(0.44)


# ---------------------------------------------------------------------------
# Startup applier
# ---------------------------------------------------------------------------


class TestStartupApplier:
    async def test_empty_file_applies_nothing(self, db, knob_paths):
        assert await lk.apply_learned_knobs_to_db(db) == 0

    async def test_db_backed_entries_applied(self, db, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {
                    "knobs": {
                        "awareness.signal_weights.test_signal": {"baseline": 0.5, "current": 0.53},
                        "awareness.depth_thresholds.Deep": {"baseline": 0.45, "current": 0.46},
                        "memory.activation_blend.base": {"baseline": 0.6, "current": 0.62},
                    }
                }
            )
        )
        applied = await lk.apply_learned_knobs_to_db(db)
        assert applied == 2  # blend entry needs no DB sync
        cursor = await db.execute(
            "SELECT current_weight FROM signal_weights WHERE signal_name='test_signal'"
        )
        assert (await cursor.fetchone())["current_weight"] == pytest.approx(0.53)
        cursor = await db.execute("SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'")
        assert (await cursor.fetchone())["threshold"] == pytest.approx(0.46)

    async def test_out_of_bounds_entry_skipped(self, db, knob_paths):
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {
                    "knobs": {
                        "awareness.signal_weights.test_signal": {"baseline": 0.5, "current": 0.9},
                    }
                }
            )
        )
        assert await lk.apply_learned_knobs_to_db(db) == 0
        cursor = await db.execute(
            "SELECT current_weight FROM signal_weights WHERE signal_name='test_signal'"
        )
        assert (await cursor.fetchone())["current_weight"] == pytest.approx(0.5)  # untouched

    async def test_sql_clamp_still_backstops(self, db, knob_paths):
        # A file value inside ±20% of baseline but outside the row's min/max
        # is clamped by the CRUD (defense in depth). Tighten the row's max:
        await db.execute(
            "UPDATE signal_weights SET max_weight = 0.51 WHERE signal_name='test_signal'"
        )
        await db.commit()
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {
                    "knobs": {
                        "awareness.signal_weights.test_signal": {"baseline": 0.5, "current": 0.55},
                    }
                }
            )
        )
        await lk.apply_learned_knobs_to_db(db)
        cursor = await db.execute(
            "SELECT current_weight FROM signal_weights WHERE signal_name='test_signal'"
        )
        assert (await cursor.fetchone())["current_weight"] == pytest.approx(0.51)


# ---------------------------------------------------------------------------
# Rollback resync hook (F2/F3 — ledger rollback re-converges consumers)
# ---------------------------------------------------------------------------


class TestRollbackResync:
    async def test_first_write_rollback_restores_db_from_metadata(self, db, knob_paths):
        """Rolling back the FIRST knob write deletes the overlay (no
        pre-image) — the DB row must still re-converge, via the ledger row's
        recorded metadata.previous, not stay drifted forever."""
        from genesis.learning.cognitive_ledger import rollback

        mod_id = await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        result = await rollback(db, mod_id)
        assert result["ok"] is True
        assert not knob_paths["overlay"].exists()  # first write → file removed
        assert "restored to previous" in result["knob_resync"]
        cursor = await db.execute(
            "SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'"
        )
        assert (await cursor.fetchone())["threshold"] == pytest.approx(0.45)

    async def test_later_write_rollback_resyncs_via_applier(self, db, knob_paths):
        from genesis.learning.cognitive_ledger import rollback

        await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        second = await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.43)
        result = await rollback(db, second)
        assert result["ok"] is True
        assert result["knob_resync"].startswith("resynced (1 from file")
        cursor = await db.execute(
            "SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'"
        )
        assert (await cursor.fetchone())["threshold"] == pytest.approx(0.44)

    async def test_baseline_not_repinned_after_first_write_rollback(self, db, knob_paths):
        """The re-pin drift hole: after first-write rollback the next apply
        must see the ORIGINAL value as baseline (0.45), not a drifted one."""
        from genesis.learning.cognitive_ledger import rollback

        mod_id = await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.44)
        await rollback(db, mod_id)
        # DB is back at 0.45; a fresh apply pins baseline 0.45 again
        await lk.apply_knob_change(db, "awareness.depth_thresholds.Deep", 0.46)
        overlay = yaml.safe_load(knob_paths["overlay"].read_text())
        assert overlay["knobs"]["awareness.depth_thresholds.Deep"]["baseline"] == 0.45

    async def test_non_knob_rollback_untouched(self, db, tmp_path):
        """Rollback of a non-ws2_effector row must not carry knob_resync."""
        from genesis.learning.cognitive_ledger import record_file_modification, rollback

        target = tmp_path / "other.md"
        mod_id = await record_file_modification(
            db, actor="skill_evolution", path=target, new_content="hello"
        )
        result = await rollback(db, mod_id)
        assert result["ok"] is True
        assert "knob_resync" not in result


# ---------------------------------------------------------------------------
# Depth absolute clamp (F4)
# ---------------------------------------------------------------------------


class TestDepthClamp:
    async def test_hand_edited_out_of_domain_threshold_clamped(self, db, knob_paths):
        """A self-attested baseline can pass the relative bound with an
        absurd absolute value — the sync path clamps to (0, 1]."""
        knob_paths["overlay"].write_text(
            yaml.safe_dump(
                {"knobs": {"awareness.depth_thresholds.Deep": {"baseline": 10.0, "current": 9.0}}}
            )
        )
        await lk.apply_learned_knobs_to_db(db)
        cursor = await db.execute(
            "SELECT threshold FROM depth_thresholds WHERE depth_name='Deep'"
        )
        assert (await cursor.fetchone())["threshold"] == pytest.approx(1.0)
