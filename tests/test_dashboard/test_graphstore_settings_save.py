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


def test_saving_another_key_leaves_mode_out_of_the_overlay(env):
    resp = env["client"].put(
        "/api/genesis/settings/graphstore", json={"enabled": False, "mode": "falkordb"}
    )
    assert resp.status_code == 200, resp.get_json()
    stored = _overlay(env)
    assert stored.get("enabled") is False
    assert "mode" not in stored, "an unrelated save must not pin the shipped mode"


def test_explicitly_changing_mode_still_writes_it(env):
    resp = env["client"].put(
        "/api/genesis/settings/graphstore", json={"enabled": True, "mode": "networkx"}
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
