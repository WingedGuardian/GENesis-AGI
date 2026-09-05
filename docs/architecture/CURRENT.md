# Genesis — Current Architecture (the Subsystem Map)

This is the **canonical judgment-layer map** of Genesis: what each subsystem is
FOR, the mechanisms auditors keep forgetting exist, what is LIVE vs shadow vs
dark, and the do-not-touch edges. It answers "does Genesis have X?" — consult
it FIRST (via the `subsystem-map` skill) before any capability claim, audit, or
competitive comparison. The package-level structural companion is
`.claude/skills/genesis-development/references/codebase-map.md`; the
philosophical "why" is `docs/architecture/genesis-v3-vision.md`.

**How this file stays honest.** Every entry claims its top-level
`src/genesis` modules in a fenced `yaml subsystem-map` block and carries a
`verified: <short-sha> <date>` stamp. CI (`subsystem-map-check`, backed by
`scripts/check_subsystem_map.py`) fails the build if a module is unmapped,
claimed twice, or vanished; stale stamps only warn. After changing a
subsystem's capabilities, update its entry and bump its stamp (PR-template
checkbox).

**Stamp a commit that SURVIVES THE MERGE — a default-branch commit, never your
feature-branch HEAD.** Squash-merge discards branch commits, so a branch-head
sha is unresolvable forever the moment the PR lands. That is not hypothetical:
two stamps already name dead feature-branch heads, and because
`check_subsystem_map.check_staleness` returns on the FIRST sha it cannot
resolve, those two silently disabled the freshness check for ALL fourteen
entries — reporting it as "shallow or incomplete git history", a cause that is
not the cause. Stamp the `origin/main` tip you verified against.
And bump ONLY when you re-verified: the stamp asserts "verified at this
commit", so bumping on faith writes a false claim into permanent record.

**Naming trap.** The `capability_map` DB table and `ego/capability_aggregator.py`
are the ego's per-domain *self-confidence model* — completely unrelated to this
document. Everything here is "subsystem map".

Maturity vocabulary: **LIVE** = wired into the runtime path and running;
**shadow** = running but observe/log-only; **dark** = built, no live caller
(usually `# GROUNDWORK(id)` — intentional, never delete as dead code);
**gated** = present but off until an env var / config / user grant enables it.

---

## 1. Memory — retrieval, consolidation, vector store

Persistent hybrid memory: FTS5 + Qdrant episodic/knowledge retrieval on the
read side, extraction and dream-cycle consolidation on the write/maintenance
side.

```yaml subsystem-map
entry: memory
modules: [memory, qdrant]
verified: ef6eb541 2026-08-10
```

