---
name: video-processing
description: Download, transcribe, analyze, and clip video content — vertical shorts, captions, thumbnails
consumer: cc_background_task
phase: 7
skill_type: uplift
---

# Video Processing

## Purpose

Turn long-form video into processed outputs: transcripts, short clips,
vertical format with captions, thumbnails. Uses FFmpeg, yt-dlp, and
transcription services. All operations via shell commands.

## When to Use

- User requests video clipping, transcription, or processing.
- An evaluation or research task involves video content.
- Content creation requires extracting highlights from longer video.
- A surplus compute task involves video analysis.

## Prerequisites

Required tools (install if missing):
- `ffmpeg` and `ffprobe` — video processing
- `yt-dlp` — video downloading from 1000+ sites
- Transcription: YouTube auto-subs (free), or Groq/OpenAI Whisper API

Check availability:
```bash
which ffmpeg ffprobe yt-dlp 2>/dev/null
```

## Pipeline

### Phase 1: Intake

**From URL:**
```bash
yt-dlp --dump-json "URL" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'Title: {d[\"title\"]}')
print(f'Duration: {d[\"duration\"]}s')
print(f'Resolution: {d.get(\"width\",\"?\")}x{d.get(\"height\",\"?\")}')
"
```

**From local file:**
```bash
ffprobe -v quiet -print_format json -show_format -show_streams "file.mp4"
```

If duration > 2 hours, ask user to specify a segment range.

### Phase 2: Download

```bash
# Best quality up to 1080p with audio
yt-dlp -f "bv[height<=1080]+ba/b[height<=1080]" -o "source.mp4" "URL"

# Also grab auto-subtitles if available (avoids transcription entirely)
yt-dlp --write-auto-subs --sub-lang en --sub-format json3 \
  --skip-download -o "source" "URL"
```

If `source.en.json3` exists, skip to Phase 4 (transcription already done).

### Phase 3: Transcription

Priority order — use the first available:

1. **YouTube auto-subs** (already downloaded in Phase 2) — free, instant
2. **Groq Whisper API** — fast cloud, free tier available
   ```bash
   curl -s https://api.groq.com/openai/v1/audio/transcriptions \
     -H "Authorization: Bearer $API_KEY_GROQ" \
     -F file=@audio.mp3 -F model=whisper-large-v3 \
     -F response_format=verbose_json -F timestamp_granularities[]=word
   ```
3. **OpenAI Whisper API** — reliable, paid
4. **Local Whisper** — if installed, slowest but free
   ```bash
   whisper source.mp4 --model small --output_format json \
     --output_dir . --language en
   ```

Extract audio first if sending to API:
```bash
ffmpeg -i source.mp4 -vn -acodec libmp3lame -q:a 2 audio.mp3
```

### Phase 4: Segment Selection

This is the core value step. Analyze the transcript and select 3-5
segments (30-90 seconds each) based on:

**Selection criteria:**
- **Hook in first 3 seconds** — starts with something attention-grabbing
- **Self-contained** — makes sense without watching the full video
- **Emotional peak** — surprise, humor, insight, controversy
- **High insight density** — says something valuable concisely
- **Clean ending** — ends on a punchline, conclusion, or cliffhanger

**Rules:**
- Start mid-sentence for stronger hooks when appropriate
- End on punchlines or key statements, not trailing off
- Avoid segments that require heavy visual context to understand
- Spread selections across the video (don't cluster)
- Each segment gets: exact timestamps, suggested title (<60 chars),
  one-sentence virality reasoning

### Phase 5: Extract and Process

For each selected segment:

**Extract clip:**
```bash
ffmpeg -ss [start] -to [end] -i source.mp4 \
  -c:v libx264 -c:a aac -preset fast -crf 23 clip_N.mp4
```

**Vertical crop (9:16 for shorts/reels):**
```bash
# Center crop (loses sides)
ffmpeg -i clip_N.mp4 -vf "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920" \
  -c:a copy clip_N_vertical.mp4

# Letterbox (keeps everything, adds black bars)
ffmpeg -i clip_N.mp4 -vf "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2" \
  -c:a copy clip_N_vertical.mp4
```

**Generate SRT captions from transcript:**
- 8-12 words per subtitle line
- 2-3 seconds per subtitle
- Break at natural pauses and sentence boundaries
- Max 42 characters per line (mobile readability)

**Burn captions into video:**
```bash
ffmpeg -i clip_N_vertical.mp4 \
  -vf "subtitles=clip_N.srt:force_style='FontSize=22,FontName=Arial,\
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,\
Shadow=1,MarginV=60,Alignment=2'" \
  -c:a copy clip_N_captioned.mp4
```

**Generate thumbnail:**
```bash
# Frame at 2 seconds in
ffmpeg -ss 2 -i clip_N.mp4 -frames:v 1 -q:v 2 clip_N_thumb.jpg
```

### Phase 6: Report

```markdown
# Video Processing Report

**Source:** [title or filename]
**Duration:** [total duration]
**Clips generated:** N

| # | Title | Duration | File | Size |
|---|-------|----------|------|------|
| 1 | [title] | [duration] | clip_1_captioned.mp4 | [size] |

## Segment Reasoning
1. **[title]** ([start]-[end]): [why this segment was selected]
```

## File Size Limits

If output exceeds platform limits, re-encode:
```bash
# Target ~45MB for Telegram (50MB limit)
ffmpeg -i input.mp4 -c:v libx264 -b:v 1500k -c:a aac -b:a 128k output.mp4
```

| Platform | Video Limit |
|----------|------------|
| Telegram | 50 MB |
| WhatsApp | 16 MB |
| Discord | 25 MB (Nitro: 500 MB) |

## Output Format

```yaml
job_id: <CLIP-YYYY-MM-DD-NNN>
source: <URL or filepath>
source_duration: <seconds>
clips:
  - number: <1-N>
    title: <short title>
    start: <HH:MM:SS>
    end: <HH:MM:SS>
    duration: <seconds>
    file: <output filepath>
    size_mb: <file size>
    format: <horizontal | vertical>
    captioned: <true | false>
    virality_reasoning: <one sentence>
transcription_method: <youtube_auto | groq_whisper | openai_whisper | local_whisper | none>
```

## References

- `docs/reference/gemini-routing.md` — For video content analysis via Gemini API
- FFmpeg documentation: https://ffmpeg.org/documentation.html
- yt-dlp documentation: https://github.com/yt-dlp/yt-dlp
