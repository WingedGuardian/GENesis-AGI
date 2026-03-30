# Provider Guide — Internal Reference for Onboarding

**This file is read by Genesis during onboarding, not by the user.**

Use this reference to give the user accurate, specific information about each
provider. Present the relevant parts conversationally — the user should never
need to open this file.

---

## Essential Setup (Phase A — must complete)

These three make Genesis fully functional. LLM + embedding are non-negotiable;
Telegram is strongly recommended.

### 1. LLM Provider — Genesis's Brain

#### OpenRouter (RECOMMENDED — single-key solution)
- **What it unlocks:** 200+ models through one API key. Genesis routes to the
  best model for each task automatically. Covers all 16+ LLM call sites.
- **Signup:** openrouter.ai/keys
- **Pricing:** Pay-per-token, varies by model. Some free community models.
- **Why recommended:** One key covers everything. No need to manage multiple
  provider accounts. Genesis's router picks the right model per task.
- **Env var:** `API_KEY_OPENROUTER`

### 2. Embedding Provider — Genesis's Memory

#### DeepInfra (RECOMMENDED)
- **What it unlocks:** Cloud embeddings via `qwen3-embedding` (1024 dims).
  This is what makes semantic memory work — store, recall, and connect ideas.
- **Signup:** deepinfra.com -> Dashboard -> API Keys
- **Pricing:** Very cheap (~$0.01/M tokens).
- **Why recommended:** Fast, reliable, cheap. Canonical embedding model.
- **Env var:** `API_KEY_DEEPINFRA`

#### DashScope (fallback)
- **What it unlocks:** Same `qwen3-embedding` model via Alibaba's infrastructure.
  Automatic fallback if DeepInfra is down.
- **Signup:** dashscope.console.aliyun.com
- **Note:** Uses `API_KEY_QWEN` (same key as the Qwen LLM provider).

### 3. Telegram — Genesis's Voice

Telegram is the primary outreach channel. Without it, Genesis can only
communicate during active CC sessions. With it, Genesis sends:
- Morning reports
- Health alerts
- Proactive insights and recommendations
- Two-way conversational interaction

- **Setup:** Create a bot via @BotFather on Telegram (~30 seconds)
- **Env vars:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`,
  `TELEGRAM_FORUM_CHAT_ID` (optional, for forum/group chats)

---

## Expansion Providers (Phase B — optional, adds capability)

Present these organized by CAPABILITY, not by provider name. The user cares
about what they can do, not which API to call.

### Speed & Volume

#### Groq
- **Capability:** Extremely fast inference for time-sensitive tasks.
- **What it unlocks:** 19 call sites — triage, classification, tagging,
  research synthesis, speech-to-text (Whisper). When speed matters more than depth.
- **Signup:** console.groq.com
- **Pricing:** Free tier with 30 RPM. Paid plans available.
- **Env var:** `API_KEY_GROQ`

#### Google (Gemini)
- **Capability:** High-volume background processing at very low cost.
- **What it unlocks:** 13 call sites — memory consolidation, surplus compute
  tasks, outreach drafting, orchestrated research, email triage.
- **Signup:** console.cloud.google.com -> APIs & Services -> Credentials
- **Pricing:** Gemini Flash free tier is very generous. Pro is paid.
- **Env var:** `GOOGLE_API_KEY`

#### Mistral
- **Capability:** Versatile mid-tier provider with embedding support.
- **What it unlocks:** 21 call sites — reflection, triage calibration, learning
  pipeline. Also provides embedding models as additional fallback.
- **Signup:** console.mistral.ai
- **Pricing:** Limited free RPM. Paid plans available.
- **Env var:** `API_KEY_MISTRAL`

### Deep Reasoning

#### Anthropic (Claude API)
- **Capability:** Heavyweight autonomous reasoning.
- **What it unlocks:** 13 call sites — deep/strategic reflection, user model
  synthesis, pre-execution assessment. The tasks that require careful, nuanced thinking.
- **Signup:** console.anthropic.com
- **Pricing:** Per-token. Higher cost, highest quality.
- **Important distinction:** Claude Code sessions use the user's CC
  subscription. This API key is separate — it's for Genesis's autonomous
  background tasks that run without a CC session.
- **Env var:** `ANTHROPIC_API_KEY`

#### DeepSeek
- **Capability:** Cheap supplementary reasoning.
- **What it unlocks:** Direct provider for reasoning tasks. Good as a
  cost-effective fallback.
- **Signup:** platform.deepseek.com
- **Pricing:** Extremely cheap per-token.
- **Env var:** `API_KEY_DEEPSEEK`

#### Qwen (Alibaba DashScope)
- **Capability:** Fallback embeddings. LLM routing slots currently unused.
- **What it unlocks:** DashScope embeddings (same key as the embedding fallback
  above). No active LLM call sites — former slots migrated to OpenRouter.
- **Signup:** dashscope.console.aliyun.com
- **Pricing:** Cheap per-token.
- **Env var:** `API_KEY_QWEN`

### Research & Web

#### Brave Search
- **Capability:** Web search for research tasks.
- **What it unlocks:** Genesis can search the web during research, fact-checking,
  and exploration tasks.
- **Signup:** brave.com/search/api
- **Pricing:** 2,000 free queries/month.
- **Env var:** `API_KEY_BRAVE`

#### Perplexity
- **Capability:** Deep orchestrated research with citations.
- **What it unlocks:** Multi-source research with automatic citation tracking.
  For when a simple web search isn't enough.
- **Signup:** perplexity.ai/settings/api
- **Env var:** `API_KEY_PERPLEXITY`

### Voice / TTS

Only relevant if the user wants voice responses.

#### ElevenLabs
- **What it unlocks:** High-quality voice synthesis.
- **Signup:** elevenlabs.io -> Profile -> API Keys
- **Env vars:** `API_KEY_ELEVENLABS`, `TTS_VOICE_ID_ELEVENLABS`

#### Cartesia Sonic
- **What it unlocks:** Low-latency voice synthesis.
- **Signup:** cartesia.ai -> Dashboard -> API Keys
- **Env vars:** `API_KEY_CARTESIA`, `TTS_VOICE_ID_CARTESIA`

### Local Inference (advanced)

#### Ollama
- **Capability:** Local embeddings and small model inference. No API key, no
  cost, no network dependency.
- **Setup:** Set `GENESIS_ENABLE_OLLAMA=true` and `OLLAMA_URL` in secrets.env.
- **Note:** Genesis uses cloud-first architecture by default. Ollama is for
  air-gapped environments or users who want to run everything locally.

---

## Quick-Start Tiers

Use these when summarizing options for the user:

**Essential (3 keys — gets Genesis fully operational):**
- OpenRouter (LLM) + DeepInfra (embeddings) + Telegram (outreach)

**Enhanced (5 keys — adds speed and volume):**
- Essential + Groq (fast inference) + Google Gemini (cheap background tasks)

**Full capability (7+ keys):**
- Enhanced + Anthropic (deep reasoning) + Brave (web research)
- Plus any specialty providers based on user interests
