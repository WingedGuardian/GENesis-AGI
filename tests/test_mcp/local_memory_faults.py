"""Owned delay and status-response faults, outside the real tool bodies."""

import json
import os
from pathlib import Path

import anyio


def install():
    from genesis.db.crud import observations

    report = Path(os.environ["LOCAL_MEMORY_TEST_REPORT"])
    role, fault = os.environ["LOCAL_MEMORY_TEST_ROLE"], os.environ["LOCAL_MEMORY_TEST_FAULT"]

    def record(event, **fields):
        with report.open("a") as stream:
            stream.write(json.dumps({"event": event, "role": role, **fields}) + "\n")

    async def hold(route):
        record("held", route=route)
        try:
            await anyio.sleep_forever()
        finally:
            record("hold-ended", route=route)

    actual_query, actual_create = observations.query, observations.create

    async def query(db, *args, **kwargs):
        if kwargs.get("source") == "owned-memory-hold":
            await hold("remote")
        return await actual_query(db, *args, **kwargs)

    async def create(db, *args, **kwargs):
        if kwargs.get("content") == "owned-memory-hold":
            await hold("local")
        return await actual_create(db, *args, **kwargs)

    observations.query, observations.create = query, create
    if role == "mixed" and fault in {"feature", "catalog"}:
        import httpx2

        original_send = httpx2.AsyncClient.send

        async def send(client, request, *args, **kwargs):
            response = await original_send(client, request, *args, **kwargs)
            if request.url.path == "/status":
                body = response.json()
                if fault == "feature":
                    body.pop("plan_bookmark_pending_version")
                else:
                    body["catalog_revision"] = "0" * 64
                record("status-fault", kind=fault)
                return httpx2.Response(response.status_code, json=body, request=request)
            return response

        httpx2.AsyncClient.send = send
