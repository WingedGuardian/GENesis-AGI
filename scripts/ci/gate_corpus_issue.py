#!/usr/bin/env python3
"""Open or update the one issue for what the weekly defect-corpus run found.

An escape exits 0 and notifies nobody, so without this it sits in a job summary. Run
by `.github/workflows/gate-corpus.yml` on main, from its own job with `issues: write`,
reading the findings the report job wrote.

Not `scripts/file_tracker_issue.py`: that one refuses without a write permission a
workflow token's `viewerPermission` is not known to report, treats a closed issue as a
duplicate so a later escape would stay silent, and needs the package installed for a
privacy scan. These bodies hold only manifest labels and rung names from this repo.

Exit 0 on nothing to do, an update or a new issue; 1 when a read or write fails, so
the job goes red rather than losing the finding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

TITLE = "Gate defect corpus: the weekly run found a test owed"
_MARKER = "<!-- gate-corpus-findings:"
#: The public can open an issue with this title or post this marker, so only what the
#: workflow's own token wrote counts. `gh` names that account differently by endpoint.
_AUTHOR = "app/github-actions"
_OWN_LOGINS = frozenset({"github-actions", "github-actions[bot]", "app/github-actions"})

Runner = Callable[[Sequence[str]], str]


def _gh(argv: Sequence[str]) -> str:
    done = subprocess.run(["gh", *argv], capture_output=True, text=True, timeout=120, check=True)
    return done.stdout


def _body(findings: list[str], run_url: str) -> str:
    digest = hashlib.sha256("\n".join(findings).encode()).hexdigest()[:16]
    lines = "\n".join(f"- {each}" for each in findings)
    return (
        f"The weekly run of `config/gate_defects.json` found:\n\n{lines}\n\n"
        f"An `ESCAPED` case or a rung that caught nothing means a test is owed. An off-aim "
        f"case means the rung it was aimed at did not notice.\n\n"
        f"Run: {run_url}\n\n{_MARKER}{digest} -->\n"
    )


def _open_issue(repo: str, gh: Runner) -> dict | None:
    """The open issue with exactly this title. A search that says it is incomplete
    raises, because a missed match would open a second issue."""
    query = f'repo:{repo} is:issue is:open author:{_AUTHOR} in:title "{TITLE}"'
    found = json.loads(gh(["api", "-X", "GET", "search/issues", "-f", f"q={query}"]))
    if found.get("incomplete_results"):
        raise RuntimeError("the issue search came back incomplete")
    for item in found.get("items", []):
        if item.get("title") == TITLE:
            return item
    return None


def _already_said(repo: str, number: int, body: str, gh: Runner) -> bool:
    """Whether the newest findings on this issue are these, so a weekly run with
    nothing new adds no comment."""
    seen = json.loads(gh(["issue", "view", str(number), "--repo", repo, "--json", "body,comments"]))
    texts = [seen.get("body") or ""]
    for comment in seen.get("comments", []):
        if (comment.get("author") or {}).get("login") in _OWN_LOGINS:
            texts.append(comment.get("body") or "")
    newest = next((t for t in reversed(texts) if _MARKER in t), "")
    return newest.rpartition(_MARKER)[2] == body.rpartition(_MARKER)[2]


def file_findings(path: Path, repo: str, run_url: str, gh: Runner = _gh) -> str:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("complete") is not True:
        return "the report did not finish, so it has no basis for a finding"
    findings = doc.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        raise ValueError(f"{path}: `findings` must be a list of strings")
    if not findings:
        return "nothing to report"
    body = _body(findings, run_url)
    issue = _open_issue(repo, gh)
    if issue is None:
        gh(["issue", "create", "--repo", repo, "--title", TITLE, "--body", body])
        return "opened an issue"
    number = issue["number"]
    if _already_said(repo, number, body, gh):
        return f"#{number} already says this"
    gh(["issue", "comment", str(number), "--repo", repo, "--body", body])
    return f"updated #{number}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("findings", type=Path)
    args = ap.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    try:
        print(file_findings(args.findings, repo, run_url))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"gate-corpus-issue: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
