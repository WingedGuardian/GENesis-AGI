"""The Bash tool's timeout ceiling, pinned wherever we give advice about it.

The Bash tool takes a `timeout` in milliseconds with a **hard maximum of
600000ms (10 minutes)**. A larger value is not honoured — the call is still
SIGTERMed at ten minutes (MEASURED 2026-08-27: `timeout: 1600000` died at exactly
`10m 0s`). So "raise the timeout" is a fix that TOPS OUT.

Both places we ship advice about this told sessions to set `>=900000ms` — above
the ceiling, therefore unreachable. A session following our own guidance believed
it had fifteen minutes and was killed at ten, which is the same
confident-wrong-answer shape the sibling pipe guard exists for: the advice
"works", and the expectation it creates is false.

CORRECTED 2026-09-16 — this docstring used to end "past ten minutes the only
correct form is `run_in_background: true`". That is right for long work in
general (MEASURED: a 400s background task completed clean, exit 0 — there is no
120s ceiling) and WRONG for deploys, which are tied to the session's lifetime and
have been killed mid-run twice that way: 2026-07-22, leaving genesis-server DOWN
during bootstrap, and 2026-09-16, during the pre-update backup. Both left the
harness's own `[killed]` marker. Deploys must be DETACHED (`systemd-run --user`).

That correction is the whole reason the second test class below exists. The
original advice shipped in PR #1221 on 2026-07-22 and was refuted by an incident
the same day; the fix landed only in CC memory, both public surfaces kept the
original, and a third deploy died to it 56 days later. Text assertions alone did
not catch that — the hook stayed silent on exactly the dangerous input while every
wording check passed — so the deploy invariant is pinned by EXECUTING the hook,
not by grepping it.

These tests assert invariants (never recommend an unreachable value; always
prescribe detachment for deploys) rather than any one wording, so they do not
fight ordinary edits to the prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The Bash tool's documented hard maximum, in milliseconds.
_CEILING_MS = 600_000

# Surfaces that give a session advice about the Bash tool's timeout.
_ADVICE_SURFACES = (
    Path(".claude/hooks/cc-deploy-timeout-guard"),
    Path(".claude/skills/genesis-development/SKILL.md"),
)

# A millisecond-scale number (6+ digits) presented as a timeout value.
_MS_LITERAL = re.compile(r"\b(\d{6,})\s*ms\b")

# The launch this repo prescribes, matched as a COMMAND rather than as loose
# tokens. Both surfaces also DISCUSS these flags in prose, so whole-file
# containment is satisfied by the discussion even when the command has lost the
# flag — measured: deleting `--unit` from the prescribed command passed a
# `"--unit" in text` assertion in both files.
_PRESCRIBED_CMD = re.compile(r"systemd-run\s+--user\s+--collect\s+--unit\s+genesis-deploy-manual")

# Phrase specific to WHY --scope is wrong. A generic negation like "not a
# substitute" appears twice elsewhere in the skill about unrelated topics, so an
# assertion on it passes no matter what this section says.
_SCOPE_RATIONALE = "keeps the caller's session id"


def _flatten_continuations(text: str) -> str:
    r"""Collapse shell line-continuations so a multi-line command matches as one.

    The prescribed launch is wrapped across three lines with trailing backslashes
    in both surfaces; without this every command-shaped assertion would have to be
    written per-line, which is how they end up matching prose instead.
    """
    return re.sub(r"\\\s*\n\s*", " ", text)


def _advice_files():
    for rel in _ADVICE_SURFACES:
        path = _REPO / rel
        assert path.exists(), f"advice surface missing: {rel}"
        yield rel, path.read_text()


class TestNoUnreachableTimeoutIsRecommended:
    def test_no_millisecond_value_above_the_ceiling(self):
        """The actual defect: '>=900000ms' cannot be satisfied."""
        offenders = []
        for rel, text in _advice_files():
            for raw in _MS_LITERAL.findall(text):
                if int(raw) > _CEILING_MS:
                    offenders.append(f"{rel}: {raw}ms > {_CEILING_MS}ms ceiling")
        assert not offenders, (
            "guidance recommends a Bash timeout above the tool's hard maximum, so a "
            "session that follows it will be SIGTERMed earlier than it expects: "
            + "; ".join(offenders)
        )


class TestTheCeilingIsStated:
    def test_surfaces_name_the_real_maximum(self):
        """Naming the default without the ceiling is what let the wrong number
        stand: '120000ms default' reads as 'raise it as needed'."""
        for rel, text in _advice_files():
            assert "600000" in text or "600,000" in text, (
                f"{rel} advises on the Bash timeout but never states the 600000ms "
                "ceiling — without it, 'set the timeout higher' reads as unbounded"
            )

    def test_surfaces_route_long_work_to_the_background(self):
        """Still correct for long work IN GENERAL — deploys are the carve-out,
        pinned separately below."""
        for rel, text in _advice_files():
            assert "run_in_background" in text, (
                f"{rel} must name run_in_background as the form for work that can "
                "exceed the ceiling — there is no foreground timeout that covers it"
            )


class TestDeploysMustBeDetached:
    """A deploy outlives the turn that starts it, so it must leave the session.

    Text checks are the weak half here: the shipped hook passed every wording
    assertion while staying SILENT on a backgrounded deploy, which is the input
    that actually killed one. So the behavioural cases below drive the real hook.
    """

    def test_surfaces_prescribe_detachment(self):
        for rel, text in _advice_files():
            assert "systemd-run" in text, (
                f"{rel} advises on deploys but never names systemd-run — a deploy "
                "held by a Bash call has been killed mid-run twice"
            )

    def test_surfaces_prescribe_the_unit_flag(self):
        """`--unit` is the flag that actually leaves the session, and it was the
        one part of the remedy nothing pinned.

        Anchored to the COMMAND, not to the token. A first attempt asserted
        `"--unit" in text` and a mutation deleting it from the prescribed command
        still passed, because both files also discuss `--unit` in prose. Whole-file
        containment in a 2800-line skill proves nothing about the one line a reader
        copies.
        """
        for rel, text in _advice_files():
            flattened = _flatten_continuations(text)
            assert _PRESCRIBED_CMD.search(flattened), (
                f"{rel} no longer prescribes `systemd-run --user --collect --unit "
                "genesis-deploy-manual`. --unit is what leaves the session; without "
                "it the command is cgroup-isolated at best and detaches nothing"
            )

    def test_surfaces_carry_the_verify_step(self):
        """A failed systemd-run launch is indistinguishable from a good one: it
        does not inherit the caller's cwd, so a relative path exits instantly and
        --collect reaps the unit, leaving no trace."""
        for rel, text in _advice_files():
            assert "--working-directory" in text, (
                f"{rel} prescribes systemd-run without --working-directory; a bare "
                "relative script path silently resolves under $HOME and the unit "
                "vanishes, which looks exactly like a successful launch"
            )
            assert "is-active" in text, (
                f"{rel} prescribes systemd-run without a verify step — nothing else "
                "distinguishes a launch that took from one that died on startup"
            )

    def test_surfaces_pass_the_environment(self):
        """MEASURED: a --user unit gets systemd's default PATH, which omits
        ~/.local/bin, so uv is missing and bootstrap takes a different branch than
        an interactive run. The shipped code passes env=os.environ.copy(); advice
        that drops that half prescribes a subtly different deploy."""
        for rel, text in _advice_files():
            assert "--setenv=PATH" in text, (
                f"{rel} prescribes a systemd-run deploy without passing PATH; the "
                "unit runs with systemd's default PATH and bootstrap diverges"
            )

    def test_surfaces_warn_that_scope_does_not_detach(self):
        """--scope is the trap: cgroup-isolated, but MEASURED to keep the caller's
        session id. Both surfaces point readers at _apply_direct, which uses
        --scope, so the distinction has to be stated or it will be copied."""
        for rel, text in _advice_files():
            lowered = text.lower()
            assert "--scope" in text, (
                f"{rel} names systemd-run but never distinguishes --scope, which "
                "does NOT leave the session"
            )
            # Mentioning the flag is not the invariant — stating WHY it fails to
            # detach is. Measured: an assertion on "not a substitute" passed a
            # mutation inverting this warning, because that phrase occurs twice
            # elsewhere in the skill about entirely unrelated topics.
            assert _SCOPE_RATIONALE in lowered, (
                f"{rel} mentions --scope without stating that it keeps the "
                "caller's session id; it is the shape a reader is most likely to "
                "copy and be silently wrong about, since _apply_direct uses it "
                "legitimately for a different reason"
            )

    # KNOWN BOUND, stated rather than papered over. Mutation-measured: DELETING the
    # --scope warning fails the test above, but INVERTING one sentence of it to
    # "--scope is a fine substitute" while leaving the rationale sentence in place
    # does NOT. Catching that would mean enumerating wrong phrasings, i.e. a
    # denylist — the exact polarity this PR removed from the guard itself. What is
    # pinned is that the rationale is present; contradicting it in adjacent prose
    # is left to review.

    def test_skill_states_what_each_signal_actually_does(self):
        """PR #1191 added INT/TERM traps at 10:33 on 2026-07-22; the hook shipped a
        contradicting "rollback does not fire on signals" claim at 23:48 the same
        day, inherited from a note rather than read from the code.

        Scoped to the SKILL deliberately. This PR moved the evidence out of the
        hook — its advisory is injected on every fire, and every claim in it is one
        more thing that can rot — so requiring the hook to carry this would fight
        the design. The skill is where a reader goes for what a kill actually did.
        """
        skill = (_REPO / _ADVICE_SURFACES[1]).read_text().lower()
        assert "sigkill" in skill, (
            "the skill does not distinguish SIGKILL (runs no trap, arbitrary "
            "state) from SIGTERM (rolls back) — without that a reader hand-repairs "
            "state on top of a completed rollback"
        )
        assert "traps int/term" in skill or "rollback" in skill, (
            "the skill does not state that update.sh traps signals and rolls back"
        )

    def test_no_surface_repeats_the_stale_no_rollback_claim(self):
        """Safe as a negative now: after the round-3 rewrite neither surface quotes
        the old claim, so there is no citation for this to false-positive on. If a
        future edit wants to cite it as history, phrase it so this still fires —
        the literal is the regression."""
        for rel, text in _advice_files():
            assert "rollback does not fire on signals" not in text.lower(), (
                f"{rel} reasserts a claim PR #1191 made false on 2026-07-22"
            )


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook requires jq")
class TestDeployGuardFiresOnTheDangerousInput:
    """The acceptance bar, replayed: the exact shape that killed a deploy.

    jq ships on ubuntu-latest, so this does not skip in CI.
    """

    HOOK = _REPO / ".claude/hooks/cc-deploy-timeout-guard"
    DEPLOY = "bash scripts/update.sh"

    def _run(self, command: str, background: bool) -> str:
        payload = json.dumps({"tool_input": {"command": command, "run_in_background": background}})
        proc = subprocess.run(
            ["bash", str(self.HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            "the deploy guard is advisory by contract and must ALWAYS exit 0; "
            f"got {proc.returncode}"
        )
        return proc.stdout

    def test_fires_on_a_backgrounded_deploy(self):
        """THE regression. The shipped hook exited early on run_in_background=true,
        treating it as correct usage — so it was inert in precisely the case that
        killed deploys on 2026-07-22 and 2026-09-16."""
        out = self._run(self.DEPLOY, background=True)
        assert out.strip(), (
            "guard stayed silent on a backgrounded deploy — that is the input that "
            "killed two deploys, and silence here is what let it happen twice"
        )
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "systemd-run" in context

    def test_fires_on_a_foreground_deploy(self):
        out = self._run(self.DEPLOY, background=False)
        assert out.strip()
        assert "systemd-run" in json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_silent_when_already_detached(self):
        """Advising systemd-run at someone already running systemd-run is noise,
        and noise is what gets a guard ignored."""
        detached = (
            "systemd-run --user --collect --unit genesis-deploy-manual "
            "--working-directory=$HOME/genesis /bin/bash -c "
            "'exec ./scripts/update.sh > ~/tmp/d.log 2>&1'"
        )
        assert self._run(detached, background=False).strip() == ""

    def test_silent_on_unrelated_commands(self):
        assert self._run("git status", background=False).strip() == ""

    def test_fires_on_scope_which_does_not_detach(self):
        """`systemd-run --user --scope` is cgroup-isolated but keeps the CALLER'S
        session id (MEASURED), so it is still session-held. A skip on the bare
        string `systemd-run` would silence a deploy that has not detached — and the
        advisory's own closing paragraph points readers at a --scope call site, so
        this is a shape readers will actually type."""
        out = self._run("systemd-run --user --scope -- bash scripts/update.sh", False)
        assert out.strip(), "guard went silent on --scope, which does not leave the session"

    def test_fires_on_setsid_which_is_not_a_detacher(self):
        """`setsid` was once on the allowlist. It starts a new SESSION but stays a
        synchronous child of the Bash call, which still waits on it and still
        kills it at the 600000ms ceiling — the exact kill this guard exists for."""
        for cmd in (
            "setsid bash scripts/update.sh",
            "setsid -f bash scripts/update.sh",
        ):
            out = self._run(cmd, False)
            assert out.strip(), (
                f"guard went silent on `{cmd}` — setsid does not leave the "
                "caller's cgroup or release the Bash call"
            )

    def test_substring_mentions_of_a_launcher_do_not_exempt(self):
        """The old `*systemd-run*` / `*--unit*` substring allowlist vouched for a
        deploy on the strength of TEXT, not a launched process. Every shape below
        mentions a launcher without executing one."""
        for cmd in (
            "bash scripts/update.sh # systemd-run --unit x detaches it",
            "NOTE=systemd-run CMD2=--unit bash scripts/update.sh",
            "echo systemd-run --unit u | bash scripts/update.sh",
            "systemd-run --unit x /bin/true & bash scripts/update.sh",
            "bash -c 'systemd-run --unit x bash scripts/update.sh'",
            # `--unit` in the child's arguments or redirect target does not
            # count — only systemd-run's own options are inspected.
            "systemd-run --user --scope -- bash -c 'exec scripts/update.sh > /tmp/--unit.log'",
            "systemd-run --user bash scripts/update.sh --unit=x",
        ):
            out = self._run(cmd, False)
            assert out.strip(), (
                f"guard went silent on `{cmd}` — a launcher that is mentioned or "
                "non-leading does not detach the deploy"
            )

    def test_prescription_names_the_held_entry_point(self):
        """The three deploy scripts are not interchangeable — a bootstrap.sh
        invocation must not be prescribed the update.sh command."""
        out = self._run("bash scripts/bootstrap.sh", False)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "./scripts/bootstrap.sh" in context
        assert "./scripts/update.sh" not in context

    def test_silent_on_unit_long_form(self):
        """`--unit=name` is a valid systemd-run spelling and must stay quiet —
        the token check is exact, not a substring."""
        detached = (
            "systemd-run --user --collect --unit=genesis-deploy-manual "
            "--working-directory=$HOME/genesis /bin/bash -c "
            "'exec ./scripts/update.sh > ~/tmp/d.log 2>&1'"
        )
        assert self._run(detached, background=False).strip() == ""

    def test_fires_on_abbreviated_scope(self):
        """systemd accepts any unambiguous long-option prefix: `--sc`, `--sco`,
        `--scop` all mean --scope to systemd-run. Matching only the full
        spelling let `--sco` slip past the disqualifier and read as detached."""
        for cmd in (
            "systemd-run --user --sco -- bash scripts/update.sh",
            "systemd-run --user --scop=x -- bash scripts/update.sh",
        ):
            out = self._run(cmd, False)
            assert out.strip(), (
                f"guard went silent on `{cmd}` — an abbreviated --scope is "
                "still --scope, and --scope does not leave the session"
            )

    def test_silent_on_abbreviated_unit(self):
        """The prefix rule cuts both ways: `--un=x` is systemd's --unit spelling
        and is genuinely detached, so it must stay quiet."""
        detached = (
            "systemd-run --user --collect --un=genesis-deploy-manual "
            "--working-directory=$HOME/genesis /bin/bash -c "
            "'exec ./scripts/update.sh > ~/tmp/d.log 2>&1'"
        )
        assert self._run(detached, background=False).strip() == ""

    def test_prescription_names_the_FAILING_segment(self):
        """_held_script used to latch onto the first segment MENTIONING a deploy
        script — including one that passed the detachment check — so
        `systemd-run … update.sh && bash host-setup.sh` prescribed update.sh to
        the held host-setup invocation."""
        cmd = (
            "systemd-run --user --collect --unit x "
            "--working-directory=$HOME/genesis /bin/bash -c 'exec ./scripts/update.sh'"
            " && bash scripts/host-setup.sh --reinstall"
        )
        out = self._run(cmd, False)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "./scripts/host-setup.sh" in context
        assert "./scripts/update.sh" not in context

    def test_always_exits_zero_even_if_the_caller_exported_errexit(self):
        """`read -d ''` returns non-zero at EOF. bash imports errexit from an
        exported SHELLOPTS, and settings.json spawns this hook as `bash <path>`,
        so without `|| true` the always-exit-0 contract breaks on a deploy."""
        proc = subprocess.run(
            ["bash", str(self.HOOK)],
            input=json.dumps({"tool_input": {"command": self.DEPLOY}}),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "SHELLOPTS": "errexit"},
        )
        assert proc.returncode == 0, (
            "hook exited non-zero under inherited errexit; a PreToolUse hook's "
            "non-zero exit is not a no-objection signal"
        )

    def test_does_not_recommend_the_remedy_that_failed(self):
        """The defect was never the absence of advice — it was confident wrong
        advice, which displaces the reader's own judgement.

        Asserting only that the old literal is gone is too weak: a reworded
        recommendation slips straight past it. Require the NEGATION to be stated.
        """
        context = json.loads(self._run(self.DEPLOY, background=False))["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "Use run_in_background: true" not in context
        assert "run_in_background" in context, (
            "the advisory must name run_in_background to rule it out; silence "
            "leaves the reader with the habit that killed two deploys"
        )
        lowered = context.lower()
        assert "nor run_in_background works" in lowered or "not the fix" in lowered, (
            "the advisory must actively say run_in_background is wrong here; "
            "merely omitting the old sentence lets a reworded version return"
        )

    def test_emitted_advisory_prescribes_the_flag_that_detaches(self):
        """`--unit` is the whole remedy. The advisory can shed evidence to the
        skill, but never this."""
        context = json.loads(self._run(self.DEPLOY, background=False))["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "--unit" in context
        assert "--working-directory" in context
        assert "--setenv=PATH" in context
        assert "is-active" in context

    def test_emitted_advisory_does_not_repeat_the_stale_claim(self):
        """ "update.sh's rollback does not fire on signals" was true when written and
        made FALSE by PR #1191 at 10:33 on 2026-07-22 — thirteen hours before the
        hook carrying it shipped. A reader who believes nothing happened will
        hand-repair state on top of a COMPLETED rollback.

        The positive half of this now lives on the skill (see
        test_skill_states_what_each_signal_actually_does): the advisory is injected
        on every fire, so it carries the instruction and the skill carries the
        evidence.
        """
        context = json.loads(self._run(self.DEPLOY, background=False))["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "does not fire on signals" not in context.lower()
