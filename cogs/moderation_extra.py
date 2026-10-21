"""Prefix moderation commands that complement the core moderation cog."""
from datetime import datetime, timezone
import asyncio
from io import BytesIO
import logging
import re
from difflib import SequenceMatcher, get_close_matches

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageSequence
from bson import ObjectId
from bson.errors import InvalidId

from utils.emojis import icon
from core.settings import safe_request, settings
from utils.ui import component_card, notice_panel, panel
from cogs.moderation import EMOJI_PATTERN, audit_reason, moderation_card
from utils.role_safety import dangerous_permissions

log = logging.getLogger("zyven.moderation.extra")
CUSTOM_EMOJI_RE = re.compile(r"<a?:(?P<name>[A-Za-z0-9_]{2,32}):(?P<id>\d{15,22})>")
UNICODE_EMOJI_RE = re.compile(
    r"(?:[#*0-9]\uFE0F?\u20E3|[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF])"
    r"(?:\uFE0F|\uFE0E)?(?:[\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D(?:[\U0001F300-\U0001FAFF\u2600-\u27BF])(?:\uFE0F)?(?:[\U0001F3FB-\U0001F3FF])?)*"
)
MESSAGE_LINK_RE = re.compile(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)")


class ModerationConfirmView(discord.ui.View):
    def __init__(self, cog, requester: discord.Member, action: str, payload: dict, title: str, description: str):
        super().__init__(timeout=90)
        self.cog = cog
        self.requester_id = requester.id
        self.guild_id = requester.guild.id
        self.action = action
        self.payload = payload
        self.title = title
        self.description = description

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the moderator who started this action can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji=discord.PartialEmoji.from_str(icon("wtick")))
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This confirmation is no longer valid in this server.", ephemeral=True)
            return
        await interaction.response.edit_message(view=component_card(panel(
            f"{icon('loading')} {self.title} in Progress",
            f"{icon('loading')} Processing the requested changes…",
        )))
        try:
            result = await self.cog.execute_bulk(interaction.guild, interaction.user, self.action, self.payload)
            embed = panel(f"{self.title} Complete", result)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Confirmed moderation action %s failed in guild %s", self.action, self.guild_id)
            embed = panel(f"{self.title} Failed", "Discord rejected the action. Check Zyven's permissions and role positions, then retry.")
        self.stop()
        await interaction.edit_original_response(view=component_card(embed))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=discord.PartialEmoji.from_str(icon("delete")))
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=component_card(panel(f"{self.title} Cancelled", "No changes were made.")))


class StealChoiceView(discord.ui.View):
    def __init__(self, cog, ctx: commands.Context, sources: list[dict]):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.sources = sources
        self.requester_id = ctx.author.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who started this steal action can choose.", ephemeral=True)
        return False

    async def finish(self, interaction: discord.Interaction, *, as_sticker: bool):
        try:
            await interaction.response.defer()
        except discord.NotFound:
            return
        embed = await self.cog.finish_steal(self.ctx, self.sources, as_sticker=as_sticker)
        self.stop()
        try:
            await interaction.edit_original_response(view=component_card(embed))
        except discord.NotFound:
            return

    @discord.ui.button(label="Steal as Emoji", style=discord.ButtonStyle.primary)
    async def steal_emoji(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, as_sticker=False)

    @discord.ui.button(label="Steal as Sticker", style=discord.ButtonStyle.success)
    async def steal_sticker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, as_sticker=True)


