"""Tests for genesis.env deploy-state helpers.

Covers ``update_in_progress()`` — the shared signal the autonomy watchdog uses
to DEFER restarting genesis-server during a deploy (incident IR-2). The contract
is fail-open: any dead / absent / corrupt / expired signal → False, and the
helper must NEVER raise into its caller (the watchdog restart path).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.env import update_in_progress


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point genesis_home() at an isolated tmp dir (via the GENESIS_HOME env)."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    return tmp_path


def _write_state(
    home: Path,
    *,
    phase: str = "bootstrap",
    pid: int | None = None,
    started_at: str | None = None,
) -> None:
    """Write an update_state.json like update.sh::_write_state does."""
    if pid is None:
        pid = os.getpid()  # a real, alive, > 1 pid
    if started_at is None:
        started_at = datetime.now(UTC).isoformat()
    (home / "update_state.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "pid": pid,
                "started_at": started_at,
                "rollback_tag": "pre-update-x",
                "timestamp": started_at,
            }
        )
    )


def _kill_dead(_pid: int, _sig: int) -> None:
    """Stand-in for os.kill on a dead pid."""
    raise ProcessLookupError


class TestStateJsonPath:
    """The CLI path — update.sh writes update_state.json (the incident path)."""

    def test_false_when_nothing_present(self, home: Path):
        assert update_in_progress() is False

    def test_true_for_live_pid_mid_bootstrap(self, home: Path):
        _write_state(home, phase="bootstrap", pid=os.getpid())
        assert update_in_progress() is True

    def test_false_when_phase_done(self, home: Path):
        # "done" is written just before the file is removed — not in progress.
        _write_state(home, phase="done", pid=os.getpid())
        assert update_in_progress() is False

    def test_false_for_dead_pid(self, home: Path, monkeypatch: pytest.MonkeyPatch):
        _write_state(home, pid=os.getpid())
        monkeypatch.setattr(os, "kill", _kill_dead)
        assert update_in_progress() is False

    def test_false_for_pid_le_1(self, home: Path):
        # An AsyncMock().pid is 1 in py3.12 — must never count as a live deploy.
        _write_state(home, pid=1)
        assert update_in_progress() is False

    def test_false_for_stale_started_at(self, home: Path):
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        _write_state(home, pid=os.getpid(), started_at=old)
        assert update_in_progress() is False

    def test_true_when_started_at_missing(self, home: Path):
        # No timestamp → fall back to pid liveness alone.
        (home / "update_state.json").write_text(
            json.dumps({"phase": "bootstrap", "pid": os.getpid()})
        )
        assert update_in_progress() is True

    def test_true_for_naive_started_at(self, home: Path):
        # A naive (tz-less) timestamp is treated as UTC by the recency check,
        # so a fresh naive start still counts as in-progress (never wrongly stale).
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()  # no offset
        _write_state(home, pid=os.getpid(), started_at=naive)
        assert update_in_progress() is True

    def test_false_for_corrupt_json(self, home: Path):
        (home / "update_state.json").write_text("{not valid json")
        assert update_in_progress() is False


class TestPidFilePath:
    """The dashboard path — updates.py writes a bare-int update_in_progress.pid."""

    def test_true_for_live_pid_file(self, home: Path):
        (home / "update_in_progress.pid").write_text(str(os.getpid()))
        assert update_in_progress() is True

    def test_false_for_dead_pid_file(self, home: Path, monkeypatch: pytest.MonkeyPatch):
        (home / "update_in_progress.pid").write_text(str(os.getpid()))
        monkeypatch.setattr(os, "kill", _kill_dead)
        assert update_in_progress() is False

    def test_false_for_pid_le_1(self, home: Path):
        (home / "update_in_progress.pid").write_text("1")
        assert update_in_progress() is False

    def test_false_for_garbage_pid_file(self, home: Path):
        (home / "update_in_progress.pid").write_text("not-a-pid")
        assert update_in_progress() is False


def test_never_raises_fails_open_to_false(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unexpected error type must fail open to False, never propagate."""
    _write_state(home, pid=os.getpid())

    def _boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    # read_text isn't in any inner except tuple — only the outer catch saves us.
    monkeypatch.setattr(Path, "read_text", _boom)
    assert update_in_progress() is False


def test_pid_file_takes_precedence_but_either_signal_counts(home: Path):
    """Both signals present → still True (dashboard path checked first)."""
    (home / "update_in_progress.pid").write_text(str(os.getpid()))
    _write_state(home, phase="done", pid=1)  # JSON path alone would be False
    assert update_in_progress() is True


