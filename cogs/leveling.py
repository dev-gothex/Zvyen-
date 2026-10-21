"""Leveling, XP and rewards commands ported to Zyven's discord.py bot.

Data is stored in the bot's MongoDB document collections. All public
commands are prefix commands and therefore use the guild's configured prefix.
"""
from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict
import inspect
import io
import logging
import random
import re
import time
from datetime import datetime, timezone

import discord
import aiohttp
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from utils.emojis import icon
from core.settings import safe_request

log = logging.getLogger("zyven.leveling")
_MAX_BACKGROUND_BYTES = 8 * 1024 * 1024
_LEVEL_CONFIG_CACHE_TTL = 30.0
_LEVEL_USER_CACHE_TTL = 600.0
_LEVEL_USER_CACHE_MAX = 20000
_UNSAFE_REWARD_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "manage_messages", "manage_threads", "manage_events", "manage_expressions",
    "manage_emojis_and_stickers", "manage_guild_expressions", "view_audit_log",
    "kick_members", "ban_members", "moderate_members", "manage_nicknames",
    "mention_everyone", "mute_members", "deafen_members", "move_members",
)


def _unsafe_reward_permissions(role: discord.Role) -> list[str]:
    return [name for name in _UNSAFE_REWARD_PERMISSIONS if getattr(role.permissions, name, False)]


def xp_for_level(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> tuple[int, int]:
    level, remaining = 0, max(0, int(xp))
    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1
    return level, remaining


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"


def _level_badge(rank: int) -> str:
    emoji_key = {1: "leveling_medal1", 2: "leveling_medal2", 3: "leveling_medal3"}.get(rank)
    return icon(emoji_key) if emoji_key else f"**#{rank}**"


def _progress_bar(current: int, maximum: int, length: int = 10) -> str:
    filled = max(0, min(length, round(max(0, current) / max(1, maximum) * length)))
    return "▰" * filled + "▱" * (length - filled)


class LevelingPanel(discord.ui.LayoutView):
    """Native Components V2 panel matching the supplied Leveling-Bot UI."""

    def __init__(self, container, source_view=None):
        super().__init__(timeout=getattr(source_view, "timeout", None))
        self.source_view = source_view
        self.requester_id = getattr(source_view, "owner_id", None)
        rows = {}
        kinds = {}
        for source_item in list(getattr(source_view, "children", ())):
            item = copy.copy(source_item)
            item._view = None
            if hasattr(item, "_parent"):
                item._parent = None
            callback = getattr(item, "callback", None)
            if getattr(callback, "item", None) is source_item:
                callback.item = item
            elif inspect.ismethod(callback) and callback.__self__ is source_item:
                item.callback = callback.__func__.__get__(item, type(item))
            is_select = isinstance(item, discord.ui.Select)
            row = source_item.row
            if row is None:
                row = next((idx for idx, group in rows.items()
                            if kinds[idx] == "buttons" and not is_select and len(group.children) < 5), None)
                if row is None:
                    row = next((idx for idx in range(5) if idx not in rows), 0)
            if row in rows and (is_select or kinds[row] == "select"):
                row = next((idx for idx in range(5) if idx not in rows), row)
            if row not in rows:
                rows[row] = discord.ui.ActionRow()
                kinds[row] = "select" if is_select else "buttons"
            rows[row].add_item(item)
        for row in sorted(rows):
            container.add_item(rows[row])
        self.add_item(container)
        for item in self.walk_children():
            item._view = self

    async def interaction_check(self, interaction):
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who opened this panel can use these buttons.", ephemeral=True)
            return False
        if self.source_view:
            return await self.source_view.interaction_check(interaction)
        return True

    async def on_timeout(self):
        if self.source_view:
            await self.source_view.on_timeout()


def _leveling_panel(container, view=None):
    return LevelingPanel(container, view)


class LevelProfileView(discord.ui.View):
    def __init__(self, cog, guild, member, owner_id):
        super().__init__(timeout=90)
        self.cog, self.guild, self.member, self.owner_id = cog, guild, member, owner_id
        self.message = None
        self.page = "overview"
        self._sync_buttons()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who opened this profile can use these buttons.", ephemeral=True)
        return False

    async def show(self, interaction, page="overview"):
        user = await self.cog._user(self.guild.id, self.member.id)
        self.page = page
        self._sync_buttons()
        container = await self.cog._profile_container(self.guild, self.member, user, page)
        await interaction.response.edit_message(view=_leveling_panel(container, self))

    def _sync_buttons(self):
        self.overview.disabled = self.page == "overview"
        self.overview.style = discord.ButtonStyle.success if self.page == "overview" else discord.ButtonStyle.secondary
        self.history.disabled = self.page == "history"
        self.history.style = discord.ButtonStyle.success if self.page == "history" else discord.ButtonStyle.primary

    async def on_timeout(self):
        self.timeout = None
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                user = await self.cog._user(self.guild.id, self.member.id)
                await self.message.edit(view=_leveling_panel(await self.cog._profile_container(self.guild, self.member, user, self.page, disabled=True), self))
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.success, custom_id="zyven_level_profile_overview")
    async def overview(self, interaction, button): await self.show(interaction, "overview")

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, custom_id="zyven_level_profile_refresh")
    async def refresh(self, interaction, button): await self.show(interaction, self.page)

    @discord.ui.button(label="History", style=discord.ButtonStyle.primary, custom_id="zyven_level_profile_history")
    async def history(self, interaction, button): await self.show(interaction, "history")


