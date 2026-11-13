"""Health snapshot, provider activity, and Guardian dialogue routes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from flask import current_app, jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

# Bound the health snapshot so a slow/contended snapshot returns 503 fast
# instead of hanging the Flask worker — which previously made the dashboard
# spin forever and failed the host Guardian's health probe. The snapshot is
# ~2s after the call_sites + gather fixes; 15s is a generous never-hang
# backstop, well under browser/proxy timeouts.
_HEALTH_SNAPSHOT_TIMEOUT_S = 15.0

# How stale a /api/genesis/health response may be. The full snapshot is ~2s and
# every open dashboard tab polls this endpoint every 15s, so it is served from a
# short-lived cache; the Guardian probe reads the same endpoint. Bridge status
# and the healthy/unhealthy verdict are still computed fresh per request.
#
# The cache itself lives in HealthDataService, NOT here. It used to be three
# module globals in this file plus a generation counter, with a separate
# staleness flag inside the service — one concern split across two modules and
# two threads, which is what produced a defect in three consecutive review
# rounds. The service owns all of it now and is touched only from the event
# loop; this module owns only the freshness POLICY, which is what a route
# should decide.
_SNAPSHOT_CACHE_TTL_S = 30.0


def invalidate_snapshot_cache() -> None:
    """Force the next /api/genesis/health to recompute, from any thread.

    Call after any mutation to the data the snapshot reads. Without it a
    ``DELETE`` plus the client's immediate refetch is answered from the cache,
    so the UI shows a success toast beside the counts it just cleared (observed
    2026-08-30: "Cleared 148" next to "showing 20 of 148").

    Dropping a cached value is only half of it — a computation already in flight
    began from pre-mutation state — so this delegates to
    ``HealthDataService.invalidate()``, which drops the cache AND disowns that
    compute. Keeping both halves in the service is what lets a single object own
    the whole freshness contract.

    MARSHALLING is this function's real job. The dashboard is threaded WSGI
    Flask, so a mutating route reaches this from a request thread: an async
    route arrives via the worker thread that bridges with
    ``run_coroutine_threadsafe``, and a sync route (``routes/providers.py``
    toggles breakers in a plain ``def``) has no running loop at all. Hopping
    onto the runtime loop here means every field the service owns is read and
    written from exactly one thread — which is what removes the cross-thread
    hazards, rather than defending against them field by field.

    The hop is ordered, not merely eventual: ``call_soon_threadsafe`` appends to
    the loop's ready queue before this returns, and the client's next request is
    dispatched by a later ``call_soon_threadsafe``, so the invalidation runs
    first. Best-effort by design — this runs AFTER the caller's mutation has
    committed, so it must never turn completed work into a 500.
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    health_data = getattr(rt, "health_data", None)
    if health_data is None or not hasattr(health_data, "invalidate"):
        return

    target = None
    try:
        target = current_app.config.get("GENESIS_EVENT_LOOP")
    except RuntimeError:
        target = None  # outside an app context (tests, direct calls)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if target is None or target is running:
        # Already on the runtime loop, or no loop is configured (unit tests, a
        # non-served runtime). A direct call is correct in both cases.
        health_data.invalidate()
        return

    try:
        target.call_soon_threadsafe(health_data.invalidate)
    except RuntimeError:
        # Loop closed mid-shutdown. Nothing is running on it, so nobody can be
        # served a stale snapshot; invalidate directly rather than raising into
        # a route whose mutation already committed.
        health_data.invalidate()


@blueprint.route("/api/genesis/inflight")
def inflight_work():
    """The work a restart of this server would cancel right now
    (genesis.util.inflight): every Claude invocation, plus the whole life of a
    dispatched session or a CLI reflection. scripts/deploy_code_only.sh asks this
    before it stops or restarts the server.

    Names sessions, so it is not public: a VERIFIED dashboard session or the
    internal bearer (the trusted local caller's token) only. Verified, not
    ``is_authenticated``: that one opens up on an install with no dashboard
    password, which is a disclosure decision it must not make
    (``has_verified_credential``).
    """
    from genesis.dashboard.auth import has_internal_bearer, has_verified_credential
    from genesis.util.inflight import snapshot

    if not (has_internal_bearer() or has_verified_credential()):
        return jsonify({"error": "authentication required"}), 403
    return jsonify(
        {
            "items": [
                {"id": i.id, "kind": i.kind, "label": i.label, "started_at": i.started_at}
                for i in snapshot()
            ]
        }
    )


