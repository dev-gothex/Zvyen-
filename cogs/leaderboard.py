"""Opt-in voice, message and invite statistics with paged leaderboard cards."""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import time

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, panel

log = logging.getLogger("zyven.leaderboard")
ACCENT = 0xFFFF00


class LeaderboardFeatureDisabled(commands.CheckFailure):
    def __init__(self, feature):
        self.feature = feature
        super().__init__(f"Leaderboard feature is disabled: {feature}")


def periods(now=None):
    now = now or datetime.now(timezone.utc)
    iso = now.isocalendar()
    return now.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}"


def voice_time_increments(started, ended):
    """Split an integer-second voice interval across its actual UTC days/weeks."""
    start, end = int(float(started)), int(float(ended))
    increments = {"voice.all": max(0, end - start)}
    cursor = start
    while cursor < end:
        current = datetime.fromtimestamp(cursor, timezone.utc)
        next_day = datetime(current.year, current.month, current.day, tzinfo=timezone.utc) + timedelta(days=1)
        segment_end = min(end, int(next_day.timestamp()))
        seconds = segment_end - cursor
        day, week = periods(current)
        increments[f"voice.daily.{day}"] = increments.get(f"voice.daily.{day}", 0) + seconds
        increments[f"voice.weekly.{week}"] = increments.get(f"voice.weekly.{week}", 0) + seconds
        cursor = segment_end
    return increments


def pretty_duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ", ".join(parts)


