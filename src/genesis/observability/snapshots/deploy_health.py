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

The awareness tick's ``_check_deploy_staleness`` consumes the same collectors
to raise a dashboard/morning-report observation. Everything is best-effort:
collectors degrade to ``None``/empty and never raise into the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
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
    rc, base, _ = _run_git(repo, "merge-base", "HEAD", _BASE_REF, timeout=_CHEAP_TIMEOUT_S)
    return base.strip() if rc == 0 and _FULL_SHA.match(base.strip()) else None


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
        listed = engine["listed"]
        facts["unbuilt"] = sum(
            1 for c in listed if not c["in_base"] and (c["branch"], c["head"]) not in held
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
            # Absent or another repository's: nothing is meant to be live here.
            # Already in origin/main: running on any branch, so not counted.
            facts["candidates"] = sum(1 for c in engine["listed"] if not c["in_base"])
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
                and isinstance(c["in_base"], bool)
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
    if state == "live" and live.get("unbuilt"):
        found.append(f"live_unbuilt:{live['unbuilt']}")
    if state in ("live", "unbound") and live.get("unlisted"):
        found.append(f"live_unlisted:{live['unlisted']}")
    if state == "live" and live.get("candidate_tier2"):
        found.append(f"live_candidate_tier2:{live['candidate_tier2']}")
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
    (:func:`collect_live`) adds the ``live_*`` keys of :func:`live_findings`."""
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
        }
    except Exception:
        logger.error("deploy_health snapshot failed", exc_info=True)
        return {"status": "error"}