def redact_for_untrusted(snapshot: dict) -> None:
    """Reduce a health snapshot, in place, to what any caller may read.

    Every implementation of ``GET /api/genesis/health`` calls this on its own
    shallow copy before answering (this module's route and the Agent Zero
    adapter's, ``az_plugins/genesis/api_health.py``). The route is reachable
    from any network (``_OPEN_FROM_ANY_NETWORK``, for the host-side
    supervisor), so the fields below go only to the internal bearer or a
    verified dashboard session. Never to a peer by its address: behind a
    reverse proxy (or a container proxy device) every outside caller arrives
    from a trusted address. Verified, not ``is_authenticated``: with no
    dashboard password there is no credential, so nothing is revealed
    (``has_verified_credential``).

    The trust decision is made once, here. The helpers below take no part in
    it and always redact, so calling one directly can only withhold more.

    Two field-specific reductions keep the shapes the pages read, then
    ``_withhold_free_text`` reduces everything else to an allow-list: every
    number, boolean and null stays, and a string stays only where it is a
    status, name, id or timestamp. So exception and provider error text,
    database message columns and file contents never reach this caller,
    including in a field a producer adds later. ``free_text_hidden`` tells a
    page that text was withheld, so it can say so.
    """
    from genesis.dashboard.auth import has_internal_bearer, has_verified_credential

    if has_internal_bearer() or has_verified_credential():
        return
    _redact_checkout_paths(snapshot)
    _redact_call_site_responses(snapshot)
    reduced = _withhold_free_text(snapshot)
    snapshot.clear()  # the caller's own shallow copy: the cached dict is untouched
    snapshot.update(reduced)
    snapshot["free_text_hidden"] = True


def _redact_checkout_paths(snapshot: dict) -> None:
    """Drop the deploy checkout's edited file names, and the probe's error
    text (``redact_for_untrusted`` decides who this runs for).

    The names can be a private fork's or this install's own files; a probe
    error can name absolute paths on the install. The status and count stay
    public. Rebuilt as new dicts: ``snapshot`` is a shallow copy, so its
    nested dicts are the cached snapshot other callers read.

    The live-move repair's reports (``live_repairs``: report names and backup
    folders under the home directory) are reduced the same way to an
    allow-list of counts (_REPAIR_COUNTS): a key added later stays private.
    """
    deploy = snapshot.get("deploy_health")
    if not isinstance(deploy, dict):
        return
    checkout, repairs = deploy.get("main_checkout"), deploy.get("live_repairs")
    names = isinstance(checkout, dict) and bool({"paths", "reason"} & checkout.keys())
    if not (names or repairs is not None):
        return
    redacted = dict(deploy)
    if names:
        redacted["main_checkout"] = {
            k: v for k, v in checkout.items() if k not in ("paths", "reason")
        }
    if repairs is not None:  # anything but the collector's dict shows as nothing (fails closed)
        redacted["live_repairs"] = (
            {k: repairs.get(k) for k in _REPAIR_COUNTS} if isinstance(repairs, dict) else None
        )
    snapshot["deploy_health"] = redacted


#: The live_repairs keys an untrusted caller sees (deploy_health.collect_live_repairs).
_REPAIR_COUNTS = (
    "saved",
    "refused",
    "unread_older",
    "hooks_pending",
    "hooks_saved",
    "unresolved",
)


