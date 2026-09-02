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

import bisect
import re

_REDACTED = "[REDACTED]"

# ── Detection patterns ───────────────────────────────────────────────────────
#
# THREE files carry credential-detection patterns, and they are NOT all the
# same by design. Enumerated, with what each does on a match:
#   * this file                              — REDACTS captured telemetry
#   * ``src/genesis/security/output_scanner.py``  — FLAGS outbound content
#   * ``src/genesis/memory/reference_extraction.py`` — WRITES a stored reference
# The first two favour recall: over-matching costs a redaction or a flag. The
# third acts on a match by creating a record the user sees, so it favours
# precision and deliberately keeps a NARROWER class (its own comment explains
# the split). Duplicated rather than imported so this hook stays stdlib-only.
# Change a shape here, decide explicitly for the other two — do not paste.
#
# The STRUCTURED class is redaction-only and has no twin: it exists because a
# captured terminal stream carries credentials with no label and no vendor
# prefix, which is not a shape the capture-side classifier needs to recognise.

# Known key prefixes — format-only, near-certain real credentials. The whole
# match is the secret, so it is redacted wholesale.
# The character class after a prefix MUST admit ``-`` and ``_``: modern keys
# namespace themselves with interior separators (``sk-ant-…``, ``sk-or-v1-…``,
# ``sk-proj-…``), so an [A-Za-z0-9]-only class stops at the first hyphen — only
# a few characters in — and never reaches the length floor. The token then
# passes through whole. Prefixes are anchored on the left so a longer ALNUM run
# cannot part-match into one — but `-` is deliberately NOT excluded: a token on
# a git-diff line arrives as `-<token>`/`+<token>` with no separator, and pane
# tails routinely hold diff output. Excluding `-` regressed those to raw
# (measured against the shipping version). Over-matching after a hyphen costs a
# redaction, which is this file's cheap direction.
_KNOWN_KEY_PREFIX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"gh[pousr]_[A-Za-z0-9]{30,}"  # GitHub PAT / OAuth / user / server / refresh
    r"|github_pat_[A-Za-z0-9_]{30,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_\-]{15,}"  # GitLab PAT
    # Hyphens are admitted deliberately: the motivating real token is
    # `sk-ant-oat01-<...>`, which a hyphen-free class cannot match at all.
    # MEASURED residual of that width, stated rather than hidden: a 20+ char
    # hyphenated slug beginning `sk-` is redacted even in prose
    # ("sk-learn-pipeline-preprocessing"). Over 779,899 tracked lines the
    # NATURAL rate of that collision is ZERO — every observed hit is a
    # deliberately adversarial lookalike in a precision-test fixture — and this
    # module favours RECALL by contract (see the header: reference_extraction
    # is the precision-favouring twin). Over-redacting a slug costs one token
    # of a 0600 diagnostic; under-redacting costs the credential.
    r"|sk-[A-Za-z0-9_\-]{20,}"  # OpenAI / Anthropic / OpenRouter / compatible
    r"|gsk_[A-Za-z0-9]{20,}"  # Groq
    r"|nvapi-[A-Za-z0-9_\-]{20,}"  # NVIDIA NIM
    r"|tvly-[A-Za-z0-9_\-]{20,}"  # Tavily
    r"|fc-[A-Za-z0-9]{24,}"  # Firecrawl (longer floor: short prefix)
    r"|hf_[A-Za-z0-9]{20,}"  # HuggingFace
    r"|xai-[A-Za-z0-9]{20,}"  # xAI
    r"|xoxb-[A-Za-z0-9\-]{20,}"  # Slack bot token
    r"|xoxp-[A-Za-z0-9\-]{20,}"  # Slack user token
    r"|AKIA[A-Z0-9]{12,}"  # AWS access key id
    r"|AIza[A-Za-z0-9_\-]{30,}"  # Google API key
    r"|di-[A-Za-z0-9]{20,}"  # DeepInfra
    r"|eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"  # JWT
    r"|csk-[A-Za-z0-9_\-]{20,}"    # Cerebras (the shipping unanchored sk- caught
                                   # these as a SUBSTRING accident; the anchor
                                   # made that a miss, so name them explicitly)
    r"|sk_live_[A-Za-z0-9]{16,}"   # Stripe secret key
    r"|rk_live_[A-Za-z0-9]{16,}"   # Stripe restricted key
    r"|ASIA[A-Z0-9]{12,}"          # AWS STS temporary access key
    r"|npm_[A-Za-z0-9]{30,}"       # npm token
    r"|pypi-[A-Za-z0-9_\-]{40,}"   # PyPI token
    r")",
)

