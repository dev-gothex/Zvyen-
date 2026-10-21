import ast
import asyncio
import importlib.util
import io
import json
import logging
import os
import platform
import re
import sys
import time
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import discord
from discord.ext import commands

from utils.emojis import icon
from core.settings import settings
from utils.ui import component_card, panel


log = logging.getLogger("zyven.owner")
_DURATION_TOKEN = re.compile(r"(\d+)([dmy])")
_DURATION_SECONDS = {"d": 86_400, "m": 30 * 86_400, "y": 365 * 86_400}


def _audit_command_name(ctx: commands.Context) -> str:
    command = getattr(ctx, "command", None)
    name = getattr(command, "qualified_name", None) or getattr(command, "name", None)
    return str(name)[:100] if name else "unknown"


async def owner_only(ctx: commands.Context) -> bool:
    allowed = ctx.author.id == settings.owner_id
    if not allowed:
        cog = ctx.bot.get_cog("Owner")
        if cog:
            command_name = _audit_command_name(ctx)
            await cog.audit(ctx, "Owner command access denied", f"User: {ctx.author} (`{ctx.author.id}`)\nCommand: `{discord.utils.escape_markdown(command_name)}`", color=0xED4245)
    return allowed


def parse_duration(value: str) -> int | None:
    compact = value.lower().replace(" ", "")
    matches = list(_DURATION_TOKEN.finditer(compact))
    if not matches or "".join(match.group(0) for match in matches) != compact:
        raise ValueError("Use a duration such as `1d`, `2m`, or `1y`.")
    seconds = sum(int(match.group(1)) * _DURATION_SECONDS[match.group(2)] for match in matches)
    if seconds <= 0 or seconds > 50 * _DURATION_SECONDS["y"]:
        raise ValueError("Choose a duration between `1d` and `50y`.")
    return seconds


def display_duration(value: str | None) -> str:
    if value is None:
        return "Permanent"
    labels = {"d": "day", "m": "month", "y": "year"}
    parts = [f"{int(count)} {labels[unit]}{'s' if int(count) != 1 else ''}" for count, unit in _DURATION_TOKEN.findall(value.lower().replace(" ", ""))]
    return ", ".join(parts) or value


