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


# ── A malformed config must survive the control that recovers it ──────────────


def _write_malformed(tmp_path: Path) -> Path:
    """A genesis.yaml whose ROOT is a list — a plausible hand-edit slip — but that
    still carries real settings the operator would not want to lose."""
    cfg = tmp_path / ".genesis" / "config" / "genesis.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "- network:\n"
        "    ollama_url: http://inference.invalid:11434\n"
        "- github:\n"
        "    user: someone\n"
    )
    return cfg


def test_malformed_root_is_backed_up_not_deleted(client, tmp_path):
    """REGRESSION: coercing a non-mapping root to {} and writing back DELETED it.

    The previous behaviour raised TypeError into an opaque 500 and left the file
    untouched. "Fixing" that by coercing to {} turned a loud, non-destructive
    error into SILENT PERMANENT DATA LOSS — on the one control documented as the
    recovery surface for a broken genesis.yaml. Losing the operator's network and
    github settings to a timezone change is strictly worse than the 500.
    """
    cfg = _write_malformed(tmp_path)
    original = cfg.read_text()

    resp = client.post("/api/genesis/settings/timezone", json={"timezone": "Europe/Paris"})
    assert resp.status_code == 200, resp.get_data(as_text=True)

    # The rewritten file carries the timezone...
    assert yaml.safe_load(cfg.read_text()) == {"timezone": "Europe/Paris"}

    # ...and the original content still exists, byte-identical, beside it.
    backups = sorted(cfg.parent.glob("genesis.malformed-*.yaml"))
    assert len(backups) == 1, f"no backup written; original content is GONE: {backups}"
    assert backups[0].read_text() == original

    # And the operator is TOLD, rather than discovering it later.
    warning = resp.get_json().get("warning", "")
    assert "malformed" in warning.lower()
    assert backups[0].name in warning, "the warning must name where the original went"


def test_a_well_formed_config_is_not_backed_up(client, tmp_path):
    """The control. A backup on every write would be noise, and would hide the
    signal this test exists to protect."""
    cfg = tmp_path / ".genesis" / "config" / "genesis.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("network:\n  ollama_url: http://inference.invalid:11434\n")

    resp = client.post("/api/genesis/settings/timezone", json={"timezone": "Europe/Paris"})
    assert resp.status_code == 200
    assert not list(cfg.parent.glob("genesis.malformed-*.yaml"))
    assert "warning" not in resp.get_json()
    # And the sibling settings SURVIVE a normal write — the whole point of merging
    # into `existing` rather than replacing it.
    data = yaml.safe_load(cfg.read_text())
    assert data["timezone"] == "Europe/Paris"
    assert data["network"]["ollama_url"] == "http://inference.invalid:11434"


def test_the_ui_actually_renders_the_recovery_warning():
    """The API returning `warning` is useless if the handler drops it.

    A deliberately structural test, because there is no JS test harness here: it
    asserts the timezone handler in dashboard.js READS `d.warning` and surfaces it.
    That is weak as tests go — it cannot prove the UI renders correctly — but it is
    not nothing: it catches the exact failure that happened, which was a backend
    field no client consumed, and it fails loudly if someone deletes the branch.
    """
    from genesis.env import repo_root

    js = (repo_root() / "src/genesis/dashboard/webui/js/dashboard.js").read_text()
    start = js.index("async saveTimezone")
    handler = js[start : start + 2000]
    assert "d.warning" in handler, (
        "the timezone handler ignores the API's `warning` field, so an operator is "
        "never told the config was replaced or where the backup went"
    )
    assert "alert(" in handler, "the warning must be impossible to miss, not just logged"


def test_a_second_malformed_write_does_not_overwrite_the_first_backup(client, tmp_path):
    """Timestamps are second-resolution, so two writes in one second collide.

    The backup exists to preserve content that is about to be replaced; a colliding
    name silently destroys the earlier one, which is the same data loss the backup
    was added to prevent, one level down.
    """
    cfg = _write_malformed(tmp_path)
    first_content = cfg.read_text()
    assert client.post("/api/genesis/settings/timezone",
                       json={"timezone": "Europe/Paris"}).status_code == 200

    # Break it again and repeat immediately — same second in practice.
    cfg.write_text("- second: malformed\n")
    second_content = cfg.read_text()
    assert client.post("/api/genesis/settings/timezone",
                       json={"timezone": "Europe/Lisbon"}).status_code == 200

    backups = sorted(cfg.parent.glob("genesis.malformed-*.yaml"))
    assert len(backups) == 2, f"a backup was overwritten: {[b.name for b in backups]}"
    saved = {b.read_text() for b in backups}
    assert first_content in saved and second_content in saved
