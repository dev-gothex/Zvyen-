"""Jail setup and moderation commands."""
import logging

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, edit_component_card, panel, send_notice

log = logging.getLogger("zyven.jail")


def jail_config_embed(guild: discord.Guild, data: dict, note: str | None = None) -> discord.Embed:
    config = data.get("jail", {})
    role = guild.get_role(config.get("role_id")) if config.get("role_id") else None
    channel = guild.get_channel(config.get("channel_id")) if config.get("channel_id") else None
    embed = panel("Configure Jail", "Manage the jail role and the only channel jailed members can access.")
    embed.add_field(name="Current Status", value="Enabled" if role and channel else "Setup Required", inline=False)
    embed.add_field(name="Role", value=role.mention if role else "Not Set", inline=True)
    embed.add_field(name="Jail Channel", value=channel.mention if channel else "Not Set", inline=True)
    if note:
        embed.add_field(name="Status", value=note, inline=False)
    embed.set_footer(text="The jail role is denied View Channel and Send Messages everywhere except the selected jail channel.")
    return embed


def jail_loading_embed(title: str, description: str) -> discord.Embed:
    return panel(f"{icon('loading')} {title}", description)


def jail_actor(ctx: commands.Context) -> bool:
    member = ctx.author
    return member.guild_permissions.administrator


async def apply_jail_permissions(bot, guild: discord.Guild, role: discord.Role, jail_channel_id: int) -> int:
    """Deny visibility/posting to the jail role across channels, except its room."""
    config = (await bot.db.get_guild(guild.id)).get("jail", {})
    jail_category_id = config.get("category_id")
    jail_category = guild.get_channel(jail_category_id) if jail_category_id else None
    default_role = guild.default_role
    saved = dict(config.get("original_overwrites", {}))
    default_saved = dict(config.get("original_default_overwrites", {}))
    for channel in guild.channels:
        key = str(channel.id)
        if key not in saved:
            before = channel.overwrites_for(role)
            saved[key] = {
                "view_channel": before.view_channel,
                "send_messages": before.send_messages,
                "send_messages_in_threads": before.send_messages_in_threads,
            }
    private_ids = {jail_channel_id}
    if isinstance(jail_category, discord.CategoryChannel) and (
        config.get("owned_category_id") or config.get("category_created")
    ):
        private_ids.add(jail_category.id)
    for channel_id in private_ids:
        channel = guild.get_channel(channel_id)
        if channel is not None and str(channel_id) not in default_saved:
            before = channel.overwrites_for(default_role)
            default_saved[str(channel_id)] = {
                "view_channel": before.view_channel,
                "send_messages": before.send_messages,
                "send_messages_in_threads": before.send_messages_in_threads,
            }
    await bot.db.set_nested(guild.id, "jail", "original_default_overwrites", default_saved)
    await bot.db.set_nested(guild.id, "jail", "original_overwrites", saved)

    # If setup moved to a different room/category, restore the old @everyone
    # permissions before restricting the new jail location.
    for key in tuple(default_saved):
        if int(key) in private_ids:
            continue
        channel = guild.get_channel(int(key))
        if channel is not None:
            overwrite = channel.overwrites_for(default_role)
            previous = default_saved[key]
            for name in ("view_channel", "send_messages", "send_messages_in_threads"):
                setattr(overwrite, name, previous.get(name))
            await channel.set_permissions(default_role, overwrite=overwrite, reason="Restore previous jail room visibility")
        default_saved.pop(key, None)
    await bot.db.set_nested(guild.id, "jail", "original_default_overwrites", default_saved)

    for channel_id in private_ids:
        channel = guild.get_channel(channel_id)
        if channel is None:
            continue
        overwrite = channel.overwrites_for(default_role)
        overwrite.view_channel = False
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        await channel.set_permissions(default_role, overwrite=overwrite, reason="Hide jail room from everyone")

    changed = 0
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        is_jail_room = channel.id == jail_channel_id
        is_jail_category = channel.id == jail_category_id
        overwrite.view_channel = is_jail_room or is_jail_category
        overwrite.send_messages = is_jail_room
        overwrite.send_messages_in_threads = is_jail_room
        await channel.set_permissions(role, overwrite=overwrite, reason="Apply jail role channel restrictions")
        changed += 1
    return changed


