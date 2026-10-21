"""Giveaway setup, entry tracking, and scheduled winner selection."""
import logging
import random
import time

import discord
from discord.ext import commands, tasks

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, notice_panel, panel
from cogs.utility import parse_duration

log = logging.getLogger("zyven.giveaways")
ENTRY_EMOJI_ID = 1561262732882481203
ENTRY_EMOJI_NAME = "winner"
GIVEAWAY_HEADER = "giveaway_entry"
GIVEAWAY_YELLOW = 0xFFFF00
FALLBACK_ENTRY_EMOJI = "🎉"


class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.finish_due_giveaways.start()

    def cog_unload(self):
        self.finish_due_giveaways.cancel()

    async def _can_manage(self, ctx: commands.Context) -> bool:
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("giveaway", {})
        staff_role_id = config.get("staff_role")
        if ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.manage_guild:
            return True
        if staff_role_id and any(role.id == int(staff_role_id) for role in getattr(ctx.author, "roles", [])):
            return True
        return False

    @commands.command(name="gstart", aliases=("giveawaystart",), help="Start a giveaway: gstart <time> <winners> <prize>")
    @commands.guild_only()
    async def gstart(self, ctx: commands.Context, duration: str, winners: int, *, prize: str):
        if not await self._can_manage(ctx):
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** or the configured giveaway staff role to start giveaways.")), allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            seconds = parse_duration(duration)
        except commands.BadArgument as exc:
            await ctx.send(view=component_card(notice_panel(str(exc))), allowed_mentions=discord.AllowedMentions.none())
            return
        if not 1 <= winners <= 25:
            await ctx.send(view=component_card(notice_panel("Choose between 1 and 25 winners.")), allowed_mentions=discord.AllowedMentions.none())
            return
        prize = prize.strip()
        if not prize or len(prize) > 200:
            await ctx.send(view=component_card(notice_panel("Giveaway prize must be 1–200 characters long.")), allowed_mentions=discord.AllowedMentions.none())
            return

        guild_config = (await self.bot.db.get_guild(ctx.guild.id)).get("giveaway", {})
        ends_at = int(time.time() + seconds)
        embed = discord.Embed(
            description=(
                f"{icon('giveaway_prize')} **{prize}** {icon('giveaway_prize')}\n\n"
                f"{icon('giveaway_dot')} **Winners:** {winners}\n"
                f"{icon('giveaway_dot')} **Ends** <t:{ends_at}:R> (<t:{ends_at}:f>)\n"
                f"{icon('giveaway_dot')} **Hosted by** {ctx.author.mention}\n\n"
                f"{icon('giveaway_dot')} **React with {icon('giveaway_reaction')} to participate!**\n\n"
                f"Ends at • <t:{ends_at}:f>"
            ),
            color=GIVEAWAY_YELLOW,
        )
        image = guild_config.get("image_url")
        if image:
            embed.set_image(url=image)
        header = f"{icon(GIVEAWAY_HEADER)} **New Giveaway** {icon(GIVEAWAY_HEADER)}"
        message = await ctx.send(content=header, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        entry_emoji = ctx.guild.get_emoji(ENTRY_EMOJI_ID) or FALLBACK_ENTRY_EMOJI
        entry_emoji_id = getattr(entry_emoji, "id", None)
        entry_emoji_name = getattr(entry_emoji, "name", None) or entry_emoji
        if entry_emoji_id is None:
            embed.description = embed.description.replace(
                f"React with {icon('giveaway_reaction')}", f"React with {FALLBACK_ENTRY_EMOJI}"
            )
            await message.edit(embed=embed)
        try:
            await message.add_reaction(entry_emoji)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not add giveaway reaction %s to %s: %s; trying the standard emoji", entry_emoji, message.id, exc)
            entry_emoji = FALLBACK_ENTRY_EMOJI
            entry_emoji_id = None
            entry_emoji_name = FALLBACK_ENTRY_EMOJI
            try:
                await message.add_reaction(entry_emoji)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not add the fallback giveaway reaction to message %s; giveaway will still start", message.id)
            else:
                embed.description = embed.description.replace(
                    f"React with {icon('giveaway_reaction')}", f"React with {FALLBACK_ENTRY_EMOJI}"
                )
                await message.edit(embed=embed)
        await self.bot.db.create_giveaway({
            "_id": str(message.id), "guild_id": ctx.guild.id, "channel_id": ctx.channel.id,
            "host_id": ctx.author.id, "prize": prize, "winners": winners,
            "ends_at": ends_at, "active": True, "entries": [],
            "entry_emoji_id": entry_emoji_id, "entry_emoji_name": entry_emoji_name,
        })
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.info("Could not delete gstart command message %s", ctx.message.id)

    @commands.command(name="gconfig", help="Show giveaway configuration")
    @commands.guild_only()
    async def gconfig(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("giveaway", {})
        role = ctx.guild.get_role(int(config["staff_role"])) if config.get("staff_role") else None
        image_url = config.get("image_url")
        if not role and not image_url:
            description = f"{icon('arrow_arrow')} No giveaway staff role and image is configured in this server."
        else:
            parts = []
            if role:
                parts.append(f"**Staff Role:** {role.mention}")
            if image_url:
                parts.append(f"**Giveaway Image:** [Open image]({image_url})")
            description = "\n".join(parts)
        embed = panel(f"{icon('giveaway')} Giveaway Configuration", description, color=GIVEAWAY_YELLOW)
        if image_url:
            embed.set_thumbnail(url=image_url)
        embed.set_author(name=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="gwystaff", invoke_without_command=True, help="Configure giveaway staff")
    @commands.guild_only()
    async def gstaff(self, ctx: commands.Context):
        await ctx.send(view=component_card(panel("Giveaway Staff", f"Use `{settings.prefix}gwystaff role <role>` to set the giveaway staff role.")), allowed_mentions=discord.AllowedMentions.none())

    @gstaff.command(name="role", help="Set the role allowed to manage giveaways")
    async def gstaff_role(self, ctx: commands.Context, role: discord.Role):
        if ctx.author.id != ctx.guild.owner_id and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to set the giveaway staff role.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self.bot.db.set_nested(ctx.guild.id, "giveaway", "staff_role", role.id)
        await ctx.send(view=component_card(panel(f"{icon('antibot_success')} Staff Role Set", f"Successfully set {role.mention} as the giveaway staff role.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="gembed", invoke_without_command=True, help="Configure the giveaway image")
    @commands.guild_only()
    async def gembed(self, ctx: commands.Context, *, image_url: str | None = None):
        if image_url or ctx.message.attachments:
            await self._save_giveaway_image(ctx, image_url)
            return
        await ctx.send(view=component_card(panel("Giveaway Embed", f"Use `{settings.prefix}gembed set <image URL>` or attach an image to the command.")), allowed_mentions=discord.AllowedMentions.none())

    @gembed.command(name="set", help="Set the image shown in new giveaways")
    async def gembed_set(self, ctx: commands.Context, *, image_url: str | None = None):
        await self._save_giveaway_image(ctx, image_url)

    async def _save_giveaway_image(self, ctx: commands.Context, image_url: str | None):
        if ctx.author.id != ctx.guild.owner_id and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to set the giveaway image.")), allowed_mentions=discord.AllowedMentions.none())
            return
        attachment = ctx.message.attachments[0] if ctx.message.attachments else None
        image_url = attachment.url if attachment else (image_url or "").strip().strip("<>")
        if not image_url:
            await ctx.send(view=component_card(notice_panel("Please provide an image URL or upload an image file.", title="Missing URL or Image")), allowed_mentions=discord.AllowedMentions.none())
            return
        if not image_url.startswith(("https://", "http://")):
            await ctx.send(view=component_card(notice_panel("The giveaway image must use a valid HTTP or HTTPS URL.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self.bot.db.set_nested(ctx.guild.id, "giveaway", "image_url", image_url)
        embed = panel(f"{icon('antibot_success')} Image Set", "Successfully set the giveaway embed image!")
        embed.set_thumbnail(url=image_url)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @gembed.command(name="reset", help="Remove the image shown in new giveaways")
    async def gembed_reset(self, ctx: commands.Context):
        if ctx.author.id != ctx.guild.owner_id and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to reset the giveaway image.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self.bot.db.unset_value(ctx.guild.id, "giveaway.image_url")
        await ctx.send(view=component_card(panel(f"{icon('giveaway_approve')} Image Reset", "Successfully reset the giveaway embed image!")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="glist", help="List ongoing giveaways")
    @commands.guild_only()
    async def glist(self, ctx: commands.Context):
        records = await self.bot.db.list_active_giveaways(ctx.guild.id)
        if not records:
            await ctx.send(view=component_card(panel("Ongoing Giveaways", "There are no active giveaways in this server.", color=GIVEAWAY_YELLOW)), allowed_mentions=discord.AllowedMentions.none())
            return
        lines = []
        for record in records[:15]:
            url = f"https://discord.com/channels/{record['guild_id']}/{record['channel_id']}/{record['_id']}"
            lines.append(f"**{discord.utils.escape_markdown(record['prize'])}**\nEnds: <t:{int(record['ends_at'])}:R> (<t:{int(record['ends_at'])}:f>)\nWinners: {record['winners']}\n[Jump to Message]({url})")
        await ctx.send(view=component_card(panel("Ongoing Giveaways", "\n\n".join(lines), color=GIVEAWAY_YELLOW)), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == self.bot.user.id or (payload.member and payload.member.bot):
            return
        giveaway = await self.bot.db.get_giveaway(payload.message_id)
        if not giveaway or not giveaway.get("active") or float(giveaway.get("ends_at", 0)) <= time.time():
            return
        if not self._is_entry_reaction(payload.emoji, giveaway):
            return
        if await self.bot.db.add_giveaway_entry(payload.message_id, payload.user_id):
            return
        # modified_count is zero for duplicate adds too; the entry is already present.

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        giveaway = await self.bot.db.get_giveaway(payload.message_id)
        if not giveaway or not self._is_entry_reaction(payload.emoji, giveaway):
            return
        await self.bot.db.remove_giveaway_entry(payload.message_id, payload.user_id)

    @staticmethod
    def _is_entry_reaction(emoji: discord.PartialEmoji, giveaway: dict) -> bool:
        expected_id = giveaway.get("entry_emoji_id")
        if expected_id is not None:
            return emoji.id == int(expected_id)
        return emoji.id is None and emoji.name == giveaway.get("entry_emoji_name", FALLBACK_ENTRY_EMOJI)

    @tasks.loop(seconds=5)
    async def finish_due_giveaways(self):
        for giveaway in await self.bot.db.list_active_giveaways():
            if float(giveaway.get("ends_at", 0)) > time.time():
                continue
            closed = await self.bot.db.close_giveaway(int(giveaway["_id"]))
            if closed:
                await self._finish_giveaway(closed)

    @finish_due_giveaways.before_loop
    async def before_finish_due_giveaways(self):
        await self.bot.wait_until_ready()

    async def _finish_giveaway(self, giveaway: dict):
        channel = self.bot.get_channel(int(giveaway["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(giveaway["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Could not retrieve giveaway channel for message %s", giveaway["_id"])
                return
        message = None
        try:
            message = await channel.fetch_message(int(giveaway["_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning("Could not retrieve giveaway message %s", giveaway["_id"])
        entries = list(dict.fromkeys(int(user_id) for user_id in giveaway.get("entries", [])))
        enough_entries = len(entries) >= int(giveaway["winners"])
        winner_ids = random.sample(entries, int(giveaway["winners"])) if enough_entries else []
        ended_at = int(time.time())
        if message and message.embeds:
            ended = message.embeds[0].copy()
        else:
            ended = discord.Embed(color=GIVEAWAY_YELLOW)
        ended.title = None
        ended.description = (
            f"{icon('giveaway_prize')} **{discord.utils.escape_markdown(giveaway['prize'])}** {icon('giveaway_prize')}\n\n"
            f"{icon('giveaway_dot')} **Ended at** <t:{ended_at}:R>\n"
            f"{icon('giveaway_dot')} **Hosted by** <@{giveaway['host_id']}>\n\n"
            + (f"{icon('giveaway_dot')} **Winners:** {', '.join(f'<@{user_id}>' for user_id in winner_ids)}"
               if winner_ids else f"{icon('giveaway_dot')} **No winner:** Not enough participants.")
            + f"\n\nEnded • <t:{ended_at}:f>"
        )
        ended.color = GIVEAWAY_YELLOW
        ended.remove_footer()
        content = f"{icon(GIVEAWAY_HEADER)} **Giveaway Ended** {icon(GIVEAWAY_HEADER)}"
        if winner_ids:
            mentions = ", ".join(f"<@{user_id}>" for user_id in winner_ids)
            result = (
                f"{icon('giveaway_reaction')} Congrats {mentions}, you won "
                f"**{discord.utils.escape_markdown(giveaway['prize'])}**! "
                f"Hosted by <@{giveaway['host_id']}>."
            )
        else:
            result = (
                f"No one won the **{discord.utils.escape_markdown(giveaway['prize'])}** giveaway "
                "because there were not enough participants."
            )
        try:
            await channel.send(result, allowed_mentions=discord.AllowedMentions(users=True))
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not send the result for giveaway %s", giveaway["_id"])
        try:
            if message:
                await message.edit(
                    content=content,
                    embed=ended,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                entry_emoji = discord.PartialEmoji(
                    name=giveaway.get("entry_emoji_name") or ENTRY_EMOJI_NAME,
                    id=giveaway.get("entry_emoji_id"),
                )
                await message.clear_reaction(entry_emoji)
            else:
                await channel.send(
                    content=content,
                    embed=ended,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not update the ended giveaway message %s", giveaway["_id"])


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
