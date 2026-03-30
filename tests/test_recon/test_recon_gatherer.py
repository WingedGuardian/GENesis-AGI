"""Tests for ReconGatherer watchlist path and release fetching."""

from __future__ import annotations

from genesis.recon.gatherer import _CONFIG_DIR, _WATCHLIST_PATH


class TestWatchlistPath:
    """Verify the watchlist path resolves correctly."""

    def test_config_dir_points_to_project_root(self) -> None:
        """_CONFIG_DIR should be <project_root>/config, not src/config."""
        assert _CONFIG_DIR.name == "config"
        # The parent of config/ should contain src/ (i.e., it's the project root)
        assert (_CONFIG_DIR.parent / "src").is_dir(), (
            f"_CONFIG_DIR parent {_CONFIG_DIR.parent} does not look like project root"
        )

    def test_watchlist_path_exists(self) -> None:
        """The watchlist YAML file should exist on disk."""
        assert _WATCHLIST_PATH.exists(), (
            f"Watchlist not found at {_WATCHLIST_PATH}"
        )

    def test_watchlist_is_yaml(self) -> None:
        """Watchlist file should be valid YAML with a 'projects' key."""
        import yaml

        with open(_WATCHLIST_PATH) as f:
            data = yaml.safe_load(f)

        assert isinstance(data, dict)
        assert "projects" in data
        assert len(data["projects"]) > 0
