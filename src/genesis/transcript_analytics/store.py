"""Durable per-source Parquet records behind one writer lock; no catalog.

Each per-source record is a directory ``sources/<key>/<record>/`` holding one
Parquet file per table, all stamped with the same ``ta.record`` id, source
identity, fingerprint and semantics. A record is published by renaming its
staged directory into place and atomically swapping ``sources/<key>/current``
to it, so a reader resolves one complete record — the old one or the new one,
never a mix. Records written by the earlier flat prototype
(``<table>__<key>.parquet`` beside ``sources/``) are read in place: their
footers carry the same ``ta.generation``/semantics stamps, so nothing is
copied and nothing can be dropped in transit.

Writes and prunes hold the single writer lock. Physical revisions are retained
until explicit prune, which requires maintenance with readers stopped.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from numbers import Real
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from genesis.transcript_analytics import history, locks, scrub
from genesis.transcript_analytics.extract import TABLES, extract_source, utc_timestamp
from genesis.transcript_analytics.identity import source_identity
from genesis.transcript_analytics.identity import source_path as source_path
from genesis.transcript_analytics.schema import SCHEMA_VERSION, SCHEMAS

MARKER = "fragments"
DEFAULT_LOCK = locks.DEFAULT_LOCK
Busy = locks.Busy
_KEY = re.compile(r"[0-9a-f]{16}\Z")
_RECORD = re.compile(r"[0-9a-f]{32}\Z")
_MAX_METADATA_BYTES = 1024 * 1024


def _extraction_signature():
    # Storage layout is independent of row semantics. A future orchestration
    # change affecting extracted rows requires an explicit reviewed domain bump.
    digest = hashlib.sha256(b"ta-extraction-v1\0")
    for name in ("extract.py", "classify.py", "schema.py", "scrub.py", "identity.py"):
        raw = Path(__file__).with_name(name).read_bytes()
        digest.update(name.encode() + b"\0" + len(raw).to_bytes(8, "big") + raw)
    return digest.hexdigest()


EXTRACTION_VERSION = _extraction_signature()
_BRIDGE_SIGNATURE = "a63ab5e40033ee4a1faedeecc2b584ee254ba260ce9786ea3996e1bc621d1189"  # pragma: allowlist secret — extraction-compatibility fingerprint
_BRIDGE_PREDECESSOR = "7be3e9853e362faed755ba100f2a4004a360623728f5cdb4c25add9d9fad7402"  # pragma: allowlist secret — extraction-compatibility fingerprint


class ScrubberUnavailable(RuntimeError):
    """The secret scrubber cannot be loaded; refusing to write anything."""


class SourceIdentityConflict(ValueError):
    """A placement key cannot select two distinct full source identities."""


def _fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _make_directory(path: Path):
    """Create the directory and fsync every ancestor entry created."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise OSError(f"store path is not a regular directory: {current}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    resolved = path.resolve(strict=True)
    for directory in (resolved, *resolved.parents):
        if directory.parent == directory:
            break
        _fsync_directory(directory.parent)


def srckey(rel: str) -> str:
    return hashlib.sha1(os.fsencode(rel), usedforsecurity=False).hexdigest()[:16]


def discover(projects: Path) -> list[tuple[Path, str]]:
    """Every *.jsonl transcript under ``projects`` except workflow journals."""
    out = []
    for dirpath, _dirs, files in os.walk(projects, followlinks=False, onerror=_discovery_error):
        for name in files:
            if name.endswith(".jsonl") and name != "journal.jsonl":
                p = Path(dirpath) / name
                if not p.is_symlink() and p.is_file():
                    out.append((p, str(p.relative_to(projects))))
    return out


def _discovery_error(error):
    raise error


def _fingerprint(st: os.stat_result) -> dict:
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


@contextlib.contextmanager
def _open_regular(path):
    """Admit the descriptor actually read without blocking on special files."""
    with open(path, "rb", opener=lambda name, flags: os.open(
        name, flags | os.O_NOFOLLOW | os.O_NONBLOCK
    )) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("source is not a regular file")
        yield handle


def source_fingerprint(path: Path, st: os.stat_result | None = None) -> dict:
    fp = _fingerprint(st or path.stat())
    if path.name.startswith("agent-"):
        meta = path.with_suffix(".meta.json")
        if meta.is_symlink():
            raise ValueError("agent metadata must not be a symlink")
        try:
            with _open_regular(meta) as handle:
                body = handle.read(_MAX_METADATA_BYTES + 1)
            if len(body) > _MAX_METADATA_BYTES:
                raise ValueError("agent metadata is too large")
            fp["meta"] = hashlib.sha256(body).hexdigest()
        except FileNotFoundError:
            fp["meta"] = None
    return fp


