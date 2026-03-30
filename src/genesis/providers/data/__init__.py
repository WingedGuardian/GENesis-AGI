"""Structured data connector framework for Genesis providers."""

from genesis.providers.data.csv_connector import CSVConnector
from genesis.providers.data.protocol import DataConnector
from genesis.providers.data.sheets_connector import GoogleSheetsConnector
from genesis.providers.data.types import (
    DataSource,
    RowFilter,
    RowUpdate,
    TabularData,
    WriteResult,
)

__all__ = [
    "CSVConnector",
    "DataConnector",
    "DataSource",
    "GoogleSheetsConnector",
    "RowFilter",
    "RowUpdate",
    "TabularData",
    "WriteResult",
]
