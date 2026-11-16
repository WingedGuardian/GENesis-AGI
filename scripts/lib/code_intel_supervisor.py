"""Captured explicit recovery operations using the existing queue and owner."""

from __future__ import annotations

import json
import os
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

    def _current_grant(self, h, provider):
        queue = self.modules["queue"]
        return self._observe_schema5(lambda db: queue._retry.load(db, h, provider, queue._native))

    def request_retry(self, data):
        self._guard()
        snapshot, modules = self.snapshot, self.modules
        queue, admission = modules["queue"], modules["owner_admission"]
        queue._native._object(
            data,
            (
                "repo_path",
                "provider",
                "origin",
                "run_mode",
                "not_after_wall_ns",
                "evidence_reference",
                "managed_config",
            ),
            "supervised retry request",
        )
        repo = queue.canonical_repo(data["repo_path"])
        h = queue.marker_hash(repo)
        with admission.repository_lock_for(modules, repo, h):
            prior = self._current_grant(
                h, data["provider"]
            )  # Never implicitly converts an older queue.
            manager = modules["scope"].ScopeManager(
                modules["unit"].bus_call,
                modules["unit"].typed_property,
                deadline=time.monotonic()
                + admission.wall_seconds(modules, data["run_mode"], os.environ),
            )
            policy = admission.policy(
                modules, data["provider"], data["run_mode"], manager, os.environ
            )
            manager.require_no_batch(modules["unit"].cgroup_empty)
            nonce = queue.secrets.token_hex(16)
            unit = queue._native.expected_unit(
                dict(hash=h, provider=data["provider"], claim_id=nonce)
            )

            def capacity(control):
                modules["admission"].assess_headroom(
                    *modules["unit"].resolve_control_group(control),
                    8 * 1024**3,
                    policy["sibling_reserve_bytes"],
                    cache_reserve=policy["cache_reserve_bytes"],
                )

            manager.require_slice_capacity(policy["slice"], capacity)
            with admission.prepared_inputs(
                snapshot,
                modules,
                repo,
                data["provider"],
                data["run_mode"],
                data["managed_config"],
                policy,
                unit=unit,
                recovery=True,
            ) as inputs:
                if data["provider"] == "cbm":
                    config = modules["managed"].runtime_config(
                        modules["unit"].config_path(data["managed_config"])
                    )
                    modules["managed"].require_enabled(
                        config
                    )  # Never enables service or removes sentinel.
                prior_source = None
                if prior and prior["status"] == "spent":
                    try:
                        old_policy = admission.policy(
                            modules, prior["provider"], prior["run_mode"], manager, os.environ
                        )
                        old_unit = queue._native.expected_unit(prior["trial"])
                        with admission.prepared_inputs(
                            snapshot, modules, repo, prior["provider"], prior["run_mode"],
                            data["managed_config"], old_policy, unit=old_unit, recovery=True,
                        ) as old:
                            prior_source = dict(nonce=prior["nonce"], source=old["source"])
                    except (OSError, ValueError):
                        # No changed-source proof. The final transaction may
                        # supersede only for its independent conclusive state;
                        # an otherwise eligible clean success still stays current.
                        prior_source = None
                candidate = dict(
                    hash=h,
                    repo_path=repo,
                    provider=data["provider"],
                    origin=data["origin"],
                    run_mode=data["run_mode"],
                    nonce=nonce,
                    source=inputs["source"],
                    policy_sha256=queue._native.digest(policy),
                    evidence_reference=data["evidence_reference"],
                    not_after_wall_ns=data["not_after_wall_ns"],
                )
                with queue._decision_gate(native=True) as (db, finalize):
                    result = queue._retry.issue(
                        db,
                        candidate,
                        queue._native,
                        queue._legs,
                        finalize=finalize,
                        fresh_cleanup=queue._fresh_gate(db),
                        prior_source=prior_source,
                    )
        return result

    def revoke_retry(self, repo, provider, nonce=None, *, grant_sha256=None):
        self._guard()
        queue = self.modules["queue"]
        repo = queue.canonical_repo(repo)
        h = queue.marker_hash(repo)
        with self.modules["owner_admission"].repository_lock_for(self.modules, repo, h):
            if grant_sha256 is None:
                self._current_grant(h, provider)
            else:
                self._observe_schema5(lambda db: None)  # Malformed content is checked after admission.
            with queue._decision_gate(native=True) as (db, finalize):
                finalize()
                result = queue._retry.revoke(db, h, provider, nonce, queue._native,
                                             grant_sha256=grant_sha256)
        return result


    def claim_selected(self, data):
        """Only a fresh committed runtime reaches this same physical owner."""
        self._guard()
        snapshot, modules = self.snapshot, self.modules
        queue, admission = modules["queue"], modules["owner_admission"]
        native = queue._native
        actual_owner = queue._recovery.owner_identity()
        native._object(data, ("repo_path", "provider", "nonce", "managed_config"), "selected claim")
        native._hex(data["nonce"], 32, "selected nonce")
        repo = queue.canonical_repo(data["repo_path"])
        h = queue.marker_hash(repo)
        with admission.repository_lock_for(modules, repo, h):
            grant = self._current_grant(h, data["provider"])
            if grant is None or grant["nonce"] != data["nonce"] or grant["repo_path"] != repo:
                raise ValueError("selected claim does not name the current grant")
            if grant["status"] == "spent":
                return dict(state="already_claimed", trial=grant["trial"], launch_authority=False)
            manager = modules["scope"].ScopeManager(
                modules["unit"].bus_call, modules["unit"].typed_property,
                deadline=time.monotonic() + admission.wall_seconds(modules, grant["run_mode"], os.environ))
            policy = admission.policy(modules, data["provider"], grant["run_mode"], manager, os.environ)
            if actual_owner != queue._recovery.owner_identity():
                raise ValueError("actual selected owner changed during policy lookup")
            manager.require_no_batch(modules["unit"].cgroup_empty)
            if actual_owner != queue._recovery.owner_identity():
                raise ValueError("actual selected owner changed during batch census")

            def capacity(control):
                modules["admission"].assess_headroom(
                    *modules["unit"].resolve_control_group(control), 8 * 1024**3,
                    policy["sibling_reserve_bytes"], cache_reserve=policy["cache_reserve_bytes"])

            manager.require_slice_capacity(policy["slice"], capacity)
            unit = native.expected_unit(dict(hash=h, provider=data["provider"], claim_id=data["nonce"]))
            with admission.prepared_inputs(snapshot, modules, repo, data["provider"], grant["run_mode"],
                                           data["managed_config"], policy, unit=unit, recovery=True) as inputs:
                if data["provider"] == "cbm":
                    config = modules["managed"].runtime_config(modules["unit"].config_path(data["managed_config"]))
                    modules["managed"].require_enabled(config)
                if actual_owner != queue._recovery.owner_identity():
                    raise ValueError("actual selected owner changed during preparation")
                reserved = dict(unit=unit, run_mode=grant["run_mode"], source=inputs["source"],
                    owner=actual_owner, cap_bytes=8 * 1024**3,
                    **{key:policy[key] for key in ("route", "slice", "wall_seconds")})

                def bind(context, actual):
                    runtime = dict(context=context, reservation=actual, policy=policy,
                        provider_options=inputs["provider_options"], environment_recipe=inputs["environment_recipe"])
                    bound = snapshot.with_runtime(runtime)
                    modules["queued"].runtime_inputs(bound, modules)
                    return bound

                request = dict(hash=h, provider=data["provider"], nonce=data["nonce"],
                               reservation=reserved, policy_sha256=native.digest(policy))
                with queue._decision_gate(native=True) as (db, finalize):
                    result = queue._retry.consume(db, request, native, queue._legs, finalize=finalize,
                        bind_runtime=bind, fresh_cleanup=queue._fresh_gate(db))
                    if result is None:
                        current_trial = queue._retry.load(db, h, data["provider"], native)["trial"]
            if result is None:
                return dict(state="already_claimed", trial=current_trial, launch_authority=False)
            claimed, bound = result
            state = admission.execute_runtime(bound, modules, actual_owner=actual_owner)  # No reserve or repository-lock reacquisition.
            return dict(state=state or "open", trial=claimed["context"], launch_authority=False)

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
