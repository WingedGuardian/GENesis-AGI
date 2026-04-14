"""Promptfoo subprocess wrapper for manual A/B model comparison.

Shells out to `promptfoo eval` with a temporary YAML config.
Used for pre-switch decisions: "Should we move from model A to model B?"

Requires Node.js and promptfoo installed (`npx promptfoo` or global).
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComparisonReport:
    """Results of an A/B model comparison via promptfoo."""

    model_a: str
    model_b: str
    dataset: str
    model_a_score: float
    model_b_score: float
    winner: str  # "a", "b", or "tie"
    details: dict = field(default_factory=dict)
    raw_output: str = ""
    success: bool = True
    error: str = ""


async def compare_models(
    *,
    model_a: str,
    model_b: str,
    dataset_path: Path,
    delay_ms: int = 0,
    timeout_s: int = 300,
    promptfoo_bin: str = "npx promptfoo",
) -> ComparisonReport:
    """Run a promptfoo A/B comparison between two models.

    Args:
        model_a: First model identifier (litellm format, e.g. "groq/llama-3.3-70b-versatile")
        model_b: Second model identifier
        dataset_path: Path to the eval dataset YAML
        delay_ms: Delay between requests (for rate limiting, e.g. 31000 for 2 RPM)
        timeout_s: Subprocess timeout
        promptfoo_bin: Promptfoo binary/command

    Returns:
        ComparisonReport with A/B scores
    """
    # Build promptfoo config
    config = _build_promptfoo_config(model_a, model_b, dataset_path, delay_ms)

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "promptfoo.yaml"
        output_path = Path(tmpdir) / "output.json"
        config_path.write_text(yaml.dump(config, default_flow_style=False))

        cmd = f"{promptfoo_bin} eval -c {config_path} -o {output_path} --no-cache"
        logger.info("Running promptfoo: %s", cmd)

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return ComparisonReport(
                model_a=model_a, model_b=model_b,
                dataset=dataset_path.stem,
                model_a_score=0, model_b_score=0,
                winner="tie",
                success=False,
                error=f"promptfoo timed out after {timeout_s}s",
            )

        if proc.returncode != 0:
            return ComparisonReport(
                model_a=model_a, model_b=model_b,
                dataset=dataset_path.stem,
                model_a_score=0, model_b_score=0,
                winner="tie",
                success=False,
                error=f"promptfoo failed (rc={proc.returncode}): {proc.stderr[:500]}",
                raw_output=proc.stdout[:2000],
            )

        # Parse output
        try:
            results = json.loads(output_path.read_text())
        except (json.JSONDecodeError, FileNotFoundError) as e:
            return ComparisonReport(
                model_a=model_a, model_b=model_b,
                dataset=dataset_path.stem,
                model_a_score=0, model_b_score=0,
                winner="tie",
                success=False,
                error=f"failed to parse promptfoo output: {e}",
                raw_output=proc.stdout[:2000],
            )

        return _parse_results(model_a, model_b, dataset_path.stem, results)


def _build_promptfoo_config(
    model_a: str, model_b: str, dataset_path: Path, delay_ms: int,
) -> dict:
    """Build a promptfoo YAML config for A/B comparison."""
    providers = [
        {"id": model_a, "label": "model_a"},
        {"id": model_b, "label": "model_b"},
    ]
    if delay_ms > 0:
        for p in providers:
            p["delay"] = delay_ms

    return {
        "providers": providers,
        "prompts": ["{{input}}"],
        "tests": str(dataset_path),
        "outputPath": "output.json",
    }


def _parse_results(
    model_a: str, model_b: str, dataset: str, results: dict,
) -> ComparisonReport:
    """Parse promptfoo JSON output into ComparisonReport."""
    try:
        table = results.get("results", {}).get("table", {})
        body = table.get("body", [])

        a_pass = 0
        b_pass = 0
        total = 0

        for row in body:
            outputs = row.get("outputs", [])
            if len(outputs) >= 2:
                total += 1
                if outputs[0].get("pass"):
                    a_pass += 1
                if outputs[1].get("pass"):
                    b_pass += 1

        a_score = a_pass / total if total > 0 else 0
        b_score = b_pass / total if total > 0 else 0

        if a_score > b_score:
            winner = "a"
        elif b_score > a_score:
            winner = "b"
        else:
            winner = "tie"

        return ComparisonReport(
            model_a=model_a, model_b=model_b,
            dataset=dataset,
            model_a_score=a_score,
            model_b_score=b_score,
            winner=winner,
            details={"total": total, "a_pass": a_pass, "b_pass": b_pass},
            success=True,
        )
    except Exception as e:
        return ComparisonReport(
            model_a=model_a, model_b=model_b,
            dataset=dataset,
            model_a_score=0, model_b_score=0,
            winner="tie",
            success=False,
            error=f"failed to parse results: {e}",
        )