def _redact_call_site_responses(snapshot: dict) -> None:
    """Drop each call site's ``last_response`` (``redact_for_untrusted``
    decides who this runs for).

    That field is the text the site's last model call returned, as its caller
    recorded it (``call_site_last_run.response_text``). A foreground reply and
    a reflection are recorded whole, so it can carry whatever that call was
    given. Everything else about a site (status, provider chain, run time,
    token count) stays public. A site that had text gets
    ``last_response_hidden: true`` so a page can say the text was withheld
    instead of showing a site that never produced any.

    Rebuilt as new dicts, for the reason ``_redact_checkout_paths`` gives. A
    value that is not a site dict cannot be reduced by key, so it is served as
    an empty one (fails closed): that covers a failed section, whose shape is
    ``{"status": "error", "error": <exception text>}``. An empty dict, not
    null, because the dashboard pages read ``.status`` off every value.
    """
    sites = snapshot.get("call_sites")
    if sites is None:
        return
    if not isinstance(sites, dict):
        snapshot["call_sites"] = {}
        return
    redacted: dict = {}
    for site_id, site in sites.items():
        if not isinstance(site, dict):
            redacted[site_id] = {}
            continue
        entry = {k: v for k, v in site.items() if k != "last_response"}
        if site.get("last_response"):
            entry["last_response_hidden"] = True
        redacted[site_id] = entry
    snapshot["call_sites"] = redacted


# --------------------------------------------------------------------------
# Free text: an allow-list over the whole snapshot
# --------------------------------------------------------------------------
#
# The snapshot gathers twenty-odd producers, several of which merge in a probe's
# details whole and many of which put an exception's text, a provider's error
# body or a database message column into a string field. A list of the fields
# to remove would leak each such field added later, so the reduction runs the
# other way: a string is kept only where it is listed below as a status, name,
# id or timestamp, and only while its value still looks like one.

#: Keys whose string value is an enum, a name from config or code, an id, a
#: commit sha or a model id, wherever they appear in the snapshot. Each was
#: checked against every producer that writes it. Not here, because some
#: producer fills them with free text: ``error``, ``message``, ``reason``,
#: ``detail(s)``, ``summary``, ``path``, ``base``, ``branch``.
_SAFE_STRING_KEYS = frozenset(
    {
        # states and enums
        "status",
        "state",
        "level",
        "sub_state",
        "active_state",
        "current_state",
        "realtime_status",
        "budget_status",
        "key_health",
        "cb_state",
        "cb_reason",
        "criticality",
        "severity",
        "alert_severity",
        "provider_type",
        "category",
        "model_tier",
        "dispatch",
        "probe_status",
        "status_reason",
        "type",
        "wal_status",
        "cc_tier",
        "sys_tier",
        "scope",
        "verdict",
        "mode",
        "window",
        "source",
        "liveness_source",
        "last_trigger_source",
        "limit_kind",
        # names and ids from config, code or the database's own keys
        "name",
        "id",
        "site",
        "call_site",
        "slot",
        "peer",
        "profile",
        "provider",
        "active_provider",
        "last_run_provider",
        "model",
        "cc_model",
        "last_run_model",
        "original",
        "fallback",
        "class_key",
        "service_label",
        "service_unit",
        "main_pid",
        "hourly_budget",
        # commits
        "head",
        "live",
        "commit",
        "new_commit",
        "deployed_commit",
        # times a producer may write in a form other than ISO 8601
        "since",
        "reset_at",
    }
)

#: Keys whose dict maps a name to an enum (``bridge.resilience_state``).
_ENUM_MAP_KEYS = frozenset({"resilience_state"})

#: Keys whose list holds names or ids (call-site ids, unit names, memory
#: classes, wings and link types, memory ids).
_NAME_LIST_KEYS = frozenset(
    {
        "chain_usage",
        "sole_sites",
        "see_also",
        "affected_sites",
        "injected_ids",
        "missing_units",
        "classes",
        "wings",
        "by_type",
    }
)

#: Exact paths whose string is an enum here though the key is free text
#: elsewhere. ``*`` matches any one key, ``[]`` any list element.
_SAFE_STRING_PATHS = (
    ("cc_sessions", "fallback", "reason"),
    ("cc_sessions", "peer_availability", "[]", "reason"),
    ("bridge", "network", "cause"),  # resilience/network_sentinel.py constants
)

#: Exact paths whose string is prose written by code or config, never by an
#: exception, a provider or a user (``_call_site_meta``, ``model_routing``,
#: ``infrastructure.resilience_state_detail``): kept whatever its shape.
_CODE_PROSE_PATHS = (
    ("call_sites", "*", "description"),
    ("call_sites", "*", "frequency"),
    ("call_sites", "*", "cost_policy"),
    ("resilience", "summary"),
)

