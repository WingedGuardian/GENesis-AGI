"""CCInvoker — async subprocess wrapper for claude -p CLI."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from genesis.cc import roster
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCNetworkOfflineError,
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCSessionError,
    CCStreamTruncatedError,
    CCTimeoutError,
)
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    CCOutput,
    EffortLevel,
    StreamEvent,
    clamp_effort,
    model_supports_effort,
)
from genesis.observability.spans import SpanKind, start_span
from genesis.util.proc_kill import (
    kill_process_group,
    process_group_alive,
    reap_bounded,
)

logger = logging.getLogger(__name__)


# From CC 2.1.277 a headless resume RESTORES the CC session's saved totals — its
# changelog: a resume no longer starts "the session's cost and usage totals at
# zero; headless sessions now save their totals at exit". So a resumed `-p` call
# reports `total_cost_usd` (and `modelUsage`) as running totals for the whole CC
# session, while `usage` tokens stay per call. MEASURED on 2.1.280 across a
# resumed haiku -> haiku -> sonnet session: totals 0.0383 -> 0.0421 -> 0.1475,
# the earlier model's entry carried over, the session id unchanged. Nothing in the
# result itself marks the change: `num_turns` is 1 every call, there is no version
# field, and the switched-to model's own entry starts at this call's tokens — so
# the VERSION, which is what defines the behaviour, is the signal.
_CC_CUMULATIVE_COST_SINCE = (2, 1, 277)
_CC_VERSION_RE = re.compile(r"\s*(\d+)\.(\d+)\.(\d+)")
# After a failed `claude --version` read, how long before the same binary is
# asked again. The read sits on the turn path (CCInvoker._cc_version), so a CLI
# that HANGS on --version would otherwise hold every reply for the full 15 s
# timeout; with this, at most one reply per 10 minutes pays it. A transient
# failure recovers within the same window, and a replaced binary is a new key
# and is read at once.
_CC_VERSION_RETRY_S = 600.0


def parse_cc_version(text: str) -> tuple[int, int, int] | None:
    """Parse the leading ``X.Y.Z`` of ``claude --version`` output, else None."""
    m = _CC_VERSION_RE.match(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def cost_is_cumulative_for(version: tuple[int, int, int] | None) -> bool:
    """Whether a CC of this version reports a resumed session's running totals.

    True on a FRESH session too, which is harmless: its first running total is
    its own cost, and ``cc_sessions.record_turn_cost`` keys its cursor by CC
    session, so a new session is never diffed against an old one. An unknown
    version reads as False — the additive reading CC used before 2.1.277.
    """
    return version is not None and version >= _CC_CUMULATIVE_COST_SINCE


def set_oom_score_adj(pid: int, score: int = 500) -> None:
    """Set OOM score adjustment for a process.

    Higher scores make the process more likely to be OOM-killed. CC subprocesses
    get +500 so the kernel kills them before genesis-server or qdrant. This is
    the container-side complement to the host VM's cgroup OOM scoring.

    This docstring used to say genesis-server sits at ``-500``. It never did.
    MEASURED 2026-09-08: the unit declared ``-500`` while the live process ran at
    ``100``, because a user manager cannot lower oom_score_adj below the inherited
    ``oom_score_adj_min`` of 0 without CAP_SYS_RESOURCE — the write fails silently.
    The unit now declares an achievable ``100``. Only the direction this function
    uses is actually available to us: RAISING needs no privilege, LOWERING is
    always refused, so every rung of the kill order has to be built by pushing
    sacrificial processes UP rather than protecting important ones DOWN.
    """
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text(str(score))
        logger.debug("Set oom_score_adj=%d for PID %d", score, pid)
    except OSError as exc:
        logger.warning("Could not set oom_score_adj for PID %d: %s", pid, exc)


# The unit properties the scope actually carries. ONE tuple, consumed by BOTH
# the probe and the real invocation, because a probe that omits them answers a
# weaker question than the one that matters. systemd-run exits non-zero on a
# property it cannot accept — MEASURED on systemd 255: an unknown assignment
# ("Unknown assignment: BogusProperty=1") and an unparseable value ("Failed to
# parse MemoryMax=...") both exit 1 — and older systemd predates the ``N%``
# syntax these use. A property-free probe therefore SUCCEEDS on such a box, its
# verdict is cached for the process lifetime, and every real dispatch then dies
# inside systemd-run before Claude starts: the exact fail-closed-to-dead outcome
# the probe was added to prevent, converted from "no isolation" to "no CC".
# The sibling probe-then-commit sites (.claude/mcp/run-codebase-memory,
# scripts/lib/code_intel_index.sh) already set their own properties at probe
# time for this reason; this was the one that did not.
_SCOPE_PROPERTIES = ("IOWeight=100", "MemoryHigh=62%", "MemoryMax=75%")


def _scope_argv(*trailing: str) -> list[str]:
    """`systemd-run` argv carrying `_SCOPE_PROPERTIES`, then `trailing` after `--`.

    With no `trailing` this is the prefix a caller prepends to its own command;
    with `/bin/true` it is the probe. Sharing the builder is what keeps the two
    from drifting apart again.
    """
    argv = ["systemd-run", "--user", "--scope", "--quiet"]
    for prop in _SCOPE_PROPERTIES:
        argv += ["-p", prop]
    argv.append("--")
    argv.extend(trailing)
    return argv


def _build_scope_args(announce: bool = True) -> list[str]:
    """Build systemd-run prefix for CC subprocess I/O isolation.

    Wraps the CC subprocess in a transient systemd scope with resource
    limits.  Each session gets its own cgroup under app.slice/run-XXXX.scope,
    separate from genesis-server.service.

    Memory limits are PERCENTAGES so they scale with the host: systemd
    resolves ``N%`` against the container's memory cgroup (verified: on a
    16 GiB container ``62%``→9.9 GiB soft, ``75%``→12 GiB hard). This keeps
    a heavy CC build from OOM-ing the box while auto-scaling up on larger
    installs and down on the 8 GiB floor — no hardcoded ceiling to re-tune.

    Returns an empty list if systemd-run is unavailable (graceful degradation)
    OR has no reachable user manager. The probe matters: ``which`` alone is not
    enough — an env-scrubbed spawner (some agent CLIs' shell tooling, CI
    runners, a stripped systemd context) has the binary but no
    DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR, and ``systemd-run --user`` then
    dies instantly with "Failed to connect to bus", taking the CC subprocess
    down with it at 0.0s. Measured on a live install.
    Same probe-then-commit pattern as .claude/mcp/run-codebase-memory, and the
    probe sets the SAME properties as the real invocation (`_SCOPE_PROPERTIES`)
    so a property the manager rejects fails at probe time rather than on every
    dispatch afterwards.

    Caching is asymmetric and lives in `_get_scope_args`: a SUCCESS is cached
    for the process lifetime, a FAILURE only until a backoff elapses. Set
    ``announce`` False on a retry so a permanently-unscoped box logs the
    warning once rather than on every re-probe forever.
    """
    if not shutil.which("systemd-run"):
        return []
    try:
        probe = subprocess.run(
            _scope_argv("/bin/true"),
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Same announced degradation as the non-zero-exit branch below. Without
        # it a timeout or a systemd-run that vanished between `shutil.which` and
        # exec drops MemoryHigh/MemoryMax silently, and the operator has no way
        # to tell an unscoped box from a scoped one.
        _log = logger.warning if announce else logger.debug
        _log(
            "systemd-run --user probe raised (%s: %s) — CC subprocesses will run "
            "WITHOUT the cgroup scope (no MemoryHigh/MemoryMax isolation)",
            type(exc).__name__,
            exc,
        )
        return []
    if probe.returncode != 0:
        _log = logger.warning if announce else logger.debug
        _log(
            "systemd-run --user probe failed (no user manager?) — CC subprocesses "
            "will run WITHOUT the cgroup scope (no MemoryHigh/MemoryMax isolation): %s",
            probe.stderr.decode(errors="replace").strip()[:200],
        )
        return []
    return _scope_argv()


# Cache the scope args. SUCCESS is cached for the process lifetime — a working
# user manager does not stop working, and the probe costs a subprocess.
#
# FAILURE is not, and the difference is the whole point. The old comment here
# ("they don't change during a process's lifetime") was written when this
# guarded a deterministic `shutil.which` result. It now guards a PROBE, which
# can fail transiently: a timeout under load, or `systemd --user` restarting
# during an update. genesis-server is long-lived, so caching one such failure
# forever silently drops MemoryHigh=62%/MemoryMax=75% from EVERY subsequent CC
# subprocess, for days — on a swapless box, which is the exact scenario the
# scope exists to prevent (see _build_scope_args). The degradation is announced
# once in a log line and then never re-evaluated, so the fail-open outlives its
# cause.
#
# The retry schedule ESCALATES rather than repeating at a fixed interval,
# because the two documented failure causes have opposite lifetimes. A user
# manager bouncing during a deploy resolves in seconds to minutes — worth
# re-probing soon. An env-scrubbed spawner with no reachable bus
# (_build_scope_args's docstring) is a PERMANENT property of that box, and a
# fixed 300s retry would probe it forever and log a warning 288x/day, turning
# a once-per-process announcement into noise. Escalating recovers fast from the
# transient case and decays to hourly on the permanent one.
_SCOPE_RETRY_SCHEDULE_S = (300.0, 900.0, 3600.0)

_SCOPE_ARGS: list[str] | None = None
_SCOPE_PROBE_FAILED_AT: float | None = None
_SCOPE_PROBE_FAILURES = 0
_SCOPE_PROBE_LOCK: asyncio.Lock | None = None


def _now() -> float:
    """Monotonic clock seam.

    Tests patch THIS, not `time.monotonic`. `invoker.time` is the stdlib module
    object, so monkeypatching its attribute replaces the clock for the whole
    interpreter for the duration of the test — including anything else pytest
    or a plugin is timing.
    """
    return time.monotonic()


def reset_scope_cache() -> None:
    """Test hook: clear the cached probe verdict and its backoff state."""
    global _SCOPE_ARGS, _SCOPE_PROBE_FAILED_AT, _SCOPE_PROBE_FAILURES  # noqa: PLW0603
    _SCOPE_ARGS = None
    _SCOPE_PROBE_FAILED_AT = None
    _SCOPE_PROBE_FAILURES = 0


def _scope_probe_lock() -> asyncio.Lock:
    """Single-flight guard: concurrent dispatches share one probe, not N."""
    global _SCOPE_PROBE_LOCK  # noqa: PLW0603
    if _SCOPE_PROBE_LOCK is None:
        _SCOPE_PROBE_LOCK = asyncio.Lock()
    return _SCOPE_PROBE_LOCK


def _scope_cooldown_active() -> bool:
    if _SCOPE_PROBE_FAILED_AT is None:
        return False
    idx = min(max(_SCOPE_PROBE_FAILURES - 1, 0), len(_SCOPE_RETRY_SCHEDULE_S) - 1)
    return _now() - _SCOPE_PROBE_FAILED_AT < _SCOPE_RETRY_SCHEDULE_S[idx]


async def _get_scope_args() -> list[str]:
    """Cached scope args. Success is permanent; failure retries on a backoff.

    ASYNC because the probe is a blocking `subprocess.run` with a 15s ceiling
    and both callers sit on the event loop. Retrying a failed probe on a timer
    — which is the whole point of the backoff — would otherwise reintroduce
    that stall periodically instead of once per process, on exactly the path
    (a probe that already failed, possibly by timing out) where it is most
    likely to be slow.
    """
    global _SCOPE_ARGS, _SCOPE_PROBE_FAILED_AT, _SCOPE_PROBE_FAILURES  # noqa: PLW0603
    if _SCOPE_ARGS:
        return _SCOPE_ARGS
    if _scope_cooldown_active():
        return []
    async with _scope_probe_lock():
        # Re-check under the lock: a concurrent dispatch may have probed while
        # this one waited, and its verdict is the one to honour.
        if _SCOPE_ARGS:
            return _SCOPE_ARGS
        if _scope_cooldown_active():
            return []
        first_failure = _SCOPE_PROBE_FAILURES == 0
        args = await asyncio.to_thread(_build_scope_args, first_failure)
        if args:
            _SCOPE_ARGS = args
            _SCOPE_PROBE_FAILED_AT = None
            _SCOPE_PROBE_FAILURES = 0
        else:
            _SCOPE_PROBE_FAILED_AT = _now()
            _SCOPE_PROBE_FAILURES += 1
        return _SCOPE_ARGS or []


# A minimal, runtime-generated CC settings file that registers the hooks a
# dispatched session cannot otherwise receive. Lives outside the repo so it is
# install-local and never committed; regenerated idempotently (see
# cc_span_settings_path).
_CC_SPAN_SETTINGS_PATH = Path.home() / ".genesis" / "cc-span-settings.json"

# The hook that enforces GENESIS_BASH_ALLOWLIST, named once so the registration
# and the pre-launch checks that verify it cannot drift apart.
_ALLOWLIST_GUARD_SCRIPT = "hooks/bash_allowlist_guard.sh"


# A sealed `gh` configuration for allowlisted sessions. Shared rather than
# per-dispatch because its content is derived from the operator's own gh config
# and is identical for every dispatch, so one idempotently-maintained directory
# has no cleanup surface and no concurrent-writer problem.
_SEALED_GH_CONFIG_DIR = Path.home() / ".genesis" / "gh-sealed"

# gh reads `config.yml` for aliases, pager and editor. Synthesised rather than
# copied: the session needs a file to exist (gh writes one on migration
# otherwise, which a read-only directory would turn into a hard failure), and
# the values we want are exactly "no aliases, no shell-spawning pager".
_SEALED_GH_CONFIG_YML = 'version: "1"\npager: cat\naliases: {}\n'


def _sealed_gh_config_dir() -> str | None:
    """A read-only ``GH_CONFIG_DIR`` that keeps ``gh`` from spawning a shell.

    THE PROBLEM. ``gh`` can be told to run arbitrary commands through its own
    configuration — a shell alias, a pager, an editor, an extension. Every one
    of those is reached with ``gh`` as the first token, so a first-token
    allowlist permits the command that installs the escape AND the command that
    triggers it. VERIFIED executing: two permitted ``gh`` invocations were
    enough to run an arbitrary program. That matters most on the one profile
    that has an allowlist, which is also the one that reads external pull
    request threads, i.e. attacker-authored text.

    THE FIX. Point the session at a directory gh cannot write: an unwritable
    ``config.yml`` means ``gh alias set`` and ``gh config set`` fail, and the
    synthesised contents pin the pager to ``cat`` so the pager route is closed
    even before that. MEASURED: auth still resolves, ordinary ``gh`` commands
    including live API calls still work, and both write paths return non-zero.

    ``hosts.yml`` IS copied, because it is where the credential lives and gh
    has no other way to find it. The copy is 0400 inside a 0500 directory owned
    by this user — the same reachability as the original, which is 0600 in the
    user's own home — so this moves a secret, it does not widen who can read
    one. Say that plainly rather than leaving it implied.

    Returns the directory, or ``None`` when it cannot be prepared. The caller
    REFUSES TO LAUNCH on that; it is not a degraded mode, because the fallback
    is the operator's own writable config.

    THIS DIRECTORY DOES NOT COVER EXTENSIONS. MEASURED: they resolve from the
    data dir, not from here, so sealing the config dir alone leaves
    ``gh extension`` wide open. ``_gh_hardening`` closes that by pointing
    ``XDG_DATA_HOME`` at this same directory; the measurement lives there.

    NOT a complete confinement of ``gh``. Subcommands that write files to
    caller-chosen paths remain available. The allowlist bounds the binary; this
    bounds one binary's self-reconfiguration. Both limits are documented in
    ``.claude/docs/background-sessions.md``.
    """
    target = _SEALED_GH_CONFIG_DIR
    try:
        # gh's OWN precedence, from `gh help environment`: GH_CONFIG_DIR, then
        # $XDG_CONFIG_HOME/gh, then ~/.config/gh. Implementing only the first
        # and last builds a valid-LOOKING seal with no credential in it on any
        # install that sets XDG_CONFIG_HOME — the session then launches
        # unauthenticated and every gh call fails, which reads as a broken
        # steward rather than as a missed config path.
        _xdg = os.environ.get("XDG_CONFIG_HOME")
        source = Path(
            os.environ.get("GH_CONFIG_DIR")
            or (Path(_xdg) / "gh" if _xdg else Path.home() / ".config" / "gh")
        )
        hosts = source / "hosts.yml"
        desired = {
            "config.yml": _SEALED_GH_CONFIG_YML,
            # Absent when gh was never authenticated. Seal anyway: an
            # unauthenticated session is no reason to leave the alias route
            # open. Read INSIDE the try — an unreadable or non-UTF-8 hosts.yml
            # would otherwise raise straight out of _build_env, past the
            # fallback this function documents.
            **({"hosts.yml": hosts.read_text(encoding="utf-8")} if hosts.is_file() else {}),
        }
        if _seal_matches(target, desired):
            return str(target)
        # REWRITES ARE SERIALISED. The seal is one shared directory, and the
        # rewrite is not atomic — it chmods the directory writable, unlinks,
        # writes, then chmods back. Two dispatches arriving together (first run,
        # or just after the operator's token changes) would otherwise interleave,
        # and the loser gets PermissionError mid-write, returns None, and the
        # caller refuses a launch that should have succeeded.
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.parent / f"{target.name}.lock"
        with open(lock_path, "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Re-check under the lock: the writer we queued behind may have
            # already produced exactly what we want.
            if _seal_matches(target, desired):
                return str(target)
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
            for stale in target.iterdir():
                if stale.is_file() and stale.name in desired:
                    continue
                # DIRECTORIES TOO. The seal's whole job since the extension
                # finding is that `<seal>/gh/extensions` does not exist — a
                # sweep that only unlinks FILES leaves exactly the subtree the
                # seal exists to prevent, and then locks it in at 0500.
                if stale.is_dir() and not stale.is_symlink():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()
            for name, body in desired.items():
                path = target / name
                path.touch(mode=0o600, exist_ok=True)
                path.chmod(0o600)
                path.write_text(body, encoding="utf-8")
                path.chmod(0o400)
            target.chmod(0o500)
        return str(target)
    except (OSError, UnicodeError):
        logger.warning("Could not prepare the sealed gh config at %s", target, exc_info=True)
        return None


def _seal_matches(target: Path, desired: dict[str, str]) -> bool:
    """Is the sealed directory already exactly ``desired``?

    Compares CONTENT and MODES both, because a seal whose files are writable is
    not a seal — a rewrite interrupted between its chmod-writable and its
    chmod-back leaves the right bytes at the wrong permissions, and a
    content-only check would call that good.

    ANY DIRECTORY ENTRY MEANS NO. Enumerating only ``is_file()`` was blind to
    the one thing the seal exists to prevent: ``XDG_DATA_HOME`` points here, so
    a planted ``gh/extensions/gh-x`` re-opens the extension route while this
    function reports the seal CLEAN and the stale sweep — also file-only —
    leaves it in place, permanently, at 0500. MEASURED before the fix: a
    planted extension survived a reseal and ``_seal_matches`` returned True.
    The seal is a flat set of files by construction, so a directory is never
    legitimate here and needs no name check.

    ANSWERS "NO" RATHER THAN RAISING when the directory moves under it. The
    first caller runs this OUTSIDE the rewrite lock, so a concurrent writer
    unlinking a stale file between the listing and the read is expected, not
    exceptional. Letting that escape would reach the caller's fallback and
    REFUSE a launch that should have succeeded — the queue-behind-the-lock path
    exists precisely to handle it, and it is only reached by returning False.
    Fail-closed is preserved: an unconfirmable seal is never reported as
    matching.
    """
    try:
        if not target.is_dir() or target.stat().st_mode & 0o777 != 0o500:
            return False
        entries = list(target.iterdir())
        if any(p.is_dir() and not p.is_symlink() for p in entries):
            return False
        present = {p.name: p for p in entries if p.is_file()}
        if set(present) != set(desired):
            return False
        return all(
            path.stat().st_mode & 0o777 == 0o400
            and path.read_text(encoding="utf-8") == desired[name]
            for name, path in present.items()
        )
    except (OSError, UnicodeError):
        return False


def _gh_hardening() -> dict[str, str] | None:
    """Environment that keeps an allowlisted ``gh`` from spawning a shell.

    ``gh`` runs a program of its own accord in a set its own documentation
    closes: an alias, the pager, the editor, the browser — and an extension,
    which is a program outright. Every one is reached with ``gh`` as the first
    token, so a first-token allowlist permits both the command that installs an
    escape and the command that fires it. This set is enumerated from
    ``gh help environment``, not from whatever a reviewer thought of next,
    which is the difference between closing the question and adding another
    round to a denylist.

    THE EXTENSION ROUTE IS NOT CLOSED BY THE CONFIG SEAL, and that is the trap
    worth stating loudly. MEASURED: extensions resolve from
    ``$XDG_DATA_HOME/gh/extensions``, NOT from ``GH_CONFIG_DIR``. An extension
    planted under the config dir was not found; one planted under the data dir
    RAN. So an install followed by an exec was arbitrary execution with every
    first token allowed. Pointing ``XDG_DATA_HOME`` at the same read-only seal
    closes both halves: the install cannot create ``<seal>/gh``, and the exec
    finds nothing. MEASURED alongside it, so the cure is known not to be worse
    than the disease: auth, live API calls and pull-request reads all still
    work under that pin.

    Editor and browser are pinned to ``true``. gh runs them through a shell, so
    a bare name suffices. Neither is reachable from inside a guarded session —
    the guard refuses a command whose first token is an environment assignment
    — but gh executes them from INHERITED environment, so they are pinned
    rather than argued about.

    MEASURED inert, and therefore deliberately NOT pinned: ``GH_PATH``. It
    tells gh where its own binary is, for extension callbacks. With a planted
    value an ordinary read still ran the real gh, and with extensions
    unreachable it redirects nothing. Pinning it would be a speculative change.

    Returns ``None`` when the seal could not be prepared. That is a REFUSAL
    signal, not a degraded mode: without the sealed directory the session falls
    back to the operator's own writable config, which is precisely where an
    alias escape is installed — and where any alias the operator already has is
    waiting. Pinning the pager alone would close one route of five and read as
    hardening.
    """
    sealed = _sealed_gh_config_dir()
    if sealed is None:
        return None
    return {
        "GH_CONFIG_DIR": sealed,
        # Extensions live under the DATA dir, not the config dir. Same seal.
        "XDG_DATA_HOME": sealed,
        "GH_PAGER": "cat",
        "PAGER": "cat",
        "GH_EDITOR": "true",
        "GIT_EDITOR": "true",
        "VISUAL": "true",
        "EDITOR": "true",
        "GH_BROWSER": "true",
        "BROWSER": "true",
    }


def _unconfinable_binary_message(binary: str) -> str:
    """Refusal text for a binary whose hardening could not be prepared."""
    return (
        f"Refusing to launch: this invocation allows {binary!r}, which can be "
        f"reconfigured to run commands, and its confinement could not be "
        f"prepared. Launching would give the session {binary!r} against a "
        f"writable configuration, which is an escape from the allowlist rather "
        f"than a weaker form of it."
    )


def _assert_hardening_present(env: dict[str, str], bash_allowlist: tuple[str, ...]) -> None:
    """Raise unless every allowlisted binary's confinement is in ``env``.

    Called on EVERY dict that is about to be launched, not once per build. The
    builder was described as "the only thing every launch path shares" and that
    was WRONG: both spawn paths merge ``_apply_login_fallback`` on top of the
    built env and launch the merged result, so a check that ended at the
    builder inspected a dict that was then added to. Same class as the
    ``env_overrides`` hole this branch closed, one call later.

    Recomputes the hardening rather than comparing against a remembered copy:
    an enumeration of variable names goes stale the moment a hardening grows a
    key, and a recompute also notices a seal that changed underneath us.
    """
    for binary in bash_allowlist:
        hardening = _BINARY_HARDENING.get(binary)
        if hardening is None:
            continue
        required = hardening()
        if required is None:
            raise RuntimeError(_unconfinable_binary_message(binary))
        wrong = sorted(k for k, v in required.items() if env.get(k) != v)
        if wrong:
            raise RuntimeError(_unhardened_env_message(binary, wrong))


def _unhardened_env_message(binary: str, wrong: list[str]) -> str:
    """Refusal text for hardening that is absent from the env being launched."""
    return (
        f"Refusing to launch: this invocation allows {binary!r}, but its "
        f"confinement is not in the environment the session would receive — "
        f"{', '.join(wrong)} {'differs' if len(wrong) == 1 else 'differ'} from "
        f"the hardened value. env_overrides is applied last and wins; drop the "
        f"override or drop the allowlist."
    )


#: Per-allowlisted-binary environment hardening. Keyed by the binary as it
#: appears in a profile's ``bash_allowlist``. A callable returning ``None``
#: means "this binary cannot be confined right now", and the invoker refuses to
#: launch rather than launching it unconfined.
_BINARY_HARDENING: dict[str, Callable[[], dict[str, str] | None]] = {"gh": _gh_hardening}


def _allowlist_guard_argv() -> list[str] | None:
    """The argv that runs the allowlist guard, computed LOCALLY.

    Single source of truth for two callers that must not disagree: the settings
    writer, which registers this command for Claude Code to run, and the
    pre-launch binding check, which runs it here to confirm it refuses.

    The binding check deliberately does NOT execute the command it parses out
    of the settings file. That file lives outside the repo and is writable by
    any same-uid process, so executing its contents would run an attacker-
    chosen argv inside the parent — which inherits the server's full
    environment, credentials included. The parsed value is COMPARED to this,
    and this is what runs.
    """
    from genesis import env

    genesis_hook = env.repo_root() / ".claude" / "hooks" / "genesis-hook"
    if not genesis_hook.exists():
        return None
    return [str(genesis_hook), _ALLOWLIST_GUARD_SCRIPT]


# Keep an owned background-wait ceiling strictly below the hard timeout_s SIGKILL
# so the CLI ends bg-wait + flushes a partial result (and prints its "terminating"
# marker) BEFORE our asyncio watchdog kills the process group. One source of truth
# for the "graceful truncation beats hard kill" invariant.
_BG_WAIT_HARD_MARGIN_MS = 60_000
# CC's general MCP operation timeout (env MCP_TIMEOUT, CC default 30_000ms), as a
# string because it goes straight into the child's environment.
#
# NOT named after CONNECT, deliberately. It DOES bound the server connect — the
# failure this ships for — but MEASURED in the shipped CC binary (2.1.246), the
# same getter also bounds tools/list, resource reads, generic MCP requests, the
# mcp_tool hook cap and the subscriptions listen stream. And CC has a SEPARATE
# MCP_CONNECT_TIMEOUT_MS (default 5_000ms) sitting next to it in the same env
# registry, so a constant called _MCP_CONNECT_TIMEOUT_MS would send the next
# maintainer grepping for the wrong variable.
#
# The widened ceiling therefore has a mid-session cost as well as a startup one: a
# server that wedges on tools/list now stalls 120s per operation rather than 30s.
# The shortest MCP-carrying dispatch budget is 600s (reflection LIGHT), so that is
# 20% of the smallest budget it is paid out of.
#
# MUST stay in step with the MCP_TIMEOUT in the repo's .claude/settings.json — the
# two cannot share a constant (one is JSON read by CC, one is Python read by us),
# so tests/test_cc/test_invoker_mcp_timeout.py compares them instead.
#
# 120s is ~11x the measured typical connect and ~4.7x the worst SUCCESSFUL one
# (25,395ms against CC's 30,000ms default, which is what made a drop possible).
_MCP_TIMEOUT_MS = "120000"
# Grace granted to surviving DESCENDANTS after the leader exits post-terminate,
# before the group-kill escalation (reap_bounded waits only on the leader, so
# without this a still-flushing MCP child gets zero grace of its own).
_ESCALATION_GRACE_S = 2.0

# Max bytes in ONE stream-json line. CC lines routinely exceed asyncio's
# 64 KiB default (a tool result is one line), so the reader is given a
# generous ceiling; a line ABOVE it is dropped rather than allowed to abort
# the stream — see the read loop in run_streaming.
_STREAM_LINE_LIMIT = 1_048_576  # 1 MiB

# Stable prefix of the CLI's headless bg-ceiling message (the numeric duration
# varies): "Background tasks still running after 600s; terminating." Matching the
# prefix is version-drift-tolerant — a miss degrades to no truncation notice
# (never a crash). See CCOutput.bg_truncated.
_BG_TRUNCATION_MARKER = "Background tasks still running after"


def _stderr_bg_truncated(stderr_text: str | None) -> bool:
    """True if CC's stderr shows it SIGKILLed background tasks at the wait ceiling."""
    return bool(stderr_text) and _BG_TRUNCATION_MARKER in stderr_text


