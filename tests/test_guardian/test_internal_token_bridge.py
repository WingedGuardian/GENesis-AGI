"""Internal API token escrow — container→host shared-mount propagation.

The host-side Guardian reads this token to bearer-authenticate its
/api/genesis/guardian-dialogue POST when the dashboard gates /api mutations.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import genesis.guardian.credential_bridge as cb


@pytest.fixture(autouse=True)
def _isolate_credential_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed default for EVERY test here: point all three host credential
    sources at absent tmp paths so no combined-bridge invocation escrows a real
    host token. A positive-contract test overrides ONLY its intended source in-body.
    (Twin of the fixture in test_credential_bridge.py — kept local to each file so
    the isolation is scoped exactly to the two credential-bridge test modules and
    never perturbs other guardian tests via a shared conftest.)

    Uses the SHARED ``monkeypatch`` fixture, NOT a fixture-owned ``MonkeyPatch`` (see
    the twin's docstring): tests here override HOME and GENESIS_HOME in-body, so a
    fixture-owned instance over the same keys restores in the wrong order at teardown
    and leaks HOME to a deleted tmp dir, reddening unrelated suites in full-suite CI.
    One shared instance → correct LIFO teardown. Same INVARIANT as the twin: no test
    here may call ``monkeypatch.undo()`` / ``delenv`` / ``delattr`` on the three
    isolated keys mid-body.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setattr(cb, "_CC_TOKEN_SOURCE", tmp_path / "cc_oauth_token.env")
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis-home"))


def test_propagate_then_load_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "container" / "internal_api_token"
    src.parent.mkdir(parents=True)
    src.write_text("tok-abc123\n")
    shared = tmp_path / "state" / "shared"

    out = cb.propagate_internal_api_token(shared_dir=shared, source_path=src)
    assert out is not None
    assert out.name == "internal_api_token.env"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    # Host reads from {state_dir}/shared/guardian/... ; state_dir == tmp_path/state.
    assert cb.load_internal_api_token(str(tmp_path / "state")) == "tok-abc123"


def test_absent_source_skips(tmp_path: Path) -> None:
    assert (
        cb.propagate_internal_api_token(
            shared_dir=tmp_path / "shared", source_path=tmp_path / "nope"
        )
        is None
    )


def test_empty_token_skips(tmp_path: Path) -> None:
    src = tmp_path / "internal_api_token"
    src.write_text("   \n")
    assert cb.propagate_internal_api_token(shared_dir=tmp_path / "shared", source_path=src) is None


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert cb.load_internal_api_token(str(tmp_path / "nope")) is None


def test_propagate_honors_genesis_home(tmp_path: Path, monkeypatch) -> None:
    """The source path follows GENESIS_HOME (matching where the dashboard writes
    it), not a hardcoded ~/.genesis — else the host Guardian gets no token."""
    gh = tmp_path / "custom-home"
    gh.mkdir()
    (gh / "internal_api_token").write_text("tok-home\n")
    monkeypatch.setenv("GENESIS_HOME", str(gh))

    out = cb.propagate_internal_api_token(shared_dir=tmp_path / "shared")
    assert out is not None
    assert cb.load_internal_api_token(str(tmp_path)) == "tok-home"


def test_combined_bridge_includes_internal_token(tmp_path: Path, monkeypatch) -> None:
    """The combined awareness-tick bridge propagates the internal token too."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    gh = tmp_path / "gh"
    gh.mkdir()
    (gh / "internal_api_token").write_text("tok-xyz\n")
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    secrets = tmp_path / "secrets.env"
    secrets.write_text("TELEGRAM_BOT_TOKEN=bot\n")

    written = cb.propagate_guardian_credentials(
        shared_dir=tmp_path / "state" / "shared", secrets_path=secrets
    )
    names = sorted(p.name for p in written)
    assert "internal_api_token.env" in names
    # Regression guard: this test overrides ONLY its intended source (GENESIS_HOME).
    # The CC-OAuth leg must stay isolated by _isolate_credential_sources, so no real
    # host CC token is escrowed here. (RED before the fixture — the import-frozen
    # _CC_TOKEN_SOURCE leaked cc_oauth_token.env.)
    assert "cc_oauth_token.env" not in names