#: The one multi-word enum value a safe key carries (``resilience.level``).
_SPACED_ENUMS = frozenset({"not configured"})

#: What a kept enum, name, id or model id looks like: no whitespace, so no
#: sentence, and no leading slash, so no absolute path. ``~`` and ``[]`` are
#: model-id characters (a routing alias, a context-size suffix).
_TOKEN = re.compile(r"[A-Za-z0-9_.:@+()~\[\]-][A-Za-z0-9_.:@+()~\[\]/-]{0,127}")
#: An ISO 8601 date or time, which can carry no text and is kept under any key.
_ISO_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
#: A dict key the walk keeps: a name, never text.
_KEY = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}")


def _path_matches(path: tuple, patterns: tuple) -> bool:
    return any(
        len(p) == len(path) and all(a in ("*", b) for a, b in zip(p, path, strict=True))
        for p in patterns
    )


def _keeps(path: tuple, key: str, value: str, *, in_list: bool, enum_map: bool) -> bool:
    """May this string go to an uncredentialed caller?"""
    if value == "" or _ISO_TIME.fullmatch(value):
        return True
    if _path_matches(path, _CODE_PROSE_PATHS):
        return True
    named = (
        (key in _NAME_LIST_KEYS)
        if in_list
        else (key in _SAFE_STRING_KEYS or enum_map or _path_matches(path, _SAFE_STRING_PATHS))
    )
    return named and (bool(_TOKEN.fullmatch(value)) or value in _SPACED_ENUMS)


def _withhold_free_text(value, path: tuple = ()):
    """A copy of ``value`` with every string the allow-lists above do not name
    removed (``redact_for_untrusted`` decides who this runs for).

    Numbers, booleans and nulls are kept everywhere; a tuple is walked as a
    list, and any other object fails closed. A withheld string in a
    dict drops its key and sets ``<key>_hidden: true``; one in a list becomes
    null, and the dict holding the list gets ``<list key>_hidden: true``. A
    dict key that is text rather than a name drops its entry and sets
    ``keys_hidden: true``. Every dict and list is rebuilt, never reused, so
    nothing done to the result reaches the cached snapshot.
    """
    if not isinstance(value, dict):
        return value
    out: dict = {}
    enum_map = bool(path) and path[-1] in _ENUM_MAP_KEYS
    for key, item in value.items():
        if not (isinstance(key, str) and _KEY.fullmatch(key)):
            out["keys_hidden"] = True
            continue
        here = (*path, key)
        if isinstance(item, str):
            if _keeps(here, key, item, in_list=False, enum_map=enum_map):
                out[key] = item
            else:
                out[f"{key}_hidden"] = True
        elif isinstance(item, dict):
            out[key] = _withhold_free_text(item, here)
        elif isinstance(item, (list, tuple)):
            out[key], hidden = _withhold_in_list(item, here, key)
            if hidden:
                out[f"{key}_hidden"] = True
        elif _is_scalar(item):
            out[key] = item
        else:  # an object the JSON encoder would expand (a dataclass): fails closed
            out[f"{key}_hidden"] = True
    return out


def _withhold_in_list(items: list | tuple, path: tuple, key: str) -> tuple[list, bool]:
    """``_withhold_free_text`` for a list held under ``key``; returns the copy
    and whether any string in it (or in a list nested in it) was withheld."""
    here = (*path, "[]")
    out: list = []
    hidden = False
    for item in items:
        if isinstance(item, str):
            if _keeps(here, key, item, in_list=True, enum_map=False):
                out.append(item)
            else:
                out.append(None)
                hidden = True
        elif isinstance(item, dict):
            out.append(_withhold_free_text(item, here))
        elif isinstance(item, (list, tuple)):
            nested, nested_hidden = _withhold_in_list(item, here, key)
            out.append(nested)
            hidden = hidden or nested_hidden
        elif _is_scalar(item):
            out.append(item)
        else:
            out.append(None)
            hidden = True
    return out, hidden


def _is_scalar(value) -> bool:
    """A number, boolean or null: kept wherever it appears."""
    return value is None or isinstance(value, (bool, int, float))


