"""Bounded projection of the existing read-only deployment status tripwire."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pwd
import re
import signal
from pathlib import Path

from genesis.util.proc_kill import kill_process_group, reap_bounded
from genesis.util.streams import read_limited

STREAM_LIMIT = 64 * 1024
COLLECTION_TIMEOUT = 20.0
TOKEN_RE = re.compile(r"b1-[0-9a-f]{24}")
VALID_BRACKET = "valid — nothing the server runs changed since the token"
FIELDS = frozenset(
    {"serving", "head", "mainpid", "invocation", "runtime-edits", "runtime-overrides", "bracket"}
)


def project(raw: bytes, *, token: str | None = None) -> dict:
    """Require every primary field; never forward explanatory text or paths."""
    values = {}
    for line in raw.decode("utf-8", errors="strict").split("\n"):
        key, sep, value = line.partition(":")
        if key not in FIELDS:
            continue
        if not sep or not value.startswith(" ") or key in values:
            raise ValueError("Serving fields unavailable")
        values[key] = value[1:]
    if set(values) != FIELDS:
        raise ValueError("Serving fields unavailable")
    if (
        any(not re.fullmatch(r"[0-9a-f]{40}", values[k]) for k in ("serving", "head"))
        or not re.fullmatch(r"[1-9][0-9]{0,19}", values["mainpid"])
        or not re.fullmatch(r"[0-9a-f]{32}", values["invocation"])
        or values["runtime-edits"] != "none"
        or not re.fullmatch(
            r"none|[1-9][0-9]{0,19} files, [0-9a-f]{16}", values["runtime-overrides"]
        )
        or (
            values["bracket"] != VALID_BRACKET
            if token is not None
            else not TOKEN_RE.fullmatch(values["bracket"])
        )
    ):
        raise ValueError("Serving fields unavailable")
    result = {**values, "mainpid": int(values["mainpid"]), "established": True}
    if token is not None:
        result.update(bracket=token, verified=True)
    return result


def child_environment() -> dict[str, str]:
    """Use installation identity, never inherited shell/Git/deploy test seams."""
    uid = os.getuid()
    return {
        "HOME": pwd.getpwuid(uid).pw_dir,
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }


async def _spawn(argv: list[str], runtime: Path):
    """Create the owned child so a cancellation during creation still kills its group.

    Cancellation (or SIGINT, which asyncio.run turns into one) can arrive while
    the child already exists in its new session but the handle has not been
    returned. The creation runs on under the shield, so the handle is recovered
    and the whole group killed before the error propagates.
    """
    creation = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=runtime,
            env=child_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    try:
        return await asyncio.shield(creation)
    except BaseException:
        # Recovery stays shielded: a repeated cancellation must not cancel the
        # creation before its handle is recovered. A creation that itself
        # failed, or was cancelled by loop teardown, leaves nothing to own; the
        # original error propagates either way.
        while not creation.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(creation)
        if not creation.cancelled() and creation.exception() is None:
            proc = creation.result()
            kill_process_group(proc)
            with contextlib.suppress(BaseException):
                await reap_bounded(proc)
        raise


async def _capture(runtime: Path, token: str | None) -> bytes:
    argv = ["/bin/bash", str(runtime / "scripts/deploy_code_only.sh"), "status"]
    if token is not None:
        argv.extend(["--verify", token])
    proc = await _spawn(argv, runtime)
    try:
        async with asyncio.timeout(COLLECTION_TIMEOUT):
            stdout, stderr, code = await asyncio.gather(
                read_limited(proc.stdout, STREAM_LIMIT),
                read_limited(proc.stderr, STREAM_LIMIT),
                proc.wait(),
            )
        if code != 0 or stdout[1] > STREAM_LIMIT or stderr[1] > STREAM_LIMIT:
            raise ValueError("Serving fields unavailable")
        return stdout[0]
    except BaseException:
        kill_process_group(proc)
        await reap_bounded(proc)
        raise


def observe(runtime: Path, *, token: str | None = None) -> dict:
    """Unknown is a static observation result, never a functional verdict."""
    if token is not None and (not isinstance(token, str) or not TOKEN_RE.fullmatch(token)):
        raise ValueError("Unsupported validation token")
    prior = signal.getsignal(signal.SIGTERM)
    try:
        try:
            return project(asyncio.run(_capture_scoped(runtime, token)), token=token)
        except Exception:
            result = {"established": False, "reason": "Serving identity unavailable"}
            if token is not None:
                result.update(bracket=token, verified=False)
            return result
    finally:
        # Loop removal/close resets SIGTERM to SIG_DFL; restore only after it.
        signal.signal(signal.SIGTERM, prior)


async def _capture_scoped(runtime: Path, token: str | None) -> bytes:
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        return await _capture(runtime, token)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
