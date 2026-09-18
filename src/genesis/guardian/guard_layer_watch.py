"""Guardian-side guard-layer watch — can the agent tooling still EVALUATE?

Every other watch here asks whether Genesis is healthy. This one asks whether the
thing that REPAIRS Genesis is healthy: the Claude Code hook launcher, the modules
its guards import, the interpreter they run on, and the binary the host's own
recovery brain launches.

Why it must live on the HOST. A broken guard layer bricks CC sessions, and the
container-side Sentinel is itself a CC call site — it would dispatch a session
into the same broken tooling. The repo already encodes that reasoning:
``sentinel/remediation_map.py``'s ``UNMAPPED_BY_DESIGN`` excludes CC-tooling
alerts precisely to avoid "waking the tool to fix the tool it's missing". So the
detector has to sit outside the blast radius, and that is here.

Why it is a side-watch and NOT a ``probe_*`` in ``collect_all_signals``.
``SignalResult`` carries no severity, so every probe there feeds
``ConfirmationStateMachine`` → ``RecoveryEngine.execute`` → ``RESTART_CONTAINER`` /
``SNAPSHOT_ROLLBACK``. **A broken hook file must never be able to restart the
container.** This module reaches the alert dispatcher and has no code path to
``RecoveryEngine``.

TWO FAILURE POLARITIES, and the quiet one is the reason this exists.

* ``hook_input`` unimportable → guards ``os._exit(2)`` before reading any payload.
  Every Bash guard that is not declared advisory refuses every command — the rule
  and its current size are derived by
  ``test_import_time_degraded.py::test_every_bash_hook_declares_its_degrade_direction``
  rather than written down, because a count maintained by hand goes stale. That
  fails CLOSED: loud, unmissable, and survivable since PR #2069 restored the
  Write/Edit repair path.
* The LAUNCHER or the venv failing → ``.claude/hooks/genesis-hook`` exits 1 (or
  141 on its own SIGPIPE trap). Claude Code treats a non-2 exit as a NON-blocking
  error when the hook emits no ``permissionDecision``, so **every security guard is
  silently off while Bash keeps working**. Nothing else on this install reports it.

ALERT-ONLY, deliberately, and this is a decision rather than an omission. An
earlier draft carried one automatic repair verb (restore ``hook_input.py`` from
HEAD). An adversarial audit reproduced two ways it destroyed work — ``git checkout
HEAD -- <file>`` overwrites the INDEX, losing staged content recoverable only via
``git fsck``, and mid-merge it clears the conflict stages and silently resolves to
ours while ``MERGE_HEAD`` remains — and the probe's own dirty/clean signal failed
OPEN, because ``git diff --quiet`` is tri-state and BOTH error codes read as
"differs", the value that authorised the write. Both were confirmed by execution.
Detection is the cheap, safe half and ships alone; the repair verb is tracked
separately so it can be built with the scrutiny it has twice shown it needs.

Probe failure / unparseable output = NO signal, never a false alert (git_watch's
rule). Never raises into the tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian.alert.base import Alert, AlertSeverity

# Reuse the exact incus-exec-with-stdin primitive cred_watch/git_watch use — same
# login-shell and kill-on-timeout discipline. Import is side-effect-free.
from genesis.guardian.cred_watch import EpisodeDecision, _incus_exec_stdin, _parse
from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

_STATE_FILE = "guard_layer_state.json"

CONDITION_LAUNCHER = "hook_launcher_dead"
CONDITION_VENV_DEAD = "venv_dead"
CONDITION_HOOK_INPUT = "hook_input_broken"
CONDITION_SHELL_PARSE = "shell_parse_broken"
CONDITION_NODE_DEAD = "node_dead"
CONDITION_CONTAINER_CC = "container_cc_dead"
CONDITION_HOST_BRAIN = "host_brain_dead"
CONDITION_PROBE_TOOLING = "probe_tooling_missing"

_CONTAINER_CONDITIONS = (
    CONDITION_LAUNCHER,
    CONDITION_VENV_DEAD,
    CONDITION_HOOK_INPUT,
    CONDITION_SHELL_PARSE,
    CONDITION_NODE_DEAD,
    CONDITION_CONTAINER_CC,
    CONDITION_PROBE_TOOLING,
)

# Human-facing one-liners. Each names the repair route, because an alert that says
# only "X is broken" makes the reader re-derive what to do at 3am.
_CONDITION_DETAIL = {
    CONDITION_LAUNCHER: (
        ".claude/hooks/genesis-hook does not run. It is what Claude Code actually "
        "invokes, so when it fails every guard exits non-2 and Claude Code reads that "
        "as NON-blocking: the guards are silently OFF while Bash keeps working."
    ),
    CONDITION_VENV_DEAD: (
        "The container's .venv interpreter does not run, so genesis-hook takes its "
        "venv-not-found branch and exits 1 — again non-blocking, again silently off. "
        "Rebuild with scripts/bootstrap.sh from OUTSIDE the venv (see issue #2071)."
    ),
    CONDITION_HOOK_INPUT: (
        "scripts/hooks/hook_input.py does not import. Every Bash guard that is not "
        "declared advisory refuses every command. Write/Edit still work (PR #2069), so "
        "an interactive session can repair the file and Bash returns on its own — every "
        "hook is a fresh subprocess, so there is no cache to clear."
    ),
    CONDITION_SHELL_PARSE: (
        "scripts/hooks/shell_parse.py does not import. The predicate-gated guards "
        "degrade; refusal is partial rather than total."
    ),
    CONDITION_NODE_DEAD: (
        "node does not run in the container. Claude Code cannot start, so no session "
        "exists to repair anything from the inside. Heal via scripts/update.sh."
    ),
    CONDITION_CONTAINER_CC: (
        "The container's `claude` does not run, so no agent session can start there "
        "even though node itself is fine. Probed directly rather than inferred from "
        "node, for the same reason the host leg probes its own binary: a dependency "
        "being healthy is not evidence that its consumer is. Heal via "
        "scripts/update.sh, which aligns the container's CC."
    ),
    CONDITION_PROBE_TOOLING: (
        "The probe could not run its own bounded checks, because a tool it depends on "
        "(`timeout`) is missing in the container. Every OTHER condition is therefore "
        "UNKNOWN this tick rather than broken - they are suppressed deliberately. "
        "Without that suppression a missing `timeout` reports all six as failing on a "
        "perfectly healthy toolchain, which is how a watch earns being ignored."
    ),
    CONDITION_HOST_BRAIN: (
        "The host's configured Claude Code binary (guardian cc.path) does not run or "
        "never answers, so the Guardian's own recovery brain cannot start. "
        "cc_align_host.sh repairs this nightly via the gateway's update-node/update-cc; "
        "run it now to not wait. (A binary that consistently exceeds its probe timeout "
        "is reported here too: unavailable in practice is unavailable.)"
    ),
}

# Container probe. Pure bash piped to `bash -s` — no interpolation, so no quoting
# to get wrong — and deliberately free of the genesis package, since it must
# report ON the interpreter and therefore cannot need it.
#
# Emits exactly one marker line: `GUARDLAYER ok` or `GUARDLAYER <failures>`.
_PROBE_SCRIPT = rb"""
set -u
REPO="$HOME/genesis"
VENV="$REPO/.venv/bin/python"
HOOK="$REPO/.claude/hooks/genesis-hook"
fails=""

