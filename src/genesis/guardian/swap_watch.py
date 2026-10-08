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
   never shipped in the repo's template): when set to a number, computes a
   byte target from host ``SwapTotal`` and asserts it as the container's swap
   ceiling. NATIVE path: Incus's own ``limits.memory.swap=<bytes>`` key, used
   when the container has a ``limits.memory`` cap that is not
   ``limits.memory.enforce=soft``: only then does Incus apply the key at
   container START (driver_lxc.go v6.0.0). FALLBACK path otherwise: the key
   only has to be swap-on, and the ceiling is written to the live cgroup each
   tick. (On a LIVE update to any ``limits.memory*`` key Incus does apply the
   key even with no ``limits.memory``, writing 0 for a boolean; the same tick
   rewrites the target, because it reads the cgroup after any key write.)
   Either way the live cgroup is repaired whenever it drifts from the target. See
   ``_compute_swap_ceiling_target`` and ``_ceiling_path``.

   **Turning it off is explicit** (owner ruling, 2026-10-08):
   ``swap_ceiling_pct: off`` asserts ``limits.memory.swap=true`` (replacing a
   byte ceiling) and lifts any finite live cgroup cap to ``max``, converging
   in one tick and doing nothing on later ticks. REMOVING the setting is not
   "off": the reconciler then simply stops managing a ceiling, and a byte
   ceiling in the key, with its cgroup value, stays as it is. (A key that is
   not swap-on is still healed to ``true`` as in step 1, and Incus's own live
   update then resets the cgroup, so a cap written only to the cgroup by hand
   does not survive that heal.) No ownership record exists, so nothing can
   strand or mis-claim one.

   **Degraded ticks hold, they don't guess** (owner-approved rework spec,
   2026-10-08, item 2): when ``swap_ceiling_pct`` is a number but the target
   can't be computed this tick (host SwapTotal unreadable), or the probes that
   pick native-vs-fallback fail, the tick writes nothing to
   ``limits.memory.swap`` at all, including the ordinary false->true heal:
   ``true`` is a boolean, and under a hard limit Incus resets a boolean key's
   live cgroup to 0 on every live update. The ceiling-unaware live protection
   still runs. A live 0 under a byte-ceiling key is reported, never
   overwritten; a live 0 under anything else is opened to ``max`` (swap off is
   the failure this reconciler exists to prevent) and reported as an uncapped
   ceiling until the target can be computed again.

Healthy path = cheap reads only (the persistent key and, when a ceiling is
configured, the ``limits.memory`` and ``limits.memory.enforce`` probes), no
writes, no alerts. A heal emits one INFO alert (guardian self-actions must be
visible; several heals in the same tick are merged into one alert); a failed
heal emits a WARNING throttled PER PROBLEM CLASS by a state file (so a
persistent fault pages at most every 24h, not per-tick), EXCEPT that a
WARNING whose own delivery fails is retried after 5 minutes rather than
silently adopting the 24h window meant for "delivered, fault persists" (a
down alert channel must not mute itself for a day). Never raises into the
tick.

Deliberate override note: an operator who explicitly set
``limits.memory.swap=false`` will be reconciled back to ``true`` — swap-on is
a Genesis install invariant (docs/reference/memory-resilience.md). Disable the
reconciler itself (``swap_reconcile_enabled: false`` in guardian config) to
opt a host out; a disabled reconciler reads and writes nothing, ceiling
included.
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
from genesis.guardian.config import SWAP_CEILING_OFF

logger = logging.getLogger(__name__)

# A persistent, DELIVERED failure re-pages at most this often (state-file
# throttle). A failure whose alert ITSELF could not be delivered backs off
# far less (see _RETRY_MINUTES) — the two must never share one window, or a
# down alert channel would silently mute a real fault for a day.
_REALERT_HOURS = 24.0
_RETRY_MINUTES = 5.0

_INCUS_TIMEOUT = 10.0

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