async def restore_jail_permissions(guild: discord.Guild, role: discord.Role, saved: dict) -> int:
    restored = 0
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        previous = saved.get(str(channel.id), {})
        for name in ("view_channel", "send_messages", "send_messages_in_threads"):
            setattr(overwrite, name, previous.get(name))
        await channel.set_permissions(role, overwrite=overwrite, reason="Reset jail role channel restrictions")
        restored += 1
    return restored


async def restore_default_overwrites(guild: discord.Guild, saved: dict) -> int:
    restored = 0
    for channel_id, previous in saved.items():
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            continue
        overwrite = channel.overwrites_for(guild.default_role)
        for name in ("view_channel", "send_messages", "send_messages_in_threads"):
            setattr(overwrite, name, previous.get(name))
        await channel.set_permissions(guild.default_role, overwrite=overwrite, reason="Restore jail room visibility")
        restored += 1
    return restored


class JailRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent: "JailSetupView"):
        super().__init__(placeholder="Select Jail Role", min_values=1, max_values=1, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        if role.is_default() or role.managed or role.permissions.administrator:
            await send_notice(interaction, "Choose a normal, assignable role without Administrator permission.")
            return
        bot_member = self.parent_view.guild.me
        if bot_member is None or role >= bot_member.top_role:
            await send_notice(interaction, "Choose a jail role below Zyven's highest role.")
            return
        current_role_id = self.parent_view.config.get("role_id")
        if current_role_id and current_role_id != role.id:
            await send_notice(interaction, "Reset the current jail setup before changing its role, so the original channel permissions can be restored safely.")
            return
        await edit_component_card(
            interaction,
            jail_loading_embed("Setting Up Jail", "Creating the jail role and channel, then applying channel restrictions…"),
            self,
        )
        self.parent_view.config["role_id"] = role.id
        self.parent_view.config["role_created"] = bool(self.parent_view.config.get("role_created", False))
        await self.parent_view.bot.db.set_nested(self.parent_view.guild.id, "jail", "role_id", role.id)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild.id, "jail", "role_created", self.parent_view.config["role_created"])
        jail_cog = self.parent_view.bot.get_cog("Jail")
        if jail_cog:
            jail_cog._jail_role_cache[self.parent_view.guild.id] = role.id
        note = "Jail role saved. Select a jail channel, then apply permissions."
        channel = self.parent_view.guild.get_channel(self.parent_view.config.get("channel_id")) if self.parent_view.config.get("channel_id") else None
        if channel:
            try:
                count = await apply_jail_permissions(self.parent_view.bot, self.parent_view.guild, role, channel.id)
                note = f"Jail role saved. Restrictions applied to {count} channels; only {channel.mention} is open."
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not apply jail role permissions in guild %s", self.parent_view.guild.id)
                note = "Role saved, but channel permissions could not be fully applied. Check Zyven's permissions and retry."
        await edit_component_card(interaction, jail_config_embed(self.parent_view.guild, {"jail": self.parent_view.config}, note), self.parent_view)


class JailChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: "JailSetupView"):
        super().__init__(placeholder="Select Jail Channel", channel_types=[discord.ChannelType.text], min_values=1, max_values=1, row=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await edit_component_card(
            interaction,
            jail_loading_embed("Applying Jail Permissions", "Updating channel overrides across this server…"),
            self,
        )
        previous_channel_id = self.parent_view.config.get("channel_id")
        self.parent_view.config["channel_id"] = channel.id
        if channel.id != previous_channel_id:
            self.parent_view.config["channel_created"] = False
        category = channel.category
        if category:
            self.parent_view.config["category_id"] = category.id
        else:
            self.parent_view.config.pop("category_id", None)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild.id, "jail", "channel_id", channel.id)
        if channel.id != previous_channel_id:
            await self.parent_view.bot.db.set_nested(self.parent_view.guild.id, "jail", "channel_created", False)
        if category:
            await self.parent_view.bot.db.set_nested(self.parent_view.guild.id, "jail", "category_id", category.id)
        else:
            await self.parent_view.bot.db.unset_value(self.parent_view.guild.id, "jail.category_id")
        role = self.parent_view.guild.get_role(self.parent_view.config.get("role_id")) if self.parent_view.config.get("role_id") else None
        note = "Jail channel saved. Apply permissions to restrict the jail role." if role else "Jail channel saved. Create or select a jail role."
        if role:
            try:
                count = await apply_jail_permissions(self.parent_view.bot, self.parent_view.guild, role, channel.id)
                note = f"Jail channel saved. Restrictions applied to {count} channels; only {channel.mention} is open."
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not apply jail channel permissions in guild %s", self.parent_view.guild.id)
                note = "Channel saved, but permissions could not be fully applied. Check Zyven's permissions and retry."
        await edit_component_card(interaction, jail_config_embed(self.parent_view.guild, {"jail": self.parent_view.config}, note), self.parent_view)


