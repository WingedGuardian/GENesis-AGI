"""Lexical metadata, immutable preparation and request-local provenance."""

import asyncio
import copy
from pathlib import Path

import pytest

from genesis.caller_context import CallerContext, request_caller
from genesis.mcp.plan_bookmark_context import (
    PENDING_META,
    current_pending_bookmark,
    pending_path,
    prepare_pending_params,
    request_pending_bookmark,
)
from genesis.mcp.shared_paths import SharedPathError

VALUE = {"version": 1, "path": "/caller/.genesis/plan_bookmark_pending.json"}
BAD = [None, [], {}, {**VALUE, "extra": 1}, {"path": VALUE["path"]},
       *({**VALUE, "version": v} for v in (True, False, 0, 2, 1.0, "1", None)),
       *({**VALUE, "path": p} for p in (None, 3, "", "relative/.genesis/plan_bookmark_pending.json",
           "/caller/plan_bookmark_pending.json", "/caller/.genesis/other.json", "/bad\0/.genesis/plan_bookmark_pending.json"))]


@pytest.mark.parametrize("value", BAD)
def test_invalid_metadata_refuses(value):
    with pytest.raises(SharedPathError):
        pending_path(value)


def test_validation_never_probes_or_resolves(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("lexical validation touched a file")

    with monkeypatch.context() as patch:
        for name in ("exists", "resolve", "expanduser", "read_bytes", "read_text", "stat"):
            patch.setattr(Path, name, forbidden)
        actual = pending_path(VALUE)
    assert actual == Path(VALUE["path"])


@pytest.mark.parametrize("metadata", [None, {}, {"progressToken": "original", "other": {"x": [1]}}])
def test_preparation_preserves_original_and_unrelated_values(metadata):
    params = {"name": "procedure_recall", "arguments": {"task_description": "synthetic"}, "_meta": metadata}
    original = copy.deepcopy(params)
    result = prepare_pending_params("tools/call", params, Path(VALUE["path"]))
    assert params == original
    assert result["arguments"] == original["arguments"] and result["name"] == original["name"]
    assert result["_meta"] == {**(metadata or {}), PENDING_META: VALUE}


@pytest.mark.parametrize("metadata", [[], "bad", 3, False, {PENDING_META: VALUE}])
def test_preparation_refuses_reserved_or_invalid_metadata(metadata):
    with pytest.raises(SharedPathError):
        prepare_pending_params("tools/call", {"_meta": metadata}, Path(VALUE["path"]))


@pytest.mark.parametrize("method,params", [("tools/list", {}), ("initialize", {}), ("tools/call", None)])
def test_non_calls_remain_identical(method, params):
    assert prepare_pending_params(method, params, Path(VALUE["path"])) is params


def test_scope_requires_same_authenticated_caller_and_resets():
    caller = CallerContext(dispatched=False)
    assert current_pending_bookmark() is None
    with request_caller(caller), request_pending_bookmark("memory", caller, {PENDING_META: VALUE}, versioned=True):
        assert current_pending_bookmark() == Path(VALUE["path"])
        with request_caller(CallerContext(dispatched=False)):
            assert current_pending_bookmark() is None
        with request_caller(None):
            assert current_pending_bookmark() is None
        with request_pending_bookmark("memory", caller, {}, versioned=True):
            assert current_pending_bookmark() is None
        assert current_pending_bookmark() == Path(VALUE["path"])
    assert current_pending_bookmark() is None


@pytest.mark.parametrize("service,profile,versioned", [
    ("health", None, True), ("memory", "external", True), ("memory", "validator", True),
    ("memory", "codex", True), ("memory", "", True), ("memory", None, False)])
def test_reserved_metadata_requires_positive_eligibility(service, profile, versioned):
    with pytest.raises(SharedPathError), request_pending_bookmark(
        service, CallerContext(profile=profile), {PENDING_META: VALUE}, versioned=versioned,
    ):
        pytest.fail("ineligible scope admitted")
    assert current_pending_bookmark() is None


async def test_overlap_cancel_and_child_capture_are_isolated():
    entered, release = asyncio.Event(), asyncio.Event()
    caller = CallerContext(dispatched=False)
    captured = []

    async def eligible():
        try:
            with request_caller(caller), request_pending_bookmark("memory", caller, {PENDING_META: VALUE}, versioned=True):
                captured.append(current_pending_bookmark())
                entered.set()
                await release.wait()
        finally:
            assert current_pending_bookmark() is None

    owner = asyncio.create_task(eligible())
    await entered.wait()
    assert current_pending_bookmark() is None
    with request_caller(CallerContext(profile="external")), request_pending_bookmark("memory", CallerContext(profile="external"), {}, versioned=True):
        assert current_pending_bookmark() is None
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert captured == [Path(VALUE["path"])] and current_pending_bookmark() is None
