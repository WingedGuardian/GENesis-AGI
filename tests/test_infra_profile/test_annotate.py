"""Annotation regen: hash-pinned, zero-spend when unchanged, keep-old on failure."""

from __future__ import annotations

from genesis.infra_profile.annotate import regenerate_annotations

# The REAL router contract — router.call returns this dataclass, never a dict.
# Using it here keeps the fake honest (a dict-shaped fake masked a repr-storing
# bug in review 2026-07-12).
from genesis.routing.types import RoutingResult


class FakeRouter:
    def __init__(self, response="- watch the thing", raise_exc=False, success=True):
        self.calls = []
        self._response = response
        self._raise = raise_exc
        self._success = success

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append(call_site_id)
        if self._raise:
            raise RuntimeError("provider down")
        return RoutingResult(
            success=self._success,
            call_site_id=call_site_id,
            content=self._response if self._success else None,
            model_id="fake-model" if self._success else None,
            error=None if self._success else "all providers failed",
        )


def _profile(section_hash="abc123"):
    return {
        "sections": {
            "storage": {
                "status": "ok",
                "hash": section_hash,
                "facts": {"mounts": []},
                "metrics": {},
            },
        },
    }


def _annotations(source_hash="abc123", text="- old note"):
    return {
        "schema_version": 1,
        "sections": {
            "storage": {"annotation": text, "source_hash": source_hash},
        },
    }


async def test_unchanged_hash_makes_zero_calls():
    router = FakeRouter()
    result = await regenerate_annotations(
        profile=_profile("abc123"),
        annotations=_annotations("abc123"),
        router=router,
        summary="s",
    )
    assert router.calls == []
    assert result == _annotations("abc123")


async def test_changed_hash_regenerates_and_pins():
    router = FakeRouter(response="- new gotcha")
    result = await regenerate_annotations(
        profile=_profile("NEW"),
        annotations=_annotations("abc123"),
        router=router,
        summary="s",
    )
    assert router.calls == ["46_infra_annotation"]
    entry = result["sections"]["storage"]
    assert entry["annotation"] == "- new gotcha"
    assert entry["source_hash"] == "NEW"
    assert entry["model"] == "fake-model"


async def test_missing_annotation_generates():
    router = FakeRouter()
    result = await regenerate_annotations(
        profile=_profile(),
        annotations={},
        router=router,
        summary="s",
    )
    assert len(router.calls) == 1
    assert result["sections"]["storage"]["source_hash"] == "abc123"


async def test_router_failure_keeps_old_annotation():
    router = FakeRouter(raise_exc=True)
    result = await regenerate_annotations(
        profile=_profile("NEW"),
        annotations=_annotations("abc123", "- old note"),
        router=router,
        summary="s",
    )
    entry = result["sections"]["storage"]
    assert entry["annotation"] == "- old note"
    assert entry["source_hash"] == "abc123"  # still pinned to OLD facts → stale


async def test_unsuccessful_result_keeps_old_annotation():
    """A failed chain RETURNS success=False (doesn't raise) — must not be pinned."""
    router = FakeRouter(success=False)
    result = await regenerate_annotations(
        profile=_profile("NEW"),
        annotations=_annotations("abc123", "- old note"),
        router=router,
        summary="s",
    )
    assert len(router.calls) == 1
    entry = result["sections"]["storage"]
    assert entry["annotation"] == "- old note"
    assert entry["source_hash"] == "abc123"


async def test_no_router_is_noop():
    original = _annotations()
    result = await regenerate_annotations(
        profile=_profile("NEW"),
        annotations=original,
        router=None,
        summary="s",
    )
    assert result is original


async def test_error_section_not_annotated():
    profile = _profile("NEW")
    profile["sections"]["storage"]["status"] = "error"
    router = FakeRouter()
    await regenerate_annotations(
        profile=profile,
        annotations={},
        router=router,
        summary="s",
    )
    assert router.calls == []
