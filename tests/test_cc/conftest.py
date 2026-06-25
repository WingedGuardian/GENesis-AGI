"""Shared fixtures for CC invoker tests.

``CCInvoker._build_args`` lazily generates a per-install CC span-settings file
(``cc_span_settings_path``). Redirect it to a temp path for every test in this
package so the suite never writes the real ``~/.genesis/cc-span-settings.json``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cc_span_settings(tmp_path, monkeypatch):
    from genesis.cc import invoker as inv_mod

    monkeypatch.setattr(
        inv_mod, "_CC_SPAN_SETTINGS_PATH", tmp_path / "cc-span-settings.json",
    )
