"""Dashboard timezone control + secrets-editor tz exclusion.

- GET /api/genesis/settings/timezone now returns the IANA ``options`` list (for
  the dropdown) alongside the current ``timezone``.
- POST writes genesis.yaml and the value is reflected even when ``USER_TIMEZONE``
  env is set — i.e. the silent no-op is fixed by the file-first resolver flip.
- The generic secrets editor no longer exposes or accepts USER_TIMEZONE /
  GENESIS_TIMEZONE (they are managed by the dedicated control above).

``Path.home`` is patched so the route reads/writes genesis.yaml under tmp_path
(the route uses ``Path.home()/.genesis/config/genesis.yaml``, matching the
resolver). The tz singleton is reset in teardown to avoid cross-test leakage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    from genesis.env import _invalidate_local_config

    _invalidate_local_config()
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    yield app.test_client()
    _invalidate_local_config()
    # Reset the process-global display-tz cache mutated by the POST → tz.reload().
    import genesis.util.tz as _tz

    _tz._USER_TZ = _tz.ZoneInfo("UTC")


def test_get_timezone_returns_options(client):
    resp = client.get("/api/genesis/settings/timezone")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "timezone" in data
    opts = data.get("options")
    assert isinstance(opts, list) and len(opts) > 100
    assert "America/Chicago" in opts
    assert "UTC" in opts
    assert opts == sorted(opts)  # stable ordering


def test_post_writes_file_and_reflects_over_env(client, tmp_path, monkeypatch):
    # Bug-repro: env is a DIFFERENT zone; the POST writes genesis.yaml and both the
    # POST response and a fresh GET must reflect the FILE value (file-first), not
    # the stale env value. Against the old env-first resolver this returned Tokyo.
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
    from genesis.env import _invalidate_local_config

    _invalidate_local_config()

    resp = client.post("/api/genesis/settings/timezone", json={"timezone": "Europe/Paris"})
    assert resp.status_code == 200
    assert resp.get_json()["timezone"] == "Europe/Paris"

    cfg = yaml.safe_load((tmp_path / ".genesis" / "config" / "genesis.yaml").read_text())
    assert cfg["timezone"] == "Europe/Paris"

    got = client.get("/api/genesis/settings/timezone").get_json()["timezone"]
    assert got == "Europe/Paris"


def test_post_rejects_invalid_zone(client):
    resp = client.post("/api/genesis/settings/timezone", json={"timezone": "Not/AZone"})
    assert resp.status_code == 400


def test_secrets_editor_hides_timezone_keys(client):
    resp = client.get("/api/genesis/secrets")
    assert resp.status_code == 200
    keys = {k["key"] for g in resp.get_json()["groups"] for k in g["keys"]}
    assert "USER_TIMEZONE" not in keys
    assert "GENESIS_TIMEZONE" not in keys


def test_secrets_put_rejects_timezone_keys(client):
    resp = client.put("/api/genesis/secrets", json={"keys": {"USER_TIMEZONE": "Europe/Paris"}})
    assert resp.status_code == 422
    assert "Unknown key" in str(resp.get_json())
