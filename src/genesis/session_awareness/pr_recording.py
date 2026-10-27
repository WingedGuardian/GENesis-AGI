"""Shared validator recording mechanics; CLI aliases retain their interfaces."""

from __future__ import annotations

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
    result = str(note).strip()
    visible = [g for g in decision.derivation.gaps if g.startswith(NOTE_PREFIXED_GAPS)]
    if visible:
        result = "; ".join(visible) + " — " + result
    if decision.parked:
        result = "PARKED (nothing material established) — " + result
    return result
