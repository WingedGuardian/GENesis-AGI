"""Deploy-staleness snapshot — is what's MERGED actually DEPLOYED here?

Genesis installs pull merged PRs two ways: a full ``scripts/update.sh`` run
(bootstrap + migrations + systemd sync + guardian redeploy + pin healing) or a
bare ``git merge`` between updates. The bare merge deploys tier-1 (code loads
on restart, schema migrations self-apply at boot) but silently skips tier-2 —
systemd unit installation, guardian host redeploy, CC/Node pins. Observed
live 2026-07-13: six days of manual merges left a shipped timer uninstalled
and the host guardian 67 files behind, with zero signal anywhere.

This snapshot makes that drift visible:

- ``last_update``  — most recent successful ``update_history`` row + age
- ``git``          — commits behind upstream, fetch age (local refs only —
  NEVER fetches; a health probe must not do network I/O)
- ``units``        — systemd unit files missing vs ``scripts/systemd/*.template``
- ``tier2_pending`` — update.sh-only paths changed since the last successful
  update (the predictive "you need to run update.sh" signal)
- ``host_gateway`` — guardian host deployed_commit drift vs HEAD, read from
  the state file the nightly cc-align timer / update.sh write (no SSH here)
- ``main_checkout`` — tracked files edited in place in the deploy checkout,
  judged by the deploy scripts' own predicate (``scripts/lib/
  deploy_checkout.sh``, run through bash): the state in which the next
  ``deploy_code_only.sh`` or ``update.sh`` run refuses. Not drift between
  merged and deployed, so the awareness check words it separately
- ``live``         — whether the checkout runs `live`, the integration branch
  ``scripts/deploy_candidates`` rebuilds from the deploy manifest. On `live`
  the behind-count, tier-2 pending and host drift are measured from the
  commit `live` was built on, never HEAD, so a candidate's own files never
  read as undeployed merges
- ``live_repairs`` — what the live-move repair (scripts/lib/live_move_repair.py)
  reported after healing a half-written `live` checkout: paths it saved that
  were not the update's, an unresolved repair

The awareness tick's ``_check_deploy_staleness`` consumes the same collectors
to raise a dashboard/morning-report observation. Everything is best-effort:
collectors degrade to ``None``/empty and never raise into the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import signal
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

# Same-package probe helper: never raises, rc -1 = timeout, -2 = exec failure.
from genesis.observability.git_health import _CHEAP_TIMEOUT_S, _run_git

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Paths whose changes only ACTIVATE via scripts/update.sh (tier-2). A bare
# git-merge that touches these leaves the install running stale plumbing until
# update.sh runs. File-level granularity is deliberate — a docs-only edit to
# bootstrap.sh costs one advisory observation, not a missed deploy.
TIER2_PATHS = (
    "scripts/systemd",
    "scripts/bootstrap.sh",
    "scripts/update.sh",
    "scripts/lib/cc_version.sh",
    "scripts/hooks",
    "pyproject.toml",
)

# Guardian-relevant paths — keep in LOCKSTEP with update.sh GUARDIAN_PATHS
# (the redeploy trigger). If update.sh's list changes, change this one.
GUARDIAN_HOST_PATHS = (
    "src/genesis/guardian",
    "src/genesis/util",
    "src/genesis/env.py",
    "src/genesis/observability",
    "src/genesis/db",
    "config/guardian-claude.md",
    "config/genesis-guardian.service",
    "config/genesis-guardian.timer",
    "config/genesis-guardian-watchman.service",
    "config/genesis-guardian-watchman.timer",
    "pyproject.toml",
    "scripts/install_guardian.sh",
    "scripts/guardian-gateway.sh",
    "scripts/lib/host_swap.sh",
    "scripts/lib/cc_tmp_volume.sh",
)

_HOST_STATE_FILE = "host_gateway_state.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Collectors (sync, injectable paths, never raise) ────────────────


def collect_git_facts(repo: Path, now: datetime | None = None, *, on_live: bool = False) -> dict:
    """Local-refs-only git staleness facts. No network, ever. ``on_live``: the
    checkout runs `live`, which the engine creates from a commit and so gives
    no upstream (MEASURED, git 2.43: ``switch -C live <sha>`` configures none);
    its behind-count is origin/main's commits since the rebuild's base."""
    now = now or _utcnow()
    facts: dict = {
        "head": None,
        "commits_behind_upstream": None,
        "fetch_age_hours": None,
    }
    try:
        rc, out, _ = _run_git(repo, "rev-parse", "--short", "HEAD", timeout=_CHEAP_TIMEOUT_S)
        if rc == 0:
            facts["head"] = out.strip()
        # Behind-count against the current branch's upstream (origin/main on a
        # standard install). Counts against the LAST FETCHED state — pair with
        # fetch_age_hours to judge how trustworthy the number is.
        upstream = _BASE_REF if on_live else "@{upstream}"
        rc, out, _ = _run_git(
            repo, "rev-list", "--count", f"HEAD..{upstream}", timeout=_CHEAP_TIMEOUT_S
        )
        if rc == 0:
            facts["commits_behind_upstream"] = int(out.strip())
        rc, gcd, _ = _run_git(repo, "rev-parse", "--git-common-dir", timeout=_CHEAP_TIMEOUT_S)
        if rc == 0 and gcd.strip():
            git_dir = Path(gcd.strip())
            if not git_dir.is_absolute():
                git_dir = repo / git_dir
            fetch_head = git_dir / "FETCH_HEAD"
            if fetch_head.exists():
                age_s = now.timestamp() - fetch_head.stat().st_mtime
                facts["fetch_age_hours"] = round(age_s / 3600, 1)
    except Exception:
        logger.debug("deploy_health: git facts collection failed", exc_info=True)
    return facts


def collect_missing_units(template_dir: Path, unit_dir: Path) -> list[str] | None:
    """Systemd unit files expected from templates but absent from the user
    unit dir. Bootstrap syncs EVERY ``*.template`` (opt-in applies to timer
    ENABLEMENT, not file presence), so any missing file means bootstrap has
    not run since that template landed. ``None`` = could not determine."""
    try:
        if not template_dir.is_dir():
            return None
        expected = sorted(t.name.removesuffix(".template") for t in template_dir.glob("*.template"))
        if not expected:
            return None
        return [name for name in expected if not (unit_dir / name).exists()]
    except Exception:
        logger.debug("deploy_health: unit comparison failed", exc_info=True)
        return None


#: A commit name as ``scripts/update.sh`` writes it into
#: ``update_history.new_commit`` — ``git rev-parse --short``, i.e. ABBREVIATED
#: (8-9 hex on every row of this install, MEASURED 2026-09-16 across 91 rows).
#: The floor is 4 because that is git's own minimum: MEASURED on git 2.43.0,
#: ``core.abbrev=4`` prints a 4-hex name while ``core.abbrev=3`` errors with
#: "abbrev length out of range". Do not raise it to the length this install
#: happens to use — an install with a lower ``core.abbrev`` would then have
#: every stored commit refused.
_ABBREV_SHA = re.compile(r"^[0-9a-f]{4,40}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def resolve_commit(repo: Path, name: str | None) -> tuple[str | None, str]:
    """Expand a stored (abbreviated) commit name to its full 40-hex SHA.

    Returns ``(sha, reason)``; ``sha`` is None when the name does not name a
    commit in this clone, and ``reason`` always says why.

    **Resolved against the OBJECT STORE only, never the ref namespace**, and
    that is the whole reason this is not a one-line ``rev-parse --verify``.
    MEASURED on git 2.43.0: with a branch named after an 8-hex prefix of a
    DIFFERENT commit, ``git rev-parse --verify --quiet '<prefix>^{commit}'``
    returns the BRANCH's commit, exit 0, **stderr empty** — the refname wins
    over the object name and ``--quiet`` suppresses the ambiguity warning
    entirely, so a caller gets a confident, silently wrong SHA. A tag shadows
    it the same way. ``--disambiguate`` consults only the object store, so no
    ref can reach it, and it LISTS every match, making a genuinely ambiguous
    prefix an error rather than a silent pick.

    ``cat-file -t`` is load-bearing rather than belt-and-braces:
    ``--disambiguate`` happily returns blobs and trees, and a 40-hex name can
    be either, so "resolved" must not be allowed to mean merely "exists".

    Note ``--disambiguate`` reports an absent object as rc=0 with EMPTY output
    rather than a non-zero rc — the ``not names`` half of the check below is
    what catches that, not the ``rc != 0`` half.

    The name is shape-validated before it reaches argv even though it comes
    from our own database or state files — a value like ``--upload-pack=…``
    arriving at a subprocess is a different class of problem than a wrong
    verdict.
    """
    if not isinstance(name, str) or not _ABBREV_SHA.match(name.strip()):
        return None, f"not a commit name: {name!r}"
    candidate = name.strip()
    rc, out, err = _run_git(
        repo, "rev-parse", f"--disambiguate={candidate}", timeout=_CHEAP_TIMEOUT_S
    )
    if rc in (-1, -2):
        # _run_git's own sentinels: -1 timeout, -2 exec failure. "I could not
        # check" and "it is not here" have different remedies, so they must not
        # share a message.
        return None, f"could not check {candidate} (git {'timed out' if rc == -1 else 'failed'})"
    names = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0 or not names:
        return (
            None,
            f"{candidate} names no object in this clone ({err.strip()[:200] or f'rc={rc}'})",
        )
    if len(names) > 1:
        return None, f"{candidate} is ambiguous — it matches {len(names)} objects"
    resolved = names[0]
    if not _FULL_SHA.match(resolved):
        return None, f"{candidate} resolved to something that is not a SHA: {resolved!r}"
    rc, kind, _ = _run_git(repo, "cat-file", "-t", resolved, timeout=_CHEAP_TIMEOUT_S)
    if rc != 0 or kind.strip() != "commit":
        return None, f"{candidate} names a {kind.strip() or 'missing'} object, not a commit"
    return resolved, f"{candidate} resolved to {resolved}"


def collect_tier2_pending(
    repo: Path, since_commit: str | None, upto: str | None = "HEAD"
) -> list[str] | None:
    """Tier-2 files changed since the last successful update.sh commit.

    Non-empty means "a bare merge brought update.sh-only changes" — the
    predictive signal. ``None`` = no baseline (no successful update recorded,
    or its commit no longer resolves after a rebase/gc), or no ``upto`` (on
    `live` whose base cannot be read)."""
    if not since_commit or not upto:
        return None
    # Through the shared resolver: this was one of three copies of the same
    # short-SHA adapter (the third is scripts/update.sh, in shell). One home
    # means the shadowing-ref defect documented above is fixed everywhere at
    # once rather than in whichever copy someone happens to open.
    resolved, _ = resolve_commit(repo, since_commit)
    if resolved is None:
        return None
    try:
        rc, out, _ = _run_git(
            repo,
            "diff",
            "--name-only",
            f"{resolved}..{upto}",
            "--",
            *TIER2_PATHS,
            timeout=_CHEAP_TIMEOUT_S,
        )
        if rc != 0:
            return None
        return [line for line in out.splitlines() if line.strip()]
    except Exception:
        logger.debug("deploy_health: tier2 diff failed", exc_info=True)
        return None


def collect_host_gateway(
    repo: Path, state_path: Path, now: datetime | None = None, *, upto: str | None = "HEAD"
) -> dict:
    """Guardian host deploy drift, from the state file cc_align_host_sync
    writes on every gateway ``version`` probe (update.sh + nightly timer).

    ``status`` values: ``no_data`` (guardian-less install or probe never ran),
    ``ok`` (host at HEAD or no guardian-path delta), ``drift`` (guardian paths
    changed since the host's deployed commit), ``unknown_commit`` (host commit
    doesn't resolve locally — converge via update.sh). ``upto`` is HEAD, or on
    `live` the commit `live` was built on (candidate guardian code never reaches
    the host); None when that cannot be read reports ``no_data``."""
    now = now or _utcnow()
    try:
        if not upto:
            return {"status": "no_data", "reason": "the base `live` was built on is unreadable"}
        if not state_path.exists():
            return {"status": "no_data"}
        data = json.loads(state_path.read_text())
        version = data.get("version") or {}
        deployed = (version.get("deployed_commit") or "").strip()
        checked_at = data.get("checked_at")
        age_hours = None
        if checked_at:
            try:
                checked_dt = datetime.fromisoformat(checked_at)
                if checked_dt.tzinfo is None:
                    checked_dt = checked_dt.replace(tzinfo=UTC)
                age_hours = round((now - checked_dt).total_seconds() / 3600, 1)
            except ValueError:
                pass
        out: dict = {
            "deployed_commit": deployed or None,
            "checked_at": checked_at,
            "age_hours": age_hours,
        }
        if not deployed or deployed == "unknown":
            out["status"] = "unknown_commit"
            return out
        # Through the shared resolver, and it matters most HERE: `deployed` is
        # read from a state file the guardian writes, so it is the least trusted
        # of the adapter's inputs. resolve_commit shape-checks it before it
        # reaches argv as well as refusing a shadowing refname.
        resolved_deployed, _ = resolve_commit(repo, deployed)
        if resolved_deployed is None:
            out["status"] = "unknown_commit"
            return out
        rc, diff_out, _ = _run_git(
            repo,
            "diff",
            "--name-only",
            f"{resolved_deployed}..{upto}",
            "--",
            *GUARDIAN_HOST_PATHS,
            timeout=_CHEAP_TIMEOUT_S,
        )
        if rc != 0:
            out["status"] = "unknown_commit"
            return out
        drift_files = [line for line in diff_out.splitlines() if line.strip()]
        out["drift_files"] = len(drift_files)
        out["status"] = "drift" if drift_files else "ok"
        return out
    except Exception:
        logger.debug("deploy_health: host gateway check failed", exc_info=True)
        return {"status": "no_data"}


#: How many dirty paths the ``main_checkout`` dict names; ``count`` stays exact
#: and ``paths_omitted`` says how many more there are. 20 is a display budget
#: for a dashboard row and an alert sentence, chosen, not measured: a deploy
#: root is normally clean, and a tree with more than 20 edited files is a mass
#: edit whose first 20 names already say what happened. The full list is one
#: ``git status`` away in the checkout itself.
MAIN_CHECKOUT_PATHS_SHOWN = 20

# The deploy scripts' own "may a deploy touch this checkout?" predicate, run
# through bash so there is ONE definition of a dirty deploy root (the ephemeral
# allowlist, the hidden assume-unchanged/skip-worktree edits). $1 is the root,
# $2/$3 the two libs: their paths are built in Python as `/`-join chains, which
# is the shape the deploy_status.sh registration test can see. Exit codes:
#   0  the predicate ran; its stdout is the tracked dirty lines (none = clean)
#   2  git could not answer (the predicate's own unreadable status, or no git dir)
#   3  not a primary checkout: a linked worktree or a clone parked in a worktree
#      path, i.e. a dev tree running the code, not the deploy root
#   4  a lib could not be sourced
_MAIN_CHECKOUT_PROBE = (
    # pipefail, as both deploy scripts run the lib: without it a failed producer
    # inside a pipeline (git ls-files feeding xargs) reads as success, and an
    # unreadable hidden edit would report the checkout clean.
    "set -o pipefail\n"
    'source "$2" || exit 4\n'
    'source "$3" || exit 4\n'
    # An empty allowlist pattern makes `grep -vE ""` drop every line: clean.
    '[ -n "${EPHEMERAL_DIRTY_RE:-}" ] || exit 4\n'
    'genesis_checkout_git_dirs "$1"\n'
    '[ -n "$_git_dir" ] && [ -n "$_common_dir" ] || exit 2\n'
    # Read here first: the lib's primary-checkout test swallows a failure of this
    # call as "not a primary checkout", which would read as not_deploy_root.
    'git -C "$1" rev-parse --is-inside-work-tree >/dev/null || exit 2\n'
    'genesis_is_primary_checkout "$1" "$_git_dir" "$_common_dir" || exit 3\n'
    'genesis_tracked_dirty_paths "$1"\n'
)


def _probe_reason(rc: int, err: str) -> str:
    last = next((line.strip() for line in reversed(err.splitlines()) if line.strip()), "")
    return f"probe exited {rc}" + (f": {last}" if last else "")


# git's C-style path quoting (quote.c): inside the double quotes a backslash
# introduces one of these escapes, or three octal digits encoding one byte.
_GIT_C_ESCAPES = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("t"): 0x09,
    ord("n"): 0x0A,
    ord("v"): 0x0B,
    ord("f"): 0x0C,
    ord("r"): 0x0D,
    ord('"'): 0x22,
    ord("\\"): 0x5C,
}
_OCTAL_DIGITS = frozenset(b"01234567")


def _git_unquote(name: bytes) -> bytes:
    """Undo git's C-style quoting of one path (``"caf\\303\\251.md"`` is
    ``café.md``); an unquoted path comes back as is. git quotes a path that
    holds a space, a quote, a backslash or a control character, and, under the
    default ``core.quotePath``, any byte above 0x7F. Never raises: an escape git
    would not emit is kept literally."""
    if len(name) < 2 or name[:1] != b'"' or name[-1:] != b'"':
        return name
    body, out, i = name[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] == 0x5C and i + 1 < len(body):
            if body[i + 1] in _GIT_C_ESCAPES:
                out.append(_GIT_C_ESCAPES[body[i + 1]])
                i += 2
                continue
            digits = body[i + 1 : i + 4]
            if len(digits) == 3 and all(d in _OCTAL_DIGITS for d in digits):
                out.append(int(digits, 8) & 0xFF)
                i += 4
                continue
        out.append(body[i])
        i += 1
    return bytes(out)


def _dirty_paths(out: bytes) -> list[str]:
    """Distinct file names in the predicate's porcelain lines, first-seen order.

    A line is two status columns, a space and the path (``--no-renames``, so
    never an ``a -> b`` pair; a name holding a newline is always quoted, so
    splitting on newlines is safe). The names are for display, so the bytes are
    decoded with any invalid UTF-8 shown escaped: a decode error must not take
    the snapshot down. One file can appear twice, as a staged change in ``git
    status`` and a hidden worktree change from the assume-unchanged pass, and
    is counted once: ``count`` is files, not status records."""
    names: dict[bytes, None] = {}  # deduplicated on the raw bytes, before the lossy decode
    for line in out.split(b"\n"):
        if len(line) > 3:
            names.setdefault(_git_unquote(line[3:]), None)
    return [name.decode("utf-8", "backslashreplace") for name in names]


def _git_dir_of(repo: Path) -> Path | None:
    """``repo``'s git directory without running git: ``.git`` itself, or the
    directory a ``gitdir:`` file names. None when neither reads."""
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        line = dot_git.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line.removeprefix("gitdir:").strip())
    return target if target.is_absolute() else repo / target


def _remove_probe_scratch(pid: int, repo: Path | None) -> None:
    """Remove the scratch index a probe with this pid left in ``repo``'s git
    directory. The lib names it after the probe shell's pid ($$)."""
    git_dir = _git_dir_of(repo) if repo is not None else None
    if git_dir is not None:
        for leftover in git_dir.glob(f"genesis-hidden-index.{pid}.*"):
            with contextlib.suppress(OSError):
                leftover.unlink()


