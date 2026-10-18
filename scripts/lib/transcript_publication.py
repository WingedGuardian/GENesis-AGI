#!/usr/bin/env python3
"""Authenticated, installation-bound progress for one frozen offsite snapshot.

Backend operations stay in the foreground shell holding the DR lock. The session
file is disposable working state; only the encrypted, fenced journal authorizes
reuse across invocations. No remote acknowledgement is an integrity receipt.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import transcript_archive as archive

VERSION = 1
CHUNK = 1024 * 1024
_SMB_REFUSED = frozenset('";\\\r\n')


def check_backend_paths(backend, *paths):
    """Match backup_backends.sh _smb_paths_ok before freezing its arguments."""
    if backend == "smb":
        for value in paths:
            if not _SMB_REFUSED.isdisjoint(value):
                raise ValueError("backend: refusing smb path " + json.dumps(value))


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("publication input is not a regular file")
    return archive.fingerprint(path.stat())


def relative(value):
    if not isinstance(value, str):
        raise ValueError("invalid publication target")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute()
            or any(p in (".", "..") for p in path.parts) or str(path) != value
            or any(c in value for c in "\0\r\n")):
        raise ValueError("invalid publication target")
    return value


def integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid publication counter")
    return value


def write_json(path, document):
    # Unpublished scratch/session state only. A failed write stops this run;
    # the next invocation reconstructs authority from the encrypted journal.
    path.write_text(json.dumps(document, ensure_ascii=True))


class Publication:
    def __init__(self, spool, scratch, password, deadline):
        self.spool, self.scratch = Path(spool), Path(scratch)
        self.pending = self.spool / "pending"
        self.password, self.deadline = password, deadline
        if not math.isfinite(deadline):
            raise ValueError("invalid publication deadline")

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def reclaim(self):
        if self.spool.is_symlink():
            raise ValueError("publication spool is a symlink")
        archive._durable_directory(self.spool)
        archive._fsync_directory(self.spool)
        for path in self.spool.iterdir():
            if re.fullmatch(r"(?:retired-|\.freeze-)[a-f0-9]{32}", path.name):
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("invalid owned publication stage")
                shutil.rmtree(path)

    def checkpoint(self, doc):
        """A visible post-replace journal must be authenticated and fenced on open."""
        with tempfile.TemporaryDirectory(dir=self.scratch, prefix="publication-") as work:
            plain = Path(work) / "journal.json"
            write_json(plain, doc)
            fd, name = tempfile.mkstemp(dir=self.pending, prefix=".journal-")
            os.close(fd)
            stage = Path(name)
            try:
                archive.crypt(plain, stage, self.password)
                with stage.open("rb") as source:
                    os.fsync(source.fileno())
                os.replace(stage, self.pending / "journal.gpg")
                archive._fsync_directory(self.pending)
            finally:
                stage.unlink(missing_ok=True)
        doc["checkpoint_at"] = time.monotonic()
        doc["checkpoint_count"] = 0
        print("Publication checkpoint: " + doc["snapshot"] + " verified_bytes="
              + str(sum(r["verified"] for r in doc["rows"])), file=sys.stderr)

    def validate(self, doc, binding):
        if (set(binding) != {"backend", "root", "host", "scope", "installation"}
                or binding["backend"] not in ("local", "smb")
                or binding["scope"] not in ("main", "all")
                or not re.fullmatch(r"Genesis/[A-Za-z0-9._-]+", binding["host"])
                or binding["host"] in ("Genesis/.", "Genesis/..")
                or any(not isinstance(x, str) or not x for x in binding.values())):
            raise ValueError("invalid publication binding")
        check_backend_paths(binding["backend"], str(self.spool), str(self.scratch))
        if (not isinstance(doc, dict) or type(doc.get("version")) is not int
                or doc["version"] != VERSION or doc.get("binding") != binding
                or not re.fullmatch(r"Genesis/[A-Za-z0-9._-]+/[0-9]{8}T[0-9]{6}Z", doc.get("snapshot", ""))
                or doc["snapshot"].rsplit("/", 1)[0] != binding["host"]
                or not isinstance(doc.get("rows"), list)):
            raise ValueError("incompatible pending snapshot; retain for explicit resolution")
        if (any(type(doc.get(x)) is not bool for x in ("claimed", "marker_pending"))
                or doc.get("verifier") != archive._VALIDATION_CONTRACT
                or doc.get("vault_status") not in ("ok", "absent", "missing", "failed", "skipped")
                or not isinstance(doc.get("gaps"), list)
                or any(not isinstance(x, str) or "\n" in x or "\0" in x
                       or not x.startswith(("skipped ", "partial ")) for x in doc["gaps"])
                or not re.fullmatch(r"[a-f0-9]{64}", doc.get("index_digest", ""))):
            raise ValueError("invalid publication metadata")
        datetime.strptime(doc["snapshot"].rsplit("/", 1)[1], "%Y%m%dT%H%M%SZ")
        seen = set()
        for number, row in enumerate(doc["rows"]):
            if row["id"] != str(number) or row["remote"] in seen:
                raise ValueError("duplicate publication row")
            seen.add(relative(row["remote"]))
            pool = binding["host"] + "/transcript-objects/"
            if not (row["remote"].startswith(doc["snapshot"] + "/")
                    or (row["remote"].startswith(pool)
                        and archive._POOL_OBJECT.fullmatch(row["remote"][len(pool):]))):
                raise ValueError("publication row escapes its snapshot")
            for field in ("size", "received", "verified", "generation"):
                integer(row[field])
            if not 0 <= row["verified"] <= row["received"] <= row["size"]:
                raise ValueError("invalid publication byte coverage")
            if any(type(row[x]) is not bool for x in ("required", "put", "receipt", "failed")):
                raise ValueError("invalid publication state")
            if row["receipt"] and row["verified"] != row["size"]:
                raise ValueError("incomplete publication receipt")
            if not re.fullmatch(r"[a-f0-9]{64}", row["digest"]):
                raise ValueError("invalid frozen digest")
            if row["role"] not in ("metadata", "core", "extra", "pool"):
                raise ValueError("invalid publication role")
            for field in ("source_stat", "frozen", "download_stat"):
                value = row[field]
                if value is None and field != "source_stat":
                    continue
                if not isinstance(value, list) or len(value) != 5 or any(type(v) is not int for v in value):
                    raise ValueError("invalid publication fingerprint")
            if row["frozen"] is None and (row["role"] != "pool" or row["receipt"]):
                raise ValueError("required input was not frozen")
            if row["frozen"] is not None and regular(self.frozen(row)) != row["frozen"]:
                raise ValueError("frozen publication input changed")
            # Old failed extras are terminal gaps, never another transfer.
            if not (row["role"] == "extra" and not row["required"] and row["failed"]):
                check_backend_paths(binding["backend"], row["remote"])

    def open(self, binding, captures):
        self.reclaim()
        if self.pending.is_symlink() or not self.pending.is_dir():
            raise ValueError("invalid pending publication directory")
        with tempfile.TemporaryDirectory(dir=self.scratch, prefix="publication-") as work:
            plain = Path(work) / "journal.json"
            archive.crypt(self.pending / "journal.gpg", plain, self.password, decrypt=True)
            doc = json.loads(plain.read_bytes())
        self.validate(doc, binding)
        archive._fsync_directory(self.pending)
        # A journal cannot waive retained history or accept a manually changed index.
        index = Path(captures) / archive._INDEX_NAME
        archive.capture_index(Path(captures), self.scratch, self.password)
        if archive.checksum(index) != doc["index_digest"]:
            raise ValueError("capture index changed during pending publication")
        for row in doc["rows"]:
            if row["receipt"] or row["failed"]:
                continue
            download = self.download(row)
            try:
                current = regular(download)
                saved = row["download_stat"]
                # Native reget appends; a killed transfer may have appended an
                # uncheckpointed suffix while the verified prefix is unchanged.
                valid = (saved is not None and current[:2] == saved[:2]
                         and (current == saved or current[2] > saved[2]))
            except (OSError, ValueError):
                valid = False
            if not valid:
                self.reset_download(row)
            else:
                with download.open("r+b") as received:
                    received.truncate(row["received"])
                    received.flush()
                    os.fsync(received.fileno())
                row["download_stat"] = regular(download)
        doc["checkpoint_at"] = time.monotonic()
        doc["checkpoint_count"] = 0
        doc["first_progress"] = True
        return doc

    def frozen(self, row):
        return self.pending / "frozen" / row["id"]

    def download(self, row):
        return self.pending / "downloads" / row["id"]

    def reset_download(self, row):
        self.download(row).unlink(missing_ok=True)
        row.update(received=0, verified=0, download_stat=None, receipt=False,
                   generation=row["generation"] + 1)

    def freeze_row(self, row):
        if self.remaining() <= 15:
            raise TimeoutError("backup window exhausted while freezing")
        source = Path(row["source"])
        before = regular(source)
        if row["source_stat"] != before:
            raise ValueError("accepted source changed before freeze")
        target = self.frozen(row)
        stage = target.with_suffix(".new")
        try:
            shutil.copyfile(source, stage)
            if (regular(source) != before or stage.stat().st_size != row["size"]
                    or archive.checksum(stage) != row["digest"]):
                raise ValueError("source changed while freezing publication")
            with stage.open("rb") as incoming:
                os.fsync(incoming.fileno())
            os.replace(stage, target)
            archive._fsync_directory(target.parent)
            row["frozen"] = regular(target)
        finally:
            stage.unlink(missing_ok=True)

    def create(self, binding, snapshot, backup, qdrant, extras, *, vault=("absent", "", "")):
        """Freeze core before authority; transcript copies then progress by row."""
        if self.pending.exists() or self.pending.is_symlink():
            raise ValueError("pending publication already exists")
        check_backend_paths(binding["backend"], str(self.spool), str(self.scratch))
        backup = Path(backup).resolve()
        self.reclaim()
        stage = self.spool / (".freeze-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        (stage / "frozen").mkdir()
        (stage / "downloads").mkdir()
        active = self.pending
        self.pending = stage
        try:
            manifest = stage / "manifest.gpg"
            rows = archive.pool_manifest(backup / "transcripts", manifest, snapshot,
                                         self.scratch, self.password)
            marker = stage / "TRANSCRIPT_POOL"
            marker.write_bytes(b"genesis-transcript-pool 1\n")
            doc = dict(version=VERSION, binding=binding, snapshot=snapshot,
                       verifier=archive._VALIDATION_CONTRACT,
                       vault_status=vault[0],
                       index_digest=archive.checksum(backup / "transcripts" / archive._INDEX_NAME),
                       rows=[], claimed=False, marker_pending=False,
                       gaps=[], checkpoint_at=time.monotonic(), checkpoint_count=0,
                       first_progress=True)

            def add(source, remote, role="core", required=True, digest=None, size=None):
                remote = relative(remote)
                check_backend_paths(binding["backend"], remote)
                source = Path(source)
                info = regular(source)
                if size is not None and info[2] != size:
                    raise ValueError("accepted capture size changed")
                digest = digest or archive.checksum(source)
                if any(r["remote"] == remote for r in doc["rows"]):
                    existing = next(r for r in doc["rows"] if r["remote"] == remote)
                    if existing["digest"] != digest or existing["size"] != info[2]:
                        raise ValueError("conflicting publication targets")
                    return
                row = dict(id=str(len(doc["rows"])), source=str(source), source_stat=info,
                           remote=remote, role=role, required=required,
                           size=info[2], digest=digest, frozen=None, put=role != "pool",
                           received=0, verified=0, generation=0, download_stat=None,
                           receipt=False, failed=False)
                doc["rows"].append(row)
                if role != "pool":
                    self.freeze_row(row)

            add(marker, snapshot + "/TRANSCRIPT_POOL", "metadata")
            add(manifest, snapshot + "/transcripts/manifest-v1.json.gpg", "metadata")
            add(backup / "data/genesis.sql.gpg", snapshot + "/data/genesis.sql.gpg")
            for collection in qdrant.split():
                name = collection + ".snapshot.gpg"
                add(backup / "data/qdrant" / name, snapshot + "/qdrant/" + name)
            for directory, pattern, recursive in (("memory", "*.gpg", False),
                    ("eval", "*.gpg", True), ("config_overrides", "*.local.yaml", False),
                    ("creds", "*.gpg", True), ("secrets", "secrets.env.gpg", False)):
                root = backup / directory
                for source in sorted(root.rglob(pattern) if recursive else root.glob(pattern)):
                    relative_name = source.relative_to(root).as_posix()
                    if directory == "creds" and relative_name == vault[1] and vault[0] != "ok":
                        relative_name = relative(vault[2])
                    add(source, snapshot + "/" + directory + "/" + relative_name)
            for name in extras:
                relative(name)
                if "/" in name:
                    raise ValueError("invalid extra archive name")
                previous = len(doc["rows"])
                try:
                    add(backup / "extra" / name, snapshot + "/extra/" + name, "extra", False)
                except (OSError, ValueError):
                    del doc["rows"][previous:]
                    doc["gaps"].append("skipped " + name + " (freeze failed)")
            local_manifest = backup / ".extra-manifest"
            if local_manifest.exists():
                doc["gaps"].extend(line for line in local_manifest.read_text().split("\n")
                                   if line.startswith(("skipped ", "partial ")))
            for name, object_id, size in rows:
                add(backup / "transcripts" / name,
                    binding["host"] + "/transcript-objects/" + object_id,
                    "pool", digest=archive._POOL_OBJECT.fullmatch(object_id).group(2), size=size)
            self.validate(doc, binding)
            self.checkpoint(doc)
            os.rename(stage, active)
            archive._fsync_directory(self.spool)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        finally:
            self.pending = active
        # Sources for metadata were unpublished stages; their frozen files are authority.
        return doc

    def compare(self, doc, row, transfer_ok):
        download = self.download(row)
        if regular(self.frozen(row)) != row["frozen"]:
            raise ValueError("frozen publication input changed")
        try:
            end = regular(download)[2]
            present = True
        except (OSError, ValueError):
            end = 0
            present = False
        if end > row["size"] or end < row["verified"]:
            self.reset_download(row)
            row["put"] = True
            return
        before = row["verified"]
        if end:
            with self.frozen(row).open("rb") as source, download.open("rb") as received:
                source.seek(before)
                received.seek(before)
                while row["verified"] < end and self.remaining() > 15:
                    count = min(CHUNK, end - row["verified"])
                    if source.read(count) != received.read(count):
                        self.reset_download(row)
                        row["put"] = True
                        return
                    row["verified"] += count
                os.fsync(received.fileno())
            row["received"] = end
            row["download_stat"] = regular(download)
        row["receipt"] = (present and (row["size"] > 0 or transfer_ok)
                          and row["verified"] == row["size"] and end == row["size"])
        if not row["receipt"] and row["verified"] == before:
            # No new verified bytes: retry the exact frozen upload next invocation.
            row["put"] = True
        if not row["required"] and not row["receipt"]:
            row["failed"] = True
        if row["verified"] > before or row["receipt"]:
            doc["checkpoint_count"] += 1
            if (doc.pop("first_progress", False)
                    or doc["checkpoint_count"] >= archive._CHECKPOINT_OBJECTS
                    or time.monotonic() - doc["checkpoint_at"] >= archive._CHECKPOINT_SECONDS):
                self.checkpoint(doc)

    def marker(self, doc):
        if any(not r["receipt"] for r in doc["rows"] if r["required"]):
            raise ValueError("required publication receipts incomplete")
        lines = ["genesis-snapshot 1", "transcript-pool 1", *doc["gaps"]]
        if doc["vault_status"] not in ("ok", "absent"):
            lines.append("payment_vault " + doc["vault_status"])
        for row in doc["rows"]:
            if row["role"] == "extra":
                name = row["remote"].rsplit("/", 1)[1]
                lines.append(("extra " + name) if row["receipt"] else ("skipped " + name + " (upload failed)"))
        return ("\n".join(lines) + "\n").encode()

    def retire(self, doc, received):
        if received.read_bytes() != self.marker(doc):
            raise ValueError("final COMPLETE differs from the original snapshot")
        target = self.spool / ("retired-" + uuid.uuid4().hex)
        os.rename(self.pending, target)
        archive._fsync_directory(self.spool)
        shutil.rmtree(target)

    def namespace(self, doc):
        directories = {doc["snapshot"], *(doc["snapshot"] + "/" + name
                                        for name in ("data", "qdrant", "transcripts"))}
        for row in doc["rows"]:
            if row["remote"].startswith(doc["snapshot"] + "/"):
                parent = PurePosixPath(row["remote"]).parent
                while str(parent).startswith(doc["snapshot"]):
                    directories.add(str(parent))
                    if str(parent) == doc["snapshot"]:
                        break
                    parent = parent.parent
        return sorted(directories)

    def check_listing(self, doc, directory, names):
        if directory not in self.namespace(doc):
            raise ValueError("unexpected publication directory")
        paths = [r["remote"] for r in doc["rows"]] + self.namespace(doc)
        allowed = {PurePosixPath(p).name for p in paths
                   if str(PurePosixPath(p).parent) == directory}
        if directory == doc["snapshot"]:
            allowed.add("COMPLETE")
        # The existing strict backend emits one LF for an empty directory.
        for name in filter(None, names):
            if name not in allowed and not archive._POOL_PARTIAL.fullmatch(name):
                raise ValueError("unexpected content in pending remote snapshot: " + repr(directory + "/" + name))

    def next(self, doc):
        if not doc["claimed"]:
            return ["claim", doc["snapshot"]]
        for row in doc["rows"]:
            if row["receipt"] or row["failed"]:
                continue
            if row["frozen"] is None:
                self.freeze_row(row)
                self.checkpoint(doc)
            operation = "put" if row["put"] else "get"
            if row["received"] > row["verified"]:
                operation = "compare"
            return [operation, row["id"], str(self.frozen(row)),
                    row["remote"], str(self.download(row))]
        marker = self.pending / "COMPLETE"
        marker.write_bytes(self.marker(doc))
        doc["marker_pending"] = True
        self.checkpoint(doc)
        return ["marker", str(marker), doc["snapshot"] + "/COMPLETE"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "open", "next", "claim", "put", "compare",
                                          "checkpoint", "retire", "summary", "directories",
                                          "check-list", "marker", "gaps"))
    for name in ("spool", "scratch", "session", "binding"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--backup")
    parser.add_argument("--snapshot")
    parser.add_argument("--qdrant", default="")
    parser.add_argument("--extras")
    parser.add_argument("--vault-status", default="absent")
    parser.add_argument("--vault-archive", default="")
    parser.add_argument("--vault-last-good", default="")
    parser.add_argument("--row", type=int)
    parser.add_argument("--ok", action="store_true")
    parser.add_argument("--received")
    parser.add_argument("--directory")
    parser.add_argument("--listing")
    args = parser.parse_args()
    engine = Publication(args.spool, args.scratch, sys.stdin.buffer.read(), args.deadline)
    binding = json.loads(Path(args.binding).read_bytes())
    session = Path(args.session)
    if args.action == "create":
        extras = Path(args.extras).read_bytes().split(b"\0") if args.extras else []
        doc = engine.create(binding, args.snapshot, args.backup, args.qdrant,
                            [os.fsdecode(x) for x in extras if x],
                            vault=(args.vault_status, args.vault_archive, args.vault_last_good))
    elif args.action == "open":
        doc = engine.open(binding, Path(args.backup) / "transcripts")
    else:
        doc = json.loads(session.read_bytes())
        if args.action == "next":
            for value in engine.next(doc):
                sys.stdout.buffer.write(os.fsencode(value) + b"\0")
        elif args.action == "claim":
            doc["claimed"] = True
            engine.checkpoint(doc)
        elif args.action == "put":
            row = doc["rows"][args.row]
            if args.ok:
                row["put"] = False
            elif not row["required"]:
                row["failed"] = True
            if not args.ok:
                print("required" if row["required"] else "optional")
        elif args.action == "compare":
            engine.compare(doc, doc["rows"][args.row], args.ok)
        elif args.action == "checkpoint":
            engine.checkpoint(doc)
        elif args.action == "retire":
            engine.retire(doc, Path(args.received))
        elif args.action == "directories":
            for directory in engine.namespace(doc):
                sys.stdout.buffer.write(os.fsencode(directory) + b"\0")
        elif args.action == "check-list":
            engine.check_listing(doc, args.directory,
                                 [os.fsdecode(name) for name in Path(args.listing).read_bytes().split(b"\n")])
        elif args.action == "marker":
            (engine.pending / "COMPLETE").write_bytes(engine.marker(doc))
            doc["marker_pending"] = True
            engine.checkpoint(doc)
        elif args.action == "gaps":
            gaps = [x.split(" ", 1) for x in doc["gaps"]]
            gaps += [("upload-failed", r["remote"].rsplit("/", 1)[1] + " (upload failed)")
                     for r in doc["rows"] if r["role"] == "extra" and r["failed"]]
            for pair in gaps:
                for value in pair:
                    sys.stdout.buffer.write(os.fsencode(value) + b"\0")
        elif args.action == "summary":
            print(json.dumps(dict(snapshot=doc["snapshot"],
                  phase=("complete" if not engine.pending.exists() else
                         "initializing" if not doc["claimed"] else
                         "acknowledging" if doc["marker_pending"] else "verifying"),
                  vault_status=doc["vault_status"],
                  age_s=max(0, int(time.time() - datetime.strptime(doc["snapshot"].rsplit("/", 1)[1],
                        "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp())),
                  verified_bytes=sum(r["verified"] for r in doc["rows"]),
                  total_bytes=sum(r["size"] for r in doc["rows"]),
                  verified_objects=sum(r["receipt"] for r in doc["rows"]),
                  total_objects=len(doc["rows"]))))
    write_json(session, doc)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.CalledProcessError) as exc:
        print("Publication incomplete: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None
