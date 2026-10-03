"""Reference integrity: no automatic human labels or preflight model calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.eval.calibration import _load_golden_set, run_calibration
from genesis.eval.rubrics import get_rubric
from genesis.routing.types import RoutingResult


def reference():
    return {
        "id": "reference-1",
        "actual": "Specific test observation",
        "expected": "deep_reflection_observation",
        "user_passed": True,
        "scorer_config": {"rubric_name": "reflection_quality", "session_context": "Test context"},
        "reference_provenance": {
            "label_source": "human",
            "reviewer": "synthetic-test-reviewer",
            "rubric_version": get_rubric("reflection_quality").version,
        },
    }


def write(path: Path, cases):
    path.write_text("\n".join(json.dumps(case) for case in cases))
    return path


@pytest.mark.parametrize(
    "key,value",
    [
        ("id", None),
        ("id", 0),
        ("id", " "),
        ("actual", None),
        ("actual", []),
        ("actual", ""),
        ("user_passed", "false"),
        ("user_passed", 0),
        ("user_passed", 1),
        ("user_passed", None),
        ("expected", {}),
        ("scorer_config", None),
        ("scorer_config", []),
        ("scorer_config", {"rubric_name": "output_quality", "session_context": "x"}),
        ("scorer_config", {"rubric_name": "reflection_quality"}),
        ("scorer_config", {"rubric_name": "reflection_quality", "session_context": 3}),
        ("scorer_config", {"rubric_name": "reflection_quality", "session_context": ""}),
        ("reference_provenance", None),
        ("reference_provenance", []),
        ("reference_provenance", {"label_source": "model"}),
        ("reference_provenance", {"label_source": "human", "reviewer": " ", "rubric_version": "1"}),
        (
            "reference_provenance",
            {"label_source": "human", "reviewer": "test", "rubric_version": "wrong"},
        ),
    ],
)
async def test_strict_rejects_complete_file_before_any_call(tmp_path, key, value):
    bad = reference()
    bad[key] = value
    bad["id"] = "bad-id" if key != "id" else value
    router = AsyncMock()
    with pytest.raises(ValueError):
        await run_calibration(
            rubric="reflection_quality",
            golden_set_path=write(tmp_path / "references.jsonl", [reference(), bad]),
            router=router,
            strict_references=True,
        )
    router.route_call.assert_not_called()


@pytest.mark.parametrize("invalid", [None, [], True, 4, "text"])
def test_nonobject_json_rejected(tmp_path, invalid):
    with pytest.raises(ValueError, match="JSON object"):
        _load_golden_set(write(tmp_path / "references.jsonl", [invalid]))


def test_duplicate_ids_rejected_but_legacy_loading_preserved(tmp_path):
    p = write(tmp_path / "references.jsonl", [reference(), reference()])
    assert len(_load_golden_set(p)) == 2
    with pytest.raises(ValueError, match="duplicate id"):
        _load_golden_set(p, strict_rubric=get_rubric("reflection_quality"))


async def test_generator_draft_to_human_reference_calibration_e2e(tmp_path, monkeypatch):
    """Real SQLite + generator writer + validator + real scorer, stub inference."""
    import genesis.eval.reflection_golden_set as generator

    db = tmp_path / "source.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE fixture (id TEXT, content TEXT)")
        await conn.execute(
            "INSERT INTO fixture VALUES ('observation-1','Specific test observation')"
        )
        await conn.commit()
    real_connect = aiosqlite.connect
    monkeypatch.setattr(generator.aiosqlite, "connect", lambda *a, **kw: real_connect(db))

    async def sample(conn, count):
        cursor = await conn.execute("SELECT id,content FROM fixture")
        return [
            dict(
                id=row[0],
                content=row[1],
                created_at="2026-10-02T00:00:00Z",
                priority=1,
                retrieved_count=0,
            )
            for row in await cursor.fetchall()
        ]

    monkeypatch.setattr(generator, "_sample_observations", sample)
    monkeypatch.setattr(generator, "_get_session_context", AsyncMock(return_value="Test context"))
    grade = AsyncMock(return_value=(0.9, "Synthetic rationale", "synthetic-model"))
    monkeypatch.setattr(generator, "_grade_observation", grade)
    draft = tmp_path / "draft.jsonl"
    await generator.generate_golden_set(1, draft)
    rows = [json.loads(x) for x in draft.read_text().splitlines() if x and not x.startswith("#")]
    assert "user_passed" not in rows[0]
    assert rows[0]["proposed_passed"] is True
    assert rows[0]["reference_provenance"]["label_source"] == "model"
    router = AsyncMock()
    with pytest.raises(ValueError, match="user_passed"):
        await run_calibration(
            rubric="reflection_quality",
            golden_set_path=draft,
            router=router,
            strict_references=True,
        )
    router.route_call.assert_not_called()
    approved = copy.deepcopy(rows[0])
    approved["user_passed"] = True
    approved["reference_provenance"].update(
        label_source="human", reviewer="synthetic-test-reviewer"
    )
    router.route_call.return_value = RoutingResult(
        success=True,
        call_site_id="judge",
        content='{"score":0.9,"rationale":"Synthetic rationale"}',
        provider_used="synthetic-provider",
        model_id="synthetic-model",
    )
    result = await run_calibration(
        rubric="reflection_quality",
        golden_set_path=write(tmp_path / "synthetic-human-reference.jsonl", [approved]),
        router=router,
        strict_references=True,
    )
    assert result.agreed_cases == 1 and result.error_cases == 0
    router.route_call.assert_awaited_once()
    frozen = draft.read_bytes()
    grade.reset_mock()
    with pytest.raises(FileExistsError):
        await generator.generate_golden_set(1, draft)
    assert draft.read_bytes() == frozen
    grade.assert_not_called()


async def test_cli_preflight_rejects_legacy_file_before_router_allocation(tmp_path, monkeypatch):
    import genesis.eval.run_reflection_calibration as cli

    def forbidden(*args, **kwargs):
        pytest.fail("invalid strict references must not allocate a model client")

    monkeypatch.setattr(cli, "StandaloneLiteLLMRouter", forbidden)
    legacy = reference()
    legacy.pop("reference_provenance")
    with pytest.raises(ValueError, match="reference_provenance"):
        await cli.run(write(tmp_path / "legacy.jsonl", [legacy]), strict_references=True)


def test_strict_rejects_rubric_instance_different_from_scoring_registry(tmp_path):
    from dataclasses import replace

    rubric = get_rubric("reflection_quality")
    different = replace(rubric, prompt_template="different prompt")
    with pytest.raises(ValueError, match="registered rubric"):
        _load_golden_set(
            write(tmp_path / "references.jsonl", [reference()]), strict_rubric=different
        )


def test_generator_default_preserves_historical_reference_path():
    from genesis.eval.reflection_golden_set import DEFAULT_OUTPUT

    assert DEFAULT_OUTPUT.name == "reflection_quality_draft.jsonl"
