"""Shared fixtures for CC invoker tests.

Two per-install paths are generated lazily on the launch path, and both are
redirected to temp for every test in this package so the suite never writes the
operator's real home:

* ``cc_span_settings_path`` → ``~/.genesis/cc-span-settings.json``, the settings
  file injected into dispatched sessions.
* ``_sealed_gh_config_dir`` → ``~/.genesis/gh-sealed``. Since 2026-09-25 it
  copies NO credential — but these tests still BUILD, WRITE and CHMOD that
  path, so without the redirect a run rewrites the real seal (contents replaced,
  locked at 0500) under the operator's home. Anything that builds an env for an
  invocation carrying ``bash_allowlist=("gh",)`` reaches it, which is a much
  wider set of tests than the ones that name the seal — MEASURED: before this
  fixture existed the allowlist launch tests created a real seal, and in the
  copying era it held the real token. Allowlist polarity on purpose: a test
  written next year inherits the isolation rather than having to remember it.
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

    THE ORIGINAL RATIONALE IS NOW HISTORICAL, and saying so is the point: the
    seal no longer copies a credential, so deleting ``_isolate_sealed_gh_config``
    can no longer put the operator's token in a developer's home. Kept anyway,
    for a smaller reason that is still live — these tests build and CHMOD the
    directory named by ``_SEALED_GH_CONFIG_DIR``, so without the redirect a run
    rewrites the real seal (0500, contents replaced) underneath any dispatch
    using it, and one test deliberately drives that path to failure.

    Left in place rather than deleted because the cost is one assertion and the
    failure it prevents is silent either way. What was updated is the REASON: a
    fixture whose docstring cites a hazard that no longer exists teaches the next
    reader something false, and is the first thing deleted by someone who checks.
    """
    yield
    from genesis.cc import invoker as inv_mod

    sealed = inv_mod._SEALED_GH_CONFIG_DIR
    assert tmp_path in sealed.parents or sealed == tmp_path / "gh-sealed", (
        f"the gh seal is not redirected to tmp ({sealed}); a test in this "
        "package can write the operator's real credential into their home"
    )
