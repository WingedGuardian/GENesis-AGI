"""Foreground route/billing boundaries, without accounts or provider calls.

The resolver gets explicit config/environment fixtures. Launcher tests replace
config loading, dotenv, PATH lookup and execve; no real credentials are read and
no Claude process can start.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import runpy
import stat
import sys
import types
from pathlib import Path

import pytest

from genesis.cc import gmodel_routes, roster

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GMODEL = _REPO_ROOT / "scripts" / "gmodel"
_SECRET = "synthetic-provider-secret-never-print"
_KEYS = {"subscription": "KIMI_CODING_API_KEY", "api": "MOONSHOT_API_KEY", "openrouter": "API_KEY_OPENROUTER"}


@pytest.fixture
def route_config():
    routes = {
        "subscription": {
            "anthropic_base_url": "https://api.kimi.ai/coding/",
            "auth_env": _KEYS["subscription"],
            "model_id": "k3[1m]",
            "context_tokens": 1048576,
            "effort": "high",
            "interactive_only": True,
        },
        "api": {
            "anthropic_base_url": "https://api.moonshot.ai/anthropic",
            "auth_env": _KEYS["api"],
            "model_id": "kimi-k3[1m]",
            "context_tokens": 1048576,
            "effort": "high",
        },
        "openrouter": {
            "anthropic_base_url": "https://openrouter.ai/api",
            "auth_env": _KEYS["openrouter"],
            "model_id": "moonshotai/kimi-k3[1m]",
            "context_tokens": 1048576,
            "effort": "high",
        },
    }
    mimo = copy.deepcopy(routes)
    mimo["subscription"].update(
        anthropic_base_url=None, auth_env="MIMO_TOKEN_PLAN_API_KEY",
        model_id="mimo-v2.6-pro[1m]", interactive_only=False,
    )
    mimo["api"].update(
        anthropic_base_url="https://api.xiaomimimo.com/anthropic",
        auth_env="MIMO_API_KEY", model_id="mimo-v2.6-pro[1m]",
    )
    mimo["openrouter"]["model_id"] = "xiaomi/mimo-v2.6-pro[1m]"
    return {
        "default": "claude", "models": {"claude": {"native_subscription": True}},
        "gmodel": {"models": {
            "kimi-k3": {"route": "auto", "routes": routes},
            "mimo-v2.6-pro": {"route": "auto", "routes": mimo},
        }},
    }


def _resolve(config, env, *, name="kimi-k3", route=None, interactive=True):
    return gmodel_routes.resolve_route(
        name, route=route, interactive=interactive, roster_data=config, environ=env,
    )


@pytest.mark.parametrize("present", list(itertools.product([False, True], repeat=3)))
def test_auto_billing_order_all_credential_combinations(route_config, present):
    env = {_KEYS[route]: _SECRET for route, exists in zip(_KEYS, present, strict=True) if exists}
    expected = next((route for route, exists in zip(_KEYS, present, strict=True) if exists), None)
    if expected is None:
        with pytest.raises(roster.RosterError):
            _resolve(route_config, env)
    else:
        selected = _resolve(route_config, env)
        assert selected.route == expected
        assert selected.auth_env == _KEYS[expected]
        assert selected.context_tokens == 1048576
        assert _SECRET not in repr(selected)


@pytest.mark.parametrize("explicit", ["subscription", "api", "openrouter"])
def test_explicit_route_overrides_available_cheaper_routes(route_config, explicit):
    route_config["gmodel"]["models"]["kimi-k3"]["route"] = "api"
    env = dict.fromkeys(_KEYS.values(), _SECRET)
    assert _resolve(route_config, env, route=explicit).route == explicit


def test_config_preference_and_cli_auto_override(route_config):
    route_config["gmodel"]["models"]["kimi-k3"]["route"] = "openrouter"
    env = dict.fromkeys(_KEYS.values(), _SECRET)
    assert _resolve(route_config, env).route == "openrouter"
    assert _resolve(route_config, env, route="auto").route == "subscription"


@pytest.mark.parametrize("explicit", ["subscription", "api", "openrouter"])
def test_explicit_missing_key_does_not_bill_other_route(route_config, explicit):
    env = {_KEYS[route]: _SECRET for route in _KEYS if route != explicit}
    with pytest.raises(roster.RosterError):
        _resolve(route_config, env, route=explicit)


@pytest.mark.parametrize("route", ["bad", "", "AUTO"])
def test_invalid_cli_route_is_not_auto(route_config, route):
    with pytest.raises(roster.RosterError):
        _resolve(route_config, dict.fromkeys(_KEYS.values(), _SECRET), route=route)


def test_invalid_saved_preference_fails_instead_of_auto(route_config):
    route_config["gmodel"]["models"]["kimi-k3"]["route"] = "typo"
    with pytest.raises(roster.RosterError):
        _resolve(route_config, dict.fromkeys(_KEYS.values(), _SECRET))


def test_unconfigured_mimo_region_skipped_but_explicit_fails(route_config):
    env = {"MIMO_TOKEN_PLAN_API_KEY": _SECRET, "MIMO_API_KEY": _SECRET}
    assert _resolve(route_config, env, name="mimo-v2.6-pro").route == "api"
    with pytest.raises(roster.RosterError):
        _resolve(route_config, env, name="mimo-v2.6-pro", route="subscription")


def test_configured_mimo_region_subscription_preferred(route_config):
    route_config["gmodel"]["models"]["mimo-v2.6-pro"]["routes"]["subscription"][
        "anthropic_base_url"
    ] = "https://token-plan-sgp.xiaomimimo.com/anthropic"
    env = {"MIMO_TOKEN_PLAN_API_KEY": _SECRET, "API_KEY_OPENROUTER": _SECRET}
    selected = _resolve(route_config, env, name="mimo-v2.6-pro")
    assert selected.route == "subscription"
    assert selected.anthropic_base_url == "https://token-plan-sgp.xiaomimimo.com/anthropic"


def test_kimi_subscription_rejected_for_noninteractive_even_with_key(route_config):
    env = dict.fromkeys(_KEYS.values(), _SECRET)
    assert _resolve(route_config, env, interactive=False).route == "api"
    with pytest.raises(roster.RosterError):
        _resolve(route_config, env, route="subscription", interactive=False)


@pytest.mark.parametrize("field,value", [
    ("auth_mode", "oauth"), ("context_tokens", 0), ("context_tokens", -1),
    ("context_tokens", True), ("context_tokens", "1M"), ("model_id", ["k3"]),
    ("auth_env", ["KEY"]), ("anthropic_base_url", ["https://invalid"]),
])
def test_malformed_route_errors_even_with_fallback_key(route_config, field, value):
    route_config["gmodel"]["models"]["kimi-k3"]["routes"]["subscription"][field] = value
    with pytest.raises(roster.RosterError):
        _resolve(route_config, {"API_KEY_OPENROUTER": _SECRET})


def test_catalog_collision_with_automated_roster_rejected(route_config):
    route_config["models"]["kimi-k3"] = {"auth_env": "OLD_KEY", "model_id": "old"}
    with pytest.raises(roster.RosterError):
        gmodel_routes.catalog(route_config)


def test_legacy_models_and_native_tiers_remain_outside_catalog(route_config):
    for name in ("claude", "opus", "sonnet", "haiku", "default", "glm-5.3"):
        assert _resolve(route_config, {}, name=name) is None


def test_foreground_catalog_never_enters_automated_failover(route_config, monkeypatch):
    for name in _KEYS.values():
        monkeypatch.setenv(name, _SECRET)
    chain = roster.failover_chain("claude", route_config)
    assert chain == []


def test_context_override_256k_is_retained(route_config):
    raw = route_config["gmodel"]["models"]["kimi-k3"]["routes"]["subscription"]
    raw.update(model_id="k3-256k", context_tokens=262144)
    selected = _resolve(route_config, {"KIMI_CODING_API_KEY": _SECRET})
    child = gmodel_routes.apply_route_env({"KIMI_CODING_API_KEY": _SECRET}, selected)
    assert child["ANTHROPIC_MODEL"] == "k3-256k"
    assert child["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert child["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "262144"


@pytest.mark.parametrize("route", ["subscription", "api", "openrouter"])
def test_authentication_isolation_and_all_model_slots(route_config, route):
    selected = _resolve(route_config, dict.fromkeys(_KEYS.values(), _SECRET), route=route)
    child = gmodel_routes.apply_route_env({
        selected.auth_env: _SECRET, "ANTHROPIC_API_KEY": "old-api-secret",
        "ANTHROPIC_AUTH_TOKEN": "old-auth-secret", "CLAUDE_CODE_OAUTH_TOKEN": "old-oauth-secret",
        "ANTHROPIC_BASE_URL": "https://old.invalid", "ANTHROPIC_MODEL": "old-model",
        "CLAUDE_CODE_EFFORT_LEVEL": "low",
    }, selected)
    assert child["ANTHROPIC_BASE_URL"] == selected.anthropic_base_url
    # Every route, the Kimi subscription included, uses the bearer slot; the
    # lower-ranked credential slots are pinned empty rather than trusted to rank.
    assert child["ANTHROPIC_AUTH_TOKEN"] == _SECRET
    assert child["ANTHROPIC_API_KEY"] == ""
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    for variable in roster._ROSTER_MODEL_ENV_VARS:
        assert child[variable] == selected.model_id
    assert child["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert child["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    assert not any(value in child.values() for value in ("old-api-secret", "old-auth-secret", "old-oauth-secret"))


@pytest.fixture
def launcher(monkeypatch, tmp_path, route_config):
    monkeypatch.setattr(os, "environ", {
        "HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "GENESIS_GMODEL_REEXEC": "1",
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gmodel_routes, "settings_dir", lambda: tmp_path / "gmodel-settings")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: False
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(roster, "load_roster", lambda *args, **kwargs: copy.deepcopy(route_config))
    return runpy.run_path(str(_GMODEL))


@pytest.mark.parametrize("route", ["subscription", "api", "openrouter"])
def test_diagnostics_hide_auth_secrets_and_show_selected_route(launcher, monkeypatch, capsys, route):
    monkeypatch.setenv(_KEYS[route], _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", route]) == 0
    captured = capsys.readouterr()
    assert _SECRET not in captured.out + captured.err
    assert route in captured.out + captured.err
    assert "ANTHROPIC_MODEL=" in captured.out


class _ExecCaptured(Exception):
    pass


def _capture_exec(launcher, monkeypatch):
    captured = {}
    def execve(path, args, env):
        captured.update(path=path, args=args, env=env)
        raise _ExecCaptured
    monkeypatch.setattr(launcher["shutil"], "which", lambda _: "/fake/claude")
    monkeypatch.setattr(launcher["os"], "execve", execve)
    return captured


def test_exec_receives_selected_route_and_passthrough_once(launcher, monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--route", "api", "--permission-mode", "plan", "--resume", "old-session"])
    assert captured["args"][:2] == ["/fake/claude", "--settings"]
    assert captured["args"][3:] == ["--permission-mode", "plan", "--resume", "old-session"]
    pinned = json.loads(Path(captured["args"][2]).read_text())
    assert pinned["env"]["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
    assert pinned["env"]["ANTHROPIC_AUTH_TOKEN"] == _SECRET
    assert pinned["env"]["ANTHROPIC_MODEL"] == "kimi-k3[1m]"
    assert stat.S_IMODE(os.stat(captured["args"][2]).st_mode) == 0o600
    assert _SECRET not in " ".join(captured["args"])
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == _SECRET
    assert captured["env"]["ANTHROPIC_MODEL"] == "kimi-k3[1m]"
    assert captured["env"]["GENESIS_ROSTER_MODEL"] == "kimi-k3"
    assert "GENESIS_GMODEL_REEXEC" not in captured["env"]


def test_route_flag_after_delimiter_is_preserved(launcher, monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--", "--route", "not-a-launcher-choice"])
    assert captured["args"][:2] == ["/fake/claude", "--settings"]
    assert captured["args"][3:] == ["--", "--route", "not-a-launcher-choice"]


@pytest.mark.parametrize("args", [["--route"], ["--route", "typo"], ["--route", "api", "--route", "subscription"]])
def test_cli_bad_route_arguments_fail_without_launch(launcher, capsys, args):
    assert launcher["main"](["--print-env", "kimi-k3", *args]) != 0
    assert capsys.readouterr().err


@pytest.mark.parametrize("resume", [["--resume", "session-id"], ["--continue"]])
def test_resume_auto_recomputes_current_available_route(launcher, monkeypatch, capsys, resume):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", *resume]) == 0
    first = capsys.readouterr()
    assert "https://api.moonshot.ai/anthropic" in first.out
    monkeypatch.delenv("MOONSHOT_API_KEY")
    monkeypatch.setenv("API_KEY_OPENROUTER", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", *resume]) == 0
    second = capsys.readouterr()
    assert "https://openrouter.ai/api" in second.out
    assert "fork" in (second.out + second.err).lower()


@pytest.mark.parametrize("print_args", [["-p", "hello"], ["--print", "hello"], ["--bg"], ["--background"]])
def test_headless_cli_skips_kimi_subscription(launcher, monkeypatch, capsys, print_args):
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", *print_args]) == 0
    assert "https://api.moonshot.ai/anthropic" in capsys.readouterr().out


def test_nontty_actual_launch_skips_kimi_subscription(launcher, monkeypatch):
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    monkeypatch.setattr(launcher["sys"].stdin, "isatty", lambda: False)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3"])
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic"


@pytest.mark.parametrize("invalid", ["gmodel: [", "[]", "false", "0"])
def test_invalid_overlay_cannot_silently_reset_billing_preference(route_config, tmp_path, monkeypatch, invalid):
    import yaml

    from genesis import _config_overlay

    base_dir = tmp_path / "config"
    base_dir.mkdir()
    (base_dir / "cc_roster.yaml").write_text(yaml.safe_dump(route_config))
    overlay_dir = tmp_path / "user-config"
    overlay_dir.mkdir()
    (overlay_dir / "cc_roster.local.yaml").write_text(invalid)
    monkeypatch.setattr(roster, "_CONFIG_DIR", base_dir)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: overlay_dir)
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    with pytest.raises(roster.RosterError):
        gmodel_routes.resolve_route("kimi-k3")


@pytest.mark.parametrize(
    "variable",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "DISABLE_AUTO_COMPACT",
    ],
)
def test_user_settings_are_not_read_the_pins_outrank_them(launcher, monkeypatch, variable):
    """User/project settings no longer gate the launch: the --settings layer the
    launcher passes outranks them, including when they are edited mid-session.
    Every pinned variable is present in that layer with the selected value."""
    settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {variable: "user-value"}}))
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--route", "api"])
    pinned = json.loads(Path(captured["args"][2]).read_text())["env"]
    assert pinned[variable] == captured["env"][variable] != "user-value"
    assert settings.read_text() == json.dumps({"env": {variable: "user-value"}})


@pytest.mark.parametrize(
    "arguments",
    [
        ["--model", "opus"],
        ["--model=opus"],
        ["--settings", '{"env":{"ANTHROPIC_AUTH_TOKEN":"injected-secret"}}'],
    ],
)
def test_conflicting_cli_overrides_cannot_switch_route(launcher, monkeypatch, capsys, arguments):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", "api", *arguments]) != 0
    captured = capsys.readouterr()
    assert "injected-secret" not in captured.out + captured.err
    assert _SECRET not in captured.out + captured.err


def test_print_env_writes_no_settings_file(launcher, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", "api"]) == 0
    assert "--settings" in capsys.readouterr().out
    assert not (tmp_path / "gmodel-settings").exists()


@pytest.mark.parametrize(
    "present,expected_skips",
    [
        (
            {"API_KEY_OPENROUTER"},
            ["subscription: missing KIMI_CODING_API_KEY", "api: missing MOONSHOT_API_KEY"],
        ),
        ({"MOONSHOT_API_KEY"}, ["subscription: missing KIMI_CODING_API_KEY"]),
    ],
)
def test_auto_says_why_it_reached_a_per_token_route(
    launcher, monkeypatch, capsys, present, expected_skips
):
    for name in present:
        monkeypatch.setenv(name, _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3"]) == 0
    err = capsys.readouterr().err
    assert "per-token" in err
    for reason in expected_skips:
        assert reason in err


def test_auto_on_subscription_prints_no_skip_explanation(launcher, monkeypatch, capsys):
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    monkeypatch.setenv("API_KEY_OPENROUTER", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3"]) == 0
    err = capsys.readouterr().err
    assert "subscription quota" in err
    assert "because" not in err


def test_routed_resume_cannot_attach_to_a_running_background_session(launcher, monkeypatch, capsys):
    """Claude Code does not attach a resume to a running background session when
    --settings is on the command line (sessions docs, "Resume a running background
    session"); a routed launch always carries it, so the route cannot be bypassed."""
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--resume", "running-session"])
    assert "--settings" in captured["args"]
    assert "not attached" in capsys.readouterr().err


def test_launch_failure_never_selects_another_billing_route(launcher, monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    monkeypatch.setenv("API_KEY_OPENROUTER", "fallback-secret")
    monkeypatch.setattr(launcher["shutil"], "which", lambda _: "/fake/claude")
    attempts = []

    def failed_exec(path, args, env):
        attempts.append(env["ANTHROPIC_BASE_URL"])
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(launcher["os"], "execve", failed_exec)
    try:
        result = launcher["main"](["kimi-k3", "--route", "api"])
    except OSError:
        pass
    else:
        assert result != 0
    assert attempts == ["https://api.moonshot.ai/anthropic"]


def test_print_env_says_its_preview_assumes_an_interactive_terminal(launcher, monkeypatch, capsys):
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    monkeypatch.setattr(launcher["sys"].stdin, "isatty", lambda: False)
    assert launcher["main"](["--print-env", "kimi-k3"]) == 0
    assert "assumes an interactive terminal launch" in capsys.readouterr().out


def test_launch_warns_about_an_effort_cap_in_user_settings(launcher, monkeypatch, capsys):
    settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"maxEffortLevel": "low"}))
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", "api"]) == 0
    assert "caps effort at low" in capsys.readouterr().err


