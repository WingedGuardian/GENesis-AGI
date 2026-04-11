"""Tests for _reply_decision punctuation tolerance."""

from genesis.autonomy.autonomous_dispatch import _reply_decision


def test_basic_approve_words():
    assert _reply_decision("approve") == "approved"
    assert _reply_decision("yes") == "approved"
    assert _reply_decision("ok") == "approved"
    assert _reply_decision("go") == "approved"
    assert _reply_decision("lgtm") == "approved"


def test_basic_reject_words():
    assert _reply_decision("reject") == "rejected"
    assert _reply_decision("no") == "rejected"
    assert _reply_decision("deny") == "rejected"
    assert _reply_decision("nope") == "rejected"


def test_punctuation_stripped():
    assert _reply_decision("ok.") == "approved"
    assert _reply_decision("Yes!") == "approved"
    assert _reply_decision("approve,") == "approved"
    assert _reply_decision("no.") == "rejected"
    assert _reply_decision("reject!") == "rejected"


def test_case_insensitive():
    assert _reply_decision("OK") == "approved"
    assert _reply_decision("YES") == "approved"
    assert _reply_decision("REJECT") == "rejected"


def test_first_word_only():
    assert _reply_decision("ok let's do it") == "approved"
    assert _reply_decision("yes please") == "approved"
    assert _reply_decision("no thanks") == "rejected"


def test_empty_and_whitespace():
    assert _reply_decision("") is None
    assert _reply_decision("   ") is None
    assert _reply_decision(None) is None


def test_all_punctuation():
    assert _reply_decision("...") is None
    assert _reply_decision("!!!") is None


def test_ambiguous():
    assert _reply_decision("maybe") is None
    assert _reply_decision("hmm") is None
