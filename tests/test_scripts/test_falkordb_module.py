"""Provisioning, integrity and unit-wiring tests for falkordb_install.sh.

See falkordb_stubs.py for the harness. Covers the hash-pinned wheel install
(OS floor and arch, pip failure, the kill switch), the engine check's verdict
and its version-tag copy reaching the shell, the rule that never switches an
ACTIVE engine unasked, pruning the copies no unit runs, and the systemd unit's
render wiring, readiness and quoting.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.falkordb_stubs import (
    BOOTSTRAP,
    INSTALL,
    LIB,
    PINNED_VERSION,
    PYPROJECT,
    REPO_ROOT,
    REQUIREMENTS,
    UNIT_TEMPLATE,
    _provisioned,
    _report,
    _results,
    _run,
    _stage,
    copy_file,
    engine_dir,
    pip_calls,
    site_file,
)

# --- the pin -----------------------------------------------------------------

#: The six wheel digests (cp312/cp313/cp314 x aarch64/x86_64), duplicated on
#: purpose: changing one should fail here and make whoever changed it say why.
WHEEL_HASHES = {
    "538f38fc6e2b871ee6a173e4529603d5ac7886a5aec34fe8d5b327779d5d00d1",  # pragma: allowlist secret
    "2275de4aa726b792c0b1e48c81735e8538a472f85e6d74739951a395499fa359",  # pragma: allowlist secret
    "a913db722197344d741cad96f4ce712fe07d47abe027f388dc892d003bb361ca",  # pragma: allowlist secret
    "966bac2094e42615198655a2955b89812bc48f8885652de547a554ce8790138d",  # pragma: allowlist secret
    "dc34b7a53ce6bf9be52c7e2d594b32d886a6f511cc84db04e7ecf4a0dd753ed1",  # pragma: allowlist secret
    "bd50f5f2be35224be4604893ed54435f34a52f960e5f09087ad6da6f4749bba4",  # pragma: allowlist secret
}


def test_the_requirements_file_pins_every_wheel_by_hash():
    text = REQUIREMENTS.read_text()
    assert re.search(rf"^falkordblite=={re.escape(PINNED_VERSION)} \\$", text, re.M), text
    assert set(re.findall(r"--hash=sha256:([0-9a-f]{64})", text)) == WHEEL_HASHES


def test_pyproject_declares_the_same_pin_and_a_compatible_client():
    """The editable install must find the hashed install's version satisfied,
    and falkordblite needs falkordb[hiredis]>=1.8.0."""
    text = PYPROJECT.read_text()
    assert f'"falkordblite=={PINNED_VERSION}"' in text
    assert '"falkordb>=1.8.1,<2"' in text


def test_pip_is_asked_for_hashes_binaries_and_no_resolution(tmp_path):
    env = _stage(tmp_path)
    result = _run(_provisioned(), env)
    assert result.returncode == 0, result.stderr
    calls = pip_calls(env)
    assert len(calls) == 1, calls
    argv = calls[0].split()
    for flag in ("--only-binary=:all:", "--no-deps", "--require-hashes"):
        assert flag in argv, (flag, argv)
    assert argv[argv.index("-r") + 1] == str(REQUIREMENTS)


# --- provisioning ------------------------------------------------------------


def test_a_verified_engine_places_its_copy_and_exports_its_paths(tmp_path):
    """The unit runs the version-tag COPY, never the venv's files."""
    env = _stage(tmp_path)
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    got = _results(result.stdout)
    assert got["OK"] == "1", result.stdout
    assert got["DIR"] == str(engine_dir(env))
    assert got["SERVER"] == str(copy_file(env, "redislite/bin/redis-server"))
    assert got["CLI"] == str(copy_file(env, "redislite/bin/redis-cli"))
    assert got["MODULE"] == str(copy_file(env, "redislite/bin/falkordb.so"))
    assert got["DIGESTS"] == str(copy_file(env, "engine.sha256"))
    assert str(Path(env["FAKE_SITE"])) not in result.stdout.split("OK=", 1)[1]

    digest = Path(got["DIGESTS"])
    check = subprocess.run(
        ["sha256sum", "--quiet", "--strict", "-c", str(digest)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr
    # Every engine file, the libraries included: the module loads them.
    assert len(digest.read_text().splitlines()) == 5


def test_an_os_below_the_glibc_floor_skips_with_the_reason(tmp_path):
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = "glibc 2.35"
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    assert (
        "below the supported floor (Ubuntu 24.04 / glibc 2.38 on x86_64 or aarch64)"
        in result.stdout
    )
    assert "the graph engine cannot be installed" in result.stdout
    assert pip_calls(env) == [], "installed a wheel this OS cannot run"
    assert _results(result.stdout)["OK"] == ""


def test_the_floor_compares_versions_not_strings(tmp_path):
    """2.4 < 2.38 < 2.100 numerically; a string compare gets two of these wrong."""
    for libc, ok in (
        ("glibc 2.38", True),
        ("glibc 2.100", True),
        ("glibc 3.0", True),
        ("glibc 2.4", False),
        ("glibc 1.99", False),
    ):
        case = tmp_path / libc.replace(" ", "-")
        case.mkdir()
        env = _stage(case)
        env["FAKE_GLIBC"] = libc
        result = _run(_provisioned(_report()), env)
        assert result.returncode == 0, result.stderr
        assert (_results(result.stdout)["OK"] == "1") is ok, (libc, result.stdout)


def test_an_unsupported_arch_is_below_the_floor(tmp_path):
    for arch, ok in (
        ("x86_64", True),
        ("aarch64", True),
        ("riscv64", False),
        ("armv7l", False),
        ("i686", False),
        ("", False),
    ):
        case = tmp_path / (arch or "empty")
        case.mkdir()
        env = _stage(case)
        env["FAKE_ARCH"] = arch
        result = _run(_provisioned(_report()), env)
        assert result.returncode == 0, result.stderr
        assert (_results(result.stdout)["OK"] == "1") is ok, (arch, result.stdout)
        assert (pip_calls(env) != []) is ok, arch


def test_a_libc_that_is_not_glibc_is_below_the_floor(tmp_path):
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = ""
    result = _run(_provisioned(), env)
    assert result.returncode == 0, result.stderr
    assert "below the supported floor" in result.stdout
    assert pip_calls(env) == []


def test_a_failed_pip_run_warns_returns_zero_and_flags_the_refusal(tmp_path):
    """rc 0 stays the contract; the flag is what keeps the installers from the
    editable install, which would fetch the same wheel with no hash check."""
    env = _stage(tmp_path)
    env["PIP_RC"] = "1"
    result = _run(_provisioned(_report() + '; echo "REFUSED=$FALKORDB_PIP_REFUSED"'), env)
    assert result.returncode == 0, result.stderr
    assert "did not install (pip rc=1)" in result.stdout
    assert _results(result.stdout)["OK"] == ""
    assert "REFUSED=1" in result.stdout.splitlines()
    assert not engine_dir(env).exists()


def test_only_a_failed_pip_run_flags_the_refusal(tmp_path):
    """A verified engine, and a check that fails AFTER pip installed the hashed
    wheel, leave the flag empty: the editable install then finds it satisfied."""
    env = _stage(tmp_path)
    result = _run(_provisioned('echo "REFUSED=$FALKORDB_PIP_REFUSED"'), env)
    assert result.returncode == 0, result.stderr
    assert "REFUSED=" in result.stdout.splitlines()
    module = site_file(env, "redislite/bin/falkordb.so")
    module.write_bytes(b"tampered")
    result = _run(_provisioned('echo "REFUSED=$FALKORDB_PIP_REFUSED"'), env)
    assert "NOT verified" in result.stdout
    assert "REFUSED=" in result.stdout.splitlines()


def test_a_tampered_engine_file_fails_and_places_no_copy(tmp_path):
    env = _stage(tmp_path)
    module = site_file(env, "redislite/bin/falkordb.so")
    data = bytearray(module.read_bytes())
    data[0] ^= 0xFF
    module.write_bytes(bytes(data))
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    assert "NOT verified" in result.stdout
    assert "does not match the committed digest" in result.stdout
    assert _results(result.stdout)["OK"] == ""
    assert not engine_dir(env).exists()


def test_a_tampered_library_fails_too(tmp_path):
    """The module links the bundled libraries, so they are engine code too."""
    env = _stage(tmp_path)
    lib = site_file(env, "falkordblite.libs/libssl-test.so.3")
    lib.write_bytes(lib.read_bytes() + b"x")
    result = _run(_provisioned(_report()), env)
    assert _results(result.stdout)["OK"] == "", result.stdout


def test_a_version_other_than_the_pin_fails(tmp_path):
    env = _stage(tmp_path, wheel=False)
    from tests.test_scripts.falkordb_stubs import fake_wheel

    fake_wheel(Path(env["FAKE_SITE"]), version="0.10.9")
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    assert f"0.10.9 is installed but {PINNED_VERSION} is pinned" in result.stdout
    assert _results(result.stdout)["OK"] == ""


def test_a_missing_venv_warns_and_never_aborts(tmp_path):
    env = _stage(tmp_path)
    env["VENV"] = str(tmp_path / "no-venv")
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    assert "no venv python" in result.stdout
    assert _results(result.stdout)["OK"] == ""


def test_kill_switch_stops_everything(tmp_path):
    env = _stage(tmp_path)
    env["GENESIS_FALKORDB_PROVISION_DISABLED"] = "1"
    result = _run(_provisioned(_report()), env)
    assert result.returncode == 0, result.stderr
    assert "DISABLED" in result.stdout
    assert pip_calls(env) == []
    assert _results(result.stdout)["OK"] == ""


def test_an_inherited_ok_flag_is_not_a_verification(tmp_path):
    """FALKORDB_ENGINE_OK from the environment must not let a unit render."""
    env = _stage(tmp_path)
    env["FALKORDB_ENGINE_OK"] = "1"
    result = _run(_report(), env)
    assert _results(result.stdout)["OK"] == ""


# --- which unit to render ----------------------------------------------------

_DEPS_UNIT = (
    'ExecStart="/usr/bin/redis-server" \\\n'
    '    --loadmodule "%h/.genesis/deps/falkordb/4.20.4/falkordb.so" \\\n'
)


def _keep(env: dict, unit: Path, provision: bool = True) -> str:
    fn = (
        f'if falkordb_unit_keep_existing "{unit}"; then '
        'echo "KEEP:$FALKORDB_KEEP_REASON"; else echo RENDER; fi'
    )
    result = _run(_provisioned(fn) if provision else fn, env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_a_failed_check_keeps_the_existing_unit(tmp_path):
    env = _stage(tmp_path)
    site_file(env, "redislite/bin/redis-server").write_bytes(b"swapped")
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(_DEPS_UNIT)
    assert _keep(env, unit) == "KEEP:unverified"


def test_no_provision_at_all_never_renders(tmp_path):
    env = _stage(tmp_path)
    assert _keep(env, tmp_path / "genesis-falkordb.service", provision=False) == "KEEP:unverified"


def test_an_active_engine_on_another_module_is_kept_without_the_switch(tmp_path):
    """The owner ruling: a deploy never moves a running engine onto new binaries."""
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(_DEPS_UNIT)
    assert _keep(env, unit) == "KEEP:active"
    assert "--user is-active genesis-falkordb.service" in Path(env["SYSTEMCTL_LOG"]).read_text()


def test_the_switch_renders_an_active_engine(tmp_path):
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    env["GENESIS_FALKORDB_SWITCH"] = "1"
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(_DEPS_UNIT)
    assert _keep(env, unit) == "RENDER"


def test_an_inactive_engine_renders(tmp_path):
    for state in ("inactive", "failed"):
        case = tmp_path / state
        case.mkdir()
        env = _stage(case)
        env["ENGINE_STATE"] = state
        unit = case / "genesis-falkordb.service"
        unit.write_text(_DEPS_UNIT)
        assert _keep(env, unit) == "RENDER", state


def test_an_engine_between_states_is_kept(tmp_path):
    """`activating` is the RestartSec window after a crash: the engine is about
    to start again, and a rewritten unit would move it onto new binaries then."""
    for state in ("activating", "reloading", "deactivating", "refreshing"):
        case = tmp_path / state
        case.mkdir()
        env = _stage(case)
        env["ENGINE_STATE"] = state
        unit = case / "genesis-falkordb.service"
        unit.write_text(_DEPS_UNIT)
        assert _keep(env, unit) == "KEEP:active", state


def test_an_engine_whose_state_cannot_be_read_is_kept(tmp_path):
    """No user bus: `is-active` prints nothing. Unknown is not "not running"."""
    env = _stage(tmp_path)
    env["ENGINE_NOBUS"] = "1"
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(_DEPS_UNIT)
    assert _keep(env, unit) == "KEEP:state_unknown"


def test_an_unreadable_state_still_renders_an_engine_already_on_the_copy(tmp_path):
    """Same module: nothing would move, so the state does not matter."""
    env = _stage(tmp_path)
    env["ENGINE_NOBUS"] = "1"
    unit = tmp_path / "genesis-falkordb.service"
    module = copy_file(env, "redislite/bin/falkordb.so")
    unit.write_text(f'    --loadmodule "{module}" \\\n')
    assert _keep(env, unit) == "RENDER"


def test_no_unit_yet_renders(tmp_path):
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    assert _keep(env, tmp_path / "genesis-falkordb.service") == "RENDER"


def test_an_active_engine_already_on_the_copy_renders(tmp_path):
    """Same module: re-rendering moves nothing, so it is not a switch."""
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    unit = tmp_path / "genesis-falkordb.service"
    module = copy_file(env, "redislite/bin/falkordb.so")
    unit.write_text(f'    --loadmodule "{module}" \\\n')
    assert _keep(env, unit) == "RENDER"


def test_the_loaded_module_reads_back_percent_and_home(tmp_path):
    unit = tmp_path / "u.service"
    unit.write_text('    --loadmodule "/opt/100%%/falkordb.so" \\\n')
    result = _run(f'falkordb_unit_loaded_module "{unit}"', {"PATH": "/usr/bin:/bin", "HOME": "/h"})
    assert result.stdout == "/opt/100%/falkordb.so"
    unit.write_text(_DEPS_UNIT)
    result = _run(f'falkordb_unit_loaded_module "{unit}"', {"PATH": "/usr/bin:/bin", "HOME": "/h"})
    assert result.stdout == "/h/.genesis/deps/falkordb/4.20.4/falkordb.so"


# --- wiring ------------------------------------------------------------------


def test_bootstrap_provisions_before_the_editable_install():
    """update.sh re-runs bootstrap.sh, never install.sh, so the lib must be in
    bootstrap; and it must run BEFORE the editable install, or that install
    fetches falkordblite itself, unhashed."""
    text = BOOTSTRAP.read_text()
    assert "lib/falkordb_install.sh" in text
    provision = text.index('falkordb_provision "$VENV_DIR"')
    editable = text.index('editable_install_guarded "$GENESIS_ROOT" "$VENV_DIR"')
    assert provision < editable


def test_install_provisions_before_the_editable_install_and_keeps_its_results():
    text = INSTALL.read_text()
    provision = text.index('falkordb_provision "$VENV_PATH"')
    editable = text.index('editable_install_guarded "$REPO_DIR" "$VENV_PATH"')
    assert provision < editable
    # A pipeline would run it in a subshell and drop the paths it exports.
    line = text[provision : text.index("\n", provision)]
    assert "|" not in line, line
    # And the render step must not re-source the lib, which resets them.
    step7 = text[editable:]
    assert 'source "$SCRIPT_DIR/lib/falkordb_install.sh"' not in step7


# --- the OS floor refuses (owner ruling) -------------------------------------
#
# pyproject.toml declares the wheel, so below glibc 2.38 the editable install
# itself fails (pip falls back to compiling the engine). The installers stop in
# one line first. These run the installers' OWN blocks, cut out of the scripts,
# against a stubbed getconf.


def _block(path: Path, start: str, end: str) -> str:
    text = path.read_text()
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _run_block(block: str, env: dict, prelude: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; SCRIPT_DIR="{REPO_ROOT / "scripts"}"; {prelude}\n{block}',
        ],
        capture_output=True,
        text=True,
        env={**env, "FALKORDB_REQUIREMENTS": str(REQUIREMENTS)},
        timeout=60,
    )


def test_the_floor_message_names_what_this_os_has_and_what_it_needs(tmp_path):
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = "glibc 2.36"
    result = _run("falkordb_os_floor_ok || falkordb_os_floor_message", env)
    assert result.stdout == (
        "this OS has glibc 2.36; Genesis needs glibc 2.38 or newer (Ubuntu 24.04+), "
        "because its graph engine ships as wheels built only for those"
    )
    env["FAKE_GLIBC"] = "glibc 2.39"
    assert _run("falkordb_os_floor_ok && echo OK", env).stdout.strip() == "OK"


def test_the_floor_message_names_the_machine_when_the_arch_is_wrong(tmp_path):
    env = _stage(tmp_path)
    env["FAKE_ARCH"] = "riscv64"
    result = _run("falkordb_os_floor_ok || falkordb_os_floor_message", env)
    assert result.stdout == (
        "this machine is riscv64; Genesis needs x86_64 or aarch64, "
        "because its graph engine ships as wheels built only for those"
    )
    env["FAKE_GLIBC"] = "glibc 2.31"
    result = _run("falkordb_os_floor_ok || falkordb_os_floor_message", env)
    assert "this OS has glibc 2.31" in result.stdout
    assert ", and this machine is riscv64" in result.stdout


def test_bootstrap_refuses_an_unsupported_arch_before_installing_anything(tmp_path):
    block = _block(
        BOOTSTRAP, 'echo "--- Graph engine (optional) ---"', 'echo "  Syncing dependencies..."'
    )
    env = _stage(tmp_path)
    env["FAKE_ARCH"] = "armv7l"
    result = _run_block(block, env, prelude=f'VENV_DIR="{env["VENV"]}"')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "this machine is armv7l; Genesis needs x86_64 or aarch64" in result.stderr
    assert pip_calls(env) == []


def test_bootstrap_refuses_below_the_floor_before_installing_anything(tmp_path):
    block = _block(
        BOOTSTRAP, 'echo "--- Graph engine (optional) ---"', 'echo "  Syncing dependencies..."'
    )
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = "glibc 2.35"
    result = _run_block(block, env, prelude=f'VENV_DIR="{env["VENV"]}"')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Genesis needs glibc 2.38 or newer" in result.stderr
    assert "Stopping before the package install" in result.stderr
    assert pip_calls(env) == [], "installed a wheel before refusing"
    # Negative control: a supported OS goes through to the provision.
    ok = tmp_path / "ok"
    ok.mkdir()
    env = _stage(ok)
    result = _run_block(block, env, prelude=f'VENV_DIR="{env["VENV"]}"')
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(pip_calls(env)) == 1


def test_bootstrap_stops_when_the_hash_pinned_wheel_did_not_install(tmp_path):
    """The editable install would fetch the same mandatory wheel unverified."""
    block = _block(
        BOOTSTRAP, 'echo "--- Graph engine (optional) ---"', 'echo "  Syncing dependencies..."'
    )
    env = _stage(tmp_path)
    env["PIP_RC"] = "1"
    result = _run_block(
        block + 'echo "REACHED THE INSTALL"\n', env, prelude=f'VENV_DIR="{env["VENV"]}"'
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Stopping before the package install, which would fetch it unverified" in result.stderr
    assert "REACHED THE INSTALL" not in result.stdout


def test_install_skips_the_editable_install_when_the_wheel_was_refused():
    text = INSTALL.read_text()
    guard = text.index('if [ "${FALKORDB_PIP_REFUSED:-}" = "1" ]; then')
    editable = text.index('editable_install_guarded "$REPO_DIR" "$VENV_PATH"')
    assert text.index('falkordb_provision "$VENV_PATH"') < guard < editable
    branch = text[guard:editable]
    assert "_ei_rc=3" in branch and "else" in branch
    assert "        3)\n" in text[editable:]


def test_bootstrap_checks_the_floor_before_the_provision_and_the_package_install():
    text = BOOTSTRAP.read_text()
    floor = text.index("if ! falkordb_os_floor_ok; then")
    assert floor < text.index('falkordb_provision "$VENV_DIR"')
    assert floor < text.index('editable_install_guarded "$GENESIS_ROOT" "$VENV_DIR"')


def test_install_fails_its_preflight_below_the_floor(tmp_path):
    block = _block(INSTALL, "# OS floor: glibc 2.38", "# RAM")
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = "glibc 2.35"
    result = _run_block(
        block + '\necho "PREFLIGHT_OK=$PREFLIGHT_OK"', env, prelude="PREFLIGHT_OK=1"
    )
    assert result.returncode == 0, result.stderr
    assert "FAIL  OS: this OS has glibc 2.35" in result.stdout
    assert result.stdout.strip().endswith("PREFLIGHT_OK=0")
    env["FAKE_GLIBC"] = "glibc 2.39"
    result = _run_block(
        block + '\necho "PREFLIGHT_OK=$PREFLIGHT_OK"', env, prelude="PREFLIGHT_OK=1"
    )
    assert "OK    OS: glibc 2.39" in result.stdout
    assert result.stdout.strip().endswith("PREFLIGHT_OK=1")


def test_install_checks_the_floor_in_its_preflight_before_any_change():
    text = INSTALL.read_text()
    floor = text.index("# OS floor: glibc 2.38")
    assert text.index("# Pre-flight checks — run BEFORE any mutations.") < floor
    assert floor < text.index('echo "  Pre-flight FAILED')


def test_no_operator_step_wording_remains():
    for path in (BOOTSTRAP, INSTALL, LIB, UNIT_TEMPLATE):
        text = path.read_text()
        assert "operator step" not in text, path


_RENDER_LOOP_HEAD = '    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template'
MU_LIB = REPO_ROOT / "scripts" / "lib" / "managed_units.sh"
MU_PY = REPO_ROOT / "scripts" / "lib" / "managed_units.py"


def _stamped(text: str) -> str:
    """A render as bootstrap writes it: stamped by the real managed_units.py."""
    return subprocess.run(
        ["python3", "-I", "-S", str(MU_PY), "stamp"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _bootstrap_render_loop() -> str:
    """bootstrap.sh's REAL unit render loop, sliced out verbatim.

    Running the loop itself rather than asserting on its text: the defect is in
    what the loop WRITES, and a text assertion would pass for a guard placed
    after the write.
    """
    lines = BOOTSTRAP.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(_RENDER_LOOP_HEAD)]
    assert len(starts) == 1, f"expected one render loop, found {len(starts)}"
    start = starts[0]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "    done")
    return "\n".join(lines[start : end + 1])


def _render_falkordb_unit(
    tmp_path, env, prefix: str = 'falkordb_provision "$VENV"', after: str = ""
):
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    (templates / UNIT_TEMPLATE.name).write_text(UNIT_TEMPLATE.read_text())
    user_dir = tmp_path / "units"
    user_dir.mkdir(exist_ok=True)
    # The loop's own setup (sourcing lib/managed_units.sh, its scratch render
    # file) sits just above the sliced loop in bootstrap.sh, so it is re-created
    # here; the checker is the real one, this repo's copy.
    script = (
        f'set -euo pipefail; source "{LIB}"; {prefix}\n'
        f'SYSTEMD_TEMPLATE_DIR="{templates}"; SYSTEMD_USER_DIR="{user_dir}"\n'
        f'GENESIS_ROOT="{tmp_path / "repo"}"; CC_BIN_DIR=/usr/local/bin\n'
        f'source "{MU_LIB}"; GENESIS_MU_PY="{MU_PY}"; GENESIS_MU_PREV=""\n'
        '_mu_dir="$(mktemp -d)"; _mu_render="$_mu_dir/render"\n'
        "SERVICES_UPDATED=0\n" + _bootstrap_render_loop() + "\n" + after + "\n"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )
    return result, user_dir / "genesis-falkordb.service"


def test_a_first_render_carries_the_verified_paths(tmp_path):
    env = _stage(tmp_path)
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    text = unit.read_text()
    assert f'ExecStart="{copy_file(env, "redislite/bin/redis-server")}"' in text
    assert f'--loadmodule "{copy_file(env, "redislite/bin/falkordb.so")}"' in text
    assert f'"{copy_file(env, "redislite/bin/redis-cli")}"' in text
    assert f'-c "{copy_file(env, "engine.sha256")}"' in text
    assert str(Path(env["FAKE_SITE"])) not in text, "the unit runs the venv, not the copy"
    assert "__" not in re.sub(r"#.*", "", text), "a placeholder survived the render"


def test_an_unverified_engine_never_rewrites_a_working_unit(tmp_path):
    env = _stage(tmp_path)
    env["PIP_RC"] = "1"
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text("LAST-WORKING-UNIT\n")
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == "LAST-WORKING-UNIT\n", "rewrote with no verified engine"
    assert "Kept: genesis-falkordb.service (graph engine not verified" in result.stdout


def test_an_unverified_engine_with_no_unit_renders_nothing(tmp_path):
    env = _stage(tmp_path)
    env["FAKE_GLIBC"] = "glibc 2.31"
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert not unit.exists(), "rendered a unit with no engine paths"
    assert "not rendered (graph engine not verified)" in result.stdout


def test_a_kept_unit_whose_module_is_gone_says_so(tmp_path):
    """Keeping the unit does not keep its module: say the engine will not start."""
    env = _stage(tmp_path)
    env["PIP_RC"] = "1"
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(_DEPS_UNIT)
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == _DEPS_UNIT
    assert "will not start" in result.stdout


def test_an_active_engine_is_kept_and_told_how_to_switch(tmp_path):
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    loaded = Path(env["HOME"]) / ".genesis" / "deps" / "falkordb" / "4.20.4" / "falkordb.so"
    loaded.parent.mkdir(parents=True)
    loaded.write_bytes(b"older-module")
    loaded.chmod(0o755)
    previous = _stamped(_DEPS_UNIT)
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(previous)
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == previous, "switched a running engine unasked"
    assert "GENESIS_FALKORDB_SWITCH=1" in result.stdout
    assert "will not start" not in result.stdout


def test_an_inactive_engine_advances_a_genesis_render(tmp_path):
    """The unit Genesis rendered for the legacy deps-dir module moves to the copy."""
    env = _stage(tmp_path)
    old_template = (
        UNIT_TEMPLATE.read_text()
        .replace('"__FALKORDB_MODULE__"', '"%h/.genesis/deps/falkordb/4.20.4/falkordb.so"')
        .replace("__REDIS_SERVER__", "/usr/bin/redis-server")
        .replace("__REDIS_CLI__", "/usr/bin/redis-cli")
    )
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(_stamped(old_template))
    # Stamped, so it is Genesis's own render rather than a hand edit.
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert str(copy_file(env, "redislite/bin/falkordb.so")) in unit.read_text()


def test_a_verified_engine_never_overwrites_a_hand_edited_unit(tmp_path):
    """The graph engine's unit is a managed unit like any other."""
    env = _stage(tmp_path)
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text("LAST-WORKING-UNIT\n")
    result, unit = _render_falkordb_unit(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == "LAST-WORKING-UNIT\n"
    assert "Kept: genesis-falkordb.service" in result.stdout


def test_the_renderers_substitute_the_resolved_paths_escaped():
    """The SHAPE, not the token: `s|__X__||g` would substitute an empty string,
    and a commented-out sed line mentioning the token would pass a bare `in`.
    Values go through _sed_repl_esc first — `|`/`&` in a path would otherwise
    corrupt the expression or fail the render under set -e."""
    for path in (BOOTSTRAP, INSTALL):
        text = path.read_text()
        for var, fn, token in (
            ("_redis_bin_esc", "_falkordb_redis_server_bin", "__REDIS_SERVER__"),
            ("_redis_cli_esc", "_falkordb_redis_cli_bin", "__REDIS_CLI__"),
            ("_falkordb_mod_esc", "_falkordb_module_path", "__FALKORDB_MODULE__"),
            ("_falkordb_digests_esc", "_falkordb_digests_path", "__FALKORDB_DIGESTS__"),
        ):
            assert f'{var}=$(_sed_repl_esc "$({fn}' in text, (path.name, var)
            assert f"s|{token}|${var}|g" in text, (path.name, token)
        assert "__FALKORDB_VERSION__" not in text, path.name


# --- the unit ----------------------------------------------------------------


def _exec_argv(unit: str) -> str:
    """The ExecStart line's arguments only (not the prose about them)."""
    assert "ExecStart=" in unit, "unit template has no ExecStart directive"
    body = unit.split("ExecStart=", 1)[1]
    return body.split("\n[", 1)[0]


def _directive_lines(unit: str, key: str) -> list[str]:
    return [ln for ln in unit.splitlines() if ln.startswith(f"{key}=")]


def _argv_lines(unit: str) -> list[str]:
    return [
        ln.strip().rstrip("\\").strip()
        for ln in _exec_argv(unit).splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_unit_is_socket_only_and_bounded():
    unit = UNIT_TEMPLATE.read_text()
    argv = _exec_argv(unit)
    assert "--port 0" in argv, "unit would open a TCP listener"
    assert "--unixsocket " in argv
    assert "--maxmemory 512mb" in argv
    assert "--maxmemory-policy noeviction" in argv
    assert "MemoryMax=1G" in unit


def test_every_engine_start_restarts_the_projector_and_retries_once():
    """#3035: the engine keeps no data, so each start (restarts included)
    re-projects. `restart`, not `start`: a `start` of a projection already
    running against the engine that just died merges into it and runs nothing
    new. `--no-block` (the projector orders After= this unit), `-` (a failed
    trigger never fails the engine start), one retry so a transient bus error
    does not leave the graph empty until the hourly timer."""
    posts = [
        line.split("=", 1)[1]
        for line in UNIT_TEMPLATE.read_text().splitlines()
        if line.startswith("ExecStartPost=")
    ]
    [trigger] = [p for p in posts if "genesis-graph-project.service" in p]
    assert trigger.startswith("-+")
    assert trigger.count("systemctl --user restart --no-block genesis-graph-project.service") == 2
    assert " start " not in trigger
    assert "||" in trigger


def test_unit_keeps_the_engines_own_record_of_slow_moments():
    tokens = _exec_argv(UNIT_TEMPLATE.read_text()).split()
    assert tokens[tokens.index("--slowlog-max-len") + 1] == "4096"
    assert tokens[tokens.index("--latency-monitor-threshold") + 1] == "100"


def test_start_limit_directives_are_in_the_unit_section():
    unit = UNIT_TEMPLATE.read_text()
    assert "\n[Service]\n" in unit, "unit template has no [Service] section"
    unit_section, service_section = unit.split("\n[Service]\n", 1)
    for key in ("StartLimitBurst", "StartLimitIntervalSec"):
        assert key in unit_section, f"{key} must be in [Unit]"
        assert key not in service_section, f"{key} in [Service] is ignored"


def test_readiness_is_polled_because_the_wheel_cannot_notify():
    """Type=simple with no --supervised, AND a readiness poll, as ONE invariant.

    The wheel's redis has no libsystemd: under Type=notify it never reports
    ready and every start times out (MEASURED). Type=simple alone is active at
    fork, before the socket answers, which the posture rule reads as a fault.
    """
    unit = UNIT_TEMPLATE.read_text()
    assert _directive_lines(unit, "Type") == ["Type=simple"]
    assert not any("--supervised" in ln for ln in _argv_lines(unit))
    posts = _directive_lines(unit, "ExecStartPost")
    assert len(posts) == 2, posts
    ready, projector = posts
    assert "PING" in ready and "PONG" in ready and "-lt 150" in ready and "sleep 0.1" in ready
    assert ready.rstrip().endswith(
        'falkordb-ready "__REDIS_CLI__" "%h/.genesis/falkordb/falkordb.sock"'
    )
    # ExecStartPost lines run in order: the projector must start only once the
    # engine answers, so the poll comes first.
    assert "genesis-graph-project.service" in projector
    assert any(ln.startswith("TimeoutStartSec=") for ln in unit.splitlines())


def test_the_readiness_script_survives_systemd_expansion():
    """systemd expands `$NAME` from the environment and `%x` specifiers in a
    command line; every shell `$` must be `$$` and the script must hold no `%`."""
    ready = _directive_lines(UNIT_TEMPLATE.read_text(), "ExecStartPost")[0]
    script = ready.split("'")[1]
    assert re.search(r"(?<!\$)\$(?!\$)", script.replace("$$", "")) is None, script
    assert "%" not in script, script
    assert "__REDIS_CLI__" not in script, "the path is spliced into the shell script"


def test_every_start_rechecks_the_engine_digest():
    """The copy's own digest file, rendered: not one in the data dir, which is
    the unit's one writable path and so one the engine could rewrite."""
    unit = UNIT_TEMPLATE.read_text()
    pre = _directive_lines(unit, "ExecStartPre")
    assert pre == [
        'ExecStartPre=+/bin/mkdir -p "%h/.genesis/falkordb"',
        'ExecStartPre=/usr/bin/sha256sum --quiet --strict -c "__FALKORDB_DIGESTS__"',
    ], pre


def test_exec_start_and_module_are_rendered_not_hardcoded():
    unit = UNIT_TEMPLATE.read_text()
    assert 'ExecStart="__REDIS_SERVER__"' in unit
    assert '--loadmodule "__FALKORDB_MODULE__"' in _exec_argv(unit)
    assert "/usr/bin/redis-server" not in _exec_argv(unit)


def test_the_server_waits_for_the_engine_but_does_not_require_it():
    """Wants=, never Requires=: the engine is optional and arms nothing."""
    server = REPO_ROOT / "scripts" / "systemd" / "genesis-server.service.template"
    text = server.read_text()
    after = [ln for ln in text.splitlines() if ln.startswith("After=")]
    assert any("genesis-falkordb.service" in ln for ln in after), f"no ordering: {after}"
    for line in text.splitlines():
        if line.startswith(("Wants=", "Requires=", "Requisite=", "BindsTo=", "PartOf=")):
            assert "genesis-falkordb" not in line, (
                f"this ARMS an engine the operator never enabled: {line}"
            )


def test_unit_write_scope_is_narrow():
    lines = UNIT_TEMPLATE.read_text().splitlines()
    assert "ProtectHome=read-only" in lines, "strict alone leaves the home writable"
    rw = [ln for ln in lines if ln.startswith("ReadWritePaths=")]
    assert rw == ['ReadWritePaths="%h/.genesis/falkordb"'], rw


def test_bootstrap_does_not_arm_the_engine():
    text = BOOTSTRAP.read_text()
    enabling = [ln for ln in text.splitlines() if "systemctl --user enable" in ln]
    assert not any("falkordb" in ln for ln in enabling), enabling


# --- systemd quoting -----------------------------------------------------------
#
# systemd.service(5) "Command Lines": each command line is unquoted using the
# "Quoting" rules of systemd.syntax(7) — unquoted whitespace separates items,
# "..." wraps a whole item, C-style escapes apply — and "%" specifiers are
# expanded (systemd.unit(5); "%%" is a literal percent). MEASURED with
# `systemd-analyze --user verify` (systemd 255): specifiers expand per item
# AFTER the split, so a spaced %h stays one item.

_C_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t"}


def _systemd_words(value: str, home: str) -> list[str]:
    words: list[str] = []
    cur: list[str] = []
    quote = None
    started = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(_C_ESCAPES.get(value[i + 1], value[i + 1]))
            started = True
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
        elif ch in "\"'" and not cur:
            quote = ch
            started = True
        elif ch.isspace():
            if started:
                words.append("".join(cur))
            cur, started = [], False
        else:
            cur.append(ch)
            started = True
        i += 1
    if started:
        words.append("".join(cur))
    return [w.replace("%%", "\0").replace("%h", home).replace("\0", "%") for w in words]


def _directive(unit: str, key: str, index: int = 0) -> str:
    """One directive's value, joined across trailing-backslash continuations."""
    lines = unit.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(f"{key}=")]
    start = starts[index]
    parts = [lines[start][len(key) + 1 :]]
    i = start
    while parts[-1].rstrip().endswith("\\"):
        parts[-1] = parts[-1].rstrip()[:-1]
        i += 1
        parts.append(lines[i])
    return " ".join(parts)


def _render(
    home: str, server: str, cli: str, module: str, digests: str = "/e/engine.sha256"
) -> str:
    return (
        UNIT_TEMPLATE.read_text()
        .replace("__HOME__", home)
        .replace("__REDIS_SERVER__", server)
        .replace("__REDIS_CLI__", cli)
        .replace("__FALKORDB_MODULE__", module)
        .replace("__FALKORDB_DIGESTS__", digests)
    )


def _word(path: str) -> str:
    result = subprocess.run(
        ["bash", "-c", f'set -u; source "{LIB}"; _falkordb_unit_word "$1"', "_", path],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_home_paths_survive_a_home_with_spaces():
    home = "/home/op with space"
    unit = _render(home, "/e/redis-server", "/e/redis-cli", "/e/falkordb.so")
    data = f"{home}/.genesis/falkordb"
    assert _systemd_words(_directive(unit, "ExecStartPre", 0), home) == ["+/bin/mkdir", "-p", data]
    assert _systemd_words(_directive(unit, "ExecStartPre", 1), home)[-1] == "/e/engine.sha256"
    argv = _systemd_words(_directive(unit, "ExecStart"), home)
    for flag, want in (
        ("--unixsocket", f"{data}/falkordb.sock"),
        ("--dir", data),
        ("--loadmodule", "/e/falkordb.so"),
    ):
        assert argv[argv.index(flag) + 1] == want, (flag, argv)
    ready = _systemd_words(_directive(unit, "ExecStartPost", 0), home)
    assert ready[-2:] == ["/e/redis-cli", f"{data}/falkordb.sock"], ready
    assert _systemd_words(_directive(unit, "ReadWritePaths"), home) == [data]


def test_resolved_paths_with_spaces_and_percent_survive():
    """Unquoted, systemd splits a path at the space; undoubled, a `%` is read
    as a specifier (MEASURED: `p%c` became `p/<unit name>`)."""
    server = "/opt/venv 100%/redislite/bin/redis-server"
    cli = "/opt/venv 100%/redislite/bin/redis-cli"
    module = "/opt/venv 100%/redislite/bin/falkordb.so"
    digests = "/opt/venv 100%/engine.sha256"
    unit = _render("/home/op", _word(server), _word(cli), _word(module), _word(digests))
    assert _systemd_words(_directive(unit, "ExecStartPre", 1), "/home/op")[-1] == digests
    argv = _systemd_words(_directive(unit, "ExecStart"), "/home/op")
    assert argv[0] == server, argv[:2]
    assert argv[argv.index("--loadmodule") + 1] == module
    ready = _systemd_words(_directive(unit, "ExecStartPost", 0), "/home/op")
    assert ready[-2] == cli


def test_a_path_the_unit_cannot_carry_is_refused():
    """systemd rejects `"`, `\\` or control characters in an executable as a
    FATAL unit error, substitutes `$VAR`/`${VAR}` inside arguments (MEASURED),
    and does no PATH lookup: such paths, and relative ones, resolve to nothing,
    which keeps the unit from rendering."""
    for bad in (
        '/opt/quote"dir/redis-server',
        "/opt/back\\slash/redis-server",
        "/opt/new\nline/redis-server",
        "/opt/dol$lar/redis-server",
        "/opt/${HOME}/redis-server",
        "relative/redis-server",
    ):
        assert _word(bad) == "", bad


def test_an_unusable_resolved_path_makes_the_engine_unverified(tmp_path):
    env = _stage(tmp_path)
    fn = (
        'FALKORDB_ENGINE_OK=1 FALKORDB_REDIS_SERVER="relative/redis-server" '
        "FALKORDB_REDIS_CLI=/e/cli FALKORDB_MODULE=/e/m.so FALKORDB_DIGESTS=/e/d; "
        "if _falkordb_engine_verified; then echo YES; else echo NO; fi"
    )
    result = _run(fn, env)
    assert result.stdout.strip() == "NO", result.stdout + result.stderr


def test_the_github_download_is_gone():
    text = LIB.read_text()
    for gone in (
        "FALKORDB_RELEASE_BASE",
        "_falkordb_expected_sha",
        "FALKORDB_DATA_DIR",
        "FALKORDB_VERSION=",
        "curl",
    ):
        assert gone not in text, gone


# --- pruning the copies no unit runs ---------------------------------------------


def _deps(env: dict) -> Path:
    return Path(env["FALKORDB_DEPS_DIR"])


def _engine_copy(deps: Path, name: str, legacy: bool = False) -> Path:
    """An engine dir on disk: the copy layout, or the legacy <version>/falkordb.so."""
    module = deps / name / ("falkordb.so" if legacy else "redislite/bin/falkordb.so")
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes(b"module")
    module.chmod(0o755)
    return module


def _prune(env: dict, unit: Path, provision: bool = True) -> subprocess.CompletedProcess:
    fn = f'falkordb_prune_engines "{unit}"; echo "RC=$?"'
    result = _run(_provisioned(fn) if provision else fn, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("RC=0"), result.stdout
    return result


def _names(deps: Path) -> list[str]:
    return sorted(p.name for p in deps.iterdir())


def test_prune_keeps_only_the_current_copy_when_no_unit_exists(tmp_path):
    env = _stage(tmp_path)
    deps = _deps(env)
    _engine_copy(deps, "4.20.4", legacy=True)
    _engine_copy(deps, "0.10.0-cp312-cp312-manylinux_2_38_x86_64")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    deps.mkdir(parents=True, exist_ok=True)
    (deps / "a-link").symlink_to(outside)
    (deps / "a-file").write_text("x")
    (deps / ".0.11.0-x.tmp-1").mkdir()
    (deps / "a..b").mkdir()
    result = _prune(env, tmp_path / "no-unit.service")
    assert _names(deps) == sorted(
        [engine_dir(env).name, "a-link", "a-file", ".0.11.0-x.tmp-1", "a..b"]
    ), result.stdout
    assert (outside / "keep").exists(), "followed a symlink out of the deps dir"
    assert "Removed graph engine copy no unit runs: 4.20.4" in result.stdout


def test_prune_keeps_the_dir_the_unit_loads(tmp_path):
    """A kept unit (an active engine) runs the legacy module: it stays."""
    env = _stage(tmp_path)
    deps = _deps(env)
    legacy = _engine_copy(deps, "4.20.4", legacy=True)
    _engine_copy(deps, "0.10.0-cp312-cp312-manylinux_2_38_x86_64")
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{legacy}" \\\n')
    _prune(env, unit)
    assert _names(deps) == sorted([engine_dir(env).name, "4.20.4"])


def test_the_switch_run_keeps_the_copy_the_engine_still_runs(tmp_path):
    """GENESIS_FALKORDB_SWITCH=1 has just pointed the unit at the new copy, but
    the engine runs the old one until its restart: the old copy stays this run,
    so a new engine that fails on restart has something to go back to."""
    env = _stage(tmp_path)
    env["GENESIS_FALKORDB_SWITCH"] = "1"
    deps = _deps(env)
    _engine_copy(deps, "4.20.4", legacy=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{engine_dir(env)}/redislite/bin/falkordb.so" \\\n')
    _prune(env, unit)
    assert "4.20.4" in _names(deps)
    # The next ordinary run prunes it.
    del env["GENESIS_FALKORDB_SWITCH"]
    _prune(env, unit)
    assert "4.20.4" not in _names(deps)


def test_prune_keeps_everything_when_a_drop_in_can_change_what_runs(tmp_path):
    """A drop-in's ExecStart replaces the base unit's, so the base unit no longer
    says which copy runs: the copy the override loads must survive."""
    env = _stage(tmp_path)
    deps = _deps(env)
    base = _engine_copy(deps, "0.10.0-cp312-cp312-manylinux_2_38_x86_64")
    _engine_copy(deps, "4.20.4", legacy=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{base}" \\\n')
    dropin = tmp_path / "genesis-falkordb.service.d"
    dropin.mkdir()
    (dropin / "override.conf").write_text("[Service]\nExecStart=\nExecStart=/x --loadmodule old\n")
    _prune(env, unit)
    assert "4.20.4" in _names(deps)
    # Negative control: a drop-in dir with no .conf in it is not a drop-in.
    (dropin / "override.conf").rename(dropin / "override.conf.disabled")
    _prune(env, unit)
    assert "4.20.4" not in _names(deps)


def test_prune_reads_a_percent_h_unit(tmp_path):
    env = _stage(tmp_path)
    deps = _deps(env)
    _engine_copy(deps, "4.20.4", legacy=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(_DEPS_UNIT)  # %h/.genesis/deps/falkordb/4.20.4/falkordb.so
    _prune(env, unit)
    assert "4.20.4" in _names(deps)


def test_prune_keeps_everything_when_the_units_module_cannot_be_read(tmp_path):
    env = _stage(tmp_path)
    deps = _deps(env)
    _engine_copy(deps, "4.20.4", legacy=True)
    _engine_copy(deps, "0.10.0-cp312-cp312-manylinux_2_38_x86_64")
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text("ExecStart=/somewhere/else\n")
    before = _names(deps)
    _prune(env, unit, provision=False)
    _prune(env, unit)
    assert _names(deps) == sorted(before + [engine_dir(env).name])


def test_prune_without_a_verified_copy_keeps_only_what_the_unit_loads(tmp_path):
    """Rollback uninstalled the wheel: nothing verifies, the unit still runs
    its copy, and that copy is all that must stay."""
    env = _stage(tmp_path, wheel=False)
    deps = _deps(env)
    loaded = _engine_copy(deps, "0.11.0-cp312-cp312-manylinux_2_38_x86_64")
    _engine_copy(deps, "4.20.4", legacy=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{loaded}" \\\n')
    _prune(env, unit)
    assert _names(deps) == ["0.11.0-cp312-cp312-manylinux_2_38_x86_64"]


def test_prune_never_touches_anything_outside_the_deps_dir(tmp_path):
    env = _stage(tmp_path)
    outside = tmp_path / "home" / ".genesis" / "deps" / "other"
    outside.mkdir(parents=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{site_file(env, "redislite/bin/falkordb.so")}" \\\n')
    _prune(env, unit)
    assert outside.exists()
    assert _names(_deps(env)) == [engine_dir(env).name]


def test_prune_with_no_deps_dir_is_a_no_op(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_DEPS_DIR"] = str(tmp_path / "absent")
    _prune(env, tmp_path / "no-unit.service", provision=False)
    assert not (tmp_path / "absent").exists()


# --- the writer-state matrix, through bootstrap's real render loop ----------------


def _render_and_prune(tmp_path, env):
    return _render_falkordb_unit(
        tmp_path,
        env,
        prefix='falkordb_provision "$VENV"',
        after='falkordb_prune_engines "$SYSTEMD_USER_DIR/genesis-falkordb.service"',
    )


def test_a_pin_bump_under_a_running_engine_keeps_its_unit_and_its_dir(tmp_path):
    """The new pin lands in its own dir; the active unit keeps the old one,
    and pruning keeps both because the unit still loads the old one."""
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    old = "0.10.0-cp312-cp312-manylinux_2_38_x86_64"
    old_module = _engine_copy(_deps(env), old)
    previous = _stamped(f'ExecStart="/x"\n    --loadmodule "{old_module}" \\\n')
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(previous)
    result, unit = _render_and_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == previous, "moved a running engine onto the new pin"
    assert _names(_deps(env)) == sorted([old, engine_dir(env).name])


def test_an_inactive_engine_moves_to_the_new_copy_and_the_old_one_goes(tmp_path):
    env = _stage(tmp_path)
    legacy = _engine_copy(_deps(env), "4.20.4", legacy=True)
    old_template = (
        UNIT_TEMPLATE.read_text()
        .replace('"__FALKORDB_MODULE__"', f'"{legacy}"')
        .replace("__REDIS_SERVER__", "/usr/bin/redis-server")
        .replace("__REDIS_CLI__", "/usr/bin/redis-cli")
        .replace("__FALKORDB_DIGESTS__", "/d")
    )
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(_stamped(old_template))
    result, unit = _render_and_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert str(copy_file(env, "redislite/bin/falkordb.so")) in unit.read_text()
    assert _names(_deps(env)) == [engine_dir(env).name]


def test_a_rollback_that_uninstalled_the_wheel_keeps_the_unit_on_its_copy(tmp_path):
    """The unit runs the copy, not the venv, so a rollback that removes the
    wheel leaves the engine able to start; the copy is kept for it."""
    env = _stage(tmp_path)
    assert _run(_provisioned(), env).returncode == 0
    first, unit = _render_falkordb_unit(tmp_path, env)
    assert first.returncode == 0, first.stderr
    rendered = unit.read_text()
    import shutil

    shutil.rmtree(env["FAKE_SITE"])
    Path(env["FAKE_SITE"]).mkdir()
    result, unit = _render_and_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == rendered
    assert _names(_deps(env)) == [engine_dir(env).name]
    check = subprocess.run(
        ["sha256sum", "--quiet", "--strict", "-c", str(copy_file(env, "engine.sha256"))],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_a_tampered_wheel_keeps_the_old_unit_and_the_dir_it_loads(tmp_path):
    env = _stage(tmp_path)
    old = "0.10.0-cp312-cp312-manylinux_2_38_x86_64"
    old_module = _engine_copy(_deps(env), old)
    previous = _stamped(f'ExecStart="/x"\n    --loadmodule "{old_module}" \\\n')
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(previous)
    site_file(env, "redislite/bin/redis-server").write_bytes(b"unhashed install")
    result, unit = _render_and_prune(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == previous
    assert _names(_deps(env)) == [old]


def test_bootstrap_prunes_once_after_the_render_loop_and_the_reload():
    """After daemon-reload: systemd then already reads the unit the prune
    decided from, so no restart can find a pre-reload unit naming a pruned copy."""
    text = BOOTSTRAP.read_text()
    call = 'falkordb_prune_engines "$SYSTEMD_USER_DIR/genesis-falkordb.service"'
    assert text.count(call) == 1
    loop_end = text.index(_bootstrap_render_loop()) + len(_bootstrap_render_loop())
    at = text.index(call)
    reload = text.index("systemctl --user daemon-reload", loop_end)
    assert loop_end < reload < at
    assert "declare -F falkordb_prune_engines" in text[reload:at]


def _xdg_block() -> str:
    return _block(
        BOOTSTRAP, 'SYSTEMD_USER_DIR="$HOME/.config/systemd/user"', "SYSTEMD_TEMPLATE_DIR="
    )


def test_bootstrap_seeds_the_user_bus_before_the_keep_check(tmp_path):
    """Without these a CC session's `systemctl --user` answers nothing, and the
    keep check reads that as "state unknown": an inactive engine is never moved."""
    text = BOOTSTRAP.read_text()
    assert text.index(_xdg_block()) < text.index(_bootstrap_render_loop())
    script = (
        _xdg_block() + '\necho "XDG=${XDG_RUNTIME_DIR}"; echo "BUS=${DBUS_SESSION_BUS_ADDRESS:-}"'
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    assert f"XDG=/run/user/{uid}" in out
    # The bus only when its socket is there.
    runtime = tmp_path / "run"
    runtime.mkdir()
    env["XDG_RUNTIME_DIR"] = str(runtime)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    assert "BUS=\n" in out + "\n"
    import socket

    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(runtime / "bus"))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    finally:
        sock.close()
    assert f"BUS=unix:path={runtime}/bus" in out
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/elsewhere"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout
    assert "BUS=unix:path=/elsewhere" in out


def test_prune_keeps_the_running_copy_whatever_the_deps_dir_spelling(tmp_path):
    """The unit carries the check's normalized paths; this run's deps dir may be
    spelled with a trailing slash or // (a $HOME that ends in /). A raw string
    compare then missed the copy the unit runs and deleted it (reproduced)."""
    env = _stage(tmp_path)
    deps = _deps(env)
    running = _engine_copy(deps, "0.11.0-cp312-cp312-manylinux_2_38_x86_64")
    _engine_copy(deps, "4.20.4", legacy=True)
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{running}" \\\n')
    env["PIP_RC"] = "1"  # unverified this run: only the unit's copy can keep it
    env["FALKORDB_DEPS_DIR"] = str(deps) + "//"
    _prune(env, unit)
    assert running.parent.parent.parent.name in _names(deps)
    assert "4.20.4" not in _names(deps)


def test_prune_keeps_everything_when_the_unit_loads_from_outside(tmp_path):
    env = _stage(tmp_path)
    deps = _deps(env)
    _engine_copy(deps, "4.20.4", legacy=True)
    elsewhere = tmp_path / "elsewhere" / "falkordb.so"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"m")
    unit = tmp_path / "genesis-falkordb.service"
    unit.write_text(f'    --loadmodule "{elsewhere}" \\\n')
    _prune(env, unit)
    assert "4.20.4" in _names(deps)


def test_a_rebuilt_tampered_copy_is_reported_not_silent(tmp_path):
    """A copy that no longer matches the committed digests is the event the
    digests exist to catch: rebuilding it must say so, and say a running engine
    still runs the old bytes until it restarts."""
    env = _stage(tmp_path)
    first = _run(_provisioned(), env)
    assert first.returncode == 0, first.stderr
    assert "WARNING: the graph engine copy" not in first.stdout
    cli = engine_dir(env) / "redislite/bin/redis-cli"
    cli.chmod(0o755)
    cli.write_bytes(cli.read_bytes() + b"x")
    second = _run(_provisioned(), env)
    assert second.returncode == 0, second.stderr
    assert (
        "did not match the committed digests, or another account could write it, and was rebuilt"
        in second.stdout
    )
    assert "restart it" in second.stdout
    third = _run(_provisioned(), env)
    assert "WARNING: the graph engine copy" not in third.stdout, "a matching copy is reused quietly"


# --- arming the unit after the render (D3) -----------------------------------
#
# bootstrap.sh's own enable step, sliced out verbatim and run after the real
# render loop, against the stubbed systemctl (owner ruling 2026-11-09): a
# verified engine that is not running is enabled AND started; a running one is
# never started or restarted; anything else is left alone.

_ENABLE_HEAD = "    if declare -F falkordb_enable_unit >/dev/null; then"


def _bootstrap_enable_step() -> str:
    lines = BOOTSTRAP.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line == _ENABLE_HEAD]
    assert len(starts) == 1, f"expected one enable step, found {len(starts)}"
    end = next(i for i in range(starts[0] + 1, len(lines)) if lines[i] == "    fi")
    return "\n".join(lines[starts[0] : end + 1])


def _systemctl_calls(env: dict) -> list[str]:
    log = Path(env["SYSTEMCTL_LOG"])
    return log.read_text().splitlines() if log.exists() else []


_ENABLE_NOW = "--user enable --now genesis-falkordb.service"
_ENABLE_ONLY = "--user enable genesis-falkordb.service"


def _starts(calls: list[str]) -> list[str]:
    """Every call that could start or restart the engine."""
    return [c for c in calls if "--now" in c or c.split()[1:2] in (["start"], ["restart"])]


@pytest.mark.parametrize("state", ["inactive", "failed"])
@pytest.mark.parametrize("enabled", ["disabled", "enabled"])
def test_a_verified_engine_that_is_not_running_is_enabled_and_started(tmp_path, state, enabled):
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = state
    env["ENGINE_ENABLED"] = enabled
    result, unit = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    assert unit.exists()
    calls = _systemctl_calls(env)
    assert _ENABLE_NOW in calls, calls
    assert "+ genesis-falkordb.service enabled + started" in result.stdout


@pytest.mark.parametrize("state", ["active", "activating", ""])
def test_a_running_or_unreadable_engine_is_never_started(tmp_path, state):
    """The never-switch-a-running-engine rule: no start, no restart."""
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = state
    if state == "active":
        # A running engine already on another module keeps its unit; give the
        # render loop a unit to keep, as the existing keep tests do.
        (tmp_path / "units").mkdir()
        (tmp_path / "units" / "genesis-falkordb.service").write_text(_stamped(_DEPS_UNIT))
    result, _ = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    calls = _systemctl_calls(env)
    assert _starts(calls) == [], calls
    assert _ENABLE_ONLY in calls, "a disabled unit is still enabled, for the next boot"


def test_a_running_enabled_engine_is_left_untouched(tmp_path):
    env = _stage(tmp_path)
    env["ENGINE_STATE"] = "active"
    env["ENGINE_ENABLED"] = "enabled"
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text(_stamped(_DEPS_UNIT))
    result, _ = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    calls = _systemctl_calls(env)
    assert not [c for c in calls if " enable" in c], calls


def test_an_unverified_engine_s_unit_is_neither_enabled_nor_started(tmp_path):
    env = _stage(tmp_path)
    env["PIP_RC"] = "1"
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "genesis-falkordb.service").write_text("LAST-WORKING-UNIT\n")
    result, _ = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    assert not [c for c in _systemctl_calls(env) if " enable" in c]


@pytest.mark.parametrize("state", ["masked", "", "linked"])
def test_a_unit_file_state_other_than_disabled_or_enabled_is_left_alone(tmp_path, state):
    env = _stage(tmp_path)
    env["ENGINE_ENABLED"] = state
    result, _ = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    assert not [c for c in _systemctl_calls(env) if " enable" in c]


def test_an_operator_opt_out_keeps_the_unit_disabled_and_stopped(tmp_path):
    env = _stage(tmp_path)
    opt_out = Path(env["HOME"]) / ".genesis" / "config" / "disabled_timers"
    opt_out.parent.mkdir(parents=True)
    opt_out.write_text("genesis-falkordb.service  # standing down\n")
    result, _ = _render_falkordb_unit(tmp_path, env, after=_bootstrap_enable_step())
    assert result.returncode == 0, result.stderr
    assert not [c for c in _systemctl_calls(env) if " enable" in c]
    assert "not enabled (listed in" in result.stdout


def test_a_failed_start_is_one_warning_and_never_aborts(tmp_path):
    env = _stage(tmp_path)
    env["ENABLE_RC"] = "1"
    result, _ = _render_falkordb_unit(
        tmp_path, env, after=_bootstrap_enable_step() + "\necho AFTER-ENABLE"
    )
    assert result.returncode == 0, result.stderr
    warnings = [line for line in result.stdout.splitlines() if "WARNING" in line]
    assert len(warnings) == 1, warnings
    assert "could not start genesis-falkordb.service" in warnings[0]
    assert "AFTER-ENABLE" in result.stdout, "bootstrap must continue"
def test_both_installers_enable_after_their_unit_render():
    """Order, not just presence: the enable must follow the render (and, in
    bootstrap, the daemon-reload) or there is no unit for it to arm."""
    boot = BOOTSTRAP.read_text()
    assert boot.index(_ENABLE_HEAD) > boot.index("systemctl --user daemon-reload")
    assert boot.index(_ENABLE_HEAD) > boot.index(_RENDER_LOOP_HEAD)
    inst = INSTALL.read_text()
    call = 'falkordb_enable_unit "$SYSTEMD_USER_DIR/genesis-falkordb.service"'
    assert inst.count(call) == 1
    assert inst.index(call) > inst.index('elif [ "$svc_name" = "genesis-falkordb.service" ]')
