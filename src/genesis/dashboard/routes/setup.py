"""Dashboard first-run setup routes — onboarding-wizard status + live key test.

Both routes are ADDITIVE and never change auth behavior. ``setup-status`` reads
PERSISTED state (the ``secrets.env`` file, the identity files, and the
``~/.genesis/setup-complete`` marker) rather than ``os.environ`` — a key or
password the user just saved through the wizard is reflected immediately, before
the server restart that reloads the process environment.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.env import secrets_path

logger = logging.getLogger(__name__)

# Same derivation as routes/config.py (sibling module): parents[4] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_IDENTITY_DIR = _REPO_ROOT / "src" / "genesis" / "identity"
_SETUP_COMPLETE_MARKER = Path.home() / ".genesis" / "setup-complete"

# A configured value for any of these counts as "an LLM key is present" for the
# wizard's key step. Kept broad on purpose — the wizard only needs to know that
# SOME usable chat provider is configured, not which one.
# Canonical env-var names as defined in secrets.env.example (the dashboard secrets
# registry only accepts these). Note the provider-suffix forms for Anthropic /
# Google / OpenAI — NOT API_KEY_* — must match exactly or a fresh install that
# already configured them reads as "missing".
_LLM_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "API_KEY_OPENROUTER",
    "API_KEY_GROQ",
    "API_KEY_DEEPSEEK",
    "API_KEY_MISTRAL",
    "GOOGLE_API_KEY",
    "API_KEY_ZENMUX",
)
_EMBEDDING_KEY_NAMES = (
    "API_KEY_VOYAGE",
    "API_KEY_DEEPINFRA",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
)

_UNSET_SENTINELS = ("", "None", "NA")


def _persisted_secrets() -> dict[str, str]:
    """Read ``secrets.env`` from disk into a dict WITHOUT mutating ``os.environ``.

    ``dotenv_values`` parses the file directly, so a just-saved key/password is
    visible immediately (``os.environ`` stays stale until the next restart).
    """
    try:
        from dotenv import dotenv_values

        path = secrets_path()
        if not path.is_file():
            return {}
        return {k: (v or "") for k, v in dotenv_values(path).items()}
    except Exception:  # noqa: BLE001 - status must never 500 the dashboard
        logger.warning("setup-status: could not read secrets.env", exc_info=True)
        return {}


def _has_any(secrets: dict[str, str], names: tuple[str, ...]) -> bool:
    return any(secrets.get(n, "").strip() not in _UNSET_SENTINELS for n in names)


@blueprint.route("/api/genesis/setup-status")
def setup_status():
    """First-run wizard state, derived from PERSISTED signals (never os.environ).

    Read-only; no auth change. Drives which onboarding steps show as done on
    initial load. Returns booleans only — never any secret value.
    """
    secrets = _persisted_secrets()

    # Identity considered "set" when USER.md exists and differs from its shipped
    # example seed (USER.md is gitignored / per-install, seeded from .example).
    identity_set = False
    try:
        user_md = _IDENTITY_DIR / "USER.md"
        if user_md.is_file():
            content = user_md.read_text().strip()
            example_path = _IDENTITY_DIR / "USER.md.example"
            example = example_path.read_text().strip() if example_path.is_file() else ""
            identity_set = bool(content) and content != example
    except OSError:
        logger.warning("setup-status: could not read USER.md", exc_info=True)

    return jsonify(
        {
            "onboarded": _SETUP_COMPLETE_MARKER.is_file(),
            "password_set": secrets.get("DASHBOARD_PASSWORD", "").strip() not in _UNSET_SENTINELS,
            "llm_key_present": _has_any(secrets, _LLM_KEY_NAMES),
            "embedding_key_present": _has_any(secrets, _EMBEDDING_KEY_NAMES),
            "identity_set": identity_set,
        }
    )


@blueprint.route("/api/genesis/keys/test", methods=["POST"])
@_async_route
async def keys_test():
    """Live-test an API key VALUE the user just entered (real HTTP call).

    Body: ``{"provider_type": "anthropic", "key": "sk-..."}``. Tests the passed
    value directly (NOT ``os.environ``), so the wizard verifies a key before or
    right after saving it — no server restart needed. The validation URL is fixed
    per provider type (``build_key_validator``); no caller-supplied base URL is
    honored, so the endpoint cannot be turned into an SSRF probe.
    """
    from genesis.observability.snapshots.api_keys import test_single_key

    data = request.get_json(silent=True) or {}
    provider_type = str(data.get("provider_type", "")).strip().lower()
    key = str(data.get("key", "")).strip()

    if not provider_type or not key:
        return jsonify({"valid": False, "error": "provider_type and key are required"}), 400

    result = await test_single_key(provider_type, key)
    return jsonify(result)


@blueprint.route("/api/genesis/setup-complete", methods=["POST"])
def setup_complete():
    """Write the ``~/.genesis/setup-complete`` marker — the wizard's final step.

    Idempotent. This is the SAME marker the terminal onboarding skill writes at
    its end: it stops the Setup card from reappearing, and clears the two other
    first-run behaviors keyed on it (the CC foreground onboarding prompt and the
    ego-cadence first-run suppression). Without this, completing the wizard would
    leave the install perpetually "not onboarded".
    """
    from datetime import UTC, datetime

    try:
        _SETUP_COMPLETE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        if not _SETUP_COMPLETE_MARKER.exists():
            _SETUP_COMPLETE_MARKER.write_text(datetime.now(UTC).isoformat() + "\n")
    except OSError as exc:
        logger.error("Failed to write setup-complete marker: %s", exc)
        return jsonify({"error": "could not write marker"}), 500
    return jsonify({"status": "ok", "onboarded": True})
