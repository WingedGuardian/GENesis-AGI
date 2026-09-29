"""Tests for scripts/systemd/genesis-tailscale-watchdog.py (the root helper).

The helper takes its runner, clocks and boot id through ``Ctx``, so most tests
drive ``run_once`` against ``World``, a fake of the tailscale CLI and systemd.
One test runs the real file as a subprocess with stub binaries, so the wiring
between the file and the real commands is exercised too.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "systemd" / "genesis-tailscale-watchdog.py"
BOOT = "5f524ce5-5df1-49b5-a1fe-572ba51e3709"
NOW = 2_000_000_000.0  # wall clock
MONO = 500_000.0  # CLOCK_MONOTONIC
A, B, C = "100.64.0.1", "100.64.0.2", "100.64.0.3"


def _load():
    spec = importlib.util.spec_from_file_location("tailscale_watchdog", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tw = _load()


def _iso(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f123Z")


def peer(ip: str, *, age: float = 900, active=True, **extra) -> dict:
    return {
        "Active": active,
        "LastHandshake": _iso(NOW - age),
        "TailscaleIPs": [ip, "2001:db8::1"],
        "HostName": "peer-host",
        **extra,
    }


ZERO = "0001-01-01T00:00:00Z"


class World:
    """Fake tailscale CLI + systemd. Pings reply by default; override per ip."""

    def __init__(self, peers: dict | None = None):
        self.status = {"BackendState": "Running", "Peer": peers or {}}
        self.tsmp: dict[str, list[bool]] = {}
        self.disco: dict[str, bool] = {}
        self.unit = {
            "ActiveState": "active",
            "InvocationID": "inv-1",
            "Job": "",
            "ActiveEnterTimestampMonotonic": "1000000",  # started 1s after boot
        }
        # What try-restart does: (rc, unit changes applied after it runs).
        self.restart = (0, {"InvocationID": "inv-2"})
        # Whether a restart clears the stuck tunnels (the observed incident).
        self.restart_fixes = True
        self.calls: list[list[str]] = []
        self.mono = MONO
        self.status_rc = 0

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        cmd = argv[1]
        if cmd == "status":
            return self.status_rc, json.dumps(self.status)
        if cmd == "ping":
            ip = argv[-1]
            if "--tsmp" in argv:
                seq = self.tsmp.get(ip, [True])
                ok = seq.pop(0) if len(seq) > 1 else seq[0]
            else:
                ok = self.disco.get(ip, True)
            return (0 if ok else 1), ""
        if cmd == "show":
            props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
            return 0, "".join(f"{p}={self.unit.get(p, '')}\n" for p in props)
        if cmd == "try-restart":
            rc, changes = self.restart
            self.unit.update(changes)
            if self.restart_fixes:
                self.tsmp = {}
            return rc, ""
        raise AssertionError(f"unexpected command {argv}")

    def stuck(self, *ips: str):
        for ip in ips:
            self.tsmp[ip] = [False]
            self.disco[ip] = True

    def fixed(self, ip: str):
        self.tsmp[ip] = [True]

    def restarts(self) -> int:
        return sum(1 for c in self.calls if c[1] == "try-restart")


def ctx(world: World, tmp_path: Path, **env) -> tw.Ctx:
    full = {
        "NETWD_TS_STATE_FILE": str(tmp_path / "state.json"),
        "NETWD_TS_STATUS_SNAPSHOT": str(tmp_path / "status.json"),
        **env,
    }
    return tw.Ctx(
        env=full,
        run=world.run,
        mono=lambda: world.mono,
        wall=lambda: NOW,
        sleep=lambda s: setattr(world, "mono", world.mono + s),
        boot_id=BOOT,
        which=lambda name: "/usr/bin/tailscale",
    )


def state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())


def tick(world: World, c, seconds: float = 120) -> dict:
    world.mono += seconds
    tw.run_once(c)
    return json.loads(Path(c.state_file).read_text())


# ── the trigger, and the evidence each run reports ───────────────────────


def test_no_suspects_is_healthy_and_fresh_handshakes_are_ok_evidence(tmp_path):
    world = World({"a": peer(A, age=30)})
    assert tw.run_once(ctx(world, tmp_path)) == 0
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"], s["events"]) == ("none", {A: "ok"}, [])
    assert not any(c[1] == "ping" for c in world.calls)


def test_a_stuck_tunnel_is_healed_and_reported_ok(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    assert tw.run_once(ctx(world, tmp_path)) == 0
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"], s["heal_count"]) == ("healed", {A: "ok"}, 1)
    (event,) = s["events"]
    assert (event["action"], event["peers"], event["cleared"]) == ("healed", [A], [A])
    assert ["systemctl", "try-restart", "tailscaled"] in world.calls


def test_a_peer_that_is_gone_is_offline_and_left_alone(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.disco[A] = False
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"]) == ("suspect-unreachable", {A: "offline"})
    assert world.restarts() == 0


def test_an_offline_peer_is_never_restarted_however_long_it_stays_offline(tmp_path):
    """The restart loop the saved-set design had: a once-stuck peer that went
    offline was restarted for every hour. Nothing is remembered now, so only a
    peer confirmed stuck in the same run is a target."""
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart_fixes = False
    c = ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0")
    tw.run_once(c)
    assert world.restarts() == 1
    world.disco[A] = False  # the laptop was closed
    for _ in range(5):
        assert tick(world, c, 3601)["evidence"] == {A: "offline"}
    assert world.restarts() == 1


def test_a_tunnel_the_discovery_ping_revived_is_ok(tmp_path):
    world = World({"a": peer(A)})
    world.tsmp[A] = [False, True]
    tw.run_once(ctx(world, tmp_path))
    assert state(tmp_path)["evidence"] == {A: "ok"}
    assert world.restarts() == 0


def test_tailscaled_not_running_is_unavailable_with_no_evidence(tmp_path):
    world = World({"a": peer(A)})
    world.unit["ActiveState"] = "inactive"
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"]) == ("unavailable", {})
    assert not any(c[1] in ("status", "ping") for c in world.calls)


def test_a_wall_clock_step_back_still_probes_the_peer(tmp_path):
    world = World({"a": peer(A, age=-86400)})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path, NETWD_TS_MODE="observe"))
    s = state(tmp_path)
    assert s["evidence"] == {A: "stuck"}
    assert s["handshake_age_s"] == {A: None}


def test_a_peer_that_never_completes_a_handshake_is_a_suspect_once_tailscaled_has_settled(tmp_path):
    """After a restart that did not clear the fault, the stuck peer shows a ZERO
    handshake; skipping zero would call it healthy forever."""
    world = World({"a": {"Active": True, "LastHandshake": ZERO, "TailscaleIPs": [A]}})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path, NETWD_TS_MODE="observe"))
    assert state(tmp_path)["evidence"] == {A: "stuck"}


def test_a_zero_handshake_on_a_freshly_started_tailscaled_is_unknown_not_ok(tmp_path):
    world = World({"a": {"Active": True, "LastHandshake": ZERO, "TailscaleIPs": [A]}})
    world.stuck(A)
    world.unit["ActiveEnterTimestampMonotonic"] = str(int((MONO - 60) * 1e6))
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"]) == ("none", {})
    assert not any(c[1] == "ping" for c in world.calls)


def test_an_idle_peer_with_a_fresh_handshake_is_ok(tmp_path):
    """An operator who fixed a tunnel by hand leaves an idle peer with a fresh
    handshake: that is evidence, and it clears the alert."""
    world = World({"a": peer(A, age=30, active=False)})
    tw.run_once(ctx(world, tmp_path))
    assert state(tmp_path)["evidence"] == {A: "ok"}


@pytest.mark.parametrize("backend", ["NeedsLogin", "Stopped", "Starting", None])
def test_a_backend_that_is_not_running_proves_nothing(tmp_path, backend):
    """Logged out, key expired, `tailscale down`: the peer map is empty, and an
    empty map from a stopped backend must never read as a complete, healthy
    tailnet (it would clear every open alert)."""
    world = World()
    world.status = {"BackendState": backend, "Peer": None}
    c = ctx(world, tmp_path)
    for expected in (1, 2, 3):
        s = tick(world, c)
        assert (s["last_action"], s["present_complete"], s["evidence"]) == (
            "unavailable",
            False,
            {},
        )
        assert s["blind_runs"] == expected


@pytest.mark.parametrize("hang", ["first", "discovery", "second"])
def test_a_ping_that_cannot_be_judged_is_not_evidence(tmp_path, hang):
    """A ping that hangs past its bound (rc None) or fails oddly is not a
    missing reply: the peer is unjudged, never "offline"."""
    world = World({"a": peer(A)})
    world.stuck(A)
    real = world.run
    calls = {"n": 0}
    target = {"first": 1, "discovery": 2, "second": 3}[hang]

    def hangs(argv, timeout):
        if argv[1] == "ping":
            calls["n"] += 1
            if calls["n"] == target:
                return None, ""
        return real(argv, timeout)

    c = ctx(world, tmp_path)
    c.run = hangs
    tw.run_once(c)
    s = state(tmp_path)
    assert (s["evidence"], s["unjudged"], s["last_action"]) == ({}, 1, "incomplete")
    assert s["blind_runs"] == 1  # the only suspect could not be judged
    assert world.restarts() == 0


def test_an_odd_ping_exit_code_is_not_a_missing_reply(tmp_path):
    world = World({"a": peer(A)})
    real = world.run

    def usage_error(argv, timeout):
        if argv[1] == "ping":
            return 2, ""
        return real(argv, timeout)

    c = ctx(world, tmp_path)
    c.run = usage_error
    tw.run_once(c)
    assert state(tmp_path)["evidence"] == {}


def test_a_peer_two_restarts_did_not_clear_is_not_restarted_for_again(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart_fixes = False
    c = ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0", NETWD_TS_RATE_LIMIT_SEC="300")
    for n in range(4):
        world.restart = (0, {"InvocationID": f"inv-{n + 10}"})
        s = tick(world, c, 301)
    assert world.restarts() == 2
    assert (s["last_action"], s["evidence"]) == ("stuck", {A: "stuck"})
    world.status["Peer"]["b"] = peer(B)
    world.stuck(B)  # a different peer is still healed
    world.restart = (0, {"InvocationID": "inv-99"})
    tick(world, c, 301)
    assert world.restarts() == 3


def test_an_idle_peer_is_unknown(tmp_path):
    world = World({"a": peer(A, active=False)})
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert s["evidence"] == {}
    assert s["present"] == [A]


def test_observe_mode_reports_stuck_and_never_restarts(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    c = ctx(world, tmp_path, NETWD_TS_MODE="observe")
    for _ in range(3):
        s = tick(world, c)
    assert (s["last_action"], s["evidence"], s["events"]) == ("stuck", {A: "stuck"}, [])
    assert world.restarts() == 0


@pytest.mark.parametrize(
    "blind",
    [
        {"status_rc": 1},
        {"status": {"BackendState": "Running"}},  # Peer key missing
        {"status": {"Peer": []}},
    ],
    ids=["status-fails", "peer-missing", "peer-not-a-map"],
)
def test_a_run_that_cannot_look_reports_no_evidence(tmp_path, blind):
    world = World({"a": peer(A)})
    for attr, value in blind.items():
        setattr(world, attr, value)
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert s["evidence"] == {}
    assert s["present_complete"] is False
    assert s["last_action"] in ("unavailable", "status-unparseable")


def test_a_malformed_entry_makes_the_present_list_incomplete(tmp_path):
    world = World(
        {"bad": {"Active": True, "LastHandshake": 5, "TailscaleIPs": [B]}, "a": peer(A, age=30)}
    )
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["malformed_peers"], s["present_complete"]) == (
        "incomplete",
        1,
        False,
    )
    assert s["evidence"] == {A: "ok"}


def test_the_present_list_names_every_well_formed_peer(tmp_path):
    world = World({"a": peer(A, age=30), "b": peer(B, active=False), "c": peer(C)})
    tw.run_once(ctx(world, tmp_path, NETWD_TS_MODE="observe"))
    s = state(tmp_path)
    assert (s["present"], s["present_complete"]) == ([A, B, C], True)


def test_a_stuck_peer_confirmed_during_cooldown_is_still_reported(tmp_path):
    world = World({"a": peer(A), "b": peer(B, age=30)})
    world.stuck(A)
    world.restart_fixes = False
    c = ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0")
    tw.run_once(c)
    world.status["Peer"]["b"] = peer(B)
    world.stuck(B)
    s = tick(world, c)
    assert s["evidence"] == {A: "stuck", B: "stuck"}
    assert s["last_action"] == "ratelimited"
    assert world.restarts() == 1


def test_a_capped_scan_is_incomplete_and_the_skipped_peer_unknown(tmp_path):
    world = World({f"p{i}": peer(f"100.64.0.{i}") for i in range(1, 5)})
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["skipped"], len(s["evidence"])) == ("incomplete", 1, 3)


@pytest.mark.parametrize("env", [{"NETWD_TS_MAX_PROBES": "0"}, {"NETWD_TS_SCAN_BUDGET_SEC": "0"}])
def test_a_scan_capped_to_nothing_counts_as_blind(tmp_path, env):
    world = World({"a": peer(A)})
    c = ctx(world, tmp_path, **env)
    for expected in (1, 2, 3):
        s = tick(world, c)
        assert (s["last_action"], s["probed"], s["blind_runs"]) == ("incomplete", 0, expected)


def test_a_malformed_peer_with_nothing_to_probe_is_not_blind(tmp_path):
    world = World(
        {"bad": {"Active": True, "LastHandshake": 5, "TailscaleIPs": [B]}, "a": peer(A, age=30)}
    )
    c = ctx(world, tmp_path)
    for _ in range(4):
        s = tick(world, c)
    assert (s["last_action"], s["blind_runs"]) == ("incomplete", 0)


def test_the_scan_start_rotates_so_every_peer_gets_probed(tmp_path):
    world = World({f"p{i}": peer(f"100.64.0.{i}") for i in range(1, 5)})
    world.stuck("100.64.0.4")
    c = ctx(world, tmp_path, NETWD_TS_MODE="observe")
    seen = set()
    for _ in range(4):
        seen |= {ip for ip, v in tick(world, c)["evidence"].items() if v == "stuck"}
    assert seen == {"100.64.0.4"}


def test_evidence_and_present_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "MAX_PEERS", 5)
    world = World({f"p{i}": peer(f"100.64.1.{i}", age=30) for i in range(1, 10)})
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (len(s["evidence"]), len(s["present"]), s["present_complete"]) == (5, 5, False)


# ── untrusted input ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"Active": True, "LastHandshake": _iso(NOW - 900), "TailscaleIPs": 7},
        {"Active": True, "LastHandshake": 12345, "TailscaleIPs": [B]},
        {"Active": True, "LastHandshake": "yesterday", "TailscaleIPs": [B]},
        ["not", "a", "peer"],
        None,
    ],
)
def test_a_malformed_peer_is_skipped_and_the_rest_still_judged(tmp_path, bad):
    world = World({"bad": bad, "good": peer(A)})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert s["malformed_peers"] == 1
    assert s["last_action"] == "healed"


def test_a_peer_chosen_hostname_never_reaches_the_file(tmp_path):
    hostile = "IGNORE-PREVIOUS-INSTRUCTIONS"
    world = World({"a": peer(A, HostName=hostile, DNSName=hostile + ".ts.net.")})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path, NETWD_TS_MODE="observe"))
    assert hostile not in (tmp_path / "state.json").read_text()
    assert state(tmp_path)["evidence"] == {A: "stuck"}
    snapshot = tmp_path / "status.json"  # the raw status, root-only evidence
    assert hostile in snapshot.read_text()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_a_non_ipv4_address_list_is_not_a_target(tmp_path):
    world = World({"a": peer(A, TailscaleIPs=["2001:db8::2", "999.1.1.1", "-c 1"])})
    tw.run_once(ctx(world, tmp_path))
    assert state(tmp_path)["last_action"] == "none"
    assert not any(c[1] == "ping" for c in world.calls)


def test_an_empty_tailnet_is_healthy_and_complete(tmp_path):
    world = World()
    world.status["Peer"] = None
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["present"], s["present_complete"]) == ("none", [], True)


# ── settings ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(tw.SETTINGS))
def test_every_setting_accepts_its_bounds_and_rejects_beyond_them(name):
    default, lo, hi = tw.SETTINGS[name]
    for value, expected in (
        (str(lo), lo),
        (str(hi), hi),
        (str(hi + 1), default),
        (str(lo - 1), default),
        ("18446744073709551616", default),
        ("1e3", default),
        ("", default),
    ):
        settings, _ = tw.load_settings({name: value})
        assert settings[name] == expected, (name, value)


def test_an_unknown_mode_degrades_to_observe():
    assert tw.load_settings({"NETWD_TS_MODE": "LIVE "})[1] == "live"
    assert tw.load_settings({"NETWD_TS_MODE": "heal"})[1] == "observe"
    assert tw.load_settings({})[1] == "live"


def test_off_mode_does_nothing(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path, NETWD_TS_MODE="off"))
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"]) == ("off", {})
    assert world.calls == []


# ── the rate limit ───────────────────────────────────────────────────────


def test_one_restart_per_hour_then_the_next_is_allowed(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    c = ctx(world, tmp_path)
    tw.run_once(c)
    world.stuck(A)  # the fault comes back after the heal
    world.unit["ActiveEnterTimestampMonotonic"] = str(int(world.mono * 1e6))
    assert tick(world, c, 3599)["last_action"] == "ratelimited"
    assert world.restarts() == 1
    world.restart = (0, {"InvocationID": "inv-3"})
    tick(world, c, 2)
    assert world.restarts() == 2


def test_tailscaled_having_just_started_counts_as_spent(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.unit["ActiveEnterTimestampMonotonic"] = str(int((MONO - 60) * 1e6))
    tw.run_once(ctx(world, tmp_path))
    assert state(tmp_path)["last_action"] == "ratelimited"
    assert world.restarts() == 0


@pytest.mark.parametrize("value", ["", "0", "n/a"])
def test_an_unreadable_start_time_never_allows_a_restart(tmp_path, value):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.unit["ActiveEnterTimestampMonotonic"] = value
    tw.run_once(ctx(world, tmp_path))
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"]) == ("stuck", {A: "stuck"})
    assert world.restarts() == 0


def test_events_from_another_boot_do_not_spend_this_boots_hour(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    tw.run_once(ctx(world, tmp_path))
    raw = state(tmp_path)
    for e in raw["events"]:
        e["boot_id"] = "00000000-old"
    raw["boot_id"] = "00000000-old"
    (tmp_path / "state.json").write_text(json.dumps(raw))
    world.stuck(A)
    world.restart = (0, {"InvocationID": "inv-3"})
    s = tick(world, ctx(world, tmp_path), 10)
    assert world.restarts() == 2
    assert [e["boot_id"] for e in s["events"]] == [BOOT]
    assert s["heal_count"] == 1


def test_a_lost_or_corrupt_file_loses_nothing_but_old_events(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    c = ctx(world, tmp_path, NETWD_TS_MODE="observe")
    tw.run_once(c)
    (tmp_path / "state.json").write_text("{corrupt")
    assert tick(world, c)["evidence"] == {A: "stuck"}


# ── what a restart did: read from systemd, never from the exit code ──────


@pytest.mark.parametrize(
    ("restart", "fixes", "action", "exit_code"),
    [
        ((0, {"InvocationID": "inv-2"}), True, "healed", 0),
        ((1, {"InvocationID": "inv-2"}), True, "healed", 0),
        ((None, {"InvocationID": "inv-2"}), True, "healed", 0),
        ((0, {"InvocationID": "inv-2"}), False, "restart-no-effect", 1),
        ((0, {"InvocationID": "inv-2", "ActiveState": "failed"}), True, "restart-failed", 1),
        ((1, {}), True, "not-restarted", 1),
        ((None, {}), True, "not-restarted", 1),
        ((None, {"Job": "42 restart running", "ActiveState": "deactivating"}), True, "pending", 1),
        ((0, {"InvocationID": ""}), True, "unverified", 1),
    ],
)
def test_the_restart_outcome_table(tmp_path, restart, fixes, action, exit_code):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart = restart
    world.restart_fixes = fixes
    assert tw.run_once(ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0")) == exit_code
    s = state(tmp_path)
    assert s["last_action"] == action
    (event,) = s["events"]
    assert (event["action"], event["rc"]) == (action, restart[0])
    assert s["evidence"][A] == ("ok" if action == "healed" else "stuck")


def test_no_restart_when_its_outcome_could_not_be_judged(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.unit["InvocationID"] = ""
    assert tw.run_once(ctx(world, tmp_path)) == 0
    s = state(tmp_path)
    assert (s["last_action"], s["evidence"], s["events"]) == ("stuck", {A: "stuck"}, [])
    assert world.restarts() == 0


def test_a_restart_that_clears_only_some_tunnels_is_not_a_heal(tmp_path):
    world = World({"a": peer(A), "b": peer(B)})
    world.stuck(A, B)
    world.restart_fixes = False
    real = world.run

    def fixes_a(argv, timeout):
        if argv[1] == "try-restart":
            world.fixed(A)
        return real(argv, timeout)

    c = ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0")
    c.run = fixes_a
    tw.run_once(c)
    s = state(tmp_path)
    (event,) = s["events"]
    assert (event["action"], event["cleared"]) == ("restart-no-effect", [A])
    assert s["evidence"] == {A: "ok", B: "stuck"}


def test_a_tunnel_that_answers_a_little_after_the_restart_is_healed(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart_fixes = False
    real = world.run

    def slow_reconnect(argv, timeout):
        if argv[1] == "try-restart":
            world.tsmp[A] = [False, False, True]
        return real(argv, timeout)

    c = ctx(world, tmp_path)
    c.run = slow_reconnect
    tw.run_once(c)
    assert state(tmp_path)["last_action"] == "healed"


def test_a_restart_that_lands_during_the_poll_is_healed(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart = (None, {"Job": "42 restart running", "ActiveState": "activating"})
    settle_at = world.mono + 5
    real = world.run

    def show_then_settle(argv, timeout):
        if argv[1] == "show" and world.mono >= settle_at:
            world.unit.update({"Job": "", "ActiveState": "active", "InvocationID": "inv-2"})
        return real(argv, timeout)

    c = ctx(world, tmp_path)
    c.run = show_then_settle
    tw.run_once(c)
    assert state(tmp_path)["last_action"] == "healed"


def test_try_restart_on_a_stopped_unit_spends_nothing(tmp_path):
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart = (0, {"ActiveState": "inactive"})
    assert tw.run_once(ctx(world, tmp_path)) == 0
    s = state(tmp_path)
    assert (s["last_action"], s["events"]) == ("unavailable", [])


# ── the file ─────────────────────────────────────────────────────────────


def test_the_event_list_keeps_the_newest_fifty(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "MAX_INEFFECTIVE_RESTARTS", 10**6)
    world = World({"a": peer(A)})
    world.stuck(A)
    world.restart_fixes = False
    c = ctx(world, tmp_path, NETWD_TS_RATE_LIMIT_SEC="300", NETWD_TS_VERIFY_SEC="0")
    for n in range(55):
        world.restart = (0, {"InvocationID": f"inv-{n + 10}"})
        s = tick(world, c, 301)
    assert len(s["events"]) == 50
    assert s["events"][-1]["mono"] == world.mono


def test_consecutive_blind_runs_are_counted_and_reset(tmp_path):
    world = World({"a": peer(A, age=30)})
    world.status_rc = 1
    c = ctx(world, tmp_path)
    for expected in (1, 2, 3):
        assert tick(world, c)["blind_runs"] == expected
    world.status_rc = 0
    assert tick(world, c)["blind_runs"] == 0


def test_the_file_is_world_readable_and_leaves_no_temp_behind(tmp_path):
    world = World({"a": peer(A, age=30)})
    tw.run_once(ctx(world, tmp_path))
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_an_unwritable_file_fails_the_unit_loudly(tmp_path, capsys):
    world = World({"a": peer(A)})
    world.stuck(A)
    c = ctx(world, tmp_path, NETWD_TS_STATE_FILE=str(tmp_path / "missing-dir" / "state.json"))
    assert tw.run_once(c) == 1
    assert "the owner will NOT be told" in capsys.readouterr().out


def test_every_event_it_writes_passes_its_own_contract(tmp_path):
    world = World({"a": peer(A), "b": peer(B)})
    world.stuck(A, B)
    world.restart_fixes = False
    tw.run_once(ctx(world, tmp_path, NETWD_TS_VERIFY_SEC="0"))
    s = state(tmp_path)
    assert s["events"] and all(tw.valid_event(e) for e in s["events"])
    assert not tw.valid_event({**s["events"][0], "peers": ["peer-host"]})


# ── the real file, run as root would run it ──────────────────────────────


_TAILSCALE_STUB = """#!/bin/bash
case "$1" in
    status) cat "$STUB_DIR/status.json" ;;
    ping)
        if [[ " $* " == *" --tsmp "* ]]; then
            [ "$(cat "$STUB_DIR/inv" 2>/dev/null)" = inv-2 ] && exit 0
            exit 1
        fi
        exit 0 ;;
