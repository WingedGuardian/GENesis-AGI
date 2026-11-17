"""The Guardian on a `live` checkout: which candidate a code revert drops (#2978).

HOST-SIDE. On `live` a code revert never runs (it would commit onto the branch
the deploy engine rebuilds); REVERT_CODE there means dropping the newest
candidate with the engine's own `drop`. This module reads the engine's view
from the container, picks that candidate, and turns a diagnosis into what the
owner approves at Gate 2. recovery.py runs the approved drop.

Everything read from the container is untrusted: the view is size-capped,
validated, and every string from it that reaches a message is cleaned and cut.
While update.lock is held on `live`, the proposal is an ESCALATE that waits for
the lock (owner rulings, #2978); recovery.py re-reads the lock before it runs a
restart, since an approval can be hours old.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction

logger = logging.getLogger(__name__)

#: The engine's own view: what the manifest lists, what `live` holds, the
#: checkout it would move and every reason drop would refuse now. Lockless.
#: Capped in the container: the host buffers the whole output.
_VIEW_CAP = 1 << 20
_LIST_CMD = (
    "set -o pipefail; cd ~/genesis && scripts/deploy_candidates list --json --move-refusals"
    f" | head -c {_VIEW_CAP}"
)
_LIST_TIMEOUT_S = 60.0
#: The text form, for the diagnosis evidence: one line per candidate, in pin
#: order, with its added date and pinned head.
_TEXT_CMD = "cd ~/genesis && scripts/deploy_candidates list 2>/dev/null | head -c 16384"
#: The lock every deploy, rebuild, drop and validation hold takes; 75 = held.
_LOCK_PROBE = 'flock -n -E 75 "${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock" true'
_LOCK_HELD = 75
_MARKER_CMD = 'head -c 64 "${GENESIS_HOME:-$HOME/.genesis}/update_in_progress.pid"'
#: A branch name goes to the shell through shlex.quote; this only refuses what
#: git's own rules never let the engine list (control characters, spaces) and a
#: leading dash, which a command would read as an option.
_BRANCH_RE = re.compile(r"[^\x00-\x20\x7f-][^\x00-\x20\x7f]{0,249}")
_OID_RE = re.compile(r"[0-9a-f]{40}")
_SHOWN_CAP = 200

#: While an episode waits for update.lock, alert the owner again this often: the
#: deploy scripts' own lock-wait ceiling (GENESIS_DEPLOY_LOCK_WAIT, 7200 s), past
#: which the holder is likely hung. Alert only (owner ruling, #2978).
WAIT_REALERT_S = 7200

#: DiagnosisResult.escalation values: why a proposal became ESCALATE here.
WAITING_ON_LOCK = "update_lock_held"
HALF_DONE_DROP = "half_done_drop"


def shown(value: object, cap: int = _SHOWN_CAP) -> str:
    """A container string fit for a message: control characters and line
    breaks collapsed to spaces, cut at ``cap`` with a marker."""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f\u0085\u2028\u2029]", " ", str(value)).split())
    return text if len(text) <= cap else text[:cap] + " (cut)"


async def _in_container(container: str, cmd: str, timeout: float = 30.0) -> tuple[int, list[str]]:
    rc, stdout, _ = await _run_subprocess(
        "incus",
        "exec",
        container,
        "--",
        "su",
        "-",
        "ubuntu",
        "-c",
        cmd,
        timeout=timeout,
    )
    # On "\n" only: splitlines() also breaks on U+2028 and the like, which would
    # cut a container line in two. A login shell may print lines first.
    return rc, [ln for ln in stdout.strip().split("\n")]


def _valid_view(data: object) -> bool:
    """The keys this module reads, with the types the engine writes."""
    if not isinstance(data, dict) or data.get("version") != 1:
        return False
    listed, live = data.get("listed"), data.get("live")
    refusals, checkout = data.get("move_refusals"), data.get("checkout")
    if not isinstance(listed, list) or not isinstance(live, dict):
        return False
    if not all(isinstance(r, dict) and isinstance(r.get("branch"), str) for r in listed):
        return False
    holds = live.get("holds")
    if holds is not None and not (
        isinstance(holds, list)
        and all(
            isinstance(h, dict)
            and isinstance(h.get("branch"), str)
            and isinstance(h.get("head"), str)
            for h in holds
        )
    ):
        return False
    if live.get("tip") is not None and not isinstance(live.get("tip"), str):
        return False
    if not isinstance(refusals, list) or not all(isinstance(r, str) for r in refusals):
        return False
    return checkout is None or isinstance(checkout, dict)


async def live_candidates(container: str) -> dict | None:
    """The engine's `list --json --move-refusals` from the container, or None
    when it cannot be read or does not have the shape this module reads."""
    rc, lines = await _in_container(container, _LIST_CMD, timeout=_LIST_TIMEOUT_S)
    lines = [ln for ln in lines if ln.strip()]
    if rc != 0 or not lines:
        return None
    try:
        data = json.loads(lines[-1])  # the last line: a login shell may print first
    except (ValueError, RecursionError):  # RecursionError: deeply nested input
        return None
    return data if _valid_view(data) else None


def drop_target(data: dict) -> tuple[tuple[str, str, str] | None, str]:
    """(branch, head `live` holds for it, `live` tip) of the newest candidate a
    drop would take out, or (None, why not). The newest is the last one `live`
    holds that the manifest still lists: holds come in manifest order, which is
    pin order (a re-pin goes last)."""
    refusals = data["move_refusals"]
    if refusals:
        return None, f"a drop would refuse: {shown(refusals[0])}"
    live = data["live"]
    tip, holds = live.get("tip"), live.get("holds")
    if tip is None or holds is None:
        return (
            None,
            f"what `live` holds could not be read ({shown(live.get('reason') or 'no `live`')})",
        )
    checkout = data.get("checkout") or {}
    if checkout.get("branch_ref") != "refs/heads/live" or checkout.get("head") != tip:
        return None, "the checkout is not on `live` at its tip"
    listed = {r["branch"] for r in data["listed"]}
    unlisted = [h["branch"] for h in holds if h["branch"] not in listed]
    if unlisted:
        return None, half_done_reason(unlisted)
    held = [h for h in holds if h["branch"] in listed]
    if not held:
        return None, "`live` holds no candidate"
    branch, head = held[-1]["branch"], held[-1]["head"]
    if not (_BRANCH_RE.fullmatch(branch) and _OID_RE.fullmatch(head) and _OID_RE.fullmatch(tip)):
        return None, "the engine named a branch or commit this Guardian does not accept"
    return (branch, head, tip), ""


def half_done_reason(unlisted: list[str]) -> str:
    return (
        f"`live` still runs {shown(', '.join(unlisted))}, which the manifest no longer lists (a "
        "drop that saved the manifest but did not move the checkout); "
        "`scripts/deploy_candidates rebuild` finishes it"
    )


def is_half_done(data: dict | None) -> bool:
    """A drop saved the manifest and could not move: `live` holds a branch the
    manifest no longer lists. Finishing it is a rebuild, never the next drop."""
    if data is None or data["live"].get("holds") is None:
        return False
    listed = {r["branch"] for r in data["listed"]}
    return any(h["branch"] not in listed for h in data["live"]["holds"])


async def update_lock_holder(container: str) -> tuple[bool | None, str]:
    """(held, who) for update.lock in the container: True when another process
    holds it, False when free, None when the probe could not say. ``who`` names
    the deploy marker's pid when one is readable, else ""."""
    rc, _ = await _in_container(container, _LOCK_PROBE)
    if rc not in (0, _LOCK_HELD):
        return None, ""
    if rc == 0:
        return False, ""
    mrc, lines = await _in_container(container, _MARKER_CMD)
    pid = lines[-1].strip() if mrc == 0 and lines else ""
    named = pid.isascii() and pid.isdigit()
    return True, (f"the deploy marker names pid {pid}" if named else "")


