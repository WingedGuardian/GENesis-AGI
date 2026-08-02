"""Tests for ModelsMdSynthesisJob — weekly models.md synthesis."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.recon.models_md_synthesis import ModelsMdSynthesisJob

_SEED_CONTENT = (
    "<!-- header -->\n"
    "## THE HEAVY LIFTERS\nseed\n"
    "## THE ALL-ROUNDERS\nseed\n"
    "## THE SPECIALISTS\nseed\n"
)


def _job_with_one_finding():
    """A job whose DB returns exactly one actionable finding."""
    findings_data = [
        ("concept1", '{"type": "pricing_change", "model": "x"}', "2026-08-01"),
    ]
    mock_cursor = MagicMock()
    mock_cursor.fetchall = AsyncMock(return_value=findings_data)
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_cursor)
    return ModelsMdSynthesisJob(db=mock_db)


class _FakeRunner:
    """Captures the DirectSessionRequest instead of spawning a real session."""

    def __init__(self):
        self.request = None

    async def spawn(self, request):
        self.request = request
        return "sess-test"


def _patch_dispatch(monkeypatch, tmp_path, *, seed: bool = True):
    """Point output_dir()/repo_root() at tmp dirs and stub the runtime.

    Returns (overlay_dir, runner). When ``seed`` is True a tracked seed doc is
    written under the fake repo root so the overlay can seed from it.
    """
    overlay_dir = tmp_path / "output"
    repo = tmp_path / "repo"
    if seed:
        seed_doc = repo / "docs" / "reference" / "models.md"
        seed_doc.parent.mkdir(parents=True, exist_ok=True)
        seed_doc.write_text(_SEED_CONTENT)
    monkeypatch.setattr(
        "genesis.recon.models_md_synthesis.output_dir", lambda: overlay_dir
    )
    monkeypatch.setattr(
        "genesis.recon.models_md_synthesis.repo_root", lambda: repo
    )
    runner = _FakeRunner()
    fake_rt = MagicMock()
    fake_rt._direct_session_runner = runner
    monkeypatch.setattr("genesis.runtime.GenesisRuntime.instance", lambda: fake_rt)
    return overlay_dir, runner


class TestOverlayTarget:
    """The job writes a local overlay, seeds it once, and never git-commits."""

    @pytest.mark.asyncio
    async def test_seeds_overlay_and_points_prompt_at_it(self, monkeypatch, tmp_path):
        overlay_dir, runner = _patch_dispatch(monkeypatch, tmp_path)
        overlay = overlay_dir / "models.md"
        assert not overlay.exists()

        result = await _job_with_one_finding().run()

        assert result["dispatched"] is True
        # Overlay was seeded from the tracked reference doc.
        assert overlay.exists()
        assert overlay.read_text() == _SEED_CONTENT
        # Prompt targets the overlay, not the tracked doc.
        assert str(overlay) in runner.request.prompt
        assert "docs/reference/models.md" not in runner.request.prompt

    @pytest.mark.asyncio
    async def test_prompt_has_no_git_commit(self, monkeypatch, tmp_path):
        _, runner = _patch_dispatch(monkeypatch, tmp_path)
        await _job_with_one_finding().run()
        prompt = runner.request.prompt
        assert "git commit" not in prompt
        assert "git add" not in prompt

    @pytest.mark.asyncio
    async def test_tool_exceptions_write_only(self, monkeypatch, tmp_path):
        _, runner = _patch_dispatch(monkeypatch, tmp_path)
        await _job_with_one_finding().run()
        # Bash dropped — with git gone the session only needs Write.
        assert runner.request.tool_exceptions == ("Write",)

    @pytest.mark.asyncio
    async def test_existing_overlay_not_reseeded(self, monkeypatch, tmp_path):
        overlay_dir, runner = _patch_dispatch(monkeypatch, tmp_path)
        overlay = overlay_dir / "models.md"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("PRIOR OVERLAY CONTENT\n")

        await _job_with_one_finding().run()

        # Seed-once: an existing overlay is left as-is (the session updates it).
        assert overlay.read_text() == "PRIOR OVERLAY CONTENT\n"
        assert str(overlay) in runner.request.prompt

    @pytest.mark.asyncio
    async def test_no_seed_skips(self, monkeypatch, tmp_path):
        # No overlay and no tracked seed → cannot proceed.
        _, runner = _patch_dispatch(monkeypatch, tmp_path, seed=False)
        result = await _job_with_one_finding().run()
        assert result == {"skipped": True, "reason": "no_seed"}
        assert runner.request is None  # never dispatched


class TestSynthesisLever:
    """The GENESIS_MODELS_MD_SYNTHESIS_OFF operator lever."""

    def test_default_on(self, monkeypatch):
        from genesis import env

        monkeypatch.delenv("GENESIS_MODELS_MD_SYNTHESIS_OFF", raising=False)
        monkeypatch.setattr(env, "_local_config", lambda: {})
        assert env.models_md_synthesis_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", " On "])
    def test_env_off(self, monkeypatch, val):
        from genesis import env

        monkeypatch.setenv("GENESIS_MODELS_MD_SYNTHESIS_OFF", val)
        assert env.models_md_synthesis_enabled() is False

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_env_falsey_stays_on(self, monkeypatch, val):
        from genesis import env

        monkeypatch.setenv("GENESIS_MODELS_MD_SYNTHESIS_OFF", val)
        assert env.models_md_synthesis_enabled() is True

    def test_local_config_disable(self, monkeypatch):
        from genesis import env

        monkeypatch.delenv("GENESIS_MODELS_MD_SYNTHESIS_OFF", raising=False)
        monkeypatch.setattr(
            env, "_local_config", lambda: {"models_md_synthesis": {"enabled": False}}
        )
        assert env.models_md_synthesis_enabled() is False

    @pytest.mark.asyncio
    async def test_runner_short_circuits_when_disabled(self, monkeypatch):
        from genesis.surplus.jobs import runners

        monkeypatch.setenv("GENESIS_MODELS_MD_SYNTHESIS_OFF", "1")
        sched = MagicMock()
        sched._models_md_synthesis_job = MagicMock()
        sched._models_md_synthesis_job.run = AsyncMock()

        await runners.run_models_md_synthesis(sched)

        # Disabled → the job is never invoked.
        sched._models_md_synthesis_job.run.assert_not_called()


class TestParseBody:
    """Test finding body parsing (prefix + JSON format)."""

    def test_standard_body(self):
        body = 'Model Intelligence\n\n{"type": "pricing_change", "model": "test"}'
        result = ModelsMdSynthesisJob._parse_body(body)
        assert result == {"type": "pricing_change", "model": "test"}

    def test_json_only(self):
        body = '{"type": "new_free_model", "api_id": "test/model"}'
        result = ModelsMdSynthesisJob._parse_body(body)
        assert result == {"type": "new_free_model", "api_id": "test/model"}

    def test_no_json(self):
        assert ModelsMdSynthesisJob._parse_body("plain text no json") is None

    def test_invalid_json(self):
        assert ModelsMdSynthesisJob._parse_body("{invalid json}") is None


class TestSerializeFindings:
    """Test finding serialization for LLM prompt."""

    def test_compact_output(self):
        findings = [
            {"type": "pricing_change", "title": "Model intelligence: pricing_change — gpt-5", "model": "gpt-5"},
        ]
        result = ModelsMdSynthesisJob._serialize_findings(findings)
        assert "pricing_change — gpt-5 (pricing_change)" in result
        assert "```json" in result
        assert '"model": "gpt-5"' in result

    def test_strips_prefix(self):
        findings = [{"type": "new_free_model", "title": "Model intelligence: new — test"}]
        result = ModelsMdSynthesisJob._serialize_findings(findings)
        assert "Model intelligence: " not in result
        assert "new — test" in result

    def test_empty_findings(self):
        assert ModelsMdSynthesisJob._serialize_findings([]) == ""


class TestValidateOutput:
    """Test output validation gates."""

    MINIMAL_VALID = (
        "<!-- header -->\n"
        "## THE HEAVY LIFTERS\nContent\n"
        "## THE ALL-ROUNDERS\nContent\n"
        "## THE SPECIALISTS\nContent\n"
    )

    def test_valid_output(self):
        original = self.MINIMAL_VALID
        # Same-ish length output with all markers
        assert ModelsMdSynthesisJob._validate_output(original, original) is None

    def test_empty_output(self):
        assert ModelsMdSynthesisJob._validate_output("", "original") == "empty output"

    def test_missing_marker(self):
        output = "## THE HEAVY LIFTERS\n## THE ALL-ROUNDERS\n"
        err = ModelsMdSynthesisJob._validate_output(output, self.MINIMAL_VALID)
        assert err is not None
        assert "THE SPECIALISTS" in err

    def test_too_short(self):
        output = self.MINIMAL_VALID
        # Original is 3x longer
        original = self.MINIMAL_VALID * 3
        err = ModelsMdSynthesisJob._validate_output(output, original)
        assert err is not None
        assert "<50%" in err

    def test_too_long(self):
        output = self.MINIMAL_VALID * 5
        original = self.MINIMAL_VALID
        err = ModelsMdSynthesisJob._validate_output(output, original)
        assert err is not None
        assert ">200%" in err


class TestQueryFindings:
    """Test finding query and type filtering."""

    @pytest.mark.asyncio
    async def test_filters_to_actionable_types(self):
        """Only actionable finding types should be returned."""
        from unittest.mock import AsyncMock, MagicMock

        # Mock DB with mixed finding types
        findings_data = [
            ("concept1", 'prefix\n\n{"type": "pricing_change", "model": "test"}', "2026-05-22"),
            ("concept2", 'prefix\n\n{"type": "new_model", "api_id": "test/bulk"}', "2026-05-22"),
            ("concept3", 'prefix\n\n{"type": "new_free_model", "api_id": "test/free"}', "2026-05-22"),
            ("concept4", 'prefix\n\n{"type": "benchmark_unmatched", "source": "aa"}', "2026-05-22"),
        ]

        mock_cursor = MagicMock()
        mock_cursor.fetchall = AsyncMock(return_value=findings_data)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        job = ModelsMdSynthesisJob(db=mock_db)
        findings = await job._query_findings()

        # Should include pricing_change and new_free_model, exclude new_model and benchmark_unmatched
        types = {f["type"] for f in findings}
        assert types == {"pricing_change", "new_free_model"}
        assert len(findings) == 2
