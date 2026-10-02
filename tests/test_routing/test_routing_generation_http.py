"""Local HTTP-path reload verification; no external inference."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.degradation import DegradationTracker
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.router import Router


@pytest.mark.asyncio
async def test_reload_http_dispatch_and_accounting_e2e(sample_config, db, tmp_path):
    """Real LiteLLM HTTP requests, reload and SQLite accounting, without paid inference."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from genesis.routing.types import CallSiteConfig

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body["model"])
            payload = json.dumps(
                {
                    "id": "local-reload-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": body["model"]},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = replace(
        sample_config.providers["paid-1"],
        provider_type="lmstudio",
        model_id="local-old",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
    )
    config = replace(
        sample_config,
        providers={"paid-1": provider},
        call_sites={
            "http-e2e": CallSiteConfig(id="http-e2e", chain=["paid-1"]),
        },
    )
    registry = CircuitBreakerRegistry(config.providers, state_file=tmp_path / "state.json")
    router = Router(
        config, registry, CostTracker(db), DegradationTracker(), LiteLLMDelegate(config)
    )
    try:
        with patch(
            "genesis.routing.litellm_delegate._resolve_api_key", return_value="local-test-only"
        ):
            first = await router.route_call(
                "http-e2e", [{"role": "user", "content": "identity"}], budget_override=True
            )
            router.reload_config(replace(config, providers={"paid-1": replace(provider, model_id="replacement")}))
            second = await router.route_call(
                "http-e2e", [{"role": "user", "content": "identity"}], budget_override=True
            )
        assert first.success and second.success
        assert seen == ["local-old", "replacement"]
        assert [first.model_id, second.model_id] == seen
        assert [first.content, second.content] == seen
        assert first.input_tokens == second.input_tokens == 7
        assert first.output_tokens == second.output_tokens == 3
        async with db.execute(
            "SELECT COUNT(*) FROM cost_events WHERE json_extract(metadata, '$.call_site') = ?", ("http-e2e",)
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
