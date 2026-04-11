"""Unit tests for genesis.contribution.identity."""
from __future__ import annotations

import json
import uuid

import pytest

from genesis.contribution import identity


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect GENESIS_HOME to a tmp dir so tests never touch ~/.genesis."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    # Also clear any cached module state — identity does not cache, but
    # guard against future refactors.
    return tmp_path


def test_first_access_creates_install(tmp_home):
    # File does not exist yet
    install_file = tmp_home / "install.json"
    assert not install_file.exists()
    info = identity.load_install_info()
    assert install_file.exists()
    # UUID round-trip
    uuid.UUID(info.install_id)
    assert info.created_at  # some ISO string
    # Re-loading returns identical data
    again = identity.load_install_info()
    assert again.install_id == info.install_id
    assert again.created_at == info.created_at


def test_corrupt_file_regenerates(tmp_home):
    install_file = tmp_home / "install.json"
    install_file.write_text("not json at all", encoding="utf-8")
    info = identity.load_install_info()
    uuid.UUID(info.install_id)
    # File is now valid
    data = json.loads(install_file.read_text(encoding="utf-8"))
    assert data["install_id"] == info.install_id


def test_missing_required_field_regenerates(tmp_home):
    install_file = tmp_home / "install.json"
    install_file.write_text(json.dumps({"not_the_right_key": 1}), encoding="utf-8")
    info = identity.load_install_info()
    uuid.UUID(info.install_id)


def test_invalid_uuid_regenerates(tmp_home):
    install_file = tmp_home / "install.json"
    install_file.write_text(
        json.dumps({"install_id": "not-a-uuid", "created_at": "2026-01-01"}),
        encoding="utf-8",
    )
    info = identity.load_install_info()
    uuid.UUID(info.install_id)
    assert info.install_id != "not-a-uuid"


def test_get_install_id_returns_string(tmp_home):
    iid = identity.get_install_id()
    uuid.UUID(iid)


def test_pseudonym_email_uses_short_prefix(tmp_home):
    full_uuid = "12345678-1234-5678-1234-567812345678"
    email = identity.pseudonym_email(full_uuid)
    assert email == "contributor-12345678@genesis.local"


def test_pseudonym_email_handles_empty():
    email = identity.pseudonym_email("")
    assert email.endswith("@genesis.local")


def test_install_json_is_valid_json(tmp_home):
    identity.load_install_info()
    install_file = tmp_home / "install.json"
    data = json.loads(install_file.read_text(encoding="utf-8"))
    assert "install_id" in data
    assert "created_at" in data
    assert "fingerprint_file" in data


def test_parent_dir_created(tmp_path, monkeypatch):
    # Point GENESIS_HOME at a nested non-existent dir
    nested = tmp_path / "a" / "b" / "c"
    monkeypatch.setenv("GENESIS_HOME", str(nested))
    info = identity.load_install_info()
    assert (nested / "install.json").exists()
    uuid.UUID(info.install_id)
