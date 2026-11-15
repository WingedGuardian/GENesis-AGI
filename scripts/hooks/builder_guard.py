#!/usr/bin/env python3
"""PreToolUse(Bash) block for a builder session: no merges, no reviews, no review-thread writes.

A builder is a Claude Code session the board launcher started in a tmux slot
for one issue (``scripts/cc-slot.sh launch``, which sets ``GENESIS_BUILDER_KEY``).
Its job ends at an open pull request plus one ``@codex review``; the closing
session handles review threads and merges (owner ruling, spec E). This hook does
nothing unless ``GENESIS_BUILDER_KEY`` is set and non-empty. With it set, it
refuses, through ``gh`` or ``curl`` / ``wget``:

- merging: ``gh pr merge``; whatever ``gh_merge.api_merge_reason`` refuses for
  every session (REST ``pulls/<n>/merge`` and ``merge-async``, the GraphQL merge
  mutations, a call it cannot read); a branch merge (``POST .../merges``,
  ``mergeBranch``);
- reviewing: ``gh pr review``; a write to ``pulls/<n>/reviews`` or below it; the
  review mutations (``addPullRequestReview``, ``submitPullRequestReview``,
  ``dismissPullRequestReview``, ``deletePullRequestReview``);
- review-thread writes: a REST reply (``pulls/<n>/comments/<id>/replies``, or a
  ``pulls/<n>/comments`` write whose body carries ``in_reply_to`` or cannot be
  read); editing or deleting a review comment (``pulls/comments/<id>``); the
  thread mutations (``resolveReviewThread``, ``unresolveReviewThread``,
  ``addPullRequestReviewThreadReply``, ``addPullRequestReviewComment``,
  ``addPullRequestReviewThread``, ``updatePullRequestReviewComment``,
  ``deletePullRequestReviewComment``, ``minimizeComment``);
- the routes around the parse: ``gh alias set`` / ``import`` and
  ``gh extension exec`` (an alias is not expanded here), ``curl -K`` (its URL
  and body come from a file), and a command-running program (``eval``,
  ``xargs``, ``find`` and others, or a program named by an expansion) whose text
  names a refused operation, and an interpreter's ``-c``/``-e`` code that does.

Paths are compared the way GitHub reads them: percent-decoded, case-insensitive,
with repeated slashes and ``./`` segments collapsed (``gh_merge``'s own rule).

Why a block and not an advisory (the hooks axiom needs a measured reason):
a reply under the owner's login clears even a Codex P1 from the merge gate
(``git_push_guard``'s replied-to rule, #4434), and a builder carries no
dispatched marker, so the merge arm lets it merge once every machine gate is
green (#1672, #4524). A builder in its own slot has no owner watching the
command. Everything else (``gh pr create``, ``gh pr comment``, ``gh pr view``,
reads, ``git push``) is left to the existing guards.

What this is, and is not: a tripwire for the spellings a builder plausibly
reaches for, the same scope ``gh_merge`` states. A deliberately disguised
spelling is out of scope; the boundary that holds there is the in-chat merge
rule. A command this guard cannot read is refused when its text names a refused
operation. Without the key set, nothing is parsed and nothing is refused. A
broken ``hook_input`` blocks every session (the degraded contract every Bash
blocker shares); a payload ``hook_input`` cannot parse reads as no command and
passes, its documented contract; any other failure blocks a builder only.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from urllib.parse import unquote, urlsplit

# Self-locate so the shared modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_input import read_payload, tool_input  # noqa: E402
except Exception:  # noqa: BLE001 — an unimportable hook_input must BLOCK, not vanish.
    if __name__ != "__main__":
        raise
    # The house degraded contract (background_pipe_guard.py): with the module that
    # reads the payload gone, this guard cannot tell a builder's merge from an `ls`,
    # and an unguarded import exits 1, which Claude Code reads as NON-blocking.
    try:
        sys.stderr.write(
            "GUARD DEGRADED (builder_guard): shared hook_input could not be imported; "
            "BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)

try:  # noqa: E402
    import discarded_write
except Exception:  # noqa: BLE001 — GUARDED: the note is a courtesy, never a reason to exit 1.
    discarded_write = None  # type: ignore[assignment]

_KEY_VAR = "GENESIS_BUILDER_KEY"

_MUTATIONS = re.compile(
    r"\b(?:resolveReviewThread|unresolveReviewThread|addPullRequestReviewThreadReply"
    r"|addPullRequestReviewComment|addPullRequestReviewThread|updatePullRequestReviewComment"
    r"|deletePullRequestReviewComment|minimizeComment|addPullRequestReview"
    r"|submitPullRequestReview|dismissPullRequestReview|deletePullRequestReview"
    r"|mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest|mergeBranch)\b"
)
_IN_REPLY = re.compile(r"\bin_?reply_?to", re.IGNORECASE)
# Matched against a normalised path (see _norm).
_GRAPHQL_PATH = re.compile(r"(?:^|/)graphql/?$")
_COMMENTS = re.compile(r"/pulls/[^/]+/comments/?$")
_WRITE_TARGETS = (
    (re.compile(r"/pulls/[^/]+/merge(?:-async)?(?:/|$)"), "a pull-request merge"),
    (re.compile(r"/pulls/[^/]+/comments/[^/]+/replies(?:/|$)"), "a reply to a review comment"),
    (re.compile(r"/pulls/[^/]+/reviews(?:/|$)"), "a pull-request review"),
    (re.compile(r"/pulls/comments/[^/]+/?$"), "an edit to a review comment"),
    (re.compile(r"/repos/[^/]+/[^/]+/merges/?$"), "a branch merge"),
)
#: A command-running program whose text names a refused operation.
_INTERPRETERS = frozenset({"python", "python3", "perl", "node", "ruby"})


def _inline_code(argv: list[str]) -> str | None:
    """The code an interpreter is given on its command line (``-c``/``-e``)."""
    for n, tok in enumerate(argv[1:], start=1):
        if tok in ("-c", "-e", "--eval") and n + 1 < len(argv):
            return argv[n + 1]
    return None


_CARRIERS = frozenset(
    {
        "eval",
        "xargs",
        "find",
        "parallel",
        "watch",
        "flock",
        "script",
        "tmux",
        "screen",
        "busybox",
    }
)
#: The blind path names an OPERATION, not a word: prose that mentions "review" or
#: "merge" (a heredoc writing notes, an issue comment) is not one. MEASURED
#: 2026-11-15 over 1,500 recorded commands, the bare words refused 8 such writes.
_BLIND = re.compile(
    r"\bgh\b[^|;&]*\bpr\s+(?:merge|review)\b"
    r"|/pulls/[^\s'\"]*/(?:merge|merge-async|replies|reviews)\b"
    r"|/merges\b|\bin_reply_to\b(?!_id)|" + _MUTATIONS.pattern,
    re.IGNORECASE,
)
_GITHUB_HOST = re.compile(r"^(?:api\.)?github\.com\.?$", re.IGNORECASE)

_TAIL = (
    "A builder session stops at an open pull request plus one `@codex review`; the "
    "closing session handles reviews, review threads and merges. Leave this to it: "
    "finish your other work, then stop."
)


def _norm(path: str) -> str:
    """A request path as GitHub reads it: decoded, lower-case, single slashes,
    no ``.`` segments, and a leading slash."""
    path = unquote(path or "").lower().split("?", 1)[0].split("#", 1)[0]
    parts: list[str] = []
    for p in path.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p and p != ".":
            parts.append(p)
    return "/" + "/".join(parts)


def _write_target(path: str) -> str | None:
    norm = _norm(path)
    for pattern, what in _WRITE_TARGETS:
        if pattern.search(norm):
            return what
    return None


def _gh_reason(seg, cmd: str, n_segments: int, cwd: str | None) -> str | None:
    from gh_merge import (
        _graphql_documents,
        _read_text,
        api_merge_reason,
        files_trusted,
    )
    from pr_close_predicate import _parse_api
    from shell_parse import gh_command

    inv = gh_command(seg.argv)
    if inv is None:
        return None
    if inv.group == "pr" and inv.subcommand in ("merge", "review"):
        return f"`gh pr {inv.subcommand}`"
    # The shared walker models neither group, so their verb is the first operand.
    verb = inv.subcommand or (inv.positionals[0] if inv.positionals else None)
    if inv.group == "alias" and verb in ("set", "import"):
        return "`gh alias` (an alias can spell a merge this guard does not expand)"
    if inv.group in ("extension", "extensions", "ext") and verb == "exec":
        return "`gh extension exec`"
    if inv.group != "api":
        return None
    trusted = files_trusted(cmd, n_segments)
    found = api_merge_reason(seg.argv, cwd, raw=seg.raw, trusted=trusted)
    if found is not None:
        return found.reason
    call = _parse_api(seg.argv)
    if call is None:
        return None
    endpoint = call.endpoint or ""
    has_body = bool(call.fields) or call.input is not None
    method = (call.method or ("POST" if has_body else "GET")).upper()
    if _GRAPHQL_PATH.search(_norm(endpoint)):
        docs = _graphql_documents(call, cwd, trusted)
        if docs is None:
            return "a GraphQL request whose query this guard cannot read"
        if any(_MUTATIONS.search(d) for d in docs):
            return "a GraphQL review, review-thread or merge mutation"
        return None
    if method in ("GET", "HEAD"):
        return None
    what = _write_target(endpoint)
    if what:
        return f"{what} through the API ({method} {endpoint})"
    if _COMMENTS.search(_norm(endpoint)):
        if any(_IN_REPLY.search(k) for k, _ in call.fields):
            return "a reply to a review comment (`in_reply_to`)"
        if call.input is not None:
            body = _read_text(call.input, cwd) if trusted else None
            if body is None:
                return "a review-comment write whose body this guard cannot read"
            if _IN_REPLY.search(body):
                return "a reply to a review comment (`in_reply_to` in the body)"
    return None


# curl and wget options that carry a request body, and those that name a method.
_CURL_BODY = {
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "--data-ascii",
    "--data-urlencode",
    "--json",
    "-F",
    "--form",
    "--form-string",
    "-T",
    "--upload-file",
}
_WGET_BODY = {"--post-data", "--post-file", "--body-data", "--body-file"}
_METHOD = {"-X", "--request", "--method"}
#: Options whose value is a file name, never the body text itself.
_FILE_BODY = {"--post-file", "--body-file", "-T", "--upload-file"}
#: curl and wget options whose value is not a URL (headers, output, auth...).
_VALUE_OPTS = {
    "-H",
    "--header",
    "-o",
    "--output",
    "-w",
    "--write-out",
    "-u",
    "--user",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-m",
    "--max-time",
    "--connect-timeout",
    "-r",
    "--range",
    "-x",
    "--proxy",
    "--cacert",
    "--cert",
    "--key",
    "-E",
    "--resolve",
    "--retry",
    "-O",
    "--output-document",
    "--header=",
    "-U",
    "--user-agent=",
    "--http-user",
    "--http-password",
    "-P",
    "--directory-prefix",
    "--timeout",
    "-t",
    "--tries",
}
#: Short curl options that take a value, inside a flag group (``-sH v``).
_SHORT_VALUE = set("HowuAebcmrxEUPt")


def _http_reason(seg) -> str | None:
    """A refused GitHub request through curl or wget."""
    argv = seg.argv
    prog = os.path.basename(argv[0])
    urls, bodies, method, unreadable = [], [], None, False
    i = 1
    while i < len(argv):
        tok = argv[i]
        name, _, glued = tok.partition("=") if tok.startswith("--") else (tok, "", "")
        value = None
        if tok in ("-K", "--config") or tok.startswith("-K") or name == "--config":
            return f"`{prog}` reading its request from a config file"
        if name == "--url":
            urls.append(glued if glued else (argv[i + 1] if i + 1 < len(argv) else ""))
            i += 1 if glued else 2
            continue
        if name in _VALUE_OPTS and not glued:
            i += 2  # the value of a header, output, user... option is not a URL
            continue
        if name in _VALUE_OPTS:
            i += 1
            continue
        if name in _METHOD or name in _CURL_BODY or name in _WGET_BODY:
            if glued or tok.startswith("--") and "=" in tok:
                value = glued
            else:
                i += 1
                value = argv[i] if i < len(argv) else ""
            if name in _METHOD:
                method = value.upper()
            else:
                bodies.append(value)
                if name in _FILE_BODY:
                    unreadable = True
        elif tok.startswith("-X") and len(tok) > 2:
            method = tok[2:].upper()
        elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 2:
            # A short-flag group: -sd BODY, -sXPUT and the like.
            letters = tok[1:]
            for n, ch in enumerate(letters):
                if ch == "K":
                    return f"`{prog}` reading its request from a config file"
                if ch in _SHORT_VALUE:
                    if not letters[n + 1 :]:
                        i += 1  # its value is the next word
                    break
                if ch in "dFT":
                    rest = letters[n + 1 :]
                    if rest:
                        bodies.append(rest)
                    else:
                        i += 1
                        bodies.append(argv[i] if i < len(argv) else "")
                    break
                if ch == "X":
                    rest = letters[n + 1 :]
                    if rest:
                        method = rest.upper()
                    else:
                        i += 1
                        method = (argv[i] if i < len(argv) else "").upper()
                    break
        elif not tok.startswith("-"):
            urls.append(tok)
        i += 1
    for body in bodies:
        if body.startswith("@") or "$" in body or "`" in body:
            unreadable = True
    method = method or ("POST" if bodies else "GET")
    for url in urls:
        try:
            parts = urlsplit(url if "://" in url else f"https://{url}")
            parts.hostname  # noqa: B018 — raises ValueError on a malformed netloc
        except ValueError:
            continue  # not a URL
        host = (parts.hostname or "").rstrip(".")
        if not _GITHUB_HOST.match(host + "."):
            if "$" not in url or method in ("GET", "HEAD"):
                continue
            if not _BLIND.search(seg.raw):
                continue
            return f"`{prog}` sending a {method} to a URL this guard cannot read"
        path = _norm(parts.path)
        if _GRAPHQL_PATH.search(path):
            if unreadable:
                return f"`{prog}` sending a GraphQL body this guard cannot read"
            if any(_MUTATIONS.search(b) for b in bodies):
                return f"`{prog}` sending a GraphQL review, review-thread or merge mutation"
            continue
        if method in ("GET", "HEAD"):
            continue
        what = _write_target(path)
        if what:
            return f"{what} through `{prog}`"
        if _COMMENTS.search(path) and (unreadable or any(_IN_REPLY.search(b) for b in bodies)):
            return f"a reply to a review comment through `{prog}`"
    return None


def reason(cmd: str, cwd: str | None) -> str | None:
    """Why a builder may not run *cmd*, or None."""
    from shell_parse import analyze_checked, mentions, rewrites_before_running

    segs, blind = analyze_checked(cmd)
    if blind is not None or rewrites_before_running(cmd):
        if mentions(cmd, _BLIND):
            return "a command this guard cannot read that names a merge, a review or a review-thread write"
        if blind is not None:
            return None
    for seg in segs:
        exe = seg.exe
        if exe == "gh":
            found = _gh_reason(seg, cmd, len(segs), cwd if seg.depth == 0 else None)
        elif exe in ("curl", "wget"):
            found = _http_reason(seg)
        elif exe in _INTERPRETERS:
            code = _inline_code(seg.argv)
            found = (
                f"`{exe}` running code that names a merge, a review or a review-thread write"
                if code is not None and _BLIND.search(code)
                else None
            )
        elif (exe in _CARRIERS or getattr(seg, "verb_unresolved", False)) and _BLIND.search(
            seg.raw
        ):
            found = "a command run through another program that names a merge, a review or a review-thread write"
        else:
            found = None
        if found:
            return found
    return None


def main() -> int:
    if not os.environ.get(_KEY_VAR, "").strip():
        return 0
    try:
        payload = read_payload()
        ti = tool_input(payload)
        cmd = ti.get("command")
        if discarded_write is not None:
            with contextlib.suppress(Exception):  # a raise here would exit 1 = NON-blocking
                discarded_write.remember(cmd)
        if not isinstance(cmd, str) or not cmd.strip():
            return 0
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        found = reason(cmd, cwd if isinstance(cwd, str) and cwd else None)
    except Exception as exc:  # noqa: BLE001 — a builder's guard that cannot run blocks
        found = f"the builder guard could not check this command ({type(exc).__name__})"
    if not found:
        return 0
    sys.stderr.write(f"BLOCKED (builder session): {found}. {_TAIL}\n")
    if discarded_write is not None:
        with contextlib.suppress(Exception):
            discarded_write.warn()
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 — never exit 1 (non-blocking) with the key set
        code = 2 if os.environ.get(_KEY_VAR, "").strip() else 0
    sys.stderr.flush()
    os._exit(code)
