"""The pages Genesis serves that carry a stylesheet — DERIVED, not listed.

WHY THIS EXISTS, and why it is derived rather than enumerated. Some pages are
built as Python strings rather than Jinja templates, so every check written as
`TEMPLATE_DIR.glob("*.html")` silently omits them. That has now happened four
times, and the fourth is the reason this module no longer contains a hand-list:

  1. the viewport-lock neutralisation was copied per page and the login page
     never got a copy — MEASURED live before the fix: `overflow: hidden`,
     `position: fixed`, and a document that could not scroll;
  2. the enumeration that found the other four missed pages missed that one too,
     while fixing exactly this defect;
  3. the CSS-isolation lint was then written against the same glob;
  4. this module replaced the glob with an explicit list of TWO sources — and a
     review found a third, `routes/terminal.py`, serving `/genesis/terminal`
     with a vendor stylesheet and an inline `<style>` that declares
     `html, body { … overflow: hidden }`. The incident-one shape, on a page no
     check could see.

A hand-list has the glob's defect with more entries: it omits silently. So the
emitter set is DISCOVERED — every module under the search roots that contains a
stylesheet link — and each discovered module must be either MAPPED to the page it
serves or EXEMPTED with a stated reason. A module that is neither fails loudly the
first time someone adds one, which is the only property that matters here.

The check runs in both directions. An unmapped emitter fails; so does a mapping
for a module that has stopped emitting, because a stale row is a reason nobody
re-reads.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TEMPLATE_DIR = SRC / "genesis/dashboard/templates"

# Where a Python-generated page could plausibly live. Deliberately two roots
# rather than all of `src`: the dashboard serves pages, and the hosting layer
# injects into someone else's. A third root would need adding here, which is a
# visible act — unlike the discovery it would otherwise escape.
_SEARCH_ROOTS = (SRC / "genesis/dashboard", SRC / "genesis/hosting")

_LINK_MARKER = re.compile(r"""rel=["']?stylesheet""", re.I)

# module path (relative to src/) -> (import path, symbol) for a page this guard
# reads, or None for one it deliberately does not, with the reason.
_PAGE_SOURCES: dict[str, tuple[str, str] | None] = {
    "genesis/dashboard/auth.py": ("genesis.dashboard.auth", "_LOGIN_HTML"),
    "genesis/dashboard/routes/terminal.py": (
        "genesis.dashboard.routes.terminal",
        "_TERMINAL_PAGE_HTML",
    ),
    # NOT a Genesis page: this injects two tags into Agent Zero's own index
    # page, which this repository does not serve and whose stylesheet
    # (`/genesis-ui/genesis-overlay.css`) is not under `webui/`. The vendor sheet
    # this guard is about is not loaded there. Exempt, and stated rather than
    # skipped — if the overlay ever grows a layout rule against AZ's own CSS,
    # that is a real question and this row is where someone will argue it.
    "genesis/hosting/agent_zero/overlay.py": None,
}


def _emitters() -> set[str]:
    """Every module under the search roots that contains a stylesheet link."""
    found: set[str] = set()
    for root in _SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            if _LINK_MARKER.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(path.relative_to(SRC).as_posix())
    return found


def stylesheet_pages() -> dict[str, str]:
    """Every servable page's HTML, keyed by a name a failure message can print.

    The template glob is deliberately NON-recursive: `templates/partials/` holds
    fragments that are included into these pages rather than served, so a
    stylesheet link there would be a defect of a different kind.
    """
    found = _emitters()
    unmapped = sorted(found - set(_PAGE_SOURCES))
    assert not unmapped, (
        f"these modules emit a stylesheet link and this file does not know about "
        f"them: {unmapped}. Each one is a page no CSS check can see. Add it to "
        "_PAGE_SOURCES with its import path and symbol, or map it to None with "
        "the reason it is not a Genesis page. Four separate defects have now been "
        "shipped by a page that no enumeration happened to name."
    )
    stale = sorted(set(_PAGE_SOURCES) - found)
    assert not stale, (
        f"_PAGE_SOURCES names modules that no longer emit a stylesheet link: "
        f"{stale}. Delete the rows — a clearance nothing exercises is a reason "
        "nobody re-reads."
    )

    pages = {p.name: p.read_text() for p in sorted(TEMPLATE_DIR.glob("*.html"))}
    for module_path, target in sorted(_PAGE_SOURCES.items()):
        if target is None:
            continue
        import_path, symbol = target
        module = importlib.import_module(import_path)
        pages[f"{Path(module_path).name}::{symbol}"] = getattr(module, symbol)
    return pages
