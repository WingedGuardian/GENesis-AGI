"""Immutable all-six Parquet generations selected by one atomic catalog.

The catalog binds the origin, source population and collection progress.
Legacy flat bodies remain immutable until individually migrated. Every writer
holds the writer lease; retirement additionally fences the selector under the
exclusive publication lease. Failed refreshes retain the accepted generation.
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
from genesis.transcript_analytics import catalog, scrub
from genesis.transcript_analytics import publication as source_publication
from genesis.transcript_analytics.extract import TABLES, extract_source, utc_timestamp
from genesis.transcript_analytics.identity import source_identity
from genesis.transcript_analytics.identity import source_path as source_path
from genesis.transcript_analytics.locks import publication
from genesis.transcript_analytics.schema import SCHEMA_VERSION, SCHEMAS

MARKER = "fragments"


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
_READ_CATALOG = object()
DEFAULT_LOCK = genesis_home() / "locks" / "transcript-analytics.lock"


class Busy(RuntimeError):
    """Another ingest/prune holds the lock."""


class ScrubberUnavailable(RuntimeError):
    """The secret scrubber cannot be loaded; refusing to write anything."""


class SourceIdentityConflict(ValueError):
    """A placement key cannot select two distinct full source identities."""


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


def _selection(data, selected):
    return catalog.load(data) if selected is _READ_CATALOG else selected


def source_keys(data, selected=_READ_CATALOG):
    selected = _selection(data, selected)
    if selected is not None:
        return sorted(selected.sources)
    return sorted(
        key
        for key in {p.stem.split("__", 1)[1] for t in TABLES for p in data.glob(f"{t}__*.parquet")}
        if catalog._KEY.fullmatch(key)
    )


def source_metadata(data: Path, key: str, selected=_READ_CATALOG) -> tuple[dict, str | None]:
    """Validate the entire source commit, including empty tables."""
    selected = _selection(data, selected)
    try:
        marker = pq.read_metadata(_table_path(data, MARKER, key, selected)).metadata or {}
    except (OSError, ValueError, KeyError):
        marker = {}
    try:
        footers = [pq.read_metadata(_table_path(data, t, key, selected)) for t in TABLES]
        records = [footer.metadata or {} for footer in footers]
    except (OSError, ValueError, KeyError):
        return marker, "missing or unreadable table"
    # The canonical marker retains captured-byte rename proof authority.
    # Reading another table's digest must not replace a missing marker proof.
    first = records[TABLES.index(MARKER)]
    if any(
        not footer.schema.to_arrow_schema().equals(SCHEMAS[table], check_metadata=False)
        for table, footer in zip(TABLES, footers, strict=True)
    ):
        return first, "incompatible table schema"
    if any(not semantics_compatible(md) for md in records):
        return first, "incompatible semantics"
    if not first.get(b"ta.generation") or any(
        any(
            md.get(k) != first.get(k)
            for k in (
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
        return first, "mixed generation"
    if srckey(os.fsdecode(first.get(b"ta.source", b""))) != key:
        return first, "source identity conflict"
    if (
        selected is not None
        and selected.sources[key] != "legacy"
        and first.get(b"ta.generation") != selected.sources[key].encode()
    ):
        return first, "selected generation mismatch"
    return first, None


def compatible_sources(data: Path, selected=_READ_CATALOG) -> tuple[list[str], list[dict]]:
    selected = _selection(data, selected)
    keys = source_keys(data, selected)
    accepted, excluded = [], []
    for key in keys:
        md, reason = source_metadata(data, key, selected)
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


def _table_path(data: Path, table: str, key: str, selected=_READ_CATALOG) -> Path:
    selected = _selection(data, selected)
    if selected is not None:
        generation = selected.sources[key]
        if generation != "legacy":
            return data / "sources" / key / generation / f"{table}.parquet"
    return data / f"{table}__{key}.parquet"


def is_current(data: Path, key: str, fp: dict, selected=_READ_CATALOG, *, source=None) -> bool:
    selected = _selection(data, selected)
    md, reason = source_metadata(data, key, selected)
    return (
        reason is None
        and (source is None or md.get(b"ta.source") == os.fsencode(source))
        and md.get(b"ta.fp") == json.dumps(fp).encode()
    )


_utc_timestamp = utc_timestamp


def _projects_root(data, projects, *, adopt=False):
    path = data / "projects-root.json"
    root = str(projects.resolve(strict=True))
    selected = catalog.load(data)
    try:
        saved = (
            selected.projects_root
            if selected is not None
            else json.loads(path.read_text())["projects_root"]
        )
    except FileNotFoundError:
        if source_keys(data, None) and not adopt:
            raise ValueError(
                "store has no projects root; ingest with --adopt-projects-root after verifying its origin"
            ) from None
        saved = None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("store projects-root record is unreadable") from exc
    if saved is not None and saved != root and not adopt:
        raise ValueError("projects root differs from the store; explicit root adoption required")
    return root


class _Publisher:
    """One run's pending map, first-success commit, and bounded checkpoints."""

    def __init__(self, data, projects, adopt, since_days=None):
        self.data = data
        selected = catalog.load(data)
        root = _projects_root(data, projects, adopt=adopt)
        now = datetime.now(UTC).isoformat()
        self.document = (
            selected.document()
            if selected is not None
            else {
                "format": catalog.FORMAT,
                "revision": uuid.uuid4().hex,
                "population_revision": uuid.uuid4().hex,
                "projects_root": root,
                "sources": {key: "legacy" for key in source_keys(data, None)},
            }
        )
        self.selected = selected
        self.document["projects_root"] = root
        self.document["collection"] = {
            "run_id": uuid.uuid4().hex,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "complete": False,
            "discovery_complete": False,
            "discovered": None,
            "visited": 0,
            "since_days": since_days,
            "rebuilt_committed": 0,
            "unchanged": 0,
            "skipped_old": 0,
            "unbuilt": None,
            "failed_sources": [],
        }
        self.pending = 0
        self.rebuilt = 0
        self.committed_rebuilt = 0
        self.unbuilt = None
        self.first_progress = True
        self.last_checkpoint = time.monotonic()
        self.checkpoint()

    def annotate_failure(self, failure):
        failure.staged_unpublished = self.pending
        failure.rebuilt_staged = self.rebuilt
        failure.rebuilt_durably_committed = self.committed_rebuilt
        failure.last_durable_revision = (
            self.selected.revision if self.selected is not None else None
        )

    @contextlib.contextmanager
    def failure_progress(self):
        """Attribute fatal publication failures throughout the writer lifetime."""
        try:
            yield
        except source_publication.Failed as failure:
            self.annotate_failure(failure)
            raise

    def checkpoint(self):
        self.document["revision"] = uuid.uuid4().hex
        if self.selected is not None and (
            self.document["sources"] != dict(self.selected.sources)
            or self.document["projects_root"] != self.selected.projects_root
        ):
            self.document["population_revision"] = uuid.uuid4().hex
        collection = self.document["collection"]
        collection["unbuilt"] = sorted(self.unbuilt) if self.unbuilt is not None else None
        collection["updated_at"] = datetime.now(UTC).isoformat()
        collection["rebuilt_committed"] = self.rebuilt
        try:
            self.selected = source_publication.publish(self.data, json.dumps(self.document))
        except source_publication.Failed as failure:
            self.annotate_failure(failure)
            raise
        self.committed_rebuilt = self.rebuilt
        self.pending = 0
        self.last_checkpoint = time.monotonic()

    def due(self):
        if self.pending and (
            self.first_progress
            or self.pending >= 128
            or time.monotonic() - self.last_checkpoint >= 30
        ):
            self.checkpoint()
            self.first_progress = False

    def select(self, key, generation, *, rebuilt):
        self.document["sources"][key] = generation
        self.pending += 1
        self.rebuilt += int(rebuilt)

    def selection(self):
        # Writer-only tentative selection: never serialize the entire map for
        # each source. Public readers use a separately loaded immutable Catalog.
        return self

    @property
    def sources(self):
        return self.document["sources"]


