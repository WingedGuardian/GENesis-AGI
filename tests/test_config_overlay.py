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

from genesis import _config_overlay


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
