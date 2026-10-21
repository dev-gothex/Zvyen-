from datetime import datetime, timezone, timedelta
import asyncio
import logging
import re
from pymongo.errors import PyMongoError

import discord
from discord import app_commands
from discord.ext import commands
from bson import ObjectId
from bson.errors import InvalidId

from utils.emojis import icon
from utils.ui import owner_bypass_permissions, panel, send_card, component_card, notice_panel
from core.settings import settings

log = logging.getLogger("zyven.moderation")
EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d{15,22}>|[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def audit_reason(actor, reason: str) -> str:
    return f"{actor}: {reason[:400]}"[:512]


def moderation_card(title: str, *, moderator, target=None, reason: str | None = None,
                    status: str | None = None, dm_done: bool | None = None,
                    channel: discord.abc.GuildChannel | None = None) -> discord.Embed:
    """Shared CV2 copy styled after the supplied Dyno-like moderation cards."""
    lines = []
    if target is not None:
        target_label = target.mention if hasattr(target, "mention") else discord.utils.escape_markdown(str(target))
        lines.append(f"{icon('members')} **Target :** {target_label}")
    lines.append(f"{icon('members')} **Moderator :** `{discord.utils.escape_markdown(moderator.display_name)}`")
    if dm_done is not None:
        lines.append(f"{icon('space')}{icon('arrow')} DM Done? {icon('wtick') if dm_done else icon('cross')}")
    if reason is not None:
        lines.append(f"{icon('space')}{icon('arrow')} {icon('reason')} **Reason :** `{discord.utils.escape_markdown(reason[:400])}`")
    if channel is not None:
        lines.append(f"{icon('wtick')} {channel.mention}")
    if status:
        lines.append(f"{icon('space')}{icon('arrow')} **Status :** {status}")
    return panel(title, "\n".join(lines))


def parse_timeout_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not match:
        raise ValueError("Use a timeout like `10m`, `2h`, or `1d`.")
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds <= 0 or seconds > 28 * 86400:
        raise ValueError("Timeout duration must be between 1 second and 28 days.")
    return timedelta(seconds=seconds)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Keep the original overwrite available for an immediate unlock while
        # its durable backup is being written in the background.
        self._channel_lock_snapshots: dict[tuple[int, int], bool | None] = {}

    def schedule_followup(self, coro, label: str):
        """Run optional DMs/logging without delaying the moderation reply."""
        task = asyncio.create_task(coro, name=f"moderation-{label}")

        def report_failure(done_task):
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Moderation follow-up failed: %s", label)

        task.add_done_callback(report_failure)
        return task

    async def prefix_permission(self, ctx: commands.Context, permission: str) -> bool:
        if ctx.author.id in {settings.owner_id, ctx.guild.owner_id} or getattr(ctx.author.guild_permissions, permission, False):
            return True
        await ctx.send(view=component_card(notice_panel(f"You need **{permission.replace('_', ' ').title()}** permission to use this command.", title="Access Denied")), delete_after=5)
        return False

    async def prefix_target_guard(self, ctx: commands.Context, member: discord.Member, *, permission: str) -> bool:
        if not await self.prefix_permission(ctx, permission):
            return False
        if member.id in {ctx.guild.owner_id, self.bot.user.id}:
            await ctx.send(view=component_card(notice_panel("The server owner and bot cannot be moderated with this command.")))
            return False
        if isinstance(member, discord.Member):
            if ctx.author.id not in {ctx.guild.owner_id, self.bot.owner_id} and member.top_role >= ctx.author.top_role:
                await ctx.send(view=component_card(notice_panel("You can only moderate members with a lower top role than yours.")))
                return False
            if ctx.guild.me is None or member.top_role >= ctx.guild.me.top_role:
                await ctx.send(view=component_card(notice_panel("Move the bot role above the target member's highest role first.")))
                return False
        elif not (ctx.author.guild_permissions.administrator or ctx.author.id in {ctx.guild.owner_id, self.bot.owner_id}):
            await ctx.send(view=component_card(notice_panel("The target is not cached, so role hierarchy could not be checked. Try again shortly.")))
            return False
        return True

    async def try_dm_action(self, target, guild: discord.Guild, action: str, reason: str) -> bool:
        if not isinstance(target, discord.Member):
            return False
        try:
            await target.send(view=component_card(panel(f"{icon('members')} {action} • {guild.name}", f"**Reason:** {discord.utils.escape_markdown(reason[:1000])}")))
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @commands.command(name="ban")
    @commands.guild_only()
    async def prefix_ban(self, ctx: commands.Context, member_ref: str, *, reason: str = "No reason provided"):
        """Ban a member; admins can use the direct ID endpoint if cache lookup misses."""
        cleaned_ref = member_ref.strip()
        match = re.fullmatch(r"<@!?(\d{15,22})>|(\d{15,22})", cleaned_ref)
        target_id = int(match.group(1) or match.group(2)) if match else None
        member = ctx.guild.get_member(target_id) if target_id is not None else None
        if member is None and target_id is not None and not (
            ctx.author.guild_permissions.administrator or ctx.author.id in {ctx.guild.owner_id, self.bot.owner_id}
        ):
            try:
                member = await ctx.guild.fetch_member(target_id)
            except discord.NotFound:
                await ctx.send(view=component_card(notice_panel("That user is not a current server member. Administrators can ban by ID.")))
                return
        if member is None and target_id is None:
            try:
                member = await commands.MemberConverter().convert(ctx, cleaned_ref)
            except commands.MemberNotFound:
                await ctx.send(view=component_card(notice_panel("Member not found. Use a mention or Discord ID for the fastest ban.")))
                return
        target = member if member is not None else discord.Object(id=target_id)
        if not await self.prefix_target_guard(ctx, target, permission="ban_members"):
            return
        reason = reason[:1000]
        await ctx.guild.ban(target, reason=audit_reason(ctx.author, reason))
        target_display = member if member is not None else f"<@{target_id}>"
        if member is not None:
            self.schedule_followup(self.try_dm_action(member, ctx.guild, "Banned", reason), "ban-dm")
        self.schedule_followup(self.log(ctx.guild, f"{target_display} was banned by {ctx.author.mention}.\nReason: {reason}"), "ban-log")
        await ctx.send(view=component_card(moderation_card("Member Banned", moderator=ctx.author, target=target_display, reason=reason, status="Successfully banned")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unban")
    @commands.guild_only()
    async def prefix_unban(self, ctx: commands.Context, user_ref: str, *, reason: str = "No reason provided"):
        if not await self.prefix_permission(ctx, "ban_members"):
            return
        cleaned_ref = user_ref.strip()
        mention_match = re.fullmatch(r"<@!?(\d{15,22})>", cleaned_ref)
        id_match = re.fullmatch(r"\d{15,22}", cleaned_ref)
        user_id = int(mention_match.group(1) if mention_match else id_match.group(0)) if mention_match or id_match else None
        reason = reason[:1000]
        try:
            if user_id is not None:
                # Guild.unban accepts an ID-only Object; fetching the user first
                # added an unnecessary Discord API request to the common path.
                user = discord.Object(id=user_id)
            else:
                query = cleaned_ref.casefold()
                matches = []
                async for entry in ctx.guild.bans(limit=None):
                    candidate = entry.user
                    searchable = {candidate.name.casefold(), str(candidate).casefold()}
                    if candidate.global_name:
                        searchable.add(candidate.global_name.casefold())
                    if query in searchable:
                        matches.append(candidate)
                if len(matches) > 1:
                    await ctx.send(view=component_card(notice_panel("More than one banned account matches that name. Use the user's ID to choose the right one.")))
                    return
                if not matches:
                    await ctx.send(view=component_card(notice_panel("No banned user matched that tag/name. Try their Discord ID.")))
                    return
                user = matches[0]
            await ctx.guild.unban(user, reason=audit_reason(ctx.author, reason))
        except discord.NotFound:
            await ctx.send(view=component_card(notice_panel("That user is not currently banned from this server.")))
            return
        target_display = f"<@{user_id}>" if user_id is not None else user
        self.schedule_followup(self.log(ctx.guild, f"{target_display} was unbanned by {ctx.author.mention}. Reason: {reason}"), "unban-log")
        await ctx.send(view=component_card(moderation_card("Member Unbanned", moderator=ctx.author, target=target_display, reason=reason, status="Successfully unbanned")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="kick")
    @commands.guild_only()
    async def prefix_kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self.prefix_target_guard(ctx, member, permission="kick_members"):
            return
        reason = reason[:1000]
        await member.kick(reason=audit_reason(ctx.author, reason))
        self.schedule_followup(self.try_dm_action(member, ctx.guild, "Kicked", reason), "kick-dm")
        self.schedule_followup(self.log(ctx.guild, f"{member} was kicked by {ctx.author.mention}. Reason: {reason}"), "kick-log")
        await ctx.send(view=component_card(moderation_card("Member Kicked", moderator=ctx.author, target=member, reason=reason, status="Successfully kicked")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="mute", aliases=("timeout",))
    @commands.guild_only()
    async def prefix_mute(self, ctx: commands.Context, member: discord.Member, duration: str = "1d", *, reason: str = "No reason provided"):
        if not await self.prefix_target_guard(ctx, member, permission="moderate_members"):
            return
        try:
            delta = parse_timeout_duration(duration)
        except ValueError as error:
            await ctx.send(view=component_card(notice_panel(str(error))))
            return
        reason = reason[:1000]
        await member.timeout(delta, reason=audit_reason(ctx.author, reason))
        self.schedule_followup(self.try_dm_action(member, ctx.guild, "Timed out", reason), "mute-dm")
        self.schedule_followup(self.log(ctx.guild, f"{member.mention} was timed out for {duration} by {ctx.author.mention}. Reason: {reason}"), "mute-log")
        await ctx.send(view=component_card(moderation_card("Member Muted", moderator=ctx.author, target=member, reason=reason, status=f"Muted for {duration}")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="unmute", aliases=("untimeout",))
    @commands.guild_only()
    async def prefix_unmute(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self.prefix_target_guard(ctx, member, permission="moderate_members"):
            return
        await member.timeout(None, reason=audit_reason(ctx.author, reason))
        self.schedule_followup(self.log(ctx.guild, f"Timeout removed for {member.mention} by {ctx.author.mention}. Reason: {reason}"), "unmute-log")
        await ctx.send(view=component_card(moderation_card("Member Unmuted", moderator=ctx.author, target=member, reason=reason, status="Successfully unmuted")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="nick", aliases=("nickname",))
    @commands.guild_only()
    async def prefix_nick(self, ctx: commands.Context, member: discord.Member, *, nickname: str | None = None):
        if not await self.prefix_target_guard(ctx, member, permission="manage_nicknames"):
            return
        if nickname and len(nickname) > 32:
            await ctx.send(view=component_card(notice_panel("Nicknames can be at most 32 characters.")))
            return
        await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
        changed = f"changed to `{discord.utils.escape_markdown(nickname)}`" if nickname else "reset"
        self.schedule_followup(self.log(ctx.guild, f"{member.mention}'s nickname was {changed} by {ctx.author.mention}."), "nickname-log")
        await ctx.send(view=component_card(moderation_card("Nickname Updated", moderator=ctx.author, target=member, status=f"Nickname {changed}")), allowed_mentions=discord.AllowedMentions.none())

    async def prefix_channel_access(self, ctx: commands.Context, action: str):
        if not await self.prefix_permission(ctx, "manage_channels"):
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send(view=component_card(notice_panel("Use this command in a text channel.")))
            return
        clear_saved_path = None
        everyone = ctx.guild.default_role
        overwrite = ctx.channel.overwrites_for(everyone)
        if action == "lock":
            path = f"security.channel_lock_overwrites.{ctx.channel.id}"
            key = str(ctx.channel.id)
            if overwrite.send_messages is False:
                await ctx.send(view=component_card(notice_panel(f"{ctx.channel.mention} is already locked.")))
                return
            self._channel_lock_snapshots[(ctx.guild.id, ctx.channel.id)] = overwrite.send_messages
            overwrite.send_messages = False
            status = "Locked for @everyone"
            title = "Channel Locked!"
        elif action == "unlock":
            key = str(ctx.channel.id)
            cache_key = (ctx.guild.id, ctx.channel.id)
            if cache_key in self._channel_lock_snapshots:
                previous = self._channel_lock_snapshots[cache_key]
                has_saved = True
            else:
                saved = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {}).get("channel_lock_overwrites", {})
                has_saved = key in saved
                previous = saved.get(key)
            if overwrite.send_messages is not False and not has_saved:
                await ctx.send(view=component_card(notice_panel(f"{ctx.channel.mention} is already unlocked.")))
                return
            overwrite.send_messages = previous
            if has_saved:
                clear_saved_path = f"security.channel_lock_overwrites.{ctx.channel.id}"
            status = "Unlocked for @everyone"
            title = "Channel Unlocked!"
        elif action == "hide":
            path = f"security.channel_hide_overwrites.{ctx.channel.id}"
            security = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {})
            saved = security.get("channel_hide_overwrites", {})
            key = str(ctx.channel.id)
            if overwrite.view_channel is False and key in saved:
                await ctx.send(view=component_card(notice_panel(f"{ctx.channel.mention} is already hidden.")))
                return
            await self.bot.db.set_nested(ctx.guild.id, "security", f"channel_hide_overwrites.{ctx.channel.id}", overwrite.view_channel)
            overwrite.view_channel = False
            status = "Hidden from @everyone"
            title = "Channel Hidden!"
        else:
            key = str(ctx.channel.id)
            saved = (await self.bot.db.get_guild(ctx.guild.id)).get("security", {}).get("channel_hide_overwrites", {})
            if overwrite.view_channel is not False and key not in saved:
                await ctx.send(view=component_card(notice_panel(f"{ctx.channel.mention} is already visible to @everyone.")))
                return
            overwrite.view_channel = saved.get(key)
            if key in saved:
                clear_saved_path = f"security.channel_hide_overwrites.{ctx.channel.id}"
            status = "Visible to @everyone"
            title = "Channel Unhidden!"
        try:
            await ctx.channel.set_permissions(everyone, overwrite=overwrite, reason=f"Channel {action} by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException):
            if action == "lock":
                self._channel_lock_snapshots.pop((ctx.guild.id, ctx.channel.id), None)
            await ctx.send(view=component_card(notice_panel("Zyven could not update this channel's permissions. Check its Manage Channels permission.")))
            return
        if action == "lock":
            self.schedule_followup(
                self.bot.db.set_nested(ctx.guild.id, "security", f"channel_lock_overwrites.{ctx.channel.id}", self._channel_lock_snapshots[(ctx.guild.id, ctx.channel.id)]),
                "lock-saved-state",
            )
        elif action == "unlock":
            self._channel_lock_snapshots.pop((ctx.guild.id, ctx.channel.id), None)
        if clear_saved_path:
            self.schedule_followup(self.bot.db.unset_value(ctx.guild.id, clear_saved_path), f"{action}-saved-state")
        self.schedule_followup(self.log(ctx.guild, f"{ctx.channel.mention} was {action}ed by {ctx.author.mention}."), f"channel-{action}-log")
        await ctx.send(view=component_card(moderation_card(title, moderator=ctx.author, channel=ctx.channel, status=status)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="lock")
    @commands.guild_only()
    async def prefix_lock(self, ctx: commands.Context):
        await self.prefix_channel_access(ctx, "lock")

    @commands.command(name="unlock")
    @commands.guild_only()
    async def prefix_unlock(self, ctx: commands.Context):
        await self.prefix_channel_access(ctx, "unlock")

    @commands.command(name="hide")
    @commands.guild_only()
    async def prefix_hide(self, ctx: commands.Context):
        await self.prefix_channel_access(ctx, "hide")

    @commands.command(name="unhide")
    @commands.guild_only()
    async def prefix_unhide(self, ctx: commands.Context):
        await self.prefix_channel_access(ctx, "unhide")

    @commands.command(name="nuke")
    @commands.guild_only()
    async def prefix_nuke(self, ctx: commands.Context):
        if not await self.prefix_permission(ctx, "manage_channels"):
            return
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send(view=component_card(notice_panel("Nuke can only recreate a text channel.")))
            return
        view = NukeConfirmView(self.bot, ctx.channel, ctx.author)
        await ctx.send(view=component_card(panel("Nuke Channel!", f"Do you really want to nuke {ctx.channel.mention}?\n\n{icon('space')}{icon('arrow')} Click `Confirm` to proceed or `Cancel` to abort."), view))

    async def target_guard(self, interaction: discord.Interaction, member: discord.Member, *, require_bot_hierarchy: bool = True) -> bool:
        guild = interaction.guild
        if member.id == guild.owner_id or member.id == self.bot.user.id:
            await send_card(interaction, notice_panel("The server owner and bot cannot be moderated with this command."), ephemeral=True)
            return False
        if interaction.user.id not in {guild.owner_id, self.bot.owner_id} and member.top_role >= interaction.user.top_role:
            await send_card(interaction, notice_panel("You can only moderate members with a lower top role than yours."), ephemeral=True)
            return False
        bot_member = guild.me
        if require_bot_hierarchy and (bot_member is None or member.top_role >= bot_member.top_role):
            await send_card(interaction, notice_panel("Move the bot role above the target member's highest role first."), ephemeral=True)
            return False
        return True

    async def log(self, guild, text):
        event_logs = self.bot.get_cog("EventLogs")
        if event_logs:
            await event_logs.send_log(guild, discord.Embed(description=text, color=0xED4245), "moderation")
            return
        try:
            doc = await self.bot.db.get_guild(guild.id)
        except PyMongoError:
            log.exception("Could not load moderation-log settings for guild %s", guild.id)
            return
        channel_id = doc.get("security", {}).get("log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            embed = discord.Embed(description=text, color=0xED4245)
            embed.set_footer(text="Zyven Development™ • Moderation Log")
            try:
                await channel.send(view=component_card(embed))
            except discord.Forbidden:
                log.warning("Missing permission to send moderation logs in channel %s (guild %s)", channel.id, guild.id)
            except discord.HTTPException:
                log.exception("Failed to send moderation log in channel %s (guild %s)", channel.id, guild.id)

    @staticmethod
    async def prefix_purge_permissions(ctx):
        if ctx.author.id == settings.owner_id or ctx.author.guild_permissions.manage_messages:
            return True
        raise commands.MissingPermissions(["manage_messages"])

    async def prefix_purge_messages(self, ctx, *, amount: int, member=None, predicate=None, label=""):
        if not hasattr(ctx.channel, "purge"):
            await ctx.send(view=component_card(notice_panel("Use purge in a text channel or thread.")))
            return
        if not 1 <= amount <= 1000:
            await ctx.send(view=component_card(notice_panel("Purge amount must be between 1 and 1,000.")))
            return
        check = predicate
        if member is not None:
            check = lambda message: message.author.id == member.id
            label = f" from {member.mention}"
        invocation_deleted = False
        try:
            await ctx.message.delete()
            invocation_deleted = True
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not delete purge invocation %s in channel %s", ctx.message.id, ctx.channel.id)
        try:
            if check is None:
                deleted = await ctx.channel.purge(limit=amount)
            else:
                deleted = await ctx.channel.purge(limit=amount, check=check)
        except discord.Forbidden:
            await ctx.send(view=component_card(notice_panel("Purge failed. Zyven needs **Manage Messages** and **Read Message History** in this channel.")), delete_after=5)
            return
        except discord.HTTPException as exc:
            log.exception("Purge failed in channel %s (guild %s)", ctx.channel.id, ctx.guild.id)
            await ctx.send(view=component_card(notice_panel(f"Purge failed while contacting Discord (error {getattr(exc, 'code', 'unknown')}). Check bot permissions and try again.")), delete_after=5)
            return
        total = len(deleted) + int(invocation_deleted)
        note = "" if invocation_deleted else " (command message could not be deleted; check Manage Messages)"
        await ctx.send(view=component_card(panel(f"{icon('tick')} Messages Cleared", f"Deleted **{total}** message(s){label} from this channel.{note}")), delete_after=3)

    @app_commands.command(name="ban", description="Ban a member from this server")
    @app_commands.guild_only()
    @owner_bypass_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self.target_guard(interaction, member): return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        await member.ban(reason=audit_reason(interaction.user, reason))
        self.schedule_followup(self.try_dm_action(member, interaction.guild, "Banned", reason), "ban-dm")
        self.schedule_followup(self.log(interaction.guild, f"{icon('moderation')} {member} was banned by {interaction.user.mention}.\nReason: {reason}"), "ban-log")
        await send_card(interaction, moderation_card("Member Banned", moderator=interaction.user, target=member, reason=reason, status="Successfully banned"), ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by their Discord ID")
    @app_commands.guild_only()
    @owner_bypass_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        try:
            target_id = int(user_id)
        except ValueError:
            await send_card(interaction, notice_panel("Enter a valid numeric Discord user ID."), ephemeral=True)
            return
        if not 0 < target_id < 2**64:
            await send_card(interaction, notice_panel("Enter a valid numeric Discord user ID."), ephemeral=True)
            return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        try:
            # The snowflake is enough for Guild.unban; fetching a User added
            # a second network request before the actual moderation action.
            user = discord.Object(id=target_id)
            await interaction.guild.unban(user, reason=audit_reason(interaction.user, reason))
        except discord.NotFound:
            await send_card(interaction, notice_panel("That user is not currently banned from this server."), ephemeral=True)
            return
        target_display = f"<@{target_id}>"
        self.schedule_followup(self.log(interaction.guild, f"{icon('moderation')} {target_display} was unbanned by {interaction.user.mention}.\nReason: {reason}"), "unban-log")
        await send_card(interaction, moderation_card("Member Unbanned", moderator=interaction.user, target=target_display, reason=reason, status="Successfully unbanned"), ephemeral=True)

    @app_commands.command(name="kick", description="Kick a member from this server")
    @app_commands.guild_only()
    @owner_bypass_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self.target_guard(interaction, member): return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        await member.kick(reason=audit_reason(interaction.user, reason))
        self.schedule_followup(self.try_dm_action(member, interaction.guild, "Kicked", reason), "kick-dm")
        self.schedule_followup(self.log(interaction.guild, f"{icon('kick')} {member} was kicked by {interaction.user.mention}.\nReason: {reason}"), "kick-log")
        await send_card(interaction, moderation_card("Member Kicked", moderator=interaction.user, target=member, reason=reason, status="Successfully kicked"), ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member for up to 28 days")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
        if not await self.target_guard(interaction, member): return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        from datetime import timedelta
        await member.timeout(timedelta(minutes=minutes), reason=audit_reason(interaction.user, reason))
        self.schedule_followup(self.try_dm_action(member, interaction.guild, "Timed out", reason), "mute-dm")
        self.schedule_followup(self.log(interaction.guild, f"{icon('timeout')} {member.mention} timed out for {minutes} minutes by {interaction.user.mention}.\nReason: {reason}"), "mute-log")
        await send_card(interaction, moderation_card("Member Muted", moderator=interaction.user, target=member, reason=reason, status=f"Muted for {minutes} minutes"), ephemeral=True)

    @app_commands.command(name="untimeout", description="Remove a member's active timeout")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self.target_guard(interaction, member): return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        await member.timeout(None, reason=audit_reason(interaction.user, f"Timeout removed: {reason}"))
        self.schedule_followup(self.log(interaction.guild, f"{member.mention}'s timeout was removed by {interaction.user.mention}.\nReason: {reason}"), "unmute-log")
        await send_card(interaction, moderation_card("Member Unmuted", moderator=interaction.user, target=member, reason=reason, status="Successfully unmuted"), ephemeral=True)

    @app_commands.command(name="purge", description="Delete recent messages from this channel")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        member: discord.Member | None = None,
        bots_only: bool = False,
    ):
        if not hasattr(interaction.channel, "purge"):
            await send_card(interaction, notice_panel("Use `/purge` in a text channel or thread."), ephemeral=True)
            return
        if member is not None and bots_only:
            await send_card(interaction, notice_panel("Choose either one member or bot messages as the filter."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        check = None
        if member is not None:
            check = lambda message: message.author.id == member.id
        elif bots_only:
            check = lambda message: message.author.bot
        if check is None:
            deleted = await interaction.channel.purge(limit=amount)
        else:
            deleted = await interaction.channel.purge(limit=amount, check=check)
        filter_label = f" from {member.mention}" if member else (" from bots" if bots_only else "")
        response = await send_card(interaction, panel(f"{icon('tick')} Messages Cleared", f"Deleted **{len(deleted)}** message(s){filter_label} from this channel."), ephemeral=True)
        await response.delete(delay=3)

    @commands.group(name="purge", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    @commands.bot_has_permissions(manage_messages=True)
    @commands.check(prefix_purge_permissions)
    async def prefix_purge(self, ctx: commands.Context, amount: str | None = None):
        """Delete messages, or show the available purge filters."""
        if amount is None:
            help_command = self.bot.get_command("help")
            await ctx.invoke(help_command, query="purge")
            return
        try:
            count = int(amount)
        except ValueError:
            help_command = self.bot.get_command("help")
            await ctx.invoke(help_command, query="purge")
            return
        await self.prefix_purge_messages(ctx, amount=count)

    @prefix_purge.command(name="user")
    async def prefix_purge_user(self, ctx: commands.Context, member: discord.Member, amount: int = 100):
        """Purge messages from one member."""
        await self.prefix_purge_messages(ctx, amount=amount, member=member)

    @prefix_purge.command(name="embeds")
    async def prefix_purge_embeds(self, ctx: commands.Context, amount: int = 100):
        """Purge messages containing embeds."""
        await self.prefix_purge_messages(ctx, amount=amount, predicate=lambda message: bool(message.embeds), label=" containing embeds")

    @prefix_purge.command(name="reactions")
    async def prefix_purge_reactions(self, ctx: commands.Context, amount: int = 100):
        """Clear reactions from recent messages."""
        if not 1 <= amount <= 1000:
            await ctx.send(view=component_card(notice_panel("Purge amount must be between 1 and 1,000.")), delete_after=3)
            return
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not delete purge invocation %s in channel %s", ctx.message.id, ctx.channel.id)
        messages = [message async for message in ctx.channel.history(limit=amount)]
        changed = 0
        for message in messages:
            if message.reactions:
                try:
                    await message.clear_reactions()
                    changed += 1
                except discord.Forbidden:
                    await ctx.send(view=component_card(notice_panel("The bot needs **Manage Messages** to clear reactions.")), delete_after=3)
                    return
        await ctx.send(view=component_card(panel(f"{icon('cleanup')} Reactions Cleared", f"Cleared reactions from **{changed}** message(s).")), delete_after=3)

    @prefix_purge.command(name="files")
    async def prefix_purge_files(self, ctx: commands.Context, amount: int = 100):
        """Purge messages containing file attachments."""
        def has_files(message):
            return any(not (attachment.content_type or "").startswith("image/") for attachment in message.attachments)
        await self.prefix_purge_messages(ctx, amount=amount, predicate=has_files, label=" containing files")

    @prefix_purge.command(name="images")
    async def prefix_purge_images(self, ctx: commands.Context, amount: int = 100):
        """Purge messages containing images."""
        def has_images(message):
            return any((attachment.content_type or "").startswith("image/") for attachment in message.attachments) or any(embed.image.url for embed in message.embeds)
        await self.prefix_purge_messages(ctx, amount=amount, predicate=has_images, label=" containing images")

    @prefix_purge.command(name="emoji")
    async def prefix_purge_emoji(self, ctx: commands.Context, amount: int = 100):
        """Purge messages containing emojis."""
        await self.prefix_purge_messages(ctx, amount=amount, predicate=lambda message: bool(EMOJI_PATTERN.search(message.content)), label=" containing emojis")

    @prefix_purge.command(name="contains")
    async def prefix_purge_contains(self, ctx: commands.Context, amount_or_text: str, *, text: str | None = None):
        """Purge up to 1,000 recent messages containing text; amount defaults to 100."""
        if text is None:
            amount = 100
            text = amount_or_text
        else:
            try:
                amount = int(amount_or_text)
            except ValueError:
                await ctx.send(view=component_card(notice_panel("Use `purge contains [amount] <text>`; amount must be between 1 and 1,000.")), delete_after=3)
                return
        await self.prefix_purge_messages(ctx, amount=amount, predicate=lambda message: text.casefold() in message.content.casefold(), label=f" containing `{discord.utils.escape_markdown(text[:80])}`")

    @prefix_purge.command(name="bot", aliases=("pb",))
    async def prefix_purge_bot(self, ctx: commands.Context, amount: int = 100):
        """Purge up to 1,000 recent bot messages; defaults to 100."""
        await self.prefix_purge_messages(ctx, amount=amount, predicate=lambda message: message.author.bot, label=" sent by bots")

    @commands.command(name="pb")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_messages=True)
    @commands.check(prefix_purge_permissions)
    async def prefix_bot_purge_alias(self, ctx: commands.Context, amount: int = 100):
        """Purge up to 1,000 recent bot messages; defaults to 100."""
        await self.prefix_purge_messages(ctx, amount=amount, predicate=lambda message: message.author.bot, label=" sent by bots")

    @app_commands.command(name="slowmode", description="Set the channel slowmode delay")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        if not isinstance(interaction.channel, discord.TextChannel):
            await send_card(interaction, notice_panel("Slowmode can only be set in a text channel."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {interaction.user}")
        self.schedule_followup(self.log(interaction.guild, f"Slowmode in {interaction.channel.mention} set to {seconds}s by {interaction.user.mention}."), "slowmode-log")
        label = "disabled" if seconds == 0 else f"set to **{seconds} seconds**"
        await send_card(interaction, panel("Slowmode Updated", f"Slowmode {label} in {interaction.channel.mention}."), ephemeral=True)

    @app_commands.command(name="lock", description="Prevent members from sending messages in this channel")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            await send_card(interaction, notice_panel("Use `/lock` in a text channel."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        lock_path = f"channel_lock_overwrites.{interaction.channel.id}"
        key = str(interaction.channel.id)
        if overwrite.send_messages is False:
            await send_card(interaction, notice_panel(f"{interaction.channel.mention} is already locked."), ephemeral=True)
            return
        cache_key = (interaction.guild_id, interaction.channel.id)
        original_send_messages = overwrite.send_messages
        self._channel_lock_snapshots[cache_key] = original_send_messages
        overwrite.send_messages = False
        try:
            await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            self._channel_lock_snapshots.pop(cache_key, None)
            raise
        # Keep the permission change on the fast path. Persist its restore
        # value asynchronously so database I/O cannot delay it.
        self.schedule_followup(
            self.bot.db.set_nested(interaction.guild_id, "security", lock_path, original_send_messages),
            "lock-saved-state",
        )
        self.schedule_followup(self.log(interaction.guild, f"{interaction.channel.mention} was locked by {interaction.user.mention}."), "lock-log")
        await send_card(interaction, moderation_card("Channel Locked!", moderator=interaction.user, channel=interaction.channel, status="Locked for @everyone"), ephemeral=True)

    @app_commands.command(name="unlock", description="Allow members to send messages in this channel")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            await send_card(interaction, notice_panel("Use `/unlock` in a text channel."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        lock_path = f"channel_lock_overwrites.{interaction.channel.id}"
        cache_key = (interaction.guild_id, interaction.channel.id)
        if cache_key in self._channel_lock_snapshots:
            previous_send_messages = self._channel_lock_snapshots[cache_key]
            has_saved = True
        else:
            security = (await self.bot.db.get_guild(interaction.guild_id)).get("security", {})
            saved = security.get("channel_lock_overwrites", {})
            has_saved = str(interaction.channel.id) in saved
            previous_send_messages = saved.get(str(interaction.channel.id))
        if overwrite.send_messages is not False and not has_saved:
            await send_card(interaction, notice_panel(f"{interaction.channel.mention} is already unlocked."), ephemeral=True)
            return
        overwrite.send_messages = previous_send_messages
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {interaction.user}")
        self._channel_lock_snapshots.pop(cache_key, None)
        if has_saved:
            self.schedule_followup(self.bot.db.unset_value(interaction.guild_id, f"security.{lock_path}"), "unlock-saved-state")
        self.schedule_followup(self.log(interaction.guild, f"{interaction.channel.mention} was unlocked by {interaction.user.mention}."), "unlock-log")
        await send_card(interaction, moderation_card("Channel Unlocked!", moderator=interaction.user, channel=interaction.channel, status="Unlocked for @everyone"), ephemeral=True)

    @app_commands.command(name="nickname", description="Change or reset a member's server nickname")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_nicknames=True)
    async def nickname(self, interaction: discord.Interaction, member: discord.Member, nickname: str | None = None):
        if not await self.target_guard(interaction, member): return
        if nickname and len(nickname) > 32:
            await send_card(interaction, notice_panel("Nicknames can be at most 32 characters."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await member.edit(nick=nickname, reason=f"Nickname changed by {interaction.user}")
        self.schedule_followup(self.log(interaction.guild, f"{member.mention}'s nickname was changed by {interaction.user.mention}."), "nickname-log")
        label = f"set to **{discord.utils.escape_markdown(nickname)}**" if nickname else "reset"
        await send_card(interaction, moderation_card("Nickname Updated", moderator=interaction.user, target=member, status=f"Nickname {label}"), ephemeral=True)

    def transferable_roles(self, guild: discord.Guild, roles):
        bot_member = guild.me
        if bot_member is None:
            return [], len(roles)
        eligible = [
            role for role in roles
            if not role.is_default()
            and not role.managed
            and not role.permissions.administrator
            and role < bot_member.top_role
        ]
        return eligible, len(roles) - len(eligible)

    @commands.command(name="transfer")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def prefix_transfer_roles(self, ctx: commands.Context, source: discord.Member, target: discord.Member):
        """Copy a member's manageable non-admin roles, then remove them from the source."""
        if source.id == target.id:
            await ctx.send(view=component_card(notice_panel("Choose two different members.")))
            return
        if source.id == ctx.guild.owner_id or target.id == ctx.guild.owner_id:
            await ctx.send(view=component_card(notice_panel("The server owner cannot be used in a role transfer.")))
            return
        if source.id == self.bot.user.id or target.id == self.bot.user.id:
            await ctx.send(view=component_card(notice_panel("The bot cannot be used in a role transfer.")))
            return

        roles, skipped = self.transferable_roles(ctx.guild, source.roles)
        if not roles:
            await ctx.send(view=component_card(notice_panel("No transferable roles found. Administrator, managed, @everyone, and roles above Zyven are skipped.")))
            return

        progress = await ctx.send(
            view=component_card(panel(
                f"{icon('loading')} Role Transfer in Progress",
                f"{icon('members')} **From:** {source.mention}\n"
                f"{icon('members')} **To:** {target.mention}\n\n"
                f"{icon('loading')} Saving recovery data and preparing `{len(roles)}` role(s)…",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # Save first so source roles remain recoverable even if Discord rejects
        # a later role update or the member leaves before manual recovery.
        try:
            await self.bot.db.save_role_recovery(ctx.guild.id, source.id, [role.id for role in roles])
            await progress.edit(view=component_card(panel(
                f"{icon('loading')} Role Transfer in Progress",
                f"{icon('wtick')} Recovery snapshot saved for {source.mention}.\n"
                f"{icon('loading')} Copying roles to {target.mention}, then removing them from {source.mention}…",
            )), allowed_mentions=discord.AllowedMentions.none())
            await target.add_roles(*roles, atomic=False, reason=f"Role transfer by {ctx.author} ({ctx.author.id})")
            await source.remove_roles(*roles, atomic=False, reason=f"Role transfer to {target} by {ctx.author} ({ctx.author.id})")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Role transfer failed in guild %s from %s to %s", ctx.guild.id, source.id, target.id)
            await progress.edit(view=component_card(notice_panel(
                f"Discord rejected part of the transfer. The recovery snapshot is saved for {source.mention}; check Zyven's role hierarchy and permissions.",
                title=f"{icon('error')} Role Transfer Failed",
            )), allowed_mentions=discord.AllowedMentions.none())
            return
        except Exception:
            log.exception("Could not save role recovery for transfer in guild %s", ctx.guild.id)
            await progress.edit(view=component_card(notice_panel(
                "Could not save the recovery snapshot, so no roles were moved. Check the database and try again.",
                title=f"{icon('error')} Role Transfer Failed",
            )), allowed_mentions=discord.AllowedMentions.none())
            return

        await progress.edit(
            view=component_card(panel(
                f"{icon('tick')} Roles Transferred",
                f"Copied `{len(roles)}` eligible role(s) from {source.mention} to {target.mention}, then removed them from {source.mention}.\n"
                f"Skipped `{skipped}` role(s), including administrator, managed, @everyone, or roles above Zyven.",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="recover")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    async def prefix_recover_roles(self, ctx: commands.Context, member: discord.Member):
        """Restore a member's saved transferable roles."""
        if member.id == ctx.guild.owner_id or member.id == self.bot.user.id:
            await ctx.send(view=component_card(notice_panel("The server owner and bot cannot use role recovery.")))
            return
        progress = await ctx.send(
            view=component_card(panel(
                f"{icon('loading')} Role Recovery in Progress",
                f"{icon('members')} **Member:** {member.mention}\n\n"
                f"{icon('loading')} Loading the saved roles and checking which can be restored…",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            recovery = await self.bot.db.get_role_recovery(ctx.guild.id, member.id)
        except Exception:
            log.exception("Could not load role recovery in guild %s for %s", ctx.guild.id, member.id)
            await progress.edit(view=component_card(notice_panel(
                "Could not read the saved roles because the database is unavailable. Try again shortly.",
                title=f"{icon('error')} Role Recovery Failed",
            )), allowed_mentions=discord.AllowedMentions.none())
            return
        if not recovery:
            await progress.edit(view=component_card(notice_panel(
                "No saved role recovery was found for that member in this server.",
                title=f"{icon('error')} No Recovery Data",
            )), allowed_mentions=discord.AllowedMentions.none())
            return
        raw_role_ids = recovery.get("role_ids", [])
        if not isinstance(raw_role_ids, (list, tuple, set)):
            raw_role_ids = []
        saved_roles = []
        for role_id in raw_role_ids:
            try:
                role = ctx.guild.get_role(int(role_id))
            except (TypeError, ValueError, OverflowError):
                continue
            if role is not None:
                saved_roles.append(role)
        roles, skipped = self.transferable_roles(ctx.guild, [role for role in saved_roles if role is not None])
        if not roles:
            await progress.edit(view=component_card(notice_panel(
                "No saved roles can currently be restored. Missing, administrator, managed, and above-Zyven roles are skipped.",
                title=f"{icon('error')} Nothing to Restore",
            )), allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            await progress.edit(view=component_card(panel(
                f"{icon('loading')} Role Recovery in Progress",
                f"{icon('wtick')} Found `{len(roles)}` restorable role(s) for {member.mention}.\n"
                f"{icon('loading')} Applying saved roles…",
            )), allowed_mentions=discord.AllowedMentions.none())
            await member.add_roles(*roles, atomic=False, reason=f"Role recovery by {ctx.author} ({ctx.author.id})")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Role recovery failed in guild %s for %s", ctx.guild.id, member.id)
            await progress.edit(view=component_card(notice_panel(
                "Discord rejected role recovery. Check Zyven's role hierarchy and permissions, then retry.",
                title=f"{icon('error')} Role Recovery Failed",
            )), allowed_mentions=discord.AllowedMentions.none())
            return
        await progress.edit(
            view=component_card(panel(
                f"{icon('tick')} Roles Recovered",
                f"Restored `{len(roles)}` saved role(s) to {member.mention}. Administrator roles are never restored; `{skipped}` role(s) were skipped.",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Automatically restore a prior transfer snapshot when its member rejoins."""
        try:
            recovery = await self.bot.db.get_role_recovery(member.guild.id, member.id)
            if not recovery:
                return
            guild_data = await self.bot.db.get_guild(member.guild.id)
            jailed_members = (guild_data.get("jail", {}).get("members", {}) or {})
            if str(member.id) in jailed_members or member.id in jailed_members:
                log.info("Skipping role recovery for jailed member %s in guild %s", member.id, member.guild.id)
                return
            await asyncio.sleep(1.0)
            saved_roles = [member.guild.get_role(int(role_id)) for role_id in recovery.get("role_ids", [])]
            roles, _skipped = self.transferable_roles(member.guild, [role for role in saved_roles if role is not None])
            if not roles:
                return
            await member.add_roles(*roles, atomic=False, reason="Automatic role recovery after rejoin")
            log.info("Restored %s saved roles to rejoining member %s in guild %s", len(roles), member.id, member.guild.id)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Automatic role recovery failed for member %s in guild %s; check role hierarchy", member.id, member.guild.id)
        except Exception:
            log.exception("Could not check role recovery for rejoining member %s in guild %s", member.id, member.guild.id)

    @app_commands.command(name="role", description="Add or remove a role from a member")
    @app_commands.guild_only()
    @owner_bypass_permissions(manage_roles=True)
    @app_commands.choices(action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")])
    async def role(self, interaction: discord.Interaction, action: app_commands.Choice[str], member: discord.Member, role: discord.Role):
        if not await self.target_guard(interaction, member): return
        if role.is_default() or role.managed:
            await send_card(interaction, notice_panel("That role cannot be assigned manually."), ephemeral=True)
            return
        if interaction.user.id not in {interaction.guild.owner_id, self.bot.owner_id} and role >= interaction.user.top_role:
            await send_card(interaction, notice_panel("You can only manage roles below your highest role."), ephemeral=True)
            return
        if interaction.guild.me is None or role >= interaction.guild.me.top_role:
            await send_card(interaction, notice_panel("Move the bot role above the role you want to manage."), ephemeral=True)
            return
        adding = action.value == "add"
        verb = "added to" if adding else "removed from"
        await interaction.response.send_message(
            view=component_card(panel(
                f"{icon('loading')} Role Update in Progress",
                f"{icon('loading')} {'Adding' if adding else 'Removing'} {role.mention} {'to' if adding else 'from'} {member.mention}…",
            )),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            if adding:
                await member.add_roles(role, reason=f"Role added by {interaction.user}")
            else:
                await member.remove_roles(role, reason=f"Role removed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.edit_original_response(view=component_card(notice_panel(
                "Zyven could not update that member's role. Check permissions and role position.",
                title=f"{icon('error')} Role Update Failed",
            )))
            return
        self.schedule_followup(self.log(interaction.guild, f"{role.mention} was {verb} {member.mention} by {interaction.user.mention}."), "role-log")
        await interaction.edit_original_response(
            view=component_card(panel(
                f"{icon('tick')} Member Role Updated",
                f"{role.mention} was {verb} {member.mention}.",
            )),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="warn", description="Record a warning for a server member")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not await self.target_guard(interaction, member, require_bot_hierarchy=False): return
        reason = reason[:1000]
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.db.warnings.insert_one({
            "guild_id": str(interaction.guild_id),
            "member_id": member.id,
            "moderator_id": interaction.user.id,
            "reason": reason[:1000],
            "created_at": datetime.now(timezone.utc),
        })
        count = await self.bot.db.warnings.count_documents({"guild_id": str(interaction.guild_id), "member_id": member.id})
        dm_done = False
        try:
            await member.send(view=component_card(panel(f"{icon('warning')} Warning from {interaction.guild.name}", f"**Reason:** {reason[:1000]}")))
            dm_done = True
        except (discord.Forbidden, discord.HTTPException):
            pass
        self.schedule_followup(self.log(interaction.guild, f"{icon('warning')} {member.mention} was warned by {interaction.user.mention}. Case `{result.inserted_id}` • Total warnings: {count}\nReason: {reason}"), "warn-log")
        await send_card(interaction, moderation_card("Warning Recorded", moderator=interaction.user, target=member, reason=reason, status=f"Warning #{count}", dm_done=dm_done), ephemeral=True)

    @app_commands.command(name="warnings", description="View a member's recent warning history")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.target_guard(interaction, member, require_bot_hierarchy=False): return
        await interaction.response.defer(ephemeral=True)
        cursor = (await self.bot.db.warnings.find({"guild_id": str(interaction.guild_id), "member_id": member.id})).sort("created_at", -1)
        records = await cursor.to_list(length=10)
        embed = panel(f"Warnings • {member}")
        if not records:
            embed.description = "No warnings recorded for this member."
        else:
            lines = [f"`{str(record['_id'])}` • {discord.utils.escape_markdown(record['reason'][:150])} — <@{record['moderator_id']}>" for record in records]
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Showing {len(records)} most recent warning(s) • Powered By Zyven Development™")
        await send_card(interaction, embed, ephemeral=True)

    @app_commands.command(name="unwarn", description="Remove one warning by its case ID")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def unwarn(self, interaction: discord.Interaction, member: discord.Member, case_id: str):
        if not await self.target_guard(interaction, member, require_bot_hierarchy=False): return
        try:
            warning_id = ObjectId(case_id)
        except (InvalidId, TypeError):
            await send_card(interaction, notice_panel("That is not a valid warning case ID. Use `/warnings` to find one."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.db.warnings.delete_one({
            "_id": warning_id,
            "guild_id": str(interaction.guild_id),
            "member_id": member.id,
        })
        if not result.deleted_count:
            await send_card(interaction, notice_panel("No warning with that case ID was found for this member in this server."), ephemeral=True)
            return
        self.schedule_followup(self.log(interaction.guild, f"Warning case `{warning_id}` for {member.mention} was removed by {interaction.user.mention}."), "warning-remove-log")
        await send_card(interaction, moderation_card("Warning Removed", moderator=interaction.user, target=member, status=f"Removed case `{warning_id}`"), ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear a member's warning history")
    @app_commands.guild_only()
    @owner_bypass_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.target_guard(interaction, member, require_bot_hierarchy=False): return
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.db.warnings.delete_many({"guild_id": str(interaction.guild_id), "member_id": member.id})
        self.schedule_followup(self.log(interaction.guild, f"{result.deleted_count} warning(s) for {member.mention} were cleared by {interaction.user.mention}."), "warning-clear-log")
        await send_card(interaction, moderation_card("Warnings Cleared", moderator=interaction.user, target=member, status=f"Removed {result.deleted_count} warning(s)"), ephemeral=True)


class NukeConfirmView(discord.ui.View):
    def __init__(self, bot, channel: discord.TextChannel, requester: discord.Member):
        super().__init__(timeout=60)
        self.bot, self.channel, self.requester = bot, channel, requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester.id:
            return True
        await interaction.response.send_message("Only the moderator who started this action can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", emoji=discord.PartialEmoji.from_str(icon("wtick")), style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = self.channel
        if interaction.user.id not in {channel.guild.owner_id, self.bot.owner_id} and not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("You need **Manage Channels** to confirm this action.", ephemeral=True)
            return
        if channel.guild.me is None or not channel.permissions_for(channel.guild.me).manage_channels:
            await interaction.response.send_message("Zyven needs **Manage Channels** to recreate this channel.", ephemeral=True)
            return
        await interaction.response.defer()
        replacement = None
        try:
            # Keep synced channels inheriting from their category. For an
            # unsynced channel, copy its role/member permission overwrites.
            synced = channel.permissions_synced
            channel_options = {
                "name": channel.name,
                "category": channel.category,
                "position": channel.position,
                "topic": channel.topic,
                "nsfw": channel.nsfw,
                "slowmode_delay": channel.slowmode_delay,
                "news": channel.is_news(),
                "default_auto_archive_duration": channel.default_auto_archive_duration,
                "default_thread_slowmode_delay": channel.default_thread_slowmode_delay,
                "reason": f"Channel nuked by {interaction.user}",
            }
            if not synced:
                channel_options["overwrites"] = dict(channel.overwrites)
            replacement = await channel.guild.create_text_channel(
                **channel_options,
            )
            await channel.delete(reason=f"Channel nuked by {interaction.user}")
            try:
                await replacement.edit(position=channel.position, reason="Restore the original channel position after nuke")
            except discord.HTTPException:
                log.warning("Could not restore channel position for recreated channel %s", replacement.id)
            await replacement.send(
                view=component_card(moderation_card("Channel Nuked!", moderator=interaction.user, channel=replacement, status="Successfully nuked")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._log(replacement.guild, replacement, interaction.user)
            self.stop()
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Nuke operation failed for channel %s", channel.id)
            if replacement is not None and channel.id in {item.id for item in channel.guild.channels}:
                try:
                    await replacement.delete(reason="Roll back incomplete channel nuke")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Could not clean up incomplete nuke clone %s", replacement.id)
            try:
                await interaction.followup.send(view=component_card(notice_panel("Nuke failed. Check Zyven's Manage Channels permission and try again.")), ephemeral=True)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Cancel", emoji=discord.PartialEmoji.from_str(icon("delete")), style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=component_card(panel("Nuke Cancelled", "The channel was not changed.")))
        self.stop()

    async def _log(self, guild: discord.Guild, channel: discord.TextChannel, moderator):
        try:
            data = await self.bot.db.get_guild(guild.id)
            channel_id = data.get("security", {}).get("log_channel")
            log_channel = guild.get_channel(channel_id) if channel_id else None
            if log_channel:
                await log_channel.send(view=component_card(moderation_card("Channel Nuked!", moderator=moderator, channel=channel, status="Successfully nuked")), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not write nuke action to the configured moderation log for guild %s", guild.id)


async def setup(bot): await bot.add_cog(Moderation(bot))