def _to_table(rows: list[dict], table: str, meta: dict) -> pa.Table:
    # The extractor returns normalized rows; repeating identity transport would
    # escape the reserved prefixes twice and break raw evidence addressing.
    return pa.Table.from_pylist(rows, schema=SCHEMAS[table]).replace_schema_metadata(meta)


def _stage_tables(data, key, meta, table_reader):
    generation = meta[b"ta.generation"].decode()
    staging = data / ".staging" / "sources" / generation
    source_publication.make_directory(staging)
    # Failed stages remain for fenced collect; preserve every primary exception.
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
    source_publication.finalize(data, staging, key, generation)
    return generation


def _migrate_legacy(data, key, publisher):
    selected = publisher.selection()
    meta, reason = source_metadata(data, key, selected)
    if reason:
        return False
    metadata = {field: value for field, value in meta.items() if field != b"ARROW:schema"}
    metadata[b"ta.generation"] = uuid.uuid4().hex.encode()
    generation = _stage_tables(
        data, key, metadata, lambda table: pq.read_table(_table_path(data, table, key, selected))
    )
    publisher.select(key, generation, rebuilt=False)
    return True


def build_source(
    path: Path,
    rel: str,
    data: Path,
    st: os.stat_result,
    *,
    _publisher=None,
) -> dict:
    """Extract one source into an immutable generation; never overwrite bodies."""
    standalone = _publisher is None
    if standalone:
        with _locked(DEFAULT_LOCK):
            publisher = _standalone_publisher(path, rel, data)
            with publisher.failure_progress():
                stats = build_source(path, rel, data, st, _publisher=publisher)
                publisher.checkpoint()
                return stats
    fp = source_fingerprint(path, st)
    res = extract_source(path, rel, stop_at=st.st_size)
    if source_fingerprint(path).get("meta") != fp.get("meta"):
        raise ValueError("agent metadata changed during extraction; retry required")
    key = srckey(rel)
    if key in _publisher.sources:
        previous, _reason = source_metadata(data, key, _publisher.selection())
        previous_source = previous.get(b"ta.source")
        if previous_source is None or previous_source != os.fsencode(rel):
            raise SourceIdentityConflict("source key identity conflict; accepted history retained")
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
    generation = _stage_tables(
        data, key, meta, lambda table: _to_table(res.tables[table], table, meta)
    )
    _publisher.select(key, generation, rebuilt=True)
    return res.stats


