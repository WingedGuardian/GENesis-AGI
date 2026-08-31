"""Guardian honors an EXPIRING gateway pause file (deploy stand-down).

The gateway `pause` verb writes `<state_dir>/paused.json` with an `expires_at`;
`run_check` stands the whole cycle down while that pause is live and unexpired,
so a planned deploy restart never escalates to confirmed_dead. The expiry is the
built-in TTL: a deploy killed mid-run (EXIT-trap resume never fires) self-heals
once `expires_at` passes, rather than leaving the Guardian blind forever.

Distinct from the container `~/.genesis/paused.json` (the shared runtime kill
switch) and from the indefinite `maintenance_file` — this is the host-side,
bounded, gateway-driven stand-down.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.guardian.check import _gateway_pause_active, run_check
from genesis.guardian.config import GuardianConfig

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _run_gateway(cmd: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "SSH_ORIGINAL_COMMAND": cmd}
    return subprocess.run(
        ["bash", str(_GATEWAY)], env=env, capture_output=True, text=True, timeout=30
    )


def _cfg(tmp_path: Path) -> GuardianConfig:
    # state_dir drives config.state_path; the gateway writes paused.json there.
    return GuardianConfig(state_dir=str(tmp_path))


def _write_pause(
    tmp_path: Path,
    *,
    expires_at: str | None,
    paused: bool = True,
    reason: str = "deploy",
    since: str = "2026-08-31T00:00:00Z",
) -> None:
    data: dict = {"paused": paused, "reason": reason, "since": since}
    if expires_at is not None:
        data["expires_at"] = expires_at
    (tmp_path / "paused.json").write_text(json.dumps(data))


NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def test_no_file_not_active(tmp_path: Path) -> None:
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_fresh_unexpired_is_active(tmp_path: Path) -> None:
    _write_pause(tmp_path, expires_at=(NOW + timedelta(minutes=10)).isoformat())
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is True


def test_expired_not_active(tmp_path: Path) -> None:
    # expires_at in the past -> the deploy pause self-healed; monitor normally.
    _write_pause(tmp_path, expires_at=(NOW - timedelta(seconds=10)).isoformat())
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_too_far_ahead_rejected_by_cap(tmp_path: Path) -> None:
    # A pause whose expiry is absurdly far out is suspect (bad writer / clock) —
    # ignore it rather than let the Guardian be muted for hours.
    cfg = _cfg(tmp_path)
    beyond = NOW + timedelta(seconds=cfg.gateway_pause_max_ahead_s + 60)
    _write_pause(tmp_path, expires_at=beyond.isoformat())
    assert _gateway_pause_active(cfg, now=NOW) is False


def test_missing_expires_at_not_active(tmp_path: Path) -> None:
    # No TTL -> fail safe toward monitoring; indefinite stand-down is the
    # maintenance_file's job, not this file's.
    _write_pause(tmp_path, expires_at=None)
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_paused_false_not_active(tmp_path: Path) -> None:
    _write_pause(tmp_path, expires_at=(NOW + timedelta(minutes=10)).isoformat(), paused=False)
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_malformed_json_fails_safe(tmp_path: Path) -> None:
    (tmp_path / "paused.json").write_text("{not valid json")
    # Never crash the check cycle on a bad file; treat as not-paused.
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_naive_expires_at_treated_as_utc(tmp_path: Path) -> None:
    # A timestamp without tzinfo must not raise; interpret as UTC.
    _write_pause(tmp_path, expires_at="2026-08-31T12:10:00")  # 10 min ahead of NOW
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is True


@pytest.mark.asyncio
async def test_run_check_stands_down_when_pause_active(tmp_path: Path) -> None:
    """Wiring: with a live gateway pause, run_check returns BEFORE it builds the
    state machine, so no state.json is ever written (real side-effect, not a mock)."""
    _write_pause(tmp_path, expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())
    await run_check(_cfg(tmp_path))
    assert not (tmp_path / "state.json").exists(), "stand-down must skip the state machine"


@pytest.mark.asyncio
async def test_run_check_does_not_stand_down_when_expired(tmp_path: Path) -> None:
    """Guard-the-guard: an EXPIRED pause must NOT stand the cycle down — otherwise
    the stand-down test above could pass vacuously. run_check proceeds far enough
    to create state.json (health probes fail against a dead endpoint, which is
    fine — we only assert the cycle did NOT early-return)."""
    _write_pause(tmp_path, expires_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    await run_check(_cfg(tmp_path))
    assert (tmp_path / "state.json").exists(), "expired pause must not skip the cycle"


# ── gateway verb round-trip: the writer + the reader must agree ──────────────


def _gateway_state_dir(home: Path) -> Path:
    return home / ".local" / "state" / "genesis-guardian"


def test_gateway_pause_verb_writes_file_guardian_honors(tmp_path: Path) -> None:
    """ACCEPTANCE: `pause <ttl>` writes a file the guardian's own reader accepts —
    the whole point of the feature (writer and reader agree)."""
    r = _run_gateway("pause 600", tmp_path)
    assert r.returncode == 0, r.stderr
    sd = _gateway_state_dir(tmp_path)
    data = json.loads((sd / "paused.json").read_text())
    assert data["paused"] is True
    assert data["reason"] == "deploy"
    assert "expires_at" in data and "since" in data
    # The reader accepts exactly what the writer produced.
    assert _gateway_pause_active(GuardianConfig(state_dir=str(sd))) is True


def test_gateway_bare_pause_uses_default_ttl(tmp_path: Path) -> None:
    r = _run_gateway("pause", tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads((_gateway_state_dir(tmp_path) / "paused.json").read_text())
    assert data.get("expires_at")


def test_gateway_pause_rejects_non_integer_ttl(tmp_path: Path) -> None:
    r = _run_gateway("pause abc", tmp_path)
    assert r.returncode == 1
    assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


def test_gateway_pause_rejects_ttl_over_cap(tmp_path: Path) -> None:
    r = _run_gateway("pause 99999", tmp_path)
    assert r.returncode == 1
    assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


def test_gateway_resume_removes_pause_file(tmp_path: Path) -> None:
    assert _run_gateway("pause 600", tmp_path).returncode == 0
    assert (_gateway_state_dir(tmp_path) / "paused.json").exists()
    r = _run_gateway("resume", tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


# ── regression guards (from the adversarial review) ─────────────────────────


@pytest.mark.parametrize("body", ["123", "[1, 2]", '"a string"', "true", "null"])
def test_non_object_json_fails_safe_no_crash(tmp_path: Path, body: str) -> None:
    """SF-2: valid JSON that is not an object must NOT raise (AttributeError on
    .get) — that would abort run_check, withhold the heartbeat, and read as DOWN."""
    (tmp_path / "paused.json").write_text(body)
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


@pytest.mark.asyncio
async def test_stand_down_still_writes_heartbeat(tmp_path: Path, monkeypatch) -> None:
    """SF-3: a stand-down must still heartbeat (Guardian is alive, just not
    watching) so the container watchdog doesn't read it as DOWN and restart it."""
    calls: list[bool] = []

    async def _spy(config) -> None:  # real path uses incus; unavailable in CI
        calls.append(True)

    monkeypatch.setattr("genesis.guardian.check._write_guardian_heartbeat", _spy)
    _write_pause(tmp_path, expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())
    await run_check(_cfg(tmp_path))
    assert calls == [True], "stand-down must write exactly one heartbeat"
    assert not (tmp_path / "state.json").exists(), "must still stand down"


def test_gateway_pause_leading_zero_ttl_is_base10(tmp_path: Path) -> None:
    """NOTE-1: a leading-zero ttl must be read base-10 (not octal) — 0888 would
    abort the arithmetic under set -e."""
    r = _run_gateway("pause 0888", tmp_path)
    assert r.returncode == 0, r.stderr
    assert (_gateway_state_dir(tmp_path) / "paused.json").exists()
