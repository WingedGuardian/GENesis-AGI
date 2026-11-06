"""A rollback puts back the exact pre-update packages, not a fresh resolve.

On 2026-10-31 an update installed FastMCP 4, whose modules live in a second
distribution (fastmcp-slim); pip installed slim's files and then uninstalled
fastmcp 2, deleting the same paths. The rollback then ran `pip install -e` on
the old checkout, which re-resolved its version RANGES to fastmcp 2.14.7 with
mcp 1.30.0 (a pair that cannot import together), left slim's files behind, and
reported "Rolled back" over a server that could not import FastMCP.

These tests rebuild that split OFFLINE: hand-made wheels (a zip with METADATA,
WHEEL and RECORD; no build backend, no network) installed into a stdlib venv by
its bundled pip, which sees only a local wheel directory. A fake `genesis`
package imports FastMCP from its `mcp.health` module, as the real one does, so
the shipped import check fails exactly where the real server did.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_scripts._update_source import UPDATE_SH, update_text

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"
VENV_SETUP = REPO_ROOT / "scripts" / "lib" / "venv_setup.sh"
HELPERS = ("venv_state.py", "venv_missing_files.py")


def _wheel(
    out: Path, name: str, version: str, files: dict[str, str], requires: tuple[str, ...] = ()
) -> Path:
    """Write a minimal valid wheel: pip installs it as-is."""
    dist = f"{name.replace('-', '_')}-{version}"
    info = f"{dist}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {r}\n" for r in requires
    )
    entries = dict(files)
    entries[f"{info}/METADATA"] = meta
    entries[f"{info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    rows = []
    for path, text in entries.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        )
        rows.append([path, f"sha256={digest}", str(len(text.encode()))])
    rows.append([f"{info}/RECORD", "", ""])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    entries[f"{info}/RECORD"] = buf.getvalue()
    path = out / f"{dist}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name_, text in entries.items():
            zf.writestr(name_, text)
    return path


@pytest.fixture(scope="module")
def wheels(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wheels")
    fast = "class FastMCP:\n    pass\n"
    _wheel(out, "fakemcp", "1.0", {"fakemcp/__init__.py": f"VERSION = '1.0'\n{fast}"})
    # 2.0 is a meta package; its modules moved into fakemcp-slim (the FastMCP 4 shape).
    _wheel(out, "fakemcp-slim", "2.0", {"fakemcp/__init__.py": f"VERSION = '2.0'\n{fast}"})
    _wheel(out, "fakemcp", "2.0", {}, requires=("fakemcp-slim==2.0",))
    _wheel(out, "extra", "1.0", {"extra_mod/__init__.py": ""})
    _wheel(out, "added", "1.0", {"added_mod/__init__.py": ""})
    # A build requirement the venv does not carry, as setuptools is absent from a
    # live venv: only an isolated build (which installs it) can build the checkout.
    _wheel(out, "fakebuildreq", "1.0", {"fakebuildreq/__init__.py": ""})
    _wheel(
        out,
        "fakegenesis",
        "1.0",
        {
            "genesis/__init__.py": "",
            "genesis/runtime.py": "class GenesisRuntime:\n    pass\n",
            "genesis/mcp/__init__.py": "",
            "genesis/mcp/health.py": "from fakemcp import FastMCP  # noqa: F401\n",
        },
    )
    return out


def _env(wheels: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheels), PIP_DISABLE_PIP_VERSION_CHECK="1")
    env.pop("PYTHONPATH", None)
    env["GENESIS_HOME"] = str(Path(env.get("TMPDIR", "/tmp")))  # the lock dir; overridden per test
    return env


def _repo(tmp_path: Path) -> Path:
    """A stand-in checkout: only the helpers restore_venv_state calls by path."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    for helper in HELPERS:
        shutil.copy(REPO_ROOT / "scripts" / "lib" / helper, repo / "scripts" / "lib" / helper)
    return repo


def _pip(venv: Path, env: dict, *args: str) -> None:
    subprocess.run(
        [str(venv / "bin" / "pip"), *args, "-q"], env=env, check=True, capture_output=True
    )


def _imports(venv: Path, env: dict) -> bool:
    code = "from genesis.runtime import GenesisRuntime; import genesis.mcp.health"
    return (
        subprocess.run(
            [str(venv / "bin" / "python"), "-c", code], env=env, capture_output=True
        ).returncode
        == 0
    )


