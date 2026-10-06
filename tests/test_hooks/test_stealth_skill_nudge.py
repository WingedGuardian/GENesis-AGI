"""stealth_skill_nudge: the safety pointer fires on every browser layer.

The payment and credential rules live in the browser-automation skill and
apply to every layer, so the pointer to them must not depend on Camoufox being
the layer in use. The stealth pointer stays Camoufox-only. Each fires once per
session through its own sentinel. Advisory only: exit 0 always.

Runs the hook as a subprocess with a private TMPDIR (where the sentinels live).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HOOK = REPO / "scripts/hooks/stealth_skill_nudge.py"


def _nudge(
    tmp: Path, layer: str | None, sid: str | None = "s1", as_string: bool = True
) -> str:
    response = {"layer": layer, "url": "https://x.example"} if layer else {"error": "boom"}
    # Production shape: an MCP tool's tool_response is a JSON STRING (measured
    # from the session observer). Feeding a dict hid that the hook never fired.
    if as_string:
        response = json.dumps(response)
    payload = {
        "tool_name": "mcp__genesis-health__browser_navigate",
        "tool_input": {"url": "https://x.example"},
        "tool_response": response,
    }
    if sid is not None:
        payload["session_id"] = sid
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp), "HOME": str(tmp)},
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_safety_pointer_fires_on_chromium(tmp_path):
    text = _nudge(tmp_path, "chromium")
    assert "Safety gates" in text and "browser-automation" in text
    assert "stealth-browser" not in text


def test_safety_pointer_fires_on_remote_cdp_and_tinyfish(tmp_path):
    assert "Safety gates" in _nudge(tmp_path, "remote_cdp", sid="a")
    assert "Safety gates" in _nudge(tmp_path, "tinyfish_cdp", sid="b")


def test_camoufox_gets_both_pointers(tmp_path):
    text = _nudge(tmp_path, "camoufox")
    assert "Safety gates" in text
    assert "stealth-browser" in text


def test_each_pointer_fires_once_per_session(tmp_path):
    assert _nudge(tmp_path, "chromium")
    assert _nudge(tmp_path, "chromium") == ""
    later = _nudge(tmp_path, "camoufox")
    assert "stealth-browser" in later and "Safety gates" not in later
    assert _nudge(tmp_path, "camoufox") == ""


def test_a_failed_navigate_is_silent_and_spends_nothing(tmp_path):
    assert _nudge(tmp_path, None) == ""
    assert "Safety gates" in _nudge(tmp_path, "chromium")


def test_no_session_id_never_persists_a_shared_sentinel(tmp_path):
    """Without a usable id every such session would share one sentinel, and the
    first would silence the pointers for all later ones. Repeat instead."""
    for sid in (None, "", "../bad"):
        assert "stealth-browser" in _nudge(tmp_path, "camoufox", sid=sid)
        assert "Safety gates" in _nudge(tmp_path, "camoufox", sid=sid)
    assert not list(tmp_path.glob("genesis_*nudge_*"))


def test_a_dict_shaped_response_still_works(tmp_path):
    """Non-MCP tools deliver an object; the helper accepts both shapes."""
    text = _nudge(tmp_path, "chromium", as_string=False)
    assert "Safety gates" in text
