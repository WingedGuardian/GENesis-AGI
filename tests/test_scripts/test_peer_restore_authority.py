"""Real SQLite and recovery writers; all databases/services are disposable."""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from genesis.db.crud.peer_restore import RestoreAuthorityError, reset_peer_authority
from tests.test_scripts import test_restore_safety as restore

ROOT = Path(__file__).resolve().parents[2]
sandbox = restore.sandbox
SCHEMA = """
CREATE TABLE peer_settings(id INTEGER PRIMARY KEY,mode TEXT,sam_realm TEXT,service_url TEXT);
INSERT INTO peer_settings VALUES(1,'fallback','realm','https://peer.example');
CREATE TABLE peers(peer_id TEXT PRIMARY KEY,epoch TEXT,revision INTEGER,active INTEGER);
INSERT INTO peers VALUES('muse','old-epoch',2,1),('revoked','revoked-epoch',4,0);
CREATE TABLE peer_grants(peer_id TEXT REFERENCES peers(peer_id),capability TEXT,decision TEXT);
INSERT INTO peer_grants VALUES('muse','task','allow');
CREATE TABLE peer_tasks(peer_id TEXT REFERENCES peers(peer_id),epoch TEXT,status TEXT);
INSERT INTO peer_tasks VALUES('muse','old-epoch','running');
"""


def _seed(path, sql=SCHEMA):
    with sqlite3.connect(path) as db:
        db.executescript(sql)
    return path


def _assert_reset(path):
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT mode FROM peer_settings").fetchall() == [("disabled",)]
        assert db.execute("SELECT * FROM peer_grants").fetchall() == []
        rows = db.execute(
            "SELECT peer_id,epoch,revision,active FROM peers ORDER BY peer_id"
        ).fetchall()
        assert [(r[0], r[2], r[3]) for r in rows] == [("muse", 3, 1), ("revoked", 5, 0)]
        assert rows[0][1] != "old-epoch" and rows[1][1] != "revoked-epoch"
        assert db.execute("SELECT * FROM peer_tasks").fetchall() == [
            ("muse", "old-epoch", "running")
        ]
        assert db.execute("SELECT sam_realm,service_url FROM peer_settings").fetchone() == (
            "realm",
            "https://peer.example",
        )
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)


@pytest.mark.parametrize("mode", ["disabled", "fallback", "sam"])
def test_modes_reset_and_repeated_restore_renews_epochs(tmp_path, mode):
    path = _seed(tmp_path / "candidate.db")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE peer_settings SET mode=?", (mode,))
    assert reset_peer_authority(path)
    _assert_reset(path)
    with sqlite3.connect(path) as db:
        first = db.execute("SELECT epoch FROM peers ORDER BY peer_id").fetchall()
    assert reset_peer_authority(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT epoch FROM peers ORDER BY peer_id").fetchall() != first


def test_legacy_and_uri_path_and_missing_file(tmp_path):
    path = _seed(tmp_path / "candidate?# ü.db", "CREATE TABLE t(x); INSERT INTO t VALUES(42);")
    assert not reset_peer_authority(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM t").fetchall() == [(42,)]
        assert (
            db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'peer_%'").fetchall() == []
        )
    missing = tmp_path / "absent.db"
    with pytest.raises(sqlite3.Error):
        reset_peer_authority(missing)
    assert not missing.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE peer_grants;",
        "DROP TABLE peer_grants; CREATE VIEW peer_grants AS SELECT 1 AS peer_id;",
        "UPDATE peer_settings SET id=2;",
        "UPDATE peer_settings SET mode='unknown';",
        "UPDATE peers SET epoch=NULL;",
        "UPDATE peers SET revision=9223372036854775807;",
        "CREATE TRIGGER keep_grants BEFORE DELETE ON peer_grants BEGIN SELECT RAISE(IGNORE); END;",
        "CREATE TRIGGER break_reset AFTER UPDATE ON peers BEGIN UPDATE peer_settings SET mode='sam'; END;",
    ],
)
def test_incompatible_or_failed_reset_rolls_back(tmp_path, mutation):
    path = _seed(tmp_path / "candidate.db", SCHEMA + mutation)
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    with pytest.raises((RestoreAuthorityError, sqlite3.Error)):
        reset_peer_authority(path)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_wal_candidate_becomes_self_contained(tmp_path):
    path = _seed(tmp_path / "candidate.db")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    reset_peer_authority(path)
    copy = tmp_path / "published.db"
    copy.write_bytes(path.read_bytes())
    _assert_reset(copy)


def test_sqlite_identifier_case_cannot_hide_authority(tmp_path):
    sql = SCHEMA
    for name in ("peer_settings", "peers", "peer_grants", "peer_tasks"):
        sql = sql.replace(name, name.upper())
    sql = sql.replace("mode TEXT", "MODE TEXT").replace("revision INTEGER", "REVISION INTEGER")
    path = _seed(tmp_path / "candidate.db", sql)
    assert reset_peer_authority(path)
    _assert_reset(path)
    partial = _seed(tmp_path / "partial.db", "CREATE TABLE PEER_TASKS(x);")
    with pytest.raises(RestoreAuthorityError):
        reset_peer_authority(partial)


