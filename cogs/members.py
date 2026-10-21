import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict, deque
import asyncio
import time
from datetime import datetime, timezone
import logging
from urllib.parse import urlparse

from utils.emojis import icon
from core.settings import settings
from utils.ui import owner_bypass_permissions, panel, send_card, component_card, notice_panel
from utils.role_safety import dangerous_permissions

log = logging.getLogger("zyven.members")


class WelcomeSetupStyleView(discord.ui.View):
    def __init__(self, bot, guild: discord.Guild, requester_id: int, channel: discord.TextChannel | None, message: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild = guild
        self.requester_id = requester_id
        self.channel = channel
        self.message = message
        self.prompt_message = None
        self.config = {
            "title": "Welcome to {server}!",
            "description": "We're glad you're here. Make yourself at home!",
            "color": "5865F2",
            "author": "",
            "author_url": "",
            "footer": "",
            "footer_url": "",
            "image_url": "",
            "thumbnail_url": "",
            "timestamp": False,
        }
        if channel is None:
            self.add_item(WelcomeChannelSelect(self, row=0))

    def setup_embed(self):
        channel = self.channel.mention if self.channel else "Choose a channel below"
        title = self.config["title"].replace("{server}", self.guild.name)
        description = self.config["description"].replace("{user}", "@NewMember").replace("{server}", self.guild.name).replace("{count}", str(self.guild.member_count or 0))
        embed = panel(
            f"{icon('welcome')} Welcome Embed Builder",
            f"**Channel:** {channel}\n\n**{title}**\n{description}",
            color=int(self.config["color"], 16),
        )
        if self.config["author"]:
            embed.add_field(name=f"{icon('autorole_settings')} Author", value=self.config["author"], inline=True)
        if self.config["footer"]:
            embed.add_field(name="Footer", value=self.config["footer"], inline=True)
        member = self.guild.get_member(self.requester_id)
        preview_variables = {
            "{user_avatar}": member.display_avatar.url if member else "",
            "{server_icon}": self.guild.icon.url if self.guild.icon else "",
        }
        image_url = self.config["image_url"] or self.config["thumbnail_url"]
        for variable, replacement in preview_variables.items():
            image_url = image_url.replace(variable, replacement)
        if image_url:
            embed.set_image(url=image_url)
        return embed

    async def edit_preview(self):
        if self.prompt_message:
            await self.prompt_message.edit(view=component_card(self.setup_embed(), self))

    def open_editor(self, interaction: discord.Interaction, kind: str):
        return interaction.response.send_modal(WelcomeSetupEditorModal(self, kind))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the member who started this welcome setup can choose the style.", ephemeral=True)
        return False

    async def save(self, interaction: discord.Interaction, style: str):
        if interaction.guild is None or interaction.guild.id != self.guild.id:
            await interaction.response.send_message("This welcome setup is no longer active in this server.", ephemeral=True)
            return
        if self.channel is None:
            await interaction.response.send_message("Choose the welcome channel from the channel picker first.", ephemeral=True)
            return
        bot_member = self.guild.me
        permissions = self.channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.send_messages:
            await interaction.response.send_message("Zyven needs **Send Messages** in that channel to post welcomes.", ephemeral=True)
            return
        if style == "classic" and not permissions.embed_links:
            await interaction.response.send_message("Zyven needs **Embed Links** in that channel for the classic bordered embed.", ephemeral=True)
            return
        await interaction.response.defer()
        welcome = (await self.bot.db.get_guild(self.guild.id)).get("welcome", {})
        welcome.update({"channel": self.channel.id, "message": self.message, "style": style, **self.config})
        # Treat the old builder defaults as blank for existing server configs.
        if welcome.get("footer") == "Member #{count}":
            welcome["footer"] = ""
            welcome["timestamp"] = False
        welcome.setdefault("enabled", True)
        await self.bot.db.set_value(self.guild.id, "welcome", welcome)
        label = f"{icon('welcomer')} CV2 clean card" if style == "cv2" else f"{icon('autorole_settings')} Classic bordered embed"
        embed = panel(f"{icon('autorole_success')} Welcome Setup Complete", f"**Channel:** {self.channel.mention}\n**Style:** {label}\n\nYour custom welcome layout is saved and ready.")
        self.stop()
        await interaction.edit_original_response(view=component_card(embed))

    @discord.ui.button(label="Basic Info", style=discord.ButtonStyle.secondary, row=1)
    async def edit_basic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_editor(interaction, "basic")

    @discord.ui.button(label="Author", style=discord.ButtonStyle.secondary, row=1)
    async def edit_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_editor(interaction, "author")

    @discord.ui.button(label="Footer", style=discord.ButtonStyle.secondary, row=1)
    async def edit_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_editor(interaction, "footer")

    @discord.ui.button(label="Images", style=discord.ButtonStyle.secondary, row=1)
    async def edit_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_editor(interaction, "images")

    @discord.ui.button(label="Welcome Message", style=discord.ButtonStyle.secondary, row=2)
    async def edit_greeting(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_editor(interaction, "greeting")

    @discord.ui.button(label="Variables", style=discord.ButtonStyle.secondary, row=2)
    async def show_variables(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = panel(
            f"{icon('welcome')} Welcome Message Variables",
            "Use these placeholders in the greeting, title, description, author, or footer. They are replaced with details for the member who joins.",
        )
        embed.add_field(name="Member", value="`{user}` mention\n`{user_name}` username\n`{user_tag}` username tag\n`{user_discrim}` legacy discriminator\n`{user_nick}` server display name\n`{user_id}` ID\n`{user_avatar}` avatar URL\n`{user_joindate}` joined date\n`{user_createdate}` account creation date\n`{user_displaycolor}` role color\n`{user_boostsince}` boost date", inline=False)
        embed.add_field(name="Server", value="`{server}` / `{server_name}` server name\n`{server_id}` server ID\n`{server_icon}` icon URL\n`{server_owner}` owner mention\n`{server_owner_id}` owner ID\n`{server_createdate}` creation date\n`{server_membercount}` / `{count}` member count\n`{server_membercount_ordinal}` member number with ordinal suffix\n`{server_membercount_nobots}` human count\n`{server_membercount_nobots_ordinal}` human count with ordinal suffix\n`{server_botcount}` bot count\n`{server_botcount_ordinal}` bot count with ordinal suffix\n`{server_rolecount}` role count\n`{server_channelcount}` channel count", inline=False)
        embed.add_field(name="Boosts", value="`{server_boostlevel}` current level\n`{server_boostcount}` total boosts\n`{server_nextboostlevel}` next level\n`{server_nextboostlevel_required}` boosts needed for next level\n`{server_nextboostlevel_until_required}` boosts remaining", inline=False)
        embed.add_field(name="Channel and date", value="`{channel}` welcome channel mention\n`{channel_name}` welcome channel name\n`{date}` current date and time\n`{newline}` line break", inline=False)
        embed.set_footer(text="Economy, inventory, autoresponder, and command-argument functions are not part of welcome messages.")
        await interaction.response.send_message(view=component_card(embed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Save as CV2", style=discord.ButtonStyle.primary, row=2)
    async def choose_cv2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, "cv2")

    @discord.ui.button(label="Save as Classic", style=discord.ButtonStyle.secondary, row=2)
    async def choose_classic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, "classic")


class WelcomeChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: WelcomeSetupStyleView, *, row: int):
        super().__init__(placeholder="Select the welcome channel", channel_types=[discord.ChannelType.text, discord.ChannelType.news], min_values=1, max_values=1, row=row)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        channel = self.parent_view.guild.get_channel(selected.id)
        if channel is None:
            try:
                channel = await self.parent_view.guild.fetch_channel(selected.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Choose a text or announcement channel that Zyven can access.", ephemeral=True)
            return
        bot_member = self.parent_view.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.send_messages:
            await interaction.response.send_message("Zyven needs **Send Messages** in that channel.", ephemeral=True)
            return
        self.parent_view.channel = channel
        await interaction.response.defer()
        await interaction.edit_original_response(view=component_card(self.parent_view.setup_embed(), self.parent_view))


class WelcomeSetupEditorModal(discord.ui.Modal):
    def __init__(self, parent: WelcomeSetupStyleView, kind: str):
        titles = {"basic": "Basic Information", "author": "Edit Author", "footer": "Edit Footer", "images": "Edit Images", "greeting": "Edit Welcome Message"}
        super().__init__(title=titles[kind])
        self.parent_view = parent
        self.kind = kind
        self.inputs = {}
        fields = {
            "basic": (("title", "Title", 256), ("description", "Description", 4000), ("color", "Hex color, e.g. 5865F2", 7)),
            "author": (("author", "Author text", 256), ("author_url", "Author icon URL (optional)", 400)),
            "footer": (("footer", "Footer text", 2048), ("footer_url", "Footer icon URL (optional)", 400)),
            "images": (("image_url", "Main image URL or {user_avatar}", 400), ("thumbnail_url", "Thumbnail URL or {user_avatar}", 400)),
            "greeting": (("message", "Welcome message with {user}, {server}, {count}", 1000),),
        }[kind]
        for key, label, max_length in fields:
            value = parent.message if key == "message" else parent.config.get(key, "")
            text_input = discord.ui.TextInput(
                label=label,
                default=value or None,
                max_length=max_length,
                required=key in {"title", "description", "color", "message"},
                style=discord.TextStyle.paragraph if key == "description" else discord.TextStyle.short,
            )
            self.inputs[key] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction):
        updates = {key: item.value.strip() for key, item in self.inputs.items()}
        for key, value in updates.items():
            if key.endswith("_url") and value:
                templated_url = value.replace("{user_avatar}", "https://example.com/avatar.png").replace("{server_icon}", "https://example.com/server.png")
                parsed = urlparse(templated_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    await interaction.response.send_message(f"`{key}` must be a valid http/https image URL, `{{user_avatar}}`, or `{{server_icon}}`.", ephemeral=True)
                    return
        if self.kind == "greeting":
            self.parent_view.message = updates.pop("message")
        if self.kind == "basic":
            color = updates["color"].removeprefix("#")
            try:
                if len(color) != 6:
                    raise ValueError
                int(color, 16)
            except ValueError:
                await interaction.response.send_message("Enter a 6-digit hex color such as `5865F2`.", ephemeral=True)
                return
            updates["color"] = color
        self.parent_view.config.update(updates)
        await interaction.response.defer(ephemeral=True)
        try:
            await self.parent_view.edit_preview()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Editor Updated", "Your changes are in the setup card. Choose **Save as CV2** or **Save as Classic** when ready.")), ephemeral=True)


def _autorole_ids(config: dict, member_type: str) -> list[int]:
    """Read current and legacy autorole field names without losing saved roles."""
    keys = ("bot_roles", "bots", "bot") if member_type == "bots" else ("human_roles", "humans", "human")
    values = []
    for key in keys:
        value = config.get(key, [])
        if value is None:
            continue
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        for item in value:
            if isinstance(item, dict):
                item = item.get("role_id", item.get("role", item.get("id")))
            try:
                if item is not None:
                    values.append(int(item))
            except (TypeError, ValueError):
                continue
    # The original setautorole command stored one role at autorole.role.
    if member_type == "humans" and config.get("role") is not None:
        try:
            values.append(int(config["role"]))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(values))


def _welcome_payload(guild: discord.Guild, member: discord.Member, welcome: dict):
    channel = guild.get_channel(welcome.get("channel")) if welcome.get("channel") else None
    owner = guild.owner
    now = int(datetime.now(timezone.utc).timestamp())
    member_count = str(guild.member_count or 0)
    bot_count = str(sum(user.bot for user in guild.members)) if guild.chunked else "Unavailable"
    human_count = str(sum(not user.bot for user in guild.members)) if guild.chunked else "Unavailable"
    ordinal = lambda value: f"{value}{'th' if 10 <= value % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(value % 10, 'th')}"
    variables = {
        "{user}": member.mention,
        "{user_name}": member.name,
        "{user_tag}": str(member),
        "{user_id}": str(member.id),
        "{user_avatar}": member.display_avatar.url,
        "{user_nick}": member.display_name,
        "{user_joindate}": f"<t:{int(member.joined_at.timestamp())}:F>" if member.joined_at else "Unknown",
        "{user_createdate}": f"<t:{int(member.created_at.timestamp())}:F>",
        "{user_displaycolor}": f"#{member.color.value:06X}",
        "{user_discrim}": member.discriminator,
        "{user_boostsince}": f"<t:{int(member.premium_since.timestamp())}:F>" if member.premium_since else "Not boosting",
        "{server}": guild.name,
        "{server_name}": guild.name,
        "{server_id}": str(guild.id),
        "{server_icon}": guild.icon.url if guild.icon else "",
        "{server_owner}": owner.mention if owner else "",
        "{server_owner_id}": str(guild.owner_id),
        "{server_createdate}": f"<t:{int(guild.created_at.timestamp())}:F>",
        "{server_membercount}": member_count,
        "{server_membercount_ordinal}": ordinal(guild.member_count or 0),
        "{server_membercount_nobots}": human_count,
        "{server_membercount_nobots_ordinal}": ordinal(int(human_count)) if human_count.isdigit() else human_count,
        "{count}": member_count,
        "{server_botcount}": bot_count,
        "{server_botcount_ordinal}": ordinal(int(bot_count)) if bot_count.isdigit() else bot_count,
        "{server_rolecount}": str(len(guild.roles)),
        "{server_channelcount}": str(len(guild.channels)),
        "{server_boostlevel}": str(guild.premium_tier),
        "{server_boostcount}": str(guild.premium_subscription_count or 0),
        "{server_nextboostlevel}": str(min(guild.premium_tier + 1, 3)),
        "{server_nextboostlevel_required}": str({0: 2, 1: 7, 2: 14}.get(guild.premium_tier, 0)),
        "{server_nextboostlevel_until_required}": str(max({0: 2, 1: 7, 2: 14}.get(guild.premium_tier, 0) - (guild.premium_subscription_count or 0), 0)),
        "{channel}": channel.mention if channel else "",
        "{channel_name}": channel.name if channel else "",
        "{date}": f"<t:{now}:F>",
        "{newline}": "\n",
    }

    def render(value: str, *, mention_user: bool = True) -> str:
        rendered = value or ""
        for variable, replacement in variables.items():
            rendered = rendered.replace(variable, replacement)
        if not mention_user:
            rendered = rendered.replace(member.mention, member.display_name)
            if owner:
                rendered = rendered.replace(owner.mention, owner.display_name)
        return rendered

    content = render(welcome.get("message", "Welcome {user} to {server}!"))
    # Discord doesn't resolve mentions in embed titles, so render names there.
    title = render(welcome.get("title", "Welcome to {server}!"), mention_user=False)
    description = render(welcome.get("description", "We're glad you're here. Make yourself at home!"))
    footer_source = welcome.get("footer", "")
    # Older versions inserted this footer by default; it was never user-authored.
    if footer_source == "Member #{count}":
        footer_source = ""
    footer = render(footer_source)
    author = render(welcome.get("author", ""))
    author_url = render(welcome.get("author_url", ""))
    footer_url = render(welcome.get("footer_url", ""))
    image_url = render(welcome.get("image_url", ""))
    thumbnail_url = render(welcome.get("thumbnail_url", ""))
    color_value = str(welcome.get("color", "5865F2")).removeprefix("#")
    try:
        color = int(color_value, 16) if len(color_value) == 6 else 0x5865F2
    except ValueError:
        color = 0x5865F2
    embed = discord.Embed(title=title or None, description=description or None, color=color)
    if welcome.get("timestamp", False) and footer:
        embed.timestamp = datetime.now(timezone.utc)
    if author:
        embed.set_author(name=author, icon_url=author_url or None)
    if footer:
        embed.set_footer(text=footer, icon_url=footer_url or None)
    if image_url:
        embed.set_image(url=image_url)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if welcome.get("style", "classic") == "cv2":
        # The greeting is sent as ordinary message content before the CV2 card.
        card_lines = [line for line in (f"**{author}**" if author else "", description) if line]
        embed.description = "\n\n".join(card_lines) or "\u200b"
        cv2_image_url = image_url or thumbnail_url
        if cv2_image_url:
            embed.set_image(url=cv2_image_url)
    return content, embed, welcome.get("style", "classic")


async def _send_welcome(channel, member: discord.Member, welcome: dict):
    content, embed, style = _welcome_payload(member.guild, member, welcome)
    allowed_mentions = discord.AllowedMentions(users=[member], roles=False, everyone=False)
    if style == "cv2":
        messages = []
        if content.strip():
            messages.append(await channel.send(content=content, allowed_mentions=allowed_mentions))
        messages.append(await channel.send(view=component_card(embed), allowed_mentions=allowed_mentions))
    else:
        messages = [await channel.send(content=content, embed=embed, allowed_mentions=allowed_mentions)]
    delete_after = int(welcome.get("autodelete", 0) or 0)
    if delete_after > 0:
        for message in messages:
            await message.delete(delay=delete_after)
    return messages[-1]


class Members(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.join_bursts = defaultdict(lambda: deque(maxlen=500))
        self.raid_alerted = set()
        self.temporary_voice_channels = set()
        self.joins_since_sweep = 0
        self._tag_profile_cache: dict[int, tuple[float, object]] = {}
        self._tag_gateway_profiles: dict[int, tuple[bool, int | None]] = {}
        self._tag_profile_fetches = asyncio.Semaphore(4)

    @commands.hybrid_group(name="vanityroles", invoke_without_command=True, case_insensitive=True, description="Give members a role when they use the configured vanity status")
    @commands.guild_only()
    async def vanityroles(self, ctx: commands.Context):
        from cogs.helpdesk import HelpPagesView

        entries = [
            (f"{settings.prefix}vanityroles setup <vanity> <role> <channel>", "Set the status text, role, and notification channel."),
            (f"{settings.prefix}vanityroles config", "Show the current vanity role configuration."),
            (f"{settings.prefix}vanityroles reset", "Remove the vanity role configuration."),
        ]
        view = HelpPagesView(requester_id=ctx.author.id, title="Zyven Vanityroles Group", entries=entries, page_size=5).set_requester(ctx.author.display_name)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @vanityroles.command(name="setup", description="Set the vanity status, role, and notification channel")
    @app_commands.describe(vanity="Invite URL or status text members must use", role="Role to grant/remove", channel="Channel for vanity role notifications")
    async def vanityroles_setup(self, ctx: commands.Context, vanity: str, role: discord.Role, channel: discord.TextChannel):
        if ctx.author.id not in {ctx.guild.owner_id, settings.owner_id} and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure vanity roles.")), allowed_mentions=discord.AllowedMentions.none())
            return
        bot_member = ctx.guild.me
        if role.is_default() or role.managed or (bot_member is not None and role >= bot_member.top_role):
            await ctx.send(view=component_card(notice_panel("Choose a regular role below Zyven's highest role.")), allowed_mentions=discord.AllowedMentions.none())
            return
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Manage Roles** to grant the vanity role.")), allowed_mentions=discord.AllowedMentions.none())
            return
        if not channel.permissions_for(bot_member).send_messages or not channel.permissions_for(bot_member).embed_links:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** and **Embed Links** in the notification channel.")), allowed_mentions=discord.AllowedMentions.none())
            return
        tag_config = (await self.bot.db.get_guild(ctx.guild.id)).get("tagroles", {})
        if int(tag_config.get("role", 0) or 0) == role.id:
            await ctx.send(view=component_card(notice_panel("Vanity Role and Tag Role need different roles so each system can manage its own role safely.")), allowed_mentions=discord.AllowedMentions.none())
            return
        vanity = vanity.strip()
        if not vanity:
            await ctx.send(view=component_card(notice_panel("Enter the vanity invite or status text to match.")), allowed_mentions=discord.AllowedMentions.none())
            return
        if "discord.gg/" in vanity.casefold() and not vanity.casefold().startswith(("http://", "https://")):
            vanity = "https://" + vanity
        elif "discord.gg/" not in vanity.casefold() and "/" not in vanity and " " not in vanity:
            vanity = f"https://discord.gg/{vanity}"
        await self.bot.db.set_value(ctx.guild.id, "vanityroles", {"status": vanity, "role": role.id, "channel": channel.id})

        embed = panel(f"{icon('antibot_success')} Vanity Role Setup Complete")
        embed.description = (
            f"{icon('dotmoving')} **Vanity Status:** `{vanity}`\n"
            f"{icon('dotmoving')} **Role:** {role.mention}\n"
            f"{icon('dotmoving')} **Channel:** {channel.mention}\n\n"
            f"{icon('arrow_arrow')} Users with this status will receive the role automatically"
        )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @vanityroles.command(name="config", aliases=("show",), description="Show the current vanity role configuration")
    async def vanityroles_show(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("vanityroles", {})
        role = ctx.guild.get_role(int(config.get("role", 0))) if config.get("role") else None
        channel = ctx.guild.get_channel(int(config.get("channel", 0))) if config.get("channel") else None
        if not config.get("status") or role is None or channel is None:
            await ctx.send(view=component_card(notice_panel("Vanity roles are not configured. Use `,vanityroles setup <vanity> <role> <channel>`.")), allowed_mentions=discord.AllowedMentions.none())
            return
        embed = panel(f"{icon('vanityroles')} Vanity Role Configuration", f"**Vanity Status:** `{config['status']}`\n**Role:** {role.mention}\n**Channel:** {channel.mention}")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @vanityroles.command(name="reset", description="Remove the vanity role configuration")
    async def vanityroles_reset(self, ctx: commands.Context):
        if ctx.author.id not in {ctx.guild.owner_id, settings.owner_id} and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure vanity roles.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self.bot.db.unset_value(ctx.guild.id, "vanityroles")
        await ctx.send(view=component_card(panel(f"{icon('antibot_success')} Vanity Role Reset", "The vanity role configuration has been removed.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name="tagroles", invoke_without_command=True, case_insensitive=True, description="Give members a role while they display this server's tag")
    @commands.guild_only()
    async def tagroles(self, ctx: commands.Context):
        from cogs.helpdesk import HelpPagesView

        entries = [
            (f"{settings.prefix}tagroles setup <role> <channel>", "Give a role to members who display this server's Discord Server Tag."),
            (f"{settings.prefix}tagroles config", "Show the current server tag role configuration."),
            (f"{settings.prefix}tagroles reset", "Remove the server tag role configuration."),
        ]
        view = HelpPagesView(requester_id=ctx.author.id, title="Zyven Tagroles Group", entries=entries, page_size=5).set_requester(ctx.author.display_name)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @tagroles.command(name="setup", description="Set the role and log channel for members displaying this server's tag")
    @app_commands.describe(role="Role granted while a member displays this server's tag", channel="Channel for tag role notifications")
    async def tagroles_setup(self, ctx: commands.Context, role: discord.Role, channel: discord.TextChannel):
        if ctx.author.id not in {ctx.guild.owner_id, settings.owner_id} and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure tag roles.")), allowed_mentions=discord.AllowedMentions.none())
            return
        bot_member = ctx.guild.me
        if role.is_default() or role.managed or (bot_member is not None and role >= bot_member.top_role):
            await ctx.send(view=component_card(notice_panel("Choose a regular role below Zyven's highest role.")), allowed_mentions=discord.AllowedMentions.none())
            return
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Manage Roles** to grant the tag role.")), allowed_mentions=discord.AllowedMentions.none())
            return
        channel_permissions = channel.permissions_for(bot_member)
        if not channel_permissions.send_messages or not channel_permissions.embed_links:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** and **Embed Links** in the notification channel.")), allowed_mentions=discord.AllowedMentions.none())
            return
        vanity_config = (await self.bot.db.get_guild(ctx.guild.id)).get("vanityroles", {})
        if int(vanity_config.get("role", 0) or 0) == role.id:
            await ctx.send(view=component_card(notice_panel("Tag Role and Vanity Role need different roles so each system can manage its own role safely.")), allowed_mentions=discord.AllowedMentions.none())
            return

        await self.bot.db.set_value(ctx.guild.id, "tagroles", {"role": role.id, "channel": channel.id})
        embed = panel(f"{icon('antibot_success')} Tag Role Setup Complete")
        embed.description = (
            f"{icon('dotmoving')} **Role:** {role.mention}\n"
            f"{icon('dotmoving')} **Channel:** {channel.mention}\n\n"
            f"{icon('arrow_arrow')} Members displaying this server's Discord Server Tag will receive the role automatically."
        )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @tagroles.command(name="config", aliases=("show",), description="Show the current server tag role configuration")
    async def tagroles_show(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("tagroles", {})
        role = ctx.guild.get_role(int(config.get("role", 0))) if config.get("role") else None
        channel = ctx.guild.get_channel(int(config.get("channel", 0))) if config.get("channel") else None
        if role is None or channel is None:
            await ctx.send(view=component_card(notice_panel("Tag roles are not configured. Use `,tagroles setup <role> <channel>`.")), allowed_mentions=discord.AllowedMentions.none())
            return
        embed = panel(f"{icon('vanityroles')} Server Tag Role Configuration", f"**Role:** {role.mention}\n**Channel:** {channel.mention}")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @tagroles.command(name="reset", description="Remove the server tag role configuration")
    async def tagroles_reset(self, ctx: commands.Context):
        if ctx.author.id not in {ctx.guild.owner_id, settings.owner_id} and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure tag roles.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self.bot.db.unset_value(ctx.guild.id, "tagroles")
        await ctx.send(view=component_card(panel(f"{icon('antibot_success')} Tag Role Reset", "The server tag role configuration has been removed.")), allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _primary_guild_has_tag(primary_guild, guild_id: int) -> bool:
        if primary_guild is None:
            return False
        if isinstance(primary_guild, dict):
            identity_enabled = primary_guild.get("identity_enabled")
            identity_guild_id = primary_guild.get("identity_guild_id")
        else:
            identity_enabled = getattr(primary_guild, "identity_enabled", None)
            identity_guild_id = getattr(primary_guild, "identity_guild_id", None)
        if identity_enabled is not True:
            return False
        try:
            return int(identity_guild_id) == guild_id
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _primary_guild_signature(primary_guild) -> tuple[bool, int | None]:
        if isinstance(primary_guild, dict):
            enabled = primary_guild.get("identity_enabled") is True
            guild_id = primary_guild.get("identity_guild_id")
        else:
            enabled = getattr(primary_guild, "identity_enabled", None) is True
            guild_id = getattr(primary_guild, "identity_guild_id", None)
        if not enabled:
            return (False, None)
        try:
            return (True, int(guild_id))
        except (TypeError, ValueError):
            return (True, None)

    async def _uses_this_server_tag(self, member: discord.Member, *, fetch_missing: bool = True) -> bool | None:
        primary_guild = getattr(member, "primary_guild", None)
        if primary_guild is None and fetch_missing:
            # Member profile fields are not consistently refreshed on every
            # discord.py version. Keep a short cache for fallback fetches, but
            # invalidate it whenever a raw member update arrives below.
            cached = self._tag_profile_cache.get(member.id)
            if cached and time.monotonic() - cached[0] < 3:
                primary_guild = cached[1]
            else:
                async with self._tag_profile_fetches:
                    try:
                        user = await self.bot.fetch_user(member.id)
                    except discord.HTTPException:
                        log.warning("Could not fetch Discord primary-guild profile for tag role member %s", member.id)
                        return None
                primary_guild = getattr(user, "primary_guild", None)
                self._tag_profile_cache[member.id] = (time.monotonic(), primary_guild)
        elif primary_guild is None:
            return None
        return self._primary_guild_has_tag(primary_guild, member.guild.id)

    async def _sync_tag_role_for_primary_guild(self, member: discord.Member, primary_guild, *, config: dict | None = None):
        guild_config = None
        if config is None:
            guild_config = await self.bot.db.get_guild(member.guild.id)
            config = guild_config.get("tagroles", {})
        role = member.guild.get_role(int(config.get("role", 0))) if config.get("role") else None
        channel = member.guild.get_channel(int(config.get("channel", 0))) if config.get("channel") else None
        if role is None or channel is None:
            return
        if guild_config is None:
            guild_config = await self.bot.db.get_guild(member.guild.id)
        vanity_config = guild_config.get("vanityroles", {})
        if int(vanity_config.get("role", 0) or 0) == role.id:
            log.warning("Tag role is disabled for guild %s because it shares the vanity role; configure a separate role", member.guild.id)
            return
        uses_tag = self._primary_guild_has_tag(primary_guild, member.guild.id)
        has_role = role in member.roles
        if uses_tag == has_role:
            return
        try:
            if uses_tag:
                await member.add_roles(role, reason="Member enabled this server's Discord Server Tag")
                title = f"{icon('vanity_status')} Server Tag Role Added"
                description = f"{member.mention} received {role.mention} for displaying this server's tag."
            else:
                await member.remove_roles(role, reason="Member removed this server's Discord Server Tag")
                title = f"{icon('vanity_status')} Server Tag Role Removed"
                description = f"{role.mention} was removed from {member.mention} because they no longer display this server's tag."
            embed = discord.Embed(title=title, description=description, color=0x57F287)
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.warning("Missing role or channel permissions for tag role update of member %s in guild %s", member.id, member.guild.id)
        except discord.HTTPException:
            log.exception("Could not update tag role or send its notification for member %s in guild %s", member.id, member.guild.id)

    async def _sync_tag_role_member(self, member: discord.Member, config: dict, *, fetch_missing: bool = True):
        role = member.guild.get_role(int(config.get("role", 0))) if config.get("role") else None
        channel = member.guild.get_channel(int(config.get("channel", 0))) if config.get("channel") else None
        if role is None or channel is None:
            return
        vanity_config = (await self.bot.db.get_guild(member.guild.id)).get("vanityroles", {})
        if int(vanity_config.get("role", 0) or 0) == role.id:
            log.warning("Tag role is disabled for guild %s because it shares the vanity role; configure a separate role", member.guild.id)
            return
        uses_tag = await self._uses_this_server_tag(member, fetch_missing=fetch_missing)
        if uses_tag is None:
            return
        has_role = role in member.roles
        if uses_tag == has_role:
            return
        try:
            if uses_tag:
                await member.add_roles(role, reason="Member enabled this server's Discord Server Tag")
                title = f"{icon('vanity_status')} Server Tag Role Added"
                description = f"{member.mention} received {role.mention} for displaying this server's tag."
            else:
                await member.remove_roles(role, reason="Member removed this server's Discord Server Tag")
                title = f"{icon('vanity_status')} Server Tag Role Removed"
                description = f"{role.mention} was removed from {member.mention} because they no longer display this server's tag."
            embed = discord.Embed(title=title, description=description, color=0x57F287)
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.warning("Missing role or channel permissions for tag role update of member %s in guild %s", member.id, member.guild.id)
        except discord.HTTPException:
            log.exception("Could not update tag role or send its notification for member %s in guild %s", member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        # Some discord.py builds don't copy primary_guild from the member
        # update payload into the cached Member. If it is available and changed,
        # reconcile from it; raw gateway updates are handled separately below.
        if getattr(before, "primary_guild", None) != getattr(after, "primary_guild", None):
            self._tag_profile_cache.pop(after.id, None)
            await self._sync_tag_role_for_primary_guild(after, after.primary_guild)

    @commands.Cog.listener()
    async def on_socket_response(self, payload: dict):
        # GUILD_MEMBER_UPDATE carries the authoritative Server Tag identity.
        # Read it directly so a stale cached User/Member object cannot make the
        # tag role silently follow vanity custom-status updates instead.
        if payload.get("t") != "GUILD_MEMBER_UPDATE":
            return
        data = payload.get("d") or {}
        user_data = data.get("user") or {}
        if "primary_guild" not in user_data:
            return
        try:
            guild_id = int(data.get("guild_id", 0))
            member_id = int(user_data.get("id", 0))
        except (TypeError, ValueError):
            return
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(member_id) if guild else None
        if member is None or member.bot:
            return
        new_profile = user_data["primary_guild"]
        new_signature = self._primary_guild_signature(new_profile)
        previous_signature = self._tag_gateway_profiles.get(member_id)
        if previous_signature is None:
            previous_signature = self._primary_guild_signature(getattr(member, "primary_guild", None))
        self._tag_profile_cache[member_id] = (time.monotonic(), user_data["primary_guild"])
        self._tag_gateway_profiles[member_id] = new_signature
        # Role/nickname/timeout updates also generate GUILD_MEMBER_UPDATE. Do
        # not treat those as Server Tag changes or fight the vanity role handler.
        if new_signature == previous_signature:
            return
        await self._sync_tag_role_for_primary_guild(member, new_profile)

    @staticmethod
    def _vanity_status_matches(status: str, configured: str) -> bool:
        def normalize(value: str) -> str:
            value = value.strip().casefold().replace("https://", "").replace("http://", "")
            value = value.removeprefix("www.").rstrip("/ ")
            return value
        return bool(normalize(configured)) and normalize(configured) in normalize(status)

    @staticmethod
    def _custom_status(member: discord.Member) -> str:
        for activity in member.activities:
            if isinstance(activity, discord.CustomActivity) and activity.name:
                return activity.name
        return ""

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        previous_status = self._custom_status(before)
        current_status = self._custom_status(after)
        # Presence events fire for every activity update. Ignore non-status
        # changes before writing local settings or copying the guild configuration.
        if previous_status == current_status:
            return
        guild_config = await self.bot.db.get_guild(after.guild.id)
        config = guild_config.get("vanityroles", {})
        role = after.guild.get_role(int(config.get("role", 0))) if config.get("role") else None
        channel = after.guild.get_channel(int(config.get("channel", 0))) if config.get("channel") else None
        vanity = str(config.get("status") or "")
        if role is None or channel is None or not vanity:
            return
        had_status = self._vanity_status_matches(previous_status, vanity)
        has_status = self._vanity_status_matches(current_status, vanity)
        if had_status == has_status:
            return
        try:
            if has_status and role not in after.roles:
                await after.add_roles(role, reason="Member added configured vanity invite to custom status")
                title = f"{icon('vanity_status')} Vanity Role Added"
                description = f"{after.mention} has been given the {role.mention} role for repping our vanity\n`{vanity}` in their status. Thanks for supporting!"
            elif had_status and role in after.roles:
                await after.remove_roles(role, reason="Member removed configured vanity invite from custom status")
                title = f"{icon('vanity_status')} Vanity Removed"
                description = (f"{after.mention} has been removed from the {role.mention} role for no longer repping our vanity\n"
                               f"`{vanity}` in their status. Thanks for your previous Support!")
            else:
                return
            embed = discord.Embed(title=title, description=description, color=0x57F287)
            embed.set_thumbnail(url=after.display_avatar.url)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            log.warning("Missing permissions to update vanity role for member %s in guild %s", after.id, after.guild.id)
        except discord.HTTPException:
            log.exception("Could not update vanity role for member %s in guild %s", after.id, after.guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        doc = await self.bot.db.get_guild(member.guild.id)
        security = doc.get("security", {})
        if security.get("anti_raid") and member.id not in security.get("extraowners", []):
            threshold = security.get("anti_raid_threshold", 8)
            now = datetime.now(timezone.utc).timestamp()
            self.joins_since_sweep += 1
            if self.joins_since_sweep >= 250:
                self.joins_since_sweep = 0
                stale = [guild_id for guild_id, events in self.join_bursts.items() if not events or now - events[-1] > 60]
                for guild_id in stale:
                    self.join_bursts.pop(guild_id, None)
                    self.raid_alerted.discard(guild_id)
            joins = self.join_bursts[member.guild.id]
            while joins and now - joins[0] > 10:
                joins.popleft()
            joins.append(now)
            if len(joins) >= threshold and member.guild.id not in self.raid_alerted:
                self.raid_alerted.add(member.guild.id)
                log_id = security.get("log_channel")
                log_channel = member.guild.get_channel(log_id) if log_id else None
                if log_channel:
                    try:
                        await log_channel.send(view=component_card(panel(f"{icon('warning')} Anti-raid Alert", f"{len(joins)} members joined within 10 seconds (alert threshold: {threshold}). Please review recent joins." , color=0xFEE75C)))
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning("Could not send anti-raid alert in guild %s", member.guild.id)
                self.bot.loop.create_task(self.clear_raid_alert(member.guild.id))
        config = doc.get("autorole", {})
        role_ids = _autorole_ids(config, "bots" if member.bot else "humans")
        bot_member = member.guild.me
        roles = []
        skipped = []
        for role_id in role_ids:
            role = member.guild.get_role(role_id)
            if role is None:
                skipped.append(f"{role_id}: role no longer exists")
            elif role.is_default() or role.managed:
                skipped.append(f"{role.name} ({role.id}): managed/default role")
            elif dangerous_permissions(role):
                skipped.append(f"{role.name} ({role.id}): restricted permissions")
            elif bot_member is None or role >= bot_member.top_role:
                skipped.append(f"{role.name} ({role.id}): at/above bot's top role")
            elif role not in member.roles:
                roles.append(role)
        if skipped:
            log.warning(
                "Skipped unassignable autorole(s) for guild %s member %s: %s",
                member.guild.id, member.id, "; ".join(skipped),
            )
        if roles:
            try:
                # One member edit for every configured role avoids one API call
                # per role; discord.py's route limiter handles Discord 429s.
                await member.add_roles(*roles, atomic=False, reason="Configured Zyven autorole")
            except discord.Forbidden:
                log.warning(
                    "Autorole assignment forbidden in guild %s for member %s; check Manage Roles and role hierarchy",
                    member.guild.id, member.id,
                )
            except discord.HTTPException as error:
                log.warning(
                    "Autorole API request failed in guild %s for member %s (status=%s code=%s): %s",
                    member.guild.id, member.id, error.status, error.code, error,
                )
        welcome = doc.get("welcome", {})
        channel = member.guild.get_channel(welcome.get("channel")) if welcome.get("channel") else None
        if channel and welcome.get("enabled", True):
            try:
                await _send_welcome(channel, member, welcome)
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send a welcome message in guild %s", member.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Remove persisted VoiceMaster ownership when a room is deleted outside the bot."""
        if channel.id not in self.temporary_voice_channels:
            return
        self.temporary_voice_channels.discard(channel.id)
        try:
            await self.bot.db.pull_value(channel.guild.id, "voicemaster.temporary_channels", channel.id)
            await self.bot.db.unset_value(channel.guild.id, f"voicemaster.temporary_owners.{channel.id}")
        except Exception:
            log.exception("Could not remove deleted temporary room %s from local storage", channel.id)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            cfg = (await self.bot.db.get_guild(guild.id)).get("voicemaster", {})
            tracked = cfg.get("temporary_channels", [])
            for channel_id in tracked:
                channel = guild.get_channel(channel_id)
                if channel is None:
                    await self.bot.db.pull_value(guild.id, "voicemaster.temporary_channels", channel_id)
                    await self.bot.db.unset_value(guild.id, f"voicemaster.temporary_owners.{channel_id}")
                elif channel.members:
                    self.temporary_voice_channels.add(channel_id)
                else:
                    try:
                        await channel.delete(reason="Cleaning up an empty Zyven temporary room after restart")
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        continue
                    self.temporary_voice_channels.discard(channel_id)
                    await self.bot.db.pull_value(guild.id, "voicemaster.temporary_channels", channel_id)
                    await self.bot.db.unset_value(guild.id, f"voicemaster.temporary_owners.{channel_id}")

    async def clear_raid_alert(self, guild_id: int):
        await asyncio.sleep(60)
        self.raid_alerted.discard(guild_id)

    @app_commands.command(name="setwelcome", description="Configure the welcome channel, message, and card style")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_guild=True)
    @app_commands.choices(style=[
        app_commands.Choice(name="CV2 clean card", value="cv2"),
        app_commands.Choice(name="Classic bordered embed", value="classic"),
    ])
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        style: app_commands.Choice[str],
        message: str = "Welcome {user} to {server}!",
    ):
        if len(message) > 1000:
            await send_card(interaction, notice_panel("Welcome messages can be at most 1000 characters."), ephemeral=True)
            return
        bot_member = interaction.guild.me
        channel_permissions = channel.permissions_for(bot_member) if bot_member else None
        if channel_permissions is None or not channel_permissions.send_messages:
            await send_card(interaction, notice_panel("Zyven needs **Send Messages** in that channel before it can post welcome cards."), ephemeral=True)
            return
        if style.value == "classic" and not channel_permissions.embed_links:
            await send_card(interaction, notice_panel("Zyven needs **Embed Links** in that channel for the classic bordered embed."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        welcome = (await self.bot.db.get_guild(interaction.guild_id)).get("welcome", {})
        welcome.update({"channel": channel.id, "message": message, "style": style.value})
        welcome.setdefault("enabled", True)
        await self.bot.db.set_value(interaction.guild_id, "welcome", welcome)
        style_label = "CV2 clean card" if style.value == "cv2" else "Classic bordered embed"
        embed = panel(f"{icon('welcome')} Welcome Setup Complete", f"📍 **Channel:** {channel.mention}\n🎨 **Style:** {style_label}\n\nUse `{{user}}`, `{{server}}`, and `{{count}}` in your welcome text.")
        embed.add_field(name="Preview", value=message or "*(empty)*", inline=False)
        await send_card(interaction, embed, ephemeral=True)

    @commands.hybrid_group(name="greet", invoke_without_command=True, description="Configure welcome messages")
    @commands.guild_only()
    async def greet(self, ctx: commands.Context):
        from cogs.helpdesk import HelpPagesView

        entries = [
            (f"{settings.prefix}greet edit [message]", "Edit the greeting text or reopen the visual welcome builder."),
            (f"{settings.prefix}greet reset", "Remove the saved welcome configuration."),
            (f"{settings.prefix}greet config", "Show the current welcome settings."),
            (f"{settings.prefix}greet autodelete <seconds>", "Delete welcome messages after the chosen delay. Use 0 to disable."),
            (f"{settings.prefix}greet disable", "Pause welcome messages without clearing the setup."),
            (f"{settings.prefix}greet enable", "Turn welcome messages back on."),
            (f"{settings.prefix}greet setup [channel] [message]", "Open the welcome embed builder and choose a display style."),
            (f"{settings.prefix}greet test", "Post a preview of the saved welcome message."),
            (f"{settings.prefix}greet channel <channel>", "Change the channel used for welcome messages."),
        ]
        view = HelpPagesView(requester_id=ctx.author.id, title="Zyven Greet Group", entries=entries, page=1, page_size=5).set_requester(ctx.author.display_name)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @greet.command(name="setup", description="Set the welcome channel and choose a message style")
    @app_commands.describe(channel="Where welcome messages will be posted", message="Welcome text; supports {user}, {server}, and {count}")
    async def greet_setup(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        *,
        message: str = "Welcome {user} to {server}!",
    ):
        if ctx.author.id not in {ctx.guild.owner_id, settings.owner_id} and not ctx.author.guild_permissions.manage_guild:
            await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure welcome messages.")), allowed_mentions=discord.AllowedMentions.none())
            return
        existing = (await self.bot.db.get_guild(ctx.guild.id)).get("welcome") or {}
        if existing.get("channel"):
            await ctx.send(
                view=component_card(panel(
                    f"{icon('welcome')} Welcome Already Set Up",
                    f"This server already has a welcome channel set to <#{existing['channel']}>. Use `{settings.prefix}greet edit` to update the existing welcome card.",
                )),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(message) > 1000:
            await ctx.send(view=component_card(notice_panel("Welcome messages can be at most 1000 characters.")))
            return
        view = WelcomeSetupStyleView(self.bot, ctx.guild, ctx.author.id, channel, message)
        if channel is not None:
            bot_member = ctx.guild.me
            channel_permissions = channel.permissions_for(bot_member) if bot_member else None
            if channel_permissions is None or not channel_permissions.send_messages:
                await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** in that channel before it can post welcome cards.")))
                return
        view.prompt_message = await ctx.send(view=component_card(view.setup_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    async def _greet_can_manage(self, ctx: commands.Context) -> bool:
        if ctx.author.id in {ctx.guild.owner_id, settings.owner_id} or ctx.author.guild_permissions.manage_guild:
            return True
        await ctx.send(view=component_card(notice_panel("You need **Manage Server** to configure welcome messages.")), allowed_mentions=discord.AllowedMentions.none())
        return False

    @greet.command(name="enable", description="Enable welcome messages")
    async def greet_enable(self, ctx: commands.Context):
        if not await self._greet_can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("welcome", {})
        if not config.get("channel"):
            await ctx.send(view=component_card(notice_panel("Run `greet setup` first and choose a welcome channel.")))
            return
        await self.bot.db.set_nested(ctx.guild.id, "welcome", "enabled", True)
        await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Messages Enabled", f"New members will be welcomed in <#{config['channel']}>.")))

    @greet.command(name="disable", description="Pause welcome messages")
    async def greet_disable(self, ctx: commands.Context):
        if not await self._greet_can_manage(ctx):
            return
        await self.bot.db.set_nested(ctx.guild.id, "welcome", "enabled", False)
        await ctx.send(view=component_card(panel(f"{icon('autorole_settings')} Welcome Messages Paused", "The welcome setup is saved. Use `greet enable` to turn it back on.")))

    @greet.command(name="channel", description="Change the welcome channel")
    async def greet_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await self._greet_can_manage(ctx):
            return
        bot_member = ctx.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not permissions.send_messages:
            await ctx.send(view=component_card(notice_panel("Zyven needs **Send Messages** in that channel.")))
            return
        await self.bot.db.set_nested(ctx.guild.id, "welcome", "channel", channel.id)
        await self.bot.db.set_nested(ctx.guild.id, "welcome", "enabled", True)
        await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Channel Updated", f"Welcome messages will be sent in {channel.mention}.")))

    @greet.command(name="config", description="Show welcome configuration")
    async def greet_config(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("welcome", {})
        if not config.get("channel"):
            await ctx.send(view=component_card(notice_panel("No welcome setup exists. Run `greet setup` to create one.")))
            return
        status = "Enabled" if config.get("enabled", True) else "Paused"
        style = "CV2 clean card" if config.get("style") == "cv2" else "Classic bordered embed"
        delay = int(config.get("autodelete", 0) or 0)
        embed = panel(f"{icon('welcome')} Welcome Configuration", f"**Status:** {status}\n**Channel:** <#{config['channel']}>\n**Style:** {style}\n**Auto-delete:** {delay}s\n\n**Greeting**\n{config.get('message', 'Welcome {user} to {server}!')}")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @greet.command(name="autodelete", description="Set welcome message auto-delete delay")
    async def greet_autodelete(self, ctx: commands.Context, seconds: int):
        if not await self._greet_can_manage(ctx):
            return
        if not 0 <= seconds <= 604800:
            await ctx.send(view=component_card(notice_panel("Choose a delay from 0 to 604800 seconds. Use 0 to keep welcome messages.")))
            return
        await self.bot.db.set_nested(ctx.guild.id, "welcome", "autodelete", seconds)
        result = "Welcome messages will stay in the channel." if seconds == 0 else f"Welcome messages will be deleted after **{seconds} seconds**."
        await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Auto-delete Updated", result)))

    @greet.command(name="edit", description="Edit the welcome text or open the visual builder")
    async def greet_edit(self, ctx: commands.Context, *, message: str | None = None):
        if not await self._greet_can_manage(ctx):
            return
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("welcome", {})
        if message is not None:
            if len(message) > 1000:
                await ctx.send(view=component_card(notice_panel("Welcome messages can be at most 1000 characters.")))
                return
            await self.bot.db.set_nested(ctx.guild.id, "welcome", "message", message)
            await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Text Updated", "The welcome text has been saved.")))
            return
        view = WelcomeSetupStyleView(self.bot, ctx.guild, ctx.author.id, ctx.guild.get_channel(config.get("channel")) if config.get("channel") else None, config.get("message", "Welcome {user} to {server}!"))
        view.config.update({key: config[key] for key in view.config if key in config})
        if view.config.get("footer") == "Member #{count}":
            view.config["footer"] = ""
            view.config["timestamp"] = False
        view.config["color"] = str(view.config.get("color", "5865F2")).removeprefix("#")
        view.prompt_message = await ctx.send(view=component_card(view.setup_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @greet.command(name="reset", description="Remove all welcome settings")
    async def greet_reset(self, ctx: commands.Context):
        if not await self._greet_can_manage(ctx):
            return
        await self.bot.db.unset_values(ctx.guild.id, ["welcome"])
        await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Setup Reset", "The welcome configuration was removed.")))

    @greet.command(name="test", description="Preview the saved welcome message")
    async def greet_test(self, ctx: commands.Context):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("welcome", {})
        channel = ctx.guild.get_channel(config.get("channel")) if config.get("channel") else None
        if channel is None:
            await ctx.send(view=component_card(notice_panel("Set a welcome channel with `greet setup` first.")))
            return
        try:
            await _send_welcome(channel, ctx.author, config)
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not post the preview. Check channel permissions.")))
            return
        await ctx.send(view=component_card(panel(f"{icon('autorole_success')} Welcome Preview Sent", f"Check {channel.mention} to see the welcome preview.")))

    @app_commands.command(name="setautorole", description="Set the role given to new members")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_roles=True)
    async def setautorole(self, interaction: discord.Interaction, role: discord.Role):
        risky = dangerous_permissions(role)
        if role.is_default() or role.managed or risky:
            detail = ", ".join(name.replace("_", " ") for name in risky)
            message = f"This role has restricted permissions: {detail}." if risky else "Choose a normal, assignable server role."
            await send_card(interaction, notice_panel(message), ephemeral=True)
            return
        if interaction.guild.me is None or role >= interaction.guild.me.top_role:
            await send_card(interaction, notice_panel("Move the bot role above the autorole first."), ephemeral=True)
            return
        if interaction.user.id not in {interaction.guild.owner_id, self.bot.owner_id} and role >= interaction.user.top_role:
            await send_card(interaction, notice_panel("You can only configure an autorole below your highest role."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_nested(interaction.guild_id, "autorole", "role", role.id)
        await send_card(interaction, panel(f"{icon('role')} Autorole Saved", f"New members will receive {role.mention} when they join."), ephemeral=True)

    def _autorole_role_check(self, ctx, role):
        bot_member = ctx.guild.me
        risky = dangerous_permissions(role)
        if role.is_default() or role.managed or risky:
            if risky:
                return "This role has restricted permissions: " + ", ".join(name.replace("_", " ") for name in risky) + "."
            return "Choose a normal, assignable autorole."
        if bot_member is None or role >= bot_member.top_role:
            return "Choose a normal role below the bot's highest role."
        if ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id} and role >= ctx.author.top_role:
            return "You can only configure a role below your highest role."
        return None

    async def _autorole_add_remove(self, ctx, member_type, role, add):
        error = self._autorole_role_check(ctx, role)
        if error:
            await ctx.send(view=component_card(panel(f"{icon('autorole_error')} Autorole Could Not Be Updated", f"{icon('autorole_arrow')} {error}")))
            return
        key = "bot_roles" if member_type == "bots" else "human_roles"
        path = f"autorole.{key}"
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("autorole", {})
        configured_ids = _autorole_ids(config, member_type)
        if add:
            if role.id not in configured_ids and len(configured_ids) >= 10:
                await ctx.send(view=component_card(panel(
                    f"{icon('autorole_error')} Autorole Limit Reached",
                    f"{icon('autorole_arrow')} A maximum of 10 {member_type} autoroles can be configured.",
                )))
                return
            await self.bot.db.add_to_set(ctx.guild.id, path, role.id)
            message = (
                f"{icon('autorole_settings')} **__Role:__** {role.mention} role has been set successfully as {member_type.title()} autorole\n\n"
                f"{icon('autorole_arrow')} This role will be automatically assigned to new {member_type[:-1]} members."
            )
            title = f"{icon('autorole_success')} Autorole Added"
        else:
            # Remove legacy and current representations so the role cannot
            # reappear in the config card or still be assigned on member join.
            for alias in (("bot_roles", "bots", "bot") if member_type == "bots" else ("human_roles", "humans", "human")):
                await self.bot.db.pull_value(ctx.guild.id, f"autorole.{alias}", role.id)
            if member_type == "humans" and int(config.get("role", 0) or 0) == role.id:
                await self.bot.db.unset_value(ctx.guild.id, "autorole.role")
            updated = (await self.bot.db.get_guild(ctx.guild.id)).get("autorole", {})
            remaining_count = len(_autorole_ids(updated, member_type))
            message = (
                f"{icon('autorole_settings')} **__Role:__** {role.mention} role has been successfully removed from {member_type.title()} autorole\n"
                f"**Remaining:** `{remaining_count}/10`\n\n"
                f"{icon('autorole_arrow')} This role will no longer be automatically assigned."
            )
            title = f"{icon('autorole_success')} Autorole Removed"
        await ctx.send(view=component_card(panel(title, message)))

    @commands.group(name="autorole", invoke_without_command=True)
    @commands.guild_only()
    async def autorole(self, ctx):
        await self._show_autorole_config(ctx)

    async def _show_autorole_config(self, ctx):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("autorole", {})
        human_roles = [role for role_id in _autorole_ids(config, "humans") if (role := ctx.guild.get_role(role_id))]
        bot_roles = [role for role_id in _autorole_ids(config, "bots") if (role := ctx.guild.get_role(role_id))]
        if not human_roles and not bot_roles:
            embed = panel(
                f"{icon('autorole_error')} Configuration Empty",
                f"{icon('autorole_arrow')} No autorole configuration found in this server.",
            )
        else:
            def role_lines(roles):
                return "\n".join(f"{icon('autorole_dotmoving')} {role.mention}" for role in roles) or "`No roles configured`"

            embed = panel(
                f"Autorole Configuration • {discord.utils.escape_markdown(ctx.guild.name)}",
                f"**__Human Autoroles__**\n{role_lines(human_roles)}\n\n"
                f"**__Bot Autoroles__**\n{role_lines(bot_roles)}\n\n"
                f"**__Total Humans:__** `{len(human_roles)}/10`\n"
                f"**__Total Bots:__** `{len(bot_roles)}/10`",
            )
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)
        await ctx.send(view=component_card(embed))

    @autorole.command(name="config")
    @commands.guild_only()
    async def autorole_config(self, ctx):
        await self._show_autorole_config(ctx)

    @autorole.group(name="bots", invoke_without_command=True)
    @commands.guild_only()
    async def autorole_bots(self, ctx):
        await self._show_autorole_config(ctx)

    @autorole_bots.command(name="add")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_bots_add(self, ctx, role: discord.Role):
        await self._autorole_add_remove(ctx, "bots", role, True)

    @autorole_bots.command(name="remove")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_bots_remove(self, ctx, role: discord.Role):
        await self._autorole_add_remove(ctx, "bots", role, False)

    @autorole.group(name="humans", invoke_without_command=True)
    @commands.guild_only()
    async def autorole_humans(self, ctx):
        await self._show_autorole_config(ctx)

    @autorole_humans.command(name="add")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_humans_add(self, ctx, role: discord.Role):
        await self._autorole_add_remove(ctx, "humans", role, True)

    @autorole_humans.command(name="remove")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_humans_remove(self, ctx, role: discord.Role):
        await self._autorole_add_remove(ctx, "humans", role, False)

    @autorole.group(name="reset", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_reset(self, ctx):
        await self._show_autorole_config(ctx)

    async def _reset_autorole(self, ctx, kind):
        keys = {
            "all": ["autorole.role", "autorole.bot_roles", "autorole.human_roles", "autorole.bot", "autorole.bots", "autorole.human", "autorole.humans"],
            "bots": ["autorole.bot_roles", "autorole.bot", "autorole.bots"],
            "humans": ["autorole.human_roles", "autorole.human", "autorole.humans"],
        }[kind]
        await self.bot.db.unset_values(ctx.guild.id, keys)
        await ctx.send(view=component_card(panel(
            f"{icon('autorole_success')} Autorole Reset",
            f"{icon('autorole_arrow')} Cleared {kind} autorole settings.",
        )))

    @autorole_reset.command(name="all")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_reset_all(self, ctx):
        await self._reset_autorole(ctx, "all")

    @autorole_reset.command(name="bots")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_reset_bots(self, ctx):
        await self._reset_autorole(ctx, "bots")

    @autorole_reset.command(name="humans")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def autorole_reset_humans(self, ctx):
        await self._reset_autorole(ctx, "humans")

    @commands.group(name="vcrole", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def vcrole(self, ctx):
        """Show the VC role command group."""
        help_command = self.bot.get_command("help")
        await ctx.invoke(help_command, query="vcrole")

    async def _show_vcrole_config(self, ctx):
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("vcrole", {})
        role = ctx.guild.get_role(int(config.get("role_id", 0) or 0))
        if role is None:
            embed = panel("VC Role Configuration", "No VC role is configured in this server.")
        else:
            embed = panel(
                "VC Role Configuration",
                f"{icon('autorole_settings')} **__Current Role:__** {role.mention} role is set as a vcrole\n\n"
                f"{icon('arrow_arrow')} This role will be automatically assigned to members in voice channels",
            )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @vcrole.command(name="add", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def vcrole_add(self, ctx, role: discord.Role):
        """Adds a role to the vcrole list"""
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("vcrole", {})
        current = ctx.guild.get_role(int(config.get("role_id", 0) or 0))
        if current is not None:
            embed = panel(
                f"{icon('vcrole_duplicate')} Already Configured",
                f"{icon('autorole_settings')} **__Current Role:__** {current.mention}\n\n"
                f"{icon('arrow_arrow')} Please **remove** the current VC role before adding a new one.",
                color=0xFEE75C,
            )
            return await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
        bot_member = ctx.guild.me
        risky = dangerous_permissions(role)
        if risky:
            return await ctx.send(
                view=component_card(notice_panel("This VC role has restricted permissions: " + ", ".join(name.replace("_", " ") for name in risky) + ".")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if role.is_default() or role.managed or bot_member is None or role >= bot_member.top_role:
            return await ctx.send(
                view=component_card(notice_panel("Choose a normal role below Zyven's highest role.")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.bot.db.set_nested(ctx.guild.id, "vcrole", "role_id", role.id)
        embed = panel(
            f"{icon('approve')} Success",
            f"{icon('autorole_settings')} **__Role:__** {role.mention} role has been set successfully as a vcrole\n\n"
            f"{icon('arrow_arrow')} This role will be automatically assigned to members in voice channels",
            color=0x57F287,
        )
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @vcrole.command(name="remove", case_insensitive=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_roles=True)
    async def vcrole_remove(self, ctx, role: discord.Role):
        """Removes the role from vcrole list"""
        config = (await self.bot.db.get_guild(ctx.guild.id)).get("vcrole", {})
        configured_role = ctx.guild.get_role(int(config.get("role_id", 0) or 0))
        if configured_role is None:
            return await ctx.send(view=component_card(notice_panel("No VC role is configured in this server.")))
        if role.id != configured_role.id:
            return await ctx.send(
                view=component_card(notice_panel(f"The configured VC role is {configured_role.mention}. Remove that role to clear the configuration.")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.bot.db.unset_value(ctx.guild.id, "vcrole.role_id")
        await ctx.send(
            view=component_card(panel(
                f"{icon('approve')} Success",
                f"{icon('autorole_settings')} **__Role:__** {configured_role.mention} has been removed from VC role configuration.",
                color=0x57F287,
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @vcrole.command(name="config", case_insensitive=True)
    @commands.guild_only()
    async def vcrole_config(self, ctx):
        """Shows the Current vcrole in this Guild"""
        await self._show_vcrole_config(ctx)

    @app_commands.command(name="voicemaster", description="Set the VoiceMaster join-to-create voice channel")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_channels=True)
    async def voicemaster(self, interaction: discord.Interaction, trigger: discord.VoiceChannel, category: discord.CategoryChannel | None = None):
        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels or not bot_member.guild_permissions.move_members:
            await send_card(interaction, notice_panel("Zyven needs **Manage Channels** and **Move Members** permissions for VoiceMaster."), ephemeral=True)
            return
        destination_category = category or trigger.category
        if destination_category:
            destination_permissions = destination_category.permissions_for(bot_member)
            can_join_destination = destination_permissions.view_channel and destination_permissions.connect
        else:
            can_join_destination = bot_member.guild_permissions.view_channel and bot_member.guild_permissions.connect
        if not can_join_destination:
            await send_card(interaction, notice_panel("Zyven needs **View Channel** and **Connect** in the VoiceMaster destination category (or server-wide if the room is created outside a category)."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_nested(interaction.guild_id, "voicemaster", "trigger_channel", trigger.id)
        await self.bot.db.set_nested(interaction.guild_id, "voicemaster", "category", category.id if category else None)
        await send_card(interaction, panel(f"{icon('voice')} VoiceMaster Saved", f"Members who join {trigger.mention} will receive their own temporary room."), ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        channel = before.channel
        if channel and channel.id in self.temporary_voice_channels and not any(not occupant.bot for occupant in channel.members):
            try:
                await channel.delete(reason="Empty Zyven temporary voice room")
            except discord.NotFound:
                self.temporary_voice_channels.discard(channel.id)
                await self.bot.db.pull_value(member.guild.id, "voicemaster.temporary_channels", channel.id)
                await self.bot.db.unset_value(member.guild.id, f"voicemaster.temporary_owners.{channel.id}")
            except (discord.Forbidden, discord.HTTPException):
                pass
            else:
                self.temporary_voice_channels.discard(channel.id)
                await self.bot.db.pull_value(member.guild.id, "voicemaster.temporary_channels", channel.id)
                await self.bot.db.unset_value(member.guild.id, f"voicemaster.temporary_owners.{channel.id}")
        doc = None
        vc_role = None
        cfg = None
        creating_room = False
        if before.channel != after.channel:
            doc = await self.bot.db.get_guild(member.guild.id)
            vcrole = doc.get("vcrole", {})
            vc_role = member.guild.get_role(int(vcrole.get("role_id", 0) or 0))
            cfg = doc.get("voicemaster", {})
            creating_room = bool(
                after.channel
                and after.channel.id == cfg.get("trigger_channel")
                and before.channel != after.channel
            )
            # Do not make the voice-role REST call delay room creation or the
            # member move when they enter the join-to-create trigger.
            if vc_role and not creating_room:
                try:
                    if before.channel is None and after.channel is not None and vc_role not in member.roles:
                        await member.add_roles(vc_role, reason="Configured Zyven VC role: joined a voice channel")
                    elif before.channel is not None and after.channel is None and vc_role in member.roles:
                        await member.remove_roles(vc_role, reason="Configured Zyven VC role: left voice channels")
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Could not update VC role for member %s in guild %s", member.id, member.guild.id)
        if not after.channel or before.channel == after.channel:
            return
        doc = doc or await self.bot.db.get_guild(member.guild.id)
        cfg = cfg or doc.get("voicemaster", {})
        if after.channel.id != cfg.get("trigger_channel"):
            return
        category = member.guild.get_channel(cfg.get("category")) if cfg.get("category") else after.channel.category
        # Carry over the category's permission setup, but make sure @everyone
        # is allowed to connect to the newly-created room. A category-level
        # Connect deny otherwise makes the room appear created while nobody
        # else can join it.
        overwrites = dict(category.overwrites) if category else {}
        everyone_overwrite = overwrites.get(member.guild.default_role, discord.PermissionOverwrite())
        everyone_overwrite.connect = True
        overwrites[member.guild.default_role] = everyone_overwrite
        try:
            room = await member.guild.create_voice_channel(
                f"{member.display_name}'s room"[:100],
                category=category,
                overwrites=overwrites,
                reason="Zyven VoiceMaster",
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not create a VoiceMaster room in guild %s", member.guild.id)
            return
        self.temporary_voice_channels.add(room.id)
        try:
            # Move immediately after Discord creates the room. Database writes
            # and the optional VC-role update used to sit in front of this API
            # call and made members wait in the trigger channel.
            await member.move_to(room)
        except (discord.Forbidden, discord.HTTPException):
            try:
                await room.delete(reason="Could not move member into the Zyven temporary room")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            else:
                self.temporary_voice_channels.discard(room.id)
            return
        try:
            await asyncio.gather(
                self.bot.db.add_to_set(member.guild.id, "voicemaster.temporary_channels", room.id),
                self.bot.db.set_nested(member.guild.id, "voicemaster", f"temporary_owners.{room.id}", member.id),
            )
        except Exception:
            # The room and move have already succeeded. Keep it usable and
            # locally tracked rather than disconnecting the member on a DB blip.
            log.exception("Could not persist temporary room %s", room.id)
        if vc_role and vc_role not in member.roles:
            try:
                await member.add_roles(vc_role, reason="Configured Zyven VC role: joined a voice channel")
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not update VC role for member %s in guild %s", member.id, member.guild.id)
        voice_controls = self.bot.get_cog("VoiceControls")
        if voice_controls:
            try:
                await room.send(
                    view=component_card(
                        voice_controls.interface_panel(cfg.get("enabled_buttons")),
                        voice_controls.control_view(cfg.get("enabled_buttons")),
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not post the VoiceMaster control panel in temporary room %s", room.id)


async def setup(bot): await bot.add_cog(Members(bot))
