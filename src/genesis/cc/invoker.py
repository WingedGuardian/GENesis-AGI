"""CCInvoker — async subprocess wrapper for claude -p CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from genesis.cc import roster
from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCSessionError,
    CCTimeoutError,
)
from genesis.cc.types import CCInvocation, CCModel, CCOutput, StreamEvent, clamp_effort
from genesis.observability.spans import SpanKind, start_span

logger = logging.getLogger(__name__)


def set_oom_score_adj(pid: int, score: int = 500) -> None:
    """Set OOM score adjustment for a process.

    Higher scores make the process more likely to be OOM-killed.
    CC subprocesses get +500 so the kernel kills them before genesis-server
    (-500) or qdrant. This is the container-side complement to the
    host VM's cgroup OOM scoring.
    """
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text(str(score))
        logger.debug("Set oom_score_adj=%d for PID %d", score, pid)
    except OSError as exc:
        logger.warning("Could not set oom_score_adj for PID %d: %s", pid, exc)


def _build_scope_args() -> list[str]:
    """Build systemd-run prefix for CC subprocess I/O isolation.

    Wraps the CC subprocess in a transient systemd scope with resource
    limits.  Each session gets its own cgroup under app.slice/run-XXXX.scope,
    separate from genesis-server.service.

    Returns an empty list if systemd-run is unavailable (graceful degradation).
    """
    if not shutil.which("systemd-run"):
        return []
    return [
        "systemd-run", "--user", "--scope", "--quiet",
        "-p", "IOWeight=100",
        "-p", "MemoryHigh=22G",
        "-p", "MemoryMax=27G",
        "--",
    ]


# Cache the scope args — they don't change during a process's lifetime.
_SCOPE_ARGS: list[str] | None = None


def _get_scope_args() -> list[str]:
    global _SCOPE_ARGS  # noqa: PLW0603
    if _SCOPE_ARGS is None:
        _SCOPE_ARGS = _build_scope_args()
    return _SCOPE_ARGS


# A minimal, runtime-generated CC settings file that registers ONLY the span
# PostToolUse hook. Lives outside the repo so it is install-local and never
# committed; regenerated idempotently (see cc_span_settings_path).
_CC_SPAN_SETTINGS_PATH = Path.home() / ".genesis" / "cc-span-settings.json"


def cc_span_settings_path() -> str | None:
    """Generate (idempotently) a minimal CC settings file that registers ONLY
    the span PostToolUse hook, and return its absolute path — or ``None`` if the
    launcher is unavailable.

    Why this exists: dispatched CC sessions run with a working directory outside
    any git repo (``~/.genesis/background-sessions``), and Claude Code discovers
    project ``.claude/settings.json`` via git-root detection — so the repo-level
    hook registration never loads there and ``cc_span_hook`` never fires. Passing
    this file via ``--settings`` injects JUST that hook; CC merges it with the
    user's settings, leaving every other hook untouched. The hook itself no-ops
    unless ``GENESIS_TRACE_ID`` is set, so attaching it to every dispatch is safe
    (and is why this is the *single* registration — the repo-level one was
    removed to avoid a double-fire when a dispatch runs in a worktree cwd, which
    *does* load repo settings).

    The hook command uses an ABSOLUTE path to the ``genesis-hook`` launcher,
    which self-locates the install root from its own filesystem position — NOT
    ``${CLAUDE_PROJECT_DIR}``, which CC leaves unset in dispatched sessions.
    Written atomically and only when stale, so it tracks the install root across
    updates with no bootstrap-ordering dependency and no per-dispatch churn.
    """
    from genesis import env

    genesis_hook = env.repo_root() / ".claude" / "hooks" / "genesis-hook"
    if not genesis_hook.exists():
        return None

    desired = json.dumps(
        {
            "hooks": {
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
            "Could not write CC span settings file at %s", path, exc_info=True,
        )
        return None


class CCInvoker:
    """Invokes claude CLI as async subprocess."""

    _TIER_RANK = {CCModel.HAIKU: 0, CCModel.SONNET: 1, CCModel.OPUS: 2}

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        working_dir: str | None = None,
        on_cc_status_change: (
            Callable[[str], Awaitable[None]] | None
        ) = None,
        on_model_downgrade: (
            Callable[[str, str, str], Awaitable[None]] | None
        ) = None,
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
        self._last_was_error = False
        self._status_lock = asyncio.Lock()
        self._protected_paths = protected_paths

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
                output.model_requested, output.model_used, output.session_id,
            )
        except Exception:
            logger.warning("Model downgrade callback failed", exc_info=True)

    def _build_args(self, inv: CCInvocation) -> list[str]:
        args = [self._claude_path, "-p"]
        # Roster routing: when model_id_override is set, model selection comes
        # entirely from ANTHROPIC_MODEL (set in _build_env). A --model flag here
        # would override that env var (CLI wins) and force the Anthropic tier
        # instead of the roster model — so omit it in that case.
        if inv.model_id_override is None:
            args += ["--model", str(inv.model)]
        args += ["--output-format", inv.output_format]
        effort = clamp_effort(inv.model, inv.effort)
        if effort != inv.effort:
            logger.warning(
                "Effort %r exceeds max for model %r — clamping to %r",
                str(inv.effort), str(inv.model), str(effort),
            )
        args += ["--effort", str(effort)]
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
        # Register the span-capture PostToolUse hook for this dispatched session.
        # Dispatched sessions run with a cwd outside any git repo, so CC never
        # loads the repo's .claude/settings.json; --settings injects just this
        # hook (CC merges it with the user's settings). No-op unless a trace is
        # active (GENESIS_TRACE_ID). See cc_span_settings_path.
        span_settings = cc_span_settings_path()
        if span_settings:
            args += ["--settings", span_settings]
        if inv.skip_permissions:
            args.append("--dangerously-skip-permissions")
        if inv.allowed_tools:
            args += ["--allowedTools", ",".join(inv.allowed_tools)]
        if inv.disallowed_tools:
            args += ["--disallowedTools", ",".join(inv.disallowed_tools)]
        if inv.bare:
            args.append("--bare")
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
        # Prevent CC's alt-screen renderer from corrupting terminal scrollback
        # in Linux/tmux.  No-op on CC <2.1.132; required post-migration.
        env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
        # Restrict Bash to an allowlist of command binaries for scoped profiles
        # (e.g. "steward" → gh only). scripts/bash_safety_hook.sh reads this and
        # blocks any non-allowlisted command. Absent → no restriction (the var
        # must not leak from the parent, so pop when the field is empty).
        if inv and inv.bash_allowlist:
            env["GENESIS_BASH_ALLOWLIST"] = ",".join(inv.bash_allowlist)
        else:
            env.pop("GENESIS_BASH_ALLOWLIST", None)
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
        if ("session" in lower and ("not found" in lower or "expired" in lower)):
            return CCSessionError(stderr_text or stdout_text)
        # Hard quota exhaustion (usage limit hit for hours — distinct from 429)
        _QUOTA_PATTERNS = (
            "usage limit", "quota exceeded", "limit reached",
            "usage cap", "spending limit", "token limit exceeded",
        )
        if any(p in lower for p in _QUOTA_PATTERNS):
            return CCQuotaExhaustedError(stderr_text or stdout_text)
        # Transient rate limit (429, recovers in minutes)
        # CC CLI says "You've hit your limit · resets Xpm" — not "rate limit"
        _RATE_LIMIT_PATTERNS = (
            "rate limit", "rate_limit", "429",
            "hit your limit", "hit the limit",
        )
        if any(p in lower for p in _RATE_LIMIT_PATTERNS):
            return CCRateLimitError(stderr_text or stdout_text)
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

    async def run(self, invocation: CCInvocation) -> CCOutput:
        """Run a dispatched CC session (traced).

        Opens a ``cc.session`` span spanning the whole subprocess lifetime so
        (a) the active trace context is injected into the child env (see
        ``_build_env``) and the CC PostToolUse hook nests tool spans under it,
        and (b) any LLM/operation spans share one trace. Best-effort — a no-op
        when capture is disabled.
        """
        invocation, roster_model = roster.apply_active(invocation)
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
                await self._run_inner(invocation), roster_model=roster_model,
            )
            with contextlib.suppress(Exception):
                span.set_attr("cost_usd", output.cost_usd)
                span.set_attr("input_tokens", output.input_tokens)
                span.set_attr("output_tokens", output.output_tokens)
                span.set_attr("model_used", output.model_used)
                if output.is_error:
                    span.set_status_error(output.error_message or "CC session error")
            return output

    async def _run_inner(self, invocation: CCInvocation) -> CCOutput:
        args = self._build_args(invocation)
        env = self._build_env(invocation)
        start = time.monotonic()

        # Extract dispatched effort from args — may differ from invocation.effort
        # if clamp_effort() clamped it for a non-Opus model.
        effort_idx = args.index("--effort") + 1
        dispatched_effort = args[effort_idx]

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model, dispatched_effort, invocation.timeout_s,
            prompt_preview,
        )

        proc = None
        reg_key: str | None = None
        try:
            scope_args = _get_scope_args()
            proc = await asyncio.create_subprocess_exec(
                *scope_args, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=invocation.working_dir or self._working_dir,
                preexec_fn=os.setpgrp,
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
                        proc.pid, exc_info=True,
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
            try:
                pgid = os.getpgid(proc.pid)
                if pgid <= 1:
                    raise ValueError(f"Refusing killpg with pgid={pgid}")
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError, TypeError):
                proc.kill()
            await proc.wait()

            # Capture stderr for diagnostics (mirrors run_streaming pattern)
            stderr_text = ""
            if proc.stderr:
                try:
                    stderr_data = await proc.stderr.read()
                    if isinstance(stderr_data, bytes):
                        stderr_text = stderr_data.decode(errors="replace")[:1000]
                except Exception:
                    pass

            logger.error(
                "CC session TIMEOUT after %.0fs (PID %s, limit=%ds)%s",
                elapsed_s, proc.pid, invocation.timeout_s,
                f" stderr: {stderr_text}" if stderr_text else "",
            )
            raise CCTimeoutError(f"Timeout after {invocation.timeout_s}s") from None
        finally:
            if reg_key is not None:
                self._unregister_proc(reg_key)
                # Don't leak a still-running proc on an abnormal exit (e.g. task
                # cancellation); communicate() reaps it on the normal path.
                if proc is not None and proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()

        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "CC subprocess finished (PID %s, exit=%s, %.1fs)",
            proc.pid, proc.returncode, elapsed / 1000,
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
        if output.is_error:
            error_text = output.error_message or output.text or "CC error"
            err = self._classify_error(error_text)
            await self._notify_status_change(err)
            raise err

        # Success — notify recovery if we were previously in error state
        if self._last_was_error:
            await self._notify_status_change(None)
        await self._fire_downgrade_callback(output)
        return output

    async def run_streaming(
        self,
        invocation: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> CCOutput:
        """Run CC with stream-json output (traced — see run() for span rationale)."""
        invocation, roster_model = roster.apply_active(invocation)
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
        # Override output format to stream-json (requires --verbose with -p).
        # Target the --output-format value by its flag, not a bare args.index("json")
        # scan — other args (e.g. --settings .../cc-span-settings.json) can contain
        # the substring "json", and keying off the flag is unambiguous.
        fmt_idx = args.index("--output-format") + 1
        args[fmt_idx] = "stream-json"
        args.insert(1, "--verbose")

        env = self._build_env(invocation)
        start = time.monotonic()

        # Extract dispatched effort from args — may differ from invocation.effort
        # if clamp_effort() clamped it for a non-Opus model.
        effort_idx = args.index("--effort") + 1
        dispatched_effort = args[effort_idx]

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC streaming session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model, dispatched_effort, invocation.timeout_s,
            prompt_preview,
        )

        try:
            scope_args = _get_scope_args()
            proc = await asyncio.create_subprocess_exec(
                *scope_args, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1_048_576,  # 1MB — CC stream-json lines can exceed 64KB default
                env=env,
                cwd=invocation.working_dir or self._working_dir,
                preexec_fn=os.setpgrp,
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
                        proc.pid, exc_info=True,
                    )
            # Feed prompt via stdin, then close to signal EOF
            if proc.stdin is not None:
                proc.stdin.write(invocation.prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
        except BaseException:
            self._unregister_proc(reg_key)
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            raise

        result_data: dict | None = None
        collected_text: list[str] = []
        event_types: list[str] = []
        timed_out = False
        terminated_after_result = False
        line_count = 0

        try:
            async with asyncio.timeout(invocation.timeout_s):
                async for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").strip()
                    if not line:
                        continue
                    line_count += 1
                    try:
                        event_raw = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("CC stream non-JSON line: %s", line[:200])
                        continue

                    etype = event_raw.get("type", "?")
                    event_types.append(etype)
                    logger.debug("CC stream event #%d: type=%s", line_count, etype)

                    event = StreamEvent.from_raw(event_raw)

                    # Log CC version from init event (pure observability)
                    if etype == "system" and event_raw.get("subtype") == "init":
                        cc_version = event_raw.get("version", "unknown")
                        logger.info("CC version: %s", cc_version)

                    if event.event_type == "text" and event.text:
                        collected_text.append(event.text)
                    if event.event_type == "result":
                        result_data = event_raw
                        result_text = event_raw.get("result", "")
                        logger.info(
                            "CC stream result: is_error=%s, result_len=%d, result_preview=%r",
                            event_raw.get("is_error"), len(result_text or ""),
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
                time.monotonic() - start, proc.pid,
            )
            try:
                pgid = os.getpgid(proc.pid)
                if pgid <= 1:
                    raise ValueError(f"Refusing killpg with pgid={pgid}")
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError, TypeError):
                proc.kill()
        finally:
            self._unregister_proc(reg_key)

        await proc.wait()
        elapsed = int((time.monotonic() - start) * 1000)

        # Read stderr for diagnostics
        stderr_data = b""
        if proc.stderr:
            stderr_data = await proc.stderr.read()
        if stderr_data:
            logger.warning("CC stderr: %s", stderr_data.decode(errors="replace")[:500])

        logger.info(
            "CC streaming finished (PID %s, exit=%s, lines=%d, has_result=%s, "
            "terminated=%s, %.1fs)",
            proc.pid, proc.returncode, line_count, result_data is not None,
            terminated_after_result, elapsed / 1000,
        )
        if event_types:
            logger.info("CC stream events: %s", " → ".join(event_types))

        if timed_out:
            partial = "".join(collected_text)
            raise CCTimeoutError(
                f"Timeout after {invocation.timeout_s}s"
                + (f" (partial: {len(partial)} chars)" if partial else ""),
            )

        if result_data is not None:
            output = self._parse_result_dict(result_data, invocation, elapsed)
            # When CC uses extended thinking, the result field can be empty
            # but the actual response was emitted as text events during streaming
            if not output.text and collected_text:
                from dataclasses import replace
                output = replace(output, text="".join(collected_text))
            if output.is_error:
                stderr_hint = stderr_data.decode(errors="replace") if stderr_data else ""
                error_text = output.error_message or output.text or stderr_hint or "CC error"
                err = self._classify_error(error_text)
                await self._notify_status_change(err)
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
                    err = CCRateLimitError("CC rate limited (stream event)")
                    await self._notify_status_change(err)
                    await self._fire_downgrade_callback(output)
                    return output
                # Empty/no response — rate limit prevented a real answer
                err = CCRateLimitError(output.text or "CC rate limited (stream event)")
                await self._notify_status_change(err)
                raise err

            # Success — notify recovery if previously errored
            if self._last_was_error:
                await self._notify_status_change(None)
            await self._fire_downgrade_callback(output)
            return output

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
        )
        await self._fire_downgrade_callback(output)
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
        self, result_data: dict, inv: CCInvocation, elapsed_ms: int,
    ) -> CCOutput:
        """Build CCOutput from a parsed result dict."""
        usage = result_data.get("usage", {})
        model_usage = result_data.get("modelUsage", {})
        model_name = next(iter(model_usage), str(inv.model))
        downgraded = self._detect_downgrade(inv.model, model_name)
        if downgraded:
            logger.warning(
                "MODEL DOWNGRADE DETECTED: requested=%s actual=%s",
                inv.model, model_name,
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
            first_line, len(raw),
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
