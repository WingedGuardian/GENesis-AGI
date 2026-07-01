"""The engine core must be vendorable to the edge voice repo unchanged: importing it
must NOT pull in any genesis-runtime dependency. Enforced in a clean subprocess (a
full pytest run has already imported aiosqlite/genesis.db via other tests, so an
in-process sys.modules check would false-pass)."""
import os
import subprocess
import sys
import textwrap


def test_core_has_no_genesis_runtime_deps():
    code = textwrap.dedent(
        """
        import sys
        for m in ("genesis.attention.types", "genesis.attention.clarity",
                  "genesis.attention.config", "genesis.attention.triggers",
                  "genesis.attention.scorer", "genesis.attention.engine"):
            __import__(m)
        forbidden = [d for d in ("aiosqlite", "genesis.db", "genesis.routing",
                                 "genesis.awareness", "genesis.memory", "genesis.observability")
                     if d in sys.modules]
        print(",".join(forbidden))
        sys.exit(1 if forbidden else 0)
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"core imported forbidden dep(s): {r.stdout.strip()!r} err={r.stderr.strip()!r}"