def _unreplayable_after_drop(dropped: int, outcome: str) -> CCStreamTruncatedError:
    """The no-retry error for a run whose stream we could not fully read.

    A drop is not only a lost-ANSWER problem; it is a lost-EVIDENCE problem. The
    over-limit line is typically a tool result, so the tool call behind it may
    already have run — an MCP write, an outreach send — and nothing downstream
    dedupes a second one. Whatever the result event then says about the run, a
    retry of it is unsafe, so every raise on a dropped stream carries the type
    the retry sites know to leave alone.

    ``outcome`` names what the run reported, so the message says which shape hit
    it rather than inviting a guess.
    """
    return CCStreamTruncatedError(
        f"CC stream dropped {dropped} over-limit line(s) "
        f"(limit={_STREAM_LINE_LIMIT} bytes) and {outcome}. The run must not be "
        "replayed: the tool calls behind the dropped line may already have run."
    )


async def _emit_bg_truncation_event(cc_session_id: str) -> None:
    """Fire a ``cc.bg_truncated`` observability event via the runtime singleton.

    Central awareness signal that a dispatched Workflow/subagent was SIGKILLed at
    the background-wait ceiling with only a partial result. Reaches the bus through
    the runtime singleton (the invoker holds no bus ref); no-ops cleanly when the
    runtime/bus is absent (tests, early startup) and never raises.
    """
    try:
        from genesis.runtime import GenesisRuntime

        bus = getattr(GenesisRuntime.instance(), "_event_bus", None)
        if bus is None:
            return
        from genesis.observability.types import Severity, Subsystem

        await bus.emit(
            Subsystem.PROVIDERS,
            Severity.WARNING,
            "cc.bg_truncated",
            "CC background tasks truncated at the wait ceiling — dispatched work "
            "was killed mid-run with a partial result",
            cc_session_id=cc_session_id,
        )
    except Exception:
        logger.debug("cc.bg_truncated event emit failed", exc_info=True)


