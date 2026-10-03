"""Foreground-only strict configuration and CLI boundary regressions."""
from __future__ import annotations

import json

import pytest

from genesis import _config_overlay
from genesis.cc import gmodel_routes, gmodel_settings, roster


@pytest.mark.parametrize("contents", ["broken: [", "[]", "null", "false"])
def test_strict_base_load_never_restores_a_default(contents, tmp_path):
    (tmp_path / "cc_roster.yaml").write_text(contents)
    with pytest.raises(roster.RosterError):
        roster.load_roster(tmp_path, strict=True)
    assert roster.load_roster(tmp_path) == {}


def test_strict_overlay_directory_cannot_be_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    (tmp_path / "cc_roster.local.yaml").mkdir()
    with pytest.raises(_config_overlay.ConfigOverlayError):
        _config_overlay.merge_local_overlay({"default": "auto"}, tmp_path / "cc_roster.yaml", strict=True)


@pytest.mark.parametrize("contents", ["", "null", "{}"])
def test_empty_strict_overlay_is_a_valid_noop(contents, tmp_path, monkeypatch):
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    (tmp_path / "cc_roster.local.yaml").write_text(contents)
    assert _config_overlay.merge_local_overlay({"a": 1}, tmp_path / "cc_roster.yaml", strict=True) == {"a": 1}


@pytest.mark.parametrize("argument", ["-p", "--print", "--bg", "--background", "--resume", "--continue"])
def test_mode_flags_inside_prompt_values_do_not_change_billing(argument):
    assert not gmodel_settings.launch_flags(["--append-system-prompt", argument])
    assert not gmodel_settings.launch_flags(["--", argument])
    assert argument in gmodel_settings.launch_flags([argument])


def test_catalog_rejects_secret_bearing_endpoint_before_listing():
    config = {"gmodel": {"models": {"example": {"routes": {"api": {
        "anthropic_base_url": "https://example.invalid/?key=accidental-secret",
        "model_id": "example", "auth_env": "EXAMPLE_KEY",
    }}}}}}
    with pytest.raises(roster.RosterError) as error:
        gmodel_routes.catalog(config)
    assert "accidental-secret" not in str(error.value)


def test_selected_provider_key_can_use_anthropic_variable():
    selected = gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                          "ANTHROPIC_API_KEY", "example", "bearer", 1048576)
    env = gmodel_routes.apply_route_env({"ANTHROPIC_API_KEY": "provider-key"}, selected)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "provider-key"
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize("selector", gmodel_routes.PROVIDER_SELECTORS)
def test_alternate_provider_switches_cannot_override_catalog(selector, tmp_path, monkeypatch):
    monkeypatch.setattr(gmodel_settings, "_MANAGED_DIR", tmp_path / "managed")
    selected = gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                          "EXAMPLE_KEY", "example", "bearer", 1048576)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", selector: "1",
                                        "HOME": str(tmp_path)}, selected)
    assert selector not in env
    with pytest.raises(roster.RosterError, match=selector):
        gmodel_settings.validate_settings(["--settings", json.dumps({"env": {selector: "1"}})], env, cwd=tmp_path)
    for disabled in ("", "0", "false", "no", "off"):
        gmodel_settings.validate_settings(["--settings", json.dumps({"env": {selector: disabled}})], env, cwd=tmp_path)


@pytest.mark.parametrize("name", ["claude", "opus", "sonnet", "haiku", "default", "legacy-peer"])
def test_invalid_unrelated_foreground_config_preserves_existing_launches(name):
    data = {"models": {"claude": {}, "legacy-peer": {}}, "gmodel": {"models": {
        "broken": {"route": "oops"},
    }}}
    assert gmodel_routes.resolve_route(name, roster_data=data, environ={}) is None
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("broken", roster_data=data, environ={})


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_explicit_yaml_effort_accepts_matching_cli_and_settings(effort, tmp_path, monkeypatch):
    monkeypatch.setattr(gmodel_settings, "_MANAGED_DIR", tmp_path / "managed")
    selected = gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                          "EXAMPLE_KEY", "example", "bearer", 1048576, effort)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", "HOME": str(tmp_path)}, selected)
    gmodel_settings.validate_settings(["--effort", effort, "--settings", json.dumps({"effortLevel": effort})], env, cwd=tmp_path)


def test_malformed_overlay_is_never_logged_before_strict_error(tmp_path, monkeypatch, caplog):
    (tmp_path / "cc_roster.yaml").write_text("models: {}\ngmodel: {models: {}}\n")
    user_config = tmp_path / "user-config"
    user_config.mkdir()
    (user_config / "cc_roster.local.yaml").write_text("gmodel: [SYNTHETIC_SECRET\n")
    monkeypatch.setattr(roster, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_config)
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("kimi-k3", environ={})
    assert "SYNTHETIC_SECRET" not in caplog.text


@pytest.mark.parametrize("optional", sorted(gmodel_settings._OPTIONAL_OPTIONS))
def test_optional_arguments_cannot_hide_later_routing_pins(optional, tmp_path, monkeypatch):
    monkeypatch.setattr(gmodel_settings, "_MANAGED_DIR", tmp_path / "managed")
    route, remaining = gmodel_settings.extract_route([optional, "--route", "api", "--settings", '{"model":"other"}'])
    assert route == "api"
    env = {"HOME": str(tmp_path), "ANTHROPIC_MODEL": "expected"}
    with pytest.raises(roster.RosterError):
        gmodel_settings.validate_settings(remaining, env, cwd=tmp_path)
    assert "--background" in gmodel_settings.launch_flags([optional, "--background"])


def test_custom_proxy_headers_cannot_cross_selected_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(gmodel_settings, "_MANAGED_DIR", tmp_path / "managed")
    selected = gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                          "EXAMPLE_KEY", "example", "bearer", 1048576)
    with pytest.raises(roster.RosterError) as error:
        gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", "ANTHROPIC_CUSTOM_HEADERS": "X-Key: OLD_SECRET"}, selected)
    assert "OLD_SECRET" not in str(error.value)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", "HOME": str(tmp_path)}, selected)
    with pytest.raises(roster.RosterError) as error:
        gmodel_settings.validate_settings(["--settings", '{"env":{"ANTHROPIC_CUSTOM_HEADERS":"X-Key: OLD_SECRET"}}'], env, cwd=tmp_path)
    assert "OLD_SECRET" not in str(error.value)
