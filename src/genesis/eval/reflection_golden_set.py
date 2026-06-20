"""Generate a synthetic golden set for the reflection_quality rubric.

Samples deep reflection observations from the DB, uses an LLM judge to
grade each one, then writes the results to a JSONL file suitable for
``calibration.run_calibration()``.

The golden set is written to ``~/.genesis/output/`` (NOT the repo)
because it contains private system context.

Usage::

    python -m genesis.eval.reflection_golden_set [--count 50] [--output PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Remap Genesis env-var names → litellm-expected names.
# Constructed to avoid false-positive hits from detect-secrets.
_AK = "API_KEY"
_GENESIS_TO_LITELLM = {
    f"{_AK}_DEEPSEEK": f"DEEPSEEK_{_AK}",
    f"{_AK}_OPENROUTER": f"OPENROUTER_{_AK}",
    f"GOOGLE_{_AK}": f"GEMINI_{_AK}",
}

_secrets_loaded = False


def _ensure_secrets() -> None:
    """Load secrets.env and remap key names for litellm compatibility."""
    global _secrets_loaded
    if _secrets_loaded:
        return
    _secrets_loaded = True

    import os

    secrets_path = Path.home() / "genesis" / "secrets.env"
    if not secrets_path.exists():
        return

    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        os.environ.setdefault(key, value)
        # Remap to litellm-expected names
        if key in _GENESIS_TO_LITELLM:
            os.environ.setdefault(_GENESIS_TO_LITELLM[key], value)


DEFAULT_OUTPUT = Path.home() / ".genesis" / "output" / "reflection_quality_golden.jsonl"
DEFAULT_COUNT = 50

# Threshold from the rubric — observations scoring >= this are "pass".
PASS_THRESHOLD = 0.6


async def _sample_observations(db: aiosqlite.Connection, count: int) -> list[dict]:
    """Sample deep reflection observations stratified by age."""
    cursor = await db.execute(
        """SELECT id, content, priority, source, created_at,
                  retrieved_count, influenced_action, resolved,
                  resolution_notes
           FROM observations
           WHERE type = 'reflection_observation'
             AND source IN ('deep_reflection', 'cc_reflection_deep')
           ORDER BY created_at DESC""",
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    all_obs = [dict(zip(cols, row, strict=True)) for row in rows]

    if len(all_obs) <= count:
        return all_obs

    # Stratified sample: 1/3 newest, 1/3 oldest, 1/3 random middle
    third = count // 3
    newest = all_obs[:third]
    oldest = all_obs[-third:]
    middle_pool = all_obs[third:-third]
    middle = random.sample(middle_pool, min(count - 2 * third, len(middle_pool)))
    sampled = newest + oldest + middle

    seen = set()
    deduped = []
    for obs in sampled:
        if obs["id"] not in seen:
            seen.add(obs["id"])
            deduped.append(obs)
    return deduped[:count]


async def _get_session_context(db: aiosqlite.Connection, obs_created_at: str) -> str:
    """Reconstruct approximate session context for an observation.

    Deep reflections are triggered by awareness ticks. We find the
    closest tick before the observation's creation time and use its
    signals as context.
    """
    cursor = await db.execute(
        """SELECT scores_json, classified_depth, signals_json,
                  trigger_reason
           FROM awareness_ticks
           WHERE created_at <= ?
             AND classified_depth IN ('Deep', 'Strategic')
           ORDER BY created_at DESC LIMIT 1""",
        (obs_created_at,),
    )
    row = await cursor.fetchone()
    if not row:
        return "No awareness tick context available for this observation."

    scores_json, depth, signals_json, trigger_reason = row
    parts = [f"Reflection depth: {depth}"]
    if trigger_reason:
        parts.append(f"Trigger: {trigger_reason}")
    if scores_json:
        try:
            scores = json.loads(scores_json)
            parts.append(f"Scores: {json.dumps(scores, indent=2)}")
        except json.JSONDecodeError:
            parts.append(f"Raw scores: {scores_json[:500]}")
    if signals_json:
        try:
            signals = json.loads(signals_json)
            parts.append(f"Signals: {json.dumps(signals, indent=2)}")
        except json.JSONDecodeError:
            pass
    return "\n".join(parts)


async def _grade_observation(
    observation_content: str,
    session_context: str,
) -> tuple[float, str, str]:
    """Grade a reflection observation using litellm directly.

    Uses the judge call site's model chain (DeepSeek V4) for cost
    efficiency. Falls back gracefully on error.

    Returns (score, rationale, model_used).
    """
    import litellm

    from genesis.eval.rubrics import get_rubric

    # Load secrets if not already in env
    _ensure_secrets()

    rubric = get_rubric("reflection_quality")
    prompt = rubric.prompt_template.format(
        actual=observation_content,
        expected="deep_reflection_observation",
        session_context=session_context,
    )

    # Try providers in order: OpenRouter (DeepSeek V4), Gemini Flash, Groq
    models = [
        "openrouter/deepseek/deepseek-chat-v3-0324",
        "gemini/gemini-2.0-flash",
        "groq/llama-3.3-70b-versatile",
    ]
    last_exc = None
    response = None
    used_model = None
    for model in models:
        try:
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            used_model = model
            break
        except Exception as exc:
            last_exc = exc
            logger.debug("Provider %s failed: %s", model, exc)
            continue

    if response is None:
        raise last_exc or RuntimeError("All judge providers failed")

    raw = response.choices[0].message.content or ""

    # Parse JSON — handle markdown fences
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    try:
        parsed = json.loads(text)
        if "score" not in parsed:
            # Valid JSON but no 'score' — raise so the caller counts it as an
            # error rather than mislabeling the golden case with a silent 0.0.
            raise ValueError("judge response missing required 'score' key")
        score = float(parsed["score"])
        score = max(0.0, min(1.0, score))
        rationale = str(parsed.get("rationale", ""))
        return score, rationale, used_model or "unknown"
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Parse failure: %s; raw=%r", exc, raw[:200])
        # Raise so the caller counts this as an error, not a "fail" label.
        raise ValueError(f"Judge response parse failure: {exc}") from exc


async def generate_golden_set(count: int, output_path: Path) -> dict:
    """Generate the golden set and write to output_path.

    Returns a summary dict with counts and pass/fail distribution.
    """
    # genesis_db_path() resolves relative to CWD which may be a worktree.
    # The real DB is always at ~/genesis/data/genesis.db.
    db_path = str(Path.home() / "genesis" / "data" / "genesis.db")
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA busy_timeout=5000")

    try:
        logger.info("Sampling %d reflection observations...", count)
        observations = await _sample_observations(db, count)
        logger.info("Sampled %d observations", len(observations))

        results = []
        passed_count = 0
        failed_count = 0
        error_count = 0

        for i, obs in enumerate(observations, 1):
            obs_id = obs["id"]
            content = obs["content"]
            created_at = obs["created_at"]

            session_context = await _get_session_context(db, created_at)

            try:
                score, rationale, model_used = await _grade_observation(
                    content, session_context,
                )
            except Exception as exc:
                logger.warning("Error grading %s: %s", obs_id, exc)
                error_count += 1
                continue

            user_passed = score >= PASS_THRESHOLD
            if user_passed:
                passed_count += 1
            else:
                failed_count += 1

            case = {
                "id": obs_id,
                "actual": content,
                "expected": "deep_reflection_observation",
                "user_passed": user_passed,
                "scorer_config": {
                    "rubric_name": "reflection_quality",
                    "session_context": session_context,
                },
                "_judge_score": score,
                "_judge_rationale": rationale,
                "_judge_model": model_used,
                "_created_at": created_at,
                "_priority": obs["priority"],
                "_retrieved_count": obs["retrieved_count"],
            }
            results.append(case)

            if i % 10 == 0:
                logger.info(
                    "Progress: %d/%d (pass=%d, fail=%d, error=%d)",
                    i, len(observations), passed_count, failed_count,
                    error_count,
                )
    finally:
        await db.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        f.write("# Golden set for the reflection_quality rubric.\n")
        f.write(f"# Generated: {count} sampled, {len(results)} graded\n")
        f.write(f"# Pass: {passed_count}, Fail: {failed_count}, Error: {error_count}\n")
        f.write("#\n")
        for case in results:
            f.write(json.dumps(case) + "\n")

    summary = {
        "sampled": len(observations),
        "graded": len(results),
        "passed": passed_count,
        "failed": failed_count,
        "errors": error_count,
        "output": str(output_path),
    }
    logger.info("Golden set written to %s: %s", output_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate reflection_quality golden set",
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help=f"Number of observations to sample (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output JSONL path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = asyncio.run(generate_golden_set(args.count, args.output))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
