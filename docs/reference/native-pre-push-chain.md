# Native pre-push hook chaining

Run `scripts/hooks/sync-hooks.sh --chain-pre-push` from the main checkout to
explicitly enroll a custom pre-push hook. Without that option, an unknown hook
is preserved and sync reports non-success. Git chooses the effective hook
location, including `core.hooksPath`; `--hook-repo /path/to/linked-worktree`
selects a linked worktree while the main checkout supplies the code.

The original remains beside the wrapper, with its bytes and mode preserved.
It runs first with the same arguments, working directory and environment; each
consumer receives the complete original stdin, up to the 1 MiB chain limit.
An original failure stops the chain. Hooks that require their exact executable
basename are unsupported: qualify the custom hook before opting in. Shared
hook directories enroll each repository explicitly; unenrolled repositories
continue to run only the preserved original.

`--restore-pre-push` removes this repository's enrollment. The final enrollment
restores the preserved original (or removes a wrapper installed where no hook
existed). Changed originals or wrappers are preserved and reported instead of
overwritten. The adjacent private ownership record and helper hashes allow
interrupted installation to retry without accepting unknown modifications.
Final restoration records its progress before replacing the wrapper and retires
ownership only after cleanup, so an interrupted cleanup can be retried. Changed
artifacts remain preserved. Helper upgrades may extend the recorded closure;
every newly claimed destination must be absent before sync changes any hooks.
An interrupted installation must finish its recorded target before switching to
a different target, and removing or reordering helpers requires a separate
transition.

The existing Genesis push gates run after an enrolled custom hook. This change
alone does not qualify native privacy enforcement or retire the advisory;
those require the subsequent scanner integration and installed qualification.
