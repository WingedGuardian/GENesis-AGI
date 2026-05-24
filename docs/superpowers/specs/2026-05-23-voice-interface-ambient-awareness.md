# PRD: Genesis Voice Interface & Ambient Awareness

**Status:** Draft v4 (Phase 0a+0b complete, Phase 1 in progress)
**Author:** Genesis + User
**Date:** 2026-05-23

## 1. Problem Statement

Genesis is a cognitive partner that remembers, learns, and anticipates — but only
through text channels (Claude Code CLI, Telegram, Dashboard). The user's original
vision includes ambient awareness: Genesis as an always-present cognitive co-pilot
that listens, understands context, and speaks up when it matters. This requires two
capabilities that don't exist today:

1. **Real-time voice conversation** — talk to Genesis naturally through a speaker/mic,
   with sub-second latency, interruption handling, and full access to Genesis's
   capabilities (memory, tools, actions).

2. **Ambient transcription & awareness** — Genesis passively listens to the user's
   environment, captures and processes what it hears, and can proactively intervene
   when something warrants Genesis's attention.

These are two modes of one system, sharing infrastructure (audio capture, transcription,
Genesis tool access) but differing in interaction model.

## 2. Prior Art: Gatekeeper v1

**Significant prior work exists** on VM 200 (`192.168.50.148`, hostname `assistant1`).
A "Gatekeeper" service was built ~3 months ago (Feb 2026) as the voice assistant
middleware layer. Current state: Docker containers exist but are all stopped.

### What Was Built
- **Gatekeeper FastAPI service** — orchestrates the voice pipeline:
  - `POST /v1/assist/respond` — receives transcript, routes (respond/store/ignore),
    calls cloud LLM (OpenRouter), returns reply text
  - `POST /v1/assist/session/touch` — extends 45-second session sustain timer
  - Admin endpoints: `/v1/admin/health`, `/v1/admin/events`, `/v1/admin/stats`,
    `/v1/admin/transcripts`
  - PII redaction (regex + spaCy NER, two-pass cascade, <250ms p95 target)
  - Encrypted transcript storage (Fernet-based)
  - Budget management (daily request + cost caps)
  - Session management (Redis-backed, 45s sustain, max turns/duration)
  - Route classification: high salience → respond, medium → store, low → ignore
  - Owner-only action execution with confidence-gated confirmation
  - HA action execution via REST API
- **Infrastructure stack** (Docker Compose):
  - Caddy reverse proxy (mTLS termination, port 8443)
  - PostgreSQL 16 (sessions/events persistence)
  - Redis 8 (short-lived state/rate limits)
  - Qdrant (memory profile, not yet wired)
  - Wyoming Faster-Whisper (STT, `base-int8` model, port 10300)
  - Wyoming Piper (TTS, `en_US-lessac-medium` voice, port 10200)
- **Security**: mTLS + bearer token auth, encrypted transcript storage,
  scoped admin access
- **HA Integration**: Shell commands + automations wired to trigger session touch
  on STT/TTS boundary events from Voice PE satellite

### What's on the HA VM (VM 100, 192.168.50.99)
- Home Assistant OS 2026.1.1 (update available to 2026.5.4)
- **Add-ons running**: Whisper (STT), Piper (TTS), openWakeWord, SSH
- Voice PE satellite configured (entity: `assist_satellite.home_assistant_voice_0a2841`)

### What Was NOT Built
- Genesis integration (Gatekeeper routes to OpenRouter, not Genesis)
- Ambient/passive transcription (reactive only — 45s session window)
- Proactive chiming (explicitly disabled in v1 plan)
- Speaker diarization
- Memory tier for ambient data
- Speech-to-speech model integration (pipeline STT→LLM→TTS only)

### Reuse Assessment
The Gatekeeper is a substantial foundation. Key components to reuse:
- Session management with sustain timer
- PII redaction cascade (regex + NER)
- Event logging and admin endpoints
- HA action execution pattern
- mTLS security model
- Docker Compose infrastructure (Postgres, Redis, Qdrant, STT, TTS)

### Relationship to This Plan
The Gatekeeper was designed for a different vision: a standalone reactive voice
assistant with a cloud LLM behind it. Our plan is fundamentally different —
Genesis IS the brain, voice is just another channel (like Telegram). The design
adapts to our plan, not the other way around.

