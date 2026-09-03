"""The migration-prefix collision gate inside git_push_guard.

WHY THIS EXISTS, measured 2026-09-03. CI's `migration-check` job
(`.github/workflows/ci.yml`) already rejects duplicate prefixes — but it runs on
the PR's merge preview at ONE MOMENT, and that verdict then goes stale. Two
failure modes slip through, and both were live on the board when this was
written:

  * STALE GREEN. #1541's `migration-check` passed 2026-09-02T13:17Z; the
    colliding `0090_ws2_calibration_sunset.py` landed on main 8.5 hours later at
    21:43Z. The tick stayed green and the PR stayed MERGEABLE. 30 of 48 open PRs
    were in this stale-CI state.
  * CROSS-PR INVISIBILITY. #1610 and #1616 each add a DIFFERENTLY-NAMED
    `0092_*.py`. Each is individually clean, so no single PR's CI can ever see
    the collision — it exists only after both merge.

The consequence is not cosmetic: BOTH runners (`db/migrations/runner.py:252-264`
and `db/data_migrations/runner.py`) run a duplicate-prefix pre-flight and raise,
so a collision halts every migration on every install at next restart until a
human renames a file. That severity plus the measured frequency is why this gate
BLOCKS rather than advises (the standing axiom is advisory-by-default; a block
needs a specific, credible, measured reason, and this has both halves).

FAIL DIRECTION, split deliberately:

  * a DEFINITE collision BLOCKS — a prefix this PR ADDS that already exists on
    the default branch, that another open PR also adds, or that this PR itself
    claims twice;
  * a failure of the gate's own PLUMBING (gh unreadable, malformed payload, a
    truncated listing we could not resolve, an impossible EMPTY listing) does
    NOT block. Walling off every merge over an auxiliary availability check is
    a worse failure than the one it prevents, and it mirrors the pin-receipt
    gate's split.

Only ADDED files claim a prefix (architect BLOCKER 2): a PR that merely edits
or deletes an existing migration claims nothing, and reading every touched path
as an addition would hard-block two PRs that both patch one old migration —
with no override, since this gate deliberately has none.

The truncation case is called out because "the listing ended" and "there is
nothing there" are indistinguishable to a naive reader: GraphQL caps a PR's
file list AND the PR list itself, #1541 hit the file cap live, and a gate that
read the short list as "no migrations" would report CLEAN on the very PR
carrying collisions.

Network-free via the `_TEST_GH_*` env-injection seams, matching the sibling
gates' convention — PLUS subprocess-level tests (seams deleted, subprocess.run
faked) so the GraphQL parse, the changeType filter, the refetch loop and the
truncation accounting are exercised rather than stubbed out from every test
(architect SHOULD-FIX 7: with autouse seams alone, the entire network layer had
zero coverage and deleting it wholesale left 24 greens).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess as _subprocess
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location(
    "git_push_guard", _WORKTREE / "scripts" / "hooks" / "git_push_guard.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_D = "src/genesis/db/migrations/"
_DD = "src/genesis/db/data_migrations/"


def _files(*paths: str, status: str = "added") -> str:
    """The `_TEST_GH_PR_FILES` wire format: one JSON object per line."""
    return "\n".join(
        json.dumps({"filename": p, "previous_filename": None, "status": status}) for p in paths
    )


def _main(*names: str) -> str:
    """The `_TEST_GH_MAIN_MIGRATIONS` seam: one entry per line (bare basename
    = the numbered dir; an entry with a slash is a full path)."""
    return "\n".join(names)


def _others(
    mapping: dict[int, list[str]],
    truncated: list[int] | None = None,
    pr_list_truncated: bool = False,
) -> str:
    """The `_TEST_GH_OPEN_PR_MIGRATIONS` seam: a JSON object."""
    return json.dumps(
        {
            "prs": {str(k): v for k, v in mapping.items()},
            "truncated": truncated or [],
            "pr_list_truncated": pr_list_truncated,
        }
    )


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    """Default every test to a readable, empty world.

    Set explicitly rather than left unset: an UNSET seam falls through to the
    live `gh` path, which would make these tests network-dependent and — worse —
    quietly pass for the wrong reason on a machine with `gh` auth. The
    subprocess-level tests below DELETE these deliberately and fake the layer
    underneath instead.
    """
    monkeypatch.setenv("_TEST_GH_PR_FILES", "")
    monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", "")
    monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({}))


class TestPrefixExtraction:
    """`_migration_prefix_claims` — what counts as claiming a prefix."""

    def test_extracts_four_digit_prefix(self):
        got = _mod._migration_prefix_claims([f"{_D}0092_tco_tool_use_id.py"])
        assert got == {"0092": ["0092_tco_tool_use_id.py"]}

    def test_two_files_under_one_prefix_are_BOTH_kept(self):
        """Last-write-wins would collapse the exact state the runner raises on
        — a PR self-colliding — into one invisible entry (SHOULD-FIX 6)."""
        got = _mod._migration_prefix_claims([f"{_D}0092_a.py", f"{_D}0092_b.py"])
        assert got == {"0092": ["0092_a.py", "0092_b.py"]}

    def test_data_migrations_claim_their_own_namespace(self):
        """Both runners raise on duplicates; only the numbered dir was covered
        by CI (SHOULD-FIX 5). The `d` prefix keeps the keys disjoint, so the
        namespaces can never cross-collide in one dict."""
        got = _mod._migration_prefix_claims([f"{_DD}d0004_purge.py", f"{_D}0004_other.py"])
        assert got == {"d0004": ["d0004_purge.py"], "0004": ["0004_other.py"]}

    def test_ignores_non_migration_paths(self):
        assert _mod._migration_prefix_claims(["src/genesis/db/crud/foo.py"]) == {}

    def test_ignores_the_runner_and_dunder_files(self):
        got = _mod._migration_prefix_claims([f"{_D}runner.py", f"{_D}__init__.py"])
        assert got == {}

    def test_ignores_a_nested_path_under_the_directory(self):
        assert _mod._migration_prefix_claims([f"{_D}sub/0092_x.py"]) == {}

    def test_requires_exactly_four_digits(self):
        assert _mod._migration_prefix_claims([f"{_D}092_short.py"]) == {}
        assert _mod._migration_prefix_claims([f"{_D}00921_long.py"]) == {}

    def test_matches_the_runners_charset_not_a_looser_one(self):
        """The runner's discovery regex is `\\w+` — a dash or dot name is
        silently SKIPPED at runtime and can never produce the RuntimeError this
        gate cites, so claiming it here would block on a phantom (NOTE 10)."""
        assert _mod._migration_prefix_claims([f"{_D}0092_a-b.py"]) == {}
        assert _mod._migration_prefix_claims([f"{_D}0092_a.b.py"]) == {}

    def test_requires_a_py_extension(self):
        assert _mod._migration_prefix_claims([f"{_D}0092_notes.md"]) == {}


class TestOnlyAddedFilesClaim:
    """BLOCKER 2: a touched file is not an added file."""

    def test_a_modified_migration_claims_nothing(self, monkeypatch):
        """Two PRs both PATCHING one existing migration must not block each
        other — neither is creating a prefix."""
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES", _files(f"{_D}0090_ws2_sunset.py", status="modified")
        )
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0090_ws2_sunset.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({1610: ["0090_ws2_sunset.py"]}))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "no migration" in msg.lower()

    def test_a_removed_migration_claims_nothing(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0090_ws2_sunset.py", status="removed"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0090_ws2_sunset.py"))
        blocked, _ = _mod._check_migration_prefixes("1616")
        assert blocked is False

    def test_a_renamed_target_still_claims(self, monkeypatch):
        """A rename CREATES the destination path — that prefix is claimed."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_renamed.py", status="renamed"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({1610: ["0092_other.py"]}))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is True
        assert "0092" in msg


