"""Code-intel index health: does a dead code index actually SAY so?

MEASURED failure this exists for (2026-09-05): the main repo's index request was
euthanized after ``MAX_ATTEMPTS`` genuine failures and its database sat as a
164 MB ``.db.corrupt`` for two weeks, while the runner logged ``index failed``
35 times. Nothing read either signal — no awareness check covered code-intel, and
the only ``src/`` consumer of the marker system is a WRITER. So it did not fail
silently; it failed LOUDLY INTO A LOG, which is operationally identical.

Fixtures are synthetic and every ambient input is pinned: these tests must never
read this box's real cache or marker dir (the same isolation the injection
watcher's suite needs, and for the same reason — a real euthanized marker on the
developer's machine would make unrelated "clean" assertions fail).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from genesis.observability.snapshots import code_intel_health as ci


@pytest.fixture(autouse=True)
def _isolate_from_the_real_install(tmp_path, monkeypatch):
    """Pin HOME so nothing resolves to this box's real ~/.cache or ~/.genesis."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CBM_CACHE_DIR", raising=False)


def _markers(tmp_path) -> Path:
    d = tmp_path / "index-requests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache(tmp_path) -> Path:
    d = tmp_path / "cbm-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _euthanized(markers: Path, repo: str, attempts: int = 5) -> Path:
    p = markers / "deadbeef0000.failed.json"
    p.write_text(json.dumps({"repo_path": repo, "attempts": attempts, "mode": "fast"}))
    return p


def _index(cache: Path, target: Path, *, suffix: str = ".db") -> Path:
    """Write a fake index db for `target`, using CBM's real slug shape.

    Slug is the FULL path with '/' -> '-', verified against a live cache entry
    (/home/ubuntu/tmp/kimi_review -> home-ubuntu-tmp-kimi_review).
    """
    p = cache / (ci.index_slug(target) + suffix)
    p.write_bytes(b"x" * 64)
    return p


def _collect(tmp_path, target: Path):
    return ci.collect(
        indexed_path=target,
        marker_dir=_markers(tmp_path),
        cache_dir=_cache(tmp_path),
    )


# ── the acceptance bar: replay the real 2026-09-05 defect ─────────────────


def test_a_euthanized_index_request_alerts(tmp_path):
    """THE ACCEPTANCE BAR — replays the measured defect.

    ``<hash>.failed.json`` is a TERMINAL state: index_marker's docstring says
    such a marker is "never retried", and it takes MAX_ATTEMPTS=5 genuine
    failures to reach. A repo abandoned by the indexer with nobody told is
    exactly the silence this check exists to break.
    """
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)  # index itself is fine — the REQUEST died
    _euthanized(_markers(tmp_path), str(target))

    h = _collect(tmp_path, target)
    findings = ci.derive_findings(h)
    assert h.euthanized, "a euthanized index request was not detected"
    assert findings, "euthanized request produced no finding"
    assert any("gave up" in f or "euthanized" in f for f in findings), findings


def test_a_corrupt_target_index_alerts(tmp_path):
    """The 164 MB `.db.corrupt` shape, measured on the live box."""
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target, suffix=".db.corrupt")

    h = _collect(tmp_path, target)
    assert h.index_state == "corrupt", h.index_state
    assert ci.derive_findings(h)


def test_an_absent_target_index_alerts(tmp_path):
    """Requested-and-never-built is as blind as corrupt."""
    target = tmp_path / "repo"
    target.mkdir()
    _euthanized(_markers(tmp_path), str(target))  # it WAS asked for

    h = _collect(tmp_path, target)
    assert h.index_state == "absent", h.index_state
    assert ci.derive_findings(h)


# ── CONTROLS: a noisy alarm gets muted, which recreates the failure ───────


def test_a_healthy_install_is_silent(tmp_path):
    """Index present, nothing euthanized -> no finding. If this fires, the
    check is unusable regardless of how well it detects the real thing."""
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)

    h = _collect(tmp_path, target)
    assert h.index_state == "ok", h.index_state
    assert not ci.derive_findings(h), ci.derive_findings(h)


