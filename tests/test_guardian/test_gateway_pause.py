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
from genesis.guardian.config import GuardianConfig, load_config

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _run_gateway(cmd: str, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "SSH_ORIGINAL_COMMAND": cmd}
    # The gateway honors GUARDIAN_STATE_DIR; a dev running the guardian locally
    # has it set, and inheriting it would make `pause` write to the REAL state
    # dir (standing down a live guardian) instead of the test's HOME.
    env.pop("GUARDIAN_STATE_DIR", None)
    return subprocess.run(
        ["bash", str(_GATEWAY)], env=env, capture_output=True, text=True, timeout=30
    )


def _cfg(tmp_path: Path) -> GuardianConfig:
    # state_dir drives config.state_path; the gateway writes paused.json there.
    # Point maintenance_file at an absent path so run_check can't take the
    # maintenance stand-down branch on a box where the real flag exists (that
    # would make the gateway-pause stand-down tests vacuous).
    return GuardianConfig(state_dir=str(tmp_path), maintenance_file=str(tmp_path / "no-maint"))


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


@pytest.mark.parametrize("paused_val", ["false", "true", "0", 1, "yes"])
def test_paused_truthy_non_bool_not_active(tmp_path: Path, paused_val) -> None:
    """SF-1 regression: `paused` must be the literal boolean True. A truthy-but-
    non-bool value (a string "false" an operator hand-edited to cancel a pause, or
    a stringified "true") must NOT stand the guardian down — the old `not data.get
    ("paused")` truthiness check treated "false"/1/"yes" as paused and MUTED the
    watchdog. This REDs on that check and passes only under `is not True`."""
    (tmp_path / "paused.json").write_text(
        json.dumps(
            {
                "paused": paused_val,
                "reason": "deploy",
                "since": "2026-08-31T00:00:00Z",
                "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
            }
        )
    )
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_malformed_json_fails_safe(tmp_path: Path) -> None:
    (tmp_path / "paused.json").write_text("{not valid json")
    # Never crash the check cycle on a bad file; treat as not-paused.
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is False


def test_naive_expires_at_treated_as_utc(tmp_path: Path) -> None:
    # A timestamp without tzinfo must not raise; interpret as UTC.
    _write_pause(tmp_path, expires_at="2026-08-31T12:10:00")  # 10 min ahead of NOW
    assert _gateway_pause_active(_cfg(tmp_path), now=NOW) is True


