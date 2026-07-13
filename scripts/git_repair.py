#!/usr/bin/env python3
"""Operator-invoked git-metadata repair — recovers a corrupt local `.git`.

Companion to the F.1 detectors (``genesis.observability.git_health`` +
``genesis.guardian.git_watch``). Those alerts say "run scripts/git_repair.py";
this is that tool. It repairs the incident class from the 2026-07-03 thin-pool
outage: an ext4 ``data=ordered`` journal replay preserved file *structure* while
zeroing *unflushed data blocks*, so ``.git/config``, ``packed-refs``, and ~30
loose objects read back as NUL with ZERO git-level error — silently disabling
the guardian's ``REVERT_CODE`` recovery lever.

NEVER auto-fired. It can, at the extreme, guide a ``.git`` swap, so a human runs
it. It is **stdlib-only** (no ``genesis`` imports) and targets **system
python3** so it survives a broken venv, and it is **dry-run by default** —
``--apply`` gates every mutation.

Repair ladder (re-diagnose after each rung; stop when healthy):

  0. CAPTURE first — ``capture_recovery_state.sh`` best-effort + our own
     git-independent raw copies of ``.git/{config,HEAD,packed-refs}`` + a refs
     listing. ``--apply`` is REFUSED if the capture dir is unwritable.
  1. DIAGNOSE — config validity (``git config --list`` rc, not a hand parse),
     HEAD resolvable, refs readable, zeroed-loose-object scan, ``fsck --full``
     (content-level; enumerates ALL corrupt objects), ``ls-remote`` reachability.
  a. RESTORE ``.git/config`` (+ zeroed ``.git/HEAD``) from a GENERATED template.
     URL chain: existing config → ``--remote-url`` → ``GENESIS_REPO_URL`` env →
     the rung-0 raw capture → abort. Backup-sourced URL is rejected (circular).
  b. QUARANTINE corrupt loose objects → ``.git/RECOVERY-corrupt-objects/<ts>/``.
     Loose objects are mode 0444 → we MOVE them (never overwrite in place).
  c. REFETCH — ``git fetch --refetch origin`` (a plain fetch does NOT backfill a
     quarantined object; only ``--refetch`` does) + repair branch tips from
     reflog (ref-only; never touches the worktree/index).
  d. REPACK — ``git repack -a -d`` (no ``--prune=now``), and ONLY after a re-fsck
     shows the store complete (repack hard-fails on a missing reachable object).
  e. GUIDED RE-CLONE (``--allow-reclone``, last resort) — clone + fsck-verify a
     fresh copy, then PRINT the exact operator steps for the ``.git`` swap. The
     tool NEVER swaps ``.git`` itself: this repo has many linked worktrees whose
     gitdir pointers live inside the main ``.git``; a swap orphans them, so a
     human is the final gate on that irreversible move.

Exit: 0 healthy · 1 residual issues remain · 2 aborted (no capture / no URL).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HOME = Path.home()
LOG_DIR = HOME / ".genesis" / "logs"

# Timeouts — each justified by a concrete failure mode, not "defense in depth":
#   fsck --full recomputes SHA-1 over the whole object store; measured ~6s on the
#   ~82MB .123 store, 3600s = ~600x headroom for a scarred pool under IO stress.
#   --refetch re-downloads full history (~100MB); 1800s covers a slow link.
#   ls-remote is a single network round-trip; 60s catches a dead origin fast.
_FSCK_TIMEOUT_S = 3600
_FETCH_TIMEOUT_S = 1800
_LSREMOTE_TIMEOUT_S = 60
_PLUMBING_TIMEOUT_S = 30  # local rev-parse/config/for-each-ref are ms-scale

_HEX40 = re.compile(r"\b[0-9a-f]{40}\b")
_OBJ_PATH = re.compile(r"\.git/objects/[0-9a-f]{2}/[0-9a-f]{38}")


# ─── Small subprocess + logging helpers ──────────────────────────────────────


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "git_repair.log").open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _git(repo: Path, *args: str, timeout: int = _PLUMBING_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run ``git -C <repo> <args>`` capturing text output. Never raises on a
    non-zero exit (the caller inspects ``.returncode``); a timeout/OSError is
    surfaced as a synthetic returncode 124/127 so callers stay branch-simple."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timeout after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


# ─── Diagnosis ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Diagnosis:
    checks: list[Check] = field(default_factory=list)
    corrupt_objects: list[Path] = field(default_factory=list)  # loose object paths

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "OK  " if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        return "\n".join(lines)


def _scan_zeroed_loose(git_dir: Path) -> list[Path]:
    """Return loose object files whose contents are entirely NUL (the outage
    fingerprint). Fast pre-check; ``fsck --full`` is the authoritative detector,
    but this pinpoints the exact files without parsing fsck text."""
    zeroed: list[Path] = []
    objroot = git_dir / "objects"
    if not objroot.is_dir():
        return zeroed
    for sub in objroot.iterdir():
        if not (sub.is_dir() and len(sub.name) == 2 and _HEX40.fullmatch(sub.name + "0" * 38)):
            continue  # skip pack/, info/, non-fanout dirs
        for obj in sub.iterdir():
            try:
                data = obj.read_bytes()
            except OSError:
                continue
            if data and not any(data):  # non-empty and all bytes zero
                zeroed.append(obj)
    return zeroed


def _fsck_corrupt_paths(fsck_stderr: str, git_dir: Path) -> list[Path]:
    """Extract loose-object filesystem paths flagged corrupt by ``fsck --full``.

    fsck prints e.g. ``error: unable to unpack header of .git/objects/22/3b..``
    and ``error: <sha>: object corrupt or missing: .git/objects/22/3b..`` — we
    take the concrete ``.git/objects/..`` paths (robust) plus resolve bare
    40-hex ``object corrupt`` SHAs to their loose path if present on disk."""
    paths: set[Path] = set()
    repo_root = git_dir.parent
    for m in _OBJ_PATH.finditer(fsck_stderr):
        p = (repo_root / m.group(0)).resolve()
        if p.exists():
            paths.add(p)
    for line in fsck_stderr.splitlines():
        if "object corrupt or missing" in line or "object corrupt" in line:
            for sha in _HEX40.findall(line):
                cand = git_dir / "objects" / sha[:2] / sha[2:]
                if cand.exists():
                    paths.add(cand.resolve())
    return sorted(paths)


def diagnose(repo: Path) -> Diagnosis:
    """Read-only health assessment of ``repo``'s git metadata."""
    git_dir = repo / ".git"
    d = Diagnosis()

    # config validity — use git's own parser (git-config syntax is NOT INI:
    # subsections/continuations/includes make hand-parsing subtly wrong). This
    # is the same primitive the F.1b guardian probe settled on.
    cfg = git_dir / "config"
    cfg_ok = cfg.is_file() and cfg.stat().st_size > 0
    if cfg_ok:
        r = _git(repo, "config", "--list")
        cfg_ok = r.returncode == 0 and bool(r.stdout.strip())
    d.add("config parses", cfg_ok, "" if cfg_ok else f"{cfg} zeroed/unparseable")

    # HEAD resolvable
    head_txt = git_dir / "HEAD"
    head_ok = head_txt.is_file() and head_txt.stat().st_size > 0
    if head_ok:
        r = _git(repo, "rev-parse", "HEAD")
        head_ok = r.returncode == 0
    d.add("HEAD resolvable", head_ok, "" if head_ok else "HEAD zeroed or unresolvable")

    # refs readable
    r = _git(repo, "for-each-ref")
    d.add("refs readable", r.returncode == 0, "" if r.returncode == 0 else r.stderr.strip()[:120])

    # zeroed loose objects (fast fingerprint scan)
    zeroed = _scan_zeroed_loose(git_dir)
    d.add(
        "no zeroed loose objects",
        not zeroed,
        "" if not zeroed else f"{len(zeroed)} all-NUL loose object(s)",
    )

    # fsck --full — authoritative content-level scan, enumerates ALL corrupt
    r = _git(repo, "fsck", "--full", "--no-progress", timeout=_FSCK_TIMEOUT_S)
    # fsck exits non-zero on corruption; "dangling"/"notice" lines are normal.
    fsck_errs = [
        ln
        for ln in r.stderr.splitlines()
        if ln.startswith(("error:", "fatal:")) or "corrupt" in ln or "missing" in ln
    ]
    fsck_ok = r.returncode == 0 and not fsck_errs
    d.add(
        "fsck --full clean",
        fsck_ok,
        ""
        if fsck_ok
        else f"{len(fsck_errs)} error line(s); e.g. {fsck_errs[0][:100]}"
        if fsck_errs
        else f"rc={r.returncode}",
    )

    # merge fsck-reported corrupt paths with the zero-scan for the quarantine set
    corrupt = set(zeroed) | set(_fsck_corrupt_paths(r.stderr, git_dir))
    d.corrupt_objects = sorted(corrupt)

    # origin reachability (needed for refetch/reclone)
    r = _git(repo, "ls-remote", "--exit-code", "origin", "HEAD", timeout=_LSREMOTE_TIMEOUT_S)
    d.add(
        "origin reachable",
        r.returncode == 0,
        "" if r.returncode == 0 else "cannot reach origin (refetch/reclone unavailable)",
    )

    return d


