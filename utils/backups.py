"""Source archives and one-time import of legacy MongoDB SQLite snapshots."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import asyncio
import gzip
import os
import pickle
import shutil
import sqlite3
import zlib
import tempfile
import zipfile

import aiohttp
import discord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "backups", "logs", "data"}
EXCLUDED_NAMES = {".env", "credentials.json", "secrets.json", "token.json"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".pem", ".key", ".sqlite", ".sqlite3", ".db", ".zip", ".gz", ".tgz", ".7z", ".rar"}
DATABASE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
SOURCE_SUFFIXES = {
    ".py", ".json", ".toml", ".yaml", ".yml", ".md", ".txt", ".ini", ".cfg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
}
MAX_SNAPSHOT_PARTS = 1024
MAX_SNAPSHOT_PART_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_DOCUMENTS = 100_000


def _configured_database_artifacts() -> set[Path]:
    """Return the configured SQLite file and its journaling sidecars."""
    configured = os.getenv("SQLITE_PATH", "data/zyven.sqlite3").strip()
    if not configured:
        return set()
    database = Path(configured).expanduser()
    if not database.is_absolute():
        database = PROJECT_ROOT / database
    database = database.resolve(strict=False)
    return {database, Path(f"{database}-wal"), Path(f"{database}-shm"), Path(f"{database}-journal")}


def _is_source_file(path: Path, relative: Path, database_artifacts: set[Path]) -> bool:
    """Allow only source/config/assets and never SQLite databases or sidecars."""
    resolved = path.resolve(strict=False)
    if resolved in database_artifacts:
        return False
    name = path.name.lower()
    suffix = path.suffix.lower()
    if any(name.endswith(item) for item in ("-wal", "-shm", "-journal")):
        base = name.rsplit("-", 1)[0]
        if base.endswith(DATABASE_SUFFIXES):
            return False
    if suffix in EXCLUDED_SUFFIXES or any(name.endswith(item) for item in (".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")):
        return False
    if name in EXCLUDED_NAMES or (name.startswith(".env.") and name != ".env.example"):
        return False
    return name in {".env.example", "dockerfile", ".dockerignore", ".gitignore"} or suffix in SOURCE_SUFFIXES


def build_source_zip() -> tuple[str, bytes]:
    """Return a timestamped ZIP containing an allowlisted source tree only."""
    created_at = datetime.now(timezone.utc)
    filename = f"Zyven-source-backup-{created_at:%Y%m%d-%H%M%S-UTC}.zip"
    output = BytesIO()
    database_artifacts = _configured_database_artifacts()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in PROJECT_ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(PROJECT_ROOT)
            if any(part in EXCLUDED_DIRS for part in relative.parts):
                continue
            if not _is_source_file(path, relative, database_artifacts):
                continue
            archive.write(path, relative.as_posix())
    return filename, output.getvalue()


async def send_source_backup(webhook_url: str) -> tuple[str, int]:
    """Build and upload the source ZIP in small parts. Never log the webhook URL."""
    filename, content = await asyncio.to_thread(build_source_zip)
    chunk_size = 8 * 1024 * 1024
    chunks = [content[offset:offset + chunk_size] for offset in range(0, len(content), chunk_size)] or [b""]
    timeout = aiohttp.ClientTimeout(total=120, connect=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        webhook = discord.Webhook.from_url(webhook_url, session=session)
        for index, chunk in enumerate(chunks, start=1):
            part_filename = filename if len(chunks) == 1 else f"{filename}.part-{index:04d}-of-{len(chunks):04d}"
            upload = discord.File(BytesIO(chunk), filename=part_filename)
            try:
                content_note = f"Zyven full source backup • {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"
                if len(chunks) > 1:
                    content_note += f"\nPart {index}/{len(chunks)} • Join attachments in numeric order to reconstruct the ZIP."
                await webhook.send(content=content_note, file=upload, allowed_mentions=discord.AllowedMentions.none())
            finally:
                upload.close()
    return filename, len(content)


def _inspect_sqlite_snapshot(snapshot: bytes | Path) -> int:
    """Validate a snapshot, including every document row, with a hard count bound."""
    from core.sqlite_store import _validate_document_row

    temporary_folder = None
    if isinstance(snapshot, (bytes, bytearray, memoryview)):
        temporary_folder = tempfile.TemporaryDirectory(prefix="zyven-snapshot-check-")
        path = Path(temporary_folder.name) / "snapshot.sqlite3"
        if len(snapshot) > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
            temporary_folder.cleanup()
            raise ValueError("SQLite snapshot exceeds the uncompressed size limit")
        path.write_bytes(snapshot)
    else:
        path = Path(snapshot)
        if path.stat().st_size > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
            raise ValueError("SQLite snapshot exceeds the uncompressed size limit")
    connection = sqlite3.connect(path)
    try:
        integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if integrity != ["ok"]:
            raise ValueError("SQLite integrity check failed: " + "; ".join(map(str, integrity[:5])))
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
            raise ValueError("Snapshot is not a Zyven SQLite database")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        if not {"collection", "id", "payload"}.issubset(columns):
            raise ValueError("Snapshot documents table has an unexpected schema")
        count = 0
        for collection, document_id, payload in connection.execute("SELECT collection,id,payload FROM documents"):
            count += 1
            if count > MAX_SNAPSHOT_DOCUMENTS:
                raise ValueError("SQLite snapshot exceeds the document limit")
            _validate_document_row(collection, document_id, payload)
        return count
    finally:
        connection.close()
        if temporary_folder is not None:
            temporary_folder.cleanup()


async def restore_sqlite_backup_from_mongo(mongo_uri: str, database_name: str, sqlite_path: str) -> str | None:
    """Extract one recent, bounded, fully validated SQLite snapshot."""
    from pymongo import AsyncMongoClient

    client = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=3000)
    destination = Path(sqlite_path)
    temporary = destination.with_suffix(destination.suffix + ".restore-tmp")
    try:
        await client.admin.command("ping")
        collection = client[database_name]["sqlite_backups"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        records = []
        cursor = collection.find(
            {"created_at": {"$gte": cutoff}, "record_type": {"$ne": "manifest"}},
            {"backup_id": 1, "part": 1, "parts": 1, "created_at": 1, "_id": 0},
        ).sort("created_at", -1).limit(MAX_SNAPSHOT_PARTS * 2)
        async for record in cursor:
            records.append(record)
        backups: dict[str, list[dict]] = {}
        for record in records:
            backup_id = record.get("backup_id")
            if backup_id:
                backups.setdefault(str(backup_id), []).append(record)
        valid = []
        with tempfile.TemporaryDirectory(prefix="zyven-snapshot-restore-") as folder:
            for backup_id, records_for_backup in backups.items():
                compressed = Path(folder) / f"{len(valid)}.gz"
                snapshot = Path(folder) / f"{len(valid)}.sqlite3"
                try:
                    created_at = records_for_backup[0].get("created_at")
                    if not isinstance(created_at, datetime):
                        raise ValueError("snapshot timestamp is invalid")
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if created_at < cutoff:
                        raise ValueError("snapshot is too old")
                    expected = int(records_for_backup[0].get("parts", 0))
                    if expected < 1 or expected > MAX_SNAPSHOT_PARTS:
                        raise ValueError("snapshot part count exceeds the limit")
                    parts = [part async for part in collection.find({"backup_id": backup_id}).sort("part", 1).limit(expected + 1)]
                    if len(parts) != expected:
                        raise ValueError("snapshot parts are incomplete")
                    numbers = [int(part.get("part", 0)) for part in parts]
                    if numbers != list(range(1, expected + 1)):
                        raise ValueError("snapshot part numbering is invalid")
                    compressed_size = 0
                    with compressed.open("wb") as output:
                        for part in parts:
                            data = part.get("data")
                            if not isinstance(data, (bytes, bytearray, memoryview)):
                                raise ValueError("snapshot part is not binary")
                            if len(data) > MAX_SNAPSHOT_PART_BYTES:
                                raise ValueError("snapshot part exceeds the size limit")
                            compressed_size += len(data)
                            if compressed_size > MAX_SNAPSHOT_COMPRESSED_BYTES:
                                raise ValueError("snapshot exceeds the compressed size limit")
                            output.write(data)
                    written = 0
                    with gzip.open(compressed, "rb") as source, snapshot.open("wb") as output:
                        while True:
                            chunk = source.read(min(1024 * 1024, MAX_SNAPSHOT_UNCOMPRESSED_BYTES - written + 1))
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_SNAPSHOT_UNCOMPRESSED_BYTES:
                                raise ValueError("snapshot exceeds the uncompressed size limit")
                            output.write(chunk)
                    document_count = _inspect_sqlite_snapshot(snapshot)
                    valid.append((created_at, document_count, backup_id, snapshot))
                except (OSError, ValueError, TypeError, OverflowError, EOFError, pickle.UnpicklingError, gzip.BadGzipFile, sqlite3.Error, zlib.error):
                    continue
            if not valid:
                return None
            largest_count = max(item[1] for item in valid)
            if largest_count >= 20:
                threshold = max(10, largest_count // 2)
                candidates = [item for item in valid if item[1] >= threshold]
            else:
                candidates = valid
            _created, _count, backup_id, snapshot_path = max(candidates, key=lambda item: item[0])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot_path, temporary)
            os.replace(temporary, destination)
            return backup_id
    finally:
        await client.close()
        if temporary.exists():
            temporary.unlink()
