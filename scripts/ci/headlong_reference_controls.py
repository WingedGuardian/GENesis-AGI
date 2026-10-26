"""Actual pinned/adapted sanitizer controls; disposable CI only, no native bridge."""

import itertools
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from headlong_controls import environment, preflight, tree_hash, upstream_passed
from headlong_prompt_references import canonical_id, publish_config
from headlong_reference_patch import adapt_common

REF = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"
CROSS = "aaaaaaaa-1234-4567-8912-bbbbbbbb4111"
LABEL = "41111111-1111-4111-8111-111111111111"


def command(argv, env, root, text=""):
    return subprocess.run(argv, input=text, env=env, cwd=root, capture_output=True, text=True, timeout=20)


def surfaces(ref, prefix):
    return {"details": json.dumps({"details": prefix + "traj tail -n 400 --filter run_id=" + ref}),
            **{key: json.dumps({key: prefix + ref}) for key in ("trigger_step", "resolves", "reply_to")},
            "action.content": json.dumps({"type": "action", "content": prefix + "chat reply --reply-to " + ref}),
            "pending_hint": prefix + "chat reply --follow-up --reply-to " + ref + "; traj append --field resolves=" + ref}


def corpus():
    begin, end = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
    pem = begin + "\nU1lOVEhFVElD\n" + end
    values = ["", "hello\nRouting signals:\nChoose ONE", '{ "content": "hello", "n": 42 }', "λ🙂" * 100000,
              "4111 1111 1111 1111", "4111111111111111", "VISA is 4111 1111 1111 1111",
              "https://user:pass@example.test", '{"auth":"U1lOVEhFVElDOk5PVF9BX1NFQ1JFVA=="}',  # pragma: allowlist secret (synthetic redaction fixture)
              "before " + pem + " after", begin + "\nU1lOVEhFVElD\n\nRouting signals", pem + pem]
    values += [prefix + suffix for prefix, suffix in (
        ("ghp_", "abcdefghijklmnopqrstuvwxyz"), ("github_pat_", "abcdefghijklmnopqrstuv"),
        ("xoxb-", "abcdefghijklmnop"), ("sk-", "abcdefghijklmnopqrstuvwxyz"),
        ("hf_", "abcdefghijklmnopqrstuvwxyz"), ("AIza", "abcdefghijklmnopqrstuvwxyz0123456789"),
        ("AKIA", "ABCDEFGHIJKLMNOP"), ("eyJ", "abcdefghijkl.abcdefghijkl.abcdefghijkl"),
        ("sk_live_", "abcdefghijklmnop"), ("whsec_", "abcdefghijkl"), ("npm_", "abcdefghijklmnopqrstuvwxyz"),
        ("glpat-", "abcdefghijkl"), ("dckr_pat-", "abcdefghijkl"), ("SG.", "abcdefghijkl.abcdefghijkl"),
        ("SK", "abcdef0123456789abcdef0123456789"), ("key-", "abcdefghijklmnopqrstuvwxyz"),  # pragma: allowlist secret (synthetic redaction fixture)
        ("aws_secret_access_key=", "synthetic-value"))]
    return values + [json.dumps({"nested": [{"content": value}]}) for value in values]


