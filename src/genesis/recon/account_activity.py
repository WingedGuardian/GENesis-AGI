"""AccountActivityMonitor — deterministic (no-LLM) watch for EXTERNAL GitHub
activity on the owner's repos.

Runs every ~2h from the surplus scheduler. It answers one question with `gh`
calls and no LLM: did a real, external human (not the owner, not a bot/org) act
on one of the owner's flagship repos — open a PR/issue, comment, post a
discussion, or reply on one? Genuine external activity is recorded as a
``github_account_activity`` observation (the 6h digest campaign consumes these);
a FIRST-TIME external contributor additionally pushes an immediate Telegram ping.

Design notes:
- **SIBLING to ``ReconGatherer``**, not a method on it — that class has a
  load-bearing no-push contract. This class pushes (like ``cc_update_analyzer``).
- **Pipeline is lazy-resolved at tick time** (``GenesisRuntime.instance()``):
  surplus init runs BEFORE outreach init, so the pipeline does not exist when
  this monitor is constructed. Its first tick is hours after boot.
- **run_gh_checked** (not ``run_gh``) so a failed poll is distinguishable from an
  empty one — a rate-limited poll must NOT advance the cursor (or a contributor
  who acted during the failed window is lost forever).
- **created_at, not updated_at.** GitHub's ``?since=`` filters on *updated_at*, so
  an old issue that is merely edited/closed/labeled re-surfaces and would be
  mis-recorded as new activity by its ORIGINAL author. We key everything on the
  immutable ``created_at`` and filter ``cursor < created_at <= watermark`` — so
  only genuine new contributions count, each with the correct actor.
- **Watermark cursor.** ``wm`` is captured BEFORE polling; the cursor advances to
  ``wm`` (not the newest event's ts), so an event created mid-poll — after one
  feed's request but before another's — is never skipped (it re-fetches next
  tick; ``since`` is exclusive and event-dedup makes the overlap a no-op).
- **State:** per-repo cursor in a home-anchored JSON sidecar (mutable,
  must-never-expire — the ``pr_watch`` precedent); event-dedup + first-contact via
  the observation-hash primitive (no new table). A non-delivered first-time ping
  is held in a ``github_ping_pending`` marker and retried each tick until it
  lands, so a failed delivery never silently burns the first-contact signal.
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
from genesis.recon.gh_cli import run_gh_checked

logger = logging.getLogger(__name__)

_SOURCE = "recon"
_ACTIVITY_TYPE = "github_account_activity"
_ACTOR_SEEN_TYPE = "github_actor_seen"
_PENDING_TYPE = "github_ping_pending"
_GH_TIMEOUT = 20
_SIDECAR_VERSION = 1
# Reserved sidecar cursor key for the account-level notifications lane. Safe as a
# sibling of the per-repo cursors: a real repo key is always "owner/name" (has a
# "/"), so this "/"-less sentinel can never collide with one, and gather() only
# ever reads cursors for keys in the resolved repo list — never this one.
_NOTIF_CURSOR_KEY = "__notifications__"


def _now_z() -> str:
    """UTC now as a ``Z``-suffixed second-precision ISO timestamp — the same
    shape GitHub returns, so cursor/created_at lexical compares are chronological.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_ts(v: str) -> str:
    """Normalize a stored timestamp to ``Z`` form. Legacy cursors were written
    via ``isoformat()`` (``+00:00`` suffix), which sorts WRONG against GitHub's
    ``Z`` timestamps (``'Z' > '+'``); normalize so comparisons stay correct."""
    if v.endswith("+00:00"):
        return v[:-6] + "Z"
    return v


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
    kind: str  # "pr" | "issue" | "comment" | "discussion" | "discussion_comment"
    node_id: str
    actor: str
    number: int | None
    title: str
    url: str
    created_at: str


def _event_hash(repo: str, kind: str, node_id: str) -> str:
    return hashlib.sha256(f"{repo}:{kind}:{node_id}".encode()).hexdigest()[:32]


def _actor_hash(login: str) -> str:
    return hashlib.sha256(f"actor:{login.lower()}".encode()).hexdigest()[:32]


