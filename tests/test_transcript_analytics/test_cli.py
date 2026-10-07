import json
import subprocess
import sys
from pathlib import Path

from genesis.transcript_analytics import cli, config


def test_disabled_and_help_never_import_analytics_dependencies(tmp_path):
    # Import blocker is stronger than having optional libraries installed locally.
    source = str(Path(__file__).resolve().parents[2] / "src")
    code = """
import sys
sys.path.insert(0, sys.argv[1])
class Block:
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in ('duckdb', 'pyarrow'): raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from genesis.transcript_analytics import cli, config
config.load = lambda: config.Config()
assert cli.main(['status']) == 0
try: cli.main(['--help'])
except SystemExit as e: assert e.code == 0
"""
    result = subprocess.run([sys.executable, "-c", code, source], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_disabled_collection_requires_optin(monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda: config.Config())
    assert cli.main(["ingest"]) == 2
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert cli.main(["ingest", "--timer"]) == 0


def test_invalid_config_does_not_collect(monkeypatch):
    def bad():
        raise ValueError("synthetic invalid configuration")

    monkeypatch.setattr(config, "load", bad)
    assert cli.main(["ingest"]) == 2
