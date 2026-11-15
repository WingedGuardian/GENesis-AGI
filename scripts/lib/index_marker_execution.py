"""Typed native execution transactions on the facade's admitted connection.

This module has no database opener or process launcher. Automatic readiness is
separate; scalar history is never native intent or cleanup evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import uuid
from pathlib import PurePosixPath

import _genesis_index_marker_recovery as recovery

INTEGER_MAX = 2**63 - 1
FIELDS = (
    "launch_reservation BLOB", "scope_registration BLOB", "native_intent BLOB",
    "terminal BLOB", "terminal_sha256 TEXT", "first_stop BLOB",
    "cleanup_first_failure BLOB", "cleanup_proof BLOB", "first_typed_conflict BLOB",
    "launch_permission TEXT CHECK(launch_permission IN ('reserved','spent','cancelled'))",
    "completion_kind TEXT", "safety_kind TEXT", "typed_applied_state TEXT",
    "typed_charged_attempts INTEGER",
    "cleanup_invalidated INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_invalidated IN (0,1))",
)


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", value):
        raise RuntimeError("unexpected retained queue column")
    return '"' + value + '"'


def upgrade(db, schema: str) -> None:
    """Rebuild protocol CHECKs atomically; the facade owns the transaction."""
    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='executions'").fetchone()
    high_water = sequence[0] if sequence else 0
    definitions = {}
    columns = {}
    for table in ("executions", "inflight"):
        body = schema.split(f"CREATE TABLE {table} (", 1)[1].split(";", 1)[0]
        body = body.replace("protocol IN (1,2)", "protocol IN (1,2,3)")
        if table == "executions":
            body = body.replace(" scalar_action TEXT,", " " + ", ".join(FIELDS) + ", scalar_action TEXT,")
        else:
            body = body.replace("REFERENCES executions(", "REFERENCES executions_native_new(")
        definitions[table] = body
        columns[table] = ",".join(_identifier(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))
    for table in ("executions", "inflight"):
        db.execute(f"CREATE TABLE {table}_native_new (" + definitions[table])
        names = columns[table]
        # Table names are the two literals above; columns pass _identifier.
        db.execute(f"INSERT INTO {table}_native_new({names}) SELECT {names} FROM {table}")  # noqa: S608
    db.execute("DROP TABLE inflight")
    db.execute("DROP TABLE executions")
    db.execute("ALTER TABLE executions_native_new RENAME TO executions")
    db.execute("ALTER TABLE inflight_native_new RENAME TO inflight")
    db.execute("CREATE INDEX executions_provider_sequence ON executions(hash,provider,execution_seq)")
    db.execute("CREATE INDEX executions_legacy_sequence ON executions(hash,protocol,original_tools,execution_seq)")
    db.execute("CREATE INDEX executions_cleanup ON executions(hash,cleanup_invalidated,execution_seq) WHERE protocol=3")
    db.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'executions',? "
               "WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='executions')", (high_water,))
    db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='executions'", (high_water,))
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("native queue upgrade failed foreign-key validation")


def context(value, legs, hash_repo) -> dict:
    return legs.validate_context(value, hash_repo, protocol=3)


def expected_unit(value: dict) -> str:
    suffix = 2**128 + int(value["claim_id"], 16)
    return f"code-intel-{value['hash'][:12]}-{value['provider']}-{suffix}"


def execution(db, value, legs):
    return legs._execution(db, value)


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"invalid {label} fields")
    return value


def _integer(value, label, *, positive=False):
    if type(value) is not int or not int(positive) <= value <= INTEGER_MAX:
        raise ValueError(f"invalid {label}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"invalid {label}")
    return value


def _hex(value, size, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _boot(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("invalid boot identity")
    return value


def source_identity(value):
    _object(value, ("commit", "provider_digest", "provider_version", "configuration_sha256", "files"), "source identity")
    if not isinstance(value["commit"], str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["commit"]) is None:
        raise ValueError("invalid source commit")
    _hex(value["provider_digest"], 64, "provider digest")
    _hex(value["configuration_sha256"], 64, "configuration digest")
    _text(value["provider_version"], "provider version")
    files = value["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("source files are required")
    for name, checksum in files.items():
        _text(name, "source filename")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError("source filename must be repository-relative")
        _hex(checksum, 64, "source file digest")
    return value


def reservation(value, claimed):
    _object(value, ("unit", "run_mode", "source", "owner", "cap_bytes", "route", "slice", "wall_seconds"), "launch reservation")
    if value["unit"] != expected_unit(claimed):
        raise ValueError("reservation has another execution scope")
    if value["run_mode"] not in ("fast", "moderate", "full"):
        raise ValueError("invalid native run mode")
    if _integer(value["cap_bytes"], "native cap", positive=True) != 8 * 1024**3:
        raise ValueError("native journal requires the full accepted job cap")
    _integer(value["wall_seconds"], "existing wall bound", positive=True)
    owner = _object(value["owner"], ("pid", "start_ticks", "boot_id"), "owner identity")
    _integer(owner["pid"], "owner pid", positive=True)
    _integer(owner["start_ticks"], "owner start", positive=True)
    _boot(owner["boot_id"])
    source_identity(value["source"])
    route, parent = value["route"], value["slice"]
    if not isinstance(parent, str) or re.fullmatch(r"[A-Za-z0-9_.\\-]+\.slice", parent) is None:
        raise ValueError("invalid expected slice")
    if route == "workload" and parent == "genesis-workload.slice":
        return value
    if route == "capped" and claimed["provider"] == "gitnexus" and parent == "app-capped.slice":
        return value
    if route == "inherit" and claimed["provider"] == "cbm":
        return value  # The trusted producer must independently bind the actual owner's Unit.Slice.
    raise ValueError("native route does not match the reservation")


def _conflict(db, row, raw):
    db.execute("UPDATE executions SET first_typed_conflict=coalesce(first_typed_conflict,?),"
               "safety_kind=CASE WHEN safety_kind='resource' THEN 'resource' ELSE 'conflict' END "
               "WHERE execution_seq=?", (raw, row["execution_seq"]))
    return "conflict"


def _launch_held(db, value, row, *, fresh_cleanup=None):
    if row["nonce_origin"] == "selected-recovery":
        raise ValueError("selected execution support is not installed")
    return blocked(db, value["hash"], value["provider"], exclude=row["execution_seq"],
                   fresh_cleanup=fresh_cleanup)


@recovery.safety_transition
def reserve(db, value, data, legs, *, fresh_cleanup=None):
    row = execution(db, value, legs)
    raw = encode(data)
    if row["launch_reservation"] is not None:
        if bytes(row["launch_reservation"]) != raw:
            return _conflict(db, row, raw)
        return "cancelled" if row["launch_permission"] == "cancelled" else "already_reserved"
    if row["typed_applied_state"] is not None or row["launch_permission"] == "cancelled":
        return "cancelled"
    _owns(db, row)
    if _launch_held(db, value, row, fresh_cleanup=fresh_cleanup):
        return "held"
    if ("fast", "moderate", "full").index(data["run_mode"]) < legs.MODES.index(row["mode"]):
        raise ValueError("reservation cannot narrow the claimed workload")
    db.execute("UPDATE executions SET launch_reservation=?,launch_permission='reserved' "
               "WHERE execution_seq=?", (raw, row["execution_seq"]))
    return "reserved"


def _owns(db, row):
    if row["typed_applied_state"] is not None or db.execute(
        "SELECT 1 FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
        (row["hash"], row["execution_seq"], row["claim_id"]),
    ).fetchone() is None:
        raise RuntimeError("native launch does not own current inflight")


def _bound_facts(facts, reserved, proof):
    if not proof._facts(facts):
        raise ValueError("invalid native scope facts")
    leaf, root = PurePosixPath(facts["leaf"]["path"]), PurePosixPath(facts["root"]["path"])
    parents = list(leaf.parents)
    if (root not in parents or leaf.name != reserved["unit"] + ".scope"
            or leaf.parent.name != reserved["slice"]
            or not facts["membership"].endswith("/" + leaf.name + "\n")
            or facts["memory_max"] != reserved["cap_bytes"]
            or facts["reporter"]["boot_id"] != reserved["owner"]["boot_id"]
            or [item["identity"]["path"] for item in facts["ancestors"]]
            != [str(path) for path in parents[:parents.index(root) + 1]]
            or any(item["identity"]["device"] != facts["root"]["device"] for item in facts["ancestors"])
            or facts["leaf"]["device"] != facts["root"]["device"]
            or not facts["ancestors"] or facts["ancestors"][-1]["identity"] != facts["root"]):
        raise ValueError("scope facts do not match reserved kernel ownership")


def registration_data(data, proof):
    """One complete shape for live registration and retained startup observations."""
    _object(data, ("unit", "invocation_id", "facts", "stop_usec"), "scope registration")
    _text(data["unit"], "scope unit")
    _hex(data["invocation_id"], 32, "scope invocation")
    _integer(data["stop_usec"], "scope stop budget", positive=True)
    if not proof._facts(data["facts"]):
        raise ValueError("invalid native scope facts")
    return data


@recovery.safety_transition
def register(db, value, data, legs, proof, *, fresh_cleanup=None):
    registration_data(data, proof)
    row = execution(db, value, legs)
    _owns(db, row)
    if row["launch_permission"] != "reserved":
        raise RuntimeError("registration requires an open launch reservation")
    reserved = json.loads(row["launch_reservation"])
    if data["unit"] != reserved["unit"] or 2 * data["stop_usec"] > reserved["wall_seconds"] * 1_000_000:
        raise ValueError("scope name or stop budget does not match reservation")
    _bound_facts(data["facts"], reserved, proof)
    raw = encode(data)
    if row["scope_registration"] is not None:
        return "already_registered" if bytes(row["scope_registration"]) == raw else _conflict(db, row, raw)
    if _launch_held(db, value, row, fresh_cleanup=fresh_cleanup):
        return "held"
    db.execute("UPDATE executions SET scope_registration=? WHERE execution_seq=?", (raw, row["execution_seq"]))
    return "registered"


@recovery.safety_transition
def begin(db, value, data, legs, proof, *, fresh_cleanup=None):
    _object(data, ("source", "executable", "facts", "registration_sha256", "before_ns"), "native intent")
    source_identity(data["source"])
    _integer(data["before_ns"], "native start observation", positive=True)
    _hex(data["registration_sha256"], 64, "scope registration digest")
    executable = _object(data["executable"], ("device", "inode", "sha256"), "accepted executable")
    _integer(executable["device"], "executable device")
    _integer(executable["inode"], "executable inode", positive=True)
    _hex(executable["sha256"], 64, "accepted executable digest")
    row = execution(db, value, legs)
    raw = encode(data)
    if row["native_intent"] is not None:
        return "already_started" if bytes(row["native_intent"]) == raw else _conflict(db, row, raw)
    _owns(db, row)
    if row["terminal"] is not None or row["cleanup_proof"] is not None or row["completion_kind"] == "unbound":
        raise RuntimeError("retained terminal or cleanup closes future native authority")
    if row["launch_permission"] != "reserved" or row["scope_registration"] is None:
        raise RuntimeError("native intent requires registered, uncancelled launch authority")
    registered, reserved = json.loads(row["scope_registration"]), json.loads(row["launch_reservation"])
    if data["registration_sha256"] != digest(registered) or data["source"] != reserved["source"]:
        raise ValueError("native source or scope binding changed")
    _bound_facts(data["facts"], reserved, proof)
    proof._complete(registered["facts"], data["facts"])
    if data["facts"]["events"]["oom_kill"] > registered["facts"]["events"]["oom_kill"]:
        db.execute("UPDATE executions SET safety_kind='resource',first_typed_conflict="
                   "coalesce(first_typed_conflict,?) WHERE execution_seq=?", (raw, row["execution_seq"]))
        return "resource"  # Preparation OOM is durable; no indexing intent/debt is fabricated.
    if _launch_held(db, value, row, fresh_cleanup=fresh_cleanup):
        return "held"
    db.execute("UPDATE executions SET native_intent=?,launch_permission='spent' WHERE execution_seq=?",
               (raw, row["execution_seq"]))
    return "started"


def cancel(db, value, legs):
    row = execution(db, value, legs)
    if row["native_intent"] is not None or row["launch_permission"] == "spent":
        return "spent"
    db.execute("UPDATE executions SET launch_permission='cancelled' WHERE execution_seq=?",
               (row["execution_seq"],))
    return "cancelled"


def status(db, value, legs):
    row = execution(db, value, legs)
    for name in ("launch_reservation", "scope_registration", "native_intent", "terminal",
                 "first_stop", "cleanup_first_failure", "cleanup_proof", "first_typed_conflict",
                 *(("control_closure",) if "control_closure" in row else ())):
        if row[name] is not None:
            try:
                row[name] = json.loads(row[name])
            except (ValueError, UnicodeError):
                row[name] = {"raw_hex": bytes(row[name]).hex()}
    for name in ("legacy_raw", "conflict_raw"):
        if row[name] is not None:
            row[name] = {"raw_hex": bytes(row[name]).hex()}
    return row


def _receipt_time(value):
    if type(value) not in (int, float):
        raise ValueError("invalid native receipt timestamp")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError("native receipt timestamp exceeds finite REAL domain") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("invalid native receipt timestamp")
    return normalized


def event(value, kind, data, recorded_at, proof):
    recorded_at = _receipt_time(recorded_at)
    # Copy through the actual encoding boundary: reject non-JSON/NaN values and
    # do not mutate the caller when the existing report decoder expands facts.
    data = json.loads(encode(data))
    if kind == "terminal":
        _object(data, ("report", "provider_ok", "sealed_ns", "source"), "native terminal")
        proof.validate_report(data["report"])
        rc = data["report"]["payload_returncode"]
        if rc is not None and not -INTEGER_MAX - 1 <= rc <= INTEGER_MAX:
            raise ValueError("native return status overflows retained integer domain")
        _integer(data["sealed_ns"], "terminal sealing observation", positive=True)
        source_identity(data["source"])
        if type(data["provider_ok"]) is not bool:
            raise ValueError("provider validation must be explicit")
    elif kind == "control-closure":
        recovery.control_data(data, sys.modules[__name__], proof)
    elif kind == "stop":
        _object(data, ("reason", "observed_ns", "boot_id", "unit"), "native stop")
        if data["reason"] not in ("pressure_stop", "wall", "cancellation"):
            raise ValueError("invalid native stop reason")
        _integer(data["observed_ns"], "stop observation", positive=True)
        _boot(data["boot_id"])
        _text(data["unit"], "stop unit")
    elif kind in ("cleanup", "cleanup-disproof"):
        if not isinstance(data, dict):
            raise ValueError("invalid cleanup observation")
        state = data.get("state")
        if state in ("quiescent", "unresolved"):
            _object(data, ("state", "registration_sha256", "facts", "populated", "owned_wait", "observed_ns"), "cleanup observation")
            _integer(data["populated"], "recursive population")
            if data["populated"] not in (0, 1) or type(data["owned_wait"]) is not bool or not proof._facts(data["facts"]):
                raise ValueError("invalid cleanup population or owned wait")
            if kind == "cleanup" and state == "quiescent" and (data["populated"] != 0 or not data["owned_wait"]):
                raise ValueError("quiescence requires recursive empty plus owned wait")
        elif kind == "cleanup" and state == "removed":
            _object(data, ("state", "registration_sha256", "root", "mounts_sha256", "boot_id", "absent", "observed_ns"), "removed scope")
            if not proof._mapping(data["root"]) or data["absent"] is not True:
                raise ValueError("removal requires original root and actual absence")
            _hex(data["mounts_sha256"], 64, "cleanup mount identity")
            _boot(data["boot_id"])
        elif kind == "cleanup" and state == "rebooted":
            _object(data, ("state", "registration_sha256", "old_boot_id", "new_boot_id", "observed_ns"), "changed boot")
            _boot(data["old_boot_id"])
            _boot(data["new_boot_id"])
            if data["old_boot_id"] == data["new_boot_id"]:
                raise ValueError("boot has not changed")
        else:
            raise ValueError("invalid cleanup observation state")
        _hex(data["registration_sha256"], 64, "cleanup scope registration")
        _integer(data["observed_ns"], "cleanup observation", positive=True)
        if kind == "cleanup-disproof" and data["populated"] != 1:
            raise ValueError("cleanup disproof requires actual population")
    else:
        raise ValueError("unsupported native event kind")
    return dict(version=3, kind=kind, context=value, data=data, recorded_at=recorded_at)


def _same_event(first, second):
    keys = ("version", "kind", "context", "data")
    return first.keys() >= set(keys) and all(first[key] == second[key] for key in keys)


def _binding(row, item, proof):
    reserved = json.loads(row["launch_reservation"]) if row["launch_reservation"] else None
    registered = json.loads(row["scope_registration"]) if row["scope_registration"] else None
    data, kind = item["data"], item["kind"]
    if reserved is None:
        return False  # Keep an out-of-order spool; it is not completion authority.
    if kind == "control-closure":
        return recovery.bind_control(row, item, sys.modules[__name__], proof)
    if kind == "stop":
        if data["unit"] != reserved["unit"] or data["boot_id"] != reserved["owner"]["boot_id"]:
            raise ValueError("stop does not name the reserved scope")
        return True
    if registered is None:
        return False
    if kind == "terminal":
        report = data["report"]
        if data["source"] != reserved["source"]:
            raise ValueError("terminal source changed")
        if report["payload_started"]:
            if row["native_intent"] is None:
                return False
            intent = json.loads(row["native_intent"])
            if report["before"] != intent["facts"] or data["sealed_ns"] < intent["before_ns"]:
                raise ValueError("terminal does not bind actual native intent")
        for facts in (report["before"], report["after"]):
            if facts is not None:
                _bound_facts(facts, reserved, proof)
                proof._complete(registered["facts"], facts)
    else:
        if data["registration_sha256"] != digest(registered):
            raise ValueError("cleanup names another scope invocation")
        if kind == "cleanup" and data["state"] != "unresolved" and row["launch_permission"] not in ("spent", "cancelled"):
            raise ValueError("cleanup requires closed future launch permission")
        if data["state"] != "rebooted":
            latest = json.loads(row["native_intent"])["before_ns"] if row["native_intent"] else 0
            if row["terminal"]:
                latest = max(latest, json.loads(row["terminal"])["data"]["sealed_ns"])
            if data["observed_ns"] < latest:
                raise ValueError("cleanup predates retained native observations")
        if data["state"] in ("removed", "rebooted"):
            if row["launch_permission"] not in ("spent", "cancelled"):
                raise ValueError("cleanup requires closed future launch permission")
            baseline = registered["facts"]
            if data["state"] == "removed":
                if any(data[key] != baseline[key] for key in ("root", "mounts_sha256")) or data["boot_id"] != baseline["reporter"]["boot_id"]:
                    raise ValueError("removal changed original kernel root")
            elif data["old_boot_id"] != baseline["reporter"]["boot_id"]:
                raise ValueError("reboot does not name original boot")
        else:
            _bound_facts(data["facts"], reserved, proof)
            baseline = registered["facts"]
            if row["terminal"]:
                baseline = json.loads(row["terminal"])["data"]["report"]["after"] or baseline
            proof._complete(baseline, data["facts"])
    return True


def _cleanup_valid(row):
    """A retained fact releases ownership only while it still applies."""
    if (row["cleanup_proof"] is None or row["cleanup_invalidated"] or _cleanup_disproved(row)
            or row["completion_kind"] == "unbound"
            or row["launch_permission"] not in ("spent", "cancelled")):
        return False
    proof = json.loads(row["cleanup_proof"])["data"]
    if proof["state"] == "rebooted":
        return True  # Its changed kernel boot closes the old tasks' lifetime.
    latest = json.loads(row["native_intent"])["before_ns"] if row["native_intent"] else 0
    if row["terminal"]:
        latest = max(latest, json.loads(row["terminal"])["data"]["sealed_ns"])
    return proof["observed_ns"] >= latest


def _nonempty_after(previous, data):
    # An observation on the old boot cannot revive old tasks after a proven
    # reboot. Other accepted population facts have the registered original boot.
    return (data.get("populated") == 1 and previous["state"] != "rebooted"
            and data["observed_ns"] >= previous["observed_ns"])


def _cleanup_disproved(row):
    if row["cleanup_proof"] is None:
        return False
    previous = json.loads(row["cleanup_proof"])["data"]
    for slot in ("cleanup_first_failure", "first_typed_conflict"):
        if row[slot] is not None:
            item = json.loads(row[slot])
            if item.get("kind") in ("cleanup", "cleanup-disproof") and _nonempty_after(previous, item["data"]):
                return True
    return False


def owned_oom(data):
    """Largest validated owned counter; absence never invents a resource cause."""
    if "startup" in data:
        startup = data["startup"]
        return max(owned_oom(startup["scope"]), startup["facts"]["events"]["oom_kill"])
    facts = (data["report"]["before"], data["report"]["after"]) if "report" in data else (data.get("facts"),)
    return max((value["events"]["oom_kill"] for value in facts if value is not None), default=0)


def _known_owned_oom(row, legs, proof, retained):
    """Interpret represented and retained facts for one exact historical subject."""
    counts = [owned_oom(json.loads(row["scope_registration"]))]
    if row["native_intent"]:
        counts.append(owned_oom(json.loads(row["native_intent"])))
    events = [json.loads(row[key]) for key in ("terminal", "cleanup_proof", "cleanup_first_failure", "first_typed_conflict", "control_closure")
              if key in row and row[key] is not None]
    value = {key: row[key] for key in legs.CONTEXT}
    for item in retained:
        if item["context"] != value:
            raise ValueError("retained resource evidence has another execution")
        checked = event(value, item["kind"], item["data"], item["recorded_at"], proof)
        if not _binding(row, checked, proof):
            raise ValueError("retained resource evidence lacks its original binding")
        events.append(checked)
    counts.extend(owned_oom(item["data"]) for item in events
                  if item.get("kind") in ("terminal", "cleanup", "cleanup-disproof", "control-closure"))
    return max(counts)


def retain_fresh_cleanup(db, row, legs, proof, *, collect, proc, clock, retained, publish):
    """Re-read the lifetime and retain owned OOM before any physical release.

    Physical proof is never cached as a recovery override. First proof, completion,
    debt and invalidation remain unchanged. The facade owns the exclusive barrier
    and supplies its collector, retained artifacts and durable publisher on this
    admitted connection; no opener or nested lock belongs here.
    """
    registered = json.loads(row["scope_registration"])
    previous = json.loads(row["cleanup_proof"])["data"]
    registration_sha256 = digest(registered)
    owned_wait = previous.get("owned_wait") is True
    try:
        data = collect(registered, registration_sha256, proof, owned_wait=owned_wait, proc=proc)
    except (OSError, ValueError):
        # Unavailable proof holds this execution while unrelated recovery continues.
        return False
    value = {key: row[key] for key in legs.CONTEXT}
    item = event(value, "cleanup", data, clock(), proof)
    if not _binding(row, item, proof):
        return False
    data, value = item["data"], item["context"]
    if owned_oom(data) > _known_owned_oom(row, legs, proof, retained):
        publish(item)
        record(db, value, item, legs, proof)
    if data["state"] not in ("quiescent", "removed", "rebooted"):
        return False
    return _cleanup_valid({**row, "cleanup_invalidated": 0, "cleanup_proof": encode(item)})


def _classification(row):
    terminal = json.loads(row["terminal"]) if row["terminal"] else None
    stop = json.loads(row["first_stop"]) if row["first_stop"] else None
    safety = row["safety_kind"] if (
        row["typed_applied_state"] is not None or row["safety_kind"] in ("resource", "conflict")
    ) else None
    if row["cleanup_first_failure"]:
        safety = safety or "uncertainty"
    if "control_closure" in row and row["control_closure"] and json.loads(row["control_closure"])["data"]["state"] != "clean":
        safety = safety or "uncertainty"
    if row["completion_kind"] == "unbound":
        return "unbound", safety or "uncertainty"
    resource = stop and stop["data"]["reason"] == "pressure_stop"
    if terminal:
        report, sealed = terminal["data"]["report"], terminal["data"]["sealed_ns"]
        registered = json.loads(row["scope_registration"])
        owned_resource = owned_oom(terminal["data"]) > registered["facts"]["events"]["oom_kill"]
        resource = resource or owned_resource
        clean = (report["outcome"] == "complete" and report["payload_returncode"] == 0
                 and terminal["data"]["provider_ok"] and not owned_resource)
        before_stop = stop is None or sealed < stop["data"]["observed_ns"]
        if not report["payload_started"] and row["native_intent"] is None:
            completion = "unstarted"
        elif clean and before_stop:
            completion = "success"
        elif report["outcome"] == "complete":
            completion = "failure"
        else:
            completion, safety = "unknown", safety or "uncertainty"
    else:
        completion = "unknown" if row["native_intent"] else "unstarted"
        if row["native_intent"]:
            safety = safety or "uncertainty"
    if resource:
        safety = "resource"
    elif stop is not None:
        safety = safety or "uncertainty"
    return completion, safety


@recovery.safety_transition
def record(db, value, item, legs, proof):
    if item["kind"] == "control-closure":
        return recovery.record_control(db, value, item, legs, proof, sys.modules[__name__])
    return _record(db, value, item, legs, proof)


def _record(db, value, item, legs, proof):
    row = execution(db, value, legs)
    if not _binding(row, item, proof):
        # Do not invent an absent reservation/registration/intent, and do not
        # infer no-start in the face of contradictory exact-context evidence.
        represented = row["first_typed_conflict"] is None or _same_event(json.loads(row["first_typed_conflict"]), item)
        _conflict(db, row, encode(item))
        db.execute("UPDATE executions SET completion_kind='unbound' WHERE execution_seq=?", (row["execution_seq"],))
        return "conflict" if represented else "retained"
    kind, raw = item["kind"], encode(item)
    if kind == "stop" and item["data"]["reason"] == "pressure_stop":
        db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?", (row["execution_seq"],))
        row["safety_kind"] = "resource"
    facts = item["data"].get("facts")
    if kind in ("cleanup", "cleanup-disproof") and facts is not None:
        baseline = json.loads(row["scope_registration"])["facts"]
        if facts["events"]["oom_kill"] > baseline["events"]["oom_kill"]:
            db.execute("UPDATE executions SET safety_kind='resource' WHERE execution_seq=?", (row["execution_seq"],))
            row["safety_kind"] = "resource"
    if (kind in ("cleanup", "cleanup-disproof") and row["cleanup_proof"] is not None
            and _nonempty_after(json.loads(row["cleanup_proof"])["data"], item["data"])):
        db.execute("UPDATE executions SET cleanup_invalidated=1 WHERE execution_seq=?", (row["execution_seq"],))
    slot = {"terminal": "terminal", "stop": "first_stop", "cleanup": "cleanup_proof",
            "cleanup-disproof": "cleanup_first_failure"}[kind]
    if kind == "cleanup" and item["data"]["state"] == "unresolved":
        slot = "cleanup_first_failure"
    if row[slot] is not None:
        if _same_event(json.loads(row[slot]), item):
            return "duplicate"
        if slot == "cleanup_proof":
            # A later empty observation cannot erase the first proof or the
            # sticky disproof obligation. Retain distinct observations as spools.
            return "retained"
        if row["first_typed_conflict"] is not None and not _same_event(json.loads(row["first_typed_conflict"]), item):
            return "retained"
        _conflict(db, row, raw)
        return "conflict"
    # slot comes only from the closed event-kind map above; values are bound.
    db.execute(f"UPDATE executions SET {slot}=? WHERE execution_seq=?", (raw, row["execution_seq"]))  # noqa: S608
    if kind == "terminal":
        identity = {key: item[key] for key in ("version", "kind", "context", "data")}
        db.execute("UPDATE executions SET terminal_sha256=? WHERE execution_seq=?", (digest(identity), row["execution_seq"]))
    current = execution(db, value, legs)
    if _cleanup_disproved(current):
        db.execute("UPDATE executions SET cleanup_invalidated=1 WHERE execution_seq=?", (row["execution_seq"],))
        current["cleanup_invalidated"] = 1
    completion, safety = _classification(current)
    db.execute("UPDATE executions SET completion_kind=?,safety_kind=? WHERE execution_seq=?",
               (row["completion_kind"] if row["typed_applied_state"] else completion, safety, row["execution_seq"]))
    return "recorded"


def _ownership_clear(row, fresh_cleanup=None):
    if recovery.control_pending(row):
        return False
    if ("control_closure" in row and row["scope_registration"] is None
            and row["native_intent"] is None and row["launch_permission"] == "cancelled"):
        return True  # Structural no-future-native permission, not kernel emptiness.
    if row["cleanup_invalidated"]:
        return fresh_cleanup is not None and fresh_cleanup(row) is True
    return _cleanup_valid(row)


def apply(db, value, legs, *, fresh_cleanup=None):
    row = execution(db, value, legs)
    if row["completion_kind"] == "unbound":
        return None
    if row["typed_applied_state"] is None:
        if row["terminal"] is None and row["first_stop"] is None and row["cleanup_proof"] is None:
            return None  # A live/may-start owner is not an inferred failed run.
        completion, safety = _classification(row)
        _owns(db, row)
        increment = False
        if completion == "success":
            state, disposition, charged = "consumed", "consumed", row["attempts"]
            reserved = json.loads(row["launch_reservation"])
            if row["provider"] == "cbm" and reserved["run_mode"] == "full":
                db.execute("UPDATE repo_state SET last_full=?,full_backoff=NULL WHERE hash=?",
                           (_receipt_time(json.loads(row["terminal"])["recorded_at"]), row["hash"]))
        else:
            reserved = json.loads(row["launch_reservation"]) if row["launch_reservation"] else None
            backoff = (row["provider"] == "cbm" and row["mode"] != "full" and reserved
                       and reserved["run_mode"] == "full" and safety is None and completion == "failure")
            if backoff:
                db.execute("UPDATE repo_state SET full_backoff=? WHERE hash=?",
                           (_receipt_time(json.loads(row["terminal"])["recorded_at"]), row["hash"]))
            increment = completion != "unstarted" and not backoff
        selected = None
        if row["nonce_origin"] == "selected-recovery":
            from _genesis_index_marker_retry import settle_selected

            selected = settle_selected(db, row, sys.modules[__name__],
                                       completion=completion, safety=safety, increment=increment)
        if selected is not None:
            state, disposition, charged, safety = selected
        elif completion != "success":
            state, disposition, charged = legs._restore(db, row, increment)
        db.execute("UPDATE executions SET typed_applied_state=?,disposition=?,typed_charged_attempts=?,"
                   "completion_kind=?,safety_kind=? WHERE execution_seq=?",
                   (state, disposition, charged, completion, safety, row["execution_seq"]))
        before, row = row, execution(db, value, legs)
        if selected is not None:
            recovery.note_transition(db, before, row)
    if _ownership_clear(row, fresh_cleanup):
        db.execute("DELETE FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
                   (row["hash"], row["execution_seq"], row["claim_id"]))
    return row["typed_applied_state"]


def drain(directory, db, legs, proof, hash_repo, *, residual=None):
    """Called only with the facade's exclusive barrier and active transaction."""
    pending = sorted(directory.glob(".execution-spool-*.spool"))
    retired = []
    # First publish all representable facts/proofs. Then interpret still-raw
    # hazards against that complete snapshot before any decision can release
    # ownership. Publishers are excluded throughout; no new spool can arrive.
    for phase in range(2):
        retained = []
        for path in pending:
            state = _drain_one(path, db, legs, proof, hash_repo, residual=residual if phase == 1 else None)
            if state in ("waiting", "retained"):
                retained.append(path)
            else:
                retired.append(path)
        pending = retained
    return retired


