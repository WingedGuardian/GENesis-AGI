"""YouTube processor using yt-dlp for transcript extraction."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_YOUTUBE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w-]+"
)


class YouTubeProcessor:
    """Extract transcripts from YouTube videos via yt-dlp."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        if not shutil.which("yt-dlp"):
            raise RuntimeError("yt-dlp is not installed")

        # Try to get subtitles/captions first
        text = await self._get_subtitles(source)
        metadata = await self._get_metadata(source)

        if not text:
            logger.info("No subtitles found for %s, attempting audio transcription", source)
            text = await self._transcribe_audio(source)

        if not text:
            raise RuntimeError(f"Could not extract transcript from {source}")

        return ProcessedContent(
            text=text,
            metadata=metadata,
            source_type="youtube",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return bool(_YOUTUBE_PATTERN.search(source))

    async def _get_metadata(self, url: str) -> dict:
        """Fetch video metadata without downloading."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "--dump-json", "--no-download", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                data = json.loads(stdout)
                return {
                    "title": data.get("title", ""),
                    "channel": data.get("channel", ""),
                    "duration": data.get("duration"),
                    "upload_date": data.get("upload_date"),
                    "url": url,
                }
        except Exception:
            logger.warning("Failed to fetch YouTube metadata for %s", url)
        return {"url": url}

    async def _get_subtitles(self, url: str) -> str | None:
        """Try to extract auto-generated captions via yt-dlp."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--write-auto-subs",
                "--sub-lang", "en",
                "--skip-download",
                "--sub-format", "vtt",
                "--print", "%(requested_subtitles)j",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None

            output = stdout.decode().strip()
            if not output or output in ("null", "NA"):
                return None

            # If we got subtitle info, re-run to actually get the text
            # yt-dlp doesn't have a clean "dump subtitles to stdout" mode,
            # so we parse the info to find the subtitle file path
            subs_data = json.loads(output)
            if isinstance(subs_data, dict) and "en" in subs_data:
                # Found English subtitles — re-fetch with actual download
                return await self._download_subtitles(url)
        except Exception:
            logger.warning("Subtitle extraction failed for %s", url, exc_info=True)
        return None

    async def _download_subtitles(self, url: str) -> str | None:
        """Download subtitles to a temp dir and read them."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--write-auto-subs",
                "--sub-lang", "en",
                "--skip-download",
                "--sub-format", "vtt",
                "-o", f"{tmpdir}/subs",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Find the downloaded subtitle file
            from pathlib import Path
            vtt_files = list(Path(tmpdir).glob("*.vtt"))
            if vtt_files:
                return self._parse_vtt(vtt_files[0].read_text(errors="replace"))
        return None

    async def _transcribe_audio(self, url: str) -> str | None:
        """Download audio and transcribe via STT as fallback."""
        import tempfile
        from pathlib import Path

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                out_path = f"{tmpdir}/audio.mp3"
                proc = await asyncio.create_subprocess_exec(
                    "yt-dlp",
                    "-x", "--audio-format", "mp3",
                    "--audio-quality", "5",
                    "-o", out_path,
                    url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    logger.warning("Audio download failed: %s", stderr.decode()[:200])
                    return None

                audio_file = Path(out_path)
                if not audio_file.exists():
                    # yt-dlp may add extension
                    candidates = list(Path(tmpdir).glob("audio.*"))
                    audio_file = candidates[0] if candidates else None
                if audio_file and audio_file.exists():
                    from genesis.channels.stt import transcribe
                    return await transcribe(audio_file.read_bytes())
        except Exception:
            logger.warning("Audio transcription failed for %s", url, exc_info=True)
        return None

    @staticmethod
    def _parse_vtt(vtt_text: str) -> str:
        """Extract plain text from WebVTT subtitle format."""
        lines: list[str] = []
        for line in vtt_text.split("\n"):
            line = line.strip()
            # Skip timing lines, headers, empty lines
            if not line or "-->" in line or line.startswith("WEBVTT") or line.startswith("Kind:"):
                continue
            # Skip numeric cue identifiers
            if line.isdigit():
                continue
            # Strip HTML-like tags
            cleaned = re.sub(r"<[^>]+>", "", line)
            if cleaned and cleaned not in lines[-1:]:
                lines.append(cleaned)
        return " ".join(lines)
