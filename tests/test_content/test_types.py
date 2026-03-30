"""Tests for genesis.content.types."""

from genesis.content.types import (
    DraftRequest,
    DraftResult,
    FormatTarget,
    FormattedContent,
    PlatformLimits,
)


class TestFormatTarget:
    def test_values(self):
        assert FormatTarget.TELEGRAM == "telegram"
        assert FormatTarget.TWITTER == "twitter"
        assert FormatTarget.LINKEDIN == "linkedin"
        assert FormatTarget.EMAIL == "email"
        assert FormatTarget.TERMINAL == "terminal"
        assert FormatTarget.GENERIC == "generic"


class TestPlatformLimits:
    def test_defaults(self):
        lim = PlatformLimits(max_length=100)
        assert lim.supports_markdown
        assert not lim.supports_html
        assert lim.truncation_suffix == "..."


class TestFormattedContent:
    def test_frozen(self):
        fc = FormattedContent(text="hi", target=FormatTarget.TELEGRAM)
        assert fc.text == "hi"
        assert not fc.truncated


class TestDraftRequest:
    def test_defaults(self):
        dr = DraftRequest(topic="AI news")
        assert dr.target == FormatTarget.GENERIC
        assert dr.tone == "professional"


class TestDraftResult:
    def test_frozen(self):
        fc = FormattedContent(text="draft", target=FormatTarget.LINKEDIN)
        dr = DraftResult(content=fc, raw_draft="draft")
        assert dr.model_used == ""
