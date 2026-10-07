"""Private manager/worker doubles for queue and shell routing tests.

Native execution authority is tested separately against the real adapter; these
doubles retain the real managed CLI parser and schema/sentinel validation.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def install_manager(
    target: Path, home: Path, main: Path, binary: Path, sentinel: Path | None = None
):
    config = home / ".genesis/config/codebase-managed.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_text(
            json.dumps(
                dict(
                    version=2,
                    main=str(main),
                    binary=str(binary),
                    build="ce11c141431aeadd788506c3a7e6942db8fd438dec369d0707a39ec9fd8c6510",
                    cache=str(home),
                    runtime=str(home),
                    sentinel=str(sentinel or home / ".genesis/codebase-memory-mcp.disabled"),
                )
            )
        )
    target.write_text(
        "import runpy\nfrom pathlib import Path\n"
        f"ns=runpy.run_path({str(ROOT / 'scripts/codebase_managed.py')!r})\n"
        "g=ns['main'].__globals__\n"
        f"g['SCRIPT']=Path({str(main / 'scripts/codebase_managed.py')!r})\n"
        "g['verify_cache']=lambda config: None\n"
        "g['show']=lambda unit,*props,**kwargs: {'UnitFileState':'enabled'}\n"
        "def ready(config):\n"
        "    g['require_enabled'](config)\n"
        "    if not Path(config['binary']).exists(): raise ValueError('backend unavailable')\n"
        "g['ready']=ready\n"
        "raise SystemExit(ns['main']())\n"
    )


def configured_sentinel(home: Path, raw: str):
    """Old shell test inputs become explicit immutable fixture settings."""
    config = home / ".genesis/config/codebase-managed.json"
    value = json.loads(config.read_text())
    value["sentinel"] = raw.replace("~/", str(home) + "/", 1) if raw.startswith("~/") else raw
    config.write_text(json.dumps(value))
