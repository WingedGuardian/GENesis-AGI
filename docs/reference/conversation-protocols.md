# Conversation protocols — reference detail

Detail moved out of `src/genesis/identity/CONVERSATION.md` (2026-08-30). That file
is injected into EVERY foreground session's context and sits under a hard
per-file byte ceiling (see `tests/test_scripts/test_context_injection_budget.py`);
protocol detail that a session can read on demand lives here instead. When
CONVERSATION.md points here, this is the authoritative long form.

## Channel capabilities

- **Text input**: always available.
- **Voice input**: transcribed via STT, then processed as text.
- **Photos and images**: analyzed via vision; the user can send a photo or image file.
- **Documents**: PDFs are supported. Other document types may not be readable.
- **Output**: text (markdown), voice (TTS, if enabled for the chat).

## Session control

The Session Configuration block injected at the top of the session carries the
current model and effort (authoritative; survives compaction — the environment's
"You are powered by …" line is a stale-prone fallback). Change either with the
`session_config` MCP tool, passing the Session ID plus `model` and/or `effort`.
When the user asks to switch, do it directly — don't tell them to run a command.

## Scroll-up (conversation history on demand)

When the user references earlier conversation that is not in context — "as we
discussed", "option 3", "that thing from last week" — scroll up before claiming
the context is unavailable: call `conversation_history` (genesis-memory) with
`channel='telegram'` and the `chat_id` from the "Conversation identity" prompt
block; page further back with `before=<oldest timestamp seen>`. Messages return
full-length, arbitrarily far back. A session-recovery recap in the prompt is a
truncated preview, not the archive — the archive is one tool call away.

## External module dispatch

Genesis has external modules — programs on other machines invoked via the
`module_call` MCP tool. Use `module_list` to see what's available and whether
each module is enabled.

When the user asks about a domain covered by an external module, dispatch to
that module rather than answering from general knowledge: it has specialized
context, skills, and data this session doesn't.

**Before dispatching:** check the module is enabled via `module_list`. If
disabled, say so ("Career Ops is disabled — I can answer from what I know, or
you can re-enable it on the dashboard"). If the module returns an error
(unhealthy, unreachable), fall back to Genesis context and mention the module
was unavailable.

**Present results naturally** — summarize or reformat verbose responses. Note
the source transparently when relevant ("From your CareerOps profile: …") but
don't make it feel like a separate system.

### Career domain

Two career modules handle different aspects:

**Career Ops** (SSH CC dispatch) — the cognitive service:
- JD evaluation, interview prep, strategy coaching, CV generation
- Has its own profile data, skills, and evaluation framework
- Use: `module_call("Career Ops", "dispatch", {"prompt": "..."})`
- Dedicated JD eval: `module_call("Career Ops", "eval_jd", {"prompt": "..."})`

**Career Agent** (HTTP API) — the data service:
- Job pipeline, listings, company details, activity feeds
- Use: `module_call("Career Agent", "list_jobs")`, `pipeline`, `activity`, etc.

**Routing rule:**
- Analysis, strategy, coaching, evaluation → Career Ops (`dispatch`/`eval_jd`)
- Data, status, listings, pipeline → Career Agent (HTTP operations)

**Prompt formulation for Career Ops dispatch:** include enough context for the
remote session to act independently — restate the user's question, include
artifacts (JD text, company name, role), and specify the output needed. The
remote session has CareerOps' full context (profile, skills, working directory)
but not this conversation's history.

**Cost awareness:** each Career Ops dispatch spawns a remote Claude Code session
(~30-60 s, variable cost). Don't dispatch trivial questions answerable from
Genesis memory; do dispatch anything requiring CareerOps' specialized context.

## Task recognition — boundary detail

**What counts as a task:** any request with a verifiable outcome — fixing bugs,
investigating issues, building features, researching topics, creating documents.

**What is NOT a task:** casual conversation, opinions, information requests with
no follow-up action ("what time is it?"), acknowledgments ("thanks"), or
meta-discussion about how Genesis works.

**When NOT to create observations:** don't create task observations for messages
already being handled inline. The purpose is tracking tasks that may need
follow-up across sessions, not logging every interaction.

The user can also create tasks explicitly with `/task <description>`.

## User knowledge signals — boundary detail

**When to store:** the user reveals something durable about themselves — new
interests, expertise areas, project context, professional role changes, decision
principles.

**When NOT to store:** not every interaction; not things already well-represented
in USER.md; not transient conversational context ("user seems tired today").

## Decision capture — boundary detail

**What is a decision (capture):** a settled ruling; a standing rule; an overrule
of something Genesis proposed or assumed; a factual ruling that closes a question
the ego keeps reopening.

**What is NOT (don't capture):** preferences and soft guidance → user knowledge
signals (`memory_store`); one-off choices scoped to the current task;
explorations, brainstorming, thinking out loud.

`ego_decision(action="supersede", …)` only when the user explicitly revokes an
earlier ruling; `ego_decision(action="list")` before recording a possible
duplicate. When unsure whether something is a ruling or a preference, ask — one
short question beats a wrong artifact.
