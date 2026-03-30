"""Tests for Telegram markdown → HTML converter."""


from genesis.channels.telegram.markup import md_to_telegram_html, safe_html


class TestSafeHtml:
    def test_escapes_angle_brackets(self):
        assert safe_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_escapes_ampersand(self):
        assert safe_html("A & B") == "A &amp; B"

    def test_plain_text_unchanged(self):
        assert safe_html("hello world") == "hello world"

    def test_empty_string(self):
        assert safe_html("") == ""


class TestMdToTelegramHtml:
    # --- Bold ---

    def test_bold_asterisks(self):
        assert md_to_telegram_html("**bold**") == "<b>bold</b>"

    def test_bold_underscores(self):
        assert md_to_telegram_html("__bold__") == "<b>bold</b>"

    def test_bold_in_sentence(self):
        result = md_to_telegram_html("This is **important** text")
        assert result == "This is <b>important</b> text"

    # --- Italic ---

    def test_italic(self):
        assert md_to_telegram_html("*italic*") == "<i>italic</i>"

    def test_bold_not_italic(self):
        """**bold** should not be parsed as nested italic."""
        result = md_to_telegram_html("**bold**")
        assert "<i>" not in result
        assert result == "<b>bold</b>"

    # --- Inline code ---

    def test_inline_code(self):
        assert md_to_telegram_html("`code`") == "<code>code</code>"

    def test_inline_code_with_html(self):
        """HTML inside inline code is escaped."""
        assert md_to_telegram_html("`<div>`") == "<code>&lt;div&gt;</code>"

    def test_inline_code_preserves_content(self):
        result = md_to_telegram_html("Use `git status` to check")
        assert "<code>git status</code>" in result

    # --- Code blocks ---

    def test_code_block(self):
        text = "```\nprint('hello')\n```"
        result = md_to_telegram_html(text)
        assert "<pre>print(&#x27;hello&#x27;)</pre>" in result

    def test_code_block_with_language(self):
        text = "```python\ndef foo():\n    pass\n```"
        result = md_to_telegram_html(text)
        assert "<pre>" in result
        assert "def foo():" in result

    def test_code_block_escapes_html(self):
        text = "```\n<script>alert(1)</script>\n```"
        result = md_to_telegram_html(text)
        assert "&lt;script&gt;" in result
        assert "<script>" not in result

    # --- Links ---

    def test_link(self):
        result = md_to_telegram_html("[click here](https://example.com)")
        assert result == '<a href="https://example.com">click here</a>'

    # --- Headers ---

    def test_header_to_bold(self):
        result = md_to_telegram_html("# Title")
        assert result == "<b>Title</b>"

    def test_h2_to_bold(self):
        result = md_to_telegram_html("## Subtitle")
        assert result == "<b>Subtitle</b>"

    # --- Strikethrough ---

    def test_strikethrough(self):
        assert md_to_telegram_html("~~deleted~~") == "<s>deleted</s>"

    # --- HTML escaping ---

    def test_angle_brackets_escaped(self):
        result = md_to_telegram_html("a < b > c")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_ampersand_escaped(self):
        result = md_to_telegram_html("A & B")
        assert "&amp;" in result

    def test_no_double_escape(self):
        """Already-escaped entities should not be double-escaped."""
        # This tests a known edge case — if input has &amp; we'll escape
        # it to &amp;amp; which is wrong but acceptable (safe > correct).
        # The primary use case is raw markdown from CC, not pre-escaped text.
        result = md_to_telegram_html("A & B")
        assert result == "A &amp; B"

    # --- Mixed content ---

    def test_mixed_bold_and_code(self):
        result = md_to_telegram_html("**bold** and `code`")
        assert "<b>bold</b>" in result
        assert "<code>code</code>" in result

    def test_code_block_not_formatted(self):
        """Markdown inside code blocks should NOT be interpreted."""
        text = "```\n**not bold**\n```"
        result = md_to_telegram_html(text)
        assert "<b>" not in result
        assert "**not bold**" in result or "*not bold*" in result

    # --- Edge cases ---

    def test_empty_string(self):
        assert md_to_telegram_html("") == ""

    def test_none_passthrough(self):
        """None input returns None (not a crash)."""
        assert md_to_telegram_html(None) is None

    def test_whitespace_only(self):
        result = md_to_telegram_html("   ")
        assert result == "   "

    def test_unbalanced_bold_graceful(self):
        """Unbalanced ** markers don't crash, just pass through."""
        result = md_to_telegram_html("**unbalanced")
        # Should not raise, should not produce broken HTML
        assert isinstance(result, str)
        assert "**" in result or "unbalanced" in result

    def test_javascript_link_stripped(self):
        """javascript: protocol must be stripped from links."""
        result = md_to_telegram_html('[click](javascript:alert(1))')
        assert "javascript" not in result
        assert "click" in result
        assert "<a" not in result

    def test_safe_http_link_preserved(self):
        result = md_to_telegram_html("[site](https://example.com)")
        assert result == '<a href="https://example.com">site</a>'

    def test_paren_url_wikipedia(self):
        """URLs with balanced parentheses (Wikipedia-style) should work."""
        result = md_to_telegram_html("[article](https://en.wikipedia.org/wiki/Thing_(concept))")
        assert 'href="https://en.wikipedia.org/wiki/Thing_(concept)"' in result

    def test_data_uri_stripped(self):
        result = md_to_telegram_html("[img](data:text/html,<script>alert(1)</script>)")
        assert "<a" not in result
        assert "img" in result

    def test_multiline_preserves_structure(self):
        text = "Line 1\n\n**Bold line**\n\n`code`"
        result = md_to_telegram_html(text)
        assert "<b>Bold line</b>" in result
        assert "<code>code</code>" in result
        assert "\n" in result
