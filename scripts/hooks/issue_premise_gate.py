#!/usr/bin/env python3
"""A foreground `gh issue create` carries the answers the premise gate reads, before it posts.

WHY. The premise gate Action (``scripts/premise_gate.py``) labels an issue
``premises-missing`` only after it is public. MEASURED 2026-11-02 (``gh issue list
--state all --search "created:>=2026-10-28"``, 84 rows, a short read): 56 of 84
carried that label. Publication cannot be undone, and the fix is one edit to the
body, so this hook asks for it at the one moment it is cheap.

PreToolUse: Bash. It BLOCKS (stderr + exit 2) a foreground ``gh issue create`` (or
its alias ``new``) when the server gate's own rule, ``premise_gate.owed_answers``,
says the body owes answers: the six ``## Premise checks`` answers for a work issue,
the five ``## Verification`` answers for one labelled ``bug``. The block names the
missing section and numbers, shows the format, and points at ``python3
scripts/premise_gate.py check <number>``. It asks for work, never for approval, so
the agent can satisfy it alone.

A DISPATCHED SESSION IS NEVER BLOCKED. ``GENESIS_CC_SESSION=1`` (the stamp
``needs_user.is_dispatched`` reads) exits 0 before anything else runs, a broken
hook tree included. Supervised relay conversations carry the same stamp and pass
too: a coverage choice, made by the owner's foreground-only ruling.

IT BLOCKS ONLY WHAT IT CAN READ IN FULL. Everything it is unsure of passes in
silence, and the server label stays the backstop. A create is judged only when all
of these hold:

* the parse is clean (no blind spot), the segment's verb resolved, and the argv
  carries no flag the gh table does not model and no positional;
* no heredoc the parser did not excise opens before it. ``excise_data_heredoc``
  removes one proven data heredoc; any other heredoc's body lines come back as
  segments, so a line reading ``gh issue create …`` inside a file being written
  would otherwise be judged as a command (MEASURED: ``cat > f <<'EOF'``, ``gh -R
  o/r issue create -F - <<'EOF'`` and ``python3 - <<'EOF'`` bodies all do);
* it files something: not ``--help``, ``--template`` or ``--recover``, and
  ``--web``/``--editor`` read FALSE by their last value in pflag's boolean set (a
  value outside it, ``--web=yes``, is unknown; gh refuses it). gh files nothing for
  those without a terminal, nor for a create with no ``-t``/``--title``, so it must
  carry a title and a body flag;
* its target is the configured public repo (``github.user``/``github.public_repo``
  in ``~/.genesis/config/genesis.yaml``; none configured enforces nothing): an
  explicit ``-R``/``--repo``, or with none, the payload cwd's git remotes, read
  locally: the ``gh repo set-default`` choice (only the value ``base``; an
  ``OWNER/REPO`` value is unknown), else EVERY remote must name it.
  ``GH_HOST`` anywhere, and for an implicit target ``GH_REPO`` or an in-command
  ``GIT_*=``/``GH_*=`` assignment, an earlier ``cd``/``pushd``/``popd`` or ``git
  remote``/``git config``/``gh repo`` step, or a wrapper before ``gh`` each make
  the target unknown; an earlier function or alias definition makes ``gh`` itself
  unknown;
* the body is known: ``--body-file`` beats ``--body`` and the last of each wins (as
  gh reads them). A file is read only if it is regular, at most 256 KB (a bound on
  the READ; GitHub's own body limit fits) and UTF-8, the create is the command's
  FIRST step, and the text has no ``<(``/``>(``: any earlier step may write it
  without looking like a writer (``rg --pre``, a process substitution). ``-F -`` and
  the ``"$(cat <<'EOF'`` idiom (matched whole, so a concatenated tail is never
  dropped) are read from the heredoc the parser excised. A ``--body`` value with any
  ``$`` or backtick, or with a substitution step, is unknown: the argv is past quote
  removal, so a single-quoted literal cannot be told from an expansion;
* every ``-l``/``--label`` value is literal, and is read as gh resolves it: a name
  equal to ``bug`` in any case counts as ``bug`` (gh matches label names
  case-insensitively, so ``-l Bug`` files the repo's ``bug``);
* ``markdown-it`` is importable, so the answers are read as the server reads them;
* the answers are read within ``_PARSE_LIMIT_S``. markdown-it is super-linear on
  hostile input and no size cap keeps it inside the hook's budget, so the read has
  its own clock and parses each text once. Past the limit the create passes and the
  hook SAYS it did not check (``additionalContext``, the channel an exit 0 delivers).

FAIL DIRECTIONS. A broken ``hook_input`` refuses locally (exit 2, ``GUARD
DEGRADED``), like ``full_suite_guard``: nothing it could import can recover it.
The parser and the gate are imported LAZILY, after a cheap raw-text prefilter, so
a command that names no issue create never pays for them; when that import fails,
``hook_input.degraded_exit`` blocks a command whose raw text names ``issue
create``/``issue new`` and allows the rest with a notice. Any error while judging
exits 0 in silence.

The hook runs the main checkout's copy (the ``genesis-hook`` launcher), the Action
the default branch's; between a merge and a local pull the two can disagree. The
parity test proves agreement at one commit.

NOT COVERED: ``gh issue edit`` and ``gh api`` filings, creates in a dispatched
session, and every shape above that passes unread. The planned after-call
read-back in ``issue_dup_advisory`` reports on those.

KNOWN RESIDUALS (round-1 class audit; each is rare, and most err toward passing):

* a quoted tilde (``-F "~/x"``) is expanded here, while gh opens the literal path
  and fails, so this blocks a create gh would refuse anyway;
* a LATER step that runs concurrently (a pipeline sibling, a ``&`` job) or the
  create's own ``> f`` can change the file after this read;
* an unquoted glob in a ``-b`` word: the argv is past expansion;
* short ``=value`` booleans (``-e=false``, ``-w=false``) are tokens the gh table
  does not model, so the create passes unread;
* ``GH_HOST``/``GH_REPO`` set only in the Bash tool's shell, not this hook's
  environment, are not seen;
* ``--attach`` appends to the body gh sends; ``prefer_editor_prompt`` makes gh
  refuse every create without a terminal; a ``url.<x>.insteadOf`` rewrite changes
  the remote gh sees but not the ``remote.*.url`` read here;
* label whitespace: ``-l ' bug'`` is stripped here, while gh looks up the name as
  typed and fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

#: Stamped on every Genesis-dispatched session (``needs_user._DISPATCH_ENV``; a test
#: pins the two equal). Read with ``os`` alone, before any sibling import, so a
#: dispatched session passes even when the hook tree is broken.
_DISPATCH_ENV = "GENESIS_CC_SESSION"


def _dispatched() -> bool:
    return os.environ.get(_DISPATCH_ENV) == "1"


if __name__ == "__main__" and _dispatched():
    sys.exit(0)

_HOOKS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HOOKS)
try:
    from hook_input import degraded_exit, field, read_payload  # noqa: E402
except Exception:  # noqa: BLE001 — nothing it could import can recover it
    if __name__ != "__main__":
        raise
    try:
        sys.stderr.write(
            "GUARD DEGRADED (issue_premise_gate): shared hook_input is incompatible; "
            "BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)

#: The cheap prefilter: all three words, anywhere. Broad on purpose (`gh issue -R
#: o/r create` puts a flag between them); a hit only buys the parse.
_PREFILTER = (
    re.compile(r"(?<![\w-])gh(?![\w-])"),
    re.compile(r"\bissue\b"),
    re.compile(r"\b(?:create|new)\b"),
)
#: What a broken parser blocks: the narrow spelling, so the repair path survives.
_DEGRADED_GATED = r"\bissue\s+(?:create|new)\b"
#: A heredoc opener (`<<`, `<<-`), never a here-string (`<<<`).
_HEREDOC_OPENER = re.compile(r"(?<!<)<<(?!<)")
#: The `"$(cat <<'EOF'` idiom's value as the parser leaves it once the heredoc is
#: excised. Matched whole, so text concatenated after its `)"` is never dropped.
_IDIOM_VALUE = re.compile(r"\$\(cat <<'[A-Za-z_][A-Za-z0-9_]*'\n\)")
_CWD_CHANGERS = frozenset({"cd", "pushd", "popd"})
#: The most of a body file this hook reads. GitHub refuses an issue body over 65,536
#: characters, which is at most 262,144 bytes of UTF-8, so a real body fits, and
#: anything larger passes unread. It bounds the READ (and the memory it takes), NOT
#: the parse time: 64 KB of hostile markdown already outlasts the hook's budget (see
#: ``_PARSE_LIMIT_S``), and that limit is what keeps the parse inside it. Deliberately
#: not the plan gate's 8 MB cap.
_MAX_BODY_BYTES = 256 * 1024
#: The parse's own clock, in seconds. The hook is registered with a 10 s timeout
#: (.claude/settings.json), and a killed hook lets the create through with no word.
#: MEASURED 2026-11-03 on 65,536-byte bodies, before the single-parse change (four
#: parses each): `*a ` x N 9.7 s, `[` x N 7.5 s, `- a` lines 5.0 s, `1. x` lists
#: 2.4 s; at 256 KB four shapes passed 20 s. No size cap fits, so the parse stops at
#: 4 s, leaving room for interpreter start, imports and the 3 s ``git config`` bound.
#: One budget for the whole command, not one per create.
_PARSE_LIMIT_S = 4.0
#: `git config` normally answers in milliseconds; an ``include.path`` naming a FIFO
#: blocks it forever (MEASURED in review). 3 s bounds that well inside the hook's 10 s
#: registration timeout, so a hung read costs 3 s and passes, not the full 10 s.
_GIT_CONFIG_TIMEOUT_S = 3
#: Steps that can make `gh` mean something else later in the same command.
_REDEFINERS = frozenset({"alias", "unalias", "function", "hash", "enable"})
#: An in-command assignment that can move gh's target (GIT_DIR, GH_REPO, GH_HOST, …).
_TARGET_ASSIGNMENT = re.compile(r"\b(?:GIT|GH)_\w*=")
_WEB = frozenset({"--web", "-w"})
_EDITOR = frozenset({"--editor", "-e"})
#: String flags gh refuses here, in any spelling: ``--template`` beside a body, and
#: ``--recover`` without a terminal (MEASURED, gh 2.100).
_NO_FILING = frozenset({"--template", "-T", "--recover"})
_TITLE = frozenset({"-t", "--title"})
_OPTIONS = (
    frozenset({"-R", "--repo", "-l", "--label", "-b", "--body", "-F", "--body-file"})
    | _WEB
    | _EDITOR
    | _NO_FILING
    | _TITLE
)

sp = pg = plan_doc = discarded_write = None  # bound by _load()
#: Parse seconds spent so far, and the creates the clock stopped (reset per call).
_parse_spent = 0.0
_unchecked: list[str] = []


class _ParseTimeout(BaseException):
    """The parse ran past ``_PARSE_LIMIT_S``. A BaseException on purpose: the
    gate's readers catch ``Exception`` (``premise_gate._bug_form_headings`` does),
    and a swallowed timeout would let a half-finished read decide the verdict."""


def _on_alarm(_signum, _frame):
    raise _ParseTimeout


def _bounded_owed_message(issue: dict) -> str | None:
    """``pg.owed_message(issue)``, parsing each text once and within what is left of
    ``_PARSE_LIMIT_S``; None when the clock ran out (could not check).

    SIGALRM: the hook runs in the main thread, and markdown-it is pure Python, so the
    signal lands between bytecodes. The timer is cancelled and the previous handler
    restored on every path."""
    global _parse_spent  # noqa: PLW0603
    left = _PARSE_LIMIT_S - _parse_spent
    if left <= 0:
        return None
    started = time.monotonic()
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, left)
            with plan_doc.reuse_parses():
                return pg.owed_message(issue)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _ParseTimeout:
        return None
    finally:
        signal.signal(signal.SIGALRM, previous)
        _parse_spent += time.monotonic() - started


def _load() -> None:
    global sp, pg, plan_doc, discarded_write  # noqa: PLW0603
    import plan_doc as _plan_doc  # noqa: PLC0415
    import shell_parse as _sp  # noqa: PLC0415

    scripts = os.path.dirname(_HOOKS)
    if scripts not in sys.path:
        sys.path.append(scripts)  # after the hooks dir, so sibling names stay hooks'
    import premise_gate as _pg  # noqa: PLC0415

    sp, pg, plan_doc = _sp, _pg, _plan_doc
    try:  # binds the module global (declared above)
        import discarded_write  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — the note is cosmetic; the block must still land
        discarded_write = None


def _canonical() -> str | None:
    """The configured public repo as lower-case ``owner/repo``, or None.

    Same source and test seam as ``git_push_guard._canonical_public_repo``."""
    override = os.environ.get("_TEST_CANONICAL_PUBLIC_REPO")
    if override is not None:
        return _slug(override) if override.strip() else None
    try:
        import yaml  # noqa: PLC0415

        cfg = yaml.safe_load(Path("~/.genesis/config/genesis.yaml").expanduser().read_text()) or {}
        gh = cfg.get("github") or {}
        user, repo = (gh.get("user") or "").strip(), (gh.get("public_repo") or "").strip()
        return _slug(f"{user}/{repo}") if user and repo else None
    except Exception:  # noqa: BLE001 — no readable config enforces nothing
        return None


def _slug(value: str) -> str | None:
    """A github.com ``owner/repo`` (lower case) from a ``-R`` value or a remote URL."""
    v = value.strip()
    if not v or "$" in v or "`" in v:
        return None
    if v.startswith("git@github.com:"):
        v = "github.com/" + v[len("git@github.com:") :]
    v = v.split("://", 1)[-1]
    parts = [p for p in v.split("/") if p]
    if len(parts) == 3:
        if parts[0].rsplit("@", 1)[-1].lower() not in ("github.com", "www.github.com"):
            return None
        parts = parts[1:]
    if len(parts) != 2:
        return None
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return f"{parts[0]}/{repo}".lower()


def _cwd_targets(cwd: str, canonical: str) -> bool:
    """Whether gh, run in ``cwd`` with no ``-R``, would file in ``canonical``.

    Read from git config, locally. A ``gh repo set-default`` choice (``gh-resolved``)
    decides; otherwise EVERY remote must name the canonical repo, because gh files
    in one of them and which one (a fork clone's ``upstream`` before ``origin``) is
    not modelled here. The hook's environment is passed through unchanged (the
    ``genesis-hook`` launcher has already scrubbed ``GIT_DIR`` and the other
    location variables, and keeps ``GIT_CONFIG_*``), so git reads the config gh
    would. A read that fails or outlasts ``_GIT_CONFIG_TIMEOUT_S`` is unknown."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "config", "--get-regexp", r"^remote\."],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_CONFIG_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if out.returncode != 0:
        return False
    urls: dict[str, str] = {}
    chosen = []
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        name, _, attr = key[len("remote.") :].rpartition(".")
        if attr == "url":
            urls[name] = value
        elif attr == "gh-resolved":
            if value != "base":
                return False  # an OWNER/REPO value is where gh files; not read here
            chosen.append(name)
    names = chosen or list(urls)
    return bool(names) and all(_slug(urls.get(n, "")) == canonical for n in names)


