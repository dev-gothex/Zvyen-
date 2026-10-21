import asyncio
import logging
import re
import time
from datetime import timedelta
from pymongo.errors import PyMongoError

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import panel, component_card
from utils.owner_permissions import owner_or_guild_permissions

log = logging.getLogger("zyven.eventlogs")
LEADING_EMOJI_RE = re.compile(r"^(?:(?:<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>|[^\w\s])\s*)+")

# The auto setup creates one channel per category, covering the 81 logging
# switches shown by the command's configuration panel.
LOG_GROUPS = {
    "members": ("member-logs", "Member Actions", 7),
    "messages": ("message-logs", "Message Actions", 16),
    "voice": ("voice-logs", "Voice Actions", 21),
    "server": ("server-logs", "Server Actions", 8),
    "channels": ("channel-logs", "Channel Actions", 13),
    "roles": ("role-logs", "Role Actions", 7),
    "moderation": ("mod-logs", "Mod Logs", 5),
    "server_events": ("server-logs", "Server Events", 4),
}
EVENT_TOTAL = 81
LOG_WEBHOOK_NAME = "Zyven Logs"
WEBHOOK_MIN_SEND_INTERVAL = 1.25
LOG_QUEUE_MAXSIZE = 32
AUDIT_CACHE_TTL = 1.5
AUDIT_MIN_REQUEST_INTERVAL = 1.0
AUDIT_LOOKUP_RETRIES = 2
CHANNEL_TITLES = {
    "members": "Member Logs Channel",
    "messages": "Message Logs Channel",
    "voice": "Voice Logs Channel",
    "server": "Server Logs Channel",
    "channels": "Channel Logs Channel",
    "roles": "Role Logs Channel",
    "moderation": "Mod Logs Channel",
}
CHANNEL_EVENT_LISTS = {
    "members": ("Member Join", "Member Leave", "Bot Add", "Bot Remove", "Nickname Update", "Members Pruned", "Server Avatar Changed"),
    "messages": (
        "Delete Attachment", "Poll Created", "Poll Deleted", "Poll Vote Added", "Poll Vote Removed", "Poll Finalized",
        "Delete Multiple Messages", "Delete Thread Message", "Delete Single Message", "Edit Thread Message", "Edit Message",
        "Thread Create", "Thread Delete", "Thread Updated", "Message Pinned", "Message Unpinned",
    ),
    "voice": (
        "Stream Start", "Stream End", "Video Start", "Video Stop", "Moved", "Voice Channel Join", "Voice Channel Leave",
        "Server Deafen", "Server Mute", "Server Undeafen", "Server Unmute", "Self Mute", "Self Unmute", "Self Deafen",
        "Self Undeafen", "Voice Activity Start", "Voice Activity Stop", "Stage Speaker Added", "Stage Speaker Removed",
        "Request to Speak", "Stage Channel Moved",
    ),
    "server": (
        "Invite Create", "Invite Delete", "Emoji Create", "Emoji Delete", "Emoji Update", "Event Create", "Event Deleted",
        "Event Updated", "Server Modified", "Server Icon Update", "Server Boost Added", "Server Boost Removed",
    ),
    "channels": (
        "Channel Create", "Channel Delete", "Channel Update", "Channel Permissions Added", "Channel Permissions Deleted",
        "Channel Permissions Updated", "Webhook Created", "Webhook Deleted", "Webhook Modified", "Webhook Avatar Updated",
        "Channel Followed", "Channel Unfollowed", "Followed Channel Updated",
    ),
    "roles": ("Role Create", "Role Delete", "Role Update", "Role Add", "Role Taken", "Role Permissions Updated", "Role Icon Update"),
    "moderation": ("Ban", "Timeout", "Kick", "Timeout Removed", "Unban"),
}


def permission_state(value: bool | None) -> str:
    return "Allow" if value is True else ("Deny" if value is False else "Inherit")


class EventLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._webhooks: dict[int, dict[str, discord.Webhook]] = {}
        self._missing_logging_routes: set[tuple[int, str]] = set()
        self._webhook_send_locks: dict[int, asyncio.Lock] = {}
        self._webhook_last_send_at: dict[int, float] = {}
        self._route_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._log_queues: dict[tuple[int, str], asyncio.Queue] = {}
        self._log_workers: dict[tuple[int, str], asyncio.Task] = {}
        self._audit_locks: dict[int, asyncio.Lock] = {}
        self._audit_cache: dict[int, tuple[float, list[tuple[int | None, int | None, object]]]] = {}
        self._audit_last_fetch_at: dict[int, float] = {}

    def cog_unload(self):
        for worker in self._log_workers.values():
            worker.cancel()
        self._log_workers.clear()

    @staticmethod
    async def send_panel(ctx, embed: discord.Embed):
        """Send command feedback in the bot's clean Components V2 card style."""
        await ctx.send(view=component_card(embed))

    async def post_channel_intro(self, channel: discord.TextChannel, group: str, actor):
        event_lines = "\n".join(f"{icon('space')}{icon('arrow')} {name}" for name in CHANNEL_EVENT_LISTS[group])
        setup_card = panel(
            CHANNEL_TITLES[group],
            f"{icon('reason')} **This channel will log the following events:**\n{event_lines}\n\n"
            f"Auto setup by {discord.utils.escape_markdown(actor.name)} • <t:{int(discord.utils.utcnow().timestamp())}:F>",
        )
        setup_card.timestamp = discord.utils.utcnow()
        setup_card.set_footer(text=f"Auto setup by {discord.utils.escape_markdown(actor.name)}")
        await channel.send(embed=setup_card)

    async def send_log(self, guild: discord.Guild, embed: discord.Embed, group: str = "server"):
        """Queue an event log without allowing gateway bursts to create unbounded waiters."""
        route = "server" if group == "server_events" else group
        key = (guild.id, route)
        queue = self._log_queues.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=LOG_QUEUE_MAXSIZE)
            self._log_queues[key] = queue
        try:
            queue.put_nowait((guild, embed, group))
        except asyncio.QueueFull:
            log.warning("Dropping event log for full route queue %s in guild %s", route, guild.id)
            return
        worker = self._log_workers.get(key)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._log_worker(key, queue))
            self._log_workers[key] = worker

    async def _log_worker(self, key: tuple[int, str], queue: asyncio.Queue):
        while True:
            guild, embed, group = await queue.get()
            try:
                await self._send_log_now(guild, embed, group)
            finally:
                queue.task_done()

    async def _send_fallback(self, guild, embed, config, logging_config, group, channel=None):
        if channel is None:
            channel_id = logging_config.get("channels", {}).get(group)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel:
            try:
                await channel.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send event log through fallback channel %s in guild %s", channel.id, guild.id)
            return
        fallback_id = config.get("security", {}).get("log_channel")
        fallback = guild.get_channel(int(fallback_id)) if fallback_id else None
        if fallback:
            try:
                await fallback.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send event log through configured fallback channel in guild %s", guild.id)
        route_key = (guild.id, group)
        if route_key not in self._missing_logging_routes:
            log.error("Missing logging webhook and usable channel for %s in guild %s", group, guild.id)
            self._missing_logging_routes.add(route_key)

    async def _repair_managed_route(self, guild, webhook_group, stale_webhook_id=None):
        """Create or re-read one managed route while holding its per-route lock."""
        route_key = (guild.id, webhook_group)
        route_lock = self._route_locks.setdefault(route_key, asyncio.Lock())
        async with route_lock:
            try:
                config = await self.bot.db.get_guild(guild.id)
            except PyMongoError:
                log.exception("Could not reload event-log settings for guild %s", guild.id)
                return {}, {}, None, None
            logging_config = config.get("logging", {})
            if not logging_config.get("managed") or not logging_config.get("enabled", False):
                return config, logging_config, None, None
            current_id = logging_config.get("webhooks", {}).get(webhook_group)
            # Another event may have repaired this route while this request was
            # waiting for the lock. Reuse its persisted ID instead of creating
            # another Discord resource.
            if current_id and (stale_webhook_id is None or int(current_id) != int(stale_webhook_id)):
                channel_id = logging_config.get("channels", {}).get(webhook_group)
                channel = guild.get_channel(int(channel_id)) if channel_id else None
                return config, logging_config, int(current_id), channel

            channel_id = logging_config.get("channels", {}).get(webhook_group)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            bot_member = guild.me
            category_id = logging_config.get("created_category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if channel is None and isinstance(category, discord.CategoryChannel) and bot_member and bot_member.guild_permissions.manage_channels:
                try:
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        bot_member: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            embed_links=True,
                            manage_webhooks=True,
                        ),
                    }
                    channel = await guild.create_text_channel(
                        LOG_GROUPS[webhook_group][0],
                        category=category,
                        overwrites=overwrites,
                        reason="Restore missing Zyven logging channel",
                    )
                    channels = dict(logging_config.get("channels", {}))
                    channels[webhook_group] = channel.id
                    created_channel_ids = list(logging_config.get("created_channel_ids", []))
                    created_channel_ids.append(channel.id)
                    await self.bot.db.set_values(guild.id, {
                        "logging.channels": channels,
                        "logging.created_channel_ids": created_channel_ids,
                    })
                    logging_config["channels"] = channels
                    logging_config["created_channel_ids"] = created_channel_ids
                    await self.post_channel_intro(channel, webhook_group, bot_member)
                except (discord.Forbidden, discord.HTTPException, PyMongoError):
                    channel = None
                    log.warning("Could not repair logging channel for %s in guild %s", webhook_group, guild.id)
            if channel and bot_member and channel.permissions_for(bot_member).manage_webhooks:
                try:
                    webhook = await channel.create_webhook(
                        name=LOG_WEBHOOK_NAME,
                        reason="Repair missing Zyven event logging webhook",
                    )
                    webhooks = dict(logging_config.get("webhooks", {}))
                    webhooks[webhook_group] = webhook.id
                    await self.bot.db.set_nested(guild.id, "logging", "webhooks", webhooks)
                    logging_config["webhooks"] = webhooks
                    self._webhooks.setdefault(guild.id, {})[webhook_group] = webhook
                    log.warning("Recreated logging webhook for %s in guild %s", webhook_group, guild.id)
                    return config, logging_config, int(webhook.id), channel
                except (discord.Forbidden, discord.HTTPException, PyMongoError):
                    log.exception("Could not repair logging webhook for %s in guild %s", webhook_group, guild.id)
            if stale_webhook_id is not None and current_id and int(current_id) == int(stale_webhook_id):
                try:
                    await self.bot.db.unset_value(guild.id, f"logging.webhooks.{webhook_group}")
                    webhooks = dict(logging_config.get("webhooks", {}))
                    webhooks.pop(webhook_group, None)
                    logging_config["webhooks"] = webhooks
                except PyMongoError:
                    log.exception("Could not clear deleted webhook ID for %s in guild %s", webhook_group, guild.id)
            return config, logging_config, None, channel

    async def _send_log_now(self, guild: discord.Guild, embed: discord.Embed, group: str = "server", repair_attempted: bool = False):
        try:
            config = await self.bot.db.get_guild(guild.id)
        except PyMongoError:
            log.exception("Could not load event-log settings for guild %s", guild.id)
            return
        logging_config = config.get("logging", {})
        if logging_config.get("managed"):
            if not logging_config.get("enabled", False):
                return
            webhook_group = "server" if group == "server_events" else group
            webhook_id = logging_config.get("webhooks", {}).get(webhook_group)
            if not webhook_id:
                original_config, original_logging = config, logging_config
                config, logging_config, webhook_id, channel = await self._repair_managed_route(guild, webhook_group)
                if not webhook_id:
                    await self._send_fallback(
                        guild,
                        embed,
                        config or original_config,
                        logging_config or original_logging,
                        webhook_group,
                        channel,
                    )
                    return
            try:
                cache = self._webhooks.setdefault(guild.id, {})
                # Keep the existing per-webhook lock and interval: the queue
                # bounds waiters, while this also protects shared webhook IDs.
                send_lock = self._webhook_send_locks.setdefault(int(webhook_id), asyncio.Lock())
                async with send_lock:
                    webhook = cache.get(webhook_group)
                    if webhook is None or webhook.id != int(webhook_id):
                        webhook = await self.bot.fetch_webhook(int(webhook_id))
                        cache[webhook_group] = webhook
                    if webhook.name != LOG_WEBHOOK_NAME:
                        webhook = await webhook.edit(
                            name=LOG_WEBHOOK_NAME,
                            reason="Use Zyven branding for event logging",
                        )
                        cache[webhook_group] = webhook
                    if embed.title:
                        embed.title = LEADING_EMOJI_RE.sub("", embed.title).strip()
                    embed.timestamp = discord.utils.utcnow()
                    embed.set_footer(text=f"{guild.name} • {discord.utils.utcnow().strftime('%H:%M')}")
                    elapsed = time.monotonic() - self._webhook_last_send_at.get(int(webhook.id), 0.0)
                    if elapsed < WEBHOOK_MIN_SEND_INTERVAL:
                        await asyncio.sleep(WEBHOOK_MIN_SEND_INTERVAL - elapsed)
                    await webhook.send(
                        embed=embed,
                        username=LOG_WEBHOOK_NAME,
                        avatar_url=self.bot.user.display_avatar.url,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self._webhook_last_send_at[int(webhook.id)] = time.monotonic()
                self._missing_logging_routes.discard((guild.id, webhook_group))
            except discord.NotFound as exc:
                if exc.code != 10015:
                    log.warning("Webhook for event logs is unavailable (id=%s, guild=%s, code=%s)", webhook_id, guild.id, exc.code)
                    return
                self._webhooks.setdefault(guild.id, {}).pop(webhook_group, None)
                if repair_attempted:
                    log.warning("Repaired event-log webhook was deleted again for %s in guild %s", webhook_group, guild.id)
                    await self._send_fallback(guild, embed, config, logging_config, webhook_group)
                    return
                fresh_config, fresh_logging, replacement_id, channel = await self._repair_managed_route(
                    guild, webhook_group, stale_webhook_id=webhook_id
                )
                if replacement_id:
                    # The helper re-read state under the route lock. Retry once
                    # with that state, never recursively repairing forever.
                    await self._send_log_now(guild, embed, group, repair_attempted=True)
                else:
                    await self._send_fallback(
                        guild,
                        embed,
                        fresh_config or config,
                        fresh_logging or logging_config,
                        webhook_group,
                        channel,
                    )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to send event log through webhook %s (guild %s)", webhook_id, guild.id)
            return
        channel_id = config.get("security", {}).get("log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            try:
                await channel.send(view=component_card(embed))
            except discord.Forbidden:
                log.warning("Missing permission to send event logs in channel %s (guild %s)", channel.id, guild.id)
            except discord.HTTPException:
                log.exception("Failed to send event log in channel %s (guild %s)", channel.id, guild.id)

    @commands.group(name="logging", invoke_without_command=True)
    @commands.guild_only()
    async def logging_group(self, ctx):
        await self.send_panel(ctx, panel("Logging Commands", f"Use `{settings.prefix}logging setup` or `{settings.prefix}logging auto` to configure logging."))

    @logging_group.command(name="setup")
    async def logging_setup(self, ctx):
        embed = panel(
            "Logging Dashboard — Coming Soon",
            f"Advanced logging configuration on the dashboard is coming soon.\n\n"
            f"For now, use `{settings.prefix}logging auto` to create and enable the full logging system.\n\n"
            f"Use `{settings.prefix}logging config` to view its status, or `{settings.prefix}logging reset` to remove the auto-created setup.",
        )
        await self.send_panel(ctx, embed)

    @logging_group.command(name="auto")
    @owner_or_guild_permissions(manage_channels=True)
    async def logging_auto(self, ctx):
        config = await self.bot.db.get_guild(ctx.guild.id)
        existing = config.get("logging", {})
        if existing.get("managed"):
            # Upgrade existing setups made before webhook logging was added.
            # Reuse their channels and only fill missing webhook configuration.
            webhooks = existing.get("webhooks", {})
            migrated = False
            try:
                for group in ("members", "messages", "voice", "server", "channels", "roles", "moderation"):
                    channel_id = existing.get("channels", {}).get(group)
                    channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None
                    webhook_id = webhooks.get(group)
                    if webhook_id:
                        try:
                            webhook = await self.bot.fetch_webhook(int(webhook_id))
                            if webhook.name != LOG_WEBHOOK_NAME:
                                webhook = await webhook.edit(
                                    name=LOG_WEBHOOK_NAME,
                                    reason="Use Zyven branding for event logging",
                                )
                            self._webhooks.setdefault(ctx.guild.id, {})[group] = webhook
                            continue
                        except discord.NotFound:
                            # Replace stale IDs below when the channel remains.
                            pass
                    if channel is None:
                        continue
                    webhook = await channel.create_webhook(name=LOG_WEBHOOK_NAME, reason=f"Logging webhook migration by {ctx.author}")
                    webhooks[group] = webhook.id
                    self._webhooks.setdefault(ctx.guild.id, {})[group] = webhook
                    await self.post_channel_intro(channel, group, ctx.author)
                    migrated = True
                if migrated:
                    await self.bot.db.set_nested(ctx.guild.id, "logging", "webhooks", webhooks)
            except (discord.Forbidden, discord.HTTPException, PyMongoError) as exc:
                log.exception("Could not upgrade logging webhooks in guild %s", ctx.guild.id)
                return await self.send_panel(ctx, panel("Logging Setup Needs Attention", f"The logging channels already exist, but webhook setup could not finish: {exc}"))
            if existing.get("enabled"):
                message = f"The logging ecosystem is already set up and enabled{'; webhook logging is ready' if migrated else ''}. Use `{settings.prefix}logging reset` before creating it again."
            else:
                message = f"The logging ecosystem is already set up, but currently disabled{'; webhook logging is ready' if migrated else ''}. Use `{settings.prefix}logging enable` to resume it, or `{settings.prefix}logging reset` before setting it up again."
            return await self.send_panel(ctx, panel("Logging Already Setup", message))

        me = ctx.guild.me
        if not me or not me.guild_permissions.manage_channels:
            return await self.send_panel(ctx, panel("Permission Required", "I need **Manage Channels** permission to create the logging channels."))
        if not me.guild_permissions.manage_webhooks:
            return await self.send_panel(ctx, panel("Permission Required", "I need **Manage Webhooks** permission to create webhook-based log messages."))
        progress = await ctx.send(view=component_card(panel(
            f"{icon('loading')} Setting Up Logging",
            "Creating the logging category and channels, then enabling event logs…",
        )))
        created = []
        try:
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=True, manage_webhooks=True),
            }
            category = await ctx.guild.create_category("Zyven Logs", overwrites=overwrites, reason=f"Logging auto setup by {ctx.author}")
            created.append(category)
            channels = {}
            webhooks = {}
            for group in ("members", "messages", "voice", "server", "channels", "roles", "moderation"):
                channel_name = LOG_GROUPS[group][0]
                channel = await ctx.guild.create_text_channel(channel_name, category=category, reason=f"Logging auto setup by {ctx.author}")
                created.append(channel)
                channels[group] = channel.id
                webhook = await channel.create_webhook(name=LOG_WEBHOOK_NAME, reason=f"Logging auto setup by {ctx.author}")
                webhooks[group] = webhook.id
                self._webhooks.setdefault(ctx.guild.id, {})[group] = webhook
                await self.post_channel_intro(channel, group, ctx.author)
            record = {"managed": True, "enabled": True, "category_id": category.id, "channels": channels, "webhooks": webhooks, "created_channel_ids": [item.id for item in created if isinstance(item, discord.TextChannel)], "created_category_id": category.id, "enabled_events": EVENT_TOTAL}
            await self.bot.db.set_nested(ctx.guild.id, "logging", "managed", record["managed"])
            for key, value in record.items():
                if key != "managed":
                    await self.bot.db.set_nested(ctx.guild.id, "logging", key, value)
        except (discord.Forbidden, discord.HTTPException, PyMongoError) as exc:
            try:
                await self.bot.db.unset_value(ctx.guild.id, "logging")
            except PyMongoError:
                log.exception("Could not clear partial logging config in guild %s", ctx.guild.id)
            for item in reversed(created):
                try:
                    await item.delete(reason="Rollback incomplete logging auto setup")
                except discord.HTTPException:
                    pass
            self._webhooks.pop(ctx.guild.id, None)
            log.exception("Logging auto setup failed in guild %s", ctx.guild.id)
            await progress.edit(view=component_card(panel("Logging Setup Failed", f"Could not create the logging channels: {exc}")))
            return
        names = "\n".join(f"› <#{channels[group]}> (`{len(CHANNEL_EVENT_LISTS[group])} events`)" for group in channels)
        await progress.edit(view=component_card(panel("Auto Logging Setup Complete!", f"{icon('greentick')} Successfully created the logging ecosystem.\n\n{icon('members')} **Category:** {category.mention}\n{icon('wtick')} **Total Events:** `{EVENT_TOTAL}` enabled\n\n{names}")))

    @logging_group.command(name="config")
    async def logging_config(self, ctx):
        data = (await self.bot.db.get_guild(ctx.guild.id)).get("logging", {})
        active = bool(data.get("managed") and data.get("enabled"))
        channels = data.get("channels", {})
        status_mark = icon("greentick") if active else icon("cross")
        lines = [
            f"{icon('wtick')} **Status:** {'Enabled' if active else 'Disabled'} {status_mark}",
            f"{icon('members')} **Enabled Events:** `{EVENT_TOTAL if active else 0}/{EVENT_TOTAL}`",
            "",
            f"{icon('reason')} **Channel Mapping:**",
        ]
        mapping_labels = ("Member-Logs", "Message-Logs", "Voice-Logs", "Server-Logs", "Channel-Logs", "Role-Logs", "Moderation")
        for (group, (_, _, _)), label in zip(list(LOG_GROUPS.items())[:7], mapping_labels):
            channel = ctx.guild.get_channel(channels.get(group)) if channels.get(group) else None
            lines.append(f"{icon('space')}{icon('arrow')} **{label}:** {channel.mention if channel else '`Not Found`'}")
        lines.extend(["", f"{icon('reason')} **Category Status:**"])
        category_order = ("members", "messages", "voice", "server", "channels", "server_events", "roles", "moderation")
        for group in category_order:
            _, label, count = LOG_GROUPS[group]
            lines.append(f"{icon('space')}{icon('arrow')} **{label}:** `{count if active else 0}/{count}` {icon('greentick') if active else icon('cross')}")
        lines.extend(["", f"Requested by {discord.utils.escape_markdown(ctx.author.name)}"])
        await self.send_panel(ctx, panel("Logging Configuration", "\n".join(lines)))

    @logging_group.command(name="enable")
    @owner_or_guild_permissions(manage_channels=True)
    async def logging_enable(self, ctx):
        data = (await self.bot.db.get_guild(ctx.guild.id)).get("logging", {})
        if not data.get("managed"):
            return await self.send_panel(ctx, panel("Setup Required", f"Setup logging first using `{settings.prefix}logging setup` or `{settings.prefix}logging auto`."))
        if data.get("enabled"):
            return await self.send_panel(ctx, panel("Logging Already Enabled", "Logging is already active."))
        await self.bot.db.set_nested(ctx.guild.id, "logging", "enabled", True)
        await self.send_panel(ctx, panel("Logging Enabled!", "Successfully resumed logging services."))

    @logging_group.command(name="disable")
    @owner_or_guild_permissions(manage_channels=True)
    async def logging_disable(self, ctx):
        data = (await self.bot.db.get_guild(ctx.guild.id)).get("logging", {})
        if not data.get("managed"):
            return await self.send_panel(ctx, panel("Setup Required", f"Setup logging first using `{settings.prefix}logging setup` or `{settings.prefix}logging auto`."))
        if not data.get("enabled"):
            return await self.send_panel(ctx, panel("Logging Already Disabled", "Logging is already disabled."))
        await self.bot.db.set_nested(ctx.guild.id, "logging", "enabled", False)
        await self.send_panel(ctx, panel("Logging Disabled", "Logging services have been paused. Channels remain available."))

    @logging_group.command(name="reset")
    @owner_or_guild_permissions(manage_channels=True)
    async def logging_reset(self, ctx):
        data = (await self.bot.db.get_guild(ctx.guild.id)).get("logging", {})
        removed = 0
        await self.bot.db.set_nested(ctx.guild.id, "logging", "enabled", False)
        for channel_id in data.get("created_channel_ids", []):
            channel = ctx.guild.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.delete(reason=f"Logging reset by {ctx.author}")
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not remove logging channel %s", channel_id)
        category = ctx.guild.get_channel(data.get("created_category_id")) if data.get("created_category_id") else None
        if category and not getattr(category, "channels", []):
            try:
                await category.delete(reason=f"Logging reset by {ctx.author}")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not remove logging category %s", category.id)
        await self.bot.db.unset_value(ctx.guild.id, "logging")
        self._webhooks.pop(ctx.guild.id, None)
        moderator = discord.utils.escape_markdown(ctx.author.name)
        await self.send_panel(ctx, panel(
            "Logging Reset Complete!",
            f"{icon('wtick')} **Successfully reset logging**\n"
            f"{icon('members')} **Moderator:** `{moderator}`\n\n"
            f"{icon('space')}{icon('arrow')} **Channels Removed:** `{removed}`",
        ))

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        deleter, audit_status = await self.find_message_deleter(message)
        deleted_by = (
            f"{deleter.mention} (`{deleter.id}`)"
            + (f" — {audit_status}" if audit_status else "")
            if deleter else audit_status
        )
        embed = panel(
            f"{icon('delete')} Message Deleted",
            f"**Author:** {message.author.mention} (`{message.author.id}`)\n"
            f"**Deleted by:** {deleted_by}\n**Channel:** {message.channel.mention}",
            color=0xED4245,
        )
        if message.content:
            embed.add_field(name="Content", value=message.content[:1024], inline=False)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(a.url for a in message.attachments)[:1024], inline=False)
        await self.send_log(message.guild, embed, "messages")

    async def find_message_deleter(self, message: discord.Message):
        """Return the moderator from Discord's audit log when one is recorded.

        Discord's message-delete gateway event contains no actor. Moderator
        deletions are only attributable through View Audit Log permission and
        the separately-delivered audit entry; self-deletions have no actor entry.
        """
        guild = message.guild
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None, "Unknown (grant the bot View Audit Log permission)"

        # Audit entries are delivered separately from gateway events. A guild
        # lock makes a burst one single-flight lookup, and the short-lived
        # bounded snapshot prevents every deletion from issuing its own request.
        audit_lock = self._audit_locks.setdefault(guild.id, asyncio.Lock())
        async with audit_lock:
            initial_cache = self._audit_cache.get(guild.id)
            cache_was_fresh = bool(initial_cache and time.monotonic() - initial_cache[0] <= AUDIT_CACHE_TTL)
            for attempt in range(AUDIT_LOOKUP_RETRIES):
                cached = self._audit_cache.get(guild.id)
                if cached and time.monotonic() - cached[0] <= AUDIT_CACHE_TTL:
                    entries = cached[1]
                else:
                    wait = AUDIT_MIN_REQUEST_INTERVAL - (time.monotonic() - self._audit_last_fetch_at.get(guild.id, 0.0))
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._audit_last_fetch_at[guild.id] = time.monotonic()
                    entries = []
                    try:
                        async for entry in guild.audit_logs(limit=25, action=discord.AuditLogAction.message_delete):
                            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                            if age > 20:
                                break
                            entries.append((
                                getattr(entry.target, "id", None),
                                getattr(getattr(entry.extra, "channel", None), "id", None),
                                entry.user,
                            ))
                    except (discord.Forbidden, discord.HTTPException):
                        log.debug("Unable to read message-delete audit log in guild %s", guild.id, exc_info=True)
                        return None, "Unknown (Discord audit log access failed)"
                    self._audit_cache[guild.id] = (time.monotonic(), entries)
                for target_id, channel_id, user in entries:
                    if target_id == message.author.id and channel_id == message.channel.id:
                        return user, None
                if attempt + 1 < AUDIT_LOOKUP_RETRIES and not cache_was_fresh:
                    # Force one bounded refresh after the gateway/audit race;
                    # concurrent callers then reuse that refreshed snapshot.
                    self._audit_cache.pop(guild.id, None)
                    await asyncio.sleep(0.45)
        # A self-deletion has no moderator audit entry. The message author is
        # therefore the best actor attribution Discord exposes in that case.
        # Mark it as likely because audit logs cannot distinguish a self-delete
        # from an unlogged/system deletion with certainty.
        return message.author, "likely self-delete; no moderator audit entry found"

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages:
            return
        guild = messages[0].guild
        if guild is None:
            return
        channel = messages[0].channel
        oldest = min((message.created_at for message in messages if message.created_at), default=None)
        description = f"**Count:** {len(messages)}\n**Channel:** {channel.mention}"
        if oldest:
            description += f"\n**Oldest included:** <t:{int(oldest.timestamp())}:R>"
        embed = panel(f"{icon('cleanup')} Messages Bulk Deleted", description, color=0xED4245)
        await self.send_log(guild, embed, "messages")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        embed = panel(f"{icon('edit')} Message Edited", f"**Author:** {before.author.mention} (`{before.author.id}`)\n**Channel:** {before.channel.mention}", color=0xFEE75C)
        embed.add_field(name="Before", value=(before.content or "*(empty)*")[:1024], inline=False)
        embed.add_field(name="After", value=(after.content or "*(empty)*")[:1024], inline=False)
        embed.add_field(name="Jump to Message", value=f"[Open message]({after.jump_url})", inline=False)
        await self.send_log(before.guild, embed, "messages")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        title = "Bot Added" if member.bot else "Member Joined"
        embed = panel(f"{icon('welcome')} {title}", f"{member.mention} (`{member.id}`) joined the server.", color=0x57F287)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        embed.add_field(name="Member Count", value=str(member.guild.member_count or 0), inline=True)
        await self.send_log(member.guild, embed, "members")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        title = "Bot Removed" if member.bot else "Member Left"
        embed = panel(f"{icon('welcome')} {title}", f"**{discord.utils.escape_markdown(str(member))}** (`{member.id}`) left the server.", color=0xED4245)
        await self.send_log(member.guild, embed, "members")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self.send_log(guild, panel(f"{icon('moderation')} Member Banned", f"{discord.utils.escape_markdown(str(user))} (`{user.id}`) was banned.", color=0xED4245), "moderation")

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.send_log(guild, panel(f"{icon('moderation')} Member Unbanned", f"{discord.utils.escape_markdown(str(user))} (`{user.id}`) was unbanned.", color=0x57F287), "moderation")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            embed = panel(f"{icon('edit')} Nickname Changed", f"**Member:** {after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}")
            await self.send_log(after.guild, embed, "members")
        if before.guild_avatar != after.guild_avatar:
            await self.send_log(after.guild, panel("Server Avatar Changed", f"**Member:** {after.mention}\nThe member's server avatar was updated."), "members")
        if before.premium_since != after.premium_since:
            title = "Server Boost Added" if after.premium_since else "Server Boost Removed"
            await self.send_log(after.guild, panel(title, f"**Member:** {after.mention}"), "server")
        before_roles, after_roles = set(before.roles), set(after.roles)
        added, removed = after_roles - before_roles, before_roles - after_roles
        if added:
            await self.send_log(after.guild, panel("Roles Added", f"**User:** {after.mention} (`{after.id}`)\n**Roles:** {', '.join(role.mention for role in added)[:900]}"), "roles")
        if removed:
            await self.send_log(after.guild, panel("Roles Removed", f"**User:** {after.mention} (`{after.id}`)\n**Roles:** {', '.join(role.mention for role in removed)[:900]}"), "roles")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        await self.send_log(channel.guild, panel(f"{icon('folder')} Channel Created", f"{channel.mention} (`{channel.id}`) was created."), "channels")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self.send_log(channel.guild, panel(f"{icon('folder')} Channel Deleted", f"**{discord.utils.escape_markdown(channel.name)}** (`{channel.id}`) was deleted.", color=0xED4245), "channels")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{discord.utils.escape_markdown(before.name)}` → `{discord.utils.escape_markdown(after.name)}`")
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.topic != after.topic:
                changes.append("**Topic:** updated")
            if before.slowmode_delay != after.slowmode_delay:
                changes.append(f"**Slowmode:** {before.slowmode_delay}s → {after.slowmode_delay}s")
            old_send = before.overwrites_for(before.guild.default_role).send_messages
            new_send = after.overwrites_for(after.guild.default_role).send_messages
            if old_send != new_send:
                changes.append(f"**@everyone send messages:** {permission_state(old_send)} → {permission_state(new_send)}")
        if isinstance(before, discord.VoiceChannel) and isinstance(after, discord.VoiceChannel):
            if before.user_limit != after.user_limit:
                changes.append(f"**User limit:** {before.user_limit or 'Unlimited'} → {after.user_limit or 'Unlimited'}")
            if before.bitrate != after.bitrate:
                changes.append(f"**Bitrate:** {before.bitrate // 1000} → {after.bitrate // 1000} kbps")
            old_connect = before.overwrites_for(before.guild.default_role).connect
            new_connect = after.overwrites_for(after.guild.default_role).connect
            if old_connect != new_connect:
                changes.append(f"**@everyone connect:** {permission_state(old_connect)} → {permission_state(new_connect)}")
        if changes:
            await self.send_log(after.guild, panel(f"{icon('edit')} Channel Updated", f"{after.mention}\n" + "\n".join(changes)), "channels")
        old_overwrites, new_overwrites = before.overwrites, after.overwrites
        old_targets, new_targets = set(old_overwrites), set(new_overwrites)
        added_targets = new_targets - old_targets
        removed_targets = old_targets - new_targets
        updated_targets = {target for target in old_targets & new_targets if old_overwrites[target] != new_overwrites[target]}
        for title, targets in (("Channel Permissions Added", added_targets), ("Channel Permissions Deleted", removed_targets), ("Channel Permissions Updated", updated_targets)):
            if targets:
                target_names = ", ".join(getattr(target, "mention", str(target)) for target in targets)
                await self.send_log(after.guild, panel(title, f"**Channel:** {after.mention}\n**Target:** {target_names[:900]}"), "channels")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        await self.send_log(role.guild, panel(f"{icon('role')} Role Created", f"{role.mention} (`{role.id}`) was created."), "roles")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self.send_log(role.guild, panel(f"{icon('role')} Role Deleted", f"**{discord.utils.escape_markdown(role.name)}** (`{role.id}`) was deleted.", color=0xED4245), "roles")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{discord.utils.escape_markdown(before.name)}` → `{discord.utils.escape_markdown(after.name)}`")
        if before.permissions != after.permissions:
            changes.append("**Permissions:** updated")
        if before.position != after.position:
            changes.append(f"**Position:** {before.position} → {after.position}")
        if before.display_icon != after.display_icon:
            changes.append("**Role Icon:** updated")
        if changes:
            await self.send_log(after.guild, panel(f"{icon('role')} Role Updated", f"{after.mention}\n" + "\n".join(changes)), "roles")

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        old = {emoji.id: emoji for emoji in before}
        new = {emoji.id: emoji for emoji in after}
        for emoji_id in new.keys() - old.keys():
            emoji = new[emoji_id]
            await self.send_log(guild, panel("Emoji Created", f"**Emoji:** {emoji}\n**Animated:** {'Yes' if emoji.animated else 'No'}", color=0x57F287), "server_events")
        for emoji_id in old.keys() - new.keys():
            emoji = old[emoji_id]
            await self.send_log(guild, panel("Emoji Deleted", f"**Emoji:** `:{emoji.name}:` (`{emoji.id}`)", color=0xED4245), "server_events")
        for emoji_id in old.keys() & new.keys():
            if old[emoji_id].name != new[emoji_id].name:
                await self.send_log(guild, panel("Emoji Updated", f"**Before:** `:{old[emoji_id].name}:`\n**After:** {new[emoji_id]}", color=0xFEE75C), "server_events")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            await self.send_log(invite.guild, panel("Invite Created", f"**Code:** `{invite.code}`\n**Channel:** {invite.channel.mention if invite.channel else 'Unknown'}\n**Inviter:** {invite.inviter.mention if invite.inviter else 'Unknown'}"), "server_events")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            await self.send_log(invite.guild, panel("Invite Deleted", f"**Code:** `{invite.code}`\n**Channel:** {invite.channel.mention if invite.channel else 'Unknown'}"), "server_events")

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent):
        await self.send_log(event.guild, panel("Event Created", f"**Event:** {discord.utils.escape_markdown(event.name)}\n**Starts:** <t:{int(event.start_time.timestamp())}:F>"), "server_events")

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        await self.send_log(event.guild, panel("Event Deleted", f"**Event:** {discord.utils.escape_markdown(event.name)}"), "server_events")

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.start_time != after.start_time:
            changes.append(f"**Start:** <t:{int(before.start_time.timestamp())}:F> → <t:{int(after.start_time.timestamp())}:F>")
        if before.description != after.description:
            changes.append("**Description:** updated")
        if changes:
            await self.send_log(after.guild, panel("Event Updated", "\n".join(changes)), "server_events")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{discord.utils.escape_markdown(before.name)}` → `{discord.utils.escape_markdown(after.name)}`")
        if before.verification_level != after.verification_level:
            changes.append(f"**Verification level:** {before.verification_level} → {after.verification_level}")
        if before.icon != after.icon:
            await self.send_log(after, panel("Server Icon Updated", "The server icon was changed."), "server_events")
        if changes:
            await self.send_log(after, panel(f"{icon('edit')} Server Settings Updated", "\n".join(changes)), "server")

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        await self.send_log(thread.guild, panel("Thread Created", f"**Thread:** {thread.mention}\n**Parent:** {thread.parent.mention if thread.parent else 'Unknown'}"), "messages")

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        await self.send_log(thread.guild, panel("Thread Deleted", f"**Thread:** `{discord.utils.escape_markdown(thread.name)}` (`{thread.id}`)", color=0xED4245), "messages")

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.archived != after.archived:
            changes.append(f"**Archived:** {'Yes' if after.archived else 'No'}")
        if changes:
            await self.send_log(after.guild, panel("Thread Updated", f"**Thread:** {after.mention}\n" + "\n".join(changes)), "messages")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel and before.mute == after.mute and before.deaf == after.deaf and before.self_mute == after.self_mute and before.self_deaf == after.self_deaf and before.self_stream == after.self_stream and before.self_video == after.self_video:
            return
        old_channel = before.channel.mention if before.channel else "None"
        new_channel = after.channel.mention if after.channel else "None"
        details = [f"**Member:** {member.mention}", f"**Channel:** {old_channel} → {new_channel}"]
        for label, old, new in (("Server mute", before.mute, after.mute), ("Server deafen", before.deaf, after.deaf), ("Self mute", before.self_mute, after.self_mute), ("Self deafen", before.self_deaf, after.self_deaf), ("Camera", before.self_video, after.self_video), ("Stream", before.self_stream, after.self_stream)):
            if old != new:
                details.append(f"**{label}:** {'On' if new else 'Off'}")
        await self.send_log(member.guild, panel(f"{icon('voice')} Voice State Updated", "\n".join(details)), "voice")


async def setup(bot):
    await bot.add_cog(EventLogs(bot))
