"""Credential scrub for procedure text, applied where every procedure is written (#4859).

Procedures are recalled into any session whose work matches them, so a credential
in one is quoted onward. Struggle extraction tells the model never to embed secrets
(``judge.py``), and nothing in code enforced it: 642d8142-class rows held a login in
plaintext.

Two layers:
* The repo's recall-first scrubber (``scripts/hooks/secret_scrub.py``, reached
  through the transcript-analytics adapter): key shapes and ``NAME=value`` /
  ``label: value`` forms. It redacts the VALUE only, so the rest of a command
  survives.
* Two forms it misses, which procedures actually used (MEASURED 2026-11-17: 25 and
  31 of 7,529 procedures): ``sshpass -p <value>``, and ``password <value>`` in prose,
  bare or quoted. A ``$VAR``, ``${...}`` or ``$(...)`` names a secret and is kept.

The scrub runs on parsed values (strings inside lists and dicts), never on JSON
text. When the shared scrubber cannot run, ``ScrubFailed`` tells the caller to
withhold that field rather than store it raw.
"""

from __future__ import annotations

import re
from typing import Any


class ScrubFailed(Exception):
    """The shared scrubber could not run; the caller withholds the text."""


_NOT_A_VALUE = r"(?![$`(])"  # a shell variable or substitution names a secret
_SSHPASS = re.compile(
    r"(\bsshpass\s+-p\s*)"
    r"(?:(?P<q>['\"])" + _NOT_A_VALUE + r"[^'\"\n]+(?P=q)"
    r"|" + _NOT_A_VALUE + r"[^\s'\"]{3,})"
)
_LABEL = r"\b(?:with\s+)?(?:password|passwd|pwd|passphrase)\b(?:\s+is|\s*[:=])?"
# Quoted: any 3+ characters between matching quotes.
_PASSWORD_QUOTED = re.compile(
    r"(?i)(" + _LABEL + r"\s*)(?P<q>['\"`])" + _NOT_A_VALUE + r"[^'\"`\n]{3,}(?P=q)"
)
# Bare: 6+ characters with a digit or symbol (an identifier like reference_lookup is
# not one), not a path or flag. "password reset" and "password manager" never match;
# "password 2fa-enabled" does, which costs one redacted phrase.
_PASSWORD_BARE = re.compile(
    r"(?i)("
    + _LABEL
    + r"\s+)"
    + _NOT_A_VALUE
    + r"(?![-/~<{*])(?=[^\s'\"`,;)]*[\d!@#%^&*+=?~])[^\s'\"`,;)]{6,}"
)


def _shared_scrub(text: str) -> str:
    from genesis.transcript_analytics import scrub as shared

    if not shared.available():
        raise ScrubFailed("shared scrubber unavailable")
    result, failed = shared.scrub_text(text)
    if failed or result is None:
        raise ScrubFailed("shared scrubber withheld the text")
    return result


def scrub_text(text: str) -> tuple[str, list[str]]:
    """Redact credential spans; return the text and the names of what matched.
    Raises ScrubFailed when the shared scrubber cannot run."""
    found: list[str] = []
    for name, pattern in (
        ("sshpass", _SSHPASS),
        ("password", _PASSWORD_QUOTED),
        ("password", _PASSWORD_BARE),
    ):
        text, count = pattern.subn(r"\1[redacted]", text)
        if count:
            found.append(name)
    shared = _shared_scrub(text)
    if shared != text:
        found.append("shared")
    return shared, found


def scrub_value(value: Any, found: list[str]) -> Any:
    """Scrub every string inside ``value`` (str, list, tuple, dict); others pass."""
    if isinstance(value, str):
        result, names = scrub_text(value)
        found += names
        return result
    if isinstance(value, list):
        return [scrub_value(v, found) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v, found) for v in value)
    if isinstance(value, dict):
        return {k: scrub_value(v, found) for k, v in value.items()}
    return value
