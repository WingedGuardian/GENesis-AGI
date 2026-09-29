"""YouTube fetch behind web_fetch: host allowlist, caption choice, the yt-dlp
argv, the certificate lever, and the MCP route (follow-up d83569bf)."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from genesis.knowledge.processors import youtube as yt
from genesis.knowledge.processors import youtube_config
from genesis.knowledge.processors.youtube import (
    YouTubeFetch,
    YouTubeProcessor,
    is_youtube_video_url,
    select_caption,
)

# ─── host allowlist ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123&t=30",
        "https://m.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/live/abc123",
        "http://WWW.YOUTUBE.COM/watch?v=abc123",
    ],
)
def test_video_urls_on_youtube_hosts_are_routed(url):
    assert is_youtube_video_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/?u=https://www.youtube.com/watch?v=abc123",  # substring lookalike
        "https://www.youtube.com.evil.example/watch?v=abc123",
        "https://yewtu.be/watch?v=abc123",  # a mirror yt-dlp's extractor would accept
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/watch",  # no video id
        "https://youtu.be/",
        "https://www.youtube.com/shorts/",  # prefix with no video id
        "ftp://youtube.com/watch?v=abc123",
        "",
    ],
)
def test_everything_else_is_not(url):
    assert not is_youtube_video_url(url)


# ─── caption choice ─────────────────────────────────────────────────────────


def _tracks(*keys):
    return {k: [{"ext": "vtt"}] for k in keys}


def test_a_dubbed_video_keeps_its_original_language_track():
    """MEASURED 2026-09-28 on a French talk with an AI English dub: the upstream
    rule took the alphabetically first "-orig" track (the dub's, en-US-orig)."""
    info = {
        "language": "fr-FR",
        "subtitles": {"live_chat": [{"ext": "json"}]},
        "automatic_captions": _tracks("en", "fr", "en-US-orig", "en-US", "fr-orig"),
    }
    assert select_caption(info) == {
        "key": "fr-orig",
        "language": "fr",
        "kind": "automatic",
        "provenance": "original",
    }


def test_manual_captions_in_the_original_language_beat_automatic():
    info = {
        "language": "de",
        "subtitles": _tracks("de", "en"),
        "automatic_captions": _tracks("de-orig", "en"),
    }
    assert select_caption(info) == {
        "key": "de",
        "language": "de",
        "kind": "manual",
        "provenance": "original",
    }


def test_a_lone_orig_track_names_the_language_when_metadata_does_not():
    info = {"automatic_captions": _tracks("es", "en", "es-orig")}
    assert select_caption(info)["key"] == "es-orig"


def test_no_language_evidence_takes_one_track_labelled_unknown():
    info = {"subtitles": _tracks("ja", "en")}
    assert select_caption(info) == {
        "key": "en",
        "language": "en",
        "kind": "manual",
        "provenance": "unknown",
    }


def test_no_tracks_means_none():
    assert select_caption({"language": "en", "subtitles": {"live_chat": [{}]}}) is None


# ─── argv and the certificate lever ─────────────────────────────────────────


def test_every_call_is_one_fixed_argv_without_config_or_cookies():
    argv = YouTubeProcessor._argv(["--skip-download"], "https://youtu.be/abc123", verify=True)
    assert argv[:3] == [sys.executable, "-m", "yt_dlp"]
    for flag in ("--ignore-config", "--no-cookies", "--no-cookies-from-browser", "--no-playlist"):
        assert flag in argv
    assert argv[argv.index("--use-extractors") + 1] == "youtube"
    assert argv[-2:] == ["--", "https://youtu.be/abc123"]
    assert "--no-check-certificates" not in argv
    assert "--no-check-certificates" in YouTubeProcessor._argv([], None, verify=False)


class _FakeExec:
    """Stands in for one yt-dlp process per call: pops (rc, stderr) outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    async def __call__(self, argv):
        self.calls.append(argv)
        rc, stderr = self.outcomes.pop(0)
        return rc, b"", stderr.encode()


_CERT = (
    "ERROR: [youtube] abc: Unable to download webpage: [SSL: CERTIFICATE_VERIFY_FAILED] "
    "certificate verify failed: self-signed certificate (caused by CertificateVerifyError(...))"
)


async def _run_once(monkeypatch, mode, *outcomes):
    fake = _FakeExec(*outcomes)
    monkeypatch.setattr(yt, "_exec", fake)
    monkeypatch.setattr(yt, "tls_mode", lambda: mode)
    result = YouTubeFetch(url="https://youtu.be/abc123")
    rc, _, _ = await YouTubeProcessor()._run(result, ["--skip-download"], result.url)
    return rc, result, fake.calls


async def test_auto_fallback_retries_once_unverified_on_a_certificate_error(monkeypatch):
    rc, result, calls = await _run_once(monkeypatch, "auto_fallback", (1, _CERT), (0, ""))
    assert rc == 0 and len(calls) == 2
    assert "--no-check-certificates" not in calls[0]
    assert "--no-check-certificates" in calls[1]
    assert "--ignore-config" in calls[1] and "--no-cookies" in calls[1]
    assert result.tls_verified is False


async def test_auto_fallback_does_not_retry_other_failures(monkeypatch):
    rc, result, calls = await _run_once(
        monkeypatch,
        "auto_fallback",
        (1, "ERROR: [youtube] abc: Video unavailable"),
    )
    assert rc == 1 and len(calls) == 1 and result.tls_verified is True


async def test_verify_never_skips_verification(monkeypatch):
    rc, result, calls = await _run_once(monkeypatch, "verify", (1, _CERT))
    assert rc == 1 and len(calls) == 1
    assert "--no-check-certificates" not in calls[0] and result.tls_verified is True


async def test_off_always_skips_verification(monkeypatch):
    _, result, calls = await _run_once(monkeypatch, "off", (0, ""))
    assert "--no-check-certificates" in calls[0] and result.tls_verified is False


async def test_after_one_fallback_the_rest_of_the_fetch_stays_unverified(monkeypatch):
    fake = _FakeExec((0, ""))
    monkeypatch.setattr(yt, "_exec", fake)
    monkeypatch.setattr(yt, "tls_mode", lambda: "auto_fallback")
    result = YouTubeFetch(url="https://youtu.be/abc123", tls_verified=False)
    await YouTubeProcessor()._run(result, [], None)
    assert len(fake.calls) == 1 and "--no-check-certificates" in fake.calls[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("verify", "verify"),
        ("auto_fallback", "auto_fallback"),
        ("off", "off"),
        (False, "off"),
        ("sometimes", "verify"),
        (None, "verify"),
    ],
)
def test_tls_mode_reads_the_lever_and_degrades_to_verify(monkeypatch, value, expected):
    monkeypatch.setattr(youtube_config, "load_config", lambda: {"tls": value})
    assert youtube_config.tls_mode() == expected


def test_the_shipped_default_is_auto_fallback():
    assert youtube_config.DEFAULTS["tls"] == "auto_fallback"
    assert youtube_config.load_config()["tls"] in youtube_config.TLS_MODES


def test_settings_validator_accepts_only_the_modes():
    from genesis.mcp.health.settings import _validate_youtube_fetch

    assert _validate_youtube_fetch({"tls": "verify"}) == []
    assert _validate_youtube_fetch({"tls": "never"})
    assert _validate_youtube_fetch({"cookies": "x"})


# ─── fetch end to end, with yt-dlp faked at the process boundary ────────────


def _fake_ytdlp(info: dict, vtt: str | None):
    """An _exec that writes what the real yt-dlp would, given the argv."""

    async def run(argv):
        out = argv[argv.index("-o") + 1].replace("%%", "%")
        stem = out.replace("video.%(ext)s", "video.")
        if "--write-info-json" in argv:
            with open(stem + "info.json", "w") as fh:
                json.dump(info, fh)
        elif vtt is not None and "--sub-langs" in argv:
            key = argv[argv.index("--sub-langs") + 1].split("^")[1].rstrip("$").replace("\\", "")
            with open(stem + key + ".vtt", "w") as fh:
                fh.write(vtt)
        return 0, b"", b""

    return run


_VTT = "WEBVTT\nKind: captions\nLanguage: fr\n\n00:00:00.000 --> 00:00:02.000\nBonsoir à tous\n"


async def test_fetch_returns_metadata_and_the_chosen_transcript(monkeypatch, tmp_path):
    info = {
        "title": "T",
        "channel": "C",
        "language": "fr-FR",
        "description": "D",
        "automatic_captions": _tracks("en", "fr-orig"),
    }
    monkeypatch.setattr(yt, "_exec", _fake_ytdlp(info, _VTT))
    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)
    result = await YouTubeProcessor().fetch("https://youtu.be/abc123", audio_fallback=False)
    assert result.transcript == "Bonsoir à tous"
    assert result.caption["key"] == "fr-orig" and result.metadata["title"] == "T"
    assert result.errors == []


async def test_fetch_refuses_a_non_youtube_host_without_running_anything(monkeypatch):
    async def boom(argv):
        raise AssertionError("yt-dlp must not run")

    monkeypatch.setattr(yt, "_exec", boom)
    result = await YouTubeProcessor().fetch("https://yewtu.be/watch?v=abc123")
    assert result.transcript is None and result.errors == ["not a YouTube video URL"]


async def test_process_keeps_its_contract_and_raises_without_a_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(yt, "_exec", _fake_ytdlp({"title": "T"}, None))
    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)

    async def no_audio(self, result, url, **kw):
        return None

    monkeypatch.setattr(YouTubeProcessor, "_transcribe_audio", no_audio)
    with pytest.raises(RuntimeError, match="Could not extract transcript"):
        await YouTubeProcessor().process("https://youtu.be/abc123")


async def test_a_cancelled_fetch_kills_the_whole_process_group():
    """A caller's timeout must not orphan yt-dlp or its JavaScript runtime."""
    import os

    task = asyncio.create_task(yt._exec([sys.executable, "-c", "import time; time.sleep(60)"]))
    await asyncio.sleep(0.5)
    children = [p for p in os.listdir("/proc") if p.isdigit()]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Every python child we started with that exact command line is gone.
    for pid in children:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                assert b"time.sleep(60)" not in fh.read()
        except (FileNotFoundError, ProcessLookupError):
            pass


# ─── the web_fetch MCP route ────────────────────────────────────────────────


@pytest.fixture
def web(monkeypatch):
    from genesis.mcp.health import web_tools, youtube_route

    calls = {"page": [], "yt": []}

    async def page(url, backend="auto", max_chars=50000):
        calls["page"].append(url)
        return {"url": url, "content": "page", "backend_used": "scrapling", "error": None}

    async def multi(urls, max_chars=50000):
        return {"results": [{"url": u, "text": "tf"} for u in urls], "backend_used": "tinyfish"}

    monkeypatch.setattr(web_tools, "_impl_web_fetch", page)
    monkeypatch.setattr(web_tools, "_impl_web_fetch_multi", multi)

    def set_fetch(transcript):
        async def fetch(self, url, *, audio_fallback=True):
            calls["yt"].append(url)
            return YouTubeFetch(
                url=url,
                metadata={"title": "V", "url": url},
                transcript=transcript,
                caption={"key": "en", "language": "en", "kind": "manual", "provenance": "original"}
                if transcript
                else None,
                errors=[] if transcript else ["ERROR: private video"],
            )

        monkeypatch.setattr(youtube_route.YouTubeProcessor, "fetch", fetch)

    tool = getattr(web_tools.web_fetch, "fn", web_tools.web_fetch)
    return tool, calls, set_fetch


async def test_a_youtube_url_returns_its_transcript(web):
    tool, calls, set_fetch = web
    set_fetch("hello there")
    out = await tool(url="https://youtu.be/abc123")
    assert out["backend_used"] == "yt-dlp" and "hello there" in out["content"]
    assert "provenance: original" in out["content"] and calls["page"] == []


async def test_a_schemeless_youtube_url_is_routed_too(web):
    tool, calls, set_fetch = web
    set_fetch("hi")
    out = await tool(url="youtube.com/watch?v=abc123")
    assert out["backend_used"] == "yt-dlp"


async def test_a_failed_youtube_fetch_falls_back_to_the_page_and_says_why(web):
    tool, calls, set_fetch = web
    set_fetch(None)
    out = await tool(url="https://youtu.be/abc123")
    assert out["content"] == "page" and "private video" in out["youtube_error"]


async def test_a_lookalike_url_never_reaches_yt_dlp(web):
    tool, calls, set_fetch = web
    set_fetch("x")
    await tool(url="https://evil.example/?u=https://www.youtube.com/watch?v=abc123")
    assert calls["yt"] == [] and len(calls["page"]) == 1


async def test_an_explicit_backend_skips_the_youtube_route(web):
    tool, calls, set_fetch = web
    set_fetch("x")
    await tool(url="https://youtu.be/abc123", backend="scrapling")
    assert calls["yt"] == []


async def test_multi_url_routes_youtube_entries_and_batches_the_rest(web):
    tool, calls, set_fetch = web
    set_fetch("t")
    out = await tool(urls=["https://youtu.be/abc123", "https://example.com/a"])
    backends = [r.get("backend_used") for r in out["results"]]
    assert backends[0] == "yt-dlp" and out["results"][1]["url"] == "https://example.com/a"


async def test_a_batch_without_youtube_keeps_the_batch_backend(web):
    tool, calls, set_fetch = web
    set_fetch("t")
    out = await tool(urls=["https://example.com/a", "https://example.com/b"])
    assert out["backend_used"] == "tinyfish" and calls["page"] == []


async def test_a_small_budget_keeps_the_transcript_before_the_description(web, monkeypatch):
    """#2568 review (Devin): a long description consumed the whole budget, so a
    fetched transcript never reached the caller."""
    from genesis.mcp.health import youtube_route

    tool, calls, set_fetch = web

    async def fetch(self, url, *, audio_fallback=True):
        return YouTubeFetch(url=url, metadata={"title": "V", "description": "D" * 5000},
                            transcript="the actual speech",
                            caption={"key": "en", "language": "en", "kind": "manual",
                                     "provenance": "original"})

    monkeypatch.setattr(youtube_route.YouTubeProcessor, "fetch", fetch)
    out = await tool(url="https://youtu.be/abc123", max_chars=300)
    assert "the actual speech" in out["content"]


async def test_the_transcript_is_clipped_to_max_chars(web):
    tool, calls, set_fetch = web
    set_fetch("word " * 1000)
    out = await tool(url="https://youtu.be/abc123", max_chars=200)
    assert len(out["content"]) == 200 and out["truncated"] is True


# ─── stderr classification (real yt-dlp output, MEASURED 2026-09-28) ─────────


@pytest.mark.parametrize(
    ("stderr", "is_cert"),
    [
        ("ERROR: [generic] x: Unable to download webpage: [SSL: CERTIFICATE_VERIFY_FAILED] "
         "certificate verify failed: self-signed certificate (_ssl.c:1000)", True),
        ("ERROR: [generic] x: Unable to download webpage: Failed to perform, curl: (60) SSL "
         "certificate OpenSSL verify result: certificate has expired (10).", True),
        # A broken local trust store is not a verification failure: no fallback.
        ("ERROR: [download] Got error: Failed to perform, curl: (77) error adding trust "
         "anchors from locations", False),
        ("ERROR: [youtube] abc: Video unavailable", False),
    ],
)
def test_only_certificate_verification_failures_trigger_the_fallback(stderr, is_cert):
    assert bool(yt._CERT_ERROR.search(stderr)) is is_cert


def test_the_diagnostic_keeps_an_error_line_yt_dlp_split_with_a_carriage_return():
    stderr = "ERROR: \r[download] Got error: HTTP Error 403: Forbidden. Giving up after 10 retries\n"
    diag = yt.network_diagnostic(stderr)
    assert "HTTP Error 403" in diag and "updating yt-dlp" in diag



def test_a_caption_key_that_could_leave_the_temp_dir_is_never_chosen():
    """Security review: the track key comes from video metadata and becomes part
    of a file path; a key with a path separator must never be selected."""
    info = {"language": "en", "subtitles": {"x/../../../../etc/cron.d/evil": [{"ext": "vtt"}],
                                            "en/..": [{"ext": "vtt"}]}}
    assert select_caption(info) is None
    info["automatic_captions"] = _tracks("en-orig")
    assert select_caption(info)["key"] == "en-orig"


async def test_a_mixed_batch_fetches_each_url_on_its_own(web, monkeypatch):
    """#2568 review (Devin): matching batch results to URLs by position misfiled
    a page when the batch backend omitted one. A batch holding a YouTube link
    now fetches every URL on its own, so each result belongs to its URL."""
    from genesis.mcp.health import web_tools

    tool, calls, set_fetch = web
    set_fetch("t")

    async def never(urls, max_chars=50000):
        raise AssertionError("a mixed batch must not go through the positional batch path")

    monkeypatch.setattr(web_tools, "_impl_web_fetch_multi", never)
    out = await tool(urls=["https://example.com/a", "https://youtu.be/abc123", "https://example.com/b"])
    assert [r["url"] for r in out["results"]] == [
        "https://example.com/a", "https://youtu.be/abc123", "https://example.com/b"]
    assert out["results"][1]["backend_used"] == "yt-dlp"
    assert sorted(calls["page"]) == ["https://example.com/a", "https://example.com/b"]


async def test_one_failing_url_does_not_sink_the_batch(web, monkeypatch):
    from genesis.mcp.health import web_tools

    tool, calls, set_fetch = web
    set_fetch("t")

    async def boom(url, backend="auto", max_chars=50000):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(web_tools, "_impl_web_fetch", boom)
    out = await tool(urls=["https://example.com/a", "https://youtu.be/abc123"])
    assert "backend exploded" in out["results"][0]["error"]
    assert out["results"][1]["backend_used"] == "yt-dlp"


async def test_batch_fetches_run_concurrently(web, monkeypatch):
    """#2568 review (Devin): one slow video held up every other link."""
    from genesis.mcp.health import youtube_route

    tool, calls, set_fetch = web
    active = {"now": 0, "peak": 0}

    async def slow(self, url, *, audio_fallback=True):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return YouTubeFetch(url=url, metadata={"title": "V"}, transcript="t",
                            caption={"key": "en", "language": "en", "kind": "manual",
                                     "provenance": "original"})

    monkeypatch.setattr(youtube_route.YouTubeProcessor, "fetch", slow)
    await tool(urls=["https://youtu.be/a1", "https://youtu.be/b2", "https://youtu.be/c3"])
    assert active["peak"] == 3


async def test_multi_url_results_keep_the_callers_order(web):
    tool, calls, set_fetch = web
    set_fetch("t")
    out = await tool(urls=["https://example.com/a", "https://youtu.be/abc123"])
    assert [r["url"] for r in out["results"]] == ["https://example.com/a", "https://youtu.be/abc123"]


# ─── the audio-fallback duration cap (owner decision 2026-09-29) ─────────────


@pytest.mark.parametrize(
    ("duration", "cap", "transcribes"),
    [(600, 120, True), (7200, 120, True), (7201, 120, False), (36000, 120, False),
     (None, 120, False), (60, 0, False)],
)
async def test_the_audio_fallback_runs_only_within_the_duration_cap(
    monkeypatch, tmp_path, duration, cap, transcribes,
):
    info = {"title": "T"} if duration is None else {"title": "T", "duration": duration}
    monkeypatch.setattr(yt, "_exec", _fake_ytdlp(info, None))
    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "audio_max_minutes", lambda: cap)
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)
    called = []

    async def audio(self, result, url, **kw):
        called.append(url)
        return "spoken words"

    monkeypatch.setattr(YouTubeProcessor, "_transcribe_audio", audio)
    result = await YouTubeProcessor().fetch("https://youtu.be/abc123")
    assert bool(called) is transcribes
    if not transcribes:
        assert result.transcript is None
        assert any("audio_max_minutes" in e for e in result.errors), result.errors