def _installed(venv: Path, env: dict) -> dict[str, str]:
    out = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


@pytest.fixture
def setup(tmp_path, wheels):
    """A working pre-update venv: fakemcp 1.0, extra 1.0, fakegenesis 1.0."""
    env = _env(wheels)
    env["GENESIS_HOME"] = str(tmp_path / "gh")
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, env=env)
    _pip(venv, env, "install", "fakemcp==1.0", "extra==1.0", "fakegenesis==1.0")
    assert _imports(venv, env)
    return venv, env, _repo(tmp_path), tmp_path / "gh" / "update-deps" / "pre-update-x.json"


def _sh(func: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    script = f'set -Eeuo pipefail\n. "{VENV_SETUP}"\n{func} "$@"\n'
    return subprocess.run(
        ["bash", "-c", script, "sh", *args], env=env, capture_output=True, text=True
    )


def _break_like_the_incident(venv: Path, env: dict, shape: str = "split-upgrade") -> None:
    """The failed update, with a removed package and an added one, in one of two
    shapes:
    - split-upgrade: FastMCP's 2 -> 4 move (fakemcp 2.0 pulls fakemcp-slim; pip
      then deletes the module files the old distribution shares with slim);
    - slim-added: slim arrives while fakemcp stays at the recorded 1.0. Removing
      slim on restore then deletes files that 1.0 owns, and pip skips 1.0 as
      already satisfied: only the damage heal puts them back."""
    if shape == "split-upgrade":
        _pip(venv, env, "install", "fakemcp==2.0", "added==1.0")
    else:
        _pip(venv, env, "install", "--no-deps", "fakemcp-slim==2.0", "added==1.0")
    _pip(venv, env, "uninstall", "-y", "extra")
    if shape == "split-upgrade":
        assert not _imports(venv, env), "fixture: the split upgrade must break the import"


# -- the incident ------------------------------------------------------------


@pytest.mark.parametrize("shape", ["split-upgrade", "slim-added"])
def test_restore_puts_back_the_exact_pre_update_set(setup, shape):
    venv, env, repo, record = setup
    before = _installed(venv, env)
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env, shape)
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert _installed(venv, env) == before, (
        "exact set: added removed, removed restored, versions back"
    )
    assert _imports(venv, env)


def test_without_the_damage_heal_the_slim_case_stays_broken(setup):
    """Control: in the slim-added shape the restore's own removal of fakemcp-slim
    deletes files the recorded (and still installed) fakemcp 1.0 owns. With the
    heal disabled the import stays broken and the restore must refuse, so the
    passing test above is the heal working, not the shape being harmless."""
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env, "slim-added")
    res = _sh(
        "reinstall_damaged_distributions() { :; }; _restore_venv_state_unlocked",
        env,
        str(repo),
        str(venv),
        str(record),
    )
    assert res.returncode == 1, res.stdout
    assert not _imports(venv, env)
    assert "still have missing files after the repair" in res.stdout
    assert "fakemcp==1.0" in res.stdout


def test_a_version_no_longer_available_is_a_refusal_that_changes_nothing(setup, wheels, tmp_path):
    """Offline with the old version gone from every source: reported, not masked,
    and refused BEFORE anything is removed. Removing the update's packages and
    then failing to fetch the old ones would leave neither set (round-3 audit)."""
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env)
    after_update = _installed(venv, env)
    partial = tmp_path / "partial-wheels"
    shutil.copytree(wheels, partial)
    for whl in partial.glob("extra-1.0-*"):
        whl.unlink()
    env = dict(env, PIP_FIND_LINKS=str(partial))
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 1
    assert "could not be fetched, so nothing was changed" in res.stdout, res.stdout + res.stderr
    assert _installed(venv, env) == after_update, "the update's set must be left exactly as it was"


def test_a_failed_repair_of_a_package_the_import_never_touches_is_not_a_success(setup):
    """Devin, #3830: verify reads metadata and the import probe covers only the
    core, so with extra's files gone and the repair failing, the restore reported
    success and the record was discarded."""
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    module = next(venv.glob("lib/python*/site-packages/extra_mod/__init__.py"))
    module.unlink()
    res = _sh(
        "reinstall_damaged_distributions() { :; }; _restore_venv_state_unlocked",
        env,
        str(repo),
        str(venv),
        str(record),
    )
    assert _imports(venv, env), "fixture: the core import must still pass"
    assert res.returncode == 1, res.stdout
    assert "still have missing files after the repair" in res.stdout
    assert "extra==1.0" in res.stdout


