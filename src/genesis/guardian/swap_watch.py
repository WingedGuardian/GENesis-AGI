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
2. **Live**: cgroup ``memory.swap.max == "0"`` → write ``max`` now — what
   incus would have written at start (``cgroup_ops.activate_swap_max``, the
   guardian-side twin of scripts/lib/container_swap.sh).
3. **Opt-in ceiling** (``swap_ceiling_pct`` in guardian config, install-local,
   never shipped in the repo's template): when set, computes a byte target
   from host ``SwapTotal`` and asserts it as the container's swap-ceiling —
   via Incus's own native ``limits.memory.swap=<bytes>`` key when the
   container has a ``limits.memory`` cap (the condition under which Incus
   actually applies that key as a ceiling rather than ignoring it), or via a
   direct cgroup write as a fallback otherwise. See ``_compute_swap_ceiling_target``.

Healthy path = two or three cheap reads, no writes, no alerts. A heal emits
one INFO alert (guardian self-actions must be visible; a step-1 heal and a
ceiling apply in the same tick are merged into one alert); a failed heal
emits a WARNING throttled PER PROBLEM CLASS by a state file (memory_watch
idiom, extended with a class key) so a persistent fault pages daily, not
per-tick, and a ceiling-only failure never mutes a later swap-off WARNING
(or vice versa). Never raises into the tick.

Deliberate override note: an operator who explicitly set
``limits.memory.swap=false`` will be reconciled back to ``true`` — swap-on is
a Genesis install invariant (docs/reference/memory-resilience.md). Disable the
reconciler itself (``swap_reconcile_enabled: false`` in guardian config) to
opt a host out.
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

# A persistent failure re-pages at most this often (state-file throttle).
_REALERT_HOURS = 24.0

_INCUS_TIMEOUT = 10.0

# Suffixes Incus's own ``units.ParseByteSizeString`` recognizes for
# ``limits.memory.swap``, and their multipliers (shared/units/units.go,
# v6.0.0 and main, read 2026-10-07). A bare integer (suffix "") is bytes.
# This mirrors Incus's parser closely enough to tell a byte-valued ceiling
# apart from garbage AND recover its numeric value (needed to compare an
# existing ceiling against a computed target without rewriting it every
# tick purely because of a cosmetic suffix difference, e.g. "8GiB" vs the
# same quantity written as a bare integer); it does not need to be a full
# re-implementation, since Incus itself is the authority that actually
# applies the value. One dict, not two in parallel, so the suffix SET
# (``_INCUS_BYTE_SUFFIXES``, derived below) can never drift from the
# multipliers used to parse it.
_INCUS_SUFFIX_MULTIPLIER: dict[str, int] = {
    "": 1,
    "B": 1,
    " bytes": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
    "EB": 1000**6,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "PiB": 1024**5,
    "EiB": 1024**6,
}
_INCUS_BYTE_SUFFIXES = frozenset(_INCUS_SUFFIX_MULTIPLIER)

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


def _parse_incus_bytes(raw: str) -> int | None:
    """The number of bytes ``raw`` encodes as a ``limits.memory.swap``
    byte-size value (a leading run of digits plus one of Incus's known
    suffixes), or None if it does not parse as one. Callers must rule out
    Incus's own boolean words first (see ``_is_swap_on``) — this function
    alone cannot tell "0" the byte size from "0" the bool.

    Matched on ASCII digits only, deliberately narrower than ``str.isdigit()``
    (which also accepts Unicode digit forms Go's byte-wise
    ``strconv.Atoi`` — the actual parser this mirrors — would reject): Incus
    validates this value server-side with the identical grammar at write time
    (``internal/instance/config.go``'s ``validate.IsSize``, which itself calls
    ``units.ParseByteSizeString``), so a value read back from ``incus config
    get`` can never contain a non-ASCII digit anyway. Matching the narrower
    grammar removes the gap rather than relying on that unreachability. The
    same bound rules out overflow-scale digit runs too: Incus's
    ``strconv.ParseInt`` rejects any integer portion that does not fit an
    int64, so ``IsSize`` — and thus ``incus config set`` — rejects it at
    write time as well, meaning ``incus config get`` can never return one
    either (fresh-context class audit on the stacked PR #3069, 2026-10-08).

    Leading zeros in the digit run are stripped BEFORE calling ``int()``:
    Incus's own parser (``strconv.ParseInt``-style accumulation) tolerates
    arbitrarily many leading zeros with no overflow, so a value like 4,301
    zeros followed by "1GiB" is a legitimate (if perverse) 1 GiB ceiling
    Incus can return from ``config get`` — but Python's ``int()`` raises
    ``ValueError`` past ~4,300 digits (``sys.get_int_max_str_digits``).
    Stripping first keeps the string handed to ``int()`` short regardless
    of the original length, with no change to the resulting value. (Codex
    finding on the stacked PR #3069, applied here to this sibling.)"""
    if not raw:
        return None
    i = 0
    while i < len(raw) and raw[i] in _ASCII_DIGITS:
        i += 1
    if i == 0:
        return None
    multiplier = _INCUS_SUFFIX_MULTIPLIER.get(raw[i:])
    if multiplier is None:
        return None
    digits = raw[:i].lstrip("0") or "0"
    return int(digits) * multiplier


def _is_parseable_incus_size(raw: str) -> bool:
    """Whether Incus would accept ``raw`` as a ``limits.memory.swap`` byte
    size AND that size is actually nonzero. See ``_parse_incus_bytes`` for
    the grammar. A ZERO-valued size ("0B", "0 bytes", "00GiB", …) parses
    structurally but represents zero bytes of additional swap — Incus's
    byte-size branch applies that value literally, so it disables swap
    exactly like the boolean FALSE spellings do. Returning True for it
    would leave a swap-off config unreconciled (Codex finding on the
    stacked PR #3069, applied here to this sibling)."""
    parsed = _parse_incus_bytes(raw)
    return parsed is not None and parsed != 0


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
    value would floor to less than one page.

    Page-aligned because the kernel's ``swap_max_write`` (mm/memcontrol.c)
    stores the write verbatim with NO rounding of its own — so alignment
    here is not defensive, it IS what makes a later readback equal exactly
    what was asked for; an unaligned target would never match and would be
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


def _cgroup_matches_target(current: str | None, target: int) -> bool:
    """True iff the live cgroup already encodes exactly ``target`` bytes.

    None (unreadable — no signal) and "max" (unbounded) never match a
    finite target: an unreadable cgroup must never be WRITTEN to (that is
    step 2's existing "no signal" contract, unchanged), and "unbounded"
    is never equal to "capped at N bytes" even when N happens to be large.
    """
    if current is None or current == "max":
        return False
    try:
        return int(current) == target
    except ValueError:
        return False


async def _limits_memory_is_set(container: str) -> bool:
    """Whether this container has a ``limits.memory`` cap — the condition
    under which Incus actually applies ``limits.memory.swap`` as a native
    swap-ceiling byte value rather than ignoring it (driver_lxc.go only
    touches the swap cgroup inside the ``if memory != ""`` branch). Used to
    choose between the native persistent-key path and the direct cgroup
    write fallback for the opt-in ceiling.

    Unreadable → False: fails toward the cgroup-write fallback, which still
    enforces the ceiling, rather than toward silently skipping it.
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
        return False
    return rc == 0 and stdout.strip() != ""


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("swap_watch alert dispatch failed", exc_info=True)


#: The two independent problem classes this watch can raise a WARNING for,
#: each throttled on its own timestamp — a ceiling-write failure (swap
#: itself is fine, just uncapped) must never silently mute the much more
#: serious "swap is actually off" WARNING for the next 24h, and vice versa.
_PROBLEM_CLASS_SWAP_OFF = "swap_off"
_PROBLEM_CLASS_CEILING = "ceiling"


def _failure_alert_due(state_file, now: datetime, problem_class: str) -> bool:
    """True when the WARNING throttle window has elapsed for ``problem_class``
    (or no state yet).

    Reads the LEGACY flat format (``{"last_failure_alert_at": "<iso>"}``,
    written before per-class throttling existed) as the ``swap_off`` class
    only — a pre-upgrade state file never recorded anything about a ceiling
    failure, so it must never be read as throttling one.
    """
    if not state_file.exists():
        return True
    try:
        data = json.loads(state_file.read_text())
        raw_at = data.get("last_failure_alert_at")
        if isinstance(raw_at, str):
            if problem_class != _PROBLEM_CLASS_SWAP_OFF:
                return True
            if not raw_at:
                return True
            last = datetime.fromisoformat(raw_at)
            return (now - last).total_seconds() >= _REALERT_HOURS * 3600
        if isinstance(raw_at, dict):
            class_at = raw_at.get(problem_class)
            if not class_at:
                return True
            last = datetime.fromisoformat(class_at)
            return (now - last).total_seconds() >= _REALERT_HOURS * 3600
        return True
    except (ValueError, OSError, TypeError, AttributeError):
        # TypeError: aware-vs-naive timestamp; AttributeError: valid JSON that
        # isn't a dict/str in the places expected. Both mean "state unusable"
        # -> alert is due, and neither may escape (module contract: never
        # raises into the tick).
        return True


def _record_failure_alert(state_file, now: datetime, problem_class: str) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if state_file.exists():
            try:
                existing = json.loads(state_file.read_text())
            except (ValueError, OSError):
                existing = {}
        raw_at = existing.get("last_failure_alert_at") if isinstance(existing, dict) else None
        if isinstance(raw_at, dict):
            by_class: dict[str, str] = dict(raw_at)
        elif isinstance(raw_at, str) and raw_at:
            # Migrate a LEGACY flat-format timestamp (pre-per-class) into the
            # dict under the swap_off class, instead of dropping it — it was
            # always a swap_off-class record (the only class that existed
            # before this PR), and discarding it here would silently erase
            # a real, still-valid throttle window the first time a DIFFERENT
            # class (ceiling) is the one being recorded. (genesis-architect
            # finding on this PR.)
            by_class = {_PROBLEM_CLASS_SWAP_OFF: raw_at}
        else:
            by_class = {}
        by_class[problem_class] = now.isoformat()
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
    ceiling_target = _compute_swap_ceiling_target(config)
    native_ceiling_applied = False

    # 1. Persistent knob. `incus config get` on an unset key returns rc=0 with
    # empty output, so unset and explicit-false both land in the set branch —
    # UNLESS a ceiling is configured and this container has a `limits.memory`
    # cap, in which case Incus's own native swap-ceiling path is in play and
    # this step asserts the CEILING instead of the plain boolean.
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
    if rc == 0:
        raw_value = stdout.strip()
        value = raw_value.lower()
        if ceiling_target is not None and await _limits_memory_is_set(container):
            native_ceiling_applied = True
            if _parse_incus_bytes(raw_value) != ceiling_target:
                try:
                    rc_set, _out, err_set = await _run_subprocess(
                        "incus",
                        "config",
                        "set",
                        container,
                        "limits.memory.swap",
                        str(ceiling_target),
                        timeout=_INCUS_TIMEOUT,
                    )
                except Exception as exc:
                    rc_set, err_set = 1, str(exc)
                if rc_set == 0:
                    healed.append(
                        f"limits.memory.swap: {value or 'unset'} → {ceiling_target} bytes "
                        f"(native ceiling, {config.swap_ceiling_pct:g}% of host swap)",
                    )
                else:
                    problems[_PROBLEM_CLASS_CEILING].append(
                        f"incus config set limits.memory.swap={ceiling_target} "
                        f"failed: {err_set.strip()}",
                    )
        elif not _is_swap_on(raw_value):
            try:
                rc_set, _out, err_set = await _run_subprocess(
                    "incus",
                    "config",
                    "set",
                    container,
                    "limits.memory.swap",
                    "true",
                    timeout=_INCUS_TIMEOUT,
                )
            except Exception as exc:
                rc_set, err_set = 1, str(exc)
            if rc_set == 0:
                healed.append(
                    f"limits.memory.swap: {value or 'unset'} → true (persists across restarts)",
                )
            else:
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    f"incus config set limits.memory.swap=true failed: {err_set.strip()}",
                )
    else:
        logger.debug("swap_watch: no incus config signal (rc=%s)", rc)

    # 2. Live cgroup.
    current = await read_swap_max(container)
    if current == "0":
        # Only "0" is the OFF defect. No ceiling configured → unchanged
        # behavior, through `activate_swap_max` exactly as before. A ceiling
        # configured → try writing it directly first; if THAT fails, fall
        # back to `activate_swap_max` (max/uncapped) rather than leaving
        # swap off — a failed ceiling is a worse-but-tolerable state, and
        # leaving swap off because a ceiling write failed would be strictly
        # worse than not having a ceiling at all.
        if ceiling_target is None:
            live_ok = await activate_swap_max(container)
            healed_value = "max"
        elif await write_swap_max(container, str(ceiling_target)):
            live_ok = True
            healed_value = f"{ceiling_target} bytes"
        elif await activate_swap_max(container):
            live_ok = True
            healed_value = "max"
            problems[_PROBLEM_CLASS_CEILING].append(
                f"cgroup write of swap ceiling ({ceiling_target} bytes) failed — "
                "fell back to uncapped (memory.swap.max=max) rather than leaving "
                "swap off",
            )
        else:
            live_ok = False
            healed_value = ""

        if live_ok:
            if config_verified:
                healed.append(f"memory.swap.max: 0 → {healed_value} (live, no restart needed)")
            else:
                # Live-healed, but the config read failed so the PERSISTENT knob
                # was never verified/set — a restart can revert memory.swap.max
                # to 0. Don't declare a clean "reconciled"; surface it so the
                # persistent half gets fixed. (Config set failing outright
                # already lands in problems above; this covers the read failing
                # while the cgroup was still writable — e.g. an incus socket
                # hiccup with the container running.)
                problems[_PROBLEM_CLASS_SWAP_OFF].append(
                    f"activated swap live (memory.swap.max 0 → {healed_value}) but "
                    "could NOT verify the persistent limits.memory.swap knob (incus "
                    "config read failed) — a container restart may revert swap to off",
                )
        else:
            problems[_PROBLEM_CLASS_SWAP_OFF].append(
                "live cgroup write failed — swap stays off until the next "
                "container start (set the persistent knob and restart to apply)",
            )
    elif (
        ceiling_target is not None
        and not native_ceiling_applied
        and current is not None
        and not _cgroup_matches_target(current, ceiling_target)
    ):
        # Fallback path: no `limits.memory` cap, so Incus's native
        # swap-ceiling handling never runs on this container at all —
        # enforce the ceiling directly on the cgroup instead.
        if await write_swap_max(container, str(ceiling_target)):
            healed.append(
                f"memory.swap.max: {current} → {ceiling_target} bytes "
                f"(cgroup fallback, no limits.memory cap, "
                f"{config.swap_ceiling_pct:g}% of host swap)",
            )
        else:
            problems[_PROBLEM_CLASS_CEILING].append(
                f"cgroup fallback write of swap ceiling ({ceiling_target} bytes) failed",
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
        logger.warning(
            "swap_watch problems (%s): %s",
            problem_class,
            "; ".join(class_problems),
        )
        now = datetime.now(UTC)
        state_file = config.state_path / "swap_watch_state.json"
        if _failure_alert_due(state_file, now, problem_class):
            title = (
                "Container swap reconcile FAILED"
                if problem_class == _PROBLEM_CLASS_SWAP_OFF
                else "Container swap ceiling FAILED"
            )
            await _send(
                dispatcher,
                AlertSeverity.WARNING,
                title,
                f"Guardian could not enforce the swap invariant on '{container}':\n- "
                + "\n- ".join(class_problems),
            )
            _record_failure_alert(state_file, now, problem_class)
