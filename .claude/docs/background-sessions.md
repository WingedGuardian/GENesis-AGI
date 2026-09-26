# Background Sessions — Decision Guide

Genesis can run background CC sessions via the `direct_session_run` MCP tool.
Read this guide any time you're considering a background session or sub-agent.

## Background Session vs Sub-agent

| Situation | Use |
|---|---|
| Task > 20 minutes | Background session |
| Needs browser automation with a persistent profile | Background session |
| Quick research returning results to this conversation | Sub-agent |
| Parallel analysis with no memory writes needed | Sub-agent |

**Default heuristic:** If you'd need to resume it later, or if results need to
outlive this conversation → background session. If you just need an answer in
the next few minutes → sub-agent.

> **Note:** the background lane owns a longer CC background-wait ceiling (set to its
> full `timeout_s`), so a dispatched `Workflow` inside a background session runs to
> completion instead of the CLI's default 600s truncation.
>
> **Origin delivery:** pass `deliver_to_origin=True` to `direct_session_run` (from a
> channel/foreground turn) and the session's terminal outcome — success *or* failure
> — is delivered back to the exact conversation it was dispatched from (the DM or
> forum topic). This is how you hand off long work from a channel and actually
> "report back." Without it, a successful background run is silent (only failures
> raise a Telegram alert); poll `direct_session_status` for the output. Delivery is
> framework-owned (the session need not — and for `observe`/`research` cannot — send
> its own report); oversized output is saved under `~/.genesis/output/` and delivered
> as a summary + file pointer.

## Dispatched from a channel? Long work MUST be a background session

