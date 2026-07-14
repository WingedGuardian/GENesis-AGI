"""Tests for LongMemEval question -> recall-query conversion (WS-1 A4).

The spike showed raw natural-language questions zero out the FTS arm. The
keyword arm strips question/stop words so content terms drive lexical recall.
"""

from __future__ import annotations

import pytest

from genesis.eval.longmemeval.query import QueryArm, build_query, extract_keywords


def test_raw_arm_is_identity():
    q = "What degree did I graduate with?"
    assert build_query(q, QueryArm.RAW) == q


def test_keyword_arm_strips_question_words():
    kw = extract_keywords("What degree did I graduate with?")
    toks = kw.split()
    assert "degree" in toks
    assert "graduate" in toks
    for stop in ("what", "did", "i", "with"):
        assert stop not in toks


def test_build_query_keyword_uses_extractor():
    q = "When did I start my new job?"
    assert build_query(q, QueryArm.KEYWORD) == extract_keywords(q)


def test_keyword_preserves_content_order():
    kw = extract_keywords("What is the name of my favorite restaurant?")
    toks = kw.split()
    assert toks == ["name", "favorite", "restaurant"]


def test_all_stopword_question_falls_back_to_raw():
    # a degenerate question with no content words must not become an empty query
    q = "What did I do?"
    assert extract_keywords(q) == q


def test_arms_enum_has_both():
    assert set(QueryArm) == {QueryArm.RAW, QueryArm.KEYWORD}


@pytest.mark.parametrize("arm", list(QueryArm))
def test_build_query_never_empty(arm):
    assert build_query("What did I do?", arm).strip()
