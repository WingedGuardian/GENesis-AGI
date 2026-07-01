"""Offline shadow runner: config resolution + run_shadow over a synthetic snapshot."""
import json
import sqlite3
from pathlib import Path

import pytest

from genesis.attention import runner
from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.runner import (
    ShadowReport,
    _graduates_to_l15,
    _snapshot_id_from_path,
    load_runner_config,
    run_shadow,
)
from genesis.attention.types import Activation, AmbientUtterance, AttentionEvent, WindowRef


def _meta(rms=0.2, ntok=20) -> str:
    return json.dumps({"asr_feats": {"ys_log_probs": [-0.1] * ntok, "n_tokens": ntok},
                       "audio": {"rms": rms}})


def _make_ambient(path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ambient_transcripts (id INTEGER PRIMARY KEY, ts TEXT, text TEXT, "
        "duration_s REAL, speaker_label TEXT, provenance TEXT, source TEXT, meta TEXT, "
        "is_user INTEGER, speaker_name TEXT)"
    )
    conn.executemany(
        "INSERT INTO ambient_transcripts (id, ts, text, duration_s, speaker_label, meta, is_user) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()


def test_snapshot_id_from_path_strips_ambient_prefix():
    # --snapshot must yield the BARE id so the dashboard reveal can reconstruct the file.
    assert _snapshot_id_from_path(Path("/x/ambient_20260701T013412Z.db")) == "20260701T013412Z"
    assert _snapshot_id_from_path(Path("/x/20260701T013412Z.db")) == "20260701T013412Z"
    assert _snapshot_id_from_path(Path("/x/custom.db")) == "custom"


def test_load_runner_config_from_explicit_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({**default_config_dict(), "version": "custom-1"}))
    assert load_runner_config(str(p)).version == "custom-1"


def test_load_runner_config_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DEFAULT_CONFIG_PATH", str(tmp_path / "absent.json"))
    assert load_runner_config(None).version == "0.2.0-taxonomy"


@pytest.mark.asyncio
async def test_run_shadow_over_snapshot(tmp_path):
    snap = tmp_path / "ambient.db"
    _make_ambient(snap, [
        (1, "2026-06-30T12:00:00+00:00", "what do you think?", 5.0, "w1:1/2", _meta(), 1),
        (2, "2026-06-30T12:05:00+00:00", "hello", 5.0, "w1:1/1", _meta(), None),
    ])
    cfg = AttentionConfig.from_dict(default_config_dict())
    report = await run_shadow(snap, cfg, snapshot_id="t", sample_n=5)
    assert isinstance(report, ShadowReport)
    assert report.total_rows == 2
    assert report.events >= 1            # the question+multi_speaker row fires
    assert report.persisted == 0         # no consumer -> nothing persisted
    assert 0.0 <= report.fire_rate <= 1.0


# ── PR3b: L1.5 seam (activation-gated graduation + verdict attach) ──

def _ev(activation, score) -> AttentionEvent:
    return AttentionEvent(
        activation=activation, score=score, triggers_fired=(), suppressors=(),
        session_id="s1", window_ref=WindowRef("s1", (1,), 0.0, 1.0),
        ts=1.0, mode_state="unknown", clarity=1.0,
    )


def test_graduates_to_l15_gates_on_activation():
    th = AttentionConfig.from_dict(default_config_dict()).thresholds   # l15_graduation = 0.4
    assert _graduates_to_l15(_ev(Activation.HARD, 0.0), th) is True         # bare-HARD (score 0) graduates
    assert _graduates_to_l15(_ev(Activation.SOFT, 0.7), th) is True         # SOFT above the floor
    assert _graduates_to_l15(_ev(Activation.SUPPRESSED, 0.9), th) is False  # a veto -> never spend a call


def test_graduates_to_l15_soft_cost_floor():
    # A raised floor lets L1.5 skip weak SOFT fires (a cost knob); HARD is unaffected.
    th = AttentionConfig.from_dict({**default_config_dict(), "thresholds": {"l15_graduation": 0.8}}).thresholds
    assert _graduates_to_l15(_ev(Activation.SOFT, 0.6), th) is False        # below the raised floor
    assert _graduates_to_l15(_ev(Activation.SOFT, 0.9), th) is True
    assert _graduates_to_l15(_ev(Activation.HARD, 0.0), th) is True         # floor never gates HARD


class _FakeSampler:
    def __init__(self, verdict):
        self.verdict = verdict
        self.windows: list = []

    async def sample(self, window, config):
        self.windows.append(window)
        return self.verdict


@pytest.mark.asyncio
async def test_run_shadow_l15_attaches_verdict(tmp_path):
    snap = tmp_path / "ambient.db"
    _make_ambient(snap, [
        (1, "2026-06-30T12:00:00+00:00", "what do you think?", 5.0, "w1:1/2", _meta(), 1),
    ])
    cfg = AttentionConfig.from_dict(default_config_dict())
    fake = _FakeSampler({"real": 0.9, "perk": 0.4})
    report = await run_shadow(snap, cfg, snapshot_id="t", sampler=fake)
    assert report.events >= 1
    assert fake.windows                                                   # sampler was invoked
    assert all(isinstance(u, AmbientUtterance) for u in fake.windows[0])  # window = live utterances
    assert report.samples[0]["l15"] == {"real": 0.9, "perk": 0.4}         # verdict rode onto the event


@pytest.mark.asyncio
async def test_run_shadow_without_sampler_leaves_verdict_none(tmp_path):
    snap = tmp_path / "ambient.db"
    _make_ambient(snap, [
        (1, "2026-06-30T12:00:00+00:00", "what do you think?", 5.0, "w1:1/2", _meta(), 1),
    ])
    cfg = AttentionConfig.from_dict(default_config_dict())
    report = await run_shadow(snap, cfg, snapshot_id="t")                 # default: no sampler
    assert report.events >= 1
    assert report.samples[0]["l15"] is None
