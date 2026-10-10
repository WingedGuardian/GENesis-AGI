"""Real dotenv parsing must retain external identity/targets through retries."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.routing import standalone


@pytest.fixture(autouse=True)
def isolated_secret_policy(monkeypatch):
    monkeypatch.setattr(standalone, "_external_secret_blocked_keys", None, raising=False)


@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize("provider", ["chosen-provider", ""])
def test_external_secret_reload_preserves_identity_and_explicit_targets(monkeypatch, tmp_path, attempts, provider):
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS
    from scripts.codex_external_mcp import _SESSION_CONTEXT

    assert frozenset((*_SESSION_CONTEXT, "GENESIS_REPO_ROOT")) == EXTERNAL_SECRET_BLOCKED_KEYS
    secrets = tmp_path / "synthetic.env"
    targets = {"GENESIS_REPO_ROOT", "GENESIS_DB_PATH", "GENESIS_HOME", "SECRETS_PATH", "QDRANT_URL", "HOME"}
    for name in _SESSION_CONTEXT:
        monkeypatch.delenv(name, raising=False)
    for name in targets:
        monkeypatch.setenv(name, f"chosen-{name}")
    monkeypatch.setenv("API_KEY_TINYFISH", provider)
    monkeypatch.delenv("API_KEY_PAGEINDEX", raising=False)
    secrets.write_text("\n".join(
        f"{name}=file-{name}" for name in targets | set(_SESSION_CONTEXT)
    ) + "\nAPI_KEY_TINYFISH=file-provider\nAPI_KEY_PAGEINDEX=synthetic-document-provider\n")  # pragma: allowlist secret — synthetic fixture values
    monkeypatch.setattr("genesis.env.secrets_path", lambda: secrets)
    monkeypatch.setattr("genesis.env.repo_root", lambda: tmp_path)
    # Fail after the REAL secret loader; a later lazy call must obey the same policy.
    config = MagicMock(side_effect=ValueError("synthetic routing unavailable"))
    monkeypatch.setattr("genesis.routing.config.load_config", config)
    standalone.configure_external_secret_loading(EXTERNAL_SECRET_BLOCKED_KEYS)
    for _ in range(attempts):
        assert standalone._build_standalone_router() is None
        for name in _SESSION_CONTEXT:
            assert name not in os.environ
        for name in targets:
            assert os.environ[name] == f"chosen-{name}"
        assert os.environ["API_KEY_TINYFISH"] == provider
        assert os.environ["API_KEY_PAGEINDEX"] == "synthetic-document-provider"  # pragma: allowlist secret — synthetic fixture value
    assert config.call_count == attempts


def test_ordinary_secret_loading_retains_existing_override_semantics(monkeypatch, tmp_path):
    secrets = tmp_path / "synthetic.env"
    secrets.write_text("API_KEY_TINYFISH=ordinary-file-provider\n")  # pragma: allowlist secret — synthetic fixture value
    monkeypatch.setenv("API_KEY_TINYFISH", "inherited-provider")
    monkeypatch.setattr("genesis.env.secrets_path", lambda: secrets)
    monkeypatch.setattr("genesis.env.repo_root", lambda: tmp_path)
    monkeypatch.setattr("genesis.routing.config.load_config", MagicMock(side_effect=ValueError("fixture")))
    assert standalone._build_standalone_router() is None
    assert os.environ["API_KEY_TINYFISH"] == "ordinary-file-provider"  # pragma: allowlist secret — synthetic fixture value


@pytest.mark.parametrize("entrypoint", [False, True])
def test_external_interpolation_uses_retained_environment(monkeypatch, tmp_path, entrypoint):
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS
    from scripts import genesis_mcp_server as server

    secrets = tmp_path / "interpolated.env"
    secrets.write_text("GENESIS_HOME=/file-home\nQDRANT_URL=http://file-provider\n"
                       "API_KEY_QWEN=${QDRANT_URL}/provider\nSYNTHETIC_CACHE=${GENESIS_HOME}/cache\n")
    monkeypatch.setenv("GENESIS_HOME", "/chosen-home")
    monkeypatch.setenv("QDRANT_URL", "http://chosen-provider")
    monkeypatch.delenv("API_KEY_QWEN", raising=False)
    monkeypatch.delenv("SYNTHETIC_CACHE", raising=False)
    monkeypatch.setattr("genesis.env.secrets_path", lambda: secrets)
    if entrypoint:
        monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
        monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", lambda: None)
        monkeypatch.setitem(server._BOOTSTRAPPERS, "health", lambda *a, **kw: None)
        monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "15000")
        server.main(["--server", "health", "--external-client", "interactive"])
    else:
        standalone.configure_external_secret_loading(EXTERNAL_SECRET_BLOCKED_KEYS)
        standalone._load_standalone_secrets(secrets)
    assert os.environ["API_KEY_QWEN"] == "http://chosen-provider/provider"  # pragma: allowlist secret — synthetic fixture value
    assert os.environ["SYNTHETIC_CACHE"] == "/chosen-home/cache"


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("selected_exists", [False, True])
def test_experiment_secret_loader_retains_external_policy_and_ordinary_control(
    monkeypatch, tmp_path, external, selected_exists,
):
    from genesis.eval import reflection_golden_set as golden
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS

    legacy = tmp_path / "genesis"
    legacy.mkdir()
    (legacy / "secrets.env").write_text("GENESIS_SESSION_ID=legacy-session\nAPI_KEY_DEEPSEEK=legacy-provider\n")  # pragma: allowlist secret — synthetic fixture values
    selected = tmp_path / "selected.env"
    if selected_exists:
        selected.write_text("GENESIS_SESSION_ID=selected-session\nAPI_KEY_DEEPSEEK=selected-provider\n")
    monkeypatch.setattr(golden.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
    monkeypatch.setattr(golden, "_secrets_loaded", False)
    monkeypatch.delenv("GENESIS_SESSION_ID", raising=False)
    monkeypatch.delenv("API_KEY_DEEPSEEK", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    if external:
        standalone.configure_external_secret_loading(EXTERNAL_SECRET_BLOCKED_KEYS)
    golden._ensure_secrets()
    golden._ensure_secrets()  # The existing idempotency contract is retained.
    if external:
        assert "GENESIS_SESSION_ID" not in os.environ
        assert os.environ.get("API_KEY_DEEPSEEK") == ("selected-provider" if selected_exists else None)
        assert os.environ.get("DEEPSEEK_API_KEY") == ("selected-provider" if selected_exists else None)
    else:
        assert os.environ["GENESIS_SESSION_ID"] == "legacy-session"
        assert os.environ["DEEPSEEK_API_KEY"] == "legacy-provider"  # pragma: allowlist secret — synthetic fixture value


@pytest.mark.parametrize("family", ["experiment", "evo", "skill_replay"])
def test_admitted_experiment_router_paths_keep_external_context(monkeypatch, tmp_path, family):
    from genesis.eval import reflection_golden_set as golden
    from genesis.experimentation import standalone_router as routing
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS

    legacy = tmp_path / "genesis"
    legacy.mkdir()
    (legacy / "secrets.env").write_text("GENESIS_SESSION_ID=legacy-session\n")
    selected = tmp_path / "selected.env"
    selected.write_text("GENESIS_SESSION_ORIGIN=legacy-origin\n")
    monkeypatch.setattr(golden.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
    monkeypatch.setattr(golden, "_secrets_loaded", False)
    monkeypatch.delenv("GENESIS_SESSION_ID", raising=False)
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
    config = SimpleNamespace(providers={"groq-free": SimpleNamespace(model_id="synthetic/model")})
    delegate = MagicMock()
    monkeypatch.setattr(routing, "load_config", lambda *a, **kw: config)
    monkeypatch.setattr(routing, "LiteLLMDelegate", lambda *a, **kw: delegate)
    monkeypatch.setattr(routing, "default_judge_chain", lambda *a, **kw: ["groq-free"])
    standalone.configure_external_secret_loading(EXTERNAL_SECRET_BLOCKED_KEYS)
    if family == "experiment":
        from genesis.experimentation import runner

        runner.StandaloneLiteLLMRouter("groq-free", config=config, delegate=delegate)
    elif family == "evo":
        from genesis.mcp.health.evo_run import _make_router

        _make_router("groq-free", config, delegate)
    else:
        from genesis.eval.skill_replay.runner import _build_judge_router

        _build_judge_router("groq-free")
    assert "GENESIS_SESSION_ID" not in os.environ
    assert "GENESIS_SESSION_ORIGIN" not in os.environ
    delegate.call.assert_not_called()
