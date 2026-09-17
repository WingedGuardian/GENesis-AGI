"""Locks on dependency pins that are COUPLED to something else in the repo.

A pin whose correctness depends on another file's value is a convention, and
conventions decay silently — nothing fails when the two drift apart. These tests
are the chokepoint: they re-derive the coupling from both sources and fail when
it breaks, so the next person to bump either number is told.
"""

from __future__ import annotations

import re
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
    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        sentinel = 'if [ -e "$HOME/.genesis/codebase-memory-mcp.disabled" ]; then'
        installer = "https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/"
        assert text.index(sentinel) < text.index(installer), relative


#: Dynamic npm-resolver invocations of gitnexus. Hoisted to module scope so the
#: ReDoS regression test below measures the SHIPPED pattern rather than a copy
#: that could drift away from it while still passing.
#:
#: `--?[A-Za-z][A-Za-z-]*` requires a LETTER directly after the dashes. That is
#: not cosmetic: with `[A-Za-z-]+` able to match a dash while `--?` also could,
#: each option token split two ways per repetition and the outer `*` made it
#: 2**n (CodeQL py/redos, HIGH).
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
    n=8 0.16ms, n=12 2.8ms, n=16 43.5ms, n=20 737ms, n=22 2,625ms -- x16 per +4,
    i.e. 2**n. The fixed pattern stays flat at ~0.02ms across the same range.

    NOTE, because the first version of this test was useless: the payload has to
    begin with WHITESPACE after `npx`, since the repetition starts with `\s+`.
    An earlier attempt used repetitions of `--\t-`, which begins with a dash, so
    the group matched zero times and both patterns returned in microseconds --
    a clean-looking result from a probe that could not have seen the bug.

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

    # Guard-the-guard: the payload must actually FAIL to match. A payload that
    # matches never explores the backtracking space, so a passing assertion
    # below would mean nothing.
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

    When the kill switch is active, skipping the registration block leaves a
    PRE-EXISTING registration pointing at the bare `codebase-memory-mcp` binary
    untouched — which bypasses the launcher entirely and starts the uncapped raw
    server in the next session, then stays uncapped after the sentinel is
    removed until somebody runs the installer again. That stale registration is
    precisely the drift `scripts/lib/mcp_register.sh` exists to repair.

    Registering while disabled is safe because the launcher is FAIL-CLOSED on
    the sentinel, which this test also pins — the argument only holds while that
    remains true.
    """
    launcher = (REPO_ROOT / ".claude" / "mcp" / "run-codebase-memory").read_text()
    assert "codebase-memory-mcp.disabled" in launcher and "exit 1" in launcher, (
        "the launcher is no longer fail-closed on the kill switch, so registering "
        "while disabled is no longer safe and this change must be revisited"
    )

    for relative in ("scripts/install.sh", "scripts/bootstrap.sh"):
        text = (REPO_ROOT / relative).read_text()
        assert "registration skipped (machine kill switch active)" not in text, (
            f"{relative} still skips registration while disabled, leaving a stale "
            "direct-binary registration in place"
        )
        assert "run-codebase-memory" in text, relative