**Cross-store integrity is detect + repair.** SQLite (`memory_metadata`/
`memory_fts`) and Qdrant share no transaction, so drift (orphaned "ghost"
points; "lying mirror" rows claiming a vector that's gone) is structurally
possible. `memory/integrity.py` (Phase 0) detects nightly via set algebra and
persists reports; `memory/integrity_repair.py` (Phase 1, `memory_reconcile`
job 04:40, `integrity_config` mode `active` — the default) repairs aged
offenders nightly: open delete-intent tombstones drained first (deferred
deletes re-attempted through the full store cascade; attempted ids excluded
from that run's ghost/mirror sets), ghosts deleted (payload exported to a
date-stamped JSONL under `~/.genesis/output/` first), mirrors re-queued
through `pending_embeddings.requeue_for_reembed` so `EmbeddingRecoveryWorker`
rebuilds the real vector. Repair is serialized against deletes from EVERY
process: in-process via the per-memory-id lock (`memory/_locks.py`);
cross-process via DB-backed tombstones (`memory/delete_tombstones.py`, rows on
`deferred_work_queue`, written by `MemoryStore.delete()` when Qdrant is down)
plus an atomic metadata/tombstone guard inside `requeue_for_reembed` (single
SQL statement — SQLite's cross-process write lock makes it atomic vs a delete
in any process, e.g. the genesis-memory MCP server's `reference_delete`). The
irreducible floor (documented, self-healing): the recovery worker's
SQLite-check→Qdrant-upsert micro-window vs a concurrent full delete can still
strand a ghost until the next nightly sweep. Truncation asymmetry is
load-bearing: a truncated point scroll keeps ghost classification sound
(metadata read is complete) but makes mirror classification unsound (can't
prove absence) — mirrors are skipped under truncation. Repair never consumes
Phase 0's (possibly sampled) report — it re-enumerates exactly. Audit rows:
`memory_reconcile_runs` (migration 0074). One-time historical cleanup was
d0008; `MemoryStore.delete()` is point-first + fail-closed (defers, returning
`{"deferred": True}` and recording a tombstone, when Qdrant is unavailable —
callers must honor it).

**Retrieval is TIERED — the hottest auto-fired paths carry the thinnest
stack.** Deep path: `memory/retrieval.py` `HybridRetriever.recall` (bitemporal
`invalid_at` filter, entrenchment, activation/decay, graph boost, diversity
penalty). The diversity penalty only shapes ORDERING —
`RetrievalResult.retrieval_score` carries the pre-penalty score and is what
J-9 quality logging reads (the MCP MEM-003 enrichment reads it too).
Easy-to-forget mechanisms:

- **CRAG** lives in the MCP-wrapper only (`memory/corrective.py`
  `maybe_correct_recall`; `top_score >= 0.75` skips grading) — not in
  `retrieval.py`.
- **Recall is read-MOSTLY, not read-only**: every hit bumps
  `retrieved_count`/`last_retrieved_at` in Qdrant + SQLite (retrieval stage
  11, the MCP drift fallback, `memory_core_facts`). Eval harnesses reading a
  frozen snapshot suppress this via `GENESIS_MEMORY_WRITEBACKS_OFF`
  (`env.memory_writebacks_off()`); any NEW write inside a read path must
  honor the same seam.
- **VoyageReranker** (`memory/reranker.py`, rerank-2.5) is API_KEY_VOYAGE-gated
  and wired into BOTH retriever stacks: the runtime context stack (long-standing)
  and — since the MCP `init()` had passed no reranker, so it silently never ran —
  the MCP recall tools. `recall()` defaults `rerank=True`; the recall tools
  (`memory_recall` / `knowledge_recall` / `reference_lookup`) rerank subject to
  `reranker.mode` in `config/memory_recall.yaml` (off|live, live default) plus the
  `GENESIS_MEMORY_RERANK_OFF` kill, read live via
  `graph_expansion.reranker_enabled()`. The gate is applied at the MCP tool
  boundary — the three recall tools plus the CRAG corrective-augmentation path
  (`memory/corrective.py`), which runs inside those tools — so the internal
  runtime stack and the hermetic LongMemEval harness (both pass explicit
  `rerank` kwargs) are unaffected.
- **`drift_recall`** (`memory/drift.py`) is the degraded-mode fallback; its
  FTS drilldown searches every collection in `source_collections`,
  rank-merged across collections.
- The proactive per-prompt path is `scripts/proactive_memory_hook.py` — since
  #1169 a **thin HTTP client** of `POST /api/genesis/hook/recall`
  (`dashboard/routes/proactive.py` → `memory/proactive.py::proactive_context`
  → the shared `_proactive_impl` engine), with a keyword-only FTS5 fallback
  when the server can't answer. Latency budget: 4.5s server / 4.75s client
  (sized to the production engine's measured cold path — embed + a 1.0s
  rerank timebox + retrieval under load; see the #1169 timeout
  investigation), inside the hook wrapper's 10s ceiling. The
  `memory_proactive` MCP tool shares the engine but stays unfiltered/
  un-reranked.
- `procedure_recall` deliberately uses Jaccard tag-overlap
  (`learning/procedural/matcher.py find_relevant`), not hybrid retrieval.
- External-world recall results are provenance-wrapped (`wrap_external_recall`)
  — first-party memory vs knowledge-base is a load-bearing distinction.
- **Entity layer (WS-H Pillar 2)** — typed entity nodes with identity:
  `entities`/`entity_mentions`/`entity_links` tables (migration 0051),
  `db/crud/entities.py` (Python frontier BFS — one flat `IN`-list query per
  depth level, NOT a recursive CTE — bi-temporal edge validity,
  EXTRACTED/INFERRED/AMBIGUOUS provenance, `merge_entity` tombstone-with-
  redirect), `memory/entity_registry.py` (string→ID resolution tiering; fuzzy
  matches queue `entity_adjudication`), `memory/entity_seed.py` (curated spine
  incl. the repo-split rule). **Adjudication drainer**
  (`memory/entity_adjudication.py`, migration 0065 `entity_adjudications`
  ledger): the hourly consumer of the `entity_adjudication` queue — a mechanical
  digit-guard rules out numeric-suffix pairs, then a two-model LLM judgment
  (`entity_adjudication` + flipped-provider `entity_adjudication_challenge`, both
  must agree) decides merge-vs-distinct. `propose_only` by default (records, does
  not apply); `live` applies via `merge_entity`. A cursor-managed reconcile sweep
  rediscovers historical fuzzy pairs. Settings lever `entity_adjudication`
  (off/propose_only/live) + `GENESIS_ENTITY_ADJUDICATION_DISABLED`. Distinct from
  `memory/entity_resolution.py`, which is near-duplicate memory-PAIR dedup.
  **Human-approval apply gate** (`entity_adjudication_approve`/`_apply`/`_reject`
  MCP tools + `approved_at`): a `proposed_merge` applies only after a human
  approves. Apply runs per-row on an owned `get_raw_db()` `BEGIN IMMEDIATE`
  transaction — re-checks identity/direction/norm staleness inside the lock,
  atomically claims (`proposed_merge`→`merge`) so concurrent appliers can't
  double-merge, and marks stale *conditionally* (only while still
  `proposed_merge`) so a losing applier can't clobber an applied merge or a human
  `distinct`. Approval is invalidated at the write boundary when a re-adjudication
  changes the approved direction/identity/norms (`record_verdict`), and the three
  write tools refuse in a dispatched/unsupervised session
  (`is_dispatched_session_env`) so no autonomous path can self-approve an
  irreversible merge. Bitemporal timestamps are canonicalized at the write gate
  (`db/timeutil.canonical_iso`, migration 0050).

**Consolidation (dream cycle)** — `memory/dream_cycle.py` (~1480 LOC):
weekly clustering (Sun 4am) persists a value-ranked worklist to
`deferred_work_queue` (`work_type="dream_synthesis_slice"`); a daily drain
(8am) processes a budgeted top-value slice. Destructive merges are gated on
`GENESIS_DREAM_CYCLE_LIVE` (env var, NOT a config key) and the drain is
**shadow-hardwired** (`dry_run=True`) — the live flip is a separate user-gated
change (#892). `_CapacityBreaker` aborts on consecutive provider exhaustion.
`_cross_wing_scan` writes `memory_links` even under dry_run — intentional
additive layer, not a leak.

**Importance shield (Phase 2c)** — `memory/dream_shield.py` +
`memory/dream_shield_config.py` (config: `config/dream_shield.yaml` + local
overlay; env kill-switch `GENESIS_DREAM_SHIELD_DISABLED`). Before enqueue, the
weekly pass removes high-salience members from clusters (skip-member) so the
merge path never consolidates them away: shielded if activation ≥ collection
percentile (production `compute_activation`), OR raw confidence ≥ a floor
(default 0.98, above the 0.95 extraction-default spike), OR betweenness
centrality ≥ the nonzero percentile. Thresholds freeze into each slice; the
drain re-checks live members via `shield_filter_live` (catches salience that
rose mid-week) and counts `shield_missing_thresholds` — which must be 0 before
any live flip. Centrality persistence widened from top-500 to all-nonzero
(`dream_centrality.py`, `graph.centrality_scores(top_n=None)`) so the shield has
a real bridge-node population; `centrality_cache` gains its first reader.

**Merge link rewiring** — the live merge (`_synthesize_and_deprecate`) COPIES
each original's external `memory_links` edges onto the synthesis
(`memory_links.copy_external_links`) so they don't dangle on the soft-deleted
original; COPY not MOVE keeps rollback reversible (it hard-deletes the
synthesis's links). The originals' now-stale edges are aged out by
`dream_link_repair`'s second pass at `deprecated_edge_prune_days` (config,
default 30 — must exceed the rollback review window), preserving ONLY the
synthesis→original provenance `extends` edge (ordinary `extends` from
`auto_link` is pruned). Age comes from the authoritative `deprecated_at`
column stamped at merge (the synthesis's `created_at` is unreliable —
`store()`'s exact-dedup can return an old pre-existing memory); non-dream
deprecations leave `deprecated_at` NULL and are never pruned.

**Relationship classifier (MW-2 lean keystone)** —
`memory/relationship_classifier.py`: coarse relationship judgment
(`duplicate`/`contradicts`/`succeeded_by`/`distinct` + confidence) between two
candidate memories, generalizing `entity_resolution.check_semantic_overlap`
(call site `dream_cycle_relationship_classify`, free-SLM chain, fail-safe
`distinct@0.0`). This is the function MW-5's merge gate consumes on ≥0.95
nominees; NOTHING calls it in the runtime yet (probe:
`scripts/dev/mw2_classifier_probe.py`, read-only vs a DB copy). Migration 0082
added five NULLable stamping columns to `memory_links` (`proposed_type`,
`confidence`, `classifier`, `review_state`, `safe_for_boost`) — NULL
`safe_for_boost` = boost-eligible (legacy default), no backfill, CHECK/PK
unchanged. Measured 2026-08-11 (n=300 sampled from the FULL ~197k-pair
similarity population — strength ≥0.75, synthesis-provenance excluded by
source marker, unordered-pair-deduped, boost-visibility-filtered, 0
fail-safes): 73.2% distinct / 16.4% duplicate / 10.0% succeeded_by / 0.4%
contradicts; ~17% of sampled edges had recall-hidden endpoints. `duplicate`
~80% hand-scored accurate, `contradicts` OVER-CALLED (bug↔fix pairs) — so
MW-5 must
challenge contradicts verdicts adversarially before acting, and the deferred
MW-2b machinery (candidate_similar type + CHECK rebuild + write-path change +
boost gating) is evidence-parked, not planned. The similarity linkers
(`linker.py` auto_link 0.90/0.75, `connection_pass.py` 0.80) still write
`extends`/`supports`/`related_to` — types recall ranking never reads
(`neighbors_of` is strength-only; type matters only via the `contradicts`
deny-list).

**Do not touch:** the drain's shadow hardwiring; the dry_run-independent link
write. **Trap:** with no embedding provider registered, memory silently
degrades to FTS5-only (see routing-providers entry).

**origin_class (WS-3 B0):** every store stamps
`owner | first_party | external_untrusted` into the Qdrant payload,
`memory_metadata`, and (KB paths) `knowledge_units` — derived in
`provenance.derive_origin_class` (explicit kwarg wins; external pipelines
outrank `source_subsystem`; `curated` is external BY DECISION — authority
tier, not authorship). Store-time derivation is conservative-first-party for
unknown internal writers; the fail-closed unknown→external rule lives only
at gate time (`security/immunity.py`). Migration 0053 backfilled history
(no owner heuristics); `scripts/backfill_origin_class_qdrant.py` mirrors the
payloads idempotently.

## 2. Execution — CC sessions (DirectSession)

Spawning, tracking, and recovering Claude Code sessions — Genesis's hands for
any task bigger than an LLM call.

```yaml subsystem-map
entry: execution-cc
modules: [cc]
verified: 29a382e7 2026-09-03
```

- **Roster peer availability is OBSERVATION, never a gate** (`cc/peer_availability.py`,
  recorded from the failover loop in `cc/conversation.py`). `roster.failover_chain`
  admits a peer on CREDENTIAL PRESENCE — "is `auth_env` set" — so a quota-blocked
  standby is indistinguishable from a healthy one in every self-report. This module
  records what the last real attempt showed, and the failover path still tries every
  peer in order regardless: a stale "unavailable" that suppressed a peer would drop a
  WORKING backup exactly when the home model is down. Two invariants worth keeping:
  only a PROVIDER REFUSAL (rate-limit/quota) is evidence about a peer — a local fault
  (offline, our timeout, an MCP crash, a stale session) is a `CCError` on the same
  branch but never reaches the provider, and `note_failure` declines it; and records
  refresh ONLY while the home model is down, so they are last-observed facts carrying
  `observed_at`/`age_seconds`, never current state. A third, learned across four review
  rounds: a self-report is an EXPOSURE surface, so the record stores NO FREE TEXT
  at all — every field is a closed set, and a row read from disk is either exactly
  what the module writes or it is DROPPED, never repaired. The state file is read
  into every health snapshot, reaches an LLM context via the health MCP tool, and
  is JSON-dumped whole into `sentinel/monitor.py`'s prompt with no sanitisation,
  so a text field here is a text field in an LLM prompt; the provider's prose goes
  to the log at the point of failure instead. That is a deletion rather than a
  guard, and it was the answer to two generators that no amount of patching
  closed: per-field REPAIR of a foreign document (seven findings), and inferring
  whether credential discovery had succeeded from a config loader that degrades
  silently by design (two P1s).
- **Failover records availability only when a peer DEMONSTRABLY served** — a
  usable output, or answer text already streamed to the user. The degenerate
  empty non-error output (a silent cap) keeps its long-standing behaviour and is
  simply NOT recorded, so it cannot clear a real block with a turn that showed
  nothing. What to DO about that empty reply — advance to the next peer, or stop
  because this attempt may already have executed tools whose effects a re-run
  would repeat — is retry policy, and it lives on the `feat/peer-effects-guard`
  branch as its own change with its own review, together with the effects
  tracking it needs and the provider-refusal classification redesign (a closed
  allowlist rather than per-case exclusions).
- **`StreamEvent.from_raw` is lossless on the live stream surface** — CC's
  `stream-json` emits ONE content block per `assistant` line, so the
  first-recognized-block parse drops nothing. MEASURED 2026-09-04 against CC
  2.1.246, 8/8 lines across two probes, 0 multi-block — including a
  thinking→text→tool_use turn and three PARALLEL tool calls, which the API packs
  into a single message and the CLI splits across three lines. The version is
  part of the claim: this is a property of a CLI build, and eight lines is a thin
  denominator for a negative property across versions and modes. The assumption
  is RECORDED by a canary in `invoker.run_streaming` (counting via
  `StreamEvent.recognized_blocks`, which lives beside the loop it mirrors so the
  two cannot drift) — a log line nothing polls: it makes a future batching CC
  diagnosable in one grep, it does not alert. If it ever fires, `invoker`'s
  `collected_text` recovery and every `event_type` consumer need revisiting.
- **Cross-session awareness — how concurrent CC sessions perceive each other.**
  LIVE. Two DISTINCT stores answer two different questions, and conflating them
  is the trap: `cc_sessions` (+ a `/proc` walk, `observability/cc_slots.
  enumerate_cc_slots`) is what the DASHBOARD renders; `session_heartbeats` is
  what SESSIONS tell each other. They share no source and neither substitutes
  for the other. `session_heartbeats` has exactly ONE reader in the tree —
  `scripts/proactive_memory_hook.py`, which prints a `[Concurrent | …]` tag into
  each peer session's context on UserPromptSubmit — so it is an AGENT-ONLY
  channel with no human surface. Written by that same hook and refreshed
  (liveness only, `updated_at`, at most one write per minute per session --
  the throttle is a stamp FILE whose contents carry the claim time; the flock
  around it resolves the parallel-tool-call race, it is not itself the
  throttle) from `scripts/hooks/session_observer_hook.py` on
  PostToolUse, because `get_active_sync` hides any row older than ten minutes
  and a heads-down session would otherwise vanish from its peers while busiest.
  **Field-source rules, each derived from measurement, not assumption:** the
  rendered `topic` is whichever of two sources is the more RECENT statement of
  what the session is doing: `cc_sessions.topic` (written by
  `memory/extraction_job.py` on a multi-hour cycle) or the charter's mission
  (set the moment a session declares a pivot), decided by comparing
  `cc_sessions.topic_updated_at` against `session_charters.mission_updated_at`
  (migration 0091). BOTH columns are new, and the second one is why:
  `last_extracted_at` is a PASS watermark the extraction job advances even on
  passes that write no topic (219 of 899 live rows carry a watermark with no
  topic), so using it as the topic's age would commit on that side the exact
  defect `mission_updated_at` exists to avoid. That column exists because `updated_at` CANNOT answer it — it is a row
  timestamp that `set_pointers` and the charter upsert also bump, so a pointer
  edit would promote a stale founding mission. Timestamps are compared PARSED, not
  lexically — for heterogeneous offsets and naive stamps, NOT for the
  microsecond reason an earlier draft gave (measured: 0 disagreements over
  200,000 same-format pairs). When the comparison is impossible — a pre-0091 row whose
  mission age is genuinely unknown, an unparseable stamp, or no extraction to
  compare against — the extracted summary keeps precedence, which is both the
  safe direction and the pre-0091 behaviour, so the migration is inert until a
  mission is next set. Then the mission regardless of age, then its newest live
  ledger item; the raw first user message is NEVER a fallback and the peer's
  typed prompt is never rendered at all, since another session's user text is
  decontextualised in yours. `model` comes from the SessionStart cache with
  `GENESIS_ROSTER_MODEL` taking precedence, NOT from `cc_sessions.model` — on a
  live install that column is dominated by the literal string `unknown`.
  Liveness comes ONLY from `session_heartbeats`: `cc_sessions.pid` is unset for
  most rows and its `status`/`last_activity_at` go stale on running sessions, so
  it must never be used to decide who is alive. **Every rendered field is
  peer-authored, so all pass through `sanitize_detail`** — a newline in any of
  them forges an extra `[Concurrent | …]` line in the reader's context. The
  writers are partial by design (each knows a different subset), so the upsert
  COALESCEs content columns; a writer distinguishes "read fine, nothing to
  report" (empty string — CLEARS) from "could not read" (None — PRESERVES), and
  collapsing those two is what makes a finished topic immortal.

- **Subagent-spawn lockdown — one source of truth across the restricted sessions**
  (`cc/types.SPAWN_TOOL_NAMES = ("Agent", "Task", "Workflow", "Skill")`). A restricted
  session escapes its tool restrictions if it can spawn a child that inherits a fresh,
  unrestricted toolset. `Agent` is the current CC tool name; `Task` the obsolete alias;
  `Workflow` orchestrates subagents; `Skill` can run in a subagent. The denylists had
  drifted (sentinel-degraded blocked the class, reflection blocked all but `Agent`,
  inbox/mail/experimentation blocked only a subset); those sessions now all reference
  `SPAWN_TOOL_NAMES` — reflection (`_REFLECTION_DENY_BUILTINS`), the inbox/mail judges,
  and the experimentation completion (each asserted against the LIVE `--disallowedTools`
  value, not an orphaned constant). `sentinel/dispatcher._DEGRADED_DISALLOWED_TOOLS` keeps
  its own literal (deliberate import-cycle avoidance) but is held ⊇ by
  `tests/test_cc/test_spawn_lockdown.py`, which locks the class so a new denylist that
  forgets spawn-blocking fails CI. The mail judge — whose prompt uses NO tools — now runs
  an ACT-NOTHING denylist (Bash + all file-edit + spawn + side-effecting actions + web
  tools, closing the Read+WebFetch exfil path), and its empty-MCP config is resolved via
  the canonical `build_mcp_config("none")` (the old hand-counted `parents[2]` path pointed
  at a nonexistent `src/config/no_mcp.json`). The inbox judge denies every genesis MCP
  *write* (`memory_store`/`settings_update`/`follow_up_create`/…) via
  `build_reflection_disallowed` minus `Bash`, keeping the reads it needs plus the optional
  `observation_write`. RESIDUALS on the inbox judge: (1) `Bash` — STILL retained for the
  `yt-dlp`/`curl` YouTube-fetch path (injection→RCE surface, open — follow-up 727a3724);
  (2) the PRIVILEGED-WRITE consumers of forged observations are now gated (the
  memory-provenance work): `observation_write` stamps the session origin
  (`session_origin_from_env`) so an eval-session write lands `external_untrusted`, and BOTH
  user-model consumers — `process_pending_deltas` (accept) and `synthesize_narrative`
  (evidence → USER_KNOWLEDGE.md) — read only trusted-origin deltas via the SQL
  `origin_class_in=TRUSTED_PRIVILEGED_WRITE_ORIGINS` filter (filtering in-query, not
  post-LIMIT, so a forged-delta flood can't starve trusted deltas out of the window, and a
  TTL-resolved barred delta can't launder into the narrative). Fail-closed on NULL; barred
  rows are left for the 14-day TTL, never discarded. The autonomy-dispatcher `task_detected`
  pickup applies the same trust check (`immunity.is_trusted_for_privileged_write`) SKIP-ONLY
  — it refuses dispatch without resolving/hiding the row (the path is inert today; producer
  stamping is in the follow-up).
  **WS-3 observation write-provenance + laundering-critical read exclusions (PR-1, built
  2026-08-22):** origin is now definite at the WRITE boundary — the CRUD chokepoint
  (`db/crud/observations.py` `create()`/`upsert()` → `derive_observation_origin`) classifies
  EVERY caller (explicit arg → session env → source map → **NULL fail-closed**), so the
  observation `source` space (uncurated, ~200 live values) is NON-load-bearing for security: a
  missed/novel external writer degrades to NULL → excluded, never trusted. Channel-attended
  `task_detected` writes stamp `owner`/`external_untrusted` via `cc.types.task_detected_origin`;
  the two stdlib-only raw-INSERT hooks (`proactive_memory_hook` pivot writer,
  `emit_bugfix_audit`) stamp origin inline. Migration 0085 backfills historical rows. On the
  READ side, the LAUNDERING-CRITICAL surfaces now EXCLUDE external/unknown-origin content
  (fail-closed: `origin_class IN ('owner','first_party')`, NULL excluded) —
  `essential_knowledge._recent_decisions` + `_active_session_pivots` (the always-loaded L1
  file), `reflection.gather_evaluation_context` (×4) + `_recent_observations` +
  `gather_for_calibration`, and `perception.context._build_memory_hits` + `_build_light_chain`
  (the deep-reflection/perception prompt pipeline) — severing the external→reflection→
  `first_party`-stamped-delta laundering path, with a deep-reflection prompt-rule belt and a
  write-side coverage guardrail (`tests/test_security/test_observation_surface_coverage.py`)
  that fails CI on any raw `INSERT INTO observations` bypass. The gate-4 pushed-surfaces
  `supervised` exemption is now channel-aware (owner-attended only). Session-origin coverage
  is durable across the gateway class: `get_or_create_foreground` (web/OpenClaw) and
  `register_voice_session` (voice) stamp `external_untrusted` on the `cc_sessions` row, so
  `reflection_window_origin` cannot read a NULL as first-party and launder an overlapping
  reflection's `user_model_delta` (0085 also grandfathers pre-deploy gateway/voice rows). The
  learning triage pipeline's per-session `retrospective`/`cc_debrief` observations are stamped
  with the analyzed conversation's channel origin (`cc.types.observation_origin_for_channel`),
  forwarded to BOTH the observations row and its memory-store copy — an inbox/mail session's
  debrief learnings can no longer surface first-party; and the coverage guardrail now follows
  the indirect `ObservationWriter.write` path + module-constant sources. On Telegram
  quote-replies, slash-command/task intent is parsed from the owner's own reply only (not the
  quoted composite), closing the `/task`+`/model`+`/effort`+`/resume` forge via quoted bot
  text. **STILL PARTIAL (PR-2, tracked follow-up):** the read-side breadth — the ~28 ego/sentinel/guardian/surplus/dashboard/
  MCP reads that should WRAP (not exclude) external content in a shared `render_safe` renderer,
  plus a read-side discovery guardrail and the boundary-marker zero-width normalization on the
  wrap path.
  **Out of scope (tracked follow-up):** `cc/direct_session` (its `research` profile runs a
  DOCUMENTED deep-research `Workflow` — blocking the class there would break that path) and
  the autonomy-executor sessions; those legitimately spawn/orchestrate and need a
  sandbox-preserving Workflow or an accepted-porousness decision.

- **Reflection tool lockdown — read-only + observations only**
  (`session_config.build_reflection_disallowed`, wired at `reflection_bridge/_bridge.py`
  into the reflection `CCInvocation.disallowed_tools`). Deep/strategic reflections run
  with a DERIVED denylist = (live `genesis-health` + `genesis-memory` registry − a read
  allowlist − `observation_write`) + the write/action built-ins (Bash/Write/Edit/Agent/
  Task/Workflow/Skill/…), so a future write tool is auto-denied. `--allowedTools` is NOT a
  strict allowlist under `--dangerously-skip-permissions` (verified empirically 2026-08-07
  via the init-event tool list — it left Bash available); only `--disallowedTools` removes
  a tool, so the scoping is a denylist. Guards (`tests/test_cc/test_reflection_tool_scope.py`):
  a sentinel asserts known write tools stay denied, a static AST scan rejects any
  write-shaped tool in the read-allowlist, and a fail-closed check confirms every
  registered tool is read-allowed or denied (a new upstream tool is auto-denied by the
  derivation). Reflection previously bypassed
  all tool scoping (the orphaned `build_reflection_config`/`_READONLY_DISALLOWED` was never
  wired) — root cause of a 2026-07-03 fabricated follow-up. Sacred-board companion: an
  autonomous/dispatched session's `follow_up_create` (source=`ego_dispatch`) is routed to
  the cold `tabled` lane, never the hot board (`mcp/health/follow_up_tools.py`).

- **Secure-by-default MCP scoping (ALL autonomous sessions)** — `CCInvocation.
  strict_mcp_config` defaults to **True** (`cc/types.py`), so `--mcp-config` is
  authoritative and the operator's user-scoped `~/.claude.json` servers (additive
  without strict — probe-verified 2026-08-09) never leak into a background session.
  `_build_args` suppresses the flag under `--bare` (bare+strict exits non-zero,
  probe-verified). This is the class fix behind the reflection lockdown above:
  ~15 autonomous `CCInvocation` sites (reflection, sentinel, inbox/mail, direct_session,
  ego cycle, autonomy executor/research) were leaking user-scoped MCP write tools.
  Foreground/interactive sites (`cc/conversation.py` ×3, `cc/checkpoint.py`) opt out
  with `strict_mcp_config=False` to keep the full toolset; a forgotten site fails
  closed (zero MCP), never open. The weekly reflection arms
  (`run_weekly_assessment`/`run_quality_calibration`) now route through the shared
  `_reflection_lockdown_kwargs` helper alongside `_reflect_inner` (they previously ran
  unrestricted). `autonomy/executor/step_dispatcher` pins CODE/VERIFICATION steps to
  the genesis-only `reflection` MCP profile (keeps memory/health, drops the
  arbitrary-source-edit servers; code edits flow through the audited Write/Edit
  built-ins). Defense-in-depth: `_USER_SCOPED_MCP_WILDCARDS` denied by name in
  `_UNIVERSAL_DISALLOW` + the reflection denylist. Guards:
  `tests/test_cc/test_mcp_strict_default.py`, `tests/test_cc/test_invoker.py`.

- `cc/direct_session.py` + `cc/conversation.py` (both >1000 LOC; split
  candidates). Profile machinery: `PROFILES`, `_PROFILE_ADDENDA`,
  `_PROFILE_SKILLS`, `_PROFILE_TO_MCP` (direct_session.py) +
  `session_config._MCP_PROFILES` (profile → MCP-server allowlist).
- **Spawn autonomy circuit breaker** (direct_session.py ~:600-635):
  `bayesian_posterior < 0.15 and total_corrections > 3` blocks non-foreground
  dispatch — flagged for review as a visible lever (Design Principle 3).
- Recovery: `recover_stale_claims` on boot (queue claims); the
  `session_reaper` job on the **learning** scheduler (CronTrigger every 6h
  + a boot-time kick) routes through `SessionManager.cleanup_stale` —
  stale non-foreground 'active' rows → `expired` (outcome unknown),
  end-hooks fired. Known interruptions record `failed`: `_run_session` has
  an explicit `CancelledError` handler, and `GenesisRuntime.shutdown()`
  cancel-and-awaits the runner's in-flight tasks (`DirectSessionRunner
  .shutdown`, 10s grace) BEFORE closing the DB so that handler can persist
  (2026-07-09; the old crud `reap_stale`, which relabeled orphans
  'completed', is deleted). J-9 counts only `completed` as success.
- **Foreground liveness (D3)** (`cc/foreground_reaper.py`): foreground rows stay
  `active` by design so the next turn can `--resume`, so a crash/restart mid-turn
  leaves one `active` forever — `query_stale` excludes foreground, so nothing
  reaped them. The `session_reaper` job now ALSO runs `reap_dark_foreground`
  (its own inner try/except — a notify failure can't abort stale-session
  cleanup): foreground rows idle >24h → `checkpointed` via `checkpoint_dark`
  (non-destructive — `get_active_foreground` matches `checkpointed` and
  `get_or_create_foreground` flips it back to `active` on reuse, so `--resume` is
  intact). On the CRISP "unanswered user turn" transcript signal (age-guarded to
  exclude a mid-flight turn, and not already covered by a rate-limit park or
  dispatch) it notifies the origin user their request was interrupted; the FUZZY
  "I'll report back" signal is shadow-logged only until its precision is measured.
  Observability-only (never re-dispatches). Lever: `cc_foreground_reaper`
  (`off|observe|notify`, default `notify`) + `GENESIS_FOREGROUND_REAPER_DISABLED`.
- **Perimeter-session hardening:** `_NO_WEB_TOOLS` / `_NO_OUTREACH_EXTRAS`
  blocklists strip risky tools from perimeter profiles — a security edge, not
  configuration convenience.
- `cc/context_injector.py` (memory→session injection) lives HERE, not in
  memory. GROUNDWORK: `reflection_bridge/_bridge.py` (v4-executor),
  `session_config.py` (hook-inheritance).
- **Background-wait ceiling ownership** (invoker.py): the CLI's headless
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600s) SIGKILLs a dispatched
  Workflow/subagent mid-run with a partial result. `CCInvocation.bg_wait_ceiling_ms`
  now owns it, clamped below `timeout_s` so graceful truncation precedes the hard
  kill; the background lane (direct_session) sets it to the full budget so long work
  (deep-research) runs to completion. A hit sets `CCOutput.bg_truncated` → a visible
  user notice + a `cc.bg_truncated` event. Foreground turns keep the 600s default so a
  conversational turn never lingers holding the per-session lock, and instead route long
  research to the background lane (`conversation._BG_RESEARCH_ROUTING`, gated on
  `origin_delivery_supported` = Telegram-only, matching the delivery resolver, via
  `direct_session_run(profile="research", deliver_to_origin=true)`).
  Origin: the 2026-07-20 silent-death of a Telegram deep-research run.
- **Background-session delivery model** (`DeliveryMode`, direct_session.py): a
  handed-off task can deliver its terminal outcome (success AND failure) back to the
  conversation it was dispatched from. `direct_session_run(deliver_to_origin=True)`
  captures the foreground origin via `GENESIS_SESSION_ID` (the foreground `cc_sessions`
  row id the health-MCP child inherits), threaded through the queue onto the request;
  `DirectSessionRunner._deliver_result_to_origin` resolves the origin's channel+thread
  and delivers a targeted send (`OutreachRequest.target_chat_id`/`target_thread_id`,
  honored in `_deliver` before category routing — DM or forum topic). Legacy callers
  (all 8) derive `SILENT`/`FAILURE_ONLY` from their notify bools → unchanged. Fixes the
  latent bug where a successful background result was saved but never sent.
- **Rate-limit park + auto-resume** (`cc/rate_limit_park.py`, `cc/rate_limit_reset.py`,
  `crud/cc_rate_limit_parks.py`, config `cc_rate_limit_resume`): the durability half of
  the same 2026-07-20 incident. When a CC call hits a rate/usage limit, the work is
  PARKED (a `cc_rate_limit_parks` row — the durable lineage object) instead of dying —
  foreground turns (after failover+contingency both fail) and background `direct_session`s
  (a new catch before the generic `except`). The park stores a parsed `reset_at`
  (`rate_limit_reset.parse_reset` off the annotated `CCRateLimitError.raw_event`/`raw_text`,
  previously discarded); `cc_sessions.rate_limit_resumes_at` finally gets a producer, and
  the foreground copy is mode-aware (no longer promises resume nothing backed). A
  `CronTrigger` engine (PR-2b) re-dispatches the actual parked work at reset via the
  delivery model (`delivery_mode=result` + `origin_session_id`); a still-limited retry
  re-limits its OWN park in place (attempts+1, backoff) — resolved by `park_id` threaded
  in `caller_context="rate_limit_resume:<id>"` — so the `needs_user` escalation stays
  reachable. Gate posture: a resume completes already-approved work (a foreground prompt,
  or an already-gated dispatch), so it is not new autonomous initiative. Reflection-bridge
  and the task executor deliberately do NOT park (periodic self-retry / already durable via
  `task_states`). Resume engine (`cc/rate_limit_resume.py`, `_wire_rate_limit_resume`
  on the learning scheduler, `CronTrigger */10`): reclaims stale claims → lists due
  parks → claims + re-dispatches each via the queue (`delivery_mode=result`); the
  re-run self-validates (re-limit → its own catch re-parks with backoff); exhausted
  parks escalate to `needs_user` with a governed (`rate_limit_park` signal) alert.
  Lever `cc_rate_limit_resume` (off|propose_only|**live**, default live) +
  `GENESIS_RATE_LIMIT_RESUME_DISABLED`. Because the resume rewrites `caller_context`
  to `rate_limit_resume:<park_id>` (needed for that re-limit lineage), the ORIGINAL
  dispatch context (`ego_proposal:<id>`) is preserved separately on
  `DirectSessionRequest.origin_caller_context` — threaded park payload → queue →
  request → session metadata — and the three ego-dispatch consumers (proposal-outcome
  recording, the post-exec auditor gate, the on-end follow-through hook) resolve back
  to it via `rate_limit_park.effective_caller_context`, so a resumed ego dispatch's
  outcome, audit, and follow-through survive the park→resume (closed 837f8b63).

## 3. Autonomy & egress gating

Every autonomous action on the outside world funnels through deterministic
in-code gates. Owner-facing delivery (Telegram/voice/email-to-owner) is NEVER
gated — that contract is one-directional.

```yaml subsystem-map
entry: autonomy-egress
modules: [autonomy, outreach, distribution, content, campaigns]
verified: 5808e7cd 2026-09-03
```

- **The chokepoint is `outreach/pipeline.py _deliver`** — ~12 send paths
  converge there. `EmailAutonomyGate` (`autonomy/email_gate.py`, WS-8
  capability cells) sits below the LLM tool layer, unbypassable: HOLD writes
  the `approval_requests` row FIRST, then `pending_email_sends`; the
  `email_gate_watcher` job (every 5 min, learning scheduler) drains approved
  sends.
- **Discord is shadow-gated** (`autonomy/shadow_gate.py`): three doors —
  `pipeline._deliver`, `outreach_poll` webhook, discord-bot `send_reply` —
  observe-only into `capability_shadow`, best-effort so it can NEVER break the
  real send. Retention-pruned >45d via `scripts/prune_capability_shadow.py`
  (disk-hygiene), mirroring the immunity shadow store. Enforcement
  (hold-for-approval) is the designed next stage. CI
  backstop: `scripts/check_external_io.py` fails on new ungated egress
  endpoints.
- **The Contributor Work-Log posts public GitHub issues, gated like email.** A
  server-side MCP tool (`contributor_issue_propose`, genesis-health) sanitizes a
  curator-drafted issue via the fail-closed `contribution/sanitize.py scan_prose`
  (title+body+labels — every string that egresses) and enforces the label policy
  ON THE CONFIGURED PUBLIC TRACKER (fail-closed, AFTER the scan: a proposal missing
  an `area:*` domain label or a difficulty/environment label —
  `_AREA_LABELS`/`_ENV_DIFFICULTY_LABELS` — is `rejected` with no row; a
  human-approved cross-repo post is not subject to the policy), then HOLDS it: the
  `approval_requests` row FIRST, then `pending_issue_posts` (mirroring the email
  gate). By default each hold is per-item owner-approved on the dashboard (excluded
  from `approve_all_pending`, like email). **Autonomous posture (opt-in,
  `require_approval: false`):** the propose tool resolves its OWN approval
  server-side (`resolved_by="genesis:contributor-worklog"`, fail-CLOSED — only an
  explicit boolean false disables the human gate; default true so a fresh clone
  never auto-posts), so the curator posts without a human — Genesis is the vetting
  authority (scan_prose + the rate cap). The `contributor_issue_watcher` drain
  (every 5 min, learning scheduler) resolves approved holds under the
  `contributor_worklog` mode lever (`autonomy/contributor_worklog_config.py`,
  default `propose_only`): `live` → `gh issue create` (shadow-gated door
  `observe_github_issue_create`, `mark_posted` BEFORE `mark_consumed` +
  pre-post `gh issue list` dedup for idempotency), rate-limited by
  `max_posts_per_day` (rolling-24h `count_posted_since`, per-row before the create
  so N approved rows in one tick are bounded; cautious-rollout brake); `propose_only`
  → dry-run terminal (never posts). The STOP is `mode: off` / the env kill (freezes
  the drain, incl. approved held rows). Terminal rows pruned >30d via
  `scripts/prune_contributor_issue_posts.py`; held rows never pruned. The
  curator campaigns are LOCAL user data (uncommitted).
- **Cold marketing-email substrate (PR1) — gated like email, recipient resolved
  in CODE.** The `marketing_send` MCP tool (genesis-outreach) stages a cold
  marketing email to a recipient resolved from the owner-curated
  `marketing_prospects` store by `prospect_id` — the tool takes NO address
  parameter, so the LLM never supplies a recipient. It refuses unless the
  `marketing_outreach` lever (`outreach/marketing_config.py`, default `off`,
  invalid→off, env kill `GENESIS_MARKETING_OUTREACH_DISABLED`) is enabled, the
  prospect exists, is not opted-out (PERMANENT suppression, never pruned), and its
  address is RFC-shaped; then enqueues to `pending_outreach` with
  `labeled_surplus=True` (threaded through the queue so the drain rebuilds a BULK
  request). Every staged send still converges on the same `_deliver` →
  `EmailAutonomyGate` chokepoint: the BULK capability cell ships at ASK, so
  everything HOLDS for owner approval (ships INERT). A GRANTED bulk cell adds a
  deterministic scope guard (`_scope_guard_trip` g2): the autonomous recipient
  MUST be a curated, non-opted-out `marketing_prospects` row (authorization is
  DECOUPLED from send-lifecycle status, so a `contacted` follow-up still sends) —
  an unknown / opted-out recipient trips (`recipient_not_curated` / `opted_out`)
  → demote + hold (fail-closed). Graduation for the BULK cell rides the generic
  capability-promotion path (`capability_grants.detect_promotable_cells` — no
  risk-class filter → `email:send:bulk` qualifies once it has ≥5 owner-approved
  successes + posterior ≥0.70). **Contact-stamping (loop-fix):** on a CONFIRMED
  delivery a matching prospect is advanced active → `contacted` via one
  active-guarded CRUD (`marketing_prospects.mark_contacted_by_email`) called from
  BOTH delivery paths — the email-gate drain (`email_gate_watcher`) for a HELD →
  owner-approved send, AND `pipeline._deliver` for a GRANTED-cell autonomous send
  (which delivers inline and never touches the drain, so the fix survives
  graduation). Keyed on the RECIPIENT being a curated prospect, NOT `cell_risk_class`
  (a FINANCIAL-misclassified pitch is still stamped); a send that never delivers
  (dropped/expired/rejected) leaves the prospect `active`, re-eligible. Without this,
  `mark_contacted` had no caller and `list_active` re-returned a delivered prospect
  every tick. **DEFERRED (hot follow-up, before arming at volume):** an atomic
  per-prospect in-flight dedup + a per-window approval-flood cap + the batch-approval
  card — `marketing_send` has no autonomous caller until the campaign is armed.
- **Marketing reply → owner Telegram ping (notify-only).** When a GENUINE human
  reply lands (auto-responders + foreign senders already gated out by the
  reply→engagement bridge, `outreach/engagement.py`) AND the reply's recipient is
  a curated `marketing_prospects` row (`get_by_email` — NOT the thread
  `signal_type`, which the owner-approval resume path overwrites to
  `email_gate_resume`, so it never survives a held→approved cold send), the owner
  gets ONE brief `submit_raw` Telegram ping (`make_marketing_reply_notifier`,
  wired in `runtime/init/outreach.py`). Owner-facing, so never gated; best-effort
  (a non-DELIVERED ping is logged, not retried — the reply is already durably
  recorded in `outreach_history`/`email_threads`). Attacker-controlled reply
  fields are HTML-escaped + control-char-stripped (`_sanitize_ping_field`) because
  the outreach telegram path delivers with `parse_mode="HTML"` unescaped. The
  autonomous `ReplyHandler` auto-reply path is unchanged by this.
- **`content/egress.py gate()` is LIVE** in the pipeline: anti-slop scrub +
  PII scan for EXTERNAL channels and `content`-category drafts only. Never
  applied to owner channels — don't add them.
- `_NEVER_DISPATCH_ACTION_TYPES` lives in `ego/session.py`, not here.
- **`DistributionManager` is not dead code** — instantiated by
  `modules/content_pipeline`, but its autonomous publish path is
  GROUNDWORK(autonomous-distribution) dark; the live Medium path is the
  `content-publish` CC skill (browser automation).
- **campaigns/** ships infrastructure only — a hard public/private contract:
  campaign names/prompts/targets are USER DATA (DB + private backups), never
  tracked source; zero shipped defaults. `CampaignRunner` cron-ticks
  programmatic prechecks then dispatches DirectSessions; a 120s reaper
  reconciles finished sessions. Prechecks receive a `db` handle in their ctx;
  `github_activity_pending` uses it to gate a digest campaign to ticks with new
  unresolved `github_account_activity` observations (fail-open on a first run or
  missing handle) so a quiet window spends nothing.
- GROUNDWORK across the entry: cross-vendor-review, per-step-verify,
  trace-verify, task-verify (built, dark), outreach-voice,
  autonomous-distribution.

## 4. Scheduling & background work

Genesis's system jobs, surplus-compute usage, and deferred-work accountability.
Note: the *learning* package hosts the other big scheduler (see entry 10).

```yaml subsystem-map
entry: scheduling-background
modules: [surplus, scheduler, follow_ups]
verified: 50b79ffb 2026-09-01
```

- **Surplus generators are deliberately BLIND to `infrastructure_alert`
  observations (2026-07-16)**: `_gather_context` excludes them so a stale/
  unverified infra critical can never be amplified into an autonomous
  "self-unblock" action (the git-corruption false alarm). Infra problems
  reach the user via guardian/health-alerts/morning-report — not surplus
  brainstorming. Don't "fix" the missing context.

- `surplus/scheduler.py` (~790 LOC) is the system-job hub (dream cycle, recon,
  pipeline cycles, maintenance, code index, model evals…); job bodies
  live in `surplus/jobs/` (gates/runners/dream/gitnexus) and the dispatch
  pipeline in `surplus/dispatch.py`, with the scheduler keeping every method
  name as a thin delegate/facade. (The long-disabled `schedule_code_audit`
  job was removed 2026-07; `CodeAuditExecutor` + the CODE_AUDIT task type
  remain for dispatch/judge consumers and manual enqueue.)
  `dispatch_once()` is **idle-gated** — surplus tasks only run when idle;
  follow-up dispatch is deliberately NOT idle-gated.
- **Durability model:** no persistent jobstore — jobs are re-registered at
  every boot + CronTrigger + `misfire_grace_time`, backed by three durable DB
  queues (`surplus_tasks`, `dead_letter`, `deferred_work_queue`).
  **IntervalTrigger resets on restart** — anything >1h must be a CronTrigger
  (documented bug class). Boot sweeps reclaim orphans immediately: the
  surplus scheduler resets `running` rows at start() without burning
  attempt_count (restart ≠ task failure), and the learning init kicks the
  recovery orchestrator at boot. Both assume SINGLE-WORKER dispatch —
  re-gate on worker ownership if v4-parallel-dispatch ships.
- **`surplus/intake.py`** (intelligence intake: atomize → score → route)
  auto-ingests curated sources into the knowledge base with NO manifest gate —
  an INTENTIONAL bypass of the conversational confirm-first path; don't "fix"
  it. BUT only tasks in `types.KB_ROUTING_TASK_TYPES` (insight-producing +
  bookmark-enrichment) route to the KB; action/maintenance/monitor/pipeline-
  intermediate output is point-in-time OPERATIONAL TELEMETRY, gated OUT at
  `dispatch._route_insights` (before the gate it filled the KB with db-
  maintenance/eval reports — 71% surplus; d0006 purged the historical rows).
  `source_pipeline` is per-source (`intake._pipeline_for_source`): Genesis-
  authored → `surplus`/first-party; crawled recon/model/github/web →
  distinct labels classified `external_untrusted` (wrapped on recall).
- **`scheduler/` (top-level package) is the UserJobScheduler** — user-authored
  cron jobs (via MCP) that dispatch background DirectSessions. Distinct from
  `surplus/scheduler.py`.
- `follow_ups/` = accountability ledger + dispatcher (every 5 min) that turns
  follow-ups into surplus tasks; retention sweep on the learning scheduler. The
  `follow_up_create` MCP tool routes USER-OWNED and purely-local operational work
  here; Genesis-repo work goes to the public GitHub tracker as an issue (CLAUDE.md
  "Where deferred work goes"). Templated pipelines write via `crud.follow_ups.create()`
  directly and are governed at their own call sites. The
  `follow_up_create`/`update` MCP tools take a `work_state`
  (ready/blocked_on_trigger/deferred_cold) and DERIVE the hot(`follow_up`)/
  cold(`tabled`) lane, so priority never picks the lane; `blocked_on_trigger`
  requires a `revisit_condition` (nullable column on `follow_ups`).
- GROUNDWORK: v4-parallel-dispatch, v4-surplus-tasks, v4-rate-tracking.

## 5. Information intake & research

Everything that pulls outside information IN: knowledge ingestion, the inbox
drop folder, web search/fetch, recon jobs, and the research pipeline.

```yaml subsystem-map
entry: intake-research
modules: [knowledge, inbox, research, recon, web, pipeline]
verified: e425b35c 2026-08-21
```

- **knowledge/**: orchestrator + manifest + tree index. Content-hash gate
  (`has_unchanged_source`) makes re-ingest of changed sources re-distill;
  `remove_unit` tombstones a source when its last unit is deleted (invariant:
  only that method may tombstone). The conversational path
  (`knowledge_ingest_source` MCP) requires explicit user confirmation —
  contrast the intake bypass in entry 4.
- **inbox/**: file-drop monitor with approval-gated dispatch; phase order
  resume → detect → create → dispatch; `approval_key_stable=True` (ONE
  site-level approval key). The refresh path folds parked files into the
  batch so approvals fire once (#914). A pending approval is HELD until the
  user resolves it (no re-ask, no age-based cancel) and is auto-cancelled only
  when *orphaned* — no live inbox row (`awaiting_approval:`/`dispatching:`)
  still references it (`count_live_rows_for_approval`); this replaced the old
  4h staleness cancel that re-detected unchanged files and nagged. Coherence +
  URL-failure heuristics gate dispatch.
- **recon/**: scheduled intelligence jobs (release watch, model intelligence
  Sun 8am, models.md synthesis Sun 10am, GitHub discovery, skill-security scan
  via external NVIDIA SkillSpector). Emits findings for triage
  (`recon_findings`/`recon_triage`) — intelligence-only, never auto-acts.
  `account_activity.py` (`AccountActivityMonitor`, sibling to `ReconGatherer` —
  which is intentionally no-push) is the exception that DOES push: a 2h
  deterministic (no-LLM) poll for EXTERNAL activity on owned repos (issues, PRs,
  comments, discussions + their replies) that pings first-time contributors via
  `submit_raw` (pipeline lazy-resolved at tick — surplus inits before outreach).
  Keys on immutable `created_at` (not `updated_at`, which GitHub's `?since=`
  filters on) with a pre-poll watermark cursor, so an edited/closed old item
  never masquerades as new. Uses `run_gh_checked` so a failed poll never advances
  the cursor; an undelivered ping is held in a `github_ping_pending` marker and
  retried each tick until it lands. An account-level **notifications lane** (repo-
  independent, same tick, own sidecar cursor) additionally surfaces activity
  BEYOND owned repos — @mentions anywhere plus responses on the owner's OUTBOUND
  contributions (`reason=author` on non-owned repos; owner-repo author items are
  dropped as the deep-poll already has them) — resolving the actor via the
  notification's `latest_comment_url` and pinging immediately in `live`.
  `off`/`observe`/`live` lever + `notifications` reason-allowlist in
  `github_steward_config`.
- **recon/career_outreach.py** (`CareerOutreachMonitor`) — the recon entry that
  not only pushes but ACTS: a daily surplus-cron ACTUATOR driving a configured
  external career-agent module (SSH CC dispatch, declared in the install's
  `~/.genesis` module overlay) to stage first-touch outreach drafts into the
  owner's mail Drafts (the module never sends — owner clicks Send), then pushing
  ONE Telegram nudge for newly-staged drafts. Ships `off` (`career_outreach_config`
  `off`/`observe`/`live` + `GENESIS_CAREER_OUTREACH_DISABLED` kill). Bridge
  lazy-resolved at tick; `execute_operation` returns an error DICT (never raises)
  so a dispatch error surfaces → job-health failure; `check_health_cached` gates;
  absent-module → clean no-op (generic installs). The owner nudge lists the drafts
  THIS tick's auto-run staged (the engine's own `Staged` replies — no mailbox census,
  which is unreliable: a bare SSH `claude -p` read answers from priors, returning a
  false-empty list); a `career_outreach_nudged` observation is the per-company
  nudge-dedup ledger. Observe mode is a bridge-REACHABILITY probe (a minimal dispatch
  proving the `claude -p` hop answers — module `check_health_cached` alone does not,
  it passes even when the remote's model auth is dead), NOT a seeder. ONE daily job
  (`max_instances=1` is per-job; a second would race
  the single remote). Discovery-sweep driving is GROUNDWORK-deferred. The auto-run
  drives the engine's COMPLETE flow INCLUDING its own accuracy/verification gate
  (never bypassed) under a headless contract. The reply is classified ONCE into a
  closed outcome set (`classify_autorun_reply` → NoneLeft/Staged/VerifyFailed/
  ModuleError/ProtocolError) — every malformed shape collapses to ProtocolError, so
  the tick loop matches on outcome TYPE and never branches on raw payload (robust by
  construction). A gate-refused draft is `VerifyFailed` (NOT a job-health error); a
  same-company re-selection breaks the loop. Turn/timeout budgets (`dispatch_max_turns` default 80 via the ipc
  per-call `max_turns` override; `dispatch_timeout_s` default 900, capped 1800 by
  config + a 3600 SSH-adapter ceiling) cover the gated flow (research → draft →
  verify → stage), MEASURED ~5.5 min live.
- **web/**: stateless search (SearXNG primary, Brave fallback) + httpx fetch
  (50k-char cap), sanitizer-wrapped; consumed via importers (MCP web tools,
  research, recon, pipeline), not runtime init.
- **research/**: `ResearchOrchestrator` over the provider registry — read-only
  capability, no egress gate needed.
- **pipeline/**: tiered research collection → triage → elevation feeding
  capability modules (crypto/prediction); pause-guarded. It is research
  plumbing, NOT the cognitive pipeline.
- GROUNDWORK: vision-ocr (image processor).

## 6. Channels & interfaces

Every surface a human (or host process) talks to Genesis through.

```yaml subsystem-map
entry: channels-interfaces
modules: [channels, dashboard, mcp, hosting, browser, mail]
verified: 50b79ffb 2026-09-01
```

- **channels/**: adapter framework. Telegram (`bridge.py` =
  `genesis-bridge.service`, boots a full runtime — LEGACY FALLBACK ONLY:
  it yields at startup, exit 200, when the genesis-server process lock is
  held, because two runtimes dual-poll getUpdates and both write
  status.json; the server hosts the same adapter via
  `hosting/standalone.py`. The polling stall watchdog records liveness
  from successful EMPTY getUpdates round trips too —
  `LivenessHTTPXRequest` — so idle chat is not a stall); voice (HA,
  OUTBOUND-only — inbound voice arrives via `dashboard/routes/voice_api.py`;
  uses `media_player.play_media`, never `assist_satellite.announce` which
  reopens the mic); Discord webhook; email SMTP. All env-gated. "OpenClaw" here
  is only the MIT origin of the Telegram transport code. Device/edge-side voice
  software (firmware, esphome, S2S/ambient bridges, edge deploy) lives in the
  separate `GENesis-Voice` repo — `channels/voice/` here is only the in-runtime
  channel. **Voice memory surface** (verified 2026-08-11): the S2S session is NOT
  memory-blind — `genesis_bridge.get_system_prompt` injects USER.md identity +
  essential-knowledge Active Context, exposes an `ask_genesis` pull-recall tool
  (searches the EXTRACTED long-term index only via `HybridRetriever.recall`, so it
  lags the 1-2h extraction cycle and has NO recency path), and each conversation
  lands as a transcript + `cc_sessions(source_tag='voice')` row mined by
  `memory/extraction_job.py`. **Cross-session recency resume** (`voice_recency.py`,
  gated by `voice_recency_resume` — ships `off`, armed after live E2E): at session
  start `get_system_prompt` injects the age-stamped tail of the most-recent prior
  voice conversation, read DIRECTLY from the transcript via a sync `mode=ro` sqlite
  lookup keyed on `last_activity_at` (NOT status — the 300s idle reaper coincides
  with the edge's 300s reconnect cache, so a status filter would miss the just-ended
  session at the exact moment a new one starts), fail-closed to `""`.
  `cc_sessions.satellite_id` (added via `_migrate_add_columns`, not the base
  `CREATE TABLE` — mirrors `last_extracted_*`) persists the device for the optional
  `per_device` scope; default `global`.
- **dashboard/**: Flask blueprint at `/genesis` (~45 route modules);
  `_async_route` bridges sync Flask onto the runtime event loop; heartbeat
  thread detects degraded-but-alive Flask; web terminal.
- **mcp/**: 5 Genesis MCP servers (health, memory, outreach, recon,
  discord-bot) + external codebase-memory; profile→server allowlist lives in
  `cc/session_config._MCP_PROFILES`. `genesis-health` is the big one (~35 tool
  modules). `standalone_health.py` serves from `~/.genesis/status.json` when no
  live runtime (stale-but-functional).
- **hosting/**: the OUTER layer that calls the runtime. `standalone.py` is the
  default (`python -m genesis serve`; also hosts the OpenClaw
  `/v1/chat/completions` endpoint); Agent Zero adapter optional.
- **browser/**: profile/state layer only (persistent
  `~/.genesis/browser-profile`, `BrowserLayer` enum, pgrep patterns as the
  single source of process detection). The automation TOOLS live in
  `mcp/health/browser.py`.
- **mail/**: Gmail IMAP recon (weekly two-layer monitor: cheap-LLM briefs →
  CC judge, sanitizer-wrapped) + reply poller (4h) + `ReplyHandler` dispatching
  restricted `mail`-profile sessions. Sending is NOT here — all sends go
  through the outreach gate (entry 3). Trap: never default a recipient to the
  agent's own address (self-send loop).
- GROUNDWORK: unified-bridge, outreach-pipeline (channels/base.py),
  guardian-dialogue (dashboard health route).

## 7. Ego & self-model

The two autonomous decision-making egos and the identity documents that shape
them.

```yaml subsystem-map
entry: ego-self-model
modules: [ego, identity, deliberation]
verified: 975d3944 2026-08-31
```

- **Two egos, both LIVE**: user ego (CEO, Opus, MCP profile `user_reflection`)
  and Genesis ego (COO, Sonnet, profile `reflection`), sharing `EgoSession`
  (~108K). `EgoCadenceManager`: adaptive proactive cycles, morning-report cron,
  30-min mechanical sweep, goal-staleness scans. Review cadence + budget
  controls before adding call sites.
- **Questions channel (`questions[]` in both output contracts)**: the ego can
  ASK the user directly — no approval gate (asking is NOTIFY_USER-class).
  `_process_questions` → ONE sequential background task → outreach
  `submit_and_wait` (NOTIFICATION category, governance applies); reply →
  `origin_class="owner"` observation + reactive signal to the ASKING ego
  (`set_reply_signal_sink` → cadence `push_reactive_event`); timeout/
  undelivered → first-party observation. Reply waiter is in-memory — restart
  mid-wait orphans the question (accepted; late reply falls to triage).
  Sequential delivery is load-bearing: two concurrent waiters in one chat
  make standalone replies unresolvable (`resolve_scoped_pending`).
- **`capability_aggregator.py` → `capability_map` table** = per-domain
  self-confidence from up to 6 sources (inverse-confidence weighted; the
  Outcome-Bus feed is flag-gated, default OFF). Reads split by intent: `get_all` and
  `get_by_domain` are raw accessors; `get_prompt_rows` and `get_weakest` are
  prompt-facing and apply two bars — `MIN_SAMPLE_SIZE` combined samples (also
  enforced on write, one shared constant) and `STALE_AFTER_DAYS` behind the
  freshest row, and they break confidence ties on `sample_size DESC` (ties at
  exactly 1.0 are common enough to fill a top-15 on their own, so without it the
  rendered set is arbitrary). Note what `updated_at` means: when the AGGREGATOR
  last wrote the row, not evidence age. Only 3 of the 6 sources are windowed
  (ego_proposals / cc_sessions / outcome_events, 30d); intervention_journal,
  autonomy_state and procedural_memory are not, so domains fed only by those
  never age out — correct for present-tense state, a known wart for the
  journal's historical events. The honest uniform reading is "the aggregator
  stopped vouching for this row N days ago". Anchored on the freshest row that
  is USABLE — date-shaped, parseable, and not in the future. Freshest-row
  anchoring means a TOTAL refresh outage hides nothing; excluding future-dated
  rows from the anchor (inside the subquery, not by clamping afterwards) stops
  one clock-skewed row from defining the window and blanking everything. A
  PARTIAL outage is not covered. Nothing is ever pruned; rows below a bar stop being refreshed and
  stop being RENDERED as present-tense capability — `get_by_domain` and
  `count_all` still read them deliberately.
  Withheld rows are named on every exit of all three renderers THAT RENDERS
  DATA, not only the empty and deep ones (the query-error exit does not
  count them — that read already failed, so the count would too): a corrupt or future-dated row is just as excluded on the
  light branch, which renders no table and so gives the reader nothing else to
  notice the loss by (`unusable_note` is also what logs). Both depth-taking
  renderers HONOUR `depth="light"` — accepting the keyword and emitting the full
  table anyway silently bills the caller for fifteen rows it did not ask for —
  and they share one sentence (`_capability_render.qualifying_subset_line`),
  because on a branch that renders no table the sentence IS the whole claim and
  two copies drift into one qualifying its figures and the other not.
  This is the naming-trap twin of this document — unrelated to the subsystem map.
- Proposal pipeline (`proposals.py`): batch WHAT/WHY/HOW digests to Telegram,
  content firewall via `validate_batch()`, 6h digest rate-limit GROUNDWORK;
  `_NEVER_DISPATCH_ACTION_TYPES` blocklist lives in `session.py`. Dispatches
  record `follow_ups` rows for accountability. `integrity.py` chain-verify is
  GROUNDWORK, explicitly NOT wired.
- **Operate-vs-develop scope stamp (2026-08-06)**: the boundary is LLM-judged,
  structure-enforced. The realist emits a per-proposal `scope` (`operate` |
  `develop`) on every genesis-ego proposal (rubric = realist rule 8; the
  `autonomy.classify_domain` SELF_MODIFY/AUTONOMOUS_BUILD fast-path overrides
  it); the reconcile `revise` verdict re-scopes sharpened content (it authors
  content pre-realist). Persisted on `ego_proposals.scope`/`scope_revision`
  (migration 0078; genesis-ego pending rows that passed the realist were
  grandfathered `operate`). Deterministic enforcement at the chokepoints every
  proposal crosses: **create** (`create_batch` — unstamped genesis draft
  DROPPED fail-closed; `develop` + self-dev-disabled created-then-TABLED;
  `operate` persisted), **revise** (`_reconcile_revise` refuses to apply an
  unscoped/develop-when-disabled sharpen — keeps the draft as a survivor for the
  realist), **dispatch-claim** (`claim_proposal_for_dispatch(allow_develop=…)` —
  a develop row is unclaimable on BOTH dispatch paths while disabled). Fail-
  closed uniformly: any absent stamp ⇒ dropped, never shipped. `EgoConfig.
  genesis_self_development_enabled` flips enforcement off (unlock); the regex
  marker gate it replaced (`_flag_develop_scope`) is deleted.
- **Pending cap is TOTAL across both egos** (15): `_process_proposals` counts
  the global pending pool (informational rows excluded; develop-flagged
  incoming excluded) and evicts global-oldest-unranked (24h guard). Evo-origin
  rows are now cap-countable (deliberate).
- **Revalidation cadence is live end-to-end**: `create_batch` stamps
  `revalidate_at` (shared helper `config.next_revalidate_at`), migration 0077
  backfilled pre-PR-6a pending rows, and reconcile reaffirm/revise RE-STAMP the
  clock (a re-validated item leaves ⚠due). Genesis-ego dispatch/investigate
  proposals get a default `expected_outputs` deliverable
  (`~/.genesis/output/ego-reports/<id>.md`) when the ego omits one; both
  dispatch-prompt paths render the required-files block
  (`_required_outputs_block`).
- **Goal provenance + additive autonomy (2026-07-16)**: `user_goals.origin`
  ('user' | 'genesis_ego', immutable after create — excluded from `update()`'s
  allow-list; CHECK-constrained; migration 0063). A `genesis_ego`-origin goal
  reviewed from the genesis ego cycle is paused/deprioritized DIRECTLY
  (`session._apply_own_goal_change`: no proposal, audit observation
  `goal_autonomous_action`); everything else — user-origin goals, the user-ego
  cycle, close/priority-increase/delete — keeps the recommend-only proposal
  path (`goal_actions.py`). The approval gates (proposal + autonomous-CLI) are
  untouched: the ego skips proposal CREATION only for its own additive
  artifacts. **ACTIVE since PR-3 (2026-07-16)** — two parsed output keys on
  the genesis ego cycle, both source_tag-gated in `_process_cycle_output`:
  `own_goal_creations` (`session._process_own_goal_creations` — THE only code
  stamping `origin='genesis_ego'`; validated in `_validate_output`; caps: 1
  per cycle + `config.max_active_ego_goals` active; `find_similar` dedupe
  across active+paused of both origins) and `own_goal_reviews`
  (`_process_own_goal_reviews` — own-lane only, non-ego goals skipped never
  proposed; routes into the #1086 double-gated direct-apply). The
  `ego_goal_create` MCP tool still has NO origin argument (provenance is
  never caller input) and all three goal-mutation MCP tools (create/update/
  progress — the last resets the staleness clock via updated_at) are
  DISALLOWED in ego cycle sessions (`_EGO_CYCLE_DISALLOWED_TOOLS` →
  `--disallowedTools`).
  Every user-facing goal surface (user-ego scanner/context, morning report,
  world snapshot, dispatch prompts, computed focus, j9 metric, extraction
  dedupe) filters `origin='user'`; the genesis context renders the own-goal
  lane (`genesis_context._own_goals_section`, staleness-annotated — what
  makes own-goal review non-blind). Visibility: `goal_autonomous_action`
  observations are user-visible by default (NOT in INTERNAL_OBS_TYPES,
  locked by test) + a morning-report own-goals count line. Paused own-goal
  tail is deliberately unbounded (user decision 2026-07-16), watched via
  that count line.
- **identity/**: SOUL/USER/VOICE/STEERING CAPS-markdown + `IdentityLoader`
  (wired via perception). `cc/session_config` reads SOUL+VOICE directly, not
  via the loader. **USER.md auto-synthesis is PERMANENTLY DISABLED** — the
  evolver writes system-owned `USER_KNOWLEDGE.md` instead, ledger-tracked.
- **deliberation/**: `deliberate()` multi-model panel with explicit dissent —
  reachable ONLY via the `deliberate` MCP tool, recursion-blocked,
  never-raises. On-demand, not a default judgment path.

## 8. Guardian & sentinel — infrastructure self-healing

Two complementary watchdogs: the host-VM Guardian (outside the container blast
radius) and the container-side Sentinel (CC-driven diagnosis/repair).

```yaml subsystem-map
entry: guardian-sentinel
modules: [guardian, sentinel]
verified: 84c7259d 2026-08-31
```

- **guardian/** is bidirectional: host side (`python -m genesis.guardian`,
  systemd timer; `check.py` runs 5 parallel probes → 6-state machine → act;
  Proxmox disk/RAM provisioning verbs) and container side (`watchdog.py`
  monitors the host Guardian every awareness tick, incl. git-SHA code-drift
  detection). Config `~/.genesis/guardian_remote.yaml`; missing → silently
  disabled.
- **autonomy zombie-scheduler watchdog** (`autonomy/watchdog.py`, run out-of-process
  by `genesis-watchdog.timer` every 300s via `watchdog_runner.py`; distinct from the
  container `watchdog.py` above): reads `~/.genesis/status.json` (written by the runtime's
  `status_writer_loop`) and RESTARTS the bridge/runtime on status-file staleness, or a
  specific scheduler on a stale `scheduler_heartbeats` entry (`awareness`/`surplus`,
  `job_health.last_run` age > 900s) — the "zombie scheduler, alive but not dispatching"
  case. Restart is deferred while `heavy_workload` is set (dream cycle), during a boot
  stabilization window, or a network outage; repeated restarts (flap) or max-restarts
  escalate to a durable owner/Telegram alert. This is why a wedged/dead surplus needs no
  `subsystem_stale` alert (§9): it is detected and self-healed here at 900s.
- **Merged ≠ deployed**: guardian code reaches the host ONLY via
  `scripts/update.sh` / `guardian-gateway.sh` (the host-deploy gate in the dev
  skill). Known wart: the watchdog's stale-alert wording inverts when the
  deployed script is NEWER than the host checkout.
- **Planned-maintenance stand-down** (`check.py::_gateway_pause_active`): reads a
  self-expiring `<state_dir>/paused.json` — written by the gateway `pause [ttl]`
  verb, bounded by `expires_at` and capped by `gateway_pause_max_ahead_s` — and
  stands the whole check cycle down beside the indefinite `maintenance_file`. This
  is the *capability* a deploy uses to pause the Guardian across the server restart
  — instead of escalating to `confirmed_dead` and firing a false down/recovered
  alert — so that a paused restart is silent; the `scripts/update.sh` caller that
  actually writes the pause across its stop/restart lands as a separate change (a
  deploy on the old caller simply runs unpaused, as today). The TTL means a deploy
  killed before its `resume` self-heals rather than muting the watchdog. During
  stand-down the heartbeat carries a `standdown` marker so `probe_guardian` reports
  DEGRADED (alive, not watching) rather than HEALTHY. Distinct from the container
  `~/.genesis/paused.json` runtime kill switch (that pauses all of Genesis; this is
  host-side and Guardian-only).
- Provisioning verbs are EXECUTE-ONLY — approval is the CALLER's
  responsibility (container obtains it via Telegram before invoking). Two
  families: Proxmox VM grows (`provision-grow-disk/-memory`, hypervisor API) and
  LOCAL container-capacity grows (`grow-root`, `set-container-limits` in
  `guardian/grow_capacity.py` — incus resizes the thin LV+fs / cgroup caps ONLINE,
  grow-only, spike-proven). Both flow through `provision_grow(kind=disk|memory|
  root|limits)` → owner-approval → the execute verb. The limits verb closes the
  VM↔container coupling (a grown VM's RAM/cores reach the container).
- Read-only `host-profile` verb (`guardian/host_profile.py`) feeds the
  `infra_profile` host plane; the CC diagnosis prompt inlines the shared-mount
  `INFRASTRUCTURE.md` (truncated) so the diagnostician starts with the body
  schema instead of re-deriving the machine's shape.
- **Out-of-band tiered alerts** run every tick through the guardian's OWN
  Telegram (survives a dead/thrashing container): storage-pool data%/metadata%
  (`pool.py`) and **RAM** (`memory_watch.py`, E-rest) — the latter over two
  axes worst-of, container cgroup (via incus-exec, best-effort) + host-VM
  `/proc/meminfo` (the reliable axis). Both use the shared `_tier_for`/
  `decide_alert` hysteresis. Read-only `disk-status`/`ram-status` verbs expose
  the same measurement to the container.
- **Container-swap invariant reconciler** (`swap_watch.py`) runs every tick:
  re-asserts `limits.memory.swap=true` (incus config) and live-activates the
  cgroup `memory.swap.max` (via `cgroup_ops`) when observed at `0` — the
  self-heal for installs that advance via bare `git pull` and never re-run
  host-setup. Heals page INFO; failures page WARNING (24h throttle); kill
  switch `swap_reconcile_enabled: false`.
- **Host zram swap** (`scripts/lib/host_swap.sh`, E-rest E3): a
  compressed-RAM-first swap tier on the host VM — `zram-swap.service` at swap
  priority 100, sized `min(MemTotal/2, 4GiB)` (`HOSTSWAP_CAP_GIB` override).
  Applied by `install_guardian.sh` Step 9c (fresh) and the gateway `redeploy`
  verb (existing installs retrofit on next update; output to stderr, never
  fails a redeploy). Degrades to one-line skips (container vantage, no
  zram.ko/zramctl, external zram, no sudo); durable opt-out = `sudo systemctl
  mask zram-swap.service`. Completes the swap story `memory_resilience.sh`
  leaves as a warning — see `docs/reference/memory-resilience.md`.
- **sentinel/** is LIVE-wired but **shadow-only autonomy**: config mode
  `"live"` is NOT implemented (dispatcher warns + downgrades); every proposed
  action requires human approval. `InfrastructureMonitor` (call site 37, free
  models) observes each awareness tick and wakes the dispatcher; state persists
  to `~/.genesis/sentinel_state.json`.
- GROUNDWORK: guardian-cgroup, guardian-bidirectional, sentinel-live-autonomy.

## 9. Ambient cognition — heartbeat, reflection, attention

The loops that make Genesis think between conversations.

```yaml subsystem-map
entry: ambient-cognition
modules: [awareness, perception, reflection, attention, session_awareness,
          session_charter.py]
verified: 29a382e7 2026-09-03
```

- **PR-watch inline surface (2026-07-21)**: a SessionStart hook
  (`scripts/surface_pr_updates.py` → `session_awareness/pr_watch.py`) mirrors the
  `upstream-pr-steward` campaign's own owner notifications — the ones it already
  logs to `outreach_history` (category `notification`, topic `%steward%`) when a
  tracked EXTERNAL PR changes — into foreground CC sessions as a one-line
  `[PRs] …` nudge, so a status change missed on Telegram still reaches the user.
  Read-only, **home-anchored DB** (NOT `genesis_db_path()`/`repo_root()`, which
  would read an empty `<worktree>/data/` — the same trap `_charter_db_path`
  avoids). Seen-state is a home-anchored JSON sidecar
  (`~/.genesis/pr_watch/seen.json`), NOT `outreach_history.opened_at` (that
  column is unwired, always NULL); a change resurfaces each session for
  `resurface_days` then stops, and the sidecar self-prunes to the `lookback_days`
  window (no retention step). Lever: settings domain `pr_watch`
  (`config/pr_watch.yaml` + `pr_watch_config.py`) + `GENESIS_PR_WATCH_DISABLED`
  kill switch; skips dispatched sessions (`GENESIS_CC_SESSION=1`) so the human's
  next foreground session still gets the nudge. The campaign's discovery/notify
  behavior lives in its install-local strategy doc (campaigns ship zero defaults).
- **Infra protection posture (2026-07-16; network plane 2026-07-17)**: hourly
  `_check_infra_protection_posture` reads the infra profile's effective facts
  and raises one `high` `infrastructure_alert` when a memory-plane protection
  is missing (container `memory.swap.max=0`, oomd pressure-kill off, host swap
  absent, incus swap knob explicitly `"false"`) or the profile is stale (>3d =
  refresh broken → distinct "posture UNKNOWN" alert). Also covers the
  **network plane** — reads the *effective* facts `networkd_default_route_keepconfig`
  (KeepConfiguration on the default-route link's OWN drop-in, not any-link) and
  `network_watchdog_enabled` (`systemctl is-enabled`, not mere file presence),
  gated strictly on `networkd_manages_default_route is True` — a networkctl-derived
  fact (the running daemon reports the default-route link
  `AdministrativeState=configured`) that suppresses the rules on NetworkManager
  installs, so no false-positive on the public repo. Only EXPLICIT defect
  values alert — absent/`None` facts stay silent (no guardian plane, cgroup
  v1, fresh install). One open row per source via `supersede_except_hash`;
  auto-resolves on recovery. Completes the silent-skip closure: provision
  (bootstrap, #1082) → reconcile (guardian, #1083) → alert (this). The
  provision-or-surface convention: a resilience feature that skips on a
  missing prereq must either provision it or register a fact this check reads.

- **Git-health alerts self-heal, slot-scoped (2026-07-16)**: the per-tick cheap
  probe auto-resolves open `git_cheap` observations on pass; the daily deep
  fsck auto-resolves `git_deep` only (fsck READS — a passing fsck must never
  clear a live `rootfs_readonly` cheap alert). Creates carry
  `skip_if_duplicate=True` (atomic INSERT…WHERE NOT EXISTS — the only guard
  that works across concurrent loops). Probe sensitivity is deliberately
  single-failure; do not add consecutive-failure gating.

- **awareness/**: the 5-min heartbeat. ~23 signal collectors (the richer
  `learning/signals/*` set REPLACES the bootstrap placeholders in
  `signals.py` — those stubs are GROUNDWORK(signal-bootstrap), not the live
  collectors). Tick → depth classification (MICRO/LIGHT/DEEP/STRATEGIC) →
  reflection dispatch. Also per-tick `_check_*` housekeeping: CC-slot RSS leak
  watch, subscription-cap detection, SQLite WAL hygiene, resilience-axis folds,
  liveness heartbeat, and (hourly) embedding-backlog degradation — counts
  `memory_metadata.embedding_status='failed'` (permanently keyword-only rows the
  rate alert misses), hybrid `high` (dashboard) / `critical` (Telegram) by band —
  plus (hourly) deploy staleness: merged-vs-deployed drift (update.sh age,
  commits behind from local refs, missing systemd units, host-guardian
  deployed_commit via `~/.genesis/host_gateway_state.json`; collectors in
  `observability/snapshots/deploy_health.py`), `high` on any drift, `critical`
  only sustained (≥7d AND ≥20 commits, or a missing unit alerted >24h).
  Also (hourly) ego cycle liveness (`_check_ego_liveness`, `ego/liveness.py`): an
  ego with no COMPLETED cycle past a conservative multiple of its current
  interval (the `job_health.last_success` gap — never the `is_running`/heartbeat/
  `next_fire_at` proxies that stay green through a deadlock) raises one
  self-superseding `high` `ego_alert` per ego, auto-resolving when a cycle lands;
  `gated`/`paused`/fresh-install are never stalls.
  Also per-tick (WS-2 M10) the SINGLE designated `alert_events` writer:
  `_persist_health_alerts` recomputes the firing set via the pure
  `mcp/health/errors.py::_compute_alerts()` and reconciles a durable open-set
  (open row per firing alert, `resolved_at` stamped on clear) — replacing the
  in-memory, per-process, one-generation `_alert_history` dict so incident
  history survives restart. It does NOT drive the ego cadence (ego has its own
  scheduler). Trap: PEP 562 lazy `__init__` — don't eager-import `loop.py`.
- **Subsystem-cessation alerts (the honest-liveness family, `_compute_alerts`)**:
  three durable `subsystem_stale`-family alerts so a dead scheduler cannot read
  healthy. `subsystem_stale:<name>` (ego CRITICAL; inbox/dashboard/outreach WARNING)
  fires when a subsystem's heartbeat — the freshest valid pulse across the durable
  `events` table and the in-memory event-bus ring — goes overdue past its per-subsystem
  `HEARTBEAT_EXPECTED` threshold (via `mcp/health/manifest.py::compute_heartbeat_staleness`);
  `subsystem_never_started:<name>`
  tells a FAILED / never-pulsed start (cross-referenced against the persisted bootstrap
  manifest, past a boot grace) from a fresh install; `subsystem_heartbeat_unknown` covers
  a corrupt/unreadable pulse. Pause-gated subsystems (`surplus`, `inbox`) downgrade
  overdue→`paused` while globally paused; conditional-pulse ones (`outreach`, whose
  channel-independent daemon pulses only while its scheduler runs) are exempt from the
  never-started inference. **surplus is EXCLUDED** from `subsystem_stale`: its wedged/dead
  state is owned by the PR-B dashboard tile (a `job_health.surplus_dispatch` signal) and
  the zombie-scheduler watchdog (§8), and its loop-END event-HB is load-fragile — a healthy
  15-30 min dispatch legitimately gaps it, so its `HEARTBEAT_EXPECTED` overdue is a generous
  3h (matching the tile) to avoid crying wolf. reflection/awareness ride the awareness tick's
  own `tick_overdue`.
- **scheduled-job telemetry (WS-2 M9)**: `runtime/_job_health.py` keeps the
  cumulative `job_health` row AND now appends per-run `job_run_events` (era
  attribution the cumulative row can't give). Writes are debounced off the
  persisted `job_health` anchors — a success only when ≥1h since the last, a
  failure on streak onset + hourly heartbeat — so a stuck sub-hourly poll costs
  ~24 rows/day. `duration_ms` is honest-or-NULL (only from an explicit
  `record_job_start` marker; never derived from `last_run`). 90-day prune via
  `_wire_drip_retention_jobs` — which (2026-07-24) also prunes the observability
  `events` bus table itself (`events_prune`, the last high-volume table with no
  retention: 45k+ rows / ~108d). `crud.events.prune` takes an ISO cutoff, not
  `days=`; the DELETE's lexical `timestamp < cutoff` is chronological because
  every `events.timestamp` is a UTC isoformat. **Failure payloads (2026-07-23):** the three
  scheduler emitters (reflection, outreach, surplus) thread the exception into
  `observability/failure_details.py`, which sets `error_type` **IFF** a real
  exception caused the failure — a semantic failure (external blocker, e.g. a
  429 quota result) carries `error_reason` and no `error_type`. That presence
  test is the structural internal-vs-external discriminator; do NOT infer it by
  pattern-matching the message, because ONE event type carries both kinds
  (`weekly_assessment.failed` fires for a genuine TypeError *and* for a provider
  quota block). `job_health.error_type` mirrors it and clears on recovery
  alongside `last_error`. Frames come only from
  `util/tasks.normalized_frames` — a second normalizer would split one bug
  into two fingerprints. **The funnel (2026-07-23):** `record_job_failure`
  gained an `exc=` param and, when an exception caused the failure, ALSO emits
  a throttled `job.failed` event (via `util/tasks.emit_sync`, sharing the
  run-event throttle -> ~24/day/job). This bridges the ~50 background-job loops
  that wrote only to `job_health` onto the event bus, where the reflex arc (a
  bus subscriber) can reach the largest class of internal defects - previously
  invisible. Semantic/no-exception failures stay off the bus; callers that emit
  their own domain `.failed` pass `emit_event=False` to avoid double-counting.
  Fire-and-forget (a broken emit never blocks the `job_health` write), and
  `job.failed` is in the ego's `_REFLEX_OWNED_EVENT_TYPES` so it never spins a
  reactive ego cycle. Ingested by the reflex arc as of PR-2b (`ingest.py`
  consumes `job.failed`, guarded on `error_type` presence).
- **A resumed reflection runs against a DIFFERENT tick than the one that asked.**
  When a user approves a reflection's gated CC fallback, `_resume_approved_reflections`
  dispatches it with `self._last_tick_result` — the CURRENT tick — because the
  loop's own scoring may never reach that depth again. So what a person approved
  and what runs are about different moments — and per that method's docstring
  the reason is availability, not a preference for fresher state: the current
  tick is the only one it has. The ORIGINATING tick is genuinely unrecoverable: the approval context is
  built from a fixed key set with no tick field, and `_approval_key` excludes
  per-invocation identity on purpose so recurring dispatches reuse one pending
  row rather than minting a new approval every tick. The resume log therefore
  names the approval, its age, and the tick being run against, and says outright
  that this is not the requesting tick — rather than implying a lineage that
  does not exist. Giving it one would mean a key-neutral carrier that does not
  currently exist; do not smuggle a tick id into `action_label` or
  `extra_context`, both of which feed the approval key.
- **perception/**: the real-time reflection engine — MICRO (and LIGHT without
  a CC bridge) run in-process via the router; DEEP/STRATEGIC go to the CC
  reflection bridge. GROUNDWORK: user-model-synthesis, pre-execution-gate
  (template exists, gate not live).
- **reflection/**: the deep/scheduled path (self-assessment, quality
  calibration, learning-stability). **Cadence trap:** jobs FIRE DAILY but an
  idempotency gate holds each to ≤1 SUCCESS per week — a failed day retries
  tomorrow, not next week.
- **attention/**: Track-1 ambient attention — SHADOW, not in runtime init;
  runs via offline CLI over pulled snapshots. The 6-module core is pure and
  edge-portable (no wall clock, no I/O, no genesis deps — test-enforced);
  `sampler.py` (L1.5 judge) is the only LLM caller, outside the core.
  **Firewall: transcript text is never persisted** — only refs + derived
  features reach `attention_events`. Config is versioned DATA
  (`~/.genesis/config/attention_config.json`).
- **session_awareness/**: WS-C ambient session-theme layer — SHADOW.
  The proactive memory hook folds each genuine user prompt's embedding
  into a per-session EMA + entity ledger
  (`~/.genesis/sessions/<id>/session_theme.json`); on a drift-trigger
  fire it spawns the detached worker (2-slot flock semaphore), which
  retrieves+ranks candidates over four lanes — vector, decisions
  (`tags~decision`, the OMI-incident class), entity-keyword drift, and
  the **entity lane** (ledger keywords → entity nodes → ≤2 typed hops →
  mentions; `ranking.ENTITY_LANE_MODE`, LIVE since the E4b flip (#993) —
  entity hits rank normally with a reserved floor of 2, and the verdict's
  `entity_candidates` count reports the live lane's contribution) — all
  Qdrant lanes EXACT search (filtered HNSW without
  payload indexes drops valid results; found 2026-07-09). Headless-
  Haiku arbiter judges candidates per fire (fail-closed parse, group-
  kill on timeout). Verdicts → `ambient_verdict.json`, tuning →
  size-capped shadow log; each arbiter attempt (incl. pre-spawn
  failures, success=0 with reason) also records a `call_site_last_run`
  row (`ambient_arbiter`, neural monitor) via its own short-lived RW
  connection. **Zero memory-row writes — never bumps
  retrieved_count** (retrieval connection is mode=ro; protects
  MEM-005/H-1 baselines). Fail-open at the hook boundary. Kill switch:
  `GENESIS_SESSION_AWARENESS_DISABLED=1`.
- **Session charter + ledger** (session-manager stages 1-2): the
  `session_charters` + `session_ledger` DB tables (migration 0058) are the
  canonical store; `~/.genesis/sessions/<sid>/charter.md` is the regenerated
  human mirror (pre-0058 `charter.json` files are a legacy read-fallback,
  imported once by `scripts/backfill_session_charters.py`).
  `scripts/genesis_precompact.py` (PreCompact hook, both triggers, 5s
  fail-open timeout, stdlib-only sqlite3, BEGIN IMMEDIATE) persists a
  foreground session's IMMUTABLE origin — the first typed user prompt,
  extracted from the transcript head at the FIRST compaction boundary — and
  bumps `compaction_count` thereafter (+ `waypoints.jsonl` deterministic
  spine). `origin_prompt`/`origin_ts` are write-once (filled only WHERE
  origin_prompt IS NULL); `mission`/`pointers`/ledger rows are living fields
  owned by the `session_charter*`/`session_ledger*` MCP tools on
  genesis-health (`mcp/health/session_charter_tools.py`), which may create a
  stub row before the first compaction. Read paths:
  `genesis_session_context.py` re-injects origin + open ledger on every
  startup/resume/compact (NOT clear), and `genesis_urgent_alerts.py` emits a
  per-turn `[Charter: <mission> | open: N]` drift tag (both mode=ro,
  fail-open). Ledger statuses: open/in_progress/done/absorbed/dropped —
  `absorbed` + `evidence` is written by the repo-pulse exact tier (below)
  as well as the MCP tools. Dispatched sessions
  (GENESIS_CC_SESSION=1) are skipped — task_states is their continuity spine.
- **Ambient ledger extractor** (session-manager stage 3) — **SHADOW**. At
  each PreCompact snapshot the hook fire-and-forgets
  `scripts/ledger_shadow_worker.py` (`--end-byte` stat'd at the boundary);
  the detached worker (`session_awareness/ledger_worker.py`) reads the
  transcript delta since its own cursor
  (`~/.genesis/sessions/<sid>/ledger_shadow_cursor.json`, advanced ONLY on
  recorded ok/empty_delta — failures re-cover their window), extracts
  agreements/pivots via headless Haiku (`ledger_extractor.py`: DATA-framed
  prompt, fail-closed parse, verbatim-quote verification), matches against
  the live ledger (exact hash + SequenceMatcher ≥0.85 — the precision
  signal) and prior shadow events (`duplicate_of`), and records rows to
  `session_ledger_shadow_runs`/`_events` (migration 0059) — **the live
  `session_ledger` is NEVER written until the data-gated flip PR**. Shared
  subprocess core with the arbiter: `session_awareness/headless.py`;
  canonical typed-prompt filter `session_awareness/transcript.py` (the
  PreCompact hook keeps a parity-tested stdlib duplicate; honors
  `promptSource` typed/queued, excludes bare slash-commands + markers).
  Levers: settings domain `session_ledger_shadow` (off|shadow; `live`
  reserved, coerced+warn) read at worker startup;
  `GENESIS_LEDGER_SHADOW_DISABLED=1` hook-level kill. Per-session flock;
  `--backfill` replays historical transcripts in typed-turn windows
  (`trigger='backfill'`, cursor untouched). Measurement:
  `scripts/ledger_shadow_report.py` (recomputed precision, FP adjudication,
  FN windowing, leak invariant); retention 45d via
  `scripts/prune_ledger_shadow.py` (disk-hygiene step 8). Telemetry:
  `call_site_last_run` row `ambient_ledger_extractor` (deliberately not a
  critical site).
- **Repo-pulse annotator** (session-manager stage 4) — **LIVE (exact tier)**.
  At SessionStart boundaries (startup/resume/compact, never clear; foreground
  only) `genesis_session_context.py` fire-and-forgets
  `scripts/repo_pulse_worker.py` (home-anchored `--db-path`;
  `GENESIS_REPO_PULSE_DISABLED=1` kill switch). The detached worker
  (`session_awareness/repo_pulse_worker.py`) takes a GLOBAL flock + 30-min
  silent debounce (`~/.genesis/repo_pulse/`), reconciles prior proposals
  against current ledger state (confirmed ONLY with same-PR evidence — the
  attribution guard; dropped→rejected; done/stale/missing→superseded),
  enumerates merged PRs since its cursor (`repo_pulse_gh.py`: slug resolved
  LIVE via `gh repo view` — config slugs return plausible-stale data; capped
  windows record `limit_hit` loudly), then matches against OPEN ledger rows
  across ALL sessions (`repo_pulse.py`): the **exact tier** auto-absorbs only
  on an explicit `Ledger: <32-hex>` PR-body marker (ledger UPDATE with PR
  evidence via `ledger_update`; bare hex → proposal; `annotation_exists`
  re-absorb guard protects reopened items), the **fuzzy tier** (headless
  Haiku, echo-numbers-only fail-closed parse) is proposal-only in EVERY mode.
  Two sibling reconciliation lanes ride the SAME merged PRs to resolve standalone
  `follow_ups` (target_kind='follow_up', migration 0084): the **follow-up marker
  lane** (#1397) auto-absorbs a follow_up whose id is cited `Follow-up: <id>` in
  OUR own PR body; the **issue-close lane** (WS-A) resolves the follow_up that
  spawned a Genesis-posted contributor issue when a contributor's PR CLOSES that
  issue (`Closes #N` → `closingIssuesReferences`, joined via
  `pending_issue_posts.source_ref`; default-branch + same-repo scoped). Both reuse
  `absorb_followup` + the annotation store + the `annotation_exists` re-absorb guard.
  Store: `repo_pulse_runs`/`_annotations` (migration 0062,
  `UNIQUE(tier,item_id,pr_number)` dedupe; CRUD `db/crud/repo_pulse.py`).
  Cursor (`cursor.json`, gh-format mergedAt watermark) advances monotonically
  ONLY on recorded ok. Proposals surface in the charter injection block
  (≥ `inject_confidence_floor`, cap 3, confirm-hint) and resolve via
  `session_ledger_update` → next reconcile sweep — or via the dashboard
  Sessions tab cockpit (PR-4b: per-session charter/ledger/waypoints/pulse
  detail at `/api/genesis/cc-sessions/<id>/charter`, confirm/reject POST
  with hint-identical semantics; the waypoints.jsonl spine gets its first
  reader here); confirmed/(confirmed+rejected) is the fuzzy precision
  metric. Levers:
  settings domain `repo_pulse` (off|propose_only|live, default live — the
  lever gates only the reversible exact absorb; invalid degrades to
  propose_only). Retention 45d via `scripts/prune_repo_pulse.py`
  (disk-hygiene). Telemetry: `call_site_last_run` row `repo_pulse` (not a
  critical site — failed runs self-heal by re-covering their window).

## 10. Learning & evaluation

Self-improvement loops and the instrumentation that keeps them honest.

```yaml subsystem-map
entry: learning-evaluation
modules: [learning, eval, experimentation, feedback, calibration, ledger]
verified: 50b79ffb 2026-09-01
```

- **learning/** is the de-facto cron host: `rt._learning_scheduler` registers
  ~20+ jobs well beyond learning (recovery orchestrator, reapers, email-gate
  drain, retention sweeps, plus the eval/feedback jobs below). CronTrigger
  discipline is load-bearing here. Loops that actually run: triage pipeline,
  procedural extraction (extract → judge → promote hourly, novelty +
  contradiction gates), weekly skill evolution, daily triage calibration.
  Weekly skill evolution is **propose-only** (autonomous auto-apply retired
  2026-08-01, #1276): it STAGES every SKILL.md edit (MINOR and larger) as a
  `skill_proposal` observation for human/CC review and never writes a skill
  file — no proposal is ever blocked or auto-applied. Under propose-only NO
  cognitive-file-modification ledger pre-image is created (the apply/resolve
  path that would record one is a deferred follow-up). Two ADVISORY signals ride
  the staged proposal for the reviewer, and neither gates staging: a deterministic
  validator suite (`skills/validator.py` — structure, trigger coverage,
  testability/vague-language, size, examples, and MINOR-content consistency; any
  hard failure un-passes it) whose outcome is recorded as a `validated` flag
  (for MINOR it reflects that suite; for MODERATE+ an LLM apply-recommendation)
  plus `validation_detail` on failure; and, WHEN ENABLED (gate on, router +
  baseline available), a shadow
  **skill-edit Critic** (`skills/skill_edit_critic.py` +
  `eval/rubrics/skill_edit_regression.py`) that screens for self-modification
  pathologies via the `judge` call site and logs a `skill_evolution_gate`
  verdict (WS1 shadow). The validator suite above is live (advisory); only the
  `autonomy_level` param is retained as (currently unread) groundwork for the
  future WS1 `enforce` mode. A complementary **held-out replay
  gate** (`eval/skill_replay/`, tool `skill_replay_run`) goes further — it
  REPLAYS a frozen per-skill golden suite (`~/.genesis/eval/skill_golden/`,
  authored via `eval/skill_golden_set.py`) against OLD vs NEW content in
  bare-Claude isolation and logs a recommend-only zero-regression verdict
  (`skill_replay_verdict` observations); also shadow, out-of-band, mutates
  nothing. Levers: `skill_evolution_gate` settings domain (off|shadow for the
  Critic + a `replay` off|shadow sub-config) + `GENESIS_SKILL_EVOLUTION_GATE_OFF`.
  `tool_discovery.py` static maps are deprecated (GROUNDWORK
  provider-migration) — use ProviderRegistry.
- **eval/**: J9 = fire-and-forget emit hooks on live cognitive paths (the
  "cannot break production" contract — hooks must never raise) + weekly Sunday
  aggregation (hard 7-day window) + an on-demand batch judge as a surplus
  task. The model gauntlet is weekly but OFF by default (paid inference) and
  NEVER auto-mutates the roster. Ten snapshot dimensions: the original five
  (memory/system/ego/cognitive/procedure) + cognitive_drift + three
  snapshot-only WS-1 A2 series (approvals gate-throughput, goals scaffold,
  noise/passivity) + `dev_quality` (findings-per-PR by severity, code-audit
  gauge, edit-failure flow; fed by the `pr_review_harvest` job — Sun 06:45
  user-tz, 45 min BEFORE the 07:30 aggregation, deliberately a separate job
  so a gh outage degrades to stale rows instead of silently nulling the
  dimension; the harvester reads INLINE PR comments via
  `gh api pulls/N/comments` — `gh pr view --json reviews,comments` misses
  bot findings). `run_weekly_aggregation` swallows per-dimension failures
  silently — the registration test in `tests/test_eval/test_j9_eval.py` is
  the guard; keep it in sync when adding a dimension. Resolver-origin
  classification for the approvals series is canonical in
  `db/crud/approval_requests.py classify_resolver` (free-text convention —
  a new `resolved_by` writer must extend the prefix tuples + drift test).
- **eval/bench/** (`genesis eval bench`, WS-1 A3): Genesis-vs-bare-Claude
  paired A/B on real tasks. FOUR A/B-ish surfaces now exist — don't conflate:
  `eval bench` (CC-arm task A/B), `eval benchmark` (provider×dataset table),
  the gauntlet (roster agentic fix-loop), Crucible/Evo (in-process prompt
  A/B). Bench isolation contract: Genesis arm = fresh no-callback CCInvoker +
  genesis-memory MCP env-redirected to a WAL-safe DB snapshot
  (`GENESIS_DB_PATH` + `GENESIS_MEMORY_WRITEBACKS_OFF` — recall is
  read-MOSTLY: without the seam it bumps retrieved_count in shared prod
  Qdrant); bare arm = `--safe-mode` (the only OAuth-compatible CLAUDE.md
  suppression; `--bare` refuses OAuth) + cleanroom CLAUDE_CONFIG_DIR.
  Task set is PRIVATE (`~/.genesis/eval/bench_tasks_v1.jsonl`; loader
  refuses in-repo paths). Judge = `bench_task_success` rubric, UNCALIBRATED
  v1 (every surface stamps `judge_calibrated: false`). Every new
  genesis-memory tool must be classified in `eval/bench/arms.py`
  (static-AST forcing test). Don't run across Sun 07:30 (J-9 aggregation).
  **A5 read surface** (WS-1 A5): the persisted paired win-rate is readable via
  `/api/genesis/eval/bench` (a compact result card in the dashboard internals
  tab) and the `bench_status` MCP tool — both aggregate-only (never per-task
  private text), shaped by the shared `eval/bench/surface.py`, filtered on
  `model_profile='bench:genesis'` (the genesis row's `metadata_json.stats` is
  self-contained), and stamped with the uncalibrated-judge + `insufficient_data`
  caveat. A stats-less/all-skip run surfaces flagged, never crashes.
- **experimentation/**: Crucible A/B + Evo fan-out — on-demand via MCP tools
  only; **recommend-only is the safety invariant** (no autonomous promotion,
  no live-cognition writes; Bonferroni + held-out re-validation).
  `standalone_router.StandaloneLiteLLMRouter` is the ONE offline Router shim
  (calibration + bench both use it — don't add inline copies).
- **feedback/**: the Outcome Bus (`outcome_events`) — **write-path LIVE
  (harvest 8:45/20:45 + the first real-time emits: task executor COMPLETED/
  FAILED, WS-2 P1b); read-path PARTIAL** — the ego-ECE calibration consumer is
  LIVE (`feedback/calibration.py::compute_ego_calibration` reads T1 ground-truth
  rows via `outcome_events.calibration_pairs(tier=1)`, scheduled from
  `runtime/init/learning.py` → one `ego_calibration_snapshots` row per run); the
  broader grading/reinforcement read path stays DARK until the P2 grader lands.
  Tier taxonomy is load-bearing: Tier-1 ground truth outranks user approval.
  `record_outcome` must never raise. Deliberately "observation, not
  reinforcement" — don't rename toward RL.
- **calibration/**: the pure ECE/MCE + confidence-bucket primitives
  (`metrics.py`, `types.py`), consumed by the WS-2 ledger (`ledger/cells.py`) and
  the ego-ECE path (`feedback/calibration.py`). **Four distinct "calibration"
  surfaces exist** (this package, `learning/triage/calibration`,
  `feedback/calibration` ego-ECE, `eval/calibration` golden-set loader) — don't
  conflate. **WS-2 P5 sunset DONE (migration 0090):** the legacy per-domain
  curve/reconcile loop (`curves.py`/`logger.py`/`reconciler.py`, the
  `calibration_curves` table, and the proto-ledger `predictions` table — archived
  to `predictions_legacy_ws2`) is retired; `calibration_cells` (P3) is the live
  calibration surface.
- **ledger/** (WS-2 P1a+P1b+P2+P3): the cognitive ledger — falsifiable predictions
  in `ledger_predictions` (migration 0064), written only through the
  validating CRUD (`db/crud/ledger_predictions.py`) against the code registry
  (`ledger/metrics.py`: 9 v1 metrics, each with a pure-SQL resolver; NO
  import path to `genesis.routing`, locked by test). **Writer hooks LIVE
  (P1b)**: `ledger/writers.py` fires fire-and-forget from four commit paths —
  outreach `_deliver`, executor pending-claim, BOTH build-lane create sites,
  ego `create_batch` — with measured base-rate prior seeds (reply ≈0.02, not
  0.5) and stated-confidence seams (`OutreachRequest.stated_confidence`;
  `task_submit` optional field → outputs JSON envelope). Hook failures
  increment a counter surfaced as `ledger:write_failed:<class>` via
  `_compute_alerts` (never block the action). **Grader LIVE (P2a)**:
  `ledger/grader.py` runs twice daily (`ledger_grader` 6:15/18:15, env
  kill-switch `GENESIS_LEDGER_GRADER_DISABLED`) — mechanically resolves due
  predictions (`list_due_open`) by running each metric's resolver, mapping
  keyed on `outcome_value` (non-None ⟺ `resolved`), and writing outcome+Brier
  through the CRUD's idempotent `resolve()`; ZERO LLM calls (own no-`routing`
  import lock). Registry drift / resolver faults alarm
  `ledger:metric_vanished:<class>` / `ledger:grade_failed:<class>` via the same
  counter→`_compute_alerts` path. **Autonomy-evidence rewire LIVE (P2b)**: the
  `learning/pipeline.py` self-grade feed (autonomy `record_success/correction`
  off the LLM classifier verdict — the A1 harm) is REMOVED; the grader now feeds
  `direct_session` earn-back from mechanically-graded `task_execution/completed`
  rows — FAILURE-ONLY (lane `completed`→success, `phase:failed`→correction,
  nothing on slowness/cancel) and SHADOW-FIRST behind `ws2_ledger.autonomy_feed`
  (off/shadow/live, default shadow, read live via `ledger/ws2_ledger_config.py`;
  live feeds the same seam #1119's `autonomy_events` windowed ledger consumes).
  **Calibration table LIVE (P3)**: `ledger/cells.py` recomputes
  `calibration_cells` + `calibration_cell_history` (migration 0069) at the end
  of every grading pass — Murphy decomposition + ECE per (domain, class,
  metric, lane, 30/90/all-time window) over resolved rows keyed on
  `resolved_at`, stated/policy_prior lanes partitioned at grouping time,
  Beta-binomial shrinkage (m=10) cell→parent-domain→global, per-tool base
  rates from `tool_call_outcomes` as the strict policy_prior lane, and
  ok/thin/unknown cold-start labels (thin/unknown NEVER render as a bare
  percentage on any surface). Writes are upsert-then-prune (never an
  observably-empty table mid-rebuild); history prunes at 180d in-pass; a
  recompute failure never blocks grading — it raises the standing
  `ledger:cell_recompute_failed` WARNING (stale-cells signal) on the same
  counter→`_compute_alerts` contract as the writer/grader alarms.
  Surfaces: `calibration_status` MCP (escalation phrasing on thin/unknown,
  top over/under-confident domains), dashboard Calibration tab
  (`/api/genesis/calibration` — cells + mechanical/fallback shares), and
  perception's advisory text repointed from legacy `calibration_curves` to
  ok stated cells (90d-preferred, ego.* excluded, byte-stable sentence
  contract). **Consumers LIVE (P4a)**: the ego-proposal **arbitration
  discount** (`ego/proposals.py::annotate_calibration`, gated by
  `ws2_ledger.arbitration` off/shadow/enforce, default shadow) reads each
  proposal's stated-lane 90d cell (`ego.<action_type>` /
  `approved_and_executes`) — thin/unknown → escalation note only (never a
  discount on ignorance), ok with overconfidence gap >0.15 →
  `_calibrated_confidence` + a digest badge; the calibrated value drives
  digest sort ONLY in enforce; a proposal is never suppressed (sovereignty
  invariant). Lookup failures count into the standing
  `ledger:arbitration_failed` WARNING. `calibration_status` also carries the
  **E1 earn-back evidence stream** (`earnback` key: windowed
  `autonomy_events` counts + posterior per demoted category — surfaced
  MCP-side instead of the design's `v_earnback_evidence` view, declared
  deviation). **B5 knob SUBSTRATE (P4b)**: `ledger/learned_knobs.py` — a
  CLOSED 3-knob registry (`awareness.signal_weights.*`,
  `awareness.depth_thresholds.*`, `memory.activation_blend.*`) with base file
  `config/learned_knobs.yaml` (documentation, never machine-written) +
  install-local overlay `~/.genesis/config/learned_knobs.local.yaml` written
  ONLY via `apply_knob_change` → `cognitive_ledger.record_file_modification`
  (actor `ws2_effector`; pre-image/rollback/MCP tool inherited; bounds
  validator-enforced ≤5%/step, ≤±20% cumulative). Startup applier in learning
  init re-syncs DB-backed knobs from file (SQL clamps backstop);
  `memory/activation.py` reads the blend through a module-level seam
  (import-time load + `reload_blend()`, shipped-constants fallback). The
  deterministic calibration TRIGGER (cell ok, n≥50, 2-window miss → ego
  proposal) is DEFERRED — structurally dormant until a lane grades
  awareness/memory behavior (tabled). The fuzzy LLM-fallback lane is
  deferred (no `acceptance_pass` writer yet). Outreach
  metrics resolve off `outreach_history.engagement_signal`
  (spike-measured 99.5% mechanical); the engagement_outcome CHECK now
  ENFORCES the canonical vocabulary (rebuild #4 in the
  `_migrate_add_columns` chain — its DDL must preserve the three older
  probe fragments, locked by test).

## 11. Routing & providers

How every LLM call picks a provider, and the registry for non-LLM tools.

```yaml subsystem-map
entry: routing-providers
modules: [routing, providers]
verified: b8232425 2026-09-02
```

- **routing/**: `config/model_routing.yaml` defines ~54 numbered call sites,
  each a free-first → paid-last chain; `never_pays` sites are filtered to
  free-only. Per-provider circuit breaker (3 failures, exponential backoff
  capped 30 min — 4h for QUOTA_EXHAUSTED; 429 = backpressure, NOT a breaker
  failure; state persisted cross-process to
  `~/.genesis/circuit_breaker_state.json`). **Probe/call evidence symmetry** —
  a probe may only undo what a probe did: `probe_suspect()` downgrades CLOSED to
  HALF_OPEN on a failed probe and a clean probe may clear THAT, but a breaker
  opened by a real call failure (`_opened_by_call`) is closed only by a real
  `record_success`. A models-listing 200 evidences *reachable*, never *working* —
  it is what a 403-on-use, an exhausted quota or a truncated completion looks
  like from outside. The probe heal deliberately does NOT fire `on_recovery`
  (which resolves the `provider_failure` observation and clears `first_trip_at`),
  so a listing can no longer erase an outage clock. This does not hold a provider
  out of rotation: OPEN auto-transitions to HALF_OPEN on backoff, HALF_OPEN is
  `is_available()`, so the next real call IS the retry. Supersedes PR #705. Consequence to know before touching it: the heal
  branch also zeroes `_trip_count`, so refusing it leaves the backoff climbing
  to the cap instead of resetting — a dead provider is retried less often, and a
  quota-dead one may wait the full 4h. `last_failure_category` is restored from
  disk for any NON-CLOSED saved state (OPEN or HALF_OPEN — see below) and never
  for CLOSED — a qualifier must not outlive the state it qualifies, or it
  poisons the heal guard for a healthy provider. The
  same rule governs `_opened_by_call`, with one asymmetry that decides the
  first cycle after any upgrade: a file written before the field existed has NO
  key, and a saved-OPEN row with no recorded origin defaults to **call**-opened,
  because legacy `probe_suspect()` produced HALF_OPEN and never OPEN — so a
  probe cannot have been the cause. Reading that absence as "probe" would hand
  the first post-upgrade probe a breaker real calls had opened. A keyless
  saved-HALF_OPEN row defaults the other way, to **probe**: that state is
  reachable both by `probe_suspect()` and by a call trip whose window elapsed,
  so its origin is genuinely ambiguous and assuming "call" would strand a
  provider that never failed a real call.
  **BOTH non-closed states restore, and that scope is what makes the guarantee
  outlive a deploy.** Restoring only OPEN silently expired it at the next
  restart: `.state` assigns HALF_OPEN when merely READ (it mutates and does not
  notify), and `save_state` serialises EVERY breaker's raw `_state`, so any
  other breaker's change persists a call-tripped one as `half_open` — which
  then restored CLOSED, i.e. a dead provider reading healthy and probe-healable
  again, the original defect returning on every merge-restart. The failure
  category restores under the same widened condition, because it qualifies any
  breaker that is not closed; blanking it for HALF_OPEN stripped the reason from
  `observability/snapshots/api_keys.py` and `dashboard/routes/vitals.py`. The ONLY
  external mutator of breaker state is the dashboard toggle
  (`dashboard/routes/providers.py`), and it goes through `force_open()` /
  `force_close()` rather than assigning private fields, so the origin flag
  cannot drift out of step with the state. Both are COMPLETE transitions --
  they notify (which is what the registry wires to `save_state`) and clear the
  qualifiers they invalidate. They are complete with respect to
  BREAKER FIELDS only — neither fires `on_recovery`, so an operator re-enable
  deliberately leaves the `provider_failure` observation open. A guard test in
  `tests/test_routing/test_circuit_breaker.py` parses the tree for assignment to
  breaker private state on a non-`self` receiver; it cannot see `__dict__` or
  `object.__setattr__` writes. KNOWN LIMIT:
  `_opened_by_call` is two-valued while the question now has three answers
  (call / probe / operator), so the dashboard mislabels an operator disable. Degradation levels are
  hand-curated: L2 sheds nice-to-haves; **L3 keeps ONLY micro-reflection,
  embeddings, tagging** — changing those sets changes what survives an outage.
  Some call sites alias another site's chain — don't assume 1:1.
- **routing/escalation.py**: breaker trips → a high-priority `provider_failure`
  observation at 5 trips (~10 min), carrying `first_trip_at` — the only
  per-provider "failing since" timestamp. Once the outage passes
  `_NOTIFY_AFTER_S` (1h) it ALSO writes a `priority="critical"` observation on a
  separate `provider_dead_notify:<name>` content hash, which the existing
  fire-once path (`outreach/scheduler.py::_critical_observations_job` +
  `mark_surfaced`) turns into exactly ONE Telegram. The age is read off the
  unresolved observation, not in-memory state, so it survives a restart; the
  EARLIEST unresolved row wins, because a duplicate would otherwise reset the
  outage clock. **The hour check is CLOCK-driven, not trip-driven**: the
  awareness tick (5 min) calls `sweep_due_notifications`, which reads the
  durable rows and holds no in-memory state — the trip-driven version starved
  when traffic stopped and its per-process flags produced four review defects.
  Lever: `provider_outage_notify` domain (off/propose_only/live) +
  `GENESIS_PROVIDER_NOTIFY_DISABLED`; off resolves open notify rows (so off→on
  re-notifies a still-dead provider, deliberately). Recovery resolves BOTH
  hashes — **notify hash FIRST**: the two
  are separately committed (this connection has no transactions), so a failure
  between them must leave the VISIBLE row open (a provider shown as failing when
  it is not, which the next recovery clears) rather than the SILENT one (an open
  notify row makes `skip_if_duplicate` suppress this provider's notifications
  until some later recovery happens to succeed). Do not raise the 10-minute
  observation to critical instead — it would page on every transient blip.
  ONE KNOWN GAP remains documented in-code, not fixed: the row is stamped at
  the 5th trip rather than the first (duration short by the ramp) — its fix
  needs the `first_trip_at` anchor bounded first; see the follow-up. The
  restart-gate gap that used to sit beside it is GONE by construction: the
  sweep consults no in-memory flag, so a restart changes nothing.
- **providers/**: the `ToolProvider` registry for NON-LLM tools (search,
  embeddings, STT/TTS, crawl, probes). Adapters register GATED ON ENV KEYS —
  silent non-registration is by design (absence ≠ bug). LLM breaker/health
  logic lives in routing, not here. No embedding provider registered → memory
  silently degrades to FTS5-only.
- A load-time guard warns if an `openrouter` provider flagged `free: true` points
  at a non-`:free` (paid) slug — the openrouter-free billing blind spot
  (`_detect_mislabeled_free_openrouter`, config-only, visibility not gating).

## 12. Platform & data

The load-bearing floor: database, runtime bootstrap, resilience, observability,
config resolution, and hygiene utilities.

```yaml subsystem-map
entry: platform-data
modules: [db, runtime, resilience, observability, security, codebase,
          restore, util, infra_profile, onboarding, env.py, _config_overlay.py]
verified: 50b79ffb 2026-09-01
```

- **onboarding/**: the live *functional floor* (`floor.py`) — the honest "is this
  install usable" signal (CC OAuth login + ≥1 routing LLM key + ≥1 embedding key),
  computed on demand from persisted `secrets.env` + CC OAuth state. The LLM leg is
  **derived from the router's own merged config** (`routing.config.load_config`,
  honoring the `model_routing.local.yaml` overlay) reduced to the provider types
  referenced by an active call-site `chain` — resolved via the same env-var patterns
  as `litellm_delegate._resolve_api_key` — so it can't drift from what routing
  actually uses (a declared-but-unchained provider doesn't count); the embedding leg
  pins to the real cloud backends (`API_KEY_DEEPINFRA`/`API_KEY_QWEN`). Deliberately decoupled
  from the `~/.genesis/setup-complete` marker (which means only "bootstrap
  finished"). Single source of truth shared by three surfaces: the dashboard
  `setup-status` route, the ego cadence gate (`_should_run` requires marker AND
  `floor_met`), and the CC session-start onboarding prompt. Above the floor,
  `readiness.py` layers a cumulative 4-tier model on the same primitives —
  **T0 Bootstrapped → T1 Functional (`floor_met`) → T2 Connected (proactive
  Telegram reach: bot token + ≥1 valid `TELEGRAM_ALLOWED_USERS` id, parity-pinned
  to the live adapter start-gate `channels.bridge._load_bridge_config`) → T3
  Autonomous (`ego.enabled`)**. Pure over secrets + one injected `ego_enabled`
  bool (no runtime import on the hot path); web-search/autonomy-posture/surplus are
  enrichment, never tier gates (autonomy has no on/off switch — it's always
  initialised and gated per-action by the mandatory approval gate). `setup-status`
  emits `tier`/`tier_name`/`telegram_configured`/`ego_enabled` additively, plus
  non-gating **enrichment** from `readiness.compute_enrichment` +
  `autonomy.config_read.read_autonomy_default_level` (fail-safe, presence-based):
  `web_search_keyed_providers` (premium providers augmenting the keyless SearXNG
  baseline), `voice_configured` (explicit `VOICE_S2S_PROVIDER` opt-in — a bare
  OpenAI LLM key does NOT count), `ego_cadence_minutes`, and `autonomy_level`
  (shipped config default, not the live earned level). The persistent Overview
  **Readiness panel** that renders the tier rail + these enrichment chips (config-
  framed, refreshed on return to Overview) is **PR-B2b** — shipped.
- **db/**: aiosqlite WAL behind `SerializedConnection` (an asyncio.Lock —
  without it interleaved commits pin `in_transaction` until restart). Two
  schema paths coexist: base DDL (`schema/_tables.py`, 118 CREATE TABLE; docs
  still say "60+") plus versioned `migrations/` run ONCE at startup before any
  other init step touches data; a failed migration ABORTS bootstrap. Ids are
  92 FROZEN legacy 4-digit ones (`0001`..`0093`, with a GAP at `0092` from a
  rename) plus new `YYYYMMDDHHMMSS_*.py` UTC timestamps — nobody allocates an
  id any more, which is what removed the cross-branch collisions. The legacy
  set is ENUMERATED in `db/_migration_ids.FROZEN_LEGACY_FILES` (a range cannot
  express the `0092` gap), and discovery REFUSES a directory holding any file
  it cannot classify, because a name matching nothing is discovered by nothing
  and would never run. Discovery enforces RUNNABLE; CI
  (`scripts/check_migration_prefixes.py`) additionally enforces this repo's
  freeze, so a fork's own 4-digit migration runs rather than bricking its boot.
  EVERY table must be in BOTH paths (fresh-install DDL + its numbered
  migration) — a design CONVENTION whose TABLE-set parity is NOT test-enforced:
  `test_db/test_schema.py`'s `EXPECTED_TABLES` allow-list only pins the DDL
  path's table SET (its `db` fixture builds via `create_all_tables` +
  `seed_data`, never the numbered migrations), and no test asserts DDL-vs-
  migration table-set parity (nor declared-TABLES-vs-sqlite_master). The one
  base-path guard that DOES exist (`test_schema_base_path_parity.py`) is
  narrower — every `INDEXES`-referenced column a numbered migration adds must
  ALSO be mirrored into `_migrate_add_columns` (the #1123/#1127 legacy-index
  crash class), not table presence. Migration atomicity is hand-rolled
  (BEGIN IMMEDIATE + a proxy that blocks stray commits/DDL autocommit) with a
  post-commit reconcile and SQLITE_LOCKED retry (2026-06-25 incident guard).
  **DATA migrations (WS-C, `db/data_migrations/`) are the OPPOSITE contract:**
  non-schema backfills (Qdrant payloads, entity graphs) that run POST-boot as a
  background `tracked_task` (kicked from `runtime/_core`), never abort boot, are
  idempotent, and are claimed atomically via the `data_migrations` ledger (so
  server + bridge-fallback can't double-run). `d`-prefixed modules expose sync
  `migrate()`+`verify()` (runner offloads via `to_thread`); `requires_operator`
  ones sit `operator_pending` and never auto-run. Shared file-discovery with the
  schema runner (`db/_migration_discovery.py`), deliberately NOT the atomic-txn
  proxy. Seed `d0001` mirrors SQLite `origin_class` onto Qdrant — idempotent, so
  a lagging install self-heals on next pull+restart with no control plane. Bulk
  write/delete migrations MUST commit in batches (`_util.commit_in_batches`) so a
  big backfill never holds the single WAL writer long enough to starve the live
  server (which waits only 5s for the lock); the runner also retries a
  lock-contended ledger write (#1179 regression guard).
- **runtime/**: sequential bootstrap (secrets → db → … → sentinel, ~27 steps);
  each step records ok/degraded/failed in the manifest — only db aborts.
  `~/.genesis/capabilities.json` + `bootstrap_manifest.json` are projected at
  bootstrap tail; readonly probes must never clobber the primary's state.
  New capabilities need `_CAPABILITY_DESCRIPTIONS` registration. Autonomy init
  installs a fail-closed `DenyHighRiskSentinel` FIRST so ctor failures degrade
  to blocking. GROUNDWORK: task-verify (constructed, `.verify()` never called
  — dark), web-dd.
- **resilience/**: RecoveryOrchestrator on a 30-min interval (3 confirmation
  probes before draining); `DeferredWorkQueue` priorities + staleness policies;
  dead-letter replay. The `dream_synthesis_slice` worklist is deliberately
  excluded from the backlog alarm (drift-guard test pins it).
  `NetworkSentinel` (`network_sentinel.py`) probes the open internet (DNS+TCP to
  public anchors) on a cadence and publishes a 3-state `network` axis
  (NORMAL/DEGRADED/OFFLINE) with asymmetric hysteresis — it OWNS its hysteresis
  and opts out of the state machine's symmetric flap protection. The axis drives
  watchdog zombie-restart suppression (a restart can't fix an ISP outage), the
  `is_any_degraded` recovery gate (at OFFLINE only — don't re-dispatch into a
  dead network), and the dashboard "Internet" light. Two OFFLINE-parking
  consumers (PR-3) gate on `network.parking_mode` (off/shadow/live, default
  shadow) through the shared `network_config.parking_decision()` gate: the
  **CC-invoker network preflight** (`cc/invoker.py::_network_preflight` — raises
  `CCNetworkOfflineError` BEFORE the subprocess spawns for a WAN endpoint,
  turning a `timeout_s`-bounded hang (7200s default) into a sub-second fail; a
  LAN CC peer, classified via `util/netclass.py`, still runs), and the
  **surplus tier filter** (`surplus/dispatch.py::_filter_tiers_for_network` —
  strips cloud compute tiers so internet-dependent tasks stay `pending` instead
  of churning `failed`). The axis stays DELIBERATELY excluded from
  `to_legacy_degradation_level` (broader legacy-level call-site shedding remains
  deferred/tabled). Lever/kill-switch in `network_config.py`
  (`GENESIS_NETWORK_SENTINEL_DISABLED`); window store `~/.genesis/network_state.json`
  (`network_state.py`, stdlib-only, self-bounding). Consumers staleness-check the
  sentinel's own `last_probe_at` and fail toward their safe default.
- **observability/**: event bus dispatches inline AND logs every event;
  persist-queue overflow drops events but emits a rate-limited "dropped"
  meta-event (WS-17). Two health layers (async probes vs systemd shell-out) — a
  probe may HEAL only a breaker that a PROBE downgraded (`probe_suspect`);
  one opened by a real call failure needs a real success, whatever the failure
  category (a models-listing 200 is exactly what a 403-on-use looks like, and a
  false heal resets the outage clock — measured live twice);
  `/health` is a dashboard route, not an MCP tool; `job_health` state machine
  is runtime-owned. `snapshots/deploy_health.py` = merged-vs-deployed drift
  (never does network I/O; host guardian state comes from
  `~/.genesis/host_gateway_state.json`, written by `cc_align_host_sync` on
  every gateway version probe — update.sh and the nightly cc-align timer);
  its `GUARDIAN_HOST_PATHS` must stay in LOCKSTEP with update.sh
  GUARDIAN_PATHS.
- **security/**: prompt-injection defense + outbound scanning — sanitizer is
  LOG-ONLY for internal sources (perimeter EMAIL/INBOX can block);
  `output_scanner` = deterministic outbound secrets/IP scan; `skill_scan`
  shells to external NVIDIA SkillSpector. NOT auth or secrets storage (that's
  `runtime/init/secrets.py` + `env.py`). **`immunity.py` = the WS-3 kill
  switch + gate policy**: `gate_mode()` re-reads `config/ws3_immunity.yaml` +
  its `.local.yaml` overlay per call (no cache, no restart — the
  `ws3_immunity` settings domain is writable); master `enabled: false`
  short-circuits every gate; `is_blockable()` is the never-block-owner/
  first-party invariant every gate routes through; the gate-time fail-closed
  unknown→external rule lives ONLY in `effective_origin_class()` (store-time
  derivation never fail-closes). Auto-demote state is written INTO the overlay
  so state and behavior share one file. **B1: gate 4 (injection) is LIVE in
  SHADOW** — `immunity_shadow.py` records a would-block into
  `immunity_shadow_events` (migration 0055) at all 8 `wrap_external_recall`
  inject sites + the proactive hook whenever `external_untrusted` content
  reaches an action-capable prompt (observe-only — the item still reaches the
  model; owner/first-party never recorded). The gate set is CI-locked in
  `test_recall_inject_coverage.py` (a new inject site or a removed emit fails).
  **Gate 1 (procedure) is LIVE in SHADOW** — `record_would_block(gate="procedure")`
  fires at the three promotion paths that have a trustworthy SOURCE-origin signal:
  the judge convergence (`judge._store_judged_procedure`, covering BOTH the
  struggle and rebuild callers) classifies by a coarse tool-name ingest scan over
  the real transcript spine (`provenance.origin_from_tool_names` — external-ingest
  tool → `external_untrusted`; over-observes by design since fetched content lives
  in tool RESULTS the spine doesn't carry); the autonomy retrospective
  (`executor/trace.py`) classifies by `initiated_by` (Genesis's own execution =
  first_party/owner; the trace has no source-tool spine); and `procedure_store`
  (`mcp/memory/procedural.py`) classifies by the CALLER session's origin
  (`session_origin_from_env()`, coalesced to first_party when the env is unset —
  an unset env is not a dispatched session; skipped duplicate teaches emit
  nothing). One promotion path remains DEFERRED (classified
  `deferred-with-reason`, no emit): the deprecated auto-extractor (`extractor.py`
  — its only signals are replay tools or a hyphen-truncating prose scrape, both
  undercount). CI-locked in `test_procedure_gate_coverage.py`.
  **Gates 2-3 (identity/autonomy) are LIVE in SHADOW.** Gate 2: the steering
  write (`learning/pipeline.py`) emits with a CHANNEL allow-map origin
  (`_CHANNEL_ORIGIN`: terminal/telegram/whatsapp/web = owner; voice + unknown
  channels fail CLOSED to external_untrusted — the polarity fix for the
  fail-open `_AUTONOMOUS_CHANNELS` deny-list, so a deny-list escape is now
  OBSERVED), and the USER_KNOWLEDGE synthesis (`runtime/init/learning.py`)
  emits with REAL propagated provenance — it aggregates the accepted deltas'
  stored `origin_class` (`external_untrusted` iff ANY contributing delta is
  external, else `first_party`), no longer hardcoding first_party-by-authorship.
  The reflection writer (`perception/writer.py`) stamps each user-model-delta
  observation with `origin_class=run_origin`, a RUN-LEVEL aggregate
  (`reflection_window_origin` → `external_untrusted` iff any external session
  overlapped the window). Residual: provenance is run-level, not per-delta (a
  per-delta signal is still future work); NULL/legacy deltas read first_party.
  Gate 3: the emit lives INSIDE
  `db/crud/capability_grants.py` (record_success/record_correction/apply_event
  — `origin_class` is a REQUIRED kwarg so every future caller must state
  provenance); all six live callers thread owner/first_party → zero rows today
  by construction. Locks: `test_identity_autonomy_gate_coverage.py` pins the
  loader's 4-method write_text surface by set-equality + the dashboard PUT
  writer manually, and discovers grant-mutation callers ALIAS-RESOLVED (bare
  `record_success` name collisions excluded). The legacy `autonomy_state`
  evidence store is a documented out-of-scope exclusion. Auto-demote wired but
  dormant (server + enforce only); retention via
  `scripts/prune_immunity_shadow.py` (disk-hygiene). The shadow log is readable
  via the `immunity_status` health MCP tool (gate-agnostic: per-gate live mode
  + per-site would-block counts — sizes the B4 enforce blast radius).
  **B4: stored-origin recall + enforce for gates 3-4 (shipped shadow; flip is a
  live `settings_update`).** Recall now plumbs the stored `origin_class`
  (migration 0054) end-to-end — `RetrievalResult.origin_class` on both the
  Qdrant and FTS5-only paths (the latter via a `search_ranked` column,
  coalescing SQLite when a pre-backfill payload is None) — so
  `item_is_blockable` is STORED-FIRST (widens to episodic-external rows; fixes
  the first-party-in-KB over-observe). A second CI sweep
  (`KNOWN_QDRANT_READ_SITES`) locks every direct Qdrant `.scroll`/`.retrieve`
  content→prompt surface; it caught `memory_core_facts` (now gated). The
  gate-2 L-tier substrate: `cc_sessions.origin_class` + `observations.origin_class`
  (migration 0057), stamped at registration from the DISPATCH PROFILE (never a
  tool scan); reflection `user_model_delta` writers carry a run-level window
  aggregate (`cc_sessions.reflection_window_origin`), so the identity emit
  derives real provenance instead of hardcoded first_party (gate-2 stays
  shadow). Enforce (gates 3-4 only; procedure/identity rejected by the
  validator honesty guard): gate-4 drops `external_untrusted` from PUSHED feeds
  (`memory_proactive`, `memory_core_facts`; the proactive hook needs no filter —
  dispatched sessions exit it at module import, total absence) ONLY in dispatched
  UNSUPERVISED sessions under enforce — the discriminator is
  `GENESIS_CC_SESSION` present (stamped unconditionally on every CCInvoker
  child) AND `GENESIS_SESSION_SUPERVISED` absent (`CCInvocation.supervised`,
  set only by ConversationManager's owner-attended invocations).
  `GENESIS_SESSION_ID` is attribution only — foreground conversations carry
  one and some autonomy dispatches don't, so it is wrong in both directions
  as a supervision signal. Explicit queries
  (`memory_recall`/`knowledge_recall`/`memory_expand`) and every foreground
  surface keep wrapped external in all modes (`should_enforce_drop`, fail-open);
  gate-3 refuses grant evidence/state writes with a blockable origin — and the
  refusal is read-only (no `ensure_cell` before the guard: external provenance
  can't even seed a NOT_DETERMINED cell). Wrap + provenance labels are
  STORED-FIRST at every inject surface (review round): `wrap_external_recall`
  and `provenance_descriptor(origin_class=…)` key on the stored origin with the
  collection check as fallback, so external EPISODIC rows are delimited/labeled
  external everywhere (MCP recall/expand/proactive, hook `Memory·external` tag,
  context injector, voice, research executor, dashboard) — the wrap is the
  compensating control on the explicit surfaces the enforce cut retains. Every
  drop/refusal still records (the enforce-mode row IS the block ledger).
  Auto-demote now pages a `critical` `infrastructure_alert` when a gate stands
  down, and counts only ENFORCED INTERVENTIONS (`count_enforced_interventions`
  — rows whose detail carries `refused`/`enforced_drops`), never wrap-only
  observation rows, so a normal explicit-recall session can't flip the gate
  back to shadow. Red-team acceptance: `test_redteam_enforce.py` (synthetic).
- **codebase/**: AST indexer (surplus task, set-difference deletes with
  CASCADE) behind the `codebase_navigate` MCP tool.
- **infra_profile/**: the infrastructure body schema — deterministic fact
  collectors (container plane + host plane via the guardian `host-profile`
  gateway verb; a missing guardian or un-redeployed gateway degrades to
  "not visible from this vantage") → per-section hashed `profile.json` +
  rendered `INFRASTRUCTURE.md` under `~/.genesis/infrastructure/`. **The
  facts/metrics split is load-bearing**: only `facts` are hashed; a hash change
  emits a dedup-gated `infrastructure_drift` observation and regenerates that
  section's LLM annotation (call site 46, strong-first — annotations are PINNED
  to source hashes; staleness derived at render, never stored). Consumers: boot
  step (delayed, non-blocking) + daily 06:20 cron + `infrastructure_profile`
  MCP tool (facts-only refresh cross-process, flock-guarded) + sentinel digest
  + the user-CLAUDE.md `container-specs` block (content owner:
  `infra_profile/claude_md.py`; update.sh invokes `--claude-md-block`).
  Distinct from `observability/snapshots/infrastructure.py` (dynamic health) —
  don't merge them. Memory-resilience invariants are first-class facts:
  container `cgroup_memory_swap_max` (tri-state — "0" IS the 2026-07 wedge
  state) + `oomd_user_slice_kill` (config-plane scan of user.slice.d drop-ins,
  laid down by `scripts/lib/memory_resilience.sh` from install/bootstrap/update) and
  host-plane `swap_total_kb`, so the annotation layer flags unprotected
  installs (see docs/reference/memory-resilience.md). Network-resilience
  invariants are first-class too: container `networkd_keep_configuration` +
  `network_watchdog_installed` (any-link/file-present facts for the annotation
  layer) alongside the posture check's *effective* variants
  `networkd_default_route_keepconfig` + `network_watchdog_enabled`, all gated by
  `networkd_manages_default_route` (the applicability gate — networkctl reports
  the default-route link `AdministrativeState=configured`, so the posture check
  stays silent on NetworkManager installs), plus a volatile `watchdog`
  heal-telemetry metric from `/run/genesis-network-watchdog.json` (see
  docs/reference/network-resilience.md).
- **restore/**: thin CLI → `scripts/restore.sh` (counterpart of the 6h
  encrypted `scripts/backup.sh` timer).
- **util/**: `atomic_write_text`, `tracked_task` (logs swallowed exceptions),
  `process_lock` (the reason bare `python -m genesis serve` blocks systemd),
  tmp discipline (`~/tmp` for large temp — never override TMPDIR).
- **env.py**: 3-tier resolution (env var → `~/.genesis/config/genesis.yaml` →
  default). **`update_in_progress()` is load-bearing**: the watchdog defers
  restarts during deploys (mid-deploy revival deadlocks bootstrap); fails open
  to "no deploy". `secrets_path()` is repo-relative unless SECRETS_PATH set.
- **_config_overlay.py**: `.local.yaml` deep-merge (user config dir first;
  dicts merge, lists REPLACE wholesale); dependency-free by design to stay
  import-cycle-safe.

## 13. Modules, skills & self-extension

The pluggable edges: capability modules, the skill library, and the pipeline
for contributing code upstream.

```yaml subsystem-map
entry: modules-skills
modules: [modules, skills, contribution, bookmark, workflows]
verified: 50b79ffb 2026-09-01
```

- **modules/**: capability modules are "hands, not brain" — a module may
  observe Genesis but never participates in cognition, and MUST NOT set
  `source_subsystem` on memory writes (test-enforced). Two-phase load
  (config/modules/*.yaml + auto-discovery; YAML wins), enabled-state persisted
  in DB. Shipped: content-pipeline (enabled, ALL auto-features OFF),
  crypto-ops, prediction-markets. GROUNDWORK: autonomous-distribution.
- **skills/**: skills are directories with SKILL.md — registration is catalog
  generation (`scripts/generate_skill_catalog.py` scans `.claude/skills/`,
  `src/genesis/skills/`, `~/.genesis/skill-library/` →
  `~/.genesis/skill_catalog.json`; refresh is prompt-hook-triggered, NOT a
  scheduler — `skill_injection_hook._ensure_catalog_fresh` spawns a detached
  regen when the catalog is missing or >1h stale, `_CATALOG_MAX_AGE_S=3600`,
  serving the next prompt), consumed by the injection hook and by
  autonomous-session resources. Skill refinement is
  propose-only: `learning/skills/applicator.py` STAGES a proposal for human/CC
  review and never writes a skill file. Recording it as a tracked
  cognitive-file modification is DEFERRED — no ledger pre-image is captured
  under propose-only; the apply/resolve path that would create one is a
  follow-up (future WS1 `enforce`).
  Voice-master exemplars are on the contribution FORBIDDEN list.
  Cross-tool export: `scripts/export_agents_md.py` writes a body-scope
  inventory (skills + action tools, never memory/brain) into a managed
  `<!-- genesis:skills -->` block in `AGENTS.md` for Cursor/Codex/other
  runtimes — on-demand and committed (re-run when skills/MCP tools change;
  `update.sh` restores AGENTS.md to HEAD, so the block must live in the commit).
- **contribution/**: `python -m genesis contribute <sha>` — sanitize-then-PR
  upstream, pseudonymous. `sanitize.scan_diff()` is FAIL-CLOSED (8 scanners;
  any finding stops). Its forbidden-globs floor duplicates
  `config/protected_paths.yaml` — keep in sync. **Leak-detection is three-layer,
  no private value hardcoded in tracked source** (a public scanner must not name
  what it redacts): (1) generic CLASS patterns in the tree — all RFC1918, CGNAT,
  fc00::/7 ULA, `/home/<user>` shapes, reusing `scripts/check_portability.sh`
  vocabulary; (2) EXACT install values in the local, generated fingerprint file
  (`~/.genesis/release-fingerprints.txt`, see `contribution/fingerprints.py`) —
  consumed by `_check_fingerprints`, the commit-msg hook, and the pre-push hook;
  (3) the same exact values in the `GENESIS_PRIVATE_PATTERNS` CI secret. CI is
  WARN-not-block on the broad classes (never hard-blocks a contributor's legit
  RFC1918 example); only exact-value matches hard-fail, on canonical non-fork
  PRs. Procedure: `public-repo leak-detection design`.
- **bookmark/**: two-tier session bookmarks stored as episodic memories +
  a lookup table; enrichment runs on surplus compute.
- **workflows/**: YAML DAG executor — GROUNDWORK(workflow-engine), built with
  NO runtime caller. Not live; do not treat as a capability.

## 14. Reflex arc — self-bug detection & repair

The afferent nerve for Genesis's own screaming bugs: detect `task.failed` /
`job.failed` exceptions, fingerprint/dedup them, and (later phases) diagnose →
card → fix under a human-gated tier model. **PR1 (dark) + PR1.5 (observability)
+ PR-2b (job.failed intake) + PR-2c (manual resolve/dismiss with verdict)** —
ingestion, its watch surface, and the human exit lane; no auto diagnose/fix
sessions/LLM yet. Spec: `docs/superpowers/specs/2026-07-21-reflex-arc-design.md`.

```yaml subsystem-map
entry: reflex-arc
modules: [reflex]
verified: 4cc75d50 2026-08-05
```

- **reflex/**: `fingerprint.py` (pure: normalize task name, line-number-free
  frame tail → stable sha, `class_key = ErrorType×subsystem` from the deepest
  genesis frame), `config.py` (`ingest_enabled` gate, default OFF +
  `GENESIS_REFLEX_INGEST_OFF` env kill), `ingest.py` (`ReflexIngestor` —
  bus subscriber ENQUEUES only to a bounded queue; a `tracked_task` worker
  drains + upserts off the event-bus dispatch path). Wired at
  `runtime/init/reflex.py` (after `tasks`), which installs the **default
  event bus** for `tracked_task` (`util/tasks.set_default_event_bus`) ONLY
  when ingestion is enabled — before this, ~63 of 66 `tracked_task` sites
  emitted no failure event.
- **Tables** (`reflex_signals` upsert-deduped by `fingerprint`;
  `reflex_diagnoses`; `reflex_verdicts` = the taste corpus, never pruned).
  Later-phase columns/statuses ship now so the lifecycle CHECK never needs a
  rebuild migration.
- **Afferent sources**: `task.failed` (from `tracked_task`) is the original
  nerve, but it is a near-empty channel — Genesis's background loops catch their
  own exceptions and never reach the `tracked_task` done-callback (0 events in
  108d, verified two installs). The **funnel (2026-07-23)** adds the real
  stream: `runtime/_job_health.record_job_failure` emits a throttled
  `job.failed` for every exception-driven background-job failure (see the
  scheduled-job telemetry entry). **Ingested as of PR-2b**: `ingest.py`
  consumes `task.failed` AND `job.failed`, guarded on `error_type` presence so
  only real exceptions become signals (a reason-only failure has no
  `error_type` and stays in the job-health/Sentinel lane — the
  `failure_details` contract).
- **Trap**: BOTH `task.failed` and `job.failed` are carved out of the ego
  reactive path (`runtime/init/ego._is_reflex_owned_event`) — reflex owns that
  class; the ego's message-keyed dedup can't absorb variable-payload failure
  bursts (a documented storm mode in `ego/cadence.py`).
- **Observability (PR1.5)**: aggregates on `db/crud/reflex_signals`
  (`count_by_status` / `top_class_keys` / `list_recent`) feed four surfaces —
  the `reflex_status` MCP tool (process-portable: config + DB only), the
  in-server health snapshot (`observability/snapshots/reflex.py`, reads live
  ingestor counters via `GenesisRuntime.peek()`, degrades to DB-only),
  a `reflex` block in `status.json` (isinstance-guarded; consumed by the
  standalone health MCP), and a dashboard overview card (`reflexSemantic()` —
  deliberately NOT in `overallHealthSemantic()`: a detected signal is the
  subsystem working, not the system degrading).
- **A4 sweep**: remaining fire-and-forget bare `create_task` sites wrapped in
  `tracked_task` (`cc_fallback_probe`, `escalation` ×2); awaited-elsewhere
  sites (`knowledge/orchestrator` tree_task, `health_data` single-flight,
  `events._db_writer`) stay bare with in-code comments — wrapping would
  double-report exceptions their callers already handle.
- **Human exit (PR-2c)**: `reflex_signal_resolve` MCP tool moves a signal to a
  terminal status — `fixed`→`resolved` (real bug fixed out-of-band, e.g. a
  normal PR; no verdict row, it isn't a card judgment), `not_a_bug`/`wont_fix`→
  `dismissed_*` + a taste-corpus verdict (`db/crud/reflex_verdicts.record`, the
  first writer of that table; `verdict_point='diagnose_card'`). Idempotent on
  terminal signals; the status transition is guarded on the observed status so a
  race is a conflict, never a clobber. Until the auto diagnose lane ships this is
  the ONLY way a signal leaves `new`.
- GROUNDWORK: auto diagnose lane (PR2), fix lane (PR3) — `reflex_diagnoses` has
  no writer yet and the card/gate/dispatch flow is NOT built. `reflex_verdicts`
  is now live (manual dismissals, PR-2c); its diagnose/fix/promotion verdict
  points remain groundwork.

## 15. Federation — private cross-owner relay

The private, human-in-the-loop Genesis↔Genesis channel: a turn-gated, cross-OWNER,
cross-install link between two DIFFERENT users' installs (knowledge exchange +
coordination + delegation). Requires no SSH and no access to sensitive data on
the peer. A **standalone subsystem, NOT a channel adapter** — the counterparty is
an UNTRUSTED peer, inverting every other channel's owner-trusted model, and the
outreach pipeline's HITL hold is email-hardcoded. Secure-by-design,
least-privilege, HITL: the agent PROPOSES, only the human COMMITS; the user's
approval always trumps a peer's urgency.

```yaml subsystem-map
entry: federation
modules: [federation]
verified: 1d75bc21 2026-08-31
```

- **STATUS (v1 PR1 — FOUNDATION, dark):** the data + crypto + transport layers
  exist and are tested; the subsystem is NOT yet wired into the runtime and ships
  `mode: off` by default. Pairing / inbound / outbound / HITL-hold / lifecycle
  land in PR2.
- **crypto.py**: PyNaCl only. Ed25519 detached signatures, **domain-separated**
  with LENGTH-PREFIXED framing (`DOMAIN | len(context):2 | context | message`) so
  a `pairing` signature can't be replayed as a `msg` and no byte inside `context`
  can shift the boundary; X25519 `Box` encryption with a DISTINCT key (never reuse the signing
  key); `SecretBox` at-rest sealing; a FROZEN canonical payload encoder; the
  transcript hash chain (`payload_hash = H(prev_hash || canonical(payload))`); and
  the SAS fingerprint for the out-of-band MITM check at pairing. v1 uses static
  Box keys (no forward secrecy — documented, acceptable for a low-volume HITL
  channel; the envelope reserves a version/ephemeral slot for a later ratchet).
- **keystore.py**: the local identity — Ed25519 + a distinct X25519 key in a
  `0600` keyfile under `~/.genesis/federation/identity.key` (GENESIS_HOME-aware),
  created LAZILY on first pairing (empty-state clean). DB write-caps are
  additionally `SecretBox`-sealed with a key DERIVED from the identity seed (no
  extra `secrets.env` bootstrap secret).
- **transport.py**: the swappable store-and-forward interface (`send`/`poll`/`ack`,
  at-least-once + `envelope_id` idempotency; the RECEIVER is the dedup/ordering
  authority via the hash chain). `MockTransport` (in-process loopback) proves the
  boundary in tests; `RelayTransport` is a GROUNDWORK stub for the PyNaCl-relay
  client (lands with the relay server). The transport is DUMB — it moves opaque
  ciphertext envelopes and never inspects the payload.
- **config.py** (+ `config/federation.yaml`): the live-read `off | propose_only |
  live` lever. OPT-IN — default `off`; an invalid/broken config degrades to `off`
  (least authority, never a silent receive/send); `GENESIS_FEDERATION_DISABLED=1`
  is the background-safe kill switch. `propose_only` = the safe operational mode
  (inbound queued, outbound held); `live` is reserved for v2 per-contact
  earned-trust automation.
- **data**: `federation_contacts` (one per paired peer — pinned keys, mailbox
  coordinates, sealed write-cap, per-direction chain heads) + `federation_messages`
  (the per-contact hash-chained transcript; bodies prunable, chain skeleton kept
  forever). Migration 0090; CRUD `db/crud/federation.py` (`append_message`
  advances the chain head atomically). Retention: transcript bodies pruned after
  `retention_days` (disk-hygiene path).

---

*Maintenance: run `python scripts/check_subsystem_map.py` from the repo root;
CI runs it on every PR. Entry stamps mark the commit each entry was last
verified against — bump them when you re-verify, not when you merely edit
prose.*
