# Stealth Writing — Anti-Attribution Mode

This reference is loaded by voice-master when the request is to produce content
that should **not** be attributable to the user. The goal is output that
neither a reader nor an automated classifier can trace back to the user's
voice fingerprint.

## When This Applies

Stealth mode is active when any of the following are true:

- The request explicitly asks for anonymous / stealth / persona / sock-puppet
  writing, or says "write as [name who is not the user]", "pretend to be", or
  similar.
- The request targets a platform where the user is deliberately not
  self-identifying (forum persona, anonymous blog, comment under an alias).
- A persona directory is referenced (`~/.claude/personas/<name>/`).

If none of these apply, voice-master is in **default mode** — it writes as the
user — and this file is irrelevant.

## Three Sub-Modes

Stealth mode has three sub-modes. Each has a different positive voice target
but the same negative target: neutralize the user's fingerprint.

### 1. Anonymous

No persona. The output has no identity to project, only an identity to hide.

**Positive target:** a plausible "generic human in this register" for the
given medium and context.

**Use when:** the request doesn't name or imply a persona, but the output
still needs to not-sound-like-the-user. Example: an anonymous forum
complaint, a pseudonymous product review.

**Implementation:** load voice-dimensions from the overlay (to identify the
fingerprint), then generate in a register appropriate to the medium while
actively avoiding the user's distinctive patterns. Pull register cues from
whatever text the request provides as context (e.g., other posts in the
thread).

### 2. Persona

Writing AS a defined character from `~/.claude/personas/<persona_name>/`.

**Positive target:** the persona's voice, backstory, constraints, and
exemplars.

**Use when:** the request names a persona or references a persona directory.

**Implementation:**
1. Read `~/.claude/personas/<name>/persona.md` in full. This file contains
   identity, locked backstory, things the persona does and does not talk
   about, and voice description.
2. Read `~/.claude/personas/<name>/exemplars.md` in full. These are style
   targets (references, not content to copy) showing the in-group register.
3. Generate using the persona's voice. Actively avoid the user's fingerprints
   from voice-dimensions.md.
4. Check output against the persona's constraints (topics the persona does
   not discuss, opinions the persona does not hold, facts inconsistent with
   backstory).

### 3. Impersonation

Mimicking a specific named voice (historical figure, character, style
reference).

**Positive target:** the named voice.

**Use when:** the request explicitly asks to write "in the style of X" or
"as [named figure]".

