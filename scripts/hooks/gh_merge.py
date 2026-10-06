"""Recognise a pull-request merge spelled through the GitHub API (#2768).

The push guard gates a merge only when it is spelled ``gh pr merge``. The same
merge sent through ``gh api`` skips every merge-gate check, so the guard refuses
that SPELLING and names the gated command. Owner ruling (2026-10-05): a ``gh api``
call that NAMES a merge passes only when it is a plainly spelled read; anything
this module cannot read in full is refused.

What a merge is, from GitHub's own descriptions (REST OpenAPI v1.1.4 and GraphQL
introspection, read 2026-10-05; the ``repositories/{id}`` alias is undocumented
but MEASURED to route): a non-GET to ``pulls/{n}/merge`` or ``.../merge-async``,
or a GraphQL ``mergePullRequest`` / ``enablePullRequestAutoMerge`` /
``enqueuePullRequest`` mutation.

THE SHAPE IS A CLOSED SET, because an argv reader that tries to model gh's flag
parser keeps missing spellings (short-flag groups such as ``-iXPUT``, where pflag
hands the next argument to the first value-taking letter). So:

1. A call whose argv NAMES a merge (a merge path segment after percent-decoding,
   or a mutation name, anywhere except the value of a non-``query`` field) is
   allowed only when its argv is exactly ``gh api <literal endpoint>`` plus flags
   from a read-only allowlist, with an explicit GET when fields are present.
   Anything else is refused.
2. A GraphQL call that names no merge is refused when its query cannot be read:
   stdin, a missing or non-regular file, a file another part of the same command
   could write first, or a document that is not literal (``$(cat f)``).
3. A PUT to an endpoint holding shell text (or a method that may be PUT: shell
   text, a short-flag group containing ``X``, an ``xargs`` carrier) is refused.
   POSTs to variable endpoints are review replies and stay silent; only PUT merges.

MEASURED on one install (2026-10-05) by replaying its 118,197 recorded session
commands (about five months) through this module with no cwd; the replay script
is local to that install, not part of the repo: 28 refusals. One is a real API
merge, 16 are reviewers' probe spellings, and about 10 are ordinary commands
(query files in multi-step commands, whole queries passed as `$(cat f)`); each
of those costs the session one rewrite.

Not covered, by design: deliberately disguised spellings and other programs that
reach the same endpoints, and branch-to-branch merges (``mergeBranch``, REST
``/merges``), which merge no pull request. A ``$(...)`` that only fills a value
inside a visible read query is allowed. This is a tripwire for ordinary
spellings; the boundary that closes the rest is server-side.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from typing import NamedTuple
from urllib.parse import unquote

from pr_close_advisory import _parse_api

_MERGE_WORD = re.compile(r"(?i)(?:^|/)merge(?:-async)?(?:$|[/?#])")
_MERGE_PATH = re.compile(
    r"(?i)(?:^|/)(?:repos/[^/\s]+/[^/\s]+|repositories/[^/\s]+)"
    r"/+pulls/+(?P<pr>[^/\s]+)/+(?:\./+)*merge(?:-async)?/*(?:[?#].*)?$"
)
_MERGE_MUTATIONS = re.compile(
    r"\b(?:mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest)\b"
)
_MUTATION = re.compile(r"\bmutation\b")
#: Shell text the guard cannot evaluate: an expansion, a command substitution, or
#: a brace expansion (`{a,b}`, `{a..b}`; gh's own `{owner}` placeholders have
#: neither a comma nor `..`).
_SHELL_TEXT = re.compile(r"[$`]|\{[^{}]*(?:,|\.\.)[^{}]*\}")
#: Anything in a command that runs code the guard does not see as a segment.
_RUNS_UNSEEN = re.compile(r"[<>]\(|\$\(|`")
_GRAPHQL = re.compile(r"(?i)(?:^|/)graphql/?$")
#: A literal GraphQL document opens with one of these; `$(cat f)` does not.
_DOCUMENT_START = ("{", "query", "mutation", "fragment", "subscription", "#")
_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")
#: `gh api` flags that cannot write (from `gh api --help`, gh 2.101).
_READ_BOOL_FLAGS = frozenset(
    {
        "--paginate",
        "--slurp",
        "--silent",
        "-i",
        "--include",
        "--verbose",
        "--allow-escape-sequences",
    }
)
_READ_VALUE_FLAGS = frozenset(
    {"--jq", "-q", "--template", "-t", "--cache", "-H", "--header", "--hostname", "-p", "--preview"}
)
#: The short flags of `gh api` that take a value, so `-XPUT` or `-qEXPR` is one
#: flag with a glued value rather than a group of letters.
_VALUE_SHORTS = frozenset({"-X", "-f", "-F", "-H", "-q", "-t", "-p"})
_READ_METHODS = frozenset({"GET", "HEAD"})
#: A bound on the file read inside a PreToolUse hook; past it the file is
#: unreadable (a refusal with a hint), never cut.
_MAX_QUERY_BYTES = 1 << 20
_QUERY_HINT = (
    "If it is not a merge, put the query inline (-f query='...'), or read it from a "
    "file (-F query=@path) in a command of its own."
)
_SPELL_HINT = (
    "If it is not a merge, spell it plainly: gh api <literal endpoint>, with -X and "
    "any fields as separate words."
)


class ApiMerge(NamedTuple):
    reason: str
    pr: str | None
    unreadable: bool
    hint: str = ""


def _is_group(tok: str) -> bool:
    """A short-flag group such as `-iX`: several letters behind one dash."""
    return (
        tok.startswith("-")
        and not tok.startswith("--")
        and len(tok) > 2
        and (tok[:2] not in _VALUE_SHORTS)
    )


#: Output filters: their values shape what is printed and can never write.
_FILTER_FLAGS = ("--jq", "-q", "--template", "-t")
#: Every spelling of a field flag with its value attached in the same word.
_ATTACHED_FIELD = re.compile(r"^(?:-[fF]=?|--field=|--raw-field=)(?P<value>.+)$", re.DOTALL)


def _field_value(tok: str) -> str | None:
    """The `key=value` carried by a field flag written as one word, or None."""
    match = _ATTACHED_FIELD.match(tok)
    return match["value"] if match else None


def _scanned_tokens(argv: list[str]) -> tuple[list[str], list[str]]:
    """``(words, queries)``. ``words`` is every token except field values and the
    values of output filters; ``queries`` is the text of every ``query=`` field,
    in any spelling. A form this does not recognise lands in ``words``, where a
    mutation name counts on its own, so a miss can only over-refuse."""
    words: list[str] = []
    queries: list[str] = []
    expect_field = False
    skip_filter = False
    for tok in argv[1:]:
        if skip_filter:
            skip_filter = False
            continue
        if expect_field:
            expect_field = False
            if tok.startswith("query="):
                queries.append(tok[len("query=") :])
            continue
        if tok in _FIELD_FLAGS:
            expect_field = True
            continue
        if tok in _FILTER_FLAGS:
            skip_filter = True
            continue
        name, eq, _ = tok.partition("=")
        if eq and name in _FILTER_FLAGS:
            continue
        if tok[:2] in ("-q", "-t") and len(tok) > 2 and not tok.startswith("--"):
            continue
        field = _field_value(tok)
        if field is not None:
            if field.startswith("query="):
                queries.append(field[len("query=") :])
            continue
        if tok.startswith("query="):
            queries.append(tok[len("query=") :])
            continue
        words.append(tok)
    return words, queries


def _graphql_code(text: str) -> str:
    """The document with its strings and comments removed (GraphQL spec, Source
    Text): a name inside either cannot execute. An unterminated string returns
    the text unchanged, so a lexing doubt keeps every word visible."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            if end < 0:
                return text
            out.append(" ")
            i = end + 3
        elif text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return text
            out.append(" ")
            i = j + 1
        elif text[i] == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _is_merge_mutation(doc: str) -> bool:
    code = _graphql_code(doc)
    return bool(_MUTATION.search(code) and _MERGE_MUTATIONS.search(code))


