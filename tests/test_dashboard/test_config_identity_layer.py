"""The dashboard edits CONVERSATION.md / STEERING.md into their local layer (#2862).

A save never writes the tracked file. It stores the layer that makes the file
resolve to the edited text, refuses a stale save, and shares the steering
writer's lock.
"""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.identity.loader import IdentityLoader
from genesis.identity.paths import REPLACE_MARKER, layer_for, read_identity

_SHIPPED = "# Steering\n\n---\n\nshipped rule\n"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    import genesis.dashboard.routes.config as config_mod

    identity = tmp_path / "identity"
    identity.mkdir()
    (identity / "STEERING.md").write_text(_SHIPPED)
    monkeypatch.setattr(config_mod, "_IDENTITY_DIR", identity)
    return app.test_client(), identity


def _open(client, name="STEERING.md"):
    resp = client.get(f"/api/genesis/config-files/{name}")
    assert resp.status_code == 200
    return resp.get_json()


def test_get_returns_the_resolved_text_and_a_version(cfg):
    client, identity = cfg
    (identity / "STEERING.local.md").write_text("---\n\nlocal rule\n")
    data = _open(client)
    assert data["content"] == read_identity("STEERING.md", identity)
    assert "local rule" in data["content"] and data["version"]


def test_an_added_rule_becomes_an_append_layer(cfg):
    client, identity = cfg
    data = _open(client)
    edited = data["content"].rstrip() + "\n\n---\n\nmy new rule\n"
    resp = client.put(
        "/api/genesis/config-files/STEERING.md",
        json={"content": edited, "version": data["version"]},
    )
    assert resp.status_code == 200 and resp.get_json()["replaced"] is False
    assert (identity / "STEERING.md").read_text() == _SHIPPED
    assert (identity / "STEERING.local.md").read_text() == "---\n\nmy new rule\n"
    assert read_identity("STEERING.md", identity).strip() == edited.strip()


def test_editing_a_shipped_rule_becomes_a_visible_replace_layer(cfg):
    client, identity = cfg
    data = _open(client)
    edited = data["content"].replace("shipped rule", "my wording")
    resp = client.put(
        "/api/genesis/config-files/STEERING.md",
        json={"content": edited, "version": data["version"]},
    )
    assert resp.status_code == 200 and resp.get_json()["replaced"] is True
    assert (identity / "STEERING.md").read_text() == _SHIPPED
    resolved = read_identity("STEERING.md", identity)
    assert resolved.startswith("_[The shipped STEERING.md is replaced by STEERING.local.md.]_")
    assert "my wording" in resolved and "shipped rule" not in resolved


def test_resaving_a_replaced_file_does_not_stack_notes(cfg):
    client, identity = cfg
    data = _open(client)
    client.put(
        "/api/genesis/config-files/STEERING.md",
        json={"content": data["content"].replace("shipped", "mine"), "version": data["version"]},
    )
    again = _open(client)
    client.put(
        "/api/genesis/config-files/STEERING.md",
        json={"content": again["content"] + "\nmore\n", "version": again["version"]},
    )
    assert read_identity("STEERING.md", identity).count("is replaced by") == 1


def test_a_stale_save_is_refused_and_keeps_the_captured_rule(cfg):
    client, identity = cfg
    data = _open(client)
    IdentityLoader(identity_dir=identity).add_steering_rule("Never drop this rule")
    resp = client.put(
        "/api/genesis/config-files/STEERING.md",
        json={"content": data["content"] + "\nedit\n", "version": data["version"]},
    )
    assert resp.status_code == 409
    assert "Never drop this rule" in read_identity("STEERING.md", identity)


def test_a_save_without_a_version_is_refused(cfg):
    client, _ = cfg
    resp = client.put("/api/genesis/config-files/STEERING.md", json={"content": "x\n"})
    assert resp.status_code == 400


def test_the_local_file_cannot_be_written_directly(cfg):
    client, identity = cfg
    resp = client.put("/api/genesis/config-files/STEERING.local.md", json={"content": "x\n"})
    assert resp.status_code == 403
    assert not (identity / "STEERING.local.md").exists()


def test_a_symlinked_local_file_is_refused(cfg, tmp_path):
    client, identity = cfg
    target = tmp_path / "elsewhere.md"
    target.write_text("secret\n")
    (identity / "STEERING.local.md").symlink_to(target)
    got = client.get("/api/genesis/config-files/STEERING.md")
    assert got.status_code == 500 and "secret" not in got.get_data(as_text=True)
    resp = client.put("/api/genesis/config-files/STEERING.md",
                      json={"content": _SHIPPED + "\nx\n", "version": "any"})
    assert resp.status_code == 500
    assert target.read_text() == "secret\n"