class TestUserTimezonePrecedence:
    """genesis.yaml ``timezone`` is authoritative; ``USER_TIMEZONE`` env is a
    DEPRECATED fallback consulted only when the file has no timezone key.

    ``_local_config`` reads ``Path.home()/.genesis/config/genesis.yaml`` (NOT
    ``GENESIS_HOME``), so we patch ``Path.home`` rather than reuse the ``home``
    fixture. Every case invalidates the module cache after mutating state.
    """

    @staticmethod
    def _write_cfg(home_dir: Path, *, timezone: str | None) -> None:
        cfg_dir = home_dir / ".genesis" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        body = "github:\n  user: someone\n"
        if timezone is not None:
            body = f"timezone: {timezone}\n" + body
        (cfg_dir / "genesis.yaml").write_text(body)

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from genesis.env import _invalidate_local_config

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("USER_TIMEZONE", raising=False)
        _invalidate_local_config()
        yield
        _invalidate_local_config()

    def test_file_wins_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The flip: genesis.yaml outranks a DIFFERENT USER_TIMEZONE env value.
        from genesis.env import _invalidate_local_config, user_timezone

        self._write_cfg(tmp_path, timezone="Europe/Paris")
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Europe/Paris"

    def test_env_fallback_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No genesis.yaml at all → env fallback preserves behavior (no regression).
        from genesis.env import _invalidate_local_config, user_timezone

        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_env_fallback_when_file_has_no_timezone_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from genesis.env import _invalidate_local_config, user_timezone

        self._write_cfg(tmp_path, timezone=None)
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_utc_when_neither_present(self, tmp_path: Path):
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "UTC"

    def test_blank_file_value_falls_through_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A whitespace-only file timezone must not shadow the env fallback with "".
        self._write_cfg(tmp_path, timezone='"   "')
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_blank_file_and_no_env_is_utc(self, tmp_path: Path):
        self._write_cfg(tmp_path, timezone='"   "')
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "UTC"

    @pytest.mark.parametrize("scalar", ["no", "false", "0"])
    def test_falsy_yaml_scalar_falls_through_to_env(
        self, scalar: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # YAML footgun: `timezone: no` -> False, `0` -> int. These are NOT valid
        # zones and must fall through to the env fallback, not shadow it with
        # "False"/"0" (which would crash CronTrigger consumers).
        self._write_cfg(tmp_path, timezone=scalar)
        monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "Europe/Paris"

    def test_invalid_file_zone_falls_through_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A typo written to genesis.yaml (e.g. by setup-local-config's free-form
        # prompt) must not be returned unvalidated and crash CronTrigger consumers.
        self._write_cfg(tmp_path, timezone="Amrica/Chicago")  # typo (invalid)
        monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "Europe/Paris"

    def test_invalid_file_and_invalid_env_is_utc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._write_cfg(tmp_path, timezone="Amrica/Chicago")
        monkeypatch.setenv("USER_TIMEZONE", "Not/AZone")
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "UTC"


class TestEmbedPriorityTier:
    """The default here is a COST decision, so it gets an explicit lock.

    Recall embeddings default to DeepInfra's paid priority tier (1.5x:
    $0.010 -> $0.015 per 1M tokens). MEASURED 2026-09-04: on the default tier
    that endpoint took 8.6-13.3s under load against a 4.5s route deadline, so
    recall failed 100% of the time (20/20 live). At this install's measured
    volume — 217 recall requests in 24h, ~120 tokens each — the premium is about
    half a cent per month.

    A regression that silently flips this to False would restore the outage; one
    that ignores the opt-out would bill another install without consent. Both
    directions are pinned.
    """

    def test_defaults_on(self, monkeypatch: pytest.MonkeyPatch):
        """Deliberately NOT using the `home` fixture — it would not isolate this.

        `home` sets GENESIS_HOME, while `_local_config` reads Path.home(), so an
        operator who exercises the documented opt-out would turn this test red for
        a reason its name actively misleads about. Patch the module globals.
        """
        import genesis.env as env_mod

        monkeypatch.delenv("GENESIS_EMBED_PRIORITY_TIER", raising=False)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {})
        assert env_mod.embed_priority_tier() is True

    def test_null_memory_key_does_not_crash_the_boot_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`memory:` with its child commented out loads as {'memory': None}.

        `.get("memory", {})` returns None there — a dict default applies to a
        MISSING key, not a null one — and `None.get(...)` raises straight out of
        the memory bootstrap that calls this. The documented opt-out must not be
        able to break the subsystem it opts out of.
        """
        import genesis.env as env_mod

        monkeypatch.delenv("GENESIS_EMBED_PRIORITY_TIER", raising=False)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {"memory": None})
        assert env_mod.embed_priority_tier() is True  # falls through to the default

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "OFF"])
    def test_env_can_opt_out(self, home: Path, monkeypatch: pytest.MonkeyPatch, value: str):
        monkeypatch.setenv("GENESIS_EMBED_PRIORITY_TIER", value)
        from genesis.env import embed_priority_tier

        assert embed_priority_tier() is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_env_can_opt_in_explicitly(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ):
        monkeypatch.setenv("GENESIS_EMBED_PRIORITY_TIER", value)
        from genesis.env import embed_priority_tier

        assert embed_priority_tier() is True

    def test_local_config_can_opt_out(self, monkeypatch: pytest.MonkeyPatch):
        """A fresh clone with no env var must still be able to say no in yaml.

        The local config is cached in MODULE GLOBALS and resolves against
        Path.home() rather than GENESIS_HOME, so it is patched directly here —
        writing a yaml file under the tmp home would simply not be read.
        """
        import genesis.env as env_mod

        monkeypatch.delenv("GENESIS_EMBED_PRIORITY_TIER", raising=False)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {"memory": {"embed_priority_tier": False}})
        assert env_mod.embed_priority_tier() is False

    def test_env_overrides_local_config(self, monkeypatch: pytest.MonkeyPatch):
        """Env beats yaml, matching every sibling accessor in this module."""
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {"memory": {"embed_priority_tier": False}})
        monkeypatch.setenv("GENESIS_EMBED_PRIORITY_TIER", "true")
        assert env_mod.embed_priority_tier() is True


# The accessors that read a NESTED key out of a local-config section, paired with
# the section they read and the value they must fall back to. This list is
# hand-maintained and therefore CANNOT catch a new accessor that forgets the helper —
# an omission is simply absent from it. `test_no_unrouted_nested_config_read` below is
# the enforcement; this list is the behavioural coverage for the ones that exist.
_SECTION_ACCESSORS = [
    ("ollama_url", "network", "http://localhost:11434"),
    ("lm_studio_url", "network", "http://localhost:1234/v1"),
    ("ollama_enabled", "network", False),
    ("embed_priority_tier", "memory", True),
    ("build_lane_enabled", "build_lane", False),
    ("models_md_synthesis_enabled", "models_md_synthesis", True),
    ("github_user", "github", ""),
    ("github_public_repo", "github", "GENesis-AGI"),
]

# Env vars that would short-circuit an accessor before it ever reads the config.
_SECTION_ACCESSOR_ENV = [
    "OLLAMA_URL",
    "LM_STUDIO_URL",
    "GENESIS_ENABLE_OLLAMA",
    "GENESIS_EMBED_PRIORITY_TIER",
    "GENESIS_BUILD_LANE_ENABLED",
    "GENESIS_MODELS_MD_SYNTHESIS_OFF",
    "GENESIS_GITHUB_USER",
    "GENESIS_GITHUB_PUBLIC_REPO",
]


class TestLocalConfigSectionRobustness:
    """A malformed section must degrade to the default, never raise.

    `_local_config`'s own docstring promises callers "fall through to their
    hardcoded defaults gracefully", and `models_md_synthesis_enabled` promises
    "local config swallows" — but a nested `.get` on a section that is not a
    mapping raises AttributeError instead. That reaches real boot paths:
    `runtime/init/memory.py` catches Exception and records an init degradation,
    so a one-line yaml typo silently runs the whole install with NO vector
    memory. The user-editable file must not be able to do that.

    Two malformed shapes, both reachable from hand-edited yaml:
      `memory:`            with the child commented out  -> None
      `memory: enabled`    a scalar where a mapping goes -> str
    """

    @pytest.fixture(autouse=True)
    def _no_env_shortcircuit(self, monkeypatch: pytest.MonkeyPatch):
        for name in _SECTION_ACCESSOR_ENV:
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(("accessor", "section", "expected"), _SECTION_ACCESSORS)
    @pytest.mark.parametrize("bad", [None, "enabled", ["a"], 7, True])
    def test_malformed_section_falls_through_to_default(
        self, monkeypatch: pytest.MonkeyPatch, accessor: str, section: str, expected, bad
    ):
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {section: bad})
        assert getattr(env_mod, accessor)() == expected

    @pytest.mark.parametrize(("accessor", "section", "expected"), _SECTION_ACCESSORS)
    @pytest.mark.parametrize("root", [["a", "b"], "scalar", 7])
    def test_non_mapping_config_root_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, accessor: str, section: str, expected, root
    ):
        """A yaml file whose ROOT is a list or scalar breaks every accessor at once.

        `_local_config` guards `None` via `or {}` but keeps a truthy non-mapping.
        """
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", root)
        assert getattr(env_mod, accessor)() == expected

    def test_non_mapping_root_does_not_break_top_level_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`user_timezone` reads a top-level key, so the root guard is its only cover."""
        import genesis.env as env_mod

        # USER_TIMEZONE is the var this accessor actually reads. Deleting the two
        # plausible-looking names instead left the isolation a no-op, and
        # `isinstance(..., str)` is true of every possible return — so the test
        # asserted only "does not raise" while claiming to exercise the fallback.
        monkeypatch.delenv("USER_TIMEZONE", raising=False)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", ["not", "a", "mapping"])
        assert env_mod.user_timezone() == "UTC"

    def test_well_formed_section_still_wins(self, monkeypatch: pytest.MonkeyPatch):
        """The guard must not swallow a VALID opt-out — that would be worse."""
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {"memory": {"embed_priority_tier": False}})
        assert env_mod.embed_priority_tier() is False


