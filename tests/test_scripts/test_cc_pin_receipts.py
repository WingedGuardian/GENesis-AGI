"""CC pin-receipt check (scripts/check_cc_pin_receipts.py).

A change moving the Claude Code pin FORWARD must carry two gate receipts where
the person merging can read them. `origin` is the public repo, so merging the pin
IS the release.

WHAT THESE TESTS ARE SHAPED AROUND. The check's authority is the merge gate, not
a CI status, so the unit under test is a PURE function — `evaluate()` takes the
two pin-file CONTENTS and the body, and no test needs a repository. It never
raises for a policy outcome; every decision comes back as a Verdict, so a test
asserting `.blocked` is asserting the same field a gate will read.

Every case below was MEASURED during review as either a live EVASION or a live
OVER-REJECTION, and says which. The over-rejection cases carry equal weight: a
gate that rejects a legitimate receipt teaches the operator to route around it,
and the first person to hit one will be using this repo's own PR template.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_cc_pin_receipts.py"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Registered BEFORE exec: @dataclass resolves its module out of sys.modules
    # and raises AttributeError on a module that is not there.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


receipts = _load("check_cc_pin_receipts", "scripts/check_cc_pin_receipts.py")

BASE = 'CC_VERSION="${CC_VERSION:-2.1.218}"\n'
HEAD = 'CC_VERSION="${CC_VERSION:-2.1.246}"\n'
BOTH = (
    "CC-Gate-Changelog: read (2.1.218, 2.1.246] in full from CHANGELOG.md, 2026-08-27\n"
    "CC-Gate-Soak: 2.1.246 soaked 2026-08-25..2026-08-27, sweep clean, signed off\n"
)


def _evaluate(body: str = BOTH, head: str = HEAD, base: str = BASE):
    return receipts.evaluate(base_pin_text=base, head_pin_text=head, body=body)


def _assert_blocked(verdict, reason: str) -> None:
    assert verdict.blocked, f"expected a block, got: {verdict.message}"
    assert verdict.reason == reason, f"blocked for {verdict.reason!r}, wanted {reason!r}"


def _assert_passes(verdict) -> None:
    assert not verdict.blocked, f"expected a pass, got: {verdict.message}"


# ── the outcome channel itself ────────────────────────────────────────────


def test_blocking_outcomes_travel_through_the_blocked_field() -> None:
    """Guards the API shape, not a behaviour — and it is load-bearing.

    An earlier version returned a Verdict for passes and RAISED for blocks, so
    `blocked` was structurally incapable of being True while its own docstring
    invited `if evaluate(...).blocked:`. That call site would have been a
    permanently open gate whose tests all passed.
    """
    assert _evaluate(body="").blocked is True
    assert _evaluate(head="# no pin at all\n").blocked is True
    assert _evaluate(body="").missing, "the blocking reason must be reported, not just flagged"


# ── the pin comparison ────────────────────────────────────────────────────


def test_unchanged_pin_needs_no_receipts() -> None:
    _assert_passes(_evaluate(body="", head=BASE))


def test_backward_pin_is_exempt() -> None:
    """A rollback returns to a version that already ran here, and the downgrade
    path is the project's incident-recovery route — the reason a managed
    `requiredMinimumVersion` floor was rejected after a real 2.1.90 → 2.1.87
    rollback. No syntax to recall under incident pressure."""
    v = _evaluate(body="", head='CC_VERSION="${CC_VERSION:-2.1.100}"\n')

    _assert_passes(v)
    assert "BACKWARD" in v.message


def test_forward_pin_with_both_receipts_passes() -> None:
    _assert_passes(_evaluate())


def test_forward_pin_without_receipts_blocks() -> None:
    v = _evaluate(body="")

    _assert_blocked(v, "receipts")
    assert "CC-Gate-Changelog" in v.message
    assert "CC-Gate-Soak" in v.message


# ── reading the pin: refuse rather than guess ─────────────────────────────


def test_a_comment_decoy_no_longer_hides_a_bump() -> None:
    """MEASURED: the old unanchored first-match regex read the COMMENT.

    `# was: CC_VERSION=...` above the real pin made a forward bump parse as
    unchanged, so the gate never fired.
    """
    head = '# was: CC_VERSION="${CC_VERSION:-2.1.218}"\n' + HEAD

    _assert_blocked(_evaluate(body="", head=head), "receipts")


@pytest.mark.parametrize(
    "second",
    [
        'CC_VERSION="2.1.999"',
        '[ -n "${X:-}" ] || CC_VERSION="2.1.999"',
        'if true; then CC_VERSION="2.1.999"; fi',
        "true && CC_VERSION=2.1.999",
        'printf -v CC_VERSION "2.1.999"',
        "eval CC_VERSION=2.1.999",
    ],
)
def test_reassignment_in_any_shape_blocks(second: str) -> None:
    """MEASURED: bash resolves 2.1.999 while the pin line still reads 2.1.218.

    cc_version.sh is SOURCED, so a line the pin pattern does not recognise still
    decides the value. The first fix was line-ANCHORED and closed only the first
    shape here — the one it had a test for. Bash executes assignments after `;`,
    `||` and `&&` and inside one-line bodies, none of which start a line.
    """
    _assert_blocked(_evaluate(head=HEAD + second + "\n"), "unreadable-head")


def test_leading_zero_version_blocks_rather_than_comparing_equal() -> None:
    """MEASURED fail-OPEN: int() collapses 2.1.0246 and 2.1.246 to one tuple.

    So a pin written with a leading zero read as "unchanged" against the plain
    form and skipped the gate. npm cannot install it either.
    """
    v = _evaluate(
        head='CC_VERSION="${CC_VERSION:-2.1.0246}"\n',
        base='CC_VERSION="${CC_VERSION:-2.1.246}"\n',
    )

    _assert_blocked(v, "incomparable")


def test_a_leading_zero_on_the_BASE_still_blocks_and_is_not_treated_as_base_side() -> None:
    """The guard against over-generalising the base-side rule.

    Base-side faults do not block — but this one is not a base-side fault. It is
    reached only when the pin MOVES (identical pins return early), and CI cannot see
    it on a PR: the merge tree carries the good HEAD pin, so lockstep passes while
    main is red. Routing it to `unreadable-base` would let a real forward bump merge
    unchecked, which is the one hole the head/base split must not open.
    """
    v = _evaluate(
        head='CC_VERSION="${CC_VERSION:-2.1.250}"\n',
        base='CC_VERSION="${CC_VERSION:-2.1.0246}"\n',
    )

    _assert_blocked(v, "incomparable")


def test_unreadable_pin_blocks_it_does_not_skip() -> None:
    """"I cannot tell what THIS PR pins" is not a reason to wave a release
    through — it is the state a human must look at. Inverse of the original."""
    _assert_blocked(_evaluate(head="# no pin here at all\n"), "unreadable-head")


@pytest.mark.parametrize(
    "base",
    ["", "   \n", "# no pin here at all\n", BASE + 'CC_VERSION="${CC_VERSION:-9.9.9}"\n'],
    ids=["empty", "whitespace", "no-assignment", "assigned-twice"],
)
def test_an_unreadable_BASE_pin_does_NOT_block(base: str) -> None:
    """Inverted 2026-08-28, and the inversion is the whole point.

    A base-side fault belongs to the base branch: every open PR inherits it, none can
    repair it through the merge gate, and that gate has no override sigil — so
    blocking here wedged the entire repository, including the PR that would fix the
    pin. It is also redundant. MEASURED: each of these makes
    `check_cc_node_lockstep` exit 1, reddening the blocking CI workflow, which the
    merge gate refuses independently and WITH a `# ci-override` escape.

    The verdict must still SAY it verified nothing — a silent pass here would be the
    fail-open this change exists to make visible.
    """
    v = _evaluate(base=base)

    assert not v.blocked, f"a base-side fault must not block: {v.message}"
    assert v.reason == "unreadable-base", f"wrong reason: {v.reason!r}"
    assert "BASE branch" in v.message, v.message
    assert "could not run" in v.message, v.message


def test_invalid_utf8_pin_file_raises_from_the_adapter(tmp_path: Path) -> None:
    """MEASURED CRITICAL: UnicodeDecodeError is NOT an OSError.

    It escaped the local handler, reached the top-level catch-all, and became
    exit 0 — a forward bump with ZERO receipts passing, from one stray byte.
    """
    pin = tmp_path / "scripts" / "lib" / "cc_version.sh"
    pin.parent.mkdir(parents=True)
    pin.write_bytes(b'CC_VERSION="${CC_VERSION:-2.1.246}"\n# \xff\xfe\n')

    with pytest.raises(receipts.PinUnreadable) as exc:
        receipts.read_pin_head(tmp_path)

    assert "UTF-8" in str(exc.value)


# ── what counts as a receipt ──────────────────────────────────────────────


def test_receipts_hidden_in_an_html_comment_do_not_count() -> None:
    """MEASURED: they satisfied a line-anchored regex while rendering INVISIBLE.

    This repo's own PULL_REQUEST_TEMPLATE.md is built entirely from `<!-- -->`
    blocks, so a receipt inside one is the likely accident, not an exotic attack.
    """
    _assert_blocked(_evaluate(body=f"<!--\n{BOTH}-->\n"), "receipts")


def test_an_UNTERMINATED_comment_also_hides_them() -> None:
    """MEASURED: the regex stripper required a closer, so it LEFT this intact.

    A body ending `<!-- notes` with the receipts below renders them invisible
    while the check counted them — the visibility rule running backwards.
    Deleting a trailing `-->` is exactly how that happens by accident.
    """
    _assert_blocked(_evaluate(body=f"<!-- template notes\n{BOTH}"), "receipts")


def test_receipts_inside_a_code_fence_do_not_count() -> None:
    """A fence is how the FORMAT gets documented, so text in one describes a
    receipt rather than asserting one."""
    _assert_blocked(_evaluate(body=f"```\n{BOTH}```\n"), "receipts")


def test_a_fence_is_closed_only_by_its_own_marker() -> None:
    """MEASURED: `~~~` does not close a ``` block, so receipts under a mismatched
    closer still render as code while the old stripper treated them as visible."""
    _assert_blocked(_evaluate(body=f"~~~\n{BOTH}```\n"), "receipts")


