# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
#
# Single source of truth for the Claude Code version Genesis installs/pins,
# PLUS the shared `cc_ensure_local` aligner that keeps the LOCAL machine's CC at
# the pin.
#
# Sourced by scripts/install.sh (container), scripts/host-setup.sh (host VM),
# scripts/bootstrap.sh, and scripts/update.sh. update.sh ALSO dispatches this pin
# to the host VM via the guardian-gateway `update-cc` op. Bump CC_VERSION here in
# ONE place — the next install/bootstrap/update run aligns the local CC, so the
# container and host never drift.
#
# Governance model (see docs/reference/cc-compatibility.md): the npm pin below
# + the unified `update-cc` updater + DISABLE_UPDATES in ~/.claude/settings.json.
# Deliberately NO managed-settings `requiredMinimumVersion` floor — a hard floor
# removes the incident-recovery downgrade path and can brick CC.
#
# Honors an inherited CC_VERSION (e.g. `CC_VERSION=2.1.180 ./install.sh`).
CC_VERSION="${CC_VERSION:-2.1.246}"

# Node.js major that the pinned Claude Code requires — derived from the CC pin's
# engines.node (e.g. `@anthropic-ai/claude-code@2.1.201` declares node >=22).
# BUMP THIS IN LOCKSTEP whenever a CC pin raises the Node floor: a stale Node
# major is what left a host on Node 18, unable to run the pinned CC, with
# Guardian's `claude -p` recovery brain silently offline. Consumed by
# host-setup.sh (host Node install) and update.sh, which dispatches it to the
# host VM via the guardian-gateway `update-node` op — mirroring `update-cc`.
NODE_MAJOR="${NODE_MAJOR:-22}"

# Known CC install prefixes (bin dirs), probed when `command -v claude` fails —
# a PATH-blind install (user npm prefix whose PATH export only fires in
# interactive shells) must be treated as installed, not reinstalled forever.
# Colon-separated; overridable for tests and exotic layouts.
CC_PROBE_DIRS="${CC_PROBE_DIRS:-/usr/local/bin:/usr/bin:$HOME/.npm-global/bin}"


# cc_ensure_updater_suppressed — re-assert CC auto-updater suppression.
#
# Ensures BOTH `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1` in the USER-level
# ~/.claude/settings.json (arg 1 overrides the path, for the host leg).
#
# WHY BOTH, AND WHY USER-LEVEL: repo/project settings apply only when CC is
# launched from that directory, and the auto-updater runs in contexts where they
# do not — which is how a machine once self-bumped past the pin mid-session, and
# how a host VM ended up several minor versions ahead of the script pin with the
# Guardian recovery brain running an unvetted CC. The npm pin only decides what a
# DELIBERATE install writes; these two keys are what stop CC moving on its own.
#
# WHY IT LIVES ON THE ALIGN PATH: install.sh/host-setup.sh set these at SETUP
# time only. Nothing re-asserted them afterwards, so an install whose settings
# drifted (hand-edit, a tool rewriting the file, a partial restore) stayed
# silently unprotected forever — the pin could be violated with no signal. This
# runs from cc_ensure_local, i.e. on every install/bootstrap/update align, so the
# suppression is re-established as often as the pin itself.
#
# Idempotent and NON-FATAL. Deliberately QUIET when already correct (it runs on
# every align); LOUD only when it actually repairs drift, so a silent regression
# becomes visible in update/bootstrap output instead of rotting unnoticed.
# Never clobbers a settings file it cannot parse — a corrupt/foreign file is
# reported, not overwritten (destroying user settings is worse than the drift).
# Both suppressions below are cross-file blindness, not dead code: the linter only
# sees THIS file, where the call is arg-less and the state variable is never read.
# In reality host-setup.sh passes the host operator's settings path, and update.sh
# consumes CC_SUPPRESSION_STATE (folding it into HOST_CC_DEGRADED).
# shellcheck disable=SC2120,SC2034
# Durable breadcrumb for the suppression outcome, so it survives a SUBPROCESS
# boundary. During a real update, update.sh runs bootstrap.sh first; bootstrap
# calls cc_ensure_local, which can REPAIR the keys — and that shell state dies
# with the subprocess. update.sh's own later call then sees an already-correct
# file, reports `ok`, and the repair reaches neither update_history nor the
# visible deploy output. This file is that missing channel.
#
# `ok` is deliberately NOT written: absence means "nothing to report", so a
# stale breadcrumb can never manufacture a degradation. Readers compare the
# recorded epoch against a mark they took before the subprocess ran.
_CC_SUPP_OUTCOME_FILE="${HOME:-}/.genesis/cc_suppression_outcome"

_cc_supp_persist_outcome() {
    [ "${CC_SUPPRESSION_STATE:-unverified}" = "ok" ] && return 0
    mkdir -p "$(dirname "$_CC_SUPP_OUTCOME_FILE")" 2>/dev/null || return 0
    # EPOCHSECONDS first: it is a bash builtin, so the stamp does not depend on
    # `date` being on PATH. A zero stamp is not harmless — the reader compares
    # it against a watermark, so `0` makes a real repair invisible rather than
    # merely undated. `date` remains the fallback for bash < 5.0.
    printf '%s %s\n' "${CC_SUPPRESSION_STATE:-unverified}" \
        "${EPOCHSECONDS:-$(date -u +%s 2>/dev/null || echo 0)}" \
        > "$_CC_SUPP_OUTCOME_FILE" 2>/dev/null || true
    return 0
}

