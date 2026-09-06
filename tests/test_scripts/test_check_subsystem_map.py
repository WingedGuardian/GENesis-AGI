"""Subsystem-map drift guard (scripts/check_subsystem_map.py).

The guard parses the fenced ``yaml subsystem-map`` blocks in
docs/architecture/CURRENT.md and diffs the claimed module set against the live
top-level contents of src/genesis, both directions. Tests use synthetic maps
and source trees under tmp_path; the git-based staleness check is exercised
through a stubbed git runner so no test depends on real history or wall clock.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_subsystem_map", _REPO_ROOT / "scripts" / "check_subsystem_map.py",
)
csm = importlib.util.module_from_spec(_spec)
sys.modules["check_subsystem_map"] = csm  # @dataclass resolves cls.__module__ here
_spec.loader.exec_module(csm)


MAP_TWO_ENTRIES = """# Genesis — Current Architecture

## Memory

```yaml subsystem-map
entry: memory
modules: [memory, qdrant]
verified: 9037d45b 2026-07-07
```

## Platform

Some prose between blocks.

```yaml subsystem-map
entry: platform
modules:
  - db
  - util
  - env.py
verified: 9037d45b 2026-07-07
```

```yaml
not_a_map_block: this yaml block has no subsystem-map tag and is ignored
```
"""


def _write_src(tmp_path: Path, packages: list[str], loose: list[str]) -> Path:
    src = tmp_path / "src" / "genesis"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "__main__.py").write_text("")
    for pkg in packages:
        d = src / pkg
        d.mkdir()
        (d / "__init__.py").write_text("")
    for mod in loose:
        (src / mod).write_text("")
    return src


# --- parse_map ---


def test_parse_map_reads_tagged_blocks_only():
    entries, errors = csm.parse_map(MAP_TWO_ENTRIES)
    assert errors == []
    assert [e.name for e in entries] == ["memory", "platform"]
    assert entries[0].modules == ["memory", "qdrant"]
    assert entries[1].modules == ["db", "util", "env.py"]
    assert entries[0].verified_sha == "9037d45b"
    assert entries[0].verified_date == "2026-07-07"


def test_parse_map_flags_malformed_yaml():
    text = "```yaml subsystem-map\nentry: [unclosed\n```\n"
    entries, errors = csm.parse_map(text)
    assert entries == []
    assert len(errors) == 1


def test_parse_map_flags_missing_or_malformed_verified():
    missing = "```yaml subsystem-map\nentry: a\nmodules: [x]\n```\n"
    _, errors = csm.parse_map(missing)
    assert len(errors) == 1

    bad_stamp = (
        "```yaml subsystem-map\nentry: a\nmodules: [x]\nverified: sometime in june\n```\n"
    )
    _, errors = csm.parse_map(bad_stamp)
    assert len(errors) == 1


def test_parse_map_flags_missing_entry_name_and_empty_modules():
    no_name = "```yaml subsystem-map\nmodules: [x]\nverified: abc1234 2026-07-07\n```\n"
    _, errors = csm.parse_map(no_name)
    assert len(errors) == 1

    no_modules = "```yaml subsystem-map\nentry: a\nverified: abc1234 2026-07-07\n```\n"
    _, errors = csm.parse_map(no_modules)
    assert len(errors) == 1


# --- live_modules ---


def test_live_modules_lists_packages_and_loose_modules(tmp_path):
    src = _write_src(tmp_path, ["memory", "db"], ["env.py"])
    (src / "__pycache__").mkdir()
    assert csm.live_modules(src) == {"memory", "db", "env.py"}


def test_live_modules_includes_non_package_dirs(tmp_path):
    # src/genesis/skills has no __init__.py (SKILL.md tree) but is still a
    # top-level subsystem directory the map must claim.
    src = _write_src(tmp_path, ["memory"], [])
    (src / "skills").mkdir()
    (src / "skills" / "SKILL.md").write_text("")
    (src / ".hidden").mkdir()
    assert csm.live_modules(src) == {"memory", "skills"}


# --- coverage diff ---


def test_unmapped_module_is_an_error(tmp_path):
    src = _write_src(tmp_path, ["memory", "qdrant", "db", "util", "rogue_new_pkg"], ["env.py"])
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)
    problems = csm.check_coverage(entries, csm.live_modules(src), allowlist={})
    assert problems.unmapped == {"rogue_new_pkg"}
    assert problems.vanished == set()


def test_allowlist_suppresses_unmapped(tmp_path):
    src = _write_src(tmp_path, ["memory", "qdrant", "db", "util", "rogue_new_pkg"], ["env.py"])
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)
    problems = csm.check_coverage(
        entries, csm.live_modules(src), allowlist={"rogue_new_pkg": "why it is fine"},
    )
    assert problems.unmapped == set()


def test_vanished_claimed_module_is_an_error(tmp_path):
    src = _write_src(tmp_path, ["memory", "db", "util"], ["env.py"])  # no qdrant
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)
    problems = csm.check_coverage(entries, csm.live_modules(src), allowlist={})
    assert problems.vanished == {"qdrant"}


def test_module_claimed_twice_is_an_error(tmp_path):
    text = MAP_TWO_ENTRIES.replace("modules: [memory, qdrant]", "modules: [memory, qdrant, db]")
    src = _write_src(tmp_path, ["memory", "qdrant", "db", "util"], ["env.py"])
    entries, errors = csm.parse_map(text)
    assert errors == []
    problems = csm.check_coverage(entries, csm.live_modules(src), allowlist={})
    assert problems.duplicates == {"db"}


def test_unused_allowlist_entry_is_a_warning(tmp_path):
    src = _write_src(tmp_path, ["memory", "qdrant", "db", "util"], ["env.py"])
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)
    problems = csm.check_coverage(
        entries, csm.live_modules(src), allowlist={"long_gone": "stale reason"},
    )
    assert problems.unmapped == set()
    assert problems.unused_allowlist == {"long_gone"}


def test_parse_map_multiline_flow_list():
    text = (
        "```yaml subsystem-map\n"
        "entry: platform\n"
        "modules: [db, util,\n"
        "          env.py]\n"
        "verified: 9037d45b 2026-07-07\n"
        "```\n"
    )
    entries, errors = csm.parse_map(text)
    assert errors == []
    assert entries[0].modules == ["db", "util", "env.py"]


# --- staleness (git stubbed; warning-only by contract) ---


def test_git_returns_none_on_timeout(monkeypatch):
    # _git must never raise — a hung git call degrades to a staleness skip.
    def hang(*args, **kwargs):
        raise csm.subprocess.TimeoutExpired(cmd="git", timeout=60)

    monkeypatch.setattr(csm.subprocess, "run", hang)
    assert csm._git(["rev-parse", "--is-shallow-repository"]) is None


def test_staleness_warns_past_threshold(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return None  # no default ref here — the ancestry check stays out of it
        if args[0] in ("cat-file", "merge-base"):
            return ""
        if args[0] == "rev-list":
            return "999"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    warnings = csm.check_staleness(entries, threshold=20)
    assert len(warnings) == 2
    assert "memory" in warnings[0]


def test_staleness_quiet_under_threshold(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return None  # no default ref here — the ancestry check stays out of it
        if args[0] in ("cat-file", "merge-base"):
            return ""
        if args[0] == "rev-list":
            return "3"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) == []


def test_staleness_unresolvable_stamp_warns_and_keeps_checking(monkeypatch):
    # One entry's stamp is unresolvable (a pre-squash orphan); the other is stale.
    # The unresolvable stamp must NOT disable policing for the rest — it warns and
    # continues, so the stale entry is still caught. (Regression: a single dead stamp
    # used to `return None` and skip the whole staleness check for every entry.)
    # Two entries with DISTINCT stamps so one can be singled out as unresolvable.
    map_text = (
        "# Genesis\n\n## Memory\n\n```yaml subsystem-map\n"
        "entry: memory\nmodules: [memory]\nverified: aaaaaaaa 2026-07-07\n```\n\n"
        "## Platform\n\n```yaml subsystem-map\n"
        "entry: platform\nmodules: [db]\nverified: bbbbbbbb 2026-07-07\n```\n"
    )
    entries, _ = csm.parse_map(map_text)
    bad = entries[0].verified_sha  # "aaaaaaaa" — distinct from entry[1]'s stamp

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            # MERGE: the resolved implementation resolves a DEFAULT REF once
            # before the loop, so the ancestry check can ask "would squash-merge
            # discard this stamp?" rather than "is it an ancestor of HEAD?" —
            # HEAD cannot answer that, because a feature-branch commit is always
            # an ancestor of its own HEAD. This stub is taught that call rather
            # than the assertion being loosened: returning None keeps the
            # ancestry branch out of THIS test, which is about the post-mortem.
            return None
        if args[0] in ("cat-file", "merge-base"):
            # SUBSTRING, not element equality: `cat-file` is passed the sha with
            # a `^{commit}` suffix, so the sha is not a distinct element of argv
            # the way it is for `merge-base`. Matching on equality here silently
            # made every stamp resolvable and the test asserted nothing.
            return None if any(bad in a for a in args) else ""
        if args[0] == "rev-list":
            return "999"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    warnings = csm.check_staleness(entries, threshold=20)
    assert warnings is not None
    assert any("unresolvable" in w and bad in w for w in warnings)  # the orphan is surfaced
    assert any(entries[1].name in w and "commits touched" in w for w in warnings)  # stale still caught


def test_staleness_degrades_on_shallow_history(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        raise AssertionError("must not inspect history on a shallow clone")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) is None


MAP_ORPHAN_STAMP = """# Genesis — Current Architecture

