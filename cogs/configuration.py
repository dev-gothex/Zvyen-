import discord
from discord import app_commands
from discord.ext import commands

from core.settings import settings
from utils.emojis import icon
from utils.ui import ConfigHome, can_manage, component_card, panel, send_component_card, with_links, send_card


class Configuration(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="config", description="Open the interactive Zyven server setup panel")
    @app_commands.guild_only()
    async def config(self, interaction: discord.Interaction):
        if not can_manage(interaction, settings.owner_id):
            await send_card(interaction, panel(f"{icon('warning')} Permission Required", "You need **Manage Server** to use this configuration panel.", color=0xED4245), ephemeral=True)
            return
        embed = panel(f"{icon('security')} Zyven Configuration", "Select a category below to configure your server.")
        embed.add_field(name="Setup Categories", value="Security • Member Setup • VoiceMaster", inline=False)
        with_links(embed)
        await send_component_card(interaction, embed, ConfigHome(self.bot, interaction.guild_id, settings.owner_id), ephemeral=True)

    @commands.command(name="config")
    @commands.guild_only()
    async def prefix_config(self, ctx: commands.Context):
        if not settings.is_owner(ctx.author.id) and not ctx.author.guild_permissions.manage_guild:
            await ctx.reply(view=component_card(panel(f"{icon('warning')} Permission Required", "You need **Manage Server** to use this configuration panel.", color=0xED4245)), mention_author=False)
            return
        embed = panel(f"{icon('security')} Zyven Configuration", "Select a category below to configure your server.")
        embed.add_field(name="Setup Categories", value="Security • Member Setup • VoiceMaster", inline=False)
        with_links(embed)
        await ctx.send(view=component_card(embed, ConfigHome(self.bot, ctx.guild.id, settings.owner_id)))


async def setup(bot):
    await bot.add_cog(Configuration(bot))
