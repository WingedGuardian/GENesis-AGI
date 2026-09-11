#!/usr/bin/env python3
"""Answer "which session produced this?" — and its inverse — from git history.

WHY THIS PARSES BODIES INSTEAD OF USING GIT'S TRAILER API
---------------------------------------------------------
``scripts/hooks/prepare-commit-msg`` stamps two trailers on every local commit:

    Install: <id8>          which install authored it
    Genesis-Session: <id8>  which CC session built it

and they land correctly — verified by running the hook. But this repository
squash-merges with ``squash_merge_commit_message=COMMIT_MESSAGES``, so the merge
commit's message is the CONCATENATION of every branch commit's message, and
GitHub then appends its own ``---------`` separator and a ``Co-authored-by:``
block at the end. Git parses trailers only from a message's LAST paragraph, and
that paragraph is now GitHub's block — so the stamped lines are never reached.
Note it is the appended block, not the concatenation, that does the damage: even
a single-commit squash loses them this way.

MEASURED 2026-09-10 on ``main``: ``%(trailers:...)`` matched 0 of the last 400
commits, while the literal text appears 720 times across those same 400 bodies.
Of the last 200 commits, 196 carry the text and 0 carry it in the final
paragraph, and a body scan attributes 196/200 (98%). The provenance was never
lost — only the query was wrong. Anything built on the trailer API here will
report a confident, total, silent absence.

WHY THE DATABASE IS ENRICHMENT AND NOT THE SOURCE OF TRUTH
-----------------------------------------------------------
A session id recovered from a commit may resolve to nothing. MEASURED the same
day: session ``59b971ca`` authored PR #1702, and has no ``cc_sessions`` row
(4,774 rows, prefix-matched) and no transcript in any of 532 CC project
directories. For that session the commit trailer is the only surviving evidence
it ever ran. So git is asked first and always; the database only ever adds
detail to an answer git already gave.

Usage:
    session_provenance.py --pr 1702          # sessions behind a PR (open or merged)
    session_provenance.py --commit <sha>     # sessions behind one commit
    session_provenance.py --branch <name>    # sessions behind a branch's own commits
    session_provenance.py --session <id8>    # everything a session shipped
    session_provenance.py --coverage [N]     # how attributable is recent history

Stdlib-only, so it runs anywhere the repo does.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# The hook shape-constrains both ids to exactly 8 lowercase hex characters.
_SESSION_RE = re.compile(r"^\s*Genesis-Session:\s*([0-9a-f]{8})\s*$", re.MULTILINE)
_INSTALL_RE = re.compile(r"^\s*Install:\s*([0-9a-f]{8})\s*$", re.MULTILINE)
_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
# The hook shape-constrains ids to exactly 8 lowercase hex; anything else reaching
# a glob or a SQL GLOB is a pattern, not an identifier.
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}")

# Bound on the history scanned when locating a PR's merge commit. Unbounded, this
# pulled full %B for every commit in the repo on each invocation, against a 120s
# subprocess timeout — making the timeout a live path rather than a theoretical
# one, on exactly the code path whose failure used to read as "the PR is open".
_PR_SEARCH_LIMIT = 4000

_SEP_FIELD = "\x1f"
_SEP_REC = "\x1e"


def _git(*args: str, check: bool = False):
    """Run git in the repo root.

    Returns stdout on success. With ``check=True`` a failure returns None so the
    caller can tell "git broke" from "git returned nothing" — without it a
    failure is indistinguishable from an empty result, which is how a timeout
    becomes a confident wrong answer. ``errors="replace"`` because commit bodies
    carry arbitrary bytes and a UnicodeDecodeError here would abort the command.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(REPO_ROOT),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError) as e:
        if check:
            print(f"git {' '.join(args)}: {e}", file=sys.stderr)
            return None
        return ""
    if r.returncode != 0:
        if check:
            print(f"git {' '.join(args)}: {r.stderr.strip()}", file=sys.stderr)
            return None
        return ""
    return r.stdout


def sessions_in(text: str) -> list[str]:
    """Session ids stamped anywhere in a commit message. Deduped, sorted.

    Anchored per-line rather than searched loosely: prose in a commit body can
    legitimately mention a trailer (this file's own docstring does), and a loose
    search would attribute a commit to a session merely discussed in it.
    """
    return sorted(set(_SESSION_RE.findall(text)))


