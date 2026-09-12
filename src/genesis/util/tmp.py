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


def should_redirect_pytest_basetemp(
    current_basetemp: str | None,
    tmpdir_env: str | None,
    home: str,
) -> bool:
    """Whether pytest's basetemp should be steered off cc-tmp. Pure — no I/O.

    pytest's ``tmp_path``/``basetemp`` default to ``<TMPDIR>/pytest-of-<user>/``.
    On a Genesis install ``TMPDIR`` is ``~/.genesis/cc-tmp`` (set for CC sessions
    by ``scripts/cc-slot.sh``), the budget-policed dir the ``genesis-tmp-watchgod``
    service reacts to — so an un-redirected suite dumps hundreds of MB there and
    trips the watchgod.

    Redirect ONLY when BOTH hold (else ``False`` — leave pytest's default AND do
    no filesystem work):
      * the caller did not already pass ``--basetemp`` (``current_basetemp is None``);
      * ``TMPDIR`` resolves to ``<home>/.genesis/cc-tmp`` or a path under it.

    On CI ``TMPDIR`` is unset → ``False`` → no-op (CI keeps its own tmp). Being a
    pure predicate (no ``big_tmp_dir()`` call) is deliberate: the caller must not
    create ``~/tmp`` on the no-op / explicit-``--basetemp`` path (that would break
    a read-only-home or CI run during config). The caller redirects to a
    per-process leaf under :func:`big_tmp_dir` (``~/tmp``) only when this is True;
    per-process because pytest clears an explicit basetemp at session start, so
    concurrent runs sharing one path would delete each other's temp. This never
    rewrites the process ``TMPDIR`` (see the module docstring).
    """
    if current_basetemp is not None:
        return False
    if not tmpdir_env:
        return False
    cc_tmp = os.path.realpath(os.path.join(home, ".genesis", "cc-tmp"))
    resolved = os.path.realpath(tmpdir_env)
    return resolved == cc_tmp or resolved.startswith(cc_tmp + os.sep)
