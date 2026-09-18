"""SQLite integrity verification and durable database quarantine.

The quarantine marker deliberately lives outside SQLite: a damaged database
cannot be trusted to persist its own failure state.  Markers are bound to the
database inode, so an atomic restore naturally makes an old quarantine stale.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sqlite3
import tempfile
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


def quarantine_path() -> Path:
    return genesis_home() / QUARANTINE_FILENAME


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
        logger.error("DB quarantine marker is unreadable: %s", marker, exc_info=True)
        return {"_malformed": True}


def database_is_quarantined(path: str | Path) -> bool:
    """Return whether *path* matches the durable quarantine marker.

    An unreadable marker logs loudly but does not permanently brick startup.
    A missing database remains quarantined when the marker names that path,
    preventing an accidental empty replacement from silently becoming live.
    """
    db_path = Path(path).resolve()
    marker = _read_marker()
    if marker is None:
        return False
    if marker.get("_malformed"):
        return True
    if marker.get("db_path") != str(db_path):
        return False
    try:
        current = _fingerprint(db_path)
    except FileNotFoundError:
        return True
    return (
        marker.get("st_dev") == current["st_dev"]
        and marker.get("st_ino") == current["st_ino"]
    )


def assert_not_quarantined(path: str | Path) -> None:
    if database_is_quarantined(path):
        raise DatabaseIntegrityError(
            f"database is quarantined; inspect {quarantine_path()} before reopening"
        )


def _write_quarantine(path: Path, *, source: str, detail: str) -> None:
    marker = quarantine_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "detected_at": datetime.now(UTC).isoformat(),
        "source": source,
        "detail": detail[:_MAX_DETAIL],
    }
    try:
        payload.update(_fingerprint(path))
    except FileNotFoundError:
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


def quarantine_database(path: str | Path, *, source: str, detail: str) -> None:
    """Durably prevent a database from being opened until it is replaced."""
    _write_quarantine(Path(path), source=source, detail=detail)


def _clear_quarantine_for(path: Path) -> None:
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
    # that established quarantine (and may race an older checker).  Only a
    # verified replacement inode clears the durable stop condition.
    if (
        marker.get("st_dev") == current["st_dev"]
        and marker.get("st_ino") == current["st_ino"]
    ):
        return
    with contextlib.suppress(FileNotFoundError):
        marker_path.unlink()


def quick_check(path: str | Path) -> IntegrityResult:
    """Run a WAL-aware, read-only ``PRAGMA quick_check``."""
    db_path = Path(path)
    if not db_path.exists():
        return IntegrityResult(True, "database does not exist yet", missing=True)

    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error as exc:
        return IntegrityResult(False, f"quick_check could not complete: {exc}")

    values = [str(row[0]) for row in rows]
    if values == ["ok"]:
        return IntegrityResult(True, "ok")
    detail = "\n".join(values) if values else "quick_check returned no rows"
    return IntegrityResult(False, detail)


def require_healthy_database(
    path: str | Path,
    *,
    source: str,
    quarantine_on_failure: bool = True,
) -> IntegrityResult:
    """Verify *path*, quarantining and raising on any non-healthy result."""
    db_path = Path(path)
    assert_not_quarantined(db_path)
    result = quick_check(db_path)
    if result.healthy:
        if not result.missing:
            _clear_quarantine_for(db_path)
        return result

    if quarantine_on_failure:
        _write_quarantine(db_path, source=source, detail=result.detail)
    raise DatabaseIntegrityError(result.detail)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and quarantine a Genesis SQLite DB")
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
    print("NEW" if result.missing else "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
