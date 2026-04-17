"""Automatic reference classification + ingestion from extraction output.

The extraction_job pipeline runs every 2h, feeds conversation chunks through
the ``9_fact_extraction`` LLM call site, and produces structured
``Extraction`` objects. This module post-processes those extractions to
identify ones that look like persistent reference data (credentials, URLs
with context, IPs with descriptions, account handles) and routes them into
the reference store via :func:`ingest_knowledge_unit`.

This is the SILENT AUTO-CAPTURE path — no user prompt, no explicit flag.
Runs on the existing extraction cadence with zero new LLM calls.

Classification is conservative:
- High precision over recall. The primary capture path is the
  ``reference_store`` MCP tool called by the session agent in real-time
  with full conversational context. This background path is a fallback
  that catches things the primary path missed.
- Regex-based detection. The LLM already did semantic extraction upstream;
  we just pattern-match on the resulting structured content.
- When uncertain, do NOT store. False positives pollute the reference
  store — the user has to delete them.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from genesis.memory.knowledge_ingest import ingest_knowledge_unit

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.extraction import Extraction
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


# ─── Classification patterns ─────────────────────────────────────────────────

# Credential shape: user/pass pair in the same content window.
# Matches variants like "username: X password: Y", "user=X pass=Y",
# "login is X, password is Y". Case-insensitive.
# The value captures use negative lookahead to skip label words so nested
# "login: username: X" correctly picks X as the user token (not "username:").
_LABEL_WORDS = r"(?:user(?:name)?|login|pass(?:word)?|pwd|email|handle)"
_CREDENTIAL_PAIR_PATTERN = re.compile(
    r"\b(?:username|user|login)\s*(?:is\s+|[:=]\s*|\s+)"
    rf"(?P<user>(?!{_LABEL_WORDS}\b)[^\s,;]+)"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*|\s+)"
    rf"(?P<pass>(?!{_LABEL_WORDS}\b)[^\s,;]+)",
    re.IGNORECASE | re.DOTALL,
)

# Standalone token/API key shape.
_CREDENTIAL_TOKEN_PATTERN = re.compile(
    r"\b(?:api[_\s-]?key|access[_\s-]?token|bearer[_\s-]?token|secret[_\s-]?key)"
    r"\s*(?:is\s*|[:=]\s*|\s+)(?P<token>[A-Za-z0-9_\-\.]{16,})",
    re.IGNORECASE,
)

# URL shape — common http(s) URL, rejects trailing punctuation.
_URL_PATTERN = re.compile(r"https?://[^\s<>\]\"']+[^\s<>\]\"'.,;!?:]")

# IPv4 with optional port. IPv6 is intentionally omitted for v1 to keep
# the false-positive rate down.
_IPV4_PATTERN = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?\b"
)

# Words that signal "this IP is for something specific" rather than just
# a random number that looks like an IP (e.g. "10.0.0 percent uptime").
_NETWORK_CONTEXT_WORDS = frozenset({
    "server", "host", "container", "hostname", "runs", "running",
    "ip", "address", "endpoint", "api", "service", "deployment",
    "proxy", "gateway", "router", "port", "bind", "listen",
    "database", "db", "backend", "upstream",
})

# Phrases that signal generic ephemeral examples — if content contains
# any of these, don't auto-capture. Reduces false positives on
# hypothetical/documentation text.
#
# The trailing character class matches sentence-separator punctuation or
# whitespace but NOT word-continuation like a dot-then-letter, so
# "for example.com" (preposition + domain) does NOT match while
# "For example, the..." and "For example. Next..." do.
_EPHEMERAL_PATTERN = re.compile(
    r"\b(?:for example|e\.g\.|such as|might be|could be|would be|"
    r"hypothetical(?:ly)?|placeholder|sample credential|dummy|"
    r"todo|fixme|tbd|fill in)"
    r"(?:[,;:]|\s|\.(?=\s|$))",
    re.IGNORECASE,
)

# Minimum extraction content length (characters) — shorter than this
# typically indicates a fragment without enough context.
_MIN_CONTENT_LENGTH = 30

# Minimum context remaining after URL removal — URLs with very little
# surrounding prose are probably just bare links without descriptive intent.
_MIN_URL_CONTEXT_LENGTH = 20


def _looks_ephemeral(content: str) -> bool:
    """Check if content contains example/placeholder markers.

    Uses regex with trailing-whitespace requirement to avoid matching
    prepositions that happen to precede domain-like tokens
    ("for example.com" is NOT an example marker).
    """
    return bool(_EPHEMERAL_PATTERN.search(content))


def _derive_identifier(extraction: Extraction, *, default_prefix: str) -> str:
    """Build a human-readable identifier for the entry.

    Preference order:
    1. First named entity from the extraction
    2. First 80 chars of content trimmed to a sentence
    3. Default prefix with a short hash of the content
    """
    if extraction.entities:
        first = extraction.entities[0].strip()
        if first:
            return first[:120]
    content = extraction.content.strip()
    if not content:
        return f"{default_prefix} (empty)"
    # Take up to the first period or newline
    for stop in (".", "\n", ";"):
        idx = content.find(stop)
        if 0 < idx < 80:
            content = content[:idx]
            break
    return content[:80].strip() or default_prefix


def classify_as_reference(extraction: Extraction) -> dict | None:
    """Attempt to classify an Extraction as a reference entry.

    Returns a dict shaped like the ``reference_store`` input
    (``kind``, ``identifier``, ``value``, ``description``, ``tags``) if
    the extraction looks like persistent reference data, otherwise None.

    Classification is conservative — prefers recall=0 (no false positives)
    to recall=high (false positives pollute the store).
    """
    content = extraction.content or ""
    if len(content) < _MIN_CONTENT_LENGTH:
        return None
    if _looks_ephemeral(content):
        return None

    tags = [e for e in extraction.entities if e and e.strip()]

    # 1. Credential pair — highest priority
    pair = _CREDENTIAL_PAIR_PATTERN.search(content)
    if pair:
        user = pair.group("user")
        password = pair.group("pass")
        if user and password and len(password) >= 4:
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="login",
                ),
                "value": f"{user} / {password}",
                "description": content,
                "tags": tags,
            }

    # 2. Standalone token / API key
    token = _CREDENTIAL_TOKEN_PATTERN.search(content)
    if token:
        tok_value = token.group("token")
        return {
            "kind": "credentials",
            "identifier": _derive_identifier(
                extraction, default_prefix="token",
            ),
            "value": f"token: {tok_value}",
            "description": content,
            "tags": tags,
        }

    # 3. URL with context
    url_match = _URL_PATTERN.search(content)
    if url_match:
        url_str = url_match.group(0)
        # Require meaningful context around the URL — bare "https://..."
        # with no explanation is ephemeral.
        context_remainder = content.replace(url_str, "").strip()
        if len(context_remainder) >= _MIN_URL_CONTEXT_LENGTH:
            return {
                "kind": "url",
                "identifier": _derive_identifier(
                    extraction, default_prefix=url_str,
                ),
                "value": url_str,
                "description": content,
                "tags": tags,
            }

    # 4. IP address with network context
    ip_match = _IPV4_PATTERN.search(content)
    if ip_match:
        lower = content.lower()
        if any(word in lower for word in _NETWORK_CONTEXT_WORDS):
            return {
                "kind": "network",
                "identifier": _derive_identifier(
                    extraction, default_prefix=ip_match.group(0),
                ),
                "value": ip_match.group(0),
                "description": content,
                "tags": tags,
            }

    return None


# ─── Body formatting ─────────────────────────────────────────────────────────


def _format_reference_body(
    *,
    kind: str,
    identifier: str,
    description: str,
    value: str,
    tags: list[str],
    session_id: str | None,
) -> str:
    """Format a reference entry body for storage.

    Leading ``[reference.{kind}] {identifier}`` header salts the content
    so entries with coincidentally identical values don't collide in
    ``store.store()``'s exact-content dedup.

    Must stay structurally identical to the ``reference_store`` MCP tool's
    formatter so content extracted automatically has the same shape as
    content stored manually.
    """
    lines = [
        f"[reference.{kind}] {identifier}",
        "",
        description.strip(),
        "",
        f"Value: {value}",
    ]
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    session_marker = session_id or "unknown"
    lines.append(f"Captured: via=extraction_job session={session_marker}")
    return "\n".join(lines)


# ─── Ingestion ───────────────────────────────────────────────────────────────


async def ingest_reference_from_extraction(
    extraction: Extraction,
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    source_session_id: str | None = None,
) -> str | None:
    """Classify an Extraction and ingest it as a reference entry if matched.

    Returns the unit_id on success, or None if the extraction was not
    classified as persistent reference data. Never raises — a classifier
    or ingestion failure logs a warning and returns None.
    """
    try:
        ref = classify_as_reference(extraction)
    except Exception:
        logger.warning(
            "reference extractor classification failed", exc_info=True,
        )
        return None

    if ref is None:
        return None

    kind = ref["kind"]
    identifier = ref["identifier"]
    value = ref["value"]
    description = ref["description"]
    tags = ref["tags"]

    body = _format_reference_body(
        kind=kind,
        identifier=identifier,
        description=description,
        value=value,
        tags=tags,
        session_id=source_session_id,
    )

    all_tags = ["reference", kind, *tags]
    tags_json = json.dumps(all_tags)

    provenance: dict = {
        "source_doc": f"extraction_job:{source_session_id or 'unknown'}",
        "source_pipeline": "extraction_job",
        "platform": "extraction_job",
    }
    if source_session_id:
        provenance["session_id"] = source_session_id

    try:
        unit_id = await ingest_knowledge_unit(
            store=store,
            db=db,
            content=body,
            project="reference",
            domain=f"reference.{kind}",
            authority="extraction_job",
            provenance=provenance,
            memory_class="fact",  # bypass 0.7x auto-reference penalty
            concept=identifier,
            tags_json=tags_json,
        )
    except Exception:
        logger.warning(
            "reference extractor ingest failed for kind=%s identifier=%s",
            kind, identifier, exc_info=True,
        )
        return None

    logger.info(
        "Auto-captured reference: kind=%s identifier=%s session=%s",
        kind, identifier, source_session_id or "unknown",
    )
    return unit_id


async def extract_references_from_chunk(
    extractions: list[Extraction],
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    source_session_id: str | None = None,
) -> int:
    """Run the classifier over a chunk of extractions, ingesting references.

    Returns the count of entries promoted to the reference store. Used by
    ``run_extraction_cycle`` as a post-processing step after each LLM
    extraction call.
    """
    count = 0
    for extraction in extractions:
        unit_id = await ingest_reference_from_extraction(
            extraction,
            store=store,
            db=db,
            source_session_id=source_session_id,
        )
        if unit_id:
            count += 1
    return count


__all__ = [
    "classify_as_reference",
    "ingest_reference_from_extraction",
    "extract_references_from_chunk",
]