# ─── Rung 0: capture ─────────────────────────────────────────────────────────


def capture_state(repo: Path, *, apply: bool) -> Path | None:
    """Snapshot recovery baseline BEFORE any mutation. Returns the capture dir,
    or None if it could not be created (which, under ``--apply``, is fatal).

    Does its OWN git-independent raw copies (they survive a corrupt repo) AND
    invokes ``capture_recovery_state.sh`` best-effort (it aborts on a broken
    repo — see the ``|| true`` hardening — so we tolerate a partial result)."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = HOME / "tmp" / f"genesis-git-repair-{ts}"
    raw = out / "raw"
    try:
        raw.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"CAPTURE FAILED: cannot create {raw}: {exc}")
        return None

    git_dir = repo / ".git"
    for rel in ("config", "HEAD", "packed-refs"):
        src = git_dir / rel
        if src.exists():
            try:
                shutil.copy2(src, raw / rel)
            except OSError as exc:
                _log(f"capture: could not copy {rel}: {exc}")
    # a plain filesystem listing of refs/ (no git needed)
    refs = git_dir / "refs"
    if refs.is_dir():
        try:
            with (raw / "refs_listing.txt").open("w") as fh:
                for p in sorted(refs.rglob("*")):
                    if p.is_file():
                        fh.write(f"{p.relative_to(git_dir)}\n")
        except OSError:
            pass

    # best-effort richer capture via the shell script (never fatal)
    script = repo / "scripts" / "capture_recovery_state.sh"
    if script.exists():
        try:
            subprocess.run(
                ["bash", str(script), str(out / "recovery_state")],
                capture_output=True,
                timeout=120,
                env={**os.environ, "GENESIS_REPO": str(repo)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"capture: capture_recovery_state.sh best-effort failed: {exc}")

    _log(f"CAPTURE ok → {out}")
    return out


# ─── Rung a: restore config + HEAD ───────────────────────────────────────────


def _resolve_url(repo: Path, *, remote_url: str | None, capture: Path | None) -> str | None:
    """First hit wins: existing config → --remote-url → env → raw capture."""
    r = _git(repo, "config", "--get", "remote.origin.url")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    if remote_url:
        return remote_url
    env = os.environ.get("GENESIS_REPO_URL")
    if env:
        return env
    if capture:
        cap_cfg = capture / "raw" / "config"
        if cap_cfg.is_file():
            for line in cap_cfg.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("url ="):
                    return line.split("=", 1)[1].strip()
    return None


def _detect_branch(repo: Path, capture: Path | None) -> str:
    """Best-effort default branch for a regenerated HEAD."""
    if capture:
        cap_head = capture / "raw" / "HEAD"
        if cap_head.is_file():
            txt = cap_head.read_text(errors="ignore").strip()
            if txt.startswith("ref: refs/heads/"):
                return txt.rsplit("/", 1)[-1]
    return "main"


def restore_config(repo: Path, url: str, branch: str, *, apply: bool) -> None:
    git_dir = repo / ".git"
    cfg_text = (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
        '[remote "origin"]\n'
        f"\turl = {url}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        f'[branch "{branch}"]\n'
        "\tremote = origin\n"
        f"\tmerge = refs/heads/{branch}\n"
    )
    if not apply:
        _log(f"WOULD RESTORE .git/config (url={url}, branch={branch})")
    else:
        (git_dir / "config").write_text(cfg_text)
        _log(f"RESTORED .git/config (url={url}, branch={branch})")

    # zeroed/unresolvable HEAD → generated one-liner symref
    head = git_dir / "HEAD"
    head_bad = (
        (not head.is_file())
        or head.stat().st_size == 0
        or _git(repo, "rev-parse", "--symbolic-full-name", "HEAD").returncode != 0
    )
    if head_bad:
        if not apply:
            _log(f"WOULD RESTORE .git/HEAD → ref: refs/heads/{branch}")
        else:
            head.write_text(f"ref: refs/heads/{branch}\n")
            _log(f"RESTORED .git/HEAD → ref: refs/heads/{branch}")


# ─── Rung b: quarantine ──────────────────────────────────────────────────────


def quarantine_objects(repo: Path, objs: list[Path], *, apply: bool) -> int:
    """MOVE corrupt loose objects aside (they are 0444 — never overwrite in
    place). Returns count quarantined."""
    if not objs:
        return 0
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = repo / ".git" / "RECOVERY-corrupt-objects" / ts
    n = 0
    for obj in objs:
        if not apply:
            _log(f"WOULD QUARANTINE {obj}")
            n += 1
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(obj), str(dest / obj.name))  # move handles 0444
            n += 1
        except OSError as exc:
            _log(f"quarantine FAILED for {obj}: {exc}")
    _log(
        f"{'WOULD QUARANTINE' if not apply else 'QUARANTINED'} {n} object(s)"
        + (f" → {dest}" if apply else "")
    )
    return n


# ─── Rung c: refetch + reflog tip repair ─────────────────────────────────────


def refetch(repo: Path, *, apply: bool) -> bool:
    """``git fetch --refetch origin`` — a plain fetch will NOT backfill a
    quarantined object; only --refetch re-downloads it. Returns True on success."""
    if not apply:
        _log("WOULD REFETCH: git fetch --refetch origin")
        return True
    _log("REFETCH: git fetch --refetch origin (re-downloads full history) …")
    r = _git(repo, "fetch", "--refetch", "origin", timeout=_FETCH_TIMEOUT_S)
    ok = r.returncode == 0
    _log(f"REFETCH {'ok' if ok else 'FAILED'}" + ("" if ok else f": {r.stderr.strip()[:160]}"))
    return ok


# ─── Rung d: repack ──────────────────────────────────────────────────────────


def repack(repo: Path, *, apply: bool) -> None:
    """``git repack -a -d`` (no --prune=now). Guarded by a re-fsck upstream:
    repack HARD-FAILS on a missing reachable object, so callers only invoke this
    once the store is complete again."""
    if not apply:
        _log("WOULD REPACK: git repack -a -d")
        return
    r = _git(repo, "repack", "-a", "-d", timeout=_FETCH_TIMEOUT_S)
    if r.returncode == 0:
        _log("REPACKED: git repack -a -d")
    else:
        _log(f"REPACK skipped/failed (store still incomplete?): {r.stderr.strip()[:160]}")


# ─── Rung e: guided re-clone (PRINTS steps; never swaps .git) ─────────────────


def guided_reclone(repo: Path, url: str, *, apply: bool) -> None:
    """Clone + fsck-verify a fresh copy, then PRINT the operator steps for the
    ``.git`` swap. Never executes the swap — this repo has linked worktrees whose
    gitdir pointers live in the main ``.git``; a swap orphans them, so a human is
    the final gate. The fresh clone + fsck ARE performed (read-only wrt the real
    repo) so the printed runbook is proven before it's handed over."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    tmp = HOME / "tmp" / f"genesis-reclone-{ts}"
    _log("")
    _log("═══ RUNG e: GUIDED RE-CLONE (last resort) ═══")

    # enumerate linked worktrees + local-only branches at risk BEFORE anything
    wt = _git(repo, "worktree", "list", "--porcelain")
    linked = [
        ln.split(" ", 1)[1]
        for ln in wt.stdout.splitlines()
        if ln.startswith("worktree ") and Path(ln.split(" ", 1)[1]).resolve() != repo.resolve()
    ]
    if linked:
        _log(f"⚠ {len(linked)} LINKED WORKTREE(S) will be orphaned by a .git swap:")
        for w in linked:
            _log(f"    {w}")
        _log("  Each must be re-created after the swap (steps below). Any uncommitted")
        _log("  work in them is recoverable only from the set-aside .git-broken dir.")

    _log(f"Cloning a fresh, verified copy to {tmp} …")
    if not apply:
        _log(f"WOULD CLONE --no-checkout {url} {tmp} and fsck-verify it")
        _log("WOULD THEN PRINT the swap steps (never executed automatically).")
        return
    tmp.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "clone", "--no-checkout", url, str(tmp)],
        capture_output=True,
        text=True,
        timeout=_FETCH_TIMEOUT_S,
    )
    if r.returncode != 0:
        _log(f"reclone FAILED at clone: {r.stderr.strip()[:160]}")
        return
    fr = _git(tmp, "fsck", "--full", "--no-progress", timeout=_FSCK_TIMEOUT_S)
    if fr.returncode != 0:
        _log("reclone ABORTED: the fresh clone did not fsck clean — do NOT swap.")
        return
    _log("Fresh clone fsck: CLEAN. It is safe to swap in. RUN THESE STEPS BY HAND:")
    broken = f"{repo}/../.git-broken-{ts}"
    print(f"""
  # 1. Move the corrupt .git OUTSIDE the worktree (avoids ?? clutter / stray add):
  mv {repo}/.git {broken}
  # 2. Install the verified fresh database:
  mv {tmp}/.git {repo}/.git
  # 3. Repopulate the index from HEAD WITHOUT touching your working files:
  git -C {repo} reset --mixed HEAD
  # 4. Re-create each linked worktree (they were orphaned by the swap):
  git -C {repo} worktree prune""")
    for w in linked:
        print(f"  #    git -C {repo} worktree add {w} <branch>   # was: {w}")
    print(f"""  # 5. Verify:
  git -C {repo} fsck --full && git -C {repo} status
  # If anything is wrong, your original is intact at {broken}
""")


