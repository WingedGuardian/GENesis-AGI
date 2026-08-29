"""A guard's defeat conditions must not be written down in prose.

This repository is public, and its hooks are the approval gates. A sentence
that names the construct which defeats a gate AND the fact that it defeated it
is a recipe, whether or not the hole is currently closed — a reader gets the
search narrowed for free, and a fix can always be reverted, missed on a sibling
surface, or not yet merged.

The rule was a sentence in the dev skill for weeks and did not hold. It was
applied to one surface and missed the sibling: the shape was redacted from the
skill document and left spelled out in a guard's own docstring, in this repo,
in the same session. A sentence cannot notice that. This can.

WHAT IS FORBIDDEN — the pairing, not the shape
  A trigger shape is legitimate, and necessary, as FIXTURE DATA: a guard's test
  cannot cover a construct it may not contain, and the guard's own code has to
  match on something. What is forbidden is PROSE — a comment, a docstring, a
  markdown line — that names a shell construct and, near it, says a gate was
  defeated. That conjunction is the recipe; either half alone is not.

  So this scans comments, docstrings and markdown, and deliberately does NOT
  scan string literals or test parameters. `cmd = "<the shape>"` is fine.
  `# <the shape> makes the guard fall silent` is not.

  That distinction is drawn by `tokenize`, not by a regex over raw source. A
  regex cannot tell a comment from a hash-prefixed line INSIDE a string
  literal, so the first version of this file scanned exactly the fixture data
  the paragraph above permits — the docstring claimed a property the code did
  not have. The one place the coarse scan survives is a file that will not
  tokenize at all, where over-reporting is the safe direction: a false positive
  is a loud CI failure, a false negative is a published recipe.

WHAT THIS IS NOT
  Not a classifier, and it cannot be. Prose is open-set: someone can describe
  the same thing in words this never sees, and no list of terms closes that.
  It is a tripwire for the shapes we have actually leaked, sized to catch the
  next instance of a mistake already made twice. Treat a pass as "the known
  recipes are absent", never as "this diff teaches nobody anything" — the
  judgement call still belongs to whoever writes the sentence.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent

# Assembled from fragments so this file does not itself carry the terms it
# forbids — the same convention the guard suites use for trigger literals.
_CONSTRUCTS = (
    "here" + "-doc",
    "heredoc",
    "ANSI-C",
    "apostrophe",
    "line continuation",
)
# The other half of the pairing: the claim that a gate stopped working.
_OUTCOMES = (
    "silent allow",
    "silently allow",
    "fell silent",
    "falls silent",
    "fall silent",
    "disarm",
    "bypass",
    "defeat",
    "slipped past",
    "sailed past",
)

# Negation, because the first version of this file had the defect it was written
# to catch. MEASURED on first run: 7 hits across the repo, and all 7 were prose
# REASSURING the reader — "never a security bypass", "cannot open a bypass",
# "does not bypass". A bag of outcome words used as a claim detector is exactly
# the shape that made a merge gate read "not a hard block" as a hard block, and
# it is documented in this repo. Skip an outcome that is denied rather than
# asserted; a denial is the opposite of a recipe.
_NEGATORS = (
    "never",
    "not ",
    "n't",
    "cannot",
    "no ",
    "rather than",
    "instead of",
    "without",
)
_NEGATION_LOOKBACK = 40

# Concessive phrases that CONTAIN a negator but assert rather than deny: "not
# only X but Y" claims X. Left unhandled, the substring "not " inside them
# suppressed a real recipe — the negation fix's own mirror-image defect, since
# a negation list is itself a bag of substrings used as a claim detector. These
# are stripped from the lookback window BEFORE the negator scan, so the
# remaining text is judged on its own.
_CONCESSIVE = (
    "not only",
    "not just",
    "not merely",
    "not simply",
)

# How close the two halves must be to read as one claim. A construct in one
# paragraph and the word "bypass" three paragraphs later is not a recipe.
_WINDOW = 240

# Known pairings that are allowed, keyed by (file, construct, outcome) — NOT by
# file. Waiving a whole file would blind this exactly where it matters most: the
# push guard is the largest surface and the likeliest place for the next leak,
# so a file-level waiver there would be worse than having no check. A triple
# waives the one sentence and leaves everything else in that file watched.
#
# The reason lives here rather than as an inline marker, because an inline
# escape hatch silences by line and gets copied to the next file without its
# justification.
_ALLOWED: dict[tuple[str, str, str], str] = {
    ("scripts/hooks/git_push_guard.py", "heredoc", "bypass"): (
        "Design rationale for a REJECTED alternative: it records that a "
        "two-token match could be split by anything inserted between the "
        "tokens, which is why the shipped detector matches a single keyword. "
        "The shapes named defeat a design that was never merged, and naming "
        "them is what justifies the rejection. Re-examine if that detector is "
        "ever changed back."
    ),
}


def _tracked(suffixes: tuple[str, ...]) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(_REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return [
        _REPO / rel
        for rel in out.stdout.split("\0")
        if rel and rel.endswith(suffixes) and (_REPO / rel).is_file()
    ]


def _comment_runs(src: str) -> list[str]:
    """Real COMMENT tokens, grouped into runs of consecutive lines.

    Grouped because the window that binds the two halves of a recipe together
    has to span a multi-line comment block; judged per line, a construct named
    on one line and the defeat claimed on the next would read as unrelated.
    """
    runs: list[list[str]] = []
    prev = None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            line = tok.start[0]
            if runs and prev is not None and line == prev + 1:
                runs[-1].append(tok.string)
            else:
                runs.append([tok.string])
            prev = line
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        # Will not tokenize — fall back to the coarse scan, which also reads
        # string literals. Over-reporting is the correct direction here; see
        # the module docstring.
        return [m.group(0) for m in re.finditer(r"(?m)^[ \t]*#.*(?:\n[ \t]*#.*)*", src)]
    return ["\n".join(r) for r in runs]


def _prose_of_python(path: Path) -> list[str]:
    """Comments and docstrings only — never string literals or test data."""
    try:
        src = path.read_text(errors="replace")
    except OSError:
        return []
    blocks = _comment_runs(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return blocks
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                blocks.append(doc)
    return blocks


def _recipe_hits(text: str) -> list[tuple[str, str]]:
    low = text.lower()
    hits = []
    for construct in _CONSTRUCTS:
        for m in re.finditer(re.escape(construct.lower()), low):
            window = low[max(0, m.start() - _WINDOW) : m.end() + _WINDOW]
            for outcome in _OUTCOMES:
                at = window.find(outcome)
                if at < 0:
                    continue
                lead = window[max(0, at - _NEGATION_LOOKBACK) : at]
                for phrase in _CONCESSIVE:
                    lead = lead.replace(phrase, " ")
                if any(neg in lead for neg in _NEGATORS):
                    continue  # denied, not asserted — see _NEGATORS
                hits.append((construct, outcome))
    return hits


def _offenders(paths: list[Path], prose_fn) -> list[str]:
    found = []
    for path in paths:
        rel = str(path.relative_to(_REPO))
        for block in prose_fn(path):
            for construct, outcome in _recipe_hits(block):
                if (rel, construct, outcome) in _ALLOWED:
                    continue
                found.append(f"{rel}: {construct!r} near {outcome!r}")
    return found


def test_python_prose_carries_no_bypass_recipe():
    """Comments and docstrings across every tracked .py file."""
    offenders = _offenders(_tracked((".py",)), _prose_of_python)
    assert not offenders, (
        "prose names a shell construct and, within the same claim, says a gate "
        "was defeated by it. That is a recipe, and this repository is public. "
        "Describe the MECHANISM instead ('a normalizer's model of the shell was "
        "narrower than the shell's'), or move the detail to a private note.\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_markdown_carries_no_bypass_recipe():
    """Every tracked .md file — skills, docs, changelogs, READMEs."""
    offenders = _offenders(_tracked((".md",)), lambda p: [p.read_text(errors="replace")])
    assert not offenders, (
        "documentation names a shell construct and says a gate was defeated by "
        "it. Same rule as the code prose above, and the surface a reader "
        "actually browses.\n  " + "\n  ".join(sorted(set(offenders)))
    )


@pytest.mark.parametrize(
    "prose,should_flag",
    [
        # The recipe: construct + defeat, together.
        ("stripping the here" + "-doc body made the guard fall silent", True),
        ("an ANSI-C span is enough to bypass the check", True),
        # Legitimate prose that must NOT trip it.
        ("the here" + "-doc body is treated as data by the shell", False),
        ("this bypass of the cache is intentional", False),
        ("apostrophes in a commit message are common", False),
        # NEGATED claims — the reassurance, not the recipe. All seven hits on
        # this file's first run over the repo were of this shape.
        ("a `|` inside a here" + "-doc body may over-block, never a bypass", False),
        ("a here" + "-doc cannot open a bypass here", False),
        ("an ANSI-C span does not bypass the gate", False),
        # CONCESSIVE — carries a negator but ASSERTS. "not only X" claims X,
        # and the negation list, being itself a bag of substrings, read it as a
        # denial and swallowed a real recipe.
        ("an ANSI-C span is not only able to bypass the gate", True),
        ("a here" + "-doc does not just bypass it, it hides the command", True),
    ],
)
def test_the_detector_discriminates(prose, should_flag):
    """CONTROL — without this, an inert predicate reports a clean repo forever.

    A scan that never fires and a codebase with nothing to find return the same
    empty list. The negative cases matter as much as the positive ones: a
    detector that flags every mention of a construct would be turned off within
    a week, and a disabled gate is a doc line with extra steps.
    """
    assert bool(_recipe_hits(prose)) is should_flag


def test_a_hash_line_inside_a_string_literal_is_not_read_as_a_comment(tmp_path):
    """The permitted-fixture half of the rule, enforced rather than asserted.

    The module docstring has always said string literals are not scanned. The
    first implementation read raw source with a regex, which cannot tell a
    comment from a hash-prefixed line inside a multiline string — so a shell
    fixture, the one thing the rule explicitly allows, tripped the check. The
    positive control below is what makes this test mean anything: without it,
    a `_prose_of_python` that returned nothing at all would pass.
    """
    shape = "ANSI" + "-C"
    recipe = f"# a {shape} span here makes the guard fall silent\n"

    literal = tmp_path / "fixture_data.py"
    literal.write_text(f"SCRIPT = '''\n{recipe}echo hi\n'''\n")
    assert _prose_of_python(literal) == [], (
        "a hash-prefixed line inside a string literal was read as prose; "
        "fixture data is permitted and must not be scanned"
    )

    control = tmp_path / "real_comment.py"
    control.write_text(recipe)
    hits = [h for block in _prose_of_python(control) for h in _recipe_hits(block)]
    assert hits, (
        "POSITIVE CONTROL FAILED — the same text as a real comment was not "
        "caught either, so the assertion above proves nothing"
    )


def test_an_untokenizable_file_falls_back_to_the_coarse_scan(tmp_path):
    """The documented degradation, locked so it stays loud rather than silent.

    A file that will not tokenize gets the regex, which over-reports. That is
    the deliberate direction — a false positive is a CI failure someone reads,
    a false negative is a recipe that ships. Pinned so the fallback cannot be
    quietly changed to "return nothing", which would look identical on a clean
    repository.
    """
    shape = "ANSI" + "-C"
    broken = tmp_path / "unparseable.py"
    broken.write_text(f"def f(:\n# a {shape} span here makes the guard fall silent\n")
    blocks = _prose_of_python(broken)
    assert blocks, "an untokenizable file yielded no prose — the fallback is gone"
    assert [h for b in blocks for h in _recipe_hits(b)]
