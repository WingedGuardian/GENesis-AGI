"""Tests for the tiered readiness layer (``genesis.onboarding.readiness``).

Readiness sits ABOVE the functional floor and must:
* compute a cumulative tier (T0..T3) that a de-configured lower gate visibly drops;
* gate T2 on Genesis being able to PROACTIVELY reach the owner via Telegram — the
  EXACT condition the live adapter's start-gate enforces, computed from the RAW
  secrets.env text with the adapter's OWN manual parser (NOT dotenv), and
  parity-pinned to ``channels.bridge._load_bridge_config`` across quoted / commented
  / interpolated shapes (incl. rejecting the "bot token pasted into
  TELEGRAM_ALLOWED_USERS" mistake);
* gate T3 on the injected ``ego_enabled`` bool (the ego/awareness loop);
* reuse the floor's own computation so the two can never drift.
"""

from __future__ import annotations

import pytest

from genesis.onboarding import floor as floor_mod
from genesis.onboarding import readiness as readiness_mod
from genesis.onboarding.floor import FloorStatus
from genesis.onboarding.readiness import (
    ReadinessStatus,
    _read_secrets_env_text,
    _telegram_reach_configured,
    compute_readiness,
)


def _floor(met: bool) -> FloorStatus:
    """A FloorStatus whose ``floor_met`` is exactly ``met`` (all three legs = met)."""
    return FloorStatus(cc_oauth=met, llm_key_present=met, embedding_key_present=met)


def _status(
    *, floor_met: bool, telegram: bool, ego: bool, onboarded: bool = True
) -> ReadinessStatus:
    return ReadinessStatus(
        floor=_floor(floor_met),
        telegram_configured=telegram,
        ego_enabled=ego,
        onboarded=onboarded,
    )


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


def test_not_onboarded_caps_at_tier2():
    # Ego enabled but bootstrap marker absent → the ego cadence can't run, so T3 is
    # NOT reached (avoids "onboarded: false" alongside Autonomous).
    assert _status(floor_met=True, telegram=True, ego=True, onboarded=False).tier == 2
    # With the marker present, the same config reaches T3.
    assert _status(floor_met=True, telegram=True, ego=True, onboarded=True).tier == 3


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
    # Floor legs AND `onboarded` are emitted by the route, NOT duplicated here.
    assert "floor_met" not in d and "cc_oauth" not in d and "onboarded" not in d


# ── T2 gate: _telegram_reach_configured (raw-text, adapter-manual semantics) ────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", False),  # nothing configured
        ("TELEGRAM_BOT_TOKEN=abc123\n", False),  # token but no recipient → can't reach
        ("TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=12345\n", True),
        # The real mistake (memory 47a55700): a bot token pasted into ALLOWED_USERS.
        # Non-numeric → no valid recipient → NOT connected.
        ("TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=abc:token\n", False),
        # Placeholder token never counts even with a valid recipient.
        ("TELEGRAM_BOT_TOKEN=PLACEHOLDER\nTELEGRAM_ALLOWED_USERS=12345\n", False),
        # Empty token never counts.
        ("TELEGRAM_BOT_TOKEN=\nTELEGRAM_ALLOWED_USERS=12345\n", False),
        # One valid UID among invalid ones still counts.
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=nope,678\n", True),
        # Whitespace-padded numeric still counts.
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS= 789 \n", True),
        # Comment lines are ignored; a real value below still parses.
        ("# a comment\nTELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\n", True),
        # Manual parse (like the adapter) does NOT strip single quotes → '12345' is
        # not a valid numeric id → NOT connected (the divergence the review caught).
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS='12345'\n", False),
        # Manual parse does NOT strip inline comments → "12345 # me" is not numeric.
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345 # me\n", False),
        # Double quotes ARE stripped (adapter's .strip('"')) → valid.
        ('TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS="12345"\n', True),
        # Valid token + recipient but a malformed setting the live loader parses
        # (DAY_BOUNDARY_HOUR) → the loader raises → adapter stays stopped → NOT
        # reachable (Codex round-2 P2).
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\nDAY_BOUNDARY_HOUR=abc\n", False),
        # A well-formed DAY_BOUNDARY_HOUR does not block reach.
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\nDAY_BOUNDARY_HOUR=6\n", True),
        # str.isdigit() accepts '²' but int() rejects it → the loader raises on the
        # allowed-user conversion → adapter stopped → NOT reachable (Codex round-3 P2).
        ("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=²\n", False),
    ],
)
def test_telegram_reach_configured(text, expected):
    assert _telegram_reach_configured(text) is expected


