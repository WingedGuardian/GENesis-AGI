"""The Guardian on a `live` checkout: which candidate a code revert drops (#2978).

HOST-SIDE, and gated by ``recovery.live_engine`` (off by default: then nothing
here runs). On `live` a code revert never runs (it would commit onto the branch
the deploy engine rebuilds); REVERT_CODE there means dropping the newest
candidate with the engine's own guarded drop. This module reads the engine's
view from the container, picks that candidate, reads which commit
genesis-server booted from, and turns a diagnosis into what the owner approves
at Gate 2. recovery.py runs the approved drop as ONE engine call.

Everything read from the container is untrusted: the view is size-capped,
validated, and every string from it that reaches a message is cleaned and cut,
except the names Gate 2 approves, which must match _BRANCH_RE and are shown
in full.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.alert.telegram import _utf16_units
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
#: order (newest last), with its added date and pinned head. Bounded by line,
#: keeping the newest end (CodeRabbit c4605811665): each line cut at 600 bytes,
#: then the last 40 lines, so a cut never removes the drop target and never
#: leaves a partial first line that could read as a row.
_EVIDENCE_ROWS = 20
_TEXT_CMD = (
    "set -o pipefail; cd ~/genesis && scripts/deploy_candidates list 2>/dev/null"
    " | cut -b 1-600 | tail -n 40"
)
#: Which commit genesis-server booted from, read the way deploy_code_only.sh
#: status reads it (scripts/lib/deploy_status.sh _read_serving): the unit's
#: ActiveEnterTimestamp against the checkout's HEAD reflog, through
#: scripts/lib/serving_commit.py, which a candidate may not change (admission).
#: Prints the unit's state, `live`'s tip and boot=<commit | unknown: why>.
_BOOT_PROBE = (
    "cd ~/genesis || exit 1; g='git --no-optional-locks'; "
    "systemctl --user show genesis-server -p ActiveState -p SubState -p InvocationID"
    " -p Result || exit 1; "
    'echo "live=$($g rev-parse -q --verify refs/heads/live)"; '
    "d=$(systemctl --user show genesis-server -p WorkingDirectory --value); "
    'if [ -z "$d" ] || [ "$(realpath -m "$d")" != "$(realpath -m .)" ]; then '
    'echo "boot=unknown: genesis-server runs in ${d:-an unreadable directory}"; exit 0; fi; '
    "b=$(systemctl --user show genesis-server -p ActiveEnterTimestamp --timestamp=unix --value); "
    "c=$($g config --type=expiry-date gc.reflogExpireUnreachable); r=$?; "
    'if [ "$r" = 1 ]; then c=$(( $(date +%s) - 2592000 )); elif [ "$r" != 0 ]; then c=; fi; '
    'echo "boot=$(python3 -I -S scripts/lib/serving_commit.py '
    '"$($g rev-parse --absolute-git-dir)/logs/HEAD" "${b#@}" "$($g rev-parse HEAD)" "$c")"'
)
#: A branch name goes to the shell through shlex.quote; this only refuses what
#: git's own rules never let the engine list (control characters, spaces) and a
#: leading dash, which a command would read as an option.
_BRANCH_RE = re.compile(r"[^\x00-\x20\x7f-][^\x00-\x20\x7f]{0,249}")
_OID_RE = re.compile(r"[0-9a-f]{40}")
_SHOWN_CAP = 200
#: A name in Gate 2's identity section is never cut: _BRANCH_RE's maximum.
_NAME_CAP = 250
#: Gate 2's identity section, in UTF-16 units; the diagnosis gets what
#: remains of Telegram's 4,096. Over it, no drop is proposed.
_IDENTITY_CAP = 3000

#: DiagnosisResult.escalation: a drop saved the manifest and did not move `live`.
HALF_DONE_DROP = "half_done_drop"


def shown(value: object, cap: int = _SHOWN_CAP) -> str:
    """A container string fit for a message: control characters and line
    breaks collapsed to spaces, cut at ``cap`` with a marker."""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f\u0085  ]", " ", str(value)).split())
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
    # An unread manifest lists nothing: held candidates would read as dropped.
    manifest = data.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("state") not in ("ok", "absent"):
        return False
    if not all(
        isinstance(r, dict) and isinstance(r.get("branch"), str) and isinstance(r.get("head"), str)
        for r in listed
    ):
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


def drop_target(data: dict) -> tuple[tuple[str, str, str, str] | None, str]:
    """(branch, head the manifest pins, head `live` holds for it, `live` tip) of
    the newest candidate a drop would take out, or (None, why not). The newest
    is the last one `live` holds that the manifest still lists: holds come in
    manifest order, which is pin order (a re-pin goes last). The pinned head is
    what the engine's --expect-candidate checks."""
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
    if not settled(data):
        return None, "the checkout is not on `live` at its tip"
    pins = {r["branch"]: r["head"] for r in data["listed"]}
    unlisted = [h["branch"] for h in holds if h["branch"] not in pins]
    if unlisted:
        return None, half_done_reason(unlisted)
    held = [h for h in holds if h["branch"] in pins]
    if not held:
        return None, "`live` holds no candidate"
    branch, head = held[-1]["branch"], held[-1]["head"]
    pinned = pins[branch]
    if not (
        _BRANCH_RE.fullmatch(branch) and all(_OID_RE.fullmatch(c) for c in (pinned, head, tip))
    ):
        return None, "the engine named a branch or commit this Guardian does not accept"
    return (branch, pinned, head, tip), ""


