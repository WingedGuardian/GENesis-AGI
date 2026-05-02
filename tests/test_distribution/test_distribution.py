"""Tests for genesis.distribution package."""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest

from genesis.distribution.base import PlatformDistributor, PostResult
from genesis.distribution.config import DistributionConfig, load_distribution_config
from genesis.distribution.manager import DistributionManager
from genesis.distribution.medium import (
    MediumDistributor,
    _extract_body,
    _extract_title,
    _js_string,
)
from genesis.modules.content_pipeline.publisher import _row_to_publish, ensure_table

# ---------------------------------------------------------------------------
# PostResult
# ---------------------------------------------------------------------------

class TestPostResult:
    def test_defaults(self):
        r = PostResult(post_id="123", platform="medium", url=None, status="published")
        assert r.error is None
        assert r.status == "published"


# ---------------------------------------------------------------------------
# Distribution Config
# ---------------------------------------------------------------------------

class TestDistributionConfig:
    def test_defaults(self):
        config = DistributionConfig()
        assert config.medium.username == ""
        assert config.medium.publish_status == "public"

    def test_load_missing_file(self, tmp_path):
        config = load_distribution_config(path=tmp_path / "nonexistent.yaml")
        assert config.medium.username == ""

    def test_load_valid_yaml(self, tmp_path):
        path = tmp_path / "distribution.yaml"
        path.write_text(
            "medium:\n"
            "  username: testuser\n"
            "  publish_status: draft\n"
        )
        config = load_distribution_config(path=path)
        assert config.medium.username == "testuser"
        assert config.medium.publish_status == "draft"

    def test_load_empty_yaml(self, tmp_path):
        path = tmp_path / "distribution.yaml"
        path.write_text("")
        config = load_distribution_config(path=path)
        assert config.medium.username == ""


# ---------------------------------------------------------------------------
# Title / body extraction
# ---------------------------------------------------------------------------

class TestContentExtraction:
    def test_extract_title_from_heading(self):
        assert _extract_title("# My Title\n\nBody text") == "My Title"

    def test_extract_title_from_h2(self):
        assert _extract_title("## Sub Title\n\nBody") == "Sub Title"

    def test_extract_title_from_first_line(self):
        assert _extract_title("Plain title\n\nBody text") == "Plain title"

    def test_extract_title_empty(self):
        assert _extract_title("") == "Untitled"

    def test_extract_body_strips_heading(self):
        body = _extract_body("# Title\n\nParagraph one.\nParagraph two.")
        assert body == "Paragraph one.\nParagraph two."

    def test_extract_body_keeps_plain(self):
        body = _extract_body("First line\nSecond line")
        assert body == "First line\nSecond line"

    def test_js_string_escapes(self):
        assert _js_string("it's") == "'it\\'s'"
        assert _js_string("line\none") == "'line\\none'"
        assert _js_string("back\\slash") == "'back\\\\slash'"


# ---------------------------------------------------------------------------
# Mock browser for MediumDistributor tests
# ---------------------------------------------------------------------------

class MockBrowser:
    """Mock BrowserClient that tracks calls and returns configurable results."""

    def __init__(self, *, logged_in: bool = True, publish_url: str = "https://medium.com/@user/post-abc123") -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._logged_in = logged_in
        self._publish_url = publish_url

    async def navigate(self, url: str) -> dict[str, Any]:
        self.calls.append(("navigate", (url,)))
        return {"url": url, "title": "Medium"}

    async def click(self, selector: str) -> dict[str, Any]:
        self.calls.append(("click", (selector,)))
        return {"clicked": selector}

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        self.calls.append(("fill", (selector, value)))
        return {}

    async def run_js(self, expression: str) -> dict[str, Any]:
        self.calls.append(("run_js", (expression,)))
        return {"result": None}

    async def snapshot(self) -> dict[str, Any]:
        self.calls.append(("snapshot", ()))
        if self._logged_in:
            return {"snapshot": "Write a story... testuser avatar", "url": self._publish_url}
        return {"snapshot": "Sign in to Medium", "url": "https://medium.com/"}

    async def screenshot(self) -> dict[str, Any]:
        self.calls.append(("screenshot", ()))
        return {"path": "/tmp/screenshot.png"}

    async def press_key(self, key: str, count: int = 1) -> dict[str, Any]:
        self.calls.append(("press_key", (key, count)))
        return {}


# ---------------------------------------------------------------------------
# MediumDistributor
# ---------------------------------------------------------------------------

