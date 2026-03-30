# Inbox Monitor — Implementation Plan

> **Target:** Implementable in a parallel session after Phase 6 is complete.
> Designed to be the first feature Genesis uses autonomously — user drops
> links/notes in a folder, Genesis evaluates and responds asynchronously.
>
> **Dependencies:** Phase 3 (surplus scheduler pattern), Phase 6 (item
> classification, CC session dispatch, evaluate skill wiring).
>
> **Not blocked by:** Phase 7 (deep reflection), Phase 8 (outreach pipeline).
> The minimum viable version bypasses both — it dispatches directly to CC
> sessions and writes responses to filesystem, without the full outreach
> governance stack.

---

## Architecture

### Core Principle

The inbox monitor is a **peripheral service**, not part of the awareness loop.
It follows the same pattern as `SurplusScheduler`:
- Own APScheduler instance
- Own configurable cadence
- Produces work items for existing infrastructure
- Participates in Genesis lifecycle (start/stop, health probes, observability)

### System Diagram

```
┌─────────────────────────────────────────────────────┐
│ Genesis Runtime                                      │
│                                                      │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ Awareness    │    │ Surplus      │                │
│  │ Loop (5m)    │    │ Scheduler    │                │
│  └─────────────┘    └──────────────┘                │
│                                                      │
│  ┌─────────────────────────────────┐                │
│  │ InboxMonitor (configurable)     │                │
│  │  ┌─────────┐  ┌──────────────┐ │                │
│  │  │ Scanner  │  │ Classifier   │ │                │
│  │  │ (mtime/  │→ │ (link/note/  │ │                │
│  │  │  hash)   │  │  ambiguous)  │ │                │
│  │  └─────────┘  └──────┬───────┘ │                │
│  └───────────────────────┼─────────┘                │
│                          │                           │
│              ┌───────────┼───────────┐              │
│              ▼           ▼           ▼              │
│     ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│     │ Surplus   │  │ Memory   │  │ Message  │      │
│     │ Queue     │  │ Store    │  │ Queue    │      │
│     │ (research)│  │ (notes)  │  │ (questions│      │
│     └────┬─────┘  └──────────┘  └──────────┘      │
│          ▼                                          │
│     ┌──────────┐                                    │
│     │ CC Session│                                   │
│     │ (evaluate │                                   │
│     │  skill)   │                                   │
│     └────┬─────┘                                    │
│          ▼                                          │
│     ┌──────────┐                                    │
│     │ Response  │                                   │
│     │ Writer    │                                   │
│     └──────────┘                                    │
└─────────────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────┐
  │ User's folder│
  │ /_genesis/   │
  │  response.md │
  └──────────────┘
```

---

## Implementation Steps

### Step 1: InboxMonitor Core Service

**Files:**
- `src/genesis/inbox/__init__.py`
- `src/genesis/inbox/types.py`
- `src/genesis/inbox/scanner.py`
- `src/genesis/inbox/monitor.py`

**`types.py`** — Data types:
```python
class InboxItemType(StrEnum):
    LINK = "link"           # URL(s) found — dispatch for research
    NOTE = "note"           # Plain text — store as observation
    AMBIGUOUS = "ambiguous" # Unclear intent — queue question for user

@dataclass(frozen=True)
class InboxItem:
    file_path: Path
    content: str
    content_hash: str
    item_type: InboxItemType
    urls: list[str]          # extracted URLs (empty for notes)
    detected_at: datetime

@dataclass(frozen=True)
class InboxConfig:
    watch_path: Path
    response_dir: str        # subdirectory name (default: "_genesis")
    check_interval_seconds: int  # default: 1800 (30 minutes)
    enabled: bool
```

**`scanner.py`** — Filesystem scanning:
- `scan_folder(path) → list[Path]` — list markdown files, exclude response dir
- `compute_hash(path) → str` — SHA-256 of file content
- `detect_changes(path, known_hashes) → list[Path]` — new or modified files
- `extract_urls(content) → list[str]` — regex extraction of HTTP(S) URLs
- `classify_item(content, urls) → InboxItemType` — heuristic classification:
  - Has URLs and minimal surrounding text → LINK
  - Has substantial text, no URLs → NOTE
  - Mixed or unclear → AMBIGUOUS

**`monitor.py`** — The service:
- `InboxMonitor` class with APScheduler instance
- `start()` / `stop()` lifecycle methods
- `_check_inbox()` — scheduled callback:
  1. `detect_changes()` for new/modified files
  2. For each changed file: classify, extract URLs
  3. Links → create surplus task (type: "inbox_research", payload: URLs + context)
  4. Notes → store as observation in memory-mcp
  5. Ambiguous → add to message_queue as pending question
  6. Update known_hashes
- Tracks processed items in a local JSON file (`{response_dir}/.inbox_state.json`)
  or in the Genesis DB (preferred if available)

**Tests:**
- Scanner: hash computation, change detection, URL extraction, classification
- Monitor: lifecycle, scheduling, dispatch to surplus queue
- Edge cases: empty files, binary files, files with no changes, sync conflicts

### Step 2: Response Writer

**Files:**
- `src/genesis/inbox/writer.py`