class OwnerHelpView(discord.ui.View):
    PAGES = [
        ("Bot Security", [
            ("ownerscan", "Run an isolated source, dependency, and command scan."),
            ("banall", "Ban all currently manageable members in the selected server after confirmation."),
            ("ownerstatus", "Show runtime health, uptime, latency, database, and loaded-module status."),
            ("perfstats", "Show rolling command latency, error, and slow-command metrics."),
            ("ratelimits [count]", "Show recent Discord global rate-limit events and retry windows."),
            ("blacklistserver <server_id> [reason]", "Block a server and make Zyven leave it."),
            ("unblacklistserver <server_id>", "Remove a server block."),
            ("blacklistservers", "List blocked servers."),
            ("globalban <user_id> [reason]", "Block a user from commands and interactions bot-wide."),
            ("globalunban <user_id> · globalbans", "Remove or list global user blocks."),
        ]),
        ("Server Management", [
            ("serverlist [page]", "List bot servers and show the invite action."),
            ("guildinfo <server_id>", "Inspect a server known to Zyven without changing it."),
            ("serverleave <server_id>", "Make the bot leave a server."),
            ("serverjoin", "Show the bot install link."),
            ("reloadcog <name>", "Reload one already-loaded feature module."),
            ("synccommands [server_id]", "Sync application commands globally or to one server."),
            ("branding [avatar|banner|bio|server|reset]", "Set server-specific owner-log branding."),
            ("team developers add|remove @user", "Update the bot Developers list."),
            ("team core add|remove @user", "Update the bot Core Team list."),
        ]),
        ("No-Prefix Access", [
            ("noprefixtrial setup", "Choose a trial channel and duration, then publish the claim card."),
            ("noprefix add @user|user_id [duration]", "Grant permanent or timed prefixless access."),
            ("noprefix remove @user|user_id", "Revoke prefixless access."),
            ("noprefix list", "List active access grants."),
            ("garib @user|user_id [duration]", "Quick alias to grant prefixless access."),
        ]),
        ("Owner Help & Rules", [
            ("ownerhelp [command]", "Show this paginated menu or details for one command."),
            ("help <command>", "Show command usage and setup details."),
            ("Duration formats", "Use `1d` for days, `2m` for 60 days, or `1y` for 365 days."),
            ("Prefixless use", "Only registered Zyven commands run without a prefix; expired grants are removed automatically."),
            ("Access", "Owner tools are restricted to the configured bot owner."),
        ]),
    ]

    def __init__(self, requester_id: int, prefix: str, page: int = 0):
        super().__init__(timeout=300)
        self.requester_id = int(requester_id)
        self.prefix = prefix
        self.page = max(0, min(page, len(self.PAGES) - 1))
        previous = discord.ui.Button(label="⏮", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
        previous.callback = self.previous
        self.add_item(previous)
        close = discord.ui.Button(label="✖", style=discord.ButtonStyle.danger, row=1)
        close.callback = self.close
        self.add_item(close)
        following = discord.ui.Button(label="⏭", style=discord.ButtonStyle.secondary, disabled=self.page + 1 >= len(self.PAGES), row=1)
        following.callback = self.next
        self.add_item(following)

    def make_embed(self) -> discord.Embed:
        title, entries = self.PAGES[self.page]
        lines = [f"• `{self.prefix}{command}` — {description}" for command, description in entries]
        embed = panel("Owner Commands", "\n".join(lines), color=0xFFFFFF)
        embed.set_footer(text=f"Page {self.page + 1}/{len(self.PAGES)} · Authorized bot owner only")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id or interaction.user.id != settings.owner_id:
            await interaction.response.send_message("Only the bot owner who opened this menu can use it.", ephemeral=True)
            return False
        return True

    async def previous(self, interaction: discord.Interaction):
        view = OwnerHelpView(self.requester_id, self.prefix, self.page - 1)
        await interaction.response.edit_message(view=component_card(view.make_embed(), view))

    async def next(self, interaction: discord.Interaction):
        view = OwnerHelpView(self.requester_id, self.prefix, self.page + 1)
        await interaction.response.edit_message(view=component_card(view.make_embed(), view))

    async def close(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=None)


class NoPrefixTrialClaimView(discord.ui.View):
    def __init__(self, duration_days: int):
        super().__init__(timeout=None)
        self.duration_days = int(duration_days)
        button = discord.ui.Button(
            label=f"Claim No-Prefix ({self.duration_days} days)",
            style=discord.ButtonStyle.primary,
            custom_id=f"zyven:noprefix_trial:claim:{self.duration_days}",
        )
        button.callback = self.claim
        self.add_item(button)

    async def claim(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Claim this trial from the server where it was posted.", ephemeral=True)
        granted = await interaction.client.db.claim_noprefix_trial(interaction.user.id, self.duration_days)
        if granted:
            interaction.client.invalidate_noprefix_cache()
            embed = panel("No-Prefix Trial Claimed", f"Your prefixless access is active for **{self.duration_days} days** and expires <t:{int(time.time()) + self.duration_days * 86400}:R>.", color=0xFFFFFF)
        else:
            previous = await interaction.client.db.noprefix_trial_claims.find_one({"_id": str(interaction.user.id)})
            if previous:
                embed = panel("Already Claimed", "You have already claimed your no-prefix trial.", color=0xFFFFFF)
            else:
                embed = panel("Access Already Active", "You already have active no-prefix access. This trial was not used.", color=0xFFFFFF)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class NoPrefixTrialSetupView(discord.ui.View):
    def __init__(self, owner_id: int, *, channel=None, duration_days: int = 14):
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.channel = channel
        self.duration_days = int(duration_days)
        select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Choose the trial claim channel…", min_values=1, max_values=1, row=0,
            default_values=[channel] if channel else [],
        )
        select.callback = self.select_channel
        self.add_item(select)
        for days in (7, 14, 30):
            button = discord.ui.Button(
                label=f"{days} Days", style=discord.ButtonStyle.secondary if days != self.duration_days else discord.ButtonStyle.primary, row=1,
            )
            button.callback = self.choose_duration(days)
            self.add_item(button)
        publish = discord.ui.Button(label="Publish Trial", style=discord.ButtonStyle.success, row=2)
        publish.callback = self.publish
        self.add_item(publish)

    def make_embed(self):
        channel_text = self.channel.mention if self.channel else "Not selected"
        return panel("No-Prefix Trial Setup", f"**Channel:** {channel_text}\n**Duration:** {self.duration_days} days\n\nChoose a text channel and trial length, then publish the claim card.", color=0xFFFFFF)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.user.id != settings.owner_id:
            await interaction.response.send_message("Only the bot owner can configure the no-prefix trial.", ephemeral=True)
            return False
        return True

    async def select_channel(self, interaction: discord.Interaction):
        self.channel = self.children[0].values[0]
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    def choose_duration(self, days: int):
        async def callback(interaction: discord.Interaction):
            self.duration_days = days
            refreshed = NoPrefixTrialSetupView(self.owner_id, channel=self.channel, duration_days=days)
            await interaction.response.edit_message(embed=refreshed.make_embed(), view=refreshed)
        return callback

    async def publish(self, interaction: discord.Interaction):
        if self.channel is None:
            return await interaction.response.send_message("Choose a channel before publishing the trial.", ephemeral=True)
        try:
            channel = interaction.guild.get_channel(int(self.channel.id))
            if channel is None:
                channel = await interaction.guild.fetch_channel(int(self.channel.id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return await interaction.response.send_message("Select a text channel where Zyven can send messages.", ephemeral=True)
            trial = discord.Embed(
                title="Get No-Prefix Access (Free Trial)",
                description=(
                    f"Claim your free no-prefix access for **{self.duration_days} days** — type commands without any prefix.\n\n"
                    "- You can claim this once\n- Access expires automatically"
                ),
                color=0xFFFFFF,
            )
            await channel.send(view=component_card(trial, NoPrefixTrialClaimView(self.duration_days)))
        except (discord.Forbidden, discord.HTTPException) as error:
            return await interaction.response.send_message(f"Could not publish the trial card: {error}", ephemeral=True)
        await interaction.response.edit_message(embed=panel("Trial Published", f"The **{self.duration_days}-day** claim card was posted in {channel.mention}.", color=0xFFFFFF), view=None)


def _scan_python_sources(root: Path) -> dict[str, object]:
    """Inspect source without importing modules or starting another bot process."""
    excluded = {
        ".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build",
        ".local", "site-packages", ".tox", ".mypy_cache", ".pytest_cache",
    }
    files = [path for path in root.rglob("*.py") if not excluded.intersection(path.parts)]
    files.sort()
    syntax_errors: list[str] = []
    parse_errors: list[str] = []
    import_roots: dict[str, set[str]] = {}
    local_roots = {path.stem for path in root.glob("*.py")}
    local_roots.update(path.name for path in root.iterdir() if path.is_dir() and path.name not in excluded)
    stdlib = getattr(sys, "stdlib_module_names", set())

    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8-sig")
            compile(source, relative, "exec")
        except SyntaxError as exc:
            syntax_errors.append(f"{relative}:{exc.lineno}: {exc.msg}")
            continue
        except (OSError, UnicodeError) as exc:
            parse_errors.append(f"{relative}: could not read source ({type(exc).__name__}: {exc})")
            continue
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            parse_errors.append(f"{relative}:{exc.lineno}: {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_roots.setdefault(alias.name.split(".")[0], set()).add(relative)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                import_roots.setdefault(node.module.split(".")[0], set()).add(relative)

    missing: list[str] = []
    for name, used_by in sorted(import_roots.items()):
        if name in stdlib or name in local_roots:
            continue
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(f"{name} (used by {', '.join(sorted(used_by)[:5])})")

    return {
        "files": files,
        "syntax": syntax_errors,
        "parse": parse_errors,
        "missing_imports": missing,
        "import_count": len(import_roots),
    }


class Owner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._guard_log_cache: dict[int, float] = {}
        self._owner_scan_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_ready(self):
        for duration_days in (7, 14, 30):
            self.bot.add_view(NoPrefixTrialClaimView(duration_days))

    async def audit_restricted_attempt(self, user, guild, channel, command_name: str):
        now = time.monotonic()
        if now < self._guard_log_cache.get(user.id, 0):
            return
        self._guard_log_cache[user.id] = now + 10
        details = SimpleNamespace(author=user, guild=guild, channel=channel)
        safe_command_name = str(command_name)[:100] or "unknown"
        await self.audit(details, "Restricted Command Attempt", f"Attempted command: `{discord.utils.escape_markdown(safe_command_name)}`\nThis user/server is blocked by owner policy.", color=0xED4245)

    async def audit(self, ctx, title: str, description: str, *, color: int = 0x5865F2):
        """Send a branded command/security event to the owner webhook."""
        if not settings.owner_log_webhook_url:
            return
        try:
            webhook = discord.Webhook.from_url(settings.owner_log_webhook_url, client=self.bot)
            embed = panel(title, description, color=color)
            embed.set_footer(text="Zyven Owner Logs")
            guild = getattr(ctx, "guild", None)
            channel = getattr(ctx, "channel", None)
            author = getattr(ctx, "author", None)
            if guild:
                embed.add_field(name="Server", value=f"{discord.utils.escape_markdown(guild.name)} (`{guild.id}`)", inline=False)
            if channel:
                embed.add_field(name="Channel", value=f"#{discord.utils.escape_markdown(getattr(channel, 'name', 'DM'))} (`{channel.id}`)", inline=True)
            if author:
                embed.add_field(name="User", value=f"{discord.utils.escape_markdown(str(author))} (`{author.id}`)", inline=True)
            brand = {}
            if guild:
                try:
                    brand = await self.bot.db.get_owner_branding(guild.id)
                except Exception:
                    pass
            if brand.get("avatar_url"):
                embed.set_thumbnail(url=brand["avatar_url"])
            elif guild and guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
            if brand.get("banner_url"):
                embed.set_image(url=brand["banner_url"])
            if brand.get("bio"):
                embed.add_field(name="Server Bio", value=brand["bio"][:1024], inline=False)
            await webhook.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            # A bad/removed webhook must not block bot commands.
            return

    @commands.Cog.listener()
    async def on_command(self, ctx: commands.Context):
        await self.audit(ctx, "Zyven Command Used", f"Command: `{discord.utils.escape_markdown(_audit_command_name(ctx))}`")

    @commands.command(name="ownerhelp", hidden=True)
    @commands.check(owner_only)
    async def ownerhelp(self, ctx: commands.Context, *, command_name: str | None = None):
        """Show owner command help, optionally with one command's usage/setup."""
        if command_name:
            command = self.bot.get_command(command_name)
            if command is None:
                return await ctx.send(view=component_card(panel("Owner Command Not Found", f"No command named `{discord.utils.escape_markdown(command_name)}` was found.")))
            usage = f"{ctx.prefix or settings.prefix}{command.qualified_name} {command.signature}".rstrip()
            detail = command.help or command.short_doc or "No description available."
            return await ctx.send(view=component_card(panel(f"Owner Command: {command.qualified_name}", f"**Usage:** `{usage}`\n\n{detail}")), allowed_mentions=discord.AllowedMentions.none())
        view = OwnerHelpView(ctx.author.id, ctx.prefix or settings.prefix)
        await ctx.send(view=component_card(view.make_embed(), view), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="ownerstatus", aliases=("ownerstats",), hidden=True)
    @commands.check(owner_only)
    async def owner_status(self, ctx: commands.Context):
        """Show owner-only runtime and dependency health information."""
        started_at = getattr(self.bot, "started_at", None)
        if not isinstance(started_at, (int, float)):
            started_at = time.monotonic()
        uptime = max(0, int(time.monotonic() - started_at))
        days, remainder = divmod(uptime, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_text = f"{days}d {hours}h {minutes}m {seconds}s"
        database = getattr(self.bot, "db", None)
        db_status = "unavailable"
        if database is not None:
            if getattr(database, "temporarily_unavailable", False):
                db_status = "temporarily unavailable"
            else:
                try:
                    await asyncio.wait_for(database.ping(), timeout=4)
                    db_status = "healthy"
                except Exception as error:
                    db_status = f"failed ({type(error).__name__})"
        member_count = sum(
            count for guild in self.bot.guilds
            if isinstance((count := getattr(guild, "member_count", 0)), int)
        )
        cogs = getattr(self.bot, "cogs", {})
        extensions = getattr(self.bot, "extensions", {})
        if not isinstance(cogs, dict):
            cogs = {}
        if not isinstance(extensions, dict):
            extensions = {}
        latency = getattr(self.bot, "latency", 0)
        if not isinstance(latency, (int, float)):
            latency = 0
        ready = self.bot.is_ready()
        if not isinstance(ready, bool):
            ready = False
        description = (
            f"**Uptime:** `{uptime_text}`\n"
            f"**Gateway latency:** `{latency * 1000:.0f} ms`\n"
            f"**Servers:** `{len(self.bot.guilds):,}`\n"
            f"**Visible members:** `{member_count:,}`\n"
            f"**Loaded cogs:** `{len(cogs):,}`\n"
            f"**Loaded extensions:** `{len(extensions):,}`\n"
            f"**Database:** `{db_status}`\n"
            f"**Ready:** `{ready}`"
        )
        await ctx.send(view=component_card(panel("Owner Runtime Status", description)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="perfstats", aliases=("performance",), hidden=True)
    @commands.check(owner_only)
    async def performance_stats(self, ctx: commands.Context):
        """Show bounded rolling command performance metrics."""
        snapshot_fn = getattr(self.bot, "performance_snapshot", None)
        snapshot = snapshot_fn() if callable(snapshot_fn) else {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot = {
            "samples": 0, "errors": 0, "average_ms": 0.0,
            "p95_ms": 0.0, "max_ms": 0.0, "slow_commands": 0,
            **snapshot,
        }
        description = (
            f"**Samples:** `{snapshot['samples']:,}`\n"
            f"**Errors:** `{snapshot['errors']:,}`\n"
            f"**Average:** `{snapshot['average_ms']:.2f} ms`\n"
            f"**P95:** `{snapshot['p95_ms']:.2f} ms`\n"
            f"**Maximum:** `{snapshot['max_ms']:.2f} ms`\n"
            f"**Slow commands (≥1s):** `{snapshot['slow_commands']:,}`"
        )
        await ctx.send(view=component_card(panel("Owner Performance Metrics", description)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="ratelimits", aliases=("ratelimitlog",), hidden=True)
    @commands.check(owner_only)
    async def rate_limit_events(self, ctx: commands.Context, count: int = 10):
        """Show recent Discord global rate-limit events from the bounded event log."""
        events_fn = getattr(self.bot, "recent_rate_limits", None)
        events = events_fn(count) if callable(events_fn) else []
        if not isinstance(events, list):
            events = []
        if not events:
            description = "No global Discord rate-limit events have been recorded since startup."
        else:
            now = time.time()
            lines = []
            for event in reversed(events):
                age = max(0, int(now - event["timestamp"]))
                lines.append(
                    f"• `{event['source']}` · retry `{event['retry_after']:.1f}s` · `{age}s ago`"
                )
            description = "\n".join(lines)
        await ctx.send(view=component_card(panel("Recent Discord Rate Limits", description)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="guildinfo", aliases=("serverinfoowner",), hidden=True)
    @commands.check(owner_only)
    async def owner_guild_info(self, ctx: commands.Context, guild_id: int):
        """Inspect a guild currently known to the bot without modifying it."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await ctx.send(view=component_card(panel("Server Not Found", f"Zyven is not currently connected to `{guild_id}`.", color=0xED4245)))
        owner = guild.owner
        features = getattr(guild, "features", ())
        if not isinstance(features, (list, tuple)):
            features = ()
        features = ", ".join(features[:12]) or "None"
        guild_name = getattr(guild, "name", "Unknown server")
        if not isinstance(guild_name, str):
            guild_name = "Unknown server"
        owner_mention = getattr(owner, "mention", None) if owner else None
        if not isinstance(owner_mention, str):
            owner_mention = f"`{guild.owner_id}`"
        channels = getattr(guild, "channels", ())
        roles = getattr(guild, "roles", ())
        channel_count = len(channels) if isinstance(channels, (list, tuple)) else 0
        role_count = len(roles) if isinstance(roles, (list, tuple)) else 0
        premium_tier = getattr(guild, "premium_tier", 0)
        if not isinstance(premium_tier, int):
            premium_tier = 0
        guild_member_count = getattr(guild, "member_count", 0)
        if not isinstance(guild_member_count, int):
            guild_member_count = 0
        description = (
            f"**Name:** {discord.utils.escape_markdown(guild_name)}\n"
            f"**ID:** `{guild.id}`\n"
            f"**Owner:** {owner_mention}\n"
            f"**Members:** `{guild_member_count:,}`\n"
            f"**Channels:** `{channel_count:,}`\n"
            f"**Roles:** `{role_count:,}`\n"
            f"**Boost tier:** `{premium_tier}`\n"
            f"**Features:** `{discord.utils.escape_markdown(features)}`"
        )
        await ctx.send(view=component_card(panel("Owner Guild Information", description)), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="reloadcog", aliases=("reload",), hidden=True)
    @commands.check(owner_only)
    async def reload_cog(self, ctx: commands.Context, extension: str):
        """Reload one already-loaded cogs.* extension without accepting arbitrary imports."""
        normalized = extension.strip().removesuffix(".py")
        qualified = normalized if normalized.startswith("cogs.") else f"cogs.{normalized}"
        extensions = getattr(self.bot, "extensions", {})
        if not isinstance(extensions, dict):
            extensions = {}
        if qualified not in extensions:
            loaded = ", ".join(sorted(name.removeprefix("cogs.") for name in extensions))
            return await ctx.send(view=component_card(panel("Extension Not Loaded", f"Use the name of an already-loaded cog.\n\n**Loaded:** `{loaded}`", color=0xED4245)))
        if qualified == "cogs.owner":
            return await ctx.send(view=component_card(panel("Reload Refused", "Reloading the owner cog from inside its own command is unsafe. Restart the bot instead.")))
        try:
            await self.bot.reload_extension(qualified)
        except Exception as error:
            log.exception("Owner failed to reload extension %s", qualified)
            return await ctx.send(view=component_card(panel("Extension Reload Failed", f"`{qualified}` failed with `{type(error).__name__}`. Check the bot log for details.", color=0xED4245)))
        await ctx.send(view=component_card(panel("Extension Reloaded", f"Successfully reloaded `{qualified}`.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="synccommands", aliases=("sync",), hidden=True)
    @commands.check(owner_only)
    async def sync_commands(self, ctx: commands.Context, guild_id: int | None = None):
        """Sync application commands globally or to one development guild."""
        try:
            if guild_id is None:
                synced = await self.bot.tree.sync()
                target = "globally"
            else:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return await ctx.send(view=component_card(panel("Server Not Found", f"Zyven is not currently connected to `{guild_id}`.", color=0xED4245)))
                target_guild = discord.Object(id=guild_id)
                self.bot.tree.copy_global_to(guild=target_guild)
                synced = await self.bot.tree.sync(guild=target_guild)
                target = f"to `{guild.name}` (`{guild_id}`)"
        except (discord.Forbidden, discord.HTTPException) as error:
            return await ctx.send(view=component_card(panel("Command Sync Failed", f"Discord rejected the sync: `{type(error).__name__}`.", color=0xED4245)))
        await ctx.send(view=component_card(panel("Application Commands Synced", f"Synced `{len(synced)}` command(s) {target}.")), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="banall", aliases=("massban", "ban-all"), hidden=True)
    @commands.guild_only()
    @commands.check(owner_only)
    async def owner_banall(self, ctx: commands.Context):
        """Owner-only bulk-ban command delegated to the confirmed moderation executor."""
        moderation = self.bot.get_cog("ModerationExtras")
        if moderation is None:
            return await ctx.send(view=component_card(panel("Ban All Unavailable", "The moderation module is still loading. Try again shortly.")))
        if ctx.guild.me is None or not ctx.guild.me.guild_permissions.ban_members:
            return await ctx.send(view=component_card(panel("Ban All Unavailable", "Zyven needs **Ban Members** permission in this server.")))
        try:
            if not ctx.guild.chunked:
                await ctx.guild.chunk(cache=True)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not fully chunk guild %s before owner banall", ctx.guild.id)
        members = [
            member for member in ctx.guild.members
            if member.id not in {ctx.guild.owner_id, self.bot.user.id, ctx.author.id}
            and member.top_role < ctx.guild.me.top_role
        ]
        if not members:
            return await ctx.send(view=component_card(panel("Ban All", "There are no members that Zyven can safely ban in this server.")))
        await moderation.ask_confirmation(
            ctx,
            "banall",
            {"member_ids": [member.id for member in members]},
            "Ban All",
            f"This owner-only action will ban **{len(members)}** currently manageable member(s). The server owner, Zyven, the command author, and higher roles are excluded. Continue?",
        )

    async def _scan_progress(self, message, percent: int, stage: str):
        try:
            await message.edit(view=component_card(panel("Zyven Full Scan", f"`{percent:3d}%` · {stage}\n\nScanning runs in the background; the bot stays online.")))
        except discord.HTTPException:
            # A deleted progress message must not cancel the report generation.
            return

    @commands.command(name="ownerscan", aliases=("fullscan",), hidden=True)
    @commands.check(owner_only)
    async def owner_scan(self, ctx: commands.Context):
        """Run source, dependency, runtime, and isolated command callback diagnostics."""
        if self._owner_scan_lock.locked():
            return await ctx.send(view=component_card(panel("Scan Already Running", "Wait for the current owner scan to finish.")))

        async with self._owner_scan_lock:
            progress = await ctx.send(view=component_card(panel("Zyven Full Scan", "`  0%` · Preparing source scan…\n\nScanning runs in the background; the bot stays online.")))
            root = Path(__file__).resolve().parents[1]
            report: dict[str, str] = {}
            await self._scan_progress(progress, 10, "Scanning Python files for syntax and import issues…")
            source_result = await asyncio.to_thread(_scan_python_sources, root)
            py_files = source_result["files"]
            syntax = source_result["syntax"]
            parse = source_result["parse"]
            missing = source_result["missing_imports"]
            report["syntax-errors.txt"] = "\n".join(syntax + parse) or "No syntax or source-read errors found."
            report["imports-and-dependencies.txt"] = "\n".join(missing) or "All detected top-level imports are available or belong to the local source tree."
            await self._scan_progress(progress, 55, f"Source scan complete ({len(py_files)} Python files); checking installed packages…")

            pip_report = "Skipped: pip is unavailable in this environment."
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "check",
                    cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
                pip_report = output.decode("utf-8", "replace")[-12000:] or "pip check returned no output."
                if proc.returncode not in (0, None):
                    pip_report = f"Exit code: {proc.returncode}\n{pip_report}"
            except (OSError, asyncio.TimeoutError) as exc:
                pip_report = f"Package check could not complete: {type(exc).__name__}: {exc}"
            report["installed-package-check.txt"] = pip_report

            await self._scan_progress(progress, 23, "Checking the live bot, gateway, and database…")
            runtime = [
                f"Checked at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
                f"Python: {platform.python_version()}",
                f"Discord connection ready: {self.bot.is_ready()}",
                f"Gateway latency: {getattr(self.bot, 'latency', float('nan')) * 1000:.0f} ms",
                f"Guilds cached: {len(getattr(self.bot, 'guilds', []))}",
            ]
            db = getattr(self.bot, "db", None)
            store = getattr(db, "store", None)
            ping = getattr(store, "ping", None)
            if ping:
                try:
                    await asyncio.wait_for(ping(), timeout=4)
                    runtime.append("Database ping: OK")
                except Exception as exc:
                    runtime.append(f"Database ping: FAILED ({type(exc).__name__}: {str(exc)[:400]})")
            else:
                runtime.append("Database ping: unavailable (no configured store ping method)")
            report["runtime-health.txt"] = "\n".join(runtime)
            report["source-inventory.txt"] = "\n".join(path.relative_to(root).as_posix() for path in py_files)

            await self._scan_progress(progress, 28, "Starting isolated command callback run; Discord, network, and database writes are disabled…")
            command_results: dict[str, object] = {"results": [], "command_count": 0}
            runner_log: list[str] = []
            with tempfile.TemporaryDirectory(prefix="zyven-selftest-") as temp_dir:
                result_path = Path(temp_dir) / "command-results.json"
                safe_env = os.environ.copy()
                for key in ("DISCORD_TOKEN", "MONGODB_URI", "MONGODB_BACKUP_URI", "OWNER_LOG_WEBHOOK_URL", "BACKUP_WEBHOOK_URL", "LAVALINK_AUTH"):
                    safe_env.pop(key, None)
                safe_env["ZYVEN_SELFTEST"] = "1"
                runner = root / "core" / "command_selftest.py"
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(runner), "--root", str(root), "--output", str(result_path),
                        cwd=str(root), env=safe_env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    total_commands = 0
                    processed = 0
                    last_progress = 28
                    started = time.monotonic()
                    while True:
                        try:
                            line = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if process.returncode is not None:
                                break
                            if time.monotonic() - started > 1800:
                                process.kill()
                                await process.wait()
                                runner_log.append("Self-test runner exceeded the 30-minute safety limit and was stopped.")
                                break
                            continue
                        if not line:
                            break
                        decoded = line.decode("utf-8", "replace").strip()
                        if decoded.startswith("TOTAL|"):
                            try:
                                total_commands = int(decoded.split("|", 1)[1])
                            except ValueError:
                                pass
                        elif decoded.startswith("PROGRESS|"):
                            parts = decoded.split("|", 3)
                            if len(parts) >= 4:
                                try:
                                    processed = int(parts[1])
                                    total_commands = int(parts[2]) or total_commands
                                    percent = 30 + int(58 * processed / max(total_commands, 1))
                                    if percent >= last_progress + 4:
                                        await self._scan_progress(progress, percent, f"Running isolated command callbacks · {processed}/{total_commands} · `{parts[3][:70]}`")
                                        last_progress = percent
                                except ValueError:
                                    pass
                        elif decoded and len(runner_log) < 80:
                            runner_log.append(decoded[:500])
                    return_code = await process.wait()
                    if result_path.exists():
                        try:
                            command_results = json.loads(result_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            runner_log.append(f"Could not read command results: {type(exc).__name__}: {exc}")
                    elif return_code:
                        runner_log.append(f"Self-test subprocess exited with code {return_code} without a result file.")
                except OSError as exc:
                    runner_log.append(f"Could not start isolated self-test subprocess: {type(exc).__name__}: {exc}")

            command_rows = command_results.get("results", [])
            status_counts: dict[str, int] = {}
            for row in command_rows:
                status = str(row.get("status", "UNKNOWN"))
                status_counts[status] = status_counts.get(status, 0) + 1
            command_lines = [
                f"{row.get('status', 'UNKNOWN')} | {row.get('command', 'unknown')} | {str(row.get('detail', '')).splitlines()[0][:300]}"
                for row in command_rows
            ]
            errors = [row for row in command_rows if row.get("status") in {"CALLBACK_EXCEPTION", "SIMULATION_TIMEOUT", "IMPORT_ERROR", "DISCOVERY_ERROR"}]
            report["command-selftest-results.txt"] = (
                "Isolated command callback results\n"
                "Inputs are synthetic. Discord/network calls and database writes are blocked or mocked.\n"
                "Callback exceptions can mean a code bug or a missing synthetic fixture; review the traceback before treating one as a confirmed production bug.\n"
                "Live permissions, Discord API responses, production database data, and database-generated custom commands are not exercised.\n\n"
                + "\n".join(command_lines)
            ) or "No registered callbacks were discovered."
            report["command-selftest-errors.txt"] = "\n\n".join(
                f"{row.get('command')}\n{row.get('status')}\n{row.get('detail', '')}" for row in errors
            ) or "No callback exceptions were captured in the isolated run."
            for error_status in ("CALLBACK_EXCEPTION", "SIMULATION_TIMEOUT", "IMPORT_ERROR", "DISCOVERY_ERROR"):
                matching = [row for row in command_rows if row.get("status") == error_status]
                report[f"command-errors/{error_status.lower()}.txt"] = "\n\n".join(
                    f"Command: {row.get('command')}\n{row.get('detail', '')}" for row in matching
                ) or f"No {error_status.lower().replace('_', ' ')} entries."
            report["command-selftest.json"] = json.dumps(command_results, ensure_ascii=False, indent=2)
            report["selftest-runner.log"] = "\n".join(runner_log) or "No runner-level errors."

            source_errors = len(syntax) + len(parse) + len(missing)
            summary = (
                "Zyven diagnostic scan\n"
                f"Python files scanned: {len(py_files)}\n"
                f"Syntax/source errors: {len(syntax) + len(parse)}\n"
                f"Missing imports: {len(missing)}\n"
                f"Command callbacks probed: {command_results.get('command_count', len(command_rows))}\n"
                f"Command callback exceptions: {len(errors)}\n"
                f"Command statuses: {json.dumps(status_counts, sort_keys=True)}\n"
                f"Dependency check: {pip_report.splitlines()[0][:300]}\n"
                f"Runtime: bot_ready={self.bot.is_ready()}\n"
                "Scope: source checks, runtime health, and isolated callback probes only; production Discord/database side effects were not executed.\n"
            )
            report["summary.txt"] = summary
            await self._scan_progress(progress, 92, "Packaging categorized results into a ZIP…")

            def make_zip() -> io.BytesIO:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, contents in report.items():
                        archive.writestr(name, contents)
                buffer.seek(0)
                return buffer

            zipped = await asyncio.to_thread(make_zip)
            await self._scan_progress(progress, 100, f"Scan complete · {len(errors)} callback exception(s), {source_errors} source/import issue(s). ZIP report attached below.")
            await ctx.send(
                content=f"Full scan finished · {len(command_rows)} command callbacks probed · {len(errors)} callback exception(s) · {source_errors} source/import issue(s).",
                file=discord.File(zipped, filename="zyven-full-scan-report.zip"),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.command(name="blacklistserver", aliases=("serverblacklist", "banserver"), hidden=True)
    @commands.check(owner_only)
    async def blacklist_server(self, ctx: commands.Context, guild_id: int, *, reason: str = "No reason provided"):
        """Blacklist a server and make Zyven leave it."""
        await self.bot.db.set_server_blacklist(guild_id, reason[:500], ctx.author.id)
        self.bot.invalidate_owner_guard_cache()
        guild = self.bot.get_guild(guild_id)
        name = f"**{discord.utils.escape_markdown(guild.name)}**" if guild else "That server"
        await ctx.send(view=component_card(panel("Server Blacklisted", f"{name} (`{guild_id}`) is blocked. {('I am leaving it now.' if guild else 'It is not currently in my server list.')}")))
        if guild:
            await guild.leave()

    @commands.command(name="unblacklistserver", aliases=("serverunblacklist", "unbanserver"), hidden=True)
    @commands.check(owner_only)
    async def unblacklist_server(self, ctx: commands.Context, guild_id: int):
        """Remove a server from the persistent blacklist."""
        removed = await self.bot.db.remove_server_blacklist(guild_id)
        self.bot.invalidate_owner_guard_cache()
        message = "Server removed from the blacklist. Add Bot can be used to invite Zyven again." if removed else "That server was not on the blacklist."
        await ctx.send(view=component_card(panel("Server Blacklist Updated", message)))

    @commands.command(name="blacklistservers", aliases=("serverblacklistlist",), hidden=True)
    @commands.check(owner_only)
    async def blacklist_servers(self, ctx: commands.Context):
        """List every blacklisted server and its reason."""
        rows = await self.bot.db.list_server_blacklist()
        description = "\n".join(f"• `{row['guild_id']}` — {discord.utils.escape_markdown((row.get('reason') or 'No reason')[:120])}" for row in rows[:20]) or "No servers are blacklisted."
        if len(rows) > 20:
            description += f"\n…and {len(rows) - 20} more."
        await ctx.send(view=component_card(panel(f"Blacklisted Servers · {len(rows)}", description, color=0xED4245)))

    @commands.command(name="globalban", aliases=("globalbanuser",), hidden=True)
    @commands.check(owner_only)
    async def global_ban(self, ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
        """Block a user from all Zyven prefix and application commands."""
        if user_id == settings.owner_id:
            return await ctx.send(view=component_card(panel("Protected Account", "The configured bot owner cannot be globally banned.", color=0xED4245)))
        await self.bot.db.set_global_ban(user_id, reason[:500], ctx.author.id)
        self.bot.invalidate_owner_guard_cache()
        await ctx.send(view=component_card(panel("Global Ban Added", f"User `{user_id}` is blocked from bot commands everywhere.\nReason: {discord.utils.escape_markdown(reason[:500])}", color=0xED4245)))

    @commands.command(name="globalunban", aliases=("globalunbanuser",), hidden=True)
    @commands.check(owner_only)
    async def global_unban(self, ctx: commands.Context, user_id: int):
        """Restore a globally banned user's bot command access."""
        removed = await self.bot.db.remove_global_ban(user_id)
        self.bot.invalidate_owner_guard_cache()
        await ctx.send(view=component_card(panel("Global Ban Updated", f"User `{user_id}` {'is unbanned' if removed else 'was not globally banned'}.")))

    @commands.command(name="globalbans", aliases=("listglobalbans",), hidden=True)
    @commands.check(owner_only)
    async def global_bans(self, ctx: commands.Context):
        """List globally blocked user IDs."""
        rows = await self.bot.db.list_global_bans()
        description = "\n".join(f"• `{row['user_id']}` — {discord.utils.escape_markdown((row.get('reason') or 'No reason')[:120])}" for row in rows[:20]) or "No users are globally banned."
        if len(rows) > 20:
            description += f"\n…and {len(rows) - 20} more."
        await ctx.send(view=component_card(panel(f"Global Bans · {len(rows)}", description, color=0xED4245)))

    @commands.command(name="serverlist", hidden=True)
    @commands.check(owner_only)
    async def server_list(self, ctx: commands.Context, page: int = 1):
        """List servers Zyven is in and show a button to install it elsewhere."""
        guilds = sorted(self.bot.guilds, key=lambda item: item.name.casefold())
        page_count = max(1, (len(guilds) + 14) // 15)
        page = max(1, min(page, page_count))
        rows = guilds[(page - 1) * 15:page * 15]
        description = "\n".join(f"**{discord.utils.escape_markdown(guild.name)}** · `{guild.id}` · {guild.member_count or '?'} members" for guild in rows) or "Zyven is not in any servers."
        view = discord.ui.View(timeout=120)
        if settings.invite_url:
            view.add_item(discord.ui.Button(label="Add Zyven to a server", style=discord.ButtonStyle.link, url=settings.invite_url))
        await ctx.send(embed=panel(f"Zyven Server List · Page {page}/{page_count} · {len(guilds)} total", description), view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="serverleave", aliases=("leaveguild",), hidden=True)
    @commands.check(owner_only)
    async def server_leave(self, ctx: commands.Context, guild_id: int):
        """Make Zyven leave a server by ID."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return await ctx.send(view=component_card(panel("Server Not Found", "Zyven is not currently in that server.", color=0xED4245)))
        name = discord.utils.escape_markdown(guild.name)
        await ctx.send(view=component_card(panel("Leaving Server", f"Zyven is leaving **{name}** (`{guild_id}`).")))
        await guild.leave()

    @commands.command(name="serverjoin", aliases=("botinvite",), hidden=True)
    @commands.check(owner_only)
    async def server_join(self, ctx: commands.Context):
        """Show the OAuth invite; a server admin must approve adding a Discord bot."""
        view = discord.ui.View(timeout=120)
        if settings.invite_url:
            view.add_item(discord.ui.Button(label="Add Zyven to a server", style=discord.ButtonStyle.link, url=settings.invite_url))
        await ctx.send(embed=panel("Invite Zyven", "Discord does not let a bot join a server by itself. Open the invite and choose a server where you have Manage Server permission."), view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="branding", invoke_without_command=True, hidden=True)
    @commands.check(owner_only)
    @commands.guild_only()
    async def branding(self, ctx: commands.Context):
        """Configure the branding used in owner webhook logs for this server."""
        data = await self.bot.db.get_owner_branding(ctx.guild.id)
        description = f"**Avatar:** {data.get('avatar_url') or 'Server icon'}\n**Banner:** {data.get('banner_url') or 'Not set'}\n**Bio:** {data.get('bio') or 'Not set'}\n\nCommands: `branding avatar <https-url>`, `branding banner <https-url>`, `branding bio <text>`, `branding server`, `branding reset`."
        await ctx.send(view=component_card(panel("Zyven Log Branding", description)))

    @branding.command(name="avatar", hidden=True)
    async def branding_avatar(self, ctx: commands.Context, url: str):
        if not url.startswith("https://") or not urlparse(url).hostname:
            return await ctx.send(view=component_card(panel("Invalid Image Link", "Use a valid HTTPS image URL.", color=0xED4245)))
        await self.bot.db.set_owner_branding(ctx.guild.id, "avatar_url", url[:1000])
        await ctx.send(view=component_card(panel("Log Avatar Updated", "Owner webhook logs in this server will use this avatar.")))

    @branding.command(name="banner", hidden=True)
    async def branding_banner(self, ctx: commands.Context, url: str):
        if not url.startswith("https://") or not urlparse(url).hostname:
            return await ctx.send(view=component_card(panel("Invalid Image Link", "Use a valid HTTPS image URL.", color=0xED4245)))
        await self.bot.db.set_owner_branding(ctx.guild.id, "banner_url", url[:1000])
        await ctx.send(view=component_card(panel("Log Banner Updated", "Owner webhook logs in this server will include this banner image.")))

    @branding.command(name="bio", hidden=True)
    async def branding_bio(self, ctx: commands.Context, *, text: str):
        await self.bot.db.set_owner_branding(ctx.guild.id, "bio", text[:1000])
        await ctx.send(view=component_card(panel("Log Bio Updated", "Owner webhook logs in this server will include this server bio.")))

    @branding.command(name="server", hidden=True)
    async def branding_server(self, ctx: commands.Context):
        await self.bot.db.clear_owner_branding(ctx.guild.id)
        await ctx.send(view=component_card(panel("Server Look Applied", "Owner webhook logs now use this server's icon and no custom banner or bio.")))

    @branding.command(name="reset", hidden=True)
    async def branding_reset(self, ctx: commands.Context):
        await self.bot.db.clear_owner_branding(ctx.guild.id)
        await ctx.send(view=component_card(panel("Branding Reset", "Custom owner-log branding for this server was cleared.")))

    @commands.group(name="noprefix", invoke_without_command=True, hidden=True)
    @commands.check(owner_only)
    async def noprefix(self, ctx: commands.Context):
        """Manage users allowed to run registered commands without the prefix."""
        await ctx.invoke(self.noprefix_list)

    @noprefix.command(name="add", hidden=True)
    async def noprefix_add(self, ctx: commands.Context, member: discord.Member, duration: str | None = None):
        """Grant prefixless access; omit duration for a permanent grant."""
        await self._grant_noprefix_access(ctx, member, duration)

    async def _grant_noprefix_access(self, ctx: commands.Context, member: discord.Member, duration: str | None = None):
        expires_at = None
        if duration is not None:
            try:
                expires_at = time.time() + parse_duration(duration)
            except ValueError as error:
                await ctx.send(view=component_card(panel(f"{icon('error')} Invalid Duration", str(error))))
                return
        await self.bot.db.add_noprefix_user(member.id, expires_at)
        self.bot.invalidate_noprefix_cache()
        access = "permanently" if expires_at is None else f"until <t:{int(expires_at)}:F> (<t:{int(expires_at)}:R>)"
        duration_label = display_duration(duration)
        active_text = "permanently" if expires_at is None else f"for {duration_label}"
        dm_embed = discord.Embed(title="No Prefix Added", color=0x2B1D1D)
        expiration = f"\nExpires: <t:{int(expires_at)}:F>" if expires_at is not None else ""
        dm_embed.description = (
            f"Duration: {duration_label}{expiration}\n\n"
            f"Your No Prefix access is now active {active_text}.\n\n"
            "You can now use commands without a prefix."
        )
        if ctx.guild:
            dm_embed.set_footer(text=ctx.guild.name)
        dm_view = discord.ui.View()
        if settings.support_url:
            dm_view.add_item(discord.ui.Button(label="Join Support Server", style=discord.ButtonStyle.link, url=settings.support_url))
        dm_sent = True
        try:
            await member.send(embed=dm_embed, view=dm_view if dm_view.children else None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            dm_sent = False
        result = f"{member.mention} can now use registered bot commands without the prefix {access}."
        if not dm_sent:
            result += "\nI couldn't DM them; they may have direct messages disabled."
        await ctx.send(
            view=component_card(panel(f"{icon('tick')} No-Prefix Access Added", result)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="garib", hidden=True)
    @commands.check(owner_only)
    async def noprefix_give(self, ctx: commands.Context, member: discord.Member, duration: str | None = None):
        """Quick owner-only alias to grant prefixless access to a user."""
        await self._grant_noprefix_access(ctx, member, duration)

    @commands.group(name="noprefixtrial", invoke_without_command=True, hidden=True)
    @commands.check(owner_only)
    async def noprefix_trial(self, ctx: commands.Context):
        """Configure and publish the one-time no-prefix free trial card."""
        await ctx.invoke(self.noprefix_trial_setup)

    @noprefix_trial.command(name="setup", hidden=True)
    async def noprefix_trial_setup(self, ctx: commands.Context):
        view = NoPrefixTrialSetupView(ctx.author.id)
        await ctx.send(embed=view.make_embed(), view=view, allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="remove", aliases=("delete", "del"), hidden=True)
    async def noprefix_remove(self, ctx: commands.Context, member: discord.Member):
        """Revoke prefixless command access from a user."""
        removed = await self.bot.db.remove_noprefix_user(member.id)
        self.bot.invalidate_noprefix_cache()
        message = f"Prefixless command access was removed from {member.mention}." if removed else f"{member.mention} did not have prefixless access."
        await ctx.send(view=component_card(panel(f"{icon('tick')} No-Prefix Access Updated", message)), allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="list", hidden=True)
    async def noprefix_list(self, ctx: commands.Context):
        """List users with active prefixless command access."""
        entries = await self.bot.db.get_noprefix_users()
        if not entries:
            description = "No users currently have prefixless command access."
        else:
            rows = []
            for entry in sorted(entries, key=lambda item: (item.get("expires_at") is not None, item.get("expires_at") or 0)):
                expires_at = entry.get("expires_at")
                expiry = "Permanent" if expires_at is None else f"Expires <t:{int(expires_at)}:R>"
                rows.append(f"• <@{entry['user_id']}> — {expiry}")
            description = "\n".join(rows)
        embed = panel(f"{icon('security')} No-Prefix Users", description)
        embed.set_footer(text="Only registered bot commands are processed without a prefix.")
        await ctx.send(view=component_card(embed), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Owner(bot))
