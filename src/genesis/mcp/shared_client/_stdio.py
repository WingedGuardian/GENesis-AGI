"""Qualified raw admission and cancellable POSIX pipe output; SDK owns framing."""

import os
import sys
from contextlib import suppress

import anyio
from mcp.server.stdio import _claim_fd, _open_stdin_diversion, _open_stdout_diversion, stdio_server
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_REQUEST, ErrorData, JSONRPCError, jsonrpc_message_adapter

from genesis.mcp.shared_http._admission import validate_envelope

from ._connection import RemotePeer


class _PipeTextOutput:
    def __init__(self, fd):
        self.fd = fd

    async def write(self, text):
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            await anyio.lowlevel.checkpoint_if_cancelled()
            try:
                count = os.write(self.fd, remaining)
            except BlockingIOError:
                await anyio.wait_writable(self.fd)
                continue
            if count == 0:
                raise BrokenPipeError("MCP output closed")
            remaining = remaining[count:]
        return len(text)

    async def flush(self):
        pass  # os.write has no userspace buffer.


async def serve(settings, header_factory):
    ready, output, peer = anyio.Event(), None, None

    async def refuse():
        # Frontend may already have ended; the owner still joins this task.
        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            await output.send(SessionMessage(JSONRPCError(
                jsonrpc="2.0", id=None,
                error=ErrorData(code=INVALID_REQUEST, message="Invalid MCP envelope"),
            )))

    class RawInput:
        async def __aiter__(self):
            async for raw in anyio.wrap_file(input_buffer):
                try:
                    body = validate_envelope(raw)
                    text = raw.decode("utf-8", "strict")
                    # Check the exact SDK JSON decoder too (e.g. a UTF-8 BOM),
                    # so its parser cannot silently drop a raw-admitted frame.
                    jsonrpc_message_adapter.validate_json(text, by_name=False)
                    await ready.wait()
                    if body.get("method") == "initialize" and body.get("id") is not None:
                        peer.arm_initialize()
                except ValueError:
                    await ready.wait()
                    # As for SDK request replies, sending must not hide input EOF
                    # behind a non-reading peer. These tasks belong to the owner.
                    peer.group.start_soon(refuse)
                    continue
                yield text

    input_buffer, restore_input = _claim_fd(0, sys.stdin, "rb", _open_stdin_diversion)
    output_buffer = restore_output = old_blocking = None
    try:
        output_buffer, restore_output = _claim_fd(1, sys.stdout, "wb", _open_stdout_diversion)
        if input_buffer.fileno() == 0 or output_buffer.fileno() == 1:
            raise RuntimeError("Shared MCP requires owned stdio descriptors")
        output_fd = output_buffer.fileno()
        old_blocking = os.get_blocking(output_fd)
        os.set_blocking(output_fd, False)
        with anyio.CancelScope() as stdio_owner:
            async with stdio_server(stdin=RawInput(), stdout=_PipeTextOutput(output_fd)) as (incoming, outgoing):
                output = outgoing
                async with anyio.create_task_group() as owners:
                    peer = RemotePeer(owners, settings, header_factory)
                    peer.front = JSONRPCDispatcher(incoming, outgoing)
                    ready.set()
                    try:
                        await peer.front.run(peer.request, peer.notify)
                    finally:
                        with anyio.CancelScope(shield=True):
                            await peer.close()
                    owners.cancel_scope.cancel()
                stdio_owner.cancel()
    finally:
        if old_blocking is not None:
            os.set_blocking(output_buffer.fileno(), old_blocking)
        if restore_output is not None:
            restore_output()
        if restore_input is not None:
            restore_input()
