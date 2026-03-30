# Claude Code Compatibility Tracking

> **Purpose:** Track Genesis's dependency on Claude Code features, version requirements,
> and update impact. CC is Genesis's intelligence layer — every CC update potentially
> affects Genesis. This document is the reference point when CC updates arrive.
>
> **Process:** When CC updates, consult this document. For each change, check the
> integration surface below and evaluate: Does it affect our wrappers? Unlock something
> we're working around? Obsolete something we built?
>
> Created: 2026-03-09

---

## Current CC Version

**Installed:** Claude Code 2.1.x (as of 2026-03-09)
**Minimum required by Genesis:** Not yet formalized (all current code works with 2.0+)

---

## Integration Surface — Genesis Components That Use CC

| Genesis Component | CC Feature Used | Files | Notes |
|-------------------|----------------|-------|-------|
| CCInvoker | `claude` CLI, `--print`, `-p` flag, `--output-format` | `src/genesis/cc/invoker.py` | Core dispatch mechanism |
| CCReflectionBridge | Background sessions, system prompts | `src/genesis/cc/reflection_bridge.py` | Deep/Strategic reflection dispatch |
| CCSessionManager | Session creation/tracking | `src/genesis/cc/session_manager.py` | Session lifecycle |
| CCCheckpoint | Session pause/resume | `src/genesis/cc/checkpoint.py` | User question handling |
| CCFormatter | Output formatting | `src/genesis/cc/formatter.py` | Response parsing |
| IntentClassifier | N/A (Genesis-internal) | `src/genesis/cc/intent.py` | No CC dependency |

### CC CLI Flags Used by Genesis

| Flag | Used By | Purpose |
|------|---------|---------|
| `--print` | CCInvoker | Non-interactive single-prompt mode |
| `-p` | CCInvoker | Prompt input |
| `--output-format json` | CCInvoker | Structured output parsing |
| `--effort` | CCInvoker (GL-1) | Thinking effort level |
| `--allowedTools` | Planned (Phase 7) | Restrict tool access per session type |
| `--permission-mode` | Planned (Phase 7) | Session permission governance |

---

## CC Features — Usage Status

### Actively Used
- CLI non-interactive mode (`--print`)
- Background session dispatch
- System prompt injection
- Effort levels

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

### Not Yet Evaluated
- **Agent denial doesn't stop** (CC 2.1) — May affect CCInvoker error handling
- **Model language configuration** — Potentially useful for multi-language user support
- **Session moves** (CC 2.1) — Could enable session migration between terminals

---

## CC Update Evaluation Checklist

When a new CC version is released, run through this:

1. **Changelog review:** What changed? New features, breaking changes, deprecations?
2. **Integration surface check:** Does any change affect the components listed above?
3. **Flag/API changes:** Are any CLI flags we use modified or deprecated?
4. **New capabilities:** Does this unlock something we're working around?
5. **Obsolescence check:** Does this make something we built unnecessary?
6. **Skill system changes:** Any changes to skill loading, SKILL.md format, or
   progressive disclosure that affect our Phase 6 skill wiring plans?
7. **Test impact:** Run Genesis CC integration tests after update.
8. **Update this document** with findings.

---

## Version History

| CC Version | Date Evaluated | Genesis Impact | Action Taken |
|------------|---------------|----------------|--------------|
| 2.0 | 2026-03-09 | Scheduled tasks noted, not adopted | Documented in research insights |
| 2.1 | 2026-03-09 | Hooks in frontmatter, forked context, wildcard perms → Phase 7 session_config | Documented, queued for Phase 7 |

---

## Known Risks

### Rebase-Like Risk for CC
CC updates are NOT like AZ rebases — we don't fork CC, we consume it as a tool.
But our wrappers (CCInvoker especially) depend on CLI behavior. If CC changes its
`--print` output format or flag semantics, our wrappers break silently.

**Mitigation:** Integration tests that exercise CCInvoker with real CC CLI calls.
Currently: `scripts/test_cc_cli.sh` (manual). Phase 7+: automated in CI.

### Desktop vs Server Gap
CC's feature roadmap prioritizes desktop app experiences (scheduled tasks, teleport,
cowork). Server-side/CLI features are secondary. Genesis runs on a headless server.
Monitor whether key features become desktop-exclusive.
