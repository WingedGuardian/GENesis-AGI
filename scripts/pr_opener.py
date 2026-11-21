#!/usr/bin/env python3
"""Which Claude Code session opened a pull request, and where it is now.

    scripts/pr_opener.py <N> [--repo OWNER/NAME] [--json]

Read-only. It asks GitHub for the PR and answers from the first source that
names a session:

0. ``corrected``: an ``Opened-by-session-correction: <8hex>`` line in the CURRENT
   body, written by hand to fix a wrong attribution. It counts only when the body
   GitHub first recorded did not carry it (a body copied from another PR copies
   its correction too). It overrides the record below, which is still reported
   (``recorded_opener``), because the record by design cannot be changed by
   editing the line it came from;
1. the body's ``Opened-by-session: <8hex>`` line as GitHub's edit history first
   recorded it: the newest stamped version written within ``CREATION_WINDOW`` of
   the PR's creation (where the stamp hook corrects a copied line), else the
   oldest stamped version. GitHub keeps every version (unless the PR's author
   deletes one by hand), so a later rewrite that drops or replaces the line cannot
   change the answer. Its source is ``body`` when the current body still says the
   same, else ``history``;
2. ``body``: the current body's line, when no version read carries one (the body
   was never edited, or the history read was cut: then it is not authoritative);
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
from datetime import datetime, timedelta
from pathlib import Path

LINE = re.compile(r"^Opened-by-session: ([0-9a-f]{8})[ \t]*$")
#: A deliberate fix of a wrong attribution, written by hand in the CURRENT body.
#: It overrides the history (which a rewrite must not be able to change); the
#: stamp hook never writes or removes it.
CORRECTION = re.compile(r"^Opened-by-session-correction: ([0-9a-f]{8})[ \t]*$")
#: A CommonMark code fence: up to three spaces, then three or more backticks or
#: tildes. It closes only on a fence of the SAME character, at least as long, with
#: nothing after it; a fence never closed runs to the end of the text.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_REMOTE = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")
# Same rule as scripts/hooks/gh_merge.py _SESSION_TRAILER (either case).
TRAILER = re.compile(r"^Genesis-Session: ([0-9a-fA-F]{8})$")
EDITS_PAGE = 100
#: How long after a PR's creation the stamp hook's own write can land: its 45 s
#: budget after a create call that may itself take seconds, with margin. Bots that
#: edit later (CodeRabbit's first edit came at 98 s or more, MEASURED) keep the
#: text they find and add no session id.
CREATION_WINDOW = timedelta(seconds=120)
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


def stamped(text: str, pattern: re.Pattern[str] = LINE) -> str | None:
    """The first ``Opened-by-session`` line in ``text``, outside fences and quotes.

    A PR that documents the line shows it in a fence or a quote; that example is
    not a stamp. A quoted line starts with ``>``, so LINE never matches it."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for line, fenced in zip(lines, fenced_flags(lines), strict=True):
        m = None if fenced else pattern.match(line)
        if m:
            return m.group(1)
    return None


def correction(body: str) -> str | None:
    """The current body's ``Opened-by-session-correction`` line, if any."""
    return stamped(body, CORRECTION)


def fenced_flags(lines: list[str]) -> list[bool]:
    """For each line, whether it is a fence line or inside a fenced code block."""
    flags: list[bool] = []
    opened: tuple[str, int] | None = None
    for line in lines:
        m = _FENCE.match(line)
        if opened is None:
            # A backtick fence's info string cannot hold a backtick (CommonMark).
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                opened = (m.group(1)[0], len(m.group(1)))
                flags.append(True)
            else:
                flags.append(False)
            continue
        flags.append(True)
        if (
            m
            and m.group(1)[0] == opened[0]
            and len(m.group(1)) >= opened[1]
            and not m.group(2).strip()
        ):
            opened = None
    return flags