def cc_span_settings_path() -> str | None:
    """Generate (idempotently) the minimal CC settings file injected into every
    dispatched session, and return its absolute path — or ``None`` if the
    launcher is unavailable.

    (The name is historical: the span hook was this file's first tenant. It now
    carries the small set of hooks a dispatch cannot otherwise receive.)

    Why this exists: dispatched CC sessions run with a working directory outside
    any git repo (``~/.genesis/background-sessions``), and Claude Code discovers
    project ``.claude/settings.json`` via git-root detection — so the repo-level
    hook registration never loads there and ``cc_span_hook`` never fires. Passing
    this file via ``--settings`` injects JUST these hooks; CC merges them with
    the user's settings, leaving every other hook untouched.

    Two hooks, and BOTH are registered UNCONDITIONALLY because both are
    documented no-ops unless an environment variable is set — which is what lets
    this stay a single fixed path written idempotently, with no per-invocation
    content for two concurrent dispatches to race over:

    * ``cc_span_hook`` (PostToolUse) no-ops unless ``GENESIS_TRACE_ID`` is set.
      This is the *single* registration — the repo-level one was removed to
      avoid a double-fire when a dispatch runs in a worktree cwd, which *does*
      load repo settings.
    * ``bash_allowlist_guard`` (PreToolUse/Bash) no-ops unless
      ``GENESIS_BASH_ALLOWLIST`` is set, returning before it reads stdin. It is
      what actually enforces a scoped profile's Bash restriction; without it the
      restriction is declared by ``_build_env`` and read by nobody. See
      ``scripts/hooks/bash_allowlist_lib.sh`` for the predicate and the reason
      an install may safely have both this and the user-level chokepoint wired.
      The no-op is cheap but not free: MEASURED ~30ms per Bash call on a live
      install, spent in the launcher rather than the guard, and paid by EVERY
      dispatched session rather than only scoped ones.

    MEASURED 2026-09-23 on CC 2.1.246, from a dispatch-shaped invocation (cwd
    outside any repo, ``--dangerously-skip-permissions``): a PreToolUse Bash
    hook supplied via ``--settings`` fires, and its exit 2 refuses the call —
    the command demonstrably does not run. Controls: the same invocation without
    ``--settings`` ran the command and left no hook marker.

    The hook commands use an ABSOLUTE path to the ``genesis-hook`` launcher,
    which self-locates the install root from its own filesystem position — NOT
    ``${CLAUDE_PROJECT_DIR}``, which CC leaves unset in dispatched sessions.
    Written atomically and only when stale, so it tracks the install root across
    updates with no bootstrap-ordering dependency and no per-dispatch churn.
    """
    from genesis import env

    genesis_hook = env.repo_root() / ".claude" / "hooks" / "genesis-hook"
    if not genesis_hook.exists():
        return None
    guard_argv = _allowlist_guard_argv()
    if guard_argv is None:  # pragma: no cover - same existence test as above
        return None

    desired = json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        # ANCHORED, and measured rather than assumed: a matcher
                        # is a REGEX (probed on CC 2.1.246 — a hook registered
                        # as "^Bash$" fires on a Bash call), so a bare "Bash"
                        # also matches "BashOutput". That tool carries no
                        # .tool_input.command, so under an allowlist it would
                        # hit this guard's fail-closed leg and be refused with a
                        # message about a command it never had. The ~15 hooks in
                        # .claude/settings.json using a bare "Bash" do not show
                        # this because they all fail OPEN on an unreadable
                        # payload.
                        "matcher": "^Bash$",
                        "hooks": [
                            {
                                "type": "command",
                                # Built from the same helper the pre-launch
                                # binding check uses, so the command that is
                                # registered and the command that is verified
                                # cannot drift. shlex.join quotes it: an install
                                # root containing a space would otherwise
                                # produce a command CC's shell cannot resolve —
                                # exit 127, which is non-blocking, i.e. a
                                # permit.
                                "command": shlex.join(guard_argv),
                                # Generous by ~300x against the guard's real cost
                                # (an env test, one jq, one awk — well under a
                                # tenth of a second), because the failure
                                # direction is asymmetric: a PreToolUse hook that
                                # exceeds its declared timeout is killed and the
                                # call PROCEEDS, so a tight bound on a
                                # containment hook converts it into a silent
                                # permit. Bounded rather than omitted so a hook
                                # that somehow hangs cannot stall the session
                                # indefinitely.
                                "timeout": 30,
                            },
                        ],
                    },
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{genesis_hook} hooks/cc_span_hook.py",
                                "timeout": 500,
                            },
                        ],
                    },
                ],
            },
        },
        indent=2,
    )

    path = _CC_SPAN_SETTINGS_PATH
    try:
        if path.exists() and path.read_text(encoding="utf-8") == desired:
            return str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name (pid-suffixed) so concurrent processes can't clobber
        # each other mid-write; os.replace is atomic.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(desired, encoding="utf-8")
        os.replace(tmp, path)
        return str(path)
    except OSError:
        logger.warning(
            "Could not write CC span settings file at %s",
            path,
            exc_info=True,
        )
        return None


