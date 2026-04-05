"""Shared environment and path resolution for Genesis runtime.

Centralizes machine-specific defaults so runtime code does not hardcode one
developer's home directory, LAN topology, or venv layout.
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
    project identifier.  E.g. ${HOME}/genesis → -home-ubuntu-genesis.
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
    return os.environ.get("OLLAMA_URL", _DEFAULT_OLLAMA_URL).strip()


def ollama_tags_url() -> str:
    return _join_url(ollama_url(), "/api/tags")


def ollama_embed_url() -> str:
    return _join_url(ollama_url(), "/api/embed")


def lm_studio_url() -> str:
    return os.environ.get("LM_STUDIO_URL", _DEFAULT_LM_STUDIO_URL).strip()


def lm_studio_health_url() -> str:
    return os.environ.get("LM_STUDIO_HEALTH_URL", _join_url(lm_studio_url(), "/models")).strip()


def ollama_enabled() -> bool:
    """Check if Ollama local inference is enabled.

    Defaults to False (cloud-primary architecture). Set GENESIS_ENABLE_OLLAMA=true
    in secrets.env for environments with a local Ollama instance.
    """
    value = os.environ.get("GENESIS_ENABLE_OLLAMA")
    if value is None:
        return False
    return value.strip().lower() not in {"0", "false", "no", "off"}


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
