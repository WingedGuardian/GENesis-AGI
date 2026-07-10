"""Isolation layer for the A/B bench's Genesis arm.

Three mechanisms keep a cognition-enabled bench arm from touching production
state (each empirically verified 2026-07-09 — see the A3 plan records):

1. **SQLite**: every bench run gets a WAL-safe snapshot of the prod DB
   (sqlite3 backup API — a plain file copy of a live 400MB+ WAL database is
   torn). The genesis-memory MCP server is pointed at it via a per-server
   ``env`` block (``GENESIS_DB_PATH``) in a bench-generated MCP config — CC
   passes MCP-config env to the server process, and the server resolves its
   DB from env before secrets.env. All MCP-side writes, including recall's
   J-9 eval events, land in the snapshot.

2. **Qdrant**: collection names are hardcoded, so the arm READS prod Qdrant.
   Store-type tools are disallowed (arms.py), and the recall path's own
   usage write-backs (retrieved_count bumps — recall is read-mostly, not
   read-only!) are suppressed via ``GENESIS_MEMORY_WRITEBACKS_OFF`` in the
   same env block.

3. **Falsifiability**: ``ProdDeltaProbe`` captures prod Qdrant point counts
   AND payload usage-sums (payload UPDATEs don't change point counts — a
   count-only probe is blind to the write-on-read class) plus prod-DB row
   counts, before and after the arms run. Any delta fails the run loudly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from genesis.env import genesis_db_path, qdrant_url, repo_root

logger = logging.getLogger(__name__)

#: The only MCP server the Genesis arm gets in v1. Memory recall is the
#: treatment under test; health/outreach tools are out of scope (and every
#: extra server is isolation surface to audit).
BENCH_MCP_SERVERS = {"genesis-memory"}

#: Qdrant collections the memory system uses (hardcoded across memory/).
_COLLECTIONS = ("episodic_memory", "knowledge_base")

#: Prod tables the probe watches. eval_events is the J-9 stream (the redirect
#: target — growth here means the env redirect failed); cc_sessions would grow
#: if an arm accidentally ran through orchestration; observations is the
#: broadest cognitive write surface reachable by mistake.
_PROBE_TABLES = ("eval_events", "eval_runs", "cc_sessions", "observations")


def snapshot_prod_db(run_dir: Path, source: Path | None = None) -> Path:
    """WAL-safe snapshot of the production DB into the run dir.

    Uses the sqlite3 backup API: consistent even against a live writer, and
    includes WAL-resident pages a naive file copy would miss. Blocking
    (seconds for ~400MB on local disk) — call via ``asyncio.to_thread`` from
    async code.
    """
    source = source or genesis_db_path()
    dest = run_dir / "genesis.db"
    run_dir.mkdir(parents=True, exist_ok=True)

    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    size = dest.stat().st_size
    if size == 0:
        raise RuntimeError(f"DB snapshot at {dest} is empty — backup failed")
    logger.info("bench: snapshotted %s → %s (%.1f MB)", source, dest, size / 1e6)
    return dest


def generate_bench_mcp_config(run_dir: Path, db_copy_path: Path) -> Path:
    """Write the bench MCP config: genesis-memory only, env-redirected.

    The per-server ``env`` block is the isolation linchpin: CC passes it to
    the spawned server process (probe-verified), and the server resolves
    GENESIS_DB_PATH from env before secrets.env (genesis_mcp_server.py's
    dotenv fill skips keys already in the environment). Written per-run —
    never through build_mcp_config's shared .generated/ cache.
    """
    from genesis.cc.session_config import render_mcp_servers

    root = repo_root().resolve()
    template_path = root / "config" / "mcp.json.template"
    config = render_mcp_servers(template_path, str(root), BENCH_MCP_SERVERS)
    if set(config["mcpServers"]) != BENCH_MCP_SERVERS:
        raise RuntimeError(
            f"bench MCP config rendered {sorted(config['mcpServers'])}, "
            f"expected exactly {sorted(BENCH_MCP_SERVERS)} — template drift?"
        )

    for server in config["mcpServers"].values():
        server["env"] = {
            "GENESIS_DB_PATH": str(db_copy_path),
            # Frozen-snapshot semantics: recall must not bump usage payloads
            # (prod Qdrant is shared; and earlier tasks must not re-rank
            # memories for later ones). See env.memory_writebacks_off.
            "GENESIS_MEMORY_WRITEBACKS_OFF": "1",
        }

    out = run_dir / "bench_mcp.json"
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return out


def count_snapshot_eval_events(db_copy_path: Path) -> int:
    """Row count of eval_events in the SNAPSHOT — the runner's positive
    control: after the Genesis arm runs, this MUST have grown (recall emits
    J-9 events through the redirected server). No growth = the arm ran
    without its memory server (degraded to bare-plus-identity) or the env
    redirect silently failed — either way the run is invalid."""
    conn = sqlite3.connect(f"file:{db_copy_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM eval_events").fetchone()[0]
    finally:
        conn.close()


@dataclass
class ProdSnapshot:
    """One capture of prod-side state the bench must not disturb."""

    qdrant_point_counts: dict[str, int] = field(default_factory=dict)
    qdrant_retrieved_sums: dict[str, int] = field(default_factory=dict)
    qdrant_last_retrieved_max: dict[str, str] = field(default_factory=dict)
    db_row_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class ProdDeltaProbe:
    """Before/after capture of production state; any delta = loud failure.

    Watches BOTH point counts and payload usage-sums: the write-on-read class
    (retrieved_count bumps inside recall) mutates payloads without changing
    counts, so a count-only probe passes green while polluting.
    """

    def __init__(self) -> None:
        self._before: ProdSnapshot | None = None

    def capture(self) -> ProdSnapshot:
        snap = ProdSnapshot()
        self._capture_qdrant(snap)
        self._capture_db(snap)
        return snap

    def start(self) -> ProdSnapshot:
        self._before = self.capture()
        return self._before

    def finish(self) -> dict:
        """Capture 'after' and diff. Returns a JSON-able report with
        ``clean: bool``. Call BEFORE persisting bench results to the prod
        eval tables — the probe window must exclude legitimate writes."""
        if self._before is None:
            raise RuntimeError("ProdDeltaProbe.finish() called before start()")
        after = self.capture()
        before = self._before

        deltas: list[str] = []
        for coll in _COLLECTIONS:
            b, a = before.qdrant_point_counts.get(coll), after.qdrant_point_counts.get(coll)
            if b != a:
                deltas.append(f"qdrant {coll} point count {b} → {a}")
            bs = before.qdrant_retrieved_sums.get(coll)
            as_ = after.qdrant_retrieved_sums.get(coll)
            if bs != as_:
                deltas.append(f"qdrant {coll} sum(retrieved_count) {bs} → {as_}")
            bt = before.qdrant_last_retrieved_max.get(coll)
            at = after.qdrant_last_retrieved_max.get(coll)
            if bt != at:
                deltas.append(f"qdrant {coll} max(last_retrieved_at) {bt!r} → {at!r}")
        for table in _PROBE_TABLES:
            b, a = before.db_row_counts.get(table), after.db_row_counts.get(table)
            if b != a:
                deltas.append(f"prod db {table} rows {b} → {a}")

        errors = sorted(set(before.errors + after.errors))
        report = {
            "clean": not deltas,
            "deltas": deltas,
            "probe_errors": errors,
            "before": {
                "qdrant_point_counts": before.qdrant_point_counts,
                "db_row_counts": before.db_row_counts,
            },
        }
        if deltas:
            logger.error("bench: PROD DELTA DETECTED: %s", "; ".join(deltas))
        return report

    # -- capture internals -------------------------------------------------

    def _capture_qdrant(self, snap: ProdSnapshot) -> None:
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=qdrant_url(), timeout=30)
            try:
                for coll in _COLLECTIONS:
                    try:
                        snap.qdrant_point_counts[coll] = client.count(
                            coll, exact=True,
                        ).count
                        total, latest = self._scroll_usage(client, coll)
                        snap.qdrant_retrieved_sums[coll] = total
                        snap.qdrant_last_retrieved_max[coll] = latest
                    except Exception as exc:
                        snap.errors.append(f"qdrant {coll}: {exc}")
            finally:
                client.close()
        except Exception as exc:
            snap.errors.append(f"qdrant client: {exc}")

    @staticmethod
    def _scroll_usage(client, coll: str) -> tuple[int, str]:
        """Full payload scroll summing retrieved_count and taking the max
        last_retrieved_at. Heavy-ish (one field over every point) but exact —
        run twice per bench run, not per task."""
        total = 0
        latest = ""
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=coll,
                limit=1024,
                offset=offset,
                with_payload=["retrieved_count", "last_retrieved_at"],
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                total += int(payload.get("retrieved_count") or 0)
                ts = str(payload.get("last_retrieved_at") or "")
                # Mixed ISO-8601 with a fixed UTC offset — lexicographic max
                # is chronological max within each format family; good enough
                # for an equality-based delta check.
                latest = max(latest, ts)
            if offset is None:
                return total, latest

    def _capture_db(self, snap: ProdSnapshot) -> None:
        try:
            conn = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
            try:
                for table in _PROBE_TABLES:
                    try:
                        snap.db_row_counts[table] = conn.execute(
                            f"SELECT COUNT(*) FROM {table}"  # noqa: S608 — fixed table list
                        ).fetchone()[0]
                    except Exception as exc:
                        snap.errors.append(f"db {table}: {exc}")
            finally:
                conn.close()
        except Exception as exc:
            snap.errors.append(f"db connect: {exc}")
