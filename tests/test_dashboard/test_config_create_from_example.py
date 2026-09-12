"""Tests for identity create-from-example in the config write resolver.

USER.md is gitignored (per-install, seeded from USER.md.example), so a fresh box
has no USER.md to edit — only the .example seed. The write resolver must allow
CREATING such a file (whitelisted by the presence of a .example sibling) while
still refusing arbitrary new identity files and path traversal.
"""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    import genesis.dashboard.routes.config as config_mod

    identity = tmp_path / "identity"
    identity.mkdir()
    monkeypatch.setattr(config_mod, "_IDENTITY_DIR", identity)
    return app.test_client(), identity


def test_create_user_md_from_example(cfg):
    client, identity = cfg
    (identity / "USER.md.example").write_text("SEED\n")
    assert not (identity / "USER.md").exists()

    resp = client.put("/api/genesis/config-files/USER.md", json={"content": "my profile\n"})
    assert resp.status_code == 200
    assert (identity / "USER.md").read_text() == "my profile\n"


def test_edit_existing_identity_file_still_works(cfg):
    client, identity = cfg
    (identity / "SOUL.md").write_text("original\n")
    resp = client.put("/api/genesis/config-files/SOUL.md", json={"content": "edited\n"})
    assert resp.status_code == 200
    assert (identity / "SOUL.md").read_text() == "edited\n"


def test_cannot_create_identity_file_without_example(cfg):
    client, identity = cfg
    # No NEWFILE.md and no NEWFILE.md.example → creation refused.
    resp = client.put("/api/genesis/config-files/NEWFILE.md", json={"content": "x"})
    assert resp.status_code == 404
    assert not (identity / "NEWFILE.md").exists()


def test_create_from_example_rejects_traversal(cfg):
    client, identity = cfg
    (identity / "USER.md.example").write_text("SEED\n")
    # basename is the last path segment ("passwd"); no passwd.example exists → 404,
    # and nothing is written outside the identity dir.
    resp = client.put("/api/genesis/config-files/..%2f..%2fetc%2fpasswd", json={"content": "x"})
    assert resp.status_code in (403, 404)
    assert not (identity / "passwd").exists()
