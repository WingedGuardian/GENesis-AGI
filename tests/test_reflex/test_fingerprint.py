"""Tests for reflex fingerprinting — pure functions, no I/O.

Fingerprint contract (reflex-arc spec §4.1 + plan):
- keyed on (normalized task name, error type, normalized frame tail)
- the exception MESSAGE is deliberately NOT an input (varies per occurrence)
- stable across deploys (frames carry no line numbers — emit side guarantees)
"""

from __future__ import annotations

from genesis.reflex.fingerprint import (
    class_key,
    derive_subsystem,
    fingerprint,
    normalize_task_name,
)

FRAMES = ["memory/sync.py:_apply_delta", "memory/store.py:get_entity"]


class TestNormalizeTaskName:
    def test_lowercases(self):
        assert normalize_task_name("Memory-Sync-Loop") == "memory-sync-loop"

    def test_scrubs_long_hex_runs(self):
        # uuid4-hex fragments embedded in dynamic task names must not split
        # fingerprints across occurrences
        assert normalize_task_name("obs-a1b2c3d4e5f6a7b8") == "obs-#"

    def test_scrubs_long_digit_runs(self):
        assert normalize_task_name("job-123456") == "job-#"

    def test_keeps_short_digits(self):
        # version-ish suffixes are identity, not noise
        assert normalize_task_name("phase2-tick") == "phase2-tick"

    def test_two_dynamic_names_collapse(self):
        a = normalize_task_name("session-0a1b2c3d4e5f6789-poll")
        b = normalize_task_name("session-ffee00112233ddcc-poll")
        assert a == b == "session-#-poll"


class TestFingerprint:
    def test_deterministic(self):
        assert fingerprint("t", "KeyError", FRAMES) == fingerprint("t", "KeyError", FRAMES)

    def test_hex16(self):
        fp = fingerprint("t", "KeyError", FRAMES)
        assert len(fp) == 16
        int(fp, 16)  # raises if not hex

    def test_task_name_distinguishes_shared_deep_frames(self):
        # Anti-over-collapse: two different tasks failing through the same
        # shared utility frames must NOT merge into one signal
        assert fingerprint("memory-sync", "TimeoutError", FRAMES) != fingerprint(
            "outreach-poll", "TimeoutError", FRAMES
        )

    def test_error_type_distinguishes(self):
        assert fingerprint("t", "KeyError", FRAMES) != fingerprint("t", "ValueError", FRAMES)

    def test_frames_distinguish(self):
        other = ["routing/router.py:route_call"]
        assert fingerprint("t", "KeyError", FRAMES) != fingerprint("t", "KeyError", other)

    def test_dynamic_task_name_does_not_split(self):
        # occurrence 1 and 2 of the same failing dynamic task → one fingerprint
        a = fingerprint("obs-a1b2c3d4e5f6a7b8", "KeyError", FRAMES)
        b = fingerprint("obs-ffee00112233ddcc", "KeyError", FRAMES)
        assert a == b

    def test_empty_frames_fallback_stable(self):
        # rolling deploy: old-process events carry no frames — coarser but stable
        assert fingerprint("t", "KeyError", []) == fingerprint("t", "KeyError", [])
        assert fingerprint("t", "KeyError", []) != fingerprint("t", "ValueError", [])


class TestDeriveSubsystem:
    def test_deepest_frame_top_package(self):
        # frames are outermost→innermost (emit side keeps extract_tb order);
        # the DEEPEST (last) frame names the failing subsystem
        assert (
            derive_subsystem(["awareness/loop.py:_tick", "memory/store.py:get"], "health")
            == "memory"
        )

    def test_basename_frame_falls_back(self):
        # emit side falls back to basename when no genesis/ segment — no package info
        assert derive_subsystem(["tasks.py:foo"], "health") == "health"

    def test_empty_frames_falls_back(self):
        assert derive_subsystem([], "health") == "health"


class TestClassKey:
    def test_format(self):
        assert class_key("KeyError", "memory") == "KeyErrorxmemory"
