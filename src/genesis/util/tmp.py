"""Dedicated on-disk location for LARGE temporary files.

Genesis routes its working temp (Claude Code's sandbox, the genesis-server systemd
unit, etc.) to ``~/.genesis/cc-tmp`` via ``TMPDIR`` — a quota-capped volume every
session's temp shares, so filling it breaks all of them at once. The
``genesis-tmp-watchgod`` service only sweeps what ENDED sessions left there (untouched
for 7 days, nothing holding it); nothing reclaims a big file parked there in time.
So code that produces a LARGE temp file (audio/video downloads,
git worktrees, eval artifacts, DB dumps) must NOT use the default temp dir — it would
land in cc-tmp (or, off the unit, ``/tmp`` which is tmpfs/RAM).

Per the ``tmp_filesystem_limit`` procedure, large temp goes to ``~/tmp`` — an on-disk
dir on the main disk, outside cc-tmp's quota. Pass :func:`big_tmp_dir` as the ``dir=`` argument to
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
    ci_env: str | None,
    tmpdir_env: str | None = None,
) -> bool:
    """Whether pytest's basetemp should be steered to ``~/tmp``. Pure — no I/O.

    pytest's ``tmp_path``/``basetemp`` default to ``<TMPDIR>/pytest-of-<user>/``,
    and on this project BOTH of the places that resolves to are small and policed:

    * ``TMPDIR=~/.genesis/cc-tmp`` in a CC session (set by ``scripts/cc-slot.sh``) —
      the budget-policed dir ``genesis-tmp-watchgod`` reclaims by killing idle
      sessions;
    * ``TMPDIR`` unset or ``/tmp`` anywhere else — and on an install whose
      ``/tmp`` is a tmpfs mount (common, and the case this was written for) that
      is RAM behind a hard kernel cap, typically a few hundred MB.

    MEASURED 2026-09-24 on one such install: a suite run from a context with no
    ``TMPDIR`` put two basetemp trees totalling 255 MB into a 512 MB RAM disk
    and paged the operator. Where ``/tmp`` is an ordinary on-disk directory the
    redirect costs nothing but the move; it is not conditioned on detecting
    which one you have, because a size probe is a runtime-varying decision on a
    path that otherwise has none, and the project's own temp policy sends large
    temp to ``~/tmp`` either way. The earlier form of this predicate redirected *only* when ``TMPDIR``
    already resolved to cc-tmp, so every other entry path — a ``systemd-run`` unit,
    a detached shell, a subprocess with a scrubbed environment, a plain
    ``TMPDIR=/tmp`` — fell through to that default.

    So the polarity is now ALLOWLIST rather than a list of temp dirs known to be
    hazardous: **redirect by default**, with exactly two exemptions.

      * an explicit ``--basetemp`` (``current_basetemp is not None``) — never
        override a caller who named a location;
      * CI (``ci_env`` set to anything but a falsey spelling) — a hosted runner
        keeps its own ample temp, and its ``$HOME`` may be read-only. EXCEPT when
        ``tmpdir_env`` points inside ``~/.genesis/cc-tmp``: no hosted runner has
        that directory, so a CI-marked run there is a LOCAL run (``CI=true
        pytest`` inside a CC session), and exempting it would put the suite's
        temp tree into the policed directory this redirect exists to protect.

    Enumerating hazardous locations instead would be a denylist, and the next
    small temp dir to appear would silently not be on it.

    ``GENESIS_BIG_TMP`` is the supported relocation knob for an operator who wants
    the suite's temp somewhere other than ``~/tmp`` (see :func:`big_tmp_dir`); it
    keeps the redirect and moves its destination, which ``--basetemp`` does not.

    Purity is deliberate: the caller must not create ``~/tmp`` on the no-op path
    (that would break a read-only-home run during config). The caller redirects to
    a per-process leaf under :func:`big_tmp_dir` only when this is True —
    per-process because pytest clears an explicit basetemp at session start, so
    concurrent runs sharing one path would delete each other's temp. This never
    rewrites the process ``TMPDIR`` (see the module docstring).
    """
    if current_basetemp is not None:
        return False
    return not _is_ci(ci_env) or _in_cc_tmp(tmpdir_env)


#: The budget-policed working temp (see the module docstring), as a template.
_CC_TMP = os.path.join("~", ".genesis", "cc-tmp")


def _in_cc_tmp(tmpdir_env: str | None) -> bool:
    """Whether a raw ``$TMPDIR`` value names cc-tmp or a path under it. Pure:
    lexical (``expanduser`` + ``normpath``), so it never touches the filesystem."""
    if not tmpdir_env:
        return False
    cc_tmp = os.path.normpath(os.path.expanduser(_CC_TMP))
    target = os.path.normpath(os.path.expanduser(tmpdir_env))
    return target == cc_tmp or target.startswith(cc_tmp + os.sep)


# ``CI`` is the de-facto cross-vendor signal; GitHub Actions (this repo's only CI —
# 9 workflows, none of which set it themselves) documents it as "Always set to
# ``true``" in its default-variables reference, consulted 2026-09-24. The falsey
# spellings are honoured because ``CI=false`` is the conventional way tooling opts
# a run OUT of CI behaviour, and reading that as "on CI" would invert it.
_CI_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _is_ci(ci_env: str | None) -> bool:
    """Whether a raw ``$CI`` value means "running on CI". Pure."""
    if ci_env is None:
        return False
    return ci_env.strip().lower() not in _CI_FALSEY
