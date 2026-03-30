# Dashboard Quick Wins & Easy Fixes

*From 2026-03-28 audit session. Pull up and execute in a future session.*

## Bug Fixes (< 30 min each)

### 1. MCP Vitals Section Broken
- **Symptom**: `"error": "query failed"` in operational vitals MCP section
- **Root cause**: Running bridge code doesn't match disk. Response has `tools`/`top_invocations_24h` keys but code on disk returns `servers` key.
- **Fix**: Restart bridge (`sudo systemctl restart genesis-bridge`), verify response matches code. If still broken, check `activity_log` table schema for `latency_ms`/`success` columns.
- **File**: `src/genesis/dashboard/routes/vitals.py:476`

### 2. Operator Shortcut Links Don't Expand Target Panels
- **Symptom**: Clicking `#queue-review`, `#routing-health` in budget panel scrolls to collapsed panel headers without expanding them.
- **Fix**: Add `@click` handler that sets the target panel's open state before scrolling, or use Alpine `x-init` with hash detection.
- **File**: `src/genesis/dashboard/templates/genesis_dashboard.html:3757-3760`

### 3. Pending Actions Panel: Remove or Relabel
- **Symptom**: Always empty. Shows "Genesis will surface proposals here as it becomes more autonomous." The `approval_requests` table has 0 rows. Autonomy approval loop isn't wired.
- **Options**:
  - (a) Remove panel entirely until autonomy system is active
  - (b) Relabel as "Autonomy Queue" and show a clearer "not yet active" state
  - (c) Repurpose to show the pending items from cognitive state markdown (regex-parsed)
- **Recommendation**: (a) — remove it. It's panel #2 in a prominent position, misleading users every time they look.
- **File**: `genesis_dashboard.html:3060-3092`

### 4. Session Patches Not Shown in Dashboard Cognitive Panel
- **Symptom**: Dashboard shows only the stale `active_context` narrative. Session patches (from `~/.genesis/session_patches.json`) aren't rendered.
- **Fix**: Include session patches in the `/api/genesis/cognitive` response. The `render()` function already assembles them — expose that or add a `patches` field to the endpoint.
- **Files**: `src/genesis/dashboard/routes/state.py:27-46`, `src/genesis/db/crud/cognitive_state.py:20-32`

## Monitoring Gaps (< 1 hour each)

### 5. Qdrant 0 Indexed Vectors — Not Flagged
- **Symptom**: Both collections show `indexed_vectors: 0`. HNSW index not building. All searches are brute-force.
- **Fix**: Add a computed health flag in `compute_state_flags()` when indexed_vectors == 0 but points > threshold (e.g., 100).
- **File**: `src/genesis/db/crud/cognitive_state.py:213`
- **Also**: Investigate why HNSW indexing isn't running in Qdrant config.

### 6. Ollama 10.7s Avg Latency — Not Flagged
- **Symptom**: `avg_embedding_latency_ms: 10766.6` shown as a plain number in vitals. No visual indicator of degradation.
- **Fix**: Add threshold-based coloring in the On-Prem vitals subsection. >2000ms = yellow, >5000ms = red.
- **File**: `genesis_dashboard.html` On-Prem section (~line 3450+)

### 7. Budget Panel: Show Current Spend
- **Symptom**: Shows limits ($2/day, $10/week, $30/month) but not current utilization.
- **Fix**: The health endpoint already returns `cost.daily_usd` and `cost.monthly_usd`. Wire these into the budget panel as progress bars or percentage indicators alongside the limits.
- **File**: `genesis_dashboard.html:3680-3692`

## Config Exposure (< 2 hours)

### 8. Add Dashboard-Editable Settings
These are currently in YAML files, editable via Config Files panel but not surfaced with proper UI:
- Reflection frequency (Deep/Light/Micro floors and thresholds) — from `autonomy.yaml`
- Surplus compute scheduling preferences
- Observation lifecycle (auto-resolve threshold, backlog warning level)
- Session limits (max concurrent background sessions)
- Guardian thresholds (container memory warning level)

**Approach**: Add a "System Settings" card to the Budget & Controls panel with key sliders/inputs that write back to the YAML files via the existing config PUT endpoint.

## Visual Polish (< 30 min each)

### 9. Embedding Stats Contradiction
- `embedding_ops_24h: 180` but `embeddings_24h: 0` in on-prem section. Confusing — one is Ollama ops, the other is Qdrant writes. Label them clearly.

### 10. Cognitive State: Show Freshness Warning
- When `active_context.created_at` is >4 hours old, show a yellow "Last updated X hours ago" badge. User should know the narrative is stale at a glance.