class TestSecretsTemplateDoesNotShadowYaml:
    """A template line that is COPIED to secrets.env silently outranks the yaml.

    `bootstrap.sh` copies secrets.env.example to secrets.env on a fresh install and
    the server loads it into the environment; every accessor here checks env FIRST.
    So an uncommented assignment in the template makes the documented yaml lever
    dead on arrival for exactly the operators reading the docs for the first time —
    including the answers `setup-local-config.sh` interactively PROMPTS for, which a
    shadowing assignment turns into a silently dead setup step.

    THE PREDICATE, because the obvious one is wrong: an assignment is safe to comment
    out iff the template value EQUALS the `${VAR:-default}` that config/model_routing.yaml
    expands. It is NOT "has no shell-expansion consumer" — the three URLs below all have
    one and are still safe, because their defaults are byte-identical.
    GENESIS_ENABLE_OLLAMA is the case that fails the predicate: template `false` vs
    expansion default `true`, so commenting it out would turn Ollama ON everywhere.
    It is asserted PRESENT below, as the control.
    """

    SHADOWING = (
        "GENESIS_EMBED_PRIORITY_TIER",
        "OLLAMA_URL",
        "LM_STUDIO_URL",
        "LM_STUDIO_HEALTH_URL",
        # Joined the list once the routing loader started resolving it through the
        # accessor; before that, removing it flipped Ollama ON for every install.
        "GENESIS_ENABLE_OLLAMA",
    )

    def _assignments(self, name: str) -> list[str]:
        from genesis.env import repo_root

        template = (repo_root() / "secrets.env.example").read_text()
        return [ln for ln in template.splitlines() if ln.strip().startswith(f"{name}=")]

    @pytest.mark.parametrize("name", SHADOWING)
    def test_yaml_lever_is_not_shadowed(self, name: str):
        assert not self._assignments(name), (
            f"secrets.env.example assigns {name}, which is copied to secrets.env and "
            "beats the genesis.yaml setting of the same name. Comment it out — its "
            f"model_routing.yaml expansion already defaults to the same value. Found: "
            f"{self._assignments(name)}"
        )

    def test_every_commented_lever_still_reaches_routing(self):
        """DELIBERATE REVERSAL of this class's earlier control, recorded not deleted.

        This test used to assert the OPPOSITE for GENESIS_ENABLE_OLLAMA — that it must
        stay ASSIGNED — because `model_routing.yaml` expanded it to `true` when unset,
        so commenting it out turned Ollama on everywhere. The predicate then was
        "template value == expansion default", and that key failed it.

        The predicate changed when the cause did. `routing/config.py` now resolves
        these placeholders through the `genesis.env` accessors, which implement
        env -> yaml -> hardcoded default. So the question is no longer "does the
        literal default match" but "does this key resolve through an accessor" — and
        under that rule every lever here can be commented, which is what finally makes
        setup-local-config.sh's answers reach the router instead of dying in yaml.

        Locking the PROPERTY rather than the instance: with no environment variable
        set, each lever must expand in routing to exactly what its accessor returns.
        An assignment reintroduced into the template would break that, because env
        would win over the accessor's yaml lookup.
        """
        import genesis.env as env_mod
        from genesis.routing.config import _ENV_ACCESSORS, _expand_env_vars

        for key in self.SHADOWING:
            if key not in _ENV_ACCESSORS:
                continue  # value-vs-default levers are covered above
            accessor = getattr(env_mod, _ENV_ACCESSORS[key])
            expected = accessor()
            expected_s = str(expected).lower() if isinstance(expected, bool) else str(expected)
            got = _expand_env_vars(f"x: ${{{key}:-SENTINEL_SHOULD_NOT_APPEAR}}").strip()
            assert got == f"x: {expected_s}", (
                f"{key}: routing expanded to {got!r} but the accessor says {expected_s!r} — "
                "the router and every other consumer disagree about the same setting."
            )

    def test_an_unmapped_placeholder_keeps_the_old_behaviour(self):
        """The control. If the accessor path leaked to every key, this would break."""
        from genesis.routing.config import _expand_env_vars

        assert _expand_env_vars("y: ${SOME_UNMAPPED_VAR:-fallback}").strip() == "y: fallback"


