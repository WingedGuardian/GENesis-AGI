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
from dataclasses import dataclass, field
from pathlib import Path

from genesis.attention.clarity import is_blip
from genesis.attention.config import (
    DEFAULT_CONFIG_PATH,
    AttentionConfig,
    default_config_dict,
    load_config,
)
from genesis.attention.consumers import ShadowStoreConsumer
from genesis.attention.engine import evaluate
from genesis.attention.snapshot import pull_snapshot
from genesis.attention.sources import SnapshotSource
from genesis.attention.types import AttentionEvent, EngineState

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


async def run_shadow(
    snapshot_path: str | Path,
    config: AttentionConfig,
    *,
    snapshot_id: str = "adhoc",
    sample_n: int = 25,
    consumer: ShadowStoreConsumer | None = None,
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
        if is_blip(utt.rms, utt.duration_s, utt.n_tokens):
            continue
        evaluated += 1
        state, ev = evaluate(utt, state, config)
        if ev is None:
            continue
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
        print(f"[{s['activation']:>10}] score={s['score']:.3f} clarity={s['clarity']:.2f} "
              f"u={s['is_user']} trig={s['triggers']} sup={s['suppressors']}")
        print(f"             text: {s['text'][:150]!r}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Attention engine offline shadow runner")
    ap.add_argument("--snapshot", help="use an existing snapshot .db instead of pulling")
    ap.add_argument("--config", help="attention_config.json (default: ~/.genesis overlay or built-in)")
    ap.add_argument("--samples", type=int, default=25)
    ap.add_argument("--persist", action="store_true", help="write events to attention_events")
    ap.add_argument("--db", help="genesis.db path for --persist (default: genesis_db_path())")
    args = ap.parse_args()

    asyncio.run(_run_cli(args))


async def _run_cli(args) -> None:
    cfg = load_runner_config(args.config)
    logger.info("config version=%s aliases=%s domain_kw=%d", cfg.version, cfg.aliases, len(cfg.domain_keywords))
    if args.snapshot:
        path = Path(args.snapshot).expanduser()
        sid = path.stem
    else:
        sid, path = await pull_snapshot()

    consumer = None
    if args.persist:
        from genesis.env import genesis_db_path
        db_path = args.db or str(genesis_db_path())
        consumer = ShadowStoreConsumer(db_path, snapshot_id=sid, config_version=cfg.version)
        logger.info("persisting to %s", db_path)
    report = await run_shadow(path, cfg, snapshot_id=sid, sample_n=args.samples, consumer=consumer)
    print_report(report)


if __name__ == "__main__":
    main()
