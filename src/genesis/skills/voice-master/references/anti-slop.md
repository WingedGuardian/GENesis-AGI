# Anti-Slop Rules — Medium-Scoped

These patterns are dead giveaways of AI-generated content. Content that
matches any applicable pattern fails the anti-slop filter and must be revised.

**Medium-scoped:** anti-slop rules are not universal. A sentence fragment that
reads as lazy in a LinkedIn post reads as native on a forum. A bullet list
that organizes a blog post cleanly looks like an LLM-wrote-this tell inside
a forum reply. Applying the wrong set of rules to the wrong medium causes
both false positives (flagging fine content) and false negatives (missing
real tells).

**Load order when voice-master runs anti-slop:**

1. Always apply the **Universal** section below.
2. Apply the section matching the current medium:
   - `social` or `professional` → apply **Professional / LinkedIn**
   - `forum` → apply **Forum / casual**
   - `longform` → apply **Long-form**
3. Apply the common **Statistical Signals**, **Revision Protocol**, and
   **Three-Check Framework** sections at the end regardless of medium.

Scan `ai-vocabulary.md` alongside these rules — Tier 1 vocabulary matches
are automatic failures in every medium.

---

## Universal

These patterns are AI tells regardless of medium. No context excuses them.

### Citation artifacts

Leftover AI artifacts: `oaicite` references, `contentReference` tags,
`turn0search` IDs, ChatGPT UTM parameters. These are raw bugs that must be
deleted before output ever reaches a reader.

### Curly quotes

ChatGPT emits Unicode curly quotes (`"` `"` `'` `'`) by default. Most human
writing in plain-text environments uses straight quotes. Replace them unless
the target environment is a CMS that auto-curls quotes anyway.

### Synonym cycling

Referring to the same thing by different names in consecutive sentences to
avoid "repetition." Real writers just use the same word.

**Before:** "The protagonist faces challenges. The main character must
overcome obstacles. The central figure eventually triumphs."
**After:** "The protagonist faces many challenges but eventually triumphs."

### Vague attributions

"Experts believe", "Studies show", "Industry reports suggest", "Research has
shown" — attributing claims to unnamed sources. Name the source or drop
the claim.

### False ranges

"From X to Y" where X and Y aren't on a meaningful scale. AI uses these to
sound comprehensive.

**Before:** "From the singularity of the Big Bang to the grand cosmic web,
from the birth of stars to the dance of dark matter."
**After:** "The book covers the Big Bang, star formation, and dark matter."

### Copula avoidance

Using "serves as", "functions as", "boasts", "features" instead of simple
"is", "has", "are". AI avoids the simplest verbs.

**Before:** "Gallery 825 serves as LAAA's exhibition space. The gallery
features four spaces and boasts over 3,000 square feet."
**After:** "Gallery 825 is LAAA's exhibition space. It has four rooms
totaling 3,000 square feet."

### Negative parallelisms

"It's not just X, it's Y" and "Not only X but also Y" — overused rhetorical
frames that AI defaults to for emphasis.

**Before:** "It's not just about the beat; it's part of the aggression.
It's not merely a song, it's a statement."
**After:** "The heavy beat adds to the aggressive tone."

### Significance inflation

Puffing up the importance of mundane things: "marking a pivotal moment in
the evolution of...", "a testament to...", "underscores the importance of...",
"shaping the future of...", "indelible mark", "enduring legacy."

**Before:** "The Statistical Institute was established in 1989, marking a
pivotal moment in the evolution of regional statistics."
**After:** "The Statistical Institute was established in 1989 to collect
and publish regional statistics independently."

### Superficial -ing analyses

Tacking present participle phrases onto sentences to fake analytical depth:
"highlighting...", "underscoring...", "showcasing...", "reflecting..."

**Before:** "The temple's color palette resonates with the region's beauty,
symbolizing Texas bluebonnets, reflecting the community's deep connection."
**After:** "The temple uses blue, green, and gold. The architect said these
reference local bluebonnets."

### Formulaic challenges

"Despite challenges... continues to thrive" — boilerplate resilience sections
that follow a predictable template.

### Tier 1 vocabulary matches

Any word or phrase matching `ai-vocabulary.md` Tier 1 is an automatic failure
in any medium. Tier 1 words include "delve", "tapestry", "testament",
"underscore", "pivotal", "landscape" (metaphorical), "intricate", "showcasing",
"fostering", "garner", "interplay", "enduring", "vibrant", "crucial",
"enhance".

---

## Professional / LinkedIn

Applies when medium is `social` or `professional`. These rules are tuned for
LinkedIn, work-email, Slack DMs in professional contexts, and short-form
public posts where the writer is identified and the stakes are reputational.

### Banned Openers

