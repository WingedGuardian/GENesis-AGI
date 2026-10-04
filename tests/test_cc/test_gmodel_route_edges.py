"""Foreground-only strict configuration, pin-file and CLI boundary regressions."""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess

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


def test_strict_dangling_user_overlay_symlink_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    (user / "cc_roster.local.yaml").symlink_to(tmp_path / "gone" / "cc_roster.local.yaml")
    repo = tmp_path / "repo"
    repo.mkdir()
    # A repo-sibling overlay that a fallback would silently pick up instead.
    (repo / "cc_roster.local.yaml").write_text("gmodel: {models: {}}\n")
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user)
    monkeypatch.setattr(_config_overlay, "_resolve_overlay_path",
                        lambda base_path: base_path.with_suffix(".local.yaml"))
    with pytest.raises(_config_overlay.ConfigOverlayError):
        _config_overlay.merge_local_overlay({"a": 1}, repo / "cc_roster.yaml", strict=True)
    # Lenient callers keep their established fallback behaviour.
    assert _config_overlay.merge_local_overlay({"a": 1}, repo / "cc_roster.yaml") == {
        "a": 1, "gmodel": {"models": {}}}


def test_strict_absent_user_overlay_delegates_to_the_shared_resolver(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path / "nothing-here")

    def resolver(base_path):  # one positional argument, like every test sandbox
        seen.append(base_path)
        return tmp_path / "absent.local.yaml"

    monkeypatch.setattr(_config_overlay, "_resolve_overlay_path", resolver)
    assert _config_overlay.merge_local_overlay({"a": 1}, tmp_path / "cc_roster.yaml", strict=True) == {"a": 1}
    assert seen == [tmp_path / "cc_roster.yaml"]


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


@pytest.mark.parametrize("mode", ["api_key", "bearer"])
def test_auth_mode_field_is_refused(mode):
    config = {"gmodel": {"models": {"example": {"routes": {"api": {
        "anthropic_base_url": "https://example.invalid", "model_id": "example",
        "auth_env": "EXAMPLE_KEY", "auth_mode": mode,
    }}}}}}
    with pytest.raises(roster.RosterError, match="auth_mode"):
        gmodel_routes.catalog(config)


def _selected(effort="high"):
    return gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                       "EXAMPLE_KEY", "example", 1048576, effort)


def test_selected_provider_key_can_use_anthropic_variable():
    selected = gmodel_routes.SelectedRoute("example", "api", "https://example.invalid",
                                           "ANTHROPIC_API_KEY", "example", 1048576)
    env = gmodel_routes.apply_route_env({"ANTHROPIC_API_KEY": "provider-key"}, selected)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "provider-key"
    assert env["ANTHROPIC_API_KEY"] == ""


@pytest.mark.parametrize("selector", gmodel_routes.PROVIDER_SELECTORS)
def test_alternate_provider_switches_pinned_empty(selector):
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", selector: "1"}, _selected())
    assert env[selector] == ""
    assert gmodel_routes.route_settings(_selected(), env)["env"][selector] == ""


@pytest.mark.parametrize("variable,inherited", [
    ("DISABLE_AUTO_COMPACT", "1"), ("DISABLE_COMPACT", "1"),
    ("CLAUDE_CODE_DISABLE_THINKING", "1"), ("MAX_THINKING_TOKENS", "0"),
    ("CLAUDE_CODE_SIMPLE", "1"), ("ANTHROPIC_CUSTOM_HEADERS", "X-Key: OLD_SECRET"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "old-oauth"),
])
def test_feature_switches_and_extra_credentials_cannot_survive(variable, inherited):
    """An inherited switch is overwritten in the launch env AND pinned in --settings."""
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key", variable: inherited}, _selected())
    pinned = gmodel_routes.route_settings(_selected(), env)["env"][variable]
    assert env[variable] == pinned
    assert pinned in ("", "0")


def test_settings_document_pins_thinking_compaction_and_model():
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _selected("max"))
    document = gmodel_routes.route_settings(_selected("max"), env)
    assert document["alwaysThinkingEnabled"] is True
    assert document["autoCompactEnabled"] is True
    assert document["model"] == "example"
    # Highest file wins the whole chain, so this replaces any user-level chain
    # that would send a Claude model ID to the routed endpoint on overload.
    assert document["fallbackModel"] == ["example"]
    assert document["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert document["env"]["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    assert document["env"]["ANTHROPIC_AUTH_TOKEN"] == "provider-key"
    for variable in roster._ROSTER_MODEL_ENV_VARS:
        assert document["env"][variable] == "example"


def test_route_settings_file_is_owner_only_and_content_named(tmp_path):
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _selected())
    document = gmodel_routes.route_settings(_selected(), env)
    directory = tmp_path / "gmodel-settings"
    path = gmodel_routes.write_route_settings(document, directory)
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == document
    assert "provider-key" not in path.name
    # Same content -> same file; different content -> a different file, so a
    # running session's settings file is never rewritten under it.
    assert gmodel_routes.write_route_settings(document, directory) == path
    other = gmodel_routes.route_settings(_selected("max"), {**env, "CLAUDE_CODE_EFFORT_LEVEL": "max"})
    assert gmodel_routes.write_route_settings(other, directory) != path
    assert json.loads(path.read_text()) == document


