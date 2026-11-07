"""Finite command boundary for copied uninstall tests; never use the real cron UID."""

import json
import os
import sys
from pathlib import Path


def owned_operand(raw):
    root = Path(os.environ["FIXTURE_ROOT"]).resolve(strict=True)
    if not raw or ".." in Path(raw).parts or raw.endswith("/"):
        raise ValueError("ambiguous fixture operand")
    addressed = Path(os.path.abspath(raw))
    # Preserve final-symlink unlink semantics, but never traverse an aliased parent.
    if not addressed.is_relative_to(root) or addressed == root:
        raise ValueError("operand outside owned fixture")
    if not addressed.parent.resolve().is_relative_to(root):
        raise ValueError("operand parent outside owned fixture")
    return addressed


def validate_rm(arguments):
    operands, options = [], True
    for arg in arguments:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-"):
            if arg not in ("-f", "-rf", "-r", "-fr"):
                raise ValueError("unadmitted rm option")
        else:
            operands.append(owned_operand(arg))
    if not operands:
        raise ValueError("rm requires owned operands")
    return operands  # every operand validated before any is forwarded


def main():
    command, *args = sys.argv[1:]
    tools = Path(__file__).parent
    with Path(os.environ["CALLS"]).open("a") as out:
        out.write("FIXTURE " + json.dumps([command, *args]) + "\n")
    if command == "crontab":
        store = Path(os.environ["FIXTURE_CRON"])
        if args == ["-l"]:
            sys.stdout.write(store.read_text())
        elif args == ["-"]:
            body = sys.stdin.read()
            with Path(os.environ["CALLS"]).open("a") as out:
                out.write("CRON_STDIN " + json.dumps(body) + "\n")
            store.write_text(body)
        else:
            raise ValueError("unadmitted crontab action")
    elif command == "sudo":
        if args[:1] == ["-n"]:
            args = args[1:]
        if not args or args[0] not in ("rm", "systemctl"):
            raise ValueError("unadmitted sudo action")
        os.execv(str(tools / args[0]), args)  # noqa: S606 - exact two owned shims
    elif command == "find":
        if args != [
            str(Path(os.environ["HOME"]) / ".genesis"),
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "!",
            "-name",
            "locks",
            "-print0",
        ]:
            raise ValueError("unadmitted find action")
        owned_operand(args[0])
        os.execv("/usr/bin/find", ["find", *args])  # noqa: S606 - fixed binary and owned read root
    elif command == "mktemp":
        if args != ["-p", os.environ["HOME"], ".genesis-uninstall.XXXXXXXX"]:
            raise ValueError("unadmitted mktemp action")
        owned_operand(args[1])
        if (
            not Path(args[1])
            .resolve(strict=True)
            .is_relative_to(Path(os.environ["FIXTURE_ROOT"]).resolve(strict=True))
        ):
            raise ValueError("mktemp directory outside owned fixture")
        os.execv("/usr/bin/mktemp", ["mktemp", *args])  # noqa: S606 - fixed owned template
    elif command == "mv":
        expected = [
            str(Path(os.environ["HOME"]) / ".bashrc.tmp"),
            str(Path(os.environ["HOME"]) / ".bashrc"),
        ]
        if args != expected:
            raise ValueError("unadmitted mv action")
        for arg in args:
            owned_operand(arg)
        if Path(args[1]).is_dir():
            raise ValueError("mv destination directory is not admitted")
        os.execv("/usr/bin/mv", ["mv", *args])  # noqa: S606 - exact owned pair
    else:
        raise ValueError("unadmitted shared-state command")


if __name__ == "__main__":
    main()