def _standalone_publisher(path, rel, data):
    """Resolve the origin only after acquiring the standalone writer lease."""
    source_publication.make_directory(data)
    selected = catalog.load(data)
    if selected is not None:
        projects = Path(selected.projects_root)
    else:
        try:
            projects = Path(json.loads((data / "projects-root.json").read_text())["projects_root"])
        except FileNotFoundError:
            projects = path
            for _part in Path(rel).parts:
                projects = projects.parent
    return _Publisher(data, projects, False)


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
    with _locked(lock_path or DEFAULT_LOCK):
        source_publication.make_directory(data)
        os.chmod(data, 0o700)
        publisher = _Publisher(data, projects, adopt_projects_root, since_days)
        with publisher.failure_progress():
            sources = discover(projects)
            collection = publisher.document["collection"]
            collection.update(discovery_complete=True, discovered=len(sources))
            publisher.unbuilt = {
                source_identity(rel) for _, rel in sources if srckey(rel) not in publisher.sources
            }
            publisher.checkpoint()
            summary["migrated"] = 0
            for path, rel in sources:
                publisher.due()
                collection["visited"] += 1
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    summary["vanished"] += 1
                    continue
                key = srckey(rel)
                if cutoff is not None and st.st_mtime < cutoff and key not in publisher.sources:
                    summary["skipped_old"] += 1
                    collection["skipped_old"] = summary["skipped_old"]
                    continue
                summary["sources"] += 1
                try:
                    fp = source_fingerprint(path, st)
                    if is_current(
                        data, key, fp, publisher.selection(), source=rel
                    ):
                        summary["unchanged"] += 1
                        collection["unchanged"] = summary["unchanged"]
                        if publisher.sources.get(key) == "legacy" and _migrate_legacy(
                            data, key, publisher
                        ):
                            summary["migrated"] += 1
                        publisher.due()
                        continue
                    stats = build_source(
                        path,
                        rel,
                        data,
                        st,
                        _publisher=publisher,
                    )
                except source_publication.Failed:
                    # A selector failure (especially uncertain post-replace fsync)
                    # is fatal. Never continue or authorize cleanup from visibility.
                    raise
                except FileNotFoundError:
                    summary["vanished"] += 1
                    summary["sources"] -= 1
                    continue
                except Exception as e:  # noqa: BLE001 - isolate source extraction failures
                    summary["failed"] += 1
                    summary["failed_sources"].append(rel)
                    collection["failed_sources"].append(source_identity(rel))
                    if isinstance(e, SourceIdentityConflict):
                        publisher.unbuilt.add(source_identity(rel))
                    print(f"transcript-analytics: FAILED {rel}: {e!r}", file=sys.stderr)
                    continue
                summary["rebuilt"] += 1
                publisher.unbuilt.discard(source_identity(rel))
                for k in ("lines", "malformed", "bytes_read"):
                    summary[k] += stats[k]
                publisher.due()
            # Compatible retained legacy history can migrate without its original
            # transcript. Preserve predecessor semantic stamps; never re-extract.
            for key in list(publisher.sources):
                if publisher.sources[key] != "legacy":
                    continue
                try:
                    if _migrate_legacy(data, key, publisher):
                        summary["migrated"] += 1
                        publisher.due()
                except source_publication.Failed:
                    raise
                except (OSError, ValueError):
                    metadata, _reason = source_metadata(data, key, publisher.selection())
                    identity = source_identity(os.fsdecode(metadata.get(b"ta.source", b"")))
                    summary["failed"] += 1
                    summary["failed_sources"].append(identity)
                    collection["failed_sources"].append(identity)
            collection["complete"] = not summary["failed"] and not summary["vanished"]
            collection["completed_at"] = (
                datetime.now(UTC).isoformat() if collection["complete"] else None
            )
            publisher.checkpoint()
            summary["rebuilt_committed"] = publisher.selected.collection["rebuilt_committed"]
            summary["catalog_revision"] = publisher.selected.revision
            with publication(exclusive=True):
                source_publication.collect(data, TABLES)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


