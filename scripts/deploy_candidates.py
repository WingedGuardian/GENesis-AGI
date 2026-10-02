#!/usr/bin/env python3
"""deploy_candidates.py — the engine of `live`, the local integration branch.

`live` is a LOCAL branch: origin/main plus the candidate branches named in the
deploy manifest (``$HOME/.genesis/deploy_manifest.json``, the path the git hooks
and the commit and merge guards read), merged on top in manifest order. It lets a
branch's code run on this install's server before its PR merges. Nothing is
pushed or merged anywhere. It is a THROW-AWAY integration branch in the sense
of gitworkflows(7) ("You must never base any work on such a branch"; git.git's
own `seen` is rebuilt the same way): every rebuild starts again from origin/main,
and nothing is ever committed on it by hand.

Run it through ``scripts/deploy_candidates``, never directly: the entry holds
update.lock (rebuild, drop and add) and the deploy marker (rebuild and drop, which
move the checkout), and passes in the one definition of the dirty-tree excuse
list.

Commands:
  add <branch> --owner <who> [--pr N]
        Put a local branch in the manifest, pinned at its current head: `live`
        runs THAT commit, never a later one, until the branch is added again.
        There is no approval step (owner ruling, 2026-10-01: running an
        unmerged branch on this install's own server needs none). Refuses
        unless the install is READY and every per-candidate check passes
        (below). Prints the next step; it does not rebuild.
  drop <branch> [--no-rebuild]
        Take a candidate out of the manifest. On `live` it then removes the
        candidate from `live`: SUBTRACT-ONLY and OFFLINE, on the base `live` is
        already on, with the heads already live. No fetch and no GitHub,
        so it works during an outage and from a plain shell
        (`env -i`). It can only remove: anything that then conflicts, or that
        shares the dropped candidate's unmerged commits, goes out too, named.
        The repair path for a candidate whose hook blocks every session.
  rebuild
        fetch origin main (a failed fetch refuses: nothing moves); check the
        install is READY; retire candidates whose PR is proven in the fetched
        main; re-derive every per-candidate check; merge the rest off-tree
        (`merge-tree --write-tree`, then `commit-tree`, always a two-parent merge
        whose final paragraph carries the `Deploy-rebuild:` trailer); move the
        checkout once, and only when something changed; sync the git hooks;
        fast-forward local main. It does NOT restart the server: it prints the
        next step.
  list  The manifest as written (checked to be this repository's). No network.
  status
        READINESS, and per candidate: whether it is in `live` now, what the next
        rebuild would do with it (included, excluded and why, retired, or
        unknown), and its PR's state (stale after 7 days without an update).
        Writes only unreachable objects.

The one rule (deploy_candidates_gate.py): a decision about a candidate is never
recorded at `add` and trusted later. Every rebuild re-derives, for every
candidate: the branch still points at the head it was added at; admission; its
PR (if any) is OPEN against main with exactly that head. A
known negative EXCLUDES the candidate, by name. An UNKNOWN (an unreadable PR
or a failed fetch) REFUSES the whole command: nothing moves.

ONE CANDIDATE PER COMMIT: two candidates may not share an unmerged commit (git
cannot say which of them owns it, so whether its code is live would have no
answer once one of them leaves). A stack goes in as its TOP branch. `add`
refuses a branch that shares one with a listed candidate; a rebuild excludes
every candidate in such a pair (a hand-edited manifest, or a base that moved
backwards), by name. A merged candidate in such a pair is not retired: it
stays listed and excluded with the other until one of them is dropped, since
retiring it would leave the other to carry its commits live alone.

READINESS (add and rebuild): the commits in deploy_candidates_gate.REQUIRED_MERGED
and PR C's file (PR_C_MARKER) are in the server's base (merge-base of the
commit it booted from and origin/main), and every git hook and helper
sync-hooks.sh installs is byte-identical to its source at HEAD, in the directory
git runs hooks from. PR C (the wipers rebuild `live`) has not merged, so every
install refuses and the engine ships INERT.

ADMISSION (v1): a candidate whose diff against its merge base with origin/main
changes a migration or the boot-time schema, the host guardian's own files,
the Claude Code pin, what drives the host on a timer, a git or Claude Code hook,
the scripts that keep the wipers off `live` (and what they source first), or this
engine, never goes live before it merges; nor does a branch that carries a
`Deploy-rebuild:` commit (it was cut from `live`).

The reflog of `live` is kept through `git gc` and `git reflog expire --all`
(gc.refs/heads/live.reflogExpire[Unreachable] = never). It is NOT kept through
an explicit `git reflog expire --expire=now`, which overrides the configuration
(MEASURED, git 2.43).

Exit codes: 0 done, 1 refused or failed (the message says which; a refusal
changes nothing), 2 usage. The entry adds 200: update.lock still held after the
wait.
"""

