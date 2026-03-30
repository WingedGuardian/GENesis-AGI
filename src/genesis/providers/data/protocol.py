"""DataConnector protocol — the universal interface for structured data access."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from genesis.providers.data.types import (
    DataSource,
    RowFilter,
    RowUpdate,
    TabularData,
    WriteResult,
)


@runtime_checkable
class DataConnector(Protocol):
    """Any structured data source that Genesis can read from and write to."""

    name: str

    async def read_rows(
        self,
        source: str,
        filters: list[RowFilter] | None = None,
        limit: int = 100,
    ) -> TabularData: ...

    async def write_rows(self, source: str, rows: list[dict]) -> WriteResult: ...

    async def update_rows(
        self, source: str, updates: list[RowUpdate]
    ) -> WriteResult: ...

    async def list_sources(self) -> list[DataSource]: ...

    async def check_health(self) -> bool: ...
