"""Tests for the LongMemEval OpenRouter client + secret loading (WS-1 A4)."""

from __future__ import annotations

import pytest

from genesis.eval.longmemeval import client as client_mod
from genesis.eval.longmemeval.client import (
    OPENROUTER_KEY_ENV,
    load_secrets,
    openrouter_api_key,
)


def test_openrouter_api_key_from_env(monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "env-key-123")
    assert openrouter_api_key() == "env-key-123"


def test_openrouter_api_key_from_secrets(monkeypatch):
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)

    def fake_load():
        monkeypatch.setenv(OPENROUTER_KEY_ENV, "secrets-key-456")

    monkeypatch.setattr(client_mod, "load_secrets", fake_load)
    assert openrouter_api_key() == "secrets-key-456"


def test_openrouter_api_key_missing_raises(monkeypatch):
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    # load_secrets is a no-op → key stays absent → must raise loudly
    monkeypatch.setattr(client_mod, "load_secrets", lambda: None)
    with pytest.raises(RuntimeError, match=OPENROUTER_KEY_ENV):
        openrouter_api_key()


def test_load_secrets_reads_keyvalue_and_skips_comments(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        '# a comment\nAPI_KEY_OPENROUTER=abc123\n\nQUOTED_KEY="q-value"\nNOT_A_PAIR_LINE\n',
    )
    monkeypatch.delenv("API_KEY_OPENROUTER", raising=False)
    monkeypatch.delenv("QUOTED_KEY", raising=False)
    monkeypatch.setattr(client_mod, "_candidate_secret_paths", lambda: [secrets])
    load_secrets(force=True)
    import os

    assert os.environ["API_KEY_OPENROUTER"] == "abc123"
    assert os.environ["QUOTED_KEY"] == "q-value"


def test_load_secrets_does_not_overwrite_existing(monkeypatch, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("API_KEY_OPENROUTER=from-file\n")
    monkeypatch.setenv("API_KEY_OPENROUTER", "already-set")
    monkeypatch.setattr(client_mod, "_candidate_secret_paths", lambda: [secrets])
    load_secrets(force=True)
    import os

    assert os.environ["API_KEY_OPENROUTER"] == "already-set"


def test_candidate_secret_paths_includes_maintree_fallback():
    from pathlib import Path

    paths = client_mod._candidate_secret_paths()
    assert Path.home() / "genesis" / "secrets.env" in paths


def test_require_results_db_fails_fast_on_missing_db(tmp_path):
    """A missing default results DB must fail BEFORE the paid run — get_db
    would silently create a fresh DB lacking eval_runs (migration-only) and
    the benchmark would crash at persistence after burning its API spend."""
    import pytest

    from genesis.eval.longmemeval.cli import require_results_db

    existing = tmp_path / "genesis.db"
    existing.touch()
    assert require_results_db(existing) == existing

    with pytest.raises(RuntimeError, match="GENESIS_REPO_ROOT"):
        require_results_db(tmp_path / "nope" / "genesis.db")
