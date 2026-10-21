import asyncio
import logging
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import (
    panel, send_card, notice_panel, component_card, edit_component_card,
)

VOICE_BULK_TARGET_CAP = 50
VOICE_BULK_BATCH_SIZE = 5
VOICE_BULK_CONCURRENCY = 4
VOICE_BULK_RATE_WINDOW = 30.0
VOICE_BULK_RATE_LIMIT = 2
log = logging.getLogger("zyven.voice")


from utils.owner_permissions import owner_or_guild_permissions
VOICE_ACTIONS = {
    "lock": ("Lock", "your voice channel", "voice_lock"),
    "unlock": ("Unlock", "your voice channel", "voice_unlock"),
    "hide": ("Hide", "your voice channel", "voice_hide"),
    "unhide": ("Reveal", "your voice channel", "voice_unhide"),
    "rename": ("Rename", "your voice channel", "voice_rename"),
    "limit": ("Limit", "your voice channel user limit", "voice_limit"),
    "mute": ("Mute", "a user", "voice_mute"),
    "unmute": ("Unmute", "a user", "voice_unmute"),
    "deafen": ("Deafen", "a user", "voice_deafen"),
    "undeafen": ("Undeafen", "a user", "voice_undeafen"),
    "permit": ("Permit", "a user", "voice_permit"),
    "ban": ("Ban", "a user from your voice channel", "voice_ban"),
    "transfer": ("Transfer", "ownership", "voice_transfer"),
    "claim": ("Claim", "ownership", "voice_claim"),
}