def _kill_probe_group(proc: subprocess.Popen, repo: Path | None = None) -> None:
    # start_new_session made the probe its own group leader, so its pid is the
    # group id; > 1 is checked anyway, since killpg(1) signals everything.
    if proc.pid > 1:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        # A probe killed mid hidden-edit pass never reaches the lib's rm, and
        # every later timed-out run would leave another index copy. The lib
        # names the scratch file after the probe shell's pid ($$, which is
        # proc.pid), so only this probe's copy matches. Removed before the
        # reap: until then the pid cannot be reused by another process.
        _remove_probe_scratch(proc.pid, repo)
    # Bounded even now: a descendant that left the group (none in these libs)
    # would hold the pipes open, and an unbounded read would wait on it. The
    # reap is bounded as well: SIGKILL stays pending while the leader is in
    # uninterruptible I/O, the very failure the timeout contains, and an
    # unbounded wait() would strand the snapshot worker on it. Popen reaps a
    # process left this way when the object is collected.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=5)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("deploy_health: checkout probe %s did not exit after SIGKILL", proc.pid)


def collect_main_checkout_dirty(repo: Path, *, timeout: float = _CHEAP_TIMEOUT_S) -> dict:
    """Tracked files edited in place in the deploy checkout (ephemeral paths
    excluded): the condition under which ``deploy_code_only.sh`` and
    ``update.sh`` refuse the next deploy.

    ``status`` is one of:

    - ``clean`` / ``dirty`` — the predicate answered; ``count`` is the exact
      number of files and ``paths`` names at most
      :data:`MAIN_CHECKOUT_PATHS_SHOWN` of them, with ``paths_omitted`` saying
      how many more there are. ``clean`` answers the tracked-edit question
      only: the deploy scripts also refuse on the branch and on incoming files
      that collide with untracked ones, which this does not check;
    - ``not_deploy_root`` — ``repo`` is a linked worktree (a dev tree running
      the code), so it is not the checkout deploys touch;
    - ``deploying`` — a deploy is in progress (``env.update_in_progress()``):
      not probed, since a deploy's own merge would read as dirty;
    - ``unknown`` — it could not be read (a lib failed to source, git failed,
      the probe timed out or could not start, or the collector itself failed);
      ``reason`` says which. Never reported as clean: an unreadable tree must
      not resolve a standing alert. One exception, shared with the deploy
      scripts: git status skips a subdirectory it cannot open and still exits
      0, so an edit under one reads clean here exactly as it does for them.

    Never raises: like every other collector here it degrades on its own
    failure, so the rest of the snapshot survives one bad read.

    Read-only by contract. ``GIT_OPTIONAL_LOCKS=0`` stops ``git status`` from
    refreshing and rewriting the index (which takes ``index.lock`` and could make
    a concurrent deploy's merge fail), and git's location variables are scrubbed
    so the probe reads ``repo`` and nothing else. The predicate's hidden-edit
    pass writes one scratch index inside ``.git`` when a flagged entry exists and
    removes it when it finishes; when the timeout kills the probe mid-pass, the
    collector removes that probe's copy itself. The probe runs in its own
    process group, and a timeout kills the whole group, so no git grandchild
    outlives it.
    """
    try:
        return _collect_main_checkout_dirty(repo, timeout)
    except Exception as exc:
        logger.warning("deploy_health: main-checkout collector failed", exc_info=True)
        return {
            "status": "unknown",
            "count": 0,
            "paths": [],
            "reason": f"collector failed: {type(exc).__name__}",
        }


