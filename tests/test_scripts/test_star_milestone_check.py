"""The star-milestone watcher: announce once on a crossing, stay silent otherwise.

WHAT THESE PIN, and why each one is here rather than assumed:

The watcher's whole value is that it fires EXACTLY ONCE per milestone and never
silently misses one. Both halves of that are failure modes with opposite fixes —
firing repeatedly is noise that gets muted, and missing a crossing turns the
parked work back into something waiting to be remembered, which is the state the
watcher exists to end. So the suite drives the transition, the repeat, and every
path that could swallow a crossing.

The other axis is the FAIL DIRECTION, which is not symmetric. A count that could
not be read must exit non-zero (a run that verified nothing must not look like a
run that found no news), while an install with no repo configured must exit ZERO
(bootstrap enables this timer on every clone, and a fresh one has nothing to
watch — a daily red unit there would be a defect this file ships, not a signal).

Install-agnostic: the network and the database are both stubbed, no real HTTP,
no real writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "scripts" / "star_milestone_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("_star_milestone_check", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_star_milestone_check"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the module at a scratch state file and stub its two side effects.

    Returns a dict the test reads back: every announcement the module attempted.
    """
    # The state file lives under GENESIS_HOME, resolved at call time.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.delenv("GENESIS_STAR_MILESTONES", raising=False)
    monkeypatch.setattr(mod, "_slug", lambda: "owner/repo")
    announced: list[tuple[int, int]] = []

    def _fake_announce(slug, milestone, count):
        announced.append((milestone, count))
        return True

    # `main` calls asyncio.run(_announce(...)); replace the whole call so the
    # test never needs a database or an event loop.
    monkeypatch.setattr(mod, "asyncio", type("A", (), {"run": staticmethod(lambda coro: coro)})())
    monkeypatch.setattr(mod, "_announce", _fake_announce)
    return {"announced": announced, "state": tmp_path / "star_milestones.json"}


def _at(monkeypatch, count: int) -> None:
    monkeypatch.setattr(mod, "_star_count", lambda slug: count)


# ---------------------------------------------------------------------------
# THE TRANSITION, AND THE REPEAT.
# ---------------------------------------------------------------------------


def test_crossing_announces_once_and_a_second_run_is_silent(wired, monkeypatch):
    """THE POINT. Firing every day would be noise that gets muted, and a muted
    watcher is the same as no watcher."""
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]

    # State persisted, so the next run has something to compare against.
    assert json.loads(wired["state"].read_text())["highest_announced"] == 200

    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)], "announced the same milestone twice"


def test_below_the_milestone_says_nothing(wired, monkeypatch):
    _at(monkeypatch, 199)
    assert mod.main() == 0
    assert wired["announced"] == []
    assert not wired["state"].exists(), "wrote state without announcing anything"


def test_exactly_on_the_boundary_counts_as_crossed(wired, monkeypatch):
    """`>=`, not `>`. 200 stars IS the 200 milestone, and an off-by-one here
    delays the wake-up until the next star arrives — which may be never."""
    _at(monkeypatch, 200)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 200)]


def test_a_jump_past_several_milestones_announces_only_the_highest(wired, monkeypatch):
    """A repo that gains a thousand stars between two runs should produce ONE
    observation, not four — and the highest is the one whose parked work is most
    likely to matter."""
    _at(monkeypatch, 2600)
    assert mod.main() == 0
    assert wired["announced"] == [(2500, 2600)]
    assert json.loads(wired["state"].read_text())["highest_announced"] == 2500


def test_the_next_milestone_after_one_already_announced_still_fires(wired, monkeypatch):
    """Announcing 200 must not disarm 500. The state records the HIGHEST
    announced, not 'done'."""
    _at(monkeypatch, 201)
    assert mod.main() == 0
    _at(monkeypatch, 501)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201), (500, 501)]


