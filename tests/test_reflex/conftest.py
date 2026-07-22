"""Shared fixtures for reflex tests.

Install isolation: ``load_reflex_config`` merges the install-local overlay
(``~/.genesis/config/reflex.local.yaml``) resolved by file STEM — so on any
install that has enabled ingestion via the overlay, tmp-file config tests
would silently load ``ingest_enabled: true`` and fail. Point the overlay
resolver at a path that never exists so these tests see only the file they
wrote. (CI has no overlay; this bit only real installs.)
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_local_overlay(monkeypatch, tmp_path):
    import genesis._config_overlay as overlay_mod

    monkeypatch.setattr(
        overlay_mod,
        "_resolve_overlay_path",
        lambda base_path: tmp_path / "no-overlay" / "never.local.yaml",
    )
