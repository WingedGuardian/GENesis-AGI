#!/usr/bin/env python3
"""Behavioral linter — enforces anti-pattern rules on Write/Edit and Bash.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads the CC hook payload from stdin (via hook_input), loads all rule YAML files from
config/behavioral_rules/, and checks the content being written.

**Bash is checked too, but only by rules that opt in** (``check_bash: true``).

Why the surface had to widen: a rule wired to Write|Edit sees the file-writing
tools and nothing else, so the same forbidden code slips through unchanged as
``cat > x.py <<EOF`` or ``python3 -c "..."``. That is not a hypothetical shape —
it is precisely the shape the no-raw-provider-calls incident took (2026-09-06),
where the offending script never passed through Write at all.

Why opt-in rather than blanket: a rule's patterns are written against SOURCE, and
shell text is a different language. Applying every rule to every command trades a
known hole for an unknown false-positive surface. A rule declares ``check_bash``
after its author has measured the fire rate on real commands, and that
measurement belongs in the rule file next to the flag.

Declared residual: a Bash payload carries no ``file_path``, so a rule's
``excludes`` path globs cannot apply to it. A heredoc writing INTO an excluded
path (say the routing layer itself) is therefore checked where the equivalent
Write would have been skipped. Resolving a redirect target out of shell text is
the hand-rolled-parser tar pit; the escape-hatch comment covers the rare case.

Exit codes:
  0 — allow (no rule violations, or only warnings)
  2 — block (a rule with severity=block matched)

Escape hatch: Add a comment containing 'behavioral-lint: ignore <rule-name>'
in the content to suppress a specific rule for that file. This leaves an
audit trail — the user approved the exception.

Emits SteerMessage for unified enforcement feedback.
"""

import json
import os
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

import yaml

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload  # noqa: E402

_RULES_DIR = Path(__file__).resolve().parent.parent / "config" / "behavioral_rules"


def _load_rules() -> list[dict]:
    """Load all rule YAML files from the behavioral_rules directory."""
    rules = []
    if not _RULES_DIR.is_dir():
        return rules
    for f in sorted(_RULES_DIR.glob("*.yaml")):
        try:
            rule = yaml.safe_load(f.read_text())
            if rule and isinstance(rule, dict) and "patterns" in rule:
                rules.append(rule)
        except Exception as exc:
            print(f"WARNING: Failed to load behavioral rule {f.name}: {exc}", file=sys.stderr)
    return rules


def _severity_of(rule: dict, pattern_def: dict) -> str:
    """Resolve a pattern's severity, falling back to the rule-level default.

    Per-pattern ``severity`` lets one rule mix hard-block code patterns with
    advisory (warn) prose patterns. Anything other than "block" resolves to
    "warn" (fail toward the softer decision).
    """
    sev = pattern_def.get("severity") or rule.get("severity", "warn")
    return "block" if sev == "block" else "warn"


def _glob_match(file_path: str, globs: list) -> bool:
    """Whether ``file_path`` matches any glob (full normalized path or basename)."""
    name = file_path.replace("\\", "/")
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch(name, g) or fnmatch(base, g) for g in globs)


def _applies_to(rule: dict, file_path: str) -> bool:
    """Whether a rule applies to the given file path.

    A rule may declare ``applies_to`` (allow-list) and/or ``excludes``
    (deny-list) as lists of globs:
    - ``excludes`` wins: a file matching an exclude glob is never checked. Use
      this for a code rule that should fire everywhere EXCEPT docs (safer than
      an allow-list, which silently misses extensionless scripts / notebooks /
      templates that carry real code).
    - ``applies_to``: when present, the rule fires only for matching files.
    Absent both → applies to all. An empty/absent file_path keeps the rule
    active (fail toward checking).
    """
    excludes = rule.get("excludes")
    if excludes and file_path and _glob_match(file_path, excludes):
        return False
    globs = rule.get("applies_to")
    if not globs:
        return True
    if not file_path:
        return True
    return _glob_match(file_path, globs)


#: Commands that can only READ. Searching a codebase for a provider endpoint is
#: indistinguishable, by regex, from calling one — `rg -n '<endpoint>' src/` and
#: `git log -S'<endpoint>'` both carry the literal. That lands hardest on the
#: audit and review sessions which most need to grep for provider usage, and a
#: `severity: block` rule there obstructs the work rather than the violation.
#:
#: SEARCH verbs only. `cat`/`head`/`tail`/`ls` were in this set for one revision
#: and it broke the acceptance bar immediately: `cat > probe.py <<'EOF' … EOF` is
#: the origin incident's literal shape and starts with `cat`. A verb that writes
#: when you point it at a redirect is not a read-only verb, and the lesson
#: generalises — the exemption is for the small set of commands that cannot
#: produce a network call, not for commands that usually don't.
_READ_ONLY_VERBS = frozenset({"rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd"})

