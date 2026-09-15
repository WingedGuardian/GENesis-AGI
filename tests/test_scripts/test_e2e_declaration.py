"""The `E2E:` PR-body declaration parser (scripts/e2e_declaration.py).

Two directions matter and both are tested here, because a parser measured on only
one of them is half measured:

  * A real declaration in any shape a PR body is ACTUALLY written in must be READ
    (markdown bullets, checkboxes, bold/backtick wrappers, CRLF, a filled line under
    a leftover template line). A gate that blocks a compliant PR at merge time is how
    an operator learns to route around it.
  * Text that only MENTIONS an E2E, or that leaves the decision unmade, must NOT read
    as a declaration — prose, a fenced example, an HTML-comment template, a bare
    `none`, a placeholder.

The `none — <reason>` form is the sharp case: it is a trailing qualification, the
exact shape `repo_pulse.FOLLOWUP_MARKER_RE` anchors at both ends to REJECT. Line-start
anchoring plus a substance check is what replaces that anchoring here, so both are
exercised directly.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location(
        "e2e_declaration", _SCRIPTS / "e2e_declaration.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def e2e():
    return _load()


# ── the two valid forms ──────────────────────────────────────────────────────


def test_the_sibling_scanner_actually_loads(e2e):
    """The module claims it uses the SAME body scanner as the pin gate "so the two
    can never disagree". That was FALSE in every run: the sibling resolves its
    dataclasses out of sys.modules, so exec-ing it unregistered raised
    AttributeError and the load silently fell back to the local copy (Kimi P2,
    2026-09-06). A claim of shared behaviour has to be executable, or it is just a
    comment."""
    assert e2e._load_sibling_readable_body() is not None


def test_the_two_scanners_agree_on_marker_visibility(e2e):
    """Guard-the-guard: with the sibling now genuinely loading, assert the local
    fallback and the shared scanner reach the same verdict on the cases that matter,
    so a future divergence is caught rather than assumed away."""
    bodies = [
        "E2E: a plain declaration\n",
        "<!--\nE2E: hidden in a comment\n-->\nreal text\n",
        "```\nE2E: inside a fence\n```\n",
        "intro\n<!-- unterminated\nE2E: after an open comment\n",
    ]
    for body in bodies:
        shared = e2e.readable_body(body)
        local = e2e._local_readable_body(body)
        assert bool(e2e._MARKER_RE.search(shared)) == bool(e2e._MARKER_RE.search(local)), body


def test_plan_form_is_read(e2e):
    r = e2e.parse_e2e("Some body\n\nE2E: run the migration on a fresh DB and check 92 applied\n")
    assert r["kind"] == "plan"
    assert "fresh DB" in r["text"]


def test_none_form_with_reason_is_read(e2e):
    r = e2e.parse_e2e("E2E: none — docs only, no runtime surface to verify\n")
    assert r["kind"] == "none"
    assert "docs only" in r["text"]


@pytest.mark.parametrize("dash", ["—", "–", "-"])
def test_none_accepts_em_en_and_plain_dashes(e2e, dash):
    """Authors type whichever dash their editor produces; all three are the same
    declaration."""
    r = e2e.parse_e2e(f"E2E: none {dash} prose-only change, nothing executes\n")
    assert r["kind"] == "none"


@pytest.mark.parametrize(
    "phrasing",
    [
        "none because this is documentation only",
        "none, docs only with no runtime surface",
        "none: prose change with no runtime surface",
        "none — docs only with no runtime surface",
        # NOTE: whitespace-ONLY separation is deliberately NOT here. An earlier
        # revision accepted it, which is exactly what let `none of the existing
        # tests cover …` — a real PLAN — classify as `none`. A separator (or an
        # explicit "needed/required", or nothing at all) is the signal; a space
        # is not.
    ],
)
def test_none_is_recognised_however_the_author_punctuates_it(e2e, phrasing):
    """FOUND BY MUTATION, not by use: requiring an em dash classified every other
    natural phrasing as a PLAN. Invisible at the gate (both kinds pass), which is
    why it needed a mutation to surface — the damage lands downstream, where the
    repo-pulse worker turns a `plan` into a hot follow-up row and a docs PR
    acquires a junk obligation reading "Run the declared E2E: none, docs only"."""
    r = e2e.parse_e2e(f"E2E: {phrasing}\n")
    assert r["kind"] == "none", f"{phrasing!r} is a `none` declaration, not a plan"


@pytest.mark.parametrize(
    "plan",
    [
        # "nonetheless" is stopped by \b alone — it passed for the wrong reason and
        # left the REAL swallow untested (architect SHOULD-FIX, 2026-09-06).
        "nonetheless run the migration and check the row count",
        # THE case that was actually broken: `none` + ordinary prose. With the
        # separator optional, this classified as `none` — a real plan swallowed,
        # which in PR-2b means the PR silently acquires NO obligation row. That is
        # the spec's failure mode (B) arriving through the parser.
        "none of the existing tests cover the new path; run it by hand after merge",
        "none needed beyond restarting genesis-server and watching the log",
    ],
)
def test_a_plan_that_merely_starts_with_a_none_ish_word_is_still_a_plan(e2e, plan):
    assert e2e.parse_e2e(f"E2E: {plan}\n")["kind"] == "plan", plan


# ── shapes a PR body is actually written in ──────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "E2E: deploy and curl the health endpoint",
        "- E2E: deploy and curl the health endpoint",
        "* E2E: deploy and curl the health endpoint",
        "> E2E: deploy and curl the health endpoint",
        "- [ ] E2E: deploy and curl the health endpoint",
        "- [x] E2E: deploy and curl the health endpoint",
        "**E2E**: deploy and curl the health endpoint",
        "`E2E`: deploy and curl the health endpoint",
        "  E2E:   deploy and curl the health endpoint",
        "e2e: deploy and curl the health endpoint",
    ],
)
def test_markdown_wrappers_are_tolerated(e2e, line):
    assert e2e.parse_e2e(f"intro\n{line}\noutro\n")["kind"] == "plan"


def test_crlf_body_still_matches(e2e):
    """GitHub's textarea stores CRLF; a line-anchored `$` would otherwise never
    match a real line (the defect repo_pulse._pr_text exists to fix)."""
    assert (
        e2e.parse_e2e("intro\r\nE2E: restart and verify the unit is active\r\n")["kind"] == "plan"
    )


def test_last_valid_occurrence_wins_over_a_template_line_above(e2e):
    """A leftover template line ABOVE a filled one must not veto it."""
    body = (
        "E2E: <one-line plan for the post-merge verification>\n"
        "E2E: run the assembler on a fixture repo and diff the output\n"
    )
    r = e2e.parse_e2e(body)
    assert r["kind"] == "plan"
    assert "assembler" in r["text"]


# ── text that must NOT read as a declaration ─────────────────────────────────


def test_prose_mentioning_e2e_mid_line_is_not_a_declaration(e2e):
    """The marker is short, so line-START anchoring is what keeps ordinary prose
    from becoming a declaration."""
    body = "I skipped the E2E: it needs hardware we do not have in CI.\n"
    assert e2e.parse_e2e(body)["kind"] == "absent"


def test_declaration_inside_an_html_comment_is_invisible(e2e):
    """This repo's PULL_REQUEST_TEMPLATE.md is built from comment blocks, so a
    declaration that never renders is the likely accident, not an exotic one."""
    body = "<!--\nE2E: <one-line plan>\n-->\nreal body text\n"
    assert e2e.parse_e2e(body)["kind"] == "absent"


def test_declaration_inside_a_fence_is_documentation_not_a_claim(e2e):
    body = "How to declare one:\n\n```\nE2E: run the thing and check the output\n```\n"
    assert e2e.parse_e2e(body)["kind"] == "absent"


def test_an_unterminated_comment_hides_to_the_end(e2e):
    """Both-ends regexes leave an unterminated opener intact, inverting the rule;
    the line scanner hides to the end, as CommonMark does."""
    body = "intro\n<!-- template start\nE2E: this never renders\n"
    assert e2e.parse_e2e(body)["kind"] == "absent"


def test_a_fenced_html_example_does_not_swallow_a_later_declaration(e2e):
    """MEASURED on the sibling checker: an unmatched `<!--` inside a fence turned
    comment-mode on, which hid the closing fence, which swallowed every marker after
    it — a compliant PR blocked by a gate with no override."""
    body = (
        "Example:\n\n```html\n<!-- an unclosed comment in an example\n```\n\n"
        "E2E: restart the server and confirm the endpoint answers 200\n"
    )
    assert e2e.parse_e2e(body)["kind"] == "plan"


def test_empty_value_does_not_borrow_the_next_line(e2e):
    """Horizontal-whitespace-only around the colon. Plain `\\s*` crosses newlines,
    which let an EMPTY marker take the following line as its value."""
    body = "E2E:\nSome unrelated following line with plenty of words in it\n"
    assert e2e.parse_e2e(body)["kind"] in {"absent", "invalid"}


@pytest.mark.parametrize(
    "value", ["<one-line plan for the post-merge verification>", "< one-line plan >"]
)
def test_unfilled_placeholder_is_invalid_not_a_plan(e2e, value):
    r = e2e.parse_e2e(f"E2E: {value}\n")
    assert r["kind"] == "invalid"


@pytest.mark.parametrize("value", ["TODO", "tbd", "pending", "n/a", "yes", "?"])
def test_refusal_words_are_invalid(e2e, value):
    assert e2e.parse_e2e(f"E2E: {value}\n")["kind"] == "invalid"


def test_bare_none_without_a_reason_is_invalid(e2e):
    """The reason is the whole point of the `none` form — a bare `none` is the
    decision left unmade wearing the grammar of a decision."""
    r = e2e.parse_e2e("E2E: none\n")
    assert r["kind"] == "invalid"
    assert "reason" in r["detail"].lower()


def test_none_with_a_placeholder_reason_is_invalid(e2e):
    r = e2e.parse_e2e("E2E: none — <reason there is no runtime surface to verify>\n")
    assert r["kind"] == "invalid"


def test_absent_and_empty_bodies(e2e):
    assert e2e.parse_e2e("nothing here\n")["kind"] == "absent"
    assert e2e.parse_e2e("")["kind"] == "absent"
    assert e2e.parse_e2e(None)["kind"] == "absent"


# ── performance: the merge gate runs under a wall clock ──────────────────────


def test_pathological_body_parses_fast(e2e):
    """A 64KB body of unterminated comment openers is what a non-greedy regex pair
    costs ~14s on. The line scanner is O(n) — bound it here so a future
    'simplification' back to regexes fails loudly instead of quietly eating the
    merge gate's budget."""
    body = ("<!-- " * 8192) + "\nE2E: run it\n"
    start = time.monotonic()
    e2e.parse_e2e(body)
    assert time.monotonic() - start < 2.0, "parse must stay well inside the gate budget"


