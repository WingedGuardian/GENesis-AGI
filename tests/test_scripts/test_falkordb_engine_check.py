"""Tests for scripts/lib/falkordb_engine_check.py.

The check is the integrity anchor between pip's one-time --require-hashes and
the unit's every-start ``sha256sum -c``. It compares each installed engine file
with the digests COMMITTED in the repo (never the installed wheel's RECORD,
which whatever installed the wheel wrote), then places the version-tag copy the
unit runs and its digest file. Driven here as a subprocess against a fake
site-packages and a fake digest file built in tmp_path, the way the installer
runs it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_scripts.falkordb_stubs import (
    CHECK,
    DIGESTS,
    ENGINE_FILES,
    FAKE_TAG,
    PINNED_VERSION,
    REQUIREMENTS,
    copy_dir,
    fake_digests,
    fake_wheel,
    tree_state,
)


def _check(
    site: Path,
    deps: Path,
    digests: Path,
    requirements: Path = REQUIREMENTS,
):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(CHECK),
            "--requirements",
            str(requirements),
            "--digests",
            str(digests),
            "--deps-root",
            str(deps),
            "--site-packages",
            str(site),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def staged(tmp_path):
    """(site, deps root, digests) for a fake pinned wheel and its digests."""
    site = fake_wheel(tmp_path / "site")
    digests = fake_digests(tmp_path / "digests.txt", site)
    return site, tmp_path / "deps", digests


def _keys(stdout: str) -> dict:
    return dict(line.split("=", 1) for line in stdout.splitlines())


def _flip(path: Path, index: int = 3) -> None:
    data = bytearray(path.read_bytes())
    data[index] ^= 0x01
    path.write_bytes(bytes(data))


def _dead_pid() -> int:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout)


# --- the copy ------------------------------------------------------------------


def test_a_matching_wheel_places_a_checkable_copy(staged):
    site, deps, digests = staged
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    target = copy_dir(deps)
    assert result.stdout.splitlines() == [
        f"FALKORDB_ENGINE_DIR={target}",
        f"FALKORDB_REDIS_SERVER={target / 'redislite/bin/redis-server'}",
        f"FALKORDB_REDIS_CLI={target / 'redislite/bin/redis-cli'}",
        f"FALKORDB_MODULE={target / 'redislite/bin/falkordb.so'}",
        f"FALKORDB_DIGESTS={target / 'engine.sha256'}",
    ]
    # The two wheel dirs stay siblings: the module's RPATH is
    # $ORIGIN:$ORIGIN/../../falkordblite.libs.
    for rel, is_binary in ENGINE_FILES.items():
        copied = target / rel
        assert copied.read_bytes() == (site / rel).read_bytes(), rel
        assert copied.stat().st_mode & 0o777 == (0o755 if is_binary else 0o644), rel
    digest = target / "engine.sha256"
    lines = digest.read_text().splitlines()
    assert len(lines) == len(ENGINE_FILES)
    paths = [line.split("  ", 1)[1] for line in lines]
    assert paths == sorted(paths)
    # sha256sum -c format, ABSOLUTE paths inside the copy: the unit checks it
    # from any cwd, and it vouches for the copy, not for the venv.
    for line in lines:
        hexdigest, path = line.split("  ", 1)
        assert len(hexdigest) == 64 and path.startswith(str(target) + "/"), line
    verified = subprocess.run(
        ["sha256sum", "--quiet", "--strict", "-c", str(digest)],
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert sorted(p.name for p in deps.iterdir()) == [target.name], "left a temp dir"


def test_a_second_run_reuses_the_copy_untouched(staged):
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    before = tree_state(deps)
    dir_ino = copy_dir(deps).stat().st_ino
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert tree_state(deps) == before
    assert copy_dir(deps).stat().st_ino == dir_ino


@pytest.mark.parametrize(
    "damage",
    ["byte", "mode", "extra_file", "digest_file", "missing_file", "symlink"],
)
def test_a_copy_that_does_not_match_is_rebuilt(staged, damage):
    """Reuse only a copy whose every file matches the committed digests. A
    changed one is rebuilt in a temp dir and renamed over it, so ExecStartPre
    passes again only on bytes that match."""
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    target = copy_dir(deps)
    old_ino = target.stat().st_ino
    if damage == "byte":
        _flip(target / "redislite/bin/redis-cli")
    elif damage == "mode":
        (target / "redislite/bin/redis-server").chmod(0o644)
    elif damage == "extra_file":
        (target / "falkordblite.libs/libextra.so").write_bytes(b"x")
    elif damage == "digest_file":
        (target / "engine.sha256").write_text("")
    elif damage == "missing_file":
        (target / "falkordblite.libs/libgomp-test.so.1").unlink()
    elif damage == "symlink":
        lib = target / "falkordblite.libs/libssl-test.so.3"
        real = deps.parent / "elsewhere.so"
        real.write_bytes(lib.read_bytes())
        lib.unlink()
        lib.symlink_to(real)
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert target.stat().st_ino != old_ino, "reused a copy that does not match"
    check = subprocess.run(
        ["sha256sum", "--quiet", "--strict", "-c", str(target / "engine.sha256")],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert not (target / "falkordblite.libs/libextra.so").exists()
    assert not (target / "falkordblite.libs/libssl-test.so.3").is_symlink()
    assert sorted(p.name for p in deps.iterdir()) == [target.name]


def test_a_symlink_at_the_target_is_replaced_without_touching_what_it_names(staged):
    site, deps, digests = staged
    elsewhere = deps.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("mine")
    deps.mkdir()
    copy_dir(deps).symlink_to(elsewhere)
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert not copy_dir(deps).is_symlink()
    assert (elsewhere / "keep").read_text() == "mine"


def test_a_copy_that_lands_wrong_is_refused_and_never_placed(staged, monkeypatch, capsys):
    """The copy is re-verified after it is written: what runs is the copy, so
    a write that went wrong must not become the engine."""
    site, deps, digests = staged
    sys.path.insert(0, str(CHECK.parent))
    try:
        import falkordb_engine_check as check
    finally:
        sys.path.pop(0)
    real_write = check._write_file

    def corrupting(path, data_from, text, mode):
        real_write(path, data_from, text, mode)
        if path.name == "falkordb.so":
            _flip(path, 0)

    monkeypatch.setattr(check, "_write_file", corrupting)
    rc = check.main(
        [
            "--requirements",
            str(REQUIREMENTS),
            "--digests",
            str(digests),
            "--deps-root",
            str(deps),
            "--site-packages",
            str(site),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "falkordb.so does not match the committed digest (copy)" in err
    assert not copy_dir(deps).exists()
    assert list(deps.iterdir()) == [], "left the temp dir"


# --- what is trusted -------------------------------------------------------------


def test_the_wheels_own_record_is_not_the_reference(staged):
    """An unhashed install writes a RECORD that agrees with whatever bytes it
    installed; pip's 'already satisfied' never re-checks them. Bytes that
    differ from the committed digests fail even when RECORD vouches for them."""
    site, deps, digests = staged
    module = site / "redislite/bin/falkordb.so"
    _flip(module)
    record = next(site.glob("falkordblite-*.dist-info")) / "RECORD"
    sha = base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest())
    record.write_text(
        re.sub(
            r"^redislite/bin/falkordb.so,sha256=[^,]+,",
            f"redislite/bin/falkordb.so,sha256={sha.rstrip(b'=').decode()},",
            record.read_text(),
            flags=re.M,
        )
    )
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "falkordb.so does not match the committed digest (installed wheel)" in result.stderr


def test_a_missing_or_wrong_record_does_not_matter(staged):
    site, deps, digests = staged
    (next(site.glob("falkordblite-*.dist-info")) / "RECORD").write_text("garbage,,\n")
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr


def test_a_tampered_byte_fails_with_one_reason_and_places_nothing(staged):
    site, deps, digests = staged
    _flip(site / "redislite/bin/falkordb.so")
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr
    assert "falkordb.so does not match the committed digest" in result.stderr
    assert not copy_dir(deps).exists()


def test_a_failure_leaves_an_earlier_copy_untouched(staged):
    """Never touching the target on failure means the previous copy stays, and
    the unit still runs bytes that matched: fail closed either way."""
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    before = tree_state(deps)
    (site / "redislite/bin/redis-cli").write_bytes(b"swapped")
    assert _check(site, deps, digests).returncode != 0
    assert tree_state(deps) == before


def test_a_version_mismatch_fails(tmp_path):
    site = fake_wheel(tmp_path / "site", version="0.12.0")
    digests = fake_digests(tmp_path / "d.txt", site, version="0.12.0")
    result = _check(site, tmp_path / "deps", digests)
    assert result.returncode != 0
    assert f"0.12.0 is installed but {PINNED_VERSION} is pinned" in result.stderr
    assert not (tmp_path / "deps").exists()


def test_the_pinned_version_is_read_from_the_requirements_file(tmp_path):
    site = fake_wheel(tmp_path / "site", version="9.9.9")
    digests = fake_digests(tmp_path / "d.txt", site, version="9.9.9")
    req = tmp_path / "req.txt"
    req.write_text("# comment\nfalkordblite==9.9.9 \\\n    --hash=sha256:" + "0" * 64 + "\n")
    result = _check(site, tmp_path / "deps", digests, req)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deps" / f"9.9.9-{FAKE_TAG}").is_dir()


def test_a_requirements_file_without_the_pin_fails(staged, tmp_path):
    site, deps, digests = staged
    req = tmp_path / "req.txt"
    req.write_text("redis==8.0.0\n")
    result = _check(site, deps, digests, req)
    assert result.returncode != 0
    assert "pins no falkordblite version" in result.stderr


def test_no_installed_wheel_fails(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    digests = tmp_path / "d.txt"
    digests.write_text("")
    result = _check(site, tmp_path / "deps", digests)
    assert result.returncode != 0
    assert "falkordblite is not installed" in result.stderr


def test_a_missing_engine_file_fails(staged):
    """The committed list is authoritative: every file it names must exist."""
    site, deps, digests = staged
    (site / "falkordblite.libs/libgomp-test.so.1").unlink()
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "libgomp-test.so.1 is missing" in result.stderr


def test_a_binary_without_the_execute_bit_fails(staged):
    """redis refuses a module without +x, and systemd cannot exec a server
    without it. The libraries are not executed, so they stay 0644."""
    site, deps, digests = staged
    (site / "redislite/bin/redis-server").chmod(0o644)
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "redis-server is not executable" in result.stderr


def test_an_installed_library_the_list_does_not_name_fails(staged):
    """The module would load it but nothing would copy or check it."""
    site, deps, digests = staged
    (site / "falkordblite.libs/libsneaky.so").write_bytes(b"x")
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "libsneaky.so is not in the committed digests" in result.stderr


def test_no_digests_for_the_installed_tag_fails(tmp_path):
    site = fake_wheel(tmp_path / "site", tag="cp399-cp399-manylinux_2_38_riscv64")
    digests = fake_digests(tmp_path / "d.txt", site)  # rows for FAKE_TAG only
    result = _check(site, tmp_path / "deps", digests)
    assert result.returncode != 0
    assert (
        f"no committed digests for falkordblite {PINNED_VERSION} "
        "cp399-cp399-manylinux_2_38_riscv64" in result.stderr
    )


def test_the_tag_with_digests_is_chosen_among_several(tmp_path):
    site = fake_wheel(tmp_path / "site")
    wheel = next(site.glob("falkordblite-*.dist-info")) / "WHEEL"
    wheel.write_text(f"Wheel-Version: 1.0\nTag: py3-none-other\nTag: {FAKE_TAG}\n")
    digests = fake_digests(tmp_path / "d.txt", site)
    result = _check(site, tmp_path / "deps", digests)
    assert result.returncode == 0, result.stderr
    assert _keys(result.stdout)["FALKORDB_ENGINE_DIR"] == str(copy_dir(tmp_path / "deps"))


def test_digests_that_drop_a_binary_fail(staged):
    """A list that no longer names a binary would verify nothing about it."""
    site, deps, digests = staged
    digests.write_text(
        "".join(
            ln + "\n"
            for ln in digests.read_text().splitlines()
            if not ln.endswith("redislite/bin/redis-cli")
        )
    )
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "list no redislite/bin/redis-cli" in result.stderr


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (f"{PINNED_VERSION} {FAKE_TAG} {'0' * 64}", "is not '<version> <tag>"),
        (f"{PINNED_VERSION} {FAKE_TAG} {'z' * 64} redislite/bin/redis-cli", "malformed sha256"),
        (f"{PINNED_VERSION} {FAKE_TAG} {'0' * 64} ../etc/passwd", "outside the engine"),
        (f"{PINNED_VERSION} {FAKE_TAG} {'0' * 64} falkordblite.libs/../x", "outside the engine"),
        (f"{PINNED_VERSION} {FAKE_TAG} {'0' * 64} redislite/__init__.py", "outside the engine"),
        (f"{PINNED_VERSION} ../up {'0' * 64} redislite/bin/redis-cli", "unusable version"),
    ],
)
def test_a_malformed_digest_line_fails(staged, line, reason):
    site, deps, digests = staged
    fake_digests(digests, site, extra=line)
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert reason in result.stderr


@pytest.mark.parametrize("bad", ['quo"te', "back\\slash", "dol$lar", "dol${HOME}lar", "new\nline"])
def test_a_deps_root_the_unit_cannot_carry_fails(tmp_path, bad):
    """systemd refuses `"`, `\\` and control characters in an executable, and
    substitutes `$VAR`/`${VAR}` inside arguments (MEASURED)."""
    site = fake_wheel(tmp_path / "site")
    digests = fake_digests(tmp_path / "d.txt", site)
    deps = tmp_path / bad / "deps"
    result = _check(site, deps, digests)
    assert result.returncode != 0
    assert "characters the unit cannot carry" in result.stderr
    assert not deps.exists()


# --- leftovers -------------------------------------------------------------------


def test_a_crash_mid_copy_leaves_a_dotted_dir_the_next_run_removes(staged):
    site, deps, digests = staged
    dead = _dead_pid()
    name = copy_dir(deps).name
    crashed = deps / f".{name}.tmp-{dead}"
    (crashed / "redislite/bin").mkdir(parents=True)
    (crashed / "redislite/bin/redis-se").write_bytes(b"half")
    stale = deps / f".{name}.stale-{dead}"
    stale.mkdir()
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in deps.iterdir()) == [name]


_MARK = 1_000_000_000  # an mtime no file this run writes can have


def _mark(path: Path) -> None:
    """Tag the copy's file so a put-back copy is told from a rebuilt one (an
    inode number cannot tell them apart: a freed one is reused at once)."""
    os.utime(path, (_MARK, _MARK))


def _marked(path: Path) -> bool:
    return path.lstat().st_mtime == _MARK


@pytest.mark.parametrize("kind", ["tmp", "stale"])
def test_a_run_killed_between_the_renames_gets_its_copy_back(staged, kind):
    """Killed after moving the old copy aside and before installing the new one:
    <name> is absent. The finished copy is put back, not deleted and rebuilt, so
    a rebuild that also failed could not leave the unit's path empty."""
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    target = copy_dir(deps)
    module = target / "redislite/bin/falkordb.so"
    _mark(module)
    target.rename(deps / f".{target.name}.{kind}-{_dead_pid()}")
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert "FALKORDB_REPLACED" not in result.stdout
    assert _marked(module), "deleted and rebuilt instead of put back"
    assert sorted(p.name for p in deps.iterdir()) == [target.name]


