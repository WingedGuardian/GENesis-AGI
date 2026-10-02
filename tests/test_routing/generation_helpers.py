"""Synthetic routing generation builders shared by component regressions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.router import Router
from genesis.routing.types import (
    BudgetStatus,
    CallSiteConfig,
    ProviderConfig,
    RetryPolicy,
    RoutingConfig,
)


def config(model="old-model", name="provider"):
    provider = ProviderConfig(name=name, provider_type="openai", model_id=model,
                              is_free=True, rpm_limit=None, open_duration_s=60)
    return RoutingConfig(providers={name: provider},
                         call_sites={"test": CallSiteConfig(id="test", chain=[name])},
                         retry_profiles={"default": RetryPolicy(max_retries=0)})


def make_router(cfg, tmp_path):
    tracker = MagicMock(db=None)
    tracker.check_budget = AsyncMock(return_value=BudgetStatus.UNDER_LIMIT)
    return Router(config=cfg,
                  breakers=CircuitBreakerRegistry(cfg.providers,
                      state_file=tmp_path / "breakers.json", persist=False),
                  cost_tracker=tracker, degradation=MagicMock(should_skip=lambda _: False),
                  delegate=LiteLLMDelegate(cfg, profile_registry=MagicMock()))


def response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                           usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1))


async def drain_escalation_tasks():
    tasks = [task for task in asyncio.all_tasks()
             if task is not asyncio.current_task() and task.get_name().startswith("escalation-")]
    if tasks:
        await asyncio.gather(*tasks)
