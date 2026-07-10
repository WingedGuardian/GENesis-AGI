"""Session-theme accumulator: EMA over prompt embeddings + entity ledger.

Pure functions over the theme state dict (see ``statefiles.empty_state``).
No I/O, no clocks — callers pass timestamps, which keeps every path
testable with synthetic vectors and fixed times.

Design constraints (WS-C, locked):
- EMA folds ONLY genuine user-prompt embeddings. File-context keywords
  feed the entity ledger at reduced weight but never touch the EMA.
- Outlier-skip guard: the recall embedding chain spans backends
  (DeepInfra → DashScope → Ollama, same Qwen3-Embedding family). A
  vector wildly off-family vs the current EMA (cosine < OUTLIER_COS)
  is skipped and counted, not folded — one bad backend response must
  not wrench the theme.
"""

from __future__ import annotations

import math

ALPHA = 0.25  # EMA weight of the newest turn
OUTLIER_COS = 0.05  # below this cosine vs EMA: skip fold, count it
OUTLIER_ESCAPE_RUN = 3  # N consecutive outliers = a real theme change, not glitches
RING_SIZE = 3  # EMA snapshots kept for the stability check

ENTITY_DECAY = 0.9  # per-turn multiplicative decay
PROMPT_KW_WEIGHT = 1.0
FILE_KW_WEIGHT = 0.3
MAX_ENTITIES = 64
MIN_ENTITY_WEIGHT = 0.05  # pruned below this after decay


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 for degenerate inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return list(v)
    return [x / norm for x in v]


def _normalize_keyword(kw: str) -> str:
    """Alias-normalize a keyword (best-effort; identity on any failure)."""
    try:
        from genesis.memory.entity_resolution import normalize_content

        return normalize_content(kw).strip().lower() or kw.lower()
    except Exception:
        return kw.lower()


def fold_turn(
    state: dict,
    vector: list[float],
    prompt_keywords: list[str],
    file_keywords: list[str] | None = None,
    *,
    pivoted: bool = False,
    now_iso: str = "",
) -> dict:
    """Fold one genuine user turn into the theme state (mutates *state*).

    The entity ledger updates on every fold — it is text-based, so the
    cross-backend embedding risk doesn't apply to it. The EMA updates
    only for non-outlier vectors. A corroborated pivot clears the
    stability ring so the trigger must re-observe a settled theme.
    """
    # ── Entity ledger ────────────────────────────────────────────────
    entities: dict[str, float] = {
        k: w * ENTITY_DECAY for k, w in state.get("entities", {}).items()
    }
    for kw in prompt_keywords:
        key = _normalize_keyword(kw)
        entities[key] = entities.get(key, 0.0) + PROMPT_KW_WEIGHT
    for kw in file_keywords or []:
        key = _normalize_keyword(kw)
        entities[key] = entities.get(key, 0.0) + FILE_KW_WEIGHT
    pruned = {k: w for k, w in entities.items() if w >= MIN_ENTITY_WEIGHT}
    if len(pruned) > MAX_ENTITIES:
        keep = sorted(pruned.items(), key=lambda kv: kv[1], reverse=True)
        pruned = dict(keep[:MAX_ENTITIES])
    state["entities"] = pruned

    # ── EMA fold ─────────────────────────────────────────────────────
    if vector:
        unit = _unit(vector)
        ema = state.get("ema")
        is_outlier = ema is not None and cosine(vector, ema) < OUTLIER_COS
        run = state.get("consecutive_outliers", 0)
        # A corroborated pivot (intent-trail Jaccard) or a RUN of
        # "outliers" is a genuine theme change, not a backend glitch —
        # skipping those turns would anchor the EMA to the dead topic
        # until the 24h reset and blind the trigger to the new theme
        # (Codex P2, PR #972). The guard only swallows transient
        # single-vector glitches.
        if is_outlier and not pivoted and run + 1 < OUTLIER_ESCAPE_RUN:
            state["outlier_skips"] = state.get("outlier_skips", 0) + 1
            state["consecutive_outliers"] = run + 1
        else:
            state["consecutive_outliers"] = 0
            if ema is None or len(unit) != len(ema):
                # First fold — or the embedding SPACE changed (backend
                # model swap): the old EMA is meaningless there, reseed.
                state["ema"] = unit
                state["ema_turns"] = 1
                state["ring"] = [unit]
            else:
                if pivoted or is_outlier:
                    # Theme changed — stability must rebuild before the
                    # trigger can fire again.
                    state["ring"] = []
                folded = [
                    (1.0 - ALPHA) * e + ALPHA * u
                    for e, u in zip(ema, unit, strict=True)
                ]
                state["ema"] = _unit(folded)
                state["ema_turns"] = state.get("ema_turns", 0) + 1
                ring = state.get("ring", [])
                ring.append(state["ema"])
                state["ring"] = ring[-RING_SIZE:]

    state["updated_at"] = now_iso
    return state


def top_entities(state: dict, n: int = 8) -> list[str]:
    """Highest-weight ledger entries — the worker's drift_recall query."""
    ranked = sorted(
        state.get("entities", {}).items(), key=lambda kv: kv[1], reverse=True,
    )
    return [k for k, _ in ranked[:n]]
