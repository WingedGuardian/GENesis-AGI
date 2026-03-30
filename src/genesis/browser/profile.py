"""BrowserProfileManager — manages persistent Playwright browser profiles.

The profile directory (default: ~/.genesis/browser-profile/) stores Chrome's
user-data-dir, which persists cookies, localStorage, and login sessions across
Playwright MCP sessions. This enables Layer 2 browser automation (managed
browser with agent-owned logins).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path

from genesis.browser.types import BrowserSession, ProfileInfo

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"


class BrowserProfileManager:
    """Manages the persistent browser profile directory.

    The profile is stored on disk and used by Playwright MCP via
    ``--user-data-dir``. This class provides utilities for inspecting,
    backing up, and selectively clearing profile state.
    """

    def __init__(self, profile_dir: str | Path | None = None) -> None:
        self._profile_dir = Path(profile_dir) if profile_dir else _DEFAULT_PROFILE_DIR

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    def ensure_dir(self) -> Path:
        """Create the profile directory if it doesn't exist."""
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        return self._profile_dir

    def get_info(self) -> ProfileInfo:
        """Get summary information about the current profile."""
        if not self._profile_dir.exists():
            return ProfileInfo(
                profile_path=str(self._profile_dir),
                exists=False,
            )

        size_bytes = sum(
            f.stat().st_size for f in self._profile_dir.rglob("*") if f.is_file()
        )
        sessions = self._list_sessions()

        return ProfileInfo(
            profile_path=str(self._profile_dir),
            exists=True,
            size_mb=round(size_bytes / (1024 * 1024), 2),
            sessions=sessions,
        )

    def _list_sessions(self) -> list[BrowserSession]:
        """List logged-in sessions by reading Chrome's cookie database."""
        cookies_db = self._profile_dir / "Default" / "Cookies"
        if not cookies_db.exists():
            return []

        sessions: dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(cookies_db))
            try:
                cursor = conn.execute(
                    "SELECT host_key, COUNT(*) FROM cookies GROUP BY host_key"
                )
                for host, count in cursor.fetchall():
                    domain = host.lstrip(".")
                    sessions[domain] = sessions.get(domain, 0) + count
            finally:
                conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.debug("Could not read cookies database", exc_info=True)
            return []

        return [
            BrowserSession(domain=domain, cookie_count=count)
            for domain, count in sorted(sessions.items())
        ]

    def clear_domain(self, domain: str) -> bool:
        """Remove cookies for a specific domain (selective logout).

        Returns True if any cookies were removed.
        """
        cookies_db = self._profile_dir / "Default" / "Cookies"
        if not cookies_db.exists():
            return False

        try:
            conn = sqlite3.connect(str(cookies_db))
            try:
                # Escape LIKE wildcards to prevent unintended matches.
                escaped = domain.replace("%", r"\%").replace("_", r"\_")
                cursor = conn.execute(
                    "DELETE FROM cookies WHERE host_key LIKE ? ESCAPE '\\'",
                    (f"%{escaped}%",),
                )
                conn.commit()
                removed = cursor.rowcount > 0
            finally:
                conn.close()
            if removed:
                logger.info("Cleared cookies for domain: %s", domain)
            return removed
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            logger.error("Failed to clear cookies for %s", domain, exc_info=True)
            return False

    def export_state(self, dest: str | Path) -> Path:
        """Export the browser state (cookies, localStorage) to a JSON file.

        This uses Playwright's storage-state format for portability.
        """
        dest_path = Path(dest)
        cookies_db = self._profile_dir / "Default" / "Cookies"

        state: dict = {"cookies": [], "origins": []}

        if cookies_db.exists():
            try:
                conn = sqlite3.connect(str(cookies_db))
                try:
                    cursor = conn.execute(
                        "SELECT host_key, name, value, path, is_secure, is_httponly "
                        "FROM cookies"
                    )
                    for row in cursor.fetchall():
                        state["cookies"].append({
                            "domain": row[0],
                            "name": row[1],
                            "value": row[2],
                            "path": row[3],
                            "secure": bool(row[4]),
                            "httpOnly": bool(row[5]),
                        })
                finally:
                    conn.close()
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                logger.warning("Could not export cookies", exc_info=True)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(json.dumps(state, indent=2))
        # Restrict permissions — file contains session tokens.
        dest_path.chmod(0o600)
        logger.info("Exported browser state to %s (%d cookies)", dest_path, len(state["cookies"]))
        return dest_path

    def backup(self, dest_dir: str | Path) -> Path | None:
        """Create a full backup of the browser profile directory.

        Returns the backup path, or None if the profile doesn't exist.
        """
        if not self._profile_dir.exists():
            return None

        dest_path = Path(dest_dir)
        dest_path.mkdir(parents=True, exist_ok=True)
        backup_path = dest_path / "browser-profile-backup"

        if backup_path.exists():
            shutil.rmtree(backup_path)

        shutil.copytree(self._profile_dir, backup_path)
        logger.info("Browser profile backed up to %s", backup_path)
        return backup_path

    def reset(self) -> bool:
        """Delete the entire browser profile (full logout from all services).

        Returns True if the profile was deleted.
        """
        if not self._profile_dir.exists():
            return False

        shutil.rmtree(self._profile_dir)
        logger.info("Browser profile reset (all sessions cleared)")
        return True
