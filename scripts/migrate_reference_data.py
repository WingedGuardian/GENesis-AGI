#!/usr/bin/env python3
"""One-shot idempotent importer for static reference data.

Reads well-known config sources and promotes them into the reference store
as ``project_type='reference'`` ``knowledge_units`` rows. Unlike the
history-mining script, this runs NO LLM calls — it parses deterministic
files and calls the shared ``ingest_knowledge_unit`` helper directly, using
the same body format the ``reference_store`` MCP tool uses so entries land
indistinguishable from manually-captured ones.

Sources:
  1. ``CLAUDE.md`` ``## Network Identity`` section → network entries
     (container IP, host VM IP, dashboard URL).
  2. ``~/.genesis/guardian_remote.yaml`` → network entry (SSH host/user/key).
  3. ``config/model_routing.yaml`` → network entries for Ollama/LM Studio
     endpoints that appear as ``base_url`` values.
  4. ``docs/reference/*.md`` → one ``fact`` entry per file with a pointer to
     the source path and the H1 / first paragraph as the description.
  5. ``~/.claude/personas/*/`` → one ``persona_pointer`` entry per persona
     directory.
  6. ``~/genesis/secrets.env`` → one ``account`` entry listing the env var
     NAMES (values NEVER touched) for discoverability of "what credentials
     Genesis has configured".

Idempotent: relies on the UNIQUE(project_type, domain, concept) constraint
on ``knowledge_units`` plus ``knowledge.upsert`` semantics. Re-running
updates existing entries in place; the first ingestion's stable unit_id is
preserved.

Usage:
    source ~/genesis/.venv/bin/activate
    python scripts/migrate_reference_data.py [--dry-run]

Dry-run prints each would-be entry with source breakdown and exits without
touching the database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Ensure genesis package is importable when run from repo root
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from dotenv import load_dotenv  # noqa: E402

from genesis.env import secrets_path  # noqa: E402

_secrets = secrets_path()
if _secrets.exists():
    load_dotenv(str(_secrets), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("migrate-reference-data")

# ─── Entry model ─────────────────────────────────────────────────────────────


@dataclass
class ReferenceEntry:
    """A would-be reference entry, ready to be ingested."""

    kind: str  # credentials|url|network|persona_pointer|account|fact
    identifier: str
    value: str
    description: str
    tags: list[str] = field(default_factory=list)
    source_file: str = ""  # for logging / provenance


# ─── Body formatter (matches reference_store MCP tool exactly) ───────────────


def _format_reference_body(entry: ReferenceEntry) -> str:
    """Replicate the ``reference_store`` MCP tool's body layout.

    The leading ``[reference.{kind}] {identifier}`` header salts the content
    so two entries with different (kind, identifier) tuples never collapse
    to byte-identical content in ``MemoryStore.store()``'s dedup pass.
    """
    lines = [
        f"[reference.{entry.kind}] {entry.identifier}",
        "",
        entry.description.strip(),
        "",
        f"Value: {entry.value}",
    ]
    if entry.tags:
        lines.append(f"Tags: {', '.join(entry.tags)}")
    lines.append(
        f"Captured: via=migrate_reference_data source={entry.source_file}",
    )
    return "\n".join(lines)


# ─── Source parsers ──────────────────────────────────────────────────────────


def _parse_network_identity(claude_md: Path) -> list[ReferenceEntry]:
    """Extract the ``## Network Identity`` section from CLAUDE.md."""
    if not claude_md.exists():
        logger.warning("CLAUDE.md not found at %s — skipping", claude_md)
        return []
    text = claude_md.read_text()
    match = re.search(
        r"^##\s+Network Identity\s*\n(.*?)(?=\n##\s|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        logger.warning("No ## Network Identity section in CLAUDE.md")
        return []
    section = match.group(1)

    entries: list[ReferenceEntry] = []
    # Match bullet lines like "- **Container IP**: 10.176.34.206 (..."
    for bullet in re.finditer(
        r"^-\s+\*\*([^*]+)\*\*:\s*(.+?)(?=\n-\s|\Z)",
        section,
        re.DOTALL | re.MULTILINE,
    ):
        label = bullet.group(1).strip()
        body = bullet.group(2).strip()
        # First token before whitespace/parenthesis is the primary value
        value_match = re.match(r"([^\s()]+)", body)
        if not value_match:
            continue
        value = value_match.group(1)
        entries.append(ReferenceEntry(
            kind="network",
            identifier=f"Genesis {label}",
            value=value,
            description=(
                f"{label} for the Genesis deployment, declared in CLAUDE.md "
                f"Network Identity section. Full line: {body}"
            ),
            tags=["genesis", "infra", "claude-md"],
            source_file="CLAUDE.md",
        ))
    return entries


def _parse_guardian_remote(path: Path) -> list[ReferenceEntry]:
    """Extract host/user/key from guardian_remote.yaml."""
    if not path.exists():
        logger.warning("guardian_remote.yaml not found at %s — skipping", path)
        return []
    # Minimal YAML parse — file is a flat key:value map by convention.
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        logger.exception("Failed to parse %s", path)
        return []
    host_ip = data.get("host_ip")
    host_user = data.get("host_user")
    ssh_key = data.get("ssh_key")
    if not (host_ip and host_user):
        logger.warning("guardian_remote.yaml missing host_ip/host_user")
        return []
    value = f"{host_user}@{host_ip}"
    if ssh_key:
        value += f" (key: {ssh_key})"
    return [ReferenceEntry(
        kind="network",
        identifier="Guardian host VM SSH",
        value=value,
        description=(
            "SSH target for Guardian running on the host VM. Used by the "
            "guardian-gateway.sh command dispatcher. Configured by "
            "install_guardian.sh during bootstrap. SSH is Guardian-only; "
            "interactive human SSH is not supported."
        ),
        tags=["guardian", "host-vm", "ssh"],
        source_file="~/.genesis/guardian_remote.yaml",
    )]


def _parse_model_routing_endpoints(path: Path) -> list[ReferenceEntry]:
    """Extract base_url values for local provider endpoints.

    Only records entries for local/private endpoints (``localhost``,
    ``10.*``, ``192.168.*``, ``${OLLAMA_URL}``, ``${LM_STUDIO_URL}``), NOT
    public cloud provider URLs — those are identical everywhere and don't
    benefit from per-deployment reference capture.
    """
    if not path.exists():
        logger.warning("model_routing.yaml not found at %s — skipping", path)
        return []
    text = path.read_text()
    entries: list[ReferenceEntry] = []
    seen: set[str] = set()
    # Match lines like "    base_url: ${OLLAMA_URL:-http://localhost:11434}"
    # or "    base_url: http://10.176.34.199:11434"
    for match in re.finditer(
        r"^\s*base_url:\s*(\S+)\s*$", text, re.MULTILINE,
    ):
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        if not _looks_local_endpoint(raw):
            continue
        # Derive identifier from the env var name or URL host
        env_match = re.search(r"\$\{([A-Z_]+)", raw)
        ident = (
            f"{env_match.group(1)} endpoint"
            if env_match
            else f"Local endpoint {raw}"
        )
        entries.append(ReferenceEntry(
            kind="network",
            identifier=ident,
            value=raw,
            description=(
                "Local model provider endpoint declared in "
                "config/model_routing.yaml. Used by the routing layer to "
                "dispatch calls to a private/on-LAN model server."
            ),
            tags=["routing", "local-model"],
            source_file="config/model_routing.yaml",
        ))
    return entries


def _looks_local_endpoint(url: str) -> bool:
    """Heuristic: is this a local/private endpoint worth indexing?"""
    if "${" in url:
        return True  # env var reference — almost always local
    lower = url.lower()
    return (
        "localhost" in lower
        or "127.0.0.1" in lower
        or re.search(r"10\.\d+\.\d+\.\d+", lower) is not None
        or re.search(r"192\.168\.\d+\.\d+", lower) is not None
    )


def _parse_docs_reference(dir_path: Path) -> list[ReferenceEntry]:
    """One fact entry per markdown file under docs/reference/."""
    if not dir_path.exists():
        logger.warning("docs/reference/ not found at %s — skipping", dir_path)
        return []
    entries: list[ReferenceEntry] = []
    for md in sorted(dir_path.glob("*.md")):
        text = md.read_text(errors="replace")
        # First H1 or the filename stem
        h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = h1.group(1).strip() if h1 else md.stem
        # First non-header paragraph for a description hint
        summary = _first_paragraph(text)
        description = (
            f"Reference document: {title}. "
            f"Source file: docs/reference/{md.name}. "
            f"{'Summary: ' + summary if summary else ''}"
        ).strip()
        entries.append(ReferenceEntry(
            kind="fact",
            identifier=f"Doc: {title}",
            value=f"docs/reference/{md.name}",
            description=description,
            tags=["docs", "reference"],
            source_file=f"docs/reference/{md.name}",
        ))
    return entries


def _first_paragraph(text: str, *, max_len: int = 400) -> str:
    """First non-header, non-empty paragraph from a markdown document."""
    lines = text.splitlines()
    buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if buf:
                break
            continue
        if stripped.startswith("#"):
            continue
        buf.append(stripped)
    para = " ".join(buf)
    return para[:max_len].rstrip()


def _parse_personas(dir_path: Path) -> list[ReferenceEntry]:
    """One persona_pointer per directory under ~/.claude/personas/."""
    if not dir_path.exists():
        logger.warning("personas dir not found at %s — skipping", dir_path)
        return []
    entries: list[ReferenceEntry] = []
    for persona_dir in sorted(dir_path.iterdir()):
        if not persona_dir.is_dir():
            continue
        persona_md = persona_dir / "persona.md"
        if not persona_md.exists():
            logger.warning(
                "persona dir %s has no persona.md — skipping", persona_dir,
            )
            continue
        text = persona_md.read_text(errors="replace")
        summary = _first_paragraph(text, max_len=500)
        description = (
            f"Persona backstory pointer for {persona_dir.name}. "
            f"Canonical source is the persona.md file in the persona "
            f"directory — follow the Value pointer to read it. "
            f"{'Summary: ' + summary if summary else ''}"
        ).strip()
        entries.append(ReferenceEntry(
            kind="persona_pointer",
            identifier=f"{persona_dir.name} persona",
            value=str(persona_md),
            description=description,
            tags=["persona", persona_dir.name],
            source_file=str(persona_dir),
        ))
    return entries


def _parse_secrets_env_names(path: Path) -> list[ReferenceEntry]:
    """Index env var NAMES from secrets.env (NEVER values).

    One ``account`` entry listing every uppercase key=... line so a future
    session can answer "what credentials does Genesis have configured" via
    reference_lookup without reading the file.
    """
    if not path.exists():
        logger.warning("secrets.env not found at %s — skipping", path)
        return []
    names: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            names.append(match.group(1))
    if not names:
        return []
    names_sorted = sorted(set(names))
    description = (
        "Index of infrastructure credentials Genesis has configured in "
        "~/genesis/secrets.env. Values are NOT stored here — only the "
        "environment variable names, for discoverability. To read a value, "
        "source the file or read os.environ from a Genesis runtime process."
    )
    return [ReferenceEntry(
        kind="account",
        identifier="Genesis secrets.env env var index",
        value=", ".join(names_sorted),
        description=description,
        tags=["secrets", "infra", "env-vars"],
        source_file="~/genesis/secrets.env",
    )]


# ─── Ingestion driver ────────────────────────────────────────────────────────


async def _ingest_entry(
    entry: ReferenceEntry,
    *,
    store,
    db,
) -> tuple[str, bool]:
    """Call the shared ingest helper. Returns (unit_id, was_insert)."""
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    body = _format_reference_body(entry)
    tags_json = json.dumps(["reference", entry.kind, *entry.tags])
    provenance = {
        "source_doc": "reference_store:migrate_reference_data",
        "source_pipeline": "migrate_reference_data",
        "platform": "migrate_reference_data",
    }

    unit_id = await ingest_knowledge_unit(
        store=store,
        db=db,
        content=body,
        project="reference",
        domain=f"reference.{entry.kind}",
        authority="migrate_reference_data",
        provenance=provenance,
        memory_class="fact",  # bypass 0.7x auto-reference penalty
        concept=entry.identifier,
        tags_json=tags_json,
    )
    # ingest_knowledge_unit doesn't return the inserted flag directly;
    # we just return the ID here and count totals at the call site.
    return unit_id, True


async def main(args: argparse.Namespace) -> None:
    import aiosqlite

    repo_root = Path(__file__).resolve().parent.parent

    # Collect entries from all sources
    sources = [
        ("CLAUDE.md", _parse_network_identity(repo_root / "CLAUDE.md")),
        (
            "guardian_remote.yaml",
            _parse_guardian_remote(Path.home() / ".genesis" / "guardian_remote.yaml"),
        ),
        (
            "model_routing.yaml",
            _parse_model_routing_endpoints(
                repo_root / "config" / "model_routing.yaml",
            ),
        ),
        (
            "docs/reference/",
            _parse_docs_reference(repo_root / "docs" / "reference"),
        ),
        (
            "personas",
            _parse_personas(Path.home() / ".claude" / "personas"),
        ),
        (
            "secrets.env",
            _parse_secrets_env_names(Path.home() / "genesis" / "secrets.env"),
        ),
    ]

    total = sum(len(entries) for _, entries in sources)
    logger.info("Collected %d reference entries from %d sources", total, len(sources))
    for name, entries in sources:
        logger.info("  %-25s → %d entries", name, len(entries))

    if total == 0:
        logger.warning("No entries collected. Exiting.")
        return

    if args.dry_run:
        logger.info("── DRY RUN — no database writes ──")
        for name, entries in sources:
            for e in entries:
                logger.info(
                    "  [%s] %-18s (identifier redacted)",
                    name, e.kind,
                )
        logger.info("Dry run complete. Re-run without --dry-run to ingest.")
        return

    # Boot minimal runtime: db + qdrant + store. No router (no LLM calls).
    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        return

    from qdrant_client import QdrantClient

    from genesis.env import qdrant_url
    from genesis.memory.embeddings import EmbeddingProvider
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.store import MemoryStore

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        qdrant = QdrantClient(url=qdrant_url(), timeout=5)
        embedding_provider = EmbeddingProvider()
        linker = MemoryLinker(qdrant_client=qdrant, db=db)
        store = MemoryStore(
            embedding_provider=embedding_provider,
            qdrant_client=qdrant,
            db=db,
            linker=linker,
        )

        succeeded = 0
        failed = 0
        for name, entries in sources:
            for entry in entries:
                try:
                    unit_id, _ = await _ingest_entry(entry, store=store, db=db)
                    logger.info(
                        "  [%s] upserted kind=%s → unit_id=%s",
                        name, entry.kind, unit_id[:12],
                    )
                    succeeded += 1
                except Exception:
                    logger.exception(
                        "  [%s] failed to ingest kind=%s", name, entry.kind,
                    )
                    failed += 1

        logger.info(
            "Migration complete: %d succeeded, %d failed (idempotent upsert — "
            "safe to re-run)",
            succeeded, failed,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "One-shot idempotent importer for static reference data "
            "(CLAUDE.md, guardian_remote.yaml, model_routing.yaml, "
            "docs/reference/, ~/.claude/personas/, secrets.env env names)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be ingested without touching the database",
    )
    args = parser.parse_args()
    asyncio.run(main(args))
