#!/usr/bin/env python3
"""Reconcile a repository's branch rulesets to the JSON in .github/rulesets/.

Rulesets are repository SETTINGS: they do not travel with a clone, a change to
one leaves no diff and no history, and nothing reviews it. This script makes
them behave like content — the files are the source of record, and applying
them is a reproducible step a fresh clone can run.

MATCHED BY NAME, never by id. A ruleset id is per-repository, so a fork or a
re-created ruleset would make an id-keyed file wrong everywhere but here. The
name is what a human sees in the settings UI and what the file declares.

DELETES NOTHING. A ruleset whose name is not among the local files is left
exactly as it is and reported. This script's job is to assert what we declare,
never to assume our directory is the whole truth about someone's repository —
and a script that removes protections it does not recognise is a script nobody
should run against a repository that matters.

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
    return json.loads(proc.stdout or "null")


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
    listing = _gh_json(["api", f"repos/{repo}/rulesets"])
    live: dict[str, dict] = {}
    for row in listing or []:
        if row.get("target") != "branch":
            continue
        full = _gh_json(["api", f"repos/{repo}/rulesets/{row['id']}"])
        live[full["name"]] = full
    return live


def _normalise(value: object) -> object:
    """Order-insensitive form for comparison.

    GitHub does not promise list order for rules or bypass actors, and a
    reordering is not a change — reporting it as drift would make the script
    cry wolf until nobody reads its output.
    """
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted((_normalise(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


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

    # CREATIONS FIRST, then updates. There is no transaction here — each
    # ruleset is its own API call — so the ORDER decides what a mid-run failure
    # leaves behind. Applying alphabetically put the approvals UPDATE (which
    # removes required_status_checks from the old combined ruleset) before the
    # checks CREATE: a failure in between left the repository with required
    # checks in NEITHER ruleset, i.e. weaker than before the run, while the
    # error message talked only about the create. Adding protection before
    # removing it makes the worst case a DUPLICATE rule — briefly stricter —
    # instead of a gap.
    absent = [(n, d) for n, d in local.items() if n not in live]
    present = [(n, d) for n, d in local.items() if n in live]
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
