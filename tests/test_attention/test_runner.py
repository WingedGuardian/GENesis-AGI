"""Offline shadow runner: config resolution + run_shadow over a synthetic snapshot."""
import json
import sqlite3

import pytest

from genesis.attention import runner
from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.runner import ShadowReport, load_runner_config, run_shadow


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


def test_load_runner_config_from_explicit_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({**default_config_dict(), "version": "custom-1"}))
    assert load_runner_config(str(p)).version == "custom-1"


def test_load_runner_config_falls_back_to_builtin(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "DEFAULT_CONFIG_PATH", str(tmp_path / "absent.json"))
    assert load_runner_config(None).version == "0.1.0-default"


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