class TestMalformedRootIsAnnounced:
    """Discarding the WHOLE file must not be quieter than discarding one section.

    `_local_section` warns when a section is not a mapping. The root guard did not,
    so an accidental top-level list — which still CONTAINS the operator's settings —
    was dropped in silence, and `_local_section` never got the chance to warn because
    no section survived to be malformed. Before the guard existed the resulting
    exception at least surfaced a memory-bootstrap degradation, so a quiet fallback
    was strictly worse than the crash it replaced.
    """

    @staticmethod
    def _load(tmp_path, monkeypatch, text):
        import genesis.env as env_mod

        cfg = tmp_path / ".genesis" / "config" / "genesis.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text)
        monkeypatch.setattr(env_mod.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", False)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", None)
        return env_mod

    def test_non_mapping_root_logs_a_warning(self, tmp_path, monkeypatch, caplog):
        import logging

        env_mod = self._load(
            tmp_path, monkeypatch, "- memory:\n    embed_priority_tier: false\n"
        )
        with caplog.at_level(logging.WARNING, logger="genesis.env"):
            assert env_mod._local_config() == {}
        assert any("not a mapping" in r.message for r in caplog.records), caplog.text
        assert any("ENTIRE file" in r.message for r in caplog.records), (
            "the warning must say the whole file is ignored, not merely that it is odd"
        )

    def test_a_well_formed_root_logs_nothing(self, tmp_path, monkeypatch, caplog):
        """Control: warning on every load would be noise that hides the signal."""
        import logging

        env_mod = self._load(
            tmp_path, monkeypatch, "memory:\n  embed_priority_tier: false\n"
        )
        with caplog.at_level(logging.WARNING, logger="genesis.env"):
            assert env_mod._local_config() == {"memory": {"embed_priority_tier": False}}
        assert not [r for r in caplog.records if "not a mapping" in r.message]

    def test_an_empty_file_logs_nothing(self, tmp_path, monkeypatch, caplog):
        """`None` from an empty file is ABSENCE, not malformation — stay quiet."""
        import logging

        env_mod = self._load(tmp_path, monkeypatch, "")
        with caplog.at_level(logging.WARNING, logger="genesis.env"):
            assert env_mod._local_config() == {}
        assert not [r for r in caplog.records if "not a mapping" in r.message]


