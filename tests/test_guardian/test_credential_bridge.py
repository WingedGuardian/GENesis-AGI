"""Tests for Telegram credential bridge — shared mount propagation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from genesis.guardian.credential_bridge import (
    load_telegram_credentials,
    propagate_telegram_credentials,
)


class TestPropagateTelegramCredentials:
    """Test container-side credential writer."""

    def _write_secrets(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_writes_only_telegram_keys(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "API_KEY_GROQ=groq-secret\n"
            "TELEGRAM_BOT_TOKEN=bot123\n"
            "TELEGRAM_FORUM_CHAT_ID=chat456\n"
            "API_KEY_MISTRAL=mistral-secret\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "bot123" in text
        assert "chat456" in text
        assert "groq-secret" not in text
        assert "mistral-secret" not in text

    def test_maps_forum_chat_id_to_chat_id(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100999\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-100999" in text
        # Should NOT contain the source key name
        assert "TELEGRAM_FORUM_CHAT_ID" not in text

    def test_accepts_direct_chat_id(self, tmp_path: Path) -> None:
        """If secrets.env uses TELEGRAM_CHAT_ID directly, it still works."""
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_CHAT_ID=-200888\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-200888" in text

    def test_forum_chat_id_takes_priority(self, tmp_path: Path) -> None:
        """TELEGRAM_FORUM_CHAT_ID wins over TELEGRAM_CHAT_ID when both present."""
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100first\n"
            "TELEGRAM_CHAT_ID=-200second\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-100first" in text

    def test_chmod_600(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        mode = stat.S_IMODE(os.stat(result).st_mode)
        assert mode == 0o600

    def test_returns_none_no_bot_token(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_FORUM_CHAT_ID=chat-only\n"
            "API_KEY_GROQ=something\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is None

    def test_returns_none_missing_secrets(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(
            shared_dir=shared,
            secrets_path=tmp_path / "nonexistent.env",
        )
        assert result is None

    def test_creates_directory(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "deep" / "nested" / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        assert result.parent.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=tok\n"
            "TELEGRAM_FORUM_CHAT_ID=chat\n"
        ))
        shared = tmp_path / "shared"
        result1 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        result2 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result1 is not None
        assert result2 is not None
        assert result1.read_text() == result2.read_text()

    def test_includes_thread_id(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=tok\n"
            "TELEGRAM_THREAD_ID=42\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_THREAD_ID=42" in text

    def test_handles_quoted_values(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN='quoted-tok'\n"
            "TELEGRAM_FORUM_CHAT_ID=\"double-quoted\"\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "quoted-tok" in text
        assert "double-quoted" in text

    def test_handles_export_prefix(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "export TELEGRAM_BOT_TOKEN=export-tok\n"
            "export TELEGRAM_FORUM_CHAT_ID=export-chat\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "export-tok" in text
        assert "export-chat" in text

    def test_skips_write_when_unchanged(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "shared"
        result1 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result1 is not None
        mtime1 = result1.stat().st_mtime
        # Second call should skip write (content unchanged)
        import time
        time.sleep(0.01)  # Ensure mtime would differ if rewritten
        result2 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result2 is not None
        mtime2 = result2.stat().st_mtime
        assert mtime1 == mtime2


class TestLoadTelegramCredentials:
    """Test host-side credential reader."""

    def test_reads_valid_file(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "shared" / "guardian"
        creds_dir.mkdir(parents=True)
        (creds_dir / "telegram_creds.env").write_text(
            "TELEGRAM_BOT_TOKEN=bot123\n"
            "TELEGRAM_CHAT_ID=chat456\n"
        )
        result = load_telegram_credentials(str(tmp_path))
        assert result["TELEGRAM_BOT_TOKEN"] == "bot123"
        assert result["TELEGRAM_CHAT_ID"] == "chat456"

    def test_returns_empty_for_missing(self, tmp_path: Path) -> None:
        result = load_telegram_credentials(str(tmp_path / "nonexistent"))
        assert result == {}

    def test_handles_comments_and_blanks(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "shared" / "guardian"
        creds_dir.mkdir(parents=True)
        (creds_dir / "telegram_creds.env").write_text(
            "# This is a comment\n"
            "\n"
            "TELEGRAM_BOT_TOKEN=tok\n"
            "  \n"
        )
        result = load_telegram_credentials(str(tmp_path))
        assert result["TELEGRAM_BOT_TOKEN"] == "tok"
        assert len(result) == 1


class TestRoundTrip:
    """Test write → read round trip."""

    def test_propagate_then_load(self, tmp_path: Path) -> None:
        # Container side: write to shared_dir (= {state_dir}/shared on host)
        secrets = tmp_path / "container" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(
            "TELEGRAM_BOT_TOKEN=my-bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100group\n"
            "TELEGRAM_THREAD_ID=7\n"
            "API_KEY_GROQ=should-not-appear\n"
        )
        # Simulate the real mount: writer sees shared_dir, reader sees state_dir/shared
        shared = tmp_path / "state" / "shared"
        propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)

        # Host side: load_telegram_credentials takes state_dir, adds shared/guardian/
        result = load_telegram_credentials(str(tmp_path / "state"))
        assert result["TELEGRAM_BOT_TOKEN"] == "my-bot-token"
        assert result["TELEGRAM_CHAT_ID"] == "-100group"
        assert result["TELEGRAM_THREAD_ID"] == "7"
        assert "API_KEY_GROQ" not in result
        assert "TELEGRAM_FORUM_CHAT_ID" not in result


class TestProvisioningCredentials:
    """Proxmox token propagation — only the three token keys ever cross."""

    def test_writes_only_proxmox_tokens(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import (
            load_provisioning_credentials,
            propagate_provisioning_credentials,
        )
        secrets = tmp_path / "container" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(
            "ANTHROPIC_API_KEY=should-not-appear\n"
            "PROXMOX_AUDIT_TOKEN=genesis@pve!ro=aaa\n"
            "PROXMOX_PROVISION_TOKEN=genesis@pve!provision=bbb\n"
            "PROXMOX_BACKUP_TOKEN=genesis@pve!backup=ccc\n"
            "TELEGRAM_BOT_TOKEN=should-not-appear-here\n"
        )
        shared = tmp_path / "state" / "shared"
        out = propagate_provisioning_credentials(shared_dir=shared, secrets_path=secrets)
        assert out is not None
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
        result = load_provisioning_credentials(str(tmp_path / "state"))
        assert result["PROXMOX_AUDIT_TOKEN"] == "genesis@pve!ro=aaa"
        assert result["PROXMOX_PROVISION_TOKEN"] == "genesis@pve!provision=bbb"
        assert result["PROXMOX_BACKUP_TOKEN"] == "genesis@pve!backup=ccc"
        assert "ANTHROPIC_API_KEY" not in result
        assert "TELEGRAM_BOT_TOKEN" not in result

    def test_requires_audit_token(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_provisioning_credentials
        secrets = tmp_path / "secrets.env"
        secrets.write_text("PROXMOX_PROVISION_TOKEN=only-provision\n")
        assert propagate_provisioning_credentials(
            shared_dir=tmp_path / "shared", secrets_path=secrets,
        ) is None

    def test_audit_only_still_propagates(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import (
            load_provisioning_credentials,
            propagate_provisioning_credentials,
        )
        secrets = tmp_path / "secrets.env"
        secrets.write_text("PROXMOX_AUDIT_TOKEN=audit-only\n")
        out = propagate_provisioning_credentials(
            shared_dir=tmp_path / "state" / "shared", secrets_path=secrets,
        )
        assert out is not None
        result = load_provisioning_credentials(str(tmp_path / "state"))
        assert result == {"PROXMOX_AUDIT_TOKEN": "audit-only"}


class TestCombinedBridge:
    """propagate_guardian_credentials fans out to both, each guarded."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, monkeypatch):
        # The mirror leg defaults its source to ~/backups/genesis-backups. Isolate
        # HOME to a clone-free sandbox so these telegram/proxmox assertions stay
        # deterministic regardless of the host having a real backup clone.
        monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))

    def test_writes_both_when_present(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_guardian_credentials
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_FORUM_CHAT_ID=chat\n"
            "PROXMOX_AUDIT_TOKEN=audit\nPROXMOX_PROVISION_TOKEN=prov\n"
        )
        shared = tmp_path / "state" / "shared"
        written = propagate_guardian_credentials(shared_dir=shared, secrets_path=secrets)
        names = sorted(p.name for p in written)
        assert names == ["proxmox_creds.env", "telegram_creds.env"]

    def test_only_telegram_when_no_proxmox(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_guardian_credentials
        secrets = tmp_path / "secrets.env"
        secrets.write_text("TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_CHAT_ID=chat\n")
        written = propagate_guardian_credentials(
            shared_dir=tmp_path / "state" / "shared", secrets_path=secrets,
        )
        assert [p.name for p in written] == ["telegram_creds.env"]

    def test_never_raises_on_missing_secrets(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_guardian_credentials
        # Nonexistent secrets file → empty list, no exception.
        written = propagate_guardian_credentials(
            shared_dir=tmp_path / "shared", secrets_path=tmp_path / "nope.env",
        )
        assert written == []


class TestBackupPassphraseEscrow:
    """GENESIS_BACKUP_PASSPHRASE escrow — breaks the circular backup trap."""

    def test_escrows_only_the_passphrase(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import (
            load_backup_passphrase,
            propagate_backup_passphrase,
        )
        secrets = tmp_path / "container" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(
            "ANTHROPIC_API_KEY=should-not-appear\n"
            "GENESIS_BACKUP_PASSPHRASE=s3cret-pass-phrase\n"
            "TELEGRAM_BOT_TOKEN=should-not-appear\n"
        )
        shared = tmp_path / "state" / "shared"
        out = propagate_backup_passphrase(shared_dir=shared, secrets_path=secrets)
        assert out is not None
        assert out.name == "backup_passphrase.env"
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
        result = load_backup_passphrase(str(tmp_path / "state"))
        assert result == {"GENESIS_BACKUP_PASSPHRASE": "s3cret-pass-phrase"}

    def test_absent_passphrase_skips(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_backup_passphrase
        secrets = tmp_path / "secrets.env"
        secrets.write_text("TELEGRAM_BOT_TOKEN=bot\n")
        assert propagate_backup_passphrase(
            shared_dir=tmp_path / "shared", secrets_path=secrets,
        ) is None

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import load_backup_passphrase
        assert load_backup_passphrase(str(tmp_path / "nope")) == {}

    def test_combined_bridge_includes_passphrase(self, tmp_path: Path, monkeypatch) -> None:
        from genesis.guardian.credential_bridge import propagate_guardian_credentials
        # Isolate HOME so the mirror leg (which defaults its source to
        # ~/backups/genesis-backups) is a deterministic no-op here — no clone in
        # the sandbox home means no "creds-mirror" entry. Mirror is covered by
        # TestMirrorCredentialBackup below.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_FORUM_CHAT_ID=chat\n"
            "PROXMOX_AUDIT_TOKEN=audit\n"
            "GENESIS_BACKUP_PASSPHRASE=pp\n"
        )
        written = propagate_guardian_credentials(
            shared_dir=tmp_path / "state" / "shared", secrets_path=secrets,
        )
        names = sorted(p.name for p in written)
        assert names == ["backup_passphrase.env", "proxmox_creds.env", "telegram_creds.env"]

    def test_combined_bridge_includes_mirror_when_clone_present(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """With a backup clone present, the combined bridge also mirrors it."""
        from genesis.guardian.credential_bridge import propagate_guardian_credentials
        home = tmp_path / "home"
        clone = home / "backups" / "genesis-backups"
        (clone / "secrets").mkdir(parents=True)
        (clone / "secrets" / "secrets.env.gpg").write_bytes(b"ENC")
        monkeypatch.setenv("HOME", str(home))
        secrets = tmp_path / "secrets.env"
        secrets.write_text("GENESIS_BACKUP_PASSPHRASE=pp\n")
        written = propagate_guardian_credentials(
            shared_dir=tmp_path / "state" / "shared", secrets_path=secrets,
        )
        names = sorted(p.name for p in written)
        assert "creds-mirror" in names
        assert "backup_passphrase.env" in names


class TestMirrorCredentialBackup:
    """G.4 container-side encrypted-backup mirror to the shared mount."""

    def _clone(self, root: Path, rels=("creds/claude.json.gpg",
                                       "creds/ssh/id_ed25519.gpg",
                                       "secrets/secrets.env.gpg")) -> Path:
        clone = root / "clone"
        for rel in rels:
            p = clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"ENCRYPTED-" + rel.encode())
        return clone

    def test_mirrors_encrypted_files_0600_with_stamp(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = self._clone(tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        assert dest is not None
        assert (dest / "creds" / "claude.json.gpg").read_bytes().startswith(b"ENCRYPTED-")
        assert (dest / "creds" / "ssh" / "id_ed25519.gpg").exists()
        assert (dest / "secrets" / "secrets.env.gpg").exists()
        assert (dest / "MIRROR_STAMP").exists()
        mode = stat.S_IMODE(os.stat(dest / "creds" / "claude.json.gpg").st_mode)
        assert mode == 0o600

    def test_skips_unchanged_on_second_run(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = self._clone(tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        f = dest / "secrets" / "secrets.env.gpg"
        ino1 = os.stat(f).st_ino
        mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        # Unchanged source ⇒ no rewrite ⇒ same inode (os.replace would swap it).
        assert os.stat(f).st_ino == ino1

    def test_prunes_vanished_source(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = self._clone(tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir()
        dest = mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        assert (dest / "creds" / "claude.json.gpg").exists()
        (clone / "creds" / "claude.json.gpg").unlink()
        mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        assert not (dest / "creds" / "claude.json.gpg").exists()
        assert (dest / "secrets" / "secrets.env.gpg").exists()  # survivor kept

    def test_prune_containment_leaves_siblings(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = self._clone(tmp_path)
        shared = tmp_path / "shared"
        (shared / "guardian").mkdir(parents=True)
        sibling = shared / "guardian" / "telegram_creds.env"  # sibling of creds-mirror
        sibling.write_text("TELEGRAM_BOT_TOKEN=keepme\n")
        mirror_credential_backup(shared_dir=shared, backup_dir=clone)
        assert sibling.read_text() == "TELEGRAM_BOT_TOKEN=keepme\n"

    def test_absent_shared_mount_returns_none(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = self._clone(tmp_path)
        assert mirror_credential_backup(
            shared_dir=tmp_path / "nonexistent", backup_dir=clone,
        ) is None

    def test_absent_clone_returns_none(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        shared = tmp_path / "shared"
        shared.mkdir()
        assert mirror_credential_backup(
            shared_dir=shared, backup_dir=tmp_path / "no-clone",
        ) is None

    def test_clone_without_gpg_returns_none(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import mirror_credential_backup
        clone = tmp_path / "clone"
        (clone / "creds").mkdir(parents=True)
        (clone / "creds" / "readme.txt").write_text("not encrypted")
        shared = tmp_path / "shared"
        shared.mkdir()
        assert mirror_credential_backup(shared_dir=shared, backup_dir=clone) is None


class TestCCOAuthTokenSync:
    """CC setup-token sync — dedicated source file, never secrets.env."""

    def test_syncs_token_and_created_at(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import (
            load_cc_oauth_token,
            propagate_cc_oauth_token,
        )
        src = tmp_path / "container" / "cc_oauth_token.env"
        src.parent.mkdir(parents=True)
        src.write_text(
            "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SYNTHETIC\n"
            "GENESIS_CC_TOKEN_CREATED_AT=1700000000\n",
        )
        shared = tmp_path / "state" / "shared"
        out = propagate_cc_oauth_token(shared_dir=shared, source_path=src)
        assert out is not None
        assert out.name == "cc_oauth_token.env"
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
        result = load_cc_oauth_token(str(tmp_path / "state"))
        assert result == {
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SYNTHETIC",
            "GENESIS_CC_TOKEN_CREATED_AT": "1700000000",
        }

    def test_backfills_created_at_from_mtime(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import (
            _read_dotenv,
            propagate_cc_oauth_token,
        )
        src = tmp_path / "cc_oauth_token.env"
        src.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-X\n")
        out = propagate_cc_oauth_token(shared_dir=tmp_path / "shared", source_path=src)
        assert out is not None
        back = _read_dotenv(out)
        assert back["GENESIS_CC_TOKEN_CREATED_AT"].isdigit()

    def test_absent_source_skips(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_cc_oauth_token
        assert propagate_cc_oauth_token(
            shared_dir=tmp_path / "shared", source_path=tmp_path / "nope.env",
        ) is None

    def test_no_token_key_skips(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import propagate_cc_oauth_token
        src = tmp_path / "cc_oauth_token.env"
        src.write_text("GENESIS_CC_TOKEN_CREATED_AT=123\n")  # created_at, no token
        assert propagate_cc_oauth_token(
            shared_dir=tmp_path / "shared", source_path=src,
        ) is None

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        from genesis.guardian.credential_bridge import load_cc_oauth_token
        assert load_cc_oauth_token(str(tmp_path / "nope")) == {}

    def test_combined_bridge_includes_cc_token(self, tmp_path: Path, monkeypatch) -> None:
        # The composite calls the leg without source_path (module default), so
        # point _CC_TOKEN_SOURCE at a tmp file for this test.
        from genesis.guardian import credential_bridge as cb
        src = tmp_path / "cc_oauth_token.env"
        src.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-X\n")
        monkeypatch.setattr(cb, "_CC_TOKEN_SOURCE", src)
        secrets = tmp_path / "secrets.env"
        secrets.write_text("TELEGRAM_BOT_TOKEN=bot\nTELEGRAM_CHAT_ID=chat\n")
        written = cb.propagate_guardian_credentials(
            shared_dir=tmp_path / "state" / "shared", secrets_path=secrets,
        )
        assert "cc_oauth_token.env" in [p.name for p in written]

    def test_never_reads_from_secrets_env(self, tmp_path: Path) -> None:
        # A token in secrets.env must NOT be picked up — the leg reads only its
        # dedicated file (guards the load_dotenv override hazard by construction).
        from genesis.guardian.credential_bridge import propagate_cc_oauth_token
        secrets = tmp_path / "secrets.env"
        secrets.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-FROM-SECRETS\n")
        out = propagate_cc_oauth_token(
            shared_dir=tmp_path / "shared",
            source_path=tmp_path / "absent.env",
            secrets_path=secrets,
        )
        assert out is None  # secrets_path is ignored for this leg
