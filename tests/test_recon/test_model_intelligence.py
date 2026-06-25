"""Tests for model intelligence recon job."""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.recon.model_intelligence import (
    ModelIntelligenceJob,
    _eol_findings_from_rows,
    _parse_gemini_deprecation_table,
    _parse_groq_deprecation_table,
)
from genesis.routing.config import load_config_from_string
from genesis.routing.model_profiles import ModelProfileRegistry


@pytest.fixture(autouse=True)
def _stub_web_fetch():
    """Stub genesis.web.fetch module-wide so run()-based tests never hit the
    live Groq deprecations page. EOL tests override this by patching
    _fetch_groq_deprecations directly or re-patching genesis.web.fetch locally.
    """
    from genesis.web.types import FetchResult

    with patch(
        "genesis.web.fetch",
        AsyncMock(return_value=FetchResult(url="", text="", error="stubbed-in-tests")),
    ):
        yield


@pytest.fixture()
async def db():
    """In-memory SQLite with observations and follow_ups tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE observations ("
            "  id TEXT PRIMARY KEY,"
            "  person_id TEXT,"
            "  source TEXT, type TEXT, category TEXT,"
            "  content TEXT, priority TEXT, speculative INTEGER DEFAULT 0,"
            "  created_at TEXT, expires_at TEXT, content_hash TEXT,"
            "  resolved INTEGER DEFAULT 0,"
            "  resolved_at TEXT, resolution_notes TEXT"
            ")"
        )
        from genesis.db.schema import TABLES
        await conn.execute(TABLES["follow_ups"])
        await conn.commit()
        yield conn


@pytest.fixture()
def registry(tmp_path) -> ModelProfileRegistry:
    """Load a registry with test profiles."""
    p = tmp_path / "profiles.yaml"
    p.write_text(textwrap.dedent("""\
        profiles:
          test-model:
            display_name: "Test Model"
            provider: test
            api_id: "test/test-model"
            intelligence_tier: A
            reasoning: A
            instruction_following: A
            anti_sycophancy: B
            context_window: 200000
            cost_tier: moderate
            cost_per_mtok_in: 2.00
            cost_per_mtok_out: 10.00
            latency: moderate
            best_for: [deep_reflection]
            avoid_for: []
            free_tier:
              available: false
            last_reviewed: "2025-01-01"
            review_source: manual
    """))
    reg = ModelProfileRegistry(p)
    reg.load()
    return reg


def _mock_openrouter_response(models: list[dict]):
    """Create a mock httpx response for OpenRouter API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": models}
    return mock_resp