# ---------------------------------------------------------------------------
# FAIL DIRECTIONS. Not symmetric, and that asymmetry is the design.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        urllib.error.URLError("no route"),
        TimeoutError("timed out"),
        ValueError("stargazers_count missing or not an int: None"),
    ],
    ids=["network", "timeout", "malformed-payload"],
)
def test_an_unreadable_count_exits_nonzero_and_touches_nothing(wired, monkeypatch, boom):
    """A run that could not READ the count must not resemble a run that found no
    news. It exits non-zero, writes no state, and announces nothing — tomorrow
    retries from the same place."""

    def _raise(slug):
        raise boom

    monkeypatch.setattr(mod, "_star_count", _raise)
    assert mod.main() == 1
    assert wired["announced"] == []
    assert not wired["state"].exists()


@pytest.mark.parametrize("bad", [True, -5, "200", None], ids=["bool", "negative", "string", "missing"])
def test_a_count_that_is_not_a_plain_nonnegative_int_is_unread(wired, monkeypatch, bad):
    """`bool` subclasses `int`: `isinstance(True, int)` is True, so a malformed
    payload carrying `true` would pass an isinstance check as a count of 1 —
    a green run for a count that was never read. Negatives compare below every
    milestone the same way a zero would."""

    # Drive _star_count's own guard rather than stubbing it away: urlopen hands
    # back the malformed payload, and the real check is what must reject it.
    import io
    import urllib.request

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResp(json.dumps({"stargazers_count": bad}).encode()),
    )
    with pytest.raises(ValueError):
        mod._star_count("owner/repo")


def test_state_written_for_a_different_repo_does_not_suppress_this_one(wired, monkeypatch):
    """Repo A's announced 500 is no reason repo B at 200 stays silent. The state
    file records the slug it belongs to; a mismatch reads as no state."""
    wired["state"].write_text(json.dumps({"slug": "alice/old", "highest_announced": 500}))
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]
    assert json.loads(wired["state"].read_text())["slug"] == "owner/repo"


def test_an_unconfigured_install_is_a_clean_no_op(monkeypatch):
    """Bootstrap enables every shipped timer on EVERY clone. A fresh install has
    no github.user, and a daily failing unit there would be a defect we shipped,
    not a signal. Exit 0, announce nothing."""
    monkeypatch.setattr(mod, "_slug", lambda: None)
    called: list[str] = []
    monkeypatch.setattr(mod, "_star_count", lambda slug: called.append(slug) or 9999)
    assert mod.main() == 0
    assert called == [], "read the count for an install with no repo configured"


def test_a_corrupt_state_file_announces_rather_than_swallowing(wired, monkeypatch):
    """The tie breaks toward announcing. A duplicate observation is an
    annoyance; a silent miss is the failure this script exists to prevent — and
    the real duplicate guard is the stable content hash in `_announce`, not this
    file."""
    wired["state"].write_text("{not json at all")
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]


def test_a_failed_announcement_exits_nonzero_and_does_not_record_it(wired, monkeypatch):
    """If the observation could not be written, the milestone was NOT announced.
    Recording it anyway would mean the crossing is never announced at all —
    the one outcome worse than announcing twice."""

    def _boom(slug, milestone, count):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mod, "_announce", _boom)
    _at(monkeypatch, 201)
    assert mod.main() == 1
    assert not wired["state"].exists(), "recorded a milestone that was never announced"


# ---------------------------------------------------------------------------
# CONFIGURATION.
# ---------------------------------------------------------------------------


def test_milestones_can_be_overridden_and_are_sorted_and_deduped(monkeypatch):
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", "500, 100,100 , 250")
    assert mod._milestones() == [100, 250, 500]


def test_a_malformed_override_refuses_rather_than_falling_back(monkeypatch):
    """Silently reverting to defaults would watch a number the operator did not
    ask for, and they would never learn the override was ignored."""
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", "200,not-a-number")
    with pytest.raises(SystemExit):
        mod._milestones()


def test_an_empty_override_refuses_too(monkeypatch):
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", " , ,")
    with pytest.raises(SystemExit):
        mod._milestones()


def test_the_observation_type_is_permanent(monkeypatch):
    """The observation IS the wake-up. Under the 14-day default TTL it would
    expire before anyone acted on it, and the watcher would be a no-op nobody
    noticed. Pinned here because the type and the TTL policy live in different
    files, so nothing else would catch them drifting apart."""
    sys.path.insert(0, str(_REPO / "src"))
    from genesis.db.crud import observations

    assert "repo_milestone_reached" in observations._PERMANENT_TYPES
    assert "repo_milestone_reached" not in observations.INTERNAL_OBS_TYPES, (
        "a milestone the user never sees cannot wake anything up"
    )


