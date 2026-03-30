# AZ Rebase + Memory Plugin Migration Plan

**Created:** 2026-03-08
**Status:** Waiting for upstream plugin system to ship to testing/main
**Upstream pin:** fa65fa3 (v0.9.8.2, 2026-02-24)
**Upstream development:** 1,441 commits ahead, plugin system stable (memory v1.0.0)

## Trigger Condition

Execute this plan when upstream ships the plugin system to `testing` or `main`.
Check periodically: `cd ~/agent-zero && git fetch upstream && git log upstream/main --oneline -5`

## Pre-Rebase Checklist

- [ ] All Genesis tests pass on current pin (`cd ~/genesis && pytest -v`)
- [ ] All uncommitted AZ patches committed or stashed
- [ ] Fork-tracking.md reviewed and current
- [ ] Migration script tested (`python scripts/migrate_faiss_to_qdrant.py --dry-run`)
- [ ] FAISS data backed up (`cp -r ~/agent-zero/usr/memory ~/agent-zero/usr/memory.backup`)

## Rebase Strategy

**Method:** Interactive rebase, NOT merge. Cleaner history.

```bash
cd ~/agent-zero
git fetch upstream
git rebase upstream/main  # or upstream/testing
```

## Known Conflicts (3 HIGH-risk)

### 1. `secrets.py` (HIGH)
**Issue:** Upstream renamed `get_project_meta_folder()` to `get_project_meta()`
**Resolution:** Update our MCP secret resolution patch to use new function name.
Search our code for `get_project_meta_folder` and replace.

### 2. `run_ui.py` (HIGH)
**Issue:** Massively restructured -- auth, plugin routes, `@extensible` decorators.
**Resolution:** Re-apply our WebSocket debounce/ping patches to the new structure.
Our changes are small (timeout values, ping config) -- locate equivalent code in
new structure and apply manually.

**CRITICAL — Server Startup Hook:** `init_a0()` contains a 3-line Genesis
bootstrap (`DeferredTask → call_extensions("server_startup")`). This MUST be
re-applied after rebase. Without it, all Genesis background systems are dead.
Verify: `grep -n "server_startup" ~/agent-zero/run_ui.py`.

### 3. `agent.py` (HIGH)
**Issue:** `@extensible` decorators added to ~20 methods. Our security hardening
touches many of the same methods.
**Resolution:** Re-apply security hardening AFTER extensible decorators. Our
changes (timeouts, output truncation) should work as method body modifications
inside the decorated methods. Long-term: move security hardening to a Genesis
plugin hook instead of core patches.

## Memory Plugin Conversion

After successful rebase, convert the adapter code to a proper plugin:

### Step 1: Create plugin structure
```
usr/plugins/genesis-memory/
├── plugin.yaml
├── default_config.yaml
├── helpers/
│   └── memory.py          # Adapter: delegates to genesis.memory
├── tools/
│   ├── memory_save.py
│   ├── memory_load.py
│   ├── memory_delete.py
│   └── memory_forget.py
├── extensions/
│   ├── monologue_start/
│   │   └── _10_memory_init.py
│   ├── monologue_end/
│   │   ├── _50_memorize_fragments.py
│   │   └── _51_memorize_solutions.py
│   └── message_loop_prompts_after/
│       └── _50_recall_memories.py
└── api/
    └── memory_dashboard.py
```

### Step 2: Implement memory.py adapter
Use `src/genesis/memory/az_adapter.py` translation layer.
Memory class delegates to:
- `genesis.memory.store.MemoryStore` for writes
- `genesis.memory.retrieval.HybridRetriever` for reads
- `genesis.qdrant.collections` for point operations

### Step 3: Disable upstream memory plugin
In AZ config, disable the default `plugins/memory/` plugin so our
`genesis-memory` plugin handles all memory operations.

### Step 4: Run migration
```bash
python scripts/migrate_faiss_to_qdrant.py --memory-dir ~/agent-zero/usr/memory/default
```

### Step 5: Verify
- [ ] AZ boots clean with genesis-memory plugin
- [ ] Memory search returns results from Qdrant
- [ ] Memory save persists to Qdrant + FTS5
- [ ] Dashboard renders identically
- [ ] MemoryConsolidator works (merge/replace/keep_separate)
- [ ] RecallMemories extension finds memories
- [ ] MemorizeFragments stores fragments
- [ ] MemorizeSolutions stores solutions
- [ ] Area filtering works (MAIN, FRAGMENTS, SOLUTIONS)
- [ ] Memory subdir isolation works

## Genesis Extension Restructuring

Current Genesis plugins (`usr/plugins/genesis/`) may need updates for the
new plugin structure:

| Current | After Rebase |
|---------|-------------|
| `_10_initialize_genesis.py` | Verify plugin discovery finds it |
| `_20_genesis_observability.py` | Verify event bus wiring |
| `_30_genesis_perception.py` | Verify reflection engine injection |
| `_40_genesis_cc_relay.py` | Verify CC bridge wiring |

Key changes to watch:
- Plugin YAML manifest format (our current `plugin.yaml` may need updating)
- Extension discovery paths (verify `get_paths()` includes our plugins)
- Config scoping (per-agent, per-project settings)

## Patches to Drop on Rebase

| Patch | Why |
|-------|-----|
| Bridge patch (`plugins.py`) | Superseded by upstream plugin system |
| Whisper import guard | Upstream fixed with pinned openai-whisper |
| Knowledge preload (if upstream added) | Check if upstream has equivalent |

## Patches to Re-apply on Rebase

| Patch | Files | Notes |
|-------|-------|-------|
| litellm dependency | requirements.txt | Still not in upstream |
| MCP secret resolution | mcp_handler.py, secrets.py | Update for renamed functions |
| WebSocket debounce | state_monitor.py, websocket.js | Apply to new code locations |
| Runtime fixes | models.py, chat_load.py | max_tokens, finish_reason |
| Security hardening | Multiple files | Consider plugin approach |
| Ollama CPU semaphore | models.py | Check if upstream addressed |
| Embedding batch size | memory.py -> plugin | Apply to plugin version |

## Testing Strategy

1. **30-test patch verification suite** (existing, from fork-tracking.md)
2. **Genesis test suite** (`cd ~/genesis && pytest -v` -- currently 651 tests)
3. **AZ web UI smoke test:**
   - Boot AZ, open dashboard
   - Search memories -- results appear
   - Save new memory -- appears in search
   - Delete memory -- removed from search
   - Consolidation test -- merge two similar memories
4. **Integration test:**
   - Genesis awareness loop tick -- reflection -- observation stored
   - Memory recall via MCP -- results from Qdrant
   - User model delta processed -- model updated

## Post-Rebase Cleanup

- [ ] Update fork-tracking.md with new base commit
- [ ] Update CLAUDE.md if any commands changed
- [ ] Update genesis test count in MEMORY.md
- [ ] Remove `.bak` FAISS files after confirming migration success
- [ ] Run `ruff check .` on both repos