from __future__ import annotations

import os
import sys

# The engine runs from the checkout it judges, and on `live` that checkout holds
# candidate code. `python3 scripts/deploy_candidates.py` puts scripts/ on
# sys.path, and ANY entry there is consulted for every module name nothing else
# answers: a candidate adding scripts/argparse.py shadows a module that exists,
# and scripts/msvcrt.py or scripts/nt.py answers a lookup the standard library
# makes on every run for a module that does not exist on Linux. Either would
# run inside every command, `drop` included, the repair path. So scripts/ is
# taken off sys.path entirely (compared through realpath: CPython resolves a
# symlinked script directory in sys.path[0] but not in __file__), before
# anything else is imported, and the engine's four sibling modules are loaded by
# a finder that answers only their names, from this directory. (os and sys are
# loaded by the interpreter before any script runs, so nothing shadows them.)
# The shell entry also sets PYTHONSAFEPATH, which never adds scripts/ at all.
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.curdir) != _SCRIPT_DIR]

import importlib.abc  # noqa: E402
import importlib.util  # noqa: E402

_SIBLINGS = frozenset(
    {
        "deploy_candidates_core",
        "deploy_candidates_gate",
        "deploy_candidates_manifest",
        "deploy_candidates_plan",
    }
)


class _SiblingFinder(importlib.abc.MetaPathFinder):
    """Answers exactly the engine's sibling module names, from its directory."""

    def find_spec(self, name, path=None, target=None):
        if name not in _SIBLINGS:
            return None
        return importlib.util.spec_from_file_location(name, os.path.join(_SCRIPT_DIR, name + ".py"))