def _positions(text: str, segments) -> list[int | None]:
    """Where each segment's raw text starts in ``text``, or None if not found.

    The cursor moves past a segment unless the next one is nested inside it, so a
    phantom line is never matched to an earlier quoted copy of itself."""
    out: list[int | None] = []
    cursor = 0
    for i, seg in enumerate(segments):
        idx = text.find(seg.raw, cursor) if seg.raw else -1
        if idx < 0:
            out.append(None)
            continue
        out.append(idx)
        nested = i + 1 < len(segments) and segments[i + 1].depth > seg.depth
        cursor = idx + 1 if nested else idx + len(seg.raw)
    return out


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


def _body_file(value, k, seg, text, cwd_known, cwd, heredoc):
    """(body, where it was read from), or None when the body cannot be known."""
    if value == "-":
        if heredoc is not None and k == 0 and seg.depth == 0:
            return heredoc, "the heredoc"
        return None
    if not value or "$" in value or "`" in value or any(c in value for c in "*?["):
        return None
    if k != 0 or "<(" in text or ">(" in text:
        return None  # an earlier or concurrent step may write it: the file now is stale
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        if not cwd_known or not cwd:
            return None
        path = os.path.join(cwd, path)
    body = _read_body(path)
    return None if body is None else (body, path)


