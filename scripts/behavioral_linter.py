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
import shlex
import sys
from fnmatch import fnmatch
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
try:
    import shell_parse  # noqa: E402
    from hook_input import degraded_exit, field, read_payload, strip_quoted  # noqa: E402
except Exception:  # noqa: BLE001 — hook_input itself failed; nothing imports it back.
    if __name__ != "__main__":
        raise
    # Reverse version skew, as in the sibling guards: fail closed locally
    # because a bare exit 1 is NON-blocking to Claude Code — the linter would
    # silently stop enforcing. See hook_input.degraded_exit.
    try:
        sys.stderr.write(
            "GUARD DEGRADED (behavioral_linter): shared hook_input could not be "
            "imported; BLOCKING until the hook tree is repaired.\n"
        )
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001 — diagnostics cannot change fail direction.
        pass
    os._exit(2)

# DEGRADED-path mention set, bound ABOVE the guarded imports. It mirrors the
# provider hostnames in config/behavioral_rules/no_raw_provider_calls.yaml — a
# crude mention match standing in for a rule engine that cannot load; the
# over-block is the intended direction while the tree is broken.
_DEGRADED_GATED = (
    r"api\.(?:openai|anthropic|mistral|groq|deepinfra|x)\.com"
    r"|openrouter\.ai|generativelanguage\.googleapis\.com"
    r"|integrate\.api\.nvidia\.com"
    # Zenmux, MiniMax and Dashscope, added with their rule-file counterparts.
    # This set is a SECOND copy of that list by construction — the comment
    # above says it mirrors it — so a provider present in one and missing from
    # the other is covered while the tree is healthy and uncovered exactly when
    # it is broken, which is the moment this over-block exists for. Found by
    # checking this list after fixing the rule file, not by a reviewer.
    r"|zenmux\.ai|api\.minimaxi?\.com|dashscope\.aliyuncs\.com"
)

try:
    import yaml  # noqa: E402
except Exception as _exc:  # noqa: BLE001 — no yaml means NO rules load: same fail-open.
    if __name__ != "__main__":
        raise
    degraded_exit("behavioral_linter", gated=_DEGRADED_GATED, exc=_exc)

_RULES_DIR = Path(__file__).resolve().parent.parent / "config" / "behavioral_rules"

#: Notebook cell types whose source is PROSE, mapped to the documentation file
#: each one IS. A markdown cell is a `.md`; a raw cell is a `.txt`.
#:
#: A markdown cell documenting `https://api.openai.com/v1/chat/completions` is
#: the same artifact as that line in a `.md` file, which the provider rule
#: exempts by extension. But the payload's path ends in `.ipynb`, so the glob
#: could not see the cell and the prose was hard-blocked by the exemption
#: written to allow it — MEASURED exit 2 (Codex P2, #1826). That false positive
#: was introduced BY the commit that wired NotebookEdit here.
#:
#: **The cell is scoped as documentation, not DISCARDED.** The first fix simply
#: dropped a prose cell's source, which silently exempted it from every OTHER
#: rule too — `no-prompt-injection` declares no exclusions at all, precisely
#: because injected text in a document is the threat, so `ignore previous
#: instructions` warned in `notes.md` and went unchecked in a markdown cell.
#: MEASURED, and introduced by the fix one commit earlier (Devin, #1826). A
#: content rule must still see the text; only a rule that EXCLUDES the matching
#: documentation extension steps aside.
#:
#: Polarity is ALLOWLIST, deliberately. An absent, unknown or non-string
#: cell_type reads as CODE: wiring NotebookEdit exists to catch a provider call
#: in a cell, so the ambiguous case must fail closed. A denylist of `{"code"}`
#: would silently exempt every cell type Jupyter adds next.
_PROSE_CELL_TYPES = {"markdown": ".md", "raw": ".txt"}


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


