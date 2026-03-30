"""Tests for genesis.content.formatter."""

from genesis.content.formatter import ContentFormatter, strip_markdown
from genesis.content.types import FormatTarget


class TestContentFormatter:
    def setup_method(self):
        self.fmt = ContentFormatter()

    def test_short_text_no_truncation(self):
        result = self.fmt.format("Hello", FormatTarget.TELEGRAM)
        assert result.text == "Hello"
        assert not result.truncated

    def test_long_text_truncated(self):
        text = "x" * 5000
        result = self.fmt.format(text, FormatTarget.TELEGRAM)
        assert result.truncated
        assert len(result.text) <= 4096
        assert result.text.endswith("...")

    def test_twitter_strips_markdown(self):
        result = self.fmt.format("**bold** text", FormatTarget.TWITTER)
        assert "**" not in result.text
        assert "bold text" in result.text

    def test_twitter_truncation(self):
        text = "a" * 300
        result = self.fmt.format(text, FormatTarget.TWITTER)
        assert result.truncated
        assert len(result.text) <= 280

    def test_original_length_tracked(self):
        result = self.fmt.format("hello", FormatTarget.GENERIC)
        assert result.original_length == 5

    def test_telegram_keeps_markdown(self):
        result = self.fmt.format("**bold**", FormatTarget.TELEGRAM)
        assert "**bold**" in result.text


class TestSplitLong:
    def setup_method(self):
        self.fmt = ContentFormatter()

    def test_short_no_split(self):
        parts = self.fmt.split_long("Hello", FormatTarget.TELEGRAM)
        assert len(parts) == 1

    def test_paragraph_split(self):
        # Create text > 4096 with clear paragraph breaks
        para = "x" * 2000
        text = f"{para}\n\n{para}\n\n{para}"
        parts = self.fmt.split_long(text, FormatTarget.TELEGRAM)
        assert len(parts) >= 2
        for p in parts:
            assert len(p.text) <= 4096

    def test_all_content_preserved(self):
        para = "word " * 400  # ~2000 chars
        text = f"{para.strip()}\n\n{para.strip()}\n\n{para.strip()}"
        parts = self.fmt.split_long(text, FormatTarget.TELEGRAM)
        recombined = "\n\n".join(p.text for p in parts)
        # All words should still be present
        assert recombined.count("word") == text.count("word")


class TestStripMarkdown:
    def test_bold(self):
        assert strip_markdown("**bold**") == "bold"

    def test_italic(self):
        assert strip_markdown("*italic*") == "italic"

    def test_inline_code(self):
        assert strip_markdown("`code`") == "code"

    def test_link(self):
        assert strip_markdown("[text](http://url)") == "text"

    def test_header(self):
        assert strip_markdown("## Header").strip() == "Header"

    def test_plain_unchanged(self):
        assert strip_markdown("plain text") == "plain text"
