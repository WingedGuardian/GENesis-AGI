# Anti-Slop Rules

These patterns are dead giveaways of AI-generated content. Content that
contains ANY of these patterns fails the anti-slop filter and must be revised.

---

## Banned Openers

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

## Banned Patterns

- Emoji-heavy formatting (one emoji per section header, walls of emoji bullets)
- Single-sentence line breaks for every thought (the LinkedIn "poem" format)
- Numbered lists where each item is exactly one short sentence
- "Agree? Repost if this resonated"
- Ending with a question designed purely for engagement farming
- "Here are N things I learned about X" (unless genuinely listing lessons)
- The word "journey" used non-literally
- "Game-changer" / "paradigm shift" / "unlock" / "leverage"
- "And here's the kicker:" / "But here's what surprised me:"
- Perfectly balanced three-part parallel structures
- "This. Just this." or "Read that again."
- Hashtag spam (#Leadership #Growth #AI #Innovation #CloudComputing)

## Banned Structural Patterns

- Every paragraph the same length
- Mechanical problem -> solution -> call-to-action structure
- Lists that are suspiciously well-organized (real people are messier)
- Posts that feel like they were written by someone who read "10 LinkedIn
  post templates" — because they probably were

## Additional AI Writing Patterns

These patterns are documented in Wikipedia's Signs of AI Writing and academic
stylometric research. They are less LinkedIn-specific but equally damning.

### Significance inflation
Puffing up the importance of mundane things: "marking a pivotal moment in the
evolution of...", "a testament to...", "underscores the importance of...",
"shaping the future of...", "indelible mark", "enduring legacy"

**Before:** "The Statistical Institute was established in 1989, marking a pivotal
moment in the evolution of regional statistics."
**After:** "The Statistical Institute was established in 1989 to collect and
publish regional statistics independently."

### Copula avoidance
Using "serves as", "functions as", "boasts", "features" instead of simple
"is", "has", "are". AI avoids the simplest verbs.

**Before:** "Gallery 825 serves as LAAA's exhibition space. The gallery features
four spaces and boasts over 3,000 square feet."
**After:** "Gallery 825 is LAAA's exhibition space. It has four rooms totaling
3,000 square feet."

### Negative parallelisms
"It's not just X, it's Y" and "Not only X but also Y" — overused rhetorical
frames that AI defaults to for emphasis.

**Before:** "It's not just about the beat; it's part of the aggression. It's not
merely a song, it's a statement."
**After:** "The heavy beat adds to the aggressive tone."

### Synonym cycling
Referring to the same thing by different names in consecutive sentences to
avoid "repetition." Real writers just use the same word.

**Before:** "The protagonist faces challenges. The main character must overcome
obstacles. The central figure eventually triumphs."
**After:** "The protagonist faces many challenges but eventually triumphs."

### False ranges
"From X to Y" where X and Y aren't on a meaningful scale. AI uses these to
sound comprehensive.

**Before:** "From the singularity of the Big Bang to the grand cosmic web, from
the birth of stars to the dance of dark matter."
**After:** "The book covers the Big Bang, star formation, and dark matter."

### Superficial -ing analyses
Tacking present participle phrases onto sentences to fake analytical depth:
"highlighting...", "underscoring...", "showcasing...", "reflecting..."

**Before:** "The temple's color palette resonates with the region's beauty,
symbolizing Texas bluebonnets, reflecting the community's deep connection."
**After:** "The temple uses blue, green, and gold. The architect said these
reference local bluebonnets."

### Vague attributions
"Experts believe", "Studies show", "Industry reports suggest" — attributing
claims to unnamed sources. Name the source or drop the claim.

### Formulaic challenges
"Despite challenges... continues to thrive" — boilerplate resilience sections
that follow a predictable template.

### Citation artifacts
Leftover AI artifacts: oaicite references, contentReference tags, turn0search
IDs, ChatGPT UTM parameters. These are raw bugs that must be deleted.

### Curly quotes
ChatGPT uses Unicode curly quotes instead of straight quotes. Replace them.

### Title case headings
AI defaults to Capitalizing Every Word In Headings. Use sentence case
(capitalize first word and proper nouns only).

### Inline-header lists
Lists where each item starts with a bolded header and colon:
"- **Topic:** Topic is discussed here." Convert to prose or simpler lists.

## Statistical Signals

Beyond pattern matching, check for these measurable AI tells:

| Signal | Human range | AI range | Why |
|--------|------------|----------|-----|
| Burstiness | 0.5-1.0 | 0.1-0.3 | Humans write in bursts; AI is metronomic |
| Type-token ratio | 0.5-0.7 | 0.3-0.5 | AI reuses the same vocabulary |
| Sentence length variation | High CoV | Low CoV | AI sentences are all roughly the same length |
| Trigram repetition | <0.05 | >0.10 | AI reuses 3-word phrases |

If your output has low burstiness AND low sentence variation AND low vocabulary
diversity, it reads as AI regardless of whether individual patterns match.

## What TO Do Instead

- Start with a specific detail, observation, or story moment
- Use natural paragraph breaks, not one-sentence-per-line
- Include rough edges — a half-formed thought, an admission of uncertainty
- Reference specific technologies, companies, or situations (not abstract)
- Write like you're explaining something to a smart colleague at lunch
- End with a genuine thought, not an engagement prompt
- 2-3 hashtags maximum, only if genuinely relevant
- Vary post length — not every post needs to be a 1,200-character opus

---

## Three-Check Anti-Slop Framework

Apply all three checks to every piece of generated content:

### Check 1: Pattern Matching

Scan the output against every banned opener, banned pattern, banned structural
pattern, and additional AI writing pattern above. Also scan against the full
vocabulary database in `ai-vocabulary.md` — Tier 1 words are automatic failures,
Tier 2 words are suspicious when clustered, Tier 3 words flag only at high
density. Any Tier 1 match is an automatic failure. No exceptions.

### Check 2: Voice Consistency

Compare the output against the selected voice exemplars:

- Does sentence length vary naturally (like the exemplars) or is it uniform?
- Does vocabulary match the user's register or is it elevated/generic?
- Is the perspective personal and specific or abstract and universal?
- Does it have opinions or does it hedge everything?

If the output reads like a different person wrote it, fail.

### Check 3: Specificity

The anti-generic test:

- Does the content contain at least one concrete detail, specific reference,
  personal experience, or data point that could NOT have been generated about
  any topic by any person?
- If you removed the topic keywords, would the content be indistinguishable
  from a template? If yes, fail.

---

## Revision Protocol

**On failure:** Don't just flag — revise. Send specific feedback to the
generation step:

> "Paragraph 2 uses 'In today's landscape' — rephrase with a specific
> observation. Paragraph 4 hedges with 'It's worth noting' — state the
> point directly or cut it."

**Revision limit:** Maximum 2 revision passes. If content still fails after
2 revisions, surface the best version to the user with remaining flags noted.
Do not loop indefinitely.
