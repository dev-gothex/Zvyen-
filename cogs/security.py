import asyncio
import logging
import re
import time
import aiohttp
from collections import defaultdict, deque
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import AntinukePunishmentMenuView, CategoryActions, EventLimitMenuView, EventLimitView, ExtraownerView, FeatureSetupView, SecurityOverviewView, antinuke_limit_menu_embed, antinuke_punishment_menu_embed, can_access_security, can_manage, category_screen, component_card, edit_original_component_card, event_limit_page_embed, extraowner_embed, followup_component_card, is_security_actor, panel, security_overview_embed, security_status_embed, send_card, send_security_access_denied, notice_panel

DEFAULT_ANTI_NUKE_EVENTS = ["member_ban", "member_unban", "member_kick", "member_prune", "bot_add", "channel_create", "channel_delete", "channel_update", "role_create", "role_delete", "role_update", "role_position", "role_link_permission", "role_invite_permission", "member_update", "member_role_update", "emoji_create", "emoji_delete", "emoji_update", "sticker_create", "sticker_delete", "sticker_update", "integration_create", "integration_delete", "integration_update", "webhook_create", "webhook_spam", "webhook_delete", "webhook_update", "guild_update", "everyone_ping", "role_ping"]
ANTINUKE_EVENT_LABELS = {
    "member_ban": "Anti Ban", "member_unban": "Anti Unban", "member_kick": "Anti Kick",
    "member_prune": "Anti Member Prune", "bot_add": "Anti Bot Add",
    "channel_create": "Anti Channel Create", "channel_delete": "Anti Channel Delete",
    "channel_update": "Anti Channel Update", "role_create": "Anti Role Create",
    "role_delete": "Anti Role Delete", "role_update": "Anti Role Update",
    "role_position": "Anti Role Position", "role_link_permission": "Anti Link Role",
    "role_invite_permission": "Anti Invite Role", "member_update": "Anti Member Update",
    "member_role_update": "Anti Member Role Update", "emoji_create": "Anti Emoji Create",
    "emoji_delete": "Anti Emoji Delete", "emoji_update": "Anti Emoji Update",
    "sticker_create": "Anti Sticker Create", "sticker_delete": "Anti Sticker Delete",
    "sticker_update": "Anti Sticker Update", "integration_create": "Anti Integration Create",
    "integration_delete": "Anti Integration Delete", "integration_update": "Anti Integration Update",
    "webhook_create": "Anti Webhook Create", "webhook_spam": "Anti Webhook Spam",
    "webhook_delete": "Anti Webhook Delete", "webhook_update": "Anti Webhook Update",
    "guild_update": "Anti Server Update", "everyone_ping": "Anti Everyone/Here Ping",
    "role_ping": "Anti Role Ping",
}
ANTINUKE_EVENT_ALIASES = {
    **{key: key for key in ANTINUKE_EVENT_LABELS},
    **{label.lower().replace("anti ", "").replace("/", "_").replace(" ", "_"): key for key, label in ANTINUKE_EVENT_LABELS.items()},
    "ban": "member_ban", "unban": "member_unban", "kick": "member_kick", "prune": "member_prune",
    "channelcreate": "channel_create", "channeldelete": "channel_delete", "channelupdate": "channel_update",
    "rolecreate": "role_create", "roledelete": "role_delete", "roleupdate": "role_update",
}
WHITELIST_PERMISSION_GROUPS = [
    ("member_ban", "Anti Ban", ("member_ban",)),
    ("member_unban", "Anti Unban", ("member_unban",)),
    ("member_kick", "Anti Kick", ("member_kick",)),
    ("member_prune", "Anti Member Prune", ("member_prune",)),
    ("bot_add", "Anti Bot Add", ("bot_add",)),
    ("channel_create", "Anti Channel Create", ("channel_create",)),
    ("channel_delete", "Anti Channel Delete", ("channel_delete",)),
    ("channel_update", "Anti Channel Update", ("channel_update",)),
    ("role_create", "Anti Role Create", ("role_create",)),
    ("role_delete", "Anti Role Delete", ("role_delete",)),
    ("role_update", "Anti Role Update", ("role_update",)),
    ("role_position", "Anti Role Position", ("role_position",)),
    ("member_update", "Anti Member Update", ("member_update", "member_role_update")),
    ("emoji_create", "Anti Emoji/Sticker Create", ("emoji_create", "sticker_create")),
    ("emoji_delete", "Anti Emoji/Sticker Delete", ("emoji_delete", "sticker_delete")),
    ("emoji_update", "Anti Emoji/Sticker Update", ("emoji_update", "sticker_update")),
    ("everyone_ping", "Anti Everyone/Here Ping", ("everyone_ping",)),
    ("role_ping", "Anti Role Ping", ("role_ping",)),
    ("integration_create", "Anti Integration", ("integration_create", "integration_delete", "integration_update")),
    ("guild_update", "Anti Guild Update", ("guild_update",)),
    ("webhook_create", "Anti Webhook Create", ("webhook_create",)),
    ("webhook_delete", "Anti Webhook Delete", ("webhook_delete",)),
    ("webhook_update", "Anti Webhook Update", ("webhook_update",)),
]
log = logging.getLogger("zyven.security")


def safe_audit_target(entry: discord.AuditLogEntry):
    """Audit target conversion can fail when Discord sends a null target ID."""
    try:
        return entry.target
    except (AttributeError, TypeError, ValueError):
        return None


def normalized_id_set(values) -> set[int]:
    """Normalize persisted Discord IDs, which may be stored as strings or ints."""
    normalized = set()
    for value in values or ():
        try:
            normalized.add(int(value))
        except (TypeError, ValueError):
            continue
    return normalized


