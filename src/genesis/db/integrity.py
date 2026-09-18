"""SQLite integrity verification and durable database quarantine.

The quarantine marker deliberately lives outside SQLite: a damaged database
cannot be trusted to persist its own failure state. Markers are bound to a
database inode, so an atomic restore naturally makes an old quarantine stale.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from genesis.env import genesis_home

logger = logging.getLogger(__name__)

QUARANTINE_FILENAME = "db_quarantine.json"
_MAX_DETAIL = 2_000


class DatabaseIntegrityError(sqlite3.DatabaseError):
    """Raised when a database is corrupt or cannot be verified safely."""


@dataclass(frozen=True)
class IntegrityResult:
    healthy: bool
    detail: str
    missing: bool = False
    checked_fingerprint: dict[str, int | str] | None = None


def quarantine_path() -> Path:
    return genesis_home() / QUARANTINE_FILENAME


@contextlib.contextmanager
def _quarantine_mutation_lock() -> Iterator[None]:
    """Serialize marker compare-and-mutate operations across processes."""
    lock_path = quarantine_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _fingerprint(path: Path) -> dict[str, int | str]:
    stat = path.stat()
    return {
        "db_path": str(path.resolve()),
        "st_dev": stat.st_dev,
        "st_ino": stat.st_ino,
    }


def _read_marker() -> dict[str, object] | None:
    marker = quarantine_path()
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text())
        if isinstance(payload, dict):
            return payload
        logger.error("DB quarantine marker is not a JSON object: %s", marker)
        return {"_malformed": True}
    except (OSError, json.JSONDecodeError):
        logger.error("DB quarantine marker unreadable: %s", marker, exc_info=True)
        return {"_malformed": True}


def database_is_quarantined(path: str | Path) -> bool:
    """Return whether *path* matches the durable quarantine marker.

    An unreadable marker fails closed. A missing database remains quarantined
    when the marker names its path, preventing an accidental empty replacement
    from silently clearing the stop condition.
    """
    db_path = Path(path)
    marker = _read_marker()
    if marker is None:
        return False
    if marker.get("_malformed"):
        return True
    if marker.get("db_path") != str(db_path.resolve()):
        return False
    try:
        current = _fingerprint(db_path)
    except FileNotFoundError:
        return True
    return marker.get("st_dev") == current["st_dev"] and marker.get("st_ino") == current["st_ino"]


def assert_not_quarantined(path: str | Path) -> None:
    if database_is_quarantined(path):
        raise DatabaseIntegrityError(
            f"database quarantined; inspect {quarantine_path()} before reopening"
        )


def _write_quarantine(
    path: Path,
    *,
    source: str,
    detail: str,
    expected_fingerprint: dict[str, int | str] | None = None,
) -> bool:
    """Publish a marker only if it still describes the checked database."""
    marker = quarantine_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    with _quarantine_mutation_lock():
        try:
            current_fingerprint = _fingerprint(path)
        except FileNotFoundError:
            current_fingerprint = None
        if expected_fingerprint is not None and current_fingerprint != expected_fingerprint:
            logger.error(
                "Refusing to quarantine %s: identity changed after integrity check",
                path,
            )
            return False

        payload: dict[str, object] = {
            "detected_at": datetime.now(UTC).isoformat(),
            "source": source,
            "detail": detail[:_MAX_DETAIL],
        }
        if current_fingerprint is not None:
            payload.update(current_fingerprint)
        else:
            payload["db_path"] = str(path.resolve())

        fd, tmp_name = tempfile.mkstemp(dir=marker.parent, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, marker)
            with contextlib.suppress(OSError):
                dir_fd = os.open(marker.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    return True


def quarantine_database(path: str | Path, *, source: str, detail: str) -> None:
    """Durably prevent a database from being opened until it is replaced."""
    _write_quarantine(Path(path), source=source, detail=detail)


def _clear_quarantine_for(path: Path) -> None:
    with _quarantine_mutation_lock():
        marker_path = quarantine_path()
        marker = _read_marker()
        if (
            marker is None
            or marker.get("_malformed")
            or marker.get("db_path") != str(path.resolve())
        ):
            return
        try:
            current = _fingerprint(path)
        except FileNotFoundError:
            return
        # A healthy result on the same inode is weaker evidence than the failure
        # that established quarantine. Only a verified replacement clears it.
        if marker.get("st_dev") == current["st_dev"] and marker.get("st_ino") == current["st_ino"]:
            return
        with contextlib.suppress(FileNotFoundError):
            marker_path.unlink()


def _quick_check_stable_identity(db_path: Path) -> IntegrityResult | None:
    """Check one inode identity, or return ``None`` when it changed mid-check."""
    try:
        before = _fingerprint(db_path)
    except FileNotFoundError:
        return None
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        error = None
    except sqlite3.Error as exc:
        rows = []
        error = f"quick_check could not complete: {exc}"
    try:
        after = _fingerprint(db_path)
    except FileNotFoundError:
        return None
    if before != after:
        return None
    if error is not None:
        return IntegrityResult(False, error, checked_fingerprint=after)
    values = [str(row[0]) for row in rows]
    if values == ["ok"]:
        return IntegrityResult(True, "ok", checked_fingerprint=after)
    detail = "\n".join(values) if values else "quick_check returned no rows"
    return IntegrityResult(False, detail, checked_fingerprint=after)


def quick_check(path: str | Path) -> IntegrityResult:
    """Run a WAL-aware, read-only ``PRAGMA quick_check`` on a stable inode."""
    db_path = Path(path)
    if not db_path.exists():
        return IntegrityResult(True, "database does not exist yet", missing=True)
    for _attempt in range(3):
        result = _quick_check_stable_identity(db_path)
        if result is not None:
            return result
    return IntegrityResult(
        False,
        "database identity changed repeatedly during quick_check; integrity is indeterminate",
    )


def require_healthy_database(
    path: str | Path,
    *,
    source: str,
    quarantine_on_failure: bool = True,
) -> IntegrityResult:
    """Verify *path*, quarantining a stable non-healthy result."""
    db_path = Path(path)
    assert_not_quarantined(db_path)
    result = quick_check(db_path)
    if result.healthy:
        if not result.missing:
            _clear_quarantine_for(db_path)
        return result
    if quarantine_on_failure and result.checked_fingerprint is not None:
        _write_quarantine(
            db_path,
            source=source,
            detail=result.detail,
            expected_fingerprint=result.checked_fingerprint,
        )
    raise DatabaseIntegrityError(result.detail)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or quarantine a Genesis SQLite DB")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("path")
    check.add_argument("--source", default="cli")
    check.add_argument("--quarantine-on-failure", action="store_true")
    mark = subparsers.add_parser("mark")
    mark.add_argument("path")
    mark.add_argument("--source", required=True)
    mark.add_argument("--detail", required=True)
    args = parser.parse_args(argv)

    if args.command == "mark":
        quarantine_database(args.path, source=args.source, detail=args.detail)
        print("quarantined")
        return 0

    try:
        result = require_healthy_database(
            args.path,
            source=args.source,
            quarantine_on_failure=args.quarantine_on_failure,
        )
    except DatabaseIntegrityError as exc:
        print(f"UNHEALTHY: {exc}")
        return 2
    print("NEW" if result.missing else "OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