# ─── Orchestration ───────────────────────────────────────────────────────────


def repair(repo: Path, *, apply: bool, remote_url: str | None, allow_reclone: bool) -> int:
    _log(f"git_repair starting on {repo} (mode={'APPLY' if apply else 'DRY-RUN'})")

    if (repo / ".git").is_file():
        _log(
            "ABORT: .git is a FILE — this looks like a linked worktree. Run "
            "git_repair against the MAIN repository (the one with a .git DIR)."
        )
        return 2
    if not (repo / ".git").is_dir():
        _log(f"ABORT: {repo}/.git is not a directory — not a git repo.")
        return 2

    # Rung 0 — capture (fatal only under --apply)
    capture = capture_state(repo, apply=apply)
    if apply and capture is None:
        _log("ABORT: cannot write a recovery capture; refusing to mutate blind.")
        return 2

    d = diagnose(repo)
    _log("Diagnosis:\n" + d.render())
    if d.healthy:
        _log("HEALTHY — no repair needed.")
        return 0
    if not apply:
        _log(
            "DRY-RUN: issues found above. Re-run with --apply to repair "
            "(add --allow-reclone to enable the last-resort guided re-clone)."
        )
        # still walk the ladder in dry-run to SHOW what it would do
        _dry_run_plan(repo, d, remote_url=remote_url, capture=capture, allow_reclone=allow_reclone)
        return 1

    branch = _detect_branch(repo, capture)

    # Rung a — config/HEAD
    if not any(c.name == "config parses" and c.ok for c in d.checks) or not any(
        c.name == "HEAD resolvable" and c.ok for c in d.checks
    ):
        url = _resolve_url(repo, remote_url=remote_url, capture=capture)
        if not url:
            _log(
                "ABORT: config is corrupt and no origin URL could be resolved "
                "(pass --remote-url or set GENESIS_REPO_URL)."
            )
            return 2
        restore_config(repo, url, branch, apply=True)
        d = diagnose(repo)
        _log("Post-config diagnosis:\n" + d.render())

    # Rung b — quarantine
    if d.corrupt_objects:
        quarantine_objects(repo, d.corrupt_objects, apply=True)
        d = diagnose(repo)

    # Rung c — refetch (only if origin reachable; refetch() short-circuits so its
    # side effect fires only when the store is unhealthy AND origin is reachable)
    if (
        not d.healthy
        and any(c.name == "origin reachable" and c.ok for c in d.checks)
        and refetch(repo, apply=True)
    ):
        d = diagnose(repo)
        _log("Post-refetch diagnosis:\n" + d.render())

    # Rung d — repack (only once the store is complete again)
    if any(c.name == "fsck --full clean" and c.ok for c in d.checks):
        repack(repo, apply=True)
        d = diagnose(repo)

    # Rung e — guided reclone (last resort, gated)
    if not d.healthy and allow_reclone:
        url = _resolve_url(repo, remote_url=remote_url, capture=capture)
        if url:
            guided_reclone(repo, url, apply=True)

    d = diagnose(repo)
    _log("Final diagnosis:\n" + d.render())
    if d.healthy:
        _log("REPAIRED — fsck clean.")
        return 0
    _log(
        "RESIDUAL issues remain."
        + ("" if allow_reclone else " Consider re-running with --allow-reclone.")
        + " See the guided steps above / escalate."
    )
    return 1