def test_empty_settings_and_peers(tmp_path):
    path = _seed(
        tmp_path / "candidate.db",
        SCHEMA
        + "DELETE FROM peer_tasks; DELETE FROM peer_grants; DELETE FROM peers; DELETE FROM peer_settings;",
    )
    assert reset_peer_authority(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,mode FROM peer_settings").fetchall() == [(1, "disabled")]


@pytest.mark.parametrize("args", [(), ("--database-only",)])
def test_actual_restore_resets_before_publishing(sandbox, monkeypatch, args):
    original = restore._seed_backup

    def seed(tmp):
        backup = original(tmp)
        dump = backup / "data/genesis.sql"
        dump.write_text(dump.read_text() + SCHEMA)
        return backup

    monkeypatch.setattr(restore, "_seed_backup", seed)
    live = restore._seed_live_db(sandbox["gd"])
    result = restore._run_restore(sandbox, *args)
    assert result.returncode == 0, result.stderr
    _assert_reset(live)


@pytest.mark.parametrize("args", [("--database-only",), ("--dry-run",)])
def test_refusal_or_dry_run_preserves_live_and_does_not_stop(sandbox, monkeypatch, args):
    original = restore._seed_backup

    def seed(tmp):
        backup = original(tmp)
        dump = backup / "data/genesis.sql"
        dump.write_text(dump.read_text() + "CREATE TABLE peer_tasks(x);")
        return backup

    monkeypatch.setattr(restore, "_seed_backup", seed)
    live = restore._seed_live_db(sandbox["gd"])
    before = live.read_bytes()
    result = restore._run_restore(sandbox, *args)
    assert (result.returncode == 0) == (args == ("--dry-run",))
    assert live.read_bytes() == before
    calls = sandbox["calls"].read_text() if sandbox["calls"].exists() else ""
    assert "stop genesis-server" not in calls


@pytest.mark.parametrize("compatible", [True, False])
def test_update_prepares_guarded_snapshot_before_stop(tmp_path, compatible):
    source = (ROOT / "scripts/update.sh").read_text()
    block = source[
        source.index('DB_FILE="$GENESIS_ROOT/data/genesis.db"') : source.index(
            "# ── Stop services for update"
        )
    ]
    root = tmp_path / "checkout"
    (root / "data").mkdir(parents=True)
    (root / "src").symlink_to(ROOT / "src", target_is_directory=True)
    live = _seed(root / "data/genesis.db", SCHEMA if compatible else "CREATE TABLE peer_tasks(x);")
    before = live.read_bytes()
    # Only the shipped preflight block runs: no deploy, service or checkout action.
    script = tmp_path / "preflight.sh"
    script.write_text(
        'set -euo pipefail\nGENESIS_ROOT="$1"\n'
        + block
        + '\nprintf "ADMITTED=%s\\n" "$DB_SNAPSHOT_TAKEN"\n'
    )
    result = subprocess.run(["bash", str(script), str(root)], capture_output=True, text=True)
    assert (result.returncode == 0) == compatible, result.stderr
    assert live.read_bytes() == before
    raw = live.with_name("genesis.db.pre-update")
    with sqlite3.connect(raw) as db:
        if compatible:
            assert db.execute("SELECT mode FROM peer_settings").fetchone() == ("fallback",)
    if compatible:
        assert "ADMITTED=1" in result.stdout
        _assert_reset(live.with_name("genesis.db.pre-update.peer-restore"))
    else:
        assert "ADMITTED=1" not in result.stdout


def test_cli_refusal_has_fixed_diagnostics(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        ["python3", "-m", "genesis.db.crud.peer_restore", str(tmp_path / "absent")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Peer restore authority check refused.\n"


@pytest.mark.parametrize("candidate_present", [True, False])
def test_rollback_to_old_code_consumes_only_guarded_candidate(tmp_path, candidate_present):
    source = (ROOT / "scripts/update.sh").read_text()
    marker = source.index("# BEGIN rollback-db-decision")
    decision = source[
        source.index("\n", marker) + 1 : source.index("# END rollback-db-decision", marker)
    ]
    start = source.index("    genesis_checkout_unlock\n", source.index("_do_rollback() {"))
    restart = source[start : source.index('\n    if [ "$checkout_ok"', start)]
    live = _seed(tmp_path / "genesis.db", "CREATE TABLE migrated(x);")
    raw = _seed(tmp_path / "genesis.db.pre-update")
    if candidate_present:
        guarded = live.with_name("genesis.db.pre-update.peer-restore")
        guarded.write_bytes(raw.read_bytes())
        reset_peer_authority(guarded)
    # Rolled-back checkout has no helper; the real rollback branch needs none.
    script = tmp_path / "old-code-rollback.sh"
    script.write_text(
        'set -euo pipefail\nDB_FILE="$1"\nMIGRATIONS_RAN=1\nDB_SNAPSHOT_TAKEN=1\n'
        "code_action=none\ncode_kept=false\nserver_down=true\nrestart_ok=true\n"
        "WERE_RUNNING=(genesis-server)\ngenesis_checkout_unlock() { :; }\n"
        "_start_genesis_server() { echo STARTED; }\nrun() {\n"
        + decision
        + restart
        + '\nprintf "DB_OK=%s\\n" "$db_ok"\n}\nrun\n'
    )
    result = subprocess.run(["bash", str(script), str(live)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ("STARTED" in result.stdout) == candidate_present
    assert f"DB_OK={'true' if candidate_present else 'false'}" in result.stdout
    if candidate_present:
        _assert_reset(live)
    else:
        with sqlite3.connect(live) as db:
            assert db.execute("SELECT name FROM sqlite_schema").fetchall() == [("migrated",)]
