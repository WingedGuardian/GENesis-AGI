#!/usr/bin/env python3
"""Which Claude Code session opened a pull request, and where it is now.

    scripts/pr_opener.py <N> [--repo OWNER/NAME] [--json]

Read-only. It asks GitHub for the PR and answers from the first source that
names a session:

1. the OLDEST version of the body that carries an ``Opened-by-session: <8hex>``
   line: GitHub keeps every version in the PR's edit history, so a later
   rewrite that drops or replaces the line cannot change the answer. Its source
   is ``body`` when the current body still says the same, else ``history``;
2. ``body``: the current body's line, when the body was never edited;
3. ``commit``: the ``Genesis-Session: <8hex>`` trailer of the PR's first
   commit, which ``scripts/hooks/prepare-commit-msg`` stamps. That names the
   session that wrote the first commit, which is usually, not always, the one
   that opened the PR.

The prefix is then matched against ``cc_sessions`` (``id`` or
``cc_session_id``) in the install's database, with the session's charter
mission when one is recorded, and the head branch against ``git worktree
list``. Every session named by a commit trailer is listed too, since a PR
finished by a second session carries both. Nothing is written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

LINE = re.compile(r"^Opened-by-session: ([0-9a-f]{8})[ \t]*$")
#: A Markdown code fence opens or closes on a line starting with ``` or ~~~.
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
# Same rule as scripts/hooks/gh_merge.py _SESSION_TRAILER (either case).
TRAILER = re.compile(r"^Genesis-Session: ([0-9a-fA-F]{8})$")
EDITS_PAGE = 100
# GitHub lists edits newest first, and the original body is the last node, so
# `last:` reads the OLDEST versions: the ones a later rewrite cannot have touched.
_EDITS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){userContentEdits(last:PAGE){totalCount "
    "nodes{editedAt diff editor{login}}}}}}"
).replace("PAGE", str(EDITS_PAGE))


def trailer_sessions(message: str) -> list[str]:
    """Session prefixes in the LAST paragraph of a commit message, in order."""
    paragraphs = [p for p in message.replace("\r\n", "\n").split("\n\n") if p.strip()]
    found: list[str] = []
    for line in (paragraphs[-1] if paragraphs else "").split("\n"):
        m = TRAILER.match(line.rstrip("\r"))
        if m and m.group(1).lower() not in found:
            found.append(m.group(1).lower())
    return found


def stamped(text: str) -> str | None:
    """The first ``Opened-by-session`` line in ``text``, outside fences and quotes.

    A PR that documents the line shows it in a fence or a quote; that example is
    not a stamp. A quoted line starts with ``>``, so LINE never matches it."""
    fenced = False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else LINE.match(line)
        if m:
            return m.group(1)
    return None


def opener(body: str, edits: list[dict], commits: list[dict]) -> tuple[str | None, str]:
    """(prefix, source) per the order in the module docstring; (None, "none")."""
    now = stamped(body)
    # MEASURED: GitHub lists edits newest first and the original body last, and two
    # versions can share a timestamp (an original and Devin's edit in the same
    # second on one PR), so the list's own order is used, reversed, never a sort.
    for edit in reversed(edits):
        first = stamped(edit.get("diff") or "")
        if first:
            return first, "body" if first == now else "history"
    if now:
        return now, "body"
    for commit in commits[:1]:
        found = trailer_sessions(_message(commit))
        if found:
            return found[0], "commit"
    return None, "none"


def _message(commit: dict) -> str:
    return f"{commit.get('messageHeadline') or ''}\n\n{commit.get('messageBody') or ''}"


class DbUnreadable(Exception):
    """The install's database could not be read; the PR-side answer still stands."""


def _gh(args: list[str]) -> dict:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def _git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[:300]}")
    return out.stdout


def _db_path() -> Path:
    # genesis.env.genesis_db_path's rule, without importing a tree: the
    # database lives in the MAIN checkout, also when this runs from a worktree.
    if os.environ.get("GENESIS_DB_PATH"):
        return Path(os.environ["GENESIS_DB_PATH"]).expanduser()
    common = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        Path(__file__).resolve().parent,
    ).strip()
    return Path(common).parent / "data" / "genesis.db"


