"""Genesis restore — counterpart to scripts/backup.sh.

Exposes `genesis restore` via `python -m genesis restore`. The actual
restoration logic lives in `scripts/restore.sh`; this package is a thin
CLI wrapper that forwards arguments.
"""
