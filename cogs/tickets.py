from __future__ import annotations
import asyncio
import datetime
import time
import uuid
import discord
from discord import app_commands
from discord.ext import commands

from utils.ticket_store import (
    get_config, set_config, get_ticket, get_user_open_count,
    next_ticket_number, create_ticket, update_ticket, delete_ticket,
)
from utils.ticket_transcript import generate_transcript
from utils.emojis import icon
from core.settings import settings

# ── Custom Emojis (all IDs from source) ───────────────────────
E_SUCCESS = icon("ticket_success")
E_CROSS = icon("ticket_cross")
E_INFO = icon("ticket_info")
E_WARN = icon("ticket_warning")
E_TICKET = icon("ticket_ticket")
E_BUG = icon("ticket_bug")
E_CLAIM = icon("ticket_claim")
E_VIEW = icon("ticket_view")
E_REOPEN = icon("ticket_reopen")
E_QUESTION = icon("ticket_question")
E_DELETE = icon("ticket_delete")
E_SETUP = icon("ticket_setup")
E_CUSTOM = icon("ticket_custom")

# ── Colors ─────────────────────────────────────────────────────
# All container accent bars + backgrounds are pure black, regardless of
# message type (success/error/info/warn all look the same now).
EMBED_COLOR   = 0x000000
SUCCESS_COLOR = 0x000000
ERROR_COLOR   = 0x000000
WARN_COLOR    = 0x000000
INFO_COLOR    = 0x000000

BUTTON_COLOR_MAP = {
    "Green":  discord.ButtonStyle.success,
    "Blue":   discord.ButtonStyle.primary,
    "Grey":   discord.ButtonStyle.secondary,
    "Yellow": discord.ButtonStyle.secondary,
    "Red":    discord.ButtonStyle.danger,
}

# Colour options for the optional Panel Text button — mirrors the map in
# cogs/setup.py. Discord has no native Black/White/Yellow button style,
# so those render as the standard grey button.
PANEL_BUTTON_COLOR_MAP = {
    "Grey":   discord.ButtonStyle.secondary,
    "Yellow": discord.ButtonStyle.secondary,
    "Blue":   discord.ButtonStyle.primary,
    "Red":    discord.ButtonStyle.danger,
    "Black":  discord.ButtonStyle.secondary,
    "White":  discord.ButtonStyle.secondary,
}

DEFAULT_CATEGORIES = ["General Support", "Giveaway Claim", "Bug Report", "Other"]

CATEGORY_EMOJIS = {
    "General Support": E_QUESTION,
    "Giveaway Claim":  E_TICKET,
    "Bug Report":      E_BUG,
    "Other":           E_CUSTOM,
}

CATEGORY_DESCRIPTIONS = {
    "General Support": "Get help with general questions",
    "Giveaway Claim":  "Claim your giveaway prize",
    "Bug Report":      "Report a bug or issue",
    "Other":           "Something not listed above",
}

BRAND      = "Zyven Ticket System"
BRAND_FULL = "Powered by Zyven Development"

# One live ticket setup wizard per guild. Repeated attempts while it is active
# get a short-lived notice and a per-user retry cooldown.
_ACTIVE_TICKET_SETUP: dict[int, dict] = {}
_TICKET_SETUP_RETRY_AFTER: dict[tuple[int, int], float] = {}
_TICKET_SETUP_LOCK = asyncio.Lock()
_TICKET_SETUP_RETRY_SECONDS = 10
_TICKET_SETUP_MESSAGE_TTL = 300


async def _reserve_ticket_setup(guild_id: int, user_id: int, state) -> tuple[bool, int]:
    now = time.monotonic()
    key = (int(guild_id), int(user_id))
    async with _TICKET_SETUP_LOCK:
        active = _ACTIVE_TICKET_SETUP.get(key[0])
        if active:
            retry_until = _TICKET_SETUP_RETRY_AFTER.get(key, 0.0)
            if retry_until > now:
                return False, max(1, int(retry_until - now + 0.99))
            _TICKET_SETUP_RETRY_AFTER[key] = now + _TICKET_SETUP_RETRY_SECONDS
            return False, 0

        token = uuid.uuid4().hex
        state._setup_token = token
        state._setup_guild_id = key[0]
        state._setup_owner_id = key[1]
        _ACTIVE_TICKET_SETUP[key[0]] = {
            "token": token,
            "owner_id": key[1],
            "started_at": now,
        }
        return True, 0


async def _release_ticket_setup(state) -> None:
    guild_id = getattr(state, "_setup_guild_id", None)
    token = getattr(state, "_setup_token", None)
    if guild_id is None or token is None:
        return
    async with _TICKET_SETUP_LOCK:
        active = _ACTIVE_TICKET_SETUP.get(int(guild_id))
        if active and active.get("token") == token:
            _ACTIVE_TICKET_SETUP.pop(int(guild_id), None)
            for key in [key for key in _TICKET_SETUP_RETRY_AFTER if key[0] == int(guild_id)]:
                _TICKET_SETUP_RETRY_AFTER.pop(key, None)


async def _delete_message_later(message, delay: int = 5) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.HTTPException:
        pass


def _schedule_message_delete(message, delay: int = 5) -> None:
    asyncio.create_task(_delete_message_later(message, delay))


async def _delete_ephemeral_setup_notice_later(interaction, delay: int = 5) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


def _schedule_ephemeral_setup_notice_delete(interaction) -> None:
    asyncio.create_task(_delete_ephemeral_setup_notice_later(interaction, 5))


def _setup_busy_text(retry_after: int = 0) -> str:
    if retry_after:
        return f"Ticket setup is busy. Try again in **{retry_after}s**."
    return (
        "A ticket setup is already running in this server. Finish it or click **Delete Setup** "
        "before starting another. Repeated setup attempts have a **10-second cooldown**."
    )


async def _load_setup_targets(state: "SetupState", guild: discord.Guild, config: dict) -> None:
    """Load the currently saved destinations so the setup wizard is editable."""
    role_id = config.get("support_role_id", config.get("staff_role_id"))
    panel_id = config.get("panel_channel_id")
    category_id = config.get("ticket_category_id", config.get("category_id"))
    log_id = config.get("log_channel_id")
    state._saved_target_ids = {
        "support_role_id": role_id,
        "panel_channel_id": panel_id,
        "ticket_category_id": category_id,
        "log_channel_id": log_id,
    }
    state.support_role = guild.get_role(int(role_id)) if role_id else None

    async def resolve_channel(channel_id):
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await guild.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            return None

    state.panel_channel = await resolve_channel(panel_id)
    state.ticket_category = await resolve_channel(category_id)
    state.log_channel = await resolve_channel(log_id)


def _target_label(target, target_id=None) -> str:
    if target is not None:
        return getattr(target, "mention", f"**{target.name}**")
    return f"Not found (`{target_id}`)" if target_id else "Not set"


# ── Helpers ────────────────────────────────────────────────────

def ticket_num(n: int) -> str:
    return f"#{n:04d}"

async def _ticket_manage_allowed(interaction, ticket: dict) -> bool:
    """Allow ticket owner, support staff, server owner, or administrators."""
    guild = interaction.guild
    user = interaction.user
    if guild is None or user is None:
        return False
    try:
        ticket_guild_id = int(ticket.get("guild_id"))
    except (TypeError, ValueError):
        return False
    if ticket_guild_id != guild.id:
        return False
    channel = getattr(interaction, "channel", None)
    channel_guild = getattr(channel, "guild", None)
    if channel_guild is not None and channel_guild.id != guild.id:
        return False
    if user.id == guild.owner_id or getattr(user.guild_permissions, "administrator", False):
        return True
    try:
        if int(ticket.get("author_id", 0)) == user.id:
            return True
    except (TypeError, ValueError):
        return False
    config = await get_config(interaction.client.db, guild.id, str(ticket.get("setup_id", "1"))) or {}
    role_id = config.get("support_role_id")
    try:
        return bool(role_id and any(role.id == int(role_id) for role in getattr(user, "roles", [])))
    except (TypeError, ValueError):
        return False

def _setup_user_allowed(interaction, state) -> bool:
    return bool(
        interaction.guild_id == getattr(state, "_setup_guild_id", None)
        and interaction.user.id == getattr(state, "_setup_owner_id", None)
        and (getattr(interaction.user.guild_permissions, "administrator", False) or interaction.user.id == interaction.guild.owner_id)
    )

async def _reject_setup_interaction(interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.send_message(view=_err("Only the administrator who started this setup can use these controls."), ephemeral=True)


def _style(color: str) -> discord.ButtonStyle:
    return BUTTON_COLOR_MAP.get(color, discord.ButtonStyle.primary)


def _category_emoji(category: str, custom_emojis: dict[str, str] | None = None):
    """Return a parsed custom/category emoji or the built-in fallback."""
    value = (custom_emojis or {}).get(category)
    if value:
        return discord.PartialEmoji.from_str(str(value))
    return CATEGORY_EMOJIS.get(category, E_QUESTION)


class MessageLayout(discord.ui.LayoutView):
    """Generic single-container layout for simple text replies."""
    def __init__(self, text: str, color: int, *, extra_rows: list[discord.ui.ActionRow] | None = None):
        super().__init__()
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)
        for row in (extra_rows or []):
            self.add_item(row)


def _err(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_CROSS} {msg}", ERROR_COLOR)

def _warn_embed(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_WARN} {msg}", WARN_COLOR)

def _ok(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_SUCCESS} {msg}", SUCCESS_COLOR)

def _ok_raw(text: str) -> MessageLayout:
    """Like _ok but takes pre-formatted text (e.g. multi-line, with its own heading)."""
    return MessageLayout(text, SUCCESS_COLOR)


class LogLayout(discord.ui.LayoutView):
    """Replaces the old log embed — a container with a heading, field-style
    lines, and a timestamp footer line (Components V2 has no embed footer)."""
    def __init__(self, title: str, color: int):
        super().__init__()
        self._container = discord.ui.Container(accent_color=color)
        self._lines: list[str] = [f"## {title}"]
        self._thumbnail: discord.ui.Section | None = None
        self._rendered = False

    def add_field(self, name: str, value: str, inline: bool = False):
        self._lines.append(f"**{name}:** {value}")

    def set_footer(self, text: str):
        """Compatibility shim — old code used embed footers for extra detail
        like a channel ID. Components V2 containers have no footer, so we
        fold this onto its own line instead of dropping it."""
        self._lines.append(f"-# {text}")

    def set_thumbnail(self, url: str):
        self._thumb_url = url

    def _render(self):
        if self._rendered:
            return
        body = "\n".join(self._lines)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
        body += f"\n\n*{ts}*"
        thumb_url = getattr(self, "_thumb_url", None)
        if thumb_url:
            section = discord.ui.Section(
                discord.ui.TextDisplay(body),
                accessory=discord.ui.Thumbnail(thumb_url),
            )
            self._container.add_item(section)
        else:
            self._container.add_item(discord.ui.TextDisplay(body))
        self.add_item(self._container)
        self._rendered = True

    def to_send_kwargs(self) -> dict:
        self._render()
        return {"view": self}


def _log_embed(title: str, color: int) -> LogLayout:
    return LogLayout(title, color)

async def send_log(guild: discord.Guild, config: dict, log: LogLayout, file: discord.File | None = None):
    log_id = config.get("log_channel_id")
    if not log_id:
        return
    ch = guild.get_channel(int(log_id))
    if not ch:
        return
    try:
        # Components V2 LayoutView cannot carry file= in the same send() call.
        # Send the CV2 log layout first, then send the transcript file as a
        # separate CV2 container (black accent) followed by the raw file.
        await ch.send(**log.to_send_kwargs())
        if file:
            transcript_note = LogLayout("Ticket Transcript", 0x000000)
            transcript_note.add_field(
                name="Transcript",
                value="The HTML transcript for this ticket is attached below.",
                inline=False,
            )
            await ch.send(**transcript_note.to_send_kwargs())
            await ch.send(file=file)
    except Exception:
        pass


def _rows_from_view(view: discord.ui.View) -> list[discord.ui.ActionRow]:
    """Folds a legacy discord.ui.View's buttons/select into ActionRows so
    they can be attached to a Components V2 LayoutView."""
    rows: list[discord.ui.ActionRow] = []
    row = discord.ui.ActionRow()
    count = 0
    for item in view.children:
        if isinstance(item, discord.ui.Select) or count >= 5:
            if count:
                rows.append(row)
            row = discord.ui.ActionRow()
            count = 0
        row.add_item(item)
        count += 1
        if isinstance(item, discord.ui.Select):
            rows.append(row)
            row = discord.ui.ActionRow()
            count = 0
    if count:
        rows.append(row)
    return rows


class PanelExtraButton(discord.ui.Button):
    """The optional 'Panel Button' shown under the panel text when it has
    no URL (i.e. it shows a popup instead of linking out). Looks up its
    text from the guild's saved config at click-time rather than capturing
    it in a closure, so it keeps working after a bot restart."""

    def __init__(self, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary, emoji: str = "", setup_id: str = "1"):
        self.setup_id = str(setup_id)
        super().__init__(
            label=label or "More Info",
            style=style,
            emoji=emoji or None,
            custom_id=f"panel{'' if self.setup_id == '1' else self.setup_id}:extra_btn",
        )

    async def callback(self, interaction: discord.Interaction):
        config = await get_config(interaction.client.db, interaction.guild.id, self.setup_id) or {}
        text = config.get("panel_button_text") or "No additional information set."
        await interaction.response.send_message(
            view=MessageLayout(text, EMBED_COLOR), ephemeral=True
        )


class PanelExtraButtonRegistration(discord.ui.LayoutView):
    """Empty-shell layout used only to re-register PanelExtraButton's
    custom_id for persistence on bot restart — never actually sent."""
    def __init__(self):
        super().__init__(timeout=None)
        row = discord.ui.ActionRow()
        row.add_item(PanelExtraButton(setup_id="1"))
        row.add_item(PanelExtraButton(setup_id="2"))
        self.add_item(row)


