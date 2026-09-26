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
    session_provenance.py --file <path> [--line N]  # sessions behind a file / one line
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
# Case-INSENSITIVE, then lowercased at the capture site. The hook shape-constrains
# to 8 hex characters but preserves the case of the source env var, so an
# uppercase CLAUDE_CODE_SESSION_ID stamps `Genesis-Session: ABCDEF12` — which a
# lowercase-only pattern reports as UNSTAMPED. Silent under-attribution, on a
# tool whose entire job is attribution.
# Anchored at COLUMN ZERO with horizontal-only padding: `\s` accepts newlines,
# so `Genesis-Session:\nabcdef12` used to match across a line break, and a
# leading `\s*` matched an indented Markdown example as a real stamp. The hook
# emits exactly one column-zero trailer line; anything else is prose.
_SESSION_RE = re.compile(r"^Genesis-Session:[ \t]*([0-9a-fA-F]{8})[ \t]*$", re.MULTILINE)
_INSTALL_RE = re.compile(r"^Install:[ \t]*([0-9a-fA-F]{8})[ \t]*$", re.MULTILINE)
# A squash-merge subject can end with SEVERAL parenthesized PR refs — GitHub
# appends one per stacked PR (measured: subjects ending `(#2152) (#2153)` exist
# on main). Collapsing to the LAST one makes the earlier numbers undiscoverable
# offline, so the tail run is captured as a whole and each ref inside it kept.
# Anchored to the END on purpose: a `(#1234)` mid-subject is prose, not a merge
# attribution.
_PR_TAIL_RE = re.compile(r"(?:\(#\d+\)\s*)+$")
_PR_NUM_RE = re.compile(r"\(#(\d+)\)")
# The hook shape-constrains ids to exactly 8 lowercase hex; anything else reaching
# a glob or a SQL GLOB is a pattern, not an identifier.
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}")

# Bound on the history scanned when locating a PR's merge commit. Unbounded, this
# pulled full %B for every commit in the repo on each invocation, against a 120s
# subprocess timeout — making the timeout a live path rather than a theoretical
# one, on exactly the code path whose failure used to read as "the PR is open".
_PR_SEARCH_LIMIT = 4000

# Record/field framing. ANY byte pattern can legally appear in a commit message,
# including whatever sentinel this tool picks — a body carrying the field token
# used to truncate at `parts[3]` and a record token split one commit into two,
# losing exactly the provenance lines this tool exists to read (reproduced:
# `scan()` returned the commit with an empty session list). The only delimiter
# that cannot collide is one messages cannot CONTAIN: NUL — git's own `-z`/`%x00`
# framing relies on the same exclusion, so it is impossible by the platform's
# own contract rather than by our token's length.
_SEP = "\x00"


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
    return sorted({m.lower() for m in _SESSION_RE.findall(text)})


def installs_in(text: str) -> list[str]:
    return sorted({m.lower() for m in _INSTALL_RE.findall(text)})