@pytest.mark.parametrize(("value", "expected"), [(30, 30), (0, 0), (-1, 120), (True, 120), ("9", 120), (None, 120)])
def test_audio_max_minutes_reads_the_lever_and_degrades_to_the_default(monkeypatch, value, expected):
    monkeypatch.setattr(youtube_config, "load_config", lambda: {"audio_max_minutes": value})
    assert youtube_config.audio_max_minutes() == expected


def test_settings_validator_checks_audio_max_minutes():
    from genesis.mcp.health.settings import _validate_youtube_fetch

    assert _validate_youtube_fetch({"audio_max_minutes": 60}) == []
    assert _validate_youtube_fetch({"audio_max_minutes": -5})
    assert _validate_youtube_fetch({"audio_max_minutes": True})



# ─── #2568 round 1: the audio bound, caption hosts, the VTT header ──────────


@pytest.mark.parametrize(
    ("extra", "reason"),
    [({"duration": 0}, "unknown length"), ({"duration": 60, "is_live": True}, "live"),
     ({"duration": 60, "live_status": "is_upcoming"}, "live"),
     ({"duration": 60, "live_status": "post_live"}, "live"), ({"duration": True}, "unknown length")],
)
async def test_the_audio_fallback_never_runs_for_an_unbounded_stream(monkeypatch, tmp_path, extra, reason):
    """#2568 review (Devin + CodeRabbit): a zero duration, or a live stream,
    passed the length cap."""
    monkeypatch.setattr(yt, "_exec", _fake_ytdlp({"title": "T", **extra}, None))
    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "audio_max_minutes", lambda: 120)
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)

    async def audio(self, result, url, **kw):
        raise AssertionError("audio must not be downloaded")

    monkeypatch.setattr(YouTubeProcessor, "_transcribe_audio", audio)
    result = await YouTubeProcessor().fetch("https://youtu.be/abc123")
    assert any(reason in e for e in result.errors), result.errors


