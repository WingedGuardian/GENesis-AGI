"""Strict, stdlib-only source publication selector shared with archive tooling.

This validates publication structure, not Parquet contents or row semantics.
An absent selector permits legacy reads only before the public sources namespace
exists. Readers retain one immutable selector throughout an operation.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

FORMAT = "ta-source-catalog-v1"
FILENAME = "source-catalog.json"
_KEY = re.compile(r"[0-9a-f]{16}\Z")
_REVISION = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = frozenset(
    ("format", "revision", "population_revision", "projects_root", "sources", "collection")
)
_COLLECTION_FIELDS = frozenset(
    (
        "run_id",
        "started_at",
        "updated_at",
        "completed_at",
        "complete",
        "discovery_complete",
        "discovered",
        "visited",
        "since_days",
        "rebuilt_committed",
        "unchanged",
        "skipped_old",
        "unbuilt",
        "failed_sources",
    )
)


class Unavailable(ValueError):
    """The authoritative source selector cannot be used; never glob-fallback."""


@dataclass(frozen=True)
class Catalog:
    revision: str
    population_revision: str
    projects_root: str
    sources: Mapping[str, str]
    collection: Mapping[str, object]

    def document(self) -> dict:
        """Independent mutable document for a writer's next candidate."""
        collection = dict(self.collection)
        for field in ("unbuilt", "failed_sources"):
            if collection[field] is not None:
                collection[field] = list(collection[field])
        return {
            "format": FORMAT,
            "revision": self.revision,
            "population_revision": self.population_revision,
            "projects_root": self.projects_root,
            "sources": dict(self.sources),
            "collection": collection,
        }


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise Unavailable("duplicate catalog key")
        result[key] = value
    return result


def _require(condition, message):
    if not condition:
        raise Unavailable(message)


def _timestamp(value):
    _require(isinstance(value, str), "catalog timestamp must be UTC text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Unavailable("invalid catalog timestamp") from exc
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed),
        "catalog timestamp must be UTC",
    )


def _time_window(value):
    if value is None:
        return True
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _collection(value):
    _require(
        type(value) is dict and value.keys() == _COLLECTION_FIELDS, "invalid collection fields"
    )
    _require(
        isinstance(value["run_id"], str) and _REVISION.fullmatch(value["run_id"]),
        "invalid collection run identifier",
    )
    for field in ("complete", "discovery_complete"):
        _require(type(value[field]) is bool, "collection flags must be booleans")
    for field in ("started_at", "updated_at"):
        _timestamp(value[field])
    if value["completed_at"] is not None:
        _timestamp(value["completed_at"])
    _require(
        (value["completed_at"] is not None) == value["complete"],
        "collection completion timestamp disagrees",
    )
    _require(
        not value["complete"] or value["discovery_complete"],
        "completed collection requires discovery",
    )
    for field in ("visited", "rebuilt_committed", "unchanged", "skipped_old"):
        _require(
            type(value[field]) is int and value[field] >= 0,
            "collection counts must be nonnegative integers",
        )
    discovered = value["discovered"]
    _require(
        (type(discovered) is int and discovered >= 0)
        if value["discovery_complete"]
        else discovered is None,
        "discovery count must reflect discovery state",
    )
    since = value["since_days"]
    _require(_time_window(since), "invalid collection time window")
    result = dict(value)
    for field in ("unbuilt", "failed_sources"):
        items = value[field]
        if field == "unbuilt" and not value["discovery_complete"]:
            _require(items is None, "undiscovered source population must be unknown")
        else:
            _require(
                type(items) is list and all(isinstance(item, str) for item in items),
                "invalid collection source identities",
            )
            result[field] = tuple(items)
    return MappingProxyType(result)


def parse(text: str | bytes) -> Catalog:
    """Parse one selector without importing the analytics execution stack."""
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Unavailable("source catalog is unreadable") from exc
    _require(type(value) is dict and value.keys() == _FIELDS, "invalid catalog fields")
    _require(value["format"] == FORMAT, "unsupported source catalog format")
    for field in ("revision", "population_revision"):
        _require(
            isinstance(value[field], str) and _REVISION.fullmatch(value[field]),
            "invalid catalog revision",
        )
    root = value["projects_root"]
    _require(
        isinstance(root, str)
        and root.startswith("/")
        and "\0" not in root
        and os.path.normpath(root) == root,
        "invalid projects-root binding",
    )
    sources = value["sources"]
    _require(type(sources) is dict, "invalid catalog source map")
    for key, generation in sources.items():
        _require(
            _KEY.fullmatch(key)
            and isinstance(generation, str)
            and (generation == "legacy" or _REVISION.fullmatch(generation)),
            "invalid catalog source reference",
        )
    return Catalog(
        value["revision"],
        value["population_revision"],
        root,
        MappingProxyType(sources),
        _collection(value["collection"]),
    )


def load(data: Path) -> Catalog | None:
    """Load authoritative selection, or None for a genuinely legacy store."""
    try:
        text = (data / FILENAME).read_bytes()
    except FileNotFoundError:
        if (data / "sources").exists() or (data / "sources").is_symlink():
            raise Unavailable("source namespace exists without its catalog") from None
        return None
    except OSError as exc:
        raise Unavailable("source catalog cannot be read") from exc
    return parse(text)
