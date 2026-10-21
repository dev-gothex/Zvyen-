"""Utility lookups, reminders, QR helpers, and media-only channel tools."""
import asyncio
import aiohttp
import io
import logging
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, panel


TIME_PART = re.compile(r"(\d+)([dhms])", re.IGNORECASE)
MEDIA_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".ogg", ".flac")
log = logging.getLogger("zyven.utility")


def parse_duration(value: str) -> int:
    parts = TIME_PART.findall(value.replace(" ", ""))
    if not parts or "".join(f"{n}{u}" for n, u in parts).lower() != value.replace(" ", "").lower():
        raise commands.BadArgument("Use a duration such as 10m, 1h30m, or 2d.")
    seconds = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit.lower()] for n, unit in parts)
    if not 1 <= seconds <= 30 * 86400:
        raise commands.BadArgument("Choose a duration from 1 second up to 30 days.")
    return seconds


def make_qr(text: str) -> discord.File:
    import qrcode

    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="zyven-qr.png")


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reminders: set[asyncio.Task] = set()
        self._banner_cache: dict[int, tuple[float, discord.User]] = {}

    async def cog_load(self):
        # Reminder and timer records live in MongoDB and resume after restart.
        for task_data in await self.bot.db.get_utility_tasks():
            self._schedule_task(task_data)

    async def cog_unload(self):
        for task in self.reminders:
            task.cancel()

    async def reply(self, ctx, title: str, description: str = "", *, color: int = 0x5865F2, file=None, delete_after: float | None = None):
        embed = panel(title, description, color=color)
        await ctx.send(view=component_card(embed), file=file, mention_author=False, allowed_mentions=discord.AllowedMentions.none(), delete_after=delete_after)

    @commands.command(name="embed", help="Build and send a custom embed")
    @commands.guild_only()
    async def embed_builder(self, ctx: commands.Context, channel: discord.TextChannel | None = None):
        """Open the interactive embed builder; an optional channel can be mentioned."""
        view = EmbedBuilderView(ctx, channel)
        await ctx.send(view=component_card(view.preview(), view), allowed_mentions=discord.AllowedMentions.none())

    async def require_media_channel(self, ctx) -> discord.TextChannel | None:
        """Return the configured media channel, or show the setup-required error."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        channel_id = config.get("media", {}).get("channel_id")
        channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await self.reply(ctx, f"{icon('error')} Error", "There is no media-only channel set.", color=0xED4245)
            return None
        return channel

    def _schedule_task(self, task_data: dict):
        task = asyncio.create_task(self._deliver_scheduled_task(task_data))
        self.reminders.add(task)
        task.add_done_callback(self.reminders.discard)

    async def _deliver_scheduled_task(self, task_data: dict):
        await asyncio.sleep(max(0, float(task_data["scheduled_at"]) - time.time()))
        guild_id = task_data.get("guild_id")
        guild = self.bot.get_guild(int(guild_id)) if guild_id else None
        user_id = int(task_data["user_id"])
        text = task_data["message"]
        kind = task_data["kind"]
        sent = False
        try:
            if kind == "reminder":
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                try:
                    await user.send(f"⏰ Reminder{f' from **{guild.name}**' if guild else ''}: {text}")
                    sent = True
                except discord.Forbidden:
                    pass
            if not sent:
                channel_id = task_data.get("channel_id")
                channel = self.bot.get_channel(int(channel_id)) if channel_id else None
                if channel is None and channel_id:
                    channel = await self.bot.fetch_channel(int(channel_id))
                if channel is not None:
                    notice = f"⏱️ <@{user_id}> **{text}**" if kind == "timer" else f"⏰ <@{user_id}>, your reminder: {text}"
                    await channel.send(notice, allowed_mentions=discord.AllowedMentions(users=True))
                    sent = True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Could not deliver scheduled %s %s", kind, task_data.get("_id"))
        finally:
            # A failed target may have left the server or blocked DMs; avoid
            # replaying an old notification on every future bot restart.
            await self.bot.db.delete_utility_task(str(task_data["_id"]))

    @commands.command(name="invite", case_insensitive=True)
    async def invite(self, ctx):
        """Get the bot invite and support links."""
        embed = panel("Zyven • Invite", "Add Zyven to your server or get help in the support server.")
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(label="Invite Me", style=discord.ButtonStyle.link, url=settings.invite_url))
        view.add_item(discord.ui.Button(label="Support Server", style=discord.ButtonStyle.link, url=settings.support_url))
        await ctx.send(view=component_card(embed, view), mention_author=False)

    @commands.command(name="roleicon", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def roleicon(self, ctx, role: discord.Role, emoji_text: str):
        """Set a role's icon from a Unicode or custom emoji."""
        guild = ctx.guild
        bot_member = guild.me
        if role.is_default() or role.managed:
            return await self.reply(ctx, f"{icon('error')} Cannot Update Role", "Default and integration-managed roles cannot have their icons changed.", color=0xED4245)
        if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
            return await self.reply(ctx, f"{icon('error')} Role Hierarchy", "You can only change the icon of roles below your highest role.", color=0xED4245)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return await self.reply(ctx, f"{icon('error')} Permission Required", "I need **Manage Roles** permission to update role icons.", color=0xED4245)
        if role >= bot_member.top_role:
            return await self.reply(ctx, f"{icon('error')} Role Hierarchy", "Move my highest role above the target role first.", color=0xED4245)
        if "ROLE_ICONS" not in guild.features:
            return await self.reply(ctx, f"{icon('error')} Role Icons Unavailable", "This server does not currently support role icons.", color=0xED4245)

        emoji_text = emoji_text.strip()
        partial = discord.PartialEmoji.from_str(emoji_text)
        display_icon: bytes | str
        try:
            if partial.id is None:
                # The API accepts a Unicode emoji directly as the role icon.
                display_icon = partial.name or emoji_text
                rendered_icon = display_icon
            else:
                extension = "gif" if partial.animated else "png"
                url = f"https://cdn.discordapp.com/emojis/{partial.id}.{extension}?size=128"
                # Reuse Discord's existing HTTP session instead of creating a
                # fresh TCP/TLS session for every role icon update.
                image_data = await asyncio.wait_for(self.bot.http.get_from_cdn(url), timeout=5)
                if partial.animated:
                    from PIL import Image

                    source = Image.open(io.BytesIO(image_data))
                    source.seek(0)
                    frame = source.convert("RGBA")
                    frame.thumbnail((128, 128))
                    output = io.BytesIO()
                    frame.save(output, format="PNG", optimize=True)
                    image_data = output.getvalue()
                display_icon = image_data
                rendered_icon = str(partial) if guild.get_emoji(partial.id) else f"`:{partial.name}:`"
            await role.edit(display_icon=display_icon, reason=f"Role icon updated by {ctx.author}")
        except (aiohttp.ClientError, discord.HTTPException, ValueError, OSError) as exc:
            log.info("Role icon update failed for role %s in guild %s: %s", role.id, guild.id, exc)
            return await self.reply(ctx, f"{icon('error')} Role Icon Update Failed", "I couldn't use that emoji as the role icon. Use a valid Unicode or custom emoji and check my role permissions.", color=0xED4245)

        moderator = discord.utils.escape_markdown(ctx.author.name)
        result = (
            f"{icon('wtick')} {role.mention}\n"
            f"{icon('members')} **Moderator :** `{moderator}`\n\n"
            f"{icon('space')}{icon('arrow')} **Icon :** {rendered_icon}"
        )
        await self.reply(ctx, "Role Icon Updated!", result, color=0x57F287)

    @commands.command(name="joined-at", aliases=("joinedat",), case_insensitive=True)
    @commands.guild_only()
    async def joined_at(self, ctx, member: discord.Member | None = None):
        """Show when a member joined this server and created their account."""
        member = member or ctx.author
        embed = panel(f"{member} • Join Dates")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:F>\n<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unavailable", inline=False)
        embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:F>\n<t:{int(member.created_at.timestamp())}:R>", inline=False)
        await ctx.send(view=component_card(embed), mention_author=False)

    @commands.command(name="github", case_insensitive=True)
    async def github(self, ctx):
        """Show the configured source code repository, if one is set."""
        url = getattr(settings, "github_url", "")
        if not url:
            await self.reply(ctx, "GitHub", "The project repository link has not been configured yet.")
            return
        embed = panel("Zyven • GitHub", f"[Open the source repository]({url})")
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(label="View on GitHub", style=discord.ButtonStyle.link, url=url))
        await ctx.send(view=component_card(embed, view), mention_author=False)

    @commands.command(name="vcinfo", case_insensitive=True)
    @commands.guild_only()
    async def vcinfo(self, ctx, *, channel: discord.VoiceChannel | None = None):
        """Show information about a voice channel or your current voice channel."""
        channel = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if channel is None:
            await self.reply(ctx, "Voice Channel Info", "Join a voice channel or provide one: `?vcinfo #voice`.", color=0xFEE75C)
            return
        members = ", ".join(member.mention for member in channel.members[:25]) or "No members"
        embed = panel(f"{channel.name} • Voice Channel")
        embed.add_field(name="Channel", value=f"{channel.mention}\nID `{channel.id}`", inline=False)
        embed.add_field(name="Members", value=f"`{len(channel.members)}/{channel.user_limit or '∞'}`\n{members}", inline=False)
        embed.add_field(name="Bitrate / Region", value=f"`{channel.bitrate // 1000} kbps` • `{channel.rtc_region or 'Automatic'}`", inline=False)
        if channel.category:
            embed.add_field(name="Category", value=channel.category.mention, inline=False)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="channelinfo", aliases=("ci",), case_insensitive=True)
    @commands.guild_only()
    async def channelinfo(self, ctx, channel: discord.abc.GuildChannel | None = None):
        """Show information about a server channel."""
        channel = channel or ctx.channel
        embed = panel(f"#{channel.name} • Channel Info")
        embed.add_field(name="General", value=f"ID `{channel.id}`\nType `{str(channel.type).replace('_', ' ').title()}`\nCreated <t:{int(channel.created_at.timestamp())}:R>", inline=False)
        embed.add_field(name="Position / Category", value=f"`{channel.position}` • {channel.category.mention if channel.category else 'None'}", inline=False)
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="Text Settings", value=f"Topic: {channel.topic or 'None'}\nSlowmode: `{channel.slowmode_delay}s`\nNSFW: `{channel.nsfw}`", inline=False)
        elif isinstance(channel, discord.VoiceChannel):
            embed.add_field(name="Voice Settings", value=f"Members: `{len(channel.members)}`\nLimit: `{channel.user_limit or 'Unlimited'}`\nBitrate: `{channel.bitrate // 1000} kbps`", inline=False)
        await ctx.send(view=component_card(embed), mention_author=False)

    @commands.command(name="badges", case_insensitive=True)
    @commands.guild_only()
    async def badges(self, ctx, member: discord.Member | None = None):
        """Show a member's public Discord profile badges."""
        member = member or ctx.author
        user = await self.bot.fetch_user(member.id)
        flags = [name.replace("_", " ").title() for name, enabled in user.public_flags if enabled]
        await self.reply(ctx, f"{member} • Badges", "\n".join(f"• {name}" for name in flags) if flags else "This member has no public profile badges.")

    @commands.command(name="banner", case_insensitive=True)
    @commands.guild_only()
    async def banner(self, ctx, member: discord.Member | None = None):
        """Show a member's profile banner with an open/download link."""
        member = member or ctx.author
        now = time.monotonic()
        cached = self._banner_cache.get(member.id)
        if cached and cached[0] > now:
            user = cached[1]
        else:
            try:
                # Discord profile lookups can stall independently of the bot's
                # gateway. Bound the wait and turn API failures into a normal
                # command response instead of an unhandled callback error.
                user = await asyncio.wait_for(self.bot.fetch_user(member.id), timeout=8.0)
            except (asyncio.TimeoutError, discord.HTTPException) as error:
                log.warning("Banner lookup failed for user %s (%s)", member.id, type(error).__name__)
                await self.reply(ctx, "Banner Unavailable", "Discord did not return this profile banner in time. Please try again shortly.", color=0xFEE75C)
                return
            self._banner_cache[member.id] = (now + 300, user)
            if len(self._banner_cache) > 500:
                expired = [user_id for user_id, (expires, _) in self._banner_cache.items() if expires <= now]
                for user_id in expired:
                    self._banner_cache.pop(user_id, None)
        if user.banner is None:
            await self.reply(ctx, "Banner", f"{member.mention} does not have a profile banner.", color=0xFEE75C)
            return
        url = user.banner.with_size(2048).url
        embed = panel(f"{member.display_name} • Banner", f"[Open full-size image]({url})")
        embed.set_image(url=url)
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(label="Download Banner", style=discord.ButtonStyle.link, url=url))
        await ctx.send(view=component_card(embed, view), mention_author=False)

    @commands.command(name="urban", case_insensitive=True)
    async def urban(self, ctx: commands.Context, *, phrase: str):
        """Look up a phrase in Urban Dictionary."""
        phrase = phrase.strip()
        if not phrase:
            return await self.reply(ctx, "Urban", f"Usage: `{settings.prefix}urban <phrase>`", color=0xFEE75C)
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://api.urbandictionary.com/v0/define", params={"term": phrase}) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.exception("Urban Dictionary lookup failed for query %r", phrase)
            return await self.reply(ctx, "Urban Lookup Unavailable", "The definition service could not be reached. Try again shortly.", color=0xFEE75C)
        definitions = payload.get("list") or []
        if not definitions:
            return await self.reply(ctx, f'Meaning of "{discord.utils.escape_markdown(phrase)}"', "No definition was found.", color=0xFEE75C)
        result = max(definitions, key=lambda item: int(item.get("thumbs_up", 0) or 0))
        clean = lambda value: discord.utils.escape_markdown(str(value or "").replace("[", "").replace("]", ""))
        embed = panel(f'Meaning of "{clean(result.get("word") or phrase)}"')
        embed.add_field(name="Definition:", value=clean(result.get("definition"))[:1024] or "Not provided.", inline=True)
        embed.add_field(name="Example:", value=clean(result.get("example"))[:1024] or "Not provided.", inline=True)
        embed.add_field(name="Author:", value=clean(result.get("author"))[:256] or "Unknown", inline=True)
        embed.add_field(name="Written On:", value=f"`{clean(result.get('written_on')) or 'Unknown'}`", inline=False)
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="permissions", aliases=("perms",), case_insensitive=True)
    @commands.guild_only()
    async def permissions(self, ctx, member: discord.Member):
        """List enabled key server permissions for a member."""
        enabled = [
            name.replace("_", " ").title()
            for name, value in member.guild_permissions
            if value
        ]
        embed = discord.Embed(title=f"Permissions of {member.display_name}", color=0x2B2D31)
        if not enabled:
            embed.add_field(name="Permissions", value="```None```", inline=False)
        else:
            parts = []
            current = []
            for name in enabled:
                candidate = ", ".join((*current, name))
                if len(candidate) > 990 and current:
                    parts.append(", ".join(current))
                    current = [name]
                else:
                    current.append(name)
            if current:
                parts.append(", ".join(current))
            for index, part in enumerate(parts):
                embed.add_field(
                    name="Permissions" if index == 0 else "Permissions (continued)",
                    value=f"```{part}```",
                    inline=False,
                )
        await ctx.send(view=component_card(embed), mention_author=False)

    @commands.command(name="reminder", aliases=("remind",), case_insensitive=True)
    async def reminder(self, ctx, duration: str, *, message: str):
        """Send yourself a reminder after a duration, e.g. `?reminder 20m drink water`."""
        seconds = parse_duration(duration)
        task_data = {
            "_id": str(uuid.uuid4()), "kind": "reminder", "guild_id": ctx.guild.id if ctx.guild else None,
            "channel_id": ctx.channel.id if ctx.guild else None, "user_id": ctx.author.id,
            "message": message, "scheduled_at": time.time() + seconds,
        }
        await self.bot.db.add_utility_task(task_data)
        self._schedule_task(task_data)
        await self.reply(ctx, "Reminder Set", f"I will remind you in **{duration}**: {discord.utils.escape_markdown(message)}")

    @commands.command(name="timer", case_insensitive=True)
    async def timer(self, ctx, duration: str, *, label: str = "Timer finished"):
        """Start a countdown timer, e.g. `?timer 5m break` (maximum 30 days)."""
        seconds = parse_duration(duration)
        task_data = {
            "_id": str(uuid.uuid4()), "kind": "timer", "guild_id": ctx.guild.id if ctx.guild else None,
            "channel_id": ctx.channel.id, "user_id": ctx.author.id,
            "message": discord.utils.escape_markdown(label), "scheduled_at": time.time() + seconds,
        }
        await self.bot.db.add_utility_task(task_data)
        self._schedule_task(task_data)
        await self.reply(ctx, "Timer Started", f"**{duration}** • {discord.utils.escape_markdown(label)}")

    @commands.command(name="prime", case_insensitive=True)
    async def prime(self, ctx):
        """Show information about Zyven Prime."""
        embed = panel("Zyven Prime", f"For premium details and availability, visit the [support server]({settings.support_url}).")
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(label="Support Server", style=discord.ButtonStyle.link, url=settings.support_url))
        await ctx.send(view=component_card(embed, view), mention_author=False)

    @commands.command(name="makeqr", case_insensitive=True)
    async def makeqr(self, ctx, *, text: str):
        """Create a downloadable QR image from text or a URL."""
        if len(text) > 1500:
            await self.reply(ctx, "QR Code", "QR content must be 1,500 characters or fewer.", color=0xFEE75C)
            return
        embed = panel("QR Code", "Generated QR code • scan or download the attached image.")
        embed.set_image(url="attachment://zyven-qr.png")
        await ctx.send(view=component_card(embed), file=make_qr(text), mention_author=False)

    @commands.command(name="setupi", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setupi(self, ctx, upi_id: str):
        """Set this server's UPI ID used by `showqr` (Manage Server required)."""
        if len(upi_id) > 100 or not re.fullmatch(r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}", upi_id):
            await self.reply(ctx, "Invalid UPI ID", "Provide a valid UPI handle, for example `name@bank`.", color=0xFEE75C)
            return
        await self.bot.db.set_nested(ctx.guild.id, "utility", "upi_id", upi_id)
        await self.reply(ctx, "UPI Saved", f"The server UPI ID is saved. Members can view its QR with `{ctx.prefix}showqr [amount]`.", color=0x57F287)

    @commands.command(name="showqr", aliases=("showupi",), case_insensitive=True)
    @commands.guild_only()
    async def showqr(self, ctx, amount: str | None = None):
        """Show the server UPI QR, optionally with a fixed payment amount."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        upi_id = config.get("utility", {}).get("upi_id")
        if not upi_id:
            await self.reply(ctx, "UPI Not Set", "A server manager can configure this with `?setupi name@bank`.", color=0xFEE75C)
            return
        payment_uri = f"upi://pay?{urlencode({'pa': upi_id, 'cu': 'INR'})}"
        amount_text = None
        if amount is not None:
            try:
                parsed_amount = Decimal(amount)
            except (InvalidOperation, ValueError):
                parsed_amount = Decimal("NaN")
            if not parsed_amount.is_finite() or parsed_amount <= 0 or parsed_amount > Decimal("10000000") or parsed_amount.as_tuple().exponent < -2:
                await self.reply(ctx, "Invalid QR Amount", f"Use a positive amount up to ₹10,000,000 with at most 2 decimal places, for example `{ctx.prefix}showqr 50`.", color=0xFEE75C)
                return
            amount_text = format(parsed_amount, "f")
            payment_uri += f"&{urlencode({'am': amount_text})}"
        details = f"UPI ID: `{upi_id}`\n" + (
            f"Fixed amount: **₹{amount_text}**. The payer can scan this QR to pay that amount."
            if amount_text is not None
            else "No amount is fixed. The payer can enter any amount in their payment app."
        )
        embed = panel("Server UPI QR", details)
        embed.set_image(url="attachment://zyven-qr.png")
        await ctx.send(view=component_card(embed), file=make_qr(payment_uri), mention_author=False)

    @commands.group(name="media", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def media(self, ctx):
        """Manage a media-only channel and user bypass list."""
        help_command = self.bot.get_command("help")
        await ctx.invoke(help_command, query="media")

    @media.command(name="setup", case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_setup(self, ctx, channel: discord.TextChannel):
        """Set up a media-only channel."""
        me = ctx.guild.me
        if me is None or not channel.permissions_for(me).manage_messages:
            await self.reply(ctx, "Bot Permission Required", f"Grant Zyven **Manage Messages** in {channel.mention} so it can remove non-media messages.", color=0xED4245)
            return
        config = await self.bot.db.get_guild(ctx.guild.id)
        current_id = config.get("media", {}).get("channel_id")
        current_channel = ctx.guild.get_channel(int(current_id)) if current_id else None
        if current_channel:
            if current_channel.id == channel.id:
                await self.reply(ctx, "Media Already Enabled", f"Media-only mode is already enabled in {channel.mention}.", color=0xFEE75C)
            else:
                await self.reply(ctx, "Media Already Configured", f"Media-only mode is already enabled in {current_channel.mention}. Run `{settings.prefix}media remove` before setting up another channel.", color=0xFEE75C)
            return
        await self.bot.db.set_nested(ctx.guild.id, "media", "channel_id", channel.id)
        await self.reply(ctx, f"{icon('approve')} Success", f"Successfully set {channel.mention} as the media-only channel.\n\nMake sure to grant me \"Manage Messages\" permission for functioning of media channel.", color=0x57F287)

    @media.command(name="remove", case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_remove(self, ctx):
        """Remove the configured media-only channel."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        channel_id = config.get("media", {}).get("channel_id")
        if not channel_id:
            await self.reply(ctx, f"{icon('error')} Error", "There is no media-only channel set.", color=0xED4245)
            return
        channel = ctx.guild.get_channel(int(channel_id))
        await self.bot.db.unset_value(ctx.guild.id, "media.channel_id")
        location = f" in {channel.mention}" if channel else ""
        await self.reply(ctx, f"{icon('approve')} Success", f"Media-only mode has been disabled{location}. The bypass list is kept if you set it up again.", color=0x57F287)

    @media.command(name="config", case_insensitive=True)
    async def media_config(self, ctx):
        """Show the configured media-only channel."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        channel_id = config.get("media", {}).get("channel_id")
        channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None
        status = f"Enabled in {channel.mention}." if channel else "Disabled. No media-only channel is configured."
        await self.reply(ctx, "Media Channel Configuration", status)

    @media.group(name="bypass", invoke_without_command=True, case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_bypass(self, ctx):
        """Manage members who can bypass the media-only channel."""
        if await self.require_media_channel(ctx) is None:
            return
        await self.reply(ctx, "Media Bypass", f"`{settings.prefix}media bypass add <member|role>`\n`{settings.prefix}media bypass remove <member|role>`\n`{settings.prefix}media bypass show`")

    @media_bypass.command(name="add", case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_bypass_add(self, ctx, member_or_role: discord.Role | discord.Member):
        """Add a member or role to the media bypass list."""
        if await self.require_media_channel(ctx) is None:
            return
        if isinstance(member_or_role, discord.Role):
            if member_or_role.is_default():
                await self.reply(ctx, "Role Not Allowed", "The @everyone role cannot bypass media-only restrictions.", color=0xED4245)
                return
            key = "media.bypass_roles"
        else:
            key = "media.bypass_users"
        await self.bot.db.add_to_set(ctx.guild.id, key, member_or_role.id)
        await self.reply(ctx, f"{icon('approve')} Success", f"{member_or_role.mention} has been added to bypass list.\n\nMake sure to grant me \"Manage Messages\" permission for functioning of media channel.", color=0x57F287)

    @media_bypass.command(name="remove", case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_bypass_remove(self, ctx, member_or_role: discord.Role | discord.Member):
        """Remove a member or role from the media bypass list."""
        if await self.require_media_channel(ctx) is None:
            return
        key = "media.bypass_roles" if isinstance(member_or_role, discord.Role) else "media.bypass_users"
        await self.bot.db.pull_value(ctx.guild.id, key, member_or_role.id)
        await self.reply(ctx, f"{icon('approve')} Success", f"{member_or_role.mention} has been removed from bypass list.", color=0x57F287)

    @media_bypass.command(name="show", case_insensitive=True)
    @commands.has_guild_permissions(manage_channels=True)
    async def media_bypass_show(self, ctx):
        """Show members who can bypass the media-only channel."""
        if await self.require_media_channel(ctx) is None:
            return
        config = await self.bot.db.get_guild(ctx.guild.id)
        media_config = config.get("media", {})
        user_ids = media_config.get("bypass_users", [])
        role_ids = media_config.get("bypass_roles", [])
        members = [f"<@{user_id}>" for user_id in user_ids]
        roles = [f"<@&{role_id}>" for role_id in role_ids]
        sections = []
        if members:
            sections.append("**Members**\n" + "\n".join(members))
        if roles:
            sections.append("**Roles**\n" + "\n".join(roles))
        await self.reply(ctx, "Media Bypass List", "\n\n".join(sections) if sections else "The bypass list is empty.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if message.content.startswith(settings.prefix):
            return
        config = await self.bot.db.get_guild(message.guild.id)
        media_config = config.get("media", {})
        if int(media_config.get("channel_id", 0) or 0) != message.channel.id:
            return
        bypass_users = {int(value) for value in media_config.get("bypass_users", [])}
        bypass_roles = {int(value) for value in media_config.get("bypass_roles", [])}
        member_role_ids = {role.id for role in message.author.roles} if isinstance(message.author, discord.Member) else set()
        if message.author.id in bypass_users or bypass_roles.intersection(member_role_ids):
            return
        is_media = any(
            (attachment.content_type or "").startswith(("image/", "video/", "audio/"))
            or attachment.filename.lower().endswith(MEDIA_EXTENSIONS)
            for attachment in message.attachments
        ) or bool(message.stickers) or any(embed.image.url or embed.video.url for embed in message.embeds)
        if is_media:
            return
        me = message.guild.me
        if me is None or not message.channel.permissions_for(me).manage_messages:
            return
        try:
            await message.delete()
            warning = await message.channel.send(f"{message.author.mention} This channel is Media only. Please send only media files.", allowed_mentions=discord.AllowedMentions(users=True))
            await asyncio.sleep(8)
            await warning.delete()
        except discord.HTTPException:
            pass


class EmbedBuilderView(discord.ui.View):
    OPTIONS = (
        ("title", "Title", "Set the embed title"),
        ("description", "Description", "Set the embed description"),
        ("field", "Add Field", "Add a name and value field"),
        ("color", "Color", "Set a hex color, for example 5865F2"),
        ("thumbnail", "Thumbnail", "Set a thumbnail image URL"),
        ("image", "Image", "Set the main image URL"),
        ("footer", "Footer Text", "Set footer text"),
        ("footer_icon", "Footer Icon", "Set the footer icon URL"),
        ("author", "Author Text", "Set author text"),
        ("author_icon", "Author Icon", "Set the author icon URL"),
    )

    def __init__(self, ctx: commands.Context, channel: discord.TextChannel | None):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.requester_id = ctx.author.id
        self.channel = channel or ctx.channel
        self.title = ""
        self.description = ""
        self.color = 0x5865F2
        self.thumbnail_url = ""
        self.image_url = ""
        self.footer_text = ""
        self.footer_icon = ""
        self.author_text = ""
        self.author_icon = ""
        self.fields: list[tuple[str, str, bool]] = []
        self.add_item(EmbedEditSelect(self))
        self.add_item(EmbedTargetChannelSelect(self, row=1))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the member who opened this embed builder can edit it.", ephemeral=True)
        return False

    def preview(self) -> discord.Embed:
        setup_instructions = (
            "Example embed. You can customize everything.\n\n"
            "• Select an option below to edit the embed.\n\n"
            "Edit the embed title and description to remove these instructions.\n\n"
            "This setup expires in 3 minutes."
        )
        ready_to_send = bool(self.title.strip() and self.description.strip())
        embed = discord.Embed(
            title=self.title or "Edit your Embed!",
            description=self.description if ready_to_send else setup_instructions,
            color=self.color,
        )
        for name, value, inline in self.fields:
            embed.add_field(name=name, value=value, inline=inline)
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)
        if self.image_url:
            embed.set_image(url=self.image_url)
        if self.footer_text or self.footer_icon:
            embed.set_footer(text=self.footer_text or None, icon_url=self.footer_icon or None)
        if self.author_text or self.author_icon:
            embed.set_author(name=self.author_text or "Embed", icon_url=self.author_icon or None)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=component_card(self.preview(), self))

    @discord.ui.button(label="Send Embed", style=discord.ButtonStyle.success, row=2)
    async def send_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.title.strip() or not self.description.strip():
            await interaction.response.send_message("Please set both the embed title and description before sending.", ephemeral=True)
            return
        channel = self.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text channel for this embed.", ephemeral=True)
            return
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not perms.send_messages or not perms.embed_links:
            await interaction.response.send_message(f"I need **Send Messages** and **Embed Links** in {channel.mention}.", ephemeral=True)
            return
        try:
            await channel.send(embed=self.preview(), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.response.send_message("Discord couldn't send this embed. Check that the image URLs and field text are valid.", ephemeral=True)
            return
        self.stop()
        done = panel("✅ Success", f"Sent the embed message to {channel.mention}.")
        await interaction.response.edit_message(view=component_card(done))

    @discord.ui.button(label="Cancel Setup", style=discord.ButtonStyle.danger, row=2)
    async def cancel_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=component_card(panel("Embed Setup Cancelled", "No embed was sent.")))


class EmbedEditSelect(discord.ui.Select):
    def __init__(self, parent: EmbedBuilderView):
        options = [discord.SelectOption(label=label, value=value, description=description) for value, label, description in parent.OPTIONS]
        super().__init__(placeholder="Choose an option to edit the embed", min_values=1, max_values=1, options=options, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EmbedEditModal(self.parent_view, self.values[0]))


class EmbedTargetChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: EmbedBuilderView, *, row: int):
        super().__init__(placeholder="Choose the channel to send the embed", channel_types=[discord.ChannelType.text, discord.ChannelType.news], min_values=1, max_values=1, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text or announcement channel.", ephemeral=True)
            return
        self.parent_view.channel = channel
        await self.parent_view.refresh(interaction)


class EmbedEditModal(discord.ui.Modal):
    def __init__(self, parent: EmbedBuilderView, key: str):
        labels = {value: label for value, label, _ in parent.OPTIONS}
        super().__init__(title=f"Edit {labels[key]}")
        self.parent_view = parent
        self.key = key
        if key == "field":
            self.value_input = discord.ui.TextInput(label="Field name", max_length=256)
            self.extra_input = discord.ui.TextInput(label="Field value", style=discord.TextStyle.paragraph, max_length=1024)
            self.add_item(self.value_input)
            self.add_item(self.extra_input)
            return
        current = {
            "title": parent.title, "description": parent.description, "color": f"{parent.color:06X}",
            "thumbnail": parent.thumbnail_url, "image": parent.image_url, "footer": parent.footer_text,
            "footer_icon": parent.footer_icon, "author": parent.author_text, "author_icon": parent.author_icon,
        }[key]
        self.value_input = discord.ui.TextInput(
            label=labels[key], default=current or None,
            style=discord.TextStyle.paragraph if key == "description" else discord.TextStyle.short,
            max_length={"title": 256, "description": 4000, "color": 6, "footer": 2048, "author": 256}.get(key, 400),
            required=True,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        parent = self.parent_view
        value = self.value_input.value.strip()
        if self.key == "field":
            if len(parent.fields) >= 25:
                await interaction.response.send_message("An embed can have at most 25 fields.", ephemeral=True)
                return
            parent.fields.append((value, self.extra_input.value.strip(), False))
        elif self.key == "color":
            try:
                parent.color = int(value.removeprefix("#"), 16)
            except ValueError:
                await interaction.response.send_message("Enter a valid six-digit hex color, for example `5865F2`.", ephemeral=True)
                return
            if not 0 <= parent.color <= 0xFFFFFF:
                await interaction.response.send_message("Enter a valid six-digit hex color, for example `5865F2`.", ephemeral=True)
                return
        elif self.key in {"thumbnail", "image", "footer_icon", "author_icon"}:
            if not value.startswith(("https://", "http://")):
                await interaction.response.send_message("Enter a valid image URL beginning with `https://` or `http://`.", ephemeral=True)
                return
            setattr(parent, {"thumbnail": "thumbnail_url", "image": "image_url", "footer_icon": "footer_icon", "author_icon": "author_icon"}[self.key], value)
        else:
            setattr(parent, {"title": "title", "description": "description", "footer": "footer_text", "author": "author_text"}[self.key], value)
        await parent.refresh(interaction)


async def setup(bot):
    await bot.add_cog(Utility(bot))