# ---------------------------------------------------------------------------
# THE STATE FILE IS UNTRUSTED INPUT. Any shape other than the one `_remember`
# writes reads as "nothing announced" — the documented fail direction for a
# corrupt file — never as a crash that repeats every day.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["[]", "null", "200", "true", '"owner/repo"'],
    ids=["array", "null", "number", "bool", "string"],
)
def test_valid_json_that_is_not_an_object_announces_rather_than_crashing(
    wired, monkeypatch, raw
):
    """`json.loads` accepts these, and `.get` on them raises. The state stays on
    disk, so a crash here would repeat on every run and the crossing would
    never be announced — the silent miss this script exists to prevent."""
    wired["state"].write_text(raw)
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]


@pytest.mark.parametrize(
    "bad", [True, -1, "500", 2.5], ids=["bool", "negative", "string", "float"]
)
def test_a_highest_announced_that_is_not_a_plain_nonnegative_int_reads_as_zero(wired, bad):
    wired["state"].write_text(json.dumps({"slug": "owner/repo", "highest_announced": bad}))
    assert mod._already_announced("owner/repo", [200, 500]) == set()


@pytest.mark.parametrize(
    "bad",
    [[200, True], [200, -1], [200, "500"], [2.5], "200", {"200": 1}],
    ids=["bool", "negative", "string", "float", "not-a-list", "object"],
)
def test_an_announced_set_with_any_bad_member_reads_as_nothing_announced(wired, bad):
    """A partly-valid list is still a file `_remember` did not write. Reading
    the valid part would trust a corrupt file; the documented fail direction
    for corrupt state is to announce, with the database id as the backstop."""
    wired["state"].write_text(
        json.dumps({"slug": "owner/repo", "announced": bad, "highest_announced": 500})
    )
    assert mod._already_announced("owner/repo", [200, 500]) == set()


# ---------------------------------------------------------------------------
# ANNOUNCEMENT STATE IS PER THRESHOLD. The observation id is already keyed on
# the milestone; a single high-water mark in the state file made every number
# below it read as announced, including one configured after the fact.
# ---------------------------------------------------------------------------


def test_a_threshold_added_below_the_high_water_mark_still_announces(wired, monkeypatch):
    """500 announced under the defaults; the operator then adds 300. A fresh
    watcher at the same count would announce 300, so this one must too."""
    _at(monkeypatch, 600)
    assert mod.main() == 0
    assert wired["announced"] == [(500, 600)]

    monkeypatch.setenv("GENESIS_STAR_MILESTONES", "200,300,500")
    assert mod.main() == 0
    assert wired["announced"] == [(500, 600), (300, 600)]

    # And once announced, it is announced.
    assert mod.main() == 0
    assert wired["announced"] == [(500, 600), (300, 600)]


def test_milestones_jumped_over_are_not_announced_later(wired, monkeypatch):
    """The other half of the same contract. A jump announces only the highest
    crossing; the lower ones it passed are COVERED by that announcement, not
    queued behind it to fire one per day."""
    _at(monkeypatch, 2600)
    assert mod.main() == 0
    assert mod.main() == 0
    assert wired["announced"] == [(2500, 2600)]
    state = json.loads(wired["state"].read_text())
    assert state["announced"] == [200, 500, 1000, 2500]
    assert state["highest_announced"] == 2500


def test_a_legacy_high_water_state_covers_every_threshold_below_it(wired, monkeypatch):
    """A state file from before the per-threshold set carries only
    `highest_announced`. It must keep meaning what it meant when written —
    everything at or below it was announced — or the upgrade re-fires 200."""
    wired["state"].write_text(json.dumps({"slug": "owner/repo", "highest_announced": 500}))
    _at(monkeypatch, 600)
    assert mod.main() == 0
    assert wired["announced"] == []


