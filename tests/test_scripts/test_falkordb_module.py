"""Artifact-integrity and unit-wiring tests for falkordb_install.sh.

See falkordb_stubs.py for the harness. Covers the downloaded module (pinned
digest, execute bit, no half files), the kill switch, and the systemd unit's
render wiring and quoting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_scripts.falkordb_stubs import (
    BOOTSTRAP,
    LIB,
    PINNED_SHA,
    REPO_ROOT,
    UNIT_TEMPLATE,
    _run,
    _stage,
)

# --- the artifact must be what we tested ----------------------------------


def test_a_pinned_version_refuses_to_install_when_it_cannot_be_verified(tmp_path):
    """Having a pin and no way to check it must fail CLOSED.

    Here we know what the bytes should be and cannot confirm it — refuse.
    """
    env = _stage(tmp_path)
    no_sha = Path(env["PATH"].split(":")[0]) / "sha256sum"
    no_sha.write_text("#!/bin/bash\nexit 127\n")
    no_sha.chmod(0o755)

    result = _run("falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert "cannot verify" in result.stdout
    assert not (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so").exists()


def test_checksum_mismatch_refuses_and_leaves_nothing_behind(tmp_path):
    """The release ships no checksums, so the pin is the only integrity check."""
    env = _stage(tmp_path)  # fake artifact => digest cannot match the pin
    result = _run("falkordb_module_install", env)

    assert result.returncode == 0, result.stderr
    assert "checksum MISMATCH" in result.stdout
    dest = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4"
    assert not (dest / "falkordb.so").exists(), "installed despite a bad digest"
    assert not (dest / "falkordb.so.partial").exists(), "left a partial download"


def test_matching_checksum_installs_with_the_execute_bit(tmp_path):
    """Redis refuses a module without +x; the release asset arrives 644."""
    env = _stage(tmp_path)
    # Make the fake artifact hash to the pinned value by pinning the fake.
    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    real_sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    env["FALKORDB_VERSION"] = "4.20.4"

    result = _run(
        f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install',
        env,
    )
    assert result.returncode == 0, result.stderr
    installed = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    assert installed.is_file(), result.stdout
    assert installed.stat().st_mode & 0o111, "module installed without +x"


def test_unpinned_version_is_refused_without_downloading(tmp_path):
    """No pinned digest means no install — the module runs as native code."""
    env = _stage(tmp_path)
    env["FALKORDB_VERSION"] = "9.9.9"
    (tmp_path / "release" / "v9.9.9").mkdir()
    (tmp_path / "release" / "v9.9.9" / "falkordb-x64.so").write_bytes(b"x")

    result = _run("falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert "no pinned checksum" in result.stdout
    assert "refus" in result.stdout
    dest = Path(env["FALKORDB_DEPS_DIR"]) / "9.9.9"
    assert not (dest / "falkordb.so").exists(), "installed an unverified module"
    assert not (dest / "falkordb.so.partial").exists(), "downloaded before refusing"


def test_an_already_present_module_repairs_a_missing_execute_bit(tmp_path):
    """`-f target` is the installed sentinel, but redis refuses it without +x.

    A leftover file with 0644 must be repaired, not reported as present — and
    a failed repair removes it so the next run reinstalls instead of trusting
    the sentinel forever.
    """
    env = _stage(tmp_path)
    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    real_sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]

    target = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    target.parent.mkdir(parents=True)
    # The pinned bytes: a cached file is re-hashed, so only a VERIFIED module
    # reaches the execute-bit repair at all.
    target.write_bytes(artifact.read_bytes())
    target.chmod(0o644)

    result = _run(
        f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install',
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "restored +x" in result.stdout
    assert target.stat().st_mode & 0o111, "module left unexecutable"


def test_second_run_is_idempotent(tmp_path):
    env = _stage(tmp_path)
    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    real_sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]

    cmd = f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install'
    assert _run(cmd, env).returncode == 0
    second = _run(cmd, env)
    assert second.returncode == 0
    assert "already present" in second.stdout


def test_chmod_failure_reports_not_installed_and_retries(tmp_path):
    """A chmod failure must not leave a file the `-f target` check calls done.

    Redis refuses a module without +x, so a file left behind after a failed
    chmod would make every later run report "already present" while the
    engine could never load it. Remove it so the next run repairs.
    """
    env = _stage(tmp_path)
    chmod_stub = Path(env["PATH"].split(":")[0]) / "chmod"
    # The stub must really chmod on the success path — it shadows /usr/bin/chmod
    # for the whole run, so a no-op success would leave the file unexecutable.
    chmod_stub.write_text(
        "#!/bin/bash\n"
        'if [ "${CHMOD_RC:-0}" != "0" ]; then exit "$CHMOD_RC"; fi\n'
        'exec /usr/bin/chmod "$@"\n'
    )
    chmod_stub.chmod(0o755)

    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    real_sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]

    env["CHMOD_RC"] = "1"
    first = _run(
        f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install',
        env,
    )
    assert first.returncode == 0, first.stderr
    assert "NOT installed" in first.stdout
    target = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    assert not target.exists(), "broken install left behind for later runs to 'find'"

    env["CHMOD_RC"] = "0"
    second = _run(
        f'_falkordb_expected_sha() {{ printf "{real_sha}"; }}; falkordb_module_install',
        env,
    )
    assert second.returncode == 0, second.stderr
    assert target.is_file(), second.stdout
    assert target.stat().st_mode & 0o111


def test_download_failure_leaves_no_half_file(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_RELEASE_BASE"] = f"file://{tmp_path / 'nonexistent'}"
    result = _run("falkordb_module_install", env)

    assert result.returncode == 0, result.stderr
    assert "download failed" in result.stdout
    dest = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4"
    assert not (dest / "falkordb.so").exists()
    assert not (dest / "falkordb.so.partial").exists()


def test_the_pinned_digest_is_the_one_we_load_tested():
    assert PINNED_SHA in LIB.read_text()


def _pin_the_fake(tmp_path) -> str:
    """A shell prefix that pins the staged fake artifact's real digest."""
    artifact = tmp_path / "release" / "v4.20.4" / "falkordb-x64.so"
    sha = subprocess.run(
        ["sha256sum", str(artifact)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    return f'_falkordb_expected_sha() {{ printf "{sha}"; }}; '


def test_a_cached_module_is_rehashed_not_trusted(tmp_path):
    """The file is native code the unit loads, so the digest binds every run,
    not only the first download: bytes that no longer match are replaced."""
    env = _stage(tmp_path)
    target = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"substituted")
    target.chmod(0o755)

    result = _run(_pin_the_fake(tmp_path) + "falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert "already present" not in result.stdout, "trusted unverified bytes"
    assert target.read_bytes() == b"pretend-module", "kept a module off its pin"
    assert target.stat().st_mode & 0o111


def test_a_cached_mismatch_that_cannot_be_refetched_is_removed(tmp_path):
    env = _stage(tmp_path)
    env["FALKORDB_RELEASE_BASE"] = f"file://{tmp_path / 'nonexistent'}"
    target = Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"substituted")
    target.chmod(0o755)

    result = _run(_pin_the_fake(tmp_path) + "falkordb_module_install", env)
    assert result.returncode == 0, result.stderr
    assert not target.exists(), "left a module that fails its digest in place"


def test_provision_installs_the_verified_module(tmp_path):
    env = _stage(tmp_path)
    result = _run(_pin_the_fake(tmp_path) + "falkordb_provision", env)

    assert result.returncode == 0, result.stderr
    assert (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so").is_file()


def test_an_unpinned_architecture_installs_nothing(tmp_path):
    """Only x64 is pinned: an arm64 box must refuse, not fetch unverified bytes."""
    env = _stage(tmp_path)
    uname = Path(env["PATH"].split(":")[0]) / "uname"
    uname.write_text("#!/bin/bash\nprintf 'aarch64\\n'\n")
    uname.chmod(0o755)

    result = _run("falkordb_provision", env)
    assert result.returncode == 0, result.stderr
    assert "no pinned checksum" in result.stdout
    assert not (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4").exists()


def test_kill_switch_stops_everything_including_the_module(tmp_path):
    env = _stage(tmp_path)
    env["GENESIS_FALKORDB_PROVISION_DISABLED"] = "1"
    result = _run(_pin_the_fake(tmp_path) + "falkordb_provision", env)

    assert result.returncode == 0, result.stderr
    assert "DISABLED" in result.stdout
    assert not (Path(env["FALKORDB_DEPS_DIR"]) / "4.20.4" / "falkordb.so").exists()


# --- wiring ----------------------------------------------------------------


def test_bootstrap_sources_the_lib_and_substitutes_the_version():
    """The lib must reach existing installs, and the unit must get a version.

    update.sh re-runs bootstrap.sh, never install.sh — so provisioning placed
    only in install.sh would never reach a box that already exists. This test
    pins the lib to bootstrap.
    """
    text = BOOTSTRAP.read_text()
    assert "lib/falkordb_install.sh" in text
    assert "falkordb_provision" in text
    assert "__FALKORDB_VERSION__" in text


def _exec_argv(unit: str) -> str:
    """The ExecStart line's arguments only.

    Asserting against the whole file is how the socket-only property went
    untested: `--port 0` also appears in the comment that explains it, so
    deleting the actual argument left the assertion green (verified by
    mutation). Match the argv, not the prose about the argv.
    """
    # Say which contract the template broke: without this an ExecStart that
    # went missing surfaces as a bare IndexError from a slice, which reads as
    # a broken test rather than a broken unit.
    assert "ExecStart=" in unit, "unit template has no ExecStart directive"
    body = unit.split("ExecStart=", 1)[1]
    return body.split("\n[", 1)[0]


def test_unit_is_socket_only_and_bounded():
    """Properties that keep the engine off the network and inside its budget."""
    unit = UNIT_TEMPLATE.read_text()
    argv = _exec_argv(unit)
    assert "--port 0" in argv, "unit would open a TCP listener"
    assert "--unixsocket " in argv
    assert "--maxmemory 512mb" in argv
    # Eviction would silently corrupt a projection; it must fail loudly instead.
    assert "--maxmemory-policy noeviction" in argv
    assert "MemoryMax=1G" in unit


def test_start_limit_directives_are_in_the_unit_section():
    """StartLimit* in [Service] is HALF-silently ignored by systemd.

    It accepts StartLimitBurst there as a legacy alias but rejects
    StartLimitIntervalSec, so burst counts against the manager's 10s default.
    Measured with RestartSec=5: burst=4 becomes unreachable and a unit whose
    module fails to load restarts forever instead of entering `failed` — which
    the posture rule assumes cannot happen.
    """
    unit = UNIT_TEMPLATE.read_text()
    # Line-anchored: "[Service]" also appears inside the comment explaining
    # this very rule, so a bare split would cut in the wrong place.
    assert "\n[Service]\n" in unit, "unit template has no [Service] section"
    unit_section, service_section = unit.split("\n[Service]\n", 1)
    for key in ("StartLimitBurst", "StartLimitIntervalSec"):
        assert key in unit_section, f"{key} must be in [Unit]"
        assert key not in service_section, f"{key} in [Service] is ignored"


def test_the_resolver_never_emits_a_non_absolute_path(tmp_path):
    """systemd REFUSES TO PARSE a unit whose ExecStart is not absolute.

    `command -v` is not enough on its own: it echoes a RELATIVE path when PATH
    holds a relative entry, and a bare name when a shell function shadows the
    binary. Either would ship a unit that fails to LOAD — strictly worse than
    the hardcoded literal this replaced, which at least parsed.
    """
    relative_dir = tmp_path / "relbin"
    relative_dir.mkdir()
    stub = relative_dir / "redis-server"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    # PATH entry given RELATIVE to cwd — `command -v` resolves it relatively.
    result = subprocess.run(
        ["bash", "-c", f'set -u; source "{LIB}"; _falkordb_redis_server_bin'],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": "relbin:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.stdout.startswith("/"), (
        f"emitted a non-absolute path systemd cannot parse: {result.stdout!r}"
    )


def test_the_resolver_prefers_a_real_binary_over_the_fallback(tmp_path):
    """The equivalence lock: guarding against relative paths must not make the
    resolver ignore a perfectly good absolute one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "redis-server"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", f'set -u; source "{LIB}"; _falkordb_redis_server_bin'],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.stdout == str(stub), f"did not resolve the real binary: {result.stdout!r}"


def test_exec_start_is_rendered_not_hardcoded():
    """systemd does no PATH lookup, so ExecStart must be an absolute path — but
    it must not be an ASSUMED one.

    /usr/bin/redis-server is right on Debian/Ubuntu and wrong wherever the
    binary lands in /usr/local/bin (source build, some RPM layouts). There the
    unit fails with a bare 203/EXEC and nothing points at the cause. The path
    is resolved at render time instead, the same way the version pin is.
    """
    unit = UNIT_TEMPLATE.read_text()
    assert 'ExecStart="__REDIS_SERVER__"' in unit, "ExecStart is not rendered"
    assert "/usr/bin/redis-server" not in _exec_argv(unit), (
        "a hardcoded interpreter path is back in ExecStart"
    )

    # and the render step must actually substitute it, or the unit ships a
    # literal placeholder that fails to parse
    bootstrap = BOOTSTRAP.read_text()
    # The SHAPE, not the token: `s|__REDIS_SERVER__||g` would substitute an
    # empty string and ship an ExecStart that does not parse, and a commented-
    # out sed line mentioning the token would also pass a bare `in` check.
    # The value goes through _sed_repl_esc first — `|`/`&` in a resolved path
    # would otherwise corrupt the expression or fail the render under set -e.
    assert '_redis_bin_esc=$(_sed_repl_esc "$(_falkordb_redis_server_bin' in bootstrap, (
        "render loop does not substitute the resolved path"
    )
    assert "s|__REDIS_SERVER__|$_redis_bin_esc|g" in bootstrap, (
        "the escaped value is not the one substituted"
    )


def test_readiness_is_notified_not_assumed():
    """Type=notify and --supervised systemd are ONE invariant, not two.

    With Type=simple, systemd reports active as soon as the process forks and
    the socket is not answering yet — MEASURED 3/3 restarts, 61-147ms. The
    posture rule reads "unit active + socket missing" as a fault, so that
    window is a false alert waiting to fire.

    They are asserted TOGETHER because either alone is worse than neither:
    Type=notify without --supervised systemd means nothing ever sends the
    readiness notification, so every start hangs until TimeoutStartSec and
    then fails.
    """
    unit = UNIT_TEMPLATE.read_text()
    # Line-anchored on the DIRECTIVE. A substring check matched the comment
    # above it that explains the choice, so reverting Type=notify left this
    # test green -- verified by mutation. Prose is not structure.
    types = [ln for ln in unit.splitlines() if ln.startswith("Type=")]
    assert types == ["Type=notify"], f"expected exactly Type=notify, found {types}"
    # Comment-stripped: _exec_argv runs to the next section header, so it
    # carries every comment below ExecStart too. Asserting the raw substring
    # is one future comment away from the vacuity already caught twice here.
    argv = [
        ln.strip().rstrip("\\").strip()
        for ln in _exec_argv(unit).splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert "--supervised systemd" in argv, (
        "Type=notify without --supervised systemd hangs every start to timeout"
    )
    # And the hang must be bounded: notify can park a start job for the
    # manager default (90s here) where simple never could.
    assert any(ln.startswith("TimeoutStartSec=") for ln in unit.splitlines()), (
        "Type=notify with no TimeoutStartSec can park a start job for 90s"
    )


def test_the_server_waits_for_the_engine_but_does_not_require_it():
    """Wants=, never Requires=: the engine is optional and arms nothing.

    Ordering only. A Requires= would make genesis-server fail to start on every
    install that never adopted the graph engine, which is most of them.
    """
    server = REPO_ROOT / "scripts" / "systemd" / "genesis-server.service.template"
    text = server.read_text()
    # BOTH directives, checked separately: After= alone orders without pulling
    # the unit in, Wants= alone pulls it in without ordering. A whole-file
    # substring check passed with one of them reverted -- verified by mutation.
    after = [ln for ln in text.splitlines() if ln.startswith("After=")]
    assert any("genesis-falkordb.service" in ln for ln in after), f"no ordering: {after}"
    # Every directive that would START it, not just the hard ones. MEASURED: a
    # DISABLED unit named in another unit's Wants= is started anyway —
    # enablement gates only what default.target pulls in. bootstrap renders the
    # engine unit on EVERY install but arms it on none, so any of these would
    # start it on a box with no redis >= 8.0.0, where it restart-loops to
    # `failed`.
    for line in text.splitlines():
        if line.startswith(("Wants=", "Requires=", "Requisite=", "BindsTo=", "PartOf=")):
            assert "genesis-falkordb" not in line, (
                f"this ARMS an engine the operator never enabled: {line}"
            )


def test_unit_write_scope_is_narrow():
    """The engine may write its data dir and nothing else in the home.

    ProtectSystem=strict does NOT cover /home, /root or /run/user — systemd's
    strict table leaves them to ProtectHome= — so without ProtectHome the
    allowlist narrows nothing and the whole home stays writable.
    """
    lines = UNIT_TEMPLATE.read_text().splitlines()
    assert "ProtectHome=read-only" in lines, "strict alone leaves the home writable"
    rw = [ln for ln in lines if ln.startswith("ReadWritePaths=")]
    assert rw == ['ReadWritePaths="%h/.genesis/falkordb"'], rw
    # The data dir must exist before the sandbox is built (a missing
    # ReadWritePaths entry fails namespace setup), and a sandboxed mkdir could
    # not create it under a read-only home — so the mkdir runs unsandboxed.
    pre = [ln for ln in lines if ln.startswith("ExecStartPre=")]
    assert pre == ['ExecStartPre=+/bin/mkdir -p "%h/.genesis/falkordb"'], pre


def test_bootstrap_does_not_arm_the_engine():
    """PR-F1's central promise: the unit is rendered, never enabled.

    Bootstrap's enable loop covers `*.timer` plus explicitly-named services, so
    arming this one is a one-line change away. The old assertion here checked
    for `WantedBy=default.target`, which is present whether or not anything
    enables it — it stated the invariant without testing it.
    """
    text = BOOTSTRAP.read_text()
    enabling = [ln for ln in text.splitlines() if "systemctl --user enable" in ln]
    assert not any("falkordb" in ln for ln in enabling), enabling


# --- systemd quoting -------------------------------------------------------
#
# systemd.service(5) "Command Lines": each command line is unquoted using the
# "Quoting" rules of systemd.syntax(7) — unquoted whitespace separates items,
# "..." wraps a whole item, C-style escapes apply — and "%" specifiers are
# expanded (systemd.unit(5); "%%" is a literal percent). ReadWritePaths= takes
# a space-separated list under the same quoting. MEASURED with
# `systemd-analyze --user verify` (systemd 255): specifiers expand per item
# AFTER the split, so a spaced %h stays one item; an unquoted literal path
# with a space splits into two.

_C_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t"}


def _systemd_words(value: str, home: str) -> list[str]:
    """Split a directive value into items the way systemd does, then expand
    %h and %% per item — the order measured above."""
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


def _directive(unit: str, key: str) -> str:
    """One directive's value, joined across trailing-backslash continuations."""
    lines = unit.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{key}="))
    parts = [lines[start][len(key) + 1 :]]
    i = start
    while parts[-1].rstrip().endswith("\\"):
        parts[-1] = parts[-1].rstrip()[:-1]
        i += 1
        parts.append(lines[i])
    return " ".join(parts)


def _render(home: str, redis_word: str) -> str:
    """The template as both renderers substitute it (sed escaping is pinned
    separately by test_systemd_template_placeholders.py). __HOME__ is
    substituted too, so a path that goes back to a rendered home is caught."""
    return (
        UNIT_TEMPLATE.read_text()
        .replace("__HOME__", home)
        .replace("__REDIS_SERVER__", redis_word)
        .replace("__FALKORDB_VERSION__", "4.20.4")
    )


def _resolve(path_env: str, tmp_path) -> str:
    return subprocess.run(
        ["bash", "-c", f'set -u; source "{LIB}"; _falkordb_redis_server_bin'],
        capture_output=True,
        text=True,
        env={"PATH": path_env, "HOME": str(tmp_path)},
    ).stdout


def test_home_paths_survive_a_home_with_spaces():
    """A spaced $HOME must reach redis and mkdir as ONE argument each."""
    home = "/home/op with space"
    unit = _render(home, "/usr/bin/redis-server")
    data = f"{home}/.genesis/falkordb"

    assert _systemd_words(_directive(unit, "ExecStartPre"), home) == [
        "+/bin/mkdir",
        "-p",
        data,
    ]
    argv = _systemd_words(_directive(unit, "ExecStart"), home)
    for flag, want in (
        ("--unixsocket", f"{data}/falkordb.sock"),
        ("--dir", data),
        ("--loadmodule", f"{home}/.genesis/deps/falkordb/4.20.4/falkordb.so"),
    ):
        assert argv[argv.index(flag) + 1] == want, (flag, argv)
    assert _systemd_words(_directive(unit, "ReadWritePaths"), home) == [data]


def test_a_resolved_interpreter_path_with_spaces_and_percent_survives(tmp_path):
    """The rendered ExecStart executable must be the resolved path, whole.

    Unquoted, systemd splits it at the space; undoubled, a `%` is read as a
    specifier (MEASURED: `p%c` became `p/<unit name>`).
    """
    bin_dir = tmp_path / "redis 100% bin"
    bin_dir.mkdir()
    stub = bin_dir / "redis-server"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    word = _resolve(f"{bin_dir}:/usr/bin:/bin", tmp_path)
    argv = _systemd_words(_directive(_render("/home/op", word), "ExecStart"), "/home/op")
    assert argv[0] == str(stub), (word, argv[:2])


def test_an_interpreter_path_systemd_refuses_is_never_rendered(tmp_path):
    """systemd rejects an executable containing `"` or `\\` as a FATAL unit
    error ("Executable name contains special characters"), quoted or not —
    MEASURED with systemd-analyze verify. Such a path is skipped like a
    relative one."""
    for name in ('quote"dir', "back\\slash"):
        bin_dir = tmp_path / name
        bin_dir.mkdir()
        stub = bin_dir / "redis-server"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        word = _resolve(f"{bin_dir}:/usr/bin:/bin", tmp_path)
        assert word.startswith("/") and '"' not in word and "\\" not in word, word
