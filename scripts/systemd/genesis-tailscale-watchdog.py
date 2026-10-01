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
each run and read by the Genesis runtime as the owning user
(``genesis.resilience.tailscale_watchdog_events``). It holds no condition of its
own, only what THIS run observed:

* ``evidence``: a verdict per peer this run could judge. ``ok`` (a handshake
  within the stale age, or the tunnel answered), ``stuck`` (all four probes
  above, this run), ``offline`` (the discovery ping failed too). A peer it
  could not judge (unprobed, idle, freshly started daemon) is simply absent:
  unknown.
* ``present``: every well-formed peer's IPv4, and whether that list is
  complete, so a peer that left the tailnet can be told apart from one this
  run skipped.
* ``events``: at most 50 restart outcomes.
* ``unhelped_restarts``: per peer, how many restarts this boot dropped every
  SSH session without a verified heal (no effect, not checkable, outcome
  unreadable or still pending, or the daemon not coming back). The only thing
  remembered about a peer between runs, only to stop restarting for a fault a
  restart does not fix, and forgotten once the peer is seen working.

The condition, "this peer's tunnel is stuck", lives in Genesis's open alert
for that peer: raised on ``stuck``, resolved only on ``ok`` or ``offline``
evidence (or on absence from a complete list), untouched when unknown. There
is no saved set here to lose, go stale, or keep a peer that went offline: only
peers confirmed stuck in THIS run are ever restarted. A peer that three
restarts this boot did not verifiably clear is not restarted for again until
it is seen working, and a run whose backend is not Running, or whose pings
cannot be judged, reports nothing about the peers it could not judge. A run
during which tailscaled itself restarted or stopped reports nothing either: its
verdicts describe a daemon that is gone. That is checked once after the scan
and once more immediately before the restart.

Nothing peer-chosen goes into it: a peer is named by a validated IPv4 address
only, and its HostName is never read. When a peer is first confirmed stuck,
the raw status is also kept as a root-only (0600) snapshot beside it, as
evidence.

Lever NETWD_TS_MODE (a drop-in on the service): live (default) | observe
(record, never restart) | off. An unknown value is treated as observe. The
drop-in is the durable off switch: the installer never touches drop-ins, but it
re-enables a disabled timer, and no mask works on a unit whose file is in
/etc/systemd/system: ``mask`` refuses it, and a ``mask --runtime`` symlink in
/run is shadowed by that file.
"""

from __future__ import annotations

import contextlib
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
# Bounds on what one run records, so a hostile or huge tailnet cannot grow the
# file without limit: peers named in one restart event, and peers listed in
# ``evidence`` / ``present`` (a larger tailnet is recorded as incomplete).
MAX_TARGETS = 20
MAX_PEERS = 1000
# Every restart drops every SSH session. A peer gets at most this many restarts
# per boot that did not end in a verified heal, whatever the outcome (no effect,
# a check that could not be judged, an unreadable or pending outcome, a daemon
# that did not come back). The count resets once the peer is seen working. Its
# alert stays open meanwhile.
MAX_UNHELPED_RESTARTS = 3
MODES = ("live", "observe", "off")

# Restart outcomes: each is recorded as an event and spends the rate limit. A
# restart that did not happen (``not-restarted`` with exit 0: tailscaled was
# stopped when try-restart ran) is not an event and spends nothing.
EVENT_ACTIONS = frozenset(
    {
        "healed",
        "restart-no-effect",
        "restart-unconfirmed",
        "restart-failed",
        "not-restarted",
        "unverified",
        "pending",
    }
)
# Runs that judged no tunnel. Counted across consecutive runs so the Genesis
# side can say the watchdog is blind, not only when it is silent. A run that
# had suspects and probed none of them counts too (see run_once).
# A run whose verdicts were voided because tailscaled restarted or stopped
# under it judged nothing it could keep, so it counts too: a crash-looping
# tailscaled must not reset the count on every other run.
BLIND_ACTIONS = frozenset({"unavailable", "status-unparseable", "daemon-changed"})
# tailscaled turned off on purpose (a disabled or masked unit, or `tailscale
# down`): nothing to watch, so not blind. A crashed but enabled unit, or a
# logged-out node, is still blind.
_DAEMON_OFF_STATES = frozenset({"disabled", "masked", "masked-runtime"})
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
    # After a restart, how long to keep pinging the stuck peers through the
    # tunnel before calling the restart ineffective. tailscaled reconnects in
    # seconds; a minute leaves room for its coordination-server round trip. It
    # bounds the WHOLE check (no peer starts a ping after it), so it must leave
    # room for at least one.
    "NETWD_TS_VERIFY_SEC": (60, 10, 300),
    # Normally 0 or 1 peers are suspects; 3 covers a shared outage without
    # letting a large tailnet hold the oneshot for minutes.
    "NETWD_TS_MAX_PROBES": (3, 0, 20),
    "NETWD_TS_SCAN_BUDGET_SEC": (60, 0, 600),
}

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


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


def default_run(argv, timeout, merge_stderr=False):
    """(returncode, output). returncode is None when the command could not run
    or hit its timeout. The output is stdout, with stderr interleaved into it
    when ``merge_stderr`` (never for ``status --json``, whose stdout must
    parse). It
    is read whole: the largest is ``status --json``, whose size the
    coordination server bounds by the tailnet's own peers."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
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


