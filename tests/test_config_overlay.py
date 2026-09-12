"""WS-14: config overlays resolve user-dir-first (``~/.genesis/config/``).

The dashboard/MCP writers land ``.local.yaml`` overlays in ``~/.genesis/config/``,
but subsystem loaders historically read them from the repo-relative sibling — so
dashboard settings changes were silently ignored (cfg-001). ``merge_local_overlay``
and ``local_overlay_mtime`` now check the user dir first, falling back to the
repo-relative sibling for back-compat.

Every test monkeypatches ``_user_config_dir`` so it never touches the real
``~/.genesis/config/`` on the dev machine.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from genesis import _config_overlay

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "genesis"
_HOME_CONFIG_RE = re.compile(r'Path\.home\(\)\s*/\s*["\']\.genesis["\']\s*/\s*["\']config["\']')


@pytest.fixture(autouse=True)
def _reset_overlay_warn_cache():
    """Isolate the module-global warn-dedupe map.

    ``_WARNED_OVERLAYS`` persists for the process, so without this one test's
    warning suppresses another's and the suite passes for the wrong reason.
    """
    from genesis import _config_overlay as ov

    ov._WARNED_OVERLAYS.clear()
    yield
    ov._WARNED_OVERLAYS.clear()


def test_merge_reads_user_dir_overlay_first(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)

    # Base file lives in a *different* (repo-like) dir with no sibling overlay.
    repo_dir = tmp_path / "repo_config"
    repo_dir.mkdir()
    base_path = repo_dir / "foo.yaml"
    base_path.write_text("a: 1\nb: 2\n")
    (user_dir / "foo.local.yaml").write_text("b: 99\n")

    merged = _config_overlay.merge_local_overlay({"a": 1, "b": 2}, base_path)
    assert merged == {"a": 1, "b": 99}  # picked up the user-dir overlay


def test_merge_falls_back_to_repo_sibling(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()  # exists, but has no overlay for foo
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)

    repo_dir = tmp_path / "repo_config"
    repo_dir.mkdir()
    base_path = repo_dir / "foo.yaml"
    (repo_dir / "foo.local.yaml").write_text("b: 42\n")

    merged = _config_overlay.merge_local_overlay({"a": 1, "b": 2}, base_path)
    assert merged == {"a": 1, "b": 42}  # back-compat: repo-relative sibling


def test_merge_no_overlay_returns_base(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)
    base_path = tmp_path / "foo.yaml"
    base = {"a": 1}
    assert _config_overlay.merge_local_overlay(base, base_path) == base


def test_local_overlay_mtime_prefers_user_dir(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)
    repo_dir = tmp_path / "repo_config"
    repo_dir.mkdir()
    base_path = repo_dir / "foo.yaml"
    overlay = user_dir / "foo.local.yaml"
    overlay.write_text("b: 1\n")
    assert _config_overlay.local_overlay_mtime(base_path) == overlay.stat().st_mtime


def test_mtime_zero_when_no_overlay(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)
    base_path = tmp_path / "foo.yaml"
    assert _config_overlay.local_overlay_mtime(base_path) == 0.0


# ── Suite-wide isolation guards ──────────────────────────────────────────────
# config/*.local.yaml overlays are install-local state (e.g. a voice-live
# install arms voice_act.local.yaml `mode: live`). Without suite-wide isolation,
# any of the 30+ config loaders' tests silently read the REAL install's
# overlays — green on CI, red (or falsely green) on a live install. The autouse
# ``_isolate_user_config_dir`` fixture in tests/conftest.py neutralizes both the
# user-dir and repo-sibling vectors of ``merge_local_overlay``. These guards
# keep that isolation honest as the codebase grows:
#   1. every module-level user-config-dir binding is patched or allow-listed,
#   2. no NEW hand-rolled ``.local.yaml`` resolver slips in unrouted+unlisted,
#   3. the repo-sibling neutralizer actually fires where a sibling exists.


def test_suite_isolates_user_config_dir_from_real_home():
    """The autouse conftest fixture must be active for every test."""
    from genesis.security import immunity

    real = Path.home() / ".genesis" / "config"
    assert _config_overlay._user_config_dir() != real
    assert immunity._user_config_dir() != real
    # immunity holds its OWN module-level _resolve_overlay_path binding, so the
    # fixture must patch that copy too (else record_demotion() falls back to the
    # real config/ws3_immunity.local.yaml sibling). Both must point at the same
    # sandboxed resolver.
    assert immunity._resolve_overlay_path is _config_overlay._resolve_overlay_path


def test_isolation_survives_a_test_calling_monkeypatch_undo(monkeypatch):
    """A test that calls ``monkeypatch.undo()`` mid-body (e.g. test_learned_knobs)
    must NOT revert the suite-isolation patch — the fixture owns its own
    MonkeyPatch instance, independent of the shared ``monkeypatch`` fixture."""
    real = Path.home() / ".genesis" / "config"
    monkeypatch.setattr("os.environ", dict(__import__("os").environ))  # any patch
    monkeypatch.undo()  # reverts THIS test's patches — must not touch the fixture's
    assert _config_overlay._user_config_dir() != real


def test_module_level_user_config_bindings_are_patched_or_listed():
    """Enumerate every module-level binding of a config-overlay seam in src.

    A module that binds ``_user_config_dir`` OR ``_resolve_overlay_path`` (an
    alias import from ``genesis._config_overlay``) OR assigns a module-level
    constant to ``Path.home()/".genesis"/"config"`` holds its OWN reference —
    patching ``genesis._config_overlay`` does not reach it, so the autouse
    fixture must patch that module's copy too. (Lazy, function-local imports
    re-resolve against the patched ``_config_overlay`` on each call and are
    fine — hence module-level only.) Each such module must be accounted for in
    exactly one bucket below.
    """
    # Patched directly by the autouse _isolate_user_config_dir fixture
    # (both _user_config_dir AND _resolve_overlay_path).
    patched_in_conftest = {"security/immunity.py"}
    # Own ``_USER_CONFIG_DIR`` + resolver; import-heavy (FastMCP registry at
    # module level), so excluded from the every-test fixture — their own tests
    # patch _USER_CONFIG_DIR (tests/test_mcp/test_settings.py, test_recon_*.py).
    self_isolated = {"mcp/health/settings.py", "mcp/recon_mcp.py"}
    # Whole-file config paths (``~/.genesis/config/<name>.yaml``), NOT
    # ``.local.yaml`` overlays — a distinct mechanism. No test invokes these
    # loaders with zero args today (verified), so they can't leak yet; listed
    # so the net SEES them and a future zero-arg caller trips this guard.
    whole_file_config = {
        "distribution/config.py",
        "ego/config.py",
        "outreach/config.py",
        "pipeline/profiles.py",
        "mcp/health/module_ops.py",
        "runtime/init/modules.py",
    }
    accounted = patched_in_conftest | self_isolated | whole_file_config

    found: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == "_config_overlay.py":
            continue  # the canonical definition itself
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in tree.body:  # module level only — lazy imports are fine
            is_alias_import = (
                isinstance(node, ast.ImportFrom)
                and node.module == "genesis._config_overlay"
                and any(a.name in ("_user_config_dir", "_resolve_overlay_path") for a in node.names)
            )
            is_home_config_const = isinstance(node, ast.Assign) and bool(
                _HOME_CONFIG_RE.search(ast.get_source_segment(text, node) or "")
            )
            if is_alias_import or is_home_config_const:
                found.add(rel)

    unaccounted = found - accounted
    assert not unaccounted, (
        f"New module-level user-config-dir binding(s) in {sorted(unaccounted)}: "
        "add to the _isolate_user_config_dir fixture (tests/conftest.py), or — "
        "if import-heavy / a whole-file config — to the matching allow-list here."
    )
    stale = accounted - found
    assert not stale, f"Stale allow-list entries (binding removed from src): {sorted(stale)}"


def test_no_unisolated_local_yaml_resolver():
    """Catch any NEW ``.local.yaml`` resolver that reads install-local overlays
    without a test seam.

    Scans EVERY file for a non-docstring ``.local.yaml`` string constant (no
    per-file ``merge_local_overlay`` skip — that would hide a file that both
    routes some configs AND hand-rolls a leaky resolver, per review). Every hit
    must land in exactly one conscious bucket; anything else is a regression.
    """
    # Hand-rolled resolvers that read a real overlay sibling WITHOUT the shared
    # seam. Independent of merge_local_overlay by design/history; tracked for
    # consolidation (follow-up e2fd22c5). NOT patched by the autouse fixture on
    # purpose: routing.config alone costs ~4.3s to import (litellm etc.), so
    # pulling these into an every-test fixture is prohibitive — consolidation
    # onto the shared resolver is the correct fix, not a per-test patch. Until
    # then, their OWN tests that load a real repo path may still merge host
    # values (a falsely-green risk, not a hard failure).
    independent_resolvers = {
        "guardian/config.py",  # provisioning.local.yaml (guardian state dir)
        "mcp/health/settings.py",  # own _load_yaml_local + _USER_CONFIG_DIR
        "recon/github_discovery.py",  # discovery topics overlay
        "recon/watchlist.py",  # recon_watchlist.local.yaml
        "routing/config.py",  # model_routing.local.yaml (load-bearing)
    }
    # Build a ``.local.yaml`` filename but resolve it through the PATCHED seam
    # (_user_config_dir / _resolve_overlay_path), so they ARE isolated by the
    # fixture — the string constant is just a filename passed to the seam.
    shared_seam_users = {
        "security/immunity.py",  # _user_config_dir()/ws3_immunity.local.yaml + _resolve_overlay_path
        "ledger/learned_knobs.py",  # _user_config_dir()/<stem>.local.yaml (user-dir-always)
    }
    # Files that merely MENTION ".local.yaml" in a message/help string.
    # cc/conversation.py: a WARNING emitted when CC failover finds no usable
    # roster peer, telling the operator where to declare one. It reads the
    # roster through genesis.cc.roster (which routes via merge_local_overlay);
    # the path appears only in the human-facing message text.
    prose_mentions = {"mcp/health/reflex_status.py", "cc/conversation.py"}
    accounted = independent_resolvers | shared_seam_users | prose_mentions

    found: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == "_config_overlay.py":
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docstring_const_ids = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(getattr(n, "body", None), list)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ".local.yaml" in node.value
                and id(node) not in docstring_const_ids
            ):
                found.add(rel)
                break

    unaccounted = found - accounted
    assert not unaccounted, (
        f"New unrouted .local.yaml resolver(s) in {sorted(unaccounted)}: route "
        "through genesis._config_overlay.merge_local_overlay (preferred, gets "
        "test isolation for free), or add to the allow-list here with rationale."
    )
    stale = accounted - found
    assert not stale, f"Stale allow-list entries (resolver removed from src): {sorted(stale)}"


def test_repo_sibling_overlay_is_neutralized(tmp_path, monkeypatch):
    """Finding-1 regression (deterministic, CI-exercised): a loader given a repo
    config path must NOT read the install-local ``<repo>/config/*.local.yaml``
    sibling.

    Points ``GENESIS_REPO_ROOT`` at a synthetic tree with a planted overlay, so
    the check runs everywhere (no dependence on host state / pre-existing
    overlays). The fixture's ``_sandboxed_resolve`` resolves ``repo_root()``
    lazily, so this re-pointing is honored. Verified-RED by disabling the
    wrapper: the planted overlay's keys then leak into ``merged``.
    """
    fake_repo = tmp_path / "fake_repo"
    cfg_dir = fake_repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "widget.yaml").write_text("base_key: 1\n")
    (cfg_dir / "widget.local.yaml").write_text("leaked_overlay_key: 999\n")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))

    sentinel = {"base_key": 1}
    merged = _config_overlay.merge_local_overlay(dict(sentinel), cfg_dir / "widget.yaml")
    assert merged == sentinel, (
        "repo-relative overlay leaked into a test merge — the _sandboxed_resolve "
        "neutralizer in conftest is not covering it"
    )


def test_repo_sibling_overlay_is_neutralized_on_real_tree():
    """Same regression against the REAL repo config dir when it has overlays
    (main tree). Complements the synthetic test above with a real-file check;
    skips on a fresh worktree / CI where the gitignored overlays are absent."""
    from genesis.env import repo_root

    cfg_dir = repo_root() / "config"
    siblings = sorted(cfg_dir.glob("*.local.yaml"))
    if not siblings:
        pytest.skip("no repo-relative overlay present in this tree")
    sib = siblings[0]
    base_path = cfg_dir / sib.name.replace(".local.yaml", ".yaml")

    sentinel = {"__sentinel_base_only__": True}
    merged = _config_overlay.merge_local_overlay(dict(sentinel), base_path)
    assert merged == sentinel, (
        f"repo-relative overlay {sib.name} leaked into a test merge — the "
        "_sandboxed_resolve neutralizer in conftest is not covering it"
    )


def test_broken_overlay_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    """A malformed overlay must FALL BACK LOUDLY, never silently.

    Regression guard for a silent-degradation path: `merge_local_overlay`
    swallowed every parse error and returned `base` with no log line, so a
    broken overlay was indistinguishable from a clean load. That is
    load-bearing wherever the overlay is the SOLE home of a setting —
    cc_roster peers, for one, where a single YAML typo yields "no fallback
    peer configured" and the discovery moment is the subscription cap.
    """
    import logging

    from genesis import _config_overlay as ov

    base_path = tmp_path / "cc_roster.yaml"
    base_path.write_text("default: claude\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "cc_roster.local.yaml").write_text("models:\n  bad: [unclosed\n")
    monkeypatch.setattr(ov, "_user_config_dir", lambda: user_dir)

    base = {"default": "claude", "models": {"claude": {"native_subscription": True}}}
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        out = ov.merge_local_overlay(base, base_path)

    assert out == base  # fell back
    assert caplog.records, "a broken overlay must not fail silently"
    msg = caplog.records[0].getMessage()
    assert "cc_roster.local.yaml" in msg  # names the offending file
    assert "NOT in effect" in msg  # states the consequence


# ── wrong ROOT SHAPE: valid YAML, not a mapping ──────────────────────────────
# Codex/CodeRabbit review of the roster-portability PR: `_deep_merge` calls
# `overlay.items()`, so a list or scalar root raised AttributeError from OUTSIDE
# merge_local_overlay — propagating to every caller of the config loader instead
# of degrading to base like every other malformed case.


@pytest.mark.parametrize(
    ("body", "shape"),
    [("- one\n- two\n", "list"), ("just-a-string\n", "str"), ("42\n", "int")],
)
def test_non_mapping_overlay_root_falls_back_and_warns(tmp_path, monkeypatch, caplog, body, shape):
    import logging

    from genesis import _config_overlay as ov

    base_path = tmp_path / "cc_roster.yaml"
    base_path.write_text("default: claude\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "cc_roster.local.yaml").write_text(body)
    monkeypatch.setattr(ov, "_user_config_dir", lambda: user_dir)

    base = {"default": "claude", "models": {"claude": {"native_subscription": True}}}
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        out = ov.merge_local_overlay(base, base_path)  # must not raise

    assert out == base  # fell back rather than exploding
    assert caplog.records, "a wrong-shape overlay must not fail silently"
    msg = caplog.records[0].getMessage()
    assert "cc_roster.local.yaml" in msg
    assert shape in msg  # names the offending shape
    assert "NOT in effect" in msg


def test_overlay_warning_is_deduped_per_file_version(tmp_path, monkeypatch, caplog):
    """Some loaders re-read the overlay on EVERY call (the immunity gate reloads
    per gate check), so an undeduped warning turns one YAML typo into a traceback
    on every memory or approval operation. Warn once per file VERSION — and warn
    again once the file changes, so a failed repair is still visible."""
    import logging
    import os

    from genesis import _config_overlay as ov

    base_path = tmp_path / "cc_roster.yaml"
    base_path.write_text("default: claude\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    local = user_dir / "cc_roster.local.yaml"
    local.write_text("models:\n  bad: [unclosed\n")
    monkeypatch.setattr(ov, "_user_config_dir", lambda: user_dir)

    base = {"default": "claude"}
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        for _ in range(5):
            assert ov.merge_local_overlay(base, base_path) == base
    assert len(caplog.records) == 1, "flooded the journal on a hot-path loader"

    # A NEW bad version must warn again — a silent second failure would hide a
    # botched repair.
    caplog.clear()
    local.write_text("models:\n  worse: {also-unclosed\n")
    os.utime(local, (0, 0))  # force a distinct mtime
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        assert ov.merge_local_overlay(base, base_path) == base
    assert len(caplog.records) == 1, "a changed bad file must warn again"


@pytest.mark.parametrize(
    ("body", "shape"),
    [("[]\n", "list"), ("false\n", "bool"), ("0\n", "int"), ('""\n', "str")],
)
def test_falsy_non_mapping_root_still_warns(tmp_path, monkeypatch, caplog, body, shape):
    """A FALSY non-mapping root used to be swallowed by an `or {}` default — no
    warning, every override silently dropped. An empty-list root is exactly the
    shape this guard advertises catching, so the non-empty cases above were not
    enough to prove it."""
    import logging

    from genesis import _config_overlay as ov

    base_path = tmp_path / "cc_roster.yaml"
    base_path.write_text("default: claude\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "cc_roster.local.yaml").write_text(body)
    monkeypatch.setattr(ov, "_user_config_dir", lambda: user_dir)

    base = {"default": "claude"}
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        assert ov.merge_local_overlay(base, base_path) == base
    assert caplog.records, f"a {shape} root was dropped silently"
    assert shape in caplog.records[0].getMessage()


@pytest.mark.parametrize("body", ["", "\n", "null\n"])
def test_empty_or_null_overlay_is_not_an_error(tmp_path, monkeypatch, caplog, body):
    """An empty file or explicit `null` legitimately has nothing to merge, and
    must NOT be reported as malformed — otherwise a freshly-created overlay
    warns on every read."""
    import logging

    from genesis import _config_overlay as ov

    base_path = tmp_path / "cc_roster.yaml"
    base_path.write_text("default: claude\n")
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "cc_roster.local.yaml").write_text(body)
    monkeypatch.setattr(ov, "_user_config_dir", lambda: user_dir)

    base = {"default": "claude"}
    with caplog.at_level(logging.WARNING, logger="genesis._config_overlay"):
        assert ov.merge_local_overlay(base, base_path) == base
    assert not caplog.records, "an empty overlay is not a malformed one"
