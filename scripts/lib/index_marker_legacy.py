"""Legacy queue ownership, artifact-local validation and durable import receipts.

Only declared content failures are quarantined. Filesystem, SQLite and arbitrary
programming errors abort visibly before retirement. Queue business operations
are supplied by the facade at call time; this module never caches GENESIS_HOME.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_HASH = re.compile(r"[0-9a-f]{16}")
_CLAIM = re.compile(r"(?P<source>.+)\.migrating-[0-9a-f]{32}", re.DOTALL)
_SPOOL = re.compile(r"\.spool-(?P<hash>[0-9a-f]{16})-[0-9a-f]{32}\.spool")
_OUTCOME = re.compile(
    r"\.outcome-spool-(?P<hash>[0-9a-f]{16})-(?P<claim>[0-9a-f]{32})-"
    r"[0-9a-f]{32}\.spool"
)
_PREFIXES = (".outcome-", ".failed-outcome-", ".last-full-", ".full-backoff-")
_RECEIPTS = """CREATE TABLE legacy_imports (
 source_name_bytes BLOB NOT NULL, content_sha256 TEXT NOT NULL,
 source_mtime_ns TEXT NOT NULL, imported_at REAL NOT NULL,
 disposition TEXT NOT NULL CHECK(disposition IN ('imported','quarantined')),
 reason TEXT NOT NULL, raw_payload BLOB,
 PRIMARY KEY(source_name_bytes,content_sha256,source_mtime_ns))"""


class LegacyArtifactInvalid(ValueError):
    """A stable, ASCII reason for an invalid artifact, never an IO failure."""


@dataclass(frozen=True)
class QueueOperations:
    normalize: Callable
    hash_repo: Callable
    coalesce: Callable
    record_outcome: Callable
    valid_number: Callable
    outcomes: tuple[str, ...]
    parse_event: Callable
    record_failed: Callable
    record_claim: Callable


def upgrade_receipts(db: sqlite3.Connection) -> None:
    """Run inside the facade's explicit schema/version transaction."""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_imports'"
    ).fetchone()
    if exists:
        db.execute("ALTER TABLE legacy_imports RENAME TO legacy_imports_v1")
    db.execute(_RECEIPTS)
    if exists:
        for row in db.execute("SELECT * FROM legacy_imports_v1"):
            name, digest, mtime, imported = (
                row["path"],
                row["content_sha256"],
                row["source_mtime_ns"],
                row["imported_at"],
            )
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(mtime, int)
                or not isinstance(imported, (int, float))
                or not math.isfinite(imported)
            ):
                raise RuntimeError("invalid legacy import metadata; upgrade not published")
            db.execute(
                "INSERT INTO legacy_imports VALUES(?,?,?,?,?,?,NULL)",
                (
                    os.fsencode(name),
                    digest,
                    str(mtime),
                    imported,
                    "imported",
                    "legacy-metadata-only",
                ),
            )
        db.execute("DROP TABLE legacy_imports_v1")


def legacy_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and not path.name.endswith(".tmp")
        and (
            path.name.endswith(".json")
            or _CLAIM.fullmatch(path.name)
            or _SPOOL.fullmatch(path.name)
            or _OUTCOME.fullmatch(path.name)
            or path.name.startswith(_PREFIXES)
        )
    )


def _claim_files(paths: list[Path], directory: Path, sync: Callable) -> list[tuple[Path, Path]]:
    claimed = []
    changed = False
    for path in paths:
        match = _CLAIM.fullmatch(path.name)
        if match or _SPOOL.fullmatch(path.name) or _OUTCOME.fullmatch(path.name):
            claimed.append((path, path.with_name(match["source"]) if match else path))
            continue
        claim = path.with_name(f"{path.name}.migrating-{secrets.token_hex(16)}")
        try:
            os.replace(path, claim)
        except FileNotFoundError:
            continue
        changed = True
        claimed.append((claim, path))
    if changed:
        sync(directory)
    return claimed


def _lock(directory: Path, wait_s: float, *, shared: bool = False):
    lock = (directory / ".queue.lock").open("a")
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(lock.fileno(), operation | fcntl.LOCK_NB)
                return lock
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "legacy index-request queue busy; request not changed"
                    ) from exc
                time.sleep(min(0.01, remaining))
    except BaseException:
        lock.close()
        raise


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise LegacyArtifactInvalid("duplicate-json-field")
    return result


def _decode(raw: bytes, *, strict=False):
    try:
        return json.loads(raw, object_pairs_hook=_unique_fields if strict else None)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise LegacyArtifactInvalid("invalid-json") from exc


def _text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise LegacyArtifactInvalid("invalid-text-encoding") from exc


