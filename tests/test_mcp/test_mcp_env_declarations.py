import os
from types import SimpleNamespace

import pytest

import genesis.env
import genesis.routing.config
import scripts.genesis_mcp_server as server
from genesis.routing.litellm_delegate import api_key_env_names


@pytest.mark.parametrize("kind", sorted(server._VALID_SERVERS))
@pytest.mark.parametrize("file_value,inherited", [(None, None), ("0", None), (None, "0"), ("1", "0"), ("0", "1")])
def test_recorder_control_is_loaded_before_installation(monkeypatch, tmp_path, kind, file_value, inherited):
    from genesis.observability import mcp_env_reads

    mcp_env_reads.uninstall()
    monkeypatch.delenv("GENESIS_MCP_ENV_RECORDER", raising=False)
    if inherited is not None:
        monkeypatch.setenv("GENESIS_MCP_ENV_RECORDER", inherited)
    secret = tmp_path / "secrets.env"
    secret.write_text("" if file_value is None else f"GENESIS_MCP_ENV_RECORDER={file_value}\n")
    monkeypatch.setenv("SECRETS_PATH", str(secret))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "_standalone_routing_env_names", lambda kind: frozenset())
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: False)
    monkeypatch.setattr(server, "_run_disabled_stub", lambda kwargs: None)
    expected = inherited if inherited is not None else file_value
    try:
        server.main(["--server", kind])
        assert (mcp_env_reads._original_environ_class is not None) == (expected != "0")
        assert os.environ.get("GENESIS_MCP_ENV_RECORDER") == expected
    finally:
        mcp_env_reads.uninstall()


def test_server_declaration_maps_cover_exactly_the_valid_servers(monkeypatch, tmp_path) -> None:
    expected_servers = {"health", "memory", "outreach", "recon", "discord-bot"}
    assert expected_servers == server._VALID_SERVERS
    assert set(server._SERVER_DECLARED_VARS) == expected_servers
    assert set(server._SERVER_DECLARED_PREFIXES) == expected_servers

    assert server._SERVER_DECLARED_VARS["health"] == frozenset(
        {
            "FIRECRAWL_API_KEY",
            "API_KEY_TINYFISH",
            "API_KEY_TAVILY",
            "API_KEY_EXA",
            "API_KEY_PERPLEXITY",
            "API_KEY_BRAVE",
            "GENESIS_BACKUP_REPO",
            "GENESIS_VNC_PASSWORD",
        }
    )
    assert server._SERVER_DECLARED_VARS["memory"] == frozenset({"API_KEY_VOYAGE"})
    assert server._SERVER_DECLARED_VARS["outreach"] == frozenset(
        {"DISCORD_WEBHOOK_URL", "OUTREACH_RECIPIENT_DISCORD"}
    )
    assert server._SERVER_DECLARED_VARS["recon"] == frozenset()
    assert server._SERVER_DECLARED_VARS["discord-bot"] == frozenset()
    assert server._SERVER_DECLARED_PREFIXES == {
        "health": (),
        "memory": (),
        "outreach": ("DISCORD_WEBHOOK_",),
        "recon": (),
        "discord-bot": (),
    }

    calls = []

    def load_config(path, *, check_api_keys):
        calls.append((path, check_api_keys))
        return SimpleNamespace(
            providers={
                "one": SimpleNamespace(provider_type="openai"),
                "two": SimpleNamespace(provider_type="mistral"),
            }
        )

    monkeypatch.setattr(genesis.env, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(genesis.routing.config, "load_config", load_config)
    expected_routing_names = frozenset(
        name for provider in ("openai", "mistral") for name in api_key_env_names(provider)
    )
    assert server._standalone_routing_env_names("health") == expected_routing_names
    assert calls == [(tmp_path / "config" / "model_routing.yaml", False)]
    assert server._standalone_routing_env_names("discord-bot") == frozenset()

    def failed_config(*args, **kwargs):
        raise OSError("config unavailable")

    monkeypatch.setattr(genesis.routing.config, "load_config", failed_config)
    assert server._standalone_routing_env_names("recon") == frozenset()
