"""Tests for URL failure detection, partial failure handling, and retry storms."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from genesis.inbox.monitor import _has_url_failures

# ---------------------------------------------------------------------------
# _has_url_failures — heuristic detection
# ---------------------------------------------------------------------------

class TestHasUrlFailures:
    """Tested against all 8 real response files: 0 FP, 0 FN."""

    def test_clean_response_no_urls(self):
        assert _has_url_failures("great evaluation", "") is False

    def test_clean_response_with_urls(self):
        content = "Check https://example.com for details"
        response = "I fetched the URL and found useful content about AI."
        assert _has_url_failures(response, content) is False

    def test_no_urls_in_content(self):
        """No URLs in input → never triggers, even if response has error language."""
        assert _has_url_failures("unfetchable video", "just a text note") is False

    @pytest.mark.parametrize("pattern", [
        "unfetchable",
        "unreachable from this host",
        "watch them yourself",
        "cannot evaluate the video",
        "cannot assess without content",
        "could not be fetched",
        "could not be accessed",
        "i could not fetch",
        "i could not access",
    ])
    def test_detects_failure_pattern(self, pattern):
        content = "See https://youtube.com/watch?v=abc123"
        response = f"The URL was {pattern} due to SSL errors."
        assert _has_url_failures(response, content) is True

    def test_case_insensitive(self):
        content = "https://youtube.com/watch?v=x"
        response = "The video was UNFETCHABLE from this environment."
        assert _has_url_failures(response, content) is True

    def test_ssl_mention_without_giveup_is_clean(self):
        """Untitled-7 scenario: mentions SSL but resolved via yt-dlp."""
        content = "https://youtube.com/watch?v=abc"
        response = (
            "YouTube blocks SSL from this container. "
            "Resolved via yt-dlp --no-check-certificates and curl. "
            "All three videos successfully read."
        )
        assert _has_url_failures(response, content) is False

    def test_genesis_genesis_md_failure(self):
        """Genesis.genesis.md scenario: all URLs failed."""
        content = "https://search.app/nuDmd\nhttps://search.app/VUjAa"
        response = (
            "YouTube: SSL error — unfetchable.\n"
            "The content itself is simply unreachable from this host.\n"
            "Option A — Watch them yourself, flag patterns back."
        )
        assert _has_url_failures(response, content) is True

    def test_untitled5_failure(self):
        """Untitled-5 scenario: 'I could not fetch' two videos."""
        content = "https://youtube.com/shorts/abc\nhttps://youtube.com/watch?v=def"
        response = "I could not fetch either YouTube video — SSL errors."
        assert _has_url_failures(response, content) is True


# ---------------------------------------------------------------------------
# mark_url_failure CRUD
# ---------------------------------------------------------------------------

class TestMarkUrlFailure:
    @pytest.fixture
    async def db(self, tmp_path):
        import aiosqlite
        db_path = tmp_path / "test.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("""
                CREATE TABLE inbox_items (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    batch_id TEXT,
                    response_path TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    evaluated_content TEXT
                )
            """)
            await conn.commit()
            yield conn

    async def test_mark_url_failure_sets_fields(self, db):
        from genesis.db.crud import inbox_items

        item_id = str(uuid.uuid4())
        await inbox_items.create(
            db, id=item_id, file_path="/test/f.md",
            content_hash="abc", status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        # Simulate having evaluated_content from a prior eval
        await db.execute(
            "UPDATE inbox_items SET evaluated_content = 'old content' WHERE id = ?",
            (item_id,),
        )
        await db.commit()

        result = await inbox_items.mark_url_failure(
            db, item_id,
            response_path="/test/f.genesis.md",
            processed_at=datetime.now(UTC).isoformat(),
        )
        assert result is True

        row = await inbox_items.get_by_id(db, item_id)
        assert row["status"] == "failed"
        assert row["error_message"] == "partial_url_failure"
        assert row["response_path"] == "/test/f.genesis.md"
        assert row["evaluated_content"] is None  # Critical: must be NULL
        assert row["retry_count"] == 1

    async def test_mark_url_failure_not_in_known(self, db):
        """Failed items with retry_count < max_retries are excluded from known."""
        from genesis.db.crud import inbox_items

        item_id = str(uuid.uuid4())
        await inbox_items.create(
            db, id=item_id, file_path="/test/f.md",
            content_hash="abc", status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db, item_id,
            response_path="/test/f.genesis.md",
            processed_at=datetime.now(UTC).isoformat(),
        )

        known = await inbox_items.get_all_known(db, max_retries=3)
        assert "/test/f.md" not in known

    async def test_mark_url_failure_no_evaluated_content(self, db):
        """get_evaluated_content returns None for url-failure items."""
        from genesis.db.crud import inbox_items

        item_id = str(uuid.uuid4())
        await inbox_items.create(
            db, id=item_id, file_path="/test/f.md",
            content_hash="abc", status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db, item_id,
            processed_at=datetime.now(UTC).isoformat(),
        )

        content = await inbox_items.get_evaluated_content(db, "/test/f.md")
        assert content is None

    async def test_mark_url_failure_no_cooldown(self, db):
        """get_last_completed_at skips failed items → no cooldown."""
        from genesis.db.crud import inbox_items

        item_id = str(uuid.uuid4())
        await inbox_items.create(
            db, id=item_id, file_path="/test/f.md",
            content_hash="abc", status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db, item_id,
            response_path="/test/f.genesis.md",
            processed_at=datetime.now(UTC).isoformat(),
        )

        last_at = await inbox_items.get_last_completed_at(db, "/test/f.md")
        assert last_at is None


# ---------------------------------------------------------------------------
# count_url_failures (retry storm prevention)
# ---------------------------------------------------------------------------

class TestCountUrlFailures:
    @pytest.fixture
    async def db(self, tmp_path):
        import aiosqlite
        db_path = tmp_path / "test.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("""
                CREATE TABLE inbox_items (
                    id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    batch_id TEXT,
                    response_path TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    evaluated_content TEXT
                )
            """)
            await conn.commit()
            yield conn

    async def test_count_zero_when_no_failures(self, db):
        from genesis.db.crud import inbox_items
        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0

    async def test_counts_recent_failures(self, db):
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        for i in range(3):
            await inbox_items.create(
                db, id=str(uuid.uuid4()), file_path="/test/f.md",
                content_hash=f"hash{i}", status="failed",
                created_at=(now - timedelta(hours=i)).isoformat(),
            )
            # Set the error_message to partial_url_failure
            await db.execute(
                "UPDATE inbox_items SET error_message = 'partial_url_failure' "
                "WHERE content_hash = ?", (f"hash{i}",),
            )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md", since_hours=48)
        assert count == 3

    async def test_excludes_old_failures(self, db):
        from genesis.db.crud import inbox_items

        old_time = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
        await inbox_items.create(
            db, id=str(uuid.uuid4()), file_path="/test/f.md",
            content_hash="old", status="failed", created_at=old_time,
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = 'partial_url_failure' "
            "WHERE content_hash = 'old'",
        )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md", since_hours=48)
        assert count == 0

    async def test_excludes_other_file_paths(self, db):
        from genesis.db.crud import inbox_items

        await inbox_items.create(
            db, id=str(uuid.uuid4()), file_path="/test/other.md",
            content_hash="x", status="failed",
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = 'partial_url_failure' "
            "WHERE content_hash = 'x'",
        )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0

    async def test_excludes_non_url_failures(self, db):
        from genesis.db.crud import inbox_items

        await inbox_items.create(
            db, id=str(uuid.uuid4()), file_path="/test/f.md",
            content_hash="y", status="failed",
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = 'cc_invocation_error' "
            "WHERE content_hash = 'y'",
        )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0
