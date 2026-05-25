---
name: content-publish
description: >
  End-to-end content creation and publishing. Takes a topic (or generates
  one), drafts in the user's voice, gets approval via Telegram, and
  publishes to Medium via browser automation. Invoke with "publish a post
  about X", "write and publish to Medium", "content-publish", or when an
  ego-dispatched session needs to create and distribute content.
keywords: [medium, publish, article, blog, post, content, write, draft, distribution, marketing]
---

## Voice (MANDATORY — NON-NEGOTIABLE)

EVERY piece of content MUST go through the voice-master skill before
publishing. This is a hard requirement, not a suggestion. The voice-master
skill is TWO parts — both must be applied:

1. **Exemplar matching** (tone, vocabulary, rhythm from real user writing)
2. **Anti-AI-slop pass** (em dashes, hedge words, uniform structure, banned phrases)

If content was not drafted with voice-master, apply it NOW before
publishing. Do NOT publish content that hasn't passed both checks.
Invoke the voice-master skill explicitly — do not attempt to apply
voice rules from memory or inline.

---

## Overview

Single skill that covers the full publish pipeline: topic → research →
draft → approval → publish. Uses voice-master for drafting, browser MCP
tools for publishing, and Telegram for approval.

Medium is the only platform currently wired. The pattern extends to other
platforms by swapping the publish step.

## Prerequisites

- Medium login active in Camoufox profile (`~/.genesis/camoufox-profile/`)
- Medium username configured in `~/.genesis/config/distribution.yaml`
- Voice-master skill available (exemplars at `~/.claude/skills/voice-master/`)
- Narrative reference card at `~/.genesis/config/genesis-narrative.md`
- **Stealth-browser skill** (`src/genesis/skills/stealth-browser/SKILL.md`) —
  MUST load before any browser interaction. Covers anti-detection timing,
  honeypot avoidance, and the VNC trusted input technique for Cloudflare
  Turnstile.

## Browser Rules

- **Always use Camoufox (default).** Never pass `stealth=False` to
  `browser_navigate`. Chromium has SSL cert issues and no anti-detection.
- **Never give up at Turnstile.** The browser tool handles it automatically
  (3-phase: auto-resolve → VNC click → human alert). If you see Turnstile
  in the response, the tool is ALREADY handling it. Wait and retry.
- **Camoufox works fine.** Any memory claiming "glxtest issues" or
  "Camoufox broken" is stale/hallucinated (debunked May 2026). Ignore it.
- **Expect to succeed.** The pipeline is wired and working. Your job is
  to persist through transient failures, not to declare failure after one
  attempt.

## Workflow

### Step 1 — Determine topic and platform

If the caller provides a topic, use it. If not, check these sources:
1. Ego proposal (if dispatched via `publish_content` action)
2. Recent recon findings (`recon_findings` MCP)
3. Knowledge base for trending topics in the user's domain

Platform is Medium unless specified otherwise.

**Output:** One sentence describing the topic and angle.

### Step 2 — Load context

Read these files (do NOT skip any):
1. `~/.genesis/config/genesis-narrative.md` — framing, key phrases
2. Voice-master exemplar index (`~/.claude/skills/voice-master/exemplars/index.md`)
3. Select 3-5 matching exemplars based on medium (long-form), tone, and
   formality 4 (public content)

### Step 3 — Research (if needed)

For topics requiring facts or current data:
- Use `WebSearch` for recent developments
- Use `knowledge_recall` for existing Genesis knowledge
- Use `recon_findings` for competitive intelligence

Skip for opinion/thought-leadership pieces where the user's perspective
is the content.

### Step 4 — Draft

Write the content following voice-master rules:
- Evidence-first, not windup
- Mix short punchy statements with longer reasoning
- No AI-tell words (delve, leverage, robust, seamless, etc.)
- Em-dashes: `--` no spaces, prefer comma/period/colon first, max 1-2
- Formality 4 for public Medium posts
- Read each paragraph aloud mentally — if it sounds like AI, rewrite

**Structure for Medium:**
- Title: short, specific, no clickbait
- Body: 3-8 paragraphs. No headers for short posts. Headers for 1000+ words.
- No hashtags, no "follow me", no CTAs unless genuinely relevant

