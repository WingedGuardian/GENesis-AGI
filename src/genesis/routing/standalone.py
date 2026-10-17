"""Standalone router bootstrap for MCP child processes.

MCP servers run as CC child processes without a fully bootstrapped
GenesisRuntime.  This module provides a lightweight router that can
make LLM calls via litellm without requiring a database connection,
event bus, or resilience state machine.

The full router in the genesis-server process is unaffected -- if
rt._router is already set, create_standalone_router() is a no-op.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from genesis.routing.types import BudgetStatus

if TYPE_CHECKING:
    from genesis.routing.router import Router


# Ordinary native MCP loading retains its historical population. External
# startup and every lazy/eval retry share a positive superset, never a suffix
# wildcard that would import unrelated deployment or publication credentials.
MCP_SECRET_KEYS = frozenset({
    # Infrastructure
    "OLLAMA_URL", "QDRANT_URL", "GENESIS_DB_PATH", "GENESIS_CC_PROJECT_ID",
    # Browser (CDP remote backend)
    "GENESIS_CDP_URL",
    # Embedding providers (required for memory MCP)
    "API_KEY_DEEPINFRA", "API_KEY_QWEN",
    # LLM providers (used by recon/outreach MCP tools)
    "GOOGLE_API_KEY", "API_KEY_GROQ", "API_KEY_MISTRAL", "API_KEY_OPENROUTER",
    "API_KEY_DEEPSEEK",
    # Ollama config
    "GENESIS_ENABLE_OLLAMA", "OLLAMA_EMBEDDING_MODEL",
    # Embedding chain levers read when the memory child builds its chains
    # (env.embed_local_first / env.embed_priority_tier). Absent here, a value
    # set only in secrets.env is dropped and the child disagrees with the
    # main runtime about the order and the recall tier.
    "GENESIS_EMBED_LOCAL_FIRST", "GENESIS_EMBED_PRIORITY_TIER",
    # HTTP transport auth
    "GENESIS_MCP_HTTP_TOKEN",
    # Discord bot (used by discord-bot MCP server)
    "DISCORD_BOT_TOKEN",
    # SQLite busy_timeout override (must pass the allowlist or a
    # secrets.env-set value would be silently dropped before the
    # setdefault below)
    "GENESIS_DB_BUSY_TIMEOUT_MS",
    # Recall read-pool knobs — _bootstrap_memory honors the same kill
    # switch + size as the server runtime, so a secrets.env-configured
    # value must reach the child too (Codex P2 on #1302)
    # GENESIS_SESSION_READ_POOL_SIZE is the one _bootstrap_memory actually
    # reads now (this process is a per-session child, not the server) —
    # without it here the documented lever is INERT, which is the third
    # instance of this class in this list after #1302 and #1587.
    "GENESIS_RECALL_READ_POOL_OFF",
    "GENESIS_RECALL_READ_POOL_SIZE",
    "GENESIS_SESSION_READ_POOL_SIZE",
    # deliberate() (Model Fusion) timeout budget — the health MCP server hosts the
    # `deliberate` tool, so a secrets.env-set override must reach this child or the
    # documented knob is inert and every call silently stays at the 1000s default
    # (Codex P2 on #1587).
    "GENESIS_DELIBERATE_TIMEOUT_S",
    # Graph-traversal telemetry kill switch (memory/graph_telemetry.py): the
    # memory server makes most traversals, so a secrets.env setting that never
    # reached it would leave the documented switch inert where it matters.
    "GENESIS_GRAPH_TELEMETRY_DISABLED",
})

EXTERNAL_SECRET_KEYS = (MCP_SECRET_KEYS - {"DISCORD_BOT_TOKEN"}) | frozenset({
    "API_KEY_TINYFISH", "FIRECRAWL_API_KEY", "API_KEY_TAVILY", "API_KEY_EXA",
    "API_KEY_PERPLEXITY", "API_KEY_PAGEINDEX", "API_KEY_CLOUDFLARE",
    "CLOUDFLARE_ACCOUNT_ID", "LM_STUDIO_URL", "GENESIS_ENABLE_LM_STUDIO",
    "LM_STUDIO_HEALTH_URL",
    "API_KEY_VOYAGE", "GENESIS_MEMORY_RERANK_OFF", "GENESIS_MEMORY_WRITEBACKS_OFF",
    "GENESIS_RECALL_RERANK_GATE_OFF", "GENESIS_RECALL_RERANK_RPM",
    "GENESIS_ENTITY_ADJUDICATION_DISABLED", "GENESIS_DREAM_SHIELD_DISABLED",
    "GENESIS_FALKORDB_STORE_DISABLED", "GENESIS_MEMORY_INTEGRITY_DISABLED",
    "GENESIS_TOP_TAGS_DISABLED", "GENESIS_ENTITY_ALIASES", "GENESIS_DAILY_BUDGET_DISABLED",
    "GENESIS_SQLITE_SLOW_MS", "GENESIS_SKILL_EVOLUTION_GATE_OFF", "GENESIS_OUTPUT_DIR",
    "GENESIS_PLANS_DIR", "USER_TIMEZONE", "GENESIS_GITHUB_USER", "GENESIS_GITHUB_PUBLIC_REPO",
    "GENESIS_VNC_PASSWORD", "GENESIS_BUILD_LANE_ENABLED", "GENESIS_MODELS_MD_SYNTHESIS_OFF",
    "GENESIS_CONTRIBUTOR_WORKLOG_DISABLED", "GENESIS_BOARD_DISABLED",
    "GENESIS_RELEASE_FINGERPRINTS", "GENESIS_MCP_STALENESS_GUARD", "GENESIS_PROCEDURE_TRIGGERS",
}) | frozenset(
    key
    for service in (
        "DEEPINFRA", "QWEN", "MISTRAL", "GROQ", "GOOGLE", "OPENROUTER",
        "NVIDIA_NIM", "GITHUB", "DEEPSEEK", "OPENAI", "XAI", "ZENMUX",
        "MINIMAX", "GLM", "CEREBRAS", "SAMBANOVA", "OLLAMA", "LMSTUDIO",
    )
    for key in (f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN")
)

logger = logging.getLogger(__name__)
_external_secret_blocked_keys: frozenset[str] | None = None


def configure_external_secret_loading(blocked_keys: frozenset[str]) -> None:
    """Set startup/lazy-retry policy once for this external MCP process."""
    global _external_secret_blocked_keys
    _external_secret_blocked_keys = blocked_keys


def _load_standalone_secrets(path: Path) -> None:
    """External launch targets win; ordinary clients retain dotenv overrides."""
    from dotenv import load_dotenv
    from dotenv.main import DotEnv

    if _external_secret_blocked_keys is None:
        load_dotenv(str(path), override=True)
    else:
        # dotenv_values hardcodes override=True during interpolation, which
        # would derive new targets/keys from file values we deliberately retain
        # from the environment. Use the same precedence for expansion and load.
        for key, value in DotEnv(path, override=False).dict().items():
            if key in EXTERNAL_SECRET_KEYS and key not in _external_secret_blocked_keys and value is not None:
                os.environ.setdefault(key, value)


def load_external_secrets() -> bool:
    """Load the selected external file; return False for ordinary callers."""
    if _external_secret_blocked_keys is None:
        return False
    from genesis.env import secrets_path

    path = secrets_path()
    if path.is_file():
        _load_standalone_secrets(path)
    return True


class NullCostTracker:
    """Drop-in for CostTracker that accepts calls without DB writes.

    The Router accesses ``self.cost_tracker.db`` for neural monitor
    recording.  Setting ``db = None`` causes those code paths to
    skip gracefully (Router guards with ``if self.cost_tracker.db``).
    """

    db = None

    async def check_budget(self, *, task_id: str | None = None) -> BudgetStatus:
        return BudgetStatus.UNDER_LIMIT

    async def record(
        self,
        call_site_id: str,
        provider: str,
        result: Any,  # CallResult at runtime
        *,
        cost_known: bool = True,
    ) -> None:
        pass


def _build_standalone_router() -> Router | None:
    """Construct a lightweight, DB-free ``Router`` with real provider access.

    Loads secrets + routing config from disk and wires a Router with no
    DB-dependent components (read-only breakers, null cost tracker, no event
    bus / dead-letter). Returns the ``Router``, or ``None`` if construction
    fails (missing config/secrets) — never raises.

    Loads provider environment, but does not set the runtime singleton. Callers
    wanting the process-wide singleton use
    ``create_standalone_router()``; callers that just need a router instance
    (e.g. the offline attention runner's ``--l15`` path) call this directly and
    own the returned object.
    """
    try:
        from genesis.env import repo_root, secrets_path
        from genesis.routing.circuit_breaker import CircuitBreakerRegistry
        from genesis.routing.config import load_config
        from genesis.routing.degradation import DegradationTracker
        from genesis.routing.litellm_delegate import LiteLLMDelegate
        from genesis.routing.router import Router

        # 1. Load API keys into environment
        sp = secrets_path()
        if sp.is_file():
            _load_standalone_secrets(sp)

        # 2. Load routing config
        config_path = repo_root() / "config" / "model_routing.yaml"
        config = load_config(config_path, check_api_keys=False)

        # 3. Construct router-lite
        delegate = LiteLLMDelegate(config)
        # Read-only: MCP child processes load the server's breaker state but must
        # not write the shared state file (only the server owns it) — WS-3c.
        breakers = CircuitBreakerRegistry(config.providers, persist=False)
        degradation = DegradationTracker(resilience_state=None)
        cost_tracker = NullCostTracker()

        from genesis.routing.daily_budget import DailyBudgetLedger

        return Router(
            config=config,
            breakers=breakers,
            cost_tracker=cost_tracker,
            degradation=degradation,
            delegate=delegate,
            event_bus=None,
            dead_letter=None,
            # Read-only, like the breakers above: MCP children load a
            # SNAPSHOT of the server's daily-budget counters at process
            # start (a provider exhausted later in the day is still called
            # from long-lived children) and never write the shared state
            # file — WS-3c. Their own usage also goes uncounted. Both are
            # the undercount side, backstopped by the provider's 429s.
            daily_budget=DailyBudgetLedger(persist=False),
        )
    except Exception:
        logger.warning(
            "Failed to build standalone router -- "
            "LLM-dependent tools will be unavailable",
            exc_info=True,
        )
        return None


def create_standalone_router() -> None:
    """Bootstrap a lightweight router on the GenesisRuntime singleton.

    Safe to call multiple times -- skips if a router is already set. Delegates
    construction to ``_build_standalone_router()`` and assigns the result to the
    singleton; on build failure the singleton's ``_router`` stays ``None``.
    """
    from genesis.runtime._core import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt._router is not None:
        return

    router = _build_standalone_router()
    if router is not None:
        rt._router = router
        logger.info("Standalone router bootstrapped for MCP server")