def _drain_one(path, db, legs, proof, hash_repo, *, residual=None):
    name = re.fullmatch(r"\.execution-spool-([0-9a-f]{16})-([0-9a-f]{32})-[0-9a-f]{32}\.spool", path.name)
    if name is None:
        raise ValueError("invalid native spool filename")
    item = json.loads(path.read_bytes(), object_pairs_hook=proof._unique)
    _object(item, ("version", "kind", "context", "data", "recorded_at"), "native event")
    if type(item["version"]) is not int or item["version"] != 3:
        raise ValueError("unsupported native event version")
    value = context(item["context"], legs, hash_repo)
    if (value["hash"], value["claim_id"]) != name.groups():
        raise ValueError("native spool filename does not own its context")
    checked = event(value, item["kind"], item["data"], item["recorded_at"], proof)
    state = record(db, value, checked, legs, proof)
    if residual is not None and state in ("waiting", "retained"):
        identity = {key: checked[key] for key in ("version", "kind", "context", "data")}
        residual.append((value["hash"], value["provider"], checked["kind"], digest(identity)))
    return state

def _provider_blocked(db, h, provider):
    if db.execute("PRAGMA user_version").fetchone()[0] == recovery.SCHEMA_VERSION:
        return recovery.provider_blocked(db, h, provider, sys.modules[__name__])
    rows = db.execute(
        "SELECT 1 FROM executions WHERE hash=? AND protocol=3 AND ("
        "completion_kind='unbound' OR "
        "(provider=? AND (safety_kind IS NOT NULL OR first_typed_conflict IS NOT NULL))) LIMIT 1",
        (h, provider),
    )
    return rows.fetchone() is not None


