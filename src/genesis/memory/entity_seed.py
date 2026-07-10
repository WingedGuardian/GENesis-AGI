"""Curated entity-layer seed — high-recall entities + the repo-split spine.

Idempotent: entities land via INSERT OR IGNORE on (norm_name, type);
relations/mentions upsert keeping the stronger claim. Run via
``scripts/apply_entity_seed.py`` (also safe to re-run after edits).

The seed is deliberately SMALL (~20): identity anchors the acceptance
test and the FTS backfill need. Everything else accrues organically via
extraction (forward-only, per the approved hybrid backfill).
"""

from __future__ import annotations

import aiosqlite

from genesis.db.crud import entities as entities_crud
from genesis.memory.entity_registry import norm

SEED_SOURCE = "seed"
SEED_CONFIDENCE = 0.95

# (name, entity_type, summary)
SEED_ENTITIES: list[tuple[str, str, str]] = [
    ("OMI", "device", "Wearable always-on AI mic (omi.me); evaluated, not adopted"),
    ("HAOS Voice PE", "device", "Home Assistant Voice Preview Edition — the invested edge device"),
    ("voice-edge-device", "concept", "Category: voice/edge hardware endpoints (wearables, smart speakers)"),
    ("GENesis-Voice", "repo", "Public repo for voice/edge-device software (firmware, esphome, bridges)"),
    ("GENesis-AGI", "repo", "Public primary repo — the Genesis cognitive core"),
    ("genesis-backups", "repo", "Private encrypted backups repo"),
    ("genesis-voice-repo-split", "concept", "Decision: voice/edge work lives in GENesis-Voice, not GENesis-AGI"),
    ("Genesis", "product", "The autonomous AI agent system itself"),
    ("Claude Code", "product", "Anthropic CLI agent — Genesis's cognitive substrate"),
    ("Qdrant", "product", "Vector store backing semantic memory"),
    ("SQLite", "product", "Primary structured store (genesis.db)"),
    ("Ollama", "product", "Optional local model server"),
    ("Telegram", "product", "Primary outreach/approval channel"),
    ("Discord", "product", "Campaign/community channel"),
    ("Home Assistant", "product", "Smart-home platform hosting the voice pipeline"),
    ("ESPHome", "product", "Firmware toolchain for the Voice PE device"),
    ("Tailscale", "product", "Overlay network between Genesis nodes"),
    ("memory system", "subsystem", "Genesis memory: SQLite+Qdrant, 4-layer recall"),
    ("session awareness", "subsystem", "Ambient session-theme layer (WS-C): EMA, drift trigger, arbiter"),
    ("guardian", "subsystem", "Host-VM watchdog deployed via update.sh"),
    ("dashboard", "subsystem", "Flask web UI on the host proxy"),
]

# (source_name, link_type, target_name, valid_at)
SEED_RELATIONS: list[tuple[str, str, str, str | None]] = [
    ("OMI", "is_a", "voice-edge-device", None),
    ("HAOS Voice PE", "is_a", "voice-edge-device", None),
    # The OMI-incident spine: category → the repo-split decision.
    ("voice-edge-device", "constrained_by", "genesis-voice-repo-split", "2026-06-14"),
    ("genesis-voice-repo-split", "governs", "GENesis-Voice", "2026-06-14"),
    ("HAOS Voice PE", "depends_on", "ESPHome", None),
    ("HAOS Voice PE", "depends_on", "Home Assistant", None),
    ("session awareness", "part_of", "memory system", None),
    ("memory system", "part_of", "Genesis", None),
    ("Genesis", "depends_on", "Claude Code", None),
    ("memory system", "depends_on", "Qdrant", None),
    ("memory system", "depends_on", "SQLite", None),
]

# (entity_name, memory_id) — hand-anchored mentions; the FTS backfill
# (E3) adds the organic ones.
SEED_MENTIONS: list[tuple[str, str]] = [
    # The GENesis-Voice repo-split decision memory (OMI-incident target).
    ("genesis-voice-repo-split", "9d36f039-3126-4721-8c71-027df1a94e2a"),
]


async def apply_seed(db: aiosqlite.Connection) -> dict:
    """Apply the full seed. Returns counts. Idempotent."""
    aliases = None  # load once inside norm() per call is fine at this scale
    ids: dict[str, str] = {}
    for name, entity_type, summary in SEED_ENTITIES:
        entity_id = await entities_crud.create_entity(
            db,
            name=name,
            norm_name=norm(name, aliases),
            entity_type=entity_type,
            summary=summary,
            source=SEED_SOURCE,
            _commit=False,
        )
        ids[name] = entity_id
    for source_name, link_type, target_name, valid_at in SEED_RELATIONS:
        await entities_crud.upsert_link(
            db,
            source_id=ids[source_name],
            target_id=ids[target_name],
            link_type=link_type,
            provenance="EXTRACTED",
            confidence=SEED_CONFIDENCE,
            valid_at=valid_at,
            _commit=False,
        )
    for entity_name, memory_id in SEED_MENTIONS:
        await entities_crud.upsert_mention(
            db,
            memory_id=memory_id,
            entity_id=ids[entity_name],
            provenance="EXTRACTED",
            confidence=SEED_CONFIDENCE,
            source=SEED_SOURCE,
            _commit=False,
        )
    await db.commit()
    return {
        "entities": len(SEED_ENTITIES),
        "relations": len(SEED_RELATIONS),
        "mentions": len(SEED_MENTIONS),
    }
