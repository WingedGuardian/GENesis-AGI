"""Dispatch an external review orchestrator at open pull requests, unattended.

WHAT THIS IS
============
Some installs run a separate tool that reviews a pull request — spawning its own
fresh-context agent sessions, judging the change through several lenses, and
posting one report as a PR comment. This module is the ADAPTER that triggers such
a tool on a schedule and decides which pull requests are worth spending it on.

This repository ships the adapter ONLY. It ships no orchestrator, names none as a
default, and assumes nothing about which one an install runs: the executable, its
argv shape, the workflow to run and the marker it stamps its report with are all
install-local configuration, because they describe a tool that lives outside this
repository. The shipped config is empty, so a fresh clone dispatches nothing and
says so — the same graceful-degradation contract every other optional dependency
here follows.

SCOPE IS THE SAFETY PROPERTY
============================
An orchestrator of this kind typically spawns agent sessions from its own process,
outside this codebase, so it does not pass through ``autonomy/cli_policy``'s
approval gate — that gate is an in-process chokepoint on Genesis's own dispatch
paths, not an intercept of another program's children. Where an operator has
given standing sign-off for autonomous review runs, a per-run gate would ask for
the same approval twice; what must NOT follow is unlimited scope. Three controls
stand in for the gate:

* ``allow_workflows`` — a closed, operator-declared set, EMPTY by default, so a
  workflow that writes code or opens pull requests cannot be dispatched by
  accident and a typo can only ever narrow what runs;
* the audit trail this module writes, so every dispatch is recoverable after the
  fact — an orchestrator keeps its own history, but outside this system's backup
  and observability, and a trail that replaces a gate should be ours;
* a config kill switch plus a hard per-scan budget, so it can be stopped and
  cannot run away.

Issue #2003 tracks routing such a dispatch through the approval gate properly.

FAIL DIRECTIONS, chosen rather than inherited
=============================================
Every uncertainty here resolves toward NOT DISPATCHING. The asymmetry is real: a
missed review costs one pull request a round it can get later, while a wrongly
repeated review spends minutes of an agent-subscription window that every
FOREGROUND session shares, and posts a duplicate report. So an unreadable comment
list, an unparseable head, an unknown CI state, an unresolvable repository and an
unconfigured orchestrator all SKIP rather than proceed — the opposite of the
usual "surface everything" posture, and deliberate.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from genesis.session_awareness import external_review_config as config

logger = logging.getLogger(__name__)

#: Where dispatch records land. Written through the shared hook-audit writer, which
#: gives one file per flush, 0600, atomic publish, and never raises at the caller.
_STORE_DIRNAME = "external_review_runs"

#: A dispatch is fire-and-forget: the workflow runs for minutes and its OUTCOME
#: lands on the pull request as the orchestrator's own comment. These are the
#: states a dispatch DECISION can end in; the audit row records exactly one.
DECISION_DISPATCHED = "dispatched"
DECISION_DRY_RUN = "dry_run"
DECISION_SKIPPED = "skipped"
DECISION_FAILED = "failed"


def store_dir() -> str:
    """The audit store path — the single answer, for this module and the shell.

    Exposed via ``--store-dir`` on the CLI so ``disk_hygiene.sh`` asks rather than
    hardcoding a second copy. That is the exact drift the shared audit writer's own
    resolver was extracted to kill: the same rule written at five call sites was
    wrong at one of them, and the pruner then trimmed the wrong directory while the
    real store grew unbounded.
    """
    override = os.environ.get("GENESIS_EXTERNAL_REVIEW_DIR")
    if override and os.path.isabs(override):
        return override
    if override:
        logger.warning("GENESIS_EXTERNAL_REVIEW_DIR must be absolute; ignoring %r", override)
    return os.path.expanduser(f"~/.genesis/{_STORE_DIRNAME}")


def log_dir() -> str:
    """Where a dispatched run's own output lands — a SUBDIRECTORY of the store.

    Kept separate from the audit rows because the two need different retention and
    only one of them the shared writer can bound: ``trim_dir_by_size`` counts and
    reaps ``*.jsonl`` ONLY, so a raw ``.log`` sitting beside the rows would be
    invisible to it and grow forever. These are file-age pruned by disk-hygiene
    instead, the same shape the retrieval-efficacy reports use. A subdirectory is
    safe here: the size trim skips anything not ending in ``.jsonl``, directories
    included.
    """
    return os.path.join(store_dir(), "logs")


def _default_runner(argv: Sequence[str], *, timeout: int = 60) -> tuple[int, str, str]:
    """Run a command, returning ``(returncode, stdout, stderr)``. Never raises."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv from callers, never a shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — a runner failure must degrade, not raise
        return 1, "", repr(exc)


