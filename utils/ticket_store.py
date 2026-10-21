"""MongoDB-backed configuration and ticket records for the ticket cog."""

import time


def _collection(database):
    return database.db["ticket_records"]


async def get_config(database, guild_id: int, setup_id: str = "1") -> dict | None:
    data = await database.get_guild(int(guild_id))
    stored = data.get("ticket")
    if not isinstance(stored, dict) or not stored:
        return None
    setups = stored.get("setups", {})
    # Older installs kept the button-panel configuration directly under
    # `ticket`; continue treating that as setup 1 until it is edited.
    selected = setups.get(str(setup_id)) if isinstance(setups, dict) else None
    if selected is None and str(setup_id) == "1":
        selected = {key: value for key, value in stored.items() if key != "setups"}
    if not isinstance(selected, dict) or not selected:
        return None
    config = dict(selected)
    # Preserve settings from the replaced Zyven ticket setup where names map
    # directly to the new wizard's fields.
    config = dict(config)
    aliases = {
        "support_role_id": "staff_role_id",
        "ticket_category_id": "category_id",
    }
    for new_key, old_key in aliases.items():
        if config.get(new_key) is None and config.get(old_key) is not None:
            config[new_key] = config[old_key]
    return config


async def set_config(database, guild_id: int, config: dict, setup_id: str = "1") -> None:
    data = await database.get_guild(int(guild_id))
    stored = data.get("ticket")
    stored = dict(stored) if isinstance(stored, dict) else {}
    setups = stored.get("setups")
    setups = dict(setups) if isinstance(setups, dict) else {}
    setup_id = str(setup_id)
    setups[setup_id] = dict(config)
    stored["setups"] = setups
    # Keep setup 1 mirrored at the old location for compatibility with
    # other ticket features and older deployments.
    if setup_id == "1":
        stored.update(config)
    await database.set_value(int(guild_id), "ticket", stored)


async def get_ticket(database, channel_id: int) -> dict | None:
    return await _collection(database).find_one({"_id": str(int(channel_id))})


async def get_user_open_count(database, guild_id: int, user_id: int, setup_id: str | None = None) -> int:
    query = {"guild_id": int(guild_id), "author_id": str(int(user_id)), "status": "open"}
    if setup_id is not None:
        if str(setup_id) == "1":
            query["$or"] = [{"setup_id": "1"}, {"setup_id": {"$exists": False}}]
        else:
            query["setup_id"] = str(setup_id)
    return await _collection(database).count_documents(query)


async def next_ticket_number(database, guild_id: int) -> int:
    guild_id = int(guild_id)
    counters = database.db["ticket_counters"]
    current = await counters.find_one({"_id": str(guild_id)})
    if current is None:
        cursor = (await _collection(database).find({"guild_id": guild_id})).sort("number", -1).limit(1)
        maximum = 0
        async for ticket in cursor:
            maximum = int(ticket.get("number", 0))
        await counters.find_one_and_update(
            {"_id": str(guild_id)}, {"$setOnInsert": {"value": maximum}}, upsert=True, return_document=True,
        )
    counter = await counters.find_one_and_update(
        {"_id": str(guild_id)}, {"$inc": {"value": 1}}, upsert=True, return_document=True,
    )
    return int(counter.get("value", 1))


async def create_ticket(database, channel_id: int, data: dict) -> None:
    record = dict(data)
    record["_id"] = str(int(channel_id))
    record["channel_id"] = str(int(channel_id))
    record["guild_id"] = int(record["guild_id"])
    record.setdefault("status", "open")
    record.setdefault("created_at", time.time())
    await _collection(database).insert_one(record)


async def update_ticket(database, channel_id: int, updates: dict) -> None:
    await _collection(database).update_one(
        {"_id": str(int(channel_id))}, {"$set": updates},
    )


async def delete_ticket(database, channel_id: int) -> None:
    await _collection(database).delete_many({"_id": str(int(channel_id))})