class TestSelfCollision:
    def test_one_pr_adding_two_files_under_one_prefix_blocks(self, monkeypatch):
        """The exact state the runner raises on; a scan comparing only against
        OTHER sources is structurally blind to it (SHOULD-FIX 6)."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_a.py", f"{_D}0092_b.py"))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is True
        assert "0092_a.py" in msg and "0092_b.py" in msg


class TestCollisionWithDefaultBranch:
    """The STALE-GREEN mode: main moved after CI last ran (#1541)."""

    def test_blocks_when_prefix_already_on_main(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0090_session_ledger.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0090_ws2_sunset.py"))
        blocked, msg = _mod._check_migration_prefixes("1541")
        assert blocked is True
        assert "0090" in msg
        # Names BOTH sides — a bare "0090 collides" leaves the reader to go
        # find what it collides with, which is the diagnosis they need.
        assert "0090_ws2_sunset.py" in msg
        assert "0090_session_ledger.py" in msg

    def test_reports_every_colliding_prefix_not_just_the_first(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            _files(f"{_D}0087_a.py", f"{_D}0088_b.py", f"{_D}0089_c.py"),
        )
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0087_x.py", "0088_y.py", "0089_z.py"))
        blocked, msg = _mod._check_migration_prefixes("1541")
        assert blocked is True
        for p in ("0087", "0088", "0089"):
            assert p in msg

    def test_allows_a_fresh_prefix(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0093_entity.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0091_last.py"))
        blocked, _ = _mod._check_migration_prefixes("1477")
        assert blocked is False

    def test_same_filename_on_main_is_not_a_collision(self, monkeypatch):
        """A long-lived branch that already merged main can show main's own
        migrations as ADDED relative to its ancient merge-base. Identical path
        = the same file, not a duplicate prefix."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0090_ws2_sunset.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0090_ws2_sunset.py"))
        blocked, _ = _mod._check_migration_prefixes("1541")
        assert blocked is False

    def test_data_migration_collision_with_main_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_DD}d0004_mine.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main(f"{_DD}d0004_theirs.py"))
        blocked, msg = _mod._check_migration_prefixes("1700")
        assert blocked is True
        assert "d0004" in msg


class TestCollisionWithOtherOpenPRs:
    """The CROSS-PR mode: each PR individually clean (#1610 vs #1616)."""

    def test_blocks_when_another_open_pr_claims_the_prefix(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({1610: ["0092_void_owner.py"]}))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is True
        assert "0092" in msg
        assert "1610" in msg  # names WHICH PR — the reader must go look at it

    def test_does_not_collide_with_itself(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({1616: ["0092_tco.py"]}))
        blocked, _ = _mod._check_migration_prefixes("1616")
        assert blocked is False

    def test_self_exclusion_is_by_number_not_by_filename(self, monkeypatch):
        """Excluding "the file I also have" instead of "my own PR row" would
        silence a REAL collision whenever two PRs add the same basename."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        monkeypatch.setenv(
            "_TEST_GH_OPEN_PR_MIGRATIONS",
            _others({1616: ["0092_tco.py"], 1610: ["0092_tco.py"]}),
        )
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is True
        assert "1610" in msg


class TestFailDirection:
    """Plumbing failures must NOT block; they must also not read as clean."""

    def test_unreadable_pr_file_list_does_not_block(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", "__error__")
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "could not" in msg.lower() or "unverified" in msg.lower()

    def test_unreadable_main_listing_does_not_block(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", "__error__")
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "could not" in msg.lower() or "unverified" in msg.lower()

    def test_an_EMPTY_main_listing_is_an_anomaly_not_a_clean_pass(self, monkeypatch):
        """90+ migrations exist on main; a successful listing with zero rows
        means the read did not describe the directory — the seen-nothing case
        that must never report no-problem (SHOULD-FIX 4). The autouse fixture
        sets this seam empty, so this test asserts on the DEFAULT world too."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "EMPTY" in msg
        assert "ok (" not in msg

    def test_unreadable_open_pr_listing_still_blocks_a_main_collision(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0090_x.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0090_ws2.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", "__error__")
        blocked, msg = _mod._check_migration_prefixes("1541")
        assert blocked is True
        assert "0090" in msg

    def test_truncated_listing_never_reads_as_clean(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0095_new.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0091_last.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({}, truncated=[1541]))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "1541" in msg
        assert "truncat" in msg.lower() or "incomplete" in msg.lower()

    def test_a_truncated_PR_LIST_never_reads_as_clean(self, monkeypatch):
        """The sweep caps the PR list itself at 100; above that, unseen PRs
        must be REPORTED, not silently treated as clean (SHOULD-FIX 3)."""
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0095_new.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0091_last.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({}, pr_list_truncated=True))
        blocked, msg = _mod._check_migration_prefixes("1616")
        assert blocked is False
        assert "TRUNCATED" in msg

    def test_partial_scan_still_states_what_it_DID_verify(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0093_entity.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0091_last.py"))
        monkeypatch.setenv("_TEST_GH_OPEN_PR_MIGRATIONS", _others({}, truncated=[1541]))
        blocked, msg = _mod._check_migration_prefixes("1477")
        assert blocked is False
        assert "default branch" in msg
        assert "1541" in msg

    def test_no_migrations_in_pr_is_a_clean_pass(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files("README.md"))
        blocked, msg = _mod._check_migration_prefixes("1657")
        assert blocked is False
        assert "no migration" in msg.lower()


def _fake_run(responses):
    """A subprocess.run stand-in: pops canned (stdout, returncode) per call and
    RECORDS each argv, so a test can assert which calls actually happened."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        stdout, rc = responses.pop(0)
        return _subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")

    return run, calls


def _graphql_payload(nodes, pr_list_has_next=False):
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": pr_list_has_next},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


def _pr_node(number, paths, has_next=False, change_type="ADDED"):
    return {
        "number": number,
        "files": {
            "pageInfo": {"hasNextPage": has_next},
            "nodes": [{"path": p, "changeType": change_type} for p in paths],
        },
    }


class TestOpenPrSweepSubprocessLevel:
    """The network layer itself — seams DELETED, subprocess.run faked.

    With the autouse seams alone, the GraphQL parse, changeType filter, refetch
    loop and truncation accounting had ZERO executions under pytest: the whole
    block could be deleted and 24 tests stayed green (SHOULD-FIX 7). These run
    the real code against canned wire payloads.
    """

    @pytest.fixture(autouse=True)
    def _no_seam(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_OPEN_PR_MIGRATIONS", raising=False)
        monkeypatch.delenv("_TEST_GH_PR_FILES", raising=False)

    def test_parses_the_graphql_payload_and_filters_changetype(self, monkeypatch):
        run, calls = _fake_run(
            [
                (
                    _graphql_payload(
                        [
                            _pr_node(1610, [f"{_D}0092_void.py"]),
                            _pr_node(1600, [f"{_D}0090_ws2.py"], change_type="MODIFIED"),
                        ]
                    ),
                    0,
                )
            ]
        )
        monkeypatch.setattr(_mod.subprocess, "run", run)
        out = _mod._open_pr_migrations(repo="o/r")
        assert out is not None
        prs, unresolved, pr_trunc = out
        # The MODIFIED file must not claim; the ADDED one must — and as a FULL
        # path, because the consumer re-parses claims through the dir-anchored
        # matcher. A basename here made every live cross-PR collision vanish
        # while all seam-fed tests stayed green (shipped once, caught only by
        # the live acceptance replay).
        assert prs == {"1610": [f"{_D}0092_void.py"]}
        assert unresolved == [] and pr_trunc is False
        assert len(calls) == 1  # no refetch when nothing overflowed

    def test_live_sweep_output_actually_collides_in_the_consumer(self, monkeypatch):
        """THE CONTRACT TEST: the sweep's real (non-seam) output must flow
        through `_check_migration_prefixes` and produce a block. The producer
        and consumer were each green in isolation while their wire format
        disagreed — only a test that wires one into the other pins the
        contract."""
        run, _ = _fake_run([(_graphql_payload([_pr_node(1610, [f"{_D}0092_void.py"])]), 0)])
        monkeypatch.setattr(_mod.subprocess, "run", run)
        # Own files + main via seams; ONLY the open-PR sweep goes live.
        monkeypatch.setenv("_TEST_GH_PR_FILES", _files(f"{_D}0092_tco.py"))
        monkeypatch.setenv("_TEST_GH_MAIN_MIGRATIONS", _main("0091_last.py"))
        blocked, msg = _mod._check_migration_prefixes("1616", repo="o/r")
        assert blocked is True
        assert "0092" in msg and "1610" in msg

    def test_an_overflowing_file_list_triggers_exactly_one_refetch(self, monkeypatch):
        run, calls = _fake_run(
            [
                (_graphql_payload([_pr_node(1541, [], has_next=True)]), 0),
                # the refetch: _pr_added_files' REST read for #1541
                (f"{_D}0090_ledger.py\n", 0),
            ]
        )
        monkeypatch.setattr(_mod.subprocess, "run", run)
        out = _mod._open_pr_migrations(repo="o/r")
        assert out is not None
        prs, unresolved, _ = out
        assert prs == {"1541": [f"{_D}0090_ledger.py"]}  # full path — see above
        assert unresolved == []
        assert len(calls) == 2
        assert "1541" in " ".join(str(a) for a in calls[1])

    def test_overflow_beyond_the_refetch_cap_lands_in_unresolved(self, monkeypatch):
        overflowing = [
            _pr_node(1500 + i, [], has_next=True)
            for i in range(_mod._MIGRATION_TRUNCATION_REFETCH_CAP + 2)
        ]
        responses = [(_graphql_payload(overflowing), 0)]
        responses += [("", 0)] * _mod._MIGRATION_TRUNCATION_REFETCH_CAP
        run, calls = _fake_run(responses)
        monkeypatch.setattr(_mod.subprocess, "run", run)
        out = _mod._open_pr_migrations(repo="o/r")
        assert out is not None
        _, unresolved, _ = out
        # Everything past the cap is REPORTED, never silently dropped.
        assert len(unresolved) == 2
        assert len(calls) == 1 + _mod._MIGRATION_TRUNCATION_REFETCH_CAP

    def test_pr_list_truncation_is_surfaced(self, monkeypatch):
        run, _ = _fake_run([(_graphql_payload([], pr_list_has_next=True), 0)])
        monkeypatch.setattr(_mod.subprocess, "run", run)
        out = _mod._open_pr_migrations(repo="o/r")
        assert out is not None
        assert out[2] is True

    def test_a_failed_graphql_call_returns_None(self, monkeypatch):
        run, _ = _fake_run([("", 1)])
        monkeypatch.setattr(_mod.subprocess, "run", run)
        assert _mod._open_pr_migrations(repo="o/r") is None

    def test_added_files_reads_status_from_the_wire(self, monkeypatch):
        """`_pr_added_files`' live jq filters by status; the seam path filters
        in Python. This exercises the SEAM path's filter with explicit
        statuses — the live filter is asserted by the jq text itself."""
        monkeypatch.setenv(
            "_TEST_GH_PR_FILES",
            _files(f"{_D}0092_a.py", status="added")
            + "\n"
            + _files(f"{_D}0090_b.py", status="modified"),
        )
        assert _mod._pr_added_files("1") == [f"{_D}0092_a.py"]


class TestCiFreshnessNote:
    """ADVISORY only — a green tick that predates the current default branch.

    Measured 2026-09-03: 30 of 48 open PRs were in this state, the oldest
    (#1223) carrying a tick from six weeks earlier. That is the majority of the
    queue, so this can only ever be a note: blocking on it would refuse 62% of
    open PRs for a condition every one of them shares, and the honest remedy
    (re-run CI against current main) is a throughput decision the owner makes,
    not something a gate should force one PR at a time.
    """

    def test_flags_a_check_older_than_the_default_branch_head(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-02T13:17:21Z")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-02T21:43:17Z")
        note = _mod._ci_freshness_note("1541")
        assert note is not None
        assert "DIFFERENT main" in note
        # Both instants named — a note that says "stale" without saying stale
        # against WHAT sends the reader back to the API to find out.
        assert "2026-09-02T13:17:21Z" in note and "2026-09-02T21:43:17Z" in note

    def test_note_does_not_assert_a_CI_verdict_it_never_read(self, monkeypatch):
        """Caught live: the first wording said "green against a different main",
        but this function never reads pass/fail — and #1541, the very PR it was
        demonstrated on, had gone RED under a concurrent push. Staleness and
        outcome are independent facts; claiming one from the other is exactly
        the unverified-assertion class the report is supposed to avoid."""
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-02T13:17:21Z")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-02T21:43:17Z")
        note = _mod._ci_freshness_note("1541")
        assert "green" not in note.lower()
        assert "pass" not in note.lower()

    def test_silent_when_the_check_is_newer_than_main(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-03T18:00:00Z")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-03T12:00:00Z")
        assert _mod._ci_freshness_note("1616") is None

    def test_silent_when_either_side_is_unreadable(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "__error__")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-03T12:00:00Z")
        assert _mod._ci_freshness_note("1616") is None
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-03T12:00:00Z")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "__error__")
        assert _mod._ci_freshness_note("1616") is None

    def test_handles_offset_timestamps_not_just_Z(self, monkeypatch):
        """GitHub returns `Z` on the checks API and a numeric offset on the
        commit API. Comparing them as STRINGS would put `2026-09-02T21:43:17Z`
        before `2026-09-02T17:43:17-04:00` — the same instant, wrong answer."""
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-02T13:17:21Z")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-02T17:43:17-04:00")
        assert _mod._ci_freshness_note("1541") is not None

    def test_a_naive_timestamp_yields_silence_not_a_crash(self, monkeypatch):
        """Comparing naive to aware raises TypeError; outside the guard that
        aborts the whole report mid-print — a partial report that looks
        structurally complete (SHOULD-FIX 9)."""
        monkeypatch.setenv("_TEST_GH_CI_COMPLETED_AT", "2026-09-02T13:17:21")
        monkeypatch.setenv("_TEST_GH_DEFAULT_HEAD_DATE", "2026-09-02T21:43:17Z")
        assert _mod._ci_freshness_note("1541") is None
