#!/usr/bin/env python3
"""File a Genesis-repo issue on the SHARED public tracker, safely.

Four cross-model review rounds on this procedure — three while it was prose, one
after it became this script — found the same shape of defect each time: a step
CLAIMING MORE CERTAINTY THAN IT HAD. Every guarantee below exists because a
reviewer showed the previous version asserting something it could not know.

Targeting (rounds 1-3, prose):
  * `--repo <install-user>/<repo>` resolved to the operator's OWN FORK.
  * Bare `gh` commands re-resolve their target from the current directory, so a
    `cd` between check and post redirects an irreversible write.
  * `gh repo view` with no argument reports the CURRENT repo — on a fork clone
    that is the fork, where the operator IS ADMIN, so a permission check on it
    PASSES and the issue lands where nobody reads it.

Certainty at boundaries (round 4, this script):
  * A capped listing is not proof of absence. The duplicate check paginates to
    exhaustion; it never infers "no duplicate" from a truncated window.
  * One `parent` hop is not the fork-network root. A fork of a fork needs the
    chain walked. (This `gh` exposes `parent` but NOT `source`, so walking is
    the available mechanism, not a stylistic preference.)
  * Check-then-act is not atomic. Lookup and creation are serialised under a
    per-tracker lock, so two concurrent sessions cannot both pass the duplicate
    check and both post.
  * A failed `gh issue create` does NOT prove nothing was posted. Every route
    the process SURVIVES — nonzero exit, timeout, SIGINT/SIGTERM, an rc=0 with
    no URL — is resolved against the tracker rather than inferred from the exit
    status. INDETERMINATE survives only when that reconciling lookup ALSO
    fails. A SIGKILL cannot be caught by anything, so it remains the one route
    that can leave an unreported issue; that is a property of SIGKILL, not a
    gap this script can close.
  * The exit code itself is guaranteed. CPython flushes stdout during shutdown
    AFTER main() returns, and a failed flush exits 120 — silently replacing a
    successful result. `_finish()` flushes while we can still react.

Shell safety: everything runs through argv lists with `shell=False`. A title
containing backticks or `$(...)` — ordinary in a technical title — would execute
if interpolated into a command line, after the privacy scan, with its output in
the public title. The absence of a shell is the mechanism, not a precaution.

This does NOT decide whether the issue SHOULD be public. The caller owns the two
hard limits in CLAUDE.md — explicit user approval for an irreversible post, and
never publishing an unfixed security defect.

Usage:
    file_tracker_issue.py --title-file T --body-file B --area area:memory \\
        --difficulty "help wanted" [--dry-run]

Exit codes: 0 filed (or dry-run OK) · 2 refused, nothing posted · 3 duplicate
found · 4 INDETERMINATE — a post may or may not exist, reconcile before retrying
· 1 unexpected error.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

# Mirrors of the fail-closed sets enforced server-side by
# ``src/genesis/mcp/health/contributor_issue.py``. Duplicated deliberately —
# importing that module pulls the whole MCP stack into a standalone script — and
# pinned against drift by tests/test_scripts/test_file_tracker_issue.py, which
# asserts these equal the canonical sets exactly.
AREA_LABELS = frozenset(
    {
        "area:memory",
        "area:dashboard",
        "area:runtime",
        "area:guardian",
        "area:autonomy",
        "area:channels",
        "area:knowledge",
        "area:eval",
        "area:other",
    }
)
DIFFICULTY_LABELS = frozenset(
    {
        "good first issue",
        "first-timers-only",
        "needs-genesis-instance",
        "help wanted",
    }
)
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})

# A fork chain deeper than this is pathological; bound the walk so a cycle or a
# misbehaving API can never spin.
MAX_FORK_DEPTH = 10

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

#: Compiled per home value. The home cannot change within a process run in
#: practice, but caching is keyed by value rather than assumed-constant.
_HOME_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _redact_home(text: str) -> str:
    """Replace this install's home directory with ``~``.

    Every refusal below goes to stderr — terminal scrollback, session
    transcripts, CI logs — and the account name in a home path is exactly the
    identifier the contribution sanitizer exists to keep out of a public
    artefact. Issue #2152 fixed one such refusal by dropping the value
    entirely. That works where the value is a path we CONSTRUCTED and can
    reconstruct from a docstring; it does not work for a subprocess's stderr,
    where the path is the diagnostic. Substituting keeps "which file" and loses
    "whose account".

    SCOPE, stated so nobody reads more into it: this redacts THIS install's
    home directory and nothing else. It is not a general scrubber, it knows
    nothing about third-party absolute paths a subprocess might print, and it
    is not a substitute for the privacy scan that guards the issue BODY. It
    closes the one class #2152 named, on the surfaces that still had it.

    A home of ``/`` is left alone: substituting there would turn every absolute
    path in the message into ``~``-prefixed nonsense, and an install whose home
    is the filesystem root has no account name in it to protect.
    """
    try:
        home = str(Path.home())
    except (RuntimeError, OSError, KeyError):
        # Path.home() resolves HOME or the passwd entry, and a stripped
        # environment with no passwd entry raises. A refusal must never fail
        # while trying to make itself safe.
        return text
    if not home or home == os.sep:
        return text
    # Anchored, not a bare str.replace. MEASURED: with a home of `/home/jay`,
    # `cannot read /home/jayson/keys/id_rsa` became `cannot read ~son/keys/...`
    # -- it half-destroys a DIFFERENT account's name and names a file that does
    # not exist, which is worse than the leak in the one direction that costs a
    # debugging round. A URL containing the same characters
    # (`https://example.invalid/home/jay/docs`) was mangled the same way.
    # Left boundary: not preceded by a word character or a slash, so
    # `/var/home/jay/...` is left alone. Right boundary: the path must END
    # there or continue with a separator, so `/home/jayson` does not match.
    #
    # THE RIGHT BOUNDARY IS A TRADEOFF, and it is deliberately asymmetric.
    # Almost every byte is legal in a POSIX path, so nothing in the text can
    # prove where a path ends. Erring one way MANGLES a valid diagnostic (it
    # names a `~` path that does not exist); erring the other LEAKS the account
    # name. Leaking is worse, so ambiguity resolves toward redaction.
    #
    #   `/` `\s` end-of-string  -- always a boundary.
    #   `.!?,:;`                -- a boundary only when whitespace or the end
    #                              follows, because all of them occur inside
    #                              real directory names. MEASURED: without that
    #                              qualifier `<home>:2`, `<home>;x` and
    #                              `<home>,v` were rewritten, and `<home>.config`
    #                              lost its first component.
    #   `'` `\"` `)`             -- always a boundary, ACCEPTED as the lossy
    #                              side. Diagnostics quote paths (`open
    #                              '<home>': denied`), and requiring whitespace
    #                              after the quote would miss that and leak. The
    #                              cost is that a directory literally named
    #                              `jay's` would be rewritten; that is a
    #                              misleading message, not a disclosure.
    return _HOME_RE_CACHE.setdefault(
        home, re.compile(rf"(?<![\w/]){re.escape(home)}"
        rf"(?=/|$|\s|['\")]|[.!?,:;](?:\s|$))")
    ).sub("~", text)


class Refused(Exception):
    """A precondition failed. NOTHING was posted — this is a hard guarantee."""


class Indeterminate(Exception):
    """The post may or may not exist. Reconcile against the tracker.

    Deliberately NOT a subclass of Refused: the entire point is that a caller
    must not treat it as "nothing happened".
    """


#: Every `gh` call is bounded. A tracker large enough to exceed this on the
#: paginated listing (order 10k+ issues) will time out rather than hang — that
#: surfaces as a Refused, never as a silent partial read.
GH_TIMEOUT_S = 120


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Execute argv with NO shell. The absence of a shell is the point."""
    return subprocess.run(  # noqa: S603 - argv list, shell=False, no interpolation
        list(argv), capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False
    )


