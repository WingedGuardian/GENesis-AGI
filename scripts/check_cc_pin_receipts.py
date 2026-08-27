#!/usr/bin/env python3
"""CC pin-receipt guard — a PR that moves the Claude Code pin FORWARD must
carry the two gate receipts in its body.

``origin`` is the public repo, so merging the pin *is* the release. The update
procedure (``docs/reference/cc-compatibility.md`` §Updating) declares two
mandatory gates before that happens: the full changelog read over
``(pinned, target]``, and the local-first soak of the candidate. Before this
guard, both were prose. A pin PR could satisfy every reviewable receipt while
never having run either.

WHAT THIS GUARD IS, PRECISELY
-----------------------------
It stops **omission, not forgery**. Anyone can type a receipt line that is not
true; nothing here can tell. That limit is deliberate and already settled in
this repo: the ``review-depth-check`` job is advisory *by design* because "a
committed audit artifact is forgeable and the local hook is editable by the
same author, so the enforcing teeth are the independent reviewer + a required
human approval". A receipt guard that claimed to prove the soak happened would
be re-litigating that, and losing.

What it does do is convert *forgetting* into *consciously writing a false
statement*, which is a different act. That is the same job — and the same
strength — as ``scripts/check_hook_versions_complete.sh``, the completeness
backstop for the hook-version ledger. Treat this as a member of that family.

DOWNGRADES ARE EXEMPT
---------------------
A pin that moves BACKWARD needs no soak receipt by construction: it returns to
a version that already ran here. More importantly, the downgrade path is the
project's incident-recovery route — it is *why* a managed-settings
``requiredMinimumVersion`` floor was evaluated and deliberately rejected (see
the same doc), after a real 2.1.90 → 2.1.87 rollback. Putting a CI gate between
an operator and that rollback would be a regression dressed as rigor. The
exemption is automatic precisely so nobody has to remember a syntax under
incident pressure.

ERROR POLICY
------------
  * FAIL CLOSED (exit 1) only on the definite case: the pin moved forward and a
    required receipt is absent.
  * SKIP (exit 0, loud notice) whenever the comparison cannot be made — no base
    SHA, base ref not fetched (shallow clone), pin file absent at base, or no PR
    body in the environment. A guard that cannot see the old pin must not
    block every PR; it says so instead. The CI job checks out with
    ``fetch-depth: 0`` so this is the abnormal path, not the normal one.

Note it compares the PARSED PIN VALUE, not whether the file changed:
``scripts/lib/cc_version.sh`` is edited for many reasons that leave the pin
alone (its suppression helpers, probe dirs, shadow scan), and those PRs must
not be asked for soak receipts.

Usage:
    python scripts/check_cc_pin_receipts.py
        env: PR_BASE_SHA (base commit), PR_BODY (pull request body)
    python scripts/check_cc_pin_receipts.py --base-sha X --body-file B
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIN_PATH = "scripts/lib/cc_version.sh"

# Same shape as scripts/check_cc_node_lockstep.py::_PIN_RE — matches
# `CC_VERSION="${CC_VERSION:-2.1.201}"`, tolerant of quoting/spacing.
_CC_PIN_RE = re.compile(r'CC_VERSION="?\$\{CC_VERSION:-([0-9]+\.[0-9]+\.[0-9]+)\}"?')

# An unfilled template placeholder — `<candidate>`, `<date>`, `<source>`. The doc
# ships a copy-pasteable example, so a body pasted verbatim must not pass.
_PLACEHOLDER_RE = re.compile(r"<[a-zA-Z][a-zA-Z0-9 ._-]*>")

# PR-body trailers, matching the repo's existing `Ledger: <id>` / `Follow-up: <id>`
# convention. Value must be non-empty — a bare marker is not a receipt.
_RECEIPTS = {
    "CC-Gate-Changelog": (
        "the full changelog read over (pinned, target], per §Updating step 1 — "
        'e.g. "CC-Gate-Changelog: read (2.1.218, 2.1.246] in full from '
        '<source>, 2026-08-27"'
    ),
    "CC-Gate-Soak": (
        "the local-first soak: candidate, interval, running-binary sweep result, "
        'sign-off — e.g. "CC-Gate-Soak: 2.1.246 on container 2026-08-25..2026-08-27, '
        'check_cc_running_versions.sh clean, sign-off recorded"'
    ),
}


class MissingReceipts(Exception):
    """The pin moved forward without the required receipts (fail closed)."""


class Skip(Exception):
    """The comparison cannot be made — say so and pass."""


def parse_pin(text: str) -> str | None:
    """Extract the CC_VERSION default from a cc_version.sh body."""
    m = _CC_PIN_RE.search(text)
    return m.group(1) if m else None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def read_pin_at(ref: str, *, repo_root: Path) -> str | None:
    """Read the pin as of ``ref``. Raises Skip when the ref is unavailable."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{_PIN_PATH}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover — git missing is not a real CI case
        raise Skip(f"cannot run git: {exc}") from exc
    if out.returncode != 0:
        raise Skip(
            f"cannot read {_PIN_PATH} at {ref} "
            f"(shallow clone, or the file did not exist there): "
            f"{out.stderr.strip()[:200]}"
        )
    return parse_pin(out.stdout)


