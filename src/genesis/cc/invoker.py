"""CCInvoker — async subprocess wrapper for claude -p CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable

from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCSessionError,
    CCTimeoutError,
)
from genesis.cc.types import CCInvocation, CCModel, CCOutput, StreamEvent

logger = logging.getLogger(__name__)


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
        self._active_proc: asyncio.subprocess.Process | None = None
        self._on_cc_status_change = on_cc_status_change
        self._on_model_downgrade = on_model_downgrade
        self._last_was_error = False
        self._status_lock = asyncio.Lock()
        self._protected_paths = protected_paths

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
        args += ["--model", str(inv.model)]
        args += ["--output-format", inv.output_format]
        args += ["--effort", str(inv.effort)]
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

    def _build_env(self, inv: CCInvocation | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # Signal to SessionStart hooks that this is a Genesis-dispatched session.
        # The genesis_session_context.py hook skips identity injection when set,
        # preventing double injection (identity is in the system prompt arg).
        env["GENESIS_CC_SESSION"] = "1"
        if inv and inv.stream_idle_timeout_ms is not None:
            env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] = str(inv.stream_idle_timeout_ms)
        return env

    async def interrupt(self) -> None:
        """Send SIGINT to active subprocess. No-op if nothing running."""
        proc = self._active_proc
        if proc and proc.returncode is None:
            proc.send_signal(signal.SIGINT)

    @staticmethod
    def _classify_error(stderr_text: str) -> CCError:
        """Classify stderr text into a typed CC exception."""
        lower = stderr_text.lower()
        # Session expiry
        if ("session" in lower and ("not found" in lower or "expired" in lower)):
            return CCSessionError(stderr_text)
        # Hard quota exhaustion (usage limit hit for hours — distinct from 429)
        _QUOTA_PATTERNS = (
            "usage limit", "quota exceeded", "limit reached",
            "usage cap", "spending limit", "token limit exceeded",
        )
        if any(p in lower for p in _QUOTA_PATTERNS):
            return CCQuotaExhaustedError(stderr_text)
        # Transient rate limit (429, recovers in minutes)
        # CC CLI says "You've hit your limit · resets Xpm" — not "rate limit"
        _RATE_LIMIT_PATTERNS = (
            "rate limit", "rate_limit", "429",
            "hit your limit", "hit the limit",
        )
        if any(p in lower for p in _RATE_LIMIT_PATTERNS):
            return CCRateLimitError(stderr_text)
        # MCP server error
        if "mcp" in lower or "mcp server" in lower:
            # Try to extract server name
            server_name = None
            for marker in ("server '", 'server "', "server: "):
                idx = lower.find(marker)
                if idx >= 0:
                    start = idx + len(marker)
                    end = stderr_text.find(
                        "'" if marker.endswith("'") else ('"' if marker.endswith('"') else " "),
                        start,
                    )
                    if end > start:
                        server_name = stderr_text[start:end]
                    break
            return CCMCPError(stderr_text, server_name=server_name)
        # Generic process error
        return CCProcessError(stderr_text)

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
        args = self._build_args(invocation)
        env = self._build_env(invocation)
        start = time.monotonic()

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model, invocation.effort, invocation.timeout_s,
            prompt_preview,
        )

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=invocation.working_dir or self._working_dir,
                preexec_fn=os.setpgrp,
            )
            self._active_proc = proc
            logger.info("CC subprocess spawned (PID %s)", proc.pid)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=invocation.prompt.encode()),
                timeout=invocation.timeout_s,
            )
        except TimeoutError:
            elapsed_s = time.monotonic() - start
            logger.error(
                "CC session TIMEOUT after %.0fs (PID %s, limit=%ds)",
                elapsed_s, proc.pid, invocation.timeout_s,
            )
            try:
                pgid = os.getpgid(proc.pid)
                if pgid <= 1:
                    raise ValueError(f"Refusing killpg with pgid={pgid}")
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError, TypeError):
                proc.kill()
            await proc.wait()
            raise CCTimeoutError(f"Timeout after {invocation.timeout_s}s") from None
        finally:
            self._active_proc = None

        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "CC subprocess finished (PID %s, exit=%s, %.1fs)",
            proc.pid, proc.returncode, elapsed / 1000,
        )
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            logger.error(
                "CC subprocess failed (exit=%s): %s",
                proc.returncode, stderr_text[:500] or "(no stderr)",
            )
            err = self._classify_error(stderr_text)
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
        """Run CC with stream-json output, calling on_event for each line."""
        args = self._build_args(invocation)
        # Override output format to stream-json (requires --verbose with -p)
        fmt_idx = args.index("json")
        args[fmt_idx] = "stream-json"
        args.insert(1, "--verbose")

        env = self._build_env(invocation)
        start = time.monotonic()

        prompt_preview = invocation.prompt[:80].replace("\n", " ")
        logger.info(
            "CC streaming session starting: model=%s effort=%s timeout=%ds prompt=%r...",
            invocation.model, invocation.effort, invocation.timeout_s,
            prompt_preview,
        )

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1_048_576,  # 1MB — CC stream-json lines can exceed 64KB default
            env=env,
            cwd=invocation.working_dir or self._working_dir,
            preexec_fn=os.setpgrp,
        )
        self._active_proc = proc
        logger.info("CC streaming subprocess spawned (PID %s)", proc.pid)
        # Feed prompt via stdin, then close to signal EOF
        if proc.stdin is not None:
            proc.stdin.write(invocation.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

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

        await proc.wait()
        self._active_proc = None
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
        )