def test_a_half_written_replacement_is_not_put_back_and_the_old_copy_is(staged):
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    target = copy_dir(deps)
    dead = _dead_pid()
    target.rename(deps / f".{target.name}.stale-{dead}")
    half = deps / f".{target.name}.tmp-{dead}"
    (half / "redislite/bin").mkdir(parents=True)
    (half / "redislite/bin/redis-se").write_bytes(b"half")
    stale_module = deps / f".{target.name}.stale-{dead}" / "redislite/bin/falkordb.so"
    _mark(stale_module)
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert _marked(target / "redislite/bin/falkordb.so")
    assert sorted(p.name for p in deps.iterdir()) == [target.name]


@pytest.mark.parametrize(
    "rel", ["redislite/bin/falkordb.so", "engine.sha256", "redislite/bin", "."]
)
def test_a_copy_another_account_could_write_is_rebuilt(staged, rel):
    """Writable by group or others, its files and digest could be changed
    together after this check, so it is not reused: rebuilt with exact modes."""
    site, deps, digests = staged
    assert _check(site, deps, digests).returncode == 0
    entry = copy_dir(deps) / rel
    entry.chmod(entry.stat().st_mode | 0o020)
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert "FALKORDB_REPLACED=1" in result.stdout.splitlines()
    assert not (copy_dir(deps) / rel).stat().st_mode & 0o022
    # Negative control: the rebuilt copy is reused as it stands.
    again = _check(site, deps, digests)
    assert "FALKORDB_REPLACED" not in again.stdout


