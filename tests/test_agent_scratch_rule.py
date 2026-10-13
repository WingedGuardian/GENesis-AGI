"""Every subagent definition carries the scratch-files rule (#2682).

Subagents usually inherit Claude Code's shared working temp as their default
temp location, and the harness also points them at a "scratchpad directory" on
that same temp. A subagent that writes bulk files there (``tempfile``,
``mktemp``, test runs) can fill it, by bytes or by inodes, and break every
session at once. They start without the parent's instructions, so the rule has
to live in each agent definition.

Polarity is ALLOWLIST: every ``.claude/agents/**/*.md`` must carry the section,
so a new agent definition fails here until it does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_AGENTS = Path(__file__).resolve().parent.parent / ".claude" / "agents"
_FILES = sorted(_AGENTS.rglob("*.md"))


def test_the_agent_directory_is_found():
    # Guard the guard: an empty glob would make every check below vacuous.
    assert len(_FILES) >= 5, _FILES


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_agent_carries_the_scratch_rule(path):
    text = path.read_text()
    m = re.search(r"^name:\s*(\S+)", text, re.M)
    assert m, f"{path.name}: no frontmatter name"
    name = m.group(1)
    assert "<!-- scratch-rule -->" in text, f"{path.name}: no scratch-rule section"
    section = text.split("<!-- scratch-rule -->", 1)[1]
    # The load-bearing parts: a per-run directory under ~/tmp named for the
    # agent (created even on an install without ~/tmp), explicit-directory use,
    # the harness scratchpad overridden, no persistent TMPDIR change, capped
    # bulk reproductions.
    assert f"mkdir -p ~/tmp && mktemp -d -p ~/tmp {name}-XXXX" in section
    assert "never rely on" in section and "default temp location" in section
    assert '"scratchpad directory"' in section
    assert "Never export or persistently change `TMPDIR`" in section
    assert "caps the count and size" in section