# --- Round-3 launcher grammar: `gmodel <name> [--route R] [claude args...]` ---


def test_route_is_recognised_only_immediately_after_the_name(launcher, monkeypatch, capsys):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route=api"]) == 0
    capsys.readouterr()
    assert launcher["main"](["--print-env", "kimi-k3", "--resume", "session", "--route", "api"]) != 0
    assert "--route" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [
    ["--safe-mode"], ["--safe-mode=1"], ["--model=kimi-k3[1m]"], ["--model", "kimi-k3[1m]"],
    ["--fallback-model=kimi-k3[1m]"], ["--settings=/some/file.json"], ["--bare"], ["--cloud"],
    ["--remote=session"], ["--teleport"], ["--environment=prod"],
    # Whole-token, no arity table: a prompt VALUE that is exactly a refused
    # token is refused too. Loud, and `--` is the documented way past it.
    ["--append-system-prompt", "--model"],
])
def test_refused_tokens_block_the_launch(launcher, monkeypatch, capsys, arguments):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", "api", *arguments]) != 0
    refused = next(a for a in arguments if a != "--append-system-prompt").partition("=")[0]
    assert f"command line: {refused}" in capsys.readouterr().err


@pytest.mark.parametrize("cluster", ["-xp", "-px"])
def test_short_cluster_containing_p_is_headless_and_says_so(launcher, monkeypatch, capsys, cluster):
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", cluster, "hello"]) == 0
    captured = capsys.readouterr()
    assert "https://api.moonshot.ai/anthropic" in captured.out
    assert "subscription: requires personal interactive use" in captured.err


