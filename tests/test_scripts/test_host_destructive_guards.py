"""Host destructive-path guards (deploy-audit H4/H5/H7/I2/I3/H8) plus the
P7 robustness/abort guards (H1/H2/H3/H6/G2/I4/I5).

These fixes harden host-side operations in scripts/host-setup.sh,
scripts/install_guardian.sh, and scripts/guardian-gateway.sh that can destroy
data, strip a working config, abort the whole script under `set -euo pipefail`,
or claim success that didn't happen:

* **H1** IP-geolocation TZ pipeline aborted host-setup on a curl failure / grep
  no-match (pipefail) before its own fallback could run — now `|| _detected_tz=""`.
* **H2** the managed-bridge detect (`incus … | grep ",YES," | …`) aborted on a
  no-match host (grep→1 under pipefail) before the `[ -n ]` skip — now `|| true`.
* **H3** the OOM `sudo sysctl --system` aborted on failure and lied about success —
  now an `if … then/else` that neither aborts nor over-claims.
* **H6** the "IOPS limits applied" echo was unconditional after `|| true` device
  commands — now gated on the idempotent IOPS sets (NOT the override, which errors
  benignly on a re-run).
* **G2** the guardian `update` verb's `OLD=$(git rev-parse …)` had no fallback —
  now `|| echo "unknown"` matching its siblings.
* **I4** udev rules were reloaded but never `trigger`ed, so the BFQ scheduler rule
  never reached existing block devices — now `udevadm trigger --subsystem-match=block`
  at BOTH sites (install_guardian.sh + guardian-gateway.sh's update mirror).
* **I5** the venv gate tested `-d "$VENV_DIR"` so a partial/broken venv was skipped —
  now `-x "$VENV_DIR/bin/python"`.

The original destructive-path guards:

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
GUARDIAN_GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"


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


# ── P7: robustness / abort / honesty guards (H1/H2/H3/H6/G2/I4/I5) ────


def test_p7_host_setup_runs_under_pipefail():
    """The abort-guards (H1/H2/H3) only matter under pipefail — lock that the
    script still declares it, so a future `set -e`→`set +o pipefail` change can't
    silently make these guards dead weight."""
    assert "set -euo pipefail" in HOST_SETUP.read_text()


def test_h1_timezone_pipeline_has_fallback():
    code = _code(HOST_SETUP)
    # curl-fail / grep-no-match must fall through to the timedatectl fallback,
    # not abort at the assignment under pipefail.
    assert '|| _detected_tz=""' in code
    # The fallback block that the guard protects must still be reachable.
    assert 'if [ -z "$_detected_tz" ]; then' in code


def test_h2_bridge_detect_no_match_is_survivable():
    code = _code(HOST_SETUP)
    # BOTH detect sites (NAT + firewall) guard the grep-no-match abort …
    assert code.count("cut -d, -f1 || true)") == 2
    # … and the unguarded form is gone entirely.
    assert "cut -d, -f1)" not in code
    # The "no managed bridge" skip path is now reachable.
    assert "No managed Incus bridge found" in HOST_SETUP.read_text()


def test_h3_oom_sysctl_neither_aborts_nor_lies():
    code = _code(HOST_SETUP)
    assert "if sudo sysctl --system > /dev/null 2>&1; then" in code
    # The bare, abort-prone form is gone.
    assert "\n    sudo sysctl --system > /dev/null 2>&1\n" not in "\n" + code + "\n"
    assert "OOM sysctl apply failed" in HOST_SETUP.read_text()  # honest failure branch


def test_h6_iops_message_gated_on_idempotent_sets():
    code = _code(HOST_SETUP)
    # The success echo is now conditional on the two idempotent IOPS sets …
    assert "_io_limits_ok=true" in code
    assert code.count("_io_limits_ok=false") == 2  # one per limits.read / limits.write set
    assert 'if [ "$_io_limits_ok" = true ]; then' in code
    # … NOT on the override, which errors benignly on a re-run (must stay `|| true`).
    assert (
        'incus config device override "$CONTAINER_NAME" root size="$DISK" 2>/dev/null || true'
        in code
    )
    assert "IOPS limits not applied" in HOST_SETUP.read_text()  # honest else branch


def test_g2_guardian_update_rev_parse_has_fallback():
    code = _code(GUARDIAN_GATEWAY)
    assert 'OLD=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")' in code
    # The unguarded form is gone (would abort a corrupt-repo update under set -e).
    assert "OLD=$(git rev-parse --short HEAD 2>/dev/null)" not in code


def test_i4_udev_reload_is_followed_by_trigger():
    """reload alone refreshes the rules DB but never re-applies to existing
    block devices; the scheduler rule needs a `trigger`. Scoped to `block` so a
    live host's other subsystems aren't re-triggered. Both sites must have it."""
    trigger = "sudo udevadm trigger --subsystem-match=block 2>/dev/null || true"
    assert trigger in _code(INSTALL_GUARDIAN)
    assert trigger in _code(GUARDIAN_GATEWAY)
    # A reload with NO following trigger is the bug — assert the trigger count
    # matches the reload count at each site.
    for path in (INSTALL_GUARDIAN, GUARDIAN_GATEWAY):
        c = _code(path)
        assert c.count("udevadm control --reload-rules") == c.count(
            "udevadm trigger --subsystem-match=block"
        )


def test_i5_venv_gate_checks_interpreter_not_dir():
    code = _code(INSTALL_GUARDIAN)
    assert 'if [ ! -x "$VENV_DIR/bin/python" ]; then' in code
    # The dir-existence gate (which skipped recreating a broken venv) is gone.
    assert 'if [ ! -d "$VENV_DIR" ]; then' not in code
    # --clear so a dangling bin/python symlink is rebuilt, not skipped by venv.
    assert '-m venv --clear "$VENV_DIR"' in code