def _collect_main_checkout_dirty(repo: Path, timeout: float) -> dict:
    from genesis import env
    from genesis.session_awareness.zero_drop_git import scrubbed_git_env

    if env.update_in_progress():
        return {"status": "deploying", "count": 0, "paths": []}
    marker_lib = repo / "scripts" / "lib" / "deploy_marker.sh"
    checkout_lib = repo / "scripts" / "lib" / "deploy_checkout.sh"
    run_env = scrubbed_git_env()
    run_env["GIT_OPTIONAL_LOCKS"] = "0"
    argv = ["bash", "-c", _MAIN_CHECKOUT_PROBE, "_", str(repo), str(marker_lib), str(checkout_lib)]
    try:
        # Bytes, not text: a tracked name need not be valid UTF-8 (raw under
        # core.quotePath=false), and decoding the whole stream would raise.
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell interpolation
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=run_env,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("deploy_health: main-checkout probe could not start: %s", exc)
        return {"status": "unknown", "count": 0, "paths": [], "reason": f"could not start: {exc}"}
    try:
        out, err_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_probe_group(proc, repo)
        logger.warning("deploy_health: main-checkout probe timed out after %ss", timeout)
        # A deploy that began meanwhile explains a slow read better than a fault.
        if env.update_in_progress():
            return {"status": "deploying", "count": 0, "paths": []}
        return {
            "status": "unknown",
            "count": 0,
            "paths": [],
            "reason": f"timed out after {timeout:g}s",
        }
    except BaseException:
        _kill_probe_group(proc, repo)
        raise
    rc = proc.returncode
    if rc < 0:
        # Killed by a signal from outside (an OOM kill), possibly mid hidden-edit
        # pass, before the lib's own rm.
        _remove_probe_scratch(proc.pid, repo)
    # A deploy that began while the probe ran (deploy_code_only.sh pull keeps the
    # server up) may have been mid-merge under it: its reading is not the
    # checkout's, so it reports the same state a deploy found up front does.
    if env.update_in_progress():
        return {"status": "deploying", "count": 0, "paths": []}
    if rc == 3:
        return {"status": "not_deploy_root", "count": 0, "paths": []}
    if rc != 0:
        err = err_bytes.decode("utf-8", "backslashreplace")
        logger.warning("deploy_health: main-checkout probe rc=%s stderr=%s", rc, err.strip())
        return {"status": "unknown", "count": 0, "paths": [], "reason": _probe_reason(rc, err)}
    # The predicate already dropped untracked and ephemeral lines.
    paths = _dirty_paths(out)
    shown = paths[:MAIN_CHECKOUT_PATHS_SHOWN]
    return {
        "status": "dirty" if paths else "clean",
        "count": len(paths),
        "paths": shown,
        "paths_omitted": len(paths) - len(shown),
    }


