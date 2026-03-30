---
name: linkedin-dm-outreach
description: >
  This skill should be used when the user asks to "write a LinkedIn message",
  "draft a connection request", "help me reach out to someone on LinkedIn",
  "write an InMail", "message this person", or when the prospect-researcher
  skill identifies a high-value contact worth reaching out to. Also triggered
  by "how should I approach [person/company]" in a LinkedIn context.
consumer: cc_foreground, cc_background_task
phase: 8
skill_type: hybrid
---

# LinkedIn DM Outreach

## Purpose

Write personalized LinkedIn messages — connection requests, InMails, and
follow-up messages — that get responses instead of being ignored. Every
message must demonstrate genuine relevance: why THIS person, why NOW,
why the user is worth responding to.

## Voice Loading

Read voice exemplars and anti-slop rules before drafting:
- `../voice-master/references/exemplars/professional.md`
- `../voice-master/references/anti-slop.md`

If the exemplar file is empty or has no good matches, also read:
- `../voice-master/references/voice-dimensions.md`

DMs are the most personal LinkedIn format — sounding templated here is
fatal. One bad outreach message can permanently close a door.

## Message Types

### Connection Request (300 characters max)

The hardest format. 300 characters to establish relevance and earn a click.

**Rules:**
- Name a specific reason for connecting (shared interest, their content,
  mutual connection, specific role/company)
- No generic "I'd love to add you to my network"
- No pitching in the connection request — ever
- If there's no genuine reason to connect, don't send the request

**Structure:** "[Specific reference to them or their work]. [Why connecting
makes sense for both sides — in one sentence]."

### First Message (after connecting)

Sent after a connection request is accepted, or to an existing connection.

**Rules:**
- Thank them for connecting (brief, not gushing)
- Reference something specific about them or their work
- State your reason for reaching out clearly
- Ask one specific question or make one specific offer
- Keep under 150 words — respect their time
- No "I hope this message finds you well"
- No walls of text about yourself

**Structure:**
- Line 1: Brief genuine acknowledgment
- Line 2-3: Why you're reaching out (specific, relevant)
- Line 4: One clear ask or offer

### Follow-Up Message

If the first message got no response after 5-7 days.

**Rules:**
- Maximum ONE follow-up. Two unanswered messages = they're not interested.
- Add new value — don't just "bump" or "circle back"
- Shorter than the first message
- No guilt ("I know you're busy but...")
- Accept that silence is an answer

### Warm Introduction Request

Asking a mutual connection to introduce you to someone.

**Rules:**
- Make it easy for the introducer — provide a brief, forwardable blurb
- Explain why the introduction makes sense for all three parties
- Never pressure — "only if you think it makes sense"

## Research Integration

Before drafting any outreach, gather intelligence on the recipient:

- Their recent posts and content (what they care about)
- Their current role and company (what they're dealing with)
- Mutual connections (potential introduction path)
- Shared interests or background (common ground)
- Their company's recent news (relevant context)

If the `prospect-researcher` skill has already run on this target, use
its findings. If not, perform lightweight research before writing.

## Outreach Scenarios

### Job Search
**Goal:** Get a conversation about opportunities.
**Approach:** Lead with what you can do for them, not what you want.
Reference specific challenges their company/team faces. Ask about the
team or the work, not about open positions directly.

### Client Acquisition
**Goal:** Start a relationship that could lead to business.
**Approach:** Never pitch in the first message. Offer genuine value first
(insight, introduction, resource). Build rapport before discussing services.
The goal is a conversation, not a sale.

### Networking
**Goal:** Build a genuine professional relationship.
**Approach:** Reference specific shared interests or complementary
expertise. Suggest a concrete (low-commitment) next step — a specific
article exchange, a brief call, a shared event.

### Recruiting / Hiring
**Goal:** Attract a candidate.
**Approach:** Lead with why you noticed THEM specifically (not "we have
an exciting opportunity"). Be transparent about the role and company.
Respect that they may not be looking.

## Output Format

```yaml
recipient: <name and context>
message_type: <connection_request | first_message | follow_up | intro_request>
scenario: <job_search | client_acquisition | networking | recruiting>
research_used: |
  <key findings about the recipient that informed the message>
message: |
  <the message text>
character_count: <for connection requests — must be under 300>
rationale: |
  <why this approach and angle>
```

## References

- `../voice-master/references/exemplars/professional.md` — Professional voice exemplars
- `../voice-master/references/anti-slop.md` — AI-tell avoidance rules
- `../prospect-researcher/SKILL.md` — Deep research on outreach targets
