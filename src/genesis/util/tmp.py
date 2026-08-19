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


def pytest_basetemp_override(
    current_basetemp: str | None,
    tmpdir_env: str | None,
    home: str,
    big_tmp: str,
) -> str | None:
    """Decide where pytest should root its per-run temp tree. Pure — no I/O.

    pytest's ``tmp_path``/``basetemp`` default to ``<TMPDIR>/pytest-of-<user>/``.
    On a Genesis install ``TMPDIR`` is ``~/.genesis/cc-tmp`` (set for CC sessions
    by ``scripts/cc-slot.sh``), the budget-policed dir the ``genesis-tmp-watchgod``
    service reacts to — so an un-redirected suite dumps hundreds of MB there and
    trips the watchgod. This returns a basetemp UNDER ``big_tmp`` (``~/tmp``, the
    non-budgeted on-disk dir) so the caller can steer ``config.option.basetemp``.

    Redirect ONLY when BOTH hold, else return ``None`` (leave pytest's default):
      * the caller did not already pass ``--basetemp`` (``current_basetemp is None``);
      * ``TMPDIR`` resolves to ``<home>/.genesis/cc-tmp`` or a path under it.

    On CI ``TMPDIR`` is unset → returns ``None`` → no-op (CI keeps its own tmp).
    This never rewrites the process ``TMPDIR`` (see the module docstring — that
    would desync Claude Code's ``TMPDIR``/``CLAUDE_CODE_TMPDIR``); it only names a
    basetemp for pytest's own option.

    Returns the shared ``<big_tmp>/pytest`` base; the CALLER must scope a
    per-process leaf under it (pytest clears an explicit basetemp at session
    start, so concurrent runs sharing one path would delete each other's temp).
    """
    if current_basetemp is not None:
        return None
    if not tmpdir_env:
        return None
    cc_tmp = os.path.realpath(os.path.join(home, ".genesis", "cc-tmp"))
    resolved = os.path.realpath(tmpdir_env)
    if resolved == cc_tmp or resolved.startswith(cc_tmp + os.sep):
        return os.path.join(big_tmp, "pytest")
    return None
