import pytest

from genesis.transcript_analytics.rendering import terminal_text


@pytest.mark.parametrize(
    "codepoint",
    list(range(32)) + list(range(127, 160)) + [0x2028, 0x2029, 0x202E, 0x2066, 0xD800, 0xDFFF],
)
def test_terminal_controls_are_visible_without_emitting_raw_characters(codepoint):
    char = chr(codepoint)
    rendered = terminal_text("before" + char + "after")
    assert char not in rendered
    assert rendered.startswith("before\\")
    assert rendered.endswith("after")


def test_terminal_ansi_and_osc_cannot_move_cursor_or_set_title():
    text = "\x1b[2J\r\x1b]0;forged title\x07"
    assert terminal_text(text) == "\\u001b[2J\\r\\u001b]0;forged title\\u0007"


def test_terminal_preserves_normal_unicode_and_literal_punctuation():
    assert terminal_text('café 👩\u200d💻 "quoted" \\path') == 'café 👩\u200d💻 "quoted" \\path'
    assert terminal_text(None) == "NULL"
    assert terminal_text(0) == "0"
