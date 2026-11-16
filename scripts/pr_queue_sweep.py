#!/usr/bin/env python3
"""Publish a read-only worklist for every open PR in the canonical repo."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "hooks"))

import git_push_guard as guard  # noqa: E402

from genesis.env import genesis_home  # noqa: E402
from genesis.session_awareness import repo_pulse_gh  # noqa: E402
from genesis.util.atomic import atomic_write_text  # noqa: E402

GUARD_PATH = ROOT / "scripts" / "hooks" / "git_push_guard.py"
REPORT_BUDGET = 15
REPORT_MAX_AGE = timedelta(hours=24)
UNTOUCHED_AGE = timedelta(days=3)
UNKNOWN_REREAD_DELAY = 5
SUBPROCESS_TIMEOUT = 7200
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
STATES = {
    "ready", "blocked", "rework-specced", "rework-unspecced", "architecture",
    "parked", "draft-unnamed",
}


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _age(value: object, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unknown age"
    seconds = max(0, int((now - parsed).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"


def _gate_rows(stdout: str) -> tuple[dict, list[dict], str | None, str | None]:
    rows, raw_rows, verdicts = {}, [], []
    pattern = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*?)\s*:\s?(.*)$")
    for line in stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name, value = match.group(1).strip(), match.group(2)
        rows[name] = value
        raw_rows.append({"name": name, "value": value, "line": line})
        if name.casefold() == "verdict":
            verdicts.append(value)
    if len(verdicts) != 1:
        return rows, raw_rows, None, None
    verdict = verdicts[0]
    if verdict == "MERGEABLE (all gates pass)":
        return rows, raw_rows, verdict, "MERGEABLE"
    if re.fullmatch(r"[1-9][0-9]* gate\(s\) would block", verdict):
        return rows, raw_rows, verdict, "BLOCKED"
    return rows, raw_rows, verdict, None


async def _run_gate(number: int, repo: str) -> tuple[int, str, str, str | None]:
    argv = [sys.executable, str(GUARD_PATH), "--check-pr", str(number), "--repo", repo]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return 127, "", f"gate spawn failed: {exc}", str(exc)
    pending = asyncio.create_task(proc.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(pending), SUBPROCESS_TIMEOUT)
    except TimeoutError:
        proc.kill()
        stdout, stderr = await pending
        return 124, stdout.decode(errors="replace"), stderr.decode(errors="replace"), "gate timed out"
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        None,
    )


def _gate_report(
    head_sha: str | None, updated_at: str | None, read_at: str,
    result: tuple[int, str, str, str | None],
) -> tuple[dict | None, str | None]:
    code, stdout, stderr, spawn_error = result
    rows, raw_rows, verdict, kind = _gate_rows(stdout)
    report = {
        "read_at": read_at,
        "reported_head_sha": head_sha,
        "reported_updated_at": updated_at,
        "verdict": verdict,
        "verdict_kind": kind,
        "rows": rows,
        "raw_rows": raw_rows,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": code,
    }
    if spawn_error:
        return report, spawn_error
    if kind is None:
        return report, "gate output omitted or malformed its verdict row"
    if (code, kind) not in {(0, "MERGEABLE"), (1, "BLOCKED")}:
        return report, f"gate exit {code} disagrees with verdict {verdict!r}"
    return report, None


def _api_pr(raw: dict, old: dict | None) -> dict:
    errors = []
    number = raw["number"]
    title = raw.get("title")
    if not isinstance(title, str):
        errors.append("title is missing or malformed")
        title = "[unreadable title]"
    draft = raw.get("isDraft")
    if type(draft) is not bool:
        errors.append("draft status is missing or malformed")
        draft = False
    head_sha = raw.get("headRefOid")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        errors.append("head SHA is missing or malformed")
        head_sha = None
    base = raw.get("baseRefName")
    if not isinstance(base, str) or not base:
        errors.append("base branch is missing or malformed")
        base = None
    updated_at = raw.get("updatedAt")
    if _parse_time(updated_at) is None:
        errors.append("updatedAt is missing or malformed")
    mergeable_raw = raw.get("mergeable")
    mergeable = mergeable_raw
    if not isinstance(mergeable, str) or mergeable not in {
        "MERGEABLE", "CONFLICTING", "UNKNOWN"
    }:
        errors.append("mergeability is missing or malformed; recorded as UNKNOWN")
        mergeable = "UNKNOWN"
    label_data = raw.get("labels")
    label_nodes = label_data.get("nodes") if isinstance(label_data, dict) else None
    labels = []
    if not isinstance(label_nodes, list):
        errors.append("labels are missing or malformed")
    else:
        for label in label_nodes:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                errors.append("a label row is malformed")
            else:
                labels.append(label["name"])
    if raw.get("_queue_error"):
        errors.append(str(raw["_queue_error"]))
    commits = raw.get("commits")
    commit_nodes = commits.get("nodes") if isinstance(commits, dict) else None
    last_commit = None
    if isinstance(commit_nodes, list) and commit_nodes:
        commit = commit_nodes[0].get("commit") if isinstance(commit_nodes[0], dict) else None
        last_commit = commit.get("committedDate") if isinstance(commit, dict) else None
    if _parse_time(last_commit) is None:
        errors.append("newest commit date is missing or malformed")
        last_commit = None
    gate = old.get("gate") if isinstance(old, dict) else None
    attempted = old.get("report_attempted_at") if isinstance(old, dict) else None
    return {
        "number": number,
        "title": title,
        "draft": draft,
        "labels": labels,
        "head_sha": head_sha,
        "base": base,
        "updated_at": updated_at if isinstance(updated_at, str) else None,
        "last_commit_at": last_commit,
        "mergeable": mergeable,
        "mergeable_raw": mergeable_raw,
        "state": "blocked",
        "gate": gate if isinstance(gate, dict) else None,
        "report_attempt": old.get("report_attempt") if isinstance(old, dict) else None,
        "conflict_files": [],
        "error": None,
        "errors": errors,
        "warnings": [],
        "report_attempted_at": attempted,
        "report_error": old.get("report_error") if isinstance(old, dict) else None,
        "report_status": "unread",
        "url": raw.get("url") if isinstance(raw.get("url"), str) else None,
    }


def _report_fresh(pr: dict, now: datetime) -> bool:
    gate = pr.get("gate")
    read_at = _parse_time(gate.get("read_at")) if isinstance(gate, dict) else None
    return bool(
        read_at
        and not pr.get("report_error")
        and gate.get("reported_head_sha") == pr.get("head_sha")
        and gate.get("reported_updated_at") == pr.get("updated_at")
        and now - read_at <= REPORT_MAX_AGE
        and re.fullmatch(r"[0-9a-fA-F]{40}", pr.get("head_sha") or "")
        and _parse_time(pr.get("updated_at"))
    )


def _candidate_key(pr: dict) -> tuple:
    gate = pr.get("gate")
    if not isinstance(gate, dict) or not _parse_time(gate.get("read_at")):
        attempted = _parse_time(pr.get("report_attempted_at"))
        return (0, attempted is not None, attempted or datetime.min.replace(tzinfo=UTC))
    return (1, False, _parse_time(gate.get("read_at")))


def _comment_heading(body: object, heading: str) -> bool:
    if not isinstance(body, str):
        return False
    first = body.lstrip().splitlines()[0] if body.lstrip() else ""
    return bool(re.match(rf"^{re.escape(heading)}(?:\s|$)", first))


async def _read_state_comments(pr: dict, repo: str, runner, now: datetime) -> None:
    labels = set(pr["labels"])
    if not labels.intersection({"needs-rework", "parked"}):
        return
    try:
        result = await repo_pulse_gh.list_queue_pr_comments(repo, pr["number"], runner=runner)
    except Exception as exc:  # per-PR read failures do not discard the queue
        pr["errors"].append(f"comment read failed: {exc}")
        pr["comment_read_error"] = str(exc)
        return
    if result.get("error") or result.get("limit_hit"):
        error = result.get("error") or (
            f"comments read {result.get('read', 0)} of {result.get('total', 0)}"
        )
        pr["errors"].append(f"comment read failed: {error}")
        pr["comment_read_error"] = error
        return
    found = {}
    for comment in result["comments"]:
        if not isinstance(comment, dict):
            pr["errors"].append("comment row is malformed")
            continue
        if comment.get("authorAssociation") not in MAINTAINER_ASSOCIATIONS:
            continue
        created = comment.get("createdAt")
        for key, heading in (("rework", "## Rework spec"), ("parked", "## Parked")):
            if not _comment_heading(comment.get("body"), heading):
                continue
            parsed = _parse_time(created)
            if parsed is None:
                pr["errors"].append(f"maintainer {heading} comment has invalid createdAt")
            elif key not in found or parsed > found[key][0]:
                found[key] = (parsed, created)
    if "rework" in found:
        pr["rework_comment_at"] = found["rework"][1]
    if "parked" in found:
        pr["parked_comment_at"] = found["parked"][1]
    if "parked" in labels:
        if pr.get("parked_comment_at"):
            pr["parked_comment_age"] = _age(pr["parked_comment_at"], now)
        elif not pr.get("comment_read_error"):
            pr["warnings"].append("parked label has no maintainer ## Parked comment")


def _state(pr: dict, now: datetime) -> None:
    labels = set(pr["labels"])
    fresh = _report_fresh(pr, now)
    gate = pr.get("gate")
    if pr["draft"]:
        pr["report_status"] = "not-applicable"
    elif pr.get("report_error"):
        pr["report_status"] = "error"
    elif fresh:
        pr["report_status"] = "fresh"
    elif gate:
        pr["report_status"] = "stale"
    else:
        pr["report_status"] = "unread"
    if pr["errors"]:
        pr["readiness_status"] = "unknown"
        pr["readiness_reason"] = "PR facts or required reads are incomplete"
    elif pr["mergeable"] != "MERGEABLE":
        pr["readiness_status"] = "unknown"
        pr["readiness_reason"] = f"GitHub mergeability is {pr['mergeable']}; readiness is unknown"
    elif pr["report_status"] != "fresh":
        pr["readiness_status"] = "unknown"
        pr["readiness_reason"] = pr.get("state_reason") or "gate report is not current"
    elif isinstance(gate, dict) and gate.get("verdict_kind") == "MERGEABLE":
        pr["readiness_status"] = "ready"
        pr["readiness_reason"] = None
    else:
        pr["readiness_status"] = "blocked"
        pr["readiness_reason"] = gate.get("verdict") if isinstance(gate, dict) else "gate verdict unavailable"
    if "needs-architecture-session" in labels:
        pr["state"] = "architecture"
    elif "needs-rework" in labels:
        if pr.get("comment_read_error"):
            pr["state"] = "blocked"
            pr["state_reason"] = "rework specification could not be read; state is unknown"
        elif pr.get("rework_comment_at"):
            pr["state"] = "rework-specced"
        else:
            pr["state"] = "rework-unspecced"
    elif "parked" in labels:
        pr["state"] = "parked"
    elif pr["draft"]:
        pr["state"] = "draft-unnamed"
    elif pr["readiness_status"] != "ready":
        pr["state"] = "blocked"
        pr["report_pending"] = pr["report_status"] == "unread"
        pr["state_reason"] = pr.get("readiness_reason") or {
            "unread": "gate report pending; no blocking row is available",
            "stale": "cached gate report is stale; readiness is unavailable",
            "error": "gate report read failed; cached readiness is not current",
        }.get(pr["report_status"])
    else:
        pr["state"] = "ready"
    if pr["state"] not in STATES:
        raise ValueError(f"invalid queue state for PR #{pr['number']}: {pr['state']}")
    pr["report_stale"] = pr["report_status"] == "stale"
    errors = list(pr["errors"])
    if pr.get("report_error"):
        errors.append(f"gate read failed: {pr['report_error']}")
    pr["errors"] = errors
    pr["error"] = "; ".join(errors) if errors else None


async def _git(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return 127, b"", str(exc).encode()
    pending = asyncio.create_task(proc.communicate())
    try:
        out, err = await asyncio.wait_for(asyncio.shield(pending), SUBPROCESS_TIMEOUT)
    except TimeoutError:
        proc.kill()
        out, err = await pending
        return 124, out, err + b"\ngit command timed out"
    return proc.returncode or 0, out, err


def _merge_tree_paths(output: bytes) -> list[str]:
    fields = output.split(b"\0")
    if not fields or not re.fullmatch(rb"[0-9a-fA-F]{40,64}", fields[0]):
        raise ValueError("merge-tree output omitted its result tree OID")
    try:
        end = fields.index(b"", 1)
    except ValueError as exc:
        raise ValueError("merge-tree output omitted the messages separator") from exc
    return [os.fsdecode(path) for path in fields[1:end]]


async def _conflict_paths(root: Path, repo_url: str, default_branch: str, pr: dict) -> list[str]:
    number, expected = pr["number"], pr.get("head_sha")
    if not isinstance(expected, str):
        raise ValueError("conflict check has no listed head SHA")
    parsed_url = urlsplit(repo_url)
    if parsed_url.scheme not in {"https", "file"} or parsed_url.username:
        raise ValueError("repo URL is not a credential-free HTTPS or local URL")
    if parsed_url.scheme == "https" and not parsed_url.netloc:
        raise ValueError("repo URL has no host")
    root.mkdir(parents=True, exist_ok=True)
    base_ref = f"refs/heads/{default_branch}"
    code, _, err = await _git(["git", "check-ref-format", base_ref])
    if code:
        raise ValueError(f"invalid default branch: {err.decode(errors='replace')[:300]}")
    bare = root / "scratch.git"
    if not bare.exists():
        code, _, err = await _git(["git", "init", "--bare", "--quiet", str(bare)])
        if code:
            raise ValueError(f"bare scratch init failed: {err.decode(errors='replace')[:300]}")
    code, out, err = await _git(["git", "-C", str(bare), "rev-parse", "--is-bare-repository"])
    if code or out.strip() != b"true":
        raise ValueError("scratch repository is not bare")
    pr_ref = f"refs/pull/{number}/head"
    local_head = f"refs/pr_queue/pr/{number}/head"
    code, _, err = await _git([
        "git", "-C", str(bare), "fetch", "--quiet", "--no-tags", repo_url,
        f"+{base_ref}:refs/pr_queue/base", f"+{pr_ref}:{local_head}",
    ])
    if code:
        raise ValueError(f"scratch fetch failed: {err.decode(errors='replace')[:400]}")
    code, out, err = await _git(["git", "-C", str(bare), "rev-parse", local_head])
    if code:
        raise ValueError(f"fetched PR head unavailable: {err.decode(errors='replace')[:300]}")
    fetched = out.decode().strip()
    if fetched.casefold() != expected.casefold():
        raise ValueError(f"PR head moved: listed {expected}, fetched {fetched}")
    code, out, err = await _git([
        "git", "-C", str(bare), "merge-tree", "--write-tree", "--name-only", "-z",
        "refs/pr_queue/base", expected,
    ])
    if code not in {0, 1}:
        raise ValueError(f"merge-tree failed: {err.decode(errors='replace')[:400]}")
    paths = _merge_tree_paths(out)
    if code == 1 and not paths:
        raise ValueError("merge-tree reported conflicts without conflict paths")
    return sorted(set(paths))


def _blocking_rows(pr: dict) -> list[dict]:
    gate = pr.get("gate")
    raw_rows = gate.get("raw_rows", []) if isinstance(gate, dict) else []
    return [
        row for row in raw_rows if isinstance(row, dict)
        if row.get("name", "").casefold() != "verdict"
        and re.search(r"\bBLOCK\b", row.get("value", ""), re.IGNORECASE)
    ]


def _finding_only(pr: dict) -> bool:
    gate = pr.get("gate")
    if not isinstance(gate, dict):
        return False
    match = re.fullmatch(r"([1-9][0-9]*) gate\(s\) would block", gate.get("verdict") or "")
    if not match:
        return False
    blocks = _blocking_rows(pr)
    findings = [row for row in blocks if row.get("name", "").casefold() in {
        "review-body", "inline-findings",
    }]
    count = int(match.group(1))
    return count == len(blocks) == len(findings)


def _escape(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\\", "\\\\")
    for char in ("`", "*", "_", "{", "}", "[", "]", "(", ")", "|", "<", ">"):
        text = text.replace(char, "\\" + char)
    return text


def _pr_line(pr: dict, now: datetime) -> str:
    gate = pr.get("gate")
    read_at = gate.get("read_at") if isinstance(gate, dict) else None
    meta = [f"state {pr['state']}", f"report {pr['report_status']}"]
    meta.append(
        f"gate read {read_at} (age {_age(read_at, now)})" if read_at else "gate never read"
    )
    if "parked" in pr["labels"]:
        age = pr.get("parked_comment_age")
        if age:
            meta.append(f"parked comment age {age}")
        elif pr.get("comment_read_error"):
            meta.append("parked comment age unavailable: read failed")
        else:
            meta.append("parked comment missing")
    meta.extend(pr.get("warnings", []))
    return f"- **#{pr['number']}** {_escape(pr['title'])} — {_escape('; '.join(meta))}"


def _render_markdown(data: dict, now: datetime) -> str:
    prs = data["prs"]
    lines = [
        f"# PR queue — `{_escape(data['repo'])}`", "",
        f"Generated: {data['generated_at']} (age at render: {_age(data['generated_at'], now)})",
        f"Read: {data['read']} of {data['open_total']} open PRs; limit_hit={str(data['limit_hit']).lower()}",
        "",
    ]
    shown = set()
    ready = [p for p in prs if p["state"] == "ready" and p["report_status"] == "fresh"]
    lines.extend(["## Ready for the maintainer's yes", ""])
    for pr in ready:
        lines.append(f"{_pr_line(pr, now)} — {pr['gate']['verdict']}")
        shown.add(pr["number"])
    if not ready:
        lines.append("- None")
    lines.append("")
    finding_only = [
        p for p in prs if p["state"] == "blocked" and p["report_status"] == "fresh"
        and _finding_only(p)
    ]
    lines.extend(["## Blocked only by unanswered findings", ""])
    for pr in finding_only:
        inline = pr["gate"]["rows"].get("inline-findings", "not printed")
        lines.append(f"{_pr_line(pr, now)} — inline-findings: {_escape(inline)}")
        shown.add(pr["number"])
    if not finding_only:
        lines.append("- None")
    lines.append("")
    conflicts = [p for p in prs if p["mergeable"] == "CONFLICTING"]
    conflicts.sort(key=lambda p: (len(p["conflict_files"]), p["number"]))
    lines.extend(["## Conflicting", ""])
    tally = Counter(path for pr in conflicts for path in pr["conflict_files"])
    for pr in conflicts:
        paths = ", ".join(f"`{_escape(path)}`" for path in pr["conflict_files"])
        lines.append(f"{_pr_line(pr, now)} — {paths or 'conflict files unavailable'}")
        shown.add(pr["number"])
    if not conflicts:
        lines.append("- None")
    if tally:
        lines.extend(["", "Conflict-file tally:"])
        lines.extend(f"- `{_escape(path)}` — {count} PR(s)" for path, count in sorted(tally.items()))
    lines.append("")
    drafts = [p for p in prs if p["state"] == "draft-unnamed"]
    lines.extend(["## Drafts with no named state", ""])
    for pr in drafts:
        lines.append(_pr_line(pr, now))
        shown.add(pr["number"])
    if not drafts:
        lines.append("- None")
    lines.append("")
    cutoff = now - UNTOUCHED_AGE
    untouched = [
        p for p in prs if any(
            stamp is not None and stamp < cutoff
            for stamp in (_parse_time(p.get("updated_at")), _parse_time(p.get("last_commit_at")))
        )
    ]
    untouched.sort(key=lambda p: min(
        (stamp for stamp in (_parse_time(p.get("updated_at")), _parse_time(p.get("last_commit_at")))
         if stamp is not None), default=datetime.max.replace(tzinfo=UTC)
    ))
    lines.extend(["## Untouched for more than 3 days", ""])
    for pr in untouched:
        lines.append(
            f"{_pr_line(pr, now)} — updatedAt {pr.get('updated_at') or 'unknown'} "
            f"({_age(pr.get('updated_at'), now)}); newest commit "
            f"{pr.get('last_commit_at') or 'unknown'} ({_age(pr.get('last_commit_at'), now)})"
        )
    if not untouched:
        lines.append("- None")
    lines.append("")
    failed = [p for p in prs if p.get("error")]
    lines.extend(["## Could not read", ""])
    for pr in failed:
        lines.append(f"{_pr_line(pr, now)} — {_escape(pr['error'])}")
        shown.add(pr["number"])
    for error in data.get("errors", []):
        lines.append(f"- {_escape(error)}")
    if not failed and not data.get("errors"):
        lines.append("- None")
    lines.append("")
    other = [p for p in prs if p["number"] not in shown]
    lines.extend(["## Other states", ""])
    for pr in other:
        detail = pr.get("state_reason") or pr.get("readiness_reason") or pr["state"]
        lines.append(f"{_pr_line(pr, now)} — {_escape(detail)}")
    if not other:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _replace_link(path: Path, target: str) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.symlink_to(target)
    os.replace(temp, path)


def _switch_current(root: Path, generation: str) -> None:
    _replace_link(root / "current", f"generations/{generation}")


def _prepare_public_links(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    generations = root / "generations"
    if generations.is_symlink():
        raise ValueError(f"generation directory must not be a symlink: {generations}")
    generations.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    public = [root / "worklist.json", root / "worklist.md"]
    if current.exists() and not current.is_symlink():
        raise ValueError(f"current generation path is not a symlink: {current}")
    for path in public:
        if path.exists() and not path.is_symlink():
            raise ValueError(f"worklist path is not a stable link: {path}")
        target = f"current/{path.name}"
        if path.is_symlink() and os.readlink(path) != target:
            raise ValueError(f"worklist link has an unexpected target: {path}")
        if not path.is_symlink():
            _replace_link(path, target)


def _complete_generation(folder: Path) -> bool:
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        data = json.loads((folder / "worklist.json").read_text(encoding="utf-8"))
        return data.get("schema") == 1 and (folder / "worklist.md").is_file()
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _current_generation(root: Path) -> str | None:
    current = root / "current"
    if not current.is_symlink():
        return None
    target = Path(os.readlink(current))
    if target.is_absolute() or len(target.parts) != 2 or target.parts[0] != "generations":
        return None
    folder = root / target
    try:
        if folder.resolve(strict=True).parent != (root / "generations").resolve(strict=True):
            return None
    except OSError:
        return None
    return target.parts[1] if _complete_generation(folder) else None


def _previous_complete_generation(root: Path) -> str | None:
    current = _current_generation(root)
    if current:
        return current
    generations = root / "generations"
    complete = [
        folder.name for folder in generations.iterdir()
        if _complete_generation(folder)
    ]
    return max(complete, default=None)


def _prune_generations(root: Path, keep: set[str]) -> None:
    generations = root / "generations"
    for folder in generations.iterdir():
        if folder.name in keep or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            if folder.resolve(strict=True).parent == generations.resolve(strict=True):
                shutil.rmtree(folder)
        except OSError as exc:
            print(f"PR queue sweep: abandoned generation cleanup failed: {exc}", file=sys.stderr)


def _publish(root: Path, data: dict, now: datetime) -> None:
    _prepare_public_links(root)
    previous = _previous_complete_generation(root)
    (root / "generations").mkdir(parents=True, exist_ok=True)
    generation = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    folder = root / "generations" / generation
    folder.mkdir(parents=True)
    atomic_write_text(folder / "worklist.json", json.dumps(data, indent=2, ensure_ascii=True) + "\n")
    atomic_write_text(folder / "worklist.md", _render_markdown(data, now))
    _switch_current(root, generation)
    keep = {generation}
    if previous:
        keep.add(previous)
    # This state is reconstructible from GitHub; retain one prior complete pair, not backups.
    _prune_generations(root, keep)


def _load_previous(root: Path, repo: str) -> dict:
    path = root / "worklist.json"
    if not path.exists():
        return {"schema": 1, "prs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"PR queue sweep: ignoring unreadable cache: {exc}", file=sys.stderr)
        return {"schema": 1, "prs": []}
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("prs"), list):
        print("PR queue sweep: ignoring cache with unsupported schema", file=sys.stderr)
        return {"schema": 1, "prs": []}
    if not isinstance(data.get("repo"), str) or data["repo"].casefold() != repo.casefold():
        print("PR queue sweep: ignoring cache for a different repo", file=sys.stderr)
        return {"schema": 1, "prs": []}
    return data


async def _sweep(
    root: Path, *, forced_pr: int | None = None, runner=None, now: datetime | None = None,
    resolved_repo: str | None = None,
) -> int:
    canonical = guard._canonical_public_repo()
    if not canonical:
        print("PR queue sweep: skipped; no canonical public repo configured")
        return 0
    try:
        repo = resolved_repo or await repo_pulse_gh.resolve_repo(runner=runner)
    except Exception as exc:
        repo = None
        repo_error = str(exc)
    else:
        repo_error = "repo slug resolve failed"
    if not repo:
        print(f"PR queue sweep: limit_hit=true read=0 open_total=0; {repo_error}", file=sys.stderr)
        return 1
    if repo.casefold() != canonical.casefold():
        print(f"PR queue sweep: skipped; live repo {repo} != canonical {canonical}")
        return 0
    try:
        listing = await repo_pulse_gh.list_queue_prs(repo=repo, runner=runner)
    except Exception as exc:
        listing = {"error": f"queue listing failed: {exc}", "read": 0, "open_total": 0,
                   "limit_hit": True, "repo": repo}
    if listing.get("error"):
        print(
            f"PR queue sweep: limit_hit=true read={listing.get('read', 0)} "
            f"open_total={listing.get('open_total', 0)}; {listing['error']}",
            file=sys.stderr,
        )
        return 1
    listed_repo = listing.get("repo")
    if not isinstance(listed_repo, str) or listed_repo.casefold() != repo.casefold():
        print("PR queue sweep: limit_hit=true; listing repo changed during read", file=sys.stderr)
        return 1
    started = now or datetime.now(UTC)

    def clock() -> datetime:
        return now if now is not None else datetime.now(UTC)

    previous = _load_previous(root, repo)
    old = {
        pr["number"]: pr for pr in previous.get("prs", [])
        if isinstance(pr, dict) and type(pr.get("number")) is int
    }
    raw_prs = listing.get("prs", [])
    seen = set()
    prs = []
    for raw in raw_prs:
        if not isinstance(raw, dict) or type(raw.get("number")) is not int:
            print("PR queue sweep: limit_hit=true; listing row omitted an integer number", file=sys.stderr)
            return 1
        if raw["number"] in seen:
            print(f"PR queue sweep: limit_hit=true; duplicate PR #{raw['number']} in listing", file=sys.stderr)
            return 1
        seen.add(raw["number"])
        prs.append(_api_pr(raw, old.get(raw["number"])))
    if listing.get("limit_hit") and not listing.get("error"):
        print(
            f"PR queue sweep: limit_hit=true read={listing.get('read', len(prs))} "
            f"open_total={listing.get('open_total', len(prs))}; publishing an explicitly partial list",
            file=sys.stderr,
        )
    for pr in prs:
        if pr["mergeable"] == "UNKNOWN":
            await asyncio.sleep(UNKNOWN_REREAD_DELAY)
            try:
                result = await repo_pulse_gh.read_queue_pr_mergeability(
                    repo, pr["number"], runner=runner
                )
            except Exception as exc:
                result = {"error": str(exc)}
            if result.get("error"):
                pr["errors"].append(f"mergeability reread failed: {result['error']}")
            else:
                pr["mergeable"] = result["mergeable"]
    for pr in prs:
        await _read_state_comments(pr, repo, runner, clock())
    if forced_pr is not None and not any(p["number"] == forced_pr for p in prs):
        listing.setdefault("errors", []).append(f"requested PR #{forced_pr} is not open")
    targets = []
    if forced_pr is not None:
        targets = [p for p in prs if p["number"] == forced_pr and not p["draft"]]
        if not targets and any(p["number"] == forced_pr and p["draft"] for p in prs):
            for pr in prs:
                if pr["number"] == forced_pr:
                    pr["warnings"].append("--pr refresh skipped for draft PR")
    else:
        targets = [p for p in prs if not p["draft"] and not _report_fresh(p, started)]
        targets.sort(key=_candidate_key)
        targets = targets[:REPORT_BUDGET]
    for pr in targets:
        pr["report_attempted_at"] = _stamp(clock())
        if not re.fullmatch(r"[0-9a-fA-F]{40}", pr.get("head_sha") or ""):
            pr["report_error"] = "gate report cannot be tied to a valid listed head SHA"
            continue
        result = await _run_gate(pr["number"], repo)
        report, error = _gate_report(
            pr["head_sha"], pr["updated_at"], _stamp(clock()), result
        )
        pr["report_attempt"] = report
        if error:
            pr["report_error"] = error
        else:
            pr["gate"] = report
            pr["report_error"] = None
    for pr in prs:
        if pr["mergeable"] == "CONFLICTING":
            try:
                pr["conflict_files"] = await _conflict_paths(
                    root, listing["repo_url"], listing["default_branch"], pr
                )
            except Exception as exc:  # per-PR conflict reads do not discard the queue
                pr["errors"].append(f"conflict files could not be read: {exc}")
        _state(pr, clock())
    completed = clock()
    data = {
        "schema": 1,
        "generated_at": _stamp(completed),
        "repo": repo,
        "open_total": listing.get("open_total", len(prs)),
        "read": listing.get("read", len(prs)),
        "limit_hit": bool(listing.get("limit_hit")),
        "default_branch": listing.get("default_branch"),
        "prs": sorted(prs, key=lambda p: p["number"]),
        "errors": listing.get("errors", []),
    }
    try:
        _publish(root, data, completed)
    except Exception as exc:
        print(f"PR queue sweep: publication failed; prior generation retained: {exc}", file=sys.stderr)
    return 0


def _show(root: Path) -> int:
    if not (root / "current").is_symlink():
        print("No cached PR queue worklist.")
        return 0
    try:
        with _queue_lock(root, shared=True):
            generation = _current_generation(root)
            if generation is None:
                print("No cached PR queue worklist.")
                return 0
            folder = root / "generations" / generation
            data = json.loads((folder / "worklist.json").read_text(encoding="utf-8"))
            content = (folder / "worklist.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cached PR queue worklist unreadable: {exc}", file=sys.stderr)
        return 0
    if not isinstance(data, dict):
        print("Cached PR queue worklist unreadable: invalid JSON shape", file=sys.stderr)
        return 0
    print(f"Cached worklist age: {_age(data.get('generated_at'), datetime.now(UTC))}")
    print(content, end="")
    return 0


@contextlib.contextmanager
def _queue_lock(root: Path, *, shared: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="print the cached markdown worklist")
    parser.add_argument("--pr", type=int, help="force a gate report refresh for one open PR")
    args = parser.parse_args(argv)
    if args.show:
        return _show(genesis_home() / "pr_queue")
    canonical = guard._canonical_public_repo()
    if not canonical:
        print("PR queue sweep: skipped; no canonical public repo configured")
        return 0
    try:
        repo = asyncio.run(repo_pulse_gh.resolve_repo())
    except Exception as exc:
        repo = None
        repo_error = str(exc)
    else:
        repo_error = "repo slug resolve failed"
    if not repo:
        print(f"PR queue sweep: limit_hit=true read=0 open_total=0; {repo_error}", file=sys.stderr)
        return 1
    if repo.casefold() != canonical.casefold():
        print(f"PR queue sweep: skipped; live repo {repo} != canonical {canonical}")
        return 0
    root = genesis_home() / "pr_queue"
    try:
        with _queue_lock(root):
            return asyncio.run(_sweep(root, forced_pr=args.pr, resolved_repo=repo))
    except Exception as exc:
        print(f"PR queue sweep: state or lock failure; previous worklist retained: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
