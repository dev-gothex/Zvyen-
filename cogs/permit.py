"""Permit module: give roles access to bot commands without Discord permissions.

Commands
--------
``permit <role>``          open the permit panel for a role (add / remove permissions)
``permit reset <role>``    clear a role's permits (and its owner-role status)
``permit reset all``       clear every role's permits and all owner roles
``permit owner <role>``    mark a role as an owner role (immune from commands)

How the grants take effect is documented in ``utils/permit.py``.
"""

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

from core.settings import settings
from utils.emojis import icon
from utils.permit import (
    PERMIT_PERMISSIONS,
    PermitConfig,
    dump_config,
    load_config,
)
from utils.ui import (
    component_card,
    edit_original_component_card,
    notice_panel,
    panel,
    send_card,
)

log = logging.getLogger("zyven.permit")

DENIED_TEXT = "Only the server owner or an Extraowner can manage permits."


async def can_manage_permits(bot, guild: discord.Guild, user_id: int) -> bool:
    """Server owner, bot owners and trusted Extraowners may change permits."""
    if user_id == guild.owner_id or settings.is_owner(user_id):
        return True
    security = (await bot.db.get_guild(guild.id)).get("security", {}) or {}
    trusted = set()
    for value in security.get("extraowners", []) or []:
        try:
            trusted.add(int(value))
        except (TypeError, ValueError):
            continue
    return user_id in trusted


class PermitConfigView(discord.ui.View):
    """Permission picker for one role (same layout as the whitelist panel)."""

    def __init__(self, cog: "Permit", guild_id: int, role: discord.Role, granted: set[str], is_owner_role: bool = False):
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.role = cog, guild_id, role
        self.granted = set(granted)
        self.is_owner_role = is_owner_role
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in PERMIT_PERMISSIONS
        ]
        self.add_item(PermitPermissionSelect(self, options))

    def make_embed(self) -> discord.Embed:
        lines = []
        for key, label in PERMIT_PERMISSIONS:
            enabled = key in self.granted
            pair = f"{icon('disableno')}{icon('enabledyes')}" if enabled else f"{icon('disabledyes')}{icon('enableno')}"
            lines.append(f"> {pair} : **{label}**")
        embed = panel("Permit Configuration for Current Server", "\n".join(lines))
        embed.add_field(name="Target", value=f"> {self.role.mention}", inline=False)
        if self.is_owner_role:
            embed.add_field(name="Owner Role", value="> Immune from commands", inline=False)
        icon_asset = getattr(self.role, "icon", None)
        if icon_asset is not None:
            embed.set_thumbnail(url=icon_asset.url)
        embed.set_footer(text="Powered By Zyven Development")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not await can_manage_permits(self.cog.bot, interaction.guild, interaction.user.id):
            await send_card(interaction, notice_panel(DENIED_TEXT, title="Access Denied"), ephemeral=True)
            return False
        return True

    async def save(self, interaction: discord.Interaction, selected: list[str]):
        await interaction.response.defer()
        granted = await self.cog.set_role_permissions(self.guild_id, self.role.id, selected)
        view = PermitConfigView(self.cog, self.guild_id, self.role, granted, self.is_owner_role)
        await edit_original_component_card(interaction, view.make_embed(), view)

    async def reset_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.reset_roles(self.guild_id, [self.role.id])
        view = PermitConfigView(self.cog, self.guild_id, self.role, set(), False)
        await edit_original_component_card(interaction, view.make_embed(), view)

    @discord.ui.button(label="Grant All Permissions", style=discord.ButtonStyle.success, row=1)
    async def grant_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, [key for key, _ in PERMIT_PERMISSIONS])

    @discord.ui.button(label="Remove Permissions", style=discord.ButtonStyle.danger, row=1)
    async def remove_permissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, [])

    @discord.ui.button(emoji=icon("delete"), style=discord.ButtonStyle.danger, row=2)
    async def delete_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.reset_role(interaction)