@blueprint.route("/api/genesis/health")
@_async_route(timeout=_HEALTH_SNAPSHOT_TIMEOUT_S)
async def health_snapshot():
    """Return system health snapshot with bridge status from status.json."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.health_data is None:
        return jsonify({"status": "unhealthy", "error": "not bootstrapped"}), 503

    # dict() copy, not the shared object: `bridge` and `status` below are
    # per-request and must never be written into the cached snapshot other
    # callers read.
    snapshot = dict(await rt.health_data.snapshot(max_age_s=_SNAPSHOT_CACHE_TTL_S))

    status_path = Path.home() / ".genesis" / "status.json"
    bridge_health = None
    try:
        raw = status_path.read_text()
        bridge_health = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass

    snapshot["bridge"] = bridge_health

    # Decided from the full snapshot, before any redaction: the supervisor and
    # the deploy scripts act on this status code, so what an uncredentialed
    # caller may read must never change it.
    infra = snapshot.get("infrastructure", {})
    db_status = infra.get("genesis.db", {}).get("status", "") if isinstance(infra, dict) else ""
    healthy = rt.is_bootstrapped and db_status == "healthy"

    redact_for_untrusted(snapshot)
    snapshot["status"] = "healthy" if healthy else "unhealthy"
    status_code = 200 if healthy else 503

    return jsonify(snapshot), status_code


@blueprint.route("/api/genesis/heartbeat")
@_async_route
async def heartbeat_canary():
    """Heartbeat canary for the Guardian — confirms awareness loop is alive."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"alive": False, "reason": "not bootstrapped"}), 503

    tick_count = 0
    last_tick_at = None
    if rt.awareness_loop is not None:
        tick_count = rt.awareness_loop.tick_count
        last_tick_at = rt.awareness_loop.last_tick_at

    return jsonify(
        {
            "alive": True,
            "tick_count": tick_count,
            "last_tick_at": last_tick_at,
        }
    ), 200


@blueprint.route("/api/genesis/liveness")
def liveness_probe():
    """Off-loop liveness probe — answers even when the event loop is STARVED.

    Deliberately a SYNC route (NO ``@_async_route``): Flask serves it on its own
    worker thread (``threaded=True``), so it never bounces onto the runtime event
    loop and therefore still responds when background work has starved that loop —
    the exact case where ``/api/genesis/heartbeat`` (async) hangs indefinitely,
    indistinguishable from a dead process. Reads only plain in-memory values (the
    off-loop loop-health sample + the awareness loop's integer counters); never
    touches a coroutine, the event bus, or any loop-bound async object.

    Always HTTP 200 when the process + Flask thread are alive — classification
    (down / starved / wedged / responsive) is the CALLER's job. Fail-closed: any
    field it cannot read is ``null`` (UNKNOWN), never a healthy-looking default, so
    a consumer must treat ``loop: null`` as unknown rather than clear.
    """
    from genesis.util import loop_health

    loop_block = None
    sample = loop_health.read()
    if sample is not None:
        loop_block = {
            "lag_ms": round(sample.drift_ms, 1),
            "peak_ms": round(sample.peak_ms, 1),
            "lagging": sample.lagging,
            "threshold_ms": sample.threshold_ms,
            "executor": sample.executor,
            # Computed at READ time (see loop_health.age_s) — a growing age under
            # starvation is the WEDGED signal.
            "sample_age_s": round(loop_health.age_s(sample), 3),
        }

    awareness_block = None
    try:
        from genesis.runtime import GenesisRuntime

        # peek() — never lazily constructs a blank singleton (this is a read-only
        # observability path); None before bootstrap → awareness stays null.
        rt = GenesisRuntime.peek()
        if rt is not None and rt.is_bootstrapped and rt.awareness_loop is not None:
            awareness_block = {
                "tick_count": rt.awareness_loop.tick_count,
                "last_tick_at": rt.awareness_loop.last_tick_at,
            }
    except Exception:
        awareness_block = None

    return jsonify(
        {
            "alive": True,
            "loop": loop_block,
            "awareness": awareness_block,
        }
    ), 200


