"""Tests for sweep_fetch.py — structured JD sweep script."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from sweep_fetch import (  # noqa: E402
    FetchedJD,
    PageCache,
    SweepRunner,
    SweepSpec,
    _title_matches_hints,
    fuzzy_match,
    generate_slugs,
    normalize_title,
    parse_spec,
    try_ashby,
    try_greenhouse,
    try_lever,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def minimal_spec_yaml(tmp_path: Path) -> Path:
    """Create a minimal valid spec YAML file."""
    spec = {
        "sweep": {
            "name": "test_sweep",
            "goal": "Test sweep",
            "owner_context": {"user": "tester"},
        },
        "cost_controls": {
            "target_total_tokens": 1000,
            "page_cache_ttl_hours": 1,
        },
        "required_tiers": ["tier_1"],
        "optional_tiers": [],
        "tiers": {
            "tier_1": {
                "companies": [
                    {"name": "TestCorp", "greenhouse_slug": "testcorp"},
                    "SimpleCo",
                ],
                "target_jds": "5-10",
                "title_search_hints": ["Engineer", "Developer"],
            }
        },
        "per_jd_extraction_schema": {
            "title_exact": "string",
            "company": "string",
            "location": "string",
        },
        "output": {
            "raw_json": "data/test-jds.json",
            "synthesis": "test-synthesis.md",
        },
    }
    path = tmp_path / "test-spec.yml"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


@pytest.fixture
def spec(minimal_spec_yaml: Path) -> SweepSpec:
    return parse_spec(minimal_spec_yaml)


# ── Spec Parsing ──────────────────────────────────────────────────────


class TestSpecParsing:
    def test_valid_spec(self, minimal_spec_yaml: Path):
        spec = parse_spec(minimal_spec_yaml)
        assert spec.name == "test_sweep"
        assert spec.required_tiers == ["tier_1"]
        assert "tier_1" in spec.tiers

    def test_missing_name(self, tmp_path: Path):
        path = tmp_path / "bad.yml"
        path.write_text("sweep:\n  goal: test\n", encoding="utf-8")
        with pytest.raises(ValueError, match="sweep.name"):
            parse_spec(path)

    def test_empty_spec(self, tmp_path: Path):
        path = tmp_path / "empty.yml"
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="sweep.name"):
            parse_spec(path)

    def test_reuse_prior_jds_parsed(self, tmp_path: Path):
        spec_data = {
            "sweep": {"name": "test"},
            "reuse_prior_jds": {
                "source": "data/prior.json",
                "filter": [{"company": "Acme", "title_contains": "Engineer"}],
            },
            "per_jd_extraction_schema": {},
            "output": {},
        }
        path = tmp_path / "reuse.yml"
        path.write_text(json.dumps(spec_data), encoding="utf-8")
        s = parse_spec(path)
        assert s.reuse_prior_jds is not None
        assert s.reuse_prior_jds["source"] == "data/prior.json"


# ── Slug Generation ───────────────────────────────────────────────────


class TestSlugGeneration:
    def test_simple_name(self):
        slugs = generate_slugs("Anthropic")
        assert "anthropic" in slugs
        assert "anthropic-ai" in slugs
        assert "anthropicai" in slugs

    def test_multi_word(self):
        slugs = generate_slugs("Scale AI")
        assert "scale-ai" in slugs
        assert "scaleai" in slugs

    def test_special_chars(self):
        slugs = generate_slugs("Together.AI")
        assert "together-ai" in slugs

    def test_no_duplicates(self):
        slugs = generate_slugs("TestCo")
        assert len(slugs) == len(set(slugs))


# ── Title Matching ────────────────────────────────────────────────────


class TestTitleMatching:
    def test_match(self):
        assert _title_matches_hints("Senior Research Engineer", ["Research Engineer"])

    def test_case_insensitive(self):
        assert _title_matches_hints("forward deployed engineer", ["Forward Deployed"])

    def test_no_match(self):
        assert not _title_matches_hints("Product Manager", ["Engineer", "Developer"])


# ── Title Normalization ───────────────────────────────────────────────


class TestTitleNormalization:
    def test_basic(self):
        assert normalize_title("Research Engineer") == normalize_title("Research Engineer")

    def test_removes_level(self):
        a = normalize_title("Senior Research Engineer")
        b = normalize_title("Research Engineer")
        assert a == b

    def test_removes_location_suffix(self):
        a = normalize_title("ML Engineer - Remote")
        b = normalize_title("ML Engineer")
        assert a == b

    def test_removes_parens(self):
        a = normalize_title("Engineer (US)")
        b = normalize_title("Engineer")
        assert a == b

    def test_order_invariant(self):
        a = normalize_title("Research Engineer")
        b = normalize_title("Engineer Research")
        assert a == b


# ── Fuzzy Matching ────────────────────────────────────────────────────


class TestFuzzyMatch:
    def test_identical(self):
        assert fuzzy_match("engineer research", "engineer research")

    def test_similar(self):
        a = normalize_title("Senior Research Engineer")
        b = normalize_title("Research Engineer, Senior")
        assert fuzzy_match(a, b)

    def test_different(self):
        a = normalize_title("Research Engineer")
        b = normalize_title("Product Manager")
        assert not fuzzy_match(a, b)


# ── ATS API Parsing ───────────────────────────────────────────────────


class TestATSParsing:
    @pytest.mark.asyncio
    async def test_greenhouse_parses(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs": [
                {
                    "title": "Applied Research Engineer",
                    "absolute_url": "https://boards.greenhouse.io/test/jobs/123",
                    "location": {"name": "San Francisco"},
                    "content": "<p>Job description</p>",
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        result = await try_greenhouse(client, "test", ["Research Engineer"])
        assert len(result) == 1
        assert result[0]["title"] == "Applied Research Engineer"
        assert result[0]["source_ats"] == "greenhouse"

    @pytest.mark.asyncio
    async def test_greenhouse_404(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        result = await try_greenhouse(client, "nonexistent", ["Engineer"])
        assert result == []

    @pytest.mark.asyncio
    async def test_greenhouse_filters_by_hints(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs": [
                {"title": "Research Engineer", "absolute_url": "url1", "location": {"name": ""}, "content": ""},
                {"title": "Accountant", "absolute_url": "url2", "location": {"name": ""}, "content": ""},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        result = await try_greenhouse(client, "test", ["Engineer"])
        assert len(result) == 1
        assert result[0]["title"] == "Research Engineer"

    @pytest.mark.asyncio
    async def test_ashby_parses(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jobs": [
                {"title": "FDE", "jobUrl": "https://ashby.io/jobs/123", "location": "Remote"}
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        result = await try_ashby(client, "test", ["FDE"])
        assert len(result) == 1
        assert result[0]["source_ats"] == "ashby"

    @pytest.mark.asyncio
    async def test_lever_parses(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "text": "Applied Engineer",
                "hostedUrl": "https://lever.co/test/123",
                "categories": {"location": "NYC"},
            }
        ]
        mock_response.raise_for_status = MagicMock()
        client = AsyncMock()
        client.get = AsyncMock(return_value=mock_response)

        result = await try_lever(client, "test", ["Engineer"])
        assert len(result) == 1
        assert result[0]["source_ats"] == "lever"


# ── Page Cache ────────────────────────────────────────────────────────


class TestPageCache:
    def test_put_and_get(self):
        with patch.dict("sweep_fetch.__dict__", {"_HAS_DISKCACHE": False}):
            cache = PageCache(ttl_hours=1)
            # Without diskcache, returns None
            cache.put("https://example.com", "content")
            assert cache.get("https://example.com") is None

    def test_with_diskcache(self, tmp_path):
        try:
            import diskcache  # noqa: F401
        except ImportError:
            pytest.skip("diskcache not installed")
        cache = PageCache(ttl_hours=1)
        cache.put("https://example.com/test", "test content")
        assert cache.get("https://example.com/test") == "test content"

    def test_miss(self):
        cache = PageCache(ttl_hours=1)
        assert cache.get("https://nonexistent.com") is None


# ── Dedup ─────────────────────────────────────────────────────────────


class TestDedup:
    def test_dedup_same_title(self, spec):
        runner = SweepRunner(spec, dry_run=True)
        jds = [
            FetchedJD("url1", "Acme", "t1", "Senior Research Engineer", "md1", "greenhouse_api"),
            FetchedJD("url2", "Acme", "t1", "Research Engineer, Senior", "md2", "web_fetch"),
        ]
        result = runner._phase_dedup(jds)
        assert len(result) == 1
        assert result[0].source == "greenhouse_api"  # Higher priority

    def test_keeps_different_titles(self, spec):
        runner = SweepRunner(spec, dry_run=True)
        jds = [
            FetchedJD("url1", "Acme", "t1", "Research Engineer", "md1", "greenhouse_api"),
            FetchedJD("url2", "Acme", "t1", "Product Manager", "md2", "web_fetch"),
        ]
        result = runner._phase_dedup(jds)
        assert len(result) == 2

    def test_dedup_cross_company(self, spec):
        runner = SweepRunner(spec, dry_run=True)
        jds = [
            FetchedJD("url1", "Acme", "t1", "Engineer", "md1", "greenhouse_api"),
            FetchedJD("url2", "Other", "t1", "Engineer", "md2", "web_fetch"),
        ]
        result = runner._phase_dedup(jds)
        assert len(result) == 2  # Different companies, no dedup


# ── Dry Run ───────────────────────────────────────────────────────────


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_plan(self, spec):
        runner = SweepRunner(spec, dry_run=True)
        result = await runner.run()
        assert result["dry_run"] is True
        assert result["total_companies"] == 2  # TestCorp + SimpleCo


# ── Company Entry Parsing ─────────────────────────────────────────────


class TestCompanyEntryParsing:
    def test_string_entry(self):
        name, explicit = SweepRunner._parse_company_entry("Anthropic")
        assert name == "Anthropic"
        assert explicit == {}

    def test_dict_entry(self):
        entry = {"name": "Anthropic", "greenhouse_slug": "anthropic"}
        name, explicit = SweepRunner._parse_company_entry(entry)
        assert name == "Anthropic"
        assert explicit["greenhouse_slug"] == "anthropic"

    def test_dict_with_careers_url(self):
        entry = {"name": "OpenAI", "careers_url": "https://openai.com/careers"}
        name, explicit = SweepRunner._parse_company_entry(entry)
        assert name == "OpenAI"
        assert explicit["careers_url"] == "https://openai.com/careers"


# ── JD URL Heuristic ─────────────────────────────────────────────────


class TestJDURLHeuristic:
    def test_greenhouse_url(self):
        assert SweepRunner._is_jd_url("https://boards.greenhouse.io/anthropic/jobs/12345")

    def test_lever_url(self):
        assert SweepRunner._is_jd_url("https://jobs.lever.co/company/some-role-id")

    def test_landing_page(self):
        assert not SweepRunner._is_jd_url("https://company.com/careers")

    def test_generic_url(self):
        assert not SweepRunner._is_jd_url("https://company.com/about")


# ── Output ────────────────────────────────────────────────────────────


class TestOutput:
    def test_write_synthesis(self, spec, tmp_path):
        runner = SweepRunner(spec, output_dir=tmp_path)
        jds = [
            {"_company": "Acme", "_tier": "tier_1", "_url": "url1", "_source": "greenhouse_api"},
            {"_company": "Other", "_tier": "tier_1", "_url": "url2", "_source": "web_crawl"},
        ]
        path = tmp_path / "synthesis.md"
        runner._write_synthesis(path, jds)
        content = path.read_text()
        assert "2" in content  # Total JDs
        assert "Acme" in content

    def test_write_narrative(self, spec, tmp_path):
        runner = SweepRunner(spec, output_dir=tmp_path)
        jds = [
            {"_company": "Acme", "title_exact": "Engineer", "location": "SF", "remote_policy": "hybrid", "_source": "ats"},
        ]
        path = tmp_path / "tier-1.md"
        runner._write_narrative(path, "tier-1", jds)
        content = path.read_text()
        assert "Acme" in content
        assert "Engineer" in content

    def test_write_empty_narrative(self, spec, tmp_path):
        runner = SweepRunner(spec, output_dir=tmp_path)
        path = tmp_path / "empty.md"
        runner._write_narrative(path, "empty", [])
        assert "No JDs found" in path.read_text()


# ── LLM Extraction ────────────────────────────────────────────────────


class TestExtractFields:
    @pytest.mark.asyncio
    async def test_success(self):
        from sweep_fetch import extract_fields

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"title_exact": "Engineer", "company": "Acme"}'
        mock_response.usage = MagicMock(total_tokens=100)

        tokens = [0]
        with patch("sweep_fetch._HAS_LITELLM", True), \
             patch("sweep_fetch._dotenv_loaded", True), \
             patch("litellm.acompletion", AsyncMock(return_value=mock_response)):
            result = await extract_fields(
                "Job description text",
                {"title_exact": "string", "company": "string"},
                ["gemini_flash"],
                tokens,
            )

        assert result is not None
        assert result["title_exact"] == "Engineer"
        assert tokens[0] == 100

    @pytest.mark.asyncio
    async def test_json_in_code_block(self):
        from sweep_fetch import extract_fields

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '```json\n{"title_exact": "Dev"}\n```'
        mock_response.usage = MagicMock(total_tokens=50)

        tokens = [0]
        with patch("sweep_fetch._HAS_LITELLM", True), \
             patch("sweep_fetch._dotenv_loaded", True), \
             patch("litellm.acompletion", AsyncMock(return_value=mock_response)):
            result = await extract_fields(
                "JD text", {"title_exact": "string"}, ["gemini_flash"], tokens,
            )

        assert result is not None
        assert result["title_exact"] == "Dev"

    @pytest.mark.asyncio
    async def test_fallback_on_error(self):
        from sweep_fetch import extract_fields

        good_response = MagicMock()
        good_response.choices = [MagicMock()]
        good_response.choices[0].message.content = '{"title_exact": "OK"}'
        good_response.usage = MagicMock(total_tokens=50)

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first model failed")
            return good_response

        tokens = [0]
        with patch("sweep_fetch._HAS_LITELLM", True), \
             patch("sweep_fetch._dotenv_loaded", True), \
             patch("litellm.acompletion", AsyncMock(side_effect=side_effect)):
            result = await extract_fields(
                "JD text", {"title_exact": "string"},
                ["ollama_local", "gemini_flash"],
                tokens,
            )

        assert result is not None
        assert result["title_exact"] == "OK"
        assert call_count == 2  # First failed, second succeeded

    @pytest.mark.asyncio
    async def test_no_litellm(self):
        from sweep_fetch import extract_fields

        with patch("sweep_fetch._HAS_LITELLM", False):
            result = await extract_fields("text", {}, ["gemini_flash"], [0])
        assert result is None


# ── Reuse Prior JDs ──────────────────────────────────────────────────


class TestReuse:
    def test_reuse_matches_filter(self, tmp_path):
        prior_data = [
            {"company": "Sakana AI", "title_exact": "Applied Research Engineer", "url": "url1"},
            {"company": "Other", "title_exact": "Manager", "url": "url2"},
        ]
        prior_path = tmp_path / "prior.json"
        prior_path.write_text(json.dumps(prior_data), encoding="utf-8")

        spec = SweepSpec(
            name="test",
            goal="",
            owner_context={},
            cost_controls={},
            required_tiers=[],
            optional_tiers=[],
            tiers={},
            extraction_schema={},
            output={},
            reuse_prior_jds={
                "source": "prior.json",
                "filter": [{"company": "Sakana", "title_contains": "Research"}],
            },
            spec_dir=tmp_path,
        )
        runner = SweepRunner(spec)
        result = runner._phase_reuse()
        assert len(result) == 1
        assert result[0]["company"] == "Sakana AI"
        assert result[0]["_source"] == "prior_sweep"

    def test_reuse_does_not_mutate_input(self, tmp_path):
        prior_data = [
            {"company": "Acme", "title_exact": "Engineer", "url": "url1"},
        ]
        prior_path = tmp_path / "prior.json"
        prior_path.write_text(json.dumps(prior_data), encoding="utf-8")
        spec = SweepSpec(
            name="test", goal="", owner_context={}, cost_controls={},
            required_tiers=[], optional_tiers=[], tiers={},
            extraction_schema={}, output={},
            reuse_prior_jds={"source": "prior.json", "filter": [{"company": "Acme", "title_contains": "Engineer"}]},
            spec_dir=tmp_path,
        )
        runner = SweepRunner(spec)
        result = runner._phase_reuse()
        # The original data should NOT have _source added
        reloaded = json.loads(prior_path.read_text())
        assert "_source" not in reloaded[0]
        assert result[0]["_source"] == "prior_sweep"

    def test_reuse_missing_file(self, tmp_path):
        spec = SweepSpec(
            name="test",
            goal="",
            owner_context={},
            cost_controls={},
            required_tiers=[],
            optional_tiers=[],
            tiers={},
            extraction_schema={},
            output={},
            reuse_prior_jds={"source": "nonexistent.json", "filter": []},
            spec_dir=tmp_path,
        )
        runner = SweepRunner(spec)
        result = runner._phase_reuse()
        assert result == []
