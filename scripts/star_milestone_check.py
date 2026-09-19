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

#: Where the highest already-announced milestone is remembered. Top level of
#: ~/.genesis, beside backup_status.json and bootstrap_manifest.json.
_STATE_PATH = Path(os.path.expanduser("~/.genesis/star_milestones.json"))

_API = "https://api.github.com"
_TIMEOUT_S = 30


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
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from genesis import env  # noqa: PLC0415 — path must be set first

    owner = (env.github_user() or "").strip()
    repo = (env.github_public_repo() or "").strip()
    if owner and repo:
        return f"{owner}/{repo}"

    # FALL BACK TO THE REPO'S OWN REMOTE, because the config key is optional and
    # a watcher that hinges on an optional key is a watcher that silently does
    # nothing. MEASURED on the install this was built for: `github.user` was
    # unset (no `github:` section in genesis.yaml at all), so the very first live
    # run reported "nothing to watch" on a repo we had been pushing to all day.
    # Fifteen green unit tests did not catch that; running it once did.
    #
    # `origin` is the repo this checkout came from, which is the repo whose stars
    # anyone here would mean. Config still wins when both keys are set, for the
    # install that deliberately watches something other than its own origin.
    return _slug_from_origin()


def _slug_from_origin() -> str | None:
    """owner/repo parsed from `origin`, or None if it is absent or not GitHub.

    Handles both URL shapes git uses — https://github.com/owner/repo(.git) and
    git@github.com:owner/repo(.git) — and refuses anything else rather than
    guessing, since a non-GitHub remote has no stargazers to read.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent),
             "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    url = out.stdout.strip()
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
    if not isinstance(count, int):
        # An absent or non-integer field is an UNREAD count, never a zero.
        # Zero would compare below every milestone and read as "no news".
        raise ValueError(f"stargazers_count missing or not an int: {count!r}")
    return count


def _already_announced() -> int:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        value = data.get("highest_announced")
        return int(value) if isinstance(value, int) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _remember(milestone: int, count: int) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "highest_announced": milestone,
                "stars_at_announcement": count,
                "announced_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(_STATE_PATH)  # atomic; a torn state file reads as "announce again"


async def _announce(slug: str, milestone: int, count: int) -> bool:
    # One short-lived RW connection, matching repo_pulse_worker's shape — the
    # closest analogue in the tree, and the pattern a timer-driven script wants:
    # no pool, no runtime, nothing left open between daily runs.
    #
    # `genesis.db` exports no `get_db`; an earlier revision of this file imported
    # one and every unit test passed, because they all stubbed this function
    # whole. The first LIVE run is what surfaced it. That is why the acceptance
    # check below the tests is not optional here.
    import aiosqlite  # noqa: PLC0415

    from genesis.db.connection import DEFAULT_DB_PATH  # noqa: PLC0415
    from genesis.db.crud import observations  # noqa: PLC0415

    content = (
        f"{slug} has passed {milestone} GitHub stars (now {count}). "
        f"Anything parked behind that number is now unblocked — check open "
        f"follow-ups whose revisit condition names a star count, and re-verify "
        f"the external requirement before acting on it, since an eligibility "
        f"rule can move while a trigger waits."
    )
    now = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(str(DEFAULT_DB_PATH), timeout=10) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        created = await observations.create(
            db,
            id=hashlib.sha256(f"star-milestone|{slug}|{milestone}".encode()).hexdigest()[:32],
            source="star_milestone_check",
            type="repo_milestone_reached",
            content=content,
            priority="high",
            created_at=now,
            category="repo",
            # Stable across runs, so a lost state file cannot produce a second
            # announcement of the same milestone.
            content_hash=hashlib.sha256(f"star-milestone|{slug}|{milestone}".encode()).hexdigest(),
            skip_if_duplicate=True,
        )
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
    announced = _already_announced()
    crossed = [m for m in milestones if m <= count and m > announced]
    if not crossed:
        logger.info(
            "%s at %d stars; nothing new crossed (highest announced: %d)", slug, count, announced
        )
        return 0

    # Announce only the HIGHEST newly-crossed milestone. A repo that gains a
    # thousand stars between two runs should produce one observation, not four —
    # and the highest is the one whose parked work is most likely to matter.
    top = crossed[-1]
    try:
        fired = asyncio.run(_announce(slug, top, count))
    except Exception as exc:  # noqa: BLE001 — the announcement IS the product
        logger.error("could not write the milestone observation: %s", exc)
        return 1

    _remember(top, count)
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
