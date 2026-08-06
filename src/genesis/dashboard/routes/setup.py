"""Dashboard first-run setup routes — onboarding-wizard status + live key test.

Both routes are ADDITIVE and never change auth behavior. ``setup-status`` reads
PERSISTED state (the ``secrets.env`` file and the identity files) plus the LIVE
functional floor (``genesis.onboarding.floor``) rather than ``os.environ`` — a key
or password the user just saved through the wizard is reflected immediately, before
the server restart that reloads the process environment.

Note: the wizard does NOT write the ``~/.genesis/setup-complete`` marker. That
marker means "bootstrap finished" and is owned by ``scripts/bootstrap.sh`` (and the
terminal onboarding skill). Whether the install is *functional* is a separate, live
signal — ``floor_met`` — computed here from the same helper the ego cadence gate and
the CC session-start prompt use, so the definition can't drift between surfaces.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.onboarding.floor import UNSET_SENTINELS, compute_floor, read_persisted_secrets
from genesis.onboarding.readiness import compute_readiness

logger = logging.getLogger(__name__)

# Same derivation as routes/config.py (sibling module): parents[4] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_IDENTITY_DIR = _REPO_ROOT / "src" / "genesis" / "identity"
_SETUP_COMPLETE_MARKER = Path.home() / ".genesis" / "setup-complete"


@blueprint.route("/api/genesis/setup-status")
def setup_status():
    """First-run wizard state, derived from PERSISTED signals (never os.environ).

    Read-only; no auth change. Drives which onboarding steps show as done on
    initial load. Returns booleans only — never any secret value.

    ``floor_met`` (and its three legs ``cc_oauth`` / ``llm_key_present`` /
    ``embedding_key_present``) is the LIVE "is this Genesis functional" signal;
    ``onboarded`` is the separate "bootstrap finished" marker, kept for display.

    Also emits the cumulative readiness tier ABOVE the floor
    (``genesis.onboarding.readiness``): ``tier`` (0..3) + ``tier_name`` plus the
    two gates the floor does not cover — ``telegram_configured`` (T2, proactive
    reach) and ``ego_enabled`` (T3). All additive; existing fields unchanged.
    """
    # Read secrets.env once; reuse for both the floor and the password check.
    secrets = read_persisted_secrets()
    floor = compute_floor(secrets=secrets)
    secrets_have_password = secrets.get("DASHBOARD_PASSWORD", "").strip() not in UNSET_SENTINELS

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

    # Ego-loop enable state gates T3 (the user-controlled driver of autonomous
    # behaviour). Resolved fail-safe: this route drives first-run and must NEVER
    # 500 — an unreadable ego config degrades to "ego off" (worst case shows T2
    # instead of T3), never a crash. Lazy import (mirrors routes/ego.py) so the
    # ego module is not pulled at blueprint import time.
    try:
        from genesis.ego.config import load_ego_config

        ego_enabled = bool(load_ego_config().enabled)
    except Exception:  # noqa: BLE001 - setup-status must never raise into first-run
        logger.warning("setup-status: ego config unreadable; ego_enabled=False", exc_info=True)
        ego_enabled = False

    readiness = compute_readiness(secrets=secrets, ego_enabled=ego_enabled)

    payload = {
        "onboarded": _SETUP_COMPLETE_MARKER.is_file(),
        "password_set": secrets_have_password,
        "cc_oauth": floor.cc_oauth,
        "llm_key_present": floor.llm_key_present,
        "embedding_key_present": floor.embedding_key_present,
        "floor_met": floor.floor_met,
        "identity_set": identity_set,
    }
    payload.update(readiness.as_dict())
    return jsonify(payload)


@blueprint.route("/api/genesis/keys/test", methods=["POST"])
@_async_route
async def keys_test():
    """Live-test an API key VALUE the user just entered (real HTTP call).

    Body: ``{"provider_type": "groq", "key": "gsk-..."}``. Tests the passed
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


# NOTE: there is deliberately no ``setup-complete`` endpoint. The wizard does not
# write the ``~/.genesis/setup-complete`` marker — that marker means "bootstrap
# finished" and is owned by ``scripts/bootstrap.sh`` and the terminal onboarding
# skill. The dashboard card keys off the LIVE ``floor_met`` from ``setup-status``
# instead, so completion can never be asserted over a non-functional install.
