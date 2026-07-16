"""Tests for ego calibration self-correction injection (genesis ego context)."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import ego_calibration as cal_crud

# These imports are the SUT — they don't exist until implemented (TDD red first).
from genesis.feedback.calibration import format_calibration_section


def _snapshot(*, low_confidence=False):
    return {
        "domain": "ego",
        "ece": 0.116,
        "mce": 0.45,
        "sample_count": 47,
        "bucket_count": 5,
        "low_confidence": low_confidence,
        "curve": [
            {"confidence_bucket": "0.6-0.7", "predicted_confidence": 0.65,
             "actual_success_rate": 1.0, "sample_count": 7},
            {"confidence_bucket": "0.8-0.9", "predicted_confidence": 0.85,
             "actual_success_rate": 0.82, "sample_count": 22},
        ],
    }


# --------------------------------------------------------------------------- #
# format_calibration_section (pure)
# --------------------------------------------------------------------------- #
class TestFormatter:
    def test_none_is_empty(self):
        assert format_calibration_section(None) == ""

    def test_low_confidence_is_empty(self):
        assert format_calibration_section(_snapshot(low_confidence=True)) == ""

    def test_deep_renders_curve(self):
        out = format_calibration_section(_snapshot(), depth="deep")
        assert "Confidence Calibration" in out
        assert "~65%" in out and "~100%" in out and "n=7" in out
        assert "~85%" in out and "~82%" in out and "n=22" in out
        assert "0.12" in out or "0.116" in out  # ECE present
        # anti-overfitting framing present
        assert "directional" in out.lower() or "weigh" in out.lower()

    def test_light_is_summary_not_curve(self):
        out = format_calibration_section(_snapshot(), depth="light")
        assert "ECE" in out
        # light must NOT include the per-bucket lines
        assert "When you report" not in out


# --------------------------------------------------------------------------- #
# _confidence_calibration_section (genesis ego context)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db(tmp_path):
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "inj.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


def _builder(db):
    from genesis.ego.genesis_context import GenesisEgoContextBuilder

    return GenesisEgoContextBuilder(db=db, health_data=None, capabilities={})


def _set_flag(monkeypatch, enabled: bool):
    from genesis.ego.types import EgoConfig

    monkeypatch.setattr(
        "genesis.ego.config.load_ego_config",
        lambda *a, **k: EgoConfig(calibration_injection_enabled=enabled),
    )


class TestSection:
    @pytest.mark.asyncio
    async def test_renders_when_flag_on_and_snapshot(self, db, monkeypatch):
        _set_flag(monkeypatch, True)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False, curve=_snapshot()["curve"],
        )
        out = await _builder(db)._confidence_calibration_section()
        assert "Confidence Calibration" in out
        assert "n=22" in out

    @pytest.mark.asyncio
    async def test_flag_off_is_empty(self, db, monkeypatch):
        _set_flag(monkeypatch, False)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False, curve=_snapshot()["curve"],
        )
        assert await _builder(db)._confidence_calibration_section() == ""

    @pytest.mark.asyncio
    async def test_no_snapshot_is_empty(self, db, monkeypatch):
        _set_flag(monkeypatch, True)
        assert await _builder(db)._confidence_calibration_section() == ""

    @pytest.mark.asyncio
    async def test_low_confidence_snapshot_is_empty(self, db, monkeypatch):
        _set_flag(monkeypatch, True)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.30, mce=0.5, sample_count=4,
            bucket_count=1, low_confidence=True, curve=_snapshot()["curve"],
        )
        assert await _builder(db)._confidence_calibration_section() == ""

    @pytest.mark.asyncio
    async def test_missing_table_is_empty_not_raise(self, tmp_path, monkeypatch):
        # No create_all_tables -> ego_calibration_snapshots absent -> OperationalError
        _set_flag(monkeypatch, True)
        path = str(tmp_path / "bare.db")
        async with aiosqlite.connect(path) as conn:
            out = await _builder(conn)._confidence_calibration_section()
            assert out == ""  # graceful pre-deploy state


    @pytest.mark.asyncio
    async def test_section_appears_in_full_build(self, db, monkeypatch):
        """WIRING: the section must show up in the assembled context build(),
        not just when called directly (built != wired)."""
        _set_flag(monkeypatch, True)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False, curve=_snapshot()["curve"],
        )
        full = await _builder(db).build()
        assert "## Confidence Calibration" in full
        # rendered before the output contract (highest salience for the confidence field)
        # Heading-anchored: plain substrings collide with body text that
        # *mentions* other sections (e.g. the own-goals affordance line says
        # "see Output Contract").
        assert full.index("## Confidence Calibration") < full.index("## Output Contract")

    @pytest.mark.asyncio
    async def test_null_flag_stays_on(self, db, monkeypatch):
        # YAML null / unexpected value must NOT silently disable (default-ON;
        # only an explicit False disables).
        from genesis.ego.types import EgoConfig

        cfg = EgoConfig()
        cfg.calibration_injection_enabled = None  # simulate YAML null
        monkeypatch.setattr("genesis.ego.config.load_ego_config", lambda *a, **k: cfg)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False, curve=_snapshot()["curve"],
        )
        out = await _builder(db)._confidence_calibration_section()
        assert "Confidence Calibration" in out  # stayed ON

    @pytest.mark.asyncio
    async def test_section_absent_from_build_when_flag_off(self, db, monkeypatch):
        _set_flag(monkeypatch, False)
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False, curve=_snapshot()["curve"],
        )
        full = await _builder(db).build()
        assert "Confidence Calibration" not in full


class TestConfigFlag:
    def test_default_is_on(self):
        from genesis.ego.types import EgoConfig

        assert EgoConfig().calibration_injection_enabled is True
