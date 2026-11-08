# Native pre-push hook chaining

Run `scripts/hooks/sync-hooks.sh --chain-pre-push` from the main checkout to
explicitly enroll a custom pre-push hook. Without that option, an unknown hook
is preserved and sync reports non-success while updating unrelated hooks and helpers.
Ownership conflicts or invalid chain state still stop synchronization before other updates. Git chooses the effective hook
location, including `core.hooksPath`; `--hook-repo /path/to/linked-worktree`
selects a linked worktree while the main checkout supplies the code.

The qualified profile is Linux with readable `/proc/self/environ`, Bash 4+ and
Git supporting `git var GIT_SHELL_PATH` (native checks use Git 2.43). The original
startup environment is retained in memory across Python interpreter startup; if
it cannot be read, native execution refuses without displaying its contents.
Executable originals without a shebang use Git’s configured shell only after
`ENOEXEC`; missing interpreters and permission failures do not take that fallback.

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

Candidate deployments refuse native-to-native helper closure changes before
admission: the current installer cannot undo them when a candidate is dropped.
An already-expanded installation also refuses a checkout move to a smaller or
reordered native closure before changing the deploy manifest or checkout.
Ordinary helper-content updates, released-main additive upgrades and qualified
legacy rollback/re-upgrade remain supported; automatic native contraction is
not supported. Fresh install and bootstrap use the same canonical sync path.

The existing Genesis push gates run after an enrolled custom hook. This change
alone does not qualify native privacy enforcement or retire the advisory;
those require the subsequent scanner integration and installed qualification.