def _finish(code: int) -> int:
    """Return `code` and make sure the interpreter cannot override it.

    CPython flushes stdout during shutdown, AFTER main() returns. If that flush
    fails the process exits 120, silently replacing whatever we returned —
    MEASURED: a run whose issue was created successfully exited 120 under
    `> /dev/full`, i.e. a confirmed post reported as an undocumented failure.

    This function is deliberately TOTAL: it is the guarantee, so it must not be
    able to raise. Every stdout shape is handled —

      * `sys.stdout is None`      (launched with fd 1 closed, e.g. `>&-`).
        Nothing to flush and nothing for shutdown to flush either.
      * a closed stream           (`flush()` raises ValueError, and so does the
        `fileno()` we would use to recover — the recovery path needs its own
        guard, which an earlier version of this function did not have).
      * a full or broken pipe     (flush raises OSError; put a usable fd under
        it so shutdown has nothing to fail on).
    """
    # BOTH streams: CPython flushes stderr at shutdown too, so a diagnostic
    # written to a failing stderr replaces the exit code with 120 exactly as a
    # failing stdout does. MEASURED: the missing-title path returned 120 instead
    # of 2 with stderr on /dev/full.
    for name in ("stdout", "stderr"):
        _repair_stream(getattr(sys, name, None))
    return code


def _repair_stream(stream) -> None:
    """Flush one stream, or put a usable fd under it. Never raises."""
    if stream is None:
        return
    try:
        stream.flush()
        return
    except (OSError, ValueError):
        pass
    except Exception:  # noqa: BLE001 - a guarantee must never raise
        return
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return  # no fd to repair; shutdown has nothing usable to flush
    with contextlib.suppress(OSError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, fd)


def _gh_json(run: Runner, argv: Sequence[str]) -> dict:
    proc = run(argv)
    if proc.returncode != 0:
        raise Refused(
            _redact_home(
                f"`{' '.join(argv)}` failed: {proc.stderr.strip() or proc.returncode}"
            )
        )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise Refused(
            _redact_home(f"`{' '.join(argv)}` returned unparseable JSON: {exc}")
        ) from exc
    if not isinstance(data, dict):
        # `gh` returning a list or null here would surface as an AttributeError
        # from the caller's .get(), which is not in any handler's except clause.
        raise Refused(
            _redact_home(
                f"`{' '.join(argv)}` returned {type(data).__name__}, expected an object"
            )
        )
    return data


