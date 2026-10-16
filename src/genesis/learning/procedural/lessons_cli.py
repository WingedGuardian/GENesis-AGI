"""``python -m genesis lessons``: list, sample, review and deprecate lessons.

- ``list``: every lesson row with its origin and state (live, held for review,
  deprecated with a reason, quarantined), and a separate count of earned rows.
- ``sample``: recent ``repeat: session`` fires from the per-session fire logs the
  procedure advisor writes, newest first, with the stored excerpt, for rating
  whether a lesson fires where it should. It reads one file per recently active
  session directory and never walks the tree.
- ``review <id>``: the owner's approval. A lesson held for review (written
  deprecated with ``HELD_REASON``) becomes live; a live, unreviewed lesson loses
  its label. It is the only path that revives a held lesson, and a dispatched
  session or one of external origin is refused it.
- ``deprecate <id> --reason R``: takes a lesson out of service; for a held one it
  is the rejection. A shipped lesson stays deprecated across seeds.

Exit codes: 0 done, 1 refused, 2 not found (or no database) or bad arguments, 3
the database is fenced (quarantined), 4 written but the trigger cache was not rebuilt,
5 the database was busy (locked) or unreadable when queried.

What the owner approves is shown first: ``list --held`` and ``review`` print each
lesson's principle, steps and trigger, with control characters escaped, because
a held lesson's text is untrusted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

DONE, REFUSED, NOT_FOUND, FENCED, CACHE_STALE, BUSY = 0, 1, 2, 3, 4, 5


class _Fenced(Exception):
    pass


class _NoDatabase(Exception):
    pass


def _db_path() -> Path:
    from genesis.env import genesis_db_path

    return Path(genesis_db_path())


async def _connect(write: bool):
    """The install database: read-only unless writing, never created."""
    import aiosqlite

    from genesis.db.admission import assert_admitted

    path = _db_path()
    try:
        assert_admitted(path)
    except Exception as exc:
        raise _Fenced(str(exc)) from exc
    try:
        if write:
            from genesis.db.connection import connect_aiosqlite_rw

            db = await connect_aiosqlite_rw(path, existing_only=True)
        else:  # an encoded URI: as_uri() escapes ? and # in the path
            db = await aiosqlite.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise _NoDatabase(f"{path}: {exc}") from exc
    except sqlite3.DatabaseError as exc:  # the guarded open's own admission refusal
        raise _Fenced(str(exc)) from exc
    db.row_factory = aiosqlite.Row
    return db


def _record(row: dict) -> dict:
    try:
        value = json.loads(row["lesson"])
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _state(row: dict) -> str:
    from genesis.learning.procedural.lesson_shape import HELD_REASON, is_live_lesson

    if row["quarantined"]:
        return "quarantined"
    if row["deprecated"]:
        reason = row.get("deprecated_reason") or ""
        return "held" if reason == HELD_REASON else f"deprecated ({_safe(reason)})"
    return "live" if is_live_lesson(row["lesson"]) else "malformed record"


async def _rebuild(db, force: bool = False) -> int:
    """Rebuild the trigger cache, which is the live advisor's file: only from the
    install's default database, into a cache redirected with
    GENESIS_PROCEDURE_TRIGGERS, or with --rebuild-cache (a relocated install), so
    a copy of the database never writes it."""
    from genesis.env import repo_root
    from genesis.learning.procedural.trigger_cache import regenerate

    own = _db_path().resolve() == (repo_root() / "data" / "genesis.db").resolve()
    if not (own or force or os.environ.get("GENESIS_PROCEDURE_TRIGGERS")):
        print(
            f"cache not rebuilt: {_db_path()} is not this install's default database; "
            "re-run with --rebuild-cache if it is the live one, or set GENESIS_PROCEDURE_TRIGGERS"
        )
        return CACHE_STALE
    try:
        await regenerate(db)
    except Exception as exc:
        print(f"written, but the trigger cache was not rebuilt: {exc}", file=sys.stderr)
        return CACHE_STALE
    return DONE


async def _list(args) -> int:
    from genesis.db.crud import procedural

    db = await _connect(write=False)
    try:
        rows = await procedural.list_lessons(db, include_quarantined=True, include_deprecated=True)
        earned = await db.execute_fetchall(
            "SELECT activation_tier, COUNT(*) FROM procedural_memory WHERE lesson IS NULL "
            "AND deprecated = 0 AND quarantined = 0 GROUP BY 1 ORDER BY 1"
        )
    finally:
        await db.close()
    shown = [r for r in rows if not args.held or _state(r) == "held"]
    for row in shown:
        rec = _record(row)
        print(
            f"{row['id']}  {_state(row):<16} {_safe(row['task_type'])}  "
            f"via={_safe(rec.get('authored_via', '?'))}/{_safe(rec.get('authored_in', '?'))} "
            f"origin={_safe(rec.get('origin', '?'))} reviewed={_safe(rec.get('reviewed', '?'))} "
            f"tier={row['activation_tier']} surfaced={row['surfaced_count']}"
        )
        if args.held:
            print("\n".join(_payload(row)))
    print(
        f"{len(shown)} lesson(s); earned rows (not lessons) by tier: "
        + (", ".join(f"{tier} {n}" for tier, n in earned) or "none")
    )
    return DONE


def _safe(value) -> str:
    """Text with control characters escaped, so stored text cannot rewrite the terminal."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))


