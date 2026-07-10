"""Tests for the container-side credential self-heal policy."""

from __future__ import annotations

import json

import pytest

from genesis.guardian import cred_selfheal
from genesis.guardian.cred_integrity import CredTarget, RestoreResult


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A sandbox home + backup dir + a single json target, restore patched."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    backup = tmp_path / "home" / "backups" / "genesis-backups" / "creds"
    backup.mkdir(parents=True)
    (backup / "claude_credentials.json.gpg").write_bytes(b"encrypted")  # presence only
    status = home / ".genesis" / "cred_integrity_status.json"

    target = CredTarget(
        "claude_credentials", ".claude/.credentials.json",
        "creds/claude_credentials.json.gpg", "json", required_keys=("claudeAiOauth",),
    )

    monkeypatch.setattr(cred_selfheal, "resolve_passphrase", lambda h: "p")

    calls = {"restore": 0}

    def fake_restore(t, *, home, backup_dir, passphrase):
        calls["restore"] += 1
        return RestoreResult(True, "restored", aside_path=str(home / "aside"),
                             backup_mtime="2026-07-10T00:00:00+00:00", detail="ok")

    monkeypatch.setattr(cred_selfheal, "restore_file", fake_restore)

    def run(startup=False, max_per_day=3):
        return cred_selfheal.check_and_selfheal(
            home=home,
            backup_dir=home / "backups" / "genesis-backups",
            status_path=status,
            targets=(target,),
            startup=startup,
            max_restores_per_day=max_per_day,
        )

    def write_creds(content: bytes):
        (home / ".claude" / ".credentials.json").write_bytes(content)

    return type("Env", (), {
        "run": staticmethod(run), "write": staticmethod(write_creds),
        "calls": calls, "home": home, "target": target,
    })


_GOOD = b'{"claudeAiOauth": {"t": 1}}'
_BAD_JSON = b'{"claudeAiOauth": '   # parse_error (ambiguous → 2-tick)
_ZEROED = b"\x00" * 16              # nul_bytes (immediate)


def test_healthy_is_ok(env):
    env.write(_GOOD)
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "ok"
    assert env.calls["restore"] == 0


def test_parse_error_needs_two_ticks(env):
    env.write(_BAD_JSON)
    first = env.run()
    assert first["targets"]["claude_credentials"]["status"] == "corrupt_pending"
    assert env.calls["restore"] == 0
    second = env.run()  # still corrupt → confirmed → restore
    assert second["targets"]["claude_credentials"]["status"] == "restored"
    assert env.calls["restore"] == 1


def test_nul_bytes_restores_immediately(env):
    env.write(_ZEROED)
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "restored"
    assert env.calls["restore"] == 1


def test_startup_restores_parse_error_immediately(env):
    env.write(_BAD_JSON)
    out = env.run(startup=True)
    assert out["targets"]["claude_credentials"]["status"] == "restored"
    assert env.calls["restore"] == 1


def test_rate_cap_blocks_restore(env, monkeypatch):
    env.write(_ZEROED)
    env.run(max_per_day=1)                      # first restore consumes the cap
    assert env.calls["restore"] == 1
    out = env.run(max_per_day=1)                # second is rate-capped
    assert out["targets"]["claude_credentials"]["status"] == "restore_failed"
    assert "rate cap" in out["targets"]["claude_credentials"]["detail"]
    assert env.calls["restore"] == 1           # no new attempt


def test_no_passphrase_is_restore_failed(env, monkeypatch):
    monkeypatch.setattr(cred_selfheal, "resolve_passphrase", lambda h: None)
    env.write(_ZEROED)
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "restore_failed"
    assert "passphrase" in out["targets"]["claude_credentials"]["detail"]
    assert env.calls["restore"] == 0


def test_unreadable_is_alert_only(env, monkeypatch):
    from genesis.guardian.cred_integrity import ValidationResult
    monkeypatch.setattr(
        cred_selfheal, "check_all",
        lambda tgts, home, backup: {
            "claude_credentials": ValidationResult(False, "unreadable", "perm denied")
        },
    )
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "corrupt"
    assert env.calls["restore"] == 0


def test_status_file_written_with_events(env):
    env.write(_ZEROED)
    env.run()
    data = json.loads((env.home / ".genesis" / "cred_integrity_status.json").read_text())
    assert data["version"] == 1
    assert "checked_at" in data
    assert any("restored" in e for e in data["recent_events"])


def test_restored_persists_then_collapses(env, monkeypatch):
    # Restore, then the file becomes valid → status stays "restored" within TTL.
    env.write(_ZEROED)
    env.run()
    env.write(_GOOD)
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "restored"
    # Force the restored record to be older than the TTL → collapses to ok.
    monkeypatch.setattr(cred_selfheal, "_RESTORED_TTL_S", -1)
    out2 = env.run()
    assert out2["targets"]["claude_credentials"]["status"] == "ok"


def test_never_raises_on_check_failure(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(cred_selfheal, "check_all", boom)
    # Must not raise; returns prior status (empty here).
    out = env.run()
    assert isinstance(out, dict)
