"""``python -m genesis session-address`` — which peer name a session id answers to.

A thin CLI over :mod:`genesis.session_awareness.peer_address`, plus the
:func:`lookup` the ``session_address`` MCP tool shares with it, so a prefix
resolves the same way on both. Read-only: it reads the Genesis database (to
expand a short id) and Claude Code's session registry, and writes nothing.

``--check`` runs Claude Code's own ``claude agents --json`` and reports every
session where the two disagree. That is the drift test for the undocumented
registry format this lookup depends on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys

from genesis.session_awareness import peer_address as pa

# `claude agents --json` MEASURED at 0.53-0.59 s over 5 runs on one install.
# The bound exists because this is an interactive diagnostic run from an agent's
# turn, and a wedged CLI would hang that turn with no other recovery; 60 s is
# about a hundred times the measured cost.
_ORACLE_TIMEOUT_S = 60


async def lookup(db, queries: list[str]) -> list[dict]:
    """Resolve each query (a full session id or a unique prefix) to its address.

    ``db`` is an open aiosqlite connection, used only to expand a prefix through
    the same resolver the charter tools use. Each result is
    ``Resolution.as_dict()`` plus ``query``. A prefix that matches no session,
    or more than one, is reported as ``unresolved-prefix`` rather than looked up
    as though it were an id.
    """
    from genesis.db.crud.session_charters import is_full_session_id, resolve_session_id

    # A list, not a dict: a query asked twice gets two answers.
    pairs: list[tuple[str, str | None]] = []
    for query in queries:
        q = query.strip().lower()
        if is_full_session_id(q):
            pairs.append((query, q))
        elif db is None:
            pairs.append((query, None))
        else:
            # resolve_session_id returns anything of 32+ characters unchanged, so
            # a long prefix is expanded through its first 31 and the result must
            # still begin with the whole query.
            cand = await resolve_session_id(db, q[:31])
            ok = is_full_session_id(cand) and cand.lower().startswith(q.lower())
            pairs.append((query, cand if ok else None))
    resolved = pa.resolve_many(sorted({sid for _q, sid in pairs if sid}))
    why = (
        "no single known session id starts with this"
        if db is not None
        else "the Genesis database was not found, so only a full session id resolves"
    )
    out = []
    for query, sid in pairs:
        if sid is None:
            out.append(
                {
                    "query": query,
                    "session_id": None,
                    "status": "unresolved-prefix",
                    "detail": [why],
                    "display": "",
                }
            )
            continue
        out.append({"query": query, **resolved[sid].as_dict()})
    return out


def _oracle() -> dict[str, str] | str:
    """``sessionId -> name`` from Claude Code itself, or an error string."""
    exe = shutil.which("claude")
    if exe is None:
        return "the claude CLI is not on PATH"
    try:
        proc = subprocess.run(
            [exe, "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=_ORACLE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"claude agents --json failed: {exc}"
    if proc.returncode != 0:
        return f"claude agents --json exited {proc.returncode}"
    try:
        rows = json.loads(proc.stdout)
    except ValueError:
        return "claude agents --json printed something that is not JSON"
    if not isinstance(rows, list):
        return "claude agents --json did not print a list"
    return {
        r["sessionId"]: r.get("name")
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("sessionId"), str)
    }


def compared(results: list[dict]) -> list[dict]:
    """The results a check can judge: the ones that name a full session id."""
    return [res for res in results if res.get("session_id")]


# The answers a check can compare with Claude Code's. Anything else (a format
# change, an ambiguity, no registry) is a failure to establish agreement, so it
# is reported, never read as "both say absent".
_COMPARABLE = (pa.OK, pa.NOT_REACHABLE, pa.UNNAMED)


def disagreements(results: list[dict], oracle: dict[str, str]) -> list[str]:
    """Every session where this lookup and Claude Code's own answer differ."""
    problems = []
    for res in compared(results):
        sid = res["session_id"]
        if res["status"] not in _COMPARABLE:
            problems.append(f"{sid}: this lookup could not decide ({res['status']})")
            continue
        ours = res["name"] if res["status"] == pa.OK else None
        theirs = oracle.get(sid)
        if ours != theirs:
            problems.append(f"{sid}: this lookup says {ours!r}, claude agents says {theirs!r}")
    return problems


def _line(res: dict) -> str:
    head = (res.get("session_id") or res["query"])[:8]
    if res["status"] == "unresolved-prefix":
        return f"{res['query']}: not resolved ({'; '.join(res['detail'])})"
    text = res["display"] or f"-> ({res['status']})"
    if res["status"] != pa.OK and res["detail"]:
        text += "  [" + "; ".join(res["detail"]) + "]"
    return f"{head} {text}"


async def _open_db():
    from urllib.request import pathname2url

    import aiosqlite

    from genesis.env import genesis_db_path

    path = genesis_db_path()
    if not path.exists():
        return None
    # Percent-encoded, as session_heartbeat.ro_uri does: a '?' or '#' in the
    # path would otherwise be read as URI syntax and open the wrong file.
    return await aiosqlite.connect(f"file:{pathname2url(str(path))}?mode=ro", uri=True)


async def _run(queries: list[str]) -> list[dict]:
    db = await _open_db()
    try:
        return await lookup(db, queries)
    finally:
        if db is not None:
            await db.close()


def _cmd(args: argparse.Namespace) -> int:
    results = asyncio.run(_run(args.ids))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for res in results:
            print(_line(res))
    if not args.check:
        return 0
    oracle = _oracle()
    if isinstance(oracle, str):
        print(f"check: {oracle}", file=sys.stderr)
        return 2
    judged = compared(results)
    skipped = len(results) - len(judged)
    problems = disagreements(results, oracle)
    for p in problems:
        print(f"check: DISAGREE {p}", file=sys.stderr)
    note = f" ({skipped} not compared: unresolved prefix)" if skipped else ""
    if not judged:
        # A check that compared nothing has checked nothing: never report it clean.
        print(f"check: nothing compared{note}", file=sys.stderr)
        return 2
    if not problems:
        print(
            f"check: agrees with claude agents on {len(judged)} of {len(results)}{note}",
            file=sys.stderr,
        )
    return 1 if problems else 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "session-address",
        help="Which SendMessage name a Claude Code session id answers to",
    )
    p.add_argument("ids", nargs="+", help="Session id, or a unique prefix of one")
    p.add_argument("--json", action="store_true", help="Print full results as JSON")
    p.add_argument(
        "--check",
        action="store_true",
        help="Compare against `claude agents --json`; exit 1 on any disagreement",
    )
    p.set_defaults(func=_cmd)
