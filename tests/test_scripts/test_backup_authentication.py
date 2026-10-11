"""Whole restore rejects counterfeit encryption and preserves live destinations."""

import json
import subprocess

import pytest

from tests.test_scripts.test_restore_offsite_pull import _run
from tests.test_scripts.test_restore_offsite_pull import sandbox as sandbox


@pytest.mark.parametrize("kind", ["memory", "eval", "secrets"])
@pytest.mark.parametrize("damage", ["literal", "bad-integrity"])
def test_restore_reports_incomplete_and_preserves_destination(sandbox, tmp_path, monkeypatch, kind, damage):
    home, repo, backup = sandbox["home"], sandbox["gd"], sandbox["backup"]
    monkeypatch.setenv("GNUPGHOME", str(home / ".gnupg"))
    # This private fixture has no running application or open database. Main's
    # supplementary host-wide /proc scan cannot inspect unrelated host users;
    # the owned offline boundary lets the test reach the decryption under test.
    monkeypatch.setenv("GENESIS_RESTORE_HOLDER_SCAN", "none")
    # An unrelated, valid SQL backup proves the run has genuine recovery work.
    (backup / "data").mkdir()
    (backup / "data/genesis.sql.gpg").write_bytes(sandbox["dump_gpg"].read_bytes())
    relative = "secrets.env" if kind == "secrets" else "note.md"
    source = backup / kind / (relative + ".gpg")
    source.parent.mkdir()
    if damage == "literal":
        plain = tmp_path / "counterfeit"
        plain.write_bytes(b"attacker-selected plaintext\n")
        subprocess.run(["gpg", "--no-options", "--batch", "--store", "--output", str(source), str(plain)],
                       capture_output=True, check=True)
    else:
        cipher = sandbox["sec_gpg" if kind == "secrets" else "mem_gpg"].read_bytes()
        source.write_bytes(cipher[:-1] + bytes([cipher[-1] ^ 1]))
    if kind == "secrets":
        target = repo / relative
    elif kind == "eval":
        target = home / ".genesis/eval" / relative
    else:
        target = home / ".claude/projects" / str(repo).replace("/", "-") / "memory" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"existing destination must survive\n")
    try:
        result = _run(sandbox, backend="none")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "decrypt failed" in result.stdout
        assert target.read_bytes() == b"existing destination must survive\n"
        status = json.loads((home / ".genesis/restore_status.json").read_text())
        assert not status["success"], status
        assert not list(target.parent.glob(".gpg-restore-*"))
    finally:
        subprocess.run(["gpgconf", "--homedir", str(home / ".gnupg"), "--kill", "gpg-agent"],
                       capture_output=True, check=False)