@pytest.mark.parametrize(
    ("default", "effective", "expected"),
    [
        (_SHIPPED, _SHIPPED, ""),
        (_SHIPPED, _SHIPPED + "\n---\n\nnew\n", "---\n\nnew\n"),
        (_SHIPPED, _SHIPPED.rstrip() + " appended to the last rule\n", None),
        (None, "only local\n", "only local\n"),
    ],
)
def test_layer_for_round_trips(default, effective, expected):
    from genesis.identity.paths import compose

    layer = layer_for("STEERING.md", default, effective)
    if expected is None:
        assert layer.startswith(REPLACE_MARKER + "\n")
    else:
        assert layer == expected
    resolved = compose("STEERING.md", default, layer) if layer else default
    resolved = resolved.split("]_\n\n", 1)[-1] if layer.startswith(REPLACE_MARKER) else resolved
    assert resolved.strip() == effective.strip()


@pytest.mark.parametrize("name", ["x/STEERING.md", "a/b/CONVERSATION.md", "x/STEERING.local.md"])
def test_a_prefixed_name_never_reaches_the_tracked_file(cfg, name):
    client, identity = cfg
    (identity / "CONVERSATION.md").write_text("conv\n")
    assert client.get(f"/api/genesis/config-files/{name}").status_code == 403
    resp = client.put(f"/api/genesis/config-files/{name}", json={"content": "PWNED\n"})
    assert resp.status_code == 403
    assert (identity / "STEERING.md").read_text() == _SHIPPED
    assert (identity / "CONVERSATION.md").read_text() == "conv\n"


def test_a_paragraph_added_without_a_separator_is_an_append_layer(cfg):
    client, identity = cfg
    (identity / "CONVERSATION.md").write_text("Conv text.\n")
    data = _open(client, "CONVERSATION.md")
    resp = client.put("/api/genesis/config-files/CONVERSATION.md",
                      json={"content": "Conv text.\n\nAnother paragraph.\n", "version": data["version"]})
    body = resp.get_json()
    assert resp.status_code == 200 and body["replaced"] is False
    assert (identity / "CONVERSATION.local.md").read_text() == "---\n\nAnother paragraph.\n"
    assert body["content"] == "Conv text.\n\n---\n\nAnother paragraph.\n"


def test_text_added_to_the_last_line_is_a_replace_layer(cfg):
    client, identity = cfg
    data = _open(client)
    resp = client.put("/api/genesis/config-files/STEERING.md",
                      json={"content": data["content"].rstrip() + " now\n", "version": data["version"]})
    assert resp.get_json()["replaced"] is True


def test_an_emptied_editor_is_refused(cfg):
    client, identity = cfg
    data = _open(client)
    resp = client.put("/api/genesis/config-files/STEERING.md",
                      json={"content": "  \n", "version": data["version"]})
    assert resp.status_code == 422
    assert not (identity / "STEERING.local.md").exists()


def test_an_unchanged_save_creates_no_local_file(cfg):
    client, identity = cfg
    data = _open(client)
    crlf = data["content"].replace("\n", "\r\n")
    resp = client.put("/api/genesis/config-files/STEERING.md",
                      json={"content": crlf, "version": data["version"]})
    assert resp.status_code == 200
    assert not (identity / "STEERING.local.md").exists()


def test_a_fifo_at_the_local_path_does_not_hang_a_read(cfg):
    import os

    client, identity = cfg
    os.mkfifo(identity / "STEERING.local.md")
    assert client.get("/api/genesis/config-files/STEERING.md").status_code == 500


def test_a_symlinked_shipped_file_is_not_followed_on_read(cfg, tmp_path):
    client, identity = cfg
    outside = tmp_path / "outside.md"
    outside.write_text("TOP SECRET\n")
    (identity / "STEERING.md").unlink()
    (identity / "STEERING.md").symlink_to(outside)
    got = client.get("/api/genesis/config-files/STEERING.md")
    assert got.status_code == 500 and "TOP SECRET" not in got.get_data(as_text=True)


@pytest.mark.parametrize("content", [5, ["a"], {"a": 1}, None])
def test_non_string_content_is_a_bad_request(cfg, content):
    client, identity = cfg
    resp = client.put("/api/genesis/config-files/STEERING.md", json={"content": content, "version": "x"})
    assert resp.status_code == 400
    assert not (identity / "STEERING.local.md").exists()
