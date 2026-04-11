"""Phase 6 contribution — version gate.

Checks whether the user's fix is already present upstream. Two
layers:

1. Cheap SHA comparison: if the user's install SHA matches upstream
   HEAD, short-circuit (version_match=True, already_fixed=False).
2. LLM semantic check: feed the user's fix + the upstream commit log
   (commits between user's SHA and upstream HEAD) to a capable model
   and ask "is this fix already upstream?". Returns confidence 0-100.

The prompt was calibrated in the Phase 6 spike against 10 hand-crafted
fixtures (3 TP / 3 TN / 4 edge cases including partial fixes,
reverted fixes, different-bug-same-function, and workaround-not-fix).
The model scored 10/10 at all thresholds 60-90. We ship with a
threshold of 75 as a safety margin against future prompt-change
hedging.

Model: Claude Haiku 4.5 preferred when ANTHROPIC_API_KEY is available,
`groq/llama-3.3-70b-versatile` as the validated fallback. Chosen via
the Genesis router when runtime is available, direct
`litellm.acompletion()` otherwise (e.g. from hook contexts).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tomllib
from pathlib import Path

from .findings import VersionGateResult

logger = logging.getLogger(__name__)

# Confidence threshold for cancelling a contribution. Calibrated in the
# spike. Lower = more cancellations; higher = more duplicate PRs.
CONFIDENCE_THRESHOLD = 75

# Preferred model chain (first available wins). Production can override
# by passing `model=...` to `check_version_gate()`.
_PREFERRED_MODELS = [
    ("anthropic/claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("groq/llama-3.3-70b-versatile", "GROQ_API_KEY"),
    ("gemini/gemini-2.0-flash", "GEMINI_API_KEY"),
]

# Copied VERBATIM from scripts/spike_version_gate_calibrate.py after
# 10/10 validation. Do NOT re-engineer without re-running the spike.
VERSION_GATE_PROMPT = """You are the Phase 6 version gate for Genesis, an autonomous AI agent system.

A user of Genesis has just committed a bug fix to their local install. Before \
their fix gets contributed upstream to the public repo, you must decide: is \
this bug already fixed in the upstream commits the user doesn't yet have?

If YES → cancel the contribution (would duplicate an existing fix).
If NO → proceed with the contribution (real new value for the community).