**`writer.py`** — Writes evaluation results as Obsidian-compatible markdown:
- `write_response(config, title, content, source_file) → Path`
- Creates `{watch_path}/{response_dir}/YYYY-MM-DD-{slug}.md`
- YAML frontmatter: date, source file, item count, status
- Body: evaluation results (four-lens analysis, architecture mapping, etc.)
- Written atomically: write to `.tmp` file, then `os.rename()`
- Obsidian conventions: `[[wiki links]]` for cross-references, tags

**Response format:**
```markdown
---
date: 2026-03-09
source: interesting-links.md
status: complete
---

# Research Evaluation — 2026-03-09

Genesis evaluated 3 links from your inbox.

## Link 1: [Title](url)

### How It Helps
...

### How It Doesn't Help
...

### How It COULD Help
...

### What to Learn From It
...

### Architecture Impact
...

---

*Evaluated by Genesis using the research-evaluation skill.*
*Follow up via Telegram/WhatsApp for discussion.*
```

### Step 3: Configuration & User Control

**Config location:** `config/inbox_monitor.yaml` (or section in existing config)

```yaml
inbox_monitor:
  enabled: true
  watch_path: "/path/to/obsidian/vault/00-INBOX"
  response_dir: "_genesis"
  check_interval_seconds: 1800  # 30 minutes
  max_urls_per_check: 10        # rate limit
  ambiguity_action: "ask"       # "ask" | "skip" | "research_anyway"
```

**User can change:**
- Path to watched folder
- Check interval (minutes, hours, daily)
- Enable/disable without restarting Genesis
- What to do with ambiguous items

**Dashboard panel (Phase 8):** Shows inbox monitor status, last check time,
pending items, recent evaluations. Quick toggle for enable/disable.

### Step 4: CC Session Dispatch

**Integration with existing infrastructure:**

The inbox monitor does NOT run evaluations itself. It creates surplus tasks
that get dispatched to CC sessions:

1. `InboxMonitor._check_inbox()` finds links
2. Creates `SurplusTask` with type "inbox_research" and payload containing URLs
3. `SurplusScheduler` picks up the task (existing dispatch loop)
4. Dispatches to CC background session with evaluate skill in system prompt
5. CC session runs the evaluation, produces structured output
6. Post-session: `ResponseWriter` takes the output, writes to `_genesis/`

**For the minimum viable version (before Phase 7 session_config):**
Use `CCInvoker` directly with a hardcoded system prompt that includes the
evaluate framework. This bypasses the full session_config machinery but
delivers working functionality.

### Step 5: AZ Extension Wiring

**File:** `usr/plugins/genesis/extensions/agent_init/_50_genesis_inbox.py`

- Reads inbox config from `config/inbox_monitor.yaml`
- Creates `InboxMonitor` instance
- Starts the monitor (if enabled)
- Registers health probe for observability
- Registers event bus listeners for inbox events

### Step 6: Foreground Context Bridge

When the user follows up via Telegram/WhatsApp about inbox evaluations,
foreground Genesis needs context. Two mechanisms:

1. **Message queue:** Inbox evaluations logged as messages in `message_queue`
   table. Foreground Genesis retrieves recent inbox activity as context.

2. **Cognitive state:** Deep reflection (Phase 7) includes inbox activity
   in the cognitive state summary. "I evaluated 3 links from your inbox
   yesterday: [topics]. Full results in _genesis/2026-03-09-evaluation.md."

For the minimum viable version (before Phase 7), use message_queue only.

---

## Build Order

```
Step 1: InboxMonitor core (scanner, classifier, monitor service)
Step 2: Response writer (Obsidian-compatible markdown output)
Step 3: Configuration (YAML config, user controls)
Step 4: CC session dispatch (surplus task → evaluate skill)
Step 5: AZ extension wiring (startup, health, observability)
Step 6: Foreground context bridge (message_queue integration)
```

Steps 1-3 are independent of CC session infrastructure and can be built
and tested with mock dispatchers. Steps 4-5 require Phase 6 to be
complete. Step 6 can happen later (Phase 7+).

---

## Verification

- [ ] InboxMonitor starts/stops cleanly with Genesis lifecycle
- [ ] Folder scanning detects new and modified files correctly
- [ ] URL extraction finds HTTP(S) links in markdown content
- [ ] Item classification: links, notes, and ambiguous items routed correctly
- [ ] No duplicate processing (hash-tracked state persists across restarts)
- [ ] Response files written atomically (no partial writes)
- [ ] Response files are valid Obsidian markdown (frontmatter, wiki links)
- [ ] Config changes take effect without restart (interval, path, enabled)
- [ ] Surplus task dispatch works end-to-end (link → CC session → response)
- [ ] Ambiguous items appear in message_queue for foreground follow-up
- [ ] Health probe reports monitor status (running, last check, items pending)
- [ ] Observability events fired for: check started, items found, evaluation
  complete, error encountered

---

## V4 Extensions (Not in Scope)

- **Tag-based routing** — scan entire vault for `#genesis/*` tags
- **Proactive research** — Genesis-initiated research based on user's notes
- **Graph-connected outputs** — response files with wiki links to existing vault notes
- **Multi-vault support** — monitor multiple folders with different configs
- **Priority classification** — urgent links processed immediately vs batched

---

*Created: 2026-03-09*
*Status: IMPLEMENTED (2026-03-10) — LLM-first classification, no heuristic layer*
*Parallel session: Yes — can be developed alongside Phase 7*
