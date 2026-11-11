# Holdout scenarios

A holdout is a small set of exact behavioural checks that builders never see.
A builder writes both a change and its tests, so its tests can agree with its
code instead of with the intent, and nothing in the PR's own evidence would show
it. A check that the builder could not read cannot be tuned that way.

Three parts:

- **The scenarios** live on the install, outside the repository:
  `$GENESIS_HOME/holdout/HOLDOUT.md` (`~/.genesis` when unset). They are never committed, never pasted into a
  PR, an issue, a comment or a prompt, and never read by a session that builds.
- **The writer** is the `holdout-writer` subagent
  (`.claude/agents/holdout-writer.md`). It is the only agent that writes or reads
  the file. The session observer records nothing for it or for the parent's call
  that spawns it, so its work does not reach memory or recall.
- **The runner** is `scripts/holdout_runner.py`. Validators run it; it prints
  scenario ids, verdicts and each scenario's `Protects` line, never the steps or
  the expected output.

An install with no `HOLDOUT.md` has no holdout: the runner prints
`no holdout on this install` and exits 0.

## Two kinds of scenario

- **Regression** (`Expect-before: PASS`): written from today's behaviour of code
  that matters, preferring behaviour no test pins. It must pass on the default
  branch now. It catches a later PR that breaks it.
- **Intent** (`Expect-before: FAIL`, with `Issue: N`): written at spec time from
  an issue's stated intent, before the issue goes to a builder. It must fail on
  the default branch now and pass once the change lands. This is the case a
  holdout exists for: the builder never sees what it will be judged against. It
  is judged only against a PR that links issue N: GitHub lists it as closed by
  the PR, or the body uses a closing keyword (`closes`, `fixes`, `resolves`
  `#N`). An unrelated PR touching the same files is never charged with it, and
  neither is a PR that is only `part of #N`: under a split, only the last PR
  delivers the intent, so it is the one that says `Closes #N`. A plain mention of
  `#N` does not link, nor does a closing reference to another repository's
  issue. A linked PR runs the scenario whatever files it touched.
  Once it is `DELIVERED`, the writer retires it and adds a regression scenario
  for the same behaviour in its place.

## Format

Each scenario is one section. Field lines are `Name: value`; `Steps:` and
`Expect:` are each followed by a fenced block.

````text
## H-01 short title
Protects: the intent, in one line (printed by the runner)
Touches: scripts/hooks/example.py, scripts/hooks/other_*.py
Expect-before: PASS
Expect-exit: 0
Issue: 123
Author: holdout-writer, 2026-11-10
Control: the mutant it was checked against, and that it failed there
Steps:
```sh
"$HOLDOUT_PYTHON" - <<'PY'
...
PY
```
Expect:
```
exact standard output
```
````

- `Touches` is a comma-separated list of path globs. A scenario runs for a PR
  only when one matches a file the merge changed.
- `Expect-before` is required. `Issue` is required for `Expect-before: FAIL`
  and ignored otherwise. `Expect-exit` defaults to `0`.
- `Author` names the writer and the UTC date the scenario was written. The
  runner reads that date to tell which scenarios a later exposure may have
  spoiled, and counts a scenario without one as spoiled. The newest such date
  in the file also decides which writer accesses it reports (below).
- A scenario passes when its exit status equals `Expect-exit` and its standard
  output equals the `Expect` block (trailing newlines ignored).