def test_telegram_reach_parity_with_bridge(tmp_path, monkeypatch):
    """The T2 signal must equal the LIVE adapter start-gate, case for case.

    This is the load-bearing pin: ``_telegram_reach_configured`` must agree with
    ``channels.bridge._load_bridge_config`` for EVERY secrets shape — including the
    quoted / inline-comment / interpolation shapes where dotenv (the floor's parser)
    and the adapter's manual parser diverge. Fails hard (not skip) if the bridge
    can't import — a silent skip would let parity drift ship unnoticed.
    """
    try:
        from genesis.channels import bridge
    except Exception as exc:  # noqa: BLE001 - the pin must be loud, never skipped
        pytest.fail(
            f"bridge must import for the parity pin (got {exc!r}); this test is the "
            "sole guard against T2 signal drift"
        )

    secrets_file = tmp_path / "secrets.env"
    monkeypatch.setattr(bridge, "secrets_path", lambda: secrets_file)

    cases = [
        "TELEGRAM_BOT_TOKEN=abc123\n",
        "TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=12345\n",
        "TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_ALLOWED_USERS=notanumber\n",
        "TELEGRAM_BOT_TOKEN=PLACEHOLDER\nTELEGRAM_ALLOWED_USERS=12345\n",
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=notanum,678\n",
        # Divergent shapes (dotenv vs manual) — both must now AGREE via manual parse:
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS='12345'\n",
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345 # me\n",
        'TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS="12345"\n',
        "TELEGRAM_BOT_TOKEN='PLACEHOLDER'\nTELEGRAM_ALLOWED_USERS=12345\n",
        # Malformed non-telegram setting: the live loader RAISES (adapter stays
        # stopped). "Would the adapter load" is False → replica must also be False.
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\nDAY_BOUNDARY_HOUR=abc\n",
        # str.isdigit()-true / int()-false unicode digit → loader raises → False.
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=²\n",
    ]
    for content in cases:
        secrets_file.write_text(content)
        # A raise from the live loader means "adapter would NOT load" — equivalent to
        # None for the reach question, so treat it as False (not a test error).
        try:
            live = bridge._load_bridge_config() is not None
        except Exception:  # noqa: BLE001 - a raising loader == not loadable == False
            live = False
        replica = _telegram_reach_configured(secrets_file.read_text())
        assert replica is live, f"parity mismatch for {content!r}: replica={replica} live={live}"


def test_read_secrets_env_text_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness_mod, "secrets_path", lambda: tmp_path / "nope.env")
    assert _read_secrets_env_text() == ""


def test_read_secrets_env_text_non_utf8_is_empty(tmp_path, monkeypatch):
    # A non-UTF-8 secrets.env must degrade to "" (not configured), never 500 the
    # route: read_text() raises UnicodeDecodeError (a ValueError subclass, NOT OSError).
    bad = tmp_path / "secrets.env"
    bad.write_bytes(b"\xff\xfe\x00TELEGRAM_BOT_TOKEN=abc\n")
    monkeypatch.setattr(readiness_mod, "secrets_path", lambda: bad)
    assert _read_secrets_env_text() == ""


# ── shared loader contract (channels.bridge_config — the single source of truth) ─


def test_build_bridge_config_contract():
    from genesis.channels.bridge_config import build_bridge_config
    from genesis.channels.bridge_config import parse_secrets_env_text as p

    assert build_bridge_config(p("")) is None  # missing token
    assert build_bridge_config(p("TELEGRAM_BOT_TOKEN=abc")) is None  # no recipient
    cfg = build_bridge_config(p("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345"))
    assert cfg is not None and cfg["allowed_users"] == {12345}
    # Values the live adapter chokes on RAISE here too (preserved fail-to-load):
    with pytest.raises(ValueError):
        build_bridge_config(
            p("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\nDAY_BOUNDARY_HOUR=x")
        )
    with pytest.raises(ValueError):
        build_bridge_config(p("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=²"))


# ── compute_readiness wiring (threads floor secrets + telegram text + ego) ─────


@pytest.fixture()
def floor_met(monkeypatch):
    """Make the floor's non-telegram legs pass so tier hinges on telegram/ego."""
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: True)
    monkeypatch.setattr(floor_mod, "_llm_key_present", lambda secrets: True)
    # embedding leg is _has_any(secrets, EMBEDDING_KEY_NAMES) — supply a key below.


def test_compute_readiness_threads_secrets_text_ego_and_onboarded(floor_met):
    secrets = {"API_KEY_DEEPINFRA": "di-xxx"}  # satisfies the floor embedding leg
    tg = "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    base = dict(secrets=secrets, secrets_text=tg, onboarded=True)
    assert compute_readiness(**base, ego_enabled=False).tier == 2
    assert compute_readiness(**base, ego_enabled=True).tier == 3
    # Ego enabled but NOT onboarded (marker absent) → capped at T2.
    assert (
        compute_readiness(secrets=secrets, secrets_text=tg, ego_enabled=True, onboarded=False).tier
        == 2
    )


def test_compute_readiness_reads_file_when_text_omitted(floor_met, tmp_path, monkeypatch):
    # When secrets_text is not passed, T2 reads the raw file via secrets_path.
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n")
    monkeypatch.setattr(readiness_mod, "secrets_path", lambda: secrets_file)
    r = compute_readiness(secrets={"API_KEY_DEEPINFRA": "d"}, ego_enabled=False, onboarded=True)
    assert r.telegram_configured is True
    assert r.tier == 2


def test_compute_readiness_floor_reuse_parity(floor_met):
    # readiness.floor must BE the floor's own computation (no drift).
    secrets = {"API_KEY_DEEPINFRA": "di-xxx"}
    r = compute_readiness(secrets=secrets, secrets_text="", ego_enabled=False, onboarded=True)
    assert r.floor.as_dict() == floor_mod.compute_floor(secrets=secrets).as_dict()
    assert r.floor.floor_met is True


def test_compute_readiness_floor_unmet_is_tier0(monkeypatch):
    # No floor monkeypatch → real legs fail on empty secrets → tier 0 even with
    # telegram + ego "configured".
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: False)
    tg = "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    assert (
        compute_readiness(secrets={}, secrets_text=tg, ego_enabled=True, onboarded=True).tier == 0
    )


def test_compute_readiness_ego_flag_is_coerced_bool(floor_met):
    tg = "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=1\n"
    r = compute_readiness(
        secrets={"API_KEY_DEEPINFRA": "d"}, secrets_text=tg, ego_enabled=1, onboarded=True
    )
    assert r.ego_enabled is True