def installs_in(text: str) -> list[str]:
    return sorted(set(_INSTALL_RE.findall(text)))


def scan(rev_range: str, limit: int | None = None) -> list[dict] | None:
    """Commits in ``rev_range``, each with the sessions stamped in its message.

    Returns None when GIT ITSELF FAILED, and [] only when the range is genuinely
    empty. Collapsing those two was a fail-open: a timeout made ``cmd_pr`` report
    a merged PR as OPEN, because "not found on main" is the branch that concludes
    "must still be open". A transport failure must never render as a state claim.
    """
    args = [
        "log",
        f"--format=%H{_SEP_FIELD}%ad{_SEP_FIELD}%s{_SEP_FIELD}%B{_SEP_REC}",
        "--date=short",
    ]
    if limit and limit > 0:
        args.append(f"-{limit}")
    args.append(rev_range)
    raw = _git(*args, check=True)
    if raw is None:
        return None

    out: list[dict] = []
    dropped = 0
    for rec in raw.split(_SEP_REC):
        if not rec.strip():
            continue
        parts = rec.strip().split(_SEP_FIELD)
        if len(parts) < 4:
            # N6: a silently-dropped record would shrink the coverage DENOMINATOR,
            # so a parse failure would read as a higher attribution rate. Count it.
            dropped += 1
            continue
        sha, date, subject, body = parts[0], parts[1], parts[2], parts[3]
        pr = _PR_RE.search(subject)
        out.append(
            {
                "sha": sha,
                "short": sha[:9],
                "date": date,
                "subject": subject,
                "pr": int(pr.group(1)) if pr else None,
                "sessions": sessions_in(body),
                "installs": installs_in(body),
            }
        )
    if dropped:
        print(
            f"warning: {dropped} commit record(s) could not be parsed and are "
            f"excluded from the counts below",
            file=sys.stderr,
        )
    return out


def enrich(session_id: str) -> dict:
    """Whatever else is known about a session id. Absence is a real answer.

    Every lookup here is best-effort: the point of the module is that a session
    can be entirely absent from both stores and still have authored real work.
    """
    info: dict = {"id": session_id, "db": None, "transcript": None}

    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT id, session_type, channel, model, status, started_at, "
                    "COALESCE(topic, '') FROM cc_sessions WHERE id GLOB ? LIMIT 2",
                    (session_id + "*",),
                ).fetchall()
            finally:
                con.close()
            if len(row) == 1:
                keys = ("id", "type", "channel", "model", "status", "started_at", "topic")
                info["db"] = dict(zip(keys, row[0], strict=False))
            elif len(row) > 1:
                # An 8-hex prefix is not guaranteed unique. Say so rather than
                # picking one and presenting a guess as a fact.
                info["db"] = {"ambiguous": len(row)}
        except sqlite3.Error:
            pass

    if PROJECTS_DIR.exists():
        try:
            hit = next(PROJECTS_DIR.glob(f"*/{session_id}*.jsonl"), None)
            if hit:
                info["transcript"] = str(hit)
        except OSError:
            pass

    return info


def _describe(info: dict) -> str:
    db = info.get("db")
    if db and "ambiguous" in db:
        return f"{db['ambiguous']} sessions share this prefix — ambiguous"
    if db:
        topic = (db.get("topic") or "").strip()
        head = f"{db.get('type', '?')}/{db.get('status', '?')} started {db.get('started_at', '?')}"
        return f"{head}{' — ' + topic[:70] if topic else ''}"
    if info.get("transcript"):
        return "no DB row, transcript on disk"
    return "no DB row, no transcript — the commit is the only record"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_pr(number: int) -> int:
    """Sessions behind a PR — merged (on main) or still open (on its branch)."""
    commits = scan("main", limit=_PR_SEARCH_LIMIT)
    if commits is None:
        print(
            f"PR #{number}: could not read main's history — refusing to guess "
            f"whether it is merged",
            file=sys.stderr,
        )
        return 1
    hits = [c for c in commits if c["pr"] == number]
    if hits:
        c = hits[0]
        print(f"PR #{number} — merged as {c['short']} on {c['date']}")
        print(f"  {c['subject']}")
        _print_sessions(c["sessions"])
        return 0

    # Not found in the scanned window. That is NOT evidence the PR is open: the
    # scan is bounded at _PR_SEARCH_LIMIT, so an older merge simply falls outside
    # it. Ask GitHub for the state rather than inferring it from our own bound —
    # the same fail-open shape as the git-failure case above, one level subtler
    # because here the scan SUCCEEDED and was merely too short.
    state, head = _pr_state_and_head(number)
    if state == "MERGED":
        print(
            f"PR #{number} — MERGED, but its merge commit is outside the "
            f"{_PR_SEARCH_LIMIT}-commit scan window. Re-run with a larger "
            f"--limit to attribute it.",
            file=sys.stderr,
        )
        return 1
    if not head:
        print(
            f"PR #{number}: no merge commit on main, and gh could not name its "
            f"head branch (unmerged and unavailable offline?)",
            file=sys.stderr,
        )
        return 1

    print(f"PR #{number} — OPEN, head branch {head}")
    return cmd_branch(head, header=False)


