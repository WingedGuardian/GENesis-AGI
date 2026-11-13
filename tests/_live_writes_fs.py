"""Stateless, pre-operation filesystem facts for the report-only write audit.

These are narrow no-effect checks, not syscall-success or permission prediction.
The caller supplies its captured OS bindings and existing pathname resolver.
"""

import stat


def _entry(fs, path):
    if path is None:
        raise ValueError("unresolved audit pathname")
    try:
        return fs.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def open_no_effect(fs, resolve, args):
    path, mode, flags = args
    if isinstance(path, int):
        return False  # fdopen(..., 'x') can write through an existing descriptor
    if mode is None and isinstance(flags, int) and flags & getattr(fs, "O_PATH", 0):
        return True  # Linux ignores data-access/creation flags with O_PATH
    exclusive = (isinstance(mode, str) and "x" in mode) or (
        isinstance(flags, int) and flags & fs.O_CREAT and flags & fs.O_EXCL
    )
    nofollow = isinstance(flags, int) and flags & getattr(fs, "O_NOFOLLOW", 0)
    if not exclusive and not nofollow:
        return False
    # A raw relative open can use an omitted dir_fd. Cwd metadata cannot prove
    # it refuses: keep the core's conservative, explicitly labelled cwd guess.
    if mode is None and not fs.path.isabs(fs.fspath(path)):
        return False
    entry = _entry(fs, resolve(path, follow=False))
    return entry is not None and (bool(exclusive) or stat.S_ISLNK(entry.st_mode))


def name_effects(fs, resolve, event, args):
    """Return (absolute name, operation) observations, or an established no-op.

    link's audit event omits follow_symlinks. Observe the source name and, for
    a symlink, its target conservatively; this is exposure, not a content write.
    """
    source = resolve(args[0], args[2], follow=False)
    source_entry = _entry(fs, source)
    if source_entry is None:
        return ()  # neither destination can change without a source
    destination = resolve(args[1], args[3], follow=False)
    destination_entry = _entry(fs, destination)
    if event == "os.link":
        if destination_entry is not None:
            return ()  # link never replaces even a dangling destination entry
        effects = [(destination, event), (source, "os.link source")]
        if stat.S_ISLNK(source_entry.st_mode):
            target = resolve(args[0], args[2])
            if target is None:
                raise ValueError("unresolved hardlink source target")
            effects.append((target, "os.link source"))
        return effects
    if destination_entry is not None and (
        source_entry.st_dev, source_entry.st_ino
    ) == (destination_entry.st_dev, destination_entry.st_ino):
        return ()  # rename onto the same inode leaves both names intact
    return ((source, event), (destination, event))


def uncertain_name_effects(resolve, event, args):
    """Keep independently resolvable names after the caller records uncertainty."""
    effects = []
    for index in (0, 1):
        try:
            target = resolve(args[index], args[index + 2], follow=False)
        except (OSError, ValueError):
            continue
        operation = "os.link source" if event == "os.link" and index == 0 else event
        effects.append((target, operation))
    return effects
