"""Tests for Qdrant collection delete guard."""

from unittest.mock import MagicMock

import pytest

from genesis.qdrant.collections import _PROTECTED_COLLECTIONS, delete_collection


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.delete_collection.return_value = True
    return client


def test_delete_protected_collection_blocked(mock_client):
    for name in _PROTECTED_COLLECTIONS:
        with pytest.raises(ValueError, match="Refusing to delete protected collection"):
            delete_collection(mock_client, name)
    mock_client.delete_collection.assert_not_called()


def test_delete_protected_collection_with_force(mock_client):
    for name in _PROTECTED_COLLECTIONS:
        result = delete_collection(mock_client, name, force=True)
        assert result is True
    assert mock_client.delete_collection.call_count == len(_PROTECTED_COLLECTIONS)


def test_delete_unprotected_collection_allowed(mock_client):
    result = delete_collection(mock_client, "test_genesis_abc123")
    assert result is True
    mock_client.delete_collection.assert_called_once_with("test_genesis_abc123")


def test_protected_collections_match_collections_list():
    from genesis.qdrant.collections import COLLECTIONS

    assert frozenset(COLLECTIONS) == _PROTECTED_COLLECTIONS
