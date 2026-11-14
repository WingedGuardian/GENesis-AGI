"""Provider safety storage on the facade's connection; no opener or live cutover."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

SCHEMA_VERSION = 5
PROVIDER_FIELDS = {
    "hash", "repo_path", "provider", "hazard_revision", "accepted_sequence",
    "accepted_terminal_sha256", "accepted_control_sha256", "accepted_source_identity",
    "accepted_evidence_reference", "grant",
}

_PROVIDER_SCHEMA = """
CREATE TABLE provider_state (
 hash TEXT NOT NULL, repo_path TEXT NOT NULL,
 provider TEXT NOT NULL CHECK(provider IN ('cbm','gitnexus')),
 hazard_revision INTEGER NOT NULL DEFAULT 0 CHECK(hazard_revision>=0),
 accepted_sequence INTEGER NOT NULL DEFAULT 0 CHECK(accepted_sequence>=0),
 accepted_terminal_sha256 TEXT, accepted_control_sha256 TEXT,
 accepted_source_identity BLOB, accepted_evidence_reference TEXT, grant BLOB,
 PRIMARY KEY(hash,provider));
CREATE INDEX executions_safety ON executions(hash,provider,execution_seq)
 WHERE safety_kind IS NOT NULL OR first_typed_conflict IS NOT NULL OR conflict_reason IS NOT NULL;
CREATE INDEX executions_unbound ON executions(hash,execution_seq) WHERE completion_kind='unbound';
CREATE UNIQUE INDEX executions_observation ON executions(hash,provider,nonce_origin)
 WHERE entry_kind='legacy-observation';
"""


def compatible_schema(db, default, native):
    """Recognize explicit schema5 without making it the startup migration target."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION and default == 4:
        validate_schema(db, native)
        return True
    if version > default:
        supported = SCHEMA_VERSION if default == 4 else default
        raise RuntimeError(f"index queue schema {version} is newer than supported {supported}")
    return version == default


def admission_schema(db, default, native, *, require_native):
    if not compatible_schema(db, default, native):
        raise RuntimeError("queue schema changed before admission")
    if require_native and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("native authority requires explicit schema5 cutover")


def refuse_unrepresented(db, residual, selected):
    """Extra conflicts fence their provider; retained cleanup keeps its interpretation."""
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        return
    for h, provider, kind, _identity in residual:
        if (h, provider) == selected and kind not in ("cleanup", "cleanup-disproof"):
            raise RuntimeError("unrepresented native conflict prevents provider admission")


@contextmanager
def decision_gate(db, api, *, native=False, admission=None):
    """Same admitted connection plus a final drain after recovery callbacks.

    This supplies evidence, not a grant or a provider-hold exception. Ordinary
    admission retains its unrepresented-conflict prefilter on the initial drain.
    """
    active = False
    try:
        with api['_legacy']._lock(api['marker_dir'](), api['OUTCOME_LOCK_WAIT_S']):
            with api['_transaction'](db):
                admission_schema(db, api['SCHEMA_VERSION'], api['_native'], require_native=native)
                imported = api['_legacy'].drain(api['marker_dir'](), db, api['_import_one'], api['_fsync_dir'])
                native_imported = []
                active = True

                def finalize():
                    if not active or not db.in_transaction:
                        raise RuntimeError('recovery decision is closed')
                    residual = []
                    native_imported.extend(api['_native'].drain(api['marker_dir'](), db, api['_legs'], api['_proof'],
                                                         api['marker_hash'], residual=residual))
                    return tuple(sorted(set(residual)))

                residual = finalize()
                if admission is not None:
                    refuse_unrepresented(db, residual, admission)
                yield db, finalize
            api['_legacy'].retire(api['marker_dir'](), imported, api['_fsync_dir'])
            api['_legacy'].retire(api['marker_dir'](), list(dict.fromkeys(native_imported)), api['_fsync_dir'])
    finally:
        active = False

