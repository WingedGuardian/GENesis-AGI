---
name: voice-master
description: >
  Foundational voice authority and AI humanizer — writes content in the user's
  authentic voice with built-in AI detection, and supports stealth / anti-
  attribution writing (forum personas, anonymous posts, "write as not-me").
  Use when asked to write/draft/generate content, invoke /voice, /write-as-me,
  or /humanize, run voice calibration, check "does this sound like me?", "make
  this sound human" / "de-AI this", "write a forum post as [persona]", or run
  AI detection ("does this sound like AI?", "check for AI patterns", "anti-slop
  check"). Do NOT use this skill for code, technical docs, or any output the
  user has not asked to be written in their voice — code styling defers to the
  separate code-voice skill.
consumer: cc_foreground
phase: content
skill_type: generation
---

# Voice Master — entry point (canonical skill in the repo skill library)

This Tier-1 entry exists so the skill is natively discoverable. The **complete**
voice-master skill — workflow, Quick Mode, calibration, stealth mode, anti-slop
rules, and all references — is maintained as the single source of truth at:

> `src/genesis/skills/voice-master/SKILL.md`

**To use this skill:** read `src/genesis/skills/voice-master/SKILL.md` and
follow it exactly — including its mandatory two-part workflow (voice match +
anti-slop audit) and its **User Calibration Overlay** resolution. All reference
files it cites live in `src/genesis/skills/voice-master/references/`.

Your private voice data (exemplars, voice-dimensions) stays in the user overlay
at `~/.claude/skills/voice-master/` and is never read from or written to this
repo. This file holds no voice behavior of its own — there is one source of
truth, in the repo skill library above.