# ---------------------------------------------------------------------------
# STATE LIVES UNDER GENESIS_HOME, resolved when it is used. An import-time
# `~/.genesis` path lets a relocated install read another install's state and
# stay silent about its own crossing.
# ---------------------------------------------------------------------------


def test_state_follows_genesis_home_not_the_default_tree(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    default_state = home / ".genesis" / "star_milestones.json"
    # Another install's state in the DEFAULT tree, for the same repository.
    default_state.write_text(json.dumps({"slug": "owner/repo", "highest_announced": 200}))
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(relocated))
    monkeypatch.delenv("GENESIS_STAR_MILESTONES", raising=False)

    # A fresh copy of the module, loaded with HOME pointed at tmp, so nothing
    # resolved at import time can reach the real home directory.
    fresh = _load()
    try:
        announced: list[tuple[int, int]] = []
        monkeypatch.setattr(fresh, "_slug", lambda: "owner/repo")
        monkeypatch.setattr(fresh, "_star_count", lambda slug: 201)
        monkeypatch.setattr(
            fresh, "asyncio", type("A", (), {"run": staticmethod(lambda coro: coro)})()
        )
        monkeypatch.setattr(
            fresh, "_announce", lambda s, m, c: announced.append((m, c)) or True
        )
        assert fresh.main() == 0
        assert announced == [(200, 201)], "read another install's state from the default tree"
        assert (relocated / "star_milestones.json").exists(), "state not written under GENESIS_HOME"
        assert json.loads(default_state.read_text()) == {
            "slug": "owner/repo", "highest_announced": 200
        }, "overwrote the default tree's state"
    finally:
        sys.modules["_star_milestone_check"] = mod


# ---------------------------------------------------------------------------
# THE SANDBOX MUST ADMIT THE PATHS THE SCRIPT RESOLVES. The database and the
# state file both follow env overrides (GENESIS_DB_PATH, GENESIS_HOME) that the
# unit cannot expand, so a fixed allow-list names the default tree only.
# ---------------------------------------------------------------------------


def _read_write_paths(template: str) -> set[str]:
    text = (_REPO / "scripts" / "systemd" / template).read_text(encoding="utf-8")
    out: set[str] = set()
    for line in text.splitlines():
        if line.startswith("ReadWritePaths="):
            out.update(line.split("=", 1)[1].split())
    return out


def test_the_unit_may_write_wherever_the_server_may_write_the_database():
    """genesis-server is the database's primary writer. Any database location
    it can write, this unit must be able to write too, or the first crossing on
    an install with a relocated database fails every day while the server is
    fine."""
    server = _read_write_paths("genesis-server.service.template")
    watcher = _read_write_paths("genesis-star-milestone.service.template")
    assert server, "genesis-server's allow-list was not found"
    assert server <= watcher, f"watcher allow-list {sorted(watcher)} is narrower than {sorted(server)}"


# ---------------------------------------------------------------------------
# REPOSITORY IDENTITY IS CASE-INSENSITIVE. GitHub treats `Owner/Repo` and
# `owner/repo` as one repository, and a clone URL keeps whichever casing it
# was typed with — so every place this script compares or keys on a slug must
# agree with GitHub, or one repository reads as two.
# ---------------------------------------------------------------------------


def _remotes(monkeypatch, text: str) -> None:
    class _Out:
        returncode = 0
        stdout = text

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Out())


def test_the_public_remote_is_found_whatever_casing_it_was_cloned_with(monkeypatch):
    """Private-fork topology: `origin` is the fork, a second remote carries the
    public repo in different casing. An exact compare misses it and watches the
    fork instead."""
    _remotes(
        monkeypatch,
        "origin\thttps://github.com/alice/private-fork.git (fetch)\n"
        "origin\thttps://github.com/alice/private-fork.git (push)\n"
        "public\thttps://github.com/wingedguardian/genesis-agi.git (fetch)\n"
        "public\thttps://github.com/wingedguardian/genesis-agi.git (push)\n",
    )
    assert mod._slug_from_remotes("GENesis-AGI") == "wingedguardian/genesis-agi"


# ---------------------------------------------------------------------------
# A FORK KEEPS THE UPSTREAM NAME BY DEFAULT. Matching on the repository name
# alone cannot tell `alice/GENesis-AGI` from the public repo, and the remote
# that sorts first would win. The match is on owner/name, and with no owner
# configured an ambiguous name is not resolved by guessing.
# ---------------------------------------------------------------------------

