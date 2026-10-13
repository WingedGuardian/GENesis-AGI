#!/usr/bin/env python3
"""Fail when tests stop being collected and the change does not explain it.

WHY. A test that stops being collected does not turn the suite red. It makes
the suite smaller, and a smaller green suite reads like a healthy one. The CI
``test`` job used to guard this with one hand-maintained number
(``check_skip_ceiling.py --min-collected``), which every test-adding PR edited.
Any two such PRs therefore conflicted on the same line, and a floor set too low
still passed. MEASURED 2026-10-12: the runner collected 43,129 tests while the
floor read 25,718, so a loss of about 40% of the suite would have passed.

WHAT IT DOES. The ``collected-floor`` job collects node ids twice, at the same
checkout path, with ``collect_ids_plugin``: once on the PR tree and once on the
commit it lands on. This script compares the two sets. A vanished id is
EXPLAINED only when the change touched its test file, which the plugin
records per id (a file name may itself contain ``::``, so an id is never cut
apart to find it). Deleting or editing a test is visible in review; a
test that drops out because of a change somewhere else is the dormancy this job
exists to catch. That includes a ``conftest.py`` change: ``tests/conftest.py``
sits above the whole suite, so letting it explain drops below it would switch
the check off for every PR that edits it (21 of 1,230 main commits since July).
Unexplained drops under a changed conftest are named as such, and a deliberate
one takes the escape line. MEASURED: over 69 consecutive main CI pairs, no drop
needed a conftest to explain it.

An unexplained drop fails, unless the PR body carries a visible line
``Collected-drop: <reason>`` (fenced code and HTML comments do not count; the
shared ``readable_body`` scanner decides visibility). The escape turns the
failure into a loud warning. A NEW collection error on the PR tree always
fails: that is a broken test module, and no reason excuses it.

SAME PATH, ON PURPOSE. MEASURED: three parametrize ids embed the checkout's
absolute root (``test_guard_ansic_fail_closed.py`` ``[GIT_WORK_TREE-<root>]``,
``test_index_marker.py`` ``[<root>]``). Collected at two different paths they
differ on every PR, so the job collects both trees in the same workspace.

REFUSAL. "Could not measure" never shares an exit code with "measured, fine":
0 ok (an escaped drop included) / 1 unexplained drop or new collection error /
2 refused (missing or malformed input, an empty tree, or an unexplained drop
whose PR body could not be read).

SUMMARY BUDGET. GitHub does not render a step summary over 1 MiB, so ids are
listed whole until a byte budget runs out, and the number omitted is stated.
The full id sets travel as the job's artifact. Stdlib only, so the bare
``python3`` the workflow runs needs nothing installed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

TAG = "COLLECTED-FLOOR"
OK, FAIL, REFUSED = 0, 1, 2

# GitHub refuses to render a step summary over 1 MiB; stay well under it and
# keep room for the closing lines.
_SUMMARY_BUDGET = 900_000

_DROP_RE = re.compile(r"^\s*collected-drop\s*:\s*(\S.*)$", re.IGNORECASE)


def _say(msg: str) -> None:
    print(f"{TAG}: {msg}")


def _refuse(msg: str) -> int:
    print(f"::error::{TAG}: REFUSING — {msg}")
    print(f"{TAG}: REFUSING — {msg}", file=sys.stderr)
    return REFUSED


def _load_tree(path: str) -> dict | str:
    """The plugin's JSON for one tree, or the reason it cannot be used."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"no collection output at {path}: collection did not finish"
    except (OSError, ValueError) as exc:
        return f"collection output at {path} is unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return f"collection output at {path} is not an object"
    for key in ("ids", "errors", "skipped"):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return f"collection output at {path} has no string list {key!r}"
    paths = data.get("paths")
    if not isinstance(paths, dict) or not all(isinstance(paths.get(i), str) for i in data["ids"]):
        return f"collection output at {path} does not map every id to its file"
    if not data["ids"]:
        return f"collection output at {path} collected 0 tests, so it measures nothing"
    return data


