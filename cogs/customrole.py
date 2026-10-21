"""Server setup and role-toggle commands used by custom role commands."""

import asyncio
import re
import time
from collections import defaultdict

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, panel
from utils.role_safety import dangerous_permissions


ROLE_TYPES = {"guest", "staff", "girl", "vip", "friend"}
COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$", re.IGNORECASE)


class CustomRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._config_cache: dict[int, tuple[float, dict]] = {}
        self._config_locks = defaultdict(asyncio.Lock)

    async def config(self, guild_id: int) -> dict:
        now = time.monotonic()
        cached = self._config_cache.get(guild_id)
        if cached and cached[0] > now:
            return dict(cached[1])
        async with self._config_locks[guild_id]:
            now = time.monotonic()
            cached = self._config_cache.get(guild_id)
            if cached and cached[0] > now:
                return dict(cached[1])
            value = (await self.bot.db.get_guild(guild_id)).get("customrole", {})
            value = dict(value) if isinstance(value, dict) else {}
            self._config_cache[guild_id] = (now + 30, value)
            return dict(value)

    def cache_config(self, guild_id: int, value: dict) -> None:
        self._config_cache[guild_id] = (time.monotonic() + 30, dict(value))

    async def respond(self, ctx, title: str, description: str):
        return await ctx.send(view=component_card(panel(title, description)), allowed_mentions=discord.AllowedMentions.none())

    async def edit_response(self, message: discord.Message, title: str, description: str):
        view = component_card(panel(title, description))
        try:
            return await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            # The work may have completed even if Discord expired/deleted the
            # progress message; try posting the final result as a new message.
            return await message.channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def set_role(self, ctx, key: str, role: discord.Role):
        risky = dangerous_permissions(role)
        bot_member = ctx.guild.me
        if role.is_default() or role.managed or risky:
            detail = ", ".join(name.replace("_", " ") for name in risky)
            message = f"This role has restricted permissions: {detail}." if risky else "Choose a normal, assignable server role."
            return await self.respond(ctx, f"{icon('autorole_error')} Role Cannot Be Configured", message)
        if bot_member is None or role >= bot_member.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "Move Zyven's highest role above this role first.")
        if ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id} and role >= ctx.author.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "You can only configure a role below your highest role.")
        cfg = await self.config(ctx.guild.id)
        if cfg.get(key) == role.id:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Already Configured", f"{role.mention} is already configured.")
        progress = await ctx.send(view=component_card(panel(f"{icon('loading')} Saving Role Setup", f"Saving {role.mention} as the {key} role…")), allowed_mentions=discord.AllowedMentions.none())
        try:
            await self.bot.db.set_nested(ctx.guild.id, "customrole", key, role.id)
        except Exception:
            return await self.edit_response(progress, f"{icon('autorole_error')} Role Setup Failed", "The database did not save this setting. Try again shortly.")
        cfg[key] = role.id
        self.cache_config(ctx.guild.id, cfg)
        if key == "reqrole":
            return await self.edit_response(
                progress,
                f"{icon('autorole_success')} Required Role Configured",
                f"{icon('autorole_settings')} Configured {role.mention} as required role for custom role commands\n\n"
                f"{icon('autorole_arrow')} Members need this role to use custom role commands",
            )
        if key == "guest":
            return await self.edit_response(
                progress,
                f"{icon('autorole_success')} Guest Role Configured",
                f"{icon('autorole_settings')} **__Usage:__**\n"
                f"{icon('autorole_dotmoving')} Use `{settings.prefix}guest <user>` to add/remove this role\n"
                f"{icon('autorole_dotmoving')} Use the command again to toggle the role\n\n"
                f"{icon('autorole_arrow')} Configured {role.mention} as Guest role",
            )
        await self.edit_response(progress, f"{icon('autorole_success')} {key.title()} Role Configured", f"Configured {role.mention} as {key.title()} role")

    @commands.group(name="setup", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def setup_group(self, ctx):
        """Configure custom role commands."""
        await ctx.invoke(self.bot.get_command("help"), query="setup")

    @setup_group.command(name="reqrole", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_reqrole(self, ctx, role: discord.Role):
        """Setup required role for custom role commands."""
        await self.set_role(ctx, "reqrole", role)

    @setup_group.command(name="guest", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_guest(self, ctx, role: discord.Role):
        """Setup guest role in the guild."""
        await self.set_role(ctx, "guest", role)

    @setup_group.command(name="staff", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_staff(self, ctx, role: discord.Role):
        """Setup staff role in the guild."""
        await self.set_role(ctx, "staff", role)

    @setup_group.command(name="girl", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_girl(self, ctx, role: discord.Role):
        """Setup girl role in the guild."""
        await self.set_role(ctx, "girl", role)

    @setup_group.command(name="vip", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_vip(self, ctx, role: discord.Role):
        """Setup VIP role in the guild."""
        await self.set_role(ctx, "vip", role)

    @setup_group.command(name="friend", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_friend(self, ctx, role: discord.Role):
        """Setup friend role in the guild."""
        await self.set_role(ctx, "friend", role)

    @setup_group.command(name="create", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_create(self, ctx, name: str, role: discord.Role):
        """Create a custom role command."""
        risky = dangerous_permissions(role)
        if role.is_default() or role.managed or risky:
            detail = ", ".join(name.replace("_", " ") for name in risky)
            message = f"This role has restricted permissions: {detail}." if risky else "Choose a normal, assignable server role."
            return await self.respond(ctx, f"{icon('autorole_error')} Role Cannot Be Configured", message)
        if ctx.guild.me is None or role >= ctx.guild.me.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "Move Zyven's highest role above this role first.")
        if ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id} and role >= ctx.author.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "You can only configure a role below your highest role.")
        name = name.casefold()
        if not COMMAND_NAME.fullmatch(name) or name in {"setup", "config", "list", "reset"}:
            return await self.respond(ctx, f"{icon('autorole_error')} Invalid Command Name", "Choose a command name with 2–32 letters, numbers, `_` or `-`.")
        cfg = await self.config(ctx.guild.id)
        custom = dict(cfg.get("commands", {}))
        if name in custom:
            return await self.respond(ctx, f"{icon('autorole_error')} Command Exists", f"The custom role command `{name}` already exists.")
        existing_command = self.bot.get_command(name)
        if existing_command and not isinstance(existing_command, commands.Group) and name not in ROLE_TYPES:
            return await self.respond(
                ctx,
                f"{icon('autorole_error')} Command Name Unavailable",
                f"`{name}` is already used by the built-in `{existing_command.qualified_name}` command. Choose another name.",
            )
        custom[name] = role.id
        progress = await ctx.send(view=component_card(panel(f"{icon('loading')} Creating Custom Command", f"Saving `{name}`…")), allowed_mentions=discord.AllowedMentions.none())
        try:
            await self.bot.db.set_nested(ctx.guild.id, "customrole", "commands", custom)
        except Exception:
            return await self.edit_response(progress, f"{icon('autorole_error')} Command Creation Failed", "The database did not save this command. Try again shortly.")
        cfg["commands"] = custom
        self.cache_config(ctx.guild.id, cfg)
        await self.edit_response(
            progress,
            f"{icon('autorole_success')} Custom Command Created",
            f"{icon('autorole_settings')} **__Usage:__**\n"
            f"{icon('autorole_dotmoving')} Use `{name} <user>` to assign/remove the role\n"
            f"{icon('autorole_dotmoving')} Requires the configured required role, or Manage Roles if no required role is set\n\n"
            f"{icon('autorole_arrow')} Created command `{name}` for {role.mention}",
        )

    @setup_group.command(name="delete", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_delete(self, ctx, *, name: str):
        """Delete a custom role command."""
        cfg = await self.config(ctx.guild.id)
        custom = dict(cfg.get("commands", {}))
        if custom.pop(name.casefold(), None) is None:
            return await self.respond(ctx, f"{icon('autorole_error')} Command Not Found", f"No custom role command named `{name}` exists.")
        progress = await ctx.send(view=component_card(panel(f"{icon('loading')} Deleting Custom Command", f"Removing `{name.casefold()}`…")), allowed_mentions=discord.AllowedMentions.none())
        try:
            await self.bot.db.set_nested(ctx.guild.id, "customrole", "commands", custom)
        except Exception:
            return await self.edit_response(progress, f"{icon('autorole_error')} Command Deletion Failed", "The database did not save this change. Try again shortly.")
        cfg["commands"] = custom
        self.cache_config(ctx.guild.id, cfg)
        await self.edit_response(progress, f"{icon('autorole_success')} Custom Role Command Deleted", f"Deleted `{name.casefold()}`.")

    @setup_group.command(name="list", case_insensitive=True)
    async def setup_list(self, ctx):
        """List all custom role commands."""
        cfg = await self.config(ctx.guild.id)
        custom = cfg.get("commands", {})
        if not custom:
            return await self.respond(ctx, f"{icon('autorole_error')} No Custom Commands", f"{icon('autorole_arrow')} No custom role commands have been created for this server")
        value = "\n".join(f"`{name}` — <@&{role_id}>" for name, role_id in sorted(custom.items()))
        await self.respond(ctx, f"{icon('customrole')} Custom Role Commands", value)

    @setup_group.command(name="config", case_insensitive=True)
    async def setup_config(self, ctx):
        """Show the current custom role configuration."""
        cfg = await self.config(ctx.guild.id)
        labels = (("Staff Role", "staff"), ("Girl Role", "girl"), ("VIP Role", "vip"), ("Guest Role", "guest"), ("Friend Role", "friend"), ("Required Role", "reqrole"))
        lines = []
        for label, key in labels:
            role = ctx.guild.get_role(cfg.get(key, 0)) if cfg.get(key) else None
            lines.append(f"**__{label}__**\n{role.mention if role else '`Not configured`'}")
        await self.respond(ctx, f"{icon('customrole')} Custom Role Configuration", "\n\n".join(lines))

    @setup_group.command(name="reset", case_insensitive=True)
    @commands.has_guild_permissions(manage_roles=True)
    async def setup_reset(self, ctx):
        """Reset all custom role configuration."""
        progress = await ctx.send(view=component_card(panel(f"{icon('loading')} Resetting Custom Role Setup", "Clearing the saved custom role settings…")), allowed_mentions=discord.AllowedMentions.none())
        try:
            await self.bot.db.set_value(ctx.guild.id, "customrole", {})
        except Exception:
            return await self.edit_response(progress, f"{icon('autorole_error')} Reset Failed", "The database did not save this change. Try again shortly.")
        self.cache_config(ctx.guild.id, {})
        await self.edit_response(progress, f"{icon('autorole_success')} Custom Role Configuration Reset", "All custom role configuration and commands were cleared.")

    async def toggle_role(self, ctx, member: discord.Member, role_key: str, role_id: int | None = None):
        cfg = await self.config(ctx.guild.id)
        required = ctx.guild.get_role(cfg.get("reqrole", 0)) if cfg.get("reqrole") else None
        if required is not None and required not in ctx.author.roles and ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id}:
            return await self.respond(ctx, f"{icon('autorole_error')} Required Role Required", f"{icon('autorole_arrow')} You need {required.mention} to use custom role commands")
        if required is None and ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id} and not ctx.author.guild_permissions.manage_roles:
            return await self.respond(ctx, f"{icon('autorole_error')} Required Role Not Set", f"{icon('autorole_arrow')} Ask a moderator to run `{settings.prefix}setup reqrole <role>` or use Manage Roles")
        selected_id = role_id if role_id is not None else cfg.get(role_key)
        role = ctx.guild.get_role(selected_id) if selected_id else None
        if role is None:
            return await self.respond(ctx, f"{icon('autorole_error')} {role_key.title()} Role Not Set", f"Configure it first using `{settings.prefix}setup {role_key} <role>`")
        risky = dangerous_permissions(role)
        if role.is_default() or role.managed or risky:
            detail = ", ".join(name.replace("_", " ") for name in risky)
            message = f"This role has restricted permissions: {detail}." if risky else "This role cannot be assigned. Configure a normal, assignable role."
            return await self.respond(ctx, f"{icon('autorole_error')} Role Cannot Be Assigned", message)
        bot_member = ctx.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or role >= bot_member.top_role:
            return await self.respond(ctx, f"{icon('autorole_error')} Role Hierarchy Error", "Zyven needs Manage Roles and its highest role must be above this role.")
        try:
            progress = await ctx.send(
                view=component_card(panel(
                    f"{icon('loading')} Updating Role",
                    f"Checking role update for {member.mention}…",
                )),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if role in member.roles:
                await member.remove_roles(role, reason=f"{ctx.author} toggled custom role {role.name}")
                await self.edit_response(progress, f"{icon('autorole_success')} Role Removed", f"Removed {role.mention} from {member.mention}.")
            else:
                await member.add_roles(role, reason=f"{ctx.author} toggled custom role {role.name}")
                await self.edit_response(progress, f"{icon('autorole_success')} Role Added", f"Added {role.mention} to {member.mention}.")
        except discord.Forbidden:
            if "progress" in locals():
                await self.edit_response(progress, f"{icon('autorole_error')} Missing Permissions", "I cannot manage that role. Move my highest role above it and check Manage Roles permission.")
            else:
                await self.respond(ctx, f"{icon('autorole_error')} Missing Permissions", "I cannot manage that role. Move my highest role above it and check Manage Roles permission.")
        except discord.HTTPException as error:
            message = f"Discord could not update this role (error {getattr(error, 'code', 'unknown')}). Check role hierarchy and try again."
            if "progress" in locals():
                await self.edit_response(progress, f"{icon('autorole_error')} Role Update Failed", message)
            else:
                await self.respond(ctx, f"{icon('autorole_error')} Role Update Failed", message)

    @commands.command(name="guest", case_insensitive=True)
    @commands.guild_only()
    async def guest(self, ctx, member: discord.Member):
        await self.toggle_role(ctx, member, "guest")

    @commands.command(name="staff", case_insensitive=True)
    @commands.guild_only()
    async def staff(self, ctx, member: discord.Member):
        await self.toggle_role(ctx, member, "staff")

    @commands.command(name="girl", case_insensitive=True)
    @commands.guild_only()
    async def girl(self, ctx, member: discord.Member):
        await self.toggle_role(ctx, member, "girl")

    @commands.command(name="vip", case_insensitive=True)
    @commands.guild_only()
    async def vip(self, ctx, member: discord.Member):
        await self.toggle_role(ctx, member, "vip")

    @commands.command(name="friend", case_insensitive=True)
    @commands.guild_only()
    async def friend(self, ctx, member: discord.Member):
        await self.toggle_role(ctx, member, "friend")

    async def custom_request(self, ctx):
        """Return a guild custom-role invocation, respecting real group subcommands."""
        if ctx.guild is None or ctx.prefix is None:
            return None
        if ctx.prefix == "" and not await self.bot.has_noprefix_access(ctx.author.id):
            return None
        content = ctx.message.content[len(ctx.prefix):].strip()
        command_name, _, target_text = content.partition(" ")
        if not command_name:
            return None
        custom = (await self.config(ctx.guild.id)).get("commands", {})
        role_id = custom.get(command_name.casefold())
        if not role_id:
            return None
        existing = self.bot.get_command(command_name.casefold())
        if existing is not None:
            if not isinstance(existing, commands.Group) and command_name.casefold() not in ROLE_TYPES:
                return None
            if isinstance(existing, commands.Group):
                subcommand = target_text.split(maxsplit=1)[0].casefold() if target_text else ""
                if subcommand in existing.all_commands:
                    return None
                # Keep the built-in group's own no-argument help behavior.
                if not target_text:
                    return None
        return command_name.casefold(), target_text.strip(), int(role_id)

    async def should_override_command(self, ctx) -> bool:
        return await self.custom_request(ctx) is not None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return
        ctx = await self.bot.get_context(message)
        request = await self.custom_request(ctx)
        if request is None:
            return
        command_name, target_text, role_id = request
        if not target_text:
            return await self.respond(ctx, f"{icon('autorole_error')} Member Required", f"Use `{ctx.prefix}{command_name} <user>`.")
        try:
            member = await commands.MemberConverter().convert(ctx, target_text)
        except commands.BadArgument:
            return await self.respond(ctx, f"{icon('autorole_error')} Member Not Found", "Mention a server member to toggle the role.")
        await self.toggle_role(ctx, member, command_name, role_id)


async def setup(bot):
    await bot.add_cog(CustomRole(bot))
