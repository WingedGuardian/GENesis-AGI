"""External startup and lazy router retries share one process policy."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from genesis.caller_context import IDENTITY_ENV_VARS


@pytest.fixture
def entry(monkeypatch, tmp_path):
    import scripts.genesis_mcp_server as server
    from genesis.routing import standalone

    path = tmp_path / "selected.env"
    monkeypatch.setattr(server, "_MCP_CRASH_DIR", tmp_path / "crashes")
    monkeypatch.setattr("genesis.env.secrets_path", lambda: path)
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
    monkeypatch.setattr("genesis.observability.mcp_env_reads.install", Mock())
    monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", Mock())
    monkeypatch.setattr(standalone, "_external_secret_keys", None, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    for key in ("GENESIS_DB_PATH", "QDRANT_URL", "UNRELATED_FIXTURE", "GENESIS_RECALL_READ_POOL_SIZE"):
        monkeypatch.delenv(key, raising=False)
    return server, standalone, path


@pytest.mark.parametrize("profile", ["external", "validator"])
@pytest.mark.parametrize("server_name", ["health", "memory"])
def test_entry_and_router_retry_preserve_external_boundary(entry, monkeypatch, profile, server_name):
    server, standalone, path = entry
    markers = IDENTITY_ENV_VARS | {"GENESIS_SLOT", "GENESIS_TRACE_ID", "GENESIS_PARENT_SPAN_ID"}
    for key in markers:
        monkeypatch.setenv(key, "inherited-fixture")
    selected_root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("GENESIS_REPO_ROOT", selected_root)
    monkeypatch.setenv("GENESIS_DB_PATH", "inherited-target")
    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "")
    path.write_text(
        "GENESIS_DB_PATH=file-target\nGENESIS_DB_BUSY_TIMEOUT_MS=file-value\n"
        "GENESIS_MCP_HTTP_TOKEN=${GENESIS_DB_PATH}/${GENESIS_DB_BUSY_TIMEOUT_MS}\n"
        "GENESIS_REPO_ROOT=/unwanted-runtime\nUNRELATED_FIXTURE=unwanted\n"
        "GENESIS_RECALL_READ_POOL_SIZE=3\n"
        + "".join(f"{key}=file-fixture\n" for key in markers)
    )
    monkeypatch.delenv("GENESIS_MCP_HTTP_TOKEN", raising=False)
    seen = []

    def check():
        assert os.environ["GENESIS_DB_PATH"] == "inherited-target"
        assert os.environ["GENESIS_REPO_ROOT"] == selected_root
        assert os.environ["GENESIS_MCP_HTTP_TOKEN"] == "inherited-target/"
        assert not markers.intersection(os.environ)
        assert "UNRELATED_FIXTURE" not in os.environ
        assert (os.environ.get("GENESIS_RECALL_READ_POOL_SIZE") == "3") == (server_name == "memory")
        seen.append(True)

    def bootstrap(*args, **kwargs):
        check()
        # Fail after the real loader; the next construction must retain policy.
        monkeypatch.setattr("genesis.routing.config.load_config", Mock(side_effect=RuntimeError("fixture")))
        for _ in range(2):
            assert standalone._build_standalone_router() is None
            check()

    monkeypatch.setitem(server._BOOTSTRAPPERS, server_name, bootstrap)
    server.main(["--server", server_name, "--external-client", profile])
    assert len(seen) == 3


def test_wrapper_clears_every_canonical_identity(monkeypatch):
    import scripts.codex_external_mcp as wrapper

    monkeypatch.setattr(wrapper, "runtime_root", lambda: "/selected-runtime")
    clean = wrapper.sanitized_environment(dict.fromkeys(IDENTITY_ENV_VARS, "fixture"))
    assert not IDENTITY_ENV_VARS.intersection(clean)


def test_initial_discovery_failure_cannot_freeze_an_incomplete_policy(entry, monkeypatch):
    from genesis.routing.config import load_config

    server, standalone, path = entry
    monkeypatch.setattr("genesis.routing.config.load_config", Mock(side_effect=OSError("synthetic-private-marker")))
    bootstrap = Mock()
    monkeypatch.setitem(server._BOOTSTRAPPERS, "memory", bootstrap)
    with pytest.raises(ValueError, match="^external dependency keys unavailable$"):
        server.main(["--server", "memory", "--external-client"])
    assert standalone._external_secret_keys is None
    assert not bootstrap.called
    # Ordinary read-recorder discovery stays best effort.
    assert server._standalone_routing_env_names("memory") == frozenset()
    monkeypatch.setattr("genesis.routing.config.load_config", load_config)
    server.main(["--server", "memory", "--external-client"])
    assert bootstrap.call_count == 1
    assert standalone._external_secret_keys


def test_file_enabled_configured_provider_keeps_its_credential(entry, monkeypatch):
    from pathlib import Path

    server, standalone, path = entry
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("genesis.env.repo_root", lambda: root)
    monkeypatch.delenv("GENESIS_ENABLE_LM_STUDIO", raising=False)
    monkeypatch.delenv("API_KEY_LMSTUDIO", raising=False)
    path.write_text("GENESIS_ENABLE_LM_STUDIO=true\nAPI_KEY_LMSTUDIO=synthetic-fixture\n")
    seen = []

    def bootstrap(*args, **kwargs):
        from genesis.routing.config import load_config

        config = load_config(root / "config/model_routing.yaml", check_api_keys=False)
        assert any(p.provider_type == "lmstudio" for p in config.providers.values())
        assert os.environ.get("API_KEY_LMSTUDIO") == "synthetic-fixture"
        seen.append(True)

    monkeypatch.setitem(server._BOOTSTRAPPERS, "memory", bootstrap)
    server.main(["--server", "memory", "--external-client"])
    assert seen == [True]
