"""AccountActivityMonitor — deterministic (no-LLM) watch for EXTERNAL GitHub
activity on the owner's repos.

Runs every ~2h from the surplus scheduler. It answers one question with `gh`
calls and no LLM: did a real, external human (not the owner, not a bot/org) act
on one of the owner's flagship repos — open a PR/issue, comment, or post a
discussion? Genuine external activity is recorded as a ``github_account_activity``
observation (the 6h digest campaign consumes these); a FIRST-TIME external
contributor (or, in C2a-2, a security advisory / account notice) additionally
pushes an immediate Telegram ping.

Design notes:
- **SIBLING to ``ReconGatherer``**, not a method on it — that class has a
  load-bearing no-push contract. This class pushes (like ``cc_update_analyzer``).
- **Pipeline is lazy-resolved at tick time** (``GenesisRuntime.instance()``):
  surplus init runs BEFORE outreach init, so the pipeline does not exist when
  this monitor is constructed. Its first tick is hours after boot.
- **run_gh_checked** (not ``run_gh``) so a failed poll is distinguishable from an
  empty one — a rate-limited poll must NOT advance the cursor (or a contributor
  who acted during the failed window is lost forever).
- **State:** per-repo cursor in a home-anchored JSON sidecar (mutable,
  must-never-expire — the ``pr_watch`` precedent); event-dedup + first-time via
  the observation-hash primitive (no new table).
- **Modes** (``github_steward_config``): ``off`` / ``observe`` (record, never
  ping — first-deploy default, seeds the seen-actor set) / ``live`` (ping).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from genesis.db.crud import observations
from genesis.env import genesis_home
from genesis.recon import github_steward_config as steward_cfg
from genesis.recon.gh_cli import run_gh, run_gh_checked

logger = logging.getLogger(__name__)

_SOURCE = "recon"
_ACTIVITY_TYPE = "github_account_activity"
_ACTOR_SEEN_TYPE = "github_actor_seen"
_GH_TIMEOUT = 20
_SIDECAR_VERSION = 1


@dataclass(frozen=True)
class ActivityResult:
    """Summary of one monitor tick."""

    mode: str = "off"
    checked_repos: int = 0
    new_events: int = 0
    pinged: int = 0
    errors: int = 0
    seeded: bool = False
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityEvent:
    repo: str
    kind: str  # "pr" | "issue" | "comment" | "discussion"
    node_id: str
    actor: str
    number: int | None
    title: str
    url: str
    updated_at: str


def _event_hash(repo: str, kind: str, node_id: str) -> str:
    return hashlib.sha256(f"{repo}:{kind}:{node_id}".encode()).hexdigest()[:32]


def _actor_hash(login: str) -> str:
    return hashlib.sha256(f"actor:{login.lower()}".encode()).hexdigest()[:32]


class AccountActivityMonitor:
    """Deterministic external-GitHub-activity watch. See module docstring."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._owner: str | None = None
        # login(lower) -> is_automation (Bot/Organization or denylisted). Cached
        # for the process lifetime so we hit users/{login} once per login ever.
        self._automation_cache: dict[str, bool] = {}

    # ── pipeline (lazy — outreach inits after surplus) ────────────────────
    def _pipeline(self):
        try:
            from genesis.runtime._core import GenesisRuntime

            return GenesisRuntime.instance().outreach_pipeline
        except Exception:
            logger.debug("github steward: outreach pipeline not resolvable", exc_info=True)
            return None

    # ── cursor sidecar (home-anchored; mutable, never-expiring state) ─────
    def _sidecar_path(self) -> Path:
        return genesis_home() / "github_steward" / "cursors.json"

    def _load_cursors(self) -> dict[str, str]:
        path = self._sidecar_path()
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                cur = data.get("cursors", {})
                return {k: v for k, v in cur.items() if isinstance(v, str)}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("github steward: cursor sidecar unreadable — treating as empty")
        return {}

    def _save_cursors(self, cursors: dict[str, str]) -> None:
        path = self._sidecar_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": _SIDECAR_VERSION, "cursors": cursors}
            # Atomic write (tmp + rename) — a torn cursor file would reprocess
            # or skip activity.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except Exception:
            logger.warning("github steward: failed to persist cursors", exc_info=True)

    # ── gh helpers ────────────────────────────────────────────────────────
    async def _resolve_owner(self) -> str | None:
        if self._owner:
            return self._owner
        ok, out = await run_gh_checked("gh", "api", "user", "--jq", ".login", timeout=_GH_TIMEOUT)
        if ok and out:
            self._owner = out.strip()
        return self._owner

    async def _resolve_flagship_repos(self, cfg: dict, owner: str) -> list[str]:
        """Config list (full ``owner/name``), or auto-select the owner's active
        public source repos, capped. No install-specific names ship in code."""
        pinned = steward_cfg.str_list(cfg, "flagship_repos")
        if pinned:
            # Normalize bare names to owner/name.
            return [r if "/" in r else f"{owner}/{r}" for r in pinned]
        cap = steward_cfg.knob_int(cfg, "auto_select_cap")
        days = steward_cfg.knob_int(cfg, "auto_select_days")
        # Fetch more than cap so the recency filter can still yield up to cap.
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"users/{owner}/repos?type=owner&sort=pushed&per_page={cap * 2}",
            "--jq",
            '.[] | select(.fork==false and .private==false) | "\\(.full_name)\\t\\(.pushed_at)"',
            timeout=_GH_TIMEOUT,
        )
        if not ok or not out:
            return []
        cutoff = datetime.now(UTC) - timedelta(days=days)
        selected: list[str] = []
        for line in out.splitlines():
            name, _, pushed = line.partition("\t")
            name, pushed = name.strip(), pushed.strip()
            if not name or not pushed:
                continue
            try:
                pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pushed_dt >= cutoff:
                selected.append(name)
            if len(selected) >= cap:
                break
        return selected

    async def _is_automation(self, login: str, denylist: set[str]) -> bool:
        """True for bots/orgs/denylisted logins (cached per login)."""
        key = login.lower()
        if key in self._automation_cache:
            return self._automation_cache[key]
        verdict = False
        if login.endswith("[bot]") or key in denylist:
            verdict = True
        else:
            out = await run_gh("gh", "api", f"users/{login}", "--jq", ".type", timeout=_GH_TIMEOUT)
            # Unresolved (empty on error) → treat as automation-unknown → NOT a
            # confirmed human, so we don't ping; recorded as observation only.
            verdict = out.strip() in ("Bot", "Organization")
            if not out.strip():
                # Don't cache an unresolved lookup — retry next sighting.
                return True
        self._automation_cache[key] = verdict
        return verdict

    # ── main tick ─────────────────────────────────────────────────────────
    async def gather(self) -> ActivityResult:
        mode = steward_cfg.effective_mode()
        if mode == "off":
            return ActivityResult(mode="off")

        cfg = steward_cfg.load_config()
        owner = await self._resolve_owner()
        if not owner:
            logger.warning("github steward: could not resolve owner login — skipping tick")
            return ActivityResult(mode=mode, errors=1)

        repos = await self._resolve_flagship_repos(cfg, owner)
        if not repos:
            logger.info("github steward: no flagship repos resolved — nothing to poll")
            return ActivityResult(mode=mode)

        denylist = {d.lower() for d in steward_cfg.str_list(cfg, "automation_denylist")}
        max_events = steward_cfg.knob_int(cfg, "max_events_per_tick")
        cursors = self._load_cursors()
        now_iso = datetime.now(UTC).isoformat()
        result_errors = 0
        new_events = 0
        pinged = 0
        baselined = 0
        details: list[str] = []

        for repo in repos:
            since = cursors.get(repo)
            ok, events = await self._poll_repo(repo, since, owner)
            if not ok:
                # Failed poll — do NOT advance the cursor (never lose a
                # contributor to a rate-limited/timed-out window).
                result_errors += 1
                details.append(f"{repo}: poll error (cursor held)")
                continue

            # Per-repo first sight (no cursor yet) → baseline SILENTLY: seed
            # seen-actors, no records/pings, cursor := now. Per-repo, NOT global:
            # a repo newly entering the auto-select set / newly pinned must
            # baseline too, else its whole history replays as "new" (ping storm).
            if since is None:
                seeded = await self._seed_actors(events)
                cursors[repo] = now_iso
                baselined += 1
                details.append(f"{repo}: baselined ({seeded} actors seeded)")
                continue

            # Process oldest-first, capped.
            events.sort(key=lambda e: e.updated_at)
            truncated = len(events) > max_events
            if truncated:
                logger.warning(
                    "github steward: %s had %d events (cap %d) — processing oldest "
                    "%d; the rest are deferred to the next tick",
                    repo,
                    len(events),
                    max_events,
                    max_events,
                )
            processed = events[:max_events]
            for ev in processed:
                if ev.actor.lower() == owner.lower():
                    continue
                if await self._is_automation(ev.actor, denylist):
                    continue
                did_ping = await self._record_event(ev, mode)
                new_events += 1
                if did_ping:
                    pinged += 1
                    details.append(f"PING {repo}#{ev.number} by {ev.actor} ({ev.kind})")

            # Advance the cursor. GitHub's `since` is EXCLUSIVE ("updated after"),
            # so the cursor must never move past — nor, on a truncation that
            # splits a same-second group, even TO — an unprocessed event's ts (its
            # deferred twin would then never satisfy `after` and be lost). Deferred
            # events re-fetch next tick; dedup makes any overlap a no-op.
            if processed:
                if not truncated:
                    cursors[repo] = processed[-1].updated_at or since
                else:
                    boundary = events[max_events].updated_at  # first deferred event
                    safe = [
                        e.updated_at
                        for e in processed
                        if e.updated_at and boundary and e.updated_at < boundary
                    ]
                    # If every processed event ties the boundary second, hold the
                    # old cursor (re-fetch the window next tick — loud, never drops).
                    cursors[repo] = safe[-1] if safe else since

        self._save_cursors(cursors)
        return ActivityResult(
            mode=mode,
            checked_repos=len(repos),
            new_events=new_events,
            pinged=pinged,
            errors=result_errors,
            seeded=baselined > 0,
            details=details,
        )

    async def _poll_repo(
        self, repo: str, since: str | None, owner: str
    ) -> tuple[bool, list[ActivityEvent]]:
        """Poll one repo's issues/PRs, comments, and discussions since the cursor.

        Returns (ok, events). ok=False if ANY sub-poll errored (so the caller
        holds the cursor). Events are unsorted; the caller time-sorts + caps and
        derives the next cursor from the events it actually processes. No
        ``--paginate``: the cursor + 2h window bound each poll under one page.
        """
        events: list[ActivityEvent] = []
        since_q = f"since={since}&" if since else ""

        # 1. Issues + PRs (the /issues endpoint returns both; .pull_request marks PRs)
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"repos/{repo}/issues?{since_q}state=all&per_page=100&sort=updated&direction=asc",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return False, []
        for row in _parse_json_list(out):
            actor = (row.get("user") or {}).get("login", "")
            if not actor:
                continue
            kind = "pr" if row.get("pull_request") else "issue"
            events.append(
                ActivityEvent(
                    repo=repo,
                    kind=kind,
                    node_id=str(row.get("node_id") or row.get("id")),
                    actor=actor,
                    number=row.get("number"),
                    title=(row.get("title") or "")[:120],
                    url=row.get("html_url", ""),
                    updated_at=row.get("updated_at", ""),
                )
            )

        # 2. Issue + PR comments
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"repos/{repo}/issues/comments?{since_q}per_page=100&sort=updated&direction=asc",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return False, []
        for row in _parse_json_list(out):
            actor = (row.get("user") or {}).get("login", "")
            if not actor:
                continue
            events.append(
                ActivityEvent(
                    repo=repo,
                    kind="comment",
                    node_id=str(row.get("node_id") or row.get("id")),
                    actor=actor,
                    number=_issue_num(row.get("issue_url", "")),
                    title=(row.get("body") or "")[:120],
                    url=row.get("html_url", ""),
                    updated_at=row.get("updated_at", ""),
                )
            )

        # 3. Discussions (GraphQL — REST doesn't cover them)
        disc_ok, disc_events = await self._poll_discussions(repo, since)
        if not disc_ok:
            return False, []
        events.extend(disc_events)

        return True, events

    async def _poll_discussions(
        self, repo: str, since: str | None
    ) -> tuple[bool, list[ActivityEvent]]:
        owner, _, name = repo.partition("/")
        query = (
            "query($o:String!,$n:String!){repository(owner:$o,name:$n){"
            "discussions(first:25,orderBy:{field:UPDATED_AT,direction:DESC}){"
            "nodes{number title updatedAt id author{login}}}}}"
        )
        ok, out = await run_gh_checked(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"o={owner}",
            "-F",
            f"n={name}",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return False, []
        events: list[ActivityEvent] = []
        try:
            nodes = (
                json.loads(out)
                .get("data", {})
                .get("repository", {})
                .get("discussions", {})
                .get("nodes", [])
            )
        except Exception:
            return True, []  # malformed graphql payload — treat as no discussions
        for node in nodes:
            updated = node.get("updatedAt", "")
            if since and updated and updated <= since:
                continue
            actor = (node.get("author") or {}).get("login", "")
            if not actor:
                continue
            events.append(
                ActivityEvent(
                    repo=repo,
                    kind="discussion",
                    node_id=str(node.get("id")),
                    actor=actor,
                    number=node.get("number"),
                    title=(node.get("title") or "")[:120],
                    url="",
                    updated_at=updated,
                )
            )
        return True, events

    async def _seed_actors(self, events: list[ActivityEvent]) -> int:
        """First-run baseline: mark every actor seen (no pings, no records)."""
        seeded = 0
        for ev in events:
            h = _actor_hash(ev.actor)
            if not await observations.exists_by_hash(self._db, source=_SOURCE, content_hash=h):
                await observations.create(
                    self._db,
                    id=uuid.uuid4().hex,
                    source=_SOURCE,
                    type=_ACTOR_SEEN_TYPE,
                    content=f"seen:{ev.actor}",
                    priority="low",
                    created_at=datetime.now(UTC).isoformat(),
                    content_hash=h,
                    skip_if_duplicate=True,
                )
                seeded += 1
        return seeded

    async def _record_event(self, ev: ActivityEvent, mode: str) -> bool:
        """Record an external-activity observation; ping iff first-time + live.

        Returns True iff a ping was sent.
        """
        ev_hash = _event_hash(ev.repo, ev.kind, ev.node_id)
        # Dedup: already processed this exact event → nothing to do.
        if await observations.exists_by_hash(self._db, source=_SOURCE, content_hash=ev_hash):
            return False

        actor_h = _actor_hash(ev.actor)
        first_time = not await observations.exists_by_hash(
            self._db, source=_SOURCE, content_hash=actor_h
        )

        now = datetime.now(UTC).isoformat()
        summary = json.dumps(
            {
                "repo": ev.repo,
                "kind": ev.kind,
                "actor": ev.actor,
                "number": ev.number,
                "title": ev.title,
                "url": ev.url,
                "first_time": first_time,
            }
        )
        await observations.create(
            self._db,
            id=uuid.uuid4().hex,
            source=_SOURCE,
            type=_ACTIVITY_TYPE,
            content=summary,
            priority="medium" if first_time else "low",
            created_at=now,
            content_hash=ev_hash,
            skip_if_duplicate=True,
        )
        # Mark actor seen (first write wins; 90d TTL).
        if first_time:
            await observations.create(
                self._db,
                id=uuid.uuid4().hex,
                source=_SOURCE,
                type=_ACTOR_SEEN_TYPE,
                content=f"seen:{ev.actor}",
                priority="low",
                created_at=now,
                content_hash=actor_h,
                skip_if_duplicate=True,
            )

        # Priority ping: a first-time external contributor. Only in live mode.
        if first_time and mode == "live":
            return await self._ping(ev)
        return False

    async def _ping(self, ev: ActivityEvent) -> bool:
        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("github steward: pipeline unavailable — cannot ping %s", ev.actor)
            return False
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        verb = {
            "pr": "opened PR",
            "issue": "opened issue",
            "comment": "commented on",
            "discussion": "started discussion",
        }.get(ev.kind, "acted on")
        num = f"#{ev.number}" if ev.number else ""
        text = f"👋 First-time contributor: {ev.actor} {verb} {ev.repo}{num}\n{ev.title}".strip()
        if ev.url:
            text += f"\n{ev.url}"
        request = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            channel="telegram",
            # Unique per event so two distinct contributors don't dedup to one ping.
            topic=f"GitHub steward: {ev.actor} {ev.kind} {ev.repo}{num}",
            context=text,
            signal_type="github_account_activity",
            salience_score=0.9,
            verbatim=True,
        )
        try:
            await pipeline.submit_raw(text, request)
            logger.info("github steward: pinged first-time contributor %s on %s", ev.actor, ev.repo)
            return True
        except Exception:
            logger.error("github steward: ping failed for %s", ev.actor, exc_info=True)
            return False


# ── module helpers ────────────────────────────────────────────────────────
def _parse_json_list(out: str) -> list[dict]:
    if not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _issue_num(issue_url: str) -> int | None:
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1] if issue_url else ""
    return int(tail) if tail.isdigit() else None