def test_route_settings_file_tightens_loose_permissions(tmp_path):
    directory = tmp_path / "gmodel-settings"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _selected())
    document = gmodel_routes.route_settings(_selected(), env)
    path = gmodel_routes.write_route_settings(document, directory)
    os.chmod(path, 0o644)
    assert gmodel_routes.write_route_settings(document, directory) == path
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_route_settings_directory_symlink_refused(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "gmodel-settings"
    link.symlink_to(real, target_is_directory=True)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _selected())
    with pytest.raises(roster.RosterError) as error:
        gmodel_routes.write_route_settings(gmodel_routes.route_settings(_selected(), env), link)
    assert "provider-key" not in str(error.value)
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("name", sorted(gmodel_routes.NATIVE_NAMES))
def test_native_names_never_load_configuration(name, monkeypatch):
    def broken(*args, **kwargs):
        raise roster.RosterError("Cannot load configuration overlay")

    monkeypatch.setattr(roster, "load_roster", broken)
    assert gmodel_routes.resolve_route(name, environ={}) is None


def test_native_names_cover_every_cc_tier():
    from genesis.cc.types import VALID_MODEL_NAMES

    assert VALID_MODEL_NAMES <= gmodel_routes.NATIVE_NAMES
    assert {roster.CLAUDE, "default"} <= gmodel_routes.NATIVE_NAMES


@pytest.mark.parametrize("name", ["claude", "opus", "sonnet", "haiku", "fable", "default", "legacy-peer"])
def test_invalid_unrelated_foreground_config_preserves_existing_launches(name):
    data = {"models": {"claude": {}, "legacy-peer": {}}, "gmodel": {"models": {
        "broken": {"route": "oops"},
    }}}
    assert gmodel_routes.resolve_route(name, roster_data=data, environ={}) is None
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("broken", roster_data=data, environ={})


@pytest.mark.parametrize("effort", gmodel_routes.EFFORTS)
def test_explicit_yaml_effort_accepts_only_matching_cli(effort):
    gmodel_settings.validate_cli(["--effort", effort], _selected(effort))
    for other in gmodel_routes.EFFORTS:
        if other != effort:
            with pytest.raises(roster.RosterError):
                gmodel_settings.validate_cli(["--effort", other], _selected(effort))


_BASE_WITH_CATALOG = """\
models:
  legacy-peer: {anthropic_base_url: "https://peer.invalid", auth_env: PEER_KEY, model_id: legacy-peer}
gmodel:
  models:
    kimi-k3:
      routes:
        api: {anthropic_base_url: "https://example.invalid", auth_env: EXAMPLE_KEY, model_id: example}
"""


def _broken_overlay(tmp_path, monkeypatch):
    (tmp_path / "cc_roster.yaml").write_text(_BASE_WITH_CATALOG)
    user_config = tmp_path / "user-config"
    user_config.mkdir()
    (user_config / "cc_roster.local.yaml").write_text("gmodel: [SYNTHETIC_SECRET\n")
    monkeypatch.setattr(roster, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_config)


def test_malformed_overlay_is_never_logged_before_strict_error(tmp_path, monkeypatch, caplog):
    """A catalog member still loads strictly: a broken overlay must not silently
    restore the shipped Auto preference, and the parser text never reaches a log."""
    _broken_overlay(tmp_path, monkeypatch)
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("kimi-k3", environ={"EXAMPLE_KEY": "k"})
    assert "SYNTHETIC_SECRET" not in caplog.text


@pytest.mark.parametrize("name", ["legacy-peer", "not-configured-anywhere"])
def test_broken_overlay_leaves_flat_roster_names_on_their_lenient_path(name, tmp_path, monkeypatch, caplog):
    """Codex 4176543937: strict loading applies only to foreground-catalog members.

    A flat-roster peer (or any non-member) returns None so the launcher keeps the
    lenient roster path it had before the catalog existed; the strict attempt
    itself logs nothing.
    """
    _broken_overlay(tmp_path, monkeypatch)
    assert gmodel_routes.resolve_route(name, environ={}) is None
    assert "SYNTHETIC_SECRET" not in caplog.text