- "Let's dive in" / "Let's break it down"
- "Here's the thing about [X]"
- "In today's fast-paced [anything]"
- "Stop doing X. Start doing Y."
- "I used to think X. Then I learned Y."
- "[Hot take emoji] Unpopular opinion:"
- "Most people don't realize..."
- "The secret to [X] that nobody talks about"
- "I just had a revelation about..."
- Any opener that could appear on a "LinkedIn cringe" compilation

### Banned Patterns

- Emoji-heavy formatting (one emoji per section header, walls of emoji
  bullets)
- Single-sentence line breaks for every thought (the LinkedIn "poem" format)
- Numbered lists where each item is exactly one short sentence
- "Agree? Repost if this resonated"
- Ending with a question designed purely for engagement farming
- "Here are N things I learned about X" (unless genuinely listing lessons)
- The word "journey" used non-literally
- "Game-changer" / "paradigm shift" / "unlock" / "leverage" (as a verb)
- "And here's the kicker:" / "But here's what surprised me:"
- Perfectly balanced three-part parallel structures
- "This. Just this." or "Read that again."
- Hashtag spam (#Leadership #Growth #AI #Innovation #CloudComputing)

### Banned Structural Patterns

- Every paragraph the same length
- Mechanical problem → solution → call-to-action structure
- Lists that are suspiciously well-organized (real people are messier)
- Posts that feel like they were written by someone who read "10 LinkedIn
  post templates" — because they probably were

### Title case headings

AI defaults to Capitalizing Every Word In Headings. Use sentence case
(capitalize first word and proper nouns only).

### Inline-header lists

Lists where each item starts with a bolded header and colon:
"- **Topic:** Topic is discussed here." Convert to prose or simpler lists.

### What TO do instead (professional)

- Start with a specific detail, observation, or story moment.
- Use natural paragraph breaks, not one-sentence-per-line.
- Include rough edges — a half-formed thought, an admission of uncertainty.
- Reference specific technologies, companies, or situations (not abstract).
- Write like you're explaining something to a smart colleague at lunch.
- End with a genuine thought, not an engagement prompt.
- 2-3 hashtags maximum, only if genuinely relevant.
- Vary post length — not every post needs to be a 1,200-character opus.

---

## Forum / casual

Applies when medium is `forum` — long-running threaded discussions on
Discourse, Reddit, phpBB-style boards, HN comments, and similar.

**Important:** many Professional anti-slop rules are WRONG in forum context.
Fragments, lowercase starts, short replies, missing apostrophes in
contractions, and comma splices are all NATIVE to forum register — treating
them as AI tells would flag native content.

### Banned Structural Patterns (forum-specific)

- **Bulleted lists in replies.** Forum replies are prose. Bullets in a reply
  are an assistant pattern. Use them only in top-level posts that warrant
  structure (rare).
- **Numbered lists in replies.** Same reasoning.
- **Section headers in a reply.** `## Section Title` in a reply is a dead
  giveaway. Top-level posts occasionally warrant headers; replies basically
  never.
- **Formal closings.** "In conclusion", "To summarize", "The bottom line is",
  "My takeaway is" — assistant patterns. Forum posts end; they don't
  summarize themselves.
- **Balanced takes.** "On the other hand", "there's also the argument that",
  "to play devil's advocate" — on an opinionated forum, balance reads as
  bot or plant. Real posters pick a lane.
- **Over-hedging.** "I could be wrong but", "just my two cents", "might be
  off here", "take this with a grain of salt" — helpful-assistant hedge
  patterns. Real forum posters state things and move on.
- **Over-apologizing.** "Sorry if this has been covered", "apologies for
  the long post" — assistant politeness. Not forum native.
- **Opening with "Great point!"** or any variant of affirmation before
  responding. Forum posters don't validate before disagreeing — they just
  disagree.

### Banned Vocabulary (forum-specific, cluster with Tier 2)

Words that are fine in professional writing but scream AI in a forum reply:

- "comprehensive"
- "furthermore"
- "moreover"
- "additionally"
- "notably"
- "indeed" (as a transition)
- "it's worth noting that"
- "to clarify"
- "arguably"
- "nevertheless"
- "consequently"
- "thus"

Not automatic failures individually — occasional use can slip — but **any
two of these in the same forum reply is a failure**. Cluster detection
catches what single-word scans miss.

### Banned Openers (forum-specific)

- "As someone who..." (assistant pattern, credential-flashing)
- "Great question!" / "Interesting point!"
- "Speaking as a long-time [X]..."
- "I'd like to point out..." / "I'd like to add..."
- "A few thoughts on this:"
- Anything that previews the structure of the reply ("I have three points")

### What TO do instead (forum)

- Start in the middle of the thought. No preamble.
- Fragments are fine. "Yeah not buying it." "Been there."
- Comma splices are fine. "Read the whole thread, not sure anyone has it
  right yet."
- Lowercase starts are OK in moderation.
- Missing apostrophes in contractions ("dont", "cant", "wont") read native
  in casual registers.
- Quote-reply to specific earlier posts when responding to them.
- Position-reference earlier posts ("the guy above", "first reply").
- Use in-group acronyms without defining them if the community uses them.
- Let the post end where it ends. No closing summary.

---

## Long-form

Applies when medium is `longform` — blog posts, essays, articles, substack
pieces, anything over ~800 words where structure is legitimate.

### Banned Patterns

- **Symmetry.** All sections the same length. All paragraphs the same
  length. All sentences the same length. Real long-form writing has
  rhythm variation — some sections are twice as long as others because
  the idea needed more room.
- **Transition words opening every paragraph.** "Furthermore", "Moreover",
  "Additionally", "However", "Nevertheless" used as automatic paragraph
  starters is an AI reflex. Good long-form transitions are embedded in
  the sentence content, not glued to the front.
- **Summary-of-the-summary endings.** "In this piece, we explored X, Y,
  and Z. As we've seen..." Restating the piece at the end in a neat
  recap is an AI reflex. Real essays end on an image, an observation,
  a punchline, or an unresolved thought — not on a mechanical recap.
- **Three-part everything.** Three examples, three reasons, three stages,
  three acts. Three is the number AI picks because it feels balanced.
  Real essays use two or four or six examples as often as three.
- **"Layered" metaphors.** "X is a tapestry of Y, interwoven with threads
  of Z." The metaphor isn't serving an argument, it's serving the prose's
  ambition. Cut it.

### Banned Openers (longform)

- "In an era where..."
- "In today's rapidly evolving..."
- "The world of X has undergone dramatic transformation..."
- "Imagine for a moment that..." (unless followed by a specific concrete
  image, which AI rarely produces)
- "Few things are as [adjective] as..."

### What TO do instead (longform)

- Start with a concrete scene, specific detail, or a pointed claim.
- Vary section length based on what each section actually needs.
- Let paragraphs be the length the thought requires.
- End where the piece actually ends, not where symmetry suggests.
- Include at least one genuinely specific anecdote, quotation, or detail
  that cannot be inferred from the thesis.

---

## Statistical Signals (all media)

Beyond pattern matching, check for these measurable AI tells:

| Signal | Human range | AI range | Why |
|--------|------------|----------|-----|
| Burstiness | 0.5-1.0 | 0.1-0.3 | Humans write in bursts; AI is metronomic |
| Type-token ratio | 0.5-0.7 | 0.3-0.5 | AI reuses the same vocabulary |
| Sentence length variation | High CoV | Low CoV | AI sentences are all roughly the same length |
| Trigram repetition | <0.05 | >0.10 | AI reuses 3-word phrases |

If your output has low burstiness AND low sentence variation AND low vocabulary
diversity, it reads as AI regardless of whether individual patterns match.

These signals apply everywhere, but the thresholds differ:
- **Forum replies** are noisier and shorter; statistical signals are less
  reliable on posts under 50 words. Fall back to pattern matching there.
- **Long-form** should score comfortably in the human range across all
  four signals.
- **Professional/social** is the middle ground — statistical signals are
  informative but not determinative.

---

## Three-Check Anti-Slop Framework

Apply all three checks to every piece of generated content. Failure in any
check triggers revision.

### Check 1: Pattern Matching

Scan the output against the **Universal** section above and against the
section matching the current medium. Any match in an applicable section is
a failure. Also scan against `ai-vocabulary.md` — Tier 1 matches are
automatic failures, Tier 2 are suspicious when clustered, Tier 3 are
density-sensitive.

### Check 2: Voice Consistency

Compare the output against the selected voice exemplars (loaded from the
user overlay — see SKILL.md):

- Does sentence length vary naturally (like the exemplars) or is it uniform?
- Does vocabulary match the user's register, or is it elevated/generic?
- Is the perspective personal and specific, or abstract and universal?
- Does it have opinions, or does it hedge everything?

If the output reads like a different person wrote it, fail.

**In stealth mode,** Check 2 is inverted: the output must NOT match the
user's voice. It should match the persona's exemplars (or generic target
for anonymous mode). See `stealth-writing.md` for the neutralization
protocol.

### Check 3: Specificity

The anti-generic test:

- Does the content contain at least one concrete detail, specific reference,
  personal experience, or data point that could NOT have been generated
  about any topic by any person?
- If you removed the topic keywords, would the content be indistinguishable
  from a template? If yes, fail.

---

## Revision Protocol

**On failure:** don't just flag — revise. Send specific feedback to the
generation step:

> "Paragraph 2 uses 'In today's landscape' — rephrase with a specific
> observation. Paragraph 4 hedges with 'It's worth noting' — state the
> point directly or cut it."

**Revision limit:** maximum 2 revision passes. If content still fails after
2 revisions, surface the best version to the user with remaining flags noted.
Do not loop indefinitely.

**In stealth mode**, after anti-slop passes, also run the adversarial critic
from `stealth-writing.md`. Anti-slop catches generic AI tells; the critic
catches "sounds like an LLM doing a persona" tells that anti-slop misses.
