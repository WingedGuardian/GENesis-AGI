"""Install-surface data-loss guards (deploy-audit B4/S1/S2/B10).

Three install/setup scripts could destroy user data on a re-run or a crash:

* **B4** `scripts/bootstrap.sh` rewrites the `~/.bashrc` genesis tmux-wrap block
  in place, non-atomically, and — when the END sentinel is missing (a
  half-written block) — deleted everything from BEGIN to EOF, taking any user
  content below it. Now: atomic temp+rename, and a missing END leaves the file
  untouched.
* **S1** `scripts/setup-local-config.sh` rebuilt `genesis.yaml` from a fresh
  literal dict on every re-run, wiping `github.private_repo` and any unmanaged
  key, with a non-atomic write. Now: load-merge existing + atomic write.
* **S2** the same script's `import yaml` aborted with a raw traceback on a
  minimal image lacking PyYAML. Now: read degrades to empty defaults, and a
  preflight fails fast with an actionable message before the write.
* **B10** `scripts/restore_cc_memory.sh` used `cp -an … || cp -a …`; coreutils
  9.1+ returns non-zero when `cp -an` skips an existing file, so the clobbering
  `cp -a` fallback overwrote the newer local files `-n` exists to protect. Now:
  rsync --ignore-existing (or `cp -an` best-effort), never a clobber.

The B4 heredoc is extracted from the shipped script and run directly; S1/B10 run
the real scripts in a sandbox; S2 is asserted on the shipped text (a missing
PyYAML is awkward to stage live).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
SETUP_LOCAL = REPO_ROOT / "scripts" / "setup-local-config.sh"
RESTORE_CC = REPO_ROOT / "scripts" / "restore_cc_memory.sh"


# ── B4: bashrc rewriter ──────────────────────────────────────────────


def _extract_bashrc_heredoc() -> str:
    """Pull the exact python heredoc that rewrites ~/.bashrc out of bootstrap.sh
    so the test runs the SHIPPED code, not a copy."""
    text = BOOTSTRAP.read_text()
    m = re.search(r"python3 - \"\$BASHRC\" <<'PYEOF'.*?\n(.*?)\nPYEOF", text, re.DOTALL)
    assert m, "could not find the bashrc-rewriter heredoc in bootstrap.sh"
    return m.group(1)


_BLOCK = "# >>> genesis tmux-wrap >>>\nclaude() { :; }\n# <<< genesis tmux-wrap <<<"


def _run_bashrc_rewriter(tmp_path, bashrc_text: str):
    src = tmp_path / "rewriter.py"
    src.write_text(_extract_bashrc_heredoc())
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(bashrc_text)
    proc = subprocess.run(
        [sys.executable, str(src), str(bashrc)],
        env={**os.environ, "GENESIS_TMUX_WRAP_BLOCK": _BLOCK},
        capture_output=True,
        text=True,
    )
    return proc, bashrc


def test_b4_atomic_and_endmarker_in_text():
    """Extraction: the rewriter is atomic (temp + os.replace) and refuses the
    END-missing swallow-to-EOF."""
    heredoc = _extract_bashrc_heredoc()
    assert "os.replace(" in heredoc and "mkstemp(" in heredoc
    assert "unterminated genesis tmux-wrap block" in heredoc
    assert 'open(path, "w").write' not in heredoc  # the old non-atomic write is gone
    assert "sys.exit(2)" in heredoc  # bail signals a distinct rc to the caller


def test_b4_bash_reports_bail_distinctly():
    """The outer bash must NOT print 'refreshed' on the missing-END bail: it
    captures the python rc and branches (0=refreshed, 2=left-untouched,
    other=warning)."""
    text = BOOTSTRAP.read_text()
    assert "<<'PYEOF' || _tw_rc=$?" in text  # captures rc without set -e aborting
    assert '[ "$_tw_rc" -eq 2 ]' in text and "left untouched" in text  # distinct bail message
    assert "(rc=$_tw_rc)" in text  # a genuine write error surfaces too


def test_b4_missing_end_preserves_user_content(tmp_path):
    """A damaged block (BEGIN, no END) with user content BELOW it must NOT be
    swallowed to EOF — the file is left untouched, warned about, and the
    rewriter exits with the distinct bail code (2), not 0."""
    bashrc = (
        "export USER_VAR=1\n"
        "# >>> genesis tmux-wrap >>>\n"
        "claude() { partial\n"  # damaged: no END sentinel
        "export IMPORTANT_USER_LINE=keep-me\n"
    )
    proc, out = _run_bashrc_rewriter(tmp_path, bashrc)
    assert proc.returncode == 2, proc.stderr
    assert "IMPORTANT_USER_LINE=keep-me" in out.read_text(), "user content was destroyed"
    assert out.read_text() == bashrc, "file must be byte-identical (untouched) on bail"
    assert "unterminated" in proc.stderr


def test_b4_wellformed_block_replaced(tmp_path):
    """A well-formed block is replaced in place; user content around it survives;
    the new block lands exactly once."""
    bashrc = (
        "export BEFORE=1\n"
        "# >>> genesis tmux-wrap >>>\n"
        "claude() { OLD; }\n"
        "# <<< genesis tmux-wrap <<<\n"
        "export AFTER=2\n"
    )
    proc, out = _run_bashrc_rewriter(tmp_path, bashrc)
    assert proc.returncode == 0, proc.stderr
    txt = out.read_text()
    assert "export BEFORE=1" in txt and "export AFTER=2" in txt
    assert "OLD" not in txt  # old block body replaced
    assert txt.count("# >>> genesis tmux-wrap >>>") == 1


# ── S1 / S2: setup-local-config.sh ───────────────────────────────────


def _run_setup_local(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Ensure the script's bare `python3` resolves to an interpreter WITH yaml
    # (the venv running these tests).
    env["HOME"] = str(home)
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(SETUP_LOCAL), "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    cfg = home / ".genesis" / "config" / "genesis.yaml"
    return proc, cfg


def test_s1_preserves_private_repo_and_unknown_keys(tmp_path):
    """A re-run must preserve github.private_repo (never prompted) and any key a
    user/other tool added — the old code wiped both."""
    import yaml

    proc0, cfg = _run_setup_local(tmp_path)
    assert proc0.returncode == 0, f"{proc0.stdout}\n{proc0.stderr}"
    # Seed values the managed write must not clobber.
    data = yaml.safe_load(cfg.read_text())
    data["github"]["private_repo"] = "my-private-backups"
    data["custom_user_key"] = {"nested": "keepme"}
    cfg.write_text(yaml.safe_dump(data))
    # Re-run (non-interactive → managed leaves get defaults, others untouched).
    proc1, _ = _run_setup_local(tmp_path)
    assert proc1.returncode == 0, f"{proc1.stdout}\n{proc1.stderr}"
    after = yaml.safe_load(cfg.read_text())
    assert after["github"]["private_repo"] == "my-private-backups", after
    assert after["custom_user_key"] == {"nested": "keepme"}, after
    assert "github" in after and "network" in after and "timezone" in after


def test_s1_atomic_write_in_text():
    """Extraction: the config write is atomic (temp + os.replace) and load-merges
    (not a fresh literal dict)."""
    text = SETUP_LOCAL.read_text()
    assert "os.replace(" in text and "mkstemp(" in text
    assert 'gh.setdefault("private_repo"' in text  # preserve, not overwrite
    assert (
        "yaml.safe_load(f) or {}" in text.split("# ── Write config", 1)[1]
    )  # load in the WRITE heredoc


def test_s2_yaml_preflight_and_guarded_read():
    """Extraction: the write path preflights PyYAML with an actionable message,
    and the read path's `import yaml` is inside the try (degrades to empty)."""
    text = SETUP_LOCAL.read_text()
    assert "python3 -c 'import yaml'" in text
    assert "python3-yaml" in text  # actionable install hint
    # The read heredoc guards its import inside the try (degrade to empty); the
    # write heredoc imports yaml at top-level (needs it — preflighted above).
    # This exact pattern is unique to the guarded read path.
    assert "try:\n    import yaml" in text