class CCInvoker:
    """Invokes claude CLI as async subprocess."""

    _TIER_RANK = {CCModel.HAIKU: 0, CCModel.SONNET: 1, CCModel.OPUS: 2, CCModel.FABLE: 3}

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        working_dir: str | None = None,
        on_cc_status_change: (Callable[[str], Awaitable[None]] | None) = None,
        on_model_downgrade: (Callable[[str, str, str], Awaitable[None]] | None) = None,
        on_cc_empty_output: (Callable[[CCInvocation, CCOutput], Awaitable[None]] | None) = None,
        protected_paths: object | None = None,
    ):
        self._claude_path = claude_path
        self._working_dir = working_dir
        # cc-loop-01: per-session subprocess registry (keyed by CCInvocation.
        # session_key, else "pid:<pid>"). Replaces a single _active_proc slot
        # so concurrent sessions don't clobber each other and `/stop` can
        # interrupt the RIGHT proc. Single-threaded asyncio → no lock needed.
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}
        self._on_cc_status_change = on_cc_status_change
        self._on_model_downgrade = on_model_downgrade
        self._on_cc_empty_output = on_cc_empty_output
        self._last_was_error = False
        self._status_lock = asyncio.Lock()
        self._protected_paths = protected_paths
        # `claude --version` per binary FILE identity — see _cc_version. Per
        # instance, never module-global: a module cache would let one invoker's
        # answer (or a test's) stand in for another's binary.
        self._cc_versions: dict[tuple[str, int, int], tuple[int, int, int]] = {}
        self._cc_version_failed_at: dict[tuple[str, int, int], float] = {}
        self._cc_version_warned: set[tuple[str, int, int] | str] = set()
        self._clock: Callable[[], float] = time.monotonic  # injectable for tests

        # Advisory check — warn early if the CLI binary is not findable.
        resolved = shutil.which(claude_path)
        if resolved:
            logger.info("Claude CLI resolved to: %s", resolved)
        else:
            logger.warning(
                "Claude CLI %r not found on PATH. CC invocations will fail. "
                "Ensure @anthropic-ai/claude-code is installed via npm and "
                "~/.npm-global/bin is on PATH.",
                claude_path,
            )

    async def _cc_version(self) -> tuple[int, int, int] | None:
        """The version of the CC binary this invoker runs, or None if unreadable.

        Keyed by the resolved file's (path, inode, mtime), so an update that
        replaces the binary under a running server is read afresh, and read once
        per file otherwise. A failed read is not cached for good — one bad moment
        must not pin a long-lived server to the additive path — but it is not
        retried for ``_CC_VERSION_RETRY_S`` either, and it warns once per file.

        Runs via ``subprocess.run`` in a thread rather than the asyncio spawner,
        which tests patch to impersonate a CC run. The 15 s bound: this runs on
        the turn path after the answer is in hand, so a CLI that hangs on
        ``--version`` holds that reply until the bound (``claude --version``
        measured 25 ms on 2.1.280). The retry cooldown limits that to one reply
        per window; the answer itself is never lost, only its cost reading
        degrades to additive.
        """
        resolved = shutil.which(self._claude_path)
        if not resolved:
            self._warn_version_once(self._claude_path, "binary not found")
            return None
        real = os.path.realpath(resolved)
        try:
            st = os.stat(real)
        except OSError as exc:
            self._warn_version_once(real, f"stat failed: {exc}")
            return None
        key = (real, st.st_ino, st.st_mtime_ns)
        cached = self._cc_versions.get(key)
        if cached is not None:
            return cached
        failed_at = self._cc_version_failed_at.get(key)
        if failed_at is not None and self._clock() - failed_at < _CC_VERSION_RETRY_S:
            return None
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._cc_version_failed_at[key] = self._clock()
            self._warn_version_once(key, f"{type(exc).__name__}: {exc}")
            return None
        version = parse_cc_version(proc.stdout) if proc.returncode == 0 else None
        if version is None:
            self._cc_version_failed_at[key] = self._clock()
            self._warn_version_once(
                key, f"exit {proc.returncode}, stdout {proc.stdout.strip()[:80]!r}"
            )
            return None
        self._cc_versions[key] = version
        return version

    def _warn_version_once(self, key: tuple[str, int, int] | str, why: str) -> None:
        if key in self._cc_version_warned:
            return
        self._cc_version_warned.add(key)
        logger.warning(
            "Could not read the CC version (%s) — recording resumed-turn cost "
            "ADDITIVELY, which over-counts on CC 2.1.277+ (cost is display-only)",
            why,
        )

    async def _with_cost_semantics(self, output: CCOutput) -> CCOutput:
        """Stamp whether ``output.cost_usd`` is a running total (see
        ``cost_is_cumulative_for``). The one place the flag is set."""
        return replace(output, cost_is_cumulative=cost_is_cumulative_for(await self._cc_version()))

    @property
    def working_dir(self) -> str | None:
        """Working directory for CC subprocess (project root for CLAUDE.md context)."""
        return self._working_dir

    def set_protected_paths(self, registry: object) -> None:
        """Late-bind ProtectedPathRegistry (initialized after CCInvoker)."""
        self._protected_paths = registry

    async def _fire_downgrade_callback(self, output: CCOutput) -> None:
        """Invoke model downgrade callback if applicable. Never raises."""
        if not output.downgraded or not self._on_model_downgrade:
            return
        try:
            await self._on_model_downgrade(
                output.model_requested,
                output.model_used,
                output.session_id,
            )
        except Exception:
            logger.warning("Model downgrade callback failed", exc_info=True)

    async def _fire_empty_output_callback(
        self,
        invocation: CCInvocation,
        output: CCOutput,
    ) -> None:
        """Notify the runtime when an output-EXPECTING invocation returns empty.

        The silent-cap signature: an Anthropic-subscription cap makes ``claude -p``
        return 0-token, no-text output with ``is_error=False`` and no
        ``rate_limit_event`` — which every success path reads as a completed run.
        Only invocations that opt in via ``expect_output`` (output-producing
        cognitive call sites) are considered; the caller has already confirmed the
        output is genuinely empty and non-error before calling this. This is
        DETECTION only — it never raises and never alters control flow (the empty
        output is still returned to the caller exactly as before). Never raises;
        mirrors ``_fire_downgrade_callback``.
        """
        if self._on_cc_empty_output is None:
            return
        try:
            await self._on_cc_empty_output(invocation, output)
        except Exception:
            logger.warning("CC empty-output callback failed", exc_info=True)

    def _refuse_unenforceable_allowlist(self, inv: CCInvocation, span_settings: str | None) -> None:
        """Refuse to launch a Bash-restricted profile whose restriction will not
        be enforced.

        ``_build_env`` exports ``GENESIS_BASH_ALLOWLIST`` for a scoped profile,
        but the export is only a DECLARATION — a hook has to read it. If the
        hook is not going to run, the profile launches with unrestricted Bash
        while every declaration in the codebase says it is confined, which is
        strictly worse than having no allowlist at all: the safety argument in
        ``autonomy/audit.py`` rests on it.

        Checked here rather than by parsing the user's settings — that would be
        the wrong instrument, since it would also have to find a USER-level
        registration, which an install may legitimately have, and refusing there
        would strand a correctly-protected box. What the invoker can answer
        exactly is whether IT armed the hook, and whether the hook BINDS.

        Called from ``_build_args``, which is the chokepoint both spawn paths
        share (``run`` and ``run_streaming``), so one check covers both.
        """
        if not inv.bash_allowlist:
            return

        allowlist = ",".join(inv.bash_allowlist)
        if span_settings is None:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but the settings file that registers the "
                f"enforcing hook could not be written, so the restriction "
                f"would not be applied. Check that "
                f".claude/hooks/genesis-hook exists and that ~/.genesis is "
                f"writable."
            )
        # ORDER MATTERS, and not only for speed. The checks below are grouped
        # cheapest-and-most-specific first: the three that read only fields of
        # this invocation, then the file read, then the subprocess probes. A
        # caller that trips one of the cheap conditions gets the message about
        # THAT condition rather than a generic one from a later check it would
        # also have failed — which is what stops a test asserting "it refuses"
        # from passing for the wrong reason.
        #
        # env_overrides is applied LAST in _build_env and wins over everything,
        # so it can blank the variable the guard reads after every other check
        # has passed. Nothing does this today; this keeps it that way.
        override = (inv.env_overrides or {}).get("GENESIS_BASH_ALLOWLIST")
        if override is not None and override != allowlist:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but env_overrides sets "
                f"GENESIS_BASH_ALLOWLIST={override!r}, and env_overrides wins — "
                f"the guard would read that instead. Set one or the other."
            )
        if inv.bare:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but --bare skips hooks entirely, so the "
                f"restriction would not be applied. Drop bare=True, or drop "
                f"the bash_allowlist and confine the profile another way."
            )
        if inv.safe_mode:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but --safe-mode disables all hooks, so the "
                f"restriction would not be applied. Drop safe_mode=True, or "
                f"drop the bash_allowlist and confine the profile another way."
            )

    async def verify_allowlist_enforceable(self, inv: CCInvocation) -> None:
        """The EXPENSIVE half of the allowlist check, kept off the event loop.

        ``_build_args`` stays synchronous — the tests call it directly, and the
        cheap checks there answer from fields of the invocation alone. What
        cannot stay there is this: a settings read, a seal write, and two
        subprocess probes with a 30s ceiling each. ``_build_args`` is called
        from inside coroutines, so running those inline would stall the whole
        server loop — channels, other sessions, cancellations — for up to a
        minute on a hanging guard.

        ``_get_scope_args`` in this module was made async for exactly this
        reason and says so; this follows it rather than inventing a second
        shape.

        MUST be awaited by every spawn path before the child is started. Both
        of them do, immediately after ``_build_args``, and a test asserts it —
        a path that skipped this would launch a profile whose confinement was
        never demonstrated, which is the defect this whole change exists to
        remove.
        """
        if not inv.bash_allowlist:
            return
        await asyncio.to_thread(self._verify_allowlist_enforceable_blocking, inv)

    def _verify_allowlist_enforceable_blocking(self, inv: CCInvocation) -> None:
        """Body of :meth:`verify_allowlist_enforceable`; runs in a worker thread."""
        allowlist = ",".join(inv.bash_allowlist)
        span_settings = cc_span_settings_path()
        if span_settings is None:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but the settings file that registers the "
                f"enforcing hook could not be written."
            )
        # The settings file is ONE shared path, so a concurrently-running
        # Genesis process on OLDER code recomputes the span-only payload, sees
        # this content as stale, and rewrites it — after we wrote it. Re-read
        # immediately before launch rather than trusting the write.
        try:
            registered = Path(span_settings).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but the settings file could not be re-read to "
                f"confirm the enforcing hook is registered ({exc.strerror})."
            ) from exc
        # The child's REAL environment, not this process's. env_overrides is
        # applied last and wins, so a probe run under os.environ would test a
        # different PATH (where `jq`/`awk` resolve from) and a different
        # GENESIS_HOOK_DEV_LOCAL (which root the guard is loaded from) than
        # the session actually gets. Defending one variable by name was the
        # narrower version of this; building the same env makes the check
        # faithful by construction.
        # _build_env is the chokepoint for the hardening itself: it refuses
        # there, on the env it returns, so the environment that was checked IS
        # the environment that gets launched. Checking a second copy here would
        # re-open the gap it closes — both spawn paths rebuild the env after
        # this runs, and only a check inside the builder covers that rebuild.
        child_env = self._build_env(inv)
        self._verify_allowlist_guard_binds(registered, inv.bash_allowlist, child_env)

    @staticmethod
    def _verify_allowlist_guard_binds(
        registered: str, bash_allowlist: tuple[str, ...], child_env: dict[str, str]
    ) -> None:
        """Prove the registered guard REFUSES and PERMITS — by running it.

        Every check above establishes that the guard is REGISTERED. None of them
        establishes that it BINDS, and the gap between those is where this
        failed once already: the launcher resolves a hook against the MAIN
        worktree, while the registrar checks the INVOKING tree, so a guard
        present to the registrar can be absent to the launcher. The launcher
        then exits non-blocking and every command runs.

        Rather than re-implement the launcher's root resolution here — which
        would be a second copy of shell logic, free to drift from the first —
        run the guard and read the verdicts. BOTH directions: a refusal alone
        would also be produced by a launcher refusing everything because it
        cannot find the guard at all, which is a contained session but not a
        working one.

        WHAT RUNS IS THE LOCALLY-COMPUTED ARGV, never the string parsed out of
        the settings file. That file sits outside the repo and is writable by
        any same-uid process, and this subprocess runs in the PARENT, which
        carries the server's whole environment — every API key in secrets.env
        among it. Executing a value read from a mutable file there would hand
        an attacker who can win one write a credential-bearing shell. So the
        registered command is parsed only to be COMPARED, and a mismatch is a
        refusal rather than a thing to run.

        The comparison is also why the entry is matched by CONTENT rather than
        taken from index 0: a second PreToolUse hook registered ahead of this
        one would otherwise be executed and its exit code read as the allowlist
        verdict.

        Costs two short subprocesses, paid only by an invocation that declares
        an allowlist. Those are rare, and the alternative is launching a profile
        whose confinement has never been demonstrated.
        """
        expected = _allowlist_guard_argv()
        if expected is None:
            raise RuntimeError(
                "Refusing to launch: the genesis-hook launcher is not present, "
                "so the Bash allowlist cannot be enforced."
            )
        try:
            commands = [
                hook.get("command", "")
                for block in json.loads(registered)["hooks"].get("PreToolUse", [])
                for hook in block.get("hooks", [])
            ]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"Refusing to launch: the registered Bash-allowlist hook could "
                f"not be read out of the settings file to verify it ({exc})."
            ) from exc
        # Matched by CONTENT, which covers both ways this can be wrong with one
        # test: the file was rewritten by another Genesis process on older code
        # (so the guard is simply absent), or it was modified to name something
        # else. Selecting by index would additionally have run a second hook
        # registered ahead of this one and read ITS exit code as the verdict.
        if not any(shlex.split(command) == expected for command in commands):
            raise RuntimeError(
                "Refusing to launch: the settings file does not register the "
                "Bash-allowlist hook this process computed — it has been "
                "rewritten since, most likely by another Genesis process "
                "running older code. Refusing rather than running what it now "
                "names."
            )
        argv = expected

        allowlist = ",".join(bash_allowlist)
        env = {**child_env, "GENESIS_BASH_ALLOWLIST": allowlist}

        def _probe(cmd: str) -> int:
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
            try:
                return subprocess.run(
                    argv,
                    input=payload,
                    capture_output=True,
                    text=True,
                    env=env,
                    # Matches the timeout the hook is registered with, so a
                    # guard slow enough to fail there fails here too rather
                    # than passing this check and being killed in the session.
                    timeout=30,
                ).returncode
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(
                    f"Refusing to launch: this invocation restricts Bash to "
                    f"[{allowlist}], but the enforcing hook could not be run to "
                    f"verify it ({exc})."
                ) from exc

        # A token that cannot be on any allowlist, so a working guard refuses it.
        refused = _probe("__genesis_allowlist_verification_probe__")
        if refused != 2:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], but the registered hook returned {refused} for "
                f"a command outside that list instead of refusing it, so the "
                f"restriction is not in force. Check that "
                f"scripts/hooks/bash_allowlist_guard.sh is present in the hook "
                f"root the launcher resolves (the MAIN worktree, which can lag "
                f"the tree this process is running from)."
            )

        permitted = _probe(bash_allowlist[0])
        if permitted != 0:
            raise RuntimeError(
                f"Refusing to launch: this invocation restricts Bash to "
                f"[{allowlist}], and the registered hook refuses even "
                f"{bash_allowlist[0]!r} (returned {permitted}), so the session "
                f"could do nothing at all. This usually means the guard is "
                f"missing from the launcher's hook root and the launcher is "
                f"refusing on its behalf."
            )

    def _build_args(self, inv: CCInvocation) -> list[str]:
        args = [self._claude_path, "-p"]
        # Roster routing: when model_id_override is set, model selection comes
        # entirely from ANTHROPIC_MODEL (set in _build_env). A --model flag here
        # would override that env var (CLI wins) and force the Anthropic tier
        # instead of the roster model — so omit it in that case.
        if inv.model_id_override is None:
            args += ["--model", str(inv.model)]
        args += ["--output-format", inv.output_format]
        # Haiku does not use an effort setting — omit --effort entirely rather
        # than pass a no-op. All other tiers accept the full low..max range.
        if model_supports_effort(inv.model):
            effort = clamp_effort(inv.model, inv.effort)
            if effort != inv.effort:
                logger.warning(
                    "Effort %r exceeds max for model %r — clamping to %r",
                    str(inv.effort),
                    str(inv.model),
                    str(effort),
                )
            args += ["--effort", str(effort)]
        elif inv.effort != EffortLevel.MEDIUM:
            # Only note when the caller explicitly asked for a non-default effort
            # that we're dropping, so intent stays visible without log spam.
            logger.debug(
                "Model %r does not use an effort setting — dropping requested effort %r",
                str(inv.model),
                str(inv.effort),
            )
        system_prompt = inv.system_prompt
        if system_prompt and inv.skip_permissions and self._protected_paths:
            protection_context = self._protected_paths.format_for_prompt()
            if protection_context:
                system_prompt = system_prompt + "\n\n" + protection_context
        if system_prompt:
            flag = "--append-system-prompt" if inv.append_system_prompt else "--system-prompt"
            args += [flag, system_prompt]
        if inv.resume_session_id:
            args += ["--resume", inv.resume_session_id]
        if inv.mcp_config:
            args += ["--mcp-config", inv.mcp_config]
        # --bare already disables ALL MCP discovery; combining it with
        # --strict-mcp-config makes CC exit non-zero (probe-verified, CC 2.1.x),
        # so skip strict under bare. strict is safe with any other config state:
        # with a --mcp-config it pins to those servers; with none it yields zero
        # servers cleanly (probe-verified) — the secure-by-default posture.
        if inv.strict_mcp_config and not inv.bare:
            args.append("--strict-mcp-config")
        # Register the dispatch hooks (span capture, Bash allowlist enforcement)
        # for this session. Dispatched sessions run with a cwd outside any git
        # repo, so CC never loads the repo's .claude/settings.json; --settings
        # injects just these hooks and CC merges them with the user's settings.
        # Both no-op unless their env var is set. See cc_span_settings_path.
        span_settings = cc_span_settings_path()
        if span_settings:
            args += ["--settings", span_settings]
        self._refuse_unenforceable_allowlist(inv, span_settings)
        if inv.skip_permissions:
            args.append("--dangerously-skip-permissions")
        if inv.allowed_tools:
            args += ["--allowedTools", ",".join(inv.allowed_tools)]
        if inv.disallowed_tools:
            args += ["--disallowedTools", ",".join(inv.disallowed_tools)]
        if inv.bare:
            args.append("--bare")
        if inv.safe_mode:
            args.append("--safe-mode")
        # Prompt is passed via stdin (see run/run_streaming), not as a CLI
        # argument.  This avoids argument-parsing edge cases (the "--"
        # separator broke -p prompt detection) and handles arbitrarily long
        # prompts safely.
        return args

    # CC's Bash sandbox root — persistent disk, managed by tmp_watchgod.
    _CC_SANDBOX_TMPDIR = Path.home() / ".genesis" / "cc-tmp"

    def _build_env(self, inv: CCInvocation | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # Signal to SessionStart hooks that this is a Genesis-dispatched session.
        # The genesis_session_context.py hook skips identity injection when set,
        # preventing double injection (identity is in the system prompt arg).
        env["GENESIS_CC_SESSION"] = "1"
        # Propagate Genesis session_id to child CC + MCP server processes
        # so eval hooks can attribute recall events to specific sessions.
        from genesis.observability.session_context import get_session_id

        _sid = get_session_id()
        if _sid:
            env["GENESIS_SESSION_ID"] = _sid
        else:
            env.pop("GENESIS_SESSION_ID", None)
        # WS-3 session-level provenance: stamp the dispatched session's origin so
        # its memory MCP writes classify accordingly (read fail-safe by
        # memory.provenance.session_origin_from_env in the MCP children).
        # Validated loudly at CCInvocation construction; POP when unset so a
        # stale value can never leak from this process's own environment.
        if inv and inv.origin:
            env["GENESIS_SESSION_ORIGIN"] = inv.origin
        else:
            env.pop("GENESIS_SESSION_ORIGIN", None)
        # WS-3 B4 gate-4: supervision marker. GENESIS_SESSION_ID above is pure
        # attribution (foreground conversations carry one too), so the enforce
        # drop needs this SEPARATE signal to spare owner-attended surfaces.
        # POP when unset so a stale value can never leak from this process.
        if inv and inv.supervised:
            env["GENESIS_SESSION_SUPERVISED"] = "1"
        else:
            env.pop("GENESIS_SESSION_SUPERVISED", None)
        # Propagate the active trace context so the CC PostToolUse span hook can
        # stitch this session's tool spans under the dispatching operation's
        # trace (cross-process). Absent when no span is active → hook no-ops.
        from genesis.observability.spans import current_trace_context

        _tc = current_trace_context()
        if _tc:
            env["GENESIS_TRACE_ID"], env["GENESIS_PARENT_SPAN_ID"] = _tc
        else:
            env.pop("GENESIS_TRACE_ID", None)
            env.pop("GENESIS_PARENT_SPAN_ID", None)
        if inv and inv.stream_idle_timeout_ms is not None:
            env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] = str(inv.stream_idle_timeout_ms)
        # The repo's .claude/settings.json carries the same value, but MOST
        # dispatched sessions cannot read it: they run with a cwd outside any git
        # repo (background_session_dir), so CC never loads the repo settings.
        # Without this line the background fleet — reflection, research, sentinel,
        # direct sessions — keeps CC's 30s default.
        #
        # The exception is a worktree-cwd dispatch (autonomy/executor/review.py
        # passes working_dir=<worktree>), where CC DOES load repo settings — see
        # the --settings comment in _build_args, which says so explicitly. Both
        # halves carry the same number, so those two paths agree either way.
        #
        # This is the half that matters most. A server dropped on connect is gone
        # for the life of the process and nothing announces it, so a foreground
        # session at least has someone present to notice the tools are missing. An
        # unattended one does not: it runs to completion believing it had memory.
        #
        # setdefault, matching the ceiling below: WITHIN THE ENV WE BUILD, an
        # operator's inherited MCP_TIMEOUT wins and env_overrides (applied last)
        # wins over both. Scoped deliberately — on a worktree-cwd dispatch CC then
        # applies .claude/settings.json over the inherited environment
        # (Object.assign, MEASURED in CC 2.1.246), so the repo value wins there
        # regardless of what we set. Harmless while both carry the same number.
        env.setdefault("MCP_TIMEOUT", _MCP_TIMEOUT_MS)
        # Own the headless background-task wait ceiling for lanes that run long
        # dispatched work. Clamp strictly below the hard timeout_s so the CLI's
        # graceful truncation + partial flush always precedes our SIGKILL. An
        # operator's value inherited from os.environ wins (setdefault); so does an
        # explicit env_overrides entry (applied last, below).
        if inv and inv.bg_wait_ceiling_ms is not None:
            hard_ms = inv.timeout_s * 1000
            # Only set the ceiling when there's room to sit a full margin below the
            # hard timeout. For a very short timeout_s the margin would drive it to
            # <=0, and 0 means "wait indefinitely" to the CLI — the opposite of intent;
            # leave the CLI default (600s) in that degenerate case.
            if hard_ms > _BG_WAIT_HARD_MARGIN_MS:
                ceil_ms = min(inv.bg_wait_ceiling_ms, hard_ms - _BG_WAIT_HARD_MARGIN_MS)
                env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", str(ceil_ms))
        # Roster routing (base_url / auth_token / model slots) + credential
        # isolation. Shared with the foreground `gmodel` launcher via
        # roster.apply_routing_env so the contract lives in ONE place. Native
        # Claude (no override fields) → all routing vars popped, ANTHROPIC_API_KEY
        # kept (Max subscription) — identical to the prior inline behavior.
        roster.apply_routing_env(
            env,
            base_url=inv.anthropic_base_url if inv else None,
            auth_token=inv.anthropic_auth_token if inv else None,
            model_id=inv.model_id_override if inv else None,
        )
        # Move CC's Bash sandbox off /tmp (512MB tmpfs) onto persistent disk.
        # CC reads CLAUDE_CODE_TMPDIR to choose where it creates
        # /claude-<uid>/<cwd>/<session-id>/ for each Bash invocation.
        # Without this, the sandbox lives on /tmp where intermittent ENOENT
        # failures break the Bash tool for entire sessions.
        # A per-invocation override isolates blast radius: e.g. the model-roster
        # gauntlet points its throwaway CC sessions at a separate sandbox so a
        # fixture that fills it can't trip genesis-tmp-watchgod into SIGKILLing a
        # LIVE foreground/background session sharing the default cc-tmp.
        env["CLAUDE_CODE_TMPDIR"] = str(
            (inv.claude_code_tmpdir if inv and inv.claude_code_tmpdir else None)
            or self._CC_SANDBOX_TMPDIR
        )
        # Keep TMPDIR consistent with CLAUDE_CODE_TMPDIR (never inconsistent — the
        # invariant util/tmp.py protects). This also completes the per-invocation
        # sandbox isolation above: without it a headless session's *subprocess*
        # temp (e.g. the gauntlet agent running the fixture's pytest, whose
        # tmp_path defaults under $TMPDIR) still lands in the inherited cc-tmp and
        # can trip genesis-tmp-watchgod. For the default sandbox both resolve to
        # cc-tmp (unchanged); for an override (gauntlet) TMPDIR follows it off
        # cc-tmp.
        env["TMPDIR"] = env["CLAUDE_CODE_TMPDIR"]
        # Prevent CC's alt-screen renderer from corrupting terminal scrollback
        # in Linux/tmux.  No-op on CC <2.1.132; required post-migration.
        env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
        # Restrict Bash to an allowlist of command binaries for scoped profiles
        # (e.g. "steward" → gh only). scripts/bash_safety_hook.sh reads this and
        # blocks any non-allowlisted command. Absent → no restriction (the var
        # must not leak from the parent, so pop when the field is empty).
        required_hardening: dict[str, dict[str, str]] = {}
        if inv and inv.bash_allowlist:
            env["GENESIS_BASH_ALLOWLIST"] = ",".join(inv.bash_allowlist)
            # PER-BINARY HARDENING. A first-token allowlist bounds WHICH binary
            # runs; it cannot bound what that binary can be told to do, and an
            # allowlisted binary with a shell escape hands the session an
            # unrestricted shell while every token is still the allowed one.
            # Each entry that needs it gets its hardening applied here.
            for binary in inv.bash_allowlist:
                hardening = _BINARY_HARDENING.get(binary)
                if hardening is None:
                    continue
                applied = hardening()
                if applied is None:
                    # FAIL CLOSED. Skipping the update would launch the session
                    # against the operator's writable configuration — the very
                    # escape this hardening exists to remove, and an outcome
                    # strictly worse than not launching at all.
                    raise RuntimeError(_unconfinable_binary_message(binary))
                required_hardening[binary] = applied
                env.update(applied)
        else:
            env.pop("GENESIS_BASH_ALLOWLIST", None)
        # Per-invocation overrides win over EVERYTHING above (inherited environ,
        # roster routing, sandbox tmpdir) — the deliberate escape hatch for env
        # the invoker doesn't model (e.g. the eval bench's CLAUDE_CONFIG_DIR
        # cleanroom). Applied last by contract; see CCInvocation.env_overrides.
        if inv and inv.env_overrides:
            env.update(inv.env_overrides)
            # Preserve the TMPDIR ≡ CLAUDE_CODE_TMPDIR invariant even if an
            # override changed CLAUDE_CODE_TMPDIR but not TMPDIR (else the two
            # silently desync). An explicit TMPDIR override still wins.
            if "CLAUDE_CODE_TMPDIR" in inv.env_overrides and "TMPDIR" not in inv.env_overrides:
                env["TMPDIR"] = env["CLAUDE_CODE_TMPDIR"]
        # LAST WORD, deliberately after env_overrides. Overrides are applied
        # last and win over everything, so they can replace a confinement that
        # was correctly applied above. Re-read the env being returned rather
        # than trusting that the update stuck: this function is the only thing
        # every launch path shares, so a check here is a check on what actually
        # runs. Comparing against the freshly computed hardening, not a list of
        # variable names, keeps it honest when a hardening grows a key.
        for binary, required in required_hardening.items():
            wrong = sorted(k for k, v in required.items() if env.get(k) != v)
            if wrong:
                raise RuntimeError(_unhardened_env_message(binary, wrong))
        return env

    @staticmethod
    def _launch_env(env: dict[str, str], inv: CCInvocation) -> dict[str, str]:
        """Last gate between a built env and the process that receives it.

        Exists because ``_build_env`` is NOT the final word: both spawn paths
        merge the login fallback on top of it. Anything that mutates the env
        after the builder goes through here, so the dict that was checked is
        the dict that launches.
        """
        if inv.bash_allowlist:
            _assert_hardening_present(env, tuple(inv.bash_allowlist))
        return env

    def _register_proc(self, key: str, proc: asyncio.subprocess.Process) -> None:
        """Register a live subprocess under a session key.

        Prunes dead entries first — a safety net so the registry only ever
        holds live procs even if a path fails to unregister. Assumes at most one
        live proc per key at a time: foreground keys (``tg:user:chat``) are
        serialized by the Telegram chat lock + the per-session conversation lock,
        and background keys (``pid:<pid>``) are unique. A live-proc clobber under
        the same key would orphan the prior proc — keep that invariant if the
        locking model changes.
        """
        for dead in [k for k, p in self._active_procs.items() if p.returncode is not None]:
            self._active_procs.pop(dead, None)
        self._active_procs[key] = proc

    def _unregister_proc(self, key: str) -> None:
        self._active_procs.pop(key, None)

    async def interrupt(self, key: str | None = None) -> None:
        """Send SIGINT to a session's subprocess. No-op if none match.

        With ``key``, targets that session's proc; without it, targets the
        most-recently-registered LIVE proc (back-compat). Concurrent sessions
        each register under their own key, so a Telegram `/stop` interrupts the
        user's session — not a background task that started later (cc-loop-01).
        """
        if key is not None:
            proc = self._active_procs.get(key)
        else:
            live = [p for p in self._active_procs.values() if p.returncode is None]
            proc = live[-1] if live else None
        if proc is not None and proc.returncode is None:
            proc.send_signal(signal.SIGINT)

    @staticmethod
    def _classify_error(stderr_text: str, stdout_text: str = "") -> CCError:
        """Classify CC output into a typed CC exception.

        Checks both stderr and stdout — when CC runs in streaming-JSON
        mode the rate-limit / quota signal often appears in stdout
        (inside the JSON stream's error event) while stderr is empty.
        Limiting classification to stderr would mis-categorize those as
        generic CCProcessError and skip downstream retry branches that
        key off the typed exception.
        """
        combined = f"{stderr_text}\n{stdout_text}"
        lower = combined.lower()
        # Session expiry
        if "session" in lower and ("not found" in lower or "expired" in lower):
            return CCSessionError(stderr_text or stdout_text)
        # Hard quota exhaustion (usage limit hit for hours — distinct from 429).
        # "session limit" / "weekly limit" are the Max-plan rolling-window and
        # weekly ceilings: real multi-minute-to-hours lockouts that reset at a
        # defined time, NOT transient 429s. The CLI wording varies ("You've hit
        # your session limit · resets 4:10am", "weekly limit reached", "usage
        # limit reached") — cover the family, not one instance. Before this,
        # "hit your session limit" matched NO pattern (the RATE list has "hit
        # your limit" but "session" splits the substring) and fell through to
        # generic CCProcessError, so the rate-limit park/resume layer never
        # engaged and background sessions died instead of parking (reflex signal
        # CCProcessError×cc, 2026-07/08).
        _QUOTA_PATTERNS = (
            "usage limit",
            "quota exceeded",
            "limit reached",
            "usage cap",
            "spending limit",
            "token limit exceeded",
            "session limit",
            "weekly limit",
        )
        if any(p in lower for p in _QUOTA_PATTERNS):
            return CCQuotaExhaustedError(stderr_text or stdout_text, raw_text=combined)
        # Transient rate limit (429, recovers in minutes)
        # CC CLI says "You've hit your limit · resets Xpm" — not "rate limit"
        _RATE_LIMIT_PATTERNS = (
            "rate limit",
            "rate_limit",
            "429",
            "hit your limit",
            "hit the limit",
        )
        if any(p in lower for p in _RATE_LIMIT_PATTERNS):
            return CCRateLimitError(stderr_text or stdout_text, raw_text=combined)
        # MCP server error
        source = stderr_text or stdout_text
        if "mcp" in lower or "mcp server" in lower:
            # Try to extract server name
            server_name = None
            for marker in ("server '", 'server "', "server: "):
                idx = source.lower().find(marker)
                if idx >= 0:
                    start = idx + len(marker)
                    end = source.find(
                        "'" if marker.endswith("'") else ('"' if marker.endswith('"') else " "),
                        start,
                    )
                    if end > start:
                        server_name = source[start:end]
                    break
            return CCMCPError(source, server_name=server_name)
        # Thinking block corruption (stale resume with extended thinking).
        # Semantically a session error — the session's thinking state is
        # incompatible with modification.  conversation.py already catches
        # CCError on resumes, so this only improves classification fidelity.
        if "thinking" in lower and "cannot be modified" in lower:
            return CCSessionError(source)
        # Generic process error
        return CCProcessError(source)

    async def _notify_status_change(self, error: CCError | None) -> None:
        """Notify callback about CC status changes.

        Protected by _status_lock to prevent concurrent invocations from
        producing spurious NORMAL signals during actual quota exhaustion.

        Args:
            error: The CC error, or None on recovery (success after failure).
        """
        if self._on_cc_status_change is None:
            return

        async with self._status_lock:
            if error is None:
                # Recovery
                self._last_was_error = False
                try:
                    await self._on_cc_status_change("NORMAL")
                except Exception:
                    logger.warning("CC status callback failed on recovery", exc_info=True)
                return

            self._last_was_error = True
            if isinstance(error, CCQuotaExhaustedError):
                status = "UNAVAILABLE"
            elif isinstance(error, CCRateLimitError):
                status = "RATE_LIMITED"
            else:
                # Other errors don't change CC status
                return

            try:
                await self._on_cc_status_change(status)
            except Exception:
                logger.warning("CC status callback failed for %s", status, exc_info=True)

    async def _network_preflight(self, invocation: CCInvocation) -> None:
        """Fail fast pre-spawn when the network is hard-OFFLINE and WAN-bound.

        PR-3 outage resilience. When the connectivity sentinel reports a fresh
        OFFLINE state and the parking lever is ``live``, a CC dispatch whose
        endpoint needs the internet raises :class:`CCNetworkOfflineError` here —
        *before* the subprocess is spawned — so it fails in well under a second
        instead of hanging up to ``timeout_s`` (7200s default; 45-55min hangs
        were observed in the 2026-07-28 outage).

        Fail-safe by construction: the lever off, an absent/stale/NORMAL/DEGRADED
        snapshot, a LAN endpoint, or any error in the check itself all fall
        through to a normal dispatch — identical to pre-sentinel behavior. Only
        the precise (fresh-OFFLINE + live + WAN endpoint) case parks.
        """
        # Function-scope imports: cc.invoker is imported very early in runtime
        # bootstrap; keep the resilience/util deps out of its module-load graph
        # (cycle-proof, same rationale as surplus/dispatch.py's scoped imports).
        try:
            from genesis.resilience import network_config
            from genesis.util import netclass

            decision = network_config.parking_decision()
            if decision in ("off", "normal"):
                return
            # Fresh OFFLINE (shadow or park). Only WAN endpoints are affected — a
            # LAN CC peer (native mesh) stays reachable through a WAN outage. The
            # native ``claude`` endpoint carries no base URL → WAN by rule, so a
            # true outage correctly parks all CC here (api.anthropic.com is down).
            endpoint_class = await netclass.default_classifier().classify_url_async(
                invocation.anthropic_base_url
            )
            if endpoint_class != netclass.WAN:
                return
            if decision == "shadow":
                logger.info(
                    "network preflight: WOULD park WAN CC dispatch "
                    "(network OFFLINE, shadow mode) — proceeding",
                )
                return
            # decision == "park" (live mode)
            raise CCNetworkOfflineError(
                "network is OFFLINE — skipping WAN CC dispatch before subprocess spawn",
            )
        except CCNetworkOfflineError:
            raise
        except Exception:
            logger.debug("network preflight check errored — proceeding", exc_info=True)

    async def run(self, invocation: CCInvocation) -> CCOutput:
        """Run a dispatched CC session (traced).

        Opens a ``cc.session`` span spanning the whole subprocess lifetime so
        (a) the active trace context is injected into the child env (see
        ``_build_env``) and the CC PostToolUse hook nests tool spans under it,
        and (b) any LLM/operation spans share one trace. Best-effort — a no-op
        when capture is disabled.
        """
        invocation, roster_model = roster.apply_active(invocation)
        await self._network_preflight(invocation)
        with start_span(
            "cc.session",
            SpanKind.CC_SESSION,
            attributes={
                "model": invocation.model,
                "roster_model": roster_model,
                "effort": invocation.effort,
                "streaming": False,
            },
        ) as span:
            output = replace(
                await self._run_inner(invocation),
                roster_model=roster_model,
            )
            with contextlib.suppress(Exception):
                span.set_attr("cost_usd", output.cost_usd)
                span.set_attr("input_tokens", output.input_tokens)
                span.set_attr("output_tokens", output.output_tokens)
                span.set_attr("model_used", output.model_used)
                if output.is_error:
                    span.set_status_error(output.error_message or "CC session error")
            return output

    async def _apply_login_fallback(
        self,
        env: dict[str, str],
        inv: CCInvocation | None,
    ) -> dict[str, str]:
        """Inject the stored 1-year setup-token ONLY when the interactive
        login is hard-expired AND a live probe confirms logged-out
        (login_health gates — never over a working login, never on
        ambiguity). Skipped entirely for cleanroom/bare invocations whose
        env_overrides manage their own auth (overrides win by contract).
        """
        if inv is not None and getattr(inv, "bare", False):
            return env
        # Peer-routed invocations (third-party ANTHROPIC_BASE_URL/AUTH_TOKEN)
        # never use the claude.ai login — injecting the Anthropic OAuth
        # setup-token there is useless at best and violates the roster's
        # credential-isolation contract (the Anthropic credential must never
        # travel toward a third-party endpoint).
        if inv and (
            getattr(inv, "anthropic_base_url", None) or getattr(inv, "anthropic_auth_token", None)
        ):
            return env
        if (
            inv
            and inv.env_overrides
            and (
                "CLAUDE_CODE_OAUTH_TOKEN" in inv.env_overrides
                or "CLAUDE_CONFIG_DIR" in inv.env_overrides
                or "ANTHROPIC_API_KEY" in inv.env_overrides
            )
        ):
            return env
        try:
            from genesis.cc.login_health import fallback_env_if_login_dead

            fb = await fallback_env_if_login_dead(cc_path=self._claude_path)
            if fb:
                return {**env, **fb}
        except Exception:
            logger.debug("login fallback check failed", exc_info=True)
        return env

    async def _run_inner(self, invocation: CCInvocation) -> CCOutput:
        args = self._build_args(invocation)
        # Off the event loop: settings read, seal write, two probes.
        await self.verify_allowlist_enforceable(invocation)
        env = self._build_env(invocation)
        env = await self._apply_login_fallback(env, invocation)
        env = self._launch_env(env, invocation)
        start = time.monotonic()

        # Extract dispatched effort from args — may differ from invocation.effort
        # if clamp_effort() clamped it, and is absent entirely for models that
        # don't use an effort setting (Haiku).
        dispatched_effort = args[args.index("--effort") + 1] if "--effort" in args else "n/a"

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model,
            dispatched_effort,
            invocation.timeout_s,
            prompt_preview,
        )

        proc = None
        reg_key: str | None = None
        try:
            scope_args = await _get_scope_args()
            proc = await asyncio.create_subprocess_exec(
                *scope_args,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=invocation.working_dir or self._working_dir,
                # Own session/group (setsid in the C helper — never preexec_fn:
                # post-fork Python can deadlock in the threaded server) so the
                # kill paths can killpg the whole claude tree.
                start_new_session=True,
            )
            reg_key = invocation.session_key or f"pid:{proc.pid}"
            self._register_proc(reg_key, proc)
            logger.info("CC subprocess spawned (PID %s)", proc.pid)
            set_oom_score_adj(proc.pid, 500)
            if invocation.on_spawn is not None:
                try:
                    await invocation.on_spawn(proc.pid)
                except Exception:
                    logger.warning(
                        "on_spawn callback failed for PID %s",
                        proc.pid,
                        exc_info=True,
                    )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=invocation.prompt.encode()),
                timeout=invocation.timeout_s,
            )
        except FileNotFoundError:
            logger.error(
                "Claude CLI not found at %r. Ensure @anthropic-ai/claude-code "
                "is installed via npm and ~/.npm-global/bin is on PATH.",
                self._claude_path,
            )
            raise CCProcessError(
                f"Claude CLI not found at '{self._claude_path}'. "
                f"Ensure @anthropic-ai/claude-code is installed via npm "
                f"and ~/.npm-global/bin is on PATH."
            ) from None
        except TimeoutError:
            elapsed_s = time.monotonic() - start
            if proc is None:
                # Spawn-time TimeoutError (ETIMEDOUT is an OSError subclass —
                # PEP 3151) misroutes here with nothing to kill.
                raise CCTimeoutError(
                    f"Timeout after {invocation.timeout_s}s (during spawn)"
                ) from None
            # Shared guarded group-kill: signals proc.pid AS the pgid (never
            # os.getpgid — it raises once the leader is reaped, leaking the
            # tree), pgid<=1 guard, direct-kill fallback; then a BOUNDED reap.
            kill_process_group(proc)
            await reap_bounded(proc)

            # Capture stderr for diagnostics (mirrors run_streaming pattern)
            stderr_text = ""
            if proc.stderr:
                try:
                    # Bounded: a setsid-escaping descendant holding stderr
                    # could otherwise stall this read past the bounded reap.
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), 5.0)
                    if isinstance(stderr_data, bytes):
                        stderr_text = stderr_data.decode(errors="replace")[:1000]
                except Exception:
                    pass

            logger.error(
                "CC session TIMEOUT after %.0fs (PID %s, limit=%ds)%s",
                elapsed_s,
                proc.pid,
                invocation.timeout_s,
                f" stderr: {stderr_text}" if stderr_text else "",
            )
            raise CCTimeoutError(f"Timeout after {invocation.timeout_s}s") from None
        finally:
            if reg_key is not None:
                self._unregister_proc(reg_key)
                # Don't leak a still-running proc on an abnormal exit (e.g. task
                # cancellation); communicate() reaps it on the normal path.
                if proc is not None and proc.returncode is None:
                    # Group-kill: a bare proc.kill() would orphan the claude
                    # tree's MCP/helper children on cancellation.
                    kill_process_group(proc)

        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "CC subprocess finished (PID %s, exit=%s, %.1fs)",
            proc.pid,
            proc.returncode,
            elapsed / 1000,
        )
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            stdout_text = stdout.decode(errors="replace").strip()
            logger.error(
                "CC subprocess failed (exit=%s): stderr=%s stdout=%s",
                proc.returncode,
                stderr_text[:500] or "(no stderr)",
                stdout_text[:500] or "(no stdout)",
            )
            err = self._classify_error(stderr_text, stdout_text)
            await self._notify_status_change(err)
            raise err

        output = self._parse_output(stdout.decode(errors="replace"), invocation, elapsed)
        output = await self._with_cost_semantics(output)
        if _stderr_bg_truncated(stderr.decode(errors="replace")):
            output = replace(output, bg_truncated=True)
            logger.warning(
                "CC background tasks truncated at wait ceiling (PID %s) — "
                "dispatched work SIGKILLed mid-run; result is partial",
                proc.pid,
            )
            await _emit_bg_truncation_event(output.session_id)
        if output.is_error:
            error_text = output.error_message or output.text or "CC error"
            err = self._classify_error(error_text)
            await self._notify_status_change(err)
            raise err

        # Success — notify recovery if we were previously in error state
        if self._last_was_error:
            await self._notify_status_change(None)
        await self._fire_downgrade_callback(output)
        # Silent-cap detection: a non-error, non-rate-limited return that reached
        # here with empty text (returncode==0 above rules out rate-limit text).
        if invocation.expect_output and not output.is_error and not output.text.strip():
            await self._fire_empty_output_callback(invocation, output)
        return output

    async def run_streaming(
        self,
        invocation: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> CCOutput:
        """Run CC with stream-json output (traced — see run() for span rationale)."""
        invocation, roster_model = roster.apply_active(invocation)
        await self._network_preflight(invocation)
        with start_span(
            "cc.session",
            SpanKind.CC_SESSION,
            attributes={
                "model": invocation.model,
                "roster_model": roster_model,
                "effort": invocation.effort,
                "streaming": True,
            },
        ) as span:
            output = replace(
                await self._run_streaming_inner(invocation, on_event),
                roster_model=roster_model,
            )
            with contextlib.suppress(Exception):
                span.set_attr("cost_usd", output.cost_usd)
                span.set_attr("input_tokens", output.input_tokens)
                span.set_attr("output_tokens", output.output_tokens)
                span.set_attr("model_used", output.model_used)
                if output.is_error:
                    span.set_status_error(output.error_message or "CC session error")
            return output

    async def _run_streaming_inner(
        self,
        invocation: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> CCOutput:
        """Run CC with stream-json output, calling on_event for each line."""
        args = self._build_args(invocation)
        # Off the event loop: settings read, seal write, two probes.
        await self.verify_allowlist_enforceable(invocation)
        # Override output format to stream-json (requires --verbose with -p).
        # Target the --output-format value by its flag, not a bare args.index("json")
        # scan — other args (e.g. --settings .../cc-span-settings.json) can contain
        # the substring "json", and keying off the flag is unambiguous.
        fmt_idx = args.index("--output-format") + 1
        args[fmt_idx] = "stream-json"
        args.insert(1, "--verbose")

        env = self._build_env(invocation)
        env = await self._apply_login_fallback(env, invocation)
        env = self._launch_env(env, invocation)
        start = time.monotonic()

        # Extract dispatched effort from args — may differ from invocation.effort
        # if clamp_effort() clamped it, and is absent entirely for models that
        # don't use an effort setting (Haiku).
        dispatched_effort = args[args.index("--effort") + 1] if "--effort" in args else "n/a"

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC streaming session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model,
            dispatched_effort,
            invocation.timeout_s,
            prompt_preview,
        )

        try:
            scope_args = await _get_scope_args()
            proc = await asyncio.create_subprocess_exec(
                *scope_args,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LINE_LIMIT,
                env=env,
                cwd=invocation.working_dir or self._working_dir,
                # Own session/group (setsid in the C helper — never preexec_fn:
                # post-fork Python can deadlock in the threaded server) so the
                # kill paths can killpg the whole claude tree.
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error(
                "Claude CLI not found at %r. Ensure @anthropic-ai/claude-code "
                "is installed via npm and ~/.npm-global/bin is on PATH.",
                self._claude_path,
            )
            raise CCProcessError(
                f"Claude CLI not found at '{self._claude_path}'. "
                f"Ensure @anthropic-ai/claude-code is installed via npm "
                f"and ~/.npm-global/bin is on PATH."
            ) from None
        logger.info("CC streaming subprocess spawned (PID %s)", proc.pid)
        set_oom_score_adj(proc.pid, 500)
        # cc-loop-01: register immediately (before the stdin feed) so the proc is
        # interruptible from spawn. If on_spawn or the stdin feed fails (broken
        # pipe, task cancellation), reap the proc and drop the registry entry —
        # don't leak either. The streaming `try` below unregisters on its paths.
        reg_key = invocation.session_key or f"pid:{proc.pid}"
        self._register_proc(reg_key, proc)
        try:
            if invocation.on_spawn is not None:
                try:
                    await invocation.on_spawn(proc.pid)
                except Exception:
                    logger.warning(
                        "on_spawn callback failed for PID %s",
                        proc.pid,
                        exc_info=True,
                    )
            # Feed prompt via stdin, then close to signal EOF
            if proc.stdin is not None:
                proc.stdin.write(invocation.prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
        except BaseException:
            self._unregister_proc(reg_key)
            kill_process_group(proc)
            raise

        result_data: dict | None = None
        collected_text: list[str] = []
        event_types: list[str] = []
        tools_seen: list[str] = []
        rate_limit_raw: dict | None = None
        timed_out = False
        terminated_after_result = False
        line_count = 0
        oversized_dropped = 0
        # Buffer overruns, which is NOT the same number. A physical line much
        # larger than the limit makes `readline()` raise once per buffer fill:
        # MEASURED on Python 3.12, one 20,000,000-byte line raised 18 times
        # before its newline arrived. `oversized_dropped` is the public
        # counter and must mean LINES — it reaches the caller as
        # `stream_lines_dropped` and drives the MCP projections and the
        # operator diagnostics, all of which would otherwise report 18 missing
        # events for one (Codex P2, PR #1625).
        oversized_overruns = 0
        # True while the reader is still discarding one over-limit physical
        # line: every raise after the first belongs to the SAME line.
        mid_oversized_line = False
        multi_block_seen = False

        try:
            async with asyncio.timeout(invocation.timeout_s):
                # Deliberately NOT `async for raw_line in proc.stdout`. That
                # protocol lets a single over-limit line abort the whole stream:
                # StreamReader.__anext__ -> readline() raises ValueError when one
                # line exceeds `limit` (1 MiB, set at spawn), and it propagated
                # out of the loop and killed the session. MEASURED 2026-09-02:
                # a browser session lost 104.4s of completed work to one
                # oversized MCP tool result.
                #
                # Recovery is safe by construction, not by hope: CPython's
                # StreamReader.readline() DELETES the consumed span (or clears
                # the buffer outright) BEFORE it raises, so re-entering the loop
                # cannot re-raise on the same bytes and cannot spin. Verified
                # against the stdlib source on Python 3.12. Progress is
                # guaranteed — each raising call consumes at least `limit`
                # bytes. The enclosing asyncio.timeout is a backstop ONLY
                # because the drop branch yields — see the sleep(0) below.
                while True:
                    try:
                        raw_line = await proc.stdout.readline()
                    except ValueError:
                        # Over-limit line. It is unusable either way (no JSON
                        # can be parsed from a truncated span), so the only
                        # question is whether losing it costs the LINE or the
                        # SESSION. Drop the line.
                        oversized_overruns += 1
                        if not mid_oversized_line:
                            # FIRST overrun of this physical line — the only
                            # one that represents a lost event.
                            oversized_dropped += 1
                            mid_oversized_line = True
                        # Yield explicitly: this branch has no other await, so
                        # without it a readline() that raises WITHOUT consuming
                        # would spin with the event loop locked out and
                        # asyncio.timeout could never fire. Progress against a
                        # real StreamReader is guaranteed by consumption, not by
                        # this — but the timeout is only a backstop if we yield.
                        await asyncio.sleep(0)
                        if oversized_overruns <= 3 or oversized_overruns % 25 == 0:
                            logger.warning(
                                "CC stream line exceeded the %d-byte limit and was "
                                "DROPPED (PID %s, lines=%d, buffer overruns=%d) — a "
                                "tool result was almost certainly too large; the "
                                "session continues",
                                _STREAM_LINE_LIMIT,
                                proc.pid,
                                oversized_dropped,
                                oversized_overruns,
                            )
                        continue
                    if not raw_line:
                        break  # EOF
                    # A successful read means the over-limit line finally
                    # ended (this is its unusable tail, which fails to parse
                    # below like any other garbage). The NEXT raise starts a
                    # new line and counts again.
                    mid_oversized_line = False
                    line = raw_line.decode(errors="replace").strip()
                    if not line:
                        continue
                    line_count += 1
                    try:
                        event_raw = json.loads(line)
                    except json.JSONDecodeError:
                        if oversized_dropped:
                            # This is almost certainly the TAIL of the line we
                            # just dropped, not a new record. MEASURED against
                            # CPython 3.12 `StreamReader.readline`: on a limit
                            # overrun it deletes through the separator only when
                            # the separator is ALREADY BUFFERED
                            # (`asyncio/streams.py`, the LimitOverrunError arm);
                            # when the newline has not arrived yet it clears the
                            # buffer and the REMAINDER of that physical line
                            # comes back from the next call. A 64-byte-limit
                            # probe returned the tail verbatim as the following
                            # "line".
                            #
                            # So its bytes are raw tool output — which may carry
                            # a credential or personal data, and which this
                            # repo's logs feed into health snapshots and LLM
                            # prompts elsewhere. It has no diagnostic value as
                            # content (a mid-JSON span never parses), so state
                            # the SIZE and withhold the bytes rather than
                            # printing 200 characters of somebody's tool result.
                            # Withheld rather than dropped silently: the count
                            # is what tells you the stream is resynchronising.
                            #
                            # Gated on the drop, not applied unconditionally: an
                            # ordinary non-JSON line on a clean stream is a CLI
                            # protocol fault, and there its text is the whole
                            # diagnostic.
                            logger.warning(
                                "CC stream non-JSON line after %d dropped "
                                "over-limit line(s) — content withheld "
                                "<%d chars, presumed tail of a dropped line>",
                                oversized_dropped,
                                len(line),
                            )
                            continue
                        logger.warning("CC stream non-JSON line: %s", line[:200])
                        continue

                    etype = event_raw.get("type", "?")
                    event_types.append(etype)
                    if etype == "rate_limit_event":
                        # Retain the raw payload (otherwise discarded) so the
                        # durability layer can parse a reset time off it.
                        rate_limit_raw = event_raw
                    logger.debug("CC stream event #%d: type=%s", line_count, etype)

                    # `StreamEvent.from_raw` keeps only the FIRST recognized
                    # content block, which is lossless only while CC emits one
                    # block per line. MEASURED 2026-09-04 against CC 2.1.246 on
                    # this exact surface (`claude -p --output-format stream-json
                    # --verbose`, two probes): 8/8 assistant lines carried
                    # exactly one block, 0 multi-block — including a
                    # thinking→text→tool_use turn and three PARALLEL tool calls,
                    # which the API packs into a single message and the CLI split
                    # across three lines. The version matters: this is a property
                    # of a CLI build, not of the protocol.
                    #
                    # So this RECORDS the assumption rather than defending
                    # against it — it is a log line, and nothing polls it. It
                    # earns its keep by making a future batching CC diagnosable
                    # in one grep instead of a week of missing tool calls.
                    # Invocation-scoped on purpose: cross-invocation de-dup
                    # belongs to the log aggregator, and one line per affected
                    # turn is what tells you WHICH turns were lossy.
                    if etype == "assistant" and not multi_block_seen:
                        n_blocks = StreamEvent.recognized_blocks(event_raw)
                        if n_blocks > 1:
                            multi_block_seen = True
                            logger.warning(
                                "CC assistant line carried %d recognized content "
                                "blocks — StreamEvent.from_raw keeps only the "
                                "first; tool calls and answer text may be dropped",
                                n_blocks,
                            )

                    event = StreamEvent.from_raw(event_raw)

                    # Log CC version from init event (pure observability)
                    if etype == "system" and event_raw.get("subtype") == "init":
                        cc_version = event_raw.get("version", "unknown")
                        logger.info("CC version: %s", cc_version)

                    if event.event_type == "text" and event.text:
                        collected_text.append(event.text)
                    if etype == "assistant":
                        # Read off the RAW content array, not the parsed
                        # StreamEvent: `from_raw` returns ONE event per message
                        # and stops at the first recognised block, so a message
                        # shaped `thinking + text + tool_use` parses as "text"
                        # and drops the tool name entirely. MEASURED on this
                        # install's own transcripts: 13 of 8655 assistant
                        # messages carrying a tool_use (0.15%) have it in a
                        # non-first position. Rare, but the failure is
                        # asymmetric — if OTHER tools were captured the list is
                        # marked runtime-sourced and rendered as authoritative
                        # while silently incomplete, which is the exact grammar
                        # this change exists to remove.
                        for block in event_raw.get("message", {}).get("content", []) or []:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            name = block.get("name")
                            # First-seen order, deduplicated — the same shape
                            # the text-scraping fallback produces.
                            if name and name not in tools_seen:
                                tools_seen.append(name)
                    if event.event_type == "result":
                        result_data = event_raw
                        result_text = event_raw.get("result", "")
                        logger.info(
                            "CC stream result: is_error=%s, result_len=%d, result_preview=%r",
                            event_raw.get("is_error"),
                            len(result_text or ""),
                            (result_text or "")[:200],
                        )
                        # First result is authoritative.  Terminate the
                        # subprocess to prevent stale task_notification events
                        # from triggering a second CC turn (which would
                        # overwrite the real answer with a throwaway response).
                        proc.terminate()
                        terminated_after_result = True
                        if on_event:
                            await on_event(event)
                        break

                    if on_event:
                        await on_event(event)
        except TimeoutError:
            timed_out = True
            logger.error(
                "CC streaming TIMEOUT after %.0fs (PID %s)",
                time.monotonic() - start,
                proc.pid,
            )
            kill_process_group(proc)
        except asyncio.CancelledError:
            # Task cancellation (runtime shutdown cancelling an in-flight
            # session) must not leak the CC child: without this handler the
            # finally below only unregistered the proc, leaving the subprocess
            # running — spending tokens and editing files — after the session
            # row was already finalized. Same guarded group-kill as the
            # timeout path; the non-streaming run() already kills in its
            # finally for exactly this case.
            logger.warning(
                "CC streaming cancelled (PID %s) — killing subprocess",
                proc.pid,
            )
            kill_process_group(proc)
            raise
        except BaseException:
            # Any other failure mid-stream (an on_event callback raising, an
            # over-limit stream-json line) must not leak the live, now-
            # unregistered tree — it would run detached where even /stop
            # can't reach it, then wedge when its unread stdout pipe fills.
            kill_process_group(proc)
            raise
        finally:
            self._unregister_proc(reg_key)

        # Bounded reap: proc.terminate() on the result path is a GRACEFUL stop
        # the child can ignore (wedged node flush / MCP teardown) — an
        # unbounded wait here would hang the dispatch AFTER the result was
        # already obtained. Bound it; on expiry escalate to the group kill.
        await reap_bounded(proc)
        # Escalate on GROUP liveness, not the leader's returncode: after the
        # graceful terminate the leader can exit (returncode set) while an
        # MCP/helper child survives in the group — gating on returncode alone
        # would leak that descendant while we report completion.
        if process_group_alive(proc):
            # The leader-only reap gives descendants no grace of their own —
            # an MCP child mid-flush would be SIGKILLed instantly. Grant a
            # short beat (stdio children normally exit sub-second on parent
            # death); costs latency only when a survivor actually exists.
            await asyncio.sleep(_ESCALATION_GRACE_S)
        if proc.returncode is None or process_group_alive(proc):
            logger.warning(
                "CC streaming group survived graceful stop/kill (PID %s, rc=%s) — group-killing",
                proc.pid,
                proc.returncode,
            )
            kill_process_group(proc)
            await reap_bounded(proc)
        elapsed = int((time.monotonic() - start) * 1000)

        # Read stderr for diagnostics
        stderr_data = b""
        if proc.stderr:
            with contextlib.suppress(TimeoutError):
                # Bounded for the same reason as the reap above.
                stderr_data = await asyncio.wait_for(proc.stderr.read(), 5.0)
        stderr_str = stderr_data.decode(errors="replace") if stderr_data else ""
        if stderr_str:
            logger.warning("CC stderr: %s", stderr_str[:500])
        bg_truncated = _stderr_bg_truncated(stderr_str)
        if bg_truncated:
            logger.warning(
                "CC background tasks truncated at wait ceiling (PID %s) — "
                "dispatched work SIGKILLed mid-run; result is partial",
                proc.pid,
            )

        logger.info(
            "CC streaming finished (PID %s, exit=%s, lines=%d, dropped_oversized=%d, "
            "has_result=%s, terminated=%s, %.1fs)",
            proc.pid,
            proc.returncode,
            line_count,
            oversized_dropped,
            result_data is not None,
            terminated_after_result,
            elapsed / 1000,
        )
        if oversized_dropped:
            # Loud at the boundary: a dropped line is invisible degradation, and
            # a session that dropped lines AND produced no result deserves the
            # cause named rather than a bare "empty output".
            logger.warning(
                "CC stream dropped %d over-limit line(s) (PID %s, limit=%d bytes)%s",
                oversized_dropped,
                proc.pid,
                _STREAM_LINE_LIMIT,
                "" if result_data is not None else " — and NO result event arrived",
            )
        if event_types:
            logger.info("CC stream events: %s", " → ".join(event_types))

        if timed_out:
            partial = "".join(collected_text)
            # A drop OUTRANKS the timeout, for the same reason it outranks the
            # two error branches below: the question a caller asks of the type
            # is "may I re-run this?", and once a line was dropped the answer is
            # no, whatever else also went wrong.
            #
            # An earlier revision of this comment claimed the opposite — that no
            # catch site replays a timeout, "both re-raise tuples in
            # cc/conversation.py carry CCTimeoutError". That was FALSE, and
            # false in the specific way this file keeps having to relearn: there
            # are THREE re-raise sites, not two. `_try_invoke` and
            # `_try_invoke_streaming` do carry CCTimeoutError;
            # `_run_failover_peer` (conversation.py:1114-1119) does NOT, so a
            # timeout there falls to the generic `except CCError` in
            # `_try_roster_failover` and advances to the next peer — replaying
            # the prompt with full tools after the first peer already ran its
            # own. Enumerating two of three members of a set and writing "both"
            # is how a safety claim gets shipped without being checked.
            if oversized_dropped:
                raise _unreplayable_after_drop(
                    oversized_dropped,
                    f"the stream then timed out after {invocation.timeout_s}s"
                    + (f" (partial: {len(partial)} chars)" if partial else ""),
                )
            raise CCTimeoutError(
                f"Timeout after {invocation.timeout_s}s"
                + (f" (partial: {len(partial)} chars)" if partial else ""),
            )

        if result_data is not None:
            output = self._parse_result_dict(result_data, invocation, elapsed)
            output = await self._with_cost_semantics(output)
            # () is a real report ("the runtime watched and saw no tool_use"),
            # distinct from None ("nothing watched"). A `if tools_seen:` guard
            # here would silently downgrade the former to the latter on every
            # tool-free streaming turn.
            #
            # But a DROPPED event means the watching was incomplete, and a
            # partial inventory presented as a complete one is worse than no
            # inventory: triage reads a non-None tuple as authoritative
            # (`learning/triage/summarizer.py:203` sets
            # `tool_calls_from_runtime`), so if the dropped event was the only
            # tool request, graders are told the runtime observed NO tools —
            # a false fact entering permanent learning. None is the honest
            # value, and it already means exactly this; triage then falls back
            # to extracting from the text, as it does for every non-streaming
            # turn (Codex P2, PR #1625).
            output = replace(
                output,
                tools_used=None if oversized_dropped else tuple(tools_seen),
            )
            # When CC uses extended thinking, the result field can be empty
            # but the actual response was emitted as text events during streaming
            if not output.text and collected_text:
                output = replace(output, text="".join(collected_text))
            if oversized_dropped:
                # Stamp it BEFORE the branches below, so every exit of this
                # block carries it — including the rate-limit-with-text branch,
                # which RETURNS a perfectly good answer off an event stream we
                # nonetheless read incompletely. A consumer that derives an
                # inventory from the events it saw (direct_session's tool
                # telemetry, and through it the protected-path auditor) has no
                # other way to know its inventory has a hole in it.
                output = replace(output, stream_lines_dropped=oversized_dropped)
            if bg_truncated:
                output = replace(output, bg_truncated=True)
                await _emit_bg_truncation_event(output.session_id)
            # The two branches below both raise a RETRYABLE error, and on a
            # dropped stream neither may. `_recover_stale_resume`
            # (cc/conversation.py) reruns the prompt from scratch on an ordinary
            # CCError, and a rate-limit error additionally sends the turn to
            # roster failover — a second full-tools invocation. Either replays
            # the side effects behind the dropped line, which is exactly what
            # CCStreamTruncatedError exists to stop; before drop-and-continue an
            # over-limit line aborted the read with a bare ValueError, so these
            # shapes never reached a retry path at all. So the classification
            # still happens — the provider's own status signal is real evidence
            # and drives scheduling back-off — but the exception that LEAVES here
            # is the no-retry type, chained so the diagnosis survives.
            #
            # Only these two. The rate-limit-WITH-text branch RETURNS rather than
            # raising, so it permits no replay, and a drop that cost nothing but
            # a trace line is still an honest success.
            if output.is_error:
                stderr_hint = stderr_data.decode(errors="replace") if stderr_data else ""
                error_text = output.error_message or output.text or stderr_hint or "CC error"
                err = self._classify_error(error_text)
                await self._notify_status_change(err)
                if oversized_dropped:
                    raise _unreplayable_after_drop(
                        oversized_dropped, "CC reported an error result"
                    ) from err
                raise err

            # CC may return is_error=false but emit rate_limit_event in
            # the stream.  Update rate-limited status for awareness/scheduling
            # but still deliver the response if it has content — throwing away
            # a valid answer just because the API signaled rate pressure wastes
            # work and forces a contingency fallback the user didn't need.
            if "rate_limit_event" in event_types:
                if output.text and output.text.strip():
                    # Valid response despite rate limit signal — deliver it
                    # but mark CC as rate-limited so scheduling can back off.
                    logger.info(
                        "CC rate-limited but response has content (%d chars) — delivering",
                        len(output.text),
                    )
                    err = CCRateLimitError(
                        "CC rate limited (stream event)", raw_event=rate_limit_raw
                    )
                    await self._notify_status_change(err)
                    await self._fire_downgrade_callback(output)
                    return output
                # Empty/no response — rate limit prevented a real answer
                err = CCRateLimitError(
                    output.text or "CC rate limited (stream event)",
                    raw_event=rate_limit_raw,
                )
                await self._notify_status_change(err)
                if oversized_dropped:
                    # A rate-limit error is the one that reaches roster failover,
                    # so this is the branch where a replay costs a SECOND live
                    # peer running the same prompt with full tools.
                    raise _unreplayable_after_drop(
                        oversized_dropped,
                        "the result carried no text under a rate-limit event",
                    ) from err
                raise err

            # A result arrived, but a dropped line left it with NO text — so the
            # dropped line WAS the answer. `_parse_result_dict` already recovers
            # an extended-thinking response from the collected text events just
            # above, which is the only benign reason a result is textless; past
            # that, empty-after-a-drop means the answer is gone.
            #
            # Both other options are wrong here. Returning it as success records
            # a phantom completion (`success = not output.is_error` in
            # cc/direct_session.py), and on the home model that calls
            # note_home_recovery() — clearing an account-wide fallback on a run
            # that produced nothing. Firing the empty-output callback instead
            # forges the silent-subscription-cap signature that cc_relay.py
            # escalates, naming a cause that is not the cause. Raise.
            # NOT gated on `expect_output`. That flag defaults False and is set
            # by 13 cognitive callers; `cc/direct_session.py` and
            # `cc/conversation.py` — the two this comment names, and the path the
            # motivating incident came from — never set it. Gating here made the
            # guard inert on exactly the callers it was written for. The harm is
            # not "the caller wanted output and got none", it is "an answer was
            # produced and we lost it", which is a failure either way.
            if oversized_dropped and not output.text.strip():
                raise _unreplayable_after_drop(
                    oversized_dropped,
                    "the result carried no text — the answer was almost certainly one of them",
                )

            # Success — notify recovery if previously errored
            if self._last_was_error:
                await self._notify_status_change(None)
            await self._fire_downgrade_callback(output)
            # Silent-cap detection: reaching here means is_error=False AND no
            # rate_limit_event (both handled above) — empty text is the signal.
            if (
                invocation.expect_output
                and not output.is_error
                and not output.text.strip()
                # A dropped over-limit line is a KNOWN cause of thin output.
                # Reporting it as the unexplained-empty signature would forge a
                # silent-subscription-cap alert naming a cause that is not the
                # cause (runtime/init/cc_relay.py aggregates these).
                and not oversized_dropped
            ):
                await self._fire_empty_output_callback(invocation, output)
            return output

        # A dropped line cost us the RESULT, not just a trace line. This must
        # NEVER return as success: `success = not output.is_error`
        # (cc/direct_session.py) would record a phantom completion, and on the
        # home model that path calls note_home_recovery(), CLEARING an
        # account-wide rate-limit fallback on the strength of a run that
        # produced nothing. The empty-text shape also forges the silent
        # subscription-cap signature that runtime/init/cc_relay.py turns into a
        # CRITICAL infrastructure alert — an alert naming a cause that is not
        # the cause. Before the drop-and-continue loop this case raised; keep it
        # raising, because a loud wrong answer beats a quiet false one.
        # ...UNLESS the missing result is already explained. A background run
        # SIGKILLed at the CLI's wait ceiling legitimately produces no result
        # event, and the no-result fallback below returns what it collected with
        # bg_truncated=True — a supported shape with its own truncation notice.
        # Inferring "the result line was dropped" when bg_truncated and real
        # text both say otherwise throws away a usable partial deliverable.
        # Only claim the drop ate the result when nothing else accounts for it.
        # `collected_text` is a list of raw text blocks and a whitespace-only
        # block is truthy, so testing the LIST would exempt a run whose only
        # "deliverable" is blank — which then reaches the empty-text cap
        # detector below and forges the very alert this PR is careful not to
        # forge. Join and strip: the question is whether real text survived.
        partial_text = "".join(collected_text).strip()
        if oversized_dropped and result_data is None and not (bg_truncated and partial_text):
            raise _unreplayable_after_drop(
                oversized_dropped,
                "NO result event arrived — the result line was almost certainly one of them",
            )

        # No result event — treat collected text as response (success path)
        if self._last_was_error:
            await self._notify_status_change(None)
        output = CCOutput(
            session_id="",
            text="".join(collected_text),
            model_used=str(invocation.model),
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=elapsed,
            exit_code=proc.returncode or 0,
            model_requested=str(invocation.model),
            via_proxy=bool(invocation.anthropic_base_url),
            bg_truncated=bg_truncated,
            tools_used=tuple(tools_seen),
            stream_lines_dropped=oversized_dropped,
        )
        if bg_truncated:
            await _emit_bg_truncation_event(output.session_id)
        await self._fire_downgrade_callback(output)
        # Silent-cap detection (no-result path): also guard on rate_limit_event,
        # since the rate-limit branch above runs only inside the result_data block.
        if (
            invocation.expect_output
            and not output.is_error
            and not output.text.strip()
            and "rate_limit_event" not in event_types
            # A drop CAN reach here now: the bg-truncation exemption above lets a
            # dropped run through when real partial text survived. That is why
            # the exemption strips before testing — text that survives is text
            # this branch will find, so it cannot be empty AND dropped. An
            # earlier revision of this comment claimed drops were impossible
            # here, which the exemption had just made false.
            and not oversized_dropped
        ):
            await self._fire_empty_output_callback(invocation, output)
        return output

    @staticmethod
    def _detect_downgrade(requested: CCModel, actual_model_name: str) -> bool:
        """Return True if the actual model is a lower tier than requested.

        Tier ordering: OPUS > SONNET > HAIKU.
        Unknown model name → False (fail open, never block).
        """
        actual_tier = CCModel.from_full_name(actual_model_name)
        if actual_tier is None:
            return False
        return CCInvoker._TIER_RANK.get(actual_tier, 0) < CCInvoker._TIER_RANK.get(requested, 0)

    def _parse_result_dict(
        self,
        result_data: dict,
        inv: CCInvocation,
        elapsed_ms: int,
    ) -> CCOutput:
        """Build CCOutput from a parsed result dict."""
        usage = result_data.get("usage", {})
        model_usage = result_data.get("modelUsage", {})
        # modelUsage lists EVERY model the session touched, including CC's
        # auxiliary haiku calls (title/topic generation) — and dict order is
        # not tier order. Taking the first key false-positived downgrade
        # detection whenever an auxiliary call was listed before the main
        # model (observed 2026-07-09: {haiku, sonnet-5} on a sonnet session).
        # The MAIN conversation model is the highest tier present.
        model_name = (
            max(
                model_usage,
                key=lambda name: self._TIER_RANK.get(
                    CCModel.from_full_name(name),
                    -1,
                ),
            )
            if model_usage
            else str(inv.model)
        )
        # `cost_is_cumulative` is NOT decided here: the result carries no signal
        # for it (see _CC_CUMULATIVE_COST_SINCE), so the run paths stamp it from
        # the CC version via _with_cost_semantics.
        downgraded = self._detect_downgrade(inv.model, model_name)
        if downgraded:
            logger.warning(
                "MODEL DOWNGRADE DETECTED: requested=%s actual=%s",
                inv.model,
                model_name,
            )
        return CCOutput(
            session_id=result_data.get("session_id", ""),
            text=result_data.get("result", ""),
            model_used=model_name,
            cost_usd=result_data.get("total_cost_usd", 0.0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            duration_ms=result_data.get("duration_ms", elapsed_ms),
            exit_code=0,
            is_error=result_data.get("is_error", False),
            model_requested=str(inv.model),
            downgraded=downgraded,
            via_proxy=bool(inv.anthropic_base_url),
        )

    def _parse_output(self, raw: str, inv: CCInvocation, elapsed_ms: int) -> CCOutput:
        """Parse JSON output from claude -p CLI.

        Looks for the last JSON line with type=result. Falls back to treating
        entire stdout as plain text if no JSON found.

        Real CLI JSON shape (verified 2026-03-08):
        {
            "type": "result", "subtype": "success", "is_error": false,
            "result": "response text",
            "session_id": "uuid",
            "total_cost_usd": 0.186,
            "duration_ms": 2426,
            "usage": {"input_tokens": 3, "output_tokens": 5, ...},
            "modelUsage": {"claude-opus-4-6": {...}},
            ...
        }
        """
        result_data = None
        for line in reversed(raw.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("type") == "result":
                    result_data = parsed
                    break
            except json.JSONDecodeError:
                continue

        if result_data is not None:
            return self._parse_result_dict(result_data, inv, elapsed_ms)

        # Fallback: no structured output found, treat as plain text.
        # This likely means CC's output schema changed — log for diagnosis.
        first_line = raw.strip().split("\n", 1)[0][:200] if raw.strip() else "(empty)"
        logger.warning(
            "CC output has no JSON result line — falling back to plain text. "
            "First line: %s (total %d chars)",
            first_line,
            len(raw),
        )
        return CCOutput(
            session_id="",
            text=raw.strip(),
            model_used=str(inv.model),
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=elapsed_ms,
            exit_code=0,
            model_requested=str(inv.model),
            via_proxy=bool(inv.anthropic_base_url),
        )
