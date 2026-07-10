"""Telegram credential bridge — BOTH SIDES. Propagates credentials via shared filesystem.

Genesis (container) owns the secrets file. This module extracts only the
Telegram credentials and writes them to the shared Incus mount, where
Guardian (host) reads them. The full secrets file never leaves the container.

Container side: propagate_telegram_credentials() — called from awareness tick
Host side: load_telegram_credentials() — called from check.py dispatcher

Both sides see the same file via Incus shared mount with shift=true.
Container writes to ~/.genesis/shared/guardian/telegram_creds.env,
host reads from $STATE_DIR/shared/guardian/telegram_creds.env.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "load_backup_passphrase",
    "load_cc_oauth_token",
    "load_provisioning_credentials",
    "load_telegram_credentials",
    "mirror_credential_backup",
    "propagate_backup_passphrase",
    "propagate_cc_oauth_token",
    "propagate_guardian_credentials",
    "propagate_provisioning_credentials",
    "propagate_telegram_credentials",
]

# Container-side paths
_CONTAINER_SHARED_DIR = Path("~/.genesis/shared").expanduser()
_CONTAINER_SECRETS = Path("~/genesis/secrets.env").expanduser()

# Output filename (same on both sides of the mount)
_CREDS_FILENAME = "telegram_creds.env"
_CREDS_SUBDIR = "guardian"

# Provisioning (Proxmox) credential propagation — only the two API token
# strings cross the bridge; host/node/vmid are non-secret config, not here.
_PROVISIONING_FILENAME = "proxmox_creds.env"
_KEY_MAP_PROVISIONING = {
    "PROXMOX_AUDIT_TOKEN": "PROXMOX_AUDIT_TOKEN",
    "PROXMOX_PROVISION_TOKEN": "PROXMOX_PROVISION_TOKEN",
}

# Backup-passphrase escrow. GENESIS_BACKUP_PASSPHRASE lives ONLY inside
# secrets.env, which the backup encrypts WITH it — so a secrets.env loss makes
# the encrypted backup undecryptable (a circular trap). Escrowing the passphrase
# to the host-side shared mount (outside the container's blast radius) breaks the
# circle: a rebuilt/fresh container can decrypt the backup from the host copy.
# The passphrase is as sensitive as everything it decrypts; the escrow file is
# 0600 on the host mount (same trust level as the telegram/proxmox tokens here).
# NOTE: backup.sh reads secrets.env via scripts/lib/load_secrets.sh (dotenv-safe
# line parser, no shell evaluation) while this escrow reads it via _read_dotenv.
# Both strip one quote layer; they can still diverge on exotic values (embedded
# newlines are impossible in a single line; unbalanced quotes differ), in which
# case the escrowed value would not match what encrypted the backup. The
# generated passphrase is a plain token; keep any hand-set one shell-safe
# (alphanumeric / base64) to stay sound.
_PASSPHRASE_FILENAME = "backup_passphrase.env"
_KEY_MAP_PASSPHRASE = {
    "GENESIS_BACKUP_PASSPHRASE": "GENESIS_BACKUP_PASSPHRASE",
}

# Credential-backup mirror (G.4). The encrypted creds+secrets bundle that
# backup.sh produces lives ONLY in the Tier-1 backup clone inside the container
# FS — a destroyed container loses it, and the Tier-2 off-site copy needs the
# very git/gh credentials it would restore (chicken-and-egg). Mirroring the
# already-encrypted bundle to the host-side shared mount lets a rebuilt container
# recover creds + the guardian control-plane key with zero network. Only .gpg
# files cross (already encrypted); the passphrase is escrowed separately by
# propagate_backup_passphrase. The guardian makes a second, host-only copy
# (creds-archive) that the container cannot reach — see cred_watch.
_MIRROR_SUBDIR = "creds-mirror"      # under <shared>/guardian/
_MIRROR_STAMP = "MIRROR_STAMP"       # completeness marker, written LAST
_MIRROR_SRC_SUBDIRS = ("creds", "secrets")  # subtrees of the Tier-1 clone to mirror

# CC recovery-brain OAuth setup-token sync. The host Guardian's `claude -p`
# recovery brain authenticates via a one-time manual `claude login` (no refresh),
# so if that login dies the brain silently goes dark. A `claude setup-token`
# 1-year OAuth token (used via CLAUDE_CODE_OAUTH_TOKEN — NOT an ANTHROPIC_API_KEY)
# minted anywhere and dropped in the dedicated container file below is synced to
# the host shared mount; the host-side diagnosis path injects it ONLY as a
# FALLBACK when its own login is dead (never degrades a working login). Two keys
# cross: the token + its creation epoch (which drives the pre-expiry warning).
# ⚠ The token lives in a DEDICATED file, NOT secrets.env: runtime/init/secrets.py
# does load_dotenv(secrets.env, override=True), which would inject
# CLAUDE_CODE_OAUTH_TOKEN into every CONTAINER-side `claude` subprocess and
# hijack the container's own CC auth. Keeping it out of secrets.env is
# load-bearing, not tidiness.
_CC_TOKEN_FILENAME = "cc_oauth_token.env"
_CC_TOKEN_SOURCE = Path("~/.genesis/cc_oauth_token.env").expanduser()
_KEY_MAP_CC_TOKEN = {
    "CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN",
    "GENESIS_CC_TOKEN_CREATED_AT": "GENESIS_CC_TOKEN_CREATED_AT",
}

# Keys to extract from container secrets.env
# Maps source key name → output key name
_KEY_MAP = {
    "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_FORUM_CHAT_ID": "TELEGRAM_CHAT_ID",  # Guardian uses CHAT_ID
    "TELEGRAM_CHAT_ID": "TELEGRAM_CHAT_ID",  # Also accept direct name
    "TELEGRAM_THREAD_ID": "TELEGRAM_THREAD_ID",
}


def propagate_telegram_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Extract Telegram credentials from secrets.env and write to shared mount.

    Called from the container side (awareness loop tick). Writes only the
    Telegram keys Guardian needs — no other secrets are exposed.

    Returns the path written, or None if no bot token found.
    """
    src = secrets_path or _CONTAINER_SECRETS
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    # Read source secrets
    source_secrets = _read_dotenv(src)
    if not source_secrets:
        logger.debug("No secrets file for telegram propagation — skipping")
        return None

    # Extract and map Telegram keys
    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP.items():
        value = source_secrets.get(src_key, "")
        if value and dst_key not in creds:  # First match wins (FORUM_CHAT_ID before CHAT_ID)
            creds[dst_key] = value

    if not creds.get("TELEGRAM_BOT_TOKEN"):
        logger.debug("No TELEGRAM_BOT_TOKEN present — skipping telegram propagation")
        return None

    out_path = _write_creds_atomic(out_dir, _CREDS_FILENAME, creds)
    logger.debug("Telegram credentials propagated to %s (%d keys)", out_path, len(creds))
    return out_path


