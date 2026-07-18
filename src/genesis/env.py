"""Shared environment and path resolution for Genesis runtime.

Centralizes machine-specific defaults so runtime code does not hardcode one
developer's home directory, LAN topology, or venv layout.

Configuration precedence (highest to lowest):
  1. Environment variable (e.g. OLLAMA_URL)
  2. ~/.genesis/config/genesis.yaml  (local install config)
  3. Hardcoded default (safe for a fresh clone)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_QDRANT_URL = "http://localhost:6333"
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_DEFAULT_LM_STUDIO_URL = "http://localhost:1234/v1"

# ---------------------------------------------------------------------------
# Local config overlay — ~/.genesis/config/genesis.yaml
# ---------------------------------------------------------------------------

_LOCAL_CONFIG: dict | None = None
_LOCAL_CONFIG_LOADED: bool = False


def _invalidate_local_config() -> None:
    """Clear the cached local config so next access re-reads from disk."""
    global _LOCAL_CONFIG, _LOCAL_CONFIG_LOADED
    _LOCAL_CONFIG = None
    _LOCAL_CONFIG_LOADED = False


def _local_config() -> dict:
    """Load ~/.genesis/config/genesis.yaml (cached after first call).

    Returns an empty dict if the file is absent or unreadable — all callers
    must fall through to their hardcoded defaults gracefully.
    """
    global _LOCAL_CONFIG, _LOCAL_CONFIG_LOADED
    if _LOCAL_CONFIG_LOADED:
        return _LOCAL_CONFIG or {}
    _LOCAL_CONFIG_LOADED = True
    cfg_path = Path.home() / ".genesis" / "config" / "genesis.yaml"
    if not cfg_path.is_file():
        _LOCAL_CONFIG = {}
        return {}
    try:
        import yaml  # noqa: PLC0415 — lazy import, yaml is always available

        with cfg_path.open() as fh:
            _LOCAL_CONFIG = yaml.safe_load(fh) or {}
    except Exception:
        logger.warning("Failed to load local config from %s", cfg_path, exc_info=True)
        _LOCAL_CONFIG = {}
    return _LOCAL_CONFIG


def repo_root() -> Path:
    value = os.environ.get("GENESIS_REPO_ROOT")
    return Path(value).expanduser() if value else _REPO_ROOT


def venv_path() -> Path:
    """Resolve the Python venv used by Genesis services and MCP servers."""
    value = os.environ.get("VENV_PATH")
    if value:
        return Path(value).expanduser()
    return repo_root() / ".venv"


def secrets_path() -> Path:
    value = os.environ.get("SECRETS_PATH")
    if value:
        resolved = Path(value).expanduser()
        logger.debug("secrets_path: SECRETS_PATH override → %s", resolved)
        return resolved
    genesis_path = repo_root() / "secrets.env"
    logger.debug("secrets_path: genesis repo → %s", genesis_path)
    return genesis_path


def genesis_db_path() -> Path:
    value = os.environ.get("GENESIS_DB_PATH")
    if value:
        return Path(value).expanduser()
    return repo_root() / "data" / "genesis.db"


def genesis_home() -> Path:
    """Resolve the Genesis runtime home (~/.genesis): output, sessions, config."""
    value = os.environ.get("GENESIS_HOME")
    return Path(value).expanduser() if value else Path.home() / ".genesis"


def memory_writebacks_off() -> bool:
    """True when retrieval write-backs (retrieved_count / last_retrieved_at
    bumps on recall) must be suppressed.

    Recall is read-mostly, not read-only: it mutates usage-tracking payloads in
    Qdrant and SQLite on every hit. That's correct in production (activation
    scoring reflects real usage) but wrong for evaluation harnesses reading a
    frozen memory snapshot — the eval bench (``genesis eval bench``) sets
    GENESIS_MEMORY_WRITEBACKS_OFF=1 in its MCP-server env so Genesis-arm
    recalls neither pollute the production Qdrant payloads (GENESIS_DB_PATH
    redirects only SQLite; Qdrant is shared) nor let earlier bench tasks
    re-rank memories for later ones. Default off: production unaffected.
    """
    return os.environ.get("GENESIS_MEMORY_WRITEBACKS_OFF", "").strip() in (
        "1",
        "true",
        "yes",
    )


def memory_rerank_off() -> bool:
    """True when Voyage cross-encoder reranking on the MCP recall tools must be
    suppressed (kill switch).

    memory_recall / knowledge_recall / reference_lookup rerank by default once
    the retriever has a reranker. This env kill (plus the ``reranker`` mode in
    ``config/memory_recall.yaml``) lets an operator turn that tool-path rerank
    off — for a Voyage cost/latency/outage concern — without a restart or code
    change. Default off: reranking stays on. Does NOT gate the internal runtime
    context stack (its reranking predates this switch); unset ``API_KEY_VOYAGE``
    for a full stop.
    """
    return os.environ.get("GENESIS_MEMORY_RERANK_OFF", "").strip() in (
        "1",
        "true",
        "yes",
    )


# A real deploy completes in minutes (update.sh's health-check phase caps at
# ~3 min). A state file whose start is older than this cutoff is a crashed or
# abandoned deploy, not a live one — treating it as stale bounds the (rare)
# PID-reuse window in which a leftover state file could otherwise suppress the
# watchdog's restart guard indefinitely.
_UPDATE_STALE_AFTER_S = 4 * 3600  # 4 hours


def _deploy_state_is_recent(state: dict) -> bool:
    """False if update_state.json's ``started_at`` is older than the stale cutoff.

    Absent/unparseable timestamp → True (fall back to PID liveness alone; never
    let a formatting quirk be the thing that suppresses the watchdog).
    """
    started_at = state.get("started_at")
    if not started_at:
        return True
    try:
        started = datetime.fromisoformat(started_at)
    except (ValueError, TypeError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (datetime.now(UTC) - started).total_seconds() < _UPDATE_STALE_AFTER_S


def update_in_progress() -> bool:
    """True while a Genesis self-update (deploy) is actively running.

    Read by the autonomy watchdog to DEFER restarting genesis-server during a
    deploy. ``update.sh`` intentionally stops the server for its
    merge/bootstrap/migrate window; a mid-deploy revival takes the DB write lock
    and deadlocks bootstrap's procedure seed (incident IR-2). Two independent
    deploy signals are honored — either one alive → in progress:

    * ``~/.genesis/update_in_progress.pid`` — a bare-integer PID. Written by the
      dashboard-orchestrated update path (``dashboard/routes/updates.py``) and by
      ``scripts/restore.sh`` while it holds the server stopped to rebuild the DB
      (so the watchdog does not revive it into a half-built database). A CLI
      ``./scripts/update.sh`` run does not write it — it uses the state file
      below. Any writer is honored: only liveness is checked, never identity.
    * ``~/.genesis/update_state.json`` — ``{phase, pid, started_at, ...}`` written
      per-phase by ``update.sh::_write_state`` (the CLI path; the incident path).
      Counts only while ``phase != "done"`` (``done`` is written immediately
      before the file is removed) and ``started_at`` is recent.

    A signal counts only if its PID is > 1 (an ``AsyncMock().pid`` is 1) AND
    still alive (``os.kill(pid, 0)``). Any dead / absent / corrupt / ``done`` /
    expired signal is treated as "no deploy", so a stale file can never
    permanently disable the watchdog. This check is defensive by contract: it
    NEVER raises into the caller (the watchdog restart path).
    """
    try:
        home = genesis_home()

        # Dashboard path: bare-int PID file (dashboard-only; absent for CLI runs).
        pid_file = home / "update_in_progress.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if pid > 1:
                    os.kill(pid, 0)
                    return True
            except (ProcessLookupError, ValueError, OSError):
                pass  # dead / invalid PID — not an active deploy

        # CLI path: update.sh state file with phase + owning PID + start time.
        state_file = home / "update_state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
                state = None
            if (
                isinstance(state, dict)
                and state.get("phase") != "done"
                and _deploy_state_is_recent(state)
            ):
                pid = state.get("pid")
                if isinstance(pid, int) and pid > 1:
                    try:
                        os.kill(pid, 0)
                        return True
                    except (ProcessLookupError, OSError):
                        pass  # owning process gone — stale state file

        return False
    except Exception:  # never raise into the watchdog loop — fail open to "no deploy"
        logger.warning(
            "update_in_progress() check failed — assuming no deploy in progress",
            exc_info=True,
        )
        return False


def claude_home() -> Path:
    """Resolve the Claude Code home (~/.claude): plans, skills, projects."""
    value = os.environ.get("CLAUDE_HOME")
    return Path(value).expanduser() if value else Path.home() / ".claude"


def plans_dir() -> Path:
    """Working plan/roadmap docs (~/.claude/plans)."""
    value = os.environ.get("GENESIS_PLANS_DIR")
    return Path(value).expanduser() if value else claude_home() / "plans"


def output_dir() -> Path:
    """Genesis report/spec/content output (~/.genesis/output)."""
    value = os.environ.get("GENESIS_OUTPUT_DIR")
    return Path(value).expanduser() if value else genesis_home() / "output"


def cc_project_dir() -> str:
    """Claude Code project directory name, derived from repo root path.

    CC uses the absolute working directory path with / replaced by - as the
    project identifier.  E.g. ``/path/to/repo`` → ``-path-to-repo``.
    """
    override = os.environ.get("GENESIS_CC_PROJECT_ID")
    if override:
        return override
    return str(repo_root()).replace("/", "-")


def qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", _DEFAULT_QDRANT_URL).strip()


def qdrant_health_url() -> str:
    return _join_url(qdrant_url(), "/healthz")


def qdrant_collections_url() -> str:
    return _join_url(qdrant_url(), "/collections")


def ollama_url() -> str:
    env_val = os.environ.get("OLLAMA_URL")
    if env_val:
        return env_val.strip()
    local_val = _local_config().get("network", {}).get("ollama_url")
    if local_val:
        return str(local_val).strip()
    return _DEFAULT_OLLAMA_URL


def ollama_tags_url() -> str:
    return _join_url(ollama_url(), "/api/tags")


def ollama_embed_url() -> str:
    return _join_url(ollama_url(), "/api/embed")


def lm_studio_url() -> str:
    env_val = os.environ.get("LM_STUDIO_URL")
    if env_val:
        return env_val.strip()
    local_val = _local_config().get("network", {}).get("lm_studio_url")
    if local_val:
        return str(local_val).strip()
    return _DEFAULT_LM_STUDIO_URL


def lm_studio_health_url() -> str:
    return os.environ.get("LM_STUDIO_HEALTH_URL", _join_url(lm_studio_url(), "/models")).strip()


def ollama_enabled() -> bool:
    """Check if Ollama local inference is enabled.

    Defaults to False (cloud-primary architecture). Set GENESIS_ENABLE_OLLAMA=true
    in secrets.env or network.ollama_enabled in ~/.genesis/config/genesis.yaml.
    """
    env_val = os.environ.get("GENESIS_ENABLE_OLLAMA")
    if env_val is not None:
        return env_val.strip().lower() not in {"0", "false", "no", "off"}
    local_val = _local_config().get("network", {}).get("ollama_enabled")
    if local_val is not None:
        return bool(local_val)
    return False


def build_lane_enabled() -> bool:
    """Check if the autonomous capability-build lane is active.

    Defaults to False — the lane ships dark. When enabled, a ``build``
    verdict on a capability-notepad drop produces a one-tap greenlight
    card whose approval dispatches an autonomous build to a draft PR
    (never a merge). Set GENESIS_BUILD_LANE_ENABLED=true in secrets.env
    or build_lane.enabled in ~/.genesis/config/genesis.yaml. A flag flip
    requires a server restart to take effect (the poll loop is only
    spawned when enabled).
    """
    env_val = os.environ.get("GENESIS_BUILD_LANE_ENABLED")
    if env_val is not None:
        return env_val.strip().lower() not in {"0", "false", "no", "off"}
    local_val = _local_config().get("build_lane", {}).get("enabled")
    if local_val is not None:
        return bool(local_val)
    return False


def user_timezone() -> str:
    """User's local timezone (IANA format).

    Precedence: USER_TIMEZONE env var → local config timezone → UTC.
    Used by tz.py and any subsystem that formats timestamps for display.
    """
    env_val = os.environ.get("USER_TIMEZONE")
    if env_val:
        return env_val.strip()
    local_val = _local_config().get("timezone")
    if local_val:
        return str(local_val).strip()
    return "UTC"


def github_user() -> str:
    """GitHub username for this Genesis install.

    Precedence: GENESIS_GITHUB_USER env var → local config → empty string.
    """
    env_val = os.environ.get("GENESIS_GITHUB_USER")
    if env_val:
        return env_val.strip()
    local_val = _local_config().get("github", {}).get("user")
    if local_val:
        return str(local_val).strip()
    return ""


def github_public_repo() -> str:
    """Public GitHub repo name (without owner prefix).

    Precedence: GENESIS_GITHUB_PUBLIC_REPO env var → local config → "GENesis-AGI".
    """
    env_val = os.environ.get("GENESIS_GITHUB_PUBLIC_REPO")
    if env_val:
        return env_val.strip()
    local_val = _local_config().get("github", {}).get("public_repo")
    if local_val:
        return str(local_val).strip()
    return "GENesis-AGI"


def deepinfra_api_key() -> str | None:
    return os.environ.get("API_KEY_DEEPINFRA", "").strip() or None


def dashscope_api_key() -> str | None:
    return os.environ.get("API_KEY_QWEN", "").strip() or None


def _join_url(base: str, path: str) -> str:
    parsed = urlsplit(base)
    if not parsed.scheme:
        return base.rstrip("/") + path
    joined_path = parsed.path.rstrip("/") + path
    return urlunsplit((parsed.scheme, parsed.netloc, joined_path, "", ""))
