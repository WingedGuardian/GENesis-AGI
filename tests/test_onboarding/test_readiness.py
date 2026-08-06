"""Tests for the tiered readiness layer (``genesis.onboarding.readiness``).

Readiness sits ABOVE the functional floor and must:
* compute a cumulative tier (T0..T3) that a de-configured lower gate visibly drops;
* gate T2 on Genesis being able to PROACTIVELY reach the owner via Telegram — the
  EXACT condition the live adapter's start-gate enforces (parity-pinned to
  ``channels.bridge._load_bridge_config``), incl. rejecting the real
  "bot token pasted into TELEGRAM_ALLOWED_USERS" mistake;
* gate T3 on the injected ``ego_enabled`` bool (the ego/awareness loop);
* reuse the floor's own computation so the two can never drift.
"""

from __future__ import annotations

import pytest

from genesis.onboarding import floor as floor_mod
from genesis.onboarding.floor import FloorStatus
from genesis.onboarding.readiness import (
    ReadinessStatus,
    _telegram_reach_configured,
    compute_readiness,
)


def _floor(met: bool) -> FloorStatus:
    """A FloorStatus whose ``floor_met`` is exactly ``met`` (all three legs = met)."""
    return FloorStatus(cc_oauth=met, llm_key_present=met, embedding_key_present=met)


def _status(*, floor_met: bool, telegram: bool, ego: bool) -> ReadinessStatus:
    return ReadinessStatus(floor=_floor(floor_met), telegram_configured=telegram, ego_enabled=ego)


# ── Tier truth table (the cumulative gate logic) ───────────────────────────────


def test_tier0_when_floor_unmet_regardless_of_higher_gates():
    # Even with telegram + ego configured, an unmet floor pins the tier at 0 —
    # tiers are cumulative, a higher gate never "leaks" past a lower one.
    assert _status(floor_met=False, telegram=True, ego=True).tier == 0


def test_tier1_floor_met_no_channel():
    s = _status(floor_met=True, telegram=False, ego=False)
    assert s.tier == 1
    assert s.tier_name == "Functional"


def test_tier2_floor_and_telegram_no_ego():
    s = _status(floor_met=True, telegram=True, ego=False)
    assert s.tier == 2
    assert s.tier_name == "Connected"


def test_tier3_floor_telegram_and_ego():
    s = _status(floor_met=True, telegram=True, ego=True)
    assert s.tier == 3
    assert s.tier_name == "Autonomous"


def test_ego_without_telegram_does_not_reach_tier3():
    # Cumulative: ego on but no channel → capped at T1 (T2 gate unmet).
    assert _status(floor_met=True, telegram=False, ego=True).tier == 1


def test_tier_names_cover_every_tier():
    names = {
        _status(floor_met=fm, telegram=tg, ego=eg).tier_name
        for fm, tg, eg in [
            (False, False, False),
            (True, False, False),
            (True, True, False),
            (True, True, True),
        ]
    }
    assert names == {"Bootstrapped", "Functional", "Connected", "Autonomous"}


def test_as_dict_shape_excludes_floor_legs():
    d = _status(floor_met=True, telegram=True, ego=True).as_dict()
    assert d == {
        "tier": 3,
        "tier_name": "Autonomous",
        "telegram_configured": True,
        "ego_enabled": True,
    }
    # Floor legs are emitted by the route, NOT duplicated here.
    assert "floor_met" not in d and "cc_oauth" not in d


# ── T2 gate: _telegram_reach_configured (proactive-reach semantics) ────────────


