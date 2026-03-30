"""Tests for genesis.content.limits."""

from genesis.content.limits import get_limits
from genesis.content.types import FormatTarget


class TestLimits:
    def test_all_targets_have_limits(self):
        for target in FormatTarget:
            lim = get_limits(target)
            assert lim.max_length > 0

    def test_telegram_limit(self):
        lim = get_limits(FormatTarget.TELEGRAM)
        assert lim.max_length == 4096

    def test_twitter_limit(self):
        lim = get_limits(FormatTarget.TWITTER)
        assert lim.max_length == 280
        assert not lim.supports_markdown

    def test_linkedin_limit(self):
        lim = get_limits(FormatTarget.LINKEDIN)
        assert lim.max_length == 3000

    def test_email_limit(self):
        lim = get_limits(FormatTarget.EMAIL)
        assert lim.max_length == 50_000
