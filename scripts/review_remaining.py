"""Advisory remaining-findings counts. The round budget itself is unchanged."""
from __future__ import annotations

from datetime import UTC, datetime

import review_late as late
import review_reflection as rr


def _weight(key: str) -> int:
    """A review-body key encodes a count, not a finding ordinal."""
    if key.startswith("r"):
        return int(key.rsplit(":", 1)[1])
    return 1


def remaining_report(cwd: str, repo: str, number: int, budget: dict, *, deadline: float) -> dict:
    """Read applicable local coverage or report unknown beside the raw API counts."""
    try:
        with rr.deadline_scope(deadline):
            return _remaining_report(cwd, repo, number, budget, deadline)
    except Exception:  # noqa: BLE001 - advisory failure must preserve the raw report
        return {"status": "unknown", "reason": "local coverage could not be validated"}


def _remaining_report(cwd: str, repo: str, number: int, budget: dict, deadline: float) -> dict:
    if budget.get("status") != "ok":
        raise rr.Refused("budget unknown")
    local_repo, local_pr = rr.pr_identity(cwd)
    if local_repo.casefold() != repo.casefold() or local_pr != number:
        raise rr.Refused("the local branch belongs to another PR")
    current = budget["current_head"]
    if not rr._is_ancestor(cwd, current, "HEAD"):
        raise rr.Refused("the current PR head is not in local history")
    rounds = budget.get("rounds") or []
    acceptance = rr._acceptance(budget, repo, number)
    # A late-only head can inherit a reflection round without contributing to
    # its raw count. Only the producer's raw population belongs in these rows.
    round_heads = {entry["head"] for entry in rounds}
    counted = {**budget, "findings": [source for source in budget.get("findings") or []
                                    if source.get("head") in round_heads]}
    found_late = late.late_findings(
        counted, rr.REFLECTION_CUTOVER_ISO, late.fix_time_reader(cwd, current),
    )
    if found_late is None:
        raise rr.Refused("late finding attribution unknown")
    late_details = late.coverage_details(
        cwd, budget, found_late, gate_lane=bool(budget.get("gate_surface")),
        acceptance=acceptance, deadline=deadline, repo=repo, number=number,
    ) if found_late else {}
    late_keys = {item["key"] for item in found_late}
    rows = []
    attributed = set()
    for entry in rounds:
        head, raw = entry["head"], entry["findings"]
        if type(raw) is not int:
            raise rr.Refused("raw count unknown")
        context = late.round_context(budget, head, datetime.max.replace(tzinfo=UTC))
        if context is None:
            raise rr.Refused("round context unknown")
        projected = {**budget, "current_head": head, "round_state": "open"}
        # The current open round uses exactly the ordinary gate context. Old
        # rounds use the existing chronological context used by late coverage.
        is_open = budget.get("round_state") == "open" and head == current
        keys = set()
        for source in entry.get("reviews") or []:
            for key in source.get("finding_keys") or []:
                if not isinstance(key, str) or not rr.HEADER_RE.fullmatch(
                    f"Round-reflection: keys={key} head={head}"
                ):
                    raise rr.Refused("finding identity unknown")
                keys.add(key)
        # Do not subtract if source attribution disagrees with the producer's
        # raw count, or one identity was assigned to multiple rounds.
        if keys & attributed or sum(_weight(k) for k in keys) != raw:
            raise rr.Refused("finding attribution unknown")
        attributed.update(keys)
        normal = rr.coverage_details(
            cwd, head,
            round_number=int(budget["count"]) if is_open else context.round_number,
            gate_lane=bool(budget.get("gate_surface")),
            prior_heads=[r["head"] for r in rounds if r["head"] != head] if is_open else context.prior_heads,
            key_rounds=rr.key_rounds(budget),
            acceptance=acceptance, round_started=entry["opened_at"],
            reviewed_at=rr._open_review_times(projected), deadline=deadline,
            repo=repo, number=number, wanted_keys=keys - late_keys,
        ) if keys - late_keys else {}
        no_defect = 0
        for key in keys:
            disposition = (late_details if key in late_keys else normal).get(key)
            if disposition is not None and disposition.kind == "no-defect":
                no_defect += _weight(key)
        rows.append({"head": head, "raw_findings": raw, "no_defect": no_defect,
                     "remaining_findings": raw - no_defect})
    # A credited late finding with no known round cannot be silently allocated.
    if any(d.kind == "no-defect" and key not in attributed for key, d in late_details.items()):
        raise rr.Refused("no-defect finding has no round attribution")
    counts = [r["remaining_findings"] for r in rows]
    trend = None
    if len(counts) >= 2:
        trend = "rising" if counts[-1] > counts[-2] else "falling" if counts[-1] < counts[-2] else "flat"
    return {"status": "ok", "rounds": rows, "trend": trend}