def semantics() -> dict[bytes, bytes]:
    return {
        b"ta.schema": SCHEMA_VERSION.encode(),
        b"ta.extract": EXTRACTION_VERSION.encode(),
        b"ta.scrub": (scrub.version() or "").encode(),
    }


def semantics_compatible(stored: dict) -> bool:
    """Exact semantics, or the single audited storage-only predecessor bridge."""
    current = semantics()
    if any(stored.get(k) != current[k] for k in (b"ta.schema", b"ta.scrub")):
        return False
    version = stored.get(b"ta.extract")
    return version == current[b"ta.extract"] or (
        EXTRACTION_VERSION == _BRIDGE_SIGNATURE
        and SCHEMA_VERSION == "3"
        and version == _BRIDGE_PREDECESSOR.encode()
    )


def _flat_sources(data: Path) -> set[str]:
    """Keys whose six tables sit in the legacy flat ``<table>__<key>`` layout."""
    keys: set[str] = set()
    for table in TABLES:
        for p in data.glob(f"{table}__*.parquet"):
            key = p.stem.split("__", 1)[1]
            if _KEY.fullmatch(key):
                keys.add(key)
    return keys


_CATALOG_FIELDS = {
    "format",
    "revision",
    "population_revision",
    "projects_root",
    "sources",
    "collection",
}


def _no_duplicate_catalog_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("invalid source selector")
        result[key] = value
    return result


def _historical_catalog(data: Path) -> dict[str, str] | None:
    """Read the audited #3002 selector; never choose a generation without it."""
    try:
        raw = (data / "source-catalog.json").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("invalid source selector") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_catalog_keys)
        if (
            not isinstance(value, dict)
            or set(value) != _CATALOG_FIELDS
            or value["format"] != "ta-source-catalog-v1"
            or not _RECORD.fullmatch(value["revision"])
            or not _RECORD.fullmatch(value["population_revision"])
            or not isinstance(value["projects_root"], str)
            or not value["projects_root"].startswith("/")
            or os.path.normpath(value["projects_root"]) != value["projects_root"]
            or not isinstance(value["sources"], dict)
            or not isinstance(value["collection"], dict)
        ):
            raise ValueError("invalid source selector")
        selection = {}
        for source_key, generation in value["sources"].items():
            if not _KEY.fullmatch(source_key) or not (
                generation == "legacy" or _RECORD.fullmatch(generation)
            ):
                raise ValueError("invalid source selector")
            selection[source_key] = generation
        return selection
    except (TypeError, AttributeError, UnicodeError, RecursionError, ValueError) as exc:
        raise ValueError("invalid source selector") from exc


def _records(data: Path, key: str) -> list[Path]:
    """Resolve one selector; absent per-source selectors may use audited history."""
    base = data / "sources" / key
    current = base / "current"
    try:
        if base.is_symlink() or base.parent.is_symlink():
            raise ValueError("invalid source selector")
        current.lstat()
    except FileNotFoundError:
        selection = _historical_catalog(data)
        if selection is None:
            return []
        generation = selection.get(key)
        if generation == "legacy":
            return []
        if generation is None:
            raise ValueError("invalid source selector") from None
        directory = base / generation
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("invalid source selector") from None
        return [directory]
    except OSError as exc:
        raise ValueError("invalid source selector") from exc
    try:
        resolved = current.resolve(strict=True)
        if not current.is_symlink() or resolved.parent != base.resolve() or not resolved.is_dir():
            raise ValueError("invalid source selector")
        return [resolved]
    except (OSError, RuntimeError) as exc:
        raise ValueError("invalid source selector") from exc


def source_keys(data: Path) -> list[str]:
    keys = set(_flat_sources(data))
    root = data / "sources"
    if root.is_dir():
        for entry in root.iterdir():
            if _KEY.fullmatch(entry.name):
                keys.add(entry.name)
    return sorted(keys)


def _table_path(data: Path, table: str, key: str) -> Path:
    records = _records(data, key)
    if records:
        return records[0] / f"{table}.parquet"
    return data / f"{table}__{key}.parquet"


def _record_id(metadata: dict) -> bytes | None:
    return metadata.get(b"ta.record") or metadata.get(b"ta.generation")


