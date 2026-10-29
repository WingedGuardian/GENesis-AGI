#!/usr/bin/env python3
"""Drive the Genesis live agent from a terminal (Phase 0 host).

Type a line to send a turn; while a turn runs, a new line steers it. While an
approval is waiting nothing is sent: a bare "yes" or "no" answers it, and any
other line is held. Commands: /i interrupt · /y ID allow · /n ID deny ·
/steer TEXT send anyway, cancelling (denying) a waiting approval · /s status ·
/q quit.

    ~/genesis/.venv/bin/python scripts/live_agent_cli.py [--steer priority|interrupt]

The session id is kept in ``~/.genesis/live_agent/state.json`` and resumed on
the next start; a stale id starts a fresh session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.env import genesis_home, repo_root  # noqa: E402
from genesis.live_agent import LiveAgent  # noqa: E402
from genesis.live_agent.agent import approval_reply, owner_environ  # noqa: E402
from genesis.util.tasks import tracked_task  # noqa: E402

_PREFIX = {
    "progress": "  ·",
    "approval": "  ? approve with /y ID or /n ID:",
    "session": "  [session]",
    "rate_limited": "  [rate limit]",
    "finished": "  ✓",
    "error": "  ! ",
}


async def _print_events(agent: LiveAgent) -> None:
    async for event in agent.events():
        print(f"{_PREFIX.get(event.kind, '  ')} {event.text}", flush=True)
        if event.kind == "disconnected":
            return


async def _command(agent: LiveAgent, line: str, last_input_at: float) -> bool:
    """Run one input line; False means quit."""
    cmd, _, arg = line.partition(" ")
    waiting = [a["id"] for a in agent.status()["pending_approvals"]]
    if cmd == "/q":
        return False
    if cmd == "/i":
        if waiting:
            print(f"  interrupting cancels approval {', '.join(waiting)} (denied)")
        await agent.interrupt()
    elif cmd in ("/y", "/n"):
        if not agent.answer(arg.strip(), allow=cmd == "/y"):
            print(f"  no pending approval {arg.strip()!r}")
    elif cmd == "/s":
        print(json.dumps(agent.status(), indent=2))
    elif cmd == "/steer":
        if not arg.strip():
            print("  /steer needs text")
            return True
        handle = await agent.turn(arg, cancel_pending=True)
        print(f"  → {'steered' if handle.steered else 'sent'} ({handle.id})")
    elif waiting:
        _answer_or_hold(agent, line, waiting, last_input_at)
    else:
        handle = await agent.turn(line)
        print(f"  → {'steered' if handle.steered else 'sent'} ({handle.id})")
    return True


def _answer_or_hold(agent: LiveAgent, line: str, waiting: list[str], last_input_at: float) -> None:
    """A line typed while an approval waits: answer it, or hold the line."""
    reply = approval_reply(line)
    if reply is not None:
        _, message = agent.answer_reply(reply, last_input_at=last_input_at)
        print(f"  {message}")
    else:
        print(
            f"  held, not sent: approval {', '.join(waiting)} is waiting. Answer yes or no, "
            "or /steer TEXT to cancel it (denying the action) and redirect"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--steer", choices=("priority", "interrupt"), default="priority")
    args = parser.parse_args()
    # This process becomes the owner's: the agent's child inherits it.
    clean = owner_environ(dict(os.environ))
    os.environ.clear()
    os.environ.update(clean)

    agent = LiveAgent(
        cwd=repo_root(),
        state_path=genesis_home() / "live_agent" / "state.json",
        steer=args.steer,
    )
    # Taken before start: an ask withdrawn while the session starts counts as a
    # change the owner has not seen yet.
    started_at = time.monotonic()
    await agent.start()
    printer = tracked_task(_print_events(agent), name="live_agent_cli.print")
    try:
        async with _stdin_lines() as readline:
            await _input_loop(agent, readline, printer, started_at)
    finally:
        printer.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await printer
        finally:
            await agent.close()
    return 0


@contextlib.asynccontextmanager
async def _stdin_lines():
    """Pipe/terminal reads cancel without leaving an executor thread behind."""
    if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
        async def read_file():
            return sys.stdin.readline()
        yield read_file
        return
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin,
    )

    async def read_pipe():
        # Drain over-limit chunks without imposing a new limit on pasted input.
        parts = []
        while True:
            try:
                parts.append(await reader.readuntil(b"\n"))
                break
            except asyncio.LimitOverrunError as exc:
                parts.append(await reader.readexactly(exc.consumed))
            except asyncio.IncompleteReadError as exc:
                parts.append(exc.partial)
                break
        return b"".join(parts).decode(
            sys.stdin.encoding or "utf-8", errors=sys.stdin.errors or "strict",
        )

    try:
        yield read_pipe
    finally:
        transport.close()


async def _input_loop(
    agent: LiveAgent,
    readline: Callable[[], Awaitable[str]],
    printer: asyncio.Task[None],
    last_input_at: float,
) -> None:
    """Read and run lines until EOF or /q.

    ``last_input_at`` anchors "the owner has seen the screen" for a bare yes /
    no. Only a line the host acts on moves it: a blank Enter does not, or it
    would let a reply meant for a withdrawn ask approve its replacement.
    """
    while True:
        reading = tracked_task(readline(), name="live_agent_cli.input")
        try:
            await asyncio.wait([reading, printer], return_when=asyncio.FIRST_COMPLETED)
            if printer.done():
                # Asks are no longer shown, so no reply can be tied to one.
                print("  ! the event printer stopped; exiting", flush=True)
                return
            line = reading.result()
        finally:
            reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reading
        if not line:
            return
        if not line.strip():
            continue
        previous, last_input_at = last_input_at, time.monotonic()
        try:
            if not await _command(agent, line.strip(), previous):
                return
        except Exception as exc:  # report and keep the host up (refusals, timeouts)
            print(f"  ! {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