def missing_receipts(body: str) -> list[str]:
    """Return the receipt markers absent, valueless, or still holding a template.

    The template case matters: the doc prints a copy-pasteable example, and a
    body pasted verbatim would otherwise satisfy a presence-only check while
    saying nothing — `CC-Gate-Soak: <candidate> on container <start>..<end>` is
    not a receipt. An unfilled `<...>` placeholder anywhere in the value is
    treated as not-filled-in.
    """
    missing = []
    for marker in _RECEIPTS:
        # Marker at line start, a colon, then something that isn't whitespace.
        pattern = re.compile(rf"^\s*{re.escape(marker)}\s*:\s*(\S.*)$", re.MULTILINE)
        m = pattern.search(body)
        if not m or _PLACEHOLDER_RE.search(m.group(1)):
            missing.append(marker)
    return missing


def check(*, base_sha: str, body: str, repo_root: Path) -> str:
    """Run the guard. Returns a message, or raises MissingReceipts / Skip."""
    if not base_sha:
        raise Skip("no base SHA in the environment — not a pull-request context")

    head_pin_file = repo_root / _PIN_PATH
    try:
        head_pin = parse_pin(head_pin_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise Skip(f"cannot read {_PIN_PATH} at HEAD: {exc}") from exc
    if head_pin is None:
        raise Skip(f"could not parse CC_VERSION from {_PIN_PATH} at HEAD")

    base_pin = read_pin_at(base_sha, repo_root=repo_root)
    if base_pin is None:
        raise Skip(f"could not parse CC_VERSION from {_PIN_PATH} at {base_sha}")

    if head_pin == base_pin:
        return f"CC pin unchanged ({head_pin}) — no receipts required."

    if version_tuple(head_pin) < version_tuple(base_pin):
        return (
            f"CC pin moved BACKWARD ({base_pin} → {head_pin}) — exempt. "
            "A rollback returns to a version that already ran here, and the "
            "downgrade path is the project's incident-recovery route."
        )

    if not body.strip():
        raise Skip("no PR body available in the environment")

    absent = missing_receipts(body)
    if absent:
        lines = [
            f"CC pin moves FORWARD ({base_pin} → {head_pin}) but the PR body is "
            f"missing {len(absent)} required gate receipt(s):",
            "",
        ]
        for marker in absent:
            lines.append(f"  {marker}: — {_RECEIPTS[marker]}")
        lines += [
            "",
            "Merging this pin publishes it: `origin` is the public repo, and the",
            "host VM follows via `update-cc`. Both gates are mandatory before that",
            "(docs/reference/cc-compatibility.md §Updating).",
            "",
            "This guard checks the receipts are PRESENT; it cannot check they are",
            "true. If a gate genuinely was not run, run it — do not write the line.",
        ]
        raise MissingReceipts("\n".join(lines))

    return f"CC pin moves forward ({base_pin} → {head_pin}) and both gate receipts are present."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CC pin-receipt CI guard.")
    parser.add_argument("--base-sha", default=os.environ.get("PR_BASE_SHA", ""))
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="Read the PR body from a file instead of $PR_BODY.",
    )
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    if args.body_file is not None:
        try:
            body = args.body_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cc-pin-receipts: cannot read {args.body_file}: {exc}", file=sys.stderr)
            return 0
    else:
        body = os.environ.get("PR_BODY", "")

    try:
        message = check(base_sha=args.base_sha, body=body, repo_root=args.repo_root)
    except MissingReceipts as exc:
        print(f"cc-pin-receipts FAILED:\n{exc}", file=sys.stderr)
        return 1
    except Skip as exc:
        print(f"cc-pin-receipts SKIPPED (non-blocking): {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — deliberate catch-all, see below
        # A bug in THIS guard must never wall off the repository. There are no
        # required status checks here, but the local merge gate
        # (`git_push_guard.py --check-pr`) blocks on a red CI rollup, so an
        # unhandled exception would stop every PR from merging — over a check
        # that only ever guards a pin bump. The blocking path stays exactly one
        # named exception wide; everything unforeseen degrades to a loud skip.
        print(
            f"cc-pin-receipts SKIPPED — unexpected error in the guard itself: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 0
    print(f"cc-pin-receipts: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
