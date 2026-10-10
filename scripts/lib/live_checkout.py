"""Is this checkout running `live`, the integration branch the deploy manifest builds?

`live` is rebuilt by ``scripts/deploy_candidates`` from origin/main plus the
candidate branches listed in ``$HOME/.genesis/deploy_manifest.json``. A deploy
path that pulls or merges into the checkout would wipe that build, so the deploy
scripts and the dashboard's update routes ask this module first.

Prints one word and exits with its code:

* ``live`` (0): HEAD is the branch ``live`` AND the manifest names this
  repository (its ``repo`` key is the absolute git common dir of this checkout).
* ``other`` (1): HEAD is not on ``live``; or there is no manifest; or the
  manifest names another repository. The caller's usual branch rule applies.
* ``unreadable`` (2): HEAD is on ``live`` but the manifest, or git, cannot be
  read well enough to say. Callers refuse.

Callers act on a verdict only when the printed word and the exit code AGREE: the
interpreter itself exits 1 on a SyntaxError or an uncaught exception and 2 on a
missing file, so a code alone could read a broken copy of this file as ``other``.

The manifest is read for its ``repo`` key ONLY
(``test_every_manifest_reader_on_this_tree_reads_only_the_repo_key`` holds every
reader to that key), with the binding the git hooks and the commit and push guards
use (``live_manifest_applies``, ``_live_manifest_binding``): ``repo`` is the
absolute path of an existing git common dir, compared by realpath. One row
differs, in the strict direction: a manifest with no ``repo`` key is
``unreadable`` here, where those readers keep a legacy rule for it. The manifest
is consulted only when HEAD is ``live``, so a checkout on any other branch never
depends on it.

Run with ``python3 -I -S`` (stdlib only): callers read this file's text before
they check anything and run that copy, as they do ``serving_commit.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

LIVE, OTHER, UNREADABLE = "live", "other", "unreadable"
_CODES = {LIVE: 0, OTHER: 1, UNREADABLE: 2}


def _git(root: str, *args: str) -> str | None:
    # Every call is a local read (no network, no lock). The bound exists because a
    # caller holds the update lock or a dashboard request thread while it waits;
    # 30 s is far past any local read and a hang reads as "cannot tell" (refuse).
    # GIT_* from the caller (GIT_DIR, GIT_WORK_TREE, ...) would point every call at
    # some other repository than <root>, so they are dropped, as the engine does.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=30, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _is_common_dir(path: object) -> bool:
    return (
        isinstance(path, str)
        and os.path.isabs(path)
        and os.path.isdir(os.path.join(path, "objects"))
        and os.path.isfile(os.path.join(path, "HEAD"))
    )


def state(root: str) -> str:
    # The full ref, not --short: with a tag also named `live`, --short prints
    # `heads/live` and the branch would read as another one.
    ref = _git(root, "symbolic-ref", "-q", "HEAD")
    if ref is None:
        # A detached HEAD makes symbolic-ref exit 1 with no output; anything git
        # cannot answer at all also lands here. Neither is `live`.
        return UNREADABLE if _git(root, "rev-parse", "--git-dir") is None else OTHER
    if ref != "refs/heads/live":
        return OTHER
    path = os.path.join(os.path.expanduser("~"), ".genesis", "deploy_manifest.json")
    if not os.path.lexists(path):
        return OTHER
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return UNREADABLE
    repo = data.get("repo") if isinstance(data, dict) else None
    if not _is_common_dir(repo):
        return UNREADABLE
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common or "\n" in common or not os.path.isabs(common):
        return UNREADABLE
    return LIVE if os.path.realpath(repo) == os.path.realpath(common) else OTHER


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: live_checkout.py <checkout root>", file=sys.stderr)
        return _CODES[UNREADABLE]
    try:
        verdict = state(argv[1])
    except Exception:  # noqa: BLE001 — a crash must read as "cannot tell", never "other"
        verdict = UNREADABLE
    print(verdict)
    return _CODES[verdict]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
