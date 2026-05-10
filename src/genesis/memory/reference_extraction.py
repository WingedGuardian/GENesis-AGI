"""Automatic reference classification + ingestion from extraction output.

The extraction_job pipeline runs every 2h, feeds conversation chunks through
the ``9_fact_extraction`` LLM call site, and produces structured
``Extraction`` objects. This module post-processes those extractions to
identify ones that look like persistent reference data (credentials, URLs
with context, IPs with descriptions, account handles) and routes them into
the reference store via :func:`ingest_knowledge_unit`.

This is the SILENT AUTO-CAPTURE path — no user prompt, no explicit flag.
Runs on the existing extraction cadence with zero new LLM calls.

Classification errs on the side of capturing more:
- False positives are harmless (unused entries sit idle). False negatives
  lose credentials permanently. Multiple overlapping patterns are
  intentional — if any one fires, we capture.
- The primary capture path is the LLM calling ``reference_store`` in
  real-time. This background path is a safety net that catches things
  the primary path missed.
- Regex-based detection. The LLM already did semantic extraction upstream;
  we just pattern-match on the resulting structured content.
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
# Strategy: ERR ON THE SIDE OF CAPTURING MORE. False positives in the
# reference store are harmless (unused entries sit idle). False negatives
# lose credentials permanently. Multiple overlapping patterns are
# intentional — if any one fires, we capture.

# A. Credential pair — broadened. Any credential-label word near a value.
# Doesn't require BOTH user+pass — a single "password: X" is enough.
_LABEL_WORDS = r"(?:user(?:name)?|login|pass(?:word)?|pwd|email|handle|account)"
_CREDENTIAL_PAIR_PATTERN = re.compile(
    r"\b(?:username|user|login)\s*(?:is\s+|[:=]\s*|\s+)"
    rf"(?P<user>(?!{_LABEL_WORDS}\b)[^\s,;]+)"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*|\s+)"
    rf"(?P<pass>(?!{_LABEL_WORDS}\b)[^\s,;]+)",
    re.IGNORECASE | re.DOTALL,
)

# A2. Single credential label — catches "email X and password Y",
# "password: Z", "account u/Name", etc. without requiring a pair.
_SINGLE_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:password|pass(?:word)?|pwd|passphrase|passcode|pin)"
    r"\s*(?:is\s+|[:=]\s*|\s+)"
    r"(?P<value>[^\s,;]{4,})",
    re.IGNORECASE,
)

# A3. Email + password in natural language (the Reddit miss pattern).
# "email X and password Y" or "email X ... password Y"
_EMAIL_PASSWORD_PATTERN = re.compile(
    r"\b(?:e-?mail)\s*(?:is\s+|[:=]\s*|\s+)"
    r"(?P<email>[^\s,;]+@[^\s,;]+)"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*|\s+)"
    r"(?P<pass>[^\s,;]{4,})",
    re.IGNORECASE | re.DOTALL,
)

# B. Known key prefixes — format-only, no keyword needed.
# These prefixes are near-certain indicators of real credentials.
_KNOWN_KEY_PREFIX_PATTERN = re.compile(
    r"(?P<token>"
    r"ghp_[A-Za-z0-9]{30,}"       # GitHub personal access token
    r"|gho_[A-Za-z0-9]{30,}"      # GitHub OAuth token
    r"|sk-[A-Za-z0-9]{20,}"       # OpenAI / Anthropic
    r"|xoxb-[A-Za-z0-9\-]{20,}"   # Slack bot token
    r"|xoxp-[A-Za-z0-9\-]{20,}"   # Slack user token
    r"|AKIA[A-Z0-9]{12,}"         # AWS access key
    r"|AIza[A-Za-z0-9_\-]{30,}"   # Google API key
    r"|di-[A-Za-z0-9]{20,}"       # DeepInfra
    r")",
)

# C. Standalone token/API key shape (broadened labels).
_CREDENTIAL_TOKEN_PATTERN = re.compile(
    r"\b(?:api[_\s-]?key|access[_\s-]?token|bearer[_\s-]?token|"
    r"secret[_\s-]?key|auth[_\s-]?token|refresh[_\s-]?token|"
    r"api[_\s-]?secret|private[_\s-]?key)"
    r"\s*(?:is\s*|[:=]\s*|\s+)(?P<token>[A-Za-z0-9_\-\.]{16,})",
    re.IGNORECASE,
)

# D. .env format — UPPER_SNAKE=value where value looks secret-like.
_ENV_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?P<key>[A-Z][A-Z0-9_]{3,})"
    r"\s*=\s*"
    r"(?P<val>[^\s]{8,})",
)

# E. SSH user@host pattern.
_SSH_PATTERN = re.compile(
    r"\bssh\s+(?:.*?\s)?(?P<userhost>[A-Za-z0-9_.\-]+@[A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)

# F. Account lifecycle — "created account X with password Y".
_ACCOUNT_LIFECYCLE_PATTERN = re.compile(
    r"\b(?:created|registered|signed?\s*up|set\s*up)\s+"
    r"(?:an?\s+)?(?:account|user|login)\s+"
    r"(?:for\s+|named?\s+|called?\s+)?"
    r"(?P<account>[^\s,;]{2,})"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*|\s+)"
    r"(?P<pass>[^\s,;]{4,})",
    re.IGNORECASE | re.DOTALL,
)

# URL shape — common http(s) URL, rejects trailing punctuation.
_URL_PATTERN = re.compile(r"https?://[^\s<>\]\"']+[^\s<>\]\"'.,;!?:]")

# IPv4 with optional port. IPv6 is intentionally omitted for v1 to keep
# the false-positive rate down.
_IPV4_PATTERN = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?\b"
)

# Shell template variable — ${VAR_NAME} or ${VAR_NAME:-default}.
# Matched as a fallback when no literal IPv4 is present but the content
# contains network context words (same gate as IPv4).
_ENV_VAR_PATTERN = re.compile(
    r"\$\{[A-Z_][A-Z0-9_]*(?::[-+]?[^}]*)?\}"
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

    Errs on the side of capturing more — false positives are harmless,
    false negatives lose credentials permanently.
    """
    content = extraction.content or ""
    if len(content) < _MIN_CONTENT_LENGTH:
        return None
    if _looks_ephemeral(content):
        return None

    tags = [e for e in extraction.entities if e and e.strip()]

    # 1. Credential pair — highest priority (user + password together)
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

    # 1b. Email + password pattern (catches "email X and password Y")
    email_pass = _EMAIL_PASSWORD_PATTERN.search(content)
    if email_pass:
        email = email_pass.group("email")
        password = email_pass.group("pass")
        if email and password and len(password) >= 4:
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="login",
                ),
                "value": f"{email} / {password}",
                "description": content,
                "tags": tags,
            }

    # 1c. Account lifecycle (catches "created account X with password Y")
    acct = _ACCOUNT_LIFECYCLE_PATTERN.search(content)
    if acct:
        account = acct.group("account")
        password = acct.group("pass")
        if account and password and len(password) >= 4:
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="account",
                ),
                "value": f"{account} / {password}",
                "description": content,
                "tags": tags,
            }

    # 1d. Single password mention (no user required)
    single = _SINGLE_CREDENTIAL_PATTERN.search(content)
    if single:
        password = single.group("value")
        if password and len(password) >= 4:
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="credential",
                ),
                "value": f"password: {password}",
                "description": content,
                "tags": tags,
            }

    # 2. Known key prefixes — format-only, near-zero false positive rate
    known_key = _KNOWN_KEY_PREFIX_PATTERN.search(content)
    if known_key:
        tok_value = known_key.group("token")
        return {
            "kind": "credentials",
            "identifier": _derive_identifier(
                extraction, default_prefix="api_key",
            ),
            "value": f"token: {tok_value}",
            "description": content,
            "tags": tags,
        }

    # 3. Standalone token / API key (labeled)
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

    # 3b. .env format — UPPER_SNAKE=value
    env = _ENV_ASSIGNMENT_PATTERN.search(content)
    if env:
        key = env.group("key")
        val = env.group("val")
        # Only capture if the key name suggests a credential
        _CRED_KEY_WORDS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "PASS", "AUTH", "API"}
        if any(w in key.upper() for w in _CRED_KEY_WORDS):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix=key,
                ),
                "value": f"{key}={val}",
                "description": content,
                "tags": tags,
            }

    # 3c. SSH user@host
    ssh = _SSH_PATTERN.search(content)
    if ssh:
        userhost = ssh.group("userhost")
        return {
            "kind": "network",
            "identifier": _derive_identifier(
                extraction, default_prefix=f"ssh {userhost}",
            ),
            "value": f"ssh {userhost}",
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

    # 4. IP address or env-var placeholder with network context
    ip_match = _IPV4_PATTERN.search(content)
    env_match = _ENV_VAR_PATTERN.search(content) if not ip_match else None
    net_match = ip_match or env_match
    if net_match:
        # For env-var matches, strip the match itself before checking context
        # to avoid false positives from defaults like "localhost" containing "host".
        ctx = content if ip_match else content.replace(net_match.group(0), "")
        lower = ctx.lower()
        if any(word in lower for word in _NETWORK_CONTEXT_WORDS):
            return {
                "kind": "network",
                "identifier": _derive_identifier(
                    extraction, default_prefix=net_match.group(0),
                ),
                "value": net_match.group(0),
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
    force_fts5_only: bool = False,
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
            force_fts5_only=force_fts5_only,
            collection="episodic_memory",
            memory_type="episodic",
        )
    except Exception:
        logger.warning(
            "reference extractor ingest failed for kind=%s identifier=<redacted>",
            kind, exc_info=True,
        )
        return None

    logger.info(
        "Auto-captured reference: kind=%s identifier=<redacted> session=%s",
        kind, source_session_id or "unknown",
    )
    return unit_id


async def extract_references_from_chunk(
    extractions: list[Extraction],
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    source_session_id: str | None = None,
    force_fts5_only: bool = False,
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
            force_fts5_only=force_fts5_only,
        )
        if unit_id:
            count += 1
    return count


__all__ = [
    "classify_as_reference",
    "ingest_reference_from_extraction",
    "extract_references_from_chunk",
]