def _names_merge(argv: list[str]) -> bool:
    """A merge path word or a merge mutation name in the argv, or a query field
    whose document is a merge mutation (outside its strings and comments)."""
    words, queries = _scanned_tokens(argv)
    for tok in words:
        if _MERGE_WORD.search(unquote(tok)) or _MERGE_MUTATIONS.search(tok):
            return True
    return any(_is_merge_mutation(q) for q in queries)


def _plain_read(argv: list[str]) -> bool:
    """`gh api <literal endpoint>` plus read-only flags, and nothing else."""
    if len(argv) < 3 or argv[1] != "api":
        return False
    positionals = 0
    explicit_read = False
    has_fields = False
    i = 2
    while i < len(argv):
        tok = argv[i]
        name, eq, value = tok.partition("=")
        if tok in _READ_BOOL_FLAGS:
            i += 1
        elif name in _READ_VALUE_FLAGS:
            i += 1 if eq else 2
        elif tok[:2] in ("-q", "-t", "-H", "-p") and len(tok) > 2:
            # A read flag with its value attached (`-q.merged`, `-HAccept:...`).
            i += 1
        elif name in ("-X", "--method"):
            method = value if eq else (argv[i + 1] if i + 1 < len(argv) else "")
            if method.upper() not in _READ_METHODS:
                return False
            explicit_read = True
            i += 1 if eq else 2
        elif tok.upper() in ("-XGET", "-XHEAD"):
            explicit_read = True
            i += 1
        elif name in _FIELD_FLAGS:
            has_fields = True
            i += 1 if eq else 2
        elif tok[:2] in ("-f", "-F") and len(tok) > 2:
            has_fields = True
            i += 1
        elif tok.startswith("-") or _SHELL_TEXT.search(tok):
            return False
        else:
            positionals += 1
            i += 1
    return positionals == 1 and (explicit_read or not has_fields)