def _queue_data(raw: bytes, ops: QueueOperations, *, allow_current=False) -> tuple[dict, bool]:
    value = _decode(raw)
    legacy = True
    if isinstance(value, dict) and "repo_path" not in value and "request" in value:
        value = _decode(raw, strict=True)
        if (
            not allow_current
            or set(value) != {"version", "kind", "request"}
            or type(value["version"]) is not int
            or value["version"] != 2
            or value["kind"] != "enqueue"
            or not isinstance(value["request"], dict)
            or set(value["request"]) != {"repo_path", "tools", "mode", "requested_at", "attempts"}
        ):
            raise LegacyArtifactInvalid("invalid-enqueue-envelope")
        value, legacy = value["request"], False
    data = ops.normalize(value)
    if data is None:
        raise LegacyArtifactInvalid("invalid-queue-fields")
    _text(data["repo_path"])
    if "\0" in data["repo_path"]:
        raise LegacyArtifactInvalid("invalid-repo-path")
    return data, legacy


def _import_outcome(db, h: str, raw: bytes, ops: QueueOperations, filename_claim=None):
    data = _decode(raw)
    if isinstance(data, dict) and type(data.get("version")) is int and data["version"] == 2:
        try:
            event = ops.parse_event(_decode(raw, strict=True))
        except (ValueError, UnicodeError, OverflowError) as exc:
            raise LegacyArtifactInvalid("invalid-outcome-fields") from exc
        if event["hash"] != h or (
            filename_claim is not None and filename_claim != event["claim_id"]
        ):
            raise LegacyArtifactInvalid("outcome-context-mismatch")
        state = ops.record_outcome(db, event, raw)
        if state == "unowned":
            raise LegacyArtifactInvalid("unowned-outcome")
        return
    if (
        not _HASH.fullmatch(h)
        or not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("action") not in ops.outcomes
        or not isinstance(data.get("claim_id"), str)
        or not ops.valid_number(data.get("recorded_at"))
        or (filename_claim is not None and filename_claim != data["claim_id"])
    ):
        raise LegacyArtifactInvalid("invalid-outcome-fields")
    _text(data["claim_id"])
    event = dict(
        version=1,
        hash=h,
        claim_id=data["claim_id"],
        action=data["action"],
        recorded_at=float(data["recorded_at"]),
    )
    state = ops.record_outcome(db, event, raw)
    if state == "unowned":
        raise LegacyArtifactInvalid("unowned-outcome")


def import_one(
    db: sqlite3.Connection, path: Path, raw: bytes, ops: QueueOperations, *, artifact_identity=None
) -> None:
    name = path.name
    outcome = _OUTCOME.fullmatch(name)
    if outcome:
        _import_outcome(db, outcome["hash"], raw, ops, outcome["claim"])
        return
    spool = _SPOOL.fullmatch(name)
    if spool:
        data, legacy = _queue_data(raw, ops, allow_current=True)
        try:
            h = ops.hash_repo(data["repo_path"])
        except (UnicodeError, ValueError, OverflowError) as exc:
            raise LegacyArtifactInvalid("invalid-repo-path") from exc
        if h != spool["hash"]:
            raise LegacyArtifactInvalid("enqueue-hash-mismatch")
        ops.coalesce(db, h, data, legacy=legacy)
        return
    for prefix, column in ((".last-full-", "last_full"), (".full-backoff-", "full_backoff")):
        if name.startswith(prefix):
            h = name[len(prefix) :]
            try:
                value = float(raw.decode().strip())
            except (UnicodeError, ValueError, OverflowError) as exc:
                raise LegacyArtifactInvalid("invalid-clock") from exc
            if not _HASH.fullmatch(h) or not math.isfinite(value):
                raise LegacyArtifactInvalid("invalid-clock")
            db.execute("INSERT OR IGNORE INTO repo_state(hash) VALUES(?)", (h,))
            query = {
                "last_full": "UPDATE repo_state SET last_full=? WHERE hash=?",
                "full_backoff": "UPDATE repo_state SET full_backoff=? WHERE hash=?",
            }[column]
            db.execute(query, (value, h))
            return
    if name.startswith(".failed-outcome-"):
        h = name.removeprefix(".failed-outcome-")
        if not _HASH.fullmatch(h):
            raise LegacyArtifactInvalid("invalid-source-name")
        raise LegacyArtifactInvalid("legacy-failed-outcome")
    if name.startswith(".outcome-"):
        _import_outcome(db, name.removeprefix(".outcome-"), raw, ops)
        return
    suffix = next(
        (s for s in (".inflight.json", ".failed.json", ".json") if name.endswith(s)), None
    )
    if suffix is None or not _HASH.fullmatch(name[: -len(suffix)]):
        raise LegacyArtifactInvalid("invalid-source-name")
    h = name[: -len(suffix)]
    data, _ = _queue_data(raw, ops)
    if suffix == ".failed.json":
        failed = {
            **data,
            "reason": "legacy terminal failure",
            "raw_payload": raw,
            "failed_at": time.time(),
        }
        ops.record_failed(db, h, failed, raw, artifact_identity)
    elif suffix == ".inflight.json":
        if isinstance(data.get("claim_id"), str):
            _text(data["claim_id"])
        action, recorded = data.get("outcome_action"), data.get("outcome_recorded_at")
        if (action is None) != (recorded is None) or (
            action is not None and (action not in ops.outcomes or not ops.valid_number(recorded))
        ):
            raise LegacyArtifactInvalid("invalid-legacy-outcome-fields")
        ops.record_claim(db, h, data, raw, artifact_identity)
    else:
        ops.coalesce(db, h, data, legacy=True)


