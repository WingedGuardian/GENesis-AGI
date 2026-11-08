#!/usr/bin/env python3
"""Hold one Genesis MCP session open across a forced update rollback (CI only).

Used by .github/workflows/update-rollback-test.yml. It starts the real
``scripts/genesis_mcp_server.py --server health`` over stdio, the same server a
Claude Code session attaches, whose lifespan keeps one long-lived database
connection for the life of the process. That is the reader shape the rollback's
database restore must survive. It then polls two read-only tools until a stop
file appears, writing one JSON line per poll:

- ``db_schema(table=<probe>)``: whether the probe table the failing update's
  migration creates is visible. This tool reads through the server's held
  lifespan connection, so it is the check that the long-lived connection sees
  the restored database (at the schema level);
- ``update_history_recent(limit=5)``: the newest update attempt. This tool
  opens a NEW connection per call, so it shows the reader process sees the
  rollback's row, not that the held connection does.

The workflow asserts on the log: the session saw the probe table while the
failed update ran (so it reads live data), and the SAME session, never
restarted, then saw it gone after the restore; its process also sees the
``rolled_back`` row (through a new connection, as above). A
poll that raises, times out or returns a tool error is logged as an error
line, never hidden.

Run with the venv python from the checkout root:
    mcp_rollback_reader.py --log FILE --stop FILE [--probe TABLE] [--interval S]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALL_TIMEOUT_S = 90.0  # a wedged server must fail the poll, not hang the job


async def _call(session: ClientSession, tool: str, arguments: dict) -> str:
    """The tool's text, or raise: a tool-side error is a failed poll."""
    result = await asyncio.wait_for(session.call_tool(tool, arguments), CALL_TIMEOUT_S)
    text = _text(result)
    if getattr(result, "isError", False):
        raise RuntimeError(f"{tool} returned a tool error: {text[:300]}")
    return text


def _text(result) -> str:
    """The tool result's text content, joined."""
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def _probe_visible(payload: str, probe: str) -> bool | None:
    """True when db_schema reported columns for the probe table, False only when
    it said that table is not found, None for anything else (no database, an
    unreadable answer): an outage must never read as "the table is gone"."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("columns"):
        return True
    if str(data.get("error", "")).startswith("Table not found"):
        return False
    return None


def _newest_status(payload: str) -> str | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get("entries") or []
    if not entries or not isinstance(entries[0], dict):
        return None
    return entries[0].get("status")


async def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=str(root / ".venv" / "bin" / "python"),
        args=[str(root / "scripts" / "genesis_mcp_server.py"), "--server", "health"],
        cwd=str(root),
        # The SDK default hands the server a filtered environment (it drops
        # GENESIS_DB_PATH and the like); pass ours whole, as a Claude Code
        # session passes its environment to the MCP servers it starts.
        env=dict(os.environ),
    )
    log = Path(args.log)
    stop = Path(args.stop)
    started = time.time()
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), CALL_TIMEOUT_S)
        tools = {
            t.name for t in (await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT_S)).tools
        }
        missing = {"db_schema", "update_history_recent"} - tools
        if missing:
            with log.open("a") as fh:
                fh.write(
                    json.dumps(
                        {"t": time.time(), "event": "missing_tools", "tools": sorted(missing)}
                    )
                    + "\n"
                )
            return 2
        with log.open("a") as fh:
            fh.write(json.dumps({"t": time.time(), "event": "ready", "tools": len(tools)}) + "\n")
        while not stop.exists():
            if time.time() - started > args.max_seconds:
                with log.open("a") as fh:
                    fh.write(json.dumps({"t": time.time(), "event": "timeout"}) + "\n")
                return 3
            row: dict = {"t": time.time(), "event": "poll"}
            try:
                schema = await _call(session, "db_schema", {"table": args.probe})
                history = await _call(session, "update_history_recent", {"limit": 5})
                row["probe_visible"] = _probe_visible(schema, args.probe)
                row["newest_update_status"] = _newest_status(history)
            except Exception as exc:  # logged, never swallowed: the workflow asserts on it
                row["event"] = "error"
                row["error"] = f"{type(exc).__name__}: {exc}"[:500]
            with log.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            if args.once:
                return 0 if row["event"] == "poll" else 4
            await asyncio.sleep(args.interval)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--stop", required=True)
    ap.add_argument("--probe", default="ci_rollback_probe")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--max-seconds", type=float, default=5400.0)
    ap.add_argument(
        "--once", action="store_true", help="poll once and exit (a fresh-session control)"
    )
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
