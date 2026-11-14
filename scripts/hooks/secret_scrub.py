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
2. ``scrub`` — redact inline secret shapes from whatever text IS captured:
   vendor key prefixes, PEM armour, structured tokens, credential headers and
   bearer values, labelled values (``password: …``, ``"api_key": …``,
   ``API_TOKEN=…``), URL userinfo passwords, and command-line credential flags.

Command-line credential flags, run LAST over what the other arms left:

* After a flag whose name ends in a credential label (``--password``,
  ``--passwd``, ``--token``, ``--secret``, ``--api-key``, ``--http-password``,
  ``-pass``, …) and a separator (whitespace, ``=``, ``:``, or ``is``), the whole
  shell word is withheld: quoted segments, escapes and adjacent quoted pieces
  are one word. A quote that opens the value and never closes withholds to the
  end of the text (to the end of the line when the flag sits inside a quote); a
  value starting with escaped quoting (a JSON-encoded command, any depth)
  withholds to the end of the line. A flag named inside a quoted argument ends
  its value at that quote. Kept: ``--password-file``-style names, a
  whitespace-separated value starting with ``-``, and an empty ``""``.
* After ``-u`` (alone or ending a grouped short flag of up to four lowercase
  letters), ``--user`` or ``--proxy-user``, everything after the first colon of
  the word is withheld and the user name kept. Kept: a value with no colon, a
  ``+`` format (``date -u +%H:%M``), and a ``/``-leading second half after a
  short ``-u`` on ``rsync`` or ``scp`` (a remote path).

Stated residuals, none of which an earlier version redacted either: an apostrophe
earlier on the line can make a quoted value read as no value; a value joined
past a closing quote (``"--token=a"b``) is cut at the quote; grouped short flags
with an uppercase letter or more than four letters (``-sSu``); a quoted value
spanning lines inside an enclosing quote; a mid-word quote with no partner on
its line ends the word; ``-p`` short password flags (``mysql -p…``,
``sshpass -p``); argv lists (``['--password', 'x']``); ``-U``; a ``-u`` user
name starting with ``+``.
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
    # The PAYLOAD segment is a generic base64url run, floor 2: requiring it to
    # start `eyJ` assumed the JSON claims always encode to that prefix, and an
    # empty claims object (`{}` → `e30`) or leading JSON whitespace (` {` →
    # `IH…`) are valid signed JWTs that assumption let through whole. Only the
    # HEADER keeps the `eyJ` anchor — it is what bounds the match starts, so
    # the failed-candidate cost profile is unchanged (MEASURED after the
    # change: 256KB of repeated failed candidates ≈ 0.3s, linear). Precision
    # residual, measured the same way as the pattern trades above: a dotted
    # token like `eyJournalfile.name.extension123` (8+ char base64url runs
    # around both dots) now redacts; over 825,937 tracked lines the only line
    # the widened payload segment newly matches is this comment's own example,
    # and over-redaction is this module's cheap direction.
    r"|eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{2,4096}\.[A-Za-z0-9_\-]{8,4096}"  # JWT
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
# `BLOCK` covers the PGP family (`-----BEGIN PGP PRIVATE KEY BLOCK-----`), the  # pragma: allowlist secret
# armour `gpg --export-secret-keys -a` emits. The pragma above is a
# false-positive suppression, not a real key: detect-secrets' PrivateKeyDetector
# matches the ARMOUR TEXT, so naming the shape this module exists to redact is
# itself a "finding" and turned the blocking leak-detector job red. The literal
# stays — it is the whole documentation value of the line — and it is the only
# occurrence that needs the pragma, because every other spelling in this file
# interpolates `_PEM_LABEL` and so is not a contiguous match.
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
        if _PEM_BEGIN_LINE.search(line) and _PEM_END_LINE.search(line):
            # A whole block on ONE line — plain armour, or serialized with
            # trailing syntax (a JSON log field, an escaped env value), where
            # neither marker ends the physical line and the mention test above
            # would wave the key through. BOTH markers in one line is a closed
            # test a mention does not meet in the pinned shapes (a grep hit or
            # prose names one marker per line); the rare line that names both
            # (`grep -e BEGIN… -e END…`) is lost whole — the cheap direction,
            # pinned as a decision in the tests.
            out.append(_REDACTED)
            i += 1
            continue
        if _armour_line(_PEM_BEGIN_LINE, line):
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
_TOKEN_LABEL = (  # noqa: S105 - credential label regex, not a value
    r"(?<![A-Za-z])(?:api[_\s-]?key|access[_\s-]?token|bearer[_\s-]?token|"  # noqa: S105
    r"secret[_\s-]?access[_\s-]?key|secret[_\s-]?key|auth[_\s-]?token|"
    r"refresh[_\s-]?token|api[_\s-]?secret|private[_\s-]?key)"
)
_CREDENTIAL_TOKEN_PATTERN = re.compile(
    # (?<![A-Za-z]) (not \b) so an underscore-joined prefix still matches, e.g.
    # the ``aws_secret_access_key`` form used in ~/.aws/credentials.
    _TOKEN_LABEL + r"\s*(?:is\s+|[:=]\s*)(?P<token>[A-Za-z0-9_\-\./+]{16,})",
    re.IGNORECASE,
)