**What to reuse as components:**
- PII redaction cascade (regex + spaCy NER) — proven, <250ms, two-tier (cloud vs storage)
- Session sustain timer pattern (45s window, reset on speech events)
- mTLS + bearer token security model
- HA shell command / automation wiring patterns for Voice PE events
- Encrypted transcript storage with scoped admin access
- Budget management (daily cost/request caps — relevant for speech model costs)
- Route classification concept (respond/store/ignore) — applicable to ambient triage
- Action execution with confidence-gated confirmation and owner-only enforcement
- Docker Compose infrastructure (Postgres, Redis, Qdrant already provisioned)

**What to NOT reuse:**
- OpenRouter as the LLM backend — replaced by Genesis HTTP Tool API
- Rule-based salience routing — replaced by Genesis's intelligence
- The Gatekeeper as the "brain" — it becomes a gateway/middleware, not the decision-maker
- The 45s reactive-only model — replaced by dual active/passive modes

**Notable Gatekeeper v1 plan concepts we should incorporate:**
- 3-tier memory materialization (short buffer / working memory / episodic summaries) —
  maps to our ambient memory tier design
- Stricter redaction for stored transcripts than cloud-bound payloads — privacy tiering
- Action confirmation gates with owner-only enforcement — relevant for home automation
- Degraded-mode behavior: explicit spoken failure diagnosis when backends are down

## 3. User Outcomes

What the user should be able to do when this is complete:

- Talk to Genesis through a Home Assistant speaker/mic like talking to Alexa, except
  with Genesis's full memory, knowledge, and action capabilities behind it
- Have Genesis listen to ambient conversations and extract useful intelligence
  (decisions made, action items, names, topics) without explicit commands
- Have Genesis proactively chime in during conversations when it has something
  valuable to contribute — without being explicitly called
- Use an earpiece for real-time coaching during conversations (what to say,
  facts to reference, suggestions)

## 4. Interaction Modes

Four core modes, plus cross-cutting capabilities that enhance all modes.
Built in layers — each mode builds on infrastructure from previous modes.

### Mode 1: Active Conversation
**Trigger:** Wake word ("Genesis") or explicit activation
**Experience:** Natural two-way conversation. Genesis responds via speaker. Low
latency (<500ms). Supports interruption. Full tool access (memory recall, knowledge
lookup, task dispatch, web search). Can brief you on topics, answer questions,
capture your thoughts — all as natural conversation capabilities, not separate modes.
**Architecture:** Speech-to-speech model (OpenAI Realtime / Gemini Live / future)
connected to Genesis HTTP Tool API via function calling. Heavy work dispatched to
CC sessions in background. The speech model should be able to continue conversing
while Genesis tools process in the background — not "let me look that up for you"
but seamless context-aware dialogue.

### Mode 2: Passive Intelligence Gathering
**Trigger:** Always on (when enabled)
**Experience:** Silent. User doesn't know Genesis is processing unless they check.
Transcripts archived. Key signals extracted and stored in ambient memory tier.
Speaker identification classifies and tags by speaker. Thought capture, dictation,
and contextual intelligence are natural outcomes of good extraction — not separate
modes requiring explicit activation.
**Architecture:** On-device Whisper → transcript chunks → extraction pipeline →
ambient memory. No real-time Genesis involvement.

### Mode 3: Proactive Situational Awareness
**Trigger:** Streaming filter detects something matching Genesis's "hot context"
**Experience:** Genesis speaks up: "Hey, that deployment you mentioned is actually
broken right now." Or notices emotional escalation and offers to help de-escalate.
Threshold is high — Genesis only interrupts for compelling reasons.
**Architecture:** Hot context window (refreshed every 5-10 min from memory) + fast
filter on every transcript chunk. Notable matches trigger full Genesis evaluation.
Response via speaker or earpiece notification.

