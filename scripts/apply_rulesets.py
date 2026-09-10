#!/usr/bin/env python3
"""Reconcile a repository's branch rulesets to the JSON in .github/rulesets/.

Rulesets are repository SETTINGS: they do not travel with a clone, a change to
one leaves no diff and no history, and nothing reviews it. This script makes
them behave like content — the files are the source of record, and applying
them is a reproducible step a fresh clone can run.

MATCHED BY NAME, never by id. A ruleset id is per-repository, so a fork or a
re-created ruleset would make an id-keyed file wrong everywhere but here. The
name is what a human sees in the settings UI and what the file declares.

DELETES NOTHING. A BRANCH ruleset whose name is not among the local files is
left exactly as it is and reported. This script's job is to assert what we
declare, never to assume our directory is the whole truth about someone's
repository — and a script that removes protections it does not recognise is a
script nobody should run against a repository that matters.

The word BRANCH is load-bearing and was missing from an earlier version of this
sentence: TAG and PUSH rulesets are filtered out of the listing, so they are
left alone AND NEVER REPORTED. An operator reading a clean run has seen the
repository's branch rules, not its whole rule surface.

Usage:
    python3 scripts/apply_rulesets.py --dry-run     # show the diff, write nothing
    python3 scripts/apply_rulesets.py --apply       # reconcile
    python3 scripts/apply_rulesets.py --apply --repo owner/name

Exit codes: 0 = in sync (or applied), 1 = drift found in --dry-run,
2 = an error that prevented a conclusion (never treat as "in sync").
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

RULESET_DIR = Path(__file__).resolve().parent.parent / ".github" / "rulesets"

# The fields we assert. A ruleset carries server-managed fields too (id,
# created_at, _links, node_id, current_user_can_bypass); comparing those would
# report drift on every run, so the comparison is scoped to what the file
# actually declares.
_COMPARED = ("target", "enforcement", "bypass_actors", "conditions", "rules")


def _gh_json(args: list[str]) -> object:
    """Run `gh` and parse stdout as JSON. Raises on any failure.

    Deliberately NOT fail-soft: this script decides whether enforcement is
    where we think it is, and an unreadable answer that degrades to "looks
    fine" is the exact failure mode the 2026-08-27 audit found (a ruleset was
    believed to be binding while a bypass entry made it decoration).
    """
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    out = proc.stdout or "null"
    if "--paginate" in args and "--slurp" in args:
        # `--paginate` alone emits each page as its OWN top-level JSON value,
        # so `json.loads` on the concatenated stdout raises the moment a second
        # page exists — turning the pagination fix into a hard failure at
        # exactly the scale it was added for (Codex P2, PR #1907). `--slurp`
        # wraps the pages in ONE array, so the pages arrive as a list of
        # per-page arrays and are flattened back to the caller's expected
        # shape here.
        pages = json.loads(out)
        if not isinstance(pages, list) or not all(isinstance(pg, list) for pg in pages):
            # RAISE rather than return the container unflattened. Returning it
            # let a page that is a DICT (an error object) reach the caller's
            # row loop, where `.get("target")` is None and the entry is silently
            # SKIPPED — so `live` came back empty, every declared ruleset read
            # ABSENT, and `--apply` would POST duplicates of rulesets that
            # already exist. That is precisely the degrade this function's own
            # docstring forbids (audit, PR #1907).
            raise RuntimeError(
                f"gh {' '.join(args)}: --slurp returned {type(pages).__name__}, "
                "expected a list of per-page lists — refusing to guess"
            )
        return [row for pg in pages for row in pg]
    return json.loads(out)


def _resolve_repo(explicit: str | None) -> str:
    """The repository to act on — resolved LIVE, never from config.

    A configured slug can name a real-but-wrong repository and return entirely
    plausible answers; this repo's own rules say to resolve it from `gh`.
    """
    if explicit:
        return explicit
    # No `--jq`: that prints a BARE string, which is not JSON, and the parse
    # then fails with a message about column 1 that says nothing about the
    # actual cause. Ask for the object and read the field.
    view = _gh_json(["repo", "view", "--json", "nameWithOwner"])
    slug = view.get("nameWithOwner") if isinstance(view, dict) else None
    if not isinstance(slug, str) or "/" not in slug:
        raise RuntimeError(f"could not resolve the current repository (got {slug!r})")
    return slug


def _local_definitions() -> dict[str, dict]:
    """Every .github/rulesets/*.json, keyed by its declared `name`."""
    out: dict[str, dict] = {}
    for path in sorted(RULESET_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        name = data.get("name")
        if not name:
            raise RuntimeError(f"{path} declares no `name` — cannot be matched")
        if name in out:
            raise RuntimeError(f"two files declare the ruleset name {name!r}")
        target = data.get("target", "branch")
        if target != "branch":
            # The live listing filters to `target == "branch"`, so a tag or push
            # ruleset declared here could never match one: the name would read
            # ABSENT on every run and `--apply` would POST another copy each
            # time, unbounded, each one enforcing. Refuse the file rather than
            # silently accumulating duplicates — the same "row skipped, reads
            # absent, create a duplicate" degrade the --slurp guard above exists
            # to prevent, one level down (audit, PR #1907).
            raise RuntimeError(
                f"{path} declares target {target!r}; this script reconciles BRANCH "
                "rulesets only and would create a duplicate of it on every apply"
            )
        out[name] = data
    if not out:
        raise RuntimeError(f"no ruleset definitions found in {RULESET_DIR}")
    return out


def _live_definitions(repo: str) -> dict[str, dict]:
    """Every live branch ruleset, by name, fetched in FULL.

    The list endpoint omits `rules` and `bypass_actors` — the two fields that
    decide whether a ruleset does anything — so each is re-fetched by id. A
    comparison against the list view alone would silently pass a ruleset whose
    every rule had been deleted.
    """
    # `--paginate`: a repo with more than one page of rulesets would otherwise
    # report a declared ruleset on page 2 as ABSENT, and `--apply` would try to
    # CREATE a duplicate of something that already exists.
    # `includes_parents=false`: for an org-owned repo this endpoint also returns
    # inherited ORG rulesets by default. One of those sharing a name with a
    # local definition would be treated as repo-owned — a drifted match then
    # PUT through the repo endpoint (which cannot reconcile it), or an exact
    # match suppressing creation of the repo ruleset we actually declared.
    # This repo is user-owned so neither bites here; other installs are the
    # point (Codex P2 ×2, PR #1907).
    listing = _gh_json(
        ["api", "--paginate", "--slurp", f"repos/{repo}/rulesets?includes_parents=false"]
    )
    live: dict[str, dict] = {}
    for row in listing or []:
        if row.get("target") != "branch":
            continue
        full = _gh_json(["api", f"repos/{repo}/rulesets/{row['id']}"])
        name = full["name"]
        if name in live:
            # The LOCAL side already refuses this (two files, one name); the
            # live side kept the last one silently, which is worse in both
            # directions. A dry run would report the survivor as in sync while
            # the hidden duplicate went on enforcing stale rules, and the
            # unmanaged-ruleset report reads the same name-keyed dict, so it
            # loses the duplicate too. Same class, opposite handling, forty
            # lines apart (Codex P2, PR #1907).
            raise RuntimeError(
                f"two LIVE rulesets are named {name!r} (ids {live[name]['id']} and "
                f"{full['id']}) — reconciling one would leave the other enforcing "
                f"unreviewed rules; remove or rename one in the repository settings"
            )
        live[name] = full
    return live


def _normalise(value: object) -> object:
    """Order-insensitive form for comparison.

    GitHub does not promise list order for rules or bypass actors, and a
    reordering is not a change — reporting it as drift would make the script
    cry wolf until nobody reads its output.
    """
    if isinstance(value, dict):
        # Keys the SERVER adds with a null value are dropped, so a
        # freshly-applied ruleset cannot read as drifted forever — every dry run
        # exiting 1 and every apply repeating a PUT that changes nothing (Codex
        # P2, PR #1907). Safe in both directions because a local definition never
        # declares a null; an explicit null on our side would drop from both.
        #
        # The example that comment used to give was WRONG and is worth correcting
        # rather than deleting, because it names the wrong echo behaviour:
        # MEASURED against this repository's live ruleset, a status-check entry
        # comes back as `{"context": "test"}` — `integration_id` is OMITTED, not
        # nulled. Absence is the real shape, and this normalisation does not
        # address it: a local `[]` or `{}` against an absent key still compares
        # as drift. Harmless as shipped (every key both files declare is echoed
        # in the same shape, verified key-for-key), but `required_reviewers` is
        # flagged beta in GitHub's schema and `approvals.json` declares it as
        # `[]` — the day the beta stops echoing it, that is where this bites.
        return {k: _normalise(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, list):
        return sorted((_normalise(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _protection_first(item: tuple[str, dict]) -> tuple[int, str]:
    """Sort key: a ruleset granting NO bypass is reconciled FIRST.

    MODULE SCOPE ON PURPOSE. Nested inside `main()` this was untestable, and
    the test written for it re-declared the same lambda — so inverting or
    deleting the shipped sort left the suite fully green while the property it
    exists to guarantee was gone (audit, PR #1907). A test grading a private
    copy of the code is the "my green is not evidence" shape, landing on the
    very fix that closed a P1.

    WHAT IT DOES NOT GUARANTEE, because the comment here used to over-claim it:
    this orders by which RULESET grants a bypass, never by which DIRECTION a
    change goes. Sound for adding a no-bypass ruleset beside a bypassed one —
    this migration — since the create lands before the update that removes the
    duplicate. It does NOT cover moving a rule OUT of the no-bypass set into
    the bypassed one: that writes the removal first and reopens the same gap a
    third time. Do that as two PRs — add to the destination, land, then remove
    from the source.
    """
    name, definition = item
    return (1 if definition.get("bypass_actors") else 0, name)


def _differences(local: dict, live: dict) -> list[str]:
    return [
        field for field in _COMPARED if _normalise(local.get(field)) != _normalise(live.get(field))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report drift, write nothing")
    mode.add_argument("--apply", action="store_true", help="reconcile the repository")
    parser.add_argument("--repo", default=None, help="owner/name (default: resolved live)")
    args = parser.parse_args()

    try:
        repo = _resolve_repo(args.repo)
        local = _local_definitions()
        live = _live_definitions(repo)
    except Exception as exc:  # noqa: BLE001 — the message is the product
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"repository: {repo}")

    # Creations before updates, and within each phase the no-bypass ruleset
    # first (`_protection_first`, whose docstring states exactly what that
    # does and does not guarantee). There is no transaction here, so ORDER
    # decides what a mid-run failure leaves behind: adding protection before
    # removing it makes the worst case a DUPLICATE rule — briefly stricter —
    # instead of a gap with required checks enforced nowhere.
    ordered = sorted(local.items(), key=_protection_first)
    absent = [(n, d) for n, d in ordered if n not in live]
    present = [(n, d) for n, d in ordered if n in live]
    drift = bool(absent)

    for name, definition in absent:
        print(f"  ABSENT   {name} — would be CREATED")
        if args.apply:
            try:
                _post(repo, definition)
                print(f"           created {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR creating {name}: {exc}", file=sys.stderr)
                print(
                    "           nothing was removed — earlier protections stand",
                    file=sys.stderr,
                )
                return 2

    for name, definition in present:
        current = live[name]
        fields = _differences(definition, current)
        if not fields:
            print(f"  IN SYNC  {name}")
            continue
        drift = True
        print(f"  DRIFT    {name} — differs in: {', '.join(fields)}")
        for field in fields:
            print(f"             local: {json.dumps(_normalise(definition.get(field)))}")
            print(f"             live : {json.dumps(_normalise(current.get(field)))}")
        if args.apply:
            try:
                _put(repo, current["id"], definition)
                print(f"           updated {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR updating {name}: {exc}", file=sys.stderr)
                return 2

    for name in sorted(set(live) - set(local)):
        # Reported, never touched — see the module docstring.
        print(f"  UNMANAGED {name} — present on the repository, not declared here; left alone")

    if args.apply:
        return 0
    if drift:
        print("\ndrift found — run with --apply to reconcile", file=sys.stderr)
        return 1
    print("\nall declared rulesets are in sync")
    return 0


def _post(repo: str, definition: dict) -> None:
    _api_with_body(
        ["api", "--method", "POST", f"repos/{repo}/rulesets", "--input", "-"], definition
    )


def _put(repo: str, ruleset_id: int, definition: dict) -> None:
    _api_with_body(
        ["api", "--method", "PUT", f"repos/{repo}/rulesets/{ruleset_id}", "--input", "-"],
        definition,
    )


def _api_with_body(args: list[str], body: dict) -> None:
    proc = subprocess.run(
        ["gh", *args],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh returned a non-zero status")


if __name__ == "__main__":
    sys.exit(main())
