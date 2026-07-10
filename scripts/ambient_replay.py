#!/usr/bin/env python3
"""WS-C replay harness: run the ambient pipeline over a recorded session.

Acceptance instrument for the session-awareness layer (NOT a CI test).
Streams a CC session transcript, feeds each GENUINE user turn through
the exact live gates (the hook's envelope + keyword functions, imported
from the hook module itself), folds the theme EMA, traces every trigger
decision, and on each fire runs the real retrieve+rank lanes (read-only)
to capture the candidate set.

KILL CRITERION (OMI incident, session 246fe52b…):
    PASS = memory 9d36f039… appears in a fire's candidate set within
    the first 10 genuine turns. FAIL → tune constants (authorized) or
    STOP and redesign if it's structural.

Usage:
    python scripts/ambient_replay.py --session-id 246fe52b-... \
        [--target 9d36f039] [--max-genuine-turns N] [--no-retrieve]
        [--transcript /path/to.jsonl]

Timestamps come from the transcript (never the wall clock); embeddings
are cached by content hash in ~/.genesis/session_awareness/ so tuning
iterations are cheap. Everything is read-only against production
stores — the replay never writes session state, Qdrant, or the DB.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.env import repo_root  # noqa: E402  (needs the sys.path insert)

# CC keys a project's transcript dir by the project path with '/' → '-';
# derive from the resolved repo root (GENESIS_REPO_ROOT-aware) so the
# default works on any install. --transcript overrides.
_TRANSCRIPT_DIR = (
    Path.home() / ".claude" / "projects" / str(repo_root()).replace("/", "-")
)
_CACHE_PATH = (
    Path.home() / ".genesis" / "session_awareness" / "replay_embed_cache.json"
)


def _load_hook_module():
    """Import the live hook for gate parity (_extract_keywords, etc.)."""
    import os

    # The hook exits at import inside dispatched Genesis sessions; the
    # replay is never that context, so clear the guard before exec.
    os.environ.pop("GENESIS_CC_SESSION", None)
    path = REPO_DIR / "scripts" / "proactive_memory_hook.py"
    spec = importlib.util.spec_from_file_location("proactive_memory_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iter_user_turns(transcript: Path):
    """Yield (timestamp, text, recent_files) for user turns, streamed.

    recent_files = file paths from assistant tool calls (Edit/Write/Read/
    NotebookEdit) since the PREVIOUS user turn — the replay equivalent of
    the live hook's PostToolUse file-context tracking.
    """
    pending_files: list[str] = []
    with transcript.open() as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            if etype == "assistant":
                msg = entry.get("message") or {}
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        fp = (block.get("input") or {}).get("file_path")
                        if fp:
                            pending_files.append(fp)
                continue
            if etype != "user" or entry.get("isMeta"):
                continue
            msg = entry.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                text = "\n".join(p for p in parts if p)
            else:
                continue
            if not text.strip():
                continue
            files = pending_files[-20:]
            pending_files = []
            yield entry.get("timestamp"), text, files


class EmbedCache:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}
        self.misses = 0

    async def embed(self, provider, text: str) -> list[float] | None:
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self.data:
            return self.data[key]
        vec = await provider.embed(text)
        if vec:
            self.data[key] = vec
            self.misses += 1
            if self.misses % 10 == 0:
                self.save()
        return vec

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


async def replay(args) -> dict:
    from genesis.session_awareness.accumulator import fold_turn, should_fold
    from genesis.session_awareness.statefiles import empty_state
    from genesis.session_awareness.trigger import check_fire, record_fire, stability

    hook = _load_hook_module()
    transcript = (
        Path(args.transcript)
        if args.transcript
        else _TRANSCRIPT_DIR / f"{args.session_id}.jsonl"
    )
    if not transcript.exists():
        raise SystemExit(f"transcript not found: {transcript}")

    # API keys live in secrets.env (env-only resolution) — load from the
    # MAIN repo via genesis.env, not this worktree (the hook's own
    # load_dotenv points at its REPO_DIR, which has no secrets file in a
    # worktree). Cloud-first chain = the hook's recall path, minus the
    # 3s interactive budget (offline harness; backend timeouts apply).
    from dotenv import load_dotenv

    from genesis import env as genesis_env

    load_dotenv(genesis_env.secrets_path())

    from genesis.memory.embeddings import EmbeddingProvider

    provider = EmbeddingProvider(
        backends=EmbeddingProvider.build_chain(ollama_first=False),
        cache_dir=None,
    )
    cache = EmbedCache(_CACHE_PATH)

    state = empty_state(args.session_id)
    trail: dict = {"msg_count": 0, "pivots": [], "last_keywords": []}
    genuine = 0
    trace: list[dict] = []
    fires: list[dict] = []
    t_mention: int | None = None  # first turn the target entity is in the ledger
    as_of_cutoff: str | None = None if args.as_of in (None, "auto") else args.as_of

    db = qdrant = None
    if not args.no_retrieve:
        import aiosqlite
        from qdrant_client import QdrantClient

        from genesis import env as genesis_env

        db = await aiosqlite.connect(
            f"file:{genesis_env.genesis_db_path()}?mode=ro", uri=True,
        )
        qdrant = QdrantClient(url=genesis_env.qdrant_url(), timeout=30)

    try:
        for ts, text, recent_files in iter_user_turns(transcript):
            if hook._is_harness_envelope(text):
                continue
            keywords = hook._extract_keywords(text)
            if len(keywords) < hook._MIN_PROMPT_WORDS:
                continue
            genuine += 1
            if args.max_genuine_turns and genuine > args.max_genuine_turns:
                break

            # Same Jaccard pivot logic as the live trail
            trail["msg_count"] += 1
            pivoted = bool(hook._detect_pivot(keywords, trail))
            if pivoted:
                trail["pivots"].append({"at_msg": trail["msg_count"]})
            if keywords:
                trail["last_keywords"] = keywords

            now = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            now_iso = now.isoformat()
            if args.as_of == "auto" and as_of_cutoff is None:
                as_of_cutoff = now_iso  # first genuine turn = session start
            thematic = should_fold(text, keywords)
            vec = await cache.embed(provider, text) if thematic else None
            if vec:
                file_kws = (
                    hook._keywords_from_files(recent_files) if recent_files else []
                )
                fold_turn(
                    state, vec, keywords, file_kws,
                    pivoted=pivoted, now_iso=now_iso,
                )
                fired, reason = check_fire(state, now)
            else:
                fired, reason = False, ("low_signal" if not thematic else "no_embed")

            row = {
                "turn": genuine,
                "ts": now_iso,
                "prompt": text[:80].replace("\n", " "),
                "pivot": pivoted,
                "embedded": bool(vec),
                "ema_turns": state.get("ema_turns", 0),
                "stability": round(stability(state.get("ring", [])), 4),
                "reason": reason,
            }
            trace.append(row)

            # Track first turn the target entity enters the ledger
            # (t_mention anchors the E4 acceptance window).
            if (
                args.target_entity
                and t_mention is None
                and args.target_entity in state.get("entities", {})
            ):
                t_mention = genuine

            if fired:
                record_fire(state, now_iso)
                fire_rec: dict = {"turn": genuine, "ts": now_iso}
                if not args.no_retrieve:
                    from genesis.session_awareness.accumulator import top_entities
                    from genesis.session_awareness.ranking import rank_candidates

                    entity_shadow: list[dict] = []
                    candidates = await rank_candidates(
                        ema=state["ema"],
                        entity_query=" ".join(top_entities(state, 8)),
                        db=db,
                        qdrant_client=qdrant,
                        embedding_provider=provider,
                        created_before=as_of_cutoff,
                        entity_lane=args.entity_lane,
                        entity_shadow_out=entity_shadow,
                        # Verbatim keys — mirrors the worker (multi-word
                        # alias-normalized keys must not be re-split).
                        entity_terms=top_entities(state, 32),
                    )
                    fire_rec["candidates"] = [
                        {
                            "memory_id": c["memory_id"],
                            "score": round(c["score"], 4),
                            "lanes": c["lanes"],
                            "entity_path": c.get("entity_path"),
                            "preview": c["preview"][:60],
                        }
                        for c in candidates
                    ]
                    if entity_shadow:
                        fire_rec["entity_shadow"] = [
                            {
                                "memory_id": s["memory_id"],
                                "path_score": round(
                                    s["entity_path"]["path_score"], 4
                                ),
                                "already_candidate": s["already_candidate"],
                            }
                            for s in entity_shadow
                        ]
                fires.append(fire_rec)
    finally:
        if db is not None:
            await db.close()
        cache.save()

    result = {
        "session_id": args.session_id,
        "as_of": as_of_cutoff,
        "genuine_turns": genuine,
        "fires": fires,
        "trace": trace,
        "outlier_skips": state.get("outlier_skips", 0),
    }
    if args.target:
        hit = None
        for f in fires:
            for c in f.get("candidates", []):
                if c["memory_id"].startswith(args.target):
                    hit = {
                        "turn": f["turn"],
                        "memory_id": c["memory_id"],
                        "lanes": c.get("lanes", []),
                    }
                    break
            if hit:
                break
        result["target"] = args.target
        result["target_hit"] = hit
        if args.target_entity:
            # E4 criterion: a fire within [t_mention, t_mention+10]
            # whose candidates include the target VIA the entity lane.
            # (The mention window replaces the original fixed turn<=10 —
            # mentions can arrive late in a session.) A miss with no
            # fire in the window is a trigger-cadence failure, reported
            # distinctly from a lane failure.
            result["target_entity"] = args.target_entity
            result["t_mention"] = t_mention
            window_fires = [
                f for f in fires
                if t_mention is not None
                and t_mention <= f["turn"] <= t_mention + 10
            ]
            entity_hit = None
            for f in window_fires:
                for c in f.get("candidates", []):
                    if (
                        c["memory_id"].startswith(args.target)
                        and "entity" in c.get("lanes", [])
                    ):
                        entity_hit = {"turn": f["turn"], "memory_id": c["memory_id"]}
                        break
                if entity_hit:
                    break
            result["entity_hit"] = entity_hit
            result["PASS"] = bool(entity_hit)
            if not entity_hit:
                result["fail_reason"] = (
                    "no_mention" if t_mention is None
                    else "no_fire_in_window" if not window_fires
                    else "target_not_in_entity_lane"
                )
        else:
            result["PASS"] = bool(hit and hit["turn"] <= 10)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", default=None)
    parser.add_argument("--target", default=None, help="memory-id prefix to hunt")
    parser.add_argument(
        "--target-entity",
        default=None,
        help="normalized ledger keyword (e.g. 'omi'); switches PASS to the "
             "E4 criterion: target reached VIA the entity lane within 10 "
             "turns of the keyword first entering the ledger",
    )
    parser.add_argument(
        "--entity-lane",
        default=None,
        choices=["off", "shadow", "live"],
        help="override ranking.ENTITY_LANE_MODE for this replay (the "
             "acceptance run uses 'live'; production stays shadow)",
    )
    parser.add_argument("--max-genuine-turns", type=int, default=0)
    parser.add_argument("--no-retrieve", action="store_true")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO cutoff: only memories created before this instant are "
             "candidates. 'auto' = the session's first turn timestamp "
             "(the honest counterfactual — no post-session leakage).",
    )
    parser.add_argument("--json", action="store_true", help="full JSON to stdout")
    args = parser.parse_args()

    result = asyncio.run(replay(args))

    if args.json:
        print(json.dumps(result, indent=1))
        return
    print(f"session {result['session_id']}: {result['genuine_turns']} genuine turns, "
          f"{len(result['fires'])} fires, {result['outlier_skips']} outlier skips")
    for row in result["trace"]:
        flag = " FIRE" if any(f["turn"] == row["turn"] for f in result["fires"]) else ""
        pv = " PIVOT" if row["pivot"] else ""
        print(f"  t{row['turn']:>3} ema={row['ema_turns']:>3} "
              f"stab={row['stability']:.3f} {row['reason']:<18}{pv}{flag} | {row['prompt']}")
    for f in result["fires"]:
        print(f"FIRE @ turn {f['turn']}:")
        for c in f.get("candidates", []):
            print(f"    {c['score']:.3f} [{','.join(c['lanes'])}] "
                  f"{c['memory_id'][:8]} {c['preview']!r}")
    if args.target:
        print(f"TARGET {args.target}: hit={result['target_hit']} "
              f"PASS={result.get('PASS')}")
        if args.target_entity:
            print(f"  t_mention={result.get('t_mention')} "
                  f"entity_hit={result.get('entity_hit')} "
                  f"fail_reason={result.get('fail_reason')}")


if __name__ == "__main__":
    main()