def settled(data: dict) -> bool:
    """Whether the checkout is on `live` at its tip: no move is half-way."""
    checkout = data.get("checkout") or {}
    tip = data["live"].get("tip")
    on_live = checkout.get("branch_ref") == "refs/heads/live"
    return tip is not None and on_live and checkout.get("head") == tip


def half_done_reason(unlisted: list[str]) -> str:
    return (
        f"`live` still runs {shown(', '.join(unlisted))}, which the manifest no longer lists (a "
        "drop that saved the manifest but did not move the checkout); "
        "`scripts/deploy_candidates rebuild` finishes it"
    )


def half_done(data: dict | None) -> str:
    """Why `live` is half-done, or "": a drop saved the manifest and could not
    move, so `live` holds a branch the manifest no longer lists. Finishing it
    is a rebuild, never the next drop."""
    if data is None or data["live"].get("holds") is None:
        return ""
    listed = {r["branch"] for r in data["listed"]}
    unlisted = [h["branch"] for h in data["live"]["holds"] if h["branch"] not in listed]
    return half_done_reason(unlisted) if unlisted else ""


def gone(data: dict, branch: str) -> bool:
    """Whether ``branch`` is out of the manifest and out of `live`."""
    holds = data["live"].get("holds")
    return (
        holds is not None
        and all(r["branch"] != branch for r in data["listed"])
        and all(h["branch"] != branch for h in holds)
    )


async def server_boot(container: str) -> dict[str, str] | None:
    """genesis-server's ActiveState, SubState, InvocationID and Result, `live`'s
    tip (``live``) and the commit it booted from (``boot``: a commit, or
    ``unknown: <why>``), or None when the unit cannot be read."""
    rc, lines = await _in_container(container, _BOOT_PROBE)
    if rc != 0:
        return None
    vals = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    return vals if "ActiveState" in vals else None


def booted_from(unit: dict[str, str] | None) -> str:
    """The commit a RUNNING genesis-server booted from, or "" when it is not
    running or that cannot be said."""
    boot = (unit or {}).get("boot", "")
    return boot if unit and unit.get("ActiveState") == "active" and _OID_RE.fullmatch(boot) else ""


