"""Should the deploy-backups prune keep this live-move repair report folder?

``python3 -I -S repair_report_keep.py <dir>`` prints why it keeps the folder and
exits 0, or prints nothing and exits 3 when the prune may take it as before.
Every other exit (a crash exits 1, a usage error 2) keeps it: only an answer
this reader gave lets a folder go.
disk_hygiene.sh's prune_backup_runs asks this for each deploy-backups folder
older than its 90 days (#4331).

A report folder holds the only saved copy of edits a live-move repair moved out
of the checkout. It is kept while deploy health would still count its saved
paths: not dismissed, not cleared by a restore (the rule of deploy_health
``_restore_counts``, held equal by a test), and holding at least one foreign
row. A report this reader cannot read is kept too, and named, until --dismiss
writes DISMISS_MARK beside it (a dismissed report goes like any other): losing a saved
edit is the outcome a prune must never produce in silence. So is the restore
run folder a kept report's in-flight record names (``restore.inflight[0]``):
its ``aside/`` can hold the only copy of content a held restore did not write
(live_move_restore Judge.unresolved). Any other folder (another name, or no
REPAIR.json yet) prunes as before. A restore run folder named for its report
(from #4011) is kept for as long as that report's folder exists.

Stdlib only, run isolated: the prune runs it from the checkout under the daily
groom, before any venv is known to be sound.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys

#: The repair's report folder name (live_move_repair.REPORT_NAME).
REPORT_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+-live-move-repair")
#: A restore run's folder name (live_move_restore_runs.RUN_NAME): from #4011 it
#: carries its report's ts too.
RUN_NAME = re.compile(
    r"[0-9]{8}T[0-9]{6}Z-[0-9]+(?:-([0-9]{8}T[0-9]{6}Z-[0-9]+))?-live-move-restore"
)
#: live_move_repair.DISMISS_MARK: --dismiss's record for a report no reader reads.
DISMISS_MARK = "DISMISSED.json"
#: Exit codes: keep, and may prune (never 1, which an uncaught exception exits with).
KEEP, PRUNE = 0, 3
#: live_move_repair.REPORT_BYTES: a larger REPAIR.json is not the writer's.
REPORT_BYTES = 4 * 1024 * 1024


def reason(report: object) -> str | None:
    """Why a parsed REPAIR.json keeps its folder, or None when it may go."""
    if not isinstance(report, dict):
        return "its REPAIR.json is not a JSON object"
    if report.get("dismissed_at"):
        return None
    block = report.get("restore")
    if (
        isinstance(block, dict)
        and type(block.get("refused")) is int
        and block["refused"] == 0
        and isinstance(block.get("restored_at"), str)
    ):
        return None
    paths = report.get("paths")
    if not isinstance(paths, list):
        return "its REPAIR.json lists no paths"
    saved = sum(1 for r in paths if isinstance(r, dict) and r.get("class") == "foreign")
    if not saved:
        return None
    return f"an unresolved live-move repair report, {saved} saved path{'s' if saved != 1 else ''}"


def keep(folder: str) -> str | None:
    """Why ``folder`` is kept, or None when the prune may take it."""
    name = os.path.basename(folder.rstrip("/"))
    if m := RUN_NAME.fullmatch(name):
        root = os.path.dirname(folder.rstrip("/")) or "."
        if m.group(1):
            report = os.path.join(root, f"{m.group(1)}-live-move-repair")
            try:
                there = stat.S_ISDIR(os.lstat(report).st_mode)
            except FileNotFoundError:
                there = False
            except OSError:  # cannot tell: kept (#4364 review)
                return f"a restore run of the report {m.group(1)}-live-move-repair, which cannot be read"
            if there:
                # Tied to its report by name (#4011): it lives as long as the report.
                return f"a restore run of the report {m.group(1)}-live-move-repair, kept while it is"
        return _held_by(root, name)
    if not REPORT_NAME.fullmatch(name):
        return None
    return _report_keep(folder)


def _held_by(root: str, run: str) -> str | None:
    """Why the restore run folder ``run`` is kept: a report under ``root``
    that is itself kept names it as its in-flight run."""
    try:
        names = sorted(n for n in os.listdir(root) if REPORT_NAME.fullmatch(n))
    except OSError as exc:
        return f"its reports cannot be listed ({exc.strerror})"
    for name in names:
        if _marked(os.path.join(root, name)):
            continue
        report = _read(os.path.join(root, name))
        if isinstance(report, str):
            continue  # an unreadable report is kept itself; it names no run here
        block = report.get("restore") if isinstance(report, dict) else None
        inflight = block.get("inflight") if isinstance(block, dict) else None
        if isinstance(inflight, list) and inflight[:1] == [run] and reason(report):
            return f"the in-flight aside of the unresolved report {name}"
    return None


def _marked(folder: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(os.path.join(folder, DISMISS_MARK)).st_mode)
    except OSError:
        return False


def _report_keep(folder: str) -> str | None:
    if _marked(folder):
        return None  # dismissed though unreadable: goes like any dismissed report
    report = _read(folder)
    if report is None:
        return None
    if isinstance(report, str):
        return report
    return reason(report)


def _read(folder: str) -> object:
    """A report folder's parsed REPAIR.json; None when it has none yet; a str
    (why) when it cannot be read as the writer writes it."""
    try:
        fd = os.open(
            os.path.join(folder, "REPAIR.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"its REPAIR.json cannot be opened ({exc.strerror})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "its REPAIR.json is not a regular file"
        raw = os.read(fd, REPORT_BYTES + 1)
        while len(raw) <= REPORT_BYTES and (more := os.read(fd, REPORT_BYTES + 1 - len(raw))):
            raw += more
    except OSError as exc:
        return f"its REPAIR.json cannot be read ({exc.strerror})"
    finally:
        os.close(fd)
    if len(raw) > REPORT_BYTES:
        return f"its REPAIR.json is over {REPORT_BYTES} bytes"
    try:
        report = json.loads(raw)
    except (ValueError, RecursionError):
        return "its REPAIR.json is not JSON"
    return report if isinstance(report, dict) else "its REPAIR.json is not a JSON object"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: repair_report_keep.py <dir>", file=sys.stderr)
        return 2
    try:
        why = keep(argv[1])
    except BaseException as exc:  # noqa: BLE001 - anything but an answer keeps the folder
        print(f"its report could not be judged ({type(exc).__name__})")
        return KEEP
    if why is None:
        return PRUNE
    print(why)
    return KEEP


if __name__ == "__main__":
    sys.exit(main(sys.argv))
