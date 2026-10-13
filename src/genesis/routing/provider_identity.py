"""Non-secret request-health identity and narrowly evidenced retirement cause."""
from __future__ import annotations

import hashlib
import json
import re

from genesis.routing.types import CallResult, ErrorCategory, ProviderConfig

_HEALTH_ID = re.compile(r"[0-9a-f]{64}")


def provider_identity(cfg: ProviderConfig) -> str:
    """Exclude aliases, keys and raw URLs from persisted identity metadata."""
    identity = [cfg.provider_type, cfg.model_id, cfg.base_url]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def retirement_failure(result: CallResult, model_id: str | None = None) -> bool:
    """Only an unambiguous affirmative 410 message establishes retirement.

    Accept whole statements, optionally naming the requested model. Unknown
    formats, negation, account retirement and appended qualifications preserve
    the hold. Only the boolean is persisted; never store raw vendor errors.
    """
    if not result.reached_provider or result.status_code != 410:
        return False
    subjects = ["model", "the model"]
    if model_id:
        subjects.extend(f"model {name}" for name in (model_id, f"'{model_id}'", f'"{model_id}"'))
    statements = {
        f"{subject} {verb} {state}".replace("  ", " ")
        for subject in subjects
        for verb in ("", "is", "was", "has been")
        for state in ("retired", "decommissioned", "at end of life")
    }
    error = result.error or ""
    # Observed str(litellm.APIError): the SDK prepends this exact type name.
    # Remove one known wrapper only; arbitrary prefixes/qualifications remain
    # unproven and cannot release a hold.
    prefix = "litellm.APIError: "
    if error.startswith(prefix):
        error = error[len(prefix):]
    message = " ".join(error.casefold().split()).rstrip(".!")
    return message in {statement.casefold() for statement in statements}


def valid_identity(value) -> bool:
    return isinstance(value, str) and _HEALTH_ID.fullmatch(value) is not None


def restored_provenance(info: dict, live: bool, *, pending: bool = False) -> tuple[str | None, str | None]:
    """Unknown or malformed provenance preserves the hold without granting reset."""
    cause, identity = info.get("failure_cause"), info.get("failure_identity")
    if not live and not pending:
        return None, None
    cause = cause if cause in ("retirement", "other", "operator") else None
    if cause == "retirement" and (
        not valid_identity(identity) or info.get("identity") != identity
        or info.get("last_failure_category") != ErrorCategory.TRANSIENT.value
        or info.get("opened_by_call") is not (not pending)
    ):
        cause = None  # internally inconsistent evidence grants no reset
    return cause, identity if valid_identity(identity) else None
