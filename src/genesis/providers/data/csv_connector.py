"""CSV file connector for the structured data framework."""

from __future__ import annotations

import csv
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from genesis.providers.data.types import (
    DataSource,
    RowFilter,
    RowUpdate,
    TabularData,
    WriteResult,
)

logger = logging.getLogger(__name__)


class CSVConnector:
    """Read/write connector for CSV files in a directory."""

    name: str = "csv"

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def _resolve_path(self, source: str) -> Path:
        """Resolve source name to a CSV file path."""
        path = self.base_dir / source
        if not path.suffix:
            path = path.with_suffix(".csv")
        return path

    @staticmethod
    def _apply_filter(row: dict, f: RowFilter) -> bool:
        """Return True if row passes the filter."""
        val = row.get(f.column, "")
        match f.operator:
            case "eq":
                return str(val) == f.value
            case "ne":
                return str(val) != f.value
            case "gt":
                try:
                    return float(val) > float(f.value)
                except (ValueError, TypeError):
                    return False
            case "lt":
                try:
                    return float(val) < float(f.value)
                except (ValueError, TypeError):
                    return False
            case "contains":
                return f.value in str(val)
            case _:
                logger.warning("Unknown filter operator: %s", f.operator)
                return True

    async def read_rows(
        self,
        source: str,
        filters: list[RowFilter] | None = None,
        limit: int = 100,
    ) -> TabularData:
        """Read rows from a CSV file, applying optional filters and limit."""
        path = self._resolve_path(source)
        if not path.exists():
            return TabularData(headers=[], rows=[], source_name=source, row_count=0)

        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            rows: list[dict] = []
            for row in reader:
                if filters and not all(self._apply_filter(row, f) for f in filters):
                    continue
                rows.append(dict(row))
                if len(rows) >= limit:
                    break

        return TabularData(
            headers=list(headers),
            rows=rows,
            source_name=source,
            row_count=len(rows),
        )

    async def write_rows(self, source: str, rows: list[dict]) -> WriteResult:
        """Append rows to a CSV file, creating it if necessary."""
        if not rows:
            return WriteResult(rows_affected=0, success=True)

        path = self._resolve_path(source)
        errors: list[str] = []

        try:
            file_exists = path.exists()
            # Determine headers from existing file or new rows
            if file_exists:
                with open(path, newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    headers = reader.fieldnames or []
            else:
                headers = list(rows[0].keys())

            mode = "a" if file_exists else "w"
            with open(path, mode, newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=headers)
                if not file_exists:
                    writer.writeheader()
                for row in rows:
                    writer.writerow({h: row.get(h, "") for h in headers})

        except OSError as exc:
            logger.error("Failed to write CSV %s: %s", path, exc, exc_info=True)
            return WriteResult(rows_affected=0, success=False, errors=[str(exc)])

        return WriteResult(rows_affected=len(rows), success=True, errors=errors)

    async def update_rows(
        self, source: str, updates: list[RowUpdate]
    ) -> WriteResult:
        """Update specific cells in a CSV file by row index."""
        path = self._resolve_path(source)
        if not path.exists():
            return WriteResult(
                rows_affected=0,
                success=False,
                errors=[f"Source {source} not found"],
            )

        errors: list[str] = []
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                headers = list(reader.fieldnames or [])
                all_rows = list(reader)

            affected = set()
            for upd in updates:
                if upd.row_index < 0 or upd.row_index >= len(all_rows):
                    errors.append(
                        f"Row index {upd.row_index} out of range "
                        f"(0-{len(all_rows) - 1})"
                    )
                    continue
                if upd.column not in headers:
                    errors.append(f"Column {upd.column!r} not found")
                    continue
                all_rows[upd.row_index][upd.column] = upd.new_value
                affected.add(upd.row_index)

            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=headers)
                writer.writeheader()
                writer.writerows(all_rows)

        except OSError as exc:
            logger.error("Failed to update CSV %s: %s", path, exc, exc_info=True)
            return WriteResult(rows_affected=0, success=False, errors=[str(exc)])

        return WriteResult(
            rows_affected=len(affected),
            success=len(errors) == 0,
            errors=errors,
        )

    async def list_sources(self) -> list[DataSource]:
        """List all CSV files in the base directory."""
        if not self.base_dir.exists():
            return []

        sources: list[DataSource] = []
        for p in sorted(self.base_dir.glob("*.csv")):
            stat = p.stat()
            # Count rows (minus header)
            with open(p, newline="", encoding="utf-8") as fh:
                row_count = max(sum(1 for _ in fh) - 1, 0)
            modified = datetime.fromtimestamp(
                stat.st_mtime, tz=UTC
            ).isoformat()
            sources.append(
                DataSource(
                    name=p.stem,
                    source_type="csv",
                    row_count=row_count,
                    last_modified=modified,
                )
            )
        return sources

    async def check_health(self) -> bool:
        """Return True if the base directory exists and is accessible."""
        return self.base_dir.exists() and os.access(self.base_dir, os.R_OK)