def read_changed(path: str) -> set[str] | str:
    """Paths from ``git diff -z --name-only``, or the reason they cannot be read.

    Read as BYTES and split on NUL only: a git path may contain a newline,
    surrounding spaces or bytes that are not UTF-8, and none of that may be
    stripped or refused. ``os.fsdecode`` keeps undecodable bytes as surrogates,
    the convention the plugin's ``os.fspath`` paths follow too.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return f"changed-file list unreadable: {type(exc).__name__}: {exc}"
    return {os.fsdecode(p) for p in raw.split(b"\0") if p}


def _conftest_dirs(changed: set[str]) -> set[str]:
    return {p.rpartition("/")[0] for p in changed if p.rpartition("/")[2] == "conftest.py"}


def under_changed_conftest(path: str, conftest_dirs: set[str]) -> bool:
    """True when a changed ``conftest.py`` sits in this file's directory or above."""
    parent = path.rpartition("/")[0]
    return any(d == "" or parent == d or parent.startswith(d + "/") for d in conftest_dirs)


def _readable_body(body: str) -> str | None:
    """Visible text of a PR body via the shared scanner, or None if unloadable."""
    name = "_cc_pin_receipts_for_collected_floor"
    try:
        path = Path(__file__).resolve().parent.parent / "check_cc_pin_receipts.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if not (spec and spec.loader):
            return None
        mod = importlib.util.module_from_spec(spec)
        # Registered before exec: that module's dataclasses resolve their own
        # module through sys.modules.
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return mod.readable_body(body)
    except Exception:
        return None


def drop_reason(body: str) -> str | None | bool:
    """The visible ``Collected-drop:`` reason, None if absent, False if unscannable."""
    visible = _readable_body(body)
    if visible is None:
        return False
    for line in visible.split("\n"):
        match = _DROP_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def _fence(lines: list[str]) -> str:
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