async def last_success_update(db: aiosqlite.Connection | None) -> dict:
    """Most recent successful update_history row (age computed by caller UIs).

    ``None`` fields = table missing/empty (pre-first-update install)."""
    if db is None:
        return {"completed_at": None, "new_commit": None, "age_days": None}
    try:
        from genesis.db.crud.update_history import last_successful_update

        row = await last_successful_update(db)
        if row is None:
            return {"completed_at": None, "new_commit": None, "age_days": None}
        completed_at, new_commit = row[0], (row[1] or None)
        age_days = None
        try:
            completed_dt = datetime.fromisoformat(completed_at)
            if completed_dt.tzinfo is None:
                completed_dt = completed_dt.replace(tzinfo=UTC)
            age_days = round((_utcnow() - completed_dt).total_seconds() / 86400, 1)
        except ValueError:
            pass
        return {
            "completed_at": completed_at,
            "new_commit": new_commit,
            "age_days": age_days,
        }
    except Exception:
        logger.debug("deploy_health: update_history query failed", exc_info=True)
        return {"completed_at": None, "new_commit": None, "age_days": None}


_LIVE_REF = "refs/heads/live"
_BASE_REF = "refs/remotes/origin/main"
_LIVE_WORDS = {0: "live", 1: "other", 2: "unreadable"}


def _run_probe(argv: list[str], timeout: float) -> tuple[int, str] | None:
    """Run a short read-only probe in its own process group with git's location
    variables scrubbed; (rc, stdout), or None when it could not start or timed
    out (the whole group is killed). The snapshot sits on the dashboard and the
    Guardian health-probe path, so a probe is bounded like the checkout probe."""
    from genesis.session_awareness.zero_drop_git import scrubbed_git_env

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell interpolation
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=scrubbed_git_env(),
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("deploy_health: probe %s could not start: %s", argv[1:2], exc)
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_probe_group(proc)
        logger.warning("deploy_health: probe %s timed out after %ss", argv[1:2], timeout)
        return None
    except BaseException:
        _kill_probe_group(proc)
        raise
    return proc.returncode, out.decode("utf-8", "replace")


def collect_live(repo: Path, *, timeout: float = _CHEAP_TIMEOUT_S) -> dict:
    """Whether ``repo`` runs `live`, judged by the deploy scripts' own predicate
    (``scripts/lib/live_checkout.py``, run afresh with ``python -I -S``; its word
    and exit code must agree), and what that means for the other collectors.

    ``state``: ``live`` / ``other`` / ``unreadable`` (the predicate's words; a
    probe that cannot run reads ``unreadable`` with a ``reason``), or
    ``deploying`` (not probed: a deploy moves the checkout). On ``live``,
    ``base`` is merge-base(HEAD, origin/main), None when unreadable,
    ``candidate_tier2`` the tier-2 paths `live` adds over it, and ``unbuilt`` /
    ``unlisted`` how many candidates the manifest lists that `live` does not
    hold, and the reverse. ``unbound``: HEAD is the branch `live` but no
    manifest of this repository says what it should hold (``unlisted`` counts
    what it does). Off `live`, ``candidates`` is how many listed candidates are
    not running (None when the engine did not answer).

    Every manifest and `live` fact comes from ONE engine read,
    ``scripts/deploy_candidates list --json``; the manifest is never read here.
    The predicate and the engine are two reads, so a rebuild landing between
    them can mix two states for one snapshot; the next one is consistent.
    Never raises."""
    try:
        return _collect_live(repo, timeout)
    except Exception as exc:
        logger.warning("deploy_health: live collector failed", exc_info=True)
        return _unanswered(repo, f"collector failed: {type(exc).__name__}")


def _on_live_branch(repo: Path) -> bool:
    rc, ref, _ = _run_git(repo, "symbolic-ref", "-q", "HEAD", timeout=_CHEAP_TIMEOUT_S)
    return rc == 0 and ref.strip() == _LIVE_REF


def _live_base(repo: Path) -> str | None:
    """What of origin/main `live` holds: the merge base of HEAD and origin/main,
    used as the `upto` of the tier-2 and Guardian comparisons. Two candidates that
    `live` merged and origin/main later merged separately give TWO merge bases,
    and either alone omits the other's files (MEASURED, git 2.43: a guardian file
    from one candidate was missing from the default single base). Then the base
    is the tree of the two bases merged (`git diff` takes a tree), so neither is
    dropped. More than two, or bases that do not merge cleanly: None (unknown)."""
    rc, out, _ = _run_git(repo, "merge-base", "--all", "HEAD", _BASE_REF, timeout=_CHEAP_TIMEOUT_S)
    bases = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    if not bases or not all(_FULL_SHA.match(b) for b in bases):
        return None
    if len(bases) == 1:
        return bases[0]
    if len(bases) != 2:
        return None
    rc, tree, _ = _run_git(
        repo, "merge-tree", "--write-tree", "--no-messages", *bases, timeout=_CHEAP_TIMEOUT_S
    )
    first = tree.splitlines()[0].strip() if tree.strip() else ""
    return first if rc == 0 and _FULL_SHA.match(first) else None


def _unanswered(repo: Path, reason: str) -> dict:
    """The predicate gave no usable verdict. Only a checkout on the branch
    `live` is "unreadable" (a finding); any other checkout is "other" with the
    reason kept, so a slow or failed probe never raises a finding on an install
    that does not run `live`."""
    logger.warning("deploy_health: live predicate gave no verdict: %s", reason)
    if _on_live_branch(repo):
        # Still on `live`: the tier-2 and guardian comparisons must not fall back
        # to HEAD (where every candidate reads as drift), so say so.
        return {"state": "unreadable", "reason": reason, "on_live_branch": True}
    # Off `live`, an unknown candidate count: carried by the awareness check,
    # never read as 0 (which would resolve a standing live_off_branch alert).
    return {"state": "other", "reason": reason, "candidates": None}


