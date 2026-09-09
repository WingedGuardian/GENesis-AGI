"""Every model-facing hook is bounded, or is exempt for a stated reason.

THE CLASS THIS LOCKS. Claude Code FILES a hook's stdout above a per-hook-entry
size cap (10,000 characters on 2.1.246 — version-volatile) and shows the model a
~2 KB preview. Nothing errors and the exit code is unchanged, so a hook that
silently contributed nothing is indistinguishable from one that worked.

MEASURED, by enumerating every filing the harness has made on this install
(849/849 by content, not a sample): 842 were one emitter, fixed by #1556 when it
moved that output behind ``scripts/hooks/hook_output.py``; 7 were a guard removed
in #1106; and ZERO came from the eleven hooks (nine Python, two shell) wired
today. So this test is not chasing a live incident — #1556 already closed the one
that existed. It exists because nothing stops the NEXT hook being wired with
unbounded model-facing output, and CLAUDE.md's instruction to "route any new
model-facing stdout through it" is prose with no mechanism behind it.

WHAT THIS GATE CANNOT SEE, stated so nobody reads it as total coverage. It
enumerates ``.claude/settings.json`` only — the repo's wiring. A user-level
``~/.claude/settings.json`` (which on this install wires further SessionStart
entries) and ``.claude/settings.local.json`` are real CC layers outside the
repo's control, and a hook wired there is invisible here. The detector also
matches ``print`` and ``sys.stdout.write``/``writelines``; it does NOT catch
``os.write(1, …)``, a print aliased to another name, or a subprocess that
inherits stdout — nor output produced by a helper module the hook imports, since
only the hook's own source is parsed.

AND AN EXEMPTION SKIPS SCANNING ENTIRELY — measured by adding an unbounded
``sys.stdout.write`` to an exempted hook and watching this suite stay green. That
is the design, not an oversight: the gate's job is to force the claim to be MADE
and STATED next to the code, not to re-verify it forever. It is also the
weakness to know about, because it is how an exemption rots. Two consequences
follow. A row here is only as good as its last read, so an exemption is a review
surface, not a settled fact. And the table stays SMALL on purpose — every row is
a place the gate stops looking, which is why routing a hook through the writer
is always preferred over adding it here.

SCOPE. Only ``SessionStart`` and ``UserPromptSubmit`` put a hook's BARE STDOUT in
front of the model. Every other event reaches it through JSON
``additionalContext``/``systemMessage``, which runs through the same persistence
path but has a different failure mode — an oversized advisory must lose prose,
never its ``permissionDecision``, which is what ``print_json_bounded`` protects.
Those events are deliberately out of scope here rather than exempted in bulk: a
36-entry exemption list on day one would be a rubber stamp.

POLARITY IS ALLOWLIST, ON PURPOSE. A wired hook that is neither routed nor
explicitly listed FAILS. Its sibling ``test_hook_input_contract.py`` is a
denylist — it catches one known-bad spelling — and a denylist cannot notice a
hook nobody thought about. That difference is the whole point of this file.

Install-agnostic: synthetic payloads, no network, no live DB.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = _REPO / ".claude" / "settings.json"

#: The events whose hooks write bare stdout the model reads. Everything else
#: reaches the model only through the JSON channel — see SCOPE above.
#:
#: `UserPromptExpansion` was MISSING, and the omission contradicted this repo's
#: own documented contract: `scripts/hooks/hook_output.py` states "Only
#: SessionStart, UserPromptSubmit and UserPromptExpansion put a hook's bare
#: stdout in front of the model". The gate enumerated two of the three, so a hook
#: wired to the third would have passed an allowlist whose entire guarantee is
#: that a newly wired model-facing hook fails by construction. The list is
#: therefore taken FROM that docstring rather than restated from memory.
_BARE_STDOUT_EVENTS = ("SessionStart", "UserPromptSubmit", "UserPromptExpansion")

#: Hooks whose model-facing output CANNOT reach the cap by construction, so how
#: they print does not matter.
#:
#: THE RULE, and it is the one this table got wrong first: an exemption may only
#: cite a bound that CONFIGURATION CANNOT CHANGE. A default in a config module is
#: not a bound — `pr_watch_config.knob_int` has no upper clamp and
#: `load_config` merges a `.local.yaml` overlay, so `max_surface: 5000` in either
#: file would have produced ~410 KB of output through two rows of this table that
#: read as verified. Adversarial review caught it. Cite a hardcoded slice or an
#: in-code clamp, never a DEFAULTS entry.
_STRUCTURALLY_BOUNDED = {
    "scripts/surface_pr_updates.py": (
        "min(..., pr_watch_config.MAX_SURFACE_CAP) clamp in main() of "
        "scripts/surface_pr_updates.py — in CODE, so a config overlay cannot "
        "raise it; clause bodies clipped [:71] by render_clause; joined into ONE "
        "line at pr_watch.select_to_surface. Cites SYMBOLS, not line numbers: "
        "this row first cited :54, a reflow moved it to :56, and nothing caught "
        "the rot"
    ),
    "scripts/surface_open_prs.py": (
        "min(..., repo_pulse_config.OPEN_PR_MAX_SURFACE_CAP) clamp in main() of "
        "scripts/surface_open_prs.py — in CODE, not the config default; each "
        "clause synthesised from ints (#1379 (12d, draft)); joined into ONE line "
        "at pr_watch.select_to_surface"
    ),
    ".claude/hooks/cbm-session-reminder.sh": (
        "621 bytes TOTAL, a single quoted heredoc (cat << 'REMINDER') with zero "
        "'$' anywhere in the file — the file size is its hard output ceiling"
    ),
    "scripts/hooks/skill_injection_hook.py": (
        "_MAX_CATALOG_NUDGES = 2 catalog nudges, each held to "
        "_MAX_NUDGE_LINE = 400 chars by a degradation ladder (not by a slice at "
        "the print), plus at most 2 literal process nudges from "
        "_check_process_discipline, which interpolate nothing. Worst case is "
        "asserted against HOOK_STDOUT_CAP by the test below rather than argued "
        "here -- an earlier version of that test checked only that the constants "
        "were truthy, and an audit raised the per-line ceiling 125x with the "
        "suite still green. The ceiling is on the rendered LINE, never on the "
        "identifiers in it: name and path come from skill frontmatter that "
        "generate_skill_catalog.py accepts unsliced, and slicing THEM bounded "
        "the output by emitting a /skill argument and a Read path that pointed "
        "nowhere. Identifiers are emitted whole or the line degrades to a form "
        "carrying none"
    ),
    "scripts/contribution_offer_hook.py": (
        "one fixed f-string; sha[:12] and subject[:200] (:63) are its only "
        "inputs, both sliced in code"
    ),
    "scripts/hooks/session_activity_touch.sh": (
        "scripts/hooks/session_activity_touch.sh writes 0 bytes to fd 1 — it "
        "touches a marker file and exits; its 2 `cat` calls are command "
        "substitutions into variables, not stdout writes"
    ),
}

#: NOT structurally bounded. Listed only because it has never been observed
#: filing, with its routing tracked elsewhere. This category exists so that debt
#: is VISIBLE rather than laundered into the table above, and it is meant to
#: drain to empty. Every entry must name where its routing is tracked.
_MEASURED_PENDING_ROUTING = {
    "scripts/proactive_memory_hook.py": (
        "7 model-facing print sites; get_active_sync (db/crud/session_heartbeats.py"
        ":165-177) has no LIMIT, so the peer loop is unbounded. NEVER observed "
        "filing (0 of 849 harness filings). Routing is tracked as its own PR "
        "because this runs on every prompt in every session and its own comment "
        "notes a broken read 'reads exactly like no concurrent sessions'."
    ),
}

#: hook_output.py is the writer itself; it legitimately calls bare print() to
#: emit what every other hook hands it. Same shape as test_hook_input_contract's
#: _ALLOWED_ENV_READERS = {"hook_input.py"}.
_SELF_EXEMPT = {"scripts/hooks/hook_output.py"}

_HOOK_LAUNCHER = "genesis-hook"


def _rel(path: Path) -> str:
    """Repo-relative key. Basenames were the first design and were too loose: a
    future hook merely SHARING a name would inherit an exemption written for a
    different file."""
    try:
        return path.resolve().relative_to(_REPO).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve(command: str) -> tuple[str, Path | None]:
    """Return (display name, resolved path) for one wired hook command.

    Two shapes exist in settings.json today and both are handled explicitly
    rather than by a general parser: `<launcher> <script.py> [args]`, where the
    script is relative to scripts/, and `bash <path>`, where the path is
    absolute under ${CLAUDE_PROJECT_DIR}. A command matching neither returns
    (command, None) so the caller FAILS on it instead of skipping it — an
    unparseable hook is exactly the case an allowlist must not wave through.
    """
    tokens = command.split()
    for i, tok in enumerate(tokens):
        if tok.endswith(_HOOK_LAUNCHER) and i + 1 < len(tokens):
            rel = tokens[i + 1]
            path = _REPO / "scripts" / rel
            return _rel(path), path
    for tok in tokens:
        if tok.endswith((".sh", ".py")):
            path = tok.replace("${CLAUDE_PROJECT_DIR}", str(_REPO))
            return _rel(Path(path)), Path(path)
    return command, None


def _wired() -> list[tuple[str, str, Path | None]]:
    """(event, name, path) for every hook wired to a bare-stdout event."""
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    out: list[tuple[str, str, Path | None]] = []
    for event in _BARE_STDOUT_EVENTS:
        for entry in settings["hooks"].get(event, []):
            for hook in entry.get("hooks", []):
                name, path = _resolve(hook["command"])
                out.append((event, name, path))
    return out


#: An inline, per-line opt-out. Requires a reason after the colon, so a waiver is
#: a visible statement in the source rather than an entry in a table nobody reads
#: next to the code it excuses.
_EXEMPT_MARKER = "hook-output-exempt:"


def unbounded_stdout_offenders(src: str) -> list[str]:
    """Calls that put unbounded text in front of the model.

    A standalone function over a SOURCE STRING rather than a path, so the
    positive control below can feed it synthetic sources. Without that, "no
    offenders" and "no detector" are the same result.

    TWO SHAPES, because covering only the first left a hole big enough to drive
    the whole gate through: an adversarial review demonstrated that reverting a
    routed hook to ``sys.stdout.write(REMINDER)`` passed every test in this PR.

    1. ``print(...)`` with no `file=` — and `file=None` counts as no file=, since
       that is documented Python for "use sys.stdout". Reading `file=` as present
       regardless of its value was a straight correctness bug in the first
       version of this detector.
    2. ``sys.stdout.write`` / ``sys.stdout.writelines`` — the spelling that
       bypasses `print` entirely. Matched on the ``.stdout`` attribute rather
       than the ``sys`` binding, so ``from sys import stdout`` style aliases are
       NOT caught; see the known-misses note in the gate's docstring.

    ``print(x, **kw)`` is flagged: a `**kwargs` entry has ``arg is None``, so it
    never satisfies the file= test. That direction fails CLOSED, which is right.
    """

    def _waived_lines() -> set[int]:
        """Lines carrying a REAL, REASONED exemption comment.

        Two independent bypasses, both MEASURED before this was rewritten:
          * `# hook-output-exempt:` with nothing after the colon was accepted,
            despite the stated contract that every waiver carries a reason. A
            waiver whose whole purpose is to record WHY cannot be satisfied by
            an empty string.
          * The marker inside a STRING LITERAL on the output call silenced it —
            `sys.stdout.write("# hook-output-exempt: fake")` exempted itself.
            A substring test over raw source cannot tell a comment from data.

        So the source is TOKENIZED and only `tokenize.COMMENT` tokens count —
        the shape `scripts/check_frozen_clock.py` already uses, and the
        best-engineered waiver in this repo. An untokenizable file yields NO
        waivers, which fails toward scanning rather than toward exemption.
        """
        waived: set[int] = set()
        try:
            import io
            import tokenize

            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type != tokenize.COMMENT or _EXEMPT_MARKER not in tok.string:
                    continue
                reason = tok.string.split(_EXEMPT_MARKER, 1)[1].strip()
                if reason:  # an empty reason is not a waiver
                    waived.add(tok.start[0])
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return set()  # unparseable -> no waivers -> everything is scanned
        return waived

    _WAIVED = _waived_lines()

    def _has_marker(lineno: int) -> bool:
        return lineno in _WAIVED

    def _is_stdout_stream(node: ast.AST) -> bool:
        """`sys.stdout`, `sys.__stdout__`, or the `.buffer` behind either.

        `sys.stdout.buffer.write(b"x")` reaches the model exactly as
        `sys.stdout.write` does -- it is the same stream, one attribute deeper.
        """
        if isinstance(node, ast.Attribute) and node.attr == "buffer":
            return _is_stdout_stream(node.value)
        return (
            isinstance(node, ast.Attribute)
            and node.attr in ("stdout", "__stdout__")
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        )

    def _names_stdout(node: ast.AST) -> bool:
        """Does this expression name the model-facing stream?

        AN ENUMERATION, MAINTAINED BY BEING WRONG FOUR TIMES, and the count is
        the point. A bare `print()` was the original detector; `file=None` was
        added after the first adversarial pass; `file=sys.stdout` after the
        second; `builtins.print` and `sys.stdout.buffer.write` after a
        cross-model pass. Each round patched the spelling that was named.

        SO THE CLAIM IS NOW BOUNDED RATHER THAN TOTAL. An earlier docstring said
        "every spelling that NAMES stdout is flagged" -- an assertion of
        completeness that the very next reviewer falsified, twice over. What is
        true is narrower and more useful: the spellings BELOW are flagged, and
        the module docstring lists what is known to be outside them. A detector
        that claims completeness stops anyone looking for round five.

        KNOWN OUTSIDE: an aliased handle (`out = sys.stdout`), a rebound name
        (`print = my_printer`), `os.write(1, ...)`, and subprocess stdout
        inherited by a child. Resolving the first two needs dataflow; flagging
        every opaque `file=` target would refuse legitimate stderr writes through
        a variable, which is a false BLOCK and the worse direction for a gate.
        """
        if isinstance(node, ast.Constant) and node.value is None:
            return True  # documented Python for "use sys.stdout"
        return _is_stdout_stream(node)

    def _real_file_kwarg(node: ast.Call) -> bool:
        """True when `file=` sends output somewhere OTHER than the model."""
        return any(
            kw.arg == "file" and not _names_stdout(kw.value)
            for kw in node.keywords
        )

    offenders: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or _has_marker(node.lineno):
            continue
        func = node.func
        is_print = (
            (isinstance(func, ast.Name) and func.id == "print")
            # `builtins.print(...)` is the SAME builtin, qualified. Round four.
            or (isinstance(func, ast.Attribute) and func.attr == "print"
                and isinstance(func.value, ast.Name) and func.value.id == "builtins")
        )
        if is_print and not _real_file_kwarg(node):
            offenders.append(f"line {node.lineno}: print() reaches the model")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in {"write", "writelines"}
            and _is_stdout_stream(func.value)
        ):
            offenders.append(f"line {node.lineno}: stdout.{func.attr}() reaches the model")
    return offenders


def test_every_model_facing_hook_is_bounded_or_exempt() -> None:
    """The gate. A wired hook must route through the writer, or be listed."""
    listed = set(_STRUCTURALLY_BOUNDED) | set(_MEASURED_PENDING_ROUTING) | _SELF_EXEMPT
    failures: list[str] = []

    for event, name, path in _wired():
        if name in listed:
            # An exemption is not a licence to vanish. The early `continue` used
            # to skip the existence check below, so a hook deleted or renamed
            # while its settings entry remained stayed "exempt" and CI accepted a
            # configuration that fails at runtime. The sibling test compares
            # command-derived NAMES only, so it stayed green too.
            if path is None:
                failures.append(
                    f"{event}: exempt entry {name!r} resolves to no script"
                )
            elif not path.exists():
                failures.append(
                    f"{event} {name}: EXEMPT but missing at {path} — an exemption "
                    "for a hook that no longer exists is a stale row, not a waiver"
                )
            continue
        if path is None:
            failures.append(f"{event}: cannot resolve a script from {name!r}")
            continue
        if not path.exists():
            failures.append(f"{event} {name}: wired but missing at {path}")
            continue
        if path.suffix != ".py":
            failures.append(
                f"{event} {name}: shell hook is neither exempt nor scannable — "
                f"add it to _STRUCTURALLY_BOUNDED with a measured reason"
            )
            continue
        offenders = unbounded_stdout_offenders(path.read_text(encoding="utf-8"))
        if offenders:
            # Deduped: a hook wired N times (genesis_session_context.py is wired
            # four times, once per --part) would otherwise report every offender
            # N times and bury the distinct ones.
            entry = f"{event} {name}: " + "; ".join(offenders)
            if entry not in failures:
                failures.append(entry)

    assert not failures, (
        "Model-facing hooks that neither route through hook_output.py nor carry "
        "an exemption:\n  "
        + "\n  ".join(failures)
        + "\n\nClaude Code FILES hook stdout above ~10,000 characters and shows the "
        "model a ~2 KB preview, silently and with exit code unchanged. Either emit "
        "through scripts/hooks/hook_output.py (BoundedStdout), or — if the output "
        "cannot reach the cap by construction — add the hook to "
        "_STRUCTURALLY_BOUNDED with the cap that proves it, quoting the file that "
        "defines that cap."
    )


def test_the_gate_can_itself_fail() -> None:
    """The detector must flag what it claims to, and must not flag what it does not.

    Copied in shape from test_context_injection_budget.py's budget-lock control.
    A gate whose detector silently matches nothing passes forever and reads
    exactly like a clean repo.
    """
    must_flag = {
        "plain": "print('hello')",
        "fstring": "print(f'[{tag}] {detail}')",
        "in a loop": "for x in items:\n    print(x)",
        "one bare print among several routed ones": (
            "import sys\nprint('a')\nprint('b', file=sys.stderr)"
        ),
        # Each of the four below passed the FIRST version of this detector. An
        # adversarial review found them by running it rather than reading it,
        # which is why they are pinned here as cases rather than described in
        # prose: a miss nobody replays comes back.
        "file=None is documented Python for sys.stdout": "print('x', file=None)",
        "sys.stdout.write bypasses print entirely": "import sys\nsys.stdout.write('x')",
        "writelines is the same hole": "import sys\nsys.stdout.writelines(['a', 'b'])",
        "**kwargs cannot prove a file= is present": "print('x', **kw)",
        # ROUND THREE of the same class, found by CodeRabbit on this PR. The
        # predicate asked "is there a file kwarg" instead of "where does it go",
        # so naming the model's own stream explicitly was the way past it. The
        # class is now ENUMERATED rather than patched again: every spelling that
        # NAMES stdout is flagged, and `sys.__stdout__` is pinned here before
        # someone finds it as round four.
        "file=sys.stdout names the model's own channel": (
            "import sys\nprint('x', file=sys.stdout)"
        ),
        "file=sys.__stdout__ is the same stream": (
            "import sys\nprint('x', file=sys.__stdout__)"
        ),
        # ROUND FOUR, from a cross-model pass. The first three rounds each
        # patched the spelling that was named; these two are why the docstring
        # no longer claims completeness.
        "builtins.print is the same builtin, qualified": (
            "import builtins\nbuiltins.print('x')"
        ),
        "sys.stdout.buffer is the same stream, one attribute deeper": (
            "import sys\nsys.stdout.buffer.write(b'x')"
        ),
    }
    for label, src in must_flag.items():
        assert unbounded_stdout_offenders(src), f"detector missed: {label}"

    must_not_flag = {
        "stderr": "import sys\nprint('x', file=sys.stderr)",
        "explicit stream": "print('x', file=stream)",
        "stderr.write is not the model's channel": "import sys\nsys.stderr.write('x')",
        # The other side of the enumeration: an OPAQUE handle stays unflagged.
        # Resolving it needs dataflow, and flagging every unknown file= target
        # would refuse legitimate stderr writes through a variable -- a false
        # BLOCK, which is the worse direction for a contract gate.
        "an aliased handle is not resolved, by design": (
            "out = open('f')\nprint('x', file=out)"
        ),
        "stderr.buffer is not the model's channel either": (
            "import sys\nsys.stderr.buffer.write(b'x')"
        ),
        "routed through the writer": (
            "from hook_output import BoundedStdout\n"
            "out = BoundedStdout(label='t')\n"
            "out.emit('x', block='t')"
        ),
        "an explicit per-line waiver with a reason": (
            "import sys\nsys.stdout.write('x')  # hook-output-exempt: it is the probe"
        ),
        "writing to a non-stdout object": "buf.write('x')",
        "no output at all": "x = 1\n",
    }
    for label, src in must_not_flag.items():
        assert not unbounded_stdout_offenders(src), f"detector false-fired on: {label}"


def test_exemptions_name_hooks_that_are_actually_wired() -> None:
    """An exemption for a hook nobody wires any more is dead weight that reads
    as coverage. Drain the list when a hook goes away."""
    wired_names = {name for _event, name, _path in _wired()}
    stale = sorted((set(_STRUCTURALLY_BOUNDED) | set(_MEASURED_PENDING_ROUTING)) - wired_names)
    assert not stale, (
        f"Exemptions naming hooks not wired to {_BARE_STDOUT_EVENTS}: {stale}. "
        "Remove them — a stale exemption still reads as a considered decision."
    )


def test_every_exemption_states_a_reason() -> None:
    """A reason is the only thing separating an exemption from a rubber stamp."""
    thin = sorted(
        name
        for name, reason in (_STRUCTURALLY_BOUNDED | _MEASURED_PENDING_ROUTING).items()
        if len(reason.strip()) < 40 or not re.search(r"[:\d]", reason)
    )
    assert not thin, (
        f"Exemptions without a substantive, specific reason: {thin}. State the cap "
        "and the file that defines it."
    )


def test_a_form_feed_does_not_desync_the_waiver_index() -> None:
    """`splitlines()` breaks on \x0c; CPython's tokenizer does not.

    One form feed above a waiver shifted every subsequent line index, so a
    legitimate `# hook-output-exempt:` marker was read off the wrong line and
    silently ignored. Fails CLOSED — a routed hook reads as an offender — which
    is the safe direction, but a gate that refuses a correct waiver is a gate
    people learn to route around.
    """
    waived = "import sys\nsys.stdout.write('x')  # hook-output-exempt: probe"
    assert unbounded_stdout_offenders(waived) == [], "baseline waiver not honoured"

    with_ff = "import sys\x0c\nsys.stdout.write('x')  # hook-output-exempt: probe"
    assert unbounded_stdout_offenders(with_ff) == [], (
        "a form feed above the waiver desynced the line index and dropped it"
    )


# ---------------------------------------------------------------------------
# CLASS A — enumeration coverage.
# ---------------------------------------------------------------------------

def test_every_bare_stdout_event_is_gated() -> None:
    """The gated set is taken FROM the writer's docstring, not from memory.

    `UserPromptExpansion` was missing, and the omission contradicted this repo's
    own documented contract: hook_output.py states that exactly three events put
    a hook's bare stdout in front of the model. Enumerating two of three means a
    hook wired to the third passes an allowlist whose whole guarantee is that a
    newly wired model-facing hook fails by construction.
    """
    doc = (_REPO / "scripts" / "hooks" / "hook_output.py").read_text(encoding="utf-8")
    for event in ("SessionStart", "UserPromptSubmit", "UserPromptExpansion"):
        assert event in doc, f"{event} is no longer named by the writer's docstring"
        assert event in _BARE_STDOUT_EVENTS, (
            f"{event} carries bare stdout per hook_output.py but is not gated"
        )


# ---------------------------------------------------------------------------
# CLASS B — exemption integrity. A waiver must be REAL and must SAY something.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "src,exempt",
    [
        ("import sys\nsys.stdout.write('x')  # hook-output-exempt: probe emits verbatim", True),
        # No reason: a waiver whose purpose is to record WHY cannot be satisfied
        # by an empty string.
        ("import sys\nsys.stdout.write('x')  # hook-output-exempt:", False),
        ("import sys\nsys.stdout.write('x')  # hook-output-exempt:   ", False),
        # In a STRING literal: a substring test over raw source cannot tell a
        # comment from data, and this silenced the call it appeared in.
        ("import sys\nsys.stdout.write('# hook-output-exempt: fake')", False),
        # A form feed desyncs splitlines() from the tokenizer; tokenizing removes
        # the line-index scheme that made that possible at all.
        ("import sys\x0c\nsys.stdout.write('x')  # hook-output-exempt: probe", True),
    ],
    ids=["reasoned", "empty", "whitespace-only", "in-a-string", "after-form-feed"],
)
def test_only_a_real_reasoned_comment_waives(src, exempt) -> None:
    assert (unbounded_stdout_offenders(src) == []) is exempt


def test_an_unparseable_hook_raises_rather_than_passing_silently() -> None:
    """`ast.parse` guards the waiver logic, so an unparseable hook never reaches
    the tokenizer at all -- it RAISES, and the gate fails loudly.

    Pinned because the alternative is the failure this whole gate exists to
    prevent: a hook the detector could not read scoring as clean. The tokenizer's
    own TokenError branch is therefore unreachable through this entry point and
    is belt-and-braces, which is worth saying rather than implying it is load
    bearing.
    """
    with pytest.raises(SyntaxError):
        unbounded_stdout_offenders("def f(:\n    pass  # hook-output-exempt: nope")


# ---------------------------------------------------------------------------
# CLASS C — an exemption may only cite a bound configuration cannot change.
# ---------------------------------------------------------------------------

def test_the_surfacing_caps_are_one_constant_shared_by_clamp_and_validator() -> None:
    """The hook CLAMPED to 20 while the validator accepted any positive int, so a
    config of 50 was accepted, reported back as 50, and silently ignored. A
    settings surface that lies about what it accepted is worse than one that
    refuses -- the operator has no way to notice."""
    import sys as _sys

    _sys.path.insert(0, str(_REPO / "src"))
    from genesis.mcp.health.settings import _validate_pr_watch, _validate_repo_pulse
    from genesis.session_awareness.pr_watch_config import MAX_SURFACE_CAP
    from genesis.session_awareness.repo_pulse_config import OPEN_PR_MAX_SURFACE_CAP

    # The hooks must clamp to the CONSTANT, not a literal that can drift from it.
    #
    # Checked on the AST, never as a substring of the file. The substring form
    # shipped first, and an adversarial audit demonstrated the near-miss: the
    # commit that fixed the clamp ALSO wrote the constant's name into the comment
    # above it, so reverting the code to a bare literal while keeping the comment
    # left this assertion green. A test that a comment can satisfy is measuring
    # prose. Verified RED against exactly that mutation before landing.
    for script, const_name, cap in (
        ("surface_pr_updates.py", "MAX_SURFACE_CAP", MAX_SURFACE_CAP),
        ("surface_open_prs.py", "OPEN_PR_MAX_SURFACE_CAP", OPEN_PR_MAX_SURFACE_CAP),
    ):
        tree = ast.parse((_REPO / "scripts" / script).read_text(encoding="utf-8"))
        clamps = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "min"
        ]
        assert clamps, f"{script} no longer clamps max_surface at all"
        assert [
            c
            for c in clamps
            if any(isinstance(a, ast.Attribute) and a.attr == const_name for a in c.args)
        ], f"{script} min() no longer names {const_name}"
        # The literal must be GONE, not merely accompanied: a surviving
        # `min(knob, 20)` beside the fixed one would re-open the drift.
        for c in clamps:
            for a in c.args:
                # `cap`, not MAX_SURFACE_CAP: comparing both scripts against
                # pr_watch's constant is right only while the two happen to be
                # equal, which is precisely the drift this test exists to catch.
                assert not (isinstance(a, ast.Constant) and a.value == cap), (
                    f"{script} still clamps to a bare literal beside the constant"
                )

    assert _validate_pr_watch({"max_surface": MAX_SURFACE_CAP + 1}), "over-cap accepted"
    assert not _validate_pr_watch({"max_surface": MAX_SURFACE_CAP}), "at-cap rejected"
    assert _validate_repo_pulse({"open_pr_max_surface": OPEN_PR_MAX_SURFACE_CAP + 1})
    assert not _validate_repo_pulse({"open_pr_max_surface": OPEN_PR_MAX_SURFACE_CAP})


def test_the_skill_exemption_bounds_the_line_not_the_identifiers() -> None:
    """The bound must sit on the rendered LINE, never on the identifiers in it.

    Two revisions of this row were wrong in opposite directions. The first cited
    only the description slices, so `name` and `path` -- user-authored frontmatter
    the catalog accepts unsliced -- could carry this exempt hook past the cap while
    the gate skipped scanning it. The second sliced them, which kept the cap claim
    true by emitting a `/skill` argument and a `Read <path>/SKILL.md` that pointed
    nowhere, and by saving a truncated name as nudge state that could never match
    the full name again, so the same skill re-nudged forever.

    The invariant that survives both: `_MAX_CATALOG_NUDGES * _MAX_NUDGE_LINE`
    bounds the output, and no identifier is ever emitted cut."""
    import ast as _ast

    src = (_REPO / "scripts" / "hooks" / "skill_injection_hook.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(src)
    consts = {
        t.id: n.value.value
        for n in _ast.walk(tree)
        if isinstance(n, _ast.Assign)
        and isinstance(n.value, _ast.Constant)
        and isinstance(n.value.value, int)
        for t in n.targets
        if isinstance(t, _ast.Name)
    }
    per_line = consts.get("_MAX_NUDGE_LINE")
    nudges = consts.get("_MAX_CATALOG_NUDGES")
    assert per_line, "the per-line ceiling is gone"
    assert nudges, "the nudge-count ceiling is gone"

    # THE ARITHMETIC, asserted rather than asserted-about. The first version of
    # this test checked only that the two constants were TRUTHY, and an audit
    # raised _MAX_NUDGE_LINE from 400 to 50_000 with the whole suite still green
    # -- a 100_000-char worst case against a 10_000 cap, on a hook the gate skips
    # scanning because it is exempt. Checking that a bound EXISTS is the same
    # mistake as checking that a comment mentions a constant.
    #
    # _PROCESS_NUDGE_ALLOWANCE covers _check_process_discipline's at-most-two
    # literal f-strings (MEASURED 802 chars, no interpolation, so they cannot
    # grow at runtime) plus headroom for the trailing newlines.
    # Read the cap from its single home rather than restating it, and by AST
    # rather than by import: this file already avoids mutating sys.path where it
    # can, and the constant is version-volatile by design.
    cap_src = (_REPO / "scripts" / "hooks" / "hook_output.py").read_text(
        encoding="utf-8"
    )
    hook_caps = {
        t.id: n.value.value
        for n in _ast.walk(_ast.parse(cap_src))
        if isinstance(n, _ast.Assign)
        and isinstance(n.value, _ast.Constant)
        and isinstance(n.value.value, int)
        for t in n.targets
        if isinstance(t, _ast.Name)
    }
    HOOK_STDOUT_CAP = hook_caps.get("HOOK_STDOUT_CAP")
    assert HOOK_STDOUT_CAP, "hook_output.py no longer defines HOOK_STDOUT_CAP"

    _PROCESS_NUDGE_ALLOWANCE = 1024
    worst_case = nudges * (per_line + 1) + _PROCESS_NUDGE_ALLOWANCE
    assert worst_case <= HOOK_STDOUT_CAP, (
        f"the skill hook's own ceiling ({nudges} x {per_line} + process nudges = "
        f"{worst_case}) exceeds HOOK_STDOUT_CAP ({HOOK_STDOUT_CAP}); its "
        f"exemption from this gate is no longer true"
    )
    # The exemption's PROSE quotes the ceiling, so it can rot away from the code
    # exactly like the line numbers two rows above did.
    assert str(per_line) in _STRUCTURALLY_BOUNDED[
        "scripts/hooks/skill_injection_hook.py"
    ], "the exemption row quotes a ceiling the code no longer uses"

    # No identifier may be sliced -- checked STRUCTURALLY. The textual form of
    # this assertion ('skill.get("name", "")[:80]' not in src) was defeated by
    # splitting the same slice across two statements: `name = skill.get(...)`
    # then `label = name[:80]`. A rename beats any substring test.
    emit = next(
        n
        for n in _ast.walk(tree)
        if isinstance(n, _ast.FunctionDef) and n.name == "main"
    )
    for node in _ast.walk(emit):
        if isinstance(node, _ast.Subscript) and isinstance(node.value, _ast.Name):
            assert node.value.id != "name", (
                "the skill name is sliced again; a cut name is a broken /skill "
                "argument and a nudge-state key that can never match"
            )
        if isinstance(node, _ast.Subscript):
            target = _ast.unparse(node.value)
            assert "path" not in target, (
                f"the skill path is sliced again ({target}); a cut path makes "
                f"the Read instruction point at nothing"
            )

    # State is keyed on the WHOLE name, so dedup cannot desync from the filter.
    saves = [
        n
        for n in _ast.walk(tree)
        if isinstance(n, _ast.Call)
        and isinstance(n.func, _ast.Name)
        and n.func.id == "_save_session_nudge"
    ]
    assert saves, "nothing records nudge state any more"
    # A literal (the two process nudges pass a fixed string) is fine; a SLICE is
    # the defect -- `name[:80]` here is what desynced state from the filter.
    for call in saves:
        for arg in call.args[1:]:
            assert not isinstance(arg, _ast.Subscript), (
                "nudge state must be keyed on the whole name; a sliced key can "
                "never match the unsliced name the candidate filter tests"
            )