# EVERY subcheck is individually bounded, and the BUDGET MUST FIT inside the outer
# incus timeout. Without the bound, one hung command - most pointedly `genesis-hook`
# itself, the exact thing being monitored - runs out the outer timeout, the marker
# below is never printed, the probe parses as None, and every container condition
# goes inconclusive: the detector quiet in precisely the failure it exists for.
#
# But a bound that does not fit is the same failure wearing a fix. An earlier
# version used 10s across SIX subchecks - 60s against a 30s outer default - so
# several wedged commands still consumed the whole budget before the marker ran.
# The arithmetic is now an asserted invariant (see
# test_the_subprobe_budget_fits_inside_the_outer_timeout), so a seventh subcheck
# fails the test instead of silently overcommitting.
#
# `-k` because plain `timeout` sends TERM only: a TERM-resistant command would not
# be bounded at all, which is the case this exists for.
#   worst case 6 x (3 + 1) = 24s < 30s outer default, leaving margin for the
#   login shell and the marker itself.
# The probe must verify its OWN tooling before trusting any verdict it produces.
# "I could not measure the subject" and "the subject is broken" are different
# claims, and collapsing them is the ENV class this module kept being caught by.
# MEASURED: without this, a missing `timeout` makes every bounded subcheck fail
# and all six conditions report broken on a healthy toolchain.
if ! command -v timeout >/dev/null 2>&1; then
  echo "GUARDLAYER probe_tooling_missing"
  exit 0
