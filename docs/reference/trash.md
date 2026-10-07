# Trash: recoverable deletes

When Genesis removes user or project data it moves it into a trash instead of
deleting it, so the removal can be undone. This is the second guarantee of the
resource-budget work (#2926): "a deletion of user or project data by Genesis is
recoverable". The code is `src/genesis/trash/` (stdlib only).

## Where things go

There is one trash, `~/.genesis/trash/` (under `GENESIS_HOME`), and an item is
always **renamed** into it, never copied. So only an item on the same volume as
`~/.genesis` can be trashed; on a standard install that covers the repo, its
worktrees, `~/.genesis` and `~/.claude`. An item on another volume is refused
and left where it is (see below).

Each trashed item gets its own entry, named `<UTC timestamp>-<name>` (a `-N`
suffix when the same name is trashed twice in one second):

```
~/.genesis/trash/20261007T024821Z-notes.md/
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
item is missing or unreadable, `''`/`.`/`..`, a mount point, `$HOME` or a parent
of it, a parent of the trash itself, already in the trash, on the Claude Code
temp volume (`~/.genesis/cc-tmp`, which has its own retention), or on another
volume than the trash. It never falls back to copying. "Another volume" is
caught twice: a different device number (another disk, a btrfs subvolume), and
the kernel's own refusal to rename across mount points (a bind mount, an
overlay's lower layer). The refusal says to ask the user before deleting the
item any other way. A trash directory that is a symlink, owned by another
user, or not mode 0700 is refused too, both when trashing and when listing or
restoring (an entry forged into such a directory is never restored).

The trash used to be planned per volume (a trash on each mount, after
`send2trash`). It was narrowed to one trash because every caller lives on the
home volume, and the per-volume roots were where the defects were (aliased
mounts, bind mounts, btrfs subvolumes whose trash could be written but never
listed). If a real caller on another volume appears, that is the time to
revisit it.

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
