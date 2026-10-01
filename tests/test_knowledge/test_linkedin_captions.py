"""LinkedIn video posts: web_fetch appends the post video's caption track.

A LinkedIn post's page fetch has the post text but never what its video says.
yt-dlp's LinkedIn extractor reads the caption track without an account; the
route reuses YouTubeProcessor's fixed argv and limits, accepts caption tracks
only from LinkedIn's media hosts, and never downloads audio.
"""

from __future__ import annotations

import json

import pytest

import genesis.knowledge.processors.youtube as yt
from genesis.knowledge.processors.linkedin import LinkedInCaptionProcessor, is_linkedin_post_url
from genesis.knowledge.processors.youtube import YouTubeProcessor, select_caption

_POST = (
    "https://www.linkedin.com/posts/someone_a-talk-about-agents-activity-7000000000000000000-AbCd"
)
_VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello from the video\n"
_LICDN = [{"ext": "vtt", "url": "https://dms.licdn.com/playlist/vid/captions.vtt"}]


@pytest.mark.parametrize(
    "url,ok",
    [
        (_POST, True),
        ("https://linkedin.com/posts/someone_slug-activity-1-x", True),
        ("https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000000/", True),
        ("http://www.linkedin.com/feed/update/urn:li:ugcPost:7000000000000000000", True),
        ("https://www.linkedin.com/in/someone/", False),
        ("https://www.linkedin.com/company/example/", False),
        ("https://www.linkedin.com/feed/", False),
        ("https://www.linkedin.com/posts/", False),
        ("https://www.linkedin.com/posts/a/b", False),
        ("https://linkedin.com.example.org/posts/someone_slug", False),
        ("https://lnkd.in/abcdef", False),
        ("ftp://www.linkedin.com/posts/someone_slug", False),
    ],
)
def test_only_a_single_post_url_is_a_post(url, ok):
    assert is_linkedin_post_url(url) is ok


def test_the_argv_uses_the_linkedin_extractor_and_the_same_lockdown():
    argv = LinkedInCaptionProcessor._argv(["--skip-download"], _POST, verify=True)
    assert argv[argv.index("--use-extractors") + 1] == "linkedin"
    for flag in ("--ignore-config", "--no-cookies", "--no-cookies-from-browser", "--no-playlist"):
        assert flag in argv
    assert argv[-2:] == ["--", _POST]
    # YouTube keeps its own extractor.
    yargv = YouTubeProcessor._argv([], "https://youtu.be/abc123", verify=True)
    assert yargv[yargv.index("--use-extractors") + 1] == "youtube"


def test_caption_tracks_are_accepted_only_from_each_sites_own_hosts():
    li = {"subtitles": {"en": _LICDN}}
    ytb = {"subtitles": {"en": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext"}]}}
    li_hosts = LinkedInCaptionProcessor.caption_hosts
    assert select_caption(li, caption_hosts=li_hosts)["key"] == "en"
    assert select_caption(ytb, caption_hosts=li_hosts) is None
    assert select_caption(li) is None  # the YouTube default rejects licdn


def _fake_ytdlp(info: dict | None, vtt: str | None, seen: list):
    async def run(argv):
        seen.append(argv)
        if info is None:
            return 1, b"", b"ERROR: [LinkedIn] 7000: Unable to extract video; please report this issue"
        out = argv[argv.index("-o") + 1].replace("%%", "%")
        stem = out.replace("video.%(ext)s", "video.")
        if "--write-info-json" in argv:
            with open(stem + "info.json", "w") as fh:
                json.dump(info, fh)
        elif vtt is not None and "--sub-langs" in argv:
            with open(stem + "en.vtt", "w") as fh:
                fh.write(vtt)
        return 0, b"", b""

    return run


@pytest.fixture
def ytdlp(monkeypatch, tmp_path):
    seen: list = []

    def use(info, vtt):
        monkeypatch.setattr(yt, "_exec", _fake_ytdlp(info, vtt, seen))

    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)
    return use, seen


async def test_a_captioned_post_video_yields_its_transcript(ytdlp):
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    result = await LinkedInCaptionProcessor().fetch(_POST)
    assert result.transcript == "hello from the video"
    assert result.caption["kind"] == "manual"


async def test_audio_is_never_used_even_when_asked(ytdlp, monkeypatch):
    use, seen = ytdlp
    use({"title": "Post text", "duration": 30}, None)

    async def boom(self, *a, **k):
        raise AssertionError("audio must never be transcribed for LinkedIn")

    monkeypatch.setattr(YouTubeProcessor, "_transcribe_audio", boom)
    result = await LinkedInCaptionProcessor().fetch(_POST, audio_fallback=True)
    assert result.transcript is None
    assert not any("-x" in argv for argv in seen)


# ─── the web_fetch route ────────────────────────────────────────────────────


@pytest.fixture
def page(monkeypatch):
    from genesis.mcp.health import web_tools

    calls = []

    async def impl(url, backend="auto", max_chars=50000):
        calls.append(url)
        return {"url": url, "content": "the post text", "backend_used": "tinyfish", "error": None}

    async def multi(urls, max_chars=50000):
        return {
            "results": [{"url": u, "text": "post " + u[-4:]} for u in urls],
            "errors": [],
            "backend_used": "tinyfish",
        }

    async def no_video(url, max_chars):
        return None, None

    monkeypatch.setattr(web_tools, "_impl_web_fetch", impl)
    monkeypatch.setattr(web_tools, "_impl_web_fetch_multi", multi)
    monkeypatch.setattr("genesis.mcp.health.youtube_route.fetch_youtube", no_video)
    return getattr(web_tools.web_fetch, "fn", web_tools.web_fetch), calls


def _body(text):
    from genesis.security.sanitizer import strip_boundary_markers

    return strip_boundary_markers(text).strip("\n")


async def test_the_transcript_rides_beside_the_unchanged_page(page, ytdlp):
    tool, calls = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    out = await tool(url=_POST)
    assert _body(out["content"]) == "the post text"
    assert _body(out["video_transcript"]) == "hello from the video"
    assert _body(out["video_caption"]["kind"]) == "manual"
    assert out["video_tls_verified"] is True
    assert calls == [_POST] and "video_error" not in out


async def test_a_text_post_is_returned_unchanged_and_silent(page, ytdlp):
    tool, _ = page
    use, _ = ytdlp
    use(None, None)  # yt-dlp: "Unable to extract video" (no <video> tag)
    out = await tool(url=_POST)
    assert _body(out["content"]) == "the post text"
    assert "video_error" not in out and "video_transcript" not in out


async def test_a_failed_lookup_is_reported_not_taken_for_a_text_post(page, ytdlp, monkeypatch):
    """Codex P1 / Devin on #2698: a rate limit or certificate failure also
    leaves no title, and was reported as 'text post'."""
    tool, _ = page

    async def throttled(argv):
        return 1, b"", b"ERROR: [LinkedIn] 7000: HTTP Error 429: Too Many Requests"

    monkeypatch.setattr(yt, "_exec", throttled)
    out = await tool(url=_POST)
    assert "429" in _body(out["video_error"])


async def test_a_post_video_without_captions_says_why(page, ytdlp):
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text"}, None)
    out = await tool(url=_POST)
    assert _body(out["content"]) == "the post text"
    assert "no captions" in _body(out["video_error"])


async def test_an_unverified_caption_fetch_is_reported(page, ytdlp, monkeypatch):
    """Codex P2 on #2698: tls_verified was dropped."""
    from genesis.knowledge.processors.youtube import YouTubeFetch

    async def fetch(self, url, *, audio_fallback=False):
        return YouTubeFetch(url=url, metadata={"title": "t"}, transcript="words",
                            caption={"kind": "manual"}, tls_verified=False)

    monkeypatch.setattr(LinkedInCaptionProcessor, "fetch", fetch)
    tool, _ = page
    out = await tool(url=_POST)
    assert out["video_tls_verified"] is False


async def test_a_long_transcript_is_clipped_and_flagged(page, ytdlp):
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}},
        "WEBVTT\n\n00:00.000 --> 00:01.000\n" + "w " * 5000)
    out = await tool(url=_POST, max_chars=2000)
    assert len(_body(out["video_transcript"])) <= 2000
    assert out["video_transcript_truncated"] is True
    assert "truncated" not in out  # the page itself was not clipped


