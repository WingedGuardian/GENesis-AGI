"""Run calibration for the reflection_quality rubric.

Validates that the judge call site (DeepSeek V4 via the Genesis Router)
agrees with the golden set labels at >= 80%.

This script requires a running Genesis runtime (for the Router) OR
can operate standalone with litellm + a lightweight router wrapper.

Usage::

    python -m genesis.eval.run_reflection_calibration [--golden PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

# The standalone Router shim lives in experimentation.standalone_router
# (LiteLLMDelegate-backed, provider-name selection, 429 retries). This script
# previously carried its own inline copy — refactored onto the shared one
# (the cleanup its docstring tracked). The bench harness uses the same shim.
from genesis.eval.calibration import DEFAULT_REFLECTION_REFERENCE
from genesis.experimentation.standalone_router import (
    DEFAULT_JUDGE_PROVIDER,
    StandaloneLiteLLMRouter,
)

logger = logging.getLogger(__name__)

DEFAULT_GOLDEN = DEFAULT_REFLECTION_REFERENCE


async def run(golden_path: Path, *, strict_references: bool = False) -> None:
    """Run calibration and print results."""
    from genesis.eval.calibration import run_calibration

    if strict_references:
        from genesis.eval.calibration import _load_golden_set
        from genesis.eval.rubrics import get_rubric

        # Reject the entire file before allocating a provider client.
        _load_golden_set(golden_path, strict_rubric=get_rubric("reflection_quality"))
    router = StandaloneLiteLLMRouter(DEFAULT_JUDGE_PROVIDER)

    try:
        result = await run_calibration(
            rubric="reflection_quality",
            golden_set_path=golden_path,
            router=router,
            strict_references=strict_references,
        )
    finally:
        await router.close()

    # Print summary
    print(f"\n{'='*60}")
    print("Reflection Quality Rubric Calibration")
    print(f"{'='*60}")
    print(f"Rubric: {result.rubric_name} v{result.rubric_version}")
    print(f"Cases: {result.total_cases}")
    print(f"Agreed: {result.agreed_cases}")
    print(f"Disagreed: {result.disagreed_cases}")
    print(f"Errors: {result.error_cases}")
    print(f"Agreement: {result.agreement_rate:.1%}")
    print(f"Threshold: {result.threshold:.1%}")
    print(f"Verdict: {'PASS ✓' if result.threshold_met else 'FAIL ✗'}")
    print(f"Duration: {result.duration_s:.1f}s")

    if result.disagreed_cases > 0:
        print("\nDisagreements:")
        for outcome in result.outcomes:
            if not outcome.agreed and not outcome.error:
                label = "pass" if outcome.user_passed else "fail"
                judge = "pass" if outcome.judge_passed else "fail"
                print(f"  {outcome.case_id[:12]}... golden={label} judge={judge} "
                      f"score={outcome.judge_score:.2f}")
                if outcome.rationale:
                    print(f"    {outcome.rationale[:100]}")

    if result.error_cases > 0:
        print("\nErrors:")
        for outcome in result.outcomes:
            if outcome.error:
                print(f"  {outcome.case_id[:12]}... {outcome.error[:100]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run reflection_quality rubric calibration",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help=f"Path to golden set JSONL (default: {DEFAULT_GOLDEN})",
    )
    parser.add_argument(
        "--strict-references",
        action="store_true",
        help="Validate declared human reference provenance before any judge calls",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Suppress litellm noise
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)

    asyncio.run(run(args.golden, strict_references=args.strict_references))


if __name__ == "__main__":
    main()
