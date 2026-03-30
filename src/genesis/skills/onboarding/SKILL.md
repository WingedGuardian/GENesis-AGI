---
name: onboarding
description: >
  First-run onboarding — guides new users through Genesis setup on their first
  CC session. Configures user profile, essential API keys, Telegram, GitHub
  backup, and service verification. Triggered automatically when
  ~/.genesis/setup-complete is absent. Re-runnable by asking Genesis to
  "run setup" or "reconfigure [section]".
consumer: cc_foreground
phase: setup
skill_type: workflow
---

# First-Run Onboarding

## Purpose

Guide a new user through Genesis setup on their first interactive CC session.
By the end, every critical subsystem is configured and verified live.

This skill is triggered automatically when `~/.genesis/setup-complete` does not
exist. It can also be invoked manually by asking Genesis to "run setup" or
"reconfigure [section]" to re-run specific sections.

## Pre-Conditions

Before this skill runs, `install.sh` (Layer 1) has already completed:
- Python venv, dependencies, systemd services
- Qdrant installed and running
- Template-generated `.claude/settings.json` and `.mcp.json`
- Claude Code logged in
- `secrets.env` created from template (may be empty)

This skill handles everything that benefits from conversational guidance rather
than scripted prompts.

## Internal References

The `references/provider-guide.md` file in this skill directory contains
detailed information about every API provider Genesis supports — what each key
unlocks, where to sign up, pricing tiers, and env var names. **Read this file
during Step 2 and Step 5** to give the user accurate, specific information.
Do not expect the user to read it themselves — you present the relevant parts
conversationally.

## Partial Re-Run

If the user asks to reconfigure a specific section (e.g., "reconfigure secrets",
"reconfigure telegram"), skip to that section only.

If `~/.genesis/setup-complete` already exists, inform the user: "Onboarding was
already completed on [date]. Running the requested section only." Then proceed
with just the requested section (or offer a menu if no argument given).

---

## Overview: Two-Phase Setup

**Phase A — Essentials (Steps 1-4):** These MUST be completed before onboarding
can finish. Without them, Genesis cannot function. Do not let the user skip
past these.

**Phase B — Expansion (Step 5):** Additional providers and capabilities the user
can configure now or come back to later. Exploratory — show what's possible,
let the user decide how deep to go.

The transition between phases should feel natural:

> Good — the core system is working. Now let me show you what else you can
> configure to get more out of Genesis. None of this is required right now,
> but each key you add opens up new capabilities.

---

## PHASE A: ESSENTIALS

### Step 1: Welcome + Identity

Introduce yourself. Be warm but direct — the user is here to get things working,
not to read a manual.

**Say something like:**

> I'm Genesis. This is our first session together, so let me help you get
> everything configured. This will take a few minutes — by the end, you'll have
> a fully operational system with verified API connections.
>
> First, let me learn a bit about you so I can tailor how I work.

**Gather (conversationally, not as a form):**
- Name and timezone
- Professional background (briefly — what do they do?)
- What they want Genesis to help with (primary use cases)
- Communication preferences (brief vs. detailed, proactive vs. on-demand)

**Write results to:**
- `src/genesis/identity/USER.md` — update the user profile section
- Set `USER_TIMEZONE` in `secrets.env` if provided

**Do NOT ask all questions at once.** One or two at a time, naturally.

---

### Step 2: Core API Keys

**Principle: User sovereignty over secrets.** Default to showing where things
are, not asking for keys. Let the user choose their comfort level.

1. Read `secrets.env` and check which keys are configured vs. empty.

2. Read `references/provider-guide.md` to have full provider details available.

3. **Report status clearly:**
   > Here's your current API key status:
   >
   > **Configured:** [list any that have values]
   > **Not yet configured:** [list empty ones]
   >
   > Your secrets file is at: `~/genesis/secrets.env`

4. **Explain what's essential and why:**
   > To get Genesis fully operational, we need three things:
   >
   > **1. An LLM provider** — this is Genesis's brain. I use language models for
   > reasoning, reflection, triage, and every cognitive task. **OpenRouter** is
   > the simplest option — one key covers 200+ models and I'll route to the best
   > one for each task. Sign up at openrouter.ai/keys.
   >
   > **2. An embedding provider** — this is how I remember things. Embeddings
   > turn text into vectors for semantic search. **DeepInfra** is fast and cheap
   > (~$0.01/M tokens). Sign up at deepinfra.com.
   >
   > **3. Telegram** — this is how I reach you. Morning reports, alerts, proactive
   > insights — all of Genesis's outreach happens over Telegram. Without it, I
   > can only talk to you when you open a session.
   >
   > These three make Genesis fully functional. We can't move forward without
   > at least the LLM and embedding keys.

