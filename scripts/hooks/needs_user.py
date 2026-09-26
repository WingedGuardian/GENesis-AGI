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

import contextlib
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import session_id as _payload_session_id  # noqa: E402
from secret_scrub import scrub  # noqa: E402

try:  # noqa: E402
    from hook_ask_policy import ask_suppressed, suppressed_reason
except Exception:  # noqa: BLE001 — version skew must fall back to the PROMPT.
    # The other two imports are bare on purpose: without them the guard cannot
    # judge at all, and its caller degrades to blocking. This one is different —
    # its absence means "this install declared no local ask policy", which is
    # exactly the public default. Falling back to asking is the same verdict a
    # clone with no config file gets, so a half-deployed hook tree costs an extra
    # prompt rather than a silent allow.
    def ask_suppressed(key: str) -> bool:  # type: ignore[misc]
        return False

    def suppressed_reason(key: str, detail: str = "") -> str:  # type: ignore[misc]
        return ""


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
    except Exception as _obs_exc:  # noqa: BLE001 - see docstring: never break the guard
        # Swallow the failure, but not SILENTLY: a swallowed write leaves the
        # operator unable to tell "no new signal" from "the audit row never
        # landed". stderr only — the hook's verdict is unchanged either way.
        with contextlib.suppress(Exception):
            sys.stderr.write(
                f"needs_user: critical observation write failed ({type(_obs_exc).__name__})\n"
            )
        return False


def decide(
    action: str,
    reason: str,
    detail: str = "",
    payload: dict | None = None,
    *,
    ask_key: str | None = None,
) -> dict:
    """Verdict for an action that requires the user, plus the record when unattended.

    Args:
        action: short name of the gated action, e.g. "read secrets.env".
                Used in the dedupe identity, so keep it stable per call site.
        reason: why the user is needed — shown to a foreground user, so write it
                for a human deciding in one glance, not for a log.
        detail: optional specifics (the command, the path). SCRUBBED before it is
                recorded; pass the real thing.
        ask_key: the ``hook_ask_policy`` key naming THIS prompt, if an install is
                allowed to turn it off locally. Omitted (the default) means the
                prompt is not suppressible at all, which is why every existing
                call site keeps its behaviour unchanged. The check sits INSIDE
                this function, after the dispatched branch, for the same reason
                the recording does: a caller cannot honour the policy and forget
                the ordering, and no local config can reach the dispatched deny.
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
      * dispatched              -> ``deny``   AND a critical observation first
      * foreground, ask_key off -> ``allow``  naming the local policy
      * foreground              -> ``ask``    (a person can answer)

    The dispatched branch is checked FIRST and takes no argument from the local
    policy. A background session is denied because nobody can answer, not because
    the prompt is enabled — so turning the prompt off must not turn the block off
    with it, and the ordering here is what makes that structural rather than
    remembered.
    """
    if not is_dispatched():
        if ask_key is not None and ask_suppressed(ask_key):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": suppressed_reason(ask_key, action),
                }
            }
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
