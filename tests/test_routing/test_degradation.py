"""Tests for degradation tracking and call-site filtering."""


from genesis.routing.degradation import DegradationTracker, should_skip_call_site
from genesis.routing.types import DegradationLevel


class TestShouldSkipCallSite:
    def test_l0_skips_nothing(self):
        assert not should_skip_call_site("12_surplus_brainstorm", DegradationLevel.NORMAL)
        assert not should_skip_call_site("2_triage", DegradationLevel.NORMAL)

    def test_l1_skips_nothing(self):
        assert not should_skip_call_site("12_surplus_brainstorm", DegradationLevel.FALLBACK)

    def test_l2_skips_surplus_outreach_morning(self):
        assert should_skip_call_site("12_surplus_brainstorm", DegradationLevel.REDUCED)
        assert should_skip_call_site("13_morning_report", DegradationLevel.REDUCED)

    def test_l2_allows_micro_reflection(self):
        assert not should_skip_call_site("3_micro_reflection", DegradationLevel.REDUCED)

    def test_l3_skips_deep_reflection(self):
        assert should_skip_call_site("5_deep_reflection", DegradationLevel.ESSENTIAL)

    def test_l3_keeps_essentials(self):
        # 2_triage was previously in this list; removed 2026-05-10 when the
        # call site was deleted from the YAML and _L3_KEEP set.
        assert not should_skip_call_site("3_micro_reflection", DegradationLevel.ESSENTIAL)
        assert not should_skip_call_site("21_embeddings", DegradationLevel.ESSENTIAL)
        assert not should_skip_call_site("22_tagging", DegradationLevel.ESSENTIAL)

    def test_l4_l5_dont_skip(self):
        assert not should_skip_call_site("2_triage", DegradationLevel.MEMORY_IMPAIRED)
        assert not should_skip_call_site("12_surplus_brainstorm", DegradationLevel.LOCAL_COMPUTE_DOWN)


class TestDegradationTracker:
    def test_default_level(self):
        t = DegradationTracker()
        assert t.current_level == DegradationLevel.NORMAL

    def test_update_level(self):
        t = DegradationTracker()
        t.update(DegradationLevel.REDUCED)
        assert t.current_level == DegradationLevel.REDUCED

    def test_should_skip_delegates(self):
        t = DegradationTracker()
        t.update(DegradationLevel.REDUCED)
        assert t.should_skip("12_surplus_brainstorm")
        assert not t.should_skip("2_triage")
