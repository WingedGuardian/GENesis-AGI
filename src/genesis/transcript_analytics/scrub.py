"""Secret scrubbing for stored text, reusing Genesis's own scrubber.

``scripts/hooks/secret_scrub.py`` is stdlib-only and fail-safe: on an internal
error it returns a placeholder, never the raw input (secret_scrub.py:426-443).
It is imported by path from the Genesis checkout (``GENESIS_REPO``, default
``~/genesis``) because this tool lives outside the repo.

Fail closed: if the scrubber cannot be imported, or it returns its error
placeholder, the text is NOT stored (None) and the row is flagged
``scrub_failed``. The scrubber covers credential shapes, not IPs/emails;
the store is local and mode 0700.
"""

from __future__ import annotations

import os
from pathlib import Path

_PLACEHOLDER = "[scrub-error: content withheld]"


def _path() -> Path:
    from genesis.env import repo_root

    repo = Path(os.environ.get("GENESIS_REPO", repo_root()))
    return repo / "scripts" / "hooks" / "secret_scrub.py"


def _load():
    """Load the scrubber from ONE read of its bytes and hash those same bytes, so
    the stamp always describes the code actually in use (re-audit SF-5)."""
    import hashlib

    path = _path()
    try:
        src = path.read_bytes()
        namespace: dict = {"__name__": "_ta_secret_scrub", "__file__": str(path)}
        exec(compile(src, str(path), "exec"), namespace)  # noqa: S102 - trusted local Genesis file
        return namespace["scrub"], hashlib.sha256(src).hexdigest()[:12]
    except Exception:  # noqa: BLE001 - any failure means: do not store text
        return None, None


_scrub, _VERSION = _load()


def version() -> str | None:
    """Content hash of the loaded scrubber, stamped into every stored source so a
    scrubber upgrade re-scrubs stored text on the next ingest (review SF-2)."""
    return _VERSION


def available() -> bool:
    return _scrub is not None


def scrub_text(text: str | None) -> tuple[str | None, bool]:
    """Return (scrubbed text or None, scrub_failed)."""
    if not text:
        return text, False
    if _scrub is None:
        return None, True
    out = _scrub(text)
    if out == _PLACEHOLDER:
        return None, True
    return out, False