@dataclass(frozen=True)
class SourceRecord:
    key: str
    data: Path
    directory: Path | None
    metadata: dict
    adapter: str | None = None

    def path(self, table):
        return (
            self.directory / f"{table}.parquet"
            if self.directory
            else self.data / f"{table}__{self.key}.parquet"
        )


def capture_source(data: Path, key: str):
    """Resolve once, then validate all six tables against that same selection."""
    try:
        directories = _records(data, key)
    except ValueError:
        return SourceRecord(key, data, None, {}), "invalid source selector"
    record = SourceRecord(key, data, directories[0] if directories else None, {})
    return _capture_record(record)


def _capture_record(record):
    try:
        marker = pq.read_metadata(record.path(MARKER)).metadata or {}
    except (OSError, ValueError, KeyError):
        marker = {}
    record = replace(record, metadata=marker)
    try:
        footers = [pq.read_metadata(record.path(table)) for table in TABLES]
        records = [footer.metadata or {} for footer in footers]
    except (OSError, ValueError, KeyError):
        return record, "missing or unreadable table"
    # The canonical marker retains captured-byte rename proof authority.
    # Reading another table's digest must not replace a missing marker proof.
    first = records[TABLES.index(MARKER)]
    record = replace(record, metadata=first, adapter=history.format_for(first))
    if any(
        not footer.schema.to_arrow_schema().equals(
            history.schema_for(table, record.adapter), check_metadata=False
        )
        for table, footer in zip(TABLES, footers, strict=True)
    ):
        return record, "incompatible table schema"
    if not record.adapter and any(not semantics_compatible(md) for md in records):
        return record, "incompatible semantics"
    if not _record_id(first) or any(
        any(
            md.get(k) != first.get(k)
            for k in (
                b"ta.record",
                b"ta.generation",
                b"ta.fp",
                b"ta.source",
                b"ta.schema",
                b"ta.scrub",
                b"ta.extract",
            )
        )
        for md in records
    ):
        return record, "inconsistent record"
    if srckey(os.fsdecode(first.get(b"ta.source", b""))) != record.key:
        return record, "source identity conflict"
    if record.directory and (
        not re.fullmatch(r"[0-9a-f]{32}", record.directory.name)
        or _record_id(first) != os.fsencode(record.directory.name)
    ):
        return record, "record directory identity conflict"
    return record, None


def source_metadata(data: Path, key: str) -> tuple[dict, str | None]:
    record, reason = capture_source(data, key)
    return record.metadata, reason


def select_sources(data: Path):
    accepted, excluded = [], []
    for key in source_keys(data):
        record, reason = capture_source(data, key)
        if reason:
            excluded.append(
                {
                    "key": key,
                    "source": source_identity(os.fsdecode(record.metadata.get(b"ta.source", b""))),
                    "reason": reason,
                }
            )
        else:
            accepted.append(record)
    return accepted, excluded


def compatible_sources(data: Path) -> tuple[list[str], list[dict]]:
    records, excluded = select_sources(data)
    return [record.key for record in records], excluded


def inputs_signature(data: Path, *, selection=None) -> str:
    """Identity of what a derived snapshot read: accepted records + semantics."""
    digest = hashlib.sha256(b"ta-inputs-v1\0")
    for key, value in sorted(semantics().items()):
        digest.update(key + b"\0" + value + b"\0")
    records, excluded = select_sources(data) if selection is None else selection
    digest.update(json.dumps(excluded, sort_keys=True).encode())
    for record in records:
        md = record.metadata
        digest.update(
            record.key.encode()
            + b"\0"
            + (_record_id(md) or b"")
            + b"\0"
            + md.get(b"ta.fp", b"")
            + b"\0"
            + md.get(b"ta.schema", b"")
            + b"\0"
            + md.get(b"ta.extract", b"")
            + b"\0"
            + md.get(b"ta.scrub", b"")
            + b"\0"
        )
    return digest.hexdigest()


def is_current(data: Path, key: str, fp: dict, *, source=None) -> bool:
    md, reason = source_metadata(data, key)
    return (
        reason is None
        and semantics_compatible(md)
        and (source is None or md.get(b"ta.source") == os.fsencode(source))
        and md.get(b"ta.fp") == json.dumps(fp).encode()
    )


_utc_timestamp = utc_timestamp


