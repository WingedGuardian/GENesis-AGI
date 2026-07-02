"""Genesis Voice — the optional voice-infrastructure dashboard section.

Serves the ``/genesis/voice`` page (a top-level nav peer of Dashboard / Event Log /
Error Log / Neural Monitor) and a tiny enablement probe that drives the nav link's
visibility. Voice is an OPTIONAL add-on: on a stock clone with no
``~/.genesis/ambient_remote.yaml`` the page 404s and the nav link stays hidden, so
non-voice installs never see voice surfaces.

The page's sub-tabs (Calibration / Bridge / STT / S2S) are all in the served template;
the Bridge tab reuses the existing ``/api/genesis/health`` ``infrastructure.ambient``
block, so no heavy voice-specific endpoint is added here.
"""
from __future__ import annotations

from pathlib import Path

from flask import abort, jsonify, send_from_directory

from genesis.dashboard._blueprint import blueprint

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _voice_configured() -> bool:
    """True when this install has the optional ambient/voice add-on configured.

    Present-but-malformed ``ambient_remote.yaml`` counts as configured (the Bridge tab
    surfaces the degraded reason); only a wholly-absent config means "no voice add-on".
    Reads the local YAML only — never the SSH edge probe — so it is cheap to call per page.
    """
    from genesis.observability.ambient_health import (
        AmbientRemoteConfigError,
        load_ambient_remote_config,
    )

    try:
        return load_ambient_remote_config() is not None
    except AmbientRemoteConfigError:
        return True


@blueprint.route("/genesis/voice")
def voice_page():
    """Serve the Genesis Voice page. 404 when the optional voice add-on isn't configured
    (a web-UI page → also auth-gated by the blueprint before_request hook)."""
    if not _voice_configured():
        abort(404)
    return send_from_directory(str(TEMPLATE_DIR), "genesis_voice.html")


@blueprint.route("/api/genesis/voice/enabled")
def voice_enabled():
    """Whether the optional voice add-on is configured — drives nav-link visibility.
    Non-sensitive boolean; open like the rest of the ``/api`` surface."""
    return jsonify({"enabled": _voice_configured()})
