"""Real dotenv semantics and positive dependency populations for external MCP."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def policy(monkeypatch, tmp_path):
    from genesis.routing import standalone

    monkeypatch.setattr(standalone, "_external_secret_keys", None)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    return standalone, tmp_path / "selected.env"


@pytest.mark.parametrize("inherited", [None, "", "inherited"])
@pytest.mark.parametrize("raw,parsed", [
    ("literal", "literal"), ("${SOURCE}", "process-source"),
    ("${UNSET_FIXTURE:-fallback}", "fallback"), ("", ""), (None, None),
])
def test_real_parser_precedence_empty_and_none(policy, monkeypatch, inherited, raw, parsed):
    standalone, path = policy
    monkeypatch.delenv("UNSET_FIXTURE", raising=False)
    monkeypatch.delenv("TARGET", raising=False)
    monkeypatch.setenv("SOURCE", "process-source")
    if inherited is not None:
        monkeypatch.setenv("TARGET", inherited)
    path.write_text("SOURCE=file-source\n" + ("TARGET\n" if raw is None else f"TARGET={raw}\n"))
    standalone.configure_external_secrets(frozenset({"TARGET"}))
    assert standalone.load_external_secrets(path) == {"SOURCE", "TARGET"}
    assert os.environ.get("TARGET") == (inherited if inherited is not None else parsed)
    assert os.environ["SOURCE"] == "process-source"


@pytest.mark.parametrize("disabled", ["1", "true", "TRUE", "t", "yes", "Y"])
def test_disabled_external_loader_never_parses(policy, monkeypatch, disabled):
    standalone, path = policy
    path.write_text("TARGET=fixture\n")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", disabled)
    monkeypatch.delenv("TARGET", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled external loader parsed the file")

    monkeypatch.setattr("dotenv.main.DotEnv", forbidden)
    standalone.configure_external_secrets(frozenset({"TARGET"}))
    assert standalone.load_external_secrets(path) == frozenset()
    assert "TARGET" not in os.environ


def test_identity_and_process_selectors_are_never_file_imported(policy, monkeypatch):
    from genesis.caller_context import EXTERNAL_IDENTITY_ENV_VARS

    standalone, path = policy
    selectors = {"GENESIS_REPO_ROOT", "GENESIS_HOME", "SECRETS_PATH"}
    for key in EXTERNAL_IDENTITY_ENV_VARS | selectors:
        monkeypatch.setenv(key, "process")
    path.write_text("".join(f"{key}=file\n" for key in EXTERNAL_IDENTITY_ENV_VARS | selectors))
    standalone.configure_external_secrets(EXTERNAL_IDENTITY_ENV_VARS | selectors)
    assert not EXTERNAL_IDENTITY_ENV_VARS.intersection(os.environ)
    standalone.load_external_secrets(path)
    assert not EXTERNAL_IDENTITY_ENV_VARS.intersection(os.environ)
    assert all(os.environ[key] == "process" for key in selectors)


def test_policy_cannot_change_after_configuration(policy):
    standalone, _ = policy
    standalone.configure_external_secrets(frozenset({"TARGET"}))
    standalone.configure_external_secrets(frozenset({"TARGET"}))
    with pytest.raises(RuntimeError, match="already configured"):
        standalone.configure_external_secrets(frozenset({"OTHER"}))


def test_missing_empty_and_malformed_file_keep_dependency_behavior(policy, monkeypatch, caplog):
    standalone, path = policy
    monkeypatch.delenv("TARGET", raising=False)
    standalone.configure_external_secrets(frozenset({"TARGET"}))
    assert standalone.load_external_secrets(path) == frozenset()
    path.touch()
    assert standalone.load_external_secrets(path) == frozenset()
    path.write_text("BAD='synthetic-private-marker\nTARGET=accepted\n")
    assert standalone.load_external_secrets(path) == {"TARGET"}
    assert os.environ["TARGET"] == "accepted"
    assert "synthetic-private-marker" not in caplog.text


def test_declared_memory_keys_follow_real_configured_provider_resolver(monkeypatch):
    from pathlib import Path

    from genesis.routing.config import load_config
    from genesis.routing.litellm_delegate import api_key_env_names
    from scripts.genesis_mcp_server import _external_file_keys

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("genesis.env.repo_root", lambda: root)
    config = load_config(root / "config/model_routing.yaml", check_api_keys=False)
    providers = {name for provider in config.providers.values() for name in api_key_env_names(provider.provider_type)}
    common = {"GENESIS_DB_PATH", "GENESIS_DB_BUSY_TIMEOUT_MS", "GENESIS_MCP_HTTP_TOKEN", "GENESIS_MCP_ENV_RECORDER"}
    assert _external_file_keys("health") == common
    keys = _external_file_keys("memory")
    assert providers <= keys
    assert common <= keys
    assert {"GENESIS_SESSION_READ_POOL_SIZE", "GENESIS_RECALL_READ_POOL_SIZE", "API_KEY_VOYAGE",
            "GENESIS_MEMORY_WRITEBACKS_OFF", "GENESIS_MEMORY_RERANK_OFF", "GENESIS_FALKORDB_STORE_DISABLED",
            "LM_STUDIO_URL", "LM_STUDIO_HEALTH_URL", "API_KEY_TINYFISH", "API_KEY_BRAVE",
            "SEARXNG_URL", "BRAVE_API_URL"} <= keys
    assert not {"GENESIS_REPO_ROOT", "GENESIS_HOME", "SECRETS_PATH", "DISCORD_BOT_TOKEN", "FIRECRAWL_API_KEY"} & keys


@pytest.mark.parametrize("disabled", [False, True])
def test_ordinary_startup_and_router_keep_their_distinct_semantics(policy, monkeypatch, disabled):
    from unittest.mock import Mock

    import scripts.genesis_mcp_server as server

    standalone, path = policy
    monkeypatch.setattr(server, "_MCP_CRASH_DIR", path.parent / "crashes")
    monkeypatch.setattr("genesis.env.secrets_path", lambda: path)
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
    monkeypatch.setattr("genesis.observability.mcp_env_reads.install", Mock())
    monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", Mock())
    monkeypatch.setattr("genesis.routing.config.load_config", Mock(side_effect=RuntimeError("fixture")))
    monkeypatch.setenv("GENESIS_DB_PATH", "process-target")
    for key in ("GENESIS_MCP_HTTP_TOKEN", "API_KEY_DEEPINFRA", "API_KEY_QWEN", "UNRELATED_FIXTURE"):
        monkeypatch.delenv(key, raising=False)
    if disabled:
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    path.write_text(
        "GENESIS_DB_PATH=file-target\nGENESIS_MCP_HTTP_TOKEN=${GENESIS_DB_PATH}\n"
        "API_KEY_DEEPINFRA=\nAPI_KEY_QWEN\nUNRELATED_FIXTURE=file-value\n"
    )
    seen = []

    def bootstrap(*args, **kwargs):
        # Ordinary startup ignores disable, filters keys, skips empty/bare values,
        # and preserves inherited targets despite file-first interpolation.
        assert os.environ["GENESIS_DB_PATH"] == "process-target"
        assert os.environ["GENESIS_MCP_HTTP_TOKEN"] == "file-target"
        assert not {"API_KEY_DEEPINFRA", "API_KEY_QWEN", "UNRELATED_FIXTURE"} & os.environ.keys()
        assert standalone._build_standalone_router() is None
        assert os.environ["GENESIS_DB_PATH"] == ("process-target" if disabled else "file-target")
        assert os.environ.get("UNRELATED_FIXTURE") == (None if disabled else "file-value")
        assert os.environ.get("API_KEY_DEEPINFRA") == (None if disabled else "")
        assert "API_KEY_QWEN" not in os.environ
        seen.append(True)

    monkeypatch.setitem(server._BOOTSTRAPPERS, "health", bootstrap)
    server.main(["--server", "health"])
    assert seen == [True]


def test_corrective_recall_retries_the_same_external_policy(policy, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from genesis.memory.corrective import _resolve_router

    standalone, path = policy
    monkeypatch.setenv("GENESIS_DB_PATH", "process-target")
    monkeypatch.delenv("UNRELATED_FIXTURE", raising=False)
    monkeypatch.setattr("genesis.env.secrets_path", lambda: path)
    monkeypatch.setattr("genesis.runtime._core.GenesisRuntime.instance", lambda: SimpleNamespace(_router=None))
    fail = Mock(side_effect=RuntimeError("fixture-construction-failure"))
    monkeypatch.setattr("genesis.routing.config.load_config", fail)
    path.write_text("GENESIS_DB_PATH=file-target\nUNRELATED_FIXTURE=unwanted\n")
    standalone.configure_external_secrets(frozenset({"GENESIS_DB_PATH"}))
    for _ in range(2):
        assert _resolve_router() is None
        assert os.environ["GENESIS_DB_PATH"] == "process-target"
        assert "UNRELATED_FIXTURE" not in os.environ
    assert fail.call_count == 2


def test_declared_dependency_covers_the_private_dotenv_adapter():
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement

    root = Path(__file__).resolve().parents[2]
    requirements = [Requirement(value) for value in tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]]
    matches = [requirement for requirement in requirements if requirement.name == "python-dotenv"]
    assert len(matches) == 1
    assert "1.2.0" in matches[0].specifier
    assert "1.1.1" not in matches[0].specifier
