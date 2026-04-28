"""Tests for genesis.distribution package."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.distribution.base import PlatformDistributor, PostResult
from genesis.distribution.config import DistributionConfig, LinkedInConfig, load_distribution_config
from genesis.distribution.linkedin import LinkedInDistributor
from genesis.distribution.manager import DistributionManager
from genesis.modules.content_pipeline.publisher import _row_to_publish, ensure_table


class TestPostResult:
    def test_defaults(self):
        r = PostResult(post_id="123", platform="linkedin", url=None, status="published")
        assert r.error is None
        assert r.status == "published"


class TestDistributionConfig:
    def test_defaults(self):
        config = DistributionConfig()
        assert config.linkedin.connected_account_id == ""
        assert config.linkedin.author_urn == ""
        assert config.linkedin.user_id == "genesis"
        assert config.linkedin.daily_post_limit == 80

    def test_load_missing_file(self, tmp_path):
        config = load_distribution_config(path=tmp_path / "nonexistent.yaml")
        assert config.linkedin.connected_account_id == ""

    def test_load_valid_yaml(self, tmp_path):
        path = tmp_path / "distribution.yaml"
        path.write_text(
            "linkedin:\n"
            "  connected_account_id: ca_test123\n"
            "  author_urn: urn:li:person:abc\n"
            "  daily_post_limit: 50\n"
        )
        config = load_distribution_config(path=path)
        assert config.linkedin.connected_account_id == "ca_test123"
        assert config.linkedin.author_urn == "urn:li:person:abc"
        assert config.linkedin.daily_post_limit == 50


class TestLinkedInDistributor:
    def test_not_available_without_config(self):
        config = LinkedInConfig()
        dist = LinkedInDistributor(config=config)
        assert not dist.available
        assert dist.platform == "linkedin"

    @pytest.mark.asyncio
    async def test_publish_fails_gracefully_without_config(self):
        config = LinkedInConfig()
        dist = LinkedInDistributor(config=config)
        result = await dist.publish("test content")
        assert result.status == "failed"
        assert "not configured" in (result.error or "")

    def test_protocol_compliance(self):
        config = LinkedInConfig()
        instance = LinkedInDistributor(config=config)
        assert isinstance(instance, PlatformDistributor)


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
        """Test that manager routes to a registered distributor."""

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
            # Create old-schema table
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
                ("pub-1", "idea-1", "linkedin", "test content", "published",
                 "li-post-123", "https://linkedin.com/post/123"),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM content_publishes WHERE id = ?", ("pub-1",))
            row = await cursor.fetchone()
            col_names = [desc[0] for desc in cursor.description]
            result = _row_to_publish(row, col_names=col_names)

            assert result.id == "pub-1"
            assert result.platform_post_id == "li-post-123"
            assert result.post_url == "https://linkedin.com/post/123"

    def test_positional_fallback_short_row(self):
        row = ("pub-1", "idea-1", "linkedin", "content", None, "draft")
        result = _row_to_publish(row)
        assert result.id == "pub-1"
        assert result.platform_post_id is None
        assert result.post_url is None