def _collect_live(repo: Path, timeout: float) -> dict:
    import sys

    from genesis import env

    if env.update_in_progress():
        # Not probed while a deploy moves the checkout. Whether HEAD is on the
        # branch `live` still decides what the other collectors compare with.
        on_live = _on_live_branch(repo)
        facts: dict = {"state": "deploying", "on_live_branch": on_live}
        if on_live:
            facts["base"] = _live_base(repo)
        return facts
    script = repo / "scripts" / "lib" / "live_checkout.py"
    got = _run_probe([sys.executable, "-I", "-S", str(script), str(repo)], timeout)
    if got is None:
        return _unanswered(repo, "the live predicate did not answer")
    rc, out = got
    word = out.strip()
    if _LIVE_WORDS.get(rc) != word:
        return _unanswered(repo, f"the live predicate answered {word!r} (rc {rc})")
    engine = None if word == "unreadable" else _observe_engine(repo, timeout)
    if word == "other" and _on_live_branch(repo):
        # HEAD is the branch `live`, but the manifest is missing or names another
        # repository, so the predicate says `other`. Candidate code is checked
        # out: compare against the base, never HEAD, and say what `live` holds.
        facts: dict = {"state": "unbound", "on_live_branch": True, "base": _live_base(repo)}
        holds = (engine or {}).get("live", {}).get("holds")
        if engine is None or holds is None:
            return {
                "state": "unreadable",
                "on_live_branch": True,
                "base": facts["base"],
                "reason": _engine_reason(engine, "what `live` holds could not be read"),
            }
        facts["unlisted"] = len(holds)
        return facts
    facts = {"state": word}
    if word == "live":
        # On `live`, an unreadable base or tier-2 diff is "unreadable", never a
        # healthy `live`: without the base nothing can be compared, so a quiet
        # snapshot would claim what it could not establish.
        base = _live_base(repo)
        if not base:
            return {
                "state": "unreadable",
                "on_live_branch": True,
                "reason": "the base `live` was built on (merge-base with origin/main) "
                "could not be read",
            }
        facts["base"] = base
        rc, diff, _ = _run_git(
            repo,
            "diff",
            "--name-only",
            f"{base}..HEAD",
            "--",
            *TIER2_PATHS,
            timeout=_CHEAP_TIMEOUT_S,
        )
        if rc != 0:
            return {
                "state": "unreadable",
                "on_live_branch": True,
                "base": base,
                "reason": "the update.sh-only files `live` adds over its base could not be listed",
            }
        facts["candidate_tier2"] = len([ln for ln in diff.splitlines() if ln.strip()])
        state = (engine or {}).get("manifest", {}).get("state")
        holds = (engine or {}).get("live", {}).get("holds")
        if engine is None or state != "ok" or holds is None:
            # The predicate bound the manifest, the engine could not read it (or
            # what `live` holds): nothing here can say whether they agree.
            return {
                "state": "unreadable",
                "on_live_branch": True,
                "base": base,
                "reason": _engine_reason(
                    engine, "the engine could not compare `live` with the manifest"
                ),
            }
        held = {(h["branch"], h["head"]) for h in holds}
        held_names = {h["branch"] for h in holds}
        listed = engine["listed"]
        # Built: `live` holds exactly this (branch, head), or the checkout already
        # has the head and no rebuild merged the branch (a contained candidate).
        # A merged branch at another head is unbuilt even when HEAD contains the
        # listed one (re-pinned backwards: `live` runs commits nobody pinned).
        facts["unbuilt"] = sum(
            1
            for c in listed
            if (c["branch"], c["head"]) not in held
            and not (c["in_checkout"] and c["branch"] not in held_names)
        )
        names = {c["branch"] for c in listed}
        facts["unlisted"] = sum(1 for h in holds if h["branch"] not in names)
    elif word == "other":
        if engine is None:
            facts["candidates"] = None  # unknown: carried, never read as 0
            facts["reason"] = "the engine (deploy_candidates list --json) did not answer"
        elif engine["manifest"]["state"] == "error":
            # A manifest exists and is broken: the engine says so on every read.
            return {"state": "unreadable", "reason": engine["manifest"]["reason"]}
        else:
            # Off `live`: the listed candidates this checkout's HEAD does not
            # contain (none when the manifest is absent or another repository's).
            facts["candidates"] = sum(1 for c in engine["listed"] if not c["in_checkout"])
    return facts


def _observe_engine(repo: Path, timeout: float) -> dict | None:
    """The engine's ``list --json`` reading, or None when it did not answer or
    printed something this does not recognise (never a silent empty reading)."""
    got = _run_probe([str(repo / "scripts" / "deploy_candidates"), "list", "--json"], timeout)
    if got is None:
        return None
    rc, out = got
    if rc != 0:
        logger.warning("deploy_health: deploy_candidates list --json exited %s", rc)
        return None
    try:
        data = json.loads(out)
        ok = (
            data["version"] == 1
            and data["manifest"]["state"] in ("absent", "ok", "foreign", "error")
            and all(
                isinstance(c["branch"], str)
                and isinstance(c["head"], str)
                and isinstance(c["in_checkout"], bool)
                for c in data["listed"]
            )
            and (
                data["live"]["holds"] is None
                or all(isinstance(h["branch"], str) for h in data["live"]["holds"])
            )
        )
    except (ValueError, TypeError, KeyError):
        ok = False
    if not ok:
        logger.warning("deploy_health: unrecognised deploy_candidates list --json output")
        return None
    return data


def _engine_reason(engine: dict | None, default: str) -> str:
    if engine is None:
        return "the engine (deploy_candidates list --json) did not answer"
    return engine["manifest"].get("reason") or engine["live"].get("reason") or default


def live_unknown(live: dict | None) -> bool:
    """True when the reading says nothing either way about the live classes: off
    `live` with an unknown candidate count. The awareness check carries the last
    actionable live findings over the first such tick; on a second it raises
    live_unreadable only while a live alert stands."""
    live = live or {}
    return live.get("state") == "other" and "candidates" in live and live["candidates"] is None


def live_findings(live: dict | None) -> list[str]:
    """The finding keys a :func:`collect_live` dict contributes; the one
    producer of ``live_*`` keys (the awareness check uses it to carry a
    previous reading over a tick it cannot act on). None of them pages."""
    live = live or {}
    state = live.get("state")
    if state == "unreadable":
        return ["live_unreadable"]
    if state == "other" and (live.get("candidates") or 0) > 0:
        return [f"live_off_branch:{live['candidates']}"]
    found = []
    if state == "unbound":
        # HEAD is the branch `live` with no manifest of this repository: the
        # deploy scripts refuse it (restart included), whatever `live` holds.
        found.append("live_unbound")
    if state == "live" and live.get("unbuilt"):
        found.append(f"live_unbuilt:{live['unbuilt']}")
    if state in ("live", "unbound") and live.get("unlisted"):
        found.append(f"live_unlisted:{live['unlisted']}")
    if state == "live" and live.get("candidate_tier2"):
        found.append(f"live_candidate_tier2:{live['candidate_tier2']}")
    return found


#: A repair report with no result yet is a repair still running, or one killed
#: partway; after this long it is the latter.
REPAIR_UNFINISHED_S = 3600


_REPAIR_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "lib" / "live_move_repair.py"
_GUARD_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "lib" / "update_state_guard.py"
_REPAIR_SUFFIX = "-live-move-repair"
#: Bytes read at a time when a saved file is compared with the checkout's, or a
#: file hashed against the tag's blob: never a whole file in memory.
_CHUNK = 1 << 20