def test_the_download_sits_under_the_swept_tmp_and_is_removed(setup):
    """Devin and CodeRabbit, #3830: a killed restore left wheels in update-deps,
    which no sweep reaches. ~/tmp is swept by prune_tmp."""
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env)
    home = Path(env["HOME"])
    log = home / "mktemp.log"
    shim = f'mktemp() {{ echo "$*" >> "{log}"; command mktemp "$@"; }}; restore_venv_state'
    res = _sh(shim, env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"-d -p {home}/tmp genesis-restore." in log.read_text()
    assert not list((home / "tmp").glob("genesis-restore.*")), "removed on the normal path"
    assert not [p for p in record.parent.iterdir() if p.name.startswith("tmp.")]


def test_only_what_was_fetched_is_installed(setup):
    """CodeRabbit, #3830: installing the whole record after the removals could
    fetch a direct-URL entry that already matched, past the point of refusal."""
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env)
    # pip's arguments, and the requirements file as it stood when pip ran.
    real = venv / "bin" / "pip"
    moved = venv / "bin" / "pip.real"
    real.rename(moved)
    log = Path(env["HOME"]) / "pip.log"
    real.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = install ]; then for a in "$@"; do [ -f "$a" ] && cat "$a" >> "{log}"; done; fi\n'
        f'exec "{moved}" "$@"\n'
    )
    real.chmod(0o755)
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert sorted(log.read_text().split()) == ["extra==1.0", "fakemcp==1.0"]


def test_missing_names_only_what_is_not_installed_as_recorded(setup):
    venv, env, repo, record = setup
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    assert _state(venv, env, "missing", str(record)).stdout == ""
    _break_like_the_incident(venv, env)
    res = _state(venv, env, "missing", str(record))
    assert res.returncode == 0, res.stderr
    assert sorted(res.stdout.split()) == ["extra==1.0", "fakemcp==1.0"]


def test_the_restore_runs_the_helpers_copied_with_the_record(setup):
    """At rollback the checkout is back at the old tag, which may predate these
    helpers: the restore must not need the checkout's copy."""
    venv, env, repo, record = setup
    before = _installed(venv, env)
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    lib = record.with_suffix(".lib")
    assert sorted(p.name for p in lib.iterdir()) == sorted(HELPERS)
    assert oct(lib.stat().st_mode & 0o777) == "0o700"
    _break_like_the_incident(venv, env)
    shutil.rmtree(repo / "scripts")  # the old tag has neither helper
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert _installed(venv, env) == before and _imports(venv, env)


def test_a_failed_capture_leaves_neither_record_nor_helper_copy(setup):
    venv, env, repo, record = setup
    (repo / "scripts" / "lib" / "venv_missing_files.py").unlink()
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 1
    assert not record.exists() and not record.with_suffix(".lib").exists()


