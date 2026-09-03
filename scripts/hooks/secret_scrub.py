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
    # JWT. The segment repeats are BOUNDED, and the ceiling is the whole point
    # rather than a guess at real token sizes: an unbounded repeat rescans the
    # remaining suffix from every `eyJ` that never completes, which is
    # quadratic on the input that FAILS to match — the common case in ordinary
    # output. MEASURED unbounded: 48/96/192KB of failed candidates took
    # 350/1587/5319ms, so a full 256KB capture crossed the scrub timeout and
    # the entire diagnostic was withheld. 4096 is far above any real JWT
    # segment (a signature is 43 chars, a fat payload a few hundred).
    r"|eyJ[A-Za-z0-9_\-]{8,4096}\.eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}"  # JWT
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


def _armour_line(pattern, line: str) -> bool:
    """True when this line IS an armour delimiter, rather than mentioning one.

    ONE distinction, and it replaces every "does this line look like key
    material" predicate this scanner used to carry. Those predicates were a
    defect GENERATOR rather than a set of bugs: each review round produced
    another shape they answered wrongly — armour with `Proc-Type:` metadata
    between the marker and the body, bodies carrying a log prefix, and a final
    base64 line short enough that no floor could admit it without admitting
    prose too. Answering better was the losing move; the question is gone. The
    body is never inspected at all now, so none of those shapes can matter.

    An armour delimiter ENDS its line (trailing whitespace aside). Whatever the
    capture puts BEFORE it — a log timestamp, a `journalctl` tag, a diff
    marker, a pipe gutter, an indent — is irrelevant, and that is precisely the
    class that kept breaking. A MENTION does not end the line: a grep hit
    closes a quote, source code closes a string, prose runs on into a sentence.
    Checked against both families rather than assumed.
    """
    stripped = line.rstrip()
    if not stripped.endswith("-----"):
        return False
    return bool(pattern.search(stripped))


def _redact_pem_blocks(text: str) -> str:
    """Redact private-key armour, including a key the capture window cut.

    A key missing one delimiter is the NORMAL case here, not an edge: the
    window (`-S -200`) and the byte cap both cut at arbitrary points. In that
    situation the surviving delimiter sits at one END of the capture — a key
    with no END ran off the bottom, a key with no BEGIN started above the top —
    so an unpaired delimiter redacts to that end of the input. Fail-closed, and
    it discards no diagnostic that was not already the key's own.
    """
    if _PEM_MARKER_LITERAL not in text:
        return text  # one substring scan covers every ordinary input
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _armour_line(_PEM_BEGIN_LINE, line):
            if _armour_line(_PEM_END_LINE, line):
                out.append(_REDACTED)  # a whole block written on one line
                i += 1
                continue
            end = next(
                (j for j in range(i + 1, n) if _armour_line(_PEM_END_LINE, lines[j])),
                None,
            )
            out.append(_REDACTED)
            i = n if end is None else end + 1
            continue
        if _armour_line(_PEM_END_LINE, line):
            # An END nothing opened: its BEGIN was above the top of the
            # capture, so everything up to here belongs to the key.
            out = [_REDACTED]
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
