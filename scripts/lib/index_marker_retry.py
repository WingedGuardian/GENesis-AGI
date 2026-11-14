"""Explicit current-grant transactions on the admitted queue; no opener or timer."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import _genesis_index_marker_recovery as recovery

MAX_GRANT_LIFETIME_NS = 14_400 * 10**9
MAX_REFERENCE_BYTES = 4096

FIELDS = (
    "version",
    "status",
    "nonce",
    "hash",
    "repo_path",
    "provider",
    "issued_boot_id",
    "issued_wall_ns",
    "issued_monotonic_ns",
    "not_after_wall_ns",
    "not_after_monotonic_ns",
    "hazard_revision",
    "frontier",
    "evidence",
    "request",
    "run_mode",
    "source",
    "policy_sha256",
    "evidence_reference",
    "trial",
)
REQUEST_FIELDS = (
    "origin",
    "snapshot_sha256",
    "mode",
    "requested_at",
    "attempts",
    "legacy_unqualified",
    "legacy_shared_attempts",
)


class InvalidGrant(ValueError):
    """Declared stored-content refusal; never a database or IO failure."""


def validate(value, native):
    native._object(value, FIELDS, "retry grant")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["status"] not in ("issued", "spent", "revoked")
        or value["provider"] not in ("cbm", "gitnexus")
        or value["run_mode"] not in ("fast", "moderate", "full")
    ):
        raise ValueError("invalid retry grant domain")
    for field, size in (("nonce", 32), ("hash", 16), ("policy_sha256", 64)):
        native._hex(value[field], size, "grant " + field)
    native._text(value["repo_path"], "grant repository")
    native._text(value["evidence_reference"], "grant run-card reference")
    if len(value["evidence_reference"].encode("utf-8")) > MAX_REFERENCE_BYTES:
        raise ValueError("grant run-card reference exceeds its UTF8 byte limit")
    native._text(value["issued_boot_id"], "grant boot")
    native._boot(value["issued_boot_id"])
    for field in (
        "issued_wall_ns",
        "issued_monotonic_ns",
        "not_after_wall_ns",
        "not_after_monotonic_ns",
    ):
        native._integer(value[field], field, positive=True)
    for field in ("hazard_revision", "frontier"):
        native._integer(value[field], field)
    if (
        value["not_after_wall_ns"] <= value["issued_wall_ns"]
        or value["not_after_wall_ns"] - value["issued_wall_ns"] > MAX_GRANT_LIFETIME_NS
        or value["not_after_monotonic_ns"] - value["issued_monotonic_ns"]
        != value["not_after_wall_ns"] - value["issued_wall_ns"]
    ):
        raise ValueError("invalid retry deadline derivation")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ValueError("invalid retry evidence set")
    for item in evidence:
        native._hex(item, 64, "grant evidence identity")
    if evidence != sorted(set(evidence)):
        raise ValueError("noncanonical retry evidence set")
    request = native._object(value["request"], REQUEST_FIELDS, "selected request")
    if (
        request["origin"] not in ("pending", "failed")
        or request["mode"] not in ("fast", "moderate", "full")
        or type(request["legacy_unqualified"]) is not int
        or request["legacy_unqualified"] not in (0, 1)
    ):
        raise ValueError("invalid selected request domain")
    native._hex(request["snapshot_sha256"], 64, "request snapshot")
    native._integer(request["attempts"], "retained debt")
    native._receipt_time(request["requested_at"])
    native._text(request["legacy_shared_attempts"], "selected shared debt")
    try:
        shared = json.loads(request["legacy_shared_attempts"])
    except (ValueError, RecursionError) as error:
        raise ValueError("invalid selected shared debt JSON") from error
    if (
        not isinstance(shared, list)
        or any(type(debt) is not int or not 0 <= debt <= native.INTEGER_MAX for debt in shared)
        or shared != sorted(set(shared))
    ):
        raise ValueError("invalid selected shared debt")
    native.source_identity(value["source"])
    if value["status"] == "spent":
        trial = native._object(
            value["trial"],
            ("hash", "repo_path", "provider", "protocol", "execution_seq", "claim_id"),
            "selected trial",
        )
        native._integer(trial["execution_seq"], "trial sequence", positive=True)
        if (
            type(trial["protocol"]) is not int
            or trial["protocol"] != 3
            or trial["claim_id"] != value["nonce"]
            or any(trial[key] != value[key] for key in ("hash", "repo_path", "provider"))
        ):
            raise ValueError("grant names another trial")
    elif value["trial"] is not None:
        raise ValueError("unspent grant cannot name a trial")
    return value


def fresh(grant, now, native):
    validate(grant, native)
    native._object(now, ("boot_id", "wall_ns", "monotonic_ns"), "grant clock")
    native._boot(now["boot_id"])
    native._integer(now["wall_ns"], "wall clock", positive=True)
    native._integer(now["monotonic_ns"], "monotonic clock", positive=True)
    if (
        grant["status"] not in ("issued", "spent")
        or now["boot_id"] != grant["issued_boot_id"]
        or not grant["issued_wall_ns"] <= now["wall_ns"] < grant["not_after_wall_ns"]
        or not grant["issued_monotonic_ns"] <= now["monotonic_ns"] < grant["not_after_monotonic_ns"]
    ):
        raise ValueError("retry authorization expired or its clock/boot changed")


def _raw_grant(db, h, provider):
    return db.execute(
        "SELECT repo_path,typeof(grant) AS storage_type,CAST(grant AS BLOB) AS raw "
        "FROM provider_state WHERE hash=? AND provider=?", (h, provider),
    ).fetchone()


def _load_row(row, h, provider, native):
    if row is None or row["raw"] is None:
        return None
    value = _decode(row["raw"], native)
    if (value["hash"], value["provider"], value["repo_path"]) != (h, provider, row["repo_path"]):
        raise InvalidGrant("retry grant belongs to another provider")
    return value


def load(db, h, provider, native):
    return _load_row(_raw_grant(db, h, provider), h, provider, native)


def _raw_digest(row):
    return hashlib.sha256(row["storage_type"].encode("ascii") + b"\0" + row["raw"]).hexdigest()


def observe_grant(db, h, provider, native):
    row = _raw_grant(db, h, provider)
    try:
        return _load_row(row, h, provider, native)
    except InvalidGrant:
        return dict(state="invalid-grant", storage_type=row["storage_type"],
                    size_bytes=len(row["raw"]), sha256=_raw_digest(row), launch_authority=False)


def _decode(raw, native):
    # The alias is registered by the existing facade/finite Source assembly;
    # this never opens another source file or a database connection.
    from _genesis_code_intel_execution import _unique

    try:
        if not isinstance(raw, (bytes, str)):
            raise ValueError("invalid grant storage")
        return validate(json.loads(raw, object_pairs_hook=_unique), native)
    except (ValueError, RecursionError) as error:
        raise InvalidGrant("invalid retry grant content") from error


def _nonce_used(db, nonce):
    from _genesis_code_intel_execution import _unique

    for row in db.execute("SELECT CAST(grant AS BLOB) FROM provider_state WHERE grant IS NOT NULL"):
        try:
            value = json.loads(row[0], object_pairs_hook=_unique)
        except (ValueError, RecursionError):
            continue  # Cannot authorize this record through the strict loader.
        if isinstance(value, dict) and value.get("nonce") == nonce:
            return True
    return False


def _row_digest(row, native):
    # Preserve original bytes in their existing row. The grant needs identity,
    # not another copy of raw legacy payload or native secrets.
    return native.digest(
        {
            key: {"bytes_sha256": hashlib.sha256(value).hexdigest()}
            if isinstance(value, bytes)
            else value
            for key, value in dict(row).items()
        }
    )


def selected_request(db, h, provider, origin, native, legs):
    if origin not in ("pending", "failed"):
        raise ValueError("retry requires an explicit pending or failed origin")
    queries = dict(
        pending="SELECT * FROM pending WHERE hash=? AND provider=?",
        failed="SELECT * FROM failed WHERE hash=? AND provider=?",
    )
    row = db.execute(queries[origin], (h, provider)).fetchone()
    if row is None or origin == "failed" and row["legacy_diagnostic"]:
        raise ValueError("selected current work is absent")
    if (
        row["mode"] not in legs.MODES
        or type(row["attempts"]) is not int
        or not 0 <= row["attempts"] < native.INTEGER_MAX
    ):
        raise ValueError("selected work has invalid or exhausted debt")
    legs.shared_debts(row["legacy_shared_attempts"])
    request = dict(
        origin=origin,
        snapshot_sha256=_row_digest(row, native),
        **{key: row[key] for key in REQUEST_FIELDS[2:]},
    )
    return dict(row), request


def view(db, h, provider, native, legs, residual, *, exclude=0):
    state = recovery.provider_snapshot(db, h, provider)
    revision, accepted = (state["hazard_revision"], state["accepted_sequence"]) if state else (0, 0)
    native._integer(revision, "hazard revision")
    native._integer(accepted, "accepted sequence")
    if state is not None and not accepted and any(state[name] is not None for name in (
        "accepted_terminal_sha256", "accepted_control_sha256",
        "accepted_source_identity", "accepted_evidence_reference",
    )):
        raise RuntimeError("partial provider safety acceptance")
    invalid_acceptance = bool(accepted) and not recovery._accepted_clean(db, state, native)
    if invalid_acceptance:
        accepted = 0
    modes, identities, frontier = [], set(), 0
    if invalid_acceptance:
        # Grant metadata is not a hazard and must never stale itself on storage.
        accepted_record = {
            key: state[key] for key in recovery.PROVIDER_FIELDS if key.startswith("accepted_")
        }
        identities.add(native.digest({"invalid_acceptance": _row_digest(accepted_record, native)}))
        if provider == "cbm":
            modes.append("full")
    for record in db.execute(
        "SELECT * FROM executions WHERE hash=? AND execution_seq<>?", (h, exclude)
    ):
        row = dict(record)
        if row["completion_kind"] == "unbound":
            raise RuntimeError("unbound possible writer prevents retry")
        if not (
            row["provider"] == provider
            or row["provider"] is None
            and row["original_tools"] in (provider, "both")
        ):
            continue
        frontier = max(frontier, row["execution_seq"])
        if row["execution_seq"] <= accepted or not (
            row["safety_kind"] or row["first_typed_conflict"] or row["conflict_reason"]
        ):
            continue
        identities.add(
            native.digest(
                dict(
                    sequence=row["execution_seq"],
                    safety=recovery.safety_projection(row),
                    scalar_conflict=row["conflict_reason"],
                )
            )
        )
        mode = (
            json.loads(row["launch_reservation"])["run_mode"]
            if row["launch_reservation"]
            else row["mode"]
        )
        if mode not in legs.MODES:
            raise ValueError("unresolved incident has no supported envelope")
        modes.append(
            "full"
            if provider == "cbm"
            and (
                row["legacy_unqualified"]
                or row["completion_kind"] == "unknown"
                or row["safety_kind"] == "uncertainty"
            )
            else mode
        )
    for query in (
        "SELECT mode FROM pending WHERE hash=? AND provider=?",
        "SELECT mode FROM failed WHERE hash=? AND provider=?",
    ):
        row = db.execute(query, (h, provider)).fetchone()
        if row:
            if row[0] not in legs.MODES:
                raise ValueError("current request envelope is invalid")
            modes.append(row[0])
    identities.update(
        identity for owner, tool, _kind, identity in residual if (owner, tool) == (h, provider)
    )
    return dict(
        hazard_revision=revision,
        frontier=frontier,
        evidence=sorted(identities),
        required_mode=max(modes or ["fast"], key=legs.MODES.index),
    )


def _replaceable(db, prior, current, native, legs, prior_source):
    if prior is None or prior["status"] != "spent":
        return
    row = native.execution(db, prior["trial"], legs)
    if row["typed_applied_state"] is None or recovery.control_pending(row):
        raise RuntimeError("unresolved selected trial must remain current")
    if (
        row["completion_kind"] != "success"
        or row["safety_kind"]
        or row["first_typed_conflict"]
        or row["terminal"] is None
        or row["control_closure"] is None
        or json.loads(row["control_closure"])["data"]["state"] != "clean"
    ):
        return
    state = db.execute(
        "SELECT * FROM provider_state WHERE hash=? AND provider=?",
        (prior["hash"], prior["provider"]),
    ).fetchone()
    if state["accepted_sequence"] == row["execution_seq"] and recovery._accepted_clean(
        db, state, native
    ):
        return
    if any(
        prior[key] != current[key] for key in ("hazard_revision", "frontier", "evidence")
    ) or legs.MODES.index(current["required_mode"]) > legs.MODES.index(prior["run_mode"]):
        return
    if prior_source is not None:
        native._object(prior_source, ("nonce", "source"), "prior input observation")
        if (
            prior_source["nonce"] == prior["nonce"]
            and native.source_identity(prior_source["source"]) != prior["source"]
        ):
            return
    raise RuntimeError("clean acceptance-eligible selected success must remain current")


def _clock():
    return dict(boot_id=recovery.owner_identity()["boot_id"],
                wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())


def issue(db, data, native, legs, *, now=None, finalize, fresh_cleanup=None, prior_source=None):
    if not db.in_transaction:
        raise RuntimeError("retry issue requires an admitted transaction")
    native._object(
        data,
        (
            "hash",
            "repo_path",
            "provider",
            "origin",
            "run_mode",
            "nonce",
            "source",
            "policy_sha256",
            "evidence_reference",
            "not_after_wall_ns",
        ),
        "retry request",
    )
    native._integer(data["not_after_wall_ns"], "explicit not-after", positive=True)
    prior = load(db, data["hash"], data["provider"], native)
    if db.execute(
        "SELECT 1 FROM inflight WHERE hash=?", (data["hash"],)
    ).fetchone() or native.physical_blocked(db, data["hash"], fresh_cleanup=fresh_cleanup):
        raise RuntimeError("current original ownership prevents retry issue")
    residual = finalize()  # Last callback: select actual work after all evidence reducers.
    row, request = selected_request(
        db, data["hash"], data["provider"], data["origin"], native, legs
    )
    if row["repo_path"] != data["repo_path"]:
        raise ValueError("selected request belongs to another repository")
    if db.execute("SELECT 1 FROM executions WHERE claim_id=?", (data["nonce"],)).fetchone():
        raise ValueError("retry nonce has already identified an execution")
    if _nonce_used(db, data["nonce"]):
        raise ValueError("retry nonce is already current")
    # Production observes clocks after cleanup/drain, never a caller's old
    # preflight time. Explicit dictionaries are the Python fixture seam only.
    if now is None:
        now = _clock()
    native._object(now, ("boot_id", "wall_ns", "monotonic_ns"), "grant clock")
    for key in ("wall_ns", "monotonic_ns"):
        native._integer(now[key], key, positive=True)
    delta = data["not_after_wall_ns"] - now["wall_ns"]
    if delta <= 0 or delta > native.INTEGER_MAX - now["monotonic_ns"]:
        raise ValueError("invalid or overflowing explicit retry deadline")
    current = view(db, data["hash"], data["provider"], native, legs, residual)
    prior_view = (
        view(
            db,
            data["hash"],
            data["provider"],
            native,
            legs,
            residual,
            exclude=prior["trial"]["execution_seq"],
        )
        if prior and prior["status"] == "spent"
        else current
    )
    _replaceable(db, prior, prior_view, native, legs, prior_source)
    if data["run_mode"] not in legs.MODES or legs.MODES.index(data["run_mode"]) < legs.MODES.index(
        current["required_mode"]
    ):
        raise ValueError("selected retry cannot cover the current required envelope")
    value = dict(
        version=1,
        status="issued",
        nonce=data["nonce"],
        hash=data["hash"],
        repo_path=data["repo_path"],
        provider=data["provider"],
        issued_boot_id=now["boot_id"],
        issued_wall_ns=now["wall_ns"],
        issued_monotonic_ns=now["monotonic_ns"],
        not_after_wall_ns=data["not_after_wall_ns"],
        not_after_monotonic_ns=now["monotonic_ns"] + delta,
        request=request,
        run_mode=data["run_mode"],
        source=data["source"],
        policy_sha256=data["policy_sha256"],
        evidence_reference=data["evidence_reference"],
        trial=None,
        **{key: current[key] for key in ("hazard_revision", "frontier", "evidence")},
    )
    fresh(value, now, native)
    legs._identity(db, data["hash"], data["repo_path"])
    db.execute(
        "INSERT INTO provider_state(hash,repo_path,provider,grant) VALUES(?,?,?,?) "
        "ON CONFLICT(hash,provider) DO UPDATE SET grant=excluded.grant",
        (data["hash"], data["repo_path"], data["provider"], native.encode(value)),
    )
    return value


def consume(db, data, native, legs, *, finalize, bind_runtime, fresh_cleanup=None, now=None):
    """Stage one selected actual-owner reservation and validated runtime atomically.

    Caller holds the admitted transaction and lifecycle/coordination leases.
    Errors must escape that transaction; only its first committed return may
    reach the same actual owner's execution core. Duplicate calls return None.
    """
    if not db.in_transaction or not callable(bind_runtime):
        raise RuntimeError("selected consumption requires its admitted runtime validator")
    native._object(data, ("hash", "provider", "nonce", "reservation", "policy_sha256"),
                   "selected consumption")
    for key, size in (("hash", 16), ("nonce", 32), ("policy_sha256", 64)):
        native._hex(data[key], size, key)
    grant = load(db, data["hash"], data["provider"], native)
    if grant is None or grant["nonce"] != data["nonce"]:
        raise ValueError("selected consumption does not name the current grant")
    if grant["status"] == "spent":
        return None  # No reconstructed runtime or first reservation ACK.
    fresh(grant, _clock() if now is None else now, native)
    row, selected = selected_request(db, data["hash"], data["provider"],
                                     grant["request"]["origin"], native, legs)
    if selected != grant["request"] or data["policy_sha256"] != grant["policy_sha256"]:
        raise ValueError("selected request or policy changed")
    claimed = legs._insert_claim(db, data["hash"], data["provider"], row,
                                 protocol=3, nonce=data["nonce"], origin="selected-recovery")
    context = claimed["context"]
    reserved = native.reservation(data["reservation"], context)
    if (reserved["owner"] != recovery.owner_identity() or reserved["source"] != grant["source"]
            or reserved["run_mode"] != grant["run_mode"]):
        raise ValueError("selected reservation does not bind actual owner and granted inputs")
    spent = dict(grant, status="spent", trial=context)
    validate(spent, native)
    db.execute("UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
               (native.encode(spent), data["hash"], data["provider"]))
    if native.reserve(db, context, reserved, legs, fresh_cleanup=fresh_cleanup) != "reserved":
        raise RuntimeError("selected reservation remains held")
    bound = bind_runtime(context, reserved)
    residual = finalize()  # All physical/runtime callbacks precede the last drain.
    if reserved["owner"] != recovery.owner_identity():
        raise ValueError("original selected owner changed before publication")
    fresh(spent, _clock() if now is None else now, native)
    current = view(db, data["hash"], data["provider"], native, legs, residual,
                   exclude=context["execution_seq"])
    if (any(current[key] != grant[key] for key in ("hazard_revision", "frontier", "evidence"))
            or legs.MODES.index(current["required_mode"]) > legs.MODES.index(grant["run_mode"])):
        raise ValueError("selected safety evidence or required envelope changed")
    _, selected = selected_request(db, data["hash"], data["provider"],
                                   grant["request"]["origin"], native, legs)
    retained = native.execution(db, context, legs)
    native._owns(db, retained)
    if (selected != grant["request"] or load(db, data["hash"], data["provider"], native) != spent
            or retained["launch_permission"] != "reserved"
            or bytes(retained["launch_reservation"]) != native.encode(reserved)
            or any(retained[key] is not None for key in (
                "scope_registration", "native_intent", "terminal", "first_stop", "cleanup_proof",
                "control_closure", "safety_kind", "first_typed_conflict"))
            or retained["cleanup_invalidated"]):
        raise ValueError("selected trial changed before publication")
    if grant["request"]["origin"] == "pending":
        db.execute("DELETE FROM pending WHERE hash=? AND provider=?",
                   (data["hash"], data["provider"]))
    return claimed, bound


def trial_binding(db, context, legs, *, reserved=None, now=None, phase="prepared"):
    """Validate retained selected authority at a native launch transition.

    The facade additionally checks the exact residual set after its last drain;
    this reader cannot stand in for that publication-boundary decision.
    """
    import _genesis_index_marker_execution as native

    if phase not in ("prepared", "started"):
        raise ValueError("unknown selected transition phase")
    native._object(context, legs.CONTEXT, "selected trial context")
    native._integer(context["execution_seq"], "selected sequence", positive=True)
    if type(context["protocol"]) is not int or context["protocol"] != 3:
        raise ValueError("selected trial requires protocol3")
    row = native.execution(db, context, legs)
    grant = load(db, context["hash"], context["provider"], native)
    if (grant is None or grant["status"] != "spent" or grant["trial"] != context
            or row["nonce_origin"] != "selected-recovery"):
        raise ValueError("native transition does not own a consumed selected grant")
    fresh(grant, _clock() if now is None else now, native)
    native._owns(db, row)
    if reserved is None:
        if row["launch_reservation"] is None:
            raise ValueError("selected trial has no reservation")
        reserved = json.loads(row["launch_reservation"])
    native.reservation(reserved, context)
    if phase == "started":
        if (row["native_intent"] is None or row["launch_permission"] != "spent"
                or json.loads(row["native_intent"])["source"] != grant["source"]):
            raise ValueError("selected intent was not durably staged")
    elif (row["native_intent"] is not None or row["launch_permission"] not in (None, "reserved")
          or row["launch_permission"] is None and row["launch_reservation"] is not None):
        raise ValueError("selected native permission is already closed")
    if (reserved["source"] != grant["source"] or reserved["run_mode"] != grant["run_mode"]
            or row["mode"] != grant["request"]["mode"]
            or row["attempts"] != grant["request"]["attempts"]
            or row["requested_at"] != grant["request"]["requested_at"]
            or row["legacy_unqualified"] != grant["request"]["legacy_unqualified"]
            or row["legacy_shared_attempts"] != grant["request"]["legacy_shared_attempts"]
            or any(row[key] is not None for key in (
                "terminal", "first_stop", "cleanup_proof", "control_closure",
                "safety_kind", "first_typed_conflict", "conflict_reason"))
            or row["cleanup_invalidated"]):
        raise ValueError("selected trial no longer binds its granted inputs and debt")
    current = view(db, context["hash"], context["provider"], native, legs, (),
                   exclude=context["execution_seq"])
    if (any(current[key] != grant[key] for key in ("hazard_revision", "frontier"))
            or not set(current["evidence"]).issubset(grant["evidence"])
            or legs.MODES.index(current["required_mode"]) > legs.MODES.index(grant["run_mode"])):
        raise ValueError("selected grant no longer covers current safety and workload")
    return grant


def trial_decision(db, context, legs, residual, *, reserved=None, now=None, phase="prepared"):
    """Exact provider evidence comparison on the admitted final-drain snapshot."""
    import _genesis_index_marker_execution as native

    grant = trial_binding(db, context, legs, reserved=reserved, now=now, phase=phase)
    current = view(db, context["hash"], context["provider"], native, legs, residual,
                   exclude=context["execution_seq"])
    if any(current[key] != grant[key] for key in ("hazard_revision", "frontier", "evidence")):
        raise ValueError("selected grant residual evidence changed")
    return grant


def settle_selected(db, row, native, *, completion, safety, increment):
    """Settle selected history without inventing its lost request origin.

    The native reducer owns the transaction, safety revision and physical
    lifetime. Expired launch authority can still owe truthful settlement.
    """
    if row["nonce_origin"] != "selected-recovery":
        return None
    context = {key: row[key] for key in (
        "hash", "repo_path", "provider", "protocol", "execution_seq", "claim_id")}
    try:
        grant = load(db, row["hash"], row["provider"], native)
        reserved = json.loads(row["launch_reservation"]) if row["launch_reservation"] else None
        if (grant is None or grant["status"] != "spent" or grant["trial"] != context
                or reserved is None
                or native.reservation(reserved, context)["source"] != grant["source"]
                or reserved["run_mode"] != grant["run_mode"]
                or any(row[key] != grant["request"][key] for key in (
                    "mode", "attempts", "requested_at", "legacy_unqualified", "legacy_shared_attempts"))):
            raise ValueError("selected settlement lost its frozen request binding")
    except ValueError:
        # Neither the current failed row nor pending presence identifies the
        # original selected request. Keep both, record uncertainty, never reset
        # its known debt or release its independent physical/control holds.
        return "failed", "selected-grant-unavailable", row["attempts"] + int(increment), safety or "uncertainty"
    if grant["request"]["origin"] == "pending":
        return None  # Existing ordinary restoration remains authoritative.
    if completion == "success" and safety is None:
        failed = db.execute("SELECT * FROM failed WHERE hash=? AND provider=?",
                            (row["hash"], row["provider"])).fetchone()
        if failed is not None and _row_digest(failed, native) == grant["request"]["snapshot_sha256"]:
            deleted = db.execute("DELETE FROM failed WHERE hash=? AND provider=?",
                                 (row["hash"], row["provider"])).rowcount
            if deleted != 1:
                raise RuntimeError("selected failure changed during settlement")
        return "consumed", "consumed", row["attempts"], safety
    return "failed", "selected-failure-retained", row["attempts"] + int(increment), safety


def launch_transition(api, context, data, operation):
    """Use the existing opener/barrier for exact selected native acknowledgements."""
    native, legs = api["_native"], api["_legs"]
    if operation not in (native.reserve, native.register, native.begin):
        raise ValueError("unknown native launch transition")
    with api["_decision_gate"](native=True) as (db, finalize):
        row = native.execution(db, context, legs)
        selected = row["nonce_origin"] == "selected-recovery"
        if not selected:
            recovery.refuse_unrepresented(db, finalize(), (context["hash"], context["provider"]))
        args = (db, context, data, legs)
        if operation is not native.reserve:
            args += (api["_proof"],)
        state = operation(*args, fresh_cleanup=api["_fresh_gate"](db))
        if selected:
            residual = finalize()  # No later physical/runtime callbacks.
            if state in ("reserved", "registered", "started"):
                trial_decision(db, context, legs, residual,
                               phase="started" if operation is native.begin else "prepared")
    return state  # Duplicate/conflict/resource statuses never become fresh permits.


def revoke(db, h, provider, nonce, native, *, grant_sha256=None):
    if not db.in_transaction:
        raise RuntimeError("retry revocation requires an admitted transaction")
    if (nonce is None) == (grant_sha256 is None):
        raise ValueError("revocation requires exactly one current grant selector")
    if grant_sha256 is not None:
        native._hex(grant_sha256, 64, "invalid-grant digest")
        row = _raw_grant(db, h, provider)
        if row is None or row["raw"] is None:
            return dict(state="absent", launch_authority=False)
        if _raw_digest(row) != grant_sha256:
            raise ValueError("revocation does not name the current invalid grant")
        try:
            _load_row(row, h, provider, native)
        except InvalidGrant as error:
            changed = db.execute("UPDATE provider_state SET grant=NULL WHERE hash=? AND provider=?",
                                 (h, provider)).rowcount
            if changed != 1:
                raise RuntimeError("invalid grant changed during revocation") from error
            return dict(state="revoked-corrupt", sha256=grant_sha256, launch_authority=False)
        raise ValueError("a valid grant requires its exact unused nonce")
    native._hex(nonce, 32, "revocation nonce")
    value = load(db, h, provider, native)
    if value is None or value["nonce"] != nonce:
        raise ValueError("revocation does not name the current grant")
    if value["status"] == "issued":
        value["status"] = "revoked"
        db.execute(
            "UPDATE provider_state SET grant=? WHERE hash=? AND provider=?",
            (native.encode(value), h, provider),
        )
    return value


COMMANDS = {"status-recovery", "request-retry", "revoke-retry", "claim-selected"}


def add_commands(sub, legs):
    for name in sorted(COMMANDS):
        parser = sub.add_parser(name)
        parser.add_argument("--repo", required=True)
        parser.add_argument("--provider", choices=legs.PROVIDERS, required=True)
        if name == "request-retry":
            parser.add_argument("--origin", choices=("pending", "failed"), required=True)
            parser.add_argument("--mode", choices=legs.MODES, required=True)
            parser.add_argument("--not-after-wall-ns", type=int, required=True)
            parser.add_argument("--evidence-reference", required=True)
            parser.add_argument(
                "--managed-config",
                default=str(Path.home() / ".genesis/config/codebase-managed.json"),
            )
        elif name == "revoke-retry":
            selected = parser.add_mutually_exclusive_group(required=True)
            selected.add_argument("--nonce")
            selected.add_argument("--grant-sha256")
        elif name == "claim-selected":
            parser.add_argument("--nonce", required=True)
            parser.add_argument("--managed-config",
                default=str(Path.home() / ".genesis/config/codebase-managed.json"))


def dispatch(args, api, supervisor):
    if args.cmd not in COMMANDS:
        raise ValueError("unknown supervised metadata command")
    repo = api["canonical_repo"](args.repo)
    h = api["marker_hash"](repo)
    if args.cmd == "status-recovery":
        native = api["_native"]

        def observe(db):
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != recovery.SCHEMA_VERSION:
                return dict(
                    state="protocol-incomplete", schema_version=version, grant_available=False
                )
            recovery.validate_schema(db, native)
            return dict(
                state="observed",
                schema_version=version,
                grant=observe_grant(db, h, args.provider, native),
                recorded_provider_hold=recovery.provider_blocked(db, h, args.provider, native),
                recorded_physical_or_control_hold=native.physical_blocked(db, h),
            )

        result = api["_legacy"].import_status(
            api["database_path"](),
            limit=0,
            schema_check=lambda db: recovery.validate_schema(db, native),
            observe=observe,
        )
        result.update(
            launch_authority=False,
            evidence_pending=any(api["marker_dir"]().glob(".execution-spool-*.spool")),
        )
    else:
        if supervisor is None:
            raise RuntimeError("captured supervised recovery is required")
        if args.cmd == "request-retry":
            result = supervisor.request_retry(
                dict(
                    repo_path=repo,
                    provider=args.provider,
                    origin=args.origin,
                    run_mode=args.mode,
                    not_after_wall_ns=args.not_after_wall_ns,
                    evidence_reference=args.evidence_reference,
                    managed_config=args.managed_config,
                )
            )
        elif args.cmd == "claim-selected":
            result = supervisor.claim_selected(dict(repo_path=repo, provider=args.provider,
                nonce=args.nonce, managed_config=args.managed_config))
        else:
            result = supervisor.revoke_retry(repo, args.provider, args.nonce,
                                             grant_sha256=args.grant_sha256)
    print(json.dumps(result, ensure_ascii=True))
    return 0