def test_a_broken_venv_is_never_recorded_as_pre_update(setup):
    venv, env, repo, record = setup
    _break_like_the_incident(venv, env)
    res = _sh("capture_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 1 and "not recorded" in res.stdout
    assert not record.exists()


def test_editables_are_recorded_by_name_and_never_touched(setup):
    """A dangling editable (its source directory gone) must survive a restore."""
    venv, env, repo, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    info = site / "dangling-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: dangling\nVersion: 0.1\n")
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(
        json.dumps({"url": "file:///gone", "dir_info": {"editable": True}})
    )
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    assert json.loads(record.read_text())["editable"] == ["dangling"]
    _break_like_the_incident(venv, env)
    assert _sh("restore_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    assert info.is_dir()


# -- the checkout's own editable install -------------------------------------

# A PEP 660 backend whose one build requirement is a local wheel, so pip's
# isolated build works offline: the editable wheel is a .pth onto src/ and a
# dist-info at VERSION.
_BACKEND = """
import base64, hashlib, os, zipfile

import fakebuildreq  # noqa: F401  (present only in an isolated build)

def _version():
    with open("VERSION") as fh:
        return fh.read().strip()

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    if os.path.exists("BROKEN"):
        raise RuntimeError("build backend broken")
    v = _version()
    info = f"genesis_v3-{v}.dist-info"
    files = {
        "genesis_v3.pth": os.path.abspath("src") + "\\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\\nName: genesis-v3\\nVersion: {v}\\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
    }
    rows = []
    for p, t in files.items():
        d = base64.urlsafe_b64encode(hashlib.sha256(t.encode()).digest()).rstrip(b"=").decode()
        rows.append(f"{p},sha256={d},{len(t.encode())}")
    files[f"{info}/RECORD"] = "\\n".join(rows + [f"{info}/RECORD,,"]) + "\\n"
    name = f"genesis_v3-{v}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as zf:
        for p, t in files.items():
            zf.writestr(p, t)
    return name
"""


def _set_checkout_version(repo: Path, version: str) -> None:
    """The tracked VERSION, and the untracked egg-info setuptools leaves in src/
    (a checkout rollback does not touch it, so it keeps the update's version)."""
    (repo / "VERSION").write_text(version + "\n")
    egg = repo / "src" / "genesis_v3.egg-info"
    egg.mkdir(exist_ok=True)
    (egg / "PKG-INFO").write_text(f"Metadata-Version: 2.1\nName: genesis-v3\nVersion: {version}\n")


def _editable_versions(venv: Path) -> list[str]:
    site = next((venv / "lib").glob("python3*/site-packages"))
    return sorted(p.name for p in site.glob("genesis_v3-*.dist-info"))


@pytest.fixture
def checkout(tmp_path, wheels):
    """Like the live install: Genesis is an editable install of the checkout, and
    importlib.metadata sees it twice (the dist-info, and src/genesis_v3.egg-info
    through the editable .pth)."""
    env = _env(wheels)
    env["GENESIS_HOME"] = str(tmp_path / "gh")
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "genesis-v3"\ndynamic = ["version"]\n'
        '[build-system]\nrequires = ["fakebuildreq"]\nbuild-backend = "backend"\nbackend-path = ["."]\n'
    )
    (repo / "backend.py").write_text(_BACKEND)
    pkg = repo / "src" / "genesis"
    (pkg / "mcp").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "runtime.py").write_text("class GenesisRuntime:\n    pass\n")
    (pkg / "mcp" / "__init__.py").write_text("")
    (pkg / "mcp" / "health.py").write_text("from fakemcp import FastMCP  # noqa: F401\n")
    _set_checkout_version(repo, "1.0")
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, env=env)
    _pip(venv, env, "install", "fakemcp==1.0", "extra==1.0")
    _pip(venv, env, "install", "--no-deps", "-e", str(repo))
    assert _imports(venv, env) and _editable_versions(venv) == ["genesis_v3-1.0.dist-info"]
    return venv, env, repo, tmp_path / "gh" / "update-deps" / "pre-update-x.json"


def test_the_checkout_is_recorded_as_editable_only_never_pinned(checkout):
    venv, env, repo, record = checkout
    names = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import importlib.metadata as m; print([d.metadata['Name'] for d in m.distributions()])",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert names.count("genesis-v3") == 2, "fixture: the checkout must be seen twice, as live"
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    rec = json.loads(record.read_text())
    assert rec["editable"] == ["genesis-v3"] and "genesis-v3" not in rec["pinned"]


def test_a_rollback_across_a_version_bump_restores_and_repoints(checkout):
    """The update bumped the checkout's version and re-installed it; the rollback
    moved the checkout back. A record pinning genesis-v3==1.0 would fail the whole
    requirements install here (no source carries that package)."""
    venv, env, repo, record = checkout
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env)
    _set_checkout_version(repo, "2.0")
    _pip(venv, env, "install", "--no-deps", "-e", str(repo))
    assert _editable_versions(venv) == ["genesis_v3-2.0.dist-info"]
    (repo / "VERSION").write_text("1.0\n")  # the checkout rollback; egg-info stays at 2.0
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "WARNING" not in res.stdout
    assert _editable_versions(venv) == ["genesis_v3-1.0.dist-info"]
    assert _state(venv, env, "verify", str(record)).returncode == 0
    assert _imports(venv, env)


def test_a_repoint_that_cannot_build_is_reported_and_keeps_the_install(checkout):
    venv, env, repo, record = checkout
    assert _sh("capture_venv_state", env, str(repo), str(venv), str(record)).returncode == 0
    _break_like_the_incident(venv, env)
    (repo / "BROKEN").write_text("")
    res = _sh("restore_venv_state", env, str(repo), str(venv), str(record))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "install metadata was not refreshed" in res.stdout
    assert _editable_versions(venv) == ["genesis_v3-1.0.dist-info"] and _imports(venv, env)


# -- venv_state.py contract --------------------------------------------------