class JailSetupView(discord.ui.View):
    def __init__(self, bot, guild: discord.Guild, config: dict, requester_id: int):
        super().__init__(timeout=300)
        self.bot, self.guild = bot, guild
        self.requester_id = requester_id
        self.config = dict(config)
        self.add_item(JailRoleSelect(self))
        self.add_item(JailChannelSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if user.id != self.requester_id:
            await send_notice(interaction, "Only the admin who opened this jail setup can use its controls.", title="Access Denied")
            return False
        if user.guild_permissions.administrator:
            return True
        await send_notice(interaction, "You need the **Administrator** permission to configure jail.", title="Access Denied")
        return False

    async def refresh(self, interaction: discord.Interaction, note: str):
        await edit_component_card(interaction, jail_config_embed(self.guild, {"jail": self.config}, note), self)

    @discord.ui.button(label="Create Role + Channel", style=discord.ButtonStyle.success, row=2)
    async def create_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_member = self.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or not bot_member.guild_permissions.manage_channels:
            await send_notice(interaction, "Zyven needs **Manage Roles** and **Manage Channels** to create the jail setup.")
            return

        role = self.guild.get_role(self.config.get("role_id")) if self.config.get("role_id") else None
        channel = self.guild.get_channel(self.config.get("channel_id")) if self.config.get("channel_id") else None
        legacy_created_channel = (
            isinstance(channel, discord.TextChannel)
            and channel.topic == "Private channel for jailed members"
            and not self.guild.get_channel(self.config.get("category_id"))
        )
        if role and channel and not legacy_created_channel:
            await send_notice(interaction, "The jail role and channel are already configured. Use **Apply Permissions** to reapply channel restrictions.")
            return

        await edit_component_card(
            interaction,
            jail_loading_embed("Setting Up Jail", "Creating the jail role and channel, then applying channel restrictions…"),
            self,
        )
        created_role = created_channel = False
        try:
            if role is None:
                role = await self.guild.create_role(
                    name="Jailed",
                    permissions=discord.Permissions.none(),
                    reason=f"Jail role created by {interaction.user}",
                )
                created_role = True
                self.config["role_id"] = role.id
                self.config["role_created"] = True
                await self.bot.db.set_nested(self.guild.id, "jail", "role_id", role.id)
                await self.bot.db.set_nested(self.guild.id, "jail", "role_created", True)
                jail_cog = self.bot.get_cog("Jail")
                if jail_cog:
                    jail_cog._jail_role_cache[self.guild.id] = role.id

            category = self.guild.get_channel(self.config.get("category_id")) if self.config.get("category_id") else None
            if category is None and (channel is None or legacy_created_channel):
                category = await self.guild.create_category(
                    name="Jail",
                    overwrites={
                        self.guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
                        self.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True),
                    },
                    reason=f"Jail category created by {interaction.user}",
                )
                self.config["category_id"] = category.id
                self.config["category_created"] = True
                await self.bot.db.set_nested(self.guild.id, "jail", "category_id", category.id)
                await self.bot.db.set_nested(self.guild.id, "jail", "category_created", True)
                await self.bot.db.set_nested(self.guild.id, "jail", "owned_category_id", category.id)

            if channel is not None and legacy_created_channel and category is not None:
                await channel.edit(category=category, reason=f"Moved jail channel into its category by {interaction.user}")

            if channel is None:
                channel = await self.guild.create_text_channel(
                    name="jail",
                    category=category,
                    topic="Private channel for jailed members",
                    reason=f"Jail channel created by {interaction.user}",
                )
                created_channel = True
                self.config["channel_id"] = channel.id
                self.config["channel_created"] = True
                await self.bot.db.set_nested(self.guild.id, "jail", "channel_id", channel.id)
                await self.bot.db.set_nested(self.guild.id, "jail", "channel_created", True)
                await self.bot.db.set_nested(self.guild.id, "jail", "owned_channel_id", channel.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not create jail role/channel in guild %s", self.guild.id)
            partial = "The role was created; retry to create the channel." if created_role and not created_channel else ""
            await self.refresh(interaction, f"Could not finish creating the jail setup. Check Zyven's role and channel permissions. {partial}".strip())
            return

        try:
            count = await apply_jail_permissions(self.bot, self.guild, role, channel.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not apply new jail setup permissions in guild %s", self.guild.id)
            await self.refresh(interaction, f"{role.mention} and {channel.mention} are ready, but permissions could not be applied everywhere. Check Zyven's permissions, then use **Apply Permissions**.")
            return
        created = "Created the jail role and channel." if created_role and created_channel else "Jail setup is ready."
        await self.refresh(interaction, f"{created} Restrictions applied to {count} channels; only {channel.mention} allows the jail role to view and send messages.")

    @discord.ui.button(label="Apply Permissions", style=discord.ButtonStyle.primary, row=2)
    async def apply_permissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = self.guild.get_role(self.config.get("role_id")) if self.config.get("role_id") else None
        channel = self.guild.get_channel(self.config.get("channel_id")) if self.config.get("channel_id") else None
        if not role or not channel:
            await send_notice(interaction, "Select or create a jail role and select a jail channel first.")
            return
        bot_member = self.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels or not bot_member.guild_permissions.manage_roles:
            await send_notice(interaction, "Zyven needs **Manage Channels** and **Manage Roles** to apply jail permissions.")
            return
        await edit_component_card(
            interaction,
            jail_loading_embed("Applying Jail Permissions", "Updating channel overrides across this server…"),
            self,
        )
        try:
            count = await apply_jail_permissions(self.bot, self.guild, role, channel.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not fully apply jail permissions in guild %s", self.guild.id)
            await self.refresh(interaction, "Permission update stopped early. Check Zyven's channel permissions, then apply again.")
            return
        await self.refresh(interaction, f"Jail access applied to {count} channels. Only {channel.mention} allows viewing and sending messages.")


class Jail(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._jail_role_cache: dict[int, int | None] = {}

    async def _configured_jail_role(self, guild: discord.Guild) -> discord.Role | None:
        if guild.id not in self._jail_role_cache:
            data = await self.bot.db.get_guild(guild.id)
            config = data.get("jail", {})
            self._jail_role_cache[guild.id] = int(config["role_id"]) if config.get("role_id") else None
        role_id = self._jail_role_cache[guild.id]
        return guild.get_role(role_id) if role_id else None

    async def _enforce_jail_role(self, member: discord.Member, role: discord.Role, reason: str):
        bot_member = member.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or role >= bot_member.top_role:
            log.warning("Cannot enforce jail for member %s in guild %s: jail role is not manageable", member.id, member.guild.id)
            return
        protected = [
            current for current in member.roles
            if current not in {member.guild.default_role, role}
            and (current.managed or current >= bot_member.top_role)
        ]
        desired = [role, *protected]
        if {r.id for r in desired} == {r.id for r in member.roles if r != member.guild.default_role}:
            return
        await member.edit(roles=desired, reason=reason)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = await self.bot.db.get_guild(member.guild.id)
        config = data.get("jail", {})
        jailed_members = config.get("members", {}) or {}
        if str(member.id) not in jailed_members and member.id not in jailed_members:
            return
        role_id = config.get("role_id")
        role = member.guild.get_role(int(role_id)) if role_id else None
        if role is None:
            log.warning("Jailed member %s rejoined guild %s but its jail role is missing", member.id, member.guild.id)
            return
        self._jail_role_cache[member.guild.id] = role.id
        try:
            await self._enforce_jail_role(member, role, "Reapplied jail after member rejoined")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not reapply jail to rejoining member %s in guild %s", member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        role = await self._configured_jail_role(after.guild)
        if role is None or role not in after.roles:
            return
        try:
            await self._enforce_jail_role(after, role, "Removed roles added to a jailed member")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not remove unauthorized roles from jailed member %s in guild %s", after.id, after.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Keep the jail role restricted when a server adds a channel later."""
        data = await self.bot.db.get_guild(channel.guild.id)
        config = data.get("jail", {})
        role = channel.guild.get_role(config.get("role_id")) if config.get("role_id") else None
        jail_channel_id = config.get("channel_id")
        if not role or not jail_channel_id:
            return
        try:
            await apply_jail_permissions(self.bot, channel.guild, role, jail_channel_id)
        except discord.NotFound:
            # Discord may deliver a channel-create event while that channel is
            # being removed; permission edits then return Unknown Channel (10003).
            log.debug("Channel %s disappeared before jail restrictions were applied in guild %s", channel.id, channel.guild.id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not restrict new channel %s for jail role in guild %s", channel.id, channel.guild.id)

    @commands.group(name="jail", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def jail(self, ctx: commands.Context, member: discord.Member | None = None, *, reason: str = "No reason provided"):
        if member is None:
            if not await self._can_configure(ctx):
                return
            data = await self.bot.db.get_guild(ctx.guild.id)
            view = JailSetupView(self.bot, ctx.guild, data.get("jail", {}), ctx.author.id)
            await ctx.send(view=component_card(jail_config_embed(ctx.guild, data), view), mention_author=False)
            return
        await self._jail_member(ctx, member, reason)

    @jail.command(name="add", case_insensitive=True)
    @commands.guild_only()
    async def jail_add(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await self._jail_member(ctx, member, reason)

    @jail.command(name="remove", aliases=("unjail",), case_insensitive=True)
    @commands.guild_only()
    async def jail_remove(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await self._unjail_member(ctx, member, reason)

    @jail.command(name="list", case_insensitive=True)
    @commands.guild_only()
    async def jail_list(self, ctx: commands.Context):
        if not await self._can_moderate(ctx):
            return
        data = await self.bot.db.get_guild(ctx.guild.id)
        config = data.get("jail", {})
        role = ctx.guild.get_role(config.get("role_id")) if config.get("role_id") else None
        if not role:
            await ctx.send(view=component_card(panel("Jail Not Configured", f"Run `{settings.prefix}jail setup` first.")), mention_author=False)
            return
        member_ids = {member.id for member in role.members}
        member_ids.update(int(user_id) for user_id in config.get("members", {}))
        sorted_ids = sorted(member_ids)
        listing = "\n".join(f"• <@{user_id}> (`{user_id}`)" for user_id in sorted_ids[:30]) if sorted_ids else "No members are currently jailed."
        if len(sorted_ids) > 30:
            listing += f"\n…and {len(sorted_ids) - 30} more."
        await ctx.send(view=component_card(panel(f"Jailed Members • {len(sorted_ids)}", listing)), mention_author=False)

    @jail.command(name="reset", case_insensitive=True)
    @commands.guild_only()
    async def jail_reset(self, ctx: commands.Context):
        if not await self._can_configure(ctx):
            return
        status_message = await ctx.send(
            view=component_card(jail_loading_embed("Resetting Jail Setup", "Restoring saved permissions and removing Zyven-created jail resources…")),
            mention_author=False,
        )

        async def finish(title: str, description: str):
            await status_message.edit(view=component_card(panel(title, description)))

        data = await self.bot.db.get_guild(ctx.guild.id)
        config = data.get("jail", {})
        role = ctx.guild.get_role(config.get("role_id")) if config.get("role_id") else None
        tracked_members = config.get("members", {})
        active_count = max(len(role.members), len(tracked_members)) if role else 0
        if role and active_count:
            await finish("Jail Reset Blocked", f"Remove all {active_count} jailed member(s) first with `{settings.prefix}jail remove @member`.")
            return
        bot_member = ctx.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await finish("Jail Reset Blocked", "Zyven needs **Manage Channels** to restore the saved channel permissions and remove jail channels/categories. Grant that permission, then retry.")
            return
        if role and config.get("role_created"):
            if not bot_member.guild_permissions.manage_roles:
                await finish("Jail Reset Blocked", "Zyven needs **Manage Roles** to delete the jail role. Grant that permission, then retry.")
                return
            if role >= bot_member.top_role:
                await finish("Jail Reset Blocked", f"Zyven cannot delete `{discord.utils.escape_markdown(role.name)}` because it is at or above Zyven's highest role. Move Zyven's highest role above the jail role, then retry.")
                return
        owned_channel_id = config.get("owned_channel_id")
        jail_channel_id = owned_channel_id or config.get("channel_id")
        jail_channel = ctx.guild.get_channel(jail_channel_id) if jail_channel_id else None
        # The topic check recognizes channels made by the previous setup
        # version, which did not yet persist channel_created.
        channel_created = bool(owned_channel_id) or config.get("channel_created") is True or (
            "channel_created" not in config
            and config.get("role_created")
            and isinstance(jail_channel, discord.TextChannel)
            and jail_channel.topic == "Private channel for jailed members"
        )
        stage = "restoring jail role channel permissions"
        try:
            if role:
                await restore_jail_permissions(ctx.guild, role, config.get("original_overwrites", {}))
            stage = "restoring @everyone channel permissions"
            await restore_default_overwrites(ctx.guild, config.get("original_default_overwrites", {}))
            if channel_created and jail_channel:
                stage = f"deleting jail channel #{jail_channel.name}"
                await jail_channel.delete(reason=f"Jail setup reset by {ctx.author}")
            owned_category_id = config.get("owned_category_id") or (config.get("category_id") if config.get("category_created") else None)
            jail_category = ctx.guild.get_channel(owned_category_id) if owned_category_id else None
            if isinstance(jail_category, discord.CategoryChannel) and not jail_category.channels:
                stage = f"deleting jail category {jail_category.name}"
                await jail_category.delete(reason=f"Jail setup reset by {ctx.author}")
            if role and config.get("role_created"):
                stage = f"deleting jail role {role.name}"
                await role.delete(reason=f"Jail setup reset by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Could not reset jail setup in guild %s", ctx.guild.id)
            reason = getattr(exc, "text", None) or str(exc) or type(exc).__name__
            await finish("Jail Reset Failed", f"Could not complete while **{stage}**. Discord returned `{type(exc).__name__}` (code `{getattr(exc, 'code', 'unknown')}`). Check Zyven's **Manage Channels/Manage Roles** permissions and ensure its highest role is above the jail role. Details: {reason[:400]}")
            return
        await self.bot.db.unset_value(ctx.guild.id, "jail")
        self._jail_role_cache.pop(ctx.guild.id, None)
        await finish("Jail Reset", "Jail settings and their saved channel overrides have been cleared.")

    @jail.command(name="setup", aliases=("config",), case_insensitive=True)
    @commands.guild_only()
    async def jail_setup(self, ctx: commands.Context):
        if not await self._can_configure(ctx):
            return
        data = await self.bot.db.get_guild(ctx.guild.id)
        view = JailSetupView(self.bot, ctx.guild, data.get("jail", {}), ctx.author.id)
        await ctx.send(view=component_card(jail_config_embed(ctx.guild, data), view), mention_author=False)

    async def _jail_member(self, ctx: commands.Context, member: discord.Member, reason: str):
        if not await self._can_moderate(ctx):
            return
        if member.id == ctx.author.id:
            await ctx.send(
                view=component_card(panel("Cannot Jail Yourself", "You cannot use the jail command on your own account.")),
                mention_author=False,
                delete_after=5,
            )
            return

        status_message = await ctx.send(
            view=component_card(panel("Jailing Member", f"Saving roles and applying the jail role to {member.mention}…")),
            mention_author=False,
        )

        async def finish(title: str, description: str, color: int = 0x5865F2):
            await status_message.edit(view=component_card(panel(title, description, color=color)))

        data = await self.bot.db.get_guild(ctx.guild.id)
        config = data.get("jail", {})
        role = ctx.guild.get_role(config.get("role_id")) if config.get("role_id") else None
        channel = ctx.guild.get_channel(config.get("channel_id")) if config.get("channel_id") else None
        if not role or not channel:
            await finish("Jail Not Configured", f"Run `{settings.prefix}jail setup` and select a role and jail channel first.", 0xED4245)
            return
        if member.id == ctx.guild.owner_id or member.id == self.bot.user.id:
            await finish("Cannot Jail Member", "The server owner and bot cannot be jailed.", 0xED4245)
            return
        bot_member = ctx.guild.me
        if bot_member is None or role >= bot_member.top_role or member.top_role >= bot_member.top_role:
            await finish("Role Hierarchy", "Move Zyven's role above the jail role and all roles held by the target before jailing them.", 0xED4245)
            return
        saved_roles = config.get("members", {}).get(str(member.id))
        already_jailed = role in member.roles
        if already_jailed and saved_roles is None:
            await finish("Already Jailed", f"{member.mention} already has the jail role.", 0xFEE75C)
            return
        if not already_jailed:
            saved_roles = [r.id for r in member.roles if r != ctx.guild.default_role and r != role]
            await self.bot.db.set_nested(ctx.guild.id, "jail", f"members.{member.id}", saved_roles)

        # Preserve managed roles (which bots cannot remove), while clearing all
        # assignable roles. Saved role IDs remain until unjail succeeds.
        managed_roles = [r for r in member.roles if r != ctx.guild.default_role and r != role and r.managed]
        try:
            # One Discord role update avoids the latency and partial state of
            # separate add_roles/remove_roles requests.
            await member.edit(roles=[role, *managed_roles], reason=f"Jailed by {ctx.author}: {reason[:400]}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not fully jail member %s in guild %s", member.id, ctx.guild.id)
            await finish("Jail Failed", "Zyven could not update the member's roles. The saved roles are kept; check Manage Roles and role positions, then try again.", 0xED4245)
            return
        remaining = managed_roles
        note = f"\n**Unchangeable managed roles:** {len(remaining)}" if remaining else ""
        embed = panel("Member Jailed", f"**Member:** {member.mention}\n**Moderator:** {ctx.author.mention}\n**Jail Channel:** {channel.mention}\n**Reason:** {discord.utils.escape_markdown(reason[:400])}{note}")
        await status_message.edit(view=component_card(embed))

    @commands.command(name="unjail", case_insensitive=True)
    @commands.guild_only()
    async def unjail(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await self._unjail_member(ctx, member, reason)

    async def _unjail_member(self, ctx: commands.Context, member: discord.Member, reason: str):
        if not await self._can_moderate(ctx):
            return
        data = await self.bot.db.get_guild(ctx.guild.id)
        config = data.get("jail", {})
        role = ctx.guild.get_role(config.get("role_id")) if config.get("role_id") else None
        if not role or role not in member.roles:
            await ctx.send(view=component_card(panel("Not Jailed", f"{member.mention} does not have the configured jail role.")), mention_author=False)
            return
        saved_ids = config.get("members", {}).get(str(member.id), [])
        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send(view=component_card(panel("Unjail Failed", "Zyven's server member record is unavailable. Try again shortly.")), mention_author=False)
            return
        if saved_ids:
            roles = [ctx.guild.get_role(role_id) for role_id in saved_ids]
            roles = [r for r in roles if r and not r.managed and r < bot_member.top_role and r != role]
        else:
            # Manual Jail-role assignments have no saved snapshot. Keep every
            # existing role and simply remove the jail role.
            roles = []
        status_message = await ctx.send(
            view=component_card(panel("Unjailing Member", f"Restoring saved roles for {member.mention}…")),
            mention_author=False,
        )

        async def finish(title: str, description: str, color: int = 0x5865F2):
            await status_message.edit(view=component_card(panel(title, description, color=color)))

        try:
            current_roles = [current for current in member.roles if current != ctx.guild.default_role and current != role]
            protected_roles = [current for current in current_roles if current.managed or current >= bot_member.top_role]
            if protected_roles:
                # Keep managed/inaccessible roles untouched; atomic=False makes
                # restoring all ordinary roles a single request.
                if roles:
                    await member.add_roles(*roles, atomic=False, reason=f"Restored roles during unjail by {ctx.author}: {reason[:400]}")
                await member.remove_roles(role, atomic=False, reason=f"Unjailed by {ctx.author}: {reason[:400]}")
            else:
                restored = {current.id: current for current in current_roles}
                restored.update({saved.id: saved for saved in roles})
                await member.edit(
                    roles=list(restored.values()),
                    reason=f"Unjailed by {ctx.author}: {reason[:400]}",
                )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not unjail member %s in guild %s", member.id, ctx.guild.id)
            await finish("Unjail Failed", "Zyven could not restore the member's roles. Check permissions and role positions.", 0xED4245)
            return
        await self.bot.db.unset_value(ctx.guild.id, f"jail.members.{member.id}")
        skipped = max(0, len(saved_ids) - len(roles)) if saved_ids else 0
        skipped_note = f"\n**Roles not restored:** {skipped} (deleted, managed, or above Zyven's top role)." if skipped else ""
        await finish("Member Unjailed", f"**Member:** {member.mention}\n**Moderator:** {ctx.author.mention}\n**Restored roles:** {len(roles)}\n**Reason:** {discord.utils.escape_markdown(reason[:400])}{skipped_note}", 0x57F287)

    async def _can_configure(self, ctx: commands.Context) -> bool:
        if jail_actor(ctx):
            bot_member = ctx.guild.me
            if bot_member and bot_member.guild_permissions.manage_roles and bot_member.guild_permissions.manage_channels:
                return True
            await ctx.send(view=component_card(panel("Bot Permissions Required", "Zyven needs **Manage Roles** and **Manage Channels** for jail setup.")), mention_author=False)
            return False
        await ctx.send(view=component_card(panel("Access Denied", "You need the **Administrator** permission to configure jail.")), mention_author=False, delete_after=5)
        return False

    async def _can_moderate(self, ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True
        await ctx.send(view=component_card(panel("Access Denied", "You need the **Administrator** permission to jail or unjail members.")), mention_author=False, delete_after=5)
        return False


async def setup(bot):
    await bot.add_cog(Jail(bot))
