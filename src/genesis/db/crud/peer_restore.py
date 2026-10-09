"""Reset restored peer authority in an isolated, existing SQLite candidate.

Only recovery writers call this; never pass the serving database. No credentials
are read or rotated. Owner-authorized Guardian snapshots use their own gates.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from uuid import uuid4


class RestoreAuthorityError(ValueError):
    """The candidate cannot safely establish disabled peer authority."""


def reset_peer_authority(path: Path) -> bool:
    """Return whether peer state existed; refuse incompatible state atomically."""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        # The caller publishes one file. Never leave reset writes in a WAL.
        if db.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise RestoreAuthorityError()
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        # SQLite folds ASCII identifier case; inventory must match its readers.
        objects = dict(db.execute("SELECT lower(name), type FROM sqlite_schema"))
        peer_objects = {name for name in objects if name == "peers" or name.startswith("peer_")}
        present = bool(peer_objects)
        if present:
            required = {
                "peer_settings": {"id", "mode"},
                "peers": {"peer_id", "epoch", "revision"},
                "peer_grants": {"peer_id", "capability", "decision"},
            }
            for table, columns in required.items():
                if objects.get(table) != "table":
                    raise RestoreAuthorityError()
                actual = {row[1].lower() for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise RestoreAuthorityError()
            settings = db.execute("SELECT id, mode FROM peer_settings").fetchall()
            if settings and (
                len(settings) != 1
                or settings[0][0] != 1
                or settings[0][1] not in {"disabled", "fallback", "sam"}
            ):
                raise RestoreAuthorityError()
            peers = db.execute("SELECT peer_id, epoch, revision FROM peers").fetchall()
            identities = set()
            renewed = []
            for peer_id, epoch, revision in peers:
                if (
                    not isinstance(peer_id, str)
                    or not peer_id
                    or peer_id in identities
                    or not isinstance(epoch, str)
                    or not epoch
                    or type(revision) is not int
                    or not 1 <= revision < 2**63 - 1
                ):
                    raise RestoreAuthorityError()
                identities.add(peer_id)
                new_epoch = uuid4().hex
                if new_epoch == epoch:
                    raise RestoreAuthorityError()
                renewed.append((new_epoch, revision + 1, peer_id))
            if settings:
                db.execute("UPDATE peer_settings SET mode='disabled' WHERE id=1")
            else:
                db.execute("INSERT INTO peer_settings(id,mode) VALUES(1,'disabled')")
            db.execute("DELETE FROM peer_grants")
            db.executemany("UPDATE peers SET epoch=?, revision=? WHERE peer_id=?", renewed)
            if (
                db.execute("SELECT id,mode FROM peer_settings").fetchall() != [(1, "disabled")]
                or db.execute("SELECT count(*) FROM peer_grants").fetchone() != (0,)
                or set(db.execute("SELECT epoch,revision,peer_id FROM peers")) != set(renewed)
            ):
                raise RestoreAuthorityError()
        if (
            db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise RestoreAuthorityError()
        db.commit()
        return present
    finally:
        # Closing an uncommitted transaction rolls it back, including a failed
        # reset or postcondition. The caller must never publish a refused stage.
        db.close()


def main() -> int:
    if len(sys.argv) != 2:
        print("Peer restore authority check refused.", file=sys.stderr)
        return 1
    try:
        changed = reset_peer_authority(Path(sys.argv[1]))
    except (OSError, sqlite3.Error, ValueError):
        print("Peer restore authority check refused.", file=sys.stderr)
        return 1
    print(
        "Restored peer access disabled; owner reauthorization required."
        if changed
        else "Legacy database: no peer authority to restore."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