# ── B10: restore_cc_memory.sh ────────────────────────────────────────


def test_b10_never_clobbers_newer_local(tmp_path):
    """A newer local memory file must survive the restore — the backup's older
    copy must NOT overwrite it (the old `|| cp -a` clobbered)."""
    genesis_root = tmp_path / "genesis"
    backup = genesis_root / "data" / "cc-memory-backup"
    backup.mkdir(parents=True)
    (backup / "note.md").write_text("OLD backup content\n")
    (backup / "only-in-backup.md").write_text("fresh from backup\n")

    home = tmp_path / "home"
    cc_id = str(genesis_root).replace("/", "-")
    mem = home / ".claude" / "projects" / cc_id / "memory"
    mem.mkdir(parents=True)
    (mem / "note.md").write_text("NEW local content — keep me\n")  # newer, must survive

    proc = subprocess.run(
        ["bash", str(RESTORE_CC), str(genesis_root)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert (mem / "note.md").read_text() == "NEW local content — keep me\n", (
        "local file was clobbered"
    )
    assert (mem / "only-in-backup.md").read_text() == "fresh from backup\n", (
        "missing file not restored"
    )


def test_b10_no_clobber_fallback_in_text():
    """Extraction: the clobbering bare `cp -a` fallback is gone from the CODE
    (comments may still describe it)."""
    code = "\n".join(
        ln for ln in RESTORE_CC.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "cp -a " not in code, code  # bare clobbering cp -a (cp -an is fine — trailing 'n')
    assert "--ignore-existing" in code
