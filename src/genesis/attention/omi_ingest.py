"""Dedicated OMI ingest service — the ONLY publicly-exposed Genesis surface.

A standalone single-route Flask app bound to ``127.0.0.1`` behind a Tailscale
Funnel. It imports NO ``GenesisRuntime`` and NO attention engine — a leak or a
compromise here cannot reach the rest of Genesis. It BUFFERS ONLY: normalized
transcript rows land in per-UTC-day snapshot dbs; the L1.5 judge (the household-
text cloud egress) stays manual and per-run user-gated, exactly as today.

Error policy is shaped by OMI's failure budget: the webhook auto-disables after
100 CUMULATIVE non-2xx responses (a counter that never resets). So POST-auth we
NEVER return 429/5xx — overload/flood/internal-error all drop-with-``200``, because
the data is lost either way and the webhook lifeline is the scarcer resource.
Pre-auth 401/403 ARE returned (a genuinely misconfigured webhook SHOULD burn its
budget and disable). A response body containing a ``message`` key push-notifies the
user's phone, so no handler ever emits one.

Concurrency: ``threaded=True`` (a slow request on a single thread would starve
OMI's 2s-connect/30s-read deliveries into the permanent kill-switch). A single
lock serializes the normalize→anchor→state→day-db critical section — also required
for the anchor read-modify-write to be correct.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from genesis.attention import omi_daydb
from genesis.attention.omi_normalize import decide_anchor, normalize_segments, parse_payload
from genesis.attention.omi_state import OmiState
from genesis.env import genesis_home, secrets_path

log = logging.getLogger("genesis.omi_ingest")

_MAX_CONTENT_LENGTH = 256 * 1024  # 256 KB — a batch is a handful of short segments
_CONFIG_PATH = Path("~/.genesis/omi_config.yaml").expanduser()
_DEFAULT_PORT = 8799


@dataclass(frozen=True)
class OmiConfig:
    enabled: bool
    port: int
    uid_allowlist: frozenset[str]
    anchor_tolerance_s: float


def load_omi_config(path: Path | None = None) -> OmiConfig | None:
    """Load ``~/.genesis/omi_config.yaml``. ``None`` if absent or disabled.

    Raises ``ValueError`` when present-and-enabled but malformed (empty allowlist),
    so a config typo surfaces instead of silently accepting nothing.
    """
    cfg_path = path or _CONFIG_PATH
    if not cfg_path.exists():
        return None
    import yaml

    data = yaml.safe_load(cfg_path.read_text()) or {}
    if not data.get("enabled", True):
        return None
    allowlist = frozenset(str(u) for u in (data.get("uid_allowlist") or []))
    if not allowlist:
        raise ValueError("omi_config.yaml is enabled but uid_allowlist is empty")
    return OmiConfig(
        enabled=True,
        port=int(data.get("port", _DEFAULT_PORT)),
        uid_allowlist=allowlist,
        anchor_tolerance_s=float(data.get("anchor_tolerance_s", 60.0)),
    )


def load_tokens(secrets_file: Path | None = None) -> tuple[str, str]:
    """Read ``(primary, previous)`` path tokens from secrets.env (bridge parser).

    ``previous`` is ``""`` when unset. Dual tokens let a rotation overlap: a gap
    would otherwise burn ~1 failure per 1-2s of speech and auto-disable OMI in
    minutes.
    """
    path = str(secrets_file or secrets_path())
    secrets: dict[str, str] = {}
    if os.path.exists(path):
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                secrets[key.strip()] = value.strip().strip('"')
    return (
        secrets.get("OMI_INGEST_SECRET_TOKEN", ""),
        secrets.get("OMI_INGEST_SECRET_TOKEN_PREVIOUS", ""),
    )


def _safe_end(seg: dict) -> float:
    try:
        return float(seg.get("end"))
    except (TypeError, ValueError):
        return 0.0


def create_app(
    *,
    config: OmiConfig,
    tokens: tuple[str, str],
    state: OmiState,
    write_rows=None,
    now_fn=None,
    heartbeat_path: Path | None = None,
) -> Flask:
    """Build the ingest app. Every dependency is injected so it unit-tests cleanly."""
    primary, previous = tokens
    now_fn = now_fn or time.time
    if write_rows is None:
        def write_rows(rows, recv_ts):  # default: write to the real day-db
            return omi_daydb.insert_rows(rows, recv_ts=recv_ts)

    # werkzeug's INFO access log would journal the secret path-token on every hit.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

    lock = threading.Lock()  # serializes the whole state + day-db critical section
    hb = {
        "started_at": now_fn(),
        "last_recv_ts": None,
        "accepted_total": 0,
        "auth_failures": 0,
        "internal_errors": 0,
    }

    def _write_heartbeat() -> None:
        if heartbeat_path is None:
            return
        try:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text(json.dumps(hb))
        except OSError:
            log.warning("omi ingest: heartbeat write failed", exc_info=True)

    def _token_ok(candidate: str) -> bool:
        ok = hmac.compare_digest(candidate, primary)
        if previous:
            ok = hmac.compare_digest(candidate, previous) or ok
        return ok

    def _process(uid: str, data, idem_key: str | None) -> int:
        """Under ``lock``: dedup → anchor → normalize → write. Returns rows written."""
        recv_ts = now_fn()
        hb["last_recv_ts"] = recv_ts
        # 1. delivery-level dedup — a pure retry must not touch anchor/day-db.
        if state.is_duplicate_delivery(idem_key, now=recv_ts):
            _write_heartbeat()
            return 0
        session_id, segments = parse_payload(data)
        if session_id and session_id != uid:
            log.warning("omi ingest: body session_id != query uid (auth uses query uid)")
        candidates = [s for s in segments if (s.get("text") or "").strip()]
        if not candidates:
            _write_heartbeat()
            return 0
        # 2. segment-uuid dedup BEFORE any anchor mutation.
        seen = state.seen_segment_ids([s.get("id") for s in candidates], now=recv_ts)
        unseen = [s for s in candidates if s.get("id") not in seen]
        if not unseen:
            _write_heartbeat()
            return 0
        # 3. anchor read-modify-write (max_end monotonic within a kept anchor).
        batch_max_end = max(_safe_end(s) for s in unseen)
        prev = state.get_anchor(uid)
        prev_epoch0 = prev[0] if prev else None
        epoch0 = decide_anchor(prev_epoch0, batch_max_end, recv_ts, config.anchor_tolerance_s)
        if prev is None or epoch0 != prev_epoch0:
            new_max_end = batch_max_end
        else:
            new_max_end = max(prev[1], batch_max_end)
        # 4. normalize + write + record, THEN persist the advanced anchor LAST. If
        # the day-db write raises, the anchor must not have moved for data that never
        # landed — the next batch re-anchors cleanly off the unchanged prior anchor.
        # All advisory: worst case is ~2s ts jitter on one batch, never lost/dup speech.
        rows = normalize_segments(unseen, uid=uid, epoch0=epoch0)
        written = write_rows(rows, recv_ts)
        state.record_segment_ids([r.segment_id for r in rows], now=recv_ts)
        state.set_anchor(uid, epoch0, new_max_end, now=recv_ts)
        hb["accepted_total"] += written
        _write_heartbeat()
        return written

    def _count_auth_failure() -> None:
        with lock:
            hb["auth_failures"] += 1
            _write_heartbeat()

    @app.post("/omi/<path_token>/ingest")
    def ingest(path_token: str):
        # Pre-auth: 401/403 returned freely (a real misconfig SHOULD disable OMI).
        if not _token_ok(path_token):
            log.warning("omi ingest: bad path token")
            _count_auth_failure()
            return jsonify({"error": "unauthorized"}), 401
        uid = request.args.get("uid", "")
        if uid not in config.uid_allowlist:
            log.warning("omi ingest: uid not allowlisted")
            _count_auth_failure()
            return jsonify({"error": "forbidden"}), 403
        # Post-auth: NEVER 5xx/429 — client errors 400/413 only; else drop-with-200.
        try:
            data = request.get_json(force=True, silent=True)
            if data is None:
                return jsonify({"error": "expected a json body"}), 400
            idem_key = request.headers.get("Idempotency-Key")
            with lock:
                accepted = _process(uid, data, idem_key)
            return jsonify({"accepted": accepted}), 200
        except HTTPException:
            # 400/413/405 — permanent client errors the policy allows; let the
            # JSON error handlers render them (they never emit a `message` key).
            raise
        except Exception:
            log.error("omi ingest: internal error (dropped, returning 200)", exc_info=True)
            with lock:
                hb["internal_errors"] += 1
                _write_heartbeat()
            return jsonify({"accepted": 0}), 200

    # JSON error handlers — central enforcement of the no-``message``-key invariant.
    @app.errorhandler(400)
    def _400(_e):
        return jsonify({"error": "bad request"}), 400

    @app.errorhandler(404)
    def _404(_e):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(405)
    def _405(_e):
        return jsonify({"error": "method not allowed"}), 405

    @app.errorhandler(413)
    def _413(_e):
        return jsonify({"error": "payload too large"}), 413

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    config = load_omi_config()
    if config is None:
        log.error("OMI ingest not configured/enabled (~/.genesis/omi_config.yaml) — exiting")
        sys.exit(2)
    primary, previous = load_tokens()
    if not primary:
        log.error("OMI_INGEST_SECRET_TOKEN missing in secrets.env — exiting")
        sys.exit(2)

    attn_dir = genesis_home() / "attention"
    state = OmiState(attn_dir / "omi_state.db")
    app = create_app(
        config=config,
        tokens=(primary, previous),
        state=state,
        heartbeat_path=attn_dir / "omi_ingest_health.json",
    )

    from genesis.util.process_lock import ProcessLock

    with ProcessLock("omi-ingest", pid_dir=attn_dir):
        log.info("OMI ingest listening on 127.0.0.1:%d (buffers-only)", config.port)
        app.run(host="127.0.0.1", port=config.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