def _loaded(text):
    try:
        return json.loads(text) if text else None
    except (ValueError, TypeError, RecursionError):
        return text


def _payload(row: dict) -> list[str]:
    """What the advisor would deliver for this lesson, one escaped line each."""
    spec = _loaded(row.get("trigger_spec"))
    steps = _loaded(row.get("steps"))
    lines = [f"    principle: {_safe(row.get('principle'))}"]
    for n, step in enumerate(steps if isinstance(steps, list) else [steps], 1):
        lines.append(f"    step {n}: {_safe(step)}")
    lines.append(f"    tools: {_safe(_loaded(row.get('tool_trigger')))}")
    if isinstance(spec, dict):
        lines.append(
            f"    patterns: {_safe(spec.get('patterns'))}  commands: {_safe(spec.get('commands'))}"
            f"  repeat: {_safe(spec.get('repeat'))}"
        )
    else:
        lines.append(f"    trigger_spec: {_safe(spec)}")
    return lines


def _fires(root: Path, days: float) -> list[dict]:
    """Fire records from logs written within ``days``, read without a lock (a
    partly written last line is skipped)."""
    cutoff = time.time() - days * 86400
    records = []
    try:
        session_dirs = list(os.scandir(root))
    except OSError:
        return records
    for entry in session_dirs:
        log = Path(entry.path) / "advisor_fires.jsonl"
        try:
            if log.stat().st_mtime < cutoff:
                continue
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
                ts = float(record["ts"])
                # Only a well-formed record from the window: a foreign or
                # tampered line is skipped, never allowed to break the listing.
                if not (math.isfinite(ts) and cutoff <= ts <= time.time() + 86400):
                    continue
                if not isinstance(record.get("procedure_id"), str):
                    continue
                records.append({**record, "ts": ts, "session": entry.name})
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
                continue
    return records


async def _sample(args) -> int:
    from genesis.db.crud import procedural

    root = Path(os.environ.get("GENESIS_SESSIONS_DIR") or Path.home() / ".genesis" / "sessions")
    fires = sorted(_fires(root, args.days), key=lambda r: r["ts"], reverse=True)[: args.n]
    db = await _connect(write=False)
    try:
        rows = {
            pid: await procedural.get_by_id(db, pid)
            for pid in {f.get("procedure_id") for f in fires}
        }
    finally:
        await db.close()
    print(
        f"{len(fires)} fire(s) of repeat-session lessons in the last {args.days:g} day(s); "
        "the principle shown is the lesson's current text"
    )
    for fire in fires:
        row = rows.get(fire.get("procedure_id"))
        lesson = f"{_safe(row['task_type'])}: {_safe(row['principle'])}" if row else "(row gone)"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(fire["ts"])))
        print(
            f"\n{when}  session {_safe(fire['session'][:8])}  agent {_safe(fire.get('agent', '?'))}  "
            f"tool {_safe(fire.get('tool', '?'))}\n  lesson: {lesson}\n"
            f"  input:  {_safe(fire.get('excerpt') or '(no excerpt)')}"
        )
    return DONE


def _cacheable(row: dict) -> bool:
    """Whether the advisor's cache would carry this lesson as it stands."""
    from genesis.learning.procedural.trigger_cache import _lesson_entry

    try:
        return _lesson_entry(row) is not None
    except Exception:
        return False


