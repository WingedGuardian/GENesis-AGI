## What's New in Genesis v3.0b17

175 commits since v3.0b16 (PRs #742–#923).

### Features

**Voice Dashboard**
- **Bridge tab cockpit**: Live edge health panel shows probe state, latency, error rates, and buffer metrics for the ambient voice bridge — the full picture instead of a single status dot.
- **Device vitals panel**: A new Device tab polls your Voice PE hardware (ESP32-S3) via Home Assistant and surfaces temperature, wifi signal, uptime, heap, loop time, and pipeline state.
- **Attention Judgment review**: A new Judgment tab shows exactly what the attention engine decided for each event — L1.5 verdict, category, reason, and reviewer note — with per-device provenance and a regression alert if bridge memory climbs again.

**Models & Intelligence**
- **Claude Fable 5 + full effort range**: Fable 5 is now available everywhere a model is chosen, alongside the full effort range (low → max).
- **`gmodel`**: A new command launches an interactive Genesis session on any roster model without editing config files.
- **Model gauntlet**: New models earn roster membership by passing a real Claude Code fix-loop task. If a model can't drive the system, it doesn't get added.
- **Chorus deliberation**: Invoke a multi-model panel on any question — different models vote, dissent, and produce a joint verdict with the minority view preserved.
- **Non-Anthropic models in Claude Code sessions**: Run Gemini, DeepSeek, and other providers as the engine for Claude Code sessions.
- **Active-model EOL detector**: Genesis watches for deprecation notices on every model it uses (Anthropic and Google) and alerts you before one goes offline.

**Self-Improvement Loop**
- **Weekly cognitive grades in morning report**: Each cognitive subsystem gets a weekly performance grade so you can see whether Genesis is improving or degrading over time.
- **Evo loop**: Genesis can propose improvements to its own reflection prompts, validated against a golden benchmark set. You approve or reject each promotion — nothing is auto-applied.
- **Grade-regression gate**: When a subsystem grade drops, Genesis alerts you and proposes an investigation rather than silently worsening.

**Autonomous Campaigns**
- **Campaigns tab**: A new dashboard tab shows all active autonomous campaigns and lets you steer them — including campaigns where Genesis stewards its own upstream open-source PRs.
- **Upstream PR stewardship**: Genesis can now manage its own contributions to upstream open-source projects — reviewing, addressing feedback, and iterating.

**Procedure Learning**
- **Concrete playbooks**: Genesis now extracts replayable step-by-step playbooks from sessions instead of essay-style summaries. Playbooks are tiered (CORE/ADVISORY/LIBRARY/DORMANT), deduped across types, and surfaced by relevance.
- **Self-learning funnel visibility**: A new health metric shows the full learning pipeline — how many procedures were extracted, surfaced, used, and promoted — honest accounting of what's actually being learned.

**Autonomy & Safety**
- **Sentinel reversibility classifier**: The Sentinel now labels every proposed action as "would auto-run" or "would still ask" before acting — running in shadow/observe mode so verdicts are logged for calibration before enforcement.
- **Guardian Proxmox provisioning**: When host storage has no room to auto-expand, Genesis can request a disk or RAM resize from the hypervisor — gated on your explicit approval.
- **Guardian self-healing SSH key**: Genesis automatically re-hardens its own Guardian SSH key (source-IP binding, no interactive terminal) without requiring manual host access.

**Dashboard & Observability**
- **Follow-ups cockpit tab**: A dedicated tab to see, manage, and drain your full follow-up backlog without scrolling through conversations.
- **Editable backup config**: Edit your backup destination and schedule directly from the dashboard instead of touching config files.
- **Editable repo watchlist**: Add or remove repositories Genesis proactively monitors for discoveries and relevant changes.
- **Model fallback state**: Dashboard and CLI now show when Genesis is running in fallback mode due to a provider outage.
- **GitHub Discovery**: Genesis proactively discovers and ranks relevant open-source repositories, surfacing candidates matching your interests via a daily curated job.
- **Honest system health**: The container health badge factors real CPU/PSI pressure instead of raw memory alone. Per-session Claude Code RSS is tracked with leak alerts. Scheduled jobs that quietly stop succeeding are now detected and surfaced.

### Improvements

- **Code auditor targets AI-generated failure modes**: The idle-time code auditor now specifically hunts patterns common in AI-written code — swallowed async errors, orphaned state, phantom guards, and iteration scars — not just generic code smells.
- **Sentinel knows its own capabilities**: The Sentinel now launches knowing which alarms it can act on and what tools it has, so it only wakes for events it can actually remediate.
- **Backup retention bounded**: Off-site dated snapshots are pruned on a GFS schedule (daily/weekly/monthly) instead of accumulating indefinitely. Host-side stale snapshots are age-pruned to prevent storage exhaustion.
- **Daily disk hygiene**: Now includes label-aware snapshot GC and `~/tmp` age-pruning to prevent disk pressure from building silently.
- **Dream-cycle split**: Heavy weekly memory clustering and lighter daily consolidation now run on separate schedules, reducing interference and resource spikes.
- **Free DeepSeek-Pro preferred**: Routing automatically prefers the free NIM deepseek-v4-pro tier on DeepSeek chains.
- **Morning report leads with next steps**: What needs your attention appears first, not a retrospective of what ran overnight.
- **External content boundary-wrapped**: Content from the web or knowledge base is labeled at the point it enters recall and injection, keeping provenance visible throughout.

### Bug Fixes

- **Container freeze from code indexing**: Creating a git worktree or running install/bootstrap could trigger multiple simultaneous indexing jobs, freezing the machine. All code-intel index spawns now route through one locked, capped entrypoint.
- **Inbox approval storm**: Two parked files were leapfrogging each other in a cancel-and-recreate cycle every 30 minutes, generating dozens of spurious approval nags to Telegram. Fixed.
- **Host-VM sync silently dead**: `update.sh`'s host-VM sync was broken by a refactor. Guardian now reconciles deploys against the actual host commit and Claude Code stays on its pinned version instead of drifting.
- **Guardian recovery had no restore point**: The healthy-snapshot lifeline was never being created on deploys, so rollback recovery had nothing to fall back to. Fixed — snapshots are now created and pruned correctly, and stopped containers are recovered.
- **codebase-memory-mcp memory leak contained**: A known upstream memory leak in the code-intelligence server is now hard-capped at 2 GB. When the cap is hit, the server restarts automatically to prevent container OOM.
- **Memory recall surfaced internal noise**: Genesis's own reflection observations, task-executor retrospectives, and ego bookkeeping were leaking into user-visible recall results. Fixed with vector cleanup and subsystem filtering.
- **Knowledge intake pipeline**: JSON findings wrapped in code fences — and bare-array findings — now parse correctly into the knowledge base instead of being stored as raw JSON strings. Deleting a knowledge item works end-to-end.
- **Backup scheduling was never wired**: Fresh installs had no scheduled backups — the timer was rendered but never enabled. Bootstrap now enables it and it appears in systemd as expected.
- **Stale Claude Code shadow copies**: Old CC binaries (from nvm or native installer) were silently winning PATH priority over the pinned version. Genesis now detects and corrects this at startup.
- **Inbox dispatch at-most-once**: Inbox items could be evaluated twice on crash/restart. Dispatch now claims items via row-state lock before processing.
- **Inbox phantom re-detection**: URLs already evaluated could repeatedly re-trigger evaluations. Fixed with deterministic known-hash tracking.
- **Inbox partial-failure auto-retry**: Batches with partial failures are auto-retried on next startup; items that can never succeed are abandoned cleanly.
- **COO reactive-waste eliminated**: ~92% of Genesis (COO) ego cycles were empty — triggered by provider-exhaustion events the COO had no way to handle. These events are now filtered at source.
- **Ego first-fire after restart**: The ego cycle silently skipped its first scheduled run after a restart due to `IntervalTrigger` behavior. Fixed by switching to `CronTrigger`.
- **Reflection and surplus JSON parsing**: Both surfaces now fence-strip model output before `json.loads`, resolving silent parse failures when models wrap output in code fences.
- **Knowledge re-ingest re-distills**: Re-ingesting a file now detects content changes via hash and re-processes only what changed, rather than silently skipping the whole file.
- **Self-send follow-up loop**: A bug caused Genesis to email itself repeatedly as a follow-up target. Fixed.
- **Guardian drift watchdog false alarms**: Was testing for exact string equality instead of containment, causing false-positive drift alerts.
- **Multiple false health alarms**: False backlog/overdue/dark alarms, DLQ false positives, and incorrect per-ego cycle health are all corrected.

### Infrastructure

- **aiohttp raised past 11 CVEs** — floor raised to >=3.14.1.
- **pip-audit CVE gate in CI** — new CI step blocks PRs that introduce known dependency vulnerabilities.
- **CC↔Node pin enforced at PR time** — CI rejects PRs that bump the Claude Code pin past its Node floor without also bumping `NODE_MAJOR`.
- **Contribution sanitizer blocks Tailscale range** — full CGNAT + IPv6 range now blocked at the sanitizer floor.
- **GitHub Actions dependencies updated** — `actions/checkout` and `actions/setup-python` bumped.
