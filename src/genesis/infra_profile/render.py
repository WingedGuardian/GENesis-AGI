"""Render the profile + annotations into consumable views.

Three views, all derived from the same JSON (never hand-edited):

- ``render_document``  — the full INFRASTRUCTURE.md
- ``sentinel_digest``  — compact section for the sentinel diagnostic context
- ``headline_facts``   — the handful of facts worth inlining into the
  user-level CLAUDE.md block
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

UNAVAILABLE_TEXT = "not visible from this vantage"


def _is_stale(section: dict[str, Any], annotation: dict[str, Any] | None) -> bool:
    if not annotation:
        return False
    return annotation.get("source_hash") != section.get("hash")


def _fmt_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    gib = value / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 1 else f"{value / (1024**2):.0f} MiB"


def render_document(
    profile: dict[str, Any],
    annotations: dict[str, Any],
) -> str:
    """Render the full INFRASTRUCTURE.md."""
    sections = profile.get("sections", {})
    ann_sections = annotations.get("sections", {})
    planes = profile.get("planes", {})

    lines: list[str] = [
        "# Infrastructure Body Schema",
        "",
        "Machine-generated self-knowledge of the environment this Genesis runs on.",
        "Facts are collected programmatically; annotations are LLM judgment pinned",
        "to the facts they were derived from. Do not hand-edit — regenerate via the",
        "`infrastructure_profile` MCP tool.",
        "",
        f"Collected: {profile.get('collected_at', 'unknown')}",
    ]
    host_plane = planes.get("host", {})
    if not host_plane.get("available", False):
        reason = host_plane.get("reason", "unknown")
        lines.append(f"Host plane: {UNAVAILABLE_TEXT} ({reason})")
    lines.append("")

    for name, section in sections.items():
        lines.append(f"## {name}")
        lines.append("")
        status = section.get("status")
        if status == "unavailable":
            lines.append(f"_{UNAVAILABLE_TEXT}: {section.get('error', 'unknown')}_")
            lines.append("")
            continue
        if status == "error":
            lines.append(
                f"_last collection FAILED ({section.get('error', 'unknown')}); "
                "facts below are from the previous successful run_",
            )
            lines.append("")

        annotation = ann_sections.get(name)
        if annotation and annotation.get("annotation"):
            if _is_stale(section, annotation):
                lines.append(
                    "**⚠ annotation STALE — facts changed since it was written:**",
                )
            lines.append(annotation["annotation"])
            lines.append("")
        elif status == "ok":
            lines.append("_no annotation yet_")
            lines.append("")

        lines.append("```json")
        lines.append(json.dumps(section.get("facts", {}), indent=2, default=str))
        lines.append("```")
        metrics = section.get("metrics") or {}
        if metrics:
            lines.append("")
            lines.append("Current readings (volatile, unhashed):")
            lines.append("```json")
            lines.append(json.dumps(metrics, indent=2, default=str))
            lines.append("```")
        lines.append("")

    return "\n".join(lines)


def headline_facts(profile: dict[str, Any]) -> dict[str, str]:
    """The handful of facts worth inlining into CLAUDE.md."""
    sections = profile.get("sections", {})

    def facts(name: str) -> dict:
        return sections.get(name, {}).get("facts", {})

    def metrics(name: str) -> dict:
        return sections.get(name, {}).get("metrics", {})

    out: dict[str, str] = {}
    mem_max = facts("memory").get("cgroup_memory_max")
    if mem_max:
        out["memory_limit"] = _fmt_bytes(mem_max)
    model = facts("cpu").get("model")
    if model:
        out["cpu"] = f"{model} ×{facts('cpu').get('count', '?')}"
    kernel = facts("kernel").get("release")
    if kernel:
        out["kernel"] = kernel
    virt = facts("virt").get("container")
    if virt:
        out["virtualization"] = virt
    root = metrics("storage").get("root", {})
    if root:
        out["root_disk"] = (
            f"{_fmt_bytes(root.get('free_bytes'))} free ({root.get('pct_used')}% used)"
        )
    journal = facts("sqlite").get("pragmas", {}).get("journal_mode")
    if journal:
        out["sqlite"] = f"journal_mode={journal}"
    host_available = profile.get("planes", {}).get("host", {}).get("available", False)
    out["host_plane"] = "visible" if host_available else UNAVAILABLE_TEXT
    return out


def sentinel_digest(
    profile: dict[str, Any],
    annotations: dict[str, Any],
    *,
    drift_since_days: int = 7,
    max_chars: int = 6000,
) -> str:
    """Compact digest for the sentinel diagnostic context.

    Headline facts + annotations (they ARE the gotchas a diagnostic session
    needs) + recently drifted sections + stale-annotation flags.

    Size cap is by CHARACTERS with an explicit truncation marker — a line
    slice under-counted multi-line annotation bodies and silently dropped
    the tail, which is exactly where the gotchas live (review 2026-07-12).
    """
    if not profile.get("sections"):
        return ""

    lines: list[str] = []
    for key, value in headline_facts(profile).items():
        lines.append(f"- {key}: {value}")

    ann_sections = annotations.get("sections", {})
    sections = profile.get("sections", {})

    now = datetime.now(UTC)
    recent: list[str] = []
    for name, section in sections.items():
        changed_at = section.get("facts_changed_at")
        if not changed_at:
            continue
        try:
            age = (now - datetime.fromisoformat(changed_at)).days
        except ValueError:
            continue
        if age <= drift_since_days:
            recent.append(f"- {name} (facts changed {age}d ago)")
    if recent:
        lines.append("")
        lines.append(f"Sections drifted in the last {drift_since_days}d:")
        lines.extend(recent)

    lines.append("")
    lines.append(
        "Operational gotchas (full profile: see INFRASTRUCTURE.md):",
    )
    for name, annotation in ann_sections.items():
        text = annotation.get("annotation")
        if not text:
            continue
        stale = _is_stale(sections.get(name, {}), annotation)
        marker = " [STALE — facts changed since written]" if stale else ""
        lines.append(f"### {name}{marker}")
        lines.append(text)

    digest = "\n".join(lines)
    if max_chars and len(digest) > max_chars:
        cut = digest.rfind("\n", 0, max_chars)
        digest = digest[: cut if cut > 0 else max_chars]
        digest += "\n…(digest truncated — full profile in INFRASTRUCTURE.md)"
    return digest
