# Gemini API Routing — Reference

> **Purpose:** Definitive reference for all Gemini API usage within Genesis.
> Any component that calls Gemini (evaluate skill, inbox monitor, surplus
> research, model routing) must follow these patterns. Incorrect usage causes
> silent hallucination — the model fabricates plausible content instead of
> failing loudly.
>
> Created: 2026-03-09

---

## API Setup

**Package:** `google-genai` (NOT `google-generativeai` — that's the old SDK)
**API Key:** `GOOGLE_API_KEY` in `~/agent-zero/usr/secrets.env`
**Free Tier Limits:** 250 requests/day (per model), 8 hours YouTube video/day

```python
from google import genai
from google.genai import types

client = genai.Client()  # reads GOOGLE_API_KEY from env
```

---

## YouTube Video Analysis

### CRITICAL: Use file_data, NOT Text URLs

The text-only URL approach **HALLUCINATES**. The model does not actually access
the video — it generates plausible-looking content that is completely fabricated.
This was confirmed empirically: three calls to the same URL with text injection
produced three entirely different fake summaries (different topics, speakers,
and channels).

**WRONG — causes hallucination:**
```python
# DO NOT DO THIS — model fabricates content
response = client.models.generate_content(
    model='gemini-2.5-flash',
    contents='Summarize this video: https://youtube.com/watch?v=VIDEO_ID'
)
```

**CORRECT — actually processes the video:**
```python
response = client.models.generate_content(
    model='gemini-3-flash-preview',
    contents=[
        types.Content(
            parts=[
                types.Part(text='Summarize this video with key points.'),
                types.Part(
                    file_data=types.FileData(
                        file_uri='https://www.youtube.com/watch?v=VIDEO_ID',
                        mime_type='video/mp4'
                    )
                )
            ]
        )
    ]
)
```

### Model Selection for YouTube

| Model | YouTube Reliability | Notes |
|-------|-------------------|-------|
| `gemini-3-flash-preview` | **Best** — use this first | Most reliable for video analysis |
| `gemini-3.1-flash-lite-preview` | Fallback | Lighter variant, use if 3 is exhausted |
| `gemini-2.5-flash` | **Avoid** | Questionable reliability, possibly being phased out |
| `gemini-2.0-flash` | **Do not use** | Being phased out, quota limits suggest deprecation |
| `gemini-1.5-flash` | **Removed** — 404 | No longer available |

**Default model for YouTube:** `gemini-3-flash-preview`

**Note:** Google is actively phasing out older Gemini models. The 2.0 quota
exhaustion observed (2026-03-09) likely reflects deprecation throttling, not
actual usage limits. Stick to 3.x models.

---

## Per-Model Quota Rotation

Free tier quotas are **per model, per day** — exhausting one model's quota does
NOT affect others. When a model returns 429 RESOURCE_EXHAUSTED:

```python
YOUTUBE_MODEL_CHAIN = [
    'gemini-3-flash-preview',          # Primary — most reliable
    'gemini-3.1-flash-lite-preview',   # Fallback — lighter 3.x variant
]

async def analyze_youtube(url: str, prompt: str) -> str:
    """Analyze YouTube video with automatic model rotation on quota exhaustion."""
    for model in YOUTUBE_MODEL_CHAIN:
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Content(parts=[
                        types.Part(text=prompt),
                        types.Part(file_data=types.FileData(
                            file_uri=url,
                            mime_type='video/mp4'
                        ))
                    ])
                ]
            )
            return response.text
        except Exception as e:
            if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                continue  # Try next model
            raise
    raise RuntimeError(f"All Gemini models exhausted for YouTube: {url}")
```

---

## Text-Only Requests (Non-YouTube)

For text-only requests (summarization, analysis, classification), standard
usage is fine:

```python
response = client.models.generate_content(
    model='gemini-3-flash-preview',
    contents='Your prompt here'
)
```

The hallucination issue is specific to content the model cannot actually access
via text injection (videos, authenticated pages, etc.).

---

## Integration Points in Genesis

| Component | Gemini Usage | Reference |
|-----------|-------------|-----------|
| Evaluate skill (Phase 6) | YouTube video analysis during source acquisition | This doc |
| Inbox monitor (Phase 6) | YouTube links dropped in inbox → dispatch for evaluation | `docs/plans/2026-03-09-inbox-monitor-plan.md` |
| Model routing | `gemini-free` provider in routing chain | `config/model_routing.yaml` |
| Surplus research | YouTube content evaluation during surplus compute | Future |

---

## Known Issues

1. **Hallucination with text URLs** — Confirmed 2026-03-09. Model generates
   plausible but entirely fabricated content. ALWAYS use file_data for URLs
   pointing to media content.

2. **Quota exhaustion timing** — Free tier resets daily but exact reset time
   is unclear. Plan for worst case: spread usage across models.

3. **Model availability flux** — `gemini-1.5-flash` was removed without notice.
   Check available models periodically: `client.models.list()`.

---

## Model Deprecation — Operational Principle

**This applies to ALL model providers, not just Gemini.**

Models advance continuously. Older versions are phased out — sometimes
silently (quota throttled to zero), sometimes abruptly (404). This is normal
industry behavior and will be the norm for the foreseeable future.

**When any API call fails or behaves unexpectedly, the first diagnostic
question should be: is this model still valid?**

Checklist for any model-related failure:
1. When was this model ID last confirmed working?
2. Has the provider released a newer version? (e.g., 2.5 → 3.0 → 3.1)
3. Is the model still listed? (`client.models.list()` for Gemini, provider
   docs for others)
4. Are quota limits suspiciously low (e.g., "limit: 0")? This often signals
   deprecation throttling, not actual overuse.

**This is especially critical for Genesis's background API calls** — the
routing config (`config/model_routing.yaml`) pins specific model strings
that don't auto-update like Claude Code does. Every model string in that
config is a potential staleness point. When failures cluster around a
specific provider, check the model string before debugging application logic.

Cross-reference: `config/model_routing.yaml` header documents this principle
for all providers.