def test_a_fresh_install_is_silent(tmp_path):
    """No marker dir, no cache, nothing ever requested.

    A fresh clone has no index and that is CORRECT, not a fault — the check
    must distinguish "never asked" from "asked and failed". Absence of an index
    only speaks when something asked for one.
    """
    target = tmp_path / "repo"
    target.mkdir()
    h = ci.collect(
        indexed_path=target,
        marker_dir=tmp_path / "no-such-markers",
        cache_dir=tmp_path / "no-such-cache",
    )
    assert not ci.derive_findings(h), ci.derive_findings(h)


def test_a_worktree_index_with_a_missing_root_is_out_of_scope(tmp_path):
    """DELIBERATELY out of scope (owner decision 2026-09-05).

    A live example exists on this box right now: a 31,431-node index whose
    worktree root was deleted. Worktrees are created and destroyed constantly
    here, so alerting on them is how this alarm would get muted — taking the
    real signal with it. Only the CONFIGURED target is in scope.
    """
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)  # the target itself is healthy
    # ...and a stale worktree index whose root is long gone:
    gone = tmp_path / "repo" / ".claude" / "worktrees" / "deleted-one"
    _index(_cache(tmp_path), gone)

    h = _collect(tmp_path, target)
    assert not ci.derive_findings(h), "a deleted worktree's index must not alert"


def test_a_euthanized_request_for_another_repo_is_out_of_scope(tmp_path):
    """Scope is the configured target, not every repo that ever failed."""
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)
    _euthanized(_markers(tmp_path), "/somewhere/else/entirely")

    h = _collect(tmp_path, target)
    assert not ci.derive_findings(h), ci.derive_findings(h)


# ── fail-loud discipline: the check must not go quiet on its own faults ───


def test_an_unreadable_marker_dir_is_reported_not_silent(tmp_path):
    """A check that cannot look must not report all-clear — the generator this
    whole class of work exists to kill."""
    from tests.conftest import require_access_denied

    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)
    markers = _markers(tmp_path)
    markers.chmod(0o000)
    require_access_denied(markers)
    try:
        h = ci.collect(indexed_path=target, marker_dir=markers, cache_dir=_cache(tmp_path))
        assert h.errors, "an unreadable marker dir read as clean"
        assert any("DEGRADED" in f for f in ci.derive_findings(h))
    finally:
        markers.chmod(0o755)


# ── alert identity: keyed on the CONDITION, never on a tally ──────────────


def test_alert_identity_ignores_counts_but_tracks_condition(tmp_path):
    """A rolling count re-pages hourly for one standing incident; the CONDITION
    changing is a genuinely different alert. Same discipline as the injection
    watcher's _IDENTITY_EXEMPT_FIELDS."""
    target = tmp_path / "repo"
    target.mkdir()
    _euthanized(_markers(tmp_path), str(target), attempts=5)
    first = ci.alert_identity(_collect(tmp_path, target))

    # Same condition, different attempt count -> identity MUST NOT change.
    _euthanized(_markers(tmp_path), str(target), attempts=9)
    assert ci.alert_identity(_collect(tmp_path, target)) == first

    # Condition changes (index also corrupt now) -> identity MUST change.
    _index(_cache(tmp_path), target, suffix=".db.corrupt")
    assert ci.alert_identity(_collect(tmp_path, target)) != first


