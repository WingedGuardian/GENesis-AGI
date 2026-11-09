"""One routing checkpoint for evidence writers, audits and external clients.

Receipt identity, existence, state and quoted content are mechanical checks.
Whether the quoted task addresses the concern, and whether disclosure is harmful,
remain explicit validator judgments recorded in the structured rationale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field

from genesis.session_awareness.pr_evidence import EvidenceDocument

logger = logging.getLogger(__name__)
_ISSUE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)"
    r"(?:#issuecomment-([1-9][0-9]*))?"
)


@dataclass(frozen=True)
class Concern:
    detail: str
    accepts_limit: bool = False


def concerns(doc: EvidenceDocument) -> dict[str, Concern]:
    """Enumerate every actionable outcome and every accepted-method decision."""
    result = {f"findings/{i}": Concern(f.summary) for i, f in enumerate(doc.findings)}
    for i, claim in enumerate(doc.claims):
        if claim.verdict != "pass" or claim.tier == "INFERRED":
            result[f"claims/{i}"] = Concern(
                claim.claim, claim.verdict == "unverified" or
                (claim.verdict == "pass" and claim.tier == "INFERRED")
            )
    for i, gap in enumerate(doc.gaps):
        result[f"gaps/{i}"] = Concern(gap.detail, gap.kind == "method")
    if not doc.deploy.established:
        result["deploy"] = Concern(doc.deploy.detail)
    if not doc.controls:
        result["controls"] = Concern("No negative control", accepts_limit=True)
    return result


@dataclass
class TrackingReport:
    pending: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.pending

    def as_dict(self) -> dict:
        return {"complete": self.complete, "pending": self.pending, "verified": self.verified}

    def note(self, previous: str | None = None) -> str:
        parts = ["TRACKING PENDING: " + "; ".join(self.pending), previous or ""]
        return " — ".join(p for p in parts if p)


def _gh_json(endpoint: str) -> dict:
    """Read a single resource; fixed host/method, argv only, no receipt text echoed."""
    try:
        # Same bound as gh_pr_metadata: an offline reader cannot hang a checkpoint.
        proc = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode:
            raise LookupError("GitHub receipt read failed")
        data = json.loads(proc.stdout)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        raise LookupError("GitHub receipt read unavailable or malformed") from None
    if not isinstance(data, dict):
        raise LookupError("GitHub receipt is not an object")
    return data


def public_receipt(url: str, repo: str, *, read=_gh_json) -> str:
    """Return actionable task text, never a PR or a comment on a different issue.

    API semantics: docs.github.com/en/rest/issues/{issues,comments}; issue
    responses include PRs (pull_request key), and comment.issue_url binds parent.
    """
    match = _ISSUE.fullmatch(url)
    if not match or match[1].lower() != repo.lower():
        raise LookupError("expected an issue/comment URL in the evidence repository")
    slug, number, comment = match.groups()
    metadata = read(f"repos/{slug}")
    if metadata.get("private") is not False:
        raise LookupError("destination is not a verified public repository")
    issue = read(f"repos/{slug}/issues/{number}")
    canonical = f"https://github.com/{slug}/issues/{number}"
    if (
        not _same_url(issue.get("html_url"), canonical)
        or "pull_request" in issue or issue.get("state") != "open"
    ):
        raise LookupError("destination must be an existing open issue, not a PR")
    body = issue.get("body")
    if comment:
        item = read(f"repos/{slug}/issues/comments/{comment}")
        parent = f"https://api.github.com/repos/{slug}/issues/{number}"
        if (
            not _same_url(item.get("issue_url"), parent)
            or not _same_url(item.get("html_url"), url)
        ):
            raise LookupError("comment does not belong to the referenced issue")
        body = item.get("body")
    if not isinstance(body, str):
        raise LookupError("receipt text is missing")
    return body


def _same_url(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and actual.lower() == expected.lower()


async def private_receipt(db, identifier: str) -> str:
    """Read one eligible, active private task by FULL id; never activate a writer."""
    from genesis.db.crud.follow_ups import PRIVATE_FOLLOW_UP_SQL

    if db is None:
        raise LookupError("private task reader unavailable in this client")
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise LookupError("private task requires a full follow-up id")
    async with db.execute(
        f"SELECT content, resolution_notes FROM follow_ups WHERE id = ? "  # noqa: S608 -- closed predicate
        f"AND {PRIVATE_FOLLOW_UP_SQL} AND kind = 'follow_up' "
        "AND status IN ('pending', 'in_progress', 'blocked')",
        (identifier,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise LookupError("private task is missing, inactive or ineligible")
    return "\n".join(value for value in row if isinstance(value, str))


async def assess_tracking(doc: EvidenceDocument, *, db=None, read_public=None) -> TrackingReport:
    """Fresh readback for each distinct reference; unavailable routing stays pending.

    Exact excerpts prove readback, not semantic relevance: the accompanying
    rationale owns coverage. Never promote a free-text Finding.disposition.
    """
    owed = concerns(doc)
    report = TrackingReport()
    entries = {}
    for entry in doc.tracking:
        entries.setdefault(entry.target, []).append(entry)
    for target in entries.keys() - owed.keys():
        report.pending.append(f"{target}: unknown or no longer outstanding target")
    cache = {}
    for target, concern in owed.items():
        items = entries.get(target, [])
        if len(items) != 1:
            report.pending.append(f"{target}: requires exactly one disposition (got {len(items)})")
            continue
        entry = items[0]
        if entry.kind == "accepted_limit":
            if concern.accepts_limit:
                report.verified.append(target + ": accepted method limit")
            else:
                report.pending.append(target + ": actionable work requires a task receipt")
            continue
        key = (entry.kind, entry.url if entry.kind == "public" else entry.id)
        if key not in cache:
            cache[key] = await _read_receipt(entry, doc.repo, db, read_public)
        body = cache[key]
        if isinstance(body, LookupError):
            report.pending.append(f"{target}: {body}")
        elif entry.quote not in body:
            report.pending.append(f"{target}: quoted concern is absent from the live task")
        else:
            report.verified.append(target + ": " + key[1])
    return report


async def _read_receipt(entry, repo: str, db, read_public) -> str | LookupError:
    try:
        if entry.kind == "public":
            body = await asyncio.to_thread(read_public or public_receipt, entry.url, repo)
        else:
            body = await private_receipt(db, entry.id)
        if not isinstance(body, str):
            raise LookupError("receipt text is unavailable")
        return body
    except LookupError as exc:
        return exc
    except Exception:
        # Schema/connection errors must not discard a valid measurement or send
        # private task contents/DB paths into client output.
        logger.warning("Verification receipt reader failed", exc_info=True)
        return LookupError("receipt reader failed; readback is unconfirmed")
