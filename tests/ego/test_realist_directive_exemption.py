"""Tests for the realist's directive-scoped exemption (PR-3, LLM-judgment).

The old design bypassed the whole realist gate when any *critical* directive
was active. PR-3 replaces that with showing active critical/high directives to
the realist so IT judges whether a draft addresses one (the ego cannot
self-grant). These cover the prompt-construction contract: the directives block
+ Rule #2 carve-out render only when directives are present, content is
sanitized, and — critically — proposal index parity is preserved (index is the
only binding contract between a proposal and its verdict).
"""

from genesis.ego.session import _build_realist_prompt


def _props(*contents):
    return [{"action_type": "maintenance", "content": c, "confidence": 0.7} for c in contents]


class TestDirectiveExemption:
    def test_no_directives_no_block(self):
        prompt = _build_realist_prompt(_props("Do X"), [])
        assert "Active User Directives" not in prompt
        assert "Directive exemption" not in prompt

    def test_empty_directives_no_block(self):
        prompt = _build_realist_prompt(_props("Do X"), [], active_directives=[])
        assert "Active User Directives" not in prompt

    def test_directives_rendered_with_carveout(self):
        directives = [
            {"id": "abc123", "priority": "high", "content": "Weekly install test"},
        ]
        prompt = _build_realist_prompt(
            _props("Run the install test"), [], active_directives=directives
        )
        assert "## Active User Directives" in prompt
        assert "[HIGH]" in prompt
        assert "id=abc123" in prompt
        assert "Weekly install test" in prompt
        # Carve-out suspends Rule #2 (zombie/duplicate) for matching drafts...
        assert "Directive exemption" in prompt
        assert "Zombie" in prompt and "Duplicate" in prompt
        # ...but explicitly keeps the other rules in force.
        assert "feasibility" in prompt.lower()

    def test_directive_content_sanitized(self):
        directives = [
            {
                "id": "x",
                "priority": "critical",
                "content": "line1\nline2 | piped " + "Y" * 300,
            },
        ]
        prompt = _build_realist_prompt(_props("p"), [], active_directives=directives)
        # Newlines → spaces, pipes → slashes, truncated to 200 chars.
        assert "line1 line2 / piped" in prompt
        assert "Y" * 300 not in prompt

    def test_index_parity_preserved_with_directives(self):
        # Proposals must stay 0-indexed regardless of the directives block —
        # index alignment is the realist's only proposal↔verdict contract.
        directives = [{"id": "d1", "priority": "high", "content": "dir"}]
        prompt = _build_realist_prompt(_props("First", "Second"), [], active_directives=directives)
        assert "0. [maintenance]" in prompt
        assert "1. [maintenance]" in prompt

    def test_backward_compatible_without_directives(self):
        # No directives → directive_section is empty → prompt is byte-identical
        # to the pre-PR-3 form for the same ego (interpolation is a no-op).
        base = _build_realist_prompt(_props("X"), [], ego_source="genesis_ego_cycle")
        with_empty = _build_realist_prompt(
            _props("X"), [], ego_source="genesis_ego_cycle", active_directives=[]
        )
        assert base == with_empty


class TestOperateVsDevelopRule:
    """The genesis (COO) ego gets an operate-vs-develop realist rule; other
    egos do not (it is a COO-specific jurisdiction constraint)."""

    def test_genesis_ego_gets_operate_rule(self):
        prompt = _build_realist_prompt(
            _props("Refactor the router"), [], ego_source="genesis_ego_cycle"
        )
        assert "Operate vs develop" in prompt
        assert "Develop, not operate" in prompt

    def test_user_ego_has_no_operate_rule(self):
        prompt = _build_realist_prompt(
            _props("Publish an article"), [], ego_source="user_ego_cycle"
        )
        assert "Operate vs develop" not in prompt

    def test_no_ego_source_has_no_operate_rule(self):
        prompt = _build_realist_prompt(_props("Do X"), [])
        assert "Operate vs develop" not in prompt
