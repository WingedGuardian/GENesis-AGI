"""Tests for genesis.autonomy.executor.research.DeepResearcherImpl."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.executor.research import DeepResearcherImpl
from genesis.autonomy.executor.types import ResearchResult

# ─── Fixtures ────────────────────────────────────────────────────────────────


@dataclass
class FakeSearchResponse:
    query: str = ""
    results: list = field(default_factory=list)
    error: str | None = None


@dataclass
class FakeSearchResult:
    title: str = "Test Result"
    url: str = "https://example.com"
    snippet: str = "This is a relevant snippet about the solution"


@dataclass
class FakeRetrievalResult:
    content: str = "Previously solved: use --flag to fix this"
    score: float = 0.8


@dataclass
class FakeRouterResult:
    success: bool = True
    content: str = "The key insight is to use X instead of Y"


@dataclass
class FakeCCOutput:
    text: str = ""
    session_id: str = "ses-research-001"
    cost_usd: float = 0.01
    model_used: str = "sonnet"
    result: str = ""


# ─── Due Diligence Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestInlineDueDiligence:
    async def test_no_router_returns_none(self) -> None:
        researcher = DeepResearcherImpl(db=AsyncMock(), router=None)
        result = await researcher.inline_due_diligence(
            {"idx": 1, "description": "Fix the API call"},
            "ConnectionError: timeout",
        )
        assert result is None

    async def test_web_finds_relevant_result(self) -> None:
        web_searcher = AsyncMock()
        web_searcher.search.return_value = FakeSearchResponse(
            results=[FakeSearchResult(
                title="How to fix timeout errors",
                snippet="Set the timeout to 30s and add retry logic",
            )],
        )

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(
            content="The solution is to increase the timeout to 30s and add retry logic.",
        )

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            router=router,
            web_searcher=web_searcher,
        )
        result = await researcher.inline_due_diligence(
            {"idx": 1, "description": "Call external API"},
            "ConnectionError: timed out after 10s",
        )
        assert result is not None
        assert "timeout" in result.lower() or "30s" in result

    async def test_web_returns_nothing(self) -> None:
        web_searcher = AsyncMock()
        web_searcher.search.return_value = FakeSearchResponse(results=[])

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(content="NOT_RELEVANT")

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            router=router,
            web_searcher=web_searcher,
        )
        result = await researcher.inline_due_diligence(
            {"idx": 1, "description": "Do something"},
            "SomeError: unknown",
        )
        assert result is None

    async def test_memory_provides_context(self) -> None:
        retriever = AsyncMock()
        retriever.recall.return_value = [
            FakeRetrievalResult(content="Use --no-cache flag to bypass this"),
        ]

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(
            content="Based on memory: use --no-cache flag to bypass the issue.",
        )

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            router=router,
            retriever=retriever,
            web_searcher=AsyncMock(search=AsyncMock(return_value=FakeSearchResponse())),
        )
        result = await researcher.inline_due_diligence(
            {"idx": 2, "description": "Build the project"},
            "CacheError: stale cache entry",
        )
        assert result is not None
        assert "cache" in result.lower()

    async def test_both_sources_fail_gracefully(self) -> None:
        web_searcher = AsyncMock()
        web_searcher.search.side_effect = Exception("network error")

        retriever = AsyncMock()
        retriever.recall.side_effect = Exception("qdrant down")

        router = AsyncMock()

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            router=router,
            web_searcher=web_searcher,
            retriever=retriever,
        )
        result = await researcher.inline_due_diligence(
            {"idx": 1, "description": "Do thing"},
            "Error happened",
        )
        assert result is None


# ─── Research Session Tests ──────────────────────────────────────���───────────


@pytest.mark.asyncio
class TestResearchSession:
    async def test_no_invoker_returns_none(self) -> None:
        researcher = DeepResearcherImpl(db=AsyncMock(), invoker=None)
        result = await researcher.research(
            {"idx": 1, "description": "Fix bug"},
            "Error",
            [],
        )
        assert result is None

    async def test_session_finds_approach(self) -> None:
        output = FakeCCOutput(
            text='Some analysis...\n```json\n{"found": true, "approach": "Use library X version 2.0 which fixes this bug", "sources": ["https://github.com/lib/issues/123"], "clues": null, "concrete_blockers": []}\n```',
        )
        invoker = AsyncMock()
        invoker.run.return_value = output

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            invoker=invoker,
        )
        result = await researcher.research(
            {"idx": 1, "description": "Fix dependency issue"},
            "ImportError: incompatible version",
            ["Tried upgrading to latest"],
        )
        assert result is not None
        assert result.found is True
        assert "library X" in result.approach
        assert result.session_id == "ses-research-001"

    async def test_session_finds_nothing_with_blockers(self) -> None:
        output = FakeCCOutput(
            text='After extensive search...\n```json\n{"found": false, "approach": null, "sources": ["https://docs.example.com"], "clues": "The API requires OAuth2 but we have no credentials", "concrete_blockers": ["Need OAuth2 client credentials for service X"]}\n```',
        )
        invoker = AsyncMock()
        invoker.run.return_value = output

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            invoker=invoker,
        )
        result = await researcher.research(
            {"idx": 2, "description": "Authenticate with service"},
            "401 Unauthorized",
            [],
        )
        assert result is not None
        assert result.found is False
        assert "OAuth2" in result.concrete_blockers[0]
        assert result.clues is not None

    async def test_session_crash_returns_failure_result(self) -> None:
        invoker = AsyncMock()
        invoker.run.side_effect = RuntimeError("subprocess killed")

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            invoker=invoker,
        )
        result = await researcher.research(
            {"idx": 1, "description": "Do thing"},
            "Error",
            [],
        )
        assert result is not None
        assert result.found is False
        assert "crashed" in result.clues.lower()

    async def test_unparseable_output_falls_back(self) -> None:
        output = FakeCCOutput(
            text="I searched a lot but here is what I found: the API is broken and needs a fix.",
        )
        invoker = AsyncMock()
        invoker.run.return_value = output

        researcher = DeepResearcherImpl(
            db=AsyncMock(),
            invoker=invoker,
        )
        result = await researcher.research(
            {"idx": 1, "description": "Fix API"},
            "Error",
            [],
        )
        assert result is not None
        assert result.found is False
        assert "API is broken" in result.clues


# ─── Exit Gate Tests (via engine) ────────────────────────────────────────────


@pytest.mark.asyncio
class TestExitGate:
    async def test_rejects_vague_blockers(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(
            content='```json\n{"verdict": "reject", "reason": "Blocker is too vague", "suggested_approach": "Try using the --force flag"}\n```',
        )

        engine = CCSessionExecutor(
            db=AsyncMock(),
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
            router=router,
        )

        from genesis.autonomy.executor.types import StepResult

        research_result = ResearchResult(
            found=False,
            concrete_blockers=["Need better tools"],
        )

        decision = await engine._challenge_failure(
            "task-1",
            {"idx": 1, "description": "Do complex thing"},
            StepResult(idx=1, status="failed", result="It didn't work"),
            research_result,
            [],
        )
        assert decision["verdict"] == "reject"
        assert "suggested_approach" in decision

    async def test_accepts_concrete_blockers(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(
            content='```json\n{"verdict": "accept", "confirmed_blockers": ["Need OAuth2 credentials for service X"], "what_needs_to_change": "Acquire OAuth2 client_id and client_secret from service X admin panel"}\n```',
        )

        engine = CCSessionExecutor(
            db=AsyncMock(),
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
            router=router,
        )

        from genesis.autonomy.executor.types import StepResult

        research_result = ResearchResult(
            found=False,
            concrete_blockers=["Need OAuth2 credentials for service X"],
        )

        decision = await engine._challenge_failure(
            "task-1",
            {"idx": 1, "description": "Authenticate"},
            StepResult(idx=1, status="failed", result="401 Unauthorized"),
            research_result,
            [],
        )
        assert decision["verdict"] == "accept"
        assert "OAuth2" in decision["confirmed_blockers"][0]

    async def test_no_router_defaults_to_accept(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        engine = CCSessionExecutor(
            db=AsyncMock(),
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
            router=None,
        )

        from genesis.autonomy.executor.types import StepResult

        decision = await engine._challenge_failure(
            "task-1",
            {"idx": 1, "description": "Thing"},
            StepResult(idx=1, status="failed", result="Error"),
            None,
            [],
        )
        assert decision["verdict"] == "accept"

    async def test_llm_failure_defaults_to_accept(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        router = AsyncMock()
        router.route_call.side_effect = Exception("LLM unavailable")

        engine = CCSessionExecutor(
            db=AsyncMock(),
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
            router=router,
        )

        from genesis.autonomy.executor.types import StepResult

        decision = await engine._challenge_failure(
            "task-1",
            {"idx": 1, "description": "Thing"},
            StepResult(idx=1, status="failed", result="Error"),
            None,
            [],
        )
        assert decision["verdict"] == "accept"

    async def test_prior_rejections_passed_in_prompt(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        router = AsyncMock()
        router.route_call.return_value = FakeRouterResult(
            content='```json\n{"verdict": "accept", "confirmed_blockers": ["Real blocker"], "what_needs_to_change": "Fix X"}\n```',
        )

        engine = CCSessionExecutor(
            db=AsyncMock(),
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
            router=router,
        )

        from genesis.autonomy.executor.types import StepResult

        prior = [
            {"verdict": "reject", "reason": "Try harder", "suggested_approach": "Do X"},
            {"verdict": "reject", "reason": "Still vague", "suggested_approach": "Do Y"},
        ]

        await engine._challenge_failure(
            "task-1",
            {"idx": 1, "description": "Thing"},
            StepResult(idx=1, status="failed", result="Error"),
            ResearchResult(found=False, concrete_blockers=["Specific blocker"]),
            prior,
        )
        # Verify router was called with prior rejections in the prompt
        call_args = router.route_call.call_args
        prompt_content = call_args[0][1][0]["content"]
        assert "Try harder" in prompt_content
        assert "Still vague" in prompt_content


# ─── Challenge Recording Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestRecordChallenge:
    async def test_creates_observation_and_follow_up(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor
        from genesis.autonomy.executor.types import StepResult

        db = AsyncMock()

        engine = CCSessionExecutor(
            db=db,
            invoker=AsyncMock(),
            decomposer=AsyncMock(),
            reviewer=AsyncMock(),
        )

        research_result = ResearchResult(
            found=False,
            clues="Partial findings about the issue",
            concrete_blockers=["Need credential X"],
            session_id="ses-123",
        )

        exit_decision = {
            "verdict": "accept",
            "confirmed_blockers": ["Need credential X"],
            "what_needs_to_change": "Acquire X from admin panel",
        }

        with (
            patch("genesis.db.crud.observations.create", new_callable=AsyncMock) as mock_obs,
            patch("genesis.db.crud.follow_ups.create", new_callable=AsyncMock) as mock_fu,
        ):
            await engine._record_challenge(
                "task-1",
                {"idx": 1, "description": "Authenticate with service"},
                StepResult(idx=1, status="failed", result="401 Unauthorized"),
                research_result,
                exit_decision,
            )

            # Observation was created
            mock_obs.assert_called_once()
            obs_call = mock_obs.call_args
            assert obs_call.kwargs["obs_type"] == "execution_challenge"
            assert obs_call.kwargs["priority"] == "high"

            # Follow-up was created
            mock_fu.assert_called_once()
            fu_call = mock_fu.call_args
            assert fu_call.kwargs["strategy"] == "ego_judgment"
            assert "Authenticate with service" in fu_call.kwargs["content"]


# ─── JSON Parsing Tests ──────────────────────────────────────────────────────


class TestJsonParsing:
    def test_parse_fenced_json(self) -> None:
        text = "Here is my analysis:\n```json\n{\"found\": true, \"approach\": \"Do X\"}\n```\nDone."
        result = DeepResearcherImpl._extract_json_from_text(text)
        assert result == {"found": True, "approach": "Do X"}

    def test_parse_bare_json(self) -> None:
        text = 'After analysis, the result is {"found": false, "clues": "nothing"}'
        result = DeepResearcherImpl._extract_json_from_text(text)
        assert result == {"found": False, "clues": "nothing"}

    def test_parse_no_json(self) -> None:
        text = "No JSON here, just plain text."
        result = DeepResearcherImpl._extract_json_from_text(text)
        assert result is None

    def test_parse_gate_response_fenced(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        text = '```json\n{"verdict": "reject", "reason": "vague"}\n```'
        result = CCSessionExecutor._parse_gate_response(text)
        assert result == {"verdict": "reject", "reason": "vague"}

    def test_parse_gate_response_bare(self) -> None:
        from genesis.autonomy.executor.engine import CCSessionExecutor

        text = 'I think: {"verdict": "accept", "confirmed_blockers": ["X"]}'
        result = CCSessionExecutor._parse_gate_response(text)
        assert result["verdict"] == "accept"