@pytest.mark.parametrize(
    "secrets, expected",
    [
        ({}, False),  # nothing configured
        ({"TELEGRAM_BOT_TOKEN": "abc123"}, False),  # token but no recipient → can't reach
        ({"TELEGRAM_BOT_TOKEN": "abc123", "TELEGRAM_ALLOWED_USERS": "12345"}, True),
        # The real mistake (memory 47a55700): a bot token pasted into ALLOWED_USERS.
        # Non-numeric → no valid recipient → NOT connected.
        ({"TELEGRAM_BOT_TOKEN": "abc123", "TELEGRAM_ALLOWED_USERS": "abc:token"}, False),
        # Placeholder token never counts even with a valid recipient.
        ({"TELEGRAM_BOT_TOKEN": "PLACEHOLDER", "TELEGRAM_ALLOWED_USERS": "12345"}, False),
        # Empty token never counts.
        ({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_USERS": "12345"}, False),
        # One valid UID among invalid ones still counts.
        ({"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_ALLOWED_USERS": "nope,678"}, True),
        # Whitespace-padded numeric still counts.
        ({"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_ALLOWED_USERS": " 789 "}, True),
    ],
)
def test_telegram_reach_configured(secrets, expected):
    assert _telegram_reach_configured(secrets) is expected


def test_telegram_reach_parity_with_bridge(tmp_path, monkeypatch):
    """The T2 signal must equal the LIVE adapter start-gate, case for case.

    Importing the bridge is heavy (pulls GenesisRuntime), which is exactly why the
    hot-path helper is a replica — this test is the pin that keeps the replica
    honest: for every secrets shape, ``_telegram_reach_configured`` agrees with
    ``_load_bridge_config() is not None``.
    """
    bridge = pytest.importorskip("genesis.channels.bridge")

    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setattr(bridge, "secrets_path", lambda: secrets_file)
    monkeypatch.setattr(floor_mod, "secrets_path", lambda: secrets_file)

    cases = [
        "",
        "TELEGRAM_BOT_TOKEN=abc123\n",
        "TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=12345\n",
        "TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=notanumber\n",
        "TELEGRAM_BOT_TOKEN=PLACEHOLDER\nTELEGRAM_ALLOWED_USERS=12345\n",
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=notanum,678\n",
    ]
    for content in cases:
        secrets_file.write_text(content)
        live = bridge._load_bridge_config() is not None
        replica = _telegram_reach_configured(floor_mod.read_persisted_secrets())
        assert replica is live, f"parity mismatch for {content!r}: replica={replica} live={live}"


# ── compute_readiness wiring (threads secrets + ego through the real floor) ─────


@pytest.fixture()
def floor_met(monkeypatch):
    """Make the floor's non-telegram legs pass so tier hinges on telegram/ego."""
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: True)
    monkeypatch.setattr(floor_mod, "_llm_key_present", lambda secrets: True)
    # embedding leg is _has_any(secrets, EMBEDDING_KEY_NAMES) — supply a key below.


def test_compute_readiness_threads_secrets_and_ego(floor_met):
    secrets = {
        "API_KEY_DEEPINFRA": "di-xxx",  # satisfies the floor embedding leg
        "TELEGRAM_BOT_TOKEN": "abc",
        "TELEGRAM_ALLOWED_USERS": "12345",
    }
    assert compute_readiness(secrets=secrets, ego_enabled=False).tier == 2
    assert compute_readiness(secrets=secrets, ego_enabled=True).tier == 3


def test_compute_readiness_floor_reuse_parity(floor_met):
    # readiness.floor must BE the floor's own computation (no drift).
    secrets = {"API_KEY_DEEPINFRA": "di-xxx"}
    r = compute_readiness(secrets=secrets, ego_enabled=False)
    assert r.floor.as_dict() == floor_mod.compute_floor(secrets=secrets).as_dict()
    assert r.floor.floor_met is True  # legs monkeypatched true + embedding key present


def test_compute_readiness_floor_unmet_is_tier0(monkeypatch):
    # No floor monkeypatch → real legs fail on empty secrets → tier 0 even with
    # telegram + ego "configured".
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: False)
    secrets = {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_ALLOWED_USERS": "12345"}
    assert compute_readiness(secrets=secrets, ego_enabled=True).tier == 0


def test_compute_readiness_ego_flag_is_coerced_bool(floor_met):
    secrets = {
        "API_KEY_DEEPINFRA": "di-xxx",
        "TELEGRAM_BOT_TOKEN": "abc",
        "TELEGRAM_ALLOWED_USERS": "1",
    }
    r = compute_readiness(secrets=secrets, ego_enabled=1)  # truthy non-bool
    assert r.ego_enabled is True
