"""Credential-file integrity validation + restore — BOTH SIDES, standalone.

CONTRACT: stdlib + PyYAML only. **ZERO ``genesis.*`` imports.** The host
guardian pipes THIS FILE'S SOURCE into the container's *system* python3
(``incus exec ... -- su - ubuntu -c "python3 - check --json"``, stdin = this
source), so the module must run with no package context and survive a broken
``.venv``. A subprocess parity test (``test_cred_integrity.py``) re-runs the
module in pipe mode and fails the build if any ``genesis.*`` import creeps in.

Credential entry points and a shared authenticated backup-decryption facility:

- **check** (read-only): validate each target credential file; return only a
  JSON verdict. No secret bytes ever cross the container boundary.
- **restore** (mutating, container-only): decrypt the last-known-good copy from
  the Tier-1 backup clone, validate the *decrypted* bytes with the same
  validator BEFORE touching the original, move the corrupt original aside, then
  atomically place the restored file. The passphrase is resolved locally
  (env → validated secrets.env → host escrow) so the guardian process — which
  only pipes the command — never handles it.
- **decrypt-backup**: authenticate one encrypted message and atomically publish
  its plaintext. ``decrypt_backup_file`` and ``decrypt_backup_stream`` share
  this boundary with backup, restore and transcript-evidence callers.

Trigger policy (locked): restore fires STRICTLY on observed corruption
(missing-with-backup / empty / NUL-zeroed / unparseable / missing structural
key). A valid-but-different file is never touched — this is what protects a
mid-refresh ``.credentials.json`` or an install that legitimately omits an
optional key from a destructive restore.

Sibling: ``credential_bridge.py`` owns the passphrase *escrow* write and uses
this module's credential-specific reader. Secrets follow load_secrets.sh's
single-line syntax; escrow values are literal bytes between '=' and record LF.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# This module is piped into the container's *system* python3, whose version is
# unknown (an install may leave python3 at 3.10 while provisioning 3.12
# elsewhere). Stay portable to 3.9+: use timezone.utc, not datetime.UTC (3.11+).
# (UP017 would "modernize" this to datetime.UTC — exactly what breaks 3.10.)
_UTC = timezone.utc  # noqa: UP017

try:
    import yaml  # PyYAML — present in the venv and in the container's system python3
    _YAML_OK = True
except ImportError:  # pragma: no cover - degradation path
    _YAML_OK = False

# ── Target inventory ────────────────────────────────────────────────────────
# Paths are HOME-relative; backup_rel is relative to the Tier-1 backup clone
# (~/backups/genesis-backups). Names/paths MUST match scripts/backup.sh §8 so a
# corrupt file always has a decryptable last-known-good copy.


@dataclass(frozen=True)
class CredTarget:
    name: str                 # stable id, e.g. "secrets_env"
    path: str                 # HOME-relative, e.g. "genesis/secrets.env"
    backup_rel: str           # inside the backup clone, e.g. "secrets/secrets.env.gpg"
    kind: str                 # "dotenv" | "json" | "yaml" | "ssh_key"
    required_keys: tuple[str, ...] = ()
    min_keys: int = 0         # dotenv only — a truncation/zeroing guard
    file_mode: int = 0o600


# secrets.env deliberately carries NO required_keys and min_keys=1: requiring a
# specific key (e.g. ANTHROPIC_API_KEY) or a key-count floor would false-positive
# a *destructive* restore on an install that legitimately omits a key or ships a
# small secrets.env, violating the strict-corruption rule. min_keys=1 means only
# a file that parses to ZERO valid keys (garbage / all-comments) is corrupt; the
# real outage signatures (empty, NUL-zeroed) are caught by the pre-checks above.
# .credentials.json keeps its one stable structural key (claudeAiOauth) — CC
# always writes it, and missing_keys is debounced 2 ticks before any restore.
DEFAULT_TARGETS: tuple[CredTarget, ...] = (
    CredTarget("secrets_env", "genesis/secrets.env", "secrets/secrets.env.gpg",
               "dotenv", min_keys=1),
    CredTarget("claude_credentials", ".claude/.credentials.json",
               "creds/claude_credentials.json.gpg", "json",
               required_keys=("claudeAiOauth",)),
    CredTarget("claude_json", ".claude.json", "creds/claude.json.gpg", "json"),
    # User-level CC settings. The PROJECT .claude/settings.json is tracked in
    # git; this one is not, and it carries its own live hooks block — so a
    # process-internal write that empties it disarms those hooks with nothing
    # to notice. Deliberately NO required_keys, for the same reason secrets.env
    # has none: a user-level hooks block is install-local and a fresh install
    # may legitimately have none, and missing_keys is RESTORABLE, so requiring
    # it would trigger a destructive restore over a perfectly healthy file. The
    # real outage signatures here (empty, NUL-zeroed, unparseable) are caught
    # without any key assumption — the same shape as claude_json above.
    CredTarget("claude_settings", ".claude/settings.json",
               "creds/claude_settings.json.gpg", "json"),
    CredTarget("gh_hosts", ".config/gh/hosts.yml", "creds/gh_hosts.yml.gpg", "yaml"),
    CredTarget("guardian_remote", ".genesis/guardian_remote.yaml",
               "creds/guardian_remote.yaml.gpg", "yaml"),
    CredTarget("genesis_yaml", ".genesis/config/genesis.yaml",
               "creds/genesis.yaml.gpg", "yaml"),
    CredTarget("ssh_guardian_key", ".ssh/genesis_guardian_ed25519",
               "creds/ssh/genesis_guardian_ed25519.gpg", "ssh_key", file_mode=0o600),
    CredTarget("ssh_id_ed25519", ".ssh/id_ed25519",
               "creds/ssh/id_ed25519.gpg", "ssh_key", file_mode=0o600),
)

# Statuses that mean "corrupt and safe to restore from backup". "unreadable"
# is deliberately excluded — a permission/IO error is ambiguous, not proven
# corruption, so it alerts but never triggers a destructive overwrite.
RESTORABLE_STATUSES = frozenset(
    {"missing", "empty", "nul_bytes", "parse_error", "missing_keys"}
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    status: str   # ok|absent|missing|empty|nul_bytes|parse_error|missing_keys|unreadable
    detail: str = ""


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    action: str   # restored|skipped_no_backup|skipped_no_passphrase|backup_invalid|
                  # decrypt_failed|restore_verify_failed|error
    aside_path: str | None = None
    backup_mtime: str | None = None
    detail: str = ""


# ── Pure validation ─────────────────────────────────────────────────────────


def _parse_dotenv(text: str) -> dict[str, str]:
    """Minimal key=value parser (mirrors credential_bridge._read_dotenv)."""
    result: dict[str, str] = {}
    for raw in text.split("\n"):
        line = raw.strip(" \t\r\v\f")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip(" \t\r\v\f")
        if key.startswith("export "):
            key = key[7:].strip(" \t\r\v\f")
        if key:
            result[key] = value.strip(" \t\r\v\f").strip("'\"")
    return result


def validate_bytes(
    kind: str,
    data: bytes,
    required_keys: tuple[str, ...] = (),
    min_keys: int = 0,
) -> ValidationResult:
    """Validate raw file bytes. Pure — the single implementation both sides use."""
    if not data or not data.strip():
        return ValidationResult(False, "empty", "file is empty")
    if b"\x00" in data:
        return ValidationResult(False, "nul_bytes", "contains NUL bytes (zeroed write)")

    if kind == "ssh_key":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ValidationResult(False, "parse_error", "not valid UTF-8")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if first.startswith("-----BEGIN ") and "PRIVATE KEY" in first:
            return ValidationResult(True, "ok")
        return ValidationResult(False, "parse_error", "missing OpenSSH private-key header")

    if kind == "dotenv":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ValidationResult(False, "parse_error", "not valid UTF-8")
        parsed = _parse_dotenv(text)
        if len(parsed) < max(min_keys, 1):
            return ValidationResult(
                False, "parse_error", f"only {len(parsed)} keys (min {min_keys})"
            )
        missing = [k for k in required_keys if not parsed.get(k)]
        if missing:
            return ValidationResult(False, "missing_keys", f"missing {','.join(missing)}")
        return ValidationResult(True, "ok")

    if kind == "json":
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            return ValidationResult(False, "parse_error", f"invalid JSON: {exc}")
        if not isinstance(obj, dict) or not obj:
            return ValidationResult(False, "parse_error", "not a non-empty JSON object")
        missing = [k for k in required_keys if k not in obj]
        if missing:
            return ValidationResult(False, "missing_keys", f"missing {','.join(missing)}")
        return ValidationResult(True, "ok")

    if kind == "yaml":
        if not _YAML_OK:
            # Degraded: without PyYAML we can only confirm non-empty/no-NUL (done
            # above). Report ok so a missing library never triggers a false restore.
            return ValidationResult(True, "ok", "yaml_unavailable")
        try:
            obj = yaml.safe_load(data)
        except yaml.YAMLError as exc:
            return ValidationResult(False, "parse_error", f"invalid YAML: {exc}")
        if not isinstance(obj, dict) or not obj:
            return ValidationResult(False, "parse_error", "not a non-empty YAML mapping")
        return ValidationResult(True, "ok")

    return ValidationResult(False, "parse_error", f"unknown kind {kind!r}")


def validate_file(
    target: CredTarget, home: Path, backup_dir: Path | None
) -> ValidationResult:
    """Validate one target on disk. Missing disambiguates on backup presence:
    missing + backup exists → corruption ("missing"); missing + no backup →
    never provisioned ("absent", healthy — the clean degradation for installs
    without backups or without an optional file like genesis.yaml)."""
    path = home / target.path
    if not path.exists():
        has_backup = backup_dir is not None and (backup_dir / target.backup_rel).exists()
        if has_backup:
            return ValidationResult(False, "missing", f"{path} absent but backup exists")
        return ValidationResult(True, "absent", f"{path} never provisioned")
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ValidationResult(False, "unreadable", f"read failed: {exc}")
    return validate_bytes(target.kind, data, target.required_keys, target.min_keys)


def check_all(
    targets: tuple[CredTarget, ...] | None,
    home: Path,
    backup_dir: Path | None,
) -> dict[str, ValidationResult]:
    tgts = targets if targets is not None else DEFAULT_TARGETS
    return {t.name: validate_file(t, home, backup_dir) for t in tgts}


# ── Restore (container-only side effects) ───────────────────────────────────


class _DecryptError(ValueError):
    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode
        self.command = None


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte — os.write may short-write; a truncated secret is unsafe."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _gpg_password(passphrase: str | bytes) -> bytes:
    password = passphrase.encode("utf-8") if isinstance(passphrase, str) else passphrase
    if not password or b"\n" in password or b"\0" in password:
        raise _DecryptError("backup passphrase must be a single line without a terminator")
    return password


def _gpg_command(src: Path, status_fd: int) -> list[str]:
    # Ignore gpg.conf: --ignore-mdc-error or an alternate output/status channel
    # would invalidate this boundary. Never put a passphrase in argv.
    return [
        "gpg", "--no-options", "--batch", "--yes", "--no-symkey-cache",
        "--pinentry-mode", "loopback", "--passphrase-fd", "0",
        "--status-fd", str(status_fd), "--decrypt", "--", str(src),
    ]


def _gpg_authenticated(status, returncode: int) -> None:
    """Require one protected, passphrase-decrypted message, after GPG exits.

    GnuPG doc/DETAILS defines these machine records. DECRYPTION_INFO alone
    also occurs on failure; DECRYPTION_OKAY alone can accept an unprotected
    packet with unsafe options. AEAD is protected even when its MDC field is
    zero. GOODMDC is obsolete and is not required for AEAD.
    """
    status.seek(0)
    data = status.read(262145)
    if returncode != 0:
        raise _DecryptError("encrypted backup decryption or integrity check failed",
                            returncode=returncode)
    if len(data) > 262144:
        raise _DecryptError("encrypted backup integrity status exceeds its limit")
    records = [line[9:].split() for line in data.split(b"\n")
               if line.startswith(b"[GNUPG:] ")]
    names = [fields[0] for fields in records if fields]
    forbidden = {b"DECRYPTION_KEY", b"DECRYPTION_FAILED", b"BADMDC", b"ERROR",
                 b"FAILURE", b"NODATA", b"BAD_PASSPHRASE", b"MISSING_PASSPHRASE"}
    required = (b"NEED_PASSPHRASE_SYM", b"BEGIN_DECRYPTION", b"DECRYPTION_INFO",
                b"DECRYPTION_OKAY", b"END_DECRYPTION", b"PLAINTEXT")
    if forbidden.intersection(names) or any(names.count(name) != 1 for name in required):
        raise _DecryptError("backup is not one passphrase-authenticated encrypted message")
    info = next(fields[1:] for fields in records if fields[0] == b"DECRYPTION_INFO")
    try:
        mdc, cipher = int(info[0]), int(info[1])
        aead = int(info[2]) if len(info) > 2 else 0
    except (ValueError, IndexError) as exc:
        raise _DecryptError("invalid encrypted backup integrity status") from exc
    if cipher <= 0 or (mdc <= 0 and aead <= 0):
        raise _DecryptError("encrypted backup has no integrity protection")


def _gpg_scratch(scratch: Path | None) -> Path:
    directory = Path(scratch) if scratch is not None else Path.home() / "tmp"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _gpg_run(src, password, output, scratch, timeout=None):
    """Bytes and staged files share argv, status checks, and process lifetime."""
    with tempfile.TemporaryFile(dir=_gpg_scratch(scratch)) as status:
        try:
            proc = subprocess.run(
                _gpg_command(Path(src), status.fileno()), input=_gpg_password(password),
                stdout=output, stderr=subprocess.DEVNULL, pass_fds=(status.fileno(),),
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise _DecryptError("gpg not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise _DecryptError("gpg decrypt timed out") from exc
        try:
            _gpg_authenticated(status, proc.returncode)
        except _DecryptError as exc:
            exc.command = proc.args
            raise
        return proc.stdout


def _gpg_decrypt(src: Path, passphrase: str) -> bytes:
    """Authenticate before returning credential bytes; retain the 60s timeout."""
    return _gpg_run(src, passphrase, subprocess.PIPE, None, timeout=60)


def decrypt_backup_file(src: Path, target: Path, passphrase: str | bytes) -> None:
    """Publish authenticated plaintext atomically; failure preserves target.

    GPG can emit all plaintext before discovering a bad trailer. It therefore
    writes only to a private sibling staging file, never the live destination.
    """
    target = Path(target)
    fd, name = tempfile.mkstemp(prefix=".gpg-restore-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stage:
            _gpg_run(src, passphrase, stage, target.parent)
            stage.flush()
            os.fsync(stage.fileno())
        os.replace(name, target)
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def decrypt_backup_stream(src: Path, passphrase: str | bytes, scratch: Path | None = None):
    """Bounded-memory stream: accept results only AFTER this context exits.

    Consumers may examine tentative bytes locally but must not publish them
    inside the context. Drain the complete message before checking integrity.
    """
    password = _gpg_password(passphrase)
    with tempfile.TemporaryFile(dir=_gpg_scratch(scratch)) as status:
        try:
            proc = subprocess.Popen(
                _gpg_command(Path(src), status.fileno()), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(status.fileno(),),
            )
        except FileNotFoundError as exc:
            raise _DecryptError("gpg not found") from exc
        try:
            proc.stdin.write(password)
            proc.stdin.close()
            yield proc.stdout
            while proc.stdout.read(65536):
                pass
            _gpg_authenticated(status, proc.wait(timeout=30))
        finally:
            proc.stdin.close()
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def restore_file(
    target: CredTarget, *, home: Path, backup_dir: Path, passphrase: str
) -> RestoreResult:
    """Restore one target from its encrypted backup. Order is the safety
    property: decrypt → validate decrypted → (only then) move original aside →
    atomic place → re-validate. A bad backup never destroys a present original."""
    src = backup_dir / target.backup_rel
    if not src.exists():
        return RestoreResult(False, "skipped_no_backup", detail=f"no backup at {src}")

    try:
        data = _gpg_decrypt(src, passphrase)
    except _DecryptError as exc:
        return RestoreResult(False, "decrypt_failed", detail=str(exc))

    decrypted = validate_bytes(target.kind, data, target.required_keys, target.min_keys)
    if not decrypted.ok:
        return RestoreResult(
            False, "backup_invalid",
            detail=f"decrypted backup {decrypted.status}: {decrypted.detail}",
        )

    target_path = home / target.path
    tmp = target_path.with_name(f".{target_path.name}.restore-tmp-{os.getpid()}")
    aside: Path | None = None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target.kind == "ssh_key":
            os.chmod(target_path.parent, 0o700)

        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.chmod(tmp, target.file_mode)

        if target_path.exists():
            stamp = datetime.now(_UTC).strftime("%Y%m%dT%H%M%SZ")
            aside = target_path.with_name(f"{target_path.name}.corrupt-{stamp}")
            os.replace(target_path, aside)
            with contextlib.suppress(OSError):
                os.chmod(aside, 0o600)  # the corrupt original is still sensitive

        os.replace(tmp, target_path)
    except OSError as exc:
        # Never leave a plaintext temp of the decrypted secret behind.
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
        return RestoreResult(
            False, "error", aside_path=str(aside) if aside else None,
            detail=f"placement failed: {exc}",
        )

    placed = validate_file(target, home, backup_dir)
    mtime = datetime.fromtimestamp(src.stat().st_mtime, _UTC).isoformat()
    if not placed.ok:
        return RestoreResult(
            False, "restore_verify_failed",
            aside_path=str(aside) if aside else None,
            backup_mtime=mtime, detail=f"placed file {placed.status}",
        )
    return RestoreResult(
        True, "restored",
        aside_path=str(aside) if aside else None,
        backup_mtime=mtime, detail=f"restored from backup dated {mtime}",
    )


# ── Passphrase resolution (container-side) ──────────────────────────────────


def _backup_passphrase_value(raw: bytes, *, escrow: bool = False) -> str | None:
    """Read this credential's file grammar without changing generic validation.

    Secrets mirror load_secrets.sh's literal, single-LF-record syntax (no
    expansion/multiline syntax). Escrow mirrors its raw writer and shell reader:
    first matching record, no quote/whitespace/newline translation of its value.
    """
    if b"\0" in raw:
        raise ValueError("backup passphrase file contains NUL")
    whitespace = " \t\r\v\f"
    selected = None
    for record in raw.decode("utf-8").split("\n"):
        if escrow:
            for prefix in ("GENESIS_BACKUP_PASSPHRASE=", "export GENESIS_BACKUP_PASSPHRASE="):
                if record.startswith(prefix):
                    return record[len(prefix):]
            continue
        line = record.strip(whitespace)
        if line.startswith("export "):
            line = line[7:]
        key, separator, value = line.partition("=")
        if not separator or key != "GENESIS_BACKUP_PASSPHRASE":
            continue
        if value.startswith(("'", '"')):
            selected = value[1:].split(value[0], 1)[0]
        else:
            for position, character in enumerate(value):
                if character == "#" and position and value[position-1] in whitespace:
                    value = value[:position]
                    break
            selected = value.rstrip(whitespace)
    return selected


def resolve_passphrase(home: Path) -> str | None:
    """env → validated secrets.env → host escrow. The escrow is the exit for the
    circular case (secrets.env itself corrupt → its passphrase is unusable)."""
    env_pass = os.environ.get("GENESIS_BACKUP_PASSPHRASE", "")
    if env_pass:
        return env_pass

    secrets = home / "genesis/secrets.env"
    if secrets.exists():
        try:
            raw = secrets.read_bytes()
        except OSError:
            raw = b""
        # Only trust secrets.env for the passphrase if it is NOT itself corrupt.
        if validate_bytes("dotenv", raw, min_keys=1).ok:
            val = _backup_passphrase_value(raw)
            if val:
                return val

    escrow = home / ".genesis/shared/guardian/backup_passphrase.env"
    if escrow.exists():
        try:
            val = _backup_passphrase_value(escrow.read_bytes(), escrow=True)
            if val:
                return val
        except (OSError, ValueError):
            pass
    return None


# ── Rate-cap helper (shared policy primitive) ───────────────────────────────


def allowed_restore(attempt_isotimes: list[str], now: datetime, max_per_day: int) -> bool:
    """True if fewer than max_per_day restore attempts fall in the last 24h."""
    if max_per_day <= 0:
        return False
    cutoff = now.timestamp() - 86400
    recent = 0
    for iso in attempt_isotimes:
        try:
            if datetime.fromisoformat(iso).timestamp() >= cutoff:
                recent += 1
        except ValueError:
            continue
    return recent < max_per_day


# ── CLI (works as `python -m ...` and as `python3 - <args>` pipe) ───────────


def _default_home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _default_backup_dir(home: Path) -> Path:
    # Derive from the RESOLVED home (respects --home), not $HOME — otherwise a
    # --home sandbox / container_home override leaks to the real backup clone.
    return home / "backups" / "genesis-backups"


def _targets_by_name() -> dict[str, CredTarget]:
    return {t.name: t for t in DEFAULT_TARGETS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cred_integrity", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--home", default=None)
    p_check.add_argument("--backup-dir", default=None)

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("--target", required=True)
    p_restore.add_argument("--json", action="store_true")
    p_restore.add_argument("--home", default=None)
    p_restore.add_argument("--backup-dir", default=None)

    p_decrypt = sub.add_parser("decrypt-backup")
    p_decrypt.add_argument("source", type=Path)
    p_decrypt.add_argument("target", type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "decrypt-backup":
        try:
            decrypt_backup_file(args.source, args.target, sys.stdin.buffer.read())
        except (OSError, ValueError, subprocess.SubprocessError):
            print("encrypted backup authentication failed", file=sys.stderr)
            return 1
        return 0
    home = Path(args.home).expanduser() if args.home else _default_home()
    backup_dir = (
        Path(args.backup_dir).expanduser() if args.backup_dir
        else _default_backup_dir(home)
    )
    backup_arg = backup_dir if backup_dir.exists() else None

    if args.cmd == "check":
        results = check_all(None, home, backup_arg)
        payload = {
            "version": 1,
            "results": {
                name: {
                    "ok": r.ok,
                    "status": r.status,
                    "detail": r.detail,
                    "path": str(home / _targets_by_name()[name].path),
                    "backup_exists": backup_arg is not None
                    and (backup_dir / _targets_by_name()[name].backup_rel).exists(),
                }
                for name, r in results.items()
            },
        }
        print(json.dumps(payload))
        return 0

    # restore
    target = _targets_by_name().get(args.target)
    if target is None:
        print(json.dumps({"ok": False, "action": "error", "detail": "unknown target"}))
        return 0
    if backup_arg is None:
        print(json.dumps({"ok": False, "action": "skipped_no_backup",
                          "detail": "no backup dir"}))
        return 0
    passphrase = resolve_passphrase(home)
    if not passphrase:
        print(json.dumps({"ok": False, "action": "skipped_no_passphrase",
                          "detail": "no passphrase in env/secrets/escrow"}))
        return 0
    result = restore_file(target, home=home, backup_dir=backup_dir, passphrase=passphrase)
    print(json.dumps({
        "ok": result.ok, "action": result.action, "aside_path": result.aside_path,
        "backup_mtime": result.backup_mtime, "detail": result.detail,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