# HTTP credential headers. Their VALUE is the secret, whatever its shape: a
# scheme plus a token (``Bearer x``, ``Basic YTpi``), a parameter list
# (``Digest username=..., response=...``, ``AWS4-HMAC-SHA256 Credential=...``)
# or cookie pairs (``SID=...; HttpOnly``). Everything from the separator to the
# END OF THE LINE is withheld, and quoting is deliberately not read: three
# review rounds each found a quoting shape (a curl argument with escaped
# quotes, an escaped quote inside a JSON value, double-encoded JSON) where a
# value grammar stopped early and let the credential through. Not finding the
# end of the value is what removes that whole class. The cost is that a
# one-line record (a serialized tool response) loses everything after such a
# header, the cheap direction. The label may sit in a quoted or escaped-quoted
# key (``'authorization': ...``, ``\"cookie\": ...``). ``credential_label_kind``
# also treats these names as token labels, so a structured member is withheld
# whatever its value looks like.
_HEADER_LABEL = r"(?<![A-Za-z])(?:(?:proxy[_\s-]?)?authorization|(?:set[_\s-]?)?cookie)"
_HEADER_PATTERN = re.compile(
    # The value may start on the NEXT line (pretty-printed JSON puts it there),
    # so the gap after the separator may hold one line break.
    _HEADER_LABEL + r"(?:\\?[\"'])?[ \t]*[:=][ \t]*(?:\r?\n[ \t]*)?(?P<value>[^\r\n]*[^\s])",
    re.IGNORECASE,
)


# A bearer credential wherever it appears, with no header label in front: a
# curl command line, a log line, a copied value. The word after "bearer" is
# withheld whatever it is. A configured bearer secret only has to be non-empty
# ASCII, so neither a length floor, nor a character class, nor a list of prose
# words can tell a credential from text: "Bearer token" may be the token. Prose
# that mentions a bearer loses the following word, the cheap direction. The
# value ends at whitespace or a quote, and an escaping backslash before a
# closing quote stays outside it, so a serialized string keeps its escapes.
_BEARER_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z])bearer[ \t]+"
    r"(?P<token>[^\s\"']+?)(?=\\[\"']|[\s\"']|\Z)",
    re.IGNORECASE,
)


# password/passphrase/pin values — keep the label, redact the value. A quoted
# value is captured whole (the quote alternatives let the value contain spaces);
# an unquoted value stops at whitespace/separator. The left anchor is
# ``(?<![A-Za-z])`` (not ``\b``) so an underscore-joined label still matches —
# e.g. the ``DB_PASSWORD`` form has no word boundary between ``_`` and ``P``.
_PASSWORD_LABEL = r"(?<![A-Za-z])(?:password|pass(?:word)?|pwd|passphrase|passcode|pin)"  # noqa: S105
_SINGLE_CREDENTIAL_PATTERN = re.compile(
    _PASSWORD_LABEL + r"\s*(?:is\s+|[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]{4,})",
    re.IGNORECASE,
)

