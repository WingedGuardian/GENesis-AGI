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
import unicodedata
from secrets import token_hex as secrets_token_hex
from typing import TYPE_CHECKING
from urllib.parse import unquote

from genesis.db.crud.knowledge import CredentialKeyTaken
from genesis.memory.knowledge_ingest import ingest_knowledge_unit
from genesis.memory.reference_ops import (
    login_is_key_shaped,
    refuse_value_in_fields,
    resolve_value,
)
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
# "login for the X persona": a preposition or article after a PROSE separator
# ("is" or a bare space) is never the login, so the match moves on to the
# labelled login after it. After an explicit colon or equals sign the next word
# IS the login, whatever it spells (an article or a preposition included).
_FILLER_WORDS = r"(?:for|of|to|the|a|an|with|at|on|in|as|via|and|or)"
_NOT_FILLER = rf"(?!{_FILLER_WORDS}(?=[\s,;]))"
_CREDENTIAL_PAIR_PATTERN = re.compile(
    # Each separator owns its leading whitespace: a shared "\s*" before a "\s+"
    # alternative makes a long whitespace run scan quadratically.
    r"\b(?:username|user|login)"
    rf"(?:\s*is\s+{_NOT_FILLER}|\s*[:=]\s*|\s+{_NOT_FILLER})"
    rf"(?P<user>(?!{_LABEL_WORDS}\b)[^\s,;]+)"
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
    r"\b(?:e-?mail)(?:\s*is\s+|\s*[:=]\s*|\s+)"
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

    The identifier is the unit's unique key and is indexed in knowledge_fts. A
    secret the text or an entity carries never becomes part of it (#2426):
    every secret ``captured_secrets`` finds in the content and the entities is
    redacted from either source.
    """
    secrets = captured_secrets([extraction.content or "", *_entity_texts(extraction)])
    if extraction.entities:
        first = extraction.entities[0].strip()
        if first:
            named = _redacted(first, secrets)
            if named == first:
                return first[:120]
            # Redacted, the entity no longer tells two credentials apart: a
            # random tag keeps each under its own key, as for the content below.
            return f"{named[:120]} #{secrets_token_hex(3)}"
    content = extraction.content.strip()
    if not content:
        return f"{default_prefix} (empty)"
    content = _redacted(content, secrets)
    # Take up to the first period or newline
    for stop in (".", "\n", ";"):
        idx = content.find(stop)
        if 0 < idx < 80:
            content = content[:idx]
            break
    identifier = content[:80].strip() or default_prefix
    if secrets:
        # With its secret redacted, this text no longer tells two credentials
        # apart ("username: admin, password: [redacted]"), and a shared key
        # would let the second capture replace the first. Each capture is kept
        # under its own key instead (owner ruling: a rotated password adds an
        # entry rather than risking a lost one). The tag is random, never
        # derived from the value: a short hash of a password, indexed in the
        # key, could be brute-forced back to a weak one.
        identifier = f"{identifier} #{secrets_token_hex(3)}"
    return identifier


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


#: What a captured secret becomes in the identifier built from the text.
_REDACTED = "[redacted]"


def _redacted(text: str, secrets: set[str]) -> str:
    """``text`` with every one of ``secrets`` replaced, longest first."""
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def _entity_texts(extraction: Extraction) -> list[str]:
    return [e for e in (extraction.entities or []) if isinstance(e, str)]


def _carries_secret(text: str, secrets: set[str]) -> bool:
    """Whether ``text`` carries one of ``secrets`` (the field check's own
    matching, ``refuse_value_in_fields``) or a secret of its own
    (``captured_secrets``)."""
    if captured_secrets([text]):
        return True
    for secret in secrets:
        try:
            refuse_value_in_fields(secret, {"text": text})
        except ValueError:
            return True
    return False

#: The identifier a capture falls back to when the one built from its text
#: still carries part of the value.
_FALLBACK_IDENTIFIER = "credential"


def _login_is_nameable(login: str, secret: str) -> bool:
    """Whether a login may be named in the indexed identifier: it is no key or
    token itself (``reference_ops.login_is_key_shaped``, ``captured_secrets``),
    and it and the secret do not contain each other ("admin / admin")."""
    folded_login, folded_secret = login.casefold(), secret.casefold()
    return not (
        login_is_key_shaped(login)
        or captured_secrets([login])
        or folded_login in folded_secret
        or folded_secret in folded_login
    )


def _with_login(identifier: str, login: str | None, secret: str | None) -> str:
    """``identifier`` naming ``login``, the login of a credential whose secret
    is ``secret``.

    A credential's description is held out of the index with its value, so
    the identifier is where a lookup by login finds it (the identifier check
    exempts the login half). Only the classifier branches that parse a login
    (a user and password pair, an email and password, an account) pass one;
    a "password: ...", "token: ..." or "KEY=..." value has none. Kept before
    any random tag; unchanged when there is no login, the login is not
    nameable (``_login_is_nameable``), or the identifier already names it."""
    login = (login or "").strip()
    if (
        not login or not secret or not secret.strip()
        or not _login_is_nameable(login, secret.strip())
        or login.casefold() in identifier.casefold()
    ):
        return identifier
    tag = _TAG_SUFFIX.search(identifier)
    base = identifier[: tag.start()] if tag else identifier
    return f"{base} ({login})" + (tag.group(0) if tag else "")


def _identifier_the_write_accepts(
    identifier: str,
    value: str,
    *,
    login: str | None = None,
    secret: str | None = None,
    secrets: set[str] | frozenset[str] = frozenset(),
) -> str:
    """``identifier``, or a neutral one when it still carries a secret.

    The write refuses a credential whose identifier carries the value or a
    secret-shaped part of it (``reference_ops.refuse_value_in_fields``); this
    also probes every other secret the extraction carries (``secrets``, from
    ``captured_secrets``), which the write never sees. The capture rule
    redacts what it isolates, but a part it does not (a token glued into
    prose) would make the capture fail and be lost; it is kept under the
    fallback name, its login and a random tag instead, and under the bare
    fallback name when even that is refused."""

    def accepted(candidate: str) -> bool:
        try:
            refuse_value_in_fields(value, {"identifier": candidate}, login_exempt=True)
            for other in secrets:
                refuse_value_in_fields(other, {"identifier": candidate})
        except ValueError:
            return False
        return True

    if accepted(identifier):
        return identifier
    fallback = f"{_FALLBACK_IDENTIFIER} #{secrets_token_hex(3)}"
    named = _with_login(fallback, login, secret)
    return named if accepted(named) else fallback


#: The description an auto-captured credential carries instead of the
#: extraction's own text. Copied into the reference, that text is free prose
#: the value hold-out would have to chase shape by shape, and nothing a lookup
#: needs: the identifier and the login carry what a lookup searches by.
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
#: A part that runs a credential word into a prefix (OPENAIAPIKEY,
#: AWSSECRETKEY). An explicit list, never "ends in KEY": HOTKEY, MONKEY and
#: TURKEY are not credentials.
_CRED_ENV_KEY_SUFFIXES = ("APIKEY", "SECRETKEY", "ACCESSKEY", "PRIVATEKEY")

#: The password in a URL's userinfo. The scheme is bounded: an unbounded one
#: scans quadratically on a long run of scheme characters with no "://" after
#: it. The password runs to the LAST at-sign before the host, so one that
#: carries an unencoded at-sign is captured whole.
_URL_USERINFO_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://(?P<user>[^\s/:@]*):(?P<pw>[^\s/]+)@",
)
#: A URL that reads as carrying userinfo with a password ("scheme://user:...@")
#: whatever the password spells, an unencoded "/" included. Where
#: ``_URL_USERINFO_PATTERN`` captures nothing but this matches, the text is
#: refused for every kind (nothing is stored), rather than parsed: an
#: unencoded "/" ends the authority (RFC 3986), so where such a password ends
#: is a guess, and a wrong guess would store part of it as an ordinary url.
#: It fails closed on a port followed by a path whose query carries an
#: at-sign ("host:8080/x?to=a@b"), a lost url capture and nothing worse.
#: Every run is bounded, so a long run with no at-sign scans linearly.
_URL_USERINFO_SHAPE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/:@]{0,256}:[^\s@]{1,256}@",
)
#: A credential-named query parameter ("?token=...", "&X-Amz-Signature=...")
#: with a value of eight or more characters. Its value is a captured secret
#: (``_SECRET_GROUPS``), so a URL carrying one is never stored as a url or
#: network reference: the no-fall-through rule refuses the text, and the value
#: is redacted from identifiers and tags. It is not captured as a credential
#: of its own: a signed or tokened URL has no login or name to keep it under,
#: and refusing keeps the value out of every store.
_URL_QUERY_SECRET_PATTERN = re.compile(
    r"[?&](?:[A-Za-z0-9.\-]{0,32}[_\-])?"
    r"(?:token|key|secret|password|passwd|pwd|sig|signature|auth|access_token|"
    r"api_key|apikey|client_secret)"
    # The value has a digit, or is long enough (16+) to be a random token:
    # "sort_key=created_at_desc" is a setting, not a key.
    r"=(?P<val>(?=[^\s&#\"'<>]*\d)[^\s&#\"'<>]{8,}|[^\s&#\"'<>]{16,})",
    re.IGNORECASE,
)

#: Quoting around a capture is never part of it; a trailing "!" or "." can be,
#: and the classifier stores what ``_unwrap`` returns as the value.
_CAPTURE_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">", '"': '"', "'": "'", "`": "`"}
_CAPTURE_OPENERS = "([{<"
_QUOTES = "\"'`"
#: How far a quoted capture may run to its closing quote: the same reach the
#: patterns' own label-to-value gaps allow (``.{0,200}?``). A quote with no
#: close within it, or only across a line break, is not taken as a quoted value.
_QUOTED_REACH = 200


def _unwrap(value: str) -> str:
    """``value`` without the matched quote or bracket pairs around it. An
    unmatched quote or closing bracket is kept (a password can end in one);
    only an unmatched OPENING bracket, which starts an aside, is dropped."""
    value = value.strip()
    while len(value) > 1 and _CAPTURE_PAIRS.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    if value and value[0] in _CAPTURE_OPENERS and _CAPTURE_PAIRS[value[0]] not in value:
        value = value[1:]
    return value


def _quoted(match: re.Match[str], group: str) -> str:
    """The raw capture, run on to its closing quote when it opens a quote it
    does not close: the patterns stop at whitespace, so a quoted multi-word
    secret would otherwise be cut to its first word. The closing quote must end
    a word, so an apostrophe inside later prose ("it's") is not taken for it."""
    raw = match.group(group) or ""
    if not raw or raw[0] not in _QUOTES or raw.count(raw[0]) != 1:
        return raw
    text, start = match.string, match.start(group)
    limit = min(len(text), start + 1 + _QUOTED_REACH)
    close = text.find(raw[0], start + 1, limit)
    while close != -1:
        if "\n" in text[start:close]:
            return raw
        if close + 1 >= len(text) or not text[close + 1].isalnum():
            return text[start:close + 1]
        close = text.find(raw[0], close + 1, limit)
    return raw


def _env_key_is_credential(key: str) -> bool:
    parts = [p for p in key.upper().split("_") if p]
    return any(
        p in _CRED_ENV_KEY_WORDS or p.endswith(_CRED_ENV_KEY_SUFFIXES) for p in parts
    )


def _capture(pattern: re.Pattern[str], match: re.Match[str], group: str) -> str | None:
    """The secret a credential-pattern match captured, or None when the match
    is ordinary text rather than a credential.

    The one rule the classifier and ``captured_secrets`` share, so what the
    classifier would keep as a reference is exactly what the no-fall-through
    rule and the identifier hold-out treat as a secret. The classifier's own
    guard against invented references is the user-text gate
    (``_user_shared``), not this rule. An environment assignment needs a
    credential word in its key; a quoted value runs to its closing quote; the
    value loses matched quotes or brackets around it.
    """
    value = _unwrap(_quoted(match, group))
    if not value:
        return None
    if pattern is _ENV_ASSIGNMENT_PATTERN:
        return value if _env_key_is_credential(match.group("key")) else None
    return value


def _connection_string(content: str, start: int, end: int) -> str:
    """The connection string that starts at ``start``: up to the next
    whitespace, less trailing punctuation, and less the closing half of a quote
    or bracket pair whose opening half sits right before it (``_unwrap``'s rule:
    a matched pair around a capture is never part of it)."""
    while end < len(content) and not content[end].isspace():
        end += 1
    value = content[start:end].rstrip(".,;")
    closer = _CAPTURE_PAIRS.get(content[start - 1]) if start > 0 else None
    if closer and value.endswith(closer):
        value = value[:-1].rstrip(".,;")
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

    # A tag that carries a secret (of the text or an entity) is dropped whole.
    tags = [e for e in extraction.entities if e and e.strip()]
    tag_secrets = captured_secrets([content, *tags])
    tags = [t for t in tags if not _carries_secret(t, tag_secrets)]

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
                "login": user,
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
                "login": email,
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
                "login": account,
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
        # The value the shared capture rule isolates (quotes off, a quoted
        # value run to its close), never the raw group: what is stored and
        # gated is what captured_secrets holds out.
        val = _capture(_ENV_ASSIGNMENT_PATTERN, env, "val")
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

    # 3d. A connection string (a URL whose userinfo carries a password): kept as
    # a credential, so its password is held out with the rest of the value.
    userinfo = _URL_USERINFO_PATTERN.search(content)
    pw = _capture(_URL_USERINFO_PATTERN, userinfo, "pw") if userinfo else None
    if userinfo and pw:
        declined = True
        if _user_shared(pw, user_text):
            return {
                "kind": "credentials",
                "identifier": _derive_identifier(extraction, default_prefix="connection"),
                "value": _connection_string(content, userinfo.start(), userinfo.end()),
                "description": _credential_description(userinfo.group("user") or None),
                "tags": tags,
            }

    # Also when only ``captured_secrets``' wider rule sees a secret ("the
    # password is sunshine"): the branches below store this text as the
    # description, so a text carrying any secret is never one of their kinds.
    # And when a URL reads as carrying a password the userinfo rule did not
    # capture (``_URL_USERINFO_SHAPE``).
    if declined or captured_secrets([content]) or _URL_USERINFO_SHAPE.search(content):
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
    if kind == "credentials":
        from genesis.memory.reference_ops import refuse_trailer_shaped_value

        refuse_trailer_shaped_value(value)
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

#: How many keys an insert-only credential capture tries before giving up: the
#: one it was classified under, then fresh random-tag forms of it. A random tag
#: (24 bits) collides with a taken key only by chance, so a second attempt
#: nearly always lands; the bound only keeps a broken store from looping.
_MAX_KEY_ATTEMPTS = 5
#: The random tag ``_derive_identifier`` appends, stripped before a new one.
_TAG_SUFFIX = re.compile(r" #[0-9a-f]{6}$")


async def _credential_holding(db, domain: str, base: str, value: str) -> str | None:
    """The id of a credential saved under ``base`` or ``base #<tag>`` whose value
    is ``value``, or None. Read through ``resolve_value``, so a row written before
    #2426 (its value still inline, no secret_ref) compares the same way."""
    tagged_len = len(base) + 2 + 6
    rows = await db.execute_fetchall(
        "SELECT * FROM knowledge_units WHERE project_type = 'reference' AND domain = ? "
        "AND (concept = ? OR (length(concept) = ? AND substr(concept, 1, ?) = ?))",
        (domain, base, tagged_len, len(base) + 2, f"{base} #"),
    )
    if not rows:
        return None
    columns = [d[1] for d in await db.execute_fetchall("PRAGMA table_info(knowledge_units)")]
    for raw in rows:
        row = dict(zip(columns, raw, strict=False))
        concept = row.get("concept") or ""
        if concept != base and _TAG_SUFFIX.sub("", concept) != base:
            continue
        if await resolve_value(db, row) == value:
            return row["id"]
    return None


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
    if kind == "credentials":
        login = ref.get("login")
        secret = value[len(login) + 3:] if login and value.startswith(f"{login} / ") else None
        identifier = _identifier_the_write_accepts(
            _with_login(identifier, login, secret),
            value,
            login=login,
            secret=secret,
            secrets=captured_secrets([extraction.content or "", *_entity_texts(extraction)]),
        )

    all_tags = ["reference", kind, *tags, "auto_captured"]
    tags_json = json.dumps(all_tags)

    provenance: dict = {
        "source_doc": f"extraction_job:{source_session_id or 'unknown'}",
        "source_pipeline": "extraction_job",
        "platform": "extraction_job",
    }
    if source_session_id:
        provenance["session_id"] = source_session_id

    # A credential is captured INSERT-ONLY (#2426): a key that already holds a
    # different value is never replaced (owner ruling: a rotated password adds
    # an entry rather than risking a lost one). Two extraction runs can pick
    # the same free key at once; the loser's write changes nothing and comes
    # back as CredentialKeyTaken, and it tries again under the key's random-tag
    # form. Same value under the key: a no-op that returns the existing unit.
    credential = kind == "credentials"
    base = _TAG_SUFFIX.sub("", identifier)
    if credential:
        # The same value already saved under this name or any of its tagged
        # forms is a no-op, so a recapture of a rotated or a pre-#2426 value
        # never mints another tagged copy.
        held = await _credential_holding(db, f"reference.{kind}", base, value)
        if held is not None:
            return held
    for attempt in range(_MAX_KEY_ATTEMPTS):
        if attempt:
            identifier = f"{base} #{secrets_token_hex(3)}"
        body = _format_reference_body(
            kind=kind,
            identifier=identifier,
            description=description,
            value=value,
            tags=tags,
            session_id=source_session_id,
        )
        # The body is what gets stored, and its identifier and tags come from
        # the extraction's entities, which the content check above does not see.
        if contains_card_number(body):
            return None
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
                insert_only=credential,
            )
        except CredentialKeyTaken:
            continue
        except Exception:
            logger.warning(
                "reference extractor ingest failed for kind=%s identifier=<redacted>",
                kind, exc_info=True,
            )
            return None
        break
    else:
        logger.warning(
            "reference extractor found no free key for kind=%s after %d attempts",
            kind, _MAX_KEY_ATTEMPTS,
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


#: Every pattern above that captures a SECRET, with the group holding it.
#: ``captured_secrets`` runs all of them and collects every match, because the
#: classifier stops at the first credential and one text can name several
#: (a password and a backup password in one sentence).
_SECRET_GROUPS = (
    (_CREDENTIAL_PAIR_PATTERN, "pass"),
    (_EMAIL_PASSWORD_PATTERN, "pass"),
    (_ACCOUNT_LIFECYCLE_PATTERN, "pass"),
    (_SINGLE_CREDENTIAL_PATTERN, "value"),
    (_KNOWN_KEY_PREFIX_PATTERN, "token"),
    (_CREDENTIAL_TOKEN_PATTERN, "token"),
    (_ENV_ASSIGNMENT_PATTERN, "val"),
    (_URL_USERINFO_PATTERN, "pw"),
    (_URL_QUERY_SECRET_PATTERN, "val"),
)


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
        secrets |= _url_parameter_secrets(text)
    return secrets


#: A URL as it appears in text: a scheme, then everything to the next space or
#: quote. Bounded scheme, one pass.
_URL_IN_TEXT = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s\"'<>`]+")
#: Where a URL's parameters start (query, fragment, ";" matrix parameters) and
#: how they split into name=value parts.
_URL_PARAMS_START = re.compile(r"[?#;]")
_URL_PARAM_SPLIT = re.compile(r"[?#;&]")
#: A parameter value reads as a secret when it is at least this long and mixes
#: letters with digits ("created_at_desc", "asc", "12345678" do not).
_URL_PARAM_SECRET_MIN = 8


def _url_parameter_secrets(text: str) -> set[str]:
    """Every parameter value of a URL in ``text`` that reads as a secret,
    whatever its name, in the query, the fragment or ";" parameters.

    Judged by the value's shape, never by its name, so a fullwidth, zero-width
    or percent-encoded spelling of "token" (or any other name) changes nothing. A
    value is judged on its decoded, normalised form and returned as written
    (and decoded), so the redaction and the field check both find it. A value
    with no digit under a credential name is caught by
    ``_URL_QUERY_SECRET_PATTERN``; a digitless one under any other name, or a
    token inside the path, is a stated limit."""
    found: set[str] = set()
    for url in _URL_IN_TEXT.findall(text):
        start = _URL_PARAMS_START.search(url)
        if not start:
            continue
        for part in _URL_PARAM_SPLIT.split(url[start.start():]):
            _name, eq, raw = part.partition("=")
            raw = (raw if eq else part).strip().rstrip(".,)")
            if not raw:
                continue
            decoded = raw
            for _ in range(3):
                decoded = unquote(decoded)
            plain = unicodedata.normalize("NFKC", decoded)
            plain = "".join(c for c in plain if unicodedata.category(c) != "Cf")
            if (
                len(plain) >= _URL_PARAM_SECRET_MIN
                and any(c.isdigit() for c in plain)
                and any(c.isalpha() for c in plain)
            ):
                found.add(raw)
                if decoded != raw:
                    found.add(decoded)
    return found


__all__ = [
    "classify_as_reference",
    "ingest_reference_from_extraction",
    "extract_references_from_chunk",
    "captured_secrets",
]
