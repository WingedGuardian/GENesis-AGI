"""Offline shadow runner — pull an ambient snapshot, replay the L1 gate over it, report.

v1 (spike): prints the fire-rate + activation/trigger breakdown + sample perked windows
so we can eyeball whether the deterministic gate is sane BEFORE building the persistence
layer. Persistence into ``attention_events`` is added after the spike checkpoint.

Invoke (NOT an MCP tool — a direct CLI, per feedback_prefer_direct_over_mcp_tools):
    python -m genesis.attention.runner [--snapshot PATH] [--config PATH] [--samples N]

The transcript text shown in samples is read from the transient snapshot for OFFLINE
eyeballing only; it is never persisted to genesis.db (firewall).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.attention.clarity import is_blip
from genesis.attention.config import (
    DEFAULT_CONFIG_PATH,
    AttentionConfig,
    Thresholds,
    default_config_dict,
    load_config,
)
from genesis.attention.consumers import ShadowConsumer, ShadowStoreConsumer
from genesis.attention.engine import evaluate
from genesis.attention.snapshot import pull_snapshot
from genesis.attention.sources import SnapshotSource
from genesis.attention.types import Activation, AttentionEvent, EngineState

if TYPE_CHECKING:
    from genesis.attention.sampler import AttentionSampler

logger = logging.getLogger(__name__)


@dataclass
class ShadowReport:
    snapshot_id: str
    total_rows: int
    evaluated: int  # non-blip
    events: int
    by_activation: dict = field(default_factory=dict)
    by_trigger: dict = field(default_factory=dict)
    by_suppressor: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)
    persisted: int = 0

    @property
    def fire_rate(self) -> float:
        return self.events / self.evaluated if self.evaluated else 0.0


def load_runner_config(path: str | None) -> AttentionConfig:
    """--config path, else the ~/.genesis overlay, else the built-in starter default."""
    if path:
        return load_config(path)
    overlay = Path(DEFAULT_CONFIG_PATH).expanduser()
    if overlay.exists():
        return load_config(overlay)
    return AttentionConfig.from_dict(default_config_dict())


def _graduates_to_l15(ev: AttentionEvent, thresholds: Thresholds) -> bool:
    """Whether an event should be scored by L1.5. Gate on ACTIVATION, not raw ``ev.score``:
    a bare-HARD fire has ``score == 0.0`` (no soft co-trigger) yet is the MOST certain
    perk-up, and SUPPRESSED events are vetoes not worth an LLM call. So HARD always
    graduates; SOFT graduates above the ``l15_graduation`` cost-floor; SUPPRESSED never."""
    if ev.activation == Activation.HARD:
        return True
    if ev.activation == Activation.SOFT:
        return ev.score >= thresholds.l15_graduation
    return False


async def run_shadow(
    snapshot_path: str | Path,
    config: AttentionConfig,
    *,
    snapshot_id: str = "adhoc",
    sample_n: int = 25,
    consumer: ShadowConsumer | None = None,
    sampler: AttentionSampler | None = None,
) -> ShadowReport:
    src = SnapshotSource(snapshot_path)
    state = EngineState()
    utt_by_id: dict[int, object] = {}
    total = evaluated = 0
    events: list[AttentionEvent] = []
    by_act, by_trig, by_supp = Counter(), Counter(), Counter()

    for utt in src.iter_utterances():
        total += 1
        utt_by_id[utt.id] = utt
        if is_blip(utt.rms, utt.duration_s, utt.n_tokens, has_audio=utt.has_audio):
            continue
        evaluated += 1
        state, ev = evaluate(utt, state, config)
        if ev is None:
            continue
        # L1.5 (opt-in): score the fire's window via a cheap LLM and attach the
        # {real, perk} verdict BEFORE the event is buffered/persisted. state.window is a
        # fire-time snapshot (immutable utterances; last element is this trigger utt);
        # its text goes to the LLM in memory only — never to genesis.db (firewall).
        if sampler is not None and _graduates_to_l15(ev, config.thresholds):
            verdict = await sampler.sample(list(state.window), config)
            ev = replace(ev, l15_verdict=verdict)
        events.append(ev)
        if consumer is not None:
            consumer.add(ev)
        by_act[ev.activation.value] += 1
        for h in ev.triggers_fired:
            by_trig[h.name] += 1
        for s in ev.suppressors:
            by_supp[s] += 1

    persisted = await consumer.flush() if consumer is not None else 0
    samples = []
    step = max(1, len(events) // sample_n) if events else 1
    for ev in events[::step][:sample_n]:
        trg = utt_by_id.get(ev.window_ref.utt_ids[-1])
        samples.append({
            "ts": ev.ts,
            "activation": ev.activation.value,
            "score": ev.score,
            "clarity": ev.clarity,
            "triggers": [h.name for h in ev.triggers_fired],
            "suppressors": list(ev.suppressors),
            "is_user": getattr(trg, "is_user", None),
            "l15": ev.l15_verdict,   # {real, perk} when --l15 ran, else None
            "text": getattr(trg, "text", ""),
        })
    return ShadowReport(
        snapshot_id, total, evaluated, len(events),
        dict(by_act), dict(by_trig), dict(by_supp), samples, persisted,
    )


def print_report(r: ShadowReport) -> None:
    print(f"\n=== ATTENTION SHADOW — snapshot {r.snapshot_id} ===")
    print(f"rows={r.total_rows}  evaluated(non-blip)={r.evaluated}  "
          f"blips_skipped={r.total_rows - r.evaluated}  events={r.events}  "
          f"fire_rate={r.fire_rate:.1%}  persisted={r.persisted}")
    print(f"by_activation: {r.by_activation}")
    print(f"by_trigger:    {dict(sorted(r.by_trigger.items(), key=lambda x: -x[1]))}")
    print(f"by_suppressor: {r.by_suppressor}")
    print(f"\n--- {len(r.samples)} sample events (text shown for OFFLINE eyeballing only) ---")
    for s in r.samples:
        l15 = f" l15={s['l15']}" if s.get("l15") is not None else ""
        print(f"[{s['activation']:>10}] score={s['score']:.3f} clarity={s['clarity']:.2f} "
              f"u={s['is_user']} trig={s['triggers']} sup={s['suppressors']}{l15}")
        print(f"             text: {s['text'][:150]!r}")


def _snapshot_id_from_path(path: Path) -> str:
    """Canonical BARE snapshot_id from a snapshot filename.

    ``pull_snapshot()`` names files ``ambient_{id}.db`` but stores the bare ``id`` in
    ``attention_events.snapshot_id`` / ``window_ref``. So ``--snapshot`` must strip the
    ``ambient_`` prefix to match — otherwise the dashboard reveal (which reconstructs
    ``ambient_{snapshot_id}.db``) would look for ``ambient_ambient_...db`` and 410."""
    return path.stem.removeprefix("ambient_")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Attention engine offline shadow runner")
    ap.add_argument("--snapshot", help="use an existing snapshot .db instead of pulling")
    ap.add_argument("--config", help="attention_config.json (default: ~/.genesis overlay or built-in)")
    ap.add_argument("--samples", type=int, default=25)
    ap.add_argument("--persist", action="store_true", help="write events to attention_events")
    ap.add_argument(
        "--l15", action="store_true",
        help="score graduated (HARD + above-floor SOFT) windows via the L1.5 "
             "'attention_salience' LLM call-site. PRIVACY: this SENDS the window's ambient "
             "transcript text to an external free LLM provider. Off by default.",
    )
    ap.add_argument("--db", help="genesis.db path for --persist (default: genesis_db_path())")
    args = ap.parse_args()

    asyncio.run(_run_cli(args))


async def _run_cli(args) -> None:
    cfg = load_runner_config(args.config)
    logger.info("config version=%s aliases=%s domain_kw=%d", cfg.version, cfg.aliases, len(cfg.domain_keywords))
    if args.snapshot:
        path = Path(args.snapshot).expanduser()
        sid = _snapshot_id_from_path(path)
    else:
        sid, path = await pull_snapshot()

    consumer = None
    if args.persist:
        from genesis.env import genesis_db_path
        db_path = args.db or str(genesis_db_path())
        consumer = ShadowStoreConsumer(db_path, snapshot_id=sid, config_version=cfg.version)
        logger.info("persisting to %s", db_path)

    sampler = None
    if args.l15:
        from genesis.attention.sampler import CALL_SITE, AttentionSampler
        from genesis.routing.standalone import _build_standalone_router
        router = _build_standalone_router()
        if router is None:
            logger.error("--l15: could not build a router (missing config/secrets); running WITHOUT L1.5")
        else:
            sampler = AttentionSampler(router)
            logger.info("L1.5 ENABLED: scoring graduated windows via '%s' — sends window "
                        "transcript text to an external LLM (egress).", CALL_SITE)

    report = await run_shadow(
        path, cfg, snapshot_id=sid, sample_n=args.samples, consumer=consumer, sampler=sampler,
    )
    print_report(report)


if __name__ == "__main__":
    main()