- Steps run with `bash` in the checkout under test, with `HOLDOUT_ROOT` set to
  it and that checkout's `src` first on `PYTHONPATH`, so `import genesis` loads
  the code under test. Run Python as `"$HOLDOUT_PYTHON"`, the runner's own
  interpreter (run the runner with the checkout's `.venv/bin/python`). `HOME`,
  `TMPDIR`, `GENESIS_HOME` and `GENESIS_DB_PATH` are temporary, `QDRANT_URL`
  points at an unroutable address, and only locale, terminal and user-name
  variables are inherited: no tokens, no SSH agent. There is no network isolation, so steps must not use
  the network. They must be deterministic and write nothing outside the
  temporary directories. A step running past 120 seconds fails, and everything it
  started is killed.
- A failing run's standard output is shown (up to 400 characters); standard
  error never is. So a step must never print the value it checks against, only
  the value it observed.
- Every `Expect` line is under 40 characters, or is a value the step derives
  from what it observed (a sha256 of the observed output, say). The holdout
  fence treats an `Expect` line of 40 or more characters, whitespace collapsed,
  anywhere in a tool result as an exposure. A long line the code under test
  prints would therefore flag a builder who only ran that code, and an intent
  scenario's line would flag the builder who implements it. A builder never
  sees the steps, so it cannot produce a derived line by chance.
- Any heading that is not a scenario heading ends the scenario above it, and
  fenced blocks outside a `Steps` or `Expect` slot are ignored. A line starting
  `## H-` always starts a scenario, even inside such a block, so never write one
  in notes. A heading must be exactly `## H-<digits> title`; anything else is
  reported as malformed.
- An expected output must fit in 1 MiB, and no file a step writes may pass
  64 MiB. Anything a step leaves running is killed when it ends.

**Tighten, never loosen.** A scenario that fails is never edited to pass. If the
failure was a real defect, the scenario has done its job; if the scenario was
wrong, it is retired (deleted) and a new one is written. Either way the old one
is spent, because its failure has been seen.

## What the runner reports

`.venv/bin/python scripts/holdout_runner.py --pr N` (from the main checkout) runs each touched scenario at the
merge commit's first parent and at the merge commit, in detached temporary
worktrees, and prints one line per scenario:

| Verdict | Means | How a validator records it |
|---|---|---|
| `PASS` | regression held at both commits | a finding (information); not a control, because nothing had to come out the other way |
| `DELIVERED` | intent scenario for a linked issue: fails before, passes after | a `controls` entry; then the writer retires it into a regression scenario |
| `REGRESSED` | passed before this merge, fails after | a failed claim at `MEASURED`: `fail-intent` |
| `NOT-DELIVERED` | intent scenario for a linked issue fails at the merge | a failed claim at `MEASURED`: `fail-intent` |
| `PENDING` | intent scenario for an issue this PR does not link; still fails, and the issue is open | nothing: not this PR's work |
| `CLOSED-NOT-DELIVERED` | as `PENDING`, but the issue is closed | a finding for the owner: work landed without linking the issue and missed the intent, the issue was closed as not planned, or the scenario is wrong |
| `DELIVERED-UNLINKED` | intent scenario for an issue this PR does not link now passes | a finding: the work landed without naming the issue |
| `FIXED` | failed before, passes after | a finding |
| `BROKEN-BEFORE` | fails at both | a finding and an issue about the default branch, not this PR |
| `STALE-INTENT` | intent scenario already passed before the merge | a finding: the scenario is wrong or the work landed elsewhere |
| `MALFORMED` | the scenario cannot be parsed | a finding for the writer |

A failing arm shows its exit status and up to 400 characters of its output,
never the expected output. Exit status: 0 no attributable failure, 1 a
`REGRESSED` or `NOT-DELIVERED` verdict, 2 usage error, 3 the run could not be
completed (it says why: not merged, merged into another branch, a git or `gh`
error, an unreadable file). The parent arm is the merge commit's first parent,
which is the default branch before the merge under squash merges, the only
method this repository allows.

For the install's own holdout, the runner also prints one line about the
holdout fence, an advisory hook that records any agent but the writer reaching
the holdout (`scripts/hooks/holdout_fence.py`, added separately): "not
installed in this checkout" until that hook exists, then the count of exposures
it recorded in `$GENESIS_HOME/logs/holdout-exposures.jsonl` (reads, writes,
accesses and content; never rotated), or that the log is missing or unreadable. The
"installed" test is the hook file's presence in the checkout. Accesses by an
agent declaring itself `holdout-writer` are counted separately. One that is no
later than the day after the newest `Author` date in the file is only counted:
the writer has written a scenario since. A later one, one with no readable time,
or any at all when no scenario carries a date, is reported with the latest time,
for the owner to match against writer runs they ordered. So the message clears
itself the next time the writer writes a scenario, for as long as that scenario
stays in the file. An `Author` date later than tomorrow is ignored here, and a
row with no readable time is always reported. The writer is known only by
the name an agent declares, so any session could claim it, and a false claim
made before the writer's latest scenario is not reported. The
fence and the count catch honest mistakes; "no exposures" is not proof that the
set is still hidden. When there are exposures, the line names the scenarios
they may have spoiled: those whose `Author` date (`YYYY-MM-DD`, the earliest
one on the line; none if any of them is not a real date) is no later than the day after the latest exposure, judged in
UTC, since dates are whole days and the extra day covers time zones. The log is
append-only, so an exposure with no readable time, or a line that does not
parse, is bounded by the next dated row that is not older than any before it; one
with no such row after it counts against every scenario. The runner reads
without the fence's lock, so a row being written at that moment can make one
run name every scenario. So does a scenario with no `Author` date, or one dated
after tomorrow. Tell the owner about any it names. When it names none, it says
every scenario was written after the latest exposure (or that there are no
scenarios to judge). This judges reads: an exposure that copied the file
somewhere could be read later, which the log cannot see. It also keeps
`$GENESIS_HOME/.ignore` listing `holdout/`, so ripgrep-based searches of the
Genesis home skip the directory.

`--has-holdout` prints whether this install keeps a holdout and where, and
reads nothing in it; skills and agents use it instead of hard-coding the path.

`--at-path DIR [--scenario H-NN ...]` runs scenarios against a directory and
prints `PASS` or `FAIL` per id. The writer uses it to check a new scenario on the
current tree and on a deliberately broken copy.

## Rules for sessions

- Never open, list, search or quote the holdout directory
  (`$GENESIS_HOME/holdout/`, `~/.genesis/holdout/` by default). Run the runner.
- Spawn `holdout-writer` in the FOREGROUND (not in the background). A
  background agent's final report arrives as a notification message in the
  parent's transcript, which memory extraction reads; a foreground agent's
  report is a tool result, which it does not. Its report carries ids and
  `Protects` lines only, but the rule keeps it that way if one slips.
- Do not ask the writer what a scenario checks beyond its `Protects` line.

## Known limits

- Only Claude Code subagents are kept out of the session observer. A session in
  another client that reads the file is not.
- A parent session that reads the file with its own tools is observed as usual.
- Transcript analytics stores subagent Bash commands, including the writer's;
  that store is not shown to sessions by proactive recall.
