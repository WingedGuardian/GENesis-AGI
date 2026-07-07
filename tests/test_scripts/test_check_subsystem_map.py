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


# --- staleness (git stubbed; warning-only by contract) ---


def test_staleness_warns_past_threshold(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[0] == "cat-file":
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
        if args[0] == "cat-file":
            return ""
        if args[0] == "rev-list":
            return "3"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) == []


def test_staleness_degrades_on_shallow_history(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "true"
        raise AssertionError("must not inspect history on a shallow clone")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) is None


def test_staleness_degrades_on_unknown_sha(monkeypatch):
    entries, _ = csm.parse_map(MAP_TWO_ENTRIES)

    def fake_git(args: list[str]) -> str | None:
        if args[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if args[0] == "cat-file":
            return None  # sha not present locally
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(csm, "_git", fake_git)
    assert csm.check_staleness(entries, threshold=20) is None


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
