"""Entity adjudication drainer — decide merge-vs-distinct for fuzzy entity PAIRS.

This is the consumer for ``work_type='entity_adjudication'`` rows in
``deferred_work_queue`` (producer: ``entities.enqueue_adjudication``, fired when
``resolve_entity`` creates an entity whose name is difflib-close to an existing
one). It is NOT the memory-pair dedup lane (``dream_entity_scan`` /
``entity_resolution_audit``) — this operates on entity NODES.

Posture — conservative, because a wrong merge is worse than a wrong split:
- A cheap **digit-guard** mechanically rules "distinct" for pairs that differ only
  by digits (``pr #989`` vs ``pr #990``) — the ~76% of fuzzy hits that are
  definitionally distinct — with zero LLM cost.
- Otherwise a **two-model** judgment (primary + flipped-provider challenge) must
  BOTH say "merge"; any disagreement or error falls to "distinct".
- In ``propose_only`` mode (default) a merge is RECORDED as ``proposed_merge`` and
  NOT applied. In ``live`` mode it is applied via ``merge_entity`` (loser
  tombstoned into survivor), and stored proposals from the shadow period apply on
  the flip, guarded against staleness.

Every verdict is deduped and recorded in ``entity_adjudications`` (order-independent
``pair_key``). One aggregate observation per run summarises what changed.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from genesis.db.crud import deferred_work as dw_crud
from genesis.db.crud import entities as entities_crud
from genesis.db.crud import entity_adjudications as adj_crud
from genesis.db.crud import memory as memory_crud
from genesis.db.crud import observations
from genesis.memory.adversarial_review import _JSON_BLOCK_RE

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

WORK_TYPE = "entity_adjudication"
CALL_SITE_PRIMARY = "entity_adjudication"
CALL_SITE_CHALLENGE = "entity_adjudication_challenge"

_MAX_ATTEMPTS = 5
# Fuzzy-comparison groups mirror entity_registry.resolve_entity: concept-cluster
# types are compared cross-type within the cluster; person/org each same-type.
_CONCEPT_CLUSTER = frozenset({"product", "device", "concept", "subsystem", "repo"})
_FUZZY_THRESHOLD = 0.85
_SNIPPET_CHARS = 300
_MENTIONS_PER_ENTITY = 3

_ADJUDICATION_PROMPT = """\
You are deduplicating a knowledge graph's ENTITY NODES. These two entities already
passed a text-similarity filter, so their names ARE close. Your job is to decide
WHY they are close:

- MERGE if the difference is purely COSMETIC — the same words/terms written
  differently: spacing, hyphenation, underscores, casing, punctuation, delimiters,
  or word order. These are one real-world thing recorded two ways.
  Examples to merge: "neural monitor" / "neural-monitor" / "neural_monitor";
  "dispatch: cli" / "dispatch=cli"; "dream cycle" / "dream-cycle" / "dream cycles".
- DISTINCT if there is a SEMANTIC difference — a different word, a version/number/
  letter suffix, or a specific sub-item vs its parent.
  Examples to keep distinct: "system" vs "systemd" (different program);
  "safety gate" vs "safety gap" (different word: gate ≠ gap); "PR-1" vs "PR-1a"
  (different identifiers); a project vs a numbered issue under it.

Entity A:
{profile_a}

Entity B:
{profile_b}

Respond with JSON only, no other text:
{{"verdict": "merge|distinct", "reasoning": "one sentence"}}

If a genuine semantic difference is plausible, choose "distinct" — a wrong merge
erases a real distinction. But do NOT call a pure formatting difference "distinct"."""

_CHALLENGE_PROMPT = """\
A reviewer judged these two entity nodes to be the SAME real-world thing and wants
to merge them (one is absorbed into the other, irreversibly). CHALLENGE that — but
only on SEMANTIC grounds.

Entity A:
{profile_a}

Entity B:
{profile_b}

The names are already known to be textually similar. A merge is WRONG only if
there is a genuine MEANING difference — a different word, a distinguishing
version/number/letter, or a specific sub-item vs its parent (e.g. "system" vs
"systemd", "safety gate" vs "safety gap", "PR-1" vs "PR-1a"). A mere formatting
difference (spacing, hyphenation, casing, punctuation, delimiter, word order of the
SAME terms) is NOT a reason to keep them apart — that is the same thing written two
ways.

