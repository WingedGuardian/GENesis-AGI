#!/usr/bin/env python3
"""Attach owner terminal controls to the persistent local live-agent host.

Commands: /s status, /i interrupt, /steer TEXT explicitly redirect,
/y ID approve, /n ID deny, /q detach. Other text sends an owner turn.
Detaching leaves the session alive. Events are not replayed and commands are
never retried automatically after a lost connection.
"""

import argparse
import asyncio
import json
import os

from live_agent_cli import _stdin_lines

from genesis.live_agent.__main__ import control_socket
from genesis.live_agent.host import _peer_uid, encode
from genesis.live_agent.wire import read_line


def command(line: str, request_id: str) -> dict | None:
    cmd, _, arg = line.partition(" ")
    if cmd == "/q":
        return None
    message = {"id": request_id}
    if cmd in ("/s", "/i"):
        message["op"] = "status" if cmd == "/s" else "interrupt"
    elif cmd in ("/y", "/n"):
        message.update(op="answer", approval_id=arg.strip(), allow=cmd == "/y")
    elif cmd == "/steer":
        message.update(op="steer", text=arg)
    elif cmd.startswith("/"):
        raise ValueError("unknown command")
    else:
        message.update(op="turn", text=line)
    return message


async def attach(path) -> None:
    reader, writer = await asyncio.open_unix_connection(str(path))
    tasks = []
    try:
        if _peer_uid(writer) != os.getuid():
            raise PermissionError("control server is not owned by this user")

        async def receive():
            while line := await read_line(reader):
                msg = json.loads(line)
                if msg.get("type") == "event":
                    print(msg["text"], flush=True)
                else:
                    print(json.dumps(msg, ensure_ascii=False), flush=True)
            print("Disconnected. History is not replayed; inspect status before resending.", flush=True)

        async def send():
            async with _stdin_lines() as readline:
                count = 0
                while line := await readline():
                    if not line.strip():
                        continue
                    count += 1
                    try:
                        msg = command(line.strip(), str(count))
                    except ValueError as exc:
                        print(str(exc), flush=True)
                        continue
                    if msg is None:
                        return
                    writer.write(encode(msg))
                    await writer.drain()

        from genesis.util.tasks import tracked_task
        tasks = [tracked_task(receive(), name="live_agent_attach.receive"), tracked_task(send(), name="live_agent_attach.send")]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.transport.abort()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=control_socket())
    asyncio.run(attach(parser.parse_args().socket))
