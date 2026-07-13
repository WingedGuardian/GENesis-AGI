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
) -> IngestResult:
    """Store every non-empty haystack turn as an episodic first-party memory."""
    stored = 0
    evidence: set[str] = set()
    for _session_idx, turn, date in instance.iter_turns():
        if not turn.content.strip():
            continue
        memory_id = await store.store(
            content=f"[{turn.role}] {turn.content}",
            source=source,
            memory_type="episodic",
            origin_class=origin_class,
            valid_at=normalize_date(date),
            # No cross-memory linking: the linker is unwired for the ephemeral
            # store, and the benchmark measures recall of stored evidence, not
            # associative link quality. auto_link would be a no-op anyway.
            auto_link=False,
        )
        stored += 1
        if turn.has_answer:
            evidence.add(memory_id)
    return IngestResult(stored_count=stored, evidence_memory_ids=evidence)
