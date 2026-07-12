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
import re
from pathlib import Path
from typing import Any

from genesis.infra_profile.paths import DOC_PATH

logger = logging.getLogger(__name__)

BLOCK_NAME = "container-specs"
CLAUDE_MD_PATH = Path("~/.claude/CLAUDE.md").expanduser()

_BLOCK_RE_TEMPLATE = r"<!-- begin:{name} -->\n?.*?<!-- end:{name} -->"


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
    """Replace the named sentinel block in ``text``; append if absent."""
    block = f"<!-- begin:{name} -->\n{content}\n<!-- end:{name} -->"
    pattern = re.compile(_BLOCK_RE_TEMPLATE.format(name=re.escape(name)), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    suffix = "" if text.endswith("\n") else "\n"
    return f"{text}{suffix}{block}\n"


def update_block(
    profile: dict[str, Any],
    claude_md_path: Path | None = None,
    *,
    ignore_update_gate: bool = False,
) -> bool:
    """Rewrite the container-specs block. Returns True if the file changed.

    ``ignore_update_gate`` is for the ``--claude-md-block`` CLI, which
    update.sh itself invokes AFTER its own sed pass — sequenced, not racing.
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