if not any(isinstance(f, _SiblingFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _SiblingFinder())

import argparse  # noqa: E402
import contextlib  # noqa: E402
import datetime  # noqa: E402
import fcntl  # noqa: E402
import json  # noqa: E402
from collections.abc import Callable, Mapping  # noqa: E402
from pathlib import Path  # noqa: E402

import deploy_candidates_core as core  # noqa: E402
import deploy_candidates_gate as gate  # noqa: E402
import deploy_candidates_manifest as manifest  # noqa: E402
import deploy_candidates_plan as plan  # noqa: E402
from deploy_candidates_core import (  # noqa: E402
    BASE_BRANCH,
    BASE_REF,
    LIVE_BRANCH,
    LIVE_REF,
    STALE_DAYS,
    GhRunner,
    Refusal,
    Repo,
    Unknown,
    out,
)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Engine(Repo):
    def __init__(self, root, env, gh=None, serving=None):
        super().__init__(root, env, gh=gh, serving=serving)
        self.store = manifest.ManifestStore(self.home, self.common_dir)

    # ── locks and preflight ──────────────────────────────────────────────
    def require_update_lock(self) -> None:
        """Every manifest mutation and every checkout move runs under update.lock
        held EXCLUSIVE by the entry: the fd it passes must be that file, and this
        run must be able to hold it exclusively (it already does, through the
        same open file)."""
        fd_text = self.env.get("DEPLOY_CANDIDATES_LOCK_FD", "")
        hint = "run scripts/deploy_candidates, which takes update.lock exclusive for this command"
        if not fd_text.isdigit():
            raise Refusal(f"this command needs {self.lock_path} (update.lock): {hint}.")
        fd = int(fd_text)
        try:
            st, lst = os.fstat(fd), os.stat(self.lock_path)
        except OSError as exc:
            raise Refusal(f"cannot check the update.lock fd {fd}: {exc}; {hint}.") from exc
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise Refusal(f"fd {fd} is not {self.lock_path} (update.lock); {hint}.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Refusal(f"update.lock is held by another process; {hint}.") from exc

    def _move_preflight(self, need_manifest: bool) -> tuple[dict | None, str]:
        """Every refusal before a checkout move, in order. Returns the manifest
        and the checkout's branch."""
        self.require_main_checkout()
        data = self.store.load()
        if need_manifest and data is None:
            raise Refusal(
                f"no deploy manifest ({self.store.path}): nothing to rebuild. Add a candidate first."
            )
        reasons = self._move_refusals()
        if reasons:
            raise Refusal(reasons[0])
        return data, self.current_branch()

    def _move_refusals(self) -> list[str]:
        """Every reason the checkout must not move now, in the order rebuild and
        drop check them, WITHOUT raising: rebuild and drop refuse on the first,
        and status reports them all, so status never predicts a rebuild that
        would refuse."""
        reasons: list[str] = []
        if self.update_state.exists():
            try:
                phase = json.loads(self.update_state.read_text()).get("phase")
            except (OSError, ValueError, AttributeError):
                phase = None
            if phase != "done":
                reasons.append(
                    f"{self.update_state} records an unfinished update.sh run; finish it with scripts/update.sh --post-merge."
                )
        branch = self.current_branch()
        if branch not in (LIVE_BRANCH, BASE_BRANCH):
            reasons.append(
                f"{self.root} is on {branch or 'a detached HEAD'}; `live` is built from main or `live`."
            )
        elsewhere = self.live_checked_out_elsewhere()
        if elsewhere:
            reasons.append(
                f"`live` is checked out in another worktree ({elsewhere}); moving it would change the files under "
                "that worktree. Switch that worktree to another branch first."
            )
        dirty = self.dirty_lines()
        if dirty:
            reasons.append(
                f"{self.root} has uncommitted tracked changes; a rebuild never stashes or discards them. "
                "Commit them on a branch cut from origin/main (never from `live`: `add` refuses a "
                "branch carrying rebuild merges), then `add` it, or set them aside, and retry:\n"
                + "\n".join("  " + ln for ln in dirty)
            )
        return reasons

    def live_set(self, base: str, data: dict | None) -> tuple[str | None, list[tuple[str, str]]]:
        """What `live` runs now: (the commit its rebuild merges sit on, [(branch,
        head)]), or (None, []) when there is no `live`. A candidate counts when a
        rebuild merge names it, OR when its pinned head is reachable from `live`
        without being in that base (a hand-edited manifest can list one an
        earlier candidate carries: no merge names it, yet its code is live).
        Listed candidates
        come in manifest order; a merged branch the manifest no longer lists
        follows, since its code is live too. drop and status both read this."""
        live_base, merged = self.live_chain(base)
        if live_base is None:
            return None, []
        tip = self.resolve(LIVE_REF)
        named = dict(merged)
        result: list[tuple[str, str]] = []
        for c in (data or {}).get("candidates", []):
            b, h = c["branch"], c["verified_head"]
            if b in named:
                result.append((b, named[b]))
            elif (
                self.resolve(h) and self.is_ancestor(h, tip) and not self.is_ancestor(h, live_base)
            ):
                result.append((b, h))
        listed = {b for b, _ in result}
        result += [(b, h) for b, h in merged if b not in listed]
        return live_base, result

    def _refuse_blockers(self, tip: str, what: str) -> None:
        blockers = plan.move_blockers(self, tip)
        if blockers:
            raise Refusal(
                f"the checkout cannot move to {tip[:12]}: files are in the way. Nothing changed ({what}). "
                "Move these aside, then retry:\n" + "\n".join("  " + b for b in blockers)
            )

    @staticmethod
    def _print_plan(p: core.Plan, retired: list[str] | None = None) -> None:
        out(f"  base: origin/main {p.base[:12]}")
        for b, head in p.merged:
            out(f"  live:     {b} ({head[:12]})")
        for b in p.contained:
            out(f"  contained: {b} — already in origin/main; nothing to merge")
        for b, reason in p.excluded.items():
            out(f"  EXCLUDED: {b} — {reason}")
        for b in retired or []:
            out(f"  retiring: {b}")

    # ── commands ─────────────────────────────────────────────────────────
    def cmd_list(self) -> int:
        data = self.store.load()
        if data is None:
            out(f"No deploy manifest ({self.store.path}): nothing is meant to be live.")
            return 0
        if not data["candidates"]:
            out("The deploy manifest lists no candidates.")
            return 0
        for i, c in enumerate(data["candidates"], 1):
            pr = f"PR #{c['pr']}" if c["pr"] is not None else "no PR"
            out(
                f"{i}. {c['branch']}  {pr}  owner {c['owner_session']}  added {c['added_at']}  "
                f"pinned at {c['verified_head'][:12]}"
            )
        return 0

    def cmd_add(self, branch: str, owner: str, pr: int | None) -> int:
        self.require_update_lock()
        if not core.valid_candidate_name(branch):
            raise Refusal(
                f"{branch!r} cannot be a candidate (not a branch name, or `live`/`main`/under them)."
            )
        owner = owner.strip()
        if not owner:
            raise Refusal("--owner is empty: name the session or person who owns this candidate.")
        if pr is not None and pr <= 0:
            raise Refusal(f"--pr {pr} is not a PR number.")
        self.store.load()  # a malformed or foreign manifest refuses before anything else
        base = self.resolve(BASE_REF)
        if not base:
            raise Refusal(f"{BASE_REF} does not resolve; fetch origin first.")
        fails = gate.readiness_failures(self, base)
        if fails:
            raise Refusal(
                "this install is not ready to run candidates on `live`:\n  - "
                + "\n  - ".join(fails)
            )
        head = self.resolve(f"refs/heads/{branch}")
        if not head:
            raise Refusal(f"there is no local branch {branch}.")
        stamp = core.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        cand = {
            "branch": branch,
            "pr": pr,
            "owner_session": owner,
            "added_at": stamp,
            "verified_head": head,
        }
        why = gate.gate_failure(self, base, cand)
        if why:
            raise Refusal(f"{branch} cannot go live: {why}")
        listed = {
            c["branch"]: c["verified_head"]
            for c in (self.store.load() or {}).get("candidates", [])
            if c["branch"] != branch
        }
        sharing = plan.shared_with(self, base, {branch: head, **listed}).get(branch, [])
        if sharing:
            # The remedy depends on how the two relate: one stacked on the other
            # (one head contains the other), or both cut from a third branch.
            carriers = [b for b in sharing if self.is_ancestor(head, listed[b])]
            stacked_on = [b for b in sharing if self.is_ancestor(listed[b], head)]
            if carriers:
                remedy = (
                    f"{', '.join(carriers)}, already listed, carries {branch}'s commits: there is "
                    f"nothing to add (to list {branch} instead, drop {', '.join(carriers)} first)."
                )
            elif len(stacked_on) == len(sharing):
                remedy = f"A stack goes in as its top branch: drop {', '.join(sharing)}, then add {branch}."
            else:
                remedy = (
                    f"They were cut from one branch that is not in origin/main. If that branch has "
                    f"merged, rebase {branch} onto a freshly fetched origin/main (`git fetch origin "
                    "main`); otherwise combine them on one branch."
                )
            raise Refusal(
                f"{branch} shares unmerged commits with {', '.join(sharing)}: two candidates may "
                f"not (git cannot say whose code a shared commit is). {remedy}"
            )
        result = {}

        def change(data: dict | None) -> dict:
            data = data or {
                "version": manifest.MANIFEST_VERSION,
                "repo": self.common_dir(),
                "candidates": [],
            }
            for c in data["candidates"]:
                if c["branch"] == branch:
                    c.update({k: v for k, v in cand.items() if k != "added_at"})
                    result["kind"] = "re-verified"
                    return data
            data["candidates"].append(cand)
            result["kind"] = "added"
            return data

        self.store.update(change)
        out(f"{branch} {result['kind']} at {head[:12]} ({self.store.path}).")
        out("Next: scripts/deploy_candidates rebuild")
        return 0

    def cmd_drop(self, branch: str, no_rebuild: bool) -> int:
        self.require_update_lock()
        # No manifest: nothing to drop, and the manifest is not created.
        if not self.store.exists():
            raise Refusal(
                f"{branch} is not a candidate: there is no deploy manifest ({self.store.path})."
            )
        data = self.store.load()
        if not any(c["branch"] == branch for c in data["candidates"]):
            raise Refusal(f"{branch} is not a candidate in {self.store.path}.")

        def remove(d: dict | None) -> dict | None:
            if d is None:
                return None
            keep = [c for c in d["candidates"] if c["branch"] != branch]
            return None if len(keep) == len(d["candidates"]) else {**d, "candidates": keep}

        if self.current_branch() != LIVE_BRANCH or no_rebuild:
            after = self.store.update(remove)
            out(f"{branch} dropped from {self.store.path}.")
            core.after_move(
                f"{branch} WAS dropped from the manifest",
                "checking which candidates share its commits",
                lambda: self._warn_shared(branch, data, after),
            )
            out(
                "The checkout is not rebuilt"
                + (" (--no-rebuild)." if no_rebuild else " (it is not on `live`).")
            )
            return 0
        # On `live`: subtract-only, offline. Every refusal comes before the manifest
        # changes, so a drop that cannot take the code out never reports it gone.
        self._move_preflight(need_manifest=True)
        walk_base = self.resolve(BASE_REF)
        if not walk_base:
            raise Refusal(f"{BASE_REF} does not resolve.")
        self.refuse_foreign(walk_base)
        live_base, live_now = self.live_set(walk_base, data)
        if live_base is None:
            raise Refusal("`live` does not exist; nothing to rebuild.")
        # Subtract-only by construction: a candidate still live that shares the
        # dropped one's unmerged commits would carry them straight back, so it
        # goes out with it, by name. A rebuild never leaves such a pair live, but
        # live_set also counts a listed candidate whose code an earlier one
        # carries (a hand-edited manifest), and drop must not trust how `live`
        # was built.
        remaining = [(b, h) for b, h in live_now if b != branch]
        dropped_head = dict(live_now).get(branch)
        sticky: dict[str, str] = {}
        if dropped_head:
            for b in plan.shared_with(
                self, live_base, {branch: dropped_head, **dict(remaining)}
            ).get(branch, []):
                sticky[b] = f"shares dropped {branch}'s unmerged commits; drop it too"
        rebuild_id = core.now().strftime("%Y%m%dT%H%M%SZ")
        p = plan.build_plan(self, live_base, remaining, rebuild_id, sticky=sticky)
        out(
            f"deploy_candidates drop {branch} (on the base `live` is on, {live_base[:12]}; nothing fetched)"
        )
        self._print_plan(p)
        self._refuse_blockers(p.tip, "the manifest still lists " + branch)
        after = self.store.update(remove)
        out(f"  {branch} dropped from {self.store.path}.")
        try:
            plan.move_checkout(self, p, LIVE_BRANCH)
        except Refusal as exc:
            raise Refusal(
                f"{exc}\nThe manifest change WAS saved ({branch} is no longer a candidate), but `live` still "
                "runs it: fix the above, then run scripts/deploy_candidates rebuild."
            ) from exc
        core.after_move(
            f"{branch} WAS dropped from the manifest and from `live`",
            "checking which candidates share its commits",
            lambda: self._warn_shared(branch, data, after),
        )
        gone = [b for b in p.excluded if b != branch]
        if gone:
            out(
                "  NOTE: "
                + ", ".join(gone)
                + " left `live` with it and come back at the next rebuild "
                "unless their own reason still holds: drop them too to keep that code out."
            )
        out(
            "  The server was not restarted: it runs what it loaded until it does. Next step, when no"
        )
        out(
            "  validation holds the lock: scripts/deploy_code_only.sh restart (launch it detached)."
        )
        return 0

    def _warn_shared(self, branch: str, before: dict, after: dict | None) -> None:
        """Name the candidates that share the dropped one's unmerged commits: they
        were kept out of `live` while both were listed, and the next rebuild puts
        them live WITH those commits unless they go too."""
        base = self.resolve(BASE_REF)
        dropped = next((c for c in before["candidates"] if c["branch"] == branch), None)
        if not (base and dropped and after):
            return
        heads = {branch: dropped["verified_head"]}
        heads.update({c["branch"]: c["verified_head"] for c in after["candidates"]})
        for b in plan.shared_with(self, base, heads).get(branch, []):
            out(
                f"  WARNING: {b} shares {branch}'s unmerged commits; the next rebuild puts it live "
                "WITH them (unless its PR has merged: then that rebuild retires it). Drop it too to "
                "keep that code out."
            )

    def cmd_rebuild(self) -> int:
        self.require_update_lock()
        data, branch = self._move_preflight(need_manifest=True)
        rebuild_id = core.now().strftime("%Y%m%dT%H%M%SZ")
        out(f"deploy_candidates rebuild {rebuild_id}")
        ok, why = self.fetch(f"+refs/heads/{BASE_BRANCH}:{BASE_REF}")
        if not ok:
            raise Unknown(
                f"{why}; a rebuild never builds on a main it could not fetch. Nothing changed."
            )
        base = self.resolve(BASE_REF)
        if not base:
            raise Refusal(f"{BASE_REF} does not resolve; nothing changed.")
        self.refuse_foreign(base)
        fails = gate.readiness_failures(self, base)
        if fails:
            raise Refusal(
                "this install is not ready to run candidates on `live`; nothing changed:\n  - "
                + "\n  - ".join(fails)
            )
        retire: list[dict] = []
        sticky: dict[str, str] = {}
        for c in data["candidates"]:
            done, why = gate.retirement(self, base, c)
            if done:
                retire.append(c)
                continue
            if why:
                sticky[c["branch"]] = f"merged upstream, not retired: {why}; drop it"
                continue
            reason = gate.gate_failure(self, base, c)
            if reason:
                sticky[c["branch"]] = reason
        held = self._retirements_held(base, data["candidates"], {c["branch"] for c in retire})
        sticky.update(held)
        retire = [c for c in retire if c["branch"] not in held]
        gone = {c["branch"] for c in retire}
        cands = [
            (c["branch"], c["verified_head"]) for c in data["candidates"] if c["branch"] not in gone
        ]
        p = plan.build_plan(self, base, cands, rebuild_id, sticky=sticky)
        self._print_plan(p, sorted(gone))
        self._refuse_blockers(p.tip, "nothing was retired")
        moved = plan.move_checkout(self, p, branch)
        state = f"`live` is at {moved.at[:12]}"
        if moved.files:
            core.after_move(state, "syncing the git hooks", lambda: plan.sync_git_hooks(self))
        # Past the commit point (`live` has moved): every step below is a
        # WARNING on failure, never a refusal claiming nothing changed.
        if retire:
            core.after_move(state, "saving the retirements", lambda: self._save_retirements(retire))
        core.after_move(
            state, "fast-forwarding local main", lambda: plan.fast_forward_main(self, base)
        )
        out(
            "  The server was not restarted: it runs what it loaded until it does. Next step, when no"
        )
        out(
            "  validation holds the lock: scripts/deploy_code_only.sh restart (launch it detached)."
        )
        return 0

    def _retirements_held(
        self, base: str, candidates: list[dict], retiring: set[str]
    ) -> dict[str, str]:
        """A merged candidate is NOT retired while a listed candidate that stays
        shares its unmerged commits (a squash merge leaves them unmerged):
        retiring it would leave the other alone, and the next rebuild would put
        those commits live with it, reverted upstream or not. It stays listed
        and excluded, and the pair stays out until one is dropped. Only a
        hand-edited manifest gets here; `add` refuses sharers."""
        if not retiring:
            return {}
        shared = plan.shared_with(self, base, {c["branch"]: c["verified_head"] for c in candidates})
        held: dict[str, str] = {}
        for b in sorted(retiring):
            partners = [p for p in shared.get(b, []) if p not in retiring]
            if partners:
                held[b] = (
                    f"merged upstream, not retired: {', '.join(partners)} shares its unmerged "
                    f"commits; drop {b} to let {', '.join(partners)} go live with them, or drop "
                    f"{', '.join(partners)}"
                )
        return held

    def _save_retirements(self, retire: list[dict]) -> None:
        """Retirements are saved only once `live` no longer holds them, and only
        for an entry that is still the one decided on (same branch, PR and
        head), so a concurrent re-add is never undone."""
        if retire:
            decided = {(c["branch"], c["pr"], c["verified_head"]) for c in retire}

            def drop_retired(d: dict | None) -> dict | None:
                if d is None:
                    return None
                keep = [
                    c
                    for c in d["candidates"]
                    if (c["branch"], c["pr"], c["verified_head"]) not in decided
                ]
                return None if len(keep) == len(d["candidates"]) else {**d, "candidates": keep}

            self.store.update(drop_retired)
            for c in retire:
                out(f"  retired: {c['branch']} — PR #{c['pr']} is in origin/main")
                core.after_move(
                    f"{c['branch']} was retired",
                    f"checking what PR #{c['pr']} merged",
                    lambda c=c: self._retirement_notes(c),
                )

    def _retirement_notes(self, c: dict) -> None:
        """What a retired candidate's PR merged, against the commit `live` ran.
        Called past the commit point (through core.after_move): a git error here
        is a warning, never a refusal."""
        # The pinned commit may have been force-pushed off the PR before it
        # merged: then the code that ran on `live` is not what main holds.
        merged_head = gate.pr_data(self, c["pr"]).get("headRefOid")
        if merged_head == c["verified_head"]:
            pass
        elif not isinstance(merged_head, str) or not self.resolve(merged_head):
            # Rewritten elsewhere and never fetched here: say so rather than
            # stay silent in the one case this check exists for.
            out(
                f"    cannot tell whether PR #{c['pr']} merged the pinned commit "
                f"{c['verified_head'][:12]}: its merged head is not in this repository"
            )
        elif not self.is_ancestor(c["verified_head"], merged_head):
            out(
                f"    WARNING: PR #{c['pr']} merged without the pinned commit "
                f"{c['verified_head'][:12]} (its head was rewritten); what ran on "
                "`live` is not what main holds"
            )
        # `live` only ever ran the pinned head; commits the branch gained after
        # it were never live here. Say so, since retirement takes the branch off
        # the manifest.
        tip = self.resolve(f"refs/heads/{c['branch']}")
        if tip and tip != c["verified_head"]:
            n = len(self.rev_list(tip, "--not", c["verified_head"]))
            if n:
                out(
                    f"    {c['branch']} has {n} commit(s) after its pinned head that were "
                    "never live here; if the PR did not carry them into main, add them again"
                )

    def cmd_status(self) -> int:
        data = self.store.load()
        branch = self.current_branch()
        head = self.resolve("HEAD")
        base = self.resolve(BASE_REF)
        out(
            f"checkout: {branch or 'detached'} at {head[:12] if head else '?'}; "
            f"origin/main (last fetched) {base[:12] if base else '?'}"
        )
        if data is None:
            out(f"No deploy manifest ({self.store.path}): nothing is meant to be live.")
            return 0
        if not base:
            out("origin/main does not resolve: nothing can be judged.")
            return 0
        try:
            fails = gate.readiness_failures(self, base)
        except Refusal as exc:  # Unknown included: report, never exit on it
            fails = [f"cannot check: {exc}"]
        out("ready: yes" if not fails else "ready: NO\n  - " + "\n  - ".join(fails))
        # What the next rebuild would refuse on, before it changed anything: the
        # same checks rebuild and drop run, so status never predicts a rebuild
        # that would refuse.
        try:
            refusals = self._move_refusals()
            foreign = self.foreign_commits(base)
        except Refusal as exc:
            refusals, foreign = [f"cannot check: {exc}"], []
        if foreign:
            refusals.append(
                "`live` holds commits that are neither a rebuild merge nor a candidate's: "
                + ", ".join(c[:12] for c in foreign)
            )
        live_merged: dict[str, str] = {}
        if self.resolve(LIVE_REF):
            try:
                _, now_live = self.live_set(base, data)
                live_merged = dict(now_live)
            except Refusal as exc:
                out(f"  WARNING: cannot read what `live` holds: {exc}")
            if branch == LIVE_BRANCH and head != self.resolve(LIVE_REF):
                out("  WARNING: HEAD is not the tip of `live`.")
        if not data["candidates"]:
            if refusals:
                out(
                    "next rebuild would REFUSE (nothing would change):\n  - "
                    + "\n  - ".join(refusals)
                )
            out("The deploy manifest lists no candidates.")
            return 0
        verdict: dict[str, str] = {}
        sticky: dict[str, str] = {}
        retiring: set[str] = set()
        for c in data["candidates"]:
            b = c["branch"]
            try:
                done, why = gate.retirement(self, base, c)
                if done:
                    retiring.add(b)
                    continue
                reason = (
                    f"merged upstream, not retired: {why}"
                    if why
                    else gate.gate_failure(self, base, c)
                )
            except Unknown as exc:
                verdict[b] = f"UNKNOWN ({exc}): the next rebuild refuses until this is readable"
                continue
            except Refusal as exc:  # status reports; it never exits on one candidate
                verdict[b] = f"cannot check ({exc}): the next rebuild refuses on this"
                continue
            if reason:
                sticky[b] = reason
        # The same hold rebuild applies, so status never predicts a retirement
        # the rebuild would withhold.
        try:
            held = self._retirements_held(base, data["candidates"], retiring)
        except Refusal as exc:  # Unknown included
            held = {}
            refusals.append(f"cannot check which retirements would be held: {exc}")
        sticky.update(held)
        for b in retiring - set(held):
            verdict[b] = "retires at the next rebuild (its PR is in origin/main)"
        cands = [
            (c["branch"], c["verified_head"])
            for c in data["candidates"]
            if c["branch"] not in verdict
        ]
        try:
            p = plan.build_plan(self, base, cands, "status-dry-run", sticky=sticky)
            blockers = plan.move_blockers(self, p.tip)
        except Refusal as exc:
            p = core.Plan(base=base, tip=base)
            blockers = []
            refusals.append(f"cannot plan the next rebuild: {exc}")
        if blockers:
            refusals.append("files are in the way of the move: " + ", ".join(blockers))
        if refusals:
            out("next rebuild would REFUSE (nothing would change):\n  - " + "\n  - ".join(refusals))
        for c in data["candidates"]:
            b = c["branch"]
            out(
                f"{b}  {'PR #' + str(c['pr']) if c['pr'] is not None else 'no PR'}  owner {c['owner_session']}"
            )
            out(f"    live at {live_merged[b][:12]}" if b in live_merged else "    not in `live`")
            if b in verdict:
                out(f"    next rebuild: {verdict[b]}")
            elif b in p.excluded:
                out(f"    next rebuild: EXCLUDED — {p.excluded[b]}")
            elif b in p.contained:
                out("    next rebuild: contained (already in origin/main)")
            else:
                out("    next rebuild: included")
            cur = self.resolve(f"refs/heads/{b}")
            if cur and self.adds_nothing(base, cur):
                out("    adds nothing beyond origin/main (its change is already there): drop it?")
            if c["pr"] is not None:
                st, why = self.pr_state(c["pr"])
                if st is not None and st["state"] == "OPEN":
                    updated = core.parse_time(str(st.get("updatedAt") or ""))
                    if updated and core.now() - updated > datetime.timedelta(days=STALE_DAYS):
                        out(f"    PR OPEN, stale (no update in {STALE_DAYS} days): keep or drop?")
        return 0

    def adds_nothing(self, base: str, head: str) -> bool:
        """Would merging ``head`` onto ``base`` leave base's files unchanged? A
        HINT for `status` only (a candidate with no PR never retires on its own);
        retirement itself is proven from the PR's merge commit."""
        p = self.git(
            f"--attr-source={base}",
            "merge-tree",
            "--write-tree",
            "--no-messages",
            base,
            head,
            check=False,
        )
        return p.returncode == 0 and p.stdout.splitlines()[0].strip() == self.tree(base)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy_candidates", description="The engine of `live`, the local integration branch."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("branch")
    a.add_argument("--owner", required=True)
    a.add_argument("--pr", type=int)
    d = sub.add_parser("drop")
    d.add_argument("branch")
    d.add_argument("--no-rebuild", action="store_true")
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("rebuild")
    return p


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    gh: GhRunner | None = None,
    serving: Callable[[Path], tuple[str | None, str]] | None = None,
    root: Path | None = None,
) -> int:
    env = dict(os.environ if env is None else env)
    # git output is decoded with surrogateescape, so a non-UTF-8 path can reach a
    # message: print it escaped rather than crash on it.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="backslashreplace")
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else 2
    # The checkout this script lives in. `root` is for tests (scratch
    # repositories); nothing in the environment can point the engine elsewhere.
    engine = Engine(
        Path(root) if root else Path(__file__).resolve().parents[1], env, gh=gh, serving=serving
    )
    try:
        engine.place(args.cmd)
        if args.cmd == "list":
            return engine.cmd_list()
        if args.cmd == "status":
            return engine.cmd_status()
        if args.cmd == "add":
            return engine.cmd_add(args.branch, args.owner, args.pr)
        if args.cmd == "drop":
            return engine.cmd_drop(args.branch, args.no_rebuild)
        if args.cmd == "rebuild":
            return engine.cmd_rebuild()
    except Unknown as exc:
        _err(
            f"ERROR: {exc}\nThis could not be established, so nothing changed; retry once it can be."
        )
        return 1
    except Refusal as exc:
        _err(f"ERROR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - an unexpected failure still exits 1, with the facts
        try:
            tip = engine.resolve(LIVE_REF)
        except Exception:  # noqa: BLE001
            tip = None
        _err(
            f"ERROR: unexpected {type(exc).__name__}: {exc}\n`live` is at "
            f"{tip[:12] if tip else 'an unreadable or missing ref'}; the manifest is "
            f"{engine.store.path}. Check both before retrying."
        )
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