# A credential label as a QUOTED KEY: a JSON member, a Python dict repr, or a
# key escaped inside a serialized string, at any realistic nesting depth
# (``{"api_key": "…"}``, ``{'DB_PASSWORD': b'…'}``, ``\\"pin\\": …``). The two
# label patterns above need the separator right after the label, so the key's
# closing quote defeated them and these values passed through whole (measured
# 2026-10-28).
# THE KEY must END at the label and sit between quotes, so ``"pin_count"`` and
# ``"password_policy"`` never match. Before the label it may carry a leading
# ``_``, and ``_`` / ``-`` / ``.`` delimited segments (``OPENAI_API_KEY``,
# ``x-api-key``, ``aws.secret_access_key``). The LONG labels (password, passwd,
# passphrase, secret) also take an undelimited prefix (``dbpassword``,
# ``mysecret``, ``userPassword``); the SHORT ones (pass, pin, key, token) do not,
# because ``"bypass"``, ``"compass"`` and ``"spin"`` would then match. So
# ``"dbpass"`` is a stated residual. Bare ``"token"`` and ``"secret"`` keys are
# redacted even when they hold a count (``{"token": 500}``): the module's
# recall-first direction.
# THE VALUE follows the header rule above, for the same reason: quoting is not
# read, and the rest of the line is withheld. A first version parsed the value as
# a string literal and a review found it ended early on the OTHER quote kind
# (``"it's a secret"``), on ``b'…'`` and on double escaping. A STRUCTURED value
# (the line opens more ``[``/``{`` than it closes) continues on later lines, so it
# is withheld until the brackets balance, at most _QUOTED_KEY_SCAN_LINES lines,
# and to that limit when they never do: a review found ``{"password": [\n
# "secret"\n]}`` leaking its next line. Brackets inside strings are counted too;
# a miscount stops late or early by a line, never earlier than the key's line.
# Linear: every scan starts at an opening quote, the repeats are bounded, and a
# match inside an already-withheld span is skipped, so each line is withheld once.
# The value STARTS at a non-space, so the whitespace gap and the value never
# compete for the same characters (an ``[^\r\n]*[^\s]`` value against a 256-space
# gap cost gap x line per attempt: measured 51 s on 5,000 lines of key + spaces).
_QUOTED_KEY_EXTRA_LABEL = r"(?<![A-Za-z])(?:client[_\s-]?secret|id[_\s-]?token|secret|token|passwd)"
_QUOTED_KEY_JOINED_LABEL = r"[A-Za-z0-9]{1,64}(?:password|passwd|passphrase|secret)"
_QUOTED_KEY_CREDENTIAL_PATTERN = re.compile(
    r"(?<=[\"'])_{0,4}(?:[A-Za-z0-9]{1,64}[_.-]){0,32}"
    r"(?:" + _TOKEN_LABEL + r"|" + _PASSWORD_LABEL + r"|" + _QUOTED_KEY_EXTRA_LABEL
    + r"|" + _QUOTED_KEY_JOINED_LABEL + r")"
    r"\\{0,15}[\"'][ \t\r\n]{0,256}[:=][ \t\r\n]{0,256}(?P<value>\S[^\r\n]*)",
    re.IGNORECASE,
)
_QUOTED_KEY_SCAN_LINES = 256


def _bracket_balance(s: str) -> int:
    """Openers minus closers OUTSIDE quoted strings. A bracket inside a string
    (``"x}"``, a password containing ``{``) neither ends nor starts a structure:
    counting it let ``{"password": ["x}",`` read as closed and leak the next line
    (found by review). String state resets per line, so one stray quote cannot
    blind the rest of the scan. Linear: one pass over the line."""
    balance = 0
    quote = ""
    escaped = False
    for ch in s:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            balance += 1
        elif ch in "]}":
            balance -= 1
    return balance


