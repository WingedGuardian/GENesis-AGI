# Changelog

All notable changes to Genesis are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows Genesis release stages (v3.0a → v3.1 → v4.0a…).

---

## [3.0a2-hf2]

### Added

- **Install UX overhaul** — welcome/recovery banners, contextual CC login
  prompts (explains Genesis vs Guardian purpose), `genesis` shell alias for
  convenient container access from host
- **Dashboard accessibility** — Incus proxy device forwarding host:5000 →
  container:5000, network topology detection (IPv4/IPv6/Tailscale), SSH
  tunnel and Tailscale guidance in post-install report
- **Network identity** — container and host IPs (v4 + v6) persisted in
  CLAUDE.md for both Genesis and Guardian; guardian-gateway appends network
  section on code updates
- **Guardian onboarding** — interactive CC login prompt during install,
  network section in Guardian CLAUDE.md
- **Uninstall script** — `scripts/uninstall.sh` for clean removal

### Fixed

- **Services not starting after install** — `genesis-server` was enabled but
  never started; service gate blocked enable/start on re-runs. Now
  unconditionally enables and starts both services
- **Dashboard unreachable from browser** — container IP not routable from
  external network; proxy device now forwards host port
- **`/setup` not found on new installs** — CC discovers slash commands from
  project root; users landing in `~` couldn't find `.claude/commands/`.
  Auto-cd to `~/genesis` on login fixes this
- **Install final output** — removed stale "start services manually" step
  (services auto-start now), shows actual service status, simplified guidance
- **Guardian stuck in CONFIRMED_DEAD** — state machine never checked if
  signals recovered; container could be perfectly healthy while Guardian
  reported it as dead indefinitely. Now auto-recovers when all signals
  return to healthy
- **Neural monitor false green for unconfigured providers** — health probe
  hit unauthenticated `/models` endpoint for providers with `base_url` but
  no API key (e.g., GLM5/Zenmux), getting HTTP 200 and reporting "reachable"
- **CC auto-updater nag** — disabled for pinned versions via
  `DISABLE_AUTOUPDATER` in project settings

---

## [3.0a2-hf1]

### Added

- **User model enrichment** — three-tier user model (identity, preferences,
  knowledge) with unified knowledge pipeline feeding reflection and conversation
- **CI workflow** — ruff lint + pytest with advisory test gate

### Fixed

- **Terminal**: WebSocket compatibility with simple_websocket >=1.0 (returns
  None on timeout instead of raising TimeoutError)
- **CC invoker**: Handle missing claude CLI gracefully (FileNotFoundError)
- **Dependencies**: Pin wsproto>=1.2 (flask-sock transitive dep)
- **Dashboard**: Stale CC status display, degradation calculation, circuit
  breaker backoff timing
- **CI**: Scope lint to src/tests/scripts, ignore preserved AZ-era test files,
  make test job non-blocking while stabilizing
- **Lint**: Resolve all ruff errors (unused vars, unsorted imports, SIM105)

---

## [3.0a2]

### Changed

- **Standalone-only architecture** — Agent Zero fully removed. Genesis runs as
  a standalone server (`python -m genesis serve`) with its own dashboard,
  terminal, and API. AZ can still be used as an optional external agent
  framework via the adapter interface, but is no longer required or bundled.
- **OpenClaw gateway** — Genesis exposes `POST /v1/chat/completions` so OpenClaw
  (or any OpenAI-compatible router) can route channels through it
- **SDK-primary engine routing** — Claude SDK API is the primary execution path;
  Claude Code subprocess is optional based on operator preference

### Added

- **Neural monitor overhaul** — provider probes, subsystem grouping, circuit
  breaker wiring, detail panel with live backend data, warning severity color,
  subsystem sector clustering, visual redesign (larger diagram, refined colors),
  call site triage with naming consistency
- **Settings UX** — human-readable labels, tooltips, channel dropdown
- **Chain editor** — CC entries editable, repositionable, and removable
- **Autonomy enforcement** — data-driven RuleEngine with graduated enforcement
  spectrum (inform → guide → guard → block), SteerMessage abstraction
