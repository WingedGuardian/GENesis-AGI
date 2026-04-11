# Forum / Casual Medium — Positive Craft Rules

Loaded by voice-master when the target medium is `forum` — long-running
threaded discussions on Discourse, Reddit, phpBB-style boards, HN comments,
and similar community sites.

This file describes **positive craft** — how to write well for this medium.
The **negative rules** (what to avoid) live in the `## Forum / casual` section
of `anti-slop.md`.

## Why Forum Is Its Own Medium

Forum register is not "LinkedIn but more casual." It's a fundamentally
different mode of writing:

- **Reply-culture, not broadcast-culture.** The natural unit is a reply to
  someone else's post, not a standalone statement.
- **Fragment-tolerant.** Incomplete sentences, trailing thoughts, and
  one-liners are native. Over-polish reads as outsider.
- **Idiom-rich.** In-group language, acronyms, running jokes, community
  references. Spelling things out is an outsider tell.
- **Low structural formality.** No opening hook, no closing summary, no
  bullet lists. The post starts where the thought starts and ends where
  it stops.
- **Thread-aware.** A good reply responds to the last few posts, not just
  the original topic.

Rules tuned for LinkedIn (structured opening, payoff lines, calls to action)
actively fail on a forum. A new user posting LinkedIn-style content on a
sports forum or subreddit is instantly visible as an outsider — whether
human or bot.

---

## Reply-Culture Norms

### Respond to the last 20 posts, not the OP

Threads drift. The most recent posts set the active topic of conversation.
Replying only to the original post when the thread has moved on 50 replies
is a sign the poster didn't read the thread, which is an outsider tell.

**Protocol:** before generating a reply, read the last ~20 posts in the
thread. Match your reply to what people are currently arguing about, not
what the OP asked three days ago.

### Quote-reply is common; inline-reply is rare

Forum platforms provide a "quote" feature. Real posters use it to anchor
their reply to a specific earlier comment. One-shot top-level replies are
fine but less common than quoted responses once a thread is active.

### Position-references are native

"Yeah what the guy above said." "First poster had it right." "Third reply
nailed it." These are native phrases. In-group readers parse them instantly.

### One-liners are legitimate replies

A real forum post can be four words. "This is the right take." "Agreed on
all counts." "Hard disagree, here's why:" followed by two sentences.
Generator output that tries to fill every reply with substantive content
overshoots — sometimes a reply is a reaction, not an essay.

---

## Thread Dynamics

### When to lurk

- Thread is moving fast and the reply-rate exceeds your own pace.
- Active drama between regulars — outsiders wading into drama get noticed.
- The topic is hyper-specific and a newcomer has no credible hook.
- The account is < 5 posts old on a thread with > 100 replies and known
  regulars dominating.

### When to post

- A natural conversation hook ("anyone else seeing...", "what's the deal
  with...") — low-stakes, high-engagement, forgiving of newcomers.
- A topic where the persona has a credible angle (a persona defined as a
  hometown sports fan posting in their team's thread is credible; the same
  persona posting in a crypto trading subforum is not).
- Music / movies / general-interest side threads — these are natural
  on-ramps for new accounts because they're personality-building, not
  expertise-gating.

### When to stay out of drama

- Users explicitly feuding.
- Moderator discipline threads ("X got banned, here's why").
- Meta-threads about forum policy.
- Posts where the primary social signal is "which side are you on" and
  the persona has no history of picking sides.

A newbie has no standing in drama. Wading in looks like a plant regardless
of which side the post takes.

---

## Pacing Rules for New Accounts

These are hard constraints for the first 5 days of a new stealth account.
Enforced by the `persona_posts` DB — pacing checks run before generation.

- **≤ 1 post per thread per 12 hours.** Burst-replying to a single thread
  is the #1 plant tell.
- **≤ 3 posts per day total** across the forum for the first 5 days.
- **No first-post in Politics threads** (or the platform's equivalent
  high-stakes category). First posts should be in low-stakes, personality-
  building categories: Music, Movies, General, Home Improvement, team-
  specific fan threads.
- **Spread the first 5 posts across 2-3 different threads.** An account
  whose first 5 posts are all in the same thread looks like a plant for
  that thread.
- **No posting in the same thread an existing persona-post is pending a
  reply to.** Wait for the reply, don't stack posts.

After 5 days and ~10 total posts, relax pacing to the persona's natural
cadence as described in the persona directory.

---

## Vibe-Matching Protocol

Before generating any candidate, the skill reads the last ~20 posts in the
target thread to calibrate register. Extract:

### Register signals

- **Sentence length distribution** — are replies 1-2 sentences or 1-2
  paragraphs?
- **Fragment frequency** — how often do regulars use incomplete sentences?
- **Profanity level** — do regulars curse casually, never, or only for
  emphasis?
- **Emoji/reaction usage** — does the forum use emoji or not (Discourse
  supports them; whether a specific community uses them is cultural).
- **Technical/jargon density** — how many in-group acronyms and
  abbreviations per post?

### Social signals

- **Who's replying to whom** — the active conversations within the thread.
- **What the current argument is** — often different from the OP.
- **Who the regulars are** — names that appear multiple times.
- **Tone of the thread** — hostile? agreeable? joking? serious?

### Output calibration

Generate candidates that match the register signals. A reply that's more
formal, more polished, or more structured than the average of the last 20
posts is an outlier and will be read as such. Err toward matching the
lower-polish end of the observed range, not the average.

---

## Platform-Specific Notes

### Discourse

- Supports @-mentions (@username) and quote-replies.
- Supports markdown in posts but most forum communities don't use much
  formatting beyond basic bold/italic and blockquotes for quoting.
- Post-rate limits exist but are generous for established accounts; new
  accounts have stricter rate limits that the pacing rules above already
  respect.
- Edit history is visible to other users — editing a post right after
  submission is normal, editing it days later is flagged.

### Reddit

- Paragraph breaks are rendered faithfully; use them.
- Downvote-aware: strongly-opinionated posts in low-agreement threads
  will get downvoted and collapse. A newcomer getting visibly downvoted
  is a persona-stress event.
- Username conventions vary wildly by subreddit.

### Phpbb-style / vBulletin

- BBCode rather than markdown in some cases.
- Post signatures are common and persona-defining.

### HN / Lobsters / similar tech forums

- Much higher baseline formality than mass-market forums, but without
  LinkedIn-style puffery. "Dry, technical, pointed" is the register.
- Stealth mode on tech forums is harder because the regulars are
  professional skeptics. Score thresholds should be higher (critic ≥ 8).

---

## Load Order

When voice-master loads with `medium=forum`:

1. Load `anti-slop.md` Universal + Forum sections.
2. Load this file (`media/forum.md`).
3. If stealth mode is also active, load `stealth-writing.md`.
4. If a persona is specified, load the persona directory.

Generate with all four layers active. Anti-slop catches generic AI tells;
forum.md provides positive craft rules; stealth-writing handles anti-
attribution if applicable; persona provides the positive voice target.