esac
"""

_SYSTEMCTL_STUB = """#!/bin/bash
inv="$(cat "$STUB_DIR/inv" 2>/dev/null || echo inv-1)"
case "$1" in
    show)
        shift 2
        while [ $# -gt 0 ]; do
            case "$2" in
                ActiveState) echo "ActiveState=active" ;;
                InvocationID) echo "InvocationID=$inv" ;;
                Job) echo "Job=" ;;
                ActiveEnterTimestampMonotonic) echo "ActiveEnterTimestampMonotonic=$STUB_START_US" ;;
            esac
            shift 2
        done ;;
    try-restart) echo inv-2 > "$STUB_DIR/inv" ;;
esac
"""


def test_the_real_file_heals_with_stub_binaries(tmp_path):
    # tailscaled must look started well over a rate-limit window ago on the
    # SAME clock the helper reads. A freshly booted machine (a CI runner)
    # cannot show that, and there the watchdog rightly declines to restart.
    mono = time.clock_gettime(time.CLOCK_MONOTONIC)
    if mono < 900:
        pytest.skip("this machine booted too recently to show a daemon that started long ago")
    for name, body in (("tailscale", _TAILSCALE_STUB), ("systemctl", _SYSTEMCTL_STUB)):
        stub = tmp_path / name
        stub.write_text(body)
        stub.chmod(0o755)
    (tmp_path / "status.json").write_text(
        json.dumps({"BackendState": "Running", "Peer": {"a": peer(A, age=10**7)}})
    )
    (tmp_path / "boot_id").write_text(BOOT + "\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "STUB_DIR": str(tmp_path),
        "STUB_START_US": str(int((mono - 600) * 1e6)),
        "NETWD_TS_RATE_LIMIT_SEC": "300",
        "NETWD_TAILSCALE_BIN": str(tmp_path / "tailscale"),
        "NETWD_SYSTEMCTL": str(tmp_path / "systemctl"),
        "NETWD_TS_STATE_FILE": str(tmp_path / "state.json"),
        "NETWD_TS_STATUS_SNAPSHOT": str(tmp_path / "snap.json"),
        "NETWD_BOOT_ID_FILE": str(tmp_path / "boot_id"),
    }
    result = subprocess.run(
        ["/usr/bin/python3", str(HELPER)], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "HEALING" in result.stdout
    s = json.loads((tmp_path / "state.json").read_text())
    assert (s["last_action"], s["boot_id"], s["evidence"]) == ("healed", BOOT, {A: "ok"})
