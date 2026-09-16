"""The Bash tool's timeout ceiling, and the deploy advice that depends on it.

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
120s ceiling) and WRONG for deploys, which are bound to the session's lifetime and
have been killed mid-run twice that way.

WHY THE DEPLOY CASES EXECUTE THE HOOK. The shipped guard passed every wording
assertion in this file while returning early — and therefore staying SILENT — when
`run_in_background` was true, which is the input that actually killed a deploy.
A text assertion cannot see that, so the regression is pinned by running the guard.

SCOPE OF THIS FILE. It covers what the advice SAYS and whether the guard fires at
all. It deliberately does NOT cover whether a command has already been detached:
deciding that needs real parsing of both shell and systemd-run option grammar, and
is tracked separately in issue #2088. The guard consequently also fires on an
already-detached launch and on a read of one of these files — over-firing, which
costs a line, rather than silence, which cost three deploys.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The Bash tool's documented hard maximum, in milliseconds.
_CEILING_MS = 600_000

_HOOK = Path(".claude/hooks/cc-deploy-timeout-guard")
_SKILL = Path(".claude/skills/genesis-development/SKILL.md")
_ADVICE_SURFACES = (_HOOK, _SKILL)

# A millisecond-scale number (6+ digits) presented as a timeout value.
_MS_LITERAL = re.compile(r"\b(\d{6,})\s*ms\b")

# The launch this repo prescribes, matched as a COMMAND rather than as loose
# tokens. Both surfaces also DISCUSS these flags in prose, so whole-file
# containment is satisfied by the discussion even when the command itself has lost
# the flag.
_PRESCRIBED_CMD = re.compile(r"systemd-run --user --collect --unit")

#: The whole prescribed command, from the bus-variable seeding through the end of
#: the redirect. It starts at XDG_RUNTIME_DIR on purpose: those assignments are part
#: of the command, not commentary — without them `systemd-run --user` cannot reach
#: the user manager from an env-scrubbed CC session at all.
#: Flag assertions are made against THIS SPAN, not the whole file — both surfaces
#: also discuss every one of these flags in prose, so a whole-file containment
#: check stays green after the command itself loses the flag. Measured: deleting
#: `--setenv=PATH` from the skill's command left all 19 tests passing.
_PRESCRIBED_SPAN = re.compile(r"XDG_RUNTIME_DIR=.*?2>&1'")


def _prescribed_span(text: str) -> str:
    """The prescribed systemd-run command as one line, or '' if absent."""
    match = _PRESCRIBED_SPAN.search(_flatten_continuations(text))
    return match.group(0) if match else ""


# Phrase specific to WHY --scope is wrong. A generic negation like "not a
# substitute" appears elsewhere in the skill about unrelated topics, so an
# assertion on it would pass no matter what this section says.
_SCOPE_RATIONALE = "keeps the caller's session id"


def _flatten_continuations(text: str) -> str:
    r"""Collapse shell line-continuations so a multi-line command matches as one."""
    return re.sub(r"\\\s*\n\s*", " ", text)


def _one_line(text: str) -> str:
    """Collapse all whitespace so a prose assertion is not defeated by wrapping."""
    return re.sub(r"\s+", " ", text)


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

    def test_skill_still_routes_ordinary_long_work_to_the_background(self):
        """run_in_background remains correct for long work IN GENERAL. Deploys are
        the carve-out, and collapsing the two is how the wrong remedy spread.

        Pinned on the general-case SENTENCE. A bare `"run_in_background" in text`
        check is satisfied by the carve-out that RULES IT OUT ("run_in_background is
        NOT the fix"), so it would stay green after the general guidance was deleted
        entirely — leaving the skill telling sessions to detach everything.
        """
        skill = _one_line((_REPO / _SKILL).read_text())
        assert "For long or unbounded work generally, use" in skill, (
            "the skill no longer routes ordinary long work to run_in_background; "
            "only the deploy carve-out remains, which over-generalises the ban"
        )


class TestDeployAdviceIsDetachment:
    """What the two surfaces SAY. The guard's behaviour is pinned separately."""

    def test_surfaces_prescribe_detachment(self):
        for rel, text in _advice_files():
            assert "systemd-run" in text, (
                f"{rel} advises on deploys but never names systemd-run — a deploy "
                "held by a Bash call has been killed mid-run twice"
            )

    def test_surfaces_prescribe_the_unit_flag(self):
        """`--unit` is the flag that actually leaves the session.

        Anchored to the COMMAND, not the token: both files also discuss `--unit` in
        prose, so whole-file containment is satisfied by the discussion even after
        the prescribed command loses the flag.
        """
        for rel, text in _advice_files():
            assert _PRESCRIBED_CMD.search(_flatten_continuations(text)), (
                f"{rel} no longer prescribes `systemd-run --user --collect --unit`"
            )

    def test_prescribed_command_carries_every_load_bearing_flag(self):
        """Asserted against the COMMAND SPAN, not the file.

        `--working-directory` because systemd-run does not inherit the caller's cwd,
        so a relative path exits instantly while `--collect` reaps the unit — a
        failed launch that looks exactly like a good one. `--setenv=PATH` because a
        --user unit otherwise runs under systemd's default PATH, which omits
        ~/.local/bin, so bootstrap takes a different branch than an interactive run.

        Measured: a whole-file check on these passed a mutation that deleted
        `--setenv=PATH` from the command, because the surrounding prose explains the
        flag. The prose is not what a reader pastes.
        """
        for rel, text in _advice_files():
            span = _prescribed_span(text)
            assert span, f"{rel} no longer contains a complete prescribed command"
            for flag in (
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
                "systemd-run --user",
                "--unit",
                "--working-directory",
                "--setenv=PATH",
            ):
                assert flag in span, (
                    f"{rel}'s prescribed command lost {flag} — it may still be "
                    "discussed in prose, but the command a reader copies is wrong"
                )

    def test_prescribed_command_seeds_the_bus_before_systemd_run(self):
        """MEASURED 2026-09-16: with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS
        both absent — which a CC session frequently is — `systemd-run --user` dies
        with "Failed to connect to bus: No medium found" and nothing starts.

        ORDER is the invariant, not just presence. `--setenv` configures the
        prospective UNIT and cannot help the CLIENT connect, so the assignments must
        precede the systemd-run invocation to have any effect. update.sh:50-54 seeds
        the same two for the same reason.
        """
        for rel, text in _advice_files():
            span = _prescribed_span(text)
            assert span, f"{rel} no longer contains a complete prescribed command"
            bus = span.index("DBUS_SESSION_BUS_ADDRESS")
            run = span.index("systemd-run")
            assert bus < run, (
                f"{rel} seeds the bus variables AFTER systemd-run; they must come "
                "first or the client cannot reach the user manager at all"
            )

    def test_no_surface_prescribes_detaching_host_setup(self):
        """host-setup.sh is interactive and runs on the bare host VM. Detaching it
        removes stdin, and at host-setup.sh:536-538 the recreate prompt treats EOF
        as the default Y — it stops and renames the existing container. A recipe
        saying "wrap the command you actually ran" would destroy one."""
        for rel, text in _advice_files():
            lowered = _one_line(text.lower())
            assert "bootstrap.sh" in lowered, (
                f"{rel} does not state why bootstrap.sh is excluded"
            )
            assert "host-setup.sh" in lowered, (
                f"{rel} does not mention host-setup.sh at all; the exclusion has to "
                "be stated, because it is a long deploy script and the obvious "
                "generalisation is destructive"
            )
            # NOT "default Y": the skill writes it as `Y` in backticks, so a bare
            # phrase match fails on formatting rather than on meaning. Pin an
            # unformatted phrase both surfaces share.
            assert "stops and renames" in lowered, (
                f"{rel} mentions host-setup.sh without naming the destructive EOF "
                "outcome that makes detaching it unsafe"
            )

    def test_surfaces_carry_the_verify_step(self):
        """Nothing else distinguishes a launch that took from one that died at
        startup — and with --collect there is no failed unit left to inspect."""
        for rel, text in _advice_files():
            assert "is-active" in text, f"{rel} omits the verify step"

    def test_surfaces_warn_that_scope_does_not_detach(self):
        """--scope is the trap: cgroup-isolated, but MEASURED to keep the caller's
        session id. _apply_direct uses it legitimately for a different reason."""
        for rel, text in _advice_files():
            lowered = _one_line(text.lower())
            assert "--scope" in text, f"{rel} never distinguishes --scope"
            assert _SCOPE_RATIONALE in lowered, (
                f"{rel} mentions --scope without saying it keeps the caller's "
                "session id — naming the flag is not the warning"
            )

    def test_surfaces_warn_that_setsid_does_not_detach(self):
        """MEASURED: `timeout 1 setsid bash -c 'sleep 4; …'` exits 124 with no
        output file. A new session id does not stop a parent from waiting.

        Pinned on the REASON, not the token. Measured: a mutation inverting this to
        "setsid IS detachment" passed a bare `"setsid" in text` check, since the
        word survives either way — the same polarity blindness already fixed for
        the --scope warning next door.
        """
        for rel, text in _advice_files():
            lowered = _one_line(text.lower())
            assert "setsid" in lowered, f"{rel} does not mention setsid"
            assert "stop the caller waiting on it" in lowered, (
                f"{rel} names setsid without saying WHY it fails to detach; it "
                "starts a new session, which is what makes it look sufficient"
            )

    def test_skill_does_not_claim_absence_of_a_trap_proves_sigkill(self):
        """The traps install at update.sh:708-709, AFTER the pre-update backup at
        :245-254 — and the 2026-09-16 deploy died during the backup. A SIGTERM
        there also runs no handler, so 'no trap ran' cannot identify the signal."""
        skill = _one_line((_REPO / _SKILL).read_text().lower())
        assert "does not identify a kill as sigkill" in skill, (
            "the skill must warn that the untrapped pre-backup window makes "
            "'no trap ran' uninformative about which signal arrived"
        )

    def test_no_surface_repeats_the_stale_no_rollback_claim(self):
        """Made false by PR #1191 at 10:33 on 2026-07-22, thirteen hours before the
        hook carrying it shipped. Safe as a negative: neither surface quotes it."""
        for rel, text in _advice_files():
            assert "rollback does not fire on signals" not in text.lower(), (
                f"{rel} reasserts a claim PR #1191 made false"
            )


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook requires jq")
class TestDeployGuardFires:
    """The guard, executed. jq ships on ubuntu-latest, so this does not skip in CI."""

    HOOK = _REPO / _HOOK
    DEPLOY = "bash scripts/update.sh"

    def _run(self, command: str, background: bool = False) -> str:
        proc = subprocess.run(
            ["bash", str(self.HOOK)],
            input=json.dumps({"tool_input": {"command": command, "run_in_background": background}}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            "the deploy guard is advisory by contract and must ALWAYS exit 0; "
            f"got {proc.returncode}: {proc.stderr[:300]}"
        )
        return proc.stdout

    def test_fires_on_a_foreground_deploy(self):
        assert self._run(self.DEPLOY).strip()

    def test_fires_on_a_backgrounded_deploy(self):
        """THE regression. The shipped guard exited early on run_in_background=true,
        treating it as correct usage — so it was inert in precisely the case that
        killed deploys on 2026-07-22 and 2026-09-16. Nothing contradicted the advice
        because the guard never spoke when the advice was followed."""
        assert self._run(self.DEPLOY, background=True).strip(), (
            "guard stayed silent on a backgrounded deploy — that is the input that "
            "killed two deploys, and the silence is what let it happen twice"
        )

    def test_silent_on_unrelated_commands(self):
        assert self._run("git status").strip() == ""

    def test_prescribed_command_creates_its_log_directory(self):
        """`~/tmp` is NOT guaranteed: install.sh and bootstrap.sh create it only when
        /tmp is small. The shell opens the redirect BEFORE exec'ing update.sh, so a
        missing directory kills the unit instantly — and --collect then leaves no
        trace, which is the exact invisible failure this advisory warns about."""
        ctx = json.loads(self._run(self.DEPLOY))["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "mkdir -p ~/tmp" in ctx, (
            "prescribed command does not create its log directory; if ~/tmp is "
            "absent the unit dies on the redirect and leaves nothing to diagnose"
        )

    def test_silent_on_host_setup(self):
        """Deliberate: the only advice this hook has is "detach", and detaching
        host-setup.sh reaches a prompt whose EOF default retires the container.
        Saying nothing beats handing over a destructive recipe."""
        assert self._run("bash scripts/host-setup.sh --host h").strip() == "", (
            "guard fired on host-setup.sh — its advisory prescribes detachment, "
            "which for this script means losing stdin at a destructive default"
        )

    def test_silent_on_bootstrap(self):
        """bootstrap.sh calls sudo unconditionally (:165) and its bare `sudo mkdir`
        at :813 is fatal under `set -e`. Detached it has no auth channel, so on any
        install where sudo prompts it aborts partway through configuring the machine.
        `update.sh` has NO sudo calls, which is why it is the only script covered."""
        assert self._run("bash scripts/bootstrap.sh").strip() == "", (
            "guard fired on bootstrap.sh — its only advice is detachment, which "
            "removes the authentication channel bootstrap needs"
        )

    def test_advisory_is_valid_json_in_the_envelope_cc_reads(self):
        """PreToolUse reaches the model ONLY through hookSpecificOutput; a bare
        top-level additionalContext is discarded, and stderr on exit 0 is never
        shown."""
        payload = json.loads(self._run(self.DEPLOY))
        assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert payload["hookSpecificOutput"]["additionalContext"].strip()

    def test_advisory_prescribes_detachment_not_the_remedy_that_failed(self):
        ctx = json.loads(self._run(self.DEPLOY))["hookSpecificOutput"]["additionalContext"]
        assert "Use run_in_background: true" not in ctx
        assert "systemd-run" in ctx and "--unit" in ctx
        assert "run_in_background is NOT the fix" in ctx, (
            "the advisory must actively rule out run_in_background; merely dropping "
            "the old sentence lets a reworded version return"
        )

    def test_advisory_keeps_shell_syntax_literal(self):
        """The prescribed command contains $HOME, $PATH and $(date …) that must
        reach the reader UNEXPANDED. A double-quoted bash assignment would have
        expanded them into the hook's own environment."""
        ctx = json.loads(self._run(self.DEPLOY))["hookSpecificOutput"]["additionalContext"]
        assert "$HOME/genesis" in ctx
        assert '"$PATH"' in ctx
        assert "$(date " in ctx

    def test_always_exits_zero_on_hostile_input(self):
        for payload in ("", "not json", "{}", '{"tool_input":{}}'):
            proc = subprocess.run(
                ["bash", str(self.HOOK)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert proc.returncode == 0, f"non-zero exit on payload: {payload!r}"

    def test_always_exits_zero_under_inherited_errexit(self):
        """`read -d ''` returns non-zero at EOF. bash imports errexit from an
        exported SHELLOPTS, and settings.json spawns this hook as `bash <path>`."""
        import os

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