class _LeaderboardFindModal(discord.ui.Modal, title="Find a Member"):
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.query = discord.ui.TextInput(placeholder="Username, display name, or user ID", required=True, max_length=80)
        self.add_item(discord.ui.Label(text="Member", component=self.query))

    async def on_submit(self, interaction):
        query = str(self.query.value).strip().lower()
        target_id = query.strip("<@!>")
        rows = self.view_ref.rows
        index = next((i for i, row in enumerate(rows) if str(row.get("user_id")) == target_id), None)
        if index is None:
            for i, row in enumerate(rows):
                member = self.view_ref.guild.get_member(int(row["user_id"]))
                if member and query in {member.name.lower(), member.display_name.lower(), (member.global_name or "").lower()}:
                    index = i; break
        if index is None:
            return await interaction.response.send_message("That member is not on this leaderboard.", ephemeral=True)
        self.view_ref.page = index // self.view_ref.page_size
        await interaction.response.defer(ephemeral=True)
        row=rows[index]; place=index+1
        if self.view_ref.mode in {"messages","message"}: value=f"{int(row.get('messages',0)):,} messages"
        elif self.view_ref.mode=="voice": value=fmt_duration(row.get("voice_secs",0))
        else:
            level,_=level_from_xp(int(row.get("xp",0))); value=f"Level **{level}** • XP: {int(row.get('xp',0)):,}"
        await interaction.followup.send(f"Found <@{row['user_id']}> — rank **#{place}** • {value}",ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        if self.view_ref.message:
            await self.view_ref.message.edit(view=_leveling_panel(self.view_ref.container(),self.view_ref))


class LevelLeaderboardView(discord.ui.View):
    def __init__(self, guild, rows, mode, owner_id, caller_profile=None):
        super().__init__(timeout=90)
        self.guild, self.rows, self.mode, self.owner_id = guild, rows, mode, owner_id
        self.caller_profile = caller_profile or {}
        self.page_size, self.page = 10, 0
        self.message=None
        self.my_rank = next((i for i, row in enumerate(rows) if int(row["user_id"]) == owner_id), None)
        self._sync_buttons()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who opened this leaderboard can use these buttons.", ephemeral=True)
        return False

    def _sync_buttons(self):
        self.back.disabled = self.page <= 0
        self.next.disabled = self.page >= max(0, (len(self.rows)-1)//self.page_size)

    def container(self):
        start = self.page * self.page_size
        page_rows = self.rows[start:start + self.page_size]
        title = {"xp": "Server Leaderboard", "level": "Server Leaderboard", "messages": "Message Leaderboard", "message": "Message Leaderboard", "voice": "Voice Leaderboard"}.get(self.mode, "Server Leaderboard")
        lines = []
        for offset, row in enumerate(page_rows):
            position = start + offset + 1
            badge = _level_badge(position) if position <= 3 else f"`{position}.`"
            level, _ = level_from_xp(int(row.get("xp", 0)))
            xp = int(row.get("xp", 0))
            if self.mode in {"messages", "message"}:
                detail = f"Messages: {int(row.get('messages', 0)):,}"
                lines.append(f"{badge} <@{row['user_id']}> - {detail}\n> Level: {level}  •  XP: {xp:,}")
            elif self.mode == "voice":
                detail = fmt_duration(row.get("voice_secs", 0))
                lines.append(f"{badge} <@{row['user_id']}> - Time: {detail}\n> Level: {level}  •  XP: {xp:,}")
            else:
                lines.append(f"{badge} <@{row['user_id']}> - Level {level}\n> XP: {xp:,}")

        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255, 255, 255))
        container.add_item(discord.ui.TextDisplay(f"# {icon('leveling_trophy')} {title}"))
        container.add_item(discord.ui.Separator())
        if self.my_rank is not None:
            caller = self.rows[self.my_rank]
        else:
            caller = self.caller_profile
        caller_xp = int(caller.get("xp", 0))
        caller_level, _ = level_from_xp(caller_xp)
        caller_rank = self.my_rank + 1 if self.my_rank is not None else 9999
        rank_lines = [f"**My Rank:** {_level_badge(caller_rank)}"]
        if self.mode in {"messages", "message"}:
            rank_lines.append(f"> Messages: {int(caller.get('messages', 0)):,}")
        elif self.mode == "voice":
            rank_lines.append(f"> Time: {fmt_duration(caller.get('voice_secs', 0))}")
        rank_lines.extend((f"> Level: {caller_level}", f"> XP: {caller_xp:,}"))
        owner = self.guild.get_member(self.owner_id)
        if owner:
            container.add_item(discord.ui.Section(discord.ui.TextDisplay("\n".join(rank_lines)), accessory=discord.ui.Thumbnail(owner.display_avatar.url)))
        else:
            container.add_item(discord.ui.TextDisplay("\n".join(rank_lines)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("\n\n".join(lines) or "> No data yet."))
        return container

    async def on_timeout(self):
        self.timeout = None
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=_leveling_panel(self.container(), self))
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, custom_id="zyven_level_lb_back")
    async def back(self, interaction, button):
        self.page=max(0,self.page-1); self._sync_buttons(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Find", style=discord.ButtonStyle.primary, custom_id="zyven_level_lb_find")
    async def find(self, interaction, button): await interaction.response.send_modal(_LeaderboardFindModal(self))

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="zyven_level_lb_next")
    async def next(self, interaction, button):
        self.page=min(max(0,(len(self.rows)-1)//self.page_size),self.page+1); self._sync_buttons(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))


class _LevelSetupMessageModal(discord.ui.Modal, title="Level-up Message"):
    def __init__(self, view):
        super().__init__(); self.setup_view=view
        self.message_input=discord.ui.TextInput(default=view.config.get("levelup_message", "GG {mention}, you reached level **{level}**!"), placeholder="Use {mention}, {username}, {level}, and {xp}", max_length=1000, required=True, style=discord.TextStyle.paragraph)
        self.add_item(discord.ui.Label(text="Custom message", component=self.message_input))

    async def on_submit(self, interaction):
        self.setup_view.config["levelup_message"]=str(self.message_input.value)
        await interaction.response.send_message("Level-up message updated.", ephemeral=True)
        await self.setup_view.refresh_message()


class _LevelSetupImageModal(discord.ui.Modal, title="Level-up Background"):
    def __init__(self, view):
        super().__init__(); self.setup_view=view
        self.url_input=discord.ui.TextInput(default=view.config.get("level_up_background", ""), placeholder="https://… direct image URL", max_length=500, required=False)
        self.add_item(discord.ui.Label(text="Image URL", component=self.url_input))

    async def on_submit(self, interaction):
        url = str(self.url_input.value).strip()
        if url:
            try:
                await self.setup_view.cog._download_background(url)
            except (ValueError, discord.HTTPException) as error:
                return await interaction.response.send_message(
                    f"{icon('leveling_cross')} I couldn't load that image. Use a public PNG, JPG, GIF, or WebP image URL. ({error})",
                    ephemeral=True,
                )
        self.setup_view.config["level_up_background"]=url
        warning = " Discord attachment links can expire; keep the source message or use a permanent image host." if url and re.search(r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/", url, re.I) else ""
        await interaction.response.send_message(f"Background image saved for review.{warning}", ephemeral=True)
        await self.setup_view.refresh_message()


class _LevelSetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view):
        super().__init__(placeholder="Select a level-up text channel…", channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=0)
        self.setup_view=view

    async def callback(self, interaction):
        self.setup_view.config["levelup_channel"]=self.values[0].id if self.values else None
        await interaction.response.edit_message(view=_leveling_panel(self.setup_view.container(),self.setup_view))


class _LevelRewardRoleSelect(discord.ui.RoleSelect):
    def __init__(self, setup_view, remove=False, options=None):
        super().__init__(placeholder="Choose one role…", min_values=1, max_values=1, row=0)
        self.setup_view, self.remove = setup_view, remove
        self.options = options

    async def callback(self, interaction):
        roles = list(self.values)
        unavailable = [role for role in roles if not role.is_assignable()]
        if unavailable:
            return await interaction.response.send_message(
                "These roles are above my role or cannot be managed: " + ", ".join(role.mention for role in unavailable),
                ephemeral=True,
            )
        unsafe = [(role, _unsafe_reward_permissions(role)) for role in roles]
        unsafe = [(role, permissions) for role, permissions in unsafe if permissions]
        if unsafe:
            return await interaction.response.send_message(
                "Level rewards can't grant administrator or moderation permissions. Remove those permissions from: "
                + ", ".join(role.mention for role, _ in unsafe),
                ephemeral=True,
            )
        if self.remove:
            role = roles[0]
            item = next((x for x in self.setup_view.config["rewards"]
                         if str(x.get("role_id")) == str(role.id)), None)
            if item:
                self.setup_view.config["rewards"].remove(item)
            await interaction.response.edit_message(content="Reward removed from this setup.", view=None)
            await self.setup_view.refresh_message()
            return
        await interaction.response.send_modal(_LevelRewardLevelModal(self.setup_view, roles))


class _LevelRewardAddView(discord.ui.View):
    def __init__(self, setup_view):
        super().__init__(timeout=120)
        self.add_item(_LevelRewardRoleSelect(setup_view))


class _LevelRewardRemoveSelect(discord.ui.Select):
    def __init__(self, setup_view, options):
        super().__init__(placeholder="Choose a reward to remove…", options=options)
        self.setup_view = setup_view

    async def callback(self, interaction):
        level, role_id = self.values[0].split(":", 1)
        self.setup_view.config["rewards"] = [x for x in self.setup_view.config["rewards"]
            if not (int(x.get("level", -1)) == int(level) and str(x.get("role_id")) == role_id)]
        await interaction.response.edit_message(content="Reward removed from this setup.", view=None)
        await self.setup_view.refresh_message()


class _LevelRewardRemoveView(discord.ui.View):
    def __init__(self, setup_view, options):
        super().__init__(timeout=120)
        self.add_item(_LevelRewardRemoveSelect(setup_view, options))


class _LevelRewardLevelModal(discord.ui.Modal, title="Level Rewards"):
    def __init__(self, setup_view, roles):
        super().__init__()
        self.setup_view, self.roles = setup_view, list(roles)
        self.level = discord.ui.TextInput(placeholder="10", min_length=1, max_length=5)
        self.add_item(discord.ui.Label(text="Required level", component=self.level))

    async def on_submit(self, interaction):
        try:
            level = int(str(self.level.value))
            if not 1 <= level <= 1000:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter a level from 1 to 1000.", ephemeral=True)
        rewards = self.setup_view.config["rewards"]
        selected_ids = {str(role.id) for role in self.roles}
        rewards[:] = [item for item in rewards if not (int(item.get("level", -1)) == level and str(item.get("role_id")) in selected_ids)]
        rewards.extend({"level": level, "role_id": role.id} for role in self.roles)
        mentions = ", ".join(role.mention for role in self.roles)
        await interaction.response.send_message(f"Added {mentions} as the reward for **Level {level}**. Press **Add Reward** again to configure another level (for example 10, 20, or 30), then click **Confirm & Apply** to save.", ephemeral=True)
        await self.setup_view.refresh_message()


class LevelSetupView(discord.ui.View):
    def __init__(self, cog, ctx, cfg=None):
        super().__init__(timeout=900); self.cog=cog; self.ctx=ctx; self.owner_id=ctx.author.id; self.page=0; self.message=None; self.finished_status=None
        cfg=cfg or {}
        rewards = []
        for item in cfg.get("rewards", []):
            role = ctx.guild.get_role(int(item.get("role_id", 0))) if ctx.guild else None
            if role is None or not _unsafe_reward_permissions(role):
                rewards.append(item)
        self.preview_filename = "levelpreview.png"
        self.config={"enabled":bool(cfg.get("enabled",False)),"levelup_enabled":bool(cfg.get("levelup_enabled",True)),"levelup_channel":cfg.get("levelup_channel"),"levelup_dm":bool(cfg.get("levelup_dm",False)),"levelup_message":cfg.get("levelup_message","{mention} leveled up to **Level {level}**! 🎉"),"level_up_background":cfg.get("level_up_background") or "","rewards":rewards}
        self._build()

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id: return True
        await interaction.response.send_message("Only the person who started setup can use these controls.",ephemeral=True); return False

    async def on_timeout(self):
        self.timeout = None
        self.stop()
        self.clear_items()
        if self.message:
            try:
                self.finished_status = "expired"
                await self.message.edit(view=_leveling_panel(self.container(), self))
            except discord.HTTPException:
                pass

    def container(self):
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255,255,255))
        if self.finished_status:
            if self.finished_status == "saved":
                content = f"# Leveling UP Setup\n> **Configuration applied successfully!** {icon('leveling_check')}"
            else:
                content = f"# Leveling UP Setup\n> **Panel {self.finished_status}.**"
            container.add_item(discord.ui.TextDisplay(content))
            return container

        thumbnail = self.ctx.guild.icon.url if self.ctx.guild and self.ctx.guild.icon else self.cog.bot.user.display_avatar.url
        headers = {
            0: "# Leveling UP Setup Wizard\n> **Welcome! Let's get things configured.**\nUse this interactive wizard to set up where and how level-up notifications appear.",
            1: "# Step 1: Channel Setup\n> **Choose where the bot should send level up alerts.**",
            2: "# Step 2: Custom Message\n> **What's the message the bot should send on a level up?**",
            3: "# Step 3: Background Image\n> **Customize the look of your level-up cards.**",
            4: "# Step 4: Final Review\n> **Review your settings before applying everything.**",
        }
        container.add_item(discord.ui.Section(discord.ui.TextDisplay(headers[self.page]), accessory=discord.ui.Thumbnail(thumbnail)))
        if self.page == 1:
            container.add_item(discord.ui.Separator())
            destination = "Direct Messages" if self.config["levelup_dm"] else ""
            if self.config["levelup_channel"]:
                destination += (" & " if destination else "") + f"<#{self.config['levelup_channel']}>"
            elif not destination:
                destination = "Current channel"
            container.add_item(discord.ui.TextDisplay(f"Currently selected: **{destination}**"))
        elif self.page == 2:
            container.add_item(discord.ui.Separator())
            message = self.config["levelup_message"]
            shown = f"{message[:100]}…" if len(message) > 100 else message
            container.add_item(discord.ui.TextDisplay(f"Pending Custom Message:\n`{shown}`"))
        elif self.page == 3:
            container.add_item(discord.ui.Separator())
            background = self.config.get("level_up_background")
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{self.preview_filename}")))
            if background:
                if re.search(r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/", background, re.I):
                    container.add_item(discord.ui.TextDisplay(f"{icon('leveling_warning')} **This Discord CDN link may expire.** Use a permanent image host for a long-term background."))
            container.add_item(discord.ui.TextDisplay("Set a background via a direct image URL, or upload a file from your device."))
        elif self.page == 4:
            background = self.config.get("level_up_background")
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{self.preview_filename}")))
            container.add_item(discord.ui.Separator())
            destination = "Direct Messages" if self.config["levelup_dm"] else ""
            if self.config["levelup_channel"]:
                destination += (" & " if destination else "") + f"<#{self.config['levelup_channel']}>"
            elif not destination:
                destination = "Current channel"
            msg = self.config["levelup_message"]
            shown = f"{msg[:50]}…" if len(msg) > 50 else msg
            rewards = self.config.get("rewards", [])
            levels = sorted({int(item.get("level", 0)) for item in rewards if int(item.get("level", 0)) > 0})
            reward_summary = f"{len(rewards)} role reward(s)" + (f" — Levels {', '.join(map(str, levels))}" if levels else "")
            container.add_item(discord.ui.TextDisplay("\n".join((
                f"- **Leveling System** — {'Enabled' if self.config['enabled'] else 'Disabled'}",
                f"- **Destination** — {destination}",
                f"- **Message** — `{shown}`",
                f"- **Background** — {'Custom' if background else 'Default'}",
                f"- **Rewards** — {reward_summary}",
            ))))
        return container

    def _build(self):
        self.clear_items()
        def add(item, row):
            # All decorated buttons are registered by View.__init__ before this
            # wizard chooses a page. Leave their decorator rows unset so that
            # registration cannot overflow; assign rows only to active controls.
            item.row = row
            self.add_item(item)

        if self.page==0:
            add(self.start_button, 0); add(self.cancel_button, 0)
        elif self.page==1:
            add(_LevelSetupChannelSelect(self), 0)
            add(self.dm_button, 1); add(self.current_button, 1); add(self.next_button, 2); add(self.cancel_button, 2)
        elif self.page==2:
            add(self.edit_message_button, 0); add(self.default_message_button, 0)
            add(self.back_button, 1); add(self.next_button, 1); add(self.cancel_button, 1)
        elif self.page==3:
            add(self.image_button, 0); add(self.upload_button, 0); add(self.reset_image_button, 0)
            add(self.back_button, 1); add(self.review_button, 1); add(self.cancel_button, 1)
        else:
            self.toggle_enable_button.label = "Disable System" if self.config["enabled"] else "Enable System"
            self.toggle_enable_button.style = discord.ButtonStyle.danger if self.config["enabled"] else discord.ButtonStyle.success
            add(self.toggle_enable_button, 0); add(self.edit_settings_button, 0)
            add(self.add_reward_button, 1); add(self.remove_reward_button, 1)
            add(self.confirm_button, 2); add(self.cancel_button, 2)

    async def refresh_message(self):
        self._build()
        if self.message:
            try:
                if self.page in {3, 4}:
                    preview = await self.preview_file()
                    await self.message.edit(attachments=[preview], view=_leveling_panel(self.container(), self))
                else:
                    await self.message.edit(view=_leveling_panel(self.container(),self))
            except discord.HTTPException: pass

    async def preview_file(self):
        preview = await self.cog._rank_card(
            self.ctx.author,
            {"xp": 350, "messages": 12, "total_xp": 350},
            self.config.get("level_up_background", ""),
        )
        self.preview_filename = "levelpreview.gif" if getattr(preview, "filename", "").endswith(".gif") else "levelpreview.png"
        return discord.File(preview, filename=self.preview_filename)

    @discord.ui.button(label="Enable DMs",style=discord.ButtonStyle.success)
    async def dm_button(self,interaction,button):
        self.config["levelup_dm"]=not self.config["levelup_dm"]; button.label="Disable DMs" if self.config["levelup_dm"] else "Enable DMs"; await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Reset to current channel",style=discord.ButtonStyle.secondary)
    async def current_button(self,interaction,button):
        self.config["levelup_channel"]=None; await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Next Step",style=discord.ButtonStyle.primary)
    async def next_button(self,interaction,button):
        self.page=min(4,self.page+1); self._build(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Start Setup",style=discord.ButtonStyle.success)
    async def start_button(self,interaction,button):
        self.page=1; self._build(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Edit Text",style=discord.ButtonStyle.primary)
    async def edit_message_button(self,interaction,button): await interaction.response.send_modal(_LevelSetupMessageModal(self))

    @discord.ui.button(label="Use System Default",style=discord.ButtonStyle.secondary)
    async def default_message_button(self,interaction,button):
        self.config["levelup_message"]="{mention} leveled up to **Level {level}**! 🎉"; await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Back",style=discord.ButtonStyle.secondary)
    async def back_button(self,interaction,button):
        self.page=max(1,self.page-1); self._build(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Set Image URL",style=discord.ButtonStyle.primary)
    async def image_button(self,interaction,button): await interaction.response.send_modal(_LevelSetupImageModal(self))

    @discord.ui.button(label="Upload File",style=discord.ButtonStyle.secondary)
    async def upload_button(self,interaction,button):
        await interaction.response.send_message("Send the level-up background image as an attachment in this channel within 60 seconds.",ephemeral=True)
        def check(message):
            return message.author.id==self.owner_id and message.channel.id==self.ctx.channel.id and bool(message.attachments)
        try:
            message=await self.cog.bot.wait_for("message",check=check,timeout=60)
            attachment=next((item for item in message.attachments if item.content_type and item.content_type.startswith("image/")),None)
            if attachment is None:
                return await interaction.followup.send("Please upload a valid image file.",ephemeral=True)
            self.config["level_up_background"]=attachment.url
            # Keep the source upload message alive: the configured Discord
            # attachment URL stops working if its message is deleted.
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            await interaction.followup.send("Background image saved for review. Keep the uploaded image message so the link stays available.",ephemeral=True)
            await self.refresh_message()
        except asyncio.TimeoutError:
            await interaction.followup.send("Upload timed out. You can try again.",ephemeral=True)

    @discord.ui.button(label="Reset to Default",style=discord.ButtonStyle.secondary)
    async def reset_image_button(self,interaction,button):
        self.config["level_up_background"]=""
        self._build()
        preview = await self.preview_file()
        await interaction.response.edit_message(attachments=[preview], view=_leveling_panel(self.container(), self))

    @discord.ui.button(label="Review Configuration",style=discord.ButtonStyle.primary)
    async def review_button(self,interaction,button):
        self.page=4; self._build()
        preview = await self.preview_file()
        await interaction.response.edit_message(attachments=[preview], view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Enable System",style=discord.ButtonStyle.success)
    async def toggle_enable_button(self,interaction,button):
        self.config["enabled"]=not self.config["enabled"]; button.label="Disable System" if self.config["enabled"] else "Enable System"; button.style=discord.ButtonStyle.danger if self.config["enabled"] else discord.ButtonStyle.success
        await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Edit Settings",style=discord.ButtonStyle.secondary)
    async def edit_settings_button(self,interaction,button):
        self.page=1; self._build(); await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Add Reward",style=discord.ButtonStyle.primary)
    async def add_reward_button(self,interaction,button):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message("You need **Manage Roles** to configure level rewards.",ephemeral=True)
        reward_view = _LevelRewardAddView(self)
        await interaction.response.send_message(view=_leveling_panel(discord.ui.Container(discord.ui.TextDisplay(f"# {icon('leveling_settings')} Add Level Reward\n> Choose one role and set the level that unlocks it. Repeat this for each reward level, such as 5, 10, 20, and 30."), accent_colour=discord.Color.from_rgb(255,255,255)),reward_view),ephemeral=True)

    @discord.ui.button(label="Remove Reward",style=discord.ButtonStyle.danger)
    async def remove_reward_button(self,interaction,button):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message("You need **Manage Roles** to configure level rewards.",ephemeral=True)
        rewards=self.config.get("rewards",[])
        if not rewards:return await interaction.response.send_message("No level rewards configured.",ephemeral=True)
        options=[]
        for item in rewards[:25]:
            role=self.ctx.guild.get_role(int(item['role_id']))
            options.append(discord.SelectOption(label=f"Level {item['level']} — {role.name if role else 'Unknown role'}"[:100],value=f"{item['level']}:{item['role_id']}"))
        reward_view = _LevelRewardRemoveView(self,options)
        await interaction.response.send_message(view=_leveling_panel(discord.ui.Container(discord.ui.TextDisplay(f"# {icon('leveling_settings')} Remove Level Reward\n> Select the level reward to remove from this setup."), accent_colour=discord.Color.from_rgb(255,255,255)),reward_view),ephemeral=True)

    @discord.ui.button(label="Confirm & Apply",style=discord.ButtonStyle.success)
    async def confirm_button(self,interaction,button):
        await self.cog._set_config(self.ctx.guild.id,setup_completed=True,enabled=self.config['enabled'],levelup_enabled=self.config['levelup_enabled'],levelup_channel=self.config['levelup_channel'],levelup_dm=self.config['levelup_dm'],levelup_message=self.config['levelup_message'],level_up_background=self.config['level_up_background'] or None,rewards=self.config['rewards'])
        self.stop(); self.clear_items(); self.finished_status="saved"
        if not self.config['enabled']:
            self.cog._clear_voice_sessions(self.ctx.guild.id)
        else:
            self.cog._start_voice_sessions(self.ctx.guild)
        await interaction.response.edit_message(view=_leveling_panel(self.container(),self))

    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.danger)
    async def cancel_button(self,interaction,button):
        self.stop(); self.clear_items(); self.finished_status="closed"; await interaction.response.edit_message(view=_leveling_panel(self.container(),self))


class _VoiceXPRateModal(discord.ui.Modal, title="Voice XP Rate"):
    def __init__(self, view):
        super().__init__()
        self.voice_view = view
        self.rate = discord.ui.TextInput(label="XP earned per minute", default=str(view.rate), min_length=1, max_length=3, placeholder="10")
        self.add_item(self.rate)

    async def on_submit(self, interaction):
        try:
            rate = int(str(self.rate.value))
            if not 1 <= rate <= 100:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter a rate between 1 and 100 XP per minute.", ephemeral=True)
        self.voice_view.rate = rate
        await self.voice_view.cog._set_config(self.voice_view.guild.id, voice_xp_per_min=rate)
        await interaction.response.send_message(f"Voice XP rate set to {rate} per minute.", ephemeral=True)
        await self.voice_view.refresh()


class VoiceXPSetupView(discord.ui.View):
    def __init__(self, cog, ctx, cfg):
        super().__init__(timeout=900)
        self.cog, self.guild, self.owner_id = cog, ctx.guild, ctx.author.id
        self.message = None
        self.setup_completed = bool(cfg.get("setup_completed", False))
        self.leveling_enabled = bool(cfg.get("enabled", False))
        self.voice_enabled = bool(cfg.get("voice_enabled", False))
        self.rate = int(cfg.get("voice_xp_per_min", 10))
        self._sync_button()

    def _sync_button(self):
        self.toggle_voice.label = "Disable Voice XP" if self.voice_enabled else "Enable Voice XP"
        self.toggle_voice.style = discord.ButtonStyle.danger if self.voice_enabled else discord.ButtonStyle.success

    def container(self):
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255, 255, 255))
        container.add_item(discord.ui.TextDisplay(
            f"# {icon('leveling_voice')} Voice XP Setup\n> Configure XP earned while members are in voice channels."
        ))
        container.add_item(discord.ui.Separator())
        system_status = "Ready" if self.setup_completed and self.leveling_enabled else "Run `levelsetup` and apply it first"
        container.add_item(discord.ui.TextDisplay("\n".join((
            f"**Leveling Setup:** {system_status}",
            f"**Voice XP:** {'Enabled' if self.voice_enabled else 'Disabled'}",
            f"**Rate:** {self.rate} XP per minute",
            "Voice time tracking begins after the main leveling setup is applied.",
        ))))
        return container

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who opened this setup can use these controls.", ephemeral=True)
        return False

    async def refresh(self):
        if self.message:
            await self.message.edit(view=_leveling_panel(self.container(), self))

    @discord.ui.button(label="Enable Voice XP", style=discord.ButtonStyle.success)
    async def toggle_voice(self, interaction, button):
        cfg = await self.cog._config(self.guild.id)
        self.setup_completed = bool(cfg.get("setup_completed", False))
        self.leveling_enabled = bool(cfg.get("enabled", False))
        self.voice_enabled = bool(cfg.get("voice_enabled", False))
        self.rate = int(cfg.get("voice_xp_per_min", 10))
        if not self.setup_completed or not self.leveling_enabled:
            await interaction.response.edit_message(view=_leveling_panel(self.container(), self))
            if not self.setup_completed:
                return await interaction.followup.send("Run `levelsetup` and click **Confirm & Apply** first.", ephemeral=True)
            return await interaction.followup.send("Leveling setup is applied, but leveling is disabled. Enable it with `levelsetup on` first.", ephemeral=True)
        self.voice_enabled = not self.voice_enabled
        await self.cog._set_config(self.guild.id, voice_enabled=self.voice_enabled)
        self._sync_button()
        await interaction.response.edit_message(view=_leveling_panel(self.container(), self))

    @discord.ui.button(label="Set XP Rate", style=discord.ButtonStyle.primary)
    async def set_rate(self, interaction, button):
        await interaction.response.send_modal(_VoiceXPRateModal(self))


class Leveling(commands.Cog):
    """Message and voice XP, rank cards, reward roles and configuration."""

    def __init__(self, bot):
        self.bot = bot
        self.users = bot.db.db["leveling_users"]
        self.settings = bot.db.db["leveling_settings"]
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._voice_sessions: dict[tuple[int, int], float] = {}
        self._xp_clock: dict[tuple[int, int], float] = {}
        self._config_cache: dict[int, tuple[float, dict]] = {}
        self._config_locks: dict[int, asyncio.Lock] = {}
        # Level XP is written through to the database, while this bounded cache
        # avoids an extra remote read every time a member earns XP or opens a profile.
        self._user_cache: OrderedDict[tuple[int, int], tuple[float, dict]] = OrderedDict()
        self._message_xp_semaphore = asyncio.Semaphore(8)
        self._last_message_error_log = 0.0
        self._last_config_error_log = 0.0

    async def cog_load(self):
        await self.users.create_index([("guild_id", 1), ("xp", -1)])
        await self.users.create_index([("guild_id", 1), ("voice_secs", -1)])
        await self.settings.create_index("guild_id")

    async def _config(self, guild_id: int) -> dict:
        defaults = {
            "_id": str(guild_id), "guild_id": guild_id, "enabled": False,
            "setup_completed": False,
            "xp_min": 15, "xp_max": 40, "cooldown": 60,
            "voice_enabled": False, "voice_xp_per_min": 10,
            "levelup_enabled": True, "levelup_channel": None, "levelup_dm": False,
            "level_up_background": None,
            "levelup_message": "GG {mention}, you reached level **{level}**!",
            "blacklist_channels": [], "blacklist_roles": [], "blacklist_users": [],
            "multipliers": [], "rewards": [], "streaks": {},
        }
        now = time.monotonic()
        cached = self._config_cache.get(guild_id)
        if cached and now - cached[0] < _LEVEL_CONFIG_CACHE_TTL:
            return copy.deepcopy(cached[1])
        lock = self._config_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._config_cache.get(guild_id)
            if cached and now - cached[0] < _LEVEL_CONFIG_CACHE_TTL:
                return copy.deepcopy(cached[1])
            try:
                stored = await self.settings.find_one({"_id": str(guild_id)})
                config = {**defaults, **stored} if stored else defaults
            except Exception:
                now = time.monotonic()
                if now - self._last_config_error_log >= 30:
                    self._last_config_error_log = now
                    log.warning("Could not load leveling settings for guild %s; using cached/default settings temporarily", guild_id, exc_info=True)
                config = cached[1] if cached else defaults
            self._config_cache[guild_id] = (time.monotonic(), copy.deepcopy(config))
            return copy.deepcopy(config)

    def _clear_voice_sessions(self, guild_id: int):
        for key in [key for key in self._voice_sessions if key[0] == guild_id]:
            self._voice_sessions.pop(key, None)

    def _start_voice_sessions(self, guild):
        now = time.time()
        for member in guild.members:
            voice = member.voice
            if voice and voice.channel and not member.bot:
                self._voice_sessions[(guild.id, member.id)] = now

    async def _set_config(self, guild_id: int, **values):
        await self.settings.update_one({"_id": str(guild_id)}, {
            "$set": {"guild_id": guild_id, **values},
            "$setOnInsert": {"_id": str(guild_id)},
        }, upsert=True)
        cached = self._config_cache.get(guild_id)
        if cached:
            config = copy.deepcopy(cached[1])
            config.update({"guild_id": guild_id, **values})
            self._config_cache[guild_id] = (time.monotonic(), config)

    async def _user(self, guild_id: int, user_id: int) -> dict:
        key = (guild_id, user_id)
        now = time.monotonic()
        cached = self._user_cache.get(key)
        if cached and now - cached[0] < _LEVEL_USER_CACHE_TTL:
            self._user_cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        user = await self.users.find_one({"_id": f"{guild_id}:{user_id}"}) or {
            "_id": f"{guild_id}:{user_id}", "guild_id": guild_id,
            "user_id": user_id, "xp": 0, "total_xp": 0, "messages": 0,
            "voice_secs": 0, "last_xp_at": 0, "last_streak_day": "",
            "streak": 0, "best_streak": 0,
        }
        self._cache_user(key, user, now)
        return copy.deepcopy(user)

    def _cache_user(self, key: tuple[int, int], user: dict, now: float | None = None):
        self._user_cache[key] = (time.monotonic() if now is None else now, copy.deepcopy(user))
        self._user_cache.move_to_end(key)
        while len(self._user_cache) > _LEVEL_USER_CACHE_MAX:
            self._user_cache.popitem(last=False)

    async def _save_user(self, guild_id: int, user_id: int, values: dict):
        await self.users.update_one({"_id": f"{guild_id}:{user_id}"}, {
            "$set": {"guild_id": guild_id, "user_id": user_id, **values},
            "$setOnInsert": {"_id": f"{guild_id}:{user_id}"},
        }, upsert=True)
        key = (guild_id, user_id)
        cached = self._user_cache.get(key)
        if cached:
            updated = copy.deepcopy(cached[1])
            updated.update(values)
            self._cache_user(key, updated)

    async def _ensure_user(self, guild_id: int, user_id: int) -> dict:
        user = await self.users.find_one({"_id": f"{guild_id}:{user_id}"})
        if user:
            return user
        user = await self._user(guild_id, user_id)
        await self._save_user(guild_id, user_id, {key: value for key, value in user.items()
                                                   if key not in {"_id", "guild_id", "user_id"}})
        return user

    async def _is_blocked(self, message, cfg):
        if str(message.channel.id) in set(map(str, cfg.get("blacklist_channels", []))):
            return True
        if str(message.author.id) in set(map(str, cfg.get("blacklist_users", []))):
            return True
        roles = {str(r.id) for r in getattr(message.author, "roles", ())}
        return bool(roles.intersection(set(map(str, cfg.get("blacklist_roles", [])))))

    async def _award_message_xp(self, message):
        from core.bot import current_guild_prefix
        active_prefix = await current_guild_prefix(self.bot, message.guild.id)
        if message.content.startswith(active_prefix) or (self.bot.user and message.content.startswith((f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"))):
            return
        cfg = await self._config(message.guild.id)
        if not cfg.get("setup_completed", False) or not cfg.get("enabled", False) or await self._is_blocked(message, cfg):
            return
        key = (message.guild.id, message.author.id)
        cooldown = max(0, int(cfg.get("cooldown", 60)))
        now = time.time()
        last_award = self._xp_clock.get(key, 0.0)
        if now - last_award < cooldown:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.time()
            last_award = self._xp_clock.get(key, 0.0)
            if now - last_award < cooldown:
                return
            user = await self._user(*key)
            stored_last_award = float(user.get("last_xp_at", 0))
            if now - stored_last_award < cooldown:
                self._xp_clock[key] = stored_last_award
                return
            gained = random.randint(max(0, int(cfg.get("xp_min", 15))), max(0, int(cfg.get("xp_max", 40))))
            multipliers = cfg.get("multipliers", [])
            for item in multipliers:
                if item.get("type") == "channel" and str(message.channel.id) == str(item.get("target_id")):
                    gained = round(gained * float(item.get("value", 1)))
                if item.get("type") == "role" and any(str(r.id) == str(item.get("target_id")) for r in message.author.roles):
                    gained = round(gained * float(item.get("value", 1)))
            old_level, _ = level_from_xp(int(user.get("xp", 0)))
            new_xp = int(user.get("xp", 0)) + gained
            new_level, _ = level_from_xp(new_xp)
            today = datetime.now(timezone.utc).date().isoformat()
            streak = int(user.get("streak", 0))
            if user.get("last_streak_day") != today:
                from datetime import date, timedelta
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                streak = streak + 1 if user.get("last_streak_day") == yesterday else 1
            history=list(user.get("xp_history", [])); history.append({"at":int(now),"xp":gained,"total":new_xp}); history=history[-25:]
            await self._save_user(*key, {
                "xp": new_xp, "total_xp": int(user.get("total_xp", user.get("xp", 0))) + gained,
                "messages": int(user.get("messages", 0)) + 1, "last_xp_at": now,
                "streak": streak, "best_streak": max(streak, int(user.get("best_streak", 0))),
                "last_streak_day": today, "xp_history": history,
            })
            self._xp_clock[key] = now
        if new_level > old_level:
            await self._assign_rewards(message.guild, message.author, cfg, new_level)
        if new_level > old_level and cfg.get("levelup_enabled", True):
            text = str(cfg.get("levelup_message") or "GG {mention}, you reached level **{level}**!")
            text = text.replace("{mention}", message.author.mention).replace("{username}", message.author.display_name).replace("{level}", str(new_level)).replace("{xp}", str(new_xp)).replace("{newlevel}", str(new_level))
            allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
            if cfg.get("levelup_dm"):
                try:
                    await message.author.send(text, allowed_mentions=allowed)
                except discord.HTTPException:
                    pass
            destination = message.guild.get_channel(int(cfg["levelup_channel"])) if cfg.get("levelup_channel") else message.channel
            if destination:
                try:
                    await destination.send(text, allowed_mentions=allowed)
                except discord.HTTPException:
                    pass

    async def _assign_rewards(self, guild, member, cfg, level):
        for item in cfg.get("rewards", []):
            if int(item.get("level", 0)) <= level:
                role = guild.get_role(int(item.get("role_id", 0)))
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Level reward")
                    except discord.HTTPException:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild and not message.author.bot:
            try:
                async with self._message_xp_semaphore:
                    await self._award_message_xp(message)
            except Exception:
                now = time.monotonic()
                if now - self._last_message_error_log >= 30:
                    self._last_message_error_log = now
                    log.exception("Could not process leveling message")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            cfg = await self._config(guild.id)
            if not cfg.get("setup_completed", False) or not cfg.get("enabled", False):
                self._clear_voice_sessions(guild.id)
                continue
            self._start_voice_sessions(guild)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not member.guild:
            return
        key = (member.guild.id, member.id)
        cfg = await self._config(member.guild.id)
        if not cfg.get("setup_completed", False) or not cfg.get("enabled", False):
            self._voice_sessions.pop(key, None)
            return
        if before.channel is None and after.channel is not None:
            self._voice_sessions[key] = time.time()
            return
        if before.channel is not None and (after.channel is None or before.channel != after.channel):
            started = self._voice_sessions.pop(key, None)
            if started is None:
                return
            seconds = int(time.time() - started)
            user = await self._user(*key)
            total = int(user.get("voice_secs", 0)) + seconds
            gained = (seconds // 60) * int(cfg.get("voice_xp_per_min", 10)) if cfg.get("voice_enabled") else 0
            xp = int(user.get("xp", 0)) + gained
            await self._save_user(*key, {"voice_secs": total, "xp": xp, "total_xp": int(user.get("total_xp", user.get("xp", 0))) + gained})
            if after.channel is not None:
                self._voice_sessions[key] = time.time()

    async def _profile_container(self, guild, member, user, page="overview", disabled=False):
        xp = int(user.get("xp", 0))
        level, remaining = level_from_xp(xp)
        needed = xp_for_level(level)
        xp_in_level = remaining
        text_rank = 1 + await self.users.count_documents({"guild_id": guild.id, "xp": {"$gt": xp}})
        voice_secs = int(user.get("voice_secs", 0))
        started = self._voice_sessions.get((guild.id, member.id))
        if started is not None:
            voice_secs += max(0, int(time.time() - started))
        voice_rank = 1 + await self.users.count_documents({"guild_id": guild.id, "voice_secs": {"$gt": voice_secs}})
        avatar = discord.ui.Thumbnail(media=member.display_avatar.url)
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255, 255, 255))
        if page == "history":
            last_day = user.get("last_streak_day") or "N/A"
            header = "\n".join((
                f"# {icon('leveling_chart')} Profile Statistics",
                f"## {icon('leveling_leaderboard')} History",
                f"- **User :** <@{member.id}>",
                f"- **Current Streak :** {int(user.get('streak', 0))} days",
                f"- **Best Streak :** {int(user.get('best_streak', 0))} days",
                f"- **Last Active :** {last_day}",
            ))
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(header), accessory=avatar))
            container.add_item(discord.ui.TextDisplay("\n".join((
                f"**XP Breakdown:** {remaining:,} / {needed:,}",
                f"> Total XP: {xp:,} | Level: {level}",
                f"> Messages: {int(user.get('messages', 0)):,} | Voice: {fmt_duration(voice_secs)}",
            ))))
        else:
            pct = int(xp_in_level / max(1, needed) * 100)
            joined = f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown"
            header = "\n".join((
                f"# {icon('leveling_chart')} Profile Statistics",
                f"## {icon('leveling_level')} Level : {level}",
                f"- **User :** <@{member.id}>",
                f"- **Text Rank :** {_level_badge(text_rank)}",
                f"- **Voice Rank :** {_level_badge(voice_rank)}",
                f"- **XP :** {remaining:,} / {needed:,}",
            ))
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(header), accessory=avatar))
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("\n".join((
                f"**Progress:** `{_progress_bar(xp_in_level, needed)}` {pct}%",
                f"> Messages: {int(user.get('messages', 0)):,} | Total XP: {xp:,}",
                f"> Voice: {fmt_duration(voice_secs)} | Joined: {joined}",
                f"> Status: {str(member.status).title() if member.status else 'Offline'}",
            ))))
        return container

    async def _download_background(self, url):
        try:
            timeout = aiohttp.ClientTimeout(total=15, connect=6)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Zyven Leveling/1.0"}) as session:
                response = await safe_request(session, url)
                async with response:
                    if response.status != 200:
                        raise ValueError(f"The image host returned HTTP {response.status}.")
                    content_length = response.headers.get("Content-Length")
                    try:
                        declared_length = int(content_length) if content_length else None
                    except ValueError as error:
                        raise ValueError("The image host returned an invalid content length.") from error
                    if declared_length is not None and declared_length > _MAX_BACKGROUND_BYTES:
                        raise ValueError("The image must be 8 MB or smaller.")
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                        raise ValueError("The URL must return a PNG, JPG, GIF, or WebP image.")
                    data = await response.content.read(_MAX_BACKGROUND_BYTES + 1)
                    if len(data) > _MAX_BACKGROUND_BYTES:
                        raise ValueError("The image must be 8 MB or smaller.")
        except ValueError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise ValueError("The bot couldn't reach that image URL.") from error
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                if image.width * image.height > 40_000_000:
                    raise ValueError("The image dimensions are too large.")
                image.verify()
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("The URL did not return a readable image.") from error
        if image_format not in {"PNG", "JPEG", "GIF", "WEBP"}:
            raise ValueError("Use a PNG, JPG, GIF, or WebP image.")
        return data, image_format

    async def _rank_card(self, member, user, background_url=""):
        level, remaining = level_from_xp(int(user.get("xp", 0)))
        needed = xp_for_level(level)
        avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
        background_bytes = None
        if background_url:
            try:
                background_bytes, _ = await self._download_background(background_url)
            except ValueError as error:
                log.warning("Could not load leveling background for guild member %s: %s", member.id, error)

        def render():
            try:
                source = Image.open(io.BytesIO(background_bytes)) if background_bytes else Image.open("assets/levelbg.png")
                is_animated = bool(background_bytes and getattr(source, "is_animated", False) and getattr(source, "n_frames", 1) > 1)
                if is_animated:
                    stride = max(1, (source.n_frames + 23) // 24)
                    background_frames, durations = [], []
                    for index in range(0, source.n_frames, stride):
                        source.seek(index)
                        background_frames.append(source.convert("RGBA").resize((661, 254)))
                        durations.append(max(20, int(source.info.get("duration", 100)) * stride))
                else:
                    background_frames = [source.convert("RGBA").resize((661, 254))]
                    durations = [100]
                loop = int(source.info.get("loop", 0)) if is_animated else 0
            except Exception:
                background_frames = [Image.new("RGBA", (661, 254), "#17151d")]
                durations, loop, is_animated = [100], 0, False

            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
                small = ImageFont.truetype("DejaVuSans.ttf", 18)
                heading = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
                level_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
            except OSError:
                font, small = ImageFont.load_default(), ImageFont.load_default()
                heading, level_font = font, font
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((110, 110))
            mask = Image.new("L", avatar.size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 109, 109), fill=255)
            progress = min(1, remaining / max(1, needed))
            rendered_frames = []
            for background in background_frames:
                canvas = Image.alpha_composite(background, Image.new("RGBA", background.size, (0, 0, 0, 115)))
                canvas.paste(avatar, (500, 70), mask)
                draw = ImageDraw.Draw(canvas)
                draw.ellipse((497, 67, 613, 183), outline="#ffffff", width=5)
                draw.rounded_rectangle((40, 40, 52, 210), radius=10, fill=(255, 255, 255, 51))
                fill_top = 40 + int(170 * (1 - progress))
                if progress:
                    draw.rounded_rectangle((40, fill_top, 52, 210), radius=10, fill="#ffffff")
                draw.text((70, 60), "YOUR RANK", font=heading, fill="white")
                draw.text((70, 95), member.display_name[:24], font=font, fill="white")
                draw.text((70, 150), f"LVL {level}", font=level_font, fill="white")
                draw.text((70, 185), f"{remaining:,} / {needed:,} XP", font=small, fill="#dddddd")
                rendered_frames.append(canvas)

            out = io.BytesIO()
            if is_animated:
                rendered_frames[0].save(out, format="GIF", save_all=True, append_images=rendered_frames[1:], duration=durations, loop=loop, disposal=2, optimize=False)
                out.filename = "rankcard.gif"
            else:
                rendered_frames[0].save(out, format="PNG")
                out.filename = "rankcard.png"
            out.seek(0)
            return out

        return await asyncio.to_thread(render)

    @commands.command(name="rank", aliases=["level", "xp"])
    async def rank(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        user = await self._user(ctx.guild.id, member.id)
        cfg = await self._config(ctx.guild.id)
        card = await self._rank_card(member, user, cfg.get("level_up_background", ""))
        xp = int(user.get("xp", 0)); level, xp_in_level = level_from_xp(xp); needed = xp_for_level(level)
        rank = 1 + await self.users.count_documents({"guild_id": ctx.guild.id, "xp": {"$gt": xp}})
        rewards = [item for item in cfg.get("rewards", []) if int(item.get("level", 0)) <= level]
        reward = max(rewards, key=lambda item: int(item.get("level", 0)), default=None)
        reward_role = ctx.guild.get_role(int(reward["role_id"])) if reward else None
        if reward_role and _unsafe_reward_permissions(reward_role):
            reward_role = None
        voice_secs = int(user.get("voice_secs", 0))
        started = self._voice_sessions.get((ctx.guild.id, member.id))
        if started is not None: voice_secs += max(0, int(time.time() - started))
        bar = _progress_bar(xp_in_level, needed)
        details = "\n".join((
            f"# {icon('leveling_chart')} Level Statistics",
            f"## {icon('leveling_level')} Level : {level}",
            f"- **User :** <@{member.id}>",
            f"- **Rank :** #{rank}",
            f"- **XP :** {xp_in_level:,} / {needed:,}",
            f"- **Role :** {reward_role.name if reward_role else 'None'}",
            f"\n**Progress:** `{bar}` {int(xp_in_level / max(1, needed) * 100)}%",
            f"> Text XP: {xp:,} | Voice: {fmt_duration(voice_secs)} | Messages: {int(user.get('messages', 0)):,}",
        ))
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255,255,255))
        filename = getattr(card, "filename", "rankcard.png")
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{filename}")))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(details))
        sent = await ctx.reply(file=discord.File(card, filename=filename), mention_author=False)
        await sent.edit(content=None, embeds=[], attachments=sent.attachments, view=_leveling_panel(container))

    @commands.command(name="profile", aliases=["pr"])
    async def profile(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        if member.bot:
            container = discord.ui.Container(discord.ui.TextDisplay(f"## {icon('leveling_cross')} Bots don't have profiles!"), accent_colour=discord.Color.from_rgb(255,255,255))
            return await ctx.reply(view=_leveling_panel(container), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        await self._ensure_user(ctx.guild.id, member.id)
        view = LevelProfileView(self, ctx.guild, member, ctx.author.id)
        view.message = await ctx.reply(view=_leveling_panel(await self._profile_container(ctx.guild, member, await self._user(ctx.guild.id, member.id), "overview"), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    # The bot already owns `leaderboard` (alias `lb`) for its message/voice/
    # invite boards, so leveling keeps its separate `levelboard` command name.
    @commands.command(name="levelboard", aliases=["levelingboard"])
    async def leaderboard(self, ctx, mode: str = "xp"):
        mode = mode.lower()
        field = {"xp": "xp", "level": "xp", "message": "messages", "messages": "messages", "m": "messages", "voice": "voice_secs", "vc": "voice_secs", "voice_secs": "voice_secs"}.get(mode)
        if not field:
            return await self._reply(ctx, "Choose `xp`, `messages`, or `voice`.", title="Leaderboard Mode")
        caller = await self._ensure_user(ctx.guild.id, ctx.author.id)
        rows = [row async for row in await self.users.find({"guild_id": ctx.guild.id})]
        rows.sort(key=lambda row: (int(row.get(field, 0)), int(row.get("xp", 0))), reverse=True)
        mode = "messages" if mode in {"message", "messages", "m"} else "voice" if mode in {"voice", "vc", "voice_secs"} else "xp"
        view=LevelLeaderboardView(ctx.guild, rows, mode, ctx.author.id, caller)
        view.message=await ctx.reply(view=_leveling_panel(view.container(), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _admin(self, ctx):
        if not ctx.author.guild_permissions.manage_guild:
            await self._reply(ctx, "You need **Manage Server** permission to use this command.", title="Permission Required", delete_after=5)
            return False
        return True

    async def _reply(self, ctx, text: str, *, title: str = "Leveling", **kwargs):
        embed = discord.Embed(title=title, description=str(text), color=discord.Color.from_rgb(255, 255, 255))
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        return await ctx.reply(embed=embed, **kwargs)

    @commands.command(name="givexp")
    async def givexp(self, ctx, member: discord.Member, amount: int):
        if not await self._admin(ctx): return
        user = await self._user(ctx.guild.id, member.id); xp = max(0, int(user.get("xp", 0)) + amount)
        await self._save_user(ctx.guild.id, member.id, {"xp": xp, "total_xp": max(0, int(user.get("total_xp", user.get("xp", 0))) + amount)})
        await self._reply(ctx, f"✅ Updated {member.mention}'s XP by **{amount:+,}**. New XP: **{xp:,}**.")

    @commands.command(name="setlevel")
    async def setlevel(self, ctx, member: discord.Member, level: int):
        if not await self._admin(ctx): return
        if level < 0 or level > 10000: return await self._reply(ctx, "Level must be between 0 and 10000.", title="Invalid Level")
        xp = sum(xp_for_level(n) for n in range(level))
        await self._save_user(ctx.guild.id, member.id, {"xp": xp, "total_xp": xp})
        await self._reply(ctx, f"✅ Set {member.mention} to level **{level}**.")

    @commands.command(name="resetxp")
    async def resetxp(self, ctx, target: str):
        if not await self._admin(ctx): return
        if target.casefold() == "all":
            progress = await ctx.reply(
                embed=discord.Embed(
                    title=f"{icon('loading')} Resetting Level XP",
                    description="Resetting XP for every member in this server…",
                    color=discord.Color.from_rgb(255, 255, 255),
                ),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            result = await self.users.update_many(
                {"guild_id": ctx.guild.id}, {"$set": {"xp": 0, "total_xp": 0}}
            )
            return await progress.edit(embed=discord.Embed(
                title="Level XP Reset",
                description=f"Reset XP for **{result.matched_count:,}** member record(s) in this server.",
                color=discord.Color.from_rgb(255, 255, 255),
            ))
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.MemberNotFound:
            return await self._reply(ctx, "Use `resetxp @member` or `resetxp all`.", title="Member Not Found")
        await self._save_user(ctx.guild.id, member.id, {"xp": 0, "total_xp": 0})
        await self._reply(ctx, f"✅ Reset {member.mention}'s XP.")

    @commands.command(name="levelsetup", aliases=["levelingsetup"])
    async def setup(self, ctx, action: str = None):
        if not await self._admin(ctx): return
        if action and action.lower() in {"on", "enable", "true"}:
            cfg = await self._config(ctx.guild.id)
            if not cfg.get("setup_completed", False):
                return await self._reply(ctx, "Run `levelsetup` and click **Confirm & Apply** before enabling leveling.", title="Setup Required")
            await self._set_config(ctx.guild.id, enabled=True); self._start_voice_sessions(ctx.guild); return await self._reply(ctx, "✅ Leveling enabled.")
        if action and action.lower() in {"off", "disable", "false"}:
            await self._set_config(ctx.guild.id, enabled=False); self._clear_voice_sessions(ctx.guild.id); return await self._reply(ctx, "✅ Leveling disabled.")
        cfg = await self._config(ctx.guild.id)
        view=LevelSetupView(self,ctx,cfg)
        preview = await view.preview_file()
        sent = await ctx.reply(file=preview, mention_author=False)
        view.message = await sent.edit(content=None, embeds=[], attachments=sent.attachments,
                                       view=_leveling_panel(view.container(), view))

    @commands.command(name="xpconfig")
    async def xpconfig(self, ctx, option: str = "show", *, value: str = None):
        if option.lower() in {"set", "enable", "disable"} and not await self._admin(ctx): return
        cfg = await self._config(ctx.guild.id)
        if option == "enable":
            if not cfg.get("setup_completed", False):
                return await self._reply(ctx, "Run `levelsetup` and click **Confirm & Apply** before enabling leveling.", title="Setup Required")
            await self._set_config(ctx.guild.id, enabled=True)
            self._start_voice_sessions(ctx.guild)
        elif option == "disable":
            await self._set_config(ctx.guild.id, enabled=False); self._clear_voice_sessions(ctx.guild.id)
        elif option == "channel":
            channel = ctx.message.channel_mentions[0] if ctx.message.channel_mentions else ctx.channel
            await self._set_config(ctx.guild.id, levelup_channel=channel.id)
        elif option == "message" and value:
            await self._set_config(ctx.guild.id, levelup_message=value[:1000])
        cfg = await self._config(ctx.guild.id)
        e = discord.Embed(title="Leveling Configuration", color=discord.Color.from_rgb(255, 255, 255))
        e.description = f"Setup complete: **{cfg.get('setup_completed', False)}**\nEnabled: **{cfg.get('enabled', False)}**\nXP range: **{cfg.get('xp_min', 15)}–{cfg.get('xp_max', 40)}**\nCooldown: **{cfg.get('cooldown', 60)}s**\nVoice XP: **{cfg.get('voice_enabled', False)}**"
        await ctx.reply(embed=e)

    @commands.command(name="xprange")
    async def xprange(self, ctx, minimum: int = None, maximum: int = None):
        if minimum is None or maximum is None:
            cfg = await self._config(ctx.guild.id); return await self._reply(ctx, f"XP range: **{cfg['xp_min']}–{cfg['xp_max']}** per message.", title="XP Range")
        if not await self._admin(ctx): return
        if minimum < 0 or maximum < minimum or maximum > 1000: return await self._reply(ctx, "Provide a valid range (0–1000).", title="Invalid XP Range")
        await self._set_config(ctx.guild.id, xp_min=minimum, xp_max=maximum)
        await self._reply(ctx, f"✅ XP range set to **{minimum}–{maximum}**.", title="XP Range Updated")

    @commands.command(name="xpcooldown")
    async def xpcooldown(self, ctx, seconds: int = None):
        cfg = await self._config(ctx.guild.id)
        if seconds is None: return await self._reply(ctx, f"XP cooldown: **{cfg.get('cooldown', 60)} seconds**.", title="XP Cooldown")
        if not await self._admin(ctx): return
        if not 0 <= seconds <= 3600: return await self._reply(ctx, "Cooldown must be between 0 and 3600 seconds.", title="Invalid Cooldown")
        await self._set_config(ctx.guild.id, cooldown=seconds); await self._reply(ctx, f"✅ XP cooldown set to **{seconds}s**.", title="XP Cooldown Updated")

    @commands.command(name="xpblacklist")
    async def xpblacklist(self, ctx, action: str = "list", kind: str = None, target: str = None):
        cfg = await self._config(ctx.guild.id)
        lists = {"channel": "blacklist_channels", "role": "blacklist_roles", "user": "blacklist_users"}
        if action in {"add", "remove", "clear"} and not await self._admin(ctx): return
        if action == "clear":
            await self._set_config(ctx.guild.id, **{key: [] for key in lists.values()}); return await self._reply(ctx, "✅ XP blacklist cleared.", title="XP Blacklist")
        if action in {"add", "remove"}:
            if kind not in lists or not target: return await self._reply(ctx, "Usage: `xpblacklist add|remove channel|role|user <mention or ID>`", title="XP Blacklist Usage")
            mention = {"channel": ctx.message.channel_mentions, "role": ctx.message.role_mentions, "user": ctx.message.mentions.users}.get(kind, [])
            ident = str(mention[0].id if mention else target.strip("<#@!&>"))
            values = list(map(str, cfg.get(lists[kind], [])))
            if action == "add" and ident not in values: values.append(ident)
            if action == "remove": values = [item for item in values if item != ident]
            await self._set_config(ctx.guild.id, **{lists[kind]: values}); return await self._reply(ctx, f"✅ {kind.title()} updated in XP blacklist.", title="XP Blacklist Updated")
        cfg = await self._config(ctx.guild.id)
        lines = [f"**{kind.title()}s:** " + (", ".join(f"`{x}`" for x in cfg.get(key, [])) or "None") for kind, key in lists.items()]
        await ctx.reply(embed=discord.Embed(title="XP Blacklist", description="\n".join(lines), color=discord.Color.from_rgb(255, 255, 255)))

    @commands.command(name="multiplier", aliases=["mult"])
    async def multiplier(self, ctx, action: str = "list", kind: str = None, target: str = None, value: float = 1.0):
        cfg = await self._config(ctx.guild.id); entries = list(cfg.get("multipliers", []))
        if action in {"set", "remove"} and not await self._admin(ctx): return
        if action in {"set", "remove"}:
            if kind not in {"channel", "role"} or not target: return await self._reply(ctx, "Usage: `multiplier set channel|role <mention or ID> <value>`", title="Multiplier Usage")
            mentions = ctx.message.channel_mentions if kind == "channel" else ctx.message.role_mentions
            ident = str(mentions[0].id if mentions else target.strip("<#@&>"))
            entries = [x for x in entries if not (x.get("type") == kind and str(x.get("target_id")) == ident)]
            if action == "set":
                if not 0.1 <= value <= 10: return await self._reply(ctx, "Multiplier must be between 0.1x and 10x.", title="Invalid Multiplier")
                entries.append({"type": kind, "target_id": ident, "value": value})
            await self._set_config(ctx.guild.id, multipliers=entries); return await self._reply(ctx, "✅ XP multiplier updated.", title="XP Multipliers")
        lines = [f"{x['type'].title()} <{x['type']}:{x['target_id']}> — **{x['value']}x**" for x in entries]
        await ctx.reply(embed=discord.Embed(title="XP Multipliers", description="\n".join(lines) or "No multipliers configured.", color=discord.Color.from_rgb(255, 255, 255)))

    @commands.command(name="rewards", aliases=["rl"])
    async def rewards(self, ctx, action: str = "list", level: int = None, roles: commands.Greedy[discord.Role] = None):
        cfg = await self._config(ctx.guild.id); items = list(cfg.get("rewards", []))
        if action in {"add", "remove"} and not await self._admin(ctx): return
        if action in {"add", "remove"}:
            roles = roles or []
            if level is None or not roles: return await self._reply(ctx, "Usage: `rewards add|remove <level> @role [@role ...]`", title="Reward Usage")
            if not 1 <= level <= 1000: return await self._reply(ctx, "Reward level must be between 1 and 1000.", title="Invalid Reward Level")
            if action == "add":
                unsafe_roles = [role for role in roles if _unsafe_reward_permissions(role)]
                if unsafe_roles:
                    return await self._reply(ctx, "Administrator or moderation permission roles can't be used as rewards: " + ", ".join(role.mention for role in unsafe_roles), title="Unsafe Reward Role")
                unassignable = [role for role in roles if not role.is_assignable()]
                if unassignable:
                    return await self._reply(ctx, "These roles are above my role or can't be managed: " + ", ".join(role.mention for role in unassignable), title="Unmanageable Reward Role")
            role_ids = {str(role.id) for role in roles}
            items = [x for x in items if not (int(x.get("level", -1)) == level and str(x.get("role_id")) in role_ids)]
            if action == "add": items.extend({"level": level, "role_id": role.id} for role in roles)
            await self._set_config(ctx.guild.id, rewards=items); return await self._reply(ctx, "✅ Level reward updated.", title="Level Rewards")
        desc = "\n".join(f"Level **{x['level']}** → <@&{x['role_id']}>" for x in sorted(items, key=lambda x: int(x['level']))) or "No rewards configured."
        await ctx.reply(embed=discord.Embed(title="Level Rewards", description=desc, color=discord.Color.from_rgb(255, 255, 255)))

    @commands.command(name="claimreward", aliases=["claim"])
    async def claimreward(self, ctx):
        user = await self._user(ctx.guild.id, ctx.author.id); level, _ = level_from_xp(user.get("xp", 0))
        cfg = await self._config(ctx.guild.id); eligible = [x for x in cfg.get("rewards", []) if int(x.get("level", 0)) <= level]
        roles = [ctx.guild.get_role(int(x["role_id"])) for x in eligible]
        roles = [r for r in roles if r and not _unsafe_reward_permissions(r) and r not in ctx.author.roles]
        if not roles: return await self._reply(ctx, "No new reward roles are available to claim.", title="Level Rewards")
        try:
            await ctx.author.add_roles(*roles, reason="Claimed leveling rewards")
            await self._reply(ctx, "✅ Claimed: " + ", ".join(r.mention for r in roles), title="Level Rewards")
        except discord.HTTPException: await self._reply(ctx, "I could not assign the reward roles. Check my role position and permissions.", title="Level Rewards")

    @commands.command(name="streak", aliases=["mystreak"])
    async def streak(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        user = await self._ensure_user(ctx.guild.id, member.id)
        last_day = user.get("last_streak_day")
        header = "\n".join((
            f"## {icon('leveling_firenation')} {member.display_name}'s Streak",
            f"- **User :** <@{member.id}>",
            f"- **Current Streak :** {int(user.get('streak', 0))} day(s)",
            f"- **Best Streak :** {int(user.get('best_streak', 0))} day(s)",
            f"- **Last Active :** {last_day or '`Never`'}",
        ))
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(255,255,255))
        container.add_item(discord.ui.Section(discord.ui.TextDisplay(header), accessory=discord.ui.Thumbnail(member.display_avatar.url)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"{icon('leveling_fire')} Your streak increases when you earn XP on a new day."))
        await ctx.reply(view=_leveling_panel(container), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="serverstats", aliases=["ss"])
    async def serverstats(self, ctx):
        rows = [row async for row in await self.users.find({"guild_id": ctx.guild.id})]
        e = discord.Embed(title=f"{ctx.guild.name} Leveling Stats", color=discord.Color.from_rgb(255, 255, 255))
        e.add_field(name="Profiles", value=f"{len(rows):,}")
        e.add_field(name="Messages", value=f"{sum(int(x.get('messages', 0)) for x in rows):,}")
        e.add_field(name="Total XP", value=f"{sum(int(x.get('total_xp', x.get('xp', 0))) for x in rows):,}")
        e.add_field(name="Voice time", value=fmt_duration(sum(int(x.get('voice_secs', 0)) for x in rows)))
        await ctx.reply(embed=e)

    @commands.command(name="voicesetup", aliases=["voicelevelsetup"])
    async def voice_setup(self, ctx):
        if not await self._admin(ctx): return
        cfg = await self._config(ctx.guild.id)
        view = VoiceXPSetupView(self, ctx, cfg)
        sent = await ctx.reply(view=_leveling_panel(view.container(), view), mention_author=False)
        view.message = sent

    @commands.command(name="voicexp")
    async def voicexp(self, ctx, action: str = "show", amount: int = None):
        if action == "setup":
            return await ctx.invoke(self.voice_setup)
        if action in {"on", "off", "rate"} and not await self._admin(ctx): return
        cfg = await self._config(ctx.guild.id)
        if action == "on":
            if not cfg.get("setup_completed", False) or not cfg.get("enabled", False):
                return await self._reply(ctx, "Run and apply `levelsetup` first, then enable leveling before voice XP.", title="Setup Required")
            await self._set_config(ctx.guild.id, voice_enabled=True)
        elif action == "off": await self._set_config(ctx.guild.id, voice_enabled=False)
        elif action == "rate":
            if amount is None or not 1 <= amount <= 100: return await self._reply(ctx, "Usage: `voicexp rate <1-100>`", title="Voice XP Usage")
            await self._set_config(ctx.guild.id, voice_xp_per_min=amount)
        cfg = await self._config(ctx.guild.id)
        await self._reply(ctx, f"Voice XP is **{'on' if cfg.get('voice_enabled') else 'off'}**, at **{cfg.get('voice_xp_per_min', 10)} XP/minute**.", title="Voice XP")

async def setup(bot):
    await bot.add_cog(Leveling(bot))
