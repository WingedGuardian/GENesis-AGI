#!/usr/bin/env python3
"""Transactional code-intel index-request queue.

Triggers enqueue work here; ``code_intel_runner.sh`` is the only consumer that
invokes the capped index entrypoint. Queue ownership, remembered outcomes, the
failure budget, and full-index timing live in one SQLite database so a process,
container, or host failure cannot expose a half-finished multi-file transition.

The module is stdlib-only because ``disk_reclaim.py`` imports it when the venv
may be unavailable. SQLite uses rollback journaling and ``synchronous=FULL``.
Do not switch to WAL until every supported SQLite contains the WAL-reset fix
(3.51.3 or an official backport); Ubuntu's current Python runtime is older.

Legacy JSON markers are renamed into recoverable claims before import, then
imported idempotently under the old queue lock. Busy enqueues fall back to
unique, fsynced spool files that the same importer coalesces into SQLite.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

MAX_ATTEMPTS = 5
FULL_INTERVAL_S = 7 * 24 * 3600
FULL_BACKOFF_S = 24 * 3600
VALID_TOOLS = ("cbm", "gitnexus", "both")
VALID_MODES = ("fast", "moderate", "full")
VALID_OUTCOMES = ("consume", "consume_full", "restore", "restore_backoff", "restore_failure")
LOCK_WAIT_S = 0.5
OUTCOME_LOCK_WAIT_S = 5.0
SCHEMA_VERSION = 2
# `automatic-ready` reports an incomplete protocol with a dedicated status that
# neither Python (1 on an uncaught exception) nor bash (1/2/126/127 on its own
# failures) produces by accident, so the runner never mistakes a crash for the
# expected pause.
AUTOMATIC_PROTOCOL_INCOMPLETE = 10
_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_CLAIM_RE = re.compile(r"^[0-9a-f]{32}$")


def _load_legacy():
    name = "_genesis_index_marker_legacy"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("index_marker_legacy.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


_legacy = _load_legacy()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
 hash TEXT PRIMARY KEY, repo_path TEXT NOT NULL,
 tools TEXT NOT NULL CHECK(tools IN ('cbm','gitnexus','both')),
 mode TEXT NOT NULL CHECK(mode IN ('fast','moderate','full')),
 requested_at REAL NOT NULL, attempts INTEGER NOT NULL CHECK(attempts >= 0));
CREATE TABLE IF NOT EXISTS inflight (
 hash TEXT PRIMARY KEY, repo_path TEXT NOT NULL,
 tools TEXT NOT NULL CHECK(tools IN ('cbm','gitnexus','both')),
 mode TEXT NOT NULL CHECK(mode IN ('fast','moderate','full')),
 requested_at REAL NOT NULL, attempts INTEGER NOT NULL CHECK(attempts >= 0),
 claim_id TEXT NOT NULL,
 outcome_action TEXT CHECK(outcome_action IN
 ('consume','consume_full','restore','restore_backoff','restore_failure')),
 outcome_recorded_at REAL,
 CHECK((outcome_action IS NULL) = (outcome_recorded_at IS NULL)));
CREATE TABLE IF NOT EXISTS failed (
 hash TEXT PRIMARY KEY, repo_path TEXT, tools TEXT, mode TEXT,
 requested_at REAL, attempts INTEGER, reason TEXT NOT NULL,
 raw_payload BLOB, failed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS failed_outcomes (
 hash TEXT PRIMARY KEY, reason TEXT NOT NULL, raw_payload BLOB,
 failed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS repo_state (
 hash TEXT PRIMARY KEY, last_full REAL, full_backoff REAL);
CREATE TABLE IF NOT EXISTS queue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def marker_dir() -> Path:
    base = os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")
    return Path(base) / "index-requests"


def database_path() -> Path:
    return marker_dir() / "queue.sqlite3"


# Legacy path helpers remain for migration and external compatibility. New state
# is only written to SQLite.
def outcome_path(h: str) -> Path:
    return marker_dir() / f".outcome-{h}"


def failed_outcome_path(h: str) -> Path:
    return marker_dir() / f".failed-outcome-{h}"


def last_full_path(h: str) -> Path:
    return marker_dir() / f".last-full-{h}"


def full_backoff_path(h: str) -> Path:
    return marker_dir() / f".full-backoff-{h}"


def canonical_repo(repo_path: str) -> str:
    return os.path.realpath(repo_path)


def marker_hash(repo_path: str) -> str:
    # Compatibility, not security: must byte-match sha1sum in the entrypoint.
    return hashlib.sha1(canonical_repo(repo_path).encode()).hexdigest()[:16]  # noqa: S324


def _union_tools(a: str, b: str) -> str:
    return a if a == b else "both"


def _highest_mode(a: str, b: str) -> str:
    order = {"fast": 0, "moderate": 1, "full": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _valid_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _normalize_queue_data(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    attempts = data.get("attempts", 0)
    requested_at = data.get("requested_at")
    if (
        not isinstance(data.get("repo_path"), str)
        or not data["repo_path"]
        or data.get("tools") not in VALID_TOOLS
        or data.get("mode") not in VALID_MODES
        or not _valid_number(requested_at)
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        # Generated state never legitimately exceeds the retry budget. Keeping
        # the accepted range bounded also guarantees attempts+1 remains safe to
        # bind when a migrated inflight row is reconciled.
        or not 0 <= attempts <= MAX_ATTEMPTS
    ):
        return None
    normalized = dict(data)
    normalized["requested_at"] = float(requested_at)
    normalized["attempts"] = attempts
    return normalized


def _valid_queue_data(data: object) -> bool:
    return _normalize_queue_data(data) is not None


# SQLITE_BUSY=5 / SQLITE_LOCKED=6 — spelled out because the sqlite3 module
# only grew named constants (and exceptions grew sqlite_errorcode) in 3.11;
# the runner invokes this with the SYSTEM python, which can be older.
_BUSY_ERRCODES = {
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
}


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code in _BUSY_ERRCODES
    # Python < 3.11: no sqlite_errorcode — fall back to the driver message
    # ("database is locked", "database table is locked", "database is busy").
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _publish_schema(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        _legacy.upgrade_receipts(db)
        db.execute(
            "INSERT INTO queue_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _initialize(db: sqlite3.Connection) -> None:
    """Create/upgrade the database once, serialized across fresh CLI processes."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"index queue schema {version} is newer than supported {SCHEMA_VERSION}")
    lock_path = marker_dir() / ".queue-db-init.lock"
    with lock_path.open("a") as lock:
        deadline = time.monotonic() + OUTCOME_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("index-request database initialization busy") from exc
                time.sleep(min(0.01, remaining))
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"index queue schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise RuntimeError(f"index queue refused rollback journal mode: {mode}")
        _publish_schema(db)


