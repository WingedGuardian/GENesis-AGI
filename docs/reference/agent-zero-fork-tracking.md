# Agent Zero Fork Tracking

**Fork:** `WingedGuardian/agent-zero` (from `frdel/agent-zero`)
**Created:** 2026-03-01
**Upstream pin:** commit `fa65fa3` (v0.9.8.2, 2026-02-24)
**Local clone:** `~/agent-zero`

## Remotes

| Name | URL | Purpose |
|------|-----|---------|
| `origin` | `github.com/WingedGuardian/agent-zero` | Our fork (push here) |
| `upstream` | `github.com/frdel/agent-zero` | Community upstream (pull from here) |

## Applied Patches

### Committed to `main`

| Commit | Source | Description | Files |
|--------|--------|-------------|-------|
| `f2f1d4c` | Local | Add missing `litellm` dep, pin `openai==1.99.5`, remove duplicate crontab | `requirements.txt` |
| `753dc76` | PR #1149 + #1150 | Resolve `§§secret()`/`$$secret()` in MCP config + handle `structuredContent` | `mcp_handler.py`, `secrets.py` |
| `9f41535` | PR #1090 | Prevent WebSocket disconnection during long-running tasks (debounce, ping timeout) | `state_monitor.py`, `browser_agent.py`, `code_execution_tool.py`, `run_ui.py`, `websocket.js` |
| `929473f` | PR #1114 | Fix negative `max_tokens`, whisper import guard, history compression resilience | `models.py`, `whisper.py`, `_90_organize_history_wait.py`, `chat_load.py` |
| `f14b946` | Local (bugfix) | Move debounce attrs from `State` to `BrowserAgent` (PR #1090 had wrong class) | `browser_agent.py` |

### Ollama CPU Saturation Fix (uncommitted, 2026-03-04)

| Files | Description |
|-------|-------------|
| `models.py` | Per-host threading semaphore serializes concurrent embedding calls to the same `api_base` (e.g. Ollama). Prevents CPU saturation when memorization pipeline fires 50-70+ embedding requests. Cloud APIs (no `api_base`) unaffected. |
| `python/helpers/memory.py` | Add `batch_size=4` to `CacheBackedEmbeddings.from_bytes_store()` — prevents sending all uncached texts in a single massive embedding request. |

### Knowledge Preload at Startup (uncommitted, 2026-03-02)

| Files | Description |
|-------|-------------|
| `preload.py` | Add `preload_knowledge()` to startup tasks — eagerly loads FAISS knowledge index so first user message isn't blocked by "Preloading knowledge..." (was 3-5 min on first msg after restart). Calls `Memory.get_by_subdir()` during async preload. |

### API Key Routing Fix (uncommitted, 2026-03-10)

| Files | Description |
|-------|-------------|
| `python/helpers/dotenv.py` | Added `get_secrets_file_path()` → `usr/secrets.env`. Added optional `path` param to `save_dotenv_value()` (backwards-compatible). After every save, reloads both `.env` and `secrets.env` so `os.environ` stays current. |
| `python/helpers/settings.py` | `_write_sensitive_settings()` now writes API keys to `usr/secrets.env` (via `path=secrets_path`) instead of `usr/.env`. Auth fields still go to `.env`. |

**Why:** AZ Settings UI wrote `API_KEY_*` stubs to `.env`. AZ loaded `.env` with `override=True`, setting empty strings in `os.environ`. Genesis loaded `secrets.env` with `override=False` — real keys silently ignored. Fix: Settings UI writes to `secrets.env`, Genesis loads with `override=True`.

### Genesis Server Startup Hook (uncommitted, 2026-03-10)

| Files | Description |
|-------|-------------|
| `run_ui.py` | 3 lines in `init_a0()`: DeferredTask fires `server_startup` extensions before server accepts connections. Creates persistent EventLoopThread for Genesis APSchedulers. |
| `usr/plugins/genesis/extensions/server_startup/_00_genesis_bootstrap.py` (new) | Calls `GenesisRuntime.instance().bootstrap()` — single entry point for all Genesis infrastructure. |

**CRITICAL REBASE INVARIANT**: This patch MUST be re-applied after every AZ
update. Without it, Genesis background infrastructure (awareness loop, learning
scheduler, inbox monitor) never starts. Verify with: `grep -n "server_startup"
~/agent-zero/run_ui.py` — should return exactly one line.

### Genesis UI Overlay Blueprint (committed 2026-03-15)

| Files | Description |
|-------|-------------|
| `usr/plugins/genesis/extensions/server_startup/_00_genesis_bootstrap.py` | Added ~10 lines to register `genesis_ui` Flask blueprint + `register_injection()` after_request hook. The blueprint serves Genesis UI assets at `/genesis-ui/` and injects `<script>/<link>` tags into AZ's index.html response. Enables full UI rebranding (logo, watermark, version label), AZ feature hiding (scheduler, projects, irrelevant settings tabs), welcome screen rewiring (New Chat→Genesis, Files→Inbox), and Genesis memory browser — all without modifying any AZ core files. |

**Rebase risk:** LOW — only modifies `_00_genesis_bootstrap.py` (our own plugin file). The `after_request` approach depends on AZ's `serve_index()` returning HTML with `</head>`, which is a stable pattern. If AZ changes to streaming responses, the injection would need adjustment (but would fail gracefully — AZ UI still works, just without Genesis branding).

### Brave Search Fallback (uncommitted, 2026-03-10)

| Files | Description |
|-------|-------------|
| `python/helpers/brave_search.py` (new) | Brave Search API helper. Returns SearXNG-compatible format (`{"results": [{"title", "url", "content"}]}`). Uses `API_KEY_BRAVE` via `models.get_api_key()`. |
| `python/tools/search_engine.py` | Added Brave Search as fallback when SearXNG is unavailable. Try SearXNG → Brave → error message. |

### Genesis Observability Extension (uncommitted, 2026-03-04)

| Files | Description |
|-------|-------------|
| `usr/plugins/genesis/extensions/agent_init/_20_genesis_observability.py` | Wires GenesisEventBus + NotificationBridge + structured logging at agent startup. Converts Genesis severity enums to AZ `NotificationType`/`NotificationPriority` enums via thin wrapper. WARNING+ events forwarded to AZ web UI. |

### Bridge Patch (committed 2026-03-01)

| Commit | Files | Description |
|--------|-------|-------------|
| `b96801a` | `plugins.py` (new), `subagents.py` | Minimal plugin discovery — adds `include_plugins` param to `get_paths()`, scans `usr/plugins/*/` for extensions, tools, prompts, agents. Matches upstream development branch API. Delete when upstream ships plugins to testing/main. |

### Security Hardening (committed 2026-03-01)

| Files | Description |
|-------|-------------|
| `guardrails.py` (new) | Central guardrail logging + prompt injection detection (7 patterns) |
| `_20_check_injection.py` (new) | Post-tool-execution injection scan extension |
| `agent.py` | Tool-specific timeouts (60-300s) + 50KB output truncation |
| `code_execution_tool.py` | Dunder attribute blocking (15 patterns) + 45-command allowlist |
| `call_subordinate.py` | Max delegation depth=5 + 5-min timeout + orphan shell cleanup |
| `tty_session.py` | Process group management (SIGTERM → SIGKILL graceful shutdown) |
| `git.py` | Clone size check (50MB) + shallow clone + 120s timeout |
| `download_work_dir_file.py` | 10MB file download limit |
| `searxng.py` | 10MB search response size check |
| `shell_ssh.py` | `shlex.quote()` on SSH cwd parameter |

## Dependency Conflict Notes

- `browser-use==0.5.11` pins `openai==1.99.2` (exact)
- `litellm>=1.78.7` requires `openai>=1.99.5`
- **Resolution:** Pin `openai==1.99.5`. browser-use works fine despite the 3-patch-version
  mismatch (1.99.2 vs 1.99.5). No API breaks between those versions.
- `litellm` was never in upstream `requirements.txt` despite being imported by `models.py`
- `chardet` pinned to `<6.0.0` (5.2.0) to fix `requests` compatibility warning

## Cherry-Pick Evaluation Notes

### Skipped from PR #1114

The memory plugin fixes (`plugins/memory/extensions/python/monologue_end/`) were
skipped because `plugins/memory/` doesn't exist in our version (v0.9.8.2). These
target a newer plugin system.

### Bug Found in PR #1090

PR #1090 added `_last_progress_time` and `_progress_debounce` to `State.__init__`
but `update_progress()` is on `BrowserAgent` (different class). Fixed with
`getattr()` lazy init in commit `f14b946`.

## Patch Status vs Upstream `development` (audited 2026-03-01)

| Commit | File(s) | Status | Rebase Risk |
|--------|---------|--------|-------------|
| `f2f1d4c` | `requirements.txt` | **STILL NEEDED** — upstream still doesn't list litellm | Low |
| `753dc76` | `mcp_handler.py` | **STILL NEEDED** — structuredContent + MCP startup secrets not upstream | Medium |
| `753dc76` | `secrets.py` | **CONFLICTING** — upstream renamed `get_project_meta_folder()` → `get_project_meta()` | **HIGH** |
| `9f41535` | `run_ui.py` | **CONFLICTING** — file massively restructured upstream (moved auth, added plugin routes, `@extensible`) | **HIGH** |
| `9f41535` | `websocket.js` | **STILL NEEDED** | Low |
| `9f41535` | `state_monitor.py` | **STILL NEEDED** — upstream still at 0.025 debounce | Low |
| `929473f` | `models.py` | **STILL NEEDED** — max_tokens guard, drop_params, finish_reason not upstream | Medium |
| `929473f` | `whisper.py` | **REDUNDANT** — upstream pinned `openai-whisper`, guard unnecessary | Drop on rebase |
| `929473f` | `_90_organize_history_wait.py` | **STILL NEEDED** | Low |
| `929473f` | `chat_load.py` | **STILL NEEDED** | Low |
| `f14b946` | `browser_agent.py` | **STILL NEEDED** | Low |
| `32011ac` | `agent.py` (security) | **STILL NEEDED** but **CONFLICTING** — upstream added `@extensible` decorators to ~20 methods | **HIGH** |
| `32011ac` | `guardrails.py`, `_20_check_injection.py` | **STILL NEEDED** — not in upstream | Low (new files) |
| `32011ac` | All other security files | **STILL NEEDED** — no security hardening upstream | Medium |

### Missing Upstream Fix

Upstream `development` added empty-content message filtering in `models.py`
(prevents sending empty-content messages to LLM APIs). We should cherry-pick
this fix.

### Rebase Strategy (for when upstream ships plugins to testing/main)

1. Cherry-pick upstream's empty-content fix NOW (before rebase)
2. Re-apply security hardening as a Genesis plugin (not core patches to agent.py)
3. Re-apply remaining fixes to new file structure
4. Drop whisper import guard (redundant) and bridge patch (superseded)
5. Fix `get_project_meta_folder` → `get_project_meta` rename in secrets.py
6. Run full 30-test patch verification + Genesis test suite
7. Update this document
8. **CRITICAL**: Re-apply Genesis server_startup hook in `init_a0()` — without
   this, all Genesis background systems are dead. Verify:
   `grep -n "server_startup" ~/agent-zero/run_ui.py`

## Upstream Sync Policy

- **Cadence:** Check upstream quarterly or when a major version drops
- **Method:** `git fetch upstream && git log upstream/main --oneline` to review new commits
- **Strategy:** Cherry-pick fixes we need. Do NOT merge upstream wholesale — too
  much churn, and we have custom patches that would conflict.
- **Before syncing:** Run the full 30-test patch verification suite (see testing notes)

## Testing

All patches verified with a 30-test suite covering:
- Dependency coexistence (litellm + openai + browser-use)
- structuredContent handling (with text, without text, backward compat)
- Secret placeholder resolution (§§ and $$ patterns, actual resolution)
- WebSocket debounce (StateMonitor, BrowserAgent, CodeExecution)
- Server/client ping config (run_ui.py, websocket.js)
- Runtime fixes (finish_reason, max_tokens, drop_params, whisper guards)
- History compression error handling
- Chat load dirty marking

Genesis tests (181) also pass clean after all Agent Zero changes.

## Memory System Strategy

Agent Zero's memory uses FAISS (local, per-session, file-based). Genesis uses
Qdrant + SQLite FTS5 (remote, cross-session, queryable).

**Current approach (Phase 5 Step 2):** Zero AZ changes. All adapter and
translation code lives in the Genesis repo (`genesis.memory.az_adapter`).
A standalone migration script (`scripts/migrate_faiss_to_qdrant.py`) handles
one-time FAISS → Qdrant data migration. AZ's `memory.py` is NOT modified —
it continues using FAISS until the rebase.

**Post-rebase approach:** Convert `genesis.memory` into a proper memory plugin
matching upstream's `plugins/memory/` structure. Disable upstream's default
memory plugin. AZ's recall/memorize extensions work unchanged — they call tool
names, not FAISS directly. Full plan: `docs/plans/az-rebase-plugin-migration.md`.

Upstream's `development` branch already decoupled memory into a plugin
(`plugins/memory/`, v1.0.0, stable), confirming this is the intended extension
point. Upstream is 1,441+ commits ahead of our pin. Modifying `memory.py` at
its old location creates worst-case rebase conflict (file moved + both sides
modified).

### Genesis Memory Adapter (in Genesis repo, no AZ changes)

| File | Purpose |
|------|---------|
| `src/genesis/memory/az_adapter.py` | Document ↔ Qdrant payload translation, area filter extraction, subdir mapping |
| `src/genesis/qdrant/collections.py` | `get_point()`, `scroll_points()` — ID-based retrieval and paginated listing |
| `scripts/migrate_faiss_to_qdrant.py` | One-time FAISS → Qdrant migration (re-embeds via EmbeddingProvider) |
| `docs/plans/az-rebase-plugin-migration.md` | Comprehensive rebase + plugin conversion plan |

See `docs/architecture/genesis-agent-zero-integration.md` for full interface
contract and backend mapping.
