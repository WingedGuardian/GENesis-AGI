"""Shared fixtures for CC invoker tests.

Two per-install paths are generated lazily on the launch path, and both are
redirected to temp for every test in this package so the suite never writes the
operator's real home:

* ``cc_span_settings_path`` → ``~/.genesis/cc-span-settings.json``, the settings
  file injected into dispatched sessions.
* ``_sealed_gh_config_dir`` → ``~/.genesis/gh-sealed``, which COPIES the
  operator's real ``gh`` credential into it. Anything that builds an env for an
  invocation carrying ``bash_allowlist=("gh",)`` reaches it, which is a much
  wider set of tests than the ones that name the seal — MEASURED: the allowlist
  launch tests created a real seal holding the real token before this fixture
  existed. Allowlist polarity on purpose: a test written next year inherits the
  isolation rather than having to remember it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cc_span_settings(tmp_path, monkeypatch):
    from genesis.cc import invoker as inv_mod

    monkeypatch.setattr(
        inv_mod,
        "_CC_SPAN_SETTINGS_PATH",
        tmp_path / "cc-span-settings.json",
    )


@pytest.fixture(autouse=True)
def _isolate_sealed_gh_config(tmp_path, monkeypatch):
    from genesis.cc import invoker as inv_mod

    monkeypatch.setattr(
        inv_mod,
        "_SEALED_GH_CONFIG_DIR",
        tmp_path / "gh-sealed",
    )


@pytest.fixture(autouse=True)
def _guard_the_seal_isolation(tmp_path):
    """Fail loudly if the isolation above is ever removed.

    Without this, deleting ``_isolate_sealed_gh_config`` breaks nothing visible:
    the suite still passes, and the only symptom is a credential copy appearing
    in the developer's home. That is precisely the shape that went unnoticed
    once already.
    """
    yield
    from genesis.cc import invoker as inv_mod

    sealed = inv_mod._SEALED_GH_CONFIG_DIR
    assert tmp_path in sealed.parents or sealed == tmp_path / "gh-sealed", (
        f"the gh seal is not redirected to tmp ({sealed}); a test in this "
        "package can write the operator's real credential into their home"
    )