def blocked(db, h: str, provider: str, *, exclude=0, fresh_cleanup=None) -> bool:
    # Physical cleanup cannot release the independent provider safety hold.
    if _provider_blocked(db, h, provider):
        return True
    return physical_blocked(db, h, exclude=exclude, fresh_cleanup=fresh_cleanup) or _provider_blocked(db, h, provider)


def physical_blocked(db, h: str, *, exclude=0, fresh_cleanup=None) -> bool:
    """Independent original physical/control obligations; never provider release."""
    if recovery.control_blocked(db, h, exclude):
        return True
    invalidated = []
    for record in db.execute(
            "SELECT * FROM executions WHERE hash=? AND protocol=3 AND (cleanup_invalidated=1 OR (execution_seq<>? AND "
            "(scope_registration IS NOT NULL OR native_intent IS NOT NULL OR "
            "(launch_reservation IS NOT NULL AND launch_permission IS NOT 'cancelled'))))",
            (h, exclude),
    ):
        row = dict(record)
        if row["cleanup_invalidated"]:
            invalidated.append(row)
        elif not _cleanup_valid(row):
            return True
    # Finish the SELECT before callbacks retain newly observed resource facts on
    # these rows. Re-read the safety predicate after those writes as well.
    return any(not _ownership_clear(row, fresh_cleanup) for row in invalidated)


