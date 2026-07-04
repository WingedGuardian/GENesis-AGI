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


# GROUNDWORK(chain-verify): NOT wired — and cannot be naively wired. The ego/
# approval chains form a single GLOBAL chain written by both egos concurrently
# (crud/ego.py), so a TOCTOU fork (two records sharing one previous_hash) is
# expected and is NOT tampering. This linear walk returns (False, idx) at the
# first fork, so a periodic verify would false-alarm. Real tamper detection needs
# a fork-tolerant check (verify each record's chain_hash independently). Kept for
# that future check + as the fork/tamper reference. NOT dead code — do not remove.
def verify_chain(records: list[dict]) -> tuple[bool, int]:
    """Walk records oldest-first and verify chain integrity.

    Parameters
    ----------
    records:
        List of dicts, each with a content hash field, ``previous_hash``,
        and ``chain_hash`` keys. The content hash field can be named
        ``content_hash`` or ``output_hash`` (ego_cycles uses the latter).
        Records with ``chain_hash=None`` (pre-migration) are skipped.

    Returns
    -------
    tuple[bool, int]:
        ``(True, -1)`` if chain is intact.
        ``(False, index)`` at the first broken link.

    Note
    ----
    Pre-migration records (chain_hash=None) are skipped implicitly.
    This means an attacker who replaces a chained record with an unchained
    one mid-chain would not be detected. This is bounded by the INSERT
    path: only newly inserted records get chain hashes, and the risk is
    limited to direct database manipulation.
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

        # Accept both column names: ego_cycles uses output_hash,
        # approval_requests uses content_hash
        c_hash = rec.get("content_hash") or rec.get("output_hash")
        if c_hash is None:
            return False, i

        # Recompute and verify the chain_hash
        recomputed = chained_hash(c_hash, stored_prev)
        if stored_chain != recomputed:
            return False, i

        prev_chain = stored_chain

    return True, -1
