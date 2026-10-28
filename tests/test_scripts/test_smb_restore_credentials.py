"""Real restore with credential-aware SMB filesystem transport; no NAS server."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.test_scripts.test_restore_offsite_pull import (
    _NEW,
    _db_value,
    _make_stub,
    _run,
    _snapshot,
)
from tests.test_scripts.test_restore_offsite_pull import sandbox as sandbox
from tests.test_scripts.test_transcript_reader import archive, payload


@pytest.mark.parametrize("state", ["fresh", "cached", "missing-cached-object", "missing-object", "legacy", "extra", "early-refusal", "dry-run"])
def test_smb_credential_lease_covers_pool_and_later_reads(sandbox, tmp_path, monkeypatch, state):
    monkeypatch.setenv("GNUPGHOME", str(sandbox["home"] / ".gnupg"))
    for key, value in {"GENESIS_BACKUP_NAS": "//synthetic/share", "GENESIS_BACKUP_NAS_USER": "synthetic-user",
                       "GENESIS_BACKUP_NAS_PASS": "synthetic-password", "SMB_ROOT": str(sandbox["offsite"]),
                       "SMB_LOG": str(tmp_path / "smb.log"), "SMB_AUTH_LOG": str(tmp_path / "auth.log")}.items():
        monkeypatch.setenv(key, value)
    model = Path(__file__).parents[1] / "smbclient_filesystem_model.py"
    _make_stub(sandbox["bind"] / "smbclient", model.read_text())
    _snapshot(sandbox, "sourcebox", _NEW)
    snap = sandbox["offsite"] / "Genesis/sourcebox" / _NEW
    destination = sandbox["home"] / ".claude/projects/project/subagents/agent-a.jsonl"
    try:
        if state not in ("legacy", "early-refusal", "dry-run"):
            plain, name = payload(tmp_path)
            cipher = tmp_path / name
            archive.crypt(plain, cipher, b"testpass")
            object_id = f"123-{archive.checksum(cipher)}.gpg"
            objects = snap.parent / "transcript-objects"
            objects.mkdir()
            remote_object = objects / object_id
            remote_object.write_bytes(cipher.read_bytes())
            (snap / "transcripts").mkdir()
            manifest_plain = tmp_path / "manifest.json"
            manifest_plain.write_text(json.dumps({"version": 1, "snapshot": f"Genesis/sourcebox/{_NEW}",
                                                  "captures": [[name, object_id, cipher.stat().st_size]]}))
            manifest = snap / "transcripts/manifest-v1.json.gpg"
            archive.crypt(manifest_plain, manifest, b"testpass")
            (snap / "COMPLETE").write_text("genesis-snapshot 1\ntranscript-pool 1\n")
            if state == "extra":
                extra_plain = tmp_path / "extra.tar"
                with tarfile.open(extra_plain, "w") as out:
                    directory = tarfile.TarInfo("owned-extra")
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o755
                    out.addfile(directory)
                    info = tarfile.TarInfo("owned-extra/value.txt")
                    info.size = len(b"owned extra")
                    out.addfile(info, io.BytesIO(b"owned extra"))
                (snap / "extra").mkdir()
                archive.crypt(extra_plain, snap / "extra/owned-extra.tar.gpg", b"testpass")
                with (snap / "COMPLETE").open("a") as out:
                    out.write("extra owned-extra.tar.gpg\n")
            if state in ("cached", "missing-cached-object"):
                first = _run(sandbox, backend="smb", host_override="sourcebox")
                assert first.returncode == 0, first.stdout + first.stderr
                assert destination.read_bytes() == b"raw\n"
                manifest.unlink()
                if state == "missing-cached-object":
                    caches = list((sandbox["home"] / "backups/.genesis-restore").glob(f"*/cache/transcripts/{object_id}"))
                    assert len(caches) == 1
                    caches[0].unlink()
            if state == "missing-object":
                remote_object.unlink()
        if state == "early-refusal":
            (snap / "COMPLETE").unlink()
        if state == "dry-run":
            credential_tmp = tmp_path / "credential-temp"
            credential_tmp.mkdir()
            monkeypatch.setenv("TMPDIR", str(credential_tmp))
            proc = _run(sandbox, backend="smb", host_override="sourcebox", extra_args=["--dry-run"])
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert not list(credential_tmp.iterdir())
            assert not (sandbox["gd"] / "data/genesis.db").exists()
            return
        proc = _run(sandbox, backend="smb", host_override="sourcebox")
        assert proc.returncode == (1 if state in ("missing-object", "early-refusal") else 0), proc.stdout + proc.stderr
        if state == "early-refusal":
            assert proc.returncode == 1
            assert not (sandbox["gd"] / "data/genesis.db").exists()
        else:
            assert _db_value(sandbox) == "99"
        if state == "missing-object":
            assert not destination.exists()
            assert "pooled inventory/object recovery incomplete" in proc.stdout
        elif state not in ("legacy", "early-refusal", "dry-run"):
            assert destination.read_bytes() == b"raw\n"
            assert destination.stat().st_mtime_ns == 1234567890000000000
        if state == "extra":
            assert (sandbox["home"] / "owned-extra/value.txt").read_bytes() == b"owned extra"
        credentials = (tmp_path / "auth.log").read_text().splitlines()
        assert credentials and all(credentials)
        assert all(not Path(path).exists() for path in credentials)
    finally:
        subprocess.run(["gpgconf", "--homedir", str(sandbox["home"] / ".gnupg"), "--kill", "gpg-agent"], check=True)