def test_body_is_bounded(e2e):
    """Beyond GitHub's cap the scan must not grow without bound."""
    body = ("filler line\n" * 20000) + "E2E: a plan far past the cap\n"
    start = time.monotonic()
    e2e.parse_e2e(body)
    assert time.monotonic() - start < 2.0


# ── the cutoff ───────────────────────────────────────────────────────────────


def test_pre_cutoff_pr_is_exempt(e2e):
    assert e2e.is_pre_cutoff("2026-01-01T00:00:00Z") is True


def test_post_cutoff_pr_is_bound(e2e):
    assert e2e.is_pre_cutoff("2099-01-01T00:00:00Z") is False


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-45T99:99:99Z"])
def test_unreadable_created_at_is_not_exempt(e2e, bad):
    """Fails CLOSED: treating an unparseable timestamp as 'old' would turn a
    finite transition window into a permanent hole."""
    assert e2e.is_pre_cutoff(bad) is False


def test_every_example_in_the_guidance_passes_the_parser(e2e):
    """A remedy the gate PRINTS must survive the gate.

    MEASURED (architect, 2026-09-06): with the substance floor at 12 alnum chars,
    `E2E: none — docs only` — the exact string GUIDANCE offers as the example —
    was rejected. On a gate with no override sigil that is a closed loop: blocked,
    type the suggested line verbatim, blocked identically, with nothing naming an
    undocumented length rule. This test makes the class unrepeatable rather than
    fixing the one number."""
    import re as _re

    # GUIDANCE only — it is the text the GATE prints. The module docstring also
    # says `E2E: none` while explaining that a bare one is INVALID, and scraping
    # that would assert the parser accepts the very shape the docs call refused.
    examples = _re.findall(r"E2E: ([^\n]+)", e2e.GUIDANCE)
    concrete = [
        ex.strip()
        for ex in examples
        if "<" not in ex  # placeholder templates are meant to be rejected
    ]
    # BOTH forms, not just `none`: scraping only the none-examples would let a
    # broken PLAN example ship (Kimi P3, 2026-09-06).
    kinds = {e2e.parse_e2e(f"E2E: {ex}\n")["kind"] for ex in concrete}
    assert concrete, "the guidance must show at least one concrete example"
    for ex in concrete:
        assert e2e.parse_e2e(f"E2E: {ex}\n")["kind"] in {"none", "plan"}, (
            f"guidance offers {ex!r} but the parser rejects it"
        )
    assert kinds == {"none", "plan"}, (
        f"the guidance must show a concrete example of BOTH forms; got {kinds}"
    )