async def candidates_evidence(container: str) -> str:
    """The manifest's candidates as the engine lists them (pin order, added
    date, pinned head), for the diagnosis; "" when there are none or the list
    cannot be read. Every line is untrusted and cleaned."""
    rc, lines = await _in_container(container, _TEXT_CMD, timeout=_LIST_TIMEOUT_S)
    if rc != 0:
        return ""
    rows = [shown(ln, 300) for ln in lines if re.match(r"\s*\d+\. ", ln)][-_EVIDENCE_ROWS:]
    if not rows:
        return ""
    first = int(re.match(r"\s*(\d+)\. ", rows[0]).group(1))
    if first > 1:
        rows.insert(0, f"({first - 1} older candidate(s) not shown; pin order, newest last)")
    return "\n".join(rows)


def attempt_line(attempt: dict | None) -> str:
    """The Guardian's last drop this episode, for the diagnosis; "" for none."""
    if not attempt:
        return ""
    return (
        f"The Guardian's last drop this episode: {shown(attempt['branch'])} (pinned at "
        f"{attempt['pinned_head'][:12]}), attempted at {shown(attempt['at'], 40)}, when `live` "
        f"was at {attempt['tip'][:12]}"
    )


# ── what the owner approves at Gate 2 ────────────────────────────────────────


def _escalation(diagnosis: DiagnosisResult, kind: str, why: str) -> DiagnosisResult:
    return replace(
        diagnosis,
        recommended_action=RecoveryAction.ESCALATE,
        drop_candidate=None,
        escalation=kind,
        likely_cause=diagnosis.likely_cause + " — " + why,
    )


def _leaving(data: dict, branch: str) -> tuple[tuple[str, ...] | None, str]:
    """The other candidates the engine's own plan of this drop takes out of
    `live` (its drop_preview), or (None, why) when they are not known in full:
    no drop is proposed then, never one with a partial list."""
    preview = data.get("drop_preview")
    leaving = preview.get("leaving") if isinstance(preview, dict) else None
    if not (
        isinstance(leaving, list)
        and all(isinstance(b, str) and _BRANCH_RE.fullmatch(b) for b in leaving)
    ):
        return None, f"the engine could not plan which candidates leave `live` with {branch}"
    if preview.get("branch") != branch:
        was = shown(preview.get("branch"), _NAME_CAP)
        return None, f"the engine planned the drop of {was}, not of {branch}"
    return tuple(leaving), ""


def _identity(target: tuple[str, str, str, str], leaving: tuple[str, ...], note: str) -> str:
    """Gate 2's section that is never cut: every name and commit the approval acts on."""
    branch, pinned, head, tip = target
    code = f"its code at {head}" + (f", pinned at {pinned}" if pinned != head else "")
    others = (
        f"Also leaving `live` with it: {', '.join(leaving)}."
        if leaving
        else "No other candidate leaves `live` with it."
    )
    return (
        f"Drops {branch} ({code}) from the deploy manifest and rebuilds `live` without it "
        f"(`live` runs {tip} now), re-syncs the git hooks, then restarts genesis-server. "
        f"{others} `scripts/deploy_candidates add {branch} --owner <who>` puts it back{note}."
    )


def _since(attempt: dict | None, target: tuple[str, str, str, str]) -> str:
    """What the proposal says about the last drop, when there was one."""
    if not attempt:
        return ""
    branch, pinned = target[0], target[1]
    was, at = shown(attempt["branch"], _NAME_CAP), shown(attempt["at"], 40)
    if branch == attempt["branch"] and pinned == attempt["pinned_head"]:
        return f". This re-runs the drop attempted at {at}, whose outcome was not seen"
    if branch == attempt["branch"]:
        return f". {was} was re-pinned at {pinned} since the drop attempted at {at}"
    return f". The drop of {was} attempted at {at}: {was} is no longer the newest candidate"


