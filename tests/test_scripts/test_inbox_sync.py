"""Tests for scripts/inbox_sync.sh — vault↔local response-file sync.

Focus: the cleanup step (step 2) must be FAIL-CLOSED on the vault listing.
Regression under test (2026-07-29 incident): when `rclone lsf` failed during a
Dropbox API outage, `REMOTE_GENESIS` silently became empty and the cleanup loop
deleted every local `*.genesis.md` response file (150 in one cycle) as
"deleted from vault" — a false positive that reset the response counter and
caused silent overwrites of already-delivered files in the user's vault.

Also locked here:
- on a failed listing the step-3 push is age-gated so an old local copy of a
  vault-side deletion cannot be re-uploaded (resurrected) before the next
  healthy cycle can clean it;
- filenames are handled as data (leading dash, spaces, ``&``, apostrophes,
  glob characters — the live vault's actual shapes), never as options;
- listing stderr stays out of the membership data.

Also locked here (#2804): a response is cleaned only if the vault was SEEN to
hold it at the end of an earlier healthy cycle (the `.genesis-seen-in-vault`
record, scoped to one remote), so a response whose upload never landed, or
that was written while the job was not running, is kept and pushed instead of
being deleted as a "user deletion"; cleanup works at any depth; a cleaned
response goes to the Genesis trash, and one the trash refuses is held (kept,
not pushed, still recorded); the push never overwrites a vault file.

Runs the REAL script with a fake `rclone` on PATH (behavior driven by env).
With ``RCLONE_VAULT_DIR`` set the fake is backed by a real directory: ``copy``
writes the pushed responses into it (honouring ``--filter-from`` and
``--ignore-existing``) and ``lsf`` lists it, so a post-push listing sees
exactly what the push delivered. The trash is the real ``genesis.trash``,
run by this interpreter against a temp ``GENESIS_HOME``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_SCRIPT = Path(
    os.environ.get(
        "INBOX_SYNC_SCRIPT_UNDER_TEST",
        Path(__file__).resolve().parents[2] / "scripts" / "inbox_sync.sh",
    )
)
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

_FAKE_RCLONE = r"""#!/bin/bash
# Fake rclone for inbox_sync.sh tests. Dispatch on subcommand.
echo "rclone $*" >> "$RCLONE_CALL_LOG"
case "$1" in
  lsf)
    n=$(( $(cat "$RCLONE_LSF_COUNT" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$RCLONE_LSF_COUNT"
    if [ "${RCLONE_LSF_RC:-0}" -ne 0 ] || [ "${RCLONE_LSF_FAIL_ON:-0}" = "$n" ]; then
      echo "simulated outage: dial tcp: lookup api.dropboxapi.com: i/o timeout" >&2
      exit "${RCLONE_LSF_RC:-1}"
    fi
    # Simulate the user deleting a vault file right before listing call N.
    if [ -n "${RCLONE_DELETE_BEFORE_LSF:-}" ] && [ "${RCLONE_DELETE_ON:-0}" = "$n" ]; then
      rm -f -- "$RCLONE_VAULT_DIR/$RCLONE_DELETE_BEFORE_LSF"
    fi
    if [ -n "${RCLONE_VAULT_DIR:-}" ]; then
      depth="-maxdepth 1"
      for a in "$@"; do [ "$a" = "-R" ] && depth=""; done
      (cd "$RCLONE_VAULT_DIR" && find . $depth -type f -name '*.genesis.md' | sed 's|^\./||' | sort)
    elif [ -n "${RCLONE_LSF_OUTPUT_FILE:-}" ] && [ -f "$RCLONE_LSF_OUTPUT_FILE" ]; then
      cat "$RCLONE_LSF_OUTPUT_FILE"
    fi
    ;;
  copy)
    [ "${RCLONE_COPY_RC:-0}" -ne 0 ] && exit "$RCLONE_COPY_RC"
    if [ -n "${RCLONE_VAULT_DIR:-}" ]; then
      src="$2"; filter=""; ignore_existing=""
      prev=""
      for a in "$@"; do
        [ "$prev" = "--filter-from" ] && filter="$a"
        [ "$a" = "--ignore-existing" ] && ignore_existing=1
        prev="$a"
      done
      excluded=""
      if [ -n "$filter" ]; then
        # "- /escaped/path" lines -> literal relative paths
        excluded=$(sed -n 's|^- /||p' "$filter" | sed 's/\\\(.\)/\1/g')
      fi
      # Real rclone copy walks dot directories too (an unanchored
      # "+ *.genesis.md" matches at any depth), so the fake does as well.
      (cd "$src" && find . -type f -name '*.genesis.md' | sed 's|^\./||') |
      while IFS= read -r rel; do
        if [ -n "$excluded" ] && printf '%s\n' "$excluded" | grep -qxF -- "$rel"; then continue; fi
        if [ -n "$ignore_existing" ] && [ -e "$RCLONE_VAULT_DIR/$rel" ]; then continue; fi
        mkdir -p "$RCLONE_VAULT_DIR/$(dirname "$rel")"
        cp "$src/$rel" "$RCLONE_VAULT_DIR/$rel"
      done
    fi
    exit 0
    ;;
  sync)
    exit 0
    ;;
esac
exit 0
"""

_REFUSING_PYTHON = """#!/bin/bash
echo "refused: not on the trash's volume" >&2
exit 1
"""


def _setup(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    """Create shim bin, local inbox dir, log path; return (env, inbox, log, call_log)."""
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    rclone = shim_bin / "rclone"
    rclone.write_text(_FAKE_RCLONE)
    rclone.chmod(0o755)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    log = tmp_path / "inbox_sync.log"
    call_log = tmp_path / "rclone_calls.log"
    call_log.write_text("")
    lsf_out = tmp_path / "lsf_output.txt"
    lsf_out.write_text("")
    home = tmp_path / "genesis_home"
    home.mkdir()

    env = {
        "PATH": f"{shim_bin}:{_BASE_PATH}",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
        "GENESIS_PYTHON": sys.executable,
        # The trash the script calls is THIS tree's genesis.trash, importable
        # whatever the interpreter's own install points at.
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "GENESIS_INBOX_PATH": str(inbox),
        "GENESIS_INBOX_SYNC_LOG": str(log),
        "RCLONE_CALL_LOG": str(call_log),
        "RCLONE_LSF_OUTPUT_FILE": str(lsf_out),
        "RCLONE_LSF_COUNT": str(tmp_path / "lsf_count"),
    }
    return env, inbox, log, call_log


_REMOTE = "dropbox:Apps/remotely-save/1/Genesis"


def _record_path(inbox: Path) -> Path:
    return inbox / ".genesis-seen-in-vault"


def _set_record(inbox: Path, names: list[str], *, remote: str = _REMOTE) -> Path:
    """Write the vault record as an earlier healthy cycle would have left it."""
    rec = _record_path(inbox)
    rec.write_text(f"# remote: {remote}\n" + "".join(f"{n}\n" for n in names))
    return rec


def _record_names(inbox: Path) -> list[str]:
    lines = _record_path(inbox).read_text().splitlines()
    assert lines and lines[0] == f"# remote: {_REMOTE}", lines[:1]
    return lines[1:]


def _use_vault_dir(env: dict, tmp_path: Path) -> Path:
    """Back the fake rclone with a real directory standing in for the vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    env["RCLONE_VAULT_DIR"] = str(vault)
    return vault


def _old_response_file(inbox: Path, name: str) -> Path:
    """Create a response file older than the 10-minute grace period."""
    f = inbox / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("response body\n")
    old = time.time() - 20 * 60
    os.utime(f, (old, old))
    return f


def _vault_file(vault: Path, name: str, body: str = "vault copy\n") -> Path:
    f = vault / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return f


def _set_listing(env: dict, names: list[str]) -> None:
    Path(env["RCLONE_LSF_OUTPUT_FILE"]).write_text(
        "".join(f"{n}\n" for n in names),
    )


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _copy_calls(call_log: Path) -> list[str]:
    return [c for c in call_log.read_text().splitlines() if c.startswith("rclone copy")]


def _trash_entries(env: dict) -> list[Path]:
    trash = Path(env["GENESIS_HOME"]) / "trash"
    return sorted(trash.iterdir()) if trash.is_dir() else []


# --- fail-closed listing (2026-07-29 incident) ---------------------------------


def test_lsf_failure_skips_cleanup_entirely(tmp_path):
    """REPRO of the 2026-07-29 wipe: listing fails → NOTHING may be deleted.

    Also asserts the SKIP line carries the diagnostics (rc + rclone stderr)."""
    env, inbox, log, _ = _setup(tmp_path)
    kept = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in (1, 2, 3)]
    _set_record(inbox, [f.name for f in kept])
    env["RCLONE_LSF_RC"] = "7"  # Dropbox API outage: lsf fails

    r = _run(env)

    assert r.returncode == 0, r.stderr
    for f in kept:
        assert f.exists(), f"{f.name} was wiped on a failed listing (fail-open bug)"
    body = log.read_text()
    assert "Cleaned up (deleted from vault)" not in body
    assert "SKIP cleanup" in body, "expected a loud skip marker in the sync log"
    assert "rc=7" in body, "SKIP line must carry the listing exit code"
    assert "simulated outage" in body, "SKIP line must carry rclone's stderr"