def _connect(wait_s: float = LOCK_WAIT_S) -> sqlite3.Connection:
    marker_dir().mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database_path(), timeout=wait_s, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={max(1, int(wait_s * 1000))}")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        _initialize(db)
        return db
    except BaseException:
        db.close()
        raise


@contextmanager
def _write_db(*, wait_s: float = LOCK_WAIT_S):
    db = _connect(wait_s)
    try:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise TimeoutError("index-request queue busy; request not changed") from exc
            raise
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _coalesce_pending(db: sqlite3.Connection, h: str, data: dict) -> None:
    row = db.execute("SELECT * FROM pending WHERE hash=?", (h,)).fetchone()
    if row:
        data = {
            "repo_path": row["repo_path"],
            "tools": _union_tools(row["tools"], data["tools"]),
            "mode": _highest_mode(row["mode"], data["mode"]),
            "requested_at": min(row["requested_at"], data["requested_at"]),
            "attempts": max(row["attempts"], data.get("attempts", 0)),
        }
    db.execute(
        "INSERT INTO pending(hash,repo_path,tools,mode,requested_at,attempts) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(hash) DO UPDATE SET repo_path=excluded.repo_path,tools=excluded.tools,"
        "mode=excluded.mode,requested_at=excluded.requested_at,attempts=excluded.attempts",
        (
            h,
            data["repo_path"],
            data["tools"],
            data["mode"],
            data["requested_at"],
            data.get("attempts", 0),
        ),
    )