def _is_byte_ceiling(raw: str) -> bool:
    """Whether this ``limits.memory.swap`` value is a nonzero byte-size
    ceiling, as opposed to a boolean. The boolean words are ruled out FIRST,
    exactly as Incus does: ``"1"`` is Incus TRUE (a boolean, for which Incus
    writes ``memory.swap.max=0`` under a hard memory limit), even though it
    also reads as a one-byte size. Testing ``_is_swap_on(v) and
    _is_parseable_incus_size(v)`` instead would call ``"1"`` a ceiling and
    leave a live 0 under it unhealed."""
    stripped = raw.strip()
    lowered = stripped.lower()
    if lowered in _INCUS_TRUE_WORDS or lowered in _INCUS_FALSE_WORDS:
        return False
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
    if pct is None or pct == SWAP_CEILING_OFF:
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


async def _ceiling_path(container: str) -> str:
    """Which path enforces a swap ceiling on this container: ``"native"``,
    ``"fallback"`` or ``"unknown"``.

    At container start, Incus applies ``limits.memory.swap`` only when
    ``limits.memory`` is set AND ``limits.memory.enforce`` is not ``"soft"``
    (driver_lxc.go v6.0.0: the swap limit is inside the ``else`` of
    ``if memoryEnforce == "soft"``, an exact, case-sensitive comparison, and
    inside ``if memory != ""``). Anything else would lose a native ceiling at
    the next start, so the ceiling goes to the live cgroup instead
    (``"fallback"``). ``"unknown"`` when either read
    fails: a DEGRADED tick, never a guess, since a wrong guess either asserts
    a key Incus ignores or skips one Incus would apply.
    """
    ok, memory = await _incus_config_get(container, "limits.memory")
    if not ok:
        return "unknown"
    if not memory:
        return "fallback"
    ok, enforce = await _incus_config_get(container, "limits.memory.enforce")
    if not ok:
        return "unknown"
    return "fallback" if enforce == "soft" else "native"


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
    """Set Incus instance config keys in one ``config set`` call. Returns
    ``(ok, error_text)``."""
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

    pct = getattr(config, "swap_ceiling_pct", None)
    ceiling_off = pct == SWAP_CEILING_OFF
    pct_configured = pct is not None and not ceiling_off
    ceiling_target = _compute_swap_ceiling_target(config) if pct_configured else None

    # mode discriminates what this tick enforces:
    #   "ceiling"  — a usable target AND a usable native/fallback probe.
    #   "degraded" — swap_ceiling_pct is a number, but the target or the probe
    #                 could not be read this tick. Holds: no persistent-key
    #                 write at all, including the ordinary false->true heal.
    #   "off"      — swap_ceiling_pct: off. Remove a ceiling: key → true,
    #                 a finite live cap → max.
    #   "none"     — unset: the ordinary #3069 behaviour; a ceiling, if any,
    #                 is left exactly as it is.
    path = "unknown"
    if ceiling_target is not None:
        path = await _ceiling_path(container)
    if ceiling_target is not None and path != "unknown":
        mode = "ceiling"
    elif pct_configured:
        mode = "degraded"
    elif ceiling_off:
        mode = "off"
    else:
        mode = "none"
    target_str = str(ceiling_target) if mode == "ceiling" else ""

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
    key_now = raw_value  # the key as we understand it after any write this tick

    if config_verified:
        if mode == "degraded":
            reason = (
                "limits.memory or limits.memory.enforce unreadable"
                if ceiling_target is not None
                else "host SwapTotal unreadable, or the ceiling is below one page"
            )
            problems[_PROBLEM_CLASS_CEILING].append(
                f"swap ceiling not asserted this tick ({reason}); the persistent "
                "limits.memory.swap key was left untouched to avoid clobbering a "
                "ceiling with a boolean value",
            )
        elif mode == "ceiling" and path == "native":
            if raw_value != target_str:
                ok, err = await _incus_config_set(container, {"limits.memory.swap": target_str})
                if ok:
                    healed.append(
                        f"limits.memory.swap: {raw_value or 'unset'} → {target_str} bytes "
                        f"(native ceiling, {config.swap_ceiling_pct:g}% of host swap)",
                    )
                    key_now = target_str
                else:
                    problems[_PROBLEM_CLASS_CEILING].append(
                        f"incus config set limits.memory.swap={target_str} failed: {err}",
                    )
        elif mode == "off" and _is_byte_ceiling(raw_value):
            ok, err = await _incus_config_set(container, {"limits.memory.swap": "true"})
            if ok:
                healed.append(
                    f"swap ceiling removed: limits.memory.swap {raw_value} → true "
                    "(swap_ceiling_pct: off)",
                )
                key_now = "true"
            else:
                problems[_PROBLEM_CLASS_CEILING].append(
                    f"could not remove the swap ceiling (limits.memory.swap={raw_value}): {err}",
                )
        elif not _is_swap_on(raw_value):
            # "none", "off", and the fallback path: the key only has to be
            # swap-on (on the fallback path Incus ignores it as a ceiling).
            ok, err = await _incus_config_set(container, {"limits.memory.swap": "true"})
            if ok:
                note = (
                    "persists across restarts; ceiling enforced on the live cgroup"
                    if mode == "ceiling"
                    else "persists across restarts"
                )
                healed.append(f"limits.memory.swap: {raw_value or 'unset'} → true ({note})")
                key_now = "true"
            else:
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    f"incus config set limits.memory.swap=true failed: {err}",
                )
    else:
        logger.debug("swap_watch: no incus config signal (rc=%s)", rc)
        if mode == "ceiling":
            problems[_PROBLEM_CLASS_CEILING].append(
                "could not read limits.memory.swap, so the persistent half of the swap "
                "ceiling is unverified this tick; a container restart may not keep it",
            )
        elif mode == "off":
            problems[_PROBLEM_CLASS_CEILING].append(
                "could not read limits.memory.swap, so the swap ceiling was not removed "
                "this tick (a stored ceiling would come back at the next restart)",
            )

    # 2. Live cgroup. None = no signal (stopped container / cgroup v1).
    current = await read_swap_max(container)

    if mode == "ceiling":
        if current is not None and current != target_str:
            if await write_swap_max(container, target_str):
                healed.append(f"memory.swap.max: {current} → {target_str} bytes (live)")
            else:
                problems[_PROBLEM_CLASS_CEILING].append(
                    f"cgroup write of swap ceiling ({target_str} bytes) failed",
                )
                if current == "0":
                    problems[_PROBLEM_CLASS_SWAP_OFF].append(
                        "the live cgroup reads 0 (swap off) and the ceiling write failed, "
                        "so swap stays off until a write succeeds",
                    )
    elif current == "0":
        if _is_byte_ceiling(key_now):
            # Writing "max" here would replace an existing ceiling with
            # unlimited swap until the next restart. Incus applies a ceiling
            # with no zero window, so a live 0 under one was written from
            # outside: report it, never overwrite it.
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
                # knob was never verified/set: a restart can revert
                # memory.swap.max to 0. Surface it so the persistent half
                # gets fixed.
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    "activated swap live (memory.swap.max 0 → max) but could NOT "
                    "verify the persistent limits.memory.swap knob (incus config "
                    "read failed) — a container restart may revert swap to off",
                )
            if mode == "degraded":
                problems[_PROBLEM_CLASS_CEILING].append(
                    "the live cgroup read 0 (swap off) and was opened to max; the "
                    "configured swap ceiling could not be computed this tick, so "
                    "swap is uncapped until it can be",
                )
        else:
            problems[_PROBLEM_CLASS_SWAP_OFF].append(
                "live cgroup write failed — swap stays off until the next "
                "container start (set the persistent knob and restart to apply)",
            )
    elif (
        mode == "off"
        and config_verified
        and current not in (None, "max")
        and not _is_byte_ceiling(key_now)
    ):
        # A finite live cap with no byte-ceiling key behind it: the cgroup
        # fallback's cap (or one written from outside). "off" lifts it. When
        # the key still holds a ceiling (its removal failed above) or could
        # not be read at all, the cgroup is left alone so the two stay
        # consistent; the problem recorded above retries next tick.
        if await write_swap_max(container, "max"):
            healed.append(
                f"swap ceiling removed: memory.swap.max {current} → max (swap_ceiling_pct: off)"
            )
        else:
            problems[_PROBLEM_CLASS_CEILING].append(
                f"could not lift the live swap cap ({current} bytes) to max",
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