# Thin wrapper so the breadcrumb is written on EVERY exit path. The inner
# function has several early `return`s; recording at each of them would be a
# convention, and a convention is what the next `return` forgets.
# shellcheck disable=SC2120  # optional args by design (settings path + extra defaults)
cc_ensure_updater_suppressed() {
    local _rc=0
    # DID THIS RUN CREATE THE FILE, or merely modify one that was already there?
    #
    # `repaired` answers neither: it is set for ANY successful modification. A
    # caller that reads it as "we made this file" then acts on a file the
    # operator already owned — which is exactly how host-setup.sh came to chown a
    # pre-existing dotfiles-managed target it had promised not to touch.
    #
    # Computed HERE, once, rather than at each `repaired` assignment: there are
    # two of those and adding a third is a convention the next branch forgets.
    # `-e` follows a symlink deliberately — the question is whether the operator
    # already had a settings file, not whether a link existed.
    local _sf="${1:-$HOME/.claude/settings.json}"
    if [ -e "$_sf" ]; then CC_SUPPRESSION_CREATED=0; else CC_SUPPRESSION_CREATED=1; fi
    # "$@" MUST be forwarded: the inner function takes an optional settings
    # path ($1) and optional extra defaults ("$@"). Dropping it silently
    # discards both for any caller that uses them.
    # shellcheck disable=SC2120  # optional args by design; no current caller passes any
    _cc_ensure_updater_suppressed_inner "$@" || _rc=$?
    # A run that did not end in `repaired` wrote nothing, so it created nothing.
    # shellcheck disable=SC2034  # cross-file: host-setup.sh reads this, the linter
    # only sees this file (same blindness already noted for CC_SUPPRESSION_STATE)
    [ "${CC_SUPPRESSION_STATE:-}" = "repaired" ] || CC_SUPPRESSION_CREATED=0
    _cc_supp_persist_outcome
    return "$_rc"
}

