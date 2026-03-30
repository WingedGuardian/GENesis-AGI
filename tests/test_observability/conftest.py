"""Shared fixtures for observability tests."""

import pytest
from aioresponses import aioresponses


@pytest.fixture
def aiohttp_mock():
    with aioresponses() as m:
        yield m