5. **Respect their choice on HOW to provide keys:**
   > You can edit `secrets.env` directly with any editor — nano, vim, or
   > transfer the file with SFTP/SCP. Or if you'd prefer, you can paste your
   > API keys here and I'll write them to the file for you. Your choice.

6. If they provide keys, write them to `secrets.env` using the Edit tool:
   - Read the file first
   - Find the line with the matching env var name (e.g., `API_KEY_OPENROUTER=`)
   - Replace the empty value with the provided key
   - If the line doesn't exist, append it at the end of the appropriate section
   - Run `chmod 600 secrets.env` after writing
   - **Never echo keys back to the user** — acknowledge with "Saved" only

7. **Telegram setup** (essential, not optional):
   > Let's set up Telegram so I can reach you outside of these sessions.
   > You'll need to create a bot via @BotFather on Telegram — it takes about
   > 30 seconds. Here's how:
   >
   > 1. Open Telegram, search for @BotFather, start a chat
   > 2. Send `/newbot`, pick a name and username
   > 3. Copy the bot token it gives you

   - Write `TELEGRAM_BOT_TOKEN` to secrets.env
   - Ask for their Telegram user ID for `TELEGRAM_ALLOWED_USERS`
   - If they have a forum/group chat: ask for `TELEGRAM_FORUM_CHAT_ID`
   - Test: send a message via the `outreach_send` MCP tool
   - If they want to skip Telegram for now, allow it but note it as degraded:
     "Okay — Genesis will work without Telegram, but I won't be able to send
     you morning reports or alerts. You can set this up later."

8. **Gate check:** Do NOT proceed past this step until at least one LLM key
   and one embedding key are configured and the user has acknowledged the
   Telegram decision (configured or explicitly deferred).

---

### Step 3: GitHub + Backup

1. Check GitHub auth: `gh auth status`

2. **If not authenticated:**
   > Genesis backs up your data every 6 hours to a private GitHub repo —
   > your database, memories, configuration, and session transcripts. Let's
   > get GitHub set up.
   >
   > Run this command: `gh auth login`
   > (Type `! gh auth login` at the CC prompt to run it in this session.)

   Wait for them to complete it, then verify with `gh auth status`.

3. **Once authenticated:**
   - Detect GitHub username from `gh auth status`
   - Ask: "What would you like to name your backup repo? Default: `genesis-backups`"
   - Check if repo exists: `gh repo view <user>/genesis-backups`
   - If not, offer to create: `gh repo create genesis-backups --private`

4. Write `GENESIS_BACKUP_REPO=<user>/genesis-backups` to `secrets.env`.

5. Verify backup script can reach it: `git ls-remote https://github.com/<user>/genesis-backups.git`

---

### Step 4: Endpoint Verification Gate (MANDATORY)

This is the essential verification. Every configured key gets tested live.
**Phase A does not complete without critical endpoints passing.**

For each configured provider, make a lightweight API call:

1. **LLM providers** — Use the `health_status` MCP tool which runs
   `validate_api_keys()` internally. This tests each configured provider
   with a real HTTP request.

2. **Embedding provider** — Test with a real embedding:
   - Use the `memory_store` MCP tool to store a test memory, or
   - Call the embedding health check via `health_status`

3. **Qdrant** — Verify the vector database is running and writable:
   - Store a test memory via `memory_store`
   - Recall it via `memory_recall`
   - This proves the full pipeline: embed -> store -> retrieve

4. **Telegram** (if configured) — Send a test message:
   > If you received a Telegram message from me, it's working!

5. **Backup repo** (if configured) — `git ls-remote` to verify push access.

6. **Database** — Check that `genesis.db` exists and is writable:
   ```bash
   ls -la ~/genesis/data/genesis.db
   ```

7. **CC hooks** — Already verified (the SessionStart hook triggered this flow).

