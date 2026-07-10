"""Credential-file integrity validation + restore — BOTH SIDES, standalone.

CONTRACT: stdlib + PyYAML only. **ZERO ``genesis.*`` imports.** The host
guardian pipes THIS FILE'S SOURCE into the container's *system* python3
(``incus exec ... -- su - ubuntu -c "python3 - check --json"``, stdin = this
source), so the module must run with no package context and survive a broken
``.venv``. A subprocess parity test (``test_cred_integrity.py``) re-runs the
module in pipe mode and fails the build if any ``genesis.*`` import creeps in.

Two entry points, one implementation both sides share:

- **check** (read-only): validate each target credential file; return only a
  JSON verdict. No secret bytes ever cross the container boundary.
- **restore** (mutating, container-only): decrypt the last-known-good copy from
  the Tier-1 backup clone, validate the *decrypted* bytes with the same
  validator BEFORE touching the original, move the corrupt original aside, then
  atomically place the restored file. The passphrase is resolved locally
  (env → validated secrets.env → host escrow) so the guardian process — which
  only pipes the command — never handles it.

Trigger policy (locked): restore fires STRICTLY on observed corruption
(missing-with-backup / empty / NUL-zeroed / unparseable / missing structural
key). A valid-but-different file is never touched — this is what protects a
mid-refresh ``.credentials.json`` or an install that legitimately omits an
optional key from a destructive restore.

Sibling: ``credential_bridge.py`` owns the passphrase *escrow* write; this
module is its *reader* on the restore path. Keep the two dotenv parsers in
sync (both strip one quote layer; see the quoting caveat in credential_bridge).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


# secrets.env deliberately carries NO required_keys: requiring a specific key
# (e.g. ANTHROPIC_API_KEY) would false-positive a *destructive* restore on an
# install that legitimately omits it, violating the strict-corruption rule. The
# structural signals (empty / NUL-zeroed — the outage signature — / <min_keys)
# catch the real threat without that risk. .credentials.json keeps its one
# stable structural key (claudeAiOauth) — CC always writes it.
DEFAULT_TARGETS: tuple[CredTarget, ...] = (
    CredTarget("secrets_env", "genesis/secrets.env", "secrets/secrets.env.gpg",
               "dotenv", min_keys=5),
    CredTarget("claude_credentials", ".claude/.credentials.json",
               "creds/claude_credentials.json.gpg", "json",
               required_keys=("claudeAiOauth",)),
    CredTarget("claude_json", ".claude.json", "creds/claude.json.gpg", "json"),
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
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key:
            result[key] = value.strip().strip("'\"")
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


class _DecryptError(Exception):
    pass


def _gpg_decrypt(src: Path, passphrase: str) -> bytes:
    """Symmetric-decrypt a backup .gpg to bytes. Matches scripts/restore.sh:
    ``gpg --batch --yes --passphrase-fd 0 -d <src>`` (passphrase on stdin)."""
    try:
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet", "--passphrase-fd", "0", "-d", str(src)],
            input=passphrase.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise _DecryptError("gpg not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise _DecryptError("gpg decrypt timed out") from exc
    if proc.returncode != 0:
        # stderr carries no passphrase; trim to keep logs bounded.
        raise _DecryptError(
            proc.stderr.decode("utf-8", "replace").strip()[:200] or "gpg failed"
        )
    return proc.stdout


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
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target.kind == "ssh_key":
            os.chmod(target_path.parent, 0o700)

        tmp = target_path.with_name(f".{target_path.name}.restore-tmp-{os.getpid()}")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(tmp, target.file_mode)

        aside: Path | None = None
        if target_path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            aside = target_path.with_name(f"{target_path.name}.corrupt-{stamp}")
            os.replace(target_path, aside)
            with contextlib.suppress(OSError):
                os.chmod(aside, 0o600)  # the corrupt original is still sensitive

        os.replace(tmp, target_path)
    except OSError as exc:
        return RestoreResult(False, "error", detail=f"placement failed: {exc}")

    placed = validate_file(target, home, backup_dir)
    mtime = datetime.fromtimestamp(src.stat().st_mtime, UTC).isoformat()
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


def resolve_passphrase(home: Path) -> str | None:
    """env → validated secrets.env → host escrow. The escrow is the exit for the
    circular case (secrets.env itself corrupt → its passphrase is unusable)."""
    env_pass = os.environ.get("GENESIS_BACKUP_PASSPHRASE", "").strip()
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
            val = _parse_dotenv(raw.decode("utf-8", "replace")).get(
                "GENESIS_BACKUP_PASSPHRASE", ""
            ).strip()
            if val:
                return val

    escrow = home / ".genesis/shared/guardian/backup_passphrase.env"
    if escrow.exists():
        try:
            val = _parse_dotenv(escrow.read_text()).get(
                "GENESIS_BACKUP_PASSPHRASE", ""
            ).strip()
            if val:
                return val
        except OSError:
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


def _default_backup_dir() -> Path:
    return _default_home() / "backups" / "genesis-backups"


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

    args = parser.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else _default_home()
    backup_dir = (
        Path(args.backup_dir).expanduser() if args.backup_dir else _default_backup_dir()
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