## Orphan

```yaml subsystem-map
entry: orphan-stamped
modules: [memory]
verified: dead1234 2026-07-01
```

## Live

```yaml subsystem-map
entry: live-stamped
modules: [db]
verified: 9037d45b 2026-07-07
```
"""


def test_one_unresolvable_stamp_does_not_abandon_the_whole_pass(monkeypatch):
    """An orphan stamp is ONE entry's problem; every other entry still counts.

    This used to `return None` on the FIRST sha it could not resolve, which
    abandoned staleness for the entire file while `main()` went on printing
    CLEAN — a check reporting success while checking nothing.

    MEASURED on this repo 2026-08-31: two orphan stamps (feature-branch commits
    discarded by squash-merge) silenced the warning for all 14 entries, hiding
    four genuinely stale ones, and the skip message blamed shallow history /
    fetch-depth — which CI already sets correctly, so it sent the reader after
    a cause that did not exist.
    """
    entries, _ = csm.parse_map(MAP_ORPHAN_STAMP)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return None  # no default ref here — the ancestry check stays out of it
        if args[0] == "cat-file":
            return None if any("dead1234" in a for a in args) else ""
        if args[0] == "rev-list":
            return "999"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    warnings = csm.check_staleness(entries, threshold=20)

    assert warnings is not None, "one unresolvable stamp must not abandon the pass"
    assert len(warnings) == 2, warnings
    assert any("orphan-stamped" in w and "not a commit" in w for w in warnings), warnings
    assert any("live-stamped" in w and "999 commits" in w for w in warnings), warnings


def test_an_unknown_sha_is_reported_and_never_counted(monkeypatch):
    """An unresolvable stamp yields no number — and does not abandon the pass.

    This asserted `is None` (the WHOLE pass abandoned) until 2026-08-31. That
    was broader than its own rationale: "cannot count honestly" is true of the
    entry whose sha is missing, not of every other entry in the file. Held as
    written it let two orphan stamps take staleness reporting off the air for
    all 14 entries while the run still printed CLEAN.

    The guarantee it existed to protect is unchanged and asserted more strictly
    here: the fake RAISES on `rev-list`, so any attempt to invent a count for an
    unresolvable sha fails the test rather than merely being unasserted.

    Merge note: origin/main added the same test independently, as
    `test_staleness_all_stamps_unresolvable_warns_never_blanket_none`. Kept as
    one test under this name; its point is folded in — every stamp unresolvable
    yields one warning PER ENTRY, never a blanket None that would mask real
    staleness for the whole file. A shallow clone remains the only genuine None
    case (see test_staleness_degrades_on_shallow_history).
    """
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return None  # no default ref here — the ancestry check stays out of it
        if args[0] in ("cat-file", "merge-base"):
            return None  # sha not present locally
        raise AssertionError(f"must not count commits for an unresolvable sha: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    warnings = csm.check_staleness(entries, threshold=20)
    assert warnings is not None, "one unresolvable stamp must not abandon the pass"
    assert len(warnings) == 2, warnings
    # Assertion messages carried through from this side; the substring matches the
    # wording the resolved implementation actually emits.
    assert all("CANNOT be counted" in w for w in warnings), warnings


# --- main (integration over a synthetic repo) ---


def _run_main(tmp_path, monkeypatch, packages: list[str], map_text: str) -> int:
    src = _write_src(tmp_path, packages, ["env.py"])
    map_path = tmp_path / "docs" / "architecture" / "CURRENT.md"
    map_path.parent.mkdir(parents=True)
    map_path.write_text(map_text)
    monkeypatch.setattr(csm, "MAP_PATH", map_path)
    monkeypatch.setattr(csm, "SRC_ROOT", src)
    monkeypatch.setattr(csm, "_git", lambda args: None)  # no git → staleness skipped
    return csm.main()


def test_main_clean_map_exits_zero(tmp_path, monkeypatch, capsys):
    rc = _run_main(tmp_path, monkeypatch, ["memory", "qdrant", "db", "util"], MAP_TWO_ENTRIES)
    assert rc == 0
    assert "CLEAN" in capsys.readouterr().out


def test_main_unmapped_module_exits_one_with_error_annotation(tmp_path, monkeypatch, capsys):
    rc = _run_main(
        tmp_path, monkeypatch, ["memory", "qdrant", "db", "util", "rogue_new_pkg"],
        MAP_TWO_ENTRIES,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "rogue_new_pkg" in out


def test_main_missing_map_file_exits_one(tmp_path, monkeypatch, capsys):
    src = _write_src(tmp_path, ["memory"], [])
    monkeypatch.setattr(csm, "MAP_PATH", tmp_path / "nope.md")
    monkeypatch.setattr(csm, "SRC_ROOT", src)
    assert csm.main() == 1
    assert "::error::" in capsys.readouterr().out


MAP_BRANCH_STAMP = """# Genesis — Current Architecture

