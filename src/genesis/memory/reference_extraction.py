"""Automatic reference classification + ingestion from extraction output.

The extraction_job pipeline runs every 2h, feeds conversation chunks through
the ``9_fact_extraction`` LLM call site, and produces structured
``Extraction`` objects. This module post-processes those extractions to
identify ones that look like persistent reference data (credentials, URLs
with context, IPs with descriptions, account handles) and routes them into
the reference store via :func:`ingest_knowledge_unit`.

This is the SILENT AUTO-CAPTURE path — no user prompt, no explicit flag.
Runs on the existing extraction cadence with zero new LLM calls.

Classification balances recall against precision:
- Genuine references are captured broadly, but the credential/network kinds
  require precise signals (an explicit separator after a credential label;
  a network context word adjacent to the IP). They run over LLM-generated
  prose, where a loose match fabricates a misleading value rather than
  sitting idle harmlessly.
- **Gated to the USER's own words.** ``classify_as_reference`` /
  ``extract_references_from_chunk`` accept ``user_text`` (the chunk's
  user-authored content); a match is only captured if its raw value appears
  there. This stops the dominant noise source — mining Genesis's OWN analysis
  prose ("HybridRetriever.recall() -> password: rerank") for references.
- **Marked distinctly.** Auto-captured entries get the ``auto_captured`` tag
  and confidence 0.60 (vs 0.85 for real-time ``reference_store`` captures), so
  manual/verified vs auto/unverified is separable everywhere downstream.
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
from genesis.security.card_shapes import contains_card_number

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.extraction import Extraction
    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


# ─── Classification patterns ─────────────────────────────────────────────────
# Strategy: capture genuine references broadly, but require PRECISION for the
# credential/network kinds — these run over LLM-generated prose, where a loose
# match fabricates a misleading value (e.g. "can pin checkpoints" -> a fake
# credential "checkpoints"). So credential labels require an explicit separator
# (: = is), not a bare space, and IP/network matches require a context word
# adjacent to the match. Other kinds (known key prefixes, labeled API tokens,
# URLs) stay broad — their formats are self-validating.

# A. Credential pair — broadened. Any credential-label word near a value.
# Doesn't require BOTH user+pass — a single "password: X" is enough.
_LABEL_WORDS = r"(?:user(?:name)?|login|pass(?:word)?|pwd|email|handle|account)"
# "login for the X persona": a preposition or article after the label is prose,
# never the login, so the match moves on to the labelled "username: X".
_FILLER_WORDS = r"(?:for|of|to|the|a|an|with|at|on|in|as|via|and|or)"
_CREDENTIAL_PAIR_PATTERN = re.compile(
    r"\b(?:username|user|login)\s*(?:is\s+|[:=]\s*|\s+)"
    rf"(?P<user>(?!{_LABEL_WORDS}\b)(?!{_FILLER_WORDS}(?=[\s,;]))[^\s,;]+)"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*)"
    rf"(?P<pass>(?!{_LABEL_WORDS}\b)[^\s,;]+)",
    re.IGNORECASE | re.DOTALL,
)

# A2. Single credential label — catches "email X and password Y",
# "password: Z", "account u/Name", etc. without requiring a pair.
_SINGLE_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:password|pass(?:word)?|pwd|passphrase|passcode|pin)"
    r"\s*(?:is\s+|[:=]\s*)"
    r"(?P<value>[^\s,;]{4,})",
    re.IGNORECASE,
)

# A3. Email + password in natural language (the Reddit miss pattern).
# "email X and password Y" or "email X ... password Y"
_EMAIL_PASSWORD_PATTERN = re.compile(
    r"\b(?:e-?mail)\s*(?:is\s+|[:=]\s*|\s+)"
    r"(?P<email>[^\s,;]+@[^\s,;]+)"
    r".{0,200}?"
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*)"
    r"(?P<pass>[^\s,;]{4,})",
    re.IGNORECASE | re.DOTALL,
)

# B. Known key prefixes — format-only, no keyword needed.
# These prefixes are near-certain indicators of real credentials.
# DELIBERATELY NARROWER than the prefix classes in
# ``scripts/hooks/secret_scrub.py`` and ``src/genesis/security/output_scanner.py``.
# All three detect credentials; only this one ACTS on a match by writing a
# reference the user sees. The other two redact or flag, where over-matching is
# nearly free — here a false positive fabricates a credential entry in the
# reference store and the dashboard, which is the exact junk-capture this
# module's precision tests exist to prevent.
#
# Concretely: admitting ``-``/``_`` into the tail (correct for redaction, since
# modern keys namespace themselves) makes ordinary hyphenated slugs clear the
# length floor — "sk-learn-pipeline-preprocessing" and "glpat-staging-eu-west1"
# were both captured as credentials while that class was shared.
#
# What the narrowing actually excludes, stated precisely because the broad
# reading ("no unlabelled key is captured here") is wrong and would mislead the
# next reader — the plain forms below ARE captured unlabelled. MEASURED against
# this pattern:
#   * captured   — ghp_/gho_ + alnum, sk- + alnum, AKIA, AIza (hyphens allowed
#                  in its tail), xoxb-/xoxp-, di-
#   * NOT captured — a NAMESPACED tail on a narrowed prefix (`sk-ant-oat01-…`,
#                  `sk-or-v1-…`), because those prefixes admit only [A-Za-z0-9]
#                  after them; and any prefix carried solely by the scrubber's
#                  wider list (`github_pat_`, `glpat-`, `gsk_`, `hf_`, …).
# For those, the labelled path (_CREDENTIAL_TOKEN_PATTERN, "the API key is
# <value>") covers the ordinary conversational phrasing, and real-time
# ``reference_store`` calls remain the primary capture path — this module is
# only the safety net.
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
    r"\s*(?:is\s+|[:=]\s*)(?P<token>[A-Za-z0-9_\-\.]{16,})",
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
    r"\b(?:password|pass|pwd)\s*(?:is\s+|[:=]\s*)"
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

# How far (chars) from the IP/env-var match to look for a context word. The
# word must be NEAR the match, not just anywhere in the content — otherwise a
# far-away "container"/"ip" promotes incidental-IP prose to a network ref.
_NETWORK_CONTEXT_WINDOW = 40

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


def _user_shared(token: str | None, user_text: str | None) -> bool:
    """True if a captured value is attributable to the USER's own words.

    ``user_text`` is the concatenation of the user-authored messages in the
    chunk. When None the gate is DISABLED (back-compat for direct callers and
    unit tests). The check is a case-insensitive substring on the RAW matched
    token (password/IP/URL/key) — concrete strings the LLM preserves verbatim —
    so the auto-capture path can't mine Genesis's OWN analysis prose for
    references.
    """
    if user_text is None:
        return True
    if not token:
        return False
    return token.lower() in user_text.lower()


#: The description an auto-captured credential carries instead of the
#: extraction's own text. That text is already stored, scrubbed, as the
#: episodic memory beside it; copied into the reference it is free text the
#: value scrub must chase shape by shape, and nothing a lookup needs.
_CREDENTIAL_DESCRIPTION = "Credential captured automatically from a conversation."


def _credential_description(login: str | None = None) -> str:
    """The fixed description, naming the login when there is one: a login is an
    identifier a lookup searches by, and split_secret keeps it searchable."""
    return f"Login {login}. {_CREDENTIAL_DESCRIPTION}" if login else _CREDENTIAL_DESCRIPTION

#: An UPPER_SNAKE=value assignment counts as a credential only when a
#: ``_``-separated part of its key names one; MODEL_NAME=... or NODE_ENV=... is
#: configuration, and API_BASE or AUTHOR is not a key (whole parts, never
#: substrings: CAPITAL contains API).
_CRED_ENV_KEY_WORDS = frozenset({
    "KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PASS", "PWD", "AUTH",
    "CREDENTIAL", "CREDENTIALS",
})

#: The password in a URL's userinfo (scheme, then user, a colon, the password,
#: an at-sign, the host): the shape a connection string carries its secret in.
_URL_USERINFO_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]*://(?P<user>[^\s/:@]*):(?P<pw>[^\s@/]+)@",
)

#: Quoting around a capture is never part of it; a trailing "!" or "." can be,
#: and the classifier stores what ``_unwrap`` returns as the value.
_CAPTURE_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">", '"': '"', "'": "'", "`": "`"}
_CAPTURE_OPENERS = "([{<"


def _unwrap(value: str) -> str:
    """``value`` without the matched quote or bracket pairs around it. An
    unmatched quote or closing bracket is kept ("Pw9)" and "Fake9word'" are
    passwords); only an unmatched OPENING bracket ("(Pw9 ...") is an aside."""
    value = value.strip()
    while len(value) > 1 and _CAPTURE_PAIRS.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    if value and value[0] in _CAPTURE_OPENERS and _CAPTURE_PAIRS[value[0]] not in value:
        value = value[1:]
    return value


def _env_key_is_credential(key: str) -> bool:
    parts = [p for p in key.upper().split("_") if p]
    return any(p in _CRED_ENV_KEY_WORDS or p.endswith("KEY") for p in parts)


def _capture(pattern: re.Pattern[str], match: re.Match[str], group: str) -> str | None:
    """The secret a credential-pattern match captured, or None when the match
    is ordinary text rather than a credential.

    The one rule the classifier, the extraction scrub and the ingest
    chokepoint share, so whatever a scrub removes from memory is exactly what
    the classifier can keep as a reference: nothing the user shared is lost.
    The classifier's own guard against invented references is the user-text
    gate (``_user_shared``), not this rule. An environment assignment needs a
    credential word in its key; the value loses matched quotes or brackets
    around it.
    """
    value = _unwrap(match.group(group) or "")
    if not value:
        return None
    if pattern is _ENV_ASSIGNMENT_PATTERN:
        return value if _env_key_is_credential(match.group("key")) else None
    return value


def classify_as_reference(
    extraction: Extraction, user_text: str | None = None,
) -> dict | None:
    """Attempt to classify an Extraction as a reference entry.

    Returns a dict shaped like the ``reference_store`` input
    (``kind``, ``identifier``, ``value``, ``description``, ``tags``) if
    the extraction looks like persistent reference data, otherwise None.

    When ``user_text`` is provided (the chunk's user-authored content), a match
    is only returned if its raw value appears in the user's own words — so the
    auto-capture path doesn't fabricate references from Genesis's analysis
    prose. ``user_text=None`` disables the gate (direct callers / unit tests).
    """
    content = extraction.content or ""
    if len(content) < _MIN_CONTENT_LENGTH:
        return None
    if _looks_ephemeral(content):
        return None
    # A payment card number is never auto-captured: the reference store is
    # plaintext, and cards belong in the encrypted card vault.
    if contains_card_number(content):
        return None

    tags = [e for e in extraction.entities if e and e.strip()]

    # 1. Credential pair — highest priority (user + password together)
    # A credential pattern that matched but whose user-text gate declined it
    # must not fall through to the url/network branches below: those store the
    # extraction text as the description, password and all.
    declined = False

    pair = _CREDENTIAL_PAIR_PATTERN.search(content)
    password = _capture(_CREDENTIAL_PAIR_PATTERN, pair, "pass") if pair else None
    if pair and password:
        user = pair.group("user")
        declined = True
        if (
            user and len(password) >= 4
            and _user_shared(password, user_text)
        ):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="login",
                ),
                "value": f"{user} / {password}",
                "description": _credential_description(user),
                "tags": tags,
            }

    # 1b. Email + password pattern (catches "email X and password Y")
    email_pass = _EMAIL_PASSWORD_PATTERN.search(content)
    password = _capture(_EMAIL_PASSWORD_PATTERN, email_pass, "pass") if email_pass else None
    if email_pass and password:
        email = email_pass.group("email")
        declined = True
        if (
            email and len(password) >= 4
            and _user_shared(password, user_text)
        ):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="login",
                ),
                "value": f"{email} / {password}",
                "description": _credential_description(email),
                "tags": tags,
            }

    # 1c. Account lifecycle (catches "created account X with password Y")
    acct = _ACCOUNT_LIFECYCLE_PATTERN.search(content)
    password = _capture(_ACCOUNT_LIFECYCLE_PATTERN, acct, "pass") if acct else None
    if acct and password:
        account = acct.group("account")
        declined = True
        if (
            account and len(password) >= 4
            and _user_shared(password, user_text)
        ):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="account",
                ),
                "value": f"{account} / {password}",
                "description": _credential_description(account),
                "tags": tags,
            }

    # 1d. Single password mention (no user required)
    single = _SINGLE_CREDENTIAL_PATTERN.search(content)
    password = _capture(_SINGLE_CREDENTIAL_PATTERN, single, "value") if single else None
    if single and password:
        declined = True
        if (
            len(password) >= 4
            and _user_shared(password, user_text)
        ):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="credential",
                ),
                "value": f"password: {password}",
                "description": _credential_description(),
                "tags": tags,
            }

    # 2. Known key prefixes — format-only, near-zero false positive rate
    known_key = _KNOWN_KEY_PREFIX_PATTERN.search(content)
    if known_key:
        tok_value = known_key.group("token")
        declined = True
        if _user_shared(tok_value, user_text):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="api_key",
                ),
                "value": f"token: {tok_value}",
                "description": _credential_description(),
                "tags": tags,
            }

    # 3. Standalone token / API key (labeled)
    token = _CREDENTIAL_TOKEN_PATTERN.search(content)
    tok_value = _capture(_CREDENTIAL_TOKEN_PATTERN, token, "token") if token else None
    if token and tok_value:
        declined = True
        if _user_shared(tok_value, user_text):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix="token",
                ),
                "value": f"token: {tok_value}",
                "description": _credential_description(),
                "tags": tags,
            }

    # 3b. .env format — UPPER_SNAKE=value
    # The first credential-keyed assignment, not merely the first assignment.
    env = next(
        (m for m in _ENV_ASSIGNMENT_PATTERN.finditer(content)
         if _capture(_ENV_ASSIGNMENT_PATTERN, m, "val")),
        None,
    )
    if env:
        key = env.group("key")
        val = env.group("val")
        declined = True
        if _user_shared(val, user_text):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(
                    extraction, default_prefix=key,
                ),
                "value": f"{key}={val}",
                "description": _credential_description(),
                "tags": tags,
            }

    # 3d. A connection string (scheme://user:password@host/...): the scrub
    # removes its password from memory, so the reference keeps the whole URL.
    userinfo = _URL_USERINFO_PATTERN.search(content)
    pw = _capture(_URL_USERINFO_PATTERN, userinfo, "pw") if userinfo else None
    if userinfo and pw:
        declined = True
        if _user_shared(pw, user_text):
            end = userinfo.end()
            while end < len(content) and not content[end].isspace():
                end += 1
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(extraction, default_prefix="connection"),
                "value": content[userinfo.start():end].rstrip(".,;"),
                "description": _credential_description(userinfo.group("user") or None),
                "tags": tags,
            }

    # Also when only the scrub's wider rule sees a secret ("the password is
    # sunshine"): the branches below store this text as the description.
    if declined or captured_secrets([content]):
        return None

    # 3c. SSH user@host
    ssh = _SSH_PATTERN.search(content)
    if ssh:
        userhost = ssh.group("userhost")
        if _user_shared(userhost, user_text):
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
        if len(context_remainder) >= _MIN_URL_CONTEXT_LENGTH and _user_shared(
            url_str, user_text,
        ):
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
        # Require a network context word NEAR the match (windowed) with word
        # boundaries — not anywhere in the content by substring. Prose that
        # merely mentions an IP (code analysis, planning notes) with a far-away
        # "container"/"ip" must NOT become a network reference. Word boundaries
        # also stop the match itself (${CONTAINER_IP}, localhost) from
        # supplying its own context.
        start, end = net_match.span()
        window = content[
            max(0, start - _NETWORK_CONTEXT_WINDOW): end + _NETWORK_CONTEXT_WINDOW
        ].lower()
        if (
            any(
                re.search(rf"\b{re.escape(word)}\b", window)
                for word in _NETWORK_CONTEXT_WORDS
            )
            and _user_shared(net_match.group(0), user_text)
        ):
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
    user_text: str | None = None,
) -> str | None:
    """Classify an Extraction and ingest it as a reference entry if matched.

    ``user_text`` (the chunk's user-authored content) gates auto-capture to
    values the USER actually shared; auto-captured entries are marked with the
    ``auto_captured`` tag and a lower confidence (0.60 vs reference_store 0.85).

    Returns the unit_id on success, or None if the extraction was not
    classified as persistent reference data. Never raises — a classifier
    or ingestion failure logs a warning and returns None.
    """
    try:
        ref = classify_as_reference(extraction, user_text=user_text)
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
    # The body is what gets stored, and its identifier and tags come from the
    # extraction's entities, which the content check above does not see.
    if contains_card_number(body):
        return None

    all_tags = ["reference", kind, *tags, "auto_captured"]
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
            confidence=0.60,  # auto-captured: lower than reference_store (0.85)
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
    user_text: str | None = None,
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
            user_text=user_text,
        )
        if unit_id:
            count += 1
    return count


#: Every pattern above that captures a SECRET, with the group holding it. The
#: scrub runs all of them over every text field and collects every match,
#: because the classifier stops at the first credential and one extraction can
#: name several ("password: X; backup password: Y").
_SECRET_GROUPS = (
    (_CREDENTIAL_PAIR_PATTERN, "pass"),
    (_EMAIL_PASSWORD_PATTERN, "pass"),
    (_ACCOUNT_LIFECYCLE_PATTERN, "pass"),
    (_SINGLE_CREDENTIAL_PATTERN, "value"),
    (_KNOWN_KEY_PREFIX_PATTERN, "token"),
    (_CREDENTIAL_TOKEN_PATTERN, "token"),
    (_ENV_ASSIGNMENT_PATTERN, "val"),
    (_URL_USERINFO_PATTERN, "pw"),
)
_TEXT_FIELDS = ("content", "event_subject", "event_verb", "event_object", "scenario", "temporal")


def _extraction_texts(extraction: Extraction) -> list[str]:
    texts = [getattr(extraction, name) for name in _TEXT_FIELDS]
    texts += list(extraction.entities)
    for rel in extraction.relationships:
        if isinstance(rel, dict):
            texts += list(rel.values())
    return [t for t in texts if isinstance(t, str) and t]


def captured_secrets(texts: list[str]) -> set[str]:
    """Every secret a credential pattern captures in ``texts`` (``_capture``'s
    rule), across every match of every pattern."""
    secrets: set[str] = set()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for pattern, group in _SECRET_GROUPS:
            for match in pattern.finditer(text):
                try:
                    value = _capture(pattern, match, group)
                except Exception:
                    # Redact the whole raw capture rather than skip it.
                    value = (match.group(group) or "").strip()
                if value:
                    secrets.add(value)
    return secrets


def scrub_credential_extraction(extraction: Extraction) -> bool:
    """Redact every credential secret an extraction carries, in place.

    The extraction job stores each extraction as an episodic memory, logs it,
    dedups and links it, and embeds it in the connection pass, AFTER it may have
    been promoted to a reference. The reference holds the value out (#2426);
    without this the same text would put it straight back into memory_fts, the
    vector store, entity mentions, memory_events and the journal. Every secret
    any credential pattern captures, in any text field, is redacted from every
    text field (with the fragment rules ``reference_ops.secret_fragments``
    applies to a stored value), whether or not auto-capture stored a reference:
    redacting more is the safe side. Returns True when anything was redacted.
    """
    from genesis.memory.reference_ops import scrub, secret_fragments

    try:
        secrets = captured_secrets(_extraction_texts(extraction))
    except Exception:
        logger.warning("credential scrub: matching failed", exc_info=True)
        return False
    if not secrets:
        return False
    fragments = sorted(
        {f for value in secrets for f in secret_fragments(value)}, key=len, reverse=True,
    )

    # A fragment with a digit or a symbol is replaced wherever it occurs; an
    # all-letter one only where it stands as a word, because this scrub runs on
    # every extraction and inside a longer word it is that word ("re-stored").
    def _clean(value: object) -> object:
        return scrub(value, fragments) if isinstance(value, str) else value

    before = _extraction_texts(extraction)
    for name in _TEXT_FIELDS:
        setattr(extraction, name, _clean(getattr(extraction, name)))
    extraction.entities = [_clean(e) for e in extraction.entities]
    extraction.relationships = [
        {k: _clean(v) for k, v in rel.items()} if isinstance(rel, dict) else rel
        for rel in extraction.relationships
    ]
    return _extraction_texts(extraction) != before


__all__ = [
    "classify_as_reference",
    "ingest_reference_from_extraction",
    "extract_references_from_chunk",
    "captured_secrets",
    "scrub_credential_extraction",
]
