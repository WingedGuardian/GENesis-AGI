"""WS-3 gate-1 / gate-2 FULL-CHAIN E2E — the real production paths drive a
shadow row end-to-end (real schema, real live config; stubs only at the LLM /
embedding boundary).

Complements the emit-contract tests: these prove the CHAIN — spine →
``judge_multi_procedure`` → store → row, and triage pipeline → directive
filter → steering write → row — which the per-link tests could not, including
the never-block invariant and the reject-is-a-non-event rule at the REAL call
sites. First run live against the deployed tree 2026-07-12 (post-B1 deploy).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import aiosqlite
import pytest

from genesis.db.crud import immunity_shadow as crud
from genesis.db.schema import create_all_tables
from genesis.learning.procedural import judge as judge_mod
from genesis.learning.procedural.judge import judge_multi_procedure


@pytest.fixture(autouse=True)
def _reset_caches():
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


async def _fresh_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await db.commit()
    return db


def _spine(*tools):
    return [
        {
            "turn": i + 1,
            "type": "tool",
            "tool": t,
            "args_summary": "x",
            "outcome": "ok",
            "error_text": "",
        }
        for i, t in enumerate(tools)
    ]


class _ProcRouter:
    """Builder-LLM stub: returns one well-formed procedure for any call."""

    def __init__(self, task_type, tools_used):
        self._task_type = task_type
        self._tools_used = tools_used

    async def route_call(self, call_site_id, messages, **kwargs):
        payload = json.dumps(
            [
                {
                    "task_type": self._task_type,
                    "principle": "Fetch then parse",
                    "scenario": "when the page needs scraping",
                    "steps": ["fetch x", "parse x"],
                    "tools_used": self._tools_used,
                    "context_tags": ["e2e"],
                }
            ]
        )

        class _R:
            success = True
            content = payload

        return _R()


def _no_embed():
    # None-provider: _principle_is_novel(embedder=None) fail-opens cleanly.
    # (A RAISING factory would crash the judge — get_embedding_provider() is
    # called unwrapped at judge._store_judged_procedure; tolerated upstream.)
    return None


@pytest.mark.asyncio
async def test_gate1_judge_chain_end_to_end():
    """spine → judge → store → shadow row: external spine records ONE row with
    the stable site ref; a first-party spine stores its procedure with NO row."""
    db = await _fresh_db()
    try:
        with patch.object(judge_mod, "get_embedding_provider", _no_embed):
            stored = await judge_multi_procedure(
                db,
                _spine("mcp__genesis-health__web_fetch", "Bash"),
                '{"url": "https://example.invalid"}',
                0.9,
                _ProcRouter("e2e-external-chain", ["web_fetch"]),
                source_session_id="e2e-1",
            )
        assert stored, "judge stored no procedure"
        rows = await crud.list_recent(db)
        assert len(rows) == 1, f"expected 1 shadow row, got {len(rows)}"
        assert rows[0]["gate"] == "procedure"
        assert rows[0]["origin_class"] == "external_untrusted"
        assert rows[0]["source_ref"] == "learning/procedural/judge.py::_store_judged_procedure"

        # Control: first-party spine → procedure stored, NO row (never-block).
        with patch.object(judge_mod, "get_embedding_provider", _no_embed):
            stored2 = await judge_multi_procedure(
                db,
                _spine("Bash", "Read"),
                "x",
                0.9,
                _ProcRouter("e2e-firstparty-chain", ["Bash"]),
                source_session_id="e2e-2",
            )
        assert stored2, "control judge stored nothing"
        assert await crud.count(db) == 1, "first-party spine must not add a row"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_gate2_steering_chain_end_to_end():
    """Real build_triage_pipeline → §6.6 → add_steering_rule → shadow row:
    a non-owner channel (voice) is BLOCKED at the write; an owner channel
    (telegram) writes with NO row; a directive-filter reject is a non-event.

    Expectation changed 2026-09-06. Step 1 previously asserted that `voice`
    WROTE the rule and recorded an external_untrusted shadow row — i.e. it
    encoded the fail-OPEN deny-list as an invariant, observing the bad write
    instead of preventing it. `_extract_steering_rule` now applies a fail-closed
    owner allow-list (`_STEERING_CHANNELS`) before writing, so a non-owner
    channel never reaches the write and the shadow emit below it is never
    reached either. The shadow row is no longer the mechanism protecting
    STEERING.md; the allow-list is.
    """
    from genesis.learning.pipeline import build_triage_pipeline
    from genesis.learning.types import OutcomeClass, TriageDepth

    db = await _fresh_db()
    written: list[str] = []
    try:

        class _Triage:
            async def classify(self, summary):
                class _T:
                    depth = TriageDepth.WORTH_THINKING
                    rationale = "e2e"

                return _T()

        class _Outcome:
            async def classify(self, summary):
                return OutcomeClass.APPROACH_FAILURE

        class _Delta:
            async def assess(self, summary):
                return None

        class _Obs:
            async def write(self, *args, **kwargs):
                return None

        class _Loader:
            def add_steering_rule(self, rule):
                written.append(rule)

        class _Out:
            text = "done."
            session_id = "e2e-steer"
            input_tokens = 900  # clears the prefilter token gate
            output_tokens = 100

        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=_Triage(),
            outcome_classifier=_Outcome(),
            delta_assessor=_Delta(),
            observation_writer=_Obs(),
            identity_loader=_Loader(),
        )

        # 1. voice (NOT in the owner allow-map): BLOCKED at the write, no row.
        #    The refusal is logged, not recorded — the shadow emit sits below
        #    the write and is never reached.
        await pipeline(_Out(), "never use the old parser", "voice")
        assert not written, "non-owner channel must not write a steering rule"
        assert await crud.count(db) == 0, "blocked write must not emit a row"

        # 2. telegram (owner): write happens, NO row (never-block invariant).
        await pipeline(_Out(), "never use the old linter", "telegram")
        assert len(written) == 1, "owner-channel steering write missing"
        assert await crud.count(db) == 0, "owner channel must not add a row"

        # 3. Non-directive on an OWNER channel: NO write, NO row (the reject is
        #    a non-event). This MUST be an owner channel: on voice the channel
        #    gate would fire first, and the assertion would pass without ever
        #    exercising the shape filter it exists to test.
        await pipeline(_Out(), "well anyway it is never too late to try", "telegram")
        assert len(written) == 1, "non-directive must not write"
        assert await crud.count(db) == 0, "directive reject must not emit"
    finally:
        await db.close()
