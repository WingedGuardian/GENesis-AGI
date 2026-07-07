"""Pure normalizer: OMI webhook transcript segments -> ``ambient_transcripts`` rows.

No I/O, no genesis imports beyond stdlib — safe to unit-test in isolation and to
reason about independently of the ingest service. The OMI wire format is traced
from the BasedHardware/omi *sender* source (the public docs are wrong on several
counts): the body is an object ``{"segments": [...], "session_id": "<uid>"}`` (a
bare array is tolerated as a fallback), ``session_id`` is the ACCOUNT uid (stable
forever, NOT per-conversation), and segment fields are snake_case (``speaker_id``,
not ``speakerId``). Casing variants are tolerated defensively so a deployed-backend
drift degrades to "still parses", never "drops real speech".

The mapping deliberately emits NO ``meta.audio`` block: OMI is text-only, so the
reader (``sources.row_to_utterance``) sets ``has_audio=False`` and the engine takes
the text-only clarity path — no fabricated ``rms=0`` loudness penalty.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

_PROVENANCE = "omi_webhook"
_SOURCE = "omi"


@dataclass(frozen=True)
class NormalizedRow:
    """One ``ambient_transcripts`` row derived from an OMI segment.

    ``segment_id`` is NOT a table column — it is the OMI segment uuid, carried
    alongside for delivery-idempotent dedup (``seen_segments``). The remaining
    fields map 1:1 onto ``ambient_transcripts`` (``id`` is autoincrement).
    """

    segment_id: str | None
    ts: str
    text: str
    duration_s: float
    speaker_label: str | None
    provenance: str
    source: str
    meta: str
    is_user: int | None
    speaker_name: str | None


def _first(seg: dict, *keys, default=None):
    """Return the first present key's value — casing/naming tolerance."""
    for k in keys:
        if k in seg:
            return seg[k]
    return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_payload(data) -> tuple[str | None, list[dict]]:
    """Extract ``(session_id, segments)`` from an already-decoded JSON body.

    Tolerates the real object shape and a bare-array fallback. ``session_id`` is
    informational only (auth decides on the query ``uid``); a mismatch is logged
    by the caller. Non-dict segment entries are dropped.
    """
    if isinstance(data, dict):
        session_id = data.get("session_id")
        raw_segs = data.get("segments") or []
    elif isinstance(data, list):
        session_id = None
        raw_segs = data
    else:
        return None, []
    segments = [s for s in raw_segs if isinstance(s, dict)]
    return session_id, segments


def normalize_segments(segments, *, uid: str, epoch0: float) -> list[NormalizedRow]:
    """Map OMI segments -> ``NormalizedRow`` list using ``epoch0`` for timestamps.

    ``epoch0`` is the per-uid anchor (see ``decide_anchor``): the wall-clock epoch
    that OMI's conversation-relative ``start=0`` corresponds to. Empty/whitespace
    segments are skipped; duration is clamped non-negative.
    """
    rows: list[NormalizedRow] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _as_float(seg.get("start"))
        end = _as_float(seg.get("end"))
        is_user_raw = _first(seg, "is_user", "isUser")
        is_user = None if is_user_raw is None else int(bool(is_user_raw))
        meta = {
            "omi": {
                "uid": uid,
                "segment_id": seg.get("id"),
                "speaker_id": _first(seg, "speaker_id", "speakerId"),
                "person_id": _first(seg, "person_id", "personId"),
                "stt_provider": _first(seg, "stt_provider", "sttProvider"),
            },
            "asr_feats": {"n_tokens": len(text.split())},
        }
        rows.append(
            NormalizedRow(
                segment_id=seg.get("id"),
                ts=datetime.fromtimestamp(epoch0 + start, UTC).isoformat(),
                text=text,
                duration_s=max(0.0, end - start),
                speaker_label=seg.get("speaker"),
                provenance=_PROVENANCE,
                source=_SOURCE,
                meta=json.dumps(meta),
                is_user=is_user,
                speaker_name=None,
            )
        )
    return rows


def decide_anchor(
    current_epoch0: float | None,
    batch_max_end: float,
    recv_ts: float,
    tolerance: float,
) -> float:
    """Return the ``epoch0`` to use for a batch (pure; the state layer persists it).

    Keep the existing anchor while this batch's predicted wall-clock
    (``current_epoch0 + batch_max_end``) lands within ``tolerance`` of the actual
    receive time. Otherwise — no anchor yet, or the prediction is off by more than
    ``tolerance`` (a conversation rollover, a downtime gap, or device thrash) —
    re-anchor so this batch's last utterance lands at ``recv_ts``. Self-correcting:
    any re-anchor puts timestamps back at the wall-clock of speech.
    """
    if current_epoch0 is None:
        return recv_ts - batch_max_end
    predicted = current_epoch0 + batch_max_end
    if abs(predicted - recv_ts) > tolerance:
        return recv_ts - batch_max_end
    return current_epoch0
