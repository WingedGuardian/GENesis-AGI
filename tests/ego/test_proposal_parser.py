"""Tests for parse_proposal_decisions() — Telegram reply parser."""

import pytest

from genesis.ego.proposals import parse_proposal_decisions


class TestBulkOperations:
    def test_approve_all(self):
        assert parse_proposal_decisions("approve all") == {0: ("approved", None)}

    def test_approved_all(self):
        assert parse_proposal_decisions("approved all") == {0: ("approved", None)}

    def test_accept_all(self):
        assert parse_proposal_decisions("accept all") == {0: ("approved", None)}

    def test_go_ahead(self):
        assert parse_proposal_decisions("go ahead") == {0: ("approved", None)}

    def test_reject_all(self):
        assert parse_proposal_decisions("reject all") == {0: ("rejected", None)}

    def test_deny_all(self):
        assert parse_proposal_decisions("deny all") == {0: ("rejected", None)}

    def test_case_insensitive(self):
        assert parse_proposal_decisions("APPROVE ALL") == {0: ("approved", None)}
        assert parse_proposal_decisions("Reject All") == {0: ("rejected", None)}

    def test_whitespace_tolerance(self):
        assert parse_proposal_decisions("  approve all  ") == {0: ("approved", None)}


class TestNumberedDecisions:
    def test_number_first(self):
        result = parse_proposal_decisions("1 approve")
        assert result == {1: ("approved", None)}

    def test_word_first(self):
        result = parse_proposal_decisions("approve 1")
        assert result == {1: ("approved", None)}

    def test_reject_with_reason(self):
        result = parse_proposal_decisions("2 reject: too expensive")
        assert result == {2: ("rejected", "too expensive")}

    def test_multiple_comma_separated(self):
        result = parse_proposal_decisions("1 approve, 2 reject: bad idea")
        assert result == {
            1: ("approved", None),
            2: ("rejected", "bad idea"),
        }

    def test_multiple_newline_separated(self):
        result = parse_proposal_decisions("1 approve\n2 reject\n3 approve")
        assert result == {
            1: ("approved", None),
            2: ("rejected", None),
            3: ("approved", None),
        }

    def test_mixed_formats(self):
        result = parse_proposal_decisions("approve 1, 2 reject: not now")
        assert result == {
            1: ("approved", None),
            2: ("rejected", "not now"),
        }

    def test_yes_and_no_synonyms(self):
        result = parse_proposal_decisions("1 yes, 2 no")
        assert result == {
            1: ("approved", None),
            2: ("rejected", None),
        }

    def test_ok_synonym(self):
        result = parse_proposal_decisions("1 ok")
        assert result == {1: ("approved", None)}

    def test_skip_synonym(self):
        result = parse_proposal_decisions("1 skip")
        assert result == {1: ("rejected", None)}


class TestFallthrough:
    """Cases that should return empty dict (fall through to correction store)."""

    def test_bare_approve(self):
        """Bare 'approve' without 'all' or number must NOT match."""
        assert parse_proposal_decisions("approve") == {}

    def test_bare_reject(self):
        assert parse_proposal_decisions("reject") == {}

    def test_conversational_text(self):
        assert parse_proposal_decisions("sounds good to me") == {}

    def test_empty_string(self):
        assert parse_proposal_decisions("") == {}

    def test_random_sentence(self):
        assert parse_proposal_decisions("I think we should reconsider the approach") == {}

    def test_number_without_action(self):
        """A number alone shouldn't match."""
        assert parse_proposal_decisions("1") == {}

    def test_partial_match_ignored(self):
        """Unknown action words are skipped, not treated as failures."""
        result = parse_proposal_decisions("1 approve, 2 maybe")
        assert result == {1: ("approved", None)}  # Only valid one parsed


class TestEdgeCases:
    def test_zero_index_ignored(self):
        """Index 0 is invalid for numbered decisions."""
        assert parse_proposal_decisions("0 approve") == {}

    def test_negative_index_ignored(self):
        assert parse_proposal_decisions("-1 approve") == {}

    def test_large_index(self):
        result = parse_proposal_decisions("99 approve")
        assert result == {99: ("approved", None)}

    def test_reason_with_colon(self):
        result = parse_proposal_decisions("1 reject: reason: has colons")
        assert result[1][0] == "rejected"
        # Reason captures everything after first colon
        assert "reason" in result[1][1]
