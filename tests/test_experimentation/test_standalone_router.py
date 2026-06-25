"""Tests for the offline StandaloneLiteLLMRouter (stubbed delegate — no API).

Covers the logic the live E2E doesn't exercise deterministically: provider
lookup + ``ValueError`` on an unknown provider, the retryable-vs-non-retryable
retry loop, ``StandaloneRoutingResult`` construction, and the ``_ensure_secrets``
side-call. ``asyncio.sleep`` and ``_ensure_secrets`` are patched so the suite is
hermetic and fast (no real backoff wait, no dependence on secrets.env).
"""

from types import SimpleNamespace

import pytest

from genesis.experimentation import standalone_router as sr
from genesis.experimentation.standalone_router import StandaloneLiteLLMRouter


@pytest.fixture(autouse=True)
def _patch_secrets(monkeypatch):
    """Replace the secrets.env loader with a counter — hermetic + assertable."""
    calls = {"n": 0}

    def _fake():
        calls["n"] += 1

    monkeypatch.setattr("genesis.eval.reflection_golden_set._ensure_secrets", _fake)
    return calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the real exponential backoff so retry tests don't wait seconds."""

    async def _noop(_delay):
        return None

    monkeypatch.setattr(sr.asyncio, "sleep", _noop)


def _config(model_id="groq/llama-test"):
    return SimpleNamespace(providers={"groq-free": SimpleNamespace(model_id=model_id)})


class _StubDelegate:
    """Returns a scripted sequence of results, one per ``call`` invocation."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def call(self, *, provider, model_id, messages, **kwargs):
        self.calls.append({"provider": provider, "model_id": model_id, "messages": messages})
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[idx]


def _result(success, content=None, error=None):
    return SimpleNamespace(success=success, content=content, error=error)


def test_unknown_provider_raises_with_available(_patch_secrets):
    with pytest.raises(ValueError, match="unknown provider"):
        StandaloneLiteLLMRouter("does-not-exist", config=_config(), delegate=_StubDelegate([]))
    # The constructor must still load secrets before the lookup failure path.
    assert _patch_secrets["n"] == 1


def test_init_pulls_model_id_and_calls_ensure_secrets(_patch_secrets):
    router = StandaloneLiteLLMRouter(
        "groq-free", config=_config("groq/m"), delegate=_StubDelegate([]),
    )
    assert router._model_id == "groq/m"
    assert router._provider_name == "groq-free"
    assert _patch_secrets["n"] == 1


async def test_route_call_success():
    delegate = _StubDelegate([_result(True, content="hello")])
    router = StandaloneLiteLLMRouter("groq-free", config=_config("groq/m"), delegate=delegate)

    res = await router.route_call("gen", [{"role": "user", "content": "hi"}])

    assert res.success is True
    assert res.content == "hello"
    assert res.model_id == "groq/m"
    assert res.provider_used == "groq-free"
    assert res.error is None
    assert len(delegate.calls) == 1
    assert delegate.calls[0]["provider"] == "groq-free"
    assert delegate.calls[0]["model_id"] == "groq/m"


async def test_route_call_retries_retryable_then_succeeds():
    delegate = _StubDelegate([
        _result(False, error="Rate limit exceeded (429)"),
        _result(True, content="recovered"),
    ])
    router = StandaloneLiteLLMRouter("groq-free", config=_config(), delegate=delegate)

    res = await router.route_call("gen", [])

    assert res.success is True
    assert res.content == "recovered"
    assert len(delegate.calls) == 2  # one retry consumed


async def test_route_call_does_not_retry_non_retryable():
    delegate = _StubDelegate([
        _result(False, error="Authentication failed: invalid api key"),
        _result(True, content="should-not-be-reached"),
    ])
    router = StandaloneLiteLLMRouter("groq-free", config=_config(), delegate=delegate)

    res = await router.route_call("gen", [])

    assert res.success is False
    assert res.error == "Authentication failed: invalid api key"
    assert res.provider_used == "groq-free"
    assert len(delegate.calls) == 1  # no retry on a non-retryable error


async def test_route_call_exhausts_retries():
    delegate = _StubDelegate([_result(False, error="503 overloaded")] * 5)
    router = StandaloneLiteLLMRouter("groq-free", config=_config(), delegate=delegate)

    res = await router.route_call("gen", [])

    assert res.success is False
    assert len(delegate.calls) == sr._MAX_RETRIES + 1  # initial + all retries


async def test_close_is_noop():
    router = StandaloneLiteLLMRouter("groq-free", config=_config(), delegate=_StubDelegate([]))
    await router.close()  # must not raise
