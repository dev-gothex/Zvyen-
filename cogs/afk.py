"""Global and server-scoped AFK status with mention tracking."""
import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

import discord
from discord.ext import commands

from utils.emojis import icon
from utils.ui import component_card, panel

AFK_COLOR = 0xF2B6CE
AFK_MAX_MENTIONS_PER_MESSAGE = 10
AFK_MAX_DM_NOTIFICATIONS_PER_MESSAGE = 10
AFK_RATE_WINDOW = 10.0
AFK_MENTION_RATE_LIMIT = 20
AFK_NOTIFICATION_RATE_LIMIT = 6
AFK_NOTIFICATION_CONCURRENCY = 4
log = logging.getLogger("zyven.afk")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def afk_embed(title: str, description: str, member: discord.abc.User | None = None) -> discord.Embed:
    embed = panel(title, description, color=AFK_COLOR)
    if member:
        embed.set_thumbnail(url=member.display_avatar.url)
    return embed


class AFKView(discord.ui.View):
    def __init__(self, cog: "AFK", requester_id: int, reason: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.reason = reason
        self.add_item(self._mode_button("Global AFK", "global", "afk_global_button", discord.ButtonStyle.danger))
        self.add_item(self._mode_button("Server AFK", "server", "afk_server_button", discord.ButtonStyle.success))

    def _mode_button(self, label, scope, emoji_key, style):
        button = discord.ui.Button(
            label=label,
            style=style,
            emoji=discord.PartialEmoji.from_str(icon(emoji_key)),
        )

        async def callback(interaction: discord.Interaction):
            if not await self.interaction_check(interaction):
                return
            await self.cog.ask_dm_preference(interaction, self.requester_id, self.reason, scope)

        button.callback = callback
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who started this AFK setup can choose.", ephemeral=True)
        return False


class AFKDMView(discord.ui.View):
    def __init__(self, cog: "AFK", requester_id: int, reason: str, scope: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.requester_id = requester_id
        self.reason = reason
        self.scope = scope
        yes = discord.ui.Button(label="Yes", style=discord.ButtonStyle.success)
        no = discord.ui.Button(label="No", style=discord.ButtonStyle.danger)
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who started this AFK setup can choose.", ephemeral=True)
        return False

    async def _yes(self, interaction: discord.Interaction):
        if await self.interaction_check(interaction):
            await self.cog.activate(interaction, self.requester_id, self.reason, self.scope, True)

    async def _no(self, interaction: discord.Interaction):
        if await self.interaction_check(interaction):
            await self.cog.activate(interaction, self.requester_id, self.reason, self.scope, False)


class AFK(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._db_warning_after = 0.0
        self._notification_semaphore = asyncio.Semaphore(AFK_NOTIFICATION_CONCURRENCY)
        self._rate_lock = asyncio.Lock()
        self._rate_buckets: dict[tuple[str, int, int], deque[float]] = {}

    async def _allow_rate(self, key: tuple[str, int, int], limit: int) -> bool:
        now = time.monotonic()
        async with self._rate_lock:
            bucket = self._rate_buckets.setdefault(key, deque())
            while bucket and now - bucket[0] >= AFK_RATE_WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def _database_failed(self, error: Exception):
        """Back off AFK lookups during database trouble without flooding logs."""
        db = getattr(self.bot, "db", None)
        if db is not None and hasattr(db, "mark_unavailable"):
            db.mark_unavailable()
        now = time.monotonic()
        if now >= self._db_warning_after:
            log.warning("Skipping AFK message checks while the database is unavailable (%s)", type(error).__name__)
            self._db_warning_after = now + 30.0

    @commands.command(name="afk", aliases=("away",), case_insensitive=True)
    @commands.guild_only()
    async def afk(self, ctx: commands.Context, *, reason: str = "I am AFK :-/"):
        """Choose a global or server-only AFK status."""
        reason = reason.strip()[:500] or "I am AFK :-/"
        embed = afk_embed(
            "AFK? See you soon!",
            f"Hey, {ctx.author.mention}\n\n"
            f"{icon('afk_arrow_arrow')} **Choose your preferred AFK status type**",
            ctx.author,
        )
        view = AFKView(self, ctx.author.id, reason)
        await ctx.send(view=component_card(embed, view), allowed_mentions=discord.AllowedMentions.none())

    async def ask_dm_preference(self, interaction: discord.Interaction, user_id: int, reason: str, scope: str):
        label = "Global" if scope == "global" else "Server"
        embed = afk_embed(
            f"{icon('afk_success')} {label} AFK Selected",
            f"{icon('afk_thinking')} **Did you want to get messages in your DM while you're AFK?**\n\n"
            f"{icon('afk_arrow_arrow')} Choose your preference below\n"
            f"-# Requested by: {interaction.user.display_name}",
            interaction.user,
        )
        view = AFKDMView(self, user_id, reason, scope)
        await interaction.response.edit_message(view=component_card(embed, view))

    async def activate(self, interaction: discord.Interaction, user_id: int, reason: str, scope: str, dm_enabled: bool):
        now = datetime.now(timezone.utc).timestamp()
        record = {
            "scope": scope,
            "guild_id": interaction.guild_id if scope == "server" else None,
            "reason": reason,
            "started_at": now,
            "dm_notifications": dm_enabled,
            "ping_count": 0,
            "pinged_by": [],
        }
        await self.bot.db.set_afk_status(user_id, record)
        label = "Global" if scope == "global" else "Server"
        embed = afk_embed(
            f"{icon('afk_success')} AFK Activated",
            f"{interaction.user.mention} Your AFK status is now set to {label}\n"
            f"**Reason:** {discord.utils.escape_markdown(reason)}\n"
            f"**DM Notifications:** {'Enabled' if dm_enabled else 'Disabled'}\n\n"
            f"{icon('afk_arrow_arrow')} You are now away from keyboard.",
            interaction.user,
        )
        await interaction.response.edit_message(view=component_card(embed))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        db = getattr(self.bot, "db", None)
        if db is None or getattr(db, "temporarily_unavailable", False):
            return

        # Any message from the AFK user means they have returned, including
        # commands, punctuation-only messages, stickers, and attachments.
        # Clear in one atomic operation so all message types behave alike.
        try:
            cleared = await db.clear_afk_status(message.author.id, guild_id=message.guild.id)
        except Exception as error:
            self._database_failed(error)
            return
        if cleared:
            pinged_by = cleared.get("pinged_by", [])
            ping_text = ", ".join(f"<@{int(user_id)}>" for user_id in pinged_by[:10]) if pinged_by else "No Pings"
            duration = format_duration(datetime.now(timezone.utc).timestamp() - float(cleared.get("started_at", 0)))
            embed = afk_embed(
                f"{icon('afk_greet')} Welcome Back {message.author.display_name}",
                f"**__AFK Duration:__** `{duration}`\n"
                f"**__Times Pinged:__** `{int(cleared.get('ping_count', 0))}`\n"
                f"**__Pinged By:__** {ping_text}\n\n"
                f"{icon('afk_arrow_arrow')} Your AFK status has been removed.",
                message.author,
            )
            await message.channel.send(
                view=component_card(embed),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        mentioned = {
            member.id: member
            for member in message.mentions
            if member.id != message.author.id
        }
        mentioned = dict(list(mentioned.items())[:AFK_MAX_MENTIONS_PER_MESSAGE])
        if not mentioned:
            return
        if not await self._allow_rate(
            ("mentions", message.guild.id, message.author.id), AFK_MENTION_RATE_LIMIT
        ):
            log.info("Rate-limited AFK mention processing for guild=%s user=%s", message.guild.id, message.author.id)
            return

        # Each ping is an atomic, guild-scoped update; run independent users
        # concurrently so a message mentioning several people does not wait
        # for each database request in sequence.
        async def record_ping(user_id: int):
            async with self._notification_semaphore:
                return await db.record_afk_ping(user_id, message.author.id, guild_id=message.guild.id)

        try:
            recorded = await asyncio.gather(*(record_ping(user_id) for user_id in mentioned))
        except Exception as error:
            self._database_failed(error)
            return

        notifications = []
        for user_id, updated in zip(mentioned, recorded):
            if not updated:
                continue
            member = mentioned[user_id]
            reason = discord.utils.escape_markdown(str(updated.get("reason") or "I am AFK :-/"))
            away_since = format_duration(datetime.now(timezone.utc).timestamp() - float(updated.get("started_at", 0)))
            notifications.append((user_id, member, reason, away_since, updated.get("dm_notifications")))

        if not notifications:
            return

        # One channel notification is enough for a message mentioning several
        # AFK users and avoids a burst of near-identical outbound messages.
        aggregate = afk_embed(
            f"{icon('afk_thinking')} AFK Members Mentioned",
            f"{len(notifications)} AFK member(s) were mentioned. They will see your message when they return.",
        )
        for _, member, reason, away_since, _ in notifications:
            aggregate.add_field(
                name=f"{member.display_name} ({member.id})",
                value=f"**Away:** `{away_since}`\n**Reason:** {reason}",
                inline=False,
            )
        try:
            async with self._notification_semaphore:
                await message.channel.send(
                    view=component_card(aggregate),
                    allowed_mentions=discord.AllowedMentions.none(),
                    reference=message,
                    mention_author=False,
                )
        except discord.Forbidden:
            log.warning("Could not send AFK mention notification in guild=%s", message.guild.id)
        except discord.HTTPException as error:
            log.warning("AFK mention notification failed in guild=%s with HTTP %s", message.guild.id, error.status)
            if error.status == 429:
                return

        dm_count = 0
        for user_id, _, _, _, dm_enabled in notifications:
            if not dm_enabled or dm_count >= AFK_MAX_DM_NOTIFICATIONS_PER_MESSAGE:
                continue
            if not await self._allow_rate(("notifications", message.guild.id, user_id), AFK_NOTIFICATION_RATE_LIMIT):
                log.info("Rate-limited AFK DM notification for guild=%s user=%s", message.guild.id, user_id)
                continue
            try:
                await self.send_ping_dm(message, user_id)
                dm_count += 1
            except discord.HTTPException as error:
                log.warning("AFK DM notification failed for user=%s with HTTP %s", user_id, error.status)
                if error.status == 429:
                    break

    async def send_ping_dm(self, message: discord.Message, user_id: int):
        try:
            async with self._notification_semaphore:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                message_text = discord.utils.escape_markdown(message.content) if message.content else (
                    ", ".join(sticker.name for sticker in message.stickers) if message.stickers else "Sticker/attachment"
                )
                await user.send(
                    view=component_card(afk_embed(
                        f"{icon('afk_pinged')} You were pinged while AFK",
                        f"**__Server__:** **{discord.utils.escape_markdown(message.guild.name)}**\n"
                        f"**__Channel__:** {message.channel.mention}\n"
                        f"**__Pinged By__:** {message.author.mention}\n"
                        f"**__Message__:** {message_text}\n\n"
                        f"[Jump to Message]({message.jump_url})",
                        message.author,
                    )),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            log.info("AFK DM notification unavailable for user=%s", user_id)


async def setup(bot):
    await bot.add_cog(AFK(bot))
