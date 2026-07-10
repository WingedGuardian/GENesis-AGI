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
from genesis.experimentation.standalone_router import (
    DEFAULT_JUDGE_PROVIDER,
    StandaloneLiteLLMRouter,
)

logger = logging.getLogger(__name__)

DEFAULT_GOLDEN = Path.home() / ".genesis" / "output" / "reflection_quality_golden.jsonl"


async def run(golden_path: Path) -> None:
    """Run calibration and print results."""
    from genesis.eval.calibration import run_calibration

    router = StandaloneLiteLLMRouter(DEFAULT_JUDGE_PROVIDER)

    try:
        result = await run_calibration(
            rubric="reflection_quality",
            golden_set_path=golden_path,
            router=router,
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
        "--golden", type=Path, default=DEFAULT_GOLDEN,
        help=f"Path to golden set JSONL (default: {DEFAULT_GOLDEN})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Suppress litellm noise
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)

    asyncio.run(run(args.golden))


if __name__ == "__main__":
    main()
