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

3. Fetch the auto-generated transcript. Request **both** English tracks and prefer
   `en-orig`:
   ```bash
   VID='VIDEO_ID'   # <- substitute the real 11-char video id
   mkdir -p ~/tmp/yt-transcripts
   rm -f ~/tmp/yt-transcripts/"$VID".*.vtt        # drop any earlier run's files
   yt-dlp --write-auto-sub --skip-download --sub-langs "en-orig,en" -o "$HOME/tmp/yt-transcripts/%(id)s" 'VIDEO_URL'
   ls ~/tmp/yt-transcripts/ | grep -F -- "$VID"
   ```
   The `rm -f` is load-bearing, not tidiness: that directory persists across runs and
   across videos, so a stale `$VID.en-orig.vtt` from an earlier fetch would satisfy
   step 4's preference check even when THIS fetch failed or returned only `en` — you
   would clean an old transcript and never see an error. `grep -F --` because a video
   id may begin with `-`, which `grep` would otherwise read as options.
   YouTube may serve two English auto-caption tracks — `en-orig` ("English
   (Original)") and `en` ("English"). Usually they are the same resource. Observed
   2026-09 on one video: the two were served with different cue segmentation, and the
   closing lines differed in wording ("subscribe" vs "subscription") — that wording
   difference is direct evidence of a different transcription. The same video served
   byte-identical tracks hours later. Cause unknown; requesting both and preferring
   `en-orig` costs one extra small download and is free insurance either way.

   `en-orig` exists only where the video's original audio is English. On a
   foreign-language video there is no `en-orig`, and `en` is a machine TRANSLATION
   with nothing in the file marking it as one. A requested track that does not exist
   is skipped silently — check the `ls`, and see step 6 if nothing was written.

4. Clean the VTT into plain text. Do the file-preference and the clean in **one**
   Bash call — shell variables do not survive between calls:
   ```bash
   VID='VIDEO_ID'   # <- substitute the real 11-char video id
   VTT="$HOME/tmp/yt-transcripts/$VID.en-orig.vtt"
   [ -f "$VTT" ] || VTT="$HOME/tmp/yt-transcripts/$VID.en.vtt"
   [ -f "$VTT" ] || { echo "no English VTT for $VID — see step 6" >&2; exit 1; }
   sed '/^WEBVTT/d;/^Kind:/d;/^Language:/d;/^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9]* -->/d;s/<[^>]*>//g;/^[[:space:]]*$/d' "$VTT" | awk '!seen[$0]++'
   ```
   The ORDER is the whole point, and each stage bit a previous version:

   - **Control-line deletes run FIRST, on raw text.** A real `WEBVTT`/`Kind:`/
     `Language:`/cue-header line carries no tags, so on raw text those predicates are
     unambiguous. Strip tags first and a genuine caption can be mistaken for a control
     line and deleted — measured: `<c>Language: Python is the topic</c>` and
     `<00:00:01.500><c>10:30</c> is the deadline` were both destroyed by a
     strip-first ordering and both survive this one.
   - The cue-header pattern is anchored to the full `HH:MM:SS.mmm -->` shape rather
     than a bare `^[0-9][0-9]:[0-9][0-9]`, so a caption that merely starts with a
     clock time is not eaten.
   - **Tag-strip next, blank-delete LAST.** `/^[[:space:]]*$/d`, not `/^$/d` — a VTT
     carries BOTH truly-empty lines (the cue separator) and single-space lines (the
     first line of a cue payload); the plain form leaves the latter. Running it last
     also collapses any line that became empty once its markup was stripped.
   - The `[ -f ]` guard matters: without it a missing VTT makes `sed` write one
     stderr line while the pipeline still exits 0 (the status is `awk`'s), so the
     failure looks like an empty transcript rather than an error.

   The `awk` de-dupes the rolling caption lines. It is whole-FILE, not adjacent, so a
   line genuinely repeated later in the talk is dropped too — and it is line-based, so
   a phrase split across a cue boundary will not survive a `grep` of the result.
   Flatten with `tr '\n' ' ' | tr -s ' '` before searching for phrases, and treat a
   word-count delta between two tracks as suggestive only: different cue segmentation
   changes what de-dupes, independently of content.

5. If the transcript is too large to read at once, pipe through
   `head -N` / `tail -n +N` to read in chunks.

6. If no English track was written, DO NOT just drop `--sub-langs` — omitting it
   narrows the request to a single English-first track rather than broadening it
   (measured: it writes one `en` file even when a dozen languages exist). List what
   the video has, then request one language:
   ```bash
   # the listing runs to thousands of lines on a translated video — the real tracks
   # are at the top, so cap it rather than dumping it into the session
   yt-dlp --list-subs --skip-download 'VIDEO_URL' 2>&1 | head -45
   yt-dlp --write-auto-sub --skip-download --sub-langs "<lang>" -o "$HOME/tmp/yt-transcripts/%(id)s" 'VIDEO_URL'
   ```
   Pick from the **"Available automatic captions"** section — `--write-auto-sub` only
   fetches those. For a track listed under "Available subtitles" (human/community),
   swap in `--write-sub`. Then clean it with step 4, substituting your `<lang>` for
   `en-orig`/`en` in the two `VTT=` lines.

   Avoid `--sub-langs all`: it requests every translation pair the video exposes,
   which is hundreds to thousands of tracks.

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
each other. Note `--sub-langs "en-orig,en"` writes up to TWO files per
video, and the `en-orig` preference in step 4 must be resolved PER VIDEO,
inside the same Bash call that cleans that video — one shared `VTT`
variable across a batch will clean the wrong file.

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

**Action:** Follow step 6 — list the available tracks, fetch one in a
language you can work with, and clean it with step 4. Only if no usable
track exists, report that and return metadata and description only.