def load_telegram_credentials(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read Telegram credentials from the shared mount (host side).

    Returns a dict with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.
    Returns empty dict if the file is missing or unreadable — the caller
    should fall back to other credential sources.
    """
    creds_path = Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _CREDS_FILENAME

    if not creds_path.exists():
        logger.debug("Telegram credentials not found at %s", creds_path)
        return {}

    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read Telegram credentials: %s", exc)
        return {}


def propagate_provisioning_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Extract Proxmox API tokens from secrets.env → shared mount (host reads).

    Only PROXMOX_AUDIT_TOKEN / PROXMOX_PROVISION_TOKEN are exposed. Requires at
    least the audit token (read-only capacity works with audit alone); the
    provision token is propagated when present. Returns the path written, or
    None if the audit token is absent.
    """
    src = secrets_path or _CONTAINER_SECRETS
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    source_secrets = _read_dotenv(src)
    if not source_secrets:
        logger.debug("No secrets file for provisioning propagation — skipping")
        return None

    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP_PROVISIONING.items():
        value = source_secrets.get(src_key, "")
        if value:
            creds[dst_key] = value

    if not creds.get("PROXMOX_AUDIT_TOKEN"):
        logger.debug("No PROXMOX_AUDIT_TOKEN present — skipping provisioning creds")
        return None

    out_path = _write_creds_atomic(out_dir, _PROVISIONING_FILENAME, creds)
    logger.debug("Provisioning credentials propagated to %s (%d keys)", out_path, len(creds))
    return out_path


def load_provisioning_credentials(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read Proxmox API tokens from the shared mount (host side).

    Returns {} when absent/unreadable — the caller falls back to legacy
    secrets or refuses to build the adapter.
    """
    creds_path = (
        Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _PROVISIONING_FILENAME
    )
    if not creds_path.exists():
        logger.debug("Provisioning credentials not found at %s", creds_path)
        return {}
    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read provisioning credentials: %s", exc)
        return {}


def propagate_backup_passphrase(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Escrow GENESIS_BACKUP_PASSPHRASE → host shared mount (host reads on restore).

    Breaks the circular trap where the only copy of the passphrase lives inside
    the very secrets.env the backup encrypts with it. Returns the path written,
    or None if the passphrase is absent.
    """
    src = secrets_path or _CONTAINER_SECRETS
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    source_secrets = _read_dotenv(src)
    if not source_secrets:
        logger.debug("No secrets file for passphrase escrow — skipping")
        return None

    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP_PASSPHRASE.items():
        value = source_secrets.get(src_key, "")
        if value:
            creds[dst_key] = value

    if not creds.get("GENESIS_BACKUP_PASSPHRASE"):
        logger.debug("No GENESIS_BACKUP_PASSPHRASE present — skipping escrow")
        return None

    out_path = _write_creds_atomic(out_dir, _PASSPHRASE_FILENAME, creds)
    logger.debug("Backup passphrase escrowed to %s", out_path)
    return out_path


def load_backup_passphrase(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read the escrowed backup passphrase from the shared mount (host side).

    Returns {} when absent/unreadable — the caller falls back to secrets.env or
    refuses to decrypt.
    """
    creds_path = (
        Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _PASSPHRASE_FILENAME
    )
    if not creds_path.exists():
        logger.debug("Escrowed backup passphrase not found at %s", creds_path)
        return {}
    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read escrowed backup passphrase: %s", exc)
        return {}


def mirror_credential_backup(
    shared_dir: Path | None = None,
    backup_dir: Path | None = None,
) -> Path | None:
    """Mirror the encrypted creds+secrets bundle → the host-side shared mount.

    Source = the Tier-1 backup clone (default ``~/backups/genesis-backups`` via
    ``cred_integrity._default_backup_dir``). Only ``*.gpg`` files under ``creds/``
    and ``secrets/`` are copied — already encrypted at rest. Copy-if-changed
    (size+mtime), prune mirror-side files whose source vanished, then write the
    ``MIRROR_STAMP`` completeness marker LAST. Returns the mirror dir on success
    (>=1 file mirrored), else None. Skips silently when the shared mount or the
    backup clone is absent (a no-backup / no-guardian install). Never raises.
    """
    # Local import: cred_integrity is the standalone validator (stdlib-only); we
    # reuse only its pure path helper so the Tier-1 layout stays defined once.
    from genesis.guardian.cred_integrity import _default_backup_dir

    src_root = backup_dir or _default_backup_dir(Path.home())
    shared_base = shared_dir or _CONTAINER_SHARED_DIR
    if not shared_base.exists():
        logger.debug("Shared mount %s absent — skipping cred mirror", shared_base)
        return None
    if not src_root.exists():
        logger.debug("No backup clone at %s — skipping cred mirror", src_root)
        return None

    # Enumerate the encrypted artifacts to mirror (rel-path → absolute source).
    src_files: dict[Path, Path] = {}
    for sub in _MIRROR_SRC_SUBDIRS:
        base = src_root / sub
        if not base.is_dir():
            continue
        for f in base.rglob("*.gpg"):
            if f.is_file():
                src_files[f.relative_to(src_root)] = f

    if not src_files:
        logger.debug("Backup clone %s has no encrypted creds to mirror", src_root)
        return None

    dest_root = shared_base / _CREDS_SUBDIR / _MIRROR_SUBDIR
    dest_root.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_root, stat.S_IRWXU)  # 0700

    for rel, srcf in src_files.items():
        destf = dest_root / rel
        if _needs_copy(srcf, destf):
            _atomic_copy(srcf, destf)

    # Prune mirror files whose source vanished (containment-guarded); keep the
    # source rel-paths plus the STAMP we are about to (re)write.
    _prune_to_keep(dest_root, set(src_files) | {Path(_MIRROR_STAMP)})

    # Completeness marker LAST — its presence means "a full mirror round finished".
    _atomic_write_text(
        dest_root / _MIRROR_STAMP,
        f"mirrored_at={_utc_now_iso()}\ncount={len(src_files)}\n",
    )
    logger.debug("Mirrored %d encrypted creds → %s", len(src_files), dest_root)
    return dest_root


def propagate_cc_oauth_token(
    shared_dir: Path | None = None,
    source_path: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Sync the CC setup-token (+ creation epoch) → host shared mount.

    Reads the DEDICATED container file (``~/.genesis/cc_oauth_token.env``,
    written by ``scripts/store_cc_token.sh``), NEVER secrets.env — see the
    module note: secrets.env is ``load_dotenv``'d with ``override=True`` and a
    token there would hijack the container's own CC auth. The host injects this
    token ONLY as a fallback when its own ``claude login`` is dead. Opt-in /
    lazy: absent file or no token → returns None (nothing propagated).

    ``secrets_path`` is accepted (and ignored) so the combined bridge can call
    every leg with a uniform signature; the token has its own source file.
    """
    src = source_path or _CC_TOKEN_SOURCE
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    source_creds = _read_dotenv(src)
    if not source_creds:
        logger.debug("No CC token file at %s — skipping cc-token propagation", src)
        return None

    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP_CC_TOKEN.items():
        value = source_creds.get(src_key, "")
        if value:
            creds[dst_key] = value

    if not creds.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.debug("No CLAUDE_CODE_OAUTH_TOKEN present — skipping cc-token propagation")
        return None

    # Backfill the creation epoch from the source file's mtime if the intake
    # script didn't stamp one — age drives the host's pre-expiry warning.
    if not creds.get("GENESIS_CC_TOKEN_CREATED_AT"):
        with contextlib.suppress(OSError):
            creds["GENESIS_CC_TOKEN_CREATED_AT"] = str(int(src.stat().st_mtime))

    out_path = _write_creds_atomic(out_dir, _CC_TOKEN_FILENAME, creds)
    logger.debug("CC OAuth token propagated to %s", out_path)  # never logs the value
    return out_path


def load_cc_oauth_token(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read the synced CC OAuth token from the shared mount (host side).

    Returns {} when absent/unreadable — the host then relies on its own
    ``claude login`` (the token is injected only when that login is dead).
    """
    creds_path = (
        Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _CC_TOKEN_FILENAME
    )
    if not creds_path.exists():
        logger.debug("Synced CC OAuth token not found at %s", creds_path)
        return {}
    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read synced CC OAuth token: %s", exc)
        return {}


def propagate_guardian_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> list[Path]:
    """Combined container-side bridge: telegram + provisioning + passphrase escrow
    + cc-token + encrypted-backup mirror.

    Wired to the awareness-loop tick (called zero-arg). A failure in one leg
    never blocks the others, and never raises into the loop.
    """
    written: list[Path] = []
    for fn in (
        propagate_telegram_credentials,
        propagate_provisioning_credentials,
        propagate_backup_passphrase,
        propagate_cc_oauth_token,
    ):
        try:
            path = fn(shared_dir=shared_dir, secrets_path=secrets_path)
        except Exception as exc:  # noqa: BLE001 — must never break the tick
            logger.warning("credential propagation (%s) failed: %s", fn.__name__, exc)
            continue
        if path:
            written.append(path)

    # Mirror leg has a different source (the backup clone, not secrets.env), so it
    # is called explicitly rather than in the secrets-path loop above.
    try:
        mirror = mirror_credential_backup(shared_dir=shared_dir)
    except Exception as exc:  # noqa: BLE001 — must never break the tick
        logger.warning("credential backup mirror failed: %s", exc)
        mirror = None
    if mirror:
        written.append(mirror)

    return written


def _write_creds_atomic(out_dir: Path, filename: str, creds: dict[str, str]) -> Path:
    """Atomic 0600 write of a creds dict; skip the write if unchanged."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    new_content = "".join(f"{k}={v}\n" for k, v in sorted(creds.items()))

    if out_path.exists():
        try:
            if out_path.read_text() == new_content:
                return out_path
        except OSError:
            pass  # File unreadable — rewrite it

    tmp_path = out_dir / f".{filename}.tmp"
    tmp_path.write_text(new_content)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    os.replace(tmp_path, out_path)
    return out_path


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _needs_copy(src: Path, dest: Path) -> bool:
    """True if dest is missing or differs from src by size or mtime.

    Steady state is stat-only: ``_atomic_copy`` preserves the source mtime, so an
    unchanged source (the common per-tick case) compares equal and is skipped.
    This is only an optimisation — on an exotic shared mount that does not
    round-trip mtime, the worst case is re-copying a ~100 KB bundle each tick,
    which is harmless (the copy is atomic and the outcome identical).
    """
    if not dest.exists():
        return True
    try:
        s, d = src.stat(), dest.stat()
    except OSError:
        return True
    return s.st_size != d.st_size or int(s.st_mtime) != int(d.st_mtime)


def _atomic_copy(src: Path, dest: Path, mode: int = 0o600) -> None:
    """Copy src → dest atomically (same-dir tmp + os.replace), preserving mtime.

    ``copy2`` preserves the source mtime so ``_needs_copy`` can skip unchanged
    files on later ticks. The destination is forced to ``mode`` (0600) after copy
    — never inherit the source's mode.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp"
    shutil.copy2(src, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def _atomic_write_text(dest: Path, text: str, mode: int = 0o600) -> None:
    """Atomic 0600 text write (same-dir tmp + os.replace)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp"
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def _prune_to_keep(root: Path, keep: set[Path]) -> None:
    """Delete files under ``root`` whose rel-path is not in ``keep``.

    Containment-guarded: only unlinks regular files that resolve to a path
    strictly under ``root`` (a symlink escaping the tree is never followed to a
    delete). Empty directories are left behind (harmless).
    """
    try:
        root_res = root.resolve()
    except OSError:
        return
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        if rel in keep:
            continue
        try:
            if f.resolve().is_relative_to(root_res):
                f.unlink()
        except OSError:
            logger.warning("cred mirror prune: could not remove %s", f, exc_info=True)


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple key=value file. Handles comments and optional quotes."""
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            # Handle 'export KEY=value' syntax
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip("'\"")
            result[key] = value
    return result
