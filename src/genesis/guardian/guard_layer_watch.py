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
CONDITION_HOST_BRAIN = "host_brain_dead"

_CONTAINER_CONDITIONS = (
    CONDITION_LAUNCHER,
    CONDITION_VENV_DEAD,
    CONDITION_HOOK_INPUT,
    CONDITION_SHELL_PARSE,
    CONDITION_NODE_DEAD,
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
    CONDITION_HOST_BRAIN: (
        "The host's configured Claude Code binary (guardian cc.path) does not run, so "
        "the Guardian's own recovery brain cannot start. cc_align_host.sh repairs this "
        "nightly via the gateway's update-node/update-cc; run it now to not wait."
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

# 1. The LAUNCHER, end to end. This is what Claude Code actually invokes, and it
#    covers what a venv check alone cannot: a missing or non-executable script, a
#    broken shebang, and the launcher's own `set -euo pipefail` SIGPIPE trap
#    (exit 141, no stderr). A launcher that cannot run makes every guard silently
#    advisory, which is the failure this whole module exists to notice.
if [ -x "$HOOK" ] && echo '{}' | "$HOOK" hooks/hook_input.py >/dev/null 2>&1; then :
else fails="$fails hook_launcher_dead"; fi

# 2. Does the interpreter run at all? The other half of the silent fail-open.
if [ -x "$VENV" ] && "$VENV" -c "" >/dev/null 2>&1; then :; else fails="$fails venv_dead"; fi

# 3+4. The two shared modules guards import at module scope. `python -c` puts the
#      cwd on sys.path, so a subshell cd is enough - no path interpolation.
if [ -x "$VENV" ]; then
  ( cd "$REPO/scripts/hooks" && "$VENV" -c "import hook_input" ) >/dev/null 2>&1 \
    || fails="$fails hook_input_broken"
  ( cd "$REPO/scripts/hooks" && "$VENV" -c "import shell_parse" ) >/dev/null 2>&1 \
    || fails="$fails shell_parse_broken"
fi

# 5. Claude Code is a Node program; without node there is no session at all.
node --version >/dev/null 2>&1 || fails="$fails node_dead"

fails="${fails# }"
echo "GUARDLAYER ${fails:-ok}"
"""


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
            config.container_name, "bash -s", _PROBE_SCRIPT, cfg.check_timeout_s
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
    except Exception:
        kill_process_group(proc)
        await reap_bounded(proc)
        logger.debug("guard_layer_watch host-brain probe raised", exc_info=True)
        return None
    return (proc.returncode or 0) == 0


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
    if not path.exists():
        return {}
    try:
        episodes = json.loads(path.read_text()).get("episodes", {})
        return episodes if isinstance(episodes, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


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

        probe = await probe_guard_layer(config)
        if probe is None:
            return  # unreachable — the state machine owns "down"

        failing = set(probe["failures"])

        # The host leg is a SEPARATE probe with three outcomes, and the third must
        # not be collapsed into either of the others.
        brain_ok = await probe_host_brain(config)
        inconclusive: set[str] = set()
        known = list(_CONTAINER_CONDITIONS)
        if brain_ok is None:
            inconclusive.add(CONDITION_HOST_BRAIN)
        else:
            known.append(CONDITION_HOST_BRAIN)
            if brain_ok is False:
                failing.add(CONDITION_HOST_BRAIN)

        state_file = config.state_path / _STATE_FILE
        episodes = _load_state(state_file)
        now = datetime.now(UTC)
        now_iso = now.isoformat()

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
                await _send(
                    dispatcher,
                    AlertSeverity.INFO,
                    f"Guard layer recovered: {condition}",
                    f"{condition} cleared. The agent tooling can evaluate again.",
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
