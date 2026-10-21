"""Small async, document-shaped SQLite store used by the bot at runtime.

The adapter intentionally keeps the old collection API so cogs do not need to
know about the storage engine. All data stays in one local SQLite file.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
import asyncio
import pickle
import sqlite3
import logging
import json
import time
import tempfile
import io

from bson import ObjectId
from pymongo import AsyncMongoClient, ReplaceOne, DeleteOne

log = logging.getLogger(__name__)


class _SafeUnpickler(pickle.Unpickler):
    """Load legacy documents without allowing pickle globals to execute code."""

    _ALLOWED_GLOBALS = {
        ("builtins", name)
        for name in ("bool", "bytes", "dict", "float", "frozenset", "int", "list", "set", "str", "tuple")
    } | {
        ("datetime", name) for name in ("date", "datetime", "time", "timedelta", "timezone")
    } | {("bson.objectid", "ObjectId")}

    def find_class(self, module, name):
        if (module, name) not in self._ALLOWED_GLOBALS:
            raise pickle.UnpicklingError(f"blocked pickle global: {module}.{name}")
        return super().find_class(module, name)


def _safe_loads(payload: bytes):
    return _SafeUnpickler(io.BytesIO(payload)).load()


class DuplicateKeyError(Exception):
    pass


class ReturnDocument:
    BEFORE = False
    AFTER = True


@dataclass
class WriteResult:
    matched_count: int = 0
    modified_count: int = 0
    deleted_count: int = 0
    inserted_id: object | None = None


_MISSING = object()
MAX_DOCUMENT_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_VALUE_DEPTH = 32
MAX_DOCUMENT_VALUE_NODES = 100_000


def _supported_document_value(value, *, depth=0, nodes=None):
    if depth > MAX_DOCUMENT_VALUE_DEPTH:
        raise ValueError("document value is too deeply nested")
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_DOCUMENT_VALUE_NODES:
        raise ValueError("document contains too many values")
    if value is None or isinstance(value, (bool, int, float, str, bytes, bytearray, ObjectId, datetime, date, datetime_time, timedelta, timezone)):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256 or "\x00" in key:
                raise ValueError("document contains an invalid field name")
            _supported_document_value(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _supported_document_value(item, depth=depth + 1, nodes=nodes)
        return
    raise ValueError(f"document contains unsupported value type: {type(value).__name__}")


def _validate_document_row(collection, document_id, payload) -> dict:
    if not isinstance(collection, str) or not collection or len(collection) > 128 or "\x00" in collection or collection.startswith("_"):
        raise ValueError("document collection name is invalid")
    if not isinstance(document_id, str) or not document_id or len(document_id) > 256 or "\x00" in document_id:
        raise ValueError("document id is invalid")
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > MAX_DOCUMENT_PAYLOAD_BYTES:
        raise ValueError("document payload is invalid or too large")
    document = _safe_loads(bytes(payload))
    if not isinstance(document, dict) or "_id" not in document or str(document["_id"]) != document_id:
        raise ValueError("document payload id does not match its row")
    _supported_document_value(document)
    return document

def _get(document: dict, path: str, missing=_MISSING):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return missing
        value = value[part]
    return value


def _set(document: dict, path: str, value):
    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            return
    target[parts[-1]] = deepcopy(value)


def _unset(document: dict, path: str):
    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.get(part)
        if not isinstance(target, dict):
            return
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, item) for item in expected):
                return False
            continue
        actual = _get(document, key)
        if isinstance(expected, dict) and any(str(op).startswith("$") for op in expected):
            for op, value in expected.items():
                if op == "$in" and (actual is _MISSING or actual not in value):
                    return False
                if op == "$gt" and (actual is _MISSING or actual is None or not actual > value):
                    return False
                if op == "$gte" and (actual is _MISSING or actual is None or not actual >= value):
                    return False
                if op == "$lt" and (actual is _MISSING or actual is None or not actual < value):
                    return False
                if op == "$lte" and (actual is _MISSING or actual is None or not actual <= value):
                    return False
                if op == "$ne" and actual is not _MISSING and actual == value:
                    return False
                if op == "$exists" and ((actual is not _MISSING) != bool(value)):
                    return False
        elif actual != expected:
            return False
    return True


class AsyncCursor:
    def __init__(self, documents: list[dict]):
        self._documents = documents

    def sort(self, key: str, direction: int = 1):
        self._documents.sort(key=lambda doc: (_get(doc, key) is None, _get(doc, key)), reverse=direction < 0)
        return self

    def limit(self, count: int):
        self._documents = self._documents[:count]
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        """Match the cursor helper used by the MongoDB-backed code paths."""
        documents = self._documents if length is None else self._documents[:length]
        return [deepcopy(document) for document in documents]

    def __aiter__(self):
        self._iterator = iter(self._documents)
        return self

    async def __anext__(self):
        try:
            return deepcopy(next(self._iterator))
        except StopIteration:
            raise StopAsyncIteration


class SQLiteCollection:
    def __init__(self, store: "SQLiteStore", name: str):
        self.store = store
        self.name = name

    async def create_index(self, *_args, **_kwargs):
        return None

    async def _all(self, query: dict | None = None) -> list[dict]:
        query = query or {}
        if "_id" in query and not isinstance(query["_id"], dict):
            rows = self.store.connection.execute(
                "SELECT payload FROM documents WHERE collection=? AND id=?",
                (self.name, str(query["_id"])),
            ).fetchall()
        elif "guild_id" in query and not isinstance(query["guild_id"], dict):
            rows = self.store.connection.execute(
                "SELECT payload FROM documents WHERE collection=? AND guild_id=?",
                (self.name, str(query["guild_id"])),
            ).fetchall()
        else:
            rows = self.store.connection.execute(
                "SELECT payload FROM documents WHERE collection=?", (self.name,)
            ).fetchall()
        return [_safe_loads(row[0]) for row in rows]

    async def find_one(self, query: dict, projection=None):
        async with self.store.lock:
            for document in await self._all(query):
                if _matches(document, query):
                    return deepcopy(document)
        return None

    async def find(self, query: dict):
        return AsyncCursor([doc for doc in self.store._snapshot(self.name, query) if _matches(doc, query)])

    async def insert_one(self, document: dict):
        document = deepcopy(document)
        document.setdefault("_id", ObjectId())
        async with self.store.lock:
            try:
                self.store.connection.execute(
                    "INSERT INTO documents(collection, id, guild_id, payload) VALUES (?, ?, ?, ?)",
                    (self.name, str(document["_id"]), str(document["guild_id"]) if document.get("guild_id") is not None else None, pickle.dumps(document, protocol=5)),
                )
                self.store._queue_mongo(self.name, document)
                self.store.connection.commit()
            except sqlite3.IntegrityError as error:
                raise DuplicateKeyError(str(error)) from error
        return WriteResult(inserted_id=document["_id"])

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        async with self.store.lock:
            docs = await self._all(query)
            for document in docs:
                if _matches(document, query):
                    before = deepcopy(document)
                    self._apply(document, update, inserting=False)
                    self._save(document)
                    return WriteResult(1, int(before != document))
            if not upsert:
                return WriteResult()
            document = {key: value for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
            self._apply(document, update, inserting=True)
            document.setdefault("_id", ObjectId())
            self._save(document, insert=True)
            return WriteResult(inserted_id=document["_id"])

    async def update_many(self, query: dict, update: dict):
        """Apply one document update to every matching record atomically."""
        async with self.store.lock:
            documents = [doc for doc in await self._all(query) if _matches(doc, query)]
            changed = []
            for document in documents:
                before = deepcopy(document)
                self._apply(document, update, inserting=False)
                if before != document:
                    changed.append((
                        pickle.dumps(document, protocol=5),
                        str(document["guild_id"]) if document.get("guild_id") is not None else None,
                        self.name,
                        str(document["_id"]),
                    ))
            if changed:
                self.store.connection.executemany(
                    "UPDATE documents SET payload=?, guild_id=? WHERE collection=? AND id=?",
                    changed,
                )
                for document in (_safe_loads(item[0]) for item in changed):
                    self.store._queue_mongo(self.name, document)
                self.store.connection.commit()
            return WriteResult(matched_count=len(documents), modified_count=len(changed))

    def _apply(self, document: dict, update: dict, *, inserting: bool):
        for operator, values in update.items():
            if operator == "$set":
                for path, value in values.items():
                    _set(document, path, value)
            elif operator == "$setOnInsert" and inserting:
                for path, value in values.items():
                    if _get(document, path) is None:
                        _set(document, path, value)
            elif operator == "$unset":
                paths = values if isinstance(values, list) else values.keys()
                for path in paths:
                    _unset(document, path)
            elif operator == "$inc":
                for path, value in values.items():
                    _set(document, path, (_get(document, path, 0) or 0) + value)
            elif operator == "$addToSet":
                for path, value in values.items():
                    current = _get(document, path)
                    if current is None:
                        current = []
                        _set(document, path, current)
                    values_to_add = value.get("$each", []) if isinstance(value, dict) and "$each" in value else [value]
                    for item in values_to_add:
                        if item not in current:
                            current.append(item)
            elif operator == "$pull":
                for path, value in values.items():
                    current = _get(document, path, [])
                    if isinstance(current, list):
                        _set(document, path, [item for item in current if item != value])

    def _save(self, document: dict, *, insert: bool = False):
        payload = pickle.dumps(document, protocol=5)
        if insert:
            self.store.connection.execute(
                "INSERT INTO documents(collection, id, guild_id, payload) VALUES (?, ?, ?, ?)",
                (self.name, str(document["_id"]), str(document["guild_id"]) if document.get("guild_id") is not None else None, payload),
            )
        else:
            self.store.connection.execute(
                "UPDATE documents SET payload = ?, guild_id = ? WHERE collection = ? AND id = ?",
                (payload, str(document["guild_id"]) if document.get("guild_id") is not None else None, self.name, str(document["_id"])),
            )
        self.store._queue_mongo(self.name, document)
        self.store.connection.commit()

    async def delete_one(self, query: dict):
        async with self.store.lock:
            for document in await self._all(query):
                if _matches(document, query):
                    self.store.connection.execute("DELETE FROM documents WHERE collection=? AND id=?", (self.name, str(document["_id"])))
                    self.store._queue_mongo_delete(self.name, document["_id"])
                    self.store.connection.commit()
                    return WriteResult(deleted_count=1)
        return WriteResult()

    async def delete_many(self, query: dict):
        async with self.store.lock:
            matched = [doc for doc in await self._all(query) if _matches(doc, query)]
            ids = [str(doc["_id"]) for doc in matched]
            self.store.connection.executemany("DELETE FROM documents WHERE collection=? AND id=?", [(self.name, item) for item in ids])
            for doc in matched:
                self.store._queue_mongo_delete(self.name, doc["_id"])
            self.store.connection.commit()
        return WriteResult(deleted_count=len(ids))

    async def count_documents(self, query: dict):
        async with self.store.lock:
            return sum(1 for doc in await self._all(query) if _matches(doc, query))

    async def find_one_and_update(self, query: dict, update: dict, *, upsert=False, return_document=False):
        async with self.store.lock:
            for document in await self._all(query):
                if _matches(document, query):
                    before = deepcopy(document)
                    self._apply(document, update, inserting=False)
                    self._save(document)
                    return deepcopy(document if return_document else before)
            if not upsert:
                return None
            document = {key: value for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
            self._apply(document, update, inserting=True)
            document.setdefault("_id", ObjectId())
            self._save(document, insert=True)
            return deepcopy(document) if return_document else None

    async def find_one_and_delete(self, query: dict):
        async with self.store.lock:
            for document in await self._all(query):
                if _matches(document, query):
                    self.store.connection.execute("DELETE FROM documents WHERE collection=? AND id=?", (self.name, str(document["_id"])))
                    self.store._queue_mongo_delete(self.name, document["_id"])
                    self.store.connection.commit()
                    return deepcopy(document)
        return None


class SQLiteStore:
    def __init__(self, path: str | Path, mongo_uri: str = "", mongo_database: str = "zyven_dev"):
        self.path = Path(path)
        self.mongo_uri = mongo_uri.strip()
        self.mongo_database_name = mongo_database.strip()
        self.mongo_client = None
        self.mongo_database = None
        self.mongo_task = None
        self.mongo_wakeup = asyncio.Event()
        self.mongo_initial_sync = asyncio.Event()
        self.connection: sqlite3.Connection | None = None
        self.lock = asyncio.Lock()
        self._collections: dict[str, SQLiteCollection] = {}

    def __getitem__(self, name: str):
        return self._collections.setdefault(name, SQLiteCollection(self, name))

    def _snapshot(self, collection: str, query: dict | None = None) -> list[dict]:
        if self.connection is None:
            return []
        query = query or {}
        if "guild_id" in query and not isinstance(query["guild_id"], dict):
            rows = self.connection.execute("SELECT payload FROM documents WHERE collection=? AND guild_id=?", (collection, str(query["guild_id"]))).fetchall()
        else:
            rows = self.connection.execute("SELECT payload FROM documents WHERE collection=?", (collection,)).fetchall()
        return [_safe_loads(row[0]) for row in rows]

    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA cache_size=-32768")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA mmap_size=268435456")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT NOT NULL, id TEXT NOT NULL, guild_id TEXT, payload BLOB NOT NULL, PRIMARY KEY(collection, id))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS mongo_outbox (collection TEXT NOT NULL, document_id TEXT NOT NULL, operation TEXT NOT NULL, payload BLOB, revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(collection, document_id))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS mongo_outbox_quarantine (collection TEXT NOT NULL, document_id TEXT NOT NULL, operation TEXT NOT NULL, payload BLOB, revision INTEGER NOT NULL, error TEXT NOT NULL, quarantined_at REAL NOT NULL, PRIMARY KEY(collection, document_id, revision))")
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(documents)")}
        if "guild_id" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN guild_id TEXT")
            for row in self.connection.execute("SELECT collection, id, payload FROM documents").fetchall():
                guild_id = _safe_loads(row[2]).get("guild_id")
                if guild_id is not None:
                    self.connection.execute("UPDATE documents SET guild_id=? WHERE collection=? AND id=?", (str(guild_id), row[0], row[1]))
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_guild ON documents(collection, guild_id)")
        self.connection.commit()
        self.mongo_task = asyncio.create_task(self._mongo_sync_loop(), name="zyven-sqlite-mongo-sync")
        if self.mongo_uri:
            try:
                await asyncio.wait_for(self.mongo_initial_sync.wait(), timeout=12)
            except asyncio.TimeoutError:
                log.warning("MongoDB initial sync is taking longer than 12 seconds; bot will continue on local SQLite")

    def _queue_mongo(self, collection: str, document: dict):
        self.connection.execute(
            "INSERT INTO mongo_outbox(collection,document_id,operation,payload,revision) VALUES(?,?, 'replace', ?,1) "
            "ON CONFLICT(collection,document_id) DO UPDATE SET operation='replace', payload=excluded.payload, revision=revision+1",
            (collection, str(document["_id"]), pickle.dumps(document, protocol=5)),
        )
        self.mongo_wakeup.set()

    def _queue_mongo_delete(self, collection: str, document_id):
        self.connection.execute(
            "INSERT INTO mongo_outbox(collection,document_id,operation,payload,revision) VALUES(?,?, 'delete', ?,1) "
            "ON CONFLICT(collection,document_id) DO UPDATE SET operation='delete', payload=excluded.payload, revision=revision+1",
            (collection, str(document_id), pickle.dumps(document_id, protocol=5)),
        )
        self.mongo_wakeup.set()

    async def _mongo_sync_loop(self):
        if not self.mongo_uri:
            log.warning("MongoDB sync is not configured; local SQLite remains active")
            self.mongo_initial_sync.set()
            return
        delay = 1
        while True:
            try:
                if self.mongo_client is None:
                    self.mongo_client = AsyncMongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=20000, retryWrites=True)
                    await self.mongo_client.admin.command("ping")
                    self.mongo_database = self.mongo_client[self.mongo_database_name]
                    async with self.lock:
                        await self._reconcile_mongo()
                    self.mongo_initial_sync.set()
                    log.info("MongoDB mirror connected; local SQLite remains the runtime database")
                # Event-driven sync: keep the bot idle when nothing is waiting,
                # and flush immediately after each durable local write.
                self.mongo_wakeup.clear()
                await self._flush_mongo_outbox()
                if self.connection.execute("SELECT 1 FROM mongo_outbox LIMIT 1").fetchone():
                    continue
                delay = 1
                await self.mongo_wakeup.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.mongo_initial_sync.set()
                log.warning("MongoDB mirror sync delayed (%s); local SQLite writes are safely queued", type(error).__name__)
                if self.mongo_client is not None:
                    await self.mongo_client.close()
                    self.mongo_client = self.mongo_database = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _reconcile_mongo(self):
        names = [n for n in await self.mongo_database.list_collection_names() if not n.startswith("_") and n != "sqlite_backups"]
        local_count = self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        remote_count = 0
        for name in names:
            remote_count += await self.mongo_database[name].count_documents({})
        if not local_count and not remote_count:
            backup_count = await self.mongo_database["sqlite_backups"].count_documents({})
            if backup_count:
                from utils.backups import restore_sqlite_backup_from_mongo
                with tempfile.TemporaryDirectory(prefix="zyven-mongo-restore-") as folder:
                    restored = Path(folder) / "restored.sqlite3"
                    backup_id = await restore_sqlite_backup_from_mongo(self.mongo_uri, self.mongo_database_name, str(restored))
                    if backup_id:
                        with sqlite3.connect(restored) as source:
                            try:
                                rows = source.execute("SELECT collection,id,guild_id,payload FROM documents").fetchall()
                            except sqlite3.OperationalError:
                                rows = [(collection, document_id, None, payload) for collection, document_id, payload in source.execute("SELECT collection,id,payload FROM documents").fetchall()]
                        validated_rows = []
                        for collection, document_id, guild_id, payload in rows:
                            _validate_document_row(collection, document_id, payload)
                            validated_rows.append((collection, document_id, guild_id, payload))
                        self.connection.executemany("INSERT OR REPLACE INTO documents(collection,id,guild_id,payload) VALUES(?,?,?,?)", validated_rows)
                        self.connection.commit()
                        local_count = len(validated_rows)
                        log.warning("Restored %s local document(s) from MongoDB snapshot %s", local_count, backup_id)
        # A new host imports the existing live Mongo collections once.
        if not local_count and remote_count:
            for name in names:
                cursor = self.mongo_database[name].find({})
                async for document in cursor:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO documents(collection,id,guild_id,payload) VALUES(?,?,?,?)",
                        (name, str(document["_id"]), str(document["guild_id"]) if document.get("guild_id") is not None else None, pickle.dumps(document, protocol=5)),
                    )
            self.connection.commit()
            log.info("Restored %s MongoDB document(s) into local SQLite", remote_count)
        elif local_count and not remote_count:
            # First deployment from an existing SQLite data folder.
            for name in self.connection.execute("SELECT DISTINCT collection FROM documents").fetchall():
                for (payload,) in self.connection.execute("SELECT payload FROM documents WHERE collection=?", (name[0],)).fetchall():
                    self._queue_mongo(name[0], _safe_loads(payload))
            self.connection.commit()
            log.info("Queued %s local SQLite document(s) for initial MongoDB mirror", local_count)
        elif local_count and remote_count:
            # MongoDB is the cross-host source of truth. Pending local edits
            # win only while they are still in the durable outbox.
            for name in names:
                remote_ids = set()
                cursor = self.mongo_database[name].find({})
                async for document in cursor:
                    remote_ids.add(str(document["_id"]))
                    pending = self.connection.execute("SELECT 1 FROM mongo_outbox WHERE collection=? AND document_id=?", (name, str(document["_id"]))).fetchone()
                    if not pending:
                        self.connection.execute("INSERT OR REPLACE INTO documents(collection,id,guild_id,payload) VALUES(?,?,?,?)", (name, str(document["_id"]), str(document["guild_id"]) if document.get("guild_id") is not None else None, pickle.dumps(document, protocol=5)))
                for local_id, payload in self.connection.execute("SELECT id,payload FROM documents WHERE collection=?", (name,)).fetchall():
                    if local_id not in remote_ids:
                        pending = self.connection.execute("SELECT 1 FROM mongo_outbox WHERE collection=? AND document_id=?", (name, local_id)).fetchone()
                        if not pending:
                            self._queue_mongo(name, _safe_loads(payload))
            for (local_name,) in self.connection.execute("SELECT DISTINCT collection FROM documents").fetchall():
                if local_name not in names:
                    for (payload,) in self.connection.execute("SELECT payload FROM documents WHERE collection=?", (local_name,)).fetchall():
                        self._queue_mongo(local_name, _safe_loads(payload))
            self.connection.commit()

    async def _flush_mongo_outbox(self):
        rows = self.connection.execute("SELECT collection,document_id,operation,payload,revision FROM mongo_outbox LIMIT 500").fetchall()
        grouped = {}
        for collection, document_id, operation, payload, revision in rows:
            try:
                if operation == "delete":
                    original_id = _safe_loads(payload)
                    if str(original_id) != document_id:
                        raise ValueError("delete payload id does not match its outbox row")
                    request = DeleteOne({"_id": original_id})
                elif operation == "replace":
                    document = _validate_document_row(collection, document_id, payload)
                    request = ReplaceOne({"_id": document["_id"]}, document, upsert=True)
                else:
                    raise ValueError("unknown outbox operation")
            except (pickle.UnpicklingError, EOFError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as error:
                # Remove only this bad row, retaining an auditable dead-letter copy so
                # one corrupt payload cannot block later durable changes.
                self.connection.execute(
                    "INSERT OR REPLACE INTO mongo_outbox_quarantine(collection,document_id,operation,payload,revision,error,quarantined_at) VALUES(?,?,?,?,?,?,?)",
                    (collection, document_id, operation, payload, revision, f"{type(error).__name__}: {error}"[:1000], time.time()),
                )
                self.connection.execute(
                    "DELETE FROM mongo_outbox WHERE collection=? AND document_id=? AND revision=?",
                    (collection, document_id, revision),
                )
                self.connection.commit()
                log.error("Quarantined malformed Mongo outbox row %s/%s revision %s (%s)", collection, document_id, revision, type(error).__name__)
                continue
            grouped.setdefault(collection, []).append((request, collection, document_id, revision))
        for collection, items in grouped.items():
            await self.mongo_database[collection].bulk_write([item[0] for item in items], ordered=False)
            for _, name, document_id, revision in items:
                self.connection.execute("DELETE FROM mongo_outbox WHERE collection=? AND document_id=? AND revision=?", (name, document_id, revision))
            self.connection.commit()

    async def close(self):
        if self.mongo_task is not None:
            self.mongo_wakeup.set()
            try:
                if self.mongo_database is not None:
                    await asyncio.wait_for(self._flush_mongo_outbox(), timeout=10)
            except Exception:
                log.warning("MongoDB mirror did not finish flushing; queued SQLite changes remain on disk")
            self.mongo_task.cancel()
            try:
                await self.mongo_task
            except asyncio.CancelledError:
                pass
            self.mongo_task = None
        if self.mongo_client is not None:
            await self.mongo_client.close()
            self.mongo_client = self.mongo_database = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    async def ping(self):
        async with self.lock:
            self.connection.execute("SELECT 1").fetchone()

    async def claim_command_cooldown(self, key: str, duration: float, now: float, purge_at):
        """Atomically enforce global command cooldowns in the local SQL store."""
        async with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT payload FROM documents WHERE collection=? AND id=?",
                    ("command_cooldowns", key),
                ).fetchone()
                active = _safe_loads(row[0]) if row else None
                expires_at = float((active or {}).get("expires_at", 0))
                if expires_at <= now:
                    replacement = {
                        "_id": key, "expires_at": now + float(duration),
                        "warned": False, "purge_at": purge_at,
                    }
                    self.connection.execute(
                        "INSERT INTO documents(collection,id,guild_id,payload) VALUES (?,?,NULL,?) "
                        "ON CONFLICT(collection,id) DO UPDATE SET payload=excluded.payload",
                        ("command_cooldowns", key, pickle.dumps(replacement, protocol=5)),
                    )
                    self._queue_mongo("command_cooldowns", replacement)
                    self.connection.commit()
                    return 0.0, False
                notify = not bool(active.get("warned", False))
                if notify:
                    active["warned"] = True
                    self.connection.execute(
                        "UPDATE documents SET payload=? WHERE collection=? AND id=?",
                        (pickle.dumps(active, protocol=5), "command_cooldowns", key),
                    )
                    self._queue_mongo("command_cooldowns", active)
                self.connection.commit()
                return max(0.0, expires_at - now), notify
            except Exception:
                self.connection.rollback()
                raise