def _body(opts, k, seg, segments, text, cwd_known, cwd, heredoc):
    # A substitution inside the create runs before gh reads the body, and may write
    # its file or build its text: unknown, except the `$(cat <<'EOF'` idiom's own cat.
    children = _children(segments, k)
    files = [v for n, v in opts if n in ("-F", "--body-file")]
    if files:
        if children:
            return None
        return _body_file(files[-1], k, seg, text, cwd_known, cwd, heredoc)
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


def _judge_create(k, seg, inv, segments, positions, text, openers, heredoc, cwd):
    """The block reason for one create segment, or None (pass)."""
    pos = positions[k]
    if openers and (pos is None or any(o < pos for o in openers)):
        return None  # possibly a heredoc body line, not a command
    argv = seg.argv
    if inv.unmodelled or inv.positionals or sp.gh_requests_help(argv):
        return None
    opts = sp.gh_option_values(argv, _OPTIONS)
    names = {n for n, _ in opts}
    if sp.gh_bool(opts, _WEB) is not False or sp.gh_bool(opts, _EDITOR) is not False:
        return None  # a browser or an editor files nothing here; a non-bool value fails
    if names & _NO_FILING or not names & _TITLE:
        return None  # without a terminal gh refuses these, and a create with no title
    canonical = _canonical()
    if not canonical or "GH_HOST" in os.environ or "GH_HOST" in text:
        return None
    earlier = segments[:k]
    if any(s.exe in _REDEFINERS or s.exe.endswith("()") for s in earlier):
        return None  # `gh` may be a function or an alias by now
    cwd_known = (
        isinstance(cwd, str)
        and os.path.isabs(cwd)
        and not any(s.exe in _CWD_CHANGERS for s in earlier)
        and all(_unwrapped(segments[i]) for i in (*_ancestors(segments, k), k))
    )
    repos = [v for n, v in opts if n in ("-R", "--repo")]
    if repos:
        if _slug(repos[-1] or "") != canonical:
            return None
    else:
        remote_steps = any(
            (s.exe == "git" and ("remote" in s.argv[1:] or "config" in s.argv[1:]))
            or (s.exe == "gh" and getattr(sp.gh_command(s.argv), "group", "") == "repo")
            for s in earlier
        )
        moved = "GH_REPO" in os.environ or _TARGET_ASSIGNMENT.search(text)
        if moved or remote_steps or not cwd_known:
            return None
        if not _cwd_targets(cwd, canonical):
            return None
    found = _body(opts, k, seg, segments, text, cwd_known, cwd, heredoc)
    if found is None:
        return None
    labels: list[str] = []
    for n, v in opts:
        if n in ("-l", "--label"):
            if v is None or "$" in v or "`" in v:
                return None
            # gh matches a name to the repo's label case-insensitively: `Bug` files `bug`
            labels += [
                pg.BUG_LABEL if x.strip().casefold() == pg.BUG_LABEL else x.strip()
                for row in csv.reader(io.StringIO(v))
                for x in row
            ]
    if plan_doc._parser() is None:
        return None  # the server reads the answers with markdown-it; line rules can differ
    issue = {
        "body": found[0],
        "labels": labels,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    message = _bounded_owed_message(issue)
    if message is None:
        _unchecked.append(
            f"Premise check did not run for `gh issue {inv.subcommand}` (segment {k + 1} of "
            f"{len(segments)} in this command): reading the answers in the body (from "
            f"{found[1]}) outlasted this hook's {_PARSE_LIMIT_S:g} s parse limit, so it was "
            f"not checked. The server gate still labels it `{pg.MISSING_LABEL}` if answers "
            "are owed; after filing, `python3 scripts/premise_gate.py check <number>` shows "
            "what it reads."
        )
        return None
    if not message:
        return None
    return (
        f"BLOCKED (issue premise check): `gh issue {inv.subcommand}` (segment {k + 1} of "
        f"{len(segments)} in this command) would publish an issue without the answers the "
        f"premise gate requires; it labels such an issue `{pg.MISSING_LABEL}` (body read "
        f"from {found[1]}):\n"
        + message
        + "After filing, `python3 scripts/premise_gate.py check <number>` shows what the "
        "gate reads."
    )


def _judge(command: str, cwd) -> list[str]:
    excised = sp.excise_data_heredoc(command)
    text, heredoc = (excised[0], excised[1]) if excised else (command, None)
    segments, blind = sp.analyze_checked(text)
    if blind is not None or not segments:
        return []
    # An excised command keeps only the opener it proved; every other `<<` is unconsumed.
    openers = [] if excised else [m.start() for m in _HEREDOC_OPENER.finditer(text)]
    positions = _positions(text, segments)
    reasons = []
    for k, seg in enumerate(segments):
        if seg.exe != "gh" or seg.verb_unresolved:
            continue
        inv = sp.gh_command(seg.argv)
        if (
            inv is None
            or inv.group != "issue"
            or inv.subcommand not in sp.GH_ISSUE_CREATE_SUBCOMMANDS
        ):
            continue
        reason = _judge_create(k, seg, inv, segments, positions, text, openers, heredoc, cwd)
        if reason:
            reasons.append(reason)
    return reasons


def _say_unchecked(context: str) -> None:
    """The could-not-check note on the ALLOW path. Claude Code drops an exit-0
    hook's stderr, so it goes out as ``additionalContext`` (as ``issue_dup_advisory``
    says its own could-not-check). Cosmetic: a failure here never changes exit 0."""
    with contextlib.suppress(Exception):
        from hook_output import print_json_bounded  # noqa: PLC0415

        print_json_bounded(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}},
            text_keys=("hookSpecificOutput.additionalContext",),
        )


