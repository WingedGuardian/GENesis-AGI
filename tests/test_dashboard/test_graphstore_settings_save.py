"""The dashboard's graphstore save persists only the keys the user changed (D3).

The settings editor PUTs the WHOLE current config. Merged as-is, every shipped
default lands in the overlay, and for graphstore that pins `mode`, turning the
quiet shipped falkordb default (graphstore_config.selection) into a loud
explicit choice. The settings dirs point at temp dirs; the validator's view of
the engine is pinned so the route's own behaviour is what is under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.mcp.health import settings as settings_mod


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base_dir = tmp_path / "repo_config"
    base_dir.mkdir()
    (base_dir / "graphstore.yaml").write_text(yaml.safe_dump({"enabled": True, "mode": "falkordb"}))
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(settings_mod, "_CONFIG_DIR", base_dir)
    monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", user_dir)
    # The validator's engine view: shipped default, no unit, no socket, so it
    # refuses exactly when the write would name `mode`.
    from genesis.memory import graphstore_config

    monkeypatch.setattr(
        graphstore_config, "load_config", lambda: {"enabled": True, "mode": "falkordb"}
    )
    monkeypatch.setattr(graphstore_config, "mode_set_by_overlay", lambda: False)
    monkeypatch.setattr(graphstore_config, "engine_unit_present", lambda: False)
    monkeypatch.setattr(
        "genesis.env.falkordb_socket_path", lambda: str(tmp_path / "no.sock"), raising=True
    )
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return {"client": app.test_client(), "overlay": user_dir / "graphstore.local.yaml"}


def _overlay(env) -> dict:
    return yaml.safe_load(env["overlay"].read_text()) or {}


def test_the_editor_shows_the_stored_mode(env):
    """Owner ruling ef540876: the page shows the stored config, not the mode
    reads use (here networkx, the engine being unprovisioned)."""
    resp = env["client"].get("/api/genesis/settings/graphstore")
    assert resp.status_code == 200
    assert resp.get_json()["config"]["mode"] == "falkordb"


@pytest.mark.parametrize("with_snapshot", [True, False])
def test_saving_another_key_leaves_mode_out_of_the_overlay(env, with_snapshot):
    """The editor sends back the mode it was shown, with or without its snapshot
    (a tab still running the editor from before the snapshot existed)."""
    shown = env["client"].get("/api/genesis/settings/graphstore").get_json()["config"]
    body = {**shown, "enabled": False}
    if with_snapshot:
        body["__shown"] = shown
    resp = env["client"].put("/api/genesis/settings/graphstore", json=body)
    assert resp.status_code == 200, resp.get_json()
    stored = _overlay(env)
    assert stored.get("enabled") is False
    assert "mode" not in stored, "an unrelated save must not pin the shipped mode"


def test_explicitly_changing_mode_still_writes_it(env):
    """The editor showed falkordb (an install where reads use the engine) and the
    user picked networkx: a change against what was shown, so it is written."""
    shown = {"enabled": True, "mode": "falkordb"}
    resp = env["client"].put(
        "/api/genesis/settings/graphstore",
        json={"enabled": True, "mode": "networkx", "__shown": shown},
    )
    assert resp.status_code == 200, resp.get_json()
    stored = _overlay(env)
    assert stored.get("mode") == "networkx"
    assert "enabled" not in stored, "an unchanged key is not copied in"


def test_a_mode_already_in_the_overlay_stays_editable(env):
    env["overlay"].write_text(yaml.safe_dump({"mode": "networkx"}))
    resp = env["client"].put(
        "/api/genesis/settings/graphstore", json={"enabled": False, "mode": "networkx"}
    )
    assert resp.status_code == 200, resp.get_json()
    assert _overlay(env) == {"mode": "networkx", "enabled": False}


def test_validation_still_refuses_selecting_an_unarmed_engine(env):
    env["overlay"].write_text(yaml.safe_dump({"mode": "networkx"}))
    resp = env["client"].put(
        "/api/genesis/settings/graphstore", json={"enabled": True, "mode": "falkordb"}
    )
    assert resp.status_code == 422
    assert "not armed" in str(resp.get_json())
    assert _overlay(env) == {"mode": "networkx"}


def test_an_explicit_falkordb_choice_is_written_when_reads_are_on_networkx(env, tmp_path):
    """Devin 4521703457: with the engine started by hand but its unit not enabled,
    choosing falkordb equals the SHIPPED value. A client that says so with a
    snapshot (here ``{}``: nothing was shown) gets it into the overlay."""
    import socket

    sock_path = tmp_path / "no.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    try:
        resp = env["client"].put(
            "/api/genesis/settings/graphstore",
            json={"mode": "falkordb", "__shown": {}},
        )
    finally:
        server.close()
    assert resp.status_code == 200, resp.get_json()
    assert _overlay(env).get("mode") == "falkordb"


def test_a_value_of_another_type_is_not_taken_as_unchanged(env):
    """Devin 4521703582: 1 == True in Python. A submitted 1 for `enabled` must
    reach validation (and be refused), not be dropped as equal to True."""
    resp = env["client"].put(
        "/api/genesis/settings/graphstore", json={"enabled": 1, "mode": "networkx"}
    )
    assert resp.status_code == 422
    assert "boolean" in str(resp.get_json())
    assert not env["overlay"].exists() or "enabled" not in _overlay(env)


def test_the_shown_snapshot_decides_what_changed(env):
    """With the editor's snapshot, a key is written exactly when the user edited
    it: here only `enabled`, though the submitted `mode` differs from the stored
    falkordb."""
    shown = {"enabled": True, "mode": "networkx"}
    resp = env["client"].put(
        "/api/genesis/settings/graphstore",
        json={"enabled": False, "mode": "networkx", "__shown": shown},
    )
    assert resp.status_code == 200, resp.get_json()
    assert _overlay(env) == {"enabled": False}


def test_the_snapshot_itself_is_never_written(env):
    shown = {"enabled": True, "mode": "networkx"}
    resp = env["client"].put(
        "/api/genesis/settings/graphstore",
        json={"enabled": False, "mode": "networkx", "__shown": shown},
    )
    assert resp.status_code == 200, resp.get_json()
    assert "__shown" not in _overlay(env)


def test_the_snapshot_is_stripped_for_every_area_not_only_graphstore(env, tmp_path):
    """dashboard.js attaches __shown to every area's save. The route removes it
    before validation and the write; codex_validator's validator refuses any
    key but dry_run_only, so a leaked snapshot would show here as a 422."""
    (tmp_path / "repo_config" / "codex_validator.yaml").write_text(
        yaml.safe_dump({"dry_run_only": True})
    )
    resp = env["client"].put(
        "/api/genesis/settings/codex_validator",
        json={"dry_run_only": False, "__shown": {"dry_run_only": True}},
    )
    assert resp.status_code == 200, resp.get_json()
    written = yaml.safe_load(
        (tmp_path / "user_config" / "codex_validator.local.yaml").read_text()
    )
    assert written == {"dry_run_only": False}
