"""Layered identity editing, shared by the dashboard's two editors (#2862).

CONVERSATION.md and STEERING.md are never written in place: an edit is stored
as the ``<stem>.local.md`` layer that makes the file resolve to it
(``identity.paths.layer_for``), under the identity directory's lock, which the
steering writer also takes. Reads return the resolved text with a version a
save must send back, so a stale save is refused rather than dropping a rule
captured while the editor was open.

Both functions return ``(body, status)`` for the route to send as JSON.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from genesis.identity import paths as identity_paths

logger = logging.getLogger(__name__)


def is_layer(name: str) -> bool:
    """Whether *name* is a LAYER identity file (CONVERSATION.md, STEERING.md)."""
    return identity_paths.POLICY.get(name) == identity_paths.LAYER


def version(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def read_layer(identity_dir: Path, name: str) -> tuple[dict, int]:
    """The resolved text of *name*, its version, and the local file it saves to."""
    local = identity_paths.local_path(name, identity_dir)
    try:
        # The non-blocking regular-file reads first, so a symlink or FIFO at the
        # shipped or local path is refused before the resolver opens it.
        identity_paths.read_regular(identity_dir / name)
        identity_paths.read_regular(local)
        content = identity_paths.read_identity(name, identity_dir)
    except Exception as exc:
        logger.error("Failed to read identity file %s: %s", name, exc, exc_info=True)
        return {"name": name, "content": "(failed to read file)"}, 500
    if content is None:
        return {"error": "not found"}, 404
    return {"name": name, "path": str(local), "content": content, "version": version(content)}, 200


def save_layer(
    identity_dir: Path, name: str, content: str, base_version: object
) -> tuple[dict, int]:
    """Save an edit to *name* into its local layer; the tracked file is never written."""
    if base_version is None:
        return {"error": f"{name} needs the version it was opened at; reload the page"}, 400
    shipped = identity_dir / name
    local = identity_paths.local_path(name, identity_dir)
    try:
        with identity_paths.locked(identity_dir):
            # Regular-file reads first: a symlink or FIFO is refused before the
            # resolver opens it, so the lock is never held across a blocking read.
            existing = identity_paths.read_regular(local)
            default = identity_paths.read_regular(shipped)
            current = identity_paths.read_identity(name, identity_dir)
            if base_version != version(current):
                return {
                    "error": f"{name} changed since you opened it; reload it and edit again"
                }, 409
            try:
                layer = identity_paths.layer_for(name, default, content)
            except ValueError as exc:
                return {"error": str(exc)}, 422
            if layer or existing is not None:
                identity_paths.write_atomic(local, layer)
            saved = identity_paths.read_identity(name, identity_dir)
    except Exception as exc:
        logger.error("Failed to save identity layer %s: %s", name, exc, exc_info=True)
        return {"error": "write failed"}, 500
    logger.info("Dashboard saved %s into %s (%d chars)", name, local.name, len(layer))
    return {
        "status": "ok",
        "name": name,
        "path": str(local),
        "version": version(saved),
        "content": saved,
        "replaced": layer.startswith(identity_paths.REPLACE_MARKER),
    }, 200
