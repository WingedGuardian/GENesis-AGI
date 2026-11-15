"""Exact owned-child allowance over the existing strict CLI fixture fences."""

import json
import os
import sys
from pathlib import Path


def install():
    from tests.test_mcp.local_health_fixture import install as install_health

    command = json.loads(os.environ['LOCAL_MODULE_TEST_COMMAND']) if os.environ.get('LOCAL_MODULE_TEST_COMMAND') else None
    if command is not None:
        assert command[:5] == [sys.executable, '-I', '-S', '-u', '-c']
    record = install_health(module_command=command)
    if marker := os.environ.get('LOCAL_MODULE_TEST_PIPE_FAILURE'):
        import asyncio

        async def pipe_failure(loop, *args, **kwargs):
            while not Path(marker).exists():
                await asyncio.sleep(.01)
            record('injected-pipe-failure')
            raise OSError('owned pipe setup fault')

        asyncio.SelectorEventLoop.connect_read_pipe = pipe_failure
    from genesis.db.connection import SerializedConnection
    from genesis.mcp.local_modules import ModuleResourceOwner

    original_close = ModuleResourceOwner.close
    original_begin = ModuleResourceOwner.begin_draining
    original_db_close = SerializedConnection.close

    async def close(owner):
        await original_close(owner)
        record('module-owner-closed', closed=owner.closed)

    def begin(owner):
        was_closing = owner._closing
        original_begin(owner)
        if not was_closing:
            record('module-owner-draining')

    async def db_close(db):
        record('db-close-start')
        return await original_db_close(db)

    ModuleResourceOwner.close = close
    ModuleResourceOwner.begin_draining = begin
    SerializedConnection.close = db_close
