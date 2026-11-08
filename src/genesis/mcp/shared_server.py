"""Qualified Uvicorn runner with resource ownership outside the HTTP lifespan."""

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import uvicorn

from genesis.caller_context import enter_shared_service
from genesis.mcp.shared_http import build_http_app
from genesis.mcp.shared_resources import initialize_shared_service
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)


class SharedServer(uvicorn.Server):
    def __init__(self, config, *, database, grace_s, close_resources=None):
        super().__init__(config)
        self.database, self.grace_s = database, grace_s
        self.close_resources = close_resources
        self.shutdown_entered = False
        self._owned_shutdown = None

    async def startup(self, sockets=None):
        try:
            await super().startup(sockets=sockets)
        except BaseException:
            # Uvicorn's started flag is still false if binding is cancelled.
            # Its SDK lifespan has already started and must settle first.
            await self.lifespan.shutdown()
            raise

    async def shutdown(self, sockets=None):
        # Admission closes synchronously before Uvicorn can cancel handlers.
        self.database.begin_draining()
        self.shutdown_entered = True
        if self._owned_shutdown is None:
            self._owned_shutdown = tracked_task(self._finish_shutdown(sockets),
                name="shared-mcp-owner-shutdown", logger=logger)
        await asyncio.shield(self._owned_shutdown)

    async def _finish_shutdown(self, sockets):
        draining = tracked_task(self.database.drain(grace_s=self.grace_s),
                                name="shared-mcp-resource-drain", logger=logger)
        try:
            await super().shutdown(sockets=sockets)
        finally:
            # Also settle lifespan on force-exit or exceptional HTTP shutdown.
            # Qualified LifespanOn tolerates an already-completed shutdown.
            await self.lifespan.shutdown()
            await asyncio.shield(draining)
            if self.close_resources is not None:
                # serve() replays captured signals before returning. Retire
                # the outer owner's resources before that replay can exit.
                await self.close_resources()


async def serve_shared(settings, *, resolver, loaded_commit, started_at, db_path,
                       status_path, cache_dir, enabled, setup_routing, setup_health_actions):
    async with AsyncExitStack() as owner:
        resources = await owner.enter_async_context(initialize_shared_service(
            settings, db_path=db_path, status_path=status_path,
            cache_dir=cache_dir, setup_routing=setup_routing,
            setup_health_actions=setup_health_actions))
        # Existing standalone routing can load dotenv. Restore process identity
        # once after bootstrap; request identity never mutates the environment.
        enter_shared_service()
        os.environ.pop("GENESIS_SLOT", None)
        app = await build_http_app(resources.server, config=settings.http, resolver=resolver,
            database=resources.database, tracker=resources.tracker, loaded_commit=loaded_commit,
            started_at=started_at, enabled=enabled)
        address = urlsplit(settings.http.url)
        config = uvicorn.Config(app, host=address.hostname, port=address.port,
            proxy_headers=False, access_log=False, log_level="warning", lifespan="on",
            timeout_graceful_shutdown=settings.shutdown_grace_s)
        server = SharedServer(config, database=resources.database, grace_s=settings.shutdown_grace_s,
                              close_resources=owner.aclose)
        try:
            await server.serve()
        finally:
            # serve() itself has no exceptional-exit cleanup. Keep the owner
            # alive while stopping an already-bound listener and SDK lifespan.
            if server.started:
                # Rejoin even if a caller cancelled its first shutdown wait.
                await server.shutdown()