- **Anti-vision identity boundaries** — selective MCP loading, executor plan
  directive for content evaluation
- **User-evaluate skill** — evaluate content through Genesis's user model
- **update.sh** — pull, sync dependencies, restart services in one command

### Fixed

- **host-setup.sh**: Fix container networking on cloud VMs (GCP, AWS, Azure) —
  UFW `deny (routed)` default policy was blocking all forwarded container traffic
  (DNS, HTTPS). Script now adds `ufw route allow` rules for the Incus bridge.
  Also adds nftables accept rules as defense-in-depth for non-UFW distros.
- **host-setup.sh**: Auto-activate `incus-admin` group after Incus install —
  script previously exited with a permission error, requiring manual
  `newgrp incus-admin` to recover
- **host-setup.sh**: Fail fast on prerequisite install or git clone errors
  instead of continuing to "Genesis is ready" with a broken container
- **host-setup.sh**: Add ERR trap with line number, command, and exit code on
  any failure; `DEBUG=1` enables full `set -x` tracing
- **host-setup.sh**: Enable IP forwarding and bridge NAT before container
  creation; show progress during package installation
- **Dashboard**: uptime counter timezone bug, restart button self-restart,
  post-AZ-removal regressions, probe override guard, detail panel staleness,
  degraded status color visibility
- **Routing**: CC-only model saves silently dropped + input validation missing
- **update.sh**: Use `--rebase` to avoid divergent-branch errors on pull
- **Terminal**: Prefill CC command without auto-executing (user chooses when)
- **push-public-release.sh**: Create tag and GitHub Release even when content
  was already pushed (previously exited early, skipping the release step)
- **install.sh**: Add `cd ~/genesis &&` to headless login instructions so
  first-time users run `claude login` from the correct directory

---

## [v3.0a] - 2026-04-03

Genesis v3 — complete autonomous agent system. First public release.
All Phase 0–9 subsystems built, wired, and tested.

### Added

- **Memory system** — hybrid Qdrant vector + SQLite FTS5 search, episodic memory
  with session provenance, proactive memory injection at session start
- **Telegram integration** — resilient polling adapter with text, voice, photo,
  and document support; supergroup/forum topic routing; streaming responses
  via edit-based drafts; voice transcription via Whisper
- **Morning reports** — daily system state digest via Telegram with configurable
  structure and LLM-generated synthesis
- **Guardian** — host-VM watchdog with agentic Claude Opus diagnosis, briefing
  bridge, credential bridge, and shared filesystem mount
- **MCP servers** — memory recall, outreach queue, health status, and recon
  tools exposed as MCP endpoints for foreground Claude Code sessions
- **Outreach pipeline** — category-based message routing (alerts, digests,
  surplus, recon), engagement tracking, morning report scheduler
- **Reflection system** — background micro/light/deep/strategic reflection
  sessions with consolidation into episodic memory
- **Dual-repo distribution** — private working repo + public GENesis-AGI release
  with automated stripping of user-specific content
- **Dashboard** — web UI with system health, session management, built-in
  terminal, settings hub
- **Standalone server** — `python -m genesis serve` runs dashboard, API, and
  all subsystems; adapter protocol for provider-agnostic operation
- **Model routing** — configurable per-call-site routing with fallback chains,
  cost tracking, and provider health monitoring
- **Inbox monitor** — filesystem inbox for asynchronous task ingestion
- **Knowledge graph** — observation/finding/pattern storage with deduplication
- **Ego session framework** — autonomous proposal pipeline (inert until beta)
- **Hooks system** — PreToolUse/PostToolUse guards for behavioral enforcement
  (blocking pip editable installs to worktrees, validating kill signals, etc.)
- **Bootstrap script** — idempotent machine setup: venv, secrets, systemd
  services, Claude Code config generation

### Breaking

- Requires Python 3.12 and Ubuntu 22.04+
- `secrets.env` must be populated with API keys before first run
- Telegram bot token required for channel features
- Qdrant must be running locally (`localhost:6333`)

---

<!-- Template for future releases:

## [vX.Y] - YYYY-MM-DD

### Added
### Changed
### Fixed
### Breaking

-->
