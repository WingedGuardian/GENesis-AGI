"""Tests for the container-side credential self-heal policy."""

from __future__ import annotations

import json
import os

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

    monkeypatch.setattr(cred_selfheal, "resolve_passphrase", lambda h, repo_root=None: "p")

    calls = {"restore": 0}

    def fake_restore(t, *, home, backup_dir, passphrase, repo_root=None):
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


def test_a_backups_directory_that_denies_search_is_reported(tmp_path, request):
    """It promises never to raise, and the awareness loop relies on that. A file it can't
    tell the state of is reported, not read as never provisioned."""
    if os.geteuid() == 0:
        pytest.skip("root ignores mode 000")
    home = tmp_path / "home"
    backups = home / "backups"
    (backups / "genesis-backups").mkdir(parents=True)
    backups.chmod(0o000)
    request.addfinalizer(lambda: backups.chmod(0o700))
    target = CredTarget("claude_credentials", ".claude/.credentials.json", "creds/x.gpg", "json")
    out = cred_selfheal.check_and_selfheal(home=home, status_path=tmp_path / "status.json", targets=(target,))
    assert out["targets"]["claude_credentials"]["status"] == "corrupt"
    assert "unreadable" in out["targets"]["claude_credentials"]["detail"]


def test_no_passphrase_is_restore_failed(env, monkeypatch):
    monkeypatch.setattr(cred_selfheal, "resolve_passphrase", lambda h, repo_root=None: None)
    env.write(_ZEROED)
    out = env.run()
    assert out["targets"]["claude_credentials"]["status"] == "restore_failed"
    assert "passphrase" in out["targets"]["claude_credentials"]["detail"]
    assert env.calls["restore"] == 0


def test_unreadable_is_alert_only(env, monkeypatch):
    from genesis.guardian.cred_integrity import ValidationResult
    monkeypatch.setattr(
        cred_selfheal, "check_all",
        lambda tgts, home, backup, repo_root=None: {
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


def test_selfheal_finds_secrets_in_the_checkout_it_runs_from(tmp_path, monkeypatch):
    # A checkout outside ~/genesis: without the process's own root the guardian
    # resolved home/genesis/secrets.env, reported it missing, and restored over
    # nothing while the live file sat in the real checkout.
    from genesis.guardian.cred_integrity import DEFAULT_TARGETS

    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.delenv("GENESIS_REPO_ROOT", raising=False)
    home = tmp_path / "home"
    checkout = tmp_path / "elsewhere"
    checkout.mkdir(parents=True)
    (checkout / "secrets.env").write_text("ANTHROPIC_API_KEY=x\nOTHER=y\nMORE=z\n")
    backups = home / "backups" / "genesis-backups"
    (backups / "secrets").mkdir(parents=True)
    (backups / "secrets" / "secrets.env.gpg").write_bytes(b"x")
    target = next(t for t in DEFAULT_TARGETS if t.name == "secrets_env")
    restores = []
    monkeypatch.setattr(cred_selfheal, "restore_file", lambda *a, **k: restores.append(k))
    monkeypatch.setattr("genesis.env.repo_root", lambda: checkout)
    monkeypatch.setenv("HOME", str(home))  # the process's own home: home=None below
    out = cred_selfheal.check_and_selfheal(
        backup_dir=backups, status_path=tmp_path / "status.json", targets=(target,)
    )
    assert restores == [], "restored although the live file is in the process's checkout"
    assert out["targets"]["secrets_env"]["status"] != "corrupt", out["targets"]["secrets_env"]


def test_a_supplied_home_never_resolves_into_the_real_checkout(tmp_path, monkeypatch):
    # A sandbox home (every test) must keep the env/home rule, never the
    # process's own repo root, or a real checkout file could be moved aside.
    seen = []
    monkeypatch.setattr("genesis.env.repo_root", lambda: tmp_path / "real-checkout")
    monkeypatch.setattr(cred_selfheal, "check_all", lambda t, h, b, repo_root=None: seen.append(repo_root) or {})
    cred_selfheal.check_and_selfheal(home=tmp_path / "sandbox", status_path=tmp_path / "s.json", targets=())
    assert seen == [None]