def test_findings_escape_every_marker_field(tmp_path):
    """Both fields that reach the finding text are escaped at ingestion.

    The previous version of this test injected into `repo_path` — which the
    SCOPE FILTER then dropped, because a mangled path no longer slugs to the
    target. It asserted over an empty list and was vacuous. Inject through
    `attempts` instead, which the filter does not key on and which really was
    unescaped, and assert the euthanized list is NON-EMPTY first so the test
    cannot silently pass on nothing again.
    """
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)
    p = _markers(tmp_path) / "deadbeef0000.failed.json"
    p.write_text(
        json.dumps(
            {
                "repo_path": str(target),  # must MATCH, or scope drops it
                "attempts": "5\nINJECTED FINDING LINE",
                "mode": "fast",
            }
        )
    )

    h = _collect(tmp_path, target)
    assert h.euthanized, "scope filter dropped the marker — the test would be vacuous"
    assert all("\n" not in str(e["attempts"]) for e in h.euthanized), h.euthanized
    assert all("\n" not in f for f in ci.derive_findings(h)), ci.derive_findings(h)


def test_a_retained_corrupt_backup_beside_a_healthy_index_is_quiet(tmp_path):
    """BLOCKER regression: `.corrupt` is a RETAINED BACKUP, not a live flag.

    The indexer's own binary says `backing up corrupt db to .corrupt` and then
    rebuilds `<slug>.db` in place; nothing ever unlinks the backup (a 164 MB one
    on this box is two weeks old). Treating its existence as failure fired the
    alarm FOREVER after a successful rebuild — a permanently-wrong alarm, which
    is precisely what this module exists to prevent.
    """
    target = tmp_path / "repo"
    target.mkdir()
    db = _index(_cache(tmp_path), target)  # rebuilt, healthy
    old = _index(_cache(tmp_path), target, suffix=".db.corrupt")  # retained backup
    os.utime(old, (db.stat().st_mtime - 3600, db.stat().st_mtime - 3600))

    h = _collect(tmp_path, target)
    assert h.index_state == "ok", h.index_state
    assert not ci.derive_findings(h), ci.derive_findings(h)


def test_a_corrupt_backup_newer_than_the_index_still_alerts(tmp_path):
    """The other direction — a backup NEWER than the live db means the current
    index is the broken one. Without this cell the fix above could be a blanket
    'ignore .corrupt', which would blind the check entirely."""
    target = tmp_path / "repo"
    target.mkdir()
    db = _index(_cache(tmp_path), target)
    new = _index(_cache(tmp_path), target, suffix=".db.corrupt")
    os.utime(new, (db.stat().st_mtime + 3600, db.stat().st_mtime + 3600))

    h = _collect(tmp_path, target)
    assert h.index_state == "corrupt", h.index_state
    assert ci.derive_findings(h)


def test_a_non_object_marker_is_recorded_not_raised(tmp_path):
    """BLOCKER-adjacent regression: `json.loads` returns lists/ints happily, and
    `.get` on those raised AttributeError straight out of collect() — past every
    except clause into the caller's bare `except Exception: logger.warning`. No
    alert, no error entry, one log line nobody reads: this module's own failure
    mode applied to itself."""
    target = tmp_path / "repo"
    target.mkdir()
    _index(_cache(tmp_path), target)
    (_markers(tmp_path) / "deadbeef0000.failed.json").write_text("[]")

    h = _collect(tmp_path, target)  # must not raise
    assert h.errors, "a structurally-invalid marker was swallowed"
    assert any("DEGRADED" in f for f in ci.derive_findings(h))


def test_a_pending_marker_for_another_repo_does_not_prove_ours_was_requested(tmp_path):
    """`requested` gates whether an ABSENT index is a fault or a fresh install.

    Marker filenames are opaque hashes carrying no repo identity, so the old
    `any(*.json)` test treated ANY pending request as proof that OUR target was
    requested — turning a fresh install into a permanent 'index ABSENT' alert
    the moment a second repo was indexed.
    """
    target = tmp_path / "repo"
    target.mkdir()  # no index, never requested
    (_markers(tmp_path) / "cafe00001111.json").write_text(
        json.dumps({"repo_path": "/some/other/repo", "mode": "fast"})
    )

    h = _collect(tmp_path, target)
    assert not h.requested, "another repo's pending request marked ours as requested"
    assert not ci.derive_findings(h), ci.derive_findings(h)


