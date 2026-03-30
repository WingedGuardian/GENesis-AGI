"""Data types for the structured data connector framework."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TabularData:
    """Tabular data returned from a data source."""

    headers: list[str]
    rows: list[dict]
    source_name: str
    row_count: int


@dataclass
class RowFilter:
    """Filter criterion for querying rows."""

    column: str
    operator: str  # eq, ne, gt, lt, contains
    value: str


@dataclass
class RowUpdate:
    """Specification for updating a single cell."""

    row_index: int
    column: str
    new_value: str


@dataclass
class WriteResult:
    """Result of a write or update operation."""

    rows_affected: int
    success: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class DataSource:
    """Metadata about an available data source."""

    name: str
    source_type: str
    row_count: int
    last_modified: str
