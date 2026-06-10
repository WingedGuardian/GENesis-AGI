# Claude Code Compatibility Tracking

> **Purpose:** Track Genesis's dependency on Claude Code features, version requirements,
> and update impact. CC is Genesis's intelligence layer — every CC update potentially
> affects Genesis. This document is the reference point when CC updates arrive.
>
> **Process:** When CC updates, consult this document. The recon subsystem
> (`cc_update_analyzer.py`) auto-detects version changes and classifies impact
> through 8 evaluation lenses (see analyzer prompt for details). This document
> is updated manually after each evaluation.
>
> Created: 2026-03-09 | Last updated: 2026-06-10

---

## Current CC Version

**Installed:** Claude Code 2.1.170 — **container + host VM** (both upgraded 2026-06-10 from 2.1.160 — adds Fable 5 access + inherited-env transcript fix). Container via npm (`--prefix /usr/local`); host via native installer (`claude install 2.1.170`).
**Pin in scripts:** `CC_VERSION=2.1.170` in `scripts/install.sh` and `scripts/host-setup.sh`
**Minimum required by Genesis:** Not yet formalized (all current code works with 2.0+).
`requiredMinimumVersion`/`requiredMaximumVersion` (2.1.163) were evaluated as a way
to enforce a floor, but they are **managed-settings-only** (read only from
`/etc/claude-code/managed-settings.json` on Linux — never from user/project
`settings.json`). Formalizing a floor would require a system-level managed-settings
file + install-script automation; deferred as a separate follow-up.

---

## Integration Surface — Genesis Components That Use CC

| Genesis Component | CC Feature Used | Files | Notes |
|-------------------|----------------|-------|-------|
| CCInvoker | `claude` CLI, `-p` flag, `--output-format` | `src/genesis/cc/invoker.py` | Core dispatch mechanism |
| CCReflectionBridge | Background sessions, system prompts | `src/genesis/cc/reflection_bridge.py` | Deep/Strategic reflection dispatch |
| CCSessionManager | Session creation/tracking | `src/genesis/cc/session_manager.py` | Session lifecycle |
| CCCheckpoint | Session pause/resume | `src/genesis/cc/checkpoint.py` | User question handling |
| CCFormatter | Output formatting | `src/genesis/cc/formatter.py` | Response parsing |
| IntentClassifier | N/A (Genesis-internal) | `src/genesis/cc/intent.py` | No CC dependency |
| Guardian Diagnosis | `-p`, `--model opus`, `--max-turns 50`, `--dangerously-skip-permissions`, `--output-format json` | `src/genesis/guardian/diagnosis.py` | Agentic diagnosis + recovery on host VM. Highest-stakes CC call in system. |

### CC CLI Flags Used by Genesis

| Flag | Used By | Purpose |
|------|---------|---------|
| `-p` | CCInvoker | Prompt input (non-interactive single-prompt mode) |
| `--output-format json` | CCInvoker | Structured output parsing |
| `--model` | CCInvoker | Model selection per session |
| `--effort` | CCInvoker | Thinking effort level |
| `--system-prompt` / `--append-system-prompt` | CCInvoker | System prompt injection |
| `--dangerously-skip-permissions` | CCInvoker | Background session permission bypass |
| `--disallowedTools` | CCInvoker | Tool blacklist for scoped sessions (inbox, mail) |
| `--mcp-config` | CCInvoker | MCP server configuration per session |
| `--resume` | CCCheckpoint | Session pause/resume |
| `--allowedTools` | CCInvoker | Tool whitelist for scoped sessions |
| `--bare` | CCInvoker | Minimal UI mode for background sessions |
| `--max-turns` | Guardian Diagnosis | Turn limit (Guardian uses config-driven value) |
| `--strict-mcp-config` | Guardian Diagnosis | Prevent global MCP config loading in diagnosis |
| `--permission-mode` | Planned | Session permission governance |

---

## CC Features — Usage Status

### Actively Used
- CLI non-interactive mode (`-p`)
- Background session dispatch with `--dangerously-skip-permissions`
- System prompt injection (`--system-prompt`, `--append-system-prompt`) — all background session paths use `--append-system-prompt`
- Effort levels (`--effort`)
- Tool blacklisting (`--disallowedTools`) and whitelisting (`--allowedTools`)
- MCP config per session (`--mcp-config`)
- Session resume (`--resume`)
- Bare mode (`--bare`)
- Turn limits (`--max-turns`) and strict MCP config (`--strict-mcp-config`) for Guardian diagnosis
- PreToolUse / PostToolUse / SessionStart / Stop / UserPromptSubmit hooks

