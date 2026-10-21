from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import (
    ERROR,
    SUCCESS,
    component_card,
    owner_bypass_permissions,
    panel,
    send_card,
)


INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
DOMAIN_RE = re.compile(r"(?<![@\w.-])(?:[\w-]+\.)+[a-z]{2,}(?::\d{1,5})?(?:/[^\s]*)?", re.I)
CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>")
UNICODE_EMOJI_RE = re.compile(
    "["
    "\\U0001F300-\\U0001FAFF"
    "\\U00002700-\\U000027BF"
    "\\U00002600-\\U000026FF"
    "]"
)
# Common adult-content domains. Keep this list conservative to avoid false positives.
NSFW_DOMAINS = (
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com", "redtube.com",
    "youporn.com", "onlyfans.com", "fansly.com", "chaturbate.com", "rule34.xxx",
)
NSFW_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*(?:" + "|".join(re.escape(x) for x in NSFW_DOMAINS) + r")(?:/|\b)", re.I)
log = logging.getLogger("zyven.automod")


EVENTS = {
    "anti_spam": {"label": "Anti spam", "emoji": icon("antispam"), "default": "mute"},
    "anti_caps": {"label": "Anti caps", "emoji": icon("anticaps"), "default": "mute"},
    "anti_link": {"label": "Anti link", "emoji": icon("antilink"), "default": "mute"},
    "anti_invite": {"label": "Anti invites", "emoji": icon("antiinvite"), "default": "mute"},
    "anti_mass_mention": {"label": "Anti mass mention", "emoji": icon("antimention"), "default": "mute"},
    "anti_emoji_spam": {"label": "Anti emoji spam", "emoji": icon("antiemoji"), "default": "mute"},
    "anti_nsfw_link": {"label": "Anti NSFW link", "emoji": icon("antinsfw"), "default": "block"},
}
PUNISHMENTS = {
    "mute": ("Mute", "⏱️"),
    "kick": ("Kick", "👢"),
    "ban": ("Ban", "🔨"),
}
PUNISHMENT_DURATIONS = {
    "anti_spam": 5,
    "anti_caps": 1,
    "anti_link": 5,
    "anti_invite": 5,
    "anti_mass_mention": 3,
    "anti_emoji_spam": 1,
}


def _automod_enabled(config: dict) -> bool:
    # Older config versions had individual switches but no master switch.
    return bool(config.get("enabled", any(config.get(key, False) for key in EVENTS)))


def _event_status(config: dict, key: str) -> bool:
    return bool(config.get(key, False))


def _punishment(config: dict, key: str) -> str:
    value = config.get("punishments", {}).get(key, EVENTS[key]["default"])
    return value if value in {"mute", "kick", "ban", "block"} else EVENTS[key]["default"]


def _settings_embed(config: dict, server_name: str = "this server") -> discord.Embed:
    # Keep this CV2 settings card compact and text-only for a clean, consistent layout.
    embed = panel("AutoMod Settings")
    lines = [
        f"**Settings for {server_name}**",
        f"**Status:** {'Enabled' if _automod_enabled(config) else 'Disabled'}",
        "",
        "**Filters & punishment**",
    ]
    for key, event in EVENTS.items():
        enabled = _event_status(config, key)
        punishment = _punishment(config, key)
        if key == "anti_nsfw_link":
            action = "Block message"
        else:
            action = PUNISHMENTS.get(punishment, ("Block message", "🚫"))[0]
            if punishment == "mute":
                action += f" ({PUNISHMENT_DURATIONS.get(key, 5)} min)"
        lines.append(f"**{event['label']}** — {action if enabled else 'Off'}")
    lines.extend([
        "",
        f"**Thresholds**  Spam: {config.get('spam_count', 5)} messages / {config.get('spam_window', 6)} sec · Mass mention: {config.get('mention_limit', 5)} · Caps: 70% uppercase",
    ])
    channel_id = config.get("log_channel")
    lines.append(f"**Logging channel**  {'<#' + str(channel_id) + '>' if channel_id else 'Not configured'}")
    lines.append("-# Manage punishment types with the AutoMod Punishment command.")
    embed.description = "\n".join(lines)
    return embed