class ModerationExtras(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.deleted_messages: dict[int, discord.Message] = {}

    def is_owner(self, ctx: commands.Context) -> bool:
        return ctx.author.id in {settings.owner_id, ctx.guild.owner_id}

    def schedule_log(self, guild: discord.Guild, text: str, label: str) -> None:
        """Keep optional moderation logging off the role update response path."""
        task = asyncio.create_task(self.log(guild, text), name=f"role-log-{label}")

        def report_failure(done_task):
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Role command log delivery failed: %s", label)

        task.add_done_callback(report_failure)

    async def require(self, ctx: commands.Context, permission: str, *, bot_permission: str | None = None) -> bool:
        if not self.is_owner(ctx) and not getattr(ctx.author.guild_permissions, permission, False):
            await ctx.send(view=component_card(notice_panel(f"You need **{permission.replace('_', ' ').title()}** permission to use this command.", title="Access Denied")), delete_after=5)
            return False
        me = ctx.guild.me
        needed = bot_permission or permission
        if me is None or not getattr(me.guild_permissions, needed, False):
            await ctx.send(view=component_card(notice_panel(f"Zyven needs **{needed.replace('_', ' ').title()}** permission for this action.")))
            return False
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Show role-name suggestions for failed role converters; keep other errors global."""
        command = ctx.command
        root = (command.root_parent or command) if command else None
        original = getattr(error, "original", error)
        role_command = bool(
            (root and root.name.casefold() == "role")
            or (command and command.name.casefold() == "removerole")
        )
        if role_command and isinstance(original, commands.RoleNotFound):
            ctx._suppress_command_error_response = True
            match = re.search(r'Role ["\'](.+?)["\'] not found', str(original), re.IGNORECASE)
            query = match.group(1).strip() if match else ""
            await self.send_role_not_found(ctx, query)
        # Other failures are delivered to the bot's command_error event by
        # discord.py automatically. Calling on_command_error directly here
        # made missing-argument replies (for example bare `warn`) appear twice.

    async def send_role_not_found(self, ctx: commands.Context, query: str):
        safe_query = discord.utils.escape_markdown(query or "(empty role name)")
        roles = [role for role in ctx.guild.roles if not role.is_default()]
        close = get_close_matches(
            (query or "").casefold(),
            [role.name.casefold() for role in roles],
            n=3,
            cutoff=0.55,
        ) if query else []
        suggestions = []
        for name in close:
            role = next((item for item in roles if item.name.casefold() == name), None)
            if role and role not in suggestions:
                suggestions.append(role)
        if not suggestions:
            command_prefix = ctx.prefix or settings.prefix
            command_name = ctx.command.qualified_name if ctx.command else "role"
            await ctx.send(
                view=component_card(panel("", f"Command usage: `{command_prefix}help {command_name}`")),
                reference=None,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=3,
            )
            return
        lines = [f"No role named `{safe_query}` exists in this server."]
        lines.extend(("", "**Did you mean:**"))
        lines.extend(f"{icon('space')} {role.mention} — `{discord.utils.escape_markdown(role.name)}`" for role in suggestions)
        lines.append("\nUse the exact role name, mention, or ID to choose the right one.")
        await ctx.send(
            view=component_card(panel(f"{icon('error')} Role Not Found", "\n".join(lines), color=0xED4245)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def role_guard(self, ctx: commands.Context, role: discord.Role, *, member: discord.Member | None = None) -> bool:
        if not await self.require(ctx, "manage_roles"):
            return False
        risky = dangerous_permissions(role)
        if role.is_default() or role.managed or risky:
            message = "This role has restricted permissions: " + ", ".join(name.replace("_", " ") for name in risky) + "." if risky else "Choose a regular, assignable role."
            await ctx.send(view=component_card(notice_panel(message)))
            return False
        if ctx.guild.me is None or role >= ctx.guild.me.top_role:
            await ctx.send(view=component_card(notice_panel("Move Zyven's highest role above the role you want to manage.")))
            return False
        if not self.is_owner(ctx) and role >= ctx.author.top_role:
            await ctx.send(view=component_card(notice_panel("You can only manage roles below your highest role.")))
            return False
        if member and (member.id == self.bot.user.id or (member.id == ctx.guild.owner_id and member.id != ctx.author.id)):
            await ctx.send(view=component_card(notice_panel("Zyven cannot change its own roles or the server owner's roles for another member.")))
            return False
        return True

    async def ask_confirmation(self, ctx: commands.Context, action: str, payload: dict, title: str, description: str):
        view = ModerationConfirmView(self, ctx.author, action, payload, title, description)
        await ctx.send(view=component_card(panel(f"Confirm {title}", description), view), allowed_mentions=discord.AllowedMentions.none())

    async def resolve_role_name(self, ctx: commands.Context, query: str) -> discord.Role | None:
        query = query.strip()
        if not query:
            await ctx.send(view=component_card(notice_panel("Enter a role mention, ID, or part of its name.")))
            return None

        mention_match = re.fullmatch(r"<@&(\d+)>|<?(\d+)>?", query)
        role_id = next((value for value in mention_match.groups() if value), None) if mention_match else None
        if role_id:
            role = ctx.guild.get_role(int(role_id))
            if role is not None:
                return role

        roles = [role for role in ctx.guild.roles if not role.is_default()]
        folded = query.casefold()
        exact = [role for role in roles if role.name.casefold() == folded]
        if len(exact) == 1:
            return exact[0]
        starts = [role for role in roles if role.name.casefold().startswith(folded)]
        if len(starts) == 1:
            return starts[0]
        contains = [role for role in roles if folded in role.name.casefold()]
        if len(contains) == 1:
            return contains[0]

        # For partial names shared by several roles, choose the clearly closest
        # name only when its match is distinct. Never guess between near-ties.
        matches = starts or contains
        if len(matches) > 1:
            ranked = sorted(
                ((SequenceMatcher(None, folded, role.name.casefold()).ratio(), role) for role in matches),
                key=lambda pair: pair[0], reverse=True,
            )
            if ranked[0][0] >= 0.55 and ranked[0][0] - ranked[1][0] >= 0.12:
                return ranked[0][1]
        if not matches:
            close = get_close_matches(folded, [role.name.casefold() for role in roles], n=5, cutoff=0.50)
            matches = [role for role in roles if role.name.casefold() in close]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                ranked = sorted(
                    ((SequenceMatcher(None, folded, role.name.casefold()).ratio(), role) for role in matches),
                    key=lambda pair: pair[0], reverse=True,
                )
                if ranked[0][0] >= 0.60 and ranked[0][0] - ranked[1][0] >= 0.12:
                    return ranked[0][1]
        if matches:
            lines = [f"More than one role matches `{discord.utils.escape_markdown(query)}`.", "", "**Choose a role:**"]
            lines.extend(f"{icon('space')} {role.mention} — `{discord.utils.escape_markdown(role.name)}`" for role in matches[:5])
            lines.append("\nAdd more of the role name, or use its mention or ID to choose exactly.")
            await ctx.send(
                view=component_card(panel(f"{icon('error')} Role Not Found", "\n".join(lines), color=0xED4245)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await self.send_role_not_found(ctx, query)
        return None

    async def execute_bulk(self, guild: discord.Guild, actor: discord.abc.User, action: str, payload: dict) -> str:
        permission = {
            "banall": "ban_members",
            "unbanall": "ban_members",
            "unmuteall": "moderate_members",
            "role_add": "manage_roles",
            "role_remove": "manage_roles",
            "role_delete": "manage_roles",
            "channel_access": "manage_channels",
        }.get(action)
        if permission and actor.id not in {settings.owner_id, guild.owner_id} and not getattr(actor.guild_permissions, permission, False):
            return f"Your **{permission.replace('_', ' ')}** permission changed. No changes were made."
        bot_permission = "manage_roles" if action in {"role_add", "role_remove", "role_delete"} else "manage_channels" if action == "channel_access" else permission
        if bot_permission and (guild.me is None or not getattr(guild.me.guild_permissions, bot_permission, False)):
            return f"Zyven now needs **{bot_permission.replace('_', ' ')}**. No changes were made."

        if action == "banall":
            banned = failed = skipped = 0
            bot_member = guild.me
            if bot_member is None:
                return "Zyven is not ready to verify the server hierarchy. No changes were made."
            for user_id in payload["member_ids"]:
                member = guild.get_member(int(user_id))
                if member is None or member.id in {guild.owner_id, self.bot.user.id, actor.id}:
                    skipped += 1
                    continue
                if member.top_role >= bot_member.top_role or (
                    actor.id not in {settings.owner_id, guild.owner_id}
                    and member.top_role >= actor.top_role
                ):
                    skipped += 1
                    continue
                try:
                    await guild.ban(
                        member,
                        delete_message_seconds=0,
                        reason=audit_reason(actor, "Ban all confirmed by moderator"),
                    )
                    banned += 1
                except discord.NotFound:
                    skipped += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            await self.log(guild, f"{actor.mention} banned {banned} member(s) with banall; {skipped} skipped, {failed} failed.")
            return f"Banned **{banned}** member(s). Skipped: **{skipped}** • Failed: **{failed}**."

        if action == "unbanall":
            removed = failed = 0
            for user_id in payload["user_ids"]:
                try:
                    await guild.unban(discord.Object(id=user_id), reason=audit_reason(actor, "Unban all confirmed by moderator"))
                    removed += 1
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            await self.log(guild, f"{actor.mention} unbanned {removed} user(s) with unbanall; {failed} failed.")
            return f"Unbanned **{removed}** user(s). Failed: **{failed}**."

        if action == "unmuteall":
            members = [guild.get_member(user_id) for user_id in payload["member_ids"]]
            members = [m for m in members if m and m.is_timed_out() and m.top_role < guild.me.top_role
                       and (actor.id in {settings.owner_id, guild.owner_id} or m.top_role < actor.top_role)]
            removed = failed = 0
            for member in members:
                try:
                    await member.timeout(None, reason=audit_reason(actor, "Unmute all confirmed by moderator"))
                    removed += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            await self.log(guild, f"{actor.mention} removed timeouts from {removed} member(s); {failed} failed.")
            return f"Unmuted **{removed}** member(s). Failed: **{failed}**."

        if action in {"role_add", "role_remove"}:
            role = guild.get_role(payload["role_id"])
            if not role or role.is_default() or role.managed or dangerous_permissions(role) or not guild.me or role >= guild.me.top_role:
                return "The target role was deleted, unassignable, or is now above Zyven's highest role. No changes were made."
            if actor.id not in {settings.owner_id, guild.owner_id} and role >= actor.top_role:
                return "The target role is now at or above your highest role. No changes were made."
            changed = failed = skipped = 0
            for user_id in payload["member_ids"]:
                member = guild.get_member(user_id)
                if not member or member.id in {guild.owner_id, self.bot.user.id}:
                    skipped += 1
                    continue
                should_have = action == "role_add"
                if (role in member.roles) == should_have:
                    skipped += 1
                    continue
                try:
                    if should_have:
                        await member.add_roles(role, reason=audit_reason(actor, "Bulk role action confirmed"))
                    else:
                        await member.remove_roles(role, reason=audit_reason(actor, "Bulk role action confirmed"))
                    changed += 1
                except (discord.Forbidden, discord.HTTPException):
                    failed += 1
            verb = "added to" if action == "role_add" else "removed from"
            await self.log(guild, f"Role {role} was {verb} {changed} member(s) by {actor.mention}; {failed} failed.")
            return f"Role {verb} **{changed}** member(s). Skipped: **{skipped}** • Failed: **{failed}**."

        if action == "channel_access":
            return await self.execute_bulk_channel_access(guild, actor, payload["operation"], payload["channel_ids"])
        if action == "role_delete":
            role = guild.get_role(payload["role_id"])
            if not role:
                return "That role no longer exists."
            if role.is_default() or role.managed or role.permissions.administrator or role >= guild.me.top_role or (actor.id not in {settings.owner_id, guild.owner_id} and role >= actor.top_role):
                return "The role is now above your or Zyven's highest role. No changes were made."
            await role.delete(reason=audit_reason(actor, "Role delete confirmed"))
            await self.log(guild, f"Role {role.name} was deleted by {actor.mention}.")
            return f"Deleted role **{discord.utils.escape_markdown(role.name)}**."
        return "Unknown moderation action."

    async def execute_bulk_channel_access(self, guild: discord.Guild, actor, action: str, channel_ids: list[int]) -> str:
        doc = await self.bot.db.get_guild(guild.id)
        security = doc.get("security", {})
        path_key = "channel_lock_overwrites" if action in {"lock", "unlock"} else "channel_hide_overwrites"
        saved = security.get(path_key, {})
        changes: list[tuple[discord.abc.GuildChannel, discord.PermissionOverwrite, str | None]] = []
        snapshot_values = {}
        clear_paths = []
        field = "send_messages" if action in {"lock", "unlock"} else "view_channel"
        for channel_id in channel_ids:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            key = str(channel.id)
            overwrite = channel.overwrites_for(guild.default_role)
            previous = saved.get(key)
            if action in {"lock", "hide"}:
                if overwrite_value_is(overwrite, field, False):
                    continue
                if key not in saved:
                    snapshot_values[f"security.{path_key}.{key}"] = getattr(overwrite, field)
                setattr(overwrite, field, False)
                changes.append((channel, overwrite, None))
            else:
                if key not in saved:
                    continue
                setattr(overwrite, field, previous)
                changes.append((channel, overwrite, f"security.{path_key}.{key}"))
        if snapshot_values:
            await self.bot.db.set_values(guild.id, snapshot_values)
        changed = failed = 0
        for channel, overwrite, clear_path in changes:
            try:
                await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"Bulk channel {action} by {actor}")
                changed += 1
                if clear_path:
                    await self.bot.db.unset_value(guild.id, clear_path)
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        await self.log(guild, f"{actor.mention} ran {action}all: {changed} channels changed, {failed} failed.")
        return f"Updated **{changed}** channel(s). Failed: **{failed}**."

    async def log(self, guild: discord.Guild, text: str):
        event_logs = self.bot.get_cog("EventLogs")
        if event_logs:
            await event_logs.send_log(guild, panel(f"{icon('moderation')} Moderation Log", text), "moderation")
            return
        try:
            channel_id = (await self.bot.db.get_guild(guild.id)).get("security", {}).get("log_channel")
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel:
                await channel.send(view=component_card(panel(f"{icon('moderation')} Moderation Log", text)), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not send moderation log in guild %s", guild.id)

    async def prefix_bulk_channels(self, ctx: commands.Context, operation: str):
        if not await self.require(ctx, "manage_channels"):
            return
        channel_ids = [channel.id for channel in ctx.guild.channels]
        saved_key = "channel_lock_overwrites" if operation in {"lock", "unlock"} else "channel_hide_overwrites"
        saved = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {}).get(saved_key, {})
        if operation in {"unlock", "unhide"}:
            channel_ids = [channel_id for channel_id in channel_ids if str(channel_id) in saved]
        if not channel_ids:
            await ctx.send(view=component_card(notice_panel("There are no matching channels to update.")))
            return
        label = operation.title() + " All"
        await self.ask_confirmation(ctx, "channel_access", {"operation": operation, "channel_ids": channel_ids}, label,
                                    f"Apply `{operation}` to **{len(channel_ids)}** channel(s)?")

    @commands.command(name="unbanall")
    @commands.guild_only()
    async def unbanall(self, ctx: commands.Context):
        if not await self.require(ctx, "ban_members"):
            return
        try:
            bans = [entry async for entry in ctx.guild.bans(limit=None)]
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not read the server ban list.")))
            return
        user_ids = [entry.user.id for entry in bans]
        if not user_ids:
            await ctx.send(view=component_card(notice_panel("There are no banned users to unban.")))
            return
        await self.ask_confirmation(ctx, "unbanall", {"user_ids": user_ids}, "Unban All",
                                    f"This will unban **{len(user_ids)}** user(s) from this server. Continue?")

    @commands.command(name="unmuteall")
    @commands.guild_only()
    async def unmuteall(self, ctx: commands.Context):
        if not await self.require(ctx, "moderate_members"):
            return
        try:
            if not ctx.guild.chunked:
                await ctx.guild.chunk(cache=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
        members = [m for m in ctx.guild.members if m.is_timed_out() and m.top_role < ctx.guild.me.top_role
                   and (self.is_owner(ctx) or m.top_role < ctx.author.top_role)]
        if not members:
            await ctx.send(view=component_card(notice_panel("There are no currently timed-out members that Zyven can manage.")))
            return
        await self.ask_confirmation(ctx, "unmuteall", {"member_ids": [m.id for m in members]}, "Unmute All",
                                    f"Remove active timeouts from **{len(members)}** member(s)?")

    @commands.command(name="lockall")
    @commands.guild_only()
    async def lockall(self, ctx: commands.Context):
        await self.prefix_bulk_channels(ctx, "lock")

    @commands.command(name="unlockall")
    @commands.guild_only()
    async def unlockall(self, ctx: commands.Context):
        await self.prefix_bulk_channels(ctx, "unlock")

    @commands.command(name="hideall")
    @commands.guild_only()
    async def hideall(self, ctx: commands.Context):
        await self.prefix_bulk_channels(ctx, "hide")

    @commands.command(name="unhideall")
    @commands.guild_only()
    async def unhideall(self, ctx: commands.Context):
        await self.prefix_bulk_channels(ctx, "unhide")

    @commands.group(name="role", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def prefix_role(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        *,
        role: str | None = None,
    ):
        # Support the short form `.role @member @role`; subcommands such as
        # `.role create <name>` and `.role humans <role>` continue to route here.
        if member is not None and role is not None:
            resolved_role = await self.resolve_role_name(ctx, role)
            if resolved_role is not None:
                await self.single_role_change(ctx, member, resolved_role, add=resolved_role not in member.roles)
                return
        prefix = ctx.prefix or settings.prefix
        await ctx.send(
            view=component_card(panel("", f"Command usage: `{prefix}help role`")),
            reference=None,
            mention_author=False,
            delete_after=3,
        )

    @prefix_role.command(name="create", case_insensitive=True)
    async def role_create(self, ctx: commands.Context, *, name: str):
        if not await self.require(ctx, "manage_roles"):
            return
        name = name.strip()
        if not name or len(name) > 100:
            await ctx.send(view=component_card(notice_panel("Role names must contain 1 to 100 characters.")))
            return
        try:
            role = await ctx.guild.create_role(name=name, reason=f"Role created by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not create that role. Check Manage Roles and role position.")))
            return
        await self.log(ctx.guild, f"Role {role.mention} was created by {ctx.author.mention}.")
        await ctx.send(view=component_card(moderation_card("Role Created", moderator=ctx.author, target=role, status="Successfully created")), allowed_mentions=discord.AllowedMentions.none())

    @prefix_role.command(name="delete", case_insensitive=True)
    async def role_delete(self, ctx: commands.Context, role: discord.Role):
        if not await self.role_guard(ctx, role):
            return
        await self.ask_confirmation(ctx, "role_delete", {"role_id": role.id}, "Delete Role",
                                    f"Delete {role.mention}? This also removes it from all members.")

    @prefix_role.command(name="rename", case_insensitive=True)
    async def role_rename(self, ctx: commands.Context, role: discord.Role, *, name: str):
        if not await self.role_guard(ctx, role):
            return
        name = name.strip()
        if not name or len(name) > 100:
            await ctx.send(view=component_card(notice_panel("Role names must contain 1 to 100 characters.")))
            return
        old_name = role.name
        try:
            await role.edit(name=name, reason=f"Role renamed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not rename that role.")))
            return
        await self.log(ctx.guild, f"Role {old_name} was renamed to {role.mention} by {ctx.author.mention}.")
        await ctx.send(view=component_card(moderation_card("Role Renamed", moderator=ctx.author, target=role, status=f"Renamed from `{discord.utils.escape_markdown(old_name)}`")), allowed_mentions=discord.AllowedMentions.none())

    @prefix_role.command(name="add", case_insensitive=True)
    async def role_add(self, ctx: commands.Context, member: discord.Member, *, role: str):
        resolved_role = await self.resolve_role_name(ctx, role)
        if resolved_role is not None:
            await self.single_role_change(ctx, member, resolved_role, add=True)

    @prefix_role.command(name="remove", case_insensitive=True)
    async def role_remove(self, ctx: commands.Context, member: discord.Member, *, role: str):
        resolved_role = await self.resolve_role_name(ctx, role)
        if resolved_role is not None:
            await self.single_role_change(ctx, member, resolved_role, add=False)

    async def single_role_change(self, ctx: commands.Context, member: discord.Member, role: discord.Role, *, add: bool):
        if not await self.role_guard(ctx, role, member=member):
            return
        if (role in member.roles) == add:
            await ctx.send(view=component_card(notice_panel(f"{member.mention} {'already has' if add else 'does not have'} {role.mention}.")))
            return
        verb = "added to" if add else "removed from"
        progress = await ctx.send(
            view=component_card(panel(
                f"{icon('loading')} Role Update in Progress",
                f"{icon('loading')} {'Adding' if add else 'Removing'} {role.mention} {'to' if add else 'from'} {member.mention}…",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            if add:
                await member.add_roles(role, reason=f"Role added by {ctx.author}")
            else:
                await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            await progress.edit(view=component_card(notice_panel("Zyven could not update that member's role. Check permissions and role position.")))
            return
        self.schedule_log(ctx.guild, f"Role {role} was {verb} {member.mention} by {ctx.author.mention}.", "member-role-update")
        await progress.edit(view=component_card(moderation_card("Member Role Updated", moderator=ctx.author, target=member, status=f"{role.mention} {verb} member")), allowed_mentions=discord.AllowedMentions.none())

    async def bulk_role_prompt(self, ctx: commands.Context, role: discord.Role, population: str, *, add: bool):
        if not await self.role_guard(ctx, role):
            return
        members = list(ctx.guild.members)
        if population == "humans":
            members = [m for m in members if not m.bot]
        elif population == "bots":
            members = [m for m in members if m.bot]
        elif population == "unverified":
            members = [m for m in members if m.pending]
        members = [m for m in members if m.id not in {ctx.guild.owner_id, self.bot.user.id} and ((role not in m.roles) if add else (role in m.roles))]
        if not members:
            await ctx.send(view=component_card(notice_panel("No eligible members need this role change.")))
            return
        action = "role_add" if add else "role_remove"
        verb = "add" if add else "remove"
        await self.ask_confirmation(ctx, action, {"role_id": role.id, "member_ids": [m.id for m in members]},
                                    f"{verb.title()} Role {population.title()}",
                                    f"{verb.title()} {role.mention} {'to' if add else 'from'} **{len(members)}** {population} member(s)?")

    @prefix_role.command(name="all", case_insensitive=True)
    async def role_all(self, ctx: commands.Context, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "all", add=True)

    @prefix_role.group(name="humans", aliases=("human", "hunms", "humnas"), invoke_without_command=True, case_insensitive=True)
    async def role_humans(self, ctx: commands.Context, *, role: discord.Role | None = None):
        if role is None:
            await ctx.send(view=component_card(notice_panel(f"Use `{settings.prefix}role humans add <role>`.")))
            return
        await self.bulk_role_prompt(ctx, role, "humans", add=True)

    @role_humans.command(name="add", case_insensitive=True)
    async def role_humans_add(self, ctx: commands.Context, *, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "humans", add=True)

    @prefix_role.group(name="bots", invoke_without_command=True, case_insensitive=True)
    async def role_bots(self, ctx: commands.Context, *, role: discord.Role | None = None):
        if role is None:
            await ctx.send(view=component_card(notice_panel(f"Use `{settings.prefix}role bots add <role>`.")))
            return
        await self.bulk_role_prompt(ctx, role, "bots", add=True)

    @role_bots.command(name="add", case_insensitive=True)
    async def role_bots_add(self, ctx: commands.Context, *, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "bots", add=True)

    @prefix_role.command(name="unverified", case_insensitive=True)
    async def role_unverified(self, ctx: commands.Context, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "unverified", add=True)

    @commands.command(name="removerole")
    @commands.guild_only()
    async def removerole(self, ctx: commands.Context, member: discord.Member, *, role: discord.Role):
        await self.single_role_change(ctx, member, role, add=False)

    @commands.group(name="rrole", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def rrole(self, ctx: commands.Context):
        await ctx.send(view=component_card(notice_panel(
            f"Use `{settings.prefix}rrole humans remove <role>` or `{settings.prefix}rrole bots remove <role>`."
        )))

    @rrole.command(name="all", case_insensitive=True)
    async def rrole_all(self, ctx: commands.Context, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "all", add=False)

    @rrole.group(name="humans", aliases=("human", "hunms", "humnas"), invoke_without_command=True, case_insensitive=True)
    async def rrole_humans(self, ctx: commands.Context, *, role: discord.Role | None = None):
        if role is None:
            await ctx.send(view=component_card(notice_panel(f"Use `{settings.prefix}rrole humans remove <role>`.")))
            return
        await self.bulk_role_prompt(ctx, role, "humans", add=False)

    @rrole_humans.command(name="remove", case_insensitive=True)
    async def rrole_humans_remove(self, ctx: commands.Context, *, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "humans", add=False)

    @rrole.group(name="bots", invoke_without_command=True, case_insensitive=True)
    async def rrole_bots(self, ctx: commands.Context, *, role: discord.Role | None = None):
        if role is None:
            await ctx.send(view=component_card(notice_panel(f"Use `{settings.prefix}rrole bots remove <role>`.")))
            return
        await self.bulk_role_prompt(ctx, role, "bots", add=False)

    @rrole_bots.command(name="remove", case_insensitive=True)
    async def rrole_bots_remove(self, ctx: commands.Context, *, role: discord.Role):
        await self.bulk_role_prompt(ctx, role, "bots", add=False)

    @commands.command(name="slowmode")
    @commands.guild_only()
    async def slowmode(self, ctx: commands.Context, value: str):
        if not await self.require(ctx, "manage_channels"):
            return
        match = re.fullmatch(r"(\d+)([sm]?)", value.lower())
        if not match:
            await ctx.send(view=component_card(notice_panel("Use seconds or a duration such as `15`, `30s`, or `2m`.")))
            return
        seconds = int(match.group(1)) * (60 if match.group(2) == "m" else 1)
        if not 0 <= seconds <= 21600:
            await ctx.send(view=component_card(notice_panel("Slowmode must be between 0 and 21,600 seconds.")))
            return
        if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            await ctx.send(view=component_card(notice_panel("Slowmode can only be changed in a text channel.")))
            return
        channel = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not update this channel's slowmode.")))
            return
        await ctx.send(view=component_card(moderation_card("Slowmode Updated", moderator=ctx.author, channel=channel, status=f"Set to {seconds} second(s)")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unslowmode")
    @commands.guild_only()
    async def unslowmode(self, ctx: commands.Context):
        if not await self.require(ctx, "manage_channels"):
            return
        channel = ctx.channel.parent if isinstance(ctx.channel, discord.Thread) else ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(view=component_card(notice_panel("Use this command in a text channel.")))
            return
        try:
            await channel.edit(slowmode_delay=0, reason=f"Slowmode removed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not remove this channel's slowmode.")))
            return
        await ctx.send(view=component_card(moderation_card("Slowmode Removed", moderator=ctx.author, channel=channel, status="Slowmode disabled")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="clone")
    @commands.guild_only()
    async def clone(self, ctx: commands.Context, *, name: str | None = None):
        if not await self.require(ctx, "manage_channels"):
            return
        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send(view=component_card(notice_panel("Clone can only copy a text channel.")))
            return
        try:
            clone = await channel.clone(name=name or channel.name, reason=f"Channel cloned by {ctx.author}")
            await clone.edit(position=channel.position + 1, reason="Place cloned channel beside its source")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(view=component_card(notice_panel("Zyven could not clone this channel. Check Manage Channels.")))
            return
        await self.log(ctx.guild, f"{channel.mention} was cloned as {clone.mention} by {ctx.author.mention}.")
        await ctx.send(view=component_card(moderation_card("Channel Cloned", moderator=ctx.author, channel=clone, status=f"Cloned from {channel.mention}")), allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild and not message.author.bot:
            self.deleted_messages[message.channel.id] = message

    @commands.command(name="snipe")
    @commands.guild_only()
    async def snipe(self, ctx: commands.Context):
        message = self.deleted_messages.get(ctx.channel.id)
        if not message:
            await ctx.send(view=component_card(notice_panel("There is no recently deleted message cached in this channel.")))
            return
        content = discord.utils.escape_markdown(message.content or "*No text content*")[:1000]
        deleted_at = int(message.created_at.replace(tzinfo=timezone.utc).timestamp())
        details = [
            f"{icon('members')} [{discord.utils.escape_markdown(message.author.display_name)}](https://discord.com/users/{message.author.id})",
            f"{icon('wtick')} **Deleted :** <t:{deleted_at}:R>",
            icon("reason"),
            f"{icon('space')}{icon('arrow')} **Message :** `{content}`",
            f"\n-# Requested by {discord.utils.escape_markdown(ctx.author.display_name)}",
        ]
        if message.attachments:
            details.extend(f"\n[{icon('links')} Attachment {i + 1}]({attachment.url})" for i, attachment in enumerate(message.attachments[:5]))
        embed = panel("Sniped Message", "\n".join(details))
        embed.set_thumbnail(url=message.author.display_avatar.url)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="enlarge")
    @commands.guild_only()
    async def enlarge(self, ctx: commands.Context, emoji_text: str):
        partial = discord.PartialEmoji.from_str(emoji_text)
        if partial.id:
            image_url = str(partial.url)
        else:
            codepoints = "-".join(f"{ord(char):x}" for char in emoji_text if ord(char) != 0xFE0F)
            if not codepoints or not EMOJI_PATTERN.search(emoji_text):
                await ctx.send(view=component_card(notice_panel("Provide a valid custom or Unicode emoji.")))
                return
            image_url = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{codepoints}.png"
        embed = panel(f"{icon('customrole')} Enlarged Emoji", emoji_text)
        embed.set_image(url=image_url)
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _steal_name(value: str, fallback: str = "stolen_emoji", *, sticker: bool = False) -> str:
        maximum = 30 if sticker else 32
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
        if not cleaned:
            cleaned = fallback
        if not cleaned[0].isalpha():
            cleaned = f"emoji_{cleaned}"
        cleaned = cleaned[:maximum]
        if len(cleaned) < 2:
            cleaned = (cleaned + "_emoji")[:maximum]
        return cleaned

    @staticmethod
    def _prepare_steal_image(data: bytes, *, as_sticker: bool, require_animation: bool = False) -> tuple[bytes, str]:
        with Image.open(BytesIO(data)) as image:
            image_format = (image.format or "PNG").upper()
            if require_animation and image_format != "GIF":
                raise ValueError("The animated emoji CDN did not return a GIF")
            if image_format == "GIF" and not as_sticker:
                # Discord animated emoji uploads are limited to 256 KiB. Keep
                # every frame, resize to emoji dimensions, and progressively
                # reduce palette/frame count until the animation fits.
                frames = []
                durations = []
                for frame in ImageSequence.Iterator(image):
                    current = frame.convert("RGBA")
                    current.thumbnail((128, 128), Image.Resampling.LANCZOS)
                    frames.append(current)
                    durations.append(max(20, int(frame.info.get("duration", image.info.get("duration", 100)))))
                if len(frames) < 2:
                    raise ValueError("The source animated emoji only contained one frame")
                loop = int(image.info.get("loop", 0))

                # GIF transparency is a palette index, not an alpha channel.
                # Reserve index 255 for transparent pixels so quantization
                # cannot turn them into black during the animated upload.
                sample = Image.new("RGBA", (128, 128 * min(len(frames), 20)), (0, 0, 0, 0))
                for sample_index, sample_frame in enumerate(frames[:20]):
                    sample.alpha_composite(sample_frame, (0, sample_index * 128))

                for stride in (1, 1, 2, 3, 4):
                    selected = frames[::stride]
                    selected_durations = durations[::stride]
                    if len(selected) < 2:
                        selected = [frames[0], frames[-1]]
                        selected_durations = [durations[0], durations[-1]]
                    for colors in (256, 128, 64, 32, 16):
                        palette_source = sample.convert("RGB").quantize(
                            colors=min(colors, 255),
                            method=Image.Quantize.FASTOCTREE,
                        )
                        palette = palette_source.getpalette() or []
                        palette = (palette[: 255 * 3] + [0, 0, 0] * 257)[: 256 * 3]
                        palette_image = Image.new("P", (1, 1))
                        palette_image.putpalette(palette)
                        quantized = []
                        for frame in selected:
                            converted = frame.convert("RGB").quantize(
                                palette=palette_image,
                                dither=Image.Dither.FLOYDSTEINBERG,
                            )
                            transparent = frame.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0)
                            converted.paste(255, mask=transparent)
                            converted.info["transparency"] = 255
                            quantized.append(converted)
                        output = BytesIO()
                        quantized[0].save(
                            output,
                            format="GIF",
                            save_all=True,
                            append_images=quantized[1:],
                            duration=selected_durations,
                            loop=loop,
                            optimize=True,
                            disposal=2,
                            transparency=255,
                            background=255,
                        )
                        if output.tell() <= 256 * 1024:
                            return output.getvalue(), "gif"
                raise ValueError("Animated GIF is too large to fit Discord's 256 KiB emoji upload limit")
            image.seek(0)
            image.convert("RGBA").save(output := BytesIO(), format="PNG")
            return output.getvalue(), "png"

    async def _download_steal_image(self, url: str) -> tuple[bytes, str]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            response = await safe_request(session, url)
            async with response:
                response.raise_for_status()
                if response.content_length and response.content_length > 15 * 1024 * 1024:
                    raise ValueError("Image is larger than 15 MB")
                data = await response.content.read(15 * 1024 * 1024 + 1)
                if not data or len(data) > 15 * 1024 * 1024:
                    raise ValueError("Image is empty or larger than 15 MB")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}:
                    raise ValueError("The source did not return a supported image content type")
                extension = content_type.removeprefix("image/").lower()
                if extension == "jpeg":
                    extension = "jpg"
                return data, extension

    async def _collect_steal_sources(self, ctx: commands.Context, target: str | None) -> list[dict]:
        sources: list[dict] = []
        seen: set[str] = set()

        def add_source(key: str, name: str, data: bytes, preview: str | None, *, emoji_tag: str = "✨", extension: str = "png", animated: bool = False):
            if key in seen or len(sources) >= 25:
                return
            seen.add(key)
            sources.append({"key": key, "name": name, "data": data, "preview": preview, "emoji_tag": emoji_tag, "extension": extension, "animated": animated})

        async def add_text_emoji(token: str):
            custom = CUSTOM_EMOJI_RE.fullmatch(token)
            if custom:
                partial = discord.PartialEmoji.from_str(token)
                key = f"custom:{partial.id}"
                if key in seen:
                    return
                # Build the CDN URL directly for compatibility with older
                # discord.py versions that do not implement PartialEmoji.url_as().
                # The extension must be GIF for animated emoji or Discord returns
                # a static image and the stolen emoji loses its animation.
                extension = "gif" if partial.animated else "png"
                emoji_url = f"https://cdn.discordapp.com/emojis/{partial.id}.{extension}?size=128&quality=lossless"
                data, extension = await self._download_steal_image(emoji_url)
                if partial.animated and not data.startswith((b"GIF87a", b"GIF89a")):
                    raise ValueError(f"Animated emoji {partial.name or partial.id} was not returned as a GIF")
                add_source(key, partial.name or "stolen_emoji", data, emoji_url, extension=extension, animated=partial.animated)
                return
            codepoints = "-".join(f"{ord(char):x}" for char in token if ord(char) not in {0xFE0E, 0xFE0F})
            if not codepoints:
                return
            key = f"unicode:{token}"
            if key in seen:
                return
            url = f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{codepoints}.png"
            data, extension = await self._download_steal_image(url)
            add_source(key, f"emoji_{codepoints.replace('-', '_')}", data, url, emoji_tag=token, extension=extension)

        async def add_image_url(url: str, name: str):
            key = f"image:{url}"
            if key in seen:
                return
            data, extension = await self._download_steal_image(url)
            add_source(key, name, data, url, extension=extension)

        async def add_message_assets(message: discord.Message):
            text = message.content or ""
            for match in CUSTOM_EMOJI_RE.finditer(text):
                await add_text_emoji(match.group(0))
            text = CUSTOM_EMOJI_RE.sub(" ", text)
            for match in UNICODE_EMOJI_RE.finditer(text):
                await add_text_emoji(match.group(0))
            for attachment in message.attachments:
                if not (attachment.content_type or "").startswith("image/") and not attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")):
                    continue
                data = await attachment.read()
                add_source(f"image:{attachment.url}", attachment.filename.rsplit(".", 1)[0], data, attachment.url, extension=attachment.filename.rsplit(".", 1)[-1].lower())
            for embed in message.embeds:
                image_url = (embed.image.url if embed.image else None) or (embed.thumbnail.url if embed.thumbnail else None)
                if image_url:
                    await add_image_url(image_url, "stolen_image")
            # Sticker messages do not expose their sticker as an attachment or
            # embed image. Read the sticker CDN asset explicitly so replying to
            # a sticker (or passing the message link) can steal it as a sticker.
            for sticker in message.stickers:
                format_value = getattr(sticker.format, "value", sticker.format)
                if format_value == 3:  # Lottie JSON is not a raster upload.
                    continue
                sticker_url = str(sticker.url)
                key = f"sticker:{sticker.id}"
                if key in seen:
                    continue
                data, extension = await self._download_steal_image(sticker_url)
                if extension not in {"png", "jpg", "webp", "gif", "avif"}:
                    continue
                add_source(key, sticker.name or "stolen_sticker", data, sticker_url, extension=extension)

        override_name = None
        if ctx.message.reference:
            source_message = ctx.message.reference.resolved
            if not isinstance(source_message, discord.Message):
                try:
                    source_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    source_message = None
            if isinstance(source_message, discord.Message):
                await add_message_assets(source_message)

        target = (target or "").strip()
        message_link = MESSAGE_LINK_RE.search(target)
        if message_link:
            channel_id, message_id = map(int, message_link.groups())
            channel = ctx.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await ctx.guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    await add_message_assets(await channel.fetch_message(message_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            target = target.replace(message_link.group(0), " ")
        elif target.isdigit():
            try:
                await add_message_assets(await ctx.channel.fetch_message(int(target)))
                target = ""
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        elif target.startswith(("https://", "http://")):
            await add_image_url(target, "stolen_image")
            target = ""

        if target:
            for match in CUSTOM_EMOJI_RE.finditer(target):
                await add_text_emoji(match.group(0))
            stripped = CUSTOM_EMOJI_RE.sub(" ", target)
            unicode_matches = list(UNICODE_EMOJI_RE.finditer(stripped))
            for match in unicode_matches:
                await add_text_emoji(match.group(0))
            remainder = UNICODE_EMOJI_RE.sub(" ", stripped).strip()
            if remainder and len(sources) == 1:
                override_name = remainder

        await add_message_assets(ctx.message)
        if override_name and sources:
            sources[0]["name"] = override_name
        return sources

    async def finish_steal(self, ctx: commands.Context, sources: list[dict], *, as_sticker: bool) -> discord.Embed:
        created_items, failures = [], []
        existing_names = {
            emoji.name.casefold() for emoji in ctx.guild.emojis
        } if not as_sticker else {sticker.name.casefold() for sticker in ctx.guild.stickers}
        for source in sources:
            try:
                data, extension = self._prepare_steal_image(
                    source["data"],
                    as_sticker=as_sticker,
                    require_animation=bool(source.get("animated")) and not as_sticker,
                )
                name = self._steal_name(source["name"], sticker=as_sticker)
                base = name
                index = 2
                while name.casefold() in existing_names:
                    suffix = f"_{index}"
                    name = f"{base[:(30 if as_sticker else 32) - len(suffix)]}{suffix}"
                    index += 1
                if as_sticker:
                    upload = discord.File(BytesIO(data), filename=f"{name}.{extension}")
                    created = await ctx.guild.create_sticker(
                        name=name,
                        description=f"Added by {ctx.author.display_name}"[:100],
                        emoji=source["emoji_tag"],
                        file=upload,
                        reason=f"Sticker added by {ctx.author}",
                    )
                    created_items.append(f"{created.name}")
                    await self.log(ctx.guild, f"Sticker {created.name} was added by {ctx.author.mention}.")
                else:
                    created = await ctx.guild.create_custom_emoji(
                        name=name,
                        image=data,
                        reason=f"Emoji added by {ctx.author}",
                    )
                    created_items.append(f"{created} (`{name}`)")
                    await self.log(ctx.guild, f"Emoji {created} was added by {ctx.author.mention}.")
                existing_names.add(name.casefold())
            except (discord.Forbidden, discord.HTTPException, ValueError, OSError) as exc:
                log.info("Could not steal asset %s in guild %s: %s", source["name"], ctx.guild.id, exc)
                failures.append(source["name"])

        kind = "Sticker" if as_sticker else "Emoji"
        status = f"Created {len(created_items)} {kind.lower()}(s)."
        if failures:
            status += f" Failed: {', '.join(failures[:10])}. Check the server {kind.lower()} limit and file format."
        if created_items:
            status += "\n\n" + "\n".join(f"• {item}" for item in created_items[:20])
        title_icon = icon("autorole_success") if created_items and not failures else icon("autorole_error")
        return panel(f"{title_icon} {kind}{'s' if len(sources) != 1 else ''} {'Added' if created_items else 'Could Not Be Added'}", status)

    @commands.command(name="steal")
    @commands.guild_only()
    async def steal(self, ctx: commands.Context, *, target: str | None = None):
        if not await self.require(ctx, "manage_emojis_and_stickers"):
            return
        try:
            sources = await self._collect_steal_sources(ctx, target)
        except (aiohttp.ClientError, discord.Forbidden, discord.HTTPException, ValueError, OSError) as exc:
            log.info("Steal source collection failed in guild %s: %s", ctx.guild.id, exc)
            await ctx.send(view=component_card(notice_panel("Zyven could not read one of the emojis or images. Check the source and try again.")))
            return
        if not sources:
            await ctx.send(view=component_card(notice_panel("Reply to a message, attach an image, or provide one or more custom/Unicode emojis.")))
            return

        embed = panel("Choose What to Steal", f"Found `{len(sources)}` item(s). Choose whether to add them as server emojis or stickers.")
        preview = sources[0].get("preview")
        if preview:
            embed.set_image(url=preview)
        view = StealChoiceView(self, ctx, sources)
        await ctx.send(
            view=component_card(embed, view),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        # discord.py dispatches bulk deletion separately; cache the newest one
        # so snipe still has a useful result after a purge.
        guild_messages = [m for m in messages if m.guild and not m.author.bot]
        if guild_messages:
            newest = max(guild_messages, key=lambda m: m.created_at)
            self.deleted_messages[newest.channel.id] = newest

    @commands.command(name="warn")
    @commands.guild_only()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self.member_permission_guard(ctx, member, "moderate_members"):
            return
        reason = reason[:1000]
        result = await self.bot.db.warnings.insert_one({"guild_id": str(ctx.guild.id), "member_id": member.id, "moderator_id": ctx.author.id, "reason": reason, "created_at": datetime.now(timezone.utc)})
        count = await self.bot.db.warnings.count_documents({"guild_id": str(ctx.guild.id), "member_id": member.id})
        dm_done = False
        try:
            await member.send(view=component_card(panel(f"{icon('warning')} Warning from {ctx.guild.name}", f"**Reason:** {discord.utils.escape_markdown(reason)}")))
            dm_done = True
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self.log(ctx.guild, f"{member.mention} was warned by {ctx.author.mention}; case `{result.inserted_id}`. Reason: {reason}")
        await ctx.send(view=component_card(moderation_card("Warning Recorded", moderator=ctx.author, target=member, reason=reason, status=f"Warning #{count}", dm_done=dm_done)), allowed_mentions=discord.AllowedMentions.none())

    async def member_permission_guard(self, ctx: commands.Context, member: discord.Member, permission: str) -> bool:
        if not self.is_owner(ctx) and not getattr(ctx.author.guild_permissions, permission, False):
            await ctx.send(view=component_card(notice_panel(f"You need **{permission.replace('_', ' ').title()}** permission to use this command.", title="Access Denied")), delete_after=5)
            return False
        if member.id in {ctx.guild.owner_id, self.bot.user.id}:
            await ctx.send(view=component_card(notice_panel("The server owner and bot cannot be moderated with this command.")))
            return False
        if not self.is_owner(ctx) and member.top_role >= ctx.author.top_role:
            await ctx.send(view=component_card(notice_panel("You can only moderate members with a lower top role than yours.")))
            return False
        return True

    @commands.command(name="warnings")
    @commands.guild_only()
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        if not await self.member_permission_guard(ctx, member, "moderate_members"):
            return
        cursor = (await self.bot.db.warnings.find({"guild_id": str(ctx.guild.id), "member_id": member.id})).sort("created_at", -1)
        records = await cursor.to_list(length=10)
        text = "\n".join(f"`{record['_id']}` • {discord.utils.escape_markdown(record['reason'][:150])} — <@{record['moderator_id']}>" for record in records) or "No warnings recorded for this member."
        embed = panel(f"Warnings • {discord.utils.escape_markdown(str(member))}", text)
        embed.set_footer(text=f"Showing {len(records)} recent warning(s)")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unwarn")
    @commands.guild_only()
    async def unwarn(self, ctx: commands.Context, member: discord.Member, case_id: str):
        if not await self.member_permission_guard(ctx, member, "moderate_members"):
            return
        try:
            warning_id = ObjectId(case_id)
        except (InvalidId, TypeError):
            await ctx.send(view=component_card(notice_panel("That is not a valid warning case ID. Use `warnings @member` to find one.")))
            return
        result = await self.bot.db.warnings.delete_one({"_id": warning_id, "guild_id": str(ctx.guild.id), "member_id": member.id})
        if not result.deleted_count:
            await ctx.send(view=component_card(notice_panel("That warning case was not found for this member.")))
            return
        await self.log(ctx.guild, f"Warning case `{warning_id}` for {member.mention} was removed by {ctx.author.mention}.")
        await ctx.send(view=component_card(panel(f"{icon('cleanup')} Warning Removed", f"Removed case `{warning_id}` for {member.mention}.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="clearwarnings", aliases=("clearwarns",))
    @commands.guild_only()
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        if not await self.member_permission_guard(ctx, member, "moderate_members"):
            return
        result = await self.bot.db.warnings.delete_many({"guild_id": str(ctx.guild.id), "member_id": member.id})
        await self.log(ctx.guild, f"{result.deleted_count} warnings for {member.mention} were cleared by {ctx.author.mention}.")
        await ctx.send(view=component_card(panel(f"{icon('cleanup')} Warnings Cleared", f"Removed **{result.deleted_count}** warning(s) for {member.mention}.")), allowed_mentions=discord.AllowedMentions.none())

async def setup(bot):
    await bot.add_cog(ModerationExtras(bot))


def overwrite_value_is(overwrite: discord.PermissionOverwrite, key: str, value: bool) -> bool:
    return getattr(overwrite, key) is value
