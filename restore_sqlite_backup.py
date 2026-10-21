"""Restore a SQLite tar.gz webhook backup without overwriting live data by default."""
import argparse
import os
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import tarfile
import tempfile

from dotenv import load_dotenv

PART_RE = re.compile(r"^Zyven-data-(.+)\.tar\.gz\.part-(\d{4})-of-(\d{4})$")
MAX_PARTS = 1024
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 32
MAX_DOCUMENTS = 100_000


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", nargs="+", type=Path, help="Downloaded numbered webhook part files")
    parser.add_argument("--output", type=Path, default=Path(os.getenv("SQLITE_PATH", "data/zyven.sqlite3")))
    parser.add_argument("--force", action="store_true", help="Replace an existing database (make a copy first)")
    args = parser.parse_args()

    indexed = []
    backup_ids = set()
    expected_counts = set()
    if len(args.parts) > MAX_PARTS:
        parser.error(f"Too many backup parts; maximum is {MAX_PARTS}")
    for path in args.parts:
        match = PART_RE.match(path.name)
        if not match:
            parser.error(f"Unexpected backup part filename: {path.name}")
        backup_id, part_number, part_count = match.groups()
        backup_ids.add(backup_id)
        expected_counts.add(int(part_count))
        indexed.append((int(part_number), path))
    if len(backup_ids) != 1 or len(expected_counts) != 1:
        parser.error("All files must belong to the same Backup ID and part count")
    count = expected_counts.pop()
    if count < 1 or count > MAX_PARTS:
        parser.error(f"Backup part count must be between 1 and {MAX_PARTS}")
    indexed.sort()
    if [number for number, _ in indexed] != list(range(1, count + 1)):
        parser.error(f"Backup is incomplete; expected all {count} parts in this Backup ID")
    if args.output.exists() and not args.force:
        parser.error(f"{args.output} already exists; refusing to overwrite it. Use --force only after making a copy.")

    compressed_size = 0
    with tempfile.TemporaryDirectory(prefix="zyven-restore-input-") as folder:
        compressed = Path(folder) / "backup.tar.gz"
        for _, path in indexed:
            try:
                part_size = path.stat().st_size
            except OSError as error:
                raise SystemExit(f"Could not stat backup part {path}: {error}") from error
            if part_size > MAX_PART_BYTES:
                raise SystemExit(f"Backup part {path.name} exceeds the size limit")
            compressed_size += part_size
            if compressed_size > MAX_COMPRESSED_BYTES:
                raise SystemExit("Backup exceeds the compressed size limit")
            with path.open("rb") as source, compressed.open("ab") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)

        with tempfile.TemporaryDirectory(prefix="zyven-restore-db-") as output_folder:
            extracted = Path(output_folder) / "zyven.sqlite3"
            found = False
            with tarfile.open(compressed, mode="r:gz") as archive:
                members_seen = 0
                for item in archive:
                    members_seen += 1
                    if members_seen > MAX_ARCHIVE_MEMBERS:
                        raise SystemExit("Archive contains too many members")
                    if item.name != "zyven.sqlite3":
                        continue
                    if found or not item.isfile():
                        raise SystemExit("Archive contains an invalid database member")
                    found = True
                    source = archive.extractfile(item)
                    if source is None:
                        raise SystemExit("Could not read database from archive")
                    written = 0
                    with extracted.open("wb") as target:
                        while True:
                            chunk = source.read(min(1024 * 1024, MAX_DATABASE_BYTES - written + 1))
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_DATABASE_BYTES:
                                raise SystemExit("Restored database exceeds the uncompressed size limit")
                            target.write(chunk)
            if not found:
                raise SystemExit("Archive does not contain a regular zyven.sqlite3 database")

            from core.sqlite_store import _validate_document_row
            with sqlite3.connect(extracted) as connection:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"documents", "mongo_outbox"}
                missing = required - tables
                if missing:
                    raise SystemExit(f"SQLite schema validation failed; missing table(s): {', '.join(sorted(missing))}")
                document_count = 0
                for collection, document_id, payload in connection.execute("SELECT collection,id,payload FROM documents"):
                    document_count += 1
                    if document_count > MAX_DOCUMENTS:
                        raise SystemExit("Restored database exceeds the document limit")
                    try:
                        _validate_document_row(collection, document_id, payload)
                    except (ValueError, TypeError, EOFError, pickle.UnpicklingError, sqlite3.Error) as error:
                        raise SystemExit(f"Restored document validation failed: {error}") from error
            if not check or check[0] != "ok":
                raise SystemExit(f"SQLite integrity check failed: {check[0] if check else 'no result'}")

            args.output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix="zyven-restore-", suffix=".sqlite3", dir=args.output.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                shutil.copyfile(extracted, temporary)
                os.replace(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
    print(f"Restored Backup ID {next(iter(backup_ids))} to {args.output}")


if __name__ == "__main__":
    main()
