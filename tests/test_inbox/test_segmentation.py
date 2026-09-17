"""Tests for inbox delta segmentation — splitting a file's delta into items.

An "item" is the unit of evaluation: each URL line is its own item; a
contiguous block of non-URL prose is a single "note" item. This is what lets
the monitor evaluate ~5 items per CC call (instead of one mega-batch) and mark
each item's lines as evaluated independently.
"""

from __future__ import annotations

from genesis.inbox.scanner import Item, extract_urls, segment_items


def test_each_url_line_is_its_own_item():
    text = "https://a.com/x\nhttps://b.com/y\nhttps://c.com/z"
    items = segment_items(text)
    assert [i.kind for i in items] == ["url", "url", "url"]
    assert [i.text for i in items] == [
        "https://a.com/x",
        "https://b.com/y",
        "https://c.com/z",
    ]


def test_contiguous_prose_is_one_note():
    text = "This is a note.\nSecond line of the note.\nThird line."
    items = segment_items(text)
    assert len(items) == 1
    assert items[0].kind == "note"
    assert "Second line" in items[0].text
    assert items[0].urls == []


def test_blank_line_separates_notes():
    text = "First note block.\n\nSecond note block."
    items = segment_items(text)
    assert [i.kind for i in items] == ["note", "note"]
    assert items[0].text == "First note block."
    assert items[1].text == "Second note block."


def test_prose_directly_above_url_attaches_to_it():
    # The natural way a person annotates a save: intent line, then the URL.
    # The prose must ride WITH the URL item (as evaluation context), not be
    # divorced into a standalone note the evaluator sees separately.
    text = "Some prose here\nhttps://a.com/x\nmore prose"
    items = segment_items(text)
    assert [i.kind for i in items] == ["url", "note"]
    assert items[0].text == "Some prose here\nhttps://a.com/x"
    assert items[0].urls == ["https://a.com/x"]
    # Trailing prose stays independent — it may belong to the NEXT item.
    assert items[1].text == "more prose"


def test_intent_annotation_above_url_is_one_item():
    text = (
        "Interesting because of the tokenizer-free continuous-space synthesis\n"
        "https://github.com/OpenBMB/VoxCPM"
    )
    items = segment_items(text)
    assert len(items) == 1
    assert items[0].kind == "url"
    assert "tokenizer-free" in items[0].text
    assert items[0].urls == ["https://github.com/OpenBMB/VoxCPM"]


def test_note_with_blank_line_before_url_stays_separate():
    # A blank line signals an independent thought, not an annotation.
    text = "Standalone thought.\n\nhttps://a.com/x"
    items = segment_items(text)
    assert [i.kind for i in items] == ["note", "url"]
    assert items[1].text == "https://a.com/x"


def test_note_above_duplicate_url_survives_as_note():
    # The first occurrence already carries its own context; an annotation on a
    # deduped re-paste must not vanish with the duplicate URL line.
    text = (
        "https://a.com/x?utm_source=android\n"
        "Why I saved this again\n"
        "https://a.com/x?utm_source=desktop"
    )
    items = segment_items(text)
    assert [i.kind for i in items] == ["url", "note"]
    assert items[1].text == "Why I saved this again"


def test_multiple_urls_on_one_line_is_one_item():
    text = "https://a.com/x and https://b.com/y"
    items = segment_items(text)
    assert len(items) == 1
    assert items[0].kind == "url"
    assert len(items[0].urls) == 2


def test_duplicate_normalized_url_deduped_within_delta():
    # Same article re-pasted with different tracking params → one item.
    text = (
        "https://a.com/x?utm_source=android\n"
        "https://a.com/x?utm_source=desktop"
    )
    items = segment_items(text)
    assert len(items) == 1


def test_empty_and_whitespace_yields_no_items():
    assert segment_items("") == []
    assert segment_items("\n\n   \n") == []


def test_blank_lines_inside_prose_close_the_block():
    # Real Genesis.md shape: URLs separated by blank lines, plus a prose block.
    text = (
        "https://venturebeat.com/a\n\n"
        "# Heading of a pasted note\n"
        "Body line one.\n"
        "Body line two.\n\n"
        "https://linkedin.com/posts/b"
    )
    items = segment_items(text)
    assert [i.kind for i in items] == ["url", "note", "url"]
    assert "Body line two." in items[1].text


def test_extract_urls_matches_http_and_bare_domain():
    urls = extract_urls("see https://x.com/a and search.app/XYZ here")
    assert "https://x.com/a" in urls
    assert "search.app/XYZ" in urls


def test_extract_urls_strips_trailing_punctuation_and_dedups():
    urls = extract_urls("https://x.com/a. https://x.com/a, https://y.com/b!")
    assert urls == ["https://x.com/a", "https://y.com/b"]


def test_item_is_frozen_dataclass():
    item = Item(text="x", kind="note", urls=[])
    assert item.text == "x"