class BoardView(discord.ui.View):
    def __init__(self, cog, ctx, mode, period, page=0):
        super().__init__(timeout=180)
        self.cog, self.ctx, self.mode, self.period, self.page = cog, ctx, mode, period, page
        # Match the custom navigation emoji controls used by the help panels.
        nav = (("left", "first"), ("previous", "previous"), ("delete", "delete"), ("skip", "next"), ("right", "last"))
        for emoji_name, action in nav:
            button = discord.ui.Button(emoji=discord.PartialEmoji.from_str(icon(emoji_name)), style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._callback(action)
            self.add_item(button)

    def _callback(self, action):
        async def callback(interaction):
            if interaction.user.id != self.ctx.author.id:
                await interaction.response.send_message("Only the person who requested this leaderboard can use these buttons.", ephemeral=True)
                return
            records = await self.cog.ranked(self.ctx.guild.id, self.mode, self.period)
            pages = max(1, (len(records) + 9) // 10)
            if action == "delete":
                await interaction.message.delete()
                return
            self.page = {"first": 0, "previous": max(0, self.page - 1), "next": min(pages - 1, self.page + 1), "last": pages - 1}[action]
            await interaction.response.edit_message(view=component_card(self.cog.board_embed(self.ctx, self.mode, self.period, records, self.page), self, accent_colour=ACCENT))
        return callback


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stats = bot.db.leaderboard_stats
        self.invite_cache = {}
        self._pending_message_counts = defaultdict(int)
        self._flushing_message_counts = defaultdict(int)
        self._message_flush_task: asyncio.Task | None = None
        self._last_message_flush_warning = 0.0
        # Prevent repeated identical messages from inflating leaderboard stats.
        self._recent_message_fingerprints = {}

    async def cog_load(self):
        self._message_flush_task = asyncio.create_task(self._message_flush_loop(), name="zyven-leaderboard-message-flush")

    async def cog_unload(self):
        if self._message_flush_task:
            self._message_flush_task.cancel()
            try:
                await self._message_flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_message_counts()

    async def _message_flush_loop(self):
        while True:
            await asyncio.sleep(2)
            await self._flush_message_counts()

    async def _flush_message_counts(self):
        if not self._pending_message_counts:
            return
        batch = self._pending_message_counts
        self._pending_message_counts = defaultdict(int)
        items = list(batch.items())
        for key, amount in items:
            self._flushing_message_counts[key] += amount
        for index, ((guild_id, user_id, day, week), amount) in enumerate(items):
            try:
                await self.stats.update_one(
                    {"_id": f"{guild_id}:{user_id}"},
                    {
                        "$inc": {
                            "messages.all": amount,
                            f"messages.daily.{day}": amount,
                            f"messages.weekly.{week}": amount,
                        },
                        "$setOnInsert": {"guild_id": guild_id, "user_id": user_id},
                    },
                    upsert=True,
                )
                key = (guild_id, user_id, day, week)
                self._flushing_message_counts[key] -= amount
                if self._flushing_message_counts[key] <= 0:
                    self._flushing_message_counts.pop(key, None)
            except Exception as error:
                for key, pending in items[index:]:
                    self._pending_message_counts[key] += pending
                    self._flushing_message_counts[key] -= pending
                    if self._flushing_message_counts[key] <= 0:
                        self._flushing_message_counts.pop(key, None)
                now = time.monotonic()
                if now - self._last_message_flush_warning >= 60:
                    self._last_message_flush_warning = now
                    log.warning("Leaderboard message counts are queued until the database recovers (%s)", type(error).__name__)
                return

    async def cog_check(self, ctx):
        """Gate every feature command until its server has opted in."""
        command = ctx.command
        root = command.root_parent or command
        if root.name in {"msglb", "voicelb", "invitelb"} and (command is root or command.name in {"on", "off"}):
            return True
        feature = None
        if root.name in {"invitelb", "inviteleaderboard", "invites", "inviter", "invited", "inviteinfo", "addinvites", "removeinvites", "clearinvites", "resetmyinvites", "setallthreshold", "unsetallthreshold", "setinviterole", "unsetinviterole", "viewinviteroles"}:
            feature = "invites"
        elif root.name in {"msglb", "msgstats", "addmessages", "removemessages", "clearmessages", "clearmsgs", "resetmymessages", "blacklistchannel", "unblacklistchannel", "blacklistedchannels", "blacklistcategory", "unblacklistcategory", "blacklistedcategories", "messagechannels", "setmessagerole", "unsetmessagerole", "viewmessageroles"}:
            feature = "messages"
        elif root.name in {"voicelb", "vcstats", "voiceleaderboard", "topvoice", "dailyvoice", "weeklyvoice", "addvctime", "reducevctime", "clearvoice", "resetmyvoice"}:
            feature = "voice"
        elif root.name == "leaderboard":
            subcommand = command.name
            feature = "invites" if subcommand == "invites" else "voice" if "voice" in subcommand else "messages"
        elif root.name == "top":
            feature = {"messages": "messages", "voice": "voice", "invites": "invites"}.get(command.name, "messages")
        if feature and ctx.guild and not await self.enabled(ctx.guild.id, feature):
            raise LeaderboardFeatureDisabled(feature)
        return True

    async def enabled(self, guild_id, feature):
        config = await self.bot.db.get_guild(guild_id)
        return bool(config.get("leaderboard", {}).get(f"{feature}_enabled", False))

    async def require_enabled(self, ctx, feature):
        if await self.enabled(ctx.guild.id, feature):
            if feature == "messages" and not ctx.author.guild_permissions.manage_guild:
                config = await self.bot.db.get_guild(ctx.guild.id)
                role_ids = {int(role_id) for role_id in config.get("leaderboard", {}).get("message_roles", [])}
                if role_ids and not any(role.id in role_ids for role in ctx.author.roles):
                    await ctx.send(view=component_card(panel("Role Required", "You need an allowed message leaderboard role to use this command."), accent_colour=ACCENT))
                    return False
            return True
        await ctx.send(view=component_card(panel("Leaderboard Disabled", f"Enable this feature first with `{settings.prefix}{'invitelb' if feature == 'invites' else 'msglb' if feature == 'messages' else 'voicelb'} on`."), accent_colour=ACCENT))
        return False

    async def _reset_with_loading(self, ctx, title, operation, success_text):
        progress = await ctx.reply(
            view=component_card(panel(
                f"{icon('loading')} {title}", "Resetting leaderboard data…", color=ACCENT
            ), accent_colour=ACCENT),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            result = await operation()
        except Exception:
            await progress.edit(view=component_card(panel(
                "Reset Failed", "The leaderboard data could not be reset. Please try again later.", color=0xED4245
            ), accent_colour=0xED4245))
            raise
        await progress.edit(view=component_card(panel(title, success_text(result), color=ACCENT), accent_colour=ACCENT))
        return progress

    async def increment(self, guild_id, user_id, field, amount=1):
        await self.stats.update_one({"_id": f"{guild_id}:{user_id}"}, {"$inc": {field: amount}, "$setOnInsert": {"guild_id": guild_id, "user_id": user_id}}, upsert=True)

    async def ranked(self, guild_id, mode, period):
        fields = {"messages": "messages.all", "daily_messages": f"messages.daily.{period}", "weekly_messages": f"messages.weekly.{period}", "voice": "voice.all", "daily_voice": f"voice.daily.{period}", "weekly_voice": f"voice.weekly.{period}", "invites": "invites.joins", "invite_regular": "invites.joins", "invite_fake": "invites.fake", "invite_left": "invites.left"}
        field = fields[mode]
        cursor = await self.stats.find({"guild_id": guild_id, field: {"$gt": 0}})
        rows = [doc async for doc in cursor]
        guild = self.bot.get_guild(guild_id)
        bot_ids = {member.id for member in guild.members if member.bot} if guild else set()
        for row in rows:
            if int(row.get("user_id", 0) or 0) in bot_ids and not row.get("is_bot"):
                await self.stats.update_one({"_id": row["_id"]}, {"$set": {"is_bot": True}})
        rows = [row for row in rows if not row.get("is_bot") and int(row.get("user_id", 0) or 0) not in bot_ids]

        # Include queued/in-flight messages so boards reflect new messages immediately.
        if "messages" in mode:
            indexed = {int(row["user_id"]): row for row in rows}
            for pending_source in (self._pending_message_counts, self._flushing_message_counts):
                for (pending_guild, user_id, pending_day, pending_week), amount in list(pending_source.items()):
                    if pending_guild != guild_id or mode not in {"messages", "daily_messages", "weekly_messages"}:
                        continue
                    if mode == "daily_messages" and pending_day != period:
                        continue
                    if mode == "weekly_messages" and pending_week != period:
                        continue
                    row = indexed.get(user_id)
                    if row is None:
                        row = {"guild_id": guild_id, "user_id": user_id, "messages": {"all": 0, "daily": {}, "weekly": {}}}
                        rows.append(row)
                        indexed[user_id] = row
                    target = row
                    parts = field.split(".")
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = int(target.get(parts[-1], 0) or 0) + amount

        def metric(document):
            value = document
            for part in field.split("."):
                value = value.get(part, 0) if isinstance(value, dict) else 0
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        rows.sort(key=metric, reverse=True)
        rows = rows[:100]
        if "voice" in mode:
            guild_config = await self.bot.db.get_guild(guild_id)
            sessions = guild_config.get("voice", {}).get("session", {})
            now = int(discord.utils.utcnow().timestamp())
            now_dt = datetime.fromtimestamp(now, timezone.utc)
            indexed = {int(doc["user_id"]): doc for doc in rows}
            day, week = periods(now_dt)
            daily_start = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc).timestamp())
            week_date = now_dt.date() - timedelta(days=now_dt.weekday())
            weekly_start = int(datetime(week_date.year, week_date.month, week_date.day, tzinfo=timezone.utc).timestamp())
            for user_id, started in sessions.items():
                try:
                    if int(user_id) in bot_ids:
                        continue
                    start_time = int(float(started))
                    interval_start = max(start_time, daily_start) if mode == "daily_voice" else max(start_time, weekly_start) if mode == "weekly_voice" else start_time
                    live_seconds = max(0, now - interval_start)
                    if not live_seconds:
                        continue
                    doc = indexed.get(int(user_id))
                    if doc is None:
                        doc = {"guild_id": guild_id, "user_id": int(user_id), "voice": {"all": 0, "daily": {}, "weekly": {}}}
                        rows.append(doc)
                    voice = doc.setdefault("voice", {})
                    if mode == "voice":
                        voice["all"] = int(voice.get("all", 0)) + live_seconds
                    elif mode == "daily_voice":
                        daily = voice.setdefault("daily", {})
                        daily[day] = int(daily.get(day, 0)) + live_seconds
                    elif mode == "weekly_voice":
                        weekly = voice.setdefault("weekly", {})
                        weekly[week] = int(weekly.get(week, 0)) + live_seconds
                except (TypeError, ValueError):
                    continue
            rows.sort(key=lambda doc: doc.get("voice", {}).get("daily", {}).get(period, 0) if mode == "daily_voice" else doc.get("voice", {}).get("weekly", {}).get(period, 0) if mode == "weekly_voice" else doc.get("voice", {}).get("all", 0), reverse=True)
        if mode == "invite_regular":
            for doc in rows:
                inv = doc.setdefault("invites", {})
                inv["regular"] = max(0, int(inv.get("joins", 0)) - int(inv.get("fake", 0)) - int(inv.get("left", 0)))
            rows.sort(key=lambda doc: doc.get("invites", {}).get("regular", 0), reverse=True)
        return rows

    def board_embed(self, ctx, mode, period, records, page):
        title = {"messages": "Message Leaderboard", "daily_messages": "Daily Message Leaderboard", "weekly_messages": "Weekly Message Leaderboard", "voice": "Voice Leaderboard", "daily_voice": "Daily Voice Leaderboard", "weekly_voice": "Weekly Voice Leaderboard", "invites": "Invite Leaderboard", "invite_regular": "Regular Invite Leaderboard", "invite_fake": "Fake Invite Leaderboard", "invite_left": "Left Invite Leaderboard"}[mode]
        field = {"messages": "messages.all", "daily_messages": f"messages.daily.{period}", "weekly_messages": f"messages.weekly.{period}", "voice": "voice.all", "daily_voice": f"voice.daily.{period}", "weekly_voice": f"voice.weekly.{period}", "invites": "invites.joins", "invite_regular": "invites.regular", "invite_fake": "invites.fake", "invite_left": "invites.left"}[mode]
        text = ["The messages are being updated in real-time!" if "messages" in mode else "Voice stats are being updated in real-time!" if "voice" in mode else "Invite stats are being updated in real-time!", ""]
        start = page * 10
        for index, row in enumerate(records[start:start + 10], start + 1):
            member = ctx.guild.get_member(int(row["user_id"]))
            name = discord.utils.escape_markdown(member.display_name if member else f"User {row['user_id']}")
            value = row
            for key in field.split("."):
                value = value.get(key, 0) if isinstance(value, dict) else 0
            rank_icon = {1: "1st", 2: "2nd", 3: "3rd"}.get(index)
            rank = f"{icon(rank_icon)} **|**" if rank_icon else f"`{index}` {icon('vclbdot')} **|**"
            if "voice" in mode:
                rendered = f"{pretty_duration(value)} in the vc"
            else:
                suffix = " messages" if "messages" in mode else " invites" if "invite" in mode else ""
                rendered = f"{int(value):,}{suffix}"
            text.append(f"{rank} **{name}** • **{rendered}**")
        if len(text) == 2:
            text.append("No statistics recorded yet.")
        text.extend(("", f"-# Requested by {ctx.author.display_name} • Page {page + 1}/{max(1, (len(records) + 9) // 10)}"))
        return panel(title, "\n".join(text), color=ACCENT)

    async def send_board(self, ctx, mode, feature):
        if not await self.require_enabled(ctx, feature):
            return
        day, week = periods()
        period = day if mode.startswith("daily") else week if mode.startswith("weekly") else "all"
        rows = await self.ranked(ctx.guild.id, mode, period)
        await ctx.send(view=component_card(self.board_embed(ctx, mode, period, rows, 0), BoardView(self, ctx, mode, period), accent_colour=ACCENT), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        # Message counters are best effort during a database outage. Do not
        # hammer an unavailable pool once per incoming chat message.
        if self.bot.db.temporarily_unavailable:
            return
        config = await self.bot.db.get_guild(message.guild.id)
        leaderboard = config.get("leaderboard", {})
        if not leaderboard.get("messages_enabled", False):
            return
        blacklist = leaderboard.get("message_blacklist", {})
        if not isinstance(blacklist, dict):
            blacklist = {}
        channel = message.channel
        parent = getattr(channel, "parent", None)
        channel_ids = {str(channel.id)}
        parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
        if parent_id:
            channel_ids.add(str(parent_id))
        category_id = getattr(channel, "category_id", None) or getattr(parent, "category_id", None)
        if category_id is None:
            category_id = getattr(getattr(channel, "category", None), "id", None) or getattr(getattr(parent, "category", None), "id", None)
        blocked_channels = {str(item) for item in blacklist.get("channels", [])}
        blocked_categories = {str(item) for item in blacklist.get("categories", [])}
        if channel_ids & blocked_channels or (category_id is not None and str(category_id) in blocked_categories):
            return
        # A selected text/forum channel also includes its child threads.
        selected_channels = {str(item) for item in leaderboard.get("message_channels", [])}
        if selected_channels and not channel_ids & selected_channels:
            return
        content = " ".join((message.content or "").casefold().split())
        attachments = tuple(sorted((item.filename, item.size) for item in message.attachments))
        if content or attachments:
            fingerprint = (content, attachments)
            cache_key = (message.guild.id, message.author.id)
            now = time.monotonic()
            previous = self._recent_message_fingerprints.get(cache_key)
            if previous and previous[0] == fingerprint and now - previous[1] < 5:
                # Refresh the window so a continuous repeat flood stays uncounted.
                self._recent_message_fingerprints[cache_key] = (fingerprint, now)
                return
            self._recent_message_fingerprints[cache_key] = (fingerprint, now)
            if len(self._recent_message_fingerprints) > 20_000:
                cutoff = now - 60
                self._recent_message_fingerprints = {
                    key: value for key, value in self._recent_message_fingerprints.items()
                    if value[1] >= cutoff
                }
        day, week = periods()
        uid = message.author.id
        self._pending_message_counts[(message.guild.id, uid, day, week)] += 1

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            if await self.enabled(guild.id, "invites"):
                await self.refresh_invites(guild)

    async def refresh_invites(self, guild):
        try:
            self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in await guild.invites()}
        except (discord.Forbidden, discord.HTTPException):
            log.info("Invite tracking needs Manage Server or View Audit Log in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        await self.refresh_invites(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        await self.refresh_invites(invite.guild)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not await self.enabled(member.guild.id, "invites"):
            return
        try:
            invites = await member.guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return
        current = {invite.code: invite.uses or 0 for invite in invites}
        previous = self.invite_cache.get(member.guild.id, {})
        used = next((invite for invite in invites if current.get(invite.code, 0) > previous.get(invite.code, 0)), None)
        self.invite_cache[member.guild.id] = current
        if used and used.inviter:
            await self.increment(member.guild.id, used.inviter.id, "invites.joins")
            if (discord.utils.utcnow() - member.created_at).days < 7:
                await self.increment(member.guild.id, used.inviter.id, "invites.fake")
            previous_member = await self.stats.find_one({"_id": f"{member.guild.id}:{member.id}"})
            if previous_member:
                await self.increment(member.guild.id, used.inviter.id, "invites.rejoins")
            await self.stats.update_one({"_id": f"{member.guild.id}:{member.id}"}, {"$set": {"guild_id": member.guild.id, "user_id": member.id, "invites.inviter_id": used.inviter.id, "invites.present": True}}, upsert=True)
            inviter_row = await self.stats.find_one({"_id": f"{member.guild.id}:{used.inviter.id}"}) or {}
            invite_counts = inviter_row.get("invites", {})
            qualified = max(0, int(invite_counts.get("joins", 0)) - int(invite_counts.get("fake", 0)) - int(invite_counts.get("left", 0)) + int(invite_counts.get("bonus", 0)))
            guild_config = await self.bot.db.get_guild(member.guild.id)
            role_map = guild_config.get("leaderboard", {}).get("invite_roles", {})
            bot_member = member.guild.me
            for threshold, role_id in role_map.items():
                try:
                    if qualified < int(threshold):
                        continue
                    role = member.guild.get_role(int(role_id))
                    if role and bot_member and role < bot_member.top_role and role not in used.inviter.roles:
                        await used.inviter.add_roles(role, reason="Invite leaderboard reward")
                except (ValueError, discord.Forbidden, discord.HTTPException):
                    log.info("Could not assign invite reward role %s in guild %s", role_id, member.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if not await self.enabled(member.guild.id, "invites"):
            return
        row = await self.stats.find_one({"_id": f"{member.guild.id}:{member.id}"})
        if row and row.get("invites", {}).get("present") and row["invites"].get("inviter_id"):
            await self.increment(member.guild.id, row["invites"]["inviter_id"], "invites.left")
            await self.stats.update_one({"_id": f"{member.guild.id}:{member.id}"}, {"$set": {"invites.present": False}})

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not await self.enabled(member.guild.id, "voice"):
            return

        def is_counted(state):
            return state.channel is not None and not any((
                state.mute, state.deaf, state.self_mute, state.self_deaf,
            ))

        before_counted = is_counted(before)
        after_counted = is_counted(after)
        channel_changed = before.channel != after.channel
        if before_counted == after_counted and not (before_counted and channel_changed):
            return

        now = discord.utils.utcnow().timestamp()
        key = f"voice.session.{member.id}"
        config = await self.bot.db.get_guild(member.guild.id)
        started = (config.get("voice", {}).get("session", {}) or {}).get(str(member.id))

        # Close a counted interval when the member leaves, changes channel,
        # or starts muting/deafening; muted/deafened time is never accrued.
        if before_counted and (not after_counted or channel_changed):
            if started is not None:
                increments = voice_time_increments(started, now)
                if increments.get("voice.all", 0):
                    await self.stats.update_one(
                        {"_id": f"{member.guild.id}:{member.id}"},
                        {"$inc": increments,
                         "$setOnInsert": {"guild_id": member.guild.id, "user_id": member.id}},
                        upsert=True,
                    )
            await self.bot.db.unset_value(member.guild.id, key)

        # Start a fresh interval only while connected and neither muted nor deafened.
        if after_counted and (not before_counted or channel_changed):
            await self.bot.db.set_value(member.guild.id, key, now)

    @commands.group(name="leaderboard", aliases=("lb",), invoke_without_command=True, case_insensitive=True)
    async def leaderboard(self, ctx):
        await ctx.send(view=component_card(panel("Leaderboard", f"Use `{settings.prefix}help leaderboard` to see commands."), accent_colour=ACCENT))

    @leaderboard.command(name="messages", aliases=("msg",))
    async def messages_board(self, ctx):
        await self.send_board(ctx, "messages", "messages")

    @commands.command(name="messageboard", aliases=("msgboard",), case_insensitive=True)
    async def messageboard(self, ctx):
        await self.send_board(ctx, "messages", "messages")

    @commands.command(name="dailymessages", aliases=("dm",), case_insensitive=True)
    async def dailymessages(self, ctx):
        await self.send_board(ctx, "daily_messages", "messages")

    @commands.command(name="weeklymessages", aliases=("wm",), case_insensitive=True)
    async def weeklymessages(self, ctx):
        await self.send_board(ctx, "weekly_messages", "messages")

    @leaderboard.command(name="dailymessages", aliases=("dm",))
    async def daily_messages_board(self, ctx):
        await self.send_board(ctx, "daily_messages", "messages")

    @leaderboard.command(name="weeklymessages", aliases=("wm",))
    async def weekly_messages_board(self, ctx):
        await self.send_board(ctx, "weekly_messages", "messages")

    @leaderboard.command(name="voice", aliases=("vc",))
    async def voice_board(self, ctx):
        await self.send_board(ctx, "voice", "voice")

    @commands.command(name="voiceleaderboard", aliases=["topvoice", "vcboard"])
    async def voiceleaderboard(self, ctx):
        await self.send_board(ctx, "voice", "voice")

    @commands.command(name="dailyvoice", aliases=("dv",))
    async def dailyvoice(self, ctx):
        await self.send_board(ctx, "daily_voice", "voice")

    @commands.command(name="weeklyvoice", aliases=("wv",))
    async def weeklyvoice(self, ctx):
        await self.send_board(ctx, "weekly_voice", "voice")

    @leaderboard.command(name="dailyvoice", aliases=("dv",))
    async def daily_voice_board(self, ctx):
        await self.send_board(ctx, "daily_voice", "voice")

    @leaderboard.command(name="weeklyvoice", aliases=("wv",))
    async def weekly_voice_board(self, ctx):
        await self.send_board(ctx, "weekly_voice", "voice")

    @leaderboard.command(name="invites", aliases=("i",))
    async def invite_board(self, ctx):
        await self.send_board(ctx, "invites", "invites")

    @commands.group(name="top", invoke_without_command=True, case_insensitive=True)
    async def top(self, ctx):
        await ctx.send(view=component_card(panel("Top Leaderboards", f"Use `{settings.prefix}help leaderboard` to see the board commands."), accent_colour=ACCENT))

    @top.command(name="messages", aliases=("msg",))
    async def top_messages(self, ctx):
        await self.send_board(ctx, "messages", "messages")

    @top.command(name="voice", aliases=("vc",))
    async def top_voice(self, ctx):
        await self.send_board(ctx, "voice", "voice")

    @top.command(name="invites", aliases=("i",))
    async def top_invites(self, ctx):
        await self.send_board(ctx, "invites", "invites")

    @commands.group(name="invitelb", aliases=("il",), invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def invitelb(self, ctx):
        await ctx.send(view=component_card(panel("Invite Leaderboard", f"Use `{settings.prefix}invitelb on` or `{settings.prefix}invitelb off`."), accent_colour=ACCENT))

    @invitelb.command(name="on", aliases=("enable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def invitelb_on(self, ctx):
        if await self.enabled(ctx.guild.id, "invites"):
            await ctx.send(view=component_card(panel("Already Enabled", "Invite leaderboard is already enabled."), accent_colour=ACCENT)); return
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "invites_enabled", True)
        await self.refresh_invites(ctx.guild)
        await ctx.send(view=component_card(panel("Invite Leaderboard Enabled", "Invite tracking is on. Invite commands are now available."), accent_colour=ACCENT))

    @invitelb.command(name="off", aliases=("disable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def invitelb_off(self, ctx):
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "invites_enabled", False)
        self.invite_cache.pop(ctx.guild.id, None)
        await ctx.send(view=component_card(panel("Invite Leaderboard Disabled", "Invite tracking and invite commands are disabled."), accent_colour=ACCENT))

    @commands.group(name="msglb", aliases=("mlb",), invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def msglb(self, ctx):
        await ctx.send(view=component_card(panel("Message Leaderboard", f"Use `{settings.prefix}msglb on` or `{settings.prefix}msglb off`."), accent_colour=ACCENT))

    @msglb.command(name="on", aliases=("enable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def msglb_on(self, ctx):
        if await self.enabled(ctx.guild.id, "messages"):
            await ctx.send(view=component_card(panel("Already Enabled", "Message leaderboard is already enabled."), accent_colour=ACCENT)); return
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "messages_enabled", True)
        await ctx.send(view=component_card(panel("Message Leaderboard Enabled", "Message tracking is on. Message leaderboard commands are now available."), accent_colour=ACCENT))

    @msglb.command(name="off", aliases=("disable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def msglb_off(self, ctx):
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "messages_enabled", False)
        await ctx.send(view=component_card(panel("Message Leaderboard Disabled", "Message tracking and message leaderboard commands are disabled."), accent_colour=ACCENT))

    @commands.group(name="voicelb", aliases=("vlb",), invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def voicelb(self, ctx):
        await ctx.send(view=component_card(panel("Voice Leaderboard", f"Use `{settings.prefix}voicelb on` or `{settings.prefix}voicelb off`."), accent_colour=ACCENT))

    @voicelb.command(name="on", aliases=("enable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def voicelb_on(self, ctx):
        if await self.enabled(ctx.guild.id, "voice"):
            await ctx.send(view=component_card(panel("Already Enabled", "Voice leaderboard is already enabled."), accent_colour=ACCENT)); return
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "voice_enabled", True)
        now = discord.utils.utcnow().timestamp()
        for member in ctx.guild.members:
            if member.voice and member.voice.channel:
                await self.bot.db.set_value(ctx.guild.id, f"voice.session.{member.id}", now)
        await ctx.send(view=component_card(panel("Voice Leaderboard Enabled", "Voice tracking is on. Voice leaderboard commands are now available."), accent_colour=ACCENT))

    @voicelb.command(name="off", aliases=("disable",))
    @commands.has_guild_permissions(manage_guild=True)
    async def voicelb_off(self, ctx):
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "voice_enabled", False)
        await self.bot.db.unset_value(ctx.guild.id, "voice.session")
        await ctx.send(view=component_card(panel("Voice Leaderboard Disabled", "Voice tracking and voice leaderboard commands are disabled."), accent_colour=ACCENT))

    @commands.command(name="invites", aliases=("i",), case_insensitive=True)
    async def invites_stats(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "invites"): return
        member = member or ctx.author
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        data = row.get("invites", {})
        body = (f"{member.mention} currently has **{int(data.get('joins', 0)) - int(data.get('left', 0)) + int(data.get('bonus', 0))}** invites.\n\n"
                f"**Total:**\n• **Joins:** **{data.get('joins', 0)}**\n• **Left:** **{data.get('left', 0)}**\n• **Bonus:** **{data.get('bonus', 0)}**\n\n"
                f"**Other:**\n• **Fake:** **{data.get('fake', 0)}**\n• **Rejoins:** **{data.get('rejoins', 0)}**")
        await ctx.send(view=component_card(panel(f"{member.display_name}'s Invites Stats", body, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="inviter", aliases=("ir",), case_insensitive=True)
    async def inviter(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "invites"): return
        member = member or ctx.author
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        inviter_id = row.get("invites", {}).get("inviter_id")
        text = f"{member.mention} was invited by <@{int(inviter_id)}>" if inviter_id else f"No inviter was recorded for {member.mention}."
        await ctx.send(view=component_card(panel("Invite Attribution", text, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="invited", aliases=("id",), case_insensitive=True)
    async def invited(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "invites"): return
        member = member or ctx.author
        cursor = (await self.stats.find({"guild_id": ctx.guild.id, "invites.inviter_id": member.id})).limit(25)
        rows = [item async for item in cursor]
        names = [f"• <@{item['user_id']}>" for item in rows if item.get("invites", {}).get("present")]
        await ctx.send(view=component_card(panel(f"Invited by {member.display_name}", "\n".join(names) or "No currently joined invitees found.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="inviteinfo", aliases=("ii",), case_insensitive=True)
    async def inviteinfo(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "invites"): return
        member = member or ctx.author
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        data = row.get("invites", {})
        body = f"**Joins:** {data.get('joins', 0)}\n**Left:** {data.get('left', 0)}\n**Fake:** {data.get('fake', 0)}\n**Rejoins:** {data.get('rejoins', 0)}\n**Bonus:** {data.get('bonus', 0)}"
        await ctx.send(view=component_card(panel(f"{member.display_name}'s Invite Info", body, color=ACCENT), accent_colour=ACCENT))

    @commands.group(name="inviteleaderboard", aliases=("ilb",), invoke_without_command=True, case_insensitive=True)
    async def inviteleaderboard(self, ctx):
        await self.send_board(ctx, "invites", "invites")

    @inviteleaderboard.command(name="total", aliases=("t",))
    async def invite_total_board(self, ctx):
        await self.send_board(ctx, "invites", "invites")

    @inviteleaderboard.command(name="regular", aliases=("r",))
    async def invite_regular_board(self, ctx):
        await self.send_invite_variant(ctx, "invite_regular")

    @inviteleaderboard.command(name="fake", aliases=("f",))
    async def invite_fake_board(self, ctx):
        await self.send_invite_variant(ctx, "invite_fake")

    @inviteleaderboard.command(name="left", aliases=("l",))
    async def invite_left_board(self, ctx):
        await self.send_invite_variant(ctx, "invite_left")

    async def send_invite_variant(self, ctx, mode):
        if not await self.require_enabled(ctx, "invites"): return
        rows = await self.ranked(ctx.guild.id, mode, "all")
        await ctx.send(view=component_card(self.board_embed(ctx, mode, "all", rows, 0), BoardView(self, ctx, mode, "all"), accent_colour=ACCENT))

    @commands.command(name="addinvites", aliases=("ai",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def addinvites(self, ctx, member: discord.Member, amount: int):
        if not await self.require_enabled(ctx, "invites"): return
        await self.increment(ctx.guild.id, member.id, "invites.bonus", max(0, amount))
        await ctx.send(view=component_card(panel("Invite Bonus Added", f"Added **{max(0, amount)}** bonus invites to {member.mention}.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="removeinvites", aliases=("rvi",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def removeinvites(self, ctx, member: discord.Member, amount: int):
        if not await self.require_enabled(ctx, "invites"): return
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        bonus = max(0, int(row.get("invites", {}).get("bonus", 0)) - max(0, amount))
        await self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$set": {"guild_id": ctx.guild.id, "user_id": member.id, "invites.bonus": bonus}}, upsert=True)
        await ctx.send(view=component_card(panel("Invite Bonus Removed", f"Removed **{max(0, amount)}** bonus invites from {member.mention}.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="clearinvites", aliases=("cvi",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def clearinvites(self, ctx, target: str = "all"):
        if not await self.require_enabled(ctx, "invites"):
            return
        if target.casefold() == "all":
            if not ctx.author.guild_permissions.administrator:
                raise commands.MissingPermissions(["administrator"])
            await self._reset_with_loading(
                ctx, "Invite Stats Cleared",
                lambda: self.stats.update_many({"guild_id": ctx.guild.id}, {"$unset": {"invites": ""}}),
                lambda result: f"Cleared invite stats for **{result.matched_count}** member(s) in this server.",
            )
        else:
            try:
                member = await commands.MemberConverter().convert(ctx, target)
            except commands.MemberNotFound:
                await ctx.send(view=component_card(panel(
                    "Member Not Found", "Use `.clearinvites @user` for one member or `.clearinvites all` for everyone.", color=ACCENT
                ), accent_colour=ACCENT))
                return
            await self._reset_with_loading(
                ctx, "Invite Stats Cleared",
                lambda: self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$unset": {"invites": ""}}),
                lambda _result: f"Cleared invite stats for {member.mention}.",
            )

    @commands.command(name="resetmyinvites", aliases=("rmi",), case_insensitive=True)
    async def resetmyinvites(self, ctx):
        if not await self.require_enabled(ctx, "invites"): return
        await self._reset_with_loading(
            ctx, "Invite Stats Reset",
            lambda: self.stats.update_one({"_id": f"{ctx.guild.id}:{ctx.author.id}"}, {"$unset": {"invites": ""}}),
            lambda _result: "Your invite stats have been reset.",
        )

    @commands.command(name="setallthreshold", aliases=("sat",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def setallthreshold(self, ctx, amount: int):
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard", "invite_threshold", max(0, amount))
        await ctx.send(view=component_card(panel("Invite Threshold Updated", f"Set the invite threshold to **{max(0, amount)}**.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="unsetallthreshold", aliases=("uat",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def unsetallthreshold(self, ctx):
        await self.bot.db.unset_value(ctx.guild.id, "leaderboard.invite_threshold")
        await ctx.send(view=component_card(panel("Invite Threshold Removed", "Removed the invite threshold." , color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="setinviterole", aliases=("sir",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def setinviterole(self, ctx, amount: int, role: discord.Role):
        await self.bot.db.set_nested(ctx.guild.id, "leaderboard.invite_roles", str(max(0, amount)), str(role.id))
        await ctx.send(view=component_card(panel("Invite Role Added", f"Members with **{max(0, amount)}** invites receive {role.mention} when assigned manually.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="unsetinviterole", aliases=("uir",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def unsetinviterole(self, ctx, amount: int):
        await self.bot.db.unset_value(ctx.guild.id, f"leaderboard.invite_roles.{max(0, amount)}")
        await ctx.send(view=component_card(panel("Invite Role Removed", f"Removed the role mapping for **{max(0, amount)}** invites.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="viewinviteroles", aliases=("vir",), case_insensitive=True)
    async def viewinviteroles(self, ctx):
        config = await self.bot.db.get_guild(ctx.guild.id)
        mappings = config.get("leaderboard", {}).get("invite_roles", {})
        body = "\n".join(f"• {threshold} invites → <@&{role_id}>" for threshold, role_id in mappings.items()) or "No invite role mappings are configured."
        await ctx.send(view=component_card(panel("Invite Role Mappings", body, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="vcstats", aliases=("vcs", "vs"), case_insensitive=True)
    async def vcstats(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "voice"): return
        member = member or ctx.author
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        voice = row.get("voice", {})
        day, week = periods()
        description = (
            f"**{member.display_name}** spent a total of **{pretty_duration(voice.get('all', 0))}** in the voice channels of the server!"
            f"\n\n**Daily:**\n{icon('leaderboard_arrow')} ・ **{pretty_duration(voice.get('daily', {}).get(day, 0))}**"
            f"\n**Weekly:**\n{icon('leaderboard_arrow')} ・ **{pretty_duration(voice.get('weekly', {}).get(week, 0))}**"
        )
        await ctx.send(view=component_card(panel(f"{member.display_name}'s Voice Stats", description, color=ACCENT), accent_colour=ACCENT))

    def parse_duration(self, text):
        import re
        matches = re.findall(r"(\d+)([dhms])", text.lower())
        if not matches or "".join(f"{n}{u}" for n, u in matches) != text.lower():
            return None
        scales = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        return sum(int(number) * scales[unit] for number, unit in matches)

    @commands.command(name="addvctime", aliases=("avt",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def addvctime(self, ctx, member: discord.Member, duration: str):
        if not await self.require_enabled(ctx, "voice"): return
        seconds = self.parse_duration(duration)
        if seconds is None:
            await ctx.send(view=component_card(panel("Invalid Duration", "Use a duration such as `1d2h30m` or `45m`."), accent_colour=ACCENT)); return
        day, week = periods()
        await self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$inc": {"voice.all": seconds, f"voice.daily.{day}": seconds, f"voice.weekly.{week}": seconds}, "$setOnInsert": {"guild_id": ctx.guild.id, "user_id": member.id}}, upsert=True)
        await ctx.send(view=component_card(panel("Voice Time Added", f"Added **{pretty_duration(seconds)}** to {member.mention}'s voice stats.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="reducevctime", aliases=("rvt",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def reducevctime(self, ctx, member: discord.Member, duration: str):
        if not await self.require_enabled(ctx, "voice"): return
        seconds = self.parse_duration(duration)
        if seconds is None:
            await ctx.send(view=component_card(panel("Invalid Duration", "Use a duration such as `1d2h30m` or `45m`."), accent_colour=ACCENT)); return
        day, week = periods()
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        voice = row.get("voice", {})
        values = {"voice.all": max(0, int(voice.get("all", 0)) - seconds), f"voice.daily.{day}": max(0, int(voice.get("daily", {}).get(day, 0)) - seconds), f"voice.weekly.{week}": max(0, int(voice.get("weekly", {}).get(week, 0)) - seconds)}
        await self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$set": values}, upsert=True)
        await ctx.send(view=component_card(panel("Voice Time Reduced", f"Reduced **{pretty_duration(seconds)}** from {member.mention}'s voice stats.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="clearvoice", aliases=("clv",), case_insensitive=True)
    @commands.has_guild_permissions(administrator=True)
    async def clearvoice(self, ctx):
        await self._reset_with_loading(
            ctx, "Voice Stats Cleared",
            lambda: self.stats.update_many({"guild_id": ctx.guild.id}, {"$unset": {"voice": ""}}),
            lambda _result: "Cleared voice stats for everyone in this server.",
        )

    @commands.command(name="resetmyvoice", aliases=("rmv",), case_insensitive=True)
    async def resetmyvoice(self, ctx):
        if not await self.require_enabled(ctx, "voice"): return
        await self._reset_with_loading(
            ctx, "Voice Stats Reset",
            lambda: self.stats.update_one({"_id": f"{ctx.guild.id}:{ctx.author.id}"}, {"$unset": {"voice": ""}}),
            lambda _result: "Your voice stats have been reset.",
        )

    @commands.command(name="msgstats", aliases=("ms",), case_insensitive=True, help="Show your message stats, or mention a member to see theirs.")
    async def msgstats(self, ctx, member: discord.Member = None):
        if not await self.require_enabled(ctx, "messages"):
            return
        member = member or ctx.author
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        msg = row.get("messages", {})
        day, week = periods()
        pending_all = pending_day = pending_week = 0
        for pending_source in (self._pending_message_counts, self._flushing_message_counts):
            for (guild_id, user_id, pending_date, pending_week_key), amount in list(pending_source.items()):
                if guild_id == ctx.guild.id and user_id == member.id:
                    pending_all += amount
                    if pending_date == day:
                        pending_day += amount
                    if pending_week_key == week:
                        pending_week += amount
        all_messages = int(msg.get("all", 0) or 0) + pending_all
        weekly_messages = int(msg.get("weekly", {}).get(week, 0) or 0) + pending_week
        daily_messages = int(msg.get("daily", {}).get(day, 0) or 0) + pending_day
        description = (
            f"All time • **{all_messages:,}** messages in this server\n"
            f"Weekly • **{weekly_messages:,}** messages in this server\n"
            f"Today • **{daily_messages:,}** messages in this server\n\n"
            "Message stats update in real-time."
        )
        await ctx.send(view=component_card(panel(f"{member.display_name}'s Messages", description, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="addmessages", aliases=("amsg",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def addmessages(self, ctx, member: discord.Member, amount: int):
        if not await self.require_enabled(ctx, "messages"): return
        if amount < 1:
            await ctx.send(view=component_card(panel("Invalid Amount", "Amount must be greater than zero."), accent_colour=ACCENT)); return
        day, week = periods()
        await self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$inc": {"messages.all": amount, f"messages.daily.{day}": amount, f"messages.weekly.{week}": amount}, "$setOnInsert": {"guild_id": ctx.guild.id, "user_id": member.id}}, upsert=True)
        await ctx.send(view=component_card(panel("Messages Added", f"Added **{amount:,}** messages to {member.mention}.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="removemessages", aliases=("rmsg",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def removemessages(self, ctx, member: discord.Member, amount: int):
        if not await self.require_enabled(ctx, "messages"): return
        if amount < 1:
            await ctx.send(view=component_card(panel("Invalid Amount", "Amount must be greater than zero."), accent_colour=ACCENT)); return
        day, week = periods()
        row = await self.stats.find_one({"_id": f"{ctx.guild.id}:{member.id}"}) or {}
        msg = row.get("messages", {})
        values = {"messages.all": max(0, int(msg.get("all", 0)) - amount), f"messages.daily.{day}": max(0, int(msg.get("daily", {}).get(day, 0)) - amount), f"messages.weekly.{week}": max(0, int(msg.get("weekly", {}).get(week, 0)) - amount)}
        await self.stats.update_one({"_id": f"{ctx.guild.id}:{member.id}"}, {"$set": values}, upsert=True)
        await ctx.send(view=component_card(panel("Messages Removed", f"Removed **{amount:,}** messages from {member.mention}.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="clearmessages", aliases=["clearmsgs", "clmsg"], case_insensitive=True)
    @commands.has_guild_permissions(administrator=True)
    async def clearmessages(self, ctx):
        await self._reset_with_loading(
            ctx, "Message Stats Cleared",
            lambda: self.stats.update_many({"guild_id": ctx.guild.id}, {"$unset": {"messages": ""}}),
            lambda _result: "Cleared message stats for everyone in this server.",
        )

    @commands.command(name="resetmymessages", aliases=("rmmsg",), case_insensitive=True)
    async def resetmymessages(self, ctx):
        if not await self.require_enabled(ctx, "messages"): return
        await self._reset_with_loading(
            ctx, "Message Stats Reset",
            lambda: self.stats.update_one({"_id": f"{ctx.guild.id}:{ctx.author.id}"}, {"$unset": {"messages": ""}}),
            lambda _result: "Your message stats have been reset.",
        )

    async def change_message_filter(self, ctx, field, channel, add):
        path = f"leaderboard.message_blacklist.{field}"
        if add:
            await self.bot.db.add_to_set(ctx.guild.id, path, str(channel.id))
            text = f"Added {channel.mention} to the message tracking exclusion list."
        else:
            await self.bot.db.pull_value(ctx.guild.id, path, str(channel.id))
            text = f"Removed {channel.mention} from the message tracking exclusion list."
        await ctx.send(view=component_card(panel("Message Tracking Updated", text, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="blacklistchannel", aliases=("blc",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def blacklistchannel(self, ctx, channel: discord.TextChannel):
        await self.change_message_filter(ctx, "channels", channel, True)

    @commands.command(name="unblacklistchannel", aliases=("ublc",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def unblacklistchannel(self, ctx, channel: discord.TextChannel):
        await self.change_message_filter(ctx, "channels", channel, False)

    @commands.command(name="blacklistedchannels", aliases=("blcs",), case_insensitive=True)
    async def blacklistedchannels(self, ctx):
        config = await self.bot.db.get_guild(ctx.guild.id)
        ids = config.get("leaderboard", {}).get("message_blacklist", {}).get("channels", [])
        value = "\n".join(f"• <#{int(cid)}>" for cid in ids) if ids else "No channels are excluded."
        await ctx.send(view=component_card(panel("Excluded Channels", value, color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="blacklistcategory", aliases=("blcat",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def blacklistcategory(self, ctx, category: discord.CategoryChannel):
        await self.change_message_filter(ctx, "categories", category, True)

    @commands.command(name="unblacklistcategory", aliases=("ublcat",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def unblacklistcategory(self, ctx, category: discord.CategoryChannel):
        await self.change_message_filter(ctx, "categories", category, False)

    @commands.command(name="blacklistedcategories", aliases=("blcats",), case_insensitive=True)
    async def blacklistedcategories(self, ctx):
        config = await self.bot.db.get_guild(ctx.guild.id)
        ids = config.get("leaderboard", {}).get("message_blacklist", {}).get("categories", [])
        value = "\n".join(f"• <#{int(cid)}>" for cid in ids) if ids else "No categories are excluded."
        await ctx.send(view=component_card(panel("Excluded Categories", value, color=ACCENT), accent_colour=ACCENT))

    @commands.group(name="messagechannels", aliases=("msgchannels",), invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def messagechannels(self, ctx):
        """Choose which channels contribute to message leaderboard counts."""
        await ctx.send(view=component_card(panel(
            "Message Count Channels",
            f"Add channels one at a time with `{ctx.prefix}messagechannels add #channel` (up to 10 selected total). "
            f"Use `{ctx.prefix}messagechannels show` to review them, `{ctx.prefix}messagechannels remove #channel` to remove one, or `{ctx.prefix}messagechannels reset` to count all non-blacklisted channels.",
            color=ACCENT,
        ), accent_colour=ACCENT))

    @messagechannels.command(name="add", aliases=("include",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def messagechannels_add(self, ctx, channel: discord.TextChannel):
        """Add one text channel at a time, up to 10 selected channels total."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        existing = {str(channel_id) for channel_id in config.get("leaderboard", {}).get("message_channels", [])}
        if str(channel.id) in existing:
            await ctx.send(view=component_card(panel("Channel Already Selected", f"Messages in {channel.mention} are already included.", color=ACCENT), accent_colour=ACCENT))
            return
        if len(existing) >= 10:
            await ctx.send(view=component_card(panel("Channel Limit Reached", f"You can select up to 10 message-count channels. Remove one with `{ctx.prefix}messagechannels remove #channel` before adding another.", color=ACCENT), accent_colour=ACCENT))
            return
        await self.bot.db.add_to_set(ctx.guild.id, "leaderboard.message_channels", str(channel.id))
        result = f"Messages in {channel.mention} will count. Selected channels (and their threads) count; exclusions still apply. **{len(existing) + 1}/10 channels selected.**"
        await ctx.send(view=component_card(panel(
            "Message Count Channel Added",
            result,
            color=ACCENT,
        ), accent_colour=ACCENT))

    @messagechannels.command(name="remove", aliases=("exclude",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def messagechannels_remove(self, ctx, channel: discord.TextChannel):
        """Remove a channel from the message leaderboard selection."""
        await self.bot.db.pull_value(ctx.guild.id, "leaderboard.message_channels", str(channel.id))
        config = await self.bot.db.get_guild(ctx.guild.id)
        remaining = config.get("leaderboard", {}).get("message_channels", [])
        explanation = "Only the remaining selected channels will count." if remaining else "No channels are selected; all non-blacklisted channels will count."
        await ctx.send(view=component_card(panel(
            "Message Count Channel Removed",
            f"Removed {channel.mention}. {explanation}",
            color=ACCENT,
        ), accent_colour=ACCENT))

    @messagechannels.command(name="show", aliases=("list",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def messagechannels_show(self, ctx):
        """Show channels selected for message leaderboard tracking."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        ids = config.get("leaderboard", {}).get("message_channels", [])
        value = "\n".join(f"• <#{int(channel_id)}>" for channel_id in ids) if ids else "All non-blacklisted channels are currently counted."
        await ctx.send(view=component_card(panel("Message Count Channels", value, color=ACCENT), accent_colour=ACCENT))

    @messagechannels.command(name="reset", aliases=("clear",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def messagechannels_reset(self, ctx):
        """Clear the selected message-count channels and count all non-blacklisted channels."""
        await self._reset_with_loading(
            ctx, "Message Count Channels Reset",
            lambda: self.bot.db.unset_value(ctx.guild.id, "leaderboard.message_channels"),
            lambda _result: "All non-blacklisted channels will count again.",
        )

    @commands.command(name="setmessagerole", aliases=("smr",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def setmessagerole(self, ctx, role: discord.Role):
        await self.bot.db.add_to_set(ctx.guild.id, "leaderboard.message_roles", str(role.id))
        await ctx.send(view=component_card(panel("Message Role Added", f"{role.mention} can now use message leaderboard commands.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="unsetmessagerole", aliases=("umr",), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def unsetmessagerole(self, ctx, role: discord.Role):
        await self.bot.db.pull_value(ctx.guild.id, "leaderboard.message_roles", str(role.id))
        await ctx.send(view=component_card(panel("Message Role Removed", f"Removed {role.mention} from message leaderboard access.", color=ACCENT), accent_colour=ACCENT))

    @commands.command(name="viewmessageroles", aliases=("vmr",), case_insensitive=True)
    async def viewmessageroles(self, ctx):
        config = await self.bot.db.get_guild(ctx.guild.id)
        ids = config.get("leaderboard", {}).get("message_roles", [])
        value = "\n".join(f"• <@&{int(rid)}>" for rid in ids) if ids else "No role restriction is configured."
        await ctx.send(view=component_card(panel("Message Leaderboard Roles", value, color=ACCENT), accent_colour=ACCENT))


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
