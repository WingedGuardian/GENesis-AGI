"""MW-1 — Tier-0 extraction judgment axes (provenance / speech_act / durability).

Three SEPARATE axes captured at extraction time to cure the "defaulting disease"
— every memory silently landing as ``memory_class=fact`` with no trust /
durability / protection signal. Persisted WRITE-ONLY on ``memory_metadata``;
NOTHING reads them yet. Consumers are downstream, already-filed workstreams:

  - ``assertion_provenance`` → recall ranking WEIGHT
    (# GROUNDWORK(mw-4-provenance-weight))
  - ``durability`` + ``expires_at`` → temporary-context TTL lifecycle
    (# GROUNDWORK(mw-4-durability-ttl))
  - ``speech_act`` (+ ``speech_act_confidence``) → existence-PROTECTION eligibility
    (# GROUNDWORK(mw-5-speech-act-protection))

Do NOT collapse the three axes into one enum — that conflation IS the defaulting
disease. Provenance (WHO asserted it) is not durability (is it a PERMANENT truth)
is not speech-act (what KIND of utterance). In particular ``assertion_provenance``
is distinct from ``memory_metadata.origin_class``: origin_class is a pipeline-trust
label that resolves to ``first_party`` for ALL transcript extraction and so has
zero power to tell "the user said it" from "Genesis inferred it".
"""

from __future__ import annotations

# ── speech_act — what KIND of utterance (drives PROTECTION eligibility) ──
SPEECH_ACTS: frozenset[str] = frozenset(
    {"rule", "decision", "correction", "observation", "claim", "preference", "question"}
)
DEFAULT_SPEECH_ACT = "observation"  # neutral, unprotected — parser fallback

# Only STRUCTURALLY-identifiable kinds earn existence-protection: a classifier can
# hit these with confidence, where "fact"/"observation" cannot. ``preference`` is
# deliberately EXCLUDED — preferences are exactly what legitimately gets superseded
# ("used to prefer X, now Y"), and the permanent-identity layer (reference_store +
# entity cards + pinning) already owns standing preferences. Consumed by MW-5's
# dream_shield protection gate. # GROUNDWORK(mw-5-speech-act-protection)
PROTECTED_SPEECH_ACTS: frozenset[str] = frozenset({"rule", "decision", "correction"})

# How sure the extractor must be that something IS a protected kind before it earns
# existence-protection. Consumed by MW-5. # GROUNDWORK(mw-5-speech-act-protection)
PROTECTION_CONFIDENCE_FLOOR = 0.7

# ── assertion_provenance — WHO asserted it (drives ranking WEIGHT) ──
ASSERTION_PROVENANCES: frozenset[str] = frozenset({"user", "external", "self_inference"})

# ── durability — a permanent truth vs transient context (drives lifecycle) ──
DURABILITIES: frozenset[str] = frozenset({"permanent", "temporary"})
DEFAULT_DURABILITY = "permanent"

# Expiry is strictly OPT-IN: a memory drops out of recall IFF
# ``durability == "temporary"`` AND a valid ``expires_at`` has elapsed.
# NULL / "permanent" / anything-else NEVER expires — a wrong "temporary" must not
# silently delete memories. The "temporary-without-an-explicit-date → default TTL"
# policy belongs to the CONSUMER (MW-4), not this write path.
# # GROUNDWORK(mw-4-durability-ttl)


def normalize_speech_act(value: object) -> str:
    """Coerce an LLM ``speech_act`` to a known value; unknown/missing → observation."""
    if isinstance(value, str) and value in SPEECH_ACTS:
        return value
    return DEFAULT_SPEECH_ACT


def normalize_durability(value: object) -> str:
    """Coerce an LLM ``durability`` to a known value; unknown/missing → permanent (safe)."""
    if isinstance(value, str) and value in DURABILITIES:
        return value
    return DEFAULT_DURABILITY


def normalize_provenance(value: object) -> str | None:
    """Coerce an LLM ``assertion_provenance``; unknown/missing → None (unclassified)."""
    if isinstance(value, str) and value in ASSERTION_PROVENANCES:
        return value
    return None


def clamp_unit(value: object, default: float = 0.5) -> float:
    """Coerce a confidence to [0.0, 1.0]; non-numeric → ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))