def _state(venv: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-P",
            str(REPO_ROOT / "scripts" / "lib" / "venv_state.py"),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
    )


def test_verify_names_every_difference(setup):
    venv, env, repo, record = setup
    assert _state(venv, env, "capture", str(record)).returncode == 0
    _pip(venv, env, "install", "added==1.0")
    _pip(venv, env, "uninstall", "-y", "extra")
    res = _state(venv, env, "verify", str(record))
    assert res.returncode == 1
    assert "missing extra==1.0" in res.stdout and "extra added==1.0" in res.stdout
    assert _state(venv, env, "extras", str(record)).stdout.split() == ["added"]
    _pip(venv, env, "install", "--no-deps", "fakemcp==2.0")
    res = _state(venv, env, "verify", str(record))
    assert "changed fakemcp==1.0 -> fakemcp==2.0" in res.stdout, res.stdout


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{}",
        '{"pinned": {}, "editable": []}',
        "not json",
        '{"pinned": {"a": "a==1.0"}, "editable": []}',
        '{"pinned": {"a": "a==1.0"}, "editable": [], "versions": {}}',
        '{"pinned": {"a": "a==1.0"}, "editable": [], "versions": {"a": "1.0 x"}}',
    ],
)
def test_an_unusable_record_is_an_error_not_a_pass(setup, content):
    venv, env, _, record = setup
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(content)
    for cmd in ("verify", "extras", "requirements"):
        assert _state(venv, env, cmd, str(record)).returncode == 2, cmd


@pytest.mark.parametrize(
    "pinned",
    [
        {"a": "a==1.0\n--index-url=http://example.invalid"},
        {"a": "--index-url=http://example.invalid"},
        {"a": "b==1.0"},
        {"a": "a>=1.0"},
        {"a": 1},
        {"x": "x==1.0", "dup": "dup==1.0"},
    ],
)
def test_a_record_entry_that_is_not_a_pin_is_refused(setup, pinned):
    """Every line handed to pip must be `name==version` or `name @ url` for the
    entry's own name; an editable name is never also pinned."""
    venv, env, _, record = setup
    record.parent.mkdir(parents=True, exist_ok=True)
    editable = ["dup"] if "dup" in pinned else []
    versions = {k: "1.0" for k in pinned}
    record.write_text(json.dumps({"pinned": pinned, "editable": editable, "versions": versions}))
    assert _state(venv, env, "requirements", str(record)).returncode == 2


@pytest.mark.parametrize("dir_info", [{}, {"editable": False}])
def test_a_local_directory_install_is_recorded_by_its_url_never_by_version(setup, dir_info):
    """Pinned as name==version it would be fetched from an index, which may carry
    a different package of that name. pip writes an empty dir_info; an explicit
    editable false is the same install."""
    venv, env, _, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    info = site / "inhouse-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: inhouse\nVersion: 0.1\n")
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(
        json.dumps({"url": "file:///src/inhouse", "dir_info": dir_info})
    )
    assert _state(venv, env, "capture", str(record)).returncode == 0
    reqs = _state(venv, env, "requirements", str(record)).stdout.splitlines()
    assert "inhouse @ file:///src/inhouse" in reqs and "inhouse==0.1" not in reqs


def test_an_extra_whose_name_is_not_plain_is_refused(setup):
    """extras feeds an unquoted pip uninstall: a glob or an option never reaches it."""
    venv, env, _, record = setup
    assert _state(venv, env, "capture", str(record)).returncode == 0
    site = next((venv / "lib").glob("python3*/site-packages"))
    info = site / "odd-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: -y*\nVersion: 0.1\n")
    (info / "RECORD").write_text("")
    res = _state(venv, env, "extras", str(record))
    assert res.returncode == 2 and res.stdout == ""


def test_a_capture_that_cannot_land_leaves_no_temporary_file(setup):
    """The record's final path is a directory, so the rename fails: an error
    (exit 2), and the temporary file is removed rather than left to pile up."""
    venv, env, _, record = setup
    record.mkdir(parents=True)
    res = _state(venv, env, "capture", str(record))
    assert res.returncode == 2
    assert sorted(p.name for p in record.parent.iterdir()) == [record.name]


def _fake_dist(site: Path, name: str, version: str, direct: dict) -> None:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(json.dumps(direct))


