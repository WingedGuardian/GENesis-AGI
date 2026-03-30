"""Google Sheets connector stub for the structured data framework."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from genesis.providers.data.types import (
    DataSource,
    RowFilter,
    RowUpdate,
    TabularData,
    WriteResult,
)

logger = logging.getLogger(__name__)


class GoogleSheetsConnector:
    """Connector for Google Sheets API.

    Requires valid service account credentials. All methods raise
    ``NotImplementedError`` if credentials are not configured.
    """

    name: str = "google_sheets"

    def __init__(
        self,
        credentials_path: Path | None = None,
        http_client: Any = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.http_client = http_client
        self._configured = (
            credentials_path is not None and Path(credentials_path).exists()
        )

    def _require_configured(self) -> None:
        if not self._configured:
            raise NotImplementedError(
                "Google Sheets connector requires valid credentials. "
                "Provide a credentials_path pointing to a service account JSON file."
            )

    async def read_rows(
        self,
        source: str,
        filters: list[RowFilter] | None = None,
        limit: int = 100,
    ) -> TabularData:
        """Read rows from a Google Sheet."""
        self._require_configured()
        raise NotImplementedError(
            "Google Sheets read_rows not yet implemented. "
            "Configure credentials and install google-api-python-client."
        )

    async def write_rows(self, source: str, rows: list[dict]) -> WriteResult:
        """Write rows to a Google Sheet."""
        self._require_configured()
        raise NotImplementedError(
            "Google Sheets write_rows not yet implemented. "
            "Configure credentials and install google-api-python-client."
        )

    async def update_rows(
        self, source: str, updates: list[RowUpdate]
    ) -> WriteResult:
        """Update rows in a Google Sheet."""
        self._require_configured()
        raise NotImplementedError(
            "Google Sheets update_rows not yet implemented. "
            "Configure credentials and install google-api-python-client."
        )

    async def list_sources(self) -> list[DataSource]:
        """List available Google Sheets."""
        self._require_configured()
        raise NotImplementedError(
            "Google Sheets list_sources not yet implemented. "
            "Configure credentials and install google-api-python-client."
        )

    async def check_health(self) -> bool:
        """Return False if credentials are not configured."""
        return self._configured