Be STRICT. False positives (saying "already fixed" when it isn't) cause \
valid community contributions to be dropped — this is the worse failure mode. \
False negatives (saying "not fixed" when it is) cause a duplicate PR that a \
maintainer can close in seconds. When in doubt, say NO.

# The user's fix

Subject: {user_subject}

Body:
{user_body}

Diff:
```
{user_diff}
```

# Upstream commits the user doesn't have

(From their install version up to upstream HEAD. If empty, user is current.)

{upstream_log}

# Your task

Analyze whether the user's fix is already present upstream. Consider:

1. Does ANY upstream commit address the SAME root cause as the user's fix?
2. Would the bug the user is fixing still exist at upstream HEAD?
3. Watch for: reverts of upstream fixes, partial fixes, workarounds that \
don't address the root cause, fixes in the same function but for a different \
bug, and scope differences (user's fix covers more than upstream's).

Respond with ONLY a JSON object matching this schema, no prose before or \
after:

{{
  "already_fixed": true | false,
  "confidence": 0-100,
  "matched_commit_sha": "<sha or null>",
  "reasoning": "<one sentence>"
}}
"""


def read_install_sha(repo_path: Path | None = None) -> str | None:
    """Read the install's source commit SHA.

    Looks for `.genesis-source-commit` (written by
    `scripts/prepare-public-release.sh` and shipped in the public
    distribution). Falls back to `git rev-parse HEAD` if the file is
    missing (developer / private-repo case).
    """
    root = repo_path or Path.cwd()
    marker = root / ".genesis-source-commit"
    if marker.is_file():
        content = marker.read_text(encoding="utf-8").strip()
        if content:
            return content.split()[0]  # defensive: ignore trailing junk

    # Fall back to HEAD
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def read_install_version(repo_path: Path | None = None) -> str:
    """Read the install's pyproject.toml version, fallback to 'unknown'."""
    root = repo_path or Path.cwd()
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return "unknown"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return "unknown"
    return str(data.get("project", {}).get("version", "unknown"))


def format_version_string(repo_path: Path | None = None) -> str:
    """Return a display string like '3.0.0a1@abc1234'."""
    version = read_install_version(repo_path)
    sha = read_install_sha(repo_path)
    short = (sha[:7] if sha else "unknown")
    return f"{version}@{short}"


_SHA_RE_VG = re.compile(r"^[0-9a-fA-F]{4,40}$")


def fetch_upstream_log(
    user_sha: str,
    upstream_sha: str,
    *,
    repo_path: Path | None = None,
    max_commits: int = 50,
) -> list[dict]:
    """Fetch the commit log between user_sha and upstream_sha.

    Returns a list of `{sha, subject, body}` dicts. Uses a delimiter
    that can't occur in commit messages to parse safely. Rejects
    non-SHA inputs to prevent `--option` injection (P2-1).
    """
    if user_sha == upstream_sha:
        return []
    if not _SHA_RE_VG.match(user_sha) or not _SHA_RE_VG.match(upstream_sha):
        logger.error(
            "version_gate: refusing fetch_upstream_log with non-SHA input: %r..%r",
            user_sha, upstream_sha,
        )
        return []

    # Record separator (RS) between fields, record terminator (GS)
    # between records. Both are ASCII control bytes that cannot appear
    # in a commit message via normal tooling. Previously we used
    # `%b<RS>%n` and split on newline, which truncated multi-line
    # commit bodies to their first line before the LLM ever saw them
    # (codex review P2 finding). Now we delimit records with GS
    # explicitly so multi-line bodies survive round-trip.
    field_sep = "\x1e"   # ASCII RS, between fields
    record_sep = "\x1d"  # ASCII GS, between records
    fmt = f"%H{field_sep}%s{field_sep}%b{record_sep}"
    try:
        proc = subprocess.run(
            [
                "git", "log",
                f"--max-count={max_commits}",
                f"--format={fmt}",
                f"{user_sha}..{upstream_sha}",
                "--",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_path) if repo_path else None,
            timeout=15,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []

    commits: list[dict] = []
    for raw in proc.stdout.split(record_sep):
        record = raw.strip("\n")
        if not record:
            continue
        parts = record.split(field_sep)
        if len(parts) < 3:
            continue
        sha, subject, body = parts[0].strip(), parts[1], parts[2]
        if not sha:
            continue
        commits.append({"sha": sha[:12], "subject": subject, "body": body.strip()})
    return commits


def _format_upstream_log(commits: list[dict]) -> str:
    if not commits:
        return "(empty — user is at upstream HEAD)"
    lines = []
    for c in commits:
        sha = c.get("sha", "?")
        subj = c.get("subject", "")
        body = c.get("body", "").strip()
        entry = f"  {sha}  {subj}"
        if body:
            for bl in body.split("\n"):
                entry += f"\n    {bl}"
        lines.append(entry)
    return "\n".join(lines)


def build_prompt(
    user_subject: str,
    user_body: str,
    user_diff: str,
    upstream_commits: list[dict],
) -> str:
    return VERSION_GATE_PROMPT.format(
        user_subject=user_subject,
        user_body=user_body,
        user_diff=user_diff,
        upstream_log=_format_upstream_log(upstream_commits),
    )


def parse_llm_response(text: str) -> tuple[bool, dict]:
    """Extract the JSON verdict from the LLM response.

    Returns (ok, parsed). Accepts ```json fences and raw JSON.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return False, {}
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return False, {}

    if "already_fixed" not in obj or "confidence" not in obj:
        return False, {}
    return True, obj


def _select_model(override: str | None) -> str | None:
    if override:
        return override
    for model, env_var in _PREFERRED_MODELS:
        if os.environ.get(env_var):
            return model
    return None


async def _call_llm(prompt: str, model: str) -> str:
    """Call the LLM via litellm.acompletion. Surfaces litellm errors."""
    import litellm  # lazy import; keeps findings/identity light
    response = await litellm.acompletion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=500,
    )
    return response.choices[0].message.content or ""


async def check_version_gate(
    *,
    user_subject: str,
    user_body: str,
    user_diff: str,
    user_sha: str,
    upstream_sha: str,
    repo_path: Path | None = None,
    model: str | None = None,
    threshold: int = CONFIDENCE_THRESHOLD,
) -> VersionGateResult:
    """Run the version gate. Returns a VersionGateResult.

    `already_fixed=True` iff the LLM says so AND confidence >= threshold.
    On LLM failure or parse error, defaults to `already_fixed=False`
    (fail-open: let the contribution proceed, surface the error).
    """
    # Layer 1: SHA short-circuit
    if user_sha and upstream_sha and user_sha == upstream_sha:
        return VersionGateResult(
            already_fixed=False,
            confidence=0,
            matched_sha=None,
            reasoning="Install is at upstream HEAD — no upstream commits to check.",
            version_match=True,
            upstream_commit_count=0,
            parse_ok=True,
        )

    upstream_commits = fetch_upstream_log(
        user_sha, upstream_sha, repo_path=repo_path,
    )

    if not upstream_commits:
        return VersionGateResult(
            already_fixed=False,
            confidence=0,
            matched_sha=None,
            reasoning="No upstream commits between install and HEAD (or log fetch failed).",
            version_match=(user_sha == upstream_sha),
            upstream_commit_count=0,
            parse_ok=True,
        )

    chosen_model = _select_model(model)
    if chosen_model is None:
        logger.error("version_gate: no LLM API key available in env; fail-open")
        return VersionGateResult(
            already_fixed=False,
            confidence=0,
            reasoning="No LLM API key configured — version gate skipped.",
            upstream_commit_count=len(upstream_commits),
            parse_ok=False,
            llm_error="no_api_key",
        )

    prompt = build_prompt(user_subject, user_body, user_diff, upstream_commits)

    try:
        raw = await _call_llm(prompt, chosen_model)
    except Exception as e:  # noqa: BLE001 — fail-open on any litellm error
        logger.error("version_gate: LLM call failed: %s", e, exc_info=True)
        return VersionGateResult(
            already_fixed=False,
            confidence=0,
            reasoning=f"LLM call failed: {e}",
            upstream_commit_count=len(upstream_commits),
            parse_ok=False,
            llm_error=str(e),
        )

    ok, parsed = parse_llm_response(raw)
    if not ok:
        logger.error("version_gate: failed to parse LLM response: %r", raw[:200])
        return VersionGateResult(
            already_fixed=False,
            confidence=0,
            reasoning="LLM response was unparseable.",
            upstream_commit_count=len(upstream_commits),
            parse_ok=False,
            llm_error="parse_error",
        )

    llm_says_fixed = bool(parsed.get("already_fixed", False))
    confidence = int(parsed.get("confidence", 0))
    matched_sha = parsed.get("matched_commit_sha")
    reasoning = parsed.get("reasoning", "")

    # Threshold gate: we only cancel if BOTH the LLM says fixed AND
    # confidence clears the bar. This is the safety margin that keeps
    # false positives (valid contributions dropped) rare.
    effective_fixed = llm_says_fixed and confidence >= threshold

    return VersionGateResult(
        already_fixed=effective_fixed,
        confidence=confidence,
        matched_sha=matched_sha if matched_sha else None,
        reasoning=reasoning,
        version_match=False,
        upstream_commit_count=len(upstream_commits),
        parse_ok=True,
    )