def _fsync_dir(path: Path) -> None:
    """Persist a directory-entry change before reporting it durable."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable_event(*, final_name: str, temp_prefix: str, data: dict) -> Path:
    """Write one immutable JSON event with file and directory durability."""
    directory = marker_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / final_name
    fd, tmp_name = tempfile.mkstemp(prefix=temp_prefix, suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(data, sort_keys=True).encode() + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        _fsync_dir(directory)
        return final
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def _write_spool(h: str, data: dict) -> Path:
    """Durably spool one immutable enqueue when SQLite is temporarily busy."""
    nonce = secrets.token_hex(16)
    return _write_durable_event(
        final_name=f".spool-{h}-{nonce}.spool",
        temp_prefix=f".spool-{h}-",
        data=data,
    )


def _write_outcome_spool(h: str, claim_id: str, action: str) -> Path:
    """Durably spool a terminal action bound to one exact inflight claim."""
    if not _HASH_RE.fullmatch(h) or not _CLAIM_RE.fullmatch(claim_id):
        raise ValueError("outcome spool requires canonical hash and claim id")
    nonce = secrets.token_hex(16)
    return _write_durable_event(
        final_name=f".outcome-spool-{h}-{claim_id}-{nonce}.spool",
        temp_prefix=f".tmp-outcome-spool-{h}-{claim_id}-",
        data={
            "version": 1,
            "claim_id": claim_id,
            "action": action,
            "recorded_at": time.time(),
        },
    )


def _legacy_files() -> list[Path]:
    return _legacy.legacy_files(marker_dir())


def _record_failed_outcome(db: sqlite3.Connection, h: str, reason: str, raw: bytes) -> None:
    db.execute(
        "INSERT INTO failed_outcomes(hash,reason,raw_payload,failed_at) VALUES(?,?,?,?) "
        "ON CONFLICT(hash) DO UPDATE SET reason=excluded.reason,"
        "raw_payload=excluded.raw_payload,failed_at=excluded.failed_at",
        (h, reason, raw, time.time()),
    )


def _record_outcome_db(
    db: sqlite3.Connection,
    h: str,
    claim_id: str,
    action: str,
    recorded_at: float,
    raw: bytes,
) -> str:
    """Record one claim-bound outcome without permitting last-writer-wins."""
    row = db.execute(
        "SELECT outcome_action FROM inflight WHERE hash=? AND claim_id=?",
        (h, claim_id),
    ).fetchone()
    if not row:
        return "unowned"
    conflict_reason = f"conflicting durable outcomes for claim {claim_id}"
    prior_failure = db.execute("SELECT reason FROM failed_outcomes WHERE hash=?", (h,)).fetchone()
    if prior_failure and prior_failure["reason"] == conflict_reason:
        return "conflict"
    if row["outcome_action"] is None:
        db.execute(
            "UPDATE inflight SET outcome_action=?,outcome_recorded_at=? "
            "WHERE hash=? AND claim_id=?",
            (action, recorded_at, h, claim_id),
        )
        return "recorded"
    if row["outcome_action"] == action:
        return "idempotent"
    db.execute(
        "UPDATE inflight SET outcome_action=NULL,outcome_recorded_at=NULL "
        "WHERE hash=? AND claim_id=?",
        (h, claim_id),
    )
    _record_failed_outcome(db, h, conflict_reason, raw)
    return "conflict"


def _import_one(db: sqlite3.Connection, path: Path, raw: bytes) -> None:
    operations = _legacy.QueueOperations(
        normalize=_normalize_queue_data,
        hash_repo=marker_hash,
        coalesce=_coalesce_pending,
        record_outcome=_record_outcome_db,
        valid_number=_valid_number,
        outcomes=VALID_OUTCOMES,
    )
    _legacy.import_one(db, path, raw, operations)


def _migrate_legacy() -> None:
    _legacy.migrate(marker_dir(), _write_db, _import_one, _fsync_dir, wait_s=LOCK_WAIT_S)


def _prepare() -> None:
    db = _connect()
    db.close()
    _migrate_legacy()


def write_marker(repo_path: str, tools: str, mode: str) -> Path:
    """Create/coalesce a request, durably spooling it if SQLite is busy."""
    if tools not in VALID_TOOLS:
        raise ValueError(f"tools must be one of {VALID_TOOLS}, got {tools!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    data = {
        "repo_path": canonical_repo(repo_path),
        "tools": tools,
        "mode": mode,
        "requested_at": time.time(),
        "attempts": 0,
    }
    h = marker_hash(repo_path)
    try:
        _prepare()
        with _write_db() as db:
            _coalesce_pending(db, h, data)
        return database_path()
    except TimeoutError:
        return _write_spool(h, data)
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        return _write_spool(h, data)


def list_markers() -> list[dict]:
    _prepare()
    db = _connect()
    try:
        now = time.time()
        return [
            {**dict(row), "age_s": max(0, int(now - row["requested_at"]))}
            for row in db.execute("SELECT * FROM pending ORDER BY hash")
        ]
    finally:
        db.close()


def claim(h: str) -> dict | None:
    _prepare()
    with _write_db() as db:
        if db.execute("SELECT 1 FROM inflight WHERE hash=?", (h,)).fetchone():
            return None
        row = db.execute("SELECT * FROM pending WHERE hash=?", (h,)).fetchone()
        if not row:
            return None
        claim_id = secrets.token_hex(16)
        db.execute(
            "INSERT INTO inflight(hash,repo_path,tools,mode,requested_at,attempts,claim_id) VALUES(?,?,?,?,?,?,?)",
            (
                h,
                row["repo_path"],
                row["tools"],
                row["mode"],
                row["requested_at"],
                row["attempts"],
                claim_id,
            ),
        )
        db.execute("DELETE FROM pending WHERE hash=?", (h,))
        return {**dict(row), "claim_id": claim_id}


def consume(h: str, *, claim_id: str) -> None:
    if not claim_id:
        raise ValueError("claim id is required")
    _prepare()
    with _write_db() as db:
        if not db.execute(
            "DELETE FROM inflight WHERE hash=? AND claim_id=?", (h, claim_id)
        ).rowcount:
            raise RuntimeError(f"cannot consume unowned claim {h}")


def _restore_db(db: sqlite3.Connection, h: str, *, attempts_inc: bool = False) -> str:
    row = db.execute("SELECT * FROM inflight WHERE hash=?", (h,)).fetchone()
    if not row:
        return "pending"
    pending = db.execute("SELECT * FROM pending WHERE hash=?", (h,)).fetchone()
    attempts = row["attempts"] + int(attempts_inc)
    if attempts >= MAX_ATTEMPTS:
        # The inflight row is one claimed generation. A pending row with the
        # same hash is newer work that arrived while that generation ran; never
        # merge it into, or delete it with, the exhausted generation.
        db.execute(
            "INSERT INTO failed(hash,repo_path,tools,mode,requested_at,attempts,reason,failed_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(hash) DO UPDATE SET repo_path=excluded.repo_path,tools=excluded.tools,mode=excluded.mode,"
            "requested_at=excluded.requested_at,attempts=excluded.attempts,reason=excluded.reason,failed_at=excluded.failed_at",
            (
                h,
                row["repo_path"],
                row["tools"],
                row["mode"],
                row["requested_at"],
                attempts,
                "attempt budget exhausted",
                time.time(),
            ),
        )
        db.execute("DELETE FROM inflight WHERE hash=?", (h,))
        return "pending" if pending else "failed"
    merged = {
        "repo_path": row["repo_path"],
        "tools": _union_tools(row["tools"], pending["tools"]) if pending else row["tools"],
        "mode": _highest_mode(row["mode"], pending["mode"]) if pending else row["mode"],
        "requested_at": min(row["requested_at"], pending["requested_at"])
        if pending
        else row["requested_at"],
        "attempts": max(attempts, pending["attempts"]) if pending else attempts,
    }
    _coalesce_pending(db, h, merged)
    db.execute("DELETE FROM inflight WHERE hash=?", (h,))
    return "pending"


def restore(h: str, *, claim_id: str, attempts_inc: bool = False) -> str:
    if not claim_id:
        raise ValueError("claim id is required")
    _prepare()
    with _write_db() as db:
        if not db.execute(
            "SELECT 1 FROM inflight WHERE hash=? AND claim_id=?", (h, claim_id)
        ).fetchone():
            raise RuntimeError(f"cannot restore unowned claim {h}")
        return _restore_db(db, h, attempts_inc=attempts_inc)


def remember_outcome(h: str, action: str, *, claim_id: str) -> Path:
    if action not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {VALID_OUTCOMES}, got {action!r}")
    if not _CLAIM_RE.fullmatch(claim_id):
        raise ValueError("claim id must be 32 lowercase hexadecimal characters")
    try:
        _prepare()
        recorded_at = time.time()
        raw = json.dumps(
            {
                "version": 1,
                "claim_id": claim_id,
                "action": action,
                "recorded_at": recorded_at,
            },
            sort_keys=True,
        ).encode()
        with _write_db(wait_s=OUTCOME_LOCK_WAIT_S) as db:
            state = _record_outcome_db(db, h, claim_id, action, recorded_at, raw)
        if state == "unowned":
            raise RuntimeError(f"cannot remember outcome for unowned claim {h}")
        if state == "conflict":
            raise RuntimeError(f"conflicting outcome for claim {h}/{claim_id}")
        return database_path()
    except TimeoutError:
        return _write_outcome_spool(h, claim_id, action)
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        return _write_outcome_spool(h, claim_id, action)


def _apply_outcome_db(db: sqlite3.Connection, h: str) -> str | None:
    row = db.execute("SELECT * FROM inflight WHERE hash=?", (h,)).fetchone()
    if not row or row["outcome_action"] is None:
        return None
    action, recorded = row["outcome_action"], row["outcome_recorded_at"]
    if action == "consume_full":
        db.execute(
            "INSERT INTO repo_state(hash,last_full,full_backoff) VALUES(?,?,NULL) "
            "ON CONFLICT(hash) DO UPDATE SET last_full=excluded.last_full,full_backoff=NULL",
            (h, recorded),
        )
        db.execute("DELETE FROM inflight WHERE hash=?", (h,))
        return "consumed"
    if action == "consume":
        db.execute("DELETE FROM inflight WHERE hash=?", (h,))
        return "consumed"
    if action == "restore_backoff":
        db.execute(
            "INSERT INTO repo_state(hash,full_backoff) VALUES(?,?) "
            "ON CONFLICT(hash) DO UPDATE SET full_backoff=excluded.full_backoff",
            (h, recorded),
        )
    return _restore_db(db, h, attempts_inc=action == "restore_failure")


def apply_remembered_outcome(h: str) -> str | None:
    _prepare()
    with _write_db() as db:
        return _apply_outcome_db(db, h)


def repend_stale_inflight() -> list[str]:
    _prepare()
    with _write_db() as db:
        hashes = [row[0] for row in db.execute("SELECT hash FROM inflight ORDER BY hash")]
        repended = []
        for h in hashes:
            state = _apply_outcome_db(db, h)
            if state is None:
                state = _restore_db(db, h, attempts_inc=True)
            if state == "pending":
                repended.append(h)
        return repended


def should_escalate_full(h: str) -> bool:
    _prepare()
    db = _connect()
    try:
        row = db.execute(
            "SELECT last_full,full_backoff FROM repo_state WHERE hash=?", (h,)
        ).fetchone()
    finally:
        db.close()
    now = time.time()
    last = row["last_full"] if row else None
    backoff = row["full_backoff"] if row else None
    if last is not None and now - last < FULL_INTERVAL_S:
        return False
    return backoff is None or now - backoff >= FULL_BACKOFF_S


def stamp_full(h: str, *, timestamp: float | None = None) -> None:
    if timestamp is not None and not _valid_number(timestamp):
        raise ValueError("full timestamp must be a finite number")
    _prepare()
    with _write_db() as db:
        db.execute(
            "INSERT INTO repo_state(hash,last_full,full_backoff) VALUES(?,?,NULL) "
            "ON CONFLICT(hash) DO UPDATE SET last_full=excluded.last_full,full_backoff=NULL",
            (h, time.time() if timestamp is None else timestamp),
        )


def mark_full_backoff(h: str, *, timestamp: float | None = None) -> None:
    if timestamp is not None and not _valid_number(timestamp):
        raise ValueError("full-backoff timestamp must be a finite number")
    _prepare()
    with _write_db() as db:
        db.execute(
            "INSERT INTO repo_state(hash,full_backoff) VALUES(?,?) "
            "ON CONFLICT(hash) DO UPDATE SET full_backoff=excluded.full_backoff",
            (h, time.time() if timestamp is None else timestamp),
        )


def _get_row(table: str, h: str) -> dict | None:
    _prepare()
    db = _connect()
    try:
        queries = {
            "inflight": "SELECT * FROM inflight WHERE hash=?",
            "failed": "SELECT * FROM failed WHERE hash=?",
            "repo_state": "SELECT * FROM repo_state WHERE hash=?",
        }
        row = db.execute(queries[table], (h,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def get_inflight(h: str) -> dict | None:
    return _get_row("inflight", h)


def get_failed(h: str) -> dict | None:
    return _get_row("failed", h)


def list_failed() -> list[dict]:
    _prepare()
    db = _connect()
    try:
        return [dict(row) for row in db.execute("SELECT * FROM failed ORDER BY hash")]
    finally:
        db.close()


def get_repo_state(h: str) -> dict:
    row = _get_row("repo_state", h)
    return row or {"hash": h, "last_full": None, "full_backoff": None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Code-intel index-request queue")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write")
    p.add_argument("--repo", required=True)
    p.add_argument("--tools", default="both", choices=VALID_TOOLS)
    p.add_argument("--mode", default="fast", choices=VALID_MODES)
    p = sub.add_parser("hash")
    p.add_argument("--repo", required=True)
    sub.add_parser("list")
    sub.add_parser("automatic-ready")
    p = sub.add_parser("import-status")
    p.add_argument("--limit", type=int, default=20)
    for name in (
        "claim",
        "apply-outcome",
        "should-escalate",
        "stamp-full",
        "mark-full-backoff",
    ):
        p = sub.add_parser(name)
        p.add_argument("--hash", required=True)
    p = sub.add_parser("consume")
    p.add_argument("--hash", required=True)
    p.add_argument("--claim-id", required=True)
    p = sub.add_parser("remember-outcome")
    p.add_argument("--hash", required=True)
    p.add_argument("--action", required=True, choices=VALID_OUTCOMES)
    p.add_argument("--claim-id", required=True)
    sub.add_parser("reconcile-inflight")
    p = sub.add_parser("restore")
    p.add_argument("--hash", required=True)
    p.add_argument("--claim-id", required=True)
    p.add_argument("--attempts-inc", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd in ("automatic-ready", "import-status"):
        try:
            result = _legacy.import_status(
                database_path(),
                limit=getattr(args, "limit", 0),
                protocol_only=args.cmd == "automatic-ready",
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            print(
                json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=True),
                file=sys.stderr if args.cmd == "automatic-ready" else sys.stdout,
            )
            return 2
        print(json.dumps(result, ensure_ascii=True))
        return AUTOMATIC_PROTOCOL_INCOMPLETE if args.cmd == "automatic-ready" else 0
    if args.cmd == "write":
        print(write_marker(args.repo, args.tools, args.mode))
        return 0
    if args.cmd == "hash":
        print(marker_hash(args.repo))
        return 0
    if args.cmd == "list":
        for m in list_markers():
            print(
                f"{m['hash']}\t{m['repo_path']}\t{m['tools']}\t{m['mode']}\t{m['attempts']}\t{m['age_s']}"
            )
        return 0
    if args.cmd == "claim":
        data = claim(args.hash)
        if data is None:
            return 1
        print(
            f"{data['repo_path']}\t{data['tools']}\t{data['mode']}\t"
            f"{data['attempts']}\t{data['claim_id']}"
        )
        return 0
    if args.cmd == "consume":
        consume(args.hash, claim_id=args.claim_id)
        return 0
    if args.cmd == "remember-outcome":
        remember_outcome(args.hash, args.action, claim_id=args.claim_id)
        return 0
    if args.cmd == "apply-outcome":
        state = apply_remembered_outcome(args.hash)
        if state is None:
            return 1
        print(state)
        return 0
    if args.cmd == "restore":
        print(restore(args.hash, claim_id=args.claim_id, attempts_inc=args.attempts_inc))
        return 0
    if args.cmd == "should-escalate":
        try:
            due = should_escalate_full(args.hash)
        except Exception as exc:  # noqa: BLE001 — CLI must reserve rc=1 for "not due"
            print(f"ERROR: should-escalate failed: {exc}", file=sys.stderr)
            return 2
        return 0 if due else 1
    if args.cmd == "stamp-full":
        stamp_full(args.hash)
        return 0
    if args.cmd == "mark-full-backoff":
        mark_full_backoff(args.hash)
        return 0
    if args.cmd == "reconcile-inflight":
        repended = repend_stale_inflight()
        if repended:
            print(f"re-pended {len(repended)} stale inflight marker(s)")
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