@pytest.mark.parametrize("name", ["kimi-k3", "legacy-peer"])
def test_broken_base_file_is_never_logged_and_falls_to_the_flat_path(name, tmp_path, monkeypatch, caplog):
    """When the BASE file is unreadable, catalog membership cannot be known: the
    selector returns None (the flat path reports the broken base) and its own
    membership read logs no parser text."""
    (tmp_path / "cc_roster.yaml").write_text("models: [SYNTHETIC_SECRET\n")
    monkeypatch.setattr(roster, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path / "no-user-config")
    assert gmodel_routes.resolve_route(name, environ={}) is None
    assert "SYNTHETIC_SECRET" not in caplog.text


def test_selection_validates_only_the_requested_catalog_entry():
    """Codex 4176543940: a broken sibling cannot stop a valid route from launching."""
    data = {"models": {"claude": {}}, "gmodel": {"models": {
        "good": {"routes": {"api": {"anthropic_base_url": "https://example.invalid",
                                    "auth_env": "EXAMPLE_KEY", "model_id": "example"}}},
        "broken-sibling": {"route": "oops", "routes": {"api": {"auth_env": "not a name"}}},
        "opus": {"routes": {}},  # a namespace clash on ANOTHER entry
    }}}
    selected = gmodel_routes.resolve_route("good", roster_data=data, environ={"EXAMPLE_KEY": "k"})
    assert selected is not None and selected.model_id == "example"
    # The listing path still validates the whole catalog.
    with pytest.raises(roster.RosterError):
        gmodel_routes.catalog(data)
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("broken-sibling", roster_data=data, environ={"EXAMPLE_KEY": "k"})


def test_selection_still_applies_the_namespace_rule_to_the_requested_entry():
    data = {"models": {"claude": {}, "shared": {}}, "gmodel": {"models": {
        "shared": {"routes": {"api": {"anthropic_base_url": "https://example.invalid",
                                      "auth_env": "EXAMPLE_KEY", "model_id": "example"}}},
    }}}
    with pytest.raises(roster.RosterError, match="Ambiguous"):
        gmodel_routes.resolve_route("shared", roster_data=data, environ={"EXAMPLE_KEY": "k"})


def test_effort_is_forced_on_for_custom_model_ids_in_both_layers():
    """Codex 4176543942: CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1 in env AND --settings."""
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _selected())
    assert env["CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"] == "1"
    document = gmodel_routes.route_settings(_selected(), env)
    assert document["env"]["CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"] == "1"


def _openrouter(model_id="vendor/example"):
    return gmodel_routes.SelectedRoute("example", "openrouter", "https://openrouter.ai/api",
                                       "EXAMPLE_KEY", model_id, 1048576)


@pytest.mark.parametrize("kind", ["subscription", "api"])
def test_model_switch_is_left_alone_off_openrouter(kind):
    selected = dataclasses.replace(_selected(), route=kind)
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, selected)
    assert "hooks" not in gmodel_routes.route_settings(selected, env)


def test_openrouter_route_refuses_model_switches_with_its_own_hook():
    """Codex 4176543944: on OpenRouter a /model switch to a Claude ID would bill an
    Anthropic model per token on the same key, so the settings layer denies it.

    Runs the generated command itself, with a real PreModelSwitch payload on stdin.
    """
    env = gmodel_routes.apply_route_env({"EXAMPLE_KEY": "provider-key"}, _openrouter())
    document = gmodel_routes.route_settings(_openrouter(), env)
    groups = document["hooks"]["PreModelSwitch"]
    assert [list(group) for group in groups] == [["hooks"]]  # no matcher: every switch
    (hook,) = groups[0]["hooks"]
    assert hook["type"] == "command"
    payload = json.dumps({"hook_event_name": "PreModelSwitch", "from_model": "vendor/example",
                          "to_model": "claude-opus-4-6", "source": "command"})
    result = subprocess.run(["sh", "-c", hook["command"]], input=payload, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 2  # exit 2 blocks the switch (hooks reference)
    assert "OpenRouter" in result.stderr and "relaunch" in result.stderr
    assert "provider-key" not in hook["command"]



@pytest.mark.parametrize("optional", sorted(gmodel_settings._OPTIONAL_OPTIONS))
def test_optional_arguments_cannot_hide_later_routing_pins(optional):
    route, remaining = gmodel_settings.extract_route([optional, "--route", "api", "--settings", '{"model":"other"}'])
    assert route == "api"
    with pytest.raises(roster.RosterError):
        gmodel_settings.validate_cli(remaining, _selected())
    assert "--background" in gmodel_settings.launch_flags([optional, "--background"])
