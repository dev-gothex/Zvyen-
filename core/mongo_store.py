"""Async MongoDB store used as Zyven's live database.

A one-time importer can bring forward legacy SQLite records or the old
MongoDB-hosted SQLite snapshots. SQLite is never opened for normal bot work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import asyncio
import logging
import pickle
import sqlite3
import tempfile

from bson import ObjectId
from pymongo import AsyncMongoClient, ReplaceOne
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument
from .sqlite_store import _safe_loads, _validate_document_row, MAX_DOCUMENT_PAYLOAD_BYTES

log = logging.getLogger(__name__)


class MongoCollection:
    """Keep the existing async collection call shape used by the bot."""

    def __init__(self, store: "MongoStore", name: str):
        self.store = store
        self.name = name

    @property
    def collection(self):
        if self.store.database is None:
            raise RuntimeError("MongoDB is not connected")
        return self.store.database[self.name]

    async def find(self, query=None, projection=None):
        return self.collection.find(query or {}, projection)

    async def find_one(self, *args, **kwargs):
        return await self.collection.find_one(*args, **kwargs)

    async def insert_one(self, *args, **kwargs):
        return await self.collection.insert_one(*args, **kwargs)

    async def update_one(self, *args, **kwargs):
        return await self.collection.update_one(*args, **kwargs)

    async def update_many(self, *args, **kwargs):
        return await self.collection.update_many(*args, **kwargs)

    async def delete_one(self, *args, **kwargs):
        return await self.collection.delete_one(*args, **kwargs)

    async def delete_many(self, *args, **kwargs):
        return await self.collection.delete_many(*args, **kwargs)

    async def count_documents(self, *args, **kwargs):
        return await self.collection.count_documents(*args, **kwargs)

    async def find_one_and_update(self, *args, **kwargs):
        return await self.collection.find_one_and_update(*args, **kwargs)

    async def find_one_and_delete(self, *args, **kwargs):
        return await self.collection.find_one_and_delete(*args, **kwargs)

    async def create_index(self, *args, **kwargs):
        return await self.collection.create_index(*args, **kwargs)


class MongoStore:
    def __init__(self, uri: str, database_name: str, legacy_sqlite_path: str | None = None):
        self.uri = uri.strip()
        self.database_name = database_name.strip()
        self.legacy_sqlite_path = Path(legacy_sqlite_path) if legacy_sqlite_path else None
        self.client = None
        self.database = None
        self._collections: dict[str, MongoCollection] = {}
        self.lock = asyncio.Lock()

    def __getitem__(self, name: str):
        # Database constructs collection handles before connect() runs; resolve
        # the real PyMongo collection only when an operation is called.
        return self._collections.setdefault(name, MongoCollection(self, name))

    async def connect(self):
        if not self.uri:
            raise RuntimeError("MongoDB is required. Set MONGODB_URI in the environment.")
        self.client = AsyncMongoClient(
            self.uri,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=7000,
            socketTimeoutMS=20000,
            retryWrites=True,
        )
        await self.client.admin.command("ping")
        self.database = self.client[self.database_name]
        await self._migrate_legacy_data_once()

    async def _migrate_legacy_data_once(self):
        """Import existing data once, preferring local SQLite as the old source of truth."""
        meta = self.database["_zyven_meta"]
        migration_id = "legacy_sqlite_to_mongodb_v1"
        state = await meta.find_one({"_id": migration_id})
        if state and state.get("status") == "complete":
            return

        names = await self.database.list_collection_names()
        excluded = {"_zyven_meta", "sqlite_backups"}
        app_names = [name for name in names if name not in excluded]
        remote_count = 0
        for name in app_names:
            remote_count += await self.database[name].count_documents({})
        # Existing remote data is only authoritative when no migration attempt is
        # recorded. A running/failed attempt may have left a partial remote copy.
        if remote_count and state is None:
            await meta.update_one(
                {"_id": migration_id},
                {"$set": {"status": "complete", "source": "existing_mongodb", "completed_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            log.info("MongoDB already contains %s live bot document(s); keeping them", remote_count)
            return

        source_path = None
        source_kind = None
        if self.legacy_sqlite_path and self.legacy_sqlite_path.is_file():
            try:
                count = await asyncio.to_thread(_legacy_sqlite_document_count, self.legacy_sqlite_path)
                if count:
                    source_path, source_kind = self.legacy_sqlite_path, "legacy_sqlite"
            except Exception as error:
                log.warning("Legacy SQLite import source is unreadable (%s)", type(error).__name__)

        # A new host may not have its old local disk. Recover the last archived
        # snapshot once, then import its documents into ordinary MongoDB collections.
        if source_path is None:
            backup_count = await self.database["sqlite_backups"].count_documents({})
            if backup_count:
                from utils.backups import restore_sqlite_backup_from_mongo
                with tempfile.TemporaryDirectory(prefix="zyven-mongo-migration-") as folder:
                    candidate = Path(folder) / "legacy.sqlite3"
                    try:
                        restored_id = await restore_sqlite_backup_from_mongo(
                            self.uri, self.database_name, str(candidate)
                        )
                    except Exception as error:
                        if remote_count:
                            raise RuntimeError("MongoDB legacy snapshot migration failed; preserving the existing live documents") from error
                        raise RuntimeError("MongoDB contains legacy backups, but none could be imported safely") from error
                    if not restored_id:
                        raise RuntimeError("MongoDB contains legacy backup records, but none is a complete valid snapshot")
                    await meta.update_one(
                        {"_id": migration_id},
                        {"$set": {"status": "running", "source": "mongodb_snapshot", "backup_id": restored_id, "started_at": datetime.now(timezone.utc)}},
                        upsert=True,
                    )
                    imported = await asyncio.to_thread(_read_legacy_sqlite_documents, candidate)
                    await self._import_documents(imported)
                    await meta.update_one(
                        {"_id": migration_id},
                        {"$set": {"status": "complete", "source": "mongodb_snapshot", "backup_id": restored_id, "document_count": sum(len(items) for items in imported.values()), "completed_at": datetime.now(timezone.utc)}},
                        upsert=True,
                    )
                    log.warning("Imported %s document(s) from MongoDB snapshot %s into live MongoDB collections", sum(len(items) for items in imported.values()), restored_id)
                    return

        if source_path is None:
            if state:
                await meta.update_one(
                    {"_id": migration_id},
                    {"$set": {"status": "failed", "reason": "migration source unavailable; remote count is not proof of completeness", "observed_remote_count": remote_count, "failed_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                raise RuntimeError("Previous MongoDB migration was interrupted and its source is unavailable; refusing to mark partial remote data complete")
            if remote_count:
                await meta.update_one(
                    {"_id": migration_id},
                    {"$set": {"status": "complete", "source": "existing_mongodb", "completed_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                return
            await meta.update_one(
                {"_id": migration_id},
                {"$set": {"status": "complete", "source": "empty_database", "completed_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            log.warning("MongoDB has no existing bot records or legacy SQLite file; starting with an empty MongoDB database")
            return

        await meta.update_one(
            {"_id": migration_id},
            {"$set": {"status": "running", "source": source_kind, "started_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        imported = await asyncio.to_thread(_read_legacy_sqlite_documents, source_path)
        await self._import_documents(imported)
        total = sum(len(items) for items in imported.values())
        await meta.update_one(
            {"_id": migration_id},
            {"$set": {"status": "complete", "source": source_kind, "document_count": total, "completed_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        log.warning("Imported %s document(s) from legacy SQLite into live MongoDB collections", total)

    async def _import_documents(self, collections: dict[str, list[dict]]):
        for name, documents in collections.items():
            if not documents:
                continue
            operations = []
            for document in documents:
                if document.get("_id") is None:
                    document["_id"] = ObjectId()
                operations.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
                if len(operations) >= 500:
                    await self.database[name].bulk_write(operations, ordered=False)
                    operations.clear()
            if operations:
                await self.database[name].bulk_write(operations, ordered=False)

    async def close(self):
        if self.client is not None:
            await self.client.close()
            self.client = None
            self.database = None
            self._collections.clear()

    async def ping(self):
        if self.client is None:
            raise RuntimeError("MongoDB is not connected")
        await self.client.admin.command("ping")

    async def count_documents(self) -> int:
        if self.database is None:
            return 0
        total = 0
        for name in await self.database.list_collection_names():
            if name not in {"_zyven_meta", "sqlite_backups"}:
                total += await self.database[name].count_documents({})
        return total

    async def claim_command_cooldown(self, key: str, duration: float, now: float, purge_at):
        collection = self.database["command_cooldowns"]
        replacement = {
            "_id": key,
            "expires_at": now + float(duration),
            "warned": False,
            "purge_at": purge_at,
        }
        while True:
            expired = await collection.find_one_and_update(
                {"_id": key, "$or": [{"expires_at": {"$lte": now}}, {"expires_at": {"$exists": False}}]},
                {"$set": {key: value for key, value in replacement.items() if key != "_id"}},
                return_document=ReturnDocument.AFTER,
            )
            if expired:
                return 0.0, False
            try:
                await collection.insert_one(replacement)
                return 0.0, False
            except DuplicateKeyError:
                active = await collection.find_one({"_id": key})
                if active is None:
                    continue
                expires_at = float(active.get("expires_at", 0))
                if expires_at <= now:
                    continue
                warning = await collection.find_one_and_update(
                    {"_id": key, "expires_at": expires_at, "warned": {"$ne": True}},
                    {"$set": {"warned": True}},
                    return_document=ReturnDocument.BEFORE,
                )
                return max(0.0, expires_at - now), warning is not None


def _legacy_sqlite_document_count(path: Path) -> int:
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])


def _read_legacy_sqlite_documents(path: Path) -> dict[str, list[dict]]:
    documents: dict[str, list[dict]] = {}
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as connection:
        for collection, document_id, payload in connection.execute("SELECT collection, id, payload FROM documents"):
            document = _validate_document_row(collection, document_id, payload)
            documents.setdefault(str(collection), []).append(document)
    return documents
