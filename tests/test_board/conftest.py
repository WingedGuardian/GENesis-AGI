"""Shared fixtures for the board tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fingerprints_present(tmp_path, monkeypatch):
    """scan_prose fails closed on a MISSING fingerprint file (terminal egress
    guard). CI has no ~/.genesis fingerprint file, so point scan_prose at a
    present (empty) one; the promotion path then depends only on the input, not
    on the ambient install. A test of the missing-file path sets its own."""
    fp = tmp_path / "fingerprints.txt"
    fp.write_text("")
    monkeypatch.setenv("GENESIS_RELEASE_FINGERPRINTS", str(fp))