class TestMediumDistributor:
    def test_not_available_without_username(self):
        dist = MediumDistributor(browser=MockBrowser(), username="")
        assert not dist.available
        assert dist.platform == "medium"

    def test_available_with_username(self):
        dist = MediumDistributor(browser=MockBrowser(), username="testuser")
        assert dist.available

    def test_protocol_compliance(self):
        dist = MediumDistributor(browser=MockBrowser(), username="testuser")
        assert isinstance(dist, PlatformDistributor)

    @pytest.mark.asyncio
    async def test_publish_fails_without_config(self):
        dist = MediumDistributor(browser=MockBrowser(), username="")
        result = await dist.publish("test content")
        assert result.status == "failed"
        assert "not configured" in (result.error or "")

    @pytest.mark.asyncio
    async def test_publish_fails_when_not_logged_in(self):
        browser = MockBrowser(logged_in=False)
        dist = MediumDistributor(browser=browser, username="testuser")
        result = await dist.publish("# Title\n\nBody text")
        assert result.status == "failed"
        assert "Not logged in" in (result.error or "")

    @pytest.mark.asyncio
    async def test_publish_succeeds(self):
        browser = MockBrowser(logged_in=True, publish_url="https://medium.com/@testuser/my-post-abc123")
        dist = MediumDistributor(browser=browser, username="testuser")
        result = await dist.publish("# My Post\n\nThis is the body.")

        assert result.status == "published"
        assert result.platform == "medium"
        assert result.url == "https://medium.com/@testuser/my-post-abc123"
        assert result.post_id == "my-post-abc123"

        # Verify browser flow: navigate to medium, snapshot (login check),
        # navigate to new-story, click title, insertText, press Enter,
        # insertText body, click Publish, click Publish now, snapshot (URL)
        nav_calls = [c for c in browser.calls if c[0] == "navigate"]
        assert any("medium.com" in c[1][0] for c in nav_calls)
        assert any("new-story" in c[1][0] for c in nav_calls)

        js_calls = [c for c in browser.calls if c[0] == "run_js"]
        assert len(js_calls) >= 2  # title + body insertText

    @pytest.mark.asyncio
    async def test_publish_handles_browser_error(self):
        browser = MockBrowser(logged_in=True)

        # Override navigate to return error on new-story
        original_navigate = browser.navigate
        call_count = 0

        async def failing_navigate(url: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count > 1:  # First call is login check, second is new-story
                return {"error": "Navigation timeout"}
            return await original_navigate(url)

        browser.navigate = failing_navigate
        dist = MediumDistributor(browser=browser, username="testuser")
        result = await dist.publish("# Test\n\nBody")
        assert result.status == "failed"
        assert "Failed to open" in (result.error or "")

    @pytest.mark.asyncio
    async def test_delete_navigates_and_clicks(self):
        browser = MockBrowser(logged_in=True)
        dist = MediumDistributor(browser=browser, username="testuser")
        ok = await dist.delete("https://medium.com/@testuser/my-post-abc123")
        assert ok

        nav_calls = [c for c in browser.calls if c[0] == "navigate"]
        assert any("my-post-abc123" in c[1][0] for c in nav_calls)

    @pytest.mark.asyncio
    async def test_delete_with_short_id(self):
        browser = MockBrowser(logged_in=True)
        dist = MediumDistributor(browser=browser, username="testuser")
        ok = await dist.delete("abc123")
        assert ok

        nav_calls = [c for c in browser.calls if c[0] == "navigate"]
        assert any("/p/abc123" in c[1][0] for c in nav_calls)


# ---------------------------------------------------------------------------
# DistributionManager
# ---------------------------------------------------------------------------

class TestDistributionManager:
    def test_no_distributors_by_default(self):
        mgr = DistributionManager()
        assert mgr.available_platforms == []

    @pytest.mark.asyncio
    async def test_distribute_unknown_platform(self):
        mgr = DistributionManager()
        results = await mgr.distribute("hello", ["twitter"])
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "No distributor" in (results[0].error or "")

    @pytest.mark.asyncio
    async def test_distribute_with_registered_platform(self):
        class FakeDistributor:
            @property
            def platform(self) -> str:
                return "test_platform"

            async def publish(self, content: str, *, visibility: str = "PUBLIC") -> PostResult:
                return PostResult(
                    post_id="fake-123",
                    platform="test_platform",
                    url="https://example.com/post/123",
                    status="published",
                )

            async def delete(self, post_id: str) -> bool:
                return True

        mgr = DistributionManager()
        mgr.register(FakeDistributor())
        assert "test_platform" in mgr.available_platforms

        results = await mgr.distribute("hello world", ["test_platform"])
        assert len(results) == 1
        assert results[0].status == "published"
        assert results[0].post_id == "fake-123"


# ---------------------------------------------------------------------------
# Publisher schema (unchanged from original)
# ---------------------------------------------------------------------------

class TestPublisherSchemaUpdate:
    @pytest.mark.asyncio
    async def test_new_columns_exist(self):
        async with aiosqlite.connect(":memory:") as db:
            await ensure_table(db)
            cursor = await db.execute("PRAGMA table_info(content_publishes)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "platform_post_id" in cols
            assert "post_url" in cols
            assert "error_message" in cols
            assert "distributed_at" in cols

    @pytest.mark.asyncio
    async def test_migration_adds_missing_columns(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("""
                CREATE TABLE content_publishes (
                    id TEXT PRIMARY KEY,
                    idea_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    published_at TEXT,
                    status TEXT NOT NULL DEFAULT 'draft'
                )
            """)
            await db.commit()

            await ensure_table(db)
            cursor = await db.execute("PRAGMA table_info(content_publishes)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "platform_post_id" in cols
            assert "distributed_at" in cols

    @pytest.mark.asyncio
    async def test_named_column_row_to_publish(self):
        async with aiosqlite.connect(":memory:") as db:
            await ensure_table(db)
            await db.execute(
                """INSERT INTO content_publishes
                   (id, idea_id, platform, content_text, status, platform_post_id, post_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("pub-1", "idea-1", "medium", "test content", "published",
                 "post-123", "https://medium.com/@user/post-123"),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM content_publishes WHERE id = ?", ("pub-1",))
            row = await cursor.fetchone()
            col_names = [desc[0] for desc in cursor.description]
            result = _row_to_publish(row, col_names=col_names)

            assert result.id == "pub-1"
            assert result.platform_post_id == "post-123"
            assert result.post_url == "https://medium.com/@user/post-123"

    def test_positional_fallback_short_row(self):
        row = ("pub-1", "idea-1", "medium", "content", None, "draft")
        result = _row_to_publish(row)
        assert result.id == "pub-1"
        assert result.platform_post_id is None
        assert result.post_url is None