### Planned to Use (Phase 6-7)
- **Skills system** — Load Genesis skills into CC background sessions
- **Hooks in frontmatter** (CC 2.1) — Per-session hook configuration via session_config.py
- **Forked skill context** (CC 2.1) — Skill isolation in background sessions
- **Wildcard permissions** (CC 2.1) — `Bash(*-h*)` style permission patterns
- **Hot reload** (CC 2.1) — Skill updates without session restart

### Evaluated — Not Using
- **Scheduled tasks** (CC 2.0) — Genesis uses APScheduler instead. CC scheduled tasks
  are desktop-only and less sophisticated than our depth-classified awareness loop.
  Re-evaluate if CC adds server-side scheduled tasks.
- **`/teleport` to claude.ai/code** — Not relevant for server-side Genesis.
- **Shift+Enter for newlines** — UX feature, no Genesis impact.
- **`MCP_CONNECTION_NONBLOCKING=true`** (CC 2.1.89) — Skips MCP connection wait
  entirely in `-p` mode. Too aggressive for Genesis — most background sessions
  need MCP tools early (ego queries health, reflection uses memory). The automatic
  5s connection bound (also 2.1.89) provides sufficient timeout safety without
  opt-in. Revisit if background sessions show >5s MCP hangs.

### Not Yet Evaluated
- **`defer` permission decision** (CC 2.1.89) — PreToolUse hooks can return
  `"defer"` to pause headless sessions at a tool call. Session resumes via
  `--resume` with hook re-evaluation. Potential building block for earned
  autonomy pipeline: hook returns `defer` on high-risk operations, Guardian
  approves, session resumes. Evaluate when autonomy work (gap closure roadmap)
  progresses.
- **`PermissionDenied` hook** (CC 2.1.89) — Fires after auto mode classifier
  denials. Return `{retry: true}` to tell the model it can retry. Not relevant
  while background sessions use `--dangerously-skip-permissions`. Evaluate if
  we move to auto mode + hooks.
- **Agent denial doesn't stop** (CC 2.1) — May affect CCInvoker error handling
- **Model language configuration** — Potentially useful for multi-language user support
- **Session moves** (CC 2.1) — Could enable session migration between terminals

---

## CC Update Evaluation Checklist

When a new CC version is released, run through this:

1. **Changelog review:** What changed? New features, breaking changes, deprecations?
2. **8-lens evaluation:** Check each change against: programmatic integration,
   hooks/permissions, MCP/tools, interactive CLI experience, performance/stability,
   security/trust, platform/environment, model/API. (See `_ANALYSIS_PROMPT` in
   `src/genesis/recon/cc_update_analyzer.py` for the full lens definitions.)
3. **Flag/API changes:** Are any CLI flags we use modified or deprecated?
4. **New capabilities:** Does this unlock something we're working around?
5. **Obsolescence check:** Does this make something we built unnecessary?
6. **Interactive UX check:** Does this change the foreground session experience?
   Rendering, scrollback, terminal behavior, keyboard shortcuts?
7. **Test impact:** Run Genesis CC integration tests after update.
8. **Update this document** with findings.

---

## Version History