class Security(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.event_bursts = defaultdict(lambda: deque(maxlen=51))
        self.event_targets = defaultdict(lambda: deque(maxlen=51))
        self.events_since_sweep = 0
        self.recovery_in_progress: set[int] = set()
        self.recovery_snapshot_locks = defaultdict(asyncio.Lock)
        self.recovery_snapshot_tasks: dict[int, asyncio.Task] = {}
        self.recovery_snapshot_deadlines: dict[int, float] = {}
        self._snapshot_pool_warning_at: dict[int, float] = {}
        self.invite_use_snapshots: dict[int, dict[str, dict]] = {}
        self.invite_snapshots_ready: set[int] = set()
        self.invite_snapshot_locks = defaultdict(asyncio.Lock)
        self.owner_dm_pending: dict[int, list[str]] = defaultdict(list)
        self.owner_dm_tasks: dict[int, asyncio.Task] = {}
        self._handled_webhook_audit_ids: set[int] = set()
        self._recent_audit_entries: dict[tuple[int, discord.AuditLogAction, int], discord.AuditLogEntry] = {}
        self._audit_lookup_locks = defaultdict(asyncio.Lock)
        self._audit_rest_lock = asyncio.Lock()
        self._audit_last_request: dict[int, float] = {}
        self._audit_global_last_request = 0.0
        self._audit_global_unavailable_until = 0.0
        self._audit_warning_at: dict[tuple[int, str], float] = {}

    def queue_owner_security_dm(self, guild: discord.Guild, actor, action: str, result: str) -> None:
        """Batch server security activity into private owner DMs, not console logs."""
        actor_id = getattr(actor, "id", None)
        actor_name = getattr(actor, "name", "Unknown")
        self.owner_dm_pending[guild.id].append(
            f"**Server:** {guild.name} (`{guild.id}`)\n"
            f"**Actor:** {actor_name} (`{actor_id if actor_id is not None else 'unknown'}`)\n"
            f"**Action:** {action}\n**Result:** {result}"
        )
        task = self.owner_dm_tasks.get(guild.id)
        if task is None or task.done():
            self.owner_dm_tasks[guild.id] = self.bot.loop.create_task(self.flush_owner_security_dms(guild))

    async def flush_owner_security_dms(self, guild: discord.Guild) -> None:
        # Batch each guild separately and deliver only to that server's owner.
        await asyncio.sleep(1.25)
        while self.owner_dm_pending[guild.id]:
            batch = self.owner_dm_pending[guild.id][:10]
            del self.owner_dm_pending[guild.id][:10]
            try:
                owner = guild.owner
                if owner is None:
                    owner = self.bot.get_user(guild.owner_id) or await self.bot.fetch_user(guild.owner_id)
                embed = panel("Zyven Server Security Activity", "\n\n".join(batch)[:4000], color=0xED4245)
                await owner.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                # A closed owner DM must never affect protection or flood host logs.
                log.debug("Could not deliver a server security DM to guild owner %s", guild.owner_id)
        self.owner_dm_tasks.pop(guild.id, None)

    async def _fetch_invite_snapshot(self, guild: discord.Guild) -> dict[str, dict] | None:
        """Read invite uses and role grants without relying on audit-log entries."""
        try:
            route = discord.http.Route("GET", "/guilds/{guild_id}/invites", guild_id=guild.id)
            payload = await self.bot.http.request(route)
        except discord.NotFound as exc:
            if exc.code == 10004:
                self.invite_use_snapshots.pop(guild.id, None)
                self.invite_snapshots_ready.discard(guild.id)
                log.debug("Guild %s disappeared during invite tracking", guild.id)
            else:
                log.warning("Invite tracking returned 404 in guild %s (code %s)", guild.id, exc.code)
            return None
        except discord.Forbidden:
            log.info("Invite tracking unavailable in guild %s; Zyven needs Manage Server or View Audit Log to read invite usage", guild.id)
            return None
        except discord.HTTPException as exc:
            log.warning("Could not refresh invite tracking in guild %s (HTTP %s, code %s)", guild.id, exc.status, exc.code)
            return None

        snapshot = {}
        for invite in payload if isinstance(payload, list) else []:
            code = invite.get("code")
            if not code:
                continue
            inviter = invite.get("inviter") or {}
            snapshot[str(code)] = {
                "uses": int(invite.get("uses") or 0),
                "inviter_id": int(inviter["id"]) if inviter.get("id") else None,
                "role_ids": {int(role_id) for role_id in invite.get("role_ids", [])},
            }
        return snapshot

    async def _prime_invite_snapshot(self, guild: discord.Guild, *, force: bool = False):
        async with self.invite_snapshot_locks[guild.id]:
            if guild.id in self.invite_snapshots_ready and not force:
                return
            snapshot = await self._fetch_invite_snapshot(guild)
            if snapshot is None:
                return
            self.invite_use_snapshots[guild.id] = snapshot
            self.invite_snapshots_ready.add(guild.id)

    async def _resolve_admin_invite_creator(self, member: discord.Member, admin_role_ids: set[int]) -> int | None:
        """Infer the creator from invite use changes, with a safe unique-role fallback."""
        guild = member.guild
        async with self.invite_snapshot_locks[guild.id]:
            if guild.id not in self.invite_snapshots_ready:
                baseline = await self._fetch_invite_snapshot(guild)
                if baseline is None:
                    return None
                self.invite_use_snapshots[guild.id] = baseline
                self.invite_snapshots_ready.add(guild.id)
                # On startup the first join may arrive before on_ready's snapshot.
                # Attribute only if exactly one invite can grant one of these roles.
                role_matches = [
                    invite for invite in baseline.values()
                    if invite["inviter_id"] and admin_role_ids.intersection(invite["role_ids"])
                ]
                return role_matches[0]["inviter_id"] if len(role_matches) == 1 else None

            for attempt in range(3):
                current = await self._fetch_invite_snapshot(guild)
                if current is None:
                    return None
                previous = self.invite_use_snapshots.get(guild.id, {})
                candidates = [
                    invite for code, invite in current.items()
                    if invite["uses"] > previous.get(code, {}).get("uses", 0)
                    and invite["inviter_id"]
                    and admin_role_ids.intersection(invite["role_ids"])
                ]
                self.invite_use_snapshots[guild.id] = current
                if len(candidates) == 1:
                    return candidates[0]["inviter_id"]
                if len(candidates) > 1:
                    log.warning("Could not safely identify a unique admin-role invite for member %s in guild %s", member.id, guild.id)
                    return None
                # Concurrent joins can consume the invite-use delta before this
                # event reads it. Fall back only when a single active invite can
                # grant an Administrator role; ambiguous matches remain unknown.
                role_matches = [
                    invite for invite in current.values()
                    if invite["inviter_id"] and admin_role_ids.intersection(invite["role_ids"])
                ]
                if len(role_matches) == 1:
                    return role_matches[0]["inviter_id"]
                if len(role_matches) > 1:
                    log.warning("Could not safely identify a unique admin-role invite for member %s in guild %s", member.id, guild.id)
                    return None
                if attempt < 2:
                    await asyncio.sleep(0.4)
            return None

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if not invite.guild or invite.guild.id not in self.invite_snapshots_ready:
            return
        async with self.invite_snapshot_locks[invite.guild.id]:
            snapshot = await self._fetch_invite_snapshot(invite.guild)
            if snapshot is not None:
                self.invite_use_snapshots[invite.guild.id] = snapshot

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            self.invite_use_snapshots.get(invite.guild.id, {}).pop(invite.code, None)

    async def authorize_security_interaction(self, interaction: discord.Interaction) -> bool:
        data = await self.bot.db.get_guild(interaction.guild_id)
        if not can_access_security(interaction, data):
            await send_security_access_denied(interaction)
            return False
        return True

    @commands.command(name="security")
    @commands.guild_only()
    async def prefix_security(self, ctx: commands.Context, *, action: str | None = None):
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.reply(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), mention_author=False, delete_after=5)
            return
        if action and action.lower() in {"status", "info"}:
            await ctx.send(view=component_card(security_status_embed(ctx.guild, data)))
            return
        embed = security_overview_embed(data)
        view = SecurityOverviewView(self.bot, ctx.guild.id, settings.owner_id, data)
        await ctx.send(view=component_card(embed, view))

    @commands.hybrid_group(name="extraowner", aliases=("extraowners",), invoke_without_command=True, case_insensitive=True, description="Manage trusted server extraowners")
    @commands.guild_only()
    async def prefix_extraowner(self, ctx: commands.Context):
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.send(view=component_card(notice_panel("Only the server owner can configure extraowners.", title="Access Denied")), allowed_mentions=discord.AllowedMentions.none(), delete_after=5)
            return
        security = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {})
        owners = sorted({int(user_id) for user_id in security.get("extraowners", [])})
        view = ExtraownerView(self.bot, ctx.guild.id, ctx.guild.owner_id, owners)
        await ctx.send(view=component_card(extraowner_embed(owners), view), allowed_mentions=discord.AllowedMentions.none())

    @prefix_extraowner.command(name="add", description="Add a trusted extraowner")
    async def prefix_extraowner_add(self, ctx: commands.Context, member: discord.Member):
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.send(view=component_card(notice_panel("Only the server owner can configure extraowners.", title="Access Denied")), allowed_mentions=discord.AllowedMentions.none(), delete_after=5)
            return
        security = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {})
        owners = {int(user_id) for user_id in security.get("extraowners", [])}
        if member.id in owners:
            await ctx.send(view=component_card(panel("Already an Extraowner", f"{member.mention} is already trusted as an Extraowner.")), allowed_mentions=discord.AllowedMentions.none())
            return
        owners.add(member.id)
        await self.bot.db.set_nested(ctx.guild.id, "security", "extraowners", sorted(owners))
        await ctx.send(view=component_card(panel(f"{icon('tick')} Success", f"Added {member.mention} as the server's Extraowner.")), allowed_mentions=discord.AllowedMentions.none())

    @prefix_extraowner.command(name="view", aliases=("list",), description="View trusted extraowners")
    async def prefix_extraowner_view(self, ctx: commands.Context):
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.send(view=component_card(notice_panel("Only the server owner can configure extraowners.", title="Access Denied")), allowed_mentions=discord.AllowedMentions.none(), delete_after=5)
            return
        security = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {})
        owners = sorted({int(user_id) for user_id in security.get("extraowners", [])})
        await ctx.send(view=component_card(extraowner_embed(owners)), allowed_mentions=discord.AllowedMentions.none())

    @prefix_extraowner.command(name="reset", description="Remove all trusted extraowners")
    async def prefix_extraowner_reset(self, ctx: commands.Context):
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.send(view=component_card(notice_panel("Only the server owner can configure extraowners.", title="Access Denied")), allowed_mentions=discord.AllowedMentions.none(), delete_after=5)
            return
        await self.bot.db.set_nested(ctx.guild.id, "security", "extraowners", [])
        await ctx.send(view=component_card(panel(f"{icon('tick')} Success", "All Extraowners have been removed.")), allowed_mentions=discord.AllowedMentions.none())

    async def _send_ignore_help(self, ctx: commands.Context, title: str, entries: list[tuple[str, str]]):
        from cogs.helpdesk import HelpPagesView

        requester = getattr(ctx.author, "display_name", str(ctx.author))
        view = HelpPagesView(requester_id=ctx.author.id, title=title, entries=entries, page_size=5).set_requester(requester)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    async def _ignore_values(self, guild_id: int, key: str) -> set:
        security = (await self.bot.db.get_guild(guild_id)).get("security", {})
        ignore = security.get("ignore", {})
        if key == "commands":
            return {str(value).casefold() for value in ignore.get(key, [])}
        try:
            return {int(value) for value in ignore.get(key, [])}
        except (TypeError, ValueError):
            return set()

    async def _change_ignore_value(self, ctx: commands.Context, key: str, value, action: str, label: str):
        values = await self._ignore_values(ctx.guild.id, key)
        normalized = value.casefold() if key == "commands" else int(value)
        if action == "add" and normalized in values:
            await ctx.send(view=component_card(panel("Already Ignored", f"`{value}` is already in the ignored {label} list.")), allowed_mentions=discord.AllowedMentions.none())
            return
        if action == "remove" and normalized not in values:
            await ctx.send(view=component_card(notice_panel(f"`{value}` is not in the ignored {label} list.", title="Not Found")), allowed_mentions=discord.AllowedMentions.none())
            return
        values.add(normalized) if action == "add" else values.discard(normalized)
        await self.bot.db.set_nested(ctx.guild.id, "security", f"ignore.{key}", sorted(values))
        action_word = "added" if action == "add" else "removed"
        await ctx.send(view=component_card(panel(f"{icon('tick')} Success", f"Successfully {action_word} `{value}` to the ignored {label} list.")), allowed_mentions=discord.AllowedMentions.none())

    async def _show_ignore_values(self, ctx: commands.Context, key: str, label: str, *, mentions: bool = False):
        values = sorted(await self._ignore_values(ctx.guild.id, key), key=str.casefold if key == "commands" else None)
        shown = [f"<@{value}>" if mentions and key == "users" or mentions and key == "bypass_users" else f"<#{value}>" if mentions and key == "channels" else f"`{value}`" for value in values]
        embed = panel(f"Ignored {label}", "\n".join(shown) if shown else f"No {label.lower()} are configured.")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name="ignore", invoke_without_command=True, case_insensitive=True, description="Configure ignored commands, channels, and users")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore(self, ctx: commands.Context):
        await self._send_ignore_help(ctx, "Zyven Ignore Group", [
            (f"{settings.prefix}ignore command", "Manage ignored commands in this guild."),
            (f"{settings.prefix}ignore channel", "Manage ignored channels in this guild."),
            (f"{settings.prefix}ignore bypass", "Manage bypassed users in this guild."),
            (f"{settings.prefix}ignore user", "Manage ignored users in this guild."),
        ])

    @prefix_ignore.command(name="help")
    async def prefix_ignore_help(self, ctx: commands.Context):
        await self.prefix_ignore(ctx)

    @prefix_ignore.group(name="command", invoke_without_command=True, case_insensitive=True)
    async def prefix_ignore_command(self, ctx: commands.Context):
        await self._send_ignore_help(ctx, "Zyven Ignore Command Group", [
            (f"{settings.prefix}ignore command show", "Displays the list of ignored commands."),
            (f"{settings.prefix}ignore command remove <command_name>", "Removes a command from the ignore list."),
            (f"{settings.prefix}ignore command add <command_name>", "Adds a command to the ignore list."),
        ])

    @prefix_ignore_command.command(name="help")
    async def prefix_ignore_command_help(self, ctx: commands.Context):
        await self.prefix_ignore_command(ctx)

    @prefix_ignore_command.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_command_add(self, ctx: commands.Context, *, command_name: str):
        command_name = command_name.strip().removeprefix(settings.prefix).strip().casefold()
        command = self.bot.get_command(command_name)
        if command is None:
            await ctx.send(view=component_card(notice_panel(f"I couldn't find `{command_name}`. Use the command's registered name.")), allowed_mentions=discord.AllowedMentions.none())
            return
        await self._change_ignore_value(ctx, "commands", command.qualified_name, "add", "commands")

    @prefix_ignore_command.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_command_remove(self, ctx: commands.Context, *, command_name: str):
        command_name = command_name.strip().removeprefix(settings.prefix).strip().casefold()
        command = self.bot.get_command(command_name)
        name = command.qualified_name if command else command_name
        await self._change_ignore_value(ctx, "commands", name, "remove", "commands")

    @prefix_ignore_command.command(name="show")
    async def prefix_ignore_command_show(self, ctx: commands.Context):
        await self._show_ignore_values(ctx, "commands", "Commands")

    @prefix_ignore.group(name="channel", invoke_without_command=True, case_insensitive=True)
    async def prefix_ignore_channel(self, ctx: commands.Context):
        await self._send_ignore_help(ctx, "Zyven Ignore Channel Group", [
            (f"{settings.prefix}ignore channel add <channel>", "Adds a channel to the ignore list."),
            (f"{settings.prefix}ignore channel remove <channel>", "Removes a channel from the ignore list."),
            (f"{settings.prefix}ignore channel show", "Displays the list of ignored channels."),
        ])

    @prefix_ignore_channel.command(name="help")
    async def prefix_ignore_channel_help(self, ctx: commands.Context):
        await self.prefix_ignore_channel(ctx)

    @prefix_ignore_channel.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_channel_add(self, ctx: commands.Context, channel: discord.TextChannel):
        await self._change_ignore_value(ctx, "channels", channel.id, "add", "channels")

    @prefix_ignore_channel.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_channel_remove(self, ctx: commands.Context, channel: discord.TextChannel):
        await self._change_ignore_value(ctx, "channels", channel.id, "remove", "channels")

    @prefix_ignore_channel.command(name="show")
    async def prefix_ignore_channel_show(self, ctx: commands.Context):
        await self._show_ignore_values(ctx, "channels", "Channels", mentions=True)

    @prefix_ignore.group(name="user", invoke_without_command=True, case_insensitive=True)
    async def prefix_ignore_user(self, ctx: commands.Context):
        await self._send_ignore_help(ctx, "Zyven Ignore User Group", [
            (f"{settings.prefix}ignore user add <user>", "Adds a user to the ignore list."),
            (f"{settings.prefix}ignore user remove <user>", "Removes a user from the ignore list."),
            (f"{settings.prefix}ignore user show", "Displays the list of ignored users."),
        ])

    @prefix_ignore_user.command(name="help")
    async def prefix_ignore_user_help(self, ctx: commands.Context):
        await self.prefix_ignore_user(ctx)

    @prefix_ignore_user.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_user_add(self, ctx: commands.Context, user: discord.User):
        await self._change_ignore_value(ctx, "users", user.id, "add", "users")

    @prefix_ignore_user.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_user_remove(self, ctx: commands.Context, user: discord.User):
        await self._change_ignore_value(ctx, "users", user.id, "remove", "users")

    @prefix_ignore_user.command(name="show")
    async def prefix_ignore_user_show(self, ctx: commands.Context):
        await self._show_ignore_values(ctx, "users", "Users", mentions=True)

    @prefix_ignore.group(name="bypass", invoke_without_command=True, case_insensitive=True)
    async def prefix_ignore_bypass(self, ctx: commands.Context):
        await self._send_ignore_help(ctx, "Zyven Ignore Bypass Group", [
            (f"{settings.prefix}ignore bypass remove <user>", "Removes a user from the bypass list."),
            (f"{settings.prefix}ignore bypass add <user>", "Adds a user to the bypass list."),
            (f"{settings.prefix}ignore bypass show", "Displays the list of bypassed users."),
        ])

    @prefix_ignore_bypass.command(name="help")
    async def prefix_ignore_bypass_help(self, ctx: commands.Context):
        await self.prefix_ignore_bypass(ctx)

    @prefix_ignore_bypass.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_bypass_add(self, ctx: commands.Context, user: discord.User):
        await self._change_ignore_value(ctx, "bypass_users", user.id, "add", "bypass users")

    @prefix_ignore_bypass.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def prefix_ignore_bypass_remove(self, ctx: commands.Context, user: discord.User):
        await self._change_ignore_value(ctx, "bypass_users", user.id, "remove", "bypass users")

    @prefix_ignore_bypass.command(name="show")
    async def prefix_ignore_bypass_show(self, ctx: commands.Context):
        await self._show_ignore_values(ctx, "bypass_users", "Bypass Users", mentions=True)

    async def capture_recovery_snapshot(self, guild: discord.Guild, *, force: bool = False) -> bool:
        # Snapshot writes can be requested by several gateway events at once.
        # Serialize by guild so a burst cannot occupy the DB pool with
        # duplicate full-server snapshots concurrently.
        async with self.recovery_snapshot_locks[guild.id]:
            return await self._capture_recovery_snapshot_locked(guild, force=force)

    async def _capture_recovery_snapshot_locked(self, guild: discord.Guild, *, force: bool = False) -> bool:
        """Persist restorable role/channel state for the opt-in recovery feature."""
        try:
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
            if (not config.get("anti_nuke") or not config.get("auto_recovery")) and not force:
                return False
            if not force and config.get("recovery_snapshot"):
                return True
            previous_snapshot = config.get("recovery_snapshot", {})
            # Role.members scans the entire guild member cache separately for
            # every role and calls a sorted-list membership check per member.
            # On large servers this is O(roles * members) synchronous work on
            # the gateway loop, delaying heartbeats and causing reconnects.
            # Build the same membership map in one pass instead.
            live_roles = [role for role in guild.roles if not role.is_default() and not role.managed]
            live_role_ids = {role.id for role in live_roles}
            members_by_role: dict[int, list[int]] = {role_id: [] for role_id in live_role_ids}
            for member in tuple(guild.members):
                for member_role in member.roles:
                    role_members = members_by_role.get(member_role.id)
                    if role_members is not None:
                        role_members.append(member.id)
            roles = [
                {"id": role.id, "name": role.name, "permissions": role.permissions.value,
                 "colour": role.colour.value, "hoist": role.hoist, "mentionable": role.mentionable,
                 "position": role.position, "member_ids": members_by_role.get(role.id, [])}
                for role in live_roles
            ]
            missing_roles = [
                dict(saved) for saved in previous_snapshot.get("roles", [])
                if int(saved.get("id", 0)) not in live_role_ids
            ]
            roles.extend(missing_roles[-50:])
            channels = []
            live_channel_ids = {channel.id for channel in guild.channels}
            live_category_ids = {
                channel.id for channel in guild.channels
                if isinstance(channel, discord.CategoryChannel)
            }
            previous_channels = {
                int(item.get("id", 0)): item
                for item in previous_snapshot.get("channels", [])
            }
            for channel in guild.channels:
                if isinstance(channel, discord.CategoryChannel):
                    kind = "category"
                elif isinstance(channel, discord.ForumChannel):
                    kind = "forum"
                elif isinstance(channel, discord.StageChannel):
                    kind = "stage"
                elif isinstance(channel, discord.TextChannel):
                    kind = "text"
                elif isinstance(channel, discord.VoiceChannel):
                    kind = "voice"
                else:
                    continue
                overwrites = []
                for target, overwrite in channel.overwrites.items():
                    allow, deny = overwrite.pair()
                    overwrites.append({
                        "id": target.id,
                        "kind": "role" if isinstance(target, discord.Role) else "member",
                        "allow": allow.value,
                        "deny": deny.value,
                    })
                item = {
                    "id": channel.id, "name": channel.name, "kind": kind,
                    "category_id": channel.category_id, "position": channel.position,
                    "overwrites": overwrites,
                }
                previous = previous_channels.get(channel.id)
                if (
                    previous and previous.get("category_id")
                    and int(previous["category_id"]) not in live_category_ids
                ):
                    # A category deletion can emit child-channel update events
                    # before its delete handler finishes. Keep the saved parent
                    # ID so Auto Recovery can reconnect those channels later.
                    item["category_id"] = previous["category_id"]
                if isinstance(channel, discord.TextChannel):
                    item.update(topic=channel.topic, nsfw=channel.nsfw, slowmode_delay=channel.slowmode_delay)
                elif isinstance(channel, discord.ForumChannel):
                    item.update(
                        topic=channel.topic, nsfw=channel.nsfw,
                        slowmode_delay=channel.slowmode_delay,
                        default_auto_archive_duration=channel.default_auto_archive_duration,
                        default_thread_slowmode_delay=channel.default_thread_slowmode_delay,
                        media=channel.type == discord.ChannelType.media,
                        available_tags=[
                            {"name": tag.name, "moderated": tag.moderated,
                             "emoji": str(tag.emoji) if tag.emoji else None}
                            for tag in channel.available_tags
                        ],
                    )
                elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    item.update(bitrate=channel.bitrate, user_limit=channel.user_limit)
                channels.append(item)
            missing_channels = [
                dict(saved) for saved in previous_snapshot.get("channels", [])
                if int(saved.get("id", 0)) not in live_channel_ids
            ]
            channels.extend(missing_channels[-50:])
            await self.bot.db.set_nested(guild.id, "security", "recovery_snapshot", {"roles": roles, "channels": channels})
            return True
        except Exception as error:
            if type(error).__name__ == "PoolTimeout":
                now = time.monotonic()
                if now - self._snapshot_pool_warning_at.get(guild.id, 0.0) >= 60:
                    self._snapshot_pool_warning_at[guild.id] = now
                    log.warning("Skipping antinuke snapshot for guild %s because the database pool is busy; it will retry on the next security event", guild.id)
            else:
                log.exception("Could not capture antinuke recovery snapshot for guild %s", guild.id)
            return False

    async def capture_if_recovery_enabled(self, guild: discord.Guild):
        if guild.id in self.recovery_in_progress:
            return
        try:
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
            if config.get("anti_nuke") and config.get("auto_recovery"):
                # Channel/role/member events can arrive in large bursts. Debounce
                # full-server snapshots off the gateway callback so recovery
                # bookkeeping cannot hold up heartbeats or other commands.
                # Debounce by moving a deadline instead of cancelling the
                # previous task. Cancelling it during a database write can
                # leave a transaction half-finished and poison a pooled conn.
                self.recovery_snapshot_deadlines[guild.id] = self.bot.loop.time() + 1.0
                previous = self.recovery_snapshot_tasks.get(guild.id)
                if previous and not previous.done():
                    return
                task = self.bot.loop.create_task(self._delayed_recovery_snapshot(guild))
                self.recovery_snapshot_tasks[guild.id] = task
        except Exception:
            log.exception("Could not refresh antinuke recovery snapshot for guild %s", guild.id)

    async def _delayed_recovery_snapshot(self, guild: discord.Guild) -> None:
        try:
            while True:
                deadline = self.recovery_snapshot_deadlines.get(guild.id, self.bot.loop.time())
                await asyncio.sleep(max(0.0, deadline - self.bot.loop.time()))
                # A new gateway event moved the debounce deadline while we
                # were waiting; wait again and keep only the latest snapshot.
                if self.recovery_snapshot_deadlines.get(guild.id) != deadline:
                    continue
                if guild.id not in self.recovery_in_progress and self.bot.get_guild(guild.id) is not None:
                    await self.capture_recovery_snapshot(guild, force=True)
                # Events received during the database write schedule one more
                # pass after it completes; they never cancel an active query.
                if self.recovery_snapshot_deadlines.get(guild.id) == deadline:
                    break
        finally:
            task = self.recovery_snapshot_tasks.get(guild.id)
            if task is asyncio.current_task():
                self.recovery_snapshot_tasks.pop(guild.id, None)
                self.recovery_snapshot_deadlines.pop(guild.id, None)

    async def recovery_overwrites(self, guild: discord.Guild, records: list[dict]) -> dict:
        overwrites = {}
        for record in records:
            target_id = int(record["id"])
            if record["kind"] == "role":
                target = guild.get_role(target_id)
            else:
                target = guild.get_member(target_id)
                if target is None:
                    try:
                        target = await guild.fetch_member(target_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        target = None
            if target is None:
                continue
            allow = discord.Permissions(int(record["allow"]))
            deny = discord.Permissions(int(record["deny"]))
            overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        return overwrites

    async def recover_deleted_channel(self, channel: discord.abc.GuildChannel, actor) -> str | None:
        guild = channel.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        snapshot = config.get("recovery_snapshot", {})
        saved = next((item for item in snapshot.get("channels", []) if item.get("id") == channel.id), None)
        if not saved:
            log.warning("No channel recovery snapshot exists for channel %s in guild %s", channel.id, guild.id)
            return "Recovery skipped: no saved channel snapshot was available."
        if saved.get("kind") not in {"category", "text", "voice", "stage", "forum"}:
            return "Recovery skipped: this channel type is not supported by the saved recovery format."
        category = guild.get_channel(saved.get("category_id")) if saved.get("category_id") else None
        overwrites = await self.recovery_overwrites(guild, saved.get("overwrites", []))
        self.recovery_in_progress.add(guild.id)
        try:
            if saved["kind"] == "category":
                restored = await guild.create_category(saved["name"], overwrites=overwrites, position=saved.get("position", 0), reason="Zyven antinuke auto-recovery")
            elif saved["kind"] == "text":
                restored = await guild.create_text_channel(
                    saved["name"], category=category, overwrites=overwrites, position=saved.get("position", 0),
                    topic=saved.get("topic"), nsfw=saved.get("nsfw", False),
                    slowmode_delay=saved.get("slowmode_delay", 0), reason="Zyven antinuke auto-recovery",
                )
            elif saved["kind"] == "voice":
                restored = await guild.create_voice_channel(
                    saved["name"], category=category, overwrites=overwrites, position=saved.get("position", 0),
                    bitrate=saved.get("bitrate"), user_limit=saved.get("user_limit", 0),
                    reason="Zyven antinuke auto-recovery",
                )
            elif saved["kind"] == "stage":
                restored = await guild.create_stage_channel(
                    saved["name"], category=category, overwrites=overwrites,
                    position=saved.get("position", 0), bitrate=saved.get("bitrate"),
                    user_limit=saved.get("user_limit", 0), reason="Zyven antinuke auto-recovery",
                )
            elif saved["kind"] == "forum":
                tags = []
                for tag in saved.get("available_tags", []):
                    emoji = tag.get("emoji")
                    if emoji and emoji.startswith("<"):
                        emoji = discord.PartialEmoji.from_str(emoji)
                    tags.append(discord.ForumTag(name=tag["name"], moderated=tag.get("moderated", False), emoji=emoji))
                restored = await guild.create_forum(
                    saved["name"], category=category, overwrites=overwrites,
                    position=saved.get("position", 0), topic=saved.get("topic"),
                    nsfw=saved.get("nsfw", False), slowmode_delay=saved.get("slowmode_delay", 0),
                    default_auto_archive_duration=saved.get("default_auto_archive_duration", 1440),
                    default_thread_slowmode_delay=saved.get("default_thread_slowmode_delay", 0),
                    available_tags=tags, media=saved.get("media", False),
                    reason="Zyven antinuke auto-recovery",
                )
            else:
                return None
        except (discord.Forbidden, discord.HTTPException):
            self.recovery_in_progress.discard(guild.id)
            log.exception("Could not recover deleted channel %s in guild %s", channel.id, guild.id)
            return "Recovery failed: Zyven needs Manage Channels and permission to apply the saved overrides."
        failed_children = []
        if saved["kind"] == "category":
            for item in snapshot.get("channels", []):
                if item.get("category_id") != channel.id:
                    continue
                child = guild.get_channel(int(item.get("id", 0)))
                if child is None:
                    continue
                try:
                    await child.edit(category=restored, reason="Zyven antinuke auto-recovery: restore deleted category")
                except (discord.Forbidden, discord.HTTPException):
                    failed_children.append(item.get("name", str(item.get("id"))))
                    log.exception("Could not restore channel %s to recovered category %s in guild %s", child.id, restored.id, guild.id)
        updated_snapshot = dict(snapshot)
        updated_channels = []
        for item in snapshot.get("channels", []):
            updated = dict(item)
            if item.get("id") == channel.id:
                updated["id"] = restored.id
            if saved["kind"] == "category" and item.get("category_id") == channel.id:
                updated["category_id"] = restored.id
            updated_channels.append(updated)
        updated_snapshot["channels"] = updated_channels
        try:
            await self.bot.db.set_nested(guild.id, "security", "recovery_snapshot", updated_snapshot)
        except Exception:
            log.exception("Recovered channel %s but could not update its recovery snapshot", channel.id)
        finally:
            self.recovery_in_progress.discard(guild.id)
        result = f"Recovered deleted channel as {restored.mention}."
        if failed_children:
            result += f" Could not move {len(failed_children)} child channel(s) back into the category: {', '.join(failed_children[:5])}."
        return result

    async def recover_deleted_role(self, role: discord.Role, actor) -> str | None:
        guild = role.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        snapshot = config.get("recovery_snapshot", {})
        saved = next((item for item in snapshot.get("roles", []) if item.get("id") == role.id), None)
        if not saved:
            log.warning("No role recovery snapshot exists for role %s in guild %s", role.id, guild.id)
            return "Recovery skipped: no saved role snapshot was available."
        self.recovery_in_progress.add(guild.id)
        try:
            restored = await guild.create_role(
                name=saved["name"], permissions=discord.Permissions(int(saved["permissions"])),
                colour=discord.Colour(int(saved["colour"])), hoist=saved.get("hoist", False),
                mentionable=saved.get("mentionable", False), reason="Zyven antinuke auto-recovery",
            )
        except (discord.Forbidden, discord.HTTPException):
            self.recovery_in_progress.discard(guild.id)
            log.exception("Could not recreate deleted role %s in guild %s", role.id, guild.id)
            return "Recovery failed: Zyven needs Manage Roles to recreate the saved role."
        position_restored = True
        permissions_restored = True
        try:
            await restored.edit(position=saved.get("position", 1), reason="Zyven antinuke auto-recovery")
        except (discord.Forbidden, discord.HTTPException):
            position_restored = False
            log.exception("Recreated role %s but could not restore its position in guild %s", role.id, guild.id)
        if saved["name"] in {"Zyven Prime Security™", "Zyven Unbypassable Security™"} and guild.me:
            try:
                await guild.me.add_roles(restored, reason="Reassign recovered antinuke security role")
            except (discord.Forbidden, discord.HTTPException):
                permissions_restored = False
                log.exception("Recreated security role %s but could not assign it to the bot in guild %s", role.id, guild.id)
        for channel_state in snapshot.get("channels", []):
            channel = guild.get_channel(int(channel_state.get("id", 0)))
            if channel is None:
                continue
            record = next((entry for entry in channel_state.get("overwrites", []) if entry.get("kind") == "role" and entry.get("id") == role.id), None)
            if record:
                allow = discord.Permissions(int(record["allow"]))
                deny = discord.Permissions(int(record["deny"]))
                try:
                    await channel.set_permissions(restored, overwrite=discord.PermissionOverwrite.from_pair(allow, deny), reason="Restore permissions for recovered role")
                except (discord.Forbidden, discord.HTTPException):
                    permissions_restored = False
                    log.exception("Recreated role %s but could not restore its channel overwrite in %s", role.id, channel.id)
        restored_members = 0
        failed_members = 0
        for member_id in saved.get("member_ids", []):
            member = guild.get_member(int(member_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    failed_members += 1
                    continue
            try:
                await member.add_roles(restored, reason="Restore membership for recovered role")
                restored_members += 1
            except (discord.Forbidden, discord.HTTPException):
                failed_members += 1
                log.exception("Recreated role %s but could not restore membership for member %s in guild %s", role.id, member.id, guild.id)
        updated_snapshot = dict(snapshot)
        updated_snapshot["roles"] = [
            {**item, "id": restored.id} if item.get("id") == role.id else dict(item)
            for item in snapshot.get("roles", [])
        ]
        updated_channels = []
        for channel_state in snapshot.get("channels", []):
            item = dict(channel_state)
            item["overwrites"] = [
                {**record, "id": restored.id} if record.get("kind") == "role" and record.get("id") == role.id else dict(record)
                for record in channel_state.get("overwrites", [])
            ]
            updated_channels.append(item)
        updated_snapshot["channels"] = updated_channels
        try:
            await self.bot.db.set_nested(guild.id, "security", "recovery_snapshot", updated_snapshot)
        except Exception:
            log.exception("Recovered role %s but could not update its recovery snapshot", role.id)
        finally:
            self.recovery_in_progress.discard(guild.id)
        details = []
        if not position_restored:
            details.append("original role position could not be restored")
        if not permissions_restored:
            details.append("the bot assignment or some channel overrides could not be restored")
        if restored_members:
            details.append(f"restored to {restored_members} member(s)")
        if failed_members:
            details.append(f"could not restore membership for {failed_members} member(s)")
        suffix = f" ({'; '.join(details)})." if details else "."
        return f"Recovered deleted role as {restored.mention}{suffix}"

    async def recover_created_channel(self, channel: discord.abc.GuildChannel, actor) -> str | None:
        guild = channel.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        self.recovery_in_progress.add(guild.id)
        try:
            await channel.delete(reason="Zyven antinuke auto-recovery: undo unauthorized channel creation")
            await self.capture_recovery_snapshot(guild, force=True)
            return "Removed the newly created channel after its antinuke threshold was reached."
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not remove newly created channel %s in guild %s", channel.id, guild.id)
            return "Recovery failed: Zyven needs Manage Channels to remove the new channel."
        finally:
            self.recovery_in_progress.discard(guild.id)

    async def recover_created_role(self, role: discord.Role, actor) -> str | None:
        guild = role.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        self.recovery_in_progress.add(guild.id)
        try:
            await role.delete(reason="Zyven antinuke auto-recovery: undo unauthorized role creation")
            await self.capture_recovery_snapshot(guild, force=True)
            return "Removed the newly created role after its antinuke threshold was reached."
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not remove newly created role %s in guild %s", role.id, guild.id)
            return "Recovery failed: Zyven needs Manage Roles to remove the new role."
        finally:
            self.recovery_in_progress.discard(guild.id)

    async def recover_updated_channel(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel, actor) -> str | None:
        guild = after.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        fields = {"name": before.name, "position": before.position, "overwrites": before.overwrites}
        if not isinstance(before, discord.CategoryChannel):
            fields["category"] = before.category
        if isinstance(before, discord.TextChannel):
            fields.update(topic=before.topic, nsfw=before.nsfw, slowmode_delay=before.slowmode_delay)
        elif isinstance(before, discord.ForumChannel):
            fields.update(topic=before.topic, nsfw=before.nsfw, slowmode_delay=before.slowmode_delay,
                          default_auto_archive_duration=before.default_auto_archive_duration,
                          default_thread_slowmode_delay=before.default_thread_slowmode_delay,
                          available_tags=before.available_tags)
        elif isinstance(before, discord.VoiceChannel):
            fields.update(bitrate=before.bitrate, user_limit=before.user_limit)
        self.recovery_in_progress.add(guild.id)
        try:
            await after.edit(**fields, reason="Zyven antinuke auto-recovery: restore saved channel settings")
            await self.capture_recovery_snapshot(guild, force=True)
            return f"Restored the updated channel settings for {after.mention}."
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not restore channel update %s in guild %s", after.id, guild.id)
            return "Recovery failed: Zyven needs Manage Channels to restore the saved settings."
        finally:
            self.recovery_in_progress.discard(guild.id)

    async def recover_updated_role(self, before: discord.Role, after: discord.Role, actor) -> str | None:
        guild = after.guild
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke") or not config.get("auto_recovery") or actor.id in normalized_id_set(config.get("extraowners")):
            return None
        self.recovery_in_progress.add(guild.id)
        try:
            await after.edit(name=before.name, permissions=before.permissions, colour=before.colour,
                             hoist=before.hoist, mentionable=before.mentionable, position=before.position,
                             reason="Zyven antinuke auto-recovery: restore saved role settings")
            await self.capture_recovery_snapshot(guild, force=True)
            return f"Restored the updated role settings for {after.mention}."
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not restore role update %s in guild %s", after.id, guild.id)
            return "Recovery failed: Zyven needs Manage Roles to restore the saved settings."
        finally:
            self.recovery_in_progress.discard(guild.id)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
            # Older deployments may have persisted Antinuke as active without
            # an explicit opt-in marker. Do not silently carry that state into
            # the opt-in-only behavior; require the owner to enable it again.
            if config.get("anti_nuke") and not config.get("anti_nuke_opt_in_confirmed"):
                await self.bot.db.set_values(guild.id, {
                    "security.anti_nuke": False,
                    "security.auto_recovery": False,
                    "security.anti_nuke_opt_in_confirmed": False,
                })
                config["anti_nuke"] = False
                config["auto_recovery"] = False
                self.invite_use_snapshots.pop(guild.id, None)
                self.invite_snapshots_ready.discard(guild.id)
                log.warning(
                    "Disabled legacy Antinuke state in guild %s because it had no explicit opt-in marker; owner can enable it again",
                    guild.id,
                )
            elif not config.get("anti_nuke") and config.get("auto_recovery"):
                await self.bot.db.set_values(guild.id, {
                    "security.auto_recovery": False,
                    "security.anti_nuke_opt_in_confirmed": False,
                })
                config["auto_recovery"] = False
            if guild.id not in self.invite_snapshots_ready:
                if config.get("anti_nuke") and "member_role_update" in config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS):
                    self.bot.loop.create_task(self._prime_invite_snapshot(guild))
            await self.capture_recovery_snapshot(guild)

    @app_commands.command(name="antinuke", description="Open the interactive antinuke setup panel")
    @app_commands.guild_only()
    async def antinuke(self, interaction: discord.Interaction):
        data = await self.bot.db.get_guild(interaction.guild_id)
        if not can_access_security(interaction, data):
            await send_security_access_denied(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        parent = CategoryActions(self.bot, interaction.guild_id, settings.owner_id, "security")
        parent.cached_data = data
        parent.overview_entry = True
        enabled = data.get("security", {}).get("anti_nuke", False)
        description = "Prevent harmful activities with active monitoring."
        embed = panel("Configure Antinuke", description)
        embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=True)
        view = FeatureSetupView(parent, "anti_nuke", "Antinuke", description, show_advanced=True)
        await followup_component_card(interaction, embed, view, ephemeral=True)

    @commands.command(name="antinuke", aliases=("antinukesetup",))
    @commands.guild_only()
    async def prefix_antinuke(self, ctx: commands.Context, action: str | None = None):
        if action:
            action = action.casefold()
            if action in {"limit", "limits"}:
                await self.send_antinuke_limit_menu(ctx)
                return
            if action in {"punishment", "punishments", "punish"}:
                data = await self.bot.db.get_guild(ctx.guild.id)
                if not is_security_actor(ctx.guild, ctx.author.id, data):
                    await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
                    return
                await self.send_antinuke_punishment_menu(ctx, data)
                return
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.reply(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), mention_author=False, delete_after=5)
            return
        parent = CategoryActions(self.bot, ctx.guild.id, settings.owner_id, "security")
        parent.cached_data = data
        parent.overview_entry = True
        enabled = data.get("security", {}).get("anti_nuke", False)
        description = "Prevent harmful activities with active monitoring."
        embed = panel("Configure Antinuke", description)
        embed.add_field(name="Current Status", value="Enabled" if enabled else "Disabled", inline=True)
        view = FeatureSetupView(parent, "anti_nuke", "Antinuke", description, show_advanced=True)
        await ctx.send(view=component_card(embed, view))

    def build_antinuke_feature_view(self, ctx: commands.Context, data: dict) -> FeatureSetupView:
        parent = CategoryActions(self.bot, ctx.guild.id, settings.owner_id, "security")
        parent.overview_entry = True
        view = FeatureSetupView(parent, "anti_nuke", "Antinuke", "Prevent harmful activities with active monitoring.", show_advanced=True)
        view.cached_data = data
        return view

    async def send_antinuke_limit_menu(self, ctx: commands.Context):
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return
        feature_view = self.build_antinuke_feature_view(ctx, data)
        await ctx.send(view=component_card(antinuke_limit_menu_embed(), EventLimitMenuView(feature_view)))

    async def send_antinuke_punishment_menu(self, ctx: commands.Context, data: dict):
        feature_view = self.build_antinuke_feature_view(ctx, data)
        await ctx.send(view=component_card(antinuke_punishment_menu_embed(), AntinukePunishmentMenuView(feature_view)))

    async def send_antinuke_limit_summary(self, ctx: commands.Context, *, page: int = 0):
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return
        feature_view = self.build_antinuke_feature_view(ctx, data)
        view = EventLimitView(feature_view, page=page)
        await ctx.send(view=component_card(event_limit_page_embed(feature_view, view.page), view))

    @commands.command(name="antinukeconfig", aliases=("antinuke_config",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_antinukeconfig(self, ctx: commands.Context):
        """Show the paginated antinuke limit and punishment summary."""
        await self.send_antinuke_limit_summary(ctx)

    @commands.command(name="antinukelimit", case_insensitive=True)
    @commands.guild_only()
    async def prefix_antinukelimit(self, ctx: commands.Context, event: str | None = None, limit: int | None = None):
        """View or set the number of event actions allowed per 10 seconds."""
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return
        config = data.get("security", {})
        limits = config.get("anti_nuke_limits", {})
        if event is None:
            await self.send_antinuke_limit_menu(ctx)
            return
        key = ANTINUKE_EVENT_ALIASES.get(event.lower().replace("-", "_").replace(" ", "_"))
        if key is None:
            await ctx.send(view=component_card(notice_panel(f"Unknown event. Use an event key such as `{settings.prefix}antinukelimit channel_delete 3`.", title="Invalid Antinuke Event")))
            return
        if limit is None:
            await ctx.send(view=component_card(panel(f"{icon('security')} {ANTINUKE_EVENT_LABELS[key]} Limit", f"Current threshold: **{limits.get(key, 3)} actions in 10 seconds**.\nSet it with `{settings.prefix}antinukelimit {key} <1-50>`.")))
            return
        if not 1 <= limit <= 50:
            await ctx.send(view=component_card(notice_panel("Choose a limit from **1 to 50** actions per 10 seconds.", title="Invalid Limit")))
            return
        await self.bot.db.set_nested(ctx.guild.id, "security", f"anti_nuke_limits.{key}", limit)
        await ctx.send(view=component_card(panel(f"{icon('tick')} Antinuke Limit Updated", f"**{ANTINUKE_EVENT_LABELS[key]}:** `{limit}` actions per 10 seconds.")))

    @commands.command(name="antinukepunishment", aliases=("antinuke_punishment",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_antinukepunishment(self, ctx: commands.Context, event: str | None = None, punishment: str | None = None):
        """View or set the response used when an event limit is reached."""
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return
        config = data.get("security", {})
        responses = config.get("anti_nuke_punishments", {})
        if event is None:
            await self.send_antinuke_punishment_menu(ctx, data)
            return
        key = ANTINUKE_EVENT_ALIASES.get(event.lower().replace("-", "_").replace(" ", "_"))
        if key is None:
            await ctx.send(view=component_card(notice_panel(f"Unknown event. Use `{settings.prefix}antinukepunishment channel_delete kick`.", title="Invalid Antinuke Event")))
            return
        if punishment is None:
            current = responses.get(key, "ban")
            await ctx.send(view=component_card(panel(f"{icon('security')} {ANTINUKE_EVENT_LABELS[key]} Response", f"Current response: **{current.title()}** (log only, kick, or ban).")))
            return
        punishment = punishment.lower()
        if punishment not in {"none", "log", "kick", "ban"}:
            await ctx.send(view=component_card(notice_panel("Choose `log`, `kick`, or `ban`.", title="Invalid Punishment")))
            return
        normalized = "none" if punishment == "log" else punishment
        await self.bot.db.set_nested(ctx.guild.id, "security", f"anti_nuke_punishments.{key}", normalized)
        await ctx.send(view=component_card(panel(f"{icon('tick')} Antinuke Response Updated", f"**{ANTINUKE_EVENT_LABELS[key]}:** **{punishment.title()}** when the limit is reached.")))

    @commands.command(name="antinukereset", case_insensitive=True)
    @commands.guild_only()
    async def prefix_antinukereset(self, ctx: commands.Context):
        """Restore default event limits and Ban responses."""
        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return
        limits = {key: 3 for key in ANTINUKE_EVENT_LABELS}
        responses = {key: "ban" for key in ANTINUKE_EVENT_LABELS}
        await self.bot.db.set_values(ctx.guild.id, {
            "security.anti_nuke_limits": limits,
            "security.anti_nuke_punishments": responses,
        })
        await ctx.send(view=component_card(panel(f"{icon('tick')} Antinuke Reset", "All event limits are back to **3 actions in 10 seconds** and every event response is set to **Ban**.")))

    @commands.command(name="whitelist", aliases=("wl",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_whitelist(self, ctx: commands.Context, action: str | None = None, member: discord.Member | None = None, event: str | None = None):
        """Manage full and per-event Antinuke whitelist exemptions."""
        # Some mention formats (especially bot mentions) can fail Member
        # conversion and leave an optional argument empty. Resolve the raw
        # Discord mention ID against the current guild as a fallback.
        mentioned_id = None
        if member is None and ctx.message.mentions:
            member = next((user for user in ctx.message.mentions if isinstance(user, discord.Member)), None)
        if member is None:
            # Accept either a Discord mention or a pasted snowflake ID. The
            # normal Member converter handles most cases; this is the fallback
            # for optional arguments and bot mentions that don't convert.
            target_match = re.search(r"<@!?(\d{15,22})>|(?<!\d)(\d{15,22})(?!\d)", ctx.message.content)
            if target_match:
                mentioned_id = int(target_match.group(1) or target_match.group(2))
                member = ctx.guild.get_member(mentioned_id)
                if member is None:
                    try:
                        member = await ctx.guild.fetch_member(mentioned_id)
                    except discord.NotFound:
                        pass
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning("Could not resolve whitelist target %s in guild %s", mentioned_id, ctx.guild.id)
        if action is None:
            # Keep the bare command as a discoverable group menu. The actual
            # whitelist listing remains available with `whitelist show`.
            from cogs.helpdesk import HelpPagesView

            prefix = ctx.prefix or settings.prefix
            entries = [
                (f"{prefix}whitelist remove <member>", "Unwhitelist a user from antinuke."),
                (f"{prefix}whitelist reset", "Resets the whitelisted users."),
                (f"{prefix}whitelist show", "Shows the list of whitelisted users."),
                (f"{prefix}whitelist add <member>", "Adds a member to the full Antinuke whitelist."),
                (f"{prefix}whitelist allow/deny <member> <event>", "Changes a single event exemption."),
            ]
            view = HelpPagesView(
                requester_id=ctx.author.id,
                title="Zyven Whitelist Group",
                entries=entries,
                page_size=5,
            ).set_requester(ctx.author.display_name)
            await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
            return

        data = await self.bot.db.get_guild(ctx.guild.id)
        if not is_security_actor(ctx.guild, ctx.author.id, data):
            await ctx.send(view=component_card(notice_panel("Only the Server Owner or Extraowners can access this menu.", title="Access Denied")), delete_after=5)
            return

        security = data.get("security", {})
        trusted = {int(user_id) for user_id in (security.get("whitelist") or [])}
        permission_map = security.get("whitelist_permissions") or {}
        action = action.lower()
        if action in {"add", "config", "configure"} and member is not None:
            granted = set(permission_map.get(str(member.id), permission_map.get(member.id, [])))
            if member.id in trusted:
                granted = {event for _, _, events in WHITELIST_PERMISSION_GROUPS for event in events}
            view = WhitelistConfigView(self.bot, ctx.guild.id, member, granted, data)
            await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
            return
        if action in {"allow", "deny"} and member is not None and event is not None:
            requested = event.lower().replace("-", "_")
            group = next((
                (key, events) for key, label, events in WHITELIST_PERMISSION_GROUPS
                if requested in {key.lower(), label.lower().replace(" ", "_"), *(item.lower() for item in events)}
            ), None)
            if group is None:
                keys = ", ".join(key for key, _, _ in WHITELIST_PERMISSION_GROUPS)
                await ctx.send(view=component_card(notice_panel(f"Unknown whitelist event. Use one of: `{keys}`.", title="Invalid Whitelist Event")))
                return
            _, group_events = group
            if member.id in trusted:
                granted = {item for _, _, events in WHITELIST_PERMISSION_GROUPS for item in events}
            else:
                granted = set(permission_map.get(str(member.id), permission_map.get(member.id, [])))
            if action == "allow":
                granted.update(group_events)
            else:
                granted.difference_update(group_events)
            trusted.discard(member.id)
            if granted:
                permission_map[str(member.id)] = sorted(granted)
            else:
                permission_map.pop(str(member.id), None)
                permission_map.pop(member.id, None)
            await self.bot.db.set_values(ctx.guild.id, {
                "security.whitelist": sorted(trusted),
                "security.whitelist_permissions": permission_map,
            })
            view = WhitelistConfigView(self.bot, ctx.guild.id, member, granted, data)
            await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())
            return
        if action in {"show", "list", ""} and member is None:
            user_ids = trusted | {int(user_id) for user_id, events in permission_map.items() if events}
            entries = [f"<@{user_id}>" for user_id in sorted(user_ids)]
            body = f"**Total:** {len(user_ids)} users"
            body += "\n\n" + ("\n".join(entries) if entries else "No members are whitelisted.")
            embed = panel("Whitelisted Users for Current Server", body[:3900])
            await ctx.send(view=component_card(embed))
            return
        if action in {"reset", "clear"} and member is None:
            await self.bot.db.set_values(ctx.guild.id, {
                "security.whitelist": [],
                "security.whitelist_permissions": {},
            })
            await ctx.send(view=component_card(panel(f"{icon('tick')} Whitelist Reset", "All trusted members were removed from the Antinuke whitelist.")))
            return
        if action not in {"all", "remove", "delete", "del"} or member is None:
            if mentioned_id is not None and member is None:
                await ctx.send(view=component_card(notice_panel("That user is not a member of this server, or Discord denied the member lookup.", title="Whitelist Member Not Found")))
                return
            prefix = ctx.prefix or settings.prefix
            usage = f"Use `{prefix}whitelist add @member` for the full whitelist, `{prefix}whitelist config @member` to view exemptions, `{prefix}whitelist allow @member everyone_ping` or `deny` to change one event, `{prefix}whitelist remove @member`, `{prefix}whitelist show`, or `{prefix}whitelist reset`."
            await ctx.send(view=component_card(notice_panel(usage, title="Invalid Whitelist Command")))
            return

        if action == "all":
            trusted.add(member.id)
            permission_map.pop(str(member.id), None)
            permission_map.pop(member.id, None)
            verb = "added to the full Antinuke whitelist"
        else:
            trusted.discard(member.id)
            permission_map.pop(str(member.id), None)
            permission_map.pop(member.id, None)
            verb = "removed from the Antinuke whitelist"
        await self.bot.db.set_values(ctx.guild.id, {
            "security.whitelist": sorted(trusted),
            "security.whitelist_permissions": permission_map,
        })
        await ctx.send(view=component_card(panel(f"{icon('security')} Whitelist Updated", f"{member.mention} was {verb}.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unwhitelist", aliases=("unwl",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_unwhitelist(self, ctx: commands.Context, member: discord.Member):
        await ctx.invoke(self.prefix_whitelist, action="remove", member=member)

    @commands.command(name="whitelisted", case_insensitive=True)
    @commands.guild_only()
    async def prefix_whitelisted(self, ctx: commands.Context):
        await ctx.invoke(self.prefix_whitelist, action="show")

    async def report_security_event(
        self, guild: discord.Guild, action: discord.AuditLogAction, target,
        label: str, event_key: str, *, immediate_admin_ban: bool = False,
    ):
        # A gateway event can finish dispatching after the bot has been removed
        # from its guild. There is no audit log to query in that case.
        if self.bot.get_guild(guild.id) is None:
            log.debug("Skipping antinuke audit lookup for departed guild %s", guild.id)
            return None, False
        # Recovery edits roles and channel overwrites itself. Those gateway
        # events must not trigger another audit-log lookup/recovery cycle.
        if guild.id in self.recovery_in_progress:
            log.debug("Skipping antinuke audit lookup during Zyven recovery in guild %s", guild.id)
            return None, False
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        # Antinuke and its recovery actions must be completely inert when the
        # server has disabled Antinuke, even if old config data left recovery on.
        if not config.get("anti_nuke"):
            return None, False
        enabled_events = config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS)
        event_enabled = event_key in enabled_events
        if not event_enabled:
            return None, False
        target_id = getattr(target, "id", None)
        entry = await self.find_recent_audit_entry(guild, action, target_id=target_id)
        actor = entry.user if entry else None
        member_permissions = config.get("whitelist_permissions") or {}
        event_whitelisted = bool(actor and event_key in member_permissions.get(str(actor.id), member_permissions.get(actor.id, [])))
        if (
            not actor
            or actor.id == self.bot.user.id
            or actor.id == guild.owner_id
            or actor.id in normalized_id_set(config.get("extraowners"))
            or actor.id in normalized_id_set(config.get("whitelist"))
            or event_whitelisted
        ):
            return None, False
        triggered = False
        if config.get("anti_nuke") and event_enabled:
            triggered = await self.report_actor_event(
                guild, actor, target, label, event_key, config,
                immediate_admin_ban=immediate_admin_ban,
            )
        return actor, triggered

    async def find_recent_audit_entry(
        self, guild: discord.Guild, action: discord.AuditLogAction, *,
        target_id: int | None = None, channel_id: int | None = None,
    ) -> discord.AuditLogEntry | None:
        """Use gateway-cached audit entries first; serialize rare REST fallbacks."""
        now = datetime.now(timezone.utc)

        def matches(entry: discord.AuditLogEntry) -> bool:
            if (now - entry.created_at).total_seconds() > 15:
                return False
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                return False
            if channel_id is not None:
                extra = getattr(entry, "extra", None)
                audit_channel = getattr(extra, "channel", None)
                entry_channel_id = (
                    getattr(audit_channel, "id", None)
                    or getattr(extra, "channel_id", None)
                    or getattr(entry.target, "channel_id", None)
                )
                if entry_channel_id != channel_id:
                    return False
            return entry.user is not None

        cached = [
            entry for (guild_id, cached_action, _), entry in tuple(self._recent_audit_entries.items())
            if guild_id == guild.id and cached_action == action and matches(entry)
        ]
        if cached:
            return max(cached, key=lambda item: item.created_at)

        # Different gateway events (role, channel, webhook) can arrive in a
        # burst. Serialize audit-log REST calls per guild and globally, spacing
        # requests so one guild cannot create a bot-wide request storm. The
        # gateway cache above handles normal cases without any REST request.
        async with self._audit_lookup_locks[guild.id]:
            cached = [
                entry for (guild_id, cached_action, _), entry in tuple(self._recent_audit_entries.items())
                if guild_id == guild.id and cached_action == action and matches(entry)
            ]
            if cached:
                return max(cached, key=lambda item: item.created_at)
            async with self._audit_rest_lock:
                guild_elapsed = time.monotonic() - self._audit_last_request.get(guild.id, 0.0)
                global_elapsed = time.monotonic() - self._audit_global_last_request
                wait_for = max(1.0 - guild_elapsed, 0.1 - global_elapsed, 0.0)
                if wait_for:
                    await asyncio.sleep(wait_for)
                cached = [
                    entry for (guild_id, cached_action, _), entry in tuple(self._recent_audit_entries.items())
                    if guild_id == guild.id and cached_action == action and matches(entry)
                ]
                if cached:
                    return max(cached, key=lambda item: item.created_at)
                # A transient Discord DNS/network outage otherwise makes every
                # gateway event queue another audit-log request. Back off
                # globally for a short period and let gateway events continue.
                if time.monotonic() < self._audit_global_unavailable_until:
                    return None
                if self.bot.get_guild(guild.id) is None:
                    return None
                self._audit_last_request[guild.id] = time.monotonic()
                self._audit_global_last_request = self._audit_last_request[guild.id]
                try:
                    async for entry in guild.audit_logs(limit=10, action=action):
                        key = (guild.id, action, getattr(entry.target, "id", 0))
                        self._recent_audit_entries[key] = entry
                        if matches(entry):
                            return entry
                    self._audit_global_unavailable_until = 0.0
                except discord.NotFound as exc:
                    if exc.code != 10004:
                        self._rate_limited_audit_warning(guild.id, "not_found", f"Discord returned 404 reading audit logs in guild {guild.id} (code {exc.code})")
                except discord.Forbidden:
                    self._rate_limited_audit_warning(guild.id, "forbidden", f"Cannot read audit logs in guild {guild.id}; check View Audit Log permission")
                except discord.HTTPException as exc:
                    self._rate_limited_audit_warning(guild.id, "http", f"Audit log lookup failed in guild {guild.id} (HTTP {exc.status}, code {exc.code})")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    self._audit_global_unavailable_until = time.monotonic() + 10.0
                    self._rate_limited_audit_warning(
                        guild.id, "network",
                        f"Audit log lookup temporarily unavailable (Discord network/DNS error); retrying after a short cooldown: {type(exc).__name__}",
                    )
                if len(self._recent_audit_entries) > 5000:
                    cutoff = datetime.now(timezone.utc).timestamp() - 60
                    self._recent_audit_entries = {
                        key: value for key, value in self._recent_audit_entries.items()
                        if value.created_at.timestamp() >= cutoff
                    }
                return None

    def _rate_limited_audit_warning(self, guild_id: int, kind: str, message: str) -> None:
        key = (guild_id, kind)
        now = time.monotonic()
        if now - self._audit_warning_at.get(key, 0.0) >= 30:
            self._audit_warning_at[key] = now
            log.warning("%s", message)

    async def report_recovery(self, guild: discord.Guild, actor, label: str, result: str):
        self.queue_owner_security_dm(guild, actor, label, result)
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        channel = guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
        if channel:
            try:
                color = 0xED4245 if "failed" in result.lower() or "skipped" in result.lower() else 0x57F287
                await channel.send(view=component_card(panel(f"{icon('security')} Auto Recovery", f"**Action:** {label}\n**Executor:** <@{actor.id}>\n**Result:** {result}", color=color)))
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send recovery report in guild %s", guild.id)

    async def report_actor_event(
        self, guild, actor, target, label: str, event_key: str,
        config: dict | None = None, *, immediate_admin_ban: bool = False,
    ):
        if config is None:
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return False
        if event_key not in config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS):
            return False
        if actor.id == self.bot.user.id or actor.id == guild.owner_id:
            return False
        if actor.id in normalized_id_set(config.get("extraowners")):
            return False
        if actor.id in normalized_id_set(config.get("whitelist")):
            return False
        member_permissions = config.get("whitelist_permissions") or {}
        if event_key in member_permissions.get(str(actor.id), member_permissions.get(actor.id, [])):
            return False
        now = datetime.now(timezone.utc).timestamp()
        self.events_since_sweep += 1
        if self.events_since_sweep >= 250:
            self.events_since_sweep = 0
            stale_keys = [key for key, events in self.event_bursts.items() if not events or now - events[-1] > 60]
            for key in stale_keys:
                self.event_bursts.pop(key, None)
                self.event_targets.pop(key, None)
        burst = self.event_bursts[(guild.id, actor.id, event_key)]
        target_burst = self.event_targets[(guild.id, actor.id, event_key)]
        while burst and now - burst[0] > 10:
            burst.popleft()
        while target_burst and now - target_burst[0][0] > 10:
            target_burst.popleft()
        burst.append(now)
        if target is not None:
            target_burst.append((now, target))
        limit = max(1, int(config.get("anti_nuke_limits", {}).get(event_key, 3)))
        # Fire once when a fresh burst reaches the configured limit. Continuing
        # to exceed the limit must not repeatedly punish or log every action.
        if not immediate_admin_ban and len(burst) != limit:
            return False
        punishment = "ban" if immediate_admin_ban else config.get("anti_nuke_punishments", {}).get(event_key, "ban")
        punishment_result = "No automatic punishment configured."
        if punishment in {"kick", "ban"}:
            punishment_result = await self.punish_executor(guild, actor, punishment, label)
        self.queue_owner_security_dm(guild, actor, label, punishment_result)
        channel_id = config.get("log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            if isinstance(target, discord.Message):
                target_label = f"{target.channel.mention} ([open message]({target.jump_url}))"
            else:
                target_label = f"`{target}`" if target is not None else "Server members"
            embed = panel(f"{icon('warning')} Antinuke Security Alert", f"**Action:** {label}\n**Executor:** {actor.mention}\n**Target:** {target_label}\n**Response:** {punishment_result}", color=0xFEE75C)
            try:
                await channel.send(view=component_card(embed))
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send antinuke alert in guild %s", guild.id)
        return True

    async def direct_ban_unlogged_actor(
        self,
        guild: discord.Guild,
        actor,
        target,
        label: str,
        event_key: str,
        config: dict | None = None,
    ) -> tuple[bool, str]:
        """Enforce message/webhook events directly, with no audit-log or burst lookup."""
        if config is None:
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return False, "Antinuke is disabled for this server."
        selected_events = set(config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS))
        # Message/webhook events do not have a dependable audit-log fallback.
        # Keep their direct protection active whenever Antinuke itself is on.
        if event_key in {"everyone_ping", "role_ping", "webhook_spam"}:
            selected_events.add(event_key)
            if selected_events != set(config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS)):
                config["anti_nuke_events"] = sorted(selected_events)
                await self.bot.db.set_nested(guild.id, "security", "anti_nuke_events", sorted(selected_events))
        elif event_key not in selected_events:
            return False, "This antinuke event is disabled."
        if actor is None or actor.id in {self.bot.user.id, guild.owner_id} or actor.id in normalized_id_set(config.get("extraowners")):
            return False, "Actor is the bot, server owner, or an extra owner."
        if actor.id in normalized_id_set(config.get("whitelist")):
            return False, "Actor is whitelisted."
        permissions = config.get("whitelist_permissions") or {}
        if event_key in permissions.get(str(actor.id), permissions.get(actor.id, [])):
            return False, "Actor is whitelisted for this event."

        result = await self.punish_executor(guild, actor, "ban", label)
        self.queue_owner_security_dm(guild, actor, label, result)
        channel = guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
        if channel:
            target_label = f"{target.channel.mention} ([open message]({target.jump_url}))" if isinstance(target, discord.Message) else "Unknown target"
            embed = panel(
                f"{icon('warning')} Antinuke Security Alert",
                f"**Action:** {label}\n**Executor:** <@{actor.id}>\n**Target:** {target_label}\n**Response:** {result}",
                color=0xED4245,
            )
            try:
                await channel.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not send direct antinuke alert in guild %s", guild.id)
        return True, result

    async def punish_executor(self, guild: discord.Guild, actor, punishment: str, label: str) -> str:
        """Apply an explicitly configured kick/ban only when the bot can safely target the executor."""
        member = guild.get_member(actor.id)
        if member is None:
            try:
                member = await guild.fetch_member(actor.id)
            except discord.NotFound:
                if punishment != "ban":
                    return f"Configured {punishment.upper()} could not run: executor is no longer a server member."
                # A webhook creator may not be cached (or may have just left).
                # The User object from the webhook is still a valid ban target.
                member = actor
            except (discord.Forbidden, discord.HTTPException):
                if punishment != "ban":
                    return f"Configured {punishment.upper()} could not verify the executor. Check the bot's server member access."
                # Let Discord decide whether the direct ban is allowed when a
                # member lookup is unavailable; the API still enforces hierarchy.
                member = actor
        bot_member = guild.me
        if member.id == guild.owner_id or member.id == self.bot.user.id:
            return f"Configured {punishment.upper()} was skipped: executor is protected by server ownership or role hierarchy."
        if isinstance(member, discord.Member) and (bot_member is None or member.top_role >= bot_member.top_role):
            return f"Configured {punishment.upper()} was skipped: executor is protected by server ownership or role hierarchy."
        reason = f"Zyven antinuke: threshold reached for {label}"
        try:
            if punishment == "ban":
                await guild.ban(member, reason=reason)
            elif punishment == "kick":
                await member.kick(reason=reason)
            else:
                return "No automatic punishment configured."
        except discord.Forbidden:
            return f"Configured {punishment.upper()} failed: check Zyven's Ban Members permission and role hierarchy."
        except discord.HTTPException:
            log.exception("Antinuke %s failed for executor %s in guild %s", punishment, actor.id, guild.id)
            return f"Configured {punishment.upper()} failed; check Zyven's permissions and logs."
        log.debug("Antinuke applied %s to executor %s in guild %s after %s threshold", punishment, actor.id, guild.id, label)
        return f"Executor was **{punishment.upper()}** by the configured antinuke response."

    async def remove_created_audit_resource(self, entry: discord.AuditLogEntry, event_key: str) -> None:
        """Remove newly-created webhook/sticker resources after antinuke detection."""
        target = safe_audit_target(entry)
        target_id = getattr(target, "id", None) or getattr(entry, "_target_id", None)
        try:
            target_id = int(target_id) if target_id is not None else None
        except (TypeError, ValueError):
            target_id = None
        if not target_id:
            return
        try:
            if event_key == "webhook_create":
                # Audit-log entries normally carry a Webhook target, so use it
                # directly. Fetching every webhook in the guild adds latency
                # and creates needless API work on bursts.
                webhook = target if callable(getattr(target, "delete", None)) else None
                if webhook is None:
                    webhook = discord.utils.get(await entry.guild.webhooks(), id=target_id)
                if webhook:
                    await webhook.delete(reason="Antinuke: remove a webhook created by an unauthorized executor")
            elif event_key == "sticker_create":
                sticker = await entry.guild.fetch_sticker(target_id)
                await entry.guild.delete_sticker(sticker, reason="Antinuke: remove a sticker created by an unauthorized executor")
        except discord.Forbidden:
            log.error(
                "Discord denied deletion of unauthorized %s %s in guild %s; verify Manage Webhooks permission and bot role",
                event_key, target_id, entry.guild.id,
            )
        except discord.HTTPException as exc:
            log.error(
                "Discord API failed to delete unauthorized %s %s in guild %s (HTTP %s, code %s)",
                event_key, target_id, entry.guild.id, exc.status, exc.code,
            )
        except AttributeError:
            log.exception("Could not resolve unauthorized %s %s in guild %s", event_key, target_id, entry.guild.id)

    async def handle_webhook_create_entry(self, entry: discord.AuditLogEntry) -> None:
        """Delete an unauthorized webhook immediately and apply its configured response."""
        entry_id = getattr(entry, "id", None)
        if entry_id is not None:
            if entry_id in self._handled_webhook_audit_ids:
                return
            self._handled_webhook_audit_ids.add(entry_id)
            if len(self._handled_webhook_audit_ids) > 5000:
                # Audit IDs are monotonic; keeping the newest few thousand is
                # enough to suppress duplicate gateway/fallback delivery.
                self._handled_webhook_audit_ids = set(sorted(self._handled_webhook_audit_ids)[-2500:])

        guild, actor = entry.guild, entry.user
        if actor is None:
            return
        target = safe_audit_target(entry)
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return

        actor_id = int(actor.id)
        permissions = config.get("whitelist_permissions") or {}
        event_exemptions = permissions.get(str(actor_id), permissions.get(actor_id, [])) or []
        trusted = set()
        for value in (*config.get("extraowners", []), *config.get("whitelist", [])):
            try:
                trusted.add(int(value))
            except (TypeError, ValueError):
                continue
        if actor_id in trusted or actor_id in {guild.owner_id, self.bot.user.id} or "webhook_create" in event_exemptions:
            return

        # Treat the global Antinuke switch as enabling protection for webhook
        # creation even if an old per-event selection omitted it. Explicitly
        # configured limits/responses are still honored.
        report_config = dict(config)
        report_config["anti_nuke_events"] = list(config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS))
        if "webhook_create" not in report_config["anti_nuke_events"]:
            report_config["anti_nuke_events"].append("webhook_create")
        await self.report_actor_event(
            guild, actor, target, "Webhook created", "webhook_create",
            config=report_config, immediate_admin_ban=True,
        )
        # Ban enforcement runs first so resource deletion/API retries cannot
        # delay action against an unauthorized webhook creator.
        await self.remove_created_audit_resource(entry, "webhook_create")

    async def handle_webhook_delete_entry(self, entry: discord.AuditLogEntry) -> None:
        """Route webhook deletions through the shared antinuke whitelist/punishment flow."""
        guild, actor = entry.guild, entry.user
        if actor is None:
            return
        target = safe_audit_target(entry)
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return

        # Older guild configs may have an explicit event list that predates
        # webhook-delete protection. Keep it active under the global antinuke
        # switch while preserving any configured threshold and punishment.
        report_config = dict(config)
        events = list(config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS))
        if "webhook_delete" not in events:
            events.append("webhook_delete")
        report_config["anti_nuke_events"] = events
        limits = dict(config.get("anti_nuke_limits", {}))
        limits.setdefault("webhook_delete", 1)
        report_config["anti_nuke_limits"] = limits
        await self.report_actor_event(
            guild, actor, target, "Webhook deleted", "webhook_delete", config=report_config
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        """Fallback detector for webhook creates if the audit-entry event is delayed."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return
        # Discord can dispatch the channel webhook event before its audit-log
        # entry is readable. Give the primary audit-entry event a moment to
        # arrive, then do one bounded lookup through the shared cache/limiter.
        await asyncio.sleep(0.75)
        entry = await self.find_recent_audit_entry(
            guild, discord.AuditLogAction.webhook_create, channel_id=channel.id
        )
        if entry:
            await self.handle_webhook_create_entry(entry)

    async def revert_role_position(self, before: discord.Role, after: discord.Role, actor) -> str:
        """Immediately restore an unauthorized role move and ban its executor."""
        position_result = "Role position could not be restored."
        try:
            await after.edit(position=before.position, reason="Zyven antinuke: revert unauthorized role position change")
            position_result = "Role position reverted."
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not revert role position for role %s in guild %s", after.id, after.guild.id)
        ban_result = await self.punish_executor(after.guild, actor, "ban", "unauthorized role position change")
        return f"{position_result} {ban_result}"

    async def handle_webhook_mention(self, message: discord.Message) -> None:
        """Ban a known webhook owner, remove the webhook and hide its channel."""
        guild = message.guild
        if guild is None or message.webhook_id is None:
            return
        details = []
        webhook = None
        owner = None
        config = (await self.bot.db.get_guild(guild.id)).get("security", {})
        try:
            await message.delete()
            details.append("triggering message deleted")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not delete webhook ping message %s in guild %s", message.id, guild.id)
        try:
            webhooks = await guild.webhooks()
            webhook = discord.utils.get(webhooks, id=message.webhook_id)
            if webhook:
                owner = getattr(webhook, "user", None)
                if owner is not None:
                    processed, result = await self.direct_ban_unlogged_actor(
                        guild,
                        owner,
                        message,
                        "@everyone/@here ping sent through a webhook",
                        "webhook_spam",
                        config,
                    )
                    details.append(f"webhook creator: {owner} (`{owner.id}`); {result}")
                else:
                    details.append("webhook creator could not be resolved; member ban was unavailable")
                await webhook.delete(reason="Zyven antinuke: webhook used for an everyone/here ping")
                details.append("webhook deleted")
            else:
                details.append("webhook was already unavailable")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not delete webhook %s in guild %s", message.webhook_id, guild.id)
            details.append("webhook deletion failed; check Manage Webhooks")
        try:
            overwrite = message.channel.overwrites_for(guild.default_role)
            overwrite.view_channel = False
            await message.channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason="Zyven antinuke: hide channel after webhook everyone/here spam",
            )
            details.append("channel hidden from @everyone")
        except (AttributeError, discord.Forbidden, discord.HTTPException):
            log.exception("Could not hide channel %s after webhook spam in guild %s", message.channel.id, guild.id)
            details.append("channel could not be hidden; check Manage Channels")
        self.queue_owner_security_dm(
            guild,
            owner or guild.owner or self.bot.user,
            "Webhook @everyone/@here ping",
            "; ".join(details),
        )
        log_channel = guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
        if log_channel:
            try:
                await log_channel.send(
                    view=component_card(panel(
                        "Webhook Ping Blocked",
                        f"A webhook sent an @everyone/@here ping in {message.channel.mention}.\n"
                        + "\n".join(f"• {item}" for item in details),
                        color=0xED4245,
                    )),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not post webhook spam report in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        actor, _ = await self.report_security_event(channel.guild, discord.AuditLogAction.channel_delete, channel, "Channel deleted", "channel_delete")
        if actor:
            result = await self.recover_deleted_channel(channel, actor)
            if result:
                await self.report_recovery(channel.guild, actor, "Channel deleted", result)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        actor, triggered = await self.report_security_event(channel.guild, discord.AuditLogAction.channel_create, channel, "Channel created", "channel_create")
        if actor and triggered:
            result = await self.recover_created_channel(channel, actor)
            if result:
                await self.report_recovery(channel.guild, actor, "Channel created", result)
            else:
                await self.capture_if_recovery_enabled(channel.guild)
        else:
            await self.capture_if_recovery_enabled(channel.guild)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        actor, triggered = await self.report_security_event(after.guild, discord.AuditLogAction.channel_update, after, "Channel updated", "channel_update")
        if actor and triggered:
            result = await self.recover_updated_channel(before, after, actor)
            if result:
                await self.report_recovery(after.guild, actor, "Channel updated", result)
            else:
                await self.capture_if_recovery_enabled(after.guild)
        else:
            await self.capture_if_recovery_enabled(after.guild)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        actor, _ = await self.report_security_event(role.guild, discord.AuditLogAction.role_delete, role, "Role deleted", "role_delete")
        if actor:
            result = await self.recover_deleted_role(role, actor)
            if result:
                await self.report_recovery(role.guild, actor, "Role deleted", result)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        actor, triggered = await self.report_security_event(role.guild, discord.AuditLogAction.role_create, role, "Role created", "role_create")
        if actor and triggered:
            result = await self.recover_created_role(role, actor)
            if result:
                await self.report_recovery(role.guild, actor, "Role created", result)
            else:
                await self.capture_if_recovery_enabled(role.guild)
        else:
            await self.capture_if_recovery_enabled(role.guild)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.position != after.position:
            changes.append(("role_position", "Role position changed"))
        if any((before.name != after.name, before.permissions != after.permissions, before.colour != after.colour,
                before.hoist != after.hoist, before.mentionable != after.mentionable)):
            changes.append(("role_update", "Role updated"))
        if not before.permissions.embed_links and after.permissions.embed_links:
            changes.append(("role_link_permission", "Role granted link permissions"))
        if not before.permissions.create_instant_invite and after.permissions.create_instant_invite:
            changes.append(("role_invite_permission", "Role granted invite permissions"))
        triggered_actor = None
        for event_key, label in changes:
            actor, triggered = await self.report_security_event(after.guild, discord.AuditLogAction.role_update, after, label, event_key)
            if event_key == "role_position" and actor and triggered:
                result = await self.revert_role_position(before, after, actor)
                await self.report_recovery(after.guild, actor, "Role position changed", result)
                return
            if actor and triggered:
                triggered_actor = actor
        if triggered_actor:
            result = await self.recover_updated_role(before, after, triggered_actor)
            if result:
                await self.report_recovery(after.guild, triggered_actor, "Role security change", result)
        else:
            await self.capture_if_recovery_enabled(after.guild)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        actor, triggered = await self.report_security_event(guild, discord.AuditLogAction.ban, user, "Member banned", "member_ban")
        if actor and triggered:
            key = (guild.id, actor.id, "member_ban")
            recent = list(self.event_targets.pop(key, ()))
            targets = {target.id: target for _, target in recent if hasattr(target, "id")}
            targets.setdefault(user.id, user)
            for banned_user in targets.values():
                try:
                    await guild.unban(banned_user, reason="Antinuke: reverse unauthorized bans in the event burst")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not reverse unauthorized ban of user %s in guild %s", banned_user.id, guild.id)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        actor, triggered = await self.report_security_event(guild, discord.AuditLogAction.unban, user, "Member unbanned", "member_unban")
        if actor and triggered:
            key = (guild.id, actor.id, "member_unban")
            recent = list(self.event_targets.pop(key, ()))
            targets = {target.id: target for _, target in recent if hasattr(target, "id")}
            targets.setdefault(user.id, user)
            for unbanned_user in targets.values():
                try:
                    await guild.ban(unbanned_user, reason="Antinuke: restore unauthorized unbans in the event burst")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not restore unauthorized unban of user %s in guild %s", unbanned_user.id, guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.report_security_event(member.guild, discord.AuditLogAction.kick, member, "Member kicked", "member_kick")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            has_admin = member.guild_permissions.administrator
            actor, triggered = await self.report_security_event(
                member.guild, discord.AuditLogAction.bot_add, member,
                "Bot added with Administrator" if has_admin else "Bot added", "bot_add",
                immediate_admin_ban=has_admin and member.id != self.bot.user.id,
            )
            if actor and triggered:
                if has_admin:
                    try:
                        await member.ban(reason="Antinuke: bot was added with Administrator permission")
                        result = "The added bot was also banned because it received Administrator permission."
                    except (discord.Forbidden, discord.HTTPException):
                        log.exception("Could not ban admin bot %s in guild %s", member.id, member.guild.id)
                        result = "Zyven could not ban the added Administrator bot; check Ban Members and role hierarchy."
                    await self.report_recovery(member.guild, actor, "Administrator bot added", result)
                else:
                    key = (member.guild.id, actor.id, "bot_add")
                    recent = list(self.event_targets.pop(key, ()))
                    targets = {target.id: target for _, target in recent if isinstance(target, discord.Member)}
                    targets.setdefault(member.id, member)
                    for added_bot in targets.values():
                        try:
                            await added_bot.kick(reason="Antinuke: remove bots added in the unauthorized event burst")
                        except (discord.Forbidden, discord.HTTPException):
                            log.exception("Could not remove unauthorized bot %s from guild %s", added_bot.id, member.guild.id)
        elif member.id != member.guild.owner_id and member.guild_permissions.administrator:
            # Discord can grant invite roles in the initial GUILD_MEMBER_ADD
            # payload, without creating an auditable member-role update. Match
            # the invite's role_ids and usage counter instead of audit logs.
            data = await self.bot.db.get_guild(member.guild.id)
            config = data.get("security", {})
            if not config.get("anti_nuke") or "member_role_update" not in config.get("anti_nuke_events", DEFAULT_ANTI_NUKE_EVENTS):
                return

            admin_role_ids = {role.id for role in member.roles if role.permissions.administrator}
            inviter_id = await self._resolve_admin_invite_creator(member, admin_role_ids)
            actor = None
            creator_result = "Invite creator could not be identified from a unique active invite."
            if inviter_id:
                actor = member.guild.get_member(inviter_id) or discord.Object(id=inviter_id)
                if inviter_id == member.guild.owner_id:
                    creator_result = "Invite creator is the server owner, whom Discord does not allow the bot to ban."
                else:
                    # A role-grant invite has no reliable audit-log actor. This
                    # direct path is gated by antinuke/event settings above and
                    # must ban the creator even if ordinary event allowlists exempt them.
                    creator_result = await self.punish_executor(
                        member.guild, actor, "ban", "Administrator role granted through invite"
                    )
                # report_actor_event deliberately exempts trusted creators. The
                # joining account is still handled below and the alert explains
                # whether the creator was exempt or their ban failed.

            bot_member = member.guild.me
            if bot_member is None or not bot_member.guild_permissions.ban_members:
                result = "Zyven could not ban the recipient: it needs Ban Members permission."
            elif member.top_role >= bot_member.top_role:
                result = "Zyven could not ban the recipient: move Zyven's highest role above the granted Administrator role."
            else:
                try:
                    await member.ban(reason="Antinuke: member joined with an unauthorized Administrator invite role")
                    result = "The recipient was banned after joining with Administrator permission."
                except discord.Forbidden:
                    result = "Zyven could not ban the recipient: Discord denied the action. Check Ban Members permission and role hierarchy."
                    log.warning("Discord denied the ban of admin-role recipient %s in guild %s", member.id, member.guild.id)
                except discord.HTTPException as exc:
                    result = f"Zyven could not ban the recipient: Discord returned HTTP {exc.status} (code {exc.code})."
                    log.warning("Could not ban admin-role recipient %s in guild %s: HTTP %s code %s", member.id, member.guild.id, exc.status, exc.code)

            log.warning("Admin-role invite handling for member %s in guild %s: creator=%s; recipient=%s", member.id, member.guild.id, creator_result, result)
            await self.report_recovery(
                member.guild, actor or discord.Object(id=member.id),
                "Administrator role granted through invite",
                f"**Invite creator:** {creator_result}\n**Recipient:** {result}",
            )
            channel = member.guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
            if channel:
                try:
                    await channel.send(view=component_card(panel(
                        f"{icon('warning')} Antinuke Admin Invite Alert",
                        f"**Member:** {member.mention}\n**Invite creator:** {creator_result}\n**Recipient result:** {result}",
                        color=0xED4245,
                    )))
                except (discord.Forbidden, discord.HTTPException):
                    log.info("Could not post admin-role invite alert in guild %s", member.guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        changes = []
        if before.nick != after.nick:
            changes.append((discord.AuditLogAction.member_update, "Member updated", "member_update"))
        if before.roles != after.roles:
            changes.append((discord.AuditLogAction.member_role_update, "Member roles updated", "member_role_update"))
        triggered_actor = None
        admin_role_granted = (
            after.id != after.guild.owner_id
            and after.id != self.bot.user.id
            and after.guild_permissions.administrator
            and any(role not in before.roles and role.permissions.administrator for role in after.roles)
        )
        admin_grant_triggered = False
        for action, label, event_key in changes:
            immediate = admin_role_granted and event_key == "member_role_update"
            actor, triggered = await self.report_security_event(
                after.guild, action, after, "Administrator role granted" if immediate else label,
                event_key, immediate_admin_ban=immediate,
            )
            if actor and triggered:
                triggered_actor = actor
                admin_grant_triggered = immediate
        if triggered_actor and admin_grant_triggered:
            try:
                await after.ban(reason="Antinuke: member received an unauthorized Administrator role")
                result = "The recipient was also banned after receiving Administrator permission."
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not ban admin-role recipient %s in guild %s", after.id, after.guild.id)
                result = "Zyven could not ban the recipient; check Ban Members and role hierarchy."
            await self.report_recovery(after.guild, triggered_actor, "Administrator role granted", result)
            return
        if triggered_actor:
            config = (await self.bot.db.get_guild(after.guild.id)).get("security", {})
            if config.get("auto_recovery"):
                try:
                    await after.edit(
                        nick=before.nick,
                        roles=[role for role in before.roles if not role.is_default()],
                        reason="Antinuke: restore unauthorized member changes",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not restore unauthorized member changes for %s in guild %s", after.id, after.guild.id)
        elif changes:
            await self.capture_if_recovery_enabled(after.guild)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        previous = {emoji.id: emoji for emoji in before}
        current = {emoji.id: emoji for emoji in after}
        for emoji_id in current.keys() - previous.keys():
            emoji = current[emoji_id]
            actor, triggered = await self.report_security_event(guild, discord.AuditLogAction.emoji_create, emoji, "Emoji created", "emoji_create")
            if actor and triggered:
                try:
                    await emoji.delete(reason="Antinuke: remove an emoji created by an unauthorized executor")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not remove unauthorized emoji %s in guild %s", emoji.id, guild.id)
        for emoji_id in previous.keys() - current.keys():
            emoji = previous[emoji_id]
            actor, triggered = await self.report_security_event(guild, discord.AuditLogAction.emoji_delete, emoji, "Emoji deleted", "emoji_delete")
            config = (await self.bot.db.get_guild(guild.id)).get("security", {})
            if actor and triggered and config.get("auto_recovery"):
                try:
                    image = await emoji.url.read()
                    await guild.create_custom_emoji(name=emoji.name, image=image, reason="Antinuke: restore a deleted emoji")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not restore deleted emoji %s in guild %s", emoji.id, guild.id)
        for emoji_id in current.keys() & previous.keys():
            old_emoji, emoji = previous[emoji_id], current[emoji_id]
            if old_emoji.name != emoji.name:
                actor, triggered = await self.report_security_event(guild, discord.AuditLogAction.emoji_update, emoji, "Emoji updated", "emoji_update")
                config = (await self.bot.db.get_guild(guild.id)).get("security", {})
                if actor and triggered and config.get("auto_recovery"):
                    try:
                        await emoji.edit(name=old_emoji.name, reason="Antinuke: restore an unauthorized emoji update")
                    except (discord.Forbidden, discord.HTTPException):
                        log.exception("Could not restore updated emoji %s in guild %s", emoji.id, guild.id)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        await self.report_security_event(after, discord.AuditLogAction.guild_update, after, "Server settings updated", "guild_update")

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        target = safe_audit_target(entry)
        target_id = getattr(target, "id", None) or getattr(entry, "_target_id", None)
        try:
            target_id = int(target_id) if target_id is not None else 0
        except (TypeError, ValueError):
            target_id = 0
        if target_id:
            self._recent_audit_entries[(entry.guild.id, entry.action, target_id)] = entry
        if len(self._recent_audit_entries) > 5000:
            cutoff = datetime.now(timezone.utc).timestamp() - 60
            self._recent_audit_entries = {
                key: value for key, value in self._recent_audit_entries.items()
                if value.created_at.timestamp() >= cutoff
            }
        if not entry.user:
            return
        action_events = {
            discord.AuditLogAction.sticker_create: ("sticker_create", "Sticker created"),
            discord.AuditLogAction.sticker_delete: ("sticker_delete", "Sticker deleted"),
            discord.AuditLogAction.sticker_update: ("sticker_update", "Sticker updated"),
            discord.AuditLogAction.integration_create: ("integration_create", "Integration added"),
            discord.AuditLogAction.integration_delete: ("integration_delete", "Integration removed"),
            discord.AuditLogAction.integration_update: ("integration_update", "Integration updated"),
            discord.AuditLogAction.webhook_create: ("webhook_create", "Webhook created"),
            discord.AuditLogAction.webhook_delete: ("webhook_delete", "Webhook deleted"),
            discord.AuditLogAction.webhook_update: ("webhook_update", "Webhook updated"),
        }
        if entry.action == discord.AuditLogAction.member_prune:
            count = getattr(entry.extra, "members_removed", 0)
            await self.report_actor_event(entry.guild, entry.user, None, f"Member prune ({count} members)", "member_prune")
            return
        event = action_events.get(entry.action)
        if event:
            event_key, label = event
            if event_key == "webhook_create":
                await self.handle_webhook_create_entry(entry)
                return
            if event_key == "webhook_delete":
                await self.handle_webhook_delete_entry(entry)
                return
            config = None
            triggered = await self.report_actor_event(
                entry.guild, entry.user, target, label, event_key, config=config
            )
            if triggered and event_key == "sticker_create":
                await self.remove_created_audit_resource(entry, event_key)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self.inspect_mention_security(after, before=before)

    async def inspect_mention_security(self, message: discord.Message, *, before: discord.Message | None = None):
        if not message.guild:
            return
        contains_everyone_mention = message.mention_everyone and (before is None or not before.mention_everyone)
        if message.webhook_id:
            if contains_everyone_mention:
                config = (await self.bot.db.get_guild(message.guild.id)).get("security", {})
                if config.get("anti_nuke"):
                    await self.handle_webhook_mention(message)
            return
        # Unlike audit-log events, a mention message already identifies its
        # sender. This also covers ordinary bot accounts; only webhook messages
        # need the creator lookup handled above.
        if not isinstance(message.author, discord.Member):
            return
        channel_permissions = message.channel.permissions_for(message.author)
        # Discord can render an @everyone/@here token even when the author
        # cannot notify anyone with it. Treat it as an antinuke event only if
        # the author actually has the channel-level permission to ping.
        new_everyone_ping = (
            contains_everyone_mention
            and channel_permissions.mention_everyone
        )
        previous_roles = {role.id for role in before.role_mentions} if before else set()
        added_roles = {
            role.id
            for role in message.role_mentions
            if role.id not in previous_roles
            and (channel_permissions.mention_everyone or role.mentionable)
        }
        # Most chat messages (including almost every command) have no security
        # relevant pings. Skip the database read entirely on that hot path.
        if not new_everyone_ping and not added_roles:
            return
        config = (await self.bot.db.get_guild(message.guild.id)).get("security", {})
        if not config.get("anti_nuke"):
            return
        if new_everyone_ping:
            # Message mentions do not produce the audit-log event that the
            # channel/role security listeners use. The author is known here,
            # so enforce the configured Ban policy directly without waiting
            # for an audit entry or a burst threshold.
            triggered, _ = await self.direct_ban_unlogged_actor(
                message.guild,
                message.author,
                message,
                "@everyone/@here mention sent by a bot" if message.author.bot else "@everyone/@here mention sent",
                "everyone_ping",
                config,
            )
            if triggered:
                try:
                    # PartialMessage.delete() does not accept a reason keyword.
                    await message.delete()
                except discord.NotFound:
                    # Another listener/worker may already have removed it.
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not remove everyone ping message %s in guild %s", message.id, message.guild.id)
            return
        if added_roles:
            triggered, _ = await self.direct_ban_unlogged_actor(
                message.guild,
                message.author,
                message,
                "Role mention sent by a bot" if message.author.bot else "Role mention sent",
                "role_ping",
                config,
            )
            if triggered:
                try:
                    # on_message can provide a PartialMessage for some gateway paths.
                    await message.delete()
                except discord.NotFound:
                    # Another listener/worker may already have removed it.
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not remove role ping message %s in guild %s", message.id, message.guild.id)

    @app_commands.command(name="security", description="Set an anti-link or anti-raid protection")
    @app_commands.guild_only()
    @app_commands.choices(feature=[app_commands.Choice(name="Anti-link", value="anti_link"), app_commands.Choice(name="Anti-raid", value="anti_raid")])
    async def security(self, interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        if not await self.authorize_security_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_nested(interaction.guild_id, "security", feature.value, enabled)
        embed = panel(f"{icon('security')} Security Updated", f"**{feature.name}:** {icon('enabled') if enabled else icon('disabled')} {'Enabled' if enabled else 'Disabled'}")
        await send_card(interaction, embed, ephemeral=True)

    @app_commands.command(name="setlog", description="Set the security and moderation log channel")
    @app_commands.guild_only()
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self.authorize_security_interaction(interaction):
            return
        bot_member = interaction.guild.me
        if bot_member is None or not channel.permissions_for(bot_member).send_messages or not channel.permissions_for(bot_member).embed_links:
            await send_card(interaction, notice_panel("Zyven needs **Send Messages** and **Embed Links** in that channel before it can be used for logs."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_nested(interaction.guild_id, "security", "log_channel", channel.id)
        await send_card(interaction, panel(f"{icon('channel')} Log Channel Updated", f"Security and moderation events will be sent to {channel.mention}."), ephemeral=True)

    @app_commands.command(name="whitelist", description="Add or remove a trusted member from AutoMod and Antinuke exemptions")
    @app_commands.guild_only()
    @app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
    async def whitelist(self, interaction: discord.Interaction, action: app_commands.Choice[str], member: discord.Member):
        if not await self.authorize_security_interaction(interaction):
            return
        data = await self.bot.db.get_guild(interaction.guild_id)
        security = data.get("security", {})
        trusted = {int(user_id) for user_id in (security.get("whitelist") or [])}
        permission_map = security.get("whitelist_permissions") or {}
        if action.value == "add":
            granted = set(permission_map.get(str(member.id), permission_map.get(member.id, [])))
            if member.id in trusted:
                granted = {event for _, _, events in WHITELIST_PERMISSION_GROUPS for event in events}
            view = WhitelistConfigView(self.bot, interaction.guild_id, member, granted, data)
            await send_card(interaction, view.make_embed(), view, ephemeral=True)
            return
        trusted.discard(member.id)
        permission_map.pop(str(member.id), None)
        permission_map.pop(member.id, None)
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.set_values(interaction.guild_id, {
            "security.whitelist": sorted(trusted),
            "security.whitelist_permissions": permission_map,
        })
        await send_card(interaction, panel(f"{icon('security')} Whitelist Updated", f"{member.mention} was removed from the Antinuke whitelist."), ephemeral=True)

    @app_commands.command(name="whitelistrole", description="Add or remove a role from AutoMod exemptions")
    @app_commands.guild_only()
    @app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
    async def whitelistrole(self, interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role):
        if not await self.authorize_security_interaction(interaction):
            return
        if role.is_default():
            await send_card(interaction, notice_panel("The @everyone role cannot be exempted from AutoMod."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        data = await self.bot.db.get_guild(interaction.guild_id)
        security = data.get("security", {})
        trusted_roles = set(security.get("whitelist_roles", []))
        trusted_roles.add(role.id) if action.value == "add" else trusted_roles.discard(role.id)
        await self.bot.db.set_nested(interaction.guild_id, "security", "whitelist_roles", sorted(trusted_roles))
        verb = "added to" if action.value == "add" else "removed from"
        await send_card(interaction, panel(f"{icon('security')} Role Whitelist Updated", f"{role.mention} was {verb} the AutoMod exemptions."), ephemeral=True)


class WhitelistConfigView(discord.ui.View):
    def __init__(self, bot, guild_id: int, member: discord.Member, granted: set[str], data: dict | None = None):
        super().__init__(timeout=300)
        self.bot, self.guild_id, self.member = bot, guild_id, member
        self.granted = set(granted)
        self.data = data or {}
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=all(event in self.granted for event in events),
            )
            for key, label, events in WHITELIST_PERMISSION_GROUPS
        ]
        self.add_item(WhitelistPermissionSelect(self, options))

    def make_embed(self) -> discord.Embed:
        lines = []
        for _, label, events in WHITELIST_PERMISSION_GROUPS:
            enabled = all(event in self.granted for event in events)
            pair = f"{icon('disableno')}{icon('enabledyes')}" if enabled else f"{icon('disabledyes')}{icon('enableno')}"
            lines.append(f"> {pair} : **{label}**")
        embed = panel("Whitelist Configuration for Current Server", "\n".join(lines))
        embed.add_field(name="Target", value=f"> {self.member.mention}", inline=False)
        embed.set_thumbnail(url=self.member.display_avatar.url)
        embed.set_footer(text="Powered By Zyven Development")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_access_security(interaction, self.data):
            await send_security_access_denied(interaction)
            return False
        return True

    async def save(self, interaction: discord.Interaction, selected_groups: list[str], *, full: bool = False):
        await interaction.response.defer()
        data = await self.bot.db.get_guild(self.guild_id)
        security = data.get("security", {})
        permission_map = dict(security.get("whitelist_permissions") or {})
        trusted = {int(user_id) for user_id in (security.get("whitelist") or [])}
        granted: set[str] = set()
        for group_key in selected_groups:
            group_events = next((events for key, _, events in WHITELIST_PERMISSION_GROUPS if key == group_key), ())
            granted.update(group_events)
        if full:
            trusted.add(self.member.id)
            permission_map.pop(str(self.member.id), None)
            permission_map.pop(self.member.id, None)
            granted = {event for _, _, events in WHITELIST_PERMISSION_GROUPS for event in events}
        else:
            trusted.discard(self.member.id)
            if granted:
                permission_map[str(self.member.id)] = sorted(granted)
            else:
                permission_map.pop(str(self.member.id), None)
                permission_map.pop(self.member.id, None)
        await self.bot.db.set_values(self.guild_id, {
            "security.whitelist_permissions": permission_map,
            "security.whitelist": sorted(trusted),
        })
        # The selector/buttons are a one-time setup flow. Once a whitelist
        # change is saved, leave the final status card without controls.
        completed = WhitelistConfigView(self.bot, self.guild_id, self.member, granted, data)
        completed.stop()
        await edit_original_component_card(interaction, completed.make_embed())

    async def remove_member(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data = await self.bot.db.get_guild(self.guild_id)
        security = data.get("security", {})
        trusted = {int(user_id) for user_id in (security.get("whitelist") or [])}
        trusted.discard(self.member.id)
        permission_map = dict(security.get("whitelist_permissions") or {})
        permission_map.pop(str(self.member.id), None)
        permission_map.pop(self.member.id, None)
        await self.bot.db.set_values(self.guild_id, {
            "security.whitelist": sorted(trusted),
            "security.whitelist_permissions": permission_map,
        })
        completed = WhitelistConfigView(self.bot, self.guild_id, self.member, set(), data)
        completed.stop()
        await edit_original_component_card(interaction, completed.make_embed())

    @discord.ui.button(label="Grant All Permissions", style=discord.ButtonStyle.success, row=1)
    async def grant_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, [key for key, _, _ in WHITELIST_PERMISSION_GROUPS], full=True)

    @discord.ui.button(label="Remove Permissions", style=discord.ButtonStyle.danger, row=1)
    async def remove_permissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, [])

    @discord.ui.button(emoji=icon("delete"), style=discord.ButtonStyle.danger, row=2)
    async def delete_whitelist_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.remove_member(interaction)


class WhitelistPermissionSelect(discord.ui.Select):
    def __init__(self, parent: WhitelistConfigView, options: list[discord.SelectOption]):
        self.parent_view = parent
        super().__init__(
            placeholder="Choose Permissions to Grant",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.save(interaction, list(self.values))


async def setup(bot):
    await bot.add_cog(Security(bot))