def _pr_state_and_head(number: int) -> tuple[str, str]:
    """``(state, headRefName)`` for a PR, or ``("", "")`` when gh cannot say.

    State is fetched alongside the head ref so a caller never has to INFER
    "merged" or "open" from whether its own bounded scan happened to reach the
    merge commit. That inference is wrong whenever the PR is older than the
    window, and wrong in the confident direction.
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", "headRefName,state"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        if r.returncode == 0:
            d = json.loads(r.stdout)
            return str(d.get("state") or ""), str(d.get("headRefName") or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return "", ""


def cmd_branch(branch: str, header: bool = True) -> int:
    """Sessions behind the commits a branch has that main does not."""
    ref = branch
    if not _git("rev-parse", "--verify", "--quiet", ref).strip():
        ref = f"origin/{branch}"
        if not _git("rev-parse", "--verify", "--quiet", ref).strip():
            print(f"No such branch locally or on origin: {branch}", file=sys.stderr)
            return 1

    commits = scan(f"main..{ref}")
    if commits is None:
        print(f"could not read history for {ref}", file=sys.stderr)
        return 1
    if header:
        print(f"branch {ref} — {len(commits)} commit(s) not in main")
    if not commits:
        print("  (nothing unique to this branch)")
        return 0

    all_sessions: set[str] = set()
    for c in commits:
        all_sessions.update(c["sessions"])
        print(
            f"  {c['short']}  {c['date']}  {','.join(c['sessions']) or '-':<28} {c['subject'][:60]}"
        )
    print()
    _print_sessions(sorted(all_sessions))
    return 0


def cmd_commit(rev: str) -> int:
    sha = _git("rev-parse", "--verify", rev, check=True).strip()
    if not sha:
        return 1
    commits = scan(f"{sha}~1..{sha}") or scan(sha, limit=1)
    if not commits:  # None (git failed) or [] (no such commit) — both are "no answer"
        print(f"No such commit: {rev}", file=sys.stderr)
        return 1
    c = commits[0]
    print(f"{c['short']}  {c['date']}  {c['subject']}")
    _print_sessions(c["sessions"])
    return 0


def cmd_session(session_id: str, limit: int) -> int:
    """Everything a session shipped: its merged commits, plus any live branches."""
    info = enrich(session_id)
    print(f"session {session_id} — {_describe(info)}")
    if info.get("transcript"):
        print(f"  transcript: {info['transcript']}")
    print()

    scanned = scan("main", limit=limit)
    if scanned is None:
        print("could not read main's history", file=sys.stderr)
        return 1
    merged = [c for c in scanned if session_id in c["sessions"]]
    print(f"on main (last {limit} commits scanned): {len(merged)}")
    for c in merged:
        pr = f"#{c['pr']}" if c["pr"] else "—"
        print(f"  {c['short']}  {c['date']}  {pr:>6}  {c['subject'][:64]}")

    live: list[tuple[str, int]] = []
    incomplete: list[str] = []
    for line in _git("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        b = line.strip()
        if not b or b == "main":
            continue
        branch_commits = scan(f"main..{b}")
        if branch_commits is None:
            # A failed branch scan is not an empty one. Swallowing it here would
            # report a SHORTER list of unlanded work than actually exists, which
            # is the wrong direction for a tool whose job is finding lost work.
            incomplete.append(b)
            continue
        n = sum(1 for c in branch_commits if session_id in c["sessions"])
        if n:
            live.append((b, n))
    if live:
        print(f"\nunlanded branches carrying this session: {len(live)}")
        for b, n in sorted(live, key=lambda x: -x[1]):
            print(f"  {n:>3} commit(s)  {b}")
    if incomplete:
        print(
            f"\nwarning: {len(incomplete)} branch(es) could not be read, so the "
            f"list above is INCOMPLETE: {', '.join(incomplete[:5])}"
            + (" ..." if len(incomplete) > 5 else ""),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_coverage(limit: int) -> int:
    """How much of recent history is attributable — the health of the mechanism."""
    commits = scan("main", limit=limit)
    if commits is None:
        print("could not read main's history", file=sys.stderr)
        return 1
    if not commits:
        print("No commits scanned.", file=sys.stderr)
        return 1
    with_s = [c for c in commits if c["sessions"]]
    pct = 100.0 * len(with_s) / len(commits)
    print(f"commits scanned:      {len(commits)}")
    print(f"with a session id:    {len(with_s)}  ({pct:.1f}%)")
    print(f"without:              {len(commits) - len(with_s)}")

    # The trailer API is measured alongside on purpose: when this number is not
    # zero, squash behaviour has changed and this tool's premise needs revisiting.
    api = _git("log", f"-{limit}", "--format=%(trailers:key=Genesis-Session,valueonly)", "main")
    api_hits = sum(1 for line in api.splitlines() if line.strip())
    print(
        f"\nvia git's trailer API: {api_hits}  "
        f"(expected ~0 — squash concatenates messages, so trailers are mid-body)"
    )

    if len(commits) - len(with_s):
        print("\nunattributed:")
        for c in commits:
            if not c["sessions"]:
                print(f"  {c['short']}  {c['date']}  {c['subject'][:66]}")
    return 0


def _print_sessions(sessions: list[str]) -> None:
    if not sessions:
        print("  sessions: NONE STAMPED (pre-hook commit, or an external contributor)")
        return
    print(f"  sessions: {len(sessions)}")
    for s in sessions:
        print(f"    {s}  {_describe(enrich(s))}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Trace work back to the session that produced it.",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="PR number (open or merged)")
    g.add_argument("--commit", help="commit-ish")
    g.add_argument("--branch", help="branch name")
    g.add_argument("--session", help="8-hex session id")
    g.add_argument(
        "--coverage",
        nargs="?",
        const=200,
        type=int,
        metavar="N",
        help="attribution rate over the last N commits",
    )
    ap.add_argument(
        "--limit", type=int, default=400, help="commits of main to scan for --session (default 400)"
    )
    args = ap.parse_args()

    # Bind the CHOSEN MODE once, from which option argparse actually set — never
    # re-derive it from a value's truthiness. Truthiness conflates "not supplied"
    # with "supplied empty", and because the fall-through target is a DIFFERENT
    # command, `--branch "$UNSET_VAR"` silently ran a full-history coverage report
    # and exited 0. A caller asking "which session authored this commit?" got a
    # different question's answer and a success code.
    handlers = {
        "pr": lambda a: cmd_pr(a.pr),
        "commit": lambda a: cmd_commit(a.commit),
        "branch": lambda a: cmd_branch(a.branch),
        "session": lambda a: cmd_session(a.session.strip().lower(), a.limit),
        "coverage": lambda a: cmd_coverage(a.coverage),
    }
    chosen = [k for k in handlers if getattr(args, k, None) is not None]
    if len(chosen) != 1:
        ap.error(f"expected exactly one mode, got {chosen or 'none'}")
    mode = chosen[0]
    value = getattr(args, mode)
    if isinstance(value, str) and not value.strip():
        ap.error(f"--{mode} was given an empty value")
    if mode == "session" and not _SESSION_ID_RE.fullmatch(value.strip().lower()):
        # N7: the id reaches a glob and a SQL GLOB. `--session '*'` is a pattern,
        # not an identifier.
        ap.error(f"--session must be 8 hex characters, got {value!r}")
    return handlers[mode](args)


if __name__ == "__main__":
    sys.exit(main())