_IDENTITY = ("InvocationID", "ActiveState", "ActiveEnterTimestampMonotonic")


def _identity(props: dict | None) -> tuple | None:
    """(InvocationID, ActiveState, start in CLOCK_MONOTONIC seconds) from
    ``systemctl show`` properties, or None when any is unreadable. The start is
    written by systemd on every start, so it records a restart this watchdog
    made even if its own record was lost, as well as reboots and an operator's
    restart. Unknown is never read as "long ago": callers fail closed."""
    if not props or not props.get("InvocationID"):
        return None
    try:
        start = int(props.get("ActiveEnterTimestampMonotonic", ""))
    except ValueError:
        return None
    if start <= 0:
        return None
    return props["InvocationID"], props.get("ActiveState", ""), start / 1e6


def daemon_identity(ctx: Ctx) -> tuple | None:
    return _identity(unit_props(ctx, "tailscaled", *_IDENTITY))


def same_daemon(expected: tuple, now: tuple | None) -> bool:
    """Whether ``now`` is the same running tailscaled as ``expected``: same
    invocation, same start, still active. Unreadable is not the same."""
    return (
        now is not None and now[0] == expected[0] and now[2] == expected[2] and now[1] == "active"
    )


def restart_tailscaled(ctx: Ctx, expected: tuple) -> tuple[str, int | None]:
    """Run ``try-restart tailscaled`` and return (outcome, exit code).

    The outcome is read from systemd's state, never from the exit code: a
    killed or failing client proves nothing about whether a restart happened.

    * the daemon read immediately BEFORE is not ``expected`` (the one the scan
      judged: another invocation, another start, or no longer active) →
      ``daemon-changed``; nothing is run, the verdicts describe a daemon that
      is gone
    * that read unreadable → ``not-attempted`` (nothing is run: a restart
      whose outcome could not be judged is not worth the dropped sessions)
    * InvocationID unreadable after → ``unverified``
    * a job still queued after the poll → ``pending``
    * a new InvocationID, unit active → ``healed``
    * a new InvocationID, unit not active → ``restart-failed``
    * the same InvocationID → ``not-restarted`` (nothing restarted)

    ``try-restart`` rather than ``restart``: restart STARTS a stopped unit, and
    an operator may have stopped tailscaled during the scan.
    """
    now = daemon_identity(ctx)
    if now is None:
        return "not-attempted", None
    if not same_daemon(expected, now):
        return "daemon-changed", None
    before = now[0]
    props = ("InvocationID", "ActiveState", "Job")
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
    __slots__ = ("ip", "age")

    def __init__(self, ip: str, age: int | None):
        self.ip = ip
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


