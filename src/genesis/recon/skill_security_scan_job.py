"""Scheduled skill-security scan recon job.

Wraps the skill_scan logic as a SurplusScheduler job (mirrors
``ModelIntelligenceJob``): resolve the SkillSpector binary, scan installed
skills, file findings for UNTRUSTED skills only, and report a summary. If
SkillSpector isn't installed it skips gracefully (never crashes the scheduler).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from genesis.security.skill_scan import (
    _default_roots,
    _default_trusted_file,
    _load_trusted_names,
    _repo_skill_roots,
    discover_skill_dirs,
    is_trusted,
    run_skillspector,
    scan_and_store,
    store_finding,
)

logger = logging.getLogger(__name__)

# Stable install path the bootstrap uses (see scripts/bootstrap.sh).
_INSTALL_BIN = Path.home() / ".genesis" / "deps" / "skillspector" / ".venv" / "bin" / "skillspector"


def _known_install_bin() -> str | None:
    return str(_INSTALL_BIN) if _INSTALL_BIN.is_file() else None


class SkillSecurityScanJob:
    """Weekly scan of installed skills via NVIDIA SkillSpector → recon findings."""

    def __init__(self, *, db: object, skillspector_bin: str | None = None) -> None:
        self._db = db
        self._bin = skillspector_bin

    def _resolve_bin(self) -> str | None:
        """Explicit bin → SKILLSPECTOR_BIN env → bootstrap install path → PATH."""
        return (
            self._bin
            or os.environ.get("SKILLSPECTOR_BIN")
            or _known_install_bin()
            or shutil.which("skillspector")
        )

    async def run(self) -> dict:
        binary = self._resolve_bin()
        if not binary:
            logger.warning("SkillSpector binary not found — skipping skill-security scan")
            return {"total_findings": 0, "skills_scanned": 0, "skipped": "skillspector not installed"}

        roots = _default_roots()
        skill_dirs = discover_skill_dirs(roots)
        trusted_names = _load_trusted_names(_default_trusted_file())
        trusted_roots = _repo_skill_roots()

        def trusted(skill_dir: Path) -> bool:
            return is_trusted(skill_dir, trusted_names=trusted_names, trusted_roots=trusted_roots)

        def scanner(skill_dir: Path) -> dict | None:
            return run_skillspector(skill_dir, skillspector_bin=binary, no_llm=True)

        results = await scan_and_store(
            self._db, skill_dirs, scanner=scanner, storer=store_finding, trusted=trusted
        )
        filed = sum(1 for r in results if r.score >= 1 and not trusted(Path(r.source)))
        logger.info(
            "Skill-security scan: %d skills scanned, %d untrusted findings filed",
            len(results),
            filed,
        )
        return {"total_findings": filed, "skills_scanned": len(results)}