def _setup_success_embed(config: dict) -> discord.Embed:
    embed = panel("AutoMod Enabled Successfully", "The selected AutoMod filters are active.", color=SUCCESS)
    lines = [f"{icon('automod_disable_no')}{icon('automod_enable_yes')} : {event['label']}" for key, event in EVENTS.items() if _event_status(config, key)]
    embed.add_field(name="Enabled Events", value="\n".join(lines) or "No filters selected", inline=False)
    channel_id = config.get("log_channel")
    embed.add_field(name="Logging", value=f"Enabled in <#{channel_id}>" if channel_id else "Not configured", inline=False)
    return embed


def _setup_prompt_embed(config: dict) -> discord.Embed:
    embed = panel(f"{icon('automod')} {icon('automod_settings')} AutoMod Setup", "Select the filters you want enabled for this server.")
    lines = []
    for key, event in EVENTS.items():
        if _event_status(config, key):
            marks = f"{icon('automod_disabled_yes')}{icon('automod_enable_yes')}"
        else:
            marks = f"{icon('automod_disable_no')}{icon('automod_enable_no')}"
        lines.append(f"{marks} : {event['label']}")
    embed.add_field(name="Available Events", value="\n".join(lines), inline=False)
    if _automod_enabled(config):
        embed.add_field(name="Current status", value="AutoMod is enabled. Selected filters will be added to the existing setup.", inline=False)
    return embed


def _rules_embed() -> discord.Embed:
    embed = panel(f"{icon('automod')} {icon('automod_settings')} AutoMod Rules", "Current default thresholds and actions.")
    rules = (
        ("Anti spam", "Triggers at 5 messages in 6 seconds. Default action: Mute for 5 minutes."),
        ("Anti caps", "Triggers above 70% uppercase on messages of 45+ characters. Default action: Mute for 1 minute."),
        ("Anti link", "Blocks links. Default action: Mute for 5 minutes."),
        ("Anti invites", "Blocks Discord server invites. Default action: Mute for 5 minutes."),
        ("Anti mass mention", "Triggers at 5 mentions. Default action: Mute for 3 minutes."),
        ("Anti emoji spam", "Triggers above 5 emojis. Default action: Mute for 1 minute."),
        ("Anti NSFW link", "Blocks known NSFW links. This action cannot be changed."),
    )
    embed.description = "\n".join(f"**{name}:** {description}" for name, description in rules)
    return embed


def _group_embed(title: str, entries: list[tuple[str, str]], *, command_root: str = "automod", page: int | None = None, page_count: int | None = None, requested_by: str | None = None) -> discord.Embed:
    # Keep this as one compact CV2 text block. Embed fields become individually
    # separated containers in component_card, unlike the clean command group UI.
    embed = panel(f"{icon('automod_settings')} {title}")
    rows = [
        f"▏ `{settings.prefix}{command_root} {command}`\n   {icon('arrow')} {description}"
        for command, description in entries
    ]
    if page is not None and page_count is not None:
        rows.append(f"-# Requested by {requested_by or 'server manager'} | Page {page + 1}/{page_count}")
    embed.description = "\n\n".join(rows)
    return embed


class AutoModGroupView(discord.ui.View):
    def __init__(self, cog: "AutoMod", guild_id: int, requester_id: int, pages: list[list[tuple[str, str]]], requester_name: str):
        super().__init__(timeout=240)
        self.cog, self.guild_id, self.requester_id = cog, guild_id, requester_id
        self.requester_name = requester_name
        self.pages = pages
        self.page = 0
        self.add_item(self._button("left", "first"))
        self.add_item(self._button("previous", "previous"))
        self.add_item(self._button("delete", "close", discord.ButtonStyle.danger))
        self.add_item(self._button("right", "next"))
        self.add_item(self._button("skip", "last"))

    def _button(self, emoji_name: str, action: str, style=discord.ButtonStyle.secondary):
        button = discord.ui.Button(emoji=discord.PartialEmoji.from_str(icon(emoji_name)), row=0, style=style)
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("Only the command user or a server manager can use these controls.", ephemeral=True)
                return
            if action == "close":
                await interaction.response.edit_message(view=component_card(panel(f"{icon('automod')} AutoMod Commands", "Command list closed.")))
                return
            self.page = {"first": 0, "previous": max(0, self.page - 1), "next": min(len(self.pages) - 1, self.page + 1), "last": len(self.pages) - 1}[action]
            embed = _group_embed("ZYVEN AUTOMOD GROUP", self.pages[self.page], page=self.page, page_count=len(self.pages), requested_by=self.requester_name)
            await interaction.response.edit_message(view=component_card(embed, self))
        button.callback = callback
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the command user or a server manager can use these controls.", ephemeral=True)
            return False
        return True