def test_only_dead_leftovers_of_the_two_patterns_are_removed(staged):
    """A live process's temp dir is its own; other dotted names are not ours."""
    site, deps, digests = staged
    name = copy_dir(deps).name
    deps.mkdir()
    live = deps / f".{name}.tmp-{os.getpid()}"
    live.mkdir()
    other = deps / ".something-else"
    other.mkdir()
    plain = deps / "unrelated.tmp-1"
    plain.mkdir()
    result = _check(site, deps, digests)
    assert result.returncode == 0, result.stderr
    assert live.exists() and other.exists() and plain.exists()


# --- generate --------------------------------------------------------------------


def _fake_whl(path: Path, version: str = PINNED_VERSION, tag: str = FAKE_TAG) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        info = f"falkordblite-{version}.dist-info"
        archive.writestr(f"{info}/METADATA", f"Name: falkordblite\nVersion: {version}\n")
        archive.writestr(f"{info}/WHEEL", f"Wheel-Version: 1.0\nTag: {tag}\n")
        for rel in ENGINE_FILES:
            archive.writestr(rel, f"wheel {rel}\n")
        archive.writestr("redislite/__init__.py", "")
    return path


def _generate(req: Path, *wheels: Path):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(CHECK),
            "--generate",
            "--requirements",
            str(req),
            *map(str, wheels),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_generate_writes_digests_the_check_accepts(tmp_path):
    whl = _fake_whl(tmp_path / "x.whl")
    req = tmp_path / "req.txt"
    sha = hashlib.sha256(whl.read_bytes()).hexdigest()
    req.write_text(f"falkordblite=={PINNED_VERSION} \\\n    --hash=sha256:{sha}\n")
    result = _generate(req, whl)
    assert result.returncode == 0, result.stderr
    rows = [ln for ln in result.stdout.splitlines() if ln and not ln.startswith("#")]
    assert len(rows) == len(ENGINE_FILES)
    # The same files as a wheel installed from it: the check accepts them.
    site = tmp_path / "site"
    with zipfile.ZipFile(whl) as archive:
        archive.extractall(site)
    for rel, is_binary in ENGINE_FILES.items():
        (site / rel).chmod(0o755 if is_binary else 0o644)
    digests = tmp_path / "d.txt"
    digests.write_text(result.stdout)
    check = _check(site, tmp_path / "deps", digests, req)
    assert check.returncode == 0, check.stderr


