"""skill_replay_run MCP tool — replay a skill edit against its golden suite (WS1).

Runs the held-out regression gate for ONE skill: replays the frozen golden task
suite against the OLD vs NEW SKILL.md content (control=OLD, treatment=NEW) and
RETURNS + LOGS a recommend-only verdict (net_positive / regression /
inconclusive). It NEVER blocks or applies an edit — ``autonomous_action`` is
always False. Gated by the ``skill_evolution_gate.replay`` lever and the
``GENESIS_SKILL_EVOLUTION_GATE_OFF`` hard kill.

OLD content resolves from the cognitive-ledger pre-image of the last
``skill_evolution`` edit (the applicator captures it via record_file_modification)
unless passed explicitly; NEW defaults to the on-disk SKILL.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _default_suite_path(skill_name: str) -> Path:
    from genesis.env import genesis_home

    return genesis_home() / "eval" / "skill_golden" / f"{skill_name}.jsonl"


async def _resolve_ledger_old_content(db, target_path: str) -> str | None:
    """OLD content = ``prior_content`` of the most-recent ``skill_evolution`` edit
    to ``target_path`` (the pre-image the applicator captured before overwriting)."""
    if db is None:
        return None
    try:
        from genesis.db.crud import cognitive_file_modifications as cfm

        rows = await cfm.recent(db, limit=50, actor="skill_evolution")
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to explicit arg
        logger.warning("skill_replay_run: ledger pre-image lookup failed", exc_info=True)
        return None
    for row in rows:
        if row.get("target_path") == target_path:
            return row.get("prior_content")
    return None


def _err(message: str) -> dict:
    return {"status": "error", "autonomous_action": False, "message": message}


async def _impl_skill_replay_run(
    *,
    skill_name: str,
    old_content: str | None = None,
    new_content: str | None = None,
    suite_path: str | None = None,
    model: str = "sonnet",
    effort: str = "medium",
    limit: int | None = None,
) -> dict:
    from genesis.env import skill_gate_off
    from genesis.learning.skills.skill_gate_config import (
        skill_replay_config,
        skill_replay_mode,
    )

    if skill_gate_off() or skill_replay_mode() == "off":
        return {
            "status": "skipped",
            "reason": "replay gate off (skill_evolution_gate.replay=off or env kill)",
            "autonomous_action": False,
            "recommend_only": True,
        }

    from genesis.cc.types import VALID_EFFORT_NAMES, VALID_MODEL_NAMES, CCModel, EffortLevel
    from genesis.learning.skills.wiring import get_skill_path, load_skill

    if model not in VALID_MODEL_NAMES:
        return _err(f"invalid model {model!r}; valid: {', '.join(sorted(VALID_MODEL_NAMES))}")
    if effort not in VALID_EFFORT_NAMES:
        return _err(f"invalid effort {effort!r}; valid: {', '.join(sorted(VALID_EFFORT_NAMES))}")

    path = get_skill_path(skill_name)
    if path is None:
        return _err(f"skill not found: {skill_name}")

    if new_content is None:
        try:
            new_content = load_skill(skill_name)
        except Exception:  # noqa: BLE001 — never crash the tool (bad encoding / TOCTOU)
            logger.warning(
                "skill_replay_run: reading SKILL.md for %s failed", skill_name, exc_info=True
            )
            return _err(f"could not read current SKILL.md for {skill_name}")
    if not new_content:
        return _err(f"no NEW content resolved for {skill_name}")

    import genesis.mcp.health_mcp as health_mcp_mod

    _service = getattr(health_mcp_mod, "_service", None)
    db = getattr(_service, "_db", None) if _service is not None else None

    if old_content is None:
        old_content = await _resolve_ledger_old_content(db, str(path))
    if not old_content:
        return _err(
            "no OLD content — pass old_content explicitly, or ensure a "
            "skill_evolution ledger pre-image exists for this skill's SKILL.md"
        )

    suite = Path(suite_path) if suite_path else _default_suite_path(skill_name)
    if not suite.exists():
        return _err(
            f"golden suite not found: {suite}. Author it with "
            f"`python -m genesis.eval.skill_golden_set --skill {skill_name}`"
        )

    from genesis.eval.skill_replay.persist import (
        log_replay_observation,
        persist_skill_replay_summary,
    )
    from genesis.eval.skill_replay.runner import run_skill_replay
    from genesis.eval.skill_replay.types import SkillReplayConfig

    knobs = skill_replay_config()
    cfg = SkillReplayConfig(epsilon=knobs["epsilon"], min_pairs=knobs["min_pairs"])

    started = datetime.now(UTC)
    try:
        report = await run_skill_replay(
            skill_name=skill_name,
            old_content=old_content,
            new_content=new_content,
            tasks_path=suite,
            model=CCModel(model),
            effort=EffortLevel(effort),
            config=cfg,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the tool
        logger.warning("skill_replay_run failed", exc_info=True)
        return _err(f"{type(exc).__name__}: {exc}")

    duration_s = (datetime.now(UTC) - started).total_seconds()
    if db is not None:
        try:
            await persist_skill_replay_summary(db, report, duration_s=duration_s)
            await log_replay_observation(db, report, now=datetime.now(UTC).isoformat())
        except Exception:  # noqa: BLE001 — persistence is non-fatal to the measurement
            logger.warning("skill_replay_run: persistence failed", exc_info=True)

    v = report.verdict
    return {
        "status": "ok",
        "autonomous_action": False,
        "recommend_only": True,
        "skill_name": skill_name,
        "verdict": v.verdict if v else None,
        "n_complete": v.n_complete if v else 0,
        "n_regressions": v.n_regressions if v else 0,
        "n_improvements": v.n_improvements if v else 0,
        "note": v.note if v else "",
        "score_winrate": v.score_winrate if v else {},
        "control_run_id": report.control_run_id,
        "treatment_run_id": report.treatment_run_id,
        "task_set_version": report.task_set_version,
        "prod_delta_clean": report.prod_delta.get("clean"),
        "notes": report.notes,
    }


@mcp.tool()
async def skill_replay_run(
    skill_name: str,
    old_content: str = "",
    new_content: str = "",
    suite_path: str = "",
    model: str = "sonnet",
    effort: str = "medium",
    limit: int = 0,
) -> dict:
    """Replay a skill edit (WS1 held-out gate): run the skill's frozen golden
    suite against OLD vs NEW SKILL.md content and RETURN + LOG a recommend-only
    verdict (net_positive / regression / inconclusive).

    RECOMMEND-ONLY — it never blocks or applies an edit (autonomous_action
    False); it logs the verdict as a ``skill_replay_verdict`` observation. Heavy
    (spawns CC sessions), so run it out-of-band. Gated by
    ``skill_evolution_gate.replay`` + ``GENESIS_SKILL_EVOLUTION_GATE_OFF``.

    Args:
        skill_name: the skill whose edit to screen (e.g. "voice-master").
        old_content: pre-edit SKILL.md body; default = the cognitive-ledger
            pre-image of the last skill_evolution edit.
        new_content: proposed SKILL.md body; default = the on-disk current file.
        suite_path: golden JSONL; default ~/.genesis/eval/skill_golden/<skill>.jsonl.
        model / effort: CC arm model tier / effort (default sonnet / medium).
        limit: cap the number of golden tasks (0 = all).
    """
    return await _impl_skill_replay_run(
        skill_name=skill_name,
        old_content=old_content or None,
        new_content=new_content or None,
        suite_path=suite_path or None,
        model=model,
        effort=effort,
        limit=limit or None,
    )
