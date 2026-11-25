"""Read the body a ``gh ... create`` command will publish, when it can be known.

Shared by the PreToolUse gates that judge a create by its body, so the rules are
one: a ``--body`` literal with no ``$`` or backtick, a ``--body-file`` no earlier
step of the same command can have written, or the quoted heredoc on ``-F -``.
Anything else is unknown (None), and each caller decides what unknown means.

Stdlib only, and it reads nothing but the body file it is given: the caller parses
the command (``shell_parse``) and passes the segments and option values in.
"""

from __future__ import annotations

import os
import re
import shlex
import stat

#: The `"$(cat <<'EOF'` idiom's value as the parser leaves it once the heredoc is
#: excised. Matched whole, so text concatenated after its `)"` is never dropped.
_IDIOM_VALUE = re.compile(r"\$\(cat <<'[A-Za-z_][A-Za-z0-9_]*'\n\)")
_CWD_CHANGERS = frozenset({"cd", "pushd", "popd"})
#: The most of a body file this module reads. GitHub refuses an issue body over 65,536
#: characters, which is at most 262,144 bytes of UTF-8, so a real body fits, and
#: anything larger passes unread. It bounds the READ (and the memory it takes), NOT
#: the parse time: 64 KB of hostile markdown already outlasts a hook's budget (see
#: ``issue_premise_gate._PARSE_LIMIT_S``), and that limit is what keeps the parse
#: inside it. Deliberately not the plan gate's 8 MB cap.
_MAX_BODY_BYTES = 256 * 1024
#: The CLOSED allow-list of programs an earlier step may run and still leave a body
#: file readable (owner ruling): none of them can write a file. Deliberately not `rg`
#: or `ag` (`--pre` runs a program), nor any other read-only tool: unlisted passes.
_INERT = frozenset({"cd", "echo", "grep", "true", "test", "["})
_TEE = re.compile(r"(?<![\w-])tee(?![\w-])")
#: The only `>` spellings the rule above exempts, each matched as a WHOLE
#: whitespace-delimited token of the raw command: neither can write the body file.
#: Any other token with a `>` (`2>f`, `2>/dev/nullx`, `>/dev/null`, `&>`, `2>>`) counts.
_STDERR_TOKENS = frozenset({"2>&1", "2>/dev/null"})
#: Tokens end at whitespace and at the list separators `;`, `&&` and `||`, so
#: `2>&1;` is the exempt token followed by a separator. Never `|` or `&` alone:
#: `2>|f` writes, and `&` is part of `2>&1`.
_TOKEN_SPLIT = re.compile(r"\s+|;|&&|\|\|")


def _ancestors(segments, k: int) -> set[int]:
    out, depth = set(), segments[k].depth
    for i in range(k - 1, -1, -1):
        if segments[i].depth < depth:
            out.add(i)
            depth = segments[i].depth
    return out


def _children(segments, k: int) -> list:
    out = []
    for seg in segments[k + 1 :]:
        if seg.depth <= segments[k].depth:
            break
        out.append(seg)
    return out


def _unwrapped(seg) -> bool:
    """The segment runs its program directly: only ``NAME=value`` words before it."""
    try:
        words = shlex.split(seg.raw.lstrip("({! \t"), comments=True)
    except ValueError:
        return False
    words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)] or [""]
    return os.path.basename(words[0]) == seg.exe


def cwd_known(segments, k: int, cwd) -> bool:
    """Whether segment ``k`` runs in ``cwd``: an absolute ``cwd``, no earlier step
    that changes directory, and the create and every segment enclosing it run their
    program directly."""
    return (
        isinstance(cwd, str)
        and os.path.isabs(cwd)
        and not any(s.exe in _CWD_CHANGERS for s in segments[:k])
        and all(_unwrapped(segments[i]) for i in (*_ancestors(segments, k), k))
    )


def _read_body(path: str) -> str | None:
    """The file's text, or None: not a regular file, over ``_MAX_BODY_BYTES``, or not
    UTF-8. The read itself is bounded (procfs reports ``st_size`` 0), and the open
    never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) <= _MAX_BODY_BYTES:
            chunk = os.read(fd, _MAX_BODY_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return None if len(data) > _MAX_BODY_BYTES else data.decode("utf-8")
    except (OSError, ValueError):  # ValueError includes UnicodeDecodeError
        return None
    finally:
        os.close(fd)


def _inert(seg) -> bool:
    """The segment runs a program on ``_INERT`` directly: its first raw word is that
    program (no ``NAME=value`` before it, no wrapper), and a grep has no ``--pre*``."""
    if seg.exe not in _INERT:
        return False
    try:
        words = shlex.split(seg.raw.lstrip("({! \t"), comments=True)
    except ValueError:
        return False
    if not words or os.path.basename(words[0]) != seg.exe:
        return False
    return not (seg.exe == "grep" and any(w.startswith("--pre") for w in seg.argv[1:]))


def _writes_nothing_before(earlier, raw: str) -> bool:
    """No earlier step can have written the body file: each runs an ``_INERT``
    program, and the raw command has no ``>`` (quoted or not) outside the two exempt
    whole tokens in ``_STDERR_TOKENS``, and no ``tee``."""
    words = _TOKEN_SPLIT.split(raw)
    if any(">" in w and w not in _STDERR_TOKENS for w in words) or _TEE.search(raw):
        return False
    return all(_inert(s) for s in earlier)


def _body_file(value, k, seg, segments, raw, cwd_ok, cwd, heredoc):
    """(body, where it was read from), or None when the body cannot be known."""
    if value == "-":
        if heredoc is not None and k == 0 and seg.depth == 0:
            return heredoc, "the heredoc"
        return None
    if not value or "$" in value or "`" in value or any(c in value for c in "*?["):
        return None
    if "<(" in raw or ">(" in raw:
        return None  # a process substitution runs with no segment of its own
    if k and not _writes_nothing_before(segments[:k], raw):
        return None  # an earlier step may write it: the file now is stale
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        if not cwd_ok or not cwd:
            return None
        path = os.path.join(cwd, path)
    body = _read_body(path)
    return None if body is None else (body, path)


def body(opts, k, seg, segments, raw, cwd_ok, cwd, heredoc):
    """(body, where it was read from) for the create at segment ``k``, or None.

    ``opts`` is ``shell_parse.gh_option_values`` over the create's argv, ``raw`` the
    whole command, and ``heredoc`` the body ``shell_parse.excise_data_heredoc`` cut
    from it (or None)."""
    # A substitution inside the create runs before gh reads the body, and may write
    # its file or build its text: unknown, except the `$(cat <<'EOF'` idiom's own cat.
    children = _children(segments, k)
    files = [v for n, v in opts if n in ("-F", "--body-file")]
    if files:
        if children:
            return None
        return _body_file(files[-1], k, seg, segments, raw, cwd_ok, cwd, heredoc)
    bodies = [v for n, v in opts if n in ("-b", "--body")]
    if not bodies:
        return None  # gh needs a terminal to ask for one
    value = bodies[-1] or ""
    if heredoc is not None and k == 0 and seg.depth == 0 and _IDIOM_VALUE.fullmatch(value):
        return heredoc, "the heredoc"
    # The argv is past quote removal, so whether bash expanded a `$` or a backtick
    # cannot be read from it: any one makes the body unknown, as for a -F path.
    if "$" in value or "`" in value or children:
        return None
    return value, "the --body argument"
