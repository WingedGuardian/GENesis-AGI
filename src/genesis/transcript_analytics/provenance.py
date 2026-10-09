"""Private atomic evidence manifest publication; no outcome database."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def write_manifest(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"generated_at": datetime.now(UTC).isoformat(), **value}
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
