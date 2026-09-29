#!/usr/bin/env python3
"""genesis-tailscale-watchdog: heal a STUCK Tailscale tunnel.

Installed to /usr/local/lib/genesis/tailscale-watchdog.py and run as root by
genesis-tailscale-watchdog.timer (about every 2 min), via
scripts/lib/network_resilience.sh. Standard library only (Python 3.8+): it runs
under the host's /usr/bin/python3, never the Genesis venv.

The failure it targets (observed on a live install): every Tailscale SSH
session to the box timed out while nothing else looked wrong. The path to the
peer worked (discovery pings answered), but WireGuard handshakes stopped
completing, because the peer kept replying through a relay region this node had
left. Restarting tailscaled on this node cleared it.

A peer is STUCK when ALL of these hold:

* it is Active (traffic wanted) with a non-zero WireGuard handshake older than
  NETWD_TS_STALE_SEC (default 300s: WireGuard abandons a session after 180s
  without a handshake, and 300s spans two ticks). A handshake that reads as in
  the FUTURE means the wall clock stepped back; the age is then unknown, so
  the peer is probed and the pings decide;
* a ping through the tunnel (``tailscale ping --tsmp``) gets no reply;
* a discovery ping (``--until-direct=false``: a relayed pong counts) DOES
  reply, so the peer is reachable and only the tunnel is dead. A peer that is
  simply gone fails both and is left alone; and
* a second tunnel ping, after the discovery ping, still gets no reply (a
  discovery ping can itself revive a cold path).

The heal is ``systemctl try-restart tailscaled``. It drops every Tailscale SSH
session on the box, so it runs at most once per NETWD_TS_RATE_LIMIT_SEC
(default an hour), measured on CLOCK_MONOTONIC from the later of tailscaled's
own start and this watchdog's last spending event. What a restart did is read
back from systemd (InvocationID, ActiveState, pending Job), never inferred
from the command's exit code.

Output: ``/run/genesis-tailscale-watchdog.json`` (0644), rewritten atomically
each run. It carries the last run's state and a list of at most 50 events. The
Genesis runtime, running as the owning user, turns each event into an owner
alert (``genesis.resilience.tailscale_watchdog_events``). Nothing peer-chosen
goes into it: a peer is named by a validated IPv4 address only, and its
HostName is never read. At detection, the raw status is also kept as a
root-only (0600) snapshot beside it, as evidence.

Lever NETWD_TS_MODE (a drop-in on the service): live (default) | observe
(record, never restart) | off. An unknown value is treated as observe.
Disabling the timer turns the watchdog off entirely.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

STATE_FILE = "/run/genesis-tailscale-watchdog.json"
SNAPSHOT_FILE = "/run/genesis-tailscale-watchdog-status.json"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
MAX_EVENTS = 50
MODES = ("live", "observe", "off")

# Actions that are recorded as an event and spend the rate limit. A restart
# that did not happen (``not-restarted`` with exit 0: tailscaled was stopped
# when try-restart ran) is not an event and spends nothing.
EVENT_ACTIONS = frozenset(
    {
        "healed",
        "restart-no-effect",
        "restart-failed",
        "not-restarted",
        "unverified",
        "pending",
        "observed",
    }
)
# Runs that could not judge any tunnel. Counted across consecutive runs so the
# Genesis side can say the watchdog is blind, not only when it is silent.
BLIND_ACTIONS = frozenset({"unavailable", "status-unparseable"})
_TRANSITIONAL = frozenset({"activating", "deactivating", "reloading", "refreshing"})

# name: (default, min, max). Every bound is finite and positive where 0 would
# disable a rail (a 0 rate limit restarts every tick; a 0 timeout never fires).
# The two scan caps allow 0: probe nothing, which records "incomplete".
SETTINGS = {
    "NETWD_TS_STALE_SEC": (300, 60, 86400),
    "NETWD_TS_RATE_LIMIT_SEC": (3600, 300, 7 * 86400),
    "NETWD_TS_PING_TIMEOUT_SEC": (3, 1, 30),
    # A ping answers in milliseconds or gives up at the ping timeout, and
    # `status` returns in well under a second, so this is reached only by a
    # hung local API.
    "NETWD_TS_CALL_TIMEOUT_SEC": (20, 5, 120),
    # try-restart waits for the job. tailscaled's default stop and start
    # timeouts are 90s each, so 200s covers both; a job still queued after
    # this plus the poll is recorded as "pending".
    "NETWD_TS_RESTART_TIMEOUT_SEC": (200, 30, 900),
    "NETWD_TS_POLL_SEC": (30, 0, 300),
    # After a restart, how long to keep pinging the stuck peer through the
    # tunnel before calling the restart ineffective. tailscaled reconnects in
    # seconds; a minute leaves room for its coordination-server round trip.
    "NETWD_TS_VERIFY_SEC": (60, 0, 300),
    # Normally 0 or 1 peers are suspects; 3 covers a shared outage without
    # letting a large tailnet hold the oneshot for minutes.
    "NETWD_TS_MAX_PROBES": (3, 0, 20),
    "NETWD_TS_SCAN_BUDGET_SEC": (60, 0, 600),
}

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def log(msg: str) -> None:
    print(f"genesis-tailscale-watchdog: {msg}", flush=True)


def load_settings(env) -> tuple[dict, str]:
    """(settings, mode) from the environment. Anything invalid or out of range
    falls back to its default with a log line, so a drop-in typo can never
    remove a bound or abort the run."""
    out = {}
    for name, (default, lo, hi) in SETTINGS.items():
        raw = env.get(name)
        if raw is None or not raw.strip():
            out[name] = default
            continue
        try:
            value = int(raw.strip(), 10)
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            log(f"ignoring invalid {name}={raw!r} (allowed {lo}..{hi}); using {default}")
            value = default
        out[name] = value
    mode = (env.get("NETWD_TS_MODE") or "live").strip().lower()
    if mode not in MODES:
        log(f"unknown NETWD_TS_MODE={mode!r}; treating as observe")
        mode = "observe"
    return out, mode


def default_run(argv, timeout):
    """(returncode, stdout). returncode is None when the command could not run
    or hit its timeout. stdout is read whole: the largest is ``status --json``,
    whose size the coordination server bounds by the tailnet's own peers."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