def test_a_subdirectory_selector_is_kept(setup):
    """A package inside a repository or archive: without its selector the
    restore would try to build the root."""
    venv, env, _, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    _fake_dist(
        site,
        "insub",
        "0.1",
        {
            "url": "https://example.invalid/r.git",
            "subdirectory": "pkg/insub",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        },
    )
    _fake_dist(
        site,
        "arcsub",
        "0.2",
        {"url": "https://example.invalid/a.tar.gz", "subdirectory": "py", "archive_info": {}},
    )
    assert _state(venv, env, "capture", str(record)).returncode == 0
    reqs = _state(venv, env, "requirements", str(record)).stdout.splitlines()
    assert "insub @ git+https://example.invalid/r.git@abc123#subdirectory=pkg/insub" in reqs
    assert "arcsub @ https://example.invalid/a.tar.gz#subdirectory=py" in reqs


@pytest.mark.parametrize(
    ("version", "direct"),
    [
        ("", {"url": "https://example.invalid/n.tar.gz", "archive_info": {}}),
        (
            "0.1",
            {
                "url": "https://example.invalid/r.git",
                "subdirectory": "a b",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            },
        ),
    ],
)
def test_a_record_capture_could_not_read_back_is_a_capture_failure(setup, version, direct):
    """No Version in the metadata, or a selector pip cannot express: capture
    refuses (the update warns and keeps the old re-resolve) rather than writing
    a record the rollback would refuse with the services stopped."""
    venv, env, _, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    info = site / "odd-0.dist-info"
    info.mkdir()
    meta = "Metadata-Version: 2.1\nName: odd\n" + (f"Version: {version}\n" if version else "")
    (info / "METADATA").write_text(meta)
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(json.dumps(direct))
    res = _state(venv, env, "capture", str(record))
    assert res.returncode == 2, res.stdout + res.stderr
    assert not record.exists()
    assert not list(record.parent.glob("*.tmp")) if record.parent.exists() else True


def test_a_changed_version_behind_the_same_url_is_not_a_match(setup):
    """A local directory installed without -e: the update rebuilt it at 2.0 from
    the same path. Same URL, different package: verify must say so."""
    venv, env, _, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    direct = {"url": "file:///src/inhouse", "dir_info": {}}
    _fake_dist(site, "inhouse", "1.0", direct)
    assert _state(venv, env, "capture", str(record)).returncode == 0
    assert _state(venv, env, "verify", str(record)).returncode == 0
    shutil.rmtree(site / "inhouse-1.0.dist-info")
    _fake_dist(site, "inhouse", "2.0", direct)
    res = _state(venv, env, "verify", str(record))
    assert res.returncode == 1 and "changed inhouse 1.0 -> 2.0" in res.stdout, res.stdout


def test_a_vcs_install_is_recorded_with_its_commit(setup):
    venv, env, _, record = setup
    site = next((venv / "lib").glob("python3*/site-packages"))
    info = site / "fromgit-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fromgit\nVersion: 0.1\n")
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": "https://example.invalid/r.git",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            }
        )
    )
    assert _state(venv, env, "capture", str(record)).returncode == 0
    reqs = _state(venv, env, "requirements", str(record)).stdout.splitlines()
    assert "fromgit @ git+https://example.invalid/r.git@abc123" in reqs


# -- update.sh wiring --------------------------------------------------------


def _block(marker: str) -> str:
    m = re.search(rf"# BEGIN {marker}[^\n]*\n(.*?)# END {marker}", update_text(), re.DOTALL)
    assert m, marker
    return m.group(1)