async def live_revert_proposal(
    container: str,
    diagnosis: DiagnosisResult,
    attempt: dict | None = None,
) -> DiagnosisResult:
    """REVERT_CODE on `live`: the drop of the newest candidate when the engine
    says a drop can run and Gate 2 can name everything it removes; ESCALATE
    after a drop that did not finish; else the container restart. Decided
    BEFORE Gate 2, so the owner approves exactly what runs. ``attempt`` is the
    state's record of the last drop this episode: while the checkout has not
    settled since, nothing is proposed (that drop may still be running), and
    once it finished, a server that has not booted from `live`'s tip gets a
    restart before any further drop."""
    data = await live_candidates(container)
    unfinished = half_done(data)
    if unfinished:
        if attempt:  # between its manifest save and its move, a running drop reads the same
            unfinished += (
                f". The drop of {shown(attempt['branch'])} attempted at "
                f"{shown(attempt['at'], 40)} may still be running: rebuild only once "
                "update.lock is free"
            )
        return _escalation(diagnosis, HALF_DONE_DROP, unfinished)
    note = ""
    if attempt and data is not None:
        was = shown(attempt["branch"], _NAME_CAP)
        if not settled(data):
            return _escalation(
                diagnosis,
                "",
                f"the checkout is not on `live` at its tip since the drop of {was} attempted at "
                f"{shown(attempt['at'], 40)}, which may still be running. Nothing is proposed "
                "now; the Guardian asks again only if this episode auto-resets",
            )
        if gone(data, attempt["branch"]):
            unit = await server_boot(container)
            # The LAST boot, running or not: a server crash-looping on the new
            # tip took the drop, and the next candidate is the suspect.
            if (unit or {}).get("boot", "") != data["live"]["tip"]:
                logger.warning("the drop of %s finished; proposing RESTART_SERVICES", was)
                boot = shown((unit or {}).get("boot") or "unreadable", 120)
                return replace(
                    diagnosis,
                    recommended_action=RecoveryAction.RESTART_SERVICES,
                    likely_cause=(
                        diagnosis.likely_cause + f" — the drop of {was} finished; the server may "
                        f"not have restarted onto it (it booted from {boot}, `live` is at "
                        f"{data['live']['tip'][:12]}). Proposing a genesis-server restart"
                    ),
                )
            note = (
                f". The drop of {was} attempted at {shown(attempt['at'], 40)} finished and the "
                "server booted from `live` since"
            )
    target, why = (
        drop_target(data)
        if data is not None
        else (
            None,
            "the engine's candidate list could not be read",
        )
    )
    if target is not None:
        leaving, why = _leaving(data, target[0])
        note = note or _since(attempt, target)
        identity = "" if leaving is None else _identity(target, leaving, note)
        if identity and _utf16_units(identity) > _IDENTITY_CAP:
            why = (
                f"the {len(leaving)} candidates leaving `live` with {target[0]} are too many "
                "to list in one approval"
            )
        elif identity:
            logger.warning("REVERT_CODE proposed on `live` — proposing the drop of %s", target[0])
            return replace(
                diagnosis,
                drop_candidate=(*target, leaving),
                gate2_identity=identity,
                likely_cause=diagnosis.likely_cause
                + f" — on `live`, REVERT_CODE drops the newest candidate "
                f"{shown(target[0], _NAME_CAP)}; the approval lists everything it removes",
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


async def interrupt_warning(
    container: str, diagnosis: DiagnosisResult, attempt: dict | None
) -> DiagnosisResult:
    """A container restart or rollback while the last drop may still be moving
    `live`: Gate 2 says the action would interrupt it. A warning, not a block:
    the boot repair exists for an interrupted move, and B2 owns the wait."""
    if not attempt or diagnosis.recommended_action not in (
        RecoveryAction.RESTART_CONTAINER,
        RecoveryAction.SNAPSHOT_ROLLBACK,
    ):
        return diagnosis
    view = await live_candidates(container)
    if view is not None and settled(view):
        return diagnosis
    warn = (
        f"Warning: a drop attempted at {shown(attempt['at'], 40)} may still be running; "
        "this action would interrupt it."
    )
    return replace(
        diagnosis, gate2_identity=" ".join(t for t in (diagnosis.gate2_identity, warn) if t)
    )
