"""WS-1 A4 — LongMemEval local benchmark harness.

Loads each LongMemEval question's haystack into a FRESH ephemeral Genesis
memory store (in-memory Qdrant + temp SQLite — zero production contact by
construction), runs Genesis recall + an LLM answer, and grades with the
standard gpt-4o judge (ported verbatim from upstream ``evaluate_qa.py``) to
produce an externally comparable per-question-type accuracy.
"""

from __future__ import annotations