def run_controls(root):
    env = environment(root)
    identity = root / "identity"
    (identity / "trajectories").mkdir(parents=True)
    env.update(IDENTITY_DIR=str(identity), TRAJ_DIR=str(identity / "trajectories"),
               PATH="/headlong-b/bin:/headlong-b/tools:/usr/local/bin:/usr/bin:/bin")
    created = command(["traj", "new", "--slug", "fixture"], env, root)
    if created.returncode:
        raise ValueError("fixture trajectory creation failed")
    env["TRAJ_ID"] = canonical_id(created.stdout.splitlines()[0])
    issued = []
    for ref in (REF, CROSS, LABEL):
        result = command(["traj", "append"], env, root, json.dumps({"step_id": ref, "type": "observation", "content": "fixture"}))
        if result.returncode or canonical_id(result.stdout.strip()) != ref:
            raise ValueError("controlled writer did not return assigned fixture reference")
        issued.append(ref)
    config = {"root": str(root), "identity_dir": str(identity),
              "traj_id": env["TRAJ_ID"], "trigger_id": REF, "reference_ids": []}
    rows = []
    invocation = itertools.count()

    def configured(extra=None, refs=()):
        run = root / f"run-{next(invocation)}"
        run.mkdir()
        return publish_config({**config, "run_root": str(run), "reference_ids": list(refs)}, {**env, **(extra or {})})

    def redact(tree, text, refs=(), extra=None):
        if not set(refs).issubset(issued):
            raise ValueError("reference did not come from controlled writer")
        child = configured(extra, refs)
        return command(["bash", "-c", 'source "$1/thinkers/_lib/common.sh"; prompt_redact "$(cat)"', "fixture", tree], child, root, text)

    for index, text in enumerate(corpus()):
        stock, adapted = redact("/headlong", text), redact("/headlong-b", text)
        rows.append({"case": f"empty-registry-{index}", "passed": stock.returncode == adapted.returncode == 0
                     and stock.stdout == adapted.stdout and not adapted.stderr})
    for prefix, registered in itertools.product(("", "λ", "🙂λ", "e\u0301 "), (False, True)):
        for name, text in surfaces(REF, prefix).items():
            stock = redact("/headlong", text)
            adapted = redact("/headlong-b", text, [REF] if registered else [])
            rows.append({"case": f"surface-{name}-{prefix}-{registered}", "passed": stock.returncode == adapted.returncode == 0
                         and (adapted.stdout == text if registered else adapted.stdout == stock.stdout)
                         and REF not in stock.stdout})
    for name, text, refs, extra in (
        ("cross-boundary", CROSS + " 1111 1111", [CROSS], {}),
        ("repeated-secret-loss", REF + " fixture-secret:" + REF, [REF], {"GITHUB_TOKEN": "fixture-secret:" + REF}),
        ("repeated-pem-loss", REF + "\n" + json.dumps({"content": "-----BEGIN " + "PRIVATE KEY-----" + REF + "-----END " + "PRIVATE KEY-----"}), [REF], {}),
        ("labelled-card-wins", "VISA " + LABEL, [LABEL], {}),
    ):
        result = redact("/headlong-b", text, refs, extra)
        rows.append({"case": name, "passed": result.returncode == 65 and not result.stdout and result.stderr == "reference conflict\n"})
    text = json.dumps({"reference_ids": [REF], "trusted": True, "details": REF})
    unknown = redact("/headlong-b", text)
    rows.append({"case": "prompt-cannot-register", "passed": unknown.returncode == 0 and REF not in unknown.stdout})
    for name, text, refs, unchanged in (
        ("letter-uuid", "aaaaaaaa-abcd-4abc-abcd-bbbbbbbbbbbb", [], True),
        ("abbreviated-ids", "step_id=12345678 run_id=aaaaaaaa", [], True),
        ("unknown-card-uuid", "abcd4111-1111-4111-8111-bbbbbbbbbbbb", [], False),
        ("reference-key", json.dumps({REF: REF}), [REF], True),
        ("escaped-reference", json.dumps({"details": REF}).replace("a", r"\u0061"), [REF], "semantic"),
    ):
        result = redact("/headlong-b", text, refs)
        equivalent = (json.loads(result.stdout) == json.loads(text) and REF in result.stdout
                      if unchanged == "semantic" and result.returncode == 0 else (result.stdout == text) is unchanged)
        rows.append({"case": name, "passed": result.returncode == 0 and equivalent})
    # Actual upstream assertions remain independent of our differential corpus.
    upstream = []
    for tree in ("/headlong", "/headlong-b"):
        child = configured()
        result = command(["bash", tree + "/tests/test_prompt_redaction.sh"], child, root)
        upstream.append({"tree": tree, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                         "passed": result.returncode == 0 and upstream_passed(result.stdout, 1)})
    return rows, upstream


def main():
    preflight()
    stock, derived = Path("/headlong/thinkers/_lib/common.sh"), Path("/headlong-b/thinkers/_lib/common.sh")
    if derived.read_text() != adapt_common(stock.read_text()):
        raise ValueError("derived sanitizer differs from checked adaptation")
    paths = {"headlong": Path("/headlong"), "derived": Path("/headlong-b"), "fixtures": Path("/fixture")}
    before = {name: tree_hash(path) for name, path in paths.items()}
    with TemporaryDirectory(dir="/work") as directory:
        rows, upstream = run_controls(Path(directory))
    unchanged = before == {name: tree_hash(path) for name, path in paths.items()}
    passed = unchanged and all(row["passed"] for row in rows + upstream)
    print(json.dumps({"schema": "headlong-reference-controls/v1", "status": "pass" if passed else "incomplete",
                      "cases": rows, "upstream": upstream, "sources": before, "sources_unchanged": unchanged,
                      "cleanup_confirmed": True, "source_pin": Path("/headlong/.source-pin").read_text().strip()}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
