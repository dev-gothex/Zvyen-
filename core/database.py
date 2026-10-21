from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import re
import time

from .sqlite_store import SQLiteStore, ReturnDocument, DuplicateKeyError

log = logging.getLogger(__name__)
_BUNDLED_SEED_DATABASE_SHA256 = "a7ed4ddd24c390a3d5f9186de8e583f6abaeadf2142298b523b6008a7bbb9f27"


def _normalize_reaction_trigger(trigger: str) -> str:
    return re.sub(r"<@!?(\d+)>", r"<@\1>", trigger).casefold()


class Database:
    # Guild settings are read from many independent message/event listeners.
    # All in-process setting writes invalidate this cache immediately, so a
    # longer TTL cuts repeated reads/deep-copies without delaying bot changes.
    GUILD_CACHE_SECONDS = 60
    MESSAGE_AUTOMATION_CACHE_SECONDS = 30
    # Give a sick database time to recover instead of retrying a failing pool
    # on every command/message and compounding the outage.
    DB_FAILURE_BACKOFF_SECONDS = 15.0
    # Avoid a database read for every regular chat message from a member
    # who is not AFK. set_afk_status invalidates this cache immediately.
    AFK_ABSENT_CACHE_SECONDS = 30.0
    AUTORESPONDER_GUILD_CACHE_SECONDS = 30.0

    def __init__(self, sqlite_path: str, mongo_uri: str = "", mongo_database: str = "zyven_dev"):
        self.store = SQLiteStore(sqlite_path, mongo_uri, mongo_database)
        self.mongo_uri = mongo_uri
        self.mongo_database = mongo_database
        self.db = self.store
        self.guilds = self.db["guild_settings"]
        self.warnings = self.db["warnings"]
        self.noprefix_users = self.db["noprefix_users"]
        self.noprefix_trial_claims = self.db["noprefix_trial_claims"]
        self.server_blacklist = self.db["owner_server_blacklist"]
        self.global_bans = self.db["owner_global_bans"]
        self.owner_branding = self.db["owner_branding"]
        self.utility_tasks = self.db["utility_tasks"]
        self.command_responses = self.db["command_responses"]
        self.command_cooldowns = self.db["command_cooldowns"]
        self.application_emoji_sync = self.db["application_emoji_sync"]
        self.leaderboard_stats = self.db["leaderboard_stats"]
        self.afk_statuses = self.db["afk_statuses"]
        self.role_recoveries = self.db["role_recoveries"]
        self.autoresponders = self.db["autoresponders"]
        self.autoreactions = self.db["autoreactions"]
        self.reaction_roles = self.db["reaction_roles"]
        self.giveaways = self.db["giveaways"]
        self._guild_cache: dict[int, tuple[float, dict]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._autoresponder_guild_cache: dict[int, tuple[float, dict[str, dict]]] = {}
        self._autoresponder_guild_locks: dict[int, asyncio.Lock] = {}
        self._autoreaction_cache: dict[int, tuple[float, list[dict]]] = {}
        self._afk_absent_until: dict[tuple[int, int | None], float] = {}
        self.last_ping_ms: float | None = None
        self.last_ping_at: float | None = None
        self._unavailable_until = 0.0

    @property
    def temporarily_unavailable(self) -> bool:
        return time.monotonic() < self._unavailable_until

    def mark_unavailable(self):
        self._unavailable_until = time.monotonic() + self.DB_FAILURE_BACKOFF_SECONDS

    def mark_available(self):
        self._unavailable_until = 0.0

    async def connect(self) -> None:
        ping_started = time.perf_counter()
        await self.store.connect()
        await self.store.ping()
        self.last_ping_ms = (time.perf_counter() - ping_started) * 1000
        self.last_ping_at = time.time()
        self.mark_available()
        await self.command_responses.delete_many({"created_at": {"$lt": datetime.now(timezone.utc) - timedelta(minutes=5)}})
        await self.command_cooldowns.delete_many({"purge_at": {"$lt": datetime.now(timezone.utc)}})
        document_count = sum([await collection.count_documents({}) for collection in (self.guilds, self.warnings, self.leaderboard_stats, self.utility_tasks)])
        log.info("Local SQLite database ready with %s core document(s); MongoDB sync runs in background", document_count)

    async def close(self) -> None:
        await self.store.close()

    async def get_application_emoji_sync_version(self, application_id: int) -> int | None:
        record = await self.application_emoji_sync.find_one({"_id": str(application_id)}, {"version": 1})
        return int(record["version"]) if record and record.get("version") is not None else None

    async def set_application_emoji_sync_version(self, application_id: int, version: int) -> None:
        await self.application_emoji_sync.update_one(
            {"_id": str(application_id)},
            {"$set": {"version": int(version), "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_guild(self, guild_id: int) -> dict:
        now = time.monotonic()
        cached = self._guild_cache.get(guild_id)
        if cached and (cached[0] > now or self.temporarily_unavailable):
            return deepcopy(cached[1])
        if self.temporarily_unavailable:
            doc = {"_id": str(guild_id), "security": {}, "automod": {}, "welcome": {}, "autorole": {}, "voicemaster": {}}
            self._guild_cache[guild_id] = (now + self.DB_FAILURE_BACKOFF_SECONDS, doc)
            return deepcopy(doc)
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._guild_cache.get(guild_id)
            if cached and (cached[0] > now or self.temporarily_unavailable):
                return deepcopy(cached[1])
            if self.temporarily_unavailable:
                doc = {"_id": str(guild_id), "security": {}, "automod": {}, "welcome": {}, "autorole": {}, "voicemaster": {}}
                self._guild_cache[guild_id] = (now + self.DB_FAILURE_BACKOFF_SECONDS, doc)
                return deepcopy(doc)
            try:
                doc = await self.guilds.find_one({"_id": str(guild_id)})
            except Exception:
                # Message listeners read guild settings frequently. Return the
                # last known config during a database outage instead of
                # spawning a failing query and traceback for every message.
                self.mark_unavailable()
                if cached:
                    return deepcopy(cached[1])
                doc = None
            if not doc:
                doc = {"_id": str(guild_id), "security": {}, "automod": {}, "welcome": {}, "autorole": {}, "voicemaster": {}}
            cache_ttl = self.DB_FAILURE_BACKOFF_SECONDS if self.temporarily_unavailable else self.GUILD_CACHE_SECONDS
            self._guild_cache[guild_id] = (time.monotonic() + cache_ttl, doc)
            return deepcopy(doc)

    async def set_value(self, guild_id: int, key: str, value):
        await self.guilds.update_one(
            {"_id": str(guild_id)}, {"$set": {key: value}}, upsert=True
        )
        self._guild_cache.pop(guild_id, None)

    async def set_values(self, guild_id: int, values: dict):
        """Write related settings together and invalidate the guild cache once."""
        if not values:
            return
        await self.guilds.update_one({"_id": str(guild_id)}, {"$set": values}, upsert=True)
        self._guild_cache.pop(guild_id, None)

    async def set_nested(self, guild_id: int, section: str, key: str, value):
        await self.set_value(guild_id, f"{section}.{key}", value)

    async def add_to_set(self, guild_id: int, key: str, value):
        await self.guilds.update_one({"_id": str(guild_id)}, {"$addToSet": {key: value}}, upsert=True)
        self._guild_cache.pop(guild_id, None)

    async def pull_value(self, guild_id: int, key: str, value):
        await self.guilds.update_one({"_id": str(guild_id)}, {"$pull": {key: value}})
        self._guild_cache.pop(guild_id, None)

    async def unset_value(self, guild_id: int, key: str):
        await self.guilds.update_one({"_id": str(guild_id)}, {"$unset": {key: ""}})
        self._guild_cache.pop(guild_id, None)

    async def unset_values(self, guild_id: int, keys: list[str]):
        """Remove related settings together and invalidate the guild cache once."""
        if not keys:
            return
        await self.guilds.update_one(
            {"_id": str(guild_id)}, {"$unset": {key: "" for key in keys}}
        )
        self._guild_cache.pop(guild_id, None)

    async def add_utility_task(self, task: dict):
        await self.utility_tasks.insert_one(task)

    async def get_utility_tasks(self) -> list[dict]:
        cursor = await self.utility_tasks.find({})
        return [task async for task in cursor]

    async def delete_utility_task(self, task_id: str):
        await self.utility_tasks.delete_one({"_id": task_id})

    async def create_giveaway(self, giveaway: dict):
        await self.giveaways.insert_one(giveaway)

    async def get_giveaway(self, message_id: int) -> dict | None:
        return await self.giveaways.find_one({"_id": str(int(message_id))})

    async def list_active_giveaways(self, guild_id: int | None = None) -> list[dict]:
        query = {"active": True}
        if guild_id is not None:
            query["guild_id"] = int(guild_id)
        cursor = (await self.giveaways.find(query)).sort("ends_at", 1)
        return [item async for item in cursor]

    async def add_giveaway_entry(self, message_id: int, user_id: int) -> bool:
        result = await self.giveaways.update_one(
            {"_id": str(int(message_id)), "active": True},
            {"$addToSet": {"entries": int(user_id)}},
        )
        return result.modified_count > 0

    async def remove_giveaway_entry(self, message_id: int, user_id: int):
        await self.giveaways.update_one(
            {"_id": str(int(message_id)), "active": True},
            {"$pull": {"entries": int(user_id)}},
        )

    async def close_giveaway(self, message_id: int) -> dict | None:
        return await self.giveaways.find_one_and_update(
            {"_id": str(int(message_id)), "active": True},
            {"$set": {"active": False, "ended_at": time.time()}},
            return_document=ReturnDocument.AFTER,
        )

    async def set_afk_status(self, user_id: int, status: dict):
        """Keep one active global or server-scoped AFK status per user."""
        user_id = int(user_id)
        await self.afk_statuses.update_one(
            {"_id": str(user_id)},
            {"$set": {"user_id": int(user_id), **status}},
            upsert=True,
        )
        for key in tuple(self._afk_absent_until):
            if key[0] == user_id:
                self._afk_absent_until.pop(key, None)

    async def get_afk_status(self, user_id: int) -> dict | None:
        return await self.afk_statuses.find_one({"_id": str(user_id)})

    async def clear_afk_status(self, user_id: int, *, guild_id: int | None = None) -> dict | None:
        user_id = int(user_id)
        cache_key = (user_id, int(guild_id) if guild_id is not None else None)
        now = time.monotonic()
        if self.temporarily_unavailable:
            return None
        if self._afk_absent_until.get(cache_key, 0) > now:
            return None
        query = {"_id": str(user_id)}
        if guild_id is not None:
            query["$or"] = [{"scope": "global"}, {"scope": "server", "guild_id": int(guild_id)}]
        status = await self.afk_statuses.find_one_and_delete(query)
        if status is None:
            self._afk_absent_until[cache_key] = now + self.AFK_ABSENT_CACHE_SECONDS
            if len(self._afk_absent_until) > 50000:
                self._afk_absent_until = {
                    key: expires_at for key, expires_at in self._afk_absent_until.items()
                    if expires_at > now
                }
        return status

    async def record_afk_ping(self, user_id: int, mentioner_id: int, *, guild_id: int) -> dict | None:
        """Atomically record a ping only when this AFK status applies here."""
        if self.temporarily_unavailable:
            return None
        status = await self.afk_statuses.find_one_and_update(
            {
                "_id": str(user_id),
                "$or": [
                    {"scope": "global"},
                    {"scope": "server", "guild_id": int(guild_id)},
                ],
            },
            {"$inc": {"ping_count": 1}, "$addToSet": {"pinged_by": int(mentioner_id)}},
            return_document=ReturnDocument.AFTER,
        )
        if status and len(status.get("pinged_by", [])) > 25:
            status["pinged_by"] = status["pinged_by"][-25:]
            await self.afk_statuses.update_one(
                {"_id": str(user_id)}, {"$set": {"pinged_by": status["pinged_by"]}}
            )
        return status

    async def save_role_recovery(self, guild_id: int, user_id: int, role_ids: list[int]) -> None:
        """Persist a member's transferable roles for manual or rejoin recovery."""
        key = f"{int(guild_id)}:{int(user_id)}"
        await self.role_recoveries.update_one(
            {"_id": key},
            {"$set": {
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "role_ids": sorted({int(role_id) for role_id in role_ids}),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    async def get_role_recovery(self, guild_id: int, user_id: int) -> dict | None:
        key = f"{int(guild_id)}:{int(user_id)}"
        return await self.role_recoveries.find_one({"_id": key})

    async def create_autoresponder(self, guild_id: int, name: str, response: str) -> bool:
        key = name.casefold()
        try:
            await self.autoresponders.insert_one({
                "_id": f"{int(guild_id)}:{key}",
                "guild_id": int(guild_id),
                "key": key,
                "name": name,
                "response": response,
            })
            self._invalidate_autoresponder_cache(guild_id)
            return True
        except DuplicateKeyError:
            return False

    async def get_autoresponder(self, guild_id: int, name: str) -> dict | None:
        guild_id = int(guild_id)
        key = name.casefold()
        now = time.monotonic()
        cached = self._autoresponder_guild_cache.get(guild_id)
        if cached and cached[0] > now:
            record = cached[1].get(key)
            return deepcopy(record) if record is not None else None
        if self.temporarily_unavailable:
            return deepcopy(cached[1].get(key)) if cached else None
        lock = self._autoresponder_guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._autoresponder_guild_cache.get(guild_id)
            if cached and cached[0] > now:
                record = cached[1].get(key)
                return deepcopy(record) if record is not None else None
            if self.temporarily_unavailable:
                return deepcopy(cached[1].get(key)) if cached else None
            try:
                records = await self.list_autoresponders(guild_id)
            except Exception:
                self.mark_unavailable()
                return deepcopy(cached[1].get(key)) if cached else None
            entries = {str(item.get("key") or item.get("name", "")).casefold(): item for item in records}
            self._autoresponder_guild_cache[guild_id] = (time.monotonic() + self.AUTORESPONDER_GUILD_CACHE_SECONDS, entries)
            if len(self._autoresponder_guild_cache) > 1000:
                self._autoresponder_guild_cache = {gid: value for gid, value in self._autoresponder_guild_cache.items() if value[0] > now}
            record = entries.get(key)
            return deepcopy(record) if record is not None else None

    def _invalidate_autoresponder_cache(self, guild_id: int):
        guild_id = int(guild_id)
        self._autoresponder_guild_cache.pop(guild_id, None)

    async def list_autoresponders(self, guild_id: int) -> list[dict]:
        cursor = (await self.autoresponders.find({"guild_id": int(guild_id)})).sort("key", 1)
        return [item async for item in cursor]

    async def edit_autoresponder(self, guild_id: int, name: str, response: str) -> bool:
        result = await self.autoresponders.update_one(
            {"_id": f"{int(guild_id)}:{name.casefold()}"},
            {"$set": {"response": response}},
        )
        self._invalidate_autoresponder_cache(guild_id)
        return result.matched_count > 0

    async def delete_autoresponder(self, guild_id: int, name: str) -> bool:
        result = await self.autoresponders.delete_one({"_id": f"{int(guild_id)}:{name.casefold()}"})
        self._invalidate_autoresponder_cache(guild_id)
        return result.deleted_count > 0

    async def create_autoreaction(self, guild_id: int, trigger: str, emoji: str) -> bool:
        key = _normalize_reaction_trigger(trigger)
        try:
            await self.autoreactions.insert_one({
                "_id": f"{int(guild_id)}:{key}",
                "guild_id": int(guild_id),
                "key": key,
                "trigger": trigger,
                "emoji": emoji,
            })
            self._autoreaction_cache.pop(int(guild_id), None)
            return True
        except DuplicateKeyError:
            return False

    async def delete_autoreaction(self, guild_id: int, trigger: str) -> bool:
        result = await self.autoreactions.delete_one({"_id": f"{int(guild_id)}:{_normalize_reaction_trigger(trigger)}"})
        if result.deleted_count:
            self._autoreaction_cache.pop(int(guild_id), None)
        return result.deleted_count > 0

    async def list_autoreactions(self, guild_id: int) -> list[dict]:
        guild_id = int(guild_id)
        now = time.monotonic()
        cached = self._autoreaction_cache.get(guild_id)
        if cached and cached[0] > now:
            return deepcopy(cached[1])
        if self.temporarily_unavailable:
            return deepcopy(cached[1]) if cached else []
        try:
            cursor = (await self.autoreactions.find({"guild_id": guild_id})).sort("key", 1)
            items = [item async for item in cursor]
        except Exception:
            self.mark_unavailable()
            return deepcopy(cached[1]) if cached else []
        self._autoreaction_cache[guild_id] = (now + self.MESSAGE_AUTOMATION_CACHE_SECONDS, items)
        if len(self._autoreaction_cache) > 1000:
            self._autoreaction_cache = {key: value for key, value in self._autoreaction_cache.items() if value[0] > now}
        return deepcopy(items)

    async def clear_autoreactions(self, guild_id: int) -> int:
        result = await self.autoreactions.delete_many({"guild_id": int(guild_id)})
        self._autoreaction_cache.pop(int(guild_id), None)
        return result.deleted_count

    async def create_reaction_role(self, guild_id: int, message_id: int, emoji_key: str, emoji: str, role_id: int) -> bool:
        try:
            await self.reaction_roles.insert_one({
                "_id": f"{int(guild_id)}:{int(message_id)}:{emoji_key}",
                "guild_id": int(guild_id),
                "message_id": int(message_id),
                "emoji_key": emoji_key,
                "emoji": emoji,
                "role_id": int(role_id),
            })
            return True
        except DuplicateKeyError:
            return False

    async def get_reaction_role(self, guild_id: int, message_id: int, emoji_key: str) -> dict | None:
        return await self.reaction_roles.find_one({
            "_id": f"{int(guild_id)}:{int(message_id)}:{emoji_key}"
        })

    async def delete_reaction_role(self, guild_id: int, message_id: int, emoji_key: str) -> bool:
        result = await self.reaction_roles.delete_one({
            "_id": f"{int(guild_id)}:{int(message_id)}:{emoji_key}"
        })
        return result.deleted_count > 0

    async def list_reaction_roles(self, guild_id: int) -> list[dict]:
        cursor = (await self.reaction_roles.find({"guild_id": int(guild_id)})).sort("message_id", 1)
        return [item async for item in cursor]

    async def clear_reaction_roles(self, guild_id: int) -> int:
        result = await self.reaction_roles.delete_many({"guild_id": int(guild_id)})
        return result.deleted_count

    async def claim_command_response(self, message_id: int) -> bool:
        """Prevent two bot workers from replying to the same prefix command."""
        try:
            await self.command_responses.insert_one({"_id": str(message_id), "created_at": datetime.now(timezone.utc)})
            return True
        except DuplicateKeyError:
            return False

    async def claim_command_cooldown(self, user_id: int, guild_id: int | None, command_name: str, duration: float) -> tuple[float, bool]:
        """Atomically enforce cooldowns and the one-time warning."""
        now = time.time()
        key = f"{int(user_id)}:{int(guild_id or 0)}:{command_name.casefold()}"
        purge_at = datetime.fromtimestamp(now + float(duration) + 3600, timezone.utc)
        return await self.store.claim_command_cooldown(key, duration, now, purge_at)

    async def add_noprefix_user(self, user_id: int, expires_at: float | None):
        await self.noprefix_users.update_one(
            {"_id": str(user_id)},
            {"$set": {"user_id": user_id, "expires_at": expires_at}},
            upsert=True,
        )

    async def remove_noprefix_user(self, user_id: int) -> bool:
        result = await self.noprefix_users.delete_one({"_id": str(user_id)})
        return result.deleted_count > 0

    async def get_noprefix_users(self) -> list[dict]:
        entries = []
        expired_ids = []
        now = time.time()
        cursor = await self.noprefix_users.find({})
        async for item in cursor:
            expires_at = item.get("expires_at")
            if expires_at is not None and float(expires_at) <= now:
                expired_ids.append(item["_id"])
            else:
                entries.append(item)
        if expired_ids:
            await self.noprefix_users.delete_many({"_id": {"$in": expired_ids}})
        return entries

    async def claim_noprefix_trial(self, user_id: int, duration_days: int) -> bool:
        """Atomically grant one lifetime free trial per user."""
        user_id = int(user_id)
        now = time.time()
        active = await self.noprefix_users.find_one({"_id": str(user_id)})
        if active and (active.get("expires_at") is None or float(active["expires_at"]) > now):
            return False
        expires_at = now + int(duration_days) * 86_400
        try:
            await self.noprefix_trial_claims.insert_one({
                "_id": str(user_id), "user_id": user_id,
                "claimed_at": now, "expires_at": expires_at,
                "duration_days": int(duration_days),
            })
        except DuplicateKeyError:
            return False
        try:
            await self.add_noprefix_user(user_id, expires_at)
        except Exception:
            await self.noprefix_trial_claims.delete_one({"_id": str(user_id)})
            raise
        return True

    async def set_server_blacklist(self, guild_id: int, reason: str, actor_id: int):
        await self.server_blacklist.update_one(
            {"_id": str(int(guild_id))},
            {"$set": {"guild_id": int(guild_id), "reason": reason, "actor_id": int(actor_id), "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def remove_server_blacklist(self, guild_id: int) -> bool:
        result = await self.server_blacklist.delete_one({"_id": str(int(guild_id))})
        return result.deleted_count > 0

    async def get_server_blacklist(self, guild_id: int) -> dict | None:
        return await self.server_blacklist.find_one({"_id": str(int(guild_id))})

    async def list_server_blacklist(self) -> list[dict]:
        cursor = (await self.server_blacklist.find({})).sort("created_at", -1)
        return [item async for item in cursor]

    async def set_global_ban(self, user_id: int, reason: str, actor_id: int):
        await self.global_bans.update_one(
            {"_id": str(int(user_id))},
            {"$set": {"user_id": int(user_id), "reason": reason, "actor_id": int(actor_id), "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def remove_global_ban(self, user_id: int) -> bool:
        result = await self.global_bans.delete_one({"_id": str(int(user_id))})
        return result.deleted_count > 0

    async def get_global_ban(self, user_id: int) -> dict | None:
        return await self.global_bans.find_one({"_id": str(int(user_id))})

    async def list_global_bans(self) -> list[dict]:
        cursor = (await self.global_bans.find({})).sort("created_at", -1)
        return [item async for item in cursor]

    async def get_owner_branding(self, guild_id: int) -> dict:
        return await self.owner_branding.find_one({"_id": str(int(guild_id))}) or {}

    async def set_owner_branding(self, guild_id: int, key: str, value):
        await self.owner_branding.update_one({"_id": str(int(guild_id))}, {"$set": {key: value}}, upsert=True)

    async def clear_owner_branding(self, guild_id: int):
        await self.owner_branding.delete_one({"_id": str(int(guild_id))})

