import logging

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import SUCCESS, component_card, panel

log = logging.getLogger("zyven.antibot")


def _antibot_config(data: dict) -> dict:
    return data.get("antibot_purge", {}) or {}


def _antibot_config_embed(config: dict) -> discord.Embed:
    channels = [f"{icon('dotmoving')} <#{int(channel_id)}>" for channel_id in config.get("channels", [])]
    bots = [f"{icon('arrow_arrow')} <@{int(bot_id)}>" for bot_id in config.get("whitelisted_bots", [])]
    channel_list = "\n".join(channels) if channels else "None"
    bot_list = "\n".join(bots) if bots else f"{icon('arrow_arrow')} No bots whitelisted"
    description = (
        "**AntiBot Purge Configuration**\n\n"
        f"**{icon('antibot_settings')} __Auto-Purge Channels__**\n"
        f"{channel_list}\n\n"
        f"**{icon('antibot_settings')} __Whitelisted Bots__**\n"
        f"{bot_list}"
    )
    return panel("AntiBot Purge Configuration", description)


def _group_embed(entries: list[tuple[str, str]], page: int, page_count: int, requester: str, *, root: str = "antibot") -> discord.Embed:
    rows = [f"▏ `{settings.prefix}{root} {command}`\n   {icon('arrow')} {description}" for command, description in entries]
    rows.append(f"-# Requested by {requester} | Page {page + 1}/{page_count}")
    return panel("ZYVEN ANTIBOT GROUP", "\n\n".join(rows))


