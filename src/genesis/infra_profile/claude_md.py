"""Project headline facts into the user-level CLAUDE.md `container-specs` block.

Same sentinel-marker convention as ``scripts/lib/claude_md_blocks.sh``
(`<!-- begin:NAME --> … <!-- end:NAME -->`); content ownership for THIS block
lives here (one content owner per block — the shell lib owns network-identity).

Deliberately light imports: ``python -m genesis.infra_profile --claude-md-block``
runs from ``scripts/update.sh`` during deploys and must not pull the collector
or runtime graphs.

Safety rails (architect review 2026-07-11):
- gated on ``env.update_in_progress()`` — never race update.sh's own sed
- profile.json missing → leave the existing block (installer placeholder) alone
- byte-identical content → no rewrite
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from genesis.infra_profile.paths import DOC_PATH

logger = logging.getLogger(__name__)

BLOCK_NAME = "container-specs"
CLAUDE_MD_PATH = Path("~/.claude/CLAUDE.md").expanduser()


# POSIX [[:space:]] in the C locale: what the shell writer's awk trims.
_POSIX_SPACE = " \t\n\v\f\r"


class MalformedBlockError(ValueError):
    """The file's markers for a block are unbalanced; it was left untouched."""


def build_block_content(profile: dict[str, Any]) -> str:
    """Render the block body (heading + bullets), without markers."""
    from genesis.infra_profile.render import headline_facts

    lines = ["## Container"]
    for key, value in headline_facts(profile).items():
        label = key.replace("_", " ").title().replace("Cpu", "CPU").replace("Sqlite", "SQLite")
        lines.append(f"- **{label}**: {value}")
    lines.append(
        f"- **Full profile**: `{DOC_PATH}` (programmatically maintained — "
        "consult it for any infrastructure-adjacent work)",
    )
    return "\n".join(lines)


def _replace_block(text: str, name: str, content: str) -> str:
    """Replace the named sentinel block in ``text``; append if absent.

    The same rule as ``write_sentinel_block`` in
    ``scripts/lib/claude_md_blocks.sh``, and one fixture table
    (``tests/test_scripts/test_claude_md_blocks.py``) drives both: a marker is
    a WHOLE line (surrounding whitespace and a CR ignored); every well-formed
    pair is removed and one fresh block takes the FIRST pair's place, so a
    duplicated block heals; a begin with no end, an end with no begin, or a
    begin inside an open block raises :class:`MalformedBlockError` and nothing
    is written. The regex this replaces left an unmatched begin in place and
    appended a new block, and its next run deleted everything between the two.
    """
    begin, end = f"<!-- begin:{name} -->", f"<!-- end:{name} -->"
    block = f"{begin}\n{content}\n{end}"
    # Split on "\n" ONLY and trim exactly POSIX [[:space:]], as awk does:
    # str.splitlines() also splits on \v, \f, \x1c-\x1e, \x85, U+2028/9 and a
    # lone \r, and str.strip() removes Unicode whitespace, so either one made
    # this writer accept (and delete through) a shape the shell writer refuses.
    lines = text.split("\n")
    lines = [f"{line}\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    kept: list[str] = []
    insert_at: int | None = None
    start: int | None = None
    for number, line in enumerate(lines, start=1):
        marker = line.strip(_POSIX_SPACE)
        if marker == begin:
            if start is not None:
                raise MalformedBlockError(
                    f"a second begin at line {number} inside the block opened at line {start}"
                )
            start = number
            if insert_at is None:
                insert_at = len(kept)
            continue
        if marker == end:
            if start is None:
                raise MalformedBlockError(f"an end at line {number} with no begin before it")
            start = None
            continue
        if start is None:
            kept.append(line)
    if start is not None:
        raise MalformedBlockError(f"the begin at line {start} has no end")
    if insert_at is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{suffix}{block}\n"
    kept.insert(insert_at, f"{block}\n")
    return "".join(kept)


def update_block(
    profile: dict[str, Any],
    claude_md_path: Path | None = None,
    *,
    ignore_update_gate: bool = False,
) -> bool:
    """Rewrite the container-specs block. Returns True if the file changed.

    Raises :class:`MalformedBlockError` (file untouched) when the block's
    markers are unbalanced, so a caller can say so rather than read it as
    "nothing to change".

    ``ignore_update_gate`` is for the ``--claude-md-block`` CLI, which
    update.sh itself invokes AFTER its own block writer — sequenced, not racing.
    The gate protects only the runtime-side refresh path.
    """
    from genesis.env import update_in_progress
    from genesis.util.atomic import atomic_write_text

    if claude_md_path is None:  # call-time resolution — see store.py rationale
        claude_md_path = CLAUDE_MD_PATH
    if not profile.get("sections"):
        logger.debug("infra_profile: no profile yet — leaving CLAUDE.md block alone")
        return False
    if not ignore_update_gate and update_in_progress():
        logger.info("infra_profile: deploy in progress — skipping CLAUDE.md rewrite")
        return False
    if not claude_md_path.exists():
        # Seeding the file is the installer's job (host-setup.sh/update.sh).
        logger.debug("infra_profile: %s missing — not seeding", claude_md_path)
        return False

    original = claude_md_path.read_text()
    updated = _replace_block(original, BLOCK_NAME, build_block_content(profile))
    if updated == original:
        return False
    atomic_write_text(claude_md_path, updated)
    logger.info("infra_profile: refreshed %s block in %s", BLOCK_NAME, claude_md_path)
    return True