fi

T="timeout -k 1 3"

# 1. The LAUNCHER. This is what Claude Code actually invokes, and it covers what
#    a venv check alone cannot: a missing or non-executable script, a broken
#    shebang, and the launcher's own `set -euo pipefail` SIGPIPE trap (exit 141,
#    no stderr). A launcher that cannot run makes every guard silently advisory.
#
#    Probed with a script name that does NOT exist, so launcher health is not
#    coupled to any condition measured separately. A working launcher reaches its
#    own error handling and says so; a broken one produces neither marker. Running
#    it against hooks/hook_input.py instead would report BOTH hook_launcher_dead
#    and hook_input_broken for a single broken import - and the launcher alert
#    says "the guards are silently off", which is the wrong polarity for that
#    failure, since a broken hook_input fails CLOSED.
launcher_out=$($T "$HOOK" __guard_layer_probe_nonexistent__.py </dev/null 2>&1 || true)
case "$launcher_out" in
  *"Hook script not found"*|*"Genesis venv not found"*) ;;
  *) fails="$fails hook_launcher_dead" ;;
esac
[ -x "$HOOK" ] || case "$fails" in *hook_launcher_dead*) ;; *) fails="$fails hook_launcher_dead";; esac

# 2. Does the interpreter run at all? The other half of the silent fail-open.
if [ -x "$VENV" ] && $T "$VENV" -c "" >/dev/null 2>&1; then :; else fails="$fails venv_dead"; fi

# 3+4. The two shared modules guards import at module scope. `python -c` puts the
#      cwd on sys.path, so a subshell cd is enough - no path interpolation.
if [ -x "$VENV" ]; then
  ( cd "$REPO/scripts/hooks" && $T "$VENV" -c "import hook_input" ) >/dev/null 2>&1 \
    || fails="$fails hook_input_broken"
  ( cd "$REPO/scripts/hooks" && $T "$VENV" -c "import shell_parse" ) >/dev/null 2>&1 \
    || fails="$fails shell_parse_broken"
fi

# 5. Claude Code is a Node program; without node there is no session at all.
$T node --version >/dev/null 2>&1 || fails="$fails node_dead"

# 6. And node being healthy is NOT evidence that Claude Code can start - the same
#    dependency-proxy mistake the host leg deliberately avoids. Probe the consumer.
$T claude --version >/dev/null 2>&1 || fails="$fails container_cc_dead"

fails="${fails# }"
echo "GUARDLAYER ${fails:-ok}"
"""


def on_a_guardian_host() -> bool:
    """Is this a machine the guardian actually manages?

    `incus` ABSENT is categorically different from `incus exec` FAILING, and
    collapsing them is what made this watch fire on machines it does not apply to:
    a developer box or a CI runner has no `incus`, so the container probe fails,
    and — since the host leg deliberately survives an unreachable container — the
    watch went on to look for the recovery brain, not find it, and alert.

    A failing `incus exec` still means the container is down, which is precisely
    when the host leg matters most. A missing `incus` means there is no container
    and no guardian, so there is nothing here to report on.

    Not a fail-open worth worrying about: if `incus` vanished from a REAL guardian
    host, every probe in `collect_all_signals` fails too and the state machine owns
    that outage — it is not this watch's to detect.
    """
    return shutil.which("incus") is not None


def _parse_probe(stdout: str) -> dict | None:
    """Parse the GUARDLAYER marker. None = no marker (unparseable ⇒ no signal)."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("GUARDLAYER "):
            rest = line[len("GUARDLAYER ") :].strip()
            return {"failures": [] if rest == "ok" else rest.split()}
    return None


async def probe_guard_layer(config) -> dict | None:
    """Live guard-layer probe inside the container via ``incus exec``.

    None = unreachable or unparseable ⇒ NO signal. A down container is the
    confirmation state machine's concern, not this watch's.
    """
    cfg = config.guard_layer
    try:
        rc, out = await _incus_exec_stdin(
            config.container_name,
            "bash -s",
            _PROBE_SCRIPT,
            cfg.check_timeout_s,
            # EVERY path in the payload is $HOME-relative, so probing as the wrong
            # user reports a healthy toolchain as broken, persistently, on any
            # install that configures another user.
            user=getattr(config, "container_user", "ubuntu"),
        )
    except (TimeoutError, OSError):
        logger.warning("guard_layer_watch probe exec failed", exc_info=True)
        return None
    if rc != 0:
        return None
    return _parse_probe(out)


