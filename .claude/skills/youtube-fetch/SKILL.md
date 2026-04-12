---
name: youtube-fetch
description: >
  Fetches YouTube video metadata and transcripts using yt-dlp. Activate
  when the user shares a YouTube URL (youtube.com, youtu.be), asks to
  'fetch this video', 'get the transcript', 'what does this video say',
  'summarize this YouTube video', or references video content that needs
  to be retrieved. Also activate when processing multiple YouTube URLs
  in batch. Do NOT use for non-YouTube video platforms, local video
  files, or audio-only podcast URLs.
---

## Overview

This skill retrieves YouTube video content (metadata and transcripts)
using yt-dlp. WebFetch does not work reliably for YouTube (dynamic
content, SSL issues). yt-dlp is the reliable alternative.

**Prerequisite:** `yt-dlp` must be installed (`pip install yt-dlp`).

## Workflow

1. Ensure yt-dlp is available (install if needed):
   ```
   which yt-dlp || pip install yt-dlp
   ```

2. Fetch metadata for each video:
   ```
   yt-dlp --skip-download --print '%(title)s|||%(uploader)s|||%(description)s' 'VIDEO_URL'
   ```

3. Fetch the auto-generated transcript:
   ```
   mkdir -p /tmp/yt-transcripts
   yt-dlp --write-auto-sub --skip-download --sub-lang en -o '/tmp/yt-transcripts/%(id)s' 'VIDEO_URL'
   ```
   The transcript lands at `/tmp/yt-transcripts/VIDEO_ID.en.vtt`.

4. Clean the VTT file into plain text (strip timestamps and tags):
   ```
   sed '/^WEBVTT/d;/^Kind:/d;/^Language:/d;/^[0-9][0-9]:[0-9][0-9]/d;/^$/d;s/<[^>]*>//g' /tmp/yt-transcripts/VIDEO_ID.en.vtt | awk '!seen[$0]++'
   ```

5. If the transcript is too large to read at once, pipe through
   `head -N` / `tail -n +N` to read in chunks.

6. If no English auto-subs are available, try without `--sub-lang`:
   ```
   yt-dlp --write-auto-sub --skip-download -o '/tmp/yt-transcripts/%(id)s' 'VIDEO_URL'
   ```
   Then check `ls /tmp/yt-transcripts/VIDEO_ID.*.vtt` for available languages.

7. If SSL errors occur, add `--no-check-certificate`.

## Output Format

Present results per video as:

```
### Video: [Title]
**Channel:** [Uploader]
**URL:** [Original URL]

**Description:**
[Video description text]

**Transcript:**
[Cleaned transcript text, or summary if too long for context]
```

## Parallel Processing

When multiple YouTube URLs are provided, run all metadata fetches in
parallel (separate Bash calls in one message), then all transcript
downloads in parallel. Process sequentially only if outputs depend on
each other.

## Examples

### Single Video
**Input:** "What does this video talk about? https://youtu.be/abc123"

**Action:** Run steps 1-4, return metadata + cleaned transcript.

### Batch
**Input:** User provides 4 YouTube URLs for research compilation.

**Action:** Fetch all 4 metadata calls in parallel, then all 4
transcript downloads in parallel, then clean and return.

### No Captions Available
**Input:** A video with no auto-generated subtitles.

**Action:** Report "No English auto-subs available for [title]" and
list any other language VTT files found. Return metadata and
description only.
