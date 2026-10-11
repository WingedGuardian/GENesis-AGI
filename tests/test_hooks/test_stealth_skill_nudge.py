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
    tmp: Path,
    layer: str | None,
    sid: str | None = "s1",
    as_string: bool = True,
    empty: bool = False,
) -> str:
    response = {"layer": layer, "url": "https://x.example"} if layer else {"error": "boom"}
    if empty:
        response = {}
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


def test_a_failed_navigate_still_gets_the_safety_pointer(tmp_path):
    """A navigate that errors after the page opened leaves that page usable
    (browser_fill can type into it), and the error carries no layer. The safety
    pointer is layer-independent, so it fires anyway. The stealth pointer needs
    the layer, so it is left unspent for the next successful Camoufox navigate."""
    text = _nudge(tmp_path, None)
    assert "Safety gates" in text and "stealth-browser" not in text
    later = _nudge(tmp_path, "camoufox")
    assert "stealth-browser" in later and "Safety gates" not in later


def test_an_empty_response_is_silent(tmp_path):
    """Both shapes: an empty object, and the JSON string '{}' an MCP tool sends."""
    assert _nudge(tmp_path, None, as_string=False, empty=True) == ""
    assert _nudge(tmp_path, None, as_string=True, empty=True) == ""
    assert not list(tmp_path.glob("genesis_*nudge_*"))


def test_a_session_id_of_maximum_length_still_fires_once(tmp_path):
    """session_id() accepts ids up to 255 bytes, but prefix + id must still fit
    one filename component, or open() fails and the pointer repeats forever."""
    sid = "a" * 255
    assert "Safety gates" in _nudge(tmp_path, "camoufox", sid=sid)
    assert _nudge(tmp_path, "camoufox", sid=sid) == ""
    # A different long id that shares the leading bytes is a different session.
    assert "Safety gates" in _nudge(tmp_path, "camoufox", sid="a" * 254 + "b")
    names = [p.name for p in tmp_path.glob("genesis_*nudge_*")]
    assert len(names) == 4 and all(len(n.encode()) <= 255 for n in names)


def test_a_short_session_id_keeps_its_readable_sentinel_name(tmp_path):
    assert _nudge(tmp_path, "camoufox", sid="s1")
    assert (tmp_path / "genesis_stealth_nudge_s1").exists()
    assert (tmp_path / "genesis_browser_safety_nudge_s1").exists()


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
