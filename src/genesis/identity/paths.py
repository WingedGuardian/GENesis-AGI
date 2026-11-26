"""Where an identity file's text comes from: the one resolver every reader uses.

Stdlib only: the SessionStart hook loads this module by file path before the
genesis package is importable (``scripts/genesis_session_context.py``).

Three policies (#2862):

* SEED (SOUL, VOICE): the per-install ``<name>`` if present, else the shipped
  ``<name>.example``.
* LAYER (CONVERSATION, STEERING): the shipped ``<name>`` plus the per-install
  ``<stem>.local.md``, appended after a ``---`` separator. A local file whose
  first line is ``<!-- genesis: replace -->`` replaces the shipped text, and
  the result opens with a line saying so.
* PLAIN (every other name): ``<name>`` as it stands.
"""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SEED = "seed"
LAYER = "layer"
PLAIN = "plain"

POLICY = {
    "SOUL.md": SEED,
    "VOICE.md": SEED,
    "CONVERSATION.md": LAYER,
    "STEERING.md": LAYER,
}

REPLACE_MARKER = "<!-- genesis: replace -->"
_LAYER_SEPARATOR = "\n\n---\n\n"


def sources(name: str, identity_dir: Path) -> list[Path]:
    """The existing files ``read_identity`` composes for *name*, in order."""
    # exists(), not is_file(): a directory or other non-file in a file's place
    # is present, so reading it raises and the caller reports it as unreadable.
    path = identity_dir / name
    policy = POLICY.get(name, PLAIN)
    if policy == SEED:
        example = identity_dir / f"{name}.example"
        return [path] if path.exists() else [example] if example.exists() else []
    found = [path] if path.exists() else []
    if policy == LAYER:
        local = identity_dir / f"{Path(name).stem}.local.md"
        if local.exists():
            found.append(local)
    return found


def local_path(name: str, identity_dir: Path) -> Path:
    """The per-install layer file for a LAYER *name*: ``<stem>.local.md``."""
    return identity_dir / f"{Path(name).stem}.local.md"


def read_identity(name: str, identity_dir: Path) -> str | None:
    """The text a reader should use for *name*, or None when no file exists.

    A single source comes back verbatim. Read errors (OSError,
    UnicodeDecodeError) propagate: callers report "unreadable" differently
    from "absent".
    """
    found = sources(name, identity_dir)
    if not found:
        return None
    if POLICY.get(name) != LAYER or found[-1].name == name:
        return found[0].read_text(encoding="utf-8")
    default = found[0].read_text(encoding="utf-8") if len(found) == 2 else None
    return compose(name, default, found[-1].read_text(encoding="utf-8"))


def _replace_note(name: str) -> str:
    return f"_[The shipped {name} is replaced by {Path(name).stem}.local.md.]_"


def compose(name: str, default: str | None, local: str | None) -> str | None:
    """A LAYER name's resolved text from its shipped text and its local layer.

    None for either means that file is absent.
    """
    if local is None:
        return default
    local = local.lstrip("\ufeff")
    first, _, rest = local.partition("\n")
    if first.strip() == REPLACE_MARKER:
        if default is None:
            return rest
        # Say so in the text itself: a replaced STEERING.md drops shipped hard
        # constraints, and git cannot show it (the local file is ignored).
        return f"{_replace_note(name)}\n\n{rest}"
    if not local.strip():
        return default
    if default is None or not default.strip():
        return local
    body = local.strip()
    # A local file that already opens with the separator needs no second one.
    sep = "\n\n" if body.partition("\n")[0].strip() == "---" else _LAYER_SEPARATOR
    return default.rstrip() + sep + body + "\n"


def layer_for(name: str, default: str | None, effective: str) -> str:
    """The local-layer text that makes *name* resolve to *effective*.

    An edit that only adds after the shipped text becomes an append layer, so
    later shipped changes still arrive; any other edit becomes a replace layer
    (the resolved text then says so). A leading replace note, as a reader was
    shown it, is dropped first, and CRLF or a BOM is normalized. Text equal to
    the shipped file gives "". Empty text over a shipped file raises
    ValueError: an empty replace layer would drop every shipped line.
    """
    effective = effective.lstrip("\ufeff").replace("\r\n", "\n")
    note = _replace_note(name)
    if effective.startswith(note):
        effective = effective[len(note):].lstrip("\n")
    if default is None:
        return effective
    default = default.lstrip("\ufeff").replace("\r\n", "\n")
    if effective.strip() == default.strip():
        return ""
    if not effective.strip():
        raise ValueError(f"an empty {name} would drop every shipped line; remove text selectively")
    prefix = default.rstrip()
    if effective.startswith(prefix):
        rest = effective[len(prefix):].strip()
        typed_separator = rest.partition("\n")[0].strip() == "---"
        layer = (rest if typed_separator else "---\n\n" + rest) + "\n"
        composed = (compose(name, default, layer) or "").strip()
        # The resolver puts a `---` between the shipped text and the layer, so an
        # addition typed without one comes back with it: still the same addition.
        # Only an addition that starts on a new line: text added to the shipped
        # text's last line changes that line, and is not an addition after it.
        on_new_line = effective[len(prefix):].startswith("\n")
        if composed == effective.strip() or (
            not typed_separator
            and on_new_line
            and composed == f"{prefix}{_LAYER_SEPARATOR}{rest}".strip()
        ):
            return layer
    return f"{REPLACE_MARKER}\n{effective}"


@contextmanager
def locked(identity_dir: Path) -> Iterator[None]:
    """Hold the identity directory's lock: every local-layer writer takes it."""
    fd = os.open(identity_dir, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_regular(path: Path) -> str | None:
    """The text of *path*, None when absent; never through a symlink or a non-file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read().decode("utf-8")


def write_atomic(path: Path, text: str) -> None:
    """Swap in *text* as a fresh 0600 file made under a random name, fsynced."""
    tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix="." + path.name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