class AntibotGroupView(discord.ui.View):
    def __init__(self, requester_id: int, pages: list[list[tuple[str, str]]], requester: str, *, root: str = "antibot"):
        super().__init__(timeout=240)
        self.requester_id = requester_id
        self.pages = pages
        self.requester = requester
        self.root = root
        self.page = 0
        for emoji_name, action, style in (
            ("left", "first", discord.ButtonStyle.secondary),
            ("previous", "previous", discord.ButtonStyle.secondary),
            ("delete", "close", discord.ButtonStyle.danger),
            ("right", "next", discord.ButtonStyle.secondary),
            ("skip", "last", discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(emoji=discord.PartialEmoji.from_str(icon(emoji_name)), style=style)
            async def callback(interaction: discord.Interaction, action=action):
                if interaction.user.id != self.requester_id and not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message("Only the command user or a server manager can use these controls.", ephemeral=True)
                    return
                if action == "close":
                    await interaction.response.edit_message(view=component_card(panel("AntiBot Commands", "Command list closed.")))
                    return
                self.page = {"first": 0, "previous": max(0, self.page - 1), "next": min(len(self.pages) - 1, self.page + 1), "last": len(self.pages) - 1}[action]
                embed = _group_embed(self.pages[self.page], self.page, len(self.pages), self.requester, root=self.root)
                await interaction.response.edit_message(view=component_card(embed, self))
            button.callback = callback
            self.add_item(button)


class AntiBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _can_manage(self, ctx: commands.Context) -> bool:
        if ctx.author.id == self.bot.owner_id or ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.reply(view=component_card(panel("Permission Required", "You need **Manage Server** to configure AntiBot Purge.")), mention_author=False)
        return False

    def _requester(self, ctx: commands.Context) -> str:
        return getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None) or str(ctx.author.id)

    @commands.group(name="antibot", invoke_without_command=True)
    @commands.guild_only()
    async def antibot(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        entries = [
            ("add <channel>", "Add a channel where bot messages are auto-purged."),
            ("remove <channel>", "Remove a channel from Auto-Purge."),
            ("whitelist", "Manage bots exempt from Auto-Purge."),
            ("config", "View Auto-Purge channels and whitelisted bots."),
            ("reset", "Clear all AntiBot Purge settings."),
        ]
        requester = self._requester(ctx)
        pages = [entries]
        embed = _group_embed(entries, 0, len(pages), requester)
        await ctx.send(view=component_card(embed, AntibotGroupView(ctx.author.id, pages, requester)))

    @antibot.command(name="add")
    @commands.guild_only()
    async def add_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.add_to_set(ctx.guild.id, "antibot_purge.channels", channel.id)
        embed = panel(
            f"{icon('antibot_success')} Channel Added",
            f"{icon('antibot_settings')} **__Channel:__** {channel.mention} is successfully added to auto-purge\n\n"
            f"{icon('arrow_arrow')} Bot messages will be automatically deleted from this channel",
            color=SUCCESS,
        )
        await ctx.reply(view=component_card(embed), mention_author=False)

    @antibot.command(name="remove")
    @commands.guild_only()
    async def remove_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.pull_value(ctx.guild.id, "antibot_purge.channels", channel.id)
        embed = panel(f"{icon('antibot_success')} Channel Removed", f"{channel.mention} has been removed from Auto-Purge.", color=SUCCESS)
        await ctx.reply(view=component_card(embed), mention_author=False)

    @antibot.group(name="whitelist", invoke_without_command=True)
    @commands.guild_only()
    async def whitelist(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        entries = [
            ("add <bot>", "Add a bot to the whitelist so its messages are not auto-purged."),
            ("remove <bot>", "Remove a bot from the Auto-Purge whitelist."),
        ]
        requester = self._requester(ctx)
        pages = [entries]
        embed = _group_embed(entries, 0, len(pages), requester, root="antibot whitelist")
        await ctx.send(view=component_card(embed, AntibotGroupView(ctx.author.id, pages, requester, root="antibot whitelist")))

    @whitelist.command(name="add")
    @commands.guild_only()
    async def whitelist_add(self, ctx: commands.Context, bot_member: discord.Member):
        if not await self._can_manage(ctx):
            return
        if not bot_member.bot:
            await ctx.reply(view=component_card(panel("Bot Required", "Mention a bot account to add it to the whitelist.")), mention_author=False)
            return
        await self.bot.db.add_to_set(ctx.guild.id, "antibot_purge.whitelisted_bots", bot_member.id)
        embed = panel(
            f"{icon('antibot_success')} Bot Whitelisted",
            f"{icon('antibot_settings')} **__Bot:__** {bot_member.mention} is successfully added to whitelist\n\n"
            f"{icon('arrow_arrow')} Messages from this bot will not be auto-deleted",
            color=SUCCESS,
        )
        await ctx.reply(view=component_card(embed), mention_author=False)

    @whitelist.command(name="remove")
    @commands.guild_only()
    async def whitelist_remove(self, ctx: commands.Context, bot_member: discord.Member):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.pull_value(ctx.guild.id, "antibot_purge.whitelisted_bots", bot_member.id)
        embed = panel(f"{icon('antibot_success')} Bot Removed", f"{bot_member.mention} has been removed from the Auto-Purge whitelist.", color=SUCCESS)
        await ctx.reply(view=component_card(embed), mention_author=False)

    @antibot.command(name="config")
    @commands.guild_only()
    async def config(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        data = await self.bot.db.get_guild(ctx.guild.id)
        await ctx.send(view=component_card(_antibot_config_embed(_antibot_config(data))))

    @antibot.command(name="reset")
    @commands.guild_only()
    async def reset(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.set_values(ctx.guild.id, {
            "antibot_purge.channels": [],
            "antibot_purge.whitelisted_bots": [],
        })
        embed = panel(
            f"{icon('antibot_success')} Configuration Reset",
            f"{icon('antibot_settings')} **__Status:__** All AntiBot Purge settings cleared\n\n"
            f"{icon('arrow_arrow')} All channels and whitelisted bots have been removed",
            color=SUCCESS,
        )
        await ctx.reply(view=component_card(embed), mention_author=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not message.author.bot:
            return
        config = _antibot_config(await self.bot.db.get_guild(message.guild.id))
        channels = {int(value) for value in config.get("channels", [])}
        whitelisted = {int(value) for value in config.get("whitelisted_bots", [])}
        # Keep this bot's own command and status replies usable; other bots follow the server whitelist.
        if message.channel.id not in channels or message.author.id in whitelisted or message.author.id == self.bot.user.id:
            return
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission in Auto-Purge channel %s", message.channel.id)
        except discord.HTTPException:
            log.exception("Could not auto-purge bot message %s", message.id)


async def setup(bot):
    await bot.add_cog(AntiBot(bot))