class AutoModBaseView(discord.ui.View):
    def __init__(self, cog: "AutoMod", guild_id: int, requester_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the command user or a server manager can use these controls.", ephemeral=True)
            return False
        if not interaction.user.guild_permissions.manage_guild and interaction.user.id != self.cog.bot.owner_id:
            await interaction.response.send_message("You need **Manage Server** to configure AutoMod.", ephemeral=True)
            return False
        return True


class AutoModDisableConfirmView(AutoModBaseView):
    """Require an explicit confirmation before removing all AutoMod config."""

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger, row=0, emoji=icon("automod_approve"))
    async def confirm_disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        current = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        if not _automod_enabled(current):
            guild = self.cog.bot.get_guild(self.guild_id)
            name = discord.utils.escape_markdown(guild.name if guild else "this server")
            await interaction.response.edit_message(
                view=component_card(panel("AutoMod Already Disabled", f"AutoMod is not enabled in **{name}**. Run `{settings.prefix}automod enable` to start setup."))
            )
            return
        await self.cog.bot.db.unset_values(self.guild_id, ["automod"])
        guild = self.cog.bot.get_guild(self.guild_id)
        server_name = discord.utils.escape_markdown(guild.name if guild else "this server")
        embed = panel(
            f"{icon('automod_approve')} Automod Disabled",
            f"Automod has been successfully disabled for **{server_name}**.\n"
            "All settings, punishments, and logs have been removed.\n\n"
            f"Current Status: {icon('automod_disabled_yes')} Disabled\n"
            f"To Re-enable use `{settings.prefix}automod enable`.",
            color=SUCCESS,
        )
        await interaction.response.edit_message(view=component_card(embed))

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = panel(
            "Automod Disable Cancelled",
            "Automod disable process has been cancelled.",
        )
        await interaction.response.edit_message(view=component_card(embed))


class AutoModEventSelect(discord.ui.Select):
    def __init__(self, parent: "AutoModBaseView", *, max_values: int = 7, placeholder: str = "Select AutoMod events"):
        self.owner_view = parent
        options = [discord.SelectOption(label=data["label"], value=key, emoji=data["emoji"]) for key, data in EVENTS.items()]
        super().__init__(placeholder=placeholder, min_values=1, max_values=max_values, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.owner_view.selected_events = list(self.values)
        await interaction.response.defer()


class AutoModSetupView(AutoModBaseView):
    def __init__(self, cog: "AutoMod", guild_id: int, requester_id: int):
        super().__init__(cog, guild_id, requester_id)
        self.selected_events = []
        self.add_item(AutoModEventSelect(self))

    @discord.ui.button(label="Enable Selected", style=discord.ButtonStyle.success, row=1, emoji=icon("automod_tick"))
    async def enable_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_events:
            await interaction.response.send_message("Select at least one AutoMod event first.", ephemeral=True)
            return
        await self._complete(interaction, self.selected_events)

    @discord.ui.button(label="Enable for All Events", style=discord.ButtonStyle.primary, row=1, emoji=icon("automod_enable_yes"))
    async def enable_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._complete(interaction, list(EVENTS))

    async def _complete(self, interaction: discord.Interaction, keys: list[str]):
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        if _automod_enabled(config):
            await interaction.response.send_message("AutoMod is already enabled. Disable it first if you want to run setup again.", ephemeral=True)
            return
        values = {f"automod.{key}": True for key in keys}
        values["automod.enabled"] = True
        await self.cog.bot.db.set_values(self.guild_id, values)
        config.update({key: True for key in keys})
        config["enabled"] = True
        embed = _setup_success_embed(config)
        await interaction.response.edit_message(view=component_card(embed, AutoModSuccessView(self.cog, self.guild_id, self.requester_id)))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1, emoji=icon("automod_left"))
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=component_card(panel(f"{icon('automod')} AutoMod Setup", "Setup cancelled.")))


