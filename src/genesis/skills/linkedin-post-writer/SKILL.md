---
name: linkedin-post-writer
description: >
  This skill should be used when the user asks to "write a LinkedIn post",
  "draft a post about", "help me post on LinkedIn", "create LinkedIn content",
  or when Genesis proactively generates post ideas during surplus compute.
  Also triggered by content calendar execution or when the user shares a topic
  they want to write about.
consumer: cc_foreground, cc_background_surplus
phase: 8
skill_type: hybrid
---

# LinkedIn Post Writer

## Purpose

Write LinkedIn posts that sound authentically human — in the user's real voice,
with genuine substance, avoiding every AI-generated content pattern. The bar is:
a reader cannot distinguish this from the user having written it themselves.

## Voice Loading

Before writing any post, load the user's voice via voice-master's overlay
resolution:

1. Read `../voice-master/SKILL.md` and follow its **User Calibration
   Overlay** section. Voice-master will load the user's exemplars and
   voice-dimensions from the out-of-repo overlay (or fall back to the
   in-repo template with a warning if no overlay exists).
2. This skill's medium is `social`. Select 3-5 social-medium exemplars
   matching this post's topic and tone from whatever voice-master loads.
3. Read `../voice-master/references/anti-slop.md` — apply the **Universal**
   and **Professional / LinkedIn** sections (forum / long-form sections
   do not apply here).

Use exemplars as stylistic reference — match sentence structure, vocabulary,
directness. Do NOT copy exemplar content.

If no overlay is present, voice-master falls back to the template and emits
a warning. Your output will be generic in that case; surface this clearly.

Every post MUST comply with the anti-slop rules. Failure to follow these rules
produces content that damages the user's professional reputation. Treat
violations as bugs, not style preferences.

## Post Types

### 1. Experience Post
Share something that actually happened — a project, a decision, a mistake, a
win. Grounded in specifics: technologies, team size, timeline, outcome.

**Structure:** Start in the middle of the story (not "Last week I..."). Give
enough context to follow. Share the insight without moralizing. End with the
thought, not a question.

### 2. Insight Post
Take a position on something in the industry. Must be a genuinely held opinion
with reasoning — not a repackaged platitude.

**Structure:** State the observation. Give evidence or reasoning (specific
examples, data, or experience). Acknowledge the counterargument. Land on your
position. Keep it under 800 characters unless the argument genuinely needs more.

### 3. Technical Post
Explain something technical clearly. Aimed at the intersection of the user's
audience: technical enough to be useful, clear enough for adjacent roles.

**Structure:** Start with the problem or situation. Explain the concept or
approach. Include a concrete example. Avoid tutorial tone — this is a peer
sharing knowledge, not a teacher lecturing.

### 4. Commentary Post
React to industry news, a trend, or someone else's post. Add original
perspective — never just summarize what happened.

**Structure:** Brief context (assume audience saw the news). Your take —
specifically, what everyone is missing or getting wrong. Why it matters to
people like the user's audience.

### 5. Question Post
Pose a genuine question the user is thinking about. Not engagement bait — a
real question with no obvious answer.

**Structure:** Set up why this question matters. Ask it clearly. Share your
current thinking if you have one (makes it a discussion, not a poll).

## Writing Process

1. **Clarify the angle** — If the user provides a topic, identify the specific
   angle before writing. "Cloud computing" is not an angle. "Why multi-cloud
   is mostly a lie companies tell themselves" is an angle.

2. **Draft in voice** — Write the post using the loaded voice exemplars.
   Vary sentence length. Include at least one specific detail (a technology
   name, a number, a company, a situation). Avoid perfect structure.

3. **Anti-slop check** — Re-read against the banned patterns list. If any
   pattern appears, rewrite that section. Common failures: the opener defaults
   to a banned pattern, the ending becomes an engagement prompt, the structure
   is too clean.

4. **Length check** — LinkedIn posts have a 3,000-character limit. Most good
   posts are 500-1,500 characters. Go longer only if the content justifies it.
   Short posts that land well outperform long posts that meander.

5. **Hashtag check** — 2-3 maximum, only genuinely relevant ones. Place at the
   end, not inline.

## When Genesis Writes Proactively

During surplus compute or content calendar execution, Genesis generates post
drafts and stages them for user review. Proactive posts should:

- Draw from recent user activity (projects, conversations, inbox items)
- Connect to topics in the user's content calendar if one exists
- Never be published without user approval — staged as surplus output
- Include a brief note explaining why this topic was chosen

## Output Format

```
---
type: <experience | insight | technical | commentary | question>
topic: <brief topic description>
estimated_length: <short(500ch) | medium(1000ch) | long(1500ch+)>
hashtags: <2-3 relevant tags>
---

[POST CONTENT]

---
genesis_notes: |
  <why this angle was chosen, what voice rules were applied,
   any concerns about authenticity>
```

## Topic Areas (User's Expertise)

<!-- Configure your expertise areas here. Examples:
- Cloud engineering and infrastructure
- DevOps / platform engineering
- AI/ML infrastructure
- Your specific domain expertise
-->
## Audience

<!-- Configure your target audience here. Examples:
- Technical professionals in your field
- Engineering managers and directors
- Recruiters and hiring managers
-->
## References

- `../voice-master/SKILL.md` — Voice authority. Follow its **User Calibration Overlay** section to load exemplars and voice-dimensions from the user overlay; exemplars themselves live outside the repo at `~/.claude/skills/voice-master/exemplars/`.
- `../voice-master/references/anti-slop.md` — Universal + Professional/LinkedIn anti-slop rules
- `../linkedin-content-calendar/SKILL.md` — Calendar planning
- `../linkedin-hook-writer/SKILL.md` — Opening line generation
