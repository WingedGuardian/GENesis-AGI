# Changelog

All notable changes to Genesis are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows Genesis release stages (v3.0a → v3.0b → v3.1 → v4.0a…).

---

## [Unreleased]

### Security

- **The contribution sanitizer now blocks Tailscale addresses before they can reach the public
  repo.** When you prepare a community contribution, the pre-push privacy scan now catches Tailscale
  CGNAT and Tailscale IPv6 addresses, and flags the full private `10.176` subnet range (not just two
  hard-coded addresses) — closing a gap where these install-specific addresses could otherwise slip
  into a public PR. The commit-message guard gained the same IPv6 coverage.

- **The HTTP client Genesis uses for outbound requests is upgraded to clear 11 security advisories.**
  `aiohttp` — the library behind health checks, provider pings, and market/price-data fetches — now
  requires 3.14.1 or newer, which fixes 11 published CVEs (including a cookie-leak-on-redirect issue
  and several denial-of-service vectors). Genesis uses it only as a client for outbound calls, so
  real-world exposure was limited, but the newer version removes the advisories outright.

### Added

- **You can now run Genesis on Claude Fable 5, and pick the full thinking-effort range on Sonnet and Fable.**
  Fable 5 (Anthropic's new top-tier model) is now a selectable model everywhere you choose one — the ego,
  campaigns, the inbox monitor, the Telegram default, the `/model` command (terminal and Telegram), and the
  dashboard dropdowns. Sonnet (now Sonnet 5) and Fable also accept the full `low`–`max` effort range,
  including `xhigh` and `max`; previously Sonnet was capped at `high`. Nothing switches automatically — your
  existing defaults are unchanged; this only makes the new options available when you want them.

- **Daily disk hygiene now prunes stale scratch and old attention snapshots.** Housekeeping now
  age-prunes leftover files in `~/tmp` (older than 7 days) and garbage-collects attention-engine
  snapshots older than 60 days — but never one behind a moment you've labeled for review, so your
  labeled history stays revealable. Keeps disk usage from creeping up between the reactive cleanups
  that previously only fired when the disk was nearly full.

- **Genesis now notices when its Claude Code subscription hits its usage cap — instead of quietly going dark.**
  A capped Anthropic subscription makes `claude -p` return *empty* output with no error, which Genesis used to
  record as a successful (but blank) run — so its background thinking (ego cycles, reflections, weekly reviews)
  could silently produce nothing for days without anyone noticing. Genesis now watches for a run of empty
  results on calls that should have produced output and, when it sees one, sends a single critical alert
  ("CC subscription likely capped — degraded until the limit resets") so you know to check. It's detection only:
  it never changes how a call runs, and it stays quiet during normal idle periods.

- **The Genesis Voice add-on's attention surface now shows the judge's reasoning — and lets you review it.**
  For the optional passive-listening add-on, the buried "Attention" tab is now a top-level **Genesis Voice →
  Judgment** review. Each moment the attention gate noticed is scored by a lightweight LLM judge that says
  whether it was real speech, whether it was worth attention, and — new — a one-word category and a short
  reason. You review the judge (worth noticing / not worth it / skip) and can jot your own *why*; your notes
  inform the judge's prompt, not any hidden weights. It stays offline observability — nothing here speaks or
  acts, and it's hidden entirely when the voice add-on isn't installed.

- **The dashboard's container-health badge now tells the truth about CPU and memory pressure.**
  Previously the badge ignored CPU entirely (it was hardwired to "healthy") and judged memory only by a
  raw usage figure that's inflated by reclaimable cache — so a busy or memory-throttling box could still
  read all-green. It now factors in actual CPU utilization and PSI (pressure-stall) readings: it stays
  green when the box is merely holding reclaimable cache, and only turns amber/red when CPU or memory is
  genuinely stalling work, with a reason that says which.

- **The dashboard now shows the memory each Claude Code session is using, and warns you if one balloons.**
  Each concurrent session normally uses well under a gigabyte; the Container card now lists per-session RSS, and
  if a single session climbs past a high ceiling — a sign it may be leaking — Genesis raises a health alert
  (reaching Telegram at the critical level) so you can restart just that session instead of finding out the hard way.

- **Genesis can now shelve "someday/maybe" ideas separately from its actionable to-do list.**
  Until now, every deferred item a session or the ego created landed in the actionable follow-up
  queue — even low-value "might be worth doing someday" ideas — so the queue filled with things
  nobody intended to act on. Genesis can now file those into a separate "tabled" lane instead
  (the dashboard Follow-ups tab already supported this; now Genesis's own sessions and ego can too,
  and can move an existing item between the two lanes). Tabled items are tracked but never surfaced
  as work or auto-actioned, keeping the actionable list focused on real commitments.

- **Genesis now catches scheduled jobs that silently stop working — running on schedule but never
  succeeding.** Some background jobs (like the weekly self-assessment and quality calibration) could
  fail week after week without ever showing up as "failed," because the failure counter is reset
  every time the server restarts. Genesis now watches the gap between a job's last run and its last
  success: if a job has been running-but-not-succeeding for more than about a week, it raises a health
  alert that reaches your daily report and the dashboard, and the job-health view now shows a
  `days_since_success` figure and a `stale` flag for every job.
- **You can now start an interactive Claude Code session on a different model with one command.**
  `gmodel <name>` launches `claude` on the model you pick: a Claude tier (`gmodel opus`) runs on
  your normal Max subscription, while a roster peer (`gmodel glm-5.2`) runs on that provider's
  native endpoint and its own API key. Plain `claude` is untouched. `gmodel` on its own lists the
  models and which have keys configured; `gmodel --print-env <name>` shows what it would do without
  launching. Your Anthropic subscription is protected — the launcher never lets a stray API key
  quietly switch you to per-token billing, and never sends your Anthropic key to a third-party
  endpoint. (Switch models by relaunching; each session is pinned to one model.)
- **You can now put a model through a "gauntlet" to prove it can actually drive Claude Code
  before you rely on it.** `genesis eval gauntlet --model <name>` has the model (native Claude,
  or a routed roster peer like GLM) fix real broken Python projects inside a live Claude Code
  session, then scores it objectively by running the project's tests — and catches cheating
  (editing the tests or pytest config to fake a pass). Results are recorded so quality can be
  tracked over time. An optional weekly run (off by default, opt-in via the roster's
  `gauntlet.scheduled`) re-checks each roster model and, if one that used to pass starts
  failing, alerts you and files a proposal for your review — it never silently drops a model
  from failover on its own.
- **Genesis now keeps its own disk clean automatically, so it won't quietly fill up and stall.**
  A daily hygiene job reaps git worktrees whose branches have already merged (moving them to a
  7-day recovery trash bin, never deleting work in progress) and clears regenerable caches that
  otherwise creep up over time. If the disk still climbs toward full, Genesis clears the heavier
  reindexable caches automatically at 90% — before the disk hits 100% and disrupts the server,
  its write-ahead log, or backups. Previously the worktree cleanup existed but was never
  scheduled, so it never actually ran.
- **The dashboard now has a Campaigns tab where you can see and control your autonomous campaigns.**
  Each campaign shows its status, schedule (with next fire time), model/effort, today's spend
  against its daily cap, completed runs vs. attempts, and whether a session is currently in
  flight — plus recent run history and live state. You can pause/resume a campaign, run one
  immediately, and edit its cadence, model, effort, daily cost cap, and a new optional
  schedule "jitter" (randomized fire times so ticks aren't perfectly periodic). Until now the
  only way to see or steer a campaign was through Genesis directly.

### Changed

- **The procedures Genesis learns are now concrete, replayable playbooks instead of vague
  summaries.** Previously every learned procedure was written as a "what this teaches: …"
  summary, and the learner only saw a heavily-truncated view of what happened (each tool's
  arguments cut to 80 characters), so the real commands, paths, and flags were lost. Now the
  learner reconstructs the actual step-by-step playbook — with the real commands used — for a
  specific recurring scenario, and skips things that aren't procedures (general best-practices,
  engineering patterns, one-off events, broad workflows; those belong in skills/CLAUDE.md). The
  result is a smaller, higher-signal procedure store.

- **Genesis now learns several distinct playbooks from one session, and stops storing duplicates.**
  A session that accomplished several different things now yields a separate playbook for each
  (instead of one muddled procedure), and a genuinely reusable sub-step can be captured on its own.
  At the same time, before saving a new procedure Genesis checks whether it already knows
  essentially the same one — even if it would be filed under a different name — and skips the
  duplicate. Together these keep the procedure store both more complete and less cluttered.

### Fixed

- **The dashboard's per-session memory row is clearer: "Claude Code Sessions", green when healthy.** The
  cryptic "CC" row that listed sessions as gray "cc-1 …" chips is now labeled **Claude Code Sessions**,
  renders each healthy session in green (amber ≥ 4 GB, red ≥ 6 GB), and shows a hover tooltip explaining
  it's per-session memory for leak detection — so "gray" no longer reads as inactive/unknown.

- **The dashboard's system-health view stays responsive under load and no longer errors out when a single
  check hiccups.** Building the health snapshot used to run its systemd service checks (several `systemctl`
  calls) and a couple of file scans directly on the main event loop, so gathering health could briefly stall
  other work; and if any one sub-check raised an unexpected error, the entire health request failed. Those
  checks now run off the main loop, and a failure in one section degrades just that section to an error state
  while the rest of the health view loads normally. Overlapping health requests now also share a single
  computation instead of each recomputing from scratch.

- **Genesis's background egos now keep their thinking rhythm across a restart instead of going quiet.**
  The two egos run proactive cycles on an adaptive schedule that stretches out when things are idle. That
  schedule was re-armed from scratch on every restart, so an install that restarts often (deploys, recovery)
  could keep pushing the next cycle further out — in the worst case starving an ego for up to its full
  backed-off interval. Each ego now anchors its first post-restart cycle to when it last actually ran: an
  overdue ego runs shortly after startup, while an up-to-date one simply keeps its cadence.

- **Uploading a large or malformed PDF to the Knowledge tab no longer freezes — or crashes — Genesis.**
  PDF text extraction used to run directly on the main event loop, so a big document could stall the whole
  server for seconds (health checks, background thinking, other requests all waited), and a corrupt or
  hostile PDF could take the process down entirely. Extraction now runs in an isolated worker process with a
  time limit: a large PDF no longer blocks anything else, and a PDF that crashes or hangs the parser fails
  just that one ingest — the rest of Genesis keeps running. Uploads that hit a transient database hiccup are
  now marked "failed" (and can be retried) instead of getting stuck showing "processing" forever.

- **Re-ingesting a knowledge source with changed content now refreshes it instead of serving the stale
  version.** Previously, once a file or URL was ingested, re-ingesting the same source was skipped on source
  identity alone — so if the underlying content changed, the knowledge base kept serving the old distilled
  version indefinitely. Re-ingestion now compares a content fingerprint and re-distills when the content has
  actually changed (unchanged content is still skipped, and a now-unreachable source falls back to its
  previously cached version).

- **The dead-letter-queue alert no longer cries wolf on self-healing bursts.** A short burst of low-value
  retry items (e.g. memory-relevance grades, which are discarded within an hour by design) could push the queue
  past its alert threshold and fire a *critical* notification for something that clears itself minutes later.
  The alert now counts only items that are genuinely stuck — pending past their designed self-heal window — so a
  transient burst stays quiet while a real, un-draining backlog still alerts. The dashboard still shows the full
  raw count.

- **The dashboard now reports each ego's cycle health separately.** Genesis runs two egos (a user-facing
  one and its own), and both recorded their proactive-cycle health under a single shared key — so on the
  health surface one ego's last run kept overwriting the other's, making it impossible to tell whether
  either was actually cycling. Each ego now tracks its own health row, so a stalled or failing ego is
  visible instead of masked.

- **Memory extraction now runs reliably after a restart.** The job that turns recent conversations into
  long-term memories was scheduled on a fixed 2-hour interval measured from server start — and that timer
  reset on every restart, so a box that restarted more often than every two hours could keep deferring
  extraction indefinitely. It now also runs shortly after each start, so extraction can't be starved by
  frequent restarts.

- **Guardian's automated recovery now acts on the right service.** Guardian's self-healing (restart,
  journal-freshness, and crash-loop probes) and its diagnostic briefing pointed at a deprecated,
  usually-inactive background unit instead of the main Genesis service. As a result a "restart" could
  report success while healing nothing, and a genuine crash loop of the main service went undetected.
  Recovery, health probes, and diagnosis now target the main service, and a guardrail test keeps them
  from drifting back.

- **Superseded and expired memories no longer resurface in Genesis's automatic recall.** Before every
  prompt, Genesis injects the most relevant memories into its working context. That fast-path recall
  wasn't checking whether a memory had expired or been superseded (replaced by a newer, consolidated
  version) — so an outdated network note, stale career details, an old build-progress snapshot, or a
  duplicate the memory system had already merged away could still surface, even though the main memory
  system filtered all of those out. The fast path now applies the same validity checks across all of
  its sources, matching the main retriever. Memories that are current (no expiry, not superseded) are
  never affected, so nothing live is ever dropped.

- **Completed background-task history no longer grows without bound.** Genesis's idle-time "surplus"
  task queue kept every finished task row forever — only never-started tasks were ever cleaned up — so
  the table crept upward over months of background work. Finished tasks (completed, failed, or
  cancelled) are now aged out after 30 days, while any task still referenced by an open follow-up is
  kept until that follow-up resolves.

- **Deleting a knowledge item from the dashboard now works end to end.** Previously the delete
  button returned a server error, and behind the scenes the item was only half-removed — dropped
  from search but with its stored embedding left behind and its source still marked as already
  ingested, so re-adding that same file or URL was silently skipped and nothing came back. Deletes
  now complete cleanly (search entry and embedding both removed), and once a source's last item is
  deleted, re-ingesting that file or URL works again.

- **The weekly self-assessment and quality-calibration jobs now recover on their own instead of
  going dark for weeks.** Previously each ran only once a week, so if that one run failed — for
  example when the shared Claude Code subscription was capped and returned empty output — the next
  attempt wasn't until the following week, and a multi-day outage could leave them stale for 2–3
  weeks. They now run daily but still complete at most once per week (an idempotency check skips the
  rest of the week once one run succeeds), so a failed day is simply retried the next day until it
  succeeds. Side effect: the successful run now normally lands early in the week rather than on Sunday.

- **`update.sh` no longer hangs if the health watchdog restarts the server mid-update.**
  During an update Genesis stops its server to swap in new code and migrate the database. The
  background health watchdog could see it "down" and restart it right then — and the revived
  server's database lock deadlocked the update's procedure-seeding step, leaving the whole update
  stuck for as long as ~30 minutes with no error. The watchdog now defers restarts while an update
  is in progress, and the seeding step is time-bounded so a contended database fails fast instead of
  hanging silently.
- **Genesis now keeps your Claude Code CLI at the version it's tested against — automatically.**
  Previously the installer only put Claude Code in place when it was *missing*, so if you already
  had an older Claude Code, bumping the pinned version never actually upgraded you — you'd silently
  keep running the old one. Now `install.sh`, `bootstrap.sh`, and `update.sh` all install *or* align
  Claude Code to the pinned version on every run (matching it exactly, so an intentional rollback
  also applies), with no manual step. It's non-fatal: if the update can't run (e.g. no permissions),
  your update still completes and Claude Code is left as-is.
- **A casual message can no longer be mistaken for a permanent "hard rule" — and your
  steering-rules file keeps its structure.** Genesis auto-adds a steering rule only when you
  actually give it a terse directive ("stop doing X", "never do Y"); an ordinary status update
  or chatty reply is no longer captured verbatim as a hard constraint, even if Genesis misread
  the moment. And when a rule is added, the section headings and layout of your `STEERING.md`
  are preserved instead of being flattened into one run-on list.

- **Conversations can take as long as the work genuinely needs, and no longer time out twice in
  a row.** The time budget for a Claude Code turn was a too-short 10 minutes, so substantial work
  started in chat could be cut off mid-task; it is now 2 hours, and a turn that does hit the limit
  no longer silently retries from scratch (which previously doubled the wait before giving up).
  Genesis is also guided to break large jobs into steps and hand genuinely long work to a
  background session it reports back on, rather than leaving you waiting in silence.