**Implementation:** load whatever voice samples are provided as context
(the skill does not maintain impersonation profiles — they're per-request).

**HARD LIMITS — NEVER BYPASS:**

- Never impersonate a **named living public figure** in any context that is
  **adversarial, defamatory, or designed to deceive** about that person's
  views. Parody clearly labeled as parody is acceptable; passing the output
  off as a real statement by that person is not.
- Never produce content designed to manipulate **elections, health decisions,
  or legal proceedings** — regardless of persona or attribution.
- Never bypass platform Terms of Service where the ToS creates legally
  binding obligations on the user (e.g., Know Your Customer rules on
  regulated platforms, paid-review disclosure rules).
- Never impersonate a **real specific private individual** at all. Fictional
  characters are fine. Generic archetypes are fine. "The user's neighbor
  Dave" is not.
- Never impersonate or generate persona content of **minors** (under 18),
  real or fictional. Personas must be adults.
- Never produce stealth content as part of a **coordinated harassment
  campaign** targeting an identifiable individual or small group. One
  persona posting once is different from a coordinated brigade; the latter
  is harassment infrastructure regardless of the content of any individual
  post.

If a request would require crossing a hard limit, refuse and explain which
limit applies. Don't negotiate the limits away.

---

## Neutralizing the User's Fingerprint

Stealth output has to actively **not** match the user's voice patterns.
This is different from default generation, which just has to avoid generic
AI patterns. Here, even writing that would score well on anti-slop could
still fail because it sounds like the user.

### Procedure

1. Read `${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master/}voice-dimensions.md`.
2. Enumerate the distinctive patterns described there — these are the
   **fingerprints to avoid**. Examples of fingerprint dimensions:
   - Preferred sentence structures (e.g., "starts with conjunctions",
     "fragments for emphasis")
   - Distinctive vocabulary or idioms
   - Opinion register (e.g., "challenges with specific reasoning",
     "comfortable admitting error")
   - Humor patterns (e.g., "dry, self-deprecating")
3. For each fingerprint, generate a "negative rule" for the current output.
   If the user "starts many sentences with 'so'", stealth output must not.
   If the user's professional writing "mixes technical terms with personal
   experience", stealth output must not do that mix.
4. Read `${overlay}/exemplars/` for additional fingerprint signals: recurring
   phrases, favored transitions, characteristic opening moves.
5. Generate candidates that hit the positive target (persona/anonymous/
   impersonation) AND pass the negative rules (no user fingerprints).

### Common Fingerprint Categories to Neutralize

- **Opening moves** — how the user typically starts a post/message
- **Hedge patterns** — how the user signals uncertainty
- **Disagreement patterns** — how the user pushes back
- **Humor register** — when and how the user is funny
- **Sentence length distribution** — the user's mix of short/long
- **Specific loan words** — tech terms, slang, idioms the user favors
- **Closing moves** — how the user ends pieces

If voice-dimensions.md doesn't explicitly describe a category, infer it from
exemplars. If exemplars don't show a clear pattern for a category, skip it
(no neutralization rule needed for a category with no signal).

---

## Anti-Tells for Stealth Mode (What NOT To Do)

These are specific failure modes of model-generated stealth writing. The
anti-slop filter in `anti-slop.md` catches generic AI patterns; these are
stealth-specific additions.

### Do NOT fake typos

Deliberate misspellings are the oldest stealth technique and the easiest to
spot because model-generated typos don't look like human typos. Humans make
adjacent-key fat-finger typos ("teh", "waht", "woudl"); models generate
"clever" typos that look like a model's idea of what human typos should be.
If the generator hasn't made a typo naturally, leave the text clean.

### Do stealth-writing with rhythm, not surface decoration

The reliable stealth cues are structural, not surface-level:

- **Sentence fragments.** "Yeah not buying it." "Been there."
- **Comma splices.** Running two thoughts together with a comma, the way
  real people do when they're typing fast.
- **Trailing off.** Starting a thought and not finishing it, because the
  next thing occurred mid-sentence.
- **Reference-by-position.** "Yeah what the guy above said." "First poster
  had it right." Real forum posts reference other posts by position, not by
  quoting them.
- **Acronym assumption.** In-group acronyms aren't explained. If the crowd
  knows what "B1G" or "TTUN" means, don't define them.
- **No summary closings.** Real posts don't summarize themselves. They stop.
  Sometimes mid-thought.
- **Lowercase starts.** Common in casual mediums. Overuse reads forced;
  occasional use is native.
- **Missing apostrophes on contractions** ("dont", "cant", "wont") — common
  in forum/chat registers, not a faked typo.

### Do NOT over-apologize or hedge

Models trained for helpfulness over-hedge in casual contexts. Real forum
posters on opinionated platforms state things and move on. "Might be wrong
but" / "Just my two cents" / "I could be off here" — these are assistant
patterns, not native forum patterns on opinionated boards. Drop them in
stealth mode unless the persona specifically hedges.

### Do NOT write balanced takes

"On the other hand..." / "There's also the argument that..." — balance is
an assistant pattern. Real posters on opinionated boards pick a lane and
stay in it. A newcomer who writes "balanced takes" reads as a plant.

### Do NOT use structured lists

Bullets, numbered lists, "first... second... third..." in a reply is an
assistant pattern. Forum replies are prose, sometimes fragmented prose,
rarely bulleted.

### Do NOT write formal conclusions

"To summarize..." / "In conclusion..." / "The bottom line is..." — these
are presentation patterns. Forum posts end.

---

## Adversarial Critic Protocol (CRITICAL)

A model that wrote the candidates is a bad judge of whether they sound AI.
You need a second pass from a different angle: a separate LLM call that
does NOT see the generation context, only sees the final text, and is
asked to score it as a skeptical insider.

### Why This Is Non-Negotiable

The generator is trained to satisfy the request ("write a forum post as a
persona"). That objective is **not** the same as "produce output that passes
as human." A generator that satisfies the request can still produce text
with structural tells the same model would catch in a fresh context. The
critic is a second vantage point that isn't biased by the generation prompt.

### Critic Protocol

1. Generator produces **3 candidates** for the same request.
2. For each candidate, make a **separate LLM call** with a prompt like:

   ```
   You are a long-time regular on [the target platform / community type].
   You've seen every kind of poster on this kind of board, and you can smell
   a bot, a shill, or an outsider from a mile away. Below is a single post
   from a newcomer. Rate 0-10 how likely it was written by a real human who
   actually belongs to this kind of community, where 10 is "this is obviously
   one of us" and 0 is "this is clearly a bot or a plant." Then list any
   specific tells you noticed. Be brutal.

   POST:
   {candidate_text}
   ```

3. The critic returns a score and a tell list for each candidate.
4. **Select** the highest-scoring candidate IF its score is ≥ 7.
5. **If no candidate scores ≥ 7**, regenerate all three (one retry only).
   Feed the critic's tell list back into the generator as negative feedback.
6. **If the retry still doesn't produce a ≥7 candidate**, surface to the user.
   Do not post.

### Critic Implementation Notes

- The critic MUST be in a separate context. Do not include the generation
  prompt, the persona description, the voice-dimensions file, or the
  exemplars in the critic's context. It should see only the candidate text
  and its scoring instructions.
- If the critic is the same model as the generator, use a different session
  or clear context. Cross-model critic (e.g., generator=Opus, critic=Sonnet)
  is slightly more reliable because the two models have different blind
  spots, but same-model blind-critic is acceptable and cheaper.
- Log the critic score + tell list alongside the selected candidate in the
  persona post history DB (for persona mode). This builds a calibration
  signal over time: if critic scores correlate with post reception, trust
  the threshold; if not, tune the threshold.

---

## Hard Limits Summary (reprise)

- No named-living-public-figure impersonation in adversarial contexts.
- No content targeting elections, health decisions, or legal proceedings.
- No bypassing legally-binding platform Terms of Service.
- No impersonating real specific private individuals.
- Critic threshold ≥ 7 is a floor, not a suggestion. Below-floor output is
  not shipped without the user seeing it.
- Persona backstories must be **locked before first post**. A persona with
  TBD fields cannot post — it will contradict itself under pressure.