def _classify(peer, now: float, stale: int, zero_ok: bool):
    """One peer: ("suspect", Suspect), ("ok", ip) or None (idle or unjudgeable).
    Raises TypeError/ValueError on a malformed entry; the caller skips that peer
    only. Reads Active, LastHandshake and TailscaleIPs, and nothing else: every
    other field is chosen by another tailnet member.

    A handshake within the stale age is evidence the tunnel works: that is
    exactly what the stuck state lacks. A zero handshake (0001-01-01) is a
    suspect only when ``zero_ok`` (tailscaled has been up longer than the stale
    age): a fresh daemon has handshaked with nobody yet, but a peer that traffic
    wants and that still has none long after is the stuck state; after a restart
    that did not clear the fault, it is the only form the fault takes. A
    handshake dated in the future means the wall clock stepped back: probed."""
    if not isinstance(peer, dict):
        raise TypeError("peer is not an object")
    raw = peer.get("LastHandshake")
    if not isinstance(raw, str):
        raise TypeError("LastHandshake is not a string")
    handshake = _parse_handshake(raw)
    ips = peer.get("TailscaleIPs")
    if not isinstance(ips, list):
        raise TypeError("TailscaleIPs is not a list")
    ip = _first_ipv4(ips)
    if ip is None:
        return None
    if peer.get("Active") is not True:
        # Idle: no traffic wanted, so nothing to probe. A fresh handshake is
        # still evidence the tunnel works (an operator fixed it by hand, say).
        fresh = handshake > 0 and 0 <= now - handshake <= stale
        return ("ok", ip) if fresh else None
    if handshake <= 0:
        return ("suspect", Suspect(ip, None)) if zero_ok else None
    age = now - handshake
    if age < 0:
        return "suspect", Suspect(ip, None)
    if age > stale:
        return "suspect", Suspect(ip, int(age))
    return "ok", ip


def _first_ipv4(ips: list) -> str | None:
    for candidate in ips:
        if isinstance(candidate, str) and _IPV4_RE.match(candidate):
            try:
                return str(ipaddress.IPv4Address(candidate))
            except ValueError:
                continue
    return None


def read_peers(status_text: str, now: float, stale: int, zero_ok: bool = False):
    """(suspects, IPv4s with a fresh handshake, malformed peer count, IPv4s of
    every well-formed peer), or None when the status cannot be read as a peer
    map at all (recorded as its own state, never as healthy). A MISSING
    ``Peer`` key is unreadable; an explicit null is a legitimate empty tailnet,
    but only on a backend that is Running: anything else returns
    ("not-running", BackendState)."""
    try:
        status = json.loads(status_text)
    except ValueError:
        return None
    if not isinstance(status, dict) or "Peer" not in status:
        return None
    backend = status.get("BackendState")
    if backend != "Running":
        # Logged out, key expired, `tailscale down`, or still starting: the peer
        # map is empty or partial and proves nothing about any tunnel.
        return "not-running", backend if isinstance(backend, str) else ""
    peers = status["Peer"]
    if peers is None:
        return [], [], 0, set()
    if not isinstance(peers, dict):
        return None
    suspects, ok, malformed, present = [], [], 0, set()
    for peer in peers.values():
        try:
            verdict = _classify(peer, now, stale, zero_ok)
            ip = _first_ipv4(peer["TailscaleIPs"])
        except (TypeError, ValueError, OverflowError, KeyError):
            malformed += 1
            continue
        if ip is not None:
            present.add(ip)
        if verdict is None:
            continue
        if verdict[0] == "ok":
            ok.append(verdict[1])
        else:
            suspects.append(verdict[1])
    return suspects, ok, malformed, present


# ── probing ──────────────────────────────────────────────────────────────


def _ping(ctx: Ctx, ip: str, *, tsmp: bool) -> bool | None:
    """True: a reply. False: no reply. None: the ping itself could not be
    judged, which is never evidence about the peer.

    The CLI exits 1 for MANY failures, not only a missing reply: it cannot
    reach the local tailscaled (a restart in progress), the backend is not
    running, no peer has that address. Its only no-reply path prints
    ``ping "<ip>" timed out`` per attempt and then fails with the error
    ``no reply`` (tailscale v1.102.4, cmd/tailscale/cli/ping.go, runPing), so
    exit 1 counts as no reply only when that is the last line it printed."""
    kind = ["--tsmp"] if tsmp else ["--until-direct=false"]
    timeout = ctx.settings["NETWD_TS_PING_TIMEOUT_SEC"]
    rc, out = ctx.run(
        [ctx.tailscale, "ping", *kind, "-c", "1", "--timeout", f"{timeout}s", "--", ip],
        ctx.settings["NETWD_TS_CALL_TIMEOUT_SEC"],
        merge_stderr=True,
    )
    if rc == 0:
        return True
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    return False if rc == 1 and lines and lines[-1] == "no reply" else None


