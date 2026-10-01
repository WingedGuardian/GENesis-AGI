"""Locks on dependency pins that are COUPLED to something else in the repo.

A pin whose correctness depends on another file's value is a convention, and
conventions decay silently — nothing fails when the two drift apart. These tests
are the chokepoint: they re-derive the coupling from both sources and fail when
it breaks, so the next person to bump either number is told.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]


def _qdrant_client_requirement() -> Requirement:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for dep in data["project"]["dependencies"]:
        if Requirement(dep).name == "qdrant-client":
            return Requirement(dep)
    pytest.fail("qdrant-client is not declared in pyproject.toml dependencies")


def _qdrant_server_version() -> tuple[int, int]:
    """The server version scripts/install.sh installs when Qdrant is absent."""
    text = (REPO_ROOT / "scripts" / "install.sh").read_text()
    m = re.search(r'QDRANT_VERSION="\$\{QDRANT_VERSION:-([0-9]+)\.([0-9]+)\.[0-9]+\}"', text)
    assert m, "could not find the QDRANT_VERSION default in scripts/install.sh"
    return int(m.group(1)), int(m.group(2))


def _qdrant_compatible(v: Version, s_major: int, s_minor: int) -> bool:
    """Qdrant's own client/server rule, from qdrant_client/common/version_check.py:
    same major, and abs(server.minor - client.minor) <= 1."""
    return v.major == s_major and abs(v.minor - s_minor) <= 1


def _candidate_versions(s_major: int, s_minor: int) -> list[Version]:
    """A dense grid around the server version, spanning both boundaries.

    Deliberately a GRID rather than a handful of sample points. An earlier version
    of this test probed three specific strings ("{major}.{minor+2}.0", one
    lower-side value, "{major+1}.0.0") and every one of its gaps was a real hole:

      - probing only ``.0`` of a disallowed minor let ``!=1.16.0`` through while
        ``1.16.1`` still resolved, so patch levels are enumerated;
      - probing ``minor - 2`` computed a valid minor when the server minor was 0
        or 1, so the window is now computed per candidate rather than assumed;
      - probing only ``major + 1`` let ``>=0`` admit 0.x clients, so majors below
        the server's are covered too.

    The grid is the declared coverage model, and it is a model — a version outside
    it has no cell and so passes in silence. It spans two majors either side and
    three minors either side of the server, which is far wider than any plausible
    coordinated bump.
    """
    majors = range(max(0, s_major - 2), s_major + 3)
    minors = range(max(0, s_minor - 3), s_minor + 4)
    patches = (0, 1, 7)  # .0 is not representative of a minor line
    return [Version(f"{a}.{b}.{c}") for a in majors for b in minors for c in patches]


def test_qdrant_client_pin_admits_only_compatible_clients():
    """Every version the specifier admits must satisfy Qdrant's compatibility rule.

    This is the coupling the qdrant-client pin exists to hold. Without it the pin
    is only a comment in pyproject.toml, and moving EITHER number — the client
    specifier or the install.sh server default — leaves CI green while the pairing
    goes unsupported.
    """
    req = _qdrant_client_requirement()
    s_major, s_minor = _qdrant_server_version()

    admitted_incompatible: list[str] = []
    admitted_compatible: list[str] = []
    for v in _candidate_versions(s_major, s_minor):
        if not req.specifier.contains(str(v)):
            continue
        target = (
            admitted_compatible
            if _qdrant_compatible(v, s_major, s_minor)
            else admitted_incompatible
        )
        target.append(str(v))

    assert not admitted_incompatible, (
        f"'{req}' admits {admitted_incompatible} — incompatible with server "
        f"{s_major}.{s_minor}.x, whose rule is same major and at most one minor "
        f"apart. Bump BOTH the client specifier and QDRANT_VERSION, or neither."
    )
    assert admitted_compatible, (
        f"'{req}' admits NO client compatible with server {s_major}.{s_minor}.x — "
        f"the pin and the server default have drifted apart in the other direction."
    )


def test_qdrant_client_pin_is_bounded_at_all():
    """An unbounded specifier is what created the skew this pin fixes.

    ``qdrant-client`` carried no specifier at all, so a fresh install resolved to
    whatever was newest and paired it with a server pinned three releases back.
    Guard the SHAPE, not just the current numbers: a future edit that drops the
    upper bound reintroduces the drift even if it happens to resolve correctly on
    the day it lands.
    """
    req = _qdrant_client_requirement()
    operators = {spec.operator for spec in req.specifier}
    assert operators & {"<", "<=", "==", "~="}, (
        f"'{req}' has no upper bound — the client will drift past the server "
        "again. Qdrant requires the two stay within one minor."
    )


def test_gitnexus_pin_is_single_sourced_and_current():
    """Install and update paths must not silently downgrade the indexed DB.

    GitNexus storage formats move with releases.  On 2026-09-16 a successful
    1.6.12 rebuild (storage v42) was made unreadable when bootstrap reinstalled
    the older 1.6.8 pin (storage v41).  Keep one reviewed pin and require both
    install paths to consume it.
    """
    version_file = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    version_text = version_file.read_text()
    matches = re.findall(
        r'^GENESIS_GITNEXUS_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
        version_text,
        re.MULTILINE,
    )
    match = matches[0] if len(matches) == 1 else None
    assert match, "gitnexus_version.sh must contain one exact semantic-version pin"
    assert match == "1.6.12"

    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        source = 'source "$_gitnexus_pin_file"'
        assert source in text
        assert text.index('if [ -r "$_gitnexus_pin_file" ]; then') < text.index(source)
        assert text.index(source) < text.index("if genesis_gitnexus_ensure_pin;")
        assert "genesis_gitnexus_ensure_pin" in text
        assert not re.search(r"gitnexus@[0-9]+\.[0-9]+\.[0-9]+", text), (
            f"{relative} carries a second GitNexus version literal"
        )

    bootstrap = (REPO_ROOT / "scripts" / "bootstrap.sh").read_text()
    assert bootstrap.index("UPDATE_STATE=") < bootstrap.index(
        'source "$_gitnexus_pin_file"'
    ), "an optional pin helper must not preempt interrupted-update recovery"


def test_installers_enforce_the_pinned_tools_node_floor():
    """The installer floor must satisfy EVERY pinned tool, not just Claude Code.

    Claude Code's floor is Node >= 22; the pinned GitNexus declares
    ``^22.18.0 || >=24.11.0``. A major-only check lets Node 22.0–22.17 install
    and then strands GitNexus: every launcher and indexing attempt refuses on
    the engine range forever. Both installers must enforce the GitNexus range
    (which is a strict subset of >= 22 anyway).
    """
    install = (REPO_ROOT / "scripts" / "install.sh").read_text()
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap.sh").read_text()
    assert 'NODE_MAJOR="${NODE_MAJOR:-22}"' in install
    for relative, text in (("install.sh", install), ("bootstrap.sh", bootstrap)):
        assert '"$major" -eq 22 && "$minor" -ge 18' in text, relative
        assert '"$major" -eq 24 && "$minor" -ge 11' in text, relative
        assert '"$major" -gt 24' in text, relative


def test_installers_do_not_run_cbm_installer_while_kill_switch_is_active():
    """Neither install path may reach the installer while the kill switch is set.

    The fetch moved to scripts/lib/cbm_installer.sh — one site for the pin, its
    digest and the install — so the ordering is now checked against the CALL,
    and the callers are additionally held to not growing a second copy of the
    fetch. The shared script also refuses on its own when run directly, so the
    guarantee does not rest on every future caller remembering to check.
    """
    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        # The sentinel is the resolved shared-site path (cbm_disable_file.sh),
        # honouring CODEBASE_MEMORY_MCP_DISABLE_FILE — never a hard-coded
        # $HOME literal that would ignore the override.
        sentinel = '[ -e "$_cbm_disable" ]'
        assert text.index(sentinel) < text.index("genesis_cbm_install"), relative
        assert 'lib/cbm_disable_file.sh' in text, relative
        assert "raw.githubusercontent.com/DeusData/codebase-memory-mcp/" not in text, relative
    # The shared script's OWN kill switch is exercised, not grepped, by
    # test_bootstrap_guards.py::test_b9_direct_run_honours_the_kill_switch — a
    # substring here would survive inverting the test to `[ ! -e ]`, or moving
    # it below the install.


#: Dynamic npm-resolver invocations of gitnexus. Hoisted to module scope so the
#: ReDoS regression test below measures the SHIPPED pattern rather than a copy
#: that could drift away from it while still passing.
#:
#: `--?[A-Za-z][A-Za-z-]*` requires a LETTER directly after the dashes. That is
#: not cosmetic: with `[A-Za-z-]+` able to match a dash while `--?` also could,
#: each option token split two ways per repetition and the outer `*` made it
#: 2**n (CodeQL py/redos, HIGH). MEASURED on the pre-fix pattern with payload
#: `"npx" + " --a"*n + " X"`: n=8 0.16ms, n=16 43.5ms, n=22 2.6s -- x16 per +4.
_RESOLVER_PATTERN = re.compile(
    r"\b(?:"
    r"npx(?:\s+--?[A-Za-z][A-Za-z-]*)*|"
    r"npm\s+(?:exec|x)(?:\s+--[A-Za-z][A-Za-z-]*)*|"
    r"pnpm\s+dlx|yarn\s+dlx|bunx"
    r")\s+(?:--\s+)?gitnexus(?:@|\s|$)"
)


def test_every_gitnexus_package_resolver_uses_the_shared_pin():
    """No install/index path may float to an npm tag or range."""
    dynamic_resolver = _RESOLVER_PATTERN
    for mutation in (
        "npx gitnexus",
        "npx -y gitnexus",
        "npx --yes gitnexus",
        "npm exec gitnexus",
        "npm exec -- gitnexus",
        "npm x gitnexus",
        "pnpm dlx gitnexus",
        "yarn dlx gitnexus",
        "bunx gitnexus",
    ):
        assert dynamic_resolver.search(mutation), mutation

    resolvers: list[tuple[str, str]] = []
    for path in (REPO_ROOT / "scripts").rglob("*.sh"):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\bnpm\s+install\b.*\bgitnexus(?:@|\s|$)", line):
                resolvers.append((str(path.relative_to(REPO_ROOT)), line.strip()))
            assert not dynamic_resolver.search(line), (
                f"{path.relative_to(REPO_ROOT)} has a dynamic GitNexus resolver: {line}"
            )

    assert len(resolvers) == 1
    for path, line in resolvers:
        assert '"gitnexus@${GENESIS_GITNEXUS_VERSION}"' in line, (
            f"{path} bypasses the shared exact pin: {line}"
        )
        assert "--engine-strict" in line

    instruction_files = list((REPO_ROOT / ".claude" / "skills").rglob("*.md"))
    for path in instruction_files:
        assert not dynamic_resolver.search(path.read_text()), (
            f"{path.relative_to(REPO_ROOT)} instructs an unpinned resolver"
        )

    managed_instruction_files = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "AGENTS.md",
        *(REPO_ROOT / ".claude" / "skills").rglob("*.md"),
        *(REPO_ROOT / ".claude" / "docs").rglob("*.md"),
    ]
    raw_parts = ["git" + "nexus", "ana" + "lyze"]
    raw_analyze = "`" + " ".join(raw_parts) + "`"
    wrapper_parts = ["node", ".git" + "nexus/run.cjs", "ana" + "lyze"]
    raw_wrapper_analyze = "`" + " ".join(wrapper_parts) + "`"
    for path in managed_instruction_files:
        text = path.read_text()
        assert raw_analyze not in text, path.relative_to(REPO_ROOT)
        assert raw_wrapper_analyze not in text, path.relative_to(REPO_ROOT)


def test_gitnexus_node_engine_contract_matches_upstream_boundaries():
    """The local predicate must match ``^22.18.0 || >=24.11.0``."""
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    expected = {
        "v20.20.2": False,
        "v21.7.3": False,
        "v22.17.9": False,
        "v22.18.0": True,
        "v22.99.0": True,
        "v23.11.1": False,
        "v24.10.9": False,
        "v24.11.0": True,
        "v25.0.0": True,
    }
    for version, supported in expected.items():
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; genesis_gitnexus_node_version_supported "$2"',
                "bash",
                str(helper),
                version,
            ],
            check=False,
        )
        assert (result.returncode == 0) is supported, version


def test_gitnexus_unreviewed_version_is_never_automatically_replaced(tmp_path):
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    npm_log = tmp_path / "npm.log"
    gitnexus = fakebin / "gitnexus"
    version_file = tmp_path / "version"
    version_file.write_text("1.6.13\n")
    gitnexus.write_text(f'#!/bin/sh\ncat "{version_file}"\n')
    gitnexus.chmod(0o755)
    npm = fakebin / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = config ]; then exit 0; fi\n'
        f'echo called > "{npm_log}"\n'
    )
    npm.chmod(0o755)
    for version, expected_rc in (
        ("1.6.13", 2),
        ("1.7.0-rc.1", 2),
        ("not-semver", 3),
    ):
        version_file.write_text(f"{version}\n")
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; genesis_gitnexus_ensure_pin',
                "bash",
                str(helper),
            ],
            env={"PATH": f"{fakebin}:/usr/bin:/bin"},
            check=False,
        )
        assert result.returncode == expected_rc, version
        assert not npm_log.exists(), version


def test_gitnexus_ensure_pin_install_upgrade_and_postconditions(tmp_path):
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    gitnexus = fakebin / "gitnexus"
    version_file = tmp_path / "version"
    npm_log = tmp_path / "npm.log"
    npm_result = tmp_path / "npm-result"
    gitnexus.write_text(f'#!/bin/sh\ncat "{version_file}"\n')
    npm = fakebin / "npm"
    npm.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = config ]; then exit 0; fi\n'
        f'printf "%s\\n" "$*" > "{npm_log}"\n'
        f'cat "{npm_result}" > "{version_file}"\n'
        f'chmod +x "{gitnexus}"\n'
    )
    npm.chmod(0o755)
    env = {"PATH": f"{fakebin}:/usr/bin:/bin"}
    command = [
        "bash",
        "-c",
        'source "$1"; genesis_gitnexus_ensure_pin',
        "bash",
        str(helper),
    ]

    for initial, installed, expected_rc, npm_called in (
        (None, "1.6.12", 0, True),
        ("1.6.8", "1.6.12", 0, True),
        ("1.6.12", "1.6.12", 0, False),
        ("1.6.8", "1.6.9", 1, True),
    ):
        npm_log.unlink(missing_ok=True)
        npm_result.write_text(f"{installed}\n")
        version_file.write_text(f"{initial or 'absent'}\n")
        gitnexus.chmod(0o755 if initial is not None else 0o644)
        result = subprocess.run(command, env=env, check=False)
        assert result.returncode == expected_rc, (initial, installed)
        assert npm_log.exists() is npm_called, (initial, installed)
        if npm_called:
            assert npm_log.read_text() == (
                "install -g --engine-strict gitnexus@1.6.12\n"
            )


def test_gitnexus_ensure_pin_refuses_to_create_a_shadow_conflict(tmp_path):
    """`npm install -g` writes only to npm's prefix. Upgrading while the resolver
    sees several same-version copies — or one copy outside the prefix — lands a
    pinned install NEXT to an untouched older one, manufacturing the conflict
    the shadow scan then refuses. Refuse first instead."""
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    fakebin = tmp_path / "bin"
    shadow = tmp_path / "home" / ".local" / "bin"  # a scanned canonical location
    fakebin.mkdir()
    shadow.mkdir(parents=True)
    version_file = tmp_path / "version"
    version_file.write_text("1.6.8\n")
    npm_log = tmp_path / "npm.log"
    for d in (fakebin, shadow):
        g = d / "gitnexus"
        g.write_text(f'#!/bin/sh\ncat "{version_file}"\n')
        g.chmod(0o755)
    npm = fakebin / "npm"
    # npm reports a prefix NEITHER binary lives under.
    npm.write_text(f'#!/bin/sh\nif [ "${{1:-}}" = config ]; then echo "{tmp_path}/prefix"; exit 0; fi\n'
                   f'printf "%s\\n" "$*" > "{npm_log}"\n')
    npm.chmod(0o755)
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}

    # Two same-version copies: the resolver accepts them, but upgrading one
    # leaves the other stale — refuse (rc 3) and never call npm.
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; genesis_gitnexus_ensure_pin', "bash", str(helper)],
        env=env, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 3
    assert not npm_log.exists()

    # The lone install outside npm's prefix gets the same verdict: an upgrade
    # would only create the second, conflicting copy.
    (shadow / "gitnexus").unlink()
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; genesis_gitnexus_ensure_pin', "bash", str(helper)],
        env=env, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 3
    assert "shadow" in result.stderr
    assert not npm_log.exists()


def test_gitnexus_resolver_finds_npm_prefix_outside_path(tmp_path):
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    path_bin = tmp_path / "path-bin"
    prefix_bin = tmp_path / "prefix" / "bin"
    path_bin.mkdir()
    prefix_bin.mkdir(parents=True)
    gitnexus = prefix_bin / "gitnexus"
    gitnexus.write_text("#!/bin/sh\necho 1.6.12\n")
    gitnexus.chmod(0o755)
    npm = path_bin / "npm"
    npm.write_text(f'#!/bin/sh\necho "{tmp_path / "prefix"}"\n')
    npm.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; genesis_gitnexus_resolve_binary; '
            "genesis_gitnexus_installed_is_pinned",
            "bash",
            str(helper),
        ],
        env={"PATH": f"{path_bin}:/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(gitnexus)


def test_gitnexus_callers_preserve_ensure_pin_status():
    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        assert "if ! genesis_gitnexus_ensure_pin" not in text
        assert re.search(
            r"if genesis_gitnexus_ensure_pin; then.*?else\s+_gitnexus_rc=\$\?",
            text,
            re.DOTALL,
        ), relative


def test_gitnexus_registration_always_drift_heals_to_fail_closed_launcher():
    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        registration = '_register_mcp "gitnexus" "user"'
        assert registration in text
        # Bound the slice at the registration CALL: extending to end-of-file
        # lets a later unrelated `-x` check satisfy the assertion even when the
        # GitNexus guard itself is gone.
        start = text.rfind("if ", 0, text.index(registration))
        end = text.index("\n", text.index(registration)) + 1
        registration_block = text[start:end]
        assert '-x "$' in registration_block
        assert ".claude/mcp/run-gitnexus" in registration_block
        assert "_GITNEXUS_PIN_READY" not in registration_block



def test_no_unpinned_gitnexus_resolver_in_repository_files():
    """The pattern above is only a detector — apply it to the repo itself.

    A mutation-table test proves the regex fires; it says nothing about whether
    an actual source file floats to an npm tag. Scan the executable surfaces
    and fail on any resolver invocation that does not pin through
    ``GENESIS_GITNEXUS_VERSION`` (``gitnexus@${GENESIS_GITNEXUS_VERSION}``) — a
    floating ``npx gitnexus`` can fetch an unreviewed release that writes an
    unreadable storage format.
    """
    offenders: list[str] = []
    scanned = 0
    for base, glob in (
        ("scripts", "**/*.sh"),
        (".claude", "**/*"),
        ("src", "**/*.py"),
    ):
        root = REPO_ROOT / base
        if not root.is_dir():
            continue
        for path in root.rglob(glob):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                if _RESOLVER_PATTERN.search(line) and "GENESIS_GITNEXUS_VERSION" not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert scanned > 0, "resolver scan covered no files — the guard is vacuous"
    assert not offenders, (
        "gitnexus resolver invocations that do not pin via "
        f"GENESIS_GITNEXUS_VERSION: {offenders}"
    )



def test_the_resolver_pattern_does_not_backtrack_exponentially():
    r"""CodeQL py/redos (HIGH) on the first version of the pattern above.

    The ambiguity: `--?` matches one dash or two, and `[A-Za-z-]+` also matches
    dashes, so ` --a` could be split two ways per repetition. Under the outer
    `*` that is 2**n, and the blowup only shows on input that FAILS to match,
    because success stops at the first accepting path.

    MEASURED against the pre-fix pattern, payload `"npx" + " --a"*n + " X"`:
    n=8 0.16ms, n=12 2.8ms, n=16 43.5ms, n=20 737ms, n=22 2,625ms -- x16 per +4.
    The fixed pattern stays flat at ~0.02ms across the same range.

    NOTE, because the first version of this test was useless: the payload has to
    begin with WHITESPACE after `npx`, since the repetition starts with `\s+`.
    An earlier attempt used repetitions of `--\t-`, which begins with a dash, so
    the group matched zero times and both patterns returned in microseconds -- a
    clean-looking result from a probe that could not have seen the bug.

    Asserted as a SCALING RATIO, min-of-3, because absolute wall-clock flakes by
    orders of magnitude on a shared box while exponential growth does not.
    """
    payload_small = "npx" + " --a" * 11 + " X"
    payload_large = "npx" + " --a" * 22 + " X"

    def fastest(payload: str) -> float:
        best = float("inf")
        for _ in range(3):
            start_t = time.perf_counter()
            _RESOLVER_PATTERN.search(payload)
            best = min(best, time.perf_counter() - start_t)
        return best

    # Guard-the-guard: the payload must actually FAIL to match. One that matches
    # never explores the backtracking space, so the timing below would be
    # measuring nothing.
    assert not _RESOLVER_PATTERN.search(payload_large), (
        "the adversarial payload matches, so it never exercises backtracking"
    )

    small = max(fastest(payload_small), 1e-6)
    large = fastest(payload_large)
    ratio = large / small
    assert ratio < 50, (
        f"doubling the adversarial input multiplied match time by {ratio:.1f}x "
        f"({small * 1000:.3f}ms -> {large * 1000:.3f}ms). The pre-fix pattern "
        "scored roughly 2**n here; a large ratio means the ambiguity is back."
    )


def test_installers_still_register_cbm_to_the_launcher_while_disabled():
    """Registration is not launching, and skipping it preserved real drift.

    Distinct from the gitnexus registration test above and from
    `test_installers_do_not_run_cbm_installer_while_kill_switch_is_active`:
    this is about CBM REGISTRATION while the sentinel is present, not about
    running its installer.

    When the kill switch is active, skipping the registration block leaves a
    PRE-EXISTING registration pointing at the bare `codebase-memory-mcp` binary
    untouched — which bypasses the launcher entirely, starts the uncapped raw
    server in the next session, and stays uncapped after the sentinel is removed
    until somebody runs the installer again. That stale registration is exactly
    the drift `scripts/lib/mcp_register.sh` exists to repair.

    Registering while disabled is safe because the launcher is FAIL-CLOSED on
    the sentinel, which this test also pins — the argument only holds while that
    remains true, so it is asserted rather than assumed.
    """
    launcher = (REPO_ROOT / ".claude" / "mcp" / "run-codebase-memory").read_text()
    assert "codebase-memory-mcp.disabled" in launcher and "exit 1" in launcher, (
        "the launcher is no longer fail-closed on the kill switch, so registering "
        "while disabled is no longer safe and that change must be revisited"
    )

    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        assert "registration skipped (machine kill switch active)" not in text, (
            f"{relative} still skips registration while disabled, leaving a stale "
            "direct-binary registration in place"
        )
        assert "run-codebase-memory" in text, relative


def _resolver(tmp_path, script: str, env_extra: dict | None = None):
    """Run a snippet against the SHIPPED resolver, in a controlled environment."""
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    env = {"PATH": f"{tmp_path}/bin:/usr/bin:/bin", **(env_extra or {})}
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "bash", str(helper)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _fake_install(root: pathlib.Path, version: str, *, with_manifest: bool) -> pathlib.Path:
    """A gitnexus that reports its version by RUNNING `node`, like the real one."""
    pkgdir = root / "lib" / "node_modules" / "gitnexus"
    (pkgdir / "dist" / "cli").mkdir(parents=True, exist_ok=True)
    entry = pkgdir / "dist" / "cli" / "index.js"
    # Executes `node`, exactly as the npm-installed entry point does — which is
    # the point: whatever `node` is on PATH decides what running it prints.
    entry.write_text('#!/usr/bin/env node\n// entry\n')
    if with_manifest:
        (pkgdir / "package.json").write_text(
            json.dumps({"name": "gitnexus", "version": version}) + "\n"
        )
    binary = root / "bin" / "gitnexus"
    binary.parent.mkdir(parents=True, exist_ok=True)
    # A SYMLINK into the package, which is how npm actually installs a bin — and
    # it is load-bearing for this fixture, not incidental: the version reader
    # resolves the link to find the manifest beside it. A wrapper script instead
    # of a symlink models the wrong layout and the reader correctly finds no
    # manifest, which is how the first version of this test failed.
    entry.chmod(0o755)
    binary.symlink_to(entry)
    return binary


def test_the_version_of_a_candidate_is_read_without_executing_it(tmp_path):
    """A stubbed `node` must not be able to answer for GitNexus's version.

    GitNexus is a Node script, so running it runs whatever `node` is first on
    PATH. Any caller that stubs node — which the launcher's own node-version
    gate forces its tests to do — made every Node-based candidate report the
    STUB's output. MEASURED before the fix: with a stub echoing `v22.22.2`, a
    real 1.6.12 install "reported" v22.22.2, the shadow scan read that as a
    conflict, and the launcher refused with "GitNexus is not installed".
    """
    (tmp_path / "bin").mkdir(exist_ok=True)
    stub = tmp_path / "bin" / "node"
    stub.write_text("#!/bin/sh\necho v22.22.2\n")
    stub.chmod(0o755)
    install = _fake_install(tmp_path / "real", "1.6.12", with_manifest=True)

    out = _resolver(tmp_path, f'_genesis_gitnexus_version_of "{install}"')
    assert out.stdout.strip() == "1.6.12", (
        f"read {out.stdout.strip()!r} — the stubbed node answered instead of the "
        "package manifest"
    )
    # CONTROL: with no manifest there is nothing to read, so it falls back to
    # executing — and then the stub DOES answer. That is the behaviour being
    # traded away, stated so the fallback is not mistaken for a guarantee.
    bare = _fake_install(tmp_path / "bare", "1.6.12", with_manifest=False)
    out = _resolver(tmp_path, f'_genesis_gitnexus_version_of "{bare}"')
    assert out.stdout.strip() == "v22.22.2", out.stdout


def test_a_resolver_that_finds_nothing_reports_failure(tmp_path):
    """The fail-open. An empty candidate list must not resolve SUCCESSFULLY.

    `printf '%s\\n'` with no arguments still writes a newline, which the
    caller's `while read` turned into one empty candidate — so "no GitNexus
    anywhere" returned rc 0 with an empty path. `genesis_gitnexus_ensure_pin`
    then believed a binary was present, read an empty version, failed its semver
    check and returned 3 instead of installing.
    """
    (tmp_path / "bin").mkdir(exist_ok=True)
    npm = tmp_path / "bin" / "npm"
    npm.write_text('#!/bin/sh\nif [ "${1:-}" = config ]; then exit 0; fi\n')
    npm.chmod(0o755)

    out = _resolver(tmp_path, 'genesis_gitnexus_resolve_binary; echo "rc=$?"')
    assert "rc=1" in out.stdout, f"resolver did not report failure: {out.stdout!r}"
    assert out.stdout.replace("rc=1", "").strip() == "", (
        f"resolver printed a path when it found nothing: {out.stdout!r}"
    )


def test_an_explicit_override_ends_the_search(tmp_path):
    """`GITNEXUS_BIN` names the binary, so there is no ambiguity for the shadow
    scan to resolve. Scanning anyway refused a conflict the operator had already
    settled — and on any machine with a second install the launcher then said
    "not installed", which is false twice over."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    chosen = _fake_install(tmp_path / "chosen", "1.6.12", with_manifest=True)
    out = _resolver(
        tmp_path,
        'genesis_gitnexus_resolve_binary',
        {"GITNEXUS_BIN": str(chosen)},
    )
    assert out.stdout.strip() == str(chosen), out.stdout + out.stderr