def sessions(prefix: str, db: Path) -> list[dict]:
    """cc_sessions rows whose id or cc_session_id starts with ``prefix``."""
    if not re.fullmatch(r"[0-9a-f]{8}", prefix) or not db.is_file():
        return []
    cols = "SELECT s.id, s.session_type, s.status, s.started_at, s.last_activity_at, s.topic"
    where = " WHERE s.id LIKE ? OR s.cc_session_id LIKE ? ORDER BY s.started_at"
    queries = (
        cols + ", c.mission FROM cc_sessions s"
        " LEFT JOIN session_charters c ON c.session_id = s.id" + where,
        # An install from before charters has no session_charters table.
        cols + ", NULL AS mission FROM cc_sessions s" + where,
    )
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise DbUnreadable(str(exc)) from None
    try:
        con.row_factory = sqlite3.Row
        last: sqlite3.Error | None = None
        for query in queries:
            try:
                rows = con.execute(query, (prefix + "%", prefix + "%")).fetchall()
            except sqlite3.Error as exc:
                last = exc
                continue
            return [dict(r) for r in rows]
        raise DbUnreadable(str(last))
    finally:
        con.close()


def worktree_for(branch: str, cwd: Path | None = None) -> str | None:
    """The worktree that has ``branch`` checked out, in this repository."""
    out = _git(["worktree", "list", "--porcelain"], cwd or Path(__file__).resolve().parent)
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def report(number: int, repo: str) -> dict:
    pr = _gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,url,state,createdAt,headRefName,isCrossRepository,body,commits",
        ]
    )
    owner, name = repo.split("/", 1)
    data = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={_EDITS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
    )
    if data.get("errors") or not (data.get("data") or {}).get("repository"):
        raise RuntimeError(f"the edit-history query failed: {str(data.get('errors'))[:300]}")
    history = data["data"]["repository"]["pullRequest"]["userContentEdits"]
    prefix, source = opener(pr.get("body") or "", history["nodes"], pr.get("commits") or [])
    committers: list[str] = []
    for commit in pr.get("commits") or []:
        for s in trailer_sessions(_message(commit)):
            if s not in committers:
                committers.append(s)
    db_problem = None
    opener_rows: list[dict] = []
    commit_rows: dict[str, list[dict]] = {s: [] for s in committers}
    try:
        db = _db_path()
        opener_rows = sessions(prefix, db) if prefix else []
        commit_rows = {s: sessions(s, db) for s in committers}
    except (DbUnreadable, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        db_problem = str(exc)[:300]
    # A fork's branch name says nothing about this install's worktrees.
    worktree = None if pr.get("isCrossRepository") else worktree_for(pr["headRefName"])
    return {
        "pr": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "state": pr["state"],
        "created_at": pr["createdAt"],
        "branch": pr["headRefName"],
        "opener": prefix,
        "source": source,
        # Only a stamped line names the opener; a trailer names who wrote the
        # first commit, usually but not always the same session.
        "authoritative": source in ("body", "history"),
        "body_line_now": stamped(pr.get("body") or ""),
        "opener_sessions": opener_rows,
        "commit_sessions": commit_rows,
        "db_problem": db_problem,
        "worktree": worktree,
        "history_truncated": history["totalCount"] > len(history["nodes"]),
    }


def _describe(rows: list[dict]) -> str:
    if not rows:
        return "    (no cc_sessions row on this install)"
    return "\n".join(
        f"    {r['id']}  {r['session_type']}/{r['status']}  started {r['started_at']}"
        f"  last active {r['last_activity_at']}\n"
        f"      {(r['mission'] or r['topic'] or '(no mission or topic)')[:200]}"
        for r in rows
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("number", type=int)
    ap.add_argument("--repo", default="WingedGuardian/GENesis-AGI")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        r = report(args.number, args.repo)
    except (
        RuntimeError,
        KeyError,
        ValueError,
        TypeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"pr_opener: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"#{r['pr']} {r['title']} ({r['state']}, opened {r['created_at']})")
    print(f"  branch {r['branch']}, worktree {r['worktree'] or '(none on this install)'}")
    if r["opener"]:
        how = {
            "body": "the body's Opened-by-session line",
            "history": "the oldest version of the body that carries the line",
            "commit": "the first commit's Genesis-Session trailer (wrote it; may not have opened it)",
        }
        print(f"  opened by session {r['opener']}, from {how[r['source']]}")
        print(_describe(r["opener_sessions"]))
        if r["source"] == "history":
            now = r["body_line_now"]
            print(f"  the body now says {now}" if now else "  the body no longer carries the line")
    else:
        print(
            "  no session recorded: no Opened-by-session line, none in the edit history,"
            " no Genesis-Session trailer on the first commit"
        )
    others = {s: rows for s, rows in r["commit_sessions"].items() if s != r["opener"]}
    for s, rows in others.items():
        print(f"  also committed by session {s}")
        print(_describe(rows))
    if r["db_problem"]:
        print(f"  NOTE: the session database could not be read ({r['db_problem']})")
    if r["history_truncated"]:
        print(f"  NOTE: only the oldest {EDITS_PAGE} body edits were read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