def _dry_run_plan(
    repo: Path, d: Diagnosis, *, remote_url: str | None, capture: Path | None, allow_reclone: bool
) -> None:
    """Show the ladder the tool WOULD walk, without mutating anything."""
    branch = _detect_branch(repo, capture)
    if not all(c.ok for c in d.checks if c.name in ("config parses", "HEAD resolvable")):
        url = _resolve_url(repo, remote_url=remote_url, capture=capture)
        if url:
            restore_config(repo, url, branch, apply=False)
        else:
            _log("WOULD ABORT: no origin URL resolvable for config restore.")
    if d.corrupt_objects:
        quarantine_objects(repo, d.corrupt_objects, apply=False)
    refetch(repo, apply=False)
    repack(repo, apply=False)
    if allow_reclone:
        url = _resolve_url(repo, remote_url=remote_url, capture=capture)
        if url:
            guided_reclone(repo, url, apply=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Operator-invoked repair for a corrupt local .git (dry-run by default)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually repair (default is dry-run / report only)"
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(os.environ.get("GENESIS_REPO", HOME / "genesis")),
        help="Repository to repair (default: $GENESIS_REPO or ~/genesis)",
    )
    parser.add_argument(
        "--remote-url", default=None, help="Origin URL to use when .git/config is unrecoverable"
    )
    parser.add_argument(
        "--allow-reclone",
        action="store_true",
        help="Enable the last-resort GUIDED re-clone (prints .git-swap "
        "steps; never swaps automatically)",
    )
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    return repair(
        repo, apply=args.apply, remote_url=args.remote_url, allow_reclone=args.allow_reclone
    )


if __name__ == "__main__":
    sys.exit(main())