async def probe_host_brain(config) -> bool | None:
    """Can the host's own recovery brain start? True/False, or None = inconclusive.

    Runs the SAME binary ``diagnosis.py`` launches — ``config.cc.path``, expanded
    the same way — rather than checking ``node --version``. That choice is the
    point: host Node is frequently nvm-managed and a systemd timer's PATH is
    minimal, so probing node directly invites a false positive. Probing the
    consumer cannot produce one, because if THIS resolution fails then the
    recovery brain genuinely cannot start, PATH problem or not.

    ``start_new_session`` plus the proc_kill pair mirror ``diagnosis.py``'s auth
    probe: ``claude`` is a wrapper, so killing only it orphans the node children
    the timeout exists to reap — and this runs on every tick.
    """
    cc_path = str(Path(config.cc.path).expanduser())
    try:
        proc = await asyncio.create_subprocess_exec(
            cc_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        # Binary absent or not executable — a definite negative, not inconclusive.
        return False
    try:
        await asyncio.wait_for(proc.communicate(), timeout=config.guard_layer.check_timeout_s)
    except TimeoutError:
        kill_process_group(proc)
        await reap_bounded(proc)
        return None  # a wedge is not proof of death
    except asyncio.CancelledError:
        # CancelledError is BaseException on 3.12, so it does NOT reach the
        # `except Exception` below. The child was started in its own session, so a
        # cancelled guardian task would otherwise leave the whole claude/node tree
        # running with nothing left to reap it. DiagnosisEngine handles this
        # explicitly for the same binary; this mirrors it. Re-raised, because
        # cancellation is not a verdict about the host brain.
        kill_process_group(proc)
        await reap_bounded(proc)
        raise
    except Exception:
        kill_process_group(proc)
        await reap_bounded(proc)
        logger.debug("guard_layer_watch host-brain probe raised", exc_info=True)
        return None
    return (proc.returncode or 0) == 0


def host_brain_state(config, brain_ok: bool | None, episode: dict | None) -> str:
    """Resolve the host leg to failing / healthy / inconclusive.

    Three inputs collapse to three outcomes, and each collapse is a decision:

    * CC diagnosis DISABLED (``config.cc.enabled`` false) ⇒ inconclusive, forever.
      That is a supported configuration — ``scripts/install_guardian.sh`` writes it
      when Claude is absent, and ``DiagnosisEngine`` skips CC for the same reason —
      so alerting on it would be a recurring false alarm about a component nobody
      wants running.
    * A PERSISTENT wedge ⇒ failing. One timeout says nothing, but a binary that
      never answers within its timeout is operationally unavailable whether or not
      it is technically dead, and treating every wedge as inconclusive left that
      state silent forever.
    * Anything else ⇒ what the probe said.
    """
    if not getattr(getattr(config, "cc", None), "enabled", True):
        return "inconclusive"
    if brain_ok is True:
        return "healthy"
    if brain_ok is False:
        return "failing"
    # A single wedge is genuinely weaker evidence than a definite negative, so it
    # stays INCONCLUSIVE — but the streak that follows must not be confirmed TWICE.
    # Returning "failing" at the threshold and then letting the generic ladder start
    # its own `consecutive` from one puts the WARN at 2 * confirm_ticks - 1 timeouts
    # instead of the configured threshold. The orchestrator therefore SEEDS the
    # ladder from the wedge streak (see where CONDITION_HOST_BRAIN is resolved), so
    # the streak is carried into the decision rather than re-earned.
    if (episode or {}).get("wedged", 0) >= config.guard_layer.confirm_ticks:
        return "failing"
    return "inconclusive"


def decide(
    condition: str, is_failing: bool, episode: dict | None, now: datetime, cfg
) -> EpisodeDecision:
    """Pure escalation decision for ONE condition (fully unit-tested).

    ``confirm_ticks`` consecutive failures before the first WARN absorbs a blip
    during, e.g., a deploy rebuilding the venv; then re-alert on cadence only.
    Matches ``git_watch``'s ladder exactly — there is no step-in, because this
    watch takes no action.
    """
    if not is_failing:
        if episode and episode.get("warned_at"):
            return EpisodeDecision("resolved", f"{condition} cleared")
        return EpisodeDecision("none", "healthy")

    consecutive = episode.get("consecutive", 0) if episode else 0
    if consecutive < cfg.confirm_ticks:
        return EpisodeDecision(
            "none", f"{condition} {consecutive}/{cfg.confirm_ticks} — confirming"
        )

    if not (episode and episode.get("warned_at")):
        return EpisodeDecision("warn", f"confirmed {condition}")

    last_alert = _parse(episode.get("last_alert_at"))
    if last_alert and (now - last_alert).total_seconds() < cfg.realert_hours * 3600:
        return EpisodeDecision("none", "already warned, within re-alert window")
    return EpisodeDecision("realert", f"still {condition} after guardian warning")


def _load_state(path: Path) -> dict:
    """Load the episode map, degrading to empty on ANY structural problem.

    Valid JSON of the wrong SHAPE (`[]`, `null`, a string) is the case that bites:
    `.get` on a non-mapping raises AttributeError, which is not a JSONDecodeError,
    so it escaped the old handler and reached the orchestrator's outer swallow.
    That logged and moved on WITHOUT rewriting the file, so every later tick hit
    the same exception after paying for the probe, and no condition was ever
    processed again. Each episode is checked too — a non-mapping entry would raise
    the same way deeper in.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    episodes = data.get("episodes")
    if not isinstance(episodes, dict):
        return {}
    return {k: _sane_episode(v) for k, v in episodes.items() if isinstance(v, dict)}


#: Counter fields are arithmetic targets and timestamp fields are parsed. A value
#: of the wrong TYPE in either passes an isinstance check on the episode itself and
#: then raises one frame deeper — which the outer swallow logs while leaving the
#: file in place, so every later tick wedges identically. Checking the container
#: and not the contents is the same scope error one level down.
_EPISODE_COUNTERS = ("consecutive", "wedged")
_EPISODE_STAMPS = ("first_seen", "warned_at", "last_alert_at")


def _sane_episode(episode: dict) -> dict:
    """Drop fields whose type would raise when used, rather than trusting them."""
    clean = dict(episode)
    for field in _EPISODE_COUNTERS:
        value = clean.get(field)
        if field in clean and (isinstance(value, bool) or not isinstance(value, int)):
            clean.pop(field)
    for field in _EPISODE_STAMPS:
        if field in clean and not isinstance(clean[field], str):
            clean.pop(field)
    return clean


def _save_state(path: Path, episodes: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "episodes": episodes}))
    except OSError:
        logger.warning("failed to persist guard-layer alert state", exc_info=True)


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("guard_layer_watch alert dispatch failed", exc_info=True)


async def check_guard_layer_and_alert(config, dispatcher) -> None:
    """Guardian tick: can the agent tooling still evaluate? Alert if not.

    Never raises into the tick. Alerts go through the host dispatcher, which POSTs
    straight to Telegram over stdlib urllib — so an alert survives a fully dead
    container, which is the whole point.
    """
    try:
        cfg = config.guard_layer
        if not getattr(cfg, "enabled", True):
            return
        if not on_a_guardian_host():
            return

        # The container probe and the host probe are INDEPENDENT, and the host leg
        # is host-local. Returning early on an unreachable container would suppress
        # the recovery brain's own failure at exactly the moment it matters most —
        # the container being down is when that brain is what fixes things.
        probe = await probe_guard_layer(config)

        state_file = config.state_path / _STATE_FILE
        episodes = _load_state(state_file)
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        failing: set[str] = set()
        inconclusive: set[str] = set()
        known: list[str] = []

        if probe is None:
            # Unreachable — the state machine owns "down". No CONTAINER evidence
            # either way, so those conditions are inconclusive rather than healthy.
            inconclusive.update(_CONTAINER_CONDITIONS)
        elif CONDITION_PROBE_TOOLING in probe["failures"]:
            # The probe could not run its own bounded checks. Its readings of every
            # OTHER condition are therefore meaningless, and reporting them would be
            # six false alarms at once. Report the one thing actually observed.
            known.append(CONDITION_PROBE_TOOLING)
            failing.add(CONDITION_PROBE_TOOLING)
            inconclusive.update(
                c for c in _CONTAINER_CONDITIONS if c != CONDITION_PROBE_TOOLING
            )
        else:
            known.extend(_CONTAINER_CONDITIONS)
            failing.update(probe["failures"])

        brain_ok = await probe_host_brain(config)
        brain_episode = episodes.get(CONDITION_HOST_BRAIN)
        # Track the wedge streak BEFORE resolving, so a persistent timeout can
        # escalate rather than staying inconclusive forever.
        if brain_ok is None:
            brain_episode = brain_episode or {}
            brain_episode["wedged"] = brain_episode.get("wedged", 0) + 1
            episodes[CONDITION_HOST_BRAIN] = brain_episode
        elif brain_episode:
            brain_episode.pop("wedged", None)

        brain_state = host_brain_state(config, brain_ok, brain_episode)
        if brain_state == "inconclusive":
            inconclusive.add(CONDITION_HOST_BRAIN)
        else:
            known.append(CONDITION_HOST_BRAIN)
            if brain_state == "failing":
                failing.add(CONDITION_HOST_BRAIN)
                # CARRY the wedge streak into the ladder rather than re-earning it.
                # Those ticks already WERE the confirmation; making `consecutive`
                # start from one again would put the WARN at 2 * confirm_ticks - 1
                # timeouts instead of the configured threshold.
                wedged = (brain_episode or {}).get("wedged", 0)
                if wedged and brain_episode is not None:
                    brain_episode["consecutive"] = max(
                        brain_episode.get("consecutive", 0), wedged - 1
                    )

        for condition in sorted(set(known) | set(episodes)):
            # An INCONCLUSIVE condition has no evidence in either direction. Without
            # this skip it would be re-admitted from persisted state, fall through
            # the not-failing branch and emit a FALSE "recovered" — clearing a ladder
            # that is still climbing, so a condition whose probe intermittently
            # wedges could never escalate past the first warning.
            if condition in inconclusive:
                continue

            is_failing = condition in failing
            episode = episodes.get(condition)

            if is_failing:
                episode = episode or {}
                episode["consecutive"] = episode.get("consecutive", 0) + 1
                episodes[condition] = episode
            elif episode:
                if not episode.get("warned_at"):
                    # Healthy again before we ever alerted: nothing to resolve, and
                    # nothing worth keeping. Leaving it strands state on disk forever
                    # for a condition that blipped once.
                    episodes.pop(condition, None)
                    continue
                episode["consecutive"] = 0

            decision = decide(condition, is_failing, episode, now, cfg)
            detail = _CONDITION_DETAIL.get(condition, "")

            if decision.action == "resolved":
                # SCOPED to the condition that cleared. Several can fail together,
                # and others may be inconclusive right now, so an unqualified "the
                # agent tooling can evaluate again" would tell the operator the
                # tooling is usable while another condition is still warned - the
                # most expensive kind of wrong, because it is an all-clear.
                # Derived from THIS TICK's evidence, not from loop mutation order.
                # Reading `episodes` mid-loop treats a condition that also recovered
                # this tick but has not been processed yet as still degraded, so two
                # simultaneous recoveries produce one alert saying STILL DEGRADED and
                # a later one saying everything is healthy — from the same invocation.
                remaining = sorted(
                    c
                    for c in episodes
                    if c != condition
                    and c in failing
                    and episodes.get(c, {}).get("warned_at")
                )
                if remaining:
                    tail = f" STILL DEGRADED: {', '.join(remaining)}."
                elif inconclusive:
                    tail = (
                        " Other conditions were not observed this tick, so this is "
                        "not an all-clear."
                    )
                else:
                    tail = " Everything this watch tracks is healthy again."
                await _send(
                    dispatcher,
                    AlertSeverity.INFO,
                    f"Guard layer recovered: {condition}",
                    f"{condition} cleared.{tail}",
                )
                episodes.pop(condition, None)
            elif decision.action in ("warn", "realert"):
                episode["warned_at"] = episode.get("warned_at") or now_iso
                episode["last_alert_at"] = now_iso
                severity = (
                    AlertSeverity.WARNING if decision.action == "warn" else AlertSeverity.CRITICAL
                )
                suffix = (
                    ""
                    if decision.action == "warn"
                    else "\n\nStill unresolved since the first warning."
                )
                await _send(
                    dispatcher,
                    severity,
                    f"Guard layer degraded: {condition}",
                    f"{detail}{suffix}",
                )

        _save_state(state_file, episodes)
    except Exception:
        # WARNING, not debug: this except sits INSIDE the function, so check.py's
        # own wrapper never fires. At debug level a persistent bug here would be
        # invisible in journald and the watch would report healthy by silence.
        logger.warning("guard_layer_watch check failed", exc_info=True)