# PEM armour. A private-key BODY is unlabelled base64 no shape rule can own;
# the BEGIN/END armour can. The pane tail is the motivating caller:
# `cat ~/.ssh/id_ed25519` in the last 200 lines is an ordinary accident and the
# highest-value secret on the box.
#
# Scanned LINE BY LINE against literal markers, not with one cross-line regex.
# The previous form anchored a non-greedy `[\s\S]*?` body on BOTH delimiters,
# which failed in both directions at once:
#
#   PERFORMANCE — every BEGIN with no END re-scanned the rest of the input.
#   MEASURED quadratic: 25.5s on a 256KB run of repeated headers, ratio 4.0 at
#   2x input. That is past the capture timeout, so the entire diagnostic was
#   discarded — the failure the timeout exists to bound, reached by ordinary
#   input rather than an attack.
#
#   CORRECTNESS — requiring both delimiters meant a HALF-VISIBLE key never
#   matched at all, and half-visible is the NORMAL case here: the capture
#   window (`-S -200`) and the 256KB input cap both cut at arbitrary positions.
#   A key whose BEGIN scrolled off, or whose END was cut, reached the log with
#   its body intact. Both directions were reproduced on the previous code.
#
# So a marker with no partner redacts the run of key-material lines ADJACENT to
# it and stops at the first ordinary line. Fail-closed on the key material,
# bounded in blast radius: one stray marker in prose (grep output, a docstring)
# cannot blank the surrounding diagnostic. Redaction is LINE-granular, so a
# complete block written on ONE line (an env assignment with escaped newlines)
# takes the whole line — the safe direction for that shape.
_PEM_LABEL = r"(?: [A-Z0-9]{1,32}){0,4}"
# `BLOCK` covers the PGP family (`-----BEGIN PGP PRIVATE KEY BLOCK-----`), the
# armour `gpg --export-secret-keys -a` emits.
_PEM_BEGIN_LINE = re.compile(rf"-----BEGIN{_PEM_LABEL} PRIVATE KEY(?: BLOCK)?-----")
_PEM_END_LINE = re.compile(rf"-----END{_PEM_LABEL} PRIVATE KEY(?: BLOCK)?-----")
_PEM_MARKER_LITERAL = "PRIVATE KEY"
_PEM_BODY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_PEM_MIN_BODY_LINE = 16
# A base64 body's LAST line is short by construction (a 2048-bit RSA key ends
# in a 2-char line), so a run bound alone leaves the key's tail behind.
_PEM_MIN_TAIL_LINE = 4
# Captured output is frequently DECORATED: a log timestamp, a `journalctl`
# prefix, a diff's leading `-`, a pipe gutter. The armour markers still match
# (they are searched, not anchored), but a decorated body line is not pure
# base64, so a column-0 test rejects it and the body survives. MEASURED across
# the decoration class on a real key: timestamp / journalctl / diff-`-` / pipe
# all wrote all 25 body lines out, while plain, indented and diff-`+` did not
# (`+` happens to be a base64 character, and `.strip()` covers indentation) —
# the shapes that pass are an accident of the alphabet, which is not coverage.
# A decorated body is recognised by its TAIL instead: a long unbroken base64
# run ending the line. The 32-char floor is what keeps ordinary prose out —
# a diagnostic line ends in a word, not in 32 unbroken base64 characters.
_PEM_MIN_DECORATED_BODY = 32
# RFC 1421 / OpenSSL legacy-encrypted armour (`openssl genrsa -aes128`,
# `ssh-keygen -m PEM -N`) puts `Proc-Type:` / `DEK-Info:` and a blank line
# BETWEEN the marker and the base64, so the body does not start on the next
# line. MEASURED before this bound existed: 14 of 14 body lines of a real
# encrypted key were written out, because the walk stopped at `Proc-Type:`.
# Armour METADATA lines, as a CLOSED set of the header names RFC 1421 and the
# OpenPGP armour actually define. A closed set on purpose: an open-set shape
# rule ("word followed by a colon") also matches a journalctl prefix, which
# would classify a decorated BODY line as metadata and skip straight past the
# key. Base64 cannot contain any of these literals, so matching them anywhere
# in the line is decoration-tolerant without being over-broad.
_PEM_ARMOUR_HEADERS = (
    "Proc-Type:",
    "DEK-Info:",
    "Version:",
    "Comment:",
    "Charset:",
    "MessageID:",
    "Hash:",
)
# How far past a marker to look for the body. Covers the RFC 1421 header block
# (two headers plus a blank line) with room to spare, and bounds how much a
# marker with no body after it can ever consume.
_PEM_BODY_LOOKAHEAD = 8
# Two UNRELATED markers (the classic case: one `grep` hit naming a BEGIN and
# another naming an END) must not pair up and swallow everything between them.
# A distance cap alone does NOT separate them — grep hits land close together —
# so the discriminator is what lies BETWEEN: a real block's interior is base64,
# optionally preceded by RFC 1421 headers and blank lines. One ordinary prose
# line in the span means these are two mentions, not one block, and each is
# then treated on its own (marker redacted, adjacent key material redacted,
# surrounding diagnostic kept). MEASURED before this check existed: a 9-line
# grep result collapsed to a single [REDACTED], destroying 6 diagnostic lines.
# The cap bounds the interior scan, keeping the pass linear; a real block is
# far shorter anyway (RSA-4096 ~52 lines, OPENSSH ~48).
_PEM_MAX_BLOCK_LINES = 200


