"""Bounded JSON delegation preserves the facade policy and captured bytes."""

import json
import os
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_codebase_managed_config import managed as managed  # noqa: F401


def test_document_facade_forwards_current_policy_and_capture(managed, monkeypatch, tmp_path):
    api = managed.read_document.__globals__
    assert callable(api.get("_read_document")), "bounded reader must use the captured unit helper"
    seen = []
    value = {"fixture": True}
    callback = seen.append
    path = tmp_path / "settings"

    def reader(candidate, *, open_flags, capture=None):
        seen.append((candidate, open_flags, capture))
        return value

    monkeypatch.setitem(api, "_read_document", reader)
    monkeypatch.setitem(api, "OPEN_FLAGS", os.O_CLOEXEC)
    assert managed.read_document(path, capture=callback) is value
    assert seen == [(path, os.O_CLOEXEC, callback)]


@pytest.mark.parametrize("payload,error,message", [
    (b'{}', None, None),
    (b'{}' + b' ' * (65536 - 2), None, None),
    (b'{}' + b' ' * (65537 - 2), ValueError, "64 KiB"),
    (b'{', json.JSONDecodeError, None),
    (b'[]', ValueError, "object"),
], ids=["empty-object", "limit", "oversized", "malformed", "array"])
def test_document_helper_bounds_validation_and_exact_capture(
    managed, tmp_path, payload, error, message
):
    helper = managed.read_document.__globals__.get("_read_document")
    assert callable(helper), "bounded reader must use the captured unit helper"
    path = tmp_path / "literal '雪 settings"
    path.write_bytes(payload)
    captured = []
    if error is not None:
        with pytest.raises(error, match=message):
            helper(path, open_flags=managed.OPEN_FLAGS, capture=captured.append)
        assert captured == []
    else:
        assert helper(path, open_flags=managed.OPEN_FLAGS, capture=captured.append) == {}
        assert captured == [payload]


def test_document_decoder_recursion_error_is_reported(managed, monkeypatch, tmp_path):
    helper = managed.read_document.__globals__.get("_read_document")
    assert callable(helper), "bounded reader must use the captured unit helper"
    path = tmp_path / "settings"
    path.write_bytes(b'{}')
    captured = []

    def too_deep(_payload):
        raise RecursionError("fixture decoder limit")

    monkeypatch.setitem(helper.__globals__, "json", SimpleNamespace(loads=too_deep))
    with pytest.raises(ValueError, match="nesting") as raised:
        helper(path, open_flags=managed.OPEN_FLAGS, capture=captured.append)
    assert isinstance(raised.value.__cause__, RecursionError)
    assert captured == []


def test_document_capture_callback_failure_propagates(managed, tmp_path):
    helper = managed.read_document.__globals__.get("_read_document")
    assert callable(helper), "bounded reader must use the captured unit helper"
    path = tmp_path / "settings"
    path.write_bytes(b'{}')

    def failed_capture(_payload):
        raise RuntimeError("fixture capture failed")

    with pytest.raises(RuntimeError, match="fixture capture failed"):
        helper(path, open_flags=managed.OPEN_FLAGS, capture=failed_capture)