# shellcheck disable=SC2120  # optional args by design (settings path + extra defaults)
_cc_ensure_updater_suppressed_inner() {
    local settings_file="${1:-$HOME/.claude/settings.json}"
    shift || true
    # Any remaining args are KEY=VALUE **set-if-absent** defaults applied in the
    # SAME atomic write (install.sh passes the subagent-nesting default). Two
    # policies, one read-modify-write: the suppression keys are ENFORCED to an
    # exact value, the defaults are only filled in when the key is missing, so a
    # deliberate operator override survives. They are merged here rather than
    # applied by a second block because a second full-file RMW on a
    # credential-bearing file doubles the lost-update window for no benefit and
    # duplicates the write contract (mode/xattr carry-over, CAS, fsync).
    local -a extra_defaults=("$@")
    # Outcome for callers that surface health (update.sh folds a non-ok value into
    # HOST_CC_DEGRADED -> update_history -> deploy health). `ok` | `repaired` |
    # `failed` | `contended` | `unverified`. A stderr line alone is not a signal —
    # it dies in a long deploy log.
    #
    # The entry value is PESSIMISTIC on purpose. This used to start at `ok`,
    # which made `ok` a default rather than a conclusion: any path that forgot
    # to set the state reported success, and an audit found nine such paths
    # across the chain. Now `ok`/`repaired` exist only where a branch EARNS
    # them by reading the file after the operation — a path added later that
    # sets nothing reports `unverified`, which every consumer treats as
    # not-ok. Fail closed by construction, not by review.
    CC_SUPPRESSION_STATE=unverified

    # Create the directory BEFORE the python3 gate: install.sh used to do this
    # unconditionally, and moving it behind the gate meant a python3-less box got
    # neither the directory nor the file (where the old bash heredoc had at least
    # produced a correct one). Report a real failure rather than swallowing it —
    # an unwritable $HOME surfaced later as "could not read/merge", which names
    # the wrong cause entirely.
    local settings_dir
    settings_dir="$(dirname "$settings_file")"
    if ! mkdir -p "$settings_dir" 2>/dev/null && [ ! -d "$settings_dir" ]; then
        CC_SUPPRESSION_STATE=failed
        echo "  WARNING: cc_ensure_updater_suppressed: cannot create $settings_dir" \
             "(is \$HOME writable?) — CC auto-updater suppression not verified" >&2
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        # python3 is NOT guaranteed on every caller's machine. install.sh is a
        # Python installer so the container always has it, and cc_settings_align.sh
        # gets it from the unit's PATH — but host-setup.sh installs python3 with
        # `incus exec <container>`, i.e. INSIDE the container. The HOST itself only
        # gets incus and nodejs, so a minimal host VM can reach here with no python3.
        #
        # The code this replaced wrote the file with a pure-bash heredoc, so such a
        # host still ended up correctly suppressed. Losing that would be a real
        # regression and a permanent one: per §Auto-Updater Suppression the HOST's
        # settings.json has no recurring self-heal, so the miss persists until
        # someone re-runs host-setup.sh by hand.
        #
        # So: CREATE is still possible without a JSON parser (we know the exact
        # contents). MERGING into an existing file is not — guessing at JSON with
        # shell would risk destroying operator settings, which is worse than the
        # drift. Create when absent; report and leave alone when present.
        if [ ! -e "$settings_file" ]; then
            local _umask_prev
            _umask_prev="$(umask)"
            umask 077                     # secrets-adjacent: never world-readable
            # Write-then-rename even here. This is the only path on this file
            # that does not go through the python reconciler's atomic swap, and a
            # plain truncating redirect can leave a zero-length settings.json if
            # the process dies mid-write — indistinguishable from success to
            # everything except the NEXT run, which then reports `failed`. The
            # exposure is narrow (file absent AND python3 missing) but the fix is
            # two lines of the same shell.
            # A DANGLING SYMLINK reaches this branch, and must not be replaced.
            #
            # MEASURED: `[ -e ]` FOLLOWS the link, so a settings.json symlinked
            # into a dotfiles checkout whose target does not exist yet tests as
            # ABSENT — correct, there is nothing there. But `mv -f` then renames
            # over the LINK ITSELF: the symlink disappears, the target is still
            # missing, and this function reports `repaired`. The operator's
            # dotfiles wiring is destroyed by a repair that claims success.
            #
            # So resolve the link and write to its TARGET. A plain redirect would
            # also write through the link, but it gives up the atomic swap this
            # branch deliberately has; renaming into the resolved path keeps both
            # the atomicity and the operator's symlink.
            local _write_to="$settings_file"
            if [ -L "$settings_file" ]; then
                local _link
                _link="$(readlink "$settings_file" 2>/dev/null || true)"
                if [ -z "$_link" ]; then
                    umask "$_umask_prev"
                    CC_SUPPRESSION_STATE=failed
                    echo "  WARNING: cc_ensure_updater_suppressed: $settings_file is a" \
                         "symlink whose target could not be read — leaving it alone" >&2
                    return 1
                fi
                case "$_link" in
                    /*) _write_to="$_link" ;;                             # absolute
                    *)  _write_to="$(dirname "$settings_file")/$_link" ;; # relative to the LINK
                esac
                if ! mkdir -p "$(dirname "$_write_to")" 2>/dev/null; then
                    umask "$_umask_prev"
                    CC_SUPPRESSION_STATE=failed
                    echo "  WARNING: cc_ensure_updater_suppressed: cannot create the directory" \
                         "for $settings_file's symlink target ($_write_to) —" \
                         "suppression NOT applied" >&2
                    return 1
                fi
            fi
            # EXCLUSIVE creation, never a PID-derived name. `$$` is
            # predictable, so on a shared host — and this branch runs under sudo
            # from host-setup.sh — another process able to write the operator's
            # .claude directory can pre-create that exact path as a SYMLINK. The
            # redirect below follows it and overwrites the victim as root,
            # before `mv` ever runs; the function then reports `repaired`.
            #
            # `mktemp` creates with O_EXCL and mode 0600, so a pre-existing path
            # makes it fail rather than be followed.
            #
            # FAIL CLOSED if mktemp is unavailable. This branch exists for
            # minimal hosts, so the tool cannot simply be assumed — but the
            # alternative is a guessable name, and declining to repair is far
            # better than repairing through someone else's symlink.
            local _tmp_settings=""
            if command -v mktemp >/dev/null 2>&1; then
                _tmp_settings="$(mktemp "${_write_to}.tmp.XXXXXX" 2>/dev/null)" || _tmp_settings=""
            fi
            if [ -z "$_tmp_settings" ]; then
                umask "$_umask_prev"
                CC_SUPPRESSION_STATE=failed
                echo "  WARNING: cc_ensure_updater_suppressed: cannot create a temporary" \
                     "file exclusively (mktemp unavailable or failed) — refusing to write" \
                     "through a predictable path; suppression NOT applied" >&2
                return 1
            fi
            if printf '%s\n' \
                '{' \
                '  "env": {' \
                '    "DISABLE_AUTOUPDATER": "1",' \
                '    "DISABLE_UPDATES": "1"' \
                '  }' \
                '}' > "$_tmp_settings" 2>/dev/null \
                && mv -f "$_tmp_settings" "$_write_to" 2>/dev/null \
                && grep -qF '"DISABLE_AUTOUPDATER": "1"' "$settings_file" 2>/dev/null \
                && grep -qF '"DISABLE_UPDATES": "1"' "$settings_file" 2>/dev/null; then
                # The greps are the post-write verification. This branch used to
                # set `repaired` on the strength of the mv alone — the ONE path
                # in the chain that wrote and never read back, reporting the
                # state that everywhere else means "verified correct after
                # writing". grep -F on the exact literals is faithful HERE
                # because this branch wrote those exact bytes itself; it is not
                # a general JSON check and must not be copied to paths that
                # merge foreign content.
                umask "$_umask_prev"
                CC_SUPPRESSION_STATE=repaired
                echo "  ! CC auto-updater suppression was MISSING in $settings_file —" \
                     "created it without python3 (both keys set, verified by re-read)" >&2
                # This branch writes ONLY the two suppression keys. Any
                # set-if-absent defaults the caller passed are silently absent,
                # and a caller that prints "suppression + <default> verified"
                # would be stating something untrue. Say so here.
                if [ "${#extra_defaults[@]}" -gt 0 ]; then
                    echo "    (no python3: the set-if-absent default(s)" \
                         "${extra_defaults[*]} were NOT applied — re-run once" \
                         "python3 is available)" >&2
                fi
                return 0
            fi
            rm -f "$_tmp_settings" 2>/dev/null || true   # create failed: no litter
            umask "$_umask_prev"
            # Own failure arm: falling through would print "already exists —
            # left untouched", which is false both when the create itself failed
            # (nothing exists) and when the mv landed but the verify greps did
            # not (something rewrote the file in the gap).
            CC_SUPPRESSION_STATE=failed
            echo "  WARNING: cc_ensure_updater_suppressed: could not create-and-verify" \
                 "$settings_file without python3 — CC auto-updater suppression NOT" \
                 "in effect; add DISABLE_AUTOUPDATER=1 and DISABLE_UPDATES=1 by hand" >&2
            return 1
        fi
        CC_SUPPRESSION_STATE=failed
        echo "  WARNING: cc_ensure_updater_suppressed: python3 not found and $settings_file" \
             "already exists — cannot merge safely, left untouched. Add" \
             "DISABLE_AUTOUPDATER=1 and DISABLE_UPDATES=1 by hand, or CC may" \
             "self-update past the pin" >&2
        return 1
    fi

    local out rc
    # TWO separate shell hazards here, both load-bearing:
    #  1. `local out` MUST stay on its own line — `local out="$(...)"` would make
    #     $? the exit status of `local`, not of the command substitution.
    #  2. the assignment must be `|| rc=$?`, NOT a bare statement: an assignment
    #     whose value is a command substitution inherits that substitution's exit
    #     status and TRIPS ERREXIT. install.sh/bootstrap.sh/host-setup.sh all run
    #     `set -euo pipefail`, so a bare call would abort the installer outright on
    #     an unparseable settings.json — the very case this function is documented
    #     to survive. Every present call site happens to neutralise errexit; this
    #     makes the function honest regardless of how the next one is written.
    rc=0
    out="$(python3 - "$settings_file" "${extra_defaults[@]}" <<'PYEOF'
import json, os, random, shutil, sys, tempfile, time

# Resolve symlinks FIRST: a dotfiles-managed settings.json is common, and
# write-by-rename onto the link would replace it with a regular file — forking
# the operator's dotfiles copy (which keeps the stale content) from the live one,
# permanently and silently. Follow the link and rewrite the real target.
path = os.path.realpath(sys.argv[1])
REQUIRED = {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
# argv[2:] are KEY=VALUE defaults applied ONLY when the key is absent, so a
# deliberate operator override is preserved. Malformed entries are ignored rather
# than failing the suppression this function primarily exists to guarantee.
DEFAULTS = {}
for _arg in sys.argv[2:]:
    if "=" in _arg:
        _k, _v = _arg.split("=", 1)
        if _k:
            DEFAULTS[_k] = _v
ATTEMPTS = 3


def fail(reason):
    # One clean line, never a traceback: the caller surfaces this verbatim, and
    # "could not read/merge" collapsed a full disk, a permission error and a
    # corrupt file into one indistinguishable message.
    print(reason, file=sys.stderr)
    sys.exit(2)


def _exhausted(reason):
    # Exit 4, not 2. Retry exhaustion IS contention — a competing writer, just
    # detected by running out of attempts rather than by losing one race, so it
    # reports the same STATE (`contended`) as exit 3; labelling it `failed`
    # would give one root cause two names and defeat the point of the state.
    #
    # But not the same CODE, because the two say opposite things about what is
    # on disk: exit 3 means a write landed and was then overwritten, exit 4
    # means nothing was written at all. A caller that collapses them has to
    # describe one of the two wrongly, which is exactly what the wrapper below
    # used to do ("was repaired but did NOT stick" for a run that repaired
    # nothing). Same state, distinct codes, honest message for each.
    print(reason, file=sys.stderr)
    sys.exit(4)


def identity():
    """The file identity we compare-and-swap against (None when absent)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        # EACCES on the parent dir, ELOOP on a symlink cycle — same family as the
        # write errors above; report cleanly rather than surfacing a traceback.
        fail("cannot stat settings.json (%s) — left untouched" % exc.strerror)
    return (st.st_ino, st.st_mtime_ns, st.st_size), st


for _attempt in range(ATTEMPTS):
    before, st_before = identity()
    if before is not None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except UnicodeDecodeError:
            fail("settings.json is not valid UTF-8 — left untouched")
        except ValueError:
            fail("settings.json is not valid JSON — left untouched")
        except OSError as exc:
            fail("settings.json unreadable (%s) — left untouched" % exc.strerror)
        if not isinstance(data, dict):
            fail("settings.json is not a JSON object — left untouched")
    else:
        data = {}

    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        fail('settings.json "env" is not an object — left untouched')

    repaired = [k for k, v in REQUIRED.items() if env.get(k) != v]
    missing_defaults = [k for k in DEFAULTS if k not in env]
    if not repaired and not missing_defaults:
        sys.exit(0)          # already correct — no write, nothing to report

    for k in repaired:
        env[k] = REQUIRED[k]
    for k in missing_defaults:
        env[k] = DEFAULTS[k]

    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".settings.", suffix=".tmp",
        )
    except OSError as exc:
        # ENOSPC / EROFS / EACCES land here. Without this the operator got a raw
        # Python traceback under a message that said "left untouched (reason
        # above)" — pointing at a stack trace instead of the real cause, which is
        # exactly the collapse this function's error handling exists to avoid.
        # EROFS is not hypothetical: the unit template documents a settings.json
        # symlinked outside %h under ProtectSystem=strict.
        fail("cannot create a temp file beside settings.json (%s) — left untouched"
             % exc.strerror)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())     # rename is atomic, but not durable on its own
        if before is not None:
            # copystat carries mode + times + xattrs; chown carries ownership when
            # permitted (the host leg runs under sudo, where it is). mkstemp already
            # created the temp 0600, so a failure here never WIDENS access.
            try:
                shutil.copystat(path, tmp)
            except OSError:
                pass
            # A rewrite must LOOK rewritten. copystat also carries the source's
            # TIMES, which would restore the pre-write mtime and hide the repair
            # from every mtime+size change detector (rsync's quick check, `find
            # -newer`, any mtime-keyed cache) — measured: a same-length value
            # correction left size AND mtime_ns byte-identical. That directly
            # undercuts this feature's own remediation advice, since "find the
            # writer that keeps rewriting this file" starts with timestamps.
            try:
                os.utime(tmp, None)
            except OSError:
                pass
            # POSIX ACLs live in system.posix_acl_access. copystat DOES carry it
            # for the file's owner — measured on ext4/CPython 3.12: os.setxattr
            # of that name succeeds unprivileged, and copystat reproduces it on
            # the replacement inode. An earlier revision REFUSED to write at all
            # when an ACL was present, on the assumption it could not be carried.
            # That was false, and the cost was severe: a default ACL on $HOME or
            # ~/.claude (setfacl -d, routine on NFS and corporate images) makes
            # every file created inside carry one, so the reconciler would never
            # write, suppression would never be established, and the timer would
            # fail daily forever with no self-heal — the exact state this exists
            # to prevent, made permanent. Verify instead of refusing.
            try:
                if ("system.posix_acl_access" in os.listxattr(path)
                        and "system.posix_acl_access" not in os.listxattr(tmp)):
                    os.unlink(tmp)
                    fail("settings.json carries a POSIX ACL this process could not "
                         "carry across — left untouched; set the two keys by hand")
            except OSError:
                pass         # filesystem without xattr support: nothing to preserve
            try:
                os.chown(tmp, st_before.st_uid, st_before.st_gid)
            except OSError:
                pass
        else:
            os.chmod(tmp, 0o600)     # brand-new file: secrets-adjacent, start private

        # COMPARE-AND-SWAP. os.replace is atomic for READERS but provides no CAS,
        # so a concurrent writer's change would be silently reverted by our stale
        # in-memory copy. CC itself rewrites this file (a /config change, a
        # permission grant), and this function now runs on every align AND on a
        # timer, so the window recurs. Re-check identity as late as possible and
        # retry the whole read-modify rather than clobber.
        # Residual: the few syscalls between this check and the rename are still
        # unguarded — irreducible without a lock the other writers do not take.
        after, _ = identity()
        if after != before:
            os.unlink(tmp)
            # Back off before re-reading. Three bare retries complete in
            # microseconds, so against a writer that takes milliseconds all
            # three land inside the same contended window and the bounded
            # retry is effectively a single attempt.
            time.sleep(0.01 + random.random() * 0.02)
            continue
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)   # never leave a stray temp holding the contents
        except OSError:
            pass
        fail("could not write settings.json (%s) — left untouched" % exc.strerror)
    except BaseException:
        try:
            os.unlink(tmp)   # e.g. KeyboardInterrupt / SIGTERM mid-write
        except OSError:
            pass
        raise

    # Verify the EFFECT, not just the action: re-read and confirm the keys are
    # actually present before claiming a repair.
    #
    # SCOPE, honestly: this catches only a clobber that lands between the rename
    # and this read. It cannot prove PERSISTENCE — MEASURED against a writer
    # rewriting this file every ~4ms, the repair landed, passed this check, and was
    # overwritten afterwards anyway. No single-shot check can do better, because a
    # competing writer takes no lock of ours. Persistence is only observable over
    # TIME: a REPEAT `repaired` on the next timer tick is the real "something on
    # this machine keeps rewriting settings.json" signal. This check exists so that
    # `repaired` means "verified correct after writing" rather than merely "wrote
    # bytes" — a narrower claim, but a true one.
    try:
        with open(path, encoding="utf-8") as f:
            final = json.load(f)
        final_env = final.get("env") or {}
        still = [k for k, v in REQUIRED.items() if final_env.get(k) != v]
        # Defaults this run WROTE are part of the write being verified — a
        # clobber that ate the default but spared the suppression keys would
        # otherwise pass, and the caller would print "applied" for a key that
        # is not there.
        still += [k for k in missing_defaults if k not in final_env]
    except (OSError, ValueError):
        still = list(REQUIRED)
    if still:
        print("repair did not stick — %s missing again immediately, so something on "
              "this machine is actively rewriting settings.json" % " ".join(sorted(still)),
              file=sys.stderr)
        sys.exit(3)

    # Report EVERY key this run wrote, defaults included. Printing only the
    # suppression repairs made a defaults-only write produce rc 0 with EMPTY
    # stdout — byte-identical to "file already correct, nothing written" — so
    # the caller reported `ok` (untouched) for a run that modified the file.
    print(" ".join(sorted(repaired + missing_defaults)))
    sys.exit(0)

_exhausted("settings.json kept changing under us (%d attempts) — left untouched rather "
     "than revert a concurrent writer" % ATTEMPTS)
PYEOF
    )" || rc=$?

    if [ "$rc" -eq 3 ] || [ "$rc" -eq 4 ]; then
        # Both are contention, and both leave the effective state unsuppressed, so
        # they share a STATE. They do not share a sentence: 3 means a write landed
        # and was overwritten before we could confirm it; 4 means the file kept
        # changing under us and we wrote NOTHING rather than revert a concurrent
        # writer. Telling an operator a repair "did not stick" when no repair was
        # attempted sends them hunting a pathological writer over a file that is
        # simply busy. Rare by construction either way, so seeing one at all is a
        # hint at an aggressive writer — but its ABSENCE proves nothing, and a
        # repeat `repaired` across ticks remains the durable signal.
        CC_SUPPRESSION_STATE=contended
        if [ "$rc" -eq 3 ]; then
            echo "  ! CC auto-updater suppression in $settings_file was repaired but did NOT" \
                 "stick (reason above) — CC may self-update past the pin; find the writer" >&2
        else
            echo "  ! CC auto-updater suppression in $settings_file was NOT applied — a" \
                 "concurrent writer kept winning (reason above); the next run should" \
                 "succeed, but a repeat means something keeps rewriting it" >&2
        fi
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        # The specific cause was already printed to stderr by the helper above
        # (unparseable / not UTF-8 / unreadable / ACL / kept-changing). Do not restate
        # it as one catch-all line — that is how a full disk read as corruption.
        CC_SUPPRESSION_STATE=failed
        echo "  WARNING: cc_ensure_updater_suppressed: $settings_file left untouched" \
             "(reason above) — verify DISABLE_AUTOUPDATER=1 and DISABLE_UPDATES=1 by hand," \
             "or CC may self-update past the pin" >&2
        return 1
    fi

    if [ -n "$out" ]; then
        # A write landed and was verified by the post-write re-read. `repaired`
        # for both shapes — the timer's repeat-repair escalation is safe because
        # only install.sh passes defaults, and a set-if-absent default can be
        # written at most once — but the MESSAGE must not cry "suppression was
        # MISSING" over a defaults-only write.
        CC_SUPPRESSION_STATE=repaired
        case "$out" in
            *DISABLE_*)
                echo "  ! CC auto-updater suppression was MISSING in $settings_file — restored: $out" >&2
                ;;
            *)
                echo "  . set-if-absent default(s) applied to $settings_file: $out" \
                     "(suppression keys verified present)" >&2
                ;;
        esac
    else
        # rc 0 with empty stdout: the reconciler READ both keys correct in this
        # process and wrote nothing. That read is the verification — the ONLY
        # way `ok` is ever granted.
        CC_SUPPRESSION_STATE=ok
    fi
    return 0
}


# cc_ensure_local — install or align the LOCAL Claude Code CLI to $CC_VERSION.
#
# Idempotent, non-fatal, drift-healing. Container/local ONLY — the host VM's CC
# is synced separately by update.sh via the guardian `update-cc` op. Callers
# decide fatality: `cc_ensure_local || true` (update.sh/bootstrap.sh, where a
# failure must never abort) or `cc_ensure_local || SETUP_WARNINGS=1` (install.sh).
#
# Behavior:
#   - npm missing            -> warn + return 0 (nothing we can do; skip)
#   - claude already at pin   -> return 0 (no-op)
#   - claude present, drifted -> reinstall @pin to the SAME prefix the existing
#                                binary resolves from (NOT `npm config get prefix`
#                                — the two can differ, which would install beside
#                                the live binary and leave `which claude` stale)
#   - claude absent (fresh)   -> install @pin to `npm config get prefix`
#                                (/usr remapped to /usr/local to avoid /usr/lib
#                                misrouting — matches install.sh)
#   - post-install: re-check `claude --version`; still != pin -> warn + return 1
#
# Exact-match to the pin (the pin may legitimately go DOWN for incident rollback;
# `npm install @X.Y.Z` pins exactly X.Y.Z). NOTE `claude --version` prints
# "2.1.173 (Claude Code)", so the version is field 1 (awk '{print $1}').
cc_ensure_local() {
    # FIRST, before any early return: re-assert auto-updater suppression. This is
    # deliberately ahead of the pin/npm checks — the common path is "already at
    # pin", which returns early, and that steady state is exactly when settings
    # drift would otherwise go unnoticed. Non-fatal: a failure here must never
    # stop the version align.
    cc_ensure_updater_suppressed || true

    local pin="${CC_VERSION:-}"
    if [ -z "$pin" ]; then
        echo "  cc_ensure_local: CC_VERSION unset — skipping" >&2
        return 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "  cc_ensure_local: npm not found — cannot manage Claude Code (skipping)" >&2
        return 0
    fi

    local existing current prefix p
    existing="$(command -v claude 2>/dev/null || true)"
    if [ -z "$existing" ]; then
        # PATH-blind check before declaring absence: a user-prefix install
        # (~/.npm-global with its PATH export in .bashrc AFTER the interactive
        # early-exit) is invisible to every non-interactive shell — which made
        # this function reinstall CC on EVERY update run on one machine while
        # a perfectly good copy sat one directory away.
        local _oldIFS="$IFS"
        IFS=':'
        for p in $CC_PROBE_DIRS; do
            if [ -x "$p/claude" ]; then
                existing="$p/claude"
                echo "  cc_ensure_local: claude found at $existing but NOT on this shell's PATH — treating as installed (fix the PATH wiring, or move the install to a system prefix)" >&2
                break
            fi
        done
        IFS="$_oldIFS"
    fi
    if [ -n "$existing" ]; then
        # Version via the resolved binary, not a PATH lookup — $existing may
        # have come from the PATH-blind prefix probe above.
        current="$("$existing" --version 2>/dev/null | awk '{print $1}')"
        if [ "$current" = "$pin" ]; then
            echo "  Claude Code already at pin ($pin)"
            return 0
        fi
        echo "--- Claude Code drift: ${current:-unknown} -> $pin (aligning) ---"
        # Reinstall to the existing binary's OWN prefix so `which claude` updates
        # (npm config prefix can differ, which would install a 2nd copy and leave
        # `which claude` stale). Assumes an npm-global install (Genesis has no
        # native-installer path — see docs/reference/cc-compatibility.md); a
        # non-npm launcher would be reinstalled to the wrong place, but the
        # post-install verify below downgrades that to a non-fatal warning.
        prefix="$(dirname "$(dirname "$existing")")"   # /usr/local/bin/claude -> /usr/local
    else
        echo "  Claude Code not installed — installing pinned $pin"
        prefix="$(npm config get prefix 2>/dev/null)"
        [ -n "$prefix" ] || prefix="/usr/local"        # guard empty-but-rc-0 output
        [ "$prefix" = "/usr" ] && prefix="/usr/local"  # avoid /usr/lib misrouting
    fi

    # Always pass --prefix explicitly (deterministic target). sudo only for
    # system prefixes; user prefixes (~/.npm-global, nvm) are user-writable.
    local -a npm_args
    npm_args=(npm install -g --prefix "$prefix" "@anthropic-ai/claude-code@${pin}")
    case "$prefix" in
        /usr|/usr/local|/opt/*)
            if [ "$(id -u)" != "0" ]; then
                if command -v sudo >/dev/null 2>&1; then
                    # PATH passthrough: npm is often nvm-managed + absent from
                    # sudo's secure_path (matches host-setup.sh).
                    npm_args=(sudo env "PATH=$PATH" "${npm_args[@]}")
                else
                    echo "  cc_ensure_local: sudo unavailable — cannot install to $prefix (skipping)" >&2
                    return 0
                fi
            fi
            ;;
    esac

    if ! timeout 300 "${npm_args[@]}"; then
        echo "  WARNING: cc_ensure_local: npm install failed (non-fatal)" >&2
        return 1
    fi
    hash -r 2>/dev/null || true   # drop bash's cached path to the old binary
    local installed
    # Verify against the binary we just installed, NOT a PATH lookup: in
    # non-interactive shells a user npm prefix (~/.npm-global, nvm) is often
    # absent from PATH, which made a SUCCESSFUL install report a false
    # "PATH mismatch" warning (seen live on a parity run 2026-07-04).
    installed="$("$prefix/bin/claude" --version 2>/dev/null | awk '{print $1}')"
    [ -n "$installed" ] || installed="$(claude --version 2>/dev/null | awk '{print $1}')"
    if [ "$installed" = "$pin" ]; then
        echo "  + Claude Code now at pin ($pin)"
        return 0
    fi
    echo "  WARNING: cc_ensure_local: install ran but 'claude --version' is ${installed:-unknown} (expected $pin) — possible npm-prefix/PATH mismatch" >&2
    return 1
}


# cc_shadow_scan — enforce the ONE-canonical-copy policy for Claude Code.
#
# Four real incidents motivated this (2026-07): an nvm-tree copy that shadowed
# the pinned CC in interactive shells only (user saw a months-old version); a
# native-installer symlink in ~/.local/bin doing the same; leftover native
# version blobs (~490MB dead weight); and a user-prefix copy invisible to
# non-interactive shells. Shadow copies drift silently because update-cc /
# cc_ensure_local only manage the canonical copy.
#
# Canonical = a copy that REPORTS THE PIN ($CC_VERSION): first the PATH
# resolution if it's at the pin, else the first at-pin copy in CC_PROBE_DIRS.
# Version-verified selection is the core safety property — `command -v` alone
# follows the INVOKING shell's PATH, and an interactive PATH can put a stale
# copy first (the exact incident class this scan exists to fix), which would
# otherwise crown the stale copy canonical and sudo-remove the good one.
# FAIL-SAFE: if NO copy at the pin exists anywhere, nothing is removed —
# a scan that cannot prove a good copy exists has no business deleting.
#
# Every OTHER copy on a known surface is removed, with loud logging. Removal
# is gated on the artifact being PROVABLY a claude-code install (npm package
# dir or a symlink into one, or the native-installer layout); anything
# unprovable is warned about and left alone. The canonical's own package dir
# and (for a native canonical) the native versions dir are never touched.
#
# Opt-out for deliberate multi-copy setups: CC_SHADOW_SCAN=0.
# Non-fatal by design; call as `cc_shadow_scan || true`.
cc_shadow_scan() {
    if [ "${CC_SHADOW_SCAN:-1}" = "0" ]; then
        echo "  cc_shadow_scan: disabled (CC_SHADOW_SCAN=0)"
        return 0
    fi
    local pin="${CC_VERSION:-}"
    if [ -z "$pin" ]; then
        echo "  cc_shadow_scan: CC_VERSION unset — skipping (cannot verify a canonical)" >&2
        return 0
    fi

    local canonical="" canon_real="" canon_pkg="" p v
    local -a _candidates=()
    p="$(command -v claude 2>/dev/null || true)"
    [ -n "$p" ] && _candidates+=("$p")
    local _oldIFS="$IFS"
    IFS=':'
    for p in $CC_PROBE_DIRS; do
        _candidates+=("$p/claude")
    done
    IFS="$_oldIFS"
    for p in "${_candidates[@]}"; do
        [ -x "$p" ] || continue
        v="$("$p" --version 2>/dev/null | awk '{print $1}')"
        if [ "$v" = "$pin" ]; then
            canonical="$p"
            break
        fi
    done
    if [ -z "$canonical" ]; then
        echo "  cc_shadow_scan: no claude at the pin ($pin) found — REFUSING to remove anything (align with cc_ensure_local / update-cc first)" >&2
        return 0
    fi
    canon_real="$(readlink -f "$canonical" 2>/dev/null || echo "$canonical")"
    # The canonical's own npm package dir — never removed, even when a STALE
    # extra symlink points into it (that link alone goes; nuking the package
    # would destroy the canonical).
    case "$canon_real" in
        */@anthropic-ai/claude-code/*)
            canon_pkg="$(readlink -f "${canon_real%%/@anthropic-ai/claude-code/*}/@anthropic-ai/claude-code" 2>/dev/null || true)"
            ;;
    esac

    # Native-installer version blobs: shadows by definition under the npm-only
    # canon (docs/reference/cc-compatibility.md), and BIG (~250MB each) —
    # UNLESS the canonical itself is a native install (then they ARE the
    # canonical's payload; leave them and let the operator migrate to npm).
    if [ -d "$HOME/.local/share/claude/versions" ]; then
        case "$canon_real" in
            "$HOME/.local/share/claude/"*)
                echo "  cc_shadow_scan: canonical is a native install — keeping $HOME/.local/share/claude/versions (consider migrating to the npm install path)" >&2
                ;;
            *)
                echo "  cc_shadow_scan: removing native-installer version blobs ($HOME/.local/share/claude/versions)"
                rm -rf "$HOME/.local/share/claude/versions"
                ;;
        esac
    fi

    local candidate real
    for candidate in \
        "$HOME"/.nvm/versions/node/*/bin/claude \
        "$HOME/.claude/local/claude" \
        "$HOME/.local/bin/claude" \
        "$HOME/.npm-global/bin/claude" \
        /usr/local/bin/claude \
        /usr/bin/claude; do
        [ -e "$candidate" ] || [ -L "$candidate" ] || continue
        real="$(readlink -f "$candidate" 2>/dev/null || echo "$candidate")"
        # The canonical copy itself (or a same-file alias like /bin vs /usr/bin
        # under usrmerge) is never touched.
        [ "$real" = "$canon_real" ] && continue
        _cc_remove_shadow "$candidate" "$canon_pkg"
    done

    # Aliases/functions can shadow every file-level fix — detect, never edit
    # a user's rc files.
    local rc hits
    for rc in "$HOME/.bashrc" "$HOME/.bash_aliases" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        hits="$(grep -nE '^[[:space:]]*alias claude=' "$rc" 2>/dev/null || true)"
        [ -n "$hits" ] && echo "  WARNING: cc_shadow_scan: 'claude' alias in $rc shadows the canonical copy — remove it manually: $hits" >&2
    done
    return 0
}

