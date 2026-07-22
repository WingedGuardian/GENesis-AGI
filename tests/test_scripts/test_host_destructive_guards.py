"""Host destructive-path guards (deploy-audit H4/H5/H7/I2/I3/H8).

These fixes harden host-side operations in scripts/host-setup.sh and
scripts/install_guardian.sh that can destroy data or strip a working config:

* **H4** a "damaged" container was force-DELETED (unrecoverable — DB, memory,
  transcripts gone), and a single transient `incus exec` failure could
  misclassify a healthy container as damaged. Now: probe-retry (3 sweeps) +
  RENAME the container aside instead of deleting it.
* **H7** a split-disk resize bind-mounted a fresh disk OVER a populated
  /home/ubuntu, shadowing the whole install. Now: only bind when the home is
  empty; a populated home is left alone with guidance.
* **H5** the guardian-state reset used $HOME (→ /root under sudo, missing the
  operator's real state), and a root-run mkdir left ~/.claude root-owned. Now:
  the operator's home + chown the dir.
* **I2** under `set -e`, a failed `MOUNT_ERR=$(incus … add …)` aborted the
  installer, making the graceful fallback dead code. Now: `if MOUNT_ERR=$(…)`.
* **I3** guardian.yaml (operator-editable) was regenerated every run, clobbering
  edits. Now: generated only when absent.
* **H8** the group-activation re-exec flattened arg quoting via `$*`. Now:
  printf %q per arg.

These are host operations (incus/sudo) that cannot be safely exercised in CI, so
these are extraction assertions on the shipped script text — the regression lock
for guards whose failure mode is destructive.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"
INSTALL_GUARDIAN = REPO_ROOT / "scripts" / "install_guardian.sh"


def _code(path: Path) -> str:
    """Script text minus comment-only lines (so assertions match real code)."""
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


# ── H4: container retire (rename-aside, not force-delete) + probe retry ──


def test_h4_retire_renames_not_force_deletes():
    code = _code(HOST_SETUP)
    # The destructive force-delete of the LIVE container is gone everywhere —
    # replaced by a rename-aside helper (its message may still mention
    # `incus delete <renamed-copy>` as a later reclaim hint, which is safe).
    assert 'incus delete "$CONTAINER_NAME" --force' not in code
    assert code.count("_retire_container_aside") >= 4  # 1 def + 3 call sites
    assert 'incus rename "$CONTAINER_NAME"' in code
    # On a rename FAILURE the helper force-stopped the container, so it must
    # restart it — never leave Genesis down with no auto-restart (architect fix).
    assert "restarting it in place" in code and "incus start" in code


def test_h4_health_probe_retries():
    """A transient exec failure must not misclassify a healthy container: the
    probes run in a retry loop."""
    code = _code(HOST_SETUP)
    assert "for _hp_attempt in 1 2 3" in code
    assert "test -x /home/ubuntu/genesis/.venv/bin/python" in code  # still probes the venv


# ── H7: bind-mount only over an empty home ──────────────────────────


def test_h7_bind_guarded_on_install_marker():
    code = _code(HOST_SETUP)
    add_region = code.split("_home_bind_src", 1)[1]
    # Discriminate on the actual INSTALL MARKER (the genesis repo), NOT on the
    # home being empty — a fresh home has /etc/skel dotfiles, so an emptiness
    # test would false-refuse a legitimate fresh split-disk bind (architect fix).
    assert "test -e /home/ubuntu/genesis" in add_region
    assert "_home_entries" not in code  # the fragile empty-count discriminator is gone
    assert "homedisk disk source" in add_region  # still binds when no install is present


# ── H5: operator home for guardian-state + chown ~/.claude ──────────


def test_h5_guardian_state_uses_operator_home():
    code = _code(HOST_SETUP)
    # The stale-state reset resolves the operator's home, not $HOME (/root under sudo).
    assert '_gs_home="$(eval echo "~${SUDO_USER:-$(whoami)}")"' in code
    assert '_guardian_state="$_gs_home/.local/state/genesis-guardian/state.json"' in code


def test_h5_claude_dir_chowned_to_operator():
    code = _code(HOST_SETUP)
    seg = code.split('mkdir -p "$_host_home/.claude"', 1)[1].split("if [ ! -f", 1)[0]
    assert 'chown "$_host_user:" "$_host_home/.claude"' in seg


# ── I2: set -e-safe mount add + retrofit-removal warning ────────────


def test_i2_mount_add_is_set_e_safe():
    code = _code(INSTALL_GUARDIAN)
    # The old `MOUNT_ERR=$(…); if [ $? -eq 0 ]` (which set -e aborts on failure)
    # is replaced by the `if MOUNT_ERR=$(…); then` form.
    assert "if MOUNT_ERR=$(incus config device add" in code
    assert "if [ $? -eq 0 ]" not in code
    assert "_had_shared_mount" in code  # warns when a retrofit removed the old mount


# ── I3: guardian.yaml preserved on re-run ───────────────────────────


def test_i3_guardian_yaml_not_clobbered():
    code = _code(INSTALL_GUARDIAN)
    assert 'if [ -f "$INSTALL_DIR/config/guardian.yaml" ]' in code
    assert "preserved" in INSTALL_GUARDIAN.read_text()
    # The heredoc generate is now in the else branch (only when absent).
    idx_guard = code.index('if [ -f "$INSTALL_DIR/config/guardian.yaml" ]')
    idx_gen = code.index('cat > "$INSTALL_DIR/config/guardian.yaml"')
    assert idx_guard < idx_gen


# ── H8: re-exec preserves arg quoting ───────────────────────────────


def test_h8_reexec_quotes_args():
    code = _code(HOST_SETUP)
    assert 'for _a in "$@"; do _ORIG_ARGS+="$(printf \'%q \' "$_a")"; done' in code
    assert '_ORIG_ARGS="$*"' not in code  # the flattening form is gone