# NOTE: run_check stand-down is tested via test_stand_down_still_writes_heartbeat
# below (fully isolated: it tripwires _check_cycle so a regression can NEVER reach
# the real production cycle — incus snapshot prune / swap reconcile / live alerts).
# The "expired pause does not stand down" gate is covered by test_expired_not_active
# at the unit level, without running run_check against the host.


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
    # SF-7a: the bare `pause` must set a REAL 1800s window, not merely stamp
    # *some* expires_at — a writer bug that collapsed the window to ~0 would
    # pass a presence check yet leave the guardian effectively un-paused.
    # since/expires_at derive from one `date +%s`, so the delta is exact.
    r = _run_gateway("pause", tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads((_gateway_state_dir(tmp_path) / "paused.json").read_text())
    since = datetime.fromisoformat(data["since"])
    expires = datetime.fromisoformat(data["expires_at"])
    assert (expires - since).total_seconds() == 1800


def test_gateway_pause_ttl_cap_boundary(tmp_path: Path) -> None:
    """SF-7b: the range guard is `1 <= ttl <= 3600` — 3600 is the INCLUSIVE cap
    (accepted, writes a real 3600s window), 3601 is one past it (rejected), and 0
    is below the floor (rejected). Locks the exact boundary so a `<`/`<=` slip in
    the shell guard can't silently widen or narrow the accepted range."""
    ok = _run_gateway("pause 3600", tmp_path)
    assert ok.returncode == 0, ok.stderr
    data = json.loads((_gateway_state_dir(tmp_path) / "paused.json").read_text())
    since = datetime.fromisoformat(data["since"])
    expires = datetime.fromisoformat(data["expires_at"])
    assert (expires - since).total_seconds() == 3600
    # one past the cap, and below the floor: both rejected, no file written
    for bad in ("pause 3601", "pause 0"):
        (_gateway_state_dir(tmp_path) / "paused.json").unlink(missing_ok=True)
        r = _run_gateway(bad, tmp_path)
        assert r.returncode == 1, f"{bad!r} should be rejected: {r.stdout} {r.stderr}"
        assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


def test_gateway_pause_rejects_non_integer_ttl(tmp_path: Path) -> None:
    r = _run_gateway("pause abc", tmp_path)
    assert r.returncode == 1
    assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


def test_gateway_pause_rejects_ttl_over_cap(tmp_path: Path) -> None:
    r = _run_gateway("pause 99999", tmp_path)
    assert r.returncode == 1
    assert not (_gateway_state_dir(tmp_path) / "paused.json").exists()


@pytest.mark.parametrize("overflow", ["18446744073709551617", "18446744073709555216"])
def test_gateway_pause_rejects_overflow_ttl(tmp_path: Path, overflow: str) -> None:
    """F6: a TTL beyond 64-bit must be rejected BEFORE `$((10#…))` wraps it into
    the accepted range. Under fixed-width bash arithmetic `18446744073709551617`
    wraps to 1 and `18446744073709555216` wraps to 3600 — both all-digit, both
    passing the lexical guard, both silently accepted without the magnitude check.
    The guard must reject them (rc 1, no file written)."""
    r = _run_gateway(f"pause {overflow}", tmp_path)
    assert r.returncode == 1, f"overflow ttl should be rejected: {r.stdout} {r.stderr}"
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
@pytest.mark.parametrize("trigger", ["gateway_pause", "maintenance"])
async def test_stand_down_still_writes_heartbeat(tmp_path: Path, monkeypatch, trigger) -> None:
    """BOTH stand-down branches (gateway pause AND maintenance) must still write
    the heartbeat — the Guardian is alive, just not watching — so the container
    watchdog doesn't read it as DOWN and restart it. Deleting the heartbeat from
    EITHER branch must fail here (SF-4).

    Fully isolated: the heartbeat is spied and _check_cycle + the alert drain are
    tripwired, so a regression fails LOUDLY instead of running the real production
    cycle (incus snapshot prune / swap reconcile / live alerts) against the host.
    """
    calls: list[str | None] = []

    async def _spy(config, standdown=None) -> None:  # real path uses incus; CI-absent
        calls.append(standdown)

    async def _tripwire(*a, **k):  # first heavy production calls after stand-down
        raise AssertionError("run_check reached the production cycle — stand-down failed")

    monkeypatch.setattr("genesis.guardian.check._write_guardian_heartbeat", _spy)
    monkeypatch.setattr("genesis.guardian.check._check_cycle", _tripwire)
    monkeypatch.setattr("genesis.guardian.check._drain_host_alert_queue", _tripwire)
    cfg = _cfg(tmp_path)
    if trigger == "gateway_pause":
        _write_pause(tmp_path, expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())
    else:  # maintenance: create the (otherwise-absent) maintenance flag
        Path(cfg.maintenance_file).write_text("")
    await run_check(cfg)
    # Exactly one heartbeat, carrying the trigger's stand-down marker (F5) so
    # probe_guardian reports DEGRADED, not HEALTHY, for the skipped cycle.
    assert calls == [trigger], f"{trigger} stand-down must write one heartbeat marked '{trigger}'"
    assert not (tmp_path / "state.json").exists(), "must still stand down"


def test_heartbeat_payload_marks_standdown() -> None:
    """F5: the payload carries a `standdown` reason only when standing down; the
    normal end-of-cycle heartbeat omits it (→ probe_guardian stays HEALTHY)."""
    from genesis.guardian.check import _heartbeat_payload

    assert _heartbeat_payload("gateway_pause")["standdown"] == "gateway_pause"
    assert _heartbeat_payload("maintenance")["standdown"] == "maintenance"
    assert "standdown" not in _heartbeat_payload()
    assert "standdown" not in _heartbeat_payload(None)
    # Always alive + timestamped regardless of stand-down.
    assert _heartbeat_payload()["guardian_alive"] is True
    assert "timestamp" in _heartbeat_payload("maintenance")


@pytest.mark.asyncio
async def test_heartbeat_write_timeout_reaps_child(tmp_path: Path, monkeypatch) -> None:
    """SF-5: when the incus heartbeat write times out, the child must be killed
    AND reaped (proc.kill + awaited proc.wait) — else a wedged incus/container
    orphans a process on every stand-down tick during a long pause. The function
    must swallow the timeout (never raise into run_check, which would withhold the
    heartbeat and read as DOWN)."""
    from genesis.guardian import check as check_mod

    class FakeProc:
        def __init__(self) -> None:
            self.killed = False
            self.waited = False
            self.returncode = None

        async def communicate(self, input=None):  # noqa: A002 - matches asyncio API
            raise TimeoutError  # simulate wait_for(communicate) timing out

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            return 0

    fake = FakeProc()

    async def _fake_exec(*a, **k):
        return fake

    monkeypatch.setattr(check_mod.asyncio, "create_subprocess_exec", _fake_exec)

    cfg = GuardianConfig(state_dir=str(tmp_path))
    await check_mod._write_guardian_heartbeat(cfg)  # must NOT raise
    assert fake.killed, "timed-out heartbeat child must be killed"
    assert fake.waited, "killed child must be reaped (awaited), not left as a zombie"


def test_gateway_pause_leading_zero_ttl_is_base10(tmp_path: Path) -> None:
    """NOTE-1: a leading-zero ttl must be read base-10 (not octal) — 0888 would
    abort the arithmetic under set -e."""
    r = _run_gateway("pause 0888", tmp_path)
    assert r.returncode == 0, r.stderr
    assert (_gateway_state_dir(tmp_path) / "paused.json").exists()


def test_config_allowlist_honors_gateway_pause_cap(tmp_path: Path) -> None:
    """P2: a YAML-set gateway_pause_max_ahead_s must reach the config (be in the
    load_config top-level allowlist), else the documented cap can't be tuned."""
    cfg_yaml = tmp_path / "guardian.yaml"
    cfg_yaml.write_text("gateway_pause_max_ahead_s: 1200\n")
    assert load_config(cfg_yaml).gateway_pause_max_ahead_s == 1200


def test_load_config_honors_state_dir_env(tmp_path: Path, monkeypatch) -> None:
    """SF-6: the READER side of the env redirect. The gateway (writer) honors
    GUARDIAN_STATE_DIR; load_config (the guardian's own config) must resolve the
    SAME dir from that env, or writer and reader land on different paused.json
    files and the stand-down silently never fires. Empty YAML so env is the only
    source of state_dir; _finalize -> _env_override applies the override."""
    cfg_yaml = tmp_path / "guardian.yaml"
    cfg_yaml.write_text("")
    custom = tmp_path / "reader-state"
    monkeypatch.setenv("GUARDIAN_STATE_DIR", str(custom))
    cfg = load_config(cfg_yaml)
    assert cfg.state_dir == str(custom)
    assert cfg.state_path == custom


def test_load_config_warns_on_cap_below_caller_ttl(tmp_path: Path, caplog) -> None:
    """F4: a gateway_pause_max_ahead_s below the 1800s default caller TTL silently
    disables the deploy stand-down — the pause file is rejected as too-far-ahead,
    so `pause` reports success while run_check keeps monitoring. load_config must
    WARN on that misconfiguration; the default (3600) must stay silent."""
    import logging

    low_yaml = tmp_path / "low.yaml"
    low_yaml.write_text("gateway_pause_max_ahead_s: 600\n")
    with caplog.at_level(logging.WARNING, logger="genesis.guardian.config"):
        cfg = load_config(low_yaml)
    assert cfg.gateway_pause_max_ahead_s == 600  # value still applied (not clamped)
    assert "below the 1800" in caplog.text

    caplog.clear()
    ok_yaml = tmp_path / "ok.yaml"
    ok_yaml.write_text("gateway_pause_max_ahead_s: 3600\n")
    with caplog.at_level(logging.WARNING, logger="genesis.guardian.config"):
        load_config(ok_yaml)
    assert "below the 1800" not in caplog.text


def test_gateway_honors_state_dir_env(tmp_path: Path) -> None:
    """P2: the gateway must write where the reader looks — GUARDIAN_STATE_DIR
    redirects both (writer here, reader via GuardianConfig.state_dir)."""
    custom = tmp_path / "custom-state"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GUARDIAN_STATE_DIR": str(custom),
        "SSH_ORIGINAL_COMMAND": "pause 300",
    }
    r = subprocess.run(["bash", str(_GATEWAY)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (custom / "paused.json").exists()
    assert _gateway_pause_active(GuardianConfig(state_dir=str(custom))) is True
