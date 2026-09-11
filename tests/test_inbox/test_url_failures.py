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

    @pytest.mark.parametrize(
        "pattern",
        [
            "unfetchable",
            "unreachable from this host",
            "watch them yourself",
            "cannot evaluate the video",
            "cannot assess without content",
            "could not be fetched",
            "could not be accessed",
            "i could not fetch",
            "i could not access",
        ],
    )
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
    """The gate is now ONE rule: the input URL must appear in the response.

    The prose-evidence ladder that used to sit under this (path segments,
    distinctive tokens, domain aliases, stoplists) was deleted, not repaired.
    MEASURED on the completed-evaluation corpus: it bought 6 of 146 URLs and
    carried six defects, each of which let an undiscussed URL baseline
    permanently. The cases below marked "Codex #1820" are those defects, kept as
    regression tests so the ladder cannot come back by accident.
    """

    def test_verbatim_url_is_covered(self):
        content = "https://example.com/some-article"
        response = "# Inbox Evaluation\n**Source:** https://example.com/some-article\nGood piece."
        assert _uncovered_urls(response, content) == []

    def test_silently_omitted_url_is_flagged(self):
        """The whole point: no give-up language, just absence."""
        content = "https://example.com/one-thing\nhttps://other.org/two-thing"
        response = "# Inbox Evaluation\n**Source:** https://example.com/one-thing\nOnly one."
        assert _uncovered_urls(response, content) == ["https://other.org/two-thing"]

    def test_coverage_ignores_scheme_www_and_trailing_slash(self):
        content = "https://www.example.com/piece/"
        response = "# Inbox Evaluation\nSee example.com/piece for details."
        assert _uncovered_urls(response, content) == []

    def test_a_verbatim_url_keeping_its_trailing_slash_is_covered(self):
        """The needle has its trailing slash stripped, so the response's own
        slash lands in the continuation tail. It is not a continuation."""
        content = "https://www.example-tech.com/some-cluster-tool/"
        response = (
            "# Inbox Evaluation\n**Source:** https://www.example-tech.com/some-cluster-tool/\n"
        )
        assert _uncovered_urls(response, content) == []

    def test_coverage_is_case_insensitive(self):
        content = "https://github.com/OpenBMB/VoxCPM"
        response = "# Inbox Evaluation\n**Source:** https://GITHUB.com/openbmb/voxcpm"
        assert _uncovered_urls(response, content) == []

    def test_a_sentence_period_after_the_url_does_not_break_the_match(self):
        content = "https://example.com/piece"
        response = "# Inbox Evaluation\nRead https://example.com/piece. It is good."
        assert _uncovered_urls(response, content) == []

    # ---- Codex #1820 regressions -------------------------------------------

    def test_a_sibling_url_does_not_vouch_for_its_prefix(self):
        """Codex #1820 P1: the continuation test was a DENYLIST of URL
        characters and could never be complete — ':', ';', '@', '+', '~', '!',
        '$', ',', '*', "'", '(' and ')' are all legal in an RFC 3986 path, so a
        response quoting only `.../foo:bar` vouched for an omitted `.../foo`."""
        content = "https://example.com/foo"
        for sep in (":", ";", "@", "+", ",", "!", "$", "*", "(", "~"):
            response = f"# Inbox Evaluation\nSee https://example.com/foo{sep}bar here."
            assert _uncovered_urls(response, content) == [content], f"leaked via {sep!r}"

    def test_a_longer_sibling_does_not_vouch_for_its_prefix(self):
        content = "https://search.app/XYZ"
        response = "# Inbox Evaluation\n**Source:** https://search.app/XYZW"
        assert _uncovered_urls(response, content) == [content]

    def test_a_different_host_ending_in_the_input_host_does_not_vouch(self):
        """The LEFT boundary, and it is not symmetric with the right one.

        The needle is scheme- and `www.`-stripped, so it is deliberately a
        SUFFIX of what a response writes — which is why the left side cannot
        mirror the right. With no left check at all, a citation of a DIFFERENT
        host whose name merely ends in the input host covered it.
        """
        content = "https://example.com/article"
        for other in (
            "https://cdn.example.com/article",  # a subdomain is a different host
            "https://notexample.com/article",  # no dot, still a different host
            "https://my-example.com/article",
        ):
            response = f"# Inbox Evaluation\n**Source:** {other}"
            assert _uncovered_urls(response, content) == [content], f"leaked via {other}"

    def test_the_www_and_scheme_variants_still_count_as_the_same_url(self):
        """The other side of the left boundary: these MUST still pass, and they
        are exactly why it cannot be a plain word-boundary check."""
        content = "https://example.com/article"
        for same in (
            "https://www.example.com/article",  # `www.` was stripped from the needle
            "http://example.com/article",  # scheme-insensitive
            "example.com/article",  # bare, at the very start of a line
        ):
            response = f"# Inbox Evaluation\n{same}"
            assert _uncovered_urls(response, content) == [], f"false miss on {same}"

    # Both boundary checks must cost O(1) per match, not O(response). There are
    # TWO independent quadratics here and they need DIFFERENT input shapes to
    # reach — a single test cannot pin both, which is how the first version of
    # this passed against both defects. A shadow gate still COMPUTES its verdict,
    # so `url_coverage_mode` mitigates neither: either would stall the event loop.

    @staticmethod
    def _scaling_ratio(build, content, k_small, k_large):
        """Cost ratio between two input sizes, as a load-INVARIANT shape test.

        An absolute wall-clock budget flakes: measured on this box, the same
        assertion passed at 0.017s idle and failed at 2.415s under a load average
        of 11 — a 140x swing with no code change. A RATIO cancels that, because
        contention slows both measurements together. Linear work over a 4x input
        gives ~4; quadratic gives ~16.

        min-of-3 per size, because the timer's noise floor is one-sided: a run
        can be arbitrarily slow (a scheduler preemption) but never faster than
        the work takes.
        """
        import time

        def best(k):
            response = build(k)
            runs = []
            for _ in range(3):
                start = time.perf_counter()
                _uncovered_urls(response, content)
                runs.append(time.perf_counter() - start)
            return min(runs)

        return best(k_large) / max(best(k_small), 1e-6)

    def test_the_continuation_scan_stops_at_the_first_non_punctuation(self):
        """Reaches the RIGHT-hand scan: separating repeats with `/` keeps every
        match's left boundary legal, so each one runs the continuation scan.

        MEASURED at k=2000 (50KB): consuming the whole run takes 7.3s, breaking
        early takes 0.002s. Asserted as a ratio — see `_scaling_ratio`.
        """
        content = "https://example.com/some-article"
        # The trailing "more" matters: without it the FINAL occurrence ends at
        # end-of-string with only a slash after it, which IS a genuine citation,
        # so the URL would be covered and no match would run the full scan.
        ratio = self._scaling_ratio(
            lambda k: "example.com/some-article/" * k + "more", content, 1000, 4000
        )
        assert ratio < 8, f"continuation scan is not O(1) per match: 4x input cost {ratio:.1f}x"

    def test_the_left_boundary_check_never_slices_the_prefix(self):
        """Reaches the LEFT-hand check: joining repeats with no separator makes
        every match illegal on the left, so each is rejected there before the
        right-hand scan ever runs.

        `lower[:m.start()]` copies the prefix on EVERY match. MEASURED at k=32000
        (768KB): slicing ~1.8s, reading one character 0.017s.
        """
        content = "https://example.com/some-article"
        ratio = self._scaling_ratio(lambda k: "example.com/some-article" * k, content, 8000, 32000)
        assert ratio < 8, f"left-boundary check is not O(1) per match: 4x input cost {ratio:.1f}x"

    def test_a_malformed_url_never_becomes_a_universal_match(self):
        """Codex #1820 P2: `https:///` normalises to an empty needle, and an
        empty pattern matches at every position — so a response saying nothing
        at all "covered" it and it baselined."""
        content = "https:///"
        response = "# Inbox Evaluation\nThis says nothing about any link whatsoever."
        assert _uncovered_urls(response, content) != []

    def test_a_shared_slug_cannot_cover_two_different_urls(self):
        """Codex #1820 P1: segment evidence was accepted without checking
        whether the same segment identified another input URL, so one mention of
        a shared slug covered BOTH — permanently baselining the omitted one.
        Under a verbatim rule this cannot arise; pinned so it cannot return."""
        content = "https://one.example/project-x9z7\nhttps://two.example/project-x9z7"
        response = "# Inbox Evaluation\nSome notes on project-x9z7 and what it does."
        assert len(_uncovered_urls(response, content)) == 2

    def test_prose_mentioning_a_path_word_is_not_coverage(self):
        """Codex #1820 P1: a whole path segment needed only 3 chars and absence
        from a finite stoplist, so `/agents` was "covered" by any response that
        discussed agents. No token-level rule can separate that from a real
        identity like `voxcpm`, which is why the rung is gone."""
        content = "https://example.com/agents"
        response = "# Inbox Evaluation\nA general discussion about agents and how they work."
        assert _uncovered_urls(response, content) == [content]

    def test_a_port_bearing_url_is_not_covered_by_its_bare_domain(self):
        """Codex #1820 P2: `hostname` dropped the port, so `example.com:8443`
        collapsed to `example.com` and passed on a bare mention of the domain."""
        content = "https://example.com:8443"
        response = "# Inbox Evaluation\nA note about example.com and nothing else."
        assert _uncovered_urls(response, content) == [content]

    # ---- exemptions ---------------------------------------------------------

    def test_template_placeholder_urls_never_demand_coverage(self):
        content = "Call https://api.github.com/repos/{repo_slug} to list them"
        response = "# Inbox Evaluation\nI did not fetch anything."
        assert _uncovered_urls(response, content) == []

    def test_no_urls_means_nothing_to_cover(self):
        assert _uncovered_urls("# Inbox Evaluation\nA plain note.", "just a text note") == []


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
        content = "https://lnkd.in/p/aB3xK9Qz"
        response = (
            "# Inbox Evaluation\n## Item 1 — Repowise (LinkedIn post)\n"
            "**Source:** https://lnkd.in/p/aB3xK9Qz\n"
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
            db,
            id=item_id,
            file_path="/test/f.md",
            content_hash="abc",
            status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        # Simulate having evaluated_content from a prior eval
        await db.execute(
            "UPDATE inbox_items SET evaluated_content = 'old content' WHERE id = ?",
            (item_id,),
        )
        await db.commit()

        result = await inbox_items.mark_url_failure(
            db,
            item_id,
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
            db,
            id=item_id,
            file_path="/test/f.md",
            content_hash="abc",
            status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db,
            item_id,
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
            db,
            id=item_id,
            file_path="/test/f.md",
            content_hash="abc",
            status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db,
            item_id,
            processed_at=datetime.now(UTC).isoformat(),
        )

        content = await inbox_items.get_evaluated_content(db, "/test/f.md")
        assert content is None

    async def test_mark_url_failure_no_cooldown(self, db):
        """get_last_completed_at skips failed items → no cooldown."""
        from genesis.db.crud import inbox_items

        item_id = str(uuid.uuid4())
        await inbox_items.create(
            db,
            id=item_id,
            file_path="/test/f.md",
            content_hash="abc",
            status="processing",
            created_at=datetime.now(UTC).isoformat(),
        )
        await inbox_items.mark_url_failure(
            db,
            item_id,
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
                db,
                id=str(uuid.uuid4()),
                file_path="/test/drop.md",
                content_hash=f"h{i}",
                status="failed",
                created_at=now.isoformat(),
            )
            await db.execute(
                "UPDATE inbox_items SET error_message = "
                "'partial_url_failure: uncovered https://x.com/a', "
                "retry_count = 1 WHERE content_hash = ?",
                (f"h{i}",),
            )
        await db.commit()

        # Raw row count still sees three...
        assert await inbox_items.count_url_failures(db, "/test/drop.md") == 3
        # ...but none has exhausted its retries, so it is not a storm.
        assert (
            await inbox_items.count_url_failures(
                db,
                "/test/drop.md",
                min_retry_count=3,
            )
            == 0
        )

    async def test_exhausted_rows_still_count_as_a_storm(self, db):
        """The protection must survive the fix: genuinely persistent failure
        (rows that used up their retries) still trips the threshold."""
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        for i in range(3):
            await inbox_items.create(
                db,
                id=str(uuid.uuid4()),
                file_path="/test/drop.md",
                content_hash=f"e{i}",
                status="failed",
                created_at=now.isoformat(),
                error_message="partial_url_failure",
                retry_count=3,
            )
        await db.commit()
        assert (
            await inbox_items.count_url_failures(
                db,
                "/test/drop.md",
                min_retry_count=3,
            )
            == 3
        )

    async def test_counts_coverage_variant_messages(self, db):
        """The storm guard must also count coverage-gate failures — their
        error_message carries the uncovered URLs after the base prefix."""
        from genesis.db.crud import inbox_items

        now = datetime.now(UTC)
        await inbox_items.create(
            db,
            id=str(uuid.uuid4()),
            file_path="/test/f.md",
            content_hash="h-cov",
            status="failed",
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
                db,
                id=str(uuid.uuid4()),
                file_path="/test/f.md",
                content_hash=f"hash{i}",
                status="failed",
                created_at=(now - timedelta(hours=i)).isoformat(),
            )
            # Set the error_message to partial_url_failure
            await db.execute(
                "UPDATE inbox_items SET error_message = 'partial_url_failure' "
                "WHERE content_hash = ?",
                (f"hash{i}",),
            )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md", since_hours=48)
        assert count == 3

    async def test_excludes_old_failures(self, db):
        from genesis.db.crud import inbox_items

        old_time = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
        await inbox_items.create(
            db,
            id=str(uuid.uuid4()),
            file_path="/test/f.md",
            content_hash="old",
            status="failed",
            created_at=old_time,
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
            db,
            id=str(uuid.uuid4()),
            file_path="/test/other.md",
            content_hash="x",
            status="failed",
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = 'partial_url_failure' WHERE content_hash = 'x'",
        )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0

    async def test_excludes_non_url_failures(self, db):
        from genesis.db.crud import inbox_items

        await inbox_items.create(
            db,
            id=str(uuid.uuid4()),
            file_path="/test/f.md",
            content_hash="y",
            status="failed",
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.execute(
            "UPDATE inbox_items SET error_message = 'cc_invocation_error' WHERE content_hash = 'y'",
        )
        await db.commit()

        count = await inbox_items.count_url_failures(db, "/test/f.md")
        assert count == 0