def _parent_slug(data: dict) -> str | None:
    parent = data.get("parent") or {}
    owner = (parent.get("owner") or {}).get("login")
    name = parent.get("name")
    return f"{owner}/{name}" if owner and name else None


def resolve_tracker(run: Runner = _run) -> str:
    """The SHARED tracker slug — the ROOT of the fork network.

    Walks the parent chain rather than taking a single hop: a fork of a fork
    would otherwise resolve to the intermediate fork, where the operator may
    well have write access, so the permission check passes and the post lands on
    the wrong tracker. This `gh` exposes ``parent`` but not ``source``, so the
    walk is the available mechanism.
    """
    data = _gh_json(run, ["gh", "repo", "view", "--json", "isFork,parent,nameWithOwner"])
    slug = data.get("nameWithOwner")
    if not slug:
        raise Refused("could not resolve a repository from this directory")

    depth = 0
    while data.get("isFork"):
        depth += 1
        if depth > MAX_FORK_DEPTH:
            raise Refused(
                f"fork chain deeper than {MAX_FORK_DEPTH} from {slug} — refusing rather "
                "than guessing which repository is the shared tracker"
            )
        parent = _parent_slug(data)
        if not parent:
            raise Refused(
                f"{slug} is a fork but its parent could not be resolved — refusing to "
                "file into the fork"
            )
        slug = parent
        data = _gh_json(run, ["gh", "repo", "view", slug, "--json", "isFork,parent,nameWithOwner"])
    return str(slug)


def check_permission(slug: str, run: Runner = _run) -> str:
    """Verify write access ON THE RESOLVED SLUG. Identity is not permission."""
    data = _gh_json(run, ["gh", "repo", "view", slug, "--json", "viewerPermission"])
    perm = data.get("viewerPermission")
    if perm not in WRITE_PERMISSIONS:
        raise Refused(
            f"no write access to {slug} (viewerPermission={perm!r}). "
            "Keep this as a local follow-up until a maintainer carries it across."
        )
    return str(perm)


def _normalize(title: str) -> str:
    """Fold case/whitespace/unicode so trivially-different titles compare equal."""
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def find_duplicate(slug: str, title: str, run: Runner = _run) -> int | None:
    """Exact normalized-title match over an EXHAUSTIVE listing.

    Two things this deliberately does not do:

    * It does not use ``--search``. A title containing ``repo:`` / ``is:`` /
      ``label:`` would be parsed as query syntax rather than searched literally.
    * It does not cap. A capped window that happens to exclude the match is
      indistinguishable from no match — absence from a truncated read is not
      absence. ``--paginate`` walks every page; the ``pull_request`` filter is
      required because GitHub's issues endpoint returns PRs too.
    """
    try:
        proc = run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{slug}/issues?state=all&per_page=100",
                "--jq",
                '.[] | select(has("pull_request")|not) | {number,title}',
            ]
        )
    except subprocess.SubprocessError as exc:
        # TimeoutExpired is NOT an OSError and is caught by no handler upstream.
        raise Refused(
            _redact_home(
                f"duplicate check on {slug} did not complete ({exc}). Refusing to file — "
                "a lookup that never finished is not 'no duplicate'."
            )
        ) from exc
    # returncode is checked BEFORE stdout is parsed, deliberately: `gh api
    # --paginate` writes each page as it arrives and only then reports a later
    # page's failure, so parsing first would read a PARTIAL listing as an
    # exhaustive one — the exact defect this function's docstring promises
    # against. Do not reorder.
    if proc.returncode != 0:
        raise Refused(
            _redact_home(
                f"duplicate check failed on {slug}: "
                f"{proc.stderr.strip() or proc.returncode}. "
                "Refusing to file — a failed lookup is not 'no duplicate'."
            )
        )
    target = _normalize(title)
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            issue = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Refused(
                _redact_home(
                    f"duplicate check returned unparseable JSON: {exc}. Refusing — a "
                    "partial read cannot prove absence."
                )
            ) from exc
        if _normalize(str(issue.get("title", ""))) == target:
            return int(issue["number"])
    return None


def validate_labels(area: str, difficulty: str) -> list[str]:
    """Both classes are mandatory, and a nonexistent label makes `gh` fail."""
    if area not in AREA_LABELS:
        raise Refused(
            f"{area!r} is not a real area label. Allowed: "
            f"{', '.join(sorted(AREA_LABELS))}. Use area:other if none fits."
        )
    if difficulty not in DIFFICULTY_LABELS:
        raise Refused(
            f"{difficulty!r} is not a real difficulty/environment label. Allowed: "
            f"{', '.join(sorted(DIFFICULTY_LABELS))}."
        )
    return [area, difficulty]


