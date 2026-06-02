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
> Created: 2026-03-09 | Last updated: 2026-06-01

---

## Current CC Version

**Installed:** Claude Code 2.1.159 (upgraded 2026-06-01 from 2.1.138 — enables Opus 4.8)
**Pin in scripts:** `CC_VERSION=2.1.159` in `scripts/install.sh` and `scripts/host-setup.sh`
**Minimum required by Genesis:** Not yet formalized (all current code works with 2.0+)

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
| 2.1.91–2.1.118 | — | Not individually tracked; no Genesis-breaking changes | System ran on 2.1.138 without known issues |
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
| 2.1.159 | 2026-06-01 | Current `latest` tag; Opus 4.8 stable | **Upgraded to this version.** All 8 Genesis integration tests passed. |

---

## Known Issues

### v2.1.89+ Scrollback Regression — RESOLVED (2026-06-01)

CC v2.1.89 changed default terminal rendering to an alt-screen mode that destroys
terminal scrollback on Linux/tmux. Confirmed cross-platform regression (GitHub issues
#41965, #41814, #42024, #42002, #42076, #42180).

**Resolution:** CC 2.1.132 added `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`. This env
var was added to `settings.json` and `CCInvoker._build_env()` in PR #479, which
unblocked the version upgrade. Now running 2.1.159 with scrollback fully functional.

**History:** Downgraded to v2.1.87 on 2026-04-01; ran on 2.1.138 (auto-updated before
update controls were in place); upgraded to 2.1.159 on 2026-06-01.

### Install Method: npm Only (2026-04-01)

Removed the standalone installer (`curl -fsSL https://claude.ai/install.sh`)
in favor of npm-only (`npm install -g @anthropic-ai/claude-code`). The standalone
binary auto-updates on every launch, which silently overrode version pinning.
npm pinning gives full version control. Upgrades are deliberate via recon triage.

**Suppressing the native installer nag:** CC shows a status bar warning when running
from npm. Set `DISABLE_INSTALLATION_CHECKS=1` to suppress this. The install script
adds this to `~/.bashrc` automatically. Do NOT run `claude install`.

**Dual npm prefix gotcha (2026-06-01):** This system has two npm prefix locations
(`/usr/local` and `/usr`). `which claude` resolves to `/usr/local/bin/claude`, so
installs must explicitly target that prefix:
```bash
sudo npm install -g --prefix /usr/local @anthropic-ai/claude-code@<version>
```
Running `sudo npm install -g` without `--prefix /usr/local` lands in `/usr/lib/` and
does not update the `claude` binary on PATH.

### Cache Inflation (since ~2.1.100, accepted)

CC sends ~20K extra `cache_creation` tokens per payload by design. This is accepted
overhead — upstream has chosen not to fix it. No action taken.

### Cost Tracking Correction (2.1.152)

`cache_creation_input_tokens` was silently reporting 0 before 2.1.152. After upgrade,
dashboard cost numbers will appear higher. This is a correctness fix, not a regression —
actual costs were always being incurred, just not displayed.

### Opus 4.8 Lean System Prompt (2.1.154+)

Opus 4.8 uses a leaner default CC system prompt. Genesis uses `--append-system-prompt`
for all background sessions (ego, reflection, conversation, direct sessions). Verified
2026-06-01: `--append-system-prompt` works correctly with Opus 4.8 — injected content
is respected. No action needed.

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