class PermitPermissionSelect(discord.ui.Select):
    def __init__(self, parent: PermitConfigView, options: list[discord.SelectOption]):
        self.parent_view = parent
        super().__init__(
            placeholder="Select permissions to toggle",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        # Picking a permission flips it: granted -> removed, missing -> granted.
        view = self.parent_view
        await view.save(interaction, sorted(view.granted.symmetric_difference(self.values)))


class Permit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- storage ----------------------------------------------------------

    async def _write(self, guild_id: int, config: PermitConfig) -> None:
        await self.bot.db.set_value(guild_id, "permit", dump_config(config))

    async def set_role_permissions(self, guild_id: int, role_id: int, keys: list[str]) -> set[str]:
        valid = {key for key, _ in PERMIT_PERMISSIONS}
        chosen = frozenset(key for key in keys if key in valid)
        async with self._locks[guild_id]:
            config = await load_config(self.bot, guild_id)
            roles = dict(config.roles)
            if chosen:
                roles[role_id] = chosen
            else:
                roles.pop(role_id, None)
            await self._write(guild_id, PermitConfig(roles=roles, owner_roles=config.owner_roles))
        return set(chosen)

    async def reset_roles(self, guild_id: int, role_ids: list[int]) -> bool:
        """Clear permits and owner status for ``role_ids``. True if anything changed."""
        async with self._locks[guild_id]:
            config = await load_config(self.bot, guild_id)
            roles = {rid: keys for rid, keys in config.roles.items() if rid not in role_ids}
            owners = frozenset(rid for rid in config.owner_roles if rid not in role_ids)
            changed = len(roles) != len(config.roles) or len(owners) != len(config.owner_roles)
            if changed:
                await self._write(guild_id, PermitConfig(roles=roles, owner_roles=owners))
            return changed

    async def reset_everything(self, guild_id: int) -> int:
        async with self._locks[guild_id]:
            config = await load_config(self.bot, guild_id)
            count = len(set(config.roles) | set(config.owner_roles))
            if count:
                await self._write(guild_id, PermitConfig())
            return count

    # -- helpers ----------------------------------------------------------

    async def _authorize(self, ctx: commands.Context) -> bool:
        if await can_manage_permits(self.bot, ctx.guild, ctx.author.id):
            return True
        await ctx.send(
            view=component_card(notice_panel(DENIED_TEXT, title="Access Denied")),
            allowed_mentions=discord.AllowedMentions.none(),
            delete_after=5,
        )
        return False

    async def _valid_role(self, ctx: commands.Context, role: discord.Role) -> bool:
        if role.is_default():
            await ctx.send(
                view=component_card(notice_panel("The @everyone role cannot be used with permit.")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _reply(self, ctx: commands.Context, title: str, text: str):
        await ctx.send(view=component_card(panel(title, text)), allowed_mentions=discord.AllowedMentions.none())

    # -- commands ---------------------------------------------------------

    @commands.group(name="permit", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def permit(self, ctx: commands.Context, role: discord.Role | None = None):
        """Add or remove command permissions for a role."""
        if role is None:
            help_command = self.bot.get_command("help")
            await ctx.invoke(help_command, query="permit")
            return
        if not await self._authorize(ctx) or not await self._valid_role(ctx, role):
            return
        config = await load_config(self.bot, ctx.guild.id)
        view = PermitConfigView(self, ctx.guild.id, role, set(config.roles.get(role.id, ())), role.id in config.owner_roles)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @permit.group(name="reset", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def permit_reset(self, ctx: commands.Context, role: discord.Role | None = None):
        """Reset the permissions of a role."""
        if role is None:
            help_command = self.bot.get_command("help")
            await ctx.invoke(help_command, query="permit")
            return
        if not await self._authorize(ctx) or not await self._valid_role(ctx, role):
            return
        if await self.reset_roles(ctx.guild.id, [role.id]):
            await self._reply(ctx, f"{icon('tick')} Success", f"Reset all permit settings for {role.mention}.")
        else:
            await self._reply(ctx, f"{icon('error')} Nothing to Reset", f"{role.mention} has no permit settings.")

    @permit_reset.command(name="all")
    @commands.guild_only()
    async def permit_reset_all(self, ctx: commands.Context):
        """Reset the permissions of all roles."""
        if not await self._authorize(ctx):
            return
        count = await self.reset_everything(ctx.guild.id)
        if count:
            await self._reply(ctx, f"{icon('tick')} Success", f"Reset permit settings for **{count}** role(s).")
        else:
            await self._reply(ctx, f"{icon('error')} Nothing to Reset", "No roles have permit settings in this server.")

    @permit.command(name="owner")
    @commands.guild_only()
    async def permit_owner(self, ctx: commands.Context, role: discord.Role):
        """Set a role as owner role (owner role is immune from commands)."""
        if not await self._authorize(ctx) or not await self._valid_role(ctx, role):
            return
        async with self._locks[ctx.guild.id]:
            config = await load_config(self.bot, ctx.guild.id)
            already = role.id in config.owner_roles
            if not already:
                await self._write(ctx.guild.id, PermitConfig(roles=config.roles, owner_roles=config.owner_roles | {role.id}))
        if already:
            await self._reply(
                ctx,
                f"{icon('error')} Already an Owner Role",
                f"{role.mention} is already an owner role. Use `{ctx.clean_prefix}permit reset {role.id}` to remove it.",
            )
        else:
            await self._reply(
                ctx,
                f"{icon('tick')} Success",
                f"{role.mention} is now an owner role. Members with it are immune from moderation commands.",
            )


async def setup(bot):
    await bot.add_cog(Permit(bot))