def _stage_artifact(db, source: Path, raw: bytes, mtime: int, import_one: Callable) -> None:
    identity = (os.fsencode(source.name), hashlib.sha256(raw).hexdigest(), str(mtime))
    if db.execute(
        "SELECT 1 FROM legacy_imports WHERE source_name_bytes=? AND content_sha256=? AND source_mtime_ns=?",
        identity,
    ).fetchone():
        return
    db.execute("SAVEPOINT legacy_artifact")
    disposition, reason, rejected = "imported", "ok", raw
    try:
        import_one(db, source, raw, identity)
    except LegacyArtifactInvalid as exc:
        db.execute("ROLLBACK TO legacy_artifact")
        disposition, reason, rejected = "quarantined", str(exc), raw
    db.execute(
        "INSERT INTO legacy_imports VALUES(?,?,?,?,?,?,?)",
        (*identity, time.time(), disposition, reason, rejected),
    )
    db.execute("RELEASE legacy_artifact")


def drain(directory: Path, db, import_one: Callable, sync: Callable) -> list[Path]:
    """Import under the caller's exclusive publication lock and transaction."""
    paths = _claim_files(legacy_files(directory), directory, sync)
    paths.sort(
        key=lambda item: bool(
            item[1].name.startswith(".outcome-") or _OUTCOME.fullmatch(item[1].name)
        )
    )
    imported = []
    for owned, source in paths:
        try:
            raw = owned.read_bytes()
            mtime = owned.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        _stage_artifact(db, source, raw, mtime, import_one)
        imported.append(owned)
    return imported


def retire(directory: Path, imported: list[Path], sync: Callable) -> None:
    """Only call after the representing transaction commits, still locked."""
    for owned in imported:
        owned.unlink(missing_ok=True)
    if imported:
        sync(directory)


def migrate(
    directory: Path, write_db: Callable, import_one: Callable, sync: Callable, *, wait_s: float
):
    if not legacy_files(directory):
        return
    with _lock(directory, wait_s):
        with write_db() as db:
            imported = drain(directory, db, import_one, sync)
        retire(directory, imported, sync)


def import_status(database: Path, *, limit: int, protocol_only=False, observe=None) -> dict:
    """Observe this rollback-journal queue without initialization or migration."""
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    try:
        with database.open("rb") as stream:
            header = stream.read(100)
    except FileNotFoundError:
        return {"state": "absent", "receipts_available": False}
    if not header:
        # An empty file is a queue no writer has initialized yet (for example a
        # creation deferred during an update); it holds no state.
        return {"state": "absent", "receipts_available": False}
    # WAL readers can create sidecars even with mode=ro. This queue is pinned
    # to DELETE; reject other headers before SQLite opens them. This is an
    # operational snapshot, not protection against manual concurrent mode flips.
    if len(header) != 100 or header[:16] != b"SQLite format 3\0" or header[18:20] != b"\1\1":
        raise RuntimeError("unsupported queue header or journal mode")
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            return {
                "state": "legacy-schema",
                "schema_version": version,
                "receipts_available": False,
            }
        if version not in (2, 3, 4):
            raise RuntimeError(f"unsupported queue schema {version}")
        if observe is not None:
            if version != 4:
                return {"state": "protocol-incomplete", "schema_version": version,
                        "execution_available": False}
            db.row_factory = sqlite3.Row
            return observe(db)
        if protocol_only:
            if version == 2:
                return {
                    "state": "legacy-schema",
                    "schema_version": version,
                    "automatic_protocol_ready": 0,
                }
            row = db.execute(
                "SELECT value FROM queue_meta WHERE key='automatic_protocol_ready'"
            ).fetchone()
            if row is None or row[0] != "0":
                raise RuntimeError("unsupported automatic queue protocol")
            return {
                "state": "protocol-incomplete",
                "schema_version": version,
                "automatic_protocol_ready": 0,
            }
        counts = dict(
            db.execute("SELECT disposition,count(*) FROM legacy_imports GROUP BY disposition")
        )
        total = sum(counts.values())
        rows = db.execute(
            "SELECT source_name_bytes,content_sha256,source_mtime_ns,disposition,reason "
            "FROM legacy_imports ORDER BY imported_at DESC,source_name_bytes,content_sha256,source_mtime_ns LIMIT ?",
            (limit,),
        )
        records = [
            dict(
                zip(
                    (
                        "source_name_hex",
                        "content_sha256",
                        "source_mtime_ns",
                        "disposition",
                        "reason",
                    ),
                    (bytes(row[0]).hex(), *row[1:]),
                    strict=True,
                )
            )
            for row in rows
        ]
        return {
            "state": "available",
            "schema_version": version,
            "receipts_available": True,
            "counts": {
                "imported": counts.get("imported", 0),
                "quarantined": counts.get("quarantined", 0),
            },
            "total": total,
            "limit": limit,
            "displayed": len(records),
            "truncated": len(records) < total,
            "records": records,
        }
    finally:
        db.close()