def _applies_to(rule: dict, file_path: str, *, prose_ext: str = "") -> bool:
    """Whether a rule applies to the given file path.

    ``prose_ext`` names the documentation extension a NOTEBOOK PROSE CELL is
    equivalent to (``.md`` for markdown, ``.txt`` for raw). When set, the
    rule's ``excludes`` are additionally tested against the notebook path
    rewritten with that extension — so a rule exempting docs steps aside for a
    prose cell, while a rule with no such exemption still sees the text, and
    directory-shaped excludes like ``tests/*`` keep matching either way.

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
    if excludes and file_path:
        if _glob_match(file_path, excludes):
            return False
        if prose_ext and _glob_match(os.path.splitext(file_path)[0] + prose_ext, excludes):
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
#: `find`/`fd` were in this set until a review measured `find . -exec curl
#: -X POST <endpoint> {} +` — an -exec arm makes them executors, not searchers.
_READ_ONLY_VERBS = frozenset({"rg", "grep", "egrep", "fgrep", "ag", "ack"})

#: Anything that could turn a search into something else. The exemption applies
#: ONLY to a command with none of these: `rg foo && curl bar` is not a search,
#: a redirect makes the command WRITE, a heredoc feeds it content, a line
#: feed is itself a command separator (`rg x\ncurl y` is two commands), a bare
#: `&` backgrounds the search while a second command runs (`grep x & curl
#: <endpoint>` — Devin SEC finding, #1826), and input process substitution runs
#: an arbitrary subcommand as the search's stdin (`rg needle <(curl
#: <endpoint>)` — CodeRabbit Major, #1826).
_CHAINS = re.compile(r"(&|\|\||[\n;|`>]|<<|<\(|\$\()")

#: Flags that turn a search verb into an EXECUTOR. `rg --pre <cmd>` runs the
#: preprocessor on every matched file and `rg --hostname-bin <cmd>` runs it for
#: hyperlink hostnames; `--pager` (ag/ack) and `git grep --open-files-in-pager`
#: spawn the named program. A search carrying one is `find -exec` in a trench
#: coat (Devin SEC finding, #1826).
#: The long forms need no per-verb scoping: a subcommand that does not define
#: one refuses it outright (MEASURED: `git log --open-files-in-pager=` exits
#: "fatal: unrecognized argument").
_EXEC_FLAGS = ("--pre", "--hostname-bin", "--pager", "--open-files-in-pager")

#: Per-git-subcommand executor spellings, as a REGEX rather than a literal
#: tuple, because neither half of this flag has a fixed spelling.
#: MEASURED against this install's git:
#:   * ABBREVIATION — git's parse-options accepts any UNAMBIGUOUS prefix, so
#:     `--op=<cmd>`, `--ope=<cmd>`, `--open=<cmd>` and `--open-files=<cmd>` all
#:     RUN <cmd>. `--o=` is refused as ambiguous with `--or`, which is why the
#:     floor is `--op`. A literal tuple can only ever list one of these.
#:   * BUNDLING — `-O` may end any short cluster: `-nO<cmd>` and `-inO<cmd>`
#:     both run. A `startswith("-O")` test sees only the bare form.
#: SCOPED per subcommand because `-O` is not one flag: on `grep` it is
#: --open-files-in-pager and executes, while on `diff`/`show` it names an
#: ORDER FILE (`git diff -O/nonexistent HEAD~1` -> "fatal: failed to read
#: orderfile"). Matching `-O` everywhere would trade this fail-open for a
#: fail-closed on legitimate spellings — the same mistake in the other
#: direction. MEASURED: `git blame -O<script>` does NOT execute it, and
#: `git -c diff.external=<cmd> diff` already fails closed because `-c` is not
#: in _GIT_SEARCH_SUBCOMMANDS.
#: The cluster is `[A-Za-z0-9]`, not `[A-Za-z]`. The first version modelled
#: bundling for LETTERS, having measured `-nO` and `-inO` — and git grep's
#: context options are DIGITS (`-1`..`-9`), which cluster the same way.
#: MEASURED by marker file, in a scratch repo:
#:     -O<script>     executed    matched by the old pattern
#:     -nO<script>    executed    matched
#:     -2O<script>    executed    NOT matched   <- bypass
#:     -i2O<script>   executed    NOT matched   <- bypass
#: That is the same bundling class one character wide, found by a reviewer
#: after the commit that claimed to close it. Instance-patched here rather
#: than redesigned because the mechanism itself is dispositioned in #2230;
#: leaving a MEASURED direct spelling open while the docstring says direct
#: spellings are closed would make that docstring wrong on the day it landed.
#: ...and the cluster STOPS at `-e` or `-f`. Both take the rest of the token
#: as their argument — `-e<pattern>`, `-f<file>` — so an `O` after one of them
#: is inside a pattern, not an option. MEASURED: `git grep
#: -e2O<endpoint>` searches and executes nothing (rc=0, no marker), while the
#: widened class matched it and hard-blocked a legitimate endpoint search.
#: That is the exact cost the read-only exemption exists to avoid, and I
#: introduced it one commit earlier by widening the class without modelling
#: which short options consume their remainder. `-E`/`-F` take no argument and
#: stay in the cluster.
#:
#: This is the FOURTH spelling of this one flag to need handling — bare,
#: abbreviated, bundled-with-letters, bundled-with-digits, and now
#: not-after-an-argument-taking-option. Each was correct and each arrived after
#: the commit that claimed to close the class. That record is the argument in
#: #2230, not a reason to expect the fifth to be different.
_GIT_EXEC_PATTERNS = {"grep": re.compile(r"^--op[a-z-]*(=|$)|^-(?:(?![ef])[A-Za-z0-9])*O")}

#: The git subcommands admitted as searches at all.
_GIT_SEARCH_SUBCOMMANDS = frozenset({"log", "grep", "show", "diff", "blame"})


def _carries_exec_flag(args: list[str], pattern: re.Pattern[str] | None = None) -> bool:
    """Whether any arg is an executor flag, in any spelling it can take.

    Shared by BOTH branches of `_is_read_only_command`. It used to be inspected
    only inside the `_READ_ONLY_VERBS` branch, so the `git` branch returned
    read-only for any `git log|grep|show|diff|blame` regardless of its flags
    (CodeRabbit Major, #1826) — the executor test existed and simply was not
    reached on half the paths it was written for.

    Scanning STOPS at the first bare `--`. Everything after it is a path
    operand, and a repository may legitimately contain a file whose name looks
    like a flag. MEASURED in a scratch repo with a tracked file named
    `-Onotes`: `git grep needle -- -Onotes` searches it and executes NOTHING,
    while `git grep -O<script> needle` executes. Without the boundary the
    matcher sees `-Onotes`, revokes the exemption, and the endpoint rule hard-
    blocks a real search — a false block on the audit sessions that most need
    to grep for provider usage (Devin finding, #1826).
    """
    if "--" in args:
        args = args[: args.index("--")]
    if any(a == f or a.startswith(f + "=") for a in args for f in _EXEC_FLAGS):
        return True
    return pattern is not None and any(pattern.match(a) for a in args)


def _is_compound(command: str) -> bool:
    """Is this more than one command, or a command that writes?

    A UNION of two detectors, because each is blind exactly where the other
    sees, and using either alone regresses a case the other already covers.

    `_CHAINS` over `strip_quoted(command)` — the operator scan, with quoted
    spans removed first. Scanning the RAW text made a quoted operator revoke
    the exemption, so `rg 'a.com/v1|b.com/v2' src` — a pure search — was
    classified not-read-only and the endpoint rule hard-blocked it. MEASURED:
    the quoted `|`, `&`, `;` and `>` spellings all did this (Codex P2, #1826).

    `shell_parse` segment count — because `strip_quoted` removes a whole
    double-quoted span, and a command substitution inside double quotes is
    ACTIVE: `rg "use `curl <endpoint>`" src` really does run curl. Stripping
    hides it; the parser resolves it into a second segment.

    Neither half is sufficient. The parser does NOT split on `<(…)` or on a
    redirect (MEASURED: both come back as one segment with no redirect
    recorded), which are two of the cases `_CHAINS` was extended to catch —
    a CodeRabbit Major on this same PR, among them. So the scan stays.

    Fails CLOSED: an unparseable command or a blind spot the parser reports
    means the shape is unknown, and an unknown shape is not a search.

    MEASURED over 17 cases — 7 that must stay exempt (quoted `|`, `&`, `;`,
    `>`, a single-quoted backtick, a plain search, a plain `git grep`) and 10
    that must not (`&&`, bare `&`, pipe, process substitution, redirect,
    heredoc, newline, `;`, and the two active substitutions inside double
    quotes) — 17/17 correct.
    """
    if _CHAINS.search(strip_quoted(command)):
        return True
    try:
        segments, blind = shell_parse.analyze_checked(command)
    except Exception:  # noqa: BLE001 - an unknown shape is not a search
        return True
    return blind is not None or len(segments) > 1


def _is_read_only_command(command: str) -> bool:
    """A single search/inspect invocation with nothing chained onto it.

    Deliberately narrow: first token in the allow-list, no shell operator that
    could smuggle a call in, and no flag that makes the search itself execute a
    program. `git` is admitted only as `git log`/`git grep`/`git show`, never
    bare, because `git` also has subcommands that write.
    """
    if _is_compound(command):
        return False
    # shlex, NOT `.split()`: the guard reads the command as TYPED while the
    # shell hands the tool a de-quoted argv, so a bare split leaves the quotes
    # attached and every flag table misses them. MEASURED bypasses of the
    # split form: `rg "--pre" <cmd>` (the original Devin finding, still open
    # through this spelling), `git grep "-O<cmd>"`, and
    # `git grep "--open-files-in-pager=<cmd>"` — all execute, all were allowed.
    # shlex is what `scripts/hooks/shell_parse.py` itself tokenizes with, and
    # `_CHAINS` above has already refused anything compound, so a simple
    # command is all this has to handle. Unbalanced quoting cannot be
    # tokenized and fails CLOSED rather than falling back to a split.
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    verb = os.path.basename(parts[0])
    if verb in _READ_ONLY_VERBS:
        return not _carries_exec_flag(parts[1:])
    if verb != "git" or len(parts) < 2 or parts[1] not in _GIT_SEARCH_SUBCOMMANDS:
        return False
    # Same executor test as the branch above — the flags are scanned AFTER the
    # subcommand, and the short-form set is chosen by it.
    return not _carries_exec_flag(
        parts[2:], _GIT_EXEC_PATTERNS.get(parts[1])
    )


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
    content: str,
    rules: list[dict],
    file_path: str = "",
    *,
    bash_mode: bool = False,
    prose_ext: str = "",
) -> list[tuple[dict, dict, str]]:
    """Check content against all rules.

    Returns one ``(rule, pattern_def, severity)`` per violated rule, choosing
    the HIGHEST-severity matching pattern for that rule (so a warn-level pattern
    can never mask a block-level one — which the old first-match ``break`` did).
    """
    violations = []
    for rule in rules:
        rule_name = rule.get("name", "unnamed")

        if not _applies_to(rule, file_path, prose_ext=prose_ext):
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
    # `new_source` is NotebookEdit's content field. Wiring NotebookEdit to this
    # hook without it would have been wiring with no effect: the read below
    # would find nothing and return 0, which is the shape of a hook that looks
    # connected and checks nothing. A notebook cell is executable — a session
    # can add a cell carrying a direct provider request and run it later, and
    # neither tool call would contain a checked endpoint (Codex P1, #1826).
    content = field(payload, "content") or field(payload, "new_string")
    prose_ext = ""
    if not content:
        content = field(payload, "new_source")
        if content:
            prose_ext = _PROSE_CELL_TYPES.get(field(payload, "cell_type").lower(), "")
    bash_mode = False
    if not content:
        content = field(payload, "command")
        bash_mode = bool(content)
    if not content:
        return 0  # Nothing to check (e.g. a delete operation).

    if not tool_name:
        tool_name = "Bash" if bash_mode else "Write"

    file_path = field(payload, "file_path") or field(payload, "notebook_path")

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

    violations = _check_content(
        content, rules, file_path, bash_mode=bash_mode, prose_ext=prose_ext
    )
    if not violations:
        return 0

    return _emit(violations, file_path, tool_name)


if __name__ == "__main__":
    sys.exit(main())
