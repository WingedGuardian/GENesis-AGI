"""Whether ~/.genesis/update_state.json still guards merged code (#3139).

update.sh writes update_state.json at each phase. An update that merged code it
has not activated (a crash, a refused or interrupted ``--post-merge``) leaves it
behind, and so does a merge-conflict exit, whose resolver needs its rollback
tag. deploy_code_only.sh and the live engine refuse to restart or move the
checkout while such a file stands, and update.sh and the dashboard refuse to
overwrite or delete it. All of them ask this module, so the shell and the
dashboard cannot disagree.

Two kinds of guarding state, because ``--post-merge`` finishes only one (#3532):
GUARDS is merged code complete on disk that bootstrap has not run on, exactly
what ``--post-merge`` finishes; HOLD is every other guarding state, where
finishing forward is wrong or cannot be known (the update never changed the
checkout, the checkout is not where it left it, the record cannot be trusted, a
merge is in progress), and its message names the remedy instead.

Verdicts, in order:

1. no file: SAFE;
2. a file that cannot be read or parsed, or is not a JSON object: HOLD;
3. phase "done": SAFE (written just before update.sh deletes the file);
3a. a merge in progress in the checkout (MERGE_HEAD), in any phase: HOLD;
4. HEAD at the update's own new code (``own_head``, a full commit id other than
   the tag's), in any phase: MIXED_TREE (forward) when the two-dot residue back
   toward the tag is non-empty, a rollback checkout killed partway (#3532);
5. phase "fetching": SAFE only when the rollback tag is HEAD, the state names
   the commit it fetched (``deploy_head``), and it says a plain run wrote it
   (``post_merge`` exactly false). A ``--post-merge`` run writes "fetching" over
   code that is already merged, sometimes with a fallback tag made at that
   merged HEAD, and a state without the key (older, or hand-edited) cannot rule
   that out: GUARDS, unless it names its own_head and HEAD is elsewhere, or
   its tag cannot be resolved: HOLD. A plain run's row anywhere else: HOLD;
6. phase "merging" with the rollback tag at HEAD: residue
   (``genesis_merge_residue`` in deploy_checkout.sh: tracked changes, ephemeral
   paths included, on the paths the merge ``rollback...deploy_head`` writes,
   every tracked change when the row names no ``deploy_head``, and files the
   range adds found untracked) is MIXED_TREE, the checkout holding part of a
   half-written update. A tracked change only outside the merge's paths is
   someone else's edit and the update merged nothing: HOLD. Else
   CONFLICT_PENDING when update_conflicts.json names the same ``deploy_head``
   (update.sh writes it only for a real conflict), otherwise SAFE (a run killed
   before its merge). HEAD at ``own_head`` (whole, by rule 4): GUARDS; with
   own_head still the tag's commit, HEAD exactly the merge update.sh's adoption
   rule accepts (deploy_head by fast-forward, or a two-parent merge of the tag
   and deploy_head, on original_branch: a run that died before recording it):
   GUARDS; the resolver's committed merge of a recorded conflict on top of the
   tag: GUARDS (both checked by rule 4's residue toward HEAD);
   a row from before ``own_head`` was recorded: GUARDS, as before; anything else
   (a checkout moved elsewhere, a merge in progress, no rollback tag): HOLD;
7. a "merging" row carrying the live engine's move record ``live_move``
   ({"to": <tip>, "phase": ref|moving|partial|clean|done}, which the engine
   writes into update.sh's own row before and after each move, #3492). Every
   residue there is the TWO-DOT diff a `git switch` writes, never main's
   symmetric range: on `live` the old and new tips diverge. HEAD at the tag:
   MIXED_TREE when the record says "partial" or the two-dot residue toward
   ``to`` is non-empty (a "clean" record, git having refused and written
   nothing, counts tracked changes only), HOLD for a tracked change only
   outside the switch's paths (someone else's edit; the rebuild moved
   nothing), else SAFE. HEAD on `live` at ``to``: MIXED_TREE (forward) when the
   two-dot residue back toward the tag is non-empty, else GUARDS (moved, not
   bootstrapped). Anywhere else, or a record of any other shape, or one under
   any phase but "merging": HOLD. The MIXED_TREE remedy follows HEAD: at the
   tag, put the paths back and Dismiss; at the update's new code, put them back
   and finish with --post-merge;
7a. a "merging" row on `live` with no record and an own_head other than the
   tag (update.sh adopted the tip, and its row no longer carries the record)
   is judged the same way toward own_head, and that verdict is final (main's
   rule 6 would judge the upstream base range, which no live switch writes);
8. phases "bootstrap", "migrations", "health_check": GUARDS (the update's code
   awaits activation), but HOLD when HEAD is not ``own_head`` or the rollback
   tag cannot be resolved for rule 4's check; any other phase, or none: HOLD.

The pid is not an input: callers answer liveness first (update.lock in the
shell, env.update_in_progress() in the dashboard). Anything this module cannot
decide guards. Retiring a guarding file on purpose is the job of that update's
``--post-merge`` (GUARDS only: a resolved conflict reads GUARDS, a pending one
merged nothing), bootstrap's crash recovery, or the dashboard's Dismiss.

Command line (``python3 -I -S``, standard library only)::

    update_state_guard.py <state-file> <repo> [<conflicts-file>]

Exit 10 when the file guards nothing, 0 when it guards merged code that
``--post-merge`` finishes, 3 when it is a pending merge conflict, 4 when the
checkout is half-written (MIXED_TREE: the message lists the paths), 6 when it
guards and finishing forward is wrong or unknown (HOLD); the remedy is printed
on stdout. Any failure inside the check (git or bash not runnable, a git call
timing out) exits 1: it judged nothing, so it joins the "could not run" class
with Python's own 1 on an uncaught exception, 2 on a usage error, and a shell's
126/127. Callers keep the state on every exit but 10, and --post-merge
finishes only 0. Only exit 10 lets a caller go on.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

SAFE = "safe"
GUARDS = "guards"
CONFLICT_PENDING = "conflict_pending"
MIXED_TREE = "mixed_tree"
#: Guards, and finishing FORWARD is wrong or cannot be known (#3532 round 2):
#: the update never changed the checkout, the checkout is not where it left it,
#: the record cannot be trusted, a merge is in progress. GUARDS is the other
#: half: merged code complete on disk that bootstrap has not run on, which is
#: exactly what ``--post-merge`` finishes.
HOLD = "holds"

# SAFE is 10, a code no default failure produces: Python exits 1 on an uncaught
# exception and 2 on a usage error, a shell 126/127 when it cannot run the file,
# so none of those can ever read as "nothing to guard".
EXIT_CODES = {GUARDS: 0, SAFE: 10, CONFLICT_PENDING: 3, MIXED_TREE: 4, HOLD: 6}

#: The engine's move phases, as deploy_candidates_plan.py writes them.
MOVE_PHASES = ("ref", "moving", "partial", "clean", "done")
_HEX = frozenset("0123456789abcdef")
#: A name holding one of these (a C0 or C1 control character, DEL, a line or
#: paragraph separator, or a byte that is not UTF-8, carried as a surrogate) is shown in bash's $'...' form, so the
#: line stays one line and nothing reaches a terminal raw.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\udc80-\udcff]")

_LIB = os.path.dirname(os.path.abspath(__file__))


# The dashboard runs this while it holds update.lock in a request thread, so a
# hung git (a wedged filesystem, a frozen cgroup) must not hold the lock for good.
# 120 s matches the dashboard's other checks; TimeoutExpired reaches every
# caller's exception path, which keeps the state (guards).
_TIMEOUT_S = 120


def _env() -> dict[str, str]:
    # Local reads only; GIT_* from the caller would point git elsewhere.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _rev(repo: str, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "-q", rev],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()


def _tag_commit(state: dict, repo: str) -> str:
    """The rollback tag's commit ("" when it names none). The value must be a
    valid tag NAME (git check-ref-format, as bootstrap.sh checks it): "pre~0" or
    "pre^{commit}" is a revision expression, which would resolve to a commit no
    such tag points at."""
    tag = state.get("rollback_tag")
    if not isinstance(tag, str) or not tag or tag.startswith("-"):
        return ""
    ok = subprocess.run(
        ["git", "-C", repo, "check-ref-format", f"refs/tags/{tag}"],
        capture_output=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    )
    if ok.returncode != 0:
        return ""
    return _rev(repo, f"refs/tags/{tag}^{{commit}}")


def _tag_commit_is_head(state: dict, repo: str) -> str | None:
    """The rollback tag's commit when it is HEAD, else None."""
    tagged = _tag_commit(state, repo)
    head = _rev(repo, "HEAD^{commit}")
    return tagged if tagged and tagged == head else None