# _cc_remove_shadow <path> <canon_pkg> — remove one shadow copy, ONLY if
# provably a claude-code install. System-prefix removals need passwordless
# sudo; user paths are removed directly. Unprovable artifacts are warned and
# kept. A package dir equal to <canon_pkg> (the canonical's own package) is
# never removed — only the stale link into it.
_cc_remove_shadow() {
    local candidate="$1" canon_pkg="${2:-}" target pkg_dir=""
    target="$(readlink "$candidate" 2>/dev/null || true)"

    if [[ "$target" == *"@anthropic-ai/claude-code"* ]]; then
        # npm-style symlink → the package dir it points into (resolve relative
        # to the symlink's own directory).
        pkg_dir="$(cd "$(dirname "$candidate")" 2>/dev/null && cd "$(dirname "$target")" 2>/dev/null && pwd)"
        pkg_dir="${pkg_dir%%/@anthropic-ai/claude-code*}/@anthropic-ai/claude-code"
        [[ "$pkg_dir" == *"@anthropic-ai/claude-code" && -d "$pkg_dir" ]] || pkg_dir=""
    elif [[ "$candidate" == "$HOME/.claude/local/claude" ]]; then
        # migrate-installer layout: the launcher plus its own npm subtree
        # (~/.claude/local/node_modules/...) — remove both, not just the
        # launcher (the package tree is hundreds of MB of dead weight).
        pkg_dir="$HOME/.claude/local/node_modules/@anthropic-ai/claude-code"
        [ -d "$pkg_dir" ] || pkg_dir=""
    elif [[ "$target" == *"/.local/share/claude/"* ]]; then
        # Native-installer symlink — the blob dir is handled (and guarded)
        # by the sweep in cc_shadow_scan; only the link goes here.
        pkg_dir=""
    else
        echo "  WARNING: cc_shadow_scan: $candidate is not provably a claude-code install — left in place (remove manually if it is one)" >&2
        return 1
    fi

    # Never rm -rf the canonical's own package — a stale SECOND link into it
    # (e.g. an old entry-file path) loses only the link.
    if [ -n "$pkg_dir" ] && [ -n "$canon_pkg" ] \
        && [ "$(readlink -f "$pkg_dir" 2>/dev/null)" = "$canon_pkg" ]; then
        echo "  cc_shadow_scan: $candidate is a stale link into the CANONICAL package — removing the link only"
        pkg_dir=""
    fi

    local -a rm_link=(rm -f "$candidate")
    local -a rm_pkg=()
    [ -n "$pkg_dir" ] && rm_pkg=(rm -rf "$pkg_dir")
    case "$candidate" in
        /usr/*|/opt/*)
            if ! sudo -n true 2>/dev/null; then
                echo "  WARNING: cc_shadow_scan: shadow at $candidate needs sudo to remove — skipped" >&2
                return 1
            fi
            rm_link=(sudo -n "${rm_link[@]}")
            [ -n "$pkg_dir" ] && rm_pkg=(sudo -n "${rm_pkg[@]}")
            ;;
    esac
    echo "  cc_shadow_scan: removing shadow copy $candidate${pkg_dir:+ (+ $pkg_dir)}"
    "${rm_link[@]}"
    [ -n "$pkg_dir" ] && "${rm_pkg[@]}"
    return 0
}


# cc_align_host_sync — align the HOST VM's Node.js major + Claude Code to the
# repo pins via the guardian gateway, healing drift. Extracted from update.sh's
# _sync_deploy_targets so BOTH update.sh and the nightly genesis-cc-align timer
# (scripts/cc_align_host.sh) share ONE implementation — a pin bump reaches the
# host's `claude -p` recovery brain without waiting for the next manual update.
#
# Args: <host_user> <host_ip> <ssh_key> <host_ver_raw>
#   host_ver_raw = raw JSON from a prior `ssh … version` call. The CALLER fetches
#   it once (update.sh reuses the same response for its redeploy decision), so
#   this function never issues the version probe itself.
# Reads globals: NODE_MAJOR, CC_VERSION (set at the top of this file when sourced).
# APPENDS any alignment failure to the global HOST_CC_DEGRADED (comma-joined; the
#   `:+` form is set -u-safe). It NEVER re-inits that global — the caller owns the
#   init and may set sibling sentinels (guardian_config_unreadable) in branches
#   this function doesn't cover. Progress → stdout.
# Non-fatal by contract: ALWAYS returns 0 so a host hiccup can't abort an
#   update run under set -e (the ERR trap is already disarmed by then). Call as
#   `cc_align_host_sync … || true` to match the cc_ensure_local convention.
cc_align_host_sync() {
    local host_user="$1" host_ip="$2" ssh_key="$3" host_ver_raw="$4"
    local host_node_major host_cc

    # HOST_VER_RAW empty = genuinely could not reach/parse the gateway — DISTINCT
    # from "CC absent" (the conflation the old inline message got wrong).
    if [ -z "$host_ver_raw" ]; then
        echo "  Host gateway unreachable (no version response) — skipping Node/CC sync (non-fatal)"
        HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_unreachable"
        return 0
    fi

    host_node_major="$(printf '%s' "$host_ver_raw" \
        | grep -oE '"node_version": "v[0-9]+' | grep -oE '[0-9]+' || true)"
    host_cc="$(printf '%s' "$host_ver_raw" \
        | grep -oE '"cc_version": "[0-9]+\.[0-9]+\.[0-9]+' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"

    # ── Persist the probe for the deploy-staleness check ──
    # observability/snapshots/deploy_health.py reads deployed_commit from this
    # state file so the health path never SSHes. Both writers (update.sh and
    # the nightly cc-align timer) funnel through here. Atomic tmp+mv; a failed
    # parse (gateway emitted non-JSON) or unreachable probe (early return
    # above) never clobbers the last-known-good state.
    local _state_file="$HOME/.genesis/host_gateway_state.json"
    local _state_tmp="${_state_file}.tmp.$$"
    if GENESIS_HOST_VER_RAW="$host_ver_raw" python3 - "$_state_tmp" 2>/dev/null <<'PYEOF'
import json
import os
import sys
from datetime import UTC, datetime

payload = {
    "checked_at": datetime.now(UTC).isoformat(),
    "version": json.loads(os.environ["GENESIS_HOST_VER_RAW"]),
}
with open(sys.argv[1], "w") as f:
    json.dump(payload, f, indent=2)
PYEOF
    then
        mv -f "$_state_tmp" "$_state_file" 2>/dev/null || rm -f "$_state_tmp"
    else
        rm -f "$_state_tmp"
    fi

    # ── Node.js major sync (prerequisite for CC) ──
    if printf '%s' "${NODE_MAJOR:-}" | grep -qE '^[0-9]{1,2}$'; then
        if [ "$host_node_major" = "$NODE_MAJOR" ]; then
            echo "  Host Node.js already at major $NODE_MAJOR — no Node sync needed"
        else
            echo "--- Host Node.js: ${host_node_major:-unknown} → syncing to major $NODE_MAJOR ---"
            # 600s: NodeSource repo-add + apt install is heavier than an npm
            # install (update-cc uses 300s); bounds a hung dpkg lock.
            if timeout 600 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-node $NODE_MAJOR" 2>&1; then
                echo "  Host Node.js updated to major $NODE_MAJOR"
                host_node_major="$NODE_MAJOR"
            else
                echo "  WARNING: Host Node.js sync failed — CC install will likely fail (host stays on ${host_node_major:-unknown})"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_node"
            fi
        fi
    fi

    # ── Claude Code sync: absence => INSTALL, drift => update ──
    if printf '%s' "${CC_VERSION:-}" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        if [ -z "$host_cc" ]; then
            # cc_version was "unavailable"/unparseable → CC is NOT installed on the
            # host. INSTALL it (do not skip) — the exact case the old code silently
            # ignored, leaving Guardian's recovery brain offline.
            echo "--- Host Claude Code not installed — installing $CC_VERSION ---"
            if timeout 300 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-cc $CC_VERSION" 2>&1; then
                echo "  Host Claude Code installed ($CC_VERSION)"
            else
                echo "  WARNING: Host Claude Code install FAILED — Guardian intelligent recovery is OFFLINE"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
            fi
        elif [ "$host_cc" = "$CC_VERSION" ]; then
            echo "  Host Claude Code already at pin ($CC_VERSION) — no CC sync needed"
        else
            echo "--- Host Claude Code drift: $host_cc → syncing to $CC_VERSION ---"
            if timeout 300 ssh -i "$ssh_key" -o BatchMode=yes -o ConnectTimeout=30 \
                "${host_user}@${host_ip}" "update-cc $CC_VERSION" 2>&1; then
                echo "  Host Claude Code updated to $CC_VERSION"
            else
                echo "  WARNING: Host Claude Code sync failed — host remains on $host_cc"
                HOST_CC_DEGRADED="${HOST_CC_DEGRADED:+$HOST_CC_DEGRADED,}guardian_host_cc"
            fi
        fi
    fi
    return 0
}