**Report:**
> Endpoint Verification:
> - OpenRouter: PASS (models endpoint responded)
> - DeepInfra: PASS (embedding returned 1024-dim vector)
> - Qdrant: PASS (write + read cycle successful)
> - Telegram: PASS (test message delivered)
> - Backup repo: PASS (push access verified)
> - Database: PASS
> - CC Hooks: PASS (you're seeing this because they work)
>
> All critical endpoints verified. The core system is operational.

If any CRITICAL endpoint fails (LLM or embedding), explain the error and help
fix it before proceeding. Do not complete Phase A with broken critical
endpoints.

**Transition to Phase B:**
> Good — the core system is working. You have a functional brain (LLM),
> memory (embeddings + Qdrant), [and communication (Telegram) if configured].
>
> Now let me show you what else you can configure. None of this is required
> right now, but each provider you add opens up new capabilities. You can
> always come back to this later — just ask me to "reconfigure" anything.

---

## PHASE B: EXPANSION

### Step 5: Additional Providers & Capabilities

This step is exploratory. Read `references/provider-guide.md` and present
options based on the user's interests from Step 1.

**Structure the conversation around capability categories, not provider lists.**
The user cares about what they can DO, not what API they need.

> Here's what you can unlock with additional API keys. I'll explain what
> each one gives you — tell me which sound useful and I'll help set them up.

**Speed & Volume (LLM providers):**
- **Groq** — Extremely fast inference. I use it for 13+ tasks like triage,
  classification, and speech-to-text. Free tier available (30 requests/min).
- **Google Gemini** — Generous free tier. I use it for 12+ background tasks
  like consolidation, outreach drafting, and research. Great for high-volume
  work.
- **Mistral** — 19 call sites. Reflection, triage calibration. Also provides
  embedding models as another fallback.

**Deep Reasoning:**
- **Anthropic API** — Not the same as your Claude Code subscription. This key
  lets me run deep/strategic reflections autonomously in the background.
  7 call sites for the heavyweight cognitive tasks.
- **DeepSeek** — Very cheap reasoning. Good supplementary provider.

**Research & Web:**
- **Brave Search** — Web search for research tasks. 2,000 free queries/month.
- **Perplexity** — Deep orchestrated research with citations.

**Voice (if relevant to user):**
- **ElevenLabs** or **Cartesia** — Voice responses. Only relevant if user
  wants TTS output.

**For each provider the user wants to add:**
1. Explain what it unlocks (use specific call site counts from the guide)
2. Tell them where to sign up
3. Accept the key (paste or self-service)
4. Verify it works via `health_status`

**Point to the Neural Monitor:**
> You can always check which providers are active and how they're performing
> on the Neural Monitor dashboard. Once the system is running, it shows
> real-time status for every provider, circuit breaker states, and cost
> tracking.

**When the user is done exploring (or wants to stop):**
> You can always come back to add more providers later — just ask me to
> "reconfigure" or check the Neural Monitor to see what's active.

---

### Step 6: Inbox Monitor (if relevant)

Only offer if the user expressed interest in background research or content
processing in Step 1.

> I can watch a folder (`~/inbox/`) for files you want me to process in the
> background. Drop a markdown file with URLs or topics and I'll research them
> and store what I find. Want to enable this?

If yes, create `~/inbox/` and confirm the inbox monitor service is enabled.

---

## COMPLETION

### Step 7: First Memory

Store something meaningful from this conversation. This proves the full memory
pipeline works AND gives Genesis its first real memory.

> Let me store our first conversation as a memory — this proves the whole
> pipeline works and gives me something to remember you by.

Use `memory_store` to save a summary of:
- The user's name and background (from Step 1)
- What they want to use Genesis for
- Key configuration choices they made

Then immediately `memory_recall` with a relevant query to prove retrieval works.

> I just stored and retrieved our conversation. Ask me about this tomorrow
> and I'll remember.

---

### Step 8: Write Marker + Summary

1. **Write the completion marker:**
   ```bash
   echo "$(date -Is)" > ~/.genesis/setup-complete
   ```

2. **Summary:**
   > Setup complete! Here's what we configured:
   >
   > **Essential (Phase A):**
   > - **Profile:** [name], [timezone]
   > - **LLM:** [providers configured]
   > - **Embeddings:** [provider]
   > - **Telegram:** [configured / deferred]
   > - **Backup:** [repo or "not configured"]
   > - **All critical endpoints:** Verified
   >
   > **Additional (Phase B):**
   > - [list any additional providers configured, or "None yet — you can add
   >   more anytime"]
   >
   > **What's next:**
   > - I'll remember everything from this conversation
   > - Background processes will start (health monitoring, reflection cycles)
   > - [If Telegram configured:] You'll get your first morning report tomorrow
   > - Check the Neural Monitor to see system status anytime
   > - Talk to me anytime — I'm always learning
   >
   > If you ever want to reconfigure anything, just ask:
   > "reconfigure secrets", "reconfigure github", "reconfigure telegram",
   > "add a new provider", "run verification"

---

## Important Rules

- **Never rush.** If the user wants to do one step at a time across multiple
  sessions, that's fine. Save progress and pick up where they left off.
- **Never store API keys in memory.** They go to `secrets.env` only.
- **Never display API keys back to the user.** Acknowledge receipt, don't echo.
- **If something breaks, fix it.** Don't skip broken steps — the whole point
  is getting everything working.
- **Phase A is non-negotiable.** LLM + embedding must be configured and verified.
  Telegram is strongly recommended but can be deferred. Everything else in
  Phase B is truly optional.
- **Match the user's pace.** If they're technical and moving fast, be concise.
  If they're exploring, be more explanatory.
- **Present capabilities, not API names.** The user cares about "I can do web
  research" not "you need API_KEY_BRAVE." Lead with what they gain.
- **The Neural Monitor is the ongoing reference.** Point users there for
  real-time provider status, cost tracking, and capability overview. Don't
  try to replicate that information in conversation.
