"""Claude Code settings that configure the CLIENT and cannot ship project-level.

Background. CC's transcript-retention sweep walks EVERY directory under
``~/.claude/projects`` but takes its retention value from the settings of
whichever session happens to run it. A session launched outside this repo merges
no project settings, falls back to CC's 30-day default, and deletes transcripts
belonging to every other project. The repo shipped ``cleanupPeriodDays: 180`` in
its own ``.claude/settings.json`` for 73 days; it only ever bound repo-launched
sessions, and 198 sessions were deleted anyway.

So these settings are seeded into the user-level ``~/.claude/settings.json`` by
three paths, all of which must agree:

* ``scripts/install.sh``      — container fresh install (it does NOT call
  bootstrap; verified by grep — every mention there is a comment);
* ``scripts/setup_claude_config.py`` — run by bootstrap, which ``update.sh``
  re-runs, so it is the path that reaches installs that already exist;
* ``scripts/host-setup.sh``   — the host VM, which has no venv and no bootstrap.

The two shell paths run before any venv exists, so they hardcode the value;
``test_shell_paths_match_manifest`` is what stops them drifting from the
manifest. Both shell blocks are extracted from the REAL shipped scripts rather
than copied, so the logic under test is what actually runs.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"
MANIFEST = REPO_ROOT / "config" / "cc-global-settings.yaml"
PROJECT_SETTINGS = REPO_ROOT / ".claude" / "settings.json"

VALID_POLICIES = {"floor", "set_if_absent"}

# CC 2.1.246's own list of keys that configure the CLIENT rather than a project
# (read from the shipped binary). Any of these in the repo's project settings is
# the bug this module exists to prevent.
CC_CLIENT_SCOPE_KEYS = frozenset(
    {
        "model",
        "outputStyle",
        "language",
        "effortLevel",
        "fastMode",
        "alwaysThinkingEnabled",
        "spinnerTipsEnabled",
        "prefersReducedMotion",
        "promptSuggestionEnabled",
        "awaySummaryEnabled",
        "precomputeCompactionEnabled",
        "switchModelsOnFlag",
        "autoUpdatesChannel",
        "viewMode",
        "syntaxHighlightingDisabled",
        "useAutoModeDuringPlan",
        "enableWorkflows",
        "disableWorkflows",
        "disableArtifact",
        "enableArtifact",
        "workflowKeywordTriggerEnabled",
        "respondToBashCommands",
        "autoCompactWindow",
        "cleanupPeriodDays",
        "forceLoginMethod",
        "keybindingFlavor",
    }
)


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text()) or {}


def _defaults() -> dict:
    return _manifest()["user_level_defaults"]


def _retention_floor() -> int:
    return _defaults()["cleanupPeriodDays"]["value"]


@pytest.fixture()
def setup_mod():
    """scripts/setup_claude_config.py is a script, not a package — load by path."""
    path = REPO_ROOT / "scripts" / "setup_claude_config.py"
    spec = importlib.util.spec_from_file_location("genesis_setup_cc_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENESIS_CC_RETENTION_DAYS", raising=False)
    return home


def _settings_file(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _settings(home: Path) -> dict:
    return json.loads(_settings_file(home).read_text())


# ------------------------------------------------------------- the manifest --


def test_every_manifest_entry_declares_a_known_policy():
    """A key with no policy (or a typo'd one) would silently never apply."""
    for key, entry in _defaults().items():
        assert isinstance(entry, dict), f"{key} must be a mapping"
        assert entry.get("policy") in VALID_POLICIES, f"{key}: bad policy"
        if entry["policy"] == "floor":
            assert isinstance(entry["value"], int) and not isinstance(entry["value"], bool)
            assert entry["value"] >= 1, "CC rejects cleanupPeriodDays < 1"


def test_retention_is_a_floor_not_set_if_absent():
    """Retention is a data-loss floor: an install with a too-low value must heal."""
    assert _defaults()["cleanupPeriodDays"]["policy"] == "floor"


# ------------------------------------------------- the container applier -----


def test_seeds_when_settings_file_absent(setup_mod, fake_home):
    _settings_file(fake_home).unlink(missing_ok=True)
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    out = _settings(fake_home)
    assert out["cleanupPeriodDays"] == _retention_floor()
    assert out["attribution"] == _defaults()["attribution"]["value"]


def test_merges_without_clobbering_other_keys(setup_mod, fake_home):
    _settings_file(fake_home).write_text(json.dumps({"model": "sonnet", "env": {"X": "1"}}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    out = _settings(fake_home)
    assert out["model"] == "sonnet"
    assert out["env"] == {"X": "1"}
    assert out["cleanupPeriodDays"] == _retention_floor()


def test_floor_raises_a_too_low_value(setup_mod, fake_home):
    """The healing case: the exact silent-no-op the original bug was made of."""
    _settings_file(fake_home).write_text(json.dumps({"cleanupPeriodDays": 30}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["cleanupPeriodDays"] == _retention_floor()


def test_floor_keeps_a_higher_operator_value(setup_mod, fake_home):
    high = _retention_floor() + 1000
    _settings_file(fake_home).write_text(json.dumps({"cleanupPeriodDays": high}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["cleanupPeriodDays"] == high


def test_env_override_actually_lowers_an_existing_higher_value(setup_mod, fake_home, monkeypatch):
    """The documented lever must DO something.

    It is advertised in the manifest, the CHANGELOG and cc-compatibility.md as
    the way to choose a lower value. As a mere lower floor it would be inert
    against an existing higher value — a documented setting that silently does
    nothing, which is the exact bug this whole module exists to fix.
    """
    monkeypatch.setenv("GENESIS_CC_RETENTION_DAYS", "30")
    _settings_file(fake_home).write_text(json.dumps({"cleanupPeriodDays": 365}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["cleanupPeriodDays"] == 30


@pytest.mark.parametrize("bad", ["0", "-9999"])
def test_env_override_refuses_values_below_one(setup_mod, fake_home, monkeypatch, bad):
    """Fail CLOSED on the one key this module exists to keep safe.

    CC's schema rejects `cleanupPeriodDays < 1`, and a negative would put the
    sweep's cutoff in the FUTURE — older than everything on disk.
    """
    monkeypatch.setenv("GENESIS_CC_RETENTION_DAYS", bad)
    _settings_file(fake_home).unlink(missing_ok=True)
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_atomic_write_preserves_file_mode(setup_mod, fake_home, mode):
    """os.replace installs a new inode; ~/.claude/settings.json can hold API keys.

    The modes here are deliberately NOT 0600: `tempfile.mkstemp` already creates
    at 0600, so a 0600 target passes even with the mode-carrying code deleted.
    Verified by mutation — the earlier single-0600 version of this test was blind.
    """
    target = _settings_file(fake_home)
    target.write_text(json.dumps({"cleanupPeriodDays": 1}))
    os.chmod(target, mode)
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert target.stat().st_mode & 0o777 == mode


def test_set_if_absent_without_a_value_writes_nothing(setup_mod):
    """A manifest typo must not push a null into every install's CC settings."""
    settings: dict = {}
    changes = setup_mod._apply_user_level_defaults(
        settings, {"newKey": {"policy": "set_if_absent"}}
    )
    assert settings == {}
    assert changes == []


def test_set_if_absent_never_overwrites(setup_mod, fake_home):
    mine = {"commit": "mine", "pr": "mine", "sessionUrl": True}
    _settings_file(fake_home).write_text(json.dumps({"attribution": mine}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["attribution"] == mine


def test_corrupt_settings_file_is_left_alone(setup_mod, fake_home, capsys):
    target = _settings_file(fake_home)
    target.write_text("{ not json")
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert target.read_text() == "{ not json"
    assert "WARNING" in capsys.readouterr().out


def test_dry_run_writes_nothing(setup_mod, fake_home):
    _settings_file(fake_home).unlink(missing_ok=True)
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=True)
    assert not _settings_file(fake_home).exists()


def test_missing_manifest_warns_and_returns(setup_mod, fake_home, capsys):
    """Must hit the GUARD, not the outer handler — both print 'WARNING'.

    Asserting only on the word was blind: deleting the manifest_path.exists()
    guard makes yaml raise FileNotFoundError, the broad `except Exception`
    catches it, and a different WARNING satisfies the assertion. Caught by
    mutation; the negative assertion is what distinguishes the two paths.
    """
    setup_mod.ensure_user_cc_defaults(Path("/nonexistent/genesis/root"), dry_run=False)
    out = capsys.readouterr().out
    assert "settings manifest not found" in out
    assert "could not apply user-level CC defaults" not in out


def test_floor_heals_an_explicit_null(setup_mod, fake_home):
    """CC resolves `cleanupPeriodDays: null` via `?? 30` — a null IS the bug.

    An absent key and an explicit null must therefore behave identically. They
    did not: the sentinel distinguished them and this path left null alone while
    both shell writers healed it.
    """
    _settings_file(fake_home).write_text(json.dumps({"cleanupPeriodDays": None}))
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)
    assert _settings(fake_home)["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize("raw", ["[]", "null", '"a string"', "42"])
def test_valid_json_that_is_not_an_object_is_refused(setup_mod, fake_home, raw):
    """Reached by configure_global_settings too, which has no wrapper."""
    target = _settings_file(fake_home)
    target.write_text(raw)
    assert setup_mod._load_user_settings(target) is None
    assert target.read_text() == raw


def test_write_preserves_a_symlinked_target(setup_mod, fake_home):
    """A dotfiles-managed settings.json is commonly a symlink.

    Writing by temp-file + rename replaced the LINK with a regular file, leaving
    the managed copy stale — the source of truth forks silently and the next
    `stow`/`chezmoi` restores the old value. In-place truncation keeps the link.
    """
    real = fake_home / "dotfiles-settings.json"
    real.write_text(json.dumps({"cleanupPeriodDays": 30}))
    link = _settings_file(fake_home)
    link.unlink(missing_ok=True)
    link.symlink_to(real)

    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)

    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert json.loads(real.read_text())["cleanupPeriodDays"] == _retention_floor()


def test_never_raises_when_the_write_fails(setup_mod, fake_home, monkeypatch):
    """Exercise the handler itself, not a guard clause in front of it.

    bootstrap.sh:595 calls this script BARE under `set -euo pipefail`, from
    update.sh's `set -Eeuo pipefail` with an armed ERR trap — so any exception
    escalates into a full deploy rollback, reported as a JSON error rather than a
    settings problem. The earlier version of this test hit the
    manifest-not-found guard and would have passed with the try/except deleted.
    """

    def boom(*_args, **_kwargs):
        raise OSError("ENOSPC")

    monkeypatch.setattr(setup_mod, "_write_json", boom)
    _settings_file(fake_home).unlink(missing_ok=True)
    setup_mod.ensure_user_cc_defaults(REPO_ROOT, dry_run=False)  # must not raise


def test_loader_handles_a_binary_corrupt_settings_file(setup_mod, fake_home, capsys):
    """UnicodeDecodeError is a ValueError, NOT a JSONDecodeError.

    Tested against `_load_user_settings` DIRECTLY, not through
    ensure_user_cc_defaults: that caller's broad `except Exception` absorbs the
    difference, so routing through it passes whichever exception tuple the loader
    catches. Verified by mutation — the wrapper-level version was blind. The
    loader is also reached by `configure_global_settings`, which has NO wrapper,
    so getting it right here is what keeps `--global` from tracebacking.
    """
    target = _settings_file(fake_home)
    target.write_bytes(b"\xff\xfe\x00garbage")
    assert setup_mod._load_user_settings(target) is None
    assert "WARNING" in capsys.readouterr().out
    assert target.read_bytes() == b"\xff\xfe\x00garbage"


# ------------------------------------------------------- the shell paths -----


def _extract_python(script: Path, marker: str) -> str:
    """The python heredoc body from a marked block in a real shipped script."""
    text = script.read_text()
    begin, end = f"# BEGIN {marker}", f"# END {marker}"
    assert begin in text and end in text, f"{marker} markers missing in {script.name}"
    block = text.split(begin, 1)[1].split(end, 1)[0]
    assert "<<'PYEOF'" in block, f"{script.name} block no longer uses a PYEOF heredoc"
    # Everything after the heredoc's own line (which carries redirections).
    body = block.split("<<'PYEOF'", 1)[1].split("\n", 1)[1].split("PYEOF", 1)[0]
    # Guard the guard: a mis-extraction yielding non-python would make every
    # test below error in a way that reads like a production bug.
    compile(body, str(script), "exec")
    return body


def _run(
    script: Path, marker: str, target: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _extract_python(script, marker), str(target)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})},
    )


SHELL_PATHS = [
    pytest.param(INSTALL_SH, "cc-user-settings", id="install.sh"),
    pytest.param(HOST_SETUP, "host-cc-user-settings", id="host-setup.sh"),
]


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_match_manifest(script, marker, tmp_path):
    """The two pre-venv paths hardcode the floor; this is the anti-drift lock."""
    target = tmp_path / "settings.json"
    target.write_text("{}")
    assert _run(script, marker, target).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_raise_a_too_low_value(script, marker, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cleanupPeriodDays": 30}))
    assert _run(script, marker, target).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_keep_a_higher_value(script, marker, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cleanupPeriodDays": 9999}))
    assert _run(script, marker, target).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == 9999


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_still_force_updater_keys(script, marker, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"KEEP": "yes"}}))
    assert _run(script, marker, target).returncode == 0
    env = json.loads(target.read_text())["env"]
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["DISABLE_UPDATES"] == "1"
    assert env["KEEP"] == "yes"


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_implement_every_manifest_key(script, marker, tmp_path):
    """The manifest is only the source of truth if the shell paths track ALL of it.

    A new `user_level_defaults` key that only the container-bootstrap path
    implements would apply on updates but never on a fresh install or the host —
    the identical shape of the original bug.
    """
    target = tmp_path / "settings.json"
    target.write_text("{}")
    assert _run(script, marker, target).returncode == 0
    out = json.loads(target.read_text())
    for key, entry in _defaults().items():
        assert key in out, f"{script.name} does not implement manifest key {key!r}"
        assert out[key] == entry["value"], f"{script.name}: {key!r} drifted from the manifest"


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_never_overwrite_attribution(script, marker, tmp_path):
    mine = {"commit": "mine", "pr": "mine", "sessionUrl": True}
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"attribution": mine}))
    assert _run(script, marker, target).returncode == 0
    assert json.loads(target.read_text())["attribution"] == mine


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_honour_the_env_override(script, marker, tmp_path):
    """All three write paths must agree on the operator's lever, not just one."""
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cleanupPeriodDays": 9999}))
    assert _run(script, marker, target, env={"GENESIS_CC_RETENTION_DAYS": "30"}).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == 30


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_refuse_env_override_below_one(script, marker, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{}")
    assert _run(script, marker, target, env={"GENESIS_CC_RETENTION_DAYS": "0"}).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize("mode", [0o600, 0o640])
@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_preserve_file_mode(script, marker, mode, tmp_path):
    """Two modes, because a single value can be the one the writer produces anyway."""
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cleanupPeriodDays": 1}))
    os.chmod(target, mode)
    assert _run(script, marker, target).returncode == 0
    assert target.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_heal_an_explicit_null(script, marker, tmp_path):
    """Parity with the python path — CC reads `null` as its 30-day default."""
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cleanupPeriodDays": None}))
    assert _run(script, marker, target).returncode == 0
    assert json.loads(target.read_text())["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_preserve_a_symlinked_target(script, marker, tmp_path):
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(json.dumps({"cleanupPeriodDays": 30}))
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert _run(script, marker, link).returncode == 0
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert json.loads(real.read_text())["cleanupPeriodDays"] == _retention_floor()


@pytest.mark.parametrize("raw", ["[]", "null", "42"])
@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_refuse_a_non_object_file(script, marker, raw, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(raw)
    assert _run(script, marker, target).returncode == 2
    assert target.read_text() == raw


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_leave_no_temp_file_behind(script, marker, tmp_path):
    """In-place writing means there is no temp file to leak or strand."""
    target = tmp_path / "settings.json"
    target.write_text("{}")
    assert _run(script, marker, target).returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_paths_refuse_an_unparseable_file(script, marker, tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{ not json")
    assert _run(script, marker, target).returncode == 2
    assert target.read_text() == "{ not json"


@pytest.mark.parametrize(("script", "marker"), SHELL_PATHS)
def test_shell_fresh_file_literal_matches_manifest(script, marker):
    """The `[ ! -f ]` branch writes a JSON literal — it drifts too."""
    text = script.read_text()
    block = text.split(f"# BEGIN {marker}", 1)[1].split(f"# END {marker}", 1)[0]
    literal = block.split("<<'CCSETTINGS'", 1)[1].split("CCSETTINGS", 1)[0]
    assert json.loads(literal)["cleanupPeriodDays"] == _retention_floor()


# -------------------------------------------------------------- the class ---


def test_project_settings_carry_no_client_scope_keys():
    """Lock the whole class, not just the key that bit us.

    A CC client-scope setting in the repo's project settings binds only sessions
    launched inside the repo, while the behaviour it controls is machine-wide —
    so it silently does nothing for every other session. That is exactly how
    ``cleanupPeriodDays: 180`` looked correct for 73 days while transcripts were
    being deleted at CC's 30-day default.
    """
    settings = json.loads(PROJECT_SETTINGS.read_text())
    offenders = sorted(CC_CLIENT_SCOPE_KEYS & set(settings))
    assert not offenders, (
        f"{offenders} configure the CC client globally and must be seeded into "
        f"~/.claude/settings.json via config/cc-global-settings.yaml, not shipped "
        f"in .claude/settings.json"
    )