def _plain_parse(argv: list[str]) -> bool:
    """No short-flag groups and no shell text outside output filters, so the
    parsed method and endpoint are the real ones."""
    words, _ = _scanned_tokens(argv)
    return not any(_is_group(t) or _SHELL_TEXT.search(t) for t in words)


def _read_text(ref: str, cwd: str | None) -> str | None:
    """A regular file named by `@path` or `--input path`, or None. Opened
    non-blocking and checked regular, so a FIFO or a device cannot hang the hook."""
    if ref in ("", "-"):
        return None
    path = ref if os.path.isabs(ref) else (os.path.join(cwd, ref) if cwd else None)
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _MAX_QUERY_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _MAX_QUERY_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _graphql_documents(call, cwd: str | None, trusted: bool) -> list[str] | None:
    """Every query document the call would send, or None if one cannot be read."""
    docs: list[str] = []
    for key, value in call.fields:
        if key != "query":
            continue
        from_file = value.startswith("@")
        text = (_read_text(value[1:], cwd) if trusted else None) if from_file else value
        if text is None:
            return None
        docs.append(text)
    if call.input is not None:
        body = _read_text(call.input, cwd) if trusted else None
        if body is None:
            return None
        try:
            query = json.loads(body).get("query")
        except (ValueError, AttributeError):
            return None
        if not isinstance(query, str):
            return None
        docs.append(query)
    if not docs or any(not d.lstrip().startswith(_DOCUMENT_START) for d in docs):
        return None
    # A mutation is a write: shell text inside one could supply the merge.
    # (In a read query it only fills a value, a documented residual.)
    if any(_MUTATION.search(d) and _RUNS_UNSEEN.search(d) for d in docs):
        return None
    return docs