def _lock_path(slug: str) -> Path:
    """Per-tracker lock file, at a path that is STABLE across sessions.

    No tempdir fallback, deliberately. ``tempfile.gettempdir()`` follows
    ``TMPDIR``, and on this project a Claude Code session has ``TMPDIR`` pointed
    at its own cc-tmp BY DESIGN while a manual shell has ``/tmp`` — so a
    fallback would hand two concurrent sessions DIFFERENT lock files for the
    same tracker, both would pass duplicate detection, and both would post. A
    lock whose identity varies by caller is not a lock; refuse instead.
    """
    # NOTE: unlinking this file while a peer holds it destroys mutual
    # exclusion — flock is on the inode, so the next caller creates a fresh
    # one and locks nothing. `scripts/disk_hygiene.sh` does not currently reap
    # ~/.genesis/locks; if a broad sweep is ever added, exclude this path.
    digest = hashlib.sha256(slug.encode()).hexdigest()[:16]
    base = Path.home() / ".genesis" / "locks"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Refused(
            f"cannot establish the per-install lock directory ({type(exc).__name__}). "
            "Refusing rather than using a TMPDIR-dependent fallback, which would "
            "let two sessions lock different files and both post."
        ) from exc
    return base / f"tracker-issue-{digest}.lock"


@contextmanager
def tracker_lock(slug: str):
    """Serialise duplicate-check + create for one tracker on this install.

    Without it, two concurrent foreground sessions can both pass the duplicate
    check before either posts, and both then post — defeating the check that
    exists precisely for that case. Cross-INSTALL races remain possible; that is
    inherent to client-side dedup and is stated here rather than hidden.
    """
    path = _lock_path(slug)
    with open(path, "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Blocking silently for a peer's full paginated listing looks like a
            # hang. Say so, then wait.
            print(_redact_home(f"waiting for the {slug} tracker lock…"), file=sys.stderr)
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _reconcile_uncertain_create(slug: str, title: str, cause: str, run: Runner) -> str:
    """Resolve an uncertain create by asking the TRACKER, not the exit status.

    Every way a create's outcome can be lost — a nonzero exit, a timeout, a
    killed process — is another path where the issue may exist anyway. Inferring
    the answer from the call means patching each path as it is discovered.
    Asking the tracker answers all of them the same way, from ground truth.

    Raises Indeterminate ONLY when the reconciling lookup itself fails, which is
    the one case where the outcome is genuinely unknowable here.
    """
    # PRECONDITION, asserted by the caller's flow rather than assumed: main()
    # ran the duplicate check under the SAME tracker lock immediately before the
    # create, and it found nothing. So an issue carrying this exact title now is
    # necessarily the one this invocation created — not a pre-existing one.
    try:
        found = find_duplicate(slug, title, run)
    except (Refused, OSError, ValueError) as exc:
        # Not just Refused: the reconciling `gh` may fail to START at all
        # (FileNotFoundError during an executable swap, ENOMEM, a decode error).
        # Those are OSError/ValueError, which main()'s generic handler turns into
        # exit 1 — "unexpected error" — when the truth is that the issue MAY
        # exist. Everything that leaves the outcome unknown must land on exit 4.
        raise Indeterminate(
            f"{cause}, and the reconciling lookup then failed ({exc}). The issue MAY "
            f"exist on {slug}. Check the tracker for this exact title BEFORE retrying "
            "or recording a local row."
        ) from exc
    if found is not None:
        return f"{slug}#{found} (reconciled: {cause}, but the issue EXISTS)"
    raise Refused(
        _redact_home(
            f"{cause}. Reconciled against {slug}: no issue with this title exists, "
            "so nothing was posted. Safe to retry."
        )
    )


def privacy_scan(title: str, body: str) -> list[str]:
    """Scan the issue text for things that must never reach a public tracker.

    Returns the WARN-level messages (for display) and raises :class:`Refused`
    on anything BLOCK-severity. Nothing is posted on a refusal, by the same
    guarantee every other Refused carries.

    WHY THIS EXISTS. CI's leak detector reads PR DIFFS and PR BODIES. Nothing
    reads issue bodies — an issue is a terminal egress surface with no backstop
    whatsoever, and until now this script's only defence was the author
    remembering. ``scan_prose`` is the sanctioned reader for that shape: it runs
    the raw-string detectors that are meaningful on prose (portability classes
    such as IPs and ``/home/<user>`` paths, personal emails outside the
    allowlist, install fingerprints, and the required ``detect-secrets`` floor)
    and deliberately skips the diff-STRUCTURAL scanners, which describe a
    unified diff's shape rather than prose.

    Do NOT substitute ``scan_diff`` here. It routes through ``parse_diff``,
    which drops every non-``+`` line, so plain text yields zero added lines and
    returns ok=True regardless of content — a fail-OPEN hole through a
    fail-CLOSED component. Its own docstring says so.

    FAIL-CLOSED IS THE POINT, including when the scanner itself is missing: a
    missing ``detect-secrets`` binary and a missing fingerprint file are both
    BLOCK findings, so an install that cannot scan cannot file.

    The commonest way to hit that is the interpreter, not the install.
    ``detect-secrets`` is resolved from ``Path(sys.executable).parent`` and is
    installed only in the repo venv, so a bare ``python3 scripts/…`` refuses
    every issue — correctly, but for a reason that is about the invocation
    rather than the draft. Run it as ``.venv/bin/python scripts/…``; the
    refusal below says so. An earlier revision of this script tried to REPAIR
    that by re-executing itself under the venv, and it was cut: the predicate
    it needed ("am I already the right interpreter?") compared resolved
    interpreter paths, and a ``--symlinks`` venv makes ``venv/bin/python``
    resolve to the same real binary as ``/usr/bin/python3`` — so the test was
    true exactly when the repair was needed, and the mechanism never fired on
    any install. Do not reintroduce it without comparing UNRESOLVED bin
    directories, and do not reintroduce it at all before asking whether the
    documentation line is the real fix.
    """
    src = str(Path(__file__).resolve().parent.parent / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from genesis.contribution.sanitize import scan_prose
    except ImportError as exc:
        raise Refused(
            _redact_home(
                f"cannot import the privacy scanner ({exc}). An issue is a public, "
                "irreversible post with no CI backstop, so it is not filed unscanned. "
                "Run from the repo with its venv: .venv/bin/python scripts/file_tracker_issue.py"
            )
        ) from exc

    # Scanned SEPARATELY, one call per field, because the line number is the
    # only locator a withheld finding leaves the author. Concatenating them put
    # the title on line 1 and shifted every body line by one, so the refusal
    # named a line the author would read as clean and edit the wrong text —
    # confidently wrong is worse than absent when the content is withheld by
    # design. Separate calls make each number native to the field it names, and
    # they retire the question of whether a value could splice across the seam
    # rather than answering it.
    blocking: list[tuple[str, object]] = []
    surfaced: list[tuple[str, object]] = []
    for where, text in (("title", title), ("body", body)):
        result = scan_prose(text)
        blocking += [(where, f) for f in result.blocking()]
        surfaced += [(where, f) for f in result.findings]
    if blocking:
        raise Refused(
            f"privacy scan BLOCKED this issue ({len(blocking)} finding(s)): "
            + "; ".join(_describe_finding(f, where) for where, f in blocking)
            + _interpreter_hint([f for _, f in blocking])
        )
    return [_describe_finding(f, where) for where, f in surfaced]


#: Findings whose ``detail`` is a fixed SENTINEL describing the scanner's own
#: state rather than any scanned content. Only these may have their message
#: rendered — see :func:`_describe_finding`.
_INFRASTRUCTURE_DETAILS = frozenset(
    {
        "missing_binary",
        "scan_error",
        "nonzero_exit",
        "missing_fingerprint_file",
    }
)


def _describe_finding(f, where: str = "draft") -> str:
    """Render a finding WITHOUT reproducing what it matched.

    A refusal is printed to stderr, which on this system lands in terminal
    scrollback, session transcripts and CI logs. Reproducing the matched text
    there takes a value that was successfully blocked from a public tracker and
    writes it somewhere durable instead — the leak the scan exists to prevent,
    moved one surface over rather than stopped (Codex P1).

    It is not hypothetical. ``sanitize.py`` stores the first 120 characters of
    the offending LINE in ``detail`` for secrets, portability hits and
    fingerprint matches, and the email scanner puts the address in ``message``.
    An earlier version of this function interpolated both.

    So: kind, scanner and line number only. The message survives ONLY for
    findings whose detail is one of the fixed sentinels above, which describe
    the scanner failing rather than anything it read — those carry no scanned
    content and are exactly the ones an operator needs spelled out, because
    they are about the environment rather than the draft.
    """
    if f.detail in _INFRASTRUCTURE_DETAILS:
        return f"[{f.kind.value}] {f.message}"
    # Deliberately no message and no detail: naming the RULE is actionable,
    # quoting the MATCH is a second copy of the secret. The FIELD and the line
    # are what is left, so they have to be right — see privacy_scan for why
    # they are scanned separately.
    at = f" line {f.line}" if f.line else ""
    return f"[{f.kind.value}] matched by {f.scanner or 'a scanner'} in the {where}{at} (content withheld)"


def _interpreter_hint(blocking) -> str:
    """Name the invocation when the refusal is about the interpreter.

    An operator reading "BLOCKED" about text they just wrote will edit the
    text, which cannot help when the real cause is that this python has no
    detect-secrets. Keyed on the SENTINEL rather than on message wording, so a
    reworded message cannot silently drop the hint.
    """
    if any(f.detail == "missing_binary" for f in blocking):
        return (
            "  This one is about the INTERPRETER, not your draft: re-run as "
            "`.venv/bin/python scripts/file_tracker_issue.py …`."
        )
    return ""


def _snapshot(data: bytes) -> tuple[str, str]:
    """Write *data* where only this process can reach it. Returns (dir, file).

    This is what makes "the posted bytes are the scanned bytes" a STRUCTURAL
    claim rather than a timing one. Handing gh the caller's path means gh
    reopens a file we do not control, so an editor save or a symlink swap
    between the last check and gh's open publishes text nothing scanned — and
    no amount of re-checking closes that, because the window ends inside
    another process (Codex P1).

    Built WITHOUT ``tempfile``, deliberately, and not merely to satisfy the
    import guard this module already carries. ``mkdtemp`` honours ``TMPDIR``,
    and on this project a Claude Code session has ``TMPDIR`` pointed at a small
    policed volume that must not be filled while a manual shell has ``/tmp`` —
    the same caller-dependence that made a tempdir fallback wrong for the lock
    path, plus a disk hazard, since a draft body has no size bound. A fixed
    location under ``~/tmp`` is both stable and the documented home for large
    transient files.

    ``os.mkdir`` with an explicit 0700 creates-or-fails atomically, so the
    directory cannot be pre-created or swapped by another user between here and
    gh's read. The name carries 16 bytes of urandom rather than the pid alone,
    which a peer could predict.

    CREATION IS ALL-OR-NOTHING. Anything that fails after the directory exists
    removes it before propagating, because the caller learns the directory's
    name only from a successful RETURN — a partial write would otherwise leave
    a copy of the body on disk that nothing knows to clean up (Devin, Codex P2).
    ``BaseException``, not ``Exception``: a SIGINT or a disk-full landing
    between the mkdir and the chmod is exactly when the leftover matters, and
    KeyboardInterrupt is not an Exception.

    Cleanup of the SUCCESSFUL case is the caller's, in a finally — and it must
    never convert a successful post into a failure, which is why that removal
    is suppressed.
    """
    parent = Path.home() / "tmp"
    parent.mkdir(parents=True, exist_ok=True)
    d = parent / f"issue-body-{os.getpid()}-{os.urandom(16).hex()}"
    os.mkdir(d, 0o700)
    try:
        path = d / "body.md"
        # Mode at CREATION, not after: a write-then-chmod leaves the body
        # world-readable for the width of that window, under a 0700 directory
        # that makes it unreachable but not unreadable to the owner's own
        # processes. os.open with 0o600 closes it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # LOOP, because os.write returns a COUNT and a short write is not an
            # error. Discarding that count lets gh post a TRUNCATED body while
            # the script reports success — which would make this function's own
            # guarantee ("the posted bytes are the scanned bytes") false in
            # exactly the direction nothing detects: posted becomes a proper
            # subset of scanned. I could not reproduce a short write on a
            # regular file here, and did not try to fill the disk; the loop is
            # correct on the API's contract rather than on an observation, and
            # the `except BaseException` below covers only the RAISING form of
            # disk-full, not this one.
            written = 0
            while written < len(data):
                n = os.write(fd, data[written:])
                if n <= 0:
                    raise OSError(errno.EIO, "short write staging the scanned body")
                written += n
        finally:
            os.close(fd)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return str(d), str(path)


def create_issue(
    slug: str, title: str, body_path: str, labels: Sequence[str], run: Runner = _run
) -> str:
    """Post it. Title and body travel as argv, never through a shell.

    An uncertain outcome is RESOLVED, not reported: a nonzero exit or a timeout
    both mean "the post may exist", so the tracker is queried and the real answer
    returned. Indeterminate survives only for the case where that query also
    fails.
    """
    argv = ["gh", "issue", "create", "--repo", slug, "--title", title, "--body-file", body_path]
    for label in labels:
        argv += ["--label", label]
    try:
        proc = run(argv)
    except subprocess.SubprocessError as exc:
        # TimeoutExpired is the common case and is EXACTLY when the server may
        # have committed the issue. Catch the whole SubprocessError family: none
        # of it is an OSError, so none of it is caught anywhere upstream.
        return _reconcile_uncertain_create(
            slug,
            title,
            _redact_home(f"gh issue create on {slug} did not complete ({exc})"),
            run,
        )
    except KeyboardInterrupt:
        # SIGINT/SIGTERM after the request went out. KeyboardInterrupt is a
        # BaseException, so it escapes every `except Exception` handler — and it
        # arrives precisely when the post may already exist.
        return _reconcile_uncertain_create(
            slug, title, f"gh issue create on {slug} was interrupted", run
        )
    if proc.returncode != 0:
        return _reconcile_uncertain_create(
            slug,
            title,
            _redact_home(
                f"gh issue create on {slug} exited {proc.returncode} "
                f"({proc.stderr.strip() or 'no stderr'})"
            ),
            run,
        )
    url = proc.stdout.strip()
    if not url:
        # rc=0 with no URL: reporting success with nothing to cite would be the
        # same false certainty in the opposite direction.
        return _reconcile_uncertain_create(
            slug, title, f"gh issue create on {slug} exited 0 but printed no URL", run
        )
    return url


def _report_posted(posted: str, why: str) -> int:
    """A confirmed post stays confirmed, whatever failed afterwards.

    Once `gh issue create` has returned a URL the issue is durable. Any later
    failure — a lock release, a print, a flush, an interrupt — is a REPORTING
    problem, and reporting it as a failure is what sends an operator to retry
    into a duplicate.
    """
    _warn(f"POSTED ({why}): {posted}")
    return _finish(0)


def _warn(message: str) -> None:
    """The single stderr writer -- and therefore where redaction belongs.

    REDACT AT THE EMITTER, NOT AT THE CONSTRUCTORS. The first version of this
    change wrapped eight `Refused(...)` sites individually and put an AST guard
    in front of them. An adversarial audit measured what that actually covered:
    thirteen of eighteen leak shapes passed the guard, because "every way a
    string can reach a Refused constructor" is an OPEN set -- `"x: " + str(exc)`,
    `"%s" % proc.stderr`, `.format(...)`, a message built on a previous line, a
    keyword argument, an aliased constructor. Meanwhile two live leaks reached
    stderr without touching a `Refused` at all: the generic `ERROR: {exc}`
    handler, and the `gh issue create` TIMEOUT cause.

    The emitters are a CLOSED set of five, enumerable and checkable, and every
    one of those leaks passes through them. `_redact_home` is idempotent, so a
    message already redacted upstream is unharmed.
    """
    with contextlib.suppress(OSError):
        print(_redact_home(message), file=sys.stderr)


def _sigterm_as_interrupt(signum, frame):  # noqa: ARG001 - signal handler signature
    """Make SIGTERM take the same reconciling path as SIGINT.

    Default SIGTERM kills the process outright, so a create that had already
    reached GitHub would leave an issue nobody knows about.
    """
    raise KeyboardInterrupt


@contextmanager
def _sigterm_reconciles() -> Iterator[None]:
    """Route SIGTERM through the reconciling path, then put the old handler back.

    Installing this globally and LEAVING it there is a bug, not a detail. main()
    is importable and is called in-process (the tests do exactly that), and a
    leaked SIGTERM handler is inherited by every process forked afterwards. An
    unrelated test's multiprocessing children then raised KeyboardInterrupt
    instead of dying on terminate(), so the parent's join() never returned --
    MEASURED in CI as two tests hanging for the full 1800s timeout, in a file
    this change does not touch.

    Restoring is therefore part of the contract: the handler is a property of
    THIS call, not of the interpreter.
    """
    try:
        prior = signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    except (ValueError, OSError):
        # not the main thread, or no signals here -- best-effort, nothing to undo
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGTERM, prior)


def main(argv: Sequence[str] | None = None, run: Runner = _run) -> int:
    """Thin wrapper so the signal scope covers every return path in _run_main."""
    with _sigterm_reconciles():
        return _run_main(argv, run)


def _run_main(argv: Sequence[str] | None, run: Runner) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--title-file", required=True, help="file holding the title (never a shell arg)"
    )
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--area", required=True, help=f"one of: {', '.join(sorted(AREA_LABELS))}")
    ap.add_argument(
        "--difficulty", required=True, help=f"one of: {', '.join(sorted(DIFFICULTY_LABELS))}"
    )
    ap.add_argument("--dry-run", action="store_true", help="run every check, post nothing")
    args = ap.parse_args(argv)

    posted: str | None = None
    snapshot_dir: str | None = None
    # Set the instant before the create is attempted. An interrupt between
    # that point and create_issue() returning leaves the outcome UNKNOWN, not
    # refused — the request may already have reached GitHub.
    create_attempted = False

    try:
        try:
            with open(args.title_file, encoding="utf-8") as fh:
                title = fh.read().strip()
            # ONE read of the body, kept as bytes. These exact bytes are what
            # gets scanned AND what gets posted — see the snapshot below.
            body_bytes = Path(args.body_file).read_bytes()
            body = body_bytes.decode("utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            # Unreadable or non-UTF-8 drafts are a "nothing was posted" outcome,
            # so they belong on exit 2 with everything else that refused — not on
            # the generic exit 1. UnicodeDecodeError is a ValueError, which no
            # handler here caught before.
            # TYPE only — the same reasoning as the staging refusal below.
            # An OSError here names the draft path, which is caller-supplied
            # and routinely under a home directory. Pre-existing, but in a
            # block this change rewrites and squarely the class this PR is
            # about, so it is corrected rather than left as the one that got
            # away.
            raise Refused(
                f"cannot read the draft files ({type(exc).__name__}) — check "
                "--title-file and --body-file are readable UTF-8."
            ) from exc
        if not title:
            raise Refused("title file is empty")
        if not body:
            raise Refused("body file is empty")
        if len(title) > 256:
            raise Refused(
                f"title is {len(title)} chars; GitHub rejects titles beyond ~256, so "
                "this would fail every retry"
            )
        if "\n" in title or "\r" in title:
            raise Refused("title contains a newline — GitHub's handling of that is unverified")

        # BEFORE the lock and before any network call. A refusal here must cost
        # nothing and leave no trace on the tracker; running it after
        # resolve_tracker would spend API calls to learn where not to post.
        for warning in privacy_scan(title, body):
            _warn(f"privacy scan: {warning}")
        # Only now, once the bytes have PASSED, are they snapshotted for gh.
        # Taking it before the scan would leave a scanned-clean snapshot on disk
        # for a draft that was then refused.
        try:
            snapshot_dir, snapshot_path = _snapshot(body_bytes)
        except OSError as exc:
            # Nothing has been posted, so this belongs on exit 2 with every
            # other refusal — not the generic exit 1 the module docstring
            # reserves for an unexpected error. Same correction as the draft
            # read above; a new failure path silently inherited the wrong code.
            # TYPE only, never str(exc). An OSError from os.mkdir/os.open
            # carries the FILENAME, and that path is under the operator's home
            # — so interpolating it reproduces exactly the class of value this
            # refusal machinery exists to keep out of stderr (CodeRabbit). I
            # removed `f.detail` from the refusal for that reason and then
            # reintroduced the same leak here, one function away.
            raise Refused(
                f"cannot stage the scanned body for posting ({type(exc).__name__}). "
                "Check that ~/tmp is writable."
            ) from exc

        labels = validate_labels(args.area, args.difficulty)
        slug = resolve_tracker(run)
        perm = check_permission(slug, run)

        # Lookup and creation are ONE critical section — see tracker_lock().
        with tracker_lock(slug):
            dup = find_duplicate(slug, title, run)
            if dup is not None:
                _warn(f"DUPLICATE: {slug}#{dup} already has this exact title. Nothing filed.")
                return _finish(3)
            if args.dry_run:
                print(
                    _redact_home(
                        f"DRY RUN ok — would file to {slug} (perm={perm}) "
                        f"with labels {labels}"
                    )
                )
                return _finish(0)
            # Create FIRST, report second. If stdout is closed or full, the post
            # has already happened — letting the print's OSError fall through to
            # the generic handler would report exit 1 for a CONFIRMED issue and
            # send the operator to retry into a duplicate.
            # gh is handed a SNAPSHOT of the scanned bytes, never the caller's
            # path. Checking the draft and then letting gh reopen it cannot
            # deliver the guarantee this refusal claims: an editor save, a
            # symlink swap or any other write between the check and gh's open
            # publishes bytes nothing scanned (Codex P1). A re-check narrows
            # that window; it does not close it, and only the caller's file can
            # change underneath us. The snapshot is written once, by us, into a
            # directory only this process can reach — so "the posted bytes are
            # the scanned bytes" is true by CONSTRUCTION rather than by timing.
            create_attempted = True
            posted = create_issue(slug, title, snapshot_path, labels, run)
        # From here the issue EXISTS. Nothing below may report otherwise — see
        # the handlers, which all check `posted` first.
        try:
            print(_redact_home(posted))
        except OSError:
            # The post is already durable; a reporting failure must not
            # reclassify it. The stderr fallback is itself guarded, because it
            # can fail for the same reason.
            with contextlib.suppress(OSError):
                print(
                    _redact_home(f"POSTED (could not write the URL to stdout): {posted}"),
                    file=sys.stderr,
                )
        return _finish(0)
    except Indeterminate as exc:
        if posted:
            return _report_posted(posted, f"after creation: {exc}")
        _warn(f"INDETERMINATE: {exc}")
        return _finish(4)
    except Refused as exc:
        if posted:
            return _report_posted(posted, f"after creation: {exc}")
        _warn(f"REFUSED: {exc}")
        return _finish(2)
    except KeyboardInterrupt:
        # An interrupt arriving while the lock is released, the URL printed, or
        # stdout flushed is AFTER the post. Reporting "nothing was created" there
        # would send the operator to retry into a duplicate.
        if posted:
            return _report_posted(posted, "interrupted after creation")
        if create_attempted:
            _warn(
                "INDETERMINATE: interrupted while creating the issue on "
                f"{slug}. The request MAY have reached GitHub. Check the tracker "
                "for this exact title BEFORE retrying."
            )
            return _finish(4)
        _warn("REFUSED: interrupted before the issue was created.")
        return _finish(2)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        if posted:
            return _report_posted(posted, f"after creation: {exc}")
        if create_attempted:
            _warn(f"INDETERMINATE: {exc} — raised while creating on {slug}; the post MAY exist.")
            return _finish(4)
        _warn(f"ERROR: {exc}")
        return _finish(1)
    finally:
        # The snapshot holds a copy of the body, so it does not outlive the run.
        # Suppressed in BOTH directions: a cleanup failure must never convert a
        # confirmed post into an error, and it must never replace a refusal's
        # exit code either.
        #
        # BaseException, not OSError. A SIGINT arriving DURING this rmtree
        # propagates out of the finally and replaces whatever main() was about
        # to return — including a confirmed post, which would then be reported
        # as an interrupt and send the operator to retry into a duplicate
        # (Codex P2). The window is small and the consequence is the exact
        # misreport this module is organised to prevent. A leftover directory
        # is 0700 under ~/tmp and the daily hygiene timer reaps it once it is 7
        # DAYS old (scripts/disk_hygiene.sh prune_tmp) — the timer is daily,
        # the threshold is not; losing the
        # correct exit code is not recoverable.
        if snapshot_dir:
            with contextlib.suppress(BaseException):
                shutil.rmtree(snapshot_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