Runner = Callable[..., tuple[int, str, str]]


def resolve_repo(runner: Runner = _default_runner) -> str | None:
    """The repo slug, resolved LIVE.

    Never a configured value: this repo's own guidance is that a configured slug can
    name a real-but-wrong repository and return plausible stale data. ``None`` on any
    failure, which callers treat as "do not dispatch".
    """
    rc, out, err = runner(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc != 0:
        logger.warning("external_review: could not resolve repo slug (%s)", err.strip())
        return None
    return out.strip() or None


def report_mentions_head(comment_bodies: Sequence[str], *, marker: str, head: str) -> bool:
    """Whether the orchestrator's own report already covers ``head``.

    Deliberately a CONTAINMENT check rather than a parse. We already know which
    commit we are asking about, so there is nothing to extract: a report that
    reviewed this head names it, and one that does not, does not. That keeps dedup
    correct for ANY orchestrator, with no per-vendor pattern to configure and
    nothing to go stale when a tool reformats its output.

    The answer lives on the pull request rather than in local state, so it is the
    same on every machine and survives this box losing its store entirely.
    """
    if not marker or len(head) != 40:
        return False
    needle = head.lower()
    return any(marker in body and needle in body.lower() for body in comment_bodies if body)


def _pr_comment_bodies(pr: int, repo: str, runner: Runner = _default_runner) -> list[str] | None:
    """Every issue-comment body on the PR, or ``None`` when the read FAILED.

    The None/[] distinction is the whole point: an empty list means "read fine, no
    comments", while None means "could not tell", and only the first is safe to treat
    as 'never reviewed'.
    """
    # Each body is base64-encoded by jq so ONE COMMENT IS ONE LINE. A plain
    # `-q .[].body` emits the bodies raw, and a review comment is many lines, so
    # splitting the output on newlines shreds each comment into fragments — the
    # report marker lands in one fragment and the head SHA in another, and a dedup
    # needing both in the same string then never matches. MEASURED against a live
    # pull request: the raw form produced 174 "bodies" for 4 comments and reported an
    # already-reviewed head as never reviewed.
    rc, out, err = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr}/comments",
            "--paginate",
            "-q",
            ".[].body | @base64",
        ]
    )
    if rc != 0:
        logger.warning("external_review: PR #%s comments unreadable (%s)", pr, err.strip())
        return None

    bodies: list[str] = []
    for line in out.split("\n"):
        token = line.strip()
        if not token:
            continue
        try:
            bodies.append(base64.b64decode(token, validate=True).decode("utf-8", "replace"))
        except (binascii.Error, ValueError) as exc:
            # One unreadable comment must not be reported as "no comments" — that
            # reads as "never reviewed" and would re-dispatch. Fail the whole read
            # instead, which the caller treats as "cannot rule out a duplicate".
            logger.warning("external_review: PR #%s comment undecodable (%r)", pr, exc)
            return None
    return bodies


def orchestrator_binary(command: str) -> str | None:
    """Resolve the configured executable, or ``None`` when it is not installed.

    Resolved explicitly rather than trusting PATH: a systemd timer runs with a
    minimal environment, and a user-local bin directory is commonly absent from it —
    the exact shape that makes a feature work by hand and silently no-op under the
    timer.
    """
    if not command:
        return None
    if os.path.isabs(command):
        return command if os.access(command, os.X_OK) else None
    found = shutil.which(command)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / command
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _repo_root() -> Path:
    from genesis.env import repo_root

    return Path(repo_root())