_FORK_AND_PUBLIC = (
    # `origin` (the fork) sorts before `upstream` (the public repo): git prints
    # remotes in name order, and a name-only match took the first it saw.
    "origin\thttps://github.com/alice/GENesis-AGI.git (fetch)\n"
    "origin\thttps://github.com/alice/GENesis-AGI.git (push)\n"
    "upstream\tgit@github.com:WingedGuardian/GENesis-AGI.git (fetch)\n"
    "upstream\tgit@github.com:WingedGuardian/GENesis-AGI.git (push)\n"
)


def test_an_owner_qualified_name_picks_the_public_remote_over_a_same_named_fork(monkeypatch):
    _remotes(monkeypatch, _FORK_AND_PUBLIC)
    assert mod._slug_from_remotes("wingedguardian/genesis-agi") == "WingedGuardian/GENesis-AGI"


def test_a_bare_name_matching_two_repositories_is_not_guessed(monkeypatch, caplog):
    """With no owner configured there is no fact that says which of the two is
    the public repo. Picking one watches the wrong repo for as long as the unit
    stays green; the documented answer for an install that has not said which
    repo it owns is the clean no-op, and the log names the key that resolves it."""
    _remotes(monkeypatch, _FORK_AND_PUBLIC)
    with caplog.at_level("WARNING", logger="star_milestone"):
        assert mod._slug_from_remotes("GENesis-AGI") is None
    assert "github.user" in caplog.text
    assert "alice/GENesis-AGI" in caplog.text and "WingedGuardian/GENesis-AGI" in caplog.text


def test_one_repository_reached_through_two_remotes_is_not_ambiguous(monkeypatch):
    """Same repo over https and ssh, typed in different casing, is ONE match."""
    _remotes(
        monkeypatch,
        "a\thttps://github.com/WingedGuardian/GENesis-AGI.git (fetch)\n"
        "b\tgit@github.com:wingedguardian/genesis-agi (fetch)\n",
    )
    assert mod._slug_from_remotes("GENesis-AGI") == "WingedGuardian/GENesis-AGI"


def test_a_single_same_named_remote_is_still_found_by_name(monkeypatch):
    """The topology the fallback was built for — `origin` only, no
    `github.user` — keeps working: one candidate is not a guess."""
    _remotes(
        monkeypatch,
        "origin\thttps://github.com/WingedGuardian/GENesis-AGI.git (fetch)\n"
        "origin\thttps://github.com/WingedGuardian/GENesis-AGI.git (push)\n",
    )
    assert mod._slug_from_remotes("GENesis-AGI") == "WingedGuardian/GENesis-AGI"


def test_an_owner_qualified_name_no_remote_carries_is_still_that_repository(monkeypatch):
    """owner/name fully names a repository; the remotes only confirm spelling.
    Falling back to `origin` here would watch a repo the config did not name."""
    _remotes(monkeypatch, "origin\thttps://github.com/alice/GENesis-AGI.git (fetch)\n")
    assert mod._slug_from_remotes("WingedGuardian/GENesis-AGI") == "WingedGuardian/GENesis-AGI"


def test_state_written_under_another_casing_of_the_same_repo_still_counts(wired):
    """Config spelling and remote spelling can differ for the SAME repository.
    Reading that as a different repo would re-announce a milestone already
    announced."""
    wired["state"].write_text(json.dumps({"slug": "Owner/Repo", "highest_announced": 200}))
    assert mod._already_announced("owner/repo", [200, 500]) == {200}


def test_the_observation_identity_does_not_depend_on_slug_casing():
    """The id and content_hash are the database-side duplicate guard. If they
    differ by casing, a slug spelled differently writes a SECOND observation
    for a milestone already announced."""
    assert mod._observation_key("Owner/Repo", 200) == mod._observation_key("owner/repo", 200)
    assert mod._observation_key("owner/repo", 200) != mod._observation_key("owner/repo", 500)


