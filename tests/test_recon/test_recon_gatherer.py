"""Tests for the recon watchlist path resolution.

The watchlist path now lives in the shared ``recon.watchlist`` store (the
gatherer/MCP loaders delegate to it), so the path checks point there.
"""

from __future__ import annotations

from genesis.recon.watchlist import WATCHLIST_PATH


class TestWatchlistPath:
    """Verify the watchlist path resolves correctly."""

    def test_config_dir_points_to_project_root(self) -> None:
        """The watchlist lives in <project_root>/config, not src/config."""
        config_dir = WATCHLIST_PATH.parent
        assert config_dir.name == "config"
        # The parent of config/ should contain src/ (i.e., it's the project root)
        assert (config_dir.parent / "src").is_dir(), (
            f"config dir parent {config_dir.parent} does not look like project root"
        )

    def test_watchlist_path_exists(self) -> None:
        """The watchlist YAML file should exist on disk."""
        assert WATCHLIST_PATH.exists(), f"Watchlist not found at {WATCHLIST_PATH}"

    def test_watchlist_is_yaml(self) -> None:
        """Watchlist file should be valid YAML with a 'projects' key."""
        import yaml

        with open(WATCHLIST_PATH) as f:
            data = yaml.safe_load(f)

        assert isinstance(data, dict)
        assert "projects" in data
        assert len(data["projects"]) > 0