def _structured_value_end(text: str, end: int, balance: int) -> int:
    """Where a value that opened ``balance`` more brackets than it closed ends: the
    end of the line where the brackets balance, else of the last line scanned. A
    balanced value longer than the scan is cut at the limit and its tail is not
    withheld: a stated residual (the PEM arm owns key blocks). The returned end
    stops before a ``\\r\\n`` pair's ``\\r``, so a CRLF line ending survives."""
    for _ in range(_QUOTED_KEY_SCAN_LINES):
        if end >= len(text):
            return len(text)
        nxt = text.find("\n", end + 1)
        nxt = len(text) if nxt == -1 else nxt
        balance += _bracket_balance(text[end:nxt])
        end = nxt
        if balance <= 0:
            break
    if 0 < end < len(text) and text[end - 1] == "\r":
        end -= 1
    return end


def _redact_quoted_key_values(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _QUOTED_KEY_CREDENTIAL_PATTERN.finditer(text):
        start, end = m.span("value")
        if start < pos:  # inside a span already withheld
            continue
        balance = _bracket_balance(m.group("value"))
        if balance > 0:
            end = _structured_value_end(text, end, balance)
        out.append(text[pos:start])
        out.append(_REDACTED)
        pos = end
    out.append(text[pos:])
    return "".join(out)


# Command-line credential flags: ``--password "two words"``, ``--token=x``,
# ``-pass 'a b'``. The label arms below need a ``:``, ``=`` or ``is`` separator
# after the label, so a value separated from its flag by whitespace alone passed
# through whole, and so did every flag named by a label only the quoted-key arm
# knew (``passwd``, ``token``, ``secret``). The flag NAME reuses the three label
# vocabularies above rather than a list of its own: the name must END at the
# label (so ``--password-file`` and ``--token-file`` are not credentials) and may
# carry ``-``/``_`` delimited segments before it (``--http-password``,
# ``--db-password``, ``--github-token``). ``-p`` is not in any vocabulary, so it
# is never read as a password flag: it is a port to ssh and docker and the
# parents flag to mkdir, and a ``-p 'a b c'`` password is a stated residual.
# The separator is whitespace, or ``=`` / ``:`` with optional whitespace around
# it, or whitespace + ``is`` + whitespace: the same separators the label arms
# accept, so a lone separator word (``--password = x``) is never read as the
# value. A flag begins a shell word: start of text, whitespace, a quote, ``=`` or
# a shell separator before it. Bounded segment repeats keep each start's scan
# bounded, and the lookbehind admits only word starts.
_FLAG_START = r"(?<![^\s\"'=;|&(`])"
_CREDENTIAL_FLAG_PATTERN = re.compile(
    _FLAG_START + r"--?(?:[A-Za-z0-9]{1,64}[_-]){0,8}"
    r"(?:" + _TOKEN_LABEL + r"|" + _PASSWORD_LABEL + r"|" + _QUOTED_KEY_EXTRA_LABEL + r")"
    r"(?P<sep>[ \t]*[=:][ \t]*|[ \t]+(?:is[ \t]+)?)",
    re.IGNORECASE,
)
# A ``user:password`` pair after curl's ``-u``/``--user`` and ``--proxy-user``
# (curl --help: ``-u, --user <user:password>``, ``-U, --proxy-user
# <user:password>``), attached (``-ualice:pw``), separated, or as a grouped short
# flag (``-su alice:pw``). Only the password goes: everything after the FIRST
# colon, the user name kept, as the URL userinfo arm keeps it. A value with no
# colon (``-u alice``, ``env -u NAME``, ``git push -u origin``) is untouched, and
# so are two colon-bearing shapes that are not credentials: a ``+`` format
# (``date -u +%H:%M``) and, after a short ``-u`` on ``rsync`` or ``scp`` only, a
# ``/``-leading remote path (``rsync -avu host:/srv``). ``-U`` is
# left out on purpose: it is wget's ``--user-agent``, whose value carries
# colons, and the pattern is case-sensitive so ``-u`` never matches it.
_USER_FLAG_PATTERN = re.compile(
    _FLAG_START + r"(?:--(?:proxy-)?user(?P<lsep>=|[ \t]+)|-[a-z]{0,4}u(?P<ssep>[ \t]*))",
)
_WORD_END = frozenset(" \t\r\n;|&<>()`")
# One double-quoted segment. The two alternatives start on different characters,
# so a failed match backtracks linearly, not quadratically.
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
# A value that starts with escaped quoting, at any encoding depth (``\"`` once
# JSON-encoded, ``\\\"`` twice).
_ESCAPED_QUOTE_START = re.compile(r"\\{1,64}[\"']")
_QUOTE_EVENT = re.compile(r"[\"'\\\n]")


def _line_end(text: str, i: int, nl: list[int] | None = None) -> int:
    """End of the line holding ``i``, before a ``\\r\\n`` pair's ``\\r``.

    ``nl`` caches the last newline found: callers ask in increasing order, and
    the first newline at or after an earlier position is also the first one at
    or after any later position up to it, so one long line is searched once
    rather than once per value on it.
    """
    if nl is not None and nl[0] >= i:
        j = nl[0]
    else:
        j = text.find("\n", i)
        j = len(text) if j == -1 else j
        if nl is not None:
            nl[0] = j
    return j - 1 if j > i and j < len(text) and text[j - 1] == "\r" else j


def _quote_close(text: str, i: int, limit: int) -> int:
    """Index just past the quote that closes the one opening at ``i``, searched
    before ``limit``; -1 when it does not close there."""
    if text[i] == "'":
        j = text.find("'", i + 1, limit)
        return -1 if j == -1 else j + 1
    m = _DOUBLE_QUOTED.match(text, i, limit)
    return -1 if m is None else m.end()


class _QuoteTracker:
    """The quote open at a position, read left to right and reset at each line.

    A flag NAMED inside a quoted argument (``-m "the --secret flag"``) has a
    value that ends where that quote closes, not a word that runs on. Line-local
    because captured text is mostly prose and output, where an apostrophe would
    otherwise colour the rest of the capture. Positions are asked in increasing
    order, so the whole text is read once.
    """

    __slots__ = ("text", "pos", "quote")

    def __init__(self, text: str) -> None:
        self.text, self.pos, self.quote = text, 0, ""

    def at(self, i: int) -> str:
        text, j, quote = self.text, self.pos, self.quote
        while j < i:
            m = _QUOTE_EVENT.search(text, j, i)
            if m is None:
                break
            c, j = m.group(), m.end()
            if c == "\n":
                quote = ""
            elif c == "\\":
                if quote != "'":
                    j += 1
            elif not quote:
                quote = c
            elif c == quote:
                quote = ""
        self.pos, self.quote = max(j, i), quote
        return quote

    def resume(self, i: int, quote: str) -> None:
        self.pos, self.quote = i, quote


def _shell_word_end(text: str, i: int, enclosed: str = "", nl: list[int] | None = None) -> int:
    """Where the shell word starting at ``i`` ends, read as the shell reads it.

    A word is a run of unquoted characters, ``'…'`` and ``"…"`` segments and
    backslash escapes, ended by unquoted whitespace or a shell metacharacter, so
    ``"a b"'c d'`` is ONE word and a quoted space never ends it. ``enclosed`` is
    the quote the word sits inside (the flag was named in a quoted argument):
    that quote ends the word too. Where the end cannot be read, more is withheld,
    never less, but only as far as the value's own quoting reaches:

    * a quote that OPENS the value and never closes runs to the end of the text,
      which is how the shell reads it (a capture cut mid-value); inside an
      enclosing quote it runs to the end of the line;
    * a quote that opens MID-word with no partner on its line ends the word
      there, so a stray quote cannot take the following lines with it;
    * a value that starts with escaped quoting (``\\"a b\\"``, at any depth) is a
      serialized string, a JSON field or a log record, whose quoting cannot be
      read here: it runs to the end of the line, as the header arm's value does.
    """
    n = len(text)
    if _ESCAPED_QUOTE_START.match(text, i):
        return _line_end(text, i, nl)
    start, line_end = i, -1
    while i < n:
        c = text[i]
        if c in _WORD_END or c == enclosed:
            break
        if c in "\"'":
            if line_end < i:
                line_end = _line_end(text, i, nl)
            opens_value = i == start
            limit = n if opens_value and not enclosed else line_end
            close = _quote_close(text, i, limit)
            if close == -1:
                if not opens_value:
                    break
                return line_end if enclosed else n
            i = close
        elif c == "\\":
            i += 2
        else:
            i += 1
    return min(i, n)


def _flag_values(text: str, pattern: re.Pattern, takes_dash_value):
    """Yield ``(match, start, end)`` of each flag value in ``text``, left to right.

    A flag inside a word already scanned is skipped, so scanned spans never
    overlap and the scan is linear in the input. ``takes_dash_value(match)``
    says whether a value starting with ``-`` is a value or the next option.
    """
    tracker = _QuoteTracker(text)
    scanned = 0
    nl = [-1]
    for m in pattern.finditer(text):
        if m.start() < scanned:  # inside a word already scanned
            continue
        start = m.end()
        if start >= len(text) or text[start] in _WORD_END:
            continue
        if text[start] == "-" and not takes_dash_value(m):
            continue
        enclosed = tracker.at(m.start())
        if enclosed and text[start] == enclosed:
            continue  # the flag ends its quoted argument: no value
        end = scanned = _shell_word_end(text, start, enclosed, nl)
        tracker.resume(end, enclosed)
        if end > start:
            yield m, start, end


def _redact_credential_flags(text: str) -> str:
    """Withhold the whole shell word after a credential flag.

    A whitespace-separated value that starts with ``-`` is read as the next
    option, not the value (``--password --verbose``); after ``=`` or ``:`` it is
    withheld whatever it starts with. An empty quoted value (``""``) is kept.
    """
    if "-" not in text:
        return text
    out: list[str] = []
    pos = 0
    for _m, start, end in _flag_values(
        text, _CREDENTIAL_FLAG_PATTERN, lambda m: any(c in m.group("sep") for c in "=:")
    ):
        if text[start:end] in ('""', "''"):
            continue
        if text[start:end].rstrip(",.") == _REDACTED:
            continue  # already withheld by an earlier arm: keep its punctuation
        out.append(text[pos:start])
        out.append(_REDACTED)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _first_colon(text: str, start: int, end: int) -> tuple[int, str]:
    """The first ``:`` in ``text[start:end]`` and the quote open at it (``""``
    when none). Backslash escapes are skipped outside quotes and inside ``"``."""
    quote, i = "", start
    while i < end:
        c = text[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
            elif c == ":":
                return i, quote
        elif c in "\"'":
            quote = c
        elif c == ":":
            return i, ""
        i += 1
    return -1, ""


# Commands whose short ``-u`` is not a user flag and whose ``host:/path``
# operand carries a colon. Only these keep a ``/``-leading second half.
_REMOTE_PATH_COMMANDS = frozenset({"rsync", "scp"})
_SEGMENT_BREAK = re.compile(r"[;|&\n(]")
_COMMAND_WORD = re.compile(r"[ \t]*(?:sudo[ \t]+)?([^\s;|&()]+)")


class _SegmentTracker:
    """The command word of the shell segment holding a position, read left to
    right (positions are asked in increasing order, so the text is read once)."""

    __slots__ = ("text", "pos", "seg", "cached")

    def __init__(self, text: str) -> None:
        self.text, self.pos, self.seg, self.cached = text, 0, 0, None

    def command_at(self, i: int) -> str:
        for m in _SEGMENT_BREAK.finditer(self.text, self.pos, i):
            self.seg, self.cached = m.end(), None
        self.pos = max(self.pos, i)
        if self.cached is None:
            word = _COMMAND_WORD.match(self.text, self.seg)
            self.cached = word.group(1).rsplit("/", 1)[-1] if word else ""
        return self.cached


def _user_flag_takes_dash_value(m: re.Match) -> bool:
    return m.group("lsep") == "=" or (m.group("lsep") is None and not m.group("ssep"))


def _redact_user_passwords(text: str) -> str:
    """Withhold the password half of a ``user:password`` after a user flag.

    Everything after the first colon in the word goes. When that colon sits
    inside a quoted segment, the quote is closed again after the marker so the
    result stays well-formed and a second scrub reads it the same way.
    """
    if "-" not in text or ":" not in text:
        return text
    out: list[str] = []
    pos = 0
    segment = _SegmentTracker(text)
    for m, start, end in _flag_values(text, _USER_FLAG_PATTERN, _user_flag_takes_dash_value):
        if text.startswith("+", start + (text[start] in "\"'")):
            continue  # a format (``date -u +%H:%M``), not a user
        colon, quote = _first_colon(text, start, end)
        if colon == -1 or text[colon + 1 : end] in ("", quote):
            continue
        if (
            text.startswith("/", colon + 1)
            and m.group("ssep") is not None
            and segment.command_at(m.start()) in _REMOTE_PATH_COMMANDS
        ):
            continue  # a remote path (``rsync -avu host:/srv``), not a password
        out.append(text[pos : colon + 1])
        out.append(_REDACTED + quote)
        pos = end
    out.append(text[pos:])
    return "".join(out)


# Credentials embedded in a URL's userinfo section (the user/password pair that
# can precede an "@" host separator). Only fires when an "@" follows, so a plain
# host-and-port URL never matches.
#
# NO LENGTH BOUND ANYWHERE, and that is the point. Two successive bounds were
# tried here and BOTH were fail-opens dressed as perf fixes: a 253 cap on the
# span after `://` (labelled "hostname" — it is the USERINFO, so the cap bought
# nothing and stopped long usernames being redacted) and then a 32 cap on the
# SCHEME (which stopped redacting any credential URL whose scheme ran longer,
# a length RFC 3986 does not limit). Each bound was added to kill a quadratic
# scan, and each one silently traded a redaction for it.
#
# The scan is gone instead of bounded: the pattern no longer matches the scheme
# AT ALL. `://` is a 3-character literal the engine can find directly, whereas a
# leading `[a-zA-Z][a-zA-Z0-9+.\-]*` re-scans the whole of every admitted-char
# run from every offset in it — that run, not the scheme's length, was the cost.
# Dropping it removes the quadratic AND the ceiling together, so there is no
# longer a length at which a real credential URL stops being redacted.
# MEASURED at 64/128/256KB on four adversarial shapes (a `://` with a long
# no-`@` tail, dense `://`, a colon storm, a long alnum run): every ratio is
# ~2.0 at 2x input, i.e. linear, and 256KB costs <15ms against a 10s timeout.
#
# Precision is unchanged by the drop: `://` in text that is not a URL is
# essentially nonexistent, and the discriminator was always the `://…:…@` shape
# rather than the scheme letters. Verified in both directions over 12 cases —
# `http://example.com/a:b@c`, `https://host:8080/path`, a bare `foo:bar@baz` and
# a plain repo URL still do NOT match.
#
# The userinfo repeat is `*`, not `+`: `https://:s3cr3t@host` (the password-only
# form a generated DSN emits, e.g. `redis://:pw@192.0.2.10:6379/0`) has an EMPTY
# username, and no other arm recognises an unlabelled password, so `+` let it
# through in plaintext.
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?P<pre>://[^:/@\s]*:)(?P<pw>[^@/\s]+)(?P<at>@)",
)

# .env-style UPPER_SNAKE=value. The bare pattern over-redacts (PYTHONPATH=…,
# EDITOR=…), so it is gated to keys whose NAME signals a secret. A quoted value
# is captured whole (so a multi-word secret like KEY='a b c' is not left with
# its tail exposed after the first space) and redacted at ANY non-zero length —
# only an EMPTY quoted value is passed through, because the corpus measurement
# behind the value floor (16 newly-unmatched assignments over 779,899 tracked
# lines) found every one of them empty: the floor's whole measured precision
# gain lives at length zero, and a higher floor quietly stopped redacting the
# short quoted secrets the parent caught (`API_KEY="abc"`, a 4-digit PIN). An
# unquoted value keeps the 6+ floor: with no closing delimiter, a short bare
# word (`API_MODE=on`) is far likelier config than credential.
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
# the empty-quoted exclusion is what removes the historical noise: an empty
# quoted value (`_OAUTH_SRC=""`) is not a secret.
#
# The bound it replaces was a FAIL-OPEN, and a subtle one: capping the key did
# not merely miss long names, it slid the match FORWARD past the name's prefix,
# so `_SECRET_KEY_HINT` no longer saw SECRET_/TOKEN_ and the value stopped being
# redacted at all. Bounding a repeat that feeds a downstream semantic check can
# break the check rather than just the reach.
_ENV_LABEL = r"(?<![A-Za-z0-9])(?P<key>_{0,4}[A-Z][A-Z0-9_]{2,512})"
_ENV_ASSIGNMENT_PATTERN = re.compile(
    _ENV_LABEL + r"(?P<sep>\s*=\s*)"
    r"(?P<val>\"[^\"]+\"|'[^']+'|[^\s]{6,})",
)
_SECRET_KEY_HINT = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|AUTH|API|CRED|PRIVATE", re.IGNORECASE
)


