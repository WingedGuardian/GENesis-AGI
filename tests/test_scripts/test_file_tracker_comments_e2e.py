"""Execute the real filer and four privacy detectors; fake gh owns every sink."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def prepare_owned_github(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fingerprint = tmp_path / "fingerprints.txt"
    fingerprint.write_text("# synthetic test fingerprint\nfixture-private-marker\n")
    body = tmp_path / "draft.md"
    original = b"Synthetic measured result with a retained control.\n"
    body.write_bytes(original if mode != "private_fingerprint" else b"fixture-private-marker\n")
    (tmp_path / "expected-body.bin").write_bytes(original)
    stub = bin_dir / "gh"
    stub.write_text(f"#!{sys.executable}\n" + r'''
import json, os, stat, sys
from pathlib import Path
root = Path(os.environ["GENESIS_COMMENT_FIXTURE_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps(args) + "\n")
if args[:2] == ["pr", "comment"]:
    (root / "raw-comment.json").write_text(json.dumps(args))
    raise SystemExit(0)
elif args[:2] == ["repo", "view"]:
    # Change the caller draft after its scan: only the immutable snapshot may post.
    (root / "draft.md").write_text("fixture-private-marker\n")
    result = ({"viewerPermission": "WRITE"} if "viewerPermission" in args
              else {"isFork": False, "nameWithOwner": "Example/Fixture"})
elif "POST" in args:
    field = args[args.index("-F") + 1]
    assert field.startswith("body=@")
    path = Path(field.removeprefix("body=@"))
    raw = path.read_bytes()
    assert raw == (root / "expected-body.bin").read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    result = {"id": 42, "body": raw.decode(),
              "html_url": "https://github.com/Example/Fixture/issues/7#issuecomment-42"}
    (root / "posted.json").write_text(json.dumps(result))
elif any(a.endswith("/comments?per_page=100") for a in args):
    assert "--paginate" in args and "--slurp" in args
    result = [[]]
elif "repos/Example/Fixture/issues/7" in args:
    result = {"number": 7, "html_url": "https://github.com/Example/Fixture/issues/7"}
else:
    raise SystemExit(71)
print(json.dumps(result))
''')
    stub.chmod(0o700)
    env = {
        "HOME": str(home), "PATH": os.pathsep.join((str(bin_dir), str(Path(sys.executable).parent), os.defpath)),
        "LANG": "C.UTF-8", "GENESIS_COMMENT_FIXTURE_ROOT": str(tmp_path),
        "GENESIS_RELEASE_FINGERPRINTS": str(fingerprint if mode != "missing_scanner_floor" else tmp_path / "absent.txt"),
    }
    assert shutil.which("gh", path=env["PATH"]) == str(stub)
    return env, body, original


@pytest.mark.parametrize("mode", ["clean_snapshot", "private_fingerprint", "missing_scanner_floor"])
def test_actual_cli_scans_before_owned_fake_github(tmp_path, mode):
    env, body, original = prepare_owned_github(tmp_path, mode)
    home = Path(env["HOME"])
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/file_tracker_issue.py"),
         "--comment-on", "7", "--body-file", str(body)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    (tmp_path / "cli-result.json").write_text(json.dumps({
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
    }, indent=2))
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()] if (tmp_path / "calls.jsonl").exists() else []
    if mode == "clean_snapshot":
        assert result.returncode == 0, result.stderr
        assert sum("POST" in args for args in calls) == 1
        assert json.loads((tmp_path / "posted.json").read_text())["body"].encode() == original
        assert body.read_bytes() != original
    else:
        assert result.returncode == 2
        assert calls == [] and not (tmp_path / "posted.json").exists()
        assert "privacy scan" in result.stderr
    assert not list((home / "tmp").glob("issue-body-*"))
