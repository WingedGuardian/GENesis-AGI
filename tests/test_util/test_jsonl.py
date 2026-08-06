"""Tests for genesis.util.jsonl — how message chunking actually behaves.

These tests pin the real behavior of ``chunk_messages``: it splits a list
into consecutive fixed-size chunks preserving order, with no user+assistant
pairing logic.
"""

from genesis.util.jsonl import ConversationMessage, chunk_messages


def _msgs(n: int) -> list[ConversationMessage]:
    return [
        ConversationMessage(role="user" if i % 2 == 0 else "assistant",
                            text=f"message {i}",
                            line_number=i)
        for i in range(n)
    ]


def test_chunks_are_capped_at_chunk_size():
    chunks = chunk_messages(_msgs(120), chunk_size=50)
    assert [len(c) for c in chunks] == [50, 50, 20]


def test_empty_is_empty():
    assert chunk_messages([], chunk_size=50) == []


def test_size_less_than_chunk_returns_single_chunk():
    chunks = chunk_messages(_msgs(5), chunk_size=50)
    assert [len(c) for c in chunks] == [5]


def test_exact_multiple_has_no_trailing_chunk():
    chunks = chunk_messages(_msgs(100), chunk_size=50)
    assert [len(c) for c in chunks] == [50, 50]


def test_preserves_message_order():
    msgs = _msgs(120)
    chunks = chunk_messages(msgs, chunk_size=50)
    flattened = [m for chunk in chunks for m in chunk]
    assert [m.line_number for m in flattened] == [m.line_number for m in msgs]


def test_default_chunk_size_is_50():
    chunks = chunk_messages(_msgs(120))
    assert [len(c) for c in chunks] == [50, 50, 20]