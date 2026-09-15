"""The pages Genesis serves that carry a stylesheet — one enumeration, shared.

WHY THIS EXISTS, and it is not tidiness. The login page is built as a Python
string in `src/genesis/dashboard/auth.py`, not as a Jinja template, so every
check written as `TEMPLATE_DIR.glob("*.html")` silently omits it. That has now
happened three times:

  1. the viewport-lock neutralisation was copied per page and the login page
     never got a copy — MEASURED live before the fix: `overflow: hidden`,
     `position: fixed`, and a document that could not scroll;
  2. the enumeration that found the other four missed pages missed this one too,
     while fixing exactly this defect;
  3. the CSS-isolation lint was then written against the same glob.

Each time the fix was "enumerate it explicitly, here". A rule that every call
site must REMEMBER is a rule that a new call site will not, so the obligation
moves here. Import `stylesheet_pages()`; do not glob the template directory.

A page appears here if it can carry a `<link rel="stylesheet">`, whether or not
it currently does — deciding that is the caller's job, not this function's.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "src/genesis/dashboard/templates"


def stylesheet_pages() -> dict[str, str]:
    """Every servable page's HTML, keyed by a name a failure message can print.

    The glob is deliberately NON-recursive: `templates/partials/` holds fragments
    that are included into these pages rather than served, so a stylesheet link
    there would be a defect of a different kind. MEASURED: `href="/index.css"`
    appears on exactly six pages — the five top-level templates and the login
    string — and on none of the 29 partials.
    """
    pages = {p.name: p.read_text() for p in sorted(TEMPLATE_DIR.glob("*.html"))}

    # Imported lazily: this module is imported by tests that do not otherwise
    # need the dashboard package, and `auth` pulls in the Flask app factory.
    from genesis.dashboard import auth

    pages["auth.py::_LOGIN_HTML"] = auth._LOGIN_HTML
    return pages
