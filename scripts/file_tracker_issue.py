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
import fcntl
import hashlib
import json
import os
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
        raise Refused(f"`{' '.join(argv)}` failed: {proc.stderr.strip() or proc.returncode}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise Refused(f"`{' '.join(argv)}` returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        # `gh` returning a list or null here would surface as an AttributeError
        # from the caller's .get(), which is not in any handler's except clause.
        raise Refused(
            f"`{' '.join(argv)}` returned {type(data).__name__}, expected an object"
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
            f"duplicate check on {slug} did not complete ({exc}). Refusing to file — "
            "a lookup that never finished is not 'no duplicate'."
        ) from exc
    # returncode is checked BEFORE stdout is parsed, deliberately: `gh api
    # --paginate` writes each page as it arrives and only then reports a later
    # page's failure, so parsing first would read a PARTIAL listing as an
    # exhaustive one — the exact defect this function's docstring promises
    # against. Do not reorder.
    if proc.returncode != 0:
        raise Refused(
            f"duplicate check failed on {slug}: {proc.stderr.strip() or proc.returncode}. "
            "Refusing to file — a failed lookup is not 'no duplicate'."
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
                f"duplicate check returned unparseable JSON: {exc}. Refusing — a partial "
                "read cannot prove absence."
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
            f"cannot establish the per-install lock directory ({base}): {exc}. "
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
            print(f"waiting for the {slug} tracker lock…", file=sys.stderr)
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
        f"{cause}. Reconciled against {slug}: no issue with this title exists, so "
        "nothing was posted. Safe to retry."
    )


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
            slug, title, f"gh issue create on {slug} did not complete ({exc})", run
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
            f"gh issue create on {slug} exited {proc.returncode} "
            f"({proc.stderr.strip() or 'no stderr'})",
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
    """stderr write that cannot itself become the failure being reported."""
    with contextlib.suppress(OSError):
        print(message, file=sys.stderr)


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
    # Set the instant before the create is attempted. An interrupt between
    # that point and create_issue() returning leaves the outcome UNKNOWN, not
    # refused — the request may already have reached GitHub.
    create_attempted = False

    try:
        try:
            with open(args.title_file, encoding="utf-8") as fh:
                title = fh.read().strip()
            with open(args.body_file, encoding="utf-8") as fh:
                body = fh.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            # Unreadable or non-UTF-8 drafts are a "nothing was posted" outcome,
            # so they belong on exit 2 with everything else that refused — not on
            # the generic exit 1. UnicodeDecodeError is a ValueError, which no
            # handler here caught before.
            raise Refused(f"cannot read the draft files: {exc}") from exc
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
                print(f"DRY RUN ok — would file to {slug} (perm={perm}) with labels {labels}")
                return _finish(0)
            # Create FIRST, report second. If stdout is closed or full, the post
            # has already happened — letting the print's OSError fall through to
            # the generic handler would report exit 1 for a CONFIRMED issue and
            # send the operator to retry into a duplicate.
            create_attempted = True
            posted = create_issue(slug, title, args.body_file, labels, run)
        # From here the issue EXISTS. Nothing below may report otherwise — see
        # the handlers, which all check `posted` first.
        try:
            print(posted)
        except OSError:
            # The post is already durable; a reporting failure must not
            # reclassify it. The stderr fallback is itself guarded, because it
            # can fail for the same reason.
            with contextlib.suppress(OSError):
                print(f"POSTED (could not write the URL to stdout): {posted}", file=sys.stderr)
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


if __name__ == "__main__":
    sys.exit(main())
