"""Provider-leg transactions on the existing queue connection; no DB opener."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time

import _genesis_index_marker_recovery as recovery

PROVIDERS = ("cbm", "gitnexus")
MODES = ("fast", "moderate", "full")
OUTCOMES = ("consume", "consume_full", "restore", "restore_backoff", "restore_failure")
MAX_ATTEMPTS = 5
INTEGER_MAX = 2**63 - 1
CONTEXT = {"hash", "repo_path", "provider", "protocol", "execution_seq", "claim_id"}

_SCHEMA = """
CREATE TABLE pending (
 hash TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('cbm','gitnexus')),
 repo_path TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('fast','moderate','full')),
 requested_at REAL NOT NULL, attempts INTEGER NOT NULL CHECK(attempts>=0),
 legacy_unqualified INTEGER NOT NULL CHECK(legacy_unqualified IN (0,1)),
 legacy_shared_attempts TEXT NOT NULL, PRIMARY KEY(hash,provider));
CREATE TABLE executions (
 execution_seq INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT NOT NULL,
 repo_path TEXT NOT NULL, provider TEXT CHECK(provider IN ('cbm','gitnexus')),
 protocol INTEGER NOT NULL CHECK(protocol IN (1,2)),
 entry_kind TEXT NOT NULL CHECK(entry_kind IN ('claim','legacy-failure')),
 claim_id TEXT NOT NULL, original_tools TEXT NOT NULL, mode TEXT NOT NULL,
 requested_at REAL NOT NULL, attempts INTEGER NOT NULL,
 legacy_unqualified INTEGER NOT NULL CHECK(legacy_unqualified IN (0,1)),
 legacy_shared_attempts TEXT NOT NULL, nonce_origin TEXT NOT NULL,
 scalar_action TEXT, recorded_at REAL, applied_state TEXT, disposition TEXT, charged_attempts INTEGER,
 legacy_reason TEXT, legacy_raw BLOB, legacy_failure_snapshot TEXT,
 conflict_reason TEXT, conflict_raw BLOB,
 UNIQUE(hash,protocol,entry_kind,claim_id),
 CHECK(protocol=1 OR (provider IS NOT NULL AND entry_kind='claim')));
CREATE INDEX executions_provider_sequence ON executions(hash,provider,execution_seq);
CREATE INDEX executions_legacy_sequence ON executions(hash,protocol,original_tools,execution_seq);
CREATE TABLE inflight (
 hash TEXT PRIMARY KEY, repo_path TEXT NOT NULL, tools TEXT NOT NULL,
 provider TEXT CHECK(provider IN ('cbm','gitnexus')),
 protocol INTEGER NOT NULL CHECK(protocol IN (1,2)),
 mode TEXT NOT NULL, requested_at REAL NOT NULL, attempts INTEGER NOT NULL,
 claim_id TEXT NOT NULL, execution_seq INTEGER NOT NULL REFERENCES executions(execution_seq),
 legacy_unqualified INTEGER NOT NULL, legacy_shared_attempts TEXT NOT NULL,
 outcome_action TEXT, outcome_recorded_at REAL,
 CHECK(protocol=1 OR provider IS NOT NULL));
CREATE TABLE failed (
 hash TEXT NOT NULL, provider TEXT CHECK(provider IN ('cbm','gitnexus')),
 legacy_diagnostic INTEGER NOT NULL,
 repo_path TEXT, tools TEXT, mode TEXT, requested_at REAL, attempts INTEGER,
 reason TEXT NOT NULL, raw_payload BLOB, failed_at REAL NOT NULL,
 legacy_unqualified INTEGER NOT NULL, legacy_shared_attempts TEXT NOT NULL,
 PRIMARY KEY(hash,provider),
 CHECK((legacy_diagnostic=1 AND provider IS NULL) OR
       (legacy_diagnostic=0 AND provider IS NOT NULL)));
