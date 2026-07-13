"""Canonical filesystem locations for the infrastructure body schema.

Everything lives under ``~/.genesis/infrastructure/`` — install-specific data,
never inside the repo tree. The rendered document is additionally mirrored to
``~/.genesis/shared/infrastructure/`` so the host-side guardian can read it via
the Incus shared mount (same mechanism as ``guardian/briefing.py``).
"""

from __future__ import annotations

from pathlib import Path

PROFILE_DIR = Path("~/.genesis/infrastructure").expanduser()
PROFILE_PATH = PROFILE_DIR / "profile.json"
ANNOTATIONS_PATH = PROFILE_DIR / "annotations.json"
DOC_PATH = PROFILE_DIR / "INFRASTRUCTURE.md"

# Host-visible mirror of the rendered doc (guardian reads it at
# $STATE_DIR/shared/infrastructure/INFRASTRUCTURE.md).
SHARED_DOC_DIR = Path("~/.genesis/shared/infrastructure").expanduser()
SHARED_DOC_PATH = SHARED_DOC_DIR / "INFRASTRUCTURE.md"
