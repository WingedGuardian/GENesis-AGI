"""Short labels for graph neighbours (memory spec MW-11, labelled links).

A related memory used to reach a session as a bare id, so the only way to judge
a link was to follow it. A label is a few words of the neighbour's OWN text.
``memory_expand`` / ``memory_recall`` and the proactive hook share this module,
so the exclusions have one definition.

Two steps, so a caller holding a pooled read connection can release it before
the vector-store round trip: :func:`label_plan` does the SQLite reads that
decide which ids MAY be labelled, :func:`labels_from_plan` reads their text.
:func:`neighbour_labels` runs both.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field

from genesis.memory.provenance import is_garbage
from genesis.security.sanitizer import strip_boundary_markers

logger = logging.getLogger(__name__)

# Brackets would end the hook's one-line ``[tag]``; markdown markers are noise in
# a few-word label.
_LABEL_DROP = str.maketrans("", "", "[](){}<>#*`")
_LABEL_CHUNK = 500  # bound parameters per IN (...), under SQLite's 999


def label_text(content: str | None, *, max_words: int, max_chars: int = 80) -> str | None:
    """A one-line label from *content*, or ``None`` when there is nothing to show.

    Strips injection-defense markers and an extraction prefix (``[discovery] ``,
    as the hook's own line does), drops brackets (the hook prints labels inside a
    one-line ``[tag]``), markdown markers and control or format characters
    (bidi overrides, zero-width, escapes), collapses whitespace, and keeps at
    most *max_words* words and *max_chars* characters, cut on a word with ``…``
    when anything was cut.
    """
    if is_garbage(content):
        return None
    text = strip_boundary_markers(content or "")
    text = "".join(
        " " if unicodedata.category(c) == "Cc" else c
        for c in text
        if unicodedata.category(c) != "Cf"
    ).lstrip()
    if text.startswith("[") and "] " in text[:30]:
        text = text[text.index("] ") + 2 :]
    # A token with no letter or digit is a list bullet or a rule, not a word.
    words = [w for w in text.translate(_LABEL_DROP).split() if any(c.isalnum() for c in w)]
    if not words:
        return None
    kept = words[:max_words]
    cut = len(kept) < len(words)
    while len(" ".join(kept)) > max_chars and len(kept) > 1:
        kept.pop()
        cut = True
    label = " ".join(kept)[:max_chars]
    return label + "…" if cut else label


def _reads_as_credential(payload: dict, domain: str) -> bool:
    """A point that names itself a credential entry, whatever SQLite says.

    Second line behind the ``knowledge_units`` lookup: a credential point whose
    row is gone (a failed cleanup) still opens with the ``[reference.credentials]``
    header and carries the credentials tag. The header test compares letters and
    digits only, after NFKC folding, so no bracket, spacing, bullet, marker or
    control-character spelling hides it; it can over-match a memory that opens by
    naming that domain, which only costs that memory its label.
    """
    opening = strip_boundary_markers(str(payload.get("content") or ""))[:200]
    folded = "".join(c for c in unicodedata.normalize("NFKC", opening).casefold() if c.isalnum())
    if "".join(c for c in domain if c.isalnum()) in folded[:60]:
        return True
    tags = payload.get("tags")
    if tags is None:
        return False
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, RecursionError):
            tags = [tags]
        if isinstance(tags, str):
            tags = [tags]
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return True  # a shape nobody writes: do not guess, show no label
    return any(str(t).strip().lower() in (domain, "credentials") for t in tags)


async def _ids_where(db, sql: str, ids: list[str], *params) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(ids), _LABEL_CHUNK):
        chunk = ids[start : start + _LABEL_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = await db.execute_fetchall(sql.format(marks=marks), (*params, *chunk))
        found.update(r[0] for r in rows)
    return found


@dataclass(frozen=True)
class LabelPlan:
    """The ids that may be labelled, and their stored origins (SQLite only)."""

    wanted: tuple[str, ...] = ()
    stored_origin: dict[str, str | None] = field(default_factory=dict)


async def label_plan(db, ids, *, include_hidden: bool) -> LabelPlan | None:
    """Decide from SQLite which of *ids* may be labelled.

    Drops credential entries (the reference store's canonical row test: older
    entries still hold the value in their stored text) and, unless
    *include_hidden*, hidden memories. Returns ``None`` (withhold EVERY label)
    when the credential or stored-origin lookup fails, or when visibility cannot
    be read and hidden memories were not asked for: those are the reads that
    keep text out, so a failure must not let it through.
    """
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return LabelPlan()
    from genesis.db.crud import memory as memory_crud
    from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN
    from genesis.memory.graphstore import invalid_memory_ids_among

    try:
        credentials = await _ids_where(
            db,
            # `params` bind before the ids, so the domain test comes first.
            "SELECT qdrant_id FROM knowledge_units WHERE lower(trim(domain)) = ? "
            "AND lower(trim(project_type)) = 'reference' AND qdrant_id IN ({marks})",
            ids,
            CREDENTIAL_DOMAIN,
        )
        stored_origin = await memory_crud.origin_class_by_ids(db, ids)
    except Exception:
        logger.warning("neighbour labels withheld: credential/origin lookup failed", exc_info=True)
        return None
    try:
        hidden = await invalid_memory_ids_among(db, set(ids))
    except Exception:
        if not include_hidden:
            logger.warning("neighbour labels withheld: visibility lookup failed", exc_info=True)
            return None
        hidden = set()
    wanted = tuple(i for i in ids if i not in credentials and (include_hidden or i not in hidden))
    return LabelPlan(wanted=wanted, stored_origin=stored_origin)


async def labels_from_plan(
    db, qdrant, plan: LabelPlan | None, *, max_words: int, max_chars: int = 80
) -> dict[str, str] | None:
    """Read the planned ids' text and label the ones that may be shown.

    ``db`` only owns the worker thread (``run_resource_thread``); no SQL runs
    here. No label for a point that names itself a credential (header or tag),
    external or unknown-origin text (the injection gate's own predicate, stored
    origin first, and no origin at all counts as unknown), garbage, or a missing
    point. A failed content read returns ``{}``: the neighbours stay unlabelled.

    One batched ``retrieve``, off the event loop. Only ``episodic_memory`` is
    read: a knowledge-base neighbour is external and gets no label either way.
    """
    if plan is None:
        return None
    if not plan.wanted:
        return {}
    from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN
    from genesis.db.request_database import run_resource_thread
    from genesis.security.immunity_shadow import item_is_blockable

    try:
        points = await run_resource_thread(
            db,
            qdrant.retrieve,
            collection_name="episodic_memory",
            ids=list(plan.wanted),
            with_payload=["content", "origin_class", "source_pipeline", "tags"],
        )
    except Exception:
        logger.warning("neighbour labels skipped: content read failed", exc_info=True)
        return {}
    allowed = set(plan.wanted)
    labels: dict[str, str] = {}
    for point in points or ():
        mid = str(point.id)
        if mid not in allowed:
            continue
        payload = point.payload or {}
        if _reads_as_credential(payload, CREDENTIAL_DOMAIN):
            continue
        # Stored origin first; a NULL there falls back to the payload's, and a
        # memory whose origin is unknown in both gets no label.
        stored = plan.stored_origin.get(mid)
        origin = stored if stored is not None else payload.get("origin_class")
        if not isinstance(origin, str) or item_is_blockable(
            collection="episodic_memory",
            source_pipeline=payload.get("source_pipeline"),
            origin_class=origin,
        ):
            continue
        label = label_text(payload.get("content"), max_words=max_words, max_chars=max_chars)
        if label:
            labels[mid] = label
    return labels


async def neighbour_labels(
    db, qdrant, ids, *, include_hidden: bool, max_words: int, max_chars: int = 80
) -> dict[str, str] | None:
    """``memory_id -> label`` for the neighbours that may be shown, else omitted.

    :func:`label_plan` then :func:`labels_from_plan` on one connection; ``None``
    means every label is withheld (see :func:`label_plan`).
    """
    plan = await label_plan(db, ids, include_hidden=include_hidden)
    return await labels_from_plan(db, qdrant, plan, max_words=max_words, max_chars=max_chars)
