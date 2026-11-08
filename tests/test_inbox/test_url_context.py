"""Coverage must distinguish URL punctuation from explicit source wrappers."""

import pytest

from genesis.inbox.url_coverage import _display_url, _uncovered_urls

BASE = "https://example.com/resource"


@pytest.mark.parametrize("ending", [")", "]", "}", "'", '"', "`", ")!", "']"])
def test_literal_terminal_delimiters_are_not_alternate_identities(ending):
    literal = BASE + ending
    assert _uncovered_urls(f"**Source:** <{BASE}>", literal) == [literal]
    assert _uncovered_urls(f"**Source:** <{literal}>", literal) == []


@pytest.mark.parametrize("ending", [")", "]", "}", "'", '"', "`"])
def test_context_free_display_does_not_guess_wrappers(ending):
    assert _display_url(BASE + ending) == BASE + ending


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("(", ")"), ("[docs](", ")"), ('"', '",'), ("`", "`."), ('("', '")!')],
)
def test_explicit_wrappers_remain_coverable(prefix, suffix):
    assert _uncovered_urls(f"**Source:** <{BASE}>", prefix + BASE + suffix) == []


@pytest.mark.parametrize("prefix", ["", "\\(", "(some prose "])
def test_literal_and_wrapped_occurrences_do_not_share_alternates(prefix):
    literal = BASE + ")"
    content = f"[docs]({BASE})\n{prefix}{literal}"
    assert _uncovered_urls(f"**Source:** <{BASE}>", content) == [literal]


@pytest.mark.parametrize(
    "url",
    [BASE + "_(part)", "https://[::1]:8443/p", BASE + "?f[0]=x", BASE + ";"],
)
def test_identity_bearing_characters_survive(url):
    assert _uncovered_urls(f"**Source:** <{url}>", url) == []
    assert _uncovered_urls("**Source:** <https://example.org/unrelated>", url) == [url]


@pytest.mark.parametrize("ending", [";", "!", "?x=1!", "_(part)", "?q='word'"])
def test_punctuation_inside_proven_wrapper_is_not_trimmed(ending):
    url = BASE + ending
    assert _uncovered_urls(f"**Source:** <{url}>", '"' + url + '"') == []
    assert _uncovered_urls(f"**Source:** <{BASE}>", '"' + url + '"')