**Output:** Complete draft with title on the first line, body below.

### Step 5 — Quality check

Before submitting for approval, verify:
- [ ] No banned AI-tell words or phrases
- [ ] No three-part lists with identical grammatical structure
- [ ] No sycophantic openers or hedging
- [ ] Em-dash count ≤ 2 for the whole post
- [ ] Reads like a person thinking, not a polished AI response
- [ ] Title is specific and interesting, not generic
- [ ] Content matches the narrative framing (genesis-narrative.md)

If any check fails, rewrite the failing section.

### Step 6 — Submit for approval

Send the draft to the user via Telegram (outreach_send MCP) with:
- The full draft text
- Platform: Medium
- Ask: "Approve to publish? Reply 'yes', 'no', or send edits."

**Wait for user response.** Do NOT publish without explicit approval.

If the user sends edits, apply them and re-submit.
If the user says no, stop. Store the draft in memory for potential future use.

### Step 7 — Publish to Medium

Follow the stored procedure (`medium_browser_publish`). The key steps:

1. Navigate to `https://medium.com/new-story` (Camoufox, always)
2. Verify editor loaded (snapshot should show "Title" heading)
3. Click the h3 title element, type title via `browser_fill`
4. Press Enter to move to body
5. Build full article HTML in JS on `window.__articleHTML`
6. Use clipboard API to write content:
   ```js
   navigator.clipboard.write([new ClipboardItem({
     'text/html': new Blob([window.__articleHTML], {type: 'text/html'}),
     'text/plain': new Blob([plainText], {type: 'text/plain'})
   })])
   ```
7. Focus body paragraph, paste via `browser_press_key("Control+v")`
8. Verify content with snapshot before proceeding
9. Click Publish button, add topics if desired, confirm publish
10. Verify via URL change to `?postPublishedType=initial`

**CRITICAL: What does NOT work (do not attempt):**
- `document.execCommand('insertText')` — silently fails in Medium editor
- `document.execCommand('insertHTML')` — stripped by Medium's sanitizer
- Synthetic `ClipboardEvent` dispatch — Medium ignores synthetic events
- Only real clipboard paste via `navigator.clipboard.write()` + `Ctrl+V` works

**If any step fails:** Take a screenshot, report the error, do NOT retry
blindly. The editor selectors may have changed.

### Step 8 — Report result

Send the published URL to the user via Telegram.

Store the outcome in memory:
- `memory_store` with tags: `["content", "published", "medium"]`
- Content: topic, URL, date, any engagement data later

## Error Handling

| Error | Action |
|-------|--------|
| Not logged in to Medium | Return error. User must VNC login (one-time). |
| Cloudflare Turnstile | Handled automatically by `browser_navigate`. The solver cascade (auto-resolve → widget click → VNC fallback) runs without intervention. Do NOT switch browsers, try TinyFish, or give up — Camoufox resolves it. |
| Editor selectors changed | Screenshot + report. Do NOT guess new selectors. |
| Draft quality check fails | Rewrite, max 2 attempts, then submit for manual review. |
| Telegram approval timeout | Store draft as pending. Do not publish. |

## What This Skill Does NOT Do

- Auto-publish without approval (every post needs explicit "yes")
- Schedule posts (use ego cadence for timing decisions)
- Cross-post to other platforms (separate skill per platform)
- Generate images or media (text-only for now)
- Analytics tracking (future: content-analytics skill)

## Examples

### Direct invocation
**User:** "Publish a Medium post about why most AI agent benchmarks are useless"

**Action:** Skip research (opinion piece) → load voice-master + narrative →
draft at formality 4 → quality check → Telegram approval → publish → report URL.

### Ego-dispatched
**Ego proposal:** `{"action": "publish_content", "topic": "earned autonomy",
"platform": "medium", "angle": "why trust frameworks matter more than
capability frameworks"}`

**Action:** Load narrative → light research (knowledge_recall for autonomy
subsystem details) → draft → quality check → Telegram approval → publish →
store outcome for ego learning.
