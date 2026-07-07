"""Structural integrity checks for the dashboard webui JS.

dashboard.js defines one large Alpine store as a single object literal. In a
JS object literal a duplicated key is legal and the *second* definition
silently wins — which is how the two ``resolveApproval`` implementations
coexisted with one shadowing the other. These tests parse the store literal
by its stable indentation convention (members at exactly 8 spaces) and
assert every member name is unique.
"""

import re
from pathlib import Path

DASHBOARD_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "genesis"
    / "dashboard"
    / "webui"
    / "js"
    / "dashboard.js"
)

STORE_OPEN = 'Alpine.store("genesisDashboard", {'
STORE_CLOSE = re.compile(r"^      \}\);")
MEMBER = re.compile(r"^        (?:async )?([A-Za-z_$][\w$]*)\s*[:(]")

# If the file shrinks below this, the indentation convention the parser
# relies on has probably changed and the scan has gone blind — fail loudly
# instead of passing on an empty member list.
MIN_EXPECTED_MEMBERS = 300


def _store_member_names() -> list[str]:
    lines = DASHBOARD_JS.read_text().splitlines()
    names: list[str] = []
    in_store = False
    for line in lines:
        if not in_store:
            if STORE_OPEN in line:
                in_store = True
            continue
        if STORE_CLOSE.match(line):
            break
        m = MEMBER.match(line)
        if m:
            names.append(m.group(1))
    return names


def test_store_literal_found_and_parsed():
    names = _store_member_names()
    assert len(names) >= MIN_EXPECTED_MEMBERS, (
        f"Only {len(names)} store members detected — the store literal or its "
        "8-space member indentation convention has changed; update this test's "
        "parser so duplicate-key detection keeps working."
    )


def test_store_member_names_are_unique():
    names = _store_member_names()
    seen: set[str] = set()
    dupes = sorted({n for n in names if n in seen or seen.add(n)})
    assert not dupes, (
        f"Duplicate keys in the genesisDashboard store literal: {dupes}. "
        "In a JS object literal the second definition silently shadows the "
        "first — merge the implementations instead."
    )
