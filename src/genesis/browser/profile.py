"""BrowserProfileManager — manages persistent browser profiles.

Two profiles exist, one per local browser layer: Camoufox (Firefox,
~/.genesis/camoufox-profile/, cookies in ``cookies.sqlite``) and the Chromium
fallback (~/.genesis/browser-profile/, cookies in ``Default/Cookies``). Each
persists cookies, localStorage and login sessions across MCP sessions.

The cookie readers here work on the profile FILES. Editing one is only safe
while no browser has the profile open: a running browser keeps its cookie jar
in memory and writes it back, so the edit would be lost or would race it.
``running_pid()`` reports that case; a caller that holds the live browser
context uses its cookie API instead (see mcp/health/browser.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path

from genesis.browser.types import BrowserSession, ProfileInfo

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"

# Per browser: cookie-database candidates relative to the profile dir (first
# existing wins), table, host column, and the lock symlink the browser keeps in
# the profile while it is open. Chromium moved its cookie file to
# Default/Network/Cookies on some platforms; the Linux profile on this install
# still uses Default/Cookies (read 2026-10-05), so both are tried.
_COOKIE_STORES: dict[str, tuple[tuple[str, ...], str, str, str]] = {
    "chromium": (
        ("Default/Network/Cookies", "Default/Cookies"), "cookies", "host_key", "SingletonLock",
    ),
    "camoufox": (("cookies.sqlite",), "moz_cookies", "host", "lock"),
}


class ProfileInUse(RuntimeError):
    """The profile is open in a browser process this caller does not control."""


def normalize_domain(domain: str) -> str:
    """Lower-case a domain and drop a leading dot; reject anything else."""
    d = (domain or "").strip().lower().lstrip(".")
    if not d or any(c.isspace() or c in "/:%*?" for c in d):
        raise ValueError(f"not a domain: {domain!r}")
    # A bare label ("com") would match every cookie under that TLD in both
    # profiles; only localhost is a real single-label cookie host.
    if "." not in d and d != "localhost":
        raise ValueError(f"not a domain (no dot): {domain!r}")
    return d


def domain_matches(host: str, domain: str) -> bool:
    """True when a cookie host belongs to ``domain``: the domain itself or one
    of its subdomains. ``x.com`` matches ``x.com`` and ``api.x.com``, never
    ``netflix.com`` (which the substring match this replaced did)."""
    h = (host or "").lower().lstrip(".")
    return h == domain or h.endswith("." + domain)


class BrowserProfileManager:
    """Manages the persistent browser profile directory.

    The profile is stored on disk and used by Playwright MCP via
    ``--user-data-dir``. This class provides utilities for inspecting,
    backing up, and selectively clearing profile state.
    """

    def __init__(
        self, profile_dir: str | Path | None = None, browser: str = "chromium",
    ) -> None:
        if browser not in _COOKIE_STORES:
            raise ValueError(f"unknown browser profile kind: {browser!r}")
        self._profile_dir = Path(profile_dir) if profile_dir else _DEFAULT_PROFILE_DIR
        self._browser = browser

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    @property
    def browser(self) -> str:
        return self._browser

    def _cookie_store(self) -> tuple[Path, str, str]:
        candidates, table, column, _lock = _COOKIE_STORES[self._browser]
        paths = [self._profile_dir / c for c in candidates]
        path = next((p for p in paths if p.exists()), paths[-1])
        return path, table, column

    def running_pid(self) -> int | None:
        """PID of a live browser that has this profile open, else None.

        Both browsers keep a lock symlink in the profile while it is open
        (Firefox ``lock`` -> ``<ip>:+<pid>``, Chromium ``SingletonLock`` ->
        ``<host>-<pid>``). The link alone proves nothing: Camoufox 156 leaves
        it in place after a clean close (MEASURED 2026-10-05), and a crash
        leaves one too. Only a link naming a live pid counts as running.
        """
        lock = self._profile_dir / _COOKIE_STORES[self._browser][3]
        try:
            target = os.readlink(lock)
        except OSError:
            return None
        m = re.search(r"(\d+)$", target)
        if not m:
            return None
        pid = int(m.group(1))
        if pid <= 1:
            return None  # never probe init or the process group
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return pid  # alive, owned by another user
        return pid

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
                browser=self._browser,
            )

        size_bytes = sum(
            f.stat().st_size for f in self._profile_dir.rglob("*") if f.is_file()
        )
        error = ""
        try:
            sessions = self._list_sessions()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            # Unreadable is its own state, never "no sessions".
            logger.debug("Could not read cookies database", exc_info=True)
            sessions = []
            error = f"cookie database unreadable: {exc}"

        return ProfileInfo(
            profile_path=str(self._profile_dir),
            exists=True,
            size_mb=round(size_bytes / (1024 * 1024), 2),
            sessions=sessions,
            browser=self._browser,
            error=error,
        )

    def _list_sessions(self) -> list[BrowserSession]:
        """Count cookies per domain in the profile's cookie database.

        Read-only (``mode=ro``, which still sees a WAL-resident write). Raises
        sqlite errors so an unreadable store is never reported as empty.
        """
        cookies_db, table, column = self._cookie_store()
        if not cookies_db.exists():
            return []

        sessions: dict[str, int] = {}
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True)
        try:
            # table and column come from _COOKIE_STORES, never from a caller.
            cursor = conn.execute(
                f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"  # noqa: S608
            )
            for host, count in cursor.fetchall():
                domain = (host or "").lstrip(".")
                sessions[domain] = sessions.get(domain, 0) + count
        finally:
            conn.close()

        return [
            BrowserSession(domain=domain, cookie_count=count)
            for domain, count in sorted(sessions.items())
        ]

    def clear_domain(self, domain: str) -> int:
        """Remove the cookies of ``domain`` and its subdomains (selective logout).

        Matching is by whole labels (see :func:`domain_matches`). Returns the
        number of cookies removed. Raises :class:`ProfileInUse` when a browser
        has the profile open, because that browser would write its in-memory
        cookies back over the edit; ``ValueError`` on a non-domain; and the
        sqlite error when the store cannot be read or written (never a quiet 0).
        """
        d = normalize_domain(domain)
        pid = self.running_pid()
        if pid is not None:
            raise ProfileInUse(
                f"the {self._browser} profile is open in browser process {pid}; "
                "its cookies can only be cleared by the session running that "
                "browser, or after it closes"
            )
        cookies_db, table, column = self._cookie_store()
        if not cookies_db.exists():
            return 0
        if cookies_db.is_symlink():
            # A cookie store is a plain file; a link could point the edit at
            # another database.
            raise ValueError(f"{cookies_db} is a symlink; not editing it")

        conn = sqlite3.connect(str(cookies_db))
        try:
            hosts = [
                h
                for (h,) in conn.execute(f"SELECT DISTINCT {column} FROM {table}")  # noqa: S608
                if domain_matches(h, d)
            ]
            removed = 0
            for host in hosts:
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (host,))  # noqa: S608
                removed += cur.rowcount
            # Re-check just before committing: a browser that opened the profile
            # since the check above would write its in-memory cookies back over
            # this edit. Narrows the window to the commit itself.
            pid = self.running_pid()
            if pid is not None:
                conn.rollback()
                raise ProfileInUse(
                    f"the {self._browser} profile was opened by browser process {pid} "
                    "during the clear; nothing was changed"
                )
            conn.commit()
        finally:
            conn.close()
        if removed:
            logger.info("Cleared %d %s cookies for domain: %s", removed, self._browser, d)
        return removed

    def export_state(self, dest: str | Path) -> Path:
        """Export the browser state (cookies, localStorage) to a JSON file.

        This uses Playwright's storage-state format for portability.
        """
        if self._browser != "chromium":
            # The query below is Chromium's schema; another profile would
            # export an empty list without saying so.
            raise NotImplementedError(
                f"export_state supports the chromium profile, not {self._browser}"
            )
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