@pytest.mark.parametrize(
    "value",
    [
        "​",  # zero-width space: non-whitespace to `\S`, invisible to a human
        ".",
        "n/a",
        "-",
        "TODO",
        "pending",
    ],
)
def test_degenerate_values_are_not_receipts(value: str) -> None:
    _assert_blocked(
        _evaluate(body=f"CC-Gate-Changelog: {value}\nCC-Gate-Soak: {value}\n"), "receipts"
    )


def test_a_placeholder_obfuscated_with_a_zero_width_char_is_still_a_placeholder() -> None:
    """This is what `_strip_formatting_chars` is actually FOR.

    A lone U+200B is already killed by the substance floor, so the parametrized
    case above does not exercise the helper at all — deleting it left the suite
    green. A ZWSP *inside* a template token is the real case.
    """
    assert receipts._value_is_real("<fr​om>") is False


def test_a_spaced_template_is_still_a_template() -> None:
    """MEASURED evasion: `< from >` walked straight past a membership test."""
    body = "CC-Gate-Changelog: read (< from >, < to >] from < source >\nCC-Gate-Soak: < candidate >\n"

    _assert_blocked(_evaluate(body=body), "receipts")


def test_unfilled_template_placeholders_do_not_count() -> None:
    body = (
        "CC-Gate-Changelog: read (<from>, <to>] in full from <source>, <date>\n"
        "CC-Gate-Soak: <candidate> on <where> <start>..<end>\n"
    )

    _assert_blocked(_evaluate(body=body), "receipts")


