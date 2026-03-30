"""Tests for CSVConnector."""

import pytest

from genesis.providers.data.csv_connector import CSVConnector
from genesis.providers.data.types import RowFilter, RowUpdate


@pytest.fixture
def csv_dir(tmp_path):
    """Create a temp dir with a sample CSV."""
    csv_file = tmp_path / "people.csv"
    csv_file.write_text("name,age,city\nAlice,30,NYC\nBob,25,LA\nCharlie,35,NYC\n")
    return tmp_path


@pytest.fixture
def connector(csv_dir):
    return CSVConnector(base_dir=csv_dir)


@pytest.mark.asyncio
async def test_read_rows_no_filters(connector):
    result = await connector.read_rows("people")
    assert result.headers == ["name", "age", "city"]
    assert result.row_count == 3
    assert result.rows[0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_read_rows_with_eq_filter(connector):
    filters = [RowFilter(column="city", operator="eq", value="NYC")]
    result = await connector.read_rows("people", filters=filters)
    assert result.row_count == 2
    assert all(r["city"] == "NYC" for r in result.rows)


@pytest.mark.asyncio
async def test_read_rows_with_ne_filter(connector):
    filters = [RowFilter(column="city", operator="ne", value="NYC")]
    result = await connector.read_rows("people", filters=filters)
    assert result.row_count == 1
    assert result.rows[0]["name"] == "Bob"


@pytest.mark.asyncio
async def test_read_rows_with_gt_filter(connector):
    filters = [RowFilter(column="age", operator="gt", value="28")]
    result = await connector.read_rows("people", filters=filters)
    assert result.row_count == 2


@pytest.mark.asyncio
async def test_read_rows_with_lt_filter(connector):
    filters = [RowFilter(column="age", operator="lt", value="30")]
    result = await connector.read_rows("people", filters=filters)
    assert result.row_count == 1
    assert result.rows[0]["name"] == "Bob"


@pytest.mark.asyncio
async def test_read_rows_with_contains_filter(connector):
    filters = [RowFilter(column="name", operator="contains", value="li")]
    result = await connector.read_rows("people", filters=filters)
    assert result.row_count == 2  # Alice, Charlie


@pytest.mark.asyncio
async def test_read_rows_with_limit(connector):
    result = await connector.read_rows("people", limit=2)
    assert result.row_count == 2


@pytest.mark.asyncio
async def test_read_rows_missing_source(connector):
    result = await connector.read_rows("nonexistent")
    assert result.row_count == 0
    assert result.rows == []


@pytest.mark.asyncio
async def test_write_rows_appends(connector, csv_dir):
    new_rows = [{"name": "Diana", "age": "28", "city": "SF"}]
    result = await connector.write_rows("people", new_rows)
    assert result.success
    assert result.rows_affected == 1

    data = await connector.read_rows("people")
    assert data.row_count == 4
    assert data.rows[-1]["name"] == "Diana"


@pytest.mark.asyncio
async def test_write_rows_creates_new_file(connector, csv_dir):
    new_rows = [{"col_a": "x", "col_b": "y"}]
    result = await connector.write_rows("new_file", new_rows)
    assert result.success
    assert (csv_dir / "new_file.csv").exists()


@pytest.mark.asyncio
async def test_write_rows_empty(connector):
    result = await connector.write_rows("people", [])
    assert result.success
    assert result.rows_affected == 0


@pytest.mark.asyncio
async def test_update_rows(connector):
    updates = [RowUpdate(row_index=0, column="city", new_value="Boston")]
    result = await connector.update_rows("people", updates)
    assert result.success
    assert result.rows_affected == 1

    data = await connector.read_rows("people")
    assert data.rows[0]["city"] == "Boston"


@pytest.mark.asyncio
async def test_update_rows_invalid_index(connector):
    updates = [RowUpdate(row_index=99, column="city", new_value="X")]
    result = await connector.update_rows("people", updates)
    assert not result.success
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_update_rows_invalid_column(connector):
    updates = [RowUpdate(row_index=0, column="nonexistent", new_value="X")]
    result = await connector.update_rows("people", updates)
    assert not result.success
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_update_rows_missing_source(connector):
    updates = [RowUpdate(row_index=0, column="name", new_value="X")]
    result = await connector.update_rows("nonexistent", updates)
    assert not result.success


@pytest.mark.asyncio
async def test_list_sources(connector):
    sources = await connector.list_sources()
    assert len(sources) == 1
    assert sources[0].name == "people"
    assert sources[0].source_type == "csv"
    assert sources[0].row_count == 3


@pytest.mark.asyncio
async def test_check_health(connector):
    assert await connector.check_health() is True


@pytest.mark.asyncio
async def test_check_health_missing_dir(tmp_path):
    connector = CSVConnector(base_dir=tmp_path / "nonexistent")
    assert await connector.check_health() is False