def scan(ctx: Ctx, suspects: list) -> tuple[dict, list, int, int, int]:
    """({ip: "ok" | "offline" | "stuck"} for every suspect judged, the stuck
    Suspects, probed, unjudged, skipped).

    A suspect whose pings could not be judged is left out of the verdicts and
    counted as unjudged. Bounded by NETWD_TS_MAX_PROBES peers and
    NETWD_TS_SCAN_BUDGET_SEC seconds. The status lists peers in a fixed order,
    so the start point rotates each tick; otherwise the same first few would
    be probed forever."""
    n = len(suspects)
    offset = int(ctx.mono() // 120) % n if n else 0
    start = ctx.mono()
    probed = unjudged = skipped = 0
    verdicts: dict = {}
    stuck = []
    for k in range(n):
        peer = suspects[(offset + k) % n]
        if (
            probed >= ctx.settings["NETWD_TS_MAX_PROBES"]
            or ctx.mono() - start >= ctx.settings["NETWD_TS_SCAN_BUDGET_SEC"]
        ):
            skipped += 1
            continue
        probed += 1
        verdict = _judge(ctx, peer.ip)
        if verdict is None:
            unjudged += 1
            continue
        verdicts[peer.ip] = verdict
        if verdict == "stuck":
            stuck.append(peer)
    return verdicts, stuck, probed, unjudged, skipped


def _judge(ctx: Ctx, ip: str) -> str | None:
    """The four-probe verdict for one suspect, or None if any ping could not be
    judged."""
    first = _ping(ctx, ip, tsmp=True)
    if first is None:
        return None
    if first:
        return "ok"
    disco = _ping(ctx, ip, tsmp=False)
    if disco is None:
        return None
    if not disco:
        return "offline"  # gone, not stuck: a restart cannot help
    again = _ping(ctx, ip, tsmp=True)
    if again is None:
        return None
    return "ok" if again else "stuck"  # a discovery ping can revive a cold path


def _check_tunnels(ctx: Ctx, targets: list, deadline: float) -> dict:
    """{ip: True | False | None} for whether each peer answers through the
    tunnel by ``deadline``: True, False (pinged, judged, never answered), or
    None (no ping to it could be judged, or it was never reached). After a
    restart tailscaled needs a few seconds to reconnect, so this retries,
    pinging the peers still waiting IN TURN so one dead tunnel cannot use the
    whole window. The deadline is checked before every ping, so it bounds the
    whole check."""
    answers: dict = {ip: None for ip in targets}
    waiting = list(targets)
    while waiting and ctx.mono() < deadline:
        for ip in list(waiting):
            if ctx.mono() >= deadline:
                break
            answer = _ping(ctx, ip, tsmp=True)
            if answer:
                answers[ip] = True
                waiting.remove(ip)
            elif answer is False:
                answers[ip] = False
        if waiting and ctx.mono() < deadline:
            ctx.sleep(2)
    return answers


# ── state file ───────────────────────────────────────────────────────────


def _valid_ip(value) -> bool:
    try:
        return isinstance(value, str) and str(ipaddress.IPv4Address(value)) == value
    except ValueError:
        return False


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def valid_event(event) -> bool:
    """Shape check for one restart event. The consumer
    (genesis.resilience.tailscale_watchdog_events) applies the same contract."""
    if not isinstance(event, dict):
        return False
    try:
        return (
            isinstance(event["id"], str)
            and len(event["id"]) <= 128
            and event["action"] in EVENT_ACTIONS
            and isinstance(event["boot_id"], str)
            and _number(event["mono"])
            and type(event["at"]) is int
            and isinstance(event["peers"], list)
            and 0 < len(event["peers"]) <= MAX_TARGETS
            and all(_valid_ip(ip) for ip in event["peers"])
            and isinstance(event["cleared"], list)
            and all(ip in event["peers"] for ip in event["cleared"])
            and isinstance(event.get("unconfirmed", []), list)
            and all(ip in event["peers"] for ip in event.get("unconfirmed", []))
            and (event["rc"] is None or type(event["rc"]) is int)
            and type(event["rate_limit_s"]) is int
        )
    except (KeyError, TypeError):
        return False


def read_events(path: str) -> tuple[list, dict]:
    """(well-formed restart events from the prior run's file, the prior file).
    Only the events matter across runs (they bound the rate limit); losing the
    file loses nothing else, and systemd's own start time still bounds it."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1_000_001)
        prior = json.loads(raw) if len(raw) <= 1_000_000 else None
    except (OSError, ValueError):
        prior = None
    if not isinstance(prior, dict):
        return [], {}
    events = prior.get("events")
    return ([e for e in events if valid_event(e)] if isinstance(events, list) else []), prior


def _read_counts(raw) -> dict:
    return {
        ip: n
        for ip, n in list(raw.items())[:MAX_PEERS]
        if _valid_ip(ip) and type(n) is int and n > 0
    }


def read_unhelped(prior: dict) -> dict:
    """{ip: restarts this boot that dropped sessions without a verified heal},
    from the prior run's file. Kept apart from ``events``, which is trimmed to
    the newest MAX_EVENTS: a count rebuilt from that list would forget old
    restarts and let a peer be restarted for again."""
    raw = prior.get("unhelped_restarts")
    return _read_counts(raw) if isinstance(raw, dict) else {}


def unhelped_by(event: dict) -> list:
    """The peers an event's restart dropped sessions for without verifiably
    clearing: every target not cleared, for every outcome but
    ``not-restarted`` (nothing restarted, nothing dropped)."""
    if event["action"] == "not-restarted":
        return []
    return [ip for ip in event["peers"] if ip not in event["cleared"]]


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


# ── the run ──────────────────────────────────────────────────────────────


def run_once(ctx: Ctx) -> int:
    prior_events, prior = read_events(ctx.state_file)
    same_boot = prior.get("boot_id") == ctx.boot_id
    events = [e for e in prior_events if e["boot_id"] == ctx.boot_id]
    heal_count = prior.get("heal_count") if same_boot else 0
    if type(heal_count) is not int or heal_count < 0:
        heal_count = 0
    blind_runs = prior.get("blind_runs") if same_boot else 0
    if type(blind_runs) is not int or blind_runs < 0:
        blind_runs = 0
    unhelped = read_unhelped(prior) if same_boot else {}
    counters = {"probed": 0, "unjudged": 0, "skipped": 0, "malformed_peers": 0}
    evidence: dict = {}
    present: list = []
    present_complete = False
    ages: dict = {}

    def finish(action: str, event: dict | None = None, exit_code: int = 0) -> int:
        nonlocal events, heal_count
        # Blind: the status could not be read, or there were suspects and not
        # one of them could be judged (scan limits, or every ping hanging).
        judged = counters["probed"] - counters["unjudged"]
        waiting = counters["skipped"] + counters["unjudged"]
        blind = action in BLIND_ACTIONS or (waiting > 0 and judged == 0)
        if event is not None:
            events = (events + [event])[-MAX_EVENTS:]
            if event["action"] == "healed":
                heal_count += 1
            for ip in unhelped_by(event):
                unhelped[ip] = unhelped.get(ip, 0) + 1
        # A peer seen working has recovered: whatever the restarts did not fix
        # is gone, so a later stall is judged afresh.
        for ip, verdict in evidence.items():
            if verdict == "ok":
                unhelped.pop(ip, None)
        record = {
            "version": 4,
            "boot_id": ctx.boot_id,
            "last_check": int(ctx.wall()),
            "last_check_mono": ctx.mono(),
            "last_action": action,
            "mode": ctx.mode,
            "heal_count": heal_count,
            "blind_runs": blind_runs + 1 if blind else 0,
            **counters,
            "evidence": evidence,
            "handshake_age_s": ages,
            "present": present,
            "present_complete": present_complete,
            "events": events,
            "unhelped_restarts": dict(sorted(unhelped.items())[:MAX_PEERS]),
            # Peers at the limit, which are not restarted for again until seen
            # working: the owner's alert must not promise a restart.
            "capped": sorted(ip for ip, n in unhelped.items() if n >= MAX_UNHELPED_RESTARTS)[
                :MAX_PEERS
            ],
        }
        try:
            write_atomic(ctx.state_file, json.dumps(record).encode(), 0o644)
        except OSError as exc:
            log(
                f"could not record '{action}' in {ctx.state_file} ({exc}); "
                "the owner will NOT be told of it"
            )
            return 1
        return exit_code

    if ctx.mode == "off":
        return finish("off")
    if not ctx.which(ctx.tailscale):
        return finish("unavailable")
    unit = unit_props(ctx, "tailscaled", "UnitFileState", *_IDENTITY) or {}
    # The daemon this run's verdicts describe, read BEFORE the status they rest on.
    judged_daemon = _identity(unit)
    if unit.get("ActiveState") != "active":
        # Stopped (not crashed: "failed") AND disabled or masked: off on purpose.
        if (
            unit.get("ActiveState") == "inactive"
            and unit.get("UnitFileState") in _DAEMON_OFF_STATES
        ):
            return finish("tailscaled-off")
        return finish("unavailable")
    rc, status = ctx.run(
        [ctx.tailscale, "status", "--json"], ctx.settings["NETWD_TS_CALL_TIMEOUT_SEC"]
    )
    if rc != 0 or not status.strip():
        return finish("unavailable")
    stale = ctx.settings["NETWD_TS_STALE_SEC"]
    started = judged_daemon[2] if judged_daemon else None
    zero_ok = started is not None and ctx.mono() - started > stale
    found = read_peers(status, ctx.wall(), stale, zero_ok=zero_ok)
    if found is None:
        log("status --json could not be read as a peer map; cannot judge tunnels this run")
        return finish("status-unparseable")
    if found[0] == "not-running":
        if found[1] == "Stopped":
            log("tailscale is down (`tailscale down`); nothing to watch")
            return finish("tailscaled-off")
        log(
            f"tailscaled's backend is {found[1] or 'unknown'}, not Running (logged out or"
            " starting); cannot judge tunnels"
        )
        return finish("unavailable")
    suspects, fresh_ips, counters["malformed_peers"], present_set = found
    if counters["malformed_peers"]:
        log(f"skipped {counters['malformed_peers']} malformed peer entr(y/ies) in status --json")
    present = sorted(present_set)[:MAX_PEERS]
    present_complete = not counters["malformed_peers"] and len(present_set) <= MAX_PEERS
    for ip in fresh_ips:
        evidence[ip] = "ok"

    verdicts, stuck, counters["probed"], counters["unjudged"], counters["skipped"] = scan(
        ctx, suspects
    )

    def void(why: str) -> int:
        # tailscaled restarted or stopped (an operator, an upgrade): every
        # verdict describes a daemon that is gone, and restarting now would
        # drop the sessions a new one just brought back.
        nonlocal evidence, present_complete
        log(f"{why}; this run's verdicts are void and nothing is restarted")
        evidence = {}
        present_complete = False
        return finish("daemon-changed")

    # A post-scan read that fails keeps the verdicts (one flaky systemctl call
    # should not blank a run); the check immediately before try-restart still
    # refuses to heal on anything but the same daemon.
    after_scan = daemon_identity(ctx)
    if (
        judged_daemon is not None
        and after_scan is not None
        and not same_daemon(judged_daemon, after_scan)
    ):
        return void("tailscaled restarted or stopped during the scan")
    evidence.update(verdicts)
    if len(evidence) > MAX_PEERS:
        # Keep what can be acted on: stuck, then offline, then ok.
        rank = {"stuck": 0, "offline": 1, "ok": 2}
        evidence = dict(sorted(evidence.items(), key=lambda kv: (rank[kv[1]], kv[0]))[:MAX_PEERS])
        present_complete = False
    for peer in stuck:
        ages[peer.ip] = peer.age
        age = "unknown" if peer.age is None else f"{peer.age}s"
        log(f"tunnel to {peer.ip} is STUCK (handshake age {age})")
    if counters["skipped"]:
        log(
            f"probed {counters['probed']} suspect peer(s); {counters['skipped']} not probed this run "
            f"(cap {ctx.settings['NETWD_TS_MAX_PROBES']} / {ctx.settings['NETWD_TS_SCAN_BUDGET_SEC']}s)"
        )
    if stuck:
        # Evidence at detection. Root-only: it names every node.
        try:
            write_atomic(ctx.snapshot_file, status.encode(), 0o600)
        except OSError as exc:
            log(f"could not save the status snapshot ({exc})")

    if not stuck:
        if counters["skipped"] or counters["unjudged"] or counters["malformed_peers"]:
            return finish("incomplete")  # an unprobed or unread peer could be stuck
        return finish("suspect-unreachable" if "offline" in verdicts.values() else "none")
    if ctx.mode == "observe":
        return finish("stuck")  # reported through the evidence; never restarted

    # The heal: only peers confirmed stuck in THIS run, at most once per window.
    rate = ctx.settings["NETWD_TS_RATE_LIMIT_SEC"]
    now = ctx.mono()
    if started is None:
        log(
            "tailscaled's start time could not be read, so the rate limit cannot be judged; not restarting"
        )
        return finish("stuck")
    spent = max([e["mono"] for e in events] + [started])
    if now - spent < rate:
        log(
            f"{len(stuck)} tunnel(s) stuck, but tailscaled started or was restarted <{rate}s ago; not restarting"
        )
        return finish("ratelimited")

    targets = sorted(p.ip for p in stuck if unhelped.get(p.ip, 0) < MAX_UNHELPED_RESTARTS)
    targets = targets[:MAX_TARGETS]
    if not targets:
        log(
            f"{MAX_UNHELPED_RESTARTS} restarts this boot have not verifiably cleared the stuck"
            " tunnel(s); not restarting for them again until they are seen working"
        )
        return finish("stuck")
    log(f"HEALING: restarting tailscaled for stuck tunnel(s) to {', '.join(targets)}")
    outcome, rc = restart_tailscaled(ctx, judged_daemon)
    if outcome == "daemon-changed":
        return void("tailscaled restarted or stopped before the heal")
    if outcome == "not-attempted":
        log(
            "could not read tailscaled's InvocationID, so a restart could not be judged; not restarting"
        )
        return finish("stuck")
    if outcome == "not-restarted" and rc == 0:
        # try-restart is a no-op on a stopped unit: nothing was restarted and
        # no session dropped, so nothing is spent.
        return void("tailscaled was stopped just before the heal (try-restart did nothing)")
    cleared: list = []
    unconfirmed: list = []
    if outcome == "healed":
        deadline = ctx.mono() + ctx.settings["NETWD_TS_VERIFY_SEC"]
        answers = _check_tunnels(ctx, targets, deadline)
        cleared = [ip for ip in targets if answers[ip] is True]
        unconfirmed = [ip for ip in targets if answers[ip] is None]
        for ip in cleared:
            evidence[ip] = "ok"
        if any(answers[ip] is False for ip in targets):
            outcome = "restart-no-effect"
        elif unconfirmed:
            # Restarted and back, but no check could be judged: neither a heal
            # nor a restart that failed to help (that would count toward the
            # limit and could stop healing this peer for the boot).
            outcome = "restart-unconfirmed"
    messages = {
        "healed": "tailscaled restarted and every stuck tunnel answers again",
        "restart-no-effect": f"tailscaled restarted, but {len(targets) - len(cleared) - len(unconfirmed)} tunnel(s) still get no reply; not retrying for {rate}s",
        "restart-unconfirmed": f"tailscaled restarted and is active, but the tunnel(s) to {', '.join(unconfirmed)} could not be checked afterwards; not retrying for {rate}s",
        "restart-failed": "tailscaled restarted but is NOT active; it may be DOWN; not retrying",
        "not-restarted": f"try-restart failed (rc={rc}) and nothing restarted; not retrying for {rate}s",
        "unverified": "could not read tailscaled's InvocationID afterwards; the restart's outcome is unknown",
        "pending": "the restart job had not finished when the poll ended; tailscaled may be hung",
    }
    log(messages[outcome])
    event = {
        "id": f"{ctx.boot_id}:{int(ctx.mono() * 1e9)}",
        "action": outcome,
        "boot_id": ctx.boot_id,
        "mono": now,
        "at": int(ctx.wall()),
        "peers": targets,
        "cleared": cleared,
        "unconfirmed": unconfirmed,
        "rc": rc,
        "rate_limit_s": rate,
    }
    return finish(outcome, event, 0 if outcome == "healed" else 1)


def main() -> int:
    return run_once(Ctx())


if __name__ == "__main__":
    sys.exit(main())