## Branch stamped

```yaml subsystem-map
entry: branch-stamped
modules: [memory]
verified: 82f84f0a 2026-08-31
```
"""


def test_a_stamp_that_will_not_survive_squash_merge_is_caught_before_it_lands(monkeypatch):
    """The PRE-mortem. The unresolvable-stamp check is a post-mortem by nature.

    A stamp naming a FEATURE-BRANCH commit resolves perfectly while the PR is
    open and dies the moment that PR is squash-merged, becoming the unresolvable
    stamp this file's other test covers. Catching it only afterwards means the
    staleness signal is already down.

    MEASURED 2026-08-31: two stamps on main were already dead this way
    (0e65071c, fbcf8ee4), and a THIRD was live on an open PR — `82f84f0a`,
    which `git merge-base --is-ancestor 82f84f0a origin/main` rejects — and
    would have landed within hours of the fix for the first two.

    Why a mechanical check rather than the prose rule in CURRENT.md's header:
    on that PR an adversarial reviewer RECOMMENDED the branch sha, reasoning
    correctly about ancestry AT REVIEW TIME while nobody was reasoning about
    what survives the merge. The recurrence path runs through careful people,
    so it needs something that does not depend on remembering.
    """
    entries, _ = csm.parse_map(MAP_BRANCH_STAMP)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return "abc"  # origin/main resolves
        if args[0] == "cat-file":
            return ""  # the stamp EXISTS locally — this is the whole trap
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return None  # …but it is not an ancestor of the default branch
        raise AssertionError(f"must not count staleness from a doomed stamp: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    warnings = csm.check_staleness(entries, threshold=20)

    assert len(warnings) == 1, warnings
    assert "not an ancestor" in warnings[0]
    assert "squash-merge" in warnings[0].lower()


def test_ancestry_is_not_checked_when_the_default_ref_is_unknown(monkeypatch):
    """Fail QUIET, not loud, when there is nothing to compare against.

    A clone with no `origin/main` (a fork, a mirror, a detached CI checkout)
    must not have every entry reported as doomed — the check would be measuring
    the absence of a ref, not the stamp.
    """
    entries, _ = csm.parse_map(MAP_BRANCH_STAMP)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[:2] == ["rev-parse", "--verify"]:
            return None  # no origin/main, no origin/master
        if args[0] == "cat-file":
            return ""
        if args[0] == "rev-list":
            return "0"
        raise AssertionError(f"ancestry must not be probed without a ref: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) == []