class TestYamlBooleanSpellings:
    """A quoted yaml boolean must mean what it says.

    PyYAML returns a plain STRING for a quoted scalar, so `bool()` reads
    `embed_priority_tier: "false"` as True — the opposite of the operator's intent,
    and on that setting it silently keeps the PAID lane running. The same value
    unquoted parses to a real False, and in secrets.env the env branch reads it
    correctly, so one intention had three spellings and two answers.

    RESTORED after an editing accident deleted this whole class: an index-based
    splice that replaced a region between two anchors removed everything between
    them, this class included. The suite stayed green because the assertions were
    GONE, not because they passed — which is why the equivalence test below
    compares the two code paths against each other rather than against a constant.
    """

    _ACCESSORS = [
        ("ollama_enabled", "network", "ollama_enabled"),
        ("embed_priority_tier", "memory", "embed_priority_tier"),
        ("build_lane_enabled", "build_lane", "enabled"),
        ("models_md_synthesis_enabled", "models_md_synthesis", "enabled"),
    ]

    @pytest.fixture(autouse=True)
    def _no_env_shortcircuit(self, monkeypatch: pytest.MonkeyPatch):
        for name in _SECTION_ACCESSOR_ENV:
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(("accessor", "section", "key"), _ACCESSORS)
    @pytest.mark.parametrize(
        "falsey",
        [
            "false", "False", "FALSE", "no", "off", "0", " false ",
            # EMPTY is false too, and it is listed here because the token-set check
            # alone gets it WRONG: "" is in none of the no-words, so it would read
            # as true — the exact regression this helper nearly introduced while
            # fixing quoted booleans, and on `embed_priority_tier` it would have
            # silently selected the PAID lane. `bool("")` was already correct.
            "", "   ",
        ],
    )
    def test_quoted_false_means_false(
        self, monkeypatch: pytest.MonkeyPatch, accessor, section, key, falsey
    ):
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {section: {key: falsey}})
        assert getattr(env_mod, accessor)() is False, f"{accessor} read {falsey!r} as true"

    @pytest.mark.parametrize(("accessor", "section", "key"), _ACCESSORS)
    @pytest.mark.parametrize("truthy", ["true", "yes", "on", "1"])
    def test_quoted_true_still_means_true(
        self, monkeypatch: pytest.MonkeyPatch, accessor, section, key, truthy
    ):
        """The control: a guard reading EVERYTHING false would pass the test above
        and break every opt-in."""
        import genesis.env as env_mod

        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
        monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {section: {key: truthy}})
        assert getattr(env_mod, accessor)() is True

    @pytest.mark.parametrize(("accessor", "section", "key"), _ACCESSORS)
    def test_native_yaml_booleans_are_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, accessor, section, key
    ):
        import genesis.env as env_mod

        for native, expected in ((False, False), (True, True)):
            monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
            monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {section: {key: native}})
            assert getattr(env_mod, accessor)() is expected

    def test_yaml_and_env_agree_on_every_spelling(self, monkeypatch: pytest.MonkeyPatch):
        """The property that matters: same token, same answer, either home.

        Asserted against the env branch ITSELF rather than a hardcoded expectation,
        so the two cannot drift apart without this failing.
        """
        import genesis.env as env_mod

        for token in ("false", "FALSE", "no", "off", "0", "true", "yes", "on", "1", "x"):
            monkeypatch.setenv("GENESIS_EMBED_PRIORITY_TIER", token)
            via_env = env_mod.embed_priority_tier()
            monkeypatch.delenv("GENESIS_EMBED_PRIORITY_TIER", raising=False)
            monkeypatch.setattr(env_mod, "_LOCAL_CONFIG_LOADED", True)
            monkeypatch.setattr(env_mod, "_LOCAL_CONFIG", {"memory": {"embed_priority_tier": token}})
            assert via_env == env_mod.embed_priority_tier(), f"{token!r} disagrees"