| CC Version | Date Evaluated | Genesis Impact | Action Taken |
|------------|---------------|----------------|--------------|
| 2.0 | 2026-03-09 | Scheduled tasks noted, not adopted | Documented in research insights |
| 2.1 | 2026-03-09 | Hooks in frontmatter, forked context, wildcard perms | Queued for Phase 7 |
| 2.1.83 | 2026-03-25 | Changelog not available | Recorded in recon findings |
| 2.1.84 | 2026-03-26 | Changelog not available | Recorded in recon findings |
| 2.1.85 | 2026-03-26 | Conditional `if` for hooks, MCP env vars, OAuth improvements | Documented, no action needed |
| 2.1.86 | 2026-03-27 | Session ID header, VCS exclusions, `--resume` fix, `--bare` MCP fix | Documented, no action needed |
| 2.1.87 | 2026-03-29 | Cowork Dispatch fix | Documented, no action needed |
| 2.1.88 | 2026-03-30 | `PermissionDenied` hook, named subagents, prompt cache fix, memory leak fixes, OOM fix for large Edit | Documented, no immediate action |
| 2.1.89 | 2026-04-01 | `defer` permission, `MCP_CONNECTION_NONBLOCKING`, 5s MCP bound, scrollback regression | Analyzer prompt broadened, scrollback workaround applied, `defer` documented for future use |
| 2.1.90 | 2026-04-01 | `--resume` cache miss fix, hook exit-code-2 fix, Edit/Write format-on-save fix, SSE+transcript O(n^2)→O(n) perf | Upgraded, tested — scrollback still broken on Linux. Downgraded to 2.1.87. |
| 2.1.91–2.1.118 | — | Range not individually tracked; no Genesis-breaking changes identified in changelog review | Pending changelog backfill via cc_update_analyzer (follow-up) |
| 2.1.119 | — | PostToolUse/PostToolUseFailure hooks gain `duration_ms` field | Additive — no action needed |
| 2.1.126 | — | `--dangerously-skip-permissions` extended to bypass `.claude/`, `.git/`, `.vscode/` writes | Additive — background sessions already use this flag |
| 2.1.128 | — | `MCP: workspace` is a reserved server name | No Genesis conflict |
| 2.1.132 | — | `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` env var lands — **scrollback fix** | Added to `CCInvoker._build_env()` and `settings.json` in PR #479 |
| 2.1.133 | — | All hooks gain `effort.level` JSON field + `$CLAUDE_EFFORT` env var | Additive — Genesis hooks only read fields they need |
| 2.1.138 | — | Running version before 2026-06-01 upgrade | Proven stable in production |
| 2.1.139 | — | Hooks run WITHOUT terminal access — terminal I/O silently suppressed | Safe — Genesis hooks only use stderr for logging |
| 2.1.143 | — | Stop hooks that block cap at 8 consecutive blocks | Safe — `genesis_stop_hook.py` never returns exit 2 |
| 2.1.150 | — | npm `stable` tag | Noted |
| 2.1.152 | — | `cache_creation_input_tokens` reporting bug fixed (was silently 0) | Dashboard cost numbers will appear higher — this is a correctness fix, not a regression |
| 2.1.153 | — | `/model` saves selection as default for new sessions | No background session impact (`--model` flag overrides) |
| 2.1.154 | — | Opus 4.8 support; lean system prompt default for Opus 4.8+ | Verified: `--append-system-prompt` works correctly with Opus 4.8 (tested 2026-06-01) |
| 2.1.156 | — | Fix thinking-block corruption bug for Opus 4.8 on session resume | Minimum safe version for Opus 4.8 extended thinking |
| 2.1.157 | — | Fix tmux copy-on-select regression | Relevant for Linux/tmux users |
| 2.1.159 | 2026-06-01 | Opus 4.8 stable | Tested — all 8 integration tests passed; CCInvoker E2E verified |
| 2.1.160 | 2026-06-01 | Current `latest` tag (promoted from `next` mid-upgrade) | **Upgraded to this version.** Auto-updater bumped from 2.1.159 in flight. Re-verified E2E via CCInvoker for Sonnet and Opus 4.8. |
| 2.1.161 | 2026-06-10 | Fixes `--output-format json`/`text` stdout corruption from background subagents; parallel-tool failures no longer cancel sibling calls; background sessions no longer boot a stale model from daemon env; `claude mcp` secret redaction | **Benefits Genesis** (json output + per-session `--model` pinning). No action. |
| 2.1.162 | 2026-06-10 | `claude agents --json waitingFor`; WebFetch permission precedence for preapproved domains; MCP sub-1000 ms timeout fix; read-only config-dir hang fix | Additive/fixes. Genesis blocks WebFetch via a hook, not `WebFetch()` rules — no impact. |
| 2.1.163 | 2026-06-10 | `requiredMinimumVersion`/`requiredMaximumVersion` (**managed-settings only**); `/plugin list`; Stop/SubagentStop hooks gain `additionalContext`; hook `if:"Bash(...)"` now matches inside subshells/backticks | Version floor noted (managed-only — deferred, see Current CC Version). Genesis hooks use tool-name `matcher`s, not `if:` command conditions — `if:` change does not apply. |
| 2.1.165, 2.1.167–2.1.168 | — | Bug fixes and reliability improvements | No action |
| 2.1.166 | 2026-06-10 | `fallbackModel` (up to 3 fallbacks); glob patterns in deny tool-name position; hardened cross-session `SendMessage` authority; thinking-disable on think-by-default models | `fallbackModel` deliberately **not** adopted — silent auto-degrade conflicts with "quality over cost." Glob deny redundant with `bash_safety_hook.sh`. |
| 2.1.169 | 2026-06-10 | `--safe-mode`/`CLAUDE_CODE_SAFE_MODE`; `/cd`; `disableBundledSkills`; `--mcp-config` + managed-MCP enforcement fixes; background sessions preserve `--bare`/`--ide` across retire→wake; project-env (`ANTHROPIC_MODEL`) honored on pre-warmed workers | Fixes touch flags Genesis uses (`--mcp-config`, `--bare`). Smoke-tested post-upgrade. No config change. |
| 2.1.170 | 2026-06-10 | **Claude Fable 5 (Mythos-class)** model access; fixed sessions not saving transcripts (and missing from `--resume`) when launched from a shell that inherited CC env vars | **Upgraded to this version.** Fable 5 → separate eval follow-up (background sessions pin `--model`, so no leak). Transcript fix benefits Genesis background sessions (inherited-env spawn path). |