async def test_the_audio_download_repeats_the_length_check_in_yt_dlp(monkeypatch, tmp_path):
    seen = []

    async def run(argv):
        seen.append(argv)
        return 0, b"", b""

    monkeypatch.setattr(yt, "_exec", run)
    monkeypatch.setattr(yt, "tls_mode", lambda: "verify")
    monkeypatch.setattr(yt, "big_tmp_dir", lambda: tmp_path)
    result = YouTubeFetch(url="https://youtu.be/abc123")
    assert await YouTubeProcessor()._transcribe_audio(result, result.url, max_seconds=600) is None
    argv = seen[0]
    assert argv[argv.index("--match-filters") + 1] == "!is_live & duration > 0 & duration <= 600"
    assert any("not downloaded" in e for e in result.errors)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [("https://www.youtube.com/api/timedtext?v=x&lang=en", True),
     ("https://rr1---sn-abc.googlevideo.com/x", True),
     ("https://evil.example/timedtext", False),
     ("http://www.youtube.com/api/timedtext", False),
     ("https://youtube.com.evil.example/x", False)],
)
def test_a_caption_track_is_used_only_from_youtube_hosts(url, allowed):
    """#2568 review (Devin): caption URLs come from metadata, and the caption
    download follows them as given."""
    info = {"language": "en", "subtitles": {"en": [{"ext": "vtt", "url": url}]}}
    assert (select_caption(info) is not None) is allowed


def test_speech_that_starts_with_language_is_kept():
    """#2568 review (Devin): the header rule dropped a spoken line."""
    vtt = ("WEBVTT\nKind: captions\nLanguage: en\n\n00:00:00.000 --> 00:00:02.000\n"
           "Language: Python is the topic\n\n00:00:02.000 --> 00:00:04.000\nKind: of great\n")
    assert YouTubeProcessor._parse_vtt(vtt) == "Language: Python is the topic Kind: of great"
