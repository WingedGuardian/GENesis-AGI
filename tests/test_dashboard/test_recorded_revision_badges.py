"""Evaluate the shipped Alpine badge expressions for every exposed state."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/templates/partials"


class Badges(HTMLParser):
    def __init__(self):
        super().__init__()
        self.badges = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "span" and "recorded_revision" in attrs.get("x-text", ""):
            self.badges.append(attrs)


@pytest.mark.parametrize("name", ["tabs/sessions.html", "modals/cc_sessions.html"])
def test_both_row_types_render_three_honest_revision_states(name):
    parser = Badges()
    parser.feed((ROOT / name).read_text())
    assert len(parser.badges) == 2  # never let a renamed expression pass vacuously
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to evaluate the actual Alpine expressions")
    for state, expected in [
        ("same_recorded_revision", "same recorded revision"),
        ("different_recorded_revision", "recorded revision differs"),
        ("unknown", "revision unknown"),
    ]:
        for badge in parser.badges:
            script = (
                "const u = " + json.dumps({"recorded_revision": state}) + ";"
                "const s = {live:u}; console.log(JSON.stringify(["
                + badge["x-text"] + "," + badge[":title"] + "]));"
            )
            result = subprocess.run([node, "-e", script], capture_output=True,
                                    text=True, check=True, timeout=10)
            label, tooltip = json.loads(result.stdout)
            assert label == expected
            assert "loaded bytes unverified" in tooltip.lower()
            assert "restart" not in (label + tooltip).lower()
            assert "older" not in (label + tooltip).lower()
