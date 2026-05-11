"""Tests for the subsystem-filter resolver and ObservationWriter source map."""

from __future__ import annotations

import pytest

from genesis.learning.observation_writer import _SUBSYSTEM_FROM_SOURCE
from genesis.memory.retrieval import _KNOWN_SUBSYSTEMS, _resolve_subsystem_filter


class TestResolveSubsystemFilter:
    """Translate the public API into (exclude, include_only) primitives."""

    def test_default_excludes_all_known(self) -> None:
        exclude, include_only = _resolve_subsystem_filter(False, None)
        assert exclude == list(_KNOWN_SUBSYSTEMS)
        assert include_only is None

    def test_include_true_disables_filter(self) -> None:
        exclude, include_only = _resolve_subsystem_filter(True, None)
        assert exclude is None
        assert include_only is None

    def test_include_list_keeps_named(self) -> None:
        """include_subsystem=['ego'] should preserve user + ego."""
        exclude, include_only = _resolve_subsystem_filter(["ego"], None)
        assert exclude == ["triage", "reflection"]
        assert include_only is None

    def test_include_list_multiple(self) -> None:
        exclude, include_only = _resolve_subsystem_filter(
            ["ego", "reflection"], None,
        )
        assert exclude == ["triage"]
        assert include_only is None

    def test_only_subsystem_string(self) -> None:
        exclude, include_only = _resolve_subsystem_filter(False, "ego")
        assert exclude is None
        assert include_only == ["ego"]

    def test_only_subsystem_list(self) -> None:
        exclude, include_only = _resolve_subsystem_filter(
            False, ["ego", "triage"],
        )
        assert exclude is None
        assert include_only == ["ego", "triage"]

    def test_mutual_exclusion_raises(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _resolve_subsystem_filter(["ego"], "triage")

    def test_mutual_exclusion_include_true_with_only(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            _resolve_subsystem_filter(True, "ego")

    def test_empty_only_subsystem_list_raises(self) -> None:
        """Silent disable is footgun-prone; require explicit non-empty list."""
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_subsystem_filter(False, [])

    def test_empty_only_subsystem_string_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_subsystem_filter(False, "")

    def test_empty_include_subsystem_list_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _resolve_subsystem_filter([], None)

    def test_known_subsystems_matches_observation_writer_map(self) -> None:
        """Sanity check: every value in the writer map must be a known subsystem."""
        for subsystem in _SUBSYSTEM_FROM_SOURCE.values():
            assert subsystem in _KNOWN_SUBSYSTEMS, (
                f"observation_writer maps to unknown subsystem {subsystem!r}; "
                f"add it to _KNOWN_SUBSYSTEMS or remove from the map"
            )


class TestObservationWriterSubsystemMap:
    """ObservationWriter's source→subsystem mapping."""

    def test_reflection_sources_tagged(self) -> None:
        reflection_sources = {
            "stability_monitor", "deep_reflection", "light_reflection",
            "micro_reflection", "quality_calibration", "weekly_assessment",
            "surplus_promotion", "retrospective",
        }
        for src in reflection_sources:
            assert _SUBSYSTEM_FROM_SOURCE.get(src) == "reflection", (
                f"{src!r} should map to 'reflection'"
            )

    def test_user_sources_stay_unmapped(self) -> None:
        """User-sourced inputs must NOT be tagged — they belong in foreground recall."""
        user_sources = ("user_reply", "direct_message")
        for src in user_sources:
            assert src not in _SUBSYSTEM_FROM_SOURCE, (
                f"{src!r} is user-sourced; should not be in subsystem map"
            )

    def test_extraction_sources_stay_unmapped(self) -> None:
        """Auto-extracted content originates with the user; preserve in recall."""
        extraction_sources = ("auto_memory_harvest", "cc_debrief")
        for src in extraction_sources:
            assert src not in _SUBSYSTEM_FROM_SOURCE

    def test_unknown_source_returns_none(self) -> None:
        """Unknown sources must fall through to NULL (user-sourced) by default."""
        assert _SUBSYSTEM_FROM_SOURCE.get("future_unknown_source") is None


class TestCrossProcessSubsystemListSync:
    """The proactive_memory_hook runs in a subprocess and duplicates
    _KNOWN_SUBSYSTEMS as _PROACTIVE_EXCLUDED_SUBSYSTEMS. Catch drift in CI
    rather than at runtime — both lists must match exactly.
    """

    def test_proactive_hook_subsystem_list_matches_known(self) -> None:
        import importlib.util
        import pathlib

        hook_path = pathlib.Path(__file__).parents[2] / "scripts" / "proactive_memory_hook.py"
        spec = importlib.util.spec_from_file_location(
            "proactive_memory_hook", hook_path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        hook_list = module._PROACTIVE_EXCLUDED_SUBSYSTEMS
        assert tuple(hook_list) == _KNOWN_SUBSYSTEMS, (
            "proactive_memory_hook._PROACTIVE_EXCLUDED_SUBSYSTEMS must "
            "match genesis.memory.retrieval._KNOWN_SUBSYSTEMS exactly. "
            f"Got hook={tuple(hook_list)} vs known={_KNOWN_SUBSYSTEMS}"
        )