async def candidates_evidence(container: str) -> str:
    """The manifest's candidates as the engine lists them (pin order, added
    date, pinned head), for the diagnosis; "" when there are none or the list
    cannot be read. Every line is untrusted and cleaned."""
    rc, lines = await _in_container(container, _TEXT_CMD, timeout=_LIST_TIMEOUT_S)
    if rc != 0:
        return ""
    rows = [shown(ln, 300) for ln in lines if re.match(r"\s*\d+\. ", ln)][:20]
    return "\n".join(rows)


# ── what the owner approves at Gate 2 ────────────────────────────────────────


def _escalation(diagnosis: DiagnosisResult, kind: str, why: str) -> DiagnosisResult:
    return replace(
        diagnosis,
        recommended_action=RecoveryAction.ESCALATE,
        drop_candidate=None,
        escalation=kind,
        likely_cause=diagnosis.likely_cause + " — " + why,
    )


async def escalate_if_update_running(
    container: str,
    diagnosis: DiagnosisResult,
    *,
    check_live: bool = False,
) -> DiagnosisResult:
    """On `live`, while update.lock is held, a drop or a restart would cut into
    the run holding it (a deploy, a rebuild, a drop, a validation hold): the
    proposal becomes ESCALATE, naming the holder, and the Guardian waits for the
    lock (owner rulings, #2978). A probe that cannot say leaves the proposal as
    it was. With ``check_live`` the branch is read only once the lock is held:
    a frozen container (a restart's usual case) then costs one probe, not three."""
    from genesis.guardian.recovery import container_on_live

    held, who = await update_lock_holder(container)
    if not held or (check_live and not await container_on_live(container)):
        return diagnosis
    logger.warning(
        "update.lock held on `live` — proposing ESCALATE instead of %s",
        diagnosis.recommended_action.value,
    )
    return _escalation(
        diagnosis,
        WAITING_ON_LOCK,
        "the checkout is on `live` and update.lock is held"
        + (f" ({who})" if who else "")
        + ": a deploy, rebuild, drop or validation hold is running, and a drop or a "
        "restart now would cut into it. Nothing was touched; the Guardian resumes "
        "once the lock is free",
    )


