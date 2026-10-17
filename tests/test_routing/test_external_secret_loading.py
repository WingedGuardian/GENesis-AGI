"""Real dotenv parsing must retain external identity/targets through retries."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from genesis.routing import standalone


@pytest.fixture(autouse=True)
def isolated_secret_policy(monkeypatch):
    from genesis.eval import reflection_golden_set as golden
    from scripts.codex_external_mcp import _SESSION_CONTEXT

    monkeypatch.setattr(standalone, "_external_secret_blocked_keys", None, raising=False)
    for marker in {*_SESSION_CONTEXT, *golden._GENESIS_TO_LITELLM, *golden._GENESIS_TO_LITELLM.values()}:
        monkeypatch.delenv(marker, raising=False)


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
    assert "SYNTHETIC_CACHE" not in os.environ


@pytest.mark.parametrize("profile", ["external", "validator", "interactive"])
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize("has_file", [False, True])
def test_native_external_entry_scrubs_inherited_identity_before_bootstrap(
    monkeypatch, tmp_path, profile, transport, has_file,
):
    from scripts import genesis_mcp_server as server
    from scripts.codex_external_mcp import _SESSION_CONTEXT

    selected = tmp_path / "native.env"
    if has_file:
        selected.write_text("\n".join(f"{key}=file-identity" for key in _SESSION_CONTEXT))
    for key in _SESSION_CONTEXT:
        monkeypatch.setenv(key, "inherited-identity")
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "15000")
    monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
    monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", lambda: None)
    entered = []

    def bootstrap(options, **kwargs):
        assert not set(_SESSION_CONTEXT).intersection(os.environ)
        assert os.environ["GENESIS_REPO_ROOT"] == str(tmp_path)
        assert kwargs == {"external_profile": profile}
        assert options["transport"] == transport
        entered.append(True)

    monkeypatch.setitem(server._BOOTSTRAPPERS, "health", bootstrap)
    server.main(["--server", "health", "--external-client", profile,
                 "--transport", transport, "--auth-token", "synthetic-http-auth"])
    assert entered == [True]


@pytest.mark.parametrize("entrypoint", [False, True])
def test_external_file_loading_admits_required_keys_and_rejects_unrelated_credentials(
    monkeypatch, tmp_path, entrypoint,
):
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS
    from scripts import genesis_mcp_server as server

    required = {
        "API_KEY_TINYFISH", "FIRECRAWL_API_KEY", "API_KEY_TAVILY", "API_KEY_EXA",
        "API_KEY_PERPLEXITY", "API_KEY_PAGEINDEX", "API_KEY_CLOUDFLARE", "CLOUDFLARE_ACCOUNT_ID",
        "LM_STUDIO_URL", "GENESIS_ENABLE_LM_STUDIO", "LM_STUDIO_HEALTH_URL",
    "API_KEY_VOYAGE", "GENESIS_MEMORY_RERANK_OFF", "GENESIS_MEMORY_WRITEBACKS_OFF",
    "GENESIS_RECALL_RERANK_GATE_OFF", "GENESIS_RECALL_RERANK_RPM",
    "GENESIS_ENTITY_ADJUDICATION_DISABLED", "GENESIS_DREAM_SHIELD_DISABLED",
    "GENESIS_FALKORDB_STORE_DISABLED", "GENESIS_MEMORY_INTEGRITY_DISABLED",
    "GENESIS_TOP_TAGS_DISABLED", "GENESIS_ENTITY_ALIASES", "GENESIS_DAILY_BUDGET_DISABLED",
    "GENESIS_SQLITE_SLOW_MS", "GENESIS_SKILL_EVOLUTION_GATE_OFF", "GENESIS_OUTPUT_DIR",
    "GENESIS_PLANS_DIR", "USER_TIMEZONE", "GENESIS_GITHUB_USER", "GENESIS_GITHUB_PUBLIC_REPO",
    "GENESIS_VNC_PASSWORD", "GENESIS_BUILD_LANE_ENABLED", "GENESIS_MODELS_MD_SYNTHESIS_OFF",
    }
    for service in ("DEEPINFRA", "QWEN", "MISTRAL", "GROQ", "GOOGLE", "OPENROUTER",
                    "NVIDIA_NIM", "GITHUB", "DEEPSEEK", "OPENAI", "XAI", "ZENMUX",
                    "MINIMAX", "GLM", "CEREBRAS", "SAMBANOVA", "OLLAMA", "LMSTUDIO"):
        required.update((f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN"))
    excluded = {"GH_TOKEN", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "GENESIS_BACKUP_REPO",
                "UNRELATED_API_KEY", "DEPLOY_TOKEN", "GENESIS_SESSION_ID", "DISCORD_BOT_TOKEN"}
    for key in required | excluded:
        monkeypatch.delenv(key, raising=False)
    selected = tmp_path / "positive.env"
    selected.write_text("\n".join(f"{key}=synthetic-value" for key in required | excluded))
    monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
    if entrypoint:
        server._load_mcp_environment("interactive")
    else:
        standalone.configure_external_secret_loading(EXTERNAL_SECRET_BLOCKED_KEYS)
    for _ in range(2):
        assert standalone.load_external_secrets()
        assert all(os.environ.get(key) == "synthetic-value" for key in required)
        assert not excluded.intersection(os.environ)


@pytest.mark.parametrize("profile", ["external", "validator", "interactive"])
@pytest.mark.parametrize("route", ["startup", "lazy", "eval"])
def test_external_loaders_retain_actual_safety_and_procedure_consumers(monkeypatch, tmp_path, profile, route):
    import runpy

    from genesis.autonomy import contributor_worklog_config as worklog
    from genesis.board import config as board
    from genesis.contribution.sanitize import scan_prose
    from genesis.eval import reflection_golden_set as golden
    from genesis.learning.procedural import trigger_cache
    from genesis.observability import mcp_staleness_guard_config as staleness
    from scripts import genesis_mcp_server as server

    marker = "synthetic-private-control-label"
    fingerprints = tmp_path / "synthetic-fingerprints.txt"
    fingerprints.write_text(marker + "\n")
    triggers = tmp_path / "synthetic-triggers.yaml"
    values = {
        "GENESIS_CONTRIBUTOR_WORKLOG_DISABLED": "1", "GENESIS_BOARD_DISABLED": "1",
        "GENESIS_MCP_STALENESS_GUARD": "1", "GENESIS_RELEASE_FINGERPRINTS": str(fingerprints),
        "GENESIS_PROCEDURE_TRIGGERS": str(triggers),
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    selected = tmp_path / "safety.env"
    selected.write_text("\n".join(f"{name}={value}" for name, value in values.items()))
    monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
    monkeypatch.setattr(worklog, "load_config", lambda: {"enabled": True, "mode": "live"})
    monkeypatch.setattr(board, "load_config", lambda: {"enabled": True, "mode": "live"})
    monkeypatch.setattr(staleness, "load_config", lambda: {"enabled": True, "mode": "block"})
    monkeypatch.setattr(golden, "_secrets_loaded", False)
    if route == "startup":
        server._load_mcp_environment(profile)
    else:
        monkeypatch.setattr("genesis.env.secrets_path", lambda: tmp_path / "missing.env")
        server._load_mcp_environment(profile)
        monkeypatch.setattr("genesis.env.secrets_path", lambda: selected)
        if route == "lazy":
            assert standalone.load_external_secrets()
        else:
            golden._ensure_secrets()
    assert worklog.effective_mode() == board.effective_mode() == staleness.effective_mode() == "off"
    scanned = scan_prose(marker)
    assert not scanned.ok
    assert any(f.scanner == "fingerprint" and f.detail == marker for f in scanned.findings)
    # Evaluate the real import-time selector in a fresh namespace without
    # mutating the shared module or writing a trigger cache.
    namespace = runpy.run_path(trigger_cache.__file__)
    assert namespace["get_cache_path"]() == triggers


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("selected_exists", [False, True])
@pytest.mark.parametrize("explicit_path", [False, True])
def test_experiment_secret_loader_retains_external_policy_and_ordinary_control(
    monkeypatch, tmp_path, external, selected_exists, explicit_path,
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
    if explicit_path:
        monkeypatch.setenv("SECRETS_PATH", str(selected))
    else:
        monkeypatch.delenv("SECRETS_PATH", raising=False)
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
        source = "selected" if selected_exists else (None if explicit_path else "legacy")
        assert os.environ.get("GENESIS_SESSION_ID") == (f"{source}-session" if source else None)
        assert os.environ.get("DEEPSEEK_API_KEY") == (f"{source}-provider" if source else None)


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