class TestNestedConfigReadsAreRouted:
    """Structural enforcement — the hand-maintained list above cannot do this."""

    def test_no_unrouted_nested_config_read(self):
        """Every nested local-config read must go through `_local_section`.

        A chained `_local_config().get(x).get(y)` — or the `(… or {}).get(y)` variant —
        is the exact shape that raises on a malformed section. Caught here as a source
        property rather than per-accessor, so an accessor added later cannot reintroduce
        the class by simply not being listed.
        """
        import ast

        from genesis.env import repo_root

        tree = ast.parse((repo_root() / "src" / "genesis" / "env.py").read_text())
        def _is_get(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
            )

        def _reads_config(node: ast.AST) -> bool:
            return any(
                isinstance(n, ast.Name) and n.id == "_local_config" for n in ast.walk(node)
            )

        # Flag a CHAINED get — `x.get(a).get(b)` — or the `(x or {}).get(b)` guard
        # form, and only when the chain actually reads the local config. A single
        # `_local_config().get(key)` is a top-level read and is safe (the root is
        # normalized), and `_local_section(name).get(key)` is the routed form; both
        # must stay green or this test would forbid its own fix.
        bad = [
            (node.lineno, ast.unparse(node))
            for node in ast.walk(tree)
            if _is_get(node)
            and (_is_get(node.func.value) or isinstance(node.func.value, ast.BoolOp))
            and _reads_config(node.func.value)
        ]
        assert not bad, (
            "nested local-config read bypassing _local_section (it will raise on a "
            f"section that is not a mapping): {bad}"
        )
