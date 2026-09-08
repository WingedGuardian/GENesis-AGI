#!/usr/bin/env python3
"""PreToolUse gate: an ask must carry a blocking gate's declared remedies.

WHY THIS EXISTS, measured rather than supposed. On 2026-08-31 the review
escalation cap blocked with a message whose first line enumerated three remedies
to relay to the user. The relay dropped the first, invented a fourth, and added
"ship as-is" — the outcome that cap exists to prevent. The user had to push back
twice before the option the gate had actually asked for was produced.

The first fix attempted was a note-to-self, which is the same class of thing that
failed: a convention, asked to hold at the one moment attention is elsewhere. This
is the mechanical version. While a gate's remedy set is declared and still
unacknowledged, an ``AskUserQuestion`` is refused unless one of its questions
offers every declared remedy among its options.

WHY A BLOCK AND NOT AN ADVISORY. The house default is advisory, and escalating
needs a specific measured reason. Here it is: the failure mode is a strong prior
overriding text that was one message back, so re-showing the text is precisely
the intervention that does not work — an advisory is more of what already failed.
The usual cost of blocking an ask does not apply either, because the convention
already mandates at least two questions per call, so the gate's question rides
ALONGSIDE whatever else is being asked and no legitimate work is refused.

FAIL DIRECTION — INVERTED, ON PURPOSE. This guard fails OPEN on any internal
error and is deliberately NOT wired through ``run_guard``. A guard that can refuse
``AskUserQuestion`` can, when buggy, leave a session unable to ask the user
anything at all, including how to unwedge it: its failure costs more than its
miss. Blast radius is bounded — a demand only exists once a cap has actually
blocked, so a malfunction bites while a stop is already live, never on ordinary
work. A test pins the absence of ``run_guard`` so this cannot be "tidied" later.

Foreground and background are blocked identically. That is correct rather than an
oversight: a block is not an "ask", it stops both equally, and the refusal names
exactly which options to add — so an unattended session can comply on its own
instead of stalling on a human who is not there.

Kill switches, because anything that can refuse a question needs an exit:
``GENESIS_GATE_ACK_DISABLED=1`` (unconditional), and ``enabled: false`` in
``~/.genesis/config/gate_ack.yaml``. Both live OUTSIDE the repo. The honest
caveat, recorded because it is easy to overstate what this achieves: anything a
session can write, a session can write to disable itself. This is a recovery
tool, not insulation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HOOK_ROOT = Path(__file__).resolve().parent
if str(_HOOK_ROOT) not in sys.path:
    sys.path.insert(0, str(_HOOK_ROOT))
if str(_HOOK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_HOOK_ROOT.parent))

_CONFIG = Path.home() / ".genesis" / "config" / "gate_ack.yaml"


def _enabled() -> bool:
    """Whether the gate is armed.

    A malformed config leaves it ARMED, which is the opposite of this guard's
    internal fail-open and is the point: an internal error can wedge a session, so
    the guard's own faults fail open, but a config typo is a human's mistake and
    silently disarming a gate on one is the failure this repo keeps re-learning.
    The env switch remains unconditional, so nobody is ever stuck.
    """
    if os.environ.get("GENESIS_GATE_ACK_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return False
    try:
        if not _CONFIG.is_file():
            return True
        import yaml

        data = yaml.safe_load(_CONFIG.read_text())
        if isinstance(data, dict) and data.get("enabled") is False:
            return False
    except Exception:  # noqa: BLE001 — a bad config must not disarm the gate
        return True
    return True


def main() -> int:
    from gate_demand import missing_remedies
    from hook_input import read_payload, tool_input

    payload = read_payload()
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    if not _enabled():
        return 0

    # A shape surprise here must not BLOCK. `hook_input.tool_input` falls back to
    # the whole payload when `tool_input` is absent, so a schema drift that moved
    # or dropped that key would yield questions=None -> every remedy "missing" ->
    # every question in the session refused. That is the one outcome this guard's
    # inverted fail direction exists to prevent, arriving through the door marked
    # "degrades toward not-covered".
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, dict):
        return 0

    from review_state import read_gate_demand

    cwd = payload.get("cwd") or None
    cwd = cwd if isinstance(cwd, str) else None
    session_id = payload.get("session_id")
    demand = read_gate_demand(
        cwd=cwd, session_id=session_id if isinstance(session_id, str) else None
    )
    if not demand:
        return 0

    remedies = demand["remedies"]
    missing = missing_remedies(tool_input(payload).get("questions"), remedies)
    if not missing:
        return 0

    by_key = {r["key"]: r.get("label") or r["key"] for r in remedies}
    listed = "\n".join(f"  - {k}: {by_key[k]}" for k in missing)
    everything = "\n".join(f"  - {r['key']}: {r.get('label') or r['key']}" for r in remedies)
    print(
        f"BLOCKED: the '{demand.get('gate', 'gate')}' gate is waiting on a decision, "
        "and this question does not offer the remedies it enumerated.\n\n"
        f"{demand.get('required_action', '')}\n\n"
        "MISSING from every question's options:\n"
        f"{listed}\n\n"
        "The full declared set:\n"
        f"{everything}\n\n"
        "ONE question must offer EXACTLY these remedies: one option per remedy, "
        "each recognisable by name in its LABEL (not only its description), and "
        "NO other options in that question. An extra option is refused too — a "
        "menu carrying a choice this gate did not offer is not this gate's menu, "
        "whatever else it also contains.\n\n"
        "If an option names two remedies, REWORD it into two; adding more will "
        "not clear this. If you genuinely need a further choice, put it in a "
        "SECOND question — other questions ride along freely, and the convention "
        "already asks for at least two, so nothing has to be dropped.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    # NOT run_guard: see the fail-direction paragraph in the module docstring.
    # An unexpected fault here must never wall off the session's ability to ask.
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail OPEN, loudly
        print(
            f"GUARD ERROR (ask_question_gate): failing OPEN — {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(0)
