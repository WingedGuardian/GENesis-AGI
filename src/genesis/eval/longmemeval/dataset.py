"""LongMemEval dataset loader.

Parses the ``longmemeval_oracle.json`` release into typed instances. Schema
(verified against the real 500-instance oracle file):

    question_id           str   ("_abs" suffix => abstention question)
    question_type         str   (6 values; see QUESTION_TYPES)
    question              str
    answer                str   (for preference qs this is a rubric; for
                                 abstention qs an explanation — see judge.py)
    question_date         str   ("YYYY/MM/DD (Day) HH:MM")
    haystack_dates        list[str]        (one per session)
    haystack_session_ids  list[str]
    haystack_sessions     list[list[turn]] (turn: {role, content, has_answer})
    answer_session_ids    list[str]
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: The six LongMemEval question types.
QUESTION_TYPES = frozenset(
    {
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "temporal-reasoning",
        "knowledge-update",
        "multi-session",
    },
)


@dataclass(frozen=True)
class Turn:
    """A single chat turn within a haystack session."""

    role: str
    content: str
    has_answer: bool = False


@dataclass(frozen=True)
class LongMemEvalInstance:
    """One LongMemEval question with its full haystack."""

    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    haystack_dates: list[str]
    haystack_session_ids: list[str]
    haystack_sessions: list[list[Turn]]
    answer_session_ids: list[str]

    @property
    def is_abstention(self) -> bool:
        """Abstention questions are marked by an ``_abs`` id suffix."""
        return self.question_id.endswith("_abs")

    def evidence_turns(self) -> list[Turn]:
        """All turns tagged ``has_answer`` (the gold evidence spans)."""
        return [t for sess in self.haystack_sessions for t in sess if t.has_answer]

    def iter_turns(self) -> Iterator[tuple[int, Turn, str | None]]:
        """Yield ``(session_index, turn, session_date)`` for every turn.

        The session's date (from ``haystack_dates`` positionally) rides along
        so ingest can stamp each memory's ``valid_at`` without re-joining.
        """
        for si, session in enumerate(self.haystack_sessions):
            date = self.haystack_dates[si] if si < len(self.haystack_dates) else None
            for turn in session:
                yield si, turn, date


def _parse_turn(raw: dict) -> Turn:
    return Turn(
        role=str(raw.get("role", "")),
        content=str(raw.get("content", "")),
        has_answer=bool(raw.get("has_answer", False)),
    )


def _parse_instance(raw: dict) -> LongMemEvalInstance:
    sessions = [[_parse_turn(t) for t in session] for session in raw.get("haystack_sessions", [])]
    return LongMemEvalInstance(
        question_id=str(raw["question_id"]),
        question_type=str(raw["question_type"]),
        question=str(raw["question"]),
        answer=str(raw["answer"]),
        question_date=str(raw.get("question_date", "")),
        haystack_dates=list(raw.get("haystack_dates", [])),
        haystack_session_ids=list(raw.get("haystack_session_ids", [])),
        haystack_sessions=sessions,
        answer_session_ids=list(raw.get("answer_session_ids", [])),
    )


def load_oracle(path: str | Path) -> list[LongMemEvalInstance]:
    """Load and parse a LongMemEval oracle JSON file into typed instances."""
    data = json.loads(Path(path).read_text())
    return [_parse_instance(raw) for raw in data]


def filter_by_types(
    instances: list[LongMemEvalInstance],
    types_csv: str,
) -> list[LongMemEvalInstance]:
    """Keep only instances whose question_type is in the comma-separated list.

    Validates every requested type against ``QUESTION_TYPES`` so a typo fails
    loudly instead of silently returning an empty slice.
    """
    wanted = {t.strip() for t in types_csv.split(",") if t.strip()}
    if not wanted:
        msg = "no question types given (empty --types would silently select nothing)"
        raise ValueError(msg)
    unknown = wanted - QUESTION_TYPES
    if unknown:
        msg = f"unknown question type(s): {sorted(unknown)}; valid: {sorted(QUESTION_TYPES)}"
        raise ValueError(msg)
    return [i for i in instances if i.question_type in wanted]
