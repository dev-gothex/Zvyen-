import platform
import time
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.settings import settings
from utils.emojis import icon
from utils.ui import component_card, edit_component_card, panel, send_component_card, send_card

log = logging.getLogger("zyven.information")


async def team_owner_only(ctx: commands.Context) -> bool:
    return settings.is_owner(ctx.author.id)


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


class MemberListView(discord.ui.View):
    PAGE_SIZE = 15

    def __init__(self, guild: discord.Guild, requester: discord.abc.User, title: str, entries: list[str]):
        super().__init__(timeout=180)
        self.guild = guild
        self.requester_id = requester.id
        self.requester = requester
        self.title = title
        self.entries = entries
        self.page = 0
        for action, emoji_name in (("first", "left"), ("previous", "previous"), ("delete", "delete"), ("next", "skip"), ("last", "right")):
            button = discord.ui.Button(
                emoji=discord.PartialEmoji.from_str(icon(emoji_name)),
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            button.callback = self.make_callback(action)
            self.add_item(button)

    @property
    def pages(self) -> int:
        return max(1, (len(self.entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def make_embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        current = self.entries[start:start + self.PAGE_SIZE]
        lines = [f"**{index + start + 1:02}.** {entry}" for index, entry in enumerate(current)]
        embed = panel(f"{self.title} [{len(self.entries)}]", "\n".join(lines) if lines else "No entries found.")
        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.url)
        embed.set_footer(text=f"Requested by {self.requester} • Page {self.page + 1}/{self.pages}", icon_url=self.requester.display_avatar.url)
        return embed

    def make_callback(self, action: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message("Only the person who requested this list can use its buttons.", ephemeral=True)
                return
            if action == "delete":
                await interaction.message.delete()
                return
            if action == "first":
                self.page = 0
            elif action == "previous":
                self.page = max(0, self.page - 1)
            elif action == "next":
                self.page = min(self.pages - 1, self.page + 1)
            else:
                self.page = self.pages - 1
            await interaction.response.edit_message(view=component_card(self.make_embed(), self))
        return callback


async def realtime_latency(bot, request_started: float) -> str:
    database_latency = getattr(bot.db, "last_ping_ms", None)
    ping_at = getattr(bot.db, "last_ping_at", None)
    if database_latency is None:
        database_value = "Warming up"
    else:
        age = max(0, int(time.time() - ping_at)) if ping_at else 0
        database_value = f"{database_latency:.2f}ms (checked {age}s ago)"
    message_latency = (time.perf_counter() - request_started) * 1000
    return (
        f"> **__Bot Latency__:** `{bot.latency * 1000:.2f}ms`\n"
        f"> **__Local SQLite Database__:** `{database_value}`\n"
        f"> **__Message Latency__:** `{message_latency:.2f}ms`"
    )


class BotInfoView(discord.ui.View):
    TEAM = {"Owners": [], "Developers": [], "Core Team": []}

    def __init__(self, bot, requester):
        super().__init__(timeout=240)
        self.bot = bot
        self.requester = requester
        self.requester_id = requester.id
        for key, label in (("system", "System"), ("general", "General"), ("team", "Team Info"), ("partners", "Partner Servers")):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=0)
            button.callback = self.tab_callback(key)
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who requested this bot information can use these buttons.", ephemeral=True)
        return False

    def tab_callback(self, tab: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                embed=await self.make_embed(tab),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return callback

    @staticmethod
    def _memory_stats() -> tuple[str, str, str, str]:
        total = available = process = None
        try:
            memory = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, value = line.split(":", 1)
                memory[key] = int(value.strip().split()[0]) * 1024
            total = memory.get("MemTotal")
            available = memory.get("MemAvailable")
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    process = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass

        def size(value):
            if value is None:
                return "Unavailable"
            return f"{value / (1024 ** 3):.2f} GB" if value >= 1024 ** 3 else f"{value / (1024 ** 2):.0f} MB"

        usage = f"{(1 - available / total) * 100:.0f}%" if total and available is not None else "Unavailable"
        return size(total), size(available), size(process), usage

    @staticmethod
    def _codebase_stats() -> tuple[int, int, int]:
        root = Path(__file__).resolve().parent.parent
        excluded = {"__pycache__", ".git", ".venv", "venv", "data", "logs", "backups"}
        files = [
            path for path in root.rglob("*.py")
            if not any(part in excluded for part in path.relative_to(root).parts)
        ]
        lines = words = 0
        for path in files:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines += len(source.splitlines())
            words += len(source.split())
        return len(files), lines, words

    def _decorate(self, embed: discord.Embed):
        embed.color = discord.Color.from_rgb(255, 255, 255)
        user = self.bot.user
        if user is not None:
            embed.set_thumbnail(url=user.display_avatar.url)
        requested_at = datetime.now().astimezone().strftime("Today at %H:%M")
        embed.set_footer(text=f"Requested by {self.requester.display_name} • {requested_at}")
        return embed

    async def make_embed(self, tab: str, request_started: float | None = None) -> discord.Embed:
        if tab == "system":
            total_ram, available_ram, process_ram, ram_usage = self._memory_stats()
            cpu_model = platform.processor() or platform.machine() or "Unavailable"
            cpu_frequency = "Unavailable"
            try:
                for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.lower().startswith("cpu mhz"):
                        cpu_frequency = f"{float(line.split(':', 1)[1]):.0f} MHz"
                        break
                    if line.lower().startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
            except (OSError, ValueError, IndexError):
                pass
            embed = panel(f"{self.bot.user.display_name if self.bot.user else 'Zyven'} Statistics")
            embed.add_field(name="Hardware", value=f"**CPU Cores** `{os.cpu_count() or 'N/A'}`\n**Frequency** `{cpu_frequency}`\n**Architecture** `{platform.machine() or 'N/A'}`\n**Processor** `{cpu_model}`", inline=True)
            embed.add_field(name="Memory", value=f"**Total RAM** `{total_ram}`\n**Available** `{available_ram}`\n**RAM Usage** `{ram_usage}`\n**Process RSS** `{process_ram}`", inline=True)
            embed.add_field(name="Platform", value=f"**OS** `{platform.system()} {platform.release()}`\n**Python** `{platform.python_version()}`\n**Discord.py** `{discord.__version__}`\n**Libraries** `{len(sys.modules)}`", inline=True)
            voice_clients = len(getattr(self.bot, "voice_clients", []))
            embed.add_field(name="Network", value=f"**Shards** `{self.bot.shard_count or 1}`\n**Ping** `{round(self.bot.latency * 1000)}ms`\n**Voice Clients** `{voice_clients}`", inline=True)
            started = getattr(self.bot, "started_at", time.monotonic())
            uptime = time.monotonic() - started
            since = datetime.fromtimestamp(time.time() - uptime).astimezone().strftime("%b %d, %Y %H:%M")
            embed.add_field(name="Uptime", value=f"**Duration** `{duration(uptime)}`\n**Since** `{since}`", inline=True)
            command_count = sum(1 for _ in self.bot.walk_commands())
            slash_count = sum(1 for _ in self.bot.tree.walk_commands())
            embed.add_field(name="Commands", value=f"**Prefix** `{command_count}`\n**Slash** `{slash_count}`\n**Cogs** `{len(self.bot.cogs)}`", inline=True)
        elif tab == "general":
            user = self.bot.user
            embed = panel(f"{user.display_name if user else 'Zyven'} Information")
            user_flags = getattr(user, "public_flags", None)
            verified = getattr(user_flags, "verified_bot", None)
            verified_text = "Yes" if verified is True else "No" if verified is False else "Unknown"
            created = user.created_at.strftime("%d %b %Y") if user else "Unavailable"
            embed.add_field(name="Bot Core", value=f"**Name** `{user.name if user else 'Zyven'}`\n**ID** `{user.id if user else 'Unavailable'}`\n**Created** `{created}`\n**Verified** `{verified_text}`", inline=True)
            file_count, line_count, word_count = self._codebase_stats()
            embed.add_field(name="Codebase", value=f"**Python Files** `{file_count:,}`\n**Lines** `{line_count:,}`\n**Words** `{word_count:,}`\n**Framework** `discord.py`", inline=True)
            member_count = sum(guild.member_count or 0 for guild in self.bot.guilds)
            embed.add_field(name="Network", value=f"**Ping** `{round(self.bot.latency * 1000)}ms`\n**Servers** `{len(self.bot.guilds):,}`\n**Users** `{member_count:,}`", inline=True)
        elif tab == "team":
            embed = panel("Team Information", color=0xFFFFFF)
            sections = []
            team_collection = self.bot.db.db["bot_team"]
            for group, people in self.TEAM.items():
                if group == "Owners":
                    owner = self.bot.get_user(settings.owner_id)
                    if owner is None:
                        owner = next(
                            (guild.get_member(settings.owner_id) for guild in self.bot.guilds if guild.get_member(settings.owner_id)),
                            None,
                        )
                    if owner is None and getattr(self.requester, "id", None) == settings.owner_id:
                        owner = self.requester
                    if owner is None:
                        try:
                            owner = await self.bot.fetch_user(settings.owner_id)
                        except (discord.HTTPException, discord.NotFound):
                            owner = None
                    if owner is not None:
                        display_name = getattr(owner, "display_name", None) or getattr(owner, "global_name", None) or owner.name
                        people = [(display_name, owner.name, owner.id)]
                else:
                    key = "core_team" if group == "Core Team" else "developers"
                    record = await team_collection.find_one({"_id": key})
                    people = [(entry.get("display_name", "Member"), entry.get("username", ""), entry.get("user_id")) for entry in (record or {}).get("members", [])]
                lines = []
                for index, (name, handle, user_id) in enumerate(people, 1):
                    safe_name = discord.utils.escape_markdown(name)
                    linked_name = f"[{safe_name}](https://discord.com/users/{user_id})" if user_id else safe_name
                    lines.append(f"**{index}.** {linked_name} — `{discord.utils.escape_markdown(handle)}`")
                if not lines:
                    lines = ["None added"]
                sections.append(f"**__{group}__**\n" + "\n".join(lines))
            sections.append("**__Bug Hunters__**\nNone found")
            embed.description = "\n\n".join(sections)
        else:
            embed = panel("Trusted By")
            top_servers = sorted(self.bot.guilds, key=lambda guild: guild.member_count or 0, reverse=True)[:2]
            if top_servers:
                lines = ["**Top Servers**"]
                for index, guild in enumerate(top_servers, 1):
                    server_url = guild.vanity_url or f"https://discord.com/channels/{guild.id}"
                    server_name = discord.utils.escape_markdown(guild.name)
                    lines.append(f"**{index}.** [{server_name}]({server_url})\n• Members: `{guild.member_count or 0:,}`")
                embed.description = "\n\n".join(lines)
            else:
                embed.description = "The bot has not joined any servers yet."
        return self._decorate(embed)


class Information(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="team", invoke_without_command=True, case_insensitive=True, hidden=True)
    async def team(self, ctx: commands.Context):
        """Manage the Developers and Core Team shown in .botinfo."""
        if ctx.author.id != settings.owner_id:
            return
        await ctx.send(view=component_card(panel("Team Commands", "Use `.team developers add @user`, `.team core add @user`, or the matching `remove` command.")), mention_author=False)

    async def _team_change(self, ctx: commands.Context, group: str, action: str, member: discord.User | discord.Member):
        if ctx.author.id != settings.owner_id:
            return
        collection = self.bot.db.db["bot_team"]
        key = "developers" if group == "developers" else "core_team"
        record = await collection.find_one({"_id": key}) or {"_id": key, "members": []}
        members = list(record.get("members", []))
        existing = next((entry for entry in members if int(entry.get("user_id", 0)) == member.id), None)
        if action == "add":
            if existing:
                return await ctx.send(view=component_card(panel("Already on Team", f"{member.mention} is already listed under **{group.title()}**.")), mention_author=False)
            members.append({"user_id": member.id, "display_name": member.display_name, "username": member.name})
            title = "Team Member Added"
            message = f"{member.mention} was added to **{group.title()}**."
        else:
            if not existing:
                return await ctx.send(view=component_card(panel("Team Member Not Found", f"{member.mention} is not listed under **{group.title()}**.")), mention_author=False)
            members = [entry for entry in members if int(entry.get("user_id", 0)) != member.id]
            title = "Team Member Removed"
            message = f"{member.mention} was removed from **{group.title()}**."
        await collection.update_one({"_id": key}, {"$set": {"members": members}}, upsert=True)
        await ctx.send(view=component_card(panel(title, message, color=0xFFFFFF)), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @team.group(name="developers", invoke_without_command=True, case_insensitive=True)
    async def team_developers(self, ctx: commands.Context):
        if ctx.author.id == settings.owner_id:
            await ctx.send(view=component_card(panel("Developers", "Use `.team developers add @user` or `.team developers remove @user`.")), mention_author=False)

    @team_developers.command(name="add")
    @commands.check(team_owner_only)
    async def team_developers_add(self, ctx: commands.Context, member: discord.Member | discord.User):
        await self._team_change(ctx, "developers", "add", member)

    @team_developers.command(name="remove", aliases=("delete", "del"))
    @commands.check(team_owner_only)
    async def team_developers_remove(self, ctx: commands.Context, member: discord.Member | discord.User):
        await self._team_change(ctx, "developers", "remove", member)

    @team.group(name="core", invoke_without_command=True, case_insensitive=True)
    async def team_core(self, ctx: commands.Context):
        if ctx.author.id == settings.owner_id:
            await ctx.send(view=component_card(panel("Core Team", "Use `.team core add @user` or `.team core remove @user`.")), mention_author=False)

    @team_core.command(name="add")
    @commands.check(team_owner_only)
    async def team_core_add(self, ctx: commands.Context, member: discord.Member | discord.User):
        await self._team_change(ctx, "core", "add", member)

    @team_core.command(name="remove", aliases=("delete", "del"))
    @commands.check(team_owner_only)
    async def team_core_remove(self, ctx: commands.Context, member: discord.Member | discord.User):
        await self._team_change(ctx, "core", "remove", member)

    @commands.command(name="prefix", case_insensitive=True)
    @commands.guild_only()
    async def prefix_command(self, ctx: commands.Context, *, new_prefix: str | None = None):
        """View or change this server's command prefix."""
        config = await self.bot.db.get_guild(ctx.guild.id)
        current = str(config.get("prefix", {}).get("value") or settings.prefix)
        if new_prefix is None:
            return await ctx.send(
                view=component_card(panel("Current Prefix", f"This server's prefix is `{current}`.")),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if ctx.author.id != settings.owner_id and not ctx.author.guild_permissions.manage_guild:
            return await ctx.send(
                view=component_card(panel(f"{icon('error')} Permission Required", "You need **Manage Server** permission to change the prefix.", color=0xED4245)),
                mention_author=False,
            )
        new_prefix = new_prefix.strip()
        if not new_prefix or len(new_prefix) > 5 or any(char.isspace() for char in new_prefix) or "@" in new_prefix:
            return await ctx.send(
                view=component_card(panel("Invalid Prefix", "Choose a prefix with 1–5 non-space characters.")),
                mention_author=False,
            )
        await self.bot.db.set_nested(ctx.guild.id, "prefix", "value", new_prefix)
        self.bot._guild_prefix_cache.pop(ctx.guild.id, None)
        embed = panel("Prefix Updated!", f"{icon('approve')} `{new_prefix}`\n{icon('member_human')} Moderator : {ctx.author.mention}\n\n{icon('arrow')} Server : {ctx.guild.name}\n{icon('arrow')} Use `{new_prefix}help` for more", color=0x2B2D31)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def latency_panel(self, request_started: float) -> discord.Embed:
        embed = panel(f"{icon('latency')} Realtime Latency")
        embed.description = await realtime_latency(self.bot, request_started)
        return embed

    @commands.command(name="ping", aliases=("latency",), case_insensitive=True)
    async def prefix_ping(self, ctx: commands.Context):
        """Show live Discord gateway, local SQLite, and command response latency."""
        request_started = time.perf_counter()
        embed = await self.latency_panel(request_started)
        await ctx.send(view=component_card(embed), mention_author=False)

    @app_commands.command(name="ping", description="Check Zyven's Discord and database connection")
    async def ping(self, interaction: discord.Interaction):
        request_started = time.perf_counter()
        await interaction.response.defer(ephemeral=True)
        embed = await self.latency_panel(request_started)
        await send_card(interaction, embed, ephemeral=True)

    @app_commands.command(name="botinfo", description="View Zyven system, general, and team information")
    async def botinfo(self, interaction: discord.Interaction):
        view = BotInfoView(self.bot, interaction.user)
        await interaction.response.send_message(
            embed=await view.make_embed("system", time.perf_counter()),
            view=view,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="stats", aliases=("botinfo", "about"), case_insensitive=True)
    async def prefix_stats(self, ctx: commands.Context):
        """Show bot status, realtime latency, and information tabs."""
        request_started = time.perf_counter()
        view = BotInfoView(self.bot, ctx.author)
        embed = await view.make_embed("system", request_started)
        await ctx.send(embed=embed, view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="serverinfo", description="Show information about this server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        view = ServerInfoView(interaction.guild, interaction.user)
        await interaction.response.send_message(view=view, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="serverinfo", aliases=("si",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_serverinfo(self, ctx: commands.Context):
        await ctx.send(view=ServerInfoView(ctx.guild, ctx.author), mention_author=False)

    @app_commands.command(name="userinfo", description="Show information about a server member")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        view = UserInfoView(member, interaction.user, interaction.guild, presence_enabled=self.bot.intents.presences)
        await interaction.response.send_message(view=view, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="userinfo", aliases=("user", "whois"), case_insensitive=True)
    @commands.guild_only()
    async def prefix_userinfo(self, ctx: commands.Context, member: discord.Member | None = None):
        member = member or ctx.author
        await ctx.send(view=UserInfoView(member, ctx.author, ctx.guild, presence_enabled=self.bot.intents.presences), mention_author=False)

    @app_commands.command(name="avatar", description="Show a member's avatar in full size")
    @app_commands.guild_only()
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        await send_card(interaction, avatar_embed(member, interaction.user), asset_download_view("Avatar", member.display_avatar), ephemeral=False)

    @commands.command(name="avatar", aliases=("av",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_avatar(self, ctx: commands.Context, member: discord.Member | None = None):
        member = member or ctx.author
        await ctx.send(view=component_card(avatar_embed(member, ctx.author), asset_download_view("Avatar", member.display_avatar)), mention_author=False)

    @app_commands.command(name="roleinfo", description="Show details and key permissions for a server role")
    @app_commands.guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        await send_card(interaction, role_info_embed(role, interaction.user), ephemeral=False)

    # `role` is the moderation role-management group; keep roleinfo/ri for lookup.
    @commands.command(name="roleinfo", aliases=("ri",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_roleinfo(self, ctx: commands.Context, *, role: discord.Role):
        await ctx.send(view=component_card(role_info_embed(role, ctx.author)), mention_author=False)

    @commands.command(name="membercount", aliases=("mc",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_membercount(self, ctx: commands.Context):
        """Show the current server member, human, bot, and available status counts."""
        guild = ctx.guild
        members = guild.members
        humans = sum(not member.bot for member in members)
        bots = sum(member.bot for member in members)
        total = guild.member_count or len(members)

        # Presence data is a privileged intent and may not be enabled for the
        # application. Only show status totals when Discord is sending them.
        stats = (
            f">>> {icon('member_total_anim')} Total Members: {total:,}\n"
            f"{icon('member_human')} Total Humans: {humans:,}\n"
            f"{icon('member_bots_anim')} Total Bots: {bots:,}"
        )
        if self.bot.intents.presences:
            online = sum(member.status == discord.Status.online for member in members)
            dnd = sum(member.status == discord.Status.dnd for member in members)
            idle = sum(member.status == discord.Status.idle for member in members)
            offline = sum(member.status == discord.Status.offline for member in members)
            stats += (
                f"\n{icon('status_online')} Online: {online:,}"
                f"\n{icon('status_dnd')} Dnd: {dnd:,}"
                f"\n{icon('status_idle')} Idle: {idle:,}"
                f"\n{icon('status_offline')} Offline: {offline:,}"
            )
        embed = panel(f"{icon('stats_members')} Member Statistics", stats)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="users", case_insensitive=True)
    async def prefix_users(self, ctx: commands.Context):
        """Show the bot-wide user, server, and shard statistics."""
        total_users = sum(guild.member_count or 0 for guild in self.bot.guilds)
        shard_count = self.bot.shard_count or 1
        shard_id = getattr(ctx.guild, "shard_id", None) if ctx.guild else None
        shard_number = (shard_id + 1) if shard_id is not None else 1
        stats = (
            f">>> {icon('stats_users')} **Total Users :** **{total_users:,}**\n"
            f"{icon('stats_guilds')} **Guild Count :** **{len(self.bot.guilds):,}**\n"
            f"{icon('stats_shards')} **Total Shards :** **{shard_count:,}**\n"
            f"{icon('stats_shard_id')} **Shard Id :** **{shard_number}/{shard_count}**"
        )
        embed = panel(f"{icon('stats_members')} Zyven Statistics", stats)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="status", case_insensitive=True)
    @commands.guild_only()
    async def prefix_status(self, ctx: commands.Context, member: discord.Member | None = None):
        """Show a member's current online status and activity."""
        member = member or ctx.author
        if not self.bot.intents.presences:
            embed = panel(
                f"{member.display_name} • Status",
                "Presence data is not enabled for this bot. Enable **Presence Intent** in the Developer Portal and in bot intents to see live status and activities.",
                color=0xFEE75C,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            return await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        status = member.status.name.replace("dnd", "Do Not Disturb").title()
        activities = []
        for activity in member.activities:
            if isinstance(activity, discord.CustomActivity):
                if activity.name:
                    activities.append(f"Custom Status: {discord.utils.escape_markdown(activity.name)}")
            elif activity.name:
                activities.append(f"{activity.type.name.title()}: {discord.utils.escape_markdown(activity.name)}")
        embed = panel(f"{member.display_name} • Status", f"**Status:** `{status}`\n" + ("\n".join(activities) if activities else "**Activity:** `None`"))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="servericon", aliases=("guildicon",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_servericon(self, ctx: commands.Context):
        """Show this server's icon at full size."""
        icon_asset = ctx.guild.icon
        if icon_asset is None:
            return await ctx.send(view=component_card(panel("Server Icon", "This server has no icon set.")), mention_author=False)
        url = icon_asset.with_size(2048).url
        embed = panel(f"{ctx.guild.name} • Server Icon", f"[Open full-size image]({url})")
        embed.set_image(url=url)
        view = discord.ui.View(timeout=180)
        view.add_item(discord.ui.Button(label="Download Server Icon", style=discord.ButtonStyle.link, url=url))
        await ctx.send(view=component_card(embed, view), mention_author=False)

    @commands.command(name="poll", case_insensitive=True)
    @commands.guild_only()
    async def prefix_poll(self, ctx: commands.Context, *, question: str):
        """Create a simple Yes/No reaction poll."""
        question = question.strip()
        if not question:
            return await ctx.send(view=component_card(panel("Poll Usage", f"Use `{settings.prefix}poll <question>`.")), mention_author=False)
        embed = panel("Poll", f"**{discord.utils.escape_markdown(question[:1000])}**\n\n👍 Yes\n👎 No")
        embed.set_footer(text=f"Poll by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        poll_message = await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        try:
            await poll_message.add_reaction("👍")
            await poll_message.add_reaction("👎")
        except discord.HTTPException:
            log.warning("Could not add poll reactions to message %s", poll_message.id)

    @commands.group(name="list", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def list_group(self, ctx: commands.Context):
        """List server members, roles, emojis, and invites."""
        await ctx.send(view=component_card(panel(
            "General Lists",
            f"`{settings.prefix}list admins`, `bots`, `boosters`, `inrole <role>`, `emojis`, `invoice`, `early`, `roles`",
        )), mention_author=False)

    async def send_member_list(self, ctx: commands.Context, title: str, members):
        entries = [member.mention for member in members]
        view = MemberListView(ctx.guild, ctx.author, title, entries)
        await ctx.send(view=component_card(view.make_embed(), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @list_group.command(name="admins", case_insensitive=True)
    async def list_admins(self, ctx: commands.Context):
        members = sorted((m for m in ctx.guild.members if m.guild_permissions.administrator), key=lambda m: m.display_name.casefold())
        await self.send_member_list(ctx, "Admin's List", members)

    @list_group.command(name="bots", case_insensitive=True)
    async def list_bots(self, ctx: commands.Context):
        members = sorted((m for m in ctx.guild.members if m.bot), key=lambda m: m.display_name.casefold())
        await self.send_member_list(ctx, "Bot's List", members)

    @list_group.command(name="boosters", case_insensitive=True)
    async def list_boosters(self, ctx: commands.Context):
        members = sorted(ctx.guild.premium_subscribers, key=lambda m: m.display_name.casefold())
        await self.send_member_list(ctx, "Booster's List", members)

    @list_group.command(name="inrole", case_insensitive=True)
    async def list_inrole(self, ctx: commands.Context, *, role: discord.Role):
        members = sorted(role.members, key=lambda m: m.display_name.casefold())
        await self.send_member_list(ctx, f"Member's list in {role.name}", members)

    @list_group.command(name="emojis", aliases=("emoji",), case_insensitive=True)
    async def list_emojis(self, ctx: commands.Context):
        entries = [f"{emoji}  `:{emoji.name}:`" for emoji in sorted(ctx.guild.emojis, key=lambda item: item.name.casefold())]
        view = MemberListView(ctx.guild, ctx.author, "Server Emojis", entries)
        await ctx.send(view=component_card(view.make_embed(), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @list_group.command(name="invoice", aliases=("invites", "invite"), case_insensitive=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def list_invoice(self, ctx: commands.Context):
        try:
            invites = await ctx.guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return await ctx.send(view=component_card(panel("Invite List", "I need **Manage Server** to view server invites.")), mention_author=False)
        entries = [
            f"`{invite.code}` • {invite.channel.mention if invite.channel else 'Unknown channel'} • {invite.uses or 0} uses • {invite.inviter.mention if invite.inviter else 'Unknown inviter'}"
            for invite in sorted(invites, key=lambda item: item.uses or 0, reverse=True)
        ]
        view = MemberListView(ctx.guild, ctx.author, "Server Invites", entries)
        await ctx.send(view=component_card(view.make_embed(), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @list_group.command(name="early", case_insensitive=True)
    async def list_early(self, ctx: commands.Context):
        roles = [role for role in ctx.guild.roles if "early" in role.name.casefold()]
        members = sorted({member.id: member for role in roles for member in role.members}.values(), key=lambda m: m.display_name.casefold())
        role_names = ", ".join(role.name for role in roles)
        title = f"Early Members{f' • {role_names}' if role_names else ''}"
        await self.send_member_list(ctx, title, members)

    @list_group.command(name="roles", case_insensitive=True)
    async def list_roles(self, ctx: commands.Context):
        entries = [role.mention for role in sorted(ctx.guild.roles, key=lambda role: role.position, reverse=True) if not role.is_default()]
        view = MemberListView(ctx.guild, ctx.author, "Server Roles", entries)
        await ctx.send(view=component_card(view.make_embed(), view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="boostcount", aliases=("bc",), case_insensitive=True)
    @commands.guild_only()
    async def prefix_boostcount(self, ctx: commands.Context):
        """Show this server's boost level, total boosts, and cached booster count."""
        guild = ctx.guild
        embed = panel(f"{icon('stats_shards')} Boost Information of {guild.name}")
        embed.description = (
            f"> {icon('stats_shards')} **Boost Level:** `{guild.premium_tier}`\n"
            f"> {icon('stats_guilds')} **Total Boosts:** `{guild.premium_subscription_count or 0}`\n"
            f"> {icon('stats_users')} **Boosters:** `{len(guild.premium_subscribers)}`"
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(view=component_card(embed), mention_author=False, allowed_mentions=discord.AllowedMentions.none())


def requested_by(embed: discord.Embed, requester: discord.abc.User) -> discord.Embed:
    embed.set_footer(text=f"Requested by {requester}", icon_url=requester.display_avatar.url)
    return embed


def cv2_section(text: str, image_url: str | None = None):
    display = discord.ui.TextDisplay(text)
    if image_url:
        return discord.ui.Section(display, accessory=discord.ui.Thumbnail(media=image_url))
    return display


class ServerInfoView(discord.ui.LayoutView):
    """Reference-style server information card with working asset controls."""
    def __init__(self, guild: discord.Guild, requester: discord.abc.User):
        super().__init__(timeout=300)
        self.guild = guild
        self.requester = requester
        self.add_item(discord.ui.Container(*self._build_components()))

    def _build_components(self):
        guild = self.guild
        member_total = guild.member_count or len(guild.members)
        humans = sum(not member.bot for member in guild.members)
        bots = sum(member.bot for member in guild.members)
        description = guild.description or ""
        header = f"## {guild.name}"
        if description:
            header += f"\n{description}"
        header += (
            f"\n\n**General**\n"
            f"**ID:** `{guild.id}`\n"
            f"**Owner:** <@{guild.owner_id}>\n"
            f"**Created:** <t:{int(guild.created_at.timestamp())}:F>\n"
            f"**Region:** {getattr(guild, 'region', None) or 'Auto'}\n"
            f"**Vanity URL:** `{guild.vanity_url_code or 'None'}`"
        )
        image = guild.icon or guild.banner
        children = [cv2_section(header, image.url if image else None), discord.ui.Separator()]
        children.append(discord.ui.TextDisplay(
            f"**Members**\n> **Total:** `{member_total:,}`\n> **Humans:** `{humans:,}`\n> **Bots:** `{bots:,}`"
            f"\n\n**Channels**\n> **AFK:** {guild.afk_channel.mention if guild.afk_channel else 'None'}"
            f"\n> **Text:** `{len(guild.text_channels)}`\n> **Voice:** `{len(guild.voice_channels)}`"
            f"\n\n**Roles & Media**\n> **Roles:** `{max(0, len(guild.roles) - 1)}`"
            f"\n> **Emojis:** `{len(guild.emojis)}`\n> **Stickers:** `{len(guild.stickers)}`"
            f"\n\n**Boosting**\n> **Level:** `{guild.premium_tier}`\n> **Boosts:** `{guild.premium_subscription_count or 0}`"
            f"\n> **Booster Role:** {getattr(guild, 'premium_subscriber_role', None).mention if getattr(guild, 'premium_subscriber_role', None) else 'None'}"
        ))
        children.append(discord.ui.Separator())
        rules = guild.rules_channel.mention if guild.rules_channel else "None"
        updates = guild.public_updates_channel.mention if guild.public_updates_channel else "None"
        system = guild.system_channel.mention if guild.system_channel else "None"
        children.append(discord.ui.TextDisplay(
            f"**Security**\n> **Verification:** `{guild.verification_level.name.replace('_', ' ').title()}`"
            f"\n> **Content Filter:** `{guild.explicit_content_filter.name.replace('_', ' ').title()}`"
            f"\n> **2FA Requirement:** `{guild.mfa_level.name.replace('_', ' ').title()}`"
            f"\n\n**System Channels**\n> **Rules:** {rules}\n> **Updates:** {updates}\n> **System Msgs:** {system}"
        ))
        children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay(f"-# Requested By {self.requester.name} • Today at <t:{int(time.time())}:t>"))
        children.append(discord.ui.Separator())
        row = discord.ui.ActionRow()
        row.add_item(self._asset_button("Server Icon", guild.icon))
        row.add_item(self._asset_button("Server Banner", guild.banner))
        row.add_item(self._asset_button("Splash", guild.splash or guild.discovery_splash))
        features = discord.ui.Button(label="Features", style=discord.ButtonStyle.secondary)
        features.callback = self.show_features
        row.add_item(features)
        children.append(row)
        return children

    @staticmethod
    def _asset_button(label: str, asset):
        if asset:
            return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=asset.with_size(1024).url)
        return discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True)

    async def show_features(self, interaction: discord.Interaction):
        feature_lines = [f"• {feature.replace('_', ' ').title()}" for feature in self.guild.features]
        embed = panel(f"{self.guild.name} • Server Features", "\n".join(feature_lines) if feature_lines else "No special features are enabled.")
        await send_card(interaction, embed, ephemeral=True)


class UserInfoView(discord.ui.LayoutView):
    """User information card matching the supplied General/Dates/Roles layout."""
    def __init__(self, member: discord.Member, requester: discord.abc.User, guild: discord.Guild, *, presence_enabled: bool = False):
        super().__init__(timeout=300)
        self.member = member
        created = int(member.created_at.timestamp())
        joined = int(member.joined_at.timestamp()) if member.joined_at else None
        roles = [role.mention for role in reversed(member.roles) if role != guild.default_role]
        roles_text = ", ".join(roles[:20]) if roles else "No roles"
        if len(roles_text) > 1000:
            roles_text = roles_text[:995].rsplit(",", 1)[0] + "…"
        try:
            badges = [name.replace("_", " ").title() for name, enabled in member.public_flags if enabled]
        except (AttributeError, TypeError):
            badges = []
        badges_text = ", ".join(badges) if badges else "None"
        status_name = member.status.name
        status = {"dnd": "Dnd", "offline": "Offline", "invisible": "Offline"}.get(status_name, status_name.title()) if presence_enabled else "Unavailable"
        activity = member.activity
        if not presence_enabled:
            activity_text = "Unavailable"
        elif activity is None:
            activity_text = "None"
        elif isinstance(activity, discord.CustomActivity):
            activity_text = activity.name or "Custom Status"
        else:
            activity_text = f"{type(activity).__name__}: {getattr(activity, 'name', None) or 'Active'}"
        content = (
            f"## {member.display_name} • User Information\n"
            f"{member.mention} • `{member.name}`\n"
            f"**ID:** `{member.id}`\n\n"
            f"**Account**\n> **Created:** <t:{created}:F> • <t:{created}:R>\n"
            f"**Server**\n> **Joined:** {f'<t:{joined}:F> • <t:{joined}:R>' if joined else 'Unavailable'}\n"
            f"> **Top Role:** {member.top_role.mention}\n\n"
            f"**Profile**\n> **Bot:** `{'Yes' if member.bot else 'No'}`\n"
            f"> **Status:** `{status}`\n> **Activity:** `{activity_text}`\n> **Badges:** `{badges_text}`\n\n"
            f"**Roles**\n{roles_text}"
        )
        children = [cv2_section(content, member.display_avatar.with_size(512).url), discord.ui.Separator()]
        children.append(discord.ui.TextDisplay(f"-# Requested By {requester.name} • Today at <t:{int(time.time())}:t>"))
        children.append(discord.ui.Separator())
        row = discord.ui.ActionRow()
        for label, callback in (
            ("Avatar", self.show_avatar),
            ("Banner", self.show_banner),
            ("Permissions", self.show_permissions),
        ):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = callback
            row.add_item(button)
        children.append(row)
        self.add_item(discord.ui.Container(*children))

    async def show_avatar(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Avatar")
        embed.set_image(url=self.member.display_avatar.with_size(1024).url)
        await send_card(interaction, embed, asset_download_view("Avatar", self.member.display_avatar), ephemeral=True)

    async def show_banner(self, interaction: discord.Interaction):
        banner = self.member.banner
        if banner is None:
            try:
                # Guild Member payloads often omit user profile assets. Fetch
                # the User object so its banner field is populated from REST.
                profile = await interaction.client.fetch_user(self.member.id)
                banner = profile.banner
            except discord.HTTPException:
                log.exception("Could not fetch profile banner for user %s", self.member.id)
                await send_card(interaction, panel("Banner", "Could not load this member's banner right now. Please try again."), ephemeral=True)
                return
        if banner is None:
            await send_card(interaction, panel("Banner", "This member does not have a profile banner."), ephemeral=True)
            return
        embed = discord.Embed(title="Banner")
        embed.set_image(url=banner.with_size(1024).url)
        await send_card(interaction, embed, asset_download_view("Banner", banner), ephemeral=True)

    async def show_permissions(self, interaction: discord.Interaction):
        enabled = [
            " ".join(part.title() for part in name.split("_"))
            for name, value in self.member.guild_permissions
            if value
        ]
        description = "\n".join(f"{icon('whitetick')} {name}" for name in enabled)
        embed = discord.Embed(title="Key Permissions", description=description or "No permissions are enabled.")
        await send_card(interaction, embed, ephemeral=True)


def asset_download_view(label: str, asset: discord.Asset) -> discord.ui.View:
    """Provide a direct, original-size asset link below an image card."""
    view = discord.ui.View(timeout=180)
    view.add_item(discord.ui.Button(
        label=f"Download {label}",
        style=discord.ButtonStyle.link,
        url=asset.with_size(4096).url,
    ))
    return view


def avatar_embed(member: discord.Member, requester: discord.abc.User) -> discord.Embed:
    asset = member.display_avatar.with_size(1024)
    embed = panel(f"{icon('member')} {member.display_name} • Avatar", f"[Open full-size image]({asset.url})")
    embed.set_image(url=asset.url)
    requested_by(embed, requester)
    return embed


def role_info_embed(role: discord.Role, requester: discord.abc.User) -> discord.Embed:
    embed = panel(f"{icon('role')} {role.name} • Role Information")
    if role.icon:
        embed.set_thumbnail(url=role.icon.url)
    embed.description = f"{role.mention} • ID `{role.id}`"
    embed.add_field(name="Role Details", value=f"Created <t:{int(role.created_at.timestamp())}:D>\nPosition `{role.position}` • Members `{len(role.members)}`", inline=True)
    embed.add_field(name="Appearance", value=f"Color `#{role.color.value:06X}`\nHoisted `{role.hoist}` • Mentionable `{role.mentionable}`\nManaged `{role.managed}`", inline=True)
    permissions = role.permissions
    key_permissions = (("Administrator", permissions.administrator), ("Manage Server", permissions.manage_guild), ("Manage Roles", permissions.manage_roles), ("Manage Channels", permissions.manage_channels), ("Ban Members", permissions.ban_members), ("Kick Members", permissions.kick_members), ("Moderate Members", permissions.moderate_members), ("Manage Messages", permissions.manage_messages), ("Mention Everyone", permissions.mention_everyone), ("Manage Webhooks", permissions.manage_webhooks))
    enabled = [name for name, value in key_permissions if value]
    embed.add_field(name="Key Permissions", value=" • ".join(enabled) if enabled else "No elevated permissions", inline=False)
    requested_by(embed, requester)
    return embed


async def setup(bot):
    await bot.add_cog(Information(bot))
