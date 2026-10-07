# Trash: recoverable deletes

When Genesis removes user or project data it moves it into a trash instead of
deleting it, so the removal can be undone. This is the second guarantee of the
resource-budget work (#2926): "a deletion of user or project data by Genesis is
recoverable". The code is `src/genesis/trash/` (stdlib only).

## Where things go

An item is always **renamed**, never copied, into a trash on its own volume:

- `~/.genesis/trash/` when the item is on the same device as `~/.genesis`;
- `<mountpoint>/.genesis-trash-<uid>/` for an item on another mount.

Each trashed item gets its own entry, named `<UTC timestamp>-<6 hex>-<name>`
(the random part keeps ids unique across volumes):

```
~/.genesis/trash/20261007T024821Z-3fa9c1-notes.md/
    item             the trashed file, directory or symlink (fixed name)
    tombstone.json   original path and name, kind, size (null when a
                     directory could not be fully read), reason, caller,
                     session id, timestamp
```

A symlink is trashed as the link itself, never its target. The tombstone is
written before the move, so a crash between the two leaves an entry that lists
as incomplete rather than an untraceable item.

## Listing and restoring

```bash
~/genesis/.venv/bin/python -m genesis.trash list
~/genesis/.venv/bin/python -m genesis.trash restore <entry> [--to PATH]
```

`restore` moves the item back to its original path (or `--to`) and removes the
entry. It refuses an incomplete entry or a destination that already exists. The
existence check and the move are two steps, so a file created at the
destination in between would be replaced: a narrow race, since the standard
library has no rename that refuses to replace.

Exit codes: 0 done, 1 refused (the reason is printed), 64 usage error.

## What is refused

`genesis.trash` leaves the item untouched and raises `TrashRefused` when the
item is missing, a mount point, `$HOME` or a parent of it, a parent of the
trash itself, already in a trash, on the Claude Code temp volume
(`~/.genesis/cc-tmp`, which has its own retention), or on a different device
from its trash. It never falls back to copying. A per-mount trash root that is
a symlink, owned by another user, or not mode 0700 is refused too.

## What goes to the trash today

- **Cognitive rollback** (`learning/cognitive_ledger.rollback`): removing a
  file a modification created, and a forced rollback over newer content no
  ledger row records. Overwriting content the ledger row already holds trashes
  nothing.
- **Task-worktree reset** (`autonomy/executor/worktree_mgr.create_worktree`): a
  leftover directory git no longer knows. A registered task worktree with
  uncommitted work is never deleted: the task fails with that reason (on every
  retry until it is cleared) and the worktree reaper
  (`scripts/worktree_lifecycle.py`) archives it, once idle, into its own
  worktree trash, `~/.genesis/worktree-trash/`, which keeps git history and
  has its own `--list-trash` / `--recover`. A LOCKED task worktree is never
  reaped; the task fails saying so, and it needs `git worktree unlock` or a
  manual removal.

The dashboard's delete actions are #2926 PR 5, and an advisory on shell `rm` is
PR 6.

## What does not go to the trash, and why

- **Retention prunes**, which delete by age on purpose: attention-snapshot GC,
  voice-transcript pruning, bundle and log rotation. With no trash expiry
  (below), trashing them would mean they never free disk.
- **Tool-own temporary state**: temp files, locks, markers, spools (for example
  the contribution-offer markers Genesis writes and consumes), caches,
  atomic-write temps.
- **Lossless replacements**, such as a gzip that replaces its source only after
  the compressed copy is verified.
- **The worktree trash's own implementation.**
- **Code with no production caller**, left as it is: the browser profile's
  `backup()`/`reset()` and the unwired `pending_reminders_hook.py`. They should
  use the trash if they are ever wired.
- **Task branches, not yet covered**: the executor's worktree cleanup still
  force-deletes a task's branch after the task ends, so a failed task's
  unpushed commits lose their only ref (#3020).

## Retention and backups

Nothing expires trash entries; expiry is blocked on #2504. The disk watchdog's
YELLOW attribution lists `~/.genesis/trash` and `~/.genesis/worktree-trash` on
their own lines, so trash growth is named when a disk fills. `backup.sh` names
its stores explicitly and does not include the trash.