def test_prompt_after_delimiter_is_passed_through_untouched(launcher, monkeypatch, capsys):
    prompt = ["--", "--model", "-p", "explain --model and --safe-mode"]
    monkeypatch.setenv("KIMI_CODING_API_KEY", _SECRET)
    # Not headless: `-p` after `--` is prompt text (--print-env previews a terminal).
    assert launcher["main"](["--print-env", "kimi-k3", *prompt]) == 0
    assert "https://api.kimi.ai/coding/" in capsys.readouterr().out
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--route", "api", *prompt])
    assert captured["args"][3:] == prompt


def test_openrouter_route_announces_model_switch_billing_and_carries_no_hook(launcher, monkeypatch, capsys):
    monkeypatch.setenv("API_KEY_OPENROUTER", _SECRET)
    captured = _capture_exec(launcher, monkeypatch)
    with pytest.raises(_ExecCaptured):
        launcher["main"](["kimi-k3", "--route", "openrouter"])
    err = capsys.readouterr().err
    assert "/model to a Claude ID bills Anthropic per-token through OpenRouter" in err
    assert "hooks" not in json.loads(Path(captured["args"][2]).read_text())


@pytest.mark.parametrize("name,route,key,toggle,downgrade", [
    # Kimi documents the K2.8 substitution for the coding subscription only.
    ("kimi-k3", "subscription", "KIMI_CODING_API_KEY", True, True),
    ("kimi-k3", "api", "MOONSHOT_API_KEY", True, False),
    ("mimo-v2.6-pro", "api", "MIMO_API_KEY", False, False),
])
def test_kimi_routes_announce_the_thinking_toggle(launcher, monkeypatch, capsys, name, route, key,
                                                  toggle, downgrade):
    monkeypatch.setenv(key, _SECRET)
    assert launcher["main"](["--print-env", name, "--route", route]) == 0
    err = capsys.readouterr().err
    assert ("Alt+T turns thinking off" in err) is toggle
    assert ("K2.8 Preview" in err) is downgrade


def test_overridden_flag_is_announced_at_launch(launcher, monkeypatch, capsys):
    monkeypatch.setenv("MOONSHOT_API_KEY", _SECRET)
    assert launcher["main"](["--print-env", "kimi-k3", "--route", "api", "--effort", "low"]) == 0
    assert "--effort is overridden by this route's pinned settings" in capsys.readouterr().err