def _when(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def opener(
    body: str, edits: list[dict], commits: list[dict], created_at: str | None = None
) -> tuple[str | None, str]:
    """(prefix, source) per the order in the module docstring; (None, "none")."""
    now = stamped(body)
    # MEASURED: GitHub lists edits newest first and the original body last, and two
    # versions can share a timestamp (an original and Devin's edit in the same
    # second on one PR), so the list's own order is used, reversed, never a sort.
    versions = [(e, stamped(e.get("diff") or "")) for e in reversed(edits)]
    stamps = [(e, s) for e, s in versions if s]
    if stamps:
        chosen = stamps[0][1]
        # A body opened with a COPIED line (a rework reusing another PR's body) is
        # corrected by the stamp hook seconds later, so the newest stamp written in
        # the creation window wins; after it, a rewrite cannot change the answer.
        created = _when(created_at)
        if created is not None:
            early = [
                s
                for e, s in stamps
                if (_when(e.get("editedAt")) or created) <= created + CREATION_WINDOW
            ]
            if early:
                # A bot that read the body before the stamp can write the copied
                # line back inside the window: the newest stamp that DIFFERS from
                # the one the body was created with is the correction, else that one.
                first = stamped(versions[0][0].get("diff") or "") if versions else None
                changed = [s for s in early if s != first]
                chosen = changed[-1] if changed else early[-1]
        return chosen, "body" if chosen == now else "history"
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


#: A read against GitHub's API answers in about a second; this bounds a hung
#: connection so an interactive lookup returns with a named error, not never.
_GH_TIMEOUT_S = 60
#: Local, read-only git plumbing (rev-parse, worktree list): milliseconds normally;
#: this bounds only a stuck filesystem or a held lock.
_GIT_TIMEOUT_S = 30


def _gh(args: list[str]) -> dict:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def _git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=_GIT_TIMEOUT_S
    )
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
    if not re.fullmatch(r"[0-9a-f]{8}", prefix):
        return []
    if not db.is_file():
        raise DbUnreadable("no session database on this install")
    queries = (
        # A charter is keyed by whichever id its writer had: MEASURED on this
        # install, of two sessions whose ids differ, one charter is under each.
        "SELECT s.id, s.session_type, s.status, s.started_at, s.last_activity_at, s.topic,"
        " COALESCE("
        "  (SELECT c.mission FROM session_charters c WHERE c.session_id = s.id),"
        "  (SELECT c.mission FROM session_charters c WHERE c.session_id = s.cc_session_id)"
        " ) AS mission"
        " FROM cc_sessions s WHERE s.id LIKE ? OR s.cc_session_id LIKE ? ORDER BY s.started_at",
        # An install from before charters has no session_charters table.
        "SELECT s.id, s.session_type, s.status, s.started_at, s.last_activity_at, s.topic,"
        " NULL AS mission"
        " FROM cc_sessions s WHERE s.id LIKE ? OR s.cc_session_id LIKE ? ORDER BY s.started_at",
    )
    # A quarantined database is never opened, not even read-only (the admission
    # fence every script-side opener consults; it fails closed).
    hooks = str(Path(__file__).resolve().parent / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    try:
        from db_admission_check import database_is_fenced
    except Exception:  # noqa: BLE001 - no fence helper: do not open
        raise DbUnreadable("the database admission check could not be loaded") from None
    if database_is_fenced(db):
        raise DbUnreadable(
            "the database is fenced (quarantined, or its fence state could not be read),"
            " so it was not opened"
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


def local_repos(cwd: Path | None = None) -> set[str]:
    """``owner/name`` (casefolded) of every github.com remote of this checkout."""
    out = _git(["remote", "-v"], cwd or Path(__file__).resolve().parent)
    found = set()
    for line in out.splitlines():
        parts = line.split()
        m = _REMOTE.search(parts[1]) if len(parts) >= 2 else None
        if m:
            found.add(m.group(1).casefold())
    return found


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
    prefix, source = opener(
        pr.get("body") or "", history["nodes"], pr.get("commits") or [], pr.get("createdAt")
    )
    recorded = prefix if source in ("body", "history") else None
    truncated = history["totalCount"] > len(history["nodes"])
    # The current body answered only because no version read carried a stamp. With
    # the history cut, an unread version may hold the real first stamp.
    unproven = (
        source == "body"
        and truncated
        and not any(stamped(e.get("diff") or "") for e in history["nodes"])
    )
    nodes = history["nodes"]
    # The oldest node is the body as created (`last:` reads the oldest versions).
    # A never-edited body has no nodes: a correction there came with the body.
    original = (nodes[-1].get("diff") or "") if nodes else None
    fixed = correction(pr.get("body") or "")
    if fixed and original is not None and not correction(original):
        prefix, source = fixed, "corrected"
    committers: list[str] = []
    for commit in pr.get("commits") or []:
        for s in trailer_sessions(_message(commit)):
            if s not in committers:
                committers.append(s)
    db_problem = None
    # None = not looked up (the database was not read), never "no such session".
    opener_rows: list[dict] | None = None
    commit_rows: dict[str, list[dict] | None] = {s: None for s in committers}
    try:
        db = _db_path()
        opener_rows = sessions(prefix, db) if prefix else None
        commit_rows = {s: sessions(s, db) for s in committers}
    except (DbUnreadable, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        db_problem = str(exc)[:300]
    # A branch name says something about this install's worktrees only for a PR
    # whose head lives in a repository this checkout has as a remote: never a
    # fork's, never another project's (--repo).
    worktree, worktree_note = None, "none on this install"
    try:
        if pr.get("isCrossRepository"):
            worktree_note = "not looked up: a fork's branch"
        elif repo.casefold() not in local_repos():
            worktree_note = "not looked up: not one of this checkout's repositories"
        else:
            worktree = worktree_for(pr["headRefName"])
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        worktree_note = "not looked up: git could not list this checkout's worktrees"
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
        "authoritative": source == "corrected" or (source in ("body", "history") and not unproven),
        "recorded_opener": recorded,
        "recorded_unproven": unproven,
        "body_line_now": stamped(pr.get("body") or ""),
        "opener_sessions": opener_rows,
        "commit_sessions": commit_rows,
        "db_problem": db_problem,
        "worktree": worktree,
        "worktree_note": worktree_note,
        "history_truncated": truncated,
        # gh returns at most 100 commits (MEASURED, gh 2.100: 123 -> 100); the
        # first commit is always among them, later sessions may not be.
        "commits_capped": len(pr.get("commits") or []) >= 100,
    }


def _describe(rows: list[dict] | None) -> str:
    if rows is None:
        return "    (session database not read; see the NOTE below)"
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
    print(f"  branch {r['branch']}, worktree {r['worktree'] or '(' + r['worktree_note'] + ')'}")
    if r["opener"]:
        how = {
            "body": "the body's Opened-by-session line",
            "history": "the oldest version of the body that carries the line",
            "corrected": "the body's Opened-by-session-correction line",
            "commit": "the first commit's Genesis-Session trailer (wrote it; may not have opened it)",
        }
        print(f"  opened by session {r['opener']}, from {how[r['source']]}")
        print(_describe(r["opener_sessions"]))
        if r["source"] == "corrected" and r["recorded_opener"] != r["opener"]:
            was = r["recorded_opener"] or "no session"
            proof = ", not proven: the history read was cut" if r["recorded_unproven"] else ""
            print(f"  (corrected by hand; the recorded stamp named {was}{proof})")
        if not r["authoritative"] and r["source"] == "body":
            print("  not proven: the edit history was cut, and an unread version may differ")
        if r["source"] == "history":
            now = r["body_line_now"]
            print(f"  the body now says {now}" if now else "  the body no longer carries the line")
    else:
        print(
            "  no session recorded: no Opened-by-session line, none in the edit history"
            + (f" read (the oldest {EDITS_PAGE} versions)" if r["history_truncated"] else "")
            + ", no Genesis-Session trailer on the first commit"
        )
    others = {s: rows for s, rows in r["commit_sessions"].items() if s != r["opener"]}
    for s, rows in others.items():
        print(f"  also committed by session {s}")
        print(_describe(rows))
    if r["db_problem"]:
        print(f"  NOTE: the session database could not be read ({r['db_problem']})")
    if r["history_truncated"]:
        print(f"  NOTE: only the oldest {EDITS_PAGE} body edits were read")
    if r["commits_capped"]:
        print("  NOTE: gh returns at most 100 commits; later sessions may be missing above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