class Ctx:
    """Everything the run touches in the outside world, so tests replace it."""

    def __init__(
        self,
        *,
        env=None,
        run=None,
        mono=None,
        wall=None,
        sleep=None,
        boot_id=None,
        which=None,
    ):
        env = os.environ if env is None else env
        self.settings, self.mode = load_settings(env)
        self.run = run or default_run
        self.mono = mono or (lambda: time.clock_gettime(time.CLOCK_MONOTONIC))
        self.wall = wall or time.time
        self.sleep = sleep or time.sleep
        self.which = which or shutil.which
        self.tailscale = env.get("NETWD_TAILSCALE_BIN") or "tailscale"
        self.systemctl = env.get("NETWD_SYSTEMCTL") or "systemctl"
        self.state_file = env.get("NETWD_TS_STATE_FILE") or STATE_FILE
        self.snapshot_file = env.get("NETWD_TS_STATUS_SNAPSHOT") or SNAPSHOT_FILE
        self.boot_id = (
            boot_id
            if boot_id is not None
            else _read_boot_id(env.get("NETWD_BOOT_ID_FILE") or BOOT_ID_FILE)
        )


def _read_boot_id(path: str) -> str:
    try:
        with open(path) as fh:
            value = fh.read().strip()
    except OSError:
        return "unknown"
    return value if re.fullmatch(r"[0-9a-fA-F-]{8,64}", value) else "unknown"


# ── systemd ──────────────────────────────────────────────────────────────


def unit_props(ctx: Ctx, unit: str, *props: str) -> dict | None:
    """``systemctl show`` properties as a dict, or None if systemctl failed."""
    argv = [ctx.systemctl, "show", unit]
    for prop in props:
        argv += ["-p", prop]
    rc, out = ctx.run(argv, ctx.settings["NETWD_TS_CALL_TIMEOUT_SEC"])
    if rc != 0:
        return None
    found = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in props:
            found[key] = value.strip()
    return found


