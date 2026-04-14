"""Automated eval runner — loads dataset, calls LLM, scores, stores results.

Uses LiteLLMDelegate directly (no Router overhead) since eval runs
target a single provider with no fallback chain needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from genesis.eval.datasets import load_dataset
from genesis.eval.scorers import get_scorer
from genesis.eval.types import (
    EvalRunSummary,
    EvalTrigger,
    ScoredOutput,
    TaskCategory,
)
import litellm

from genesis.routing.config import load_config
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.rate_gate import ProviderRateGate
from genesis.routing.types import RoutingConfig

# Suppress litellm's verbose cost-calculator noise in eval output
litellm.suppress_debug_info = True
litellm.set_verbose = False

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

logger = logging.getLogger(__name__)

# Default routing config path
_CONFIG_PATH = None  # Resolved lazily

# Retry config for transient errors
_MAX_RETRIES = 2
_RETRY_BASE_DELAY_S = 5.0
_RETRYABLE_SUBSTRINGS = (
    "rate limit", "ratelimit", "429", "503", "service unavailable",
    "overloaded", "timeout", "connection", "temporarily",
)


def _default_config_path() -> Path:
    from pathlib import Path
    return Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception is a transient error worth retrying."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _RETRYABLE_SUBSTRINGS)


async def run_eval(
    *,
    provider_name: str,
    dataset_name: str,
    trigger: EvalTrigger = EvalTrigger.MANUAL,
    config: RoutingConfig | None = None,
    config_path: Path | None = None,
    db: aiosqlite.Connection | None = None,
    system_prompt: str | None = None,
) -> EvalRunSummary:
    """Run an eval dataset against a single provider.

    Args:
        provider_name: Provider key from model_routing.yaml (e.g. "cerebras-qwen")
        dataset_name: Dataset name without .yaml extension
        trigger: What triggered this eval
        config: Pre-loaded routing config (optional)
        config_path: Path to model_routing.yaml (optional, uses default)
        db: Database connection for storing results (optional)
        system_prompt: Optional system prompt override

    Returns:
        EvalRunSummary with all results
    """
    # Load config
    if config is None:
        cfg_path = config_path or _default_config_path()
        config = load_config(cfg_path)

    if provider_name not in config.providers:
        raise ValueError(
            f"unknown provider '{provider_name}' — "
            f"available: {', '.join(sorted(config.providers))}"
        )

    provider_cfg = config.providers[provider_name]
    delegate = LiteLLMDelegate(config)

    # Set up rate gate if provider has an RPM limit
    rate_gate: ProviderRateGate | None = None
    if provider_cfg.rpm_limit and provider_cfg.rpm_limit > 0:
        rate_gate = ProviderRateGate(provider_name, provider_cfg.rpm_limit)
        logger.info(
            "Rate gate active for %s: %d RPM (%.1fs between requests)",
            provider_name, provider_cfg.rpm_limit, rate_gate.interval,
        )

    # Load dataset
    cases = load_dataset(dataset_name)
    if not cases:
        raise ValueError(f"dataset '{dataset_name}' is empty")

    run_id = uuid.uuid4().hex
    start_time = time.monotonic()
    results: list[ScoredOutput] = []
    passed_count = 0
    failed_count = 0
    skipped_count = 0

    logger.info(
        "Starting eval run %s: provider=%s dataset=%s cases=%d",
        run_id[:8], provider_name, dataset_name, len(cases),
    )

    for case in cases:
        scorer = get_scorer(case.scorer_type)
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": case.input_text})

        case_start = time.monotonic()
        call_result = None
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            # Respect RPM limit before each attempt
            if rate_gate is not None:
                waited = await rate_gate.acquire()
                if waited > 0.1:
                    logger.debug(
                        "Rate gate: waited %.1fs before %s (attempt %d)",
                        waited, case.id, attempt + 1,
                    )

            try:
                call_result = await delegate.call(
                    provider=provider_name,
                    model_id=provider_cfg.model_id,
                    messages=messages,
                )
                last_exc = None
                break  # success
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES and _is_retryable(exc):
                    delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                    # Honour retry_after if available from result (not applicable here
                    # since delegate raised, but check string for "retry after N")
                    logger.warning(
                        "Eval case %s attempt %d/%d failed (%s), retrying in %.0fs",
                        case.id, attempt + 1, _MAX_RETRIES + 1, exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    break

        if last_exc is not None:
            logger.warning(
                "Eval case %s skipped for %s after %d attempts: %s",
                case.id, provider_name, _MAX_RETRIES + 1, last_exc,
            )
            skipped_count += 1
            results.append(ScoredOutput(
                case_id=case.id,
                passed=False,
                score=0.0,
                actual_output=f"ERROR: {last_exc}",
                scorer_type=case.scorer_type,
                scorer_detail=f"call failed: {last_exc}",
                skipped=True,
            ))
            continue

        assert call_result is not None
        latency_ms = (time.monotonic() - case_start) * 1000

        if not call_result.success:
            # Transient errors (rate limit, service unavailable) → skip.
            # Genuine provider errors (auth failure, bad request) → fail.
            is_transient = (
                call_result.retry_after_s is not None
                or call_result.status_code in (429, 503)
            )
            if is_transient:
                logger.warning(
                    "Eval case %s skipped (transient, status=%s): %s",
                    case.id, call_result.status_code, call_result.error,
                )
                skipped_count += 1
                results.append(ScoredOutput(
                    case_id=case.id,
                    passed=False,
                    score=0.0,
                    actual_output=call_result.error or "provider error",
                    scorer_type=case.scorer_type,
                    scorer_detail=f"provider error (transient, {call_result.status_code}): {call_result.error}",
                    latency_ms=latency_ms,
                    skipped=True,
                ))
            else:
                failed_count += 1
                results.append(ScoredOutput(
                    case_id=case.id,
                    passed=False,
                    score=0.0,
                    actual_output=call_result.error or "provider error",
                    scorer_type=case.scorer_type,
                    scorer_detail=f"provider error: {call_result.error}",
                    latency_ms=latency_ms,
                ))
            continue

        actual = call_result.content or ""
        passed, score, detail = scorer.score(
            actual, case.expected_output, case.scorer_config or None,
        )

        if passed:
            passed_count += 1
        else:
            failed_count += 1

        results.append(ScoredOutput(
            case_id=case.id,
            passed=passed,
            score=score,
            actual_output=actual,
            scorer_type=case.scorer_type,
            scorer_detail=detail,
            latency_ms=latency_ms,
            input_tokens=call_result.input_tokens,
            output_tokens=call_result.output_tokens,
            cost_usd=call_result.cost_usd,
        ))

    duration_s = time.monotonic() - start_time

    # Fair aggregate: exclude skipped from denominator
    # A model that answered 6/6 correctly but skipped 9 scores 100%, not 40%
    attempted = passed_count + failed_count
    aggregate = passed_count / attempted if attempted > 0 else 0.0

    summary = EvalRunSummary(
        run_id=run_id,
        model_id=provider_name,
        model_profile=provider_cfg.profile or provider_name,
        dataset=dataset_name,
        trigger=trigger,
        task_category=cases[0].category if cases else TaskCategory.CLASSIFICATION,
        total_cases=len(cases),
        passed_cases=passed_count,
        failed_cases=failed_count,
        skipped_cases=skipped_count,
        aggregate_score=aggregate,
        duration_s=duration_s,
        results=results,
    )

    logger.info(
        "Eval run %s complete: %d/%d attempted passed (%.0f%%), %d skipped, in %.1fs",
        run_id[:8], passed_count, attempted, aggregate * 100, skipped_count, duration_s,
    )

    # Store to DB if available
    if db is not None:
        from genesis.eval.db import insert_run
        await insert_run(db, summary)

    return summary