def test_a_bare_trailer_cannot_borrow_the_next_lines_value() -> None:
    """MEASURED: `\\s*` matches newlines under MULTILINE, so an empty
    `CC-Gate-Changelog:` consumed the following soak line as its value and BOTH
    markers came back satisfied by one real receipt."""
    v = _evaluate(body="CC-Gate-Changelog:\n" + BOTH.splitlines()[1] + "\n")

    _assert_blocked(v, "receipts")
    assert "CC-Gate-Changelog" in v.message


# ── over-rejection: the other failure direction ───────────────────────────


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("- ", ""),
        ("* ", ""),
        ("+ ", ""),
        ("- [x] ", ""),
        ("- [ ] ", ""),
        ("> ", ""),
        ("**", "**"),
        ("`", "`"),
        ("  ", ""),
    ],
    ids=["bullet-dash", "bullet-star", "bullet-plus", "task-done", "task-open",
         "blockquote", "bold", "code", "indented"],
)
def test_receipts_written_in_ordinary_markdown_still_count(prefix: str, suffix: str) -> None:
    """MEASURED over-rejection — and the one most likely to bite a real user.

    This repo's PULL_REQUEST_TEMPLATE.md ends in a `- [ ]` checklist under
    "## Testing", which is exactly where an author will write a soak receipt.
    The strict line-start form blocked every shape here, at merge time, on a
    public release.
    """
    body = (
        f"{prefix}CC-Gate-Changelog{suffix}: read (2.1.218, 2.1.246] in full, 2026-08-27\n"
        f"{prefix}CC-Gate-Soak{suffix}: 2.1.246 soaked, sweep clean, signed off\n"
    )

    _assert_passes(_evaluate(body=body))


