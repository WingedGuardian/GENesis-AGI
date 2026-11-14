"""Cgroup IO settings on user units/scopes, and whether they can take effect.

``IOWeight=`` and its siblings are cgroup io-controller settings. On a user
unit or a ``systemd-run --user`` scope they take effect only when the user
manager has the io controller; systemd's default user manager delegation often
leaves it out, and then systemd accepts the setting and ignores it with no
message. These tests pin the three parts that make that visible:

* the reader answering "is io delegated to this user's manager?" from a
  cgroup tree (fake trees here, never the live one);
* the registry of every place Genesis sets such a key, locked against the
  tracked tree so a new site cannot be added without being named;
* the posture rule that warns, naming each site, when the answer is no.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from genesis.infra_profile import cgroup_io

REPO = Path(__file__).resolve().parents[2]
MANAGER = "/user.slice/user-1000.slice/user@1000.service"


def _tree(tmp_path: Path, controllers: str | None) -> Path:
    sys_root = tmp_path / "sys"
    cg = sys_root / "fs/cgroup" / MANAGER.lstrip("/")
    cg.mkdir(parents=True)
    if controllers is not None:
        (cg / "cgroup.controllers").write_text(controllers + "\n")
    return sys_root


# ── the reader ───────────────────────────────────────────────────────────


def test_io_absent_from_the_manager_controllers_reads_not_delegated(tmp_path):
    sys_root = _tree(tmp_path, "cpu memory pids")
    assert cgroup_io.io_delegated(sys_root, MANAGER) is False


def test_io_present_reads_delegated(tmp_path):
    sys_root = _tree(tmp_path, "cpuset cpu io memory pids")
    assert cgroup_io.io_delegated(sys_root, MANAGER) is True


def test_a_controller_merely_containing_io_is_not_io(tmp_path):
    # Whole-word match: a token like "iocost" or "bio" must not count as io.
    sys_root = _tree(tmp_path, "cpu memory pids iocost")
    assert cgroup_io.io_delegated(sys_root, MANAGER) is False


def test_unreadable_controllers_is_unknown_not_false(tmp_path):
    # cgroup v1, or a manager cgroup without the file: the question has no
    # answer, which must never read as "not delegated" (that would alert).
    sys_root = _tree(tmp_path, None)
    assert cgroup_io.io_delegated(sys_root, MANAGER) is None


def test_no_manager_cgroup_is_unknown(tmp_path):
    assert cgroup_io.io_delegated(tmp_path / "sys", None) is None


# ── the registry, locked against the tracked tree ────────────────────────

# Directories that hold code which can set a unit or scope property, read as
# git's TRACKED files (`git ls-files`), never a filesystem walk: an untracked
# scratch file is not a setter, and in a main checkout .claude/worktrees holds
# every linked worktree.
_SCANNED = ("src", "scripts", "config", ".claude/mcp", ".claude/hooks")
_ASSIGNMENT = re.compile(r"\b(" + "|".join(cgroup_io.IO_KEYS) + r")\s*=")
_CODE_SUFFIXES = {".py", ".sh", ".template", ".service", ".timer", ".slice", ".scope", ".conf", ""}


def _tracked_files() -> list[Path]:
    # A failed listing must fail the test loudly, never read as "no setters".
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", *_SCANNED],
        capture_output=True,
        check=True,
    ).stdout
    return [REPO / name for name in out.decode().split("\0") if name]


def _files_setting_io_keys() -> set[str]:
    found: set[str] = set()
    for path in _tracked_files():
        if not path.is_file() or path.suffix not in _CODE_SUFFIXES:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("#", ";")):
                continue  # a comment (Python, shell, unit file) sets nothing
            if _ASSIGNMENT.search(line):
                found.add(path.relative_to(REPO).as_posix())
                break
    return found


def test_the_scan_sees_a_known_setter():
    # Guard the guard: an instrument that finds nothing is indistinguishable
    # from one that looks at nothing. The CC session scope is a known setter.
    assert "src/genesis/cc/invoker.py" in _files_setting_io_keys()


def test_every_file_setting_an_io_key_is_registered_and_nothing_stale():
    registered = {setter.path for setter in cgroup_io.SETTERS}
    found = _files_setting_io_keys()
    assert found - registered == set(), (
        "these files set a cgroup IO key on a unit or scope but are not in "
        "genesis.infra_profile.cgroup_io.SETTERS, so the inert-setting warning "
        "would not name them"
    )
    assert registered - found == set(), "registered setters that no longer set an IO key"


def test_every_registered_key_is_a_known_io_key():
    for setter in cgroup_io.SETTERS:
        assert setter.keys and set(setter.keys) <= set(cgroup_io.IO_KEYS)


# ── the warning text ─────────────────────────────────────────────────────


def test_warning_names_every_setter_its_keys_and_the_working_alternative():
    text = cgroup_io.warning_detail()
    for setter in cgroup_io.SETTERS:
        assert setter.path in text
        for key in setter.keys:
            assert key in text
    assert "no effect" in text
    assert "IOSchedulingClass" in text and "IOSchedulingPriority" in text


def test_every_setter_declares_a_known_kind():
    # The kind picks the alternative the note offers; a scope cannot take
    # IOSchedulingClass= (systemd-run refuses it: "Unknown assignment").
    for setter in cgroup_io.SETTERS:
        assert setter.kind in ("scope", "service")


def test_warning_gives_the_scope_alternative_and_does_not_oversell_delegation():
    text = cgroup_io.warning_detail()
    # A scope is told to wrap its command; IOSchedulingClass= is for services.
    assert "ionice -c2 -n7" in text and "ionice -c3" in text
    assert "for a service, set IOSchedulingClass=" in text
    # Priority needs a scheduler that honours it.
    assert "BFQ" in text and "mq-deadline" in text
    # Delegation is necessary, not sufficient, and its restart has a cost.
    assert "not sufficient" in text and "io.cost.qos" in text
    assert "restarts every user unit" in text
