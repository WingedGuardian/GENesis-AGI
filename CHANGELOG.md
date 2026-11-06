# Changelog

All notable changes to Genesis are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows Genesis release stages (v3.0a → v3.0b → v3.1 → v4.0a…).

---

## [Unreleased]

## [v3.0b19] - 2026-10-24

### Added

- **Daily free-tier budgets for providers.** Providers can now carry
  `rpd_limit` (requests/UTC-day) and `tpd_limit` (tokens/UTC-day) in
  `config/model_routing.yaml`, each in the provider's own unit. When a
  provider's daily budget is spent, routing deselects it until the next UTC
  day instead of burning the rest of the day on 429s that silently push load
  onto paid fallbacks — you get one warning event at the crossing, and the
  live counters appear in the dashboard's routing config API
  (`daily_budget`). Groq ships with its measured caps (200K tokens/day,
  1K requests/day); other providers are opt-in. Counters survive restarts
  (`~/.genesis/routing_budget_state.json`) and err toward undercounting, so
  a miscount can never lock a healthy provider out. Kill switch:
  `GENESIS_DAILY_BUDGET_DISABLED=1`.

- **Optional graph engine: module fetched, service unit written, nothing armed.**
  `bootstrap.sh` now downloads the FalkorDB engine module (one file under
  `~/.genesis/deps`, checked against a pinned digest and re-checked on every
  run) and writes a `genesis-falkordb` user unit that stays **disabled**: it
  listens on no TCP port, only a unix socket in `~/.genesis/falkordb`, and can
  write nothing in your home but its own data directory where the host allows
  user-unit sandboxing. Nothing on your system changes — the `redis-server`
  8.x the engine needs is a manual, opt-in step described in SETUP.md, "Graph
  engine (FalkorDB)". Once you select it (graphstore `mode: falkordb`) or
  enable its unit, the infrastructure health check alerts if it fails, stops
  (a start-only unit after a reboot), or loses its socket; an engine you never
  armed stays silent. A provision that cannot verify the module does not
  re-render an existing unit onto it.
  `GENESIS_FALKORDB_PROVISION_DISABLED=1` skips the module download.

- **Genesis now enumerates its own stranded work instead of trying to remember
  it.** Finished code sitting on a branch nobody pushed, a pushed branch with no
  pull request, edits left uncommitted in a worktree — all of it was invisible
  unless somebody happened to think of it. A detector now sweeps the repository
  on a schedule and keeps one row per standing condition, so the question "what
  fell through the cracks?" has an answer that was counted rather than recalled.

  Getting that count right is the whole problem. Because this project
  squash-merges, a branch that landed weeks ago still looks like it is ahead of
  the main line forever, so simply asking git turned up 145 candidates of which
  roughly 18 were real. Matching each branch against the project's pull-request
  history by NAME is what recovers the precision — but a name is a hint, not a
  receipt, and treating it as one hid real work. A pull request records the
  exact commit it merged, and the server can be asked what it actually holds;
  the detector now prefers those. If the recorded commit IS your local commit,
  or contains it, the work landed and no clock is consulted. If the server's
  copy of the branch differs from yours and yours is not simply older, then you
  are holding commits that exist nowhere else, and no pull request on that name
  covers them — however recently it merged, and whether it merged or was
  abandoned. Only when the commits themselves settle nothing do the merge and
  close timestamps get a say. Measured against this project's history, the
  older name-and-clock rule was hiding five branches whose work existed on no
  server, including finished fixes with tests; four of them sat behind a closed
  pull request and one behind an open one that was reviewing something else.

  The same mistake runs the other way too, and had to be closed from both ends.
  Looking a branch up by its NAME alone means a branch you renamed — or one you
  created locally to review somebody's pull request under a name of your own —
  matches nothing in the history, so the exact commit that would have proved it
  was already handled never gets consulted, and it is reported as stranded.
  Names change; the commit a pull request points at does not. Branches are now
  matched by both, which on this project's own history removed four such false
  reports out of twenty-six — every one of them a branch sitting on the exact
  commit of a real pull request. The same reasoning settles a smaller one: if
  you have the same branch open in two working copies at once, they are now two
  separate rows rather than one, so acknowledging the uncommitted work in one
  of them no longer silently speaks for the other.

  Coverage that cannot be established is now reported rather than assumed. If
  the merged commit is one this machine has never fetched, the branch is
  flagged as unconfirmed and carries the single command that settles it, on the
  principle that an extra row somebody dismisses costs less than a clean board
  that lied. Where the ambiguity runs the other way — the server's copy differs
  and there is no way to tell ahead from behind — the detector reports neither,
  leaving any existing row exactly as it was. Closed-but-unmerged pull requests
  are still read as deliberate abandonment and suppressed, but a decision to
  abandon covers the work that was IN the pull request, not commits made
  afterwards or never sent. Every run publishes its full arithmetic, including
  what it declined to judge.

  There is no list of branch-name prefixes to ignore. A branch that is meant to
  sit there — a backup, a scratch experiment — is acknowledged with a written
  reason, which leaves a record a person can read instead of a rule nobody can
  see. The acknowledgement is tied to the exact commit it was granted against,
  so it lapses by itself the moment new work lands on that branch — and only
  then. An earlier version let ordinary typing revoke one: editing a file
  inside an acknowledged working copy briefly pushed it under an age filter,
  and the reconciler read "not reported this time" as "gone". Not reporting
  something and it having gone away are now different things, which is also
  what stops a recurring finding from silently restarting its count.

  Three failure modes shaped the rest. A sweep that cannot see everything says
  so and changes nothing, rather than reporting the branches it failed to look
  at as resolved — a detector that manufactures a clean board is worse than no
  detector. A detector that quietly dies would go on answering "nothing"
  forever, so it reports its own liveness. And the subtler one: a detector that
  is alive but half-blind — say its access token expired — would keep its
  liveness green, keep its numbers, and never notice anything new again. That
  now raises its own alarm, separately from whatever it found, and every count
  it publishes says which parts of the picture the last look actually covered.

  Read the board with `zero_drop_status`, which reports how long ago it was
  compiled, because a stale zero is not a clean one. Off, quiet, or noisy is a
  setting (`zero_drop`: `off`, `observe`, `alert`). It ships on `observe`: the
  board fills and is readable, while the summary-alert lane waits on a
  refinement to how such alerts expire. A damaged setting falls back to
  `observe` rather than to `off`, on the principle that a silent detector is
  the more dangerous of the two.

- **The graph engine can now answer, and nothing routes to it yet.** A second
  memory-graph backend lands behind the existing seam: a long-lived FalkorDB
  server reached over a unix socket, with its own projection of the memory
  graph. **Reads do not move** — the selector in `config/graphstore.yaml`
  defaults to the in-process NetworkX store, and every unreadable or
  unrecognised value degrades to it as well. Moving reads onto the new engine is
  a deliberate one-line change; nothing moves them there by accident.

  What it buys, when you do move: NetworkX rebuilds its whole projection
  whenever any writer invalidates it, and the new store never rebuilds. It also
  re-checks whether a memory has expired on every read instead of once per
  rebuild, so a memory whose validity window closes becomes invisible
  immediately rather than waiting for the next write to trigger a rebuild.

  What it does not change: both stores hide exactly the same memories — 400 real
  traversals were replayed through both and disagreed on none of them, which is
  the point, since which backend answers must never change what you are shown.
  Betweenness centrality deliberately stays on NetworkX whichever backend is
  selected; the new engine cannot compute it, and quietly substituting a
  different measure would change which memories are protected from
  consolidation. If the engine is unreachable, traversals fall back to NetworkX
  and then to SQL rather than returning an empty answer that would read as "this
  memory has no connections".

  If you do arm the engine, build its projection first with `python -m
  genesis.memory.graphstore_project` — safe to re-run at any time, including
  while the engine is being read, because the new copy is built alongside the
  old one and swapped in atomically. Selecting the engine before building a
  projection is refused rather than silently answered, since a never-built
  projection makes every memory look like it has no connections and the answer
  alone cannot tell you which it is. A projection that was built and is
  genuinely empty — a fresh install, or one whose links were all pruned — is
  answered normally, not refused.

  **This update does change your dependencies.** The graph engine is part of the
  memory architecture rather than an optional add-on, so its client ships as a
  normal dependency — which also raises `redis` from 7.x to 8.x, because the
  client requires it. Genesis itself never connects to redis outside the graph
  engine; the version matters only because other libraries import it. Before
  shipping this, that upgrade was tested against the stack that does: the MCP
  server framework and its task/queue dependency import and run correctly under
  redis 8, and the full graph projection was built and queried on it. Nothing to
  do on your side beyond the usual update.

- **"What fell through the cracks?" now has one answer instead of five partial
  ones.** Genesis already detected work stranded in git — finished code on a
  branch nobody pushed, a pushed branch with no pull request, edits left
  uncommitted. Everything else it was carrying was counted somewhere else, or
  nowhere: open pull requests in one cache, agreements in a per-session ledger,
  deferred items in another store, things waiting on you in a third. Each
  surface was correct about its own slice and none of them was the answer, which
  is how something goes missing while every individual view looks fine.

  A single board now assembles all of it on demand — nothing new is stored, and
  every part is derived from a store that already owned the data. Read it on the
  new Zero-Drop tab in the dashboard, or as one line in the morning report.

  The rules it follows are the point, more than the counts themselves. A count
  of a population carries the denominator that makes it checkable: not "3
  stranded" but "3 open of 11 tracked". The one exception is the section for
  things waiting on you, where the count IS the population and an all-time
  total would size it against a number nobody acts on — and that exception is
  stated rather than left as an inconsistency. A part that could not be read
  says so rather than reporting nothing, and it degrades on its own: one
  unavailable store no longer costs you the ones that were readable, and the
  board says plainly that an unread part is not a zero. A source that has gone
  stale withholds its number entirely instead of showing an old one beside a
  caveat, because a figure printed next to a warning is still read as a figure —
  and if the page itself stops refreshing, it now says so instead of quietly
  showing you the last board that loaded.
  The stranded-work count always carries the detector's own age for the same
  reason: a zero from something that has not run in a week is not a clean board,
  and saying so is the difference between a report and a reassurance.

  One number it surfaces did not exist anywhere before. A ledger row is shown
  by its own session and by nothing else, so once that session ends its open
  rows are invisible to every process and person — the hourly sweep that chases
  them acts on a handful at a time but never counted the population it was
  drawing from. Now it is counted, across every session.

  The board also states what it does NOT cover, since a list of five sections
  reads as complete unless it says otherwise.

- **Follow-ups now record which session they came from.** The `source_session`
  column existed and repo-pulse read it to attribute completions — but no live
  producer wrote it: across the four sources that create follow-ups today, 513
  of 513 rows were NULL, so every annotation the repo-pulse worker made carried
  empty provenance. It now fills without guessing. The inbox evaluator and the
  task executor pass through the session id they already held and were
  dropping. The `follow_up_create` tool takes a `source_session` the calling
  session reads off its own per-turn tag: an 8-character prefix is resolved
  against every store that can hold a session id — including the one written
  before a new session has done anything else, which is exactly when a session
  first describes its own work — and a prefix that does not resolve to a single
  complete id is stored as NULL, and says so, rather than as a truncated or
  malformed one. A dispatched session, which never receives that per-turn tag
  and so had no way to know its own id, is now told it at session start.
  Callers with no session, like surplus ideation, keep an honest NULL.

- **The commit-message hook now warns about security-incident framing.** Public
  artifacts should describe what the code does, not assert in past tense that this
  system leaked or exposed something — a fix does not need a confessed incident to
  justify it. The hook already blocked private addresses and install identifiers in
  commit messages; it now also prints a warning (never a block) when a message's
  SUBJECT reads that way, at the point where rewriting is still free. This matters
  most for commit messages specifically: squash merges take their text from the
  branch commits, so that wording is what lands permanently, and a pushed public
  branch cannot be force-pushed to correct it. Scope and wording are measured
  against this repository's own history, and the test suite re-derives both the
  hit rate and the recall on every run so a later edit cannot quietly narrow it.

- **Career pipeline "bite" notifications (opt-in, off by default).** Genesis can now
  ping you on Telegram when a company advances into an engaged stage (in conversation /
  interviewing / offer) in your job-search pipeline — so you hear about momentum
  without watching the board. It only READS your pipeline and notifies you (no
  autonomous action, no sending), on an independent lever separate from the
  draft-staging driver. Ships OFF: enable it by pointing `data_module` at your
  career-agent module and setting `bite_relay_mode` to `observe` then `live`.

- **The development skill now covers why a PR's CI can be missing, red, or
  lying.** Four diagnosis traps that each produced a wrong conclusion in one
  session, added beside the existing conflict-suppression trap rather than
  duplicating it: mergeability reading `UNKNOWN` after any merge (and so
  suppressing CI queue-wide for a while); a PR opened against a non-default base
  never running CI at all, because the workflow filters on the base and
  retargeting emits no triggering event; `workflow_dispatch` completing without
  ever appearing in the PR's check rollup; and `gh run rerun` replaying the
  original merge commit, so it cannot clear a failure caused by a base that has
  since been fixed.

  Also recorded: `gh run view --log-failed` truncates on a large suite and shows
  only PASSED lines, so a failing job reads as a clean log; and the class where
  two individually-green pull requests break `main` together, because one adds a
  repo-wide invariant test and the other adds a violation of it, with no textual
  conflict between them and no signal until both have merged.

- **A verification keyed on existence can only confirm.** The skill's
  verify-outcomes guidance now names the shape directly: a check that would still
  pass if the thing were absent, stale, or the wrong object has proven nothing.
  Three worked examples, all of the same shape — waiting for a new artifact by
  testing that any exists, matching the first row rather than the intended one,
  and counting from a listing sorted by a key other than the one being counted.

- **A refused command now says that the WHOLE command was discarded, not just the
  step that was refused.** A blocking hook cancels the entire Bash call, so
  `cat > config.py <<'EOF' … EOF && git commit`, refused for the commit, also loses
  the write — and the message named only the commit, which reads as "the commit
  didn't happen" rather than "and the edit you just made never happened". Every
  guard that can refuse a Bash command now adds a line saying so, and approval
  prompts carry the same warning in the dialog, since declining skips every other
  step too.

  The note deliberately names nothing — not the files, not the steps. Working out
  which files a command would have written means deciding what each tool's flags
  do, a set with no closed boundary that produced fourteen review findings.
  Listing the parsed steps instead looks safer and is not: the shared parser
  splits on `|` as well as `&&`, and drops a plain redirect target, so the case
  this exists for renders as `cat / PORT=8080 / EOF` — no filename, and heredoc
  body lines presented as commands. Stating the one fact true of every block
  needs no knowledge of any tool and cannot go stale.

  It appears on any command with more than one step, which is most of them, and
  is load-bearing on the subset that actually lost a write. Separating those
  needs the tool-semantics guesswork above, so the ubiquity is the price of the
  note being unable to mislead.

- **The development guide now names three ways a test can look rigorous and
  prove nothing.** A test that fails before the fix is the standard evidence
  that it caught something, but the failure has to come from the assertion that
  describes the defect — when it comes from setup instead, what it proves is
  that the test never built the situation it was written for. Related: a test
  of the seam between two components has to run both of them, because a value
  typed by hand in the middle is exactly where the defect hides, and a copy of
  the fix pasted into the test grades the copy rather than the shipped code.
  The guide also records that two reviewers pushing opposite values for the
  same field position a review apart is a signal that the code depends on
  something that is not fixed, not an invitation to pick a side.

- **Genesis now remembers which merged PRs still need their end-to-end check.**
  "Verify this after it merges" used to live in whoever merged it — measured, one
  of two owner-directed merges had its E2E forgotten until someone asked, and both
  found real problems once run. Every merged PR now gets a durable row, and a
  docs-only change closes its own row automatically with the reason recorded, so
  the list holds real work rather than noise. Documentation and prompt/skill edits
  are exempted by a fixed path rule, never by a model deciding case-by-case. See
  what is outstanding with
  `python scripts/repo_pulse_worker.py --verification-backlog`; turn the whole
  thing off with `verification_enabled: false` in `config/repo_pulse.yaml`.
  Recording starts from the merges the pulse worker sees after the upgrade; it
  does not reach back over history already behind the worker's cursor. Verified
  against a scratch copy of this repo's real merge history — 105 of 105 merges
  recorded, 21 auto-exempted as documentation — which is a check of the
  behaviour, not a description of rows your install will have.

- **The desktop-takeover authorization gate, shipped before anything can call
  it.** `autonomy/desktop_gate.py` is the deterministic check that would stand
  between Genesis and your own keyboard, mouse and screen. Nothing calls it: the
  actuator is inert and the loop lands later. An actuator with a caller and no
  gate *is* the ungated capability, so the gate goes first and alone.

  It decides from what the actuator RESOLVED — the operation, the key chord, the
  target window's handle, and the element name, control type and `IsPassword`
  the accessibility tree reported — never from the acting model's prose about
  its own intent. Consent inferred from a model's self-report is the weakness
  the whole gate exists to avoid. A call that omits any of that is REFUSED with
  a reason naming the missing field, rather than held: a hold would ask you to
  approve an action nobody can describe, and a missing field is a caller defect,
  not a judgement to delegate.

  Authority is per SESSION, MISSION and WINDOW, and every field it carries is
  compared. The window is identified by `(handle, process id)` rather than by
  title, because two browser tabs are both called "New Tab" and a title changes
  when the document inside it does — so a title bar authorises the wrong window
  while looking strict, and stops matching the right one. The mission binds an
  id, never the mission text, which is model-generated and gets rewritten on
  every re-plan. A grant must also be a grant and not a hold (both are rows of
  the same action type, so a `kind` marker is what stops approving one held
  click from handing over the session), must be approved and unconsumed, must
  carry a wire format this version knows, must have been resolved through a
  channel whose messages originate outside this box — Telegram or voice,
  deliberately narrower than `classify_resolver`'s "human" class, because
  `dashboard` is stamped by a route any local process can reach with the
  internal bearer token and `user` is merely a default — and must be unexpired
  against a config TTL bounded in both directions, so a backwards clock step
  cannot mint a permanent grant.

  Risk comes from four independent signals combined highest-wins, because the
  ways an action can be dangerous are not the same shape. Labels are one of
  them: "Send", "Delete", "Sign up" cross the identity bar. But `Pay` and
  `Remove account` are on no desktop label list while the reversibility
  classifier has always called them irreversible, so that verdict now counts
  too. A drag acts on geometry and no label describes its effect, so it holds. A
  key chord is checked against an allowlist of chords that only navigate or edit
  text — `ctrl+enter` sends the mail in most clients and appears on no denylist
  anyone finished writing, so an unrecognised chord holds. That polarity is the
  point: a denylist's misses on a safety boundary are vulnerabilities, an
  allowlist's are one hold apiece.

  Ordinary work stays ordinary, which is a safety property and not a
  convenience. Typing "please delete the old draft" into a text editor is not an
  irreversible action, clicking in a window called "Messages" is not sending
  one, and a commit hash is not a bank transfer — money is matched by card, IBAN
  and SSN *shapes* as well as labels, with the IBAN validated by its ISO 7064
  checksum rather than its shape, because a country-code pattern alone held
  roughly one git SHA in every hundred and ten. A gate that stops the most
  ordinary thing you do teaches you to wave it through.

  Secret fields are outside all of it: `IsPassword`, or a target merely *named*
  like one, is a refusal with no approval path — custom controls routinely do
  not set the flag, and the list covers the family (OTP, one-time and
  verification codes, 2FA, recovery keys, passkeys, security questions, SSNs).
  That match is scoped to the target alone, so a window called "Password
  Manager" does not make its controls unreachable. "Pin" gets its own rule
  rather than a bare keyword, because a case-insensitive match made "Pin to
  taskbar" a secret field — and a secret-field match is a refusal with no way
  to proceed, so a false one is the most obstructive mistake this gate can
  make.

  That "nothing types into a password box" is an absolute, and keeping it true
  is why paste is not on the inert-chord list. A keypress has no resolved
  target, so `IsPassword` is always false for one and the refusal has nothing
  to match against; with `tab` inert and `ctrl+v` inert, the loop could move
  focus into a credential box and paste into it. Copy, undo and backspace stay,
  because none of them can put chosen content anywhere.

  The approval a person reads is newline-delimited, one field per line, rather
  than quoting the window and control inline. Screen text cannot contain a
  newline by the time it reaches the card, but it can contain an apostrophe: an
  element named `Cancel' in 'Notepad` produced a card that read as well-formed
  and named the wrong window. Structure closes that; stripping quotes would
  have corrupted every legitimate title.

  Screen-supplied text is treated as hostile where it reaches a person: window
  titles and element names pass through the canonical control-character strip
  and a declared length bound before they reach the approval you read, so a
  malicious window cannot forge extra lines, reorder the text with bidi
  overrides, or conceal part of it — while the full value is kept verbatim in
  the row's context.

  `shadow` — the shipped default — now reports the verdict the live path
  computes, from the same function, instead of recomputing it by hand. A shadow
  that silently disagrees with live is worse than no shadow, because shadow is
  the mode installs actually run and the only thing telling you what live would
  do.

  Arming needs both `mode: live` and `live_opt_in: true` in
  `config/desktop_takeover.yaml` (default `shadow`; env kill
  `GENESIS_DESKTOP_TAKEOVER_DISABLED`, checked before any config read, and now
  honouring `true`/`yes`/`on` rather than only the literal `1` — it previously
  ignored every spelling but one, silently, on the control documented as the
  one thing config damage cannot get around). Both time windows have an upper
  bound as well as a lower one: the lever is described as bounded and was
  bounded on one side, so a mistyped `grant_ttl_minutes: 30000` was a 20-day
  grant. Anything past the maximum falls back to the default rather than
  clamping to the ceiling, because every degradation here moves toward less
  authority. And the
  domain is deliberately absent from the settings MCP so arming is a conscious
  file edit rather than one unconfirmed API call. If your local overlay is
  present but unparseable the capability goes to `off` rather than falling back
  to defaults, because the base ships `enabled: true` and your disable can only
  live in the overlay — degrading it would quietly re-enable something you
  switched off. The capability cell can permanently DENY but can never GRANT, so
  no amount of banked evidence converts session consent into standing autonomy.
  Desktop rows are withheld from the generic dashboard approvals queue and from
  the unified comms feed, refused by the per-item resolve path, excluded from
  `approve_all_pending`, and sit outside the voice-gated types: neither a batch
  "approve all" tap nor a bare spoken "approve" aimed at something else can hand
  over the machine. That set is maintained by enumeration rather than by an
  allowlist, so it was checked by listing every reader of the approvals table —
  the comms feed was the one that had been missed.

  Three limits stated plainly rather than left to be discovered. The grant
  predicate is not a complete authorization boundary — the database is writable
  by the uid every Genesis process runs as, so the allowlist closes the
  app-layer path and nothing more. The classifier raises the bar on ordinary
  software but is not a defence against adversarial UI, since a hostile page
  names its own controls. And approving a held action currently authorizes
  nothing: the row is recorded, no surface can resolve it yet, and what an
  approval should buy is a new authorization surface that lands with the consent
  path in a later change.

- Branch protection now lives in the repository as reviewable files. Two rulesets are declared in `.github/rulesets/` and applied by `scripts/apply_rulesets.py` (`--dry-run` shows the diff first): one keeps the maintainer bypass so the approval rule stays usable on a single-maintainer repo, and a second — with no bypass at all — makes `test`, `leak-detector` and `lint` bind every merge. Previously a single bypass entry made every server-side rule advisory for the account doing the merging, so a required check could not actually stop anything.

- **A queued message can now be cancelled before it sends.** Scheduling a
  reminder or notification for later used to be one-way: once queued, the only
  states the message could reach were "sent" or "sent". Changing your mind meant
  either letting a stale message arrive and sending a correction after it, or
  marking the original as delivered — which writes a record saying you were told
  something you never were, and a later session reads that record and believes
  it. Cancelling is now its own state, so a retracted message is recorded as
  retracted rather than disguised as delivered. Two new tools: one lists what is
  currently queued and when each item is due, the other cancels one by id. A
  cancel that finds nothing to cancel — an unknown id, a message already sent,
  or one cancelled a moment ago — says so plainly instead of reporting success.

- The cross-model reviewer is now asked whether the approach is right, not only whether the code is. It was previously told to hunt defects and nothing else, so a solution that was wrong in shape produced round after round of real findings with nobody ever saying why they kept coming.

- **Superseding a memory is now its own operation.** Until now the only way to
  mark a memory as corrected was to pass `supersedes` to `memory_store`, which
  meant the deprecation could only happen as a side effect of writing new
  content. There was no way to say "this memory replaces that one" about two
  memories that already existed, and a supersede that failed could not simply be
  retried -- retrying meant re-sending the content too.

  `memory_supersede(old_id, new_id)` does just the one thing. Both ids accept
  the same short handles as the rest of the system, and because the caller names
  both, both can be checked before anything is written: a handle naming no
  memory, an ambiguous prefix, a memory asked to replace itself, or a successor
  that is itself deprecated are all rejected with nothing changed. A failed call
  is a no-op that is safe to retry.

  `memory_store(supersedes=...)` keeps working and is still the right call when
  the correction is new content.

- The daily disk-hygiene run now ages out the guard replay corpus cache
  (`~/.genesis/output/guard-corpus.jsonl`) and any temp left by an interrupted
  rebuild after 45 days. The cache is regenerable and holds verbatim command
  lines from your own sessions, so it no longer lives forever by default.

- **The library that will record which live session is using which worktree
  lands here. Nothing calls it yet.** With many sessions sharing one repository,
  nothing says who is where, so the daily cleanup can archive a worktree a
  session is still working in — leaving that session broken and its unsaved work
  recoverable only by hand. The fix records ownership using git's own "this
  worktree is in use" marker, which the cleanup job already respects. This
  release ships only the piece that knows how to write and read that marker: the
  hook that takes a claim on first edit, and the cleanup-side release, follow
  separately, and until they land nothing behaves differently. Recording the
  fact is the whole protection when it arrives; nothing enforces anything.
- **Both signals the cleanup job had for "is anyone using this" turned out not to
  work, and that is why this exists.** The first looked for a session whose
  working directory was inside the worktree; sessions change directory per
  command, so their working directory never leaves the main checkout — measured
  as none of two hundred worktrees, while seven sessions were running. The second
  asked how recently anything in the worktree changed, but only looked at the top
  two levels of directories, and editing a file updates only that file. A
  worktree whose source had just been edited still reported nineteen days idle
  and eligible for cleanup; git could see the edit, the check could not. Nearly
  all of this project's source sits below the level that check looks at, so this
  was the ordinary case rather than an edge one.
- **Exactly one thing is recorded: which live process is using the worktree.**
  Whether there is unsaved work is deliberately NOT recorded — the cleanup job
  can determine that itself, at the moment it decides, and does. Writing it down
  in advance would mean checking it in one place and acting on it somewhere else,
  and everything that had to be built to keep those two in agreement turned out
  to be where the problems came from.
- **A marker left by something else is reported, never touched.** A hand-written
  one, or one belonging to another tool, is left exactly as found, and ours
  carries an identifier specific enough that ordinary notes cannot be mistaken
  for it. Claims are tied to a process and to when that process started, so a
  recycled process number cannot be mistaken for the original owner, and a claim
  that cannot be given a condition for lifting is never written at all.

- Shared one evidence-driven web research method across foreground researchers,
  research-profile background sessions, autonomous blocker investigation, and
  tool-less autonomous steps assigned the legacy research skill.
- Added public-repository-only GitHub.com search and source-inspection tools for
  background research through a fixed unauthenticated endpoint, with explicit API
  failures, pagination, bounded trees, UTF-8 file reads up to 8 MiB, and truncation
  reporting.

- **A git command that rewinds the whole worktree now says so, instead of being
  noted as an ordinary discard.** `git checkout <commit> -- .` and its relatives
  rewrite every tracked path under the pathspec, so anything merged since that
  commit is reverted — and because the revert lands in your own working tree it
  appears inside your own diff, looking deliberate. There is no natural point at
  which anyone notices. On one install two merged pull requests were reverted in a
  single operation; the first was found by luck and the second only by a separate,
  later check. The guard already snapshotted the worktree for these verbs, but its
  note said a snapshot exists, which is not the same as saying what the command
  does. It now names the operation, the repository, and the conflict-aware
  alternatives (`git merge --squash`, `git cherry-pick`, `git apply --3way`) that
  fail loudly where this one succeeds silently.

- **The recovery instructions in that note are now the ones that actually
  recover.** Two earlier versions each offered a single command, and each lost
  work. `git stash apply --index <sha>` was dropped on the belief that it writes
  conflict markers into the file being rescued; re-measured, it exits 1 and leaves
  the file untouched — a safe refusal, not corruption — and on a freshly rewound
  tree it restores everything with the staged/unstaged split intact. Its
  replacement, `git checkout <sha> -- .`, is the one that silently loses work:
  path checkout is overlay-mode by default, so a file your work had DELETED is
  never removed again and the rewind's copy survives to be committed. The note now
  gives the complete form first, the robust `--no-overlay` fallback second, and
  states the fallback's cost — it lands everything staged. Each claim was checked
  by running the command against a repository carrying a staged change, an
  unstaged change, a tracked deletion and a dirty file outside the pathspec.

- **The advisory can no longer be lost by being too long.** Claude Code persists a
  hook payload over its size cap and hands the model a short preview instead, so
  an oversized advisory is an absent one, silently. One rewind note is about 1.3 KB
  and the payload crossed the cap at eight repositories — precisely the
  multi-repository compound the per-repository note was added to serve. The bound
  drops whole notes and says how many it dropped, rather than trimming the text:
  every note ends in a recovery command carrying a snapshot id, and a cut value
  would hand someone a truncated id dressed as a recovery instruction.

- **Rewind events are counted whether or not a snapshot was possible.** Whether
  this note should ever become a hard block is a question about how often the
  situation arises, so each event is recorded in the snapshot log. The flag used
  to ride on the snapshot row, which made the count a measure of how often
  `git stash create` happened to produce an id: a rewind from a clean worktree
  wrote no row at all and went uncounted — and a clean worktree is the likeliest
  setting for the incident being measured, where you have no local edits of your
  own and the rewind reverts someone else's merged work. Rows stay metadata only:
  no command text and no commit id, because the log is durable and a command line
  can carry credentials.

- **The detection no longer misreads git's own option grammar.** It matched any
  argument that happened to equal a verb name, so `git add checkout <file> .` —
  three filenames — reported a rewind and wrote false evidence into the log that
  would justify escalating to a block. It skipped every dash-prefixed token, so
  `git checkout - -- .` (the previously checked-out branch, a real reversion) went
  unwarned while an option's *value* was named as the commit being restored. And
  it treated index-only commands as worktree rewrites: measured against real git,
  `git read-tree --reset` and `git restore --staged` touch only the index, while
  `-u` and `--worktree` respectively are what reach the working tree.

- A `gh` listing that is silently capped now says so before it runs. `gh pr list` returns at most 30 items by default, `run list` 20, `workflow list` 50 — so an unflagged listing is already a bounded read with nothing in the command to show it, which is how a count drawn from one can be wrong without looking wrong. It also fires when an explicit `--limit` merely restates the default and therefore changed nothing, warns about every distinct cap in a compound command rather than only the first, and says so when it could not parse the command well enough to check. For `gh search` it names the API's 1,000-result ceiling instead of telling you to raise a limit gh will refuse. Advisory only: never blocks, fires once per listing kind per session, and stays quiet for `--help`, `--web`, and any limit you passed that actually narrows or widens the read.

- Codex can use a reviewed set of Genesis health and recall tools as an external MCP client without joining Genesis session tracking.

- **A vendor stylesheet's layout rule with no Genesis answer now fails a test
  instead of shipping.** Every page loads an inherited sheet before any Genesis
  one, and two of its layout rules land on selectors Genesis also uses. Neither
  errors, neither logs, and a page that renders wrong renders wrong quietly: one
  of them made a page unscrollable and was fixed by copying three declarations
  into each page that hit it, so the page that never hit it never got a copy;
  the other laid every panel's header down the left side of its panel as a
  narrow vertical gutter and went unnoticed for months. Both were found by
  someone looking at a page, which is the one check with no harness.

  The guard asserts, per page, that any layout property an inherited sheet sets
  on a shared selector is answered by a Genesis rule on the same selector, at the
  top level, in a sheet that loads later — and, across pages, that an answer
  present on one page is present on all of them, which is what the second
  incident actually was. Polarity is allowlist: a denylist of known-bad selectors
  would have passed cleanly for the entire time the panel rule was broken,
  because nobody knew to add it.

- **It compares selectors for identity rather than modelling the cascade, and
  that is a retreat from two models that were each attacked successfully.**
  Matching a bare class against a descendant form needed a rule for which one
  wins, so specificity was compared; specificity answers who wins where both
  rules apply and says nothing about where, so a hover-scoped answer was accepted
  while every element nobody was pointing at still took the vendor declaration; a
  coverage test was added on top of it.

  Both were estimates. The specificity estimate over-counted in five
  separately-constructed selector shapes, two of which accepted a live leak end
  to end. The coverage test compared flat token sets, which cannot see position
  or multiplicity, so a descendant, a child, a sibling and an id-qualified form
  of the same class all read as covering it — substituting the first of those for
  the shipped answer reintroduced the original incident with 26 tests green.

  Four consecutive reviews each found the model fail-open in a new shape, and the
  shapes had nothing in common except that a person had to think of them. An
  answer must now be written on the vendor's own selector, normalised for
  whitespace and group order. Two rules with the same selector apply to the same
  elements at the same specificity, so neither question needs estimating, and
  what remains — which sheet loads later, which declaration is important — are
  facts rather than approximations.

  The cost is stated rather than discovered: a genuinely covering answer written
  any other way now reads as unanswered. That is a false alarm, and the remedy is
  a table row naming the reason. The failure it replaces was silence.

- **An unresolvable stylesheet link now fails the guard instead of disarming
  it.** A link whose href named no file under the web root was dropped; with none
  left the page had no owned sheet, and the test skipped. One cache-busting query
  string on one link therefore turned the whole check off for that page and the
  run reported a skip and exited zero. Query strings and fragments are stripped,
  so an ordinary cache-buster resolves; anything still unresolvable is reported.

- **A stylesheet this repository does not ship makes a page unreadable, and the
  guard now says so instead of standing down.** A link to another origin is
  rightly excused from the resolvability check — it is not ours to ship — and it
  then failed to resolve too, so it fell out of both the owned and the vendor
  list and the page skipped by a second route. Moving one page's links to a
  content network gave a skip and exit zero on a page whose rules had not
  changed. Not ours to READ is not the same as clean.

- **A clearance is for a declaration, never for a slot.** The table of vendor
  declarations that need no answer was keyed on selector and property, while the
  comment beside it said, correctly, that the same property at a different value
  would be a real leak and must still fail. Changing the vendor's `width: 100%`
  on the page root to `250px` was cleared by a row written for the other number,
  with every test green. Rows now carry the value they excuse.

- **Type selectors are matched case-insensitively, as HTML defines them.** Keys
  and comparisons were case-sensitive, so rewriting the vendor's own page-root
  rule in capitals left the whole viewport lock in place with the suite green —
  one of the two incidents this guard exists for, reproduced with the shift key.
  Class and id names stay case-sensitive, which is why the fold walks tokens
  rather than lowercasing the text. Whitespace around a combinator is now
  insignificant too, so a minified vendor rule and a hand-written answer compare
  equal.

- **Shapes the reader cannot see are refused rather than dropped.** A rule nested
  inside another, and any block at-rule not on the descend list, previously
  vanished with everything in them; both now raise and name the remedy. The
  property set gained the near neighbours it was missing — a clip set through
  `max-height` passed while `height` was watched — and a stylesheet that parses
  to zero rules is reported instead of reading as a sheet with no leaks.

- **The last approximation is gone, and it was never in the comparison.** Six
  rounds of review each found this guard failing open in a new shape, and the
  pattern only became legible when the findings were bucketed by pipeline stage:
  none of them landed on the selector comparison the previous round introduced.
  They landed one stage earlier, on the "key" — an estimate of which elements a
  selector could reach, derived by reducing each selector to its last compound,
  its classes, or a bare element name. That estimate ran BEFORE the comparison
  and decided whether a rule was looked at, so its errors were silent by
  construction. Measured against the shipped vendor sheet, thirty of its
  hundred and seventy-five layout declarations — seventeen per cent — derived no
  key at all and were invisible to every check here, an identifier-selected panel
  rule among them.

  Since the comparison already works on normalised selector text, the key can be
  that text. One stage instead of two, no approximation left anywhere, and the
  last-compound split, the class-name pattern, the bare-element rule and the
  parenthesis stripper are deleted rather than fixed. Measured after: zero
  invisible declarations, zero new findings on the real stylesheets, both
  original incidents still caught. The round count had been tracking remaining
  estimating stages, not remaining bugs.

- **What the guard cannot read is now refused rather than skipped.** A rule
  nested inside another was refused already; a conditional nested inside a rule
  was not, so it walked through the refusal added for exactly that shape. Joining
  it: a cascade layer carrying a layout declaration, whose precedence outranks
  everything compared here; a local import, which pulls in a stylesheet nobody
  parses, while the font-service import the vendor sheet opens with is allowed
  through by name; a stylesheet link under a media condition, whose declarations
  answer nothing outside it; and a vendor layout rule whose selector embeds
  another selector, where a reader would reasonably expect an answer on the
  embedded one to count and identity cannot say so. The single real instance of
  that last shape carries a row explaining why it is inert.

- **Inline style blocks are read, having been documented as invisible.** Four of
  the six pages declare layout properties in a `<style>` block — forty-seven
  selectors' worth on one of them — and a Genesis layout claim the guard cannot
  see makes the vendor declaration it answers read as unclaimed, so the selector
  was skipped rather than checked. Blocks are now collected alongside links, in
  document order, and their position is read rather than assumed.

- **The login page is in scope, at the third attempt.** It is built as a string in
  Python rather than as a template, so every check written against a glob of the
  template directory has omitted it: once when the viewport fix was copied per
  page and it never got a copy, once while that very omission was being fixed,
  and once more when this guard was first written. The enumeration now lives in
  one place that both test files import, with the reason attached — a rule every
  call site has to remember is a rule the next call site will not.

- **The redesign deleted every estimate about how the inputs RELATE and kept
  every estimate about what the inputs ARE.** That is the sentence the last six
  review rounds were circling. Specificity and coverage — estimates about whether
  two selectors reach the same elements — were deleted and replaced by identity.
  Four other estimates were left standing, all upstream of the comparison, all
  deciding whether a rule was ever looked at: which pages exist, which sheets the
  browser actually applies, which properties count as layout, and which selector
  shapes are unreadable. Nine measured members, four of them live fail-opens
  demonstrated against the shipped stylesheets.

  Each is now closed the way the at-rule walk already worked — an allowlist with
  a refusal on the tail, so a shape invented next year fails loudly instead of
  vanishing.

- **The page population is derived rather than listed.** The previous fix
  replaced a directory glob with an explicit list of two page sources, which has
  the glob's defect and more entries: it omits silently. A review then found a
  third page — a terminal view serving a vendor stylesheet and declaring
  `overflow: hidden` on the document root, the first incident's exact shape — and
  a search of the tree found a fourth emitter besides. Emitters are now
  discovered mechanically and each must be mapped to the page it serves or
  exempted with a stated reason; an unmapped one fails, and so does a mapping for
  a module that has stopped emitting.

- **One function now answers whether a stylesheet is applied at all.** It was
  answered in four places from four partial rules and not at all in three more.
  Marking the sheet that carries the panel answer as an alternate stylesheet left
  every test green; so did marking it disabled; so did putting a media condition
  on an inline block rather than on a link. The same function is asked of a
  `<style>` element and a `<link>`, and an unrecognised condition raises rather
  than being guessed at.

- **Refusals that fired only in the cases nobody reaches now fire
  unconditionally.** The external-stylesheet refusal ran only when a page had no
  readable local pair, so adding an unread content-network layout sheet to any
  real page sidestepped it entirely. The import exemption admitted any absolute
  URL rather than the font service it was written for, so a content-network
  import was dropped whole. Both measured green before, both refused now.

- **Three smaller members, each measured.** Ownership is classified from the
  resolved path, because a link spelled with a dot segment resolves to the vendor
  sheet while a prefix check called it ours. A stray brace is not reported as a
  parse error — the parser folds it into the next rule's selector, so that rule
  keys under the wrong name and its leaks read as absent; preludes that did not
  parse are refused, and the check reads the token type the parser actually
  produces rather than the one it seemed like it should. And `@namespace` is
  refused outright, since prefixes are per-sheet and identical selector text in
  two sheets can target different elements.

- **The layout-property set gained the shorthands of what it already watched,
  and an honest statement of what it is.** A flex shorthand bypassed the
  longhand; an inset shorthand bypassed position. Those are closed at no cost —
  zero new findings on the real stylesheets. The set is still a deliberate
  sample: 136 non-custom properties in the shipped sheets sit outside it, of
  which roughly 69 plausibly affect layout, and watching those would flood the
  guard into uselessness. That the line is a judgement rather than a boundary is
  now said rather than implied.

- **The refusal list in the module docstring was four claims larger than the
  code.** A reader trusts that list instead of re-checking, which is the same
  mechanism that let the second incident ship — a page that obviously had a copy.
  Every entry is now pinned by a test that constructs the shape it names.

- Measured both directions throughout: fifteen of fifteen, nine constructed leaks caught and six correct trees left alone, plus every replay from every previous round still firing. The collected-test floor is re-derived again.

- **Genesis can now be the brain behind a desktop assistant.** Any tool that
  speaks OpenAI chat-completions to a configurable `base_url` can be pointed at
  `POST /v1/desk/chat/completions` instead of a model vendor. Every turn then
  routes through Genesis's router, so provider choice, cost tracking, circuit
  breakers and observability are Genesis's — and the desktop holds no model
  credential of its own. Authenticate with `GENESIS_DESK_TOKEN`, which opens
  this route and nothing else; the broad `GENESIS_MCP_HTTP_TOKEN` also works
  here, but it opens every other `/v1` route too, so it is the wrong credential
  to hand a desktop client.

  **Two lanes, because the turns are not the same shape.** `desk_primary` leads
  with a capable model: a client that fires its own tools by emitting an exact
  control tag needs instruction-following, since a near-miss silently does
  nothing. `desk_fast` leads with a fast one, for replies composed while a
  call is live. Select with the `X-Genesis-Lane` header, or with a `-fast` /
  `-primary` suffix on `model` where a proxy strips unknown headers; anything
  unrecognised resolves to the capable lane, because being quietly served from
  the fast one is the harder failure to notice. Both chains are configurable in
  `config/model_routing.yaml`.

  Honest about what it is not. It performs no memory recall — a client that
  builds its own system prompt should reach Genesis memory through an explicit
  tool, not by injection, or two context builders end up fighting over one
  prompt. It refuses streaming, tool-call turns, and image content: the routing
  layer is text-only, and answering an image question from the text alone would
  describe something never seen. Every refusal is an error object with no
  `choices` key, so a client parsing the completion raises rather than speaking
  the error aloud as if it were the answer.

  A provider that answers with no content at all is one of those refusals, not
  an empty reply passed through: the endpoint returns 502 rather than handing
  back a blank turn for the desktop to speak as silence. Note the limitation
  that sits behind it — the router stops its fallback walk on the first
  success, and a blank answer counts as one, so the later links in the chain
  are not tried. Fixing that belongs to the router and every caller it serves
  rather than to this endpoint, and is tracked separately.

- **Every `/v1` route is now checked for authentication by a test, not a
  convention.** The suite enumerates them from the live route map and asserts
  each refuses an anonymous caller, so a surface added later is covered without
  anyone remembering to add it. A route that should be public has to be named,
  with a reason, where a reviewer will see it.

- **A pull request's branch is now deleted when the request closes without
  merging.** Merged requests already cleaned up after themselves; closed ones
  did not, and every abandoned or superseded request stranded its branch on the
  public repository. Measured before this change: thirty of the hundred-odd
  branches on the remote were exactly such leftovers, and because a squash
  merge severs ancestry, those leftovers are indistinguishable from unmerged
  work — which is how stale branches were later mistaken for pending changes
  and grew duplicate pull requests for work that had already landed. Deletion
  is recoverable (the closed request keeps a restore button), skips branches
  another open request still uses, and can be switched off in the Actions UI.

- **The push gate now enforces the publish rule it could only document.** A
  prompt, not a block. A re-push to a branch that is public but has no open
  pull request no longer slides through on its first-push approval: the gate
  asks, and says why — such a branch runs no CI and no leak scan at all, which
  is the state the publish rule exists to prevent. The lookup fails toward the
  previous behaviour: a question it cannot answer changes no verdict and
  manufactures no prompt, and a dry run stays silent because it publishes
  nothing.

- **Plan documents carry a structured header.** A plan that outlives one
  session now opens with YAML frontmatter naming its status, the `origin/main`
  it was written against, what adopting it commits the project to (`binds`),
  what it forecloses (`prevents`), and the decision/ledger/issue ids it
  executes — plus a `## ═══ SUPERSEDED BELOW ═══` divider separating live
  content from archaeology. Three failure modes motivate it: three things
  enumerate the plan directory but all key on filename and mtime, so a
  commitment living only in a plan file is tracked by nothing; a long-running
  plan accretes with no signal for which part is current (measured on one
  install: 2,501 of 4,488 lines below the divider); and the line numbers and
  PR heads a plan cites go quietly false. `pinned.main` turns the last one
  into a one-command read. The header is written by hand and nothing validates
  it yet — the upstream linter it borrows vocabulary from checks body
  structures this header does not emit, so a checker is a separate later
  change.

- **Every hook that can fire on Bash must now declare its degrade direction, and
  the size of the refusal is computed rather than written down.** A new allowlist
  gate parses `.claude/settings.json`, runs each such hook against a poisoned
  `hook_input`, and asserts that it either carries a degraded handler (emits
  `GUARD DEGRADED`, exits 2) or is NAMED — in `_ADVISORY_BY_DESIGN` with words
  verified verbatim against its own docstring, or in `_NOT_PYTHON_ON_BASH` because
  it never imports the module. There is no third bucket, so a guard wired later
  that forgets its handler fails by construction instead of quietly ceasing to
  guard — an omission invisible in production, because Claude Code treats a non-2
  exit as a non-blocking error when the hook emits no `permissionDecision`, so a
  guard dying on its import traceback exits 1, the command runs, and nothing
  distinguishes it from a guard that looked and approved.

  Motivation: that figure was wrong three times in review and then went stale a
  fourth time with nobody being wrong at all, when another change wired one more
  Bash hook. Four hand-written copies existed across the repo; all four are now
  gone, replaced by a pointer to the derived gate.

  **The population filter, not the assertion, is where this kind of gate fails.**
  The first version compared `matcher != "Bash"` — an exact-string test against a
  field Claude Code treats as a regex — and discarded any command its narrow
  pattern could not parse. An adversarial audit broke it four ways against mutated
  copies of the real settings: alternation and wildcard matchers were invisible,
  and an existing blocker respelled as a bare `python3 …/guard.py` silently left
  the population with the suite still green. One in-repo hook was already outside
  the gate for that reason. The enumerator now evaluates the matcher as a regex
  and makes an unresolvable command a failing row rather than a skipped one, the
  shape `test_hook_output_contract.py::_resolve` already used. All four evasion
  spellings, a collapsed enumeration, and an invented allowlist justification were
  each confirmed to turn the suite red.

  The rule itself was derived by execution rather than assumed: the first
  hypothesis — that a hook importing `hook_input` blocks — was refuted, since all
  of them import it at module scope, including every non-blocker. A docstring
  keyword scan was tried as the discriminator and rejected, because "advisory"
  also appears in three hooks that do refuse. Two limits are stated rather than
  assumed away: the enumeration is scoped to the repo settings, so a user-level
  settings file can wire more on the same matcher invisibly, and shell hooks on
  the matcher never import the module and so are exempted by name rather than
  left unseen.

- **The Guardian can now see the agent tooling die.** A new host-side side-watch
  (`guardian/guard_layer_watch.py`, wired into `run_check`) probes whether the CC
  hook layer can still EVALUATE: the `genesis-hook` launcher runs end to end, the
  container venv interpreter runs, `hook_input` and `shell_parse` import, container
  `node` runs, and the host's own configured Claude Code binary starts. That last
  one probes the CONSUMER `diagnosis.py` actually launches rather than
  `node --version`, so a PATH failure is a true positive rather than a false one —
  host Node is frequently nvm-managed and a systemd timer's PATH is minimal.

  Two failure polarities motivate it, and the quiet one is the reason it exists.
  An unimportable `hook_input` fails CLOSED: every non-advisory Bash guard refuses,
  loud and unmissable. A dead launcher or venv makes `genesis-hook` exit non-2,
  which Claude Code treats as a non-blocking error, so every security guard is
  silently off while Bash keeps working and nothing on the install would report it.

  It must be host-side: a broken guard layer bricks CC sessions, and the
  container-side Sentinel is itself a CC call site, so it would dispatch into the
  same broken tooling — reasoning the repo already encodes independently in
  `sentinel/remediation_map.py`'s `UNMAPPED_BY_DESIGN`. It is deliberately NOT a
  `probe_*` in `collect_all_signals`: `SignalResult` carries no severity, so every
  probe there feeds the confirmation ladder into `RecoveryEngine.execute`, and a
  broken hook file must never be able to trigger a container restart. Alerts reach
  Telegram over stdlib urllib straight from the host, so they survive a fully dead
  container.

  **Alert-only, by decision rather than omission.** An earlier draft carried one
  automatic repair verb. An adversarial audit reproduced two ways it destroyed
  work — `git checkout HEAD -- <file>` overwrites the index, so staged content is
  lost and recoverable only via `git fsck`, and mid-merge the same command clears
  the conflict stages and silently resolves to ours while `MERGE_HEAD` remains, so
  the next commit drops the other side with no marker — and the probe's own
  dirty/clean signal failed OPEN, because `git diff --quiet` is tri-state and both
  error codes read as "differs", the value that authorised the write. The verb was
  removed rather than patched, and its config knobs are absent rather than
  defaulted off, since a disabled destructive path is still one config edit from
  running. A test asserts the module defines no repair function and that no
  executed payload carries a mutating git verb.

  Two further audit findings are fixed in the detection half: an episode that
  never reached a warning is now cleared when the condition goes healthy, rather
  than stranding state whose stale start timestamp silently expired every
  elapsed-time window on the next occurrence; and a condition whose probe returns
  INCONCLUSIVE is skipped rather than falling through the healthy branch, which
  had emitted a false "recovered" and reset a ladder that was still climbing. The
  host probe now uses the house `kill_process_group`/`reap_bounded` pair with
  `start_new_session`, matching `diagnosis.py`, so a wedged probe cannot orphan
  node children on a watch that runs every tick.

- The merge gate now runs as an advisory GitHub check (`genesis-merge-gate`,
  workflow `merge-gate`) on every pull request, review, and comment, producing
  the same `git_push_guard.py --check-pr` report installs run at merge time —
  phase 1 of issue #1670. It is deliberately NOT a required check; promotion
  follows the criteria documented in `.github/workflows/merge-gate.yml` after
  its false-block rate is measured. While running under Actions the gate
  excludes its own workflow's checks from the CI rollup so the check can never
  vouch for or block on itself (a check inspecting its own pending run would
  deadlock); the interactive merge path is unchanged.

- **A daily watcher that announces once when the public repo crosses a star
  milestone**, so work parked behind "revisit at N stars" wakes up on its own
  instead of waiting to be remembered. It reads the count unauthenticated, writes
  a permanent observation on the transition, and stays silent otherwise; each
  configured milestone is announced once, including one added below a milestone
  already announced. An install with no repo configured falls back to the
  checkout's remote for the public repo, matched by owner/name (then `origin`);
  when several remotes carry repositories with the configured name and no owner
  is set, it does not guess and logs which key to set, and an install with no
  candidate at all is a clean no-op rather than a daily failing unit. Its state
  lives under `GENESIS_HOME`, and it never writes to a database the integrity
  check has quarantined.

- **The CC model roster config now documents DeepSeek as a worked failover peer,
  including a billing trap it is easy to walk into.** The roster is what runs your
  Claude Code turn on another provider when the Anthropic subscription hits its
  usage cap, and the shipped config file explains how to declare a peer without
  shipping one. DeepSeek previously appeared only in a list of vendor-published
  endpoints nobody here had tried; it is now a full example with its endpoint,
  model id and credential variable.

  The reason it earned its own entry is cost. DeepSeek's Anthropic-compatible
  endpoint bills per token, and it silently maps model names it does not
  recognise rather than rejecting them: an unsupported name becomes its cheap
  Flash model, while anything beginning `claude-opus` becomes its Pro model and is
  billed at the Pro price. The vendor's own quickstart sets only the base URL and
  the key, which leaves Claude Code's opus-tier variable holding a Claude name —
  so opus-tier calls quietly bill at the higher rate. Configuring the peer through
  the roster avoids this, because the roster sets every model variable to the
  peer's own id; the note is there for anyone wiring it up by hand instead.

- **The rule against retiring someone else's pull request is now reachable
  from the command line.** `genesis-development` carries a standing rule — a
  reviewer session retires nothing; a premise-wrong PR gets the
  `needs-architecture-session` label and stays open; a superseded one gets a
  comment naming its successor and also stays open; retiring belongs to the
  session taking up the revival. Nothing enforced or surfaced it, so it bound
  only a session that happened to have loaded the skill. A new advisory hook
  prints the rule at the moment a command would close a PR, on the forms
  visible in the command text: `gh pr close` (including the separated-repo
  spelling that was once a real bypass in the merge gate), a
  `closePullRequest` GraphQL mutation, and a `state=closed` field on a pull or
  issue endpoint. It never blocks, never prompts, and behaves identically in a
  dispatched session as in a foreground one.

  Advisory by design rather than by compromise. An enforcing version drew
  eleven review findings, five of them one unanswerable question: whether a
  gate can tell from argv that a command closes a PR. It cannot — `gh api
  graphql` takes its mutation from stdin or a file, neither of which is in
  argv. Under enforcement that gap is a bypass which must be closed and cannot
  be; under an advisory it is a note that did not fire. The note says so
  itself, so a reader meets the boundary where they rely on it rather than in
  a docstring.

- **The graph engine's projection now refreshes on its own, hourly.** If you
  armed the graph engine, its projection of the memory graph used to be a
  deliberate act — you ran `python -m genesis.memory.graphstore_project` by hand,
  and from that moment the projection drifted from the real graph until you ran
  it again. Nothing announced the drift, because a stale projection and a current
  one are indistinguishable from the engine's side: it serves links you have
  since removed, omits links you have since added, and never errors.

  MEASURED on a live install, six days after one manual projection: **2,286 links
  and 659 memories missing.** The drift is also bursty rather than steady — one
  week on the same install ranged from 4,075 new links in a day down to 5 — so
  "it has probably not changed much" is not a safe assumption to run on.

  A new `genesis-graph-project.timer` now rebuilds it every hour. That bounds how
  far behind the projection can be, which is what makes moving reads onto the
  engine a decision you can take at any moment rather than one that lands on a
  graph as stale as the day you built it. A full rebuild MEASURED 16.1s against
  285,683 links, and it is built alongside the live copy and swapped in
  atomically, so nothing reading the graph sees a partial one.

  **This does not move your reads.** The selector in `config/graphstore.yaml`
  still defaults to the in-process NetworkX store. The projection is kept current
  whenever the engine *exists*, not only once reads point at it — deliberately,
  since a projection that only starts refreshing after you flip the lever gives
  you a stale graph at exactly the moment you begin trusting it.

  **If you never armed the graph engine, this does nothing at all.** The timer is
  installed everywhere, so it also runs on installs that have no engine — there
  it exits immediately with one line and no error, rather than failing hourly
  forever. Setting `enabled: false` in `config/graphstore.yaml` stops it too.

  Two caveats, both worth knowing if you are relying on this. An hour is a
  *bound*, not freshness-on-write: a burst of writes inside the hour can still
  leave the projection meaningfully behind, and write-level freshness needs a
  database-side change signal that is tracked separately. And the bound is a
  little over an hour rather than exactly one — the schedule adds up to three
  minutes of deliberate jitter so this job does not collide with the other
  timers, and the published snapshot is further behind by however long the
  rebuild takes.

- Every install now gets the Grep code-search MCP server (`searchGitHub`) registered automatically — literal/regex search across roughly a million public GitHub repositories, with no API key to obtain. It registers as `grep-app` and never removes or replaces an entry already under that name: if you have your own, or you point `GENESIS_GREP_MCP_URL` at a proxy after setup, Genesis leaves your registration alone and prints the two steps that hand the name over (remove it, then re-run bootstrap). `web_search` also gained `include_domains` / `exclude_domains`, so the documented recipe for scoping an Exa search to a single site now actually takes effect instead of silently searching the whole web.

- **Finishing a piece of work now includes auditing the CLAIMS it produced, not
  only its code.** A green test suite checks what the code does; nothing checked
  the sentences written *about* it — the numbers in a commit message, a
  docstring's assertion about behaviour, a reply saying a review finding was
  fixed, a stored memory. Those are permanent record, and permanent record has
  no tone of voice: the next session reads a confident sentence as a measured
  one.

  The genesis-development skill gains a section listing the surfaces where those
  claims accumulate, and a short walk over them — re-compute the numbers,
  re-measure anything taken before the tree moved, check that bulk edits landed
  everywhere they were supposed to, and re-derive rather than pick a side when a
  comment and a tool disagree. The session wrap-up rule now names the same
  obligation for sessions that wrote numbers, dates or status assertions into
  permanent record.

- **Decision question registry.** `config/decisions.yaml` is the single
  enumerable home for every decision Genesis makes between bounded choices —
  including ones currently made by a hardcoded constant standing in for a
  judgment. Each entry declares its primitive (`choice` / `score` / `noul`),
  the question wording, what the caller does with the answer (`consumes`), and
  what it falls back to when no decision backend is available. A
  gitignored `decisions.local.yaml` overlay is deep-merged on top, following
  the `model_routing.yaml` pattern.

  `consumes` is the load-bearing field: it turns "may this site branch on a
  probability?" from something a reviewer has to notice into a property the
  loader enforces. `consumes: threshold` requires an explicit threshold inside
  `(0, 1)`, a stray threshold on a non-thresholding consumer is rejected, and
  only thresholding sites need calibration at all — `argmax` and `ordering`
  consumers stay fully functional without it. High-cardinality choices must
  declare a `cardinality_strategy`, because options share a fixed token budget
  in encoder-based decision models and labels stop being distinguishable as
  the set grows.

  Registering a decision does not wire it. Every entry starts inert and is
  promoted individually on measured evidence that it beats its incumbent.

- An optional Claude Code status line, `scripts/genesis_statusline.py`, shows the current branch, the session's open ledger rows, the local review streak against its cap, and the branch's PR with its review state — `feat/x · ledger:2 · streak:1/3 · PR#42:approved`. Turn it on with a `statusLine` command in `~/.claude/settings.json`; the script's header has the setup, including a one-line `sh -c` recipe for showing it alongside a status line you already use (Claude Code allows only one). Nothing is enabled by default, and any field it cannot read shows `—` rather than blanking the line.

- A validator session can now record what it concluded about a merged PR, and the ledger keeps the reason when it could not finish. Four verdicts: two close the obligation, and `fail-intent` / `cannot-verify` leave it open with a note the next validator inherits — so a PR that could not be checked on your install says why, instead of looking untouched. The verdict is derived from the evidence document rather than chosen, so it always says what the evidence says: `scripts/pr_verification.py close --pr N --evidence-file <doc>` records it (`print-schema` gives a template), `repo_pulse_worker.py --verification-backlog` shows attempted rows inline, and `--verification-log --pr N` prints the full record for one PR. An obligation cannot be marked handled by a document that established nothing, and a failed verification can never be quietly parked. A claim nobody could check is recorded as `unverified` rather than as a pass or a failure, and only an established failure reaches you as a failed verification — one that was merely suspected keeps the row open with the suspicion named. The document must say whether deployment was established, and a verification run on a tree that does not carry the merge can never close the obligation. Failed verifications lead the backlog, so a long queue cannot hide one.

- **Threshold decisions abstain near their cut.** A decision site that acts on
  a probability threshold must now declare a `dead_band` around its cut and a
  `tie_rule` for scores landing exactly on a band edge. The new
  `DecisionSpec.gate()` returns act, decline or abstain, and abstains for any
  score inside the band and for every score outside calibrated mode. Near-cut
  decisions are not repeatable: re-sending the identical request to a pinned
  decision model can flip roughly one in five of them while the aggregate rate
  barely moves, so a site may not take a branch that a retry could reverse.

- **Every failed Claude Code invocation now emits one `cc.invocation_failed`
  event.** Previously a CC error surfaced only in whatever the calling
  subsystem chose to log, so failures across the many CC call sites had no
  common signal. The invoker now emits the event on both its entry points —
  including the pre-spawn offline check — and then re-raises the original
  error unchanged. It carries the error class, streaming flag, requested model,
  the roster model the call was actually routed to, session id and, where the
  call site is tagged, a caller tag naming the dispatching subsystem, and
  appears in
  `health_errors`. The raw error text is not stored in the event (it is built
  from unbounded CLI output); only its length is recorded. Rate-limit and
  quota errors are WARNING; everything else is ERROR. Repeats of the same error
  from the same tagged caller on the same route are coalesced to one event
  per minute, and the
  next event's message reports how many were folded into it; untagged callers
  are never pooled together. Cancellations emit nothing, and the fallback
  liveness probe is exempt only for its expected rate-limit answer — a probe
  that times out or fails otherwise is reported. The event does not wake a
  reactive ego cycle, since that cycle would itself need Claude Code.

- Every proactive memory recall now leaves a retrieval trace: which retriever (vector, full-text, event, graph) found each candidate and at what rank, its fused, rerank and final scores, and whether it was injected or dropped (and why). Traces are written in the background, kept 14 days, and can be switched off with `proactive.trace: off` in `config/memory_recall.yaml`.

- Sessions now learn at start when another install has left handoff files in a configured shared directory. Each is listed as an untrusted claim to verify, never with its contents, and nothing is dispatched from it. Mark one handled with `python -m genesis handoffs mark <id> --note "..."`; handled state stays local, and a `<name>-REPLY.md` sibling also counts. Off until an absolute `dir:` is set in `~/.genesis/config/handoffs.local.yaml`. The session-start scan runs under a hard time bound, so a slow or hung shared mount reports "unknown" instead of silently producing nothing; `python -m genesis handoffs list` scans the whole directory without that bound.

- **A locked code-only deploy: `scripts/deploy_code_only.sh`.** Runtime code is an
  editable install, so most changes deploy by pulling main and restarting the
  server. Done by hand, that took no lock and could land in the middle of
  another session's validation against the live server, or of `update.sh`. The
  script takes the same lock `update.sh` takes, exclusively, and queues for it.
  A validation run holds the lock shared
  (`flock -s -w 7200 "${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock" <cmd>`), so a deploy waits for
  it to finish; `update.sh` keeps refusing rather than queuing.
  - **Four modes.** `deploy` (the default) pulls main and restarts. `pull` pulls
    and syncs the git hook copies without restarting, and accepts any range: it
    names every change to `src/`, `config/` or `pyproject.toml` that the running
    server has not loaded, and the next step. `restart` restarts the tree as it
    stands. `status` is read-only and takes no lock.
  - **What the server booted from.** Every mode reports the commit the running
    server started from against the tree, read from HEAD's reflog at the unit's
    start time. It is "unknown" when the reflog does not reach back that far,
    has a gap left by pruned entries, does not end at HEAD, shows the clock
    stepping back across the boot, has a move in the boot's own second, or when
    the boot is older than git's expiry for unreachable reflog entries (which
    can remove a detour as a pair without leaving a gap). The reflog cannot show
    a manual `git reflog expire --rewrite`, a move whose committer time was
    backdated, or one made after the clock stepped back behind the boot; the
    server recording its own commit at boot is the complete answer.
  - **`deploy` restarts only for what the server loads:** when the files under
    `src/`, `config/` and `pyproject.toml` are the ones the server booted from,
    after the merge and at every commit HEAD has held since the boot (a docs or
    hooks range, or nothing to merge), it neither stops nor restarts the server.
    The history matters because the server imports lazily: after a `pull` of
    code, a module it loaded stays in memory even once a later commit restores
    the files. `restart` forces one.
  - **It refuses before anything changes:** a linked worktree, an unfinished
    `update.sh` run, a branch other than main, a dirty tracked tree, a unit that
    runs a different venv or from a different directory, a live foreign deploy
    marker (or one that cannot be written), a diverged tree, and a venv that
    does not match the
    `pyproject.toml` being deployed. That check reads the installed project's own
    metadata: an editable install from this checkout, with requirements (base and
    every optional group, compared parsed) and `requires-python` equal to the
    incoming file's, every base requirement actually present at a version it
    accepts, and the installed entry points (console commands, plugins) equal to
    the file's, since a new command gets its shim only from a reinstall. The refusal names the command that fixes it: `update.sh` when there
    is a range to merge, `update.sh --post-merge` at the tip (plain `update.sh`
    stops at "Already up to date" without reinstalling, unless update.sh-only
    paths changed since its last recorded run), and neither when the
    venv's python is older than the incoming `requires-python`.
  - **`deploy` stops the server before the fast-forward**, as `update.sh` does,
    so no request runs against a mix of old and new modules. A merge git refuses
    starts it again on the unchanged tree, and any failure while it is stopped
    starts it before exiting, and so does a restart whose start half fails.
  - **Just before any restart it checks again** that the checkout is the exact
    commit it checked, with no tracked change and no untracked file under
    `src/`, `config/` or `pyproject.toml`, and that no server is running outside
    the unit (such as `update.sh`'s fallback, which a restart would not replace).
    `deploy` and `restart` also refuse those last two before anything changes.
    If a late check fails after `deploy` stopped the server, refusing would leave
    it down, so it is restarted on the tree as it stands with its health check,
    and the run ends in a critical alert naming what changed. The git hook
    copies are synced after the restart, not while the server is down.
  - **A range that adds a file already present here, untracked, is refused.** A
    fast-forward overwrites an ignored file without asking, so a local secrets or
    settings file would be lost. A tracked file or directory the range replaces
    is git's own and does not count. The merge itself runs with
    `--no-overwrite-ignore`, so an ignored file that appears after that check
    (written by the server, or another session) is refused by git at the merge,
    and a stopped server goes back up on the unchanged tree. The regenerable files
    reset for the merge (`AGENTS.md`, the procedure trigger cache, which update.sh
    discards the same way) are named as they are reset, in a refusal's message and
    in the exit alert, so a refused merge never reads as "nothing changed" in
    silence.
  - **Across a restart** it holds the deploy marker (the watchdog defers) and
    pauses the host Guardian (no false "Genesis down" alert), then waits for
    health with the same window `update.sh` computes. The health request goes to
    the loopback address with no proxy and no `.curlrc`, and counts only if the
    same unit pid owns the port before and after it.
  - It queues for the lock for up to two hours by default, as long as a
    validation's documented hold. Healthy means the
    RESTARTED unit is serving: the unit is active in a new systemd activation (its
    invocation id, since the kernel can hand the new process the old pid), and every
    socket listening on the health port is one of that pid's own (read from
    `/proc`; when ownership cannot be read the answer is no). The subsystems are
    then compared with the pre-restart manifest, as `update.sh` does; a
    regression is reported and alerted, and the deploy stands.
  - **On a failed health check it alerts and holds.** The tree is not reverted.
  - It names what a code-only deploy leaves to `update.sh`: activation paths
    (units, bootstrap, the Claude Code pin, dependencies), code the host Guardian
    runs, and code the tmp watchgod runs from the tree.
  - Launch `deploy` and `restart` detached, as a transient `systemd-run --user`
    unit named with the time, so a second launch queues on the lock (the command
    is in the script's header): a session's background job dies with the session.
  - For a validation against the live server, the script judges the bracket:
    take the `bracket:` token `status` prints at the start, and run
    `status --verify <token>` at the end (exit 0 is valid). A token exists only
    when the boot commit is known, HEAD's runtime files are the ones the server
    booted from and were at every commit HEAD held since the boot, and nothing
    under them is edited outside git. It covers a
    restart (boot commit, MainPID, systemd invocation id, which a reused pid
    cannot fake) and an edit to an ignored runtime override such as a
    `config/*.local.yaml`, fingerprinted without the files the server rewrites on
    its own, and the user overlays in `~/.genesis/config` that the loaders prefer.
    HEAD may move over docs or hooks without invalidating the run. It is a
    tripwire, not a certificate: it cannot see, and reads valid through, a change
    to the venv's installed packages, the other files the server reads from
    `~/.genesis/config` (only the `*.local.yaml` overlays are fingerprinted), an
    edit undone without moving HEAD (a stash and its pop included), and a
    rewritten or backdated reflog. Proving what the server runs needs the server
    to report its own identity.

- On an install that runs a local integration branch named `live` (declared by `~/.genesis/deploy_manifest.json`, and only in the repository the hooks ship in), `git commit` and merge commits on `live` are refused, and on every install a push that would publish a commit carrying a `Deploy-rebuild:` trailer is refused on every remote, so a branch cut from `live` cannot carry other candidates' unreviewed code into a pull request. The Claude Code merge guard also refuses a `git merge` on `live`, including a fast-forward, which no git hook can see. Not covered: any other way of moving the `live` ref (a fast-forward merge or pull in a plain shell, `reset`, `branch -f`, `update-ref`, `checkout -B`, a fetch into it), and cherry-pick, revert and rebase, none of which run these hooks. Worktree instructions now create branches from origin/main with `--no-track`, so a new branch does not take origin/main as its upstream.

- A root Tailscale watchdog (`genesis-tailscale-watchdog.timer`, every ~2 min) heals a stuck Tailscale tunnel: when a peer that traffic wants has not completed a WireGuard handshake for 5 minutes, and the peer answers discovery pings while the tunnel does not, it runs `systemctl try-restart tailscaled`, at most once an hour. What the restart did is read back from systemd, and it counts as a heal only if the stuck peer then answers through the tunnel. Each run reports only what it observed, per peer; the awareness tick keeps one owner alert per stuck peer, paged once and resolved only on evidence that the tunnel answers again or the peer went offline, and only peers confirmed stuck in the same run are ever restarted; a restart that failed, hung or could not be confirmed pages too. A stuck-tunnel alert ends a day after the tunnel was last seen stuck, and turning the watchdog off withdraws its alerts. A watchdog that stops reporting, or keeps reporting that it cannot check any tunnel, raises its own alert, and the protection-posture check flags a host with tailscaled but no enabled watchdog. It installs on any systemd host with a tailscaled unit. `NETWD_TS_MODE=observe|off` in a drop-in turns it down or off durably (off also withdraws its alerts), and `scripts/uninstall.sh` now removes both root watchdog timers, and names anything it could not remove instead of reporting success. The network-resilience installer no longer writes through a symlink: reinstalling over a masked unit (a symlink to `/dev/null`) could change `/dev/null`'s mode, since `chmod` follows symlinks.

- **An install can now switch off the credentials guard's `secrets.env` prompt without loosening any block.** Set `hooks.asks.secrets_env: off` in `~/.genesis/config/genesis.yaml`. Clones with no local config keep the prompt. Switching it off does not approve the command: the guard makes no permission decision and leaves a note naming the setting, and Claude Code's own permission settings then decide the command. A dispatched session is still denied. The key set is closed: a misspelled or unknown key, a duplicate key, a value that is not a boolean, or an unreadable file keeps the prompt, and the prompt says why. The push / PR-open approval prompt has no such switch, on purpose, because it is where code leaves the machine; `push_publish: off` is ignored with a note. The setting is read from the file only; there is no supported environment-variable override.

- **Optional: Claude Code's built-in `WebSearch` can be answered by Genesis's own `web_search` chain.** Set `GENESIS_CC_WEB_OVERRIDE=1` in `~/.genesis/cc-slot.env`, and each interactive slot created after that loads a new plugin, `plugins/genesis-web-override/`, from your checkout. It answers the built-in search in the main session and in subagents. It falls back to the built-in on any failure, and whenever a call restricts domains. It runs on Claude Code's early-access function hooks, which the lever turns on for those slots only. Details and limits are in `.claude/docs/web-tools-guide.md`.
- **Claude Code sessions that Genesis dispatches now always run with function hooks off.** They pin `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=0`, so a plugin's JS modules cannot start running in background work if Claude Code later turns the feature on by default.

- **Guardian: pool relief acts early, from measured growth, and can use VG
  space LVM's autoextend could not.** Relief now keeps a 7-day pool history
  and also acts before the 3% / 10% reserve, when data or metadata would fill
  within 48 hours at the measured rate. Early relief frees pre-recovery and
  superseded snapshots, and the rollback snapshot only once it is older than
  48 hours and (on LVM) measured holding space; a younger one is still only
  deleted at the reserve. On an LVM thin pool with the `genesis-thinpool`
  profile, when data reaches 80% and the volume group has free space but less
  than one 20% autoextend step (so LVM would not extend at all), relief grows
  the pool by that space, keeping some unallocated for metadata. New
  `storage_pool` keys: `early_horizon_hours`, `lifeline_max_age_hours`,
  `history_sample_interval_s`, `history_max_samples`, `extend_keep_free_mib`.

- **`web_fetch` now reads what a LinkedIn post's video says.** For a LinkedIn post, the page is fetched as before, and when the post has a captioned video its transcript comes back in a separate `video_transcript` field, so the inbox evaluator (which has no shell) sees the video's content, not just the post text. It uses captions only, never audio; a post whose video has no captions, or whose captions could not be fetched, says why in `video_error`, and a text-only post is unchanged.

- **A peer session's `SendMessage` name now appears next to its id.** Each `[Concurrent | …]` awareness line ends with the peer's address, for example `a1b2c3d4 -> genesis-7f (cc-2:@1.%1)` (its name, then its tmux pane), so a session that knows which peer owns some work can message it instead of guessing by topic. For any other session id, use the new `session_address` MCP tool or `python -m genesis session-address <id-or-prefix>`; add `--check` to compare the answer with Claude Code's own `claude agents --json`. A peer whose process has ended reads `(not reachable)`, and nothing is ever guessed: an address is shown only when exactly one live process holds that session.

- Local Codex CLI sessions can now stop recognized review requests and fix commits when the shared review budget requires fresh approval, handing the decision back to the user.

- **`scripts/deploy_candidates`: the engine for `live`, a local integration branch that runs unmerged pull requests on this install's server before they merge (inactive for now).** Nothing is pushed or merged anywhere. `live` is origin/main plus the candidate branches listed in `~/.genesis/deploy_manifest.json`, rebuilt from scratch without touching the working tree (`git merge-tree` and `git commit-tree`), so several open PRs can run together while they are reviewed.
  - **Pinned commits, no approval step.** `add` pins a candidate to the commit it sees, and `live` runs that commit, never a later one. If the branch moves, the candidate leaves `live` until it is added again. There is no approval step: running an unmerged branch on the install's own server does not need one.
  - **What `rebuild` checks.** Each rebuild re-checks every candidate: that the branch still points at the pinned commit, the admission rules, and that its PR (if any) is still open against `main` with that commit at its head. A candidate that fails is excluded, by name and reason. A rebuild that cannot read something it needs, such as GitHub or a fetch of `main`, changes nothing.
  - **Retirement.** A candidate is retired only once its PR's merge commit is in the fetched `main`.
  - **Other `rebuild` behaviour.** It refuses a dirty checkout, a commit on `live` that it did not make, a `live` checked out in another worktree, and a file in the way of the move. It moves the checkout once, and only when the files change, then syncs the git hooks and fast-forwards local `main`. It never restarts the server.
  - **`drop`** only removes code from `live`. It needs no network, and it works from a plain shell (`env -i`), so a candidate that breaks the session hooks can still be taken out.
  - **One candidate per commit.** Two candidates may not share a commit that is not reachable from `main` (a squash-merged commit never becomes reachable), so a stack of branches goes in as its top branch. `add` refuses a branch that shares one with a listed candidate, naming the remedy for how the two relate, and a rebuild excludes every candidate in such a pair, by name. A merged candidate in such a pair is not retired until one of the pair is dropped.
  - **Admission** refuses:
    - any change to a database or data migration, and the boot-time schema files;
    - the host guardian's own files, the Claude Code pin, and what drives the host on a timer;
    - git and Claude Code hooks;
    - the scripts that keep `update.sh` and code-only deploys off `live`;
    - the engine itself.
  - **Nothing can go live yet.** `add` and `rebuild` require the server to be running the change that teaches `update.sh`, crash recovery, the guardian's code revert and the dashboard's update routes about `live`. That change has not merged.

- **An install can now switch off the first-push prompt for its public repo, for a single plain `git push` only.** Set `hooks.asks.push_publish: off` in `~/.genesis/config/genesis.yaml`. The push guard then stops asking for the first push of the current branch only when every one of these holds:
  - The whole command is exactly one plain `git push` (for example `git push -u origin HEAD`). There is no other step of any kind, no redirection, no subshell, no global option between `git` and `push`, no environment prefix and no wrapper. A chained command still asks, so run the first push as its own command.
  - The remote git really pushes to resolves, after any URL rewrite, to exactly `https://github.com/<owner>/<repo>` for the public repo configured under `github.user` / `github.public_repo`. The ssh and scp forms always ask.
  - Its push config is simple, and no `http.*` git config, remote proxy or remote helper is set.
  - No proxy, TLS-trust, ssh-program or git-config-injection environment variable is visible to the hook.
  - A live check, with redirects and credential prompts refused, confirms the branch does not yet exist there.

  Like the `secrets_env` switch, it approves nothing: the guard makes no permission decision and leaves a note naming the setting. `gh pr create` is not covered, because without a terminal gh refuses to push an unpushed branch rather than pushing it. Everything else still asks or blocks as before, including force pushes, pushes anywhere else, close-then-push, re-pushes with no open PR, and round-cap asks. A dispatched session is still denied. Clones with no local config keep the prompt. This replaces the earlier note that `push_publish: off` is ignored.

  Known residue: the hook cannot see shell-profile environment variables, aliases, functions or `PATH`. It assumes its payload's cwd is the shell's cwd, as every arm of the push guard does. It also cannot see a concurrent process changing config or the branch, or publishing the same branch, between its check and the push.
- **Fixed: a bare `git push` is now checked against the remote git actually pushes to.** When `branch.<name>.remote` named a remote other than origin and its remote-tracking ref was missing, the guard assumed origin. A re-push could then be allowed silently on the strength of origin while the push went to the other remote. The guard now reads `branch.<name>.remote` from config, and an unreadable value asks.

- **Embedding backend order is now an install setting.** Set
  `memory.embed_local_first: true` in `~/.genesis/config/genesis.yaml` (or
  `GENESIS_EMBED_LOCAL_FIRST=true`) to put the local Ollama backend ahead of the
  cloud one. Unset, the order stays cloud-first with Ollama as the fallback.
  It applies to every chain built without an explicit order: runtime storage
  and recall, the memory MCP server, and default providers. It changes the
  order only, never which model writes. Takes effect on restart.

- **A parser for the PR body's `## Acceptance` section and source pointer.**
  `scripts/acceptance_declaration.py` reads a PR body and reports what "done"
  means (the list items under the first `## Acceptance` heading) and where the
  work came from (`Closes #N`, a `Ledger:` or `Follow-up:` row id, or a
  `Spec:`/`Plan:` name). It reads a small, closed Markdown grammar over the same
  visibility scanner the other PR-body checks use, so comments and fenced
  examples never count. A body it cannot read reliably is refused with a reason
  rather than guessed at: one over GitHub's 65,536-character cap, or one with a
  code fence that is indented or inside a list item or quote. Nothing calls it
  yet; wiring it into PR creation and the merge report is a separate change.

- **PR size counter.** `scripts/pr_shape.py` turns a unified `git diff -M` into a counted line total and a size band (`ok`/`shape`/`override`), excluding tests, changelog, prose, binaries, blanks, comments and verbatim moves; `parse_shape` reads a `Shape:` reason from a PR body. No callers yet — wiring into the review gate is a later maintainer change.

- Added opt-in shared Serena services for the main checkout, reducing duplicate language-server memory across concurrent clients while keeping linked worktrees isolated.

- Added an open-questions store: sessions can park a decision that only the owner can make (`open_question_raise`), mark which ledger rows, follow-ups or board cards it blocks, and close it with what settled it. The morning report counts the questions still waiting on you. The store is local only and never posted anywhere. Also adds the work board's local stores and a `board` setting (`off` by default) that the upcoming GitHub Projects board builds on.

- **`readable_body` gains an opt-in `keep_blank` that preserves paragraph boundaries.** `scripts/check_cc_pin_receipts.py` and the fallback in `scripts/e2e_declaration.py` now emit a blank source line (outside fences and comments) as `""` when called with `keep_blank=True`, so paragraph-aware readers can see where a list item ends. The default stays `False`; no existing caller changes.

- Added promotion onto the work board: `board_promote` turns a private ledger row or follow-up into a public GitHub issue after the owner approves it individually. The draft is privacy-scanned first; a record an open question blocks is refused; the issue never names the private record; and a crash mid-post can never create the issue twice. Everything that can change while a promotion waits for approval (the record closing, a label being deleted, a new open question) is checked again right before the issue is created: a promotion that can no longer go out is ended with the reason logged, so it can be proposed again once fixed. Promotion does not put the issue on the board's project yet; that arrives with the board reconciler. `scripts/board_setup.py` creates or checks the board's GitHub project (dry run by default): it works only on the project recorded in the board config (or one it creates and records), links it to the tracker repo, and never deletes a Status column. The issue poster's duplicate check now also searches all open issues, not only the 200 most recent.

- Added an advisory `main-reverts` row to the `--check-pr` merge report: it names the files a PR's diff carries that none of the PR's own commits touched, the signature of a merge from main that kept stale copies and would silently revert main, along with how to repair it; it never blocks a merge.

- Added foreground `gmodel` routes for Kimi K3 and MiMo V2.6 Pro, with subscription, direct API and OpenRouter choices and automatic credential-based selection that says why it picked a per-token route. A routed session's endpoint, credential, context window, effort and compaction/thinking settings are pinned for the whole session, so editing a settings file mid-session cannot re-route it; a `maxEffortLevel` cap in any settings file still applies, and `gmodel` warns about one at launch. Managed (organization) settings outrank `gmodel`; `/status` shows whether one is in force. The model is pinned at launch, and `/model` can still change it for the session on the same endpoint and key; on the OpenRouter route a Claude model ID bills Anthropic per token, and on Kimi routes Alt+T turns thinking off (on the Kimi subscription, Kimi then serves K2.8 Preview), both said at launch. `--effort` and `--autocompact` are accepted, and `gmodel` says the pins outrank them. The effort-cap warning also checks the `settings.local.json` that Claude Code reads at the repository root or main checkout. The Kimi subscription route reads `KIMI_CODING_API_KEY`, the same key as the Kimi Code roster example.

- Added optional verified placement of indexing batches in a dedicated workload slice; routing failures defer indexing instead of falling back outside the requested boundary.

- Added a read-only work-board reconciler: every five minutes it reads your GitHub project board and records what is on it. It counts cards by column and kind, measures how many of the repo's open issues and pull requests are on the board, and logs each card moved into In Progress once. `board_status` shows that summary and whether the reconciler is alive; `board_item` shows one card's column, its blockers and any open question holding it. It writes nothing to GitHub. It reports in every mode, but it reads GitHub only when the board's mode is not `off`, Genesis is not paused and a project is configured; `board_item` likewise reads nothing while the mode is `off`.

- **Backups can now include extra directories you choose.** Set `GENESIS_BACKUP_EXTRA_DIRS` to directories under your home directory (separated by `:`); each is kept as an encrypted archive in the off-site backup tier, with rebuildable caches such as `.venv` left out, rebuilt fresh on every run, and `restore.sh` puts each directory back whole, keeping any existing copy beside it. See SETUP.md, "Extra directories".

- Claude Code sessions, foreground and background alike, are now kept from hand-editing tracked files in your install's main checkout (the deployed install). A file edit (Write/Edit/MultiEdit/NotebookEdit) to a tracked file there is refused with a pointer to work in a worktree and open a PR. Shell commands are never refused: the guard compares the checkout before and after each command and, if a tracked file changed or HEAD moved, tells the session which files changed and, when the command was aimed at the main checkout, how to restore them — so a stray edit is caught and can be cleaned up before `scripts/deploy_code_only.sh` refuses to deploy a dirty tree. It reports after the fact, so it does not stop a command's effects. Untracked files, worktrees, and the dashboard update's own merge sessions are unaffected. Turn it off with `settings_update("main_checkout_guard", {"enabled": false})` or `GENESIS_MAIN_CHECKOUT_GUARD=0`.

- Add explicit pinned Codebase configuration staging and read-only diagnostics; setup validates resolved settings paths before staging, synchronizes staged files and directory entries before immutable publication, preserves created staging on failure and the machine disable switch, and reports incompatible builds while limiting the environment path override to status.

- Added a private qualification journal with immutable manifests funding the
  complete request schedule, decimal reservations, provider-scoped generation
  ownership, retained billing evidence and explicit recovery state. This is
  groundwork for the qualification rebuild; it performs no paid requests.

- **A click that opens a new tab is followed.** When a click opens a new tab or popup (a `target=_blank` link, a form submitted to a new tab, `window.open`), the browser tools switch to it and the click result names it (`new_page`); the original tab stays open, and if the new tab closes itself (a sign-in popup) the tools return to the tab that opened it, or to the nearest still-open tab in that chain.

- **The merge gate now checks the rework contract on rebuilt PRs.** When a PR replaces one that was sent back for rework (labelled `needs-rework` or `needs-architecture-session`), whether its body has a `Replaces: #N` or `Supersedes: #N` field line (prose is never read as a declaration) or its commits contain the old PR's head, the merge is blocked until the old PR carries a maintainer's `## Rework acknowledgement` comment, posted before the new PR opened, and the new PR's body has a `## Rework` section with `Replaces:`, `Split:`, `Deviations:` and `Questions answered:` lines. `--check-pr` shows this as a new `rework` row. The owner can waive it with `# rework-override`, which is logged.

- Managed Codebase now has deliberate native enable, disable and remove commands with validated startup, independent failure cleanup and preservation of immutable settings and provider state. Custom unit dependency namespaces refuse before activation.

- **A session can ask whether a heavy job fits before launching it.** `python -m genesis.hostmetrics preflight --name N --ram GB --cpu PCT` (with the Genesis venv's python) answers GO, WAIT, NO or ASK (exit 0, 3, 2, 4) against a configurable share (default 80%) of the container's memory, CPU and disk, counting current load and, when the guardian is configured, the host's free memory too. `status` prints the readings it uses. Tune it with `GENESIS_RB_*` variables or `~/.genesis/resource-budget.env`; see `docs/reference/resource-budget.md`.

- **Heavy jobs can run admitted and capped, visible to every session.** `python -m genesis.hostmetrics run --name N --ram GB --cpu PCT -- CMD` (with the Genesis venv's python) runs the preflight. On GO it launches the command in a `genesis-job-*` systemd scope, with the RAM and CPU estimates as hard caps. On exit it reports the job's peak memory and CPU time, and whether the job hit its memory cap. `status` lists live jobs, and `preflight` counts each job's unused reservation, so sessions see each other's jobs once they are launched and stop over-committing memory. `--wait-until-fits MIN` waits for room instead of giving up. A watchdog stops the job if the box stays over the line for a minute. Without a systemd user manager the job runs uncapped at low priority and says so; if systemd refuses the caps, `run` stops with an error instead.

- **Genesis now tells you when the next deploy would refuse to run.** The hourly deploy-health check notices tracked files edited in place in the install's own checkout (by hand, by another coding tool, or by Genesis's own runtime writers), which make `scripts/deploy_code_only.sh` and `scripts/update.sh` refuse. It raises a non-paging alert on the dashboard and in the morning report naming the files, and clears it once they are restored or brought in through a pull request. The health snapshot's `deploy_health` section carries the current list under `main_checkout`.

- The disk guardian now names the file that is filling a disk. A file held open for writing that is a large share of its filesystem (25% by default), or that grew by 5% of it in one poll, triggers a critical alert naming the file, the processes writing it and, for a file in Claude Code's temp space, the session. Fast growth also switches the guardian to its 5-second poll. Detection only: nothing is stopped or deleted. Thresholds are `DG_RUNAWAY_PCT`, `DG_RUNAWAY_RATE_PCT` and `DG_RUNAWAY_MIN_MB` in `watchgod.conf`.
- In observe mode (`WATCHGOD_ACT=0`) the disk guardian now sends its alerts, marked as observe-mode, instead of only logging them. Observe mode still changes nothing on disk.

- Added offline reference admission and whole-corpus feedback review for the
  qualification rebuild, with separate human/frontier labels, policy-bound
  receipts and exact corpus/policy human approval. No grading calls are made.

- Added qualification corpus validation and offline production-contract adapters
  for relevance and novelty, preserving source-labelled references and actual
  rendered candidate identity. Full execution and storage replay land later.

Corpus JSONL decoding uses UTF-8 with structured invalid-byte refusal. Adapter observations are scoped to each invocation, raw relevance must be a numeric value in [0, 1], and embeddings that store as an all-zero float32 vector are rejected.

- Deletes of user data by Genesis are now recoverable: a cognitive-file rollback and a leftover task-worktree directory move into a Genesis-owned trash (`~/.genesis/trash/`, list and restore with `python -m genesis.trash`) instead of being deleted. A task worktree holding uncommitted work is no longer force-deleted when its task is re-run; the task fails with the reason and the worktree is kept for the worktree reaper. See `docs/reference/trash.md`.

- `scripts/deploy_candidates add <branch> --approve-hooks <who>` lets a candidate that changes a git or Claude Code hook run on `live` before it merges, once the owner has approved it: the approval is recorded for the pinned commit only, a rebuild leaves out any approved candidate whose hook would merge into bytes nobody approved (naming why), and when a candidate leaves `live` its installed git hooks are put back to what the checkout now holds. The deploy manifest moves to version 3.

- Qualification transport retains responses before SDK cleanup, refuses repeated completions, and reconciles exact costs through generation receipts. Documented routing metadata and disabled gateway response caching guard both live acceptance and replay; contradictory identities retain unresolved liability. Frozen bindings and incident recovery are required; the execution runner and spending approval remain separate.

- Add offline qualification preflight that checks complete reference admission, actual production questions and committed disposable storage effects before the later runner can request execution approval, with historical-source and procedure-metadata validation, normalizer registry source binding and stale-cache refusal.

- Add current-bound offline qualification reports and separately approved serial Flash-only or MiMo–Flash runs with operating allocations, retained billing incidents and observed-cost reporting; provider charges can exceed the approved allowance.

- **Graph-engine fallbacks are now recorded durably.** Every memory-graph traversal (recall enrichment, memory expansion, drift queries and the ambient worker) records which graph store was configured, which one answered, and why they differed, in the evaluation events table, kept 30 days. The first fallback of each request is written the moment it happens, and a record that fails to save is also noted in a local file, so the evidence cannot vanish silently. This is the evidence the FalkorDB default-on decision waits on; until now those fallbacks only reached logs, and from the MCP memory server not even that. Set `GENESIS_GRAPH_TELEMETRY_DISABLED=1` to turn it off.

- The review-round evaluator (`scripts/review_budget.py`) now reports what a round reflection must answer for: an immutable key per finding in the open round (`c<comment id>`; `r<review id>:<count>` for the findings in a review body, answered as one; `i<comment id>` for a Codex findings comment), whether every key could be read, which reviewers reported on the current head, and which reviewers reported on any other head of the PR (the reviewers a settle window should wait for). Nothing consumes these fields yet; the round-reflection tool and gate that read them are later changes. Ids are read from GitHub's `fullDatabaseId` (the older `databaseId` is deprecated). A missing id never changes the round count or status; it only marks the keys unknown.

- Shared credential-label recognition for structured consumers, using the same token, password and environment-label vocabulary as the plain-text scrubber. Existing plain-text redaction behavior is unchanged.

- New `scripts/review_reflection.py`: the round-reflection format and its coverage check. A round reflection is an empty commit, made before a review round's first fix, that answers for every finding of the round (distribution, premise verdict, scope against the acceptance list, decision, and one disposition per finding key from `review_budget`). `covered_keys` is the single check a later commit-gate change will call: it counts a reflection only when it is an empty commit naming the round head and it meets every rule (the round's structure and obligations by lane, the recurring-class rule against the branch's previous round, a readable adversarial audit where one is cited, no escalation). `status` reports what the open round owes and whether it has settled; `validate` checks a file offline. Nothing calls it yet.

- **`python -m genesis.trash put PATH --reason R`** moves a file or directory into the Genesis trash from the shell, so a session deleting user or project data can make it recoverable instead of using `rm`. The trash also refuses to move the live Genesis database (or its `-wal`/`-shm` files, or a folder holding it).

- **Memory-server census for the graph-engine cutover.** Once an hour genesis-server records which memory servers are running and whether each one started on code that records graph traversals. The FalkorDB default-on decision needs 14 days in which every traversal was on record, and a memory server started before that code was deployed records nothing, so the clock can only start once none is left.

- **Graph-engine cutover report.** `scripts/graph_cutover_report.py` says whether the FalkorDB graph engine is ready to become the default: PASS after 14 consecutive days with no fallback and enough traffic, NOT YET with what is still short, or INCONCLUSIVE when some of the evidence may be missing.

- **An install can now switch off the review-request prompt that appears when the review history cannot be read.** Set `hooks.asks.review_request: off` in `~/.genesis/config/genesis.yaml`. A PR comment requesting a Codex review, as the whole command, whose round history the guard could not read because GitHub failed to answer just then (every error code on the evaluator's transient allowlist) then goes ahead with a context note instead of a prompt. Persistent or tamper codes (deleted findings, truncated responses, a malformed record, a configuration error, a moving head) keep asking. The cost: while GitHub is unreadable, the round budget is not checked for that request. The round-cap prompt, a request whose PR number cannot be resolved, a dispatched session's deny, and the refusal of a review request chained with a commit, push, merge, close or PR create are unchanged. Like the other keys, it approves nothing.
- **A failed GitHub read in the review-round counter is retried once** while its read budget allows, instead of one failure deciding the result.
- **The unreadable-history prompt now says why the read failed**: the evaluator's error codes plus a fixed-vocabulary class of each failed `gh` call (for example `gh_failure:http_502`, `gh_failure:rate_limited`, `gh_failure:hook_deadline`), never the raw error text.

- A session that deletes user data with `rm`, `unlink` or `shred` (Claude Code memory, plans and settings, `~/.genesis` config and output, the gitignored identity and local config files) now gets a note pointing it at `python -m genesis.trash put`, which can be undone. The note is advisory and never blocks a command.

- Added an opt-in, paid-model-free native Codex CLI/app-server hook harness with isolated shell, patch, and MCP sinks.

- `python -m genesis handoffs send` delivers a handoff file to another install over ssh, for installs that share no handoff directory. It writes into the peer's configured handoff `dir` (and refuses when none is set), treats a re-send of the same content as a no-op, needs `--replace` to overwrite different content, and reads the file back by sha256. `--session` also adds one fixed-text pointer row to a session's ledger on the peer, and `--dry-run` writes nothing. `handoffs peers` lists peers from `~/.genesis/config/peers.local.yaml`, and `handoffs sessions` lists a peer's recent sessions read-only. A peer handoff is context, untrusted on arrival, not a way to report findings. See `docs/reference/peer-handoff.md`.

- Enforce external and validator Genesis MCP tool profiles during discovery and direct calls, and skip unused health dispatch initialization for external clients.

- Added an opt-in guardian swap ceiling (`swap_ceiling_pct` in a deployed
  install's guardian config): caps this container's share of host swap as a
  percentage of the host's total swap pool, enforced through Incus's own
  native swap-ceiling key under a hard memory cap, or a direct cgroup write
  otherwise (no memory cap, or `limits.memory.enforce: soft`). Set
  `swap_ceiling_pct: off` to remove a ceiling; removing the setting leaves
  whatever is set alone. Install-local; the public default remains uncapped.

- Codex's inactive validator boundary now admits workspace patch sources and destinations while refusing protected instructions, paths outside the workspace, symlinks and shared file inodes.

- Optional transcript analysis can parse local records with structured credential scrubbing and explicit unresolved identity and usage. The parser is a library; collection and query commands follow separately.

- Add an inactive, strict local request protocol for future interactive Codex validators, currently limited to a private-file protocol probe.

- Genesis now names a slow SQLite statement in its log the first time it happens. Any statement on a shared database connection (including the row fetches of its cursors), and any pooled recall read, that takes at least `GENESIS_SQLITE_SLOW_MS` (default 1000 ms; `0` turns it off) logs one warning with the SQL text (never its parameters), how long it queued for the connection versus how long it ran, whether it finished, was cancelled or failed, and what it was most likely stuck behind. Repeats are limited to one line per statement and outcome per minute, unless a repeat is at least twice as slow.

- Added snapshot-bound restore retries and compatible legacy/v2 encrypted transcript readers before new-format capture is enabled. Explicit local sources take precedence over off-site configuration, separate Tier-1 audit records remain recoverable, and failed selection initialization cleans unpublished staging.
- Fixed selected cache identity and inventory validation, preventing damaged inventory from silently omitting recovery, and made retirement resumable after interrupted deletion. Identical legacy transcript copies collapse; differing plaintext/encrypted copies require an explicit encoding preference.
- Fixed fresh pooled transcript recovery over SMB by keeping its temporary credentials available through all off-site downloads.

- **An install can now switch off the prompt for a push onto an open pull request's branch from another checkout.** Set `hooks.asks.push_pr_branch: off` in `~/.genesis/config/genesis.yaml`. A `git push <remote> <current>:refs/heads/<branch>` then goes ahead with a context note instead of a prompt, but only when `<branch>` is the head of an open pull request on this repository, the remote is a github.com https repo of the configured owner, `<branch>` is not a default branch, and no other step in the command can change git config first. It is separate from `push_routine`, which never reaches this push. Force pushes, deletes, tags, unqualified destinations, every block and a dispatched session's deny are unchanged. Like the other keys, it approves nothing.

- Standalone hosting provides bounded request-body ingestion for the dependent
  peer send/cancel API: an absolute body deadline, decoded and framing byte caps,
  and bounded unread-body cleanup. The existing listener and network topology are
  preserved; peer routes and task execution are not activated by this prerequisite.

- `python -m genesis vault` creates an encrypted card vault for virtual cards: `init` seals an age key under a strong passphrase, `add` encrypts a card's details to the vault's public key (a merchant scope or `--single-use` is required), and `list`, `retire` and `status` manage it. The commands that read a secret run only in your own terminal. Install the new `payments` extra to use it.

- **Browser profiles are copied aside before a new browser version first opens them.** A browser upgrades its profile on first open, and the older version may then refuse it. Before the browser tools open a profile with a different Camoufox or Chromium version, the profile is copied to `<profile>.pre-v<old version>-<time>` (caches left out), so going back to the old version can restore it. The newest copy per profile is kept, the newest of each version for 90 days, and older ones are removed after 14 days. Sessions launching at the same time make one copy, a launch that fails before opening the profile does not make another, and a copy is skipped when it would move the disk into the disk watchdog's ORANGE or RED tier (a disk already ORANGE still gets one, unless it would reach RED). A copy that cannot be made (low disk, profile open elsewhere) is logged and never stops the launch; a launch waits up to 10 minutes for another session's copy of the same profile.

- `python -m genesis vault unlock` hands the card vault's key to genesis-server, which holds it in memory until `vault lock`, 30 minutes idle, or a restart. `vault status` shows whether it is unlocked and the last vault event. Every unlock, lock and refusal is recorded with the process that asked, and an unlock that cannot be recorded is refused.

- **`scripts/install_browser_stack.sh` installs the browser stack in one step.** It fetches the Camoufox engine the installed camoufox package pairs with (camoufox 0.5.7 or later; never on an older package, whose fetch deletes the whole engine cache), installs the Chromium the browser tools' fallback launches (patchright's when patchright is installed, otherwise playwright's) and reinstalls a build whose files went missing, and installs missing system libraries through non-interactive sudo or prints the command to run. The download goes to `~/tmp`, a run is skipped when the engine cache, the download directory or Chromium's directory has less than 4 GB free (`GENESIS_BROWSER_MIN_FREE_GB`, measured with any btrfs quota taken into account), and a re-run does nothing when everything is in place. A `genesis-browser-stack.service` unit can run it unattended; it refuses to run inside a Claude Code session.

- When a browser click, or a fill's focus click, fails after Playwright has started sending the mouse events ("performing click action"), the error now carries a `diagnostics` snapshot taken at that moment: the browser layer, its age and open pages; the last JS dialog and whether it was settled; the document's visibility, focus and ready state and whether a modal dialog is open; whether the page navigated; and the target's box against the viewport, with what lies under the point the click aimed at (or `point: unknown` when that point is not known). Strings are limited to standard tag names and fixed keywords, and numbers are rounded and bounded, so no field value or page text is included. If the page navigated after the click, the snapshot says so instead of describing the new page, and a click that fails close to its 60-second limit skips the probes rather than losing its error.

- **Disk hygiene retires Camoufox engines nothing runs.** camoufox 0.5 keeps every engine version it fetches (about 2.4 GB each) and never deletes one, and an install upgraded from camoufox 0.4 still holds the old engine in its cache root. Once the engine the installed camoufox pairs with has been paired for 14 days, `scripts/disk_hygiene.sh` removes the older version directories and the old root engine's files, through its mount-aware deleter. It keeps every engine while `CAMOUFOX_EXECUTABLE_PATH` or a `camoufox set` choice is in effect, and it never removes an engine a running browser uses, one launched in the last 14 days, or camoufox 0.5's own files. It skips the step when it cannot see the process table or the provisioner is running. A version directory is moved aside before it is deleted, so an interrupted run is finished by the next one.

- The card vault is now backed up with the encrypted credentials every backup run and comes back with a restore, staged and checked but never put in place for you. A vault backup that fails, or a backed-up vault that disappears, fails the backup run and sends the backup-failed alert. `python -m genesis vault init`, `add` and `retire` remind you to run a backup right away.

- When a session files an issue with `gh issue create`, it now sees up to three of the closest existing issues (open, or closed in the last 30 days) found by GitHub's semantic search, with the exact command to close the new one as a duplicate if it is one. The note never blocks, and when it cannot check (for example on gh older than 2.98) it says why. The search itself needs gh 2.98 or newer.

- The PID/task budget alert now names what is holding the budget: the Claude Code sessions, services and cgroups with the most tasks, and a full per-tick breakdown saved under `~/.genesis/logs/pid_snapshots/` while the budget stays at or above 80%.

- When a Claude Code task's output file is growing fast enough to fill cc-tmp (ORANGE or RED), the disk guardian now pauses that command (SIGSTOP, never a kill, never the session itself) instead of only paging. Every pause is recorded and paged with exact `scripts/watchgod thaw <pid>` and `scripts/watchgod kill <pid>` commands, and reminded every 6 hours while it stays paused. The file itself is not emptied yet.

- After pausing a runaway Claude Code task, the disk guardian now also empties its output file once every writer it paused is confirmed stopped, which frees the space at once. A writer it did not see only refills the file (task outputs are appended to), and that refill is detected and paused again. The last 1 MB is kept, owner-only, in `~/tmp/watchgod-truncated` when the home disk has room. A refill is detected and handled again.

- When cc-tmp is its own volume, the disk guardian now keeps a small reserve there (5% of the volume, at most 128 MB) and releases it at RED, so a session's Bash keeps the last few MB it needs while a runaway is dealt with. It is held for one poll after the guardian empties a runaway output in cc-tmp, and the guardian's own reserves no longer read as the disk filling. A reserve that cannot be removed is now reported as not released.

- Machine connectors can now use prepared background-session receipts with explicit cancellation, bounded execution, private full results, and restart reconciliation. These native controls preserve existing tool and owner-approval boundaries and do not enable network access by themselves.

- Added an opt-in same-owner A2A conversation interface: enrolled agents can ask Genesis to reason with its normal background tools, continue bounded conversations, poll/cancel native turns and retrieve scanned results. Owner approval remains in Telegram/dashboard; no daily peer task quota or new listener.

- Add a governed local agent channel and durable A2A pull outbox for explicitly
enrolled same-owner peers, with scoped idempotent collection ACKs and correlated
native replies. Preserve message identity through existing retry queues; add no
HTTP sender, listener or peer approval authority.

- The round reflection can now owe late findings, each credited only under its own round: a review that opened no round, or one that arrived after a fix to its head. Its readers take the review budget's own late flags and round opening times, and read earlier rounds' classes only from reflections that are git ancestors of the one judged. Nothing more is owed yet: the cutover that turns it on is set when the commit gate enforces reflections, and `review_reflection.py status --cutover <time>` reports what it would owe.

- Send peer API outage and recovery notices through Telegram, preserving incidents across restarts and keeping deliberate disablement quiet.

- Expose current peer API readiness in health snapshots and periodic status, and return an unavailable response when an enabled API cannot accept conversations.

- Make 15 GitNexus tools available through portable Codex project configuration, using the repository's pinned launcher; defer source-editing rename and group-metadata sync.

- Support an explicitly approved DeepSeek Pro–Flash qualification run with separate campaigns and the existing serial spending controls.

- genesis-server can now ask the owner to approve a card release with one Telegram button tap. The tap counts only from the first id in `TELEGRAM_ALLOWED_USERS`, on the exact message it sent, unedited, and only once; anything else, or no tap within 10 minutes, refuses. Every request and outcome is recorded in the vault audit log. Nothing asks for an approval yet.

- Add read-only managed worker availability checks for configuration, readiness and namespace-local access prerequisites, including persistence-specific storage, SQLite sidecars and effective executable access. Preserve the installed virtualenv selection in the indexing runner's checks.

- Add bounded serving-status requests for supervised Codex validators, including owned-child cleanup on termination.

- **The card-release approval message now shows the Pay button's label and the names of the other fields the fill will touch.** A `release_request` may carry `button_label`, the visible label of the Pay control the fill tool will click, read from the page. The tap shows it on a `Button:` line, with control and format characters removed, whitespace collapsed and at most 40 characters, or `Button: (not given)` when the request has none. A request may also carry `also_filled`, the names of the non-card fields the tool will fill, each one of `email`, `name`, `phone`, `address_line1`, `address_line2`, `city`, `region`, `postal_code`, `country` or `company`. The tap lists them on an `Also filled:` line, or shows `none`. Any other name refuses the request (`bad also_filled`) before any audit row is written. The audit row records the button label only as its length and the field names as a list. No field values are sent or stored.

- **A test-vault harness for trying card fill with a test card.**
  `scripts/payments_test_vault.py` builds a separate vault that holds only
  Stripe's public test card (labelled `TEST 4242`), serves it on its own socket,
  and asks for each approval in its own terminal (type `y`) instead of on
  Telegram. A branch's card-fill tool can then be run live without the real
  vault, its socket, its cards or the bot. Every approval it shows starts with
  a `TEST:` line. See
  `docs/reference/payment-test-vault.md`.

- **The memory hook now records how long each of its steps took.** Every run of the UserPromptSubmit memory hook writes one `[hook-phases]` line to stderr: the milliseconds spent in each step (startup imports, the heartbeat writes, the server call, the keyword fallback, and so on), whether it ran out of time, and the container's I/O, CPU and memory pressure averages at that moment. When the hard stop ends a stuck run, the line names the step it cut. Claude Code keeps this line in the transcript's hook record and never shows it to the model, so a slow or cut prompt can be traced to the step and the stall behind it. Details: `.claude/docs/proactive-memory-hook.md`, "Phase line".

- **See which skills actually fire.** A new `skill_usage_status` health tool, and `python -m genesis.learning.skills.usage`, report per skill how often it was loaded, typed as a slash command, read, nudged by the skill hook, preloaded by an agent or injected into dispatched sessions, with counts split between interactive and headless sessions. Each skill gets a state (active, zombie, dead, not_nudgeable) decided only by chosen uses, so a skill that is injected into dispatched sessions but never picked does not show as active. It only reads existing transcripts and the session table.

- A graph-engine read that times out now records its own cause on the cutover telemetry row: the process (pid, start, age), how far past its deadline it fired, the awaits it cancelled, and the container's stall time across the wait. Rows also carry a latency spread of answered traversals, the cutover report prints each reset's cause, the ambient worker's log lines carry time and pid, and the engine unit keeps a deeper slow log plus a latency monitor.

- When a database write gives up on a lock (after its retries, or at once for a script), Genesis now records the moment in a JSON file under `~/.genesis/logs/lock_snapshots/`. The warning line names that file. The record includes the SQLite error kind and which processes held locks on that database. It also names this process's own connections to that database: which code opened each one, whether it is inside a transaction, and what statement it is running. Running tasks are counted by name. Connections opened outside Genesis's database helpers are not named. SQLite's own waiters are not visible in the kernel lock table, so the file shows who held a lock, not who was queued behind it. Set `GENESIS_LOCK_SNAPSHOT_DISABLED=1` to turn the recording off.

- Add `genesis db query` for one SQLite-enforced read-only statement, with parameter binding, row limits, escaped TSV and strict JSON. Reject duplicate JSON column names and explicit empty database paths. Cap retained-result accounting at 100 MB and use a cooperative SQLite deadline with a setup-time busy cap; these are not hard process-memory or wall-clock bounds. SQLite-owned sidecars and temporary spill files can still be written. FTS5 works, rtree remains unsupported, and refused statements exit 4.

- **The Tailscale watchdog can keep this node's home DERP region pinned.** Set
  `NETWD_TS_PREFER_DERP=<region id>` in a drop-in on
  `genesis-tailscale-watchdog.service` (off by default) and each run keeps the
  node homed on that region with `tailscale debug force-prefer-derp`,
  re-applying it whenever the relay drifts off the region or tailscaled has
  restarted (including the watchdog's own restart heal), since a restart clears
  the pin. This avoids a one-way tunnel that can follow a home-region change
  when a peer keeps sending to the old region. A lapse (the home left the
  pinned region while tailscaled kept running: either the region was
  unreachable at a netcheck, or the pin was cleared) raises an owner alert
  naming the result of the re-apply, as does a pin that cannot be applied or
  judged; observe mode reports and changes nothing.

- Allow a smaller Pro incumbent baseline in Flash model comparisons while retaining full Flash qualification, detailed incomplete legacy reports, and clear reporting of the baseline's limited scope.


### Changed

- **Changelog entries are now written one file at a time, so two branches
  never collide over them.** Add a file under `changelog.d/` instead of editing
  `CHANGELOG.md`; at release time they are folded into the `[Unreleased]`
  section and removed. Two branches writing two different filenames have
  nothing to merge, which is the entire point — a shared changelog is a file
  everyone edits at the same position, and git reports that as a conflict
  somebody has to resolve by hand.

  Measured against this repository's own queue beforehand: of 49 open pull
  requests, 21 could not merge, and 18 of those 21 conflicted on `CHANGELOG.md`
  and nothing else — every other file in them merged cleanly. A smarter merge
  setting cannot fix it, because GitHub ignores a repository's `.gitattributes`
  in its server-side merge; that was measured against GitHub's own merge engine
  rather than assumed.

  Nothing in the directory is skipped quietly. A misspelled name, a timestamp
  that is fourteen digits but not a real date, an unknown category, an empty
  file, or a nested folder is reported rather than ignored, because a fragment
  that is silently passed over is a changelog entry that never ships and that
  nobody notices — the release section still looks complete.

- The commit and push guards no longer interrupt you with an approval dialog when a command cannot be parsed safely. Previously an unparseable command that mentioned a commit, push, or merge raised a prompt asking you to approve it; now the guard refuses it outright and tells the agent how to rewrite it (use the Write tool for prose, or pass the text as a file). The one approval that remains is the deliberate one: publishing externally with `git push` or `gh pr create` still asks. A crashed parseability probe also now reports itself instead of failing quietly.

- **Graph-enriched recall no longer surfaces memories that recall itself
  hides.** When a memory was returned, Genesis also showed its graph
  neighbours — but that traversal never applied the visibility filter the rest
  of recall uses, so consolidated-away and expired memories were presented as
  live context. They are now filtered out of the graph entirely. On this
  install that changed the neighbour list for roughly a quarter of enriched
  results, and 6.5% of them turned out to have had neighbours that were
  *entirely* hidden memories.

- **The deferred-work rule now says plainly that filing something is not
  deferring it.** It opened with a fix-now bias and then spent a paragraph on
  where a record belongs depending on who owns the work — and that second half
  read as though it answered the whole question. "This is repo work, therefore a
  GitHub issue" became a disposition rather than a filing location, so an in-scope
  problem could get a ticket instead of a fix, and the tracker made that look like
  diligence.

  The two decisions are now separated in the order they should be made: is this in
  scope for what the session is doing, and only then, where does the record live.
  An issue is a public record, not a disposal — in-scope work is filed *and* done,
  often in the same session. The mechanics section of the MCP tools guide carries
  the same note at its top, since that is the page someone lands on when they are
  already reaching for the routing answer and have skipped the question before it.

- **Corrected a stale example about bounded output.** The development guide
  cited the cap on a subprocess's captured output as an example of a size limit
  that cuts without saying so. That limit now reports the true total alongside
  the bytes it kept, so the guide no longer describes it as silent — but it
  also records why the fix is not yet complete end to end: the notice is added
  at the tail of a large field that every later reader trims well before it,
  so a declaration still has to survive its consumers to count as one.

- **A review session no longer closes a pull request it is not reviving.** When
  a PR turns out to be wrong at the premise rather than in its details, it now
  gets a `needs-architecture-session` comment carrying the evidence and stays
  open; closing it belongs to whoever picks up the redesign, at the moment they
  pick it up. An open PR with the reasoning attached is found by listing the
  queue, and its review threads stay where the conversation is happening — a
  closed one is found only if somebody remembers it existed. If a PR should be
  closed and nobody is picking it up, that is a question for the maintainer
  rather than a call the review station makes on its own.

- **A code review that finds an unrelated bug now files it and lets the pull
  request through.** Reviews regularly turn up real defects that already existed
  and have nothing to do with the change being reviewed. The rule is one
  question: does the change work without that fix? If it does, the finding
  becomes a tracked issue and the change merges; only a defect that actually
  breaks the change gets folded into it. This keeps a review from widening into
  an open-ended cleanup, which is what turns a small change into a long series
  of review rounds. A security defect is the exception and is never posted
  publicly before it is fixed.

- **Changing the review gate no longer has to pass the gate it is changing.** A PR
  touching the enforcement-hook surface now runs an expedited lane: both cross-model
  reviewers on round 1 instead of one, findings triaged before any code changes, a
  single batched fix push, and a hard stop at two rounds. It is a stricter-review,
  fewer-rounds trade — the always-fix floor (P1, security, destructive, fail-open) is
  never waived, and a new P1 in the fix itself stops for a human decision. The lane is
  process doctrine layered under the existing round caps, so nothing about the gate's
  code changed. Which secondary reviewer an install uses is now named once in
  `merge_gate.secondary_reviewer` in local config, rather than written into the repo
  where it goes stale; installs without one simply run Codex-only.

- **The development skill now names the six axes review findings actually fall
  along.** Genesis classified 31 review findings from two pull requests,
  attributing each to the commit that introduced the line it points at. Of the 25
  distinct findings left after removing duplicates and one that later proved
  unfounded, 24 fall into six shapes: a signal that does not change when the
  property it stands for changes; a scope or lifetime narrower than the operation
  it guards; an input the type permits but nobody pictured; an equivalence
  claimed but never checked; a failure path that leaves work behind; and a
  caller's explicit option silently dropped. The 25th was a test whose identity
  comparison was unsound in both directions. Those six are now a checklist to run
  against new code before the first review rather than after it.

  The same measurement settled where findings live, which was worth knowing
  because two successive intuitions about it were both wrong. Counting every
  round, 21 of the 31 sat in the original implementation — but a first review has
  no fix code to find anything in, so that number is partly arithmetic. Counting
  only the rounds that actually loop, it is nearly even: 11 findings were already
  in the original code and 10 were in code written to answer an earlier round.
  Neither writing the feature nor answering the review is the dominant source,
  and the checklist applies to both.

  The entry records what would disprove it, with a rate and a sample size rather
  than an adjective, and a note on the attribution method — an earlier version of
  this measurement was inverted by treating a review comment's recorded commit as
  the commit that introduced the line, when it is the branch head at the moment
  the review was posted.

- **Batch code-index commands and their newly spawned descendants now receive the maximum OOM preference without sacrificing their supervisor.** Only the disposable batch child is adjusted; the watchdog and launcher keep their inherited score so they can still pause, stop, and reap it. The preference applies within the kernel's eligible OOM domain, and a lower override or an adjustment that cannot be verified refuses the heavy workload instead of silently weakening protection. Workers already running in an external daemon are not reprioritized by this launcher.

- **The development guide now covers five ways a verification can be complete
  and still be wrong.** All five came out of one long pass through the pull
  request queue, and each had already cost hours. A change can be verified three
  separate ways and still ship a resource defect, because a correctness suite
  cannot go red on one — and where the code sits on a path that gets killed on a
  timeout, and the kill permits what it was meant to refuse, a scan that slows
  down super-linearly on attacker-chosen input is the defect no matter how right
  its answers are; the fix is a timing test. A clean measurement over a corpus of
  real inputs is a lead and not a clearance, because a corpus records what has
  been typed rather than what can be typed, so a null result now has to be handed
  on with its denominator and its blind spot attached. The matrix that replaces
  it is generated from the grammar of what a rule flags rather than from history,
  run against both trees so the baseline can be seen to move, with a note on the
  probe that reads a verdict from an exit code and so cannot tell "ask" from
  "allow". Refuting a reviewer's finding is itself a claim at the same evidence
  bar, and an admitted gap in the argument is the reason not to state the
  conclusion. And a local checker's verdict about a remote pull request depends
  on the tree it ran in, so a claim about a PR's current state has to come from a
  tree at the default branch. Recorded alongside the first of those: a lesson
  about a vulnerability does not need the vulnerability's parameters — the shape
  of the failure is what teaches, while exact inputs, thresholds and affected
  counts only compose into a recipe that keeps working for anyone still on the
  unfixed code.

- **The guide now says which pull request to pick up first when several are
  eligible.** A fix restores functionality that is already broken, so it goes
  ahead of new functionality — but the measurement behind that is about size
  rather than kind. Across the 65 open fix and feature pull requests on this
  repository, features average 2,024 added lines against 804 for fixes, and 67%
  of them exceed a thousand lines against 29%; separately, 86% of pull requests
  over a thousand lines need three or more external review rounds, against 6%
  under two hundred. The gap in findings per pull request is much smaller, so the
  rule is written on the property rather than the prefix: a small feature is not
  deprioritised, and a large fix gets no pass. It remains one input among
  several, not an ordering.

- genesis-development skill: rising review findings now prompt a provenance check; split correct separable additions or revert incorrect ones before adding more changes.

- **The inbox now notices when an evaluation silently skips a URL — and says so
  before it starts acting on it.** The accountability check only ever caught
  give-up *language* ("could not be fetched"); a response that simply never
  mentioned one of its URLs passed, and the omitted link was permanently
  absorbed into the evaluated baseline — invisible, unrecoverable loss. A
  coverage check now requires every scanner-recognized input URL's complete
  parsed identity to APPEAR in the response. Scheme and host case plus one leading `www.` are
  presentation variants; userinfo, port, path, query, and fragment remain
  identity-bearing. The evaluation prompt requires each URL echoed as a
  `**Source:**` line. It ships in **shadow**
  (`url_coverage_mode: shadow` in `config/inbox_monitor.yaml`): it computes its
  verdict and logs opaque stable ids for what it *would* have re-queued (never
  raw signed URLs or credentials), and changes nothing. Set
  `url_coverage_mode: enforce` to have a miss re-queue the item through the
  existing bounded retry path with the partial response preserved.

  Coverage now counts only dedicated `**Source:**` fields and preserves legal
  terminal URL characters. Exact retry-exhausted items remain terminal across
  unrelated edits without suppressing genuinely new siblings. New batch rows
  store versioned, lossless item boundaries in the existing database column;
  malformed text or binary values are safely re-derived instead of crashing
  the monitor or replaying a whole file. Legacy rows whose item boundaries
  cannot be reconstructed retain the bounded file-level storm guard.

  Shadow is the default deliberately. The check is new, and replaying it over
  this install's completed evaluations flags roughly half of older responses —
  which would re-queue them into a retry path that parks a whole file after
  `max_retries` with no notification. Watch the shadow log first; enforce when
  the rate reflects responses written under the `**Source:**` prompt.

  **The link the evaluator is asked to fetch is now the link you wrote.** The
  coverage check keeps every trailing character so it can fail closed on
  ambiguous punctuation, which is right for deciding identity and wrong for
  telling the evaluator what to open: a markdown link, a quoted URL or a URL in
  a code span used to be handed over with its wrapper still attached
  (`https://example.com/foo)`), so the fetch asked for a page that does not
  exist. Measured across this install's stored evaluations and current inbox,
  340 of 18,119 extracted links (1.9%) ended in wrapper punctuation; that is
  now 11 (0.06%).

  Trimming is structural rather than a guess at prose: a bracket or quote is
  dropped only when nothing in the link opens it, so a balanced
  `/wiki/Foo_(bar)` and an IPv6 address keep their brackets. Sentence
  punctuation alone is never enough — a link ending in `;` or `!` is left
  exactly as written, because nothing proves that character is prose and
  asking for a different address than you saved is the very loss this check
  exists to catch. That is what the 11 remaining are: 9 links ending in a
  bare comma or semicolon, plus 2 templates the check already skips. Links
  wrapped in markdown emphasis (`**`) are also out of scope and still reach
  the evaluator wrapped.

  The check itself is unchanged and still compares the link as written; it
  simply also accepts the trimmed form **of that same link**, so an answer
  citing exactly what it was asked for counts. A different link that merely
  looks similar still does not.

  One known false positive, left documented rather than patched: a response that
  cites a URL *without* its tracking or query parameters (the prompt asks for
  verbatim) reads as a miss. Stripping tracking params does not fix it — measured
  at 0 of 146 rescued — and stripping the whole query string would merge
  genuinely different URLs (`watch?v=A` and `watch?v=B`), which is worse than the
  false positive.

- **A note written directly above a URL now travels with it.** The natural way
  to annotate a saved link — intent on one line, URL on the next — used to be
  split into two unrelated items, so the evaluator never saw why the link was
  saved. Adjacent prose now rides in the URL's own item (a blank line keeps a
  thought independent), and an already-evaluated line elided from a delta
  leaves a separator so unrelated lines can't be mistaken for annotations.

- **Inbox items are now evaluated one at a time by default**
  (`items_per_eval: 1`, previously 5), with default effort `high` and a 20-minute
  evaluation timeout. Each link gets an evaluation session to itself — no more
  depth dilution across batch neighbours — at the cost of more (smaller)
  sessions. Raise `items_per_eval` in `config/inbox_monitor.yaml` to trade
  depth for fewer sessions.

- **Editing an inbox file before its previous snapshot was evaluated no longer
  burns a retry or strands sibling batches.** Startup recovery atomically
  returns every pre-dispatch row to the bounded retry lane without incrementing
  its retry count. Complete outstanding work is rebuilt from the current file
  and completed baseline, including a never-created tail when a crash interrupted
  multi-batch row creation.

- **An inbox evaluation is not baselined before its synchronous durable side
  effects finish.** Follow-up and build-lane writes still treat ordinary local
  errors as non-fatal, but cancellation or process exit leaves the batch
  non-completed and recoverable instead of permanently hiding a missing side
  effect behind a completed baseline.

- Quiet hours are now OFF by default, so a message you schedule for 01:30 arrives at
  01:30. Previously outreach shipped with a 22:00–07:00 window that silently held every
  non-urgent category until morning, with no way to turn it off or to override it for a
  specific send. To re-enable it, set real times under `quiet_hours` in
  `config/outreach.yaml` (or your local overlay); `start` equal to `end` means disabled.

- When a change keeps failing review, both escalation tiers now offer the remedy that was missing: hand it back to be rebuilt. Every option those two tiers used to print assumed the change was salvageable — audit harder, redesign, narrow, shelve — so a change whose *premise* was wrong could only be patched round after round, which is the one situation more review cannot fix. They now name the fork explicitly, say what evidence distinguishes the two cases (handing back a change that only needed polish is the more expensive mistake), and say plainly that work handed back was not wasted.

- The merge gate now weighs review findings against what a change costs to get wrong, instead of holding every change to one bar. A serious finding — a P1, or a Critical/Major from CodeRabbit — still stops any merge outright, in every lane. Below that, a change to an enforcement hook, a CI workflow, an API surface or a migration blocks on the same two findings it always did, while ordinary code, and prose or test-only changes, tolerate more before the gate refuses. Configuration counts as ordinary, not as documentation. The verdict names the lane and the threshold it used, so a merge that passes with findings outstanding says so plainly rather than looking clean.

- The project instructions loaded into every session no longer carry issue or pull-request numbers. A ticket number dates the sentence around it — usually a status like "not built yet" — which quietly stops being true the day the ticket closes, and nothing fails when it does. The two that had accumulated are rewritten to say what stays true, and a test now fails if another is added.

- **When the review escalation cap blocks, its remedies are now put to you
  directly instead of being relayed.** Previously the gate printed its four
  options and the session retold them to you in its own words — which is how a
  real block once reached a user with the first option missing, a fourth
  invented, and "ship as-is" added, the one outcome that gate exists to prevent.
  Now, while the cap is the tier your next commit would hit, the gate's own
  question is appended to the next question the session asks you through the
  question tool, carrying the gate's own wording — the option text is a verbatim
  excerpt of the gate's message — in the gate's own order, with "hand it back"
  first. The session does not author those options, so it cannot drop, reword or
  pad them. Nothing else changes: the acknowledgement comments mean exactly what
  they meant before, and no gate reads your answer back. Turn the substitution
  off with `GENESIS_GATE_MENU_DISABLED=1` — any value except `0`, `false`, `no`
  and `off`, which name the switch's own position and so mean "leave it on" —
  or by creating `~/.genesis/config/gate_menu_disabled`. With it off, the gate
  blocks and the session asks exactly as it did before.

- **What the menu does NOT cover, stated so the claim is not wider than the
  thing.** It appears only for the escalation cap: the round-2 mode-switch tier
  and the final-round terminal still rely on the session relaying their options,
  and the terminal is excluded deliberately — its options are different, so
  showing the cap's menu there would be the very substitution error this exists
  to prevent. It only reaches you through the question tool, so a session that
  relays the block as ordinary prose is not covered at all, and a session that
  happens to ask four questions at once will not get the menu appended (the tool
  caps a call at four, and a rejected call would cost you the session's own
  questions too). It stops being offered as soon as the gate ACCEPTS your
  acknowledgement — which is not the same as a commit landing: the gate clears
  the streak the moment it recognises the acknowledgement, deliberately even if
  a later rule then blocks that same commit — except after "hand it back",
  where the gate deliberately
  forbids that acknowledgement, so the menu keeps being offered on that branch
  until you leave it or use the off switch. If a session is sitting in one
  worktree and committing into another, the menu will not appear. And a session
  in a long-lived worktree created before this change keeps that worktree's own
  frozen hook configuration, so the commit gate there still blocks — its entry
  is redirected to the current script — while the menu, which needs a new entry
  that worktree does not have, stays silent until the worktree catches up with
  the main branch.

- **Cleaning up an old worktree no longer destroys anything.** The daily cleanup
  used to move a worktree aside and then permanently delete it a week later, and
  restoring one deliberately did not bring back edits that had never been
  committed — so a worktree holding unsaved work could be cleaned up and, seven
  days on, be gone for good. It is now packed into a verified archive that is
  kept indefinitely, with a patch of any uncommitted edits saved beside it. On
  the install this was developed against, the very next cleanup would have taken
  twenty-five worktrees, two of which held uncommitted work.
- **The archive is only trusted once it has been read back in full.** Writing it
  and checking the first entry is not a check: a half-written archive passes that
  and then the original is deleted. Every entry is now walked and the count
  compared against the source before anything is removed, and if any part of that
  fails the original is left exactly where it was.
- **Worktrees that are still being worked in are left alone**, including ones with
  a paused rebase or merge, ones containing another worktree, and ones explicitly
  marked as in use. Each worktree now also gets a single plain-language state —
  in use, protected, fresh, at risk, or ready to clean up — so what the cleanup
  decided and what any other view reports can never disagree.
- **Asking for the machine-readable report now returns only that.** When the
  report was requested without network access, an explanatory note was printed
  ahead of the data on the same channel, which made the output unparseable for
  anything reading it programmatically. The note now goes where a person reads it
  and a parser does not.
- **An archived worktree is no longer readable by other accounts on the
  machine.** A reaped worktree is a verbatim copy of a working tree, which
  routinely holds a private key or environment file. Rolling that into an
  archive under ordinary settings republished it at world-readable permissions
  inside a world-listable directory, so a file that was private in the worktree
  stopped being private the moment it was archived. The archive, the directory,
  and the recovery patch are now created private from the outset rather than
  tightened afterwards.
- **The archive is forced to disk before the original is removed.** Reading a
  freshly written archive back proves the bytes are in memory, not that they
  would survive a power cut, and publishing the finished name guarantees that
  nobody sees a half-written file rather than that the name itself survives a
  crash. A badly timed power loss could therefore keep the deletion and lose the
  archive — the one outcome this whole change exists to prevent.
- **An archived worktree whose name is legal on disk but illegal as a git
  reference now still gets its safety anchor.** Names beginning with a dot, or
  containing a space and several ordinary punctuation characters, were rejected
  when the anchor was created; the archive was still recorded, so the anchor
  that keeps the archived history reachable was silently missing and a later
  cleanup could discard it.
- **Recovering a worktree whose branch was deleted now produces a working
  checkout.** This is the common case rather than an exotic one, because the
  task runner deletes the branch right after cleanup. Recovery previously moved
  the files back, reported success, and left a directory that git refused to
  operate on. It now rebuilds a real checkout at the recorded commit and tells
  you the one command that restores the branch name.
- **A worktree that contains another worktree is left alone.** Moving the outer
  one takes the inner one's files with it and can orphan its history.
- **A cleanup run that cannot list the worktrees now says so instead of
  reporting that there are none.** Failure and emptiness were the same answer,
  so a timed-out or failing scan published a confident, valid-looking view in
  which every worktree had vanished, and nothing downstream could tell that from
  the truth. Such a run now reports the error, changes nothing, and leaves the
  previous view in place.
- **An archived worktree now carries its own history, instead of pointing at
  history kept somewhere else.** Previously the archive held the files while the
  commits stayed reachable only through a reference outside it — first the
  branch, then a marker created just before cleanup. Every one of those is
  something another process can remove, and each removal left a perfectly intact
  archive whose contents could no longer be reconstructed. The archive now
  contains the commits themselves, so nothing outside it has to survive for a
  recovery to work. Verified by deleting the branch, expiring every reference and
  running a full garbage collection until the commit was provably gone, then
  recovering it from the archive alone.
- **The commit recorded for an archive is the one it actually contains.** The
  identifier was taken from a scan that runs over every worktree up front and can
  be minutes old, so anything committed in that window was archived but recorded
  under the earlier commit — sending a later recovery to a commit the archive
  never held. It is now read at the moment of archiving.
- **A worktree that contains its own bookkeeping file no longer loses it.** The
  internal metadata file was written unconditionally, which replaced a file of
  the same name belonging to the worktree — in the worktree and in the archive at
  once, since the archive is made from the moved copy. The worktree file is now
  preserved alongside ours, under a name that says where it came from.
- **A cleanup run started from inside a worktree no longer archives the
  directory you are standing in.** The check for processes using a worktree
  skipped the running process and its parent shell, so a hand-run from a cold
  worktree could not see the one user it definitely had. Entering a directory
  refreshes no timestamps either, so an otherwise idle worktree passed the
  staleness test while somebody was working in it.
- **Worktree names that are not plain text no longer break the scan.** A name
  containing a line break was split into a truncated name matching nothing on
  disk, and a name containing bytes that are not valid text crashed the scan
  outright rather than reporting a failure. Names are now read as the bytes they
  are, with records separated unambiguously.
- **An archive with a very long name can be opened again.** The temporary
  directory used during recovery was named after the archive, which could push it
  past the length limit the filesystem enforces — so an archive that was created
  successfully could never be recovered. That name is now a fixed length.
- **Recovery no longer buries the archive one level down.** When creating the
  worktree fails after the destination has already been made, moving the archive
  into that destination places it inside rather than at it. Recovery now refuses
  and says so, leaving the archive intact for another attempt, instead of
  reporting success with everything one directory deeper than stated.
- **A branch is only treated as merged when the merge matches it.** The check
  accepted any merged request that had once used the same branch name, whatever
  it was merged into and whatever has been committed since — so a branch carrying
  unmerged work could be archived early. It now requires the default target and
  the same commit.

- **The OpenClaw completions endpoint now requires a bearer token, and existing
  installs must add one.** `POST /v1/chat/completions` previously accepted
  unauthenticated requests; it now refuses them, matching every other `/v1/*`
  route. The dashboard's session gate exempts that whole prefix by design —
  machine callers have no browser session — so each such route carries its own
  bearer check, and this one now does too.

  **What to do when you update.** Set `GENESIS_MCP_HTTP_TOKEN` in `secrets.env`
  if it is not already set, and put that same value in your OpenClaw provider
  config as the API key — it is an ordinary OpenAI-compatible client, so the
  header needs no custom code. Until you do, the endpoint answers 401. With the
  variable unset it answers 503, and the boot-time warning now names every
  surface that depends on it rather than the voice API alone.

  The repo's own setup instructions for that client were stale in the same
  direction — a placeholder key and the wrong port — and have been corrected.

- **Two refusals on the `/v1` surface got less confusing.** A non-ASCII
  `Authorization` header raised inside the constant-time comparison and surfaced
  as a 500 instead of a 401; the comparison is now on bytes. And a quoted
  whitespace-only token in `secrets.env` counted as configured, which a blank
  credential then satisfied; it is stripped now, as the dashboard password
  already was. Both had been true of every `/v1/voice/*` route since they were
  written, and are fixed once rather than three times because the check finally
  has a single implementation.

- Review continuation now counts distinct GitHub-reviewed commit heads. In Claude Code, ordinary pull requests require a fresh native user approval for every review request and fix commit after four reviewed heads, with round six and later strongly discouraged. Review-gate changes use the expedited two-round lane plus one exact-head confirmation. Autonomous sessions deny actions that need approval, unreadable evidence never becomes a zero-round allowance, and protected actions must use separate commands so one approval cannot cover a branch switch, multiple requests, or multiple commits. Dynamic shell-expanded review-comment bodies take the conservative ask/deny path.

- Published prose no longer carries em dashes at all: the anti-slop scrubber rewrites both the spaced form and the bare word—word form to two hyphens closed up (word--word) on every publishing path (email, Discord, Medium, content review copies), and the voice-master, genesis-voice, and deliverable-builder style rules prescribe the same form. Conversational output keeps its em dash. The scrubber also no longer flags its own output when the rewrite lands next to inline code. Genesis's soul gains a Socratic clause: serious discussions advance by real questions that expose the load-bearing assumption, not by lecturing.

- The merge gate no longer requires a scheduled `code-review` marker by default; `leaks` alone is the shipped default, and it remains irreducible. A required kind is a promise that some producer emits its marker, and for `code-review` none does: measured 2026-09-16 across every owner-authored comment and review on all 51 open non-draft PRs and the 40 most recently merged ones, `leaks` markers appear 47 and 31 times respectively and `code-review` markers appear zero times — never emitted at any head, not merely stale at an older one. The gate therefore blocked 51/51 open PRs on a kind nothing produces, so every recent merge necessarily carried `# scheduled-review-override`, which demoted an exception valve into the standard merge incantation and, being a single sigil, disarmed the leaks lane along with it. Nothing about leak protection changes: `leaks` stays required and carries its mechanical ancestor relief, and the blocking `leak-detector` CI job still runs per head. Per-head *quality* review is unaffected too — Codex-at-head freshness and the inline-finding thresholds both have live producers and still block. An install that does run a `code-review` routine re-arms the kind by naming the larger set in local config: `merge_gate: {required_scheduled_reviews: [code-review, leaks]}`. Because the default is now minimal rather than maximal, a discarded config value narrows rather than tightens, so the fallback now prints a NOTE when the key was visibly declared in the file — the same contract `required_ci_workflows` already carries.

- **The guard-replay corpus now says how old it is, how much it is missing, and what it could not see — and a limited run names what it was limited from.** Two ways the tool could overstate what it had measured, both silent. A cached corpus was returned with nothing said about its age, so a rate computed over a stale corpus read exactly like one computed over the whole transcript tree; the run now prints the cache's build date, its age, and how many transcript files have changed since — on stdout beside the corpus size, where the number that gets quoted actually lives. The date is rendered in UTC rather than local time, deliberately: this line exists to be pasted into a pull request, and a local timestamp with a timezone abbreviation would publish the operator's region and the hour they were at the machine. Separately, `--limit` truncated without a word: `corpus: 500 unique real commands` against a corpus of 149,385 is a subset wearing the grammar of a full sweep, printed one line above a rate. A limited run now names the denominator, the flag, and the fact that it is the first N in sort order rather than a sample — the corpus is sorted, so a limited run is biased toward whatever sorts lowest.

  Five ways this line could have lied are now reported instead, and the enumeration is deliberate — a universal claim here would be the same overstatement the change exists to remove. An unreadable subdirectory used to shrink the denominator silently, because a directory walk swallows that failure and raises nothing, so a partly-seen tree produced a confident count; the walk now counts what it could not read and marks both figures floors. A transcript rotated away mid-walk is counted the same way rather than quietly dropped. A cache whose timestamp is in the FUTURE — a clock step, a restore from a machine that was ahead — used to render as "0 minutes ago" with "0 changed", the strongest possible currency claim produced by a broken input; it is now reported as a future timestamp with age and drift declared unknown. A cache that exists but cannot be read says so instead of vanishing from the output, and an unrenderable timestamp no longer aborts a run that has already paid for a full corpus load.

  Separately, the cache is now stamped with the moment its build STARTED rather than the moment it was written. The write lands at the end of a walk that takes minutes, so a transcript appended during the build carried an older timestamp than the cache and read as unchanged — the corpus was short and the line said it was current. Stamping the start reports anything touched during the build as drift, which is the honest direction. Two known cases remain unreported and are named rather than implied: a transcript whose own timestamp moves backwards (a restore, a checkout) still reads as unchanged, and a cache timestamp far in the past renders as an absurd age rather than an error.

  Deliberately no staleness threshold and no automatic rebuild. A strict freshness check fires on every single run, because the newest transcript is always the caller's own session and is seconds old, and a full rebuild walks the entire transcript tree. The other unusable-cache branches rebuild because the cache cannot be READ; a stale cache reads fine and is merely shorter, so the defect is the silence rather than the staleness, and naming it is the fix.

  Also corrected: the `bash_safety` replay declaration listed the hook beside five that this repository wires in `.claude/settings.json`, and it wires none for that one — it never has, in any ref. Where the hook runs at all it is registered by hand in a user-level settings file outside the repository, so on a fresh clone it is not wired, and its registration cannot be read from inside the repository at all. A test now binds that claim to the settings file rather than to its own prose, so it fails if the wiring ever appears.

- Inbox evaluations now receive the complete canonical `evaluate` and
  `user_evaluate` skills in a deterministic system prompt. Inbox scans stop
  before changing durable state if any required instruction component is
  missing, unreadable, or empty, instead of silently using a reduced fallback.

- A question needing an answer must be asked through the question tool, carry
  the context to answer it, and be **re-asked** if it goes unanswered while it
  still blocks. Two failures were compounding: a prose question in the body of
  a message gets missed, and one offering bare options asks the reader to
  supply the analysis the session owed. Measured: two decisions sat unanswered
  for 11 and 12 days while being restated each turn and worked around.

- **A fail-open in a local development guard is no longer withheld from the
  public tracker as though it were a security defect.** The filing rule said a
  security defect — *"an unpatched bypass, a credential exposure, anything
  exploitable"* — is never filed publicly before it is fixed. That is right, but
  the bare word "bypass" swept in every guard fail-open, which is most of what
  this repo builds. The rule now turns on **who gains**: a defect is
  security-class when disclosure hands someone a capability they do not already
  have. A local hook fails that test on its own terms — it runs only where an
  install wires it, whoever can trigger it already has commit access to that
  checkout, and the worst outcome is an under-reviewed change reaching a pull
  request that still waits on maintainer approval, which is the state of any PR
  authored without those hooks.

  The rule also now says to verify the chain rather than the wording. The
  specific misfire it was written from: a deny message naming *"secrets"* was
  read as proof that credential scanning was the defeated control. It was not —
  that message describes what `--no-verify` does to git's own pre-commit chain,
  while this repo's privacy controls are a blocking CI job and two PreToolUse
  hooks, none of which that flag reaches.

- **The genesis-development skill now carries the prior that decides whether a
  guard should refuse at all**, ahead of its existing section on which direction
  a guard should fail in. Advisory is the default; escalating to a block needs a
  specific, credible, measured reason, and neither "for safety" nor a single
  incident is one. The section is explicit that this answers the VERDICT
  question — advisory or block, when the guard evaluates — and not the DEGRADE
  question of what a guard does when it cannot evaluate, which the existing
  fail-semantics section decides per boundary and whose contract test requires
  many hooks to fail closed. It also scopes itself to guards rather than
  approval boundaries, described as a class (anything gating autonomy,
  spending, data destruction, or publication) rather than as a list of names.
  An adversarial audit of the first draft found it stating all three of those
  too broadly — text that would have contradicted an enforced test, banned a
  shipped ask pattern, and read as a four-member denylist — which is recorded
  here because the audit requirement on prompt surfaces is what caught it.

- **Updated the routed model versions.** The routing table had fallen behind the providers, so several call sites were asking for models that have since been superseded. Providers now point at current models. Three of the bumps cost noticeably more per token, so check them if you watch spend: the adversarial-counterargument lane (`openrouter-gpt55`, now GPT-6 Astra) is about 4x its previous rate, the Kimi provider (`kimi-k2.5`, now Kimi K3, first in the code-auditor chain) about 5x, and GLM 5.3 through ZenMux about 1.7x. Sonnet 5 and GPT-5.6 are cheaper than the models they replace. Four of the bumped providers (xAI, Qwen, MiniMax and the direct OpenAI route) are wired to no call site, so nothing routes to them yet. This change deliberately does not touch the NVIDIA NIM DeepSeek provider: its model reached end of life and now answers HTTP 410, 18 call sites reference it, and repointing them is its own decision.

- **The merge gate now scores Devin's review findings like Codex's.** A Devin severe bug or critical security finding blocks a merge in every lane, and a non-severe one counts toward the review score like a Codex P2; a finding Devin posted twice counts once, and Devin can withdraw its own finding with a `✅ **Resolved**` reply. CodeRabbit scoring is unchanged. When Codex has not reviewed a PR's head, `# substitute-review` lets a Devin or CodeRabbit review of that exact head stand in for it — the owner approves each use in conversation, the gate refuses it in unattended sessions, and keeps the base-branch check and the head binding that `# stale-review-override` drops.

- **Seven routing providers whose names encoded a model version were renamed, and old names keep working.** `glm51`, `kimi-k2.5`, `minimax-m25`, `openrouter-deepseek-v4-flash`, `openrouter-gpt55`, `gpt-5.4` and `openai-gpt5` now carry names that do not go stale the next time the underlying model is updated. A rename would normally break every install that keeps a local routing overlay, because the overlay is the one place the change cannot reach — an override on a renamed provider would silently vanish, and a chain pinned to an old name would be discarded in favour of the shipped chain, both without an error. Old names are therefore migrated automatically wherever they appear: in the overlay, in a chain, and at the command line, so `--model openrouter-deepseek-v4-flash` still resolves. Your overlay is also brought up to date the next time the dashboard saves, rather than carrying the old name indefinitely. If an overlay somehow names both an old and a new form, the new one wins; if a chain names both, it collapses to one entry rather than routing to the same provider twice.

- **Build sessions now verify before they open a PR, when this install can show the result.** The genesis-development skill adds a build step: a change whose effect this install can show, and whose run can be undone, is run from the branch before the PR opens, risky surfaces ship behind a shadow flag, and when acceptance needs the owner's hands the session iterates with them and opens the PR once they confirm. Runtime code keeps its post-merge `E2E:` obligation until a supported way to run a branch in the live runtime exists.

- Genesis now pins Claude Code **2.1.280** (was 2.1.246); `scripts/update.sh` aligns both the container and the host. This range fixes a startup stall and a headless hang on the recovery path, and makes the `opus` model alias resolve to Opus 5.5. Genesis also turns off syncing your claude.ai skills and plugins into Claude Code (`syncClaudeAiSkills` / `syncClaudeAiPlugins` set to `false`). It only does this where you have not set them yourself and nothing of that kind is synced yet; see the settings-defaults entry for what happens where something is.

- **Existing installs now receive Genesis's Claude Code settings defaults**, not just fresh ones. On the next `update.sh` run or daily settings check, the container's `~/.claude/settings.json` gains any of these that are not already set. Values you have set yourself are never changed.
  - `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2`: one level of nested subagents.
  - `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH=8192`: MCP tool descriptions longer than Claude Code's 2,048-character default now reach the model whole, for roughly 1,200 extra tokens per session. This affects some third-party code-intelligence tools. To use a different cap, edit `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` in `~/.claude/settings.json`; your value is kept from then on. The `CC_MCP_DESCRIPTION_LIMIT` variable only takes effect where the key is not yet set, such as a fresh install, and a value below 2,048 is refused with a warning.
  - `syncClaudeAiSkills` and `syncClaudeAiPlugins` set to `false`: stops syncing your claude.ai account's skills and plugins into this machine's Claude Code. This is only applied where nothing of that kind has been synced yet. Turning sync off makes Claude Code hide skills or plugins it already synced and move them to `.trash`, and turning sync back on downloads them again rather than restoring them. So where anything is already in the synced folder, Genesis sets the key to `true` instead, which keeps syncing exactly as before and records the choice, and prints a note once saying how to turn it off. The choice then survives signing out of claude.ai, which empties the synced folder. While a machine is syncing, Claude Code keeps an account folder there even with no items left, so a syncing machine is recognised on its first update. Where the folder cannot be read, Genesis leaves the setting alone and prints a note on every update. On a machine where the folder is empty or missing, including one that has never synced, this update turns sync off; the install output and the daily settings check say so, and to sync your claude.ai skills or plugins into it, set the key to `true`. Use the JSON values `true`/`false`: Claude Code treats anything else, including the string `"true"`, as off.

  The host VM is not changed. Adding these defaults is not reported as a degraded deploy.

- The review-round doctrine now says one thing: **round 4 is terminal.** Past four
  reviewed heads the decision is to merge with the outstanding issues filed, or to
  send the change back for rework — a fifth round happens only where you
  explicitly authorise one, and that authorisation is asked again every round
  after. The retired `FINAL_ROUND_CAP = 7` is deleted rather than deprecated a
  second time: no gate consulted it, while its value had been chosen to avoid
  colliding with the four-head boundary, so a dead tier was still shaping the live
  one and reading as though a seventh round were real. The `final-round-accept`
  sigil is still recognised and still refused out loud, so an older checkout is
  told it is dead rather than silently ignored.

- **Embedding writes now go to the cloud backend first.** Storage previously led
  with the local Ollama backend, on the reasoning that a background write has no
  deadline so the slower local path costs nothing. That left out the CPU: local
  embedding is inference, and on a host without a GPU every memory write burned
  cores the rest of the system was contending for. Measured through the real
  chain, 20 calls each on representative memory-length texts: local p50 2395.8ms
  / p95 2952.9ms, cloud p50 207.8ms / p95 399.0ms — **11.5x at p50**. Ollama
  stays as the second rung, so a cloud outage degrades to local writes rather
  than losing them, and recall was already cloud-first. The change is to the
  chain builder's DEFAULT rather than one call site, because fifteen components
  construct an embedding provider without naming a chain — six in the package
  (execution traces, stale-embedding repair, procedural embedding and its
  promoter, the procedural MCP tool, the session-awareness worker) and nine
  maintenance scripts, the busiest being the MCP server that backs memory,
  reference and knowledge writes for every session. Flipping only the runtime
  wiring would have left them on local inference. Storage stays on the ordinary
  rate tier rather than the paid priority tier recall uses; the standalone memory
  MCP server now builds a separate priority-tier provider for recall, as the full
  runtime already did, so interactive recall does not inherit the ordinary tier's
  queue. Asking for local-first explicitly still works, and Ollama remains the
  fallback rung.

  **Every embedding chain now uses exactly one model.** Backends are only
  interchangeable if they produce vectors in the same space; matching dimension
  is not enough. DeepInfra and the local Ollama model are the same Qwen3 0.6B
  model (measured cosine 0.9997-0.9999 on identical text), but the DashScope
  backend is a different model and is no longer mixed into a Qwen3 chain — before
  this, an install with DashScope and Ollama but no DeepInfra would have written
  DashScope vectors into its Qwen3 memory collection. On such an install recall
  now runs on the local model only, which is slower but no longer compares
  vectors from two different models. DashScope is still used when it is the
  only backend configured. A provider assembled by hand from
  backends in different spaces now refuses to start, and the embedding cache is
  keyed by vector space (a one-time cold cache after updating). Every embedding
  backend must now declare the vector space it produces; a custom backend that
  does not is refused rather than silently mixed in. The dashboard's embedding
  panel now reads the chains the server actually built and names the model that
  wrote the server process's most recent vector, so a fallback to the local
  model during a cloud outage is marked as a fallback, with the configured
  primary beside it. (Writes made by a session's own memory tools run in a
  separate process and are not reflected there.) The LongMemEval harness,
  which writes into brand-new throwaway collections, now keeps its cloud-first
  chain even when the local model is a different one.

  **What this changes about where your data goes.** Memory and knowledge content
  is now sent to the cloud embedding provider at WRITE time, not only at recall.
  Retrieved bodies already left the host on the read path — the post-retrieval
  reranker scores `(query, document)` pairs remotely — so this is a change in
  when and how often, and it adds a second vendor to the set that sees stored
  text. Anything that must not leave the machine should not be stored in a body
  that retrieval can surface; that is true before this change and remains true
  after it.

- **The inbox now acts on its URL-coverage check, and tells you when it gives up
  on something.** An evaluation that never cites one of its input URLs as a
  `**Source:**` field is re-queued instead of being marked done. This check ran
  in observe-only mode until compliance was measured: across the first 42
  evaluations under the citation rule, every one cited its sources and none would
  have been re-queued. To observe without acting, set `url_coverage_mode: shadow`.
  Previously, an item that used up its retries, or a file that tripped the
  repeated-failure guard, simply stopped being evaluated, with no signal. Now you
  get an alert naming the file (by its path inside the inbox folder) and, where
  individual items stopped, each of them, with links shown without query strings
  or token-like path segments. The guard's retry lane now stops the file's URL
  retries instead of re-checking it on every scan. An approval request that ends
  unanswered no longer uses up one of the item's retries, which could leave the
  item never evaluated again. A `**Source:**` line inside a list item or
  blockquote, including nested ones such as `- > **Source:**`, now counts as a
  citation. With network parking live (`resilience.parking_mode: live`), an
  outage no longer spends an item's retries. Invalid `items_per_eval`,
  `max_retries`, `timeout_s` and interval values in the config file now fall back
  to their defaults with a warning, including `true`, fractional and infinite
  values, and the settings tool rejects such values for the integer settings it checks
  instead of reporting them applied.

- **The tmp watchgod now guards whole disks instead of policing a 500 MB budget, and never kills a session or deletes live work.** It watches every filesystem Genesis writes to (root, `$HOME`, `/tmp`, the cc-tmp volume), including a separately limited volume that shares a device with root, and measures the space that is really left — whichever is closer of what `df` reports and the btrfs quota on the volume, plus btrfs metadata headroom, which `df` cannot see. Tiers follow free space and time-to-full: YELLOW logs who is writing, ORANGE starts a reclaim pass (caches, `~/tmp` older than 2 days) and pages a warning, RED releases a preallocated reserve file, starts last-resort reclaim and pages. A page is recorded as sent only once it is actually queued, so a full disk cannot swallow the emergency page. Reclaim runs (daily and under pressure) never overlap, every step that deletes working files (`~/tmp`, background sandboxes, cc-tmp) refuses to run when it cannot see which files are in use, uninstall stops them, and `scripts/update.sh` now restarts the watchgod whenever its code changed, so an update actually reaches it. `scripts/watchgod status` shows the state. cc-tmp gets real retention instead: an hourly sweep removes what ended sessions left behind once nothing holds it and nothing has touched it for 7 days. Install-local overrides (observe mode `WATCHGOD_ACT=0`, thresholds) go in `~/.genesis/config/watchgod.local.conf`. The old `CC_TMP_BUDGET_MB`, `SACRED_GROUND_MB` and `CC_TMP_CAPACITY_MB` settings no longer do anything.

- **The merge gate now requires a Codex review of the exact head commit; a clean Codex re-review comment counts only when its commit id resolves to that head (see the later entry on clean Codex reviews).** When Codex re-reviews and finds nothing, it posts a comment that names the commit only by an abbreviated id, and an abbreviated id does not identify the commit being merged. The gate still reads that comment and mentions it in its block message. When a clean comment is Codex's only word on the current head, merge with an owner-approved `# substitute-review` resting on a Devin or CodeRabbit review of that exact head. `--check-pr` no longer reports `ok (clean comment at head)`.

- **The decision registry validates through one strict pydantic model.**
  `DecisionSpec` and `Fallback` are now pydantic dataclasses with strict field
  types, so loading a file and constructing a spec in code run the same rules.
  - Identifiers have a grammar (ASCII snake_case), not just a type.
  - Text fields must be real strings, and `latency_budget_ms` a real integer.
  - Score levels must be distinct and ordered, and every threshold/band rule
    runs on construction.
  - `pydantic` is now a declared dependency. It was already installed through
    fastapi, litellm and mcp.
- **One YAML entry point.** Every parse failure is a `RegistryError`. The
  loader rejects duplicate keys, aliases, merge keys (`<<`), unhashable keys
  and non-canonical numbers (YAML 1.1 reads `0200` as 128, `1:30` as 90 and
  `0:0.5` as 0.5). An unreadable or mis-encoded file is a `RegistryError` too.
- **Overlays only override.** The shipped registry must validate on its own
  before any overlay is merged, so an overlay cannot mask a broken shipped file.
  - The `decisions.local.yaml` overlay is found where every other config
    overlay is: `~/.genesis/config/` first, then beside the shipped file.
  - An overlay may not add a decision or an option, and may not change a
    question's type, what it consumes, or its criteria.
  - An overlay may not set any field to null.
  - Unknown top-level keys and falsy non-mapping overlays are errors.
- **Gate verdicts name their fallback.** Legacy mode returns a `legacy`
  verdict that routes to `fallback.legacy` and needs no score. A
  calibrated-mode score abstains unless it carries a well-formed calibration
  version. `DecisionSpec.fallback_for()` resolves a verdict to its declared
  behaviour.
- **No site declares an outcome source it does not have.** Six sites named
  label sources that either did not exist or held other labels, so all are
  unset until a real persisted signal exists.
- **Three ego proposal decisions are registered:** reconcile, scope and
  realist, each described as its call site actually behaves. The registry
  header states that coverage is incomplete.

- **Inbox alerts identify stopped items by line number.** An alert about an item the inbox stopped evaluating now names each link by its site and the line it sits on in the file (for example `example.com — line 12`), instead of a partly masked link path. No part of a link's path or query string, where share links keep their access tokens, appears in the alert any more.

- **Wrapped untrusted content carries a keyed id on both boundary markers.** `ContentSanitizer.wrap_content` now emits `<external-content … id="…" note="…">` and a matching `</external-content id="…">`. The id is derived from the content and a per-install secret (created once as `~/.genesis/boundary_key`, mode 0600), so the same content always wraps identically while text inside a block cannot compute the id that would close it. The opening marker states that the block ends only at the closing marker with that id, and the voice, distillation and inbox prompts say so too. Wrapped text itself is passed on unchanged, apart from Unicode line and paragraph separators becoming newlines and a missing value becoming empty text. `strip_boundary_markers` removes markers with or without an id, and leaves tags that only start with the marker name alone. Injection-pattern scanning also checks the text with every line boundary as a newline and with invisible and control characters removed, so perimeter checks (email, inbox) can block some inbound messages that previously passed. Code that matched the literal `</external-content>` closing marker must now allow the id attribute.

- **The inbox evaluator no longer has shell access, and YouTube videos come through `web_fetch`.** The evaluation session reads untrusted content, so it now runs without Bash. The genesis `web_fetch` tool returns a YouTube video's title, channel, description and transcript. It picks captions in the video's own language (manual before automatic) and says which track it used, marking the text as untrusted content the way fetched pages are; a video without captions returns its metadata only, since this route never transcribes audio. Previously only English automatic captions were tried, and non-English videos often came back with no transcript at all. Every yt-dlp call ignores user config files and cookies, stays on YouTube hosts, and verifies certificates. A new `youtube_fetch` setting (`tls: verify | auto_fallback | off`) controls what happens on a certificate error; the default retries that one call once without verification and flags the result. `audio_max_minutes` (default 120) limits audio transcription in knowledge ingestion to videos of that length or less, read from the metadata before anything is downloaded. `yt-dlp` is now a declared dependency, with its JavaScript challenge solver installed locally.

- **Skill suggestions no longer fire on everyday words.** The per-prompt skill nudge ignores common words such as "user", "genesis", "plan", "model", "report" and "deploy" when they appear in a skill's NAME, so skills like `user_evaluate` and `genesis-voice` stop surfacing on unrelated prompts. A skill that should still match one of those words can list it under `keywords:` in its frontmatter; `genesis-development` and `cc-update` now do. Skills installed as a vendor plugin bundle under `~/.genesis/skill-library/<vendor>/<bundle>/skills/` are now suggested only when the prompt names that vendor — every word of the folder name, or for AWS also "sagemaker", "bedrock" or "hyperpod" — and a skill's own `keywords:` do not bypass that. Once the vendor is named, those skills match on their full names again, so "sagemaker planning" still suggests the bundle's `planning` skill.

- The paid OpenRouter Claude lanes now route to Claude Sonnet 5.5 and Claude Opus 5.5 (from Sonnet 5 / Opus 5), which cost the same or less. They now drop `temperature`, `top_p` and `top_k` before the request leaves, because current-generation Claude models reject sampling parameters and the desk endpoint forwards a caller-supplied temperature down these lanes. The Claude model profiles also now use OpenRouter's catalogue ids, so the model-intelligence price-drift check can see them.

- **The merge gate now keeps reviewer trust narrow and each reviewer's verdicts separate.** Codex is the one primary reviewer that merge freshness and the review-round counter wait on. Under `# substitute-review`, which the owner still approves each time, any GitHub App reviewer with a real review of the exact head can now stand in for Codex, not just Devin and CodeRabbit. The exceptions are bots the pull request's own workflow can drive and CodeQL, which is only shown. A stand-in whose finding format the gate does not yet read is labelled "format unknown: its findings are not scored yet", so the owner knows that before approving. Codex, Devin and CodeRabbit findings are still scored by their own formats. Each reviewer's findings and newest verdict are kept separately, so one reviewer's clean verdict or reply can no longer clear another reviewer's finding. Reviewer definitions live in `scripts/review_findings.py` and change only through a reviewed pull request.

- **A review round now means a pull-request head that drew findings, from any GitHub App reviewer.** Before, the round budget counted every commit Codex reviewed, clean reviews included, and ignored Devin and CodeRabbit entirely. Now a head counts once any App reviewer posted a finding on it, so a clean re-review no longer uses up a round, while a Devin or CodeRabbit finding does. Informational comments (Codex P3, Devin 🔍, CodeRabbit nitpicks) never open a round. A reviewer whose comment format the gate does not know counts on any inline comment. Reviews posted before the change keep the old count, so no open pull request's round number moved when it shipped. A review on a commit that a force-push later removed now counts instead of making the budget unreadable, which had blocked every commit on such a pull request. On the gate lane, the one free confirmation request after round two is spent once it has been asked for (its marker names a head other than the current one), and a head that itself drew findings gets no free request. `--check-pr` gains an advisory `rounds` row showing the count, the lane's terminal round, and findings per round with a RISING marker.

- Restore NVIDIA free routing with DeepSeek V4.1 Flash after the previous endpoint retired; use MiMo V2.6 Pro for ordinary paid Pro routes and both Fusion panels, preserving the protected novelty and evaluation judge baselines.

- CONTRIBUTING now states that stacked PRs are not supported yet, so open each PR against `main` once the PR it builds on has merged.

- Every plan and every issue that specifies work now answers five premise questions before it starts — the questions live in `.claude/docs/premise-check.md` ("At plan and issue time"), are restated in `CLAUDE.md` and the feature-request issue template, and are pointed to by the genesis-development `references/plan-docs.md`.

- Deep and Strategic reflection prompts now ask observations to explain the finding, evidence, significance and next step in four short lines, while keeping the existing JSON output fields. The reflection topic message shows every line of each observation whole, instead of cutting every observation at 300 characters; a long observation shows as many whole lines as fit and says how many it left out, and a single line too long to fit is shortened and marked.

- AGENTS.md now states the deploy and working-tree boundary for external clients: never edit the main checkout directly (use a worktree + PR), deploy only through `scripts/update.sh` / `scripts/deploy_code_only.sh`, and never run `scripts/deploy_candidates` (the operator tool for the local `live` integration branch).

- The plan reminder that fires around plan mode now also asks, for development plans, that free reads are done, the architect plan review is run and verified, and measured edge cases are decided rather than asked; it points at the genesis-development "Due diligence before building" and "Architecture Review" sections for the rest. It stays advisory.

- The push guard now refuses the first push of your current branch to your configured public repository unless the same command opens its pull request — `git push -u origin HEAD && gh pr create --title "..." --body-file <file>` (a ready PR, not a draft). The pull request command must be one `gh` accepts without a terminal: a title and a body (an existing `--body-file`, or `--body`), or one of `--fill`, `--fill-first`, `--fill-verbose`, with literal values (no `$`, backticks, globs or `~`). Labels, assignees, reviewers, milestones and projects are refused in it, because `gh` fails the whole create on a name it cannot resolve after the push has already run — add them afterwards with `gh pr edit`. Work that is not ready for review stays committed locally. This is a convenience check on the common case only: other ways of spelling a push keep their approval prompt, and what `gh` decides at run time (which repository or base branch it targets, a failed create) is not checked here — the guarantee that no branch reaches the public repository unchecked is the server-side leak scan that runs on every branch push. With `hooks.asks.push_publish: off`, the chained form is the one the first-push prompt is silenced for; pushes to any other repository, re-pushes, dry runs, and installs with no public repository declared behave as before.

- `scripts/deploy_code_only.sh deploy` and `restart` now refuse to restart genesis-server while it is running Claude work a restart would cancel. genesis-server reports that work itself on a new authenticated endpoint, `GET /api/genesis/inflight`: every Claude invocation, plus the whole life of a dispatched session or a CLI reflection, from before its Claude process starts until its result is stored and delivered. Work other subsystems do after their Claude call returns, such as a chat turn delivering its reply, is not covered (#2917). The script also refuses on any live Claude process below the server. The refusal names each item and changes nothing; `--allow-killing <item,…|all>`, with the items it prints (`<pid>@<start>` or a reported id), proceeds deliberately. A server too old to answer (HTTP 404) falls back to the process check alone, with a note; any other failure to ask refuses unless `--allow-killing all` is given.

- The external-I/O check is a per-class, count-baselined census covering provider hosts in `src/`, `scripts/`, and `tests/`, case-insensitively across multiple executable formats, reconciled with `provider_health`, `model_routing` and the CC roster; it is a review tripwire, not a spend barrier.

- The `gh pr close` advisory now states the superseded-PR exception: the closing session may retire a superseded PR when the owner authorized the supersession, the successor is open or merged, and a per-file check shows every part covered or moot.

- Premise checks, rework specs and new PRs now consider PR shape, using the size rule from #2737 measured in counted lines (`scripts/pr_shape.py`). Under 500 lines is the target; from 500 to 1,000 a `Shape:` line in the PR body says why the change cannot be smaller; over 1,000 needs owner approval. The rule is a guideline the building session weighs, and a stated reason is enough. A PR sent back for rework now carries a decision-complete spec: what is kept, what is deleted, the shape, a split plan, and every question either answered or explicitly delegated. The builder acknowledges that spec on the old PR in a `## Rework acknowledgement` comment before opening any replacement, and each replacement PR reports against it in a `## Rework` section, giving its split position, its deviations with their reasons, and its answers to delegated questions. The rules live in `.claude/docs/premise-check.md`, the `genesis-architect` agent, the closing-session and genesis-development skills, `CLAUDE.md`, `AGENTS.md` and the feature-request issue template.

- `BrowserLayer` now names the four layers the browser tools actually have (`camoufox`, `chromium`, `remote_cdp`, `tinyfish_cdp`), equal to the `layer` field of a successful `browser_navigate` result. The old `FETCH`, `MANAGED`, `RELAY` and `VISUAL` members, which described a planned design and had no runtime users, are removed.
- The `browser_navigate` tool description now says what happens on a Cloudflare challenge: the tool works on it itself (on Camoufox and Chromium) and, if it stays unresolved, sends an alert and returns `turnstile.status == "blocked"`, rather than waiting for a person over VNC. If the challenge handling itself fails, the result has no `turnstile` field, so check the page title.

- **`scripts/update.sh` now tells an extras-only backup gap apart from an off-site failure.** When the core off-site copy is complete and only opt-in extra directories are missing, the pre-update backup check reports `backup:tier2_extras` instead of `backup:tier2`.

- Setup renders disabled, bounded native Codebase query units and requires explicit pinned provider staging instead of automatic upgrades; startup refuses unsupported enablement, armed disable switches and missing kernel limits.

- Only the main session publishes now. A subagent may run only known read-only `git`/`gh` commands and local git work; anything else — `git push`, `gh pr create`, a PR comment or `@codex review` request, a review, a merge, an issue or release write, `gh auth login`, a `gh api` write, an unknown or aliased command, or a git/gh command written in a form the guard cannot read (a line continuation, a too-long command, a program held in a variable) — is refused, with instructions to commit locally and hand the work back to the main session. This includes re-pushes to an existing PR branch and replies to review findings. Fanned-out subagents no longer put one approval prompt per agent in front of you. With `hooks.asks.push_publish: off`, a first push spelled `git -C <a worktree of the same repository> push …`, or followed by `2>&1` and a `| tail -N` / `| head -N` output filter, is now silenced like a plain `git push`.

- Genesis uninstall now refuses busy indexing or unverified Codebase shutdown and uses one guarded cleanup path from the host or container, retaining configured state outside its documented deletion roots.

- Codebase MCP now uses managed, individually capped analysis readers against an explicitly enabled query backend. Install and bootstrap register this route even without a PATH provider; missing setup refuses without a raw or address-space fallback. The launcher and capped child retain the selected installed interpreter and normalized HOME; literal paths use a transport compatible with systemd 249 options.

- Queued Codebase indexing requires managed native authority and a pinned physical worker in the existing 8 GiB scope; unavailable setup preserves pending work and admitted jobs release lifecycle admission before waiting. The coordinator retains its nonmaximum inherited OOM adjustment while only the native child reaches1000. Failed canonical lock creation refuses managed indexing even if a temporary fallback opens.
- Read-only write-path diagnosis respects the runner's inherited filesystem sandbox and actual persistence selection, including native snapshot scratch for persistent exports. Custom write roots remain supported through an explicit operator-owned native allowlist; detected restrictions preserve queued attempts before provider spawn.

- **The merge gate no longer requires the scheduled leaks review on an outside contributor's fork PR.** The review guards the maintainer's private data, which an outsider's own text cannot hold. The exemption applies only while the PR is wholly theirs: opened by a human non-maintainer from a fork, every commit authored by them with no co-author trailer, the title renamed only by them, the body edited only by them or a named review app, and CI's leak-detector green at the current head. A leaks review that ran and objected is never overruled. `--check-pr` shows `leaks not required: outside contribution by <login>`, and a fork PR the exemption could not clear says why.

- The merge gate's `--check-pr` now writes the squash commit body and prints `--body-file <path>` in its `merge-with` line, so each squash commit on `main` records the reviewed head (`Squashed-From:`) and the sessions that built the branch (`Genesis-Session:`). At merge the gate recomputes that body from the live PR and refuses a file that differs. `--body`, `--subject` and `--author-email` are now refused on every merge, including under `# stale-review-override`, and `gh help pr merge` is no longer refused as a merge.

- On an install whose checkout runs `live` (the integration branch `scripts/deploy_candidates` builds), `scripts/deploy_code_only.sh restart` now restarts the server on it, while `deploy` and `pull` refuse and name the rebuild instead; the dashboard's Apply update and Resolve conflicts actions refuse there too, rather than starting an update that would move the checkout off `live`. A refused Apply now shows its reason instead of returning silently. Installs on `main` are unchanged.

- **The dashboard's memory-file Delete, and the file API's delete route, now move the file to the Genesis trash** instead of deleting it, so it can be restored with `python -m genesis.trash restore`. A file the trash refuses (Claude Code's temp volume, another volume) is left in place with an error, the memory index (`MEMORY.md`) can no longer be deleted, and the file browser no longer shows or opens the trash folders.

- Routing no longer calls `nvidia-nim-deepseek`: NVIDIA accepted DeepSeek V4.1 Flash requests and never answered, so every call waited out its timeout before falling back, and the outage paged as a provider failure. Where it led or sat among paid models, its place is now `deepseek-flash` (DeepSeek's own API, the same V4.1 Flash; needs `API_KEY_DEEPSEEK`, paid) followed by `openrouter-deepseek-flash`. Eight call sites now lead with this paid rung (dream-cycle synthesis, both adversarial challenge sites, wing backfill, procedure extraction, knowledge distillation, task retrospective and premortem); chains that have other free rungs simply drop NIM. A `gemini-free-latest` provider (Google's `gemini-flash-latest` alias) is declared but not yet in any chain: it currently resolves to the same model as `gemini-free`. Installs without `API_KEY_DEEPSEEK` will see a missing-key warning for these chains; `openrouter-deepseek-flash` still covers them. With these chains, an `API_KEY_DEEPSEEK` now satisfies the onboarding LLM-key floor and an NVIDIA-only key no longer does.

- Capped work now runs in its own systemd slice, `app-capped.slice` (a child of `app.slice`, so no limit above it changes): `python -m genesis.hostmetrics run` jobs unless `--slice` names another, the codebase-memory MCP wrapper's scope, and GitNexus index batches (CBM batches keep inheriting their caller's slice for their ancestor admission). The slice keeps a kill counter that outlives each job's scope, which lets the disk guardian tell a job killed at its own memory cap from any other OOM kill.

- A provider that has been dead for an hour now pages (critical, Telegram) only when an essential call site that uses it has no other available provider. While fallback still covers every essential site, its notice is written at high priority instead (dashboard and morning report) and says that calls are falling back; when coverage is lost later, the notice is promoted and pages then. With the coverage map unavailable, behaviour is unchanged.

- When the container's disk I/O stalls, the watchdog now logs which of its processes were doing I/O (top five by current rate, with their systemd unit and Claude Code session), plus the container's total I/O rate, next to its pressure warning. A stall in the journal now says who was doing I/O, or that the I/O came from outside the container.

- New host installations use a loopback dashboard proxy and server binding, stopping setup when the proxy cannot be verified. Existing installations can migrate through the Guardian ingress command, which preserves configuration and keeps direct HTTP health separate from container ICMP checks. Remote dashboard access uses an owner tunnel or restricted HTTPS proxy.
- Guardian retains environment-proxy routing for custom HTTP targets and bypasses it only for numeric loopback. Ingress migration refuses unproven or conflicting loaded Guardian service profiles before mutation; optional installer alignment checks the profile after unit installation/reload and preserves settings when proof is unavailable.

- The round reflection's ordinary-lane ladder now asks for the independent premise check at round 2 and the class sweep at round 3. A premise check must cite its output file as `Premise-evidence: <path> <verdict>`; the file has to carry the `Design-premise:` block, and the reflection's verdict and claims must match what that file concludes, so a check that found the premise BROKEN cannot be cited as sound. Evidence files are read only when they are regular files of at most 1 MiB, with a relative path resolved against the repository, and `review_reflection.py status` refuses rather than guess when the PR body changes while it is being read.

- A premise check's `Design-premise:` block now has one exact shape, defined in `.claude/docs/premise-check.md`: the verdict line at column 0, then the claim lines with the verdict first (`P1 TRUE — <claim>`), in plain text. `review_reflection.py` reads only that shape, so a word inside a claim's text can no longer be taken for its verdict. Premise checks written in the earlier free-text shape no longer count as premise evidence.

- Trusted peer enrollment no longer requires a daily task allowance. The deprecated option remains compatible with saved databases and is omitted from peer listings.
- Scoped peer health checks remain available while A2A discovery returns a clear not-ready response until the task transport is installed. Revoked peers no longer suppress the missing-credential boot warning.
- Peer and backend credentials cannot reuse the app's loaded active or fallback Flask session-signing keys; boot warnings and request checks use the same loaded authority.
- Peer routes no longer read request bodies (no current route uses one); an oversized declared length is still refused after authentication.

- General rubric judging now tries DeepSeek V4.1 Flash directly and through OpenRouter before the V4 Pro fallback. This interim operator preference does not qualify Flash for procedure suppression.

- The note shown when a session closes a pull request now says the builder of a rework may retire the PR it replaces, under the same conditions as the closing session: the owner's spec on record, the last replacement open, and a per-file coverage mapping in the closing comment. Devin never closes one; the closing session does.

- **The browser tools launch Camoufox from an explicitly resolved engine.** A camoufox upgrade can no longer make a browser call delete the installed engine and download a new one (about 1.3 GB) mid-call. A missing or broken engine now returns `Camoufox is not ready: …` with the step to give the operator, browser packages upgraded under a running session return a restart message instead of an import error, and a new `browser_automation` capability shows whether a launchable engine is present. On camoufox below 0.5 the launched binary is unchanged.

- **The browser skills now describe the browser tools as they work after the recent fixes.** `browser-automation` says that `browser_fill` has no overall deadline (it fails only when one step stalls for 30 s, and reports progress while it types), that `browser_sessions` and `browser_clear_domain` cover both the Camoufox and the Chromium profiles and match whole domain labels, that a click which opens a new tab on remote CDP makes that tab the Genesis tab, and what the "still running" and "no longer on the page" errors of the page tools mean. `stealth-browser` no longer says that a remote navigate leaves fast timing switched on.

- **The Chromium fallback and `scripts/browser.py` run through patchright when it is installed.** patchright is Playwright with its automation signals hidden, so sites that flag plain Playwright Chromium see an ordinary browser. The page now follows the window instead of a fixed 1280x720 viewport, and `browser_run_js` on Chromium runs in the page's own JavaScript world. Without patchright the fallback keeps plain Playwright and logs that it is detectable; an installed patchright that fails to import is reported, never silently replaced.

- **Every install gets the browser stack.** `bootstrap.sh` and `install.sh` install the `browser` extra (now camoufox 0.5.7+, playwright and patchright 1.62) after the core packages, as a separate step whose failure only warns. `bootstrap.sh`, which `update.sh` runs, then starts `genesis-browser-stack.service` without waiting, so the 1.5 GB first download never lengthens an update's downtime. A new daily `genesis-browser-stack.timer` retries it. `install.sh` runs the step inline, where sudo can install missing system libraries. Until the download finishes, the browser tools refuse with the operator step, and the Chromium fallback and `scripts/browser.py` now do the same when their Chromium build is missing, instead of Playwright's advice to run its installer. After an update that pins a new engine, the `browser_automation` capability reads degraded until the server next restarts. Restart Claude Code sessions after this update: a session started before it runs older browser code, which would download the new engine itself.

- MCP child servers now record names-only reads of undeclared `secrets.env` keys, providing evidence to narrow their future environment allowlists without changing what they load.

- Dashboard: the web terminal now accepts same-origin connections only.

- A premise check's `Design-premise:` block is now closed. From the verdict line to the next blank line, every line must be one of the block's lines: the verdict, the required `Expected before checking:` line directly under it, the claims, then `Effect:`, `Better shape:`, `PR-shape:`, `Persisted-state:` or a code fence. Any other line there is refused by its line number, so no misspelled claim or misplaced Expected line inside the block can be read as prose. A heading or list item labelled `Design-premise:` is refused only when it carries an uppercase `SOUND` or `BROKEN`, so a lowercase question about the check is no longer refused.

- A review that lands on a commit after one of that commit's descendants in the same PR was already reviewed no longer counts as a new review round; it belongs to the earlier round. The review budget now lists every finding review with the round it belongs to (or none, when no earlier round exists), so the round reflection can later be made to answer it.

- `scripts/update.sh` now runs on a checkout bound to the `live` integration branch instead of refusing it: in place of the merge it runs the candidates engine's rebuild as the last step before the stop (fetch origin/main, readiness, every per-candidate check, one move), then activates as usual. What it records in update history and redeploys to the host guardian is the origin/main commit `live` sits on, never the candidates on top. A rebuild that moved `live` and then failed keeps the rollback tag as the old tip, writes no update state, and says to re-run `scripts/update.sh`; a rollback on `live` also puts back the git hooks the rebuild installed. Only origin is accepted as the update remote on `live`, a commit made on `live` by hand refuses before anything changes, and bootstrap's crash recovery refuses to roll `live` back and points to `scripts/update.sh --post-merge`. The dashboard keeps refusing updates on `live` and now names `scripts/update.sh` as the way to update it. Nothing changes on `main`.

- The `evaluate` skill now searches for alternatives before scoring a tool: Genesis's own open PRs and issues, public repositories, and, for anything that runs inside a Claude Code session, the Claude Code mods and plugins catalogues. An ADAPT verdict starts by reverse-engineering the reference implementation. `genesis-development`, `web-research` and the `genesis-researcher` agent search mods the same way.

- The review budget now marks each listed finding review as late or not, and records when each review round opened, so the round reflection can read both instead of inferring them.

- On the `live` integration branch, `scripts/update.sh` now rebuilds `live` after it stops the server, in the place a merge takes on `main`, instead of before the stop. The rebuild therefore runs under the same safeguards as a merge: one database snapshot, the same in-progress record, and the normal rollback. If the rebuild refuses, fails or is interrupted, the old `live` tip (with its git hooks) is put back and the services restart on it; previously an interrupt or a failed stop could leave a moved `live` with only a message to re-run. The rollback learns what the rebuild did to the checkout from a record the rebuild writes before and after it moves (never by guessing from git), so it moves back only a tip the rebuild itself made, and it restarts nothing when the rebuild may have written part of the new tree. Each update on `live` stays down a few seconds longer while the rebuild runs, and exit code 4 (a hand-edited systemd unit) can now follow a stop on `live`, when the refused unit is one only the rebuilt tip brings. That exit 4 also records the run as rolled back in the update history, which the pre-stop exit 4 on `main` does not; a rollback that does not complete (including a failed git-hook restore or service restart) exits 1 and leaves no refusal record.

- **Claude Code is now pinned to 2.1.299** (was 2.1.280). Your next `scripts/update.sh` moves both the container and the host to it. The `sonnet` and `haiku` model aliases now resolve to Sonnet 5.5 and Haiku 5.5, so any Genesis call that names a bare alias runs on the newer model. Unattended `claude -p` runs now wait for background commands they started, and stop those commands after 30 minutes by default. Hooks that fail to match a tool call now block it instead of being skipped silently. See `docs/reference/cc-compatibility.md` (Version History) for the full evaluation.

- **The ledger extractor, memory arbiter and repo-pulse matcher now run on the latest Haiku.** They were pinned to Haiku 4.5. They now use Claude Code's `haiku` alias on your subscription, which today means Haiku 5.5 (roughly 4–10 s per call instead of 40–56 s on 4.5). They always run on your own Claude login, even when the session that triggered them was launched against another provider. Their run records now store the exact model that answered.

- **Routed Claude, GPT and Kimi calls through OpenRouter now always use the newest model in their family.** The Claude Haiku, Sonnet and Opus routes, the GPT Sol and GPT Astra routes, and the Kimi route now use OpenRouter's `~…-latest` ids instead of fixed versions. Today that means Haiku 5.5 (was 4.5), Sonnet 5.5, Opus 5.5, GPT 6.1 Sol (was 5.6), GPT 6 Astra and Kimi K3. A new release in one of those families is picked up without an update, which can change quality and price; Genesis reports routing cost but does not cap it. The two DeepSeek routes stay on fixed versions on purpose: one is part of the evaluation judge, and the other carries a measured permission to merge duplicate procedures.

- A closing session asked to close out open PRs now merges each PR in scope that is ready at its current head, without asking again per PR. Override sigils, a PR that hit a round cap, terminal-round merge-and-accept, a real finding left unfixed, PRs over 1,000 counted lines, and design questions still need the owner's yes per PR.

- Reconcile the managed Codebase roadmap and worker documentation with merged #3012, open runtime/availability repairs, and the remaining production and resource gates.

- A round reflection now counts for a finding when the only commits between the reviewed head and the reflection were made before that finding's review, or merge the default branch in without adding anything beyond git's merge and the resolution of its conflicts. Previously any such commit disqualified the reflection, so a session that kept committing while a head's reviews were still arriving could never reflect without rewriting history.

- The graph census (schema 2) counts a memory server as clean only when it can prove the server's code came from the main checkout (including the venv configuration CPython actually finds, and no stray module that would replace one the server imports), the kill-switch state it actually runs with (its secrets file, which a startup reload lets override the launch environment), and its databases by file identity and by the files it holds open; anything unproven is recorded with a reason and keeps the FalkorDB cutover clock from starting. The verdict now reports an unproven kill switch as unknown rather than as on, and reads rows from the earlier schema as predating the contract.

- **Development sessions load a much smaller skill.** `genesis-development` is
  now a shared core with a router at its top, and the rest of its text moved,
  verbatim, into one skill per session type: `build-session` and `spec-session`
  (new), and `closing-session` and `validating-merges` (which grew). Commit
  rules every committing session needs are in the core's
  `references/commit-path.md`. A build session now loads about 49k tokens of
  skill text instead of 87k; a spec session about 9k. The move was generated by
  `scripts/skill_move.py` from `scripts/skill_split/rows.yaml`, and `--check`
  proves every source line landed exactly once.

- **Sessions started at a checkout's root list every skill with its
  description again** (on a 1M-token context window, while the catalog fits
  60,000 chars). Claude Code gives the always-on skill listing 1% of the
  context window by default; a large catalog (122 skills on one measured
  install) needs more, so the lower-priority skills were listed by name only,
  without the description that tells a session when to use them. The repo now
  sets `skillListingBudgetFraction` to 0.02. The budget is a ceiling, so an
  install with fewer skills pays nothing extra. Sessions started elsewhere,
  including most dispatched sessions, keep Claude Code's default, and a model
  with a 200k-token window still gets too small a budget for a full install.

- **The closing-session skill is about a sixth shorter.** It went from 2,494
  to 2,080 lines. Text repeated between its original half and the half moved
  in from `genesis-development` now has one copy. Gate internals a closing
  session reads only on demand moved to references:
  - `genesis-development/references/gate-fix-lane.md` (new), the protocol for
    PRs that touch the enforcement-hook surface;
  - `release-review.md` (new);
  - `merge-gate-findings.md`.

  Six places where the two copies disagreed now follow the newer owner ruling,
  or the gate code. The Load Gate states that there is no "repair" session type.

- **The direct-`sqlite3`-write block now names the read-only route.** When the
  push guard blocks a `sqlite3` command that mentions a write keyword, its
  message now points read-only queries at
  `~/genesis/.venv/bin/python -m genesis db query "<sql>"`, where SQLite itself
  enforces read-only. Detection is unchanged.

- **The rule against hand-rolled parsing in guards now covers the whole class.**
  The rule now covers authorizing an operation from a spelling or snapshot that
  does not establish its promised property, beyond git argv. Identity lookups
  and structural parses establish only their own observed facts, not arbitrary
  later effects; exact-form membership remains a closed structural claim.
  When it cannot resolve that fact before the event, it fails closed, abstains,
  or checks after the event; it never lets a pattern stand in for the fact when
  allowing.


### Removed

- **The cc-tmp watchdog's ORANGE tier no longer kills idle terminal sessions.**
  At 75% of the cc-tmp budget the watchdog used to reap unattached CC tmux
  sessions idle more than two hours. It never fired and could not have helped if
  it had. Measured directly in the current log: the loop was reached on 356
  ORANGE polls and killed nothing; a further ~1,029 polls with no kills are
  reported in the pull request that first raised this, from a window this
  install's rotated log can no longer confirm. The code's own comment already
  gave the reason — sessions are not what fills cc-tmp — and one measured
  episode is the demonstration: an install sat ORANGE for 2h45m on a
  third-party tool's index cache plus live session trees, none of which a
  session kill would have touched. ORANGE now records the stuck state and
  stops; the RED tier (90%) still kills every unattached session and is
  unchanged. One residual, stated rather than hidden: killing a process closes
  its descriptors and so releases any unlinked-but-held blocks it pinned, which
  a directory-usage figure cannot see. That reclaim was never reachable from
  this tier, for a reason the old predicate hid — it judged idleness with
  tmux's session-activity timestamp, which tracks terminal output rather than
  the process, so "idle" meant "not printing", never "not writing". Measured: a
  session writing 8MB/s to disk advanced that timestamp by 0 seconds. The loop's
  best case was freeing space the tier could not measure, and its worst case was
  reaping a session that was working.

- **The one background-session profile that could run a shell has been removed.** It was allowed exactly one binary — the GitHub CLI — for stewarding this project's own pull requests against other repositories: read state, comment, reopen, close, and escalate code-change requests rather than editing anything itself. It never ran. Zero sessions, against 257, 167 and 154 for the three busiest profiles, so nothing depended on it and nothing changes for anyone by its absence.
- **It was removed rather than confined, and the reasoning is the specification for adding one back.** Restricting a shell to a single permitted binary bounds *which program runs*; it says nothing about what that program can be told to do, and four measured facts each close off a different repair. The tool will run programs of its own accord through its own configuration — an alias, the pager, the editor, an extension — every one of them reached with the permitted name as the first word, so the restriction permits both installing an escape and firing it; that one is closed, by a read-only configuration directory which is retained. It reads arbitrary local files through its own documented flags, which means denying the file-reading *tools* cannot make "no file reads" true while the binary itself is permitted — this is what defeated the attempt to confine it by tool scope. It writes files to caller-chosen paths, so "it only comments" was never a property the restriction conferred. And a profile's tool denials can be re-enabled per request unless the tool sits in a protected set.
- **All four want the same unbuilt thing: a restriction at the sub-command level.** Gating the profile behind an operator switch was built, reviewed and discarded, because a switch only moves the decision — its enabled position still hands a session that reads text written by strangers the operator's identity on that service, plus file read and write through the permitted binary. A capability whose enabled state cannot be made safe is not made safer by being switchable, and "ready to turn on" was not a state it could reach.
- **The machinery stays, and is not dead code.** The permitted-binary map, the guard that enforces it, and the per-binary hardening all key on the *binary*, not on any profile name — so an installation that grants that shell to a profile of its own, through the documented profile-overlay hook, still gets the sealed configuration and the pinned program routes with no further wiring. The map being empty is a statement about what ships, never about the mechanism. A test asserts the emptiness and a separate one asserts the hardening survives, because those are different claims and both were easy to get wrong.
- **The floor is measured against what SHIPS, not against the running install.** The guards that assert no shipped profile grants a shell originally read the live tables — which the profile-overlay hook mutates in place at import — so on an installation that deliberately grants that shell to a profile of its own, this project's own tests went red. A floor that breaks on the supported configuration is not a floor, and it contradicted the reasoning above it. The tables are now snapshotted before the overlay loader runs, and a test simulates such an installation to hold the distinction.
- **Removal was verified as complete rather than assumed.** A profile is registered in six separate tables, and dropping it from only the one that gates dispatch would leave a name that cannot run while another table still reports it as holding a shell. The check enumerates the tables from the module itself rather than from a list written alongside, so renaming one is loud rather than silent — though it filters by type, so a table of a different type is covered only by its own explicit assertion, and the count it asserts is a floor rather than a proof of completeness.

- `secrets.env.example` no longer lists `API_KEY_AZURE`, `API_KEY_BEDROCK`, `API_KEY_HUGGINGFACE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `TESTSPRITE_API_KEY` or `CONTAINER`. Nothing in Genesis read any of them, so the dashboard's Provider Keys panel stops showing eight fields that had no effect. Values already set in an existing `secrets.env` are left alone.

- **The loopback-only dashboard default is withdrawn for now.** On an existing install the update that applied it cut off the host dashboard proxy, a Tailscale address inside the container, and the Guardian health check until manual host steps ran. The dashboard keeps listening as before until the change returns with a rollout that keeps existing installs working.
- **An install whose full update refuses at the dashboard-loopback check can take this rollback with `scripts/deploy_code_only.sh pull` on a clean checkout.** The installed `update.sh` runs that check before it merges new code, so it cannot reach the withdrawal by itself; the code-only pull has no such check, and the next full update then runs without it.


### Fixed

- **A broken critical call site no longer falls silent after a day.** The
  dashboard's `callsite:down` view only counted failures from the last 24
  hours, so a slow-cadence critical site (like the weekly strategic
  reflection) that failed its last run went quiet after a day and stayed
  broken-and-invisible until its next attempt — the longer something was
  broken, the quieter Genesis got. Critical sites now stay red for up to 35
  days after a failed last run (matching the job-health floor), while
  non-critical sites keep the tight 24h window so long-abandoned one-off
  sites still don't nag.

- **An update that leaves part of the system broken now says so.** After every
  update Genesis checked which subsystems had come up — except it asked the health
  endpoint for a list that endpoint has never returned, so the answer was always
  "nothing wrong" and the check had never once reported anything on any install.
  It now reads the record the server writes when it finishes starting, and compares
  it against the same record from before the restart. The comparison is what makes
  it trustworthy: a subsystem's own status is ambiguous, because most of them catch
  their own startup errors and end up described the same way as one whose optional
  dependency is simply absent. Something that was working before the update and is
  not working after it is unambiguous. Anything that got worse is named in the
  update history and in the update's own output, while a component that was already
  dormant — no optional dependency installed, nothing to start — stays quiet
  instead of crying wolf on every update.

  Findings are recorded, not treated as failures: one optional subsystem not
  starting is not a reason to undo an otherwise good update. The subsystems the
  server genuinely cannot run without were already covered — without them it does
  not finish starting at all, and the update rolls itself back.

  The server now also records which process wrote that startup record, so the
  update can tell its own server's report from one written by another Genesis
  process on the same machine at the same moment. When it cannot tell, it says so
  rather than reporting all-clear — including on the first update after this
  change, which has no earlier record to compare against.

- **The merge gate now reads review findings it previously could not see at all.**
  When a reviewer's finding points at a line outside the changes a pull request
  actually made, CodeRabbit cannot attach it to the diff and puts it in the review
  summary instead. The gate only ever read the attached findings, so those went
  unreported — a pull request could show a clean review status with real findings
  outstanding. Measured on the install this was built against: 27 findings across
  23 open pull requests were invisible this way, 15 of them rated Major, including
  one about silently losing a stored preference and one about writing unredacted
  URLs to a log.

  Those findings are now collected and shown, deduplicated across re-reviews and
  kept at the highest severity any review gave them. Only a Critical one holds up
  a merge: the rest are reported but do not block, because a finding delivered
  this way has no comment thread, and the usual way to accept a finding you
  disagree with is to reply in its thread. Blocking on something you cannot answer
  would leave no way forward.

  A review summary quotes code, so it can contain text that looks like the
  summary's own structure. Findings are therefore located by where they sit in the
  document rather than by matching that text wherever it appears, which stops a
  finding being hidden or filed against the wrong file by content in the pull
  request under review. If the summary says it carries more findings than were
  read, the merge is held rather than proceeding on a partial list.

- **Two end-of-turn checks were firing correctly and reaching nobody.** When
  Claude finishes responding, Genesis checks the reply for work handed back to
  you that it could have done itself, and for a claim of completion with no
  evidence behind it. Both checks worked; both then wrote their reminder to a
  channel the assistant cannot read, while the file said in as many words that
  the text reached the next turn.

  Claude Code shows a model a hook's plain output for only a few kinds of
  event, and the end of a turn is not one of them. What that channel does
  instead is worth stating plainly, because it is not what the name suggests:
  handing text back there does not leave a message for later, it declines to
  end the turn and says why. Claude keeps working and takes the note into
  account. That is the right shape for these two — being told you have handed
  work back, or claimed something is done without checking, is precisely a case
  for not stopping yet — but it is a change in behaviour and not only in
  plumbing, so it is bounded: the reminder speaks once and then stays quiet,
  costing at most one extra step rather than looping.

  Both stay quiet when a reply ends by asking you something. A turn that hands
  control back is already stopping correctly, and declining to end it would
  have Claude talking past the person it is waiting on — including at the point
  where it asks whether to open a pull request. That mattered only once the
  reminders started arriving; while they went nowhere, firing on the wrong turn
  cost nothing.

  A third check, for code changed without a review, deliberately stays out of
  this path. It depends on a condition rather than on something in the reply,
  so it would repeat until the condition cleared; it also already arrives by
  another route, at the start of each of your messages, and always did.

  The one other end-of-turn check Genesis runs — the deliverable gate, which
  refuses to end a session that rendered a document nobody verified — keeps
  repeating on purpose, and is unchanged. A reminder said twice adds nothing; a
  gate that gives up after being acknowledged once was never a gate. It still
  lets go the moment the document is verified or the draft is abandoned.

  Now that the two reminders cost a step instead of nothing, three ways they
  read a reply wrongly are fixed. A quoted review verdict saying the work is
  *not* ready no longer reads as a claim that it is. A reply quoting the phrase
  "read the implementation completely" no longer reads as "implementation
  complete". And naming a check that has not happened yet — testing planned for
  next session, verification deferred or blocked — no longer buys silence from
  the reminder that exists for exactly that case; only a check reported as done
  does. The same holds for a check reported as not having run ("did not run",
  "timed out", "errored"), even when the same sentence says something else
  passed.

  Deliberately unchanged: a reply that phrases finishing in the negative ("I
  won't merge without your go-ahead") still gets the reminder. Measured against
  this install's own history, suppressing those would have silenced the
  reminder on turns that genuinely wanted it and caught none of the wrong ones.

- **Genesis notices when its own file edits fail.** The sensor behind the
  tool-call calibration base rate had recorded zero failures in roughly 25,000
  rows over eight weeks — not because nothing failed, but because on current
  Claude Code a failed `Edit` or `Write` fires no hook at all, so the failure
  path was unreachable and the base rate read a constant "everything succeeds".
  Both outcomes were in the session transcript the whole time. Genesis now scans
  that transcript when a turn ends and records every edit outcome, success and
  failure together as one population, so the calibration lane finally measures
  something real: on this install's transcripts the rate comes out near 0.95
  rather than a flat 1.0. Existing rows are left alone and re-scans are
  idempotent, so nothing is double-counted. Each recorded outcome also names the
  session it actually happened in — it previously stored the wrong session id, or
  none, for most rows.

- **The worktree-removal guard no longer refuses a command for *mentioning* a
  removal, and no longer misses one that is really happening.** It matched raw
  command text, so the phrase inside a quoted grep pattern, a heredoc body, a
  docstring or a commit message read as the operation itself. That is not only
  friction: a blocked command is discarded *whole*, so a false match on the last
  step silently threw away the file writes in the earlier ones while the error
  mentioned only the rule that fired. The check now keys on the parsed command —
  the executable, its subcommand and its real arguments. Replayed over 51,052
  unique (command, directory) pairs from one install's own session history, each
  from the directory it was typed in: 154 commands blocked before and 154 after,
  but it is a different set — 6 refusals of commands that merely *mention* a
  removal are released, and 6 real removals that were previously allowed are
  caught. The unchanged total is why the composition is quoted rather than the
  count. That corpus is built from real transcripts and cannot be published, so
  these totals are the scale at which the change was observed rather than a
  result anyone else can reproduce; the composition is the claim.
- **A worktree removal hidden behind a global git flag is now caught.** The
  check required `git` to be followed immediately by the subcommand, so
  `git -C <path> worktree remove <target>` slipped past it entirely. Two shapes
  did: a global flag before the subcommand, and — found by cross-model review —
  a global flag whose *operand* happened to equal the subcommand's own name,
  which made the operand extraction anchor one token early and skip the segment.
  The parser now reports the subcommand's index from the same scan that finds
  it, so no caller has to re-derive it.
- **The global Bash safety hook is now syntax-checked by the test suite.** It is
  loaded for every session on the machine, and a shell syntax error there exits 2
  — which Claude Code reads as "block" — so a typo would have refused every
  subsequent command, including the one needed to repair it. Nothing checked for
  that before.
- **An ordinary `pip install` run from inside a worktree is no longer blocked.**
  The editable-install check looked for `-e` as a plain substring, so any pip
  command carrying a long option whose name begins with `e` supplied one —
  `--exclude-files`, `--extra-index-url`, `--exists-action` — as did a package
  name with `-e` inside it, such as `pytest-env`. Since the same check also fires
  when the current directory is a worktree, that hard-blocked routine installs
  from every worktree, and a blocked command is discarded whole. It now asks
  whether a `-e` *option* is present rather than whether those two characters
  appear anywhere.
- **Editable installs that were slipping through are now caught.** `-qe`, `-ve`
  and `--editable=<path>` all reach pip's editable code path and none of them
  contains a literal `-e`, so the old substring never saw them. Abbreviated long
  flags are covered too — pip installs from `--ed` just as it does from
  `--editable` — which the stricter check would otherwise have started missing.
  The same gaps existed in the project-level copy of the check and are closed
  there too.
- **A forced worktree removal is now caught however the flag is spelled.** The
  check wanted `-f` followed by a literal space, so a tab before the operand or
  the flag placed last did not count; and it wanted the long form spelled out in
  full, while git removes just as happily on `--f`. Measured against every
  spelling git accepts, the old check missed 39% of them and the new one misses
  none.
- **The shell-side checks stay deliberately over-matching about WHERE the command
  starts.** They run in the global hook with no access to the command parser, so
  keying them on command position means modelling shell grammar with a regex — an
  open set. An attempt to do so fell open on a leading redirection and on the
  `command` / `env` wrappers, all of which the broader form catches. They keep
  refusing some commands that merely mention an operation; that friction is the
  deliberate price of not leaving a hole in a check that exists because of a real
  crash. Recognising a flag is a separate question that needs no grammar, which
  is why the two fixes above do not reopen that hole.
- **A removal launched through a shell option bundle, or through a tool named by
  its full path, is caught again.** `bash -ce '<removal>'` runs the removal, but
  the parser read the bundle's trailing letter as the script and never looked
  inside; and the check for tools that carry a command string required the tool's
  name to start a word, so `/usr/bin/find … -exec <removal>` matched nothing. It
  now asks the parser, which has already resolved the executable, instead of
  re-reading the text.
- **A removal inside a command the parser cannot read is refused again.** When
  tokenizing fails the guard falls back to a coarse text match, and that fallback
  still required `git` immediately before the subcommand — so a global flag in
  between let it through on exactly the input nothing else could inspect.

- **A crafted Bash command could run a safety guard past its timeout, and a timed-out
  hook does not block — it permits.** The shared shell parser had no bound on the work
  a single command could ask of it. Cost rises with the length of the command, with
  its nesting depth, and (measured, 3.4x) with the shape of its padding, and Claude
  Code's documented hook contract is explicit that a hook which reaches its timeout
  "doesn't block the tool call". So a guard that ran out of clock did not refuse the
  command — it allowed it.

  Measured end to end against the real protected-paths guard, on a payload it
  genuinely refuses, under its registered 10s timeout: refused in under half a second
  unnested at 64 KB, but **killed at the timeout** at deep nesting, at every depth
  once the command reached 300 KB, and — with no nesting whatsoever — at 450 KB.
  Eleven of fifteen measured cells failed open. Reaching this needs a command far
  larger than ordinary work produces, which for a security guard is the threat model
  rather than a mitigation.

  Both axes are now bounded, because neither subsumes the other: a length cap alone
  leaves nesting free to multiply the work, and a depth bound alone leaves the first
  pass — which happens before any recursion — completely unbounded. Every one of the
  same fifteen cells now refuses, well inside the budget. Both limits are set from
  data rather than taste, sized against the longest and deepest commands in this
  install's own history, and neither fires on any of them; no command's top-level
  segments change, which is what keeps the guards that match working directories by
  exact string unaffected.

  The limits are sized against the tightest path rather than the loosest, which is not
  the one the individual guards are registered on: the shell safety hook is registered
  at 5 seconds and delegates to three guards over the same command, one of which
  analysed it three times over — five parses on one budget, not the two an earlier
  measurement assumed. Those duplicates are now parsed once. The two limits are chosen
  together, because the parser re-scans the remaining text at every level and the
  worst case is a command at the size limit nested to the depth limit — cost is length
  times levels, so neither number means anything alone. The two directions are
  deliberately not treated as symmetric: over the size limit a command is refused or
  prompted, while over the timeout it runs unchecked, so margin is bought on the side
  that fails open.

  Bounding the work alone would have traded one fail-open for a worse one — a guard
  that quietly stops seeing a buried command and allows it — so the bounds and the
  signal ship together. One traversal now returns both what the command runs and
  whether the parser could read all of it, and every guard that fails closed on an
  unreadable command asks that single question. A test derives the consumer set from
  the code and requires it, so a consumer added later cannot quietly go back to the
  unchecked call; an over-length command yields no segments at all rather than a
  parsed prefix, because truncating a command mid-string changes the meaning of
  everything after the cut.

  Refusal messages also stopped guessing at the cause. Each blind spot carries its own
  explanation and its own way out, so a command that is merely too long or too deeply
  nested no longer tells the reader to go fix quoting that was never the problem — and
  the protected-paths fallback no longer describes an unresolved shell variable or an
  unresolvable relative path as "an untokenizable command", which it had been doing
  for two of its three callers.

- **Clearing every review marker now also clears the legacy pre-upgrade one**, which
  it could previously skip while reporting success. That legacy file authorizes a
  commit entirely on its own — it needs no per-worktree marker directory to exist, or
  to be readable — but it was removed at the very end of the routine, below a check
  that returns early in exactly those two states. An install carrying only the legacy
  marker, which is what an upgrade leaves behind, therefore got "cleared 0, no
  failures" while a time-valid marker sat intact and could authorize an unreviewed
  commit for its lifetime. It is now cleared before anything can return early, so the
  ordering itself prevents this rather than a second check catching it; a marker
  directory that cannot be read is still reported as the separate failure it is.

- **An out-of-memory kill inside a deliberately capped job no longer pages as a
  container emergency.** Some background jobs run under their own memory cap on
  purpose, so a runaway one is killed without touching anything else — that is
  the containment working. But the kill counter the monitor watches aggregates
  every such event up to the container, so a contained kill paged as "processes
  OOM-killed in the container" and pointed at your sessions as the likely
  victims, while the container itself had gigabytes free. One stuck indexing
  job produced eleven emergency pages in two hours this way.

  The monitor now asks the system journal which unit actually died before
  paging. A kill fully accounted for by known capped jobs is recorded in the
  log and the durable snapshot but does not page; anything else — an unknown
  unit, no record, or no journal to ask — pages exactly as before, now naming
  the killed unit when it is known. The set of "known capped jobs" is
  configurable. Attribution can only ever downgrade an explained kill, never
  silence an unexplained one.

- **A failed or retried delivery can no longer page the same alert twice.** When sending
  an alert to Telegram failed transiently, two independent recovery mechanisms
  both took ownership of the retry: the durable alert queue kept its copy for
  the next pass, and the delivery pipeline separately queued its own retry.
  Each then delivered — one alert, two pages, minutes apart, in the middle of
  exactly the kind of incident where you are reading the channel carefully.

  The durable queue is now the single owner: a caller that carries its own
  retry tells the pipeline not to queue a second one. The queue's fourteen-day
  patience is kept deliberately — the pipeline's own retry gives up after
  about an hour and a half, so handing the alert over would have traded the
  duplicate for a page silently lost in any longer outage. The same rule stops
  a failed retry from queueing a shadow copy of itself, which was quietly
  possible before and delivered doubles the same way.

- **Per-session memory on the dashboard now counts the whole session, not just
  one process of it.** A Claude Code session is not a single program: alongside
  the main process it runs a language server and a fleet of MCP servers, and
  those children turn out to be about two thirds of what a session actually
  costs. Only the main process was being measured, so seven concurrent sessions
  reported roughly 5 GB when the real figure was closer to 16 GB.

  The more consequential half was invisible: the warning and critical
  thresholds were applied to that same partial number. A session whose true
  footprint had grown past the alert threshold still showed a small main
  process and reported healthy, so the alert could not fire in the situation it
  was written for. Session memory is now measured across the whole process
  tree, the thresholds are rebased on what a healthy session actually costs,
  and the alert says how much sits in the main process versus its children —
  so the figure can be reconciled against what you would see in `top`.

- **Genesis's self-review now reads your whole reply before judging it.** After
  each interaction a background pipeline grades what happened — whether the
  request was met, what to learn from it — and writes the verdict to long-term
  memory. It was shown only the first 1,000 characters of a response, with no
  indication that anything had been left out — so an ordinary answer could arrive
  looking like one that stopped mid-sentence. Measured over a month of production
  traffic on this install, about two in five graded responses were being shortened
  that way. The limit is now a safety valve set above real traffic; if a very long
  response does have to be shortened, the middle is removed rather than the end and
  the gap is labelled. The graders are also given the response inside explicit
  markers, and told about a shortened or interrupted run only when there is an
  actual signal to report — and the "shortened" signal now comes from the
  pipeline's own record of what it did, rather than from searching the reply for
  a phrase. A reply that
  happens to discuss this mechanism (or an incoming message that quotes it back)
  can no longer make the grader believe the reply was shortened when it was not,
  and the label on a genuinely shortened reply no longer claims anything about
  whether the model stopped early.

- **The tools a session used are now read from the session, not from its own
  reply.** The same self-review pipeline told its graders which tools an
  interaction used by pattern-matching the reply's text — so a reply that merely
  *discussed* running a tool was reported as having run it. Genesis now records
  tool use as it happens and reports that instead. Where it cannot — some
  invocations do not stream — the graders are told plainly that any names were
  extracted from the reply and may be tools it only mentioned, and where there
  is no evidence either way they are told that too, rather than being handed a
  bare "none" that reads like a finding. One visible consequence: short
  interactions that genuinely used a tool are now reviewed where some were
  previously filtered out, so review volume rises slightly.

- **A reply cannot forge the notes written beside it.** The reviewed text is
  wrapped in markers so a grader can tell the interaction apart from the
  system's own remarks about it — but the markers were a fixed pair, which a
  reply could simply write, ending the quoted region early and placing its own
  lines where the system's go. The markers are now derived per message and
  checked against it, so no message can contain the one that ends its own
  region. The user's message is wrapped the same way; it had not been wrapped at
  all, which is the same gap on the input an outside sender writes.

- **Tools are reported as requested, not as run.** The runtime records the
  moment the model asks for a tool, which is before anything happens — a call a
  safety hook then denies looks identical. The line shown to the reviewer says
  so instead of asserting the tool ran.

- **The size limit on reviewed text now bounds what it claims to.** It counted
  characters, so text in a non-Latin script or heavy in emoji could be several
  times larger than the limit implied, with no combined bound across the message
  and the reply together. Counting bytes bounds it for any script. Ordinary
  English text is unaffected — for it the two counts are the same number.

- **A long request is no longer cut off before Genesis reviews it.** The same
  pipeline shortened the user's own message to its first 500 characters with
  nothing marking the cut, and then asked a grader to judge whether the reply
  matched the request. On this install 15% of incoming messages were over that
  limit. Requests now reach the reviewer whole; a genuinely enormous one is
  shortened from the middle, keeps its ending, and says how much was removed.

- **One malformed character no longer stops Genesis reviewing an interaction at
  all.** Some text encodings allow a stray half-character that is technically
  invalid on its own. JSON accepts it, so it can arrive in an incoming message or
  in a reply, and the self-review pipeline measures text in bytes — so it stopped
  with an error before grading began, and every lesson from that interaction was
  lost. Text is now checked as it enters review, and anything unencodable is
  shown in a visible escaped form rather than crashing the pass or being silently
  dropped.

- **A message can no longer plant a procedure Genesis will follow later.** One
  older path that turns a finished interaction into a reusable, stored procedure
  was handing the request and the reply to the model as plain text, with nothing
  marking where they ended. Text written by whoever sent the message could
  therefore pose as the pipeline's own instructions — and because the result of
  that step is SAVED and recalled by later sessions, a single crafted message
  could leave behind a procedure Genesis would go on following. Both pieces of
  text now sit inside markers the text itself cannot forge, the same protection
  the review prompts already use.

- **A partly-explained batch of out-of-memory kills now pages instead of going quiet.** When several processes are killed between two checks, the monitor asks the system journal which ones died so a kill inside a job's own memory cap does not raise an alarm. But it only checked that the units it *found* were expected — so if two processes died and the journal explained one of them, the alarm was suppressed and the unexplained kill went unreported. It now requires every observed kill to be accounted for, and pages when any is not. It also reads the journal from an exact position rather than a timestamp, so an entry on the boundary is never counted twice, and two kills of the same job are counted as two rather than collapsed into one.

- **An alert held for approval is no longer re-held on every retry.** When a queued alert routes to email, the approval gate records a pending item and takes ownership of delivering it. The alert queue did not treat that as finished, so it resubmitted the same alert on the next pass and created a fresh pending item each time — and approving them would have delivered the same alert once per hold. A held alert is now treated as handed over, matching what the recovery worker already did.

- **"N commits behind" now means how far behind YOU are.** The update notice
  counted the commits between the two release tags and labelled the result as
  your distance from the new version. On any install that pulls main between
  releases those are different numbers, and not by a little: the tag moves once a
  release while your deployment moves constantly. Measured on a live install, the
  dashboard read "v3.0b18 (668 commits behind)" on a tree that was twenty commits
  behind that tag. The figure was true about the release and false about the
  reader, which is worse than a wrong sum — it looked like a checked number, so
  nobody checked it.

  Both places that produced it are fixed, and the list of changes shown beneath
  it now covers the same range as the count, instead of ending "and 658 more"
  next to a figure the reader takes as their own. A third place in the codebase
  already measured this correctly and its field name said so; the two that were
  wrong shared a name that did not.

  The same rule is now applied to the cases where the count cannot be taken at
  all. When the distance could not be measured, one of these reported "1 commit
  behind" beside an empty list of changes, and the other reported zero — which
  renders as "up to date" and cleared a real pending update from the dashboard.
  Neither number had been counted. A measurement that failed now says it failed,
  and a measured zero is left alone instead of being rounded up to an update that
  does not exist for this reader.

- **A Codex round request is now refused unless the hook can read which pull
  request it names.** The cap counts a PR's existing Codex reviews and blocks a
  further round until a conscious `# escalation-ack`, but it resolves the PR
  from the command text — and a `PreToolUse` hook sees that text *before* the
  shell expands anything. Any identity it could not read simply skipped the cap.

  Measured: writing the request as
  `for n in 1625 1576 1609; do gh pr comment $n --body "@codex review"; done`
  posted round requests against two pull requests already at or past the cap,
  with no acknowledgement and none of the step-back triage the block exists to
  force. The same request written with a literal number was refused correctly,
  so the cap was doing its job whenever it could tell which PR it was reading.

  Both identity-bearing values are now covered — the PR target *and* the
  repository (`--repo`/`-R`, or the owner/repo inside a URL target); with a
  literal number and an unreadable repo the count went to a nonexistent API
  path, errored, and failed open. Each is accepted by an **allowlist** of what
  gh documents (`[<number> | <url> | <branch>]`, `[HOST/]OWNER/REPO`), spelled
  in characters the shell does not act on, so a spelling nobody thought of is
  refused by construction rather than added to a list next time.

  What this costs, since it will be met in normal use: the common
  `gh pr comment 1234 --repo "$SLUG" --body "@codex review"` now needs the slug
  written out (or dropped, when running inside the repo). Measured over 530
  distinct real round requests, 47 (8.9%) are refused; in that corpus every one
  of them is a request whose PR or repository the cap genuinely could not read.
  A PR URL keeps working as copied from a browser, `/files`, `#issuecomment-…`
  and all.

  Two fail-opens are deliberately kept: a request with no positional target at
  all, and a literal branch name. The acknowledgement sigil does not clear this
  refusal either — it is computed across the whole command, so one sigil on a
  loop would license a round on every pull request the loop touched.

- **The worktree-removal guard no longer goes silent when a bound stops the
  parse.** It decided by searching `analyze`'s segment list, and a parse halted
  by the substitution-depth or command-length bound yields no segments at all —
  by design, so a searching consumer cannot read "stopped looking" as "nothing
  found". The guard read it as the latter: a removal wrapped past the depth
  bound produced no targets and was allowed, on a guard whose only job is
  blocking.

  It now asks `analyze_checked` once per invocation and treats a blind spot the
  same way it already treats a command shlex cannot tokenize — falling back to
  the coarser regex extractor, which reads the raw text and so cannot be hidden
  from by nesting. Measured by mutation: with the new branch removed, a real
  `git worktree remove` nested eight substitutions deep exits 0 (allowed); with
  it, 2 (blocked). A deep command carrying no removal is still allowed, so the
  fallback discriminates rather than blocking everything unreadable.

  This also restores the chokepoint invariant that every consumer of the shared
  parser either uses the checked entry point or is listed with a stated reason
  why blindness is safe for it. That reason could not be written here.

- **The dependency audit no longer fails on a litellm advisory Genesis cannot
  reach.** `CVE-2026-37004` (CVSS 9.8) is server-side template injection through
  the `dotprompt_content` parameter of the `/prompts/test` endpoint, from an
  unsandboxed `jinja2.Environment`. The sink is
  `litellm/proxy/prompts/prompt_endpoints.py` and it is served only by the proxy,
  which Genesis never starts — the same structural reason the four `2026-11`
  proxy advisories are already triaged. Verified rather than assumed: all five
  `litellm` imports in the tree are the client library, and the tree contains no
  reference to `litellm.proxy`, `proxy_server`, or that endpoint.

  It is triaged rather than upgraded because pip-audit names 1.83.7 as the fix,
  which is past the 1.82.x releases the pin exists to avoid. Here the
  reachability argument is what holds, not the version — so the pin stays and the
  advisory is recorded with its reasoning next to the four of its own class.

- **Memory recall no longer fails on some ordinary prompts.** A query could be expanded into a search expression the text index refused to parse, which surfaced as an error instead of results. Expansion terms are now cleaned before they are combined, and a malformed expression falls back to a plain keyword search rather than failing the whole recall.

- **Memory recall handles concurrent sessions better.** Recall reads share a small pool of database connections, and once every connection was in use the next recall waited — long enough to exceed its time budget and fall back to a keyword-only search. The pool is now sized from the CPUs available to Genesis rather than a fixed four, so the point at which recalls start queueing moves up with the machine. It is still a ceiling, not an unlimited pool: enough simultaneous recalls will still queue, and `GENESIS_RECALL_READ_POOL_SIZE` raises it further. A pool that fails partway through opening now cleans up after itself instead of leaving connections behind.

- **A database problem no longer surfaces as a raw error from a memory
  recall.** The in-process graph store answers by reading the memory database,
  and if that read failed — a locked file, a connection that had been closed
  underneath it — the underlying error escaped instead of being reported as
  "the graph is unavailable". That distinction is what the fallback chain keys
  on, so the failure skipped it entirely: no fallback to the SQL path, no
  warning in the log, just the raw error arriving wherever the traversal had
  been called from.

  Both halves are now reported as unavailability, which is what the surrounding
  code was already written to handle. The second half matters because the SQL
  fallback reads the same database connection that just failed — so for anything
  other than a passing lock it fails the same way, and fixing only the first
  half would have moved the raw error rather than removing it.

- **GitHub steward: no more notifications about your own activity.** A notification was reported whenever the thread had been touched, and the "actor" was resolved as whoever had commented most recently — which, on your own pull requests, is you. Routine review-bot traffic therefore arrived as digest entries with no contributor attached. The steward now reports a thread only when somebody who is not you and not a bot actually acted on it during the window the notification covers, and it reads three text surfaces it previously ignored entirely: a pull request's inline review comments and review summary bodies, and commit comments. Editing a comment counts as acting, and a thread whose surfaces could not be fully read is still reported rather than silently dropped. Because the steward now identifies who ACTED rather than who wrote the `@`, a mention alert says so: GitHub keeps reporting `mention` on every later update of a thread you were mentioned in once, so the person the alert names is frequently not the one who mentioned you.

- **The server's OOM protection setting now matches what the kernel actually uses.** The service declared a value the system silently refused to apply, so the setting looked correct on every surface while the kernel used a different one — and nothing anywhere reported the mismatch. The declaration is now a value that can actually take effect, and Genesis raises a standing alert if any of its services ever declares a setting the kernel does not honour.

- **Related memories now show how strongly they are actually connected, and the
  same question gives the same answer twice.** When a memory could be reached by
  more than one route of equal length, the graph walk credited it to whichever
  route it happened to look at first and reported THAT connection's strength —
  often the weaker one. It now uses the strongest connection that actually
  reaches it. Because strength is also what orders these results before the top
  few are shown, an understated memory sank in that ordering and could drop out
  of view.

  Measured against the live graph: of every connection the walk computes, 6.4%
  were understated, affecting half of all lookups — but most of those never reach
  you, so the number that matters is smaller. Of the related memories actually
  shown, the set changes on 1.9% of lookups and their order on 8.2%. The same
  change also removes a second oddity: which related memories you saw could shift
  between runs on identical data, purely from database row order — 3.1% of
  lookups before, none after. Nothing changes about which memories are
  *reachable*, and no connection is ever credited with a lower strength than
  before.

  The SQL path Genesis falls back to when the in-process graph is unavailable had
  the same flaw in a worse form: it could return the same memory twice, once with
  each route's strength, taking two of the handful of slots shown and dragging a
  false weaker number into the ordering. Fixing that exposed two more differences
  in the same place — the fallback would return a memory linked to itself as its
  own related memory (30 such links exist), and it answered with one level of
  results even when asked for none. Both are closed, so the two paths now agree
  on every case covered here: same memories, same strengths, same labels, same
  order. One difference is left standing deliberately, because it cannot occur
  with the identifiers Genesis actually uses: the fallback's loop protection
  compares identifiers as text, so it would over-prune if one memory's id were
  contained in another's.

- **The related-memory graph no longer goes quietly stale after the database
  recovers from an error.** Genesis keeps an in-process copy of the memory graph
  and rebuilds it when the database it was built from changes. The check for
  "changed" compared the connection object it had been handed — but that object
  is a wrapper, and when the database recovers from a run of errors it replaces
  the connection *inside* the wrapper while the wrapper itself stays the same.
  The check therefore could not notice, and the freshness signal it fell back on
  is per-connection, so it was being compared against a counter belonging to the
  connection that had just been closed.

  The effect was a graph that could keep serving its old contents indefinitely,
  and it was worst at exactly the wrong moment: right after the database had
  trouble, which is when the copy is most likely to be out of date. Genesis now
  tracks the connection underneath the wrapper, so a recovery is seen as what it
  is and the graph is rebuilt once on the next read.

- **Filing a bug you find while reviewing no longer needs to be asked about.** The
  rule used to require explicit approval for every GitHub issue, which is what kept
  adjacent bugs unfiled — someone finds a real problem mid-review, has nowhere cheap
  to put it, and it evaporates. Now the gate is what goes IN the issue rather than
  permission to open one: scrub anything personal or identifying, keep it to
  technical detail, and ask when a case is borderline either way. A security defect
  is still never filed publicly before it is fixed.
- **Three review-process rules that said two things at once now say one.** The docs
  claimed a gate-touching PR gets no stale-review leniency at all, while a passage
  further down measured the opposite — the check is on the delta's files, not the
  PR's, so a docs-only follow-up can still be trivial. The code agrees with the
  second. And a usage-limits reply from the code reviewer was described as proof it
  cannot review, contradicting a measured case where findings arrived anyway; it is
  now the strongest available evidence rather than proof, which matters because that
  judgement is what authorises reaching for a fallback reviewer.

- **A command whose operation the shell assembles is now treated as unknown
  rather than as harmless.** The shared parser behind the approval guards uses
  Python's shell tokenizer, which reproduces quoting faithfully but performs no
  expansion at all. A word built by the shell therefore parses cleanly into
  something other than what actually runs, and when that word is the one
  choosing the operation, the guards saw a tidy parse naming nothing they gate —
  the same answer they give for a command that gates nothing at all. The parser
  now marks such a segment and reports it, so an operation it cannot establish
  routes to the existing "ask a human" path instead of a confident all-clear.
  Two word-generating constructs are covered — substitution and brace expansion
  — and the rule for each is stated as what it accepts rather than as a list of
  the spellings that fool it, so spellings nobody has thought of are covered
  too. Two further constructs are named in the code as deliberately uncovered,
  with the reason for each, because the recurring mistake on this file is
  claiming a set is complete. Measured against 129,179 real commands from this
  install's history, 15 change how they are read, every one of them from a
  silent allow to a prompt; ordinary work that interpolates a variable into an
  argument, a path, or a `gh api` endpoint is untouched. A sibling guard that
  answered an unreadable command by falling back to a weaker check now runs both,
  because the weaker check cannot see the most destructive shapes.

- Merge gate: a duplicated CI dispatch no longer wedges the scheduled-review relief. When one commit carries both a cancelled and a successful run of the same job, the leaks relief now drops the superseded cancel exactly as the CI check already did, so a single `--check-pr` can no longer report CI green while telling you a green job is not green.

- **Sessions that read untrusted incoming messages can no longer reach tools they
  were never meant to have.** Genesis runs email replies and community messages
  through deliberately restricted sessions, because the content is written by
  someone else and may try to give Genesis instructions. Those restrictions were
  written as a list of what to block, which means anything nobody thought to add
  to the list was allowed by default — so the restricted sessions could read the
  queue of pending messages, and could ask for a disk to be grown or a backup to
  be taken. Both of those last two still needed explicit approval before anything
  happened, so nothing could have changed the machine on its own; the problem was
  that they were reachable at all. The community-message session was missing the
  restrictions entirely, where the email one had them. Both are now covered, and
  the rule is checked the other way round: every tool on that server must be
  explicitly either blocked or justified in writing, so a newly added tool fails
  the check until someone decides which side of the line it belongs on.

- **The graph projector no longer publishes a snapshot the database was never
  in, and no longer overwrites a newer one with an older one.** Building a
  projection reads the links and the visibility metadata separately, and each
  read was taking its own view of the database — so a change that touched both
  in one transaction could be half-seen, publishing an edge whose endpoint
  should have been hidden. Both reads now share one view.

  The claim on the right to publish had a matching gap. A projector takes a
  lease before it builds, and the lease expires on a timer so a crashed run
  cannot hold it forever — but nothing checked the lease was still held at the
  moment of publication. A build slower than its lease could therefore hand the
  claim to a second run and then overwrite that run's newer result, walking the
  graph backwards with nothing to notice. Publication now happens only while the
  lease is still held, and a build that lost it is discarded rather than
  published: whoever holds the lease has data at least as fresh, so refusing is
  recoverable where reversing is not.

  Two smaller corrections. A memory whose expiry timestamp is present but empty
  was hidden by the incumbent path and shown by the new one — the two backends
  disagreeing about the same row, which is the one thing this work exists to
  prevent. The same was true of a bare zero. Both are fixed, and the parity is
  now settled by running the incumbent query itself over the whole range of
  values that column can hold, rather than against a list of cases someone
  thought of. And the projection command answers an unreadable or missing
  database with one line and an exit code, the way it already answered an
  unreachable engine, instead of a stack trace.

- **Four things that only broke on a genuinely fresh machine are fixed, found by
  the new fresh-install check on its very first run.** A clean install of Qdrant
  reported a setup warning anyway and printed "Genesis REQUIRES Qdrant" directly
  under the line saying it had just installed it. The `genesis` command and the
  auto-cd on login both assumed the repo lives at one specific path, so on any
  other clone location the command was installed already broken. One service unit
  shipped with a placeholder where a directory should be, which systemd rejects.
  And a server that failed to start printed a warning and carried on as though
  the install had succeeded. None of these are visible on a machine that already
  has Genesis running, which is why twelve weeks of installer changes went by
  without anyone noticing them.
- **A failed setup now says what went wrong.** The installer tracked warnings as
  a single yes/no flag set from eight different places, several of which print no
  warning text at all — so "setup completed with warnings" meant scrolling back
  through hundreds of lines to guess which one fired. Each warning now records its
  own reason, and both the summary and the strict-mode failure list them.

- **Qdrant's memory-protection setting is now one the system will actually
  honour.** The vector store that Genesis depends on declared a protection level
  no user-level service is permitted to set, so the setting was refused — quietly,
  with no error anywhere, while every place you could look to check it kept
  reporting the value it never got. Existing installs are migrated in place, since
  this particular service has no other path to receive the fix, and it is
  restarted so the change actually takes effect rather than merely being written
  down.
- **The check that watches for this class of problem now looks at every service
  Genesis installs**, rather than only those whose names begin with "genesis".
  Two of its own services fell outside that, including the one this change fixes —
  so the check would have stayed silent about exactly the thing it exists to
  catch. A test now derives the list from what the repo actually installs, so a
  newly added service cannot quietly fall outside it again.

- **A test run fronted by a package-manager runner no longer slips past the
  full-suite guard.** The guard that stops a whole-suite pytest run on a shared
  box keyed on the command being `pytest` itself, so `uv run pytest tests/`,
  `uvx pytest tests/`, `poetry run pytest tests/` and their hatch/pdm/xvfb-run
  equivalents sailed through while the bare form was blocked. The shell parser
  now sees through these runners to the command they carry — but only through
  their `run` subcommand, deliberately: treating the whole tool as a
  pass-through would have made the parser skip past the first word of *every*
  subcommand, hiding commands that other guards catch today. Any other
  subcommand still resolves to the tool itself, so `uv pip install pytest` and
  `poetry add pytest` install the package rather than looking like a run, and a
  package name handed to a flag (`uv run --with pytest ruff check .`) is read as
  the dependency it is. Replayed against 37,568 real commands from one install's
  history: one resolution changed, and it was a `poetry run python` the parser
  had been reading as `poetry` — the fix behaving, not a casualty.
- **An unfamiliar option no longer lets a whole-suite run through that guard.**
  These runners accept options before the command they carry, and some of those
  options take a value of their own. The parser recognised a list of them, and
  where an option was missing from that list its value could be mistaken for the
  command being run — so the guard looked at the wrong word, concluded the run
  was something else, and allowed it. Keeping such a list complete means tracking
  another project's options as they change, which is not a thing this parser can
  promise. It now stops at the first option it does not recognise and reports
  what it is sure of — that the command is being carried by one of these runners —
  which is enough for the guard to find the run. Commands that are not test runs
  are unaffected, and a familiar option still resolves exactly as before.

- **A malformed tool call no longer reports itself as a missing value.** When a long
  free-text argument is sent with a closing tag that does not match its opening tag,
  every parameter declared after it is absorbed into that string, and the server then
  reports those parameters as *missing* — which reads like the caller forgot them, or
  like a broken tool. One such call was refused six times in a row before the shape was
  recognised, with the evidence sitting in the error's own echoed input the whole time.
  The MCP middleware now inspects a missing-argument error, and when a *provided*
  argument's text contains the markup for a parameter reported *missing*, it replaces
  the message with both readings — the malformed-call one and the genuinely-absent one
  — and what to do in each case. It is offered as a possibility rather than a verdict,
  because prose *about* tool-call syntax looks identical from the arguments alone. This
  runs on every MCP tool rather than the handful that happen to have that shape, and
  only ever on a call that is already failing — it never alters arguments and cannot
  make a well-formed call fail.

- **The rm-guard now reads a multi-line command the way the shell reads it,
  comments included.** It already joined a line continuation before checking a
  delete's flags and target, which is what the shell does — but it did so with a
  pattern that had no notion of a comment, and a comment ends at the newline rather
  than continuing past it. A trailing comment on a multi-line command could
  therefore make the guard read the two lines as one, and stop recognising what the
  second line was. It now tracks quotes and comments while joining, and where a
  comment begins is checked against the shell itself rather than inferred — both
  ways round, since a `#` that only looks like a comment start must not stop the
  join either. Which closing parentheses end a word is part of that question, and
  the shell's answer is not uniform: a `)` closing a process substitution, an
  extglob pattern or an array assignment leaves the word open, while one closing a
  subshell or an arithmetic command ends it, and one form is decided by a shell
  option rather than by syntax, so the setting this hook actually runs under is
  what settles it. Each form was checked against the shell rather than reasoned
  about, because an error in either direction lets a payload through — and the
  checking is what corrected the first two attempts, both of which had a member
  wrong. Every parenthesis is now classified from its own opener rather than
  inheriting from an enclosing one; a single depth count could express neither
  that nor the case where a comment swallows a bracket.

  Nothing else about the guard changes, and the measurement says so with its
  denominators. Structurally: a command with no line-joining backslash is
  returned byte-identical — verified rather than assumed, on all 106,415 distinct
  commands in this install's history at the time of the run, and independently
  fuzzed over 198,927 generated strings. Only the 1,838 (1.7%) that do contain one
  can be affected at all, and across those no verdict moves in either direction.
  The absolute counts drift as history accumulates; the 1.7% reachable fraction
  held across runs.

  That last figure is a realism check and not a coverage claim, and the
  difference matters: the shapes this fix deliberately does change are ones real
  history happens not to contain, so a corpus finding no movement is the expected
  result rather than evidence of no effect. What covers those is the enumeration
  above, each form checked against the shell one at a time.

  Quoting is tracked per nested command, not as one flag for the whole string.
  A command substitution runs a fresh command with its own quoting, so a quote
  belonging to that nested command does not close the word around it — and a
  single quote-state slot read the nested opener as the outer closer, left
  quote mode early, and then mistook ordinary quoted data for the start of a
  comment. The join was then skipped where the shell performs one, failing in
  the same direction as above: the flags split across the newline and the
  delete stopped being recognised. Both spellings of the construct are handled, along
  with parameter expansion, where the shell never starts a comment at all.
  Each was measured against the shell one at a time rather than generalised
  from the first, and the state each opens is asserted to close again — a
  context that never resets would disable comment recognition for the rest of
  the command, which fails in the other direction.

- **Correcting a memory with a short id handle did nothing, silently.**
  `memory_store` accepts a `supersedes` id, and the proactive recall hook prints
  memories as short `id:<8-char>` handles -- but only the READ path
  (`memory_expand`) resolved those handles. On the write path the handle went
  into an exact-match update, matched nothing, and the "did I find it" answer
  was thrown away. The call returned an id, so the caller read success, while
  the stale memory stayed live in recall and the only trace was a `succeeded_by`
  graph edge pointing from something that was not a memory.

  Measured on one install: three such edges inside one 14-minute window, none
  ever before, and every one of the three handles named exactly one memory --
  resolution alone would have prevented all three. Two of them were retried with
  the full id about ninety seconds later, so the silence also duplicated the
  correction; the third was simply lost.

  `supersedes` now accepts the same short handles the rest of the system hands
  out, never guesses an ambiguous one, and refuses a full-length id that names
  no memory instead of writing a dangling edge and reporting nothing.

  The target is resolved **before the new memory is written**, which is what
  makes the rejection cheap and total: nothing is stored, nothing is linked, and
  there is no half-completed operation for the caller to reason about. That
  ordering also removes a way the old code could corrupt a memory outright --
  resolving after the write meant a short handle could match the row the call
  had just created and deprecate the new memory as its own successor.

- Corrected the instruction files where they described the merge gate wrongly. Seven claims ran in the dangerous direction — they made the gate sound looser or narrower than it is — including one that said a pull request with no review at all could merge on CI alone, and one that said editing a hook changed nothing until a sync step ran. CodeRabbit, whose findings can block a merge on their own, was named in none of them.

- Background task failures now reach the reflex nerve. Two independent defects each discarded every one of them without a trace: the event was published one severity level below the threshold the reflex ingestor listens on (and the ego deliberately ignores it as reflex-owned, so it reached nothing), and its payload carried no `error_type`, so an event that did arrive was dropped at the ingestor's admission gate. The emit now clears the severity floor and builds its payload through the shared `failure_details` helper, and an end-to-end test drives the real producer into the real consumer so neither gate can silently reopen.

- **Owner-facing messages no longer skew outreach reply-rate calibration.** Every
  message Genesis sends to *you* over Telegram or voice (approvals, digests,
  blockers, alerts, GitHub-activity pings, career nudges, marketing reply-pings,
  career "bite" alerts) was silently creating reply-received / positive-engagement
  predictions in the calibration ledger. Those go to you, not to an external
  recipient, so they can never get a reply — the predictions always resolved as
  "no reply", quietly dragging Genesis's measured outreach reply rate toward zero.
  The prediction hook now skips the owner-facing channels (Telegram / voice), so
  calibration reflects only genuine external outreach. Discriminating on channel
  (not category) is deliberate: a cold-marketing prospect email carries the same
  `notification` category as an owner ping but goes to `email` — so it stays
  correctly calibrated, while the owner ping is skipped. A one-time migration
  also **voids the historical** owner-facing predictions already in the
  ledger, so the existing contamination clears at the next grading recompute
  instead of lingering in the all-time reply-rate cell.

- **Provider-outage notifications remain visible in a crowded observations table and describe only recorded evidence.** The outage clock is now read with a hash-scoped, index-covered query instead of a fetch-100-and-filter pass — on an install with more than 100 unresolved routing observations, the old read silently dropped the target provider row and reported no outage at all. Separately, sparse failure trips remain accumulated until a recovery is recorded, so a low-traffic provider can still reach escalation; messages describe the absence of a recorded recovery rather than claiming continuous call failures.

- **The guardian briefing's observation digest announces truncation.** The
  dynamic briefing showed the 15 newest unresolved observations with no hint
  that more existed — on a busy install the oldest (longest-standing)
  problems silently vanished from the recovery brain's context. A full
  digest now carries a "showing the 15 newest of N" marker (or an honest
  "total unavailable" note if the count fails); a short read needs neither.

- **Ambient background workers no longer clutter the session-resume picker.**
  The shared headless runner (memory arbiter, session-ledger extractor,
  repo-pulse matcher) spawned its one-turn `claude -p` calls from the server's
  own working directory, so their transcripts accumulated under the interactive
  project and surfaced in Claude Code's `--resume` list — previewing as
  injected hook text, since they contain no human prompt. The runner now uses
  a fresh working directory per call, outside any repository, removed again
  afterwards. Nothing can pre-place instructions — or a hook-bearing project
  config, which would execute — in a directory whose name did not exist a
  moment ago, which removes the working-directory surface a long-lived shared
  directory exposed. Directories ABOVE it are a separate matter and are not
  addressed here: Claude Code reads memory files from a working directory's
  ancestors as well. This also keeps the resume picker clean, stops the
  project's session-start hooks from injecting kilobytes of context into
  calls that cannot use it, and denies the judges every built-in tool
  outright.

- **The session table now tells the truth about which sessions exist and
  which are alive.** Interactive terminal sessions had no creation event —
  a two-hourly poll eventually adopted them, recorded as already finished —
  so a live session read as completed, a young one didn't exist at all, and
  a session that died abruptly stayed "active" forever (no code path ever
  wrote a completion timestamp; the dashboard's failed-in-24h counter also
  sat at zero because its queries referenced columns that don't exist).
  Now: a session-start hook registers an honest active row (with the
  process id) the moment a session begins; adoption records live sessions
  as live and stamps dead ones with their best-known end time; every
  status writer stamps the matching timestamp through one disciplined
  function; each prompt advances the activity clock and un-does a stale
  "completed" verdict; and the liveness reaper gains real evidence — a
  fresh heartbeat always proves alive, a provably-dead process id
  checkpoints the row in ~30 minutes instead of 24 hours (lever:
  `close_dead` / `dead_process_minutes` in the reaper config). The
  dashboard stat queries use real columns, counting historical rows via
  their last activity so no backfill is needed. A fourth, unclosable
  session-row writer in the budget tracker was removed outright.

- **A slow boot is no longer mistaken for a failed deploy.** The post-update
  health gate gave the server a fixed three minutes to answer, but a busy
  machine can take five just to finish starting — and the gate then rolled a
  perfectly good deploy back to the old code. The gate now watches the server
  process itself: while it is alive and starting it keeps waiting, and the
  moment the process dies the wait stops early instead of padding out the
  clock. The window is fifteen minutes — three times the slowest boot actually
  seen — and its ceiling is not a chosen number: it is derived from how long
  the host watchdog has been asked to stand down, because a deploy that waits
  longer than that leaves the watchdog looking at a machine it has been told
  to ignore. Raising one now raises the other, and asking for more than the
  ceiling says so instead of silently truncating. Adjustable via
  `GENESIS_DEPLOY_HEALTH_WINDOW_SECS`, never less than the old three minutes.
  The wait is measured on a clock that cannot be stepped, so an NTP correction
  mid-deploy can no longer cut it short or stretch it, and a moment where the
  service state cannot be read is treated as "ask again", not as a death. The
  rollback message now states how long it waited and what state the service
  was in.

- **Merged entities stay findable, and merge chains stay walkable.** Four
  rails for the moment entity merges start applying: asking about something by
  a name that was merged away now finds the surviving entity instead of
  nothing; lookups follow multi-hop merge chains to the live record instead of
  stopping one hop in at a tombstone; a person or organization sharing a name with a concept
  can no longer block the concept lookup and mint an avoidable duplicate; and
  a merge proposal invalidated by identity drift re-enters the review queue
  immediately instead of waiting for the weekly sweep to rediscover it.
  Verdicts also now record which judging policy produced them, and pairs
  judged "distinct" under the old, stricter policy are re-examined under the
  current one — previously they were settled forever on rules that no longer
  apply. The `live` entity-adjudication mode, which merges new pairs with no
  approval step, is turned off for now: it runs as `propose_only` and the
  settings lever refuses it until a check at merge time lands (#2742).
  Approved merges still apply through `entity_adjudication_apply`.

- **A session slot that lost its Claude Code no longer traps you at a bare
  prompt — the door offers to rebuild it, and asks first.** A slot could exist
  as a plain shell (its claude exited, or it was created bare), and connecting
  to it silently attached to that shell instead of launching anything: the
  connection machinery discards its launch command when the session already
  exists, so every entry landed at the same dead prompt. On one install that
  went on for three weeks.

  Connecting to such a slot now says plainly what was found, and — only at a
  real terminal, only on an explicit yes, with No as the default — ends the
  bare session so the normal creation path rebuilds it with its full
  environment. Consent is for the state you were shown, not for the slot: if
  anything changed while you decided (something started in that pane, another
  connection got there first, even the whole server restarting underneath),
  the door stands down and attaches instead. Without a terminal to ask, it
  only reports, and names the manual route. The slot listing marks such
  sessions too, pointing at the door that can now actually fix them.

- **A changelog entry removed at a release cut can no longer come back on its own.**
  `CHANGELOG.md` carried a `merge=union` attribute, which takes lines from both
  sides of a conflicting hunk and therefore cannot express a removal. Cutting a
  release moves every unreleased entry under a version heading, so any branch open
  across a release and merging the base afterwards could have the removed entries
  silently restored — no conflict, exit 0. Measured on a live branch: 287 removed
  lines were present again after an ordinary merge, in a branch that used
  `changelog.d/` fragments and never edited `CHANGELOG.md` deliberately.

  The attribute is replaced by a bare `merge`, which pins git's built-in text
  merge so that a clone's `merge.default` or a configured merge driver cannot
  route the file back through union. Such a collision now conflicts visibly and
  is resolved by taking the base's file and moving the entry into a fragment. A
  branch cut while union was in force still union-merges once on its first
  catch-up merge; adding `/CHANGELOG.md merge` to a clone's `.git/info/attributes`
  closes that for every branch of the clone. The collision the attribute existed
  to smooth over is addressed structurally by `changelog.d/`, one fragment per
  change, which has no merge to get wrong.

- The per-prompt memory hook can no longer lose its own output. Claude Code
  silently files a hook's stdout past a size threshold and shows only a short
  preview, so an oversized injection costs that prompt its recalled memories,
  its list of concurrent sessions, and the safety line that tells Claude not to
  treat another session's text as your input — with no error anywhere. Four
  parts of the injection had no size limit; the worst was a pasted hash or token
  in your prompt, which the hook kept whole and then repeated on every later
  prompt in the session. Each part is now bounded where it is built: over-long
  pasted tokens are dropped rather than cut (a cut one collides with other ids),
  the session-trail line drops whole old topics instead of ending mid-topic, a
  long code signature is shortened while its file path is kept intact, and the
  concurrent-session list is capped with an explicit "+N more not shown" so a
  short list is never mistaken for a quiet machine.

- Discord sends and polls now go to the channel you actually asked for. `outreach_send`
  accepts a sub-channel name (`channel="announcements"`) instead of only
  `channel="discord"`, which always resolved to one configured default — so an
  announcement could land in the dev channel and still report success. A channel with no
  webhook configured is now refused, by both the message and the poll paths, with an
  error naming the setting to add rather than silently posting somewhere else. The
  refusal is treated as permanent: it is reported once instead of being retried for
  hours, and the reason is carried through to the log rather than dropped.

- The one-click fleet door now opens a fresh session picker every time, and a second
  fleet window no longer takes over the first one. A tmux pane mode belongs to the pane
  rather than the client, so the picker survived the terminal window closing — the next
  connect landed back inside the previous chooser, showing another session's preview
  with keystrokes going to the picker instead of the app. And because every window
  attached to the same landing session, opening a second one reset the pane under the
  first, ending whatever was running there. The door is now a script that resets the
  pane only when nobody is attached, and hands a concurrent window its own short-lived
  session instead. The numbered slots are untouched either way.

- **The Qdrant client is now pinned to a version the bundled Qdrant server
  actually supports.** The client dependency carried no version constraint at
  all, while the installer defaults to installing server 1.14.0 when none is
  present — so a fresh install pulled whatever client was newest that day and
  paired it with a server three releases behind. Qdrant's own rule is that the
  two must share a major version and differ by at most one minor, so every client
  start logged an incompatibility warning.

  That drift was not only cosmetic. The 1.16 client removed a search method the
  memory near-duplicate check still called, so on any install that had picked up
  a 1.16-or-newer client, that check could only fail. It was not reached from
  anywhere in the running system, which is the sole reason nobody saw it — and
  also why no test caught it. That call now goes through the same query path the
  rest of the memory system uses, so it no longer depends on a removed method,
  and the migration is covered by tests.

  Two limits worth stating plainly. The new tests cover the migration and the
  filtering around it, not the check as a whole — it looks memories up by a
  payload key that most stored points do not carry, so it only ever examines a
  small fraction of the store. That is a longer-standing problem, tracked
  separately and deliberately not changed here. And the pin is now enforced by a
  test that re-derives the client/server compatibility window from both files,
  so the two can no longer drift apart quietly.

  Nothing about your stored memories changes, and the server is untouched. If you
  had previously installed a newer client by hand, updating will move it back into
  the supported range.

- **Claude Code sessions are far less likely to silently lose an MCP server that
  was slow to start.** A session starts eight MCP servers inside about eleven
  seconds, and
  Claude Code gives each one 30 seconds to connect. The heaviest Genesis server
  needs roughly 10 seconds on an idle machine — mostly import time before it runs
  any of its own code — so on a loaded box it can exceed the limit, and when it
  does the session simply runs without that server's tools for its entire life.
  Nothing announces this. On the install this was found on, a session had been
  running without all 34 memory tools and it was noticed only by reaching for one.

  The limit is now raised to 120 seconds for every install. That is roughly eleven
  times the typical connect, so a server has to be genuinely stuck rather than
  merely slow before it is dropped.

  The cost is only paid when something really is broken, but it is slightly wider
  than just a slower start: Claude Code applies this same limit to other
  server operations, so a server that wedges mid-session now holds a request for
  up to two minutes rather than thirty seconds. Against the shortest background
  task budget that carries servers at all, that is a fifth of the budget in the
  worst case.

  This covers Genesis's own background sessions too, and that half matters more.
  A dispatched session runs outside the repository, so it never reads the
  project's settings and would otherwise have kept the old limit — and it is
  exactly the case where nobody is present to notice that a session spent an hour
  working without the memory it thought it had.

  This reduces how often the problem happens; it does not make it visible. Being
  told when a server fails to connect is tracked separately.

- **Two ways of writing a command that the safety guards could not see.** The
  guards work out what a command actually runs by looking through the small
  helper programs people put in front of it — the ones that set an environment
  variable, apply a timeout, or feed arguments in from a list. To do that the
  parser has to know which of a helper's own options are followed by a value, so
  it can step over them and reach the real command. Two entries in that table
  were wrong in the direction that matters: they claimed an option takes a value
  when it only takes one optionally, so the parser stepped over the command
  itself and the guards examined its first argument instead. Both
  forms run, and both reached a guard that would otherwise have refused them.
  The two wrong entries are gone, and a test re-derives the table from each tool's
  own option parser on the machine it runs on — falling back to its documentation
  where the parser cannot be read — so an entry that stops matching reality fails
  the build rather than going quiet. Reading the documentation alone was not
  enough: one helper accepts an option that its own help text never mentions, and
  a check that trusted the help would have condemned a correct entry and invited
  a repair that reopened the hole. Replayed against 68,308 real
  commands from one install's history, 1,744 of which use these helpers: no
  command's reading changed except the ones this fixes.

- **Every panel on the dashboard gets its header back, and the voice page can
  scroll.** Panel titles had been rendering as a narrow vertical strip down the
  left-hand side of their panel, with the title itself floating level with the
  middle of the body rather than above it, and the panel's content pushed into
  the remaining space. On a full-height panel that put the title hundreds of
  pixels below the top of the box it names, often past the bottom of the window
  — so panels looked untitled, with a wide empty gutter beside them.

  The cause was a layout rule in the inherited base stylesheet, written for a
  different application's side-by-side panes, landing on a selector this
  dashboard also uses. The dashboard's own rule for the same selector never said
  anything about layout, so there was nothing for it to override. Nothing errored
  and nothing logged; it simply looked wrong, on every tab, for months.

  Looking for other instances of the same leak found a third, and then a fourth.
  The base stylesheet also pins the page root to a fixed, non-scrolling viewport.
  Four pages undo that, in four separate places — the voice page did not, so it
  could not be scrolled at all below the first screenful, and neither did the
  login page, which is built as a string in Python rather than as a template and
  had been overlooked twice for that reason. Both neutralisations now live on the
  stylesheet every page loads, rather than being copied per page, which is what
  let two pages be forgotten.

  Nothing automatic guards this class yet. A check that reads the stylesheets a
  page links was built alongside this fix and has been separated onto its own
  branch: it found the original defect from the text alone, and it was also
  found fail-open in four consecutive reviews, so it is not something to hold a
  verified one-declaration fix behind. Worth saying plainly because the absence
  is the status quo rather than a regression — the three instances above were
  each found by a person looking at a page.

- **The Zero-Drop tab stops showing a confident count while its own refresh is
  failing.** The board already refused to print a number when the detector
  behind it was stale or blind. It did not apply the same rule to itself: once
  a board had loaded, a refresh that started failing left the body carrying a
  loud "Refresh is FAILING" banner while the badge above it still showed a
  figure, for a board that could by then be hours old. The badge now withholds
  the number and says which fault is responsible.

  The fix runs a little deeper than that panel. Every dashboard panel tracked a
  single value that meant both "what is my transport doing right now" and "did
  the last attempt fail", and starting a retry reset it — so any panel asking
  whether its data was trustworthy got "yes" for the whole duration of each
  retry. Worse, when a server returns errors for a sustained period the client's
  backoff grows until it outlives the poll interval, at which point every
  request is overtaken by the next one before it finishes and the failure is
  never recorded at all. A panel could then show a confident, hours-old number
  with no warning of any kind, indefinitely. A failure is now remembered until a
  request actually succeeds.

  That remembering has one visible side effect beyond the Zero-Drop tab, named
  here rather than left to be discovered: a panel that has never loaded
  successfully now shows the previous attempt's error text while it retries,
  where it used to show nothing. Ten panels can reach that state. It reads as an
  improvement — a panel that is failing says why — but it is a change, and it
  arrived as a consequence of the fix rather than as its goal.

  The same correction was needed in a second place: the equivalent function for
  modal panels cleared the failure too, and was missed the first time. Five
  modal surfaces render that text.

  It also names every fault that holds at once — "detector blind + refresh
  failing" — rather than showing only the most severe. The previous wording
  picked one, so anyone who fixed the fault they were shown would have seen the
  number return with a second fault still true.

  Separately, when the open-pull-request cache cannot be read at all, that part
  now says "Unavailable — … (not a zero)" in the same words as every other part
  of the board. It had been printing the bare reason, which reads like a
  statement of fact rather than a part that could not be read; the distinction
  was carried only by the text colour.

- **A red CI `test` job now names its failures somewhere log truncation
  cannot reach.** The job printed one line per test across ~26,000 tests, and
  GitHub's log APIs drop the tail of an oversized step log — which is exactly
  where pytest prints its FAILURES block. Measured on a real red run: the
  log, the failed-step view, the whole-run log and the raw endpoint all cut
  at ~44% of the suite, so the run named no failing test through any route,
  and the junit report that held the names was discarded with the runner.
  Diagnosis of that failure required compiling the runner's git version from
  source to reproduce locally — a cost this change exists to make nobody pay
  twice.

  Three changes, one per layer: pytest drops `-v` for `-rfE` (compact output,
  with both failures AND errors listed in the terminal summary — an error is
  a different outcome from a failure, so `-rf` alone would leave every
  collection-time breakage named nowhere); a failure-only step parses the
  junit report and writes every failed/errored test id — whole, with the
  first line of its message — to the job's step summary, which the Actions
  UI renders outside the log entirely; and the junit report itself is
  uploaded as a run artifact so the full tracebacks survive the runner. The
  summary script always exits 0 — the job is already failing, and a narrator
  that can fail replaces the real failure with its own — with missing and
  unparseable reports carried as printed notices, including the case where
  pytest died before writing any report at all.

  The step summary is bounded to GitHub's 1 MiB cap, which is a hard external
  budget rather than a preference: a summary over it is not rendered, so an
  unbounded list would print nothing at all and take the artifact pointer
  with it. The budget is spent on WHOLE rows, the closing pointer is reserved
  before any row is written, and a shortfall is stated with its denominator —
  measured at 12,000 failures, the summary lands at 1,048,288 bytes with
  7,674 rows listed and the remaining 4,326 declared.

- Code-intelligence requests now use a FULL-sync SQLite queue with atomic
  coalescing, ownership, retry, and terminal-outcome transitions. Existing
  file markers are atomically claimed during migration so rolling-upgrade
  writers cannot be unlinked, and lock-contended enqueues use a durable spool
  instead of disappearing. The runner stops and restores work when its
  escalation query errors, still defers if its execution lock is unavailable,
  and only clears last-resort index caches after their rebuild is durably queued.

- **A plan's bookmark keeps its name when the plan has frontmatter.**
  `plan_bookmark_hook` names a bookmark by the plan document's first markdown
  heading, and that name is what `/unshelve` searches by keyword. It scanned
  only the first ten lines of the file, so a plan opening with YAML
  frontmatter pushed its heading out of range, the title came back empty, and
  the bookmark became unfindable — silently, since nothing raised and nothing
  logged. Frontmatter is now skipped before the scan, with the ten-line window
  kept (a heading far down a plan document is a section, not its title) and a
  bounded search for the closing fence so a leading `---` thematic break is
  still read as one.

- Temp protection now starts on a fresh install regardless of where the repo is cloned, and the installer says so when it does not. `genesis-tmp-watchgod.service` was being installed from a stale second copy of the unit that hardcoded `~/genesis`, so on any checkout somewhere else the service was enabled but could never start. `systemctl enable --now` reports success for that failure, so the installer did too; it now checks the service is both enabled and actually running, and warns with the reason when it is not. An existing install already broken this way is healed by the ordinary update path — `bootstrap.sh` rewrites the unit file and reloads systemd, which is enough to bring a restarting unit up — and the installer additionally clears a unit left in a failed state, for the case where the restart loop had given up.

- Short-lived `claude` subcommands no longer take a persistent tmux slot. `claude setup-token`, `claude mcp list`, `claude update` and the like print and exit, so a slot left them occupying session capacity and holding a tmux session open after the command was gone — and the pane's scrollback was captured on exit, which for `setup-token` means a long-lived credential. That capture was already scrubbed and fails closed; this keeps the credential out of the captured region in the first place. Long-running subcommands deliberately keep their slot: `claude agents`, `claude gateway` and `claude ultrareview` are a TUI, a daemon and a minutes-long job, and losing them to a dropped SSH connection is the failure the slot exists to prevent. Bypassed commands run in the repo, matching where a slot would have put them, so `mcp` scope and `project` state still resolve the same way.

- An approval you granted for one step of a background task no longer releases that whole task if the task later blocks. Groundwork ships with it for the blocked-task approvals themselves: releasing a blocked task will consume the approval that released it, so a task that blocks a second time asks again instead of resuming on your earlier answer, and the approval is matched by meaning rather than by text so it can no longer be missed because of how the request happened to be stored. Nothing creates those blocked-task approvals yet — that arrives separately, on top of this.

- Pin GitNexus 1.6.12 from one shared version file so fresh installs and updates cannot downgrade an index into an unreadable storage format. Installers upgrade older versions but refuse to auto-downgrade a newer one; indexing and a fail-closed MCP launcher refuse every unreviewed version. Installers also enforce the package's Node engine range instead of falling back to a floating npm release.
- Align fresh-install and bootstrap Node checks with the repository's documented Node 22 floor for the pinned Claude Code release.
- Preserve the machine-local Codebase MCP kill switch across new sessions and bootstrap registration while its OOM fix remains under review.
- Give GitNexus rebuilds their measured 8 GiB scope instead of the shared 2 GiB cap that killed every full rebuild; Codebase indexing retains its separate 2 GiB limit.

- A `claude` subcommand that skips the session slot now validates the temp directory it resolves, instead of accepting any directory that merely exists — a root-owned leftover from an earlier `sudo` run would previously have been handed to `claude install`/`update` — and says so when no usable one is found rather than failing silently. An explicit `TMPDIR` from the caller is still honoured as-is. The installer also reports temp protection as enabled only when the unit is *durably* enabled, since `systemctl is-enabled` also succeeds for runtime-only enablement that disappears at the next reboot.

- **The deploy guidance no longer prescribes the one mechanism that has killed
  deploys, and the advisory no longer goes quiet when you use it.** Both the
  genesis-development skill and the pre-deploy advisory told sessions to run
  `update.sh` in the background — and the advisory returned early, saying nothing,
  whenever a command actually did. Deploys started that way have been killed
  mid-run twice, once leaving the server down through bootstrap. Nothing ever
  contradicted the advice in practice, because the guard only spoke when the advice
  was being ignored.

  Both surfaces now prescribe handing the deploy to systemd as a detached unit, and
  the advisory fires whether the deploy was foregrounded or backgrounded.

  The advice is also narrower than it was, and deliberately so. It covers
  `update.sh` only. `bootstrap.sh` and `host-setup.sh` both need a channel a
  detached service does not have — bootstrap calls `sudo` unconditionally, and
  host-setup is interactive with a recreate prompt whose no-input default stops and
  renames the existing container. `update.sh` is the only one of the three with no
  `sudo` calls at all, and the only one actually exercised this way end to end. The
  advisory no longer mentions the other two rather than offer a recipe that breaks
  or destroys something.

  One thing measured while fixing it, which our own notes had stated the other way
  round: backgrounding has no ten-minute ceiling — a 400-second task completed
  cleanly — so the problem was never a timeout. A background task is bound to the
  session, which is a different failure and needs a different fix.

  The skill also now describes what a mid-run kill actually does, which depends on
  the phase: during the pre-update backup no signal handler is installed at all,
  before the merge an interrupt restarts the services it stopped, and from the merge
  onward it rolls back. That distinction matters for diagnosis, because "no handler
  ran" does not tell you which signal arrived.

  Three things a reader can now rely on that were previously wrong or missing.
  `systemd-run --user --scope` does **not** detach — it isolates the cgroup and
  keeps the caller's session — and neither does `setsid`, which starts a new session
  without stopping the caller waiting on it. `--setenv=PATH` is required, because a
  user unit otherwise runs under a PATH that omits `~/.local/bin` and makes
  bootstrap take different branches. And "no signal handler ran" does **not** mean a
  deploy was SIGKILLed: the handlers are installed after the pre-update backup, so a
  kill during the backup leaves the same evidence either way.

  A stale comment in `update.sh` that described the dashboard's direct update path
  as un-isolated and a pending bug is corrected too — it isolates today, and the
  warning now sits on the fallback branch where it still applies.

  The advisory deliberately does not try to work out whether you have already
  detached; it says so, and over-fires rather than risking silence. Narrowing that
  is tracked separately.

- The `genesis-merge-gate` self-exclusion now filters the mirror check by name
  under ANY workflowName: an API-published verdict was observed attached to a
  check suite owned by a different workflow (GitHub assigns the suite, not the
  publisher), so the previous empty-workflowName requirement let the mirror
  double-count as `ci: red` on the local merge path.

- Let capped GitNexus refreshes run on cache-heavy systems by excluding reclaimable file-page cache from memory admission while retaining sibling and cache reserves.

- Closed three suppression holes in the OOM monitor that could silence a genuine container-pressure page: attribution now requires evidence of which cgroup's limit fired (the container root's `memory.events.local` oom counter — the journal names only the victim unit), startup baselining advances the journal cursor so pre-baseline records can never account for a new kill (with a page-and-reanchor fallback when the journal is down at startup), and a reconciliation deficit ensures a unit-failure record that systemd logs LATE retires the kill already paged for instead of covering the next one. Unverifiable trigger evidence now pages instead of suppressing. Also: the CC-sessions dashboard legend matches the 6/8 GB whole-tree thresholds, and the codebase-memory MCP scope is launched with `--collect` so failed scopes can't collide with a reused PID.

- **Updates no longer proceed without a verified SQLite recovery point.** Genesis now quarantines a stably corrupt database, preserves last-known-good backups, aborts updates for database-risk backup failures, and loudly records non-database backup degradation even when an update can safely continue.

- Stop an ambient Git environment from redirecting the review gates' own Git
  queries: the hook launcher now scrubs Git's repository-location variables for
  the hook it runs, not only for its own discovery, and the shared review-state
  and review-scope helpers scrub at every Git call site so a directly-invoked
  gate is covered too. An exported `GIT_DIR`/`GIT_WORK_TREE` previously made
  those helpers describe a different repository than the one under review, in
  every case toward letting a change through.
- Stop a Git attributes setting from quietly lowering how much review a change
  needs. A global `core.attributesFile` marking source files binary collapsed
  the staged diff to nothing countable, and the depth gate then treated a large
  change as a trivial one; the gates now ask Git to ignore that setting when
  they measure a diff.
- Leave your Git configuration alone. Nothing here removes `GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT` or `GIT_CONFIG_PARAMETERS` from the
  environment, because all four are where Git reads `safe.directory` from — and
  on a box where the repository is owned by a different user, such as a
  bind-mounted container or a CI job, removing them made Git refuse and the
  review gates read that refusal as "nothing to review". The push guard also
  needs to see the same configuration as the push it is checking.

- Stop shipped scripts from resolving their own directory through `CDPATH`. With `CDPATH` exported, `cd` searches it, so an install, update, or backup step could resolve into a different checkout — and the Bash command guard could locate its guard scripts outside the repository.

- Hooks, session-boundary workers and timer-driven jobs no longer read or write a database that has been quarantined for failing an integrity check. Previously the central connection factories refused, but the script-side hooks connected directly and consulted nothing — the path by which hook writers kept writing to a database already marked corrupt, which is how the corruption kept returning. A syscall-level replay of every automatic entry point now measures zero opens of a quarantined database, with an unfenced control proving each probe can still tell the difference.
- Closed four read-write openers that could make the problem worse rather than merely observe it: the two MCP health tools, the guardian's deploy-reference read, and the dashboard update route. A read-write open of a quarantined database can write a stale journal back into the main file when it closes, so these were a route by which simply *looking* at system health could corrupt it further.
- Fixed a post-commit hook regression introduced alongside this work: the audit helper git installs into its hooks directory could not import its new dependency from there, so every bug-fix commit silently skipped recording its audit entry. The dependency is now installed alongside it.
- Read-only connections, short-lived standalone connections, session peer-awareness reads, and start-of-session procedure loading are all covered for the first time; a read-only handle still holds the database open against an operator trying to replace it.
- Added structural gates so this cannot quietly regress: the set of connection-opening functions is enumerated automatically and each must perform the check, and a second gate covers the script tree. Adding a new way to open the database fails the build until it is classified. Operator-run command-line tools stay deliberately uncovered — a person is present, and some of them exist to inspect a database precisely because it is suspect.
- Scope is stated honestly rather than implied: roughly a dozen read-only openers inside the application are still uncovered and are listed in issue #2180. The check removes the class of unattended writer that drove the incident; it is not a guarantee that nothing can touch the file, and recovery still depends on stopping the services that hold it.

- **Secrets-gate and provider-rule hardening.** Provider call patterns now
  match an explicit port (`api.openai.com:443`) and OpenAI `/v1/embeddings`.
  `rg x <(curl <endpoint>)` no longer slips the read-only exemption — input
  process substitution, a bare `&` background operator, and executor flags
  (`rg --pre`, `--hostname-bin`, `--pager`) all break the search exemption
  now, and that executor test is SHARED with the `git` branch, which had been
  returning read-only for any `git log|grep|show|diff|blame` regardless of
  its flags. Neither half of that flag has a fixed spelling, so the test is a
  per-subcommand regex rather than a literal table: git accepts any
  unambiguous prefix (`--op=<cmd>` runs), and `-O` bundles into any short
  cluster (`-nO<cmd>` runs). The command is tokenized with `shlex` instead of
  `.split()` because the guard reads it as typed while the shell hands the
  tool a de-quoted argv — `rg "--pre" <cmd>` slipped every table on the quote
  alone. `-O` is scoped per subcommand: on `grep` it is that pager flag, on
  `diff`/`show` it names an ORDER FILE, so matching it everywhere would swap
  one fail-open for a fail-closed. A quoted bare `secrets.env` that resolves
  in cwd no longer gates as
  if it were an operand (`allow_bare` honoured by the stat and inode arms),
  a quoted mention is not a denial, and a heredoc feeding an interpreter is
  scanned instead of stripped — `python3 <<EOF … cat secrets.env` can no
  longer hide a credential read. Globs deeper than two wildcard segments
  gate instead of walking past the hook budget. `behavioral_linter` and
  `secrets_env_access_guard` fail CLOSED when a shared sibling module cannot
  be imported (was exit 1 = non-blocking allow), a failed critical-
  observation write is logged to stderr instead of swallowed, and the hook
  write path uses the canonical guarded `connect_sqlite_rw` factory.
  Wiring `NotebookEdit` read every cell as executable, so a markdown cell
  that merely documented a provider endpoint was hard-blocked by the same
  rule that exempts `*.md` for that exact content — a notebook's path ends
  in `.ipynb`, so the documentation globs could never see the cell. Only an
  explicitly prose cell type is exempt; an absent or unrecognised one is
  still read as code. Both guards' docstrings now state the measured scope
  (3 of 11 access paths, issue #2230) — the correction had landed in one of
  the two copies, leaving the public guard still telling readers the gap was
  two known residuals.

- The merge report no longer says `inline-findings: ok` when review findings
  went unscored. Some findings never count toward the blocking score by design —
  a review bot the gate does not recognise, a comment below Major, an anchor
  outside the diff or on a documentation path — and the row said `ok` because the
  score was zero, which is true and is not what it was read to mean. It now
  carries how many were not scored, pointing at the detail already printed above
  it. Where a count cannot be trusted — some reviewer output cannot be parsed
  into findings, or a review channel under-reported — the number is shown as a
  floor rather than an exact total, and both causes are named when both apply,
  because only one of them is recoverable by re-running.
- A review is no longer silently ignored because the gate does not recognise who
  wrote it. A second bot, or a human collaborator, could leave a finding on the
  diff or in a review body and reach nothing the report mentioned, so the row
  still read `ok`. Those are now counted and named. They are never scored and
  never block — the gate's job here is to make sure the reviewer is *seen*, and
  judging what they said is the reader's. Recognising a format is still what
  earns a weight: an unrecognised comment may carry five findings or none, so
  wherever one contributes, the reported total is shown as a floor (`3+ items`)
  rather than an exact count of findings.
- Nothing about what blocks a merge has changed, and a stranger still cannot
  make it block: only a recognised reviewer's findings are read for severity.

- Request a 4 GiB Codebase batch scope by default. Probe each tool's scope independently and check the Codebase scope's actual cgroup placement, zero-swap limit, effective page-aligned cap, host memory and shared-ancestor headroom before starting the indexer; defer when the full effective cap cannot be accommodated.

- **A shell invocation the shell never executes is no longer reported as a
  command, wherever the option sits.** The parser decided "does this run?" from
  the option bundle carrying `-c` alone, so every option BEFORE it was ignored:
  `bash -n -c CMD`, `bash -o noexec -c CMD` and an invalid `bash -z -c CMD` all
  parse and run nothing, yet each produced a nested segment and every guard
  downstream blocked a command that was never going to execute.

  Measured against the installed bash rather than reasoned about, and the
  measurement changed the fix. No-exec is not a property of the `-c` bundle —
  it is ONE left-to-right scan over the whole option list, and the two letters
  involved do not behave alike. `n` is ordinary shell state: `+n` clears it and
  the LAST occurrence wins, so `bash -n +n -c CMD` really does execute CMD and
  a naive "any `-n` means inert" rule would have hidden a live command — a
  bypass introduced by the fix for a false block. `D` is an invocation action
  rather than state: either sign selects it and nothing later clears it, which
  is why `bash -c +D CMD` was still reported. State now carries in both
  directions across the selector, so `bash -n -c +n CMD` (runs) and
  `bash -o noexec -c +o noexec CMD` (runs) are distinguished from
  `bash +n -n -c CMD` (inert). Twenty-one shapes are pinned by a corpus that
  re-derives every expectation from the real shell on each run, so it cannot
  quietly stop describing bash the way the option table once did.

- **`sh` now carries the union of both shells' option letters, closing a bypass
  on hosts where `/bin/sh` is Bash.** The allowlist is selected by the
  executable's BASENAME, not by the binary the name resolves to. Where `sh` is
  dash — as on the machine the table was measured on — dash's strict set looked
  right. Where `sh` is Bash, `sh -ch CMD` runs the command while the strict set
  rejected the bundle, so the parser returned no script at all and the nested
  command was invisible to every segment-based guard.

  The two error directions are not symmetric: too strict HIDES a command that
  really runs, while too permissive costs a false block on an invocation that
  fails anyway. `sh` therefore takes the union and `dash` keeps the strict set
  under its own name, where the basename does identify the binary. Recorded
  because the first attempt at this fix used bash's letters alone and dropped
  `I` and `V`, which dash accepts — reintroducing the same bypass in the
  opposite direction, on the very host it was being tested on. The existing
  dash-bundle tests caught it.

- **A shell-inertness test no longer passes when the shell it probes is
  missing.** The probe runs the inner interpreter through `bash -c` and asserts
  its payload did not print. With that interpreter absent, bash reports the
  failure on stderr and leaves stdout empty, which satisfies the assertion for
  entirely the wrong reason. The skip is now keyed on the template's own
  interpreter rather than on one shell by name, so a row added later for another
  shell cannot reintroduce it.

- The safety guards now see a command run through a launcher such as `eval`, `su -c`, `flock`, `script` or `systemd-run`. Previously the guards resolved only the launcher, so a removal of a protected directory, or a publish behind one of them, ran unchallenged while the same command typed directly was stopped. The guards do not model each launcher's option grammar and do not inspect what it carries — a carried command can be spelled past any inspection — so a launcher is refused outright when the command is one that guard governs. Re-issue the command without the launcher to proceed.

- **`bootstrap.sh` wrote a broken `agent-zero.service`, and nothing noticed.**
  Unit files are generated from templates containing `__TOKEN__` placeholders,
  and two scripts generate them: `install.sh` and `bootstrap.sh`. Each kept its
  own hand-written list of substitutions, and the lists had drifted —
  `install.sh` learned to substitute `__AZ_ROOT__` and `bootstrap.sh` never did.

  `sed` does not complain about a token it has no instruction for; it copies it
  through. So `bootstrap.sh` produced a unit reading
  `WorkingDirectory=__AZ_ROOT__`, which installs and enables reporting success
  and could only ever fail at start. MEASURED on a live install: that exact line,
  with a correctly substituted path on the line directly below it.

  Both scripts now substitute the same set, and a test pins them to each other
  and to the tokens the templates actually use. The test asks "is every token
  used by a template handled by every generator" rather than looking for tokens
  known to be bad, so the next template that invents one fails immediately
  instead of shipping a unit with a literal placeholder in it.

  In practice this was latent for most installs, since `agent-zero.service` is
  not enabled by default — but the same class had already reached a unit once
  before, and the generators had no way to catch the third occurrence.

  **Nothing to do on your side.** The next time you run `bootstrap.sh` the unit
  is rewritten correctly.

- **Uninstall left stale schedule state behind for three timers.** A timer
  marked `Persistent=true` records when it last ran, in a stamp file that
  removing the unit does *not* delete. `systemd.timer(5)` says to clear that
  state *before* uninstalling such a unit, and `uninstall.sh` does — from a list
  of timer names maintained by hand, which had fallen behind the timers that
  actually exist.

  `genesis-code-intel.timer` and `genesis-backup.timer` were both missing from
  it, and from the disable list as well. Removing the unit files by wildcard is
  not a substitute for either step: it leaves the stamp in place, and it leaves
  dangling enabled links behind. The practical effect is that reinstalling could
  inherit a stale "last run" and immediately replay a run it should have
  skipped.

  All three timers — including the new projection timer — are now disabled and
  have their state cleared, and a test derives the list of `Persistent=true`
  timers from the templates themselves and checks each one is covered. The
  previous test enforced this for a single named timer, so it could not see the
  next two.

- Corrected the grep.app guidance in the web-tools guide and the researcher agent: it is literal/regex code search, not semantic search, and it is reached through the keyless Grep MCP server rather than by scraping the HTML search page.

- The skills inventory in `AGENTS.md` now gives each skill's real directory, so an external agent client (Codex, Cursor, OpenCode) can open a skill instead of guessing its path from its name. Six skills nest inside a container directory and were unreachable by the obvious guess; twenty-three live under a second root entirely.

- `memory_recall` and `follow_up_create` now reach Claude Code with their full descriptions. Claude Code cuts every MCP tool description at 2,048 characters, and both tools were well over it, so the model had been working from a contract missing its tail — including enum values and required-field rules needed to call them correctly. Both descriptions were cut back to their essentials and now fit under that limit with no setting required. A test now fails any Genesis MCP tool description over the limit.

- **The OpenClaw chat-completions endpoint no longer hangs for five minutes, or
  answers with a success status it does not mean, when Genesis is shutting
  down.** If the conversation loop had stopped but was still configured, a
  request would sit waiting for the full five-minute timeout while occupying
  one of only three concurrent slots, so a few such requests made the endpoint
  unresponsive for everyone. If the loop had closed outright, the failure
  arrived after the response had already begun, so the caller received HTTP 200
  with a generic "I encountered an error" body and nothing indicating the
  request had not been served.

  Both states are now refused immediately with 503, and the two possible
  refusals say which one happened rather than sharing a message.

  Readiness is also re-checked at the moment the request is actually
  submitted, not only when the response begins. The two are separated by the
  whole of the request handler, so a shutdown landing in that gap previously
  produced the same five-minute wait the check was added to prevent. And if
  the loop stops *after* submission — which no check beforehand can see — the
  wait now gives up as soon as the loop is gone instead of running to the full
  timeout, so a shutdown frees the endpoint promptly rather than leaving it
  occupied by requests that can no longer be served.

- **A message split across several text blocks is no longer truncated to the
  first one.** Clients that send content as a list of blocks — the usual shape
  when a message accompanies an image or other attachment — had everything
  after the first text block silently dropped, so Genesis answered a shortened
  version of the question with no sign anything was missing. In the common
  case the discarded part is the instruction that follows the attachment. All
  text blocks are now used, separated by a blank line.

  Note the voice endpoints still carry their own copies of this parsing and are
  not covered by this change; see issue #2272.

- **Conversations that arrive through a gateway no longer contribute to the
  context Genesis carries into every session.** Essential Knowledge includes
  the topics of recent sessions, and that list is injected into every session
  Genesis starts — so anything reaching it is read thereafter as Genesis's own
  background. It previously included sessions from any source, so a topic
  chosen by a web, WhatsApp, voice or agent caller sat alongside the owner's
  own work with nothing distinguishing it. Only owner-attended channels now
  qualify.

  The set of channels that count is taken from the same check the rest of the
  system uses, rather than listed separately, so the two cannot disagree as
  channels are added.

  This narrows the surface rather than closing it. Sessions recorded from
  transcript files carry no channel at all, and are still included — excluding
  them would empty Essential Knowledge of almost everything, since that is how
  most sessions are recorded. Closing that properly means recording the channel
  when the session is first written, which is tracked separately.

- **`agent` is a recognised channel.** It counts as outside content for every
  trust decision — it is not owner-attended and its provenance is external —
  so a conversation arriving that way is already handled correctly before any
  endpoint exists to serve one. It also stops routine refusals on that channel
  being logged as warnings about an unknown channel.

- The temp watchdog no longer deletes files a program is still writing, and its
  emergency trigger now measures the real ceiling. Under temp pressure the
  cleanup removed directories without checking whether anything was using them,
  so a long download or install could die mid-write on its own missing files;
  cleanup now leaves a directory alone while a process holds a file open inside
  it (writers that close each file as they go are not covered — the check sees
  only descriptors open at sweep time). Separately, on btrfs-backed installs
  the "disk nearly full" emergency check was reading the shared pool's free
  space instead of the cc-tmp volume's true 2 GiB cap and so could never fire;
  it now computes headroom against the configured capacity
  (`CC_TMP_CAPACITY_MB` in watchgod.conf, default 2048), deliberately without
  reference to how full the underlying disk is: that disk is shared on every
  supported setup, so counting it would have let a full system disk trigger the
  emergency inside an almost-empty temp directory and destroy in-flight work
  without freeing anything. A genuinely full disk keeps its own separate
  trigger. Below 150 MB of true
  headroom every protection is bypassed and everything reclaimable is
  reclaimed — including files written seconds ago and the active session's own
  temp tree, with unix sockets the one exception because deleting them frees
  nothing and breaks cross-session messaging. That exception now actually
  holds everywhere: the cache cleanup used to delete sockets sitting inside a
  cache directory moments after the main pass had deliberately kept them.
  Nothing may stand between Claude Code and its temp space. The emergency is also now evaluated when deciding which cleanup tier
  to run, not only inside the most aggressive one, so a temp budget configured
  larger than the volume can no longer keep it out of reach. Two further
  cleanup defects are fixed alongside: the check that confirms the
  in-use detection is working was itself misreporting failure on any
  real-sized scan, and the cleanup silently reclaimed nothing at all whenever
  no Claude Code session directory was present.

- **A running session can no longer be told its work was interrupted.** The
  liveness reaper checked "is this session alive?" once at the start of a pass
  and then worked through rows one at a time — so a session that came back to
  life partway through that pass could still be checkpointed, and its user got
  a message saying nothing was running on a request that was in fact still
  running. The aliveness check now happens again at the moment of the write,
  inside the same database statement, so there is no window between the two.
- **A session routed to a non-default model records that model, not "Claude".**
  Sessions launched against an alternate model were registered under the model
  Claude Code reports for itself, so the dashboard showed the wrong one and
  disagreed with the session's own heartbeat record.
- **The "last 24 hours" session counts really are the last 24 hours.** Two more
  status queries compared timestamps as text, which let anything sharing the
  cutoff's calendar date through regardless of its time — measured on a live
  install, 45 rows counted where 33 belonged, some of them over a day old.
  The overcount grows through the day and resets at midnight.
- **One malformed session row no longer silences the whole sweep.** An
  unexpected error while checking whether a recorded process was still alive
  could abort an entire reaper pass, leaving every other finished session
  marked active until the next run.

- The fleet door no longer makes tmux log an error on every connection. It
  checked whether a session name was free with `has-session`, and on tmux "no
  such session" is an *error*, recorded in the server's message log. Because
  the check runs on the success path — the name is always free — one entry
  accumulated per connection. The door now tests for the session without making
  tmux raise an error.

  This fragment previously claimed the fix also stopped a yellow bar flashing
  across the window and dropping you into an unrelated session. It did not, and
  the mechanism it gave was wrong: a tmux error is not painted on attached
  clients' status lines from a door invoked as an ssh `RemoteCommand`, where
  the issuing client has no session and the error goes to its own stderr.
  MEASURED on tmux 3.4 — three absent probes added three log entries and zero
  bytes to an attached client's stream, while a `display-message` control on
  that client did paint. The yellow bar had a different cause and is fixed
  separately.

- The slot door no longer adds three entries to tmux's server message log on
  every login. `cc-slot.sh` tested for a session with `tmux has-session`, which
  answers "absent" by ERRORING, and all three probes asked on paths where
  absent is the ANSWER rather than a fault: the free-slot search exits by
  failing, so allocating any slot logged an error, and the two pre-create
  checks logged one each on every first connection to a slot. Replaced with a
  silent `_session_exists` helper that answers from tmux's own `-f` filter.

  This also corrects the mechanism stated in the previous fleet-door fix, which
  claimed a tmux error is "painted on every attached client's status line" and
  that redirecting stderr therefore did nothing. MEASURED on tmux 3.4 and
  refuted: with a client attached through a pty, three absent probes added
  three log entries and zero bytes to that client's stream, while a
  `display-message` control on the same client did paint. For an ssh
  RemoteCommand door the issuing client has no session, so the error goes to
  that client's own stderr, which the redirect did suppress. The yellow bar
  operators actually saw came from a different mechanism entirely — a terminal
  reply delivered as keystrokes into `choose-tree` — and is fixed separately.

- `memory_recall(include_deprecated=True)` now returns graph neighbours for the
  superseded memories it surfaces, instead of returning the memory with an empty
  `graph_neighbors`. The traversal re-applied the visibility filter the caller
  had explicitly opted out of, so an audit or history recall could not see how a
  superseded memory was connected. `memory_expand` gains the same
  `include_deprecated` parameter, which it previously had no way to express. A
  superseded neighbour is labelled `"hidden": true` so it cannot be mistaken for
  a live one. The parameter un-hides superseded memories ONLY — a bitemporally
  expired memory stays out of the traversal, matching the search beside it, which
  applies its expiry filter unconditionally. Ordinary recall is unchanged:
  hiding what recall itself hides remains the default on every backend.

- **MCP servers no longer burn CPU while sitting idle.** Every Genesis MCP server started a background task-queue worker that FastMCP creates by default, backed by an in-process fake Redis whose polling loop woke 100 times a second forever. The queue was always empty — no Genesis tool uses background tasks — so the work was pure waste. Measured on one install: **3.85% of a CPU core per server process**, and with five servers per open Claude Code session that reached **~1.58 cores burned continuously** across 41 processes. Genesis now switches the worker off at startup. Tool behaviour is unchanged; the same 81 tools are served, and the idle cost drops to zero.

- The SSH fleet door no longer drops you into an unrelated session on opening.
  A terminal that answers a DECRQM mode query replies with bytes like
  `ESC [ ? 2004 ; 2 $ y`, and tmux's client key parser delivers them as
  KEYSTROKES — it consumes Device-Attributes replies, which end in `c`, but not
  these, which end in `y`. The door landed straight in `choose-tree`, where `?`
  opens the search prompt (the yellow bar) and a bare digit chooses that entry.
  One stray reply therefore both painted the status line and took the
  selection, deterministically the same one, and the keypress meant to dismiss
  the bar confirmed it.

  The landing screen is now a line-based menu with no single-key actions, so
  stray bytes join a line that fails to parse and the menu redraws. `t` opens
  the session tree, `r` refreshes, `q` drops to a shell, and `Ctrl-b s` opens
  the tree from anywhere as before.

  Ruled out by measurement rather than argument: draining the tty before
  attaching (still stolen), hosting the chooser in a pane (prompt gone, still
  stolen), any recovery keypress (already switched), and upgrading tmux — 3.7c
  was built from source and re-probed and is still stolen, and a pane's query
  never reaches the terminal on 3.4 either, so an upgrade removes no source.

- The fleet door now degrades to a login shell when its picker is missing or
  not executable, and says why. Previously the pane command failed, which
  destroyed the window, then the session, then the connection — so a missing
  file logged the operator out with `[exited]` and no explanation, on every
  connect.

- The generated `~/.ssh/config` block no longer describes the door as opening
  `choose-tree`, or `Ctrl-b s` as reopening "the picker". Both were stale, and
  that text ships to client machines.

- **The test suite no longer fills a RAM-backed temp filesystem.** pytest's own
  temporary tree is now placed under `~/tmp` (on disk) for every local run, not
  just runs launched from a session whose `TMPDIR` already pointed at the
  budget-policed working temp. Runs started from a systemd unit, a detached
  shell, or any context with `TMPDIR` unset previously fell through to the
  default `/tmp`, which on a standard install is a small tmpfs — one suite could
  put hundreds of megabytes into memory and trip the temp watchdog. CI runs and
  any run passing `--basetemp` are unaffected; set `GENESIS_BIG_TMP` to place the
  tree somewhere other than `~/tmp`.

- **PR-watch notifications are credited to the code that produces them.** Five
  files described the `[PRs] …` session nudge as mirroring "the
  `upstream-pr-steward` campaign's own owner notifications". No such campaign
  exists. MEASURED: #792 added the `steward` DirectSession profile and referred
  to an intended "upstream-PR stewardship campaign" in a code comment, but the
  hyphenated slug appears **0 times** in its diff; #1182 coined that slug and
  wrote **all five** prose sites in a single commit. So this was not a claim
  inherited down a chain of commits — one change minted the name and every
  reference to it at once, and no campaign by that name exists. The rows
  `pr_watch` reads are written by `recon/account_activity.py` — a poller inside
  genesis-server whose topics are prefixed `GitHub steward: `, which is what the
  `%steward%` LIKE actually matches — and by the `github-activity-digest`
  campaign's digests. Prose only, no behaviour change; the corrected text names
  both real producers and records that the campaign does not exist, so the
  attribution is not re-derived from the old wording.

- Declining the Grep code-search server (`GENESIS_GREP_MCP_URL=""`) no longer claims an existing registration is still active when it could not read `~/.claude.json`. It now says the state is unknown and tells you to check with `claude mcp list`, instead of reporting a live entry and offering a removal command for something that may not exist.

- **The contributor-review workflow now posts its review request under a maintainer identity.** The automated reviewer resolves who asked from the comment author and refuses a request authored by `github-actions[bot]`, so every request this workflow posted was rejected in seconds without the diff being read — the request had never once succeeded. The request is now sent by a separate job holding a fine-grained `REVIEW_REQUEST_TOKEN`, stored as a secret of a `review-request` GitHub Environment that only that job names and that admits only the `main` branch — so the job that decides eligibility cannot resolve the token at all, and keeps the default token, which drops to read-only. The environment is referenced with `deployment: false`, so no deployment record appears on contributors' pull requests. Because the two jobs run on separately allocated runners, the posting job re-reads the pull request and its issue and re-applies the same eligibility check immediately before writing, so a pull request closed or returned to draft in between gets no request. An unset secret warns and posts nothing rather than falling back to the identity the reviewer rejects, and an expired one fails the job with the reason instead of throwing. Both `github-script` steps are pinned to a commit SHA rather than a mutable tag. A new test asserts these properties across every job in the file, including that no job checks out head code, runs a shell step, or interpolates an expression into a `github-script` body — the property that separates an outside author from a privileged credential in a `pull_request_target` run.
- **A contributor PR can no longer be sealed out of its one automatic review by a comment that merely mentions the reviewer — including the reviewer's own.** The one-review-per-issue budget matched `@codex review` anywhere in any comment body. Measured over the 1000 most recent comments on this repository, that matched 374 bodies and 96 of them were the reviewer's own footer, which reads `Comment "@codex review" or ...` and lands on a pull request before any human asks — so the reviewer sealed an issue's only automatic review by introducing itself, with no retry behind it. The match now requires the request to begin or end a line, which drops 136 of those 374 and loses none of the genuine requests (the four human bodies it stops matching all discuss the trigger in backticks rather than asking for a review). A request this workflow posts is recognised by exact equality against the body it writes rather than by a marker substring, so quoting that marker in prose — it appears in the workflow, its test and this changelog — cannot spend the budget either. The posting step re-reads the pull request immediately before writing and declines if a request is already there, using the same predicate rather than a weaker copy: the workflow's concurrency group is keyed on the event name, so a pull-request event and an issue event naming the same PR run in different groups and could both post. That narrows the window rather than closing it — closing it would need a durable claim on the issue, which needs a wider credential than this convenience feature justifies, and the budget stays documented as the courtesy bound it is.

- **The Secrets editor can no longer delete a setting you never typed in.** Opening a configured override and pressing **Save** without editing it used to submit an empty value, and the dashboard read an empty value as *remove this override* — so an untouched field silently deleted the setting. That was unavoidable from the browser, because the empty string meant both "I did not change this" and "clear this" and nothing could tell the two apart. Clearing is now its own **Clear** button beside Save, sending an explicit instruction, and an empty Save is refused with a message pointing at it. The write API states the same thing: a key you omit is left alone, an explicit null clears it (only for settings that have an unset state to fall back to), and an empty value is rejected rather than guessed at. Reload the dashboard after updating — a tab left open with the old page will have its clears refused until you do.

- Conversation cost tracking (Telegram and dashboard chats) stays accurate on Claude Code 2.1.277 and later, including after switching models with `/model`. Those versions report a resumed session's cost as a running total, and Genesis had been adding that total to its record on every turn, so each reply counted the whole conversation again; a three-turn session was recorded at about 2.8x its real cost. Genesis now records only each turn's share. Costs recorded before this fix are not corrected, and a conversation already open when you update counts its earlier turns once more on its next reply.

- **A background session allowed to run the GitHub CLI no longer inherits the operator's credential.** The sealed configuration directory that keeps such a session from reconfiguring the tool into running arbitrary commands copied the credential file into itself, because that is the only place the tool looks for a token. The argument for the copy was that it lands owner-only inside an owner-only directory and so widens nobody's read access, which is true and answered the wrong question: it handed a session whose stated job is reading EXTERNAL pull requests a token carrying whatever scopes the operator's own login carries — on the install where this was measured, including the ability to delete repositories. The copy is gone, the directory is now synthesised from a constant, and the tool cannot authenticate from it; verified by execution against a control that does authenticate. Installs that already ran the old code are healed by a startup reconciliation, and that is a deliberate addition rather than a free consequence: the directory's own stale sweep runs only when a session is dispatched with a restricted shell, and once no profile ships one — which a companion change brings about — nothing would ever run it again, so a previously-copied credential would sit on disk indefinitely. The reconciliation is wired into the unconditional credential self-heal at startup, is idempotent, and deliberately does not CREATE the directory where none exists.
- **Every credential variable the tool documents is pinned empty, not just the famous one.** Its own environment reference lists four — two for the public host, two for Enterprise — in explicit precedence order, and the tool resolves all of them ahead of the configuration file, so an inherited value would outrank the credential-free directory. Scope, stated precisely because the obvious phrasing overstates it: this covers every session launched through the shared invoker. THREE other paths start a session outside it and are not covered — one passes no environment at all, and two build one by copying the parent's wholesale while carrying a comment saying they mirror the shared builder, which they now do only partially. The count is given because the first draft of this note said "one", described the mechanism of that one, and would therefore have sent anyone grepping for the residual straight past the other two. Out of scope here, tracked as issue #2445. The first version of this change pinned one of the four and was bypassed by the variable documented on the same line: measured, an empty first variable plus a value in the second returns the second. The set is now enumerated from that reference, in one place, with the citation and the version consulted, and the pin is enforced rather than merely set, per variable, and enforced UNCONDITIONALLY. That last word is the correction: an earlier draft put the check inside the per-binary hardening, which the launch path consults only for a session that declared a restricted shell — a shape exactly one shipped profile can produce, which has never been dispatched, and which a companion change removes outright. So the check never ran on any session that has actually run. The pin was set by the builder and never verified, meaning an environment override could have restored a credential on any session. The check is now separate from the binary hardening and runs on every dispatch. Separating them also removed a latent hazard, since the credential entries sat last in the hardening's literal: a name added to the enumeration that collided with one of the hardening's own keys would have silently overwritten it and passed every check, because the verification recomputes the same wrong values. The lesson is narrower than the bug: the same function already enumerated the tool's *program* routes from that same document and said so proudly, and the credential half was then hand-picked from memory.
- **Two limits stated rather than left to be discovered, because the obvious reading of the change is wrong in both cases.** An empty credential variable does **not** by itself de-authenticate: measured, the tool falls back to the configuration file on disk and authenticates normally, and the variable that actually decides the question is the configuration directory — which is pinned only on the allowlisted path. So a dispatched session permitted a shell without declaring an allowlist still reaches the credential on disk. Pinning that variable more widely was built, reviewed and removed: keying it on session origin reads tight but covers six dispatch profiles and every non-owner-attended conversation channel, and would additionally point a process-global data directory at a read-only tree for all of them; pointing it at the sealed directory is a fail-open on precisely the installs that need it, since a rebuild that fails before its sweep leaves the old copied credential in place.
- **The sealed directory is no longer addressed by PATH once its identity has been checked, so link and race tricks against it are closed by construction rather than one spelling at a time.** This is a consequence of the reconciliation itself and was found by auditing it rather than by it failing: every filesystem operation the rewrite performs — permission change, directory listing, recursive delete, file write — follows links, and the routine had been reachable only from a dispatch shape that has never occurred on any measured install, so nothing exercised it. Wiring it into startup makes it run on every boot, which is what turned a dormant hazard into a live one. Measured on the pre-fix code: a sealed directory replaced by a link to another directory came back emptied and locked read-only, and an entry inside it replaced by a link to an unrelated file had that file's contents overwritten with the seal's constant. The link case is now refused before anything is modified, with the link's target named in the log so the planted path is recoverable from one line rather than reconstructed; a link found among the entries is removed rather than followed; and the fast path that decides "this directory is already correct" no longer accepts a link as a matching entry, which is what would otherwise have skipped the repair entirely. The same-shape check was added to the dispatch path too, where it refuses the launch rather than silently continuing. A THIRD route was then found by asking what else writes through a caller-influenced path rather than by waiting for another report: the rewrite lock lives in the sealed directory's PARENT and was opened in a mode that both follows a link and empties what it lands on, before any of the checks above run — measured, an unrelated file was emptied to zero bytes while the reconciliation otherwise succeeded and logged nothing. That open now refuses a linked final component outright and no longer empties an existing real lock file, since the lock needs no content. A FOURTH route was a second name for the same file. A hard link is not a symbolic link, so the type checks did not see it, and the entry then satisfied every remaining test — name, permissions, contents — because it IS the same inode. The sweep therefore kept it and wrote through it, and worse, the fast path that decides whether any repair is needed reported the directory CLEAN while a WRITABLE name outside it owned the tool's configuration file. Measured: an alias was written through that outside name and read back from inside the sealed directory, which is the escape the whole mechanism exists to close. A 0500 directory is the only write protection here, and a second link to the inode goes around it invisibly to any content or permission check. The link count is now part of the comparison, in the fast path as well as the rewrite, because the fast path is what decides whether the rewrite runs at all.

  A FIFTH route closed the enumeration for good: a named pipe. It is not a link and not a directory, and the check that decided which entries counted also did not recognise it as a file — so it was dropped from the comparison set entirely, the directory was reported correct, and the tool then read the planted name THROUGH the pipe and adopted the account in it. Under a name the tool actually reads this is worse than a wrong verdict: reading a pipe with no writer blocks indefinitely, so the comparison would have hung the check itself on every dispatch and, because it now runs at startup, every boot.

  So the file-type test is now an ALLOWLIST — an entry must be a regular file, and a pipe, socket, device, directory or link is a non-match by construction. That subsumes the two link checks written earlier, and it is the same correction this change already made to the restricted-shell classification: five spellings were found one at a time, each after the previous fix shipped, which is what a denylist does.

  Two more properties came out of auditing the rewrite rather than from it failing. It now VERIFIES what it produced before reporting success, because it was write-only: an entry planted between the last file being written and the directory being locked ended up inside the finished directory while the function returned it as prepared, and the mismatch was only noticed on the next call — one dispatch after the session that should have been refused had already run. And the recursive removal is bounded: unbounded, a deeply nested planted tree raised an error the documented contract does not cover, escaping into the caller, and left the directory WRITABLE on the way out. The mode is now restored whatever happens, which is the half that matters — wrong contents are self-correcting because the next call rewrites them, whereas a writable directory is the one thing the mechanism cannot tolerate.

  And the type test had to stop being answered from a cached inspection. The first version of it inspected each name once and then read the CONTENTS by name a second time, so the type gate described one file and the bytes came from whatever the name pointed at by then — measured by an adversarial review against a racing writer, one false-clean verdict in roughly half a million trials, where a clean verdict was only reachable by following a link the type gate had already rejected. One hit is one escape, because a clean verdict skips the repair entirely. Every gate is now answered from an open handle to the exact file the bytes come from, with the handle refusing to follow a link and refusing to block on a pipe. That property is pinned by a test that forbids resolving a name twice, rather than by a race, because a race that never reproduces proves nothing: re-running the same probe against the fixed code finds nothing in 600,000 trials, but its control arm shows the competing writer never won the window even once.

  Two further defects came out of auditing that fix. The comparison read an entry of ANY size into memory before rejecting it, which matters because this routine runs at the first step of startup, synchronously, ahead of secrets and database initialisation, and the read happens before the sweep — so it cannot heal its way out: a sparse file of 64 MiB cost a third of a second and about three times its size in resident memory, linearly, and a multi-gigabyte one is free to create. The size is now compared before the contents, which is also strictly more correct, since a differing size IS a differing content. And one judgement about those two checks was wrong in BOTH directions before measurement settled it. First: that the file-TYPE test was a redundant layer, because a pipe reads as empty — true only of an EMPTY pipe, and a pipe can hold data with no writer attached, in which case every other test passes and only the type test refuses it. Then, correcting that: that the type test was therefore the only thing standing — also wrong, because the size check landed in the same round and a pipe reports a size of zero, as do a socket and a directory, so the size check rejects every one of those first. What is true is that the two shadow each other: neither can be shown to matter by removing it alone, so the mutation arms for them are deliberately combined. Both are kept, for reasons now written down rather than assumed — the type test refuses before anything is read and survives any future relaxation of the size check, and it is the read CEILING, not the size check, that bounds the work a planted file can cause.

  22 tests cover the class, 25 counting parametrised arms, each asserting on what the planted entry POINTS AT rather than on the sealed directory, plus controls proving the reconciliation still removes what it exists to remove, that a real lock file survives, and that the depth bound still clears a genuine two-level extension tree. The limit is stated in the code rather than implied: refusing a linked final component does not cover a linked ANCESTOR directory, which is a broader compromise than this routine can bound.

- **A profile-overlay field that advertised allowlisting the interpreter running the server is withdrawn.** It offered locally-defined shell profiles a way to permit exactly that interpreter and run a module through it. That route stopped launching the moment unreviewed binaries began being refused — the interpreter's name is in neither table, so it raises before a session starts — and the refusal is right rather than something to relax: an interpreter runs arbitrary code from its own command line, so no environment setting confines it. What was wrong was leaving a comment instructing the next author to use it. The field and the comment are gone, the test that used it now names a binary that can actually launch, and reviving the route is recorded as needing a real hardening entry with each escape measured, not a field on a context object.

- **A restricted-shell entry is now matched by its canonical name, which closes a quieter hole.** The guard that enforces the restriction compares the first word of the command, so an entry naming the tool by absolute path — or with stray whitespace — was permitted to run while the lookup that applies its hardening missed it entirely: the session launched with the binary allowed and no sealed configuration, against the operator's own writable one, which is precisely the escape the seal exists to close. Both the applying and the verifying site now resolve through one helper that takes the base name, and an entry is then classified into one of three states: hardened, deliberately needing none, or unreviewed and refused. The base name is what closes the measured hole; stripping whitespace closes nothing, because the guard's own first-token split cannot produce a token containing whitespace, and an entry spelled that way permits no command at all. Refusing an unreviewed name is the correction to a skip that was a denylist in allowlist clothing: the version-control binary is the next thing anyone would allow, and it runs programs of its own accord through its configuration exactly as this tool does, so it now refuses until someone writes its hardening. Case is deliberately not folded, because on a case-sensitive filesystem a differently-cased name is a different binary and folding would apply one tool's hardening to another. The third state is not decoration: a first attempt refused every name it did not recognise, and that broke a legitimate entry for a binary which simply has nothing to harden — measured on a real one. The fix was to name those explicitly rather than to go back to skipping silently, so the set of binaries deliberately needing no hardening is now a declared list, and anything outside both lists refuses.
- **And the second limit is the one worth reading twice: "the tool holds no credential" is not "the session is unauthenticated".** Only the first is this change's claim. Whether the session can reach a credential is decided by its tool scope, not its environment — a profile that permits filesystem reads, skips permission prompts and keeps an outbound notification tool can be instructed by the very content it ingests to read an owner-readable credential file and send it onward. No environment setting touches that. The companion change REMOVES that profile outright rather than narrowing its tools, having measured that a tool-scope fix cannot work while the binary itself is permitted — the tool reads arbitrary files through its own documented flags. A future shell-granting profile faces the same problem; removing a credential from the environment is not a substitute for solving it.

- **The cc-tmp watchdog no longer deletes Claude Code's empty socket
  directories.** Its sweeps already refused to delete a unix socket, but a
  socket is a file and that guard could not protect the *directory* holding
  it — a sockets directory survived only while something inside kept it
  non-empty, which is protection by accident. Reproduced at both deletion
  sites: an empty sockets directory and an empty daemon root were removed
  while a socket-holding sibling survived. Both sites now spare those
  directories and any directory beneath them, while still reclaiming regular
  files inside them and still reaping unrelated empty directories.
  The exclusions are **exact paths**, not name patterns, and both halves of
  that matter. A `cc-socks*` pattern would make an unbounded subtree immortal
  (a directory merely *starting* with that name would pin everything under it
  against the watchdog forever), and `cc-daemon-*` is worse than wide — Claude
  Code also uses that string as a `mkdtemp` prefix for throwaway directories
  holding a single log file, so a pattern would empty each one and then spare
  the husk permanently, turning the empty-directory reaper into an
  empty-directory leaker. The paths are enumerated rather than computed from a
  single expression, because the watchdog and a Claude Code session do not
  share an environment: measured on a live install, the watchdog runs with
  `XDG_RUNTIME_DIR` set by its service manager while a session under a
  terminal multiplexer has it unset and falls back to its temp dir, so both
  locations existed at once and a default-expansion evaluated in the watchdog
  would have stopped sparing the one inside the budget it sweeps.
  Severity, stated precisely: Claude Code recreates a missing sockets
  directory on demand, so this is not a lasting outage. What it costs is a
  lost race — a sweep landing between the path check and the directory's
  creation makes that session refuse to bind, and it runs without
  cross-session messaging for its lifetime rather than retrying.
  The exclusions are also escaped and normalised before they reach the search
  tool, which takes a *pattern* rather than a literal path. The temp directory
  is configurable, so a bracket, star or question mark in it — or a trailing
  slash — used to leave the exclusion matching nothing at all, deleting the
  very directory it was written to spare. The same omission was found on the
  exclusion protecting the active session's own files, and that one needs no
  configuration change at all to reach: the project directory name comes from
  the repository path, so a bracket in a repository name was enough to make
  the emergency sweep delete the running session's working files. Reproduced
  in every shape against a plain-path control that kept them. Path
  normalisation now happens where the configured value enters rather than at
  each place it is compared, because normalising one side of a comparison
  moves the mismatch instead of closing it.

- **`worktree_lifecycle.py --recover` brings an archived worktree's uncommitted work back when it can, says what it checked, and never deletes a trash entry.**
  Archiving keeps the worktree's git registration, locked as the history anchor. Recovery now moves the archived tree back under that registration when it still belongs to the archive, so tracked edits, the staged/unstaged split, intent-to-add entries and untracked files come back with it. It does not claim the result is exact: it reports what it checked (this archive holds the lock, HEAD is the recorded commit, the index is present) and exits with status 2, naming the problem, when any check fails, for example a missing index, a branch that moved or was deleted since archiving, or a registration this archive does not hold. A deleted branch is reported with the command that recreates it; recovery no longer changes HEAD itself. A check that cannot be answered stops the recovery with nothing changed.
  `--recover --dry-run` takes the same checks before anything is placed, so it reports the same problems a real run would.
  When the registration is gone, recovery recreates the worktree at its recorded ref with its untracked files, as before, and exits 2 if the archive held uncommitted changes it could not restore, or does not record whether it did (archives made before that was recorded); it says which. A worktree's own `.trash_meta.json`, set aside at archive time, gets its name back on both paths, including when it is a directory. Recovery no longer deletes a plain trash directory, and when nothing can be checked out it copies the files into a plain directory and exits 2 instead of moving the only copy.
  Archiving now records the name it wrote its patch snapshot under, and recovery names exactly that file. For an archive without that record, several candidate files are listed without guessing which is the reaper's, since a worktree's own files can leave the reaper's snapshot anywhere in the naming sequence.
  Archive metadata is now written atomically, so a failed write (a full disk, for example) leaves the previous copy intact rather than a truncated file recovery cannot read.

- **locate no longer returns duplicate hits from linked worktrees outside .claude/worktrees/.**
  The repo scope now prunes every linked worktree that git worktree list --porcelain reports.

- **The push and merge guards now account for git's repository-selection
  options.** Besides the working directory and `git -C`, git can be pointed at a
  repository with `--git-dir` / `--work-tree` or the `GIT_DIR`, `GIT_WORK_TREE`
  and `GIT_COMMON_DIR` environment variables. The guards resolved the branch,
  remote and push configuration from the directory a command ran in, so a
  command using those options was classified against the wrong checkout. They
  now treat any of these forms as a working directory they cannot resolve, which
  every check already handles conservatively: a force push is refused, a publish
  asks for approval instead of qualifying as a routine re-push, and a merge onto
  the default branch is refused. The pre-push privacy review now reports that it
  did not scan such a push, rather than scanning the diff of the directory the
  command ran in. Pushing or merging from inside the repository (`cd` or
  `git -C`) behaves exactly as before.

- **The command guards now recognise several shell spellings they previously
  could not see.** The shared shell parser that every Bash guard uses to work out
  which command a segment runs now handles a `NAME+=value` or subscripted
  `name[i]=value` prefix assignment, `env` assignment words that are not plain
  identifiers (including after `env --`), assignments after the bash `time`
  keyword, a wrapper's operands after `--` (`timeout -- 5 cmd`), and a command
  that follows an earlier one inside a subshell. It also no longer treats a word
  containing `=` as an assignment after wrappers such as `nohup` or `timeout`,
  which run that word as the program, and follows `sudo`'s own rule: a path
  containing `=` (`/opt/k=v/tool`, `~/k=v/tool`) is the program it runs.
  Parentheses inside quotes, comments and `${...}` expansions are no longer
  counted as subshell boundaries.

- **Inbox capability-build verdicts are no longer silently dropped when the
  build lane is off.** A `BUILD` recommendation is deliberately skipped by
  follow-up creation because the build lane owns it — but the lane is
  optional, and when it is unwired or disabled it ignores the verdict too, so
  the evaluation reached no consumer at all. With the lane not live, the
  verdict is now recorded by what it says: `build` becomes a pinned,
  user-owned follow-up; `needs_discussion` becomes a pinned follow-up labelled
  as a discussion rather than a build task; a `dont_build` veto is kept as a
  tabled record and never becomes actionable work. Each carries the verdict's
  stated reason. As in the lane, a `build` verdict whose build spec is missing
  or incomplete is recorded as `needs_discussion`, and a BUILD block with no
  valid verdict creates nothing and logs a warning. Rows are deduplicated per
  item and verdict: a re-evaluation that merely rephrases the next step adds
  nothing, while a changed verdict is recorded. With the lane live, nothing
  changes. If you enable the lane later, an item's still-pending follow-up is
  closed with a note once the lane takes that item over (when the item is next
  re-evaluated), because the lane's greenlight card replaces it; and an item
  the lane already holds gets no follow-up if the lane is later disabled.

- **Inbox items replaced by a newer edit are no longer reported as failed.**
  When a file changed while its evaluation was parked on an approval, the old
  row was marked `failed`, so superseded snapshots made up nearly all inbox
  failures and buried the real ones. Those rows now get a new `superseded`
  status, keep their reason in `error_message`, and do not consume a retry. A
  schema migration adds the status and reclassifies existing rows whose reason
  is a supersession (`superseded by newer modification`, the older
  `superseded by new inbox scan`, and `content changed`); other invalidation
  reasons (approval cancelled/expired, source file deleted, content removed
  before retry) stay `failed`. The user-ego inbox backlog count accounts for
  the new status, and the inbox overlay gains a superseded tab; each status tab
  now asks the server for that status instead of filtering the newest 50 rows,
  which could hide older rows of the chosen status. `scripts/inbox_check.py`
  now applies pending schema migrations before it runs. Detection behaviour is
  unchanged: superseded rows were already invisible to the known-file scan
  and the retry lane.

- Proactive memory recall is faster: the recent-files keyword lane no longer ORs in words that appear in half or more of all memories (which FTS5's ranking ignores anyway but still had to score across the whole store), and the procedure-suggestion cache now refreshes in the background instead of on the request. Every recall now also logs a one-line stage timing, so typical latency is measurable from the server journal.

- **Eval health now reports each model profile separately and separates harness noise from wrong answers.** When a provider was repointed at a new model, `eval_staleness` summed both models' history under one row and labelled it with whichever profile ran last, so its counts described neither. Each `(provider, profile)` now gets its own entry with a `recent` block (last 30 days of that profile's runs) showing the wrong-answer `failure_rate` and the `infra_rate` (rate-limit skips plus provider errors such as timeouts). `unreliable` now fires on the infrastructural share, so a timeout-heavy run is flagged while a reproducible wrong-answer rate is reported as a rate instead. Manual benchmarks (`scheduled: false`) and profiles a provider has moved on from (`superseded: true`) are no longer reported as `stale`, and staleness is measured from the last scheduled run, so a manual run cannot hide a stopped scheduler. `stale_count` and `unreliable_count` now only count profiles that are still scheduled. A failed query (including a missing `eval_results` table beside recorded runs) now reports `status: error` rather than `no_data`, and each scheduled profile carries `days_since_scheduled`, the age its `stale` verdict was taken on. A run whose timestamp cannot be read is counted in `unreadable_runs` and left out of every age, instead of posing as the newest run or being aged as today. The dashboard's eval section shows the profile, the recent failure rate, and the infrastructure rate whenever there were skips or provider errors (including a window where no case could be scored); a stale profile shows its scheduled-run age rather than the age of a later manual run; manual and superseded rows are greyed out; and a failed or empty eval snapshot is shown as such instead of silently disappearing.

- The stranded-work sweep no longer reports every archived worktree as unreadable: it recognises a reaper archive by its lock reason, its tarball in `~/.genesis/worktree-trash` and its missing directory, and keeps holding anything it cannot prove is an archive. The daily disk-hygiene run also releases worktree claims left behind by sessions that have exited (`GENESIS_WORKTREE_STALE_CLAIM_RELEASE=0` disables that). Archived worktrees are still never deleted: `worktree_lifecycle.py --expire-trash` exists but is off by default and is not scheduled, so it deletes nothing unless you pass `--retention-days N` or set `GENESIS_WORKTREE_TRASH_RETENTION_DAYS` yourself.

- **The Guardian now keeps its own snapshots from filling an LVM-thin storage
  pool, instead of only warning about it.** A thin snapshot costs almost
  nothing when it is taken, then grows with every block the container
  rewrites. The daily "healthy" rollback snapshot was replaced create-first,
  and the replacement can be refused once the pool is past LVM's autoextend
  threshold. When that happened the old snapshot stayed and kept growing. On
  one install it grew for a week until the pool hit 100% and the container's
  filesystem went read-only, while CRITICAL alerts fired every six hours. (An
  earlier entry said this snapshot is "never more than a day old"; that held
  only while the replacement succeeded.) Now:
  - On LVM-thin pools, when the pool refuses the new snapshot on a real
    measurement, the old one is over a day old, and LVM shows the snapshots
    hold at least the larger of 1 GiB and 1% of the pool that no live volume
    maps, the Guardian deletes the old snapshot first, then creates the new
    one. A pool that is full of live container data keeps its rollback
    snapshot, since deleting it would free almost nothing. A failed probe never
    counts as a refusal. On btrfs and dir pools this step does not run; the
    backstop below protects them. A refused refresh retries within about an hour and sends an
    alert, throttled rather than hourly; it used to be only a log line.
  - A backstop runs every tick: when free data space is at or below 3% of the
    pool, or free metadata space at or below 10%, it deletes one Guardian
    snapshot (pre-recovery snapshots first, the rollback snapshot last), waits
    five minutes for a fresh measurement, and alerts on each delete. If no
    Guardian snapshot is left, it alerts that something else is filling the
    pool. No new Guardian snapshot is taken while free space is inside that
    reserve. A rollback snapshot is only ever deleted when a newer one was just
    created, or when this check's health probe found the container healthy;
    while it is not, the backstop keeps them and alerts. The daily prune never
    deletes a rollback snapshot. A delete whose outcome is unknown (the client timed out) stops
    the pass instead of moving on to another snapshot. If relief cannot measure or identify the pool for an hour, it
    says so in a daily warning rather than going quiet.
  - Every Guardian path that deletes or restores a snapshot (relief,
    rotation, the daily prune, the rollback target) now matches only the exact
    names the Guardian generates (`guardian-YYYYmmdd-HHMMSS`, optionally
    `-healthy` or `-pre-recovery`). Previously the prune and rotation matched
    any name starting with `guardian-`, so a hand-made `guardian-…-healthy`
    snapshot could be deleted or chosen as the rollback target. Relief never
    acts on a pool it cannot identify, and nothing here grows the pool.
  - The pool is identified by what incus declares (`lvm.vg_name`,
    `lvm.thinpool_name`), not inferred. A backend that cannot be determined,
    thick LVM, and backends whose pool path is not a mount (zfs, ceph) now read
    as "not measured" rather than falling back to `df`, which measured the host
    filesystem instead of the pool. On btrfs and dir pools, free space is what
    `df` reports as available, so reserved blocks count as used.
  - Levers: `storage_pool.relief_mode` (`live` / `alert_only` / `off`) and the
    two reserves in `guardian.yaml`, plus the `GUARDIAN_POOL_RELIEF_DISABLED=1`
    kill switch, which stops every automatic delete. Recovery runbook:
    `docs/reference/thin-pool-recovery.md`.

- **`web_search` now runs the backend you ask for, and says exactly why a search
  failed.** `backend="brave"` used to run the whole SearXNG-then-Brave chain, so
  it was answered by SearXNG whenever that was up. `backend="brave"` and
  `backend="searxng"` now run only the named backend. A search where every
  backend failed used to report `backend_used: "searxng"` and a bare "All search
  backends unavailable". It now reports `backend_used: null`, and the error
  names each backend it tried with its reason, e.g.
  `searxng: ConnectError: …; brave: API_KEY_BRAVE is not set`. That makes an
  unset Brave key or a missing SearXNG service visible at a glance. Voice web
  search now uses the standard chain (TinyFish first). It used to ask for
  `brave`, which in practice meant SearXNG first, and a SearXNG whose upstream
  engines are rate-limited can answer spoken questions with unrelated pages
  (a weather question returned a software download page). A failed voice search
  is now reported as a failure, not as "No results found", and voice search
  gives up after 20 seconds with a "timed out" error rather than outlasting the
  30-second voice tool-call limit. TinyFish search snippets now carry the same
  untrusted-content markers as SearXNG and Brave snippets. A failed explicit search whose credential is missing now says so (for example
  `API_KEY_TAVILY is not set`); other failures return a short summary, with the detail
  in the server log.

- **The commit review gate no longer asks for approval on commits in unrelated
  repositories.** A commit in a scratch repository with no remote (or only a
  filesystem remote) asked "the open pull request's review history could not be
  read reliably", and was refused outright in a background session, because the
  failed pull-request lookup was read as unreadable evidence rather than as
  "gh has no repository to look in here". The review budget now applies only to
  an open pull request on the configured public repository. A failed lookup
  consults no budget when the repository has nothing gh could resolve to GitHub
  (no remote or branch push target that might be GitHub — a target naming a
  remote counts by that remote's own URLs, so a clone of a local repository
  qualifies — and `GH_REPO` and `GIT_DIR` unset) and the command is a single
  `git commit`, optionally with `-c <key>=<value>` and one
  `-C <dir>` — the one shape that provably commits in the repository that was
  checked. A branch whose open pull request lives in a repository with a
  different name also consults no budget. Nothing changes on the public
  repository itself: an unreadable lookup there still asks in the foreground and
  is refused when dispatched. Every other shape keeps that behaviour too,
  including `cd <dir> && git commit` (use `git -C <dir> commit` in a scratch
  repository), `--git-dir`, `GIT_DIR`, `pushd`, `env -C`, a wrapper, process
  substitution, a commit message built with `$(…)`, a remote that might be
  GitHub, or an
  undeterminable public repository.

- **The desk endpoint refuses output budgets under 16 tokens.** `/v1/desk/chat/completions` used to accept `max_tokens` as low as 1, and at budgets that small the fast lane's thinking-suppressed model can return nothing, which the endpoint reports as a 502 the client then retries. Such a request now gets a 400 that names the minimum, and no provider call is spent on it.

- **A broken `model_routing.local.yaml` no longer takes every model call site offline.** Most mistakes in that file used to stop the router from starting: a key with nothing under it, a value of the wrong type, or a retry profile name that was misspelled or had no body. Genesis still came up, but no model call site could route, and the only sign was one log line. Invalid YAML was ignored with only a warning. Now, whenever the overlay cannot be used, Genesis logs an error, records a health observation that shows on the dashboard, and routes on the shipped config without the overlay's overrides until you fix it. Every restriction the overlay makes still applies during that fallback, so the fallback never allows a call, provider, attempt or spend that your file ruled out. A provider you disabled or marked not free stays that way. The lower `rpd_limit`, `tpd_limit` and `rpm_limit` apply. `never_pays` holds. `dispatch: api` and `dispatch: cli` keep a `dual` call site on that one executor. Where your file and the shipped config leave a call site no executor in common, it is blocked: it stays listed as API-only with no providers, so it runs nothing and is not treated as a provider outage. A narrowed chain stays narrowed and in your order. The lower retry `max_retries` and `max_total_s` apply, including through a call site moved to a tighter retry profile. A provider whose model, type, endpoint, profile or params you changed is left out, because the shipped values are not the ones you run. Longer retry waits (`base_delay_ms`, `max_delay_ms`, `backoff_multiplier`) and lower `jitter_pct` apply too: the retry deadline (`max_total_s`) is checked before each attempt, so where one is set, slower pacing means fewer attempts. A retry wait that cannot be read means no retries, like an unreadable cap. So does a longer `open_duration_s`. Settings that limit nothing, such as `keep_alive` or a description, are set aside with the rest of the file. If an overlay carries both a provider's old name and its new one, the new one wins, as in the normal load; an old name with no settings under it is ignored, also as in the normal load. If every essential call site is blocked, system degradation stays normal rather than falling back to the older check that counts paid providers, and **Reload routing config** re-reads which essential sites are blocked. A retry backoff too large to compute is capped at `max_delay_ms` instead of failing the call. Old provider names from before a rename are read the same way the normal load reads them. Where the file names a provider, call site or retry profile but the value there cannot be read, Genesis reads it the restrictive way: the provider is disabled, the call site is blocked, and `max_retries` is 0. A retry deadline that cannot be read keeps the shipped one, because a deadline of 0 would stop the call site entirely. If the file cannot be parsed at all, nothing can be read from it, and the fallback runs on the shipped config as it is. A bare `call_sites:` header is handled the same way; it used to remove every call site silently. Some values used to load and then fail on every call: text or NaN where a number belongs, or `"false"` in quotes for `free`, `default_paid` or `never_pays`. These are now rejected at load time and trigger the same fallback. A `null` on `free`, `default_paid`, `never_pays` or `retry_profile` resets that field to its default. `enabled` keeps its existing rules: strings such as `"false"`, and empty values, mean off. The dashboard's **Reload routing config** button, and a call-site save with nothing to change, now refuse a broken overlay and keep the running config, and the routing editor shows the dispatch mode the running router actually uses. Saving a call site from the dashboard checks the exact file it is about to write, and refuses any save the next restart would reject. It also refuses to overwrite an overlay file it cannot read. It can still replace a malformed chain on the call site being saved. Separately, an API-only call site whose providers are all disabled now stays listed as blocked, with a warning. It used to be dropped, and the autonomous dispatcher then treated it as `dual`, which could escalate it to a Claude Code session.

- The `pre-merge-commit` git hook is now installed like `pre-commit` and `pre-push`: by `sync-hooks.sh` (which also runs at session start and version-tracks it) and by the hook loops in `install.sh` and `bootstrap.sh`. Previously nothing installed it. Its older check against merging a PR branch back into local `main` never takes effect on current git (the merge head it reads does not exist yet while the hook runs), and it is left inert on purpose; only its new `live` refusal is active.

- **The push guard now recognises `git push -u origin HEAD` as a push of the current branch, and checks the push config for every spelling it recognises.** It used to recognise only the bare branch name, so `HEAD`, its alias `@`, `refs/heads/<branch>` and `HEAD:refs/heads/<branch>` fell to the generic "approve this push" prompt. For those spellings a re-push of an already-published branch prompted every time, and the checks that report a public branch with no open PR, or a push that follows a `gh pr close` in the same command, never ran. They run now. A first push still asks, and a push naming another branch, a tag, or `main` from a feature branch keeps asking as before. A re-push also keeps asking, whatever the spelling, when the repository's config changes what the push does: a push refspec or `push.default` that sends it to another branch, a push URL (`pushurl`, `pushInsteadOf`) that differs from the fetch URL, a remote with more than one URL, a legacy `.git/remotes` or `.git/branches` file, submodule recursion, `push.followTags`, `push.gpgSign`, a mirror remote, or a configured `receivepack` program. The same applies when the push command supplies its own config (`git -c …`, `--config-env`, a `VAR=value` prefix), carries a git option other than one `-C` before `push`, or shares the command with any step other than `cd`, `git status`, `git rev-parse`, `git add` or `git commit`, or with a redirect, since such a step could change the config before the push runs. Previously the plain `git push origin <branch>` skipped the config check entirely, and the bare `git push` checked only some of these settings. A `--dry-run` given as a push-option value (`git push -o --dry-run …`) no longer counts as a dry run.

- **The safety guards now refuse a command they cannot read line by line.** When a command is split across lines with trailing backslashes and names an operation a guard checks (`git clean`, a git command that discards uncommitted work such as `reset` or `checkout`, a force push, a commit, an `rm`, a worktree removal, or a `pytest` run), the guard refuses it with a message saying to put the command on one line. A continued command cannot be read, so a mention counts even when it is only in text such as a commit message. The pull-request-close, capped-read and tmux advisories give a short note instead of their specific check. The guards also read a checked operation the way the shell assembles it from quotes and backslashes. Continued commands whose text mentions none of these run as before.

- Autonomous code tasks now start their branch from the commit the remote's default branch (`origin/HEAD`) points at, instead of whatever the Genesis checkout has checked out, and the branch no longer tracks the default branch. Their delivery scope check compares against that recorded commit instead of local `main`, which could lag the remote, and their draft PR targets that default branch by name instead of a hardcoded `main`. A repository without a usable `origin/HEAD` (absent, or naming a branch that no longer exists) keeps the previous behaviour.

- **Every malformed registry file is now a `RegistryError`.** PyYAML's built-in
  constructors raise `ValueError`, `AttributeError` and `KeyError` on some bad
  input (an impossible date, `!!timestamp nope`, `!!bool abc`), and those
  escaped the registry's error contract. The loader now converts any failure
  at its YAML boundary.
- **The memory-volatility decision is a ranked yes/no.** It now asks whether a
  memory is likely to stop being true within two months, and consumes
  `ordering`: its probability ranks memories for re-verification. Measured
  zero-shot against later evidence, this phrasing separates memories that went
  stale at AUC 0.66, better than the previous five-way choice (0.62 at best),
  and unlike a choice it needs no rule for turning an answer into a rank.

- **Guardian: no snapshot delete on a pool that has already recovered.** Pool
  relief now re-measures the pool immediately before deleting and stops if the
  shortfall has eased (an autoextend landing, space freed elsewhere). Before,
  it checked only that it was still the same pool. Delete-first rotation now
  tries the create once more after its measurements, so if the pressure has
  cleared it makes an ordinary create-then-delete rotation instead of deleting
  the rollback snapshot first.

- **The neural monitor maps every call site to a subsystem.**
  - 26 call sites that rendered only in an "Other" card are now grouped. Some
    join existing groups (memory, reflection, executor, learning);
    four new groups take the rest: Dream Cycle, Awareness, Conversation and
    Evaluation.
  - Every group stays in one connected block on the grid. The hand-placed
    map gives each of its groups exactly one cell per site, and the new
    groups flow into rows below it, each starting on a new row when it would
    otherwise wrap. Before, a site without a position stacked on the
    top-left cell.
  - Five ids that only survive as stale run-history rows are marked removed,
    so they stop reappearing as live tiles: four with no caller or a newer
    name, and one job that now runs as a direct session and records no
    call-site data.
  - A test now fails CI if any routing call site or live call-site metadata
    entry is left ungrouped, duplicated, or without a display name, or if a
    hand-placed group has the wrong number of cells or a split block. Metadata
    entries that are not call sites of their own (an alias or a work tag) are
    marked `tile: False` and kept off the grid.

- **`update.sh` no longer deletes the wrong file on exit.** Its exit trap named its
  temp copy through a path bash evaluates when the trap fires, so an exit from
  inside a sourced library function (an unbound variable is enough) deleted the
  library and leaked the copy. And a server started by its `nohup` fallback
  inherited the "running from the copy" flag, so a dashboard update launched from
  that server ran in place and its trap deleted `scripts/update.sh` itself. The
  path is now bound once at startup, and "we are the copy" is proven by the
  copy's own path.
- **`update.sh`'s Guardian lease renewer no longer holds the update lock after it
  exits.** The renewer's `sleep` inherited the lock and outlived the deploy, for up
  to 15 minutes after a clean exit and about an hour after a kill. It is now
  started without the lock, and it stops once the deploy that started it is gone —
  judged by the process's identity (pid and start time), so a killed deploy that
  lingers as an unreaped zombie, or a new process that reuses its pid, does not
  keep the Guardian paused. The pause, resume and renewer moved to
  `scripts/lib/guardian_pause.sh`; the deploy marker and the list of tracked files
  a deploy may find dirty moved to `scripts/lib/deploy_marker.sh`, shared with
  `restore.sh`.
- **A zombie no longer holds the deploy marker.** A deploy that was killed but not
  yet reaped left `~/.genesis/update_in_progress.pid` naming a zombie, and every
  liveness check read that as a deploy still running: new deploys refused, and the
  watchdog kept from restarting a down server. The marker's holder now counts only
  if it is running and not a zombie, in the deploy scripts and the watchdog's
  reader alike. The holder check compares no clocks, so a wall-clock step cannot
  make a live holder read as stale.
- **`restore.sh` refuses when it cannot write the deploy marker.** The write was
  unchecked, so an unwritable marker read as held: the restore stopped the server
  and rebuilt the database without the marker the watchdog defers on, which let
  the watchdog restart the server mid-rebuild. It now refuses before stopping
  anything, and says why.

- **Bearer-token checks compare bytes, and bearer tokens must be ASCII.** The MCP HTTP transport's auth guard and the dashboard's internal-API bearer check compared the presented token as text, so a token with non-ASCII characters (or header bytes that are not valid UTF-8) raised inside the comparison and the request failed with a 500. Every bearer check now compares bytes and refuses a wrong token with 401. `GENESIS_MCP_HTTP_TOKEN`, `GENESIS_DESK_TOKEN` and `--auth-token` values containing non-ASCII characters are now treated as unconfigured, with a warning: such a token has more than one byte spelling, which could let two different configured tokens match the same credential. Operator step: if either token contains non-ASCII characters, replace it with an ASCII one (generated tokens are always ASCII).

- **The disk guardian's reclaim paths are safe to enable.** Neither an ORANGE reclaim pass nor the daily groom deletes the code-intel indexes any more (only the guardian's RED last-resort pass does); a completely full filesystem under ~25 MB now reaches RED; a new pressure episode gets its reclaim levers back instead of inheriting the previous episode's cooldown; pages state what reclaim actually did (a start is reported as queued, a failed start or a refused sweep says so, and the cc-tmp sweep counts only units it actually removed) and label the writer list as this user's processes (block-device writes; on a tmpfs page, which those counters cannot see, the page says writers cannot be attributed). No delete crosses a mount: every recursive delete in the guardian's shell scripts (the `~/tmp` prune, the background-sandbox reap, the cc-tmp sweep and the sessions prune) goes through one helper that spares a directory which is, or holds, a mount — read from the mount table, so a bind mount or a same-device volume counts, including names with newlines or control bytes — and every one refuses outright, saying why, when the kernel mount table cannot be read; `disk_reclaim.py` refuses a cache directory that holds a mount, or when the mount table cannot be read. The observe-mode notice now says OOM capture still pages.

- **The safety guards now refuse text the shell builds while it runs.** A command that writes part of its text as an escape inside `$'…'` quoting (a letter, a newline, or a character given by its code) is treated like a command split across lines: when it names an operation a guard checks, the guard refuses it and says to write that text out plainly. The discard guard and the commit gate also refuse a git command whose subcommand is not written out (a variable, a substitution, a brace expansion or an escape stands in its place), as the push guard already did. A tab, a quote, a backslash or a `?` written as an escape counts as built text too, because a shell that reads the text again treats each as syntax; escapes that never mean anything to a shell (a bell, a terminal escape, a form feed) are left alone. Where a tab escape sits in a command that names a checked operation, write a literal tab or `$(printf '\t')` instead. Commands that name no checked operation, and variables in ordinary arguments, work as before.

- **The content-boundary key no longer affects knowledge caching, and is read safely.** The web knowledge processor removes the fetcher's own boundary wrapper (a complete opener and closer with matching ids) before the page text is hashed, so a changed or restored `~/.genesis/boundary_key` no longer makes unchanged pages re-distill; local documents are hashed exactly as they are. The key file is used only if it is a regular file no larger than a key and holding exactly 64 hex characters, read without blocking; anything else falls back to a per-process key with a warning. Web pages already in the knowledge base re-distill once on their next re-ingest, as they would after #2537's wrapper change in any case.

- **`worktree_lifecycle.py --recover` works again.** Two changes that each
  passed on their own met on `main`: `--recover` gained a `--dry-run` preview,
  and recovery moved into a locked helper that was never passed the dry-run flag.
  Every recovery, preview or real, then stopped with `NameError` before
  touching anything. The flag is now passed through, so an archived worktree can
  be previewed and restored again.

- **The commit-message hook reads the install's private-identifier fingerprints
  the way the pre-push review does.** Both read the same fingerprint file, but the
  hook used `grep -E` and the pre-push review uses Python's `re`, so they disagreed
  about the same pattern. A pattern using Python-only syntax (`\A`, `\d`, a
  lookbehind) matched nothing at commit time. A pattern `grep -E` rejected (an
  unbalanced parenthesis) made the whole file read as "no match", so one bad line
  turned off every fingerprint, and a file saved with CRLF line endings matched
  nothing. The hook now reads the file with the pre-push rules, in Python, and in the
  Genesis venv's interpreter, picked in the pre-push review's order (the main
  checkout's first, since a pattern can compile in one Python version and not another): each line
  trimmed, comments and blank lines skipped, and a pattern Python cannot compile
  matched as literal text, named in a warning by line number. It prints line numbers
  only, never a pattern (the pattern is the private value) or the file's path (which
  can name a user or a host). A line that is not UTF-8 is still checked, byte for
  byte, instead of stopping the whole check. With no usable Python (a broken install,
  since bootstrap builds the venv before it installs the hooks) the hook says loudly
  that the message was not checked, and does not block. An `errexit` or `pipefail`
  inherited through `SHELLOPTS` or `BASH_ENV` no longer turns a clean commit into a
  failed hook. The pre-push review's reader is hardened the same way: a pattern that
  breaks the compiler in any way (a huge repeat count, deep nesting) is matched as
  literal text, and a byte that is not UTF-8 no longer stops its scan, so one bad
  line cannot turn off every other fingerprint there either.

- The commit gate's review-round lookup now reads a pull request's review evidence in one GitHub GraphQL query instead of five REST calls, cutting its typical time from about 5 seconds to under 2. Commits on busy pull requests now rarely fall back to an approval prompt (or a refusal, in background sessions) because the lookup ran out of time.

- A clean Codex review now satisfies the merge gate's freshness check. Codex posts no review object when it finds nothing, only a comment naming the commit by a short id, so the gate refused to merge until someone waived the check. It now accepts that comment (or Codex's PR-summary "Completed" row) when the short id resolves to exactly one of the pull request's own commits, that commit is the current head, Codex left no review object there, no Codex comment on the pull request carries findings, the comment was not edited by anyone but Codex, and the pull request has never been force-pushed, retargeted or had its branch restored. When it refuses a clean signal, the block message says which one and why.

- **The commit gate's no-pull-request exemption now works under a global git remote setting.** Any `remote.<name>.*` key in the global git config (for example `remote.origin.prune`) makes git list that remote, with no URL, in every repository that has none. The gate read that empty URL as possibly GitHub, so a scratch repository with no remote still asked for approval on every commit. A remote whose listed URL is empty is now skipped, whatever made it empty (a stray key, an empty or whitespace-only `url`, a remote holding only gh's own `gh-resolved` key, or an `insteadOf` rewrite to nothing), since `gh` resolves repositories from the same listing and finds nothing to resolve either. A push-only URL is listed on its own line and still counts.

- **The merge gate now reads CI from the newest run of each workflow.** A PR
  whose CI failed (for example on a broken base branch) and then passed in a
  new run on the same head used to stay `ci: red`, because the check rollup
  keeps every run. For each workflow, the newest run on the head now decides as
  a whole: a newer passing run clears an older run's failures, and a newer run
  that failed, was cancelled or is still running decides the result in the same
  way. Results are never mixed across runs, so two failed runs cannot add up to
  a pass. A newer run that skips a job also clears that job's failure from an
  older run. A check whose run cannot be identified is always counted. This does
  not block re-running a failed job until it passes: GitHub reports only a
  re-run's latest attempt.

- **`update.sh` refuses a checkout it must not deploy, before it touches anything.**
  A linked worktree at any path (git decides, not the path name), a bare
  repository, a detached HEAD, or any branch other than `main` is refused before the
  lock, the rollback tag and the backup, and the message names the branch it found.
  An update run on another branch used to merge `main` into that branch and restart
  services on the result. `GENESIS_ALLOW_NON_DEPLOY_BRANCH=1` admits another named
  branch deliberately; it never admits a worktree, a detached HEAD, or `live`. The
  checks now live in `scripts/lib/deploy_checkout.sh`, which
  `scripts/deploy_code_only.sh` also uses (with no override), so the two deploy
  paths cannot disagree. `update.sh`'s dirty-tree check is the shared one too: a
  file renamed into an excused path now counts as a local change, and an unreadable
  working-tree status refuses instead of passing as clean.
- **`update.sh` deploys the commit it fetched.** It pins the fetched `main` head and
  merges that exact commit; a fetch by another session in the same checkout can no
  longer change what is deployed. A merge that reports success without bringing
  that commit in, or that landed on a branch other than the one the run started
  on, fails the update.
- **`update.sh` never resets a checkout someone else is using.** If another session
  or editor switches the branch, commits, or edits a tracked file while an update
  runs, the update refuses before it merges. During the pre-update backup nothing
  has stopped yet; after the stop, the rollback takes over. That rollback resets
  only the merge it made itself. A plain branch switch with nothing uncommitted is
  switched back without force, and the other branch keeps its commits. Any other
  move is left exactly as it is. Services are reinstalled and restarted only on the
  pre-update code; otherwise the rollback reports itself incomplete and says what
  is in the way. The old rollback checked the original branch out and hard-reset
  it, which could destroy that work, and could restart services on code nobody
  had validated.
- **`update.sh` no longer overwrites a local ignored file that the update starts
  tracking.** Before any service stops, it lists the files the incoming commits
  add or change and refuses, naming them, if any already exist locally as untracked or
  ignored files (a local settings or secrets file, for example). Move them aside and
  re-run. The same check runs again as the last step before the merge, so a file
  that appears while services are stopped is refused too, before anything merges.
- **Local edits to `AGENTS.md` and `config/procedure_triggers.yaml` are kept.**
  `update.sh` used to discard them before every merge, with no copy. It now saves
  them first, before any service stops, under `~/.genesis/premerge-backups/<run>/`
  (a patch plus a copy) and prints where. `update.sh`'s own rollback, which resets
  tracked files, first saves any edit not already backed up, including on a
  `--post-merge` run. It discards an edit only when the update changes
  that file or the edit is staged (otherwise git keeps it through the merge), and
  only once a backup of its current content exists. The daily disk-hygiene run prunes these backups
  after 45 days.

- **A deploy that left the server down now says so in its "success" record.** Every
  success entry `update.sh` writes now carries `genesis-server-not-restarted` when the
  server was not brought back, including the no-change path. On the no-change path, "brought back" now means the health endpoint
  answers within a bounded wait, not that the unit merely reads as active (a
  crash-looping server does),
  and the leftover update-failure marker is cleared only then. With the server down, a
  no-change run records nothing over a failed, rolled-back or other unresolved update,
  so the next run still recovers it. The status it reads comes from the install's own
  database rather than a fixed home path, an unreadable status is treated as a
  possible failure (recover, and run the full activation) rather than as "nothing
  happened", and a missing venv now prints a warning that the history entry could not
  be written, instead of skipping it silently.

- **Re-applying Genesis gstack patches no longer replaces the current `codex`
  skill with an outdated copy.** Genesis no longer ships a `codex` overlay;
  `scripts/apply_gstack_patches.sh` now leaves the upstream gstack `codex` skill
  as-is. An install that customises its `codex` skill maintains that
  customisation locally. The review-checklist overlay and the safety and
  description patches are unchanged.

- **The worktree guard refuses a removal whose worktree it cannot read.** Every direct `git worktree remove` is meant to be refused, but the guard only acted when it could read the worktree being removed. It now also refuses when `xargs` or `parallel` runs the removal (or a `git worktree` whose operation is not written out) without naming the worktree, when the command hands the removal to a shell or `source` through stdin, and when a command it cannot tokenize, or a carrier such as `eval`, names the removal with no worktree after it. These refusals name no target, and each gives the remedy for text that is not a command. Measured over one install's recorded commands that mention worktrees (29,556, 2026-09-29): the guard refuses exactly the 178 that the guard on current `main` refuses, none new, and nothing it refuses runs here. Known costs: prose inside a here-document that names the removal under `xargs` is refused, as is a shell fed a here-document whose body only mentions the removal (write such text to a file instead), and `git worktree` under `xargs` with `--help` or a replace-string option.

- **A code-only deploy now refuses a pyproject that moves the package root.** The editable install points at the source directory through a `.pth` that only a reinstall rewrites, and `scripts/deploy_code_only.sh`'s dependency gate never looked at it: a change to `[tool.setuptools.packages.find].where` passed, and the server restarted on the old location. The gate now compares the directory the install's `.pth` names with the one the incoming `pyproject.toml` lays out, and sends a difference to `scripts/update.sh`. It reads the incoming layout from one shape only (the `setuptools.build_meta` backend, and `[tool.setuptools]` holding just `packages.find` with one `where` other than the checkout itself, which is what this repository uses); any other pyproject build configuration is refused as one it cannot check, so such a tree deploys through `update.sh`. A test fails if this repository's own `pyproject.toml` leaves that shape.

- Skill suggestions no longer fire on a single common word. "session" no longer suggests the closing-session skill and "api" no longer suggests an API-gateway skill; a skill is suggested when its full name or one of its declared keywords appears. A slash command's own name is no longer read as the task either. Replayed over 6,803 real prompts, suggestions dropped from 28.5% of prompts to 14.1%.

- The `outreach_queue` MCP tool no longer accepts a `status` argument. It never applied it, so a status-filtered call silently returned unfiltered results. Its description now states that it lists the 20 most recent sent messages and points to `outreach_pending` for queued ones. The `observation_write`/`observation_query`/`observation_resolve` and `procedure_recall` descriptions now state their real contracts: valid priorities, duplicate handling, the 50-row default query limit with no total, and the top-3 cap and usage counting.

- The `live` integration-branch guards close four gaps. The pre-push check for `Deploy-rebuild:` commits now reads commits as stored and treats any line after the subject that starts with the trailer as one, so a trailer key alias (`trailer.<key>.key`), `i18n.logOutputEncoding`, a `git replace` ref, or trailing comment, `Conflicts:` or scissors text can no longer hide one, and a push is refused when the check cannot read its commits. The Claude Code merge guard refuses a merge that follows, in the same command, any git or `gh` command that can change the current branch (everything except reads, `add`, `commit`, `fetch`, `pull` and `merge`), for example `git checkout live && git merge --ff-only feature`, which moved `live` unrefused (the same form reached `main`), and asks for the two to be run separately. The commit guard's repository checks now fit inside its time limit, so a slow `git` makes it refuse instead of being killed and letting the commit through. `~/.genesis/deploy_manifest.json` may now name its repository in a top-level `"repo"` (that checkout's absolute git common dir); the git hooks and both Claude Code guards then apply the `live` protections only in that repository. A manifest without the key behaves as before; one that cannot be read, or whose `"repo"` is not an existing git common dir, keeps the protections on everywhere.

- **A syntax error in `model_routing.local.yaml` now takes routing down until it is fixed.** A file that cannot be read or parsed as YAML holds no restriction the router can read, so loading the shipped config in its place would lift every one of them (disabled providers, `never_pays`, narrowed chains, lower limits). The load is refused instead: a router starting on that file is not created, the ERROR log and a high-priority health observation name the file, and a dashboard reload refuses it and keeps the running config. A router that is already running is unaffected until the next restart. Fix or remove the file before restarting. An overlay that parses but fails validation still falls back to the shipped config with its restrictions applied, as before. Separately, the degradation check now returns to essential-site coverage after a routing reload, even when an earlier reload had no essential call site; previously it stayed on the provider-count check until a restart.

- **`deploy_code_only.sh status --verify` now sees a change to the scripts the server itself uses.** The validation bracket treated only `src/`, `config/` and `pyproject.toml` as what the server depends on, but the server also imports two files under `scripts/` (the code-index marker helper and the worktree-claim lever) and runs four more afresh on each use (the disk-reclaim remediation, the Bash-allowlist guard with its library, and the hook launcher it runs that guard through). A pull that changed only one of them left the token valid. Now it voids the token, and an uncommitted edit to one reads as a runtime edit. Only the two imported files are reported as pending a restart and make `deploy` restart the server; a change to a script run afresh is live at once and asks for no restart. A test checks the lists against the ways the server's code refers to files under `scripts/`.

- **WebSearch override: says why it fell back when another plugin rewrote the
  result.** The override calls Genesis `web_search` through Claude Code's
  normal tool pipeline, so another plugin's PostToolUse hook that replaces
  large MCP results with a summary made most searches fall back to the
  built-in, logged only as "the reply was not JSON". The log now names the
  likely cause, and `.claude/docs/web-tools-guide.md` explains how to exempt
  `web_search` from such a hook (for token-optimizer:
  `TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS`).

- **Retry eligible Claude Code provider overloads (HTTP 529) below the existing approval gate.** Ego cycles, reconcile/realist checks and inbox evaluations retry the same invocation after 30s, 2m and 5m. Known-work overloads (more than one CLI turn) and MCP-backed overloads instead use the replay-unsafe boundary: no stale-resume recovery, full-tools roster failover or durable rate-limit park. The inbox persists a hold that survives scans and retry-cap increases without advancing its completed baseline; a per-item operator command requires explicit replay acknowledgement before release through normal approval/claim handling. Unknown or unusable turn counts remain retryable by accepted policy and can replay work; this is not an exactly-once guarantee. Eligible overload exhaustion does not spend inbox retry budget. Eligible overloads retain capacity-limit handling elsewhere (conversation failover, direct-session parking, Telegram notice and warning-level failure events). Classification retains structured result and stderr evidence even with trailing diagnostics. Detection also stops matching numbers that merely contain `429` or `529`, and treats "temporarily limiting requests (not your usage limit)" as transient rate pressure rather than quota exhaustion. Recovery procedure: `docs/reference/inbox-replay-safety.md`.

- The OpenAI-compatible chat-completions endpoint now returns HTTP 400 when a request does not end in a usable user message (empty, image-only or malformed content, or a trailing non-user entry), instead of falling back to an earlier user message from the history and running that instruction again.

- **Disk reclaim no longer deletes the code-intel indexes on its own.**
  `scripts/disk_reclaim.py` used to clear the code-intel index databases at
  95 % disk use whenever a caller did not say otherwise, and the automatic
  disk-cleanup remediation was one such caller. Deleting them forces a full
  re-index later, which is heavy. The script now never clears them unless a
  threshold is passed with `--last-resort-above`, and only the disk guardian's
  last-resort pass does that. Package and download caches are still cleared
  as before.

- **Test runs no longer keep every passing test's temporary directory.**
  The pytest configuration now sets `tmp_path_retention_policy = "failed"`,
  so a passing test's `tmp_path` is removed when the test finishes and only
  failing tests keep theirs for debugging. Runs that pass their own
  `--basetemp` previously kept every directory until removed by hand.

- **Each ego now sees its own history, not the other ego's.** The user ego and
  the Genesis ego share one cycle history. Two parts of each ego's context
  were read from that history without telling the egos apart. The "Your
  Previous Assessment" section came from whichever ego had run most recently,
  so an ego was often handed the other ego's focus summary as its own. The
  focus selector's "recent focuses (avoid repetition)" list mixed in the other
  ego's choices too. Both reads are now scoped to the ego they are for. Cycles
  recorded before the ego source was tracked are no longer offered to either
  ego. The dashboard's all-ego views are unchanged.

- **Eval runs keep their lock files in `~/.genesis/locks/`, not `~/tmp`.**
  The bench, gauntlet and skill-replay runners each refuse to start while
  another run of the same kind is going. Their lock files lived in `~/tmp`,
  scratch space that is pruned by age and can fill up, so a lock file pruned
  between runs could fail to be re-created and stop a run from starting. For
  one release a run also takes the old `~/tmp` lock, so a runner from before
  the update and one from after it still cannot run at the same time.

- **Genesis's review and research subagents keep their scratch files out of
  Claude Code's shared temp.** Each agent definition now tells the agent to
  create its files in one directory of its own under `~/tmp`, pass that
  directory explicitly, cap reproductions that create many or large files,
  and clean up. A subagent writing to the default temp location could fill
  the space every session shares and break them all.

- **An OOM-kill alert is no longer lost when it cannot be queued.** If the
  disk guardian could not write the alert (a full disk is the likely cause),
  the kill was recorded but no page was ever sent. The alert is now kept and
  retried on every poll until it is queued, and a later kill's alert names
  both. Separately, the guardian's "can I see other processes?" check no
  longer counts its own child processes, which matters if it ever runs in a
  private process namespace.

- **`scripts/update.sh` now chooses its update remote by one fixed rule and pins it.** It used to take the first remote, in name order, whose URL contained the repository name, so a remote added later with the same repository name (a fork fetched for review, for example) could be chosen ahead of `origin`. The shared rule (`genesis.util.update_remote`) uses `origin` when it names the public repository, or when no remote does, and compares repository names whole rather than by substring. It refuses when another remote names the public repository and `origin` does not, because a fork fetched for review carries the same name: an install whose `origin` is a private repository pins its public remote once, with `git config genesis.updateRemote <remote>`, and the refusal says so. `update.sh` pins an `origin` choice in the checkout's own git config on first use, so a remote added afterwards cannot change it, and prints how the remote was chosen. `GENESIS_UPDATE_REMOTE` overrides it for one run. The update-available check uses the same rule, read-only, and reports a refusal as a failed check rather than comparing against `origin`.

- **The worktree reaper no longer archives a worktree with uncommitted changes on the short 7-day clock.** A merge verdict describes commits, never uncommitted edits, and it can pass with nothing merged at all: a branch with no commits of its own has a `main` commit as its tip, and a branch stacked on a squash-merged PR head looks merged too. Any dirty worktree now waits the 14-day unmerged window and is reported as at-risk meanwhile; a clean merged worktree still drains at 7 days. A filename (with `core.quotePath=false`) or a worktree name that is not valid UTF-8 no longer ends the reaper run, and an archive of such a worktree can be recovered.

- **A failed update no longer throws away uncommitted edits made while it ran.** If another session or editor changed tracked files after `update.sh` merged, and a later step failed, the rollback's `git reset --hard` discarded those edits; only `AGENTS.md` and `config/procedure_triggers.yaml` were saved. The rollback now switches the branch back to the pre-update commit with a non-forced `git checkout --no-overwrite-ignore`, which leaves every edit to a file the update did not change in place, keeps staged changes staged, and refuses outright when a file the update changed has local changes or when an untracked or ignored file sits where it would write. It also refuses when the update changed a submodule, whose files git does not check. When it refuses, it reports the update as not rolled back, keeps the migrated database to match the code left in place, and does not restart services.

- **The worktree reaper no longer mistakes a recently changed worktree for an idle one.** Its activity clock missed several kinds of change below its shallow scan: a file or folder deleted or renamed deep in the tree, a path git quotes because it holds unusual bytes, an edit inside a submodule, and a commit, whose status is clean afterwards. It also archived a worktree changed while the scan was running. The clock now reads three signals, one for each kind of change: every directory's mtime (anything created, deleted or renamed, at any depth), the paths git reports as changed (edits), and when HEAD last moved, read from the timestamp inside the newest reflog entry other than the ones a worktree's creation writes, which `git gc` leaves intact. Right before archiving, the reaper re-checks the age and compares the worktree's commit, branch and status with what it read at classification, and holds the worktree if anything changed or cannot be read. A worktree it holds no longer stays listed for archiving on the board until the next run.

- Merge-gate clean Codex signals now bind repo-wide to the verified head, and the history veto covers head force-pushes, head-branch deletion or restoration, and base changes. Base changes stay vetoes because retargeting changes the effective diff without moving the reviewed head; a base force-push no longer voids a signal because the default base forbids it and a forced non-default base must pass through a vetoed base change to merge. Edit-history completeness is required only for Codex Bot comments, and untrusted edits permanently refuse clean signals with the editor named. Freshness reports use the gate's recorded pass reason and say when the head moves afterward.

- Update-history readers (MCP tool, dashboard status, backup tab) now order by true instant via datetime(started_at) and expose per-row deploy facts; a success row carrying genesis-server-not-restarted is surfaced as "server not restarted" instead of a plain live success.

- **Tailscale watchdog state is handled more reliably.** It preserves daemon continuity when identities are incomplete, keeps polling through unreadable systemd reads, counts malformed-only scans as blind, recognizes runtime-enabled timers and masked services, and verifies every watchdog unit before reporting uninstall success.

- **Knowledge ingestion now routes every YouTube video URL to the YouTube processor.** That covers `/live/` and `/embed/` URLs, and watch URLs whose `v=` is not the first query parameter (playlist and share links); all of these used to go to the generic web processor and lose their transcripts. A URL that only contains a YouTube-looking fragment, or a `watch?v=` with no video id, no longer reaches the YouTube processor. The registry now asks the matched processor whether it handles a URL before routing it there.

- **The stranded-branch checks no longer freeze once the repository passes 2000 pull requests.** Zero-drop reads the full PR history to tell merged branches from stranded ones, and `max_prs` (the `gh pr list --limit`) was 2000. A read that fills its limit is treated as truncated, and the branch checks are skipped for that sweep, so once the repository had more PRs than that, every sweep skipped them. `max_prs` is now 100000. gh pages until the PRs run out, so the higher ceiling reads the whole history in the same time; a read that still fills it still freezes rather than guessing.

- **Interactive Claude Code sessions no longer start the Discord MCP server.** `.mcp.json` copied every server from `config/mcp.json.template`, so each interactive session spawned `discord-bot` too, even though only the `community-responder` background profile uses it. The render now leaves out servers that only a dispatch profile uses. The template still lists them, so `community-responder` keeps its Discord tools. This also covers conversation-channel sessions, which run in the checkout and load the same `.mcp.json`; Discord tools are now available only to `community-responder`. (A dispatch on the `full` MCP profile was never affected: it runs outside the repo and never loaded `.mcp.json`. The docs now say so.) `install.sh` now renders `.mcp.json` with the same script `bootstrap.sh` runs. Re-running `install.sh` still leaves an existing `.mcp.json` alone; existing installs pick the change up at their next `bootstrap.sh` (which `update.sh` runs on every applied update; a code-only deploy does not re-render it), and in sessions started after that.

- Routing reloads keep active requests on their original model and isolate replacement-model health. Same-alias model changes preserve daily usage; provider renames preserve incident history; model replacements retain account and operator holds. Captured API-key health preserves essential coverage; proven retirement starts a fresh incident and retires obsolete observations, including delayed writes after alias replacement. Ordinary renames preserve incident ownership and acknowledgments through restart, partial recovery persists provenance, and resilience uses the captured generation. Captured requests retain their RPM policy while sharing pacing history; current health snapshots update cloud resilience in full and readonly runtimes without stale snapshots overwriting live state.

- **Fixed the merge-gate PR-body scanner reading documented examples as real declarations.** `readable_body` now follows CommonMark on the two rules it diverged from: a closing fence must be the same character and at least as long as the opener (a ```` block no longer ends at an inner ``` line), and lines split only on `\n`, `\r\n` and `\r` (a line joined by NEL or a Unicode line/paragraph separator no longer reads as two). The `e2e_declaration` fallback scanner carries the same fix so the two body readers stay in agreement.

- Leave disabled Codebase indexing requests pending without repeatedly claiming them, and support a shared GitNexus Node runtime selection for indexing and MCP readers (`GITNEXUS_NODE_BIN` or `~/.genesis/gitnexus-node`).

- **`ego_decision` and proposal-rejection capture no longer fold a new ruling into
  an existing decision that only shares its `[type/category]` tag.** Tags match
  literally, only a repeat of the same ruling reaffirms (whichever path recorded
  it first, and even when the 500-character cap cut a rejection's provenance
  note), and `record` returns the other same-tag rulings, most recently affirmed
  first, as `related` with `related_total`.

- Generated reflection reference datasets now remain unapproved drafts instead of assigning machine grades as human labels. Existing files are preserved, and calibration offers strict reference validation before judge calls. Empty generation can be retried, experiment tools retain their historical reference default, and strict validation rejects duplicate grading questions. Drafts publish atomically so interrupted writes do not block retry with a partial final file.

- NVIDIA health probes now use its models catalog instead of incorrectly reporting
a missing API key when the probe URL was absent. Catalog observations cannot
change NVIDIA breakers. Unsupported probe targets retain breaker-based display;
the health API distinguishes probe support, credential presence and breaker
authority. Model routing and qualification are unchanged.

- **`worktree_lifecycle.py --recover` restores a worktree's own `.trash_meta.json` in a subdirectory.**
  When the registration is gone and recovery recreates the worktree, it copies the archive's untracked files back and skips the reaper's own metadata. It skipped every file of that name at any depth, so a worktree's file of the same name in a subdirectory was not restored, and `--dry-run` did not count it. Only the file at the tree's root is the reaper's; the rest are now copied back and counted.

- Global process discovery now records inspection candidates without terminating CLI or browser processes, including when legacy reaper arm flags are present. Explicitly owned jobs keep their existing cancellation behavior.

- **Push-argument parsing now follows Git, in one shared scanner.** The push
  guard read several push spellings differently from Git: a value after a
  bundle-ending `-o` (`-uo -f`, `-uo +x`), a `git -c +k=v` global value,
  `origin -- -f`, a `--repo=fork` that is only an `-o` value, Git's unique
  long-option abbreviations (`--mirr` is a real `--mirror`), and which
  destination wins when a push names both `--repo` and a positional repository
  (Git uses the positional one). A forced push to origin spelled any of these
  ways now gets the force block instead of an approval prompt.

  Two spellings now get the lighter path, because Git treats them as plain
  pushes: `git push -- origin HEAD` and abbreviated safe flags such as
  `--set-up` can ride the re-push path. `git push --force --repo origin backups`
  now asks instead of blocking, because Git pushes it to `backups`.

- A force push away from origin is now blocked unless the whole command is one
  plain `git push`, with no prefix, wrapper, git global option, redirect, pipe
  or other step; its destination is read before the command runs, so those
  additions can change what the push targets. Non-force pushes carrying any
  prefix, wrapper or untrusted git global config option are blocked regardless
  of destination, because supplied config can retarget the push or add a forced
  refspec such as `remote.<name>.push=+…` or
  `remote.<name>.pushurl=…` that has no force flag. A force push directly to
  origin keeps the specific public-repository block.

  The structural force-push rule deliberately means retyping commands such as
  `git -C <dir> push -f backups`, `git push -f backups 2>&1`,
  `git push -f backups | cat`, `cd x && git push -f backups` and
  `timeout … git push -f backups` as a single plain push. For non-force pushes,
  `git -C <dir>`, `git -P` and `git --no-pager` remain safe; command prefixes
  and wrappers are blocked even for a disjoint remote.

- **Bootstrap stops when crash recovery cannot be safely completed.**
  Crash recovery no longer runs `git reset --hard`: it undoes only its own
  update's merge with a non-forced checkout. If ownership or a safe rollback
  cannot be established, bootstrap exits 1 and keeps the recovery state file.
  If a merge is in progress, bootstrap refuses without aborting it — run
  `git merge --abort` yourself, then rerun bootstrap.

- Review-request compounds now allow only inert neighbors (`cd`, `sleep`, read-only `gh pr view/checks`, and `git status`); other steps are refused. A close predicate may name a pull-request close in the refusal message, but never decides whether the compound is refused.

- **Refuse unsafe staged install-local path changes before an update stops services.**
  On divergent history, an update now refuses when an excused install-local path
  has a staged change the pre-merge clear cannot undo, including additions,
  deletions, intent-to-add entries, gitlinks, and any staged change on the
  de-tracked transitional paths. Staged edits to `AGENTS.md` and
  `config/procedure_triggers.yaml`, fast-forward updates, and local-ahead updates
  remain allowed. The guard runs again immediately before services stop.

- Re-push approval now uses Git's porcelain dry-run ref flag to distinguish first publishes from updates to the current branch. Recorded approvals retain an offline path; remapped refs and unsafe transports continue to require approval.

- **Code-only deploys save install-local edits before resetting ephemeral files.**
  They store edits under `~/.genesis/premerge-backups`, refresh backups after
  service stop hooks run, and refuse flagged edits that cannot be safely reset.
  A failed reset is not reported as completed and still raises the deploy-failure alert.

- **Merges through the GitHub API go through the merge gate.** A pull-request merge sent with `gh api` (REST `pulls/N/merge` or `merge-async`, or a GraphQL merge mutation) is now refused with a pointer to the gated `gh pr merge` command, so every merge-gate check runs. `gh pr merge --help` on its own is no longer refused.

- **Web fetches say when Scrapling is unavailable.** When Scrapling's fetcher cannot be imported (it needs browser-automation packages such as playwright and browserforge), web fetches fall back to plain HTTP without TLS impersonation. That fallback used to be silent; it now logs one warning per process naming the import error.

- **The browser reminders now reach the session.** Genesis's own tools return their result as JSON text, which the reminder hook read as empty, so the reminder after `browser_navigate` never fired. It now fires, on every browser layer, with a pointer to the browser-automation skill's safety gates for payments and credentials; on Camoufox it also points to the stealth-browser skill.

- The browser tools' shared log (`~/tmp/mcp_health.log`) is now rotated by the daily disk-hygiene job once it passes 10 MB, keeping up to two older copies (which the 7-day `~/tmp` cleanup also removes); it previously grew without limit.

- **`browser_sessions` and `browser_clear_domain` cover both browser profiles.** They now read and clear Camoufox's cookies as well as Chromium's, label each result by browser, and use the running browser when one is open. Clearing `x.com` no longer also clears `netflix.com`: domains match whole labels (`x.com` and its subdomains), and a bare public suffix such as `com` is refused. `browser_clear_domain` returns the number of cookies removed instead of yes or no. A profile another session's browser has open is listed from its file and is not cleared; its entry says why.

- **Driving your own Chrome (remote CDP) uses a tab of its own.** Genesis now opens one tab in your Chrome and works there, instead of taking over the first tab you had open. Within a session it reuses that tab (one per Chrome endpoint) when it reconnects, and it never closes it; if it cannot tell whether that tab is still open, the reconnect fails rather than open a second one. A remote session no longer switches on fast "collaborate" timing for the built-in browser that runs afterwards (remote clicks, fills and uploads keep their own fast timing). `browser_snapshot` on a remote tab now clears the "page state changed" advisory, as the advisory says it will.

- **Long form fields no longer time out.** `browser_fill` had a deadline based on the length of the text, which cut off values of a few hundred characters and reset the page. It now times out only if the browser stops responding for 30 seconds in one step.

- **Browser clicks land, or fail saying why.** On the default Camoufox browser, a click on an element below the visible part of the page used to do nothing while reporting success, and a click on an element hidden behind a cookie banner or dialog clicked the banner instead. Clicks now scroll the element into view and aim at a point that is really on the element (a link that wraps over two lines or a rotated element no longer counts as covered by its own container). If something covers the element, the click fails with an error naming the covering element, so it can be dismissed and the click retried, instead of a fallback pressing Enter behind it. (A covered element that is disabled or still moving times out rather than being reported as covered.) A styled checkbox or radio is clicked through its label. A click that may already have been delivered, including one cut off by the tool's timeout, is not followed by a fallback click, and its error says it may have taken effect rather than reporting a plain failure.

- **Each browser closes on its own schedule, and one browser's restart no longer drops another.** After switching from the built-in browser to your own Chrome (remote CDP), the built-in browser used to stay open for as long as you kept using Chrome, because any browser call kept every browser alive. Each browser now closes about an hour after its own last use; a TinyFish cloud session follows the same rule and keeps billing until then. Restarting the built-in browser after its window was closed no longer disconnects your Chrome or ends a TinyFish session, and closing the built-in browser's window now also stops its background driver process. A TinyFish session whose connection dropped is now ended before a new one is created, rather than left billing.

- **The Codex shell guard no longer tells an agent to stop on a denial it can fix itself.** A chained commit, or a heredoc the guard cannot parse, is now denied with "rewrite the command and run it again" instead of "STOP and handoff to the user". Review-budget and approval denials still stop. A deliberate denial also no longer adds a second "review guard unavailable" line, which made every denial read as a broken guard; that line now appears only when the guard script cannot be found.

- **Cancelling a knowledge upload can no longer delete a file outside the upload
  folder.** The cancel action deleted whatever path the upload's database row
  held, without checking that it was still inside the upload directory. It now
  removes only the upload's own file, addressed by name inside the upload's own
  directory and never through a symlink, and refuses (409) while leaving the
  file and the row alone if the row points anywhere else. The dashboard's Cancel
  button now shows that refusal instead of clearing the dialog as if the cancel
  had worked.

- Strictly proven data-bearing heredocs for `cat`, Git message, and `gh` body receivers no longer expose prose as shell commands; unproven heredocs remain visible to shell and secrets guards.

- The Backup tab's health banner now warns when configured extra backup directories were skipped, archived only in part, or did not reach the off-site copy. Those runs still count as successful. Before, an install without an off-site backend showed nothing, and one with an off-site backend reported the off-site copy as unfinished even though its core snapshot was complete. A gap in the core off-site copy, or a failed schedule check, still takes precedence.

- **The dream cycle now records which memory replaced one it retires.** Both consolidation paths (synthesis and duplicate merging) write the successor to the memory database, not only to the vector index, and rolling a run back clears it again. A synthesis that turns out to be an exact copy of one of its own source memories is now skipped instead of retiring that memory, and an explicit supersede on a dream-retired memory can no longer be silently redirected to the dream's successor. The dream cycle also leaves alone any memory the database already retired, and never retires a memory into one that is itself retired, and rolling a run back no longer undoes an explicit supersede made after the run.

- The merge gate reads CodeRabbit's new bold severity header (`**🟠 Major**`) as well as the old italic one. Since CodeRabbit switched formats on 2026-10-06, its Critical and Major findings had stopped blocking merges, and review-round counting had counted every Trivial comment as a finding.

- Fixed managed Codebase query startup to use the installed venv interpreter,
verify physical CPU/task ceilings, admit the full query budget against current
reclaim-aware ancestor usage at startup, and bound readiness around native
appearance without prematurely consuming its RPC window during preflight.

- Guarded uninstall preserves the original coordination lock namespace while removing other state inside the fixed deletion roots. Unsafe overlapping custom layouts refuse before retirement. Default host cleanup completes its guarded container transaction before removing Guardian artifacts. Backend retirement requires loaded/disabled or exact absence; masked and generated states refuse cleanup. Retention diagnostics account for final root symlinks and retained locks. Existing fallback GitNexus writers, diagnostic symlink loops, both Qdrant binary locations and dangling known unit links are handled explicitly.

- Fixed qualification reference admission and approval to validate the full
  current policy at each public boundary, and exclude configured bare candidate
  model IDs and routing aliases from frontier grading.

- Managed Codebase enablement checks typed backend commands and containment before
  activation and again after explicit native refreshes. Exact current rendered
  sources and override-free native namespaces govern activation and retirement;
  unsupported units are preserved for operator intervention. Installation uses
  the inspected manager directly, and final checks refresh cached native state.
  Supported retirement remains independent of settings and interpreter existence.
  Custom installs use their
  existing `VENV_PATH` selection. Removal validates the full fixed artifact set
  before unlinking and reports partial removal and reload failures.

- Qualification reference admission now requires declared human or approved frontier
feedback actors, rejects contradictory human machine identities, and includes
frontier feedback reviewers in human approval independence. Historical receipts
without actor metadata remain review obligations; existing corpus approvals are
not transferred to modified snapshots.

- The disk guardian no longer sends an EMERGENCY "cgroup OOM kill" page when capped work is killed at its own memory cap: jobs started with `python -m genesis.hostmetrics run`, the codebase-memory MCP wrapper and GitNexus index batches (which run in `app-capped.slice`), and opt-in indexing batches in `genesis-workload.slice`. Attribution is now counted from those slices' own cgroup counters, which say both where the killed process lived and whose limit fired, instead of matching journal text, which counted one notice per burst of kills and changes wording across systemd versions. A kill anywhere else, a kill inside those slices that a limit outside them caused, or anything that cannot be measured still pages. Each event in `~/.genesis/logs/oom_events.log` records the count per slice and the units the journal names. `OOM_CONTAINED_UNIT_PREFIXES` is replaced by `OOM_CONTAINED_SLICES`.

- The dashboard's Apply update, Resolve conflicts and Dismiss actions no longer delete or overwrite the in-progress marker of a deploy that started at the same moment, which could let the watchdog restart the server in the middle of that deploy. They now refuse while a deploy, an update or a validation hold is running, and say so.

- When a provider answers 429 with a free-tier daily quota (Google's `QuotaFailure` with a `PerDay` quota id and a `RetryInfo` delay), routing now deselects that provider until the reset time the provider gave, instead of calling the spent quota on every request for the rest of the day. The block is persisted in the daily-budget state file, so it survives a restart; it never trips the circuit breaker. A per-minute 429, or one without a parseable delay, is handled as before.

- **A resumed code task no longer runs against the main checkout when its worktree folder is an orphan.** Resume now checks that git still lists the folder as a live worktree of this repository, under its own name, instead of asking git inside it (which walks up to the main repository and says yes). Worktree checks also ignore `GIT_DIR`-style overrides in the environment (while keeping command-line git config such as `safe.directory`), and the trash refuses to list or restore from a trash folder that is no longer a private directory of yours, or that it cannot read.

- External Codex memory calls no longer process pending Claude session bookmarks; ordinary Genesis clients retain their existing behavior.

- On an install running the `live` integration branch, only regular files may now go under the hook directories (a directory or submodule in place of a hook, or a hook list naming one, is refused like a symbolic link), and dropping a candidate whose git hooks `live` installed is refused while the checkout is off `live`, naming the fix (switch to `live`, then drop), instead of leaving its hooks running.

- The proactive memory hook now stops itself 8.5 seconds after launch when a step stalls (a slow disk, a busy box), keeping what it already printed and saying in one line that recall may be missing, instead of being killed at Claude Code's 10-second limit with its whole output discarded. Its 8-second budget also counts from launch now. A stall inside a disk sync (fsync) can still outlast the limit.

- Fixed the container-swap reconciler clobbering an operator- or Incus-native
  `limits.memory.swap` byte-ceiling value back to `true` on every tick, which
  would have flipped swap off continuously once such a value was set.

- Codex budget checks require an absolute `git -C <directory>` for mutating commits and an explicit GitHub PR number/repository or a standalone GitHub PR URL for review requests. Context-dependent targets receive a rewrite instruction before budget lookup.

- On an install running the `live` integration branch, two candidates can no longer combine into a git hook list that names a directory (one lists a name, the other adds a directory there): the rebuild excludes them by name instead of installing nothing for that hook and refusing every later rebuild.

- **Fixed: SQLite temp files from genesis-server, the bridge and the watchdog no longer land on the Claude Code temp volume.** These units keep `TMPDIR` on `~/.genesis/cc-tmp` on purpose, because the Claude Code sessions they launch need it. SQLite inherited it too, so a large sort or index build spilled a 100-200 MB `etilqs_*` file onto that small volume, and the disk guardian paged it as a runaway file. The units now also set `SQLITE_TMPDIR=~/tmp`, which SQLite reads before `TMPDIR`. Only SQLite's spills move; `TMPDIR` is unchanged. A missing or unwritable `~/tmp` falls back to `TMPDIR`, as before the change, and the unit logs a warning saying so instead of refusing to start. Existing installs pick it up when the units are re-rendered (`scripts/update.sh`) and the services restart.

- Fixed the guardian swap reconciler treating `limits.memory.swap: 1` as a
  one-byte swap ceiling. `1` is an Incus boolean (true), so a live
  `memory.swap.max` of 0 under it is ordinary swap-off and is now healed
  instead of reported and left off.

- Memory correction retries now reject unrelated missing or ambiguous successor handles while preserving exact-ID repair after a successor disappears.

- On an install running the `live` integration branch, deploy health no longer reads a candidate's own files as undeployed merges or guardian drift, counts commits behind against origin/main, and raises non-paging findings when `live` cannot be read, when the checkout is off `live` while the deploy manifest lists candidates, when the manifest lists candidates `live` does not hold (or `live` holds ones it no longer lists), or when a candidate brings update.sh-only files. Every manifest and `live` fact comes from one new read-only engine call, `scripts/deploy_candidates list --json`. The Guardian watchdog's host checks (gateway staleness, SSH key hardening, Claude Code auth, linger) now run on any branch instead of only on `main`.

- Memory recall no longer turns a long pasted prompt into a full-text query over every word in it. The expanded query, the file-keyword lane's base and the AND→OR retry now keep at most 32 search tokens, counted over every word including repeats (a snake_case identifier counts once per piece), filled with the most frequent words in the prompt; prompts within that budget are unchanged. Measured on a copy of a 113,000-row memory index over 40 long and 20 short real prompts: the slowest 5% of long-prompt searches fell from 26 s to 0.75 s and the largest temp sort from 155 MiB to 9 MiB; short prompts returned identical results. On long prompts the top results change (median 29% overlap with the unbounded ranking). Query expansion also skips the `session_note` tag, which sits on most memories and widened every expanded query.

- Backup passphrases preserve Unicode whitespace bytes consistently across shell loading, Guardian integrity checks and escrow. Encryption and decryption reject line-feed passphrases before invoking GPG.

- Fixed the daily `git fsck` monitor paging on noise (#2745). Its alert now
  shows the lines that actually failed instead of thousands of harmless
  `dangling` objects, and a failure is re-checked two minutes later before it
  pages. A failure that passes the re-check, or whose re-check reports only
  objects that turn out to exist (a race with another git writer), is recorded
  as a `high` observation (morning report, dashboard), not paged. The scan also no longer
  holds up the awareness tick while it runs.

- **Fixed: an update no longer silently overwrites a systemd unit you edited by hand.** `scripts/update.sh` used to re-render every unit under `~/.config/systemd/user` from its template, discarding any change made in the unit file itself, with nothing in the output saying so. Each rendered unit now ends with a `# genesis-managed` stamp, and an update that finds an edited unit refuses before stopping anything (exit 4), naming the unit and the two fixes: move the change into a drop-in (`<unit>.d/override.conf`, which updates never touch), or run `scripts/update.sh --take-template <unit>` to install the template, with the old file saved under `~/.genesis/deploy-backups/`. The VNC units `scripts/setup-vnc.sh` writes are covered the same way. Timers listed in `~/.genesis/config/disabled_timers` stay off across updates (`systemctl mask` cannot work on these units). Every update now ends with a list of what it changed outside the checkout: units written or kept, timers skipped, Claude Code pin realignments and removed shadow copies. `update.sh --post-merge`, which finishes a merge that already happened, never refuses: it keeps an edited unit and names it. `--take-templates` never replaces a masked unit; only `--take-template <unit>` does.

- Fixed backup-integrity fault injection to cover both GPG decryption option spellings and refreshed the measured atomic-write census after adding authenticated backup publication.

- Refuse first Guardian-enabled loopback activation without a maintained pre-existing pause, including the legacy updater bootstrap and interrupt restart boundaries. Document staging merged updater code through the locked code-only pull before the first full update.

- Requalify ingress rollout controls with qualified and refused pre-stop restart harnesses and the measured atomic-write denominator, without changing the production prerequisite guard.

- The rework merge gate now accepts a `## Rework spec` or `## Rework acknowledgement` heading followed by a qualifier set off by punctuation, such as `## Rework spec (amended, decision-complete)`, and the US spelling "acknowledgment". It accepted only the exact heading, which blocked rebuilds whose spec was posted with a qualifier. A heading continued by a bare word ("Rework acknowledgement needed") still does not count.

- Host uninstall supports an explicit container coordination namespace with `--container-genesis-home`, preserving contention with external-runtime index writers across login initialization.

- Awareness posture now recognizes every Incus false spelling and zero-byte swap ceiling while treating unset values as healthy. Guardian INFO alerts distinguish swap repairs from configured swap-ceiling changes and name `swap_ceiling_pct`.

- Web fetch now reports the client that actually ran, direct-session profile descriptions match their configured permissions, and the architecture index points to tracked decision records.

- Bound SMB client shutdown after the operation timeout.
- Git repair now reports fsck diagnostics from both streams and ignores reflog-only noise.
- Managed status seeds systemd's user-session environment; Claude Code 2.1.280 subcommands are classified by lifetime.

- `host-setup.sh` now checks that a proxy device is configured to forward the dashboard address to this container before printing it. When none does, it says what it found, prints the command that fixes it, and offers only routes that reach the container directly, instead of advertising a host URL that does not answer.

- **A hung test no longer holds up every other test run unnoticed.** A run refused by the box-wide test lock (and `scripts/pytest_lock_wait.py`) now names the test the holder is running and for how long, and flags it as LIKELY HUNG after 10 minutes. The per-test timeout drops from 1 hour to 10 minutes, and a run without `pytest-timeout` installed says so at the start instead of at the end.

- **Updates no longer delete the end of your `~/.claude/CLAUDE.md` when a managed block's end marker is missing.** The block writers (network identity, container specs) now leave the file untouched and say which marker is unbalanced and on which line; before, everything from the unmatched begin marker to the end of the file, Personal Notes included, was removed.

- **Updates no longer trust every newly installed skill automatically.** The skill-security allowlist (`~/.genesis/config/skill_scan_trusted.txt`) is seeded the first time bootstrap runs and then left alone; a skill added later stays untrusted, so the scheduled scan reports it, until you review it and add its name to that file. Before, every update re-seeded the list from whatever was installed.

- A morning report, or a queued alert with its own identity, is no longer dropped as a duplicate just because its first 200 characters match an earlier message, and deliveries retried after a send failure are no longer dropped as duplicates of each other. A repeated identical backup failure that had to be queued for retry is still delivered once a day.

- Updates no longer delete MCP servers you added to `.mcp.json`. Bootstrap now replaces only the servers Genesis ships in `config/mcp.json.template`, keeps every other server and setting, saves the previous file under `~/.genesis/deploy-backups/` whenever it changes, and names what it updated, removed or kept. It also stops dropping and re-adding the Serena entry on every run (#2413).

- One malformed file in the alert queue no longer stops queued alerts from being delivered or causes new alerts to be dropped; it is set aside as `.corrupt` with a warning naming the bad field, and deleted once it is more than 14 days old.

- Updates no longer silently overwrite the system config files Genesis installs under `/etc` (the journald size cap, the sshd dead-client setting, the memory and network resilience drop-ins and watchdog units). Each now ends with a `# genesis-managed` line; a file you edited by hand is kept and named in the update summary, and `update.sh --take-template <full path>` installs the Genesis version after backing yours up. Files from earlier installs are marked once, without restarting anything.

- A runaway file that was emptied while still held open, and then filled up again, now pages again (marked "again") instead of staying silent; a writer that closes its file for a moment is still judged against its earlier size.

- Guardian's code-revert recovery now stops when its `git stash` fails or does not finish, and says so, instead of reverting anyway and reporting a misleading revert failure.

- The deploy advisory for `update.sh` now appears only when a command can actually run the script, instead of on every command that mentions it (reading, grepping or editing the file no longer pulls in the full note).

- `SECRETS_PATH` is now honoured by the loaders of `secrets.env` that read a fixed path regardless: the Claude Code hooks (session context, urgent alerts, proactive memory), `gmodel`, the guardian credential bridge, and the reflection and LongMemEval eval harnesses. With `SECRETS_PATH` unset the hooks and `gmodel` read their own checkout's file as before, the bridge reads the checkout's file (unchanged for a checkout at `~/genesis`), and the two eval harnesses try the checkout's own `secrets.env` before the `~/genesis` one. Two readers stay home- or checkout-relative on purpose: the guardian credential-integrity check and the disk-hygiene store knobs.

- Qualification retains empty or whitespace-only processing and cleanup failures by exception type, so restarting cannot silently resume a failed attempt or resend its completion.

- Preserve existing masked Codebase query units during installation and updates, so the remaining unit reload and timer setup can complete.

- Qualification retains failures even when exception formatting raises, and preserves required evidence fields when a credential value matches a field name, allowing verified answers to recover without another completion.
- Live qualification refuses LiteLLM's optional unredacted payload printer before accessing credentials or sending requests; retained answers still replay offline.

- **The MCP health log can be moved off the shared home directory.** Importing
  the health MCP's browser module opens `~/tmp/mcp_health.log` and routes every
  `genesis.*` log line of that process into it, so a test run that merely
  imported the module wrote into the log the live MCP servers share.
  `GENESIS_MCP_LOG_DIR` now names that directory (and the Turnstile debug log's);
  unset keeps `~/tmp`, and a directory that does not exist attaches no log file.

- An update that merged code it never activated, or that stopped on merge conflicts, is no longer forgotten: the record it leaves (`~/.genesis/update_state.json`) used to be deleted by simply opening the dashboard's Updates tab, by starting a new update, or by an early exit of another run, after which `scripts/deploy_code_only.sh` could restart the server onto code bootstrap and migrations never ran on. One rule now decides whether that record still guards anything, and `update.sh`, `deploy_code_only.sh`, the `live` engine, bootstrap's crash recovery and the dashboard all use it. A plain `scripts/update.sh` refuses with exit 5 and names the remedy (finish it with `scripts/update.sh --post-merge`, resolve the conflicts, or Dismiss it on the dashboard's update card); the dashboard's Apply refuses the same way, and the update card offers Dismiss even for a record it cannot read. A leftover that guards nothing (a run that stopped before merging anything) no longer blocks a code-only deploy.

- Managed (prepared-receipt) background sessions handle abnormal endings more precisely. A launch that fails inside process creation (missing or non-executable program, bad working directory) is now an ordinary failure that frees its runner slot, instead of holding the slot until restart as if a child might still run. Confirming that a killed process group is gone is bounded; past the bound the receipt is recorded once as cleanup-unconfirmed, with a log line, and keeps its slot. The slot is held until the receipt's result is persisted, not only until the process exits. A later cancel no longer erases the cleanup-unconfirmed record, and a receipt finished during restart recovery keeps its cost and token totals. These sessions have no caller yet; this prepares them for the peer conversation API.

- Qualification transport now normalizes recovery metadata before redaction/GET,
  contains diagnostic rendering failures, admits only conservatively supported key
  and budget headroom, and constructs the HTTP client before durable dispatch.
  Existing receipt and no-resend semantics remain in place.

- Qualification now retains explicit invalid evidence for excessive or malformed
  response and billing JSON, redacts unsafe escaped diagnostics, and avoids
  repeated matching of malformed quoted text. Recovery remains GET-only and
  never resends a dispatched completion.

- **Refuse unsupported pinned Codebase paths before staging.** Linux repository
  and cache identities containing a backslash, including symlink targets, now
  refuse before native configuration can rewrite the path and select another
  cache. Status retains metadata and independently checks systemd, including
  when a provider path contains a symlink loop. Literal binary/interpreter paths
  and structural reads of old settings retain their existing behavior.

- **Managed agent turns can select a configured fallback before starting work.** Tool-disabled readiness checks detect provider quota refusals without replaying a task that already ran. Existing deadlines, cancellation and approvals remain in force. The neural monitor adds **Fallback**, and dashboard indicators distinguish last successful fallback from the models used by concurrent tasks.

- Treat an intentionally disabled peer API as disabled instead of reporting a deployment regression; enabled API readiness checks now include its conversation dependencies.

- Keep deployment status fields aligned with the values used to calculate their verification token when runtime state changes during a status read.

- Retain qualification source bytes outside temporary storage and require verified primary and restored evidence before complete admission. Checkpoint copied evidence when later source reads or validation fail.

- A test that hangs now ends the run after its 600-second per-test limit with a stack dump of every thread, instead of freezing the CI job until the job time limit cancels it.

- The telemetry secret scrubber also redacts HTTP credential headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`) and bearer credentials wherever they appear.

- Refuse unsupported repository and cache identities before managed runtime effects or readiness retries, while preserving diagnostic and retirement reads.

- Gate complete Discord bot replies before chunking or delivery, refusing sensitive content and failed safety checks without echoing values. Send and observe the shared egress gate's cleaned text.

- When bootstrap recovers from an interrupted update, the recovery is now recorded in the update history as `crashed_recovered` (previously the database rejected that status and the record was silently lost), and the dashboard flags it. If the record still cannot be written, bootstrap prints the error instead of hiding it.

- The deep reflection's intelligence digest now counts the knowledge items ingested since the last deep cycle, grouped by pipeline, instead of always reporting that none were ingested; if the count cannot be read, the digest says it is unavailable rather than zero.

- Commands after a here-doc whose body breaks the command parse (an unmatched backtick or an apostrophe in the body) are no longer invisible to the safety guards: the parse reports the command as unreadable, and a guard refuses it when it names the operation that guard checks, with a hint to write the body with an editor tool and run the rest as its own step. The check fails closed: it also reports a here-doc whose end it cannot prove (no terminator line, or one that only ends it inside a substitution), whose delimiter spelling it cannot resolve, or whose unquoted body holds a command substitution or a `${...}` expansion the shell would run, so an occasional here-doc that hides nothing is reported too.

- An install where codebase-memory-mcp is registered but its managed setup was never done now says so: a health alert names what is missing (the settings file or the enabled query unit) with the configure/enable remedy and the opt-out file, and bootstrap, install and update print one matching line instead of an unconditional pointer. Creating the opt-out file silences both.

- Vault audit writes now keep independent transaction ownership through cancellation and rollback. Failed rollback retires the audit connection, preventing later requests from committing abandoned rows; unlock remains fail-closed. Shutdown drops the key first and bounds its audit-owner drain without cancelling cleanup. Already-queued commits cannot be undone, and SQLite worker shutdown has no hard deadline.

- Scoped job cancellation waits for owned descendants after leader exit, keeps the first caller cancellation cooperative, and forces on the second. Watchdog stops retain their separate ten-second force deadline after leader exit. Launch without a reachable systemd user manager is refused.

- Transcript analysis keeps uncertain hook names unknown while retaining scrubbed commands, rejects negative durations and counts, preserves surrounding records after deeply nested malformed JSON, retains scrubbed model-refusal diagnostics, and evaluates chronology against the captured input rather than later appends.

- **Peer conversation admission avoids a circular database wait with result persistence.**
  Database identity is still checked against the actual private connection before
  admission, but before acquiring its write lock. Persistence warnings identify
  exception classes and SQLite error names without logging private error values;
  regression tests use production database settings and bound their send wait.

- Hook advisories and guard explanations that contain non-ASCII text (em dashes, accented letters, CJK, emoji) are now shortened to the longest part that fits the hook output budget instead of being dropped entirely; a hook whose decision fits the budget with its explanation emptied no longer reports itself as over budget; and hook output containing an unpaired surrogate character is written with that character escaped instead of crashing the hook.

- An update now installs the `test` and `payments` packages (pytest-timeout, hypothesis, the card vault's pyrage and zxcvbn) after the core packages, as a separate step that never fails the update. Before, no install path installed them: local test runs could not import some test files and ran without a per-test timeout, and `genesis vault` refused with "install the payments extra". A code-only deploy now warns, without refusing, when they are missing. The first update after this change installs them; on an install already at the latest commit, run `scripts/update.sh --post-merge`.

- The `youtube-fetch` skill's fallback (yt-dlp) transcript step no longer breaks when the skill is loaded with arguments: its de-duplication kept only the first line, or failed with an awk syntax error.

- **A `live` rebuild refuses to move a checkout someone changed while it was planning.** The rebuild now records where the checkout was (its branch, commit and a clean tracked tree) before it fetches, and checks it again once it holds the checkout lock and just before it writes anything. A commit, branch switch or tracked edit made in that window makes the rebuild refuse with nothing moved, and `update.sh` rolls back as for any other refusal, instead of resetting `live` over the commit or moving the checkout off the other branch. `update.sh` passes the branch and commit it verified (`rebuild --expect-branch --expect-head`). A commit or branch switch that lands in the last moment before the switch is still possible; the rebuild then prints a warning naming it, and it stays in the reflog. A tracked edit made in that same last moment is not warned about. Rebuilding `live` is still disabled on every install until the remaining safeguards land.

- Review requests now refuse a PR URL combined with `--repo`/`-R`, even when both name the same repository, and refuse unsupported explicit hosts before checking review capacity. Use a standalone github.com PR URL or a literal PR number with a repository flag; Codex retains its explicit-host requirement.

- **A rollback on the `live` integration branch no longer reports itself complete when its git hooks or the server did not come back.** `deploy_candidates restore-hooks`, which a `live` rollback runs to put back the old tip's git hooks, now exits 1 when a hook could not be written, the hook list could not be read, `sync-hooks.sh` is missing, fails or times out, or any installed hook still differs from the checkout's copy afterwards (a hand-edited hook included), so the rollback is recorded as incomplete instead of rolled back. When a `live` update is refused for a hand-edited systemd unit (exit 4), the rollback now waits for the restarted server to answer its health endpoint before it records anything: an unhealthy server makes the update history, `last_update_failure.json` and the exit code (1) all say the rollback did not complete. That wait (up to 180 seconds) applies only to that path; rollbacks on `main` are unchanged. A rebuild or drop that cannot sync the hooks after it has moved still only warns.

- **The open review round is found by the head, not by list position.** A PR's
  rounds are listed in the order of its commit list, which GitHub sorts by date,
  so after a rebase or cherry-pick a commit can be listed after the head. The
  budget then read the head's open round as complete, and its findings as owed
  by nobody. `review_budget`, `review_reflection` and the `--check-pr` rounds row
  now find the open round as the one whose head is the current head; a budget
  that says a round is open but carries no single entry for the head owes an
  unknown amount, never nothing. Every open PR owes the same as before; the
  `status` output's `round_head` is now empty when no round is open, where it
  used to name the newest round by list position. The round trend still reads
  list order and is fixed separately.

- **A peer send that fails with 503 now names the receipt ID it computed.** The route still answers `503 not_ready` when `POST /v1/agent/a2a/message:send` outlives its 15-second bound, or fails after its receipt committed, but the body now carries that receipt ID as a top-level `taskId`. The admission is shielded and keeps running after the timeout, so a client that retried with a new message ID could start the same work twice. The send may still be refused after the 503, so the agent conversations reference now says to poll `GET /tasks/<taskId>` (404 until the admission commits, and for good if it was refused) or retry with the same `messageId`, which returns the original receipt or the refusal and, while the original admission is still in flight, waits on that same work. Other 503 bodies are unchanged.

- **The morning report states current facts instead of stale or mixed ones.** System Health now shows the live PID/task budget of the binding cgroup level (percent, tasks, and which level), read at report time, so an earlier PID alert can be weighed against the current figure; when the reading is not available it says "unavailable" rather than showing an old number. An open PID-budget alert, including one raised before a restart, is resolved automatically once the budget is back below 75%, and a new rise past the alert line alerts again after at most a quarter of the usual hour-long cooldown. Pending deferred work is shown as two figures, the recovery backlog and the scheduled worklist (a deliberate drain, not a backlog), instead of one mixed total, with any stalled worklist items counted directly; every System Health count (queues, surplus queue depth, awareness ticks, Claude Code session counts) that could not be read now says "unavailable" rather than 0 or "None". Genesis's own reflection and self-audit records (gap clustering, wing/memory/procedure audits, self-unblock and prompt reviews, and quarantined reflections) no longer appear in the report's observation sections, where they read as live faults; they are still counted in the "Genesis internal" line, and the dashboard observations panel still shows them. Reflection contradictions stay in the report, because they are written for human judgment.

- The morning-report prompt instructs the model to keep recorded figures and attributed causes separate from current conditions. It preserves supplied measurement times/windows, leaves unknown freshness unknown, and requires current evidence for infrastructure/provider/security alarms while retaining active-goal and outreach blockers. Observation creation-age demotion is unchanged.

- Task-completion rubric 1.1.0 instructs judges to keep missing or contradicted required deliverables below the existing pass threshold, separately from writing quality; qualification requires fresh version-bound references.

- Ordinary Claude streaming now classifies the final effective quota state rather than the presence of a quota event, preserving answers and ordinary empty-output detection for informational events and usable overage.

- Record agent task status transitions and owner-approval changes consistently, and keep task-list pagination stable when an anchor task changes or is deleted.

- Strengthen the queued-index replay test to require complete outcome retirement
  without repeating the fake provider, and bound its SQLite locker readiness wait.

- Managed Codebase status reports conditional queue initialization-lock prerequisites without opening the queue or rejecting a usable current-schema queue.


### Security

- **The guards no longer lose sight of a git command that begins with certain
  global options.** To work out what a git command does, the guards first walk
  past any global options in front of the subcommand. An option that takes a
  separate value has to be stepped over together with that value; an option the
  walk does not recognise is stepped over alone. Three options that really do
  take a value were missing from that list, so the value was taken for the
  subcommand and the real operation behind it was never seen. No unusual
  quoting was involved — an ordinary, fully written-out command was enough, and
  both a publish and a worktree removal could pass their guards this way.

  All three are now recognised, in the four places the list is kept. Anyone
  updating gets the fix by pulling; there is nothing to enable.

  The list is no longer maintained by hand. A new test asks the installed git
  itself which global options consume a value — sweeping the binary for
  candidates and probing each — and fails if any of them is one the walk would
  step over alone. It checks in that direction only, so a list carrying options
  your git does not have is still fine: git rejects those commands outright.
  A future git release that adds such an option now turns the build red instead
  of quietly reopening the gap.

  That test is also what found the third option. An earlier version of this fix
  covered two and asserted, in a code comment, that the full set could not be
  determined automatically. That was wrong, and it was the reason the third was
  missed — the claim is what justified stopping. Deriving the set takes about
  twenty seconds.

- **An unlisted git global option no longer hides the subcommand from the push
  guard.** Verb discovery walks a git command's leading options to find the
  subcommand, and stepped over any option it did not recognise as consuming a
  value — assuming an unknown option to be harmless. When such an option does
  consume one, that VALUE lands in the verb slot: the walk reports it as the
  subcommand, the real operation is never seen, and gates keyed on that
  subcommand stand down. Three real options behaved exactly this way and were
  listed one at a time as each was found.

  The failure direction is now inverted, on the signal that means "the verb
  cannot be trusted". An option in none of the three classification sets marks
  the verb UNESTABLISHED, which routes to the unresolved-verb blind spot the
  push guard already refuses. Previously the walk answered confidently and
  wrongly; it now declines to answer. A future git release adding a
  value-consuming global becomes a refused command rather than an open door.

  The three sets were MEASURED against the installed binary with an oracle
  rather than read from help output, which omits several of these options
  entirely. Of 750 candidates from the binary's own strings table: 8 consume a
  value, 12 are valueless, 4 run no subcommand, 726 are rejected outright.

  **What did NOT change is the accessor that answers "which word is the verb".**
  An earlier revision made that return "unknown" too, which is the honest
  answer and was wrong to ship: 24 call sites across five guards read its
  existing sentinel as "this segment is not the operation I gate", so widening
  it widened what each guard ignores. Measured on that revision, a worktree
  removal behind `--bare` went from refused to allowed, and a `--no-verify`
  behind any unclassified global became invisible to the commit rule — a
  fail-open introduced by a change meant to close one. Teaching each guard to
  consult the blind spot is per-guard work on five enforcement hooks and is
  tracked separately; this change does not attempt it, and the residual it
  leaves is exactly what was there before.

  Measured over 50,845 unique real commands (18,936 containing git/gh segments,
  35,311 segments) by diffing every parser answer a guard can branch on:
  `git_subcommand`, `gh_pr_subcommand`, `commit_skips_hooks` and the subcommand
  index are **identical on both trees, 0 flips**. Only the trust signal moves,
  on 2 commands: an unknown-option probe that git itself rejects, now refused
  instead of parsed, and `git --help <topic>`, which stops being flagged
  because a help query runs no subcommand.

- **A deliberate relaxation, and the check that keeps it honest.** A gated verb
  written after one of the options that run no subcommand — `--exec-path` and
  its siblings — was refused and is now allowed, because git never
  reaches the verb. That is a false block removed, but it makes several allow
  decisions depend on a classification — so both new sets are re-derived from
  the installed binary on every CI run, and the checks fail if an exempt option
  ever starts running the subcommand, or a supposedly valueless one turns out
  to consume a value. A fix for a hand-maintained list that fails open should
  not introduce a second one.

  The oracle for that derivation must not resolve through the repository.
  Reading the marker from the probe repo's own config makes the lookup miss for
  any global that changes repository or config resolution, which scores it as
  "git rejects this" while its subcommand runs perfectly well — how `--bare`
  came to be absent from every set while the sweep reported a tidy total. The
  marker now lives in a file named by absolute path. A completeness check
  seeded from git's own usage line covers the other direction, since a sweep
  blind to a class reports the same tidy total either way.

- **Three spellings of the same `gh` option now answer alike.** A glued short
  value (`-R<value>`) was tested as a whole token, so a repository passed that
  way was read as an unreadable option NAME and flagged, while the separated
  and `=`-attached spellings of the identical command were not. Glued values
  are recognised for the shorts a dispatcher declares — measured per program,
  because `gh` accepts `-Rowner/repo` and `git` rejects `-C/path` outright.

- A refusal names a remedy that applies. Two causes now reach one predicate,
  and telling a session to write its subcommand out literally when it already
  is names a rewrite it cannot perform — on a hard block, the message is the
  only way out.

- **A hook guard whose own code fails to load now refuses the command instead of
  disappearing.** Claude Code treats any exit code other than 2 as a non-blocking
  error, so a guard that raised while it was still importing did not degrade — it
  vanished, while the session went on believing it was protected. Six guards are
  covered: protected-path deletion, discard, push/merge, the review gate, the
  worktree guard and the full-suite guard. Each now falls back to a deliberately
  crude read of the raw command text and refuses anything that names a gated
  operation — over-broad by design and loud about it, rather than standing silently
  aside. A payload that names no command at all is refused on the same reasoning,
  and no in-band waiver is honoured on that path, because binding a waiver to the
  command it waives needs the very parser that is missing.

  One guard is deliberately **not** covered: the background-pipe guard, whose only
  usable signal is the pipe character — present in 70% of ordinary commands, so
  refusing on it would make a broken install unrepairable rather than safe. What it
  protects is also the mildest here: a backgrounded pipeline whose output is
  swallowed, which costs a re-run. Two advisory hooks are unaffected, since an
  import failure there loses a note and never a refusal.

  Both partial-update orderings fail closed: an older guard calling the newer
  helper, and a newer guard whose helper is still old — in which case nothing it
  could import can recover it, so it refuses locally.

  While the hook tree is broken these guards refuse roughly a third of ordinary
  commands. That is the cost of the state, not of normal operation, and the usual
  cause is a partially updated install — which the refusal now says.

- The dashboard now withholds your API key values, backup target, backup paths and NAS username from a caller that has not logged in. Setting a dashboard password had always protected them; not setting one now withholds them too. The affected panels show the values again once you set a dashboard password and log in, and nothing else about them changed.
- On an install with no dashboard password set, the login page now says so instead of appearing to log you in. It previously accepted the form and redirected while creating no session at all, so the protected panels stayed blank with no explanation.

- The dashboard's Files tab now requires you to be logged in, on installs that have a dashboard password set. Previously every file route — list, read, write, rename, delete, download, upload — answered anyone who could reach the dashboard port, even with a password configured. Installs with no dashboard password keep working exactly as before, since there is no credential to check.
- Separately, and regardless of whether you have a password, the file browser now refuses to serve paths whose names identify them as credentials: names ending in a credential word (`api-token`, `service_creds.env`), key and environment extensions (`.env`, `.pem`, `.key`), and the usual dotfiles (`.netrc`, `.npmrc`). It was willing to hand over the Claude Code OAuth token and Genesis's internal API token, because the old rule matched `credentials.json` by exact name while the real file is `.credentials.json`. This narrows a measured exposure rather than closing the category: a credential named for its service alone, or carrying no recognised word or extension, is still served, and the rule has no bearing on the write routes.
- The `GENESIS_DASHBOARD_API_AUTH=off` escape hatch reopens the file browser's write, create, rename, delete and upload routes along with the rest of the API's mutations, so a legacy machine caller it was meant to restore keeps working; file reads still require login. The credential-name rule now judges only the path below each allowed root, so an install whose home directory happens to carry such a name is not locked out of the file browser.

- **A background profile that restricts Bash to named binaries now has something enforcing that restriction.** Scoped profiles declare their allowed commands by exporting an allowlist environment variable, and the only code that read it was a global shell hook this repository has never wired into its own settings — so on a fresh clone the restriction was declared and applied by nobody, and such a session ran with unrestricted Bash while every declaration around it said otherwise. Wiring that hook into the repository's settings would not have helped: a dispatched session runs with a working directory outside any git repository, and Claude Code discovers project settings by git-root detection, so repository settings never load there. Enforcement now travels through the settings file the invoker already injects into every dispatched session, which is the one channel that does reach one. The hook returns before reading its input unless an allowlist is actually set, so sessions that declare none are unaffected — measured in both directions on a live install: an allowlisted session refuses a command outside its list and still runs one on it, and a session with no allowlist runs ordinary commands untouched.

  **An allowlist bounds which binary runs, never what that binary can be told to do**, and the one binary a built-in profile allows is one that can be configured to run commands — through an alias, a pager, an editor, a browser, or an extension, each reached with that binary as every token, so the allowlist permits both the command that installs the escape and the command that triggers it. Such a binary now gets per-binary hardening alongside the allowlist, pinning every one of those routes. The set is taken from the tool's own documented environment reference rather than from whatever the last review round happened to find, because routes arriving one review at a time is the signature of a denylist.

  **The route that does not behave like the others is worth stating on its own, because the obvious mitigation misses it entirely.** Extensions are not resolved from the configuration directory — they come from the data directory — so sealing the configuration alone leaves installing an extension and then executing it as arbitrary execution with every token still allowed. This was measured rather than reasoned about, in both directions: an extension placed under the configuration directory was not found, while one under the data directory ran. Pointing the data directory at the same read-only location closes both halves, since the install cannot create the directory it needs and the execution finds nothing. Measured alongside it, so the cure is known not to be worse than the disease: authentication, live API calls and pull-request reads all still work under that pin. One further variable that can name a program was measured INERT and deliberately left alone, with the measurement recorded beside the code so it is not pinned speculatively later.

  The hardening is treated as REQUIRED rather than best-effort, and the check lives where the environment is BUILT and again immediately before the process receives it. That placement is the point, and getting it wrong twice is what taught it: both spawn paths rebuild the environment after any pre-launch verifier runs, and then merge a login fallback on top of THAT, so a check at either earlier point inspects a dictionary that is subsequently added to. The environment that was checked is now the environment that launches. An invocation whose confinement could not be prepared, or whose environment overrides would strip it, is refused rather than launched in a weaker form.

  **What this does NOT give you, stated plainly because a previous version of this note implied otherwise.** A first-token allowlist bounds which BINARY runs. It is not a sandbox. The one binary a built-in profile permits is itself a general-purpose file writer and an authenticated API client, so a scoped session can still write files on the host and reach the network with the operator's credentials — including writes that take effect on a later run. An internal comment elsewhere in the codebase rested a safety argument on that restriction meaning the session "cannot write files"; it did not, and that argument has been removed rather than reworded. Bounding this properly needs a subcommand-level allowlist, which is tracked separately. The honest summary of this change is that a restriction which was previously applied by nobody is now applied — not that the session is confined.

  The chaining denylist gained a bare `&`, which backgrounds the first command and runs the next one and so is not covered by `&&` — that one was a real escape from an allowlisted first token. Parentheses were added alongside it as defence in depth; against real bash they reach nothing the other entries leave open, so they close nothing measurable and do cost an ordinary parenthesis in a filter, a query or a title. Both the enumeration and the cost are recorded beside the code rather than left to be rediscovered.

  Two further supporting changes. The invoker now REFUSES to launch a profile whose allowlist could not be armed rather than launching it unconfined — whether because the settings file could not be written, or because the invocation also asked for a mode that disables hooks entirely. Silently downgrading containment is the failure this whole change exists to remove, so it is not a downgrade worth making quietly at launch either. And the shared hook launcher now runs a shell hook under a shell instead of handing it to the Python interpreter; every hook wired until now was Python, so this path had never been exercised. Routing the new hook through that launcher rather than by absolute path is deliberate — it inherits the launcher's resolution to the main worktree, so the hook cannot drift per branch the way a git-tracked guard otherwise does.

  Separately, the global shell hook no longer treats an unreadable payload as nothing to act on when an allowlist is in force. Reading the command out of that payload needs a JSON tool on PATH, and if it was missing, malformed, or the command was empty, the hook exited cleanly and the restriction dropped in silence — the one failure mode a containment gate must not have. It now refuses instead, and only in that case: a session with no allowlist behaves exactly as before, so a host without that tool is not newly blocked from doing ordinary work.

- The dashboard's Knowledge tab now serves only the knowledge base, not the reference store. Both live in the same table, and the knowledge routes returned every column of every row regardless of which store it belonged to — so reference entries, which carry their stored value in the row body, were reachable through the knowledge browser as well as through the References tab that is built to render them safely. The knowledge routes now refuse that partition on read, search, detail and delete, and the Knowledge tab's counts describe the set it can actually browse. The References tab is unchanged and remains the way to view those entries.

- **A desktop client no longer needs a credential that can do everything.** Until now the desk brain endpoint (`/v1/desk/chat/completions`) authenticated with `GENESIS_MCP_HTTP_TOKEN`, the same token that opens every other `/v1` route: tool execution, memory writes, and the OpenClaw route that starts Claude Code. So any desktop client pointed at Genesis held all of that in its own config file. A new optional `GENESIS_DESK_TOKEN` opens the desk route and nothing else; every other `/v1` route and the MCP transport refuse it. **Nothing breaks on update:** the broad token is still accepted by the desk route, so existing clients keep working. To narrow what a client holds, generate a desk token (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), put it in `secrets.env` as `GENESIS_DESK_TOKEN`, restart genesis-server, and give that token to the client instead. **One behaviour change to know about:** a whitespace-only `GENESIS_MCP_HTTP_TOKEN` (for example a quoted `"   "`) now counts as unset everywhere. The dashboard already treated it that way, but the MCP HTTP transport used to accept it as the literal secret; it now refuses to start instead, the same as when the token is empty. Likewise a whitespace-only `--auth-token` argument now falls back to the environment variable, and a token with surrounding whitespace is trimmed before use, so the MCP transport and the dashboard always compare the same value. A client that also calls a `/v1/voice/*` route (such as `tool_call`) still needs the broad token for those calls; voice has no scoped token yet. The boot warning now names only the routes that are actually disabled.

- **The recursive-delete safety guard now judges a wildcard by the folder it
  sits in.** Deleting `<a>/<b>/<c>/*` removes everything deleting `<a>/<b>/<c>`
  would, but the depth floor counted the `*` as a fourth path level. So it
  refused the folder and allowed its entire contents: `~/tmp/*` or `~/tmp/tmp*/`
  in a scratch area shared by every session, for example. The floor now applies
  to the literal prefix before the first `*`, `?` or `[`.
  A wildcard whose literal prefix is at least four levels deep (for example
  `/home/<user>/tmp/<job>/*`) is unaffected. Patterns that can match `..` and
  climb upward are refused: a dot-wildcard in the middle of a path (`…/.?/…`),
  a `..` after any wildcard, and any extglob group (`@(…)`, `!(…)` and the
  rest), whose extent the guard cannot see. Measured
  over 1,622 recorded delete commands on one install: 20 (1.23%) are newly
  refused, all wildcard cleanups directly under a shared scratch folder, and
  none is newly allowed. Keep per-task scratch in its own subfolder so a
  wildcard cleanup stays at least four levels deep.

- **Every page `web_fetch` returns is now marked as untrusted content.** Pages fetched through TinyFish, Firecrawl, Crawl4AI or the Ladder backend used to reach the calling session unmarked; only the plain HTTP path wrapped them. All of them now arrive inside the same `<external-content>` boundary, and so does every other string in the result (titles, descriptions, authors, error text a backend echoes back, and any field a backend adds later), URLs and language tags included (only the name of the backend Genesis used stays plain), so sessions reading fetched links, including the inbox evaluator, can tell the page's text from their own instructions. The clean-up that runs before wrapping now also neutralises boundary markers a page nests inside one another, which it previously left partly intact.

- **Observations written by a session over untrusted content are now clearly marked.** When a session reading attacker-authored material (the inbox evaluator, a gateway conversation) writes an observation, its type, source and category are stored with an `untrusted:` prefix, and `critical` priority becomes `high`. Genesis's own pipelines select observations by exact name, so such a row can no longer be mistaken for an escalation, an update notice, an alert or a detected task, and it can no longer page the owner. Nothing is refused: the row is kept under a name that says where it came from, and the owner's world snapshot still shows the inbox evaluator's signals, marked as untrusted content.

- The Genesis API (`/api/genesis/*` and the tool API `/api/t/*`) now answers only callers on loopback or the Tailscale address range by default, whether or not a dashboard password is set; the host supervisor's health, heartbeat and pause-state probes stay reachable from anywhere. **If you open the dashboard over your LAN rather than Tailscale, add your LAN to `GENESIS_DASHBOARD_TRUSTED_NETWORKS`** (comma-separated CIDRs; loopback is always allowed), or set `GENESIS_DASHBOARD_NETWORK_SCOPE=off` to disable the check. See `secrets.env.example`.

- The CI leak scan (detect-secrets, gitleaks, email, binary-artifact and private-pattern checks) now runs on every push to every non-main branch, not only on pull requests, so a branch published without a PR is still checked. The pull-request check and the new branch check run the same script, `scripts/ci/leak_scan.sh`. On a branch push the private-pattern scan and a new gitleaks history scan cover every commit the branch carries that main does not, so a secret added and then removed inside the branch is still caught; the email and binary-artifact checks also read that history, and a merge commit is read as its conflict resolution. Every push gets its own scan run, and scan logs name the file and line of a finding but never the matched text.

- **The VNC fallback no longer starts without a password.** When systemd is unavailable, the browser tools start `x11vnc` themselves; with no `~/.genesis/vnc_passwd` that fallback used to start it with no authentication. It now keeps password authentication like the `genesis-vnc` unit, and when the password file is missing or unusable (not a readable file of at least 8 bytes) it starts nothing and logs that `scripts/setup-vnc.sh` creates one.

- Reject unencrypted or integrity-invalid OpenPGP backups during recovery and transcript evidence reads; failed decryption preserves existing destination files.

- Backup restores and migration rollback reset saved peer authority before database
installation. Guardian snapshot rollback explicitly warns that approval restores
saved peer permissions.

- **Genesis now refuses to type, store or send a payment card number through its general tools.** `browser_fill` refuses a card number as the value, and `browser_run_js` refuses a script containing one. `reference_store` and automatic reference capture never save one, and outbound posts (including Discord replies) flag one as critical. Detection needs a known card brand prefix and length plus a valid checksum, so ordinary long numbers, timestamps and UUIDs pass. Card entry is meant for the encrypted card vault tracked in #3172.

- The smb off-site backend now refuses a file or directory name holding a quote, `;`, a backslash or a newline instead of passing it to smbclient; such a file is reported as a failed upload.

- The dashboard's content routes now answer only to requests addressed to this machine: by IP address, `localhost`, the machine's own hostname, or its Tailscale name. If you open the dashboard by another name, add it to `GENESIS_DASHBOARD_ALLOWED_HOSTS` or use the IP address; `GENESIS_DASHBOARD_HOSTNAME_CHECK=off` turns the check off.

- **Browser tools no longer pass a payment card number shown on a page back to the model or into logs.** Every browser tool result and error (page snapshots, `browser_run_js` results, page URLs and titles, exception text, the tool-timeout message) now shows a card number as `[card redacted]`, including one a script returns as a number or as escaped JSON, and one split into digit groups across snapshot lines, list items or object values. `browser_screenshot` refuses, writing no file, when the text a page renders in any frame shows a card number: visible text, form values, placeholders, labels, titles, alt text, options and CSS-generated content. `mcp_health.log` is redacted for every Genesis log line it receives, tracebacks included, and so are the Turnstile debug log, FastMCP's tool-error log and the CAPTCHA Telegram alert's page URL. Detection is the same as the input guards: a known card brand prefix and length plus a valid checksum.

- **Logs redacted for card numbers now also catch a card split across separators.** The card log filter on `mcp_health.log` and the browser tool loggers now redacts a card number whose digit groups are split across lines, separators such as ` / `, or one digit per box, in the message, its arguments and its traceback, and one spelled as `\u` escapes when the whole log message is JSON text. A log record that cannot be turned into text or checked is replaced with a withheld notice instead of being logged as is or raising into the code that logged it, and JSON text nested too deeply to check is withheld rather than raising an error.


### Added

- **A warning when a git command is about to rewind your whole working tree.**
  `git checkout <commit> -- .` reads like "put these files back", but the `.`
  matches every tracked path — so it rewrites the entire tree to that commit and
  silently reverts anything merged since. Because the reversion lands in your own
  working tree, it then shows up inside your own diff looking deliberate. That
  happened here: one such command reverted two already-merged changes, the first
  caught by luck and the second only by a separate check.

  The guard already snapshotted the tree beforehand, so nothing was unrecoverable
  — but its note was the same one it prints for every checkout, and said nothing
  about what had just happened. It now recognises the shape (`checkout`,
  `restore` with a source, `switch`, and `read-tree -u`, applied across a
  directory or the whole tree) and says so plainly: what the command rewrites,
  which repository, that the reversion will look intentional in your diff, and
  the conflict-aware alternatives that fail loudly instead (`merge --squash`,
  `cherry-pick`, `apply --3way`).

  It also gives you a recovery command that works. The obvious one does not:
  after a rewind every tracked file is staged, which is exactly what
  `git stash apply` refuses to merge into — run it and you get conflict markers
  written into the files you were trying to save. The note points at the
  restore-from-snapshot form instead, and says why.

  It warns rather than blocks, deliberately: the command is recoverable, and this
  guard blocks only what its snapshot cannot recover. Everyday discards
  (`git checkout .`, `git checkout -- file`, `git checkout HEAD -- .`) stay
  silent, because a warning that fires on routine work is worth nothing on the
  day it matters.
### Fixed

- **Code-intelligence queue results now survive SQLite lock pressure without
  replaying successful work.** Enqueues and terminal runner outcomes have a
  file-and-directory-fsynced fallback inbox, and terminal events are bound to
  the exact claim generation so stale or conflicting results cannot consume
  newer work. Legacy numeric state is range-checked before SQLite binding, an
  unreadable legacy artifact is quarantined without wedging the queue, temporary
  event files cannot be mistaken for complete state, an exhausted inflight
  generation no longer deletes a newer pending request, and direct queue
  mutations require the claim nonce. Empty index directories no longer queue a
  pointless rebuild. Daily disk hygiene also exits
  non-zero after completing its other cleanup steps when disk remains critical
  or last-resort cache reclaim had to defer, making the unit failure visible;
  the existing reactive disk remediation independently retries every 30 minutes
  and escalates after its configured attempt limit.
- **Captured session output is now redacted before it is stored.** When an
  interactive session exits, Genesis records a tail of the terminal scrollback to
  `~/.genesis/logs/cc_exit_<slot>.log` so a crash can be diagnosed afterwards.
  That tail is raw terminal output, so it passes through the secret scrubber on
  the way in; if the scrubber cannot run, the tail is withheld rather than stored
  unscrubbed. The exit status and crash diagnosis are recorded either way, so the
  log keeps its diagnostic value.
- **Credential detection covers modern key formats.** The shared
  secret-detection patterns expected an unbroken run of letters and digits after a
  vendor prefix, which most current key formats no longer are. Detection now
  handles them, along with several provider prefixes, JWTs, webhook URLs and bot
  tokens. Detection is shape-based and therefore not exhaustive; the labelled and
  `KEY=VALUE` forms remain the broadest net. The reference-capture path keeps a
  deliberately narrower rule, so an ordinary hyphenated name is never stored as
  though it were a credential.
- **Secret scrubbing completes in linear time on every input shape.** Two
  detection patterns backtracked quadratically against long unbroken character
  runs, taking tens of seconds on 40KB of the wrong shape. The rework is
  measured across a matrix of input classes rather than a single benchmark —
  the first attempt was validated on one shape and remained quadratic on
  another — and every cell now completes in under 40ms where the slowest was
  previously tens of seconds. A locked perf-matrix test spans those classes so
  a future pattern edit cannot pass on a flattering input. In practice the
  capture path was already insulated (terminal capture wraps long lines at pane
  width, measured), so this is hardening for any future caller that feeds
  unbroken machine output.
- **Private-key blocks and more vendor key formats are detected.** A pasted or
  displayed PEM private key is now redacted — including one that is only
  partly on screen, since a captured tail starts and ends at arbitrary points
  — along with several additional vendor prefixes, and tokens appearing on
  diff-style `-`/`+` lines are handled the same as anywhere else. Redaction
  around a key marker covers the adjacent key material and stops there, so an
  ordinary diagnostic keeps its surrounding lines. Key material is recognised
  even when the capture decorates every line — a log timestamp, a service
  prefix, a diff marker, an indent — rather than only when it starts at the
  left margin. The scrub subprocess in the
  exit-capture path also gained a wall-clock bound and an input cap, both
  failing toward withholding the tail rather than storing it unscrubbed.
- **A password inside a URL is redacted whatever the URL looks like.** Two
  length limits had been placed on parts of a connection URL to keep scanning
  fast, and each one quietly stopped redacting past its ceiling. Both are gone:
  the scan no longer looks at the part that needed bounding, so it stays linear
  with no length at which a real credential is missed. A URL that carries a
  password and no username — the form some generated connection strings take —
  is now redacted too; nothing else recognises an unlabelled password, so it
  had been stored as-is.
- **Long terminal lines are redacted as one line, not as fragments.** A
  terminal stores a line longer than the window is wide as several rows.
  The capture now reassembles them before scanning, so a value that happens to
  straddle the right edge is treated as the single value it is. And when a
  capture exceeds its byte cap, the newest output is what survives — the cap
  used to keep the oldest scrollback and discard the dying words the log
  exists to record.
- **The scrubber runs on any supported Python.** Its patterns use only
  widely-available regex syntax, so the capture path works with whatever
  `python3` an install provides rather than requiring a recent one — it is
  deliberately not tied to the project virtualenv, which may be unavailable at
  exactly the moment a session is crashing. Scanning cost stays linear in the
  size of the captured text.
- **SECURITY.md described a posture the code left behind two months ago.** The
  security policy told operators to treat the dashboard API as
  "unauthenticated administrative access" and said the dashboard password
  "protects the web UI, not the programmatic API". Neither has been true since
  the API mutation gate landed: with a password set, state-changing `/api`
  requests require a bearer token or an authenticated same-origin cookie with a
  CSRF check, and `/v1` enforces its own bearer. The doc now says so -- along
  with the limits that decide whether you still need network isolation, because
  you do: the gate covers mutations only, reads stay open, it is inert when no
  password is set, and it has a documented kill switch.

  Corrected in the same pass: the autonomy ladder was described as seven levels
  (`L0`-`L6`) when four ship (`L1`-`L4`, the rest deferred), the per-category
  permissions were described as six categories when there are four, and the
  process-group-kill example was attributed to a PreToolUse hook when it is
  runtime library hardening -- a different guarantee, since a hook cannot be
  bypassed by the agent and a helper only protects its own call sites. The
  guard section also named one linter as the enforcement mechanism for shell
  and URL policy; that linter only ever sees file edits, and the shell and web
  guards are separate programs chosen by tool matcher.

  Newly documented rather than corrected: provenance stamping and the two
  privileged-write paths gated on it, the approval requirement and journal now
  standing in front of irreversible entity merges, session-id validation before
  filesystem use, and an External Egress section that says plainly which
  outbound channel actually enforces a gate today and which are only observed.


### Changed

- **A capability can no longer earn standing autonomy just by existing.**
  Genesis's per-capability trust cells can be promoted from "ask me every time"
  to a standing grant once a capability accumulates approved successes. That
  promotion path was scoped by evidence but not by capability: any future
  capability whose cells recorded five approved successes would have been
  offered to you for promotion, and approving it would have converted
  per-action approval into standing authority. The same was true of anything
  money-related: Genesis's own definition calls financial actions
  "never trust-unlockable", but nothing actually stopped a financial capability
  being promoted -- it was held back only by the order of two statements in the
  email path. Promotion now requires passing two fixed bars: the capability must
  be on a short allowlist (email only today) and must not be financial, checked
  both where candidates are proposed and at the state change itself. Everything
  else stays at ask-me-every-time for its whole life -- still learning, still
  tracking evidence, but never converting that into a standing grant without a
  deliberate code change. No behaviour change for ordinary email sends.

- **The session charter now lists every open ledger item, not just the oldest
  six.** The ledger is a curated list of one-line to-dos, and the old window
  meant a session with more than six open items never saw a newly added one in
  its own prompt — it existed only in the aggregate count. The list is now
  effectively unbounded (a 200-row ceiling with an explicit "more than 200 —
  the rest are not listed" note), and an oversized charter block degrades by
  dropping whole sections with a marker rather than cutting mid-bullet, with
  the open/closed count preserved.

- **A review comment on documentation no longer blocks a merge.** The pre-merge
  check already declined to count findings on prose, but its idea of prose was
  narrow: markdown counted only underneath `docs/`, so a comment on a top-level
  guide, or on one of the instruction files that shape how the assistant works,
  was treated as a finding on code and held the merge. Any markdown, reStructured-
  Text or AsciiDoc file now counts as documentation wherever it lives.

  The narrowness was deliberate, and it is being relaxed on one specific ground:
  a separate check, at the moment work is committed rather than merged, classifies
  those instruction files independently and still requires a proper review before
  they can change. So what moves is only whether a reviewer's comment on prose can
  hold up a merge — not whether prose gets reviewed.

  Reversible without another change: `merge_gate.doc_findings` accepts `skip`
  (the default, prose never blocks), `p1_only` (only the highest severity blocks
  on prose), or `score` (prose is treated exactly like code, the previous
  behaviour). Findings are listed in the pre-merge report under every setting —
  the lever decides whether they count, never whether anyone sees them.
## [v3.0b18] - 2026-09-07

### Added

- **Work you agreed to in a session that has since ended no longer goes quiet.**
  A session's ledger is its list of agreements, and it was only ever visible to
  that session — so when a session ended with items still open, they stopped
  being visible to anything. On the install this was built against, 15 items had
  been sitting undisposed, the oldest for 52 days, and not all of them were
  internal development work — some were user-facing requests, which is the class
  most likely to be missed everywhere else.

  An hourly sweep now turns such an item into a follow-up asking what should
  happen to it — done, absorbed into other work, or no longer needed. It only
  fires when BOTH the item has gone untouched for five days AND its session has
  been quiet for five days, so an item you are still working on is never taken
  out of your hands. Disposing of the item closes the follow-up automatically at
  the next sweep. Escalations arrive up to five per hour rather than all at once,
  and they appear in `follow_up_list` and the dashboard follow-ups tab under the
  source `ledger_escalation` — deliberately not in the morning report, since
  ledger text is free-form and has contained credentials.

  They arrive unclassified rather than guessed at: the sweep genuinely cannot
  tell one of your errands from an internal development item, so it asks.

  Tune or disable it with the `ledger_escalation` settings domain (`stale_days`,
  `quiet_days`, `max_per_run`, `priority`), or turn it off entirely with
  `GENESIS_LEDGER_ESCALATION_DISABLED=1`.

- **A `tmux kill-server` with no socket binding now draws an advisory.** tmux
  resolves its target server from the inherited `$TMUX` variable before
  `TMUX_TMPDIR`, so a cleanup aimed at a scratch or probe server can address
  the main server instead and take down every live session on it — including
  the one issuing the command. Clearing `$TMUX` does not help: that only
  re-targets the default socket, which is usually the main server too. A new
  advisory-tier guard flags a `kill-server` carrying no explicit `-S`/`-L`
  binding and points at the one safe form — binding the kill to its own
  socket. Session-scoped kills (`kill-session`) are deliberately not flagged,
  and the advisory never blocks anything.

- **The review-round limit now has an end, not just a speed bump.** The existing
  limit pauses after three rounds in which an independent reviewer keeps finding new
  problems, and asks for a conscious decision to continue — but acknowledging it
  resets the count, so the same three-round cycle can repeat without limit. A change
  could consume fifteen rounds and never be told "enough", only "enough, for now".

  A second count now runs over the whole life of a change, and at seven rounds it
  stops rather than pausing. Two full cycles have already run by then, each of which
  already asked for a decision; something still surfacing new problems after that is
  not converging, and the remaining question — accept what is outstanding and ship
  it, or abandon the branch and start from a design that does not need seven rounds —
  is a judgement call, not one to keep deferring. Both answers require a person, so
  the block says so and neither is available to an unattended session.

  Where the decision is recorded — committing the accepted work — the acceptance is
  spent when used: it clears one commit, once, and re-using it does not buy another
  round. That is the whole difference between a terminal and a fourth repeatable
  escape hatch. The limit also applies when requesting the next review, because the
  local count sleeps through rounds that happen entirely in the cloud, which is
  exactly the shape a long loop takes; that second check keeps no state of its own,
  so there the acknowledgement is required every time rather than being spent once —
  and one acknowledgement covers one request, so a single decision cannot be chained
  into several.

### Fixed

- **Marketing campaign updates now post to their own Telegram topic.** The marketing
  campaign's tick updates previously routed to the shared Morning Reports topic; they
  now go to a dedicated "Marketing" forum topic via a new `marketing` outreach
  category, keeping them separate from the morning report and other digests. Existing
  installs pick up the category automatically on restart (an additive schema
  migration); the morning report and all other topics are unaffected.
- **Proactive memory recall could time out completely.** The embedding provider
  queues standard-rate requests when a model is busy, and that wait can run past
  recall's four-and-a-half-second budget — measured at eight to thirteen seconds
  against a budget of four and a half — so every lookup fails and sessions run with
  no recalled memory at all. Recall now asks for the provider's priority lane, which
  answers in about 650 milliseconds regardless of how long the prompt is.

  **This costs a little more, and the amount is worth knowing: one and a half times
  the standard embedding rate, on recall only.** At one person's usage that is a
  fraction of a cent a month, and declining it means keeping a feature that does not
  work. Storing memories stays on the standard rate — that runs in the background
  with nothing waiting on it. Set `GENESIS_EMBED_PRIORITY_TIER=false` to decline the
  faster lane; recall then falls back to keyword-only search whenever the queue is
  longer than the deadline.

  Scope: this covers the proactive-recall path served by genesis-server. An explicit
  `memory_recall` tool call through a standalone MCP process still uses the standard
  lane, so it can be slow without failing.

  The setting is a yaml lever as well as an environment one, and the template no
  longer overrides it: a fresh install copies `secrets.env.example` to `secrets.env`
  and the environment is read first, so an uncommented assignment in the template
  would have quietly outranked `memory.embed_priority_tier: false` and left the
  documented opt-out doing nothing.

- **The setup script's questions about local inference servers had no effect.** The
  same shadowing applied to the Ollama and LM Studio addresses: the template assigned
  them, a fresh install copied that to its environment, and the environment outranks
  the config file — so the address the interactive setup script asks for was written
  to the config and then ignored, and every call went to localhost regardless. Those
  assignments are now commented out; the values they held were already the defaults.
  The Ollama on/off switch deliberately stays assigned, because unlike the addresses
  its default differs from the template value, and removing it would switch Ollama on
  everywhere.

- **A malformed config section could quietly weaken the private-data scan.** The
  fingerprint harvester, which collects this install's private values so they can be
  blocked from ever reaching a public push, read config sections the same unguarded
  way — and its error handling covers the whole harvest, so one bad section dropped
  not just the addresses it was reading but the timezone and private-repository
  patterns queued behind them, with nothing logged. Sections are now read defensively
  there too. Separately, ignoring a malformed section is no longer silent anywhere: it
  logs which section was discarded and that defaults are in force, because two of
  those settings fail toward spending money and toward running an autonomous job the
  operator had switched off.

- **Model routing ignored the install config, so half the system talked to the
  wrong machine.** Settings like the local inference server's address are resolved
  in one documented order — environment, then the install config file, then a
  built-in default — but the routing layer expanded its placeholders from the
  environment alone. That was invisible while the template forced the same values
  into the environment anyway; removing those assignments so the config file could
  work is what exposed it. An install pointing at a remote inference server ended
  up with its dashboard, health check and embeddings reaching that server while
  routed model calls still went to localhost. Routing now resolves those settings
  the same way everything else does, which also means the setup script's questions
  about local inference finally take effect. An environment variable still wins
  where one is set, and any placeholder without a matching setting behaves exactly
  as before.

- **A quoted "false" in the install config meant true.** Settings written in
  `genesis.yaml` are read as booleans, but a value in quotes arrives as text, and
  any non-empty text counted as on — so `embed_priority_tier: "false"` kept the
  paid lane running, while the same word unquoted, or written in `secrets.env`,
  correctly turned it off. One intention, three places to write it, two answers.
  The same spellings now mean the same thing everywhere, for every on/off setting
  in that file.

- **An override set from the dashboard could never be unset again.** Some settings
  can be given a value in the config file or overridden in the environment, and the
  environment always wins. The settings editor could set those overrides but had no
  way to clear one, so the first time you typed a value there it took over
  permanently — later edits to the config file appeared to do nothing, with no way
  back short of hand-editing the file the editor exists to avoid. Clearing the field
  now removes the override and hands the setting back to the config file. Required
  credentials still cannot be blanked.

- **A config file that was unreadable as settings was ignored in silence.** A
  malformed section already said so; a malformed file did not, even though it still
  contained everything the operator had written — including the switch that keeps
  memory lookups off the paid lane. It now says plainly that the whole file is being
  ignored and where it is.

- **The timezone control could delete the config it exists to repair.** If the
  file was malformed in a way that left its contents unreadable as settings, the
  dropdown rewrote it with the timezone alone — discarding whatever else was in
  there, silently, on the one control documented as the way to recover. It now
  copies the original alongside first, writes the timezone, and says plainly what
  it did and where the copy went.

- **Settings the template ships commented out disappeared from the dashboard.**
  Some defaults are deliberately left commented so the equivalent setting in
  `genesis.yaml` keeps working. The dashboard's editor only recognised
  uncommented lines, so those keys vanished from it and updates were rejected as
  unknown — including several that had been invisible this way for some time.
  They are listed again, simply with no value set.

- **A one-line typo in the install config could silently disable vector memory.**
  Accessors that read a nested setting out of `~/.genesis/config/genesis.yaml`
  assumed the section around it was a mapping. Two shapes an ordinary edit produces
  are not: a section whose only child is commented out (which yaml reads as empty
  rather than absent), and a section given a plain value instead of a block. Either
  one raised on the next read, and because the memory subsystem catches everything
  around its own startup, the install would come up reporting a degradation and then
  run with no vector memory at all — from a config file the operator is invited to
  edit by hand. Every such setting — the local inference URLs, the Ollama switch, the
  recall priority lane, the build lane, the models-file synthesis job, and the GitHub
  identity — now falls back to its documented default instead, as does a config file
  whose top level is malformed outright.
- **Two branches that each add a changelog entry no longer collide over it.**
  This file is an append-only list of independent bullets, so two branches
  adding an entry under the same heading are not disagreeing about anything —
  they are inserting at the same position, which git's default merge reports as
  a conflict a human has to resolve by hand. It now merges with git's `union`
  driver, which keeps both sides' lines instead of leaving markers. It makes no
  promise about their ORDER — git's own documentation says union "tends to leave
  the added lines in the resulting file in random order and the user should
  verify the result" — so a merged section may need its entries re-sorted by
  hand. For a list of independent bullets that is proofreading, not breakage.

  Measured before the change, against the repository's own open work: of 49
  open pull requests, 21 could not merge, and **18 of those 21 conflicted on
  this file and nothing else** — every other file in them merged cleanly.
  **Two things it deliberately does not do**, because the measurement above is
  easy to over-read. It does not make a conflicting pull request mergeable on
  GitHub: GitHub ignores a repository's `.gitattributes` in its server-side
  merge, measured against GitHub's own merge engine on two branch pairs built to
  collide on this file, which conflicted both with the attribute present and
  without it. And it does not help a branch's *first* merge, which is the one an
  already-open pull request needs — attributes resolve from the checkout rather
  than from the commits being merged, so on a branch created before this file
  existed the merge that introduces the rule is not governed by it. Measured on
  a real open pull request: the changelog still conflicts.

  What it does buy, stated narrowly: once a branch contains the file — every
  branch cut after this lands, and any older branch after its first merge —
  later merges of the base branch resolve the changelog with no hand-editing.
  Measured in that direction across the same 18: all of them clean, with every
  bullet from both sides intact. The structural fix is one fragment per change
  under `changelog.d/`; this rule does not replace it.

  The rule is scoped to the one file at the repository root, and the tests
  enforce that scope over the complete tracked-file list rather than a sample.
  The leading slash matters: a pattern without one matches the basename at
  every depth, which would silently hand the same driver to any future
  vendored or subproject changelog.

  What `union` cannot express is a **removal**. If one side deletes lines while
  the other edits the same place, it keeps the deleted lines and reports
  success — so pruning an entry, reverting a commit that added one, or cutting
  a release (which moves entries under a version heading rather than adding
  them) can quietly come out wrong, with a zero exit code and nothing visibly
  duplicated to catch the eye. Read the merged file in those three cases.

  Union merges lines, not records, and that reaches insertion-only merges too:
  two entries sharing an identical aligned line — the same closing sentence, the
  same title — can collapse into one, again at exit 0. Measured across the 18
  real colliding pull requests, every bullet from both sides survived intact in
  all 18; the failure needs identical lines and these entries are long
  distinctive prose. So it is a real edge with a measured rate of zero, worth
  knowing when writing a terse or templated entry.

  The attribute also governs `git revert` and `git cherry-pick`
  (`gitattributes(5)`) and, measured here, `git merge-tree` — but only when the
  checkout running them already carries this rule, since attributes resolve from
  the current checkout rather than from the commits being compared. Two
  consequences were measured rather than assumed: reverting an *older* commit
  that added an entry is absorbed, and git then reports "nothing to commit" with
  a non-zero exit, so a caller checking exit status still notices; and the
  guardian's automated `git revert HEAD` on a clean tree is unaffected, because
  there both sides equal the base and the driver never runs.
- **A session slot started after another tmux server no longer gets the wrong
  temp directory.** A new slot created while a tmux server started in some other
  context is already running used to inherit that server's temp directory
  (often the small system `/tmp` Genesis keeps Claude off of). The temp
  directory and the OAuth-durability setting are now pinned to the slot
  explicitly (when a usable temp directory exists at all — if none does, the
  session is left on the system default rather than pointed at a bad path).

- **Two branches can no longer pick the same database-migration number.** Each
  new migration is now named by the UTC time it was written rather than by the
  next free number, so nobody has to check what anyone else took — and two
  people working at once cannot both claim the same one. The numbers already in
  use are frozen exactly as they are; an existing install is unaffected and runs
  nothing again. A migration that has already shipped can no longer be renamed
  or removed either: installs that already ran it would never run its
  replacement, so the two would drift apart with nothing to notice. And a
  migration whose name is subtly wrong — a digit too few, filed in the wrong
  folder — is now reported instead of being quietly skipped, which is what used
  to happen: the file simply never ran, and the change that needed it shipped
  without it.
- **The wrong-repo commit check now says when it did not run.** It works out which
  repository a `git add`/`commit` targets by reading the command text, and when that
  text did not determine a directory — a shell variable, a command substitution, a
  glob — it joined the unexpanded token onto the current path anyway. The result
  cannot exist, so every lookup against it failed and the check was skipped through
  the same branch that means "this repository is not covered". A command it could
  not inspect was therefore indistinguishable from one it deliberately ignored.
  It now reports that the check did not run, on **119 of 2,264 (5.3%)** real
  `add`/`commit` commands. Deliberately an advisory and **not** a new refusal:
  replaying those same 119 through the old behaviour, it blocked **0** of them —
  it was failing open, so nobody has ever been wrongly stopped by this, and making
  it refuse would newly stop 119 ordinary commands to fix a silence.

- **The cold-marketing campaign no longer re-pitches the same person.** Once a
  marketing pitch is delivered to a prospect, that prospect is marked contacted and
  drops out of the campaign's target list — previously nothing recorded the contact,
  so the campaign would have re-pitched every delivered target on each run. Works on
  both the owner-approved and (future) autonomous send paths; a pitch that never
  delivers (dropped, expired, or rejected) leaves the prospect eligible for a later,
  re-worked pitch. (The substrate still ships off by default.)

### Changed

- **Mistral Large is now tracked as a paid provider.** Mistral removed the Large
  model family from free-tier entitlement (unannounced; surfaces as
  `403 tier_not_allowed`). The `mistral-large-free` provider is now flagged
  `free: false`, so its spend is recorded at real rates ($0.5/$1.5 per MTok)
  instead of $0, and call sites marked `never_pays` no longer route to it. The
  provider name keeps its historical `-free` suffix to avoid churning the 30
  chains that reference it. If your account tier still gets Large at $0,
  override `free: true` in your local routing overlay.

### Added

- **Claude Code's auto-updater suppression now re-asserts itself, and "verified"
  means verified.** The two kill switches (`DISABLE_AUTOUPDATER`/`DISABLE_UPDATES`
  in the user-level `~/.claude/settings.json`) were written only at install time,
  so a machine whose settings drifted stayed silently unprotected until someone
  re-ran setup — twice CC self-updated past the pin that way. One shared owner now
  re-asserts them on every install/bootstrap/update and on a daily container timer
  (`genesis-cc-settings-align.timer`), whose unit goes red rather than staying
  green when it cannot verify. The outcome channel is fail-closed by
  construction: the state starts `unverified` and is promoted to `ok`/`repaired`
  only where a post-operation read confirms both keys are on disk — an audit
  found nine paths that previously reported success without checking (a
  busy lock, a missing library, a write never read back, and callers that
  discarded the outcome entirely), and each now either verifies or says plainly
  that it could not.

  Three follow-ons keep that honesty intact where it was still leaking. A repair
  performed during the early part of a deploy is now recorded even though the
  later check finds nothing left to fix — previously that repair vanished
  entirely, because it happened in a separate process whose result could not
  travel back. A verified-clean run that happens to overlap another run now
  counts as clean, so the next unrelated repair is no longer misreported as "the
  second in a row" and does not raise a false alarm about something repeatedly
  rewriting the settings file. And `uninstall.sh --dry-run` no longer clears the
  saved timer schedules for real: that was the one step in the uninstall that
  ignored dry-run, and it can change whether a missed scheduled run replays
  after a later reinstall.
- **Telegram ping when someone replies to a marketing pitch.** When a real person
  replies to one of Genesis's cold marketing emails, you now get one brief
  Telegram notification — the sender and the first line of their reply.
  Auto-responders (out-of-office / bounces) and spoofed senders are filtered out,
  so you're pinged only for genuine human replies, and never for sends. The reply
  is still recorded on the dashboard exactly as before; this just surfaces it to
  you immediately instead of waiting for you to go look.

- **Gated autonomous cold marketing outreach (inert by default).** Genesis can
  now stage cold marketing emails to an owner-curated prospect list via the new
  `marketing_send` tool. The recipient is resolved in code from a private
  `marketing_prospects` store (by id — the tool never accepts a raw address),
  respects permanent opt-outs, and every send still holds at the email
  authorization gate for your approval. It ships OFF: nothing sends until you set
  the `marketing_outreach` lever to `observe`/`live` (kill switch:
  `GENESIS_MARKETING_OUTREACH_DISABLED=1`). Autonomous cold sending requires BOTH
  affirmatively setting the lever to `live` AND the BULK capability cell earning a
  grant through your approvals — in `observe` (or `off`) every cold send holds for
  your explicit approval even after the cell is granted. Manage prospects and
  opt-outs in the `marketing_prospects` table. Hardened: the `marketing_send`
  actuator is reachable only from the `campaign` session profile (every other
  profile — including the untrusted-inbound `mail`/`community-responder` perimeter
  — denies it, so an injected inbound message can't reach it); arming `live` is
  overlay-file-only and cannot be set through `settings_update` (a model can't
  self-elevate past the observe gate); a held send to a permanently opted-out
  prospect is refused before delivery regardless of how the pitch classified (a
  money-pattern body that lands as FINANCIAL no longer bypasses the bulk-only
  opt-out check); and the `pending_outreach.labeled_surplus` migration no longer
  swallows ALTER errors (a transient lock is retried, a real failure fails loud); and an
  already-approved cold send is halted at delivery if the lever is flipped to `off` / the
  kill switch is set before it goes out (the outer off-switch is now honored deliver-side,
  not only at enqueue — the held send is paused and resumes if you re-enable).

- **Contributor-issue close loop.** When an external contributor's merged PR
  closes a GitHub issue Genesis posted from the Contributor Work-Log (via a
  `Closes #N` keyword), the repo-pulse worker now auto-resolves the originating
  follow-up — so shipped contributor work no longer lingers as a false TODO.
  Scoped to default-branch merges and same-repo references; idempotent, and
  fails closed on any ambiguity (empty/unresolvable state no-ops; a genuine read
  failure fails the run and preserves the cursor to re-cover). Gated by the same
  `repo_pulse` lever. Hardened: the originating follow-up id is resolved to its
  canonical form at proposal time (a prefix/tagged/uppercase handle resolves to
  the full id; an ambiguous or unknown handle is rejected rather than stored as a
  ref the join can never match); and only issues Genesis actually CREATED are
  treated as authoritative close-links — any issue Genesis merely ADOPTED (a
  pre-existing open issue with a coincidental same title, or one re-found after a
  crash) is recorded as adopted and excluded from the join, so a PR closing it never
  falsely resolves a follow-up. (Adopt provenance no longer relies on issue
  authorship, which cannot be trusted on a single-account install.)

- **Contributor issues are labeled by domain and difficulty, enforced at the source.**
  Every issue the Contributor Work-Log proposes must now carry an `area:*` domain
  label (memory/dashboard/runtime/guardian/autonomy/channels/knowledge/eval, or
  `area:other`) and a difficulty/environment label (`good first issue`,
  `first-timers-only`, `needs-genesis-instance`, or `help wanted`) —
  `contributor_issue_propose` rejects a proposal missing either (fail-closed, after
  the privacy scan). The public PR template now prompts for a `Closes #NNN` keyword,
  and CONTRIBUTING documents that a bare `#NNN` won't auto-close the linked issue.

- **Concurrent-session awareness now says what a peer is working on, and which
  model it runs.** When several Claude Code sessions share an install, each one
  is shown a `[Concurrent | …]` line per peer. Those lines previously carried a
  digest of the peer's last few tool calls — so a peer read as
  `Bash grep -n "Version History"`, which says nothing about what it is doing.
  They now carry the peer's model and its topic. The topic is whichever is the
  more recent account of what that session is doing: the summary Genesis
  already writes when it summarises a session's activity, refreshed on a cycle
  of a couple of hours, or the session's own mission, set the moment its
  purpose changes. A mission declared after the last summary wins; otherwise
  the summary does. Sessions whose mission predates this release keep showing
  the summary, because there is no honest way to know when an older mission was
  set. Where neither exists it falls back to the session's newest in-progress
  or open ledger item. All of those are written by Genesis rather
  than by you: the raw first user message is deliberately never used, for the
  same reason the peer's typed prompts are already withheld — another session's
  user text is decontextualised in yours.

- **A session that is working but not being typed into no longer disappears
  from its peers.** Peer lines are hidden once a session's heartbeat is ten
  minutes old, and the heartbeat previously only refreshed when its user typed
  — so a session heads-down on a long task silently vanished from everyone
  else's view exactly while it was busiest. A tool-use refresh now keeps it
  visible. It is throttled to at most one write a minute per session; on every
  other tool call it costs a file stat and the hook's own module load, which is
  a few milliseconds and no database work at all.

- **Outreach total-cessation monitoring, without the old false-alarm trap.**
  Outreach is now in the `subsystem_stale` alert set (WARNING) alongside
  ego/inbox/dashboard. Previously it was excluded because its heartbeat was
  *emergent* — a side-effect of an outreach job succeeding — and the outreach
  scheduler only starts once a messaging channel (Telegram) registers, so a
  Telegram-less install never pulsed and naively adding it would fire a permanent
  unresolvable alert. Two pieces close that trap: (1) a dedicated,
  channel-independent heartbeat daemon (`outreach/heartbeat.py`) that pulses **only
  while the outreach scheduler is actually running** (`is_running`) — so a
  never-started/stopped scheduler goes stale instead of reading a false `alive`; and
  (2) an enable-gate (`_subsystem_enabled('outreach')` = Telegram configured, via the
  same side-effect-free `build_bridge_config` loader onboarding-readiness uses) so a
  dashboard-only install is benign. Documented boundary: a Telegram-configured install
  whose scheduler *never once started* (registration failed) emits no pulse and stays
  a benign `no_heartbeat` — outreach IS a bootstrap-manifest entry (`ok` = scheduler
  *constructed*, not running), so it is explicitly exempted from the started-silent
  `never_started` inference (a constructed-but-not-started scheduler is benign; a genuine
  init *failure* still surfaces); a *wedged-but-alive* loop is job_health's domain. The
  new `subsystem_stale:outreach` id is handled generically by the existing consumers
  (morning-report dedup by prefix, the Sentinel `subsystem_stale:` disposition).

- **Session-start surface for age-stale open PRs.** The repo-pulse worker now
  also caches the open-PR set each boundary, and a SessionStart hook lists the
  ones idle past a threshold (default 7 days) as one passive inline line —
  `[Open PRs] 3 open PRs idle ≥7d — #1379 (12d) · #1223 (12d, dependabot) …`.
  Visibility only: no CI/review state, never "ready to merge", no Telegram, no
  follow-up rows, no auto-merge. Age-based by design (no `reviewDecision`/status
  reducer — those carry no signal for owner PRs that sit at `REVIEW_REQUIRED`
  forever). Levers under the `repo_pulse` settings domain (`open_pr_enabled`,
  `open_pr_stale_days`, `max_open_prs`, `open_pr_resurface_days`,
  `open_pr_max_surface`); `GENESIS_REPO_PULSE_DISABLED` / `enabled: false` stop it.
  Robustness (review round): the fetch now sorts `sort:updated-asc` so a capped
  window (>`max_open_prs` open PRs) keeps the STALEST end — the lane's target —
  instead of gh's newest-first default that would drop aged PRs indefinitely; the
  cache/seen sidecars anchor on `genesis_home()` (honors `GENESIS_HOME`) so a
  relocated install keeps writer + reader in sync; and the hook's
  `GENESIS_REPO_PULSE_DISABLED` gate matches the exact `1` the worker honors (a
  looser truthy set would half-disable the subsystem). `mode: off` / `enabled:
  false` are full-worker stops (all lanes); `open_pr_enabled` is the lane-only knob.
  The surface's freshness TTL now derives from `min_interval_minutes` (2×, 1-day
  floor) so a large debounce (≥1 day) can't expire the cache before the worker is
  allowed to refresh it and silently suppress the surface for the whole window.

- **The Contributor Work-Log can post curated newcomer issues autonomously
  (opt-in).** A new `require_approval: false` lever lets the curator's
  privacy-vetted issues post without a per-item approval prompt — Genesis is the
  gate (the fail-closed privacy scan still runs on every draft, and the row never
  surfaces as an approval request). A cautious-rollout `max_posts_per_day` cap
  limits how many issues post per rolling 24h, so a bad batch surfaces one at a
  time rather than all at once; `mode: off` (or `GENESIS_CONTRIBUTOR_WORKLOG_DISABLED`)
  halts posting — re-checked immediately before each create — and freezes the queue.
  Ships SAFE — `require_approval` defaults true, and it is overlay-file-only (not a
  one-call settings/dashboard toggle), so a fresh install always requires human approval.

- **A per-prompt nudge when a session's memory MCP is running stale code.** Each
  Claude Code session's MCP subprocesses snapshot their code at spawn and never
  reload, so a deploy landing mid-session leaves recall — and its current security
  read-exclusions — on the old code until the session restarts (there is no
  auto-restart). The UserPromptSubmit hook now emits a one-line nudge when this
  session's MCP predates the last successful deploy, reusing the exact
  `commit_identity.is_stale` verdict the dashboard stale-code badge uses (a session
  *ahead* of the deploy, e.g. a manual `git pull`, is never flagged). Throttled per
  session and fail-open: a fresh session stays silent, and any read/parse miss emits
  nothing.

- **Page-top backup-health banner (server-authoritative).** The dashboard now
  surfaces a page-top banner when backups need attention — unconfigured, never run,
  last run failed, timer stopped, overdue, replication incomplete, or an unreadable
  status record — and stays hidden when healthy. The health verdict
  (`{state, code, reason}`) is computed once on the server in
  `routes/backup.py::_backup_health` and added to `/api/genesis/backup/status`; the
  client only renders it. Computing it server-side removes the client-side
  fetch-coordination and clock-skew failure modes entirely, keys "configured" and
  the off-site-incomplete check on current signals (a real `.git` clone, the
  resolved Tier-2 backend) rather than a stale status record, treats a malformed
  status record as unreadable instead of healthy, and does not false-flag a valid
  custom schedule as overdue. The failure reason rendered on the (unauthenticated)
  status route is sanitized to strip home-directory paths.

- **One-click "lobby" terminal door — reattach the whole CC fleet after a client
  reboot.** `generate-ssh-config.sh` now emits a dedicated `Host <host>-lobby`
  block (placed ahead of the numeric-slot wildcard, since ssh takes the first
  matching `RemoteCommand`) that attaches a stable `lobby` tmux session on the
  same socket as the `cc-*` slots and opens straight into the session picker
  (`choose-tree`). One reconnect brings every live session back — they persist in
  tmux on the box, only the client died — pick any slot and jump in (`Ctrl-b s`
  reopens the picker). All emitted blocks are keyed on the stable Tailscale IP
  (not the MagicDNS name), so they keep resolving even when the client's DNS is
  disrupted. `lobby` is not a `cc-N` name, so it never consumes a slot or trips
  the slot cap, and numeric-slot routing through `cc-slot.sh` is unchanged. Pair
  with one Windows Terminal shortcut (`wt.exe ssh <host>-lobby`) for a
  one-double-click return to the whole fleet.

- **Claude Code session exits are recorded.** When a CC session's process exits
  — a clean quit, a crash, or an OS/kill signal — its exit status (with a
  signal-decoded hint) and a tail of the terminal are written to
  `~/.genesis/logs/cc_exit_<slot>.log`, so a session that vanishes is
  diagnosable instead of leaving no trace. Self-rotating; no effect on the
  session lifecycle.

- **cgroup OOM kills are captured.** The temp watchdog now records any new
  out-of-memory kill in the container cgroup (timestamp, memory state, top
  processes) to `~/.genesis/logs/oom_events.log` and alerts once — turning a
  previously invisible cause of vanished processes into a durable signal.
  Degrades to a no-op on hosts without the cgroup-v2 interface.

- **Telegram DM "scroll-up".** Sessions can now read the conversation archive
  on demand: `conversation_history` accepts `chat_id` (scoped to one chat) and
  `before` (page arbitrarily far back, full-length messages), every fresh
  telegram session is told its own chat id, and the session-recovery recap is
  character-budgeted and tail-biased so the END of long replies (option lists,
  conclusions) survives instead of being cut at 300 characters.

- **Firecrawl as an explicit paid escalation backend.**
  `web_fetch(url, backend="firecrawl")` / `web_search(query,
  backend="firecrawl")` reach the Firecrawl cloud API (needs
  `FIRECRAWL_API_KEY`) — including from Bash-less background sessions. Never
  part of the automatic chains: it burns credits only when you ask for it.

- **Claude Code login-expiry warning + background fallback.** The interactive
  claude.ai login's refresh token has a fixed lifetime that routine use does
  not extend; Genesis now warns via Telegram days ahead
  (`GENESIS_CC_LOGIN_EXPIRY_WARN_DAYS`, default 5), and — if the operator has
  stored a 1-year `claude setup-token` via `scripts/store_cc_token.sh` —
  background sessions fall back to it when the login is confirmed dead (never
  over a working login).

- **Settings writes are provenance-stamped, and disabling the approval gate
  requires confirmation.** Every settings write records `# set-by: <actor> @
  <time>` in the overlay file so a deliberate operator choice is never
  mistaken for drift, and setting `manual_approval_required: false` now needs
  an explicit confirmation flag and announces itself via Telegram.

- **Opt-in career-outreach monitor (off by default).** A new daily monitor that,
  once enabled, drives a configured external career-agent module to stage
  first-touch outreach drafts into your mail Drafts, then sends you a single
  Telegram nudge to review and send them — it never sends mail itself (you click
  Send). Each auto-run drives the career-agent's OWN accuracy/verification gate
  end-to-end, so a draft is staged only if it passes that gate; a draft that
  can't be verified is skipped, not staged. Ships `off`; flip
  `off → observe → live` in `career_outreach.yaml` (env kill switch:
  `GENESIS_CAREER_OUTREACH_DISABLED=1`). No-ops cleanly on installs that don't
  have a career-agent module configured.

- **cc-tmp blast-radius isolation now converges on its own.** The dedicated,
  size-capped volume that stops a runaway temp write from filling the container
  root — and taking every Claude Code session down with it — used to attach only
  during a guardian redeploy that happened to land while no CC session was live,
  which on a busy install is almost never. A cold-start apply (before the server
  starts) plus a periodic retry timer now keep trying until a quiet moment is
  found, and the infrastructure-posture alert tells "converging" apart from
  "needs attention" so it doesn't nag when it's simply waiting. No action needed;
  a deliberate container restart closes it immediately.

- **GitHub steward now surfaces responses to your upstream contributions.** Beyond
  the flagship-repo deep-poll, the account-activity monitor gained an account-level
  notifications lane: it pings you when someone @mentions you on any repo, or
  responds on an issue/PR you filed on another project's repo (your outbound
  contributions — the flagship deep-poll can't see those). Tunable via the
  `notifications` reason-allowlist in `github_steward.yaml`; respects the same
  `off`/`observe`/`live` lever and pings immediately in `live`.

- **Contributor Work-Log — a curated supply of newcomer-friendly public issues.**
  Genesis can now turn items from its own backlog or a codebase scan into public
  GitHub issues for contributors to pick up — but never on its own say-so. Each
  draft is sanitized server-side (a fail-closed scan of the title, body, *and*
  labels blocks anything that looks like a private address, path, or secret) and
  then held for your per-item approval on the dashboard; batch "approve all"
  deliberately skips these, so every public post is an individual decision. It
  ships in `propose_only` mode: drafts are proposed and approved but never
  actually posted (dry-run) until you flip `mode: live` in a
  `config/contributor_worklog.local.yaml` overlay. Once live, an approved draft
  posts via `gh issue create`, de-duplicated against already-open issues so a
  retry can't double-post. Kill switch: `GENESIS_CONTRIBUTOR_WORKLOG_DISABLED=1`.

- **GitHub activity notifications.** Genesis now watches your active GitHub repos
  every couple of hours and pings you on Telegram when an *external* contributor
  opens their first PR/issue, comments, starts a discussion, or replies on one —
  filtering out your own activity, bots, and CI so you only hear about real
  people. It keys on when things were actually *created*, so an old issue that's
  merely edited or closed never masquerades as new activity; and if a ping can't
  be delivered, it's retried until it lands rather than lost. Off by default on a
  fresh install (it baselines quietly first); enable pings by setting
  `mode: live` in a `config/github_steward.local.yaml` overlay once it has run.
  Kill switch: `GENESIS_GITHUB_STEWARD_DISABLED=1`. A companion campaign
  pre-check (`github_activity_pending`) lets an opt-in digest campaign batch the
  non-urgent activity into a periodic Telegram summary while only spending on
  ticks that actually have new activity — quiet windows spawn nothing.

- **The operations ego stays out of development work.** Genesis's COO ego no
  longer proposes Genesis-development tasks (reviewing pull requests, scoping
  refactors, patch-planning) — those are deterministically moved to the tabled
  lane instead of reaching your approval queue, while genuine operational
  diagnosis (a failing backup, a stuck provider) still flows normally. When you
  later choose to unlock self-development, one config flag
  (`genesis_self_development_enabled`) turns the guardrail off.

- **One proposal queue of 15, not 15 per ego.** The pending-proposal cap now
  counts both egos together, so the approval board can no longer grow to ~30
  items.

- **Stale proposals get premise re-checks.** Older pending proposals were
  invisible to the premise-revalidation cadence (the field was never
  backfilled) and re-validated items stayed flagged as overdue forever; both
  are fixed, so dead proposals stop lingering on the board.

- **Every ego investigation names its deliverable.** Investigation dispatches
  from the operations ego now always carry a concrete output file that
  post-dispatch verification checks, and both dispatch paths tell the session
  exactly where to write it.

- **Real spreadsheets and polished decks from the deliverable builder.** When the
  optional OfficeCLI tool is present, Genesis now produces genuine `.xlsx` files
  (with working formulas and cell formatting) and higher-fidelity `.pptx` decks,
  instead of falling back to CSV or a basic pandoc slide export. It renders,
  screenshots the result to check the layout, and catches broken or stale formulas
  before the file reaches you. The tool is installed automatically during setup;
  if it is unavailable, the builder degrades cleanly to the previous CSV/pandoc path.

- **Stale-code guard for procedure storage after a deploy.** A Claude Code
  session loads its Genesis tools once when it starts and keeps running that
  version until it restarts, so a fix shipped by a deploy stays dormant in an
  already-open session. For the one tool that rewrites a learned procedure by
  similarity match, acting on outdated matching logic could silently overwrite
  the wrong procedure. Genesis now detects that a deploy landed after the session
  started and blocks that tool with a clear "restart this session" message until
  you do. Read tools and append-only writes are unaffected. Tunable via the
  `mcp_staleness_guard` setting (`block` default, `warn`, or `off`).

- **See which Claude Code sessions are running pre-deploy code.** The dashboard
  CC Sessions view now shows a "stale — restart" badge on any live session whose
  loaded tool code is older than the current deploy, so you can tell at a glance
  which sessions to restart to pick up the latest fixes (a session keeps its
  startup code until you restart it). The check compares the session's actual
  loaded commit against the deployed commit and only flags sessions that are
  genuinely BEHIND it — so a session already on the current code, one started
  mid-deploy, or one ahead of the last recorded deploy is never falsely flagged.
  Passive and advisory only; a session with no recorded commit shows nothing
  rather than guessing.

- **First-run setup wizard on the dashboard.** A new install can go from a fresh
  dashboard to a working, connected Genesis without touching a terminal. A
  dismissible Setup card on the Overview tab walks you through setting a dashboard
  password, adding a model or embedding API key — with a live "test this key"
  button that makes a real call so you know it works before saving — and writing
  your identity profile (USER.md), which can now be created straight from the web.
  The card reflects a live **readiness floor** — a genuinely usable Genesis needs
  Claude Code logged in, at least one chat/routing key, and at least one embedding
  key — so it shows honestly what's still missing (including a reminder to run
  `claude login` in a terminal, which the web UI can't do for you) rather than ever
  claiming "done" over an install that can't yet think. A banner also nudges you to
  set a password whenever the dashboard is open without one.

- **Genesis's skills and tools are now portable to other AI coding tools.** A
  new generator writes an auto-maintained inventory of Genesis's skills and
  action tools into `AGENTS.md` — the cross-tool entry point that Cursor,
  Codex, and other agent runtimes read — so Genesis's capability layer travels
  with the repo instead of being locked to one harness. The inventory is
  body-scope only: it lists what Genesis can *do*, and deliberately excludes
  its memory and cognition. Refresh it any time with
  `python scripts/export_agents_md.py`.

- **Memory self-healing is now on by default, and deletes survive outages.**
  Two upgrades complete the memory-repair story: (1) when a memory delete
  can't finish because the vector store is unreachable, the intent is now
  recorded as a durable "tombstone" — the nightly repair job re-attempts the
  delete until it completes, and nothing will rebuild that memory's vector in
  the meantime; (2) repair is now safe against deletes from every Genesis
  process (not just the main server), so the nightly reconcile job runs by
  default (`memory_integrity` mode `active`). Set `mode: passive` in your
  local overlay for detection-only. Recall also stops surfacing "ghost"
  leftovers: results whose backing record was deleted are filtered out of
  vector search and ambient core-facts instead of reappearing until the
  nightly sweep. (This treats any vector without a metadata record as a
  ghost — including pre-metadata-era legacy vectors on long-lived installs;
  those become invisible to semantic recall and are removed by the nightly
  repair with their contents exported first.)

- **Genesis can now repair memory drift automatically (opt-in).** Building on
  the Memory Integrity checks (which detect drift between the memory database
  and the vector store) and the one-time startup cleanup, a nightly repair job
  fixes any drift that reappears: orphaned "ghost" vectors are removed (their
  contents exported to `~/.genesis/output/` first as a safety net), and
  memories that silently lost their vector are re-queued so full semantic
  search is restored. Enable it by setting `memory_integrity` mode to `active`
  (it becomes the default in an upcoming release, once repairs are fully
  serialized against deletes from every Genesis process);
  `GENESIS_MEMORY_INTEGRITY_DISABLED=1` turns the memory-integrity jobs off
  entirely. Every repair run is recorded, so what changed is always auditable.

- **Talk to Genesis by voice to remember things and set reminders.** When
  enabled, you can tell the voice assistant "remember I prefer morning meetings"
  and it stores that for later, or "remind me to call the plumber Thursday at
  9am" and it delivers the reminder to you at that time. Ships off by default —
  turn it on with the `voice_act` setting (or `GENESIS_VOICE_ACT_DISABLED` to
  force it off). Asking the voice assistant about your past and memories is
  unchanged and always available.

- **Genesis now notices when its own memory quietly degrades.** Memory is stored
  across three backends that have to agree; when they silently drift — a memory
  that still exists but has become unfindable by search, or a leftover vector
  with no memory behind it — recall just gets quietly worse, with no error and
  no signal. Genesis now runs two nightly read-only checks: one verifies the
  three stores still agree, the other replays a set of known good recalls and
  watches whether their quality slips over time. If either degrades, it raises a
  standing alert and shows the state on a new Memory Integrity tile in the
  dashboard, instead of the rot staying invisible. Detection-only for now
  (it reports; it does not yet repair), read-only, and off-peak. Controlled by
  the `memory_integrity` setting (`off` / `passive` / `active`, default
  `passive`); the recall check stays quiet until you seed it a few known
  query→memory pairs (`scripts/seed_recall_golden_set.py`).

- **When a background job fails, Genesis now records what actually broke.** The
  scheduled jobs that quietly keep Genesis healthy — pruning, sweeps, harvests,
  reconnaissance — used to log a failure with the details thrown away, leaving a
  blank error behind. They now capture the real exception type and a compact
  traceback, so a recurring internal bug becomes diagnosable instead of
  invisible. The same failures are now surfaced onto Genesis's internal event
  stream, laying the groundwork for it to detect and eventually help fix its own
  bugs — kept deliberately dormant for now (it observes, it does not act).

- **An interrupted request no longer vanishes silently.** If a message you sent
  on Telegram was cut off before Genesis finished answering — a crash, a
  restart, or the session going dark mid-turn — Genesis now notices the
  abandoned session on its next hygiene pass and tells you it was interrupted so
  you can re-send, instead of leaving you waiting on a reply that never comes.
  Configurable via `cc_foreground_reaper` (`off` / `observe` / `notify`, default
  `notify`); observability-only — it never silently re-runs the lost work.

- **Ambient capture now alerts when auto-recovery can't bring a wedged device
  back.** For installs running the optional voice/ambient edge with device
  auto-recovery armed, Genesis now raises a capture-health alert when the device
  has been dark for hours *and* the automatic reboots failed to restore it — the
  one case that means a real fault rather than a device simply being switched off.
  Installs without auto-recovery are unchanged: a merely-absent device still never
  alerts (nothing on the network distinguishes "unplugged" from "crashed", so the
  signal fires only where arming recovery already asserts the device should be up).
  The alert names how long it's been dark and how many reboot attempts failed, with
  no device address or key ever included.

- **When Genesis hands a long task off to run in the background and says "I'll
  report back," it now actually does.** Ask for something heavy from Telegram — a
  deep research run, a long analysis — and Genesis can dispatch it to a background
  session and deliver the finished result straight back into the same
  conversation (your DM or the same forum topic) when it's done, success or
  failure. Previously a background task's successful result went nowhere: it was
  saved to the database but never sent, so a promised "I'll get back to you"
  silently never arrived (the cause behind a research request on 2026-07-20 that
  vanished). Long results arrive as a short summary plus a saved file rather than
  a wall of messages. Failures are reported in the same thread instead of
  disappearing.

- **When Genesis hits a usage/rate limit, your request comes back on its own.**
  Previously, hitting the limit dropped the work — and the message you got even
  claimed "background tasks will resume automatically" when nothing would. Now
  the request is saved the moment the limit is hit, and Genesis automatically
  picks it back up once the limit resets and delivers the answer to the same
  conversation. If capacity keeps being exhausted it backs off and eventually
  tells you it needs a hand rather than retrying forever. (This closes the
  other half of the 2026-07-20 disappearance.)

- **Genesis's tunable cognition knobs now live in one auditable file.** Three
  cognitive parameters — awareness signal weights, depth thresholds, and the
  memory-activation blend — form a closed "learned knob" registry
  (`config/learned_knobs.yaml` documents it; learned values live in
  `~/.genesis/config/learned_knobs.local.yaml`). Every change goes through the
  cognitive ledger (pre-image capture, drift-guarded rollback, visible in the
  dashboard) with hard bounds: ≤5% per step, ≤±20% total from baseline.
  Nothing adjusts autonomously yet — this is the substrate; the evidence-gated
  proposer arrives once calibration data covers these domains.

- **Ego proposals now carry their track record.** When Genesis's ego proposes
  an action, the proposal digest can show how proposals of that type have
  actually fared: a domain with a healthy graded history whose stated
  confidence runs >15 points hot gets a badge ("⚖ stated 0.90 → track record
  0.62 (n=41)"), and a domain with too little graded history gets an explicit
  "escalate — track record not yet trustworthy" note instead of a number.
  Annotation-only by default (`ws2_ledger.arbitration: shadow`); an optional
  `enforce` mode lets the track record drive the digest ordering. A proposal
  is never hidden — annotations inform, the user decides.

- **The `calibration_status` view now includes earn-back evidence.** For any
  autonomy category running below its earned level, the tool surfaces the
  recent graded evidence (windowed successes/corrections + Bayesian posterior)
  so "has it re-earned that level?" is answerable from mechanically graded
  data instead of judgment.

- **Genesis now screens its own skill self-edits for degradation.** When the
  skill-evolution loop rewrites one of Genesis's own skill files, a new Critic
  reads the change and flags the classic self-modification failure modes —
  quietly dropping a safety guard or scope limit, forgetting existing
  capability, over-narrowing the skill to a single case, or gaming its own
  success metric. It runs in shadow first: it records a verdict (surfaced when
  it flags something) without blocking the edit, so the pattern can be watched
  before the screen is ever given veto power. Tunable via the new
  `skill_evolution_gate` setting (`off` | `shadow`) with a
  `GENESIS_SKILL_EVOLUTION_GATE_OFF` kill switch.

- **Genesis can now regression-test a skill edit by actually re-running it.**
  Beyond reading the diff, Genesis can replay a frozen set of real tasks against
  both the old and the new version of a skill and compare how well each does —
  promoting the change only when it shows zero regressions and a real
  improvement. Like the diff screen it starts in shadow: it records a
  `net_positive` / `regression` / `inconclusive` verdict (surfaced when it finds
  a regression) and never blocks or applies an edit. It runs on demand via the
  new `skill_replay_run` tool against a per-skill task suite you author with
  `python -m genesis.eval.skill_golden_set`, and shares the
  `skill_evolution_gate` setting (a new `replay` section) and kill switch.

- **Voice conversations now feed real memory extraction (W0.5).** S2S voice
  conversations used to land in episodic memory as one growing raw blob per
  session close — duplicated on replays, never mined for facts. They now
  land as per-conversation transcripts (CC-transcript format, under
  `~/.genesis/voice-transcripts/`, outside the Claude Code resume picker)
  that the existing memory extraction job mines incrementally like every
  other channel: facts, references, and a topic index instead of raw dumps.
  A new authenticated `POST /v1/voice/conversation` endpoint lets a voice
  edge machine deliver its conversation turns with replay-safe, idempotent
  append semantics (double-fires and cumulative re-sends land exactly once,
  and turns are durable before the endpoint acknowledges). Voice transcripts
  are kept for 1 year (daily prune; an in-progress conversation is never
  touched), and voice conversation rows are excluded from the Claude Code
  session budget and the ego's capability self-model — they are
  conversations, not work sessions.

- **Genesis now keeps an honest, mechanical scorecard of its own confidence.**
  The cognitive ledger's graded predictions roll up into a unified calibration
  table (`calibration_cells`, migration 0069), recomputed after every grading
  pass: per domain/metric/window, how often Genesis said X% and how often it
  was actually right — Brier score, Murphy decomposition (is the miss
  overconfidence or uninformativeness?), and a shrinkage-stabilized track
  record, plus per-tool success base rates. Cold-start honesty is built in:
  cells with thin or no data say "unverified — escalate to the user" and
  never render as a bare percentage, on any surface. Explore it via the new
  `calibration_status` MCP tool, a dashboard **Calibration** tab (with the
  mechanical-vs-LLM grading shares), and Deep/Strategic reflections now read
  their "when you report ~80% you're right ~60%" advisory from this real
  graded record instead of the legacy proto-calibration table.

- **Voice graduation door (W0).** The core now exposes an authenticated
  `POST /v1/voice/graduate` endpoint where a voice edge machine can land
  typed "graduation" events (synthesized claims from ambient/meeting
  capture — never raw transcripts). Events are quarantined verbatim in a
  new `graduation_events` table with idempotent delivery (safe edge
  retries), and nothing consumes them yet — the policy drainer that routes
  them into memory arrives in a later phase. Dispositioned events are
  pruned after 90 days; pending events are never pruned. The memory
  metadata schema also gains dormant provenance/trust columns
  (`provenance_class`, `trust_level`, `attribution`, `origin_ref`,
  `capture_clarity`) for that later phase.

- **A runaway Claude Code temp write can no longer fill the container's root
  disk.** `~/.genesis/cc-tmp` — the scratch space every CC session (and
  genesis-server) writes to — now lives on its own size-capped incus storage
  volume. A runaway temp write fills only that volume; the container root
  filesystem is never touched (a full rootfs would otherwise kill every CC
  session). It also guards the reverse case: unrelated rootfs growth can no
  longer starve cc-tmp below the watchdog's floor. Applied automatically on
  fresh installs (host-setup) and on existing installs (guardian redeploy, when
  no CC session is live so a running session is never disturbed); size defaults
  to 2 GiB, override with `CCTMPVOL_SIZE_GIB`. Requires a block/CoW-backed
  storage pool (lvm/zfs/btrfs/ceph — what the default install uses); a dir-backed
  pool can't enforce the cap on its own device, so it's skipped rather than
  giving a cosmetic guarantee. A new infrastructure-posture check raises a
  standing alert on any container install where cc-tmp is not yet isolated, and
  `scripts/lib/cc_tmp_volume.sh`'s `cc_tmp_volume_remove` reverts it.

- **Autonomy stops grading its own homework.** Genesis's autonomy earn-back
  evidence used to be fed by the LLM classifier's own verdict on each
  interaction — it decided it "succeeded", and that counted toward earning more
  autonomy (a system grading itself). That self-grade feed is removed. In its
  place the mechanical grader feeds earn-back evidence from *actually completed
  vs. actually failed* autonomous tasks, and only a genuine failure counts
  against a category — a task that was merely slow or got cancelled never does.
  Ships in shadow mode by default (it logs what it would record, changes no
  autonomy levels); an operator flips it to live via the new `ws2_ledger`
  setting after watching the real pattern.

- **Those predictions now get graded.** A mechanical grader runs twice daily
  and settles every prediction whose deadline has passed — reading the actual
  outcome straight from the system's own records (did the reply arrive, did the
  task complete, did the job run clean), with zero LLM calls on that path. Each
  prediction gets a real accuracy score, so the system's confidence starts
  being measured against what actually happened instead of never being checked.
  A prediction that references a metric no longer in the code, or whose grader
  hits an error, surfaces as a health alert rather than silently rotting.

- **Every significant action now commits with a falsifiable prediction.**
  Outreach sends, autonomous task claims, build-lane verdicts, and ego
  proposals each write their prediction rows the moment they commit — code
  in the commit path, not discipline. Priors seed from measured base rates
  (a reply prediction starts at the real ~2%, not an optimistic 50%), stated
  confidence threads through where it genuinely exists (task submissions
  gained an optional confidence field), and a failing hook can never block
  or slow the action itself — failures surface as a health alert instead.
  Task completions and failures also now land on the outcome bus in real
  time (the first live emits), instead of waiting for the twice-daily
  harvester.

- **Your decisions now stick.** When you reject a proposal with a reason —
  from Telegram, the dashboard, the chat tab, or in conversation — the ruling
  is captured as a durable **Settled Decision** that the ego sees in every
  future cycle and may not re-propose, re-litigate, or work around. Repeat
  rulings on the same theme reaffirm the existing decision instead of piling
  up duplicates; only you can supersede one. Previously each entry point
  recorded a different subset of side effects (the dashboard recorded almost
  nothing), so the ego could agree with you in chat and then re-propose the
  same thing days later. A new `ego_decision` tool also captures rulings you
  state directly in conversation.

- **Your deny reasons finally count as engagement.** The engagement signal
  now covers all Genesis outbound — outreach messages *and* ego proposals —
  and a typed reason on a proposal counts as engagement. The system will no
  longer claim "the user doesn't engage" while you're actively ruling on its
  proposals. The dashboard reject flow nudges for a reason ("Why? This
  teaches the ego — a reason becomes a standing rule"), which stays optional.

- **Groundwork: Genesis can now measure whether its entity graph would improve
  recall, without changing any results yet.** A new shadow-only lane resolves a
  recall query to entities in its knowledge graph, walks their relationships,
  and records how many new, still-valid memories it would have surfaced that
  ordinary search missed. This is measurement only (one internal metric per
  recall); it ships off by default and never alters what recall returns, so the
  data can decide whether building the live version is worth it.

- **Genesis now tidies near-duplicate entities in its knowledge graph.** When
  it learns about a "thing" (a project, tool, concept, person) whose name is
  very close to one it already knows, it now decides whether they are the same
  thing or genuinely different, and can merge the duplicates so its memory of
  you stays coherent instead of fragmenting across "neural monitor" /
  "neural-monitor" / "neural_monitor". Two independent models must agree before
  anything merges, and pairs that only differ by a number (`PR #989` vs
  `PR #990`) are never merged. It ships in **shadow mode by default** — it
  records what it *would* merge without touching anything, so you can review the
  proposals first; flip it to live with
  `settings_update("entity_adjudication", {"mode": "live"})` (or turn it off
  entirely). A background sweep also reconciles the entities it already had.

- **Genesis now watches whether the host machine's clock is actually being
  kept in sync.** The container shares its host's clock, so if the host's
  time-sync daemon dies or stops syncing, TLS handshakes, log timestamps, and
  scheduled jobs all quietly skew — and nothing noticed. The infrastructure
  profile's host plane now reports which NTP daemon runs the clock and a
  composite health state (synced / degraded / unsynced) that catches a dead
  daemon even while the kernel's own sync flag still claims all is well; a
  state flip surfaces as an infrastructure-drift observation like any other
  change to the machine's vitals.

- **Genesis now writes down its predictions before acting — the substrate.**
  A new cognitive ledger stores falsifiable predictions ("this outreach will
  get a reply within 72 hours, confidence 0.02") behind a hard validation
  gate: a prediction that doesn't name a registered, mechanically-checkable
  metric with a deadline literally cannot be written. Nine starter metrics
  ship with their grading rules implemented and tested (replies, task
  completion, clean job days, build greenlights, ego-proposal execution).
  This is the foundation; the hooks that write predictions on every action
  and the grader that scores them land next.

- **An unprotected box now tells you.** If a crash-resilience protection is
  missing on your install — container swap disabled, systemd-oomd
  pressure-kill not configured, no host swap, the container's swap allowance
  switched off, or (on a systemd-networkd–managed box) the network
  address-retention or the self-healing networkd watchdog missing — Genesis
  now raises a standing alert (dashboard + morning report) naming what's
  missing and how to fix it, and clears it automatically once the protection
  is restored. The network checks stay silent on NetworkManager installs,
  where they don't apply. Previously a box that was *always* unprotected
  produced no signal at all; only a *change* was detected. If the
  infrastructure self-profile stops refreshing (>3 days old), you get a
  distinct "posture unknown" alert instead of stale claims.

- **Genesis can now take a hypervisor backup of its own host VM — and grows
  finally get their safety net.** The provisioning gate has always wanted a
  recent backup before an irreversible grow, but nothing could take one, so
  the check sat disabled. Now a stale backup turns a grow proposal into a
  backup→verify→grow chain under one clearly-worded approval: the backup
  starts immediately (vzdump, on a third API token that can back up but never
  resize), verification runs in the background for however long the dump
  takes, old backups rotate away automatically (keep-last, configurable), and
  the grow executes only after the backup verifies and a fresh safety
  re-check passes. Backups can also be taken on demand (`provision_vzdump`),
  have their own weekly budget separate from grows (note: the existing
  `max_actions_per_week` cap now counts grows only), and an interrupted
  verification resumes after a restart with no state lost. Restore is
  deliberately not included — that is a destructive operation with its own
  upcoming review.

- **The guardian now keeps container swap enabled on its own.** Swap is what
  turns a memory spike into graceful slowdown instead of a machine-wedging
  thrash, but the setting only got applied when host setup ran — an install
  that just pulls code updates could sit unprotected indefinitely (observed
  live: a second install ran for weeks one memory spike away from the wedge).
  The guardian now re-checks the setting every tick and repairs both halves
  when drifted: the persistent config (so future restarts have it) and the
  live cgroup (so protection is immediate, no restart needed). Repairs page an
  INFO note; a repair that can't complete pages a warning at most daily. Hosts
  where swap-off is deliberate can opt out (`swap_reconcile_enabled: false`).

- **The dashboard has a Sessions cockpit.** A new Sessions tab shows every
  CC session next to what it's actually FOR: the immutable origin prompt it
  was born from, its living mission, the full TODO-ledger with status and
  evidence, a compaction-waypoint timeline, and the repo-pulse panel —
  "looks shipped by PR #N" proposals you can confirm (closes the item with
  the PR as evidence) or reject with one click. The overview card's
  quick-glance modal stays and links through to the full cockpit.

- **Your session TODO ledger now notices when a merged PR ships an item.** N
  parallel sessions each carry open ledger rows, and until now closing them
  meant remembering to do it by hand — work shipped in one session left stale
  "open" items in another. At session boundaries Genesis now checks recently
  merged PRs against every open ledger item: a PR that explicitly cites
  `Ledger: <item-id>` in its body closes the item automatically (with the PR
  recorded as evidence, and reversible); anything less certain — a bare id
  mention, or an AI-judged title/description match — becomes a small
  "looks shipped by PR #N — confirm or ignore" note in the session's charter
  block, never a silent close. Levers: settings domain `repo_pulse`
  (off / propose_only / live) and the `GENESIS_REPO_PULSE_DISABLED=1` kill
  switch.

- **Genesis now remembers how its background jobs actually ran, not just a
  running tally.** Until now each scheduled job kept only a single cumulative
  row, so a job that failed for a week and then recovered looked identical to
  one that never failed — the history was gone. Genesis now keeps a per-run
  record (with real durations where a job marks its own start), so a bad patch
  or an intermittent outage leaves a visible trace instead of vanishing into an
  average. It stays cheap on purpose: a healthy or a stuck high-frequency job is
  recorded at most hourly, so the log captures every distinct episode without
  drowning in routine ticks.

- **Alert history survives a restart.** The list of what's currently wrong used
  to live only in memory and reset every time Genesis restarted, so an incident
  that opened and closed while you weren't looking left no record. Alerts are now
  written to a durable incident log — each one opened when it starts firing and
  stamped resolved when it clears — so you can see what happened overnight, not
  just what's broken right now.

- **Genesis notices when it stops learning about you.** The stream that updates
  Genesis's model of you from its own reflections had gone quiet for months with
  nothing flagging it. Genesis now watches that stream and raises a (non-paging)
  alert if it goes silent for more than two weeks, and clears it automatically
  once fresh learning resumes.

- **Data backfills now heal themselves on update.** Some upgrades need more than
  a schema change — they need existing data reshaped (e.g. tagging every stored
  memory's vector with its provenance class). Those backfills used to be
  one-off scripts each install had to remember to run by hand, so an install
  that skipped one silently drifted. Genesis now runs them automatically in the
  background after startup, tracked in a ledger so each runs exactly once, never
  blocks boot, and simply no-ops on an install that's already current — so a
  lagging clone catches up on its next update with nothing to run manually. The
  first one backfills provenance onto older memory vectors.

- **Legacy memories join the wing system.** ~2.9K memories stored before
  wing classification existed (most predating the user-work wings added in
  May) sat in `general/uncategorized`, invisible to wing-filtered recall.
  A new one-shot supervised backfill (`scripts/wing_backfill.py`) classifies
  them in two stages — the deterministic taxonomy layers first, then an LLM
  batch pass through a new `wing_backfill` routing call site for the rows
  keywords can't reach — and writes wing/room/life-domain to both SQLite
  metadata and the Qdrant payload (with revert-on-failure so the stores never
  diverge), making them reachable by wing-filtered recall. Dry-run by default;
  the bulk write is gated on a human-reviewed sample.

- **Genesis survives — and now auto-recovers from — a wedged network.** Under
  heavy memory pressure the container's networking daemon can hit kernel
  timeouts, drop its DHCP lease, and take the machine off the network until
  someone restarts the daemon by hand. Fresh and updated installs now (1) pin
  the address so a networking failure keeps the connection instead of dropping
  it, and (2) run a lightweight watchdog that detects a wedged/inactive
  networking daemon and restarts it automatically (address-preserving, so no
  blip). Heal events are recorded and surfaced in the infrastructure profile,
  so a recurring fault is visible instead of silent. Networking runs a
  different manager (e.g. NetworkManager) or lacks sudo? It skips cleanly.
  See `docs/reference/network-resilience.md`.

- **Genesis now notices when merged code isn't actually deployed.** Pulling
  changes with a bare `git merge` between updates loads the new code on
  restart but silently skips everything only `update.sh` activates — new
  systemd units, the host guardian redeploy, tool pins. One install ran six
  days like that with a shipped timer never installed and zero signal
  anywhere. The health snapshot now has a `deploy_health` section (days since
  the last update, commits behind, missing units, host-guardian drift — no
  network calls), and the awareness loop raises a dashboard alert on any
  drift, escalating to a Telegram page only when it's sustained (a week stale
  and 20+ commits behind, or a missing unit ignored for a day). The fix is
  always the same and the alert says so: run `scripts/update.sh`.

- **Recall now follows memory links — the benchmark-proven +12.6pp mechanism
  lands in production, shadow-first.** LongMemEval showed that appending
  1-hop linked neighbors to recalled results lifts answer accuracy from 64.8%
  to 77.4% (temporal reasoning +23pp, multi-session +17pp), but that expansion
  only existed in the eval harness. It's now wired into every MCP recall
  surface (full, compact previews, proactive injection), shipping in `shadow`
  mode: expansion is computed and measured (`eval_events`) without changing
  any output until the `memory_recall` settings domain flips
  `graph_expansion.mode` to `live` — no restart needed, and `off` is the
  instant kill switch. Expanded neighbors carry their stored provenance and
  pass the exact same injection defenses (wrap/count/drop) as organic
  results; `contradicts` links are never followed, and neighbors are flagged
  `via_graph` so callers can weigh them. The benchmark harness now calls this
  same production primitive, so future eval numbers measure shipped code.

- **A resumed conversation can no longer run twice at once.** If an SSH drop
  leaves a Claude Code session executing headless and you resume that same
  conversation elsewhere, both processes used to write to the same transcript
  and the same files simultaneously (Claude Code itself raises no warning).
  Now Genesis tracks which process owns each conversation: the newer session
  wins, the orphan's file-changing tools are blocked with instructions, the
  resuming session is told about the orphan at startup, and you get paged on
  Telegram within minutes. Intentional dual-sessions have a documented
  override. Bootstrap also hardens the root cause: sshd detects dead SSH
  clients in ~60s (instead of hours), and interactive `claude` launches are
  wrapped in a uniquely-named tmux session so a dropped connection leaves a
  reattachable session instead of an orphan (opt out:
  `GENESIS_NO_TMUX_WRAP=1`).

- **Genesis now notices the agreements a session forgot to write down — in
  shadow.** At every compaction boundary a detached worker re-reads the
  conversation since the last checkpoint and proposes missed "yes, do that"
  moments and direction pivots as session-ledger candidates. Proposals are
  only LOGGED for now (the live ledger is never touched): each one carries a
  verbatim quote that is deterministically checked against the transcript,
  and a precision report compares proposals against what was actually
  captured by hand — the safety net earns write access with data before it
  gets it. Levers: the `session_ledger_shadow` settings domain (off/shadow)
  and a `GENESIS_LEDGER_SHADOW_DISABLED=1` kill switch; a `--backfill` mode
  replays past sessions for tuning. Also fixes a charter bug where a session
  whose first message was a bare slash command (like `/compact`) could have
  recorded that command as its permanent origin.

- **Benchmark runs can now select exactly the arms they pay for.** The
  LongMemEval harness gained `--arms` (comma-separated labels, e.g.
  `--graph --arms raw,raw+graph`), so a paired baseline-vs-graph comparison
  no longer forces the full four-arm spend. Unknown labels fail fast with
  the selectable universe listed — a paid run never silently widens.

- **A memory spike now degrades gracefully instead of wedging the machine.**
  On systems that ran out of memory with no swap and no userspace OOM killer,
  one greedy process could drag the whole box into unrecoverable thrash —
  load in the hundreds, SSH dead, everything down together. Installs now set
  up systemd-oomd with pressure-percentage kill policies (adaptive to any
  machine size, applied automatically on bootstrap and on your next update,
  skipped cleanly where systemd/oomd/PSI aren't available), the Genesis
  server marks itself `avoid` so the greedy session tree dies first, managed
  container installs get swap enabled (`limits.memory.swap`), and setup warns
  with exact remediation when swap is missing or disabled. The infrastructure
  profile records the swap/oomd state as facts, so an unprotected install is
  flagged in `INFRASTRUCTURE.md`. Runbook: `docs/reference/memory-resilience.md`.

- **The CC Sessions card now tells the truth, and clicking it shows why.**
  The dashboard card used to show a DB-side "active" count that routinely
  disagreed with the processes actually running, plus a bare "3/20" that
  looked like a session count but was really the hourly rate budget. The card
  now shows both counts side by side ("N live / M db", amber when they
  diverge), labels the budget as a budget, and opens a per-session modal:
  every session from the last 24 hours with its live process (slot, pid,
  memory), its charter (mission, compactions, open ledger items), and
  explicit badges wherever the database and reality disagree — including
  live processes with no session row at all.

- **Sessions now keep a durable TODO ledger that survives compaction.** An
  agreement made mid-session ("yes, do that") used to live only in
  conversation — one compaction summary could quietly drop it. Sessions can
  now record agreements as ledger items the moment they happen; open items
  re-inject into every post-compaction window (with a per-turn
  `[Charter: … | open: N]` tag showing drift at a glance) until they're
  closed as done, absorbed elsewhere, or consciously dropped. Charters — the
  session's origin, a living mission line, and pointers to its governing
  docs — moved from per-session JSON files into the database so all of this
  is queryable; the human-readable `charter.md` mirror stays.

- **The memory benchmark can now measure whether Genesis's memory graph
  actually helps.** A new `--graph` mode runs every benchmark arm twice — once
  against a plain store and once against a store where memories link to
  similar earlier memories exactly as they do in production — and follows
  those links at recall time to pull in related memories the search itself
  missed. Baseline and graph runs use fully separate stores, so the
  comparison is honest (links can't quietly tint the baseline's ranking). Per
  question, the results record how many links formed and how much extra gold
  evidence the graph surfaced, and a graph run that formed no links says so
  loudly instead of silently matching its baseline. The memory linker's
  similarity threshold is also now configurable per instance instead of fixed.

- **The memory benchmark now grades temporal questions fairly and explains
  its misses.** The LongMemEval reader gets the question's date (the
  benchmark's own convention — without it, "how many weeks ago…" questions
  were unanswerable by construction), so temporal scores now measure memory,
  not a missing calendar. New `--dump-dir` writes per-question diagnostics
  (query, recalled memories, answer, verdict) for failure analysis, a new
  evidence-coverage metric shows *how much* of the gold evidence was
  retrieved (not just whether any was), and `--types` runs a single question
  category — so a targeted slice no longer costs a full 500-question run.

- **Claude Code sessions no longer forget what they were started for.** Long
  sessions compact their context many times, and each summary is biased toward
  recent work — after enough compactions a session can no longer connect
  "what's next" to its original goal. Now the first compaction snapshots the
  session's charter (the verbatim opening prompt) to disk, and every later
  window gets it re-injected automatically. The origin survives any number of
  compactions; `/clear` still means a genuinely fresh start. Charters live in
  `~/.genesis/sessions/<session-id>/charter.md`.

- **Provenance-based content isolation is now enforce-ready (running in
  observe-only mode).** Genesis already tags where every memory came from —
  its own thinking, your messages, or the outside world (a webpage, an
  ingested doc, an email). This work makes that tag follow the content all the
  way through recall, so Genesis can tell, at the moment content is about to
  enter a prompt, whether it is quoted outside material rather than a trusted
  instruction. The stored tag decides everywhere: outside material that landed
  in Genesis's own session memory (not just the knowledge base) is delimited
  and labeled external on every recall surface — proactive hints, voice
  responses, and the dashboard memory browser included — instead of reading as
  first-party memory. The protection is armed but watching, not acting: it records
  what it *would* block so the behavior can be verified against real traffic
  before it's switched on. When switched on (a live setting, instantly
  reversible), outside-world content is held back only from automatic,
  uninvited context in unsupervised background sessions — your explicit
  searches and everything in a normal foreground conversation keep working
  exactly as before. Held back means fully out of the loop: a blocked item
  also earns no retrieval credit, so it can't quietly climb the memory
  ranking through the very sessions that refuse it. If the guard ever fights
  legitimate activity, it stands itself down and pages you.

- **Genesis now notices when memories quietly lose semantic search.** When an
  embedding permanently fails, that memory becomes keyword-only — findable by
  exact words but invisible to meaning-based recall — and nothing flagged the
  pile building up (the existing alert only caught embeddings failing *right
  now*, not the backlog left behind). A new hourly check counts these
  permanently-stuck memories: a modest pile shows on the dashboard, and a large
  one (a real chunk of the store gone semantically dark) pages you on Telegram.
  It clears itself once the memories are re-embedded.

- **The recovery brain no longer drifts behind a version bump.** A nightly
  job re-aligns the host's Claude Code and Node.js to the pinned versions.
  Previously this happened only when you ran an update, so a pin bump could
  leave the host's autonomous-recovery Claude Code lagging for days. It heals
  only real drift, is a clean no-op when everything is already aligned, and a
  failed alignment now shows up as a failed timer instead of going quiet.

- **Genesis notices if systemd "linger" gets turned off.** Linger is what
  keeps Genesis's background services alive after you log out — if it is ever
  disabled, everything dies silently at the next logout. A new health check
  watches both the host and the container and raises a single alert (with the
  exact re-enable command) when linger is off, before the next logout can bite.

- **Code quality is now a measured series, not a feeling.** A new
  `dev_quality` dimension in the weekly eval snapshots tracks review
  findings per merged PR (by severity, harvested from the PR bots' inline
  comments every Sunday), open code-audit findings, and the edit-failure
  rate — so "the codebase is getting better" is a falsifiable trend on the
  dashboard, honest about small numbers (rates go null, not zero, when
  there's no data).

- **Editing a file now surfaces that subsystem's traps.** The first time a
  session touches each subsystem, its do-not-touch edges and easy-to-forget
  mechanisms from the architecture map are injected right at the edit — the
  invariants arrive while the code is being written instead of at review
  time.

- **The idle-time code auditor got a sharper brief.** Its instructions now
  use a four-class failure taxonomy tuned to AI-generated code (structural,
  async/state, error-handling, tests), it picks targets from real
  import-graph data (highest fan-in modules first), and findings carry a
  category. Also fixes a long-standing bug where the auditor could never
  report a critical-severity finding — its output schema simply didn't
  include the word.

- **Every memory now carries a provenance class — and immunity gets its kill
  switch before it gets its gates.** Everything Genesis stores is stamped
  `owner`, `first_party`, or `external_untrusted` at write time (your words /
  Genesis's own observations / content pulled off the world, including
  ingested documents), with all existing memories backfilled. This is the
  foundation for the WS-3 immunity gates: crafted external content becomes
  blockable from turning into procedures, identity edits, or autonomy
  evidence by *origin*, not content guessing. The control surface ships
  first: a live-editable `ws3_immunity` settings domain (master switch +
  per-gate off/shadow/enforce) that takes effect instantly with no restart —
  and owner/first-party content can never be blocked in any mode, by
  construction. No gates are active yet; they arrive in the next phase, in
  observe-only shadow mode.

- **Code reviews follow a written protocol instead of convention.** The
  architect reviewer now opens with a scope-drift check (did the change do
  what was asked — nothing more, nothing less), grades findings on an
  explicit BLOCKER / SHOULD-FIX / NOTE ladder, must quote the exact line
  motivating each finding or have the finding's confidence capped, consults
  prior review learnings before starting, and closes with an explicit
  completion status. Adapted from the gstack review framework.

- **The host Guardian now tells you before its AI recovery brain loses its
  login — and can survive it without you touching the host.** The Guardian's
  autonomous diagnosis runs on Claude Code authenticated by a one-time login at
  install, which never refreshes; if it died, the brain silently went dark and
  you'd only find out mid-incident. Genesis now watches that login's health and
  alerts you (over Telegram) if it goes dead, and warns ~30 days before a
  fallback token would expire. Optionally, mint a one-year token with `claude
  setup-token` from any machine and pipe it to `scripts/store_cc_token.sh` —
  Genesis syncs it to the host and uses it **only** as a fallback when the
  host's own login is dead, never overriding a working login. The token is a
  subscription token (not an API key), is stored 0600 and never logged, and the
  health signal sends only status booleans — never the token or your account
  details. You can set this up at install or just wait for the first alert.

- **`genesis eval bench` — a Genesis-vs-bare-Claude A/B benchmark you can run
  in one command.** Each task in a private task set runs through two arms: a
  cognition-enabled Genesis session (identity + read-only recall from your
  real memory) and a plain Claude Code session with zero Genesis context. An
  LLM judge grades both against success criteria written before the run, and
  the paired win-rate lands in the eval tables and a JSON report. Honesty is
  built in: the judge is labeled uncalibrated until it's validated against
  your own verdicts, small pilots print "insufficient data" instead of fake
  significance, and every run proves it wrote nothing to production memory —
  recall's own usage counters are suppressed for the run so benchmarking
  never distorts what your live Genesis considers important.

- **`genesis eval longmemeval` — measure Genesis's memory on a public
  benchmark.** Runs LongMemEval, an external long-term-memory QA benchmark:
  each question's prior-conversation "haystack" is loaded into a throwaway
  memory store built from scratch (so your real memory is never touched), then
  Genesis recalls and answers, and the standard gpt-4o judge grades it — giving
  a number you can compare to published results. It reports accuracy per
  question type and across two query styles (the raw question vs. keyword
  terms), so you can see how much question phrasing affects recall. One command
  against the public dataset; nothing about your own data leaves the machine.

- **The API Keys panel now shows how often each provider is causing fallbacks.**
  A provider that is misconfigured or failing — for example one whose key is
  missing yet is still wired in as the preferred choice on many call sites — used
  to read only "missing (fallback active)" with no sense of scale. Each provider
  row now shows "in N fallbacks (24h) · last …" beside its key status, so an
  ongoing fallback storm is visible at a glance instead of hiding in the logs.

- **Your credentials now survive losing the whole container.** Genesis mirrors
  its encrypted credential bundle (secrets, SSH keys including the guardian
  control-plane key, and Claude/GitHub credentials — all GPG-encrypted) onto the
  host, outside the container's blast radius, and the host guardian keeps a
  second copy the container can't touch. If the container is ever destroyed, a
  fresh one can be rebuilt with credentials intact from the host — no network and
  no chicken-and-egg (previously the only backup copies lived *inside* the
  container). The guardian also warns you if that mirror goes stale, so you find
  out backups have stopped landing *before* you need them. See the
  container-loss runbook in `docs/reference/recovery-and-portability-workflow.md`.

- **Genesis now detects and repairs corrupted credential files on its own.**
  If a critical credential or wiring file (`secrets.env`, your Claude Code and
  GitHub credentials, SSH keys, `guardian_remote.yaml`, `genesis.yaml`) gets
  zeroed, truncated, or otherwise corrupted — the exact kind of damage a storage
  outage can cause mid-write — Genesis notices on its next cycle, restores the
  file from your encrypted backup with the corrupt copy set aside for
  inspection, and alerts you (telling you to rotate anything that may have
  changed since the backup). It only ever acts on *proven* corruption, never on
  a file that is simply different, and it validates the decrypted backup before
  touching the original, so a bad backup can never make things worse. The
  host-side guardian is the backstop: it watches the same files and steps in to
  restore them if Genesis is too degraded to heal itself. Restores are
  rate-capped, and installs without backups configured still get the detection
  and alert. Requires the backup passphrase escrow shipped in the previous
  release. (Backup-failure alerts also reach Telegram now — they were being
  silently dropped on installs using the shipped `outreach.yaml`.)

- **Three new honesty-first metric series on the dashboard's compounding
  panel, plus retrieval precision@3.** The weekly eval now tracks how your
  approval gates actually get resolved (by you vs. auto-expired vs.
  fail-closed cancels — unknown resolver values are surfaced, never silently
  bucketed), goal completion (reports "no data yet" instead of a fake 0%
  until goals actually close), and a noise/passivity view (stale follow-ups,
  empty ego cycles, rejected-proposal clusters). Memory retrieval gains a
  precision@3 series alongside precision@5, with judge changes marked as
  series breaks. Trend arrows are now honest too: the arrow shows which way a
  metric moved, while its color shows whether that's an improvement — a
  rising noise metric no longer glows green.

- **Genesis can now grow this VM's disk or RAM from the hypervisor — with your
  approval — to fix the one storage failure nothing else could.** When the
  container's storage pool has no room left to auto-expand into (the structural
  cause of a full-pool outage), the only real fix is to add space at the
  Proxmox layer. Genesis can now propose that grow, ask you to APPROVE or DENY,
  and on approval grow the virtual disk and absorb it into the pool — or, if
  Genesis itself is down in the outage, the host-side guardian can do it as part
  of recovery. It is **off by default**, every change is approval-gated and
  rate-capped, grows are one-attempt/never-auto-retried, and only two
  read/write-split Proxmox tokens are ever stored. A grow that Proxmox accepts
  but then fails to carry out in the background (e.g. a storage-permission gap)
  is now reported as a clear failure rather than a vague "couldn't confirm."
  Setup and the full safety model: `docs/reference/proxmox-provisioning.md`.

- **A fresh session now recalls what's already in flight.** When you start an
  interactive session, its opening context now includes a terse snapshot of the
  current working state — active autonomy tasks, live git worktrees, and
  recently-touched plan files — so it can pick up in-progress threads instead of
  starting cold. It's framed as the session's own recollection, not a report to
  read back to you.

- **Backups now cover the credentials and wiring you'd need to actually rebuild —
  and the backup passphrase is escrowed so a lost secrets file can't lock you out
  of your own backup.** The encrypted backup set now includes your SSH keys, the
  GitHub and Claude Code credentials, and the host/network wiring config (the
  difference between "restore" and "reprovision from scratch"). Separately, the
  backup passphrase — which previously lived *only* inside the very secrets file
  the backup encrypts with it — is now escrowed to the host outside the container,
  so a secrets-file loss no longer leaves the encrypted backup undecryptable.
  Restore reads the escrowed passphrase automatically and stages recovered
  credentials to a review directory rather than overwriting live ones.

- **You now get one Telegram when a model provider has been dead for an hour.**
  Genesis already detected a provider failing every call and already showed it on
  the dashboard — but nothing ever told you. The record it wrote was
  high-priority, and only *critical* ones reach Telegram; the matching call-site
  alert is a warning, which the outreach path filters out. So a provider could be
  down for days while the only trace was a dashboard panel nobody was looking at.

  One message, then quiet: it names the provider and how long it has been failing,
  and does not repeat. The hour is deliberate — the underlying record is written
  after about ten minutes, which is right for a dashboard row and far too eager
  for a notification, since most breaker trips resolve themselves. If the provider
  genuinely recovers and later dies again, you are told again.

  Nothing new was added to receive it: this reuses the existing critical-record
  path, so there is no new alert type and no new table. The duration is read
  from the stored outage record rather than from memory, so it survives a
  restart mid-outage without re-notifying — and the hourly check is driven by
  Genesis's own 5-minute awareness tick rather than by provider traffic, so a
  provider that goes quiet after failing still gets reported. One new lever:
  the `provider_outage_notify` setting (`off` / `propose_only` / `live`,
  default `live`; env `GENESIS_PROVIDER_NOTIFY_DISABLED=1` forces off) —
  `propose_only` skips the immediate page (the record still shows on the dashboard, to the ego, and in the next morning report), and
  turning it off resolves any open notification so re-enabling tells you about
  a still-dead provider again.

### Changed

- **The session charter now lists every open ledger item, not just the oldest
  six.** The ledger is a curated list of one-line to-dos, and the old window
  meant a session with more than six open items never saw a newly added one in
  its own prompt — it existed only in the aggregate count. The list is now
  effectively unbounded (a 200-row ceiling with an explicit "more than 200 —
  the rest are not listed" note), and an oversized charter block degrades by
  dropping whole sections with a marker rather than cutting mid-bullet, with
  the open/closed count preserved.

- **Mistral Large is now tracked as a paid provider.** Mistral removed the Large
  model family from free-tier entitlement (unannounced; surfaces as
  `403 tier_not_allowed`). The `mistral-large-free` provider is now flagged
  `free: false`, so its spend is recorded at real rates ($0.5/$1.5 per MTok)
  instead of $0, and call sites marked `never_pays` no longer route to it. The
  provider name keeps its historical `-free` suffix to avoid churning the 30
  chains that reference it. If your account tier still gets Large at $0,
  override `free: true` in your local routing overlay.

- **The Claude Code model roster now ships infrastructure, not a preconfigured
  provider.** `config/cc_roster.yaml` previously shipped a `glm-5.2` peer pointed at
  `open.bigmodel.cn`, which requires Chinese real-name identity verification (实名认证)
  to buy a Coding Plan — so on any install outside China the documented rate-limit
  fallback could not be provisioned at all. The base config now ships only the native `claude` entry
  plus commented examples for both Z.AI platforms (`api.z.ai` international,
  `open.bigmodel.cn` China) and several other Anthropic-compatible providers.

  **If you were using the shipped peer, you must now declare it yourself** in
  `~/.genesis/config/cc_roster.local.yaml`, which is deep-merged over the base file
  and is where the `cc_roster` settings domain already writes:

  ```yaml
  models:
    glm-5.3:
      anthropic_base_url: "https://api.z.ai/api/anthropic"
      auth_env: ZAI_CODING_API_KEY
      model_id: glm-5.3
      failover_order: 1
  ```

  This matters because the failure is quiet FOR THE USER: an overlay setting
  `default: glm-5.2` with no matching entry falls back to native Claude, so a
  subscription-cap fallback you believed was configured would simply not engage.
  It is not silent in the logs (`apply_active` logs an error with a traceback),
  and the `cc_roster` settings domain rejects such a write outright — the quiet
  path is a hand-edited overlay.

  `secrets.env.example` now documents all three GLM key slots and which endpoint
  each one serves: a Coding Plan key (`ZAI_CODING_API_KEY`) is required for a roster peer
  because Claude Code speaks the Anthropic protocol, while a general/prepaid key
  (`ZHIPU_API_KEY`) works only on `/api/paas/v4`. Using the general key on a coding
  endpoint returns `1113 Insufficient balance` even when the account is funded.

  The `validated:` field is unchanged but now documented as advisory only: it is
  parsed into `RosterEntry.validated` and then acted on by nothing, so it gates
  nothing. Stale stamps were dropped rather than carried forward unverified.

- **Executor Gate 2 (`17_executor_review`) leads with paid DeepSeek V4-pro.**
  After the NIM repoint moved the free NIM tier from V4-pro to V4-flash, this
  deliverable-quality gate now leads with the paid `openrouter-deepseek-v4`
  (pro-grade) for maximum review quality, with free NIM flash + paid v4-flash +
  qwen as fallbacks. A deliberate cost-vs-quality lever on a quality-critical
  gate; the other repointed sites stay on free flash.

- **Reflection sessions are now strictly read-only.** Genesis's autonomous
  background reflections (deep/strategic) can read freely to investigate, but can
  no longer call any write or action tool — their only output is observations (and
  the structured reflection result the system parses). Previously reflections ran
  with unrestricted tool access and could, in rare cases, mint a follow-up or other
  write from ungrounded reasoning. Relatedly, any follow-up created by an
  autonomous/dispatched session now lands in the recoverable *tabled* lane for
  review rather than directly on the actionable follow-up board — the board stays
  reserved for your (foreground) work. A foreground session can promote a tabled
  item to the board.

- **All autonomous background sessions are now MCP-scoped by default.** Genesis
  runs many kinds of background Claude Code sessions (reflection, sentinel,
  inbox/mail triage, autonomy executor, ego gates, research). These are now secure
  by default: each session loads only the Genesis MCP tools it is explicitly given
  and no longer additively inherits the operator's user-scoped MCP servers (e.g.
  code-editing tools) that were never intended for autonomous use. A session that
  forgets to scope itself now fails closed (no extra tools) rather than open. Your
  own foreground conversations are unchanged — they keep the full toolset. This
  closes a latent tool-scope gap; nothing you'd notice day to day, no action needed.

- **Free-tier model refresh.** Groq is retiring Llama 3.3 70B (the model behind
  several of Genesis's free reasoning/extraction/tagging steps) on 2026-08-16, so
  those steps now use Groq's recommended replacement, gpt-oss-120b. Structured-output
  and extraction quality are unchanged; triage-depth labeling may shift by about one
  level on some items. No action needed on your end.

- **Stale ego proposals get tabled on a generous backstop.** Ego proposals you
  haven't acted on move to the recoverable *tabled* lane on a per-urgency
  schedule (roughly 10 days for critical up to 30 for low) — a backstop behind
  the ego's ongoing reconcile review, tuned to sit well past normal decision
  time so it only clears the genuinely-forgotten. Tabling is reversible, never
  deletion.

- **Setting a dashboard password now also protects the API.** Previously
  `DASHBOARD_PASSWORD` guarded only the web pages, while the underlying API —
  including the endpoint that reads and writes your saved keys — stayed reachable
  by anyone who could open the dashboard. With a password set, state-changing API
  calls now require your login session (Genesis's own components authenticate with
  an internal token). The gate is applied in every supported hosting mode (both the
  standalone server and Agent Zero), so no mode leaves the API open. Read-only
  calls and the voice API are unaffected. If one of your own local integrations
  breaks, set `GENESIS_DASHBOARD_API_AUTH=off` to disable just this gate without
  removing the password.

- **The push-approval prompt now appears only on a branch's first push, not on
  every push.** Publishing a branch to the public repo still asks for your
  approval the first time — that's the moment code actually goes public — but
  re-pushing fixes to a branch that's already on the remote (the normal
  PR-iteration loop) no longer re-prompts. A genuinely new branch prompts again,
  pushing to `main` still prompts, force-pushes are still hard-blocked, and
  autonomous/dispatched sessions still can't push at all. The check reads the live
  remote and fails safe: any uncertainty (unreachable remote, ambiguous target)
  falls back to asking.

- **Reflection model and effort are now editable from the dashboard, and Deep
  reflection thinks harder by default.** The model and reasoning effort behind
  each reflection depth — light, deep, and strategic — used to be fixed in code.
  They're now a settings domain you can edit live from the dashboard Config tab
  (a new **Reflection Models** panel), with no restart. These govern reflections
  running on the Claude Code CLI path: Deep and Strategic run on the CLI by
  design, so this is their primary model/effort; Light runs primarily via the API
  free-model chain and uses its value only when it falls back to the CLI. An
  effort control appears only for effort-capable models — switch a depth off Haiku
  and an effort setting surfaces (Haiku ignores effort at dispatch). The defaults
  also move to the new `xhigh` reasoning tier: Deep steps up from `high`, and
  Strategic settles at `xhigh` (from `max`).

- **`git push` in an interactive Claude Code session now asks for your approval
  instead of hard-stopping.** This safety hook used to block the command outright
  with no in-session way through; now Claude Code shows you a native approve/deny
  prompt you confirm with one keystroke — a gate the agent cannot self-satisfy.
  `gh pr create` no longer prompts on its own when it can't publish code —
  opening a PR is then just a review request, so `git push && gh pr create` asks
  once (for the push) and a standalone create on already-pushed code doesn't
  prompt at all. Only the one form that can publish — a create with no `--head`
  from a branch that isn't fully pushed, where gh pushes the branch itself — is
  gated like a push; any explicit `--head` (which tells gh to skip pushing) is
  never gated. The "already pushed?" check queries the live remote, so it isn't
  fooled by a stale local reference to a since-deleted branch, and any network
  error just gates. Autonomous/background Genesis sessions stay blocked from
  pushing directly (their real delivery path is separately gated).
  Force pushes stay hard-blocked; branch names that merely contain `-f` (e.g.
  `fix/…-false-positives`) are no longer mistaken for a force-push.

- **Genesis now keeps its own internal event log from growing without bound.**
  The observability event stream — the record of everything Genesis notices and
  does — was the last high-volume table with no cleanup, growing steadily on
  disk. A daily maintenance pass now trims events older than 90 days, matching
  the retention already applied to Genesis's other internal logs. No visible
  change to day-to-day use; it just stops a slow disk leak.

- **Proactive memory recall holds up better when several sessions are active at
  once.** When multiple Claude Code sessions run side by side, each prompt's
  memory recall used to spend much of its time-budget on bookkeeping (usage
  counters, quality metrics) that has nothing to do with the answer — and under
  that load it would sometimes time out and fall back to the weaker keyword-only
  memory (`[Memory·degraded]`). That bookkeeping now runs in the background after
  the results are returned, and the internal tag index refreshes in the
  background instead of stalling the first prompt after a restart. Recall quality
  is unchanged; it just stops dropping to the degraded path under concurrency.

- **Memory recall reads no longer wait in line behind the rest of the system's
  writes.** Everything Genesis does shared a single database connection, so when
  it was busy writing (reflections, learning, other sessions), a prompt's memory
  lookup could sit waiting for its turn — the main reason recall slowed down and
  occasionally dropped to the weaker keyword-only memory when several sessions
  were active. Memory lookups now read through a dedicated read-only connection
  pool that runs alongside the writes instead of behind them, so recall stays
  responsive under load. It falls back to the shared connection automatically if
  the pool is ever unavailable, so nothing breaks — recall is never slower than
  before. Recall quality is unchanged.

- **The proactive-memory hook is now a thin client of the server engine.**
  The Claude Code `UserPromptSubmit` hook (`scripts/proactive_memory_hook.py`)
  was a ~2,000-line hand-maintained fork of the retrieval engine — its own
  Qdrant search, RRF fusion, and formatting — that drifted from the real
  engine (no reranker, entity lane, or graph expansion). It now posts each
  prompt to the server recall endpoint (`POST /api/genesis/hook/recall`) and
  renders the result, so every memory improvement ships once instead of twice.
  If genesis-server is unreachable the hook degrades to a keyword-only FTS5
  search (clearly labelled) so a prompt is never blocked, and self-heals on the
  next prompt. The fork's content-quality guards are preserved server-side on
  the endpoint path — malformed rows (raw JSON blobs / YAML frontmatter) and
  non-intentional `knowledge_base` ingestions (surplus/recon crawl) are filtered
  out of proactive injection, as before. New knobs: `GENESIS_PROACTIVE_HOOK_MODE` (`server`/`local`/`off`)
  and `GENESIS_PROACTIVE_HOOK_URL` (see `env.example`); `proactive_metrics.json`
  gains a `mode` + `server_ms` field so the fallback rate is observable.
  **Upgrade note:** the memories surfaced per prompt will differ (and improve) —
  the engine's reranker/fusion/intent-aware budget replaces the fork's simpler
  ranking; command turns surface fewer, decision questions more.

- **The voice API is now fail-closed.** Previously, leaving
  `GENESIS_MCP_HTTP_TOKEN` unset left every `/v1/voice/*` route open to the
  network. Now an unset token disables the voice API (503 + a boot-time
  warning in the server log). **Upgrade note:** if you use the voice API,
  set `GENESIS_MCP_HTTP_TOKEN` in `secrets.env` and make sure your Home
  Assistant / voice-addon configs send it as a Bearer token — token-less
  setups stop working on this upgrade.

- **Updates are more resilient and briefly less disruptive.** `scripts/update.sh`
  now downloads new code *before* stopping Genesis, so a slow or stalled network
  fetch no longer prolongs the restart — and a failed fetch leaves the server
  running, untouched. The machine-info blocks in `~/.claude/CLAUDE.md` are also
  regenerated *after* services come back up instead of during the offline
  window, trimming the downtime slightly.

- **Finished background-queue rows are now pruned after 45 days.** The internal
  deferred-work queue kept every completed item forever; it now retains 45 days
  of history and drops the rest, so the queue can't slowly grow without bound.

- **Every terminal door now leads to the same persistent session.** Running
  `claude` by hand over SSH or in the dashboard web terminal now lands in a
  persistent numbered tmux slot (`cc-N`, lowest free — the same pool the SSH
  slot hostnames use), with a printed map of your existing slots and how to
  reattach. A dropped connection or closed browser tab just detaches the
  session; walking back in can never spawn a second copy. Previously, manual
  launches got a uniquely-named throwaway tmux session that nothing ever
  reattached to — abandoned launches quietly accumulated as orphaned
  processes. Extra arguments (e.g. `--resume <id>`) are forwarded into the
  slot; opt out per-shell with `GENESIS_NO_TMUX_WRAP=1`. The wrapper updates
  itself on your next `update.sh` run.

### Fixed

- **A model your account tier cannot use no longer stalls the fallback chain.**
  When a provider refuses a call because the plan does not include that model,
  the refusal arrives as an HTTP 403 whose message names the plan or subscription
  you would need. Those are the same words a genuine "you have used up your
  allowance" message uses, so the router read the refusal as an exhausted quota
  and did what that calls for: waited, and tried the same provider again. Since
  the answer can never change, that wait was spent against the chain's overall
  time budget for nothing — measured at several seconds on each attempt.

  Entitlement refusals are now recognised on their own terms. The router gives up
  on that provider immediately and moves to the next one in the chain, and a
  provider that keeps refusing escalates onto the longer hold-out window instead
  of levelling off at half an hour. To be exact about what that is worth: the
  hold-out is identical to the existing one for the first four trips and only
  pulls ahead during a sustained outage, so the gain is in not re-probing a
  provider that has been dead for hours — not in the first few minutes.

  The same correction is applied to an exhausted allowance, which had the same
  problem for the same reason: a spent quota is a billing state, so waiting a
  few seconds cannot change it either, and the limit usually applies to the
  whole account rather than one model — so a single walk could pay that wait
  once per provider it tried. Both now behave the way a rate-limit already did,
  which is to stop asking and move on.

- **SECURITY.md described a posture the code left behind two months ago.** The
  security policy told operators to treat the dashboard API as
  "unauthenticated administrative access" and said the dashboard password
  "protects the web UI, not the programmatic API". Neither has been true since
  the API mutation gate landed: with a password set, state-changing `/api`
  requests require a bearer token or an authenticated same-origin cookie with a
  CSRF check, and `/v1` enforces its own bearer. The doc now says so -- along
  with the limits that decide whether you still need network isolation, because
  you do: the gate covers mutations only, reads stay open, it is inert when no
  password is set, and it has a documented kill switch.

  Corrected in the same pass: the autonomy ladder was described as seven levels
  (`L0`-`L6`) when four ship (`L1`-`L4`, the rest deferred), the per-category
  permissions were described as six categories when there are four, and the
  process-group-kill example was attributed to a PreToolUse hook when it is
  runtime library hardening -- a different guarantee, since a hook cannot be
  bypassed by the agent and a helper only protects its own call sites. The
  guard section also named one linter as the enforcement mechanism for shell
  and URL policy; that linter only ever sees file edits, and the shell and web
  guards are separate programs chosen by tool matcher.

  Newly documented rather than corrected: provenance stamping and the two
  privileged-write paths gated on it, the approval requirement and journal now
  standing in front of irreversible entity merges, session-id validation before
  filesystem use, and an External Egress section that says plainly which
  outbound channel actually enforces a gate today and which are only observed.

- **A graph backend that cannot answer no longer erases the shield's memory.**
  "The graph store is unreachable" and "no bridge memories exist" used to look
  identical — an empty answer — so a missing library or unreachable backend
  made the nightly centrality pass wipe its cache, and the importance shield
  then protected nothing until the backend came back AND the pass re-ran.
  Unavailability is now its own loud signal: the pass keeps the previous
  bridge-node population standing and says why, while a genuinely empty graph
  still supersedes stale rows. Two writers also stopped leaving the cached
  graph stale: superseding a memory now tells the graph about the new
  succession edge, and the integrity sweep that purges a dead memory's edges
  now invalidates the cache it just made wrong.

- **A schema rebuild no longer destroys columns a private fork added.** The
  ledger table rebuild (widening a constraint means rebuilding the table on
  SQLite) copied a hardcoded upstream column list and then dropped the old
  table — so on an install whose supported private fork had added its own
  column, that column and all of its data were silently, irreversibly gone.
  The rebuild now reads the live table first, re-creates any column it does
  not recognize from that column's own declaration, and copies its data; the
  one shape it cannot re-create (NOT NULL with no default) stops the
  migration with a clear message before anything is dropped.

- **When every provider fails, the log now says which ones.** A routing failure
  recorded only how many attempts were made — but a provider skipped because its
  circuit breaker is open, its API key is missing, or the budget is spent costs
  no attempt at all, so "2 attempts" on a seven-provider chain looked exactly
  like a two-provider chain that was fully tried. The exhaustion event and result
  now name every provider involved, alongside how many the chain had to offer —
  and the log line keeps the two kinds apart: providers whose call actually
  failed print under `failed:`, providers passed over before any call print
  under `skipped:` with the reason (no API key, breaker open, budget exceeded),
  because a never-called provider labelled "failed" reads as an outage where
  there may be none. One-time cost of reshaping the message: the Errors
  dashboard keys manual resolutions on the message prefix, so an exhaustion
  group resolved before this change reappears once under its new key — resolve
  it again and it stays resolved.

- **The temp-space watchdog no longer severs cross-session messaging when it
  goes nuclear.** At its most aggressive cleanup tier the watchdog deleted every
  top-level directory of Claude Code's working temp — including the directory
  holding each live session's messaging socket. The sockets are zero bytes, so
  deleting them reclaimed nothing, while every running session silently became
  unreachable to its peers until restarted. The nuclear sweep now spares unix
  sockets (and only them — all reclaimable bytes are still deleted) and logs
  how many it preserved.

- **Marketing campaign updates now post to their own Telegram topic.** The marketing
  campaign's tick updates previously routed to the shared Morning Reports topic; they
  now go to a dedicated "Marketing" forum topic via a new `marketing` outreach
  category, keeping them separate from the morning report and other digests. Existing
  installs pick up the category automatically on restart (an additive schema
  migration); the morning report and all other topics are unaffected.

- **Proactive memory recall could time out completely.** The embedding provider
  queues standard-rate requests when a model is busy, and that wait can run past
  recall's four-and-a-half-second budget — measured at eight to thirteen seconds
  against a budget of four and a half — so every lookup fails and sessions run with
  no recalled memory at all. Recall now asks for the provider's priority lane, which
  answers in about 650 milliseconds regardless of how long the prompt is.

  **This costs a little more, and the amount is worth knowing: one and a half times
  the standard embedding rate, on recall only.** At one person's usage that is a
  fraction of a cent a month, and declining it means keeping a feature that does not
  work. Storing memories stays on the standard rate — that runs in the background
  with nothing waiting on it. Set `GENESIS_EMBED_PRIORITY_TIER=false` to decline the
  faster lane; recall then falls back to keyword-only search whenever the queue is
  longer than the deadline.

  Scope: this covers the proactive-recall path served by genesis-server. An explicit
  `memory_recall` tool call through a standalone MCP process still uses the standard
  lane, so it can be slow without failing.

  The setting is a yaml lever as well as an environment one, and the template no
  longer overrides it: a fresh install copies `secrets.env.example` to `secrets.env`
  and the environment is read first, so an uncommented assignment in the template
  would have quietly outranked `memory.embed_priority_tier: false` and left the
  documented opt-out doing nothing.

- **The setup script's questions about local inference servers had no effect.** The
  same shadowing applied to the Ollama and LM Studio addresses: the template assigned
  them, a fresh install copied that to its environment, and the environment outranks
  the config file — so the address the interactive setup script asks for was written
  to the config and then ignored, and every call went to localhost regardless. Those
  assignments are now commented out; the values they held were already the defaults.
  The Ollama on/off switch deliberately stays assigned, because unlike the addresses
  its default differs from the template value, and removing it would switch Ollama on
  everywhere.

- **A malformed config section could quietly weaken the private-data scan.** The
  fingerprint harvester, which collects this install's private values so they can be
  blocked from ever reaching a public push, read config sections the same unguarded
  way — and its error handling covers the whole harvest, so one bad section dropped
  not just the addresses it was reading but the timezone and private-repository
  patterns queued behind them, with nothing logged. Sections are now read defensively
  there too. Separately, ignoring a malformed section is no longer silent anywhere: it
  logs which section was discarded and that defaults are in force, because two of
  those settings fail toward spending money and toward running an autonomous job the
  operator had switched off.

- **Model routing ignored the install config, so half the system talked to the
  wrong machine.** Settings like the local inference server's address are resolved
  in one documented order — environment, then the install config file, then a
  built-in default — but the routing layer expanded its placeholders from the
  environment alone. That was invisible while the template forced the same values
  into the environment anyway; removing those assignments so the config file could
  work is what exposed it. An install pointing at a remote inference server ended
  up with its dashboard, health check and embeddings reaching that server while
  routed model calls still went to localhost. Routing now resolves those settings
  the same way everything else does, which also means the setup script's questions
  about local inference finally take effect. An environment variable still wins
  where one is set, and any placeholder without a matching setting behaves exactly
  as before.

- **A quoted "false" in the install config meant true.** Settings written in
  `genesis.yaml` are read as booleans, but a value in quotes arrives as text, and
  any non-empty text counted as on — so `embed_priority_tier: "false"` kept the
  paid lane running, while the same word unquoted, or written in `secrets.env`,
  correctly turned it off. One intention, three places to write it, two answers.
  The same spellings now mean the same thing everywhere, for every on/off setting
  in that file.

- **An override set from the dashboard could never be unset again.** Some settings
  can be given a value in the config file or overridden in the environment, and the
  environment always wins. The settings editor could set those overrides but had no
  way to clear one, so the first time you typed a value there it took over
  permanently — later edits to the config file appeared to do nothing, with no way
  back short of hand-editing the file the editor exists to avoid. Clearing the field
  now removes the override and hands the setting back to the config file. Required
  credentials still cannot be blanked.

- **A config file that was unreadable as settings was ignored in silence.** A
  malformed section already said so; a malformed file did not, even though it still
  contained everything the operator had written — including the switch that keeps
  memory lookups off the paid lane. It now says plainly that the whole file is being
  ignored and where it is.

- **The timezone control could delete the config it exists to repair.** If the
  file was malformed in a way that left its contents unreadable as settings, the
  dropdown rewrote it with the timezone alone — discarding whatever else was in
  there, silently, on the one control documented as the way to recover. It now
  copies the original alongside first, writes the timezone, and says plainly what
  it did and where the copy went.

- **Settings the template ships commented out disappeared from the dashboard.**
  Some defaults are deliberately left commented so the equivalent setting in
  `genesis.yaml` keeps working. The dashboard's editor only recognised
  uncommented lines, so those keys vanished from it and updates were rejected as
  unknown — including several that had been invisible this way for some time.
  They are listed again, simply with no value set.

- **A one-line typo in the install config could silently disable vector memory.**
  Accessors that read a nested setting out of `~/.genesis/config/genesis.yaml`
  assumed the section around it was a mapping. Two shapes an ordinary edit produces
  are not: a section whose only child is commented out (which yaml reads as empty
  rather than absent), and a section given a plain value instead of a block. Either
  one raised on the next read, and because the memory subsystem catches everything
  around its own startup, the install would come up reporting a degradation and then
  run with no vector memory at all — from a config file the operator is invited to
  edit by hand. Every such setting — the local inference URLs, the Ollama switch, the
  recall priority lane, the build lane, the models-file synthesis job, and the GitHub
  identity — now falls back to its documented default instead, as does a config file
  whose top level is malformed outright.

- **A session slot started after another tmux server no longer gets the wrong
  temp directory.** A new slot created while a tmux server started in some other
  context is already running used to inherit that server's temp directory
  (often the small system `/tmp` Genesis keeps Claude off of). The temp
  directory and the OAuth-durability setting are now pinned to the slot
  explicitly (when a usable temp directory exists at all — if none does, the
  session is left on the system default rather than pointed at a bad path).

- **The cold-marketing campaign no longer re-pitches the same person.** Once a
  marketing pitch is delivered to a prospect, that prospect is marked contacted and
  drops out of the campaign's target list — previously nothing recorded the contact,
  so the campaign would have re-pitched every delivered target on each run. Works on
  both the owner-approved and (future) autonomous send paths; a pitch that never
  delivers (dropped, expired, or rejected) leaves the prospect eligible for a later,
  re-worked pitch. (The substrate still ships off by default.)

- **Campaign names stored before the control-character fix are now cleaned at
  startup.** Names have been sanitized at the write boundary since the previous
  release, so nothing new lands malformed, but rows written earlier were never
  repaired. The cleanup now runs during campaign initialization, before the
  scheduler registers its jobs — the ordering matters, because each campaign's
  scheduled job is keyed by its name, and renaming afterwards would leave the
  running job pointing at a name that no longer exists. A campaign whose cleaned
  name would collide with another campaign's is left untouched and logged rather
  than merged.

  A campaign's history now travels with the rename — both its durable health
  record and its individual run history. Leaving either behind was not merely
  untidy: an abandoned health record keeps reporting the job as stale on every
  health sweep, indefinitely, because nothing checks whether the job still
  exists; and the run history is looked up by exact name, so a scheduled-job
  prediction spanning the cleanup would have been scored against a series with a
  hole in it, or discarded as having no runs at all. Where the name being moved
  into already carried an abandoned record from a deleted campaign, the campaign's
  own live history now wins and the leftover is removed — previously the reverse
  happened, keeping the record that could never be written to again.

  One name is now refused outright: a campaign called `pending_reaper` collides
  with an internal job the scheduler registers for itself, and would have been
  evicted at startup with no error and no log — it would simply never run again.
  It is rejected when a campaign is created, and a stored name that would clean up
  into it is left alone, since a name with a stray invisible character still runs.

- **YouTube transcripts are less likely to come back quietly incomplete.** When
  Genesis fetches a video transcript it now asks for both English caption tracks and
  prefers the original ASR (`en-orig`) over the `en` variant. Observed once: the two
  were served as different transcriptions — different cue segmentation, and different
  wording in the closing lines — while the same video served identical tracks hours
  later. The cause is unknown and it did not reproduce, so this is insurance rather
  than a diagnosed fix, but preferring `en-orig` costs only one extra small download.
  Two real bugs fixed alongside it: the cleaning step left a stray whitespace-only
  line in every transcript (a caption file carries both empty and single-space lines,
  and the old filter matched only the empty ones), and the documented recovery path
  for a video with no English captions could not work — dropping `--sub-langs`
  narrows the request to one English-first track instead of broadening it, so it
  could never surface the other languages it promised.

- **The `deliberate` MCP tool ("Model Fusion") no longer fails on real prompts.** Two
  distinct bugs: (1) analysis mode 404'd because the orchestrator slug
  `openai/gpt-oss-120b:free` was retired from OpenRouter's catalog (`:free` variant gone)
  — switched to the live base slug `openai/gpt-oss-120b`; (2) real multi-paragraph prompts
  false-timed-out at 240s (a 6-model frontier panel + judge legitimately runs several
  minutes, while a trivial ping finished in ~33s) — the budget is now a single env-driven
  knob (`GENESIS_DELIBERATE_TIMEOUT_S`, default 1000s) read per-call and threaded through
  `core.deliberate()` (which previously hard-pinned 240s, silently overriding the backend
  default). Also hardened the panels' one remaining concrete `x-ai/grok-4.3` slug to the
  drift-resistant `~x-ai/grok-latest`.

- **The morning report no longer cries "surplus heartbeat overdue" during a long
  healthy dispatch.** Surplus emits its subsystem heartbeat only at the end of a
  dispatch cycle, and a single healthy dispatch can run 15-30 minutes — longer than
  the old 10-minute overdue threshold — so a busy-but-healthy surplus was flagged
  "heartbeat overdue" in the morning report and the subsystem-heartbeats view. The
  threshold is loosened to 3 hours, matching the surplus dashboard tile's own
  liveness bound; a genuinely dead surplus is still caught within ~15 minutes by the
  scheduler watchdog, which reads a separate, per-dispatch signal.

- **A partial write to the concurrent-session record no longer erases fields it
  was not told about.** The row has several writers that each know a different
  part of it, and all but one of its columns were overwritten unconditionally —
  so a writer that simply did not know the model wiped the stored one. The
  model cache holds a bounded number of sessions, so a long-lived session whose
  entry had aged out would destroy its own model on the next write. Every
  content column is now preserved when a writer omits it; only the source tag,
  which has a real default, is still overwritten.

- **The Queues card could report "healthy — queues are clear" for counters it
  never collected.** When the queues section of the health snapshot fails, it is
  replaced wholesale by an error marker carrying no per-counter detail. The
  card's verdict only inspected the per-counter error list, so it read every
  depth as a missing zero and returned a confident green — displayed beside the
  panel's own "Queue data unavailable" notice, and folded into the overall
  dashboard status. Unmeasured zeros are now reported as unknown rather than
  healthy. Relatedly, the "not a confirmed zero" notice was keyed on a list
  shared by all four queue sources, so an unrelated counter failing printed it
  above a correctly-counted list of discarded rows; it is now scoped to
  failures of the count it actually describes.

- **The `/genesis/monitor` page had the same discarded-count bug, plus a worse
  variant: rows you could not clear.** Its "Clear All Discarded" button was shown
  only when the 20-row sample held more than one entry, while its label printed
  the true count — so a backlog whose sample failed to load displayed
  "Discarded (148)" with no clear control at all. The button now follows the real
  depth, and appears even when that depth could not be read — it deletes every
  discarded row regardless, so withholding it was what stranded the backlog. The
  header labels the sample as truncated. (A queue holding exactly one row still
  shows no clear-all button — that row is cleared by its own Clear control,
  which is the intended behaviour.)

- **Dashboard reported the discarded-queue depth as 20 when it was 148.** The
  Queues panel and the attention strip both rendered `discarded_items.length` —
  the length of a deliberately capped `LIMIT 20` review sample — instead of
  `discarded_count`, the true unbounded depth the backend already supplied. Any
  backlog above 20 therefore displayed as exactly 20, and because the displayed
  number equalled the cap it looked like a plausible total rather than a
  truncation. The depth and the review sample are now reconciled ONCE, by the
  backend, and published as a single object; every surface renders what it is
  given rather than deciding for itself which of two numbers to believe. So the
  panel can no longer claim a backlog while showing "no items awaiting review",
  disable the button that clears it, or report 0 while listing rows. The review
  list is labelled "showing 20 of N" whenever it is truncated, so the sample
  cannot be mistaken for the whole queue, and a depth that could not be read is
  now reported as unknown instead of as an empty queue — with the clear-all
  control still available, since it removes every row regardless of what was
  counted.

  **API change:** the health snapshot's `queues` section gains a `discarded`
  object (`total`, `sample`, `sample_truncated`, `known`). The previous
  `discarded_count` and `discarded_items` keys remain, and are now derived from
  that object. `discarded_items` is unchanged. `discarded_count` changes in two
  states, both toward honesty: when the depth query fails it reports the rows
  actually in hand rather than 0, and when the depth and the sample disagree it
  reports the larger rather than the depth alone. Anything reading it as "the
  queue depth" — including the >100 queue-depth alert — keeps working and
  under-reports far less in those states: where a failed count previously
  yielded zero, it now yields the rows actually in hand.

  A depth is reported as EXACT whenever the read that produced it was complete,
  not merely whenever the count query happened to succeed. A review sample read
  under a `LIMIT` of one past the cap that comes back short has exhausted the
  matching rows at its own snapshot, so it is the depth — which means a small
  queue is now reported exactly even while the count query is failing, instead
  of as "5+" beside a "queue data unavailable" notice for a number that had in
  fact just been measured. It also means a count that disagrees with a complete
  sample no longer influences the total in either direction: a count taken
  before rows arrived reads low, and one taken before a prune removed them reads
  high, and neither can be detected by comparing two reads that never shared an
  instant. Only a TRUNCATED sample still depends on the count, and there the
  total is published as a floor unless the count is consistent with it. The
  remaining exposure is stated rather than hidden: with a truncated sample, a
  prune landing between the two reads can still publish an inflated depth as
  exact for one cache window; closing that needs both values read under one
  snapshot and is tracked separately.

  The Queues card's verdict follows the same principle: a diagnostic no longer
  doubles as an answer to "is this counter known". The card previously read any
  entry in the section's error list as an uncollected counter, so once a depth
  could recover from whichever read completed, an exactly-measured queue
  rendered a precise number beside "some queue counters could not be collected".
  Errors are not suppressed — they stay in the payload and the panel still shows
  them — they simply stop deciding a verdict they no longer describe. Counters
  that publish no exactness of their own are unaffected, and one unrecovered
  error alongside a recovered one still marks the section unknown.

- **"Clear all reviewed" now says how many rows it will actually delete.** It
  always deleted every discarded/expired row, not the 20 displayed — harmless
  while the panel hid the difference, misleading once it reports the true
  depth. The button reads "Clear all N" with a tooltip stating it is permanent
  and covers rows not shown.

- **Clearing the queue no longer leaves the dashboard showing the rows it just
  deleted.** The health snapshot is cached for up to 30s and nothing invalidated
  it, so the client's immediate refetch re-rendered pre-delete counts: a
  "Cleared 148 discarded items" toast beside a panel still listing them, with
  per-row Clear buttons that silently did nothing. Mutations now bust the cache.

  The cache moved into the health service, alongside the computation it caches,
  and is reached only from the event loop — invalidation raised from a web
  request is handed to the loop rather than touching shared state across
  threads. A snapshot whose computation began before a mutation is never
  published and never handed to a caller that arrived after it, so a cleared
  queue cannot reappear for the rest of the cache window. On a host that
  configures no event loop for that hand-off (an embedded plugin host, where
  invalidation runs on the request thread instead), the cached value is read
  once and reused rather than tested and then re-read, so an invalidation
  arriving mid-read can no longer make the endpoint fail outright. Callers that need
  current data are unaffected: only this endpoint accepts a cached result, and
  it says so explicitly.

- **The ego's self-model stopped presenting stale, thin and arbitrarily-ranked
  rows as present-tense capability.** `capability_map` feeds three ego-prompt
  sections and the capability-improvement scanner. Measurements below come from
  two live installs, distinguished as **A** (627 rows) and **B** (2102 rows) —
  they differ because the flag-gated Outcome-Bus feed is on for A only.

  *Thin rows.* Sources 5 and 6 already refused to emit a signal below 3 samples;
  the journal / proposals / autonomy / procedural sources had no floor. Since
  `procedural_memory.task_type` is a per-item slug rather than a category, that
  left the large majority of the map as one-procedure "domains" — 596 of 597 on
  A, 2066 of 2067 on B. On B they reached the ego: two single-procedure rows sat
  in the rendered top-15, outranking a domain with n=70. The floor now applies
  to the COMBINED sample size in `compute_capability_map`, and again on read, so
  rows written before it existed are not still surfaced.

  *Stale rows.* `updated_at` records when the AGGREGATOR last wrote a row, not
  the age of its evidence. Only 3 of the 6 sources are time-windowed
  (ego_proposals / cc_sessions / outcome_events, 30d); intervention_journal,
  autonomy_state and procedural_memory are not, so domains fed only by those
  never age — correct for present-tense state such as lifetime counters and
  currently-stored procedures, and a documented wart for the journal's
  historical events. The honest uniform reading is "the aggregator stopped
  vouching for this row N days ago". For windowed-source domains the effect was
  real and measured **on B**: a 43-day-old `1.0` at #2 in the rendered
  self-model, and a **93-day-old** `0.0` row at the top of `get_weakest`,
  steering the improvement scanner at a domain with no qualifying evidence since
  May. Prompt-facing reads now exclude rows more than 14 days behind the
  freshest. **On A the window excludes nothing** — maximum observed lag there is
  6 days.

  *Arbitrary ranking.* Confidence is a ratio, so well-exercised domains pile up
  at exactly `1.0` — 19 such rows on A, more than filling a 15-row table. With
  no secondary sort key SQLite returned an arbitrary 15, and an `n=3` row
  displaced one with `n=3276`. Both bars are powerless here because every tied
  row clears them; on A the top-15 was byte-identical before and after
  filtering. The prompt read and `get_weakest` now break ties on
  `sample_size DESC`, which is what actually changes A's rendered table
  (`code_index` n=94 and `model_eval` n=44 replace four n=3 rows). Confidence
  remains the primary key, so a very-high-n domain scoring slightly below `1.0`
  can still fall outside the top-15; reworking the primary ranking is out of
  scope here.

  *Anchor safety.* The window anchors on the freshest USABLE row — date-shaped,
  parseable, not in the future. Anchoring on the freshest row rather than
  wall-clock means a totally dead refresh job ages the table uniformly and hides
  **nothing**, instead of blanking the self-model the moment the scheduler
  breaks. The other direction matters too: `MAX()` is unbounded above, so a
  single row stamped ahead of real time would otherwise define the window for
  every other row and hide all of them silently — and self-perpetuatingly, since
  nothing rewrites a domain that has stopped being emitted. Future rows are
  therefore EXCLUDED from the anchor rather than the anchor being clamped after
  the fact: clamping leaves a uniformly-old table entirely outside the window. A
  partial refresh outage remains uncovered and is tracked separately.

  *Reads split by intent.* `get_all` and `get_by_domain` stay raw accessors;
  `get_prompt_rows` and `get_weakest` carry the policy, so a future non-prompt
  consumer cannot inherit ego-prompt filtering by accident. A new `count_all`
  lets a renderer tell "the map is empty" apart from "every row was filtered" —
  two states that must not produce the same sentence, since each is a false
  claim in the other's situation. All three renderers now distinguish them and
  name the real row count when rows were withheld.

  *Anchor totality.* A `COALESCE` fallback to wall-clock is retained as
  belt-and-braces. It is not an active guard: the anchor subquery filters on the
  same predicate as the outer read, so it yields NULL only when no row passes
  the outer predicate either and the result is empty regardless. It is kept so
  the two cannot silently diverge later without a fallback already in place.

  Three consequences are deliberate. **(1)** The light-depth "avg confidence"
  figure moves sharply — `0.06 → 0.94` on A. The old number was not a capability
  average at all: dominated by hundreds of zero-confidence one-sample rows, it
  reported roughly "what share of stored procedure slugs carry confidence".
  That branch renders no table, so it now states both figures as the qualifying
  subset rather than as whole-map facts — left unqualified it read "31 domains
  tracked (avg 94%)" over a 627-domain map averaging 6%.
  **(2)** "N domains tracked" drops for the same reason (`627 → 31` on A,
  `2102 → 19` on B); the renderers show a top-15/top-12, so those tables stay
  full. **(3)** The focused-deficiency line reads `get_by_domain` — deliberately
  unfiltered, since a capability-improvement cycle targets a domain *because* it
  is weak — and is resolved BEFORE the empty-table check, so it survives even
  when every row is filtered out. It now also stamps the row's last-vouched
  date, because an unlabelled unfiltered row is exactly the present-tense claim
  on old evidence this work removes elsewhere. All three renderers' empty states
  now say rows were filtered rather than claiming no data exists; the base
  builder additionally stopped rendering a query failure as an empty map.

  *Withheld rows are named at every depth, and light means light.* The
  dropped-row report reached the empty and deep exits but not the light one —
  where it matters most, because that branch renders no table and so leaves the
  reader nothing else to notice a loss by (the same call is what LOGS, so an
  operator got no signal either). Separately, the Genesis renderer ACCEPTED a
  `depth="light"` request and rendered the full fifteen-row table anyway: the
  caller believed it had asked for the cheap form and was billed for the
  expensive one. Both now honour it, sharing one sentence rather than two
  copies — on a branch with no table the sentence is the entire claim, so a
  figure qualified in one renderer and unqualified in the other is the same
  "one field, two truth claims" defect from the other side. Neither was
  reachable through today's focus profiles: of the seven,
  `capability_performance` is `deep` in three and `skip` in four, and the
  fallback used for an unknown focus type is `deep` — never `light` anywhere,
  and the compaction layer only ever upgrades a section's depth. So this is a
  latent fix, not a live one. What made both survive review is the more useful finding: the
  render-state matrix built to catch exactly this class enumerated depth
  *beside* its cross product instead of *inside* it, so all of its cells ran at
  one depth. Depth is now an axis of the product.

  *A negative window is refused instead of silently disabling de-duplication.*
  `intervention_journal.aggregate_by_type` rendered a negative day count as the
  SQLite modifier `'--N days'`, which SQLite rejects, yielding NULL; the
  comparison against NULL is then NULL rather than false, so the exclusion held
  for every row and every proposal was counted twice again — from a call that
  returned a perfectly healthy-looking result. The sibling windowed API already
  refused this loudly; the two no longer disagree. No shipped caller passes a
  negative value, so this closes a trap rather than a live bug.

  Nothing is deleted: rows below either bar stay in the table and stop being
  RENDERED as present-tense capability. They are still read deliberately — by
  `get_by_domain` for the focused-deficiency line, and by `count_all` to say how
  many were withheld — and they simply stop being refreshed.

- **A test no longer reads the wall clock once at import and races the suite.**
  `test_surplus_liveness.py` captured `datetime.now(UTC)` at module import and
  seeded a heartbeat 30 minutes ahead of it; production ages that seed against
  the *live* clock with a 5-minute future-skew tolerance, so the assertion only
  held while under 25 minutes had elapsed since import — the whole suite's
  runtime, not the test's. Past that edge it failed, and a re-run went green,
  so it read as a flake; a 31-run survey put it at roughly 3% of runs. The seed
  is now computed when the helper is called, shrinking the margin from the
  suite's runtime to one test's. Measured on both sides of the boundary against
  real production code: the case passes with 16 minutes of simulated elapsed
  time and fails at 26.

- **SSH slot cap no longer collapses below the running session count.** The
  interactive-slot launcher (`scripts/cc-slot.sh`) sized its cap from
  *instantaneous free RAM* (`(MemAvailable − reserve) / per_session`), so each
  running session lowered free RAM and thus lowered the cap *below* the number
  already running — locking the operator out of a new session (and even
  misreporting "3/2 active") while other apps' memory use silently ate slots too.
  The cap is now a stable function of the box's TOTAL RAM (a new pure, unit-tested
  `genesis.cc.session_cap` helper), so it scales per install, does not shrink as
  sessions run, and ignores unrelated apps. It is **container-aware** — it uses the
  cgroup memory limit and CPU affinity, not host `/proc` values, so a container that
  sees host RAM is sized for its real limit (not the host). Live free RAM is used
  only as an OOM circuit-breaker, and a new session only starts when there is room
  for a full session (never over-committing a swapless box). Any interactive SSH
  login (a slot hostname or a plain shell running `claude`, from a LAN/Tailscale IP)
  is the operator and gets an emergency slot above the safe cap; the cap itself
  never turns it away — when the box is full or memory is tight it offers to reattach
  or end a chosen session to make room (the ended session's transcript persists,
  resume with `claude --resume`), and an ATTACHED session needs an explicit confirm
  before it's ended. Two honest corners still decline: a non-interactive login
  (no terminal to prompt on) is guided to reattach, and a genuine OOM-floor breach
  with no slot to trade is refused rather than risking an OOM. The dashboard web
  terminal / local console (no `SSH_CONNECTION`) is held to the safe cap. Reattaching
  always works. Tunable via `~/.genesis/cc-slot.env`
  (`GENESIS_CC_SYSTEM_RESERVE_MB` / `_PER_SESSION_MB` / `_OOM_FLOOR_MB` /
  `_EMERGENCY_SLOTS`); the gate fails open so it can never strand you. See
  `docs/reference/tailscale-ssh-access.md`.

- **Heartbeat GC no longer lets a clock-skewed future row starve a subsystem's
  liveness signal.** The `keep_latest_per_subsystem` heartbeat GC
  (`db/crud/events.py::prune`) kept the row equal to the per-subsystem
  `MAX(timestamp)`. Because `timestamp` is ISO **text**, a corrupt/clock-skewed
  future row (e.g. `2099-…`) sorts as that MAX and survived the retention window
  forever, while genuine pulses aged out and were deleted — leaving
  `compute_heartbeat_staleness` with only the future row, which it rejects as
  materially-future, degrading the verdict to a permanent `unknown` (a false
  "can't tell" for a subsystem that may be perfectly healthy or truthfully
  stale). The GC now uses two distinct future bounds: (a) it deletes only
  *implausibly*-far-future rows (> 1 day ahead — corrupt beyond any clock-skew
  recovery), and (b) it anchors the "keep newest" on the newest row within the
  read-side display tolerance (`observability.liveness.FUTURE_SKEW_TOLERANCE_MINUTES`),
  so the preserved pulse is one the staleness read accepts (`alive`/`overdue`). The
  wide destructive horizon is deliberate: a *modestly*-future row ages into validity
  instead of being destroyed, and a **backward** clock skew at GC time cannot delete
  genuinely-recent pulses. A write-time clamp was considered and rejected: the only
  production trigger is host clock skew, against which a clamp is ineffective (at
  write time `now()` *is* the skewed value), so the retention layer — re-evaluated at
  GC time — is the layer that actually closes the hole.

- **The run_in_background pipe guard no longer false-blocks a `|` inside a quoted
  argument.** The old inline check (`${CMD//||/ }` then `grep -qF "|"`) blocked any
  literal `|`, so backgrounding `gh api … --jq '.[] | .x'` or `grep -F '|' file`
  was wrongly rejected. It's now a small Python hook (`background_pipe_guard.py`)
  using the canonical quote/redirect-aware parser (`shell_parse.has_top_level_pipe`),
  so only a genuine top-level pipe — whose backgrounded stdout really is swallowed —
  blocks; a `|` in quotes, a `||`, or a `>|` redirect does not. (Convenience guard:
  a `|` inside a heredoc body or `case` pattern is a documented residual that may
  still over-block — never a security bypass.)

- **A dead subsystem scheduler no longer reads "healthy" on the dashboard.** When
  a background subsystem's scheduler/loop stops firing entirely (total cessation),
  its heartbeat pulse goes silent — but nothing turned that into a signal, so the
  Ego tile (and the rollup badge) could show green while the egos were dead, and no
  alert was raised. Now the Errors view raises a `subsystem_stale:<name>` alert when
  the ego (→ critical), inbox, or dashboard (→ warning) scheduler goes overdue past
  its threshold, and the Ego tile flips to error ("scheduler stopped — no heartbeat
  in Nh"), failing loud (`unknown`) if the signal can't be read. The alert is
  pause-aware — a deliberately paused Genesis no longer false-alarms — and never
  fires on a merely idle or freshly-booted install. This complements the existing
  "running-but-failing" job alarms, which cannot see a job that has stopped running
  at all. (Surplus already surfaces a wedged/dead loop via its own dashboard tile;
  outreach total-cessation is tracked separately, since its heartbeat only runs once
  a messaging channel is configured.)

- **A subsystem that never started no longer reads "healthy" either.** The
  total-cessation alert above catches a scheduler that ran and then *died*; a
  subsystem that *failed to start* (its bootstrap init raised, or it registered but
  never emitted a single pulse) has no heartbeat at all — which looked identical to
  a fresh, never-run install, so it stayed silent. Now the health check cross-
  references the persisted bootstrap manifest: an enabled ego (→ critical) or inbox
  (→ warning) that the manifest shows failed to initialize, or that registered but
  never pulsed past a boot grace, raises a distinct `subsystem_never_started:<name>`
  alert and flips the Ego tile to error. It fails benign in every ambiguous case —
  a fresh install, a deliberately disabled or unconfigured subsystem, or an
  unreadable manifest never false-alarm — so the only new signal is a genuinely
  broken start. (Covers ego + inbox; a never-started dashboard thread is out of
  scope — it isn't a bootstrap-manifest entry.)

- **The dashboard Surplus health tile no longer reads green while the surplus
  scheduler is wedged.** Its verdict previously came from an activity proxy that
  shows "idle" for a stalled scheduler, so a stuck surplus loop appeared healthy —
  the same class of false-green just fixed for the ego tiles. It now reports a
  genuine stall (no completed dispatch cycle for hours, when not paused) as an
  error, and fails loud (`unknown`) if the liveness data can't be read, never green.
  Thresholds are conservative (3h floor) so a normal quiet system never false-alarms.

- **The Errors view no longer shows a clean "0 errors" when a data source is
  actually down.** The unified-errors endpoint queried each source (events, dead
  letters, deferred work, resolutions, alerts) behind a silent catch, so a DB/FTS
  outage returned HTTP 200 with zero counts and read as "data is clean". It now
  reports which sources failed (`partial` / `sources_failed`); the Errors tab shows
  a "data may be incomplete" banner and suppresses the clean-state check, and the
  overview attention list flags the degrade.

- **Operational Vitals no longer reports embedding throughput as `0` on a query
  failure.** A failed SQLite read for "Points written/24h" / "Pending queue"
  previously wrote a literal `0`, indistinguishable from a real zero. It now
  degrades to `—` with a `throughput_error` reason, distinct from Qdrant
  reachability.

- **A scheduler-heartbeat probe that cannot evaluate now surfaces a WARNING event
  instead of failing silent.** The probe's exception path previously returned
  `healthy` with no signal; it now emits a WARNING (visible on the Errors tab)
  while deliberately keeping the probe result `healthy`, so the remediation engine
  does not treat "can't evaluate" as a downed scheduler and page hourly.

- **Contributor-issue privacy scan no longer over-blocks legitimate Markdown.**
  The `scan_prose` secret-scan floor ran `detect-secrets scan --string <line>` per
  line; argparse then misread any line whose content starts with `-` (a Markdown
  `---` horizontal rule, a `--flag` example — both common in issue/PR prose) as an
  unknown option (exit 2), which the fail-closed nonzero-exit branch turned into a
  spurious BLOCK. Switched to the `--string=<value>` form so the value binds
  literally even when it starts with a dash; secret detection is unchanged for all
  other input. Locked with real-binary regression tests (a `---`/`--flag` body
  scans clean; a planted key still BLOCKs).

- **A transient `git ls-remote` failure no longer re-prompts an already-approved
  branch push.** The push-approval hook prompts only on a branch's FIRST push; a
  re-push of fixes to the same, already-published branch should be silent. But the
  "already on the remote?" check was a live `git ls-remote` that fail-closes to a
  prompt on any network hiccup, so a flaky network re-prompted every re-push. A new
  stdlib allowlist (`scripts/hooks/push_allowlist.py`, state in
  `~/.genesis/pushed_branches.json`) caches the confirmed-on-remote fact locally so
  re-pushes are decided OFFLINE. It is keyed on (branch, remote push-URL set) — never
  the remote name — so the same branch name on a different repo is never conflated,
  and it is written ONLY on a live ls-remote HIT (which proves the branch is already
  on the remote), so it can never authorize a genuine first push. Corrupt/absent
  state and any error fail OPEN to the existing prompt path; entries expire after 90
  days (a recorded branch stays trusted for that window even if its remote copy is
  later deleted).

- **A scheduled job that has run repeatedly but never once succeeded now raises a
  health alert.** Such a job was invisible to every alarm: the "silently failing"
  check needs a prior success to measure a gap against, and the consecutive-failure
  counter resets on every restart. So a job that failed from its very first run —
  e.g. a daily actuator whose external login expired on day one — could fail silently
  for weeks. A new restart-proof check (using the monotonic lifetime run/failure
  counters) surfaces any job with zero successes and repeated failures as a WARNING
  health alert (dashboard + health surface). It is deliberately out of Sentinel's
  auto-remediation scope — a never-succeeded job is a config/auth/code defect a
  service restart cannot fix — and is not auto-escalated to Telegram.

- **The career-outreach monitor now nudges reliably from what it staged, and its
  `observe` mode surfaces a dead career-agent bridge instead of failing silently.**
  The daily monitor used to re-derive its owner nudge by asking the external
  career-agent to enumerate its staged drafts — an unreliable read that came back
  empty, so newly-staged drafts were never surfaced; and `observe` mode could fail
  silently for days when the agent's login expired. It now nudges directly from the
  drafts each tick actually staged (deterministic), and `observe` is a lightweight
  reachability probe that records a job-health failure when the bridge can't answer.
  Still ships `off`.

- **FTS5 recall no longer starves on multi-word queries.** `_prepare_fts5` builds a
  bare space-separated FTS5 MATCH, which SQLite treats as an implicit AND — so a
  verbose query (`reference_lookup` / `knowledge_recall` natural-language text, and
  memory recall on its non-expanded fallback path) required *every* token to be
  present and otherwise returned nothing. A shared `db/crud/_fts.py::fetch_fts` now
  runs the precise AND query first and, only when it returns zero rows and the query
  isn't an already-structured boolean expression, retries the terms OR-joined —
  adding partial matches where there were none while leaving every already-matching
  query unchanged. Applied to the recall surfaces `knowledge.search_fts` and
  `memory.search_ranked` (the latter's `boolean=False` path, which the hot recall
  path falls back to when `expand_query` can't expand, e.g. Qdrant unavailable).
  `memory.search` is left strict-AND on purpose — its only caller resolves entity
  names by `results[0]` and must not be widened to single-term matches.
  Audited-clean: `extraction_job`'s dedup check already OR-joins; `voice/hygiene`'s
  constant-match sweep is unaffected.

- **Ego cycles no longer deadlock when the approval gate is disabled, and the
  dashboard stops reporting a stalled ego as healthy.** With
  `manual_approval_required` set to false, a leftover pending approval row
  (raised earlier while the gate was on) kept blocking both egos' pre-flight
  check forever — cycles silently stopped while every status surface still
  showed "ego active". The pre-flight now honors the gate-off setting (and the
  gate clears the stale row on the next dispatch), so cycles resume
  immediately. Separately, the dashboard and a new hourly liveness check now
  read the ego's last *completed* cycle (not the loop-alive flag), so a stalled
  ego reads "stalled" / "waiting on approval" instead of green — with a
  conservative threshold that never false-flags a legitimate slow cadence or
  quiet-hours lull. The mandatory approval gate itself is unchanged (default
  stays on; nothing auto-approves when it is on).

- **Fresh container installs now get OOM/fork-wedge protection.**
  `scripts/install.sh` (the fresh-container path) never applied the
  memory-resilience provisioning (systemd-oomd pressure-kill, swap invariant,
  raised per-user-slice `TasksMax`) that `bootstrap.sh` and `update.sh` already
  did — so a freshly installed box sat unprotected against the OOM-thrash /
  `Cannot fork` wedge until its first `update.sh` run. It now applies the same
  idempotent, adaptive provisioning at install time.

- **The core Claude Code spawner now uses the shared hardened group-kill.**
  `cc/invoker.py` — the launcher behind every CC session Genesis runs — carried
  the patterns the repo-wide sweep retired everywhere else: `preexec_fn` spawns,
  three `getpgid`-based kill paths (which leak the tree once the leader is
  reaped), two direct-child-only cleanup kills on cancellation/stdin failure,
  and an unbounded post-kill wait. All migrated to `genesis.util.proc_kill`.
  Also hardened: any non-timeout exception escaping the streaming loop (a
  callback raising, an over-limit stream line) now group-kills instead of
  leaking a detached, unregistered session; the graceful terminate-after-result
  stop is bounded and escalates to a group kill if the group survives it;
  post-kill stderr reads are bounded.

- **Dead NVIDIA NIM models retired from routing (silent free→paid fallback leak
  closed).** NIM EOL'd `deepseek-ai/deepseek-v4-pro` (HTTP 410) and made
  `moonshotai/kimi-k2.6` 404-for-account (both confirmed by live probe). Every
  chain led with a dead free provider, so once its breaker opened those ~14 cognitive
  call-sites silently fell through to paid OpenRouter fallbacks. `nvidia-nim-deepseek`
  is repointed to the live free `deepseek-ai/deepseek-v4-flash-0731` (fast, valid JSON);
  the dead `nvidia-nim-kimi` provider is removed and dropped from every chain (base
  sites fall to `groq-free`, adversarial `_challenge` sites lead with DeepSeek — model
  independence preserved). The eval `judge` keeps the calibrated paid V4-pro first (NIM
  now serves flash, not the calibrated pro), and the `38a` procedure-novelty precision
  gate is pinned to V4-pro only. A new `test_config_invariants.py` locks the dead-slug
  denylist, per-chain non-NIM fallback, `_challenge` model-independence, and a
  deepseek-family judge.

- **Subprocess timeouts no longer orphan helper process trees (repo-wide
  sweep).** Several launchers Genesis runs (the code-review helper, the
  headless/CLI/recovery-brain `claude` runners, promptfoo/pytest eval
  scorers, deterministic step commands) fork their own children; on timeout
  the old kills reached only the direct child, leaving the rest of the tree
  running until reboot. All eight spawn sites — plus the original autonomy
  reviewer, migrated off its private copy — now share one hardened guarded
  group-kill (`genesis.util.proc_kill`): own process group via
  `start_new_session` (never `preexec_fn` — post-fork deadlock risk in a
  threaded server), `killpg` on the leader pid directly (immune to the
  leader-already-reaped race), a `pgid<=1` safety guard, a bounded reap, and
  a logged fallback when the group kill is refused. The contribution CLI
  additionally group-kills on Ctrl+C so an interactive abort can't strand
  its reviewer. The delivery `git push` — which runs under the autonomy
  executor's single-slot semaphore — also gained a hard 300s bound, closing
  the last unbounded subprocess wait on that critical path (a
  network-stalled push could previously wedge all autonomy task execution).

- **Inbox approval-request storm ended.** A stale-hash defect made the inbox
  monitor see phantom "modified" files every 30-minute scan, each time
  cancelling the pending approval and sending a fresh Telegram request — up to
  48 messages a day. The known-hash map is now a single recency scan (newest
  decisive row wins), and new/changed inbox content while a request is pending
  parks onto the SAME request instead of cancelling it: one approval message,
  ever, per outstanding batch — and approving once evaluates everything
  outstanding at that moment.

- **Inbox approvals never re-ask.** Delivered inbox approval requests send no
  reminders (per-policy `reask_overrides` in `autonomous_cli_policy.yaml`,
  `0` = never; other approval types keep their 24h re-ask). A request whose
  Telegram delivery FAILED still retries each scan until one send succeeds —
  that's recovery, not a reminder.

- **Rate-limit-parked background sessions resume faithfully.** A parked
  session's re-dispatch now carries its full execution shape (system prompt /
  strategy doc, attribution tag, skills) instead of resuming with defaults,
  and campaign bookkeeping follows the park to the delivering session's real
  result instead of recording a false failed run (bounded at 7 days so a stuck
  resume can never stall a campaign forever).

- **Test runs no longer trip the temp-protection watchdog.** pytest writes its
  scratch tree under `$TMPDIR`, which on a Claude Code session is the
  budget-policed `~/.genesis/cc-tmp`; a broad suite could fill it and drive the
  `genesis-tmp-watchgod` service into a sustained high-pressure state. pytest is
  now redirected to `~/tmp` (off the budget) for every run rooted in the repo,
  the dev console, autonomy verification, and the eval gauntlet — CI is
  unaffected.

- **Temp watchdog no longer loops on non-reclaimable pressure.** When
  `~/.genesis/cc-tmp` stays over budget after the watchdog's cache cleanup, it
  now re-measures before considering any idle-session reap (so it never reaps a
  session that cleanup already made unnecessary) and, if nothing is reclaimable
  and nothing is safely reapable, raises a single alert instead of re-evaluating
  every poll.

- **Contribution secret-scanning now actually blocks leaked secrets.** The sanitizer
  that checks community-contribution diffs before opening a public PR ran two secret
  scanners — detect-secrets (the required floor) and gitleaks — but both were silently
  finding nothing: detect-secrets' output parser missed every hit because it didn't
  account for the confidence/entropy suffix in the tool's output, and gitleaks was
  invoked with a flag combination that made it scan nothing from its input. A diff
  containing an API token or private key could pass the sanitizer clean. Both scanners
  now work (gitleaks also loads the repo's custom PII rules), and new tests exercise the
  real scanner binaries so this can't silently regress. The privacy scanners for IP
  addresses, emails, and install fingerprints were unaffected. The gitleaks layer was
  further hardened after a security review: a scanner error (bad config, unexpected
  exit) now surfaces a visible warning instead of silently reporting "clean"; the
  scanner's own rules file is pinned to the committed version and is itself on the
  contribution-forbidden list, so a contribution can't weaken the gate that scans it.

- **Autonomy no longer wedges when its cross-vendor reviewer hangs.** A task's
  quality gate runs an adversarial verification through a `codex exec` subprocess.
  That call had no timeout, so a hung codex (a known upstream model-catalog-refresh
  hang) would hold the autonomy executor's shared execution slot indefinitely —
  stalling every queued task until a restart. The call is now bounded by a hard
  timeout (default 2h, override with `GENESIS_CODEX_REVIEW_TIMEOUT_S`); on timeout
  the codex process tree is killed and verification degrades to the next reviewer in
  the chain, freeing the executor. Mirrors the existing hard-timeout on deterministic
  executor subprocesses.

- **No more false "critical failure" alarms when the system is briefly busy.** The
  health signal that watches your local infrastructure (database, vector store, and
  Ollama if enabled) probes those services with a short timeout. When background work
  momentarily stalls Genesis's event loop, those probes could time out even though the
  services were perfectly healthy — firing a spurious "critical failure" that triggered
  a reflection and a Telegram alert. Genesis now recognizes when a probe timed out
  because the loop was starved (rather than because a service is actually down) and
  suppresses the false alarm, while still firing on a genuine outage. A new diagnostic
  also captures what code was blocking the loop during such a stall, to help track down
  the underlying cause.

- **Restricted reasoning sessions can no longer escape their tool restrictions by
  spawning.** Several of Genesis's restricted Claude Code sessions (deep/strategic
  reflection, the inbox/mail judges, and the experimentation completion) could spawn a
  subagent that ran with full, unrestricted tools — escaping the restrictions placed on
  the parent. Their denylists blocked the obsolete subagent-spawn tool name but not the
  current one, nor the `Workflow`/`Skill` spawn paths. These denylists now deny the whole
  spawn class (subagent, workflow, and skill spawns) from a single shared definition,
  with a guardrail test so a new session can't silently reopen the gap. (Working
  background sessions that legitimately orchestrate — e.g. the deep-research `Workflow`
  path — are intentionally out of scope and tracked separately.)

- **Inbox/mail evaluation judges further hardened against adversarial external input.**
  Both judges reason over untrusted content (emails / dropped inbox items) with
  permissions skipped. The mail judge — whose prompt uses no tools at all — now runs a
  full act-nothing denylist (shell, all file-edit, subagent-spawn, side-effecting actions,
  and web tools), and a stale config-path bug that pointed its empty-MCP profile at a
  nonexistent file was fixed. The inbox judge now denies every memory/settings write tool
  (it only ever needed reads plus a single optional observation write), closing a path
  where injected content could mutate stored memory or settings; the denial is derived from
  the reflection read-only denylist, so a future write tool is auto-covered. Two narrower
  residuals on the inbox judge remain tracked (not closed here): it keeps shell access for
  one job — fetching YouTube links — and its retained observation writer is not yet
  provenance-stamped or type-constrained; both are handled in follow-up work.

- **The autonomous-CLI approval gate now ships ON by default.** Every background
  Claude Code session Genesis dispatches must be rooted in an explicit user
  approval — but the committed policy config shipped the gate *off*, so a fresh
  clone would auto-approve autonomous sessions without asking. The shipped default
  is now `manual_approval_required: true`, and a guardrail test pins the committed
  config so the loader's file-wins-over-code-default behavior can never silently
  ship the gate off again.

- **Inbox approvals no longer nag.** A pending "inbox evaluation" approval now
  holds until you respond — it is asked once and blocks until approved, like
  every other approval, instead of re-sending a fresh request every few hours
  for content that hasn't changed. A stuck (orphaned) approval that can never be
  dispatched is still auto-recovered, so the monitor never wedges.

- **A "free" fallback model that was quietly a paid one.** An OpenRouter fallback
  used by several background steps was labeled free but pointed at a paid model, so
  on the rare occasions it was reached it could incur spend that Genesis recorded as
  $0. It now uses a curated pool of genuinely-free models with automatic failover,
  and Genesis warns at startup if any provider marked "free" actually points at a
  paid model — so cost tracking can't silently miss real spend. No action needed on
  your end.

- **Replying "yes" to an approval topic no longer starts a confused new chat.**
  If you replied "yes"/"approve" to the *topic itself* (the forum topic header)
  instead of the specific approval/proposal/content message, Genesis got no
  context and spun up a fresh conversation that answered "I don't have anything
  to confirm — what are you saying yes to?". It now recognizes that case and
  asks you to reply to the specific message (or tap its ✅ button) rather than
  guessing — it deliberately won't act on an ambiguous topic-level reply.

- **Deep reflections no longer silently lose their output.** When a deep
  reflection ended its session with a plain-prose wrap-up instead of the
  required structured JSON (~40% of runs), both parsers failed: the Telegram
  topic showed a "not parseable" stub and — worse — that cycle's cognitive
  output (updated context summary, observations, memory consolidations, and
  follow-up research it wanted to queue) was discarded. Genesis now re-derives
  the structured result from the prose in one follow-up model call, so the
  reflection's findings are kept and the topic shows a real summary. If the
  salvage can't recover valid output, behavior is unchanged from before.

- **Engagement rate now measures real outreach, not your own approval pings.**
  The "N sent / X% engagement" figure counted every internal Telegram message
  Genesis sends *you* — approval prompts, the morning digest, blockers, alerts,
  surplus research posts — as "outreach," so the denominator filled with
  housekeeping and the engagement rate read near-zero even when genuine posts got
  normal reactions. It now counts only messages sent to the outside world (your
  external channels — Discord, email, and the like — rather than your own
  Telegram), so the rate (on the dashboard, in the awareness signal, and in
  reflection) reflects how your actual outreach is landing.

- **Email replies now count as engagement.** When someone replies to an email
  Genesis sent (outreach pitch, follow-up), the reply was recorded for thread
  tracking but never registered as an engagement outcome — so reply rates read
  as zero and the prediction ledger graded every real reply as silence. The
  reply poller now writes the engagement back to the outreach record (without
  overwriting a richer outcome you set manually), so reply metrics and ledger
  calibration reflect reality. Automated messages (out-of-office responders,
  bounces, list mail) and replies from an address other than the one contacted
  are filtered out, so they can't inflate the reply rate.

- **Background work now survives Claude session/weekly limits instead of failing.**
  When Claude Code hit its Max-plan session or weekly usage limit, the message
  ("You've hit your session limit · resets 4:10am") wasn't recognized as a limit
  at all — it was treated as a generic crash, so the background session died and
  its work was lost rather than being parked to auto-resume when the limit reset.
  These limits are now recognized, and the resume is scheduled from the real
  reset time in your account's own timezone (previously a bare reset time could
  be read in the server's timezone and land hours off).

- **No more spurious embedding error on restart.** A one-time procedure-embedding
  repair runs shortly after boot; on some installs it fired before the network was
  warm and logged a scary "all embedding backends failed" error with a full
  traceback on every restart, even though it harmlessly retried on the next boot.
  The repair now waits for boot I/O to settle and retries a cold-start blip before
  giving up, and a not-yet-ready dependency is logged as a quiet, tracebackless
  "will retry next boot" notice rather than an error. Operators can tune or disable
  the wait with `GENESIS_DATA_MIGRATION_BOOT_DELAY_S` (seconds; `0` to disable).

- **Re-embedding a memory no longer downgrades its recall ranking.** When a
  memory's vector was rebuilt (after a vector-store outage, or by the nightly
  repair job), its priority class was silently recomputed from the text alone —
  discarding any deliberate classification and, for reference-style entries,
  re-applying a ranking penalty that lowered how often they surfaced. Rebuilds
  now preserve the stored class. Installs that already drifted are healed
  automatically on update: a one-time pass re-syncs each memory's stored class
  onto its vector (a clean no-op if nothing drifted).

- **Learned procedures no longer overwrite each other.** Genesis identified a
  stored "how-to" procedure by its coarse topic label alone, so two genuinely
  different lessons that happened to share a label would silently replace one
  another — each new lesson destroying the previous one under the same row. On
  one install a single `code_review` row had absorbed (and lost) ~30 distinct
  lessons this way. Procedures are now matched by the similarity of the lesson
  itself: a genuine refinement still updates in place, but a distinct lesson is
  kept as its own procedure instead of overwriting an unrelated one.

- **Legacy procedures with stale similarity vectors are re-embedded on update.**
  The procedure-overwrite fix above matches lessons by similarity, but a
  procedure edited many times before that fix shipped kept a similarity vector
  describing an *older* version of the lesson — which could still misjudge a new
  distinct lesson as "the same" and overwrite it. Installs are healed
  automatically on update: a one-time pass re-embeds each repeatedly-edited
  procedure from its current text so the matching is trustworthy (a clean no-op
  where nothing was stale).

- **Deleting a memory no longer risks leaving an orphaned vector behind.** A
  memory lives across SQLite and a vector store; if the vector store hiccupped
  mid-delete, Genesis used to remove the memory's records but leave its vector
  stranded — invisible bloat that could subtly pollute search, and it never
  cleaned itself up. Deletes are now ordered vector-first and fail closed: if the
  vector store is unavailable the whole delete is deferred and retried, so a
  memory is never left half-removed. A one-time startup cleanup removes any
  existing orphaned vectors (their contents are exported to `~/.genesis/output`
  first) and restores memories that had lost their vector back to full search —
  healing the drift the new Memory Integrity checks surface.

- **Rebuilding a memory's vector no longer resurrects a superseded one.** When a
  missing vector is rebuilt, Genesis re-stamps it with the memory's current
  state — so a memory that was retired or superseded while its vector was still
  pending stays excluded from recall instead of quietly reappearing, and its real
  confidence is preserved rather than reset to a default.

- **Internet outages no longer leave a mess behind.** A long connectivity loss
  used to pile up hundreds of duplicate queued alerts, spam the health view with
  false "delivery exhausted" warnings, and — worst — put Genesis in an endless
  restart loop (it announced "going offline" over and over because restarting
  can't fix a dead network). Now: a repeated delivery failure for the same alert
  is de-duplicated instead of re-queued; a duplicate that was already delivered,
  or an email held for your approval, is treated as done rather than retried into
  a false failure; deferred messages actually expire on their 4-hour deadline
  instead of lingering forever; and the watchdog, after restarting a few times
  for the same reason, backs off and sends you one warning instead of restarting
  on a loop.

- **Genesis now sheds low-priority background work when its providers are
  struggling.** The degradation system that's meant to skip non-essential work
  (surplus brainstorms, the morning report) during a provider brownout was wired
  up but never actually triggered on provider failures — it does now, so a rough
  patch for the model providers no longer drags every background task down with it.

- **Genesis's safety guardrails work again.** The hooks that block dangerous
  actions in Claude Code sessions — force-pushing, `rm -rf` on your data
  directories or database, committing to `main` without a review, writing to
  protected files, booting Genesis against a worktree — had gone silently inert:
  a change in how Claude Code hands data to hooks meant they read an empty input
  and waved everything through. They now read the input correctly and block as
  intended, with a regression test that feeds each guard a real payload so a
  future Claude Code change can't quietly disable them again.

- **Those revived guardrails no longer block harmless commands.** With the guards
  working again, two pre-existing over-eager checks surfaced: the `rm -rf` guard
  mis-read the *rest* of a command — a `2>/dev/null`, a `> log`, or a second line
  — as if it were another path to delete, so a perfectly safe deep-path cleanup
  got blocked; and the "don't pipe a backgrounded command" check counted a
  logical-OR (`a || b`) as a pipe and blocked it too. Both now parse the shell
  correctly, so legitimate commands go through while genuinely dangerous ones
  (`rm -rf /` with or without a redirect, a real pipe in the background) are still
  stopped — covered by regression tests for each construct.

- **When a scheduled job fails, Genesis now records what actually went wrong.**
  Job failures were logged with only the job's name and whatever text the error
  happened to carry — and for the most common failures that text was *empty*, so
  the record read "Scheduled job 'memory_extraction' failed:" with nothing after
  it. Failures now carry the error type and the code location, in both the event
  log and the job-health view, which is the difference between a failure you can
  diagnose and one you can only count. Genesis also now distinguishes a bug in
  its own code from an outside blocker (a provider outage, a rate limit) rather
  than filing both the same way — so "the API was down" no longer looks like
  something to go fix in the code.

- **A failed update that had already run database migrations now rolls the
  database back too.** Previously a rollback restored the code and dependencies
  but left the (newly migrated) database in place, so the rolled-back older code
  ran against a newer schema. Rollback now restores the pre-update database
  snapshot whenever migrations ran (the server is stopped at that point, so it's
  a clean swap), reloads systemd units, and states plainly what it did and didn't
  revert.

- **Recovering from a failed update actually brings the server back.** If a
  previous update failed and left the server stopped, the next update used to
  finish and report "success" while the server stayed down and health was never
  checked. It now detects a recovery run (from the leftover failure record) and
  restarts + health-verifies the server. A server the operator deliberately
  stopped is left alone, but recorded as not-running rather than a bare success.

- **A large one-time data cleanup no longer briefly freezes the running system.**
  Post-startup data migrations (one-off cleanups/backfills of stored knowledge and
  history) run alongside the live system, which allows only one writer to the
  database at a time. A big cleanup used to do all its work in a single long write,
  briefly blocking every other write for ~10+ seconds — long enough that the system
  logged "database is locked" errors and, in one case, had to re-run the cleanup
  after a restart. Bulk cleanups now save their progress in small batches, releasing
  the database between them, and the bookkeeping that records a migration as "done"
  now retries briefly if it hits a momentary lock — so a cleanup is never
  needlessly repeated. (Slow per-record checks were also moved out of the locked
  window.)

- **The dashboard now recognizes updates started from the command line.** Its
  "update in progress" checks previously only saw dashboard-triggered updates,
  so a command-line `update.sh` run could be interrupted (its state wiped) or a
  second update launched over it. The dashboard now consults the same
  deploy-in-progress signal the rest of the system uses, and a dashboard-started
  update no longer runs in the server's own service group (where the update
  stopping the server could kill the update itself).

- **Two updates can no longer run at the same time.** If an update is already in
  progress, a second `update.sh` (from another session, or the dashboard) now
  refuses immediately instead of running concurrently — previously two updates
  could overlap, each stopping the server and merging, and corrupt the deploy.

- **An interrupted update no longer leaves the server down.** If a self-update
  is interrupted after it has stopped the server (a Ctrl-C, a system shutdown,
  or an unexpected failure inside an internal step), it now rolls back to the
  previous version and restarts the server instead of exiting with the service
  stopped. An interrupt *before* the server is stopped simply cleans up and
  exits, leaving the running system untouched.

- **Background sessions no longer get silently cut off after 10 minutes.** A
  long background task (for example deep research running as a background
  session) used to be killed at about 10 minutes with only a partial result and
  no signal. Background sessions now run to completion within their time budget,
  and if any background work is ever cut short by a time limit, the result is
  flagged as incomplete rather than delivered as if it were finished.

- **Updates are more resilient to network stalls, bad merges, and mid-update
  crashes.** Several robustness fixes to the self-update path (`update.sh`):
  network operations (fetching the latest code, post-update health checks, and
  guardian SSH) are now time-bounded, so a hung connection can no longer stall
  an update indefinitely; the pre-update database snapshot is now a
  transactionally-consistent SQLite backup instead of a plain file copy that
  could be torn if the server wrote to it mid-copy; a merge conflict now records
  complete, valid conflict details for the assisting session (multi-line git
  output no longer corrupts that file); and an update that ships a broken
  database-migration module now rolls back cleanly instead of silently skipping
  migrations and running the new code against an old schema.

- **Host setup no longer force-deletes a container it wrongly thinks is
  damaged, or hides an install behind a new disk.** Host-side hardening: a
  container flagged "damaged" is now **renamed aside** (its database, memory, and
  transcripts preserved and reclaimable) instead of force-deleted, and a single
  transient health-probe blip no longer misclassifies a healthy container (the
  probes retry). A split-disk resize only binds the larger disk when the
  container does not already hold an install — never over an existing one, which
  would make it "disappear." Also: the guardian-state reset and `~/.claude`
  ownership now use the real operator account under `sudo` (not root); an
  existing operator-edited `guardian.yaml` is preserved on re-run instead of
  overwritten; a failed shared-mount step degrades gracefully instead of aborting
  the installer mid-way; and re-launching setup to pick up group membership no
  longer mangles arguments containing spaces.

- **Setup and restore scripts no longer risk destroying user data on a re-run
  or crash.** Three install-surface fixes: re-running local-config setup now
  preserves your existing `github.private_repo` and any custom keys (it rebuilt
  the file from scratch before, wiping them) and writes atomically; the shell
  wrapper that setup installs in `~/.bashrc` is rewritten atomically and, if it
  finds a half-written block from an earlier interrupted run, leaves the file
  untouched instead of deleting everything below it; the CC-memory restore never
  overwrites a newer local file with an older backup copy (a `cp` quirk on newer
  systems used to); and local-config setup fails with a clear "install PyYAML"
  message up front instead of a raw traceback after you've answered every prompt.

- **The knowledge base no longer fills with Genesis's own operational
  telemetry.** Background maintenance and eval tasks (DB maintenance, disk
  cleanup, model/J9 evals, backup verification, research/prompt-review
  intermediates) were routing their point-in-time status reports into the
  knowledge base as if they were durable, recallable knowledge — growing it to
  ~71% operational noise and crowding out real ingested content. Those tasks no
  longer write to the knowledge base, a one-time cleanup removes the historical
  telemetry rows on the next restart, and crawled external intelligence
  (model/GitHub/web scans) is now correctly labelled as external-world content
  rather than Genesis's own memory.

- **Per-prompt memory recall no longer silently degrades on busy installs.**
  The server-side recall budget behind the proactive memory hook was sized
  against a dev install that (unnoticed) ran no reranker and a half-size
  corpus; on a loaded production install the real pipeline routinely exceeded
  it, so most prompts fell back to keyword-only recall with a
  `[Memory·degraded]` banner. The budget now matches the measured production
  cold path (4.5s server / 4.75s client, still inside the hook's 10s ceiling),
  the cross-encoder rerank stage is timeboxed at 1s (degrading to fusion order
  rather than eating the whole budget), recall responses report whether
  reranking actually **executed** (not merely was requested — the
  requested-vs-executed confusion is how the old budget got validated), and
  slow recalls log a per-stage timing breakdown to the journal.

- **Restore fails loudly instead of silently doing nothing, and decrypted
  secrets are never briefly world-readable.** Disaster-recovery hygiene fixes:
  an unattended restore with no terminal (and no `--force`) now aborts with a
  clear message instead of declining every prompt and reporting success; a
  restore pointed at an empty or wrong backup fails instead of exiting "success"
  having restored nothing; a failed pull of the two largest payloads (vectors,
  transcripts) is now reported rather than silently skipped; decrypted
  secrets/transcripts/memory are written private-by-default (no world-readable
  window); the backup-failed alert now fires even when a backup aborts early;
  and the plaintext database dump is cleaned up even if a backup dies mid-run.

- **Backups now verify the database archive is restorable, can't collide with a
  restore, and never re-badge stale data as fresh.** Three disaster-recovery
  integrity fixes: the 6-hourly backup now decrypt-verifies the SQLite archive
  with the passphrase a recovery box would actually use, so a
  rotated-but-not-re-escrowed passphrase is caught immediately (and the fresh
  copy is held out of the off-site snapshot until re-escrowed) instead of
  surfacing at disaster time; backup and restore share a lock so the timer can
  never snapshot a half-restored database (a backup skips quietly, a restore
  waits then says who's holding the lock); and the off-site snapshot only ever
  contains payloads regenerated that run — a reachable collection that fails to
  snapshot now fails the backup loudly instead of silently shipping the previous
  run's copy under a fresh timestamp (a genuinely absent or unreachable vector
  store degrades gracefully, since it rebuilds from the database). All off-site
  operations are time-bounded, so a hung network mount degrades to a
  partial-backup alert instead of wedging backups indefinitely.

- **The dashboard no longer shows a false "degraded / sentinel stale" during an
  update.** While `update.sh` restarts the server, the freshly booted server's
  sentinel heartbeat is briefly empty, which used to paint the Services card
  amber ("sentinel stale — last heartbeat Nm ago") even though nothing was
  wrong. The health snapshot now recognizes an in-progress deploy (the same
  signal the watchdog already uses to defer restarts) and shows a neutral
  "deploying" state for that window instead — a genuine fault (an escalated
  sentinel, a down service) still shows through.

- **Voice conversation delivery no longer double-writes turns under a burst.**
  The `POST /v1/voice/conversation` landing did a read-then-append (count the
  transcript's lines, then write the new turns) with an `await` in the middle.
  Because the dashboard serves requests on threads that all feed one event
  loop, two near-simultaneous deliveries of the same conversation (a voice
  edge can fire the same disconnect twice within a second) could both read the
  same line count and both append the same turns, duplicating them in the
  transcript. Deliveries now hold a lock across the read-and-append so the
  turns land exactly once.

- **Legacy voice conversation blobs are swept from episodic memory.** The old
  one-blob landing left duplicated, ever-growing "Voice conversation [...]"
  memories polluting recall (and their vector embeddings polluting semantic
  search). A daily voice-hygiene job now removes them across all storage
  layers; it runs as a standing sweep (not a one-shot migration) so blobs
  written by a voice edge that hasn't been updated yet are cleaned up too,
  and it logs loudly when it finds any — a nonzero sweep after your edge is
  current means a stale producer is back.

- **`update.sh` no longer aborts on Serena's config churn.** `.serena/project.yml`
  is now install-local (untracked): Serena rewrites the file's comment block on
  its own version bumps, so any install running the Serena MCP went permanently
  "dirty" and every update required a manual stash dance. The updater carries
  your live copy through the transition automatically, and fresh clones need
  nothing — Serena regenerates the file on first run.

- **Dashboard health cards stop crying wolf.** The API Keys and Queues cards
  read "degraded" whenever *any* provider key was unconfigured or *any*
  deferred-work item was queued — even when nothing was actually wrong. Both
  now trust the system's own criticality- and age-aware verdict: the API Keys
  card stays healthy when the only missing keys belong to dormant or
  fallback-only providers (and still degrades when a genuinely load-bearing
  key is missing or a provider is out of credits), and the Queues card
  degrades on genuine backlog signals — recovery work, or a processing /
  embedding queue past the backend's own depth threshold — rather than the
  normal in-flight worklist the background drainer churns through.
  The API Keys "N ok / M" tally is also correct now (it previously double-
  counted local providers, so the numbers didn't add up).

- **The Ego card says "needs action" instead of "degraded" when approvals are
  waiting.** Pending ego proposals are a review queue awaiting you, not a system
  fault — so the card no longer paints itself amber-degraded when more than a
  handful pile up. It shows a distinct "needs action" state (its own accent
  colour and ◆ glyph), and pending approvals no longer drag the overall
  dashboard health to "degraded" — they ride along as a note on an otherwise
  healthy system.

- **A long-retired background job no longer haunts the job-health view.** The
  infra-monitor job was replaced months ago, but its stale record lingered and
  showed as a perpetually-"healthy" job (the staleness check only catches jobs
  that run-but-fail, not ones that stopped running entirely). Its fossil record
  is now purged on upgrade.

- **Provisioning approvals can be retried, and never race each other.** A
  grow/limits approval prompt that timed out unanswered used to silently block
  every retry for 24 hours (the generic outreach dedup window treated the
  retry as a duplicate of the expired prompt). Provisioning approvals and
  outcome notices are now never deduplicated — every request reaches you —
  and a new in-flight guard suppresses a genuinely concurrent duplicate
  prompt (e.g. a double-click) so a plain APPROVE reply always resolves
  unambiguously.

- **Messages Genesis sends you now arrive exactly as written — no silent
  rewriting.** Notifications, reminders, and reply-and-wait prompts were
  quietly run through an LLM "drafter" before delivery, which could reword or
  even invert their meaning: a test message asking "please reply with a plain
  message" went out as "…failed, reply to verify," inventing an alarming
  status that was never there. Delivery paths (`outreach_send`, the
  reply-and-wait tool, the queued-message drain) now deliver the composed text
  verbatim, and the internal notification paths that relay a machine fact
  (health-remediation alerts, "update available/failed," session-failure
  alerts, ego notifications, surfaced reflection questions) do the same — so a
  factual alert can never be creatively rewritten into a false claim. Reflection
  questions also keep their full text and every option intact. Generative
  content (marketing drafts) still uses the drafter, as intended.

- **Memory search actually reranks now.** `memory_recall` and
  `knowledge_recall` advertised Voyage cross-encoder reranking and defaulted it
  on, but the recall tools were built without a reranker, so it silently never
  ran — searches returned raw fusion order instead of the promised
  relevance-reranked results. The reranker is now wired into the recall tools
  (no change without an `API_KEY_VOYAGE`). It can be turned off live via the
  `memory_recall` setting `reranker.mode: off` or `GENESIS_MEMORY_RERANK_OFF=1`
  if you want to trade a little recall quality for lower latency/cost.

- **Fresh installs come up correctly the first time.** Several install-only
  defects are fixed: generated systemd units no longer get a broken service
  PATH (missing the npm-global bin dir) when Claude Code isn't on PATH yet at
  unit-generation time, so background services can reliably find the `claude`
  CLI; the triage-calibration and user-knowledge seed files are now populated
  from their templates during `install.sh` (previously only `bootstrap.sh` did
  this), so triage runs with real calibration instead of an empty prompt on a
  fresh install; and the content-pipeline module now honors its declared
  enabled state instead of always seeding disabled.

- **Time-limited internal state expires on schedule.** Two time-to-live checks
  compared expiry timestamps stored in different formats: observations could be
  resolved as expired up to a day early, while same-day cognitive-state entries
  lingered in the morning report as "active" long after they had actually
  expired. Both comparisons are now format-normalized, so internal state lives
  exactly as long as intended.

- **The morning report's numbers are real now.** Report generation previously
  counted truncated display lists (reporting "5 follow-ups" when 268 existed),
  sometimes inverted protective facts into alarms (an active OOM-protection
  service reported as an OOM risk), and — worst — the carefully grounded
  draft was silently re-drafted by a generic model with no grounding rules
  before delivery. The report context now opens with an authoritative
  Ground Truth section of exact totals, truncated lists are labeled
  "showing N of M", protective mechanisms are tagged so they can't be read
  as risks, and the grounded draft is delivered as-is (single draft pass).

- **Reflections stop arguing with themselves about signals that never
  fired.** All reflection depths now see live awareness signals in one
  canonical format (previously light and deep cycles each got a different
  shape, so one cycle couldn't recognize what the other had cited), the
  prompt now clearly separates live tick signals from stored-observation
  history, and a guard strips any signal-by-name-and-value claim from a
  reflection's persisted narrative when that signal wasn't actually in the
  live tick. This ends the loop where a phantom claim ("signal X=0.9") got
  written into cognitive state, re-read by the next reflection, debunked,
  and then re-asserted for days. The guard only annotates — it never blocks
  or rejects a reflection's update.

- **Reflection updates in Telegram are now real summaries.** The reflection
  topic previously relayed the model's raw output, so a malformed reflection
  could leak internal tool-call chatter to your Telegram verbatim. Messages
  are now built only from the parsed reflection fields (assessment, key
  observations, next focus); when a reflection's output can't be parsed you
  get a short "completed — stored for review" notice instead of noise, and
  unparseable output is no longer stored as a reflection summary that later
  reflections would re-read and argue with.

- **Demoted autonomy can actually earn its way back now.** Earn-back
  eligibility used to be computed over a category's entire lifetime record,
  so after a rough patch the math could require months of flawless behavior
  before Genesis would even *propose* restoring a level — in practice the
  demotion was permanent and the system nagged about it forever. Eligibility
  now looks at a recent evidence window (45 days by default,
  `earnback.window_days` in `config/autonomy.yaml`): old mistakes age out,
  recent clean behavior counts, and promotion still always requires your
  explicit approval. While an earn-back proposal is sitting in your queue,
  the internal "autonomy regressed" alarm also calms down instead of firing
  on every awareness tick.

- **The engagement-outcome vocabulary is now actually enforced.** The
  database rule meant to constrain how outreach engagement gets labeled was
  silently toothless (a SQL quirk made every value pass), so labels drifted
  for months and anything could be written. The table now enforces the
  canonical vocabulary, historical stragglers were normalized, and the two
  doors that passed raw client strings straight through (an MCP tool and a
  dashboard endpoint) validate first — a bogus value gets a polite rejection
  instead of a crash.

- **The host recovery brain no longer goes blind on a misconfigured work
  directory.** If the guardian's configured Claude Code work directory already
  exists but isn't writable by the guardian (for example a root-owned
  `/var/lib` path left over from an older install), it now detects that with a
  real write probe and falls back to a user-writable directory instead of
  handing the recovery session an unusable working directory. Previously only a
  *non-creatable* directory triggered the fallback; an existing-but-unwritable
  one slipped through and could blind the recovery brain exactly when it was
  needed most.

- **Disaster recovery no longer risks corrupting the thing it's recovering.** A
  script audit found three ways deploy/restore could bite at the worst moment,
  now fixed: (1) during a database restore, the health watchdog could restart
  the server mid-rebuild — into a half-populated database that the next backup
  would then capture as the newest "complete" snapshot; restore now holds the
  same deploy-in-progress marker the watchdog already honors, so it stands down
  until the restore finishes. (2) The pre-restore "undo" copy was taken from the
  live database without its write-ahead log and then the original was deleted —
  leaving a torn, stale rollback copy exactly when an operator needs to undo a
  bad restore; the copy is now taken after the writer is stopped, via a
  WAL-aware snapshot. (3) The Guardian installer aborted on any host without
  Claude Code already installed — which is every fresh host, since the installer
  runs before Node/CC are set up — because an "optional" CLI probe wasn't guarded
  under strict mode; it now degrades gracefully as intended.
  sending on external channels (email, chat), Genesis scans messages and
  quarantines anything that looks like a leaked credential. Its API-key
  patterns predated today's key formats, so newer shapes slipped through
  unflagged — OpenAI project and service-account keys (`sk-proj-…`,
  `sk-svcacct-…`, `sk-admin-…`), OpenRouter keys (`sk-or-…`), and underscored
  key bodies. The patterns now cover the full modern `sk-*` family, GitHub
  tokens (`ghp_…`/`gho_…`/`github_pat_…`) are recognized for the first time,
  and benign look-alikes (hyphenated slugs such as "sk-learn-pipeline") stay
  unflagged.

- **Idle abandoned sessions can now actually be cleaned up.** The process
  reaper's "is anyone looking at this terminal?" check counted every tmux
  pane as live — including sessions nothing is attached to. Under
  persistent slot sessions that meant an abandoned session could never be
  reclaimed, no matter how long it sat idle. A tmux pane now counts as live
  only while its session has a client attached; a detached session is still
  spared as long as it shows recent activity (so a dropped connection
  mid-work is never at risk), and only one that is BOTH detached and idle
  past the 7-day window becomes a cleanup candidate. The reaper remains in
  observe-only mode — it reports what it would clean up and touches nothing
  until explicitly armed.

- **Answering Genesis's questions with a plain message now actually works.**
  When Genesis asked something and waited for your answer (approvals,
  provision prompts, send-and-wait questions), an internal ordering bug left
  the waiting mechanism blind to where the question had been delivered — so
  a plain (non-quote) reply never matched it. Your answer instead spawned an
  unrelated conversation turn, and the question sat unanswered until it
  timed out. The delivery context is now attached in the right order, plain
  replies match the question they answer, and a tripwire warning fires if
  this ordering ever regresses.

- **Replying to Genesis without quote-replying now counts.** When Genesis
  asked you something on Telegram and you answered with a plain message
  (no quote-reply), your answer reached the waiting conversation but the
  outreach record never learned it was answered — it would later be marked
  "ignored" or "ambivalent" as if you'd said nothing. Only 3 of 1,021
  outreach records ever carried a real reply signal because of this. A
  standalone reply that resolves a pending question is now recorded on the
  outreach record exactly like a quote-reply, so Genesis's picture of what
  you actually respond to stops being systematically wrong.

- **GitNexus stops rewriting your instruction files.** Every reindex used to
  inject a block of "MUST run impact analysis before every edit"-style
  mandates into CLAUDE.md and AGENTS.md (contradicting the project's own
  advisory-tools principle) and regenerate its skill files, leaving the
  working tree dirty enough to block deploy pulls. Injection is now disabled
  at the source via a committed `.gitnexusrc` (`skipAgentsMd` + `skipSkills`)
  that reaches every install with a plain pull; AGENTS.md is hand-curated
  (useful GitNexus pointers kept, mandates gone), and the hourly strip job
  stays as a safety net for rc-unaware GitNexus versions — now covering both
  files.

- **Genesis resumes learning about you.** The stream of "user model deltas" —
  the small observations reflections make about your preferences, constraints,
  and working style — had been effectively dead since the v3 release: the
  quality gate demanded more certainty (0.90) than the reflection model ever
  expresses (its honest "high certainty" sits at 0.85), so almost nothing
  passed — 2 deltas in 3.5 months. The gate now matches the model's real
  confidence scale, and the reflection prompt no longer contradicts itself
  about the bar (it demanded 0.9, called 0.85 "high certainty", and showed a
  0.8 example all at once). If the stream stays silent anyway, the staleness
  alarm shipped in the sensor-fabric release will say so within two weeks.

- **Setup now installs the package its OOM protection depends on.** Genesis's
  memory-pressure protection (systemd-oomd) was applied by setup but only *if*
  the `systemd-oomd` package already happened to be installed — on a minimal
  install where it wasn't, setup quietly skipped the whole layer and left the
  box exposed to the exact OOM-thrash wedge the protection exists to prevent,
  with no signal beyond a line in setup output nobody reads. Bootstrap now
  provisions the package before applying the layer, so the protection actually
  deploys everywhere. Idempotent (a no-op when already present) and it never
  forces oomd on a kernel that can't support it.

- **Wing-filtered memory recall stops missing memories it should return.**
  Asking for memories in a specific wing (e.g. `infrastructure`) silently
  under-returned two kinds of rows: memories with no vector (FTS-only) were
  dropped outright because the wing filter had no wing to check against them,
  and thousands of older embedded memories were excluded because their vector
  carried no wing tag even though their record did. Recall now checks each
  memory's authoritative wing (from its stored record) rather than a
  denormalized copy, so FTS-only rows are reachable; and a one-shot supervised
  re-sync (`scripts/wing_payload_resync.py`) backfills the missing wing tag
  onto ~5.3K older vectors so vector-based wing recall returns them too.
  Dry-run by default; the bulk re-sync is gated on a human-reviewed sample.

- **Enabling container swap no longer waits for a restart to take effect.**
  Host setup lets the container's memory cgroup spill into host swap under
  pressure, so a memory spike degrades into swapping instead of thrashing the
  whole box into a wedge. But that setting only took effect the next time the
  container *started*, so retrofitting an already-running install looked done
  while swap stayed off until a reboot — leaving the box exposed to the exact
  OOM wedge the setting exists to prevent. Setup now activates it live on the
  running container, so the protection is real immediately.

- **Genesis's database keeps more in memory and stops over-syncing.** The main
  shared SQLite connection held only SQLite's tiny ~2 MiB default page cache and
  fsynced on every single commit (`synchronous=FULL`), even though the
  standalone connection helper already used the lighter, equally safe `NORMAL`
  mode under WAL. Both connection paths now hold a 256 MiB page cache, and the
  main connection matches `NORMAL` — fewer disk syncs and less page re-fetching
  under load, with no durability loss beyond what WAL already implies.

- **Genesis's inner monologue now knows who each thought is about.** Every
  ambient micro-reflection used to be tagged as relevant to "both" the user
  and Genesis — the tag was computed from which sensors *ran* (all of them,
  every tick) instead of what the reflection was actually *about* — so the
  filter that keeps user-activity noise out of Genesis's self-management
  context never excluded anything. Micro-reflections now report which
  signals drove them, and the relevance tag is computed from that (with the
  old behavior as a safe fallback when the model omits the field). User-ego
  context is unaffected by design: it never ingested these reflections in
  the first place.

- **Recall-time graph expansion got ~5x faster.** The just-shipped 1-hop
  expansion hydrated each linked neighbor with its own database query — and
  because the memory content table is a full-text index (no plain lookup on
  the id column), every one of those was a full scan. On a typical 10-neighbor
  expansion that measured ~750-940ms of pure overhead on the recall path. It
  now hydrates every neighbor in a single batched query (~80-130ms), returning
  identical results. Purely a performance fix — same neighbors, same order,
  same provenance and visibility filtering.

- **A dashboard request during startup can no longer crash the server.** The
  async-route bridge falls back to a throwaway event loop when the runtime
  loop isn't available — but shared database connections are bound to the
  runtime loop, so a health poll landing in that window raised cross-loop
  errors that could take the whole process down (observed as exit code 2).
  Both windows now degrade to a clean HTTP 503 instead: a configured-but-not-
  yet-running loop never executes the handler at all, and the loop-less
  fallback catches the cross-loop failure and logs it rather than crashing.

- **Code-intelligence indexing can no longer storm the machine.** Keeping the
  code graph fresh used to fire a full reindex on every commit, in the
  background, with no coordination — and if disk cleanup had reclaimed the index
  first, each "quick refresh" was secretly a full rebuild from scratch. Enough
  of them at once saturated disk I/O and dragged the whole box to a crawl. Three
  changes fix this at the root: (1) disk cleanup no longer deletes the code
  index except as a genuine last resort (very low free space), so refreshes stay
  incremental; (2) commits and setup now *queue* an index request instead of
  spawning one — a small idle-gated job does the work only when the machine is
  quiet, one at a time; and (3) whatever does run is watched live and
  automatically paused when the system gets busy, so an index can never hold the
  box hostage. Routine refreshes are now the cheap "fast" pass, with the full
  pass reserved for a weekly idle window. Also fixes a latent bug where the
  GitNexus refresh had been silently failing on every run due to an unsupported
  flag.

- **Background research sessions can now reach Genesis's discovery tools, and the
  session queue no longer strands work after a restart.** Research-profile
  background sessions were silently cut off from the recon tools (GitHub and
  model-intelligence discovery, skill scanning, findings storage) — the discovery
  engine they exist to drive — so a research session could investigate but never
  record what it found there; they now load those tools. Separately, if a queued
  background session was claimed moments before a restart, it could sit stuck
  indefinitely; the queue now re-checks for stranded claims periodically, not just
  once at boot. Two smaller hardening fixes ride along: read-only background
  profiles can no longer edit follow-ups (only *create* was blocked before), and
  the executor's plan reviewer no longer suppresses a genuine gap when a task's
  requirements explicitly call for a specific timeout/retry/escalation behavior.

- **The neural monitor's Ego panel now shows real ego activity, not phantom tiles.**
  The dashboard was rendering a few "ego" tiles that looked healthy but never
  actually ran — leftovers from an earlier ego redesign that split the ego into
  two cycles and made its compaction step non-LLM. They're gone, the Ego panel now
  lists the three live ego call sites, and a couple of other stale tiles (triage,
  bookmark enrichment) were removed too. A months-old leftover activity record can
  no longer resurrect a removed tile as if it were live.

- **Memories recovered after an embedding outage keep their wing/room/life_domain
  filters, and keyword-only results are no longer ranked as artificially fresh.**
  When the embedding provider was down, memories were stored keyword-only and
  re-embedded on recovery — but the rebuilt vector dropped its wing/room/life_domain
  tags, so it silently vanished from any domain-scoped ("wing=…") recall. Recovery
  now restores those fields. Separately, keyword-only memories were being scored as
  if created just now (maximum freshness), letting old notes outrank genuinely recent
  ones; they now use their real creation time.

- **A failed re-embed no longer leaves a memory in a permanent limbo state, and
  recovered memories keep their project_type recall filter.** When the embedding
  provider gave up on a memory, its status was left saying "still queued" forever —
  a stale marker that could trigger doomed writes to the vector store for a memory
  that has no vector. Failed embeds are now recorded truthfully, and a
  recovery pass heals any already-stranded rows — restoring the ones that did
  embed and marking the ones that truly failed. Deleting a memory now also clears
  its entity mentions. Memories re-embedded after an outage also keep their `project_type`
  so they stay visible in project-scoped recall.

- **Storing a memory no longer briefly stalls other work while it talks to the
  vector store.** The store, supersede, and delete paths made blocking vector-store
  HTTP calls directly on the event loop, so a slow round-trip could momentarily
  freeze concurrent Telegram, dashboard, and reflection activity. Those calls now
  run off-thread (matching the background paths), keeping the system responsive
  under load.

- **Telegram no longer drops messages or breaks approval buttons when the
  legacy bridge gets started alongside the server.** Two Genesis processes
  polling the same bot token split incoming updates between them (observed:
  thousands of `Conflict` errors, unresponsive approval buttons, and a
  corrupted health-status file monitored by the watchdog). The bridge now
  yields cleanly at startup whenever the server is running — however it was
  started (deploy script habit, manual, self-heal) — and the server calls out
  a rogue bridge loudly instead of failing silently. The broken self-heal
  rule that restarted the bridge on a stale awareness heartbeat (a leftover
  from when the bridge owned the awareness loop) is gone; a health alert
  takes its place, and server restarts for genuinely dead schedulers remain
  with the external watchdog.

- **Quiet hours no longer restart the Telegram poller.** The stall detector
  only counted arriving messages as signs of life, so any 15+ minutes of
  silence looked like a hung poller and triggered an updater restart, all day
  long. Successful empty polls now count as liveness; real network hangs
  still trigger the restart.

- **Protected-path guarding now covers the real systemd unit sources.** The
  critical-path rules protected a stale copy of the watchdog unit under
  `config/` while leaving `scripts/systemd/*.template` — the files installs
  actually render live units from — editable from relay channels. The rules
  now protect all unit templates, and the unused `config/genesis-watchdog.*`
  duplicates (stale 60s cadence, wrong dependencies) are removed.

- **The dashboard stops lying about monitoring state — six correctness fixes.**
  The Observations badge no longer reads 0 on a fresh page load while unsurfaced
  observations are waiting (its count now loads with the page and refreshes on
  the standard poll). The Event Log defaults to INFO+ so the once-a-minute
  heartbeat no longer drowns the feed (DEBUG stays one click away). The Neural
  Monitor shows long-stale runs as days ("67d") instead of raw hours
  ("1613.5h"). The Error Log's time-range column shows the date on both ends
  when a group spans days. The observation source filter folds per-session
  UUIDs into a single "session (any)" option. And high-traffic tabs show
  "Loading…" during their first fetch instead of flashing a false "No X found".

- **The Guardian's recovery brain no longer goes dark when its work directory is
  misconfigured.** On some installs the Guardian's configured CC work dir points
  at a path it can't create (e.g. a root-owned dir on an install that predates
  the setup script) — which used to make its intelligent diagnosis fail mid-
  incident and silently drop it to alert-only. It now falls back to a writable
  state dir so the self-healing path keeps working.

- **Knowledge-base results no longer go missing from DRIFT memory recall's
  keyword arm.** The wing-scoped keyword (FTS) search inside the DRIFT
  fallback pipeline only ever looked at episodic memories, so a recall that
  included knowledge sources could only surface knowledge via vector
  similarity — exact-phrase and keyword matches on ingested docs were
  silently dropped. It now searches every requested collection.

- **Background sessions that crash or get cancelled no longer show up as
  "completed."** A session interrupted mid-run used to sit "active" until a
  cleanup job quietly relabeled it completed — so the dashboard and
  Genesis's own success metrics counted crashes as wins. Cancelled sessions
  are now recorded as failed on the spot, orphaned ones are marked expired
  (outcome unknown) at boot and every 6 hours, and only genuinely finished
  sessions read as completed.

- **Asking Genesis to grow a disk or wait on your reply now works from a Claude
  Code session, not only from inside the running server.** The two tools that
  block on your Telegram reply (`provision_grow`, `outreach_send_and_wait`) need
  the live message pipeline, which runs in the Genesis server — not in the
  separate helper process a CC session talks to. They used to fail there with
  "pipeline not initialized"; they now hand the request to the server so an
  approval-and-wait, or an approval-gated grow, can be driven from a session.

- **The Guardian no longer false-alarms that the host gateway is "stale" after a
  healthy update — and can no longer roll it back.** A guardian redeploy advances
  the host's deployed code without moving its install-dir git checkout, so the
  staleness check (which compared against that lagging checkout) fired a spurious
  "gateway stale" alert after every clean update, and its self-heal could copy the
  *older* checked-out gateway over the current one. The check now measures against
  the commit that was actually deployed, so a healthy update stays quiet while a
  genuinely frozen gateway is still detected and repaired.

- **Memory recall no longer surfaces Genesis's own internal noise.** Machine-generated
  decisional output — background reflections, autonomy task retrospectives, and ego-dispatch
  records — was leaking into normal recall because several writers didn't mark themselves as
  internal-subsystem writes. Those writers are now tagged, so the content stays available to the
  subsystem that produced it but no longer pollutes user-facing recall (which measurably improves
  results on reflection-adjacent queries). Existing installs can purge already-embedded legacy
  noise with the new `scripts/backfill_source_subsystem.py`, then
  `scripts/cleanup_subsystem_qdrant.py` — both dry-run by default; add `--apply` to commit.

- **A dead provider kept reporting itself recovered, so a multi-day outage was
  recorded as a series of short incidents that each "recovered".** The
  `/v1/models` health probe healed a circuit breaker whenever the model was still
  *listed* by the provider — but a model can be listed and not callable. When an
  account loses entitlement to a model, the listing endpoint keeps answering 200
  while every real call returns 403, which is exactly the shape the probe read as
  "recovered". Closing the breaker fired the recovery hook, which resolved the
  `provider_failure` record and cleared the only per-provider "failing since"
  timestamp Genesis keeps; the next real call failed again and the cycle
  restarted, so the outage clock never accumulated and no surface could report a
  duration. Measured over one real multi-day outage: eight separate failure
  records, every one of them closed automatically while the provider was in fact
  answering nothing at all. The rule now is symmetry: a health probe may undo
  a health probe's own suspicion, and nothing else. If real calls broke a
  provider, a real call has to prove it fixed.

  This does not take a provider out of rotation. A tripped provider is
  automatically put back on probation once its backoff window passes, and a
  provider on probation is still called — so the next real request to it *is*
  the retry, and a success clears it. What changed is only that Genesis no
  longer announces a recovery it has not actually seen.

  **What this changes for you:** a provider that is genuinely dead is now retried
  progressively less often instead of every couple of minutes, settling at once
  every 30 minutes — or once every 4 hours when the failure looks like an
  exhausted quota. That is the point for a dead provider, but it also means a
  provider whose quota resets can wait up to 4 hours before Genesis tries it
  again, using the next provider in the chain until then. Reading the retry delay
  the provider itself reports is the exact fix and is tracked as follow-up work.

- **Disabling a provider from the dashboard now holds against health checks,
  and re-enabling one actually clears it.** The toggle used to reach into the
  breaker and set its fields directly, which left the new "who broke this"
  record out of step in both directions: a provider switched off by hand could
  be quietly switched back on by a routine health check, and a provider
  switched back on kept a stale "real calls failed here" mark that made it
  refuse to go green again until real traffic arrived. Both transitions are now
  operations the breaker performs on itself, so the state and the reason for it
  are set together, with a test that scans for code setting breaker state from
  outside. (A disabled provider still returns to rotation when its
  backoff window elapses — that is separate, and tracked.)

- **Removed a provider toggle that could never work.** The Tool Providers card
  on the Overview tab carried disable/enable buttons that posted the tool
  provider's name to an endpoint which only knows routing-provider names. The
  two are different namespaces, so every click returned "not found" and popped
  an error. The working toggle is on the Internals tab and is unaffected.

- **Upgrading in the middle of an outage no longer forgets that the outage is
  real.** The saved breaker file written by the previous version has no record
  of what broke a provider, and reading that silence as "a health check did it"
  would have let the first health check after the upgrade close a breaker that
  real calls had opened — losing the outage exactly once, at the worst moment.
  A saved-as-failing provider with no recorded cause is now treated as broken by
  real calls, which is the only thing it can have been.

- **Alerts now say how long they have been firing.** `alert_events` has recorded
  `created_at` for every open alert with 90-day retention since it was
  introduced, and nothing read it — so a condition three days old was rendered
  identically to one three minutes old. Alert messages now carry
  `(ongoing for 3d 4h)` once an alert has been continuously open for more than an
  hour. Enriching the message rather than adding a widget means every surface
  that renders an alert message gains the duration at once — the dashboard
  banner, the morning report and the outreach path. Note the existing filters
  still apply: the morning report omits call-site warnings and the Telegram path
  carries only whitelisted critical alerts, so for a degraded call site the
  duration appears on the dashboard. Self-isolating: a failure to compute the
  duration costs the suffix, never the alert.

- **The dashboard could not display a tripped circuit breaker at all.** The
  routing API emits breaker state from a lowercase-valued enum, while four
  frontend comparisons tested against an uppercase literal — a comparison that is
  never true. The provider dot rendered green, the toggle button read "disable",
  and the Provider Keys indicator stayed green regardless of the real state, with
  the "circuit breaker open" tooltip unreachable. All four now route through a
  single case-insensitive helper, and a guard test scans the frontend directories
  so a new file cannot reintroduce the comparison.

- **The dashboard now tells "broken" apart from "not proven working yet."** A
  provider is shown three ways instead of two: red when real calls are failing
  and it is not being used, hollow amber when it is still in rotation but has
  not completed a call since its last trouble, and green when it is healthy.
  Hovering says which, and why — real calls failed, or only a health check
  could not reach it. Previously a provider awaiting its next call looked
  identical to one actively failing, which overstated the problem; that state
  is now common enough to be worth naming, since recovery waits for a real call.

- **Alert severity dots were always amber, including for critical alerts.** The
  colour map was keyed lowercase while severities are emitted uppercase, so every
  lookup missed and fell through to the warning colour. The map now normalises
  case, covers the full emitted vocabulary, and gives an unrecognised severity its
  own colour instead of silently painting it as a warning.

- **Grouped error severity was ranked alphabetically, which inverts it.** The
  severity vocabulary is lowercase, so a plain `MAX()` orders
  `warning > info > error > debug > critical` — critical sorts lowest. Any group
  mixing a critical with a warning reported "warning" and rendered amber,
  silently downgrading the most severe events in the group. Ranking is now
  explicit.

### Security

- **Invisible-character stripping now covers every invisible Unicode format
  character, not a hand-picked 13 of them.** Campaign names and awareness-signal
  text are normalized before they reach a line-parsed prompt, to stop injected text
  forging or concealing a line. That normalizer enumerated 13 of Unicode's 170 `Cf`
  format characters, silently omitting concealment characters from the very families
  it did cover — most pointedly U+061C ARABIC LETTER MARK, sibling of the
  already-stripped LRM/RLM, plus SOFT HYPHEN, WORD JOINER and the invisible U+E0000
  tag block. The set is now derived from Python's Unicode database by an explicit
  rule (strip a format character only when it is genuinely invisible — zero-width,
  a bidi override, an invisible direction mark, or an annotation control), with a
  test that regenerates it and fails if the two ever diverge.

  Format characters that are *visible* content are deliberately preserved, so the
  wider net does not corrupt real text: the Arabic number and end-of-ayah signs,
  Syriac abbreviation mark, Kaithi number signs and Egyptian hieroglyph joiners all
  pass through, as do zero-width joiner and non-joiner — stripping those would break
  every emoji sequence (👨‍👩‍👧 → three separate people) and change Persian and Indic
  words, where the non-joiner is orthographically required.

- **A campaign name made only of invisible characters is now rejected instead of
  being created with an empty name.**

- **A malformed Claude Code session id can no longer create directories outside
  the session tree.** Hooks store per-session state under
  `~/.genesis/sessions/<session-id>/`, interpolating the id straight into the
  path — and two sites create the directory. An id containing `/` or `..` therefore
  escaped that tree, and the guard against it had been hand-copied into some hooks
  in three different shapes while being omitted from eight call sites across four
  files. The path-building sites now go through one shared helper
  (`hook_input.session_path`), which returns nothing for an unsafe id so the caller
  skips exactly the filesystem operation and nothing else; a site that needs only
  the yes/no answer calls the shared validator directly. Normal sessions are
  unaffected. This is the hook contract, not a repo-wide one: several other hooks
  and a number of paths under `src/` still carry their own hand-written check —
  including one file this change otherwise touches — and consolidating those is
  separate work. An id that fails the check falls back to
  the shared `unknown` key — itself a directory, so such sessions share one bucket
  rather than escaping the tree.

- **A leading shell redirect can no longer slip the push/commit approval gates.**
  The shared command parser now recognizes shell redirections (`2>/dev/null`,
  `> out.log`, `2>&1`, `&>log`, `>| f`, `< in`, `<<<`) and consumes the operator
  and its target instead of leaking them into the parsed argv. Previously a
  *leading* redirect (`git 2>/dev/null push --force`, `git 2>&1 commit --no-verify`)
  made the parser read the redirect token as the git subcommand, so the push and
  commit gates never recognized the command and skipped their approval checks. The
  redirect target is measured as one complete shell word, so an escaped or
  concatenated-quote space inside it (`git 2>err\ log push`,
  `git 2>pre"a b"post push`) no longer hides the subcommand either. As
  a bonus, a targeted local `pytest` run that redirects output
  (`pytest tests/x.py 2>&1`) is no longer misclassified as a whole-suite run.

- **Observation content can no longer launder untrusted origin into privileged
  cognitive surfaces.** Observation rows now carry a definite origin stamped at the
  write boundary: the CRUD chokepoint classifies every writer (explicit origin →
  dispatching-session origin → source classification → NULL fail-closed), so an
  unknown/novel writer's rows degrade to excluded rather than silently trusted.
  Owner-attended `task_detected` writes stamp `owner`; gateway ones stamp
  `external_untrusted`. The two stdlib-only hook writers that bypass the CRUD layer
  (the conversation-pivot writer and the post-commit audit writer) stamp origin
  inline, and a one-time migration backfills historical rows. On the read side, the
  laundering-critical surfaces — the always-loaded essential-knowledge file and the
  deep-reflection / perception prompt pipeline — now exclude external/unknown-origin
  content (fail-closed; unknown-origin excluded), severing the path by which external
  content could reach reflection and re-enter the user model as a trusted delta. A
  coverage guardrail fails CI on any new raw observation-insert that bypasses the
  origin chokepoint. The pushed-surfaces supervision exemption for interactive
  sessions is now restricted to owner-attended channels (terminal/Telegram); gateway
  conversations no longer receive it. Gateway and voice conversation sessions carry
  a durable `external_untrusted` session origin so a reflection overlapping them
  cannot launder its user-model delta to first-party, and the learning triage
  pipeline stamps its per-session `retrospective`/`cc_debrief` observations with the
  analyzed conversation's channel origin (an inbox/mail session's learnings can no
  longer surface as first-party). The coverage guardrail now also follows the
  indirect `ObservationWriter` writer and module-constant sources. On Telegram
  quote-replies, slash-command and task intent are parsed from the owner's own reply
  only — a `/task` (or `/model`, `/effort`, `/resume`) embedded in quoted bot text
  can no longer forge an owner-authorized command.

- **Untrusted content can no longer poison your user model or trigger autonomy.**
  Background sessions that process external material (e.g. the inbox evaluator over
  the links you drop in) now write observations stamped with their true origin, and
  the pipelines that would auto-apply an observation into privileged state — your
  learned user model, and autonomous task dispatch — refuse any update whose origin
  isn't first-party. A crafted item can no longer smuggle a high-confidence "fact
  about you" into Genesis's self-model or spawn a task. Refused updates are held
  (not discarded) and logged, and normal reflection-authored updates are unaffected.

- **The dashboard API is now hardened against cross-site request forgery (CSRF).**
  With a password set, a state-changing API call authenticated by your login
  cookie must now originate from the dashboard itself (verified via the browser's
  `Sec-Fetch-Site`/`Origin` headers). Previously another page — including a
  separate service sharing your dashboard's host — could ride your logged-in
  session to trigger dashboard actions; that path is now refused. Genesis's own
  components (which use an internal token) and read-only calls are unaffected, and
  the same `GENESIS_DASHBOARD_API_AUTH=off` switch disables this along with the
  rest of the gate.

### Removed

- **The duplicate-session guard is gone — it fought the wrong enemy.** The
  guard (July 14) keyed "who owns this conversation" on process liveness, but
  a slot session lives for days and serves many conversations, so reopening
  any conversation in a different slot manufactured a phantom "duplicate
  executor" — denying legitimate work and paging critical alerts for
  incidents that weren't happening. With every door now attach-or-create
  (above), the accidental-twin scenario the guard existed for can no longer
  occur; deliberately resuming one conversation in two terminals at once is
  allowed and left to your judgment. Removed: the PreToolUse deny hook, the
  session-owner registry (leftover `~/.genesis/session-owners` data is
  cleaned up on next bootstrap), the session-start warning, and the paging
  check. The fast dead-SSH-client detection (sshd ClientAlive) stays — it
  makes dropped connections detach cleanly.

## [v3.0b17] - 2026-07-06

### Added

- **The voice dashboard's Bridge tab is now a full cockpit.** Instead of a bare status line, it
  shows the ambient edge bridge's complete live health, grouped for scanning: memory leak-watch
  (parent / diarization-child / total RSS, ORT-arena state, pool recycles), capture activity
  (utterances, rows per hour, last-utterance age), diarization worker state, connection
  stability (connects, dark events, gap durations), and speaker-ID status — read on demand from
  the edge via the new `GET /api/genesis/voice/bridge` endpoint. Any new health field the edge
  starts reporting surfaces automatically under "Other."

- **Voice dashboard Device tab: live Voice PE hardware vitals.** Temperature, WiFi signal,
  uptime, reset reason, free heap, loop time, and voice-pipeline status, polled from Home
  Assistant on demand (`GET /api/genesis/voice/device`; set `HA_VOICE_PE_PREFIX`). *(Shipped
  earlier in #876; the changelog entry was missed at the time.)*

- **The Genesis Voice add-on's attention surface now shows the judge's reasoning — and lets you review it.**
  For the optional passive-listening add-on, the buried "Attention" tab is now a top-level **Genesis Voice →
  Judgment** review. Each moment the attention gate noticed is scored by a lightweight LLM judge that says
  whether it was real speech, whether it was worth attention, and — new — a one-word category and a short
  reason. You review the judge (worth noticing / not worth it / skip) and can jot your own *why*; your notes
  inform the judge's prompt, not any hidden weights. It stays offline observability — nothing here speaks or
  acts, and it's hidden entirely when the voice add-on isn't installed.

- **Attention events now record which device they came from, and the Judgment tab can filter by it.**
  Every "perk-up" event the passive-listening engine emits now carries the source device (the home
  edge connection, or `omi` once the wearable connector lands), and the dashboard's Judgment review
  gains a device dropdown plus a per-event source pill — so you can review one device's moments in
  isolation. The engine also stops penalizing text-only sources: an utterance with no audio capture
  (no loudness to measure) is scored on its text signals instead of being docked 25% clarity or
  dropped as near-silence junk, which is what OMI wearable transcripts will need.

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

- **Ambient bridge memory-leak regression alert.** If the edge bridge's RSS ever climbs past the
  healthy plateau again (total > 1000 MB or diarization child > 450 MB), ambient health flips to
  degraded and you get a one-time Telegram alert naming the breach — no nagging, and normal
  workload bursts never trigger it. Requires an ambient edge reporting the `rss_*` health keys.

- **The dashboard now has a Campaigns tab where you can see and control your autonomous campaigns.**
  Each campaign shows its status, schedule (with next fire time), model/effort, today's spend
  against its daily cap, completed runs vs. attempts, and whether a session is currently in
  flight — plus recent run history and live state. You can pause/resume a campaign, run one
  immediately, and edit its cadence, model, effort, daily cost cap, and a new optional
  schedule "jitter" (randomized fire times so ticks aren't perfectly periodic). Until now the
  only way to see or steer a campaign was through Genesis directly.

- **Genesis now stewards its own open-source pull requests instead of filing-and-forgetting.**
  A new background campaign checks the upstream PRs Genesis has authored (e.g. to litellm,
  Qwen-Agent) every couple of days, and acts on what changed: it nudges a stalled PR once,
  pings you when a maintainer responds or merges, and closes PRs that have gone unanswered
  past a grace window — so contributions don't quietly die of inactivity. It runs in a new
  locked-down session profile whose shell is restricted to the `gh` CLI only; any code
  changes a reviewer asks for are escalated to you rather than pushed automatically.

- **You can now run Genesis on Claude Fable 5, and pick the full thinking-effort range on Sonnet and Fable.**
  Fable 5 (Anthropic's new top-tier model) is now a selectable model everywhere you choose one — the ego,
  campaigns, the inbox monitor, the Telegram default, the `/model` command (terminal and Telegram), and the
  dashboard dropdowns. Sonnet (now Sonnet 5) and Fable also accept the full `low`–`max` effort range,
  including `xhigh` and `max`; previously Sonnet was capped at `high`. Nothing switches automatically — your
  existing defaults are unchanged; this only makes the new options available when you want them.

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

- **Three session skills now ship with the repo: `/shelve`, `/unshelve`, and `genesis-voice`.** `/shelve`
  bookmarks the current session with tags and a context note; `/unshelve` finds bookmarked sessions later by
  keyword and gives you the resume command. `genesis-voice` is the style guide Genesis applies when it writes
  as itself (outreach, community posts, DMs) — first person, prove-don't-claim, with a mandatory anti-slop
  pass anchored to the voice-master audit. Previously these existed only as untracked files on the
  development install.

- **Genesis now measures how often its automatic memory surfacing repeats itself within a session.**
  Each prompt's surfaced memories are tracked per session, and the health snapshot's proactive-memory
  section gains an `overlap_7d` rollup — the share of surfaced memories over the last 7 days that had
  already been shown earlier in the same session. Measurement only: what gets surfaced is unchanged.
  This data decides whether a planned improvement ships (skipping re-injection of memories already in
  context to free slots for novel ones).

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

- **Genesis now notices when its Claude Code subscription hits its usage cap — instead of quietly going dark.**
  A capped Anthropic subscription makes `claude -p` return *empty* output with no error, which Genesis used to
  record as a successful (but blank) run — so its background thinking (ego cycles, reflections, weekly reviews)
  could silently produce nothing for days without anyone noticing. Genesis now watches for a run of empty
  results on calls that should have produced output and, when it sees one, sends a single critical alert
  ("CC subscription likely capped — degraded until the limit resets") so you know to check. It's detection only:
  it never changes how a call runs, and it stays quiet during normal idle periods.

- **Off-site backups now self-prune on a grandfather-father-son schedule instead of growing forever.**
  When you back up to an off-site target (NAS/SMB or a mounted path), each run now prunes old dated
  snapshots — keeping the last 7 daily, 4 weekly, and 6 monthly — so remote storage stays bounded. It
  never deletes the most recent complete snapshot (the one a restore uses), never touches an
  in-progress snapshot, and never affects your local keep-forever transcript archive.

- **Daily disk hygiene now prunes stale scratch and old attention snapshots.** Housekeeping now
  age-prunes leftover files in `~/tmp` (older than 7 days) and garbage-collects attention-engine
  snapshots older than 60 days — but never one behind a moment you've labeled for review, so your
  labeled history stays revealable. Keeps disk usage from creeping up between the reactive cleanups
  that previously only fired when the disk was nearly full.

- **Genesis now keeps its own disk clean automatically, so it won't quietly fill up and stall.**
  A daily hygiene job reaps git worktrees whose branches have already merged (moving them to a
  7-day recovery trash bin, never deleting work in progress) and clears regenerable caches that
  otherwise creep up over time. If the disk still climbs toward full, Genesis clears the heavier
  reindexable caches automatically at 90% — before the disk hits 100% and disrupts the server,
  its write-ahead log, or backups. Previously the worktree cleanup existed but was never
  scheduled, so it never actually ran.

- **The Sentinel now learns which infrastructure fixes would be safe to run itself — observe-only.**
  Every fix the Sentinel proposes still requires your approval, exactly as before. What's new: each
  proposed action is additionally classified as "would run autonomously" (reversible, programmatic,
  and matching a conservative allowlist of known-safe command shapes — service restarts, journal
  vacuums, cache drops) or "would still ask you" (anything permanent, data-mutating, unlisted, or
  that could take down the Sentinel's own host process), and the verdict is logged to
  `sentinel_log.jsonl`. This shadow data calibrates a future opt-in autonomous tier against real
  incidents before it is ever allowed to act. The mode lives in `config/sentinel.yaml`
  (`autonomy.mode: shadow`); `live` is reserved and not yet implemented — nothing executes without
  approval regardless of the setting.

- **Genesis now re-hardens its own host SSH key automatically — no human on the host required.** The
  Guardian's control key (the one the container uses to manage the host) is supposed to carry `no-pty`
  and a source-IP `from=` restriction, but before this those were applied only at install time, so an
  install set up before the hardening — or one whose container address changed — kept a weaker key
  until someone re-ran the installer by hand. The watchdog now checks the key's hardening every cycle
  and, when it has drifted, calls a new `reharden-key` gateway operation that rewrites the key to the
  hardened form and proves the rewrite still works before committing to it (a built-in 120-second
  dead-man's-switch restores the previous key otherwise, so it can never lock Genesis out of its own
  host). A moved-but-stable source address heals on its own; a flapping address is left alone and
  reported instead of chased. For the rare manual case, `install_guardian.sh --reharden-key-only`
  re-hardens the key without a full re-install.

- **The Guardian now maintains a rolling "healthy" snapshot of the container — the restore point its
  snapshot-rollback recovery always needed.** Once a day, while every health check passes, the Guardian
  takes a `-healthy` container snapshot and deletes the previous one (exactly one exists, never more
  than a day old, so copy-on-write growth can't quietly fill the storage pool the way stale snapshots
  once did). Snapshot rollback — the one recovery action that works even with the network down — now
  has a real target; before this, nothing ever created a healthy snapshot, so rollback could never
  succeed. Kill switch: `snapshots.healthy_enabled: false` in `guardian.yaml`.

- **The Genesis container now comes back on its own after a host reboot — including an unclean one.**
  `host-setup.sh` sets incus `boot.autostart` on the container (applied to existing installs on
  re-run, not just fresh ones). Previously an unclean host reboot could leave the container stopped
  indefinitely, taking Genesis and its network access down until someone noticed.

### Changed

- **The idle-time code auditor now hunts the failure modes AI-generated code actually has.** Its
  briefing gained a research-backed taxonomy — swallowed async errors, orphan state without
  teardown, race surfaces, phantom guards, near-duplicate helpers, cosmetic abstractions, pattern
  abandonment, and constraints quietly removed during refinement cycles — so audit findings target
  the defect classes iterative LLM development is known to produce instead of only generic lint
  categories. Development guidance gained the matching discipline: iterate with scoped explicit
  prompts (never "improve this"), and diff refinements for what they *removed*.

- **The Sentinel (Genesis's internal emergency responder) now only wakes for problems it can
  actually fix.** Previously any CRITICAL health alert woke it — including things it has no way to
  remediate, like backups failing to reach a remote repo, a provider running out of credits, or an
  external API outage. Those now stay visible on the dashboard and still notify you directly (backup
  failures were added to the immediate-notification list), but they no longer trigger an automated
  diagnostic session that can only conclude "nothing I can do." Each alert is matched against the
  remediation tools available on your install, so e.g. a Guardian alarm only wakes the Sentinel where
  a Guardian is actually configured. Installs that intentionally don't run backups no longer see a
  false "backup failed" critical alert at all.

- **When the Sentinel does wake, it now launches knowing exactly what it can act on.** Its diagnostic
  session is handed the live list of remediation tools available on your install and told which one
  applies to each firing alarm, so its proposed fixes stay grounded in what's actually possible here
  — and it escalates with its diagnosis instead of inventing a command when nothing available can fix
  the problem. It also launches with a short orientation to your system's shape and pointers to the
  architecture docs it can consult.

- **A rejected Sentinel approval can no longer freeze the Sentinel.** If you rejected (or let
  expire) a Sentinel dispatch approval while the underlying alarm stayed active, the Sentinel parked
  itself waiting forever and silently ignored every new alarm. It now applies your decision on the
  next check: a rejection suppresses that alarm pattern for 24h and returns the Sentinel to healthy,
  ready for the next real emergency. Sentinel state changes also now appear in the event log under
  their own "sentinel" subsystem, so a parked or wedged state is visible instead of silent.

- **The dashboard loads faster and shows consistent status/time formats.** The web UI's
  scripts and styles moved out of the page into cacheable files, so after your first visit
  only data — not the whole 700KB page — is re-fetched. Overview health chips now use one
  consistent 5-state color system with shape glyphs (● ▲ ✕ ◔ ○) so state is readable
  without color, and ages render in one format everywhere ("just now" / 5m / 3h / 2d)
  instead of mixed styles.

- **The dashboard Infrastructure card now labels the ambient-capture bridge as "Voice Bridge"** —
  a clearer, user-facing name. It still only appears when a voice/ambient edge is configured.

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

### Fixed

- **A false "deferred work backlog" health warning no longer fires every few minutes.** The
  weekly memory dream-cycle parks a large synthesis worklist that drains a little each day by
  design; the health check was counting that scheduled batch against the "postponed due to
  degradation" queue alarm, so it tripped a WARNING on every awareness tick. The alarm now
  watches only genuine recovery backlog (a stalled worklist still surfaces after a full drain
  cycle), and the dashboard shows the batch worklist separately from recovery work.

- **The dashboard's Infrastructure card is clearer at a glance: failed probes sort to the top, and a
  stray raw row is gone.** Probes now order worst-first (down/error, then degraded, then healthy) so
  problems surface at the top of the card instead of wherever they happened to fall in insertion
  order. The "Claude Code Sessions" data no longer also leaks out as a raw, unlabeled `cc_slots` row
  — it appears only in its dedicated section. And when an infrastructure probe crosses the
  healthy↔unhealthy line (e.g. Qdrant goes down, then recovers), that transition is now recorded in
  the Activity feed — with startup-grace and flap dampening so a restart blip or a rapidly-bouncing
  probe won't spam it.

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

- **Automatic Qdrant restart works again.** The self-heal action that restarts a downed Qdrant
  targeted the wrong systemd manager and failed with "Access denied," leaving the vector store
  without automatic recovery. It now restarts the correct user-scoped service.

- **The emergency responder can't stall its own monitoring anymore.** If a Sentinel remediation
  session was running when a health tick fired, the tick could block behind it long enough to
  trip the very "monitoring is overdue" alarm the Sentinel exists to prevent. The tick now skips
  cleanly when a remediation is already in flight.

- **The watchdog's memory-reclaim cooldown now survives its own restarts, so it can't trigger an
  I/O storm under sustained memory pressure.** When container memory runs high, the watchdog reclaims
  a small, capped amount of page cache — throttled to at most once every 5 minutes to avoid an I/O
  spiral. That throttle lived only in memory, but the watchdog runs as a fresh short-lived process on
  each check, so it reset every run and never actually held. The cooldown is now persisted, so
  repeated reclaims stay correctly spaced even across the watchdog's process boundary.

- **The Guardian's snapshot safety gate now measures real LVM thin-pool allocation instead of the
  host root filesystem.** On LVM-backed storage, the free-space check behind "is it safe to take a
  snapshot" ran `df` against a path that lives on the host's root disk — so it could happily approve
  snapshots while the actual thin pool was nearly full (the same blindness behind the pool-exhaustion
  outage). The gate now reads the pool's own data/metadata usage and refuses at the configured high
  tiers; non-LVM backends keep the existing byte-headroom check, where `df` is correct.

- **The Guardian can now recover a container that is fully stopped.** Its container-restart recovery
  action used `incus restart`, which errors out on a stopped instance — exactly the state an unclean
  host reboot leaves behind. On restart failure it now falls back to `incus start`.

- **Taking a pre-recovery snapshot can no longer evict the healthy rollback snapshot.** The
  make-room-before-create step deleted the oldest snapshots purely by count, and at the default
  retention that meant deleting the healthy restore point right before the risky recovery action that
  might need it. The latest healthy snapshot is now exempt, mirroring the existing prune exemption.

- **Snapshot rollback now recovers when the storage driver refuses to restore a non-latest
  snapshot.** ZFS (documented) won't restore past newer snapshots; if the restore fails and newer
  guardian-created snapshots exist, they are deleted (they capture the already-broken state) and the
  restore is retried once.

- **Guardian's automated recovery now acts on the right service.** Guardian's self-healing (restart,
  journal-freshness, and crash-loop probes) and its diagnostic briefing pointed at a deprecated,
  usually-inactive background unit instead of the main Genesis service. As a result a "restart" could
  report success while healing nothing, and a genuine crash loop of the main service went undetected.
  Recovery, health probes, and diagnosis now target the main service, and a guardrail test keeps them
  from drifting back.

- **`update.sh` was silently skipping the host-VM sync (guardian redeploy + host Node/Claude Code
  pin healing).** A recent refactor re-indented the two inline Python snippets that read
  `guardian_remote.yaml`; the resulting parse error was suppressed, the host address resolved
  empty, and the entire host-side block quietly skipped on every update — guardian code stopped
  reaching the host and host pin drift went unhealed, while update history still reported the host
  as healthy. The snippets are now single-line (immune to the indentation class), an unusable
  guardian config now prints a loud warning and is recorded as a degraded subsystem, and a
  repo-wide test guards against any shell script reintroducing the indented-snippet shape.

- **The host Guardian no longer gets stranded on an old commit.** `update.sh` decided whether to
  redeploy the Guardian by looking only at what the *current* update pulled in — so on a run that
  pulled nothing, a host that was already behind stayed behind indefinitely (and it could fall
  behind whenever it had last been deployed from a commit that was later rebased away). The
  redeploy now compares the host's actually-deployed commit against the current code and reconciles
  the difference: it redeploys when Guardian-relevant code differs, converges a host on an
  unrecognized/orphaned commit back onto current, and only falls back to the old behavior when the
  host can't be reached — so a lagging host heals itself on the next update instead of drifting. If your
  machine ever accumulated a second Claude Code install (an old nvm-tree copy, a native-installer
  leftover, or an install in a directory only interactive shells can see), it could silently shadow
  the version Genesis pins — you'd see a months-old Claude Code in your terminal while Genesis
  believed everything was current, or updates would pointlessly reinstall on every run. The
  install/update scripts now enforce a single canonical copy: provable duplicates are removed (with
  clear logging), ambiguous files are only warned about, and shell aliases that shadow the real
  binary are flagged. Set `CC_SHADOW_SCAN=0` if you deliberately run multiple copies.

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

- **Updates no longer abort when a schema migration actually succeeded** — if the
  database was busy during an update (for example a background task writing at the same
  time), a migration could commit successfully yet still surface a transient "database
  is locked" error, which made the update roll the code back while the database had
  already moved forward. Updates now confirm whether the migration was truly applied
  before treating it as a failure, and give migrations more room to wait out a busy
  database.

- **Background code-intelligence indexing can no longer freeze your machine.** Creating a git
  worktree (or running install/bootstrap) could fire several full-repo indexing jobs at once —
  uncapped, each building its own multi-gigabyte code graph — saturating the container's disk-write
  limit until SSH and the dashboard stopped responding. All background indexing now goes through a
  single entrypoint that skips worktrees entirely (the live LSP covers those sessions with no index
  needed), runs at most one index per repo at a time, and caps the indexer's memory/IO/CPU so even
  a single job can't starve the system. A repo-wide test blocks any future code path from spawning
  a raw indexer again. Set `CODE_INTEL_INDEX_DISABLE=1` to opt out of background indexing entirely.

- **The codebase-memory code-intelligence server can no longer eat all your RAM.** The third-party
  `codebase-memory-mcp` binary has a known upstream memory leak (grows without bound over hours of
  use — several gigabytes per Claude Code session, enough to freeze the whole container when a few
  sessions run at once). Every instance now starts inside a hard 2 GB memory cap: when the leak hits
  the cap, only that server is killed and the session keeps working (reconnect it with `/mcp`).
  Existing installs pick the cap up automatically on their next update — the setup script now also
  detects and re-points a stale registration that would have bypassed the capped launcher. Override
  with `CODEBASE_MEMORY_MCP_MEMORY_MAX` if your machine has RAM to spare.

- **Skill suggestions now actually fire — and nested skill packs show up in the catalog.** The
  prompt-time skill nudge scored matches against the length of your prompt, so on any wordy prompt a
  genuine match was diluted below the firing threshold and suggestions near-never appeared; a single
  clear name-or-keyword match is now enough. Suggestions also no longer get crowded out by the process
  (TDD/brainstorming) nudges — each kind keeps its own slots — and library-skill suggestions now tell
  you exactly what to do (`Read <path>/SKILL.md`) instead of pointing at a command that can't load
  them. The catalog builder now looks inside container directories (skill packs like `gitnexus/` or
  plugin repos in the skill library), indexing the real skills inside instead of one useless entry for
  the folder. And a stale catalog can no longer cut the nudge off entirely: regeneration now runs in
  the background while the current prompt uses the existing catalog.

- **Reflection summaries are no longer degraded to raw JSON dumps when the model wraps its output in a
  code fence.** Light and strategic reflections store a searchable summary of what they found. When the
  model wrapped its answer in a markdown ```json fence — which it often does — the summary step failed to
  parse it and stored a truncated raw dump (fence markers included) instead of the assessment and focus,
  degrading later recall. A ```json-tagged fence now parses the same as bare JSON (other fence types
  are left untouched), matching how the rest of the reflection pipeline already reads it.

- **Reflections that omit a confidence score are now recorded honestly instead of masquerading as
  "0.7 confident".** The reflection prompt explicitly forbids defaulting to 0.7, but when a reflection
  came back without a confidence value the system silently filled in 0.7 — indistinguishable from the
  model genuinely reporting it. An absent confidence is now stored as 0.5: deep reflections carry a
  `confidence_defaulted` marker in the cycle's routing record, and light reflections log the sentinel,
  so you can tell reported confidence from a filled-in default.
  The Light reflection prompt also regains its hard output caps and "signals are the only valid
  evidence" verification rule, which were stranded in a prompt file the loader never used.

- **Reactive ego events no longer force the expensive Opus model, critical escalations always think at
  full effort, and a missing ego prompt file fails loudly instead of silently running a placeholder.**
  Reactive events (deadline alerts, breaker trips) used to override every ego onto Opus at high effort
  regardless of your configured model — now each ego handles them on its own base configuration.
  Critical escalations previously ran at *medium* effort (less thinking than a routine tick); they now
  always run at high effort. And if a per-ego identity prompt file is missing, the ego refuses to start
  with a clear error naming the file, instead of quietly substituting a generic placeholder identity
  (the obsolete legacy `EGO_SESSION.md` prompt is removed). The ego prompts themselves also got
  corrections: the user ego no longer lists a health tool it doesn't have, no longer writes the morning
  report (the dedicated morning-report pipeline is the sole source — its daily-briefing cycle now
  focuses on what you need today), and the pending-proposal guidance matches the real threshold.

- **Genesis's COO (self-maintenance ego) no longer burns cognitive cycles reacting to model-provider
  outages it can't fix.** When a provider chain runs out of options (e.g. a temporary DeepSeek outage),
  the "all providers exhausted" alerts no longer wake the ego into a full high-effort reasoning cycle that
  can only conclude "nothing I can do" — these were the large majority of its zero-outcome reactive cycles.
  The outage still reaches the ego through its normal system-health context, so nothing is hidden; it just
  stops paying to react to it. Relatedly, the cross-type procedure-dedup check now skips its judgment call
  outright when that chain is down, instead of firing a doomed request that would raise yet another alert.

- **Genesis's background egos now keep their thinking rhythm across a restart instead of going quiet.**
  The two egos run proactive cycles on an adaptive schedule that stretches out when things are idle. That
  schedule was re-armed from scratch on every restart, so an install that restarts often (deploys, recovery)
  could keep pushing the next cycle further out — in the worst case starving an ego for up to its full
  backed-off interval. Each ego now anchors its first post-restart cycle to when it last actually ran: an
  overdue ego runs shortly after startup, while an up-to-date one simply keeps its cadence.

- **The Internals "composite" self-improvement score is no longer dragged down by draft
  procedures** — Genesis extracts candidate procedures from its own sessions; these start
  unvalidated (near-zero confidence) until they prove useful. The weekly composite score was
  averaging *every* procedure's confidence, so a burst of new drafts made the score crater
  even though nothing had actually regressed. The score now reflects only validated
  procedures. Genesis also caps how many drafts a single session can create, so the
  procedure store stops accumulating dead weight.

- **Genesis stops cluttering its procedure store with general working-style rules.** When it
  learns a "procedure" from a work session, it now tells a reusable *task procedure* (how a
  specific tool or system works) apart from a *behavioral directive* (a general habit like
  "double-check before acting" — which belongs in its standing instructions, not the procedure
  store). Directives are no longer stored, removing the most common source of near-duplicate
  procedures. The check errs toward keeping, so genuine procedures are never dropped.

- **Background research findings reach your knowledge base again — instead of piling up as unreadable
  JSON blobs.** Genesis's idle-time research (brainstorms, gap clustering, code and wing audits) sends its
  findings through an intake step that splits them into individual knowledge entries. That step couldn't
  read the output format models actually produce (code-fenced JSON, and the bare-array shape code audits
  emit), so months of findings were stored as single raw JSON dumps — useless to search and recall. Intake
  now parses these formats, skips empty result envelopes outright, and wing-audit results are split into
  individual findings like the other research types. A new `scripts/cleanup_fenced_knowledge_units.py`
  (dry-run by default) re-parses the previously mangled entries into proper knowledge units and removes
  the junk ones.

- **Re-ingesting a knowledge source with changed content now refreshes it instead of serving the stale
  version.** Previously, once a file or URL was ingested, re-ingesting the same source was skipped on source
  identity alone — so if the underlying content changed, the knowledge base kept serving the old distilled
  version indefinitely. Re-ingestion now compares a content fingerprint and re-distills when the content has
  actually changed (unchanged content is still skipped, and a now-unreachable source falls back to its
  previously cached version).

- **Memory extraction now runs reliably after a restart.** The job that turns recent conversations into
  long-term memories was scheduled on a fixed 2-hour interval measured from server start — and that timer
  reset on every restart, so a box that restarted more often than every two hours could keep deferring
  extraction indefinitely. It now also runs shortly after each start, so extraction can't be starved by
  frequent restarts.

- **Superseded and expired memories no longer resurface in Genesis's automatic recall.** Before every
  prompt, Genesis injects the most relevant memories into its working context. That fast-path recall
  wasn't checking whether a memory had expired or been superseded (replaced by a newer, consolidated
  version) — so an outdated network note, stale career details, an old build-progress snapshot, or a
  duplicate the memory system had already merged away could still surface, even though the main memory
  system filtered all of those out. The fast path now applies the same validity checks across all of
  its sources, matching the main retriever. Memories that are current (no expiry, not superseded) are
  never affected, so nothing live is ever dropped.

- **Deleting a knowledge item from the dashboard now works end to end.** Previously the delete
  button returned a server error, and behind the scenes the item was only half-removed — dropped
  from search but with its stored embedding left behind and its source still marked as already
  ingested, so re-adding that same file or URL was silently skipped and nothing came back. Deletes
  now complete cleanly (search entry and embedding both removed), and once a source's last item is
  deleted, re-ingesting that file or URL works again.

- **Fresh installs and repairs now get scheduled housekeeping running without a manual step.** The
  bootstrap script rendered Genesis's systemd timer units (watchdog health check, daily disk hygiene)
  but never enabled them, so on a new machine — or after a repair re-render — the timers sat installed
  yet dead until something else switched them on. Bootstrap now enables and starts the housekeeping
  timers idempotently, right after the render pass; environments without systemd are unaffected. The
  backup timer is deliberately left for you to enable after configuring backups (passphrase + verify
  run), so it is not auto-started here.

- **Completed background-task history no longer grows without bound.** Genesis's idle-time "surplus"
  task queue kept every finished task row forever — only never-started tasks were ever cleaned up — so
  the table crept upward over months of background work. Finished tasks (completed, failed, or
  cancelled) are now aged out after 30 days, while any task still referenced by an open follow-up is
  kept until that follow-up resolves.

- **The weekly self-assessment and quality-calibration jobs now recover on their own instead of
  going dark for weeks.** Previously each ran only once a week, so if that one run failed — for
  example when the shared Claude Code subscription was capped and returned empty output — the next
  attempt wasn't until the following week, and a multi-day outage could leave them stale for 2–3
  weeks. They now run daily but still complete at most once per week (an idempotency check skips the
  rest of the week once one run succeeds), so a failed day is simply retried the next day until it
  succeeds. Side effect: the successful run now normally lands early in the week rather than on Sunday.

- **Campaign results no longer sit uncaptured until the next scheduled tick.** Previously a
  campaign that ran every couple of days would finish its background session but not record
  the outcome (or cost, or notify you) until the *following* tick — so a finished run could
  stay invisible for days, and its spend went uncounted. A new reaper now captures finished
  sessions within minutes and cleans up runs orphaned by past crashes, so campaign status and
  cost stay accurate in near-real-time.

- **Uploading a large or malformed PDF to the Knowledge tab no longer freezes — or crashes — Genesis.**
  PDF text extraction used to run directly on the main event loop, so a big document could stall the whole
  server for seconds (health checks, background thinking, other requests all waited), and a corrupt or
  hostile PDF could take the process down entirely. Extraction now runs in an isolated worker process with a
  time limit: a large PDF no longer blocks anything else, and a PDF that crashes or hangs the parser fails
  just that one ingest — the rest of Genesis keeps running. Uploads that hit a transient database hiccup are
  now marked "failed" (and can be retried) instead of getting stuck showing "processing" forever.

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

- **Two pending inbox approvals could cancel each other forever, re-pinging you every 30 minutes for
  nothing.** When new content arrived while one link approval was still pending, the refresh step
  invalidated every row parked on it — including an unrelated file's — but only re-queued the
  newly-detected file. That other file's rows reappeared as "new" on the very next scan, which
  cancelled the fresh approval in turn: two files could leapfrog cancel-and-recreate every
  30-minute scan with zero actual change on disk. Invalidated files are now folded into the same
  refresh batch, so one merged approval covers everything in a single cycle instead of chasing itself.

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

- **Genesis no longer floods its own approvals with follow-up emails addressed to itself.**
  A follow-up drafted in a background session could lose its thread (and therefore its
  recipient) on the way to the outreach queue, then fall back to Genesis's own email address.
  The capability gate correctly held each one for approval, but because the queue kept
  retrying, a new held "email to myself" piled up every few minutes. Genesis now keeps the
  thread recipient with the queued message, never sends an email to its own address, and
  treats a held or undeliverable message as resolved instead of retrying it forever. A
  message that can never be delivered is now dropped after a day rather than looping.

### Security

- **The contribution sanitizer now blocks Tailscale addresses before they can reach the public
  repo.** When you prepare a community contribution, the pre-push privacy scan now catches Tailscale
  CGNAT and Tailscale IPv6 addresses, and flags the full private RFC1918 subnet range (not just two
  hard-coded addresses) — closing a gap where these install-specific addresses could otherwise slip
  into a public PR. The commit-message guard gained the same IPv6 coverage.

- **The HTTP client Genesis uses for outbound requests is upgraded to clear 11 security advisories.**
  `aiohttp` — the library behind health checks, provider pings, and market/price-data fetches — now
  requires 3.14.1 or newer, which fixes 11 published CVEs (including a cookie-leak-on-redirect issue
  and several denial-of-service vectors). Genesis uses it only as a client for outbound calls, so
  real-world exposure was limited, but the newer version removes the advisories outright.

- **The autonomy gate now honors each action category's context ceiling before auto-dispatching a
  background action.** When deciding whether a background action may run, the gate compared the
  required level against the raw *earned* level — so a category that had earned a high level could
  clear a bar it shouldn't in a more restricted context, most notably letting a financial action pass
  its level check. It now uses the ceiling-clamped effective level, so background auto-dispatch of
  higher-risk categories (e.g. financial) is refused regardless of earned level. Your explicit
  approval was, and still is, required for these actions — this closes a defense-in-depth gap behind
  that approval.

- **The Genesis→host control SSH key is now bound to the container's source address and denied an
  interactive terminal.** The key that lets the container drive recovery on the host is installed with
  a `from=` source-IP restriction and `no-pty`, so a copied key can't be used from another machine on
  your LAN and can't request a shell. Re-running the Guardian installer upgrades an existing key in
  place; if the source address can't be confirmed it keeps the terminal restriction and safely skips
  the address lock (verified by the installer's own connectivity test) rather than risk locking
  Guardian out.

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
