"""Captured explicit recovery operations; no opener, timer or native workload."""

from __future__ import annotations

import json
import time
from pathlib import Path


class Supervisor:
    """Explicit queue operations bound to the actual captured runner source."""

    def __init__(self, snapshot, modules, lock_check):
        self.snapshot, self.modules, self.lock_check = snapshot, modules, lock_check

    def _guard(self):
        snapshot = self.snapshot
        if (
            snapshot.profile != "runner"
            or snapshot.runtime_value is not None
            or not snapshot.current_matches()
        ):
            raise ValueError("captured supervised reconciliation requires current runner source")
        self.lock_check()  # Existing canonical fd8 identity and actual lock.

    def _observe_schema5(self, observe):
        queue = self.modules["queue"]

        def read(db):
            queue._recovery.admission_schema(
                db, queue.SCHEMA_VERSION, queue._native, require_native=True
            )
            return dict(supported_schema5=True, value=observe(db))

        result = queue._legacy.import_status(
            queue.database_path(),
            limit=0,
            schema_check=lambda db: queue._recovery.validate_schema(db, queue._native),
            observe=read,
        )
        if result.get("supported_schema5") is not True:
            raise RuntimeError("explicit schema5 cutover is required")
        return result["value"]  # Advisory only; the admitted transaction rereads it.




    def reconcile_context(self, context):
        self._guard()
        modules = self.modules
        queue = modules["queue"]
        context = queue._native.context(context, queue._legs, queue.marker_hash)
        with modules["owner_admission"].repository_lock(modules, context):
            self._observe_schema5(lambda db: None)
            with queue._decision_gate(native=True) as (db, finalize):
                reconcile_orphan(modules, context, db)
                # Drain callback evidence, but do not interpret the residual set
                # as grant/acceptance authority. This reply acknowledges only
                # the exact row; retained conflicts still fence later admission.
                finalize()
                result = queue._native.status(db, context, queue._legs)
        return result  # Acknowledgement only after the existing durable commit.


def reconcile_orphan(modules, context, db, *, proc=Path("/proc")):
    """Collect a fresh exact-owner witness on the admitted supervisor connection.

    This is an internal captured-supervisor seam, not generic event publication.
    The caller holds canonical coordination locks and the publication barrier.
    """
    queue, scope, proof = modules["queue"], modules["scope"], modules["proof"]
    native, legs = queue._native, queue._legs
    if not db.in_transaction:
        raise RuntimeError("supervised reconciliation requires an admitted transaction")
    context = native.context(context, legs, queue.marker_hash)
    row = native.execution(db, context, legs)

    def fresh(current):
        return queue._fresh_cleanup(db, current, proc=proc)

    if row["typed_applied_state"] is None:
        native._owns(db, row)
    if row["control_closure"] is not None or row["launch_reservation"] is None:
        native.reconcile_execution(db, context, legs, fresh_cleanup=fresh)
        return native.status(db, context, legs)
    reserved = native.reservation(json.loads(row["launch_reservation"]), context)
    if not scope.owner_ended(reserved["owner"], proof.__dict__, proc=proc):
        raise ValueError("original owner still lives")
    cleanup = None
    if row["scope_registration"] is not None:
        registered = json.loads(row["scope_registration"])
        if not scope.owner_ended(registered["facts"]["reporter"], proof.__dict__, proc=proc):
            raise ValueError("original reporter still lives")
        cleanup = scope.cleanup_observation(
            registered, native.digest(registered), proof, owned_wait=False, proc=proc
        )
        if cleanup["state"] not in ("removed", "rebooted"):
            previous = json.loads(row["cleanup_proof"])["data"] if row["cleanup_proof"] else None
            if (
                not native._cleanup_valid(row)
                or previous.get("owned_wait") is not True
                or not fresh(row)
            ):
                raise ValueError("original kernel lifetime is not proved ended")
            # A retained actual original wait plus a fresh qualified observation
            # already supplies physical proof; never relabel the new no-wait body.
            cleanup = None
    elif row["native_intent"] is not None:
        raise ValueError("intent without original registration cannot close structurally")
    observer = queue._recovery.owner_identity()
    if observer["boot_id"] != scope._boot(proc):
        raise ValueError("supervisor observer belongs to another boot")
    data = dict(
        state="orphaned",
        reason="owner_died",
        reservation_sha256=native.digest(reserved),
        observer=observer,
        observed_ns=time.monotonic_ns(),
        foreground=None,
        monitor=None,
        stop=None,
    )
    item = native.event(context, "control-closure", data, time.time(), proof)
    if native.record(db, context, item, legs, proof) != "recorded":
        raise RuntimeError("orphan closure was not newly retained")
    if cleanup is not None:
        item = native.event(context, "cleanup", cleanup, time.time(), proof)
        if native.record(db, context, item, legs, proof) == "retained":
            queue._spool_native_event(item)
    native.reconcile_execution(db, context, legs, fresh_cleanup=fresh)
    return native.status(db, context, legs)