class _Summary:
    """Markdown for the step summary, spent on WHOLE rows within a budget."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.used = 0

    def add(self, text: str) -> bool:
        # Sized as it will be written (the summary file escapes surrogates too),
        # so an undecodable path byte cannot raise here.
        size = len(text.encode("utf-8", "backslashreplace"))
        if self.used + size > _SUMMARY_BUDGET:
            return False
        self.parts.append(text)
        self.used += size
        return True

    def id_block(self, title: str, ids: list[str]) -> None:
        if not ids:
            return
        fence = _fence(ids)
        if not self.add(f"\n**{title}** ({len(ids)})\n\n{fence}\n"):
            self.parts.append(f"\n**{title}**: {len(ids)} omitted (summary budget spent)\n")
            return
        shown = 0
        for nodeid in ids:
            if not self.add(nodeid + "\n"):
                break
            shown += 1
        self.parts.append(f"{fence}\n")
        if shown < len(ids):
            self.parts.append(
                f"\n{len(ids) - shown} of {len(ids)} not listed (summary budget spent); "
                "every id is in the job's `collected-floor` artifact.\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", help="collect_ids_plugin JSON for the base tree")
    parser.add_argument("pr", help="collect_ids_plugin JSON for the PR tree")
    parser.add_argument(
        "--changed",
        required=True,
        help="file: `git diff -z --name-only` output (NUL-separated paths)",
    )
    body = parser.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file", help="the PR body, read for a Collected-drop line")
    body.add_argument(
        "--body-unreadable", action="store_true", help="the PR body could not be read"
    )
    body.add_argument("--no-body", action="store_true", help="no PR exists for this change")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)
    # A path with bytes that are not UTF-8 arrives as surrogates; print it
    # escaped rather than crash on the one id this job most needs to name.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")

    base = _load_tree(args.base)
    if isinstance(base, str):
        return _refuse(base)
    pr = _load_tree(args.pr)
    if isinstance(pr, str):
        return _refuse(pr)
    changed = read_changed(args.changed)
    if isinstance(changed, str):
        return _refuse(changed)
    body_text = None
    if args.body_file:
        try:
            body_text = Path(args.body_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return _refuse(f"PR body file unreadable: {type(exc).__name__}: {exc}")

    base_ids, pr_ids = set(base["ids"]), set(pr["ids"])
    vanished = base_ids - pr_ids
    added = pr_ids - base_ids
    conftest_dirs = _conftest_dirs(changed)
    file_of = base["paths"]
    unexplained = sorted(i for i in vanished if file_of[i] not in changed)
    explained = sorted(vanished - set(unexplained))
    new_errors = sorted(set(pr["errors"]) - set(base["errors"]))
    newly_skipped = sorted(set(pr["skipped"]) - set(base["skipped"]))

    _say(
        f"base {len(base_ids)}, PR {len(pr_ids)}: +{len(added)} added, "
        f"-{len(vanished)} vanished ({len(explained)} explained by the change, "
        f"{len(unexplained)} unexplained)"
    )
    for module in newly_skipped:
        _say(f"now skipped at import (not collected): {module}")
    for nodeid in base["errors"]:
        if nodeid not in pr["errors"]:
            _say(f"base-only collection error (fixed by this change): {nodeid}")

    verdict = OK
    escape = None
    if new_errors:
        verdict = FAIL
        for nodeid in new_errors:
            print(f"::error::{TAG}: new collection error on the PR tree: {nodeid}")
    if unexplained:
        files = sorted({file_of[i] for i in unexplained})
        for nodeid in unexplained:
            _say(f"unexplained: {nodeid}")
        near = [f for f in files if under_changed_conftest(f, conftest_dirs)]
        if near:
            where = ", ".join(sorted(d or "." for d in conftest_dirs))
            print(
                f"::warning::{TAG}: {len(near)} of those file(s) sit under a conftest.py "
                f"this change edits ({where}); that conftest change is the likely cause."
            )
        if body_text is not None:
            escape = drop_reason(body_text)
        if escape is False:
            return _refuse("could not load the shared PR-body scanner to read Collected-drop")
        if escape:
            print(
                f"::warning::{TAG}: {len(unexplained)} test(s) in {len(files)} untouched file(s) "
                f"stopped being collected; accepted by the PR's Collected-drop line: {escape}"
            )
        elif args.body_unreadable:
            return _refuse(
                f"{len(unexplained)} unexplained drop(s), and the PR body could not be read "
                "to check for a Collected-drop line"
            )
        else:
            verdict = FAIL
            print(
                f"::error::{TAG}: {len(unexplained)} test(s) in {len(files)} file(s) this change "
                "did not touch stopped being collected (a directory out of collection, a "
                "config or dependency change, an import now skipped). If deliberate, add a "
                "line 'Collected-drop: <reason>' to the PR body, then re-run this job, or "
                "push a commit: a re-run clears the failure only while the merge gate reads "
                "a re-run's latest attempt (#2624)."
            )
    if explained:
        print(f"::notice::{TAG}: {len(explained)} test(s) removed from files this change touched")

    if args.summary:
        summary = _Summary()
        state = "FAIL" if verdict == FAIL else ("OK, drop accepted" if escape else "OK")
        summary.add(
            f"## Collected tests: {state}\n\nBase {len(base_ids)}, PR {len(pr_ids)}: "
            f"+{len(added)} added, -{len(vanished)} vanished "
            f"({len(explained)} explained, {len(unexplained)} unexplained).\n"
        )
        if escape:
            summary.add(f"\nAccepted by `Collected-drop:` {escape}\n")
        summary.id_block("New collection errors on the PR tree", new_errors)
        summary.id_block("Unexplained: vanished from files this change did not touch", unexplained)
        summary.id_block("Modules now skipped at import", newly_skipped)
        summary.id_block("Explained: removed from files this change touched", explained)
        try:
            with open(args.summary, "a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write("".join(summary.parts))
        except OSError as exc:
            print(f"::warning::{TAG}: could not write the step summary: {exc}")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
