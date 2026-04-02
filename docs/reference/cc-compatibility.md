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
> Created: 2026-03-09 | Last updated: 2026-04-01

---

## Current CC Version

**Installed:** Claude Code 2.1.87 (downgraded 2026-04-01 — v2.1.88-90 scrollback regression on Linux)
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
| `--allowedTools` | Planned (Phase 7) | Restrict tool access per session type |
| `--permission-mode` | Planned (Phase 7) | Session permission governance |

---

## CC Features — Usage Status

### Actively Used
- CLI non-interactive mode (`-p`)
- Background session dispatch with `--dangerously-skip-permissions`
- System prompt injection (`--system-prompt`, `--append-system-prompt`)
- Effort levels (`--effort`)
- Tool blacklisting (`--disallowedTools`)
- MCP config per session (`--mcp-config`)
- Session resume (`--resume`)
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

---

## Known Issues

### v2.1.89+ Scrollback Regression (2026-04-01)

CC v2.1.89 changed default terminal rendering to an alt-screen mode that destroys
terminal scrollback. Confirmed cross-platform regression (GitHub issues #41965,
#41814, #42024, #42002, #42076, #42180). Affects all terminals including tmux.
v2.1.90 does not explicitly fix this but touches fullscreen scrollback code.

**`CLAUDE_CODE_NO_FLICKER=0`:** Platform-dependent. Works on macOS Terminal.app,
confirmed NOT working on Linux or Windows (GitHub #41965 comments). Setting `=1`
enables the alt-screen explicitly and is unlikely to help.

**Workaround:** Downgraded to v2.1.87
(`npm install -g @anthropic-ai/claude-code@2.1.87`). This loses 2.1.88-90
passive bugfixes (prompt cache, memory leak, StructuredOutput, --resume cache,
SSE perf) but restores scrollback. Tested v2.1.90 — scrollback still broken
on Linux despite fullscreen scrollback code changes. v2.1.87 is the last
known-good version for Linux terminal scrollback.

### Install Method: npm Only (2026-04-01)

Removed the standalone installer (`curl -fsSL https://claude.ai/install.sh`)
in favor of npm-only (`npm install -g @anthropic-ai/claude-code`). The standalone
binary auto-updates on every launch, which silently overrode a deliberate
downgrade to v2.1.87 — the running process was always the standalone at v2.1.90
while `claude --version` reported 2.1.87 from the npm copy. npm pinning gives
full version control. Upgrades are deliberate via recon triage.

**Suppressing the native installer nag:** CC shows a status bar warning
("Claude Code has switched from npm to native installer. Run `claude install`")
when running from npm. Set `DISABLE_INSTALLATION_CHECKS=1` to suppress this.
The install script adds this to `~/.bashrc` automatically. Do NOT run
`claude install` — it reinstalls the standalone binary and undoes version pinning.

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
