"""Shared environment and path resolution for Genesis runtime.

Centralizes machine-specific defaults so runtime code does not hardcode one
developer's home directory, LAN topology, or venv layout.

Configuration precedence (highest to lowest):
  1. Environment variable (e.g. OLLAMA_URL)
  2. ~/.genesis/config/genesis.yaml  (local install config)
  3. Hardcoded default (safe for a fresh clone)
"""

from __future__ import annotations

import logging
import os
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
