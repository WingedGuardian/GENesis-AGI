"""Container-swap reconciler — HOST-SIDE (guardian).

``limits.memory.swap true`` is set by host-setup.sh at container creation and
on manual re-runs, and #1079 activates it live there too — but an install that
advances via bare ``git pull`` (observed on a sibling install) never re-runs
host-setup, so the knob can sit unset (and the live cgroup at
``memory.swap.max=0``) indefinitely. Every memory spike then becomes the
load-100 D-state OOM-thrash wedge instead of degrading into swap pressure —
the exact failure the setting exists to prevent, silent until it fires.

The guardian is the only Genesis component that runs host-side with incus +
sudo access *continuously*, so it reconciles on OBSERVED state each tick:

1. **Persistent**: ``incus config get limits.memory.swap`` is not already
   swap-on (``_is_swap_on``: any Incus TRUE spelling, or a parseable byte-size
   ceiling) → ``incus config set ... true`` (covers unset, every FALSE
   spelling, and unparseable garbage; applies at every future container
   start).
2. **Live**: cgroup ``memory.swap.max == "0"`` → write ``max`` now
   (``cgroup_ops.activate_swap_max``). ``max`` is Genesis's own uncapped-swap
   default, not a value Incus writes: with a hard ``limits.memory``, Incus
   writes ``0`` for a boolean ``limits.memory.swap`` and the parsed byte value
   for a ceiling. So when the key holds a ceiling, a live ``0`` is reported
   and left alone, never overwritten with ``max``.
3. **Opt-in ceiling** (``swap_ceiling_pct`` in guardian config, install-local,
   never shipped in the repo's template): when set, computes a byte target
   from host ``SwapTotal`` and asserts it as the container's swap-ceiling —
   via Incus's own native ``limits.memory.swap=<bytes>`` key when the
   container has a ``limits.memory`` cap (the condition under which Incus
   actually applies that key as a ceiling rather than ignoring it), or via a
   direct cgroup write as a fallback otherwise. See
   ``_compute_swap_ceiling_target``.

   **Ownership marker.** The reconciler tracks the byte value it last
   asserted in an Incus instance key, ``user.genesis.swap_ceiling`` — NOT a
   local file. Verified viable against Incus v6.0.0 source (2026-10-08):
   ``driver_lxc.go``'s live-apply branch is gated per-changed-key on
   ``key == "limits.memory" || strings.HasPrefix(key, "limits.memory.")``, so
   a ``user.*`` key changing in the same call can never trigger it; the CLI's
   ``config set <key>=<value>...`` form parses every pair into ONE API call,
   so the native assert and its marker land atomically; and ``user.*`` keys
   need no schema registration (``internal/instance/config.go``). This also
   means the marker is structurally bound to the one container it lives on
   (no cross-container staleness possible) and there is no local file to
   corrupt. Removing ``swap_ceiling_pct`` reverts the ceiling ONLY if the
   live-enforced value still matches the marker — an operator-set value is
   never touched, and an unreadable marker means HOLD, not "revert anyway."

   **Degraded ticks hold, they don't guess** (owner-approved rework spec,
   2026-10-08, item 2): when ``swap_ceiling_pct`` is set but the target can't
   be computed this tick (host SwapTotal unreadable, or the SEPARATE
   ``limits.memory`` probe used to pick native-vs-fallback fails), the tick
   skips writing ``limits.memory.swap`` ENTIRELY — including the ordinary
   false→true heal — and only records a problem. Writing ``true`` here would
   be a boolean value, and Incus resets a BOOLEAN key's live cgroup to 0 on
   every live update; forcing that during exactly the tick that can't
   re-assert a real ceiling would silently disable swap instead of leaving
   whatever was already enforced in place. A degraded tick never touches the
   persistent key or ceiling-specific cgroup writes, but it does NOT suspend
   the ORIGINAL (ceiling-unaware) live-cgroup protection: if the key already
   holds a valid byte ceiling from an earlier successful tick and the live
   cgroup reads 0, that is still reported rather than overwritten with
   ``max`` — nothing new needs computing to know that.

Healthy path = two or three cheap reads (persistent key, ownership marker,
and — only when a ceiling is configured — the ``limits.memory`` probe), no
writes, no alerts. A heal emits one INFO alert (guardian self-actions must be
visible; several heals in the same tick are merged into one alert); a failed
heal emits a WARNING throttled PER PROBLEM CLASS by a state file (so a
persistent fault pages at most every 24h, not per-tick) — EXCEPT that a
WARNING whose own delivery fails is retried after 5 minutes rather than
silently adopting the 24h window meant for "delivered, fault persists" (a
down alert channel must not mute itself for a day). Never raises into the
tick.

Deliberate override note: an operator who explicitly set
``limits.memory.swap=false`` will be reconciled back to ``true`` — swap-on is
a Genesis install invariant (docs/reference/memory-resilience.md). Disable the
reconciler itself (``swap_reconcile_enabled: false`` in guardian config) to
opt a host out — a disabled reconciler never reads or writes the marker
either.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.cgroup_ops import activate_swap_max, read_swap_max, write_swap_max

logger = logging.getLogger(__name__)

# A persistent, DELIVERED failure re-pages at most this often (state-file
# throttle). A failure whose alert ITSELF could not be delivered backs off
# far less (see _RETRY_MINUTES) — the two must never share one window, or a
# down alert channel would silently mute a real fault for a day.
_REALERT_HOURS = 24.0
_RETRY_MINUTES = 5.0

_INCUS_TIMEOUT = 10.0

# The Incus instance key that tracks the byte value this reconciler last
# asserted as the swap ceiling (native key or cgroup fallback, whichever
# applied) — see the module docstring for why this lives in Incus config
# rather than a local file.
_MARKER_KEY = "user.genesis.swap_ceiling"

# Incus's own boolean-word lists (shared/util/boolean.go, v6.0.0 and main,
# read 2026-10-07: `IsTrue` = {"true","1","yes","on"}, `IsFalse` =
# {"false","0","no","off"}, case-insensitive). driver_lxc.go checks these
# BEFORE trying to parse a byte size — `if IsTrueOrEmpty(v) || IsFalse(v) {
# SetMemorySwapLimit(0) } else { parse as byte size }` — so the bare digit
# strings "0" and "1" are claimed by the boolean check and NEVER reach the
# byte-size parser, even though they also look like valid byte sizes (0
# bytes, 1 byte). Checking these word lists first is required, not cosmetic:
# without it, an operator's explicit "0" (deliberately off) would be
# misread as "a 0-byte ceiling" and left unreconciled, silently defeating
# the guardian's own "false is always reconciled back to true" invariant.
_INCUS_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_INCUS_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


_ASCII_DIGITS = frozenset("0123456789")


def _is_parseable_incus_size(raw: str) -> bool:
    """Whether ``raw`` is a NONZERO Incus byte-size ceiling — trusting
    Incus's own write-time validation rather than re-mirroring its suffix
    grammar. Callers must rule out Incus's own boolean words first (see
    ``_is_swap_on``) — this function alone cannot tell "0" the byte size
    from "0" the bool.

    ``incus config get`` only ever returns a value that already passed
    ``validate.IsSize`` (→ ``units.ParseByteSizeString``) at some past
    ``config set`` — so any value that is not one of Incus's own boolean
    spellings (ruled out by the caller) and starts with a digit IS a
    validated Incus size, whatever suffix it carries, on WHATEVER Incus
    version wrote it. An earlier version of this function re-validated the
    suffix against an allowlist copied from one version's source
    (``shared/units/units.go``) — a second, driftable parser duplicating
    Incus's own authority: ``host-setup.sh`` installs Incus from
    ``latest/stable``, unpinned, so a future release adding a new size
    suffix would have made this function treat a perfectly legitimate
    ceiling as garbage and silently overwrite it with "true". Removed
    rather than hardened, per three rounds of review finding a new edge
    in that same mirror. (Codex finding on PR #3069, 2026-10-08: "Remove
    the second Incus size parser.") This rework also drops the SEPARATE
    suffix-multiplier table the first ceiling draft (#3081) re-added for
    computing an actual byte value — comparisons here are plain string
    equality against ``str(target)`` (Genesis always writes a bare byte
    count itself), so no caller ever needs to parse an arbitrary suffix
    back into bytes (owner-approved rework spec, 2026-10-08, item: "drop
    the parser and compare as strings").

    Only the ZERO/nonzero question is still ours to answer — Incus's own
    validator accepts a zero-valued size as a legitimate value, but it
    means swap-OFF, so it must not be misclassified as swap-on (round-2
    finding) or crash on an absurdly zero-padded one (round-3 finding).
    Both are handled by scanning digit CHARACTERS only
    (``any(ch != "0" ...)``), never via ``int(raw[:i])`` — Incus's own
    parser (``strconv.ParseInt``-style accumulation) tolerates arbitrarily
    many leading zeros with no overflow, so a value like 4,301 zeros
    followed by "1GiB" is a legitimate (if perverse) 1 GiB ceiling Incus
    can return from ``config get``, but Python's ``int()`` raises
    ``ValueError`` past ~4,300 digits (``sys.get_int_max_str_digits``)."""
    if not raw:
        return False
    i = 0
    while i < len(raw) and raw[i] in _ASCII_DIGITS:
        i += 1
    if i == 0:
        return False
    return any(ch != "0" for ch in raw[:i])


def _is_swap_on(raw: str) -> bool:
    """Whether this ``limits.memory.swap`` value already explicitly encodes
    swap-on, so the reconciler should leave it alone: any Incus TRUE
    spelling, or a parseable byte-size ceiling (the native swap-ceiling
    form — Incus parses anything that is not an ``IsTrueOrEmpty``/``IsFalse``
    boolean word as a byte size; see driver_lxc.go). Empty/unset and any
    Incus FALSE spelling are NOT swap-on — both still need the reconciling
    'set true' call below, unchanged from today; this is the guardian's
    existing "deliberate override: false is always reconciled back to true"
    policy, now applied to every spelling of false, not just the literal
    word "false"."""
    stripped = raw.strip()
    lowered = stripped.lower()
    if not stripped or lowered in _INCUS_FALSE_WORDS:
        return False
    if lowered in _INCUS_TRUE_WORDS:
        return True
    return _is_parseable_incus_size(stripped)


def _host_swap_total_bytes() -> int | None:
    """Host ``/proc/meminfo`` SwapTotal, in bytes.

    The guardian runs HOST-side (a KVM guest), so this is the real host swap
    pool, never a container-namespaced view — measured 2026-10-07: the value
    reads 0 from inside the container's own /proc/meminfo, confirming the
    vantage point matters. None when unreadable (missing file, no SwapTotal
    line, malformed value) — NEVER 0, which a caller would otherwise read as
    "host has no swap at all" and go on to compute a bogus near-zero target.

    A small, self-contained read rather than importing
    ``host_profile._read_meminfo``: that function is private to its own
    module and pulls in several unrelated imports (asyncio, platform,
    socket, …) for one field; duplicating ~10 lines here keeps this module's
    dependency surface to what the reconciler itself needs.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _compute_swap_ceiling_target(config) -> int | None:
    """The configured swap ceiling, in bytes, page-aligned — or None when
    unconfigured, when host SwapTotal can't be read, or when the computed
    value would floor to less than one page. A None return while
    ``config.swap_ceiling_pct`` is itself set means DEGRADED, not
    unconfigured — callers must check ``config.swap_ceiling_pct`` directly to
    tell the two apart (see ``check_container_swap_and_alert``'s ``mode``).

    Page-aligned because the kernel's ``swap_max_write`` (mm/memcontrol.c,
    read 2026-10-08: v6.8 ``page_counter_memparse`` stores
    ``bytes // PAGE_SIZE`` with no rounding of its own beyond that floor, and
    ``swap_max_show`` prints ``pages * PAGE_SIZE`` back) — so alignment here
    is not defensive, it IS what makes a later readback equal exactly what
    was asked for; an unaligned target would never match and would be
    rewritten (and re-alerted) every tick for no behavioural reason.

    Never returns 0: a ``swap_ceiling_pct`` too small for the host's pool to
    express in at least one page is DISABLED here (with a warning) rather
    than rounded down to 0, because a 0-byte cap means swap-off — exactly
    the state this whole reconciler exists to prevent.
    """
    pct = getattr(config, "swap_ceiling_pct", None)
    if pct is None:
        return None
    swap_total = _host_swap_total_bytes()
    if swap_total is None or swap_total <= 0:
        logger.debug(
            "swap_watch: host SwapTotal unreadable or zero — ceiling disabled this tick",
        )
        return None
    page = os.sysconf("SC_PAGE_SIZE")
    raw_target = int(swap_total * pct / 100)
    target = (raw_target // page) * page
    if target < page:
        logger.warning(
            "swap_watch: computed swap ceiling (%d bytes, %.4g%% of %d bytes "
            "host swap) is smaller than the page size (%d) — disabling the "
            "ceiling this tick rather than writing a near-zero cap. Raise "
            "swap_ceiling_pct or the host's swap pool.",
            raw_target,
            pct,
            swap_total,
            page,
        )
        return None
    return target


async def _limits_memory_state(container: str) -> str:
    """Whether this container has a ``limits.memory`` cap — the condition
    under which Incus actually applies ``limits.memory.swap`` as a native
    swap-ceiling byte value rather than ignoring it (driver_lxc.go only
    touches the swap cgroup inside the ``if memory != ""`` branch). THREE-WAY,
    not boolean: ``"set"`` (native path applies), ``"unset"`` (no cap — the
    cgroup-fallback path applies), or ``"unknown"`` (the read itself failed —
    a DEGRADED tick, Codex P2 "warn when persistence is unknown during
    fallback": an unreadable probe must not be silently treated as either
    "set" or "unset", since guessing wrong either asserts a native key Incus
    will ignore, or skips the native key Incus would actually have applied).
    """
    try:
        rc, stdout, _stderr = await _run_subprocess(
            "incus",
            "config",
            "get",
            "--expanded",
            container,
            "limits.memory",
            timeout=_INCUS_TIMEOUT,
        )
    except Exception:
        return "unknown"
    if rc != 0:
        return "unknown"
    return "set" if stdout.strip() else "unset"


async def _incus_config_get(container: str, key: str) -> tuple[bool, str]:
    """Read one Incus instance config key. Returns ``(ok, value)`` —
    ``ok=False`` means unreadable (incus down, container gone, a non-zero
    exit), which callers must treat as "no signal", never as "empty"."""
    try:
        rc, stdout, _stderr = await _run_subprocess(
            "incus",
            "config",
            "get",
            "--expanded",
            container,
            key,
            timeout=_INCUS_TIMEOUT,
        )
    except Exception:
        logger.warning("swap_watch: incus config get %s failed", key, exc_info=True)
        return False, ""
    if rc != 0:
        return False, ""
    return True, stdout.strip()


async def _incus_config_set(container: str, pairs: dict[str, str]) -> tuple[bool, str]:
    """Set one or more Incus instance config keys in ONE ``config set`` call
    — the CLI's ``<key>=<value>...`` form parses every pair into a single API
    request (``cmd/incus/config.go`` ``cmdConfigSet.Run``, read 2026-10-08),
    so bundling the real value with its ownership marker here is atomic: no
    tick can observe one written without the other. An empty value clears
    that key (Incus has no distinct "unset via set" — an empty string reads
    back as empty, which this module already treats identically to "absent"
    everywhere else, e.g. ``_is_swap_on``'s unset/false-spelling branch).
    Returns ``(ok, error_text)``.
    """
    args = [f"{k}={v}" for k, v in pairs.items()]
    try:
        rc, _stdout, stderr = await _run_subprocess(
            "incus",
            "config",
            "set",
            container,
            *args,
            timeout=_INCUS_TIMEOUT,
        )
    except Exception as exc:
        return False, str(exc)
    if rc != 0:
        return False, stderr.strip()
    return True, ""


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> bool:
    """Dispatch one alert. Returns whether it was actually DELIVERED — a
    dispatcher with no channels configured, or every channel failing, returns
    a falsy result rather than raising (see ``alert/dispatcher.py``); this
    return value is what lets the throttle distinguish "warned, fault
    persists" (the normal 24h window) from "nobody has been told yet" (a
    much shorter retry — see ``_RETRY_MINUTES``). Never raises into the
    tick."""
    try:
        result = await dispatcher.send(Alert(severity=severity, title=title, body=body))
        return bool(result)
    except Exception:
        logger.warning("swap_watch alert dispatch failed", exc_info=True)
        return False


def _class_last_attempt(data, problem_class: str) -> tuple[datetime | None, bool]:
    """The (timestamp, was_delivered) of the last throttle record for
    ``problem_class``, migrating every prior on-disk shape this state file
    has ever had: the original flat ``{"last_failure_alert_at": "<iso>"}``
    (pre-per-class — always the swap_off class, and always treated as
    delivered, since no retry concept existed yet); the per-class dict of
    bare ISO strings (also always "delivered", same reason); and the current
    per-class dict of ``{"at": iso, "delivered": bool}`` objects. Returns
    ``(None, False)`` when there is nothing usable, which callers read as
    "due" — never silently erase a real, still-valid window by guessing
    wrong about an unfamiliar shape."""
    raw_at = data.get("last_failure_alert_at") if isinstance(data, dict) else None
    if isinstance(raw_at, str):
        if problem_class != _PROBLEM_CLASS_SWAP_OFF or not raw_at:
            return None, False
        try:
            return datetime.fromisoformat(raw_at), True
        except ValueError:
            return None, False
    if isinstance(raw_at, dict):
        entry = raw_at.get(problem_class)
        if isinstance(entry, str) and entry:
            try:
                return datetime.fromisoformat(entry), True
            except ValueError:
                return None, False
        if isinstance(entry, dict):
            at = entry.get("at")
            if not at:
                return None, False
            try:
                return datetime.fromisoformat(at), bool(entry.get("delivered", True))
            except (ValueError, TypeError):
                return None, False
    return None, False


#: The two independent problem classes this watch can raise a WARNING for,
#: each throttled on its own timestamp — a ceiling-write failure (swap
#: itself is fine, just uncapped) must never silently mute the much more
#: serious "swap is actually off" WARNING for the next 24h, and vice versa.
_PROBLEM_CLASS_SWAP_OFF = "swap_off"
_PROBLEM_CLASS_CEILING = "ceiling"


def _failure_alert_due(state_file, now: datetime, problem_class: str) -> bool:
    """True when the throttle window has elapsed for ``problem_class`` (or no
    state yet). The window itself depends on whether the LAST attempt was
    actually delivered — 24h once delivered, 5min while delivery itself is
    failing (SHOULD-FIX: an install with a down alert channel must not
    silently mute a real swap fault for a day just because nobody could be
    told the first time)."""
    if not state_file.exists():
        return True
    try:
        data = json.loads(state_file.read_text())
    except (ValueError, OSError):
        return True
    last, delivered = _class_last_attempt(data, problem_class)
    if last is None:
        return True
    try:
        window = _REALERT_HOURS * 3600 if delivered else _RETRY_MINUTES * 60
        return (now - last).total_seconds() >= window
    except (TypeError, OverflowError):
        # aware-vs-naive timestamp or similar — state unusable, alert is due.
        return True


def _record_failure_alert(state_file, now: datetime, problem_class: str, delivered: bool) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if state_file.exists():
            try:
                existing = json.loads(state_file.read_text())
            except (ValueError, OSError):
                existing = {}
        raw_at = existing.get("last_failure_alert_at") if isinstance(existing, dict) else None
        by_class: dict[str, dict] = {}
        if isinstance(raw_at, dict):
            for k, v in raw_at.items():
                # Normalize a bare-ISO-string per-class entry (the pre-retry
                # shape) into the current {at, delivered} form rather than
                # dropping it — it was always "delivered" under that shape.
                by_class[k] = v if isinstance(v, dict) else {"at": v, "delivered": True}
        elif isinstance(raw_at, str) and raw_at:
            # Migrate the LEGACY flat (pre-per-class) timestamp — it was
            # always a swap_off-class record (the only class that existed
            # before per-class throttling), and discarding it here would
            # silently erase a real, still-valid throttle window the first
            # time a DIFFERENT class (ceiling) is the one being recorded.
            by_class = {_PROBLEM_CLASS_SWAP_OFF: {"at": raw_at, "delivered": True}}
        by_class[problem_class] = {"at": now.isoformat(), "delivered": delivered}
        state_file.write_text(json.dumps({"last_failure_alert_at": by_class}))
    except OSError:
        logger.warning("could not persist swap_watch alert state", exc_info=True)


async def check_container_swap_and_alert(config, dispatcher) -> None:
    """Reconcile the container-swap invariant; alert on heal or failure.

    Never raises into the tick. An unreadable signal (incus down, container
    stopped, cgroup absent) is "no signal", not a defect — container-down is
    the state machine's job, not this watch's.
    """
    if not getattr(config, "swap_reconcile_enabled", True):
        return

    container = config.container_name
    healed: list[str] = []
    problems: dict[str, list[str]] = {
        _PROBLEM_CLASS_SWAP_OFF: [],
        _PROBLEM_CLASS_CEILING: [],
    }

    pct_configured = getattr(config, "swap_ceiling_pct", None) is not None
    ceiling_target = _compute_swap_ceiling_target(config)

    # mode discriminates the three ceiling states a tick can be in:
    #   "ceiling"  — a usable target AND a usable limits.memory probe.
    #   "degraded" — swap_ceiling_pct IS set, but either the target or the
    #                limits.memory probe could not be read this tick. Holds:
    #                no persistent-key write at all, including the ordinary
    #                false->true heal (see the module docstring).
    #   "none"     — swap_ceiling_pct is unset/invalid; the ordinary
    #                false->true heal applies, plus reverting any ceiling
    #                this reconciler previously asserted.
    limits_memory_state = "unknown"
    if ceiling_target is not None:
        limits_memory_state = await _limits_memory_state(container)
    if ceiling_target is not None and limits_memory_state != "unknown":
        mode = "ceiling"
    elif pct_configured:
        mode = "degraded"
    else:
        mode = "none"

    # 1. Persistent knob. `incus config get` on an unset key returns rc=0 with
    # empty output, so unset and explicit-false both land in the heal branch.
    try:
        # --expanded so a profile-inherited true reads as true (a plain get is
        # local-only and would trigger a redundant local override + a false
        # "was off" heal alert). Verified supported on live incus.
        rc, stdout, _stderr = await _run_subprocess(
            "incus",
            "config",
            "get",
            "--expanded",
            container,
            "limits.memory.swap",
            timeout=_INCUS_TIMEOUT,
        )
    except Exception:
        logger.warning("swap_watch: incus config get failed", exc_info=True)
        rc, stdout = 1, ""
    config_verified = rc == 0
    raw_value = stdout.strip() if rc == 0 else ""
    key_now = raw_value  # tracks the key's value as WE understand it after any write this tick
    # Set for real only inside mode=="none"; stays False whenever that branch
    # never ran (any other mode, or config_verified is False) — exactly the
    # cases where step 3 must fall through to its own (correct) logic rather
    # than reading an unset flag.
    native_revert_attempted = False

    # 2. Ownership marker. Read every tick regardless of mode: it is the only
    # record of a ceiling this reconciler previously asserted, so even a
    # "mode=none" tick must consult it to know whether a revert is owed.
    marker_readable, marker_raw = (False, "")
    if config_verified:
        marker_readable, marker_raw = await _incus_config_get(container, _MARKER_KEY)
    marker_now = marker_raw if marker_readable else None  # None = unreadable -> hold, never guess

    if config_verified:
        if mode == "degraded":
            reason = (
                "limits.memory unreadable"
                if ceiling_target is not None
                else "host SwapTotal unreadable, or the ceiling is below one page"
            )
            problems[_PROBLEM_CLASS_CEILING].append(
                f"swap ceiling not asserted this tick ({reason}); the persistent "
                "limits.memory.swap key was left untouched to avoid clobbering a "
                "ceiling with a boolean value",
            )
        elif mode == "ceiling":
            target_str = str(ceiling_target)
            if limits_memory_state == "set":
                # NATIVE PATH.
                if raw_value != target_str:
                    ok, err = await _incus_config_set(
                        container,
                        {"limits.memory.swap": target_str, _MARKER_KEY: target_str},
                    )
                    if ok:
                        healed.append(
                            f"limits.memory.swap: {raw_value or 'unset'} → {target_str} bytes "
                            f"(native ceiling, {config.swap_ceiling_pct:g}% of host swap)",
                        )
                        key_now = target_str
                        marker_now = target_str
                    else:
                        problems[_PROBLEM_CLASS_CEILING].append(
                            f"incus config set limits.memory.swap={target_str} failed: {err}",
                        )
                elif marker_now is not None and marker_now != target_str:
                    # Already correct natively; the marker didn't catch up (a
                    # prior tick's bundled set partially landed before this
                    # fix existed, or an operator coincidentally matched the
                    # target). Reclaim it alone — no need to touch the real
                    # key, which is already right.
                    ok, err = await _incus_config_set(container, {_MARKER_KEY: target_str})
                    if ok:
                        marker_now = target_str
                    else:
                        problems[_PROBLEM_CLASS_CEILING].append(
                            f"could not record the swap-ceiling marker: {err}",
                        )
            else:  # limits_memory_state == "unset" — FALLBACK PATH.
                # The native key is inert without a limits.memory cap, but the
                # baseline invariant (never literally off) still applies; the
                # actual ceiling is enforced on the live cgroup below.
                if not _is_swap_on(raw_value):
                    ok, err = await _incus_config_set(container, {"limits.memory.swap": "true"})
                    if ok:
                        healed.append(
                            f"limits.memory.swap: {raw_value or 'unset'} → true "
                            "(persists across restarts; ceiling enforced via cgroup fallback)",
                        )
                        key_now = "true"
                    else:
                        problems[_PROBLEM_CLASS_SWAP_OFF].append(
                            f"incus config set limits.memory.swap=true failed: {err}",
                        )
        else:  # mode == "none"
            if not _is_swap_on(raw_value):
                ok, err = await _incus_config_set(container, {"limits.memory.swap": "true"})
                if ok:
                    healed.append(
                        f"limits.memory.swap: {raw_value or 'unset'} → true (persists across restarts)",
                    )
                    key_now = "true"
                else:
                    problems[_PROBLEM_CLASS_SWAP_OFF].append(
                        f"incus config set limits.memory.swap=true failed: {err}",
                    )
            # Native-side revert: a marker from an earlier ceiling, and the
            # persistent key (as we now understand it) still equals it. If
            # marker_now is truthy but this doesn't match, it may be the
            # cgroup-fallback path — decided below, once the live value is
            # known.
            if marker_now and key_now == marker_now:
                native_revert_attempted = True
                ok, err = await _incus_config_set(
                    container,
                    {"limits.memory.swap": "true", _MARKER_KEY: ""},
                )
                if ok:
                    healed.append(
                        f"swap ceiling removed: limits.memory.swap {marker_now} bytes → "
                        "true (swap_ceiling_pct no longer set)",
                    )
                    key_now = "true"
                    marker_now = ""
                else:
                    problems[_PROBLEM_CLASS_CEILING].append(
                        f"could not revert the native swap ceiling ({marker_now} bytes) "
                        f"to true: {err}",
                    )
    else:
        logger.debug("swap_watch: no incus config signal (rc=%s)", rc)

    # 3. Live cgroup. Only "0" is the defect in the no-ceiling baseline; None
    # = no signal (stopped container / cgroup v1), any other value already
    # permits swap.
    current = await read_swap_max(container)

    if mode == "ceiling":
        target_str = str(ceiling_target)
        if current is not None and current != target_str:
            if await write_swap_max(container, target_str):
                healed.append(f"memory.swap.max: {current} → {target_str} bytes (live)")
                if limits_memory_state == "unset" and marker_now != target_str:
                    ok, err = await _incus_config_set(container, {_MARKER_KEY: target_str})
                    if ok:
                        marker_now = target_str
                    else:
                        problems[_PROBLEM_CLASS_CEILING].append(
                            f"applied the cgroup fallback ceiling but could not record the "
                            f"swap-ceiling marker: {err}",
                        )
            else:
                problems[_PROBLEM_CLASS_CEILING].append(
                    f"cgroup write of swap ceiling ({target_str} bytes) failed",
                )
        elif (
            current == target_str
            and limits_memory_state == "unset"
            and marker_now is not None
            and marker_now != target_str
        ):
            # Fallback path, already correct live, but the marker never
            # caught up (a crash or failed set between the cgroup write and
            # the marker write on an earlier tick) — reclaim it alone.
            ok, err = await _incus_config_set(container, {_MARKER_KEY: target_str})
            if ok:
                marker_now = target_str
            else:
                problems[_PROBLEM_CLASS_CEILING].append(
                    f"could not reclaim the swap-ceiling marker (cgroup fallback): {err}",
                )
    else:
        # "degraded" and "none" both fall back to the ORIGINAL, ceiling-
        # unaware step-2 logic — using key_now, which in "degraded" mode is
        # untouched raw_value (nothing was written above) and in "none" mode
        # may already reflect this tick's heal or revert.
        if current == "0":
            if _is_swap_on(key_now) and _is_parseable_incus_size(key_now):
                # Writing "max" here would replace an existing ceiling with
                # unlimited swap until the next restart. Incus applies a
                # ceiling with no zero window, so a live 0 under one was
                # written from outside: report it, never overwrite it.
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    f"limits.memory.swap holds a ceiling ({key_now}) but the live "
                    "memory.swap.max is 0 — left as is; restart the container or "
                    "re-set the key to apply the ceiling",
                )
            elif await activate_swap_max(container):
                if config_verified:
                    healed.append("memory.swap.max: 0 → max (live, no restart needed)")
                else:
                    # Live-healed, but the config read failed so the PERSISTENT
                    # knob was never verified/set — a restart can revert
                    # memory.swap.max to 0. Don't declare a clean "reconciled";
                    # surface it so the persistent half gets fixed.
                    problems[_PROBLEM_CLASS_SWAP_OFF].append(
                        "activated swap live (memory.swap.max 0 → max) but could NOT "
                        "verify the persistent limits.memory.swap knob (incus config "
                        "read failed) — a container restart may revert swap to off",
                    )
            else:
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    "live cgroup write failed — swap stays off until the next "
                    "container start (set the persistent knob and restart to apply)",
                )
        elif mode == "none" and marker_now and not native_revert_attempted:
            # native_revert_attempted gates this on whether the native branch
            # above ALREADY determined (and tried to act on) the native case
            # — not on whether current happens to still equal the marker.
            # Without this gate, a FAILED native revert leaves key_now,
            # marker_now and current all still equal to the stale ceiling,
            # which this cgroup branch would misread as "the fallback path
            # was in effect": it would write the live cgroup to max and
            # clear the marker, stranding the persisted key at the old
            # ceiling with no further signal to fix it (genesis-architect
            # finding on this rework, 2026-10-08).
            if current == marker_now:
                # Cgroup-fallback revert: the native side didn't match above,
                # so this was the fallback path in effect.
                if await write_swap_max(container, "max"):
                    ok, err = await _incus_config_set(container, {_MARKER_KEY: ""})
                    if ok:
                        healed.append(
                            f"swap ceiling removed: memory.swap.max {marker_now} bytes → max "
                            "(swap_ceiling_pct no longer set)",
                        )
                        marker_now = ""
                    else:
                        problems[_PROBLEM_CLASS_CEILING].append(
                            "reverted the live cgroup swap ceiling but could not clear the "
                            f"swap-ceiling marker: {err}",
                        )
                else:
                    problems[_PROBLEM_CLASS_CEILING].append(
                        f"could not revert the cgroup swap ceiling ({marker_now} bytes) to max",
                    )
            else:
                # Neither the persistent key nor the live cgroup matches the
                # marker: an operator changed the enforced value directly.
                # Nothing to revert — just stop tracking it.
                ok, err = await _incus_config_set(container, {_MARKER_KEY: ""})
                if ok:
                    marker_now = ""
                else:
                    problems[_PROBLEM_CLASS_CEILING].append(
                        f"could not clear a stale swap-ceiling marker: {err}",
                    )

    if healed:
        logger.info("swap_watch healed: %s", "; ".join(healed))
        await _send(
            dispatcher,
            AlertSeverity.INFO,
            "Guardian set container swap",
            "Container-swap invariant reconciled on "
            f"'{container}':\n- "
            + "\n- ".join(healed)
            + "\n\nWithout this, a memory spike wedges the box into D-state "
            "thrash instead of degrading into swap. If swap-off was "
            "intentional, set swap_reconcile_enabled: false in guardian "
            "config.",
        )

    for problem_class, class_problems in problems.items():
        if not class_problems:
            continue
        logger.warning("swap_watch problems (%s): %s", problem_class, "; ".join(class_problems))
        now = datetime.now(UTC)
        state_file = config.state_path / "swap_watch_state.json"
        if _failure_alert_due(state_file, now, problem_class):
            title = (
                "Container swap reconcile FAILED"
                if problem_class == _PROBLEM_CLASS_SWAP_OFF
                else "Container swap ceiling FAILED"
            )
            delivered = await _send(
                dispatcher,
                AlertSeverity.WARNING,
                title,
                f"Guardian could not enforce the swap invariant on '{container}':\n- "
                + "\n- ".join(class_problems),
            )
            _record_failure_alert(state_file, now, problem_class, delivered)
