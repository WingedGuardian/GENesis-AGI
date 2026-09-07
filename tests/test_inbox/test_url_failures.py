"""Tests for URL failure detection, partial failure handling, and retry storms."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from genesis.inbox.monitor import _has_url_failures, _uncovered_urls

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
# _uncovered_urls — per-URL coverage check (silent-omission detection)
# ---------------------------------------------------------------------------

class TestUncoveredUrls:
    """Silent omission is invisible to _has_url_failures (no give-up language
    is emitted for a URL the model never mentions). _uncovered_urls closes
    that hole: every input URL must leave SOME trace in the response."""

    def test_no_urls_returns_empty(self):
        assert _uncovered_urls("any response", "just a note") == []

    def test_silently_omitted_url_detected(self):
        content = "https://example.com/first-article https://other.org/second-piece"
        response = (
            "# Inbox Evaluation\n\n## first-article\n"
            "Deep discussion of the example.com piece only.\n"
        )
        assert _uncovered_urls(response, content) == [
            "https://other.org/second-piece",
        ]

    def test_full_coverage_by_slug(self):
        content = "https://example.com/first-article https://other.org/second-piece"
        response = (
            "# Inbox Evaluation\n## 1. first-article\n...\n"
            "## 2. second-piece\n...\n"
        )
        assert _uncovered_urls(response, content) == []

    def test_platform_name_is_not_evidence_for_a_path_bearing_url(self):
        """A platform name identifies the SITE, never WHICH item on it.

        Deliberate contract change (2026-09-06, CodeRabbit finding on #1820):
        an earlier revision accepted "the LinkedIn post" as coverage for a
        lone linkedin.com URL. MEASURED by probe: that let a response which
        fetched nothing and named only the platform baseline its URL — the
        exact silent-drop class this gate exists to catch. Domain-level
        evidence now counts only when the domain IS the identity (no path).
        """
        content = "https://www.linkedin.com/posts/someone_zx9qv84k"
        response = "# Inbox Evaluation\nThe LinkedIn post argues that agents..."
        assert _uncovered_urls(response, content) == [content]

    def test_platform_name_is_not_evidence_for_a_repo_url(self):
        content = "https://github.com/OpenBMB/VoxCPM"
        response = "# Inbox Evaluation\nA GitHub project worth noting."
        assert _uncovered_urls(response, content) == [content]

    def test_query_only_url_is_not_a_bare_domain(self):
        """A query carries identity exactly as a path does.

        The domain used to be split on "/" alone, so "example.com?v=abc" was
        treated as the whole domain: the slug rung never ran and the domain
        rung then passed on the stem "example". Same class as the platform-name
        hole (CodeRabbit, #1820).
        """
        content = "https://example.com?v=abc123xyz"
        response = "# Inbox Evaluation\nAn example of the pattern, discussed."
        assert _uncovered_urls(response, content) == [content]

    def test_fragment_only_url_is_not_a_bare_domain(self):
        content = "https://example.com#deep-section-anchor"
        response = "# Inbox Evaluation\nAn example worth noting."
        assert _uncovered_urls(response, content) == [content]

    def test_query_identity_counts_as_coverage(self):
        """The flip side: a query value IS identity evidence when cited."""
        content = "https://watch.tv?id=first-clip-9f2"
        response = "# Inbox Evaluation\nThe first-clip-9f2 video is good."
        assert _uncovered_urls(response, content) == []

    def test_same_domain_guard_survives_query_only_urls(self):
        """Two query-only URLs share a domain — one being discussed must not
        vouch for the other (the count keyed on the query before the fix)."""
        content = (
            "https://watch.tv?id=first-clip-9f2 https://watch.tv?id=second-clip-3k8"
        )
        response = "# Inbox Evaluation\nThe first-clip-9f2 video is interesting."
        assert _uncovered_urls(response, content) == [
            "https://watch.tv?id=second-clip-3k8"
        ]

    def test_url_does_not_ride_a_siblings_coverage(self):
        """Rung 1 is a substring test, so a URL that is a PREFIX of a sibling
        used to pass on the sibling's quoted text — bypassing the same-domain
        guard entirely (architect audit, measured 3/3)."""
        content = "https://search.app/XYZ https://search.app/XYZW"
        response = "# Inbox Evaluation\n**Source:** https://search.app/XYZW"
        assert _uncovered_urls(response, content) == ["https://search.app/XYZ"]

    def test_domain_only_url_does_not_ride_a_deeper_sibling(self):
        content = "https://example.com https://example.com/deep/path-9f2"
        response = "# Inbox Evaluation\nThe deep/path-9f2 piece is good."
        assert _uncovered_urls(response, content) == ["https://example.com"]

    @pytest.mark.parametrize(
        "token",
        ["news", "docs", "read", "share", "link", "open", "apps", "sites",
         "mail", "medium"],
    )
    def test_generic_words_are_not_identity_as_path_segments(self, token):
        """The stopword set must be the SAME on every rung — these words were
        excluded as domain stems but still passed as path segments (10/10)."""
        content = f"https://example.com/{token}"
        response = f"# Inbox Evaluation\nA piece about {token} in general."
        assert _uncovered_urls(response, content) == [content]

    def test_tracking_param_is_not_identity(self):
        """A utm campaign word must not vouch for the item, and must not make
        the domain rung unreachable for a URL whose identity IS the domain."""
        content = "https://example.com/?utm_source=newsletter"
        response = "# Inbox Evaluation\nThis week's newsletter covers agents."
        assert _uncovered_urls(response, content) == [content]

    def test_bare_year_is_not_identity(self):
        content = "https://example.com/2024/03/some-deep-article"
        response = "# Inbox Evaluation\nA 2024 retrospective, broadly."
        assert _uncovered_urls(response, content) == [content]

    def test_slug_does_not_match_inside_a_longer_word(self):
        content = "https://example.com/agent"
        response = "# Inbox Evaluation\nAbout AI agents in general."
        assert _uncovered_urls(response, content) == [content]

    def test_malformed_ipv6_url_does_not_raise(self):
        """urlsplit raises ValueError on a bracketed IPv6 literal; the scan
        must not abort the whole inbox cycle (measured raise, no handler)."""
        content = "check https://[fe80::1/report"
        assert _uncovered_urls("# Inbox Evaluation\nnothing", content)

    def test_schemeless_url_carrying_a_scheme_in_its_query(self):
        """`"://" in u` is a substring test, not a scheme test — a scheme-less
        URL whose QUERY contains :// folded its host into the remainder."""
        content = "bit.ly/xk3?u=https://other.com/thing"
        response = "# Inbox Evaluation\nThe xk3 link is a redirect."
        assert _uncovered_urls(response, content) == []

    def test_brace_wrapped_real_url_is_not_exempt(self):
        """The {} placeholder exemption must not excuse a real URL that merely
        got a trailing brace from the surrounding prose."""
        content = "see {https://example.com/secret-slug-9f2}"
        response = "# Inbox Evaluation\nI did nothing at all."
        assert _uncovered_urls(response, content) != []

    def test_bare_domain_url_is_covered_by_domain_evidence(self):
        """When the URL carries no path the domain IS the identity, so
        domain/stem evidence is genuine evidence — not a platform gesture."""
        content = "https://voxcpm.ai"
        response = "# Inbox Evaluation\nvoxcpm is a tokenizer-free TTS model."
        assert _uncovered_urls(response, content) == []

    def test_domain_alias_cannot_vouch_for_two_urls_on_same_domain(self):
        # Two linkedin URLs; response covers one by slug and says "LinkedIn"
        # generally — the alias must NOT mark the second one covered.
        content = (
            "https://linkedin.com/posts/a_first-post-zx9qv84k\n"
            "https://linkedin.com/posts/b_other-topic-qq7ttv2m"
        )
        response = (
            "# Inbox Evaluation\n## LinkedIn: first-post\n"
            "Covers zx9qv84k in depth. LinkedIn content quality varies.\n"
        )
        uncovered = _uncovered_urls(response, content)
        assert uncovered == ["https://linkedin.com/posts/b_other-topic-qq7ttv2m"]

    def test_coverage_is_case_insensitive(self):
        content = "https://github.com/OpenBMB/VoxCPM"
        response = "# Inbox Evaluation\nvoxcpm is a tokenizer-free TTS model."
        assert _uncovered_urls(response, content) == []

    def test_youtube_video_id_counts_as_coverage(self):
        content = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        response = "# Inbox Evaluation\nThe video (dQw4w9WgXcQ) demonstrates..."
        assert _uncovered_urls(response, content) == []

    def test_template_placeholder_urls_never_demand_coverage(self):
        # A pasted API doc line like api.github.com/repos/{slug} is prose,
        # not a fetchable URL (MEASURED: 1 such false flag in the 133-item
        # historical corpus replay, 2026-09-06).
        content = "Call https://api.github.com/repos/{repo_slug} to list them"
        response = "# Inbox Evaluation\nA note about GitHub API usage."
        assert _uncovered_urls(response, content) == []

    @pytest.mark.parametrize(
        ("content", "response"),
        [
            # Reproductions from the adversarial review (5/5 passed the stem
            # rung before _GENERIC_STEM_TOKENS): incidental English words
            # matching a shortener's domain stem or alias are NOT evidence.
            (
                "https://search.app/nuXmqZ9",
                "# Inbox Evaluation\nI ran a web search to verify the claim.",
            ),
            (
                "https://share.google/XyZabQ7pL",
                "# Inbox Evaluation\nPlease share feedback on this analysis.",
            ),
            (
                "https://medium.com/@someone/deep-dive-abcdefgh",
                "# Inbox Evaluation\nMedium confidence in this assessment.",
            ),
            (
                "https://read.cv/someperson",
                "# Inbox Evaluation\nA good read overall, worth noting.",
            ),
            (
                "https://news.ycombinator.com/item?id=4159",
                "# Inbox Evaluation\nNo real news here beyond the headline.",
            ),
        ],
    )
    def test_generic_stem_words_are_not_coverage_evidence(
        self, content, response
    ):
        assert _uncovered_urls(response, content) == [content]

    def test_shortener_covered_by_verbatim_source_line(self):
        # The prompt now requires echoing each URL verbatim — the compliant
        # form for opaque shorteners whose target is discussed by title.
        content = "https://lnkd.in/p/eYssnmfd"
        response = (
            "# Inbox Evaluation\n## Some Article Title\n"
            "**Source:** https://lnkd.in/p/eYssnmfd\nGreat piece about agents."
        )
        assert _uncovered_urls(response, content) == []


class TestRealEvaluatorOutputPasses:
    """Regression guard from a LIVE compliance run (2026-09-06).

    The gate's strictness is only safe if the evaluator actually complies with
    the `**Source:** <url>` contract the prompt now states. Two faithful
    reproductions were run against the REAL rendered prompt; all three URLs —
    including the two hard cases, an opaque LinkedIn shortener needing a
    redirect follow and a YouTube link needing yt-dlp — were quoted verbatim
    and passed. These fixtures pin that: if a future change to the ladder
    starts flagging compliant output, this fails.
    """

    def test_github_item_with_intent_annotation(self):
        content = (
            "Interesting because of the continuous-space synthesis architecture\n"
            "https://github.com/OpenBMB/VoxCPM"
        )
        response = (
            "# Inbox Evaluation\n## https://github.com/OpenBMB/VoxCPM\n"
            "**Source:** https://github.com/OpenBMB/VoxCPM\n"
            "**Classification:** Genesis-relevant | **Decision:** Research\n"
            'Your note — "continuous-space synthesis architecture" — points at '
            "the real contribution.\n"
        )
        assert _uncovered_urls(response, content) == []

    def test_opaque_shortener_quoted_verbatim(self):
        content = "https://lnkd.in/p/eYssnmfd"
        response = (
            "# Inbox Evaluation\n## Item 1 — Repowise (LinkedIn post)\n"
            "**Source:** https://lnkd.in/p/eYssnmfd\n"
            "**Resolved to:** https://www.linkedin.com/posts/ai-agents-ugcPost-749\n"
        )
        assert _uncovered_urls(response, content) == []

    def test_youtube_item_fetched_via_ytdlp(self):
        content = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        response = (
            "# Inbox Evaluation\n## Item 2 — Rick Astley\n"
            "**Source:** https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "Fetched successfully via yt-dlp.\n"
        )
        assert _uncovered_urls(response, content) == []


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
                    evaluated_content TEXT,
                    drop_id TEXT,
                    batch_items TEXT
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
                    evaluated_content TEXT,
                    drop_id TEXT,
                    batch_items TEXT
                )
            """)
            await conn.commit()
            yield conn

    async def test_count_zero_when_no_failures(self, db):
        from genesis.db.crud import inbox_items
        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0

    async def test_first_misses_on_distinct_urls_are_not_a_storm(self, db):
        """THE BLOCKER (architect audit 2026-09-06): the storm counter counts
        ROWS per file, and one-item-per-evaluation makes one row per URL — so
        three DIFFERENT URLs each missing coverage ONCE tripped a threshold
        meant for three retries of the same content, parking the whole file
        with its remaining URLs never evaluated. Only retry-EXHAUSTED rows
        may count as persistent failure."""
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        for i in range(3):
            await inbox_items.create(
                db, id=str(uuid.uuid4()), file_path="/test/drop.md",
                content_hash=f"h{i}", status="failed",
                created_at=now.isoformat(),
            )
            await db.execute(
                "UPDATE inbox_items SET error_message = "
                "'partial_url_failure: uncovered https://x.com/a', "
                "retry_count = 1 WHERE content_hash = ?", (f"h{i}",),
            )
        await db.commit()

        # Raw row count still sees three...
        assert await inbox_items.count_url_failures(db, "/test/drop.md") == 3
        # ...but none has exhausted its retries, so it is not a storm.
        assert await inbox_items.count_url_failures(
            db, "/test/drop.md", min_retry_count=3,
        ) == 0

    async def test_exhausted_rows_still_count_as_a_storm(self, db):
        """The protection must survive the fix: genuinely persistent failure
        (rows that used up their retries) still trips the threshold."""
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        for i in range(3):
            await inbox_items.create(
                db, id=str(uuid.uuid4()), file_path="/test/drop.md",
                content_hash=f"e{i}", status="failed",
                created_at=now.isoformat(),
                error_message="partial_url_failure", retry_count=3,
            )
        await db.commit()
        assert await inbox_items.count_url_failures(
            db, "/test/drop.md", min_retry_count=3,
        ) == 3

    async def test_counts_coverage_variant_messages(self, db):
        """The storm guard must also count coverage-gate failures — their
        error_message carries the uncovered URLs after the base prefix."""
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        await inbox_items.create(
            db, id=str(uuid.uuid4()), file_path="/test/f.md",
            content_hash="h-cov", status="failed",
            created_at=now.isoformat(),
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = "
            "'partial_url_failure: uncovered https://x.com/a' "
            "WHERE content_hash = 'h-cov'",
        )
        await db.commit()
        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 1

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
