"""Load a LongMemEval haystack into an ephemeral Genesis store.

Turn-level ingest (validated by the spike): each non-empty turn becomes one
episodic memory tagged ``origin_class=first_party`` — the haystack is the
user's OWN prior conversation history, so ``first_party`` is both the honest
tag and the one the WS-3 provenance/immunity gates never block (an
``external_untrusted`` tag would risk distorting recall). ``valid_at`` carries
the session's date so temporal-reasoning questions can order memories in time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from genesis.memory.provenance import ORIGIN_FIRST_PARTY

if TYPE_CHECKING:
    from genesis.eval.longmemeval.dataset import LongMemEvalInstance
    from genesis.memory.store import MemoryStore

_SOURCE = "longmemeval_haystack"
# "2023/05/01 (Mon) 10:00" -> capture date + time, drop the "(Day)" token.
_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\D+(\d{1,2}):(\d{2})")


@dataclass
class IngestResult:
    stored_count: int
    evidence_memory_ids: set[str] = field(default_factory=set)


def normalize_date(raw: str | None) -> str | None:
    """Parse the LongMemEval date format to an ISO-8601 string; ``None`` on miss."""
    if not raw:
        return None
    m = _DATE_RE.search(raw)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi).isoformat()
    except ValueError:
        return None


async def ingest_haystack(
    store: MemoryStore,
    instance: LongMemEvalInstance,
    *,
    origin_class: str = ORIGIN_FIRST_PARTY,
    source: str = _SOURCE,
    auto_link: bool = False,
) -> IngestResult:
    """Store every non-empty haystack turn as an episodic first-party memory.

    ``auto_link=True`` (graph arm only; needs a store built ``with_linker``)
    creates similarity links at insert time exactly as prod does: each turn
    links against the ALREADY-ingested earlier turns, so link topology is
    prod-faithful (later→earlier edges; a post-hoc pass would produce edges to
    later memories that prod can never build). Baseline arms keep ``False`` —
    links contaminate ranking for every arm sharing a store (graph boost AND
    activation connectivity), so the graph arm gets its own store.
    """
    stored = 0
    evidence: set[str] = set()
    for _session_idx, turn, date in instance.iter_turns():
        if not turn.content.strip():
            continue
        # Prepend the session date to the content so the reader can reason over
        # time (temporal-reasoning / knowledge-update questions need to compute
        # intervals and pick the latest fact); recall carries content verbatim.
        date_prefix = f"[{date}] " if date else ""
        memory_id = await store.store(
            content=f"{date_prefix}[{turn.role}] {turn.content}",
            source=source,
            memory_type="episodic",
            origin_class=origin_class,
            valid_at=normalize_date(date),
            auto_link=auto_link,
        )
        stored += 1
        if turn.has_answer:
            evidence.add(memory_id)
    return IngestResult(stored_count=stored, evidence_memory_ids=evidence)
