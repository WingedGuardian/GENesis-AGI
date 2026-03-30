"""Test that connectors satisfy the DataConnector protocol."""

from genesis.providers.data.csv_connector import CSVConnector
from genesis.providers.data.protocol import DataConnector


def test_csv_connector_is_data_connector(tmp_path):
    connector = CSVConnector(base_dir=tmp_path)
    assert isinstance(connector, DataConnector)


def test_csv_connector_has_name(tmp_path):
    connector = CSVConnector(base_dir=tmp_path)
    assert connector.name == "csv"