@blueprint.route("/api/genesis/provider-activity")
@_async_route
async def provider_activity():
    """Return per-provider call stats from the activity tracker."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.activity_tracker is None:
        return jsonify([])

    provider_name = request.args.get("provider")
    if provider_name:
        result = rt.activity_tracker.summary(provider_name)
        if isinstance(result, dict):
            return jsonify([result])
        return jsonify(result)

    result = await rt.activity_tracker.summary_with_db_fallback()
    return jsonify(result)


# GROUNDWORK(guardian-dialogue): Self-heal protocol endpoint.
# V4 Step 1: acknowledge concern + respond need_help (no self-healing yet).
# V4.5+: Genesis inspects its own state and attempts self-repair.
@blueprint.route("/api/genesis/guardian-dialogue", methods=["POST"])
@_async_route
async def guardian_dialogue():
    """Receive a health concern from the Guardian and respond.

    Protocol: Guardian sends failing signals, Genesis responds with
    one of: handling, need_help, stand_down.
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()

    if not rt.is_bootstrapped:
        return jsonify(
            {
                "acknowledged": False,
                "status": "need_help",
                "action": "",
                "eta_s": 0,
                "context": "Genesis is not bootstrapped",
            }
        ), 503

    # Check if Genesis is paused — Guardian should stand down
    if rt.paused:
        pause_reason = ""
        try:
            pause_path = Path.home() / ".genesis" / "paused.json"
            if pause_path.exists():
                data = json.loads(pause_path.read_text())
                pause_reason = data.get("reason", "")
        except (json.JSONDecodeError, OSError):
            pass

        return jsonify(
            {
                "acknowledged": True,
                "status": "stand_down",
                "action": "paused",
                "eta_s": 0,
                "context": f"Genesis is paused: {pause_reason}"
                if pause_reason
                else "Genesis is paused",
            }
        ), 200

    # Log the concern for observability
    try:
        concern = request.get_json(silent=True) or {}
        failing = concern.get("signals_failing", [])
        logger.warning(
            "Guardian health concern received: signals_failing=%s, duration_s=%s",
            failing,
            concern.get("duration_s"),
        )
    except (ValueError, TypeError, AttributeError) as exc:
        logger.debug("Failed to parse Guardian concern payload: %s", exc, exc_info=True)

    # Dispatch Sentinel if available — container-side guardian handles it.
    # Guardian uses sentinel_state for event-driven standing (no wall-clock
    # timeout). As long as Sentinel is active, Guardian waits indefinitely.
    sentinel = getattr(rt, "_sentinel", None)
    if sentinel is not None and not sentinel.is_active:
        try:
            from genesis.sentinel import SentinelRequest
            from genesis.util.tasks import tracked_task

            tracked_task(
                sentinel.dispatch(
                    SentinelRequest(
                        trigger_source="guardian_dialogue",
                        trigger_reason=f"Guardian concern: signals_failing={failing}",
                        tier=2,
                        context=concern,
                    )
                ),
                name="sentinel-guardian-dialogue",
            )
            # Sentinel just dispatched — report its initial state
            s_state = getattr(sentinel, "_state", None)
            state_val = s_state.current_state if s_state else "investigating"
            return jsonify(
                {
                    "acknowledged": True,
                    "status": "handling",
                    "action": "sentinel_dispatched",
                    "eta_s": 0,
                    "sentinel_state": state_val,
                    "context": f"Sentinel dispatched ({state_val})",
                }
            ), 200
        except Exception:
            logger.warning("Sentinel dispatch failed — falling back to need_help", exc_info=True)
    elif sentinel is not None and sentinel.is_active:
        # Sentinel is already mid-remediation — tell Guardian to wait.
        # Without this, Guardian interprets the fallthrough as "need_help"
        # and escalates, causing competing restart attempts.
        s_state = getattr(sentinel, "_state", None)
        state_val = s_state.current_state if s_state else "investigating"
        return jsonify(
            {
                "acknowledged": True,
                "status": "handling",
                "action": "sentinel_already_active",
                "eta_s": 0,
                "sentinel_state": state_val,
                "context": f"Sentinel active ({state_val})",
            }
        ), 200

    return jsonify(
        {
            "acknowledged": True,
            "status": "need_help",
            "action": "",
            "eta_s": 0,
            "context": "Genesis acknowledges the concern but cannot self-repair (Sentinel unavailable)",
        }
    ), 200
