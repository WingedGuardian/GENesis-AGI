"""Adopted direct literal argv census; deliberately no dynamic-call claim."""
import ast
from pathlib import Path


def marked_environment(call):
    env = next((keyword.value for keyword in call.keywords if keyword.arg == 'env'), None)
    return (
        isinstance(env, ast.Dict) and len(env.keys) == 2 and env.keys[0] is None
        and ast.dump(env.values[0]) == ast.dump(ast.parse('os.environ', mode='eval').body)
        and isinstance(env.keys[1], ast.Constant) and env.keys[1].value == 'GENESIS_AUTOMATED_PUSH'
        and isinstance(env.values[1], ast.Constant) and env.values[1].value == '1'
    )


def census(root, overrides=None):
    rows = []
    paths = sorted(Path(root).rglob('*.py'))
    for path in paths:
        source = (overrides or {}).get(path, path.read_text(encoding='utf-8'))
        tree = ast.parse(source, filename=str(path))  # Parse errors are fatal.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args = node.args
            argv = args[0].elts if args and isinstance(args[0], (ast.List, ast.Tuple)) else args
            words = [value.value if isinstance(value, ast.Constant) else None for value in argv]
            if words[:2] == ['git', 'push']:
                rows.append({'path': str(path.relative_to(root)), 'line': node.lineno, 'callee': ast.unparse(node.func), 'automated_inherited_env': marked_environment(node)})
    return {'files_parsed': len(paths), 'callers': rows, 'parse_errors': [], 'scope': 'Every src Python file parsed; fixed direct literal git/push argv prefix in list/tuple or variadic calls only. No dynamic argv, aliases, shell strings, APIs, or external scripts completeness claim.'}


def test_all_direct_literal_git_push_callers_are_automated():
    report = census(Path(__file__).resolve().parents[2] / "src")
    assert report["callers"], "direct publisher census unexpectedly empty"
    assert all(row["automated_inherited_env"] for row in report["callers"]), report