def test_failed_listing_leaves_record_untouched(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    rec = _set_record(inbox, ["Genesis-1.genesis.md"])
    before = rec.read_text()
    env["RCLONE_LSF_RC"] = "1"

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert rec.read_text() == before, "a failed listing must not rewrite the record"


def test_failed_listing_push_is_age_gated(tmp_path):
    """On a failed-listing cycle the push must be gated to fresh files
    (--max-age) — otherwise an old local copy of a file the user just deleted
    on the vault is silently re-uploaded (resurrection), because cleanup is
    skipped but the push still runs."""
    env, inbox, _, call_log = _setup(tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    env["RCLONE_LSF_RC"] = "1"

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = _copy_calls(call_log)
    assert calls, "push step did not run after a failed listing"
    assert "--max-age" in calls[-1], (
        "failed-listing push not age-gated — old files can resurrect deletions"
    )


def test_healthy_cycle_push_not_age_gated(tmp_path):
    """A healthy cycle must push ALL responses (no age gate)."""
    env, inbox, _, call_log = _setup(tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    _set_listing(env, ["Genesis-1.genesis.md"])

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = _copy_calls(call_log)
    assert calls and "--max-age" not in calls[-1], (
        "healthy-cycle push must NOT be age-gated (all responses ship)"
    )


# --- the record: only a response the vault was seen to hold is a deletion ------


def test_never_listed_response_survives_and_is_pushed(tmp_path):
    """The 2026-10-28 outage: responses written while the sync was not running
    were never in the vault. They must be kept and pushed, not reaped."""
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _vault_file(vault, "Older-1.genesis.md")
    _old_response_file(inbox, "Older-1.genesis.md")
    gap = _old_response_file(inbox, "Written-during-outage-1.genesis.md")
    _set_record(inbox, ["Older-1.genesis.md"])  # record from before the outage

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert gap.exists(), "an unsynced response was deleted as a user deletion"
    assert (vault / gap.name).exists(), "the unsynced response was not pushed"
    assert "kept 1 never-listed" in log.read_text()


def test_recorded_response_deleted_in_vault_goes_to_trash(tmp_path):
    """Intent lock, with the filename shapes a real vault produces
    (spaces / & / apostrophe): a response the record names and the healthy
    listing lacks was deleted by the user → moved to the Genesis trash."""
    env, inbox, log, _ = _setup(tmp_path)
    gone = _old_response_file(inbox, "My todos & musings-1.genesis.md")
    stays = _old_response_file(inbox, "Someone's Notes-2.genesis.md")
    _set_record(inbox, [gone.name, stays.name])
    _set_listing(env, [stays.name])  # user deleted the first in Obsidian

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert not gone.exists(), f"genuine user deletion no longer honored; log:\n{log.read_text()}"
    assert stays.exists()
    assert "Cleaned up (deleted from vault): My todos & musings-1.genesis.md" in log.read_text()
    entries = _trash_entries(env)
    assert len(entries) == 1, entries
    assert (entries[0] / "item").read_text() == "response body\n", "trashed copy is not the file"


def test_trash_refusal_holds_the_response(tmp_path):
    """The trash refuses → the response is HELD: kept, excluded from this
    cycle's push (no resurrection), and kept in the record (retried)."""
    env, inbox, log, call_log = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    refusing = tmp_path / "bin" / "refusing-python"
    refusing.write_text(_REFUSING_PYTHON)
    refusing.chmod(0o755)
    env["GENESIS_PYTHON"] = str(refusing)
    held = _old_response_file(inbox, "Held [draft]-1.genesis.md")
    _set_record(inbox, [held.name])  # vault no longer has it: a user deletion

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert held.exists(), "a response the trash refused was deleted anyway"
    assert not (vault / held.name).exists(), "a held response was re-uploaded (resurrection)"
    body = log.read_text()
    assert "HELD (trash refused" in body and "not on the trash's volume" in body
    assert held.name in _record_names(inbox), "held response dropped from the record"


def test_first_run_seeds_record_and_cleans_nothing(tmp_path):
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _vault_file(vault, "In-vault-1.genesis.md")
    local_only = _old_response_file(inbox, "Local-only-1.genesis.md")

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert local_only.exists()
    assert "no usable vault record yet" in log.read_text()
    assert sorted(_record_names(inbox)) == ["In-vault-1.genesis.md", "Local-only-1.genesis.md"]


def test_record_for_another_remote_is_not_used(tmp_path):
    """Changing the remote must never read as 'every response was deleted'."""
    env, inbox, log, _ = _setup(tmp_path)
    f = _old_response_file(inbox, "Genesis-1.genesis.md")
    _set_record(inbox, [f.name], remote="otherdrive:Somewhere")
    _set_listing(env, [])

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert f.exists(), "a record written for another remote drove a deletion"
    assert "no usable vault record yet" in log.read_text()


def test_symlinked_record_is_not_used_or_followed(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    f = _old_response_file(inbox, "Genesis-1.genesis.md")
    target = tmp_path / "elsewhere.txt"
    target.write_text(f"# remote: {_REMOTE}\n{f.name}\n")
    _record_path(inbox).symlink_to(target)
    _set_listing(env, [])

    r = _run(env)

    assert r.returncode == 0, r.stderr
    assert f.exists(), "a symlinked record drove a deletion"
    assert target.read_text() == f"# remote: {_REMOTE}\n{f.name}\n", "symlink target was written"
    assert not _record_path(inbox).is_symlink()


def test_record_path_that_is_a_directory_is_reported_and_cleans_nothing(tmp_path):
    """A directory at the record path (e.g. mirrored from a vault folder of that
    name) must never be read as a record, and must be reported every cycle."""
    env, inbox, log, _ = _setup(tmp_path)
    f = _old_response_file(inbox, "Genesis-1.genesis.md")
    (_record_path(inbox)).mkdir()
    _set_listing(env, [])

    assert _run(env).returncode == 0
    assert f.exists()
    assert "WARNING: state path is not a regular file" in log.read_text()


def test_mirror_excludes_state_named_directories(tmp_path):
    env, inbox, _, call_log = _setup(tmp_path)
    _set_listing(env, [])
    assert _run(env).returncode == 0
    sync_calls = [c for c in call_log.read_text().splitlines() if c.startswith("rclone sync")]
    assert sync_calls and "--exclude .genesis-*/**" in sync_calls[0], sync_calls


def test_record_comes_from_the_post_push_listing(tmp_path):
    """A response pushed this cycle is recorded as seen, so deleting it in the
    vault before the next cycle is a deletion, not an upload to retry."""
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    fresh = _old_response_file(inbox, "Fresh-1.genesis.md")
    _set_record(inbox, [])

    assert _run(env).returncode == 0
    assert (vault / fresh.name).exists()
    assert fresh.name in _record_names(inbox), "a pushed response is not recorded as seen"

    (vault / fresh.name).unlink()  # user deletes it right away
    assert _run(env).returncode == 0
    assert not fresh.exists(), "a deletion right after the push was not honoured"
    assert not (vault / fresh.name).exists(), "the deleted response was resurrected"


def test_deletion_between_push_and_record_is_still_honoured(tmp_path):
    """A recorded response deleted after the step-2 listing (so after it was
    seen) must stay recorded, so its deletion is honoured next cycle rather
    than read as an upload to retry. The fake deletes the vault copy right
    before the step-4 listing (listing call 2), so only the step-2 listing
    carries it."""
    env, inbox, _, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _vault_file(vault, "Seen-1.genesis.md")
    local = _old_response_file(inbox, "Seen-1.genesis.md")
    _set_record(inbox, [])
    env["RCLONE_DELETE_BEFORE_LSF"] = "Seen-1.genesis.md"
    env["RCLONE_DELETE_ON"] = "2"

    assert _run(env).returncode == 0
    assert local.name in _record_names(inbox), (
        "a response seen in step 2 was dropped from the record"
    )

    del env["RCLONE_DELETE_BEFORE_LSF"]
    Path(env["RCLONE_LSF_COUNT"]).unlink()
    assert _run(env).returncode == 0
    assert not local.exists(), "the deletion was not honoured on the next cycle"
    assert not (vault / local.name).exists(), "the deleted response was resurrected"


def test_failed_push_keeps_the_response_unseen(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    _use_vault_dir(env, tmp_path)
    f = _old_response_file(inbox, "Unlanded-1.genesis.md")
    _set_record(inbox, [])
    env["RCLONE_COPY_RC"] = "5"

    assert _run(env).returncode == 0
    assert f.name not in _record_names(inbox), "an unlanded upload was recorded as seen"

    assert _run(env).returncode == 0  # still failing: must still be kept
    assert f.exists()


def test_failed_post_push_listing_records_the_pre_push_listing(tmp_path):
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _vault_file(vault, "Listed-1.genesis.md")
    _old_response_file(inbox, "Listed-1.genesis.md")
    new = _old_response_file(inbox, "New-1.genesis.md")
    _set_record(inbox, ["Listed-1.genesis.md"])
    env["RCLONE_LSF_FAIL_ON"] = "2"  # the step-4 listing fails

    assert _run(env).returncode == 0
    assert "post-push listing failed" in log.read_text()
    assert _record_names(inbox) == ["Listed-1.genesis.md"]
    assert new.exists()


def test_push_never_overwrites_a_vault_file(tmp_path):
    env, inbox, _, call_log = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _vault_file(vault, "Kept-1.genesis.md", body="user edited\n")
    _old_response_file(inbox, "Kept-1.genesis.md")
    _set_record(inbox, ["Kept-1.genesis.md"])

    assert _run(env).returncode == 0
    assert (vault / "Kept-1.genesis.md").read_text() == "user edited\n"
    assert all("--ignore-existing" in c for c in _copy_calls(call_log))


# --- any depth, dot directories, moves, names ----------------------------------


def test_nested_response_cleaned_and_kept_like_top_level(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    gone = _old_response_file(inbox, "Projects/Ideas-1.genesis.md")
    stays = _old_response_file(inbox, "Projects/Ideas-2.genesis.md")
    _vault_file(vault, "Projects/Ideas-2.genesis.md")
    _set_record(inbox, ["Projects/Ideas-1.genesis.md", "Projects/Ideas-2.genesis.md"])

    assert _run(env).returncode == 0
    assert not gone.exists(), "a nested response deleted in the vault was not cleaned"
    assert stays.exists()


def test_dot_directories_are_never_walked(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    dotted = _old_response_file(inbox, ".trash/Old-1.genesis.md")
    _set_record(inbox, [".trash/Old-1.genesis.md"])
    _set_listing(env, [])

    assert _run(env).returncode == 0
    assert dotted.exists(), "a response under a dot directory was cleaned"


def test_move_in_vault_is_logged_as_a_move(tmp_path):
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    old = _old_response_file(inbox, "Notes-1.genesis.md")
    _vault_file(vault, "Archive/Notes-1.genesis.md")
    _set_record(inbox, ["Notes-1.genesis.md"])

    assert _run(env).returncode == 0
    assert not old.exists()
    assert "Moved in vault: Notes-1.genesis.md -> Archive/Notes-1.genesis.md" in log.read_text()


def test_move_to_obsidian_trash_is_a_deletion_not_a_move(tmp_path):
    env, inbox, log, _ = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    old = _old_response_file(inbox, "Notes-1.genesis.md")
    _vault_file(vault, ".trash/Notes-1.genesis.md")
    _set_record(inbox, ["Notes-1.genesis.md"])

    assert _run(env).returncode == 0
    assert not old.exists()
    body = log.read_text()
    assert "Cleaned up (deleted from vault): Notes-1.genesis.md" in body
    assert "Moved in vault" not in body


def test_leading_dash_name_kept_when_listed(tmp_path):
    """A leading-dash filename is DATA, not grep options: when the healthy
    listing contains it, it must be kept (grep -qxF -- regression)."""
    env, inbox, _, _ = _setup(tmp_path)
    dashy = _old_response_file(inbox, "-x notes-1.genesis.md")
    _set_listing(env, [dashy.name])

    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert dashy.exists(), (
        "leading-dash filename deleted despite being in a healthy listing (grep read it as options)"
    )
    assert dashy.name in _record_names(inbox)

    _set_listing(env, [])  # now genuinely gone from the vault
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert not dashy.exists()


def test_newline_in_name_is_kept_and_never_compared(tmp_path):
    env, inbox, log, _ = _setup(tmp_path)
    weird = _old_response_file(inbox, "two\nlines-1.genesis.md")
    _set_record(inbox, ["two", "lines-1.genesis.md"])  # what grep -F would split it into
    _set_listing(env, [])

    assert _run(env).returncode == 0
    assert weird.exists()
    assert "name contains a newline" in log.read_text()


def test_trailing_slash_in_inbox_path_still_cleans(tmp_path):
    env, inbox, _, _ = _setup(tmp_path)
    env["GENESIS_INBOX_PATH"] = str(inbox) + "/"
    gone = _old_response_file(inbox, "Genesis-1.genesis.md")
    _set_record(inbox, [gone.name])
    _set_listing(env, [])

    assert _run(env).returncode == 0
    assert not gone.exists(), "a trailing slash on the inbox path disabled cleanup"


def test_bulk_deletion_on_healthy_listing_warns_but_proceeds(tmp_path):
    """A large legit cleanup still works (no resurrection loop) but logs loudly."""
    env, inbox, log, _ = _setup(tmp_path)
    files = [_old_response_file(inbox, f"Genesis-{n}.genesis.md") for n in range(1, 9)]
    _set_record(inbox, [f.name for f in files])
    _set_listing(env, [])  # user deleted all of them in Obsidian

    r = _run(env)

    assert r.returncode == 0, r.stderr
    for f in files:
        assert not f.exists(), "healthy-listing deletions must still be honored"
    body = log.read_text()
    assert "WARNING: bulk cleanup" in body, "expected loud bulk-deletion warning"


def test_state_files_excluded_from_mirror_and_not_pushed(tmp_path):
    """The step-1 sync must carry --exclude '.genesis-*' so the mirror can
    never delete the sync's own state; and no .genesis-* file is ever pushed."""
    env, inbox, _, call_log = _setup(tmp_path)
    vault = _use_vault_dir(env, tmp_path)
    _old_response_file(inbox, "Genesis-1.genesis.md")
    _set_record(inbox, [])

    r = _run(env)

    assert r.returncode == 0, r.stderr
    calls = call_log.read_text().splitlines()
    sync_calls = [c for c in calls if c.startswith("rclone sync")]
    assert sync_calls and "--exclude .genesis-*" in sync_calls[0], (
        "step-1 sync missing --exclude .genesis-* — the mirror will delete local state files"
    )
    # What the push may send is decided by the filter the script writes (the
    # fake copies only *.genesis.md, so asserting on the vault would prove
    # nothing about state files): it must end by excluding everything else.
    assert (vault / "Genesis-1.genesis.md").exists()
    filter_lines = (inbox / ".genesis-push-filter").read_text().splitlines()
    assert filter_lines[-2:] == ["+ *.genesis.md", "- **"], filter_lines


def test_symlinked_inbox_root_is_still_walked(tmp_path):
    """The inbox root may be a symlink to the real folder; cleanup must walk it
    (find does not follow a start-point symlink by default)."""
    env, inbox, _, _ = _setup(tmp_path)
    link = tmp_path / "inbox-link"
    link.symlink_to(inbox)
    env["GENESIS_INBOX_PATH"] = str(link)
    gone = _old_response_file(inbox, "Projects/Gone-1.genesis.md")
    _set_record(inbox, ["Projects/Gone-1.genesis.md"])
    _set_listing(env, [])

    assert _run(env).returncode == 0
    assert not gone.exists(), "a vault deletion was not applied through a symlinked inbox root"