def _move_record(state: dict) -> tuple[str, str] | None:
    """(to, phase) of a well-formed ``live_move``; None for any other shape."""
    rec = state.get("live_move")
    if not isinstance(rec, dict):
        return None
    to, phase = rec.get("to"), rec.get("phase")
    if not (isinstance(to, str) and len(to) in (40, 64) and set(to) <= _HEX):
        return None
    return (to, phase) if phase in MOVE_PHASES else None


#: genesis_merge_residue, and with ``$6`` set, without the records on the
#: known-ephemeral tracked paths (EPHEMERAL_DIRTY_RE, defined once in
#: deploy_marker.sh), the path matched whole: a tracked "d/x AGENTS.md" counts.
_RESIDUE_SH = """
. "$1/deploy_checkout.sh" || exit 1
if [ -z "$6" ]; then genesis_merge_residue "$2" "$3" "$4" ${5:+"$5"}; exit; fi
. "$1/deploy_marker.sh" && [ -n "${EPHEMERAL_DIRTY_RE:-}" ] || exit 1
set -o pipefail
genesis_merge_residue "$2" "$3" "$4" ${5:+"$5"} | while IFS= read -r -d '' r; do
    [[ "$r" != '??'* && " ${r:3}" =~ ^($EPHEMERAL_DIRTY_RE)$ ]] || printf '%s\\0' "$r"
done
"""


