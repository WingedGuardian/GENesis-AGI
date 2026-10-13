#!/usr/bin/env python3
"""star_milestone_check.py — announce once when the public repo crosses a star
milestone, so a decision parked behind "revisit at N stars" actually wakes up.

WHY THIS EXISTS. A follow-up row whose revisit condition is "when we reach 200
stars" is a note, not a trigger: nothing in this system evaluates that sentence,
so the row waits to be REMEMBERED. The install has paid for that shape before —
work recorded only somewhere nobody reads back is work dropped. This is the
cheapest thing that turns the sentence into an event: one unauthenticated API
read a day, and an observation on the transition.

WHAT IT DELIBERATELY DOES NOT DO. It does not touch the follow-up row. An
earlier design flipped `work_state` from `blocked_on_trigger` to `ready` so the
row would enter the actionable queue by itself. That needed a new crud function
(`work_state` is set only at creation, and `kind` is DERIVED from it, so the two
must move together), and moving a row into the hot lane hands it to readers —
ego dispatch among them — whose behaviour was not traced. The observation names
the row instead. Announcing costs nothing if wrong; dispatching might not.

FAIL DIRECTIONS, each chosen rather than inherited:

  - Could not READ the count (network, rate limit, malformed JSON) -> exit
    non-zero, write nothing, touch no state. A run that verified nothing must
    not report success; the next day's run retries. The cost of a miss is one
    day, which is why this is allowed to be noisy rather than clever.
  - State file missing or corrupt -> treat as "nothing announced yet" and
    announce. A duplicate announcement is a mild annoyance; a SILENT miss is the
    failure this script exists to prevent, so the tie breaks toward announcing.
    `skip_if_duplicate` plus a stable content hash keeps that from becoming
    spam — the state file is the primary guard and the hash is the backstop.
  - Count read, no milestone crossed -> exit 0 having done nothing. The common
    case, and it must stay silent or the unit becomes noise nobody reads.

WHY UNAUTHENTICATED. A public repo's star count needs no token, and the
unauthenticated limit (60/hr) is three orders of magnitude above one call a day.
Requiring a token would add an auth failure mode and a secret to a unit that
needs neither — and `gh` is not guaranteed on the unit's PATH.

Exit codes: 0 = the count was read (whether or not anything was announced).
Non-zero = the count was NOT read, or an announcement was attempted and failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("star_milestone")

#: Crossing any of these announces once. Ascending, deduped, positive.
#: Overridable via GENESIS_STAR_MILESTONES (comma-separated) so an install can
#: care about a different number without editing shipped code.
_DEFAULT_MILESTONES = (200, 500, 1000, 2500, 5000, 10000)

#: The state file's name. It lives at the top level of the runtime home, beside
#: backup_status.json and bootstrap_manifest.json — see `_state_path`.
_STATE_NAME = "star_milestones.json"

_API = "https://api.github.com"
_TIMEOUT_S = 30


def _genesis_env():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from genesis import env  # noqa: PLC0415 — path must be set first

    return env


def _state_path() -> Path:
    """Where the announced milestones are remembered, resolved per call.

    Through `genesis_home()`, never a `~/.genesis` fixed at import: an install
    relocated with GENESIS_HOME would otherwise read another install's state —
    and stay silent about its own crossing — then overwrite that state.
    """
    return _genesis_env().genesis_home() / _STATE_NAME


def _milestones() -> list[int]:
    raw = os.environ.get("GENESIS_STAR_MILESTONES", "")
    if not raw.strip():
        return list(_DEFAULT_MILESTONES)
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            # A malformed override is a configuration error, not a reason to
            # silently fall back to defaults that mean something different.
            raise SystemExit(
                f"GENESIS_STAR_MILESTONES: {part!r} is not an integer"
            ) from None
        if n > 0:
            out.add(n)
    if not out:
        raise SystemExit("GENESIS_STAR_MILESTONES was set but named no positive value")
    return sorted(out)


def _slug() -> str | None:
    """owner/repo for the public repo, or None when this install has none.

    None is a CLEAN NO-OP, not a failure, and the distinction is load-bearing:
    bootstrap enables every shipped timer on every clone, so a fresh install
    with no `github.user` would otherwise run this unit into a daily failure
    that looks like a defect and trains its reader to ignore the unit. An
    install that has not said which repo it owns simply has nothing to watch.

    Note this is the ONLY path that returns without reading a count while still
    exiting 0 — every other silence is an error, because a run that could not
    read the count must never look like a run that found no news.
    """
    env = _genesis_env()

    owner = (env.github_user() or "").strip()
    repo = (env.github_public_repo() or "").strip()
    if owner and repo:
        return f"{owner}/{repo}"

    # FALL BACK TO THE REPO'S OWN REMOTES, because the config key is optional and
    # a watcher that hinges on an optional key is a watcher that silently does
    # nothing. MEASURED on the install this was built for: `github.user` was
    # unset (no `github:` section in genesis.yaml at all), so the very first live
    # run reported "nothing to watch" on a repo we had been pushing to all day.
    # Fifteen green unit tests did not catch that; running it once did.
    #
    # The remote to read is the one pointing at the PUBLIC repo — an install
    # whose `origin` is a private fork keeps a second remote for it, and the
    # fork's star count (or its 404) is not the milestone anyone parked work
    # behind. `github_public_repo` selects it by owner/name — see
    # `_slug_from_remotes` for why the name alone is not enough. Config still
    # wins when both keys are set, for the install that deliberately watches
    # something other than its own repo.
    return _slug_from_remotes(env.github_public_repo())


def _parse_github_slug(url: str) -> str | None:
    """owner/repo parsed from one remote URL, or None if it is not GitHub.

    Handles both URL shapes git uses — https://github.com/owner/repo(.git) and
    git@github.com:owner/repo(.git) — and refuses anything else rather than
    guessing, since a non-GitHub remote has no stargazers to read.
    """
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:",
                   "ssh://git@github.com/"):
        if url.startswith(prefix):
            path = url[len(prefix):]
            break
    else:
        return None
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def _slug_from_remotes(public_repo: str = "") -> str | None:
    """owner/repo for the public repo, resolved from this checkout's remotes.

    Every fetch URL is resolved to owner/name and compared, case-insensitively,
    against *public_repo*:

      - `owner/name` names one repository exactly. The remote carrying it
        supplies the spelling; if none does, the configured value is still the
        answer, since it fully names the repo.
      - A bare `name` (the shipped default) matches a fork too: GitHub forks
        keep the upstream name unless renamed, so `alice/GENesis-AGI` and the
        public repo are indistinguishable by name. One distinct repository with
        that name is the answer. TWO OR MORE is not resolved by guessing — not
        by remote order, not by preferring or avoiding `origin` — because
        nothing in the checkout says which one is public, and a wrong pick
        watches a fork for as long as the unit stays green. It returns None,
        the same clean no-op as an install that has not said which repo it
        owns (which is what it is), and logs a WARNING naming the candidates
        and the key that settles it.
      - No remote with that name: `origin`, then any other GitHub remote.

    None also when no remote parses.
    """
    wanted = public_repo.strip().strip("/")
    # An owner-qualified value is parsed through the same rule as a remote URL,
    # so `owner/name.git` or a stray path segment cannot match differently.
    qualified = (
        _parse_github_slug(f"https://github.com/{wanted}") if "/" in wanted else None
    )
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent),
             "remote", "-v"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return qualified
    if out.returncode != 0:
        return qualified
    fetch_slugs: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "(fetch)" not in line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        slug = _parse_github_slug(fields[1])
        if slug:
            fetch_slugs.setdefault(fields[0], slug)
    if qualified is not None:
        for slug in fetch_slugs.values():
            if _repo_key(slug) == _repo_key(qualified):
                return slug
        return qualified
    if not fetch_slugs:
        return None
    if wanted and "/" not in wanted:
        # casefold: GitHub repo names are case-insensitive, and a clone URL
        # keeps whatever casing it was typed with. Keyed by repository, so one
        # repo reached through two remotes (https and ssh) is one candidate.
        candidates: dict[str, str] = {}
        for slug in fetch_slugs.values():
            if slug.rsplit("/", 1)[-1].casefold() == wanted.casefold():
                candidates.setdefault(_repo_key(slug), slug)
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        if len(candidates) > 1:
            logger.warning(
                "remotes carry %d repositories named %s (%s) and no owner is "
                "configured, so which one is public is unknown; not guessing. "
                "Set github.user (or GENESIS_GITHUB_USER) to the public repo's "
                "owner to watch it.",
                len(candidates), wanted, ", ".join(sorted(candidates.values())),
            )
            return None
    if "origin" in fetch_slugs:
        return fetch_slugs["origin"]
    return next(iter(fetch_slugs.values()))


def _star_count(slug: str) -> int:
    # S310: scheme and host are the constants above; only the slug varies, and
    # it comes from this install's own config rather than from any request.
    req = urllib.request.Request(  # noqa: S310
        f"{_API}/repos/{slug}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "genesis-star-milestone"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        payload = json.load(resp)
    count = payload.get("stargazers_count")
    # `type(...) is int`, not isinstance: bool subclasses int, so `true` in a
    # malformed payload would otherwise pass as a count of 1 — a successful
    # silent exit for a count that was never read. Negatives are the same
    # shape of lie: they compare below every milestone.
    if type(count) is not int or count < 0:
        # An absent or non-integer field is an UNREAD count, never a zero.
        # Zero would compare below every milestone and read as "no news".
        raise ValueError(f"stargazers_count missing or not an int: {count!r}")
    return count


def _repo_key(slug: str) -> str:
    """One repository, one key. GitHub treats `Owner/Repo` and `owner/repo` as
    the same repo, and config and a remote URL can spell it differently — so
    every comparison and every persisted identity goes through this."""
    return slug.casefold()


def _observation_key(slug: str, milestone: int) -> str:
    """The seed of the observation's deterministic id and content_hash."""
    return f"star-milestone|{_repo_key(slug)}|{milestone}"


