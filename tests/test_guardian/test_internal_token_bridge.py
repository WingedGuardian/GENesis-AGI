"""Internal API token escrow — container→host shared-mount propagation.

The host-side Guardian reads this token to bearer-authenticate its
/api/genesis/guardian-dialogue POST when the dashboard gates /api mutations.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import genesis.guardian.credential_bridge as cb


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
    assert "internal_api_token.env" in sorted(p.name for p in written)
