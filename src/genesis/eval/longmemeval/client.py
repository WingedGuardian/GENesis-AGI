"""OpenRouter client factory + secret loading for the LongMemEval harness.

Reader (answer) and judge both call the SAME fixed model via OpenRouter's
OpenAI-compatible endpoint, so the metric is reproducible and INDEPENDENT of
Genesis's cognitive routing. OpenRouter proxies to the real
``gpt-4o-2024-08-06`` weights, preserving comparability to the published
numbers. No fallback chain: if the pinned model is unreachable the run fails
loudly rather than silently grading with a different model (which would make
the number lie).
"""

from __future__ import annotations

import os
from pathlib import Path

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "API_KEY_OPENROUTER"
#: Fixed reader + judge model (OpenRouter slug for gpt-4o-2024-08-06).
DEFAULT_MODEL = "openai/gpt-4o-2024-08-06"

_secrets_loaded = False


def _candidate_secret_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        from genesis.env import secrets_path

        paths.append(secrets_path())
    except Exception:  # noqa: BLE001 - env resolution is best-effort here
        pass
    # Worktree execution: repo_root() may resolve to the worktree (no
    # secrets.env). Fall back to the canonical main-tree location.
    paths.append(Path.home() / "genesis" / "secrets.env")
    return paths


def load_secrets(*, force: bool = False) -> None:
    """Load KEY=VALUE lines from secrets.env into ``os.environ`` (no overwrite)."""
    global _secrets_loaded  # noqa: PLW0603
    if _secrets_loaded and not force:
        return
    for path in _candidate_secret_paths():
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break
    _secrets_loaded = True


def openrouter_api_key() -> str:
    """Resolve the OpenRouter API key, loading secrets.env on demand."""
    key = os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        load_secrets()
        key = os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        msg = (
            f"{OPENROUTER_KEY_ENV} not found in env or secrets.env. The judge "
            "requires OpenRouter access to gpt-4o for a comparable number."
        )
        raise RuntimeError(msg)
    return key


def build_client(api_key: str | None = None):
    """Build an OpenAI-compatible client pointed at OpenRouter."""
    from openai import OpenAI

    return OpenAI(api_key=api_key or openrouter_api_key(), base_url=OPENROUTER_BASE_URL)