def _already_announced(slug: str, milestones: list[int]) -> set[int]:
    """The milestones already announced (or covered by an announcement).

    PER THRESHOLD, not a high-water mark. A single "highest announced" made
    every number below it read as announced — including one an operator adds
    to GENESIS_STAR_MILESTONES afterwards, which a fresh watcher at the same
    count would announce. The observation id was already keyed per milestone;
    the state now agrees with it.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()
    # Any shape other than the object `_remember` writes is corrupt state, and
    # corrupt state reads as "nothing announced" (see FAIL DIRECTIONS). Valid
    # JSON can still be the wrong shape — `[]`, `null`, `200` — and a crash on
    # it would repeat every day, since the file stays on disk.
    if not isinstance(data, dict):
        return set()
    # State belongs to a repository. A file written for a different slug — or
    # one written before slugs were recorded — must not suppress this repo's
    # milestones: repo A's announced 500 is no reason repo B at 200 stays silent.
    # The observation id and content_hash already key on the repo, so announcing
    # again here cannot double-post a milestone the database remembers.
    recorded = data.get("slug")
    if not isinstance(recorded, str) or _repo_key(recorded) != _repo_key(slug):
        return set()
    # `type(...) is int` for the same reason as `_star_count`: bool subclasses
    # int, and a negative is not a milestone anyone announced.
    if "announced" in data:
        members = data["announced"]
        # All or nothing. A list with one bad member is a file `_remember` did
        # not write, and trusting its valid part would trust a corrupt file.
        if not isinstance(members, list) or not all(
            type(m) is int and m > 0 for m in members
        ):
            return set()
        return set(members)
    # A file written before the per-threshold set carries only the high-water
    # mark. Read it as what it meant when written: everything at or below it
    # was announced — otherwise the upgrade re-fires every lower milestone.
    value = data.get("highest_announced")
    if type(value) is not int or value < 0:
        return set()
    return {m for m in milestones if m <= value}


def _remember(slug: str, announced: set[int], count: int) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from genesis.util.atomic import atomic_write_text  # noqa: PLC0415

    atomic_write_text(
        _state_path(),
        json.dumps(
            {
                "slug": slug,
                "announced": sorted(announced),
                # Kept beside the set for a human reading the file; the set is
                # what `_already_announced` trusts when both are present.
                "highest_announced": max(announced),
                "stars_at_announcement": count,
                "announced_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
    )


async def _announce(slug: str, milestone: int, count: int) -> bool:
    # One short-lived RW connection, matching repo_pulse_worker's shape — the
    # closest analogue in the tree, and the pattern a timer-driven script wants:
    # no pool, no runtime, nothing left open between daily runs.
    #
    # `genesis.db` exports no `get_db`; an earlier revision of this file imported
    # one and every unit test passed, because they all stubbed this function
    # whole. The first LIVE run is what surfaced it. That is why the acceptance
    # check below the tests is not optional here.
    #
    # The open goes through `connect_aiosqlite_rw`, never a raw
    # `aiosqlite.connect`: that factory is the database ADMISSION check, and it
    # refuses a database the integrity layer has quarantined — before the open
    # and again after it. A timer runs with nobody watching, which is exactly
    # the writer class that must not reach a quarantined file. The refusal
    # RAISES, so `main` exits non-zero and records nothing; tomorrow retries.
    # `existing_only` because a missing database is a wrong path, not one to
    # create empty and write a single row into.
    import aiosqlite  # noqa: PLC0415

    from genesis.db.connection import connect_aiosqlite_rw  # noqa: PLC0415
    from genesis.db.crud import observations  # noqa: PLC0415
    from genesis.env import genesis_db_path  # noqa: PLC0415

    content = (
        f"{slug} has passed {milestone} GitHub stars (now {count}). "
        f"Anything parked behind that number is now unblocked — check open "
        f"follow-ups whose revisit condition names a star count, and re-verify "
        f"the external requirement before acting on it, since an eligibility "
        f"rule can move while a trigger waits."
    )
    now = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(_observation_key(slug, milestone).encode()).hexdigest()
    obs_id = digest[:32]
    async with connect_aiosqlite_rw(genesis_db_path(), existing_only=True, timeout=10) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            created = await observations.create(
                db,
                id=obs_id,
                source="star_milestone_check",
                type="repo_milestone_reached",
                content=content,
                priority="high",
                created_at=now,
                category="repo",
                # Stable across runs, so a lost state file cannot produce a second
                # announcement of the same milestone.
                content_hash=digest,
                skip_if_duplicate=True,
            )
        except aiosqlite.IntegrityError:
            # The one refusal that means "announced before": the deterministic
            # id already exists but the dedup clause let the INSERT through,
            # because the prior announcement was RESOLVED manually (dedup
            # matches only resolved = 0 rows) and the primary key still rejects
            # the retry. A resolved milestone stays announced — resurrecting it
            # would re-fire a wake-up somebody dismissed.
            #
            # CONFIRM that is what happened rather than assume it. Any other
            # constraint refusal wrote nothing, and treating it as "announced"
            # would let `main` record a milestone that never was — a silent
            # miss, the one outcome worse than announcing twice.
            async with db.execute(
                "SELECT 1 FROM observations WHERE id = ?", (obs_id,)
            ) as cur:
                if await cur.fetchone() is None:
                    raise
            logger.info(
                "%s milestone %d was announced before and resolved; not re-announcing",
                slug, milestone,
            )
            return False
    return created is not None


def main() -> int:
    slug = _slug()
    if slug is None:
        logger.info("no public repo configured for this install; nothing to watch")
        return 0
    try:
        count = _star_count(slug)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.error("could not read the star count for %s: %s", slug, exc)
        return 1

    milestones = _milestones()
    announced = _already_announced(slug, milestones)
    crossed = [m for m in milestones if m <= count and m not in announced]
    if not crossed:
        logger.info(
            "%s at %d stars; nothing new crossed (announced: %s)",
            slug, count, sorted(announced) or "none",
        )
        return 0

    # Announce only the HIGHEST newly-crossed milestone. A repo that gains a
    # thousand stars between two runs should produce one observation, not four —
    # and the highest is the one whose parked work is most likely to matter.
    # The lower ones it passed are COVERED by that announcement and recorded
    # with it, so they do not fire one per day afterwards.
    top = crossed[-1]
    try:
        fired = asyncio.run(_announce(slug, top, count))
    except Exception as exc:  # noqa: BLE001 — the announcement IS the product
        logger.error("could not write the milestone observation: %s", exc)
        return 1

    _remember(slug, announced | {m for m in milestones if m <= top}, count)
    logger.info(
        "%s crossed %d stars (now %d) — observation %s",
        slug,
        top,
        count,
        "written" if fired else "already present",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