def validate_schema(db, native):
    """A version number alone is not evidence of a completed conversion."""
    required = {field.split()[0] for field in native.FIELDS} | {"control_closure", "entry_kind", "claim_id"}
    columns = {row[1] for row in db.execute("PRAGMA table_info(executions)")}
    provider = {row[1] for row in db.execute("PRAGMA table_info(provider_state)")}
    metadata = db.execute("SELECT value FROM queue_meta WHERE key='schema_version'").fetchone()
    if (db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            or not required <= columns or provider != PROVIDER_FIELDS
            or metadata is None or metadata[0] != str(SCHEMA_VERSION)):
        raise RuntimeError("incomplete provider safety schema")


def upgrade(db, legs, native):
    """Explicit structural conversion; the caller owns rollback and publication."""
    if not db.in_transaction:
        raise RuntimeError("provider safety conversion requires an explicit transaction")
    if db.execute("PRAGMA user_version").fetchone()[0] != 4:
        raise RuntimeError("provider safety conversion requires schema4")
    schema = legs._SCHEMA.replace(
        "entry_kind IN ('claim','legacy-failure')", "entry_kind IN ('claim','legacy-failure','legacy-observation')"
    ).replace(" claim_id TEXT NOT NULL, original_tools", " claim_id TEXT, original_tools").replace(
        " scalar_action TEXT,", " control_closure BLOB, scalar_action TEXT,"
    ).replace(
        " CHECK(protocol=1 OR (provider IS NOT NULL AND entry_kind='claim'))",
        " CHECK((entry_kind='legacy-observation' AND protocol=1 AND provider IS NOT NULL AND claim_id IS NULL)"
        " OR (entry_kind<>'legacy-observation' AND claim_id IS NOT NULL)),"
        " CHECK(protocol=1 OR (provider IS NOT NULL AND entry_kind='claim'))",
    )
    native.upgrade(db, schema)
    for statement in _PROVIDER_SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    db.execute("UPDATE queue_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    project_legacy(db, legs, native, initial=True)
    validate_schema(db, native)


def _startup_data(value, native, proof, *, closure_ns):
    """Validate optional original-scope observations without creating registration."""
    startup = native._object(value, ("scope", "facts", "observed_ns"), "startup observation")
    native.registration_data(startup["scope"], proof)
    native._integer(startup["observed_ns"], "startup observation", positive=True)
    if not proof._facts(startup["facts"]) or startup["observed_ns"] > closure_ns:
        raise ValueError("invalid startup facts or observation ordering")
    proof._complete(startup["scope"]["facts"], startup["facts"])


def control_data(value, native, proof):
    """Validate the entire control witness; original-run binding is separate."""
    fields = ("state", "reason", "reservation_sha256", "observer", "observed_ns", "foreground", "monitor", "stop")
    native._object(value, fields + (("startup",) if isinstance(value, dict) and "startup" in value else ()), "control closure")
    if (value["state"], value["reason"]) not in (
        ("clean", "normal"), ("uncertain", "monitor_failed"),
        ("uncertain", "owned_wait_unavailable"), ("uncertain", "startup_incomplete"), ("orphaned", "owner_died"),
    ):
        raise ValueError("invalid control closure state or reason")
    native._hex(value["reservation_sha256"], 64, "control reservation digest")
    native._integer(value["observed_ns"], "control observation", positive=True)
    identities = [value["observer"]]
    for name in ("foreground", "monitor"):
        child = value[name]
        if child is not None:
            native._object(child, ("identity", "returncode"), "owned child witness")
            rc = child["returncode"]
            if rc is not None and (type(rc) is not int or not -native.INTEGER_MAX - 1 <= rc <= native.INTEGER_MAX):
                raise ValueError("invalid owned child return status")
            identities.append(child["identity"])
    for identity in identities:
        native._object(identity, ("pid", "start_ticks", "boot_id"), "control process identity")
        native._integer(identity["pid"], "control pid", positive=True)
        native._integer(identity["start_ticks"], "control start", positive=True)
        native._boot(identity["boot_id"])
    if value["state"] == "clean":
        foreground, monitor = value["foreground"], value["monitor"]
        if ((foreground is None) != (monitor is None)
                or foreground is not None and (foreground["returncode"] is None or monitor["returncode"] != 0)):
            raise ValueError("clean control requires actual foreground and successful monitor waits")
    if value["stop"] is not None:
        native.event({}, "stop", value["stop"], 0, proof)
    if "startup" in value:
        _startup_data(value["startup"], native, proof, closure_ns=value["observed_ns"])
    return value


def owner_identity():
    """Actual publishing process identity; caller input cannot supply this fact."""
    tail = Path("/proc/self/stat").read_text().rpartition(")")[2].split()
    return {"pid": os.getpid(), "start_ticks": int(tail[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def control_pending(row):
    return ("control_closure" in row and row["launch_reservation"] is not None
            and row["control_closure"] is None)


def control_blocked(db, h, exclude):
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        return False
    return db.execute(
        "SELECT 1 FROM executions WHERE hash=? AND protocol=3 AND execution_seq<>? "
        "AND launch_reservation IS NOT NULL AND control_closure IS NULL LIMIT 1", (h, exclude),
    ).fetchone() is not None


def _bind_startup(row, data, native, proof):
    """Cross-bind retained early facts to the original foreground and live record."""
    reserved, startup, foreground = json.loads(row["launch_reservation"]), data["startup"], data["foreground"]
    scope = startup["scope"]
    if (foreground is None or scope["facts"]["reporter"] != foreground["identity"]
            or scope["unit"] != reserved["unit"]
            or 2 * scope["stop_usec"] > reserved["wall_seconds"] * 1_000_000):
        raise ValueError("startup observation names another original child or scope")
    native._bound_facts(scope["facts"], reserved, proof)
    native._bound_facts(startup["facts"], reserved, proof)
    if row["scope_registration"] is not None:
        registered = json.loads(row["scope_registration"])
        if any(scope[key] != registered[key] for key in ("unit", "invocation_id", "stop_usec")):
            raise ValueError("startup observation names another registered scope")
        proof._complete(scope["facts"], registered["facts"])


def bind_control(row, item, native, proof):
    reserved, data = json.loads(row["launch_reservation"]), item["data"]
    if data["reservation_sha256"] != native.digest(reserved):
        raise ValueError("control closure names another reservation")
    if (data["state"] != "orphaned") != (data["observer"] == reserved["owner"]):
        raise ValueError("control closure has another observer")
    identities = [reserved["owner"]]
    for name in ("foreground", "monitor"):
        if data[name] is not None:
            identity = data[name]["identity"]
            if identity["boot_id"] != reserved["owner"]["boot_id"] or any(identity["pid"] == old["pid"] for old in identities):
                raise ValueError("control children must be distinct processes on the original boot")
            identities.append(identity)
    if row["control_closure"] is not None and native._same_event(json.loads(row["control_closure"]), item):
        return True  # Lost ACK replay remains valid after later physical observations.
    if data["reason"] == "startup_incomplete" and (
            row["launch_permission"] != "cancelled" or row["native_intent"] is not None or data["foreground"] is None):
        raise ValueError("startup uncertainty requires cancelled permission, actual child and no native intent")
    if "startup" in data:
        _bind_startup(row, data, native, proof)
    if data["state"] == "clean":
        if row["scope_registration"] is None:
            if (row["native_intent"] is not None or row["launch_permission"] != "cancelled"
                    or data["foreground"] is not None or data["monitor"] is not None):
                raise ValueError("unlaunched closure requires cancelled permission without native evidence")
        elif data["foreground"] is None or data["monitor"] is None:
            raise ValueError("launched closure requires actual owned child witnesses")
    latest = 0
    if data["observer"]["boot_id"] == reserved["owner"]["boot_id"]:
        if row["native_intent"]:
            latest = json.loads(row["native_intent"])["before_ns"]
        for field, key in (("terminal", "sealed_ns"), ("first_stop", "observed_ns"), ("cleanup_proof", "observed_ns")):
            if row[field]:
                observation = json.loads(row[field])["data"]
                if observation.get("state") != "rebooted":
                    latest = max(latest, observation[key])
    if data["observed_ns"] < latest:
        raise ValueError("control closure predates retained native observation")
    if data["stop"] is not None:
        stop = native.event(item["context"], "stop", data["stop"], item["recorded_at"], proof)
        native._binding(row, stop, proof)
        if (data["stop"]["boot_id"] == data["observer"]["boot_id"]
                and data["stop"]["observed_ns"] > data["observed_ns"]):
            raise ValueError("control closure predates its stop")
    return True


def fresh_control(db, context, item, native, legs, proof):
    """Before DB or busy spool, prove this is the original live publisher."""
    data = item["data"]
    if data["state"] == "orphaned" or data["observer"] != owner_identity():
        raise ValueError("generic control publisher is not the original live owner")
    if db is None or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("control publication requires readable schema5 reservation")
    row = native.execution(db, context, legs)
    if row["launch_reservation"] is None or not bind_control(row, item, native, proof):
        raise ValueError("control publication requires its original reservation")


def record_control(db, context, item, legs, proof, native):
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("control evidence requires schema5")
    row = native.execution(db, context, legs)
    if row["launch_reservation"] is None or not bind_control(row, item, native, proof):
        native._conflict(db, row, native.encode(item))
        db.execute("UPDATE executions SET completion_kind='unbound' WHERE execution_seq=?", (row["execution_seq"],))
        return "conflict"
    if "startup" in item["data"] and max(
            native.owned_oom(item["data"]),
            native.owned_oom(json.loads(row["scope_registration"])) if row["scope_registration"] else 0) > 0:
        db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?", (row["execution_seq"],))
        row = native.execution(db, context, legs)
    if item["data"]["stop"] is not None:
        native._record(db, context, native.event(context, "stop", item["data"]["stop"], item["recorded_at"], proof), legs, proof)
        row = native.execution(db, context, legs)  # Stop reduction may occupy the first conflict slot.
    if row["control_closure"] is not None:
        if native._same_event(json.loads(row["control_closure"]), item):
            return "duplicate"
        represented = row["first_typed_conflict"] is None or native._same_event(json.loads(row["first_typed_conflict"]), item)
        native._conflict(db, row, native.encode(item))
        return "conflict" if represented else "retained"
    db.execute("UPDATE executions SET control_closure=?,launch_permission=CASE WHEN launch_permission='spent' "
               "THEN 'spent' ELSE 'cancelled' END,safety_kind=CASE WHEN ?='clean' THEN safety_kind "
               "ELSE coalesce(safety_kind,'uncertainty') END WHERE execution_seq=?",
               (native.encode(item), item["data"]["state"], row["execution_seq"]))
    return "recorded"


def _conflict_identity(raw):
    if raw is None:
        return None
    value = json.loads(raw)
    if isinstance(value, dict) and value.keys() >= {"version", "kind", "context", "data"}:
        return {key: value[key] for key in ("version", "kind", "context", "data")}
    return value


def safety_projection(row):
    control = json.loads(row["control_closure"]) if row["control_closure"] else None
    return (row["safety_kind"], row["completion_kind"] == "unbound", bool(row["cleanup_invalidated"]),
            _conflict_identity(row["first_typed_conflict"]),
            _conflict_identity(row["control_closure"]) if control and control["data"]["state"] != "clean" else None)


def _new_hazard(before, after):
    return ((after[0] is not None and after[0] != before[0])
            or any(after[i] and after[i] != before[i] for i in range(1, 5)))


def _advance_revision(db, row, provider):
    db.execute("INSERT INTO provider_state(hash,repo_path,provider) VALUES(?,?,?) "
               "ON CONFLICT(hash,provider) DO NOTHING", (row["hash"], row["repo_path"], provider))
    state = db.execute("SELECT repo_path,hazard_revision FROM provider_state WHERE hash=? AND provider=?",
                       (row["hash"], provider)).fetchone()
    if (state["repo_path"] != row["repo_path"] or type(state["hazard_revision"]) is not int
            or not 0 <= state["hazard_revision"] < 2**63 - 1):
        raise RuntimeError("provider safety identity or revision exhausted")
    db.execute("UPDATE provider_state SET hazard_revision=hazard_revision+1 WHERE hash=? AND provider=?",
               (row["hash"], provider))


def note_transition(db, before, after):
    """All native safety writers share the same represented before/after boundary."""
    if (db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            and _new_hazard(safety_projection(before), safety_projection(after))):
        _advance_revision(db, after, after["provider"])


def safety_transition(operation):
    """Count represented adverse changes inside the caller's existing transaction."""
    @wraps(operation)
    def observed(db, context, *args, **kwargs):
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return operation(db, context, *args, **kwargs)
        if not db.in_transaction:
            raise RuntimeError("safety transition requires an active transaction")
        original = db.execute("SELECT * FROM executions WHERE execution_seq=?", (context["execution_seq"],)).fetchone()
        if original is None:
            return operation(db, context, *args, **kwargs)  # Preserve the authoritative unowned-context error.
        before = dict(original)
        result = operation(db, context, *args, **kwargs)
        after = dict(db.execute("SELECT * FROM executions WHERE execution_seq=?", (context["execution_seq"],)).fetchone())
        note_transition(db, before, after)
        return result
    return observed


def _accepted_clean(db, state, native):
    import _genesis_code_intel_execution as proof
    import _genesis_index_marker_legs as legs

    selected = db.execute("SELECT * FROM executions WHERE execution_seq=?", (state["accepted_sequence"],)).fetchone()
    if selected is None:
        return False
    row = dict(selected)
    if (row["protocol"] != 3 or row["entry_kind"] != "claim"
            or any(row[key] != state[key] for key in ("hash", "repo_path", "provider"))
            or row["typed_applied_state"] != "consumed" or row["completion_kind"] != "success"
            or row["safety_kind"] is not None or row["first_typed_conflict"] is not None
            or row["native_intent"] is None or row["scope_registration"] is None
            or row["terminal"] is None or row["control_closure"] is None
            or not isinstance(state["accepted_evidence_reference"], str)
            or not state["accepted_evidence_reference"] or "\0" in state["accepted_evidence_reference"]):
        return False
    context = {key: row[key] for key in legs.CONTEXT}
    try:
        reserved = native.reservation(json.loads(row["launch_reservation"]), context)
        if native.source_identity(json.loads(state["accepted_source_identity"])) != reserved["source"]:
            return False
        for kind, field, checksum in (
            ("terminal", "terminal", state["accepted_terminal_sha256"]),
            ("control-closure", "control_closure", state["accepted_control_sha256"]),
        ):
            original = json.loads(row[field])
            native._object(original, ("version", "kind", "context", "data", "recorded_at"), "accepted witness")
            if type(original["version"]) is not int or original["version"] != 3 or original["context"] != context or original["kind"] != kind:
                return False
            checked = native.event(context, kind, original["data"], original["recorded_at"], proof)
            semantic = {key: checked[key] for key in ("version", "kind", "context", "data")}
            if native.digest(semantic) != checksum or not native._binding(row, checked, proof):
                return False
        return (row["terminal_sha256"] == state["accepted_terminal_sha256"]
                and json.loads(row["control_closure"])["data"]["state"] == "clean"
                and native._classification(row) == ("success", None) and native._cleanup_valid(row))
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        return False


def provider_blocked(db, h, provider, native):
    """Read safety coverage; this never grants a retry or advances acceptance."""
    if provider not in ("cbm", "gitnexus"):
        raise ValueError("invalid provider safety selection")
    state = db.execute("SELECT * FROM provider_state WHERE hash=? AND provider=?", (h, provider)).fetchone()
    accepted = 0
    if state is not None:
        accepted = state["accepted_sequence"]
        known = db.execute("SELECT repo_path FROM repo_state WHERE hash=?", (h,)).fetchone()
        if (known is None or state["repo_path"] != known[0]
                or type(accepted) is not int or not 0 <= accepted <= 2**63 - 1
                or type(state["hazard_revision"]) is not int or not 0 <= state["hazard_revision"] <= 2**63 - 1):
            raise RuntimeError("invalid provider safety identity or revision")
        if accepted and not _accepted_clean(db, state, native):
            return True
        if not accepted and any(state[name] is not None for name in (
            "accepted_terminal_sha256", "accepted_control_sha256", "accepted_source_identity", "accepted_evidence_reference",
        )):
            raise RuntimeError("partial provider safety acceptance")
    if db.execute("SELECT 1 FROM executions WHERE hash=? AND completion_kind='unbound' LIMIT 1", (h,)).fetchone():
        return True  # Contradictory possible-writer evidence is not a covered provider incident.
    return db.execute(
        "SELECT 1 FROM executions WHERE hash=? AND (provider=? OR "
        "(provider IS NULL AND original_tools IN (?,'both'))) AND execution_seq>? AND "
        "(safety_kind IS NOT NULL OR first_typed_conflict IS NOT NULL OR conflict_reason IS NOT NULL) LIMIT 1",
        (h, provider, provider, accepted),
    ).fetchone() is not None


def _requested(tools):
    if tools not in ("cbm", "gitnexus", "both"):
        raise RuntimeError("unassigned legacy attribution")
    return ("cbm", "gitnexus") if tools == "both" else (tools,)


def _mark_legacy(db, row):
    if (row["safety_kind"] is None and (row["entry_kind"] == "legacy-failure" or row["conflict_reason"]
            or row["scalar_action"] not in ("consume", "consume_full"))):
        db.execute("UPDATE executions SET safety_kind='uncertainty' WHERE execution_seq=?", (row["execution_seq"],))
        return True
    return False


def legacy_transition(operation):
    """Common scalar reducer coverage includes reconciliation and embedded outcomes."""
    @wraps(operation)
    def observed(db, row, *args, **kwargs):
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return operation(db, row, *args, **kwargs)
        if not db.in_transaction:
            raise RuntimeError("legacy safety requires an active transaction")
        before = (row["safety_kind"], row["conflict_reason"], row["conflict_raw"])
        result = operation(db, row, *args, **kwargs)
        after = dict(db.execute("SELECT * FROM executions WHERE execution_seq=?", (row["execution_seq"],)).fetchone())
        changed = _mark_legacy(db, after)
        if changed or before[1:] != (after["conflict_reason"], after["conflict_raw"]):
            for provider in _requested(after["original_tools"]):
                _advance_revision(db, after, provider)
        return result
    return observed


def _observe_legacy(db, source, provider, domain, snapshot, native, legs, *, raw=None):
    repo = legs._identity(db, source["hash"], source["repo_path"])
    identity = domain + ":" + native.digest(snapshot)
    retained = native.encode(snapshot) if raw is None else raw
    cursor = db.execute(
        "INSERT INTO executions(hash,repo_path,provider,protocol,entry_kind,claim_id,original_tools,mode,"
        "requested_at,attempts,legacy_unqualified,legacy_shared_attempts,nonce_origin,legacy_raw,safety_kind) "
        "VALUES(?,?,?,1,'legacy-observation',NULL,?,?,?,?,1,?,?,?,'uncertainty') ON CONFLICT DO NOTHING",
        (source["hash"], repo, provider, source.get("original_tools", provider), source["mode"],
         source["requested_at"], source["attempts"], source["legacy_shared_attempts"], identity, retained),
    )
    return cursor.rowcount == 1


def project_legacy(db, legs, native, *, h=None, initial=False, affected=None):
    """Interpret legacy history once; leave original settlement and work untouched."""
    if not db.in_transaction or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("legacy projection requires a schema5 transaction")
    changes = {} if affected is None else affected
    args = () if h is None else (h,)
    query = ("SELECT execution_seq FROM executions WHERE protocol IN (1,2) AND entry_kind<>'legacy-observation'"
             if h is None else "SELECT execution_seq FROM executions WHERE protocol IN (1,2) AND entry_kind<>'legacy-observation' AND hash=?")
    # Finish the ID cursor before mutating this table; retain no history blobs.
    # https://www.sqlite.org/isolation.html#no_isolation_between_operations_on_the_same_database_connection
    sequences = [row[0] for row in db.execute(query, args).fetchall()]
    for sequence in sequences:
        row = dict(db.execute("SELECT * FROM executions WHERE execution_seq=?", (sequence,)).fetchone())
        if _mark_legacy(db, row):
            for provider in _requested(row["original_tools"]):
                changes[row["hash"], provider] = row
        if initial and row["original_tools"] == "both" and row["scalar_action"] in ("consume", "consume_full"):
            for pending in db.execute("SELECT * FROM pending WHERE hash=?", (row["hash"],)):
                source = dict(pending)
                if _observe_legacy(db, source, source["provider"], "legacy-pending-v1", source, native, legs):
                    changes[source["hash"], source["provider"]] = source
    pending_query = ("SELECT * FROM pending WHERE legacy_unqualified=1" if h is None
                     else "SELECT * FROM pending WHERE legacy_unqualified=1 AND hash=?")
    for pending in db.execute(pending_query, args):
        source = dict(pending)
        if _observe_legacy(db, source, source["provider"], "legacy-pending-v1", source, native, legs):
            changes[source["hash"], source["provider"]] = source
    if affected is None:
        for (_, provider), source in changes.items():
            _advance_revision(db, source, provider)


def legacy_import(kind):
    """Legacy-only import batches count each affected provider once, including nested scalar calls."""
    def decorate(operation):
        @wraps(operation)
        def observed(db, h, data, *args, **kwargs):
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION or (kind == "pending" and not kwargs.get("legacy", True)):
                return operation(db, h, data, *args, **kwargs)
            import _genesis_index_marker_execution as native
            import _genesis_index_marker_legs as legs

            before = dict(db.execute("SELECT provider,hazard_revision FROM provider_state WHERE hash=?", (h,)))
            result = operation(db, h, data, *args, **kwargs)
            changes = {}
            project_legacy(db, legs, native, h=h, affected=changes)
            if kind == "claim" and isinstance(data.get("claim_id"), str) and data["claim_id"]:
                row = db.execute("SELECT * FROM executions WHERE hash=? AND protocol=1 AND entry_kind='claim' AND claim_id=?",
                                 (h, data["claim_id"])).fetchone()
                if row is not None:
                    source = dict(row)
                    core = {"repo_path": os.path.realpath(data["repo_path"]), "original_tools": data["tools"],
                            "mode": data["mode"], "requested_at": float(data["requested_at"]),
                            "attempts": data["attempts"], "nonce_origin": "legacy-provided"}
                    if any(source[name] != value for name, value in core.items()):
                        candidate = dict(hash=h, protocol=1, entry_kind="claim", claim_id=data["claim_id"], **core)
                        for provider in set(_requested(source["original_tools"])) | set(_requested(data["tools"])):
                            if _observe_legacy(db, source, provider, "legacy-identity-v1", candidate, native, legs, raw=args[0]):
                                changes[h, provider] = source
            current = dict(db.execute("SELECT provider,hazard_revision FROM provider_state WHERE hash=?", (h,)))
            for (_, provider), source in changes.items():
                if current.get(provider, 0) == before.get(provider, 0):
                    _advance_revision(db, source, provider)
            return result
        return observed
    return decorate