def claim(db, h: str, provider: str, legs, *, fresh_cleanup=None):
    if blocked(db, h, provider, fresh_cleanup=fresh_cleanup):
        return None
    return legs.claim(db, h, provider, protocol=3)


def _finish_pointer(db, row):
    deleted = db.execute(
        "DELETE FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
        (row["hash"], row["execution_seq"], row["claim_id"]),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("native execution does not own current inflight")


def reconcile(db, legs, *, fresh_cleanup=None):
    """Only unreserved native claims have structural proof of no launch."""
    restored = []
    rows = list(db.execute(
        "SELECT e.* FROM executions e JOIN inflight i ON i.execution_seq=e.execution_seq "
        "WHERE e.protocol=3 ORDER BY e.execution_seq"
    ))
    for record in rows:
        value = {key: record[key] for key in legs.CONTEXT}
        if reconcile_execution(db, value, legs, fresh_cleanup=fresh_cleanup):
            restored.append(record["hash"])
    return restored


def reconcile_execution(db, value, legs, *, fresh_cleanup=None):
    """The existing reducer for one exact row; never reconstruct its pointer."""
    row = execution(db, value, legs)
    if row["completion_kind"] == "unbound":
        return False
    if row["terminal"] is not None or row["first_stop"] is not None or row["cleanup_proof"] is not None:
        apply(db, value, legs, fresh_cleanup=fresh_cleanup)
        return False
    if row["typed_applied_state"] is not None:
        current = db.execute("SELECT 1 FROM inflight WHERE hash=? AND execution_seq=? AND claim_id=?",
                             (row["hash"], row["execution_seq"], row["claim_id"])).fetchone()
        if current is not None and _ownership_clear(row, fresh_cleanup):
            _finish_pointer(db, row)
        return False
    if row["launch_reservation"] is not None and row["launch_permission"] != "cancelled":
        return False  # Reservation/intent recovery requires its bound facts.
    _owns(db, row)
    safety = row["safety_kind"] or ("uncertainty" if row["scope_registration"] else None)
    selected = None
    if row["nonce_origin"] == "selected-recovery":
        from _genesis_index_marker_retry import settle_selected

        selected = settle_selected(db, row, sys.modules[__name__],
                                   completion="unstarted", safety=safety, increment=False)
    if selected is None:
        state, disposition, charged = legs._restore(db, row, False)
    else:
        state, disposition, charged, safety = selected
    if not recovery.control_pending(row) and (row["scope_registration"] is None or _ownership_clear(row, fresh_cleanup)):
        _finish_pointer(db, row)
    before = execution(db, value, legs)  # Fresh-cleanup callbacks may already have retained safety.
    db.execute(
        "UPDATE executions SET completion_kind='unstarted',typed_applied_state=?,"
        "disposition=?,typed_charged_attempts=?,safety_kind=coalesce(safety_kind,?) WHERE execution_seq=?",
        (state, disposition, charged, safety, row["execution_seq"]),
    )
    recovery.note_transition(db, before, execution(db, value, legs))
    return True


COMMANDS = {"reserve-launch", "register-scope", "begin-native", "cancel-launch",
            "remember-native-event", "apply-native", "execution-status"}


def add_commands(sub):
    for name in sorted(COMMANDS):
        parser = sub.add_parser(name)
        parser.add_argument("--context", required=True)
        if name in ("reserve-launch", "register-scope", "begin-native", "remember-native-event"):
            parser.add_argument("--data", required=True)
        if name == "remember-native-event":
            parser.add_argument("--kind", choices=("terminal", "stop", "cleanup", "cleanup-disproof", "control-closure"), required=True)


def dispatch(args, api, proof):
    value = json.loads(args.context, object_pairs_hook=proof._unique)
    operation = api[args.cmd.replace("-", "_")]
    if args.cmd == "remember-native-event":
        result = operation(value, args.kind, json.loads(args.data, object_pairs_hook=proof._unique))
    elif hasattr(args, "data"):
        result = operation(value, json.loads(args.data, object_pairs_hook=proof._unique))
    else:
        result = operation(value)
    print(json.dumps(str(result) if args.cmd == "remember-native-event" else result, ensure_ascii=True))
    success = {"reserve-launch": "reserved", "register-scope": "registered",
               "begin-native": "started", "cancel-launch": "cancelled"}
    if args.cmd in success:
        return 0 if result == success[args.cmd] else 1
    return 1 if result is None else 0