### Mode 4: Active Coaching
**Trigger:** Explicit activation ("Genesis, coach me through this conversation")
**Experience:** Genesis listens to both the user and the other person. Provides
real-time suggestions via earpiece or screen overlay. Context-aware (knows what
the user wants to achieve, who they're talking to).
**Architecture:** Speech model in listen-only mode for ambient audio + active
suggestion mode for user's earpiece. Requires multi-stream audio handling.

### Cross-Cutting Capabilities (not separate modes)
These are features that any mode should support when the underlying infrastructure
is mature enough:
- **Contextual briefing**: Active mode naturally supports "brief me on X" via
  memory recall + synthesis — it's just a conversation with Genesis.
- **Thought capture**: Passive mode with good speaker ID captures monologues
  naturally — no explicit "I'm thinking out loud" trigger needed.
- **Acoustic monitoring**: "Alert me if you hear X" — a filter on the ambient
  audio stream (sound classification or keyword matching). Always-on in both
  active and passive modes. This is a new frontier (non-speech audio analysis)
  that may require specialized models.
- **Multi-speaker intelligence**: Speaker diarization + per-speaker trust levels
  enable different handling for different people. Critical for families, meetings.

## 5. Architecture

### 5.1 System Diagram

```
HOME ASSISTANT (Proxmox VM 100 — 192.168.50.99)
  ├── Voice PE satellite (mic + speaker)
  ├── Wake word detection (OpenWakeWord — already running)
  ├── Whisper add-on (STT — already running)
  ├── Piper add-on (TTS — already running)
  ├── Mode controller (passive ↔ active switching) [NEW]
  └── Automations: STT/TTS events → Gatekeeper touch

GATEKEEPER (Proxmox VM 200 — 192.168.50.148, hostname: assistant1)
  ├── FastAPI gateway (sessions, routing, redaction) [EXISTS, stopped]
  ├── Caddy reverse proxy (mTLS, port 8443) [EXISTS, stopped]
  ├── PostgreSQL 16 (events, sessions) [EXISTS, stopped]
  ├── Redis 8 (session state) [EXISTS, stopped]
  ├── Wyoming Faster-Whisper (backup STT) [EXISTS, stopped]
  ├── Wyoming Piper (backup TTS) [EXISTS, stopped]
  ├── Qdrant (ambient memory — not yet wired) [EXISTS, stopped]
  ├── Genesis connector (replaces OpenRouter) [NEW]
  ├── Ambient transcription receiver [NEW]
  └── Speaker diarization pipeline [NEW]

SPEECH MODEL (Cloud API) [NEW]
  ├── OpenAI Realtime API / Gemini Live / future provider
  ├── Handles active conversation (Mode 1, 4)
  └── Calls Genesis tools via function calling

GENESIS CONTAINER (100.97.179.21)
  ├── HTTP Tool API [NEW — FastMCP streamable-http transport]
  │   ├── /mcp/health/* — health tools
  │   ├── /mcp/memory/* — memory_recall, memory_store, etc.
  │   ├── /mcp/outreach/* — outreach tools
  │   └── /mcp/recon/* — recon tools
  ├── Ambient Transcript Pipeline [NEW]
  │   ├── Receives transcript chunks from Gatekeeper
  │   ├── Raw transcript storage (local → NAS offload later)
  │   ├── Extraction pipeline (signals from chunks)
  │   └── Ambient memory tier (separate Qdrant collection)
  ├── Ambient Attention System [NEW]
  │   ├── Hot context window (refreshed from memory every 5-10 min)
  │   ├── Streaming filter (fast model, every chunk)
  │   └── Notable signal → Genesis evaluation → chime-in decision
  ├── Voice Channel Adapter [NEW — extends ChannelAdapter]
  │   ├── HTTP connection to Gatekeeper
  │   ├── Integrates with ConversationLoop (same as Telegram)
  │   ├── VoiceDeliveryHelper for outbound voice
  │   └── Outreach pipeline integration (proactive messages)
  └── Existing systems (ego, awareness loop, outreach, CC relay)

NAS (user's network storage)
  └── Raw transcript archive (90-day retention)
```

### 5.2 Genesis HTTP Tool API

**Why:** Speech models call tools via function calling over HTTP. Current MCP tools
only support stdio transport (Claude Code subprocess). Everything downstream needs
HTTP access to Genesis tools.

**How:** FastMCP 2.14.6 natively supports `streamable-http` transport. Minimal change:
add `--transport` and `--port` flags to `genesis_mcp_server.py`. Tools work immediately
over HTTP without any MCP code changes.

**Key files:**
- `scripts/genesis_mcp_server.py` — add transport flag
- `.mcp.json` — add HTTP variant entries
- `src/genesis/mcp/health/__init__.py` — no changes needed (transport-agnostic)

**Security:** API key or token auth on HTTP endpoints. Not exposed to internet —
Tailscale network only.

### 5.3 Ambient Memory Tier

**Why:** Current memory system (~23K memories) would be overwhelmed by ambient
transcription volume (50-200 signals/day, 18-73K/year). Quality would degrade.

**Design:**
- Separate Qdrant collection: `ambient_memory` (distinct from `episodic_memory`)
- Separate SQLite table: `ambient_transcripts` (raw chunks) + `ambient_signals`
  (extracted structured data)
- Lower default confidence: 0.3-0.5 (vs 0.8-0.95 for normal memories)
- Aggressive decay: unreferenced ambient memories expire after configurable period
- Promotion gate: ambient memory enters main memory only after validation
  (referenced N times, confirmed by user, or cross-correlated)
- Opt-in query: `memory_recall` does NOT search ambient by default.
  Requires explicit `include_ambient=True` parameter.
- Volume caps: max N signals extracted per day to prevent memory bloat

### 5.4 Ambient Attention System ("Hot Context")

**Why:** Deciding what's "notable" in ambient audio requires knowing what Genesis
knows. But querying full memory on every 10-30s chunk is too expensive/slow.

**Design:**
- **Hot context window**: A pre-loaded set of ~20-50 items Genesis currently
  cares about. Refreshed every 5-10 min from memory + goals + active tasks.
  Contents: active goals, recent decisions, known issues, people of interest,
  pending action items, monitoring watches.
- **Streaming filter**: Cheap/fast model (Haiku, Groq Llama, or local SLM)
  compares each transcript chunk against hot context. Binary output: notable
  or not. 99% → archive. 1% → escalate.
- **Escalation path**: Notable match → full Genesis evaluation (memory recall +
  context check + decision). Response time: 30-60 seconds from utterance.
- **Proactive output**: If Genesis decides to chime in → routes through outreach
  pipeline to voice channel adapter → speaker output.

This is NOT the ego. It's a lighter-weight, faster attention system. The ego
continues to operate on its own cadence for strategic decisions.

### 5.5 Transcription Quality & Privacy

**Transcription pipeline:**
```
Mic → VAD (is anyone speaking?)
  → NO: discard, don't transcribe
  → YES: chunk (10-30s segments)
    → Whisper (on-device or Groq cloud)
      → Confidence check (token probabilities)
        → Low confidence: flag, don't process further
        → High confidence: proceed
          → Speaker diarization (who is speaking?)
          → Presidio PII scan → redacted transcript
          → Archive raw + process redacted
```

**Quality mitigations:**
- VAD before Whisper eliminates silence hallucination
- Short chunks (10-30s) prevent long-segment quality degradation
- Confidence scoring filters garbled transcription
- Speaker diarization enables per-speaker trust levels

**Privacy:**
- Presidio PII filter (deterministic, not LLM-dependent) between transcript
  and extraction pipeline
- Three data tiers with different retention:
  - Raw audio: NOT stored by default (option for on-device temporary buffer)
  - Redacted transcript: stored on NAS, 90-day retention
  - Extracted signals: in ambient memory tier, confidence-tagged

**Multi-speaker handling:**
- Primary user: voice profile match → high-confidence ambient
- Known speakers: registered profiles → medium-confidence
- Unknown speakers: → low-confidence, limited extraction
- Children/noise: → filtered more aggressively by extraction prompt

### 5.6 Voice Channel Adapter

New Genesis channel, following existing `ChannelAdapter` pattern:

**Implements:**
- `ChannelAdapter` ABC (start, stop, send_message, send_voice, get_capabilities)
- Handler factory routing incoming transcripts to `ConversationLoop`
- WebSocket/HTTP connection to Home Assistant custom integration
- VoiceDeliveryHelper integration for outbound voice synthesis

**Registers with:**
- `runtime.register_channel("voice", adapter, recipient=...)` for outreach
- Outreach pipeline can route alerts, proactive messages to voice channel
- Observations created for channel-specific failures

**Directory structure:**
```
src/genesis/channels/voice/
├── __init__.py
├── adapter.py          # VoiceChannelAdapter(ChannelAdapter)
├── handlers.py         # Route transcript → ConversationLoop
├── config.py           # Hot-reloadable voice channel config
├── attention.py        # Hot context + streaming filter
├── transcript.py       # Transcript storage + extraction pipeline
└── transport.py        # WebSocket/HTTP to Home Assistant
```

## 6. Due Diligence (2026-05-23)

Verified findings from code intelligence and live testing. Confidence levels
reflect what was actually tested vs assumed.

### 6.1 FastMCP HTTP Transport — VERIFIED (confidence: 90%)
**Test:** Launched Genesis health MCP server over `streamable-http`, performed
full MCP handshake (initialize → tools/list → tools/call).
- `create_streamable_http_app()` works with Genesis MCP servers ✓
- MCP initialize returns session ID, protocol negotiation succeeds ✓
- `tools/list` returns all 49 health tools ✓
- `tools/call` on `health_status` executes correctly (returned "unavailable"
  because runtime wasn't bootstrapped — expected in bare test) ✓

**Wrinkle:** MCP HTTP is stateful (requires session ID header after init).
Speech model function calling expects stateless HTTP. Need either a thin
REST wrapper maintaining sessions, or SSE transport. Solvable, not a blocker.

**Verdict:** Phase 0a is confirmed feasible.

### 6.2 ChannelAdapter Extension — VERIFIED (confidence: 95%)
**Analysis:** GitNexus impact analysis + Serena reference search on ChannelAdapter.
- Only 2 implementations exist: TelegramAdapterV2, EmailAdapter
- Blast radius of adding a third: effectively zero (LOW risk)
- `register_channel` called in exactly 2 places (standalone.py, bridge.py)
- VoiceDeliveryHelper already accepts any ChannelAdapter via parameter type
- No hidden coupling or assumptions about channel count

**Verdict:** Phase 1 voice channel adapter is straightforward.

### 6.3 Ambient Memory Tier — FEASIBLE WITH CAVEATS (confidence: 70%)
**Analysis:** Read memory store.py, retrieval.py, dream_cycle.py, linker.py.
- `HybridMemoryStore.store()` already has a `collection` parameter for
  explicit Qdrant collection override — can write to `ambient_memory` today
- `HybridRetriever.recall()` iterates a `collections` list — adding
  `ambient_memory` when `include_ambient=True` is mechanically simple
- Confidence gate already exists: low confidence → FTS5 only, skip Qdrant.
  Ambient memories (0.3-0.5) would naturally get gated, which is desirable.

**Caveats requiring Phase 3 sub-plan:**
- Dream cycle sweeps `episodic_memory` — needs awareness of ambient collection
  or it'll miss ambient decay/consolidation
- Memory dedup checks all content regardless of collection — ambient could
  false-positive match against existing memories
- Linker creates graph edges across collections — ambient→real edges may
  pollute the knowledge graph (or may be valuable — needs experimentation)
- FTS5 table is collection-agnostic — need filter column for ambient exclusion

**Verdict:** Feasible. Not "just add a collection name" — surrounding systems
(dream cycle, dedup, linking) need auditing. Detailed sub-plan needed at Phase 3.

### 6.4 HA Continuous Audio Capture — SOLVABLE (confidence: 60%)
**Not fully verified.** User confirms a workaround was found during Gatekeeper
development. Needs re-verification during Phase 2 planning. The Assist pipeline
is wake-word-triggered by design, so passive mode likely requires a custom
integration or separate audio daemon.

### 6.5 Unverified Areas (research needed per-phase)
| Area | Blocks | Confidence | Status |
|------|--------|------------|--------|
| Speech-to-speech models | Phase 4 | 45% | Not researched |
| Speaker diarization | Phase 2 | 25% | Not researched |
| VAD library selection | Phase 2 | 50% | Not researched |
| Hot context architecture | Phase 5 | 35% | Theoretical only |
| Gatekeeper freshness | Phase 0b | 40% | Read code, not run |
| Whisper hallucination mitigation | Phase 2 | 50% | Not tested |

Each of these will get dedicated due diligence before its phase begins.

## 7. Build Phases

Each phase has a quality gate. Do not advance until the current phase is
tested and validated.

### Phase 0: Infrastructure Foundation
**Goal:** Two things that everything downstream depends on.

**0a: Genesis HTTP Tool API**
Expose existing MCP tools over HTTP so any client can call them.
Modify `genesis_mcp_server.py` to support `--transport streamable-http`.
Add token auth. Test with curl.
Quality gate: Can call `health_status`, `memory_recall`, `knowledge_recall`
over HTTP and get correct responses.
Effort: Small (hours). Dependencies: None.

**0b: Evaluate & Revive Voice Infrastructure**
1. Bring up Docker containers on VM 200. Update HA OS (2026.1.1 → 2026.5.4).
2. Verify existing reactive voice path works: HA wake word → STT → Gatekeeper
   → OpenRouter → TTS → speaker. This proves the infrastructure is functional.
3. Audit Gatekeeper code for components to extract and reuse vs rewrite.
   The Gatekeeper is a component source, not the target architecture.
4. Deploy SSH key from Genesis container to both VMs for reliable access.
Quality gate: Can say wake word, ask a question, get spoken response.
Understand exactly what the Gatekeeper does and what we want to keep.
Effort: Small-Medium (day). Dependencies: VMs running.

### Phase 1: Genesis-Connected Voice (Reactive)
**Goal:** Skip the Gatekeeper entirely. Genesis exposes an OpenAI-compatible
endpoint. HA sends transcripts directly to Genesis. Genesis answers with full
memory and context. Still reactive (wake word → 45s session), not ambient.
**Architecture (revised from audit):**
- Gatekeeper SKIPPED — Genesis IS the brain, no middleware needed
- `VoiceConversationHandler`: fast path bypassing CC — memory recall + direct
  router call for <5s latency (CC takes 10-60s, unacceptable for voice)
- OpenAI-compatible endpoint at `/v1/voice/chat/completions` (distinct from
  OpenClaw's `/v1/chat/completions`)
- HA's HACS `openai-compatible-conversation` integration sends transcripts
- `VoiceSessionManager`: in-memory 45s sustain timer (from Gatekeeper pattern)
- `VoiceChannelAdapter`: outbound TTS via HA REST API (not registered with
  outreach pipeline — avoids 3am TTS side effects)
- `voice_conversation` call site: groq→gemini→mistral (free, fast)
**Implementation:**
- `src/genesis/channels/voice/` — handler, sessions, adapter
- `src/genesis/dashboard/routes/voice_api.py` — Flask blueprint
- `config/model_routing.yaml` — voice_conversation call site
**Status:** Genesis-side code complete. HA integration pending (HACS setup).
**Quality gate:** Say "Genesis, what did we discuss yesterday?" → Genesis
recalls from memory → speaks answer via HA speaker. Full round trip <5s.
**Dependencies:** Phase 0a + 0b
**Estimated effort:** Medium (days-week)

### Phase 2: Ambient Transcription + Storage
**Goal:** Continuous transcription of ambient audio, stored and archived.
No Genesis processing yet — just capture and store.
**Scope:**
- HA continuous audio capture (mode controller: passive vs active)
- VAD before transcription (Silero VAD or WebRTC VAD)
- On-device Whisper for passive mode transcription (already running on HA)
- Gatekeeper receives continuous transcript chunks (new endpoint)
- Gatekeeper's existing PII redaction cascade filters transcripts
- Raw (redacted) transcript storage on Genesis container
- Speaker diarization (pyannote or simpler approach)
**Quality gate:** 1 hour of ambient transcription manually audited.
Measure: word error rate, hallucination rate, speaker attribution.
Must be >80% accurate on speech segments before advancing.
**Dependencies:** Phase 0b (HA + Gatekeeper running)
**Estimated effort:** Medium (week)
**Research needed:**
- VAD library selection (Silero vs WebRTC VAD)
- HA continuous capture feasibility (may need custom integration)
- Speaker diarization quality + resource requirements
- Whisper model size vs quality tradeoff on HA hardware

### Phase 3: Ambient Memory + Extraction Pipeline
**Goal:** Extract useful intelligence from transcripts and wire into Genesis
memory system. Not just storage — understanding.
**Scope:**
- Ambient memory tier: separate Qdrant collection (`ambient_memory`)
- Ambient SQLite tables: `ambient_transcripts`, `ambient_signals`
- Lower default confidence (0.3-0.5) for ambient memories
- Extraction pipeline: LLM extracts signals from transcript chunks
  (decisions, action items, people, topics, commitments)
- Aggressive decay: unreferenced ambient memories expire
- Promotion gate: ambient → main memory after validation
- `memory_recall` extension: `include_ambient` parameter (opt-in)
- Volume caps: max signals per day to prevent bloat
**Quality gate:** 1 day of extracted signals manually reviewed. Measure:
signal relevance rate >70%, false positive rate <5%, PII leak rate 0%.
**Dependencies:** Phase 2 (transcripts flowing)
**Estimated effort:** Medium-Large (week+)

### Phase 4: Speech-to-Speech Model (Active Mode Upgrade)
**Goal:** Replace the pipeline STT→LLM→TTS path with a speech-to-speech
model for active conversations. Sub-second latency, natural turn-taking,
interruption support.
**Scope:**
- Speech model integration (OpenAI Realtime API or Gemini Live)
- Speech model calls Genesis HTTP Tool API via function calling
- HA mode controller: wake word → route audio to speech model instead
  of pipeline path
- Speech model can converse while Genesis tools process in background
- Fallback to pipeline path when speech model unavailable
**Quality gate:** Conversational latency <500ms for direct responses,
<2s when tool calling is involved. Natural interruption handling.
**Dependencies:** Phase 0a (HTTP API), Phase 1 (Genesis voice channel)
**Research needed (dedicated pass before implementation):**
- Speech model landscape: OpenAI Realtime, Gemini Live, NVIDIA models,
  open-source options
- Function calling support is the key filter
- Latency benchmarks, cost comparison
- WebRTC vs WebSocket for audio streaming
**Estimated effort:** Large (weeks)

### Phase 5: Ambient Attention + Proactive Chiming
**Goal:** Genesis detects notable events in ambient stream and speaks up
when warranted.
**Scope:**
- Hot context window: pre-loaded set of ~20-50 items Genesis currently
  cares about. Refreshed every 5-10 min from memory + goals + tasks.
- Streaming filter: cheap/fast model checks each transcript chunk against
  hot context. Binary: notable or not.
- Escalation path: notable match → full Genesis evaluation (memory recall +
  context) → decision to chime in or stay quiet
- Proactive output: routes through outreach pipeline to voice channel
- Intervention threshold tuning (iterative, needs real-world testing)
**Quality gate:** 1 week of ambient monitoring. Chime-in relevance >80%,
false alarms <2/day, response latency <60s from utterance.
**Dependencies:** Phase 3 (ambient memory), Phase 1 or 4 (voice output)
**Estimated effort:** Large (weeks)

### Phase 6: Advanced Capabilities (Iterative)
**Goal:** Coaching mode, acoustic monitoring, emotional intelligence,
multi-speaker dynamics.
**Scope:** Each capability is a focused extension:
- Active coaching: multi-stream audio + earpiece routing
- Acoustic monitoring: sound classification for non-speech events
  (doorbell, alarms, etc.) — may need specialized models
- Emotional/social intelligence: tone analysis + careful intervention
  judgment for tense situations
- Multi-speaker profiles: voice fingerprinting, per-speaker trust tiers
**Quality gate:** Per-capability, tested independently with user feedback.
**Dependencies:** Phases 0-5 (full infrastructure)
**Estimated effort:** Ongoing (months, iterative)

## 8. Key Dependencies & Research Items

### Must Research Before Implementation
- [ ] Speech-to-speech model landscape: OpenAI Realtime, Gemini Live, NVIDIA
      models, open-source options. Function calling support is the key filter.
      (Blocks Phase 4 only — Phases 0-3 use pipeline STT→LLM→TTS)
- [ ] HA continuous audio capture: can the Assist pipeline stream audio
      continuously, or do we need a custom integration for passive mode?
      (Blocks Phase 2)
- [ ] Speaker diarization: pyannote quality + resource requirements. Where
      should it run — VM 200 or Genesis container? (Blocks Phase 2)
- [ ] VAD library selection: Silero VAD vs WebRTC VAD vs pyannote VAD.
      (Blocks Phase 2)
- [x] Inventory VMs 100 and 200: **DONE.** VM 100 has HA OS with Whisper,
      Piper, openWakeWord running. VM 200 has Gatekeeper stack (stopped).

### Hardware Available
- [x] Voice PE satellite on HA (already configured, entity ID known)
- [ ] Mic array quality assessment (current Voice PE mic vs dedicated mic)
- [ ] Earpiece/earbuds for coaching mode (Bluetooth to phone/HA)
- [ ] NAS access for transcript archival

### External Service Dependencies
- [ ] Speech model API account (OpenAI/Google/other) with realtime access
      (Phase 4 only)
- [x] Groq API (already have — STT cloud fallback)
- [x] TTS providers (already have — ElevenLabs/Cartesia/Fish)
- [x] OpenRouter account (exists on Gatekeeper, useful as fallback)

### VM Access
- [ ] Deploy SSH key from Genesis container to VM 100 (root@192.168.50.99)
      and VM 200 (zorror@192.168.50.148) for reliable automated access.
      Password auth hits rate limits with rapid connections.

## 9. Open Questions

1. **Speech model selection**: Which speech-to-speech model to start with?
   Needs dedicated research pass. Technology maturity is the gating factor.
   This blocks Phase 4 only — Phases 0-3 work without it.

2. **Gatekeeper architecture evolution**: How much of the Gatekeeper do we
   keep vs rewrite? It's a well-built middleware but was designed for a
   different vision. The code audit in Phase 0b will inform this.

3. **HA continuous capture**: The Assist pipeline is designed for wake-word-
   triggered sessions, not continuous streaming. Passive mode may need a
   custom HA integration or a separate audio capture daemon.

4. **Ambient memory decay policy**: How aggressively should unreferenced
   ambient memories expire? Days? Weeks? Needs experimentation.

5. **Multi-user consent model**: When Genesis is listening to conversations
   with other people, what's the framework? Notification? Different handling
   for different relationships?

6. **Cost model**: Speech model sessions are expensive. Budget envelope for
   active voice mode? The Gatekeeper's existing budget management pattern
   (daily caps) is a good starting point.

7. **Compute placement**: Where should speaker diarization and extraction
   LLM calls run? VM 200 has its own Docker stack and Qdrant. Genesis
   container has the memory system. Split or consolidate?

## 10. Success Criteria

**Phase 0-1 success:** "I can ask Genesis a question through my speaker and
get a spoken answer with full memory access."

**Phase 2-3 success:** "Genesis captured that conversation I had yesterday
and I can recall key points from it."

**Phase 4-5 success:** "Genesis spoke up during a conversation to tell me
something I needed to know, without being asked."

**Full vision success:** "Genesis is my cognitive co-pilot throughout the day —
it hears what I hear, remembers what I forget, and speaks up when it matters."

## 11. Relationship to Other Features

- **Feature 3 (Goal-Driven Autonomy)**: Proactive chiming (Mode 3) connects
  to goal-driven behavior. If a goal is "prepare for interview with X," ambient
  awareness of a conversation mentioning X could trigger briefing preparation.
  Follow-up: `8d6ac932` (pinned).

- **Feature 4 (Swappable Agent Backend)**: The HTTP Tool API (Phase 0a) is the
  same infrastructure needed for non-CC agents to access Genesis tools. Building
  it for voice also enables Feature 4.

- **Existing outreach pipeline**: Voice channel plugs into the existing outreach
  architecture. Proactive chiming routes through the same governance, dedup, and
  delivery tracking as Telegram messages.

## 12. Key Files Reference

### Existing Genesis (to integrate with)
- `src/genesis/channels/base.py` — ChannelAdapter ABC
- `src/genesis/channels/voice.py` — VoiceDeliveryHelper (reuse)
- `src/genesis/channels/stt.py` — Groq Whisper STT (reuse for cloud fallback)
- `src/genesis/channels/tts.py` — TTS providers (reuse)
- `src/genesis/channels/bridge.py` — Channel bootstrap pattern
- `src/genesis/mcp/health/__init__.py` — FastMCP tool registration pattern
- `scripts/genesis_mcp_server.py` — MCP server launcher (modify for HTTP)
- `src/genesis/outreach/pipeline.py` — Outreach delivery (integrate voice channel)
- `src/genesis/awareness/loop.py` — Signal collection (ambient signals feed here)

### Existing Gatekeeper (on VM 200, to extract from)
- `/home/zorror/assistant/gatekeeper/app/main.py` — FastAPI app, request routing
- `/home/zorror/assistant/gatekeeper/app/redaction.py` — PII redaction cascade
- `/home/zorror/assistant/gatekeeper/app/sessions.py` — Session management (Redis)
- `/home/zorror/assistant/gatekeeper/app/routing.py` — Salience classification
- `/home/zorror/assistant/gatekeeper/app/home_assistant.py` — HA action execution
- `/home/zorror/assistant/gatekeeper/app/crypto.py` — Transcript encryption
- `/home/zorror/assistant/gatekeeper/app/budget.py` — Cost/request caps
- `/home/zorror/assistant/compose/docker-compose.yml` — Full infrastructure stack
- `/home/zorror/assistant/assistant-foundation-plan.md` — Original v1 design spec
- `/home/zorror/assistant/docs/ha-automations.yaml` — HA wiring patterns

### New (to create)
- `src/genesis/channels/voice/` — Voice channel adapter package
- `src/genesis/ambient/` — Ambient transcription + extraction + attention system
- Ambient memory tier (Qdrant collection + SQLite schema extension)
- HA custom integration for continuous capture (if needed for passive mode)