def _comparable(blob) -> bool:
    """True for an embedding a cosine can compare: the right bytes, finite, not all zero."""
    from genesis.learning.procedural.embedding import unpack_embedding

    vec = unpack_embedding(blob)
    return vec is not None and all(math.isfinite(x) for x in vec) and any(vec)


def _unattended() -> str | None:
    """Why this session may not approve a lesson, or None when it may.

    Approval is the owner's act: a dispatched (unsupervised) session, or one of
    external origin, could be steered by the very text a held lesson carries.
    A foreground session the owner works through, or a terminal, may approve.
    """
    from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED, session_origin_from_env
    from genesis.security.immunity_shadow import DispatchGateRefused, guard_human_gate

    try:
        guard_human_gate("lesson_review")
    except DispatchGateRefused:
        return "a dispatched session cannot approve a lesson; run review from a foreground session or a terminal"
    if session_origin_from_env() == ORIGIN_EXTERNAL_UNTRUSTED:
        return "a session of external origin cannot approve a lesson"
    return None


async def _review(args) -> int:
    from genesis.db.crud import procedural
    from genesis.learning.procedural.lesson_shape import HELD_REASON, is_live_lesson
    from genesis.learning.procedural.operations import _best_same_procedure_match

    refusal = _unattended()
    if refusal:
        print(f"refused: {refusal}")
        return REFUSED
    db = await _connect(write=True)
    try:
        # Hold the write lock for the whole review: what is checked here (the
        # row, its live twins) is what gets approved, and no writer can change
        # either in between.
        await db.execute("BEGIN IMMEDIATE")
        row = await procedural.get_by_id(db, args.id)
        if row is None or row.get("lesson") is None:
            print(f"no lesson with id {args.id}")
            return NOT_FOUND
        state = _state(row)
        if state in ("live", "held") and not _cacheable(row):
            print(
                f"refused: {args.id} has a field the advisor cannot read (its record, trigger, "
                "steps or principle); fix or deprecate it"
            )
            return REFUSED
        if state in ("live", "held"):
            print(f"approving {args.id} ({_safe(row['task_type'])}):")
            print("\n".join(_payload(row)))
        if state == "live":
            cursor = await db.execute(
                "UPDATE procedural_memory SET lesson = json_set(lesson, '$.reviewed', json('true')) "
                "WHERE id = ? AND deprecated = 0 AND quarantined = 0 "
                "AND json_valid(lesson) AND json_type(lesson) = 'object'",
                (args.id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                print(f"refused: {args.id}'s lesson record is not valid JSON to SQLite")
                return REFUSED
            await db.commit()
            print(f"reviewed: {args.id} (already live; now marked reviewed)")
            return await _rebuild(db, args.rebuild_cache)
        if state != "held":
            print(f"refused: {args.id} is {state}; only a lesson held for review can be revived")
            return REFUSED
        live = await procedural.list_by_task_type(
            db, row["task_type"], lessons=True, limit=None, live_only=True
        )
        siblings = await db.execute_fetchall(
            "SELECT id FROM procedural_memory WHERE task_type = ? AND deprecated_reason = ? AND id != ?",
            (row["task_type"], HELD_REASON, args.id),
        )
        if siblings:
            print("other held lessons for this task: " + ", ".join(r[0] for r in siblings))
        # --force waives only what it names: the twin check still runs over
        # every comparable row, and a twin it finds is always reported.
        verdict = "warning" if args.force else "refused"
        if not _comparable(row.get("principle_embedding")):
            print(
                f"{verdict}: twin check unavailable (no usable embedding on {args.id}; only "
                "live rows are re-embedded, so it stays that way); re-run with --force to "
                "revive anyway"
            )
            if not args.force:
                return REFUSED
        else:
            comparable = [r for r in live if _comparable(r.get("principle_embedding"))]
            twin, sim = _best_same_procedure_match(comparable, row["principle_embedding"])
            if twin is not None:
                print(
                    f"{verdict}: live lesson {twin['id']} already teaches this (similarity "
                    f"{sim:.2f}); deprecate this one, or re-run with --force"
                )
                if not args.force:
                    return REFUSED
            unusable = [r["id"] for r in live if r not in comparable]
            # The hourly backfill embeds only a missing (NULL) vector; nothing
            # repairs a malformed one, so the advice differs.
            missing = [
                r["id"] for r in live if r not in comparable and not r.get("principle_embedding")
            ]
            broken = [r["id"] for r in live if r not in comparable and r.get("principle_embedding")]
            if missing:
                print(
                    f"{verdict}: twin check incomplete (no embedding yet on "
                    + ", ".join(missing)
                    + "; the hourly backfill adds it); retry after it runs, or re-run with --force"
                )
            if broken:
                print(
                    f"{verdict}: twin check incomplete (a malformed embedding on "
                    + ", ".join(broken)
                    + ", which nothing repairs); deprecate that lesson, or re-run with --force"
                )
            if unusable and not args.force:
                return REFUSED
        # One compare-and-set write: it applies only while the row is still held.
        cursor = await db.execute(
            "UPDATE procedural_memory SET deprecated = 0, deprecated_reason = NULL, "
            "activation_tier = 'ADVISORY', lesson = json_set(lesson, '$.reviewed', json('true')) "
            "WHERE id = ? AND deprecated = 1 AND deprecated_reason = ? AND quarantined = 0 "
            "AND json_valid(lesson) AND json_type(lesson) = 'object'",
            (args.id, HELD_REASON),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            print(f"refused: {args.id}'s lesson record is not valid JSON to SQLite; it stays held")
            return REFUSED
        after = await procedural.get_by_id(db, args.id)
        if not is_live_lesson(after["lesson"]) or not _cacheable(after):
            await db.rollback()
            print(f"refused: {args.id} has a malformed lesson record; it stays held")
            return REFUSED
        await db.commit()
        print(f"reviewed: {args.id} is live")
        return await _rebuild(db, args.rebuild_cache)
    finally:
        if db.in_transaction:  # a refusal under the lock releases it
            await db.rollback()
        await db.close()


async def _deprecate(args) -> int:
    from genesis.db.crud import procedural
    from genesis.learning.procedural.lesson_shape import HELD_REASON, SHIPPED_RETIRED_REASON

    reason = args.reason.strip()
    if not reason or reason in (HELD_REASON, SHIPPED_RETIRED_REASON):
        print(
            "refused: give a reason of your own (the held and shipped-retired reasons are reserved)"
        )
        return NOT_FOUND
    db = await _connect(write=True)
    try:
        row = await procedural.get_by_id(db, args.id)
        if row is None or row.get("lesson") is None:
            print(f"no lesson with id {args.id}")
            return NOT_FOUND
        await procedural.update(db, args.id, deprecated=1, deprecated_reason=reason)
        print(f"deprecated: {args.id} ({_safe(reason)})")
        return await _rebuild(db, args.rebuild_cache)
    finally:
        await db.close()


def _run(handler):
    def run(args) -> int:
        try:
            return asyncio.run(handler(args))
        except _Fenced as exc:
            print(f"database not opened: {exc}", file=sys.stderr)
            return FENCED
        except _NoDatabase as exc:
            print(f"no database: {exc}", file=sys.stderr)
            return NOT_FOUND
        except sqlite3.Error as exc:  # locked past the busy timeout, or not a Genesis database
            print(f"database busy or unreadable: {exc}", file=sys.stderr)
            return BUSY

    return run


def _window(value: str) -> float:
    days = float(value)
    if not (math.isfinite(days) and days > 0):
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return days


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("lessons", help="Lessons: list / sample / review / deprecate")
    sub = p.add_subparsers(dest="lessons_command", required=True)
    lp = sub.add_parser("list", help="Every lesson with its origin and state")
    lp.add_argument("--held", action="store_true", help="Only lessons held for review")
    lp.set_defaults(func=_run(_list))
    sp = sub.add_parser("sample", help="Recent fire-once lesson fires, to rate relevance")
    sp.add_argument("--n", type=_positive, default=20)
    sp.add_argument("--days", type=_window, default=14.0)
    sp.set_defaults(func=_run(_sample))
    rp = sub.add_parser("review", help="Approve a lesson (revives a held one)")
    rp.add_argument("id")
    rp.add_argument(
        "--force", action="store_true", help="Revive despite a live twin or no embedding"
    )
    rp.add_argument(
        "--rebuild-cache", action="store_true", help="Rebuild the cache from a relocated database"
    )
    rp.set_defaults(func=_run(_review))
    dp = sub.add_parser("deprecate", help="Take a lesson out of service (rejects a held one)")
    dp.add_argument("id")
    dp.add_argument("--reason", required=True)
    dp.add_argument(
        "--rebuild-cache", action="store_true", help="Rebuild the cache from a relocated database"
    )
    dp.set_defaults(func=_run(_deprecate))