def _is_key_material(line: str) -> bool:
    """True for a base64 body line — the shape a PEM key's payload takes.

    Deliberately narrow: anything with a space, punctuation or a short length
    is an ordinary log line and STOPS the redaction run.
    """
    stripped = line.strip()
    if len(stripped) < _PEM_MIN_BODY_LINE:
        return False
    return all(ch in _PEM_BODY_CHARS for ch in stripped)


def _is_decorated_key_material(line: str) -> bool:
    """True when the line ENDS in a long base64 run (a prefixed body line)."""
    stripped = line.rstrip()
    run = 0
    for ch in reversed(stripped):
        if ch not in _PEM_BODY_CHARS:
            break
        run += 1
    return run >= _PEM_MIN_DECORATED_BODY


def _is_key_tail(line: str) -> bool:
    """A SHORT pure-base64 line — the final line of a key body."""
    stripped = line.strip()
    return (
        _PEM_MIN_TAIL_LINE <= len(stripped) < _PEM_MIN_BODY_LINE
        and all(ch in _PEM_BODY_CHARS for ch in stripped)
    )


def _is_armour_metadata(line: str) -> bool:
    """An armour header line (`Proc-Type:`, `DEK-Info:`, …) — not body."""
    stripped = line.strip()
    return any(header in stripped for header in _PEM_ARMOUR_HEADERS)


def _body_line(line: str) -> bool:
    """A key-body line, decorated or not — and never armour metadata."""
    if _is_armour_metadata(line):
        return False
    return _is_key_material(line) or _is_decorated_key_material(line)


def _pem_body_end(lines: list[str], start: int, n: int) -> int:
    """Index just past the key body beginning at or after ``start``.

    The body does not always start on the very next line: RFC 1421 armour puts
    `Proc-Type:` / `DEK-Info:` and a blank line first, and any of those lines
    may carry the same decoration as the rest of the capture. Rather than model
    each of those shapes — the column-0 assumption is exactly what let decorated
    bodies through — look AHEAD a bounded number of lines for the first line
    that is recognisably key material, and redact from the marker through the
    run it starts. If no body turns up within the bound the marker stands alone
    and costs exactly its own line, which is what keeps a lone marker in prose
    from taking the surrounding diagnostic with it.
    """
    limit = min(n, start + _PEM_BODY_LOOKAHEAD)
    probe = start
    while probe < limit and not _body_line(lines[probe]):
        # Deliberately NOT "bail on the first unrecognised line": under
        # decoration a blank line is not blank (it carries the prefix), and a
        # rule that models which separators are allowed is the same column-0
        # assumption one level up. The BOUND is the protection instead — a
        # marker with no body within it consumes only itself.
        probe += 1
    if probe >= limit:
        return start
    k = probe
    while k < n and _body_line(lines[k]):
        k += 1
    if k < n and _is_key_tail(lines[k]):
        k += 1
    return k


