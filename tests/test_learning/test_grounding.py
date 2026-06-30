"""Tests for the procedure grounding score (observability-only gate).

Grounding measures what fraction of a built procedure's distinctive step-tokens
(commands / flags / paths) appear in the execution haystack. It NEVER drops a
procedure — these tests pin the tokenizer behavior the C2b spike validated
(real commands ground high; fabricated/discussed-only ones ground low; empty or
untokenizable cases fail open to 1.0).
"""

from __future__ import annotations

from genesis.learning.procedural.grounding import grounding_score


def test_empty_haystack_is_one():
    # Cannot assess → 1.0 (never warns, never drops).
    assert grounding_score(["run gh api repos/x/y"], "") == 1.0


def test_no_distinctive_tokens_is_one():
    # Prose with no command/flag/path tokens → cannot assess → 1.0.
    assert grounding_score(["do the thing carefully"], "some haystack text") == 1.0
    assert grounding_score([], "anything") == 1.0


def test_real_command_grounds_high():
    haystack = '{"command": "gh api repos/WingedGuardian/Genesis --jq .name"}'
    score = grounding_score(
        ["Run `gh api repos/WingedGuardian/Genesis --jq .name` to read the repo"],
        haystack,
    )
    assert score >= 0.75


def test_fabricated_command_grounds_low():
    haystack = '{"command": "gh api repos/WingedGuardian/Genesis"}'
    score = grounding_score(
        ["Run `frobnicate --quux /nonsense/path/here` to fix it"],
        haystack,
    )
    assert score < 0.25


def test_placeholder_normalization_grounds_templated_step():
    # A templated step should ground against the concrete command that ran.
    haystack = '{"command": "gh api repos/WingedGuardian/Genesis/code-scanning"}'
    score = grounding_score(
        ["gh api repos/<owner>/<repo>/code-scanning"],
        haystack,
    )
    assert score >= 0.5


def test_flags_and_paths_extracted_from_plain_text():
    haystack = "systemctl --user restart genesis-server /home/ubuntu/genesis/data"
    score = grounding_score(
        ["systemctl --user restart the unit, data at /home/ubuntu/genesis/data"],
        haystack,
    )
    assert score >= 0.5


def test_backtick_command_tokenized_not_atomic():
    # Regression for the tokenizer bug: a backtick-wrapped command must yield its
    # individual tokens, not one atomic never-matching blob.
    haystack = "ruff check . && pytest tests/test_x.py -q"
    score = grounding_score(["`ruff check .` then `pytest tests/test_x.py -q`"], haystack)
    assert score >= 0.75