def _merge_residue(
    repo: str,
    rb_commit: str,
    deploy_head: str,
    two_dot: bool = False,
    *,
    ephemeral_ok: bool = False,
) -> list[str]:
    """genesis_merge_residue's records (NUL-separated on the wire, so every
    legal file name arrives exactly; bytes that are not UTF-8 are kept as
    surrogates); any failure to run it is residue. ``ephemeral_ok`` leaves out
    the known-ephemeral paths."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            _RESIDUE_SH,
            "update_state_guard",
            _LIB,
            repo,
            rb_commit,
            deploy_head,
            "--two-dot" if two_dot else "",
            "1" if ephemeral_ok else "",
        ],
        capture_output=True,
        text=True,
        errors="surrogateescape",
        env=_env(),
        timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        return [f"(the merge residue check failed: exit {proc.returncode})"]
    return [rec for rec in proc.stdout.split("\0") if rec]


def _conflict_recorded(conflicts_path: str, deploy_head: str) -> bool:
    """Does the conflict record name this commit? A record that exists but
    cannot be read, parsed, or is not an object counts as naming it: doubt keeps
    the conflict the resolver is waiting on."""
    if not os.path.lexists(conflicts_path):
        return False
    try:
        with open(conflicts_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    return data.get("deploy_head") == deploy_head


#: How a MIXED_TREE reason starts when HEAD is at the update's own new code (a
#: rebuilt live tip, or main's recorded merge): restoring the listed paths to
#: HEAD then puts the FORWARD tree back, which --post-merge finishes (message()
#: reads it to give that remedy).
_FORWARD = "the checkout is at the update's new code"

#: HOLD remedies, one per kind of state. None recommends --post-merge: it
#: refuses every one of them.
_NOT_MOVED = (
    "The update never changed the checkout, so there is nothing to finish: save "
    "that edit if you want it (it may be another session's), then Dismiss the "
    "update on the dashboard's update card and run scripts/update.sh again"
)
_MOVED_AWAY = (
    "The checkout is not where the update left it, so finishing the update would "
    "activate code it never checked: put the checkout where you want it, then "
    "Dismiss the update on the dashboard's update card and run scripts/update.sh again"
)
_UNTRUSTED = (
    "Nothing can be told from the record: check the checkout by hand (git status, "
    "git log -1), then Dismiss the update on the dashboard's update card and run "
    "scripts/update.sh again"
)
_IN_MERGE = (
    "Finish or abort that merge first, and the state is judged again. A conflict "
    "resolver the update started may be working in this checkout: abort only when "
    "none is (the dashboard's update card shows none running and "
    "~/.genesis/update_in_progress.pid names no live process), saving any edits "
    "you want before git merge --abort; otherwise wait for it"
)
_NO_TAG = (
    "Without its rollback tag the checkout cannot be checked for a half-written "
    "tree: check it by hand (git status), then Dismiss the update on the "
    "dashboard's update card and run scripts/update.sh again"
)


def _hold(reason: str, remedy: str) -> tuple[str, str]:
    """HOLD, its reason carrying the remedy (message() prints it)."""
    return HOLD, f"{reason}. {remedy}"


def _own_head(state: dict) -> str:
    """The update's own new code (update.sh records it the moment it adopts its
    merge or rebuilt tip), as a full commit id; "" when the row names none."""
    own = state.get("own_head")
    return own if isinstance(own, str) and len(own) in (40, 64) and set(own) <= _HEX else ""


def _shell_token(name: str) -> str:
    """A raw file name as ONE shell token that pastes back as the same name:
    shlex.quote, or bash's $'...' when the name holds a control character or a
    non-UTF-8 byte (shlex would keep those raw, splitting the line or reaching
    a terminal)."""
    if not _CONTROL.search(name):
        return shlex.quote(name)
    out = []
    for ch in name:
        code = ord(ch)
        if ch in "\\'":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif _CONTROL.match(ch):  # C1, U+2028/9: its UTF-8 bytes, needing no locale
            out.append("".join(f"\\x{byte:02x}" for byte in ch.encode()))
        else:
            out.append(ch)
    return "$'" + "".join(out) + "'"


def _shown(record: str) -> str:
    """A residue record as one display line: its status, then the path as one
    shell token ("(...)" notes as they are)."""
    if record.startswith("("):
        return record
    return record[:3] + _shell_token(record[3:])


def _mixed(tag: str, to: str, residue: list[str], *, forward: bool = False) -> tuple[str, str]:
    """MIXED_TREE, its reason naming the two commits and listing every path.
    ``forward``: HEAD is at ``to``, not the tag, so the remedy finishes forward."""
    lines = [_shown(r) for r in residue] or [
        "(the rebuild recorded that git wrote part of the tree)"
    ]
    where = (
        f"{_FORWARD} ({to[:12]}), half-written back toward {tag[:12]}"
        if forward
        else f"the checkout is half-written between {tag[:12]} and {to[:12] or 'its merge'}"
    )
    return MIXED_TREE, where + ":\n" + "\n".join("  " + ln for ln in lines)


def _forward(repo: str, tag: str, to: str) -> tuple[str, str] | None:
    """HEAD is at ``to``, the update's new code: MIXED_TREE (forward) when the
    two-dot residue back toward the tag is non-empty, the paths a rollback
    checkout from ``to`` to the tag writes (one killed partway leaves HEAD at
    ``to`` with tag-side files written); None when the tree is whole. The one
    implementation for main and `live` (#3532 round 2).

    The known-ephemeral paths do not count here (round 4): with HEAD at the new
    code the update has adopted it, and the runtime rewrites those paths
    (GitNexus, AGENTS.md's stat block) while bootstrap, migrations and the
    health check run; update.sh backs them up and clears them before every
    merge and rebuild, and a half-written one loses nothing. The checks at the
    rollback tag still count them: that is main's existing, tested behaviour
    (test_merging_at_the_tag_with_tracked_changes_guards), not a safety need,
    and changing it is tracked on its own (#3566)."""
    residue = _merge_residue(repo, to, tag, two_dot=True, ephemeral_ok=True)
    return _mixed(tag, to, residue, forward=True) if residue else None


def _live_verdict(state: dict, repo: str, to: str, move_phase: str) -> tuple[str, str]:
    """A live row judged toward ``to``: the engine's record (rule 6), or the
    adopted tip once update.sh dropped the record (own_head, rule 6a). Only the
    paths a switch between the tag and ``to`` writes count as residue; a
    tracked change anywhere else is someone else's edit, which guards (as it
    always has) but is not a half-written tree."""
    tag = _tag_commit(state, repo)
    head = _rev(repo, "HEAD^{commit}")
    if not tag or not head:
        return _hold("its rollback tag or HEAD cannot be resolved", _NO_TAG)
    if head == tag:
        # "clean": the engine compared the tree before and after, and git wrote
        # nothing, so nothing on disk is the move's.
        if move_phase != "clean":
            residue = _merge_residue(repo, tag, to, two_dot=True)
            if residue or move_phase == "partial":
                return _mixed(tag, to, residue)
        if _merge_residue(repo, tag, ""):
            return _hold("tracked files changed that the move does not write", _NOT_MOVED)
        return SAFE, "the rebuild moved nothing"
    branch = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()
    if head == to and branch == "refs/heads/live":
        return _forward(repo, tag, to) or (
            GUARDS,
            f"it moved live to {to[:12]}, which bootstrap has not run on",
        )
    return _hold(
        "the checkout is neither at its rollback tag nor at the rebuilt live tip", _MOVED_AWAY
    )


def _adopted_tip(state: dict, repo: str) -> str | None:
    """Rule 6a: a "merging" row on `live` with no record, whose own_head is a
    commit other than the tag's, is the row update.sh wrote after adopting the
    rebuilt tip (the record goes with that write). Its rollback can still be
    killed partway, so the row is judged toward own_head as a record would be."""
    if state.get("original_branch") != "live" or "live_move" in state:
        return None
    own = state.get("own_head")
    if not (isinstance(own, str) and len(own) in (40, 64) and set(own) <= _HEX):
        return None
    tag = _tag_commit(state, repo)
    return own if tag and own != tag else None


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
            env=_env(),
            timeout=_TIMEOUT_S,
        ).returncode
        == 0
    )


def _merge_landed(state: dict, repo: str, tag: str, head: str, deploy_head: str) -> bool:
    """Is HEAD exactly the merge update.sh's plain run would have adopted as
    its own? Mirrors its adoption rule (update.sh, the `_merged_head` /
    `_merged_parents` test after `git merge`, and the check right after it):
    on ORIGINAL_BRANCH, containing deploy_head, and either deploy_head itself
    (a fast-forward from the tag) or a two-parent merge whose first parent is
    the tag and second deploy_head. The case update.sh accepts with HEAD at the
    tag (a no-op merge) is the tag rule's."""
    branch = state.get("original_branch")
    if not (tag and head and deploy_head and isinstance(branch, str) and branch):
        return False
    on = subprocess.run(
        ["git", "-C", repo, "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()
    if on != f"refs/heads/{branch}":
        return False
    full = _rev(repo, f"{deploy_head}^{{commit}}")
    if not full or not _is_ancestor(repo, full, head):
        return False
    if head == full:
        return _is_ancestor(repo, tag, head)
    parents = subprocess.run(
        ["git", "-C", repo, "rev-parse", f"{head}^@"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.split()
    return parents == [tag, full]


def state_verdict(state: object, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for an already-parsed state. Returns (verdict, reason)."""
    if not isinstance(state, dict):
        return _hold("it cannot be read", _UNTRUSTED)
    phase = state.get("phase")
    if "live_move" in state and phase != "merging":
        return _hold(f"it carries a live move record in phase {phase!r}", _UNTRUSTED)
    if phase == "done":
        return SAFE, "it finished"
    # One check for every phase: no verdict below is true of a checkout in the
    # middle of a merge, and finishing forward would bootstrap over conflict
    # markers (#3532 round-2 audit).
    if _rev(repo, "MERGE_HEAD"):
        return _hold("a merge is in progress in the checkout", _IN_MERGE)
    if "live_move" in state:
        move = _move_record(state)
        if move is None:
            return _hold("its live move record cannot be read", _UNTRUSTED)
        return _live_verdict(state, repo, *move)
    deploy_head = state.get("deploy_head")
    deploy_head = deploy_head if isinstance(deploy_head, str) else ""
    own = _own_head(state)
    tag = _tag_commit(state, repo)
    head = _rev(repo, "HEAD^{commit}")
    # HEAD at the update's own new code in ANY phase (main's merge or a live
    # tip): a rollback checkout back to the tag killed partway leaves HEAD there
    # with tag-side files written (#3532 round 2).
    if own and tag and own != tag and head == own:
        mixed = _forward(repo, tag, own)
        if mixed:
            return mixed
    if phase == "fetching":
        # Only a state a plain run wrote (post_merge exactly False) can be safe: a
        # --post-merge, a state from before the key existed, or a hand edit may
        # be "fetching" over merged code at a fallback tag made at that HEAD.
        if state.get("post_merge") is not False:
            # A --post-merge names the code it was finishing (own_head): finish
            # it only where it was left (audit 1).
            if own and head != own:
                return _hold(
                    "the run finishing an earlier merge stopped while fetching, and HEAD is not its code",
                    _MOVED_AWAY,
                )
            if own and not tag:
                return _hold(
                    "the run finishing an earlier merge stopped; its rollback tag cannot be resolved",
                    _NO_TAG,
                )
            return GUARDS, "it may be a --post-merge run that stopped before bootstrap"
        if deploy_head and tag and tag == head:
            return SAFE, "it stopped before merging anything"
        if tag and tag == head:
            return _hold("it stopped while fetching and names no fetched commit", _UNTRUSTED)
        return _hold("phase 'fetching', but HEAD is not its rollback tag", _MOVED_AWAY)
    if phase == "merging":
        adopted = _adopted_tip(state, repo)
        if adopted:
            # Final: main's rule 6 would judge the upstream base range
            # (deploy_head), which is not what a switch between the two live
            # tips writes (#3532 round 1).
            return _live_verdict(state, repo, adopted, "done")
        if tag and head == tag:
            residue = _merge_residue(repo, tag, deploy_head)
            if residue:
                return _mixed(tag, deploy_head, residue)
            # A tracked change the merge never writes (residue counts only the
            # merge's paths): someone else's edit, and the update merged nothing.
            if deploy_head and _merge_residue(repo, tag, ""):
                return _hold("tracked files changed while it was merging", _NOT_MOVED)
            if deploy_head and conflicts_path and _conflict_recorded(conflicts_path, deploy_head):
                return CONFLICT_PENDING, "it stopped on merge conflicts; nothing was merged"
            return SAFE, "it stopped before merging anything"
        if own and tag and own != tag and head == own:
            return GUARDS, "it merged code bootstrap has not run on"
        if own and head == own and not tag:
            return _hold("its rollback tag cannot be resolved", _NO_TAG)
        if own == tag and _merge_landed(state, repo, tag, head, deploy_head):
            # update.sh merged and died before recording the merge as its own
            # (own_head still the tag's commit): the merge it would have
            # adopted, so finishing it is right (audit 3). "Dismiss and run
            # again" would leave it never bootstrapped: the rerun finds nothing.
            return _forward(repo, tag, head) or (GUARDS, "it merged code bootstrap has not run on")
        if (
            tag
            and deploy_head
            and conflicts_path
            and _conflict_recorded(conflicts_path, deploy_head)
            and _is_ancestor(repo, tag, head)
            and _is_ancestor(repo, deploy_head, head)
        ):
            # The conflict resolver merged deploy_head on top of the tag: the
            # merge --post-merge exists to finish.
            return _forward(repo, tag, head) or (
                GUARDS,
                "its merge conflicts were resolved; bootstrap has not run on the merge",
            )
        if not own and tag and head:
            # A row from before own_head was recorded: as before, finishing it
            # is the operator's call.
            return GUARDS, "it merged code bootstrap has not run on"
        return _hold("the checkout is neither at its rollback tag nor at its merge", _MOVED_AWAY)
    if phase in ("bootstrap", "migrations", "health_check"):
        if own and head != own:
            return _hold(f"it stopped in phase '{phase}', and HEAD is not its code", _MOVED_AWAY)
        if own and not tag:
            return _hold(
                f"it stopped in phase '{phase}'; its rollback tag cannot be resolved", _NO_TAG
            )
        return GUARDS, f"it stopped in phase '{phase}'"
    if isinstance(phase, str) and phase:
        return _hold(f"it names a phase no update writes ({phase!r})", _UNTRUSTED)
    return _hold("it names no phase", _UNTRUSTED)


def verdict(state_path: str, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for the file on disk."""
    if not os.path.lexists(state_path):
        return SAFE, "there is no state file"
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return _hold("it cannot be read", _UNTRUSTED)
    if conflicts_path is None:
        conflicts_path = os.path.join(
            os.path.dirname(os.path.abspath(state_path)), "update_conflicts.json"
        )
    return state_verdict(state, repo, conflicts_path)


def message(state_path: str, verdict_: str, reason: str) -> str:
    """What every refusing caller says, so the remedy reads the same everywhere."""
    if verdict_ == MIXED_TREE:
        # The one place the paths are printed from (#3492): callers print this.
        # Restoring to HEAD gives back whichever tree HEAD is: the code that ran
        # before at the tag, the rebuilt tip at it, so the last step differs.
        head = (
            f"{state_path} records an update that stopped partway: {reason}\n"
            "These paths hold part of an update's checkout. They may also include another "
            "session's edits: save any you want to keep (copy them aside) before restoring "
            "anything. Then restore tracked paths to HEAD with git restore --source=HEAD "
            "--staged --worktree -- <path>, delete the listed untracked files, and remove a "
            "stale .git/index.lock if no git process is running. "
        )
        if reason.startswith(_FORWARD):
            return head + (
                "HEAD is the update's new code, so the checkout then holds that tree: finish with "
                "scripts/update.sh --post-merge (it passes once these paths are back). "
                "Dismissing the update instead (the dashboard's update card) leaves a checkout "
                "holding code bootstrap never ran on, with nothing to stop a restart onto it."
            )
        return head + (
            "HEAD is the code that ran before the update, so the checkout is then that code "
            "again: Dismiss the update on the dashboard's update card (or remove "
            "update_state.json). Do NOT run scripts/update.sh --post-merge: it refuses a "
            "half-written tree."
        )
    if verdict_ == HOLD:
        return (
            f"{state_path} records an update that cannot be finished forward: {reason}. "
            "(scripts/update.sh --post-merge refuses it.)"
        )
    if verdict_ == CONFLICT_PENDING:
        return (
            f"{state_path} records an earlier update that stopped on merge conflicts; "
            "nothing was merged. Resolve them (Resolve with Opus on the dashboard's "
            "update card, or merge the deploy_head in update_conflicts.json and run "
            "scripts/update.sh --post-merge), or Dismiss it on the dashboard's update card."
        )
    return (
        f"{state_path} records an earlier update that has not finished ({reason}). "
        "Finish it with scripts/update.sh --post-merge, or Dismiss it on the "
        "dashboard's update card."
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: update_state_guard.py <state-file> <repo> [<conflicts-file>]", file=sys.stderr
        )
        return 2
    state_path, repo = argv[0], argv[1]
    try:
        v, reason = verdict(state_path, repo, argv[2] if len(argv) == 3 else None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - any failure to decide could not run
        # Exit 1, not GUARDS' 0: a check that failed partway judged nothing,
        # and --post-merge finishes a guarding state, so 0 would let it go past
        # a half-written tree nobody looked at (#3492). Every caller proceeds
        # only on 10, and --post-merge refuses 1.
        print(
            f"{state_path} could not be checked ({type(exc).__name__}); it is kept. Retry once "
            "the check can run (scripts/update.sh --post-merge refuses until then), or Dismiss "
            "it on the dashboard's update card."
        )
        return 1
    if v != SAFE:
        print(message(state_path, v, reason))
    return EXIT_CODES[v]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