def _is_block_interior(lines: list[str], start: int, stop: int) -> bool:
    """True when every line in ``[start, stop)`` belongs inside PEM armour."""
    for idx in range(start, stop):
        stripped = lines[idx].strip()
        if not stripped or _is_armour_metadata(lines[idx]):
            continue
        if (
            _is_key_material(lines[idx])
            or _is_key_tail(lines[idx])
            or _is_decorated_key_material(lines[idx])
        ):
            continue
        return False
    return True


def _redact_pem_blocks(text: str) -> str:
    """Redact PEM private keys, including half-visible ones. Linear."""
    if _PEM_MARKER_LITERAL not in text:
        return text  # one substring scan covers every ordinary input
    lines = text.split("\n")
    # Every END position, found once, so a BEGIN never re-scans the tail.
    end_at = [i for i, ln in enumerate(lines) if _PEM_END_LINE.search(ln)]
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        begin = _PEM_BEGIN_LINE.search(line)
        if begin:
            if _PEM_END_LINE.search(line, begin.end()):
                out.append(_REDACTED)  # whole block on one line
                i += 1
                continue
            after = bisect.bisect_right(end_at, i)
            if (
                after < len(end_at)
                and end_at[after] - i <= _PEM_MAX_BLOCK_LINES
                and _is_block_interior(lines, i + 1, end_at[after])
            ):
                out.append(_REDACTED)  # matched pair: redact through the END
                i = end_at[after] + 1
                continue
            # No END, or one too far away to belong to this marker: redact the
            # marker plus whatever key body actually follows it.
            out.append(_REDACTED)
            i = _pem_body_end(lines, i + 1, n)
            continue
        if _PEM_END_LINE.search(line):
            # An END we never opened: the BEGIN was clipped above, so the body
            # is whatever key material we have already emitted.
            if out and _is_key_tail(out[-1]):
                out.pop()
            while out and _body_line(out[-1]):
                out.pop()
            out.append(_REDACTED)
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)

# Structured credentials — identified by SHAPE, not by a vendor prefix. Both
# carry their secret as a positional path/field component, so no label pattern
# ever sees them and no prefix class can match them.
_STRUCTURED_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    # Discord webhook: the trailing path segment IS the credential.
    # (?i:...) scoped to this alternative: schemes and hosts are
    # case-insensitive (RFC 3986) and real logs carry both forms. Scoped rather
    # than global so the alternatives below keep their exact casing semantics.
    r"(?i:https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/)"
    r"\d{15,}/[A-Za-z0-9_\-]{40,}"
    # Telegram bot token: <bot-id>:<secret>. The anchor rejects only a preceding
    # DIGIT — enough to stop a match starting mid-way through a longer digit
    # run, which is all it needs to do. It must NOT reject a preceding letter:
    # the form Genesis itself builds is ``…/bot<id>:<secret>``, where the id is
    # preceded by "bot", and a word-boundary-style anchor silently passes the
    # whole token through. The floor is set from the vendor's DOCUMENTED
    # example (34 chars), not from one locally observed token, so a shorter
    # legitimate token cannot slip under it.
    r"|(?<!\d)\d{6,}:[A-Za-z0-9_\-]{30,}"
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

# password/passphrase/pin values — keep the label, redact the value. A quoted
# value is captured whole (the quote alternatives let the value contain spaces);
# an unquoted value stops at whitespace/separator. The left anchor is
# ``(?<![A-Za-z])`` (not ``\b``) so an underscore-joined label still matches —
# e.g. the ``DB_PASSWORD`` form has no word boundary between ``_`` and ``P``.
_SINGLE_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:password|pass(?:word)?|pwd|passphrase|passcode|pin)"
    r"\s*(?:is\s+|[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]{4,})",
    re.IGNORECASE,
)

