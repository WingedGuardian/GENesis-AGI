"""PATCH response masks secret/sensitive config values (Codex #1131 idx-27 follow-on).

Skipping a masked sensitive field on save preserves its stored value; the PATCH
response must then NOT echo that value back (update_config returns the full live
config). _mask_config_response nulls secret/sensitive keys in the response.
"""

from __future__ import annotations

from genesis.dashboard.routes.modules import _mask_config_response


def test_masks_secret_and_sensitive_values():
    fields = [
        {"name": "api_key", "type": "secret", "value": "sk-real"},
        {"name": "token", "type": "string", "sensitive": True, "value": "tok-real"},
        {"name": "threshold", "type": "int", "value": 50},
    ]
    config = {"api_key": "sk-real", "token": "tok-real", "threshold": 50}
    masked = _mask_config_response(config, fields)
    assert masked["api_key"] is None
    assert masked["token"] is None
    assert masked["threshold"] == 50


def test_no_sensitive_fields_passthrough():
    fields = [{"name": "threshold", "type": "int", "value": 50}]
    assert _mask_config_response({"threshold": 50}, fields) == {"threshold": 50}
