"""Tests for ResponseFormatter."""

from genesis.cc.formatter import ResponseFormatter
from genesis.cc.types import ChannelType

fmt = ResponseFormatter()


def test_terminal_passthrough():
    result = fmt.format("Hello **world**", channel=ChannelType.TERMINAL)
    assert result == ["Hello **world**"]


def test_web_passthrough():
    result = fmt.format("# Title\n\nBody", channel=ChannelType.WEB)
    assert result == ["# Title\n\nBody"]


def test_telegram_split_long():
    text = "a" * 5000
    result = fmt.format(text, channel=ChannelType.TELEGRAM)
    assert len(result) == 2
    assert len(result[0]) <= 4096
    assert len(result[1]) <= 4096


def test_telegram_preserves_code_blocks():
    text = "Before\n```python\nprint('hi')\n```\nAfter"
    result = fmt.format(text, channel=ChannelType.TELEGRAM)
    assert len(result) == 1
    assert "```python" in result[0]


def test_telegram_split_respects_code_blocks():
    text = "A" * 4000 + "\n```\ncode\n```\n" + "B" * 100
    result = fmt.format(text, channel=ChannelType.TELEGRAM)
    for chunk in result:
        if "```" in chunk:
            assert chunk.count("```") % 2 == 0


def test_format_question_with_options():
    content = {"text": "Which vehicle?", "options": ["2022 Civic", "2019 RAV4"]}
    result = fmt.format_question(content, channel=ChannelType.TELEGRAM)
    assert "1." in result[0]
    assert "2022 Civic" in result[0]
    assert "2019 RAV4" in result[0]


def test_whatsapp_formatting():
    text = "This is **bold** and *italic*"
    result = fmt.format(text, channel=ChannelType.WHATSAPP)
    assert len(result) >= 1


def test_short_text_no_split():
    result = fmt.format("short", channel=ChannelType.TELEGRAM)
    assert result == ["short"]


def test_format_question_with_context():
    content = {"text": "What next?", "context": "Vehicle research", "options": []}
    result = fmt.format_question(content, channel=ChannelType.TERMINAL)
    assert "[Vehicle research]" in result[0]
    assert "What next?" in result[0]
