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


@pytest.mark.parametrize("prefix", ["", "\\(", "(some prose\n", "(closed) "])
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


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("(see ", ")"),
        ("[read more at ", "]"),
        ('"see ', '"'),
        ("'read ", "'"),
        ('"[docs](', ')"'),
        ("'[docs](", ")'"),
        ('"(see ', ')"'),
        ("``", "``"),
        ("```", "```"),
        ("don't (see ", ")"),
    ],
)
def test_source_context_covers_prose_and_code_wrappers(prefix, suffix):
    from genesis.inbox.url_coverage import _coverage_url_occurrences

    source = prefix + BASE + suffix
    assert _coverage_url_occurrences(source)[0].display == BASE
    assert _uncovered_urls(f"**Source:** <{BASE}>", source) == []


@pytest.mark.parametrize(
    "prefix",
    ["(closed) ", "\\(see ", "(see\n", '"closed" ', '\\"see ', '"see\n'],
)
def test_closed_escaped_or_previous_line_context_does_not_prove_wrapper(prefix):
    literal = BASE + (')' if '(' in prefix else '"')
    assert _uncovered_urls(f"**Source:** <{BASE}>", prefix + literal) == [literal]


def test_previous_url_punctuation_is_not_context_for_the_next_url():
    other = "https://example.org/contains("
    literal = BASE + ")"
    source = other + " " + literal
    assert _uncovered_urls(f"**Source:** <{other}>\n**Source:** <{BASE}>", source) == [literal]


def test_contraction_is_not_proof_of_a_single_quote_wrapper():
    literal = BASE + "'"
    assert _uncovered_urls(f"**Source:** <{BASE}>", "don't use " + literal) == [literal]


@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("internal_count", [1, 2, 3])
def test_internal_matching_code_runs_make_terminal_wrapper_unproved(width, internal_count):
    run = "`" * width
    body = BASE + (run + "literal") * internal_count
    raw = body + run
    assert _uncovered_urls(f"**Source:** <{body}>", run + raw) == [raw]
    assert _uncovered_urls(f"**Source:** <{raw}>", run + raw) == []


@pytest.mark.parametrize(("width", "inside"), [(1, 2), (2, 1), (3, 1), (3, 2)])
def test_different_internal_code_width_remains_literal(width, inside):
    body = BASE + "`" * inside + "literal"
    assert _uncovered_urls(f"**Source:** <{body}>", "`" * width + body + "`" * width) == []


@pytest.mark.parametrize(("opener", "ending", "later_ending"), [
    ('"', '")', '"'), ("(", ")]", ")"), ("`", "`)", "`"),
])
def test_ambiguous_raw_url_cannot_leave_stale_context_for_later_url(opener, ending, later_ending):
    first = "https://example.org/first" + ending
    later = BASE + later_ending
    source = opener + first + " " + later
    assert _uncovered_urls(f"**Source:** <{first}>\n**Source:** <{BASE}>", source) == [later]


def test_balanced_url_pairs_do_not_erase_surrounding_prose_context():
    first = "https://example.org/first_(part)"
    source = "(see " + first + " and " + BASE + ")"
    assert _uncovered_urls(f"**Source:** <{first}>\n**Source:** <{BASE}>", source) == []


@pytest.mark.parametrize(("opener", "closer"), [("(", ")"), ("[", "]"), ("{", "}")])
def test_later_url_opener_cannot_hide_the_first_source_closure(opener, closer):
    from genesis.inbox.url_coverage import _coverage_url_occurrences

    raw = BASE + closer + opener + "again" + closer
    assert _coverage_url_occurrences(opener + raw)[0].display == raw


@pytest.mark.parametrize(("opener", "closer"), [("(", ")"), ("[", "]"), ("{", "}")])
def test_closed_source_frame_cannot_be_reopened_by_prior_url_bytes(opener, closer):
    first = "https://example.org/first" + closer + opener
    later = BASE + closer
    source = opener + first + " " + later
    assert _uncovered_urls(f"**Source:** <{first}>\n**Source:** <{BASE}>", source) == [later]


@pytest.mark.parametrize(("opener", "closer"), [("(", ")"), ("[", "]"), ("{", "}")])
def test_extra_terminal_closer_makes_the_whole_alternate_unproved(opener, closer):
    raw = BASE + closer + closer
    assert _uncovered_urls(f"**Source:** <{raw[:-1]}>", opener + raw) == [raw]


def test_misnested_intrinsic_pairs_cannot_supply_later_context():
    first = "https://example.org/first([)]"
    later = BASE + ")"
    assert _uncovered_urls(f"**Source:** <{first}>\n**Source:** <{BASE}>", "(" + first + " " + later) == [later]


def test_source_quote_cannot_close_across_an_unclosed_prose_frame():
    raw = BASE + '"'
    assert _uncovered_urls(f"**Source:** <{BASE}>", '"said (then " and ' + raw) == [raw]


def test_quoted_url_keeps_literal_unmatched_parenthesis():
    url = BASE + ")"
    assert _uncovered_urls(f"**Source:** <{url}>", '("' + url + '")') == []


@pytest.mark.parametrize(("prefix", "suffix"), [("(see ", ")"), ('"see ', '"'), ("[see ", "]"), ("", "")])
def test_angle_url_isolated_from_outer_prose_wrappers(prefix, suffix):
    raw = BASE + ")"
    source = prefix + "<" + raw + ">" + suffix
    assert _uncovered_urls(f"**Source:** <{BASE}>", source) == [raw]
    assert _uncovered_urls(f"**Source:** <{raw}>", source) == []


def test_angle_containment_preserves_outer_frame_for_next_url():
    first = "https://example.org/first)"
    source = "(see <" + first + "> and " + BASE + ")"
    assert _uncovered_urls(f"**Source:** <{first}>\n**Source:** <{BASE}>", source) == []


@pytest.mark.parametrize("width", [1, 2, 3])
def test_angle_characters_inside_code_do_not_shield_code_closure(width):
    run = "`" * width
    assert _uncovered_urls(f"**Source:** <{BASE}>", run + "<" + BASE + run + ">") == []


def test_escaped_angle_opener_does_not_create_an_autolink_boundary():
    from genesis.inbox.url_coverage import _coverage_url_occurrences

    source = "(see \\<" + BASE + ")>)"
    assert _coverage_url_occurrences(source)[0].display == BASE