def _projects_root(data, projects, *, adopt=False):
    path = data / "projects-root.json"
    root = str(projects.resolve(strict=True))
    try:
        saved = json.loads(path.read_text(), object_pairs_hook=_no_duplicate_catalog_keys)["projects_root"]
    except FileNotFoundError:
        if source_keys(data) and not adopt:
            raise ValueError(
                "store has no projects root; ingest with --adopt-projects-root after verifying its origin"
            ) from None
        return root
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("store projects-root record is unreadable") from exc
    if (not isinstance(saved, str) or not saved.startswith("/") or "\0" in saved
            or os.path.normpath(saved) != saved):
        raise ValueError("store projects-root record is unreadable")
    if saved != root and not adopt:
        raise ValueError("projects root differs from the store; explicit root adoption required")
    return root


def _to_table(rows: list[dict], table: str, meta: dict) -> pa.Table:
    # The extractor returns normalized rows; repeating identity transport would
    # escape the reserved prefixes twice and break raw evidence addressing.
    return pa.Table.from_pylist(rows, schema=SCHEMAS[table]).replace_schema_metadata(meta)


def _publish_record(data: Path, key: str, meta: dict, table_reader) -> str:
    """Stage, fence and atomically select one complete source record."""
    record = meta[b"ta.record"].decode()
    key_base = data / "sources" / key
    staging = data / ".staging" / "sources" / key / record
    staging.mkdir(parents=True)
    try:
        for table in TABLES:
            temporary = staging / f"{table}.parquet"
            body = table_reader(table).replace_schema_metadata(meta)
            if not body.schema.equals(SCHEMAS[table], check_metadata=False):
                raise ValueError("staged source schema failed validation")
            pq.write_table(body, temporary, compression="zstd")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            footer = pq.read_metadata(temporary)
            observed = footer.metadata or {}
            if (
                not footer.schema.to_arrow_schema().equals(SCHEMAS[table], check_metadata=False)
                or footer.num_rows != body.num_rows
            ):
                raise ValueError("staged source schema or row count failed validation")
            if any(observed.get(field) != value for field, value in meta.items()):
                raise ValueError("staged source metadata failed validation")
        if {path.name for path in staging.iterdir()} != {f"{table}.parquet" for table in TABLES}:
            raise ValueError("staged source table population failed validation")
        if srckey(os.fsdecode(meta[b"ta.source"])) != key:
            raise ValueError("staged source identity failed validation")
        # Fence the complete record, then make it visible with one atomic swap.
        _fsync_directory(staging)
        _make_directory(key_base)
        target = key_base / record
        if target.exists() or target.is_symlink():
            raise ValueError(f"record already exists: {key}/{record}")
        os.rename(staging, target)
        _fsync_directory(key_base)
        _fsync_directory(staging.parent)
        link_tmp = key_base / f".current.{os.getpid()}"
        link_tmp.unlink(missing_ok=True)  # crashed swap may leave a same-pid temp link
        current = key_base / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        os.symlink(record, link_tmp)
        os.replace(link_tmp, current)
        try:
            _fsync_directory(key_base)
        except BaseException:
            if previous is None:
                current.unlink()
            else:
                link_tmp.symlink_to(previous)
                os.replace(link_tmp, current)
            _fsync_directory(key_base)
            raise
        _prune_staging_parents(staging)
        return record
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        _prune_staging_parents(staging)
        raise


def build_source(
    path: Path,
    rel: str,
    data: Path,
    st: os.stat_result,
    *,
    lock_path=None,
    _locked=False,
) -> dict:
    if not scrub.available():
        raise ScrubberUnavailable("secret scrubber could not be loaded; nothing written")
    if _locked:
        return _build_source(path, rel, data, st)
    with locks.writer(lock_path or DEFAULT_LOCK):
        _make_directory(data)
        os.chmod(data, 0o700)
        return _build_source(path, rel, data, st)


