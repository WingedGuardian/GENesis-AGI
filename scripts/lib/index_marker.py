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

import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

FULL_INTERVAL_S = 7 * 24 * 3600
FULL_BACKOFF_S = 24 * 3600
LOCK_WAIT_S = 0.5
OUTCOME_LOCK_WAIT_S = 5.0
SCHEMA_VERSION = 3
# `automatic-ready` reports an incomplete protocol with a dedicated status that
# neither Python (1 on an uncaught exception) nor bash (1/2/126/127 on its own
# failures) produces by accident, so the runner never mistakes a crash for the
# expected pause.
AUTOMATIC_PROTOCOL_INCOMPLETE = 10
# A busy queue is transient contention. The CLI reports it as 75 and the runner
# logs it and exits 0, like an overlapping tick, so the unit never reads failed.
BUSY_STATUS = 75


def _load_sibling(name, spec):
    module = sys.modules.get(name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


_legacy = _load_sibling(
    "_genesis_index_marker_legacy",
    importlib.util.spec_from_file_location(
        "_genesis_index_marker_legacy", Path(__file__).with_name("index_marker_legacy.py")
    ),
)
_legs = _load_sibling(
    "_genesis_index_marker_legs",
    importlib.util.spec_from_file_location(
        "_genesis_index_marker_legs", Path(__file__).with_name("index_marker_legs.py")
    ),
)
_cli = _load_sibling(
    "_genesis_index_marker_cli",
    importlib.util.spec_from_file_location(
        "_genesis_index_marker_cli", Path(__file__).with_name("index_marker_cli.py")
    ),
)
MAX_ATTEMPTS = _legs.MAX_ATTEMPTS
VALID_TOOLS = (*_legs.PROVIDERS, "both")
VALID_MODES = _legs.MODES
VALID_OUTCOMES = _legs.OUTCOMES

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
        previous = db.execute("PRAGMA user_version").fetchone()[0]
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        if previous < 2:
            _legacy.upgrade_receipts(db)
        if previous < 3:
            _legs.upgrade(db)
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


class QueueConversionDeferred(RuntimeError):
    """An ordinary queue operation met an older queue, or creation during an update.

    Only the explicit strict cutover converts an existing queue. Converting on an
    ordinary connect is unsafe across code rollbacks: an update that fails after
    bootstrap restores the older code but not the queue, and the older code then
    refuses the newer schema. Not a ``TimeoutError``, so no busy-spool path turns
    it into a newer-format artifact the older code cannot import.
    """


def _update_in_progress() -> bool:
    """True while an update runs or left its state file (fail closed on errors).

    ``update.sh`` holds ``locks/update.lock`` exclusively for its whole run. A
    shared probe ignores validation holds (``flock -s``), unlike bootstrap's own
    exclusive probe of the same lock (``_update_lock_held``): creating a queue
    during a validation hold is safe, during an update it is not.

    A state file a failed update left behind keeps deferring creation (requests
    still spool, nothing is lost) until that update is resolved. No staleness
    rule here, unlike ``genesis.env``: this module is stdlib-only, and an
    unresolved update is exactly when a newly created queue is unsafe.
    """
    state = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis"))
    try:
        (state / "update_state.json").lstat()
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    try:
        fd = os.open(state / "locks" / "update.lock", os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _conversion_gate(db: sqlite3.Connection, *, allow_convert: bool) -> int:
    """Return the schema version, refusing any creation or upgrade not allowed now."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION or allow_convert:
        return version
    if version == 0 and db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None:
        if _update_in_progress():
            raise QueueConversionDeferred("index queue creation deferred during an update")
        return version
    raise QueueConversionDeferred(
        f"index queue is schema {version}; conversion happens only via strict cutover"
    )


def _initialize(db: sqlite3.Connection, *, allow_convert: bool = False) -> None:
    """Create/upgrade the database once, serialized across fresh CLI processes."""
    version = _conversion_gate(db, allow_convert=allow_convert)
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
        # Re-check under the lock: another writer may have created the queue
        # at an older schema between the first check and the lock.
        version = _conversion_gate(db, allow_convert=allow_convert)
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


def _connect(wait_s: float = LOCK_WAIT_S, *, allow_convert: bool = False) -> sqlite3.Connection:
    marker_dir().mkdir(parents=True, exist_ok=True)
    path = database_path()
    existed = path.exists()
    # Probe before connecting, which would create an empty file. An update that
    # starts after this probe is caught by the gate instead; the empty file it
    # may leave is never deleted here: a process that already opened it could
    # still initialize that unlinked inode, losing its request. This code's gate
    # and readiness observer treat it as absent; older code after a rollback
    # reports it as an observation error until its next write initializes it.
    if not existed and not allow_convert and _update_in_progress():
        raise QueueConversionDeferred("index queue creation deferred during an update")
    db = sqlite3.connect(path, timeout=wait_s, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={max(1, int(wait_s * 1000))}")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        _initialize(db, allow_convert=allow_convert)
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


def _coalesce_pending(db: sqlite3.Connection, h: str, data: dict, *, legacy=False) -> None:
    shared = [data["attempts"]] if legacy and data["tools"] == "both" else []
    _legs.coalesce(db, h, data, legacy=legacy, shared=shared)


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


def _header_version() -> int | None:
    """``user_version`` from the file header (offset 60), readable while SQLite is locked.

    Ordinary writes never change it; only the explicit cutover does, so a stale
    page left by a crashed cutover is the one state where it can mislead.
    """
    try:
        with database_path().open("rb") as stream:
            header = stream.read(100)
    except OSError:
        return None
    if len(header) != 100 or header[:16] != b"SQLite format 3\0":
        return None
    return int.from_bytes(header[60:64], "big")


def _write_spool(h: str, data: dict, *, legacy_body: bool = False) -> Path:
    # A deferred conversion spools the plain request body that schema-2 code
    # writes and imports, so an older checkout still consumes it after a rollback.
    nonce = secrets.token_hex(16)
    return _write_durable_event(
        final_name=f".spool-{h}-{nonce}.spool",
        temp_prefix=f".spool-{h}-",
        data=data if legacy_body else {"version": 2, "kind": "enqueue", "request": data},
    )


def _write_outcome_spool(context: dict, event: dict) -> Path:
    h, claim_id = context["hash"], context["claim_id"]
    nonce = secrets.token_hex(16)
    return _write_durable_event(
        final_name=f".outcome-spool-{h}-{claim_id}-{nonce}.spool",
        temp_prefix=f".tmp-outcome-spool-{h}-{claim_id}-",
        data=event,
    )


def _legacy_files() -> list[Path]:
    return _legacy.legacy_files(marker_dir())


def _record_outcome_db(db, event, raw):
    # Only the version-dispatched importer and validated direct writer call this.
    if event["version"] == 1:
        return _legs.legacy_outcome(
            db, event["hash"], event["claim_id"], event["action"], event["recorded_at"], raw
        )
    try:
        return _legs.record_event(db, event, raw)
    except _legs.UnownedExecution:
        return "unowned"


def _parse_scalar_event(data: dict) -> dict:
    expected = _legs.CONTEXT | {"version", "kind", "action", "recorded_at"}
    if (
        not isinstance(data, dict)
        or set(data) != expected
        or type(data["version"]) is not int
        or data["version"] != 2
        or data["kind"] != "outcome"
    ):
        raise ValueError("invalid scalar outcome envelope")
    context = {key: data[key] for key in _legs.CONTEXT}
    return _legs.scalar_event(context, data["action"], data["recorded_at"], marker_hash)


def _import_pending(db, h, data, *, legacy=True):
    try:
        _coalesce_pending(db, h, data, legacy=legacy)
    except _legs.IdentityConflict as exc:
        raise _legacy.LegacyArtifactInvalid("repository-identity-conflict") from exc


def _import_failed(db, h, data, raw, artifact_identity):
    try:
        _legs.legacy_failed(db, h, data, raw=raw, artifact_identity=artifact_identity)
    except _legs.IdentityConflict as exc:
        raise _legacy.LegacyArtifactInvalid("repository-identity-conflict") from exc


def _import_claim(db, h, data, raw, artifact_identity):
    try:
        _legs.legacy_claim(db, h, data, raw=raw, artifact_identity=artifact_identity)
    except _legs.IdentityConflict as exc:
        raise _legacy.LegacyArtifactInvalid("repository-identity-conflict") from exc


def _import_one(db, path, raw, artifact_identity=None):
    operations = _legacy.QueueOperations(
        normalize=_normalize_queue_data,
        hash_repo=marker_hash,
        coalesce=_import_pending,
        record_outcome=_record_outcome_db,
        valid_number=_valid_number,
        outcomes=VALID_OUTCOMES,
        parse_event=_parse_scalar_event,
        record_failed=_import_failed,
        record_claim=_import_claim,
    )
    _legacy.import_one(db, path, raw, operations, artifact_identity=artifact_identity)


def _migrate_legacy() -> None:
    _legacy.migrate(marker_dir(), _write_db, _import_one, _fsync_dir, wait_s=LOCK_WAIT_S)


def _prepare() -> None:
    db = _connect()
    db.close()
    _migrate_legacy()


def convert_queue() -> None:
    """Explicitly convert an older queue, then import retained legacy artifacts.

    No ordinary operation converts (see ``QueueConversionDeferred``); the strict
    cutover is this function's only intended runtime caller.
    """
    db = _connect(allow_convert=True)
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
    # A busy spool keeps the current envelope only on a queue known to be at the
    # current schema; otherwise it may be one older code reads after a rollback,
    # which imports only the plain format.
    current = False

    def busy_spool() -> Path:
        return _write_spool(
            h, data, legacy_body=not (current or _header_version() == SCHEMA_VERSION)
        )

    try:
        db = _connect()
        db.close()
        current = True
        _migrate_legacy()
        with _write_db() as db:
            _coalesce_pending(db, h, data)
        return database_path()
    except QueueConversionDeferred:
        return _write_spool(h, data, legacy_body=True)
    except TimeoutError:
        return busy_spool()
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        return busy_spool()


def list_markers() -> list[dict]:
    _prepare()
    db = _connect()
    try:
        now = time.time()
        return [
            {
                **dict(row),
                "tools": row["provider"],
                "legacy_shared_attempts": _legs.shared_debts(row["legacy_shared_attempts"]),
                "age_s": int(min(_legs.INTEGER_MAX, max(0, now - row["requested_at"]))),
            }
            for row in db.execute("SELECT * FROM pending ORDER BY hash,provider")
        ]
    finally:
        db.close()


def claim(h: str, *, provider: str) -> dict | None:
    _prepare()
    with _write_db() as db:
        result = _legs.claim(db, h, provider)
        if result is not None:
            _legs.validate_context(result["context"], marker_hash)
        return result


def consume(context: dict) -> None:
    remember_outcome(context, "consume")
    apply_remembered_outcome(context)


def restore(context: dict, *, attempts_inc=False) -> str:
    remember_outcome(context, "restore_failure" if attempts_inc else "restore")
    return apply_remembered_outcome(context)


def remember_outcome(context: dict, action: str) -> Path:
    event = _legs.scalar_event(context, action, time.time(), marker_hash)
    context = {key: event[key] for key in _legs.CONTEXT}
    raw = json.dumps(event, sort_keys=True).encode() + b"\n"
    try:
        _prepare()
        with _write_db(wait_s=OUTCOME_LOCK_WAIT_S) as db:
            state = _record_outcome_db(db, event, raw)
        if state == "unowned":
            raise RuntimeError("cannot remember outcome for an unowned execution")
        if state == "conflict":
            raise RuntimeError("conflicting durable queue outcomes")
        return database_path()
    except TimeoutError:
        return _write_outcome_spool(context, event)
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        return _write_outcome_spool(context, event)


def apply_remembered_outcome(context: dict) -> str | None:
    context = _legs.validate_context(context, marker_hash)
    _prepare()
    with _write_db() as db:
        return _legs.apply(db, context)


def repend_stale_inflight() -> list[str]:
    _prepare()
    with _write_db() as db:
        return _legs.reconcile(db)


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
            "repo_state": "SELECT * FROM repo_state WHERE hash=?",
        }
        row = db.execute(queries[table], (h,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def get_inflight(h: str) -> dict | None:
    return _get_row("inflight", h)


def get_failed(h: str, *, provider=None) -> dict | None:
    _prepare()
    db = _connect()
    try:
        rows = list(db.execute("SELECT * FROM failed WHERE hash=?", (h,)))
        if provider is not None:
            if provider not in _legs.PROVIDERS:
                raise ValueError("provider must be cbm or gitnexus")
            rows = [row for row in rows if row["provider"] == provider]
        if len(rows) > 1:
            raise ValueError("provider required for multiple failed legs")
        return dict(rows[0]) if rows else None
    finally:
        db.close()


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


def _cli_context(args):
    context = _legacy._decode(args.context.encode("utf-8"), strict=True)
    _legs.validate_context(context, marker_hash)
    return context


def _machine_hash(value: str) -> str:
    if len(value) != 16 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("invalid machine repository hash")
    return value


def _pending_record(row: dict) -> tuple[str, ...]:
    h = _machine_hash(row["hash"])
    repo, provider, age = row["repo_path"], row["provider"], row["age_s"]
    if (
        not _legs.stored_repo(repo)
        or _legs.stored_hash(repo) != h
        or provider not in _legs.PROVIDERS
        or type(age) is not int
        or not 0 <= age <= _legs.INTEGER_MAX
    ):
        raise ValueError("invalid machine pending snapshot")
    return "1", "pending", h, repo, provider, str(age)


def _claim_record(row: dict, h: str, provider: str) -> tuple[str, ...]:
    context = _legs.validate_context(row["context"], marker_hash)
    if (
        context["hash"] != h
        or context["provider"] != provider
        or any(
            row.get(key) != context[key] for key in ("hash", "repo_path", "provider", "claim_id")
        )
        or row.get("tools") != provider
        or row.get("mode") not in VALID_MODES
    ):
        raise ValueError("invalid machine claim snapshot")
    return (
        "1",
        "claim",
        context["hash"],
        context["repo_path"],
        context["provider"],
        row["mode"],
        json.dumps(context, ensure_ascii=True, separators=(",", ":")),
    )


def _write_machine(records) -> None:
    # Render/validate the complete population before emitting any byte. The
    # consumer checks both actual process status and complete NUL framing.
    encoded = []
    for record in records:
        for field in record:
            if not isinstance(field, str) or "\0" in field:
                raise ValueError("invalid machine field")
            encoded.append(field.encode("utf-8") + b"\0")
    sys.stdout.buffer.write(b"".join(encoded))
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    return _cli.main(argv, globals())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except QueueConversionDeferred as exc:
        print(f"index_marker: {exc}", file=sys.stderr)
        sys.exit(2)
    except TimeoutError as exc:
        # A busy queue is transient: BUSY_STATUS lets the runner defer the tick.
        print(f"index_marker: {exc}", file=sys.stderr)
        sys.exit(BUSY_STATUS)
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            import traceback

            traceback.print_exc()
            sys.exit(2)
        print(f"index_marker: queue busy: {exc}", file=sys.stderr)
        sys.exit(BUSY_STATUS)
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