def is_help_only(argv: list[str]) -> bool:
    """Exactly `gh pr merge --help` or `gh pr merge -h`, and nothing else.

    A closed form on purpose: with any other word present, gh's flag parser may
    hand `--help` to a value flag (`-sb --help` is a squash merge whose body is
    `--help`), so the merge gate must judge it.
    """
    return (
        len(argv) == 4
        and os.path.basename(argv[0]) == "gh"
        and argv[1:3] == ["pr", "merge"]
        and argv[3] in ("--help", "-h")
    )


def _through_xargs(raw: str, argv: list[str]) -> bool:
    """Was this gh segment run by xargs, which appends words the guard never sees?"""
    try:
        words = shlex.split(raw)
    except ValueError:
        return re.search(r"(?<![\w-])xargs(?![\w-])", raw) is not None
    head = words[: max(0, len(words) - len(argv))]
    return any(os.path.basename(w) == "xargs" for w in head)


def files_trusted(command: str, n_segments: int) -> bool:
    """A query file is read only when nothing else in the command can write it
    between this check and the read gh makes."""
    return n_segments == 1 and not _RUNS_UNSEEN.search(command)


def api_merge_reason(
    argv: list[str], cwd: str | None, *, raw: str = "", trusted: bool = True
) -> ApiMerge | None:
    """Why this argv merges a PR through the API (or cannot be read), else None.

    ``cwd`` resolves a relative ``@file`` / ``--input`` path (None: unknown, so a
    relative path is unreadable). ``raw`` is the segment's source text, read only
    to notice an ``xargs`` carrier. ``trusted`` is :func:`files_trusted`.
    """
    if len(argv) < 2 or os.path.basename(argv[0]) != "gh" or "api" not in argv[1:]:
        return None
    call = _parse_api(argv)
    via_xargs = bool(raw) and _through_xargs(raw, argv)

    if _names_merge(argv):
        if not via_xargs and _plain_read(argv):
            return None
        endpoint = unquote(call.endpoint) if call and call.endpoint else ""
        match = _MERGE_PATH.search(endpoint)
        named = match or next(
            (m for t in argv[1:] if (m := _MERGE_PATH.search(unquote(t))) is not None), None
        )
        pr = named["pr"] if named is not None else None
        if match is not None and not via_xargs and _plain_parse(argv):
            implied = "POST" if call.fields or call.input is not None else "GET"
            method = (call.method or implied).upper()
            return ApiMerge(f"a REST {method} to pulls/{pr}/merge", pr, False)
        return ApiMerge("a merge spelled in a form this guard cannot read", pr, True, _SPELL_HINT)

    if call is None:
        return None
    method = (call.method or ("POST" if call.fields or call.input is not None else "GET")).upper()
    may_put = (
        method == "PUT"
        or bool(_SHELL_TEXT.search(method))
        or via_xargs
        or any(_is_group(t) and "X" in t for t in argv[1:])
    )
    endpoint = call.endpoint or ""
    # GraphQL is always a POST, so a variable endpoint with an explicit other
    # method (a PR-body PATCH with --input) is not a GraphQL candidate.
    has_query = any(k == "query" for k, _ in call.fields) or (
        call.input is not None and method == "POST"
    )
    if _GRAPHQL.search(unquote(endpoint)) or (_SHELL_TEXT.search(endpoint) and has_query):
        if via_xargs:
            return ApiMerge("a GraphQL request run through xargs", None, True, _QUERY_HINT)
        docs = _graphql_documents(call, cwd, trusted)
        if docs is None:
            return ApiMerge(
                "a GraphQL request whose query this guard cannot read", None, True, _QUERY_HINT
            )
        if any(_is_merge_mutation(d) for d in docs):
            return ApiMerge("a GraphQL merge mutation", None, False)
        return None
    if not endpoint:
        if may_put:
            return ApiMerge("a gh api call with no visible endpoint", None, True, _SPELL_HINT)
        return None
    if _SHELL_TEXT.search(endpoint) and may_put:
        return ApiMerge("a write to an endpoint holding shell text", None, True, _SPELL_HINT)
    return None
