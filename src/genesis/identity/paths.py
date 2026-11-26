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
    local = found[-1].read_text(encoding="utf-8").lstrip("\ufeff")
    default = found[0].read_text(encoding="utf-8") if len(found) == 2 else ""
    first, _, rest = local.partition("\n")
    if first.strip() == REPLACE_MARKER:
        if len(found) == 1:
            return rest
        # Say so in the text itself: a replaced STEERING.md drops shipped hard
        # constraints, and git cannot show it (the local file is ignored).
        return f"_[The shipped {name} is replaced by {found[-1].name}.]_\n\n{rest}"
    if not local.strip():
        return default if len(found) == 2 else None
    if not default.strip():
        return local
    body = local.strip()
    # A local file that already opens with the separator needs no second one.
    sep = "\n\n" if body.partition("\n")[0].strip() == "---" else _LAYER_SEPARATOR
    return default.rstrip() + sep + body + "\n"
