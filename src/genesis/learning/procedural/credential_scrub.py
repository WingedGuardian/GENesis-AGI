"""Credential scrub for procedure text, applied where every procedure is written (#4859).

Procedures are recalled into any session whose work matches them, so procedure
text is kept free of credential values. Struggle extraction also asks the model
not to embed secrets (``judge.py``); this is the code-level layer under that.

Two layers:
* The repo's recall-first scrubber (``scripts/hooks/secret_scrub.py``, reached
  through the transcript-analytics adapter): key shapes and ``NAME=value`` /
  ``label: value`` forms. It redacts the VALUE only, so the rest of a command
  survives.
* Procedure-specific forms: a password given to ``sshpass -p``, and ``password
  <value>`` in prose, bare or quoted. A ``$VAR``, ``${...}`` or ``$(...)`` names a
  secret and is kept.

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
# sshpass -p takes one argument and it is always the password, so the whole shell
# word is redacted whatever its length: single- or double-quoted (each body stops
# only at its own quote; an unclosed quote runs to the line end) or bare. Other
# options may come first (-e, -v, -P <prompt>, -o <x>; those that take an argument
# keep it), and any whitespace (a non-breaking space, a newline) or a backslash-
# newline continuation may separate -p from its value. A word starting with a
# variable or substitution, bare or double-quoted, names a secret and is kept; a
# single-quoted one is literal text and is redacted.
_GAP = r"(?:[^\S\n]|\\\r?\n)"  # within one shell command line
# One shell word: quoted parts (either quote) and bare characters, joined; an
# option's argument such as -P "Enter password:" is read whole.
_WORD_PART = r"'[^'\n]*'|\"(?:[^\"\\\n]|\\.)*\"|\\."
_WORD = r"(?:" + _WORD_PART + r"|[^\s'\"\\-])(?:" + _WORD_PART + r"|[^\s'\"\\])*"
_SSHPASS = re.compile(
    r"(\bsshpass(?:"
    + _GAP
    + r"+-(?:[Pfdo]"
    + _GAP
    + r"*"
    + _WORD
    + r"|[^\spPfdo]\S*))*"  # disjoint from [Pfdo], so no backtracking blow-up
    + _GAP
    + r"+-p"
    + _GAP
    + r"*(?:\n"
    + _GAP
    + r"*)?)"  # at most one bare newline; the group keeps the two runs unambiguous
    r"(?!\"?[$`(])"
    r"(?:'[^'\n]*'?|\"(?:[^\"\\\n]|\\.)*\"?|[^\s'\"\\]|\\.)+"
)
_LABEL = r"\b(?:with\s+)?(?:password|passwd|pwd|passphrase)\b(?:\s+is|\s*[:=])?"
# Quoted: any 3+ characters between matching quotes. Each body stops only at its
# own quote (in double quotes and backticks, not one escaped with a backslash), so
# a value holding the other kind or an escaped quote is redacted whole. The closing
# quote is required: a prompt such as -P "Enter password:" ends in a quote that
# opens nothing.


def _quoted(quote: str) -> str:
    """One quoted value. A backslash escapes the quote inside double quotes and
    backticks, so that arm is tried first; the plain body follows, so a value
    ending in a backslash (C:\\keys\\) is still caught. Single quotes have no
    escapes in the shell, so their body is plain."""
    plain = r"[^" + quote + r"\n]{3,}" + quote
    if quote == "'":
        return quote + _NOT_A_VALUE + plain
    escaped = r"(?:[^" + quote + r"\\\n]|\\.){3,}" + quote
    return quote + _NOT_A_VALUE + r"(?:" + escaped + "|" + plain + ")"


_PASSWORD_QUOTED = re.compile(
    r"(?i)(" + _LABEL + r"\s*)(?:" + _quoted("'") + "|" + _quoted('"') + "|" + _quoted("`") + ")"
)
# Bare: 6+ characters with a digit or symbol (an identifier like reference_lookup is
# not one), not a path or flag. A leading $ names a variable and is kept; a later $
# counts as a symbol. "password reset" and "password manager" never match;
# "password 2fa-enabled" does, which costs one redacted phrase.
_PASSWORD_BARE = re.compile(
    r"(?i)("
    + _LABEL
    + r"\s+)"
    + _NOT_A_VALUE
    + r"(?![-/~<{*])(?=[^\s'\"`,;)]*[\d!@#$%^&*+=?~])[^\s'\"`,;)]{6,}"
)
# What a string the scrub could not process is stored as.
WITHHELD = "[withheld: credential scrub could not run]"


def _shared_scrub(text: str) -> str:
    from genesis.transcript_analytics import scrub as shared

    if not shared.available():
        raise ScrubFailed("shared scrubber unavailable")
    result, failed = shared.scrub_text(text)
    if failed or result is None:
        raise ScrubFailed("shared scrubber withheld the text")
    return result


def credential_key(key: str) -> bool:
    """True when the shared policy withholds a member's value by its key alone."""
    from genesis.transcript_analytics import scrub as shared

    verdict = shared.credential_key(key)
    if verdict is None:
        raise ScrubFailed("shared scrubber unavailable")
    return verdict


def _credential_key_or_withhold(key: str, found: list[str]) -> bool:
    """credential_key; when the policy cannot answer, the value is withheld too
    (True), so it is never stored unchecked."""
    try:
        return credential_key(key)
    except ScrubFailed:
        found.append("withheld")
        return True


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


# The field names procedure documents are written with: a failure mode
# (operations.record_failure) and a lesson (lesson_shape._LESSON_KEYS, pinned by a
# test). They name fields, never carry a value, so they are kept as they are and a
# value under one is scrubbed as a value; that keeps a document's shape when the
# shared scrubber cannot run (times_hit stays an int).
_SCHEMA_KEYS = frozenset({
    "description", "conditions", "times_hit", "transient",
    "authored_via", "authored_in", "origin", "reviewed", "authored_at",
})


def _scrub_key(key: Any, found: list[str], index: int) -> Any:
    """A dict key, scrubbed. A known field name (``_SCHEMA_KEYS``) is kept; when
    the scrub cannot run, any other key is withheld by position."""
    if not isinstance(key, str):
        return scrub_value(key, found)
    if key in _SCHEMA_KEYS:
        return key
    try:
        result, names = scrub_text(key)
    except ScrubFailed:
        found.append("withheld")
        return f"[withheld key {index + 1}]"
    found += names
    return result


def scrub_value(value: Any, found: list[str]) -> Any:
    """Scrub every string inside ``value`` (str, list, tuple, dict); others pass.

    A dict member under a credential-named key (``password``, ``token``,
    ``api_key``: the shared structured policy) is always redacted; keys are
    scrubbed too (``_scrub_key``). A string the scrub cannot process becomes
    ``WITHHELD`` on its own, so its siblings and the shape around it survive;
    ``found`` then names "withheld". Keys that scrub to the same text are both kept,
    the later one numbered."""
    if isinstance(value, str):
        try:
            result, names = scrub_text(value)
        except ScrubFailed:
            found.append("withheld")
            return WITHHELD
        found += names
        return result
    if isinstance(value, list):
        return [scrub_value(v, found) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v, found) for v in value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            key = _scrub_key(k, found, len(out))
            while key in out:  # two keys scrubbed to the same text: keep both
                key = f"{key} ({len(out) + 1})"
            if (
                isinstance(k, str)
                and v is not None
                and k not in _SCHEMA_KEYS
                and _credential_key_or_withhold(k, found)
            ):
                out[key] = "[redacted]"
                found.append("key")
            else:
                out[key] = scrub_value(v, found)
        return out
    return value
