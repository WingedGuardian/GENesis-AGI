"""Pin reading must agree with bash (scripts/ci/cc_pin_parse.py).

A CI guard over the Claude Code pin is only as good as its answer to "what does
the runtime actually use?". The guards previously used an unanchored
``re.search`` for ``CC_VERSION="${CC_VERSION:-…}"``, which diverges from bash
twice: ``search`` returns the FIRST match while bash keeps the LAST assignment,
and an unanchored pattern matches inside a ``#`` comment that bash never
executes.

The consequence was live on main: one commented-out decoy line above the real
pin made ``check_cc_node_lockstep.py`` read a version the machine never
installs, so a CC bump could be checked against the wrong Node floor entirely.

The load-bearing test here is ``test_parser_agrees_with_bash``: rather than
asserting the parser matches a value someone typed into a fixture, it sources
each fixture with real bash and asserts the parser reports what bash reports.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PIN_FILE = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


pin = _load("cc_pin_parse", "scripts/ci/cc_pin_parse.py")
lockstep = _load("check_cc_node_lockstep", "scripts/check_cc_node_lockstep.py")


def _bash_resolves(text: str, var: str, tmp_path: Path) -> str:
    """What bash ACTUALLY sets `var` to after sourcing `text`.

    Ground truth for the parser. Safe here because every fixture is written by
    this test file — the parser itself must never source a real pin file, which
    on a pull_request is attacker-controlled.
    """
    f = tmp_path / "pin.sh"
    f.write_text(text)
    r = subprocess.run(
        ["bash", "-c", f'unset {var}; . "{f}"; printf "%s" "${{{var}:-}}"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


# ── the property that matters ─────────────────────────────────────────────

_FIXTURES = {
    "plain": '#!/usr/bin/env bash\nCC_VERSION="${CC_VERSION:-2.1.246}"\n',
    "commented decoy above": (
        "#!/usr/bin/env bash\n"
        '# was: CC_VERSION="${CC_VERSION:-2.1.218}"\n'
        'CC_VERSION="${CC_VERSION:-2.1.246}"\n'
    ),
    "commented decoy below": (
        'CC_VERSION="${CC_VERSION:-2.1.246}"\n#   old: CC_VERSION="${CC_VERSION:-2.1.100}"\n'
    ),
    "indented comment decoy": (
        '    # CC_VERSION="${CC_VERSION:-1.0.0}"\nCC_VERSION="${CC_VERSION:-2.1.246}"\n'
    ),
    "trailing comment on the real line": (
        'CC_VERSION="${CC_VERSION:-2.1.246}"   # bumped 2026-08\n'
    ),
    "prose mentioning a version": (
        "# See the 2.1.90 -> 2.1.87 rollback for why there is no floor.\n"
        'CC_VERSION="${CC_VERSION:-2.1.246}"\n'
    ),
}


@pytest.mark.parametrize("name", list(_FIXTURES))
def test_parser_agrees_with_bash(name, tmp_path: Path) -> None:
    """The parser's answer must equal the shell's answer, fixture by fixture."""
    text = _FIXTURES[name]
    expected = _bash_resolves(text, "CC_VERSION", tmp_path)

    assert pin.parse_cc_version(text) == expected, (
        f"{name}: parser and bash disagree — the guard would check a version "
        f"the machine never installs"
    )


def test_the_old_first_match_regex_would_have_failed_these(tmp_path: Path) -> None:
    """Pins the REGRESSION, not just the fix.

    Reproduces the exact pattern the guards used to carry. If someone reverts to
    an unanchored first-match search, this test says so in the same terms the
    incident did.
    """
    import re

    old = re.compile(r'CC_VERSION="?\$\{CC_VERSION:-([0-9]+\.[0-9]+\.[0-9]+)\}"?')
    text = _FIXTURES["commented decoy above"]

    old_answer = old.search(text).group(1)
    bash_answer = _bash_resolves(text, "CC_VERSION", tmp_path)

    assert old_answer != bash_answer, "fixture no longer reproduces the bug"
    assert pin.parse_cc_version(text) == bash_answer


# ── refusing to guess ─────────────────────────────────────────────────────


def test_duplicate_assignment_is_unparseable_not_last_wins() -> None:
    """Bash takes the last; this deliberately refuses instead.

    Nobody writes two effective pin assignments by accident, so "the last one
    wins" would be the guard quietly picking a winner in a file engineered to
    have two. `None` forces the caller to fail closed.
    """
    text = 'CC_VERSION="${CC_VERSION:-2.1.218}"\nCC_VERSION="${CC_VERSION:-2.1.246}"\n'

    assert pin.parse_cc_version(text) is None


def test_absent_is_none() -> None:
    assert pin.parse_cc_version('NODE_MAJOR="${NODE_MAJOR:-22}"\n') is None
    assert pin.parse_node_major('CC_VERSION="${CC_VERSION:-2.1.1}"\n') is None


def test_node_major_parses_and_is_an_int() -> None:
    got = pin.parse_node_major('NODE_MAJOR="${NODE_MAJOR:-22}"\n')

    assert got == 22
    assert isinstance(got, int)


# ── the real file, and the guard that consumes it ─────────────────────────


def test_the_live_pin_file_parses() -> None:
    """Smoke: the shipped cc_version.sh must be readable by this parser."""
    text = _PIN_FILE.read_text()

    assert pin.parse_cc_version(text) is not None
    assert pin.parse_node_major(text) is not None


def test_lockstep_guard_reads_past_a_comment_decoy(tmp_path: Path) -> None:
    """End-to-end through the guard that ships today.

    Before the fix this returned ('2.1.100', 18) for a file bash resolves as
    ('2.1.246', 22) — the Node floor would have been checked against a version
    that is not installed anywhere.
    """
    f = tmp_path / "cc_version.sh"
    f.write_text(
        "#!/usr/bin/env bash\n"
        '# example: CC_VERSION="${CC_VERSION:-2.1.100}" NODE_MAJOR="${NODE_MAJOR:-18}"\n'
        'CC_VERSION="${CC_VERSION:-2.1.246}"\n'
        'NODE_MAJOR="${NODE_MAJOR:-22}"\n'
    )

    assert lockstep.parse_pins(f) == ("2.1.246", 22)


def test_lockstep_guard_fails_closed_on_an_ambiguous_pin(tmp_path: Path) -> None:
    """A file it cannot read must BLOCK, never resolve to something plausible."""
    f = tmp_path / "cc_version.sh"
    f.write_text(
        'CC_VERSION="${CC_VERSION:-2.1.218}"\n'
        'CC_VERSION="${CC_VERSION:-2.1.246}"\n'
        'NODE_MAJOR="${NODE_MAJOR:-22}"\n'
    )

    with pytest.raises(lockstep.LockstepViolation) as exc:
        lockstep.parse_pins(f)

    assert "CC_VERSION" in str(exc.value)