def main() -> int:
    if _dispatched():
        return 0
    payload = read_payload()
    command = field(payload, "command")
    if not all(p.search(command) for p in _PREFILTER):
        return 0
    try:
        _load()
    except Exception as exc:  # noqa: BLE001 — degraded_exit decides; it never returns
        sys.stdin = io.StringIO(json.dumps(payload))  # it reads the payload itself
        degraded_exit("issue_premise_gate", gated=_DEGRADED_GATED, exc=exc)
    if discarded_write is not None:
        with contextlib.suppress(Exception):  # a raise here would exit 1 = NON-blocking
            discarded_write.remember(command)
    global _parse_spent  # noqa: PLW0603
    _parse_spent = 0.0
    _unchecked.clear()
    try:
        reasons = _judge(command, payload.get("cwd"))
    except Exception:  # noqa: BLE001 — could not check: the server label is the backstop
        return 0
    if not reasons:
        if _unchecked:
            _say_unchecked("\n\n".join(_unchecked))
        return 0
    print("\n\n".join(reasons + _unchecked), file=sys.stderr)
    if discarded_write is not None:
        with contextlib.suppress(Exception):
            discarded_write.warn()  # a compound's earlier steps did not run either
    return 2


if __name__ == "__main__":
    sys.exit(main())
