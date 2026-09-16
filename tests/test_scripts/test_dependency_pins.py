"""Locks on dependency pins that are COUPLED to something else in the repo.

A pin whose correctness depends on another file's value is a convention, and
conventions decay silently — nothing fails when the two drift apart. These tests
are the chokepoint: they re-derive the coupling from both sources and fail when
it breaks, so the next person to bump either number is told.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_installers_enforce_the_declared_node_22_floor():
    install = (REPO_ROOT / "scripts" / "install.sh").read_text()
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap.sh").read_text()
    assert 'NODE_MAJOR="${NODE_MAJOR:-22}"' in install
    assert '[ "${ver:-0}" -ge 22 ]' in install
    assert '[[ "$major" -ge 22 ]]' in bootstrap


def test_installers_do_not_run_cbm_installer_while_kill_switch_is_active():
    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        sentinel = 'if [ -e "$HOME/.genesis/codebase-memory-mcp.disabled" ]; then'
        installer = "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/"
        assert text.index(sentinel) < text.index(installer), relative


def test_every_gitnexus_package_resolver_uses_the_shared_pin():
    """No install/index path may float to an npm tag or range."""
    dynamic_resolver = re.compile(
        r"\b(?:"
        r"npx(?:\s+--?[A-Za-z-]+)*|"
        r"npm\s+(?:exec|x)(?:\s+--[A-Za-z-]+)*|"
        r"pnpm\s+dlx|yarn\s+dlx|bunx"
        r")\s+(?:--\s+)?gitnexus(?:@|\s|$)"
    )
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
        registration_block = text[text.rfind("if ", 0, text.index(registration)) :]
        assert '-x "$' in registration_block
        assert ".claude/mcp/run-gitnexus" in registration_block
        assert "_GITNEXUS_PIN_READY" not in registration_block


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
