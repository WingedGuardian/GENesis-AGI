"""Tests for the SkillSpector wrapper (skill-security scan → recon findings)."""

from __future__ import annotations

from pathlib import Path

from genesis.security.skill_scan import (
    ScanResult,
    discover_skill_dirs,
    parse_report,
    report_to_finding,
    scan_and_store,
    severity_to_priority,
    store_finding,
)

# A representative SkillSpector JSON report (the contract: skill / risk_assessment /
# components / issues / metadata), modeled on a real `skillspector scan -f json` output.
SAMPLE_REPORT = {
    "skill": {
        "name": "demo-skill",
        "source": "/skills/demo-skill",
        "scanned_at": "2026-06-21T00:00:00Z",
    },
    "risk_assessment": {"score": 78, "severity": "HIGH", "recommendation": "DO NOT INSTALL"},
    "components": [
        {"path": "scripts/sync.py", "type": "python", "lines": 87, "executable": True, "size_bytes": 2000},
    ],
    "issues": [
        {
            "id": "E2",
            "category": "Data Exfiltration",
            "pattern": "Env Variable Harvesting",
            "severity": "HIGH",
            "confidence": 0.94,
            "location": "scripts/sync.py:23",
            "finding": "for key, val in os.environ.items(): ...",
            "explanation": "Collects env vars and sends them externally.",
        },
    ],
    "metadata": {
        "has_executable_scripts": True,
        "skillspector_version": "2.0.0",
        "llm_requested": False,
        "llm_available": False,
    },
}


def test_severity_to_priority_maps_severities_to_recon_priorities():
    assert severity_to_priority("CRITICAL") == "high"
    assert severity_to_priority("HIGH") == "high"
    assert severity_to_priority("MEDIUM") == "medium"
    assert severity_to_priority("LOW") == "low"
    # Unknown / missing severities default to low rather than crashing.
    assert severity_to_priority("") == "low"
    assert severity_to_priority("bogus") == "low"


def test_parse_report_extracts_core_fields():
    result = parse_report(SAMPLE_REPORT)
    assert isinstance(result, ScanResult)
    assert result.name == "demo-skill"
    assert result.score == 78
    assert result.severity == "HIGH"
    assert result.recommendation == "DO NOT INSTALL"
    assert result.has_executable_scripts is True
    assert len(result.issues) == 1
    assert result.issues[0]["category"] == "Data Exfiltration"
    assert result.issues[0]["location"] == "scripts/sync.py:23"


def test_parse_report_tolerates_missing_fields():
    # A minimal/odd report must not crash — score defaults to 0, issues to [].
    result = parse_report({"skill": {"name": "bare"}})
    assert result.name == "bare"
    assert result.score == 0
    assert result.severity == "LOW"
    assert result.issues == []
    assert result.has_executable_scripts is False


def test_report_to_finding_builds_recon_finding():
    result = parse_report(SAMPLE_REPORT)
    finding = report_to_finding(result)
    assert finding["job_type"] == "skill-security"
    assert finding["priority"] == "high"
    assert "demo-skill" in finding["title"]
    assert "78" in finding["title"]  # score surfaced in the title
    # Summary names the top issue category so triage is actionable at a glance.
    assert "Data Exfiltration" in finding["summary"]
    assert "DO NOT INSTALL" in finding["summary"]


def test_report_to_finding_handles_dict_location():
    # SkillSpector emits `location` as either a string ("file:line") or a dict
    # ({"file","start_line","end_line"}); the summary must render a clean
    # "file:line", never a raw dict repr.
    result = parse_report(
        {
            "skill": {"name": "x"},
            "risk_assessment": {"score": 25, "severity": "MEDIUM", "recommendation": "CAUTION"},
            "issues": [
                {
                    "category": "Memory Poisoning",
                    "severity": "HIGH",
                    "location": {"file": "SKILL.md", "start_line": 288, "end_line": None},
                }
            ],
        }
    )
    finding = report_to_finding(result)
    assert "SKILL.md:288" in finding["summary"]
    assert "{" not in finding["summary"]  # no raw dict repr leaked into the text


def test_report_to_finding_safe_skill_is_low_priority():
    safe = parse_report(
        {
            "skill": {"name": "safe-skill"},
            "risk_assessment": {"score": 5, "severity": "LOW", "recommendation": "SAFE"},
            "issues": [],
        }
    )
    finding = report_to_finding(safe)
    assert finding["priority"] == "low"
    assert "safe-skill" in finding["title"]


def test_discover_skill_dirs_finds_skill_md_subdirs(tmp_path):
    (tmp_path / "skA").mkdir()
    (tmp_path / "skA" / "SKILL.md").write_text("a")
    (tmp_path / "skB").mkdir()
    (tmp_path / "skB" / "SKILL.md").write_text("b")
    (tmp_path / "not-a-skill").mkdir()  # no SKILL.md → excluded

    found = discover_skill_dirs([tmp_path, tmp_path / "does-not-exist"])

    names = sorted(p.name for p in found)
    assert names == ["skA", "skB"]


async def test_scan_and_store_files_findings_above_threshold():
    stored: list[dict] = []

    def fake_scanner(skill_dir: Path) -> dict:
        if skill_dir.name == "bad":
            return {
                "skill": {"name": "bad", "source": str(skill_dir)},
                "risk_assessment": {"score": 78, "severity": "HIGH", "recommendation": "DO NOT INSTALL"},
                "issues": [{"category": "Data Exfiltration", "severity": "HIGH"}],
            }
        return {
            "skill": {"name": "good", "source": str(skill_dir)},
            "risk_assessment": {"score": 0, "severity": "LOW", "recommendation": "SAFE"},
            "issues": [],
        }

    async def fake_storer(db: object, finding: dict) -> None:
        stored.append(finding)

    results = await scan_and_store(
        None,
        [Path("/x/bad"), Path("/x/good")],
        scanner=fake_scanner,
        storer=fake_storer,
        min_score=21,
    )

    # Both scanned + parsed; only the above-threshold one is filed to recon.
    assert sorted(r.name for r in results) == ["bad", "good"]
    assert len(stored) == 1
    assert stored[0]["title"].startswith("Skill 'bad'")
    assert stored[0]["priority"] == "high"


async def test_scan_and_store_skips_failed_scans():
    async def fake_storer(db: object, finding: dict) -> None:
        raise AssertionError("must not store when the scan fails")

    results = await scan_and_store(
        None,
        [Path("/x/timed-out")],
        scanner=lambda d: None,  # None == scan failed/timed out
        storer=fake_storer,
        min_score=0,
    )

    assert results == []


async def test_store_finding_persists_queryable_recon_finding(db):
    """A stored finding is retrievable via the same query recon_findings uses."""
    from genesis.db.crud import observations as obs_crud

    finding = report_to_finding(parse_report(SAMPLE_REPORT))
    finding_id = await store_finding(db, finding)
    assert finding_id

    rows = await obs_crud.query(
        db, source="recon", type="finding", category="skill-security", limit=10
    )
    assert len(rows) == 1
    assert "demo-skill" in rows[0]["content"]
    assert rows[0]["priority"] == "high"