class VoiceBulkConfirmView(discord.ui.View):
    def __init__(self, cog, ctx, action: str, channel: discord.VoiceChannel, targets: list[discord.Member]):
        super().__init__(timeout=120)
        self.cog = cog
        self.ctx = ctx
        self.action = action
        self.channel = channel
        self.targets = targets
        self.requester_id = ctx.author.id
        confirm = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started this bulk move can confirm it.", ephemeral=True)
            return False
        return True

    async def _cancel(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return
        self.stop()
        await interaction.response.edit_message(
            view=component_card(notice_panel("Bulk Move Cancelled", "No members were moved."))
        )

    async def _confirm(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return
        self.stop()
        if not await self.cog._allow_bulk_rate(interaction.guild_id, self.requester_id):
            await interaction.response.edit_message(
                view=component_card(notice_panel("Please Slow Down", "Try the bulk move again in a moment."))
            )
            return
        await interaction.response.defer()
        moved, failure = await self.cog._execute_bulk_move(
            interaction.guild_id,
            self.action,
            self.channel,
            self.targets,
            self.ctx.author,
        )
        if failure:
            await edit_component_card(
                interaction,
                notice_panel("Bulk Move Stopped", f"Moved {moved} of {len(self.targets)} member(s). {failure}"),
            )
            return
        await edit_component_card(
            interaction,
            panel("Success", f"Moved {moved} member(s) to {self.channel.mention}.", color=0x57F287),
        )


class VoiceControlButton(discord.ui.Button):
    def __init__(self, cog, action: str, row: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=icon(VOICE_ACTIONS[action][2]),
            custom_id=f"zyven:voice:{action}",
            row=row,
        )
        self.cog = cog
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_control(interaction, self.action)


class VoiceControlView(discord.ui.View):
    def __init__(self, cog, enabled: list[str] | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        for index, action in enumerate(enabled or VOICE_ACTIONS):
            if action in VOICE_ACTIONS:
                self.add_item(VoiceControlButton(cog, action, index // 5))


class VoiceButtonSelect(discord.ui.Select):
    def __init__(self, parent):
        self.setup_view = parent
        options = [
            discord.SelectOption(label="Enable All Buttons", value="all"),
            *[
                discord.SelectOption(label=f"{label} — {target}", value=key, emoji=icon(VOICE_ACTIONS[key][2]))
                for key, (label, target, _) in VOICE_ACTIONS.items()
            ],
        ]
        super().__init__(
            placeholder="Select buttons to enable",
            min_values=1,
            max_values=len(options),
            options=options,
            custom_id="zyven:voice:setup-select",
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.setup_view.requester_id:
            await interaction.response.send_message("Run the VoiceMaster setup command to configure these buttons.", ephemeral=True)
            return
        if "all" in self.values:
            enabled = list(VOICE_ACTIONS)
        else:
            enabled = [key for key in VOICE_ACTIONS if key in self.values]
        await self.setup_view.save(interaction, enabled)


def setup_button_panel():
    embed = panel(
        "VoiceMaster Setup",
        "Select the buttons you want to enable for the VoiceMaster system.\n\n"
        "Select **Enable All Buttons** for the full interface.\n"
        "You can customize specific buttons from the dropdown below.",
    )
    return embed


class VoiceSetupModeSelect(discord.ui.Select):
    def __init__(self, parent):
        self.setup_view = parent
        super().__init__(
            placeholder="Choose Automatic or Custom setup",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Automatic Setup", value="auto", description="Create or reuse a VoiceMaster category, interface, and trigger VC."),
                discord.SelectOption(label="Custom Setup", value="custom", description="Choose the interface text channel and trigger voice channel."),
            ],
            custom_id="zyven:voice:setup-mode",
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.setup_view.requester_id:
            await interaction.response.send_message("Only the person who started setup can choose this option.", ephemeral=True)
            return
        if self.values[0] == "custom":
            await edit_component_card(
                interaction,
                panel("Custom VoiceMaster Setup", "Select the text channel where the interface should be posted."),
                VoiceCustomInterfaceView(self.setup_view.cog, self.setup_view.ctx),
            )
            return
        await interaction.response.defer()
        guild = interaction.guild
        auto_resources = {}
        try:
            category = next((item for item in guild.categories if item.name.casefold() == "voicemaster"), None)
            if category is None:
                category = await guild.create_category("VoiceMaster", reason=f"VoiceMaster automatic setup by {interaction.user}")
                auto_resources["category"] = category.id
            interface = next((item for item in category.text_channels if item.name.casefold() == "interface"), None)
            if interface is None:
                interface = await guild.create_text_channel("interface", category=category, reason=f"VoiceMaster automatic setup by {interaction.user}")
                auto_resources["interface_channel"] = interface.id
            trigger = next((item for item in category.voice_channels if item.name.casefold() == "join to create vc"), None)
            if trigger is None:
                trigger = await guild.create_voice_channel("Join to create VC", category=category, reason=f"VoiceMaster automatic setup by {interaction.user}")
                auto_resources["trigger_channel"] = trigger.id
            # Track every resource selected by Automatic Setup so reset can
            # remove the complete auto configuration, including reused items.
            auto_resources.update({
                "category": category.id,
                "interface_channel": interface.id,
                "trigger_channel": trigger.id,
            })
        except (discord.Forbidden, discord.HTTPException):
            await self.setup_view.cog.delete_auto_resources(guild, auto_resources, "Cleaning up incomplete VoiceMaster setup")
            await edit_component_card(interaction, notice_panel("Automatic setup could not create the required channels. Check Zyven's Manage Channels permission."))
            return
        await self.setup_view.cog.bot.db.set_values(guild.id, {"voicemaster.pending_auto_resources": auto_resources})
        setup_values = {
            "trigger_channel": trigger.id,
            "category": category.id,
            "interface_channel": interface.id,
            "setup_mode": "auto",
            "auto_resources": auto_resources,
        }
        await edit_component_card(
            interaction,
            setup_button_panel(),
            VoiceButtonSetupView(self.setup_view.cog, self.setup_view.ctx, interface, setup_values),
        )


class VoiceSetupModeView(discord.ui.View):
    def __init__(self, cog, ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.requester_id = ctx.author.id
        self.add_item(VoiceSetupModeSelect(self))
        self.add_item(VoiceSetupCloseButton(cog, self.requester_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can choose this option.", ephemeral=True)
            return False
        self.cog.touch_setup_session(interaction.guild_id, interaction.user.id)
        return True


class VoiceCustomInterfaceSelect(discord.ui.ChannelSelect):
    def __init__(self, parent):
        self.setup_view = parent
        super().__init__(
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            placeholder="Select interface text channel",
            custom_id="zyven:voice:setup-interface",
        )

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await edit_component_card(
            interaction,
            panel("Custom VoiceMaster Setup", "Now select the voice channel members will join to create their temporary room."),
            VoiceCustomTriggerView(self.setup_view.cog, self.setup_view.ctx, channel),
        )


class VoiceCustomInterfaceView(discord.ui.View):
    def __init__(self, cog, ctx: commands.Context):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.requester_id = ctx.author.id
        self.add_item(VoiceCustomInterfaceSelect(self))
        self.add_item(VoiceSetupCloseButton(cog, self.requester_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can choose these channels.", ephemeral=True)
            return False
        self.cog.touch_setup_session(interaction.guild_id, interaction.user.id)
        return True


class VoiceCustomTriggerSelect(discord.ui.ChannelSelect):
    def __init__(self, parent):
        self.setup_view = parent
        super().__init__(
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
            placeholder="Select trigger voice channel",
            custom_id="zyven:voice:setup-trigger",
        )

    async def callback(self, interaction: discord.Interaction):
        selected_trigger = self.values[0]
        guild = interaction.guild
        if guild is None:
            await send_card(interaction, notice_panel("Custom VoiceMaster setup can only be used inside a server."), ephemeral=True)
            return
        # ChannelSelect values are AppCommandChannel partial objects. Resolve
        # the selected ID to a real guild channel before checking overwrites.
        trigger = guild.get_channel(selected_trigger.id)
        if trigger is None:
            try:
                trigger = await guild.fetch_channel(selected_trigger.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await send_card(interaction, notice_panel("Zyven could not load that voice channel. Please reopen setup and try again."), ephemeral=True)
                return
        if not isinstance(trigger, discord.VoiceChannel):
            await send_card(interaction, notice_panel("Please select a voice channel that still exists in this server."), ephemeral=True)
            return
        bot_member = guild.me
        if bot_member is None:
            await send_card(interaction, notice_panel("Zyven could not verify its permissions in this server."), ephemeral=True)
            return
        destination_permissions = trigger.permissions_for(bot_member)
        missing = [name for name, allowed in (
            ("Manage Channels", destination_permissions.manage_channels),
            ("Move Members", destination_permissions.move_members),
            ("View Channel", destination_permissions.view_channel),
            ("Connect", destination_permissions.connect),
        ) if not allowed]
        if missing:
            await send_card(interaction, notice_panel(f"Zyven is missing **{', '.join(missing)}** in {trigger.mention} or its category. Check channel permission overwrites."), ephemeral=True)
            return
        setup_values = {
            "trigger_channel": trigger.id,
            "category": trigger.category_id,
            "interface_channel": self.setup_view.interface_channel.id,
            "setup_mode": "custom",
            "auto_resources": {},
        }
        await edit_component_card(
            interaction,
            setup_button_panel(),
            VoiceButtonSetupView(
                self.setup_view.cog,
                self.setup_view.ctx,
                self.setup_view.interface_channel,
                setup_values,
            ),
        )


class VoiceCustomTriggerView(discord.ui.View):
    def __init__(self, cog, ctx: commands.Context, interface_channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.requester_id = ctx.author.id
        self.interface_channel = interface_channel
        self.add_item(VoiceCustomTriggerSelect(self))
        self.add_item(VoiceSetupCloseButton(cog, self.requester_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can choose these channels.", ephemeral=True)
            return False
        self.cog.touch_setup_session(interaction.guild_id, interaction.user.id)
        return True


class VoiceButtonSetupView(discord.ui.View):
    def __init__(self, cog, ctx: commands.Context, publish_channel: discord.TextChannel | None = None, setup_values: dict | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.requester_id = ctx.author.id
        self.publish_channel = publish_channel or ctx.channel
        self.setup_values = setup_values or {}
        self.add_item(VoiceButtonSelect(self))
        self.add_item(VoiceSetupCloseButton(cog, self.requester_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can choose these buttons.", ephemeral=True)
            return False
        self.cog.touch_setup_session(interaction.guild_id, interaction.user.id)
        return True

    async def save(self, interaction: discord.Interaction, enabled: list[str]):
        guild_id = interaction.guild_id
        guild = interaction.guild
        if guild is None:
            await send_card(interaction, notice_panel("Custom VoiceMaster setup can only be used inside a server."), ephemeral=True)
            return
        # ChannelSelect returns an AppCommandChannel partial, which has no
        # permissions_for() or send() methods. Resolve it to the guild's real
        # channel before checking permissions or publishing the interface.
        interface_channel = guild.get_channel(self.publish_channel.id)
        if interface_channel is None:
            try:
                interface_channel = await guild.fetch_channel(self.publish_channel.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await send_card(interaction, notice_panel("Zyven could not load the selected interface channel. Please reopen setup and try again."), ephemeral=True)
                return
        if not isinstance(interface_channel, discord.TextChannel):
            await send_card(interaction, notice_panel("Please select a text channel that still exists in this server."), ephemeral=True)
            return
        bot_member = guild.me
        if bot_member is None:
            await send_card(interaction, notice_panel("Zyven could not verify its permissions in this server."), ephemeral=True)
            return
        interface_permissions = interface_channel.permissions_for(bot_member)
        missing = []
        if not bot_member.guild_permissions.manage_channels:
            missing.append("Manage Channels")
        if not interface_permissions.view_channel:
            missing.append("View Channel")
        if not interface_permissions.send_messages:
            missing.append("Send Messages")
        if missing:
            await send_card(interaction, notice_panel(f"Zyven is missing **{', '.join(missing)}** in {interface_channel.mention} or its category."), ephemeral=True)
            return
        updates = {
            "voicemaster.enabled_buttons": enabled,
            "voicemaster.interface_channel": interface_channel.id,
        }
        for key in ("trigger_channel", "category", "setup_mode", "auto_resources"):
            if key in self.setup_values:
                updates[f"voicemaster.{key}"] = self.setup_values[key]
        old_config = (await self.cog.bot.db.get_guild(guild_id)).get("voicemaster", {})
        if self.setup_values.get("setup_mode") == "auto":
            created_resources = dict(old_config.get("auto_resources", {}))
            created_resources.update(self.setup_values.get("auto_resources", {}))
            updates["voicemaster.auto_resources"] = created_resources
        await self.cog.remove_interface_panel(guild, old_config)
        await self.cog.bot.db.set_values(guild_id, updates)
        await self.cog.bot.db.unset_value(guild_id, "voicemaster.pending_auto_resources")
        await self.cog.lock_interface_channel(guild, interface_channel)
        await edit_component_card(interaction, panel("VoiceMaster has been set up successfully"))
        panel_message = await interface_channel.send(
            view=component_card(self.cog.interface_panel(enabled), VoiceControlView(self.cog, enabled)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.cog.bot.db.set_values(guild_id, {"voicemaster.interface_message_id": panel_message.id})
        self.cog.clear_setup_session(guild_id, self.requester_id)


class VoiceSetupCloseButton(discord.ui.Button):
    def __init__(self, cog, requester_id: int):
        super().__init__(label="Close Setup", style=discord.ButtonStyle.secondary, row=1)
        self.cog = cog
        self.requester_id = requester_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who started setup can close it.", ephemeral=True)
            return
        if interaction.guild_id is not None:
            if self.cog.clear_setup_session(interaction.guild_id, self.requester_id):
                await self.cog.cleanup_pending_auto_resources(interaction.guild_id)
        await edit_component_card(
            interaction,
            panel("VoiceMaster Setup Closed", f"Setup closed. Run `{settings.prefix}voicemaster setup` whenever you want to start again."),
        )



class VoiceMemberSelect(discord.ui.Select):
    def __init__(self, cog, action: str, channel: discord.VoiceChannel, requester_id: int):
        self.channel_id = channel.id
        self.cog = cog
        self.action = action
        members = sorted(channel.members, key=lambda member: member.id != requester_id)[:25]
        options = [
            discord.SelectOption(label=member.display_name[:100], value=str(member.id))
            for member in members
        ]
        super().__init__(placeholder="Select a member in your voice channel", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        result = await self.cog.current_room(interaction)
        if result is None:
            return
        channel, _ = result
        member = interaction.guild.get_member(int(self.values[0]))
        if channel.id != self.channel_id or member is None or member not in channel.members:
            await send_card(interaction, notice_panel("That member is no longer connected to your voice channel. Open the control again to refresh the list."), ephemeral=True)
            return
        await self.cog.handle_target(interaction, self.action, member)


class VoiceMemberSelectView(discord.ui.View):
    def __init__(self, cog, action: str, channel: discord.VoiceChannel, requester_id: int):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.add_item(VoiceMemberSelect(cog, action, channel, requester_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who opened this member list can use it.", ephemeral=True)
            return False
        return True


class VoiceRenameModal(discord.ui.Modal, title="Rename Voice Channel"):
    name = discord.ui.TextInput(label="New voice channel name", min_length=1, max_length=100)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        result = await self.cog.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.cog.can_control(interaction, owner_id):
            return
        name = str(self.name.value).strip()
        if not name:
            await send_card(interaction, notice_panel("Enter a channel name."), ephemeral=True)
            return
        await channel.edit(name=name, reason=f"Temporary room renamed by {interaction.user}")
        await send_card(interaction, panel("Voice Room Renamed", f"Room name set to **{discord.utils.escape_markdown(name)}**."), ephemeral=True)


class VoiceLimitModal(discord.ui.Modal, title="Set Voice Channel Limit"):
    limit = discord.ui.TextInput(label="User limit (0 removes the limit)", min_length=1, max_length=2, default="0")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.limit.value)
        except ValueError:
            amount = -1
        if not 0 <= amount <= 99:
            await send_card(interaction, notice_panel("Enter a user limit from 0 to 99."), ephemeral=True)
            return
        result = await self.cog.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.cog.can_control(interaction, owner_id):
            return
        await channel.edit(user_limit=amount, reason=f"Temporary room limit changed by {interaction.user}")
        label = "unlimited" if amount == 0 else str(amount)
        await send_card(interaction, panel("Voice Room Limit Updated", f"Member limit for {channel.mention} is **{label}**."), ephemeral=True)

class VoiceControls(commands.Cog):
    """Owner controls for rooms created by the VoiceMaster join channel."""

    def __init__(self, bot):
        self.bot = bot
        self.setup_sessions: dict[int, dict] = {}
        self._bulk_locks: dict[int, asyncio.Lock] = {}
        self._bulk_move_semaphore = asyncio.Semaphore(VOICE_BULK_CONCURRENCY)
        self._bulk_rate_lock = asyncio.Lock()
        self._bulk_rate_buckets: dict[tuple[int, int], deque[float]] = {}

    async def _allow_bulk_rate(self, guild_id: int | None, user_id: int) -> bool:
        if guild_id is None:
            return False
        now = time.monotonic()
        async with self._bulk_rate_lock:
            bucket = self._bulk_rate_buckets.setdefault((guild_id, user_id), deque())
            while bucket and now - bucket[0] >= VOICE_BULK_RATE_WINDOW:
                bucket.popleft()
            if len(bucket) >= VOICE_BULK_RATE_LIMIT:
                return False
            bucket.append(now)
            return True

    async def _move_one_member(self, member: discord.Member, channel: discord.VoiceChannel, reason: str):
        async with self._bulk_move_semaphore:
            await member.move_to(channel, reason=reason)

    async def _execute_bulk_move(self, guild_id: int | None, action: str, channel: discord.VoiceChannel, targets: list[discord.Member], actor):
        if guild_id is None:
            return 0, "This command can only be used inside a server."
        lock = self._bulk_locks.setdefault(guild_id, asyncio.Lock())
        if lock.locked():
            return 0, "Another bulk voice operation is already running in this server."
        moved = 0
        async with lock:
            for start in range(0, len(targets), VOICE_BULK_BATCH_SIZE):
                batch = targets[start:start + VOICE_BULK_BATCH_SIZE]
                results = await asyncio.gather(
                    *(self._move_one_member(member, channel, f"Voice {action} by {actor}") for member in batch),
                    return_exceptions=True,
                )
                batch_failure = None
                for result in results:
                    if result is None:
                        moved += 1
                        continue
                    if isinstance(result, discord.Forbidden):
                        batch_failure = batch_failure or "Discord denied a move; check permissions and role position."
                        continue
                    if isinstance(result, discord.HTTPException):
                        if result.status == 429:
                            log.warning("Stopping bulk voice %s after a Discord 429 in guild=%s", action, guild_id)
                            batch_failure = batch_failure or "Discord rate-limited this operation; no further members were moved."
                        else:
                            log.warning("Stopping bulk voice %s after HTTP %s in guild=%s", action, result.status, guild_id)
                            batch_failure = batch_failure or "Discord rejected a move; no further members were moved."
                        continue
                    log.error("Stopping bulk voice %s after unexpected move failure in guild=%s: %r", action, guild_id, result)
                    batch_failure = batch_failure or "A move failed unexpectedly; no further members were moved."
                if batch_failure:
                    return moved, batch_failure
        return moved, None

    async def _request_bulk_move(self, ctx, action: str, channel: discord.VoiceChannel, targets: list[discord.Member]):
        if len(targets) > VOICE_BULK_TARGET_CAP:
            return await self._voice_reply(ctx, "Error", f"This operation is limited to {VOICE_BULK_TARGET_CAP} members at a time.")
        if not targets:
            return await self._voice_reply(ctx, "Success", f"Moved 0 member(s) to {channel.mention}.", success=True)
        verb = "move members from your current voice channel" if action == "moveall" else "pull members from all voice channels"
        view = VoiceBulkConfirmView(self, ctx, action, channel, targets)
        await ctx.send(
            view=component_card(
                panel("Confirm Bulk Move", f"Confirm to {verb} to {channel.mention}? This will move **{len(targets)}** member(s)."),
                view,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def has_active_setup(self, guild_id: int) -> bool:
        session = self.setup_sessions.get(guild_id)
        if session is None:
            return False
        if session["expires"] <= time.monotonic():
            self.setup_sessions.pop(guild_id, None)
            return False
        return True

    def start_setup_session(self, guild_id: int, user_id: int):
        self.setup_sessions[guild_id] = {"owner": user_id, "expires": time.monotonic() + 300, "message": None}

    def set_setup_message(self, guild_id: int, user_id: int, message):
        session = self.setup_sessions.get(guild_id)
        if session and session["owner"] == user_id:
            session["message"] = message

    def touch_setup_session(self, guild_id: int | None, user_id: int):
        if guild_id is None:
            return
        session = self.setup_sessions.get(guild_id)
        if session and session["owner"] == user_id and session["expires"] > time.monotonic():
            session["expires"] = time.monotonic() + 300

    def clear_setup_session(self, guild_id: int, user_id: int):
        session = self.setup_sessions.get(guild_id)
        if session and session["owner"] == user_id:
            self.setup_sessions.pop(guild_id, None)
            return True
        return False

    async def close_setup_session(self, guild_id: int):
        session = self.setup_sessions.pop(guild_id, None)
        if session is None:
            await self.cleanup_pending_auto_resources(guild_id)
            return False
        message = session.get("message")
        if message is not None:
            try:
                await message.edit(view=component_card(panel("VoiceMaster Setup Closed", f"Setup closed. Run `{settings.prefix}voicemaster setup` whenever you want to start again.")))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await self.cleanup_pending_auto_resources(guild_id)
        return True

    async def delete_auto_resources(self, guild: discord.Guild, resources: dict, reason: str):
        for key in ("trigger_channel", "interface_channel"):
            channel_id = resources.get(key)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel is not None:
                try:
                    await channel.delete(reason=reason)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        category_id = resources.get("category")
        category = guild.get_channel(int(category_id)) if category_id else None
        if isinstance(category, discord.CategoryChannel) and not category.channels:
            try:
                await category.delete(reason=reason)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def cleanup_pending_auto_resources(self, guild_id: int):
        document = await self.bot.db.get_guild(guild_id)
        config = document.get("voicemaster", {})
        resources = config.get("pending_auto_resources", {})
        if resources:
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.delete_auto_resources(guild, resources, "Cleaning up cancelled VoiceMaster setup")
        await self.bot.db.unset_value(guild_id, "voicemaster.pending_auto_resources")

    async def lock_interface_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Make the public interface read-only while retaining the bot's posting access."""
        config = (await self.bot.db.get_guild(guild.id)).get("voicemaster", {})
        locks = config.get("interface_lock_overwrites", {})
        channel_key = str(channel.id)
        if channel_key not in locks:
            everyone_before = channel.overwrites_for(guild.default_role)
            bot_member = guild.me
            bot_before = channel.overwrites_for(bot_member) if bot_member else discord.PermissionOverwrite()
            locks[channel_key] = {
                "everyone": {
                    "send_messages": everyone_before.send_messages,
                    "send_messages_in_threads": everyone_before.send_messages_in_threads,
                    "create_public_threads": everyone_before.create_public_threads,
                    "create_private_threads": everyone_before.create_private_threads,
                },
                "bot": {
                    "send_messages": bot_before.send_messages,
                    "send_messages_in_threads": bot_before.send_messages_in_threads,
                },
            }
        await self.bot.db.set_values(guild.id, {"voicemaster.interface_lock_overwrites": locks})
        everyone_overwrite = channel.overwrites_for(guild.default_role)
        everyone_overwrite.send_messages = False
        everyone_overwrite.send_messages_in_threads = False
        everyone_overwrite.create_public_threads = False
        everyone_overwrite.create_private_threads = False
        await channel.set_permissions(guild.default_role, overwrite=everyone_overwrite, reason="Keep the VoiceMaster interface read-only")
        bot_member = guild.me
        if bot_member:
            bot_overwrite = channel.overwrites_for(bot_member)
            bot_overwrite.send_messages = True
            bot_overwrite.send_messages_in_threads = True
            await channel.set_permissions(bot_member, overwrite=bot_overwrite, reason="Allow Zyven to post the VoiceMaster interface")

    async def remove_interface_panel(self, guild: discord.Guild, config: dict):
        channel_id = config.get("interface_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return False
        message_id = config.get("interface_message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
            if guild.me and message.author.id == guild.me.id:
                await message.delete()
                return True
            return False

        # Migrate panels posted before their message IDs were stored.
        bot_id = guild.me.id if guild.me else None
        try:
            async for message in channel.history(limit=100):
                if bot_id is None or message.author.id != bot_id:
                    continue
                pending = list(message.components)
                while pending:
                    component = pending.pop()
                    if "VoiceMaster Interface" in str(getattr(component, "content", "")):
                        await message.delete()
                        return True
                    pending.extend(getattr(component, "children", ()))
        except (discord.Forbidden, discord.HTTPException):
            return False
        return False

    async def delete_reset_resources(self, guild: discord.Guild, config: dict) -> list[str]:
        failed = []
        for channel_id in config.get("temporary_channels", []):
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            try:
                await channel.delete(reason="VoiceMaster configuration reset")
            except (discord.Forbidden, discord.HTTPException):
                failed.append(f"temporary channel {channel_id}")

        created = dict(config.get("auto_resources", {}))
        created.update(config.get("pending_auto_resources", {}))
        if not created and config.get("setup_mode") != "custom":
            # Older automatic setups predate explicit ownership tracking.
            category = guild.get_channel(int(config["category"])) if config.get("category") else None
            interface = guild.get_channel(int(config["interface_channel"])) if config.get("interface_channel") else None
            trigger = guild.get_channel(int(config["trigger_channel"])) if config.get("trigger_channel") else None
            if isinstance(category, discord.CategoryChannel) and category.name.casefold() == "voicemaster":
                if isinstance(interface, discord.TextChannel) and interface.name.casefold() == "interface" and interface.category_id == category.id:
                    created["interface_channel"] = interface.id
                if isinstance(trigger, discord.VoiceChannel) and trigger.name.casefold() == "join to create vc" and trigger.category_id == category.id:
                    created["trigger_channel"] = trigger.id
                if created:
                    created["category"] = category.id
        for key in ("trigger_channel", "interface_channel"):
            channel_id = created.get(key)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel is None:
                continue
            try:
                await channel.delete(reason="VoiceMaster automatic setup reset")
            except (discord.Forbidden, discord.HTTPException):
                failed.append(channel.name)
        category_id = created.get("category")
        category = guild.get_channel(int(category_id)) if category_id else None
        if isinstance(category, discord.CategoryChannel) and not category.channels:
            try:
                await category.delete(reason="VoiceMaster automatic setup reset")
            except (discord.Forbidden, discord.HTTPException):
                failed.append(category.name)
        return failed

    async def restore_interface_channel_locks(self, guild: discord.Guild, config: dict):
        locks = config.get("interface_lock_overwrites", {})
        for channel_id, saved in locks.items():
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            for target, key in ((guild.default_role, "everyone"), (guild.me, "bot")):
                values = saved.get(key)
                if target is None or not values:
                    continue
                overwrite = channel.overwrites_for(target)
                for permission, value in values.items():
                    setattr(overwrite, permission, value)
                await channel.set_permissions(target, overwrite=overwrite, reason="Restore VoiceMaster interface channel permissions")

    async def cog_load(self):
        # Register every stable custom ID so posted interfaces keep working after restarts.
        self.bot.add_view(VoiceControlView(self))

    def interface_panel(self, enabled: list[str] | None = None) -> discord.Embed:
        enabled = enabled or list(VOICE_ACTIONS)
        embed = panel(
            "VoiceMaster Interface",
            "Use the buttons below to manage your voice channel.",
        )
        lines = ["**__Control Buttons__**"]
        for action in enabled:
            if action not in VOICE_ACTIONS:
                continue
            label, target, emoji_key = VOICE_ACTIONS[action]
            lines.append(f"{icon(emoji_key)} - **[{label}]({settings.support_url})** {target}")
        embed.description += "\n\n" + "\n".join(lines)
        return embed

    def control_view(self, enabled: list[str] | None = None) -> VoiceControlView:
        return VoiceControlView(self, enabled)

    def settings_panel(self, guild: discord.Guild, config: dict) -> discord.Embed:
        trigger = guild.get_channel(config.get("trigger_channel")) if config.get("trigger_channel") else None
        category = guild.get_channel(config.get("category")) if config.get("category") else (trigger.category if trigger else None)
        interface_channel_id = config.get("interface_channel")
        interface_channel = f"<#{interface_channel_id}>" if interface_channel_id else "Not configured"
        configured = config.get("enabled_buttons")
        enabled = [key for key in (configured or []) if key in VOICE_ACTIONS]
        labels = (", ".join(VOICE_ACTIONS[key][0] for key in enabled) or "None") if configured is not None else "Not configured"
        embed = panel("VoiceMaster Configuration")
        embed.description = (
            f"> **Interface Channel:** {interface_channel}\n"
            f"> **Trigger VC:** {trigger.mention if trigger else 'Not configured'}\n"
            f"> **Category:** {discord.utils.escape_markdown(category.name) if category else 'Not configured'}\n"
            f"> **Active Temp Channels:** {len(config.get('temporary_channels', []))}\n"
            f"> **Enabled Buttons:** {labels}"
        )
        return embed

    async def show_button_setup(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("voicemaster", {})
        if not config.get("trigger_channel") or not config.get("interface_channel"):
            await ctx.send(view=component_card(notice_panel(f"Set up VoiceMaster first with `{settings.prefix}voicemaster setup`, then you can update its buttons.")))
            return
        channel = ctx.guild.get_channel(config.get("interface_channel")) if config.get("interface_channel") else None
        channel = channel if isinstance(channel, discord.TextChannel) else ctx.channel
        setup_values = {
            key: config[key]
            for key in ("trigger_channel", "category", "interface_channel", "setup_mode", "auto_resources")
            if key in config
        }
        await ctx.send(view=component_card(setup_button_panel(), VoiceButtonSetupView(self, ctx, channel, setup_values)))

    async def show_setup_mode(self, ctx: commands.Context):
        embed = panel(
            "VoiceMaster Setup",
            "Choose how you want to set up VoiceMaster.\n\n"
            "**Automatic Setup** creates or reuses the VoiceMaster category, interface channel, and trigger VC.\n"
            "**Custom Setup** lets you select your own interface channel and trigger VC.",
        )
        return await ctx.send(view=component_card(embed, VoiceSetupModeView(self, ctx)))

    @commands.group(name="voicemaster", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def prefix_voicemaster(self, ctx: commands.Context):
        """Configure VoiceMaster temporary channels and their interface."""
        help_command = self.bot.get_command("help")
        await ctx.invoke(help_command, query="voicemaster")

    @prefix_voicemaster.command(name="config")
    @commands.guild_only()
    async def prefix_voicemaster_config(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("voicemaster", {})
        await ctx.send(view=component_card(self.settings_panel(ctx.guild, config)))

    @prefix_voicemaster.command(name="setup")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    async def prefix_voicemaster_setup(self, ctx: commands.Context):
        if self.has_active_setup(ctx.guild.id):
            await ctx.send(view=component_card(notice_panel(f"A VoiceMaster setup is already in progress in this server. Use `{settings.prefix}voicemaster close` to close it, or wait for it to expire.")))
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("voicemaster", {})
        trigger = ctx.guild.get_channel(int(config["trigger_channel"])) if config.get("trigger_channel") else None
        interface = ctx.guild.get_channel(int(config["interface_channel"])) if config.get("interface_channel") else None
        if isinstance(trigger, discord.VoiceChannel) and isinstance(interface, discord.TextChannel):
            await ctx.send(view=component_card(notice_panel(f"VoiceMaster is already set up in this server. Use `{settings.prefix}voicemaster buttons` to update the panel, or `{settings.prefix}voicemaster reset` before setting it up again.")))
            return
        await self.cleanup_pending_auto_resources(ctx.guild.id)
        bot_member = ctx.guild.me
        if bot_member is None or not ctx.channel.permissions_for(bot_member).send_messages:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** in this channel to start setup.")))
            return
        self.start_setup_session(ctx.guild.id, ctx.author.id)
        try:
            message = await self.show_setup_mode(ctx)
        except Exception:
            self.clear_setup_session(ctx.guild.id, ctx.author.id)
            raise
        self.set_setup_message(ctx.guild.id, ctx.author.id, message)

    @prefix_voicemaster.command(name="close")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    async def prefix_voicemaster_close(self, ctx: commands.Context):
        """Close an active VoiceMaster setup session."""
        if not await self.close_setup_session(ctx.guild.id):
            await ctx.send(view=component_card(notice_panel("There is no active VoiceMaster setup to close.")))
            return
        await ctx.send(view=component_card(panel("VoiceMaster Setup Closed", f"The active setup was closed. You can run `{settings.prefix}voicemaster setup` again.")))

    @prefix_voicemaster.command(name="buttons")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    async def prefix_voicemaster_buttons(self, ctx: commands.Context):
        """Choose which controls appear on the VoiceMaster interface."""
        bot_member = ctx.guild.me
        if bot_member is None or not ctx.channel.permissions_for(bot_member).send_messages:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** in this channel to start setup.")))
            return
        await self.show_button_setup(ctx)

    @prefix_voicemaster.command(name="reset")
    @commands.guild_only()
    @owner_or_guild_permissions(manage_channels=True)
    async def prefix_voicemaster_reset(self, ctx: commands.Context):
        progress = await ctx.send(view=component_card(panel("VoiceMaster Reset in Progress", "Removing the current interface and VoiceMaster resources…")))
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("voicemaster", {})
        try:
            await self.close_setup_session(ctx.guild.id)
            await progress.edit(view=component_card(panel("VoiceMaster Reset in Progress", "Restoring interface permissions and removing temporary voice rooms…")))
            await self.remove_interface_panel(ctx.guild, config)
            await self.restore_interface_channel_locks(ctx.guild, config)
            failed = await self.delete_reset_resources(ctx.guild, config)
            await progress.edit(view=component_card(panel("VoiceMaster Reset in Progress", "Clearing the saved VoiceMaster configuration…")))
            await self.bot.db.unset_values(ctx.guild.id, [
                "voicemaster.trigger_channel",
                "voicemaster.category",
                "voicemaster.interface_channel",
                "voicemaster.interface_message_id",
                "voicemaster.enabled_buttons",
                "voicemaster.interface_lock_overwrites",
                "voicemaster.setup_mode",
                "voicemaster.auto_resources",
                "voicemaster.pending_auto_resources",
                "voicemaster.temporary_channels",
                "voicemaster.temporary_owners",
                "voicemaster.temporary_hide_overwrites",
                "voicemaster.temporary_lock_overwrites",
            ])
            detail = "VoiceMaster panel and temporary rooms were removed. Auto setup channels were deleted; custom channels were kept."
            if failed:
                detail += f"\nCould not delete: {', '.join(failed[:5])}. Check Zyven's Manage Channels permission."
            await progress.edit(view=component_card(panel("VoiceMaster Reset Complete", detail)))
        except Exception:
            await progress.edit(view=component_card(notice_panel(f"VoiceMaster reset did not finish. Check Zyven's permissions and the bot logs, then try `{settings.prefix}voicemaster reset` again.")))
            raise

    async def handle_control(self, interaction: discord.Interaction, action: str):
        if action == "lock":
            await self.set_locked(interaction, True)
        elif action == "unlock":
            await self.set_locked(interaction, False)
        elif action in {"hide", "unhide"}:
            await self.set_hidden(interaction, action == "hide")
        elif action == "rename":
            await interaction.response.send_modal(VoiceRenameModal(self))
        elif action == "limit":
            await interaction.response.send_modal(VoiceLimitModal(self))
        elif action == "claim":
            await self.claim_room(interaction)
        else:
            result = await self.current_room(interaction)
            if result is None:
                return
            channel, owner_id = result
            if not await self.can_control(interaction, owner_id):
                return
            if not channel.members:
                await send_card(interaction, notice_panel("There are no members connected to this voice channel."), ephemeral=True)
                return
            label = VOICE_ACTIONS[action][0]
            await send_card(
                interaction,
                panel(f"{label} a Member", "Choose a member currently connected to your voice channel."),
                VoiceMemberSelectView(self, action, channel, interaction.user.id),
                ephemeral=True,
            )

    async def claim_room(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if owner_id:
            if owner_id == interaction.user.id:
                await send_card(interaction, notice_panel("You already own this voice room."), ephemeral=True)
                return
            current_owner = next((member for member in channel.members if member.id == owner_id), None)
            if current_owner:
                await send_card(interaction, notice_panel(f"{current_owner.mention} still owns this room and is connected."), ephemeral=True)
                return
            departed_owner = await self.resolve_member(channel.guild, owner_id)
            if departed_owner:
                await self.set_member_connect(channel, departed_owner, None, reason=f"Abandoned room claimed by {interaction.user}")
        if channel.overwrites_for(channel.guild.default_role).connect is False:
            await self.set_member_connect(channel, interaction.user, True)
        await self.bot.db.set_nested(channel.guild.id, "voicemaster", f"temporary_owners.{channel.id}", interaction.user.id)
        await send_card(interaction, panel("Voice Room Claimed", f"You are now the owner of {channel.mention}."), ephemeral=True)

    async def set_hidden(self, interaction: discord.Interaction, hidden: bool):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        config = (await self.bot.db.get_guild(channel.guild.id)).get("voicemaster", {})
        state_path = f"temporary_hide_overwrites.{channel.id}"
        saved = config.get("temporary_hide_overwrites", {}).get(str(channel.id))
        if hidden:
            everyone = channel.overwrites_for(channel.guild.default_role)
            already_hidden = everyone.view_channel is False
            if not already_hidden and saved is None:
                saved = {}
                owner = await self.resolve_member(channel.guild, owner_id)
                for member in (owner, interaction.user):
                    if member:
                        saved[str(member.id)] = channel.overwrites_for(member).view_channel
                await self.bot.db.set_nested(channel.guild.id, "voicemaster", state_path, saved)
            everyone.view_channel = False
            await channel.set_permissions(channel.guild.default_role, overwrite=everyone, reason=f"Voice room hidden by {interaction.user}")
            for member in (await self.resolve_member(channel.guild, owner_id), interaction.user):
                if member:
                    overwrite = channel.overwrites_for(member)
                    overwrite.view_channel = True
                    await channel.set_permissions(member, overwrite=overwrite, reason=f"Voice room owner access preserved by {interaction.user}")
        else:
            everyone = channel.overwrites_for(channel.guild.default_role)
            everyone.view_channel = None
            await channel.set_permissions(channel.guild.default_role, overwrite=everyone, reason=f"Voice room revealed by {interaction.user}")
            if isinstance(saved, dict):
                for member_id, value in saved.items():
                    member = await self.resolve_member(channel.guild, int(member_id))
                    if member:
                        overwrite = channel.overwrites_for(member)
                        overwrite.view_channel = value
                        await channel.set_permissions(member, overwrite=overwrite, reason=f"Voice room visibility restored by {interaction.user}")
                await self.bot.db.unset_value(channel.guild.id, f"voicemaster.{state_path}")
            else:
                owner = await self.resolve_member(channel.guild, owner_id)
                for member in (owner, interaction.user):
                    if member:
                        overwrite = channel.overwrites_for(member)
                        overwrite.view_channel = None
                        await channel.set_permissions(member, overwrite=overwrite, reason=f"Legacy room visibility restored by {interaction.user}")
        state = "hidden" if hidden else "visible"
        await send_card(interaction, panel(f"Voice Room {state.title()}", f"{channel.mention} is now {state}."), ephemeral=True)

    async def handle_target(self, interaction: discord.Interaction, action: str, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        label = VOICE_ACTIONS[action][0]
        if action in {"mute", "unmute", "deafen", "undeafen"}:
            if member not in channel.members:
                await send_card(interaction, notice_panel("Choose a member who is connected to your temporary room."), ephemeral=True)
                return
            permission_name = "deafen_members" if action in {"deafen", "undeafen"} else "mute_members"
            bot_member = channel.guild.me
            if bot_member is None or not getattr(bot_member.guild_permissions, permission_name):
                await send_card(interaction, notice_panel(f"Zyven needs **{permission_name.replace('_', ' ').title()}** permission for this action."), ephemeral=True)
                return
            if action in {"mute", "unmute"}:
                await member.edit(mute=action == "mute", reason=f"Voice room {label.lower()} by {interaction.user}")
            else:
                await member.edit(deafen=action == "deafen", reason=f"Voice room {label.lower()} by {interaction.user}")
            await send_card(interaction, panel(f"Member {label}d", f"{member.mention} was {label.lower()}d in {channel.mention}."), ephemeral=True)
            return
        if action in {"permit", "ban"}:
            if action == "ban" and member.id in {interaction.user.id, owner_id, channel.guild.owner_id}:
                await send_card(interaction, notice_panel("You cannot ban yourself, the server owner, or this room's owner from the room."), ephemeral=True)
                return
            await self.set_member_connect(channel, member, action == "permit", reason=f"Voice room {label.lower()} by {interaction.user}")
            description = f"{member.mention} can join {channel.mention}." if action == "permit" else f"{member.mention} can no longer join {channel.mention}."
            await send_card(interaction, panel(f"Member {label}", description), ephemeral=True)
            return
        if action == "transfer":
            if member.bot or member.id == interaction.user.id:
                await send_card(interaction, notice_panel("Choose another human member as the new room owner."), ephemeral=True)
                return
            if member not in channel.members:
                await send_card(interaction, notice_panel("The new owner must be connected to this room."), ephemeral=True)
                return
            old_owner = await self.resolve_member(channel.guild, owner_id)
            locked = channel.overwrites_for(channel.guild.default_role).connect is False
            if old_owner and old_owner.id != member.id:
                await self.set_member_connect(channel, old_owner, None, reason=f"Room ownership transferred by {interaction.user}")
            if locked:
                await self.set_member_connect(channel, member, True, reason=f"New room owner set by {interaction.user}")
            await self.bot.db.set_nested(channel.guild.id, "voicemaster", f"temporary_owners.{channel.id}", member.id)
            await send_card(interaction, panel("Voice Room Owner Changed", f"{member.mention} now owns {channel.mention}."), ephemeral=True)

    @commands.group(name="voice", aliases=("vcmod",), invoke_without_command=True, case_insensitive=True)
    async def prefix_voice(self, ctx: commands.Context):
        """Show the voice command group and available controls."""
        help_command = self.bot.get_command("help")
        await ctx.invoke(help_command, query="voice")

    async def _voice_reply(self, ctx, title: str, text: str, *, success: bool = False):
        color = 0x57F287 if success else 0xED4245
        emoji = icon("approve") if success else icon("error")
        await ctx.send(
            view=component_card(panel(f"{emoji} {title}", text, color=color)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _voice_member_action(self, ctx, member: discord.Member | None, action: str, *, permission: str):
        if member is None:
            return await self._voice_reply(ctx, "Error", f"You need to mention a member to {action}.")
        if not getattr(ctx.author.guild_permissions, permission):
            return await self._voice_reply(ctx, "Error", f"You need the **{permission.replace('_', ' ').title()}** permission to {action} members.")
        if member.voice is None or member.voice.channel is None:
            return await self._voice_reply(ctx, "Error", f"{member.display_name} is not connected to any voice channel.")
        if not getattr(ctx.guild.me.guild_permissions, permission):
            return await self._voice_reply(ctx, "Error", f"Zyven needs the **{permission.replace('_', ' ').title()}** permission.")
        channel = member.voice.channel
        try:
            if action == "mute":
                await member.edit(mute=True, reason=f"Voice muted by {ctx.author}")
            elif action == "unmute":
                await member.edit(mute=False, reason=f"Voice unmuted by {ctx.author}")
            elif action == "deafen":
                await member.edit(deafen=True, reason=f"Voice deafened by {ctx.author}")
            elif action == "undeafen":
                await member.edit(deafen=False, reason=f"Voice undeafened by {ctx.author}")
            elif action == "kick":
                await member.move_to(None, reason=f"Disconnected from voice by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", f"I couldn't {action} {member.mention}. Check my permissions and role position.")
        past = {"mute": "muted", "unmute": "unmuted", "deafen": "deafened", "undeafen": "undeafened", "kick": "disconnected"}[action]
        if action == "kick":
            text = f"{member.mention} has been disconnected from {channel.mention}."
        else:
            text = f"{member.mention} has been {past} in {channel.mention}."
        await self._voice_reply(ctx, "Success", text, success=True)

    @prefix_voice.command(name="mute")
    async def prefix_voice_mute(self, ctx, member: discord.Member | None = None):
        """Mute a member who is connected to a voice channel."""
        await self._voice_member_action(ctx, member, "mute", permission="mute_members")

    @prefix_voice.command(name="unmute")
    async def prefix_voice_unmute(self, ctx, member: discord.Member | None = None):
        """Unmute a member in a voice channel."""
        await self._voice_member_action(ctx, member, "unmute", permission="mute_members")

    @prefix_voice.command(name="deafen")
    async def prefix_voice_deafen(self, ctx, member: discord.Member | None = None):
        """Deafen a member in a voice channel."""
        await self._voice_member_action(ctx, member, "deafen", permission="deafen_members")

    @prefix_voice.command(name="undeafen")
    async def prefix_voice_undeafen(self, ctx, member: discord.Member | None = None):
        """Undeafen a member in a voice channel."""
        await self._voice_member_action(ctx, member, "undeafen", permission="deafen_members")

    @prefix_voice.command(name="kick")
    async def prefix_voice_kick(self, ctx, member: discord.Member | None = None):
        """Disconnect a member from their current voice channel."""
        await self._voice_member_action(ctx, member, "kick", permission="move_members")

    @commands.command(name="vckick", case_insensitive=True)
    @commands.guild_only()
    async def prefix_vckick(self, ctx, member: discord.Member | None = None):
        """Alias for `voice kick`."""
        await self._voice_member_action(ctx, member, "kick", permission="move_members")

    @commands.command(name="vcunmute", case_insensitive=True)
    @commands.guild_only()
    async def prefix_vcunmute(self, ctx, member: discord.Member | None = None):
        """Alias for `voice unmute`."""
        await self._voice_member_action(ctx, member, "unmute", permission="mute_members")

    async def _bulk_voice_action(self, ctx, action: str):
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to join a voice channel first.")
        permission = "mute_members" if action in {"mute", "unmute"} else "deafen_members"
        if not getattr(ctx.author.guild_permissions, permission):
            return await self._voice_reply(ctx, "Error", f"You need **{permission.replace('_', ' ').title()}** permission to use this command.")
        if not getattr(ctx.guild.me.guild_permissions, permission):
            return await self._voice_reply(ctx, "Error", f"Zyven needs **{permission.replace('_', ' ').title()}** permission.")
        members = [member for member in channel.members if member.id != ctx.author.id]
        if not members:
            return await self._voice_reply(ctx, "Error", "There are no other members in your voice channel.")
        try:
            await asyncio.gather(*(
                member.edit(**({"mute": action == "mute"} if action in {"mute", "unmute"} else {"deafen": action == "deafen"}), reason=f"Voice {action}all by {ctx.author}")
                for member in members
            ))
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't update everyone. Check my permissions and role position.")
        past = {"mute": "muted", "unmute": "unmuted", "deafen": "deafened", "undeafen": "undeafened"}[action]
        await self._voice_reply(ctx, "Success", f"{len(members)} member(s) have been {past} in {channel.mention}.", success=True)

    @prefix_voice.command(name="muteall")
    async def prefix_voice_muteall(self, ctx):
        """Mute everyone else in your voice channel."""
        await self._bulk_voice_action(ctx, "mute")

    @prefix_voice.command(name="unmuteall")
    async def prefix_voice_unmuteall(self, ctx):
        """Unmute everyone else in your voice channel."""
        await self._bulk_voice_action(ctx, "unmute")

    @prefix_voice.command(name="deafenall")
    async def prefix_voice_deafenall(self, ctx):
        """Deafen everyone else in your voice channel."""
        await self._bulk_voice_action(ctx, "deafen")

    @prefix_voice.command(name="undeafenall")
    async def prefix_voice_undeafenall(self, ctx):
        """Undeafen everyone else in your voice channel."""
        await self._bulk_voice_action(ctx, "undeafen")

    @prefix_voice.command(name="kickall")
    async def prefix_voice_kickall(self, ctx):
        """Disconnect everyone else from your voice channel."""
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to join a voice channel first.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Move Members** permission to use this command.")
        targets = [member for member in channel.members if member.id != ctx.author.id]
        try:
            await asyncio.gather(*(member.move_to(None, reason=f"Voice kickall by {ctx.author}") for member in targets))
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't disconnect everyone. Check my permissions and role position.")
        await self._voice_reply(ctx, "Success", f"Disconnected {len(targets)} member(s) from {channel.mention}.", success=True)

    @prefix_voice.command(name="move")
    async def prefix_voice_move(self, ctx, member: discord.Member | None = None, channel: discord.VoiceChannel | None = None):
        """Move a member from one voice channel to another."""
        if member is None:
            return await self._voice_reply(ctx, "Error", "You need to mention a member to move.")
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to provide a destination voice channel.")
        if not member.voice or not member.voice.channel:
            return await self._voice_reply(ctx, "Error", f"{member.display_name} is not connected to any voice channel.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Move Members** permission to use this command.")
        source = member.voice.channel
        try:
            await member.move_to(channel, reason=f"Moved by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't move that member. Check my permissions and role position.")
        await self._voice_reply(ctx, "Success", f"Moved {member.mention} from {source.mention} to {channel.mention}.", success=True)

    @prefix_voice.command(name="moveall")
    async def prefix_voice_moveall(self, ctx, channel: discord.VoiceChannel | None = None):
        """Move everyone from your voice channel to another channel."""
        source = ctx.author.voice.channel if ctx.author.voice else None
        if source is None:
            return await self._voice_reply(ctx, "Error", "You need to join a voice channel first.")
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to provide a destination voice channel.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Move Members** permission to use this command.")
        targets = list(source.members)
        return await self._request_bulk_move(ctx, "moveall", channel, targets)

    @prefix_voice.command(name="pull")
    async def prefix_voice_pull(self, ctx, member: discord.Member | None = None):
        """Pull a member into your current voice channel."""
        await self._voice_pull_action(ctx, member)

    async def _voice_pull_action(self, ctx, member: discord.Member | None):
        if member is None:
            return await self._voice_reply(ctx, "Error", "You need to mention a member to pull.")
        destination = ctx.author.voice.channel if ctx.author.voice else None
        if destination is None:
            return await self._voice_reply(ctx, "Error", "You need to join a voice channel first.")
        if not member.voice or not member.voice.channel:
            return await self._voice_reply(ctx, "Error", f"{member.display_name} is not connected to any voice channel.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Move Members** permission to use this command.")
        try:
            await member.move_to(destination, reason=f"Pulled by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't pull that member. Check my permissions and role position.")
        await self._voice_reply(ctx, "Success", f"Moved {member.mention} to {destination.mention}.", success=True)

    @commands.command(name="vcpull", case_insensitive=True)
    @commands.guild_only()
    async def prefix_vcpull(self, ctx, member: discord.Member | None = None):
        """Alias for `voice pull`."""
        await self._voice_pull_action(ctx, member)

    @prefix_voice.command(name="pullall")
    async def prefix_voice_pullall(self, ctx, channel: discord.VoiceChannel | None = None):
        """Move members from all voice channels into the specified channel."""
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to provide a destination voice channel.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Move Members** permission to use this command.")
        targets = [member for source in ctx.guild.voice_channels if source.id != channel.id for member in source.members]
        return await self._request_bulk_move(ctx, "pullall", channel, targets)

    async def _voice_channel_access(self, ctx, action: str):
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel is None:
            return await self._voice_reply(ctx, "Error", "You need to join a voice channel first.")
        if not ctx.author.guild_permissions.manage_channels or not ctx.guild.me.guild_permissions.manage_channels:
            return await self._voice_reply(ctx, "Error", "You and Zyven need **Manage Channels** permission to use this command.")
        everyone = channel.overwrites_for(ctx.guild.default_role)
        if action == "lock":
            everyone.connect = False
        elif action == "unlock":
            everyone.connect = None
        elif action == "private":
            everyone.view_channel = False
        else:
            everyone.view_channel = None
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=everyone, reason=f"Voice {action} by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't update this channel's permissions.")
        descriptions = {
            "lock": "No one can join this voice channel now.",
            "unlock": "Members can join this voice channel again.",
            "private": "This voice channel is now hidden from @everyone.",
            "unprivate": "This voice channel is visible to @everyone again.",
        }
        await self._voice_reply(ctx, "Success", descriptions[action], success=True)

    @prefix_voice.command(name="lock")
    async def prefix_voice_lock(self, ctx):
        """Lock your current voice channel."""
        await self._voice_channel_access(ctx, "lock")

    @prefix_voice.command(name="unlock")
    async def prefix_voice_unlock(self, ctx):
        """Unlock your current voice channel."""
        await self._voice_channel_access(ctx, "unlock")

    @prefix_voice.command(name="private")
    async def prefix_voice_private(self, ctx):
        """Hide your current voice channel from @everyone."""
        await self._voice_channel_access(ctx, "private")

    @prefix_voice.command(name="unprivate")
    async def prefix_voice_unprivate(self, ctx):
        """Make your current voice channel visible to @everyone."""
        await self._voice_channel_access(ctx, "unprivate")

    @commands.command(name="vcban", case_insensitive=True)
    @commands.guild_only()
    async def vcban(self, ctx, member: discord.Member | None = None):
        """Prevent a connected member from rejoining their current voice channel."""
        if member is None:
            return await self._voice_reply(ctx, "Error", "You need to mention a member to voice-ban.")
        channel = member.voice.channel if member.voice else None
        if channel is None:
            return await self._voice_reply(ctx, "Error", f"{member.display_name} is not connected to any voice channel.")
        if not ctx.author.guild_permissions.move_members or not ctx.guild.me.guild_permissions.move_members or not ctx.guild.me.guild_permissions.manage_channels:
            return await self._voice_reply(ctx, "Error", "You need **Move Members** permission and Zyven needs **Move Members** and **Manage Channels**.")
        overwrite = channel.overwrites_for(member)
        overwrite.connect = False
        try:
            await channel.set_permissions(member, overwrite=overwrite, reason=f"Voice-ban by {ctx.author}")
            await member.move_to(None, reason=f"Voice-ban by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            return await self._voice_reply(ctx, "Error", "I couldn't voice-ban that member. Check my permissions and role position.")
        await self._voice_reply(ctx, "Success", f"{member.mention} was voice-banned from {channel.mention}.", success=True)

    async def current_room(self, interaction: discord.Interaction):
        member = interaction.user
        voice = getattr(member, "voice", None)
        channel = voice.channel if voice else None
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            await send_card(interaction, notice_panel("Join one of Zyven's temporary voice rooms first."), ephemeral=True)
            return None
        config = (await self.bot.db.get_guild(interaction.guild_id)).get("voicemaster", {})
        if channel.id not in config.get("temporary_channels", []):
            await send_card(interaction, notice_panel("This is not a tracked Zyven temporary room."), ephemeral=True)
            return None
        owner_id = config.get("temporary_owners", {}).get(str(channel.id))
        return channel, owner_id

    async def can_control(self, interaction: discord.Interaction, owner_id: int | None) -> bool:
        if owner_id == interaction.user.id or settings.is_owner(interaction.user.id) or interaction.user.guild_permissions.manage_channels:
            return True
        await send_card(interaction, notice_panel("Only this room's owner or a member with **Manage Channels** can change it."), ephemeral=True)
        return False

    async def set_member_connect(self, channel: discord.VoiceChannel, member: discord.Member, value: bool | None, *, reason: str | None = None):
        overwrite = channel.overwrites_for(member)
        overwrite.connect = value
        await channel.set_permissions(member, overwrite=overwrite, reason=reason)

    async def resolve_member(self, guild: discord.Guild, member_id: int | None):
        if member_id is None:
            return None
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @app_commands.command(name="voiceclaim", description="Claim an unowned or abandoned Zyven temporary room")
    @app_commands.guild_only()
    async def voiceclaim(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if owner_id:
            if owner_id == interaction.user.id:
                await send_card(interaction, notice_panel("You already own this voice room."), ephemeral=True)
                return
            current_owner = next((member for member in channel.members if member.id == owner_id), None)
            if current_owner:
                await send_card(interaction, notice_panel(f"{current_owner.mention} still owns this room and is connected."), ephemeral=True)
                return
            departed_owner = await self.resolve_member(channel.guild, owner_id)
            if departed_owner:
                await self.set_member_connect(channel, departed_owner, None, reason=f"Abandoned room claimed by {interaction.user}")
        if channel.overwrites_for(channel.guild.default_role).connect is False:
            await self.set_member_connect(channel, interaction.user, True)
        await self.bot.db.set_nested(channel.guild.id, "voicemaster", f"temporary_owners.{channel.id}", interaction.user.id)
        await send_card(interaction, panel("Voice Room Claimed", f"You are now the owner of {channel.mention}."), ephemeral=True)

    @app_commands.command(name="voiceowner", description="Transfer your temporary room to a member in it")
    @app_commands.guild_only()
    async def voiceowner(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        if member.bot or member.id == interaction.user.id:
            await send_card(interaction, notice_panel("Choose another human member as the new room owner."), ephemeral=True)
            return
        if member not in channel.members:
            await send_card(interaction, notice_panel("The new owner must be connected to this room."), ephemeral=True)
            return
        old_owner = await self.resolve_member(channel.guild, owner_id)
        everyone_locked = channel.overwrites_for(channel.guild.default_role).connect is False
        if old_owner and old_owner.id != member.id:
            await self.set_member_connect(channel, old_owner, None, reason=f"Room ownership transferred by {interaction.user}")
        if everyone_locked:
            await self.set_member_connect(channel, member, True, reason=f"New room owner set by {interaction.user}")
        await self.bot.db.set_nested(channel.guild.id, "voicemaster", f"temporary_owners.{channel.id}", member.id)
        await send_card(interaction, panel("Voice Room Owner Changed", f"{member.mention} now owns {channel.mention}."), ephemeral=True)

    @app_commands.command(name="voicename", description="Rename your temporary voice room")
    @app_commands.guild_only()
    @app_commands.describe(name="New room name (1–100 characters)")
    async def voicename(self, interaction: discord.Interaction, name: str):
        name = name.strip()
        if not 1 <= len(name) <= 100:
            await send_card(interaction, notice_panel("Room names must be 1–100 characters."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        await channel.edit(name=name, reason=f"Temporary room renamed by {interaction.user}")
        await send_card(interaction, panel("Voice Room Renamed", f"Room name set to **{discord.utils.escape_markdown(name)}**."), ephemeral=True)

    @app_commands.command(name="voicelimit", description="Set or remove the member limit for your temporary room")
    @app_commands.guild_only()
    async def voicelimit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        await channel.edit(user_limit=limit, reason=f"Temporary room limit changed by {interaction.user}")
        label = "unlimited" if limit == 0 else str(limit)
        await send_card(interaction, panel("Voice Room Limit Updated", f"Member limit for {channel.mention} is **{label}**."), ephemeral=True)

    async def set_locked(self, interaction: discord.Interaction, locked: bool):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        owner = interaction.user if owner_id == interaction.user.id else await self.resolve_member(channel.guild, owner_id)
        overwrite = channel.overwrites_for(channel.guild.default_role)
        was_locked = overwrite.connect is False
        lock_state_path = f"temporary_lock_overwrites.{channel.id}"
        config = (await self.bot.db.get_guild(channel.guild.id)).get("voicemaster", {})
        saved_overwrites = config.get("temporary_lock_overwrites", {}).get(str(channel.id))
        overwrite.connect = False if locked else None
        if locked:
            if not was_locked and saved_overwrites is None:
                saved_overwrites = {}
                for member in (owner, interaction.user):
                    if member:
                        saved_overwrites[str(member.id)] = channel.overwrites_for(member).connect
                await self.bot.db.set_nested(channel.guild.id, "voicemaster", lock_state_path, saved_overwrites)
            if owner:
                await self.set_member_connect(channel, owner, True, reason=f"Room owner exempted from lock by {interaction.user}")
            if interaction.user.id != (owner.id if owner else None):
                await self.set_member_connect(channel, interaction.user, True, reason=f"Room manager exempted from lock by {interaction.user}")
        await channel.set_permissions(
            channel.guild.default_role,
            overwrite=overwrite,
            reason=f"Temporary room {'locked' if locked else 'unlocked'} by {interaction.user}",
        )
        if not locked and isinstance(saved_overwrites, dict):
            for member_id, connect in saved_overwrites.items():
                member = await self.resolve_member(channel.guild, int(member_id))
                if member:
                    await self.set_member_connect(channel, member, connect, reason=f"Room lock permissions restored by {interaction.user}")
            await self.bot.db.unset_value(channel.guild.id, f"voicemaster.{lock_state_path}")
        elif not locked:
            # Older rooms may have been locked before original overwrites were tracked.
            if owner:
                await self.set_member_connect(channel, owner, None, reason=f"Legacy room lock cleared by {interaction.user}")
            if interaction.user.id != (owner.id if owner else None):
                await self.set_member_connect(channel, interaction.user, None, reason=f"Legacy manager lock cleared by {interaction.user}")
        state = "locked" if locked else "unlocked"
        await send_card(interaction, panel(f"Voice Room {state.title()}", f"{channel.mention} is now {state}."), ephemeral=True)

    @app_commands.command(name="voicelock", description="Prevent @everyone from joining your temporary room")
    @app_commands.guild_only()
    async def voicelock(self, interaction: discord.Interaction):
        await self.set_locked(interaction, True)

    @app_commands.command(name="voiceunlock", description="Allow members to join your temporary room again")
    @app_commands.guild_only()
    async def voiceunlock(self, interaction: discord.Interaction):
        await self.set_locked(interaction, False)

    @app_commands.command(name="voicekick", description="Disconnect a member from your temporary room")
    @app_commands.guild_only()
    async def voicekick(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        if member.id == owner_id:
            await send_card(interaction, notice_panel("The room owner cannot be disconnected with this command."), ephemeral=True)
            return
        if member.id == interaction.guild.owner_id:
            await send_card(interaction, notice_panel("The server owner cannot be disconnected with this command."), ephemeral=True)
            return
        if member not in channel.members:
            await send_card(interaction, notice_panel("That member is not connected to this room."), ephemeral=True)
            return
        bot_member = channel.guild.me
        if bot_member is None or member.top_role >= bot_member.top_role:
            await send_card(interaction, notice_panel("Move Zyven's role above that member before disconnecting them."), ephemeral=True)
            return
        await member.move_to(None, reason=f"Disconnected from a Zyven temporary room by {interaction.user}")
        await send_card(interaction, panel("Member Disconnected", f"{member.mention} was disconnected from {channel.mention}."), ephemeral=True)

    async def set_member_access(self, interaction: discord.Interaction, member: discord.Member, allowed: bool):
        await interaction.response.defer(ephemeral=True)
        result = await self.current_room(interaction)
        if result is None:
            return
        channel, owner_id = result
        if not await self.can_control(interaction, owner_id):
            return
        if not allowed and member.id in {interaction.user.id, owner_id, interaction.guild.owner_id}:
            await send_card(interaction, notice_panel("You cannot deny yourself, the server owner, or this room's owner access."), ephemeral=True)
            return
        await self.set_member_connect(
            channel,
            member,
            allowed,
            reason=f"Voice room access {'granted' if allowed else 'denied'} by {interaction.user}",
        )
        label = "can join" if allowed else "cannot join"
        await send_card(interaction, panel("Voice Room Access Updated", f"{member.mention} {label} {channel.mention}."), ephemeral=True)

    @app_commands.command(name="voicepermit", description="Allow a member to join your temporary room")
    @app_commands.guild_only()
    async def voicepermit(self, interaction: discord.Interaction, member: discord.Member):
        await self.set_member_access(interaction, member, True)

    @app_commands.command(name="voicedeny", description="Prevent a member from joining your temporary room")
    @app_commands.guild_only()
    async def voicedeny(self, interaction: discord.Interaction, member: discord.Member):
        await self.set_member_access(interaction, member, False)


async def setup(bot):
    await bot.add_cog(VoiceControls(bot))