async def test_a_short_transcript_is_not_flagged_truncated(page, ytdlp):
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    out = await tool(url=_POST)
    assert "video_transcript_truncated" not in out


@pytest.mark.parametrize("url", [_POST.removeprefix("https://"), "HTTPS://" + _POST[8:]])
async def test_a_post_url_without_or_with_an_uppercase_scheme_still_gets_captions(page, ytdlp, url):
    """Codex P2 on #2698: an uppercase scheme was prefixed again."""
    tool, calls = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    out = await tool(url=url)
    assert "hello from the video" in _body(out["video_transcript"])


async def test_an_explicit_backend_skips_the_caption_lookup(page, ytdlp):
    tool, _ = page
    use, seen = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    await tool(url=_POST, backend="tinyfish")
    assert seen == []


async def test_a_batch_entry_carries_the_video_fields(page, ytdlp):
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    out = await tool(urls=["https://example.com/a", _POST])
    by_url = {_body(r["url"]): r for r in out["results"]}
    assert _body(by_url[_POST]["text"]).startswith("post ")
    assert _body(by_url[_POST]["video_transcript"]) == "hello from the video"
    assert "video_transcript" not in by_url["https://example.com/a"]


async def test_a_batch_post_with_no_page_keeps_the_page_error_and_the_video(page, ytdlp, monkeypatch):
    """Codex/Devin on #2698: a transcript-only row hid the page failure, and a
    missing-page row lost the caption error."""
    from genesis.mcp.health import web_tools

    async def multi(urls, max_chars=50000):
        return {"results": [], "errors": [{"url": u, "error": "page_not_found"} for u in urls],
                "backend_used": "tinyfish"}

    monkeypatch.setattr(web_tools, "_impl_web_fetch_multi", multi)
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    out = await tool(urls=[_POST])
    assert out["results"] == []
    row = out["errors"][0]
    assert _body(row["error"]) == "page_not_found"
    assert _body(row["video_transcript"]) == "hello from the video"


async def test_a_batch_with_a_youtube_video_and_a_post_keeps_each_result_on_its_url(
    page, ytdlp, monkeypatch
):
    """Class audit: the two overlays share one gather; results must not cross."""
    from genesis.mcp.health import youtube_route

    async def video(url, max_chars):
        return {"url": url, "content": "yt transcript", "backend_used": "yt-dlp"}, None

    monkeypatch.setattr(youtube_route, "fetch_youtube", video)
    tool, _ = page
    use, _ = ytdlp
    use({"title": "Post text", "subtitles": {"en": _LICDN}}, _VTT)
    yt_url = "https://youtu.be/abc123"
    out = await tool(urls=[yt_url, _POST])
    by_url = {_body(r["url"]): r for r in out["results"]}
    assert _body(by_url[yt_url]["content"]) == "yt transcript"
    assert "video_transcript" not in by_url[yt_url]
    assert _body(by_url[_POST]["video_transcript"]) == "hello from the video"