# Credentials embedded in a URL's userinfo section (the user/password pair that
# can precede an "@" host separator). Only fires when an "@" follows, so a plain
# host-and-port URL never matches.
# Only the SCHEME is bounded, and that bound carries the whole perf win:
# MEASURED on a 40KB run, 2,788ms -> 6.6ms from the scheme bound alone. The span
# after `://` was previously capped at 253 as if it were a hostname — it is the
# USERINFO (the username before the `:`), so that cap bought nothing and simply
# stopped long usernames being redacted. Unbounded here, same 6.6ms.
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]{0,32}://[^:/@\s]+:)(?P<pw>[^@/\s]+)(?P<at>@)",
)

# .env-style UPPER_SNAKE=value. The bare pattern over-redacts (PYTHONPATH=…,
# EDITOR=…), so it is gated to keys whose NAME signals a secret. A quoted value
# is captured whole (so a multi-word secret like KEY='a b c' is not left with
# its tail exposed after the first space); an unquoted value is a 6+ run of
# non-space so short non-secret values aren't touched.
# LINEAR BY CONSTRUCTION, on every character class — a perf property measured
# on one input shape is not a perf property (the first fix here benchmarked an
# all-alphanumeric run, the exact class its anchor excluded, and was quadratic
# on underscore-bearing input; TestScrubIsLinearAcrossInputShapes now spans the
# classes). Three pieces:
#   * `(?<![A-Za-z0-9])` — a run of admitted characters offers only the starts
#     the lookbehind allows, not every offset;
#   * the BOUNDED repeats `_{0,4}` and `{2,512}` — plain syntax, deliberately
#     NOT possessive. The ceiling alone is what removes the quadratic: it caps
#     the work a failed `=` can spend re-scanning the key, so cost is O(N x 512)
#     rather than O(N^2). MEASURED whole-blob, ceilings kept, no possessives:
#     39KB/78KB/156KB = 349/705/1441ms — exactly linear; a realistic 200x200
#     pane tail is 80ms and the observer hook's 2000-char cap is 15ms, against
#     a 10s scrub timeout. Possessive quantifiers (`*+`, `{m,n}+`) would shave
#     the constant but are Python 3.11+ ONLY (bpo-433030), and this module is
#     imported by whatever bare `python3` the install has — so they made the
#     scrubber fail to import on an older system interpreter, withholding every
#     captured tail. They were removed for that reason and the removal is
#     MEASURED semantically free: 0 differing outputs over 20,270 strings.
#     Pinned by TestPatternsStayPortable;
#   * the 512 ceiling — bounds the per-start scan. The ceiling CANNOT reproduce
#     the old slide-forward fail-open for ordinary names (the lookbehind blocks
#     an alnum-preceded restart, so an over-ceiling key is a CLEAN MISS, not a
#     silently disabled hint). ONE residual, accepted and pinned by
#     TestEnvKeyCeilingResidual: a >512-char key with INTERIOR underscores can
#     match a late suffix without the hint — the lookbehind deliberately admits
#     a start after `_`, which is what keeps `2FA_TOKEN=` catchable. Longest
#     real key observed on an install: 31 chars.
#
# The lookbehind admits a LEADING underscore for recall (`_MY_SECRET=…`), and
# the quoted-value floor is what removes the historical noise: an empty or
# trivial quoted value (`_OAUTH_SRC=""`) is not a secret.
#
# The bound it replaces was a FAIL-OPEN, and a subtle one: capping the key did
# not merely miss long names, it slid the match FORWARD past the name's prefix,
# so `_SECRET_KEY_HINT` no longer saw SECRET_/TOKEN_ and the value stopped being
# redacted at all. Bounding a repeat that feeds a downstream semantic check can
# break the check rather than just the reach.
_ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<key>_{0,4}[A-Z][A-Z0-9_]{2,512})"
    r"(?P<sep>\s*=\s*)"
    r"(?P<val>\"[^\"]{6,}\"|'[^']{6,}'|[^\s]{6,})",
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
        text = _redact_pem_blocks(text)
        text = _KNOWN_KEY_PREFIX_PATTERN.sub(_REDACTED, text)
        text = _STRUCTURED_CREDENTIAL_PATTERN.sub(_REDACTED, text)
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
