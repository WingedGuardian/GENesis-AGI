"""Credential shapes shared by the detectors that redact or flag them.

One ordered table of credential shapes, as data: a name and a regex fragment
per shape. Stdlib only, with no ``genesis`` imports and nothing newer than
Python 3.10 syntax in a pattern, so a script that cannot import the package can
load this file by path (as ``scripts/browser.py`` loads ``card_shapes.py``).

Readers, and what each does on a match:

* ``genesis.security.output_scanner`` FLAGS outbound content (an external send
  is quarantined) and REDACTS the span in text kept for a trusted reader. It
  adopts every shape here except the four families it already owns in a
  tighter form (its ``_LOCAL_FAMILIES``).
* ``scripts/hooks/secret_scrub.py`` REDACTS captured telemetry. It still holds
  its own copy of these two patterns and is to load this table instead. Until
  it does, ``tests/test_security/test_credential_shapes.py`` holds the two
  copies equal, character for character and flag for flag, so a shape changed
  in either file alone fails CI.

``genesis.memory.reference_extraction`` is NOT a reader. It writes a stored
reference on a match, so it keeps a deliberately narrower prefix set of its
own; do not derive it from this table or fold it in.

The table is recall-first. Each width was chosen where over-matching costs a
redaction, and the measured reasoning for each one (why a body class admits
``-``, why the JWT repeats are bounded, why the bot-token anchor rejects only a
digit) sits beside the scrubber's copy. In the outbound scanner an over-match
costs a quarantined send, so adding or widening an entry here changes what is
refused outbound: measure false positives on real outbound text first.
"""

from __future__ import annotations

import re

# A prefix must not continue a longer alphanumeric run. ``-`` is deliberately
# allowed before it: a token on a diff line arrives as ``-<token>``.
PREFIX_ANCHOR = r"(?<![A-Za-z0-9_])"

# Vendor key prefixes: the whole match is the secret. Order is the scrubber's
# alternation order and is part of the pinned parity.
KEY_PREFIX_SHAPES: tuple[tuple[str, str], ...] = (
    ("github", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("github_fine_grained", r"github_pat_[A-Za-z0-9_]{30,}"),
    ("gitlab", r"glpat-[A-Za-z0-9_\-]{15,}"),
    ("openai_compatible", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("groq", r"gsk_[A-Za-z0-9]{20,}"),
    ("nvidia", r"nvapi-[A-Za-z0-9_\-]{20,}"),
    ("tavily", r"tvly-[A-Za-z0-9_\-]{20,}"),
    ("firecrawl", r"fc-[A-Za-z0-9]{24,}"),
    ("huggingface", r"hf_[A-Za-z0-9]{20,}"),
    ("xai", r"xai-[A-Za-z0-9]{20,}"),
    ("slack_bot", r"xoxb-[A-Za-z0-9\-]{20,}"),
    ("slack_user", r"xoxp-[A-Za-z0-9\-]{20,}"),
    ("aws_access_key", r"AKIA[A-Z0-9]{12,}"),
    ("google", r"AIza[A-Za-z0-9_\-]{30,}"),
    ("deepinfra", r"di-[A-Za-z0-9]{20,}"),
    # Bounded repeats: unbounded, every start that never completes rescans the
    # rest of the input.
    ("jwt", r"eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{2,4096}\.[A-Za-z0-9_\-]{8,4096}"),
    ("cerebras", r"csk-[A-Za-z0-9_\-]{20,}"),
    ("stripe_secret", r"sk_live_[A-Za-z0-9]{16,}"),
    ("stripe_restricted", r"rk_live_[A-Za-z0-9]{16,}"),
    ("aws_sts", r"ASIA[A-Z0-9]{12,}"),
    ("npm", r"npm_[A-Za-z0-9]{30,}"),
    ("pypi", r"pypi-[A-Za-z0-9_\-]{40,}"),
)

# Credentials identified by structure, with no vendor prefix: the secret is a
# positional part of a URL or an ``<id>:<secret>`` pair.
STRUCTURED_SHAPES: tuple[tuple[str, str], ...] = (
    (
        "discord_webhook",
        r"(?i:https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/)"
        r"\d{15,}/[A-Za-z0-9_\-]{40,}",
    ),
    ("telegram_bot_token", r"(?<!\d)\d{6,}:[A-Za-z0-9_\-]{30,}"),
)

KNOWN_KEY_PREFIX_PATTERN = re.compile(
    PREFIX_ANCHOR + "(?:" + "|".join(fragment for _, fragment in KEY_PREFIX_SHAPES) + ")"
)
STRUCTURED_CREDENTIAL_PATTERN = re.compile(
    "(?:" + "|".join(fragment for _, fragment in STRUCTURED_SHAPES) + ")"
)