def _rollback_deps(tmp_path: Path, taken: int, restore_rc: int, pip_rc: int, import_rc: int) -> str:
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "pip").write_text(f"#!/bin/sh\necho PIP-RESOLVE\nexit {pip_rc}\n")
    (venv / "bin" / "python").write_text(f"#!/bin/sh\nexit {import_rc}\n")
    for f in ("pip", "python"):
        (venv / "bin" / f).chmod(0o755)
    script = (
        "set -Eeuo pipefail\n"
        f'GENESIS_ROOT="{tmp_path}"\nVENV_DIR="{venv}"\nDEPS_STATE_TAKEN={taken}\n'
        f'DEPS_STATE_FILE="{tmp_path}/rec.json"\nrestart_ok=true\n'
        f'restore_venv_state() {{ echo "RESTORE $3 WAIT=${{GENESIS_BROWSER_STACK_WAIT_S:-}}"; return {restore_rc}; }}\n'
        "f() {\n" + _block("rollback-deps") + '    echo "PIP_OK=$pip_ok"\n}\nf\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def test_the_rollback_restores_from_the_record_when_one_was_taken(tmp_path):
    out = _rollback_deps(tmp_path, taken=1, restore_rc=0, pip_rc=0, import_rc=0)
    assert (
        f"RESTORE {tmp_path}/rec.json" in out and "PIP-RESOLVE" not in out and "PIP_OK=true" in out
    )


def test_the_restore_waits_on_the_browser_lock_for_minutes_not_hours(tmp_path):
    out = _rollback_deps(tmp_path, taken=1, restore_rc=0, pip_rc=0, import_rc=0)
    assert "WAIT=600" in out


@pytest.mark.parametrize(
    ("pip_rc", "import_rc", "salvage"),
    [(0, 0, "Genesis imports"), (1, 0, "did not"), (0, 1, "did not")],
)
def test_a_failed_restore_fails_the_rollback_keeps_the_record_and_salvages(
    tmp_path, pip_rc, import_rc, salvage
):
    """The exact restore already removed what the update added; the old re-resolve
    then runs as salvage, and the rollback still reports incomplete."""
    out = _rollback_deps(tmp_path, taken=1, restore_rc=1, pip_rc=pip_rc, import_rc=import_rc)
    assert "PIP_OK=false" in out and "record kept" in out
    assert "PIP-RESOLVE" in out and "Salvage: re-" in out and salvage in out


def test_a_held_browser_lock_changes_nothing_and_skips_the_salvage(tmp_path):
    out = _rollback_deps(tmp_path, taken=1, restore_rc=75, pip_rc=0, import_rc=0)
    assert "PIP_OK=false" in out and "held its lock" in out and "record kept" in out
    assert "PIP-RESOLVE" not in out


@pytest.mark.parametrize(
    ("pip_rc", "import_rc", "ok"), [(0, 0, "true"), (1, 0, "false"), (0, 1, "false")]
)
def test_without_a_record_the_resolve_must_also_import(tmp_path, pip_rc, import_rc, ok):
    """The old path, which once reported success over a server that could not
    import FastMCP, now also has to import Genesis and its MCP stack."""
    out = _rollback_deps(tmp_path, taken=0, restore_rc=0, pip_rc=pip_rc, import_rc=import_rc)
    assert "PIP-RESOLVE" in out and f"PIP_OK={ok}" in out


@pytest.mark.parametrize(("capture_rc", "taken"), [(0, "1"), (1, "0")])
def test_capture_sets_the_flag_only_when_the_record_was_written(tmp_path, capture_rc, taken):
    script = (
        "set -Eeuo pipefail\n"
        f'GENESIS_HOME="{tmp_path}"\nROLLBACK_TAG=pre-update-x\nGENESIS_ROOT=/r\nVENV_DIR=/v\n'
        f'capture_venv_state() {{ : > "$3"; return {capture_rc}; }}\n'
        + _block("deps-capture")
        + 'echo "TAKEN=$DEPS_STATE_TAKEN"\n'
    )
    (tmp_path / "update-deps").mkdir()
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert f"TAKEN={taken}" in out


@pytest.mark.parametrize(
    ("post_merge", "existing", "captured", "taken"),
    [
        (
            "true",
            '{"pinned": {"a": "a==1.0"}, "editable": [], "versions": {"a": "1.0"}}',
            False,
            "1",
        ),
        ("true", None, True, "1"),
        ("true", "{}", True, "1"),
        (
            "false",
            '{"pinned": {"a": "a==1.0"}, "editable": [], "versions": {"a": "1.0"}}',
            True,
            "1",
        ),
    ],
)
def test_a_post_merge_keeps_the_record_taken_before_its_merge(
    tmp_path, post_merge, existing, captured, taken
):
    """--post-merge reuses the conflicted run's rollback tag; that run recorded the
    packages before merging. Re-capturing would check imports against the merged
    code and overwrite the pre-update record."""
    rec = tmp_path / "update-deps" / "pre-update-x.json"
    rec.parent.mkdir()
    if existing is not None:
        rec.write_text(existing)
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    script = (
        "set -Eeuo pipefail\n"
        f'GENESIS_HOME="{tmp_path}"\nROLLBACK_TAG=pre-update-x\nGENESIS_ROOT="{REPO_ROOT}"\n'
        f'VENV_DIR="{venv}"\nPOST_MERGE={post_merge}\n'
        'capture_venv_state() { echo CAPTURED; : > "$3"; }\n'
        + _block("deps-capture")
        + 'echo "TAKEN=$DEPS_STATE_TAKEN"\n'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert ("CAPTURED" in out) is captured and f"TAKEN={taken}" in out
    if not captured:
        assert rec.read_text() == existing


def test_disk_hygiene_ages_out_records_helper_copies_and_leftovers(tmp_path):
    """The prune lines as disk_hygiene.sh runs them: a record, its helper copy
    and a killed run's temporary files older than 30 days go; newer ones, and
    anything else in the directory, stay."""
    text = (REPO_ROOT / "scripts" / "disk_hygiene.sh").read_text()
    start = text.index("--- update package-record prune")
    body = text[text.index("\n", start) + 1 : text.index("\n\n", start)]
    deps = tmp_path / "update-deps"
    (deps / "old.lib").mkdir(parents=True)
    (deps / "new.lib").mkdir()
    old = [
        "old.json",
        "old.lib/venv_state.py",
        "old.lib/venv_missing_files.py",
        "x.tmp",
        "tmp.ab12",
    ]
    for name in [*old, "new.json", "new.lib/venv_state.py", "other.txt"]:
        (deps / name).write_text("")
    for name in [*old, "other.txt"]:
        os.utime(deps / name, (0, 0))
    subprocess.run(
        ["bash", "-c", f"f() {{\n{body}\n}}\nf"],
        env=dict(os.environ, GENESIS_HOME=str(tmp_path)),
        check=True,
    )
    left = sorted(str(p.relative_to(deps)) for p in deps.rglob("*"))
    assert left == ["new.json", "new.lib", "new.lib/venv_state.py", "other.txt"]


def _shell_fn(name: str) -> str:
    text = update_text()
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


def _record_fn() -> str:
    return _shell_fn("_retire_deps_record") + _shell_fn("_record_update_history")


def _nothing_new_exit_branch() -> str:
    """The "Already up to date" branch of update.sh, up to its exit."""
    text = UPDATE_SH.read_text()
    start = text.index("elif _nothing_new; then\n")
    return text[start : text.index("    exit 0\n", start)]


def test_the_already_up_to_date_exit_retires_the_record():
    """A no-op update takes the record before it knows nothing is new, and that
    branch writes an update_history row only for a stale marker or a
    degradation. So it retires the record itself, before any exit, or every
    clean no-op leaves one behind (measured on a disposable runner)."""
    branch = _nothing_new_exit_branch()
    assert "_retire_deps_record" in branch
    assert "exit" not in branch.split("_retire_deps_record", 1)[0]


@pytest.mark.parametrize(("taken", "kept"), [(1, False), (0, True)])
def test_retiring_removes_only_a_record_this_run_took(tmp_path, taken, kept):
    rec = tmp_path / "rec.json"
    rec.write_text("{}")
    (tmp_path / "rec.lib").mkdir()
    script = (
        f'DEPS_STATE_TAKEN={taken}\nDEPS_STATE_FILE="{rec}"\n'
        + _shell_fn("_retire_deps_record")
        + "\n_retire_deps_record\n"
    )
    subprocess.run(["bash", "-c", script], check=True)
    assert rec.exists() is kept
    assert (tmp_path / "rec.lib").exists() is kept


@pytest.mark.parametrize(
    ("status", "taken", "kept"),
    [
        ("success", 1, False),
        ("rolled_back", 1, False),
        ("failed", 1, True),
        ("conflicts_pending", 1, True),
        ("success", 0, True),
    ],
)
def test_the_record_is_retired_only_when_an_outcome_completes(tmp_path, status, taken, kept):
    """_record_update_history is where every outcome lands; a completed one
    (success, clean rollback) retires the record this run took, and nothing else
    does. No database here, so the function returns right after."""
    rec = tmp_path / "rec.json"
    rec.write_text("{}")
    (tmp_path / "rec.lib").mkdir()
    (tmp_path / "rec.lib" / "venv_state.py").write_text("")
    script = (
        f'GENESIS_ROOT="{tmp_path}"\nDEPS_STATE_TAKEN={taken}\nDEPS_STATE_FILE="{rec}"\n'
        + _record_fn()
        + f'\n_record_update_history {status} "" ""\n'
    )
    subprocess.run(["bash", "-c", script], check=True)
    assert rec.exists() is kept
    assert (tmp_path / "rec.lib").exists() is kept, "the helper copy goes with its record"