def test_the_marker_dir_matches_the_helper_that_writes_it(tmp_path, monkeypatch):
    """PARITY LOCK for a hand-copied path.

    `default_marker_dir` re-derives what `scripts/lib/index_marker.py::marker_dir`
    computes, because `src/` cannot import from `scripts/lib`. Tests CAN (other
    call sites already import it by path), so the drift is locked here rather
    than asserted away in a docstring.
    """
    import importlib.util

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    spec = importlib.util.spec_from_file_location(
        "_im", Path("scripts/lib/index_marker.py").resolve()
    )
    im = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(im)
    assert ci.default_marker_dir() == im.marker_dir()


# ── awareness check wiring — "wired", not merely "substantive" ────────────


async def _alerts(db):
    cur = await db.execute(
        "SELECT content, priority, resolved_at FROM observations"
        " WHERE source = 'code_intel_health_monitor' AND type = 'infrastructure_alert'"
    )
    return [dict(r) for r in await cur.fetchall()]


async def _live(db):
    return [a for a in await _alerts(db) if not a["resolved_at"]]


@pytest.fixture
async def db():
    import aiosqlite

    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the check's config at a synthetic target + dirs."""
    target = tmp_path / "repo"
    target.mkdir(exist_ok=True)
    from genesis.awareness import code_intel_health_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", lambda: {**cfg_mod.DEFAULTS})
    monkeypatch.setattr(cfg_mod, "indexed_path", lambda cfg: target)
    monkeypatch.setattr(ci, "default_marker_dir", lambda: _markers(tmp_path))
    monkeypatch.setattr(ci, "default_cache_dir", lambda: _cache(tmp_path))
    return target


@pytest.mark.asyncio
async def test_check_creates_one_high_alert(db, wired, tmp_path):
    from genesis.awareness.loop import _check_code_intel_health

    _euthanized(_markers(tmp_path), str(wired))
    await _check_code_intel_health(db)
    live = await _live(db)
    assert len(live) == 1, live
    # high, NOT critical: a dead index degrades tool quality; it does not lose
    # the user's data or session context.
    assert live[0]["priority"] == "high"
    assert "ANSWERING FROM NOTHING" in live[0]["content"]


@pytest.mark.asyncio
async def test_check_is_idempotent_for_same_state(db, wired, tmp_path):
    from genesis.awareness.loop import _check_code_intel_health

    _euthanized(_markers(tmp_path), str(wired))
    await _check_code_intel_health(db)
    await _check_code_intel_health(db)
    assert len(await _live(db)) == 1, "a standing incident re-paged"


@pytest.mark.asyncio
async def test_recovery_resolves_the_alert(db, wired, tmp_path):
    """The index coming back must CLEAR the alert, not orphan it."""
    from genesis.awareness.loop import _check_code_intel_health

    marker = _euthanized(_markers(tmp_path), str(wired))
    await _check_code_intel_health(db)
    assert len(await _live(db)) == 1

    marker.unlink()  # someone cleared the dead request...
    _index(_cache(tmp_path), wired)  # ...and the index rebuilt
    await _check_code_intel_health(db)
    assert not await _live(db), "recovery left the alert standing"


@pytest.mark.asyncio
async def test_disabling_the_check_resolves_a_standing_alert(db, wired, tmp_path, monkeypatch):
    """Disabling must not strand the last alert on the health surfaces."""
    from genesis.awareness.loop import _check_code_intel_health

    _euthanized(_markers(tmp_path), str(wired))
    await _check_code_intel_health(db)
    assert len(await _live(db)) == 1

    monkeypatch.setenv("GENESIS_CODE_INTEL_HEALTH_DISABLED", "1")
    await _check_code_intel_health(db)
    assert not await _live(db), "disabling orphaned the alert"


@pytest.mark.asyncio
async def test_a_healthy_install_writes_no_alert(db, wired, tmp_path):
    from genesis.awareness.loop import _check_code_intel_health

    _index(_cache(tmp_path), wired)
    await _check_code_intel_health(db)
    assert not await _alerts(db), "healthy state produced an alert"
