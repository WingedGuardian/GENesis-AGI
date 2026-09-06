"""PreToolUse hook: anything that touches ``secrets.env`` needs the user.

``secrets.env`` holds the credentials for Genesis's own cognitive architecture —
its routing providers, its channels, its embedding and reranking stack. Those
keys are not a general-purpose key drawer for whatever an agent decides to do;
CLAUDE.md states the principle directly ("Cognitive architecture is not a
service"). Until this hook existed, reading that file and spending a key it holds
was completely ungated: no prompt, no record, no accounting.

**Why an ASK and not a silent block.** The owner wants to KNOW when the
credentials are tapped, and to decide. Sourcing the file is legitimate often
enough (a setup script, an operator one-liner) that a hard block in a foreground
session would obstruct real work — but consequential enough that it should never
happen unseen. That is what an ``ask`` is for, and this hook is a deliberate
instance of the rare "hook that asks the user" exception rather than a drift into
asking.

**Dispatched sessions are DENIED, loudly** — see ``needs_user``.

**Matching is by RESOLVED PATH, not by command text.** The first version matched
the literal string ``secrets.env`` in a Bash command and was broken in seconds by
``cat ~/genesis/secrets.*``, ``cat s*.env`` and friends — and it never saw
``Read``/``Grep`` at all. Enumerating spellings is the hand-rolled-matcher tar
pit; ``secrets_target.touches_secrets`` instead expands globs and compares
inodes, so a spelling nobody imagined still resolves to the same file. Its two
declared residuals (shell variables, and a copy being gated only at the copy)
live in that module's docstring.

Fail-open on a malformed payload: this is a consent gate on a file access, not a
destructive-action guard, and a crashed hook must not wedge every tool call.
"""

from __future__ import annotations

import json
import os
import sys

# Self-locate so sibling hook modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, tool_input  # noqa: E402
from needs_user import decide  # noqa: E402
from secrets_target import touches_secrets  # noqa: E402

#: Fields carrying a path across the tools this hook is wired to. Read/Edit/Write
#: use `file_path`; Grep uses `path`, `pattern` AND `glob`; Glob uses `pattern`;
#: NotebookEdit uses `notebook_path`. Collected generously — a field we do not
#: read is a hole, and `glob` was exactly that: MEASURED, the first version
#: silently allowed `Grep {"pattern":"API_KEY","path":"~/genesis","glob":"secrets.env"}`,
#: which with `output_mode: "content"` returns the key VALUES. The tests only
#: exercised Bash, so nothing caught it.
_PATH_FIELDS = ("file_path", "path", "pattern", "notebook_path", "glob")


def main() -> int:
    payload = read_payload()
    ti = tool_input(payload)

    paths = [ti[f] for f in _PATH_FIELDS if isinstance(ti.get(f), str)]
    command = ti.get("command") if isinstance(ti.get("command"), str) else ""

    # Grep's `glob` is relative to its `path` (or the cwd). Checking it alone
    # only catches the case where the pattern happens to resolve from here, so
    # also offer the joined form — `path="~/genesis"` + `glob="secrets.env"` is
    # a read of the real file and must gate the same as the full path would.
    base, pat = ti.get("path"), ti.get("glob")
    if isinstance(base, str) and isinstance(pat, str) and base and pat:
        paths.append(os.path.join(os.path.expanduser(base), pat))

    if not touches_secrets(paths=paths, command=command):
        return 0

    # One line, quoted, so the owner sees WHAT is happening rather than being
    # asked to approve an abstraction. Bounded: a prompt nobody reads is a prompt
    # that gets clicked through, and the full text is in the transcript anyway.
    subject = " ".join((command or " ".join(paths)).split())
    if len(subject) > 240:
        subject = subject[:240] + " …"

    reason = (
        "GENESIS CREDENTIALS — this is not a routine approval.\n\n"
        "Something is about to access secrets.env, which holds the API keys for "
        "Genesis's own cognitive architecture (routing providers, channels, "
        "embeddings). Approving this lets it use those keys, and any spend made "
        "outside a Genesis call site is invisible to cost tracking, the budget "
        "cap, and provider-health accounting.\n\n"
        f"{'Command' if command else 'Path'}:\n  {subject}\n\n"
        "Approve only if you know why this needs the credentials. If it is an "
        "LLM call, it belongs in a routing call site instead."
    )

    print(json.dumps(decide("access secrets.env", reason, detail=subject, payload=payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
