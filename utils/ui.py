"""Shared Zyven embed and component styles used by every cog."""
import asyncio
import copy
import inspect
import logging
import sys
import traceback

import discord

from .emojis import icon
from core.settings import settings

BRAND = 0x5865F2
SUCCESS = 0x57F287
WARNING = 0xFEE75C
ERROR = 0xED4245
log = logging.getLogger("zyven.ui")
BRAND_CREDIT = "Powered By Zyven Development™"


def panel(title: str, description: str = "", *, color: int = BRAND) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=BRAND_CREDIT)
    return embed


def notice_panel(description: str, *, title: str = "Action Needed") -> discord.Embed:
    """Standard CV2-ready panel for command validation and permission notices."""
    return panel(f"{icon('error')} {title}", description, color=ERROR)


def state(value: bool) -> str:
    return f"{icon('enabled') if value else icon('disabled')} **{'Enabled' if value else 'Disabled'}**"


def setting_field(embed: discord.Embed, label: str, value: bool, *, inline: bool = True):
    embed.add_field(name=label, value=state(value), inline=inline)


def with_links(embed: discord.Embed) -> discord.Embed:
    embed.add_field(name="Links", value=f"[Support Server]({settings.support_url}) • [Invite Zyven]({settings.invite_url})", inline=False)
    return embed


def embed_sections(embed: discord.Embed) -> list[str]:
    """Render embed copy as separate text sections for real CV2 dividers."""
    sections = []
    parts = []
    if embed.title:
        parts.append(f"## {embed.title}")
    if embed.description:
        parts.append(embed.description)
    if parts:
        sections.append("\n\n".join(parts))
    for field in embed.fields:
        sections.append(f"**{field.name}:** {field.value}" if field.inline else f"**{field.name}**\n{field.value}")
    if not sections:
        sections.append("\u200b")
    remaining = 3900  # leave room for the footer text inside the V2 limit
    clipped = []
    for section in sections:
        if remaining <= 0:
            break
        section = section[:remaining]
        clipped.append(section)
        remaining -= len(section)
    return clipped


class ComponentCard(discord.ui.LayoutView):
    """Place a legacy panel's copy and controls inside one Components V2 container."""
    def __init__(self, embed: discord.Embed, source_view: discord.ui.View | None = None, *, accent_colour=None):
        if isinstance(source_view, ComponentCard):
            source_view = source_view.source_view
        super().__init__(timeout=getattr(source_view, "timeout", 180.0))
        self.source_view = source_view
        # Filled by the command/invocation send helpers. This is a final
        # ownership gate shared by all Components V2 buttons and selects,
        # including views whose individual callbacks do not implement checks.
        self.requester_id: int | None = getattr(source_view, "requester_id", None)
        children = []
        sections = embed_sections(embed)
        thumbnail_url = embed.thumbnail.url if embed.thumbnail else None
        if thumbnail_url and sections:
            children.append(discord.ui.Section(
                discord.ui.TextDisplay(sections[0]),
                accessory=discord.ui.Thumbnail(media=thumbnail_url),
            ))
            sections = sections[1:]
        for section in sections:
            if children:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(section))
        # Components V2 does not carry legacy embed images over automatically;
        # render them explicitly so Avatar/Banner and other image cards show.
        image_url = embed.image.url if embed.image else None
        if image_url:
            children.append(discord.ui.Separator())
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)))
        if source_view and source_view.children:
            children.append(discord.ui.Separator())
            rows: dict[int, discord.ui.ActionRow] = {}
            row_kinds: dict[int, str] = {}
            for source_item in list(source_view.children):
                item = copy.copy(source_item)
                # A copied v1 View item still points at its original View.
                # Detach it before placing the clone in a v2 ActionRow.
                item._view = None
                if hasattr(item, "_parent"):
                    item._parent = None
                callback = getattr(item, "callback", None)
                if getattr(callback, "item", None) is source_item:
                    # Decorated View callbacks keep the source item in their
                    # internal callback wrapper. Pass the displayed clone so
                    # Select values and Button state come from this interaction.
                    callback.item = item
                elif inspect.ismethod(callback) and callback.__self__ is source_item:
                    # A Select subclass callback is usually bound to the source
                    # Select. Rebind it to the clone so its values are current.
                    item.callback = callback.__func__.__get__(item, type(item))
                old_row = item.row
                is_select = isinstance(item, discord.ui.Select)
                row_index = old_row
                if row_index is None:
                    if is_select:
                        row_index = next((i for i in range(5) if i not in rows), 0)
                    else:
                        row_index = next((i for i, row in rows.items() if row_kinds[i] == "buttons" and len(row.children) < 5), None)
                        if row_index is None:
                            row_index = next((i for i in range(5) if i not in rows), 0)
                if row_index in rows and (is_select or row_kinds[row_index] == "select"):
                    row_index = next((i for i in range(5) if i not in rows), row_index)
                if row_index not in rows:
                    rows[row_index] = discord.ui.ActionRow()
                    row_kinds[row_index] = "select" if is_select else "buttons"
                rows[row_index].add_item(item)
            children.extend(rows[index] for index in sorted(rows))
        if embed.footer.text:
            if children:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(f"-# {embed.footer.text}"))
        # Use Discord's neutral card border like the reference panels.
        self.container = discord.ui.Container(*children, accent_colour=accent_colour)
        self.add_item(self.container)
        # Some discord.py 2.6.x nesting paths leave child view references unset.
        # The ViewStore dispatches nested components through item.view, so pin
        # every descendant to this live LayoutView after the tree is complete.
        for item in self.walk_children():
            item._view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the person who ran this command can use these controls.", ephemeral=True)
            return False
        if self.source_view is not None:
            return await self.source_view.interaction_check(interaction)
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item, /) -> None:
        context = (
            "CV2 component callback failed | component=%s custom_id=%s user_id=%s guild_id=%s channel_id=%s interaction_id=%s values=%r"
            % (
                type(item).__name__,
                getattr(item, "custom_id", None),
                interaction.user.id,
                interaction.guild_id,
                interaction.channel_id,
                interaction.id,
                getattr(item, "values", None),
            )
        )
        # Write directly to stdout as well as the logger: several bot hosts
        # display stdout only, and their console may hide stderr log handlers.
        print(context, file=sys.stdout, flush=True)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stdout)
        sys.stdout.flush()
        embed = panel(f"{icon('error')} Action Failed", "That control hit an error. Please try again; the bot recorded the failure.", color=ERROR)
        if interaction.is_expired():
            log.info("Skipped CV2 error response for expired interaction %s", interaction.id)
            return
        try:
            await send_card(interaction, embed, ephemeral=True)
        except discord.NotFound as exc:
            if exc.code == 10062:
                log.info("Discord interaction %s expired before the CV2 error response", interaction.id)
                return
            log.exception("Could not report CV2 callback failure to the user")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not report CV2 callback failure to the user")


def component_card(embed: discord.Embed, view: discord.ui.View | None = None, *, accent_colour=None) -> ComponentCard:
    return ComponentCard(embed, view, accent_colour=accent_colour)


def bind_component_owner(card: ComponentCard, user_id: int) -> ComponentCard:
    """Restrict an interactive command card to the user who invoked it."""
    # Persistent panels (for example, the public ticket creation panel) are
    # intentionally available to every server member.
    if card.timeout is not None:
        card.requester_id = user_id
        if card.source_view is not None and getattr(card.source_view, "requester_id", None) is None:
            card.source_view.requester_id = user_id
    return card


async def send_component_card(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None, *, ephemeral: bool = True):
    card = bind_component_owner(component_card(embed, view), interaction.user.id)
    return await interaction.response.send_message(view=card, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())


async def edit_component_card(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None):
    card = bind_component_owner(component_card(embed, view), interaction.user.id)
    kwargs = {"content": None, "embeds": [], "attachments": [], "view": card}
    if interaction.response.is_done():
        # Deferred component interactions have already been acknowledged; edit
        # their source message through the interaction webhook instead.
        return await interaction.edit_original_response(**kwargs)
    return await interaction.response.edit_message(**kwargs)


async def edit_original_component_card(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None):
    card = bind_component_owner(component_card(embed, view), interaction.user.id)
    return await interaction.edit_original_response(content=None, embeds=[], attachments=[], view=card)


async def followup_component_card(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None, *, ephemeral: bool = True):
    card = bind_component_owner(component_card(embed, view), interaction.user.id)
    return await interaction.followup.send(view=card, ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())


async def send_card(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View | None = None, *, ephemeral: bool = True):
    """Send an embed's copy and optional controls as a clean CV2 container."""
    if interaction.response.is_done():
        return await followup_component_card(interaction, embed, view, ephemeral=ephemeral)
    return await send_component_card(interaction, embed, view, ephemeral=ephemeral)


def schedule_access_denied_delete(interaction: discord.Interaction, message=None, delay: float = 5.0):
    async def expire():
        await asyncio.sleep(delay)
        try:
            if message is not None:
                await message.delete()
            else:
                await interaction.delete_original_response()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    asyncio.create_task(expire(), name="zyven-expire-access-denied")


