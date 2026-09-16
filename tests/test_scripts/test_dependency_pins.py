"""Locks on dependency pins that are COUPLED to something else in the repo.

A pin whose correctness depends on another file's value is a convention, and
conventions decay silently — nothing fails when the two drift apart. These tests
are the chokepoint: they re-derive the coupling from both sources and fail when
it breaks, so the next person to bump either number is told.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

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
