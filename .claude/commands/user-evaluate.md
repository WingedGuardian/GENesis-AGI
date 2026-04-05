---
name: user-evaluate
description: >
  Evaluate content through the lens of what Genesis knows about the user.
  Articles, research, ideas, tools, or any content the user wants analyzed
  for personal relevance and actionable value. The differentiator is the
  user model — Genesis's accumulated understanding of who this person is.
---

# User Evaluation Framework

## Purpose

Evaluate content the user cares about — through the lens of what Genesis knows
about them. Produce personalized, actionable findings that go beyond what a
generic AI summary would give. The value Genesis adds is context: connecting
this content to the user's interests, goals, projects, and knowledge.

## Core Principle

**Assume it matters.** The user put this content here for a reason. Your job is
to find HOW it matters to them, not WHETHER it matters. Never dismiss content as
irrelevant based on the user model. The user decides what matters; Genesis finds
the value.

## Phase 1: Context Assembly

### User Model Loading

Before evaluating ANY content, assemble the deepest user context available:

1. **Read USER.md** — the compressed snapshot (always available, but this is
   the floor, not the ceiling)
2. **Search memory system** — use `memory_recall` MCP tool to find context
   about the user's relationship to this content's topics. Search for:
   - Topics related to the content
   - Recent user interests and activities
   - Past evaluations of similar content
   - Projects the user is working on
3. **Check recent observations** — user activity signals, conversation patterns
4. **Check user_model_cache** — structured fields (interests, goals, expertise)

The richer your understanding of the user, the more valuable the evaluation.
If the memory system returns nothing relevant, that's fine — fall back to
content-native analysis. But you MUST search first.

### Source Acquisition

Fetch all sources simultaneously. Never serialize independent lookups.

When a source is inaccessible, exhaust all autonomous options:

1. Try the primary tool (WebFetch, scrape, direct access)
2. Try alternative tools (Firecrawl, other MCP tools)
3. Route to a different model/service:
   - YouTube video → Gemini API (native YouTube URL support)
   - Paywalled article → Firecrawl (JS rendering, paywall bypass)
   - Authenticated service → check for specialized MCP tools
4. Try creative workarounds (transcript APIs, metadata services, cached versions)
5. Only then ask the user — with specific options, not "what was it about?"

## Phase 2: Four-Lens Evaluation

Evaluate EVERY finding through all four lenses. Do not skip or collapse them.

### Lens 1: What This Is

Content-native analysis. Ground this in the ACTUAL content — not what you think
it probably says.

- What's the core argument, thesis, or contribution?
- What evidence or data supports it?
- What's new or notable about this? What's the state of the art?
- Who created it and what's their credibility/perspective?
- What context is needed to understand it properly?

### Lens 2: How This Could Help You

User-model-informed value extraction. This is where Genesis's understanding of
the user makes the evaluation personal.

- How does this connect to the user's known interests and goals?
- What knowledge or skills of theirs does this build on?
- What problems they're working on does this address?
- What opportunities does this create for them specifically?
- What connections to their other work or interests exist?

If the user model is thin on this topic, say so honestly and provide
content-native value assessment instead. Don't fabricate personal connections.

**CRITICAL:** Never filter content out because it doesn't match the user model.
The user may be exploring NEW interests. "This doesn't match your known profile"
is information to note, not a reason to dismiss.

### Lens 3: What We Could Do With It

Collaborative actions — what Genesis and the user could do together. The "we"
framing is deliberate. Genesis is a partner, not a report generator.

- "I could research this further and report back"
- "You might want to reach out to [person/org]"
- "We could prototype/test/build on this"
- "I can monitor this space and alert you to developments"
- "This connects to [other thing] — we should evaluate them together"
- "I could set up [automation/tracking/integration] for this"

Be concrete. "This is interesting" is not an action. "I could set up a weekly
scan of this author's publications" is.

### Lens 4: What to Watch

Critical assessment. Cover the user's back.

- What's missing from this content? What questions aren't answered?
- What counterarguments or alternative perspectives exist?
- What biases does the source have? (funding, ideology, platform)
- What would need to be true for this to be useful? What assumptions?
- What could go wrong if the user acts on this without verification?
- Is this content current? Could it be outdated?

## Phase 3: Synthesis

### Lightweight Tags (Report-Only)

Include these in the evaluation output to help the user prioritize. These are
Genesis's SUGGESTIONS — not binding decisions. The user has authority.

**Action Timeline:**
- **Now** — immediately actionable, time-sensitive, or low-effort high-value
- **Soon** — worth pursuing but not urgent
- **Someday** — valuable reference, revisit when relevant

**Relevance:**
- **Direct** — clearly connects to known interests/projects
- **Tangential** — indirect connection, might become relevant
- **Background** — general knowledge, broadens perspective

### Action Items

If the evaluation produces concrete action items for the user, note them with
a reference to `docs/actions/user/active.md` as the canonical tracking location.
Format:

> **Action item identified:** [description] — should be tracked in
> `docs/actions/user/active.md`

## Phase 4: Discussion & Refinement

When evaluating with the user (interactive):
- Present findings with the four-lens structure
- Invite pushback — the user knows their own context better than the model
- Update assessments when corrected, don't defend them
- Surface non-obvious connections between this and other content

When evaluating autonomously (inbox, surplus):
- Apply the four lenses without interactive refinement
- Flag low-confidence personal connections for user review
- Be explicit about what the memory system DID and DIDN'T surface

## Phase 5: Documentation

### Action Item Tracking
Action items go to `docs/actions/user/active.md`. Follow the format in
`docs/actions/README.md`.

### Memory Updates
If the evaluation reveals something about the user's interests or goals that
the user model doesn't capture, note it: "This evaluation suggests you're
interested in [X] — this isn't reflected in the current user model."

## Anti-Patterns

### Do NOT:
- Dismiss content because the user model doesn't mention this topic
- Over-personalize to the point of filtering out useful information
- Assume the user's interest level — they put it here, it matters
- Produce a summary instead of an evaluation
- Skip URLs — every URL must be fetched and individually addressed
- Say "I have what I need" and skip remaining sources
- Fabricate personal connections when the user model is thin
- Store priority/timeline tags as binding metadata on action items
- Make "we could" suggestions that Genesis can't actually do

### Watch For:
- "This doesn't match your profile" — that's not a reason to dismiss,
  it's a reason to note and explore
- "This is interesting" — that's not an evaluation, add substance
- Lens 2 becoming a filter instead of a value-finder
- Lens 3 becoming vague ("look into this") instead of concrete
- Lens 4 becoming dismissive instead of protective
- Assuming the user already knows things — explain enough for context
