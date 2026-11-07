"""deploy_candidates_manifest.py — the deploy manifest: its shape, and the one
place it is written.

The manifest (``$HOME/.genesis/deploy_manifest.json``) is INTENT only:

    {"version": 3, "repo": <absolute git common dir>,
     "candidates": [{"branch", "pr", "owner_session", "added_at",
                     "verified_head", "hook_approval"}]}

``verified_head`` pins the commit `add` saw: `live` runs that commit and never a
later one, until the candidate is added again. Running an unmerged branch on this
install's own server needs no approval (owner ruling, 2026-10-01), with ONE
exception: a candidate that changes a git or Claude Code hook goes live only with
the owner's approval in chat, recorded per candidate as ``hook_approval``
(owner ruling, #2978, 2026-10-06): ``null``, or ``{"head": <the pinned
verified_head>, "approved_by": <who said yes>, "approved_at": <time>}``. The
approval names the head it was given for, so adding the branch again at another
head clears it. Versions 1 (an ``owner_approved`` boolean) and 2 (no
``hook_approval``) are refused; no install ever held either, since `add` refuses
until the engine is armed.

The git hooks and the commit and merge guards read only ``repo`` from this file.
Everything here VALIDATES ON WRITE as well as on read, so no command can write a
manifest that every later reader, ``drop`` included, would then refuse.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path

# Imported only through deploy_candidates.py, whose finder resolves the
# sibling modules from this directory: scripts/ is never on sys.path.
from deploy_candidates_core import HEX40, Refusal, valid_candidate_name  # noqa: E402

MANIFEST_VERSION = 3
_ENTRY_KEYS = {"branch", "pr", "owner_session", "added_at", "verified_head", "hook_approval"}
_APPROVAL_KEYS = {"head", "approved_by", "approved_at"}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def manifest_problem(data: object) -> str:
    """Why this is not a manifest this engine writes, or "" when it is."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("version") != MANIFEST_VERSION:
        return f"version {data.get('version')!r}, not {MANIFEST_VERSION}"
    repo = data.get("repo")
    if not isinstance(repo, str) or not os.path.isabs(repo):
        return '"repo" is not an absolute path'
    cands = data.get("candidates")
    if not isinstance(cands, list):
        return '"candidates" is not a list'
    seen = set()
    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            return f"candidate {i} is not an object"
        if set(c) != _ENTRY_KEYS:
            return f"candidate {i} has keys {sorted(c)}, not {sorted(_ENTRY_KEYS)}"
        if not valid_candidate_name(c["branch"]):
            return f"candidate {c['branch']!r} is not a branch name a candidate can have"
        if c["branch"] in seen:
            return f"candidate {c['branch']} is listed twice"
        seen.add(c["branch"])
        pr = c["pr"]
        if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0):
            return f"candidate {c['branch']} has a pr that is not a positive number"
        for key in ("owner_session", "added_at"):
            if not _nonempty(c[key]):
                return f"candidate {c['branch']} has no {key}"
        if not isinstance(c["verified_head"], str) or not HEX40.match(c["verified_head"]):
            return f"candidate {c['branch']} has no full verified_head"
        ha = c["hook_approval"]
        if ha is not None:
            if not isinstance(ha, dict) or set(ha) != _APPROVAL_KEYS:
                return f"candidate {c['branch']} has a hook_approval that is not {sorted(_APPROVAL_KEYS)}"
            if ha["head"] != c["verified_head"]:
                return f"candidate {c['branch']}'s hook_approval is for another head than the pinned one"
            for key in ("approved_by", "approved_at"):
                if not _nonempty(ha[key]):
                    return f"candidate {c['branch']}'s hook_approval has no {key}"
    return ""


class ForeignManifest(Refusal):
    """The manifest names another repository: for this checkout there is no
    manifest (what the predicate reads as `other`), not a broken one."""


class ManifestStore:
    """Reads and writes one manifest, bound to one repository's git directory."""

    def __init__(self, home: Path, common_dir: Callable[[], str]):
        self.path = Path(home) / ".genesis" / "deploy_manifest.json"
        self.lock_path = Path(home) / ".genesis" / "deploy_manifest.json.lock"
        self._common_dir = common_dir

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict | None:
        """The manifest, validated, or None when there is none. Anything this
        engine cannot read raises Refusal: never "empty"."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refusal(f"the deploy manifest {self.path} is unreadable: {exc}") from exc
        why = manifest_problem(data)
        if why:
            raise Refusal(
                f"the deploy manifest {self.path} is malformed: {why}. Fix it by hand; nothing was changed."
            )
        if os.path.realpath(data["repo"]) != self._common_dir():
            raise ForeignManifest(
                f"the deploy manifest {self.path} belongs to another repository ({data['repo']}), "
                f"not {self._common_dir()}."
            )
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def update(self, change: Callable[[dict | None], dict | None]) -> dict | None:
        """Read-modify-write under the manifest's own lock. ``change`` gets the
        validated manifest (or None) and returns what to write (None: nothing).
        The NEW value is validated before it is written: a change that would
        leave a manifest the readers refuse raises Refusal and writes nothing."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            data = self.load()
            new = change(data)
            if new is None:
                return None
            why = manifest_problem(new)
            if why:
                raise Refusal(
                    f"refusing to write a malformed deploy manifest: {why}. Nothing was changed."
                )
            self._write(new)
            return new
