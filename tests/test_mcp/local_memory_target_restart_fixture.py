"""Owned restart observers; wraps actual serving and never selects a client mode."""

import json
import os
from pathlib import Path

from tests.test_mcp.local_memory_target_fixture import install as install_target


def install():
    install_target()
    report = Path(os.environ["LOCAL_MEMORY_TEST_REPORT"])

    def record(event, **fields):
        with report.open("a") as output:
            output.write(json.dumps({"event": event, **fields}) + "\n")

    if os.environ["LOCAL_MEMORY_TEST_ROLE"] == "mixed":
        from genesis.mcp.shared_client import _connection, _stdio

        actual_claim, actual_get = _stdio._claim_fd, _connection.RemotePeer._get
        generations = []

        def claim(fd, *args):
            result = actual_claim(fd, *args)
            if fd == 0:
                record("target-input-claimed")
            return result

        async def get(peer):
            generation = await actual_get(peer)
            if not any(generation is old for old in generations):
                generations.append(generation)
            record("target-generation", generation=next(
                index for index, old in enumerate(generations, 1) if generation is old))
            return generation

        _stdio._claim_fd, _connection.RemotePeer._get = claim, get
    elif os.environ.get("LOCAL_MEMORY_TARGET_BACKEND") == "pre-contract":
        from starlette.responses import JSONResponse
        from starlette.routing import request_response

        from genesis.mcp import shared_server

        actual_build = shared_server.build_http_app

        async def build(*args, **kwargs):
            app = await actual_build(*args, **kwargs)
            assert any(route.path == "/mcp/v3" for route in app.router.routes)
            app.router.routes[:] = [route for route in app.router.routes if route.path != "/mcp/v3"]
            route = next(route for route in app.router.routes if route.path == "/status")
            original = route.endpoint

            async def status(request):
                response = await original(request)
                body = json.loads(response.body)
                assert "database_target_contract" in body
                del body["database_target_contract"]
                return JSONResponse(body)

            route.endpoint, route.app = status, request_response(status)
            record("target-pre-contract", paths=[route.path for route in app.router.routes])
            return app

        shared_server.build_http_app = build
