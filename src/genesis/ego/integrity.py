"""Content integrity utilities for ego pipeline audit tracking.

Provides hash and size computation for tracking content mutations
through the ego cycle → realist gate → proposal pipeline, plus
hash-chain functions for tamper-evident audit trails (Verified
Autonomy Layer 8).

Reference: doi.org/10.5281/zenodo.19096229, Section 11
"""

from __future__ import annotations

import hashlib
import json


def content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode()).hexdigest()


def content_size(text: str) -> int:
    """Byte count of UTF-8 encoded text."""
    return len(text.encode())


# -- Hash chain functions (Verified Autonomy L8) --


def canonical_json(fields: dict) -> str:
    """Deterministic JSON serialisation for reproducible hashes.

    Sorted keys, no whitespace, ASCII-only. Two dicts with the same
    key-value pairs always produce the same string regardless of
    insertion order.
    """
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def chained_hash(content_hash_val: str, previous_hash: str | None) -> str:
    """SHA-256 of content hash chained to previous record's chain hash.

    The chain structure: each record's digest includes the previous
    record's chain_hash. Modifying any record cascades hash invalidation
    through all subsequent records.

    The genesis sentinel value ``"genesis"`` is used for the first record
    in the chain (no predecessor).
    """
    payload = f"{previous_hash or 'genesis'}:{content_hash_val}"
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_chain(records: list[dict]) -> tuple[bool, int]:
    """Walk records oldest-first and verify chain integrity.

    Parameters
    ----------
    records:
        List of dicts, each with ``content_hash``, ``previous_hash``,
        and ``chain_hash`` keys. Records with ``chain_hash=None``
        (pre-migration) are skipped gracefully.

    Returns
    -------
    tuple[bool, int]:
        ``(True, -1)`` if chain is intact.
        ``(False, index)`` at the first broken link.
    """
    prev_chain: str | None = None
    for i, rec in enumerate(records):
        stored_chain = rec.get("chain_hash")
        if stored_chain is None:
            # Pre-migration record — skip, don't break the chain
            continue

        stored_prev = rec.get("previous_hash")

        # Verify the previous_hash link
        if (stored_prev or None) != (prev_chain or None):
            return False, i

        # Recompute and verify the chain_hash
        recomputed = chained_hash(rec["content_hash"], stored_prev)
        if stored_chain != recomputed:
            return False, i

        prev_chain = stored_chain

    return True, -1