def _script(path: Path):
    """A script of the tree this module ships in, loaded afresh: the repair's
    own rules, so this reader judges what the writer judged."""
    spec = importlib.util.spec_from_file_location(f"_deploy_health_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_live_repairs(genesis_home_dir: Path, now: float | None = None) -> dict:
    """The live-move repair's REPAIR.json reports (scripts/lib/live_move_repair.py).

    ``saved``: each foreign row's re-apply step, OFFERED only while running it
    destroys nothing nobody saved: the checkout is still on the report's
    ``tag`` commit and each part the step writes (the node, the index entries,
    the flags) holds what the repair left there, nothing, or already what the
    step puts back, and every index entry it drops below its own path (the
    writer's ``below``, engine rows included) holds what the repair left there
    or nothing: a row there that cannot be judged (malformed) withholds it.
    ``saved_from``: the ts of each offered step's report, in the same order.
    It clears once the step would change nothing
    (:func:`_verdict`) or the report is dismissed (a newer report's row for a
    path replaces an older one's). A row that is neither (someone wrote the
    path since, or the checkout moved on) is never offered again: ``moved_on``
    holds one line per report naming how many, its ts and its backup dir, until
    it is dismissed. ``dismiss``: the ts of each report with an offered step.
    REPAIR.json is user-writable, so untrusted: its stored step text is never
    shown (each offered step is rebuilt by the repair's ``reapply_step``), and
    a row any field of which is not what the writer writes (:func:`_fields_ok`),
    or that no step can be built for, is skipped and counted in ``malformed``
    (``malformed_in``: the ts of each undismissed report holding one).
    ``unresolved``: the newest repair
    says so, or has had no result for REPAIR_UNFINISHED_S (killed partway),
    while the update state's guard still calls the checkout half-written: a
    repair killed after its restore leaves no result, and only the tree says
    whether it healed. The repair's module loads only for an undismissed
    report's foreign row (unloadable, no helper of it is called and every
    such row reads moved on), the guard only for a report that reads unresolved;
    each checkout's index and HEAD are read once per call."""
    now = time.time() if now is None else now
    offered: dict[str, tuple[str, str]] = {}  # path -> (step, ts)
    moved: dict[str, tuple[str, str]] = {}  # path -> (ts, backup dir)
    unresolved, repo, malformed = False, None, 0
    malformed_in: set[str] = set()
    repair, loaded = None, False
    checkouts: dict = {}
    for d in sorted((genesis_home_dir / "deploy-backups").glob("*" + _REPAIR_SUFFIX)):
        try:
            m = json.loads((d / "REPAIR.json").read_text(encoding="utf-8"))
            age = now - (d / "REPAIR.json").stat().st_mtime
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(m, dict):
            continue
        result, dismissed = m.get("result"), bool(m.get("dismissed_at"))
        unresolved = (result is None and age > REPAIR_UNFINISHED_S) or str(result).startswith(
            "unresolved"
        )
        unresolved = unresolved and not dismissed
        # Shapes as the guardian's collector checks them: a report of the wrong
        # shape is skipped row by row, never raised (that would blank the snapshot).
        paths = m.get("paths")
        paths = [r for r in paths if isinstance(r, dict)] if isinstance(paths, list) else []
        repo = m.get("repo") if isinstance(m.get("repo"), str) else None
        tag = m.get("tag") if isinstance(m.get("tag"), str) else None
        ts = d.name[: -len(_REPAIR_SUFFIX)]
        # Parents first, by path components (`a/b` before `a-b`), as the
        # writer orders its steps.
        foreign = sorted(
            (r for r in paths if r.get("class") == "foreign"),
            key=lambda r: str(r.get("path")).split("/"),
        )
        judged: list[tuple[str, dict, str]] = []
        for row in foreign:
            path = row.get("path")
            if dismissed:
                if isinstance(path, str):
                    offered.pop(path, None)
                    moved.pop(path, None)
                continue
            if not _fields_ok(d, row):
                malformed += 1
                malformed_in.add(ts)
                continue
            if not loaded:
                loaded = True
                try:
                    repair = _script(_REPAIR_SCRIPT)
                except Exception:  # noqa: BLE001 - unloadable: nothing can be judged
                    repair = None
            judged.append((path, row, _verdict(repair, d, repo, tag, row, checkouts)))
        # A row's step drops the index entries of the rows below it before it
        # re-adds its own: offered only while none of those moved on, and each
        # entry it drops still holds what the repair left there, or nothing.
        # With the repair's module unloadable every verdict is "moved" already.
        gone = [p + "/" for p, _, v in judged if v == "moved"]
        below = _below(repair, paths) if judged and repair is not None else {}
        for path, row, verdict in judged:
            offered.pop(path, None)
            moved.pop(path, None)
            if verdict == "offer" and any(g.startswith(path + "/") for g in gone):
                verdict = "moved"
            if verdict == "offer" and not all(
                _left_alone(repair, d, paths, p, _checkout(repair, repo, checkouts))
                for p in _drops(repair, row, below.get(path, ()))
            ):
                verdict = "moved"
            if verdict == "offer":
                # Never the stored text: the repair's own reapply_step, from the
                # checked row, the report's own dir and the writer's `below`.
                try:
                    offered[path] = (
                        repair.reapply_step(repo, str(d), row, below.get(path, ())),
                        ts,
                    )
                except Exception:  # noqa: BLE001 - a row no step can be built for is malformed
                    malformed += 1
                    malformed_in.add(ts)
            elif verdict == "moved":
                moved[path] = (ts, str(d))
    counts: dict[tuple[str, str], int] = {}
    for key in moved.values():
        counts[key] = counts.get(key, 0) + 1
    return {
        "saved": [step for step, _ in offered.values()],
        "saved_from": [ts for _, ts in offered.values()],
        "dismiss": sorted({ts for _, ts in offered.values()}),
        "moved_on": [
            f"{n} saved path(s) from repair {ts} are no longer re-applicable here (the "
            f"checkout moved on); their bytes are in {backup}"
            for (ts, backup), n in sorted(counts.items())
        ],
        "moved_on_paths": len(moved),
        "malformed": malformed,
        "malformed_in": sorted(malformed_in),
        "unresolved": unresolved and _mixed(genesis_home_dir, repo),
    }


def _mixed(genesis_home_dir: Path, repo: str | None) -> bool:
    """Does the shared restart guard call the checkout half-written? Doubt
    (no checkout named, a guard that cannot run) keeps the report's word."""
    try:
        guard = _script(_GUARD_SCRIPT)
        verdict, _ = guard.verdict(str(genesis_home_dir / "update_state.json"), repo)
    except Exception:  # noqa: BLE001 - any failure to decide keeps "unresolved"
        return True
    return verdict == guard.MIXED_TREE


def _relative(path: object) -> bool:
    """A plain relative path: no empty, `.` or `..` part, no NUL. Anything else
    in an untrusted report could leave the checkout (or loop the writer's
    _parents(), which never ends for an absolute path)."""
    return (
        isinstance(path, str)
        and "\0" not in path
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _inside(backup: Path, saved: object) -> bool:
    """No saved copy, or one that resolves inside the report's own backup dir
    (its parent resolved: the copy itself may be a symlink, never followed)."""
    if saved is None:
        return True
    if not _relative(saved):
        return False
    root = os.path.realpath(backup)
    parent = os.path.realpath(os.path.join(root, os.path.dirname(str(saved))))
    return parent == root or parent.startswith(root + os.sep)


_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_MODES = frozenset({"100644", "100755", "120000", "160000"})


def _entry(e: object, stage: bool) -> bool:
    """A (mode, oid) tree entry or, with ``stage``, a (mode, oid, stage) index entry."""
    return (
        isinstance(e, list)
        and len(e) == (3 if stage else 2)
        and all(isinstance(x, str) for x in e)
        and e[0] in _GIT_MODES
        and _OID.fullmatch(e[1]) is not None
        and (not stage or e[2] in ("0", "1", "2", "3"))
    )


def _fields_ok(backup: Path, row: dict) -> bool:
    """Every field reapply_step builds a step from is what the writer writes:
    paths relative (copies inside the backup), entries git's, a FIFO's mode
    octal, a flag one `ls-files -v` letter. Anything else is not shown."""
    special, flag, blobs = row.get("special"), row.get("flag"), row.get("blobs") or []
    through = row.get("unreachable_through")
    return (
        _relative(row.get("path"))
        and _inside(backup, row.get("saved_as"))
        and isinstance(row.get("disk_changed"), bool)
        and (row.get("tag") is None or _entry(row.get("tag"), False))
        and isinstance(row.get("index"), list)
        and all(_entry(e, True) for e in row["index"])
        and isinstance(blobs, list)
        and all(_relative(b) and _inside(backup, b) for b in blobs)
        and (through is None or _relative(through))
        and (
            flag is None
            or (isinstance(flag, str) and len(flag) == 1 and flag.isascii() and flag.isalpha())
        )
        and (
            special is None
            or (
                isinstance(special, dict)
                and isinstance(special.get("type"), str)
                and re.fullmatch(r"[0-7]{4}", str(special.get("mode"))) is not None
            )
        )
    )


def _below(repair, paths: list[dict]) -> dict[str, list[str]]:
    """The writer's `below`: per directory, the row paths under it whose own
    step re-adds what they held, or that held nothing (never an engine row's
    entry), in the report's row order."""
    below: dict[str, list[str]] = {}
    for r in paths:
        if not _relative(r.get("path")) or (r.get("class") != "foreign" and r.get("index")):
            continue
        for d in repair._parents(r["path"]):
            below.setdefault(d, []).append(r["path"])
    return below


def _drops(repair, row: dict, below) -> list[str]:
    """The paths the row's step drops from the index below its own, as
    ``reapply_step`` builds it: ``below`` when the step writes a stage-0
    entry, else none (its own path is _verdict's)."""
    if [tuple(e) for e in row["index"]] == repair._as_index(row.get("tag")):
        return []
    return list(below) if any(e[2] == "0" for e in row["index"]) else []


def _left_alone(repair, backup: Path, paths: list[dict], path: str, state: dict | None) -> bool:
    """Does every row of ``path`` hold, in the index, what the repair left
    there, or nothing? An engine row's leftover is the tag's entry (the repair
    put it back); a foreign row's too (the restore wrote the tag's). A row that
    cannot be judged (malformed, another class) never reads as left alone."""
    if state is None:
        return False
    have = [tuple(e) for e in state["entries"].get(path, [])]
    for r in paths:
        if r.get("path") != path:
            continue
        if r.get("class") == "foreign":
            ok = _fields_ok(backup, r)
        else:
            ok = r.get("class") == "engine" and (
                r.get("tag") is None or _entry(r.get("tag"), False)
            )
        if not ok or have not in ([], repair._as_index(r.get("tag"))):
            return False
    return True


def _checkout(repair, repo: str | None, checkouts: dict) -> dict | None:
    """The checkout's index entries, flags and HEAD, read once per collect
    (None, also once, for a checkout that is gone or cannot be read)."""
    if repo not in checkouts:
        checkouts[repo] = None
        if repair is not None and repo and os.path.isabs(repo) and os.path.isdir(repo):
            with contextlib.suppress(Exception):
                entries, flags = repair._index_state(repo)
                head = repair._out(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}")
                checkouts[repo] = {"entries": entries, "flags": flags, "head": head}
    return checkouts[repo]


def _verdict(repair, backup: Path, repo: str | None, tag: str | None, row: dict, checkouts) -> str:
    """ "reapplied" when running the row's printed step (the repair's
    ``reapply_step``) would change nothing; "offer" while the checkout is on
    the report's tag commit and every part the step writes holds what the
    repair left there (the tag's node, nothing, or a tag directory as the tag
    has it; the tag's index entries; no flag) or already what the step puts
    back, so the step destroys nothing nobody saved; else "moved" (someone
    wrote the path since, the checkout moved on, or it cannot be judged).

    Part by part as the step is built: its index entries, when the step writes
    them (re-adding them also resets the flags, so the flags must then be
    exactly the row's); its flags (else the step only sets them); and its node,
    when the step puts one back, never read through a symlinked parent. A row
    the repair saw beyond a symlinked or non-directory parent gets its index
    part only. A socket or device cannot be made again by the step: it reads
    re-applied once a node of its type is back."""
    try:
        state = _checkout(repair, repo, checkouts)
        if state is None:
            return "moved"
        path = row["path"]
        want = [tuple(e) for e in row["index"]]
        left = repair._as_index(row.get("tag"))
        writes_index = want != left
        have = [tuple(e) for e in state["entries"].get(path, [])]
        flag, cur = row.get("flag") or "", state["flags"].get(path) or ""
        need = (flag in ("S", "s"), flag.islower())  # skip-worktree, assume-unchanged
        got = (cur in ("S", "s"), cur.islower())
        flags_back = got == need or (
            not writes_index and all(g or not n for n, g in zip(need, got, strict=True))
        )
        index_back = not writes_index or have == want
        disk = bool(row.get("disk_changed")) and not row.get("unreachable_through")
        here = _here(repair, repo, path) if disk else None
        node_back = not disk or _node_back(repair, backup, repo, path, here, row)
        if index_back and flags_back and node_back:
            return "reapplied"
        if state["head"] != tag:
            return "moved"
        as_left = (
            (index_back or have == left)
            and (flags_back or not cur)
            and (node_back or _as_left(repair, repo, path, here, row, state))
        )
        return "offer" if as_left else "moved"
    except Exception:  # noqa: BLE001 - a row that cannot be judged is never offered
        return "moved"


def _here(repair, repo: str, path: str):
    """The checkout node's lstat; None when absent (beyond a non-directory
    parent too, as git calls it); False beyond a symlinked parent (never read
    through one: it can point outside the checkout)."""
    parent = repair._link_parent(repo, path)
    if parent is not None:
        return False if os.path.islink(os.path.join(repo, parent)) else None
    try:
        return os.lstat(os.path.join(repo, path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _holds_nothing(mode: int) -> bool:
    """A socket or device: no bytes the repair could save, none a step loses."""
    return stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def _node_back(repair, backup: Path, repo: str, path: str, here, row: dict) -> bool:
    """Is the node the step puts back already there?"""
    saved, special = row.get("saved_as"), row.get("special")
    if here is False:
        return False
    if isinstance(saved, str) and saved:
        return here is not None and _same_node(str(backup / saved), os.path.join(repo, path))
    if not isinstance(special, dict):  # nothing to put back: the step only removes
        return here is None
    if here is None:
        return False
    kind = next((name for test, name in repair._KINDS if test(here.st_mode)), None)
    if special.get("type") != "fifo":
        return kind == special.get("type")
    return kind == "fifo" and f"{stat.S_IMODE(here.st_mode):04o}" == special.get("mode")


def _as_left(repair, repo: str, path: str, here, row: dict, state: dict) -> bool:
    """Does the path hold what the repair left there (or nothing at all)? The
    tag's blob by git mode and id; a gitlink's empty directory; a directory
    the tag has, holding exactly the tag's files."""
    if here is None:
        return True
    if here is False:
        return False
    full, entry = os.path.join(repo, path), row.get("tag")
    if entry is None:
        return stat.S_ISDIR(here.st_mode) and _tag_dir(repair, repo, path, state)
    mode, oid = entry
    if mode == "160000":
        return stat.S_ISDIR(here.st_mode) and not os.listdir(full)
    return _blob_is(full, here, mode, oid)


def _tag_dir(repair, repo: str, path: str, state: dict) -> bool:
    """The directory holds exactly the tag's entries under it (the tag tree is
    read once per checkout, and only for such a row; gitlinks are empty)."""
    if "tree" not in state:
        state["tree"] = repair._tree(repo, state["head"])
    want = {p[len(path) + 1 :]: e for p, e in state["tree"].items() if p.startswith(path + "/")}
    seen: set[str] = set()
    full = os.path.join(repo, path)
    for top, dirs, files in os.walk(full):
        for name in files + dirs:
            node = os.path.join(top, name)
            rel, st = os.path.relpath(node, full), os.lstat(node)
            if stat.S_ISDIR(st.st_mode):
                if rel in want:  # a gitlink: an empty directory
                    dirs.remove(name)
                    if want[rel][0] != "160000" or os.listdir(node):
                        return False
                    seen.add(rel)
                continue
            if _holds_nothing(st.st_mode):
                continue
            if rel not in want or not _blob_is(node, st, *want[rel]):
                return False
            seen.add(rel)
    return bool(want) and seen == set(want)


def _open_regular(path: str) -> int:
    """An fd on a regular file, never following a symlink nor blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    return fd


def _blob_is(full: str, st: os.stat_result, mode: str, oid: str) -> bool:
    """Is the node the blob ``oid`` at git ``mode`` (the executable bit is the
    one permission git tracks)? Hashed in _CHUNK reads, never read whole."""
    h = hashlib.new("sha256" if len(oid) == 64 else "sha1")
    if mode == "120000":
        if not stat.S_ISLNK(st.st_mode):
            return False
        data = os.fsencode(os.readlink(full))
        h.update(b"blob %d\0" % len(data) + data)
    elif mode in ("100644", "100755"):
        if not stat.S_ISREG(st.st_mode) or bool(st.st_mode & stat.S_IXUSR) != (mode == "100755"):
            return False
        fd = _open_regular(full)
        try:
            size, read = os.fstat(fd).st_size, 0
            h.update(b"blob %d\0" % size)
            while chunk := os.read(fd, _CHUNK):
                h.update(chunk)
                read += len(chunk)
        finally:
            os.close(fd)
        if read != size:
            return False
    else:
        return False
    return h.hexdigest() == oid


def _same_bytes(a: str, b: str) -> bool:
    fa = _open_regular(a)
    try:  # the first descriptor is closed even when the second open fails
        fb = _open_regular(b)
        try:
            while True:
                x, y = os.read(fa, _CHUNK), os.read(fb, _CHUNK)
                if x != y:
                    return False
                if not x:
                    return True
        finally:
            os.close(fb)
    finally:
        os.close(fa)


def _same_node(saved: str, here: str) -> bool:
    """The saved node against the checkout's, as `cp -a` would put it back,
    never following a symlink: a file by its executable bit (the one
    permission git tracks), its size, then its bytes in _CHUNK reads; a symlink
    by its target; a FIFO by its mode; a directory entry by entry. A socket or
    device is ignored: the repair cannot save one."""
    a, b = os.lstat(saved), os.lstat(here)
    if stat.S_IFMT(a.st_mode) != stat.S_IFMT(b.st_mode):
        return False
    if stat.S_ISLNK(a.st_mode):
        return os.readlink(saved) == os.readlink(here)
    if stat.S_ISREG(a.st_mode):
        return (
            a.st_mode & stat.S_IXUSR == b.st_mode & stat.S_IXUSR
            and a.st_size == b.st_size
            and _same_bytes(saved, here)
        )
    if stat.S_ISFIFO(a.st_mode):
        return stat.S_IMODE(a.st_mode) == stat.S_IMODE(b.st_mode)
    if not stat.S_ISDIR(a.st_mode):
        return False
    names, theirs = (
        sorted(n for n in os.listdir(d) if not _holds_nothing(os.lstat(os.path.join(d, n)).st_mode))
        for d in (saved, here)
    )
    return names == theirs and all(
        _same_node(os.path.join(saved, n), os.path.join(here, n)) for n in names
    )


def live_repair_findings(repairs: dict | None) -> list[str]:
    """The ``live_repair_*`` keys of a :func:`collect_live_repairs` dict."""
    repairs = repairs or {}
    found = [f"live_repair_saved:{len(repairs['saved'])}"] if repairs.get("saved") else []
    if repairs.get("moved_on"):
        found.append(f"live_repair_moved_on:{repairs.get('moved_on_paths') or 0}")
    if repairs.get("unresolved"):
        found.append("live_repair_unresolved")
    if repairs.get("malformed"):
        found.append(f"live_repair_malformed:{repairs['malformed']}")
    return found


# Sustained-staleness thresholds (the awareness check's paging axis). A
# finding-CLASS boundary, so they live here beside derive_findings — the
# single producer of finding keys — not in the awareness layer: an alert
# formula written against facts the findings gate can filter out first is
# exactly the bug this placement prevents.
STALE_UPDATE_DAYS = 7.0
STALE_UPDATE_COMMITS = 20


def derive_findings(
    *,
    missing_units: list[str] | None,
    tier2_pending: list[str] | None,
    host_gateway: dict,
    commits_behind: int | None,
    update_age_days: float | None = None,
    behind_threshold: int = 50,
    main_checkout: dict | None = None,
    live: dict | None = None,
    live_repairs: dict | None = None,
) -> list[str]:
    """Stable, order-deterministic finding keys — the alert/dedup contract.

    Keys (not prose) so the awareness check can hash them for observation
    dedup and tests can assert exactly. Two behind-related classes with
    DIFFERENT thresholds: ``stale_update`` (≥STALE_UPDATE_DAYS old AND
    ≥STALE_UPDATE_COMMITS behind — the sustained condition the awareness
    check pages on) and ``behind_upstream`` (> behind_threshold regardless
    of update age — a plain volume signal).

    ``main_checkout`` (``collect_main_checkout_dirty``'s dict) adds
    ``main_checkout_dirty:<n>`` for tracked edits in the deploy checkout and
    ``main_checkout_unreadable`` when that could not be read: never nothing, so
    an unreadable tree cannot clear a standing dirty finding. ``clean``,
    ``not_deploy_root`` and ``deploying`` add nothing. ``live``
    (:func:`collect_live`) adds the ``live_*`` keys of :func:`live_findings`, and
    ``live_repairs`` (:func:`collect_live_repairs`) the ``live_repair_*`` keys."""
    findings: list[str] = []
    if missing_units:
        findings.append("missing_units:" + ",".join(sorted(missing_units)))
    if tier2_pending:
        findings.append(f"tier2_pending:{len(tier2_pending)}")
    if host_gateway.get("status") == "drift":
        findings.append("host_guardian_drift")
    elif host_gateway.get("status") == "unknown_commit":
        findings.append("host_guardian_unknown_commit")
    if (
        update_age_days is not None
        and commits_behind is not None
        and update_age_days >= STALE_UPDATE_DAYS
        and commits_behind >= STALE_UPDATE_COMMITS
    ):
        findings.append(f"stale_update:{round(update_age_days, 1)}d,{commits_behind}behind")
    if commits_behind is not None and commits_behind > behind_threshold:
        findings.append(f"behind_upstream:{commits_behind}")
    findings.extend(main_checkout_findings(main_checkout))
    findings.extend(live_findings(live))
    findings.extend(live_repair_findings(live_repairs))
    return findings


def main_checkout_findings(main_checkout: dict | None) -> list[str]:
    """The finding keys a ``collect_main_checkout_dirty`` dict contributes. The
    one producer of ``main_checkout_*`` keys: :func:`derive_findings` uses it,
    and so does the awareness check when it carries a previous tick's checkout
    reading forward over a tick whose own reading it cannot act on."""
    status = (main_checkout or {}).get("status")
    if status == "dirty":
        count = main_checkout.get("count")
        # None: a dirty state carried from a standing alert, count unknown.
        return [f"main_checkout_dirty:{'?' if count is None else count}"]
    if status == "unknown":
        return ["main_checkout_unreadable"]
    return []


# ── Snapshot entry point (HealthDataService) ────────────────────────


def _collect_sync(repo: Path, genesis_home_dir: Path) -> dict:
    """All filesystem/git collectors in one worker-thread hop."""
    main_checkout = collect_main_checkout_dirty(repo)
    # A linked worktree (a dev tree, e.g. a session's MCP) is not the checkout
    # `live` runs in: the engine would answer for the deploy checkout.
    live = None if main_checkout.get("status") == "not_deploy_root" else collect_live(repo)
    live_ = live or {}
    on_live = live_.get("state") == "live" or bool(live_.get("on_live_branch"))
    # On `live`, the base it was built on (None, i.e. unknown, mid-deploy or
    # when unreadable); never HEAD, where every candidate reads as drift.
    upto = live_.get("base") if on_live else "HEAD"
    git_facts = collect_git_facts(repo, on_live=on_live)
    git_facts["live"] = (live or {}).get("state")
    missing_units = collect_missing_units(
        repo / "scripts" / "systemd",
        Path.home() / ".config" / "systemd" / "user",
    )
    host_gateway = collect_host_gateway(repo, genesis_home_dir / _HOST_STATE_FILE, upto=upto)
    return {
        "git": git_facts,
        "missing_units": missing_units,
        "host_gateway": host_gateway,
        "main_checkout": main_checkout,
        "live": live,
        "upto": upto,
        "live_repairs": collect_live_repairs(genesis_home_dir),
    }


async def deploy_health(db: aiosqlite.Connection | None) -> dict:
    """Snapshot section for HealthDataService — see module docstring."""
    try:
        from genesis.env import genesis_home, repo_root

        repo = repo_root()
        collected = await asyncio.to_thread(_collect_sync, repo, genesis_home())
        update = await last_success_update(db)
        tier2 = await asyncio.to_thread(
            collect_tier2_pending, repo, update.get("new_commit"), collected["upto"]
        )
        findings = derive_findings(
            missing_units=collected["missing_units"],
            tier2_pending=tier2,
            host_gateway=collected["host_gateway"],
            commits_behind=collected["git"].get("commits_behind_upstream"),
            update_age_days=update.get("age_days"),
            main_checkout=collected["main_checkout"],
            live=collected["live"],
            live_repairs=collected["live_repairs"],
        )
        return {
            "status": "attention" if findings else "healthy",
            "findings": findings,
            "last_update": update,
            "git": collected["git"],
            "missing_units": collected["missing_units"],
            "tier2_pending": tier2,
            "host_gateway": collected["host_gateway"],
            "main_checkout": collected["main_checkout"],
            "live": collected["live"],
            "live_repairs": collected["live_repairs"],
        }
    except Exception:
        logger.error("deploy_health snapshot failed", exc_info=True)
        return {"status": "error"}