def test_markdown_inside_a_real_value_still_counts() -> None:
    """MEASURED over-rejection under the old `<...>` shape heuristic: any angle
    bracket voided a genuine receipt, and markdown in a PR body is routine."""
    body = (
        "CC-Gate-Changelog: read (2.1.218, 2.1.246] <b>fully</b>, 2026-08-27\n"
        "CC-Gate-Soak: 2.1.246 soaked, sweep clean, signed off\n"
    )

    _assert_passes(_evaluate(body=body))


def test_a_leftover_template_line_above_a_filled_one_does_not_veto_it() -> None:
    """MEASURED: the scan took the FIRST match, so an abandoned template line
    above the real receipt rejected the real receipt."""
    _assert_passes(_evaluate(body="CC-Gate-Changelog: <source>\n" + BOTH))


def test_crlf_bodies_parse() -> None:
    """GitHub delivers CRLF. `\\r` is horizontal whitespace, so this must not
    silently stop matching."""
    _assert_passes(_evaluate(body=BOTH.replace("\n", "\r\n")))


def test_placeholder_set_covers_every_token_in_the_shipped_example() -> None:
    """Guards the anti-drift property.

    The earlier version of this test used `example.split()`, so it only saw
    tokens with no attached punctuation — `<from>` and `<to>`, the two most
    human-typed, were unguarded. Derive it the same way the module does.
    """
    for _why, example in receipts._RECEIPTS.values():
        for token in re.findall(r"<[^<>]+>", example):
            assert token.lower() in receipts._PLACEHOLDERS


def test_readable_body_is_linear_on_a_pathological_body() -> None:
    """MEASURED: the regex stripper took ~14.6s on 65_500 bytes of unterminated
    fence openers — reachable by any contributor within GitHub's body cap, and a
    likely SIGKILL on the merge gate, whose remaining checks are then skipped."""
    import time

    body = "```x\n" * 13_100
    start = time.perf_counter()
    receipts.readable_body(body)

    assert time.perf_counter() - start < 1.0


# ── the CLI adapter ───────────────────────────────────────────────────────


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
    )


@pytest.fixture
def bumped_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose HEAD moves the pin FORWARD with no receipts.

    Built rather than borrowed: pointing the CLI at the live repo made the old
    test vacuous, because the working tree's pin equals HEAD's pin, so BOTH modes
    exited 0 and the advisory invariant was never exercised.
    """
    pin = tmp_path / "scripts" / "lib"
    pin.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (pin / "cc_version.sh").write_text(BASE)
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    (pin / "cc_version.sh").write_text(HEAD)
    return tmp_path


def test_advisory_never_fails_while_enforcing_does(bumped_repo: Path, tmp_path: Path) -> None:
    """The advisory invariant AND its counterpart, on a REAL forward bump.

    Both halves matter: without the enforcing assertion, deleting `--advisory`
    handling entirely would leave this green.
    """
    body = bumped_repo / "body.md"
    body.write_text("no receipts here at all")
    common = ("--base-sha", "HEAD", "--body-file", str(body), "--repo-root", str(bumped_repo))

    advisory = _run_cli("--advisory", *common)
    enforcing = _run_cli(*common)

    assert advisory.returncode == 0, advisory.stderr
    assert "::warning" in advisory.stdout, "an advisory finding must surface as an annotation"
    assert enforcing.returncode == 1, enforcing.stdout + enforcing.stderr


def test_enforcing_mode_refuses_without_a_base(bumped_repo: Path) -> None:
    """A missing base is "I could not check", not "nothing to check"."""
    body = bumped_repo / "body.md"
    body.write_text("x")

    res = _run_cli("--base-sha", "", "--body-file", str(body), "--repo-root", str(bumped_repo))

    assert res.returncode == 1


def test_the_live_pin_file_is_readable_by_the_checker() -> None:
    """Smoke against the SHIPPED file — repo-state-dependent by design, so that
    a parser change which refuses the real pin fails here rather than in CI."""
    text = (_REPO_ROOT / "scripts" / "lib" / "cc_version.sh").read_text()

    assert receipts._pin_of(text, where="live") is not None