def scan(
    rev_range: str,
    limit: int | None = None,
    path: str | None = None,
    follow: bool = False,
) -> list[dict] | None:
    """Commits in ``rev_range``, each with the sessions stamped in its message.

    ``path`` restricts the walk to commits touching that file. ``follow`` asks
    git to continue the walk across a rename, so a file's earlier provenance
    under its old name is not silently truncated; it only ever applies to the
    single path this tool passes, and git itself cannot follow copies or
    complex rename chains, so even with it the answer can end early.

    Returns None when GIT ITSELF FAILED, and [] only when the range is genuinely
    empty. Collapsing those two was a fail-open: a timeout made ``cmd_pr`` report
    a merged PR as OPEN, because "not found on main" is the branch that concludes
    "must still be open". A transport failure must never render as a state claim.
    """
    args = [
        "log",
        "-z",  # NUL frame terminators — see _SEP for why nothing else is safe
        # %x00 because an argv element cannot itself contain a NUL byte.
        "--format=%H%x00%ad%x00%s%x00%B",
        "--date=short",
    ]
    if follow and path is not None:
        args.append("--follow")
    if limit is not None:
        # A nonpositive limit previously OMITTED the bound entirely, so the scan
        # silently became unbounded while every message still described it as
        # capped. Refuse instead: an unbounded scan the caller did not ask for is
        # a different operation, not a lenient reading of the same one.
        if limit <= 0:
            raise ValueError(f"history limit must be positive, got {limit}")
        args.append(f"-{limit}")
    args.append(rev_range)
    if path is not None:
        args += ["--", path]
    raw = _git(*args, check=True)
    if raw is None:
        return None

    out: list[dict] = []
    dropped = 0
    # %x00 fields + -z record terminator: fields = H, ad, s, B, then a NUL record
    # boundary, so the stream splits into groups of exactly 4.
    fields = raw.split(_SEP)
    for i in range(0, len(fields) - 3, 4):
        sha, date, subject, body = (f.strip("\n") for f in fields[i : i + 4])
        if not sha:
            dropped += 1
            continue
        tail = _PR_TAIL_RE.search(subject)
        prs = (
            [int(m.group(1)) for m in _PR_NUM_RE.finditer(tail.group(0))]
            if tail
            else []
        )
        out.append(
            {
                "sha": sha,
                "short": sha[:9],
                "date": date,
                "subject": subject,
                "pr": prs[-1] if prs else None,
                "prs": prs,
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


def _gh_head_name(ref: str) -> str:
    """The GitHub head name for a local ref.

    A remote-tracking ref is `origin/<branch>` locally but `<branch>` on
    GitHub, so `gh pr list --head origin/foo` queries a name that does not
    exist and a merged remote-tracking branch reads as unlanded. Strip only
    the remote prefix here; the full ref stays in use for local git lookups.
    """
    for prefix in ("refs/remotes/origin/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


# A branch NAME can be reused across many merged PRs, so the head list is
# bounded — but a list returned AT the cap is evidence of truncation, not
# completeness. Callers must treat it as such: an omitted squash tip reads as
# unlanded work that shipped.
_MERGED_PR_LIMIT = 200


def _merged_pr_heads(ref: str) -> tuple[list[str], bool]:
    """(Head OIDs of merged PRs whose head name matches ``ref``, complete).

    ``complete`` is False when the response filled the page exactly — there may
    be heads GitHub never sent — and when gh could not answer at all.
    """
    try:
        r = subprocess.run(
            [
                "gh", "pr", "list", "--head", _gh_head_name(ref),
                "--base", "main", "--state", "merged",
                "--limit", str(_MERGED_PR_LIMIT),
                "--json", "number,headRefOid",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        if r.returncode != 0:
            return [], False
        rows = json.loads(r.stdout)
        return (
            [str(pr.get("headRefOid") or "") for pr in rows],
            len(rows) < _MERGED_PR_LIMIT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError,
            json.JSONDecodeError, OSError, ValueError):
        return [], False


def _is_ancestor(a: str, b: str) -> bool:
    """`git merge-base --is-ancestor`, False on any error."""
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, b],
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return False


def _is_patch_merged(branch: str) -> bool:
    """Whether every commit on ``branch`` is already in main.

    `git cherry` compares each branch commit's patch INDEPENDENTLY, but this
    repository's normal landing is a SQUASH whose single commit carries the
    aggregate patch — so every original commit is marked `+` and a squash-merged
    branch reads as unlanded forever (reproduced: a two-commit squashed branch
    was reported as unlanded). Three methods, cheapest first — ancestor (free,
    covers ordinary merges), a merged-PR join validated against the merged HEAD
    OBJECT rather than the name (a reused branch name is not evidence THIS work
    shipped), then patch-id for the single-commit case. Fail-safe toward
    "not merged" on any error: reporting unlanded work that already shipped is
    the harmless direction for a tool that exists to find it.
    """
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, "main"],
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=10,
        )
        if r.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    tip = _git("rev-parse", branch).strip()
    if tip:
        heads, _ = _merged_pr_heads(branch)
        if tip in heads:
            return True
    out = _git("cherry", "main", branch)
    if not out:
        return False
    return not any(line.startswith("+") for line in out.splitlines())


def _db_fenced(db_path: Path) -> bool:
    """Admission fence: True when the database must not be touched.

    A fenced (quarantined) database is skipped rather than opened — including
    for a read, because the gate over scripts-side SQLite openers requires the
    consult, and because a fenced DB's contents are precisely what cannot be
    trusted. The shim lives in scripts/hooks and fails CLOSED: a present shim
    that cannot establish the fence state reads as fenced. A checkout that
    predates the admission machinery has no fence to consult — unfenced.
    """
    hooks = str(Path(__file__).resolve().parent / "hooks")
    try:
        if hooks not in sys.path:
            sys.path.insert(0, hooks)
        from db_admission_check import database_is_fenced
    except ImportError:
        return False
    try:
        return bool(database_is_fenced(db_path))
    except Exception:  # noqa: BLE001 - the shim's own policy: never crash here
        return True


def enrich(session_id: str) -> dict:
    """Whatever else is known about a session id. Absence is a real answer.

    Every lookup here is best-effort: the point of the module is that a session
    can be entirely absent from both stores and still have authored real work.
    """
    info: dict = {
        "id": session_id,
        "db": None,
        "transcript": None,
        # Lookup failures are tracked, not collapsed into the absent case: a
        # corrupt DB or unreadable directory is "unknown", and reporting it as
        # "no DB row" turns a failed check into a confident false provenance.
        "db_error": False,
        "db_fenced": False,
        "transcript_error": False,
        "transcript_ambiguous": 0,
    }

    if DB_PATH.exists() and _db_fenced(DB_PATH):
        info["db_fenced"] = True
    elif DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    # The stamped id is a CC transcript id prefix, stored in
                    # `cc_session_id` for foreground sessions — `id` alone is
                    # Genesis's own UUID namespace and misses them entirely
                    # (the hook's own lookup recipe checks both columns).
                    "SELECT id, session_type, channel, model, status, started_at, "
                    "COALESCE(topic, '') FROM cc_sessions "
                    "WHERE id GLOB ? OR cc_session_id GLOB ? LIMIT 3",
                    (session_id + "*", session_id + "*"),
                ).fetchall()
            # NOTE the LIMIT below is 3, not 2: with LIMIT 2 the count could only
            # ever be "at least 2", yet it was rendered as the exact sentence
            # "2 sessions share this prefix". Fetching one more lets the message
            # distinguish exactly-2 from more-than-2 honestly.
            finally:
                con.close()
            if len(row) == 1:
                keys = ("id", "type", "channel", "model", "status", "started_at", "topic")
                info["db"] = dict(zip(keys, row[0], strict=False))
            elif len(row) > 1:
                # An 8-hex prefix is not guaranteed unique. Say so rather than
                # picking one and presenting a guess as a fact — and say whether
                # the count is exact, since the query is capped.
                info["db"] = {"ambiguous": len(row), "capped": len(row) >= 3}
        except sqlite3.Error:
            info["db_error"] = True

    if PROJECTS_DIR.exists():
        try:
            hits = list(PROJECTS_DIR.glob(f"*/{session_id}*.jsonl"))
            if len(hits) == 1:
                info["transcript"] = str(hits[0])
            elif len(hits) > 1:
                # An 8-char filename prefix is no more unique on disk than the
                # DB prefix is in `id` — `next(...)` would pick an arbitrary
                # filesystem-order match and print it as THIS session.
                info["transcript_ambiguous"] = len(hits)
        except OSError:
            info["transcript_error"] = True

    return info


def _describe(info: dict) -> str:
    db = info.get("db")
    if db and "ambiguous" in db:
        n = db["ambiguous"]
        more = "at least " if db.get("capped") else ""
        return f"{more}{n} sessions share this prefix — ambiguous"
    if db:
        topic = (db.get("topic") or "").strip()
        head = f"{db.get('type', '?')}/{db.get('status', '?')} started {db.get('started_at', '?')}"
        return f"{head}{' — ' + topic[:70] if topic else ''}"
    parts: list[str] = []
    if info.get("db_fenced"):
        parts.append("DB fenced (quarantined)")
    elif info.get("db_error"):
        parts.append("DB unreadable")
    else:
        parts.append("no DB row")
    if info.get("transcript_ambiguous"):
        parts.append(
            f"{info['transcript_ambiguous']} transcripts share this prefix — ambiguous"
        )
    elif info.get("transcript_error"):
        parts.append("transcripts unreadable")
    elif info.get("transcript"):
        parts.append("transcript on disk")
    else:
        parts.append("no transcript")
    if info.get("db_error") or info.get("db_fenced") or info.get("transcript_error"):
        return "; ".join(parts) + " — enrichment INCOMPLETE, not absent"
    if info.get("transcript"):
        return "no DB row, transcript on disk"
    if info.get("transcript_ambiguous"):
        return "; ".join(parts)
    return "no DB row, no transcript — the commit is the only record"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_pr(number: int, limit: int | None = None) -> int:
    """Sessions behind a PR — merged (on main) or still open (on its branch)."""
    window = limit or _PR_SEARCH_LIMIT
    commits = scan("main", limit=window)
    if commits is None:
        print(
            f"PR #{number}: could not read main's history — refusing to guess "
            f"whether it is merged",
            file=sys.stderr,
        )
        return 1
    hits = [c for c in commits if number in c["prs"]]
    if hits:
        c = hits[0]
        print(f"PR #{number} — merged as {c['short']} on {c['date']}")
        print(f"  {c['subject']}")
        _print_sessions(c["sessions"])
        return 0

    # Not found in the scanned window. That is NOT evidence the PR is open: the
    # scan is bounded, so an older merge simply falls outside it. Ask GitHub for
    # the state — and, for a merged PR, its mergeCommit OID, which identifies the
    # merge directly rather than by how far back our window happened to reach.
    state, head, head_oid, merge_oid = _pr_lookup(number)
    if state == "CLOSED":
        # Closed without merging. gh still returns a head ref, so falling through
        # would print "OPEN" for a PR that is demonstrably not open.
        print(f"PR #{number} — CLOSED without merging (head {head or head_oid or '?'})")
        if head_oid and _git("rev-parse", "--verify", "--quiet", head_oid).strip():
            return _report_commits(scan(f"main..{head_oid}"), head)
        if head:
            return cmd_branch(head, header=False)
        # GitHub can return an empty headRefName for a closed PR whose branch is
        # deleted. Printing the state and exiting 0 would be an INCOMPLETE answer
        # rendered as success — the named-branch path returns nonzero, so this
        # one does too.
        print(
            f"PR #{number}: provenance unreadable — head branch is gone",
            file=sys.stderr,
        )
        return 1
    if state == "MERGED":
        # The window missed it, but GitHub names the merge commit exactly —
        # read that commit instead of demanding a bigger scan.
        if merge_oid:
            merged = scan(f"{merge_oid}~1..{merge_oid}") or scan(merge_oid, limit=1)
            if merged:
                c = merged[0]
                print(f"PR #{number} — merged as {c['short']} on {c['date']}")
                print(f"  {c['subject']}")
                _print_sessions(c["sessions"])
                return 0
        print(
            f"PR #{number} — MERGED, but its merge commit is outside the "
            f"{window}-commit scan window and the merge commit is not in this "
            f"clone. Re-run with a larger --limit to attribute it.",
            file=sys.stderr,
        )
        return 1
    if head_oid:
        # The PR's head is an OBJECT, not a name: for a cross-repository PR the
        # headRefName is unqualified, and resolving it as a branch would
        # silently attribute whichever same-named local branch happens to exist
        # — or fail when the fork's branch was never fetched here.
        if not _git("rev-parse", "--verify", "--quiet", head_oid).strip():
            print(
                f"PR #{number} — OPEN, head {head_oid[:12]} is not in this clone "
                f"(cross-repository or unfetched head). Run `git fetch` for it "
                f"first; attributing the same-named local branch instead would "
                f"be a guess, not an answer.",
                file=sys.stderr,
            )
            return 1
        print(f"PR #{number} — OPEN, head {head} ({head_oid[:12]})")
        return _report_commits(scan(f"main..{head_oid}"), head)
    if not head:
        print(
            f"PR #{number}: no merge commit on main, and gh could not name its "
            f"head branch (unmerged and unavailable offline?)",
            file=sys.stderr,
        )
        return 1

    print(f"PR #{number} — OPEN, head branch {head}")
    return cmd_branch(head, header=False)


def _report_commits(commits: list[dict] | None, label: str) -> int:
    """Print the per-commit table + session rollup for a resolved ref."""
    if commits is None:
        print(f"could not read history for {label}", file=sys.stderr)
        return 1
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


def _pr_lookup(number: int) -> tuple[str, str, str, str]:
    """``(state, headRefName, headRefOid, mergeCommitOid)``, ``("", "", "", "")``
    when gh cannot say.

    State is fetched alongside the head ref so a caller never has to INFER
    "merged" or "open" from whether its own bounded scan happened to reach the
    merge commit. That inference is wrong whenever the PR is older than the
    window, and wrong in the confident direction. The OIDs are taken alongside
    because a branch NAME is not an identity: names are reused, and a fork's
    unqualified name resolves to nothing (or worse, to an unrelated local
    branch) here.
    """
    try:
        r = subprocess.run(
            [
                "gh", "pr", "view", str(number), "--json",
                "headRefName,headRefOid,state,mergeCommit",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        if r.returncode == 0:
            d = json.loads(r.stdout)
            merge = d.get("mergeCommit") or {}
            return (
                str(d.get("state") or ""),
                str(d.get("headRefName") or ""),
                str(d.get("headRefOid") or ""),
                str(merge.get("oid") or ""),
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return "", "", "", ""


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
    return _report_commits(commits, ref)


def cmd_commit(rev: str) -> int:
    raw = _git("rev-parse", "--verify", rev, check=True)
    if raw is None:
        # `check=True` returns None on failure; `.strip()` on it raised an
        # AttributeError traceback instead of the intended clean CLI error, for
        # any misspelled or deleted revision.
        print(f"no such commit: {rev}", file=sys.stderr)
        return 1
    sha = raw.strip()
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
    refs = _git(
        "for-each-ref",
        "--format=%(refname:short) %(objectname)",
        "refs/heads",
        "refs/remotes/origin",
        check=True,
    )
    if refs is None:
        print("could not enumerate branches", file=sys.stderr)
        return 1
    # Local AND remote-tracking refs: work pushed then deleted locally is still
    # unlanded work, and a heads-only scan used to omit it silently. Dedupe on
    # the TIP OBJECT — `foo` and `origin/foo` sharing a tip are one branch — but
    # keep ALL the names, not the first: merged-PR evidence is keyed by name,
    # and the name GitHub knows may be the alias the dedupe would have dropped.
    tips: dict[str, list[str]] = {}
    for line in refs.splitlines():
        b, _, tip = line.partition(" ")
        b = b.strip()
        tip = tip.strip()
        if not b or b in ("main", "origin/main") or b.endswith("/HEAD"):
            continue
        tips.setdefault(tip, []).append(b)

    for tip, names in tips.items():
        b = names[0]  # display name; classification below uses every alias
        merged_heads: list[str] = []
        heads_complete = True
        for name in dict.fromkeys(_gh_head_name(n) for n in names):
            # Dedupe on the GitHub head name: `feature` and `origin/feature`
            # issue the identical query, so a failure on the redundant second
            # call would invalidate a complete result already obtained.
            heads, complete = _merged_pr_heads(name)
            merged_heads.extend(h for h in heads if h)
            heads_complete &= complete
        # A tip is merged when ANY name proves it — the alias GitHub knows may
        # not be the one that survived dedupe.
        if _is_ancestor(tip, "main") or tip in merged_heads:
            # This repo SQUASH-merges, so a merged branch's original commits are
            # not ancestors of main and `main..<tip>` still returns them.
            # Reporting those as "unlanded" is the opposite of this tool's job:
            # it would manufacture lost work out of work that shipped.
            continue
        cherry = _git("cherry", "main", tip)
        if cherry and not any(ln.startswith("+") for ln in cherry.splitlines()):
            continue  # every commit's patch is upstream — patch-equivalent merge
        if not heads_complete:
            # A full page means heads GitHub never sent may exist — one of them
            # could be this tip. Classifying from a truncated list resurrects
            # shipped work, so the branch is INCOMPLETE, not unlanded.
            incomplete.append(b)
            continue
        # A branch that ADVANCED past a squash merge still carries the shipped
        # originals on `main..<tip>`, and a session confined to that merged
        # prefix would be reported unlanded. Bound the scan at the latest merged
        # PR head (across ALL the tip's names) that is an ancestor of the tip.
        base = "main"
        ancestors = [h for h in merged_heads if _is_ancestor(h, tip)]
        if ancestors:
            latest = _git("merge-base", "--independent", *ancestors).split()
            if latest:
                base = latest[0]
        branch_commits = scan(f"{base}..{tip}")
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


def cmd_file(path: str, line: int | None, limit: int) -> int:
    """Sessions behind a file — one line's commit via blame, or its history."""
    if line is not None:
        # `--line-porcelain` emits the 40-hex sha as the first field of each
        # blame block — stable across the porcelain's per-commit grouping.
        raw = _git(
            "blame", "--line-porcelain", f"-L{line},{line}", "--", path,
            check=True,
        )
        if raw is None:
            print(f"could not blame {path}:{line}", file=sys.stderr)
            return 1
        first = raw.split(None, 1)[0] if raw.split(None, 1) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", first or ""):
            print(f"could not blame {path}:{line}", file=sys.stderr)
            return 1
        if set(first) == {"0"}:
            print(f"{path}:{line} is uncommitted work — no session to report")
            return 0
        return cmd_commit(first)

    commits = scan("main", limit=limit, path=path, follow=True)
    if commits is None:
        print(f"could not read history for {path}", file=sys.stderr)
        return 1
    if not commits:
        print(f"{path}: no commits found (not on main, or beyond --limit)")
        return 1
    print(f"{path} — {len(commits)} commit(s) in the last {limit} on main:")
    return _report_commits(commits, path)


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
    def _positive(v: str) -> int:
        """argparse type: a history bound must be positive.

        Validated at PARSE time rather than left to `scan` to raise, so a bad
        value is a clean usage error instead of a traceback mid-run. A
        nonpositive limit previously omitted the bound entirely, making the scan
        silently unbounded while every message still described it as capped.
        """
        n = int(v)
        if n <= 0:
            raise argparse.ArgumentTypeError(f"must be a positive integer, got {n}")
        return n

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="PR number (open or merged)")
    g.add_argument("--commit", help="commit-ish")
    g.add_argument("--branch", help="branch name")
    g.add_argument("--file", help="path — sessions behind commits touching it")
    g.add_argument("--session", help="8-hex session id")
    g.add_argument(
        "--coverage",
        nargs="?",
        const=200,
        type=_positive,
        metavar="N",
        help="attribution rate over the last N commits",
    )
    ap.add_argument(
        "--line",
        type=_positive,
        help="with --file: attribute that single line via git blame",
    )
    ap.add_argument(
        "--limit",
        type=_positive,
        help="commits of main to scan (--session/--file default 400, --pr "
        "default 4000)",
    )
    args = ap.parse_args()
    if args.line is not None and args.file is None:
        ap.error("--line requires --file")

    # Bind the CHOSEN MODE once, from which option argparse actually set — never
    # re-derive it from a value's truthiness. Truthiness conflates "not supplied"
    # with "supplied empty", and because the fall-through target is a DIFFERENT
    # command, `--branch "$UNSET_VAR"` silently ran a full-history coverage report
    # and exited 0. A caller asking "which session authored this commit?" got a
    # different question's answer and a success code.
    handlers = {
        "pr": lambda a: cmd_pr(a.pr, a.limit),
        "commit": lambda a: cmd_commit(a.commit),
        "branch": lambda a: cmd_branch(a.branch),
        "file": lambda a: cmd_file(a.file, a.line, a.limit or 400),
        "session": lambda a: cmd_session(a.session.strip().lower(), a.limit or 400),
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
