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
        (`env -i`). It can only remove: anything that then conflicts, or carries
        the dropped commits, goes out too, named. The repair path for a
        candidate whose hook blocks every session.
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
  adopt <new-branch> --owner <who>
        Snapshot the checkout's uncommitted TRACKED edits as one commit on a new
        branch, without touching the checkout. On `live` the edits are carried
        onto the base `live` sits on, so the branch never carries a rebuild
        merge. Then report, file by file, whether each snapshotted file equals
        origin/main, an open PR's head, or neither.

The one rule (deploy_candidates_gate.py): a decision about a candidate is never
recorded at `add` and trusted later. Every rebuild re-derives, for every
candidate: the branch still points at the head it was added at; admission; its
PR (if any) is OPEN against main with exactly that head. A
known negative EXCLUDES the candidate, by name. An UNKNOWN (an unreadable PR
or a failed fetch) REFUSES the whole command: nothing moves.

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
    IDENTITY,
    LIVE_BRANCH,
    LIVE_REF,
    STALE_DAYS,
    GhRunner,
    Refusal,
    Repo,
    Unknown,
    out,
)

carries = plan.carries  # re-exported for tests and readers
ADOPT_REF_PREFIX = "refs/deploy-candidates/adopt/"


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
        if self.update_state.exists():
            try:
                phase = json.loads(self.update_state.read_text()).get("phase")
            except (OSError, ValueError, AttributeError):
                phase = None
            if phase != "done":
                raise Refusal(
                    f"{self.update_state} records an unfinished update.sh run; finish it with scripts/update.sh --post-merge."
                )
        branch = self.current_branch()
        if branch not in (LIVE_BRANCH, BASE_BRANCH):
            raise Refusal(
                f"{self.root} is on {branch or 'a detached HEAD'}; `live` is built from main or `live`."
            )
        elsewhere = self.live_checked_out_elsewhere()
        if elsewhere:
            raise Refusal(
                f"`live` is checked out in another worktree ({elsewhere}); moving it would change the files under "
                "that worktree. Switch that worktree to another branch first."
            )
        dirty = self.dirty_lines()
        if dirty:
            raise Refusal(
                f"{self.root} has uncommitted tracked changes; a rebuild never stashes or discards them. "
                "Turn them into a candidate with `scripts/deploy_candidates adopt <branch> --owner <who>`:\n"
                + "\n".join("  " + ln for ln in dirty)
            )
        return data, branch

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
            out(
                f"  contained: {b} — already in origin/main or an earlier candidate; nothing to merge"
            )
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
            self._warn_stacked(branch, data, after)
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
        live_base, merged = self.live_chain(walk_base)
        if live_base is None:
            raise Refusal("`live` does not exist; nothing to rebuild.")
        dropped_head = dict(merged).get(branch)
        remaining = [(b, h) for b, h in merged if b != branch]
        rebuild_id = core.now().strftime("%Y%m%dT%H%M%SZ")
        p = plan.build_plan(
            self,
            live_base,
            remaining,
            rebuild_id,
            removed={branch: dropped_head} if dropped_head else None,
        )
        out(
            f"deploy_candidates drop {branch} (on the base `live` is on, {live_base[:12]}; nothing fetched)"
        )
        self._print_plan(p)
        self._refuse_blockers(p.tip, "the manifest still lists " + branch)
        self.store.update(remove)
        out(f"  {branch} dropped from {self.store.path}.")
        try:
            plan.move_checkout(self, p, LIVE_BRANCH)
        except Refusal as exc:
            raise Refusal(
                f"{exc}\nThe manifest change WAS saved ({branch} is no longer a candidate), but `live` still "
                "runs it: fix the above, then run scripts/deploy_candidates rebuild."
            ) from exc
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

    def _warn_stacked(self, branch: str, before: dict, after: dict | None) -> None:
        """Name the candidates that carry the dropped one's unmerged commits: the
        next rebuild keeps them, and their code with it, unless they go too."""
        base = self.resolve(BASE_REF)
        dropped = next((c for c in before["candidates"] if c["branch"] == branch), None)
        if not (base and dropped and after and self.resolve(dropped["verified_head"])):
            return
        others = {
            c["branch"]: c["verified_head"]
            for c in after["candidates"]
            if self.resolve(c["verified_head"])
        }
        try:
            stacked = plan.stacked_on(self, base, dropped["verified_head"], others)
        except Refusal as exc:
            out(f"  NOTE: could not check which candidates carry {branch}'s commits ({exc}).")
            return
        for b in stacked:
            out(
                f"  WARNING: {b} carries {branch}'s unmerged commits: drop it too to take that code out."
            )

    def cmd_rebuild(self) -> int:
        self.require_update_lock()
        data, branch = self._move_preflight(need_manifest=True)
        rebuild_id = core.now().strftime("%Y%m%dT%H%M%SZ")
        out(f"deploy_candidates rebuild {rebuild_id}")
        ok, why = self.fetch(f"refs/heads/{BASE_BRANCH}:{BASE_REF}")
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
        gone = {c["branch"] for c in retire}
        cands = [
            (c["branch"], c["verified_head"]) for c in data["candidates"] if c["branch"] not in gone
        ]
        p = plan.build_plan(self, base, cands, rebuild_id, sticky=sticky)
        self._print_plan(p, sorted(gone))
        self._refuse_blockers(p.tip, "nothing was retired")
        if plan.move_checkout(self, p, branch):
            plan.sync_git_hooks(self)
        # Retirements are saved only once `live` no longer holds them, and only
        # for an entry that is still the one decided on (same branch, PR and
        # head), so a concurrent re-add is never undone.
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
                # `live` only ever ran the pinned head; commits the branch
                # gained after it were never live here. Say so, since the
                # retirement takes the branch off the manifest.
                # The pinned commit may have been force-pushed off the PR before
                # it merged: then code that ran on `live` is not what main holds.
                merged_head = gate.pr_data(self, c["pr"]).get("headRefOid")
                if merged_head == c["verified_head"]:
                    pass
                elif not isinstance(merged_head, str) or not self.resolve(merged_head):
                    # Rewritten elsewhere and never fetched here: say so rather
                    # than stay silent in the one case this check exists for.
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
                tip = self.resolve(f"refs/heads/{c['branch']}")
                if tip and tip != c["verified_head"]:
                    n = len(self.rev_list(tip, "--not", c["verified_head"]))
                    if n:
                        out(
                            f"    {c['branch']} has {n} commit(s) after its pinned head that were "
                            "never live here; if the PR did not carry them into main, add them again"
                        )
        plan.fast_forward_main(self, base)
        out(
            "  The server was not restarted: it runs what it loaded until it does. Next step, when no"
        )
        out(
            "  validation holds the lock: scripts/deploy_code_only.sh restart (launch it detached)."
        )
        return 0

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
        except Unknown as exc:
            fails = [f"unknown: {exc}"]
        out("ready: yes" if not fails else "ready: NO\n  - " + "\n  - ".join(fails))
        live_merged: dict[str, str] = {}
        if self.resolve(LIVE_REF):
            _, chain = self.live_chain(base)
            live_merged = dict(chain)
            if branch == LIVE_BRANCH and head != self.resolve(LIVE_REF):
                out("  WARNING: HEAD is not the tip of `live`.")
        if not data["candidates"]:
            out("The deploy manifest lists no candidates.")
            return 0
        verdict: dict[str, str] = {}
        sticky: dict[str, str] = {}
        for c in data["candidates"]:
            b = c["branch"]
            try:
                done, why = gate.retirement(self, base, c)
                if done:
                    verdict[b] = "retires at the next rebuild (its PR is in origin/main)"
                    continue
                reason = (
                    f"merged upstream, not retired: {why}"
                    if why
                    else gate.gate_failure(self, base, c)
                )
            except Unknown as exc:
                verdict[b] = f"UNKNOWN ({exc}): the next rebuild refuses until this is readable"
                continue
            if reason:
                sticky[b] = reason
        cands = [
            (c["branch"], c["verified_head"])
            for c in data["candidates"]
            if c["branch"] not in verdict
        ]
        p = plan.build_plan(self, base, cands, "status-dry-run", sticky=sticky)
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
                out("    next rebuild: contained (already in origin/main or an earlier candidate)")
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
        p = self.git("merge-tree", "--write-tree", "--no-messages", base, head, check=False)
        return p.returncode == 0 and p.stdout.splitlines()[0].strip() == self.tree(base)

    def cmd_adopt(self, branch: str, owner: str) -> int:
        if not core.valid_candidate_name(branch):
            raise Refusal(f"{branch!r} cannot be the adopted branch's name.")
        owner = owner.strip()
        if not owner:
            raise Refusal("--owner is empty: name the session or person who owns these edits.")
        if self.resolve(f"refs/heads/{branch}"):
            raise Refusal(f"the branch {branch} already exists; choose a new name.")
        head = self.resolve("HEAD")
        if not head:
            raise Refusal("HEAD does not resolve.")
        dirty = self.dirty_lines()
        if not dirty:
            raise Refusal("no uncommitted tracked edits: nothing to adopt.")
        paths = [ln[3:] for ln in dirty]
        idx = (
            Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
            / f"deploy-candidates-adopt.{os.getpid()}.index"
        )
        try:
            env = {"GIT_INDEX_FILE": str(idx), "GIT_LITERAL_PATHSPECS": "1"}
            self.git("read-tree", head, extra_env=env)
            self.git("add", "-A", "--", *paths, extra_env=env)
            tree = self.git("write-tree", extra_env=env).stdout.strip()
        finally:
            idx.unlink(missing_ok=True)

        def msg(on: str) -> str:
            return (
                "adopt: uncommitted edits from the live checkout\n\n"
                f"A snapshot of {len(paths)} tracked file(s) edited in place on top of {on[:12]}.\n\n"
                f"Adopted-by: {owner}\n"
            )

        snapshot = self.git(
            "commit-tree", tree, "-p", head, input=msg(head), extra_env=IDENTITY
        ).stdout.strip()
        parent, commit = head, snapshot
        live_base = self._adopt_base(head)
        if live_base and live_base != head:
            # On `live`: carry only the edits (HEAD -> snapshot) onto the base `live`
            # sits on, so the new branch carries no rebuild merge (admission refuses
            # those) and none of the other candidates' code.
            p = self.git(
                "merge-tree",
                "--write-tree",
                "--no-messages",
                f"--merge-base={head}",
                live_base,
                snapshot,
                check=False,
            )
            if p.returncode == 1:
                raise Refusal(
                    "the edits touch lines a candidate on `live` changed, so they cannot be separated from "
                    "it: commit them to that candidate's branch instead. Nothing was written."
                )
            if p.returncode != 0:
                raise Refusal(f"git merge-tree could not carry the edits: {p.stderr.strip()}")
            parent = live_base
            commit = self.git(
                "commit-tree",
                p.stdout.splitlines()[0].strip(),
                "-p",
                live_base,
                input=msg(live_base),
                extra_env=IDENTITY,
            ).stdout.strip()
        # Create, never overwrite: an empty old value refuses an existing ref.
        self.git(
            "update-ref",
            "-m",
            f"deploy-candidates: adopt by {owner}",
            f"refs/heads/{branch}",
            commit,
            "",
        )
        out(
            f"adopted {len(paths)} file(s) as {branch} at {commit[:12]} (on top of {parent[:12]}); "
            "the checkout is untouched."
        )
        try:
            self._adopt_report(paths, snapshot)
        finally:
            self._adopt_cleanup()
        out("Next: decide which edits still need to go live (the report above), then")
        out(f"  scripts/deploy_candidates add {branch} --owner {owner}")
        return 0

    def _adopt_base(self, head: str) -> str | None:
        """The base `live` sits on, when HEAD carries rebuild merges; else None."""
        base = self.resolve(BASE_REF) or head
        live_base, merged = self.live_chain(base) if self.resolve(LIVE_REF) else (None, [])
        if not merged or self.resolve(LIVE_REF) != head:
            return None
        return live_base

    def _adopt_report(self, paths: list[str], snapshot: str) -> None:
        """Each snapshotted file compared with origin/main and every open PR's
        head. Its fetches land in refs/deploy-candidates/adopt/ (removed after),
        never origin/main's ref, so it needs no lock and races no rebuild."""
        notes = []
        ok, why = self.fetch(f"refs/heads/{BASE_BRANCH}:{self._adopt_ns()}main")
        base = self.resolve(f"{self._adopt_ns()}main") if ok else self.resolve(BASE_REF)
        if not ok:
            notes.append(f"{why}; compared with the last-fetched origin/main")
        prs: list[tuple[int, str]] = []
        rc, text, err = self._gh(
            ["pr", "list", "--state", "open", "--limit", "1000", "--json", "number,headRefOid"],
            str(self.root),
            self.env,
        )
        if rc != 0:
            notes.append(
                f"open PR heads unavailable ({' '.join((err or text).split())}): no file was compared with a PR"
            )
        else:
            try:
                rows = json.loads(text)
                prs = [(int(r["number"]), str(r["headRefOid"])) for r in rows]
                if len(rows) >= 1000:
                    notes.append(
                        "the open PR list hit its limit of 1000: some PRs were not compared"
                    )
            except (ValueError, KeyError, TypeError):
                notes.append("the open PR list is unreadable: no file was compared with a PR")
        available: list[tuple[int, str]] = []
        missing: list[str] = []
        for n, sha in prs:
            if not self.resolve(sha):
                self.fetch(f"refs/pull/{n}/head:{self._adopt_ns()}pr/{n}")
            if self.resolve(sha):
                available.append((n, sha))
            else:
                missing.append(f"#{n}")
        if missing:
            notes.append(
                f"the heads of {len(missing)} open PR(s) could not be fetched and were not compared: {' '.join(missing)}"
            )
        out("Per file (the snapshot compared with origin/main and each open PR's head):")
        width = max(len(p) for p in paths)
        for path in paths:
            mine = self.blob_at(snapshot, path)
            verdict = []
            if base is not None and self.blob_at(base, path) == mine:
                verdict.append("equals origin/main")
            for n, sha in available:
                if self.blob_at(sha, path) == mine:
                    verdict.append(f"equals PR #{n}'s head")
            out(
                f"  {path.ljust(width)}  {', '.join(verdict) if verdict else 'neither (not origin/main, not an open PR head)'}"
            )
        for note in notes:
            out(f"  NOTE: {note}.")

    def _adopt_ns(self) -> str:
        """This run's own ref namespace: two adopts at once (adopt takes no lock)
        never fetch into, or clean up, each other's refs."""
        return f"{ADOPT_REF_PREFIX}{os.getpid()}/"

    def _adopt_cleanup(self) -> None:
        """Remove this run's refs, and those of any adopt whose process is gone
        (killed mid-run), so their objects do not stay reachable for ever. A
        live process's refs are never touched."""
        mine = str(os.getpid())

        def gone(ns: str) -> bool:
            if ns == mine:
                return True
            if not ns.isdigit() or int(ns) <= 1:  # never probe 0 (own group) or 1 (init)
                return False
            try:
                os.kill(int(ns), 0)  # signal 0: an existence probe, nothing is sent
            except ProcessLookupError:
                return True
            except OSError:  # alive, owned by someone else
                return False
            return False

        refs = [
            r
            for r in self.git(
                "for-each-ref", "--format=%(refname)", ADOPT_REF_PREFIX, check=False
            ).stdout.split()
            if gone(r[len(ADOPT_REF_PREFIX) :].split("/", 1)[0])
        ]
        if refs:
            self.git(
                "update-ref", "--stdin", input="".join(f"delete {r}\n" for r in refs), check=False
            )


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
    ad = sub.add_parser("adopt")
    ad.add_argument("branch")
    ad.add_argument("--owner", required=True)
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
        if args.cmd == "adopt":
            return engine.cmd_adopt(args.branch, args.owner)
    except Unknown as exc:
        _err(
            f"ERROR: {exc}\nThis could not be established, so nothing changed; retry once it can be."
        )
        return 1
    except Refusal as exc:
        _err(f"ERROR: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