def inventory(data, selected=_READ_CATALOG):
    selected = _selection(data, selected)
    if selected is not None:
        return selected.document()["collection"]
    try:
        return json.loads((data / "inventory.json").read_text())
    except (OSError, ValueError):
        return {"unavailable": "no source discovery inventory"}


def _prunable_key(marker, projects, cutoff, key=None):
    try:
        md = pq.read_metadata(marker).metadata or {}
        rel = os.fsdecode(md.get(b"ta.source", b""))
        last = _utc_timestamp(md.get(b"ta.last_ts", b"").decode())
    except (OSError, ValueError):
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
        selected = catalog.load(data)
        # Pruning is read-only with respect to root binding: never adopt implicitly.
        try:
            root = (
                selected.projects_root
                if selected is not None
                else json.loads((data / "projects-root.json").read_text())["projects_root"]
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                "store projects root is unbound or unreadable; ingest with explicit adoption first"
            ) from exc
        if root != str(projects.resolve(strict=True)):
            raise ValueError("projects root differs from the store")
        retired = []
        for key in source_keys(data, selected):
            marker = _table_path(data, MARKER, key, selected)
            key = _prunable_key(marker, projects, cutoff, key)
            if key is None:
                continue
            retired.append(key)
        if retired and selected is not None:
            document = selected.document()
            for key in retired:
                document["sources"].pop(key)
            document["revision"] = uuid.uuid4().hex
            document["population_revision"] = uuid.uuid4().hex
            source_publication.publish(data, json.dumps(document))
            source_publication.collect(data, TABLES)
        elif retired:
            # Bootstrap the immutable legacy population before retiring keys.
            publisher = _Publisher(data, projects, False)
            for key in retired:
                publisher.document["sources"].pop(key)
            publisher.checkpoint()
            source_publication.collect(data, TABLES)
        removed = len(retired)
    return removed
