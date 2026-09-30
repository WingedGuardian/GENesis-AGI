"""YouTube processor: metadata and transcript through yt-dlp.

Used by knowledge ingestion (``process``) and by the genesis ``web_fetch`` MCP
tool (``fetch``), which is how a session without Bash reads a video.

Every yt-dlp call is one deterministic argv: the venv's own ``yt_dlp`` module,
``--ignore-config`` (no user config file can add an output path, a command or
credentials), no cookies, the YouTube extractor only, one video only, and
``--`` before the URL so a URL can never be read as an option. Certificate
verification follows the ``youtube_fetch`` lever (``youtube_config``).

Caption selection and the failure diagnostics are adapted from claude-video
(github.com/bradautomates/claude-video, ``skills/watch/scripts/download.py``,
MIT License, Copyright (c) 2026 Bradley Bonanno).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from genesis.knowledge.processors.base import ProcessedContent
from genesis.knowledge.processors.youtube_config import audio_max_minutes, tls_mode
from genesis.util.proc_kill import kill_process_group, reap_bounded
from genesis.util.tmp import big_tmp_dir

logger = logging.getLogger(__name__)

# One audio transcription at a time per process: each can download up to the
# length cap of audio and hold it in memory for speech-to-text (#2568 review).
_AUDIO_SLOTS = asyncio.Semaphore(1)
# At most three yt-dlp processes at once per process: a 10-link batch would
# otherwise start ten, each with a JavaScript runtime (#2568 class audit).
_YTDLP_SLOTS = asyncio.Semaphore(3)
# Audio download ceiling per allowed minute of video. 2 MB per minute is well
# above speech bitrates, so a real track fits and a padded stream cannot.
_AUDIO_MB_PER_MINUTE = 2

_YOUTUBE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w-]+"
)

# Hosts whose video URLs this processor fetches. yt-dlp's YouTube extractor
# also accepts many third-party mirror hosts, so the host is checked here,
# never left to the extractor.
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_VIDEO_PATH_PREFIXES = ("/shorts/", "/live/", "/embed/")

# A caption track key comes from the video's own metadata and becomes part of a
# file name and a yt-dlp filter, so it is held to language-tag characters.
_TRACK_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")

# Hosts a caption track may be downloaded from. The track URLs come from the
# video's metadata, and a caption download from --load-info-json follows them
# as given, so a track pointing anywhere else is dropped (#2568 review). If
# that download fails, yt-dlp may re-extract from the video page and follow
# fresh URLs this check never saw; those still come from the YouTube extractor.
_CAPTION_HOST_SUFFIXES = (".youtube.com", ".googlevideo.com")


def _caption_urls_allowed(formats: object) -> bool:
    if not isinstance(formats, list) or not formats:
        return False
    for fmt in formats:
        url = fmt.get("url") if isinstance(fmt, dict) else None
        if url is None:
            continue
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if parts.scheme != "https" or not (
            host == "youtube.com" or host.endswith(_CAPTION_HOST_SUFFIXES)
        ):
            return False
    return True

# A certificate-VERIFICATION failure, in both forms yt-dlp prints (MEASURED
# 2026-09-28 against self-signed, untrusted-root and expired test hosts): Python
# ssl ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: ...") and
# curl_cffi, which yt-dlp uses for some requests ("curl: (60) SSL certificate
# OpenSSL verify result: ..."). A local CA-loading failure (curl 77, "error
# adding trust anchors") is deliberately NOT one: skipping verification is no
# fix for a broken trust store.
_CERT_ERROR = re.compile(
    r"certificate_verify_failed|certificate verify failed|CertificateVerifyError|"
    r"unable to get local issuer certificate|curl: \(60\)|SSL certificate problem",
    re.IGNORECASE,
)


def is_youtube_video_url(url: str) -> bool:
    """True for a single-video URL on a YouTube host (not a playlist or channel)."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if host in _SHORT_HOSTS:
        return len(parts.path.strip("/")) > 0
    if host not in _YOUTUBE_HOSTS:
        return False
    if parts.path == "/watch":
        return bool(parse_qs(parts.query).get("v"))
    segments = parts.path.split("/")
    return parts.path.startswith(_VIDEO_PATH_PREFIXES) and len(segments) > 2 and bool(segments[2])


