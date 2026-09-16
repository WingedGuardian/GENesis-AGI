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
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

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
    """Extraction: the clobbering `cp -an … || cp -a …` fallback is gone, and the
    two modern paths (rsync, cp --update=none) propagate real errors via exit
    code (no `|| true` masking) — the caller keys _CCMEM_RESTORED on our status."""
    code = "\n".join(
        ln for ln in RESTORE_CC.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "|| cp -a" not in code and "|| \\" not in code  # the old clobber fallback is gone
    assert "--ignore-existing" in code  # rsync no-clobber
    assert "--update=none" in code  # coreutils 9.3+ stable no-clobber (proper exit codes)
    # The only best-effort `|| true` is the pre-9.3 cp -an fallback (last resort).
    assert code.count("|| true") <= 1


# ── S3: install.sh's Auto-cd hook rewriter ───────────────────────────────────
#
# The guard used to be `! grep -q <marker>`, so an install that already had the
# hook never received the repo-path fix: its login line kept pointing at
# wherever the repo used to be, and `cd` into a missing directory is what the
# fix existed to prevent. Rewriting it means editing ~/.bashrc in place, which
# every interactive shell sources — so the write is atomic and the shapes it
# does NOT recognise are refused rather than guessed at.

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
_AUTOCD_MARK = "# Auto-cd to Genesis project on login"


def _extract_autocd_rewriter() -> str:
    """Pull the shipped Auto-cd rewriter out of install.sh — the real program."""
    text = INSTALL_SH.read_text()
    m = re.search(
        r"python3 - \"\$HOME/\.bashrc\" <<'PYEOF'.*?\n(.*?)\nPYEOF", text, re.DOTALL
    )
    assert m, "could not find the Auto-cd rewriter heredoc in install.sh"
    prog = m.group(1)
    assert "GENESIS_AUTOCD_MARK" in prog, "matched the wrong heredoc"
    return prog


def _shipped_autocd_line(repo: str) -> str:
    """Build the hook line with install.sh's OWN construction, not a lookalike.

    An earlier version of this helper used `shlex.quote`. That is a different
    producer from the shipped `printf %q` — measured on bash 5.2.21 they emit
    different text for the same hostile path — so the test proved a quoting
    scheme EXISTS rather than that install.sh uses it, and reverting line 795 to
    a raw $REPO_DIR left this whole file green. Same shape as the sed-escape
    miss on this branch: a mechanism verified in isolation binds nothing.
    """
    m = re.search(r"^\s*(_AUTOCD_LINE=.*)$", INSTALL_SH.read_text(), re.MULTILINE)
    assert m, "could not extract _AUTOCD_LINE from install.sh — stale"
    built = subprocess.run(
        ["bash", "-c",
         f'REPO_DIR="$1"\n{m.group(1).strip()}\nprintf "%s" "$_AUTOCD_LINE"', "_", repo],
        capture_output=True, text=True, check=True,
    )
    return built.stdout


def _run_autocd(tmp_path, bashrc_text: str, repo: str):
    src = tmp_path / "autocd.py"
    src.write_text(_extract_autocd_rewriter())
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(bashrc_text)
    proc = subprocess.run(
        [sys.executable, str(src), str(bashrc)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GENESIS_AUTOCD_MARK": _AUTOCD_MARK,
            "GENESIS_AUTOCD_LINE": _shipped_autocd_line(repo),
        },
    )
    return proc, bashrc.read_text()


def test_autocd_rewrites_a_stale_repo_path(tmp_path):
    """The defect: an existing install keeps pointing at the old checkout."""
    proc, out = _run_autocd(
        tmp_path,
        f"{_AUTOCD_MARK}\n[ -d /old/path ] && cd /old/path\nexport KEEPME=yes\n",
        "/new/repo",
    )
    assert proc.returncode == 0, proc.stderr
    assert "[ -d /new/repo ] && cd /new/repo" in out
    assert "/old/path" not in out
    # Everything below the hook is the user's and must survive the rewrite.
    assert "export KEEPME=yes" in out


def test_autocd_leaves_an_already_correct_hook_alone(tmp_path):
    """rc 10 means "nothing to do" — not an error, and not a rewrite."""
    text = f"{_AUTOCD_MARK}\n[ -d /new/repo ] && cd /new/repo\n"
    proc, out = _run_autocd(tmp_path, text, "/new/repo")
    assert proc.returncode == 10, proc.stderr
    assert out == text, "an already-correct hook was rewritten anyway"


@pytest.mark.parametrize(
    "body",
    [
        f'{_AUTOCD_MARK}\nalias ll="ls -l"\n',  # marker, then something else
        f"{_AUTOCD_MARK}\n",  # marker is the last line
    ],
)
def test_autocd_refuses_a_shape_it_did_not_write(tmp_path, body):
    """Refuse rather than guess which line to overwrite.

    ~/.bashrc is sourced by every interactive shell. Overwriting a line this
    installer did not write is worse than leaving a stale path alone, so the
    unrecognised shapes exit with a distinct rc the caller turns into a warning.
    """
    proc, out = _run_autocd(tmp_path, body, "/new/repo")
    assert proc.returncode == 4, proc.stderr
    assert out == body, "refused, but the file changed anyway"


def test_autocd_survives_a_repo_path_full_of_shell_metacharacters(tmp_path):
    """The path crosses via the ENVIRONMENT and is %q-quoted by the caller.

    A path containing a quote and a semicolon must land as data — the resulting
    .bashrc has to still parse, or every future login breaks.
    """
    # A real directory whose NAME carries the metacharacters, so the assertion
    # can be about where the shell ENDS UP rather than about text.
    hostile_dir = tmp_path / 'x"; echo PWNED; echo "'
    hostile_dir.mkdir()
    hostile = str(hostile_dir)
    proc, out = _run_autocd(
        tmp_path, f"{_AUTOCD_MARK}\n[ -d /old ] && cd /old\n", hostile
    )
    assert proc.returncode == 0, proc.stderr
    written = tmp_path / ".bashrc"

    # PARSING is not the property that matters, and asserting it is how this
    # test used to pass on a broken line: with a RAW $REPO_DIR the quotes still
    # balance across the line, so `bash -n` is happy while the shell executes
    # something else entirely. MEASURED — reverting the %q left this green.
    # So SOURCE it and ask where the shell actually went, and whether anything
    # else ran.
    # PWD goes to its own FILE so stdout stays free to catch anything the line
    # executed. Grepping the combined output for a marker cannot work here —
    # the marker is part of the directory NAME, so it appears in a correct PWD
    # too, and an earlier version of this assertion failed on exactly that.
    landed = tmp_path / "landed.txt"
    probe = subprocess.run(
        ["bash", "-c",
         f'cd /; . {shlex.quote(str(written))}; printf "%s" "$PWD" > {shlex.quote(str(landed))}'],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, f"sourcing the rewritten .bashrc failed: {probe.stderr}"
    assert landed.read_text() == hostile, (
        f"the hook did not cd to the intended directory.\n"
        f"  wanted: {hostile!r}\n  landed: {landed.read_text()!r}\n{out}"
    )
    assert probe.stdout == "" and probe.stderr == "", (
        f"the path escaped its quoting and executed something:\n"
        f"  stdout: {probe.stdout!r}\n  stderr: {probe.stderr!r}"
    )


def test_autocd_write_is_atomic_and_the_caller_captures_rc():
    """Same contract as the tmux-wrap rewriter: temp + rename, rc not swallowed."""
    prog = _extract_autocd_rewriter()
    assert "mkstemp(" in prog and "os.replace(" in prog, "the write is not atomic"
    text = INSTALL_SH.read_text()
    assert "<<'PYEOF' || _ac_rc=$?" in text, (
        "the rc is not captured, so `set -e` would abort before the warning"
    )


def test_autocd_does_not_destroy_a_symlinked_bashrc(tmp_path):
    """A ~/.bashrc symlinked into a dotfiles repo must stay a symlink.

    MEASURED before the fix: `os.replace` on the LINK swapped it for a regular
    file. The dotfiles repo kept the STALE path, the next `stow -R` / `chezmoi
    apply` reverted or conflicted, and the installer printed success — in the
    file this module exists to prevent.
    """
    real = tmp_path / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text(f"{_AUTOCD_MARK}\n[ -d /old ] && cd /old\nexport KEEPME=1\n")
    link = tmp_path / ".bashrc"
    link.symlink_to(real)

    src = tmp_path / "autocd.py"
    src.write_text(_extract_autocd_rewriter())
    proc = subprocess.run(
        [sys.executable, str(src), str(link)],
        capture_output=True, text=True,
        env={**os.environ,
             "GENESIS_AUTOCD_MARK": _AUTOCD_MARK,
             "GENESIS_AUTOCD_LINE": _shipped_autocd_line("/new/repo")},
    )
    assert proc.returncode == 0, proc.stderr
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    # The edit must land in the REAL file, which is what the dotfiles repo tracks.
    assert "cd /new/repo" in real.read_text()
    assert "export KEEPME=1" in real.read_text()


@pytest.mark.parametrize(
    "ch", [" ", " ", "\x85", "\x0c", "\x0b", "\x1c"]
)
def test_autocd_does_not_rewrite_line_separators_bash_ignores(tmp_path, ch):
    """`splitlines()` breaks on characters that do NOT end a line for bash.

    Round-tripping through it turned each into a newline ANYWHERE in the file,
    including content the rewriter was never asked to touch — and outside a
    quoted string that splits one command into two, so the user gets
    `command not found` on every login. `bash -n` passes throughout, so nothing
    else would have caught it. MEASURED: 6 of 7 such characters mutated.
    """
    payload = f'export MSG="a{ch}b"\n'
    proc, out = _run_autocd(
        tmp_path,
        f"{_AUTOCD_MARK}\n[ -d /old ] && cd /old\n{payload}",
        "/new/repo",
    )
    assert proc.returncode == 0, proc.stderr
    assert payload in out, (
        f"user content was mutated: {ch!r} was rewritten as a newline"
    )


def test_autocd_refuses_when_two_hooks_exist(tmp_path):
    """bash runs top to bottom, so the LAST hook decides the final cd.

    Rewriting only the first would leave the effective login directory stale
    while the installer reported success — a false claim in the install log.
    """
    body = (f"{_AUTOCD_MARK}\n[ -d /old1 ] && cd /old1\n"
            f"{_AUTOCD_MARK}\n[ -d /old2 ] && cd /old2\n")
    proc, out = _run_autocd(tmp_path, body, "/new/repo")
    assert proc.returncode == 4, proc.stderr
    assert out == body, "refused, but the file changed anyway"


def test_autocd_preserves_file_mode(tmp_path):
    """mkstemp creates 0600; the rewrite must not silently re-permission a
    user file it was only asked to edit."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(f"{_AUTOCD_MARK}\n[ -d /old ] && cd /old\n")
    bashrc.chmod(0o644)
    src = tmp_path / "autocd.py"
    src.write_text(_extract_autocd_rewriter())
    proc = subprocess.run(
        [sys.executable, str(src), str(bashrc)],
        capture_output=True, text=True,
        env={**os.environ,
             "GENESIS_AUTOCD_MARK": _AUTOCD_MARK,
             "GENESIS_AUTOCD_LINE": _shipped_autocd_line("/new/repo")},
    )
    assert proc.returncode == 0, proc.stderr
    assert bashrc.stat().st_mode & 0o777 == 0o644, "the file mode was changed"


def test_autocd_handles_an_indented_hook(tmp_path):
    """The marker is compared stripped, so an indented pair IS recognised —
    the hook-shape test must strip too, or it is rejected forever."""
    proc, out = _run_autocd(
        tmp_path,
        f"  {_AUTOCD_MARK}\n  [ -d /old ] && cd /old\n",
        "/new/repo",
    )
    assert proc.returncode == 0, proc.stderr
    assert "  [ -d /new/repo ] && cd /new/repo" in out, (
        f"indentation was not preserved:\n{out}"
    )