def _started_mono(ctx: Ctx) -> float:
    """tailscaled's last start, in CLOCK_MONOTONIC seconds (0 when unknown).
    systemd writes it on every start, so it records a restart this watchdog
    made even if the watchdog's own record of it was lost, as well as reboots
    and an operator's manual restart."""
    props = unit_props(ctx, "tailscaled", "ActiveEnterTimestampMonotonic") or {}
    try:
        return int(props.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
    except ValueError:
        return 0.0


def restart_tailscaled(ctx: Ctx) -> tuple[str, int | None]:
    """Run ``try-restart tailscaled`` and return (outcome, exit code).

    The outcome is read from systemd's state, never from the exit code: a
    killed or failing client proves nothing about whether a restart happened.

    * InvocationID unreadable BEFORE → ``not-attempted`` (nothing is run: a
      restart whose outcome could not be judged is not worth the dropped sessions)
    * InvocationID unreadable after → ``unverified``
    * a job still queued after the poll → ``pending``
    * a new InvocationID, unit active → ``healed``
    * a new InvocationID, unit not active → ``restart-failed``
    * the same InvocationID → ``not-restarted`` (nothing restarted)

    ``try-restart`` rather than ``restart``: restart STARTS a stopped unit, and
    an operator may have stopped tailscaled during the scan.
    """
    props = ("InvocationID", "ActiveState", "Job")
    before = (unit_props(ctx, "tailscaled", *props) or {}).get("InvocationID", "")
    if not before:
        return "not-attempted", None
    rc, _ = ctx.run(
        [ctx.systemctl, "try-restart", "tailscaled"],
        ctx.settings["NETWD_TS_RESTART_TIMEOUT_SEC"],
    )
    deadline = ctx.mono() + ctx.settings["NETWD_TS_POLL_SEC"]
    while True:
        after = unit_props(ctx, "tailscaled", *props) or {}
        settled = not after.get("Job") and after.get("ActiveState") not in _TRANSITIONAL
        if settled or ctx.mono() >= deadline:
            break
        ctx.sleep(1)
    if not after.get("InvocationID"):
        return "unverified", rc
    if after.get("Job") or after.get("ActiveState") in _TRANSITIONAL:
        return "pending", rc
    if after["InvocationID"] != before:
        return ("healed" if after.get("ActiveState") == "active" else "restart-failed"), rc
    return "not-restarted", rc


# ── status parsing ───────────────────────────────────────────────────────


class Suspect:
    __slots__ = ("ip", "handshake", "age")

    def __init__(self, ip: str, handshake: str, age: int | None):
        self.ip = ip
        self.handshake = handshake  # tailscaled's own value; hashed, never stored
        self.age = age  # None: age unknown (never handshaked, or the clock stepped back)


def _parse_handshake(raw: str) -> float:
    """Epoch seconds for Go's RFC 3339 time. Raises ValueError if unparseable.
    Go prints 0-9 fractional digits and Python before 3.11 accepts only 3 or
    6, so the fraction is normalised to 6 first."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("handshake has no timezone")
    return parsed.timestamp()


def _suspect(peer, now: float, stale: int, zero_ok: bool) -> Suspect | None:
    """One peer's verdict. Raises TypeError/ValueError on a malformed entry; the
    caller skips that peer only. Reads Active, LastHandshake and TailscaleIPs,
    and nothing else: every other field is chosen by another tailnet member.

    A zero handshake (0001-01-01) is a suspect only when ``zero_ok``: tailscaled
    has been up longer than the stale age. A fresh daemon has handshaked with
    nobody yet, but a peer that traffic wants and that has still not completed
    one long after is exactly the stuck state; after a restart that did not
    clear the fault, it is the only form the fault takes."""
    if not isinstance(peer, dict):
        raise TypeError("peer is not an object")
    if peer.get("Active") is not True:
        return None
    raw = peer.get("LastHandshake")
    if not isinstance(raw, str):
        raise TypeError("LastHandshake is not a string")
    handshake = _parse_handshake(raw)
    ips = peer.get("TailscaleIPs")
    if not isinstance(ips, list):
        raise TypeError("TailscaleIPs is not a list")
    ip = None
    for candidate in ips:
        if isinstance(candidate, str) and _IPV4_RE.match(candidate):
            try:
                ip = str(ipaddress.IPv4Address(candidate))
            except ValueError:
                continue
            break
    if ip is None:
        return None
    if handshake <= 0:
        return Suspect(ip, raw, None) if zero_ok else None
    age = now - handshake
    if age < 0:
        return Suspect(ip, raw, None)
    if age > stale:
        return Suspect(ip, raw, int(age))
    return None


def find_suspects(status_text: str, now: float, stale: int, zero_ok: bool = False):
    """(suspects, malformed peer count), or None when the status cannot be read
    as a peer map at all (recorded as its own state, never as healthy)."""
    try:
        status = json.loads(status_text)
    except ValueError:
        return None
    if not isinstance(status, dict):
        return None
    peers = status.get("Peer")
    if peers is None:  # null: no peers, a legitimate empty tailnet
        return [], 0
    if not isinstance(peers, dict):
        return None
    suspects, malformed = [], 0
    for peer in peers.values():
        try:
            found = _suspect(peer, now, stale, zero_ok)
        except (TypeError, ValueError, OverflowError):
            malformed += 1
            continue
        if found is not None:
            suspects.append(found)
    return suspects, malformed


# ── probing ──────────────────────────────────────────────────────────────


def _ping(ctx: Ctx, ip: str, *, tsmp: bool) -> bool:
    kind = ["--tsmp"] if tsmp else ["--until-direct=false"]
    timeout = ctx.settings["NETWD_TS_PING_TIMEOUT_SEC"]
    rc, _ = ctx.run(
        [ctx.tailscale, "ping", *kind, "-c", "1", "--timeout", f"{timeout}s", "--", ip],
        ctx.settings["NETWD_TS_CALL_TIMEOUT_SEC"],
    )
    return rc == 0


def scan(ctx: Ctx, suspects: list) -> tuple[Suspect | None, int, int, bool]:
    """(stuck peer or None, probed, skipped, saw an unreachable peer).

    Bounded by NETWD_TS_MAX_PROBES peers and NETWD_TS_SCAN_BUDGET_SEC seconds.
    The status lists peers in a fixed order, so the start point rotates each
    tick; otherwise the same first few would be probed forever."""
    n = len(suspects)
    offset = int(ctx.mono() // 120) % n if n else 0
    start = ctx.mono()
    probed = skipped = 0
    unreachable = False
    for k in range(n):
        peer = suspects[(offset + k) % n]
        if (
            probed >= ctx.settings["NETWD_TS_MAX_PROBES"]
            or ctx.mono() - start >= ctx.settings["NETWD_TS_SCAN_BUDGET_SEC"]
        ):
            skipped += 1
            continue
        probed += 1
        if _ping(ctx, peer.ip, tsmp=True):
            continue
        if not _ping(ctx, peer.ip, tsmp=False):
            unreachable = True
            continue
        if _ping(ctx, peer.ip, tsmp=True):
            continue
        return peer, probed, skipped, unreachable
    return None, probed, skipped, unreachable


# ── state file ───────────────────────────────────────────────────────────


def valid_event(event) -> bool:
    """Shape check for one event. The consumer
    (genesis.resilience.tailscale_watchdog_events) applies the same contract."""
    if not isinstance(event, dict):
        return False
    try:
        return (
            isinstance(event["id"], str)
            and len(event["id"]) <= 128
            and isinstance(event["incident"], str)
            and bool(_HEX32_RE.match(event["incident"]))
            and event["action"] in EVENT_ACTIONS
            and isinstance(event["boot_id"], str)
            and isinstance(event["mono"], (int, float))
            and not isinstance(event["mono"], bool)
            and isinstance(event["at"], int)
            and not isinstance(event["at"], bool)
            and isinstance(event["peer_ip"], str)
            and str(ipaddress.IPv4Address(event["peer_ip"])) == event["peer_ip"]
            and (event["handshake_age_s"] is None or type(event["handshake_age_s"]) is int)
            and (event["rc"] is None or type(event["rc"]) is int)
            and type(event["rate_limit_s"]) is int
        )
    except (KeyError, TypeError, ValueError):
        return False


def read_state(path: str) -> dict:
    """The prior run's file as a dict (empty when absent or unreadable), with
    only well-formed events kept."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1_000_001)
        state = json.loads(raw) if len(raw) <= 1_000_000 else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    events = state.get("events")
    state["events"] = [e for e in events if valid_event(e)] if isinstance(events, list) else []
    return state


def write_atomic(path: str, data: bytes, mode: int) -> None:
    """Write via a fresh temp file in the same directory, then rename, so a
    reader never sees a half-written file and an existing file's mode is never
    inherited. The temp file is removed on any failure."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".genesis-tailscale-watchdog.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def incident_id(boot_id: str, peer: Suspect) -> str:
    """Stable for one stuck incident: the peer's handshake does not move while
    its tunnel is stuck. So every detection of it, healed or observed, carries
    the same id, and the owner is paged once per incident."""
    material = f"{boot_id}|{peer.ip}|{peer.handshake}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


# ── the run ──────────────────────────────────────────────────────────────


def _tunnel_answers(ctx: Ctx, ip: str) -> bool:
    """Whether the peer answers through the tunnel within NETWD_TS_VERIFY_SEC
    of a restart. tailscaled needs a few seconds to reconnect, so this retries."""
    deadline = ctx.mono() + ctx.settings["NETWD_TS_VERIFY_SEC"]
    while True:
        if _ping(ctx, ip, tsmp=True):
            return True
        if ctx.mono() >= deadline:
            return False
        ctx.sleep(2)


def run_once(ctx: Ctx) -> int:
    state = read_state(ctx.state_file)
    events = [e for e in state["events"] if e["boot_id"] == ctx.boot_id]
    heal_count = state.get("heal_count") if state.get("boot_id") == ctx.boot_id else 0
    if type(heal_count) is not int or heal_count < 0:
        heal_count = 0
    counters = {"probed": 0, "skipped": 0, "malformed_peers": 0}
    blind_runs = state.get("blind_runs") if state.get("boot_id") == ctx.boot_id else 0
    if type(blind_runs) is not int or blind_runs < 0:
        blind_runs = 0

    def finish(action: str, event: dict | None = None, exit_code: int = 0) -> int:
        nonlocal events, heal_count
        runs = blind_runs + 1 if action in BLIND_ACTIONS else 0
        if event is not None:
            events = (events + [event])[-MAX_EVENTS:]
            if event["action"] == "healed":
                heal_count += 1
        record = {
            "version": 1,
            "boot_id": ctx.boot_id,
            "last_check": int(ctx.wall()),
            "last_check_mono": ctx.mono(),
            "last_action": action,
            "mode": ctx.mode,
            "heal_count": heal_count,
            "blind_runs": runs,
            **counters,
            "events": events,
        }
        try:
            write_atomic(ctx.state_file, json.dumps(record).encode(), 0o644)
        except OSError as exc:
            log(
                f"could not record '{action}' in {ctx.state_file} ({exc}); the owner will NOT be told of it"
            )
            return 1
        return exit_code

    if ctx.mode == "off":
        return finish("off")
    if not ctx.which(ctx.tailscale):
        return finish("unavailable")
    active = unit_props(ctx, "tailscaled", "ActiveState") or {}
    if active.get("ActiveState") != "active":
        return finish("unavailable")

    rc, status = ctx.run(
        [ctx.tailscale, "status", "--json"], ctx.settings["NETWD_TS_CALL_TIMEOUT_SEC"]
    )
    if rc != 0 or not status.strip():
        return finish("unavailable")
    stale = ctx.settings["NETWD_TS_STALE_SEC"]
    started = _started_mono(ctx)
    found = find_suspects(
        status, ctx.wall(), stale, zero_ok=started > 0 and ctx.mono() - started > stale
    )
    if found is None:
        log("status --json could not be read as a peer map; cannot judge tunnels this run")
        return finish("status-unparseable")
    suspects, counters["malformed_peers"] = found
    if counters["malformed_peers"]:
        log(f"skipped {counters['malformed_peers']} malformed peer entr(y/ies) in status --json")

    stuck, counters["probed"], counters["skipped"], unreachable = scan(ctx, suspects)
    if counters["skipped"]:
        log(
            f"probed {counters['probed']} suspect peer(s); {counters['skipped']} not probed this run "
            f"(cap {ctx.settings['NETWD_TS_MAX_PROBES']} / {ctx.settings['NETWD_TS_SCAN_BUDGET_SEC']}s)"
        )
    if stuck is None:
        # A suspect the cap left unprobed could be the stuck one, so a capped
        # scan never reads as healthy.
        if counters["skipped"]:
            return finish("incomplete")
        return finish("suspect-unreachable" if unreachable else "none")

    # Evidence before anything changes. Root-only: it names every node.
    try:
        write_atomic(ctx.snapshot_file, status.encode(), 0o600)
    except OSError as exc:
        log(f"could not save the status snapshot ({exc})")

    age = "unknown" if stuck.age is None else f"{stuck.age}s"
    rate = ctx.settings["NETWD_TS_RATE_LIMIT_SEC"]
    now = ctx.mono()
    spent = max([e["mono"] for e in events if e["action"] in EVENT_ACTIONS] + [started])
    if spent > 0 and now - spent < rate:
        log(
            f"tunnel to {stuck.ip} stuck (handshake age {age}) but tailscaled started, or this "
            f"watchdog acted, <{rate}s ago; NOT acting"
        )
        return finish("ratelimited")

    def event(action: str, rc: int | None) -> dict:
        return {
            "id": f"{ctx.boot_id}:{int(ctx.mono() * 1e9)}",
            "incident": incident_id(ctx.boot_id, stuck),
            "action": action,
            "boot_id": ctx.boot_id,
            "mono": now,
            "at": int(ctx.wall()),
            "peer_ip": stuck.ip,
            "handshake_age_s": stuck.age,
            "rc": rc,
            "rate_limit_s": rate,
        }

    if ctx.mode == "observe":
        log(f"OBSERVE: tunnel to {stuck.ip} stuck (handshake age {age}); not restarting")
        return finish("observed", event("observed", None))

    log(f"HEALING: tunnel to {stuck.ip} stuck (handshake age {age}); restarting tailscaled")
    outcome, rc = restart_tailscaled(ctx)
    if outcome == "not-attempted":
        log(
            "could not read tailscaled's InvocationID, so a restart could not be judged; not restarting"
        )
        return finish("unavailable")
    if outcome == "healed" and not _tunnel_answers(ctx, stuck.ip):
        # tailscaled came back, but the tunnel it was restarted for did not.
        outcome = "restart-no-effect"
    if outcome == "not-restarted" and rc == 0:
        # try-restart is a no-op on a stopped unit: nothing was restarted and
        # no session dropped, so nothing is spent.
        log("tailscaled was stopped during the scan; not restarted")
        return finish("unavailable")
    messages = {
        "healed": "tailscaled restarted and the tunnel answers again",
        "restart-no-effect": "tailscaled restarted, but the tunnel still gets no reply; not retrying",
        "restart-failed": "tailscaled restarted but is NOT active; it may be DOWN; not retrying",
        "not-restarted": f"try-restart failed (rc={rc}) and nothing restarted; not retrying for {rate}s",
        "unverified": "could not read tailscaled's InvocationID; the restart's outcome is unknown",
        "pending": "the restart job had not finished when the poll ended; tailscaled may be hung",
    }
    log(messages[outcome])
    return finish(outcome, event(outcome, rc), 0 if outcome == "healed" else 1)


def main() -> int:
    return run_once(Ctx())


if __name__ == "__main__":
    sys.exit(main())