def build_panel_layout(
    *,
    title: str,
    description: str,
    color: int,
    author_name: str = "",
    author_icon_url: str = "",
    thumbnail_url: str = "",
    image_url: str = "",
    action_view: discord.ui.View,
    panel_button_label: str = "",
    panel_button_url: str = "",
    panel_button_text: str = "",
    panel_button_style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    panel_button_emoji: str = "",
    setup_id: str = "1",
) -> discord.ui.LayoutView:
    """Builds the main ticket panel (button panel or dropdown panel) as a
    Components V2 layout, replacing the old embed-based panel builder.
    `action_view` supplies the buttons/select (TicketPanelView or
    TicketSelectPanel) which get folded into this layout's action rows.

    `panel_button_*` describes an entirely optional extra button shown
    right under the panel's main text (e.g. a "Rules" link or a "FAQ"
    popup), separate from the ticket-opening controls in `action_view`.
    If `panel_button_label` is empty, no button is added at all.
    - If `panel_button_url` is set, it's a link button to that URL.
    - Otherwise, if `panel_button_text` is set, clicking shows that text
      as an ephemeral popup.
    """

    class PanelLayout(discord.ui.LayoutView):
        def __init__(self):
            super().__init__(timeout=None)
            container = discord.ui.Container(accent_color=color)

            heading = f"## {title}" if title else ""
            body_text = "\n".join(t for t in (heading, description) if t)

            if author_name:
                section = discord.ui.Section(
                    discord.ui.TextDisplay(body_text or "​"),
                    accessory=discord.ui.Thumbnail(author_icon_url) if author_icon_url else discord.ui.Thumbnail("https://cdn.discordapp.com/embed/avatars/0.png"),
                )
                container.add_item(section)
            elif thumbnail_url:
                section = discord.ui.Section(
                    discord.ui.TextDisplay(body_text or "​"),
                    accessory=discord.ui.Thumbnail(thumbnail_url),
                )
                container.add_item(section)
            else:
                container.add_item(discord.ui.TextDisplay(body_text))

            # Entirely optional extra button under the panel text — only
            # appears at all if the admin configured a label for it.
            if panel_button_label:
                btn_row = discord.ui.ActionRow()
                if panel_button_url:
                    btn_row.add_item(discord.ui.Button(
                        label=panel_button_label,
                        style=discord.ButtonStyle.link,
                        url=panel_button_url,
                        emoji=panel_button_emoji or None,
                    ))
                else:
                    btn_row.add_item(PanelExtraButton(
                        label=panel_button_label,
                        style=panel_button_style,
                        emoji=panel_button_emoji,
                        setup_id=setup_id,
                    ))
                container.add_item(btn_row)

            if image_url:
                container.add_item(discord.ui.Separator())
                container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)))

            # Fold the action view's buttons/select into rows inside the container
            for row in _rows_from_view(action_view):
                container.add_item(row)

            self.add_item(container)

    return PanelLayout()


# ── Multi-Step Form Engine ─────────────────────────────────────

def _modal_field(label: str, **kwargs):
    """Create a modern Discord UI label containing a text input."""
    return discord.ui.Label(text=label[:45], component=discord.ui.TextInput(**kwargs))


class StepModal(discord.ui.Modal):
    def __init__(self, title: str, fields: list[dict], category: str,
                 step_num: int, total: int, collected: dict, on_done):
        super().__init__(title=f"{category} — Step {step_num}/{total}" if total > 1 else title)
        self.category  = category
        self.step_num  = step_num
        self.total     = total
        self.collected = collected
        self.on_done   = on_done
        self._inputs   = []

        for f in fields:
            field_label = f["label"][:45]
            inp = discord.ui.TextInput(
                placeholder=f.get("placeholder", "")[:100],
                required=f.get("required", True),
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short,
                max_length=f.get("max_length", 500),
            )
            self.add_item(discord.ui.Label(text=field_label, component=inp))
            self._inputs.append((field_label, inp))

    async def on_submit(self, interaction: discord.Interaction):
        for field_label, inp in self._inputs:
            self.collected[field_label] = inp.value
        await self.on_done(interaction, self.collected)


def _get_form_steps(config: dict, category: str) -> list[dict]:
    forms = config.get("form_steps", {})
    cat_steps = forms.get(category)
    if not cat_steps:
        return [{"title": "Ticket Details", "fields": [
            {"label": "Reason", "placeholder": "Describe your issue or request…",
             "required": False, "long": True, "max_length": 1000}
        ]}]
    return cat_steps


async def start_form(interaction: discord.Interaction, category: str, setup_id: str = "1"):
    refresh_ticket_emojis()
    config = await get_config(interaction.client.db, interaction.guild.id, setup_id)
    if not config:
        return await interaction.response.send_message(
            view=_err("Ticket system not configured. Ask an admin to run `/setup`."),
            ephemeral=True,
        )

    steps     = _get_form_steps(config, category)
    collected: dict = {}

    async def after_step(inter: discord.Interaction, answers: dict, step_index: int):
        if step_index + 1 < len(steps):
            nxt = steps[step_index + 1]
            modal = StepModal(
                nxt["title"], nxt["fields"], category,
                step_index + 2, len(steps), answers,
                lambda i, a: after_step(i, a, step_index + 1),
            )
            await inter.response.send_modal(modal)
        else:
            reason = "\n".join(f"**{k}:** {v}" for k, v in answers.items() if v)
            await open_ticket(inter, category, reason or "No reason provided.", answers, setup_id=setup_id)

    first = steps[0]
    modal = StepModal(
        first["title"], first["fields"], category,
        1, len(steps), collected,
        lambda i, a: after_step(i, a, 0),
    )
    await interaction.response.send_modal(modal)


# ── Ticket Control Buttons ─────────────────────────────────────

class TicketControlView(discord.ui.View):
    def __init__(self):
        refresh_ticket_emojis()
        super().__init__(timeout=None)
        emoji_by_id = {
            "ticket:claim": "ticket_claim", "ticket:close": "ticket_delete",
            "ticket:delete": "ticket_delete", "ticket:reopen": "ticket_reopen",
        }
        for child in self.children:
            emoji_name = emoji_by_id.get(getattr(child, "custom_id", ""))
            if emoji_name:
                child.emoji = discord.PartialEmoji.from_str(icon(emoji_name))

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary,
                       custom_id="ticket:claim", emoji=E_CLAIM)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("Ticket data not found."), ephemeral=True)

        config       = await get_config(interaction.client.db, interaction.guild.id, str(ticket.get("setup_id", "1")))
        support_role = interaction.guild.get_role(config["support_role_id"]) if config else None

        if support_role and support_role not in interaction.user.roles:
            return await interaction.response.send_message(
                view=_err("Only support staff can claim tickets."), ephemeral=True
            )

        if ticket.get("claimed_by"):
            claimer = interaction.guild.get_member(int(ticket["claimed_by"]))
            name    = claimer.display_name if claimer else "Unknown"
            return await interaction.response.send_message(
                view=_warn_embed(f"Already claimed by **{name}**."), ephemeral=True
            )

        await update_ticket(interaction.client.db, interaction.channel.id, {"claimed_by": str(interaction.user.id)})

        await interaction.response.send_message(
            view=_ok(f"{E_CLAIM} **{interaction.user.display_name}** has claimed this ticket.")
        )

        try:
            if not interaction.channel.name.startswith("claimed-"):
                await interaction.channel.edit(name=f"claimed-{interaction.channel.name}")
        except Exception:
            pass

        if config:
            log = _log_embed(f"{E_CLAIM} Ticket Claimed", INFO_COLOR)
            log.add_field(name="Claimed By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            log.add_field(name="Channel",    value=interaction.channel.mention, inline=True)
            log.add_field(name="Ticket",     value=ticket_num(ticket.get("number", 0)), inline=True)
            await send_log(interaction.guild, config, log)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger,
                       custom_id="ticket:close", emoji=E_DELETE)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)


# ── Closed Ticket Buttons ──────────────────────────────────────