def test_generate_refuses_a_wheel_that_is_not_pinned(tmp_path):
    whl = _fake_whl(tmp_path / "x.whl")
    req = tmp_path / "req.txt"
    req.write_text(f"falkordblite=={PINNED_VERSION} \\\n    --hash=sha256:{'0' * 64}\n")
    result = _generate(req, whl)
    assert result.returncode != 0
    assert "is not one of the pinned wheel files" in result.stderr


# --- the committed file ------------------------------------------------------------


def test_the_committed_digests_cover_every_pinned_wheel_at_the_pin():
    """One row per engine file for each of the six wheels the requirements pin
    (cp312/cp313/cp314 x x86_64/aarch64), all at the pinned version: moving the
    pin without regenerating this file fails here."""
    sys.path.insert(0, str(CHECK.parent))
    try:
        import falkordb_engine_check as check
    finally:
        sys.path.pop(0)
    table = check.read_digests(DIGESTS)
    expected_tags = {
        f"cp3{minor}-cp3{minor}-manylinux_2_38_{arch}"
        for minor in (12, 13, 14)
        for arch in ("x86_64", "aarch64")
    }
    assert {version for version, _ in table} == {PINNED_VERSION}
    assert {tag for _, tag in table} == expected_tags
    for key, rows in table.items():
        names = sorted(rows)
        assert names[:3] == sorted(r for r in names if r.startswith("falkordblite.libs/")), key
        assert set(names) >= {
            "redislite/bin/redis-server",
            "redislite/bin/redis-cli",
            "redislite/bin/falkordb.so",
        }, key
        assert len(names) == 6, key
    header = [ln for ln in DIGESTS.read_text().splitlines() if ln.startswith("#")]
    assert any("--generate" in ln for ln in header), "the header names the command"
    assert not re.search(r"/home/|/Users/", DIGESTS.read_text()), "a private path in the header"
