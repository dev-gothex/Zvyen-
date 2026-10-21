"""Per-server automatic message reactions."""

import re

import discord
from discord.ext import commands

from utils.emojis import icon
from utils.ui import component_card, panel
from utils.owner_permissions import owner_or_guild_permissions


def normalize_mentions(text: str) -> str:
    return re.sub(r"<@!?(\d+)>", r"<@\1>", text).casefold()


class AutoReact(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="autoreact", aliases=("react",), invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def autoreact(self, ctx: commands.Context):
        """Manage automatic reactions for this server."""
        await ctx.invoke(self.bot.get_command("help"), query="autoreact")

    @autoreact.command(name="add", case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def add(self, ctx: commands.Context, *, trigger_and_emoji: str):
        """Add an automatic reaction: trigger text followed by an emoji."""
        parts = trigger_and_emoji.rsplit(maxsplit=1)
        if len(parts) != 2:
            prefix = ctx.prefix or ","
            return await ctx.send(view=component_card(panel("", f"Command usage: `{prefix}help autoreact add`")), delete_after=3)
        trigger, raw_emoji = parts[0].strip(), parts[1].strip()
        if not trigger:
            prefix = ctx.prefix or ","
            return await ctx.send(view=component_card(panel("", f"Command usage: `{prefix}help autoreact add`")), delete_after=3)
        emoji = discord.PartialEmoji.from_str(raw_emoji)
        if not emoji.name:
            return await ctx.send(view=component_card(panel(f"{icon('cross')} Invalid Emoji", "Please provide a valid emoji.")))
        stored_emoji = str(emoji)
        created = await self.bot.db.create_autoreaction(ctx.guild.id, trigger, stored_emoji)
        if not created:
            embed = panel(
                f"{icon('cross')} Trigger Exists",
                f"The trigger {trigger} already exists. Remove it before adding it again.",
            )
            return await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
        embed = panel(
            f"{icon('whitetick')} Trigger Added",
            f"Successfully added trigger {trigger} with emojis: {stored_emoji}",
        )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @autoreact.command(name="remove", aliases=("delete",), case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def remove(self, ctx: commands.Context, *, trigger: str):
        """Remove an automatic reaction trigger."""
        trigger = trigger.strip()
        removed = await self.bot.db.delete_autoreaction(ctx.guild.id, trigger)
        if not removed:
            embed = panel(f"{icon('cross')} Trigger Not Found", f"No reaction trigger was found for {trigger}.")
            return await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
        embed = panel(
            f"{icon('whitetick')} Trigger Removed",
            f"Successfully removed trigger {trigger}",
        )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @autoreact.command(name="list", case_insensitive=True)
    @commands.guild_only()
    async def list_triggers(self, ctx: commands.Context):
        """List this server's automatic reaction triggers."""
        triggers = await self.bot.db.list_autoreactions(ctx.guild.id)
        if not triggers:
            return await ctx.send(view=component_card(panel(f"{icon('cross')} Auto-Reaction Triggers", "There are no reaction triggers in this server.")))
        lines = [f"**{index}.** {item['trigger']} {item['emoji']}" for index, item in enumerate(triggers, 1)]
        await ctx.send(
            view=component_card(panel("Auto-Reaction Triggers", "\n".join(lines)[:4000])),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @autoreact.command(name="reset", case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def reset(self, ctx: commands.Context):
        """Remove all automatic reaction triggers from this server."""
        removed = await self.bot.db.clear_autoreactions(ctx.guild.id)
        if not removed:
            return await ctx.send(view=component_card(panel(f"{icon('cross')} No Triggers", "There are no reaction triggers to reset.")))
        await ctx.send(view=component_card(panel(f"{icon('whitetick')} Triggers Reset", f"Removed {removed} auto-reaction trigger(s).")))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        content = normalize_mentions(message.content)
        for item in await self.bot.db.list_autoreactions(message.guild.id):
            if normalize_mentions(item["trigger"]) in content:
                try:
                    await message.add_reaction(item["emoji"])
                except discord.HTTPException:
                    continue


async def setup(bot):
    await bot.add_cog(AutoReact(bot))
