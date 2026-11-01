"""Shared validator recording mechanics; CLI aliases retain their interfaces."""

from __future__ import annotations

import json
import re

from genesis.session_awareness.pr_evidence import NOTE_PREFIXED_GAPS, Decision


def resolve_repo(open_repos: list[str], pr_number: int, repo: str) -> str:
    """Return *repo* if ``(repo, pr_number)`` is an OPEN obligation, else raise LookupError.

    The document names the repository, so nothing is chosen here — only CHECKED.
    Before the document named it, a ``--repo`` flag did, unchecked, and it closed a
    DIFFERENT repository's obligation with this PR's evidence at exit 0, permanently
    (MEASURED end-to-end on an earlier draft). "Nothing open for this PR" and "that
    repo is not among the open ones" are different facts, so they read differently.
    """
    if not open_repos:
        raise LookupError(
            f"no OPEN row for PR #{pr_number} — it may be discharged already, or "
            f"never opened. Run `python3 scripts/repo_pulse_worker.py "
            f"--verification-backlog` to see the open set."
        )
    if repo not in open_repos:
        raise LookupError(
            f"no OPEN row for {repo}#{pr_number} (the repository the evidence document "
            f"names). Open rows for PR #{pr_number}: {', '.join(open_repos)}. If the "
            f"document names the wrong repository, correct the document."
        )
    return repo


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # json.loads keeps the LAST of two equal keys without a word, so a document with
    # "verdict" twice records whichever the author wrote second. Refuse instead.
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(
                f"the evidence document repeats the key {key!r} in one object; JSON "
                f"would keep only the last one silently, so it is refused"
            )
        out[key] = value
    return out


def attempt_note(decision: Decision, note: str | None) -> str | None:
    """Keep generated deployment/failure/park prefixes identical across writers."""
    if decision.closing:
        return None
    visible = [g for g in decision.derivation.gaps if g.startswith(NOTE_PREFIXED_GAPS)]
    parts = ["; ".join(visible), (note or "").strip()]
    if decision.parked:
        parts.insert(0, "PARKED (nothing material established)")
    return " — ".join(part for part in parts if part)


def gh_merge_commit(repo: str, pr: int) -> str:
    """The PR's merge commit sha from GitHub, or raise LookupError naming why not.

    The one network call in this tool, kept to one function so tests replace it
    (``main(..., merge_commit_of=...)``) and never call gh for real.
    """
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        raise LookupError("gh is not installed or not on PATH")
    argv = ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "mergeCommit"]
    try:
        # 120 s: gh on a dead network can hang with no output, and a validator's run
        # would sit there silently; a refusal that names the cause can be retried.
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        raise LookupError("gh did not answer within 120 s (offline?)") from None
    except OSError as exc:
        raise LookupError(f"gh could not run: {exc}") from None
    except UnicodeDecodeError:
        # text=True decodes inside run(): a non-UTF-8 byte raises here, not in json.
        raise LookupError("gh returned output that is not UTF-8") from None
    if proc.returncode != 0:
        raise LookupError(f"gh exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        oid = (json.loads(proc.stdout).get("mergeCommit") or {}).get("oid")
    except (ValueError, AttributeError):
        raise LookupError("gh returned output that is not the expected JSON") from None
    if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}", oid):
        raise LookupError(f"{repo}#{pr} has no merge commit on GitHub (is it merged?)")
    return oid


def check_merge_commit(claimed: str, actual: str) -> str | None:
    """None when *claimed* is a prefix of *actual* of 7+ hex characters, else why not."""
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", claimed) and actual.startswith(claimed.lower()):
        return None
    return (
        f"merge_commit {claimed!r} is not the PR's merge commit ({actual[:12]}…). It "
        f"must name the PR's own merge — not main HEAD, not the deployed commit (on a "
        f"stacked PR: the commit on its base branch, not the parent's squash on main) — "
        f"as 7 to 40 hex characters. MEASURED 2026-10-27: 5 of 15 documents in one group "
        f"named the wrong commit. Correct the document."
    )
