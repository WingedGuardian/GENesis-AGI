from types import SimpleNamespace

import genesis.env
import genesis.routing.config
import scripts.genesis_mcp_server as server
from genesis.routing.litellm_delegate import api_key_env_names


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