When you are a foreground session driving a **Telegram/voice/OpenClaw reply**, your
turn **ends after you respond** — there is no live session left to report back when a
later-finishing task completes. A deep-research `Workflow` (or any 100+-agent fan-out)
run **inline** in such a turn is force-killed by the CLI's headless background-wait
ceiling (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`, ~10 min) with only a partial result,
and nothing delivers it. This silently killed a real Telegram deep-research request on
2026-07-20.

So for a channel-dispatched request needing deep/multi-source research or any
background work likely to exceed a few minutes: **do NOT run it inline — dispatch it
via `direct_session_run` (`profile="research"`, `deliver_to_origin=true`) and reply
that it's running in the background.** The background lane owns a longer wait ceiling
(set to its full `timeout_s`), runs to completion, and — with `deliver_to_origin` —
delivers the finished outcome back to this exact conversation (the delivery model
merged in #1192). Terminal/interactive sessions may still run Workflows inline (you're
present to see them). The foreground system prompt (`conversation._BG_RESEARCH_ROUTING`)
nudges this automatically — but only for channels the delivery model can actually
report back to (**Telegram**, per `origin_delivery_supported`). On channels the
resolver can't address (WEB/OpenClaw, WhatsApp, VOICE) the result would fall back to
the owner surface, so the nudge is withheld rather than promise a report-back that
lands elsewhere.

## Profiles

| Profile | Browser | observation_write | outreach_send | follow_up_create | Web search |
|---|---|---|---|---|---|
| `observe` | ✗ | ✗ | ✗ | ✗ | ✓ |
| `research` | ✗ | ✓ | ✗ | ✓ | ✓ |
| `interact` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `steward` | ✗ | ✓ | ✓ | ✓ | ✓ |

Most profiles block: Bash, Edit, task_submit, settings_update,
direct_session_run, module_call. Use `interact` for workflows that operate
external platforms (publishing, form filling) and need to communicate with the
user. Use `research` for investigation that writes observations/follow-ups;
it also reaches the `genesis-recon` tools, including read-only GitHub.com search and
source inspection of public repositories through a fixed unauthenticated API endpoint.
File reads are limited to 8 MiB. Its shared `web-research` skill and
research MCP configuration are required; dispatch fails clearly if either cannot
be loaded. Every other recon tool is derived from the live registry and denied.
Use `observe` for read-only investigation.

**MCP scoping is secure-by-default.** `CCInvocation.strict_mcp_config` defaults to
True, so every background session gets `--strict-mcp-config`: it loads ONLY the
servers in its generated `--mcp-config` (its `mcp_profile`) and never additively
inherits the operator's user-scoped `~/.claude.json` MCP servers (Claude Code's
`--mcp-config` is additive without strict — probe-verified). A profile that maps to
no genesis servers therefore runs with zero MCP tools (fail-closed), not the
operator's full set. Only human-driven foreground/interactive sessions
(`cc/conversation.py`, `cc/checkpoint.py`) opt out (`strict_mcp_config=False`) to
keep the full user-scoped toolset. As defense-in-depth, `_UNIVERSAL_DISALLOW` also
denies the user-scoped servers by name (`_USER_SCOPED_MCP_WILDCARDS`).

**`steward` is the one built-in Bash-enabled profile** — its Bash is restricted
to the `gh` CLI only, enforced by `scripts/hooks/bash_allowlist_guard.sh`, which
the invoker registers in the `--settings` file it injects into every dispatched
session, reading the `GENESIS_BASH_ALLOWLIST` env var set from
`CCInvocation.bash_allowlist`. That injected registration is the one that
matters: a dispatched session's working directory is outside any git repo, so
Claude Code's git-root settings discovery never loads the repo's
`.claude/settings.json`, and this repo wires the hook there in no ref anyway.
An install may ALSO wire the global chokepoint `scripts/bash_safety_hook.sh` in
its user-level settings; both share one predicate
(`scripts/hooks/bash_allowlist_lib.sh`), so the second copy reaches the same
verdict rather than a different one. The invoker refuses to launch a profile
whose allowlist it cannot arm, including when `bare` or `safe_mode` would
disable hooks, and verifies the registered guard actually refuses and permits
before launching.

It still blocks Edit/Write/browser. Built for the upstream-PR stewardship
campaign: it reads/comments/reopens/closes Genesis's own PRs to external repos
and escalates code-change requests rather than editing or pushing itself. A
profile grants a scoped shell by appearing in `_PROFILE_BASH_ALLOWLIST`
(`src/genesis/cc/direct_session.py`); without an entry there, a Bash-granting
profile's shell is governed only by the global destructive-op blocks. The
allowlist matches the command's **first token** and blocks embedded newlines
plus chaining/piping/substitution/redirection — `; & && || |` backtick `$() > <`
and also `( )`. Two of those are worth knowing about: `&` on its own backgrounds
the first command and RUNS THE NEXT one, so it is not covered by `&&`; and the
parentheses are defence in depth rather than a measured escape — against real
bash a subshell is unreachable in every position the other entries leave open,
so the cost they carry is real (a parenthesis in a `--jq` filter, a search
query or a PR title is refused) while the protection is speculative. If that
trade ever wants revisiting, the enumeration is in
`scripts/hooks/bash_allowlist_lib.sh` and the cost is pinned by tests.

**What first-token allowlisting cannot do on its own**, stated because the one
built-in case is also the one exposed to external content: it bounds WHICH
binary runs, never what that binary can be told to do. An allowlisted binary
that can be configured to run commands hands the session a shell while every
token is still the allowed one — the same limit the overlay-profile note below
records for interpreters.

`gh` is such a binary, so it gets **per-binary hardening** alongside the
allowlist. It will run a program of its own accord through an alias, the pager,
the editor, the browser, or an extension — the set `gh help environment`
documents — and every one of them is reached with `gh` as the first token, so
the allowlist permits both the command that installs an escape and the command
that fires it. An allowlisted session therefore runs with all of them pinned:
`GH_CONFIG_DIR` at a read-only configuration Genesis maintains that carries no
aliases and a pager that is not a shell, the editor and browser variables at an
inert command, and `XDG_DATA_HOME` at that same read-only directory.

**`XDG_DATA_HOME` is the one that is easy to get wrong**, so it is called out
rather than left to the reader: extensions do NOT live under `GH_CONFIG_DIR`.
MEASURED — an extension planted under the config directory was not found, while
one under the data directory ran. Sealing the config directory alone therefore
leaves `gh extension install` followed by `gh extension exec` as arbitrary
execution with both first tokens allowed. Pinning the data directory at the
same read-only seal closes both halves: the install cannot create the directory
it needs, and the exec finds nothing.

`GH_PATH` is deliberately NOT pinned. It tells `gh` where its own binary is for
extension callbacks, and it was measured inert: with a planted value an
ordinary read still ran the real `gh`, and with extensions unreachable it
redirects nothing.

NO CREDENTIAL IS IN THE SEALED DIRECTORY, as of 2026-09-25. It used to hold a
copy of `hosts.yml`, because that is the only place `gh` looks for the token —
owner-only inside an owner-only directory, so the copy widened nobody's read
access. It was removed anyway: the reachability argument was sound and answered
the wrong question, because it handed a session that reads external pull requests
a token with the operator's full scopes (on the install where this was measured:
`delete_repo`, `gist`, `read:org`, `repo`, `workflow` — the list is a property of
that `gh auth login`, not of this code; what generalises is that the session held
whatever the operator held). An ALLOWLISTED session now launches UNAUTHENTICATED
(MEASURED: `gh auth status` under the seal reports "not logged into any GitHub
hosts", against a control that authenticates).

`GH_TOKEN` is pinned to the empty string for EVERY dispatched session, not only
allowlisted ones, because `_build_env` starts from an unfiltered copy of this
process's environment and `gh` resolves `GH_TOKEN` AHEAD of `hosts.yml`
(MEASURED: a bogus token returns 401 against a good `hosts.yml`). An empty value
reads as UNSET, so the pin neutralises an inherited token without inventing one.

**Read this before assuming a non-allowlisted session is unauthenticated — it is
not.** MEASURED: with no `GH_CONFIG_DIR` pin, a session with `GH_TOKEN=""` is
STILL FULLY AUTHENTICATED, because `gh` falls back to `hosts.yml` on disk. The
deciding variable is `GH_CONFIG_DIR`, and it is pinned ONLY on the allowlisted
path. So a dispatched session that has `Bash` without a declared allowlist — the
common case, since the allowlist is set at exactly one call site — still reaches
the operator's GitHub credential.

Pinning `GH_CONFIG_DIR` more widely was tried and rejected, for two reasons worth
keeping. Keying it on `origin == external_untrusted` reads tight and is not:
MEASURED, that origin covers six dispatch profiles and every non-owner-attended
conversation channel, dashboard included, and the pin would also have pointed
`XDG_DATA_HOME` — a process-global base directory — at a read-only tree for all
of them. And pinning the SEAL is a fail-OPEN on exactly the installs that need
it, because a rewrite that fails before its stale sweep leaves a pre-heal seal
still holding the copied credential. The honest fix for that surface is denying
`Bash` on the one path that processes external content: unrestricted `Bash` can
read the config file whatever `GH_CONFIG_DIR` says, so an env pin buys the loss
of AMBIENT authentication and never confinement.

Hardening is keyed by
binary in `_BINARY_HARDENING` (`src/genesis/cc/invoker.py`) and applied in
`_build_env`, which REFUSES to return an environment whose hardening it could
not prepare or that a later override stripped — the environment that was
checked is the environment that launches, because there is only one that both
spawn paths build. A new allowlisted binary that can spawn a shell needs an
entry there, and the allowlist alone should not be read as confining it.

**Still NOT confined — and this is the sentence to read before granting any
scoped shell.** The allowlist is enforced, which is a real improvement over a
restriction nothing applied. It is not a sandbox. The permitted binary writes
files to caller-chosen paths and makes API calls, so `Write`/`Edit` being blocked
describes the TOOLS, not everything that can put bytes on disk or reach the
network. It no longer makes those calls AS THE OPERATOR by default — the
credential is gone from the seal — but an armed profile is given one on purpose,
and the filesystem half is unchanged either way. Two consequences worth stating plainly
rather than leaving to be discovered:

- a scoped session can modify files on this host, INCLUDING files that take
  effect on a later run, so "it can only comment on pull requests" is not a
  property the allowlist gives you;
- the profile that has this grant also ingests external, attacker-authored
  content, so treat its capability as "acts with whatever credential it was
  armed with, plus filesystem write", not as "reads and replies". Unarmed it
  holds no GitHub credential at all; that is the floor, not the confinement.

Bounding this properly needs a SUBCOMMAND-level allowlist rather than a
first-token one. Until that exists, do not write a safety argument that rests
on a scoped shell being unable to do something — `autonomy/audit.py` carried
exactly such an argument and it was wrong.

### Install-local profiles (overlay)

A deployment can register extra profiles — including Bash-scoped ones — without
editing the tracked `direct_session.py`, by adding an optional, gitignored
`genesis/cc/profile_overlay.py` exposing `register(ctx)`. The loader
(`_load_profile_overlays`) is a no-op when that module is absent (the default).
`ctx` is a `ProfileOverlayContext` that hands over the same building-block
disallow lists the built-ins use plus the venv-Python path, and an
`add_profile(name, *, disallow, addendum, bash_allowlist=(), mcp_profile=...,
skills=...)` method. `add_profile` refuses to redefine a built-in profile, so an
overlay can only add. This keeps install-specific session profiles (their names,
prompts, and tool scope) out of the shared repo while the generic mechanism
ships upstream. Note: allowlisting an interpreter (e.g. the venv Python) pins
only the command's first token — `python -c`/`python <file>` still pass — so an
interpreter-scoped overlay profile relies on its addendum for the
behavioural "only run module X" restriction, appropriate only for trusted
(Genesis-internal) sessions, not untrusted input.

## Memory Access Policy

Background sessions have strict memory isolation:

- **Vector store writes (Qdrant) are BLOCKED for ALL profiles.** No background
  session can call `memory_store`, `memory_synthesize`, or `memory_extract`.
  Episodic memory is exclusively for foreground user interactions.
- **Knowledge ingestion is BLOCKED for ALL profiles.** `knowledge_ingest`,
  `knowledge_ingest_batch`, and `knowledge_ingest_source` require explicit user
  authorization in an interactive session.
- **SQLite table writes are profile-gated.** `observation_write`,
  `reference_store`, `procedure_store` are available to research/interact but
  not observe. These write to structured tables, not vector stores.
- **Server-side code is unaffected.** Ego corrections, reflection output, and
  other server-side `MemoryStore.store()` calls bypass tool-level blocking
  because they don't go through MCP.
- **The session output IS the deliverable.** Background session findings belong
  in the final message (session transcript), not in vector stores. The
  foreground user reviews and decides what to persist.

## Key Parameters

- **`timeout_minutes`** — default 15, max 60. Use 60 for long research tasks.
  The clock runs the entire time, including during rate limit waits.
- **`model` / `effort`** — default Sonnet/High. Haiku for cheap bulk tasks.
- **`profile`** — see table above. Choose the minimum profile that covers the task.

## Preserve Partial Progress

The session's terminal output is the research deliverable. For work likely to
exceed one run, ask it to write a bounded artifact under its background-session
directory as it progresses; do not instruct it to use blocked vector-memory
tools. Structured observation/reference writes remain available when the task
specifically calls for those durable records.

## Rate Limits Are Shared

Background sessions share your account's Claude API rate limit with your
foreground session.

- Rate limit hits don't just block the background session — they block you too
- Rate limit wait time counts against `timeout_minutes` — 5 min waiting = 5 min less work
- Sessions that exhaust timeout during a wait fail with a Telegram failure notification
- Memory writes committed before failure are preserved

**Implication:** Don't run heavy background sessions during active foreground
work. Schedule long research sessions for idle periods.

## Failure Recovery

There is no resume path for failed background sessions. If a session fails:
1. Check the session output and any artifact path it reported.
2. Relaunch with the partial output or artifact path and ask it to continue.

Failure modes:
- **Timeout** → Telegram notification + any written artifact preserved
- **Rate limit during wait** → countdown expires → same as timeout
- **Crash** → Telegram notification, same recovery path

## MCP Tool

```
direct_session_run(
    prompt="...",
    profile="research",      # observe | interact | research
    timeout_minutes=60,      # 15 default, up to 60 for long research
    model="sonnet",          # sonnet | opus | haiku | fable
    effort="high",
)
```