---

## Known Issues

### v2.1.89+ Scrollback Regression — RESOLVED (2026-06-01)

CC v2.1.89 changed default terminal rendering to an alt-screen mode that destroys
terminal scrollback on Linux/tmux. Confirmed cross-platform regression (GitHub issues
#41965, #41814, #42024, #42002, #42076, #42180).

**Resolution:** CC 2.1.132 added `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`. This env
var was added to `settings.json` and `CCInvoker._build_env()` in PR #479, which
unblocked the version upgrade. Now running 2.1.160 with scrollback fully functional.

**History:** Downgraded to v2.1.87 on 2026-04-01; ran on 2.1.138 (auto-updated before
update controls were in place); upgraded to 2.1.159 on 2026-06-01; auto-updater bumped
to 2.1.160 mid-session.

### Install Method: npm Only (2026-04-01)

Removed the standalone installer (`curl -fsSL https://claude.ai/install.sh`)
in favor of npm-only (`npm install -g @anthropic-ai/claude-code`). The standalone
binary auto-updates on every launch, which silently overrode version pinning.
npm pinning gives full version control. Upgrades are deliberate via recon triage.

**Suppressing the native installer nag:** CC shows a status bar warning when running
from npm. Set `DISABLE_INSTALLATION_CHECKS=1` to suppress this. The install script
adds this to `~/.bashrc` automatically. Do NOT run `claude install`.

**Dual npm prefix gotcha (2026-06-01, revised 2026-06-10):** Containers may have
multiple npm prefix locations. The install script now auto-detects which prefix PATH
resolves by checking `npm config get prefix`:
- **User-level prefix** (e.g. `~/.npm-global`): installs without sudo or `--prefix`,
  so `which claude` finds the new binary directly.
- **System-level prefix** (`/usr/local` or `/usr`): installs with `sudo --prefix /usr/local`
  to avoid the `/usr/lib` misrouting issue.

For manual upgrades, use plain `npm install -g` (no `--prefix`) — it installs to
your configured prefix, which is what PATH finds:
```bash
npm install -g @anthropic-ai/claude-code@<version>
```
If your prefix requires root (`/usr/local`), add `sudo --prefix /usr/local`.

**Host VM variation (verified 2026-06-10):** The host VM uses CC's **native
installer** — NOT npm. Versioned binaries live in `~/.local/share/claude/versions/`
with `~/.local/bin/claude` a symlink to the active version. **Do NOT `npm install`
on the host** — npm and the native installer conflict. Update to a specific version
with the native installer's own command (use the full path, since `~/.local/bin` is
usually not on the host's login/SSH PATH):
```bash
"$HOME/.local/bin/claude" install <version>   # e.g. 2.1.170 — repoints the symlink, keeps old versions for rollback
"$HOME/.local/bin/claude" --version           # verify
```
Rollback is just `claude install <old-version>` (prior versions remain in
`versions/`). `DISABLE_AUTOUPDATER=1` is set on the host, so updates are manual.

**Host CC reachability — important:** The host's login/SSH shell PATH does NOT
include `~/.local/bin`, so a bare `claude --version` over SSH (and the Guardian
gateway's `version` op) reports `"unavailable"` even when CC is installed and fine.
What matters is that the **`genesis-guardian.service` systemd unit explicitly sets
`PATH=$HOME/.local/bin:...`**, so Guardian diagnosis (`cc.path: "claude"`)
resolves the binary correctly. Verify the real state with:
```bash
env -i PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" claude --version
```
The host CC binary is managed separately from the Genesis→Guardian *code* sync
(`redeploy`/`update` gateway verbs sync code only, never the CC binary).

### Auto-Updater Suppression Requires User-Level Settings

**Critical gotcha discovered 2026-06-01:** `DISABLE_AUTOUPDATER=1` and
`DISABLE_UPDATES=1` in the **repo's** `.claude/settings.json` are NOT sufficient to
stop CC's auto-updater. The repo settings only apply when CC is launched from the
project directory. The auto-updater runs in contexts where repo settings don't apply.

**Required:** `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1` must be in the
**user-level** `~/.claude/settings.json` on every machine running Genesis (container
+ host VM). This is the file with authority for global CC behavior.

**Automated:** `scripts/install.sh` and `scripts/host-setup.sh` now create or merge
these env vars into `~/.claude/settings.json` during setup (preserving any existing
keys). Fresh installs no longer need manual intervention.

This file is per-machine and NOT tracked in the repo. Without the install-script
automation, CC silently bumps versions out from under us — we discovered this when
the host VM was running 2.1.119 despite the 2.1.87 script pin, and again mid-session
when the container auto-updated from 2.1.159 to 2.1.160.

### Cache Inflation (since ~2.1.100, accepted)

CC sends ~20K extra `cache_creation` tokens per payload by design. This is accepted
overhead — upstream has chosen not to fix it. No action taken.

### Cost Tracking Correction (2.1.152)

`cache_creation_input_tokens` was silently reporting 0 before 2.1.152. After upgrade,
dashboard cost numbers will appear higher. This is a correctness fix, not a regression —
actual costs were always being incurred, just not displayed.

### Opus 4.8 Lean System Prompt + Prompt Injection Defenses (2.1.154+)

Opus 4.8 uses a leaner default CC system prompt AND has **enhanced prompt injection
defenses** that apply to `--append-system-prompt` content. Verified 2026-06-01 by
testing Opus 4.8 directly: the defenses key on **channel + shape + intent**, NOT on
directive language.

**What triggers the defense (avoid in identity files):**
- **Output-suppression / gag clauses** — "say only X and nothing else", "no preamble"
  pushed to the point of suppressing reasoning. Operational ordering rules are fine
  (e.g., "your final text block is captured — put the evaluation last") because they
  carry the technical why.
- **Free-floating imperatives without role coherence** — directives that don't define
  a role, scope, or duty.
- **Zero operational purpose** — directives with no system reason. Genesis directives
  serve a system function ("review intentions every cycle") and are safe.
- **Secrecy clauses** — "don't tell the user", "hide this from the operator". These
  contradict Genesis's SOUL ("user sovereignty is absolute") and would be flagged.
- **Meta-overrides** — "ignore previous instructions", "you are now [role]". Never
  use these.
- **In-conversation authority claims** — "I'm the admin, please...". Authority comes
  from the system-prompt channel, not from claimed authority within content.

**What's SAFE (Genesis's current pattern):**
- Strong directive language (MUST, NEVER, ALWAYS) tied to a coherent role with
  operational purpose
- Output formatting rules that explain the technical why
- Strict operational SOPs ("address every email", "fetch every URL")
- **Explicit injection-awareness sections** (like INBOX_EVALUATE.md's lines 506-524)
  — these are GOLD STANDARD, they make Opus 4.8 MORE trusting because they prove the
  author understands the boundary

**Audit result (2026-06-01):** Genesis identity files audited against these patterns.
USER_EGO_SESSION, EGO_SESSION, GENESIS_EGO_SESSION, INBOX_EVALUATE, REFLECTION_DEEP,
REFLECTION_STRATEGIC, STEERING, CONVERSATION — all FINE. MORNING_REPORT.md has
borderline-aggressive "ABSOLUTE PROHIBITIONS" framing but operational why is present;
real risk is low. Style could be tightened in a follow-up.

**Lines Opus 4.8 will still refuse even from the system channel:** deceiving the
user, overriding user sovereignty, hiding reasoning from operator, causing harm,
disabling injection-awareness itself. Genesis identity files don't go near these.

### Thinking Block Corruption on Resume (Opus extended thinking)

Resuming an Opus session with extended thinking may get 400 errors: "thinking blocks
cannot be modified." Classified as `CCSessionError` (PR #479). `conversation.py`
already catches `CCError` on resumes and recovers via `_recover_stale_resume()`.
Mitigated, not eliminated.

---

## Known Risks

### Rebase-Like Risk for CC
CC updates are NOT like AZ rebases — we don't fork CC, we consume it as a tool.
But our wrappers (CCInvoker especially) depend on CLI behavior. If CC changes its
output format or flag semantics, our wrappers break silently.

**Mitigation:** Integration tests that exercise CCInvoker with real CC CLI calls.
Currently: `scripts/test_cc_cli.sh` (manual). Phase 7+: automated in CI.

### Desktop vs Server Gap
CC's feature roadmap prioritizes desktop app experiences (scheduled tasks, teleport,
cowork). Server-side/CLI features are secondary. Genesis runs on a headless server.
Monitor whether key features become desktop-exclusive. The v2.1.89 scrollback
regression is an example — rendering optimizations designed for desktop apps
degrading the headless/tmux experience.