def _build_source(
    path: Path,
    rel: str,
    data: Path,
    st: os.stat_result,
) -> dict:
    """Extract one source into a new immutable record; never overwrite bodies."""
    fp = source_fingerprint(path, st)
    # Fingerprint the descriptor actually read (review #23): a path re-stat can
    # describe a different inode than the one extraction consumed, and comparing
    # only ``meta`` missed body churn. Compare the whole fingerprint afterwards.
    with _open_regular(path) as handle:
        res = extract_source(path, rel, stop_at=st.st_size, fh=handle)
        observed = source_fingerprint(path, os.fstat(handle.fileno()))
    if observed != fp:
        raise ValueError("source changed during extraction; retry required")
    key = srckey(rel)
    if key in source_keys(data):
        previous, reason = source_metadata(data, key)
        if reason is None:
            previous_source = previous.get(b"ta.source")
            if previous_source is None or previous_source != os.fsencode(rel):
                raise SourceIdentityConflict(
                    "source key identity conflict; accepted history retained"
                )
    meta = {
        b"ta.source": os.fsencode(rel),
        b"ta.fp": json.dumps(fp).encode(),
        b"ta.schema": SCHEMA_VERSION.encode(),
        **semantics(),
        b"ta.record": uuid.uuid4().hex.encode(),
        b"ta.ingested_at": datetime.now(UTC).isoformat().encode(),
        b"ta.last_ts": (
            res.last_timestamp if res.chronology_known and res.last_timestamp else ""
        ).encode(),
        b"ta.content_sha256": res.content_sha256.encode(),
        b"ta.stats": json.dumps(res.stats).encode(),
    }
    _publish_record(data, key, meta, lambda table: _to_table(res.tables[table], table, meta))
    return res.stats


def _prune_staging_parents(staging: Path):
    """Remove the now-empty ``.staging`` chain a completed record left behind."""
    for directory in (staging.parent, *staging.parents):
        if directory.name == ".staging":
            with contextlib.suppress(OSError):
                directory.rmdir()
            break
        with contextlib.suppress(OSError):
            directory.rmdir()
        if directory.exists():
            break


def _sweep_staging(data: Path):
    """Leftovers from a killed writer (SIGKILL/SIGTERM skip Python cleanup)."""
    root = data / ".staging"
    if root.is_dir():
        shutil.rmtree(root)


def ingest(
    projects: Path,
    data: Path,
    *,
    since_days: float | None = None,
    lock_path: Path | None = None,
    adopt_projects_root: bool = False,
) -> dict:
    if since_days is not None and (
        isinstance(since_days, bool)
        or not isinstance(since_days, Real)
        or not math.isfinite(since_days)
        or since_days <= 0
    ):
        raise ValueError("since_days must be a finite positive number")
    if not scrub.available():
        raise ScrubberUnavailable("secret scrubber could not be loaded; nothing written")
    summary = {
        "sources": 0,
        "rebuilt": 0,
        "unchanged": 0,
        "skipped_old": 0,
        "vanished": 0,
        "failed": 0,
        "failed_sources": [],
        "lines": 0,
        "malformed": 0,
        "bytes_read": 0,
        "seconds": 0.0,
    }
    t0 = time.monotonic()
    cutoff = time.time() - since_days * 86400 if since_days else None
    with locks.writer(lock_path or DEFAULT_LOCK):
        _make_directory(data)
        os.chmod(data, 0o700)
        root = _projects_root(data, projects, adopt=adopt_projects_root)
        sources = discover(Path(root))
        _atomic_write(data / "projects-root.json", json.dumps({"projects_root": root}))
        _sweep_staging(data)
        keys = set(source_keys(data))
        unbuilt = {source_identity(rel) for _, rel in sources if srckey(rel) not in keys}
        for path, rel in sources:
            summary["sources"] += 1
            try:
                st = os.stat(path)
                key = srckey(rel)
                if cutoff is not None and st.st_mtime < cutoff and key not in keys:
                    summary["skipped_old"] += 1
                    summary["sources"] -= 1
                    continue
                fp = source_fingerprint(path, st)
                if is_current(data, key, fp, source=rel):
                    summary["unchanged"] += 1
                    unbuilt.discard(source_identity(rel))
                    continue
                stats = build_source(path, rel, data, st, _locked=True)
            except FileNotFoundError:
                summary["vanished"] += 1
                summary["sources"] -= 1
                continue
            except Exception as e:  # noqa: BLE001 - isolate source extraction failures
                summary["failed"] += 1
                summary["failed_sources"].append(rel)
                print(f"transcript-analytics: FAILED {rel}: {e!r}", file=sys.stderr)
                continue
            summary["rebuilt"] += 1
            unbuilt.discard(source_identity(rel))
            for k in ("lines", "malformed", "bytes_read"):
                summary[k] += stats[k]
        _sweep_staging(data)
        summary["unbuilt"] = sorted(unbuilt)
        summary["complete"] = not summary["failed"] and not summary["vanished"]
        summary["completed_at"] = datetime.now(UTC).isoformat()
        summary["projects_root"] = root
        summary["seconds"] = round(time.monotonic() - t0, 2)
        _atomic_write(
            data / "projects-root.json", json.dumps({"projects_root": summary["projects_root"]})
        )
        _atomic_write(data / "inventory.json", json.dumps(summary))
    return summary