#: Anything that could turn a search into something else. The exemption applies
#: ONLY to a command with none of these: `rg foo && curl bar` is not a search,
#: a redirect makes the command WRITE, and a heredoc feeds it content.
_CHAINS = re.compile(r"(&&|\|\||[;|`>]|<<|\$\()")


def _is_read_only_command(command: str) -> bool:
    """A single search/inspect invocation with nothing chained onto it.

    Deliberately narrow: first token in the allow-list AND no shell operator that
    could smuggle a call in. `git` is admitted only as `git log`/`git grep`/
    `git show`, never bare, because `git` also has subcommands that write.
    """
    if _CHAINS.search(command):
        return False
    parts = command.strip().split()
    if not parts:
        return False
    verb = os.path.basename(parts[0])
    if verb in _READ_ONLY_VERBS:
        return True
    return verb == "git" and len(parts) > 1 and parts[1] in {"log", "grep", "show", "diff", "blame"}


def _escaped(content: str, rule_name: str, *, bash_mode: bool) -> bool:
    """Whether the opt-out comment disarms ``rule_name`` for this content.

    On the Write path ``content`` is one file's body, so a bare substring test is
    right: the token is a comment the author put in that file.

    On the Bash path ``content`` is a whole compound command, and a substring test
    is a BYPASS — any mention anywhere disarms the rule for everything else on the
    line. MEASURED: ``git commit -m 'doc the behavioral-lint: ignore
    no-raw-provider-calls hatch' && curl -X POST <provider>/chat/completions``
    exits 0 against a ``severity: block`` rule, and that is a plausible accident
    rather than an attack — documenting the hatch silently switches it on.
    So a command must carry the token as a TRAILING comment, which is the form
    the emitted ``suppress_key`` already advertises.
    """
    token = f"behavioral-lint: ignore {rule_name}"
    if not bash_mode:
        return token in content
    return re.search(rf"#\s*{re.escape(token)}\s*$", content.strip()) is not None


def _check_content(
    content: str, rules: list[dict], file_path: str = "", *, bash_mode: bool = False
) -> list[tuple[dict, dict, str]]:
    """Check content against all rules.

    Returns one ``(rule, pattern_def, severity)`` per violated rule, choosing
    the HIGHEST-severity matching pattern for that rule (so a warn-level pattern
    can never mask a block-level one — which the old first-match ``break`` did).
    """
    violations = []
    for rule in rules:
        rule_name = rule.get("name", "unnamed")

        if not _applies_to(rule, file_path):
            continue

        # Escape hatch: an explicit opt-out comment turns off the whole rule.
        if _escaped(content, rule_name, bash_mode=bash_mode):
            continue

        best: tuple[dict, dict, str] | None = None
        best_rank = -1
        for pattern_def in rule.get("patterns", []):
            regex = pattern_def.get("regex", "")
            if not regex:
                continue
            try:
                matched = re.search(regex, content, re.IGNORECASE | re.MULTILINE)
            except re.error:
                print(f"WARNING: Invalid regex in rule {rule_name}: {regex}", file=sys.stderr)
                continue
            if not matched:
                continue
            severity = _severity_of(rule, pattern_def)
            rank = 2 if severity == "block" else 0
            if rank > best_rank:
                best = (rule, pattern_def, severity)
                best_rank = rank
                if rank == 2:
                    break  # block is the max — no pattern can outrank it
        if best is not None:
            violations.append(best)
    return violations


def _plain_stderr(rule: dict, pattern_def: dict, severity: str, name: str, file_path: str) -> str:
    """Format a violation without the genesis package (fresh-install fallback).

    Mirrors SteerMessage.to_stderr()'s shape so downstream text assertions and
    the human-readable format stay stable when genesis isn't importable.
    """
    label = "BLOCKED" if severity == "block" else "WARNING"
    lines = [f"\n{label}: Behavioral rule '{name}' violated", f"  Rule: {name}"]
    if file_path:
        lines.append(f"  File: {file_path}")
    if pattern_def.get("context"):
        lines.append(f"  Issue: {pattern_def['context']}")
    fix = rule.get("description", "")
    if rule.get("suggestion"):
        fix += "\n  " + rule["suggestion"]
    if fix:
        lines.append(f"  Fix: {fix}")
    lines.append(f"  Escape: Add '# behavioral-lint: ignore {name}' if user-approved")
    return "\n".join(lines) + "\n"


