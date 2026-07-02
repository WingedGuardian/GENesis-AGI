"""Shared fixtures for observability tests."""

import functools
from unittest.mock import MagicMock

import aiohttp
import pytest
from aioresponses import aioresponses


def _patch_client_response_init():
    """Patch ClientResponse.__init__ to accept stream_writer / writer kwargs.

    aioresponses (through its latest release, 0.7.9) constructs ClientResponse
    without the ``stream_writer`` (aiohttp >=3.10) / ``writer`` (aiohttp >=3.12)
    keyword-only arg that current aiohttp requires — so a bare ``aioresponses``
    mock raises ``TypeError: ClientResponse.__init__() missing ... 'stream_writer'``
    under aiohttp >=3.14.  This shim silently absorbs whichever variant the
    library passes and supplies a MagicMock default when it is missing, so the
    mock response can be created.  It is what lets the runtime dep stay on
    aiohttp >=3.14.1 (which fixes 11 advisories) while still mocking with
    aioresponses in tests — do not remove it until aioresponses ships native
    aiohttp 3.14 support.
    """
    original_init = aiohttp.ClientResponse.__init__

    @functools.wraps(original_init)
    def _patched_init(self, *args, **kwargs):
        # Ensure whichever kwarg the current aiohttp expects is present.
        # aiohttp >=3.12 uses "writer"; >=3.10 used "stream_writer".
        for key in ("writer", "stream_writer"):
            kwargs.setdefault(key, MagicMock())
        try:
            return original_init(self, *args, **kwargs)
        except TypeError:
            # If the original __init__ doesn't accept both, strip the one
            # it doesn't know about and retry.
            for key in ("writer", "stream_writer"):
                kwargs.pop(key, None)
            kwargs.setdefault("writer", MagicMock())
            try:
                return original_init(self, *args, **kwargs)
            except TypeError:
                kwargs.pop("writer", None)
                kwargs["stream_writer"] = MagicMock()
                return original_init(self, *args, **kwargs)

    aiohttp.ClientResponse.__init__ = _patched_init  # type: ignore[method-assign]
    return original_init


@pytest.fixture
def aiohttp_mock():
    original_init = _patch_client_response_init()
    try:
        with aioresponses() as m:
            yield m
    finally:
        aiohttp.ClientResponse.__init__ = original_init  # type: ignore[method-assign]
