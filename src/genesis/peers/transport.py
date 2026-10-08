"""Keep aiohttp parser/transport failures from echoing private capability leases.

The normal middleware never sees parser errors. This local protocol adapter
retains aiohttp parsing and lifecycle, but replaces its error disclosure sinks.
It never patches the framework globally or falls back to the unsafe handler.
"""

import logging
from datetime import UTC, datetime

from aiohttp import web
from aiohttp.web_protocol import RequestHandler

logger = logging.getLogger(__name__)


def _audit(status):
    logger.warning(
        "peer_broker_audit timestamp=%s credential=unverified endpoint=broker/transport "
        "task=- segment=- operation=unknown outcome=%d",
        datetime.now(UTC).isoformat(),
        status,
    )


class _PrivateRequestHandler(RequestHandler):
    def log_exception(self, *args, **kwargs):
        _audit(500)

    def log_debug(self, *args, **kwargs):
        # Framework arguments can include a parser exception or request bytes.
        pass

    def handle_error(self, request, status=500, exc=None, message=None):
        _audit(status)
        if request.writer.output_size > 0:
            raise ConnectionError("Peer response already started")
        response = web.json_response(
            {"code": "validation_error" if status == 400 else "operation_refused"},
            status=status,
        )
        response.force_close()
        return response


class _PrivateServer(web.Server):
    def __call__(self):
        return _PrivateRequestHandler(self, loop=self._loop, **self._kwargs)


class PrivateAppRunner(web.AppRunner):
    async def _make_server(self):
        # Preserve Application middleware/request construction/startup/cleanup.
        # aiohttp exposes no public custom RequestHandler factory. These two
        # protocol hooks are verified by real malformed-wire integration tests.
        server = await super()._make_server()
        return _PrivateServer(
            server.request_handler,
            request_factory=server.request_factory,
            handler_cancellation=server.handler_cancellation,
            **server._kwargs,
        )
