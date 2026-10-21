"""Per-server text autoresponders."""

import discord
from discord.ext import commands

from utils.emojis import icon
from utils.ui import component_card, panel
from utils.owner_permissions import owner_or_guild_permissions


class Autoresponder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def send_status(self, ctx: commands.Context, title: str, description: str, *, success: bool = False):
        status_icon = icon("whitetick") if success else icon("cross")
        embed = panel(f"{status_icon} {title}", description)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="autoresponder", aliases=("autoresponders",), invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def autoresponder(self, ctx: commands.Context):
        """Manage text autoresponders for this server."""
        await ctx.invoke(self.bot.get_command("help"), query="autoresponder")

    @autoresponder.command(name="create", case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def create(self, ctx: commands.Context, name: str, *, response: str):
        """Create an autoresponder: trigger name followed by the reply text."""
        name = name.strip()
        response = response.strip()
        if not name or len(name) > 80:
            return await self.send_status(ctx, "Invalid Trigger", "Use a trigger name between 1 and 80 characters.")
        if not response:
            return await self.send_status(ctx, "Empty Response", "The autoresponder message cannot be empty.")
        if len(response) > 2000:
            return await self.send_status(ctx, "Response Too Long", "Autoresponder messages must be 2,000 characters or fewer.")
        created = await self.bot.db.create_autoresponder(ctx.guild.id, name, response)
        if not created:
            return await self.send_status(ctx, "Autoresponder Already Exists", f"Autoresponder `{name}` already exists in {ctx.guild.name}.")
        await self.send_status(ctx, "Autoresponder Created", f"Created autoresponder `{name}` in {ctx.guild.name}.", success=True)

    @autoresponder.command(name="delete", case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def delete(self, ctx: commands.Context, *, name: str):
        """Delete an existing autoresponder."""
        deleted = await self.bot.db.delete_autoresponder(ctx.guild.id, name.strip())
        if not deleted:
            return await self.send_status(ctx, "Autoresponder Not Found", f"Autoresponder `{name.strip()}` was not found in {ctx.guild.name}.")
        await self.send_status(ctx, "Autoresponder Deleted", f"Deleted autoresponder `{name.strip()}` from {ctx.guild.name}.", success=True)

    @autoresponder.command(name="edit", case_insensitive=True)
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def edit(self, ctx: commands.Context, name: str, *, response: str):
        """Edit an existing autoresponder's reply text."""
        response = response.strip()
        if not response:
            return await self.send_status(ctx, "Empty Response", "The autoresponder message cannot be empty.")
        if len(response) > 2000:
            return await self.send_status(ctx, "Response Too Long", "Autoresponder messages must be 2,000 characters or fewer.")
        updated = await self.bot.db.edit_autoresponder(ctx.guild.id, name.strip(), response)
        if not updated:
            return await self.send_status(ctx, "Autoresponder Not Found", f"Autoresponder `{name.strip()}` was not found in {ctx.guild.name}.")
        await self.send_status(ctx, "Autoresponder Updated", f"Updated autoresponder `{name.strip()}` in {ctx.guild.name}.", success=True)

    @autoresponder.command(name="config", case_insensitive=True)
    @commands.guild_only()
    async def config(self, ctx: commands.Context):
        """List all autoresponders in this server."""
        responders = await self.bot.db.list_autoresponders(ctx.guild.id)
        if not responders:
            return await self.send_status(ctx, "No Autoresponders", f"There are no autoresponders in {ctx.guild.name}.")
        names = "\n".join(discord.utils.escape_markdown(item["name"]) for item in responders)
        embed = panel(f"Autoresponders in {ctx.guild.name}")
        embed.add_field(name=f"Autoresponder [{len(responders)}]", value=names[:1024], inline=False)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return
        responder = await self.bot.db.get_autoresponder(message.guild.id, message.content.strip())
        if responder:
            await message.channel.send(
                responder["response"],
                allowed_mentions=discord.AllowedMentions.none(),
                reference=message,
                mention_author=False,
            )


async def setup(bot):
    await bot.add_cog(Autoresponder(bot))