def network_diagnostic(stderr: str) -> str:
    """The last yt-dlp error line, with a hint for the common failure classes."""
    # yt-dlp rewrites its progress line with "\r", so an ERROR line can contain one.
    text = (stderr or "").replace("\r", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    detail = (errors or lines or ["yt-dlp failed with no output"])[-1][:500]
    lower = detail.lower()
    if any(s in lower for s in ("javascript runtime", "js runtime", "ejs", "challenge solver")):
        hint = "YouTube's JavaScript challenge could not be solved; yt-dlp or its solver package needs an update."
    elif any(s in lower for s in ("sign in", "login required", "authentication required", "not a bot")):
        hint = "This video requires signing in; it cannot be fetched without an account."
    elif "429" in lower or "too many requests" in lower:
        hint = "YouTube is rate limiting requests; try again later."
    elif re.search(r"\b403\b", lower) or "forbidden" in lower:
        hint = "HTTP 403; usually fixed by updating yt-dlp to its latest release."
    elif _CERT_ERROR.search(lower):
        hint = "Certificate verification failed (see the youtube_fetch tls setting)."
    elif any(s in lower for s in ("private video", "video unavailable", "has been removed")):
        hint = "The video is private, removed or unavailable."
    else:
        hint = "Unclassified yt-dlp failure."
    return f"{detail} — {hint}"


def select_caption(info: dict) -> dict | None:
    """Choose at most one caption track: the video's original language first,
    manual captions over automatic ones, with the choice's provenance labelled.

    Adapted from claude-video's ``select_caption`` (MIT), auto mode only.
    Returns ``{key, language, kind, provenance}`` or ``None``.
    """
    def usable(tracks: object) -> dict:
        if not isinstance(tracks, dict):
            return {}
        return {
            k: v for k, v in tracks.items()
            if v and k != "live_chat" and isinstance(k, str) and _TRACK_KEY.fullmatch(k)
            and _caption_urls_allowed(v)
        }

    manual = usable(info.get("subtitles"))
    automatic = usable(info.get("automatic_captions"))
    # The video's language (yt-dlp takes it from the original audio track)
    # first; else a lone "<lang>-orig" speech-recognition track names it.
    originals = [k for k in automatic if k.endswith("-orig")]
    original = originals[0].removesuffix("-orig") if len(originals) == 1 else None
    target = info.get("language") or original

    def base(key: str) -> str:
        return key.removesuffix("-orig").split("-")[0]

    def matches(key: str) -> bool:
        return bool(target) and base(key) == str(target).split("-")[0]

    def english_first(key: str) -> tuple[bool, str]:
        return base(key) != "en", key

    if target:
        for kind, tracks in (("manual", manual), ("automatic", automatic)):
            # Within the target language, the "-orig" speech-recognition track
            # beats a translated one. (Upstream swapped in originals[0] blindly,
            # which picks an AI-dubbed track's "-orig" when a video has two.)
            keys = sorted(
                (k for k in tracks if matches(k)),
                key=lambda k: (not k.endswith("-orig"), k != target, k),
            )
            if keys:
                key = keys[0]
                # "language-match": the track's language matches the video's
                # stated language. Not a check of the captions against the audio.
                return {"key": key, "language": key.removesuffix("-orig"), "kind": kind,
                        "provenance": "language-match"}
    # No language evidence matched: take one available track, labelled unknown.
    for kind, tracks in (("manual", manual), ("automatic", automatic)):
        if tracks:
            key = min(tracks, key=english_first)
            return {"key": key, "language": key.removesuffix("-orig"), "kind": kind,
                    "provenance": "unknown"}
    return None


@dataclass
class YouTubeFetch:
    """What one fetch of a video produced."""

    url: str
    metadata: dict = field(default_factory=dict)
    transcript: str | None = None
    caption: dict | None = None
    tls_verified: bool = True
    errors: list[str] = field(default_factory=list)


def _summary(info: dict, url: str) -> dict:
    return {
        "title": info.get("title", ""),
        "channel": info.get("channel") or info.get("uploader", ""),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "language": info.get("language"),
        "description": info.get("description") or "",
        "url": info.get("webpage_url") or url,
    }


async def _exec(argv: list[str]) -> tuple[int, bytes, bytes]:
    """Run one yt-dlp process in its own group; kill the group if cancelled,
    so a caller's timeout never orphans yt-dlp or its JavaScript runtime."""
    async with _YTDLP_SLOTS:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await proc.communicate()
        except BaseException:
            kill_process_group(proc)
            await reap_bounded(proc)
            raise
        return proc.returncode or 0, stdout, stderr


class YouTubeProcessor:
    """Extract metadata and transcripts from YouTube videos via yt-dlp."""

    def can_handle(self, source: str) -> bool:
        return bool(_YOUTUBE_PATTERN.search(source))

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        result = await self.fetch(source)
        if not result.transcript:
            reason = "; ".join(result.errors) or "no captions and no audio transcript"
            raise RuntimeError(f"Could not extract transcript from {source}: {reason}")
        return ProcessedContent(
            text=result.transcript,
            metadata={**result.metadata, "caption": result.caption,
                      "tls_verified": result.tls_verified},
            source_type="youtube",
            source_path=source,
        )

    async def fetch(self, url: str, *, audio_fallback: bool = True) -> YouTubeFetch:
        """Metadata plus the best caption track; audio transcription when the
        video has no captions and ``audio_fallback`` is set."""
        url = url.strip()
        if "://" not in url:
            url = "https://" + url  # the ingestion registry pattern allows no scheme
        result = YouTubeFetch(url=url, metadata={"url": url})
        if not is_youtube_video_url(url):
            result.errors.append("not a YouTube video URL")
            return result
        if url.lower().startswith("http://"):
            # Never fetch a cleartext YouTube URL: an on-path responder could
            # answer before the HTTPS redirect, and the result would still
            # claim verified TLS (#2568 review).
            url = "https://" + url[len("http://"):]
            result.url = url
        if importlib.util.find_spec("yt_dlp") is None:
            result.errors.append("yt-dlp is not installed")
            return result

        with tempfile.TemporaryDirectory(dir=big_tmp_dir()) as tmp:
            out = str(Path(tmp).resolve()).replace("%", "%%") + "/video.%(ext)s"
            info_path = Path(tmp) / "video.info.json"
            rc, _, stderr = await self._run(
                result,
                ["--skip-download", "--write-info-json", "--no-write-subs",
                 "--no-write-auto-subs", "-o", out],
                url,
            )
            if rc != 0 or not info_path.is_file():
                result.errors.append(network_diagnostic(stderr))
                return result
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                result.errors.append(f"yt-dlp metadata unreadable: {type(exc).__name__}")
                return result
            result.metadata = _summary(info, url)
            live_status = info.get("live_status")
            # Only a finished recording has a length that bounds its audio.
            recorded = info.get("is_live") is not True and live_status in (
                None, "not_live", "was_live",
            )

            track = select_caption(info)
            if track:
                manual = track["kind"] == "manual"
                rc, _, stderr = await self._run(
                    result,
                    ["--load-info-json", str(info_path), "--skip-download",
                     "--no-write-info-json",
                     "--write-subs" if manual else "--no-write-subs",
                     "--write-auto-subs" if not manual else "--no-write-auto-subs",
                     "--sub-langs", "-all,^" + re.escape(track["key"]) + "$",
                     "--sub-format", "vtt", "-o", out],
                    None,
                )
                vtt = Path(tmp) / f"video.{track['key']}.vtt"
                if vtt.is_file():
                    result.transcript = self._parse_vtt(vtt.read_text(errors="replace")) or None
                    result.caption = track
                else:
                    result.errors.append(
                        "caption download failed: "
                        + (network_diagnostic(stderr) if rc else "no caption file written")
                    )

        cap = audio_max_minutes()
        duration = result.metadata.get("duration")
        bounded = (
            recorded
            and isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and 0 < duration <= cap * 60
        )
        if not result.transcript and audio_fallback and not bounded:
            # Live, upcoming, zero, unknown or over-cap length: never download
            # an audio track whose size nothing bounds.
            audio_fallback = False
            if not recorded:
                why = "a live or unfinished stream"
            elif isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
                why = f"{int(duration) // 60} min long"
            else:
                why = "of unknown length"
            result.errors.append(
                f"no captions; audio transcription skipped: the video is {why} "
                f"(youtube_fetch audio_max_minutes: {cap})"
            )
        if not result.transcript and audio_fallback:
            logger.info("No captions for %s, attempting audio transcription", url)
            async with _AUDIO_SLOTS:
                text = await self._transcribe_audio(result, url, max_seconds=cap * 60)
            if text:
                result.transcript = text
                result.caption = {"key": None, "language": None, "kind": "audio transcription",
                                  "provenance": "speech-to-text"}
        return result

    async def _run(
        self, result: YouTubeFetch, args: list[str], url: str | None
    ) -> tuple[int, bytes, str]:
        """One yt-dlp call under the tls lever. Under ``auto_fallback`` a
        certificate-verification failure is retried once unverified, and the
        rest of this fetch then stays unverified (one warning per fetch)."""
        mode = tls_mode()
        verify = mode != "off" and result.tls_verified
        if not verify:
            result.tls_verified = False
        rc, stdout, raw = await _exec(self._argv(args, url, verify=verify))
        stderr = raw.decode(errors="replace")
        if rc != 0 and verify and mode == "auto_fallback" and _CERT_ERROR.search(stderr):
            logger.warning(
                "YouTube certificate verification failed for %s; retrying once without "
                "verification (youtube_fetch tls: auto_fallback): %s",
                url or "(cached info)", network_diagnostic(stderr),
            )
            result.tls_verified = False
            rc, stdout, raw = await _exec(self._argv(args, url, verify=False))
            stderr = raw.decode(errors="replace")
        return rc, stdout, stderr

    @staticmethod
    def _argv(args: list[str], url: str | None, *, verify: bool) -> list[str]:
        argv = [
            sys.executable, "-m", "yt_dlp",
            "--ignore-config", "--no-cookies", "--no-cookies-from-browser",
            "--no-playlist", "--use-extractors", "youtube",
            # yt-dlp enables only deno by default; Genesis requires Node.
            "--js-runtimes", "node",
            "--no-progress",
        ]
        if not verify:
            argv.append("--no-check-certificates")
        argv.extend(args)
        if url is not None:
            argv.extend(["--", url])
        return argv

    async def _transcribe_audio(
        self, result: YouTubeFetch, url: str, *, max_seconds: int
    ) -> str | None:
        """Download audio and transcribe via STT as fallback."""
        try:
            with tempfile.TemporaryDirectory(dir=big_tmp_dir()) as tmpdir:
                out = str(Path(tmpdir).resolve()).replace("%", "%%") + "/audio.%(ext)s"
                rc, _, stderr = await self._run(
                    result,
                    # The length check again at download time, by yt-dlp itself,
                    # in case the video changed since the metadata call.
                    ["--match-filters", f"!is_live & duration > 0 & duration <= {max_seconds}",
                     "--max-filesize", f"{max(1, max_seconds // 60) * _AUDIO_MB_PER_MINUTE}M",
                     "-x", "--audio-format", "mp3", "--audio-quality", "5", "-o", out],
                    url,
                )
                if rc != 0:
                    result.errors.append("audio download failed: " + network_diagnostic(stderr))
                    return None
                candidates = sorted(Path(tmpdir).glob("audio.*"))
                if not candidates:
                    result.errors.append("audio not downloaded (the length check rejected it)")
                    return None
                # --max-filesize bounds the DOWNLOAD; the converted file is a
                # separate file, so it is checked before it is read (#2568 review).
                limit = max(1, max_seconds // 60) * _AUDIO_MB_PER_MINUTE * 1024 * 1024
                if candidates[0].stat().st_size > limit:
                    result.errors.append("audio file larger than the audio size limit; not transcribed")
                    return None
                if candidates:
                    from genesis.channels.stt import transcribe

                    # transcribe() runs its request in a worker thread that a
                    # cancellation cannot stop, so a cancelled fetch waits for it
                    # before releasing the audio slot (#2568 review).
                    job = asyncio.ensure_future(transcribe(candidates[0].read_bytes()))
                    try:
                        text = await asyncio.shield(job)
                    except asyncio.CancelledError:
                        with contextlib.suppress(BaseException):
                            await job
                        raise
                    if not text:
                        result.errors.append(
                            "audio downloaded but speech-to-text returned nothing "
                            "(provider unavailable, unconfigured or no speech)"
                        )
                    return text or None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Audio transcription failed for %s", url, exc_info=True)
            result.errors.append("audio transcription failed")
        return None

    @staticmethod
    def _parse_vtt(vtt_text: str) -> str:
        """Extract plain text from WebVTT subtitle format."""
        lines: list[str] = []
        in_header = True  # header lines (WEBVTT, Kind:, Language:) precede the first cue
        for line in vtt_text.split("\n"):
            line = line.strip()
            if "-->" in line:
                in_header = False
                continue
            # Skip empty lines, and anything in the header block: a caption
            # that says "Language: ..." is speech, not a header.
            if not line or in_header:
                continue
            # Skip numeric cue identifiers
            if line.isdigit():
                continue
            # Strip HTML-like tags
            cleaned = re.sub(r"<[^>]+>", "", line)
            if cleaned and cleaned not in lines[-1:]:
                lines.append(cleaned)
        return " ".join(lines)
