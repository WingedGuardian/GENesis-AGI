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