- **Genesis no longer mistakes your status updates for its own failures.** When you tell Genesis
  how your own projects, plans, or deadlines are going ("the offer fell through", "I didn't attend
  the conference", "let's keep going on the paper — it's never too late"), it sometimes scored the
  whole interaction as its own "approach failure" — which could trigger a spurious learned rule in
  STEERING.md and dock the autonomy it had earned. Genesis now judges an interaction only by the
  concrete tasks it actually attempted that turn, so sharing context, expressing a future intent, or
  getting a clarifying question back before it acts is correctly treated as success. Genuine
  shortfalls on tasks it did attempt — including when they're mixed into the same message as a status
  update — are still caught.

- **Inbox notes that change without adding anything new no longer get re-scanned over and over.**
  Editing an inbox note in a way that changes its bytes but not its actual content — re-pasting a
  link with different tracking/share parameters, reordering lines, tweaking whitespace — used to
  leave the note looking "modified" on every scan, which (while a link approval was pending) could
  repeatedly cancel and recreate that approval. Genesis now recognizes there's no new content and
  marks the note current in a single scan, so it settles instead of churning.

- **Links that fail partway through an evaluation now retry themselves.** When only some of the
  links in a note evaluate successfully and the rest fail (for example, a few get rate-limited),
  the failed links used to sit untouched until you edited the note again. Genesis now
  automatically re-attempts the stranded links on a later scan on its own — bounded so a link that
  keeps failing eventually stops retrying rather than looping.

- **Inbox evaluations no longer cram a whole batch of links into one giant pass — and stop
  re-evaluating links they've already covered.** When you drop many URLs into an inbox note,
  Genesis now evaluates them in small groups (≈5 at a time, configurable via
  `items_per_eval`), each producing its own `…-N.genesis.md` response file, instead of one
  sprawling evaluation of everything at once. Crucially, once a link has been evaluated it is
  not re-evaluated when you add new links to the same note — only the genuinely new links are
  processed (previously an approved evaluation could re-chew the entire file). When the CLI
  approval gate is on, you approve a drop once and all its groups run under that single
  approval. Duplicate follow-up items from the same recommendation are now also prevented.

- **An approved inbox evaluation that gets interrupted mid-run can no longer be evaluated
  twice.** If the server restarted (or crashed) in the narrow window right after you approved a
  link evaluation but before it finished, the next scan could re-run the same evaluation and
  write a duplicate `…-N.genesis.md` file. Genesis now claims each evaluation the moment it
  starts, so an interrupted one is recovered and retried rather than run a second time.

- **Campaign results no longer sit uncaptured until the next scheduled tick.** Previously a
  campaign that ran every couple of days would finish its background session but not record
  the outcome (or cost, or notify you) until the *following* tick — so a finished run could
  stay invisible for days, and its spend went uncounted. A new reaper now captures finished
  sessions within minutes and cleans up runs orphaned by past crashes, so campaign status and
  cost stay accurate in near-real-time.

- **Genesis now stewards its own open-source pull requests instead of filing-and-forgetting.**
  A new background campaign checks the upstream PRs Genesis has authored (e.g. to litellm,
  Qwen-Agent) every couple of days, and acts on what changed: it nudges a stalled PR once,
  pings you when a maintainer responds or merges, and closes PRs that have gone unanswered
  past a grace window — so contributions don't quietly die of inactivity. It runs in a new
  locked-down session profile whose shell is restricted to the `gh` CLI only; any code
  changes a reviewer asks for are escalated to you rather than pushed automatically.

- **Your morning report now shows Genesis's weekly cognitive-quality grades.** Each week
  Genesis grades its own subsystems (memory, ego, procedural, awareness, reflection) A–F;
  the morning report now surfaces them, so cognitive health is visible at a glance instead
  of buried in a dashboard. Healthy grades compress to a single "nominal" line — it only
  elaborates on a subsystem that's low.

- **Genesis now tells you when its own cognitive quality regresses — and proposes a fix.**
  When a subsystem's weekly grade drops to F or falls sharply from the week before, Genesis
  sends you an alert and files a dashboard proposal to investigate (for example, running an
  experiment on the affected subsystem). Nothing changes automatically — it's a heads-up plus
  a recommendation you approve or dismiss.

- **Genesis can now propose improvements to how it reflects — and apply them only with your approval.**
  The Evo loop measures variations of the deep-reflection prompt against a golden set
  (with held-out re-validation), and when one is a confirmed improvement it files a
  proposal on the dashboard. Approving it updates the live reflection prompt; the change
  is fully reversible (one click rolls it back). Nothing is ever applied automatically —
  Genesis recommends, you decide.

- **Genesis stops cluttering its procedure store with general working-style rules.** When it
  learns a "procedure" from a work session, it now tells a reusable *task procedure* (how a
  specific tool or system works) apart from a *behavioral directive* (a general habit like
  "double-check before acting" — which belongs in its standing instructions, not the procedure
  store). Directives are no longer stored, removing the most common source of near-duplicate
  procedures. The check errs toward keeping, so genuine procedures are never dropped.

### Changed

- **Genesis surfaces its learned procedures by relevance — now including unproven
  drafts, carefully.** A procedure Genesis has learned but not yet validated can be
  surfaced when it's a strong match for what you're doing, but only on a higher
  relevance bar than proven procedures and clearly flagged as an *unproven draft —
  suggestion, not authoritative*. This lets a genuinely useful draft help (and earn
  its way to proven status through use) instead of sitting unused forever, without
  ever presenting it as settled guidance. Blind session-start injection still stays
  limited to the most-proven, always-on procedures. Genesis also now repairs
  procedures that were missing their embedding, so they stop being silently
  invisible to this relevance matching. Genesis also now counts each time a
  procedure is surfaced into context this way, so its own self-learning health
  check reports learned procedures honestly as *reaching* its attention rather
  than falsely flagging them as lost — and this surfacing count is kept strictly
  separate from the signals that promote a procedure, so merely showing a draft
  can never inflate its standing.

- **Procedures you actually use now earn their keep.** How often a learned
  procedure is recalled ("reads") now counts as a dampened usefulness signal:
  frequently-recalled procedures rank higher when surfaced, and can be promoted
  to higher activation tiers (reads alone can reach passive surfacing; the
  proactive advisory tier still requires a real success). A procedure also now
  graduates from speculative to validated on its first real success — previously
  nothing ever cleared that flag.
- **The dashboard Infrastructure card now labels the ambient-capture bridge as "Voice Bridge"** —
  a clearer, user-facing name. It still only appears when a voice/ambient edge is configured.

### Fixed

- **The Internals "composite" self-improvement score is no longer dragged down by draft
  procedures** — Genesis extracts candidate procedures from its own sessions; these start
  unvalidated (near-zero confidence) until they prove useful. The weekly composite score was
  averaging *every* procedure's confidence, so a burst of new drafts made the score crater
  even though nothing had actually regressed. The score now reflects only validated
  procedures. Genesis also caps how many drafts a single session can create, so the
  procedure store stops accumulating dead weight.
- **Genesis no longer floods its own approvals with follow-up emails addressed to itself.**
  A follow-up drafted in a background session could lose its thread (and therefore its
  recipient) on the way to the outreach queue, then fall back to Genesis's own email address.
  The capability gate correctly held each one for approval, but because the queue kept
  retrying, a new held "email to myself" piled up every few minutes. Genesis now keeps the
  thread recipient with the queued message, never sends an email to its own address, and
  treats a held or undeliverable message as resolved instead of retrying it forever. A
  message that can never be delivered is now dropped after a day rather than looping.
- **Updates no longer abort when a schema migration actually succeeded** — if the
  database was busy during an update (for example a background task writing at the same
  time), a migration could commit successfully yet still surface a transient "database
  is locked" error, which made the update roll the code back while the database had
  already moved forward. Updates now confirm whether the migration was truly applied
  before treating it as a failure, and give migrations more room to wait out a busy
  database.

## [v3.0b16] - 2026-06-21

### Added

- **Genesis earns email autonomy you can revoke in one click** (#734, #737, #738) — once Genesis has sent a kind
  of email with your approval enough times, it proposes a promotion: it asks "may I send these
  on my own from now on?" — and only you can say yes. If a promoted send ever goes wrong, that
  autonomy is revoked immediately and the next send holds for your approval again, whether the
  system catches it (a send to the wrong person, or a sudden burst of sends) or you flag it
  yourself. A new dashboard **Autonomy** tab shows what Genesis is allowed to do on its own and
  a log of what it has done, with a "Flag as bad" button on every autonomous send. A per-send
  Telegram notice is available but off by default (the tab is where to look) — turn it on with
  `email_send_notify` in the autonomy config.

- **See what Genesis did as a timeline** (#718, #726) — the dashboard has a new **Traces** tab that
  renders each recorded operation (a reflection, an ego cycle, a dispatched session) as a
  nested waterfall: pick a recent trace and its LLM calls, sub-sessions, and tools lay out as
  bars on a shared timeline, with click-through detail for any span (provider, model, tokens,
  cost, attributes). It reads the traces Genesis already captures, so you can inspect an
  operation end to end instead of piecing it together from logs.

- **Genesis can A/B-test its own thinking before changing it** (#729) — a new experimentation
  harness runs two versions of a cognitive config (for example a reflection prompt, or an
  awareness signal weight) against a graded golden set, measures which does better with a
  real significance test, and surfaces a recommendation you act on — it never promotes a
  change on its own. It guards against gaming its own grader (the rubric must be calibrated,
  and a "win" has to survive a second, independent judge). Results show up in the new
  `experiment_status` health tool, and a weekly "cognitive drift" snapshot now tracks whether
  Genesis is still challenging itself (dissent rate, proposal diversity).

- **Genesis spots goals it's stuck on and asks before easing off them** (#720) — when a goal you
  set has been worked on (several dispatched sessions) but still isn't moving, Genesis now
  recognizes it as *stuck* rather than merely idle, bumps it up for review, and digs into
  *why* it stalled instead of nudging it again. If it concludes the goal should be paused or
  deprioritized, that becomes a proposal you approve or reject — nothing about your goals
  changes without your say-so.

- **Genesis records traces of what it does** (#718, #722) — reflections, ego cycles, every LLM
  call, and the tools its dispatched Claude Code sessions run are now captured as
  nested trace spans (one trace per operation), so its activity can be inspected
  end to end instead of pieced together from logs. Capture is on by default and can
  be turned off via the new `observability` settings domain (or
  `GENESIS_SPANS_DISABLED=1`); spans are kept for a configurable window (default 14
  days) and pruned automatically.

- **You can undo a change Genesis made to its own skills or calibration** (#717) — when Genesis
  autonomously refines a skill, retunes its triage calibration, or re-synthesizes its user
  knowledge, it now keeps a recoverable snapshot of the previous version. If one of those
  self-edits turns out worse, you can list the recent self-modifications and roll any of them
  back to its prior contents — with a safety check that refuses to overwrite a file that has
  changed since (unless you force it).

- **Earned autonomy can be restored after a regression** (#715) — when Genesis loses a level of
  autonomy in a category (e.g. after a correction), that demotion is no longer a dead end. Once
  the category's track record recovers enough that the evidence again supports the earned level,
  Genesis proposes restoring it and asks you to approve — it never silently re-grants authority,
  and it won't nag while the lower level is genuinely warranted. Previously a demoted category
  had no path back up.
- **Genesis tells its own memories apart from what it read on the world** (#716) — every recalled
  knowledge-base item (ingested docs, and the new corrective web results) is now labeled
  "external-world knowledge (source: …)" wherever it reaches Genesis's context: explicit
  recall, the proactive memory hook, voice, and the dashboard memory search. First-party
  memories (Genesis's own observations and your conversations) stay labeled as such, so the
  model never mistakes an ingested document — or a web snippet — for its own ground truth.
  The knowledge-base relevance floor that keeps low-quality bulk content out of answers now
  applies reliably (it previously slipped past keyword-only matches).

- **Genesis self-corrects a bad memory recall instead of running with it** (#711) — on high-stakes
  lookups (the explicit memory and knowledge recall tools), Genesis now grades whether the
  recalled results are actually on-topic, and when a recall comes back clearly irrelevant it
  automatically tries again — broadening the search, drawing on the knowledge base, and (for
  knowledge queries only) the web — rather than feeding itself off-topic context. Conservative
  by design: confident recalls are left untouched, grading is skipped when results are already
  strong, and it fails fast so it never slows a recall if the grader is unavailable. Latency-
  sensitive paths (the proactive hook, voice, in-session context injection) are unaffected.

- **The dashboard's Observations panel shows where each item stands** (#697) — every observation
  now carries a colour-coded stage badge: **new** (unread, still needs attention), **read**
  (Genesis has seen it), **acted** (it drove a proposal or follow-up), or **resolved**.
  Already-seen items stop blaring, so the panel — and Genesis's own thinking — surface what's
  genuinely new instead of a wall of stale alerts.

- **Browse and manage your reference store from the dashboard** (#674, #676) — a new **References** tab
  lists every credential, URL, IP, and account handle Genesis has stored, grouped by kind, with
  search and a per-entry badge showing whether you saved it (verified) or Genesis auto-captured
  it. Secret values stay hidden until you click reveal, then you can copy or delete any entry.
  This replaces the old `~/.genesis/known-to-genesis.md` text file (now retired) with a single,
  always-current, access-controlled view — no more stale or secret-leaking flat file.

- **Genesis now detects and auto-heals a stalled Guardian updater** (#669, #670) — it watches whether the
  Guardian's *deployed* updater script on the host matches the code it has actually pulled. If
  the updater silently froze (the failure that left it ~2 months stale), Genesis notices within
  a few checks, automatically redeploys the current updater once, and re-verifies — escalating
  to you only if the self-heal doesn't resolve it. Closes the blind spot where the host kept
  pulling new code while its updater quietly stopped refreshing.

- **A new "deliverable-builder" skill produces send-ready work, not raw markdown** (#657) — when you
  ask Genesis to build a job take-home, client report, one-pager, or deck, it runs a gated
  pipeline: it frames the deliverable with you (audience, format, what leads), drafts and
  structures it to lead with the strongest point, writes it in your voice, strips AI tells,
  renders it to the right file format (PDF or DOCX, never a raw `.md`), and a fresh-context
  reviewer checks the finished artifact against the original requirements before it reaches you.
  The session won't quietly end with an unverified deliverable.

- **Background tasks can now produce those deliverables on their own** (#668) — when a `/task` you
  submit will produce a send-ready artifact (report, deck, take-home, one-pager), the intake now
  captures how it should look and read (format, visual style, whether it must pass as fully
  human-written, audience), and the autonomous executor runs the deliverable-builder pipeline as
  the final step — handing you the finished, verified file instead of a raw dump. Rendered
  documents now default to a clean modern font (so they read like a real document, not a LaTeX
  paper); set the visual style to `formal` or `academic` to change it.

- **Content Genesis sends to other people is auto-cleaned before it goes out** (#654) —
  email, Discord, and the article/post drafts you review now pass through a
  deterministic check that fixes the most common AI giveaway (a spaced em dash,
  `like — this`) and scans for accidentally-included secrets (API keys,
  credentials) before the message leaves Genesis. Messages to *you* (Telegram,
  voice) are left exactly as written.

- **Genesis now watches its own database journal size** (#647, #687) — if SQLite's
  write-ahead log grows abnormally large (the sign of a stuck database reader
  holding the file open), Genesis raises a high/critical alert on Telegram and in
  the morning report, instead of letting it balloon silently for days.

- **The dashboard shows your database journal (WAL) size at a glance** (#687) — the
  Infrastructure health panel now displays the SQLite WAL size next to the
  database probe, colored green / amber / red, so you can spot DB-lock pressure
  building before it ever trips an alert.

### Changed

- **Your morning report now tells you what to do, not just what happened** (#733) — it
  ends with a **Next Steps & Blockers** section that names the few highest-leverage
  actions for the day and what's blocking progress (a stalled follow-up, a pending
  approval, an issue gating one of your goals), drawn only from items already in
  the report. This replaces the vaguer "follow-up suggestions" guidance, so the
  briefing highlights what matters and the action it implies instead of just
  aggregating status.

- **Interactive Claude Code consoles can run friction-free again, when you want
  them to** — the SSH/tmux dev-console slot and the dashboard web terminal still
  default to `--permission-mode auto` (auto-approves common operations, but still
  prompts you on deny/ask rules), but you can now opt a session back into
  `--dangerously-skip-permissions` by setting `GENESIS_CC_PERMISSION_MODE=bypass`.
  For the SSH slot, put that line in `~/.genesis/cc-slot.env` (SSH sessions don't
  read your shell profile); for the dashboard terminal, set it in the dashboard's
  environment. Headless autonomous sessions are unaffected.

- **`update.sh` now keeps Claude Code in sync on your host VM too** — if you run
  Genesis with a Guardian on a separate host VM, updates previously only touched
  the container's Claude Code, letting the host drift behind. `update.sh` now
  checks the host's version against a single pin (`scripts/lib/cc_version.sh`)
  and updates the host to match when it has drifted, so container and host never
  fall out of step. It's skipped when already in sync and never fails your update
  if the host is unreachable.
- **Voice: you now choose exactly which alerts are spoken aloud** (#618) — the
  Voice PE only speaks alerts on an allowlist you control (`voice.alert_ids`
  in `outreach.yaml`) instead of chiming for every blocker, alert, and
  approval. The default set covers what's worth interrupting you for: disk
  and memory emergencies, memory-system failures (embeddings, vector
  search), a stalled awareness loop, Sentinel decisions that need your
  approval, and blocked autonomous tasks. CLI approval prompts and generic
  provider credit-exhaustion no longer chime by default. Everything still
  arrives on Telegram regardless — this only controls what's spoken out loud.
- **Earlier memory and memory-search alerts** (#618) — the container-memory alert
  now fires at 85% (was 90%) and the vector-search-failure alert at 50%
  failure (was 100% only), so you hear about pressure and degradation
  sooner, on both Telegram and voice.
- **Voice runs from its own repo now** — the Voice PE device firmware and the
  voice bridges (the conversational OpenAI Realtime bridge, plus a new
  ambient-listening capture service) have moved to the separate
  [GENesis-Voice](https://github.com/WingedGuardian/GENesis-Voice) repo, which
  documents the full setup. Genesis keeps its internal voice integration; if you
  flash the device or run a voice bridge, get them from GENesis-Voice.

### Fixed

- **Inbox items added soon after an evaluation are no longer silently skipped** (#736) — if you added a
  link or note to an inbox file within the cool-down window just after Genesis had evaluated that
  file, the new item could be marked as seen without ever being evaluated, and it stayed stranded
  until you edited the file again. Genesis now defers those additions and picks them up on the
  next pass once the cool-down clears, so nothing you add gets lost.

- **Re-sharing an article you already added no longer creates a duplicate evaluation** (#736) — links
  often carry per-share tracking parameters (for example, the same LinkedIn post shared from your
  phone vs. your desktop produces different URLs), which used to make a re-paste look brand new.
  Genesis now ignores those tracking parameters when deciding what's new, so the same article
  isn't evaluated twice or spawn a duplicate follow-up.

- **Voice approvals now resolve the action you actually mean** (#731) — when you say "approve" or
  "reject" over voice, Genesis tells you which action it acted on, and if more than one action is
  awaiting your decision it reads the options back and asks which one — instead of silently
  resolving whichever was most recent (which could be the wrong one).

- **Fewer false health alarms about Genesis's own subsystems** (#723, #725, #728, #732) — several background loops report
  health through a periodic heartbeat, and a couple could trip "overdue" or "dark" alarms while
  perfectly healthy. The ego's check-in rode its proactive-thinking timer, which slows during quiet
  periods and gets pushed back by other work, so it could go hours between ticks and trip the 4-hour
  alarm; and the reflection loop only emitted a heartbeat when it actually ran one, so calm overnight
  stretches read as silent. Both now send a steady lightweight "alive" heartbeat independent of their
  work pace. Separately, weekly "quality drift" and "learning regression" warnings no longer linger:
  each weekly check supersedes and clears the previous flag once the metric recovers, the regression
  alarm only fires on a sustained drop rather than a noisy wobble, and anything older than three days
  is demoted and tagged historical instead of repeated as a fresh alarm.

- **Genesis's self-quality metrics are now accurate** (#708, #724) — several bugs were skewing the numbers Genesis
  uses to grade its own competence (the J9 readiness grades, the morning-report quality figures, and
  the gate that decides which self-improvements ship). Memory retrieval quality (MRR) was computed
  against database arrival order instead of the actual retrieval rank; each memory search logged its
  internal "recall" event twice, inflating the counts; reflection quality was scored as a running
  total over the newest reflections, so it drifted downward purely because new reflections hadn't been
  referenced yet (a "quality crater" that was a measurement artifact); and a malformed LLM-judge
  verdict was silently recorded as a confident "0 / fail" instead of an error. All now reflect
  reality — reflection quality is measured over a fair, fixed age window and reports "insufficient
  data" when there aren't enough mature reflections, and the readiness grades and ship-gate are
  trustworthy. Genesis also now tracks whether its ranking merely favors the memories it retrieves
  most often, so entrenchment can be watched over time.

- **Knowledge-base searches stopped silently returning nothing** (#721) — the relevance floor that
  trims low-quality knowledge results was a fixed absolute cutoff that, on the score scale recall
  actually produces, sat above the entire range — so searching the knowledge base (or a broad
  memory search across everything) could return *zero* knowledge results even when directly
  relevant ingested documents existed. The floor is now relative to the best-matching result, so
  the strongest knowledge hit always survives and a proportional tail of weaker matches is kept,
  regardless of the underlying score scale.

- **Memory search got more precise on multi-word queries** (#721) — query expansion (which broadens a
  search with related terms) could pull in documents that matched only a broad category tag —
  the structural labels like class/wing/life-domain that Genesis attaches to *every* memory — so
  an off-topic document could outrank genuinely relevant ones. Those ever-present structural tags
  are now excluded from expansion, and for multi-word queries the related terms only *boost*
  documents that already match part of your query rather than surfacing on their own.

- **Genesis no longer loses contradicting or superseding links between memories** (#719) — its memory
  graph could only hold one relationship between any two memories, so recording a second kind
  (for example marking a pair as "contradicts" when they were already linked as "supports", or
  "succeeded_by" when one memory replaces another) was silently dropped. Different relationship
  types between the same two memories are now all kept, so Genesis reasons over a fuller, more
  honest picture of how its memories relate.

- **Procedure learning survives a two-provider outage** (#710) — the routine that captures reusable
  procedures from Genesis's own struggles ran on only two free model providers; when both were
  down at once it exhausted its chain and silently stopped learning. A third independent free
  fallback now keeps it working through overlapping provider outages.

- **Star-count updates no longer crowd high-priority alerts** (#714) — GitHub star-count reconnaissance
  pings inherited their watched project's priority (e.g. "high" for the main repo), so vanity
  "+N stars" deltas competed with genuinely important findings in the morning report and alert
  lane. They're now recorded at low priority — still tracked for trend deltas, just no longer
  treated as urgent.

- **Genesis's at-a-glance state views stopped showing internal noise** (#712) — three cleanups to the
  dashboard and to Genesis's own always-on context: empty sessions (ended before any messages were
  exchanged) no longer appear as ghost "0 msgs" rows in the recent-sessions list; the "Active Work"
  summary no longer ingests raw harness notifications (task-completion blobs, system reminders,
  slash-command metadata) as if they were your prompts; and the memory "Wings" breakdown shows only
  real, controlled-vocabulary domains instead of malformed or one-off tags. What you — and Genesis —
  see reflects genuine activity, not plumbing.

- **A campaign that crashes mid-tick no longer fails silently** (#706) — when a scheduled campaign
  tick raises an error, Genesis now records it in job-health tracking, so the failure surfaces
  in the dashboard and to the ego instead of vanishing into the server log. Campaign
  reliability problems become visible instead of going unnoticed.

- **Surplus brainstorm messages read like prose, not raw JSON** (#707) — Genesis's background
  brainstorm ideas posted to the Telegram "Surplus" topic now render as clean bulleted text
  (idea, detail, and why it matters) instead of the raw ```json``` code block the model
  produces. Plain-text and non-JSON messages are unaffected.
- **The neural monitor labels every cognitive call site correctly.** (#702) Eight call sites
  that previously showed blank (the eval judge, voice conversation, session observer,
  task pre-mortem, intelligence intake, both resume-review passes, and the executor's
  failure-exit gate) now display their purpose, category, and cost. Sites that actually
  run on the Claude Code subscription (the ego cycle and the deep/strategic/weekly/quality
  reflections) now read "CC background" with their CC model shown in the chain, instead of
  being mislabeled as a paid API cost.

- **Updates now reliably load the new code** (#700) — an update could finish "successfully"
  while the running Genesis process kept executing the *old* code: when the updater
  stopped the server, systemd's auto-restart could bring it back on the pre-update
  code before the new code was even pulled, and the updater's final restart was a
  no-op on the already-running process. The database migrated but the live process
  didn't, leaving new code on disk and old code in memory. The updater now forces a
  true restart at the end and makes sure the server stays down during the upgrade, so
  an update always activates the version it just installed.
- **Spend on GLM, MiniMax and other aggregator models is now reported accurately** (#701) — these
  providers' usage was silently recorded as $0 because their model names aren't in the cost
  library Genesis relies on, hiding real spend in cost reports. Genesis now falls back to each
  model's configured price when the library can't price it, so spend reflects what you're
  actually using. (Visibility only — it never throttles or blocks calls.)

- **Queued retries survive a routing-config change** (#701) — every time the provider routing config
  was reloaded (e.g. toggling a provider in the dashboard), Genesis was expiring *all* of the
  queued "retry the whole chain" requests before its scheduled retry job could replay them.
  Those items now persist across a config reload and get retried as intended.
- **A rate-limited or over-budget request no longer knocks a working provider offline** (#703) —
  when a provider replied "too many requests" (429) or rejected a single request as too large
  or against policy (400/422), Genesis treated it like an outage: it retried the doomed request
  several times and tripped that provider's circuit breaker, taking it out of rotation for
  everything else for up to 30 minutes. Now those responses fail straight over to the next
  provider without retrying or benching the one that's actually healthy — so you get faster
  failover and far fewer false "provider down" blips.
- **Idle fallback providers heal on their own instead of staying stuck** (#705) — a provider that
  recovered from an outage but then received little or no traffic could sit in a half-recovered
  "on probation" state indefinitely, because only a real successful request could fully clear it.
  Genesis's free health probes now confirm such a provider is reachable and restore it to normal
  rotation (and clear its lingering "failing" alert), so rarely-used backups don't get permanently
  benched.

- **A single request can't hang for minutes across retries and failover** (#705) — each routing profile
  now has an aggregate time budget, so the worst case where one request's retries multiply across
  the whole provider chain into a multi-minute stall is bounded. It only caps the retry/failover
  multiplier on one request (checked between attempts, never mid-call) — background thinking that
  legitimately takes a while is unaffected.

- **Recovered providers stop alarming once they come back** (#698) — when a model provider's
  circuit breaker reopens after an outage, Genesis now clears that provider's "failing"
  alert instead of leaving it lingering for days until it expired. Per-session conversation
  telemetry no longer floods the Observations panel either, so the panel reflects current
  state rather than a backlog of stale entries.

- **Cost reporting now shows your real spend, not a phantom figure** (#694) — the health
  tool that Genesis's reflections consult was reporting a *notional* "if Claude Code
  were billed by the API" number (hundreds of dollars a month) as if it were actual
  cost, with no budget context. That phantom figure drove false "cost is accelerating"
  alerts in reflections and the morning report. Genesis now reports true spend from
  recorded cost events against your configured budget. The morning report shows a
  single grounded line — month-to-date spend versus your cap — with no projections or
  spike alarms, and reflections no longer analyze cost at all.

- **No more false "CRITICAL / degraded" alarm when a paid provider runs out of
  credits** — health now judges degradation by whether your *essential* work is
  covered, not by how many paid providers are down. If OpenRouter (or any paid
  provider) goes down but your free providers still cover the essentials,
  Genesis stays NORMAL instead of flashing a system-wide CRITICAL. The alarm
  now fires only when an essential capability genuinely has no working provider.

- **Clearer API-key colors on the dashboard** (#698) — the API Keys panel now shows
  🟡 yellow for a key that's missing/unconfigured, 🔴 red for a key that's set
  but not working (circuit breaker open, including out-of-credits), and 🟢 green
  for working. A paid provider that's down now shows up red on the API-keys card
  (e.g. "openrouter — out of credits") without raising a system-wide alarm.
- **Approving a light reflection's Claude Code fallback now actually runs it** (#693) —
  when all of light reflection's free model providers were down at once, Genesis
  would ask you to approve a Claude Code fallback, but approving it did nothing:
  the reflection was never resumed (only deep and strategic reflections were).
  Light reflections are now resumed on approval like the others, and a deferred
  reflection is logged instead of silently dropped.

- **Genesis can now detect replies to the emails it sends** (#689) — outbound email
  was going out without a real Message-ID header, so mail clients couldn't thread
  it and Genesis couldn't match incoming replies back to the original message.
  Outbound mail now carries a proper Message-ID, so replies are recognized and
  routed to the right conversation.

- **Background work deferred during an outage is no longer silently dropped** (#689) —
  when the system was degraded, the recovery pass marked queued reflection and
  outreach work "done" without ever running it, and a stuck outreach item could
  block reflection retries entirely. Deferred work is now kept until it actually
  runs, reflections are no longer blocked behind it, and recovery holds off
  re-trying until the system is genuinely stable.

- **The skill auto-tuner can no longer truncate a large skill** (#687) — Genesis's
  weekly skill-refinement pass reviewed long skill files from a clipped
  3,000-character view and could auto-apply a much shorter rewrite, silently
  dropping most of the content. It now reviews the full skill, and any
  auto-applied edit that would shrink a skill below half its size is held for
  review instead of overwriting the file.

- **The dashboard's degraded-mode banner no longer overflows** (#687) — a long
  "providers down" summary now wraps instead of spilling past the edge on
  narrow windows.

- **Telegram approval buttons work again** (#686) — tapping the inline **Approve** / **Approve all**
  buttons (and any inline-keyboard button) had silently stopped doing anything for several days.
  Telegram was dropping every button press before Genesis received it, because the Guardian's
  recovery-approval check had narrowed the bot's update filter to text messages only. Genesis now
  always requests Telegram's default update set (which includes button presses) — and the Guardian
  check no longer narrows it — so button presses are delivered and resolve immediately again.

- **Off-site backups can now actually be restored** (#673) — the large data (the
  database, vector memory, and transcripts) is stored only on your off-site
  (NAS) target, but the restore tool had no way to fetch it — so a from-scratch
  recovery silently couldn't bring back your database or memory. Restore now
  pulls the latest off-site snapshot before restoring, and backups are written
  as dated point-in-time snapshots (so you can recover a *specific* run, not just
  the last one) with transcripts included off-site too.

- **A backup that can't reach off-site storage no longer fails silently** (#672) — if
  you've configured an off-site (NAS) backup target and a run captures your data
  locally but can't replicate it off-site, Genesis now sends a distinct alert
  ("off-site replication failed — local backup OK") and records `offsite_confirmed`
  in the backup status. The backup still counts as successful (your local copy is
  intact); only the off-site replica is flagged as missing. Local-only setups (no
  off-site target) are unaffected.

- **Restoring a backup is now safe against corruption** (#671) — `restore.sh` now stops
  the running Genesis server before swapping the SQLite database (so a live
  connection can't corrupt the restore), clears stale write-ahead-log sidecars
  that would otherwise replay onto and corrupt the restored DB, and runs an
  integrity check on the result — warning loudly if it's not sound. It
  deliberately leaves the server stopped afterward so you can verify the restore
  before bringing Genesis back up.

- **The host Guardian's self-update no longer throws away your local config on a
  conflict** — when it pulls new code and a local setting (e.g. the container IP)
  clashes with an upstream change to the same lines, the update used to silently
  discard those local changes. It now preserves them in a recoverable git stash
  (and tells you they're recoverable) instead of dropping them. The update also
  reports its result reliably, so a successful update that pulled changes is no
  longer misread as a failure.

- **Guardian host updates no longer silently stall** (#670) — on hosts where the
  Guardian's `CLAUDE.md` had been pinned with git's skip-worktree flag, the
  Guardian's self-update (`git pull`) would abort the moment that file changed
  upstream, quietly leaving the host Guardian stuck on old code. The update now
  clears the flag first, so existing installs self-heal and stay current.

- **Guardian host self-updates are now reliable on hosts with passwordless sudo** (#669)
  — an unguarded step while refreshing kernel tuning could make the Guardian's
  self-update abort partway, so it reported a failure (and could leave its own
  updater script frozen on old code) even though the code pull had already
  succeeded. Kernel tuning is now strictly best-effort and can't derail the
  update; the update reliably refreshes the updater first and records what it
  deployed; and there's a new one-step recovery path to refresh a stalled updater.

- **Telegram `/stop` now stops your session, not a background task** (#656) — when a
  background task (reflection, inbox, an ego session, etc.) was running at the
  same time as your chat, `/stop` could interrupt the wrong one. Each session's
  Claude Code subprocess is now tracked separately, so `/stop` always targets
  the generation in your conversation.

- **The Guardian alerts once when Genesis goes down — and once when it's back** (#655) —
  previously, if Genesis went down and its diagnosis couldn't reach Claude Code,
  the host Guardian re-ran a full investigation and re-sent a critical Telegram
  alert every 30 seconds until recovery — an alert storm. It now sends a single
  "down" alert per outage (no repeats, however long it lasts), and when Genesis
  comes back on its own it sends a single "restored" notification — which it
  never did before.

- **"database is locked" errors under load are largely gone** (#634, #647) — several independent paths could pin
  the database or fail on a transient lock. A cancelled read could leave a stale lock while the
  write-ahead log ballooned (it reached ~2 GB); a long-lived MCP connection left read transactions
  open after read-only calls, pinning the WAL and making `memory_store` / `reference_store` fail until
  a restart; and several standalone connections (the ego tools, web-agent cost tracking, the
  contribution gate, and two hooks) opened with no wait-for-lock timeout, so brief contention failed
  immediately. Reads are now cancellation-safe with a size-bounded journal, MCP calls release their
  snapshot at each boundary and are serialized, and the standalone connections share one WAL-aware
  helper with a bounded wait — so writes ride out transient contention and the WAL stays bounded.

- **Claude Code hooks work from a git worktree again** — the hook launcher
  located the Python venv via a `git worktree list | head` pipeline that, with
  many worktrees, died on SIGPIPE under `set -o pipefail` and **silently
  disabled every hook** (session activity capture, file/edit audit logging) when
  you ran Claude Code from a worktree. It now resolves the main repo with
  `git rev-parse --git-common-dir` (no pipe), so hooks fire reliably everywhere.
- **Outreach emails actually send now** (#637) — email (and Discord/voice) outreach was
  being misaddressed to the Telegram forum chat for any category that routes to
  the supergroup, so every such send failed and silently piled up as retries.
  Forum/topic routing is now correctly Telegram-only; other channels deliver to
  their own recipient.
- **A slow or failed email can no longer stall Genesis** (#637) — SMTP sending now runs
  off the event loop, so a hung or rejected send no longer freezes heartbeats,
  health checks, or the awareness loop.
- **Provider hangs no longer stall reflections and the dream cycle** (#627) — when a
  model provider hangs (accepts the connection but never responds), Genesis
  now fails over to the next provider within its timeout instead of blocking
  for minutes. Reflections and the nightly dream cycle stop piling up
  dead-lettered work during provider outages, and adversarial review and
  reflections keep running when free-tier providers are down (extra free
  fallbacks added, plus a paid last-resort for the dream-cycle challenge).
- **The weekly memory consolidation (dream cycle) no longer melts down during a
  provider outage.** Previously, if its LLM providers were unavailable, the run
  attempted every cluster anyway — burning hours and flooding the retry queue
  while merging almost nothing. It now aborts early once the providers are
  clearly saturated and defers the rest to the next run, and no longer
  dead-letters its own consolidation attempts.
- **Job health no longer shows a permanent failure after a job recovers.** (#624)
  A scheduled job that failed once kept that failure timestamp in the health
  view forever, even after it started succeeding again; recovery now clears
  the stale failure and error so job health reflects reality.
- **Circuit-breaker trips now survive a restart.** (#626) A provider that tripped
  open was silently coming back available on every restart (a saved-state
  casing mismatch), so a failing provider got retried immediately instead of
  serving out its backoff. Breaker state is now also written atomically, and
  MCP helper processes no longer overwrite the shared state file.
- **The error log no longer silently under-counts during incident storms.** (#631)
  When the event-persistence queue filled up, events were dropped without a
  trace — so the dashboard and health views under-reported errors exactly when
  things were worst. Dropped events are now counted and made visible (an
  "event queue overflow" warning in the same error views, plus a live counter
  on the health snapshot), the buffer is 10× larger (500 → 5000) to absorb
  bursts, and a single un-serializable event can no longer drop a whole batch.
- **Dashboard settings changes now actually take effect.** (#632) Overrides you saved
  from the dashboard (or the settings tool) are written to `~/.genesis/config/`,
  but several subsystems (inbox, surplus, resilience, voice/TTS, perception
  confidence, and more) still read their `.local.yaml` overlay from the repo's
  `config/` dir — so your changes were silently ignored, even after a restart.
  Loaders now read the user-config overlay first (falling back to the repo path
  for older installs), and the settings tool reports the correct saved path.

### Security

- **Hardened remote Claude Code dispatch against shell injection.** (#625) The SSH
  module adapter now shell-quotes the model, effort, and path values it sends
  to a remote host, so a crafted value can no longer run arbitrary commands
  there. Normal dispatch is unchanged.
- **Documented the dashboard's network-exposure model.** (#646) `SECURITY.md` now
  spells out that the dashboard binds all interfaces for proxy/overlay reach
  and that its `/api`, `/v1`, web terminal, and noVNC console are
  unauthenticated administrative access — so operators know to keep those ports
  on a private overlay (e.g., Tailscale) or behind a reverse proxy and never
  expose them publicly.
- **Interactive Claude Code consoles no longer skip all permission checks.** (#630, #646)
  The dashboard web terminal and the SSH dev-console slot now launch Claude
  Code in auto-permission mode instead of `--dangerously-skip-permissions`:
  common operations still run without prompting, but risky ones ask for your
  approval right there in the session (you're present to answer). Headless,
  autonomous sessions are unchanged — they have no one to answer a prompt.

- **Pinned secure floors for bundled dependencies.** `urllib3`, `idna`, and
  `certifi` now carry minimum-version floors so a fresh or cached install can't
  resolve to a version with a known CVE (dependency audit #638). No behavior
  change — existing installs already satisfy the floors.

---

## [v3.0b15] - 2026-06-12

### Added

- **Campaign subsystem** (#549, #556, #559, #600) — Genesis can now run
  autonomous outreach campaigns end to end: scheduled multi-step sequences
  with a Discord webhook adapter and Discord voice pipeline, per-campaign
  profiles, and category validation. Campaigns respect your timezone and
  dedupe so the same target isn't contacted twice.
- **Voice: wake word and proactive speech** (#569, #570, #581) — say
  "hey genesis" to start a conversation hands-free. Genesis can chime
  proactively to get your attention, pre-announce before speaking, and ask
  for approvals out loud with a spoken yes/no.
- **Voice: tool use in conversation** (#580, #590) — the speech-to-speech
  bridge can call Genesis tools mid-conversation, so spoken requests trigger
  real actions instead of just talk.
- **Email thread tracking + autonomous replies** (#565) — Genesis follows
  email conversations as threads and can draft and send replies on its own,
  with weekly-job resilience so long-running threads aren't dropped.
- **Procedural learning** (#591) — Genesis extracts reusable procedures
  from your sessions through a three-stream pipeline, so repeated workflows
  become things it knows how to do rather than re-derives each time.
- **Memory immune system + self-correcting facts** (#545, #552) — memory
  defends against bad or contradictory writes with adversarial review, and a
  supersession chain automatically replaces stale facts with newer ones so
  recall reflects what's currently true.
- **Inbox follow-ups and digests** (#544, #547) — inbox evaluations produce
  structured recommendations, can create tracked follow-ups, and surface a
  digest, with a dashboard filter to focus the queue.
- **Discord polls + morning-report anti-drift** (#560, #562) — Discord
  outreach supports polls, and the morning report carries an anti-drift
  signal to keep autonomous activity aligned with your priorities.

### Fixed

- **Dropping a folder onto the dashboard uploader hung forever** — folder
  drops now upload every file inside, preserving the folder structure under
  the uploads directory. Single-file and multi-file uploads are unchanged.
- **Voice conversations fell a turn behind or got stuck** (#579, #596, #602)
  — fixed a turn-behind bug, stale-session recovery, and the audio path
  after the pipecat 1.3.0 upgrade. Turn-taking is sharper and background
  noise is reduced.
- **Eval quality dashboard could stall** — the nightly memory-scoring job
  now resumes where it left off, scores in parallel within provider rate
  limits, and ignores duplicate judgments, so the compounding-intelligence
  metrics stay accurate and update reliably.
- **Dashboard white flash and file-browser glitches** (#575, #592) — fixed
  a white flash on load, a too-short file browser, post-upload UX, and
  multi-file upload.
- **Scheduled jobs could fire at the wrong time or not at all** (#548, #550,
  #557) — weekly jobs are spread across the week, all jobs use your
  timezone, and interval jobs were converted to cron so they survive restarts
  instead of silently never running.
- **Watchdog falsely reported failure after a slow restart** — it now
  confirms the service is actually back up before reporting, so a successful
  recovery no longer shows as a failed health check.
- **Disk could fill from runaway logs** (#537) — the systemd journal is
  capped at 200MB to prevent disk bloat.
- **Terminal scrollback dropped chunks of output in tmux** — the Claude
  Code pin is now 2.1.173 and the forced-classic-renderer override was
  removed from project settings, so sessions can use the fullscreen renderer
  (`/tui fullscreen`), which keeps the complete conversation scrollable
  in-app and exportable to tmux with `Ctrl+O` then `[`.
- **UI icons rendered as underscores when connecting from Windows** — the
  tmux session launcher now forces a UTF-8 locale and passes `-u`, so the
  Claude Code logo, checkmarks, and prompt glyphs render correctly. Reconnect
  (detach + re-SSH) for the fix to take effect.
- **Install: npm prefix auto-detection** (#606) — the installer detects
  your npm prefix instead of hardcoding `/usr/local`, so setup works across
  more environments.

### Security

- **Removed pickle from the embedding cache** (#536) — the on-disk
  embedding cache now uses JSON instead of pickle, closing a code-execution
  risk from untrusted cache files (CVE-2025-69872).
- **Cleared dependency vulnerabilities in the voice bridge** (#597) ---
  updated the voice bridge lockfile, resolving 44 of 46 flagged dependency
  advisories.

---

## [v3.0b14] - 2026-06-04

### Added

- **Voice S2S pipeline** (#524, #525, #530, #532, #535) — speech-to-
  speech voice conversations via Wyoming protocol and GPT-Realtime API.
  Includes conciseness nudge, audio output fix, and 30-minute idle
  timeout.
- **Ego notification pipeline** (#531) — proposals deliver through
  outreach with dedup, rate limiting, and quiet hours. Content firewall
  prevents information leakage in dispatched content sessions.
- **Ego domain separation** (#529) — user ego and genesis ego operate
  with distinct information boundaries. Domain-aware realist catches
  cross-domain proposals before delivery.
- **Verified autonomy** (#521, #522) — ECE calibration metric and
  quality scorer for autonomous execution. Adversarial review layer
  validates dispatch outcomes.
- **Dispatch gate** (#516) — ego proposals route through the autonomy
  approval pipeline before execution.
- **Life domain model** (#539) — memory system supports life domain
  tags (employment, personal, genesis). User profile structured around
  life dimensions.
- **Essential knowledge: active work** (#509) — real-time active work
  section in the ego's essential context window.
- **Dashboard contribution toggle** (#533) — contribution offers can
  be enabled or disabled from the dashboard Settings tab.
- **Hook pipeline wiring** (#518) — outcome verification, skill
  injection, and feedback audit connected to the hook system.

### Fixed

- **Ego reactive spinning** (#538) — reactive signal threshold raised
  from WARNING to ERROR, eliminating thousands of noise-driven ego
  cycles per week. Infrastructure escalations filtered from user ego
  context. Dispatch verification fuzzy-matches similar filenames
  instead of false-failing.
- **Contribution hook in worktrees** (#538) — config gate resolves
  from the main repo root instead of the worktree path.
- **Dream cycle OOM** (#517, #528) — entity resolution OOM guard
  prevents unbounded Qdrant searches. Chunked dedup handles large
  memory buckets without memory exhaustion.
- **DeepSeek V4 cost tracking** (#526) — custom cost entries for
  models not in litellm's registry. Dream cycle hardening for edge
  cases.

---

## [v3.0b13] - 2026-06-01

### Added

- **Provider failure escalation** (#512) — circuit breaker trips that
  cycle 5+ times without recovery now auto-create high-priority
  observations. The ego picks them up naturally instead of relying on
  manual investigation. Recovery clears the escalation state.
- **Investigation model override** (#512) — ego proposal dispatches
  respect per-action-type model configuration (`dispatch_model_overrides`
  in ego.yaml). Investigations default to Opus for deeper reasoning.
- **Dashboard circuit breaker visibility** (#512) — LLM provider cards
  in operational vitals show breaker state (OPEN/HALF-OPEN badges),
  trip count, and last failure category.
- **Goal decomposition** (#501) — ego goals support subgoals,
  cascade tracking, goal_type, and cadence scheduling.
- **Goal-driven behavior** (#494) — staleness signals, deep context
  injection, and assessment for ego goal management.
- **Reflection corpus recording** (#503) — captures deep reflection
  observations for quality measurement and prompt optimization.
- **Content validation hooks** (#500) — gitleaks rules and commit-msg
  hook for automated content validation.

### Fixed

- **Surplus restart flooding** (#504) — restart-resilient scheduling
  with completed_at cooldown prevents re-enqueuing on server restart.
  Watchdog heartbeat refresh prevents false staleness detection.
- **Morning report staleness** (#506) — inbox count from DB instead
  of filesystem, observation surfacing lifecycle respected, standing
  items use proper datetime comparison.
- **Ego domain boundaries** (#506) — user ego no longer sees
  infrastructure observations that belong to the Genesis ego's domain.
- **Light reflection duplicates** (#502, #505) — eliminated duplicate
  observations and injected prior context for continuity.
- **Routing config cleanup** (#510) — removed stale model entries
  from routing chains after upstream model availability changes.
- **Bookmark search** (#511) — bookmarks searched via SQL instead
  of memory retriever for reliability.

### Changed

- **Realist gate tightened** (#504) — bypass threshold raised from
  high to critical-only priority.
- **CODEOWNERS + PR discussion** (#508) — require discussion
  before PRs on the public repo.

---

## [v3.0b12.1] - 2026-05-30

### Added

- **Reflection quality rubric** (#493) — LLM-as-judge rubric scoring
  deep reflection observations on specificity, actionability, novelty,
  and grounding. Foundation for DSPy prompt optimization. Calibrated at
  98% agreement on 50 hand-graded cases.
- **Golden set generator** — one-shot script to bootstrap rubric
  calibration data from existing deep reflection observations.
- **Standalone calibration runner** — validates the rubric outside the
  full Genesis runtime using a lightweight litellm router wrapper.
- **Voyage AI reranking** (#489) — post-retrieval reranking via
  Voyage rerank-2.5 for memory recall precision.
- **Memory recall defaults** (#492) — rerank enabled by default with
  opt-out for latency-sensitive callers.
- **Ego realist upgrade** (#491) — realist gate now uses Opus with
  domain boundary enforcement.
- **User job timezone fix** (#490) — scheduler respects user timezone
  for job scheduling.
- **Ego CycleType removal** (#486) — legacy run_cycle() enum removed.
- **PageIndex document indexing** (#487) — tree-based vectorless RAG
  for structured PDFs via PageIndex cloud API.
- **Dream cycle entity resolution** (#483) — graph enrichment during
  dream cycles with entity resolution and relationship extraction.

### Fixed

- **CodeQL security alerts** (#485) — resolved all 8 open alerts.

---

## [v3.0b11] - 2026-05-23

48 PRs merged. Proposal lifecycle redesigned — ego's focus board
decoupled from user approval queue. Ego reliability hardened across
resolution UX, realist gate, and sovereignty guards. Reflection goes
event-driven. Dashboard gains observations tab and eval metrics.

### Added

- **Board/queue separation** (#412) — ego's 0-3 focus board is now
  independent of the user's pending approval queue. New `unboard` action
  rotates ego focus without destroying user approvals. 14-day auto-table
  for stale proposals.
- **Unified proposal resolution** (#411) — natural language approval
  ("ok", "yes", "sounds good") recognized across Telegram, MCP tool,
  and CC sessions. Re-validate path for withdrawn proposals.
- **User directives** (#399) — direct instructions to the ego with
  rich goal context and MCP tools for goal/directive management.
- **Goal-proposal linking** (#390, #403) — proposals advance specific
  user goals with progress tracking and Opus-quality dispatch.
- **Critical observation alerting** (#369, #370) — automatic Telegram
  alerts for critical observations with delivery gating and dedup.
- **Observations tab** (#367) — browse and resolve observations on
  the dashboard.
- **J-9 eval metrics** (#378, #383) — evaluation dimensions and
  meta-health heartbeat surfaced on dashboard.
- **Workflow visibility** (#380) — phase timeline with linked
  follow-ups on dashboard.
- **TinyFish Browser API** (#388) — Layer 4 CDP option for browser
  automation.
- **Model assessment framework** (#372) — activated scheduler for
  provider evaluation.
- **Weekly models.md synthesis** (#410) — recon pipeline auto-generates
  model intelligence report.

### Changed

- **Event-driven reflection** (#408) — anomaly focus with delta-only
  Light prompts. No re-reporting known conditions.
- **Silent micro ticks** (#404) — perception runs without LLM unless
  critical signal detected.
- **Jurisdiction separation** (#387) — user ego and genesis ego
  operate in distinct domains.
- **Update polling** — state-based silent-death detection replaces
  wall-clock timeout with 30-second startup grace period.

### Fixed

- **24h sovereignty guard** (#411, #412) — tabling and withdrawal
  blocked for proposals delivered less than 24 hours ago.
- **Realist confabulation** (#412) — realist gate can no longer
  fabricate system state claims from failure patterns in history.
- **Observation TTL tuning** (#407) — infrastructure types expire
  faster to prevent stale belief errors.
- **Ego outcome visibility** (#391) — clear FAILED/OK outcomes in
  proposal history context.
- **Reactive event dedup** (#397) — content-dedup prevents 30-minute
  spam from repeated signals.
- **Guardian depth checks** (#377) — health API probes with 503 retry.
- **Sentinel stale heartbeat** (#376) — detects and reports on
  dashboard.
- **Inbox re-evaluation** (#402) — previously evaluated items no
  longer re-processed.
- **Dashboard UX** (#364, #394, #396) — approvals, ego badges, work
  tab, timezone handling, silent-death detection.

---

## [v3.0b10] - 2026-05-15

5 PRs merged. Dream cycle adds retroactive memory consolidation. Ego
proposals show attribution (User vs Genesis). Promoted surplus insights
now feed into deep reflection instead of dead-ending. Guardian hardened
against VM crashes.

### Added

- **Dream cycle** (#359) — retroactive episodic memory consolidation.
  Background process reviews recent memories, identifies clusters and
  patterns, and synthesizes higher-order observations.
- **Ego attribution** (#362) — proposals display which ego (User CEO
  vs Genesis COO) created them, on both Telegram digests and the
  dashboard.
- **Surplus→reflection pipeline** (#362) — promoted surplus insights
  feed into deep reflection context. After routing, insights are marked
  consumed so they don't re-appear.

### Fixed

- **Ego dispatch pipeline** (#358) — timeout handling, double-dispatch
  prevention, and message persistence for proposal execution.
- **Dream cycle safety** (#363) — bucket chunking, memory preflight
  validation, and async yielding to prevent runaway consolidation.
- **Guardian VM crash hardening** (#361) — kernel OOM tuning, MCP
  process isolation, and preflight health checks before diagnosis.

---

## [v3.0b9] - 2026-05-14

54 PRs merged. Ego gains layers 3--6 (realist gate, cross-ego isolation,
capability map, reactive cycles, model tiering). The surplus engine gets
an intelligence intake pipeline. Approval system rebuilt. Memory and
learning subsystems hardened across a dozen fixes. Browser automation
gains stealth and VNC-based Turnstile bypass. CC's Bash sandbox moved
off volatile `/tmp` to prevent intermittent session-breaking failures.

### Added

- **Ego layers 3--6** (#333, #335, #346) — realist gate for proposal
  quality control, cross-ego isolation (user ego and Genesis ego run
  independently), capability map for self-awareness, reactive cycles
  that respond to environmental changes, and model tiering for
  cost-appropriate execution.
- **Intelligence intake pipeline** (#349) — surplus engine atomizes
  incoming intelligence signals, scores them for relevance, and routes
  to the appropriate processing lane.
- **Stealth browser skill** (#338) — VNC trusted-input technique for
  bypassing anti-bot protections like Cloudflare Turnstile.
- **VNC Turnstile auto-bypass** (#348) — wires VNC trusted input into
  the browser automation layer for hands-free CAPTCHA solving.
- **Medium self-healing login** (#342) — `MediumDistributor` recovers
  from expired sessions without manual intervention.
- **Ego Opus dispatch** (#347) — interact-profile sessions use Opus
  for higher-quality output.
- **Voice-master quick mode** (#341) — lightweight voice application
  with anti-AI audit rules.
- **Ego publish profile type** (#339) — adds `publish` to the interact
  profile types for content distribution dispatch.
- **Evolution proposal review tool** (#316) — MCP tool for triaging
  ego proposals.
- **DB migration auto-apply at startup** (#302) — pending migrations
  run automatically on server start.
- **Memory lifecycle GC** (#352) — garbage collection for
  `pending_embeddings`, events rotation, and `retrieved_count` tracking.

### Changed

- **Automated-subsystem memory writes no longer get embedded into
  Qdrant.** Ego corrections, triage signals, and reflection
  observations now land in SQLite (`memory_metadata` + FTS5) only.
  They were already filtered out of foreground recall by default
  (see prior changelog entry); the only paths that surface them are
  explicit opt-ins (`only_subsystem=...` or `include_subsystem=...`),
  and those work via FTS5 keyword search — no Qdrant vector index
  needed. This avoids paying ongoing embedding + storage cost for
  capability that has no live consumer. New writes never touch
  Qdrant; the included one-off script
  `scripts/cleanup_subsystem_qdrant.py` (dry-run by default) cleans
  legacy points from existing installs.

### Fixed

- **`memory_metadata.invalid_at` is now actually honored at recall
  time.** The bitemporal "fact stopped being true at X" column was
  schema-only since the v3.0a bitemporal migration — writes were
  possible (`invalidate_memory()`) but recall never read the value.
  Recall now always filters `invalid_at IS NULL OR invalid_at > now()`
  across FTS5, Qdrant, and drift paths. Rows past their expiry no
  longer surface. Backwards-compatible: every legacy row has NULL
  `invalid_at`, which passes the filter unchanged.
- **Observation TTL now applies to the dual-write memory copy.**
  `ObservationWriter` propagates each observation's `expires_at` as
  `invalid_at` on the linked `memory_metadata` row. Previously the
  observation expired from the `observations` table (via the
  scheduled `resolve_expired` sweep) but its embedded MemoryStore
  copy persisted forever — silently leaking expired content into
  recall under the few code paths that bypassed default filtering.

### Added

- **Procedure auto-extraction now fires on SUCCESS outcomes from
  autonomous channels.** Previously the triage pipeline only extracted
  procedures from `APPROACH_FAILURE` and `WORKAROUND_SUCCESS` outcomes,
  so the procedural_memory table grew only from rare failure patterns.
  Successful autonomous task completions (`inbox`, `mail`, `reflection`,
  `surplus` channels) now also drive extraction, giving the system a
  baseline pattern for the next run of the same task type. Foreground
  SUCCESS is intentionally NOT auto-extracted — foreground procedures
  are user-initiated via the `procedure_store` MCP.
- **Procedure novelty gate.** Auto-extracted procedures are now
  compared against existing procedures of the same `task_type` via
  cosine similarity of their `principle` embeddings. If the new
  principle is ≥0.85 similar to an existing one, storage is skipped.
  Prevents the table from filling with paraphrases of the same insight
  as SUCCESS-path extraction broadens the trigger surface. Fail-open
  when the embedding stack is unavailable.
- **Proactive procedure recall hook.** Procedures now surface
  automatically on every CC prompt — same UserPromptSubmit pathway as
  the proactive memory hook. The hook reuses the prompt embedding the
  memory hook already computes, compares it against `principle_embedding`
  BLOBs stored on each procedure row, and emits a single
  `[Procedure | task_type | id:xxx]` line when the top match's cosine
  ≥ 0.7. Top-1 only — most prompts won't surface a procedure. New
  `principle_embedding` column on `procedural_memory` (forward-only;
  pre-existing rows store NULL and are skipped until they're
  re-extracted or re-taught). Effectively replaces the manual
  `procedure_recall`-before-multi-step-tasks reminder in CLAUDE.md.
- **Four user-work wings added to the memory taxonomy:** `dev_workflow`,
  `research`, `integrations`, and `career`. Previously the taxonomy only
  modelled Genesis-internal subsystems (memory, learning, routing,
  infrastructure, channels, autonomy), so all user-domain memories
  (git/PR/CI activity, paper reading, third-party API integrations,
  career and job-search work) collapsed into `general/uncategorized`.
  New keyword and tag rules route the obvious cases; the long tail
  will still land in `general` until reclassified. The Genesis-internal
  `provider` tag still routes to `routing`, not `integrations` —
  user-work integrations come in via specific service names (minimax,
  abacus, litellm, etc.).
- **Foreground recall excludes automated-subsystem content by
  default.** Memory writes from ego corrections, triage signals, and
  reflection observations are now tagged with a new
  `source_subsystem` column. By default, `memory_recall` MCP, the
  internal `HybridRetriever.recall()`, drift recall, and the
  UserPromptSubmit proactive-memory hook all filter these rows out
  so they don't pollute user-facing answers with the system's own
  decisional commentary. Two new opt-in parameters expose the tagged
  content: `include_subsystem` augments the default set
  (`include_subsystem=True` returns everything;
  `include_subsystem=["ego"]` adds ego writes alongside user
  content), and `only_subsystem` flips into subsystem-only mode
  (`only_subsystem="ego"` returns just ego corrections, for ego's
  own self-recall). Migration 0016 backfills `reflection` for
  existing rows tagged with `reflection_observation` /
  `reflection_summary` in FTS5. Other subsystems are tagged
  forward-only on new writes.
- **LLM-as-judge eval primitive** — new `LLMJudgeScorer`
  (`ScorerType.LLM_JUDGE`), versioned `Rubric` registry, and a
  calibration job that grades a rubric against a hand-graded golden
  set and refuses to promote it below 80% agreement. The judge runs
  through a new `judge` call site in `config/model_routing.yaml`
  (DeepSeek V4 Pro via OpenRouter), so cost, fallback, and circuit
  breakers come for free. First rubric:
  `memory_recall_grounding`. The primitive is the foundation for
  follow-on CRAG retrieval grading and ego eval-drift work; nothing
  in the live runtime calls it yet, so this update is plumbing only
  for now.

### Changed

- **Procedure extraction routes to stronger models.** The
  `38_procedure_extraction` call site chain is now
  `cerebras-qwen` (Qwen 3 235B, free) →
  `openrouter-deepseek-v4` (V4 Pro, paid) →
  `groq-free` (Llama 3.3 70B). Mistral Large and Gemini Flash are
  dropped — Mistral underperformed for this synthesis task and Gemini
  Flash is too small. DeepSeek V4 Pro is enabled via `default_paid:
  true`; at the realistic event-driven frequency of this call site,
  the spend is negligible.
- **`judge` call site is in the L2 / tmp-pressure-high skip lists**
  — when Genesis is degraded or disk-pressured, judge calls back
  off automatically, in line with the existing rules for non-critical
  background work.
- **Confusable call-site IDs renamed.** Three IDs previously shared
  overloaded descriptors that made the routing config ambiguous in
  the neural monitor and source code:
  `17_fresh_eyes_review` → `17_executor_review` (executor Gate 2),
  `23_fresh_eyes_review` → `23_outreach_review` (outreach pre-send),
  `email_triage` → `outreach_email_triage`. If you reference these
  IDs in custom routing config, an eval CLI invocation, or a dashboard
  bookmark, update to the new names. Migration `0015_rename_confusable_call_sites`
  renames existing rows in `call_site_last_run` and `deferred_work_queue`
  at server start; historical `cost_events.metadata` entries are left
  as-written.

### Fixed

- **Outcome classifier no longer silently claims SUCCESS on parse
  failure.** When the LLM response was unparseable, the classifier
  previously returned `OutcomeClass.SUCCESS`, which let bad runs
  silently update autonomy weights and skip procedure extraction.
  Failed classifications now return a dedicated `CLASSIFICATION_FAILED`
  sentinel; the learning pipeline detects it after the classifier call
  and skips downstream learning (delta assessment, attribution
  routing, procedure extraction, steering rule capture) instead of
  proceeding with phantom success. The sentinel renames the internal
  `OutcomeClass.UNKNOWN` value to make its role as an error marker
  explicit (it was never a real 6th outcome category, just a fallback
  bucket).
- **Triage pipeline no longer crashes silently in procedure extraction.**
  The procedure extraction block referenced `summary.output_text`, which
  is not a field on `InteractionSummary` — the correct field is
  `response_text`. The `AttributeError` was caught by the surrounding
  exception handler, so the bug was invisible at runtime but blocked
  every auto-extraction. Same fix applied to the behavioral correction
  recorder (BIS).
- **Call sites with no API key stay visible on the dashboard.**
  Previously, a call site whose entire provider chain had no API key
  configured was silently dropped from `cfg.call_sites` at startup,
  making it invisible everywhere (dashboard, routing API, health
  snapshot). On a partially-configured install (some keys set, some
  empty) you couldn't tell which call sites were unreachable or what
  you needed to add. Keyless providers now stay registered with
  `has_api_key=False`; the router skips them at routing time exactly
  the way it skips a tripped circuit breaker, and the neural monitor
  shows the call site with a red **NO API KEY CONFIGURED** badge plus
  a banner naming the env vars (`API_KEY_<TYPE>`) that would enable
  it. Partial API-key configuration is the normal install state, not
  a bug — it should be discoverable. Sentinel does not alert on
  these sites (existing filter for `wired:False`/`disabled`/no
  `last_run_at` covers it).
- **Approval system overhauled** (#323, #329, #351) — removes
  subsystem scoping, adds instant wake on approval, startup recovery
  for pending approvals, and staleness guard for approvals blocking the
  inbox monitor indefinitely.
- **Ego self-suppression eliminated** (#331) — removes root causes
  of ego cycles suppressing their own output, plus fixes deep reflection
  floor bug.
- **Sentinel alarm flapping cooldown** (#340) — 15-minute cooldown
  prevents repeated alarm/clear cycles from spamming notifications.
  Adds `MemAvailable` metric.
- **CC Bash sandbox moved off volatile `/tmp`** (#357) — sets
  `CLAUDE_CODE_TMPDIR` to persistent disk (`~/.genesis/cc-tmp`),
  eliminating intermittent ENOENT failures that broke the Bash tool
  for 7+ sessions.
- **Inbox startup wake delay** (#354, #355) — uses
  `asyncio.call_later` for reliable startup wake instead of immediate
  wake that raced with event loop bootstrap.
- **Migration 0017 transaction fix** (#350) — removes erroneous
  `db.commit()` since the migration runner manages transactions.
- **Inbox content hash normalization** (#330) — prevents duplicate
  processing. Strengthens YouTube fallback instructions.
- **Learning pipeline structural fixes** (#332) — 5 fixes for the
  procedural learning pipeline.
- **Memory tagging and recall fixes** (#324, #325, #326, #327) ---
  separate episodic/knowledge stores, Qdrant collection tagging, drift
  null safety, CBM hook, and curated KB migration.
- **Contribution gate force-with-lease** (#320) — explicit expected
  SHA prevents accidental overwrites.
- **Cloud-only install Ollama exclusion** (#328) — `probe_ollama`
  excluded from critical failure on cloud-only installs.
- **Runtime config path fix** (#344) — corrects config path in
  runtime/init modules.
- **Surplus operational fixes** (#343) — zombie approvals, backup
  verification, failure visibility.
- **Surplus cognitive context enrichment** (#345) — enriches task
  context for higher-quality surplus output.

### Migrations

- **0014_eval_results_metadata** — adds `metadata_json` to
  `eval_results` for structured judge output.
- **0015_rename_confusable_call_sites** — renames overloaded call-site
  IDs in `call_site_last_run` and `deferred_work_queue`.
- **0016_source_subsystem** — backfills `source_subsystem` column on
  `memory_metadata` for subsystem content filtering.
- **0017_ego_tables** — ego world model, proposal, and session tables.

---

## [v3.0b8] - 2026-05-09

A late-day batch focused on web intelligence, ego self-regulation, and
operational hygiene. TinyFish becomes a first-class web-tools backend,
ego learns to back off when the user is absent, and the surplus surface
gains an autonomous research pipeline.

### Added

- **TinyFish web tools provider** (#292) — new `web_search`,
  `web_fetch`, and `web_agent` adapters under `genesis.providers`.
  TinyFish is the new primary in `web_search` / `web_fetch` auto chains
  (gated on `API_KEY_TINYFISH`), with SearXNG / Brave / Scrapling /
  Crawl4AI retained as fallbacks. `web_fetch` gains a `urls` parameter
  for parallel multi-URL retrieval (1--10 URLs).
- **Anticipatory research pipeline** (#291) — 2-step pipeline
  generating search queries from observation context and synthesizing
  TinyFish-fetched results with source URLs, scheduled every 12h via
  the analytical lane.
- **`SELF_UNBLOCK` brainstorm category** (#291) — third daily
  brainstorm alongside `BRAINSTORM_USER` and `BRAINSTORM_SELF`,
  focused on identifying internal blockers Genesis can clear without
  user input.
- **User-recency cadence tiers for ego** (#287) — ego's max
  cycle interval now adapts to time-since-last-foreground-session
  (5 tiers from 240m at <24h to 4320m at >14d). Adaptive backoff still
  operates within each tier; only the ceiling moves.

### Changed

- **Ego output contracts now include `communication_decision`** (#286)
  — both user and Genesis ego JSON contracts now expose the
  `send_digest` / `stay_quiet` / `urgent_notify` field that was
  previously described in narrative only. The default flips from
  `stay_quiet` to `send_digest`, so proposals are no longer silently
  swallowed when the field is omitted.
- **MCP code-intelligence tools auto-upgrade on install/bootstrap**
  (#299) — `scripts/bootstrap.sh` and `scripts/install.sh` now re-run
  the codebase-memory-mcp installer unconditionally (idempotent, pulls
  latest) and call `uv tool upgrade serena-agent` when Serena is
  already present. Existing installs get the latest versions on the
  next bootstrap; fresh installs are unchanged. GitNexus is
  intentionally left on its prerelease channel.

### Fixed

- **Mergeable check actually fires now** (#290) — the
  UNKNOWN/CONFLICTING block from PR #270 lived in
  `bash_safety_hook.sh`, which was never wired into `settings.json`.
  Moved the check into the actually-deployed `git_push_guard.py`, so
  `gh pr merge` now hard-blocks on UNKNOWN or CONFLICTING mergeable
  status.

---

## [v3.0b7] - 2026-05-09

Ego gets two new self-awareness features, references move into the
episodic graph, and Opus 4.7's xhigh effort tier becomes a first-class
option.

### Added

- **Ego causal intervention journal** (#284) — every proposal now
  tracks its lifecycle (proposed → approved/rejected → executed →
  outcome) in a queryable journal. Ego can correlate decisions with
  outcomes to learn from past judgments.
- **Ego self-model capability map** (#288) — Genesis maintains a
  live capability inventory aggregated from MCP tools, channels,
  modules, and memory wings. Ego references this when proposing
  actions to avoid suggesting things it can't do.
- **Email outbound channel** (#289) — Genesis can now send email
  via the configured outbound provider. Third outreach lane alongside
  Telegram and dashboard.
- **GitHub star tracking** (#289) — recon source captures
  GENesis-AGI repo stargazer activity. Surfaces in morning reports.
- **xhigh effort tier** (#297) — Claude Code 2.1.111's xhigh tier
  for Opus 4.7 is now recognized everywhere Genesis hands off effort
  level (CC invoker, Telegram `/effort`, `session_set_effort` MCP,
  dashboard). Defaults remain at `high`; xhigh is opt-in.
- **Morning report observations** (#285) — recent unresolved
  observations are surfaced alongside the usual morning digest, so
  operators see what Genesis is paying attention to.
- **Follow-up retention cleanup** (#293) — completed and failed
  follow-ups older than 30 days are now purged daily at 02:30 UTC.
  Pinned items are preserved.

### Changed

- **Reference storage migrates to episodic memory** (#296) — 52
  reference vectors move from `knowledge_base` to `episodic_memory`
  via SQLite migration 0013 + Qdrant init-time migration (idempotent).
  References now surface naturally via all memory recall paths.
  `reference_lookup` continues to work; only the storage collection
  changed.
- **Disk alert threshold** (#295) — `health_alerts` now fires
  WARNING at <15% free disk (was CRITICAL-only at <10%). The 10–15%
  gap is no longer a blind spot.

### Fixed

- **Ego self-reinforcing holdback loop** (#283) — ego could spiral
  into withdrawing its own proposals based on its own prior
  decisions. The holdback heuristic now considers proposal age and
  user signal correctly.
- **Heartbeat cleanup not wired** (#281) — subsystem heartbeats
  weren't being aged out, leaving stale records in the dashboard.
- **Surplus task double-enqueue** (#281) — `active_by_type` check
  now matches the dispatch loop's filter, so scheduled surplus jobs
  don't double-enqueue.
- **Outreach metric mislabels** (#289) — corrected mislabeled
  outreach counters in the dashboard.

---

## [v3.0b6] - 2026-05-09

Memory retrieval gets faster graph traversal, explicit drift control,
and better observability.

### Added

- **NetworkX graph engine** (#279) — in-memory graph over 43K+ memory
  links replaces recursive SQL queries. Enables centrality scoring and
  shortest-path queries. Falls back to SQL if NetworkX is unavailable.
- **DRIFT retrieval mode** (#279) — `memory_recall` gains a `mode`
  parameter: `"auto"` (default, unchanged behavior), `"standard"`
  (no drift fallback), `"drift"` (direct 3-phase retrieval).
- **Recall instrumentation** (#279) — every `memory_recall` call now
  logs which pipeline was used (standard, drift, auto→drift) for
  retrieval quality analysis.

### Fixed

- **Knowledge re-ingestion creates duplicates** (#279) — the
  orchestrator now uses idempotent upsert with stale Qdrant cleanup
  instead of raw insert. Re-ingesting a URL no longer creates orphaned
  vectors.
- **DB resilience** (#273) — awareness tick survives transient SQLite
  connection failures with automatic recovery and alert deduplication.

### Changed

- **Dashboard call site badges** (#278) — parallelization indicator
  shows which call sites run concurrently.
- **Routing updates** (#274, #277) — DeepSeek V4 Flash added, GLM 5.1
  renamed, call site descriptions added to routing config.

---

## [v3.0b5] - 2026-05-07

Sentinel gets smarter, ego learns its boundaries, and a cascade of
observation spam gets silenced at the source.

### Changed

- **Sentinel upgraded to Opus** (#245) — the container-side health
  guardian now runs on the strongest available model. Both Sentinel and
  Guardian prompts gain planning directives, tenacity rules, known
  pitfalls from production incidents, and live operational context
  injection from essential knowledge.
- **Ego domain boundaries** (#248) — User Ego no longer tracks
  operational costs or opines on config values. Genesis Ego stays in its
  infrastructure lane. Both egos receive explicit rules separating user
  career goals from Genesis marketing goals.

### Fixed

- **Observation spam eliminated** (#248) — micro-reflection dedup
  was hashing LLM-generated summary text, which varies each tick. Now
  hashes structural properties (tags, anomaly flag, signal names).
  Stops the 21+ duplicate `user_goal_staleness` observations per day.
- **Approval gate restored** (#245) — PR #240 accidentally set the
  live config to `manual_approval_required: false`. Fixed with
  three-layer config separation: code default (True, safe fallback),
  repo YAML (false, friction-free installs), local overlay (user
  preference, gitignored).
- **Telegram polling reconnected** (#245) — adapter_v2 was stuck in
  a stall loop (26 consecutive 900s stalls). Server restart
  reinitialized the connection cleanly.
- **Files tab fills viewport** (#245) — the 1400px max-width
  constraint lifts when the Files tab is active. File content viewer
  now resizable in both directions (#247).
- **Download button visible** (#245) — enlarged with text label.

### Removed

- **CC version watcher deactivated** (#248) — the automatic Claude
  Code update signal was generating noise. Genesis version watcher
  (upstream update detection) stays active.

### Infrastructure

- **Ubuntu/noble portability** (#248) — `scripts/host-setup.sh` now
  accepts `GENESIS_CONTAINER_IMAGE` env var override instead of
  hardcoding `images:ubuntu/noble`.

---

## [v3.0b4] - 2026-05-06

Settings get a proper overhaul, ego recovers from a multi-day deadlock,
and memory recall learns to try harder when results are thin.

### Added

- **Dashboard PWA support** (#242) — manifest + service worker make
  the dashboard installable as a standalone mobile app. Memory tab gains
  a 30-day growth sparkline and wing distribution badges.
- **File download** (#232) — Files tab gets a download button with
  50MB cap, path traversal protection, and symlink-aware security.
- **Drift recall fallback** (#233) — when `memory_recall` returns
  sparse results (<3), the 3-phase drift retrieval algorithm
  (global scan → cluster drill-down → weighted RRF) fires automatically.
  Silent degradation on failure.
- **Query term expansion** (#234) — `expand_query_terms` parameter
  exposed on the `memory_recall` MCP tool, enabling tag co-occurrence
  query expansion for ambiguous searches.

### Changed

- **Settings consolidation** (#240) — all per-subsystem timezone
  fields replaced by `genesis.env.user_timezone()`. Dashboard settings
  tab gets domain ordering, expanded form domains, and descriptions
  for all 18 settings groups.
- **Inbox retry dedup** (#243) — scanner reuses existing failed rows
  instead of creating duplicates on retry. CC invoker captures stderr
  on timeout for diagnostics. Evaluation timeout raised to 900s.

### Fixed

- **Ego deadlock** (#241) — approval blocks no longer trip the circuit
  breaker (new `CycleBlockedError` exception). Approval requests get
  timeouts (1h CLI, 2h sentinel). Telegram proposals split at 4096 chars
  instead of failing silently. Proposal field truncation limits raised
  4–5x.

---

## [v3.0b3] - 2026-05-05

Web tools get MCP exposure so background sessions and subagents can
actually use them. Ego proposals flow through approval correctly.
SSH dispatch enables cross-machine module communication.

### Added

- **SSH IPC adapter** (#225) — external modules can now dispatch
  prompts to remote Claude Code instances over SSH. Two modes: CC
  (structured JSON) and SHELL (raw commands). Enables module
  communication without standing up HTTP services.
- **Protected paths guard** (#226) — PreToolUse hook blocks accidental
  deletion of session transcripts, backups, snapshots, browser profiles,
  and the production database.

### Changed

- **Web tools exposed via MCP** (#229) — `web_fetch` and `web_search`
  are now MCP tools on genesis-health, making Scrapling, Crawl4AI,
  SearXNG, and the paid search backends accessible to background
  sessions, ego, and subagents (previously required Bash/Python imports).
  Behavioral nudges steer sessions toward these over CC's built-in
  WebFetch/WebSearch.
- **Ego proposal flow** (#228) — proposals now route through the
  approval gate correctly. Auto-promote removed; all proposals require
  explicit approval before execution.
- **Sentinel alarm clearing** (#227) — auto-clear fires only when the
  specific pending alarm resolves, not all alarms indiscriminately.
- **Temp file conventions** (#226) — `~/tmp/` documented as the
  standard transient path. `/tmp/` (512MB tmpfs) is off-limits.

### Fixed

- **Migration runner compatibility** (#230) — migration 0010 handles
  databases that lack the `memory_metadata` table (test fixtures, fresh
  installs before DDL runs).
- **Dashboard memory bar** — uses correct anonymization percentage for
  status assessment.
- **Drift recall and step dispatcher** — critical bugs in recall
  query, bi-temporal column migration, and dispatcher routing.

---

## [v3.0b2] - 2026-05-03

Ego becomes perceptive, task execution gets smarter about blockers, and
Genesis can now bootstrap code intelligence tools on fresh machines.
Seventeen PRs landed — a mix of new capabilities, reliability fixes, and
documentation that reflects what the system actually is.

### Added

- **Ego memory surfacing** (#207) — the ego now pulls relevant memories
  before proposing actions, grounds proposals in evidence, and flags
  recurring observation patterns (Hapax-style proactive discovery).
- **Planning-first direct sessions** (#207) — background CC sessions
  receive a planning instruction so they structure work before executing.
- **Voice identity layer** (#207) — `VOICE.md` defines output taste
  (tone, rhythm, vocabulary) injected into content generation and ego
  sessions.
- **Deep research for task blockers** (#216) — when the task executor
  hits an unresolvable blocker, it spawns a deep-research session and
  uses the findings to construct an exit gate, rather than spinning.
- **Architecture Decision Records** (#217) — seven ADRs documenting
  load-bearing choices (ego ephemeral sessions, surplus routing, memory
  wings, no silent timeouts, router dead-letter, LLM-first judgment).
- **Memory DRIFT recall** (#217) — bi-temporal columns on memory
  metadata enable time-aware retrieval and staleness detection.
- **Medium distribution** (#210) — publish to Medium via Camoufox
  browser automation with voice-calibrated formatting.
- **Code intelligence bootstrap** (#222) — `bootstrap.sh` and
  `install.sh` now install and configure codebase-memory-mcp, GitNexus,
  and Serena automatically on fresh machines. Includes MCP registration
  and initial indexing.
- **Architecture deep-dives and case studies** (#213) — three
  subsystem deep-dives (routing, memory, autonomy) and four case studies
  showing Genesis in practice.
- **Positioning and taxonomy docs** (#217) — "Genesis vs. CLAUDE.md"
  differentiator and the Four C's external vocabulary.

### Changed

- **Approval staleness guard** (#208) — stale approval records are now
  pruned on each cycle. Infrastructure monitor respects disable flag.
- **Ego interact profile expanded** (#215) — the interact safety
  profile now permits content publishing dispatch.
- **README primitives section** (#223) — updated to reflect
  genesis-router and genesis-memory as the two extractable libraries.

### Fixed

- **Surplus scoring collapse** (#209) — scoring function no longer
  collapses to zero when all candidates tie. Watchgod /tmp protection
  and surplus routing corrected.
- **Telegram polling** (#211) — retry logic on polling timeout,
  reduced alert noise from transient failures, morning report
  completeness improved.
- **Knowledge source pipeline default** (#206) — new knowledge sources
  default to `knowledge_ingest` pipeline instead of `recon`.
- **Browser keystroke typing** (#221) — CDP remote sessions now type
  per-keystroke instead of bulk-setting input values, fixing sites that
  validate on keypress.
- **CI stability** (#219) — fixed lint errors (unused imports,
  f-string prefixes), duplicate migration prefix detection, and test
  isolation for migration runner.
- **STEERING.md write protection** (#214) — autonomous learning
  pipelines can no longer modify steering rules without user approval.

---

## [v3.0b1] - 2026-05-01

First beta. The ego subsystem---Genesis's autonomous decision-making
layer---is stable and public. Two egos (User Ego and Genesis Ego) run on
adaptive cadence, propose actions via Telegram, and execute approved work
autonomously. The reflection pipeline now feeds both egos balanced
context instead of flooding one with infrastructure noise.

### Added

- **Ego module** (#26, #27) — two autonomous egos with ephemeral
  sessions, model selection, proposal board, and tiered execution.
  User Ego (CEO, Opus) focuses on user goals; Genesis Ego (COO, Sonnet)
  handles system health. Both dispatch CC sessions with approval gates.
- **Reflection rebalancing** (#196) — observations now carry relevance
  tags (`:user`, `:genesis`, `:both`). Each ego sees what it needs
  instead of everything. Two new signal collectors track user goal
  staleness and session activity patterns.
- **Ego context enrichment** (#205) — User Ego now sees an activity
  pulse (goal staleness, session rhythm, conversation count), model
  freshness warnings, and backlog depth (inbox, recon, follow-ups).
  Genesis Ego gets signal trend arrows across ticks. Both egos see
  recent proposal outcomes for self-calibration.
- **Sequential task execution** (#193) — tasks execute one at a time
  with per-step approval skipping for trusted subsystems.
- **Task intake gate** (#199) — SQLite trigger rejects malformed task
  submissions before they reach the executor.
- **Pinned follow-ups** (#185) — follow-up items can be pinned so they
  survive batch resolution.

### Changed

- **Approval gate redesign** (#198) — stable approval keys for
  recurring dispatches (ego cycles, inbox evaluation). One approval per
  request, no reuse of stale approvals. Pass 3 content-blind matching
  removed entirely.
- **Repetitive micro reflections reduced** (#195) — consecutive
  identical micro observations are suppressed.

### Fixed

- **Genesis Ego crash** (#198) — `signals_json` stored as a list, not
  a dict. Every genesis ego cycle hit `AttributeError` on `.items()`.
- **Approval notifications** (#29) — per-tick notifications are now
  idempotent; duplicate approvals filtered (#33).
- **Executor worktree persistence** (#188) — worktree paths survive
  server restarts.
- **Dashboard memory gauge** (#202) — displays anonymous memory
  percentage instead of used percentage.
- **Resilience metrics** (#201) — correct memory metric source, /tmp
  pressure axis, phantom L2 autonomy level.
- **Ego dashboard controls** (#192) — column names, model override,
  budget cap fixes.

---

## [v3.0a11] - 2026-04-28

Guardian auto-sync, task executor maturity, ego module. Themes:
**autonomous execution**, **adversarial verification**, **cognitive
architecture**, and **host VM self-maintenance**.

### Added

- **Guardian auto-sync** (#168, #169, #170, #171) — host VM Guardian now
  stays automatically in sync with container updates. When you update
  Genesis, changed Guardian-relevant code is pushed to the host via SSH.
  Drift detection alerts within 15 minutes if sync fails silently.
  No more manual SSH to update Guardian code.
- **Ego module** (#182) — autonomous decision-making cycle with cadence
  management, proposal board, context assembly (user + Genesis + system),
  and session dispatch. Dashboard route for ego status.
- **LinkedIn distribution** (#182) — content delivery via Composio SDK
  with OAuth2. Graceful degradation when unconfigured. Optional
  `[distribution]` dependency.
- **Typed module config schema** (#167) — `ConfigField` dataclass with
  type/min/max/required/sensitive metadata. `ModuleBase` mixin for
  zero-boilerplate config. Auto-discovery for new modules without YAML.
  Dashboard widget fix: correct input types for all field kinds.
- **Session intent trail** (#179) — detects topic pivots via keyword
  similarity, injects `[Session trail] topic → topic → ...` into every
  prompt so conversation flow survives compaction.
- **Task executor pipeline** (#177) — tool-capable adversarial
  verification with Codex, recovery resume for interrupted tasks.
- **Sentinel rejection test coverage** (#166) — 6 tests verifying the
  24-hour dispatch suppression window after user rejection.

### Changed

- **Decomposer uses CC invoker** (#181) — task decomposition now uses
  CC invoker (Sonnet) instead of route_call. Falls back to route_call
  if invoker unavailable.
- **Adversarial review runs in worktree** (#183) — Codex and CC invoker
  verification now execute in the task's worktree directory, not the
  repo root. Fixup steps receive the original plan content and longer
  feedback (2000 chars, up from 500).
- **Browser concurrency safety** (#166) — all 7 interaction tools now
  acquire a lock before accessing shared page state.

### Fixed

- **Blocked tasks resume on approval** (#178) — dispatcher polls for
  approved-but-unconsumed approvals on blocked tasks, re-dispatching
  without requiring a server restart.
- **Dispatcher dedup guard** (#181) — tasks reset to PENDING are
  re-dispatchable without server restart.
- **Plan path tilde expansion** (#178) — `expanduser()` on plan paths.
- **PENDING→FAILED transition** (#178) — tasks that fail before REVIEWING
  no longer get stuck in PENDING forever.
- **Concurrent session contamination** (#173) — raw user messages from
  other sessions no longer appear in concurrent session tags.
- **Observability gaps** (#165) — `exc_info=True` on timeout-path log
  calls; replaced `contextlib.suppress(Exception)` with logged warnings.
- **Update subprocess logging** (#165) — direct update and CC tier
  spawning now log to `~/.genesis/` instead of /dev/null.

### Upgrade notes

**Existing users with Guardian on a host VM:** One-time bootstrap required
to enable auto-sync. Run on your **host VM** (not the container):

```bash
cd ~/.local/share/genesis-guardian
incus exec genesis -- tar -cf - -C /home/ubuntu/genesis \
    src/ scripts/ pyproject.toml config/guardian-claude.md | tar -xf -
cp scripts/guardian-gateway.sh ~/.local/bin/guardian-gateway.sh
chmod +x ~/.local/bin/guardian-gateway.sh
systemctl --user restart genesis-guardian.timer
```

Or: `bash scripts/install_guardian.sh --non-interactive`

After this one-time step, all future updates are automatic.

---

## [v3.0a10] - 2026-04-24

31-commit release. Themes: **multi-step surplus pipelines**, **browser
stealth**, and **reflection quality**.

### Added

- **Surplus pipeline engine** (#147, #149) — deterministic multi-step
  task chains for analytical work. Each step runs on free-tier models;
  the pipeline mechanically advances between steps. First pipeline:
  prompt effectiveness review (catalog call sites, sample outputs,
  evaluate and recommend improvements).
- **Follow-up management** (#146) — `follow_up_update` MCP tool for
  modifying tracked follow-up items.
- **Browser stealth layer 2** (#128) — humanized mouse movements, typing
  cadence, click randomization, and CAPTCHA escalation for automated
  browser sessions.
- **CDP remote backend** (#135) — drive a real Chrome browser over
  Tailscale instead of running headless locally.

### Changed

- **Reflection quality improvements** (#123, #127, #139) — identity
  context for API reflection path, surplus decoupled from reflection
  engine, sentinel recovery wiring, light cognitive state, frequency
  tuning, and NOMINAL quality gate for infrastructure monitoring.
- **Browser reliability** (#126, #133, #134) — always-headed mode, hard
  timeouts, keyboard fallback, ambiguous selector guard, noVNC scaling
  fix.

### Fixed

- **Database write serialization** (#141) — prevents permanent connection
  lock when concurrent writes collide on aiosqlite.
- **Dashboard scroll restoration** (#124) — mouse wheel scrolling works
  on all pages again.
- **Sentinel dashboard indicator** (#130) — yellow indicator for approval
  states plus CI skip markers.
- **Safety fixes** (#136) — surplus test hardening, morning report idle
  filter, sentinel re-verify.

### Removed

- **Infrastructure monitor schedule** — removed from surplus cron.
  Produced noise (459 insights, 1 promotion). Returns as a focused
  "monitor the monitors" pipeline in a future release.

---

## [v3.0a9] - 2026-04-22

7-commit release. Themes: **background session spawner**, **content
pipeline**, **browser reliability**, and **outreach fixes**.

### Added

- **Direct session spawner** (#118, #121) — spawn profile-constrained
  background CC sessions via `direct_session_run` MCP tool. Three safety
  profiles (observe, interact, research) control what each session can do.
  DB-backed dispatch queue ensures sessions outlive the calling session.
- **Content pipeline activation** (#117) — content module wired into
  outreach system with CONTENT category for multi-platform publishing.
- **Browser process hygiene** (#115) — idle timeout (1h auto-cleanup),
  orphan process detection, background reaper for stuck browser processes.

### Changed

- **Browser stale context recovery** (#116) — detects dead browser pages
  and transparently reconnects. Session history tracking and VNC
  environment improvements.

### Fixed

- **Outreach pipeline** (#122) — approval reuse, alert routing, surplus
  topic handling, staleness decay. Fixes pre-existing test failures in
  cognitive state rendering.

---

## [v3.0a8] - 2026-04-21

21-commit release. Themes: **knowledge dashboard UX**, **browser
automation upgrade**, **cross-session awareness**, and **CI/security
hardening**.

### Added

- **Knowledge dashboard overhaul** (#104) — in-page confirm modals
  (immune to browser dialog blocking), drag-drop file upload, processing
  mode toggle (extract vs store-as-is), parallel distillation pipeline
  (4x concurrent), and crash recovery for stuck uploads.
- **File modification audit trail** (#109) — PostToolUse hook records all
  Write/Edit operations with session ID, file path, and file hash. Query
  "what session modified this file?" in one SQL call.
- **Browser collaborative mode** (#107) — side-panel extension for
  real-time observation of automated browser sessions.
- **Cross-session awareness** (#97) — awareness loop now tracks
  observations across sessions with TTL-based hygiene.
- **Output safety convention** (#112) — pre-commit hook warns when
  non-code files are staged, directing to `~/.genesis/output/`.

### Changed

- **Camoufox as primary browser** (#108) — anti-fingerprint browser now
  default for all automation. Chromium available as fallback.
- **Neural monitor grid redesign** (#106) — reorganized dashboard grid
  layout for better information density.
- **Proactive memory enrichment** (#95, #96) — hook results now include
  age, wing, and ID for expand-without-re-search. Limits bumped to
  300/200 chars with smart sentence truncation.
- **Cerebras-Qwen routing** (#104) — promoted to 6 call site chains
  (3 primary, 3 fallback) for surplus and knowledge workloads.
- **Sweep infrastructure** (#98, #102) — provider registry cleanup, MCP
  audit, CLAUDE.md compression.

### Fixed

- **CI test suite** (#110) — resolved 30 pre-existing failures. Skip
  guards for optional dependencies, mock fixes, routing assertion updates.
- **Security hardening** (#111, #113) — prevent stack trace exposure in
  file API responses, clear-text logging of sensitive reference data.
- **Surplus Telegram delivery** (#105) — surplus-originated reflections
  now reach Telegram instead of silently completing.
- **Approval system** (#101) — micro-reflection salience gate removed
  (user sees everything), approval_request_id now populated on
  cli_approved.
- **Stale update banner** (#103) — dashboard auto-resolves the
  update-available banner after successful update.
- **Process reaper** (#10aa9edc) — extended to kill stale Claude sessions
  older than 7 days.

---

## [v3.0a7] - 2026-04-19

25-commit release. Themes: **dashboard and settings overhaul**, **web
fetching upgrade**, **timezone correctness**, and **operational
documentation**.

### Added

- **Scrapling TLS fingerprinting** (#75) — web fetcher upgraded with
  anti-bot bypass via `curl_cffi` TLS impersonation. Cloudflare Quick
  Actions (`/markdown`, `/json`) for JS-rendered content extraction.
- **Observation surfacing + output verification** (#77) — autonomous
  task executor now verifies its own output against success criteria.
  Observations surface in dashboard and outreach.
- **Surplus config wiring + DB-backed approvals** (#84) — surplus
  compute settings configurable via dashboard. Sentinel approvals
  persisted to database (survive restarts).
- **MCP module config overlay** (#94) — MCP tools now discover modules
  from both repo and local config directories, matching runtime behavior.
- **Contribution sanitizer** — auto-blocks gitignored paths from upstream
  PRs.

### Changed

- **Identity file deduplication** (#93) — consolidated overlapping
  content across CLAUDE.md, SOUL.md, STEERING.md, and CONVERSATION.md.
  Each file now has a distinct scope with no redundancy.
- **Settings panel functional** (#82, #89) — settings viewer, routing
  panel consolidation, environment variable expansion fix. Previously
  read-only, now editable.
- **Approval queue** — moved from dedicated page to dashboard overview
  with inline resume mechanism.
- **Knowledge and Memory UI** (#86) — resizable file browser, improved
  layout, tmux compatibility fix.
- **Process management docs** — CLAUDE.md documents systemd units, MCP
  server lifecycle, and the nohup prohibition.

### Fixed

- **Timezone across the board** (#79) — outreach scheduling, alert
  timestamps, and follow-up due dates now respect the configured user
  timezone instead of defaulting to UTC.
- **Neural monitor accuracy** (#92) — disabled providers excluded from
  health display and dropdown. Accuracy metrics cleaned up.
- **Anthropic provider regression** (#88) — providers restored after
  routing config change accidentally dropped them. False queue-empty
  alerts eliminated.
- **SSH PATH** (#87) — Claude CLI now found in SSH RemoteCommand context
  (Guardian diagnosis sessions).
- **Knowledge tab** (#82, #83) — stats endpoint AttributeError fixed,
  CSS corrected, tab fully functional.
- **Strategic reflection routing** (#81) — reflection sessions now route
  to correct providers. Essential knowledge noise reduced.
- **Morning report** (#85) — formatting, missing data handling, and
  observation inclusion fixes.
- **Extraction quality** (#76) — dashboard thresholds tuned, code index
  priority corrected.

---

## [v3.0a6] - 2026-04-17

137-commit release. Major themes: **knowledge ingestion pipeline**,
**embedding storm fix** (Ollama CPU spikes eliminated), **awareness
scoring overhaul**, and **persistent reference store**.

### Added

- **Knowledge ingestion pipeline** (#67, #68) — `knowledge_ingest` MCP
  tool for ingesting files and URLs as authoritative knowledge units.
  Dashboard file upload UX with drag-drop support and ingestion worker.
- **Awareness scoring overhaul** (#65) — signal redistribution across
  subsystems, subsystem-level signals, citation tracking for score
  attribution.
- **Persistent reference store** (#58) — unified store for credentials,
  URLs, IPs, and account handles learned across sessions. Auto-capture
  from conversations, `reference_lookup` retrieval, read-only mirror at
  `~/.genesis/known-to-genesis.md`.
- **Merge/push safety hooks** (#60) — PreToolUse hooks block `git merge`
  on main and `git push origin main` to enforce PR workflow.
- **Session observer** — real-time tool activity capture for foreground
  CC sessions, feeding memory extraction.
- **Codebase navigation MCP tool** — progressive drill-down code
  exploration (`codebase_navigate`).

### Changed

- **Queue-first extraction** (#66) — memory extraction no longer
  hammers the embedding backend with hundreds of sequential calls.
  Stores FTS5-only, queues embeddings for the recovery worker's paced
  drain (10/min). Reduces Ollama embed calls from ~562/hr to ~10/min.
- **Ollama health cache** (#66) — `is_available()` results cached for
  120s, eliminating ~818 uncached `/api/tags` polls per hour.
- **Budget event emission** (#71) — `budget.exceeded` events fire once
  per budget period (daily/weekly/monthly) instead of on every routing
  call. Reduces log entries from ~2,857/7hr to 1 per period crossing.
- **Embedding recovery drain limit** — increased 100 → 500 to handle
  full extraction cycle output in a single recovery pass.
- **CLAUDE.md scope split** — extracted Serena guide, moved dev rules
  to genesis-development skill, compressed main CLAUDE.md.

### Fixed

- **Systemd PATH** (#66) — service templates now include Claude CLI bin
  dir (`__CC_BIN_DIR__`), fixing "Claude CLI not found" errors in
  Telegram bridge sessions. Detected at install time, falls back to
  `~/.npm-global/bin`.
- **Embedding recovery status** (#66) — recovery worker now updates
  `memory_metadata.embedding_status` from "pending" to "embedded"
  after successful recovery (was stale on the queue-first path).
- **Security**: redact identifier in migration dry-run log (#70).
- **Cognitive state catch-22** (#64) — dashboard quality issues and
  circular dependency in state initialization.
- **Backup passphrase, cost attribution, dashboard UX** (#63) — four
  fixes from post-Codex audit.
- **OpenCode wrapper** (#61) — silent exit when no stale sessions exist.

---

## [v3.0a5] - 2026-04-17

120-commit batch release — memory v4, surplus compute, eval framework,
follow-ups, new providers, and update-system improvements.

### Added

- **4-layer memory redesign** (#37) — hybrid retrieval (vector + FTS5 +
  RRF fusion), wing/room taxonomy, essential knowledge layer, activation
  scoring, graph traversal.
- **Skill validator + evolution pipeline** (#34) — validation framework
  for skills with evolution tracking.
- **Encrypted backups** (#53) — Qdrant snapshot encryption, backup
  history migration script.
- **"Your Genesis"** (#48) — encrypted backups, `restore.sh`, unified
  docs for the dual-repo model.
- **Outreach recovery worker** — retries failed deliveries with backoff.
- **Approval staleness + session timezone** — stale approvals
  auto-expire, timezone-aware session tracking.
- **Dashboard timezone endpoint** — configurable timezone via settings.

### Fixed (install hardening, PRs #46-52)

13 install fixes from fresh-VM testing:
- Auto-scale container resources to host capacity (#46).
- Five bugs from fresh VM install test (#47).
- Single incus exec smoke test + timezone seed (#50).
- Unbound `UBUNTU_UID` in timezone seed (#51).
- Remove `secrets.env` seed that broke `git clone` (#52).
- TTY detection, timezone persistence across `apt-get`, `read` EOF.

### Fixed (other)

- **Security**: CodeQL findings — stack-trace exposure, workflow
  permissions (#39).
- **Reflection**: post-Codex audit Phase 1+2 — stop silent failures,
  influence timing, surplus count, scheduler timezone, `parse_failed`.
- **Routing/Sentinel**: `cb.is_available()` fix + `watchdog_failing`
  Tier 2.
- **Guardian**: SSH test uses gateway-compatible ping; `cp -rT` for
  update path.
- **Telegram**: offset persist suppressed on fresh processes (#43).
- **Dashboard**: portability — genericize tz examples (#45).
- **Outreach**: remove dead dedup code.
- **CI**: detect-secrets false positive allowlist (#41).
- README updated — Genesis in 30 seconds, quickstart first, 100k+ LOC.
- Stale branch auto-cleanup after public releases (#44).

---

## [v3.0a4] - 2026-04-13

### Changed

- **Merge-based update system** — Genesis updates via `git merge` instead
  of rebase, compatible with the dual-repo model. Three-tier CC
  escalation for conflict resolution: Haiku (watch), Sonnet (resolve
  trivial), Opus (deep incompatibilities). Crash recovery via
  `update_state.json` phase tracking with automatic rollback.
- Tag-based version comparison (robust against squash-merge divergence).
- Dashboard poll timeout extended to 10 minutes.
- Service management without systemd D-Bus session bus (reads PID from
  lock file).

### Fixed

- PID file cleanup moved to Python `finally` blocks.
- `proc.wait(timeout=3600)` prevents hung CC session wedging background
  thread.
- Escalation recovery in `update_progress()` auto-spawns Tier 2 after
  Flask restart.
- JSON heredoc injection fixed (`FAILEOF`/`CEOF` replaced with
  `json.dumps` via env vars).
- Removed nohup fallback from service management; systemd only.
- `_orchestrator_alive` set inside lock before `thread.start()` (TOCTOU).

---

## [v3.0a3-hf3] - 2026-04-12

Public-primary repo overhaul — Genesis now defaults to install-agnostic
configuration. Machine-specific values (IPs, timezone, GitHub identity)
move to `~/.genesis/config/genesis.yaml` instead of being hardcoded in
the repo. Sets up the public repo (`GENesis-AGI`) as the primary
development target going forward.

### Added

- **Local config overlay** (`~/.genesis/config/genesis.yaml`). Three-tier
  precedence: env var > local config > safe default. Covers Ollama/LM
  Studio URLs, timezone, GitHub identity. Generate with
  `./scripts/setup-local-config.sh`.
- **`setup-local-config.sh`** — Interactive setup script for new installs.
  Auto-detects system timezone, migrates `career-agent.yaml` to local
  overlay on first run.
- **Local module overlay** (`~/.genesis/config/modules/`). User-specific
  module configs (e.g. career-agent) live outside the repo; local files
  take precedence over repo files on same filename.
- **Local research-profile overlay** (`~/.genesis/config/research-profiles/`).
  `ProfileLoader.merge_overlay()` loads user-specific profiles not
  committed to the repo.
- **CI leak detector** — `leak-detector` job in `.github/workflows/ci.yml`
  blocks PRs with hardcoded timezones, personal paths, private repo refs,
  secrets (`detect-secrets`), and personal email addresses.
- **`config/genesis.yaml.example`** — Template for local config.

### Changed

- Config YAMLs (`ego`, `outreach`, `inbox_monitor`, `mail_monitor`):
  timezone defaults changed from `America/New_York` to `UTC`. Existing
  installs set timezone in `~/.genesis/config/genesis.yaml`.
- `tz.py`, dataclass defaults, and config loaders now resolve timezone
  via `user_timezone()` from `env.py` instead of hardcoded string.
- CLAUDE.md: hardcoded IPs and GitHub usernames removed; network config
  points to local config file.
- `.claude/docs/dual-repo.md` rewritten for three-repo model.

### Fixed

- `prepare-public-release.sh` portability scan now excludes `ci.yml`
  (the leak-detector job contains timezone patterns as scanner definitions,
  not config leaks). Removed stale `Build Order` CLAUDE.md regex.

---

## [v3.0a3-hf1] - 2026-04-11

Hotfix immediately after v3.0a3 to restore Phase 6 functionality in the
public release and clear a caplog-flakiness regression. Also rides along
a small community security fix.

### Fixed

- **Release-pipeline templating was too broad.** `prepare-public-release.sh`'s
  step 5b passes (`find + grep + sed -i`) rewrote the contribution
  sanitizer's own regex patterns, the `tz.py` default timezone, and a
  couple of test fixtures that legitimately hold these literals as data.
  In the v3.0a3 public release this shipped a broken Phase 6 sanitizer —
  patterns like `${HOME}/genesis` didn't parse as intended, `\bUTC\b` was
  flagging the opposite of user-specific timezones, and the `tz.py` default
  `_DEFAULT_TZ` was clobbered. Added inline `-not -path` exclusions to
  every 5b templating pass for `src/genesis/contribution/sanitize.py`,
  `tests/test_contribution/test_sanitize.py`, `src/genesis/util/tz.py`,
  `tests/test_util/test_tz.py`, `tests/test_autonomy/test_protection.py`,
  `tests/test_hooks/test_inline_hooks.py`, and `tests/conftest.py`.
  Restores Phase 6 sanitizer correctness and clears 11 public CI failures.
- **Flaky `caplog` assertion in `test_dispatch_unknown_falls_back_to_dual`.**
  Commit `0ad9567` had previously removed the exact same assertion
  because caplog's logger-name filter interacts with other tests' logger
  configuration under the full suite; commit `3bbae15` re-introduced it
  in the F1 dispatch routing wiring. Dropped the log-message sniff again;
  kept the behavioural fallback assertion.
- **Telegram adapter refuses to start with empty / invalid
  `TELEGRAM_ALLOWED_USERS`.** Cherry-picked from community PR
  `WingedGuardian/GENesis-AGI#29`. Previously the bot would start silently
  and allow messages from **all** users when `allowed_users` was empty or
  contained only invalid UIDs (e.g. someone pasting a bot token into the
  wrong field). Dashboard `PUT /api/genesis/secrets` now rejects values
  containing `:` (looks like a bot token) or non-numeric IDs with a clear
  error pointing to `@userinfobot`. `secrets.env.example` documents the
  expected format for each Telegram field.

### Known Issues (tracked as follow-ups, not blocking this hotfix)

- `tests/test_runtime/test_runtime_retriever.py::test_retriever_created_after_bootstrap`
  fails only in GH Actions CI (passes locally on 2026-04-11) — suspected
  test isolation / mock state pollution under the full suite. Filed as a
  follow-up investigation; does not affect runtime behaviour.
- `tests/test_qdrant/test_collections.py` has no `skipif` fixture and
  errors (not fails) when Qdrant isn't running on `localhost:6333`.
  Separate hotfix will add a module-level fixture that pings the port and
  skips the suite with a clear message on `ConnectionError`.

---

## [v3.0a3] - 2026-04-11

Large release. Major new features: **community contribution pipeline**
(Phase 6), **Sentinel** container-side guardian, **self-update infrastructure**,
and a top-to-bottom overhaul of the install experience, Guardian recovery,
approval UX, and the neural monitor dashboard. Also clears a long tail of
runtime, routing, and observability issues accumulated since v3.0a2-hf5.

### Added

**Community contribution pipeline (Phase 6)**

- **`genesis contribute <sha>` CLI** — one-shot pipeline that converts a
  `fix:` commit into a draft PR against the public Genesis repo. Flow:
  divergence check → version gate → sanitizer → adversarial review →
  consent prompt → draft PR via `gh`. Pseudonymous by default
  (`contributor-<id>@genesis.local`); `--identify` uses the user's real
  git identity. MVP scope: bug fixes only (`--allow-non-fix` to override).
- **Post-commit offer hook** — committing a `fix:` commit drops a marker
  in `~/.genesis/pending-offers/`; the `contribution_offer_hook.py`
  UserPromptSubmit hook injects a `[Contribution]` system-reminder on
  the next prompt so Genesis can proactively offer to upstream the fix.
  `fix(local):` scope opts out of the offer entirely.
- **Fail-closed sanitizer** — refuses any diff containing secrets, personal
  email addresses, hardcoded IPs, `/home/ubuntu` paths, or files on the
  `contribution_forbidden` tier of `config/protected_paths.yaml`. Runs
  `detect-secrets`, portability, and path-tier scanners.
- **Adversarial review chain** — Codex CLI first, Claude Code subagent
  fallback, Genesis-native reviewer last. First-success wins; result is
  embedded in the PR body.
- **PR body metadata** — every generated PR includes contributor install
  version (`<version>@<short-sha>`), version drift status, pseudonymous
  install ID, sanitizer finding count + scanners run, and review result.
- **Branch-push flow** — contributions land on a fresh branch named by
  commit sha, pushed to the contributor's fork. E2E CLI test covers the
  full hook → sanitizer → review → branch-push path.

**Sentinel (container-side guardian)**

- **New package `src/genesis/sentinel/`** — container-side complement to
  the host-side Guardian. Runs inside the container, monitors Genesis
  infrastructure with the fire alarm taxonomy (WARN / DEGRADED / DOWN),
  and triggers dormant remediations via the registry.
- **Trigger sources + infrastructure monitor** — wires Qdrant, database,
  memory, and process health into the Sentinel trigger pipeline.
- **Runtime wiring + capability registration** — Sentinel registers as a
  first-class capability, surfaces state in the dashboard Services card,
  and its awareness is folded into Guardian briefings + diagnosis.
- **V4 architecture §8.1/8.2 updated** with implementation status.

**Self-update infrastructure**

- **`GenesisVersionCollector`** — awareness-loop collector checks for
  upstream updates every 6h (configurable), stores observations, sends
  Telegram alerts, surfaces "update available" in the dashboard health
  panel, and detects update failures.
- **Update settings domain** — new `config/updates.yaml` with check
  interval, notification channel, and auto-apply policy (opt-in only).
  Configurable via `settings_update("updates", ...)` MCP tool.
- **Schema migration framework** — `src/genesis/db/migrations/` with
  `MigrationRunner`, CLI (`python -m genesis.db.migrations`), and
  versioned migration files. Tracking table `schema_migrations` records
  applied migrations. First migration: `update_history` table.
- **Public release CI/CD** — `.github/workflows/public-release.yaml`
  triggered on version tags. Runs `prepare-public-release.sh`, secret
  scan, portability scan, and uploads sanitized artifact for maintainer
  review.
- **`detect-secrets` dependency** — added to `[release]` optional deps in
  `pyproject.toml`, unblocking the secret scan step that was previously
  silently skipping.

**Install & host setup**

- **13 resilience fixes from failure-mode audit** — hardens `install.sh`,
  `bootstrap.sh`, and `update.sh` against partial failures, rerun damage,
  missing preconditions, and silently-skipped steps.
- **Container smoke test + damage detection on re-run** — re-running the
  installer now detects a damaged previous run and either repairs or
  fails loudly instead of silently producing a broken state.
- **Tailscale in host setup** — `host-setup.sh` installs Tailscale and
  prompts for authentication during setup (supports `TAILSCALE_AUTH_KEY`
  for unattended installs).
- **Node.js + Claude Code on host VM** — `host-setup.sh` installs Node.js
  20.x and Claude Code on the host (not just the container), enabling
  Guardian CC diagnosis sessions.
- **Node.js ≥ 20** required (was 18); Guardian state reset on container
  recreate.

**Approval UX & autonomy**

- **Approval UX redesign** — dedicated Telegram topic, inline buttons,
  call-site gating so approvals are attributed to the caller, not the
  model. Batch CLI approval flow.
- **Autonomous CLI approval gate wired into standalone server** — gate +
  `approvals` topic registered during standalone startup (previously
  only wired in the AZ hosting mode, silently disabled standalone).
- **Inbox approval-pending resume flow** — stable approval key + resume
  path so restarts don't orphan in-flight approvals.

**Dashboard & observability**

- **Neural monitor visual overhaul** — glowing dots, cleaner layout,
  proportional radial placement, constellation map layout option,
  provider chain fixes. Dispatch mode toggle wired to runtime routing
  with save-verify feedback.
- **Sentinel state in Services card.**
- **Config tab UX overhaul** — visibility, dropdowns, tooltips, health
  indicators. Secret values gated behind auth.
- **Dropped-tick events surfaced** from the awareness loop.
- **Container memory decomposition** into anon/file/kernel components.
- **`runtime.peek()`** — read-only runtime snapshot used by observability
  callers that previously forced full runtime access.

**Docs & conventions**

- **No-silent-timeouts rule** added to `CLAUDE.md` — new timeouts on
  reflections, CC calls, and long-thinking paths require explicit user
  approval with evidence of a real failure mode.
- **Never ignore a bug** rule — bugs encountered in any work must be
  fixed inline or tracked as follow-ups; "out of scope" is not an option.
- **V4 ego / infra self-monitor design** + incident report.

### Changed

- **`update.sh` overhaul** — pre-update backup via `backup.sh`, rollback
  tags (`pre-update-{timestamp}`), idempotent `bootstrap.sh` post-pull
  (replaces manual pip install), health verification with 3× retry,
  automatic rollback on failure, CC-assisted recovery context file
  (`~/.genesis/last_update_failure.json`).
- **Mistral routing rationalization** — consolidated Mistral providers
  and call sites, raised `mistral-large-free` rpm 2 → 4 and
  `mistral-small-free` rpm 2 → 30 based on observed usage (previous
  limits were ~5× over-conservative).
- **Routing tail fallback** added for sites 29 and 35, stopping the
  sentinel DOWN alarm from chain exhaustion.
- **Proactive DLQ orphan scan** on routing config reload — expires DLQ
  items whose `call_site_id` no longer exists instead of leaving them
  stranded.
- **Misinterpreted memory-backlog signal removed end-to-end** from the
  awareness loop (was firing on normal state).
- **Watchdog staleness threshold** 300s → 900s to stop false positives
  during legitimate long-running ticks.
- **`runtime/_core.py` split** under the 600 LOC soft target — converted
  to `runtime/` package with 20 init modules. Extracted mixins:
  `_properties.py`, `_pause_state.py`, `_init_delegates.py`,
  `_degradation.py`, `_capabilities.py`, `_job_health.py`. Re-exported
  from `__init__.py` for backward compatibility.
- **`ashutdown`** async shutdown path + `job_health` envelope for MCP
  health surfaces.
- **`8_memory_consolidation`** call site renamed to `8_ego_compaction`
  for clarity.
- **Ego sessions** remain inert until beta — built but not registered
  in bootstrap.

### Fixed

- **Guardian recovery hardening** — auth middleware was blocking
  Guardian's own health probes, contributing to the 2026-04-08 memory
  exhaustion incident. Auth now gates browser pages only; `/api/` and
  `/v1/` routes are exempt. See `docs/incidents/2026-04-08-memory-exhaustion.md`.
- **Broken page cache reclaim** — watchdog/Guardian collector service
  name fix plus explicit reclaim trigger; container no longer drifts
  toward OOM under sustained read load.
- **Guardian heartbeat decoupled from HEALTHY state** — previously,
  Guardian only emitted heartbeats while reporting HEALTHY, so DEGRADED
  or DOWN states silently stopped the heartbeat stream.
- **Guardian ICMP probe** retries once to absorb bridge ARP races that
  were producing spurious DOWN readings on container recreate.
- **Runtime status writer decoupled from awareness tick** — a slow tick
  no longer blocks status writes, and a slow status write no longer
  delays the next tick.
- **`surplus.py` zombie runtime singleton** — the surplus worker was
  spawning a parallel Genesis runtime in-process when the primary
  runtime's observability snapshot asked for state. Fixed by routing
  through `runtime.peek()`.
- **Circular import crashing `genesis-memory` MCP** — resolved, with
  loud failure reporting instead of the previous silent-skip behavior.
- **Browser tools converted from Playwright sync → async API** —
  sync-in-async-context was deadlocking the MCP server.
- **`TopicManager` wired into standalone startup path** (was only
  wired in AZ mode, silently missing in standalone).
- **IPC non-dict response wrapping** — `module_call` no longer returns
  a bare list when a module returns one; wrapped consistently so
  callers don't need to handle both shapes.
- **Inbox routing** — removed free-SLM routing path, kept approval gate,
  fixed empty-content bug that was dropping messages.
- **Autonomous DM silent fallback surfaced** — fallback path used to
  silently succeed with no user visibility; now surfaces the fallback
  and doesn't stall reask on fail.
- **`update.sh` rollback correctness** — rollback used `git checkout <tag>`
  which left the repo in detached HEAD. Now `git checkout main &&
  git reset --hard <tag>` preserves the branch. Silent failure paths
  (`|| true`) removed, ERR trap covers all mutating steps, health
  endpoint + migration failures now trigger rollback. Added worktree
  guard (refuses to run from `.claude/worktrees/`). `update_history`
  rows written on both success and failure.
- **Migration runner atomicity** — body + tracking row were committed
  separately (risk of "applied but unrecorded"), and Python sqlite3
  auto-commits before DDL when using `db.commit()`/`db.rollback()`.
  Fixed with explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` SQL
  including DDL in the transaction. Regression test added.
- **Public release CI secret scan** — `detect-secrets` failures were
  silenced by `|| true`, bare `except: print(0)`, and `2>/dev/null`,
  converting scanner crashes into "0 findings" (false PASS). Now fails
  loudly.
- **`GenesisVersionCollector`** — `_check_upstream` silently returned
  `(0, "")` on git fetch failure. Now raises with stderr context.
  Local update resolves prior `genesis_update_available` observations
  so dashboard alert clears immediately. Failure file archived to
  `.processed.json` after processing instead of being re-read every
  awareness tick.
- **Updates settings validator** — non-dict sections silently passed;
  `auto_apply.allowed_impacts` accepted `action_needed` and `breaking`
  despite config comment saying those always require manual approval.
  Both now rejected.
- **Observability** — `errors.py` data-returning paths now log at ERROR
  with `exc_info=True` (dead letter query, circuit breaker check, event
  log query, genesis update alert query). One wrong log message fixed.
- **Health MCP** — hermetic cleanup rounds 2 + 3, transport smoke
  canary expanded to full read-only matrix, heartbeat query error
  raised DEBUG → ERROR, narrow error handling, tighter bootstrap
  manifest messages, worktree test isolation fix in `conftest.py`.
- **Test suite** — cleared 26 pre-existing test failures; root-caused
  test pollution; added 31 new tests for version collector, migration
  runner atomicity, and settings validator edge cases.
- **`SMOKE_FAIL` unbound var** in install scripts.
- **Integer pixel margins** for neural monitor periphery dots (were
  rendering blurry on fractional values).

### Known Limitations

- **Phase 6 MVP is bug-fixes-only.** Feature contributions are blocked
  by the version gate unless `--allow-non-fix` is passed explicitly.
- **Ego sessions remain inert.** Built but not registered in bootstrap;
  will be wired when the autonomous proposal pipeline is ready for
  live use.

---

## [v3.0a2-hf5] - 2026-04-07

### Added

- **Tailscale in host setup** — `host-setup.sh` now installs Tailscale and
  prompts for authentication during setup. Headless server users get an
  immediately usable dashboard URL on their tailnet without SSH tunneling.
  Supports `TAILSCALE_AUTH_KEY` env var for CI/unattended installs.
- **Node.js + Claude Code on host VM** — `host-setup.sh` now installs
  Node.js 20.x and Claude Code on the host VM (not just inside the
  container), enabling Guardian CC diagnosis sessions and direct host
  interaction from day one.

### Changed

- **Guardian framing** — Guardian is no longer framed as optional. Install
  failures now show a prominent box identifying Guardian as a core subsystem
  (health monitoring, diagnosis, recovery) that must be fixed. Final setup
  report reworded: Guardian is "always running"; Claude Code auth enables
  agentic diagnosis as an add-on, not as the thing that "enables" Guardian.

---

## [3.0a2-hf4]

### Fixed

- **GCP split-disk install** — on cloud VMs where `/home` is a separate
  larger disk than `/`, Incus now stores container data under
  `/home/incus-data` instead of the root partition. Disk check validates
  the actual Incus storage location and requires 15GB free.
- **Guardian pip bootstrap** — Debian creates venvs without pip even when
  `ensurepip` imports successfully (module is present but non-functional).
  Guardian now detects missing pip post-venv and bootstraps via
  `ensurepip --upgrade` or `get-pip.py`.

---

## [3.0a2-hf3]

### Added

- **Provider Keys panel** — write-only secrets management in Settings tab.
  Shows configured/not_set status for all 39 API keys across 7 groups parsed
  from `secrets.env.example`. Values are never returned by the API. Atomic
  file writes (tempfile + os.replace), chmod 600, immediate env reload.
- **Config tab UX** — human-readable labels, tooltips, dropdowns for enum
  settings (provider, model, effort, channels), proper domain name display.
  Replaced all underscore identifiers and free-text fields that need exact values.

### Fixed

- **Install portability** — `install_guardian.sh` now auto-detects host Python
  version and installs the matching `python3.X-venv` package if missing.
  Supports Debian 12 (Python 3.11) — Guardian only needs pyyaml, no 3.12
  requirement on the host VM.
- **Container venv** — `host-setup.sh` tries `python3.12-venv` first, falls
  back to `python3-venv` for distros that don't package them separately.
- **Network identity in CLAUDE.md** — `update.sh` now detects and rewrites
  unresolved template variables (`${CONTAINER_IP:-localhost}` etc.) with
  real IPs from the running container and guardian_remote.yaml.
- **Pre-commit hook** — `secrets.env.example` was blocked by the secrets
  file filter (regex matched `secrets\.env` before the `.example` suffix).
  Now explicitly allows `.example` files through.

---

## [3.0a2-hf2]

### Added

- **Dashboard authentication** — optional password-based access control for the
  dashboard. Set `DASHBOARD_PASSWORD` in secrets.env to enable. Cookie-based
  30-day sessions, rate-limited login (5 attempts/5-min lockout), logout button.
  When no password is set, dashboard works as before (backward compatible).
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
