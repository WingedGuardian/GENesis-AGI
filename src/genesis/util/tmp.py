"""Dedicated on-disk location for LARGE temporary files.

Genesis routes its working temp (Claude Code's sandbox, the genesis-server systemd
unit, etc.) to ``~/.genesis/cc-tmp`` via ``TMPDIR`` — a small, budget-policed folder
the ``genesis-tmp-watchgod`` service cleans and, when it fills, reclaims by **killing
idle CC sessions**. So code that produces a LARGE temp file (audio/video downloads,
git worktrees, eval artifacts, DB dumps) must NOT use the default temp dir — it would
land in cc-tmp (or, off the unit, ``/tmp`` which is tmpfs/RAM).

Per the ``tmp_filesystem_limit`` procedure, large temp goes to ``~/tmp`` — an on-disk
dir that is not watchgod-budgeted. Pass :func:`big_tmp_dir` as the ``dir=`` argument to
``tempfile.NamedTemporaryFile`` / ``mkdtemp`` / ``TemporaryDirectory``. Do NOT override
the process ``TMPDIR`` to achieve this — that breaks Claude Code (it assumes
``TMPDIR``/``CLAUDE_CODE_TMPDIR`` consistency) and violates the procedure.
"""

from __future__ import annotations

import os
from pathlib import Path


def big_tmp_dir() -> str:
    """Return a dedicated on-disk dir for large temp files, creating it if missing.

    Honors the ``GENESIS_BIG_TMP`` env override (else ``~/tmp``). Returns a ``str``
    so it can be passed directly as the ``dir=`` argument of ``tempfile`` helpers.
    """
    target = os.environ.get("GENESIS_BIG_TMP") or str(Path.home() / "tmp")
    Path(target).mkdir(parents=True, exist_ok=True)
    return target