async def live_revert_proposal(container: str, diagnosis: DiagnosisResult) -> DiagnosisResult:
    """REVERT_CODE on `live`: the drop of the newest candidate when the engine
    says a drop can run; ESCALATE while update.lock is held or after a drop that
    did not finish; else the container restart. Decided BEFORE Gate 2, so the
    owner approves exactly what runs."""
    waiting = await escalate_if_update_running(container, diagnosis)
    if waiting.recommended_action == RecoveryAction.ESCALATE:
        return waiting
    data = await live_candidates(container)
    if is_half_done(data):
        unlisted = [
            h["branch"]
            for h in data["live"]["holds"]
            if h["branch"] not in {r["branch"] for r in data["listed"]}
        ]
        return _escalation(diagnosis, HALF_DONE_DROP, half_done_reason(unlisted))
    target, why = (
        drop_target(data)
        if data is not None
        else (
            None,
            "the engine's candidate list could not be read",
        )
    )
    if target is not None:
        branch, head, tip = target
        logger.warning("REVERT_CODE proposed on `live` — proposing the drop of %s", branch)
        return replace(
            diagnosis,
            drop_candidate=target,
            likely_cause=(
                diagnosis.likely_cause
                + f" — On `live`, REVERT_CODE means: drop the newest candidate {branch} "
                f"(its code at {head[:12]}) from the deploy manifest and rebuild `live` "
                f"without it (`live` runs {tip[:12]} now), re-sync the git hooks, then "
                "restart genesis-server. Candidates that share its unmerged commits leave "
                f"`live` with it (the engine names them). `scripts/deploy_candidates add "
                f"{branch} --owner <who>` puts it back"
            ),
        )
    logger.warning("REVERT_CODE proposed on `live` — proposing RESTART_CONTAINER (%s)", why)
    return replace(
        diagnosis,
        recommended_action=RecoveryAction.RESTART_CONTAINER,
        likely_cause=(
            diagnosis.likely_cause
            + " — the checkout is on `live` (or could not be shown not to be), where "
            f"a code revert never runs, and no candidate can be dropped now ({why}). "
            "Proposing a container restart: it lets a boot repair run where one is "
            "installed. A bad candidate is removed with "
            "`scripts/deploy_candidates drop <branch>`"
        ),
    )