CREATE UNIQUE INDEX failed_unassigned ON failed(hash) WHERE legacy_diagnostic=1;
"""


class IdentityConflict(ValueError):
    """A compatibility hash already has a different canonical repository."""


class UnownedExecution(RuntimeError):
    """A complete context does not select this retained execution."""


def shared_debts(value: str) -> list[int]:
    debts = json.loads(value)
    if (
        not isinstance(debts, list)
        or not all(type(debt) is int and 0 <= debt <= MAX_ATTEMPTS + 1 for debt in debts)
        or debts != sorted(set(debts))
    ):
        raise RuntimeError("invalid retained legacy shared debt")
    return debts


def _shared(values) -> str:
    encoded = json.dumps(sorted(set(values)), separators=(",", ":"))
    shared_debts(encoded)
    return encoded


def _settled_repo(h: str, repo: str) -> str:
    # A spelling already stored under this hash is kept as written (see
    # stored_repo), so a later move cannot change it or fail it. Anything else
    # (a raw incoming path) is canonicalized.
    if stored_repo(repo) and stored_hash(repo) == h:
        return repo
    return os.path.realpath(repo)


def _identity(db: sqlite3.Connection, h: str, repo: str) -> str:
    repo = _settled_repo(h, repo)
    row = db.execute("SELECT repo_path FROM repo_state WHERE hash=?", (h,)).fetchone()
    if row and row["repo_path"] is not None and row["repo_path"] != repo:
        raise IdentityConflict("queue hash has another canonical repository")
    db.execute(
        "INSERT INTO repo_state(hash,repo_path) VALUES(?,?) ON CONFLICT(hash) "
        "DO UPDATE SET repo_path=coalesce(repo_state.repo_path,excluded.repo_path)",
        (h, repo),
    )
    return repo


def coalesce(db: sqlite3.Connection, h: str, data: dict, *, legacy=False, shared=()) -> None:
    repo = _identity(db, h, data["repo_path"])
    providers = PROVIDERS if data["tools"] == "both" else (data["tools"],)
    for provider in providers:
        if provider not in PROVIDERS or data["mode"] not in MODES:
            raise ValueError("invalid provider or mode")
        prior = db.execute(
            "SELECT * FROM pending WHERE hash=? AND provider=?", (h, provider)
        ).fetchone()
        mode, requested, debt = data["mode"], data["requested_at"], data["attempts"]
        old, originals = bool(legacy), set(shared)
        if prior:
            mode = max((mode, prior["mode"]), key=MODES.index)
            requested = min(requested, prior["requested_at"])
            debt = max(debt, prior["attempts"])
            old |= bool(prior["legacy_unqualified"])
            originals.update(shared_debts(prior["legacy_shared_attempts"]))
        db.execute(
            "INSERT INTO pending VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(hash,provider) DO UPDATE SET "
            "repo_path=excluded.repo_path,mode=excluded.mode,requested_at=excluded.requested_at,"
            "attempts=excluded.attempts,legacy_unqualified=excluded.legacy_unqualified,"
            "legacy_shared_attempts=excluded.legacy_shared_attempts",
            (h, provider, repo, mode, requested, debt, int(old), _shared(originals)),
        )


def _conflict(db, sequence, reason, raw):
    db.execute(
        "UPDATE executions SET conflict_reason=coalesce(conflict_reason,?),"
        "conflict_raw=coalesce(conflict_raw,?) WHERE execution_seq=?",
        (reason, raw, sequence),
    )


def _legacy_execution(db, h, data, nonce, origin, kind, raw=None):
    repo = _settled_repo(h, data["repo_path"])
    fields = {
        "repo_path": repo,
        "original_tools": data["tools"],
        "mode": data["mode"],
        "requested_at": data["requested_at"],
        "attempts": data["attempts"],
        "nonce_origin": origin,
    }
    prior = db.execute(
        "SELECT * FROM executions WHERE hash=? AND protocol=1 AND entry_kind=? AND claim_id=?",
        (h, kind, nonce),
    ).fetchone()
    if prior:
        if any(prior[key] != value for key, value in fields.items()):
            _conflict(db, prior["execution_seq"], "conflicting legacy claim identity", raw)
            return dict(prior), False
        return dict(prior), True
    _identity(db, h, repo)
    shared = [data["attempts"]] if data["tools"] == "both" else []
    cursor = db.execute(
        "INSERT INTO executions(hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,"
        "mode,requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin,"
        "legacy_reason,legacy_raw,legacy_failure_snapshot,applied_state) "
        "VALUES(?,?,?,1,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (
            h,
            repo,
            data["tools"] if data["tools"] in PROVIDERS else None,
            kind,
            nonce,
            data["tools"],
            data["mode"],
            data["requested_at"],
            data["attempts"],
            _shared(shared),
            origin,
            data.get("reason"),
            raw,
            data.get("legacy_failure_snapshot") if kind == "legacy-failure" else None,
            "legacy-failed" if kind == "legacy-failure" else None,
        ),
    )
    return dict(
        db.execute("SELECT * FROM executions WHERE execution_seq=?", (cursor.lastrowid,)).fetchone()
    ), True


def legacy_claim(db, h, data, *, raw=None, artifact_identity=None):
    nonce = data.get("claim_id")
    origin = "legacy-provided"
    if not isinstance(nonce, str) or not nonce:
        if artifact_identity is None:
            raise RuntimeError("missing legacy claim artifact identity")
        name, digest, mtime = artifact_identity
        encoded = json.dumps([h, 1, name.hex(), digest, mtime], separators=(",", ":")).encode()
        nonce = hashlib.sha256(encoded).hexdigest()[:32]
        origin = "synthetic-artifact"
    row, matches = _legacy_execution(db, h, data, nonce, origin, "claim", raw)
    if not matches:
        return
    existing = db.execute("SELECT 1 FROM inflight WHERE hash=?", (h,)).fetchone()
    if not existing and row["applied_state"] is None:
        db.execute(
            "INSERT INTO inflight(hash,repo_path,tools,provider,protocol,mode,requested_at,attempts,"
            "claim_id,execution_seq,legacy_unqualified,legacy_shared_attempts) "
            "VALUES(?,?,?,?,1,?,?,?,?,?,1,?)",
            (
                h,
                row["repo_path"],
                row["original_tools"],
                row["provider"],
                row["mode"],
                row["requested_at"],
                row["attempts"],
                nonce,
                row["execution_seq"],
                row["legacy_shared_attempts"],
            ),
        )
    if data.get("outcome_action") is not None:
        legacy_outcome(db, h, nonce, data["outcome_action"], data["outcome_recorded_at"], raw)


def legacy_outcome(db, h, nonce, action, recorded_at, raw):
    row = db.execute(
        "SELECT * FROM executions WHERE hash=? AND protocol=1 AND entry_kind='claim' AND claim_id=?",
        (h, nonce),
    ).fetchone()
    if not row:
        return "unowned"
    return _record_scalar(db, row, dict(action=action, recorded_at=recorded_at), raw)


@recovery.legacy_transition
def _record_scalar(db, row, event, raw):
    if row["conflict_reason"]:
        return "conflict"
    if row["scalar_action"] is None:
        # Validated timestamps accept finite numbers; Python int would bind as
        # signed64 INTEGER even for a REAL column. Normalize every scalar path.
        recorded_at = float(event["recorded_at"])
        db.execute(
            "UPDATE executions SET scalar_action=?,recorded_at=? WHERE execution_seq=?",
            (event["action"], recorded_at, row["execution_seq"]),
        )
        db.execute(
            "UPDATE inflight SET outcome_action=?,outcome_recorded_at=? WHERE execution_seq=?",
            (event["action"], recorded_at, row["execution_seq"]),
        )
        return "recorded"
    if row["scalar_action"] == event["action"]:
        return "idempotent"
    legacy = "legacy " if row["protocol"] == 1 else ""
    _conflict(db, row["execution_seq"], f"conflicting {legacy}durable outcomes", raw)
    return "conflict"


def _failed(db, h, provider, data, *, diagnostic=False, legacy=False, shared=()):
    db.execute(
        "INSERT INTO failed VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hash,provider) DO UPDATE SET "
        "repo_path=excluded.repo_path,tools=excluded.tools,mode=excluded.mode,"
        "requested_at=excluded.requested_at,attempts=excluded.attempts,reason=excluded.reason,"
        "raw_payload=excluded.raw_payload,failed_at=excluded.failed_at,"
        "legacy_unqualified=excluded.legacy_unqualified,legacy_shared_attempts=excluded.legacy_shared_attempts",
        (
            h,
            provider,
            int(diagnostic),
            data.get("repo_path"),
            data.get("tools"),
            data.get("mode"),
            data.get("requested_at"),
            data.get("attempts"),
            data["reason"],
            data.get("raw_payload"),
            data["failed_at"],
            int(legacy),
            _shared(shared),
        ),
    )


def legacy_failed(db, h, data, *, raw=None, artifact_identity=None):
    if data.get("repo_path") is None or data.get("tools") is None:
        _failed(db, h, None, data, diagnostic=True, legacy=True)
        return
    # Preserve the old failed table's complete row. Extra artifact metadata
    # remains in exact raw receipts, rather than becoming a new semantic input.
    snapshot = {"hash": h}
    for key in (
        "repo_path",
        "tools",
        "mode",
        "requested_at",
        "attempts",
        "reason",
        "raw_payload",
        "failed_at",
    ):
        value = data.get(key)
        snapshot[key] = value.hex() if isinstance(value, bytes) else value
    identity = snapshot
    if artifact_identity is not None:
        name, digest, mtime = artifact_identity
        identity = [name.hex(), digest, mtime]
    nonce = hashlib.sha256(
        json.dumps(
            [h, 1, "legacy-failure", identity],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    retained = raw if raw is not None else data.get("raw_payload")
    retained_data = {
        **data,
        "legacy_failure_snapshot": json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
    }
    _legacy_execution(db, h, retained_data, nonce, "synthetic-row", "legacy-failure", retained)
    shared = [data["attempts"]] if data["tools"] == "both" else []
    for provider in PROVIDERS if data["tools"] == "both" else (data["tools"],):
        if provider not in PROVIDERS:
            raise RuntimeError("invalid attributed legacy failure")
        _failed(db, h, provider, data, legacy=True, shared=shared)


def upgrade(db: sqlite3.Connection):
    """Caller owns BEGIN IMMEDIATE, receipt dispatch, version and commit."""
    for table in ("pending", "inflight", "failed"):
        db.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
    db.execute("ALTER TABLE repo_state ADD COLUMN repo_path TEXT")
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for row in db.execute("SELECT * FROM pending_v2 ORDER BY hash"):
        data = dict(row)
        coalesce(
            db,
            data["hash"],
            data,
            legacy=True,
            shared=[data["attempts"]] if data["tools"] == "both" else [],
        )
    for row in db.execute("SELECT * FROM failed_v2 ORDER BY hash"):
        data = dict(row)
        legacy_failed(db, data["hash"], data)
    for row in db.execute("SELECT * FROM inflight_v2 ORDER BY hash"):
        data = dict(row)
        legacy_claim(db, data["hash"], data)
    for table in ("pending", "inflight", "failed"):
        db.execute(f"DROP TABLE {table}_v2")
    db.execute(
        "INSERT INTO queue_meta(key,value) VALUES('automatic_protocol_ready','0') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def stored_repo(repo: object) -> bool:
    """A stored spelling is canonical by construction; never re-resolve it.

    Writers store ``realpath`` once. Re-resolving later would reject a row
    whose repository moved or gained a symlink, and a whole-population
    check would then stop every tick. The entrypoint resolves the live path.
    """
    return (
        isinstance(repo, str)
        and repo.startswith("/")
        and not repo.startswith("//")
        and "\0" not in repo
        and os.path.normpath(repo) == repo
    )


def stored_hash(repo: str) -> str:
    # Compatibility, not security: the hash of the stored canonical spelling.
    return hashlib.sha1(repo.encode()).hexdigest()[:16]  # noqa: S324


def validate_context(context: dict, _hash_repo=None, *, protocol: int = 2) -> dict:
    # _hash_repo is accepted for call compatibility and deliberately unused: the
    # hash is checked against the stored spelling, never a live resolution.
    if type(protocol) is not int or protocol not in (2, 3):
        raise ValueError("unsupported claim protocol")
    if not isinstance(context, dict) or set(context) != CONTEXT:
        raise ValueError("complete queue claim context is required")
    repo = context["repo_path"]
    if (
        not stored_repo(repo)
        or not isinstance(context["hash"], str)
        or re.fullmatch(r"[0-9a-f]{16}", context["hash"]) is None
        or context["hash"] != stored_hash(repo)
        or context["provider"] not in PROVIDERS
        or type(context["protocol"]) is not int
        or context["protocol"] != protocol
        or type(context["execution_seq"]) is not int
        or not 1 <= context["execution_seq"] <= INTEGER_MAX
        or not isinstance(context["claim_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", context["claim_id"]) is None
    ):
        raise ValueError("invalid queue claim context")
    return dict(context)


def scalar_event(context, action, recorded_at, _hash_repo=None):
    context = validate_context(context)
    if action not in OUTCOMES or (
        action in ("consume_full", "restore_backoff") and context["provider"] != "cbm"
    ):
        raise ValueError("invalid provider outcome action")
    if (
        isinstance(recorded_at, bool)
        or not isinstance(recorded_at, (int, float))
        or not math.isfinite(recorded_at)
    ):
        raise ValueError("invalid outcome timestamp")
    return dict(version=2, kind="outcome", **context, action=action, recorded_at=recorded_at)


def _execution(db, context):
    row = db.execute(
        "SELECT * FROM executions WHERE execution_seq=?", (context["execution_seq"],)
    ).fetchone()
    if not row or row["entry_kind"] != "claim" or any(row[key] != context[key] for key in CONTEXT):
        raise UnownedExecution("outcome does not own this queue execution")
    return dict(row)


def claim(db, h, provider, *, protocol=2):
    if type(protocol) is not int or protocol not in (2, 3):
        raise ValueError("unsupported claim protocol")
    if provider not in PROVIDERS:
        raise ValueError("provider must be cbm or gitnexus")
    if db.execute("SELECT 1 FROM inflight WHERE hash=?", (h,)).fetchone():
        return None
    row = db.execute("SELECT * FROM pending WHERE hash=? AND provider=?", (h, provider)).fetchone()
    if row is None:
        return None
    _identity(db, h, row["repo_path"])
    nonce = secrets.token_hex(16)
    cursor = db.execute(
        "INSERT INTO executions(hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,"
        "mode,requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin) "
        "VALUES(?,?,?,?,'claim',?,?,?,?,?,?,?,'generated')",
        (
            h,
            row["repo_path"],
            provider,
            protocol,
            nonce,
            provider,
            row["mode"],
            row["requested_at"],
            row["attempts"],
            row["legacy_unqualified"],
            row["legacy_shared_attempts"],
        ),
    )
    sequence = cursor.lastrowid
    db.execute(
        "INSERT INTO inflight(hash,repo_path,tools,provider,protocol,mode,requested_at,attempts,"
        "claim_id,execution_seq,legacy_unqualified,legacy_shared_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            h,
            row["repo_path"],
            provider,
            provider,
            protocol,
            row["mode"],
            row["requested_at"],
            row["attempts"],
            nonce,
            sequence,
            row["legacy_unqualified"],
            row["legacy_shared_attempts"],
        ),
    )
    db.execute("DELETE FROM pending WHERE hash=? AND provider=?", (h, provider))
    context = dict(
        hash=h,
        repo_path=row["repo_path"],
        provider=provider,
        protocol=protocol,
        execution_seq=sequence,
        claim_id=nonce,
    )
    return {
        **dict(row),
        "tools": provider,
        "claim_id": nonce,
        "legacy_shared_attempts": shared_debts(row["legacy_shared_attempts"]),
        "context": context,
    }


def record_event(db, event, raw):
    context = {key: event[key] for key in CONTEXT}
    return _record_scalar(db, _execution(db, context), event, raw)


def _restore(db, row, increment):
    debt = row["attempts"] + int(increment)
    pending = db.execute(
        "SELECT 1 FROM pending WHERE hash=? AND provider=?", (row["hash"], row["provider"])
    ).fetchone()
    if debt >= MAX_ATTEMPTS:
        data = {
            **row,
            "tools": row["provider"],
            "attempts": debt,
            "reason": "attempt budget exhausted",
            "raw_payload": None,
            "failed_at": time.time(),
        }
        _failed(
            db,
            row["hash"],
            row["provider"],
            data,
            legacy=bool(row["legacy_unqualified"]),
            shared=shared_debts(row["legacy_shared_attempts"]),
        )
        return ("pending" if pending else "failed"), "budget-exhausted", debt
    data = {**row, "tools": row["provider"], "attempts": debt}
    coalesce(
        db,
        row["hash"],
        data,
        legacy=bool(row["legacy_unqualified"]),
        shared=shared_debts(row["legacy_shared_attempts"]),
    )
    return "pending", "restored", debt


def apply(db, context):
    row = _execution(db, context)
    if row["conflict_reason"]:
        raise RuntimeError("conflicting durable queue outcome requires reconciliation")
    if row["applied_state"] is not None:
        return row["applied_state"]
    action = row["scalar_action"]
    if action is None:
        return None
    inflight = db.execute(
        "SELECT * FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
        (context["hash"], context["execution_seq"], context["claim_id"]),
    ).fetchone()
    if inflight is None:
        raise RuntimeError("execution no longer owns an inflight row")
    if action == "consume_full":
        db.execute(
            "UPDATE repo_state SET last_full=?,full_backoff=NULL WHERE hash=?",
            (row["recorded_at"], row["hash"]),
        )
    if action in ("consume", "consume_full"):
        state, disposition, charged = "consumed", "consumed", row["attempts"]
    else:
        if action == "restore_backoff":
            db.execute(
                "UPDATE repo_state SET full_backoff=? WHERE hash=?",
                (row["recorded_at"], row["hash"]),
            )
        state, disposition, charged = _restore(db, row, action == "restore_failure")
    deleted = db.execute(
        "DELETE FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
        (row["hash"], row["execution_seq"], row["claim_id"]),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("queue claim changed during settlement")
    db.execute(
        "UPDATE executions SET applied_state=?,disposition=?,charged_attempts=? WHERE execution_seq=?",
        (state, disposition, charged, row["execution_seq"]),
    )
    return state


def reconcile(db):
    repended = []
    for row in list(db.execute("SELECT * FROM inflight ORDER BY hash")):
        if row["protocol"] != 2:
            continue  # Preserve possible old native work; never blanket-restore it.
        context = {key: row[key] for key in CONTEXT}
        execution = _execution(db, context)
        if execution["scalar_action"] is None:
            # Protocol2 has no automatic launch/intent path. This is not proof
            # about legacy or future typed-protocol executions.
            event = dict(context, action="restore", recorded_at=time.time())
            record_event(db, event, b"")
        if apply(db, context) == "pending":
            repended.append(row["hash"])
    return repended