async def send_notice(interaction: discord.Interaction, description: str, *, title: str | None = None):
    """Send validation and permission feedback in the shared clean CV2 card."""
    if title is None:
        lowered = description.lower()
        title = "Access Denied" if lowered.startswith(("you need", "only the", "only server")) else "Action Needed"
    embed = notice_panel(description, title=title)
    if interaction.is_expired():
        log.info("Skipped notice for expired interaction %s", interaction.id)
        return None
    try:
        sent_message = None
        original_response = False
        if interaction.response.is_done():
            sent_message = await interaction.followup.send(
                view=component_card(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), wait=True
            )
        else:
            sent_message = await interaction.response.send_message(
                view=component_card(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            original_response = True
        if title and title.casefold() == "access denied":
            schedule_access_denied_delete(interaction, None if original_response else sent_message)
        return sent_message
    except discord.HTTPException as exc:
        if exc.code in {10062, 50027} or exc.status == 401:
            log.info("Discord interaction %s expired before notice delivery", interaction.id)
            return None
        log.warning("Could not send notice for interaction %s: %s", interaction.id, exc)
        return None


async def ensure_antinuke_roles(guild: discord.Guild, owned_role_ids: set[int]) -> tuple[list[discord.Role], set[int]]:
    """Create the two named administrator roles shown by the security setup panel."""
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise discord.Forbidden(None, "Zyven needs Manage Roles to create the antinuke security roles.")
    # Place Prime first, then Unbypassable, immediately below the bot's
    # highest role. Existing roles below these positions shift down.
    role_names = ("Zyven Prime Security™", "Zyven Unbypassable Security™")
    roles = []
    newly_owned = set(owned_role_ids)
    admin_permissions = discord.Permissions(administrator=True)
    candidates = {role.id: role for role in guild.roles if role.name in role_names}
    try:
        async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.role_create):
            role_id = getattr(entry.target, "id", None)
            if role_id in candidates and entry.user and entry.user.id == me.id:
                newly_owned.add(role_id)
    except (discord.Forbidden, discord.HTTPException):
        # Roles created by this code path are tracked below even if audit logs
        # are unavailable; unrelated roles with matching names remain untouched.
        pass
    for name in role_names:
        role = next((candidate for candidate in candidates.values() if candidate.name == name and candidate.id in newly_owned), None)
        if role is None:
            role = await guild.create_role(
                name=name,
                permissions=admin_permissions,
                mentionable=False,
                reason="Create the configured antinuke security roles",
            )
            newly_owned.add(role.id)
        if role >= me.top_role:
            raise discord.Forbidden(None, "Zyven's highest role must be above the antinuke security roles.")
        await role.edit(
            permissions=admin_permissions,
            colour=discord.Colour(0xFFFFFF),
            reason="Configure the antinuke security role",
        )
        roles.append(role)
    top_position = me.top_role.position
    if top_position <= len(roles):
        raise discord.Forbidden(None, "There is not enough role hierarchy space below Zyven's highest role.")
    positions = {
        roles[0]: top_position - 1,
        roles[1]: top_position - 2,
    }
    await guild.edit_role_positions(positions=positions, reason="Place antinuke security roles directly below Zyven")
    await guild.me.add_roles(*roles, atomic=False, reason="Assign Zyven its antinuke security roles")
    return roles, newly_owned


def antinuke_setup_embed(guild: discord.Guild, roles: list[discord.Role], security: dict) -> discord.Embed:
    embed = panel(
        f"Unbypassable Security Configuration For {guild.name}",
        f"{icon('tick')} Antinuke and Auto Recovery are enabled.\n\nZyven created these Administrator roles, set them to white, placed them directly below its bot role, and assigned them to itself:",
    )
    bot_role = None
    if guild.me:
        bot_role = next((role for role in guild.me.roles if role.managed and role.name == guild.me.name), guild.me.top_role)
    role_lines = [f"1. {roles[0].mention}", f"2. {roles[1].mention}", f"3. {bot_role.mention if bot_role else 'Zyven'}"]
    embed.add_field(name="Role order", value="\n".join(role_lines), inline=False)
    selected = set(security.get("anti_nuke_events", [key for key, _, _ in EventFilterSelect.OPTIONS]))
    events = [f"{icon('txck') if security.get('anti_nuke') and key in selected else icon('cross')} {label}" for key, label, _ in EventFilterSelect.OPTIONS]
    embed.add_field(name="Antinuke Features Enabled", value="\n".join(events[:14]), inline=False)
    embed.add_field(name="More Antinuke Features", value="\n".join(events[14:]), inline=False)
    embed.add_field(
        name="Additional Protections",
        value=(
            f"{icon('txck') if security.get('anti_nuke') and security.get('anti_link_role') else icon('cross')} Anti Link Role\n"
            f"{icon('txck') if security.get('anti_nuke') and security.get('anti_invite_role') else icon('cross')} Anti Invite Role\n"
            f"{icon('txck') if security.get('auto_recovery') else icon('cross')} Auto Recovery"
        ),
        inline=False,
    )
    embed.add_field(name="Role placement", value="The security roles sit directly below Zyven's highest bot role.", inline=False)
    return embed


def can_manage(interaction: discord.Interaction, owner_id: int) -> bool:
    return settings.is_owner(interaction.user.id) or interaction.user.id == owner_id or bool(interaction.user.guild_permissions.manage_guild)


def is_server_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.user.id == interaction.guild.owner_id


def can_access_security(interaction: discord.Interaction, data: dict | None = None) -> bool:
    """Antinuke/security panels are restricted to the server owner and Extraowners."""
    if interaction.guild is None:
        return False
    return is_security_actor(interaction.guild, interaction.user.id, data)


def is_security_actor(guild: discord.Guild, user_id: int, data: dict | None = None) -> bool:
    trusted = (data or {}).get("security", {}).get("extraowners", [])
    # IDs may have been serialized as strings by older JSON/Mongo-backed
    # configurations. Normalize before checking so Extraowners keep access.
    try:
        trusted_ids = {int(value) for value in trusted}
    except (TypeError, ValueError):
        trusted_ids = set()
    return user_id == guild.owner_id or user_id in trusted_ids


def can_manage_view(interaction: discord.Interaction, owner_id: int, view) -> bool:
    """Apply server-owner/Extraowner rules to every nested security panel control."""
    current = view
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "category", None) == "security" or getattr(current, "key", None) == "anti_nuke":
            return can_access_security(interaction, getattr(current, "cached_data", None))
        current = getattr(current, "feature_view", None) or getattr(current, "parent_view", None)
    return can_manage(interaction, owner_id)


async def send_security_access_denied(interaction: discord.Interaction, *, detail: str = "Only the Server Owner or Extraowners can access this menu."):
    return await send_notice(interaction, detail, title="Access Denied")


