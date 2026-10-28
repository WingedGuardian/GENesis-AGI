"""Surplus value probe: which gathered surplus insights a deep reflection CITES.

The record lives in the routing summary, which is persisted as the details of
the ``deep_reflection.completed`` event. Three states must stay distinct:
probe not run (no surplus gathered: no ``surplus_probe`` key), the model
reported citations (a list, possibly empty), and the model omitted the field
(``cited`` is None).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.output_router import OutputRouter, parse_deep_reflection_output
from genesis.reflection.types import DeepReflectionOutput


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def router(tmp_path):
    return OutputRouter(reflections_dir=tmp_path / "reflections")


async def _promoted(db, ids):
    from genesis.db.crud import surplus

    for n, sid in enumerate(ids):
        await surplus.upsert(
            db,
            id=sid,
            content=f"insight number {n}",
            source_task_type="brainstorm_self",
            generating_model="m",
            drive_alignment="curiosity",
            created_at="2026-01-01T00:00:00+00:00",
            ttl="2099-01-01T00:00:00+00:00",
            confidence=0.6,
        )
        await surplus.promote(db, sid, promoted_to="idea:x")


# ── Parsing ────────────────────────────────────────────────────────────


class TestParseSurplusCited:
    def test_list_of_ids_parsed(self):
        out = parse_deep_reflection_output(
            json.dumps(
                {
                    "observations": ["o"],
                    "surplus_cited": ["a", " b ", "a"],
                }
            )
        )
        assert out.surplus_cited == ["a", "b"]

    def test_empty_list_is_reported_zero(self):
        out = parse_deep_reflection_output(
            json.dumps(
                {
                    "observations": ["o"],
                    "surplus_cited": [],
                }
            )
        )
        assert out.surplus_cited == []

    def test_absent_is_none(self):
        out = parse_deep_reflection_output(json.dumps({"observations": ["o"]}))
        assert out.surplus_cited is None

    def test_non_list_is_none(self):
        out = parse_deep_reflection_output(
            json.dumps(
                {
                    "observations": ["o"],
                    "surplus_cited": "a",
                }
            )
        )
        assert out.surplus_cited is None

    def test_non_string_entries_dropped(self):
        out = parse_deep_reflection_output(
            json.dumps(
                {
                    "observations": ["o"],
                    "surplus_cited": ["a", 3, None, {"id": "b"}, ""],
                }
            )
        )
        assert out.surplus_cited == ["a"]


# ── Routing: the probe record ─────────────────────────────────────────


class TestRouteRecordsCitations:
    @pytest.mark.asyncio
    async def test_cited_subset_recorded(self, db, router):
        await _promoted(db, ["s1", "s2", "s3"])
        out = DeepReflectionOutput(observations=["obs"], surplus_cited=["s2"])
        summary = await router.route(out, db, gathered_surplus_ids=("s1", "s2", "s3"))
        assert summary["surplus_probe"] == {
            "gathered": ["s1", "s2", "s3"],
            "cited": ["s2"],
            "rejected": 0,
            "rejected_ids": [],
        }
        # Consumption marking is unchanged.
        assert summary["surplus_consumed"] == 3

    @pytest.mark.asyncio
    async def test_ids_outside_gathered_set_rejected(self, db, router):
        await _promoted(db, ["s1", "s2"])
        out = DeepReflectionOutput(
            observations=["obs"],
            surplus_cited=["s1", "made-up", "s9"],
        )
        summary = await router.route(out, db, gathered_surplus_ids=("s1", "s2"))
        assert summary["surplus_probe"]["cited"] == ["s1"]
        assert summary["surplus_probe"]["rejected"] == 2
        assert summary["surplus_probe"]["rejected_ids"] == ["made-up", "s9"]

    @pytest.mark.asyncio
    async def test_rejected_sample_is_bounded_but_count_is_full(self, db, router):
        await _promoted(db, ["s1"])
        bogus = [f"bogus-{n}" for n in range(30)] + ["x" * 500]
        out = DeepReflectionOutput(observations=["obs"], surplus_cited=["s1", *bogus])
        summary = await router.route(out, db, gathered_surplus_ids=("s1",))
        probe = summary["surplus_probe"]
        assert probe["cited"] == ["s1"]
        assert probe["rejected"] == 31
        assert probe["rejected_ids"] == bogus[:20]  # whole ids, never cut

    @pytest.mark.asyncio
    async def test_citing_none_records_zero(self, db, router):
        await _promoted(db, ["s1"])
        out = DeepReflectionOutput(observations=["obs"], surplus_cited=[])
        summary = await router.route(out, db, gathered_surplus_ids=("s1",))
        assert summary["surplus_probe"] == {
            "gathered": ["s1"],
            "cited": [],
            "rejected": 0,
            "rejected_ids": [],
        }

    @pytest.mark.asyncio
    async def test_omitted_field_records_not_reported(self, db, router):
        await _promoted(db, ["s1"])
        out = DeepReflectionOutput(observations=["obs"])
        summary = await router.route(out, db, gathered_surplus_ids=("s1",))
        assert summary["surplus_probe"] == {
            "gathered": ["s1"],
            "cited": None,
            "rejected": 0,
            "rejected_ids": [],
        }

    @pytest.mark.asyncio
    async def test_no_surplus_gathered_means_probe_not_run(self, db, router):
        out = DeepReflectionOutput(observations=["obs"], surplus_cited=["s1"])
        summary = await router.route(out, db)
        assert "surplus_probe" not in summary

    @pytest.mark.asyncio
    async def test_probe_persisted_in_event_details_not_message(self, db, tmp_path):
        bus = AsyncMock()
        router = OutputRouter(event_bus=bus, reflections_dir=tmp_path / "r")
        await _promoted(db, ["s1", "s2"])
        out = DeepReflectionOutput(observations=["obs"], surplus_cited=["s1"])
        await router.route(out, db, gathered_surplus_ids=("s1", "s2"))
        completed = [
            c
            for c in bus.emit.call_args_list
            if len(c.args) > 2 and c.args[2] == "deep_reflection.completed"
        ]
        assert len(completed) == 1
        call = completed[0]
        assert call.kwargs["surplus_probe"]["cited"] == ["s1"]
        # The id lists stay out of the one-line human message.
        assert "s2" not in call.args[3]


# ── Prompt: the gathered items carry ids and the ask is made ──────────


class TestPromptCarriesIds:
    @pytest.mark.asyncio
    async def test_digest_lines_carry_insight_ids(self, db):
        from genesis.reflection.context_gatherer import ContextGatherer

        await _promoted(db, ["sid-alpha", "sid-beta"])
        digest, ids = await ContextGatherer()._promoted_surplus_digest(db)
        assert sorted(ids) == ["sid-alpha", "sid-beta"]
        assert "[id: sid-alpha]" in digest
        assert "[id: sid-beta]" in digest

    @pytest.mark.asyncio
    async def test_deep_prompt_asks_for_surplus_cited(self, db):
        from genesis.cc.reflection_bridge._prompts import build_enriched_prompt
        from genesis.reflection.context_gatherer import ContextGatherer

        await _promoted(db, ["sid-alpha"])
        tick = AsyncMock()
        tick.tick_id = "t1"
        tick.timestamp = "2026-01-01T00:00:00+00:00"
        tick.trigger_reason = "test"
        tick.signals = []
        prompt, _obs, surplus_ids = await build_enriched_prompt(
            tick,
            db=db,
            context_gatherer=ContextGatherer(),
        )
        assert surplus_ids == ("sid-alpha",)
        assert '"surplus_cited"' in prompt
        assert "[id: sid-alpha]" in prompt


# ── Output contracts: the field is asked for wherever the JSON is defined ──


def test_system_prompt_requires_surplus_cited_even_when_empty():
    """REFLECTION_DEEP.md says to omit empty fields; surplus_cited is the
    exception, or "cited none" would arrive as "not reported"."""
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / "src" / "genesis" / "identity" / "REFLECTION_DEEP.md"
    ).read_text()
    assert "`surplus_cited` is required, as `[]`" in text
    assert '"surplus_cited":' in text


def test_salvage_prompt_offers_surplus_cited():
    from genesis.cc.reflection_bridge._output import _SALVAGE_PROMPT

    optional = _SALVAGE_PROMPT.split("Optional")[1]
    assert '"surplus_cited"' in optional
