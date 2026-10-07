"""Per-source Parquet store: discovery, staleness, atomic publish, prune.

Layout (flat, so a single-level backup lister can copy it):
    <data>/<table>__<srckey>.parquet      one file per table per source
    <data>/.staging/                      same filesystem; never matched by the table globs

No source index; projects-root.json binds the origin. A source needs
rebuilding when any of its table files is
missing, or when the ``fragments`` file's stored fingerprint, schema version
or scrubber version differs from now. ``fragments`` is renamed into place LAST,
so it is the commit marker: a crash part-way through publishing leaves it stale
and the next run rebuilds the whole source. Every writer (ingest, prune) holds
one exclusive lock, so two runs cannot interleave their publishes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import sys
import time
import uuid
from datetime import UTC, date, datetime
from numbers import Real
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from genesis.env import genesis_home
from genesis.transcript_analytics import scrub
from genesis.transcript_analytics.extract import TABLES, extract_source, utc_timestamp
from genesis.transcript_analytics.identity import source_identity
from genesis.transcript_analytics.identity import source_path as source_path
from genesis.transcript_analytics.locks import publication
from genesis.transcript_analytics.schema import SCHEMA_VERSION, SCHEMAS

MARKER = "fragments"
EXTRACTION_VERSION = hashlib.sha256(
    b"".join(
        Path(__file__).with_name(name).read_bytes()
        for name in (
            "extract.py",
            "classify.py",
            "schema.py",
            "scrub.py",
            "identity.py",
            "store.py",
        )
    )
).hexdigest()
_PUBLISH_ORDER = [t for t in TABLES if t != MARKER] + [MARKER]
DEFAULT_LOCK = genesis_home() / "locks" / "transcript-analytics.lock"
_STAGING_MAX_AGE_S = 3600  # an ingest holds the lock, so older staging files are dead


class Busy(RuntimeError):
    """Another ingest/prune holds the lock."""


class ScrubberUnavailable(RuntimeError):
    """The secret scrubber cannot be loaded; refusing to write anything."""


@contextlib.contextmanager
def _locked(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Busy(str(lock_path)) from e
        yield


def srckey(rel: str) -> str:
    return hashlib.sha1(os.fsencode(rel), usedforsecurity=False).hexdigest()[:16]


def discover(projects: Path) -> list[tuple[Path, str]]:
    """Every *.jsonl transcript under ``projects`` except workflow journals."""
    out = []
    for dirpath, _dirs, files in os.walk(projects, followlinks=False):
        for name in files:
            if name.endswith(".jsonl") and name != "journal.jsonl":
                p = Path(dirpath) / name
                if not p.is_symlink() and p.is_file():
                    out.append((p, str(p.relative_to(projects))))
    return out


def _fingerprint(st: os.stat_result) -> dict:
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def source_fingerprint(path: Path, st: os.stat_result | None = None) -> dict:
    fp = _fingerprint(st or path.stat())
    if path.name.startswith("agent-"):
        meta = path.with_suffix(".meta.json")
        if meta.is_symlink():
            raise ValueError("agent metadata must not be a symlink")
        try:
            fp["meta"] = hashlib.sha256(meta.read_bytes()).hexdigest()
        except FileNotFoundError:
            fp["meta"] = None
    return fp


def semantics() -> dict[bytes, bytes]:
    return {
        b"ta.schema": SCHEMA_VERSION.encode(),
        b"ta.extract": EXTRACTION_VERSION.encode(),
        b"ta.scrub": (scrub.version() or "").encode(),
    }


def source_metadata(data: Path, key: str) -> tuple[dict, str | None]:
    """Validate the entire source commit, including empty tables."""
    try:
        records = [pq.read_metadata(_table_path(data, t, key)).metadata or {} for t in TABLES]
    except (OSError, ValueError):
        return {}, "missing or unreadable table"
    first = records[0]
    if any(any(md.get(k) != v for k, v in semantics().items()) for md in records):
        return first, "incompatible semantics"
    if not first.get(b"ta.generation") or any(
        any(md.get(k) != first.get(k) for k in (b"ta.generation", b"ta.fp", b"ta.source"))
        for md in records
    ):
        return first, "mixed generation"
    return first, None


def compatible_sources(data: Path) -> tuple[list[str], list[dict]]:
    keys = sorted({p.stem.split("__", 1)[1] for t in TABLES for p in data.glob(f"{t}__*.parquet")})
    accepted, excluded = [], []
    for key in keys:
        md, reason = source_metadata(data, key)
        if reason:
            excluded.append(
                {
                    "key": key,
                    "source": source_identity(os.fsdecode(md.get(b"ta.source", b""))),
                    "reason": reason,
                }
            )
        else:
            accepted.append(key)
    return accepted, excluded


def _table_path(data: Path, table: str, key: str) -> Path:
    return data / f"{table}__{key}.parquet"


def is_current(data: Path, key: str, fp: dict) -> bool:
    md, reason = source_metadata(data, key)
    return reason is None and md.get(b"ta.fp") == json.dumps(fp).encode()


_utc_timestamp = utc_timestamp


def _projects_root(data, projects, *, adopt=False):
    path = data / "projects-root.json"
    root = str(projects.resolve(strict=True))
    try:
        saved = json.loads(path.read_text())["projects_root"]
    except FileNotFoundError:
        if any(data.glob(f"{MARKER}__*.parquet")) and not adopt:
            raise ValueError(
                "store has no projects root; ingest with --adopt-projects-root after verifying its origin"
            ) from None
        saved = None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("store projects-root record is unreadable") from exc
    if saved is not None and saved != root and not adopt:
        raise ValueError("projects root differs from the store; explicit root adoption required")
    if saved != root:
        temporary = data / ".projects-root.tmp"
        temporary.write_text(json.dumps({"projects_root": root}))
        os.replace(temporary, path)


def _to_table(rows: list[dict], table: str, meta: dict) -> pa.Table:
    # The extractor returns normalized rows; repeating identity transport would
    # escape the reserved prefixes twice and break raw evidence addressing.
    return pa.Table.from_pylist(rows, schema=SCHEMAS[table]).replace_schema_metadata(meta)


def _rename_candidates(data, projects):
    """Index absent paths once per ingest, never once per discovered source."""
    if projects is None:
        return {}
    paths = {}
    for table in _PUBLISH_ORDER[::-1]:
        for path in data.glob(f"{table}__*.parquet"):
            paths.setdefault(path.stem.split("__", 1)[1], path)
    candidates = {}
    for key, path in paths.items():
        try:
            md = pq.read_metadata(path).metadata or {}
            previous = json.loads(md[b"ta.fp"])
            old_rel = os.fsdecode(md[b"ta.source"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if (
            not isinstance(previous, dict)
            or key != srckey(old_rel)
            or (projects / old_rel).exists()
            or not md.get(b"ta.content_sha256")
        ):
            continue
        identity = tuple(previous.get(k) for k in ("dev", "ino", "size", "mtime_ns"))
        if any(type(v) is not int for v in identity):
            continue
        candidates.setdefault(identity, []).append((key, old_rel, md[b"ta.content_sha256"]))
    return candidates


def _rename_keys(candidates, data, rel, fp, content_digest):
    """Absent old path + exact inode fingerprint + exact captured-byte digest.

    Existing hardlinks are independent sources; legacy rows without a digest
    and reused inodes with changed bytes are not rename proof and are retained.
    """
    identity = tuple(fp[k] for k in ("dev", "ino", "size", "mtime_ns"))
    return [
        key
        for key, old_rel, digest in candidates.get(identity, [])
        if old_rel != rel
        and digest == content_digest.encode()
        and any(_table_path(data, table, key).exists() for table in TABLES)
    ]


def _invalidate_snapshot(data):
    (data / "derived/current").unlink(missing_ok=True)


def _retire_sources(data, keys):
    # Marker first: a killed migration cannot leave two compatible identities.
    for key in keys:
        for table in _PUBLISH_ORDER[::-1]:
            _table_path(data, table, key).unlink(missing_ok=True)


def build_source(
    path: Path,
    rel: str,
    data: Path,
    st: os.stat_result,
    rename_candidates: dict | None = None,
) -> dict:
    """Extract one source and publish all of its tables, marker last."""
    fp = source_fingerprint(path, st)
    res = extract_source(path, rel, stop_at=st.st_size)
    if source_fingerprint(path).get("meta") != fp.get("meta"):
        raise ValueError("agent metadata changed during extraction; retry required")
    key = srckey(rel)
    staging = data / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    meta = {
        b"ta.source": os.fsencode(rel),
        b"ta.fp": json.dumps(fp).encode(),
        b"ta.schema": SCHEMA_VERSION.encode(),
        **semantics(),
        b"ta.generation": uuid.uuid4().hex.encode(),
        b"ta.ingested_at": datetime.now(UTC).isoformat().encode(),
        b"ta.last_ts": (
            res.last_timestamp if res.chronology_known and res.last_timestamp else ""
        ).encode(),
        b"ta.content_sha256": res.content_sha256.encode(),
        b"ta.stats": json.dumps(res.stats).encode(),
    }
    tmp_paths = {}
    try:
        for table in _PUBLISH_ORDER:
            tmp = staging / f"{table}__{key}.{os.getpid()}.tmp"
            pq.write_table(_to_table(res.tables[table], table, meta), tmp, compression="zstd")
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            tmp_paths[table] = tmp
        retired = _rename_keys(
            rename_candidates or {},
            data,
            rel,
            fp,
            res.content_sha256,
        )
        with publication(exclusive=True) if retired else contextlib.nullcontext():
            if retired:
                _invalidate_snapshot(data)
                # Keep old table bodies until replacement commits. Marker
                # removal excludes them during a crash; retry cleans leftovers.
                for old_key in retired:
                    _table_path(data, MARKER, old_key).unlink(missing_ok=True)
            for table in _PUBLISH_ORDER:  # marker last
                os.replace(tmp_paths[table], _table_path(data, table, key))
            _retire_sources(data, retired)
    finally:
        for tmp in tmp_paths.values():
            if tmp.exists():
                tmp.unlink()
    return res.stats


def _sweep_staging(data: Path) -> None:
    staging = data / ".staging"
    if not staging.is_dir():
        return
    cutoff = time.time() - _STAGING_MAX_AGE_S
    for p in staging.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except FileNotFoundError:
            pass


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
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
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
    with _locked(lock_path or DEFAULT_LOCK):
        _projects_root(data, projects, adopt=adopt_projects_root)
        _sweep_staging(data)
        sources = discover(projects)
        rename_candidates = _rename_candidates(data, projects)
        for path, rel in sources:
            try:
                st = os.stat(path)
            except FileNotFoundError:
                summary["vanished"] += 1
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                summary["skipped_old"] += 1
                continue
            summary["sources"] += 1
            try:
                fp = source_fingerprint(path, st)
                # A possible rename must be checked against newly captured
                # bytes, even if a destination already has equal metadata.
                # Cached destination digests cannot rule out same-metadata edits.
                identity = tuple(fp[k] for k in ("dev", "ino", "size", "mtime_ns"))
                possible_rename = any(
                    old_rel != rel for _, old_rel, _ in rename_candidates.get(identity, [])
                )
                if not possible_rename and is_current(data, srckey(rel), fp):
                    summary["unchanged"] += 1
                    continue
                stats = build_source(path, rel, data, st, rename_candidates=rename_candidates)
            except FileNotFoundError:  # removed between stat and read
                summary["vanished"] += 1
                summary["sources"] -= 1
                continue
            except Exception as e:  # noqa: BLE001 - one bad source must not stop the run (review SF-4)
                summary["failed"] += 1
                summary["failed_sources"].append(rel)
                print(f"transcript-analytics: FAILED {rel}: {e!r}", file=sys.stderr)
                continue
            summary["rebuilt"] += 1
            for k in ("lines", "malformed", "bytes_read"):
                summary[k] += stats[k]
        inventory = {
            "discovered": len(sources),
            "since_days": since_days,
            "unbuilt": [
                rel for _, rel in sources if not _table_path(data, MARKER, srckey(rel)).exists()
            ],
            "failed_sources": summary["failed_sources"],
        }
        inventory_path = data / "inventory.json"
        encoded = json.dumps(inventory, sort_keys=True)
        if not inventory_path.exists() or inventory_path.read_text() != encoded:
            temporary = data / ".staging/inventory.json"
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(encoded)
            os.replace(temporary, inventory_path)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


def inventory(data):
    try:
        return json.loads((data / "inventory.json").read_text())
    except (OSError, ValueError):
        return {"unavailable": "no source discovery inventory"}


def _prunable_key(marker, projects, cutoff):
    try:
        md = pq.read_metadata(marker).metadata or {}
        rel = os.fsdecode(md.get(b"ta.source", b""))
        last = _utc_timestamp(md.get(b"ta.last_ts", b"").decode())
    except (OSError, ValueError):
        print(f"prune: unreadable marker retained: {marker.name}", file=sys.stderr)
        return None
    if not rel or (projects / rel).exists():
        return None
    if last is None:
        print(f"prune: unknown retention timestamp retained: {rel}", file=sys.stderr)
        return None
    if last >= cutoff:
        return None
    return marker.name[len(MARKER) + 2 : -len(".parquet")]


def prune(data: Path, *, before: str, projects: Path, lock_path: Path | None = None) -> int:
    """Delete the files of sources whose transcript is GONE and whose last
    record is older than ``before`` (ISO date). A source whose transcript
    still exists is kept: the next ingest would only rebuild it.

    Refuses (ValueError) a malformed date, and a projects directory that is
    missing or differs from the bound root. An empty bound root is valid."""
    cutoff = datetime.combine(date.fromisoformat(before), datetime.min.time(), tzinfo=UTC)
    if not projects.is_dir():
        raise ValueError(f"projects directory missing: {projects}")
    removed = 0
    with _locked(lock_path or DEFAULT_LOCK), publication(exclusive=True):
        # Pruning is read-only with respect to root binding: never adopt implicitly.
        try:
            root = json.loads((data / "projects-root.json").read_text())["projects_root"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                "store projects root is unbound or unreadable; ingest with explicit adoption first"
            ) from exc
        if root != str(projects.resolve(strict=True)):
            raise ValueError("projects root differs from the store")
        invalidated = False
        for marker in data.glob(f"{MARKER}__*.parquet"):
            key = _prunable_key(marker, projects, cutoff)
            if key is None:
                continue
            if not invalidated:
                _invalidate_snapshot(data)
                invalidated = True
            for table in _PUBLISH_ORDER[::-1]:
                _table_path(data, table, key).unlink(missing_ok=True)
            removed += 1
    return removed
