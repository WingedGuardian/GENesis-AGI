#!/usr/bin/env python3
"""Stdlib-only credential/secret scrubber for CC hooks.

Hooks that persist tool activity (session telemetry, audit trails) can
incidentally capture secrets — a ``cat secrets.env``, an ``export API_KEY=…``,
a token echoed in command output. Once such text lands in a per-session JSONL
or the DB it flows into memory extraction and proactive recall, i.e. it leaks
into places with a much longer life than the transcript. This module redacts
the high-confidence secret shapes BEFORE any such capture.

Design constraints:
- **Stdlib only, import-light.** Hooks run on a <50ms budget and must not
  import ``genesis`` (heavy) or touch the network. So the detection patterns
  are copied from ``src/genesis/memory/reference_extraction.py`` rather than
  imported. Keep the two in rough sync — this file favors RECALL (redact a
  little too eagerly) because a missed secret is worse than a redacted
  non-secret in low-stakes telemetry, whereas the extraction classifier favors
  precision (it fabricates references on false positives).
- **Fail-safe, never crash.** ``scrub`` returns a fully-withheld placeholder if
  its own regex work raises, never the raw input; ``is_secret_path`` /
  ``command_touches_secret`` return conservative booleans.
- **One secret-filename vocabulary.** ``is_secret_path`` is the single source of
  truth; ``command_touches_secret`` reuses it (tokenizes the command and asks
  ``is_secret_path`` per token) so the two can never drift apart.

Two layers callers combine:
1. ``is_secret_path`` / ``command_touches_secret`` — flag inputs whose *bodies*
   are secret by nature (``.env``, ``*.pem``, ``id_ed25519``, ``cat secrets``),
   so the caller can skip capturing the body entirely.
2. ``scrub`` — redact inline secret shapes from whatever text IS captured.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# ── Detection patterns (mirror of reference_extraction.py) ───────────────────

# Known key prefixes — format-only, near-certain real credentials. The whole
# match is the secret, so it is redacted wholesale.
_KNOWN_KEY_PREFIX_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9]{30,}"  # GitHub personal access token
    r"|gho_[A-Za-z0-9]{30,}"  # GitHub OAuth token
    r"|sk-[A-Za-z0-9]{20,}"  # OpenAI / Anthropic
    r"|xoxb-[A-Za-z0-9\-]{20,}"  # Slack bot token
    r"|xoxp-[A-Za-z0-9\-]{20,}"  # Slack user token
    r"|AKIA[A-Z0-9]{12,}"  # AWS access key id
    r"|AIza[A-Za-z0-9_\-]{30,}"  # Google API key
    r"|di-[A-Za-z0-9]{20,}"  # DeepInfra
    r")",
)

# Labeled API/access/secret tokens — keep the label, redact the value.
# ``secret[_\s-]?access[_\s-]?key`` is listed explicitly (before the shorter
# ``secret[_\s-]?key``) so the AWS secret-key label is caught.
_CREDENTIAL_TOKEN_PATTERN = re.compile(
    # (?<![A-Za-z]) (not \b) so an underscore-joined prefix still matches, e.g.
    # the ``aws_secret_access_key`` form used in ~/.aws/credentials.
    r"(?<![A-Za-z])(?:api[_\s-]?key|access[_\s-]?token|bearer[_\s-]?token|"
    r"secret[_\s-]?access[_\s-]?key|secret[_\s-]?key|auth[_\s-]?token|"
    r"refresh[_\s-]?token|api[_\s-]?secret|private[_\s-]?key)"
    r"\s*(?:is\s+|[:=]\s*)(?P<token>[A-Za-z0-9_\-\./+]{16,})",
    re.IGNORECASE,
)

# password/passphrase/pin values — keep the label, redact the value.
_SINGLE_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:password|pass(?:word)?|pwd|passphrase|passcode|pin)"
    r"\s*(?:is\s+|[:=]\s*)"
    r"(?P<value>[^\s,;]{4,})",
    re.IGNORECASE,
)

# Credentials embedded in a URL userinfo: scheme://user:PASSWORD@host.
# Only fires when an ``@`` follows, so ``host:port/path`` URLs never match.
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]+:)(?P<pw>[^@/\s]+)(?P<at>@)",
)

# .env-style UPPER_SNAKE=value. The bare pattern over-redacts (PYTHONPATH=…,
# EDITOR=…), so it is gated to keys whose NAME signals a secret.
_ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[A-Z][A-Z0-9_]{2,})"
    r"(?P<sep>\s*=\s*)"
    r"(?P<val>[^\s]{6,})",
)
_SECRET_KEY_HINT = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|AUTH|API|CRED|PRIVATE", re.IGNORECASE
)

# Files whose CONTENTS are secret by nature. ONE vocabulary, anchored per path
# segment; reused by command_touches_secret via tokenisation.
_SECRET_FILENAME_CORE = (
    r"\.env(?:\.[\w.-]+)?"  # noqa: S105 - a regex of secret FILENAMES, not a password
    r"|secrets?\.env"
    r"|[\w.-]*\.pem"
    r"|[\w.-]*\.key"
    r"|[\w.-]*\.(?:p12|pfx|jks)"
    r"|[\w.-]*\.token"
    r"|id_(?:rsa|ed25519|ecdsa|dsa)"
    r"|\.netrc|\.pgpass|\.htpasswd|\.npmrc|\.pypirc|\.git-credentials"
    r"|\.kube/config"
    r"|credentials(?:\.json)?"
)
_SECRET_FILE_RE = re.compile(rf"(?:^|/)(?:{_SECRET_FILENAME_CORE})$", re.IGNORECASE)
# Non-secret templates that share a secret-ish name.
_TEMPLATE_SUFFIX_RE = re.compile(r"\.(?:example|sample|template|dist)$", re.IGNORECASE)
# Shell metacharacters that separate command tokens.
_CMD_TOKEN_SPLIT = re.compile(r"[\s;|&()<>'\"=]+")


def _redact_value_group(pattern: re.Pattern, text: str, group: str) -> str:
    """Replace only the captured ``group`` (the secret value) within each match,
    by SPAN — never a substring search, which could redact a label that happens
    to equal the value (``password: password``)."""

    def _repl(m: re.Match) -> str:
        start, end = m.span(group)
        base = m.start()
        return m.group(0)[: start - base] + _REDACTED + m.group(0)[end - base :]

    return pattern.sub(_repl, text)


def _redact_env(text: str) -> str:
    def _repl(m: re.Match) -> str:
        if _SECRET_KEY_HINT.search(m.group("key")):
            return f"{m.group('key')}{m.group('sep')}{_REDACTED}"
        return m.group(0)

    return _ENV_ASSIGNMENT_PATTERN.sub(_repl, text)


def scrub(text: str) -> str:
    """Redact high-confidence secret shapes from ``text``.

    Fail-safe: on any internal error returns a placeholder, never the raw input.
    """
    if not text:
        return text
    try:
        text = _KNOWN_KEY_PREFIX_PATTERN.sub(_REDACTED, text)
        text = _redact_value_group(_CREDENTIAL_TOKEN_PATTERN, text, "token")
        text = _redact_value_group(_SINGLE_CREDENTIAL_PATTERN, text, "value")
        text = _redact_value_group(_URL_CREDENTIAL_PATTERN, text, "pw")
        text = _redact_env(text)
        return text
    except Exception:  # noqa: BLE001 - never leak raw text on a scrub failure
        return "[scrub-error: content withheld]"


def is_secret_path(path: str) -> bool:
    """True if ``path`` names a file whose contents are secret by nature."""
    if not path:
        return False
    base = path.rsplit("/", 1)[-1]
    if _TEMPLATE_SUFFIX_RE.search(base):
        return False
    if _SECRET_FILE_RE.search(path):
        return True
    low = base.lower()
    return "secret" in low or "credential" in low


def command_touches_secret(command: str) -> bool:
    """True if any token of a shell command names a secret-bearing file — its
    output or edit body is likely to contain secrets, so the caller should not
    capture it. Tokenises on shell metacharacters and reuses ``is_secret_path``
    so the two never diverge."""
    if not command:
        return False
    return any(tok and is_secret_path(tok) for tok in _CMD_TOKEN_SPLIT.split(command))


def scrub_info(info: dict) -> dict:
    """Scrub every string value in a flat dict (in place) and return it."""
    for k, v in list(info.items()):
        if isinstance(v, str):
            info[k] = scrub(v)
    return info