@pytest.mark.parametrize("reason", ["docs only", "prose-only", "no runtime surface"])
def test_short_but_real_reasons_are_accepted(reason, e2e):
    """The floor admits the short, honest reasons a docs PR actually gives."""
    assert e2e.parse_e2e(f"E2E: none — {reason}\n")["kind"] == "none"


@pytest.mark.parametrize("junk", ["x", "ok", "n/a", "?"])
def test_contentless_reasons_are_still_refused(junk, e2e):
    """The control that keeps the lowered floor honest."""
    assert e2e.parse_e2e(f"E2E: none — {junk}\n")["kind"] == "invalid"


def test_empty_marker_is_reported_as_empty_not_absent(e2e):
    """The shipped PR template ends with a bare `E2E:` line, so this is the state
    EVERY template-created PR starts in. Reporting "no E2E: line in the PR body"
    to an author looking at a body that visibly contains one is the `invalid`
    bucket failing at the one case it most owes (architect SHOULD-FIX)."""
    r = e2e.parse_e2e("## Testing\n\nE2E:\n")
    assert r["kind"] == "invalid"
    assert "EMPTY" in r["detail"]


def test_the_shipped_pr_template_does_not_satisfy_the_gate(e2e):
    """Guard-the-guard on the whole convention: the template's guidance lives in an
    HTML comment and its declaration line is bare, so an untouched template must
    read as a decision NOT made. If this ever passes, every template-created PR
    silently satisfies the gate."""
    template = (
        Path(__file__).resolve().parents[2] / ".github" / "PULL_REQUEST_TEMPLATE.md"
    ).read_text()
    assert e2e.parse_e2e(template)["kind"] in {"absent", "invalid"}


def test_guidance_names_both_forms_and_the_seam(e2e):
    """The block message must teach the whole contract: `none` is legitimate, and it
    does not release the validator (spec §8.13)."""
    g = e2e.GUIDANCE
    assert "E2E: <one-line plan" in g and "E2E: none —" in g
    assert "validator" in g.lower()