class ClosedTicketView(discord.ui.View):
    def __init__(self):
        refresh_ticket_emojis()
        super().__init__(timeout=None)
        emoji_by_id = {"ticket:delete": "ticket_delete", "ticket:reopen": "ticket_reopen"}
        for child in self.children:
            emoji_name = emoji_by_id.get(getattr(child, "custom_id", ""))
            if emoji_name:
                child.emoji = discord.PartialEmoji.from_str(icon(emoji_name))

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger,
                       custom_id="ticket:delete", emoji=E_DELETE)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("Ticket data not found."), ephemeral=True)
        if not await _ticket_manage_allowed(interaction, ticket):
            return await interaction.response.send_message(
                view=_err("Only the ticket owner, support staff, or a server administrator can delete tickets."), ephemeral=True
            )
        config = await get_config(interaction.client.db, interaction.guild.id, str(ticket.get("setup_id", "1")))

        await interaction.response.send_message(view=_warn_embed("Deleting ticket in **3 seconds…**"))
        transcript_file = await generate_transcript(interaction.channel)

        if config and ticket:
            number    = ticket.get("number", 0)
            author_id = ticket.get("author_id")
            author    = interaction.guild.get_member(int(author_id)) if author_id else None
            category  = ticket.get("category", "General Support")

            log = _log_embed(f"{E_DELETE} Ticket Deleted", ERROR_COLOR)
            log.add_field(name="Author",     value=f"{author.mention if author else 'Unknown'} (`{author_id}`)", inline=False)
            log.add_field(name="Deleted By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            log.add_field(name="Category",   value=category, inline=True)
            log.add_field(name="Ticket",     value=ticket_num(number), inline=True)
            log.set_footer(f"ID: {interaction.channel.id}")
            await send_log(interaction.guild, config, log, transcript_file)

            if author:
                try:
                    t2 = await generate_transcript(interaction.channel)
                    # Send a CV2 layout message first (black accent), then the
                    # transcript file as a separate message — attaching both in
                    # one call causes a glitch with Components V2.
                    dm_msg = LogLayout(f"Ticket Transcript — {ticket_num(number)}", 0x000000)
                    dm_msg.add_field(
                        name="Server",
                        value=interaction.guild.name,
                        inline=True,
                    )
                    dm_msg.add_field(
                        name="Category",
                        value=category,
                        inline=True,
                    )
                    dm_msg.add_field(
                        name="Status",
                        value=(
                            f"Your **{category}** ticket {ticket_num(number)} has been closed. "
                            f"The transcript is attached below."
                        ),
                        inline=False,
                    )
                    dm_msg.set_footer("Powered by Zyven Development")
                    await author.send(**dm_msg.to_send_kwargs())
                    await author.send(file=t2)
                except Exception:
                    pass

        await delete_ticket(interaction.client.db, interaction.channel.id)
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except Exception:
            pass

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success,
                       custom_id="ticket:reopen", emoji=E_REOPEN)
    async def reopen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("Ticket data not found."), ephemeral=True)
        if not await _ticket_manage_allowed(interaction, ticket):
            return await interaction.response.send_message(
                view=_err("Only the ticket owner, support staff, or a server administrator can reopen tickets."), ephemeral=True
            )
        config = await get_config(interaction.client.db, interaction.guild.id, str(ticket.get("setup_id", "1")))

        author = interaction.guild.get_member(int(ticket["author_id"]))
        if author:
            await interaction.channel.set_permissions(author, read_messages=True, send_messages=True)

        try:
            name = interaction.channel.name.removeprefix("claimed-")
            await interaction.channel.edit(name=name)
        except Exception:
            pass

        await update_ticket(interaction.client.db, interaction.channel.id, {"status": "open", "claimed_by": None})

        await interaction.response.send_message(
            view=_ok(f"{E_REOPEN} Ticket reopened by {interaction.user.mention}.")
        )
        await interaction.message.edit(view=TicketControlView())

        if config:
            log = _log_embed(f"{E_REOPEN} Ticket Reopened", SUCCESS_COLOR)
            log.add_field(name="Reopened By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            log.add_field(name="Channel",     value=interaction.channel.mention, inline=True)
            log.add_field(name="Ticket",      value=ticket_num(ticket.get("number", 0)), inline=True)
            await send_log(interaction.guild, config, log)


# ── Close Logic ────────────────────────────────────────────────

async def close_ticket(interaction: discord.Interaction):
    ticket = await get_ticket(interaction.client.db, interaction.channel.id)
    if not ticket:
        return await interaction.response.send_message(
            view=_err("This is not a ticket channel."), ephemeral=True
        )
    if not await _ticket_manage_allowed(interaction, ticket):
        return await interaction.response.send_message(
            view=_err("Only the ticket owner, support staff, or a server administrator can close tickets."),
            ephemeral=True,
        )

    config = await get_config(interaction.client.db, interaction.guild.id, str(ticket.get("setup_id", "1")))
    author = interaction.guild.get_member(int(ticket["author_id"]))
    if author:
        await interaction.channel.set_permissions(author, read_messages=False)

    await update_ticket(interaction.client.db, interaction.channel.id, {"status": "closed"})

    close_text = (
        f"## {E_DELETE} Ticket Closed\n"
        f"Closed by {interaction.user.mention}.\n\n"
        f"> {E_DELETE} **Delete** — removes this channel & DMs transcript to user\n"
        f"> {E_REOPEN} **Reopen** — restores access and reopens the ticket"
    )
    await interaction.response.send_message(
        view=MessageLayout(close_text, ERROR_COLOR, extra_rows=_rows_from_view(ClosedTicketView()))
    )

    if config:
        number = ticket.get("number", 0)
        log = _log_embed(f"{E_DELETE} Ticket Closed", WARN_COLOR)
        log.add_field(name="Author",    value=f"{author.mention if author else 'Unknown'} (`{ticket['author_id']}`)", inline=False)
        log.add_field(name="Closed By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        log.add_field(name="Ticket",    value=ticket_num(number), inline=True)
        log.add_field(name="Channel",   value=interaction.channel.mention, inline=True)
        log.set_footer(f"ID: {interaction.channel.id}")
        await send_log(interaction.guild, config, log)


# ── Open Ticket ────────────────────────────────────────────────

async def open_ticket(interaction: discord.Interaction, category: str,
                      reason: str, form_data: dict | None = None, *, setup_id: str = "1"):
    refresh_ticket_emojis()
    config = await get_config(interaction.client.db, interaction.guild.id, setup_id)
    if not config:
        return await interaction.response.send_message(
            view=_err("Ticket system not configured."), ephemeral=True
        )

    limit      = config.get("ticket_limit", 3)
    open_count = await get_user_open_count(interaction.client.db, interaction.guild.id, interaction.user.id, setup_id)
    if open_count >= limit:
        return await interaction.response.send_message(
            view=_warn_embed(
                f"You have **{open_count}/{limit}** open tickets. "
                f"Please close one before opening another."
            ),
            ephemeral=True,
        )

    cat_ch       = interaction.guild.get_channel(int(config["ticket_category_id"]))
    support_role = interaction.guild.get_role(int(config["support_role_id"]))
    number       = await next_ticket_number(interaction.client.db, interaction.guild.id)
    safe_name    = "".join(c for c in interaction.user.name.lower() if c.isalnum())[:12]
    channel_name = f"ticket-{number:04d}-{safe_name}"

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user:               discord.PermissionOverwrite(
                                            read_messages=True, send_messages=True, attach_files=True),
        interaction.guild.me:           discord.PermissionOverwrite(
                                            read_messages=True, send_messages=True, manage_channels=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel = await interaction.guild.create_text_channel(
        name=channel_name,
        category=cat_ch,
        overwrites=overwrites,
        topic=f"{category} • {interaction.user} • Ticket {ticket_num(number)}",
    )

    await create_ticket(interaction.client.db, channel.id, {
        "guild_id":   str(interaction.guild.id),
        "author_id":  str(interaction.user.id),
        "category":   category,
        "reason":     reason,
        "number":     number,
        "channel_id": str(channel.id),
        "claimed_by": None,
        "setup_id": str(setup_id),
    })

    mention = support_role.mention if support_role else ""

    # Components V2 messages can't carry separate `content` alongside the
    # new components, so the ping mentions are folded into the TextDisplay
    # itself — mentions in text displays still notify users/roles.
    body = (
        f"{interaction.user.mention} {mention}\n\n"
        f"## {category} • Ticket {ticket_num(number)}\n"
        f"Welcome {interaction.user.mention}!\n"
        f"A support team member will be with you shortly.\n\n"
        f"**{E_VIEW} Ticket Info**\n"
        f"> **Category:** {category}\n"
        f"> **Status:** Open\n"
        f"> **Ticket:** {ticket_num(number)}"
    )

    if form_data and len(form_data) > 1:
        answers = "\n".join(
            f"> **{k}:** {v}" for k, v in form_data.items() if v
        )
        body += f"\n\n**{E_CUSTOM} Your Answers**\n{answers}"
    elif reason and reason != "No reason provided.":
        body += f"\n\n**{E_CUSTOM} Reason**\n> {reason}"

    container = discord.ui.Container(accent_color=EMBED_COLOR)
    section = discord.ui.Section(
        discord.ui.TextDisplay(body),
        accessory=discord.ui.Thumbnail(interaction.user.display_avatar.url),
    )
    container.add_item(section)

    class _TicketOpenLayout(discord.ui.LayoutView):
        def __init__(self):
            super().__init__(timeout=None)
            for row in _rows_from_view(TicketControlView()):
                container.add_item(row)
            self.add_item(container)

    allowed_ticket_mentions = discord.AllowedMentions(
        users=[interaction.user],
        roles=[support_role] if support_role else [],
        everyone=False,
        replied_user=False,
    )
    await channel.send(view=_TicketOpenLayout(), allowed_mentions=allowed_ticket_mentions)

    await interaction.response.send_message(
        view=_ok(f"Your ticket has been created: {channel.mention}"),
        ephemeral=True,
    )

    if config.get("log_channel_id"):
        log = _log_embed(f"{E_TICKET} Ticket Opened", INFO_COLOR)
        log.add_field(name="Author",   value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        log.add_field(name="Category", value=category, inline=True)
        log.add_field(name="Channel",  value=channel.mention, inline=True)
        log.add_field(name="Ticket",   value=ticket_num(number), inline=True)
        if reason and reason != "No reason provided.":
            log.add_field(name="Reason", value=reason[:500], inline=False)
        log.set_thumbnail(interaction.user.display_avatar.url)
        log.set_footer(f"ID: {channel.id}")
        await send_log(interaction.guild, config, log)


# ── Panel Type 1: Button Panel ─────────────────────────────────

class TicketPanelView(discord.ui.View):
    def __init__(
        self,
        button_label: str = "Open a Ticket",
        button_emoji: str | None = None,
        # Legacy single-colour param kept for backwards-compat
        button_color: str = "Blue",
        # New per-button colours dict: {"main": "Blue", "Bug Report": "Red", ...}
        button_colors: dict | None = None,
        active_categories: list[str] | None = None,
        category_emojis: dict[str, str] | None = None,
        show_main_button: bool = True,
        setup_id: str = "1",
    ):
        refresh_ticket_emojis()
        super().__init__(timeout=None)
        self.setup_id = str(setup_id)
        if active_categories is None:
            active_categories = DEFAULT_CATEGORIES[:]

        # Resolve colour for a given key
        def _color(key: str, fallback: str) -> discord.ButtonStyle:
            if button_colors:
                return _style(button_colors.get(key, fallback))
            return _style(button_color if key == "main" else fallback)

        # Main button always opens first active category (or General
        # Support) — entirely skipped if show_main_button is False, so the
        # category buttons become the only way to open a ticket.
        if show_main_button:
            first_cat = active_categories[0] if active_categories else "General Support"
            main_btn = discord.ui.Button(
                label=button_label,
                style=_color("main", "Blue"),
                custom_id=f"panel{'' if self.setup_id == '1' else self.setup_id}:open",
                emoji=button_emoji or E_TICKET,
                row=0,
            )
            main_btn.callback = self._cb(first_cat)
            self.add_item(main_btn)

        # Default colour fallbacks per built-in category
        _default_colors = {
            "General Support": "Grey",
            "Giveaway Claim":  "Green",
            "Bug Report":      "Red",
            "Other":           "Grey",
        }

        # Up to 4 category buttons on row 1 (Discord limit: 5 per row)
        for cat in active_categories[:4]:
            fallback = _default_colors.get(cat, "Grey")
            btn = discord.ui.Button(
                label=cat,
                style=_color(cat, fallback),
                custom_id=f"panel{'' if self.setup_id == '1' else self.setup_id}:{cat.lower().replace(' ', '_')[:80]}",
                emoji=_category_emoji(cat, category_emojis),
                row=1,
            )
            btn.callback = self._cb(cat)
            self.add_item(btn)

        # Safety net: if both the main button and every category were
        # somehow removed, fall back to a single generic open button so
        # the panel is never left with nothing clickable on it.
        if not self.children:
            fallback_btn = discord.ui.Button(
                label=button_label,
                style=_color("main", "Blue"),
                custom_id=f"panel{'' if self.setup_id == '1' else self.setup_id}:open",
                emoji=button_emoji or E_TICKET,
                row=0,
            )
            fallback_btn.callback = self._cb("General Support")
            self.add_item(fallback_btn)

    def _cb(self, category: str):
        async def callback(interaction: discord.Interaction):
            await start_form(interaction, category, self.setup_id)
        return callback


# ── Panel Type 2: Dropdown Select Panel ───────────────────────

class TicketSelectPanel(discord.ui.View):
    def __init__(self, active_categories: list[str] | None = None, category_emojis: dict[str, str] | None = None, setup_id: str = "2"):
        refresh_ticket_emojis()
        super().__init__(timeout=None)
        self.setup_id = str(setup_id)
        cats = active_categories or DEFAULT_CATEGORIES[:]
        options = [
            discord.SelectOption(
                label=cat,
                description=CATEGORY_DESCRIPTIONS.get(cat, "Open a ticket"),
                emoji=_category_emoji(cat, category_emojis),
                value=cat,
            )
            for cat in cats[:10]
        ]
        select = discord.ui.Select(
            placeholder="Select a ticket category…",
            options=options,
            custom_id="panel2:select",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        category = interaction.data["values"][0]
        await start_form(interaction, category, self.setup_id)


# ── Help Menu ──────────────────────────────────────────────────

HELP_PAGES = {
    "root": {
        "title": f"{E_INFO} Zyven Ticket System — Help",
        "body": (
            f"Welcome to **Zyven Ticket System**!\n"
            f"Select a topic from the menu below.\n\n"
            f"> {E_TICKET} **Getting Started** — how to open tickets\n"
            f"> {E_CLAIM} **Staff Commands** — manage tickets\n"
            f"> {E_SETUP} **Admin Commands** — configure the system\n"
            f"> {E_CUSTOM} **Multi-Step Forms** — custom ticket forms\n"
            f"> {E_VIEW} **Panel Types** — buttons vs dropdown\n"
            f"> {E_INFO} **Features** — available ticket tools"
        ),
    },
    "start": {
        "title": f"{E_TICKET} Getting Started",
        "body": (
            f"**Opening a Ticket**\n"
            f"> Go to the ticket panel channel.\n"
            f"> Click a category button **or** choose from the dropdown panel.\n"
            f"> Fill in the form that appears and submit.\n"
            f"> A private channel will be created just for you.\n\n"
            f"**Inside Your Ticket**\n"
            f"> {E_CLAIM} **Claim** — a staff member takes ownership\n"
            f"> {E_DELETE} **Close** — closes and locks the channel\n\n"
            f"**Ticket Limit**\n"
            f"> You can have up to 3 open tickets at once (set by admins)."
        ),
    },
    "staff": {
        "title": f"{E_CLAIM} Staff Commands",
        "body": (
            f"`{{prefix}}ticket close` / `/close` — Close the current ticket\n"
            f"`{{prefix}}ticket add @user` / `/add` — Add a member\n"
            f"`{{prefix}}ticket remove @user` / `/remove` — Remove a member\n"
            f"`{{prefix}}ticket transcript` / `/transcript` — Generate an HTML transcript\n"
            f"`{{prefix}}ticket ticketinfo` / `/ticketinfo` — View ticket details\n\n"
            f"**Closed Ticket Buttons**\n"
            f"> {E_DELETE} **Delete** — deletes channel & DMs transcript\n"
            f"> {E_REOPEN} **Reopen** — restores access and reopens"
        ),
    },
    "admin": {
        "title": f"{E_SETUP} Admin Commands",
        "body": (
            f"`{{prefix}}ticket setup` / `/setup` — Button panel setup *(Administrator only)*\n"
            f"`{{prefix}}ticket setup2` / `/setup2` — Dropdown panel setup\n"
            f"`{{prefix}}ticket panel2` / `/panel2` — Repost dropdown panel\n"
            f"`{{prefix}}ticket setform <category>` / `/setform` — Configure ticket forms\n\n"
            f"**What You Can Configure**\n"
            f"> Support role, panel channel, ticket category, log channel\n"
            f"> Panel title, description, author name & icon\n"
            f"> Footer text & icon, thumbnail, banner image\n"
            f"> Button label, emoji & colour\n"
            f"> Remove unused category buttons from the panel"
        ),
    },
    "forms": {
        "title": f"{E_CUSTOM} Multi-Step Forms",
        "body": (
            f"Each category supports **1–4 custom form steps**.\n\n"
            f"**Default** — 1 step, asks for a reason.\n\n"
            f"**Custom via `{{prefix}}ticket setform` or `/setform`**\n"
            f"> Set a title for each step\n"
            f"> Add up to 4 fields per step\n"
            f"> Answers appear formatted in the ticket channel\n\n"
            f"**Example — Bug Report:**\n"
            f"> Step 1: Bug title + description\n"
            f"> Step 2: Steps to reproduce\n"
            f"> Step 3: Expected vs actual behaviour"
        ),
    },
    "panels": {
        "title": f"{E_VIEW} Panel Types",
        "body": (
            f"**Panel 1 — Button Panel** *(sent by `{{prefix}}ticket setup` or `/setup`)*\n"
            f"> Main open button + category buttons\n"
            f"> Fast, one click to open a ticket\n\n"
            f"**Panel 2 — Dropdown Panel** *(sent by `{{prefix}}ticket panel2` or `/panel2`)*\n"
            f"> Users select from a dropdown menu\n"
            f"> Shows category name, description & emoji\n"
            f"> Supports up to 10 categories\n"
            f"> Displays the animated ticket banner GIF\n\n"
            f"Both panels support multi-step forms."
        ),
    },
    "about": {
        "title": f"{E_INFO} Ticket Features",
        "body": (
            f"The ticket system keeps support conversations private and organized.\n\n"
            f"**Features**\n"
            f"> Two panel styles — buttons & dropdown\n"
            f"> Multi-step customisable ticket forms\n"
            f"> Claim, close, reopen & delete workflow\n"
            f"> HTML transcripts DMed to user on delete\n"
            f"> Full log channel with detailed embeds\n"
            f"> Per-user ticket limits\n"
            f"> Persistent buttons across bot restarts\n"
            f"> Ticket data is stored persistently\n\n"
            f"Powered by Zyven Development"
        ),
    },
}


class HelpSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a help topic…",
            custom_id="help:select",
            options=[
                discord.SelectOption(label="Getting Started",  value="start",  emoji=E_TICKET,   description="How to open and use tickets"),
                discord.SelectOption(label="Staff Commands",   value="staff",  emoji=E_CLAIM,    description="Manage tickets as staff"),
                discord.SelectOption(label="Admin Commands",   value="admin",  emoji=E_SETUP,    description="Setup and configuration"),
                discord.SelectOption(label="Multi-Step Forms", value="forms",  emoji=E_CUSTOM,   description="Custom ticket forms"),
                discord.SelectOption(label="Panel Types",      value="panels", emoji=E_VIEW,     description="Buttons vs dropdown panel"),
                discord.SelectOption(label="About",            value="about",  emoji=E_INFO,     description="Features and credits"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        page = HELP_PAGES.get(self.values[0], HELP_PAGES["root"])
        await interaction.response.edit_message(view=HelpView(page_key=self.values[0]))


class HelpView(discord.ui.LayoutView):
    def __init__(self, page_key: str = "root"):
        super().__init__(timeout=180)
        page = HELP_PAGES.get(page_key, HELP_PAGES["root"])
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        body = page["body"].replace("{prefix}", settings.prefix)
        container.add_item(discord.ui.TextDisplay(f"## {page['title']}\n{body}"))
        self.add_item(container)
        row = discord.ui.ActionRow()
        row.add_item(HelpSelect())
        self.add_item(row)


# ── Form Builder ───────────────────────────────────────────────

class AddStepModal(discord.ui.Modal, title="Add Form Step"):
    step_title = _modal_field("Step Title", placeholder="e.g. Bug Details", max_length=80)
    field1     = _modal_field("Field 1 Label", placeholder="e.g. Describe the bug", max_length=80)
    field2     = _modal_field("Field 2 Label (optional)", placeholder="e.g. Steps to reproduce", required=False, max_length=80)
    field3     = _modal_field("Field 3 Label (optional)", placeholder="e.g. Expected behaviour", required=False, max_length=80)
    field4     = _modal_field("Field 4 Label (optional)", placeholder="e.g. Screenshot URL", required=False, max_length=80)

    def __init__(self, category: str, steps: list, view: "FormBuilderView"):
        super().__init__()
        self.category = category
        self.steps    = steps
        self.fbview   = view

    async def on_submit(self, interaction: discord.Interaction):
        fields = [
            {"label": f.component.value, "placeholder": "", "required": f is self.field1, "long": True, "max_length": 1000}
            for f in [self.field1, self.field2, self.field3, self.field4] if f.component.value
        ]
        self.steps.append({"title": self.step_title.component.value, "fields": fields})
        self.fbview.steps = self.steps

        config = await get_config(interaction.client.db, interaction.guild.id, self.fbview.setup_id) or {}
        forms  = config.get("form_steps", {})
        forms[self.category] = self.steps
        config["form_steps"] = forms
        await set_config(interaction.client.db, interaction.guild.id, config, self.fbview.setup_id)

        await interaction.response.edit_message(
            view=FormBuilderView(self.category, steps=self.steps, status=f"{E_SUCCESS} Step **{len(self.steps)}** added!", setup_id=self.fbview.setup_id)
        )


def _form_builder_text(category: str, steps: list, status: str = "", hint: str = "") -> str:
    summary = "\n".join(
        f"> **Step {i+1}:** {s['title']} — {len(s['fields'])} field(s)"
        for i, s in enumerate(steps)
    ) if steps else "> Default single-step form (reason only)"
    lines = [f"## {E_CUSTOM} Form Builder — {category}"]
    if status:
        lines.append(status)
    lines.append(f"**Current form:**\n{summary}")
    if hint:
        lines.append(hint)
    lines.append("\nAdd more steps or click **Done**.")
    return "\n".join(lines)


class FormBuilderView(discord.ui.LayoutView):
    def __init__(self, category: str, steps: list | None = None, status: str = "", hint: str = "", setup_id: str = "1"):
        super().__init__(timeout=300)
        self.category = category
        self.setup_id = str(setup_id)
        self.steps: list = steps if steps is not None else []

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(_form_builder_text(category, self.steps, status, hint)))
        self.add_item(container)

        row = discord.ui.ActionRow()

        add_btn = discord.ui.Button(label="Add Step", style=discord.ButtonStyle.primary, emoji=E_CUSTOM)
        add_btn.callback = self._add_step_cb
        row.add_item(add_btn)

        reset_btn = discord.ui.Button(label="Reset to Default", style=discord.ButtonStyle.danger, emoji=E_DELETE)
        reset_btn.callback = self._reset_cb
        row.add_item(reset_btn)

        done_btn = discord.ui.Button(label="Done", style=discord.ButtonStyle.success, emoji=E_SUCCESS)
        done_btn.callback = self._done_cb
        row.add_item(done_btn)

        self.add_item(row)

    async def _add_step_cb(self, interaction: discord.Interaction):
        if len(self.steps) >= 4:
            return await interaction.response.send_message(
                view=_err("Maximum 4 steps per form."), ephemeral=True
            )
        await interaction.response.send_modal(AddStepModal(self.category, self.steps, self))

    async def _reset_cb(self, interaction: discord.Interaction):
        config = await get_config(interaction.client.db, interaction.guild.id, self.setup_id) or {}
        forms  = config.get("form_steps", {})
        forms.pop(self.category, None)
        config["form_steps"] = forms
        await set_config(interaction.client.db, interaction.guild.id, config, self.setup_id)
        await interaction.response.edit_message(
            view=FormBuilderView(self.category, steps=[], status=f"{E_SUCCESS} Form reset to the default single-step form.", setup_id=self.setup_id)
        )

    async def _done_cb(self, interaction: discord.Interaction):
        summary = "\n".join(
            f"> **Step {i+1}:** {s['title']} — {len(s['fields'])} field(s)"
            for i, s in enumerate(self.steps)
        ) if self.steps else "> Default single-step form (reason only)"
        await interaction.response.edit_message(
            view=_ok_raw(f"## {E_SUCCESS} Form Saved — {self.category}\n{summary}")
        )


# ── Setform Autocomplete ───────────────────────────────────────

async def _setform_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    setup_id = str(getattr(interaction.namespace, "setup", "1") or "1")
    config = await get_config(interaction.client.db, interaction.guild.id, setup_id) or {}
    cats   = config.get("active_categories", DEFAULT_CATEGORIES[:])
    # Also include any categories that already have form steps configured
    #  but were removed from active list — so admins can still edit them
    extra  = [c for c in config.get("form_steps", {}) if c not in cats]
    all_cats = cats + extra
    return [
        app_commands.Choice(name=c, value=c)
        for c in all_cats
        if current.lower() in c.lower()
    ][:25]


# ── Cog ───────────────────────────────────────────────────────

class _PrefixResponse:
    """Small adapter so prefix `ticket close` reuses the button/slash close flow."""
    def __init__(self, ctx: commands.Context):
        self.ctx = ctx

    async def send_message(self, content=None, **kwargs):
        kwargs.pop("ephemeral", None)
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        return await self.ctx.send(content=content, **kwargs)


class _PrefixInteraction:
    def __init__(self, ctx: commands.Context):
        self.client = ctx.bot
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.user = ctx.author
        self.response = _PrefixResponse(ctx)


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="ticket", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def ticket_prefix(self, ctx: commands.Context):
        await ctx.send(view=HelpView(page_key="root"), allowed_mentions=discord.AllowedMentions.none())

    @ticket_prefix.command(name="help", case_insensitive=True)
    async def ticket_help_prefix(self, ctx: commands.Context):
        await ctx.send(view=HelpView(page_key="root"), allowed_mentions=discord.AllowedMentions.none())

    async def _open_setup_prefix(self, ctx: commands.Context, *, dropdown: bool):
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send(view=_err("You need **Administrator** permission to configure tickets."), delete_after=5)
        state = SetupState()
        state.dropdown_setup = dropdown
        setup_id = "2" if dropdown else "1"
        reserved, retry_after = await _reserve_ticket_setup(ctx.guild.id, ctx.author.id, state)
        if not reserved:
            if retry_after:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
                return
            await ctx.send(view=_warn_embed(_setup_busy_text()), delete_after=5)
            _schedule_message_delete(ctx.message, 5)
            return
        try:
            existing = await get_config(self.bot.db, ctx.guild.id, setup_id) or {}
        except Exception:
            await _release_ticket_setup(state)
            raise
        state.active_categories = existing.get("active_categories", DEFAULT_CATEGORIES[:])
        state.category_emojis = existing.get("category_emojis", {})
        await _load_setup_targets(state, ctx.guild, existing)
        if dropdown:
            state.panel_title = existing.get("panel_title", "Support Center")
            state.panel_description = existing.get("panel_description", "Select a ticket type from the menu below.")
            state.author_name = existing.get("author_name", "")
            state.author_icon_url = existing.get("author_icon_url", "")
            state.footer_text = existing.get("footer_text", "Powered by Zyven Development")
            state.footer_icon_url = existing.get("footer_icon_url", "")
            state.thumbnail_url = existing.get("thumbnail_url", "")
            state.image_url = existing.get("image_url", "")
            state.panel_button_label = existing.get("panel_button_label", "")
            state.panel_button_url = existing.get("panel_button_url", "")
            state.panel_button_text = existing.get("panel_button_text", "")
            state.panel_button_emoji = existing.get("panel_button_emoji", "")
            state.panel_button_color = existing.get("panel_button_color", "Grey")
        else:
            state.show_main_button = existing.get("show_main_button", True)
            state.panel_button_label = existing.get("panel_button_label", "")
            state.panel_button_url = existing.get("panel_button_url", "")
            state.panel_button_text = existing.get("panel_button_text", "")
            state.panel_button_emoji = existing.get("panel_button_emoji", "")
            state.panel_button_color = existing.get("panel_button_color", "Grey")
        try:
            await ctx.send(view=SetupStep1Layout(state, dropdown=dropdown), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            await _release_ticket_setup(state)
            raise

    @ticket_prefix.command(name="setup", case_insensitive=True)
    async def ticket_setup_prefix(self, ctx: commands.Context):
        await self._open_setup_prefix(ctx, dropdown=False)

    @ticket_prefix.command(name="setup2", case_insensitive=True)
    async def ticket_setup2_prefix(self, ctx: commands.Context):
        await self._open_setup_prefix(ctx, dropdown=True)

    @ticket_prefix.command(name="panel2", case_insensitive=True)
    async def ticket_panel2_prefix(self, ctx: commands.Context):
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send(view=_err("You need **Administrator** permission to post a ticket panel."), delete_after=5)
        config = await get_config(self.bot.db, ctx.guild.id, "2")
        if not config:
            return await ctx.send(view=_err(f"Run `{ctx.prefix}ticket setup2` first."))
        cats = config.get("active_categories", DEFAULT_CATEGORIES[:])
        category_emojis = config.get("category_emojis", {})
        cat_list = "\n".join(f"> {_category_emoji(c, category_emojis)} **{c}**" for c in cats)
        description = f"{config.get('panel_description', 'Select a ticket type from the menu below.')}\n\n{cat_list}"
        guild_icon = ctx.guild.icon.url if ctx.guild.icon else None
        panel_view = build_panel_layout(
            title=config.get("panel_title", "Support Center"), description=description, color=EMBED_COLOR,
            author_name=config.get("author_name") or ctx.guild.name,
            author_icon_url=config.get("author_icon_url") or guild_icon or "",
            thumbnail_url=config.get("thumbnail_url") or guild_icon or "",
            image_url=config.get("image_url", ""), action_view=TicketSelectPanel(cats, category_emojis, setup_id="2"), setup_id="2",
            panel_button_label=config.get("panel_button_label", ""),
            panel_button_url=config.get("panel_button_url", ""),
            panel_button_text=config.get("panel_button_text", ""),
            panel_button_style=PANEL_BUTTON_COLOR_MAP.get(config.get("panel_button_color", "Grey"), discord.ButtonStyle.secondary),
            panel_button_emoji=config.get("panel_button_emoji", ""),
        )
        panel_ch = ctx.guild.get_channel(int(config["panel_channel_id"]))
        if panel_ch is None:
            panel_ch = await ctx.guild.fetch_channel(int(config["panel_channel_id"]))
        await panel_ch.send(view=panel_view, allowed_mentions=discord.AllowedMentions.none())
        await ctx.send(view=_ok(f"Dropdown panel sent to {panel_ch.mention}!"))

    @ticket_prefix.command(name="setform", case_insensitive=True)
    async def ticket_setform_prefix(self, ctx: commands.Context, *, category: str):
        if not ctx.author.guild_permissions.administrator:
            return await ctx.send(view=_err("You need **Administrator** permission to configure ticket forms."), delete_after=5)
        config = await get_config(self.bot.db, ctx.guild.id, "1") or {}
        existing = config.get("form_steps", {}).get(category, [])
        hint = "> Each step opens a modal. Add up to 4 steps with up to 4 fields each; use Reset to restore the default."
        await ctx.send(view=FormBuilderView(category, steps=existing[:], hint=hint), allowed_mentions=discord.AllowedMentions.none())

    @ticket_prefix.command(name="close", case_insensitive=True)
    async def ticket_close_prefix(self, ctx: commands.Context):
        await close_ticket(_PrefixInteraction(ctx))

    @ticket_prefix.command(name="add", case_insensitive=True)
    async def ticket_add_prefix(self, ctx: commands.Context, member: discord.Member):
        ticket = await get_ticket(self.bot.db, ctx.channel.id)
        if not ticket:
            return await ctx.send(view=_err("This is not a ticket channel."))
        prefix_interaction = _PrefixInteraction(ctx)
        if not await _ticket_manage_allowed(prefix_interaction, ticket):
            return await ctx.send(view=_err("Only the ticket owner, support staff, or a server administrator can change ticket access."))
        if getattr(getattr(member, "guild", None), "id", None) != ctx.guild.id:
            return await ctx.send(view=_err("The member must belong to this server."))
        await ctx.channel.set_permissions(member, read_messages=True, send_messages=True)
        await ctx.send(view=_ok(f"Added {member.mention} to the ticket."), allowed_mentions=discord.AllowedMentions.none())

    @ticket_prefix.command(name="remove", case_insensitive=True)
    async def ticket_remove_prefix(self, ctx: commands.Context, member: discord.Member):
        ticket = await get_ticket(self.bot.db, ctx.channel.id)
        if not ticket:
            return await ctx.send(view=_err("This is not a ticket channel."))
        prefix_interaction = _PrefixInteraction(ctx)
        if not await _ticket_manage_allowed(prefix_interaction, ticket):
            return await ctx.send(view=_err("Only the ticket owner, support staff, or a server administrator can change ticket access."))
        if getattr(getattr(member, "guild", None), "id", None) != ctx.guild.id:
            return await ctx.send(view=_err("The member must belong to this server."))
        await ctx.channel.set_permissions(member, overwrite=None)
        await ctx.send(view=_ok(f"Removed {member.mention} from the ticket."), allowed_mentions=discord.AllowedMentions.none())

    @ticket_prefix.command(name="transcript", case_insensitive=True)
    async def ticket_transcript_prefix(self, ctx: commands.Context):
        if not await get_ticket(self.bot.db, ctx.channel.id):
            return await ctx.send(view=_err("This is not a ticket channel."))
        transcript = await generate_transcript(ctx.channel)
        await ctx.send(view=_ok("Transcript ready!"), file=transcript, allowed_mentions=discord.AllowedMentions.none())

    @ticket_prefix.command(name="ticketinfo", case_insensitive=True)
    async def ticket_info_prefix(self, ctx: commands.Context):
        ticket = await get_ticket(self.bot.db, ctx.channel.id)
        if not ticket:
            return await ctx.send(view=_err("This is not a ticket channel."))
        author = ctx.guild.get_member(int(ticket["author_id"]))
        claimer_id = ticket.get("claimed_by")
        claimer = ctx.guild.get_member(int(claimer_id)) if claimer_id else None
        author_label = author.mention if author else f"`{ticket['author_id']}`"
        details = [
            f"## {E_VIEW} Ticket {ticket_num(ticket.get('number', 0))}",
            f"**Author:** {author_label}",
            f"**Category:** {ticket.get('category', 'Unknown')}",
            f"**Status:** {ticket.get('status', 'open').title()}",
            f"**Claimed:** {claimer.mention if claimer else 'Unclaimed'}",
        ]
        if ticket.get("reason") and ticket["reason"] != "No reason provided.":
            details.append(f"**Reason:** {ticket['reason'][:500]}")
        container = discord.ui.Container(accent_color=SUCCESS_COLOR if ticket.get("status") == "open" else ERROR_COLOR)
        body = "\n".join(details)
        if author:
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(body), accessory=discord.ui.Thumbnail(author.display_avatar.url)))
        else:
            container.add_item(discord.ui.TextDisplay(body))
        layout = discord.ui.LayoutView()
        layout.add_item(container)
        await ctx.send(view=layout, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_ready(self):
        refresh_ticket_emojis()
        # Restore persistent views — must match the custom_ids used when panels were sent
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketSelectPanel())
        self.bot.add_view(TicketControlView())
        self.bot.add_view(ClosedTicketView())
        self.bot.add_view(PanelExtraButtonRegistration())

    @app_commands.command(name="tickethelp", description="View ticket system help and commands")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=HelpView(page_key="root"), ephemeral=True)

    @app_commands.command(name="panel2", description="Send the dropdown-style ticket panel")
    @app_commands.default_permissions(administrator=True)
    async def panel2_cmd(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message(view=_err("You need Administrator permission to use this command."), ephemeral=True)
        config = await get_config(interaction.client.db, interaction.guild.id, "2")
        if not config:
            return await interaction.response.send_message(
                view=_err("Run `/setup` or `/setup2` first."), ephemeral=True
            )

        DEFAULT_BANNER = ""

        cats = config.get("active_categories", DEFAULT_CATEGORIES[:])
        category_emojis = config.get("category_emojis", {})
        cat_list = "\n".join(
            f"> {_category_emoji(c, category_emojis)} **{c}**"
            for c in cats
        )
        description = (
            f"{config.get('panel_description', 'Select a ticket type from the menu below.')}"
            f"\n\n{cat_list}"
        )

        author_name = config.get("author_name") or interaction.guild.name
        author_icon = config.get("author_icon_url") or (
            interaction.guild.icon.url if interaction.guild.icon else None
        )
        thumb = config.get("thumbnail_url") or (
            interaction.guild.icon.url if interaction.guild.icon else None
        )
        image_url = config.get("image_url") or DEFAULT_BANNER

        panel_view = build_panel_layout(
            title=config.get("panel_title", "Support Center"),
            description=description,
            color=EMBED_COLOR,
            author_name=author_name,
            author_icon_url=author_icon or "",
            thumbnail_url=thumb or "",
            image_url=image_url,
            action_view=TicketSelectPanel(cats, category_emojis, setup_id="2"),
            panel_button_label=config.get("panel_button_label", ""),
            panel_button_url=config.get("panel_button_url", ""),
            panel_button_text=config.get("panel_button_text", ""),
            panel_button_style=PANEL_BUTTON_COLOR_MAP.get(config.get("panel_button_color", "Grey"), discord.ButtonStyle.secondary),
            panel_button_emoji=config.get("panel_button_emoji", ""),
            setup_id="2",
        )

        panel_ch = interaction.guild.get_channel(int(config["panel_channel_id"]))
        if not panel_ch:
            panel_ch = await interaction.guild.fetch_channel(int(config["panel_channel_id"]))

        await panel_ch.send(view=panel_view)
        await interaction.response.send_message(
            view=_ok(f"Dropdown panel sent to {panel_ch.mention}!"), ephemeral=True
        )

    @app_commands.command(name="setform", description="Configure a multi-step form for a ticket category")
    @app_commands.describe(category="The ticket category to configure")
    @app_commands.autocomplete(category=_setform_autocomplete)
    @app_commands.choices(setup=[
        app_commands.Choice(name="Ticket Setup 1 — Buttons", value="1"),
        app_commands.Choice(name="Ticket Setup 2 — Dropdown", value="2"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def setform_cmd(self, interaction: discord.Interaction, category: str, setup: str = "1"):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message(view=_err("You need Administrator permission to use this command."), ephemeral=True)
        config   = await get_config(interaction.client.db, interaction.guild.id, setup) or {}
        existing = config.get("form_steps", {}).get(category, [])
        hint = (
            "> Each step = one modal shown to the user\n"
            "> Up to 4 steps, up to 4 fields each\n"
            "> Click **Add Step** to build your form\n"
            "> Click **Reset** to go back to the default"
        )
        view = FormBuilderView(category, steps=existing[:], hint=hint, setup_id=setup)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="close", description="Close the current ticket")
    async def close_cmd(self, interaction: discord.Interaction):
        await close_ticket(interaction)

    @app_commands.command(name="add", description="Add a member to this ticket")
    @app_commands.describe(member="Member to add")
    async def add_member(self, interaction: discord.Interaction, member: discord.Member):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("This is not a ticket channel."), ephemeral=True)
        if not await _ticket_manage_allowed(interaction, ticket):
            return await interaction.response.send_message(view=_err("Only the ticket owner, support staff, or a server administrator can change ticket access."), ephemeral=True)
        if getattr(getattr(member, "guild", None), "id", None) != interaction.guild.id:
            return await interaction.response.send_message(view=_err("The member must belong to this server."), ephemeral=True)
        await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)
        await interaction.response.send_message(
            view=_ok(f"Added {member.mention} to the ticket.")
        )

    @app_commands.command(name="remove", description="Remove a member from this ticket")
    @app_commands.describe(member="Member to remove")
    async def remove_member(self, interaction: discord.Interaction, member: discord.Member):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("This is not a ticket channel."), ephemeral=True)
        if not await _ticket_manage_allowed(interaction, ticket):
            return await interaction.response.send_message(view=_err("Only the ticket owner, support staff, or a server administrator can change ticket access."), ephemeral=True)
        if getattr(getattr(member, "guild", None), "id", None) != interaction.guild.id:
            return await interaction.response.send_message(view=_err("The member must belong to this server."), ephemeral=True)
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(
            view=_ok(f"Removed {member.mention} from the ticket.")
        )

    @app_commands.command(name="transcript", description="Generate an HTML transcript of this ticket")
    async def transcript_cmd(self, interaction: discord.Interaction):
        if not await get_ticket(interaction.client.db, interaction.channel.id):
            return await interaction.response.send_message(view=_err("This is not a ticket channel."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        f = await generate_transcript(interaction.channel)
        await interaction.followup.send(view=_ok("Transcript ready!"), file=f, ephemeral=True)

    @app_commands.command(name="ticketinfo", description="View details about the current ticket")
    async def ticket_info(self, interaction: discord.Interaction):
        ticket = await get_ticket(interaction.client.db, interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(view=_err("This is not a ticket channel."), ephemeral=True)

        author     = interaction.guild.get_member(int(ticket["author_id"]))
        claimer_id = ticket.get("claimed_by")
        claimer    = interaction.guild.get_member(int(claimer_id)) if claimer_id else None
        number     = ticket.get("number", 0)
        status     = ticket.get("status", "open")
        color      = SUCCESS_COLOR if status == "open" else ERROR_COLOR

        author_display = author.mention if author else f"`{ticket['author_id']}`"
        body_lines = [
            f"## {E_VIEW} Ticket {ticket_num(number)}",
            f"**Author:** {author_display}",
            f"**Category:** {ticket.get('category', 'Unknown')}",
            f"**Status:** {'Open' if status == 'open' else 'Closed'}",
            f"**Claimed:** {claimer.mention if claimer else 'Unclaimed'}",
        ]
        if ticket.get("reason") and ticket["reason"] != "No reason provided.":
            body_lines.append(f"**Reason:** {ticket['reason'][:500]}")
        body = "\n".join(body_lines)

        container = discord.ui.Container(accent_color=color)
        if author:
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(body),
                accessory=discord.ui.Thumbnail(author.display_avatar.url),
            ))
        else:
            container.add_item(discord.ui.TextDisplay(body))

        class _InfoLayout(discord.ui.LayoutView):
            def __init__(self):
                super().__init__()
                self.add_item(container)

        await interaction.response.send_message(view=_InfoLayout(), ephemeral=True)


# Ticket setup wizard from the provided ticket project, adapted to this bot's storage.
from utils.ticket_store import get_config, set_config

# ── Custom Emojis ──────────────────────────────────────────────
E_SUCCESS = icon("ticket_success")
E_CROSS = icon("ticket_cross")
E_INFO = icon("ticket_info")
E_WARN = icon("ticket_warning")
E_TICKET = icon("ticket_ticket")
E_BUG = icon("ticket_bug")
E_CLAIM = icon("ticket_claim")
E_VIEW = icon("ticket_view")
E_REOPEN = icon("ticket_reopen")
E_QUESTION = icon("ticket_question")
E_DELETE = icon("ticket_delete")
E_SETUP = icon("ticket_setup")
E_CUSTOM = icon("ticket_custom")

# ── Colors ─────────────────────────────────────────────────────
# All container accent bars + backgrounds are pure black, regardless of
# message type (success/error/info/warn all look the same now).
EMBED_COLOR   = 0x000000
SUCCESS_COLOR = 0x000000
ERROR_COLOR   = 0x000000
WARN_COLOR    = 0x000000
INFO_COLOR    = 0x000000

BRAND_FULL = "Powered by Zyven Development"

DEFAULT_BANNER = ""

DEFAULT_CATEGORIES = ["General Support", "Giveaway Claim", "Bug Report", "Other"]

COLOR_OPTIONS = ["Blue", "Green", "Red", "Grey", "Yellow"]

BUTTON_COLOR_MAP = {
    "Green":  discord.ButtonStyle.success,
    "Blue":   discord.ButtonStyle.primary,
    "Grey":   discord.ButtonStyle.secondary,
    "Yellow": discord.ButtonStyle.secondary,
    "Red":    discord.ButtonStyle.danger,
}

# Colour options for the optional Panel Text button. Discord only has 5
# real non-link button styles (Blurple, Grey, Green, Red — Yellow has no
# native style). Black/White/Yellow all render as Discord's grey button
# since there's no native style for them; labelled honestly in the picker
# so admins know what they'll actually get.
PANEL_BUTTON_COLOR_OPTIONS = ["Grey", "Yellow", "Blue", "Red", "Black", "White"]

PANEL_BUTTON_COLOR_MAP = {
    "Grey":   discord.ButtonStyle.secondary,
    "Yellow": discord.ButtonStyle.secondary,
    "Blue":   discord.ButtonStyle.primary,
    "Red":    discord.ButtonStyle.danger,
    "Black":  discord.ButtonStyle.secondary,
    "White":  discord.ButtonStyle.secondary,
}

CATEGORY_EMOJIS = {
    "General Support": E_QUESTION,
    "Giveaway Claim":  E_TICKET,
    "Bug Report":      E_BUG,
    "Other":           E_CUSTOM,
}


# ── Shared State ───────────────────────────────────────────────

class SetupState:
    def __init__(self):
        self.support_role: discord.Role | None               = None
        self.panel_channel: discord.TextChannel | None       = None
        self.ticket_category: discord.CategoryChannel | None = None
        self.log_channel: discord.TextChannel | None         = None
        self.dropdown_setup: bool = False
        self.ticket_limit: int       = 3
        self.panel_title: str        = "Support Center"
        self.panel_description: str  = "Click a button below to open a ticket."
        # Pre-filled defaults — server name/icon resolved at finish time
        self.author_name: str        = ""          # filled at finish with guild.name
        self.author_icon_url: str    = ""          # filled at finish with guild.icon
        self.footer_text: str        = "Powered by Zyven Development"
        self.footer_icon_url: str    = ""          # bot avatar, resolved at finish
        self.thumbnail_url: str      = ""          # guild icon, resolved at finish
        self.image_url: str          = DEFAULT_BANNER
        self.button_label: str       = "Open a Ticket"
        self.button_emoji: str       = ""
        # Per-button colours: key = category name or "main"
        self.button_colors: dict[str, str] = {
            "main":            "Blue",
            "General Support": "Grey",
            "Giveaway Claim":  "Green",
            "Bug Report":      "Red",
            "Other":           "Grey",
        }
        self.active_categories: list[str] = DEFAULT_CATEGORIES[:]
        self.category_emojis: dict[str, str] = {}
        # Whether the main "Open a Ticket" button is shown on the button
        # panel. Category buttons remain the only way to open tickets if
        # this is turned off — guarded so it can never be turned off when
        # there are zero categories left (and vice versa).
        self.show_main_button: bool  = True
        # Optional extra button shown under the panel's main text.
        # Empty label = no button at all on the panel.
        self.panel_button_label: str = ""
        self.panel_button_url: str   = ""   # if set -> link button
        self.panel_button_text: str  = ""   # if no url -> shown as popup on click
        self.panel_button_emoji: str = ""
        self.panel_button_color: str = "Grey"
        # Track whether user explicitly edited author/footer/thumbnail
        # so we know not to overwrite at finish if they cleared them
        self._author_edited: bool    = False
        self._footer_edited: bool    = False
        self._thumb_edited: bool     = False

    def resolved_author_name(self, guild: discord.Guild) -> str:
        if self._author_edited:
            return self.author_name
        return self.author_name or guild.name

    def resolved_author_icon(self, guild: discord.Guild) -> str:
        if self._author_edited:
            return self.author_icon_url
        return self.author_icon_url or (guild.icon.url if guild.icon else "")

    def resolved_footer_icon(self, bot: discord.Client) -> str:
        if self._footer_edited:
            return self.footer_icon_url
        return self.footer_icon_url or (bot.user.display_avatar.url if bot.user else "")

    def resolved_thumbnail(self, guild: discord.Guild) -> str:
        if self._thumb_edited:
            return self.thumbnail_url
        return self.thumbnail_url or (guild.icon.url if guild.icon else "")


# ── Components V2 Helpers ───────────────────────────────────────

class MessageLayout(discord.ui.LayoutView):
    """Generic single-container layout for simple text replies."""
    def __init__(self, text: str, color: int, *, extra_rows: list[discord.ui.ActionRow] | None = None):
        super().__init__()
        container = discord.ui.Container(accent_color=color)
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)
        for row in (extra_rows or []):
            self.add_item(row)


def _ok(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_SUCCESS}  {msg}", SUCCESS_COLOR)

def _ok_raw(text: str) -> MessageLayout:
    """Like _ok but takes pre-formatted text (e.g. with its own heading)."""
    return MessageLayout(text, SUCCESS_COLOR)

def _err(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_CROSS}  {msg}", ERROR_COLOR)

def _info(msg: str) -> MessageLayout:
    return MessageLayout(f"{E_INFO}  {msg}", INFO_COLOR)


# ── Modals ─────────────────────────────────────────────────────

class PanelTextModal(discord.ui.Modal, title="Panel Text Settings"):
    panel_title = _modal_field(
        "Panel Title", placeholder="e.g. Support Center", max_length=100,
    )
    panel_description = _modal_field(
        "Panel Description", style=discord.TextStyle.paragraph,
        placeholder="Click a button below to open a ticket.",
        max_length=300, required=False,
    )
    author_name = _modal_field(
        "Author Name  (blank = server name)",
        placeholder="Defaults to your server name", required=False, max_length=100,
    )
    author_icon_url = _modal_field(
        "Author Icon URL  (blank = server icon)",
        placeholder="Defaults to your server icon", required=False, max_length=500,
    )

    def __init__(self, state: SetupState):
        super().__init__()
        self.state = state
        self.panel_title.component.default       = state.panel_title
        self.panel_description.component.default = state.panel_description
        self.author_name.component.default       = state.author_name
        self.author_icon_url.component.default   = state.author_icon_url

    async def on_submit(self, interaction: discord.Interaction):
        self.state.panel_title       = self.panel_title.component.value
        self.state.panel_description = self.panel_description.component.value
        self.state.author_name       = self.author_name.component.value
        self.state.author_icon_url   = self.author_icon_url.component.value
        self.state._author_edited    = True
        await interaction.response.send_message(
            view=_ok("Panel text updated."), ephemeral=True
        )


class FooterMediaModal(discord.ui.Modal, title="Footer, Thumbnail & Banner"):
    footer_text = _modal_field(
        "Footer Text  (blank = 'Zyven Ticket System')",
        placeholder="Zyven Ticket System", required=False, max_length=200,
    )
    footer_icon_url = _modal_field(
        "Footer Icon URL  (blank = bot avatar)",
        placeholder="Defaults to bot avatar", required=False, max_length=500,
    )
    thumbnail_url = _modal_field(
        "Thumbnail URL  (blank = server icon)",
        placeholder="Defaults to your server icon", required=False, max_length=500,
    )
    image_url = _modal_field(
        "Banner Image URL  (blank = default GIF)",
        placeholder="Leave blank to use no banner image", required=False, max_length=500,
    )

    def __init__(self, state: SetupState):
        super().__init__()
        self.state = state
        self.footer_text.component.default     = state.footer_text
        self.footer_icon_url.component.default = state.footer_icon_url
        self.thumbnail_url.component.default   = state.thumbnail_url
        self.image_url.component.default       = state.image_url

    async def on_submit(self, interaction: discord.Interaction):
        self.state.footer_text     = self.footer_text.component.value or "Zyven Ticket System"
        self.state.footer_icon_url = self.footer_icon_url.component.value
        self.state.thumbnail_url   = self.thumbnail_url.component.value
        self.state.image_url       = self.image_url.component.value or DEFAULT_BANNER
        self.state._footer_edited  = True
        self.state._thumb_edited   = True
        await interaction.response.send_message(
            view=_ok("Footer, thumbnail and banner updated."), ephemeral=True
        )


class ButtonLabelModal(discord.ui.Modal, title="Main Button Settings"):
    button_label = _modal_field(
        "Button Label", placeholder="e.g. Open a Ticket",
        default="Open a Ticket", max_length=80,
    )
    button_emoji = _modal_field(
        "Button Emoji", placeholder="Leave blank for default ticket emoji",
        required=False, max_length=100,
    )

    def __init__(self, state: SetupState):
        super().__init__()
        self.state = state
        self.button_label.component.default = state.button_label
        self.button_emoji.component.default = state.button_emoji

    async def on_submit(self, interaction: discord.Interaction):
        self.state.button_label = self.button_label.component.value
        self.state.button_emoji = self.button_emoji.component.value
        await interaction.response.send_message(
            view=_ok("Button settings updated."), ephemeral=True
        )


class AddCategoryModal(discord.ui.Modal, title="Add New Category"):
    cat_name = _modal_field(
        "Category Name", placeholder="e.g. Partnership Request", max_length=80,
    )
    cat_emoji = _modal_field(
        "Category Emoji (optional)",
        placeholder="Paste custom emoji, e.g. <:support:123456789012345678>",
        required=False, max_length=100,
    )

    def __init__(self, state: SetupState, *, dropdown: bool = False):
        super().__init__()
        self.state    = state
        self.dropdown = dropdown

    async def on_submit(self, interaction: discord.Interaction):
        name = self.cat_name.component.value.strip()
        if not name:
            return await interaction.response.send_message(
                view=_err("Category name cannot be empty."), ephemeral=True
            )
        if name in self.state.active_categories:
            return await interaction.response.send_message(
                view=_err(f"**{name}** already exists."), ephemeral=True
            )
        if len(self.state.active_categories) >= 10:
            return await interaction.response.send_message(
                view=_err("Maximum 10 categories allowed."), ephemeral=True
            )
        emoji_value = self.cat_emoji.component.value.strip()
        if emoji_value:
            try:
                parsed_emoji = discord.PartialEmoji.from_str(emoji_value)
                if not parsed_emoji.name or (emoji_value.startswith("<") and parsed_emoji.id is None):
                    raise ValueError("invalid emoji")
            except (TypeError, ValueError):
                return await interaction.response.send_message(
                    view=_err("That emoji format is not valid. Paste a server custom emoji such as `<:name:emoji_id>` or a normal emoji."),
                    ephemeral=True,
                )
            emoji_value = str(parsed_emoji)
        self.state.active_categories.append(name)
        self.state.category_emojis[name] = emoji_value
        # Default colour for new button
        self.state.button_colors[name] = "Grey"

        # Rebuild whichever step-2 flow this modal was opened from
        # (button panel vs dropdown panel) — previously this always
        # rebuilt the button-panel view even when launched from /setup2.
        if self.dropdown:
            await interaction.response.edit_message(
                view=SetupStep2Layout(self.state, dropdown=True)
            )
        else:
            await interaction.response.edit_message(
                view=SetupStep2Layout(self.state)
            )


# ── Panel Text Button (optional extra button under panel text) ─

class PanelButtonModal(discord.ui.Modal, title="Panel Button"):
    button_label = _modal_field(
        "Button Label  (blank = no button at all)",
        placeholder="e.g. Rules, FAQ, Chat Here", required=False, max_length=80,
    )
    button_url = _modal_field(
        "Button URL  (optional)",
        placeholder="https://… — leave blank to show popup text instead",
        required=False, max_length=500,
    )
    button_text = _modal_field(
        "Popup Text  (shown if no URL is set)",
        style=discord.TextStyle.paragraph,
        placeholder="Text shown to the user when they click the button",
        required=False, max_length=1000,
    )
    button_emoji = _modal_field(
        "Button Emoji  (optional)", required=False, max_length=100,
    )

    def __init__(self, state: SetupState):
        super().__init__()
        self.state = state
        self.button_label.component.default = state.panel_button_label
        self.button_url.component.default   = state.panel_button_url
        self.button_text.component.default  = state.panel_button_text
        self.button_emoji.component.default = state.panel_button_emoji

    async def on_submit(self, interaction: discord.Interaction):
        label = self.button_label.component.value.strip()
        url   = self.button_url.component.value.strip()

        if url and not (url.startswith("http://") or url.startswith("https://")):
            return await interaction.response.send_message(
                view=_err("Button URL must start with `http://` or `https://`."), ephemeral=True
            )
        if label and not url and not self.button_text.component.value.strip():
            return await interaction.response.send_message(
                view=_err("Set either a **URL** or some **Popup Text** for the button to do something when clicked."),
                ephemeral=True,
            )

        self.state.panel_button_label = label
        self.state.panel_button_url   = url
        self.state.panel_button_text  = self.button_text.component.value.strip()
        self.state.panel_button_emoji = self.button_emoji.component.value.strip()

        msg = "Panel button removed." if not label else "Panel button updated."
        await interaction.response.send_message(view=_ok(msg), ephemeral=True)


class PanelButtonColorSelect(discord.ui.Select):
    _STYLE_DISPLAY_NAMES = {
        discord.ButtonStyle.primary:   "Blurple",
        discord.ButtonStyle.secondary: "Grey",
        discord.ButtonStyle.success:   "Green",
        discord.ButtonStyle.danger:    "Red",
    }

    def __init__(self, state: SetupState):
        self.state = state
        current = state.panel_button_color
        super().__init__(
            placeholder="Colour for the panel button…",
            options=[
                discord.SelectOption(
                    label=c,
                    value=c,
                    description=f"Renders as Discord's {self._STYLE_DISPLAY_NAMES[PANEL_BUTTON_COLOR_MAP[c]]} button",
                    default=(c == current),
                )
                for c in PANEL_BUTTON_COLOR_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.panel_button_color = self.values[0]
        await interaction.response.send_message(
            view=_ok(f"Panel button colour set to **{self.values[0]}**."), ephemeral=True
        )


class PanelButtonColorView(discord.ui.LayoutView):
    def __init__(self, state: SetupState):
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(
            f"{E_INFO}  Pick a colour for the panel button.\n"
            f"-# Discord has no native Black/White/Yellow button — these render as the standard grey button."
        ))
        row = discord.ui.ActionRow()
        row.add_item(PanelButtonColorSelect(state))
        container.add_item(row)
        self.add_item(container)


# ── Button Colour Select (targets a specific button key) ───────

class SingleButtonColorSelect(discord.ui.Select):
    def __init__(self, state: SetupState, target: str, label: str):
        self.state  = state
        self.target = target
        current = state.button_colors.get(target, "Blue")
        super().__init__(
            placeholder=f"Colour for: {label}",
            options=[
                discord.SelectOption(
                    label=c,
                    value=c,
                    description=f"Set {label} to {c}",
                    default=(c == current),
                )
                for c in COLOR_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.button_colors[self.target] = self.values[0]
        await interaction.response.send_message(
            view=_ok(f"Colour set to **{self.values[0]}**."), ephemeral=True
        )


class SingleButtonColorView(discord.ui.LayoutView):
    def __init__(self, state: SetupState, target: str, label: str):
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_color=INFO_COLOR)
        container.add_item(discord.ui.TextDisplay(f"{E_INFO}  Choose a colour for **{label}**."))
        row = discord.ui.ActionRow()
        row.add_item(SingleButtonColorSelect(state, target, label))
        container.add_item(row)
        self.add_item(container)


# ── All-buttons colour picker ───────────────────────────────────

class AllButtonsColorSelect(discord.ui.Select):
    """Shows a dropdown to pick which button to recolour."""
    def __init__(self, state: SetupState):
        self.state = state
        options = [
            discord.SelectOption(
                label=f"Main Button ({state.button_label})",
                value="main",
                description=f"Currently: {state.button_colors.get('main', 'Blue')}",
                emoji=E_TICKET,
            )
        ] + [
            discord.SelectOption(
                label=cat,
                value=cat,
                description=f"Currently: {state.button_colors.get(cat, 'Grey')}",
                emoji=_category_emoji(cat, state.category_emojis),
            )
            for cat in state.active_categories
        ]
        super().__init__(
            placeholder="Which button to recolour?",
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        label  = "Main Button" if target == "main" else target
        await interaction.response.send_message(
            view=SingleButtonColorView(self.state, target, label),
            ephemeral=True,
        )


class AllButtonsColorView(discord.ui.LayoutView):
    def __init__(self, state: SetupState):
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_color=INFO_COLOR)
        container.add_item(discord.ui.TextDisplay(f"{E_INFO}  Pick a button to change its colour."))
        row = discord.ui.ActionRow()
        row.add_item(AllButtonsColorSelect(state))
        container.add_item(row)
        self.add_item(container)


# ── Delete Categories Select ───────────────────────────────────

class DeleteCatsSelect(discord.ui.Select):
    """Lets the admin remove category buttons/options — and, on the button
    panel only, the main 'Open a Ticket' button itself. Guarded so the
    panel can never end up with nothing left to click: removing the main
    button is blocked while zero categories remain, and removing the last
    category is blocked while the main button is already hidden."""

    MAIN_VALUE = "__main_button__"

    def __init__(self, state: SetupState, *, dropdown: bool, parent_message: discord.Message | None):
        self.state          = state
        self.dropdown        = dropdown
        self.parent_message  = parent_message

        options = [
            discord.SelectOption(label=c, value=c, emoji=_category_emoji(c, state.category_emojis))
            for c in state.active_categories
        ]
        # The main button is only ever a removable option on the button
        # panel — the dropdown panel has no separate main button.
        if not dropdown and state.show_main_button:
            options.append(discord.SelectOption(
                label=f"Open a Ticket — Main Button ({state.button_label})",
                value=self.MAIN_VALUE,
                emoji=E_TICKET,
                description="The main button shown above the category buttons",
            ))

        super().__init__(
            placeholder="Select what to remove…",
            min_values=1,
            max_values=max(1, len(options)),
            options=options or [discord.SelectOption(label="Nothing left to remove", value="__none__")],
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction):
        wants_main = self.MAIN_VALUE in self.values
        cats_to_remove = [v for v in self.values if v != self.MAIN_VALUE]

        # Work out what would remain after this removal, then block the
        # whole action if it would leave the panel with nothing clickable.
        remaining_cats = [c for c in self.state.active_categories if c not in cats_to_remove]
        remaining_main = self.state.show_main_button and not wants_main

        if not self.dropdown and not remaining_cats and not remaining_main:
            return await interaction.response.send_message(
                view=_err(
                    "Can't remove that — the panel needs **at least one** "
                    "category button or the main button to stay clickable."
                ),
                ephemeral=True,
            )
        if self.dropdown and not remaining_cats:
            return await interaction.response.send_message(
                view=_err("Can't remove the last category — the dropdown needs at least one option."),
                ephemeral=True,
            )

        for cat in cats_to_remove:
            if cat in self.state.active_categories:
                self.state.active_categories.remove(cat)
                self.state.category_emojis.pop(cat, None)
                self.state.button_colors.pop(cat, None)
        if wants_main:
            self.state.show_main_button = False

        removed_bits = []
        if cats_to_remove:
            removed_bits.append(", ".join(cats_to_remove))
        if wants_main:
            removed_bits.append("the main button")
        removed_summary = " and ".join(removed_bits)

        remaining = ", ".join(self.state.active_categories) or "None"
        if not self.dropdown:
            remaining += f"  •  Main button: {'shown' if self.state.show_main_button else 'hidden'}"

        # Refresh this ephemeral picker in place so it reflects what's left
        await interaction.response.edit_message(
            view=DeleteCatsView(self.state, dropdown=self.dropdown, parent_message=self.parent_message)
        )

        # Also push the change back to the underlying step-2 panel message,
        # which previously went stale after removing a category from /setup2
        # or /setup.
        if self.parent_message is not None:
            try:
                if self.dropdown:
                    await self.parent_message.edit(view=SetupStep2Layout(self.state, dropdown=True))
                else:
                    await self.parent_message.edit(view=SetupStep2Layout(self.state))
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            view=_ok(f"Removed {removed_summary}. **Remaining:** {remaining}"), ephemeral=True
        )


class DeleteCatsView(discord.ui.LayoutView):
    def __init__(self, state: SetupState, *, dropdown: bool = False, parent_message: discord.Message | None = None):
        super().__init__(timeout=60)
        container = discord.ui.Container(accent_color=INFO_COLOR)
        has_options = bool(state.active_categories) or (not dropdown and state.show_main_button)
        text = (
            f"{E_INFO}  Select what to remove."
            if has_options
            else f"{E_WARN}  Nothing left to remove."
        )
        container.add_item(discord.ui.TextDisplay(text))
        row = discord.ui.ActionRow()
        row.add_item(DeleteCatsSelect(state, dropdown=dropdown, parent_message=parent_message))
        container.add_item(row)
        self.add_item(container)


# ── Setup Step 1: Channels & Roles ─────────────────────────────

class DeleteSetupButton(discord.ui.Button):
    def __init__(self, state: SetupState):
        self.state = state
        token = getattr(state, "_setup_token", "expired")
        super().__init__(
            label="Delete Setup",
            style=discord.ButtonStyle.danger,
            emoji=E_DELETE,
            custom_id=f"ticket_setup:delete:{token}",
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != getattr(self.state, "_setup_owner_id", None):
            return await interaction.response.send_message(
                view=_err("Only the admin who started this setup can delete it."), ephemeral=True
            )
        try:
            await interaction.response.defer()
            await interaction.message.delete()
        except discord.HTTPException:
            return await interaction.followup.send(
                view=_err("Could not delete this setup message. Please try again."), ephemeral=True
            )
        await _release_ticket_setup(self.state)


class _SupportRoleSelect(discord.ui.RoleSelect):
    def __init__(self, state: SetupState):
        self.state = state
        super().__init__(placeholder="① Support Role  (required)", min_values=1, max_values=1,
                         default_values=[state.support_role] if state.support_role else [])

    async def callback(self, interaction: discord.Interaction):
        self.state.support_role = self.values[0]
        self.state._saved_target_ids["support_role_id"] = self.values[0].id
        await interaction.response.edit_message(view=SetupStep1Layout(self.state, dropdown=self.state.dropdown_setup))


class _PanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, state: SetupState):
        self.state = state
        super().__init__(
            placeholder="② Panel Channel  (required)",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            default_values=[state.panel_channel] if state.panel_channel else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.panel_channel = self.values[0]
        self.state._saved_target_ids["panel_channel_id"] = self.values[0].id
        await interaction.response.edit_message(view=SetupStep1Layout(self.state, dropdown=self.state.dropdown_setup))


class _TicketCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, state: SetupState):
        self.state = state
        super().__init__(
            placeholder="③ Ticket Category  (required)",
            channel_types=[discord.ChannelType.category],
            min_values=1, max_values=1,
            default_values=[state.ticket_category] if state.ticket_category else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.ticket_category = self.values[0]
        self.state._saved_target_ids["ticket_category_id"] = self.values[0].id
        await interaction.response.edit_message(view=SetupStep1Layout(self.state, dropdown=self.state.dropdown_setup))


class _LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, state: SetupState):
        self.state = state
        super().__init__(
            placeholder="④ Log Channel  (optional)",
            channel_types=[discord.ChannelType.text],
            min_values=0, max_values=1,
            default_values=[state.log_channel] if state.log_channel else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.log_channel = self.values[0] if self.values else None
        self.state._saved_target_ids["log_channel_id"] = self.values[0].id if self.values else None
        await interaction.response.edit_message(view=SetupStep1Layout(self.state, dropdown=self.state.dropdown_setup))


class SetupStep1Layout(discord.ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _setup_user_allowed(interaction, self.state):
            return True
        await _reject_setup_interaction(interaction)
        return False

    def __init__(self, state: SetupState, *, dropdown: bool = False):
        super().__init__(timeout=300)
        self.state    = state
        state.dropdown_setup = dropdown
        self._setup_view_marker = object()
        state._current_setup_view_marker = self._setup_view_marker
        self.dropdown = dropdown

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(_step1_text(dropdown=dropdown, state=state)))

        for select_cls in (_SupportRoleSelect, _PanelChannelSelect, _TicketCategorySelect, _LogChannelSelect):
            row = discord.ui.ActionRow()
            row.add_item(select_cls(state))
            container.add_item(row)

        btn_row = discord.ui.ActionRow()
        label = "Next — Customise Dropdown Panel" if dropdown else "Next — Customise Panel"
        next_btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji=E_SETUP)
        next_btn.callback = self._next_cb
        btn_row.add_item(next_btn)
        btn_row.add_item(DeleteSetupButton(state))
        container.add_item(btn_row)

        self.add_item(container)

    async def on_timeout(self):
        if getattr(self.state, "_current_setup_view_marker", None) is self._setup_view_marker:
            await _release_ticket_setup(self.state)

    async def _next_cb(self, interaction: discord.Interaction):
        if not self.state.support_role or not self.state.panel_channel or not self.state.ticket_category:
            return await interaction.response.send_message(
                view=_err("Please select a **Support Role**, **Panel Channel** and **Ticket Category** first."),
                ephemeral=True,
            )
        if self.dropdown:
            await interaction.response.edit_message(view=SetupStep2Layout(self.state, dropdown=True))
        else:
            await interaction.response.edit_message(view=SetupStep2Layout(self.state))


# ── Setup Step 2: Customisation ────────────────────────────────

class SetupStep2Layout(discord.ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _setup_user_allowed(interaction, self.state):
            return True
        await _reject_setup_interaction(interaction)
        return False

    """Customisation step shared by /setup (button panel) and /setup2 (dropdown panel)."""
    def __init__(self, state: SetupState, *, dropdown: bool = False):
        super().__init__(timeout=300)
        self.state    = state
        self._setup_view_marker = object()
        state._current_setup_view_marker = self._setup_view_marker
        self.dropdown = dropdown

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(_step2_text(state, dropdown=dropdown)))

        # Row 0 — inside container
        row0 = discord.ui.ActionRow()
        panel_text_btn = discord.ui.Button(label="Panel Text", style=discord.ButtonStyle.primary, emoji=E_CUSTOM)
        panel_text_btn.callback = self._panel_text_cb
        row0.add_item(panel_text_btn)

        footer_media_btn = discord.ui.Button(label="Footer & Media", style=discord.ButtonStyle.primary, emoji=E_VIEW)
        footer_media_btn.callback = self._footer_media_cb
        row0.add_item(footer_media_btn)

        if not dropdown:
            button_label_btn = discord.ui.Button(label="Button Label", style=discord.ButtonStyle.secondary, emoji=E_TICKET)
            button_label_btn.callback = self._button_label_cb
            row0.add_item(button_label_btn)
        container.add_item(row0)

        # Row 0.5 — optional extra panel button (e.g. Rules/FAQ), inside container
        row_extra = discord.ui.ActionRow()
        panel_btn_btn = discord.ui.Button(label="Panel Button", style=discord.ButtonStyle.secondary, emoji=E_VIEW)
        panel_btn_btn.callback = self._panel_button_cb
        row_extra.add_item(panel_btn_btn)

        panel_btn_color_btn = discord.ui.Button(label="Panel Button Colour", style=discord.ButtonStyle.secondary, emoji=E_INFO)
        panel_btn_color_btn.callback = self._panel_button_color_cb
        row_extra.add_item(panel_btn_color_btn)
        container.add_item(row_extra)

        # Row 1 — inside container
        row1 = discord.ui.ActionRow()
        if not dropdown:
            colour_btn = discord.ui.Button(label="Button Colours", style=discord.ButtonStyle.secondary, emoji=E_INFO)
            colour_btn.callback = self._colour_cb
            row1.add_item(colour_btn)

        add_btn = discord.ui.Button(label="Add Category", style=discord.ButtonStyle.success, emoji=E_SUCCESS)
        add_btn.callback = self._add_category_cb
        row1.add_item(add_btn)

        remove_label = "Remove Category / Main Btn" if not dropdown else "Remove Category"
        remove_btn = discord.ui.Button(label=remove_label, style=discord.ButtonStyle.danger, emoji=E_DELETE)
        remove_btn.callback = self._remove_category_cb
        row1.add_item(remove_btn)
        container.add_item(row1)

        # Row 2 — inside container
        row2 = discord.ui.ActionRow()
        back_btn = discord.ui.Button(label="◀ Back", style=discord.ButtonStyle.secondary, emoji=E_REOPEN)
        back_btn.callback = self._back_cb
        row2.add_item(back_btn)

        preview_label = "Preview & Send" if dropdown else "Preview & Finish"
        preview_btn = discord.ui.Button(label=preview_label, style=discord.ButtonStyle.success, emoji=E_SUCCESS)
        preview_btn.callback = self._preview_cb
        row2.add_item(preview_btn)
        row2.add_item(DeleteSetupButton(state))
        container.add_item(row2)

        self.add_item(container)

    async def on_timeout(self):
        if getattr(self.state, "_current_setup_view_marker", None) is self._setup_view_marker:
            await _release_ticket_setup(self.state)

    async def _panel_text_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PanelTextModal(self.state))

    async def _footer_media_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FooterMediaModal(self.state))

    async def _button_label_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ButtonLabelModal(self.state))

    async def _panel_button_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PanelButtonModal(self.state))

    async def _panel_button_color_cb(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=PanelButtonColorView(self.state),
            ephemeral=True,
        )

    async def _colour_cb(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=AllButtonsColorView(self.state),
            ephemeral=True,
        )

    async def _add_category_cb(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddCategoryModal(self.state, dropdown=self.dropdown))

    async def _remove_category_cb(self, interaction: discord.Interaction):
        has_anything_to_remove = bool(self.state.active_categories) or (
            not self.dropdown and self.state.show_main_button
        )
        if not has_anything_to_remove:
            return await interaction.response.send_message(
                view=_err("Nothing left to remove."), ephemeral=True
            )
        # Pass the parent (step-2) message through so the select's callback
        # can refresh it after removing categories — previously the panel
        # underneath went stale and Add Category afterwards could even
        # rebuild the wrong panel type from /setup2.
        await interaction.response.send_message(
            view=DeleteCatsView(self.state, dropdown=self.dropdown, parent_message=interaction.message),
            ephemeral=True,
        )

    async def _back_cb(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SetupStep1Layout(self.state, dropdown=self.dropdown))

    async def _preview_cb(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=PreviewLayout(self.state, dropdown=self.dropdown)
        )


# ── Confirm View ───────────────────────────────────────────────

class PreviewLayout(discord.ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _setup_user_allowed(interaction, self.state):
            return True
        await _reject_setup_interaction(interaction)
        return False

    def __init__(self, state: SetupState, *, dropdown: bool = False):
        super().__init__(timeout=120)
        self.state    = state
        self._setup_view_marker = object()
        state._current_setup_view_marker = self._setup_view_marker
        self.dropdown = dropdown

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(_preview_text(state, dropdown=dropdown)))

        row = discord.ui.ActionRow()
        back_btn = discord.ui.Button(label="◀ Back", style=discord.ButtonStyle.secondary, emoji=E_REOPEN)
        back_btn.callback = self._back_cb
        row.add_item(back_btn)

        finish_label = "Send Dropdown Panel" if dropdown else "Finish Setup"
        finish_btn = discord.ui.Button(label=finish_label, style=discord.ButtonStyle.success, emoji=E_SUCCESS)
        finish_btn.callback = self._finish_cb
        row.add_item(finish_btn)
        row.add_item(DeleteSetupButton(state))
        container.add_item(row)

        self.add_item(container)

    async def on_timeout(self):
        if getattr(self.state, "_current_setup_view_marker", None) is self._setup_view_marker:
            await _release_ticket_setup(self.state)

    async def _back_cb(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=SetupStep2Layout(self.state, dropdown=self.dropdown))

    async def _finish_cb(self, interaction: discord.Interaction):
        refresh_ticket_emojis()
        s = self.state
        bot = interaction.client

        # Resolve defaults
        author_name     = s.resolved_author_name(interaction.guild)
        author_icon_url = s.resolved_author_icon(interaction.guild)
        footer_icon_url = s.resolved_footer_icon(bot)
        thumbnail_url   = s.resolved_thumbnail(interaction.guild)

        setup_id = "2" if self.dropdown else "1"
        existing = await get_config(interaction.client.db, interaction.guild.id, setup_id) or {}
        config = {
            "support_role_id":    s.support_role.id,
            "panel_channel_id":   s.panel_channel.id,
            "ticket_category_id": s.ticket_category.id,
            "log_channel_id":     s.log_channel.id if s.log_channel else None,
            "ticket_limit":       existing.get("ticket_limit", s.ticket_limit),
            "panel_title":        s.panel_title,
            "panel_description":  s.panel_description,
            "author_name":        author_name,
            "author_icon_url":    author_icon_url,
            "footer_text":        s.footer_text,
            "footer_icon_url":    footer_icon_url,
            "thumbnail_url":      thumbnail_url,
            "image_url":          s.image_url,
            "button_label":       existing.get("button_label", s.button_label),
            "button_emoji":       existing.get("button_emoji", s.button_emoji),
            "button_colors":      existing.get("button_colors", s.button_colors),
            "show_main_button":   existing.get("show_main_button", s.show_main_button),
            "active_categories":  s.active_categories,
            "category_emojis":   s.category_emojis,
            "panel_button_label": s.panel_button_label,
            "panel_button_url":   s.panel_button_url,
            "panel_button_text":  s.panel_button_text,
            "panel_button_emoji": s.panel_button_emoji,
            "panel_button_color": s.panel_button_color,
            "form_steps":         existing.get("form_steps", {}),
        }
        await set_config(interaction.client.db, interaction.guild.id, config, setup_id)

        try:
            if self.dropdown:
                from cogs.tickets import TicketSelectPanel, build_panel_layout

                cats = s.active_categories
                cat_list = "\n".join(
                    f"> {_category_emoji(c, s.category_emojis)} **{c}**"
                    for c in cats
                )
                description = f"{s.panel_description}\n\n{cat_list}"
                panel_view = build_panel_layout(
                    title=s.panel_title,
                    description=description,
                    color=EMBED_COLOR,
                    author_name=author_name,
                    author_icon_url=author_icon_url,
                    thumbnail_url=thumbnail_url,
                    image_url=s.image_url,
                    action_view=TicketSelectPanel(cats, s.category_emojis, setup_id="2"),
                    panel_button_label=s.panel_button_label,
                    panel_button_url=s.panel_button_url,
                    panel_button_text=s.panel_button_text,
                    panel_button_style=PANEL_BUTTON_COLOR_MAP.get(s.panel_button_color, discord.ButtonStyle.secondary),
                    panel_button_emoji=s.panel_button_emoji,
                    setup_id="2",
                )
            else:
                from cogs.tickets import TicketPanelView, build_panel_layout

                panel_view = build_panel_layout(
                    title=s.panel_title,
                    description=s.panel_description,
                    color=EMBED_COLOR,
                    author_name=author_name,
                    author_icon_url=author_icon_url,
                    thumbnail_url=thumbnail_url,
                    image_url=s.image_url,
                    action_view=TicketPanelView(
                        button_label=s.button_label,
                        button_emoji=s.button_emoji or None,
                        button_color=s.button_colors.get("main", "Blue"),
                        button_colors=s.button_colors,
                        active_categories=s.active_categories,
                        category_emojis=s.category_emojis,
                        show_main_button=s.show_main_button,
                        setup_id="1",
                    ),
                    panel_button_label=s.panel_button_label,
                    panel_button_url=s.panel_button_url,
                    panel_button_text=s.panel_button_text,
                    panel_button_style=PANEL_BUTTON_COLOR_MAP.get(s.panel_button_color, discord.ButtonStyle.secondary),
                    panel_button_emoji=s.panel_button_emoji,
                )

            panel_ch = interaction.guild.get_channel(s.panel_channel.id)
            if panel_ch is None:
                panel_ch = await interaction.guild.fetch_channel(s.panel_channel.id)

            await panel_ch.send(view=panel_view)

            if self.dropdown:
                done_text = (
                    f"{E_SUCCESS}  **Dropdown Panel Sent**\n\n"
                    f"Your dropdown panel is live in {panel_ch.mention}.\n\n"
                    f"> {E_VIEW}  Users select a ticket type from a dropdown\n"
                    f"> {E_CUSTOM}  Use `/setform` to configure per-category forms\n"
                    f"> {E_INFO}  Use `/tickethelp` or `{settings.prefix}ticket help` for the command reference"
                )
            else:
                done_text = (
                    f"{E_SUCCESS}  **Setup Complete**\n\n"
                    f"Your ticket system is live in {panel_ch.mention}.\n\n"
                    f"> {E_TICKET}  Button panel sent to the panel channel\n"
                    f"> {E_VIEW}  Use `/setup2` to configure the dropdown panel\n"
                    f"> {E_CUSTOM}  Use `/setform` to configure ticket forms\n"
                    f"> {E_INFO}  Use `/tickethelp` or `{settings.prefix}ticket help` for the command reference"
                )
            await interaction.response.edit_message(view=_ok_raw(done_text))

        except Exception as e:
            await interaction.response.edit_message(view=_err(f"Config saved but failed to send panel: `{e}`"))
        finally:
            await _release_ticket_setup(s)



# ── Text Builders (Components V2) ──────────────────────────────

def _step1_text(*, dropdown: bool = False, state: SetupState | None = None) -> str:
    kind = "Ticket Setup 2 — Dropdown Panel" if dropdown else "Ticket Setup 1 — Button Panel"
    posted_as = "dropdown panel" if dropdown else "button panel"
    text = (
        f"## {E_SETUP}  {kind} — Step 1 of 2\n"
        f"Configure your channels and roles below. Current selections are shown here; choose a different value to change one.\n\n"
        f"**{E_CLAIM}  Required**\n"
        f"> **①** Support Role — who can manage tickets\n"
        f"> **②** Panel Channel — where the {posted_as} is posted\n"
        f"> **③** Ticket Category — where ticket channels open\n\n"
        f"**{E_INFO}  Optional**\n"
        f"> **④** Log Channel — where ticket events are logged\n\n"
    )
    if state is not None:
        config_ids = getattr(state, "_saved_target_ids", {})
        text += (
            f"**Currently Selected**\n"
            f"> **Support Role:** {_target_label(state.support_role, config_ids.get('support_role_id'))}\n"
            f"> **Panel Channel:** {_target_label(state.panel_channel, config_ids.get('panel_channel_id'))}\n"
            f"> **Ticket Category:** {_target_label(state.ticket_category, config_ids.get('ticket_category_id'))}\n"
            f"> **Log Channel:** {_target_label(state.log_channel, config_ids.get('log_channel_id'))}\n\n"
        )
    return text + "Fill in the required fields then click **Next**."


def _step2_text(state: SetupState, *, dropdown: bool = False) -> str:
    cats = ", ".join(
        f"{_category_emoji(category, state.category_emojis)} {category}"
        for category in state.active_categories
    ) or "None"
    kind = "Dropdown Panel" if dropdown else "Ticket"
    lines = [
        f"## {E_CUSTOM}  {kind} Setup — Step 2 of 2",
        "Customise your panel below.\n",
        f"> {E_CUSTOM}  **Panel Text** — title, description, author name & icon",
        f"> {E_VIEW}  **Footer & Media** — footer, thumbnail, banner",
        f"> {E_VIEW}  **Panel Button** — optional extra button (link or popup) under the panel text",
        f"> {E_INFO}  **Panel Button Colour** — colour for that extra button",
    ]
    if not dropdown:
        lines.append(f"> {E_TICKET}  **Button Label** — main button label & emoji")
        lines.append(f"> {E_INFO}  **Button Colours** — colour for every button on the panel")
    lines.append(f"> {E_SUCCESS}  **Add Category** — add a new ticket category option")
    remove_line = (
        "remove an existing category, or remove the main button" if not dropdown else "remove an existing category option"
    )
    lines.append(f"> {E_DELETE}  **Remove Category / Main Btn** — {remove_line}")
    lines.append(f"\n**Active Categories:** {cats}")
    if not dropdown:
        lines.append(f"**Main Button:** {'Shown' if state.show_main_button else 'Hidden'}")
    if state.panel_button_label:
        lines.append(f"**Panel Button:** {state.panel_button_label} ({state.panel_button_color})")
    if not dropdown:
        colors_preview = "  ".join(
            f"`{k}: {v}`" for k, v in state.button_colors.items()
            if k == "main" or k in state.active_categories
        )
        lines.append(f"**Colours:** {colors_preview or 'defaults'}")
    finish_label = "Preview & Send" if dropdown else "Preview & Finish"
    lines.append(f"\nClick **{finish_label}** when ready.")
    return "\n".join(lines)


def _preview_text(s: SetupState, *, dropdown: bool = False) -> str:
    kind = "Dropdown Panel" if dropdown else "Button Panel"
    finish_label = "Send Dropdown Panel" if dropdown else "Finish Setup"

    lines = [
        f"## {E_VIEW}  {kind} — Preview",
        f"Review your settings then click **{finish_label}**.\n",
        f"> **Panel Channel:** {s.panel_channel.mention}",
        f"> **Support Role:** {s.support_role.mention}",
        f"> **Ticket Category:** {s.ticket_category.name}",
        f"> **Log Channel:** {s.log_channel.mention if s.log_channel else 'None'}",
        f"> **Panel Title:** {s.panel_title}",
        f"> **Author Name:** {s.author_name or '(server name)'}",
        f"> **Footer Text:** {s.footer_text}",
        f"> **Thumbnail:** {f'{E_SUCCESS} Set' if s.thumbnail_url else '(server icon)'}",
        f"> **Banner Image:** {f'{E_SUCCESS} Set' if s.image_url else 'None'}",
        f"> **Active Categories:** {', '.join(f'{_category_emoji(category, s.category_emojis)} {category}' for category in s.active_categories) or 'None'}",
        f"> **Panel Button:** {f'{s.panel_button_label} ({s.panel_button_color})' if s.panel_button_label else 'None'}",
    ]
    if not dropdown:
        lines.append(f"> **Main Button:** {'Shown' if s.show_main_button else 'Hidden'}")
    if not dropdown:
        colors_text = "  ".join(
            f"`{k}: {v}`" for k, v in s.button_colors.items()
            if k == "main" or k in s.active_categories
        ) or "defaults"
        lines.append(f"> **Button Colours:** {colors_text}")
    return "\n".join(lines)


# ── Cog ───────────────────────────────────────────────────────

class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure the Zyven Ticket System button panel")
    @app_commands.default_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message(view=_err("You need Administrator permission to use this command."), ephemeral=True)
        refresh_ticket_emojis()
        state = SetupState()
        state.dropdown_setup = False
        reserved, retry_after = await _reserve_ticket_setup(interaction.guild.id, interaction.user.id, state)
        if not reserved:
            await interaction.response.send_message(
                view=_warn_embed(_setup_busy_text(retry_after)), ephemeral=True
            )
            _schedule_ephemeral_setup_notice_delete(interaction)
            return
        try:
            existing = await get_config(interaction.client.db, interaction.guild.id, "1") or {}
        except Exception:
            await _release_ticket_setup(state)
            raise
        state.active_categories = existing.get("active_categories", DEFAULT_CATEGORIES[:])
        state.category_emojis = existing.get("category_emojis", {})
        await _load_setup_targets(state, interaction.guild, existing)
        state.show_main_button   = existing.get("show_main_button", True)
        state.panel_button_label = existing.get("panel_button_label", "")
        state.panel_button_url   = existing.get("panel_button_url", "")
        state.panel_button_text  = existing.get("panel_button_text", "")
        state.panel_button_emoji = existing.get("panel_button_emoji", "")
        state.panel_button_color = existing.get("panel_button_color", "Grey")
        try:
            await interaction.response.send_message(
                view=SetupStep1Layout(state, dropdown=False), ephemeral=True
            )
        except Exception:
            await _release_ticket_setup(state)
            raise

    @app_commands.command(name="setup2", description="Configure and send the dropdown ticket panel")
    @app_commands.default_permissions(administrator=True)
    async def setup2(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message(view=_err("You need Administrator permission to use this command."), ephemeral=True)
        refresh_ticket_emojis()
        # Reserve one setup wizard per server before any database work.
        state = SetupState()
        state.dropdown_setup = True
        reserved, retry_after = await _reserve_ticket_setup(interaction.guild.id, interaction.user.id, state)
        if not reserved:
            await interaction.response.send_message(
                view=_warn_embed(_setup_busy_text(retry_after)), ephemeral=True
            )
            _schedule_ephemeral_setup_notice_delete(interaction)
            return
        try:
            existing = await get_config(interaction.client.db, interaction.guild.id, "2") or {}
        except Exception:
            await _release_ticket_setup(state)
            raise
        state.panel_title       = existing.get("panel_title", "Support Center")
        state.panel_description = existing.get("panel_description", "Select a ticket type from the menu below.")
        state.author_name       = existing.get("author_name", "")
        state.author_icon_url   = existing.get("author_icon_url", "")
        state.footer_text       = existing.get("footer_text", "Powered by Zyven Development")
        state.footer_icon_url   = existing.get("footer_icon_url", "")
        state.thumbnail_url     = existing.get("thumbnail_url", "")
        state.image_url         = existing.get("image_url", DEFAULT_BANNER)
        state.active_categories = existing.get("active_categories", DEFAULT_CATEGORIES[:])
        state.category_emojis = existing.get("category_emojis", {})
        await _load_setup_targets(state, interaction.guild, existing)
        state.panel_button_label = existing.get("panel_button_label", "")
        state.panel_button_url   = existing.get("panel_button_url", "")
        state.panel_button_text  = existing.get("panel_button_text", "")
        state.panel_button_emoji = existing.get("panel_button_emoji", "")
        state.panel_button_color = existing.get("panel_button_color", "Grey")
        try:
            await interaction.response.send_message(
                view=SetupStep1Layout(state, dropdown=True), ephemeral=True
            )
        except Exception:
            await _release_ticket_setup(state)
            raise

def refresh_ticket_emojis():
    """Resolve namespaced Developer Portal emojis after the shared sync."""
    mapping = {
        "E_SUCCESS": "ticket_success", "E_CROSS": "ticket_cross", "E_INFO": "ticket_info",
        "E_WARN": "ticket_warning", "E_TICKET": "ticket_ticket", "E_BUG": "ticket_bug",
        "E_CLAIM": "ticket_claim", "E_VIEW": "ticket_view", "E_REOPEN": "ticket_reopen",
        "E_QUESTION": "ticket_question", "E_DELETE": "ticket_delete", "E_SETUP": "ticket_setup",
        "E_CUSTOM": "ticket_custom",
    }
    previous = {key: globals()[key] for key in mapping}
    for key, emoji_name in mapping.items():
        globals()[key] = icon(emoji_name)
    CATEGORY_EMOJIS.update({
        "General Support": E_QUESTION,
        "Giveaway Claim": E_TICKET,
        "Bug Report": E_BUG,
        "Other": E_CUSTOM,
    })
    # Help page text is assembled once at import time; replace its fallback
    # symbols too after the application emoji sync finishes.
    for page in HELP_PAGES.values():
        for key, old_value in previous.items():
            if old_value and old_value != globals()[key]:
                page["title"] = page["title"].replace(old_value, globals()[key])
                page["body"] = page["body"].replace(old_value, globals()[key])


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
    await bot.add_cog(TicketsCog(bot))
