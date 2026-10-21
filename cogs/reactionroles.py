"""Persistent reaction-role configurations."""

import discord
from discord.ext import commands

from utils.emojis import icon
from utils.ui import component_card, panel


def emoji_parts(raw: str) -> tuple[discord.PartialEmoji, str]:
    emoji = discord.PartialEmoji.from_str(raw.strip())
    key = f"custom:{emoji.id}" if emoji.id else f"unicode:{emoji.name}"
    return emoji, key


def payload_emoji_parts(payload: discord.RawReactionActionEvent) -> tuple[str, str]:
    emoji = payload.emoji
    key = f"custom:{emoji.id}" if emoji.id else f"unicode:{emoji.name}"
    return str(emoji), key


class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def respond(self, ctx, title: str, description: str):
        await ctx.send(
            view=component_card(panel(title, description)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="reactionrole", aliases=("reactionroles",), invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def reactionrole(self, ctx):
        """Configure reaction roles on messages."""
        await ctx.invoke(self.bot.get_command("help"), query="reactionrole")

    @reactionrole.command(name="add", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def add(self, ctx, message_id: int, emoji_text: str, role: discord.Role):
        """Add a reaction role to a message."""
        if role.is_default() or role.managed:
            return await self.respond(ctx, f"{icon('autorole_error')} Invalid Role", "Choose a normal role that Zyven can manage.")
        restricted = [name for name, enabled in role.permissions if enabled]
        if restricted:
            permissions = ", ".join(f"`{name}`" for name in restricted)
            return await self.respond(
                ctx,
                f"{icon('autorole_error')} Dangerous Permission",
                f"{icon('autorole_arrow')} Role {role.mention} has restricted permission: {permissions}",
            )
        if ctx.guild.me is None or role >= ctx.guild.me.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "Move Zyven's highest role above the reaction role first.")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "You can only configure reaction roles below your highest role.")
        try:
            message = await ctx.channel.fetch_message(message_id)
            if message.guild is None or message.guild.id != ctx.guild.id:
                return await self.respond(ctx, f"{icon('autorole_error')} Invalid Message", "That message is not in this server.")
            emoji, key = emoji_parts(emoji_text)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return await self.respond(ctx, f"{icon('autorole_error')} Invalid Message or Emoji", "Check the message ID and provide a valid emoji.")

        created = await self.bot.db.create_reaction_role(ctx.guild.id, message_id, key, str(emoji), role.id)
        if not created:
            return await self.respond(ctx, f"{icon('autorole_error')} Reaction Role Exists", "That message already has a role configured for this emoji.")
        try:
            await message.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            await self.bot.db.delete_reaction_role(ctx.guild.id, message_id, key)
            return await self.respond(ctx, f"{icon('autorole_error')} Could Not Add Reaction", "Give Zyven permission to view the message and add reactions, then try again.")

        await self.respond(
            ctx,
            f"{icon('autorole_success')} Reaction Role Added",
            f"{icon('autorole_dotmoving')} **__Message ID:__** `{message_id}`\n"
            f"{icon('autorole_dotmoving')} **__Emoji:__** {emoji}\n"
            f"{icon('autorole_dotmoving')} **__Role:__** {role.mention}\n\n"
            f"{icon('autorole_arrow')} Users can now react with {emoji} to receive {role.mention}.",
        )

    @reactionrole.command(name="remove", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def remove(self, ctx, message_id: int, emoji_text: str):
        """Remove a reaction role from a message."""
        emoji, key = emoji_parts(emoji_text)
        removed = await self.bot.db.delete_reaction_role(ctx.guild.id, message_id, key)
        if not removed:
            return await self.respond(ctx, f"{icon('autorole_error')} Reaction Role Not Found", "No reaction role matched that message and emoji.")
        await self.respond(ctx, f"{icon('autorole_success')} Reaction Role Removed", f"Removed the reaction role for `{message_id}` and {emoji}.")

    @reactionrole.command(name="list", case_insensitive=True)
    @commands.guild_only()
    async def list_roles(self, ctx):
        """List configured reaction roles in this server."""
        entries = await self.bot.db.list_reaction_roles(ctx.guild.id)
        if not entries:
            return await self.respond(ctx, f"{icon('autorole_error')} Configuration Empty", f"{icon('autorole_arrow')} No reaction roles configured in this server.")
        rows = []
        for item in entries:
            role = ctx.guild.get_role(item["role_id"])
            role_text = role.mention if role else f"Deleted role ({item['role_id']})"
            rows.append(
                f"{icon('autorole_dotmoving')} **__Message:__** `{item['message_id']}`\n"
                f"{icon('autorole_dotmoving')} **__Emoji:__** {item['emoji']} → {role_text}"
            )
        description = f"{icon('autorole_settings')} **__Total Count:__** `{len(entries)}`\n\n" + "\n\n".join(rows)
        await self.respond(ctx, "Reaction Role Configuration", description[:4000])

    @reactionrole.command(name="reset", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def reset(self, ctx):
        """Clear all reaction roles in this server."""
        count = await self.bot.db.clear_reaction_roles(ctx.guild.id)
        if not count:
            return await self.respond(ctx, f"{icon('autorole_error')} Configuration Empty", "There are no reaction roles to reset.")
        await self.respond(
            ctx,
            f"{icon('autorole_success')} Reset Successful",
            f"{icon('autorole_settings')} **__Status:__** `{count}` Reaction Roles has been successfully reset\n\n"
            f"{icon('autorole_arrow')} All reaction role configurations have been cleared.",
        )

    async def _apply_role(self, payload: discord.RawReactionActionEvent, add: bool):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        emoji, key = payload_emoji_parts(payload)
        config = await self.bot.db.get_reaction_role(payload.guild_id, payload.message_id, key)
        if not config:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        role = guild.get_role(config["role_id"])
        if role is None or role >= guild.me.top_role:
            return
        try:
            if add:
                await member.add_roles(role, reason=f"Reaction role {emoji}")
            elif role in member.roles:
                await member.remove_roles(role, reason=f"Reaction role removed {emoji}")
        except (discord.Forbidden, discord.HTTPException):
            return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._apply_role(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._apply_role(payload, False)


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