def inventory(data: Path):
    try:
        return json.loads((data / "inventory.json").read_text())
    except (OSError, ValueError):
        return {"unavailable": "no source discovery inventory"}


def _prunable_key(marker, projects, cutoff, key=None):
    try:
        md = pq.read_metadata(marker).metadata or {}
        rel = os.fsdecode(md.get(b"ta.source", b""))
        last = _utc_timestamp(md.get(b"ta.last_ts", b"").decode())
        relative = Path(rel)
        if (
            not relative.parts or relative.is_absolute() or ".." in relative.parts
            or not (projects / relative).resolve().is_relative_to(projects.resolve())
        ):
            return None
    except (OSError, ValueError, RuntimeError):
        print(f"prune: unreadable marker retained: {marker.name}", file=sys.stderr)
        return None
    if not rel or (key is not None and srckey(rel) != key) or (projects / rel).exists():
        return None
    if last is None:
        print(f"prune: unknown retention timestamp retained: {rel}", file=sys.stderr)
        return None
    if last >= cutoff:
        return None
    return key if key is not None else marker.name[len(MARKER) + 2 : -len(".parquet")]


def _remove_record(data: Path, key: str):
    base = data / "sources" / key
    if base.is_symlink() or base.parent.is_symlink():
        raise ValueError("refusing symlinked source directory")
    if base.is_dir() or base.is_symlink():
        for entry in list(base.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        base.rmdir()
        _fsync_directory(data / "sources")
    for table in TABLES:
        (data / f"{table}__{key}.parquet").unlink(missing_ok=True)
    _fsync_directory(data)


def _retained_records(data, key):
    selected, reason = capture_source(data, key)
    if reason:
        return []
    records = [selected]
    base = data / "sources" / key
    try:
        if base.exists():
            for entry in base.iterdir():
                if (
                    entry.name == "current" or entry == selected.directory
                    or (entry.is_symlink() and re.fullmatch(r"\.current\.[0-9]+", entry.name))
                ):
                    continue
                if entry.is_symlink() or not entry.is_dir():
                    return []
                record, reason = _capture_record(SourceRecord(key, data, entry, {}))
                if reason:
                    return []
                records.append(record)
        if selected.directory and any(
            os.path.lexists(data / f"{table}__{key}.parquet") for table in TABLES
        ):
            flat, reason = _capture_record(SourceRecord(key, data, None, {}))
            if reason:
                return []
            records.append(flat)
    except (OSError, ValueError, RuntimeError):
        return []
    return records


def _prunable_record(record: SourceRecord, projects: Path, cutoff: datetime, key: str):
    # flat-v1 stamps the newest persisted row, not the source chronology. When
    # the original is gone there is no trustworthy bound proving later source
    # events were never extracted, so retirement must retain it.
    if record.adapter == "flat-v1":
        return None
    return _prunable_key(record.path(MARKER), projects, cutoff, key)


def prune(data: Path, *, before: str, projects: Path, lock_path: Path | None = None) -> int:
    """Delete the files of sources whose transcript is GONE and whose last
    validated retained records are all older than ``before`` (ISO date). A source whose transcript
    still exists is kept: the next ingest would only rebuild it.

    Maintenance only: stop readers first. Refuses (ValueError) a malformed date, and a projects directory that is
    missing or differs from the bound root. An empty bound root is valid."""
    cutoff = datetime.combine(date.fromisoformat(before), datetime.min.time(), tzinfo=UTC)
    if not projects.is_dir():
        raise ValueError(f"projects directory missing: {projects}")
    removed = 0
    with locks.writer(lock_path or DEFAULT_LOCK):
        # Pruning is read-only with respect to root binding: never adopt implicitly.
        try:
            root = json.loads((data / "projects-root.json").read_text())["projects_root"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                "store projects root is unbound or unreadable; ingest with explicit adoption first"
            ) from exc
        if root != str(projects.resolve(strict=True)):
            raise ValueError("projects root differs from the store")
        retired = []
        for key in source_keys(data):
            records = _retained_records(data, key)
            if records and all(
                _prunable_record(record, projects, cutoff, key) == key
                for record in records
            ):
                retired.append(key)
        for key in retired:
            _remove_record(data, key)
        removed = len(retired)
    return removed