class AutoModSuccessView(AutoModBaseView):
    @discord.ui.button(label="Show Rules", style=discord.ButtonStyle.secondary, row=0)
    async def show_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(view=component_card(_rules_embed()), ephemeral=True)

    @discord.ui.button(label="Enable AutoMod Logging", style=discord.ButtonStyle.success, row=0)
    async def enable_logging(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I could not find this server. Please run the setup again.", ephemeral=True)
            return
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        channel = guild.get_channel(int(config["log_channel"])) if config.get("log_channel") else None
        if channel is None:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                await interaction.response.send_message("I need **Manage Channels** permission to create the private AutoMod log channel.", ephemeral=True)
                return
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
            for role in guild.roles:
                if role != guild.default_role and (role.permissions.manage_guild or role.permissions.manage_messages):
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            try:
                channel = await guild.create_text_channel("automod-logs", overwrites=overwrites, reason="AutoMod logging setup")
            except discord.Forbidden:
                await interaction.response.send_message("I could not create the log channel. Check my **Manage Channels** permission.", ephemeral=True)
                return
            except discord.HTTPException:
                await interaction.response.send_message("Discord could not create the AutoMod log channel. Please try again.", ephemeral=True)
                return
            await self.cog.bot.db.set_nested(self.guild_id, "automod", "log_channel", channel.id)
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        await interaction.response.edit_message(view=component_card(_setup_success_embed(config), self))


def _punishment_embed(config: dict) -> discord.Embed:
    embed = panel(f"{icon('automod')} AutoMod Punishments", "Select one or more events, choose an action, then save.")
    for key, event in EVENTS.items():
        if key == "anti_nsfw_link":
            continue
        value = _punishment(config, key).title()
        if value == "Mute":
            value += f" ({PUNISHMENT_DURATIONS.get(key, 5)} minutes)"
        embed.add_field(name=f"{event['emoji']} {event['label']}", value=value, inline=True)
    embed.add_field(name="Anti NSFW link", value="Block message (fixed)", inline=True)
    return embed


class PunishmentActionSelect(discord.ui.Select):
    def __init__(self, parent: "AutoModPunishmentView"):
        self.owner_view = parent
        options = [discord.SelectOption(label=label, value=value, emoji=emoji) for value, (label, emoji) in PUNISHMENTS.items()]
        super().__init__(placeholder="Choose punishment", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.owner_view.selected_action = self.values[0]
        await interaction.response.defer()


class AutoModPunishmentView(AutoModBaseView):
    def __init__(self, cog: "AutoMod", guild_id: int, requester_id: int):
        super().__init__(cog, guild_id, requester_id)
        self.selected_events = []
        self.selected_action = None
        self.add_item(AutoModEventSelect(self, max_values=6, placeholder="Select events to update punishment"))
        self.add_item(PunishmentActionSelect(self))

    @discord.ui.button(label="Save Punishment", style=discord.ButtonStyle.success, row=2, emoji=icon("wtick"))
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_events or not self.selected_action:
            await interaction.response.send_message("Choose at least one event and a punishment.", ephemeral=True)
            return
        action = self.selected_action
        values = {
            f"automod.punishments.{key}": action
            for key in self.selected_events
            if key != "anti_nsfw_link"
        }
        await self.cog.bot.db.set_values(self.guild_id, values)
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        embed = _punishment_embed(config)
        embed.description = f"{icon('tick')} Saved **{PUNISHMENTS[action][0]}** for {len(self.selected_events)} selected event(s)."
        await interaction.response.edit_message(view=component_card(embed, self))

    @discord.ui.button(label="Back to Settings", style=discord.ButtonStyle.secondary, row=2, emoji=icon("left"))
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        guild = self.cog.bot.get_guild(self.guild_id)
        await interaction.response.edit_message(view=component_card(_settings_embed(config, guild.name if guild else "this server")))


def _ignore_embed(guild: discord.Guild | None, config: dict) -> discord.Embed:
    channel_lines = []
    for raw_id in config.get("ignored_channels", []):
        channel_id = int(raw_id)
        channel_lines.append(f"<#{channel_id}>")
    role_lines = [f"<@&{int(role_id)}>" for role_id in config.get("ignored_roles", [])]
    embed = panel(f"{icon('automod')} Ignored Channels & Roles", "These channels and roles bypass AutoMod checks.")
    embed.add_field(name="Ignored Channels", value="\n".join(channel_lines) or "None", inline=False)
    embed.add_field(name="Ignored Roles", value="\n".join(role_lines) or "None", inline=False)
    return embed


class AutoModIgnoreView(AutoModBaseView):
    @discord.ui.button(label="Back to Settings", style=discord.ButtonStyle.secondary, emoji=icon("left"))
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = (await self.cog.bot.db.get_guild(self.guild_id)).get("automod", {})
        guild = self.cog.bot.get_guild(self.guild_id)
        await interaction.response.edit_message(view=component_card(_settings_embed(config, guild.name if guild else "this server")))


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.recent = defaultdict(lambda: deque(maxlen=30))
        self.messages_since_sweep = 0
        self._role_member_count_cache: dict[int, tuple[float, int]] = {}

    @app_commands.command(name="automod", description="Configure an automatic message filter")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_guild=True)
    @app_commands.choices(feature=[
        app_commands.Choice(name="Anti-spam", value="anti_spam"),
        app_commands.Choice(name="Anti-caps", value="anti_caps"),
        app_commands.Choice(name="Anti-link", value="anti_link"),
        app_commands.Choice(name="Block Discord invites", value="anti_invite"),
        app_commands.Choice(name="Mass mentions", value="anti_mass_mention"),
        app_commands.Choice(name="Emoji spam", value="anti_emoji_spam"),
        app_commands.Choice(name="NSFW links", value="anti_nsfw_link"),
    ])
    async def automod_slash(self, interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_values(interaction.guild_id, {
            f"automod.{feature.value}": enabled,
            "automod.enabled": True,
        })
        label = f"{icon('enabled')} Enabled" if enabled else f"{icon('disabled')} Disabled"
        await send_card(interaction, panel(f"{icon('automod')} AutoMod Updated", f"**{feature.name}:** {label}"), ephemeral=True)

    @commands.group(name="automod", invoke_without_command=True)
    @commands.guild_only()
    async def automod(self, ctx: commands.Context):
        """Display the AutoMod command group; use subcommands to configure it."""
        if not await self._can_manage(ctx):
            return
        entries = [
            ("enable", "Start the setup flow and choose filters."),
            ("disable", "Disable AutoMod in this server."),
            ("config", "View enabled filters, punishment, limits, and logging."),
            ("punishment", "Choose mute, kick, or ban for selected events."),
            ("ignore", "Whitelist a role or channel from AutoMod."),
            ("unignore", "Remove a role or channel from the ignore list."),
            ("logging <channel>", "Set the AutoMod log channel; omit channel to clear it."),
            ("limit spam <count> <seconds>", "Set the spam threshold."),
            ("limit mentions <count>", "Set the mass-mention threshold."),
        ]
        pages = [entries[:5], entries[5:]]
        requester_name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None) or str(ctx.author.id)
        embed = _group_embed("ZYVEN AUTOMOD GROUP", pages[0], page=0, page_count=len(pages), requested_by=requester_name)
        await ctx.send(view=component_card(embed, AutoModGroupView(self, ctx.guild.id, ctx.author.id, pages, requester_name)))

    async def _can_manage(self, ctx: commands.Context) -> bool:
        if ctx.author.id == self.bot.owner_id or ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.reply(view=component_card(panel(f"{icon('error')} Permission Required", "You need **Manage Server** to configure AutoMod.", color=ERROR)), mention_author=False)
        return False

    @automod.command(name="enable")
    @commands.guild_only()
    async def enable(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("automod", {})
        if _automod_enabled(config):
            await ctx.reply(view=component_card(panel(f"{icon('automod_settings')} AutoMod Already Enabled", "AutoMod is already active in this server. Disable it first if you want to run setup again.")), mention_author=False)
            return
        embed = _setup_prompt_embed(config)
        await ctx.send(view=component_card(embed, AutoModSetupView(self, ctx.guild.id, ctx.author.id)))

    @automod.command(name="disable")
    @commands.guild_only()
    async def disable(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("automod", {})
        if not _automod_enabled(config):
            await ctx.reply(
                view=component_card(panel("AutoMod Already Disabled", f"AutoMod is not enabled in this server. Run `{settings.prefix}automod enable` to start setup.")),
                mention_author=False,
            )
            return
        server_name = discord.utils.escape_markdown(ctx.guild.name)
        embed = panel(
            "Disable Automod Confirmation",
            f"Are you sure you want to disable Automod for **{server_name}**?\n\n"
            "This will remove all custom event settings, punishments, ignored roles/channels, & logging channel data.\n"
            "Click 'Yes' to disable Automod or 'No' to cancel.",
        )
        await ctx.send(view=component_card(embed, AutoModDisableConfirmView(self, ctx.guild.id, ctx.author.id)))

    @automod.command(name="config", aliases=["settings", "show"])
    @commands.guild_only()
    async def config(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        data = await self.bot.db.get_guild(ctx.guild.id)
        await ctx.send(view=component_card(_settings_embed(data.get("automod", {}), ctx.guild.name)))

    @automod.command(name="punishment", aliases=["punishments"])
    @commands.guild_only()
    async def punishment(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("automod", {})
        await ctx.send(view=component_card(_punishment_embed(config), AutoModPunishmentView(self, ctx.guild.id, ctx.author.id)))

    @automod.command(name="logging", aliases=["log"])
    @commands.guild_only()
    async def logging_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.set_nested(ctx.guild.id, "automod", "log_channel", channel.id if channel else None)
        message = f"AutoMod logs will be sent to {channel.mention}." if channel else "AutoMod logging has been cleared."
        await ctx.reply(view=component_card(panel(f"{icon('tick')} AutoMod Logging Updated", message, color=SUCCESS)), mention_author=False)

    @automod.group(name="ignore", invoke_without_command=True)
    @commands.guild_only()
    async def ignore(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        entries = [
            ("role <role>", "Add a role to the AutoMod ignore list."),
            ("channel <channel>", "Add a channel to the AutoMod ignore list."),
            ("show", "Show ignored roles and channels."),
            ("reset", "Clear all AutoMod ignores."),
        ]
        pages = [entries]
        requester_name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None) or str(ctx.author.id)
        embed = _group_embed("ZYVEN AUTOMOD GROUP", entries, command_root="automod ignore", page=0, page_count=len(pages), requested_by=requester_name)
        await ctx.send(view=component_card(embed, AutoModGroupView(self, ctx.guild.id, ctx.author.id, pages, requester_name)))

    @ignore.command(name="role")
    @commands.guild_only()
    async def ignore_role(self, ctx: commands.Context, role: discord.Role):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.add_to_set(ctx.guild.id, "automod.ignored_roles", role.id)
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Role Ignored", f"{role.mention} is now exempt from AutoMod.", color=SUCCESS)), mention_author=False)

    @ignore.command(name="channel")
    @commands.guild_only()
    async def ignore_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.add_to_set(ctx.guild.id, "automod.ignored_channels", channel.id)
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Channel Ignored", f"{channel.mention} is now exempt from AutoMod.", color=SUCCESS)), mention_author=False)

    @ignore.command(name="show")
    @commands.guild_only()
    async def ignore_show(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("automod", {})
        await ctx.send(view=component_card(_ignore_embed(ctx.guild, config), AutoModIgnoreView(self, ctx.guild.id, ctx.author.id)))

    @ignore.command(name="reset", aliases=["clear"])
    @commands.guild_only()
    async def ignore_reset(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.set_values(ctx.guild.id, {"automod.ignored_roles": [], "automod.ignored_channels": []})
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Ignore List Reset", "All ignored AutoMod roles and channels have been cleared.", color=SUCCESS)), mention_author=False)

    @automod.group(name="unignore", invoke_without_command=True)
    @commands.guild_only()
    async def unignore(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        entries = [("role <role>", "Remove a role from the AutoMod ignore list."), ("channel <channel>", "Remove a channel from the AutoMod ignore list.")]
        pages = [entries]
        requester_name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None) or str(ctx.author.id)
        embed = _group_embed("ZYVEN AUTOMOD GROUP", entries, command_root="automod unignore", page=0, page_count=len(pages), requested_by=requester_name)
        await ctx.send(view=component_card(embed, AutoModGroupView(self, ctx.guild.id, ctx.author.id, pages, requester_name)))

    @unignore.command(name="role")
    @commands.guild_only()
    async def unignore_role(self, ctx: commands.Context, role: discord.Role):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.pull_value(ctx.guild.id, "automod.ignored_roles", role.id)
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Role Unignored", f"{role.mention} has been removed from the AutoMod ignore list.", color=SUCCESS)), mention_author=False)

    @unignore.command(name="channel")
    @commands.guild_only()
    async def unignore_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._can_manage(ctx):
            return
        await self.bot.db.pull_value(ctx.guild.id, "automod.ignored_channels", channel.id)
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Channel Unignored", f"{channel.mention} has been removed from the AutoMod ignore list.", color=SUCCESS)), mention_author=False)

    @automod.group(name="limit", invoke_without_command=True)
    @commands.guild_only()
    async def limits(self, ctx: commands.Context):
        if not await self._can_manage(ctx):
            return
        entries = [("spam <count> <seconds>", "Set the rapid-message threshold (3–20 messages, 2–30 seconds)."), ("mentions <count>", "Set the mention threshold (2–20).")]
        pages = [entries]
        requester_name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None) or str(ctx.author.id)
        embed = _group_embed("ZYVEN AUTOMOD GROUP", entries, command_root="automod limit", page=0, page_count=len(pages), requested_by=requester_name)
        await ctx.send(view=component_card(embed, AutoModGroupView(self, ctx.guild.id, ctx.author.id, pages, requester_name)))

    @limits.command(name="spam")
    @commands.guild_only()
    async def spam_limit(self, ctx: commands.Context, count: int, seconds: int):
        if not await self._can_manage(ctx):
            return
        if not 3 <= count <= 20 or not 2 <= seconds <= 30:
            await ctx.reply(view=component_card(panel(f"{icon('error')} Invalid Spam Limit", "Use 3–20 messages and a 2–30 second window.", color=ERROR)), mention_author=False)
            return
        await self.bot.db.set_values(ctx.guild.id, {"automod.spam_count": count, "automod.spam_window": seconds})
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Spam Limit Saved", f"Trigger at **{count} messages in {seconds} seconds**.", color=SUCCESS)), mention_author=False)

    @limits.command(name="mentions")
    @commands.guild_only()
    async def mention_limit(self, ctx: commands.Context, count: int):
        if not await self._can_manage(ctx):
            return
        if not 2 <= count <= 20:
            await ctx.reply(view=component_card(panel(f"{icon('error')} Invalid Mention Limit", "Choose between **2 and 20** mentions.", color=ERROR)), mention_author=False)
            return
        await self.bot.db.set_nested(ctx.guild.id, "automod", "mention_limit", count)
        await ctx.reply(view=component_card(panel(f"{icon('tick')} Mention Limit Saved", f"Trigger at **{count} mentions**.", color=SUCCESS)), mention_author=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.inspect_message(message, count_spam=True)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self.inspect_message(after, count_spam=False)

    async def inspect_message(self, message: discord.Message, *, count_spam: bool):
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        if message.author.guild_permissions.manage_messages:
            return
        guild_id = message.guild.id
        guild_config = await self.bot.db.get_guild(guild_id)
        config = guild_config.get("automod", {})
        if not _automod_enabled(config):
            return
        ignored_roles = {int(value) for value in config.get("ignored_roles", [])}
        ignored_channels = {int(value) for value in config.get("ignored_channels", [])}
        if message.channel.id in ignored_channels or any(role.id in ignored_roles for role in message.author.roles):
            return
        security = guild_config.get("security", {})
        exempt_roles = {int(value) for value in security.get("whitelist_roles", [])}
        if message.author.id in security.get("whitelist", []) or any(role.id in exempt_roles for role in message.author.roles):
            return

        reason = None
        event_key = None
        content = message.content or ""
        if _event_status(config, "anti_nsfw_link") and NSFW_RE.search(content):
            event_key, reason = "anti_nsfw_link", "NSFW links are blocked in this server."
        elif _event_status(config, "anti_invite") and INVITE_RE.search(content):
            event_key, reason = "anti_invite", "Discord server invites are blocked."
        elif _event_status(config, "anti_link") and (URL_RE.search(content) or DOMAIN_RE.search(content)):
            event_key, reason = "anti_link", "Links are blocked in this server."
        elif _event_status(config, "anti_mass_mention"):
            mention_limit = int(config.get("mention_limit", 5))
            now = time.monotonic()
            large_role_mention = False
            for role in message.role_mentions:
                cached = self._role_member_count_cache.get(role.id)
                if cached and cached[0] > now:
                    role_member_count = cached[1]
                else:
                    role_member_count = len(role.members)
                    self._role_member_count_cache[role.id] = (now + 30, role_member_count)
                if role_member_count >= mention_limit:
                    large_role_mention = True
                    break
            if len(self._role_member_count_cache) > 4096:
                self._role_member_count_cache = {
                    role_id: value for role_id, value in self._role_member_count_cache.items()
                    if value[0] > now
                }
            if len(message.mentions) >= mention_limit or len(message.role_mentions) >= mention_limit or large_role_mention or message.mention_everyone:
                event_key, reason = "anti_mass_mention", "Mass mentions are blocked."
        if reason is None and _event_status(config, "anti_caps"):
            letters = [character for character in content if character.isalpha()]
            if len(content) >= 45 and len(letters) >= 10 and sum(character.isupper() for character in letters) / len(letters) > 0.70:
                event_key, reason = "anti_caps", "Messages with excessive capital letters are blocked."
        if reason is None and _event_status(config, "anti_emoji_spam"):
            emoji_count = len(CUSTOM_EMOJI_RE.findall(content)) + len(UNICODE_EMOJI_RE.findall(content))
            if emoji_count > int(config.get("emoji_limit", 5)):
                event_key, reason = "anti_emoji_spam", "Emoji spam is blocked."
        if reason is None and count_spam and _event_status(config, "anti_spam"):
            self.messages_since_sweep += 1
            if self.messages_since_sweep >= 500:
                self.messages_since_sweep = 0
                now = datetime.now(timezone.utc).timestamp()
                stale = [key for key, events in self.recent.items() if not events or now - events[-1] > 60]
                for key in stale:
                    self.recent.pop(key, None)
            now = datetime.now(timezone.utc).timestamp()
            bucket = self.recent[(guild_id, message.author.id)]
            spam_count = int(config.get("spam_count", 5))
            spam_window = int(config.get("spam_window", 6))
            while bucket and now - bucket[0] > spam_window:
                bucket.popleft()
            bucket.append(now)
            if len(bucket) >= spam_count:
                event_key, reason = "anti_spam", "Please slow down; spam is blocked."
                bucket.clear()
        if reason and event_key:
            await self._take_action(message, config, event_key, reason)

    async def _take_action(self, message: discord.Message, config: dict, event_key: str, reason: str):
        try:
            await message.delete()
        except discord.Forbidden:
            log.warning("AutoMod cannot delete messages in guild %s channel %s", message.guild.id, message.channel.id)
            return
        except discord.HTTPException:
            log.exception("Failed to remove an AutoMod message in guild %s", message.guild.id)
            return

        action = _punishment(config, event_key)
        action_label = "Block message"
        try:
            if action == "mute":
                duration = PUNISHMENT_DURATIONS.get(event_key, 5)
                await message.author.timeout(timedelta(minutes=duration), reason=f"AutoMod: {reason}")
                action_label = f"Muted for {duration} minute(s)"
            elif action == "kick":
                await message.author.kick(reason=f"AutoMod: {reason}")
                action_label = "Kicked"
            elif action == "ban":
                await message.author.ban(reason=f"AutoMod: {reason}", delete_message_seconds=0)
                action_label = "Banned"
            else:
                action = "block"
        except discord.Forbidden:
            action_label = "Message blocked; bot lacks permission to apply the configured punishment"
            log.warning("AutoMod lacks permission to %s member %s in guild %s", action, message.author.id, message.guild.id)
        except discord.HTTPException:
            action_label = "Message blocked; the configured punishment failed"
            log.exception("AutoMod punishment failed in guild %s", message.guild.id)

        try:
            await message.author.send(f"{reason} Server: **{message.guild.name}**")
        except (discord.Forbidden, discord.HTTPException):
            pass
        log_channel_id = config.get("log_channel")
        log_channel = message.guild.get_channel(int(log_channel_id)) if log_channel_id else None
        if log_channel is None:
            security = (await self.bot.db.get_guild(message.guild.id)).get("security", {})
            security_log_id = security.get("log_channel")
            log_channel = message.guild.get_channel(int(security_log_id)) if security_log_id else None
        if log_channel:
            try:
                event = EVENTS[event_key]
                embed = panel(f"{icon('automod')} AutoMod Action", f"**Member:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Filter:** {event['emoji']} {event['label']}\n**Action:** {action_label}\n**Reason:** {reason}", color=ERROR)
                await log_channel.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send AutoMod log for guild %s", message.guild.id)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