def credential_label_kind(label: str) -> str | None:
    """Recognize structured labels using the exact plain-text vocabulary.

    Structured values have no prose length floor. Environment labels retain
    uppercase/key-shape constraints; the secret hint alone is insufficient.
    """
    for kind, pattern in (
        ("token", _TOKEN_LABEL),
        ("token", _HEADER_LABEL),
        ("password", _PASSWORD_LABEL),
    ):
        if re.search(pattern + r"\s*\Z", label, re.IGNORECASE):
            return kind
    match = re.search(_ENV_LABEL + r"\s*\Z", label)
    if match and _SECRET_KEY_HINT.search(match.group("key")):
        return "env"
    return None


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
    return _scrub(text, flags=True)


def scrub_base(text: str) -> str:
    """``scrub`` without the two credential-flag passes.

    Not a weaker scrub to store with: it exists so a caller that decides
    something from WHETHER the scrubber changed a span (transcript analytics
    hides changed JSON spans from its whole-text pass) can decide it exactly as
    before those passes existed, then apply ``scrub`` itself. The flag passes
    run last and only add redaction, so that caller's output can only gain
    redaction from them.
    """
    return _scrub(text, flags=False)


def _scrub(text: str, *, flags: bool) -> str:
    if not text:
        return text
    try:
        text = _redact_pem_blocks(text)
        text = _KNOWN_KEY_PREFIX_PATTERN.sub(_REDACTED, text)
        text = _STRUCTURED_CREDENTIAL_PATTERN.sub(_REDACTED, text)
        text = _redact_value_group(_HEADER_PATTERN, text, "value")
        text = _redact_value_group(_BEARER_VALUE_PATTERN, text, "token")
        text = _redact_quoted_key_values(text)
        text = _redact_value_group(_CREDENTIAL_TOKEN_PATTERN, text, "token")
        text = _redact_value_group(_SINGLE_CREDENTIAL_PATTERN, text, "value")
        text = _redact_value_group(_URL_CREDENTIAL_PATTERN, text, "pw")
        text = _redact_env(text)
        # The flag passes run LAST, on what every other arm already withheld.
        # They only ever replace text with the marker, so nothing an earlier arm
        # redacts can survive them; run first, they consumed labels and quotes
        # the label arms needed (``--secret password: x`` lost ``x``).
        if flags:
            text = _redact_credential_flags(text)
            text = _redact_user_passwords(text)
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