def build_argv(block: dict[str, Any], *, workflow: str, pr: int) -> list[str] | None:
    """The full argv for one dispatch, or ``None`` when the config cannot make one.

    ``argv`` is an install-local template because an orchestrator's command line is
    its own business; ``{workflow}`` and ``{pr}`` are the only substitutions, and a
    template naming neither is accepted (a tool may take the PR another way).
    """
    exe = orchestrator_binary(str(block.get("command") or ""))
    if not exe:
        return None
    template = block.get("argv")
    if not isinstance(template, list) or not all(isinstance(a, str) for a in template):
        logger.warning("external_review: orchestrator.argv must be a list of strings")
        return None
    return [exe] + [a.replace("{workflow}", workflow).replace("{pr}", str(pr)) for a in template]


def dispatch(
    pr: int,
    *,
    workflow: str,
    block: dict[str, Any],
    dry_run: bool = False,
) -> tuple[str, str]:
    """Spawn the orchestrator for ``pr``. Returns ``(decision, detail)``.

    DETACHED and fire-and-forget: a review runs for minutes, far past any caller's
    patience, so this returns as soon as the child is started. The workflow's
    OUTCOME is not this function's to report — it lands on the pull request as the
    orchestrator's own comment, and the audit row records that a dispatch happened.
    """
    allowed = block.get("allow_workflows") or []
    if not isinstance(allowed, list) or workflow not in allowed:
        # A consistency check between the two values the CALLER supplied, not an
        # independent guard: `allowed` comes from the same block as the workflow, so
        # a caller that supplies both can satisfy it. It catches the realistic
        # mistake — a workflow passed alongside the install's real config — and an
        # earlier version of this comment claimed it could stop a caller "routing
        # around" the closed set, which it cannot. The set is enforced where it is
        # READ FROM DISK, in external_review_config.workflow_name.
        return DECISION_FAILED, f"workflow {workflow!r} is not in allow_workflows"

    if dry_run:
        return DECISION_DRY_RUN, f"would run {workflow} for PR #{pr}"

    argv = build_argv(block, workflow=workflow, pr=pr)
    if not argv:
        return DECISION_FAILED, "orchestrator not installed or not configured"

    # BOTH streams go to a per-run log, and neither is discarded. A detached run
    # reports its verdict nowhere else this process can see, and "no comment appeared
    # on the pull request" is the same observation for a crash, a refusal, and a run
    # still in flight. Retention: file-age pruned by disk-hygiene (see log_dir).
    try:
        logs = Path(log_dir())
        logs.mkdir(parents=True, exist_ok=True)
        run_log = logs / f"run-pr{pr}-{os.getpid()}.log"
        log_fh = run_log.open("ab")
    except Exception as exc:  # noqa: BLE001
        return DECISION_FAILED, f"could not open run log: {exc!r}"

    try:
        with log_fh:
            subprocess.Popen(  # noqa: S603 — resolved binary, config argv, no shell
                argv,
                cwd=str(_repo_root()),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:  # noqa: BLE001 — a spawn failure is recorded, not raised
        return DECISION_FAILED, f"spawn failed: {exc!r}"
    return DECISION_DISPATCHED, f"{workflow} dispatched for PR #{pr}; log {run_log.name}"


def compress_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ACTED-ON decision in full, plus ONE counted summary of the skips.

    A scan considers the whole open queue — 72 pull requests on this repository the
    day this was written — and most are skipped for a handful of repeated reasons.
    Writing a row each is both noise and a real failure: the shared audit writer
    refuses a batch over its row cap OUTRIGHT, so an uncompressed scan wrote NOTHING
    and the audit trail — the control standing in for the approval gate this path
    does not pass through — was silently empty. MEASURED on the first live scan.

    This is a SELECTION, not a truncation: nothing acted on is dropped, and the
    skipped population survives as counts per reason with its denominator intact. The
    result is bounded by the dispatch budget plus one, so a queue of any size fits.
    """

    def _fold(bucket: list[dict[str, Any]], decision: str) -> dict[str, Any]:
        by_reason: dict[str, int] = {}
        for row in bucket:
            key = str(row.get("reason") or row.get("detail") or "unknown")
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "decision": decision,
            "kind": "scan_summary",
            f"{decision}_total": len(bucket),
            f"{decision}_by_reason": by_reason,
            "prs": sorted(int(r["pr"]) for r in bucket if isinstance(r.get("pr"), int)),
        }

    skipped = [r for r in rows if r.get("decision") == DECISION_SKIPPED]
    # FAILURES fold too. Folding only the skips left the one population that can
    # legitimately reach every PR in the queue — a dispatch that fails for every one
    # of them — passing through whole, over the writer's row cap, and refused
    # outright. The bound this function advertises has to hold for EVERY decision
    # that can occur N times, not just the one that happened to when it was written.
    failed = [r for r in rows if r.get("decision") == DECISION_FAILED]
    acted = [r for r in rows if r.get("decision") not in (DECISION_SKIPPED, DECISION_FAILED)]
    out = list(acted)
    if failed:
        out.append(_fold(failed, DECISION_FAILED))
    if skipped:
        out.append(_fold(skipped, DECISION_SKIPPED))
    return out


#: How long a recorded dispatch suppresses another for the same (pr, head).
#: A review runs for minutes; this is generous enough to cover a slow one and short
#: enough that a genuinely lost dispatch retries the same day.
DISPATCH_COOLOFF_S = 6 * 3600


def recent_dispatch_heads(within_s: int = DISPATCH_COOLOFF_S) -> set[tuple[int, str]]:
    """``(pr, head)`` pairs this box dispatched recently, from the audit store.

    THE SECOND HALF OF DEDUP, and the half that cannot live on the pull request.
    Asking the PR answers "did a report appear", which is the wrong question while a
    review is still running or after one died without posting: both look identical to
    "never reviewed", so every tick would dispatch the same head again — MEASURED,
    three consecutive ticks, three dispatches of one PR. At roughly 3% of the shared
    subscription window each, an unattended loop is the expensive failure this
    module's stated fail direction says must never happen.

    Reading our own attempts restores that posture: an attempt we cannot confirm
    completed is treated as possibly in flight, and we skip. Unreadable store, absent
    store, malformed row — every one degrades to an EMPTY set, which is the
    permissive direction, so this can only ever suppress a dispatch that the PR-side
    check already permitted. It narrows; it never widens.
    """
    import time

    out: set[tuple[int, str]] = set()
    store = Path(store_dir())
    if not store.is_dir():
        return out
    cutoff = time.time() - max(0, within_s)
    try:
        entries = [e for e in os.scandir(store) if e.is_file() and e.name.endswith(".jsonl")]
    except OSError as exc:
        logger.warning("external_review: audit store unreadable (%r)", exc)
        return out

    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                continue
            for line in Path(entry.path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("decision") != DECISION_DISPATCHED:
                    continue
                pr = row.get("pr")
                head = str(row.get("head") or "").lower()
                if isinstance(pr, int) and len(head) == 40:
                    out.add((pr, head))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            # One bad file must not blind the whole check; the rest still narrow.
            logger.warning("external_review: audit row unreadable in %s (%r)", entry.name, exc)
            continue
    return out


def record(rows: Sequence[dict[str, Any]]) -> str | None:
    """Append dispatch records to the audit store. Never raises."""
    if not rows:
        return None
    hooks_dir = _repo_root() / "scripts" / "hooks"
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))
    try:
        from audit_jsonl import write_batch
    except Exception as exc:  # noqa: BLE001
        logger.warning("external_review: audit writer unavailable (%r)", exc)
        return None
    return write_batch(store_dir(), list(rows))


def open_prs(repo: str, runner: Runner = _default_runner) -> list[dict[str, Any]] | None:
    """Open PRs with the fields eligibility needs, or ``None`` when the read failed."""
    rc, out, err = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,headRefOid,isDraft,statusCheckRollup,title",
        ],
        timeout=120,
    )
    if rc != 0:
        logger.warning("external_review: could not list open PRs (%s)", err.strip())
        return None
    try:
        parsed = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        logger.warning("external_review: PR list unparseable (%r)", exc)
        return None
    return parsed if isinstance(parsed, list) else None


def ci_is_green(pr: dict[str, Any]) -> bool | None:
    """Whether every check on the PR has concluded successfully.

    ``None`` means UNKNOWN — an absent or unreadable rollup — which callers treat as
    not-eligible rather than as green. An empty rollup is unknown too, not success:
    a conflicting branch produces exactly that, and it is the state where CI has told
    us nothing at all.
    """
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return None
    for check in rollup:
        if not isinstance(check, dict):
            return None
        # A check run reports `conclusion`; a legacy commit status reports `state`.
        conclusion = (check.get("conclusion") or check.get("state") or "").upper()
        if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            continue
        return False
    return True


def eligibility(
    pr: dict[str, Any],
    *,
    already_reviewed: bool,
    comments_readable: bool,
    skip_drafts: bool,
    require_ci_green: bool,
) -> tuple[bool, str]:
    """Whether this PR should get a review now, and the REASON either way.

    Pure: every input is already resolved, so the policy is testable without a
    network. The reason string is returned for the eligible case too — an audit row
    saying only "dispatched" cannot answer "why this PR and not that one".
    """
    head = str(pr.get("headRefOid") or "").lower()
    if len(head) != 40:
        return False, "head oid missing or malformed"

    if not comments_readable:
        # Cannot tell whether this head was already reviewed. Repeating a review
        # costs shared subscription minutes and posts a duplicate report, so the
        # unknown resolves toward not spending.
        return False, "comments unreadable — cannot rule out a duplicate review"

    if already_reviewed:
        return False, f"already reviewed at head {head[:12]}"

    if skip_drafts and bool(pr.get("isDraft")):
        return False, "draft"

    if require_ci_green:
        green = ci_is_green(pr)
        if green is None:
            return False, "CI state unknown (no rollup, or a conflicting branch)"
        if not green:
            return False, "CI not green"

    return True, f"no report for head {head[:12]}"


def prefilter(
    pr: dict[str, Any],
    *,
    skip_drafts: bool,
    require_ci_green: bool,
    recently_dispatched: set[tuple[int, str]],
) -> tuple[bool, str]:
    """The checks that cost NOTHING, run before the ones that cost an API call.

    Ordering is a real property here, not tidiness. The comment read is a PAGINATED
    call per pull request, and the fields these checks need — ``isDraft``,
    ``statusCheckRollup`` — already arrived with the PR list. Reading comments first
    meant ~1700 paginated calls a day against a shared rate limit on a 72-PR queue,
    to reach an answer the free fields had already settled. MEASURED: 80 comment
    calls in one scan where the draft and CI filters alone would have ended it.

    Also the home of the in-flight suppression, because it must precede the PR-side
    check: a dispatch that is still running has produced no report yet, so asking the
    pull request would say "never reviewed" and dispatch again.
    """
    head = str(pr.get("headRefOid") or "").lower()
    number = pr.get("number")
    if len(head) != 40:
        return False, "head oid missing or malformed"

    if skip_drafts and bool(pr.get("isDraft")):
        return False, "draft"

    if require_ci_green:
        green = ci_is_green(pr)
        if green is None:
            return False, "CI state unknown (no rollup, or a conflicting branch)"
        if not green:
            return False, "CI not green"

    if isinstance(number, int) and (number, head) in recently_dispatched:
        return False, f"dispatched recently for head {head[:12]} — may still be in flight"

    return True, "passed cheap filters"


def _consider(
    pr: dict[str, Any],
    *,
    slug: str,
    mode: str,
    cfg: dict[str, Any],
    runner: Runner,
    recent: set[tuple[int, str]] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Decide and (if eligible) dispatch one PR. Returns ``(audit_row, decision, reason)``.

    The single place a decision is made, so :func:`scan` and :func:`review_one`
    cannot drift apart in what they enforce — the budget is the only thing that
    differs between them, and it is applied by the caller.
    """
    number = int(pr["number"])
    block = config.orchestrator(cfg)
    workflow = config.workflow_name(cfg)
    head = str(pr.get("headRefOid") or "").lower()
    row: dict[str, Any] = {"pr": number, "repo": slug, "head": head[:40], "mode": mode}

    if not workflow:
        row["decision"] = DECISION_SKIPPED
        row["reason"] = "no permitted workflow configured"
        return row, DECISION_SKIPPED, row["reason"]
    row["workflow"] = workflow

    # CHEAP FIRST — see prefilter's docstring. Most pull requests end here, having
    # cost nothing beyond the list call that fetched them.
    passed, reason = prefilter(
        pr,
        skip_drafts=bool(cfg.get("skip_drafts", True)),
        require_ci_green=bool(cfg.get("require_ci_green", True)),
        recently_dispatched=recent if recent is not None else set(),
    )
    if not passed:
        row["decision"] = DECISION_SKIPPED
        row["reason"] = reason
        return row, DECISION_SKIPPED, reason

    bodies = _pr_comment_bodies(number, slug, runner)
    already = bool(
        bodies is not None
        and report_mentions_head(bodies, marker=str(block.get("report_marker") or ""), head=head)
    )
    ok, reason = eligibility(
        pr,
        already_reviewed=already,
        comments_readable=bodies is not None,
        skip_drafts=bool(cfg.get("skip_drafts", True)),
        require_ci_green=bool(cfg.get("require_ci_green", True)),
    )
    row["reason"] = reason
    if not ok:
        row["decision"] = DECISION_SKIPPED
        return row, DECISION_SKIPPED, reason

    decision, detail = dispatch(number, workflow=workflow, block=block, dry_run=(mode == "dry_run"))
    row["decision"] = decision
    row["detail"] = detail
    # Report the ELIGIBILITY reason, not the dispatch detail: "no report for head X"
    # is the fact an operator reads the log to learn, and "would run <workflow>"
    # restates the decision already in the column beside it. A FAILURE is the
    # exception — there the detail IS the reason.
    return row, decision, (detail if decision == DECISION_FAILED else reason)


def _preflight(cfg: dict[str, Any], mode: str) -> str | None:
    """The reasons a scan should not start at all, checked once, up front.

    Returns a detail string to report, or ``None`` to proceed. Checking here rather
    than per-PR means an unconfigured install reports ONE clean line instead of
    marking every open pull request as a failed dispatch.
    """
    if not config.workflow_name(cfg):
        return "no permitted workflow configured — nothing to dispatch"
    if mode == "live":
        block = config.orchestrator(cfg)
        if not orchestrator_binary(str(block.get("command") or "")):
            return "review orchestrator is not installed — nothing to dispatch"
        if not block.get("report_marker"):
            # Without a marker there is no way to recognise an existing report, so
            # every tick would re-review every pull request. Refuse rather than spend.
            return "orchestrator.report_marker is unset — cannot detect existing reports"
    return None


def review_one(
    pr: int,
    *,
    repo: str | None = None,
    runner: Runner = _default_runner,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consider exactly ONE pull request.

    A separate path from :func:`scan` on purpose. Reporting a single PR by filtering
    a whole-queue scan would let the run dispatch a DIFFERENT pull request and simply
    not mention it — the flag would describe something other than what happened.
    """
    cfg = config.load_config() if cfg is None else cfg
    mode = config.effective_mode(cfg)
    summary: dict[str, Any] = {"mode": mode, "dispatched": 0, "considered": 0, "decisions": []}
    if mode == "off":
        summary["detail"] = "disabled (config mode=off or kill switch set)"
        return summary

    blocked = _preflight(cfg, mode)
    if blocked:
        summary["detail"] = blocked
        return summary

    slug = repo or resolve_repo(runner)
    if not slug:
        summary["detail"] = "repo slug unresolved — not dispatching"
        return summary
    summary["repo"] = slug

    detail = pr_detail(pr, slug, runner)
    if detail is None:
        summary["detail"] = f"PR #{pr} unreadable — not dispatching"
        return summary

    # The budget binds HERE too. It is a cap on spend against a shared subscription,
    # not a property of the scan loop, so a path that ignored it would make an
    # explicit `max_dispatches_per_scan: 0` true on one entry point and false on the
    # other — and the config documents that an operator who writes 0 means it.
    if config.max_dispatches_per_scan(cfg) < 1:
        summary["detail"] = "per-scan budget is 0 — not dispatching"
        return summary

    summary["considered"] = 1
    row, decision, reason = _consider(
        detail,
        slug=slug,
        mode=mode,
        cfg=cfg,
        runner=runner,
        recent=recent_dispatch_heads(),
    )
    summary["decisions"].append((pr, decision, reason))
    if decision in (DECISION_DISPATCHED, DECISION_DRY_RUN):
        summary["dispatched"] = 1
    record([row])
    return summary


def pr_detail(pr: int, repo: str, runner: Runner = _default_runner) -> dict[str, Any] | None:
    """One PR's eligibility fields, or ``None`` when the read failed."""
    rc, out, err = runner(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "number,headRefOid,isDraft,statusCheckRollup,title",
        ]
    )
    if rc != 0:
        logger.warning("external_review: PR #%s unreadable (%s)", pr, err.strip())
        return None
    try:
        parsed = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        logger.warning("external_review: PR #%s payload unparseable (%r)", pr, exc)
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("number") else None


def scan(
    *,
    repo: str | None = None,
    runner: Runner = _default_runner,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find eligible open PRs and dispatch up to the budget. Never raises.

    Returns a summary dict for the CLI to render and for tests to assert on. Every
    decision — dispatched, dry-run or skipped-with-reason — reaches the audit store,
    because the trail is what stands in for the approval gate this path does not pass
    through, and a trail recording only the dispatches cannot show that the runner
    was being conservative rather than idle.
    """
    cfg = config.load_config() if cfg is None else cfg
    mode = config.effective_mode(cfg)
    summary: dict[str, Any] = {"mode": mode, "dispatched": 0, "considered": 0, "decisions": []}
    if mode == "off":
        summary["detail"] = "disabled (config mode=off or kill switch set)"
        return summary

    blocked = _preflight(cfg, mode)
    if blocked:
        summary["detail"] = blocked
        return summary

    slug = repo or resolve_repo(runner)
    if not slug:
        summary["detail"] = "repo slug unresolved — not dispatching"
        return summary
    summary["repo"] = slug

    prs = open_prs(slug, runner)
    if prs is None:
        summary["detail"] = "open-PR list unreadable — not dispatching"
        return summary

    budget = config.max_dispatches_per_scan(cfg)
    recent = recent_dispatch_heads()
    rows: list[dict[str, Any]] = []
    # ATTEMPTS, not successes. A dispatch that FAILS has still consumed a decision —
    # and if every dispatch fails (a malformed argv, a vanished binary) counting only
    # successes means the cap never binds, so a whole queue is attempted, one row per
    # PR is produced, and the audit writer then refuses the oversized batch OUTRIGHT.
    # MEASURED: 80 eligible PRs, budget 1, zero audit files written, exit 0 printing
    # "dispatched=0" — indistinguishable from a healthy quiet scan.
    attempts = 0

    for pr in prs:
        summary["considered"] += 1
        number = pr.get("number")
        if not isinstance(number, int):
            continue

        # Only pay for the comment read while budget remains: once the cap is spent
        # every remaining PR is skipped regardless, and a scan over a 60-PR queue
        # would otherwise make 60 API calls to reach the same answer.
        if attempts >= budget:
            rows.append(
                {
                    "pr": number,
                    "repo": slug,
                    "decision": DECISION_SKIPPED,
                    "reason": "per-scan budget spent",
                    "mode": mode,
                }
            )
            continue

        row, decision, reason = _consider(
            pr, slug=slug, mode=mode, cfg=cfg, runner=runner, recent=recent
        )
        rows.append(row)
        if decision != DECISION_SKIPPED:
            attempts += 1
        summary["decisions"].append((number, decision, reason))
        if decision in (DECISION_DISPATCHED, DECISION_DRY_RUN):
            summary["dispatched"] += 1

    record(compress_rows(rows))
    return summary