def owner_bypass_permissions(**required):
    """App-command user-permission check that exempts the configured bot owner."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if settings.is_owner(interaction.user.id):
            return True
        permissions = interaction.permissions
        missing = [name for name, value in required.items() if getattr(permissions, name, False) != value]
        if missing:
            raise discord.app_commands.MissingPermissions(missing)
        return True
    return discord.app_commands.check(predicate)


class ConfigHome(discord.ui.View):
    """Top-level category selector with button navigation for mobile-friendly setup."""

    def __init__(self, bot, guild_id: int, owner_id: int):
        super().__init__(timeout=240)
        self.bot, self.guild_id, self.owner_id = bot, guild_id, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        data = await self.bot.db.get_guild(self.guild_id)
        if not can_manage(interaction, self.owner_id) and not can_access_security(interaction, data):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    @discord.ui.select(placeholder="Select a setup category", options=[
        discord.SelectOption(label="Security", description="Anti-link, anti-raid, trusted members", value="security", emoji=icon("security")),
        discord.SelectOption(label="Member Setup", description="Welcome messages and autorole", value="members", emoji=icon("welcome")),
        discord.SelectOption(label="VoiceMaster", description="Join-to-create voice rooms", value="voicemaster", emoji=icon("voice")),
    ])
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        if select.values[0] == "security":
            data = await self.bot.db.get_guild(self.guild_id)
            if not can_access_security(interaction, data):
                await send_security_access_denied(interaction)
                return
        elif not can_manage(interaction, self.owner_id):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return
        await show_category(interaction, self.bot, self.guild_id, self.owner_id, select.values[0])


async def show_category(interaction: discord.Interaction, bot, guild_id: int, owner_id: int, category: str):
    await interaction.response.defer()
    data = await bot.db.get_guild(guild_id)
    embed, view = category_screen(data, bot, guild_id, owner_id, category)
    await edit_original_component_card(interaction, embed, view)


def security_overview_embed(data: dict) -> discord.Embed:
    security = data.get("security", {})
    owners = security.get("extraowners", [])
    embed = panel("Configure Security", "Enhance server security with `antinuke` and `extraowner`.")
    embed.add_field(name="Current Status", value="Enabled" if security.get("anti_nuke", False) else "Disabled", inline=True)
    embed.add_field(name="Current Extraowners", value="\n".join(f"↳ <@{uid}>" for uid in sorted(owners)) if owners else "↳ None", inline=False)
    return embed


def security_status_embed(guild: discord.Guild, data: dict) -> discord.Embed:
    security = data.get("security", {})
    automod = data.get("automod", {})
    enabled = bool(security.get("anti_nuke", False))
    selected = set(security.get("anti_nuke_events", [key for key, _, _ in EventFilterSelect.OPTIONS]))
    status_mark = icon("tick") if enabled else icon("cross")
    embed = panel(f"Zyven Security Status • {guild.name}", "Review Zyven's configured protection and role placement below.")
    embed.add_field(name="Antinuke Status", value="Enabled" if enabled else "Disabled", inline=True)
    bot_member = guild.me
    top_role = bot_member.top_role.mention if bot_member else "Unavailable"
    role_position = bot_member.top_role.position if bot_member else "?"
    embed.add_field(name="Bot Role", value=f"{top_role} • position {role_position}. Keep it above roles it must manage.", inline=False)
    event_lines = [f"{status_mark if enabled and key in selected else icon('cross')} {label}" for key, label, _ in EventFilterSelect.OPTIONS]
    embed.add_field(name="Antinuke Features", value="\n".join(event_lines), inline=False)
    extras = [
        f"{icon('tick') if enabled and security.get('anti_link_role') else icon('cross')} Anti-link role protection",
        f"{icon('tick') if enabled and security.get('anti_invite_role') else icon('cross')} Anti-invite role protection",
        f"{icon('tick') if security.get('anti_link') else icon('cross')} Anti-link filter",
        f"{icon('tick') if automod.get('anti_invite') else icon('cross')} Invite filter",
        f"{icon('tick') if security.get('auto_recovery') else icon('cross')} Auto Recovery",
        f"{icon('tick') if security.get('extraowners') else icon('cross')} Extraowner access",
    ]
    embed.add_field(name="Additional Protections", value="\n".join(extras), inline=False)
    return embed


class SecurityOverviewView(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int, data: dict | None = None):
        super().__init__(timeout=240)
        self.bot, self.guild_id, self.owner_id = bot, guild_id, owner_id
        self.cached_data = data or {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_access_security(interaction, self.cached_data):
            await send_security_access_denied(interaction)
            return False
        return True

    @discord.ui.button(label="Antinuke", style=discord.ButtonStyle.secondary, row=0)
    async def antinuke(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = CategoryActions(self.bot, self.guild_id, self.owner_id, "security")
        parent.cached_data = self.cached_data
        parent.overview_entry = True
        await show_feature(interaction, parent, "anti_nuke", "Antinuke", "Prevent harmful activities with active monitoring.", show_advanced=True)

    @discord.ui.button(label="Extraowner", style=discord.ButtonStyle.secondary, row=0)
    async def extraowner(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can configure extraowners.")
            return
        data = await self.bot.db.get_guild(self.guild_id)
        await edit_component_card(interaction, extraowner_embed(data.get("security", {}).get("extraowners", [])), ExtraownerView(self.bot, self.guild_id, self.owner_id))

    @discord.ui.button(label="✖", style=discord.ButtonStyle.danger, row=0)
    async def clear_extraowners(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can change extraowners.")
            return
        await interaction.response.defer()
        await self.bot.db.set_nested(self.guild_id, "security", "extraowners", [])
        self.cached_data.setdefault("security", {})["extraowners"] = []
        await edit_original_component_card(interaction, security_overview_embed(self.cached_data), self)


async def show_security_overview(interaction: discord.Interaction, bot, guild_id: int, owner_id: int, *, ephemeral: bool = True):
    data = await bot.db.get_guild(guild_id)
    embed = security_overview_embed(data)
    view = SecurityOverviewView(bot, guild_id, owner_id, data)
    if interaction.response.is_done():
        return await interaction.followup.send(view=component_card(embed, view), ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())
    return await interaction.response.send_message(view=component_card(embed, view), ephemeral=ephemeral, allowed_mentions=discord.AllowedMentions.none())


def category_screen(data: dict, bot, guild_id: int, owner_id: int, category: str):
    section = data.get(category, {})
    names = {
        "security": (f"{icon('security')} Security Configuration", "Protect your server and manage trusted members."),
        "automod": (f"{icon('automod')} AutoMod Configuration", "Choose which automatic message filters are active."),
        "members": (f"{icon('welcome')} Member Configuration", "Set the welcome experience and new-member role."),
        "voicemaster": (f"{icon('voice')} VoiceMaster Configuration", "Create temporary voice rooms when members join a trigger channel."),
    }
    title, description = names[category]
    embed = panel(title, description)
    view = CategoryActions(bot, guild_id, owner_id, category, cached_data=data)
    if category == "security":
        setting_field(embed, "Anti-link Protection", section.get("anti_link", False))
        setting_field(embed, "Anti-nuke Protection", section.get("anti_nuke", False))
        setting_field(embed, "Anti-raid Protection", section.get("anti_raid", False))
        embed.add_field(name="Anti-raid Join Limit", value=f"{section.get('anti_raid_threshold', 8)} joins in 10 seconds", inline=False)
        embed.add_field(name="Log Channel", value=f"<#{section['log_channel']}>" if section.get("log_channel") else "Not configured", inline=False)
        embed.add_field(name="AutoMod Exemptions", value=f"{len(section.get('whitelist', []))} member(s) • {len(section.get('whitelist_roles', []))} role(s)", inline=False)
    elif category == "automod":
        for key, label, emoji_key in (
            ("anti_spam", "Anti-spam", "antispam"),
            ("anti_caps", "Anti-caps", "anticaps"),
            ("anti_link", "Anti-link", "antilink"),
            ("anti_invite", "Anti-invites", "antiinvite"),
            ("anti_mass_mention", "Mass mentions", "antimention"),
            ("anti_emoji_spam", "Emoji spam", "antiemoji"),
            ("anti_nsfw_link", "NSFW links", "antinsfw"),
        ):
            setting_field(embed, f"{icon(emoji_key)} {label}", section.get(key, False))
        embed.add_field(name="Spam Threshold", value=f"{section.get('spam_count', 5)} messages in {section.get('spam_window', 6)} seconds", inline=True)
        embed.add_field(name="Mention Threshold", value=f"{section.get('mention_limit', 5)} mentions", inline=True)
    elif category == "members":
        welcome = data.get("welcome", {})
        role = data.get("autorole", {}).get("role")
        embed.add_field(name="Welcome Channel", value=f"<#{welcome['channel']}>" if welcome.get("channel") else "Not configured", inline=False)
        embed.add_field(name="Welcome Message", value=welcome.get("message", "Welcome {user} to {server}!"), inline=False)
        embed.add_field(name="Welcome Style", value=f"{icon('welcomer')} CV2 clean card" if welcome.get("style", "classic") == "cv2" else f"{icon('autorole_settings')} Classic bordered embed", inline=False)
        embed.add_field(name="Autorole", value=f"<@&{role}>" if role else "Not configured", inline=False)
    else:
        trigger, category_id = section.get("trigger_channel"), section.get("category")
        embed.add_field(name="Join-to-create Channel", value=f"<#{trigger}>" if trigger else "Not configured", inline=False)
        embed.add_field(name="Voice Category", value=f"<#{category_id}>" if category_id else "Use the trigger channel's category", inline=False)
    embed.set_footer(text=BRAND_CREDIT)
    return embed, view


class CategoryActions(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int, category: str, *, cached_data: dict | None = None):
        super().__init__(timeout=240)
        self.bot, self.guild_id, self.owner_id, self.category = bot, guild_id, owner_id, category
        self.cached_data = cached_data or {}
        self.add_item(BackButton(self))
        if category == "security":
            link_button = discord.ui.Button(label="Anti-link", style=discord.ButtonStyle.secondary, row=0)
            link_button.callback = self.open_antilink
            self.add_item(link_button)
            anti_button = discord.ui.Button(label="Antinuke", style=discord.ButtonStyle.secondary, row=0)
            anti_button.callback = self.open_antinuke
            self.add_item(anti_button)
            extra_button = discord.ui.Button(label="Extraowner", style=discord.ButtonStyle.secondary, row=0)
            extra_button.callback = self.open_extraowners
            self.add_item(extra_button)
            raid_button = discord.ui.Button(label="Anti-raid", style=discord.ButtonStyle.secondary, row=1)
            raid_button.callback = self.open_antiraid
            self.add_item(raid_button)
            self.add_item(ChannelSettingSelect(self, "security", "log_channel", "Choose a log channel", (discord.ChannelType.text, discord.ChannelType.news), row=2))
            clear_log = discord.ui.Button(label="Clear Log Channel", style=discord.ButtonStyle.danger, row=3)
            clear_log.callback = self.make_clear("security", "log_channel")
            self.add_item(clear_log)
        elif category == "automod":
            feature_labels = (
                ("anti_spam", "Spam", "antispam"), ("anti_caps", "Caps", "anticaps"),
                ("anti_link", "Links", "antilink"), ("anti_invite", "Invites", "antiinvite"),
                ("anti_mass_mention", "Mentions", "antimention"), ("anti_emoji_spam", "Emoji spam", "antiemoji"),
                ("anti_nsfw_link", "NSFW links", "antinsfw"),
            )
            for index, (key, label, emoji_key) in enumerate(feature_labels):
                button = discord.ui.Button(label=label, emoji=icon(emoji_key), style=discord.ButtonStyle.primary, custom_id=f"zyven:{category}:{key}", row=index // 3)
                button.callback = self.make_toggle(key)
                self.add_item(button)
            spam_config = discord.ui.Button(label="Spam Limit", style=discord.ButtonStyle.secondary, row=3)
            spam_config.callback = self.open_spam_settings
            self.add_item(spam_config)
            mention_config = discord.ui.Button(label="Mention Limit", style=discord.ButtonStyle.secondary, row=3)
            mention_config.callback = self.open_mention_settings
            self.add_item(mention_config)
        elif category == "members":
            self.add_item(ChannelSettingSelect(self, "welcome", "channel", "Choose welcome channel", (discord.ChannelType.text, discord.ChannelType.news), row=0))
            self.add_item(RoleSettingSelect(self, row=1))
            self.add_item(WelcomeStyleButton(self, "cv2", row=2))
            self.add_item(WelcomeStyleButton(self, "classic", row=2))
            message_button = discord.ui.Button(label="Edit Welcome Message", style=discord.ButtonStyle.primary, row=3)
            message_button.callback = self.edit_welcome
            self.add_item(message_button)
            clear_welcome = discord.ui.Button(label="Clear Welcome", style=discord.ButtonStyle.danger, row=3)
            clear_welcome.callback = self.make_clear("welcome", "")
            self.add_item(clear_welcome)
            clear_role = discord.ui.Button(label="Clear Autorole", style=discord.ButtonStyle.danger, row=3)
            clear_role.callback = self.make_clear("autorole", "")
            self.add_item(clear_role)
        elif category == "voicemaster":
            self.add_item(ChannelSettingSelect(self, "voicemaster", "trigger_channel", "Choose join-to-create channel", (discord.ChannelType.voice,), row=0))
            self.add_item(ChannelSettingSelect(self, "voicemaster", "category", "Choose destination category", (discord.ChannelType.category,), row=1))
            clear_voice = discord.ui.Button(label="Clear VoiceMaster", style=discord.ButtonStyle.danger, row=2)
            clear_voice.callback = self.make_clear("voicemaster", "trigger_channel,category")
            self.add_item(clear_voice)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.category == "security":
            data = await self.bot.db.get_guild(self.guild_id)
            if not can_access_security(interaction, data):
                await send_security_access_denied(interaction)
                return False
        elif not can_manage(interaction, self.owner_id):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    def make_toggle(self, key: str):
        async def toggle(interaction: discord.Interaction):
            if not can_manage_view(interaction, self.owner_id, self):
                await send_notice(interaction, "You need **Manage Server** to change settings.")
                return
            await interaction.response.defer()
            data = await self.bot.db.get_guild(self.guild_id)
            current = data.get(self.category, {}).get(key, False)
            await self.bot.db.set_nested(self.guild_id, self.category, key, not current)
            fresh = await self.bot.db.get_guild(self.guild_id)
            if self.category == "automod":
                active = any(fresh.get("automod", {}).get(event, False) for event in ("anti_spam", "anti_caps", "anti_link", "anti_invite", "anti_mass_mention", "anti_emoji_spam", "anti_nsfw_link"))
                await self.bot.db.set_nested(self.guild_id, "automod", "enabled", active)
                fresh = await self.bot.db.get_guild(self.guild_id)
            embed, view = category_screen(fresh, self.bot, self.guild_id, self.owner_id, self.category)
            await edit_original_component_card(interaction, embed, view)
        return toggle

    def make_clear(self, section: str, key: str):
        async def clear(interaction: discord.Interaction):
            if not can_manage_view(interaction, self.owner_id, self):
                await send_notice(interaction, "You need **Manage Server** to change settings.")
                return
            labels = {
                ("security", "log_channel"): "the security and moderation log channel",
                ("welcome", ""): "the welcome channel and message",
                ("autorole", ""): "the new-member autorole",
                ("voicemaster", "trigger_channel,category"): "the VoiceMaster trigger and destination category",
            }
            setting_label = labels.get((section, key), "this configuration")
            embed = panel(
                "Confirm Configuration Clear",
                f"Are you sure you want to clear {setting_label}? This setting will stop working until it is configured again.",
                color=ERROR,
            )
            await edit_component_card(interaction, embed, ConfirmClearView(self, section, key))
        return clear

    async def edit_welcome(self, interaction: discord.Interaction):
        data = getattr(self, "cached_data", {})
        modal = WelcomeMessageModal(self, data.get("welcome", {}).get("message", "Welcome {user} to {server}!"))
        await interaction.response.send_modal(modal)

    async def open_spam_settings(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        config = getattr(self, "cached_data", {}).get("automod", {})
        await interaction.response.send_modal(SpamThresholdModal(self, config.get("spam_count", 5), config.get("spam_window", 6)))

    async def open_mention_settings(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        config = getattr(self, "cached_data", {}).get("automod", {})
        await interaction.response.send_modal(MentionThresholdModal(self, config.get("mention_limit", 5)))

    async def open_antinuke(self, interaction: discord.Interaction):
        await show_feature(interaction, self, "anti_nuke", "Antinuke", "Prevent harmful activities with active monitoring.", show_advanced=True)

    async def open_antiraid(self, interaction: discord.Interaction):
        await show_feature(interaction, self, "anti_raid", "Anti-raid", "Monitor sudden member joins and suspicious activity.")

    async def open_antilink(self, interaction: discord.Interaction):
        await show_feature(interaction, self, "anti_link", "Anti-link", "Remove URLs posted by members without Manage Messages.")

    async def open_extraowners(self, interaction: discord.Interaction):
        data = getattr(self, "cached_data", {})
        owners = data.get("security", {}).get("extraowners", [])
        view = ExtraownerView(self.bot, self.guild_id, self.owner_id, owners)
        await edit_component_card(interaction, extraowner_embed(owners), view)


class ConfirmClearView(discord.ui.View):
    def __init__(self, parent: CategoryActions, section: str, key: str):
        super().__init__(timeout=120)
        self.parent_view, self.section, self.key = parent, section, key

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.parent_view.owner_id, self):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    def paths(self) -> list[str]:
        if not self.key:
            return [self.section]
        return [f"{self.section}.{part}" for part in self.key.split(",")]

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = self.parent_view
        await interaction.response.defer()
        await parent.bot.db.unset_values(parent.guild_id, self.paths())
        fresh = await parent.bot.db.get_guild(parent.guild_id)
        embed, view = category_screen(fresh, parent.bot, parent.guild_id, parent.owner_id, parent.category)
        embed.add_field(name="Configuration Cleared", value="The selected setting was removed successfully.", inline=False)
        await edit_original_component_card(interaction, embed, view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = self.parent_view
        data = await parent.bot.db.get_guild(parent.guild_id)
        embed, view = category_screen(data, parent.bot, parent.guild_id, parent.owner_id, parent.category)
        await edit_component_card(interaction, embed, view)


class SpamThresholdModal(discord.ui.Modal, title="Configure Anti-spam Limit"):
    count = discord.ui.TextInput(label="Messages before spam action (3–20)", min_length=1, max_length=2, required=True)
    window = discord.ui.TextInput(label="Time window in seconds (2–30)", min_length=1, max_length=2, required=True)

    def __init__(self, parent: CategoryActions, count: int, window: int):
        super().__init__()
        self.parent_view = parent
        self.count.default = str(count)
        self.window.default = str(window)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_manage_view(interaction, self.parent_view.owner_id, self):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        try:
            count, window = int(self.count.value), int(self.window.value)
        except ValueError:
            await send_notice(interaction, "Enter whole numbers for both limits.")
            return
        if not 3 <= count <= 20 or not 2 <= window <= 30:
            await send_notice(interaction, "Choose 3–20 messages and a 2–30 second window.")
            return
        await interaction.response.defer(ephemeral=True)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "automod", "spam_count", count)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "automod", "spam_window", window)
        self.parent_view.cached_data.setdefault("automod", {}).update(spam_count=count, spam_window=window)
        await send_card(interaction, panel("Anti-spam Limit Saved", f"Spam action triggers at **{count} messages in {window} seconds**."), ephemeral=True)


class MentionThresholdModal(discord.ui.Modal, title="Configure Mention Limit"):
    limit = discord.ui.TextInput(label="Mentions before message removal (2–20)", min_length=1, max_length=2, required=True)

    def __init__(self, parent: CategoryActions, current: int):
        super().__init__()
        self.parent_view = parent
        self.limit.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.parent_view.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        try:
            limit = int(self.limit.value)
        except ValueError:
            await send_notice(interaction, "Enter a whole number from 2 to 20.")
            return
        if not 2 <= limit <= 20:
            await send_notice(interaction, "Enter a whole number from 2 to 20.")
            return
        await interaction.response.defer(ephemeral=True)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "automod", "mention_limit", limit)
        self.parent_view.cached_data.setdefault("automod", {})["mention_limit"] = limit
        await send_card(interaction, panel("Mention Limit Saved", f"Messages with **{limit} or more mentions** will be removed."), ephemeral=True)


async def show_feature(interaction: discord.Interaction, parent: CategoryActions, key: str, title: str, description: str, *, show_advanced: bool = False):
    await interaction.response.defer()
    data = await parent.bot.db.get_guild(parent.guild_id)
    section = "automod" if key in {"anti_spam", "anti_invite", "anti_mass_mention"} else "security"
    enabled = data.get(section, {}).get(key, False)
    embed = panel(f"Configure {title}", description)
    embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=True)
    view = FeatureSetupView(parent, key, title, description, show_advanced=show_advanced)
    view.cached_data = data
    await edit_original_component_card(interaction, embed, view)


class FeatureSetupView(discord.ui.View):
    def __init__(self, parent: CategoryActions, key: str, title: str, description: str, *, show_advanced: bool = True):
        super().__init__(timeout=240)
        self.parent_view, self.key, self.title, self.description = parent, key, title, description
        self.cached_data = getattr(parent, "cached_data", {})
        if key == "anti_raid":
            threshold_button = discord.ui.Button(label="Join Limit", style=discord.ButtonStyle.secondary, row=1)
            threshold_button.callback = self.open_raid_limit
            self.add_item(threshold_button)
        if key == "anti_nuke" and show_advanced:
            limits_button = discord.ui.Button(label="Antinuke Limit", style=discord.ButtonStyle.secondary, row=1)
            limits_button.callback = self.open_event_limits
            self.add_item(limits_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.parent_view.owner_id, self):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    async def update(self, interaction: discord.Interaction, enabled: bool):
        if self.key == "anti_nuke" and not can_access_security(interaction, self.cached_data):
            await send_security_access_denied(interaction)
            return
        if not can_manage_view(interaction, self.parent_view.owner_id, self):
            await (send_security_access_denied(interaction) if self.key == "anti_nuke" else send_notice(interaction, "You need **Manage Server** to change settings."))
            return
        section = "automod" if self.key.startswith("anti_") and self.key in {"anti_spam", "anti_invite", "anti_mass_mention"} else "security"
        await interaction.response.defer()
        if self.key == "anti_nuke" and enabled:
            current = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
            if current.get("security", {}).get("anti_nuke", False):
                self.cached_data.setdefault("security", {}).update(current.get("security", {}))
                embed = panel(f"Configure {self.title}", self.description)
                embed.add_field(name="Current Status", value="Enabled", inline=True)
                await edit_original_component_card(interaction, embed, self)
                await followup_component_card(
                    interaction,
                    panel("Antinuke Already Enabled", f"{icon('tick')} Antinuke is already enabled."),
                    ephemeral=True,
                )
                return
        antinuke_roles = None
        deleted_antinuke_roles = []
        failed_antinuke_roles = []
        failed_antinuke_role_details = []
        if self.key == "anti_nuke" and enabled:
            try:
                current = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
                security_config = current.get("security", {})
                old_owned_ids = {int(role_id) for role_id in security_config.get("antinuke_role_ids", [])}
                antinuke_roles, owned_ids = await ensure_antinuke_roles(interaction.guild, old_owned_ids)
            except discord.Forbidden:
                await send_notice(interaction, "Antinuke was not enabled. Zyven needs **Manage Roles**, and its highest role must be above both antinuke security roles so it can move them directly underneath.")
                return
            except discord.HTTPException:
                log.exception("Could not create antinuke security roles in guild %s", interaction.guild_id)
                await send_notice(interaction, "Antinuke was not enabled because Discord could not create the security roles. Check Zyven's **Manage Roles** permission and try again.")
                return
            selected_events = set(security_config.get("anti_nuke_events", [key for key, _, _ in EventFilterSelect.OPTIONS]))
            selected_events.update({"role_position", "role_link_permission", "role_invite_permission", "webhook_spam"})
            event_limits = dict(security_config.get("anti_nuke_limits", {}))
            # Enable antinuke with the requested Ban-by-default response for
            # every monitored event; event groups remain configurable later.
            event_punishments = {event_key: "ban" for event_key in selected_events}
            for event_key in selected_events:
                event_limits.setdefault(event_key, 3)
            recovery_cog = self.parent_view.bot.get_cog("Security")
            if not recovery_cog or not await recovery_cog.capture_recovery_snapshot(interaction.guild, force=True):
                await send_notice(interaction, "Antinuke was not enabled because Auto Recovery could not save a server snapshot. Check the bot logs and MongoDB connection, then try again.")
                return
            await self.parent_view.bot.db.set_values(self.parent_view.guild_id, {
                "security.anti_link_role": True,
                "security.anti_invite_role": True,
                "security.auto_recovery": True,
                "security.anti_nuke_opt_in_confirmed": True,
                "security.anti_nuke_events": sorted(selected_events),
                "security.anti_nuke_limits": event_limits,
                "security.anti_nuke_punishments": event_punishments,
                "security.antinuke_role_ids": sorted(owned_ids),
            })
            self.parent_view.bot.loop.create_task(recovery_cog._prime_invite_snapshot(interaction.guild, force=True))
            self.cached_data.setdefault("security", {}).update(anti_link_role=True, anti_invite_role=True, auto_recovery=True)
        elif self.key == "anti_nuke" and not enabled:
            data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
            owned_ids = {int(role_id) for role_id in data.get("security", {}).get("antinuke_role_ids", [])}
            # Recover ownership for roles created by earlier Zyven builds, which
            # did not persist role IDs. Only trust matching audit entries by this bot.
            candidate_names = {"Zyven Unbypassable Security™", "Zyven Prime Security™"}
            candidates = {role.id: role for role in interaction.guild.roles if role.name in candidate_names}
            try:
                async for entry in interaction.guild.audit_logs(limit=100, action=discord.AuditLogAction.role_create):
                    target_id = getattr(entry.target, "id", None)
                    if target_id in candidates and entry.user and entry.user.id == self.parent_view.bot.user.id:
                        owned_ids.add(target_id)
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not inspect role creation audit log while disabling antinuke in guild %s", self.parent_view.guild_id)
            for role_id in owned_ids:
                role = interaction.guild.get_role(role_id)
                if role is None:
                    deleted_antinuke_roles.append(role_id)
                    continue
                bot_member = interaction.guild.me
                if bot_member is None or not bot_member.guild_permissions.manage_roles:
                    failed_antinuke_roles.append(role_id)
                    failed_antinuke_role_details.append(f"{role.name}: Zyven needs Manage Roles")
                    continue
                if role >= bot_member.top_role:
                    failed_antinuke_roles.append(role_id)
                    failed_antinuke_role_details.append(f"{role.name}: move Zyven's highest role above it")
                    continue
                try:
                    await role.delete(reason="Antinuke disabled; remove Zyven-created security role")
                    deleted_antinuke_roles.append(role_id)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failed_antinuke_roles.append(role_id)
                    if isinstance(exc, discord.Forbidden) or getattr(exc, "code", None) == 50013:
                        detail = "Discord denied Manage Roles or role hierarchy access"
                    else:
                        detail = f"Discord HTTP {getattr(exc, 'status', '?')} (code {getattr(exc, 'code', '?')})"
                    failed_antinuke_role_details.append(f"{role.name}: {detail}")
                    log.warning("Could not remove Zyven antinuke role %s from guild %s: %s", role_id, self.parent_view.guild_id, detail)
            await self.parent_view.bot.db.set_nested(
                self.parent_view.guild_id,
                "security",
                "antinuke_role_ids",
                sorted(owned_ids - set(deleted_antinuke_roles)),
            )
            await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "security", "auto_recovery", False)
            await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "security", "anti_nuke_opt_in_confirmed", False)
            security_cog = self.parent_view.bot.get_cog("Security")
            if security_cog:
                security_cog.invite_use_snapshots.pop(self.parent_view.guild_id, None)
                security_cog.invite_snapshots_ready.discard(self.parent_view.guild_id)
            self.cached_data.setdefault("security", {})["auto_recovery"] = False
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, section, self.key, enabled)
        self.cached_data.setdefault(section, {})[self.key] = enabled
        embed = panel(f"Configure {self.title}", self.description)
        embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=True)
        await edit_original_component_card(interaction, embed, self)
        if antinuke_roles:
            config = (await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)).get("security", {})
            await followup_component_card(interaction, antinuke_setup_embed(interaction.guild, antinuke_roles, config), ephemeral=True)
        elif self.key == "anti_nuke" and not enabled:
            if failed_antinuke_roles:
                detail_text = "\n".join(f"• {detail}" for detail in failed_antinuke_role_details[:4])
                result = f"{icon('warning')} Antinuke disabled, but {len(failed_antinuke_roles)} security role(s) could not be deleted. Fix the listed permission or role-order issue, then move Zyven's highest role above each security role and delete it manually.\n{detail_text}"
                title = "Antinuke Disabled"
            else:
                result = f"{icon('tick')} Antinuke disabled."
                title = "Antinuke Disabled"
            await followup_component_card(interaction, panel(title, result), ephemeral=True)

    @discord.ui.button(label="Enable", style=discord.ButtonStyle.success)
    async def enable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update(interaction, True)

    @discord.ui.button(label="Disable", style=discord.ButtonStyle.danger)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update(interaction, False)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        if getattr(self.parent_view, "overview_entry", False):
            embed = security_overview_embed(data)
            view = SecurityOverviewView(self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, data)
        else:
            embed, view = category_screen(data, self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, self.parent_view.category)
        await edit_original_component_card(interaction, embed, view)

    async def open_event_limits(self, interaction: discord.Interaction):
        view = EventLimitMenuView(self)
        embed = antinuke_limit_menu_embed()
        await edit_component_card(interaction, embed, view)

    async def open_raid_limit(self, interaction: discord.Interaction):
        # A modal must be the first response, so keep this cached settings read short.
        current = self.cached_data.get("security", {}).get("anti_raid_threshold", 8)
        await interaction.response.send_modal(AntiRaidLimitModal(self.parent_view, current))


class AntiRaidLimitModal(discord.ui.Modal, title="Configure Anti-raid Join Limit"):
    limit = discord.ui.TextInput(label="Joins within 10 seconds before alert", min_length=1, max_length=3, required=True)

    def __init__(self, parent: CategoryActions, current: int):
        super().__init__()
        self.parent_view = parent
        self.limit.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.parent_view.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        try:
            value = int(self.limit.value)
        except ValueError:
            await send_notice(interaction, "Enter a whole number from 2 to 100.")
            return
        if not 2 <= value <= 100:
            await send_notice(interaction, "Enter a whole number from 2 to 100.")
            return
        await interaction.response.defer(ephemeral=True)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "security", "anti_raid_threshold", value)
        self.parent_view.cached_data.setdefault("security", {})["anti_raid_threshold"] = value
        await send_card(interaction, panel("Anti-raid Limit Saved", f"Zyven will alert at **{value} joins in 10 seconds**."), ephemeral=True)


class EventFilterSelect(discord.ui.Select):
    OPTIONS = [
        ("member_ban", "Anti Ban", "Monitor member bans"),
        ("member_unban", "Anti Unban", "Monitor member unbans"),
        ("member_kick", "Anti Kick", "Monitor member kicks"),
        ("member_prune", "Anti Prune", "Monitor member prunes"),
        ("bot_add", "Anti Bot Add", "Monitor bots joining the server"),
        ("channel_create", "Anti Channel Create", "Monitor new channels"),
        ("channel_delete", "Anti Channel Delete", "Monitor deleted channels"),
        ("channel_update", "Anti Channel Update", "Monitor channel changes"),
        ("role_create", "Anti Role Create", "Monitor new roles"),
        ("role_delete", "Anti Role Delete", "Monitor deleted roles"),
        ("role_update", "Anti Role Update", "Monitor role changes"),
        ("role_position", "Anti Role Position", "Monitor role ordering changes"),
        ("role_link_permission", "Anti Link Role", "Monitor roles granted embed/link permissions"),
        ("role_invite_permission", "Anti Invite Role", "Monitor roles granted invite permissions"),
        ("member_update", "Anti Member Update", "Monitor nickname and member setting changes"),
        ("member_role_update", "Anti Member Role Update", "Monitor member role changes"),
        ("emoji_create", "Anti Emoji Create", "Monitor new server emojis"),
        ("emoji_delete", "Anti Emoji Delete", "Monitor deleted server emojis"),
        ("emoji_update", "Anti Emoji Update", "Monitor emoji name changes"),
        ("sticker_create", "Anti Sticker Create", "Monitor new server stickers"),
        ("sticker_delete", "Anti Sticker Delete", "Monitor deleted server stickers"),
        ("sticker_update", "Anti Sticker Update", "Monitor sticker changes"),
        ("integration_create", "Anti Integration Add", "Monitor added integrations"),
        ("integration_delete", "Anti Integration Remove", "Monitor removed integrations"),
        ("integration_update", "Anti Integration Update", "Monitor integration changes"),
        ("webhook_create", "Anti Webhook Create", "Monitor created webhooks"),
        ("webhook_spam", "Anti Webhook Spam", "Monitor webhook creation bursts"),
        ("webhook_delete", "Anti Webhook Delete", "Monitor deleted webhooks"),
        ("webhook_update", "Anti Webhook Update", "Monitor webhook changes"),
        ("guild_update", "Anti Server Update", "Monitor server setting changes"),
        ("everyone_ping", "Anti Everyone/Here Ping", "Monitor @everyone and @here mentions"),
        ("role_ping", "Anti Role Ping", "Monitor server role mentions"),
    ]

    PAGE_SIZE = 25

    def __init__(self, parent, selected: list[str]):
        self.parent_view = parent
        start = parent.page * self.PAGE_SIZE
        page_options = self.OPTIONS[start:start + self.PAGE_SIZE]
        options = [discord.SelectOption(label=label, value=value, description=description, default=value in selected) for value, label, description in page_options]
        super().__init__(placeholder=f"Choose events to monitor • Page {parent.page + 1}/{parent.page_count}", min_values=0, max_values=len(options), options=options, row=0)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not can_manage_view(interaction, self.parent_view.feature_view.parent_view.owner_id, self.parent_view.feature_view):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        start = self.parent_view.page * self.PAGE_SIZE
        visible_keys = {value for value, _, _ in self.OPTIONS[start:start + self.PAGE_SIZE]}
        selected = sorted((self.parent_view.selected_keys - visible_keys) | set(self.values))
        guild_id = self.parent_view.feature_view.parent_view.guild_id
        bot = self.parent_view.feature_view.parent_view.bot
        await interaction.response.defer()
        await bot.db.set_nested(guild_id, "security", "anti_nuke_events", selected)
        self.parent_view.feature_view.cached_data.setdefault("security", {})["anti_nuke_events"] = selected
        view = EventFilterView(self.parent_view.feature_view, selected, page=self.parent_view.page)
        embed = panel("Configure Antinuke Events", "Select the audit events Zyven should watch. Your selection saves immediately.")
        labels = [label for value, label, _ in self.OPTIONS if value in selected]
        embed.add_field(name="Watched Events", value=" • ".join(labels) if labels else "No events selected", inline=False)
        await edit_original_component_card(interaction, embed, view)


class EventFilterView(discord.ui.View):
    def __init__(self, feature_view: FeatureSetupView, selected: list[str], *, page: int = 0):
        super().__init__(timeout=240)
        self.feature_view = feature_view
        self.selected_keys = set(selected)
        self.page = page
        self.page_count = (len(EventFilterSelect.OPTIONS) + EventFilterSelect.PAGE_SIZE - 1) // EventFilterSelect.PAGE_SIZE
        self.add_item(EventFilterSelect(self, selected))
        if page > 0:
            self.add_item(discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, row=1, custom_id="zyven:antinuke:previous"))
            self.children[-1].callback = self.previous_page
        if page + 1 < self.page_count:
            self.add_item(discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, row=1, custom_id="zyven:antinuke:next"))
            self.children[-1].callback = self.next_page

    async def previous_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page + 1)

    async def show_page(self, interaction: discord.Interaction, page: int):
        view = EventFilterView(self.feature_view, sorted(self.selected_keys), page=page)
        embed = panel("Configure Antinuke Events", f"Select the audit events Zyven should watch. Your selection saves immediately. **Page {page + 1}/{self.page_count}.**")
        selected_labels = [label for value, label, _ in EventFilterSelect.OPTIONS if value in self.selected_keys]
        embed.add_field(name="Watched Events", value=" • ".join(selected_labels)[:1024] or "None selected", inline=False)
        await edit_component_card(interaction, embed, view)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = self.feature_view
        await interaction.response.defer()
        data = await parent.parent_view.bot.db.get_guild(parent.parent_view.guild_id)
        section = "automod" if parent.key in {"anti_spam", "anti_invite", "anti_mass_mention"} else "security"
        enabled = data.get(section, {}).get(parent.key, False)
        embed = panel(f"Configure {parent.title}", parent.description)
        embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=True)
        await edit_original_component_card(interaction, embed, parent)


class EventLimitView(discord.ui.View):
    PAGE_SIZE = 5

    def __init__(self, feature_view: FeatureSetupView, *, page: int = 0):
        super().__init__(timeout=240)
        self.feature_view = feature_view
        self.page_count = (len(ANTINUKE_LIMIT_GROUPS) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        self.page = max(0, min(page, self.page_count - 1))
        first = discord.ui.Button(label="≪", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
        first.callback = self.first_page
        self.add_item(first)
        previous = discord.ui.Button(label="‹", style=discord.ButtonStyle.secondary, row=1, disabled=self.page == 0)
        previous.callback = self.previous_page
        self.add_item(previous)
        self.add_item(ResetAntinukeLimitsButton(self, row=1))
        following = discord.ui.Button(label="›", style=discord.ButtonStyle.secondary, row=1, disabled=self.page + 1 >= self.page_count)
        following.callback = self.next_page
        self.add_item(following)
        last = discord.ui.Button(label="≫", style=discord.ButtonStyle.secondary, row=1, disabled=self.page + 1 >= self.page_count)
        last.callback = self.last_page
        self.add_item(last)

    async def first_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, 0)

    async def next_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page + 1)

    async def previous_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page - 1)

    async def last_page(self, interaction: discord.Interaction):
        await self.show_page(interaction, self.page_count - 1)

    async def show_page(self, interaction: discord.Interaction, page: int):
        view = EventLimitView(self.feature_view, page=page)
        await edit_component_card(interaction, event_limit_page_embed(self.feature_view, view.page), view)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

class AntinukeResetConfirmView(discord.ui.View):
    def __init__(self, feature_view: FeatureSetupView, page: int):
        super().__init__(timeout=120)
        self.feature_view, self.page = feature_view, page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        parent = self.feature_view
        security = parent.cached_data.setdefault("security", {})
        limits = {key: 3 for key, _, _ in EventFilterSelect.OPTIONS}
        responses = {key: "ban" for key, _, _ in EventFilterSelect.OPTIONS}
        await interaction.response.defer()
        await parent.parent_view.bot.db.set_values(parent.parent_view.guild_id, {
            "security.anti_nuke_limits": limits,
            "security.anti_nuke_punishments": responses,
        })
        security["anti_nuke_limits"] = limits
        security["anti_nuke_punishments"] = responses
        embed = panel("Reset Complete", "All antinuke event limits are back to **3 actions in 10 seconds**, and every response is set to **Ban**.")
        await edit_original_component_card(interaction, embed, EventLimitView(parent, page=self.page))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = EventLimitView(self.feature_view, page=self.page)
        await edit_component_card(interaction, event_limit_page_embed(self.feature_view, view.page), view)


ANTINUKE_LIMIT_GROUPS = [
    ("member_ban", "Anti Ban", ("member_ban",)),
    ("member_unban", "Anti Unban", ("member_unban",)),
    ("member_kick", "Anti Kick", ("member_kick",)),
    ("member_prune", "Anti Prune", ("member_prune",)),
    ("bot_add", "Anti Bot Add", ("bot_add",)),
    ("channel_create", "Anti Channel Create", ("channel_create",)),
    ("channel_delete", "Anti Channel Delete", ("channel_delete",)),
    ("channel_update", "Anti Channel Update", ("channel_update",)),
    ("role_create", "Anti Role Create", ("role_create",)),
    ("role_delete", "Anti Role Delete", ("role_delete",)),
    ("role_update", "Anti Role Update", ("role_update", "role_position", "role_link_permission", "role_invite_permission")),
    ("member_update", "Anti Member Update", ("member_update", "member_role_update")),
    ("guild_update", "Anti Server Update", ("guild_update",)),
    ("everyone_ping", "Anti Everyone/Here", ("everyone_ping",)),
    ("emoji_create", "Anti Emoji/Sticker Create", ("emoji_create", "sticker_create")),
    ("emoji_delete", "Anti Emoji/Sticker Delete", ("emoji_delete", "sticker_delete")),
    ("emoji_update", "Anti Emoji/Sticker Update", ("emoji_update", "sticker_update")),
    ("role_ping", "Anti Role Ping", ("role_ping",)),
    ("integration_create", "Anti Integration", ("integration_create", "integration_delete", "integration_update")),
    ("webhook_create", "Anti Webhook Create", ("webhook_create", "webhook_spam")),
    ("webhook_delete", "Anti Webhook Delete", ("webhook_delete",)),
    ("webhook_update", "Anti Webhook Update", ("webhook_update",)),
]


def antinuke_limit_menu_embed() -> discord.Embed:
    return panel("Antinuke Limit Configuration", "Select an event from the dropdown below to configure its limit.")


class EventLimitMenuView(discord.ui.View):
    def __init__(self, feature_view: FeatureSetupView):
        super().__init__(timeout=240)
        self.feature_view = feature_view
        self.add_item(EventLimitEventSelect(self))
        reset = discord.ui.Button(label="❌", style=discord.ButtonStyle.danger, row=1)
        reset.callback = self.reset
        self.add_item(reset)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            await send_security_access_denied(interaction)
            return False
        return True

    async def reset(self, interaction: discord.Interaction):
        await edit_component_card(
            interaction,
            panel("Configuration Reset", "Reset all antinuke limits and punishments to their defaults?", color=0xED4245),
            AntinukeResetConfirmView(self.feature_view, 0),
        )

    async def back(self, interaction: discord.Interaction):
        feature = self.feature_view
        security = feature.cached_data.get("security", {})
        embed = panel(f"Configure {feature.title}", feature.description)
        embed.add_field(name="Current Status", value="Enabled" if security.get(feature.key, False) else "Disabled", inline=True)
        await edit_component_card(interaction, embed, feature)


class EventLimitEventSelect(discord.ui.Select):
    def __init__(self, parent: EventLimitMenuView):
        self.parent_view = parent
        options = [
            discord.SelectOption(label=label, value=str(index))
            for index, (_, label, _) in enumerate(ANTINUKE_LIMIT_GROUPS)
        ]
        super().__init__(placeholder="Select Event to Configure Limit", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent = self.parent_view.feature_view
        if not can_manage_view(interaction, parent.parent_view.owner_id, parent):
            await send_security_access_denied(interaction)
            return
        _, label, event_keys = ANTINUKE_LIMIT_GROUPS[int(self.values[0])]
        await edit_component_card(interaction, event_limit_embed(parent, label, event_keys), EventLimitDetailView(parent, label, event_keys, 0))


class ResetAntinukeLimitsButton(discord.ui.Button):
    def __init__(self, parent: EventLimitView, *, row: int):
        super().__init__(label="❌", style=discord.ButtonStyle.danger, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not can_manage_view(interaction, self.parent_view.feature_view.parent_view.owner_id, self.parent_view.feature_view):
            await send_security_access_denied(interaction)
            return
        embed = panel(
            "Configuration Reset",
            "Are you sure you want to reset all antinuke event limits and punishments?\n\n**Default Limit:** `3` actions in 10 seconds\n**Default Punishment:** `Ban`",
            color=0xED4245,
        )
        await edit_component_card(interaction, embed, AntinukeResetConfirmView(self.parent_view.feature_view, self.parent_view.page))


def event_limit_page_embed(parent: FeatureSetupView, page: int) -> discord.Embed:
    security = parent.cached_data.get("security", {})
    start = page * EventLimitView.PAGE_SIZE
    events = ANTINUKE_LIMIT_GROUPS[start:start + EventLimitView.PAGE_SIZE]
    lines = []
    for key, label, event_keys in events:
        limit = security.get("anti_nuke_limits", {}).get(key, security.get("anti_nuke_limits", {}).get(event_keys[0], 3))
        punishment = security.get("anti_nuke_punishments", {}).get(key, security.get("anti_nuke_punishments", {}).get(event_keys[0], "ban"))
        response = {"none": "LOG ONLY", "kick": "KICK", "ban": "BAN"}.get(punishment, "LOG ONLY")
        lines.append(f"**{label}**\n› Limit: `{limit}` | Punishment: `{response}`")
    page_count = (len(ANTINUKE_LIMIT_GROUPS) + EventLimitView.PAGE_SIZE - 1) // EventLimitView.PAGE_SIZE
    embed = panel("Antinuke Limit Configuration", "\n\n".join(lines))
    embed.set_footer(text=f"Powered By Zyven Development™ • Page {page + 1} / {page_count}")
    return embed


def event_limit_embed(parent: FeatureSetupView, label: str, event_keys: tuple[str, ...]) -> discord.Embed:
    limits = parent.cached_data.get("security", {}).get("anti_nuke_limits", {})
    current = limits.get(event_keys[0], 3)
    return panel(f"Configure {label}", f"Select the new limit below.\n\n**Current Limit:** `{current}` actions in 10 seconds.")


class EventLimitDetailView(discord.ui.View):
    LIMIT_CHOICES = [*range(1, 21), 25, 30, 40, 50]

    def __init__(self, feature_view: FeatureSetupView, label: str, event_keys: tuple[str, ...], page: int):
        super().__init__(timeout=240)
        self.feature_view, self.label, self.event_keys, self.page = feature_view, label, event_keys, page
        current = feature_view.cached_data.get("security", {}).get("anti_nuke_limits", {}).get(event_keys[0], 3)
        options = [discord.SelectOption(label=str(value), value=str(value), default=value == current) for value in self.LIMIT_CHOICES]
        self.add_item(EventLimitValueSelect(self, options))
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            await send_notice(interaction, "You need **Manage Server** to use this configuration panel.")
            return False
        return True

    async def back(self, interaction: discord.Interaction):
        await edit_component_card(interaction, antinuke_limit_menu_embed(), EventLimitMenuView(self.feature_view))

class EventLimitValueSelect(discord.ui.Select):
    def __init__(self, parent: EventLimitDetailView, options: list[discord.SelectOption]):
        self.parent_view = parent
        super().__init__(placeholder=f"Select Limit for {parent.label}", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent = self.parent_view
        feature = parent.feature_view
        if not can_manage_view(interaction, feature.parent_view.owner_id, feature):
            await send_security_access_denied(interaction)
            return
        value = int(self.values[0])
        db_values = {f"security.anti_nuke_limits.{key}": value for key in parent.event_keys}
        await interaction.response.defer()
        await feature.parent_view.bot.db.set_values(feature.parent_view.guild_id, db_values)
        limits = feature.cached_data.setdefault("security", {}).setdefault("anti_nuke_limits", {})
        limits.update({key: value for key in parent.event_keys})
        refreshed = EventLimitDetailView(feature, parent.label, parent.event_keys, parent.page)
        await edit_component_card(
            interaction,
            panel(f"{icon('tick')} Limit Saved • {parent.label}", f"**New Limit:** `{value}` actions in 10 seconds.\nSelect another value or use **Back** to configure another event."),
            refreshed,
        )


def antinuke_punishment_menu_embed() -> discord.Embed:
    return panel(
        "Antinuke Punishment Configuration",
        "Select an event from the dropdown below to configure its punishment.",
    )


class AntinukePunishmentMenuView(discord.ui.View):
    def __init__(self, feature_view: FeatureSetupView):
        super().__init__(timeout=240)
        self.feature_view = feature_view
        options = [
            discord.SelectOption(label=label, value=str(index))
            for index, (_, label, _) in enumerate(ANTINUKE_LIMIT_GROUPS)
        ]
        self.add_item(AntinukePunishmentEventSelect(self, options))
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            return True
        await send_security_access_denied(interaction)
        return False

    async def back(self, interaction: discord.Interaction):
        feature = self.feature_view
        security = feature.cached_data.get("security", {})
        embed = panel(f"Configure {feature.title}", feature.description)
        embed.add_field(name="Current Status", value="Enabled" if security.get(feature.key, False) else "Disabled", inline=True)
        await edit_component_card(interaction, embed, feature)


class AntinukePunishmentEventSelect(discord.ui.Select):
    def __init__(self, parent: AntinukePunishmentMenuView, options: list[discord.SelectOption]):
        self.parent_view = parent
        super().__init__(placeholder="Select Event to Configure Punishment", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent = self.parent_view.feature_view
        if not can_manage_view(interaction, parent.parent_view.owner_id, parent):
            await send_security_access_denied(interaction)
            return
        key, label, event_keys = ANTINUKE_LIMIT_GROUPS[int(self.values[0])]
        security = parent.cached_data.get("security", {})
        values = security.get("anti_nuke_punishments", {})
        current = values.get(key, values.get(event_keys[0], "ban"))
        embed = panel(
            f"Configure {label}",
            f"Select the punishment below.\n\n**Current Punishment:** `{ {'none': 'LOG ONLY', 'kick': 'KICK', 'ban': 'BAN'}.get(current, 'LOG ONLY') }`",
        )
        view = AntinukePunishmentDetailView(parent, key, label, event_keys, current)
        await edit_component_card(interaction, embed, view)


class AntinukePunishmentDetailView(discord.ui.View):
    def __init__(self, feature_view: FeatureSetupView, key: str, label: str, event_keys: tuple[str, ...], current: str):
        super().__init__(timeout=240)
        self.feature_view, self.key, self.label, self.event_keys = feature_view, key, label, event_keys
        choices = [("Log Only", "none"), ("Kick", "kick"), ("Ban", "ban")]
        options = [discord.SelectOption(label=label, value=value, default=value == current) for label, value in choices]
        self.add_item(AntinukePunishmentValueSelect(self, options))
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.primary, row=1)
        back.callback = self.back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if can_manage_view(interaction, self.feature_view.parent_view.owner_id, self.feature_view):
            return True
        await send_security_access_denied(interaction)
        return False

    async def back(self, interaction: discord.Interaction):
        await edit_component_card(
            interaction,
            antinuke_punishment_menu_embed(),
            AntinukePunishmentMenuView(self.feature_view),
        )


class AntinukePunishmentValueSelect(discord.ui.Select):
    def __init__(self, parent: AntinukePunishmentDetailView, options: list[discord.SelectOption]):
        self.parent_view = parent
        super().__init__(placeholder=f"Select Punishment for {parent.label}", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent = self.parent_view
        feature = parent.feature_view
        if not can_manage_view(interaction, feature.parent_view.owner_id, feature):
            await send_security_access_denied(interaction)
            return
        value = self.values[0]
        await interaction.response.defer()
        await feature.parent_view.bot.db.set_values(
            feature.parent_view.guild_id,
            {f"security.anti_nuke_punishments.{key}": value for key in parent.event_keys},
        )
        cache = feature.cached_data.setdefault("security", {}).setdefault("anti_nuke_punishments", {})
        cache.update({key: value for key in parent.event_keys})
        refreshed = AntinukePunishmentDetailView(feature, parent.key, parent.label, parent.event_keys, value)
        embed = panel(
            f"{icon('tick')} Punishment Saved • {parent.label}",
            f"**Current Punishment:** `{ {'none': 'Log only', 'kick': 'Kick', 'ban': 'Ban'}[value] }`\nChoose another response or use **Back** to configure a different event.",
        )
        await edit_component_card(interaction, embed, refreshed)


class ExtraownerView(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int, owners: list[int] | None = None):
        super().__init__(timeout=240)
        self.bot, self.guild_id, self.owner_id = bot, guild_id, owner_id
        self.owners = list(owners or [])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can configure extraowners.")
            return False
        return True

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success, row=0)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await edit_component_card(interaction, extraowner_embed(self.owners, "Select members to add."), ExtraownerPickerView(self, "add"))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.secondary, row=0)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await edit_component_card(interaction, extraowner_embed(self.owners, "Select members to remove."), ExtraownerPickerView(self, "remove"))

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, row=0)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can change extraowners.")
            return
        await interaction.response.defer()
        await self.bot.db.set_nested(self.guild_id, "security", "extraowners", [])
        self.owners = []
        await edit_original_component_card(interaction, extraowner_embed(self.owners), self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        data = await self.bot.db.get_guild(self.guild_id)
        view = SecurityOverviewView(self.bot, self.guild_id, self.owner_id, data)
        await edit_original_component_card(interaction, security_overview_embed(data), view)


def extraowner_embed(owners: list[int], prompt: str | None = None) -> discord.Embed:
    embed = panel("Configure Extraowner", "Designated user with owner-level command access.")
    embed.add_field(name="Current Extraowners", value="\n".join(f"↳ <@{uid}>" for uid in sorted(owners)) if owners else "↳ No Extraowners", inline=False)
    if prompt:
        embed.add_field(name="Action", value=prompt, inline=False)
    return embed


class ExtraownerPickerView(discord.ui.View):
    def __init__(self, parent: ExtraownerView, action: str):
        super().__init__(timeout=240)
        self.parent_view = parent
        if action == "add":
            self.add_item(MemberSettingSelect(parent, row=0))
        else:
            self.add_item(RemoveExtraownerSelect(parent, row=0))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can configure extraowners.")
            return False
        return True

    @discord.ui.button(label="Back", style=discord.ButtonStyle.primary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await edit_component_card(interaction, extraowner_embed(self.parent_view.owners), self.parent_view)


class MemberSettingSelect(discord.ui.UserSelect):
    def __init__(self, parent: ExtraownerView, *, row: int):
        super().__init__(placeholder="Add trusted extraowners", min_values=1, max_values=25, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can change extraowners.")
            return
        await interaction.response.defer()
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        owners = set(data.get("security", {}).get("extraowners", []))
        owners.update(user.id for user in self.values)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "security", "extraowners", list(owners))
        self.parent_view.owners = sorted(owners)
        await edit_original_component_card(interaction, extraowner_embed(self.parent_view.owners), self.parent_view)


class RemoveExtraownerSelect(discord.ui.UserSelect):
    def __init__(self, parent: ExtraownerView, *, row: int):
        super().__init__(placeholder="Select trusted members to remove", min_values=1, max_values=5, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not is_server_owner(interaction):
            await send_notice(interaction, "Only the server owner can change extraowners.")
            return
        await interaction.response.defer()
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        owners = set(data.get("security", {}).get("extraowners", []))
        owners.difference_update(user.id for user in self.values)
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "security", "extraowners", sorted(owners))
        self.parent_view.owners = sorted(owners)
        await edit_original_component_card(interaction, extraowner_embed(self.parent_view.owners), self.parent_view)


class ChannelSettingSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: CategoryActions, section: str, key: str, placeholder: str, types, *, row: int):
        super().__init__(placeholder=placeholder, channel_types=list(types), min_values=1, max_values=1, row=row)
        self.parent_view, self.section, self.key = parent, section, key

    async def callback(self, interaction: discord.Interaction):
        if not can_manage_view(interaction, self.parent_view.owner_id, self.parent_view):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        if self.section == "security" and self.key == "log_channel":
            bot_member = interaction.guild.me
            permissions = self.values[0].permissions_for(bot_member) if bot_member else None
            if permissions is None or not permissions.send_messages or not permissions.embed_links:
                await send_notice(interaction, "Zyven needs **Send Messages** and **Embed Links** in that channel before it can be used for logs.")
                return
        if self.section == "welcome" and self.key == "channel":
            bot_member = interaction.guild.me
            if bot_member is None or not self.values[0].permissions_for(bot_member).send_messages:
                await send_notice(interaction, "Zyven needs **Send Messages** in that channel before it can be used for welcomes.")
                return
        if self.section == "voicemaster":
            bot_member = interaction.guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels or not bot_member.guild_permissions.move_members:
                await send_notice(interaction, "Zyven needs **Manage Channels** and **Move Members** for VoiceMaster.")
                return
            cfg = (await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)).get("voicemaster", {})
            if self.key == "category":
                destination_category = self.values[0]
            else:
                destination_category = interaction.guild.get_channel(cfg.get("category")) if cfg.get("category") else self.values[0].category
            if destination_category:
                permissions = destination_category.permissions_for(bot_member)
                can_join_destination = permissions.view_channel and permissions.connect
            else:
                can_join_destination = bot_member.guild_permissions.view_channel and bot_member.guild_permissions.connect
            if not can_join_destination:
                await send_notice(interaction, "Zyven needs **View Channel** and **Connect** in the VoiceMaster destination category (or server-wide if the room is outside a category).")
                return
        await interaction.response.defer()
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, self.section, self.key, self.values[0].id)
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        embed, view = category_screen(data, self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, self.parent_view.category)
        await edit_original_component_card(interaction, embed, view)


class WelcomeStyleButton(discord.ui.Button):
    def __init__(self, parent: CategoryActions, style: str, *, row: int):
        self.parent_view = parent
        self.style = style
        if style == "cv2":
            super().__init__(label="CV2 Card", style=discord.ButtonStyle.primary, row=row)
        else:
            super().__init__(label="Classic Embed", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction):
        if not can_manage_view(interaction, self.parent_view.owner_id, self.parent_view):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        await interaction.response.defer()
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "welcome", "style", self.style)
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        embed, view = category_screen(data, self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, "members")
        await edit_original_component_card(interaction, embed, view)


class RoleSettingSelect(discord.ui.RoleSelect):
    def __init__(self, parent: CategoryActions, *, row: int):
        super().__init__(placeholder="Choose the new-member autorole", min_values=1, max_values=1, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.parent_view.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        role = self.values[0]
        guild = interaction.guild
        if role.is_default() or role.managed or role.permissions.value != 0:
            await send_notice(interaction, "Choose a normal, assignable autorole with no permissions enabled.")
            return
        if guild.me is None or role >= guild.me.top_role:
            await send_notice(interaction, "Move the bot role above the autorole first.")
            return
        if interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
            await send_notice(interaction, "You can only configure an autorole below your highest role.")
            return
        await interaction.response.defer()
        await self.parent_view.bot.db.set_nested(self.parent_view.guild_id, "autorole", "role", role.id)
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        embed, view = category_screen(data, self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, "members")
        await edit_original_component_card(interaction, embed, view)


class WelcomeMessageModal(discord.ui.Modal, title="Welcome Message"):
    message = discord.ui.TextInput(label="Message", max_length=1000, required=True)

    def __init__(self, parent: CategoryActions, current: str):
        super().__init__()
        self.parent_view = parent
        self.message.default = current

    async def on_submit(self, interaction: discord.Interaction):
        if not can_manage(interaction, self.parent_view.owner_id):
            await send_notice(interaction, "You need **Manage Server** to change settings.")
            return
        await interaction.response.defer(ephemeral=True)
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        welcome = data.get("welcome", {})
        welcome["message"] = self.message.value
        await self.parent_view.bot.db.set_value(self.parent_view.guild_id, "welcome", welcome)
        data = await self.parent_view.bot.db.get_guild(self.parent_view.guild_id)
        embed, view = category_screen(data, self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id, "members")
        await followup_component_card(interaction, embed, view, ephemeral=True)


class BackButton(discord.ui.Button):
    def __init__(self, parent: CategoryActions):
        emoji = None if parent.category == "members" else icon("back")
        super().__init__(label="Categories", style=discord.ButtonStyle.secondary, emoji=emoji, row=4)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        embed = panel(f"{icon('security')} Zyven Configuration", "Select a category below to view or change its server settings.")
        embed.add_field(name="Configuration", value="Security • AutoMod • Welcome & Autorole • VoiceMaster", inline=False)
        await edit_component_card(interaction, embed, ConfigHome(self.parent_view.bot, self.parent_view.guild_id, self.parent_view.owner_id))
