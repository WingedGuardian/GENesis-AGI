#!/usr/bin/env python3
"""One chokepoint for "this action needs the user" — decide, and record the block.

A PreToolUse hook that wants a human decision has two audiences, and only one of
them exists at any moment. In a foreground session a person can answer, so the
right verdict is ``ask``. In a Genesis-dispatched session nobody can, so the same
``ask`` is a silent block — the standing design axiom names this exactly: *an ask
with no human present is a block nobody intended*.

The block itself is correct and deliberate: a background session must not reach
into credentials or any other user-gated surface unattended. What is NOT
acceptable is that it happens QUIETLY. A dispatched session dying against a wall
it can never pass is, by construction, a catch-22 — there is no such thing as a
legitimate instance — so it earns a ``critical`` observation, which reaches the
owner's alerts channel.

**Why a helper rather than a convention.** Every hook that asks would otherwise
have to remember to record the unattended case, and a convention is exactly what
reviewers keep finding one missing instance of. ``decide()`` returns the verdict
AND records in the same call, so a caller cannot take the block without emitting
the signal. (Same reasoning as ``hook_output.py`` owning the stdout budget
instead of asking twelve blocks to check it.)

Two things this module gets from elsewhere rather than re-deriving, both because
an adversarial review caught the hand-rolled versions:

* **The write goes through ``observations.create_sync``**, not a raw ``sqlite3``
  INSERT. Raw writes are blocked by policy, and for good reason here: the CRUD
  path also computes the TTL and resolves ``origin_class``, and its dedupe is an
  atomic ``INSERT … WHERE NOT EXISTS`` — the SELECT-then-INSERT this module
  originally planned is the exact cross-process race that path warns against.
* **The recorded detail is scrubbed** (``secret_scrub.scrub``). The excerpt is a
  command line, it lands in a ``critical`` row that reaches the owner's alerts
  channel, and a command like ``export API_KEY_X=… ; cat secrets.env`` would
  otherwise broadcast the value. A credentials guard that leaks credentials is
  worse than none.

Constraints inherited from the hook environment: sub-50ms budget, so the genesis
import is lazy and happens ONLY on the rare dispatched-deny path (the pattern
``session_observer_hook`` uses behind its throttle). **Recording must never break
the guard**: a hook whose observation write fails still returns its verdict, or a
logging bug becomes a security hole.
"""

from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import session_id as _payload_session_id  # noqa: E402
from secret_scrub import scrub  # noqa: E402

#: Stamped on every Genesis-dispatched (autonomous/headless) session by
#: ``cc/invoker.py``. A user-launched foreground session does not carry it.
#: Same detector ``git_push_guard._is_dispatched`` uses — deliberately not
#: re-derived, because two detectors that disagree is worse than either.
_DISPATCH_ENV = "GENESIS_CC_SESSION"

_SOURCE = "hook.needs_user"
_TYPE = "background_session_blocked_needs_user"
_CATEGORY = "system_health"
_PRIORITY = "critical"


def is_dispatched() -> bool:
    """True in a Genesis-dispatched session — i.e. no human can answer a prompt."""
    return os.environ.get(_DISPATCH_ENV) == "1"


def _record(action: str, detail: str, session: str) -> bool:
    """Write the critical observation via the CRUD sync path. Never raises.

    Every failure is swallowed on purpose: this exists to make a block LOUD, and
    a loud-failure that broke the block would invert the safety property it was
    added to protect. Returns False when nothing was written — including when the
    row was deduped away, which is a success for the owner and a "no new signal"
    for the caller.
    """
    try:
        # Lazy, and only on the deny path: the genesis import is heavy and the
        # foreground path must not pay for it.
        sys.path.insert(
            0,
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "src"),
        )
        from genesis.db.crud.observations import create_sync
        from genesis.env import genesis_db_path

        content = (
            f"A background (Genesis-dispatched) session was BLOCKED because "
            f"'{action}' requires the user, and a dispatched session has nobody to "
            f"ask. The block is correct — background sessions must not take "
            f"user-gated actions unattended — but the session cannot make progress "
            f"past it and will not recover on its own. "
            f"Session: {session}. Detail: {scrub(detail) if detail else '(none)'}"
        )
        # Identity is (action, session): the same wall hit twice in one session is
        # one finding. A different session hitting the same wall IS news again,
        # because it means the dispatch keeps being sent into a dead end.
        digest = hashlib.sha256(f"{_TYPE}|{action}|{session}".encode()).hexdigest()

        db_path = os.environ.get("GENESIS_DB_PATH") or str(genesis_db_path())
        return create_sync(
            db_path,
            source=_SOURCE,
            type=_TYPE,
            content=content,
            priority=_PRIORITY,
            category=_CATEGORY,
            content_hash=digest,
            origin_class="first_party",
        )
    except Exception:  # noqa: BLE001 - see docstring: never break the guard
        return False


def decide(action: str, reason: str, detail: str = "", payload: dict | None = None) -> dict:
    """Verdict for an action that requires the user, plus the record when unattended.

    Args:
        action: short name of the gated action, e.g. "read secrets.env".
                Used in the dedupe identity, so keep it stable per call site.
        reason: why the user is needed — shown to a foreground user, so write it
                for a human deciding in one glance, not for a log.
        detail: optional specifics (the command, the path). SCRUBBED before it is
                recorded; pass the real thing.
        payload: the full CC hook payload. ALWAYS pass it. The session id is the
                other half of the dedupe identity, and it arrives on the payload
                under the current contract — an earlier revision read
                ``CLAUDE_SESSION_ID`` from the environment instead, which CC no
                longer sets, so every session collapsed to ``"unknown"`` and
                findings from DIFFERENT dispatched sessions merged into one row.
                That is the failure mode the dedupe is meant to preserve against
                (repeat dispatches into the same dead end is the signal), and it
                would have been invisible: fewer alerts looks like fewer problems.

    Returns a PreToolUse payload:
      * foreground  -> ``ask``   (a person can answer)
      * dispatched  -> ``deny``  AND a critical observation is recorded first
    """
    if not is_dispatched():
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }

    _record(action, detail, _payload_session_id(payload or {}))
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED — '{action}' requires the user, and this is a "
                f"Genesis-dispatched session with nobody to ask. This is not a "
                f"transient failure: no retry, model change, or rephrasing will "
                f"clear it. Stop attempting it and report the block in your "
                f"handoff so a foreground session can pick it up. It has also "
                f"been raised as a critical observation — but say it in the "
                f"handoff regardless, in case that record did not land."
            ),
        }
    }