class TestOpenRouterCheck:
    """OpenRouter model scanning."""

    @pytest.mark.asyncio
    async def test_detects_new_model(self, db, registry) -> None:
        """New model with 100k+ context is flagged."""
        models = [
            {
                "id": "new-provider/new-model",
                "name": "New Model",
                "context_length": 500_000,
                "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] >= 1
        new_model_findings = [
            f for f in result["findings"] if f["type"] == "new_model"
        ]
        assert len(new_model_findings) == 1
        assert new_model_findings[0]["api_id"] == "new-provider/new-model"

    @pytest.mark.asyncio
    async def test_detects_pricing_change(self, db, registry) -> None:
        """Pricing change on known model is flagged."""
        models = [
            {
                "id": "test/test-model",
                "name": "Test Model",
                "context_length": 200_000,
                "pricing": {
                    "prompt": "0.000003",  # 3.00/MTok vs 2.00 in profile
                    "completion": "0.000010",  # same
                },
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        pricing_findings = [
            f for f in result["findings"] if f["type"] == "pricing_change"
        ]
        assert len(pricing_findings) == 1
        assert pricing_findings[0]["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_empty_response_handled(self, db) -> None:
        """Empty OpenRouter response produces no findings."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] == 0

    @pytest.mark.asyncio
    async def test_api_failure_handled(self, db) -> None:
        """OpenRouter API failure is handled gracefully."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("down"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] == 0


class TestStalenessCheck:
    """Profile staleness detection."""

    @pytest.mark.asyncio
    async def test_stale_profile_flagged(self, db, registry) -> None:
        """Profile with last_reviewed > 30 days ago is flagged."""
        job = ModelIntelligenceJob(db=db, profile_registry=registry)

        # Mock OpenRouter to return empty
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        stale = [f for f in result["findings"] if f["type"] == "stale_profile"]
        assert len(stale) == 1  # test-model was reviewed 2025-01-01
        assert stale[0]["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_no_registry_no_staleness(self, db) -> None:
        """Without registry, no staleness findings."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["stale_findings"] == 0


class TestReconFollowUpPipeline:
    """Recon creates follow-ups for new free models."""

    @pytest.mark.asyncio
    async def test_new_free_model_creates_follow_up(self, db, registry, tmp_path) -> None:
        """New free model creates a follow-up with structured payload."""
        import json

        models = [
            {
                "id": "new-provider/free-model",
                "name": "Free Model",
                "context_length": 100_000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]

        # Use tmp_path for cache to avoid polluting real cache
        cache_path = tmp_path / "free_model_cache.json"

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("genesis.recon.model_intelligence._FREE_MODEL_CACHE_PATH", cache_path),
        ):
            await job.run()

        # Check follow-up was created
        cursor = await db.execute(
            "SELECT * FROM follow_ups WHERE source = 'recon_pipeline'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

        fu = dict(rows[0])
        assert fu["strategy"] == "surplus_task"
        assert "new-provider/free-model" in fu["content"]
        assert fu["status"] == "pending"

        # Verify structured payload in reason
        payload = json.loads(fu["reason"])
        assert payload["task_type"] == "model_eval"
        assert payload["payload"]["model_id"] == "new-provider/free-model"

    @pytest.mark.asyncio
    async def test_no_follow_up_without_surplus_queue(self, db, registry, tmp_path) -> None:
        """Without surplus queue, follow-up is still created (no cap check)."""
        models = [
            {
                "id": "another/free-model",
                "name": "Another Free",
                "context_length": 50_000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]

        cache_path = tmp_path / "free_model_cache.json"
        job = ModelIntelligenceJob(db=db, profile_registry=registry)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("genesis.recon.model_intelligence._FREE_MODEL_CACHE_PATH", cache_path),
        ):
            await job.run()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM follow_ups WHERE source = 'recon_pipeline'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1  # follow-up created even without queue


class TestFindingStorage:
    """Finding persistence in observations."""

    @pytest.mark.asyncio
    async def test_findings_stored_in_db(self, db, registry) -> None:
        """Findings are persisted to observations table."""
        models = [
            {
                "id": "new/big-model",
                "name": "Big Model",
                "context_length": 200_000,
                "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE category = 'model_intelligence'"
        )
        count = (await cursor.fetchone())[0]
        assert count == result["total_findings"]
        assert count > 0


# ── Active-provider EOL detection (Groq deprecations page) ─────────────────

# A representative slice of Groq's real deprecations table (incl. header,
# separator, single-digit date, multi-replacement cell, and a non-row line).
_SAMPLE_GROQ_TABLE = """\
## Deprecation History
| Deprecated Model | Shutdown Date | Recommended Replacement Model ID |
| --- | --- | --- |
| `moonshotai/kimi-k2-instruct-0905` | 04/15/26 | `openai/gpt-oss-120b` |
| `llama3-70b-8192` | 8/30/25 | `llama-3.3-70b-versatile` |
| `mixtral-8x7b-32768` | 03/20/25 | `mistral-saba-24b`  `llama-3.3-70b-versatile` |
Some prose line | with a pipe but not a table row
"""

# A config where one Groq provider (groq-doomed) uses a deprecated model and
# another (groq-free) uses llama-3.3-70b-versatile — which appears in the table
# ONLY as a replacement, so it must NOT fire (the no-false-alarm case).
_EOL_CONFIG_YAML = """\
providers:
  groq-free:
    type: groq
    model: llama-3.3-70b-versatile
    free: true
    rpm_limit: 30
  groq-doomed:
    type: groq
    model: moonshotai/kimi-k2-instruct-0905
    free: true
    rpm_limit: 30
  gemini-free:
    type: google
    model: gemini-3-flash-preview
    free: true
    rpm_limit: 15
call_sites:
  s_micro:
    chain: [groq-free, gemini-free]
  s_report:
    chain: [groq-doomed]
  s_other:
    chain: [groq-doomed, groq-free]
"""

# Google's Gemini deprecations table: 4 columns, month-name dates, multi-section,
# "No shutdown date announced" for undated models, a "Preview models" separator,
# and a prose line. Only the two rows with a real dated shutdown must parse.
_SAMPLE_GEMINI_TABLE = """\
## Gemini 3 models
| **Model** | **Release date** | **Shutdown date** | **Recommended replacement** |
|---|---|---|---|
| `gemini-3.5-flash` | May 19, 2026 | No shutdown date announced | |
| `gemini-3.1-flash-lite` | May 7, 2026 | May 7, 2027 | |
| Preview models ||||
| `gemini-3-flash-preview` | December 17, 2025 | No shutdown date announced | `gemini-3.5-flash` |
## Gemini 2.5 Flash models
| `gemini-2.5-flash` | June 17, 2025 | October 16, 2026 | `gemini-3.5-flash` |
Some prose | with a pipe but no date | here too | and here |
"""


class TestGeminiDeprecationParser:
    """Pure parsing of Google's Gemini deprecations markdown table."""

    def test_parses_dated_rows_only(self) -> None:
        rows = _parse_gemini_deprecation_table(_SAMPLE_GEMINI_TABLE)
        assert ("gemini-3.1-flash-lite", "2027-05-07", "") in rows
        assert ("gemini-2.5-flash", "2026-10-16", "gemini-3.5-flash") in rows
        assert len(rows) == 2

    def test_skips_undated_models(self) -> None:
        # "No shutdown date announced" models must NOT fire (the no-false-alarm
        # case — exactly our current gemini-3-flash-preview situation).
        models = [r[0] for r in _parse_gemini_deprecation_table(_SAMPLE_GEMINI_TABLE)]
        assert "gemini-3.5-flash" not in models
        assert "gemini-3-flash-preview" not in models

    def test_skips_headers_separators_prose_and_preview_label(self) -> None:
        models = [r[0] for r in _parse_gemini_deprecation_table(_SAMPLE_GEMINI_TABLE)]
        assert "Model" not in models and "**Model**" not in models
        assert all("---" not in m for m in models)
        assert "Preview models" not in models
        assert "Some prose" not in models

    def test_no_surrounding_pipe_variant(self) -> None:
        # genesis.web.fetch may render rows without surrounding pipes.
        live = "`gemini-2.5-flash`| June 17, 2025| October 16, 2026| `gemini-3.5-flash`\n"
        rows = _parse_gemini_deprecation_table(live)
        assert ("gemini-2.5-flash", "2026-10-16", "gemini-3.5-flash") in rows

    def test_accepts_abbreviated_months(self) -> None:
        # A narrowed column could abbreviate months — must NOT silently drop a
        # real dated shutdown (the dangerous false-negative direction).
        live = "`gemini-x`| Jan 5, 2026| Sep 30, 2027| `repl`\n"
        rows = _parse_gemini_deprecation_table(live)
        assert ("gemini-x", "2027-09-30", "repl") in rows

    def test_empty_or_garbage_returns_empty(self) -> None:
        assert _parse_gemini_deprecation_table("") == []
        assert _parse_gemini_deprecation_table("no tables here\njust text") == []
        assert _parse_gemini_deprecation_table(
            "| **Model** | **Release date** | **Shutdown date** | **Replacement** |\n"
            "|---|---|---|---|"
        ) == []


class TestGroqDeprecationParser:
    """Pure parsing of the Groq deprecations markdown table."""

    def test_parses_real_format_rows(self) -> None:
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        assert ("moonshotai/kimi-k2-instruct-0905", "2026-04-15", "openai/gpt-oss-120b") in rows
        # single-digit month/day normalized to ISO
        assert ("llama3-70b-8192", "2025-08-30", "llama-3.3-70b-versatile") in rows
        assert len(rows) == 3

    def test_skips_header_separator_and_prose(self) -> None:
        models = [r[0] for r in _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)]
        assert "Deprecated Model" not in models
        assert all("---" not in m for m in models)

    def test_multi_replacement_cell_kept_as_text(self) -> None:
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        mixtral = next(r for r in rows if r[0] == "mixtral-8x7b-32768")
        assert "mistral-saba-24b" in mixtral[2]
        assert "llama-3.3-70b-versatile" in mixtral[2]

    def test_empty_or_garbage_returns_empty(self) -> None:
        assert _parse_groq_deprecation_table("") == []
        assert _parse_groq_deprecation_table("no tables here\njust text") == []
        # header + separator only (no dated rows)
        assert _parse_groq_deprecation_table(
            "| Deprecated Model | Shutdown Date | Replacement |\n| --- | --- | --- |"
        ) == []

    def test_parses_live_no_leading_pipe_format(self) -> None:
        # The format genesis.web.fetch ACTUALLY returns — cells separated by
        # pipes with NO surrounding pipes (`model| date| repl`). Regression for
        # the bug the live E2E caught; also the real llama-3.3-70b/Aug-16 row.
        live = (
            "### August 16, 2026\n"
            "Deprecated Model| Shutdown Date| Recommended Replacement Model ID\n"
            "---|---|---\n"
            "`llama-3.1-8b-instant`| 08/16/26| `openai/gpt-oss-20b`\n"
            "`llama-3.3-70b-versatile`| 08/16/26| `openai/gpt-oss-120b` or `qwen/qwen3.6-27b`\n"
        )
        rows = _parse_groq_deprecation_table(live)
        assert ("llama-3.1-8b-instant", "2026-08-16", "openai/gpt-oss-20b") in rows
        assert (
            "llama-3.3-70b-versatile", "2026-08-16",
            "openai/gpt-oss-120b or qwen/qwen3.6-27b",
        ) in rows
        assert len(rows) == 2

    def test_rejects_non_calendar_dates(self) -> None:
        # A version-number-like field (e.g. 13/45/26) in the middle column must
        # not be parsed as a shutdown date.
        assert _parse_groq_deprecation_table("`some/model`| 13/45/26| `repl`") == []


class TestEOLMatcher:
    """Matching parsed rows against our active Groq providers."""

    def _cfg(self):
        return load_config_from_string(_EOL_CONFIG_YAML)

    def test_no_false_alarm_when_our_models_not_deprecated(self) -> None:
        # The regression test for the whole episode: llama-3.3-70b-versatile is
        # only a *replacement* in the table, so groq-free must NOT fire.
        cfg = self._cfg()
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        findings = _eol_findings_from_rows(rows, cfg.providers, cfg.call_sites)
        assert all(f["provider"] != "groq-free" for f in findings)

    def test_fires_for_our_deprecated_model_with_blast_radius(self) -> None:
        cfg = self._cfg()
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        findings = _eol_findings_from_rows(rows, cfg.providers, cfg.call_sites)
        assert len(findings) == 1  # only groq-doomed's model is deprecated
        f = findings[0]
        assert f["type"] == "active_model_eol"
        assert f["model"] == "moonshotai/kimi-k2-instruct-0905"
        assert f["provider"] == "groq-doomed"
        assert f["vendor"] == "groq"
        assert f["shutdown_date"] == "2026-04-15"
        assert f["replacement"] == "openai/gpt-oss-120b"
        assert f["blast_radius"] == ["s_other", "s_report"]

    def test_unrelated_deprecation_yields_nothing(self) -> None:
        # firehose guard: a Groq deprecation for a model we don't use → []
        cfg = self._cfg()
        rows = [("some/model-we-dont-use", "2026-12-31", "repl")]
        assert _eol_findings_from_rows(rows, cfg.providers, cfg.call_sites) == []

    def test_non_groq_model_ignored(self) -> None:
        # default vendor gate is groq — a google model string must not match
        cfg = self._cfg()
        rows = [("gemini-3-flash-preview", "2026-06-01", "gemini-3.5-flash")]
        assert _eol_findings_from_rows(rows, cfg.providers, cfg.call_sites) == []

    def test_fires_for_our_deprecated_gemini_model(self) -> None:
        # The google vendor gate matches our active google provider and tags
        # the finding with vendor/source + the right blast radius.
        cfg = self._cfg()
        rows = [("gemini-3-flash-preview", "2026-09-01", "gemini-3.5-flash")]
        findings = _eol_findings_from_rows(
            rows, cfg.providers, cfg.call_sites,
            provider_type="google", vendor="google",
            source="gemini_deprecations_page",
        )
        assert len(findings) == 1
        f = findings[0]
        assert f["model"] == "gemini-3-flash-preview"
        assert f["provider"] == "gemini-free"
        assert f["vendor"] == "google"
        assert f["shutdown_date"] == "2026-09-01"
        assert f["blast_radius"] == ["s_micro"]
        assert f["source"] == "gemini_deprecations_page"

    def test_google_gate_ignores_groq_model(self) -> None:
        # symmetric to the groq gate: a groq model string must not match under
        # the google gate (no cross-vendor false positives).
        cfg = self._cfg()
        rows = [("llama-3.3-70b-versatile", "2026-08-16", "x")]
        assert _eol_findings_from_rows(
            rows, cfg.providers, cfg.call_sites,
            provider_type="google", vendor="google",
            source="gemini_deprecations_page",
        ) == []


class TestActiveProvidersJob:
    """The wired job method, surfacing, dedup, and run() integration."""

    @pytest.mark.asyncio
    async def test_check_active_providers_fires(self, db) -> None:
        job = ModelIntelligenceJob(db=db)
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        cfg = load_config_from_string(_EOL_CONFIG_YAML)
        with (
            patch.object(job, "_fetch_groq_deprecations", AsyncMock(return_value=rows)),
            patch("genesis.routing.config.load_config", return_value=cfg),
        ):
            findings = await job._check_active_providers()
        assert len(findings) == 1
        assert findings[0]["model"] == "moonshotai/kimi-k2-instruct-0905"

    @pytest.mark.asyncio
    async def test_check_active_providers_fires_gemini(self, db) -> None:
        # The google vendor path runs independently of Groq and fires for an
        # active gemini provider with a dated shutdown.
        job = ModelIntelligenceJob(db=db)
        cfg = load_config_from_string(_EOL_CONFIG_YAML)
        gemini_rows = [("gemini-3-flash-preview", "2026-09-01", "gemini-3.5-flash")]
        with (
            patch.object(job, "_fetch_groq_deprecations", AsyncMock(return_value=[])),
            patch.object(
                job, "_fetch_gemini_deprecations",
                AsyncMock(return_value=gemini_rows),
            ),
            patch("genesis.routing.config.load_config", return_value=cfg),
        ):
            findings = await job._check_active_providers()
        google = [f for f in findings if f["vendor"] == "google"]
        assert len(google) == 1
        assert google[0]["model"] == "gemini-3-flash-preview"
        assert google[0]["provider"] == "gemini-free"

    @pytest.mark.asyncio
    async def test_per_vendor_gating_google_only(self, db) -> None:
        # google-only install: Gemini IS checked, Groq is never fetched.
        job = ModelIntelligenceJob(db=db)
        cfg = load_config_from_string(
            "providers:\n  g:\n    type: google\n    model: m\n    free: true\n"
            "call_sites:\n  s:\n    chain: [g]\n"
        )
        groq_fetch = AsyncMock(return_value=[])
        gem_fetch = AsyncMock(return_value=[])
        with (
            patch.object(job, "_fetch_groq_deprecations", groq_fetch),
            patch.object(job, "_fetch_gemini_deprecations", gem_fetch),
            patch("genesis.routing.config.load_config", return_value=cfg),
        ):
            findings = await job._check_active_providers()
        assert findings == []
        groq_fetch.assert_not_called()  # no Groq providers → never fetches Groq
        gem_fetch.assert_called_once()  # has Google provider → checks Gemini

    @pytest.mark.asyncio
    async def test_per_vendor_gating_groq_only(self, db) -> None:
        # groq-only install: Groq IS checked, Gemini is never fetched.
        job = ModelIntelligenceJob(db=db)
        cfg = load_config_from_string(
            "providers:\n  gq:\n    type: groq\n    model: m\n    free: true\n"
            "call_sites:\n  s:\n    chain: [gq]\n"
        )
        groq_fetch = AsyncMock(return_value=[])
        gem_fetch = AsyncMock(return_value=[])
        with (
            patch.object(job, "_fetch_groq_deprecations", groq_fetch),
            patch.object(job, "_fetch_gemini_deprecations", gem_fetch),
            patch("genesis.routing.config.load_config", return_value=cfg),
        ):
            findings = await job._check_active_providers()
        assert findings == []
        gem_fetch.assert_not_called()  # no Google providers → never fetches Gemini
        groq_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_active_providers_config_failure_is_safe(self, db) -> None:
        job = ModelIntelligenceJob(db=db)
        with patch("genesis.routing.config.load_config", side_effect=OSError("boom")):
            assert await job._check_active_providers() == []

    @pytest.mark.asyncio
    async def test_fetch_deprecations_failsafe_on_error(self, db) -> None:
        from genesis.web.types import FetchResult

        job = ModelIntelligenceJob(db=db)
        with patch(
            "genesis.web.fetch",
            AsyncMock(return_value=FetchResult(url="u", text="", error="503")),
        ):
            assert await job._fetch_groq_deprecations() == []

    @pytest.mark.asyncio
    async def test_fetch_deprecations_failsafe_on_exception(self, db) -> None:
        job = ModelIntelligenceJob(db=db)
        with patch("genesis.web.fetch", AsyncMock(side_effect=RuntimeError("net"))):
            assert await job._fetch_groq_deprecations() == []

    @pytest.mark.asyncio
    async def test_eol_surfaces_as_high_priority_finding_not_intake(self, db) -> None:
        """active_model_eol → recon observation (type='finding',
        category='active_model_eol', priority='high'), bypassing run_intake."""
        job = ModelIntelligenceJob(db=db)
        finding = {
            "type": "active_model_eol", "model": "x/y", "provider": "p",
            "vendor": "groq", "shutdown_date": "2026-08-16",
            "replacement": "z", "blast_radius": ["s1", "s2"],
            "source": "groq_deprecations_page",
        }
        await job._store_finding(finding)
        cur = await db.execute(
            "SELECT type, category, priority, content_hash FROM observations "
            "WHERE category = 'active_model_eol'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["type"] == "finding"
        assert rows[0]["priority"] == "high"
        assert rows[0]["content_hash"]

    @pytest.mark.asyncio
    async def test_eol_observation_dedups_on_model_and_date(self, db) -> None:
        job = ModelIntelligenceJob(db=db)
        finding = {
            "type": "active_model_eol", "model": "x/y",
            "shutdown_date": "2026-08-16", "blast_radius": [],
        }
        await job._store_finding(finding)
        await job._store_finding(finding)  # same (model, date) → deduped
        cur = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE category = 'active_model_eol'"
        )
        assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_run_wires_eol_into_summary_and_storage(self, db, tmp_path) -> None:
        """Built != wired: run() invokes the EOL check, counts it, stores it."""
        job = ModelIntelligenceJob(db=db)
        rows = _parse_groq_deprecation_table(_SAMPLE_GROQ_TABLE)
        cfg = load_config_from_string(_EOL_CONFIG_YAML)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("genesis.recon.model_intelligence._FREE_MODEL_CACHE_PATH",
                  tmp_path / "free_cache.json"),
            patch.object(job, "_fetch_groq_deprecations", AsyncMock(return_value=rows)),
            patch("genesis.routing.config.load_config", return_value=cfg),
        ):
            result = await job.run()
        assert result["eol_findings"] == 1
        cur = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE category = 'active_model_eol'"
        )
        assert (await cur.fetchone())[0] == 1