def _emit(
    violations: list[tuple[dict, dict, str]], file_path: str, tool_name: str = "Write"
) -> int:
    """Print each violation to stderr; return the max exit code (2 = block).

    Prefers SteerMessage formatting, but if the genesis package isn't importable
    (a fresh/partial install), falls back to plain text that STILL returns the
    correct exit code — so a block never silently degrades to a non-blocking
    error just because genesis wasn't on the path.
    """
    try:
        from genesis.autonomy.steering import SteerMessage
        from genesis.autonomy.types import ApprovalDecision, EnforcementLayer

        use_steer = True
    except Exception:
        use_steer = False

    exit_code = 0
    block_texts: list[str] = []
    warn_texts: list[str] = []
    for rule, pattern_def, severity in violations:
        name = rule.get("name", "unnamed")
        is_block = severity == "block"
        if use_steer:
            msg = SteerMessage(
                layer=EnforcementLayer.HARD_BLOCK,
                rule_id=name,
                decision=ApprovalDecision.BLOCK if is_block else ApprovalDecision.ACT,
                severity="critical" if is_block else "medium",
                title=f"Behavioral rule '{name}' violated",
                context=pattern_def.get("context", ""),
                suggestion=rule.get("description", "")
                + ("\n  " + rule.get("suggestion", "") if rule.get("suggestion") else ""),
                tool_name=tool_name,
                file_path=file_path,
                can_suppress=True,
                suppress_key=f"# behavioral-lint: ignore {name}",
            )
            text = msg.to_stderr()
            code = msg.to_exit_code()
        else:
            text = _plain_stderr(rule, pattern_def, severity, name, file_path)
            code = 2 if is_block else 0
        (block_texts if is_block else warn_texts).append(text)
        if code > exit_code:
            exit_code = code

    if exit_code == 2:
        # A block fired: exit 2 delivers stderr to the model, so surface every
        # message (blocks and any warns) there.
        for text in block_texts + warn_texts:
            print(text, file=sys.stderr)
    elif warn_texts:
        # Warn-only: PreToolUse stderr on exit 0 is DISCARDED by Claude Code, so
        # the advisory must ride hookSpecificOutput.additionalContext (stdout),
        # which is delivered to the model.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "\n".join(warn_texts),
                    }
                }
            )
        )
    return exit_code


def main() -> int:
    payload = read_payload()

    tool_name = payload.get("tool_name") if isinstance(payload, dict) else ""
    tool_name = tool_name if isinstance(tool_name, str) else ""

    # What is being checked, and under which contract.
    #
    # Write/Edit  -> the file content. Every rule applies (the historical path).
    # Bash        -> the command text. ONLY rules that opted in via
    #                ``check_bash: true`` apply — see the module docstring.
    #
    # Decided by the FIELD present, not by tool_name alone: the legacy env-var
    # payload contract carries no tool_name at all, and a hook that went silent
    # under one of the two contracts is the exact failure hook_input exists to
    # prevent. tool_name is used only to label the message.
    content = field(payload, "content") or field(payload, "new_string")
    bash_mode = False
    if not content:
        content = field(payload, "command")
        bash_mode = bool(content)
    if not content:
        return 0  # Nothing to check (e.g. a delete operation).

    if not tool_name:
        tool_name = "Bash" if bash_mode else "Write"

    file_path = field(payload, "file_path")

    # Never lint the rule-definition files themselves: they necessarily contain
    # the very patterns they match (the kill-all call literals, the hide-on-
    # error CSS), so self-linting hard-blocks every edit to this directory — a
    # #1227 side effect once the hook gained teeth. Editing the rules is how the
    # rules get fixed; it must not require the escape hatch in each file. Match
    # the ACTUAL rules dir by resolved path (not a bare substring, which would
    # also skip an unrelated */config/behavioral_rules/* path elsewhere).
    if file_path:
        try:
            if _RULES_DIR.resolve() in Path(file_path).resolve().parents:
                return 0
        except (OSError, ValueError, RuntimeError):
            pass

    rules = _load_rules()
    if bash_mode:
        if _is_read_only_command(content):
            return 0
        rules = [r for r in rules if r.get("check_bash") is True]
    if not rules:
        return 0

    violations = _check_content(content, rules, file_path, bash_mode=bash_mode)
    if not violations:
        return 0

    return _emit(violations, file_path, tool_name)


if __name__ == "__main__":
    sys.exit(main())
