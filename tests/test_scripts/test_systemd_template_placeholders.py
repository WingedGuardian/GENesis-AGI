"""Every placeholder in a unit template is substituted by every renderer.

WHY THIS EXISTS. Unit templates carry `__TOKEN__` placeholders, and TWO separate
scripts render them: `scripts/install.sh` and `scripts/bootstrap.sh`. Each keeps
its own hand-written list of `sed` expressions. Nothing connected the two lists
to each other, or either list to the tokens the templates actually use, so a
template could introduce a token that a renderer had never heard of — and `sed`
passes an unknown token through verbatim rather than failing.

The result is a unit file that installs, enables and reports success with a
literal `__TOKEN__` where a path belongs. Three instances, all real:

* `agent-zero.service` shipped with a literal `__AZ_ROOT__` (recorded in
  `scripts/install.sh`'s own comment, since fixed THERE).
* `genesis-falkordb.service` in PR #1834 introduced `__REDIS_SERVER__` and
  `__FALKORDB_VERSION__`, which neither renderer substitutes. Caught only by the
  `fresh-install` CI job.
* `agent-zero.service` again, this time from `bootstrap.sh`, which never gained
  the `__AZ_ROOT__` expression `install.sh` did. MEASURED on a live install:
  `WorkingDirectory=__AZ_ROOT__` on line 10 while `ExecStart` on line 11 carried
  a correctly substituted venv path — the divergence, visible in one file.

The CI job that caught the second one runs `install.sh` ONLY, so the
`bootstrap.sh` render path had no placeholder coverage of any kind. That is why
this is a test over the SOURCE rather than a third assertion inside one job:
it covers every renderer without needing every renderer to be executed.

POLARITY IS ALLOWLIST. The question asked is "is every token used by a template
present in this renderer's substitution set", not "does any token look
suspicious". A denylist of known-bad tokens would pass by construction for the
next token nobody thought of, which is the only kind that has ever shipped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO / "scripts" / "systemd"

#: Renderers that expand unit templates, and the glob each one renders. Both
#: render EVERY template in the directory, so both must know every token.
RENDERERS = ("install.sh", "bootstrap.sh")

#: The tokens known to be shared by every renderer. This is a FLOOR used to
#: prove the extraction below actually worked — not the expected answer.
#:
#: Without it this whole module degrades silently: if `_substituted_tokens`
#: stopped matching (a reformat, a switch from `sed -e` to something else), it
#: would return an EMPTY set, every "is the template's token in this set"
#: assertion would be vacuous on an empty template list, and the suite would go
#: green while checking nothing. A derived set that can legitimately be a subset
#: of the truth needs an independent denominator; this is that denominator.
_MINIMUM_SHARED_TOKENS = frozenset({"__HOME__", "__VENV__", "__REPO_DIR__", "__CC_BIN_DIR__"})

#: Leading `[A-Z0-9]` rather than `[A-Z]`: a token is not required to start
#: with a letter, and matching only letter-initial ones would silently exempt
#: `__2FA_DIR__` from every assertion below.
_TOKEN = re.compile(r"__[A-Z0-9][A-Z0-9_]*__")
#: `-e "s|__TOKEN__|...|g"` — the only form either renderer uses today.
#:
#: LIMIT, stated because an earlier version of this comment claimed the
#: opposite and was wrong: anchoring on `s|` does NOT by itself exclude a token
#: named inside a COMMENT. A comment containing `s|__FOO__|bar|g` would have
#: registered `__FOO__` as substituted and granted a false clean — so
#: `_substituted_tokens` strips full-line comments BEFORE matching, and that
#: strip is what provides the property, not this pattern.
#:
#: Still not complete: a trailing comment on a line that also carries real code
#: is not removed (these are shell scripts, not a tokenized language). That is
#: a narrower hole than the one it replaces, and it fails toward a FALSE CLEAN,
#: so it is written down rather than left implied.
_SED_EXPR = re.compile(r"s\|(__[A-Z0-9][A-Z0-9_]*__)\|")
#: A full-line shell comment: optional whitespace, then `#`.
_COMMENT_LINE = re.compile(r"^\s*#")


def _templates() -> list[Path]:
    return sorted(TEMPLATE_DIR.glob("*.template"))


def _tokens_used(path: Path) -> set[str]:
    return set(_TOKEN.findall(path.read_text(encoding="utf-8")))


def _substituted_tokens(renderer: str) -> set[str]:
    """Tokens `renderer` actually expands, comments excluded.

    The comment strip is load-bearing: a renderer's prose about a token it does
    NOT expand must never count as expanding it, or this whole module grants a
    clean bill of health off documentation.
    """
    source = (REPO / "scripts" / renderer).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not _COMMENT_LINE.match(line)
    )
    return set(_SED_EXPR.findall(code))


def test_the_template_directory_is_not_empty():
    """The denominator. Every assertion below iterates templates, and a loop
    over zero templates passes having checked nothing."""
    found = _templates()
    assert len(found) >= 10, (
        f"only {len(found)} unit template(s) under {TEMPLATE_DIR} — the glob is "
        "probably wrong, and every placeholder assertion in this module is "
        "vacuous until it is fixed"
    )


@pytest.mark.parametrize("renderer", RENDERERS)
def test_the_extraction_actually_found_a_renderers_substitutions(renderer):
    """Proves `_substituted_tokens` works before anything trusts its output.

    An empty or near-empty result here means the regex stopped matching, not
    that a renderer stopped substituting — and every parity assertion would
    then pass for the wrong reason.
    """
    found = _substituted_tokens(renderer)
    missing = _MINIMUM_SHARED_TOKENS - found
    assert not missing, (
        f"scripts/{renderer}: expected at least {sorted(_MINIMUM_SHARED_TOKENS)} "
        f"to be substituted, but the extraction found {sorted(found)}. Either the "
        "renderer changed shape and this test's parser needs updating, or the "
        "renderer genuinely stopped substituting these."
    )


@pytest.mark.parametrize("renderer", RENDERERS)
def test_every_template_token_is_substituted_by_every_renderer(renderer):
    """The actual guard. A token no renderer expands ships a literal
    `__TOKEN__` into a live unit file, which installs and enables cleanly."""
    known = _substituted_tokens(renderer)
    offenders: list[str] = []
    for template in _templates():
        for token in sorted(_tokens_used(template) - known):
            offenders.append(f"{template.name}: {token}")

    assert not offenders, (
        f"scripts/{renderer} does not substitute these template placeholder(s), "
        "so it renders them verbatim into the installed unit:\n  "
        + "\n  ".join(offenders)
        + f'\n\nAdd a matching `-e "s|__TOKEN__|<value>|g"` to scripts/{renderer}, '
        "or stop using the token in the template."
    )


def test_the_two_renderers_agree_on_their_substitution_sets():
    """Divergence is the root cause, not the individual missing token.

    Both scripts render the same directory, so a token one expands and the
    other does not means the SAME template produces a working unit or a broken
    one depending on which entrypoint the operator happened to run. That is how
    `__AZ_ROOT__` reached a live install: `install.sh` gained the expression and
    `bootstrap.sh` never did.
    """
    sets = {r: _substituted_tokens(r) for r in RENDERERS}
    a, b = RENDERERS
    only_a = sets[a] - sets[b]
    only_b = sets[b] - sets[a]
    assert not (only_a or only_b), (
        "the two renderers substitute different token sets, so a template "
        "renders correctly under one entrypoint and broken under the other:\n"
        f"  only in {a}: {sorted(only_a)}\n"
        f"  only in {b}: {sorted(only_b)}"
    )


def test_the_graph_projection_units_use_only_shared_tokens():
    """This PR's own units, pinned deliberately.

    The unit this PR adds is the reason the audit above happened, so it gets an
    assertion of its own rather than relying on the sweep: it must use only
    tokens every renderer already knows, which is what keeps it out of the
    class entirely instead of solving the class for one more member.
    """
    units = [p for p in _templates() if p.name.startswith("genesis-graph-project.")]
    assert len(units) == 2, (
        f"expected the .service and .timer templates, found {[p.name for p in units]}"
    )
    for unit in units:
        extra = _tokens_used(unit) - _MINIMUM_SHARED_TOKENS
        assert not extra, (
            f"{unit.name} introduces placeholder(s) {sorted(extra)} beyond the set "
            "every renderer shares. Resolve the value at RUNTIME in "
            "scripts/graph_project_runner.sh instead — an install-specific path "
            "does not belong in a render-time token."
        )


def test_a_token_named_only_in_a_comment_does_not_count_as_substituted(tmp_path, monkeypatch):
    """LOCKS the comment strip, which is the difference between this module
    checking behaviour and checking documentation.

    Without it, a renderer whose PROSE mentions `s|__FOO__|bar|g` — which this
    very repo's comments do, when explaining how to add an expression — would
    register __FOO__ as handled and grant a clean pass for a token it never
    expands.
    """
    fake = tmp_path / "scripts"
    fake.mkdir()
    (fake / "faker.sh").write_text(
        '# To add one, write:  -e "s|__ONLY_IN_A_COMMENT__|value|g"\n'
        '  #  -e "s|__ALSO_COMMENTED__|value|g"\n'
        'sed -e "s|__ACTUALLY_SUBSTITUTED__|$x|g" "$t"\n',
        encoding="utf-8",
    )
    # This module, reached through sys.modules rather than imported by name —
    # a self-import would be circular, and patching the global directly would
    # not survive the function's own module-level lookup.
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)

    found = _substituted_tokens("faker.sh")
    assert found == {"__ACTUALLY_SUBSTITUTED__"}, (
        f"expected only the real expression, got {sorted(found)} — a token named "
        "in a comment is being counted as substituted"
    )


# -- Persistent= timers must be cleaned up by uninstall ------------------------


def _persistent_timers() -> list[Path]:
    return [
        p
        for p in TEMPLATE_DIR.glob("*.timer.template")
        if "Persistent=true" in p.read_text(encoding="utf-8")
    ]


def test_every_clean_site_uses_the_shared_timer_inventory():
    """PER SITE, not a whole-file count — that distinction is the bug.

    `uninstall.sh` has TWO cleanup paths: direct-container and host-driven.
    Each used to carry its own hardcoded `clean --what=state` list, and they
    drifted: a timer was added to one and not the other, which the file's own
    comment had already named as an asymmetry once before it happened again.

    An earlier version of this test counted timer names across the WHOLE file
    and so passed while one path was entirely uncovered — it proved a name
    appeared *somewhere*, which is not the property that matters. A reviewer
    caught exactly that. The fix is a single shared inventory variable; this
    asserts every site uses it, so a new timer added to the inventory reaches
    both paths and no site can hardcode its way back out.
    """
    uninstall = (REPO / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    # Comments stripped first. Without this the scan matches PROSE mentioning
    # the command — including this repo's own explanatory comments and the one
    # in uninstall.sh that describes this very rule — and reports them as
    # hardcoded sites. The sibling `_substituted_tokens` above had the
    # identical defect; the lesson did not transfer on its own.
    code = "\n".join(
        line for line in uninstall.splitlines() if not _COMMENT_LINE.match(line)
    )
    sites = re.findall(r"clean --what=state([^\n]*)", code)
    # Denominator: both cleanup paths must be present, or this is vacuous.
    assert len(sites) >= 2, (
        f"expected at least 2 `clean --what=state` sites (direct-container and "
        f"host-driven), found {len(sites)} — the scan is probably broken"
    )
    hardcoded = [site.strip() for site in sites if "$GENESIS_PERSISTENT_TIMERS" not in site]
    assert not hardcoded, (
        "every `clean --what=state` site must use $GENESIS_PERSISTENT_TIMERS so "
        "the two uninstall paths cannot drift; these hardcode their own list:\n  "
        + "\n  ".join(hardcoded)
    )


def test_the_shared_inventory_lists_every_persistent_timer():
    """The inventory is only worth using if it is COMPLETE.

    Derived from the templates rather than copied, so adding a
    `Persistent=true` timer without adding it here fails.
    """
    uninstall = (REPO / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    match = re.search(
        r'GENESIS_PERSISTENT_TIMERS="(.*?)"', uninstall, re.S
    )
    assert match, "GENESIS_PERSISTENT_TIMERS is not defined in scripts/uninstall.sh"
    listed = set(match.group(1).split())

    expected = {p.name.removesuffix(".template") for p in _persistent_timers()}
    missing = expected - listed
    assert not missing, (
        "these Persistent=true timers are not in GENESIS_PERSISTENT_TIMERS, so "
        f"their schedule stamps survive an uninstall: {sorted(missing)}"
    )


# NOT CHECKED HERE, deliberately: whether each persistent timer is also
# DISABLED. An earlier draft asserted "the name appears at least twice in
# uninstall.sh" as a proxy for disable-plus-clean. It was wrong twice over — the
# shared inventory collapses each name to ONE occurrence, so the count stopped
# meaning anything the moment the duplication it measured was removed; and
# `genesis-cc-align.timer` is host-only by contract with no container leg, so it
# is CORRECTLY absent from container-side disable lists and the rule would have
# cried wolf on a file that was right.
#
# A real check would have to parse the disable invocations, which are not
# uniform (a `for` loop, inline `systemctl --user disable`, and a
# `container_exec` heredoc), and would need to know which units are host-only.
# That is worth doing; it is not worth faking with a count. Left as a stated
# gap rather than a green test that proves nothing.


def test_there_is_at_least_one_persistent_timer_to_check():
    """Denominator for the test above: an empty list would pass it silently."""
    assert len(_persistent_timers()) >= 2, (
        f"found {len(_persistent_timers())} Persistent=true timer template(s) — "
        "the detection is probably broken, making the uninstall check vacuous"
    )