# ---------------------------------------------------------------------------
# THE WRITE GOES THROUGH DATABASE ADMISSION, against a real schema. These do
# not stub `_announce`: both earlier write-path defects in this file were
# invisible to tests that replaced it whole.
# ---------------------------------------------------------------------------


def _scratch_db(path: Path, *, abort_trigger: bool = False) -> None:
    import asyncio

    import aiosqlite

    sys.path.insert(0, str(_REPO / "src"))
    from genesis.db.schema._migrations import create_all_tables

    async def _build():
        async with aiosqlite.connect(str(path)) as db:
            await create_all_tables(db)
            if abort_trigger:
                # A constraint refusal that is NOT the deterministic primary key.
                await db.execute(
                    "CREATE TRIGGER refuse_obs BEFORE INSERT ON observations "
                    "BEGIN SELECT RAISE(ABORT, 'refused by test'); END"
                )
            await db.commit()

    asyncio.run(_build())


def _obs_ids(path: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM observations WHERE source = 'star_milestone_check'"
            ).fetchall()
        ]
    finally:
        conn.close()


@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """A real-schema database the script resolves as ITS database, with the
    quarantine marker home isolated to tmp."""
    db = tmp_path / "data" / "genesis.db"
    db.parent.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    sys.path.insert(0, str(_REPO / "src"))
    import genesis.db.connection as conn_mod
    import genesis.env as env_mod

    # The seam the script resolves at call time (the suite's autouse isolation
    # already points it at tmp; this names THIS test's database).
    monkeypatch.setattr(env_mod, "genesis_db_path", lambda: db)
    # An opener resolved at import time must not escape to a real database.
    monkeypatch.setattr(conn_mod, "DEFAULT_DB_PATH", db)
    return db


def test_announce_writes_one_row_on_an_unfenced_database(scratch_db):
    """CONTROL for the quarantine test below: the real write path writes here,
    so a refusal there is the fence, not a broken fixture."""
    import asyncio

    _scratch_db(scratch_db)
    assert asyncio.run(mod._announce("owner/repo", 200, 201)) is True
    assert len(_obs_ids(scratch_db)) == 1


def test_announce_refuses_a_quarantined_database(scratch_db, tmp_path):
    """A database that failed an integrity check takes no write from a timer
    that runs with nobody watching. The refusal must RAISE, so `main` exits
    non-zero and records nothing — tomorrow retries."""
    import asyncio

    from genesis.db.integrity import DatabaseIntegrityError, quarantine_database

    _scratch_db(scratch_db)
    (tmp_path / ".genesis").mkdir(exist_ok=True)
    quarantine_database(scratch_db, source="test", detail="unit")
    with pytest.raises(DatabaseIntegrityError):
        asyncio.run(mod._announce("owner/repo", 200, 201))
    assert _obs_ids(scratch_db) == [], "wrote to a quarantined database"


def test_a_resolved_milestone_is_not_reannounced(scratch_db):
    """State lost after the milestone's observation was resolved by hand: the
    retry hits the retained primary key. That is 'already announced', not a
    failure, and certainly not a second wake-up."""
    import asyncio

    import aiosqlite

    from genesis.db.crud import observations

    _scratch_db(scratch_db)
    assert asyncio.run(mod._announce("owner/repo", 200, 201)) is True
    (row_id,) = _obs_ids(scratch_db)

    async def _resolve():
        async with aiosqlite.connect(str(scratch_db)) as db:
            await observations.resolve(
                db, row_id, resolved_at="2026-01-01T00:00:00+00:00", resolution_notes="test"
            )

    asyncio.run(_resolve())
    assert asyncio.run(mod._announce("owner/repo", 200, 205)) is False
    assert _obs_ids(scratch_db) == [row_id]


def test_an_integrity_error_that_is_not_the_retained_row_is_a_failure(scratch_db):
    """Only the deterministic id already existing means 'announced before'. Any
    other constraint refusal wrote NOTHING; swallowing it would let `main`
    record a milestone that was never announced — the one outcome worse than
    announcing twice."""
    import asyncio
    import sqlite3

    _scratch_db(scratch_db, abort_trigger=True)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(mod._announce("owner/repo", 200, 201))
    assert _obs_ids(scratch_db) == []