Respond with JSON only, no other text:
{{"verdict": "merge|distinct", "reasoning": "one sentence"}}

Say "distinct" ONLY if you can name a real semantic difference; otherwise "merge"."""


# ── digit-guard ──────────────────────────────────────────────────────────────


def _digit_collapse(s: str) -> str:
    return re.sub(r"\d+", "#", s)


def digit_only_difference(a: str, b: str) -> bool:
    """True when two names differ ONLY by digit runs (``pr #989`` vs ``pr #990``).

    These are definitionally distinct — a numbered series — and must never be
    merged. Ruling them out mechanically saves the ~76% of fuzzy hits that are
    numeric-suffix collisions from ever reaching the LLM.
    """
    return a != b and _digit_collapse(a) == _digit_collapse(b)


# ── entity loading / redirect resolution ─────────────────────────────────────


async def _resolve_active(db: aiosqlite.Connection, entity_id: str) -> dict | None:
    """Follow ``merged_into`` redirects to the active survivor, or None if the
    chain dead-ends in a merged-with-no-target / gone / missing entity.

    Uses ``get_entity`` (raw row, does NOT follow merges) — the redirect walk is
    done here."""
    seen: set[str] = set()
    current = entity_id
    while current and current not in seen:
        seen.add(current)
        ent = await entities_crud.get_entity(db, current)
        if ent is None:
            return None
        if ent["status"] == "active":
            return ent
        if ent["status"] == "merged" and ent["merged_into"]:
            current = ent["merged_into"]
            continue
        return None  # gone, or merged with no target
    return None


# ── LLM adjudication ─────────────────────────────────────────────────────────


async def _entity_profile(db: aiosqlite.Connection, ent: dict) -> str:
    """Compact profile for the LLM: name, type, summary, top mention snippets."""
    lines = [
        f"Name: {ent['name']}",
        f"Type: {ent['entity_type']}",
        f"Summary: {ent.get('summary') or '(none)'}",
    ]
    try:
        mentions = await entities_crud.memories_mentioning(
            db, [ent["entity_id"]], limit_per_entity=_MENTIONS_PER_ENTITY
        )
    except Exception:
        mentions = []
    snippets: list[str] = []
    for m in mentions:
        mem = await memory_crud.get_by_id(db, m["memory_id"])
        if mem and mem.get("content"):
            snippets.append(mem["content"].strip().replace("\n", " ")[:_SNIPPET_CHARS])
    if snippets:
        lines.append("Mentioned in:")
        lines.extend(f"- {s}" for s in snippets)
    return "\n".join(lines)


def _parse_verdict(text: str) -> dict[str, str] | None:
    """Parse a ``{"verdict": ..., "reasoning": ...}`` JSON reply, fence-tolerant.

    Returns None on any parse failure so the caller can fail safe to 'distinct'.
    """
    text = (text or "").strip()
    match = _JSON_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    verdict = data.get("verdict")
    if verdict not in ("merge", "distinct"):
        return None
    return {"verdict": verdict, "reasoning": str(data.get("reasoning", ""))}


def _retry(reason: str) -> dict[str, Any]:
    """A non-answer (infra/parse failure): keep both for now, but signal the
    caller to RETRY rather than record a permanent verdict — a transient outage
    must not burn a pair to 'distinct' forever."""
    return {"verdict": "distinct", "reasoning": reason, "provider": "error", "retryable": True}


async def adjudicate_pair(router: Router, profile_a: str, profile_b: str) -> dict[str, Any]:
    """Two-model merge-vs-distinct judgment. Returns
    ``{"verdict": "merge"|"distinct", "reasoning": str, "provider": str,
    "retryable": bool}``.

    A "merge" requires the primary AND the flipped-provider challenge to BOTH
    say merge. A model genuinely saying "distinct" is a real verdict (recorded).
    An infra/parse failure sets ``retryable=True`` (fail-safe distinct, but the
    caller re-queues instead of recording — a transient outage never burns a
    pair to a permanent verdict).
    """
    prompt = _ADJUDICATION_PROMPT.format(profile_a=profile_a, profile_b=profile_b)
    try:
        primary = await router.route_call(
            CALL_SITE_PRIMARY,
            [{"role": "user", "content": prompt}],
            suppress_dead_letter=True,
        )
    except Exception as exc:
        logger.debug("Adjudication primary call error", exc_info=True)
        return _retry(f"primary error: {exc}")
    if not primary.success:
        return _retry(f"primary LLM error: {primary.error}")
    primary_verdict = _parse_verdict(primary.content or "")
    if primary_verdict is None:
        return _retry("primary parse error")
    prov_p = primary.provider_used or "?"
    if primary_verdict["verdict"] == "distinct":
        return {**primary_verdict, "provider": prov_p, "retryable": False}

    # Primary said merge — require the challenge to agree.
    challenge_prompt = _CHALLENGE_PROMPT.format(profile_a=profile_a, profile_b=profile_b)
    try:
        challenge = await router.route_call(
            CALL_SITE_CHALLENGE,
            [{"role": "user", "content": challenge_prompt}],
            suppress_dead_letter=True,
        )
    except Exception as exc:
        logger.debug("Adjudication challenge call error", exc_info=True)
        return _retry(f"challenge error: {exc}")
    if not challenge.success:
        return _retry(f"challenge LLM error: {challenge.error}")
    challenge_verdict = _parse_verdict(challenge.content or "")
    if challenge_verdict is None:
        return _retry("challenge parse error")
    if challenge_verdict["verdict"] != "merge":
        # Real dissent — the challenge genuinely says distinct. Record it.
        return {
            "verdict": "distinct",
            "reasoning": f"challenge overrode: {challenge_verdict['reasoning']}",
            "provider": prov_p,
            "retryable": False,
        }
    prov_c = challenge.provider_used or "?"
    return {
        "verdict": "merge",
        "reasoning": primary_verdict["reasoning"],
        "provider": f"{prov_p}+{prov_c}",
        "retryable": False,
    }


# ── survivor selection ───────────────────────────────────────────────────────


async def _pick_survivor(db: aiosqlite.Connection, ent_a: dict, ent_b: dict) -> tuple[str, str]:
    """Return ``(survivor_id, loser_id)``: keep the better-attested entity.

    More mentions wins (``merge_entity`` warns the loser is often the
    better-attested record). Ties break to the older ``updated_at`` — the more
    established node — falling back to id order for determinism.
    """
    ca = await entities_crud.count_entity_mentions(db, ent_a["entity_id"])
    cb = await entities_crud.count_entity_mentions(db, ent_b["entity_id"])
    if ca != cb:
        return (
            (ent_a["entity_id"], ent_b["entity_id"])
            if ca > cb
            else (ent_b["entity_id"], ent_a["entity_id"])
        )
    ua, ub = ent_a.get("updated_at") or "", ent_b.get("updated_at") or ""
    if ua != ub:
        return (
            (ent_a["entity_id"], ent_b["entity_id"])
            if ua < ub
            else (ent_b["entity_id"], ent_a["entity_id"])
        )
    lo, hi = sorted((ent_a["entity_id"], ent_b["entity_id"]))
    return lo, hi


# ── drain ────────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def run_adjudication_drain(
    db: aiosqlite.Connection,
    router: Router,
    *,
    mode: str,
    budget: int = 20,
) -> dict[str, int]:
    """Drain up to ``budget`` pending adjudication rows. Returns a counts summary.

    In ``live`` mode a Phase 0 first applies any ``proposed_merge`` backlog stored
    during the shadow period (with a staleness guard). Phase 1 then judges pending
    queue rows. Emits one aggregate observation if anything changed.
    """
    counts = {
        "judged": 0,
        "distinct": 0,
        "mechanical_distinct": 0,
        "proposed": 0,
        "merged": 0,
        "stale": 0,
        "noop": 0,
        "discarded": 0,
        "retried": 0,
    }
    example_merges: list[str] = []

    if mode == "live":
        await _apply_proposed_backlog(db, counts, example_merges, budget=budget)

    rows = await dw_crud.query_pending(db, work_type=WORK_TYPE, limit=budget)
    for item in rows:
        try:
            await _process_row(db, router, item, mode, counts, example_merges)
        except Exception:
            logger.exception("Adjudication row failed — resetting to pending: %s", item.get("id"))
            await dw_crud.update_status(db, item["id"], status="pending")

    await _emit_run_observation(db, counts, example_merges, mode)
    return counts


async def _process_row(
    db: aiosqlite.Connection,
    router: Router,
    item: dict,
    mode: str,
    counts: dict[str, int],
    example_merges: list[str],
) -> None:
    item_id = item["id"]
    try:
        payload = json.loads(item.get("payload_json") or "{}")
        eid_new = payload["entity_id"]
        eid_similar = payload["similar_entity_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        await dw_crud.update_status(
            db,
            item_id,
            status="discarded",
            error_message="unparseable entity_adjudication payload",
            completed_at=_now(),
        )
        counts["discarded"] += 1
        return

    if item.get("attempts", 0) >= _MAX_ATTEMPTS:
        await _exhaust(db, item, counts)
        return

    ent_a = await _resolve_active(db, eid_new)
    ent_b = await _resolve_active(db, eid_similar)
    # Gone, or already converged to the same survivor → nothing to decide.
    if ent_a is None or ent_b is None or ent_a["entity_id"] == ent_b["entity_id"]:
        await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())
        counts["noop"] += 1
        return

    # Already judged this pair (non-stale verdict) → don't re-spend an LLM call.
    existing = await adj_crud.get_by_pair(db, ent_a["entity_id"], ent_b["entity_id"])
    if existing is not None and existing["verdict"] != "stale":
        await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())
        counts["noop"] += 1
        return

    # Digit-guard — mechanical distinct, zero LLM cost.
    if digit_only_difference(ent_a["norm_name"], ent_b["norm_name"]):
        await adj_crud.record_verdict(
            db,
            entity_a=ent_a["entity_id"],
            entity_b=ent_b["entity_id"],
            verdict="distinct",
            reasoning="names differ only by digits",
            provider="mechanical",
            mode=mode,
            norm_a=ent_a["norm_name"],
            norm_b=ent_b["norm_name"],
        )
        await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())
        counts["judged"] += 1
        counts["mechanical_distinct"] += 1
        return

    await dw_crud.update_status(db, item_id, status="processing", last_attempt_at=_now())
    profile_a = await _entity_profile(db, ent_a)
    profile_b = await _entity_profile(db, ent_b)
    result = await adjudicate_pair(router, profile_a, profile_b)

    # Infra/parse non-answer → re-queue (attempts already incremented above; the
    # top-of-function cap eventually discards a persistently-failing pair). Never
    # record a permanent verdict from a transient outage.
    if result.get("retryable"):
        await dw_crud.update_status(db, item_id, status="pending")
        counts["retried"] += 1
        return

    counts["judged"] += 1

    common = dict(
        entity_a=ent_a["entity_id"],
        entity_b=ent_b["entity_id"],
        reasoning=result["reasoning"],
        provider=result["provider"],
        mode=mode,
        norm_a=ent_a["norm_name"],
        norm_b=ent_b["norm_name"],
        updated_a=ent_a.get("updated_at"),
        updated_b=ent_b.get("updated_at"),
    )

    if result["verdict"] == "distinct":
        await adj_crud.record_verdict(db, verdict="distinct", **common)
        await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())
        counts["distinct"] += 1
        return

    # verdict == merge
    if mode == "live":
        # Extraction-race guard: profile-building + two LLM calls opened an await
        # gap since we resolved these. Re-resolve immediately before the
        # irreversible merge; if either side moved (merged/renamed/gone) or they
        # converged, record the proposal instead of applying a stale merge.
        fresh_a = await _resolve_active(db, ent_a["entity_id"])
        fresh_b = await _resolve_active(db, ent_b["entity_id"])
        if (
            fresh_a is None
            or fresh_b is None
            or fresh_a["entity_id"] != ent_a["entity_id"]
            or fresh_b["entity_id"] != ent_b["entity_id"]
        ):
            survivor_id, loser_id = await _pick_survivor(db, ent_a, ent_b)
            await adj_crud.record_verdict(
                db,
                verdict="proposed_merge",
                loser_id=loser_id,
                survivor_id=survivor_id,
                **common,
            )
            await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())
            counts["proposed"] += 1
            return
        # Pick the survivor from the FRESH entities — a concurrent write may have
        # changed the mention counts survivor selection is based on, so choosing
        # from the pre-race snapshot could tombstone the now-better-attested node.
        survivor_id, loser_id = await _pick_survivor(db, fresh_a, fresh_b)
        await entities_crud.merge_entity(db, loser_id=loser_id, survivor_id=survivor_id)
        await adj_crud.record_verdict(
            db,
            verdict="merge",
            loser_id=loser_id,
            survivor_id=survivor_id,
            applied_at=_now(),
            **common,
        )
        counts["merged"] += 1
        _note_merge(example_merges, ent_a, ent_b)
    else:  # propose_only
        survivor_id, loser_id = await _pick_survivor(db, ent_a, ent_b)
        await adj_crud.record_verdict(
            db,
            verdict="proposed_merge",
            loser_id=loser_id,
            survivor_id=survivor_id,
            **common,
        )
        counts["proposed"] += 1
        _note_merge(example_merges, ent_a, ent_b)
    await dw_crud.update_status(db, item_id, status="completed", completed_at=_now())


async def _apply_proposed_backlog(
    db: aiosqlite.Connection,
    counts: dict[str, int],
    example_merges: list[str],
    *,
    budget: int,
) -> None:
    """Live-mode Phase 0: apply proposed_merge verdicts stored during shadow.

    Staleness guard: an identity that drifted since the proposal (one side
    merged/renamed/gone) is marked ``stale`` and NOT applied.
    """
    proposals = await adj_crud.list_proposed_merges(db, limit=budget)
    for p in proposals:
        # A `stale` verdict is not a dead end: the reconcile sweep's dedup set
        # (settled_pair_keys) excludes stale, so a stale pair is re-enqueued on
        # the next sweep pass and re-adjudicated with its current identities.
        ent_a = await _resolve_active(db, p["entity_a"])
        ent_b = await _resolve_active(db, p["entity_b"])
        if ent_a is None or ent_b is None or ent_a["entity_id"] == ent_b["entity_id"]:
            await adj_crud.mark_stale(db, pair_key=p["pair_key"])
            counts["stale"] += 1
            continue
        # norm_name drift → the thing we judged is not the thing we'd merge now.
        if ent_a["norm_name"] != p.get("norm_a") or ent_b["norm_name"] != p.get("norm_b"):
            await adj_crud.mark_stale(db, pair_key=p["pair_key"])
            counts["stale"] += 1
            continue
        survivor_id, loser_id = await _pick_survivor(db, ent_a, ent_b)
        await entities_crud.merge_entity(db, loser_id=loser_id, survivor_id=survivor_id)
        await adj_crud.mark_applied(
            db, pair_key=p["pair_key"], loser_id=loser_id, survivor_id=survivor_id
        )
        counts["merged"] += 1
        _note_merge(example_merges, ent_a, ent_b)


async def _exhaust(db: aiosqlite.Connection, item: dict, counts: dict[str, int]) -> None:
    """Discard a row that has hit the attempt cap; surface it once in health."""
    item_id = item["id"]
    reason = f"entity_adjudication exhausted {_MAX_ATTEMPTS} attempts"
    await dw_crud.update_status(
        db, item_id, status="discarded", error_message=reason, completed_at=_now()
    )
    counts["discarded"] += 1
    try:
        content = json.dumps({"deferred_id": item_id, "payload": item.get("payload_json")})
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="entity_adjudication",
            type="deferred_work_exhausted",
            content=content,
            priority="medium",
            created_at=_now(),
            content_hash=hashlib.sha256(reason.encode()).hexdigest(),
            skip_if_duplicate=True,
        )
    except Exception:
        logger.error("Failed to write exhaustion observation for %s", item_id, exc_info=True)


def _note_merge(acc: list[str], ent_a: dict, ent_b: dict) -> None:
    if len(acc) < 5:
        acc.append(f"{ent_a['name']!r} ⇄ {ent_b['name']!r}")


async def _emit_run_observation(
    db: aiosqlite.Connection,
    counts: dict[str, int],
    example_merges: list[str],
    mode: str,
) -> None:
    """One aggregate, user-visible observation per run that changed something.

    Type ``entity_adjudication`` is deliberately NOT in INTERNAL_OBS_TYPES, so it
    surfaces in the morning report (digest-note visibility). Aggregate, never
    per-pair — a busy run is one line, not a flood.
    """
    changed = counts["merged"] + counts["proposed"] + counts["stale"]
    if changed == 0:
        return
    if mode == "live":
        headline = f"Genesis merged {counts['merged']} duplicate entities"
    else:
        headline = f"Genesis proposed {counts['proposed']} entity merges (shadow)"
    content = json.dumps(
        {
            "headline": headline,
            "mode": mode,
            "merged": counts["merged"],
            "proposed": counts["proposed"],
            "stale": counts["stale"],
            "judged": counts["judged"],
            "examples": example_merges,
        }
    )
    try:
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="entity_adjudication",
            type="entity_adjudication",
            content=content,
            priority="low",
            created_at=_now(),
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            skip_if_duplicate=True,
        )
    except Exception:
        logger.error("Failed to write entity_adjudication run observation", exc_info=True)


# ── reconcile sweep ──────────────────────────────────────────────────────────


def _fuzzy_group(entity_type: str) -> str:
    """The comparison scope for a type, mirroring resolve_entity."""
    if entity_type in _CONCEPT_CLUSTER:
        return "cluster"
    return entity_type  # person / org compare same-type; others never fuzzy-match


def _compute_sweep_pairs(
    slice_entities: list[tuple[str, str, str]],
    group_candidates: dict[str, list[tuple[str, str, str]]],
    seen_pair_keys: set[str],
    cap: int,
) -> list[tuple[str, str]]:
    """Pure-CPU pair discovery (runs off the event loop via ``to_thread``).

    For each entity in the slice, find the fuzzy neighbours (difflib ≥ threshold,
    same comparison group) that are not digit-only differences and not already
    recorded/pending. Returns up to ``cap`` ``(entity_id, similar_entity_id)`` pairs.
    """
    out: list[tuple[str, str]] = []
    for norm, eid, etype in slice_entities:
        group = _fuzzy_group(etype)
        candidates = group_candidates.get(group, ())
        for cand_norm, cand_id, _ct in candidates:
            # Skip only the SAME entity. Do NOT skip on ``cand_norm == norm``: two
            # DIFFERENT entities can share a norm_name across types (UNIQUE is on
            # norm_name+entity_type). Live resolve_entity reuses those cross-type,
            # but historical/legacy duplicates are only recoverable HERE — an
            # exact-norm cross-type pair is a legitimate merge candidate.
            if cand_id == eid:
                continue
            key = f"{min(eid, cand_id)}|{max(eid, cand_id)}"
            if key in seen_pair_keys:
                continue
            if digit_only_difference(norm, cand_norm):
                continue
            if difflib.SequenceMatcher(None, norm, cand_norm).ratio() >= _FUZZY_THRESHOLD:
                seen_pair_keys.add(key)  # dedupe within this run too
                out.append((eid, cand_id))
                if len(out) >= cap:
                    return out
    return out


async def _pending_pair_keys(db: aiosqlite.Connection) -> set[str]:
    """pair_keys of currently-pending adjudication queue rows (both orientations
    collapse via the sorted key)."""
    rows = await dw_crud.query_pending(db, work_type=WORK_TYPE, limit=10000)
    keys: set[str] = set()
    for r in rows:
        try:
            p = json.loads(r.get("payload_json") or "{}")
            a, b = p["entity_id"], p["similar_entity_id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        keys.add(f"{min(a, b)}|{max(a, b)}")
    return keys


_SWEEP_CURSOR_KEY = "entity_adjudication_sweep_cursor"
_SWEEP_RERUN_DAYS = 7


async def maybe_run_sweep(
    db: aiosqlite.Connection,
    *,
    drain_budget: int,
    slice_size: int,
    enqueue_cap: int,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Advance the cursor-managed reconcile sweep by one slice, if appropriate.

    Gated on a low-water mark — skip while the queue already holds ≥ 2×budget
    pending rows, so the sweep never outruns the drain. Cursor lives in
    ``ego_state``; a completed pass idles until it is ``_SWEEP_RERUN_DAYS`` old,
    then restarts from offset 0 (weekly self-heal). Returns the sweep result, or
    None when skipped.
    """
    from genesis.db.crud import ego as ego_crud

    now = now or datetime.now(UTC)
    pending = await dw_crud.count_pending(db, work_type=WORK_TYPE)
    if pending >= 2 * drain_budget:
        return None

    raw = await ego_crud.get_state(db, _SWEEP_CURSOR_KEY)
    state: dict[str, Any] = {}
    if raw:
        try:
            state = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            state = {}
    offset = int(state.get("offset", 0) or 0)
    completed_at = state.get("completed_at")
    if completed_at:
        # Idle after a full pass — re-run only once it is a week stale.
        try:
            done = datetime.fromisoformat(completed_at)
            if (now - done).days < _SWEEP_RERUN_DAYS:
                return None
        except (ValueError, TypeError):
            pass
        offset = 0  # start a fresh pass

    result = await run_reconcile_sweep(
        db, slice_size=slice_size, enqueue_cap=enqueue_cap, cursor_offset=offset
    )
    new_state = {
        "offset": result["next_offset"],
        "completed_at": now.isoformat() if result["completed"] else None,
    }
    await ego_crud.set_state(db, key=_SWEEP_CURSOR_KEY, value=json.dumps(new_state))
    return result


async def run_reconcile_sweep(
    db: aiosqlite.Connection,
    *,
    slice_size: int = 200,
    enqueue_cap: int = 50,
    cursor_offset: int = 0,
) -> dict[str, Any]:
    """Rediscover fuzzy pairs among EXISTING entities (one bounded slice).

    The producer only enqueues newly-created near-duplicates; this recovers the
    historical backlog and heals any install. Slice bounds per-run CPU; the caller
    advances/persists ``cursor_offset``. Difflib runs off-thread.

    Returns ``{"enqueued": int, "next_offset": int, "completed": bool,
    "total": int}``.
    """
    # Snapshot candidate lists on the loop; compute pairs off-thread.
    cluster_types = list(_CONCEPT_CLUSTER)
    cluster = await entities_crud.list_norm_names(db, entity_types=cluster_types)
    persons = await entities_crud.list_norm_names(db, entity_types=["person"])
    orgs = await entities_crud.list_norm_names(db, entity_types=["org"])
    group_candidates = {"cluster": cluster, "person": persons, "org": orgs}

    # Flat, stable ordering (by entity_id) over all fuzzy-eligible entities.
    all_entities = sorted(cluster + persons + orgs, key=lambda t: t[1])
    total = len(all_entities)
    slice_entities = all_entities[cursor_offset : cursor_offset + slice_size]

    # Settled verdicts (merge/distinct/proposed_merge) are skipped; `stale` pairs
    # are deliberately NOT in this set so the sweep re-discovers and re-adjudicates
    # them. Pending queue rows are also skipped (both orientations).
    seen = await adj_crud.settled_pair_keys(db)
    seen |= await _pending_pair_keys(db)

    pairs = await asyncio.to_thread(
        _compute_sweep_pairs, slice_entities, group_candidates, seen, enqueue_cap
    )

    enqueued = 0
    for eid, cand_id in pairs:
        await entities_crud.enqueue_adjudication(db, entity_id=eid, similar_entity_id=cand_id)
        enqueued += 1

    next_offset = cursor_offset + slice_size
    completed = next_offset >= total
    return {
        "enqueued": enqueued,
        "next_offset": 0 if completed else next_offset,
        "completed": completed,
        "total": total,
    }