def _pending_hash(login: str) -> str:
    """Hash namespace for the retry marker — DISTINCT from ``_actor_hash`` so a
    still-pending actor is never mistaken for an already-seen one (a shared hash
    would make the first-contact check treat "ping owed" as "already told")."""
    return hashlib.sha256(f"pending:{login.lower()}".encode()).hexdigest()[:32]


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
                return {k: _norm_ts(v) for k, v in cur.items() if isinstance(v, str)}
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

    async def _is_automation(self, login: str, denylist: set[str]) -> bool | None:
        """True for bots/orgs/denylisted logins, False for confirmed humans, or
        None when the human/bot verdict can't be resolved (the ``users/{login}``
        lookup failed). The caller must NOT drop a None-verdict event — it holds
        the cursor so a transient API failure never loses a possible human. Only
        confirmed verdicts are cached (per login); an unresolved lookup retries
        next tick."""
        key = login.lower()
        if key in self._automation_cache:
            return self._automation_cache[key]
        if login.endswith("[bot]") or key in denylist:
            self._automation_cache[key] = True
            return True
        # run_gh_checked (not run_gh) so a FAILED lookup is distinguishable from a
        # resolved-but-non-bot type — a rate-limited/timed-out classification must
        # not masquerade as "confirmed automation" and silently drop the event.
        ok, out = await run_gh_checked(
            "gh", "api", f"users/{login}", "--jq", ".type", timeout=_GH_TIMEOUT
        )
        if not ok or not out.strip():
            return None  # unresolved — do NOT cache, do NOT drop; retry next tick
        verdict = out.strip() in ("Bot", "Organization")
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
            # No flagship repos — the repo loop below no-ops, but the account-level
            # notifications lane is INDEPENDENT of flagship repos and still runs.
            logger.info("github steward: no flagship repos resolved — notifications-only tick")

        denylist = {d.lower() for d in steward_cfg.str_list(cfg, "automation_denylist")}
        max_events = steward_cfg.knob_int(cfg, "max_events_per_tick")
        cursors = self._load_cursors()
        # Watermark: captured BEFORE any poll. Every cursor advances to this, so
        # an event created between two feeds' requests can't be skipped.
        wm = _now_z()
        result_errors = 0
        new_events = 0
        pinged = 0
        baselined = 0
        details: list[str] = []

        # Deliver any owed retry pings first (live only) — actor-global, so this
        # runs independent of which repo produced them.
        if mode == "live":
            try:
                redelivered = await self._drain_pending(mode)
                if redelivered:
                    pinged += redelivered
                    details.append(f"retry: delivered {redelivered} owed ping(s)")
            except Exception:
                logger.warning("github steward: pending-drain failed", exc_info=True)

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
            # seen-actors, no records/pings, cursor := wm. Per-repo, NOT global:
            # a repo newly entering the auto-select set / newly pinned must
            # baseline too, else its whole history replays as "new" (ping storm).
            if since is None:
                seeded = await self._seed_actors(events)
                cursors[repo] = wm
                baselined += 1
                details.append(f"{repo}: baselined ({seeded} actors seeded)")
                continue

            # created_at watermark window: strictly after the cursor (exclusive,
            # like GitHub's `since`), up to and including the watermark. This is
            # what turns an edited-old-item (old created_at) into a no-op and
            # defers a mid-poll event (created_at > wm) to the next tick.
            window = [e for e in events if e.created_at and since < e.created_at <= wm]
            window.sort(key=lambda e: e.created_at)
            truncated = len(window) > max_events
            if truncated:
                logger.warning(
                    "github steward: %s had %d in-window events (cap %d) — processing "
                    "oldest %d; the rest are deferred to the next tick",
                    repo,
                    len(window),
                    max_events,
                    max_events,
                )
            processed = window[:max_events]
            classify_failed = False
            for ev in processed:
                if ev.actor.lower() == owner.lower():
                    continue
                auto = await self._is_automation(ev.actor, denylist)
                if auto is None:
                    # Human/bot verdict unresolved (lookup failed) — NEVER drop a
                    # possible human. Flag the repo so its cursor holds and this
                    # event re-fetches next tick (dedup absorbs any overlap).
                    classify_failed = True
                    continue
                if auto:
                    continue
                did_ping = await self._record_event(ev, mode)
                new_events += 1
                if did_ping:
                    pinged += 1
                    details.append(f"PING {repo}#{ev.number} by {ev.actor} ({ev.kind})")

            # A repo with an unresolved classification holds its cursor entirely
            # (like a poll error): re-poll next tick when the lookup may succeed.
            # Already-recorded events dedup, already-pinged actors stay seen, so
            # re-processing is idempotent.
            if classify_failed:
                details.append(f"{repo}: classify unresolved — cursor held")
                continue

            # Advance the cursor. No truncation → advance to the watermark (the
            # whole window is done). Truncation → advance only to the newest
            # PROCESSED created_at that is STRICTLY BEFORE the first deferred
            # event's ts — because `since` is exclusive, landing ON a split
            # same-second group would strand the deferred twin. If every
            # processed event ties that boundary, hold the old cursor (re-fetch
            # the window next tick — loud, never drops).
            if not truncated:
                cursors[repo] = wm
            elif processed:
                boundary = window[max_events].created_at
                safe = [
                    e.created_at
                    for e in processed
                    if e.created_at and boundary and e.created_at < boundary
                ]
                cursors[repo] = safe[-1] if safe else since

        # ── Account-level notifications lane (repo-independent; shares wm) ──
        # Surfaces activity BEYOND the flagship repos: @mentions of the owner
        # anywhere + responses on the owner's OUTBOUND contributions (issues/PRs
        # they filed on others' repos). Own cursor under _NOTIF_CURSOR_KEY.
        notif = steward_cfg.notifications_cfg(cfg)
        if notif["enabled"]:
            n_since = cursors.get(_NOTIF_CURSOR_KEY)
            if n_since is None:
                # First run → baseline silently: adopt the watermark, do NOT
                # replay the existing inbox as pings (storm guard, like repos).
                cursors[_NOTIF_CURSOR_KEY] = wm
                details.append("notifications: baselined")
            else:
                nc, items = await self._poll_notifications(
                    owner, n_since, wm, notif["reasons"], denylist, max_events
                )
                for item in items:
                    did = await self._record_notification(item, mode)
                    new_events += 1
                    if did:
                        pinged += 1
                        details.append(
                            f"PING notif {item['repo']} ({item['reason']}) by {item['actor']}"
                        )
                # next_cursor: the watermark on a clean sweep, a boundary timestamp
                # on truncation (the newer rest defer to next tick), or None to HOLD
                # (a transient gh/classification error → re-poll the window next tick;
                # already-recorded items dedup).
                if nc is not None:
                    cursors[_NOTIF_CURSOR_KEY] = nc
                else:
                    details.append("notifications: cursor held (error)")

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
        holds the cursor). Events are unsorted and carry ``created_at``; the
        caller applies the ``cursor < created_at <= wm`` window, sorts + caps.
        Always ``--paginate``: the ``since`` window is usually one page, but a
        stale cursor (post-downtime) or a busy repo must never silently drop a
        second page of contributors.
        """
        events: list[ActivityEvent] = []
        since_q = f"since={since}&" if since else ""

        # 1. Issues + PRs (the /issues endpoint returns both; .pull_request marks PRs)
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"repos/{repo}/issues?{since_q}state=all&per_page=100&sort=updated&direction=desc",
            "--paginate",
            "--slurp",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return False, []
        for row in _parse_paged(out):
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
                    created_at=row.get("created_at", ""),
                )
            )

        # 2. Issue + PR comments
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"repos/{repo}/issues/comments?{since_q}per_page=100&sort=updated&direction=desc",
            "--paginate",
            "--slurp",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return False, []
        for row in _parse_paged(out):
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
                    created_at=row.get("created_at", ""),
                )
            )

        # 3. Discussions + their comments (GraphQL — REST doesn't cover them)
        disc_ok, disc_events = await self._poll_discussions(repo, since is None)
        if not disc_ok:
            return False, []
        events.extend(disc_events)

        return True, events

    async def _poll_discussions(
        self, repo: str, baseline: bool
    ) -> tuple[bool, list[ActivityEvent]]:
        """Discussions AND their comments, each as its own event with its own
        ``createdAt`` + author. The caller applies the created_at window, so a
        new external REPLY on an old discussion surfaces while an old discussion
        merely bumped by an owner reply does not.
        """
        owner, _, name = repo.partition("/")
        first = 100 if baseline else 25
        # Concatenate `first` (avoid f-string brace-escaping against GraphQL {}).
        query = (
            "query($o:String!,$n:String!){repository(owner:$o,name:$n){"
            "discussions(first:" + str(first) + ",orderBy:{field:UPDATED_AT,direction:DESC}){"
            "nodes{number title createdAt url id author{login} "
            "comments(last:20){nodes{id createdAt url author{login}}}}}}}"
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
            ) or []
        except Exception:
            return True, []  # malformed graphql payload — treat as no discussions
        for node in nodes:
            number = node.get("number")
            title = (node.get("title") or "")[:120]
            d_actor = (node.get("author") or {}).get("login", "")
            if d_actor:
                events.append(
                    ActivityEvent(
                        repo=repo,
                        kind="discussion",
                        node_id=str(node.get("id")),
                        actor=d_actor,
                        number=number,
                        title=title,
                        url=node.get("url", ""),
                        created_at=node.get("createdAt", ""),
                    )
                )
            for c in (node.get("comments") or {}).get("nodes", []) or []:
                c_actor = (c.get("author") or {}).get("login", "")
                if not c_actor:
                    continue
                events.append(
                    ActivityEvent(
                        repo=repo,
                        kind="discussion_comment",
                        node_id=str(c.get("id")),
                        actor=c_actor,
                        number=number,
                        title=title,
                        url=c.get("url", ""),
                        created_at=c.get("createdAt", ""),
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
                    created_at=_now_z(),
                    content_hash=h,
                    skip_if_duplicate=True,
                )
                seeded += 1
        return seeded

    async def _record_event(self, ev: ActivityEvent, mode: str) -> bool:
        """Record an external-activity observation; ping iff first-contact + live.

        First-contact is collapsed to the ACTOR across both the seen-marker and
        the pending-marker: a returning contributor OR a still-pending one never
        re-pings. Returns True iff a ping was DELIVERED this call.
        """
        ev_hash = _event_hash(ev.repo, ev.kind, ev.node_id)
        # Dedup: already processed this exact event → nothing to do.
        if await observations.exists_by_hash(self._db, source=_SOURCE, content_hash=ev_hash):
            return False

        actor_h = _actor_hash(ev.actor)
        pending_h = _pending_hash(ev.actor)
        # Contact owed unless we have already told them (seen) or currently have a
        # retry queued for them (pending). The seen-marker is checked across ALL
        # rows (permanent — a contributor never decays back to first-time, even
        # after its 90d row is TTL-resolved). The pending-marker is checked
        # UNRESOLVED-ONLY: once the daily TTL sweep resolves an abandoned (never
        # delivered) pending row, it must STOP suppressing — otherwise a resolved
        # row would read as "already contacted" forever and the contributor could
        # never be pinged again. An expired pending re-arms first-contact.
        seen = await observations.exists_by_hash(self._db, source=_SOURCE, content_hash=actor_h)
        pending = await observations.exists_by_hash(
            self._db, source=_SOURCE, content_hash=pending_h, unresolved_only=True
        )
        first_time = not (seen or pending)

        now = _now_z()
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
        # Always record the activity observation — the durable record the 6h
        # digest consumes (dedup by event hash).
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

        if not first_time:
            return False

        if mode != "live":
            # Observe: seed the seen-marker (no ping expected), so the eventual
            # live flip does not treat this actor as first-time.
            await self._mark_seen(ev.actor, now)
            return False

        # Live first-contact: attempt the ping.
        if await self._ping(ev):
            await self._mark_seen(ev.actor, now)
            return True

        # Not delivered — queue a durable retry (do NOT mark seen; the actor
        # stays owed a ping until one lands). Keyed by actor, so a second event
        # by the same actor while pending neither re-pings nor duplicates this.
        await observations.create(
            self._db,
            id=uuid.uuid4().hex,
            source=_SOURCE,
            type=_PENDING_TYPE,
            content=json.dumps(self._ping_payload(ev)),
            priority="medium",
            created_at=now,
            content_hash=pending_h,
            skip_if_duplicate=True,
        )
        return False

    async def _mark_seen(self, actor: str, now: str) -> None:
        await observations.create(
            self._db,
            id=uuid.uuid4().hex,
            source=_SOURCE,
            type=_ACTOR_SEEN_TYPE,
            content=f"seen:{actor}",
            priority="low",
            created_at=now,
            content_hash=_actor_hash(actor),
            skip_if_duplicate=True,
        )

    @staticmethod
    def _ping_payload(ev: ActivityEvent) -> dict:
        return {
            "repo": ev.repo,
            "kind": ev.kind,
            "node_id": ev.node_id,
            "actor": ev.actor,
            "number": ev.number,
            "title": ev.title,
            "url": ev.url,
        }

    async def _drain_pending(self, mode: str) -> int:
        """Re-attempt owed first-time pings. On delivery, mark the actor seen and
        resolve the pending marker; otherwise leave it for the next tick. Returns
        the count delivered this call."""
        rows = await observations.query(
            self._db, source=_SOURCE, type=_PENDING_TYPE, resolved=False, limit=100
        )
        delivered = 0
        for row in rows:
            try:
                payload = json.loads(row["content"])
            except Exception:
                continue
            actor = payload.get("actor", "")
            if not actor:
                continue
            pending_h = _pending_hash(actor)
            # Defensive: an actor seen via another path → resolve the stale
            # pending WITHOUT a duplicate ping.
            if await observations.exists_by_hash(
                self._db, source=_SOURCE, content_hash=_actor_hash(actor)
            ):
                await observations.resolve_by_content_hash(
                    self._db,
                    source=_SOURCE,
                    content_hash=pending_h,
                    resolved_at=_now_z(),
                    resolution_notes="actor already seen",
                )
                continue
            ev = ActivityEvent(
                repo=payload.get("repo", ""),
                kind=payload.get("kind", ""),
                node_id=str(payload.get("node_id", "")),
                actor=actor,
                number=payload.get("number"),
                title=payload.get("title", ""),
                url=payload.get("url", ""),
                created_at="",
            )
            if await self._ping(ev):
                await self._mark_seen(actor, _now_z())
                await observations.resolve_by_content_hash(
                    self._db,
                    source=_SOURCE,
                    content_hash=pending_h,
                    resolved_at=_now_z(),
                    resolution_notes="retry delivered",
                )
                delivered += 1
            # else: leave the pending row unresolved — retried next tick.
        return delivered

    async def _ping(self, ev: ActivityEvent) -> bool:
        """Send the first-time-contributor ping. Returns True ONLY on a confirmed
        DELIVERED result — a FAILED/IGNORED/REJECTED verdict (submit_raw does not
        raise on these) must not count as delivered."""
        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("github steward: pipeline unavailable — cannot ping %s", ev.actor)
            return False
        from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

        verb = {
            "pr": "opened PR",
            "issue": "opened issue",
            "comment": "commented on",
            "discussion": "started discussion",
            "discussion_comment": "replied on discussion",
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
            result = await pipeline.submit_raw(text, request)
        except Exception:
            logger.error("github steward: ping failed for %s", ev.actor, exc_info=True)
            return False
        if result is not None and result.status == OutreachStatus.DELIVERED:
            logger.info("github steward: pinged first-time contributor %s on %s", ev.actor, ev.repo)
            return True
        logger.warning(
            "github steward: ping to %s not delivered (status=%s) — will retry",
            ev.actor,
            getattr(result, "status", None),
        )
        return False

    # ── account-level notifications lane ──────────────────────────────────
    async def _poll_notifications(
        self,
        owner: str,
        since: str,
        wm: str,
        reasons: set[str],
        denylist: set[str],
        max_events: int,
    ) -> tuple[str | None, list[dict]]:
        """Poll the account notifications feed for the configured ``reason``s.

        Returns ``(next_cursor, items)``. ``next_cursor`` is the value to store:
        the watermark ``wm`` on a clean full sweep; the last-examined item's
        timestamp on truncation (the newer rest defer to the next tick); or
        ``None`` to HOLD the cursor (a transient gh/classification error — the
        window re-polls next tick and already-recorded ``items`` dedup by hash).
        ``items`` are the external-human notifications to record — owner/bot
        actors, out-of-window items, and (unsupported) Discussion subjects are
        filtered out here.
        """
        ok, out = await run_gh_checked(
            "gh",
            "api",
            f"notifications?all=true&since={since}&per_page=100",
            "--paginate",
            "--slurp",
            timeout=_GH_TIMEOUT,
        )
        if not ok:
            return None, []  # feed poll failed → hold the cursor

        # Candidate window: reason + updated_at window + owner-repo filter for
        # author/subscribed. Sorted OLDEST-first so a truncating cap makes forward
        # progress (process the oldest, advance to that boundary, defer the newer).
        candidates: list[dict] = []
        for n in _parse_paged(out):
            reason = n.get("reason", "")
            if reason not in reasons:
                continue
            updated = n.get("updated_at", "")
            # updated_at window: strictly after the cursor (exclusive), up to wm.
            if not (updated and since < updated <= wm):
                continue
            repo_obj = n.get("repository") or {}
            repo = repo_obj.get("full_name", "")
            if not repo:
                continue
            repo_owner = (repo_obj.get("owner") or {}).get("login", "")
            # author/subscribed are only interesting on repos the owner does NOT
            # own — an owner-repo one is self-activity the deep-poll already has.
            if (
                reason in steward_cfg.OWNED_ONLY_NOTIFICATION_REASONS
                and repo_owner.lower() == owner.lower()
            ):
                continue
            candidates.append(
                {
                    "reason": reason,
                    "repo": repo,
                    "updated": updated,
                    "thread_id": str(n.get("id", "")),
                    "subject": n.get("subject") or {},
                }
            )
        candidates.sort(key=lambda c: c["updated"])
        truncated = len(candidates) > max_events
        if truncated:
            logger.warning(
                "github steward: %d in-window notifications (cap %d) — processing the "
                "oldest %d, deferring the newer rest to the next tick",
                len(candidates),
                max_events,
                max_events,
            )
        work = candidates[:max_events]

        # Per-item failures NEVER hold the account-level cursor (one bad item — a
        # deleted comment, a discussion, a malformed payload — must not freeze the
        # whole lane). Unresolvable/unsupported items are recorded digest-only
        # (ping=False) so nothing is silently lost; the cursor always advances.
        items: list[dict] = []
        for c in work:
            subject = c["subject"]
            base = {
                "repo": c["repo"],
                "reason": c["reason"],
                "thread_id": c["thread_id"],
                "updated_at": c["updated"],
                "number": _issue_num(subject.get("url", "")),
                "title": (subject.get("title") or "")[:120],
                "url": _api_to_html_url(subject.get("url", "")),
            }
            if subject.get("type") == "Discussion":
                # GraphQL-only actor — record for the digest, no ping (follow-up).
                items.append({**base, "actor": "", "ping": False})
                continue
            actor = await self._resolve_notification_actor(subject)
            if actor is None:
                items.append({**base, "actor": "", "ping": False})  # unresolved → digest-only
                continue
            if actor.lower() == owner.lower():
                # The owner is the resolved (latest) actor. For author/subscribed
                # that is self-activity on the owner's own thread → skip entirely.
                # For a mention it means the owner is merely the latest commenter —
                # keep the thread (digest-only), don't self-ping, don't drop it.
                if c["reason"] not in steward_cfg.OWNED_ONLY_NOTIFICATION_REASONS:
                    items.append({**base, "actor": "", "ping": False})
                continue
            auto = await self._is_automation(actor, denylist)
            if auto is True:
                continue  # bot / org (None → can't tell → surface rather than drop)
            items.append({**base, "actor": actor, "ping": True})

        # Advance the cursor. Clean sweep → wm. Truncation → the newest processed
        # timestamp STRICTLY BEFORE the first deferred item (tie-safe: advancing to
        # a same-second boundary would strand the deferred twin, since `since` is
        # exclusive); if the whole batch ties the boundary, hold at `since` and
        # re-poll next tick.
        if not truncated:
            return wm, items
        boundary = candidates[max_events]["updated"]
        safe = [c["updated"] for c in work if c["updated"] < boundary]
        return (safe[-1] if safe else since), items

    async def _resolve_notification_actor(self, subject: dict) -> str | None:
        """Best-effort resolve of the human behind a notification (the feed carries
        no actor inline). Tries ``latest_comment_url`` then ``subject.url``, reading
        ``.user.login``; a gh error or malformed JSON on one URL just falls through
        to the next. Returns the login, or ``None`` if unresolved — it NEVER signals
        a cursor hold, so a single unresolvable item (deleted comment, discussion,
        malformed payload) can't freeze the account-level lane. The login is a
        best-effort label: for a mention it is the notification's latest commenter,
        which may differ from the exact actor that triggered it."""
        for key in ("latest_comment_url", "url"):
            u = subject.get(key)
            if not u:
                continue
            ok, out = await run_gh_checked("gh", "api", u, timeout=_GH_TIMEOUT)
            if not ok:
                continue  # try the next URL — do NOT hold on a per-item error
            try:
                login = (json.loads(out).get("user") or {}).get("login")
            except Exception:
                login = None
            if login:
                return login
        return None

    async def _record_notification(self, item: dict, mode: str) -> bool:
        """Record a notification observation; ping immediately in ``live`` mode.

        Unlike :meth:`_record_event` this is NOT first-contact-gated — every
        distinct notification update (deduped by thread id + updated_at) is
        signal, and BOTH mentions and author-responses ping. A failed ping is not
        retried durably: the observation is always recorded, so the 6h digest is
        the fallback surface. Returns True iff a ping was DELIVERED this call.
        """
        ev_hash = _event_hash(
            item["repo"], "notification", f"{item['thread_id']}:{item['updated_at']}"
        )
        if await observations.exists_by_hash(self._db, source=_SOURCE, content_hash=ev_hash):
            return False
        content = json.dumps(
            {
                "repo": item["repo"],
                "kind": f"notification_{item['reason']}",
                "reason": item["reason"],
                "actor": item["actor"],
                "number": item["number"],
                "title": item["title"],
                "url": item["url"],
                "first_time": False,
            }
        )
        await observations.create(
            self._db,
            id=uuid.uuid4().hex,
            source=_SOURCE,
            type=_ACTIVITY_TYPE,
            content=content,
            priority="medium",
            created_at=_now_z(),
            content_hash=ev_hash,
            skip_if_duplicate=True,
        )
        # Ping only externally-attributable items (item["ping"]); Discussion /
        # unresolved-actor / owner-latest items are recorded digest-only.
        if mode == "live" and item.get("ping"):
            return await self._ping_notification(item)
        return False

    async def _ping_notification(self, item: dict) -> bool:
        """Immediate Telegram ping for a mention / outbound-contribution response.
        Returns True ONLY on a confirmed DELIVERED result."""
        pipeline = self._pipeline()
        if pipeline is None:
            logger.warning("github steward: pipeline unavailable — cannot ping notification")
            return False
        from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

        num = f"#{item['number']}" if item["number"] else ""
        if item["reason"] in ("mention", "team_mention"):
            lead = f"💬 {item['actor']} mentioned you in {item['repo']}{num}"
        else:  # author — a response to one of the owner's outbound contributions
            lead = f"📨 {item['actor']} responded on your {item['repo']}{num}"
        text = lead
        if item["title"]:
            text += f"\n{item['title']}"
        if item["url"]:
            text += f"\n{item['url']}"
        request = OutreachRequest(
            category=OutreachCategory.NOTIFICATION,
            channel="telegram",
            topic=f"GitHub steward: {item['actor']} {item['reason']} {item['repo']}{num}",
            context=text,
            signal_type="github_account_activity",
            salience_score=0.9,
            verbatim=True,
        )
        try:
            result = await pipeline.submit_raw(text, request)
        except Exception:
            logger.error("github steward: notification ping failed", exc_info=True)
            return False
        if result is not None and result.status == OutreachStatus.DELIVERED:
            logger.info(
                "github steward: pinged notification (%s) by %s on %s",
                item["reason"],
                item["actor"],
                item["repo"],
            )
            return True
        logger.warning(
            "github steward: notification ping not delivered (status=%s)",
            getattr(result, "status", None),
        )
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


def _parse_paged(out: str) -> list[dict]:
    """Flatten a ``gh api --paginate --slurp`` payload — an outer array whose
    elements are each page's array — into a flat list of rows. A plain (single,
    non-slurped) array is returned as-is, so this is safe on both shapes."""
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    flat: list[dict] = []
    for item in data:
        if isinstance(item, list):
            flat.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def _issue_num(issue_url: str) -> int | None:
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1] if issue_url else ""
    return int(tail) if tail.isdigit() else None


def _api_to_html_url(api_url: str) -> str:
    """Convert a notification subject's API URL to a browser URL. A PR subject's
    API URL uses ``/pulls/<n>`` where the browser path is ``/pull/<n>``; issues
    map straight through. Non-``/repos/`` shapes (e.g. discussions) degrade to the
    API URL unchanged."""
    if not api_url:
        return ""
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/", 1)
    return url.replace("/pulls/", "/pull/", 1)
