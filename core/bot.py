import asyncio
from collections import deque
import math
import time
import logging
import re
import os
import signal

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

from .database import Database
from utils.backups import send_source_backup
from utils import permit as permit_utils
from utils.emojis import icon, sync_application_emojis
from .settings import settings
from utils.ui import ComponentCard, bind_component_owner, component_card, notice_panel, panel, send_card, schedule_access_denied_delete

log = logging.getLogger("zyven")


def _consume_task_result(task: asyncio.Task) -> None:
    """Retrieve errors from database work that outlives a caller's soft timeout."""
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def await_without_cancelling(awaitable, timeout: float):
    """Apply a response deadline without cancelling an in-flight DB operation.

    Database operations get a soft response deadline without cancelling in-flight writes.
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait((task,), timeout=timeout)
    if task in done:
        return task.result()
    task.add_done_callback(_consume_task_result)
    raise asyncio.TimeoutError


class MentionWelcomeView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot
        self.add_item(discord.ui.Button(
            label="Support Server", style=discord.ButtonStyle.link, url=settings.support_url
        ))

    @discord.ui.button(label="Features", style=discord.ButtonStyle.secondary)
    async def show_features(self, interaction: discord.Interaction, button: discord.ui.Button):
        helpdesk = self.bot.get_cog("Helpdesk")
        if helpdesk is None:
            await interaction.response.send_message(
                "The feature browser is still loading. Try again in a moment.", ephemeral=True
            )
            return
        from cogs.helpdesk import HelpView
        await interaction.response.send_message(
            view=component_card(helpdesk.home_embed(), HelpView()), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def dynamic_command_prefix(bot, message: discord.Message):
    # Accept only the configured prefix, plus mentions and explicitly
    # authorized no-prefix access.
    guild_prefix = settings.prefix
    if message.guild is not None:
        guild_prefix = await current_guild_prefix(bot, message.guild.id)
    command_prefixes = [guild_prefix]
    prefixes = list(command_prefixes)
    if bot.user:
        # Let users invoke commands with a bare bot mention followed by the
        # command, with or without a separating space.
        prefixes = [f"<@{bot.user.id}>", f"<@!{bot.user.id}>", *command_prefixes]
    # A configured prefix or bot mention always keeps its normal parsing.
    if any(message.content.startswith(prefix) for prefix in command_prefixes) or (
        bot.user and message.content.startswith((f"<@{bot.user.id}>", f"<@!{bot.user.id}>"))
    ):
        return prefixes
    # Most messages are ordinary chat. Only inspect saved custom-role names
    # for users already known to have no-prefix access.
    first_word = message.content.split(maxsplit=1)[0].lower() if message.content.split(maxsplit=1) else ""
    if not first_word:
        return prefixes
    if not await bot.has_noprefix_access(message.author.id):
        return prefixes
    if bot.get_command(first_word) is not None:
        return ["", *prefixes]
    if message.guild is not None:
        custom_role_cog = bot.get_cog("CustomRole")
        if custom_role_cog is not None:
            try:
                custom = (await custom_role_cog.config(message.guild.id)).get("commands", {})
                if first_word in custom:
                    return ["", *prefixes]
            except Exception:
                log.debug("Could not check custom role command for prefixless user", exc_info=True)
    return prefixes


async def current_guild_prefix(bot, guild_id: int | None) -> str:
    if guild_id is None:
        return settings.prefix
    now = time.monotonic()
    cached = bot._guild_prefix_cache.get(guild_id)
    if cached and cached[0] > now:
        return cached[1]
    lock = bot._guild_prefix_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = bot._guild_prefix_cache.get(guild_id)
        if cached and cached[0] > now:
            return cached[1]
        fallback = cached[1] if cached else settings.prefix
        if bot.db.temporarily_unavailable:
            bot._guild_prefix_cache[guild_id] = (now + bot.db.DB_FAILURE_BACKOFF_SECONDS, fallback)
            return fallback
        try:
            config = await await_without_cancelling(bot.db.get_guild(guild_id), timeout=0.5)
            prefix = str(config.get("prefix", {}).get("value") or settings.prefix)
            bot.db.mark_available()
        except Exception:
            # A single shared lookup per guild avoids a DB timeout for every
            # incoming message while Atlas is unreachable.
            bot.db.mark_unavailable()
            prefix = fallback
            # Prefix lookup is on the message-dispatch path. A transient DB
            # pool timeout should fall back quietly; the database health loop
            # reports the underlying outage without filling the console once
            # per guild/message burst.
            log.debug("Could not load prefix for guild %s; using cached/default prefix", guild_id, exc_info=True)
        ttl = bot.db.DB_FAILURE_BACKOFF_SECONDS if bot.db.temporarily_unavailable else 10
        # Prefix configuration is effectively static during normal operation;
        # cache it longer to avoid putting an extra database read on command
        # dispatch after every short idle period.
        if not bot.db.temporarily_unavailable:
            ttl = 60
        bot._guild_prefix_cache[guild_id] = (now + ttl, prefix)
        return prefix


class ZyvenContext(commands.Context):
    async def _apply_server_prefix(self, args, kwargs):
        active_prefix = await current_guild_prefix(self.bot, self.guild.id) if self.guild is not None else settings.prefix
        if self.guild is not None and active_prefix != settings.prefix:
            def update_text(value):
                if not isinstance(value, str):
                    return value
                return re.sub(
                    rf"(?<=`){re.escape(settings.prefix)}(?=[A-Za-z])",
                    lambda _match: active_prefix,
                    value,
                )

            if args and isinstance(args[0], str):
                args = (update_text(args[0]), *args[1:])
            elif isinstance(kwargs.get("content"), str):
                kwargs["content"] = update_text(kwargs["content"])

            embed = kwargs.get("embed")
            if embed is not None:
                embed = embed.copy()
                if embed.title:
                    embed.title = update_text(embed.title)
                if embed.description:
                    embed.description = update_text(embed.description)
                for index, field in enumerate(embed.fields):
                    embed.set_field_at(index, name=update_text(field.name), value=update_text(field.value), inline=field.inline)
                if embed.footer.text:
                    embed.set_footer(text=update_text(embed.footer.text), icon_url=embed.footer.icon_url)
                kwargs["embed"] = embed

        view = kwargs.get("view")
        if isinstance(view, ComponentCard):
            kwargs["view"] = bind_component_owner(view, self.author.id)
            if self.guild is not None and active_prefix != settings.prefix:
                for item in view.walk_children():
                    if isinstance(item, discord.ui.TextDisplay):
                        item.content = update_text(item.content)
        return args, kwargs

    async def send(self, *args, **kwargs):
        args, kwargs = await self._apply_server_prefix(args, kwargs)
        return await super().send(*args, **kwargs)

    async def reply(self, *args, **kwargs):
        args, kwargs = await self._apply_server_prefix(args, kwargs)
        return await super().reply(*args, **kwargs)


class ZyvenBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        # Vanity role assignment depends on members' Discord custom statuses.
        # The Presence Intent must also be enabled in the Discord Developer Portal.
        intents.presences = True
        intents.moderation = True
        intents.emojis_and_stickers = True
        intents.message_content = True
        intents.reactions = True
        intents.voice_states = True
        super().__init__(
            command_prefix=dynamic_command_prefix,
            intents=intents,
            owner_id=settings.owner_id,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions.none(),
            context_class=ZyvenContext,
        )
        self.remove_command("help")
        self.db = Database(settings.sqlite_path, settings.mongo_uri, settings.mongo_database)
        self.owner_ids = settings.owner_ids
        self.started_at = time.monotonic()
        self._emoji_sync_started = False
        self._noprefix_users: dict[int, float | None] = {}
        self._guild_prefix_cache: dict[int, tuple[float, str]] = {}
        self._guild_prefix_locks: dict[int, asyncio.Lock] = {}
        self._ignore_config_cache: dict[int, tuple[float, dict]] = {}
        self._noprefix_cache_expires = 0.0
        self._noprefix_cache_lock = asyncio.Lock()
        self._local_command_claims: dict[int, float] = {}
        self._local_command_claim_lock = asyncio.Lock()
        self._command_cooldowns: dict[tuple[int, int, str], tuple[float, bool]] = {}
        self._global_rate_limit_until = 0.0
        self._last_global_rate_limit_log = 0.0
        self._last_global_rate_limit_notice = 0.0
        self._rate_limit_events = deque(maxlen=200)
        self._performance_samples = deque(maxlen=500)
        self._command_started_at: dict[int, tuple[float, str, str]] = {}
        self._owner_alert_tasks: set[asyncio.Task] = set()
        self._last_owner_alert_at: dict[str, float] = {}
        self._owner_guard_cache: dict[tuple[int, int], tuple[float, dict | None]] = {}
        self._owner_guard_pending: dict[tuple[int, int], asyncio.Future] = {}
        self._last_database_fallback_log = 0.0
        self._last_database_health_warning = 0.0
        self._backup_task: asyncio.Task | None = None
        self._db_ping_task: asyncio.Task | None = None
        self._inviter_welcome_tasks: set[asyncio.Task] = set()
        self._shutdown_started = False
        self._presence_task: asyncio.Task | None = None
        self.tree.on_error = self.on_app_command_error
        # Role permits (cogs/permit.py): publish a member's permitted permissions
        # while their command runs and enforce owner-role immunity.
        permit_utils.install_permission_patch()
        self.tree.interaction_check = self._permit_interaction_check
        self.before_invoke(self._permit_before_invoke)

    async def has_noprefix_access(self, user_id: int) -> bool:
        # The configured bot owner is always trusted to run commands without
        # a prefix; other users must have an active `np add` grant.
        if settings.is_owner(user_id):
            return True
        now = time.monotonic()
        if now >= self._noprefix_cache_expires:
            async with self._noprefix_cache_lock:
                now = time.monotonic()
                if now >= self._noprefix_cache_expires:
                    try:
                        entries = await await_without_cancelling(self.db.get_noprefix_users(), timeout=1.0)
                        self._noprefix_users = {int(item["user_id"]): item.get("expires_at") for item in entries}
                        self._noprefix_cache_expires = now + 30
                    except Exception:
                        # Back off failed refreshes so a DB outage does not
                        # turn every prefixless command into another query.
                        self._noprefix_cache_expires = now + self.db.DB_FAILURE_BACKOFF_SECONDS
                        log.debug("Could not refresh no-prefix users; using the last cached entries", exc_info=True)
        expiry = self._noprefix_users.get(user_id)
        return user_id in self._noprefix_users and (expiry is None or float(expiry) > time.time())

    def invalidate_noprefix_cache(self):
        self._noprefix_cache_expires = 0.0

    def invalidate_owner_guard_cache(self):
        self._owner_guard_cache.clear()

    @staticmethod
    def command_cooldown_seconds(command_name: str) -> float:
        """Use longer buckets for help, network searches, and risky changes."""
        name = command_name.casefold().replace("_", " ")
        root = name.split()[0]
        if root in {"ban", "unban", "kick", "mute", "timeout", "unmute", "untimeout", "jail", "unjail", "purge", "pb"}:
            return 2.5
        if root in {"globalban", "globalunban", "blacklistserver", "unblacklistserver", "serverleave", "nuke", "reset", "delete"} or any(token in name for token in (" delete", " reset", " blacklist", " globalban")):
            return 12.0
        if root in {"image", "img", "gif", "weather", "search", "play", "join", "serverinfo", "userinfo", "avatar", "stats"}:
            return 6.0
        if root in {"help", "ownerhelp", "commands", "h"}:
            return 4.0
        if root in {"set", "config", "branding", "ticket", "antinuke", "automod", "ignore", "extraowner", "role", "prefix", "welcome", "autorole"} or any(token in name for token in (" set", " enable", " disable", " toggle", " add", " remove", " config")):
            return 4.0
        return 2.5

    async def command_cooldown(self, user_id: int, guild_id: int | None, command_name: str) -> tuple[float, bool]:
        """Use an in-process cooldown; command dispatch must not wait on persistent storage."""
        return self._claim_local_cooldown(user_id, command_name)

    async def _check_app_command_cooldown(self, interaction: discord.Interaction) -> bool:
        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        self._command_started_at[interaction.id] = (time.perf_counter(), command_name, "application")
        remaining, notify = await self.command_cooldown(interaction.user.id, interaction.guild_id, command_name)
        if remaining <= 0:
            return True
        if not interaction.response.is_done():
            if notify:
                await interaction.response.send_message(
                    embed=panel("Command Cooldown", f"Wait **{remaining:.1f} seconds** before using `/{command_name}` again.", color=0xFEE75C),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                # Discord requires every application command interaction to
                # be acknowledged; defer repeats without sending another card.
                await interaction.response.defer(ephemeral=True)
        return False

    @staticmethod
    def prefix_command_name(ctx: commands.Context) -> str:
        """Resolve group subcommands so each leaf gets its own cooldown."""
        command = ctx.command
        if command is None:
            return "unknown"
        prefix = ctx.prefix or settings.prefix
        content = ctx.message.content[len(prefix):].strip() if ctx.message.content.startswith(prefix) else ctx.message.content.strip()
        tokens = content.split()
        for token in tokens[1:]:
            if not isinstance(command, commands.Group):
                break
            child = command.get_command(token)
            if child is None:
                break
            command = child
        return command.qualified_name

    def _claim_local_cooldown(self, user_id: int, command_name: str) -> tuple[float, bool]:
        """Apply the normal one-notice cooldown using local process state."""
        now = time.monotonic()
        key = (int(user_id), 0, command_name.casefold())
        state = self._command_cooldowns.get(key)
        if state and state[0] > now:
            remaining = state[0] - now
            if state[1]:
                return remaining, False
            self._command_cooldowns[key] = (state[0], True)
            return remaining, True
        duration = self.command_cooldown_seconds(command_name)
        self._command_cooldowns[key] = (now + duration, False)
        return 0.0, False

    @staticmethod
    def _rate_limit_exception(error) -> discord.HTTPException | None:
        """Return the underlying Discord 429 exception, if one is present."""
        current = error
        for _ in range(3):
            if isinstance(current, discord.HTTPException) and current.status == 429:
                return current
            current = getattr(current, "original", None)
            if current is None:
                break
        return None

    def _record_global_rate_limit(self, error, source: str) -> float:
        """Extend the process-wide command backoff using Discord's retry_after."""
        rate_error = self._rate_limit_exception(error)
        if rate_error is None:
            return 0.0
        try:
            retry_after = max(1.0, min(300.0, float(getattr(rate_error, "retry_after", 5.0))))
        except (TypeError, ValueError):
            retry_after = 5.0
        now = time.monotonic()
        self._global_rate_limit_until = max(self._global_rate_limit_until, now + retry_after)
        event = {
            "timestamp": time.time(),
            "source": source,
            "retry_after": round(retry_after, 2),
            "backoff_until": time.time() + retry_after,
        }
        self._rate_limit_events.append(event)
        self._maybe_alert_rate_limit(event)
        if now - self._last_global_rate_limit_log >= 10:
            self._last_global_rate_limit_log = now
            log.warning("Discord global HTTP rate limit detected from %s; backing off command dispatch for %.1fs", source, retry_after)
        return retry_after

    def global_rate_limit_remaining(self) -> float:
        return max(0.0, self._global_rate_limit_until - time.monotonic())

    def performance_sample(self, command: str, kind: str, duration_ms: float, status: str) -> None:
        """Record one bounded command performance sample and emit structured logs."""
        sample = {
            "timestamp": time.time(),
            "command": command,
            "kind": kind,
            "duration_ms": round(float(duration_ms), 2),
            "status": status,
        }
        self._performance_samples.append(sample)
        self._maybe_alert_latency()
        level = logging.WARNING if duration_ms >= 1000 else logging.INFO
        log.log(level, "command_performance command=%s kind=%s duration_ms=%.2f status=%s", command, kind, duration_ms, status)

    def performance_snapshot(self) -> dict:
        samples = list(self._performance_samples)
        durations = sorted(float(item["duration_ms"]) for item in samples)
        errors = sum(item.get("status") != "ok" for item in samples)
        if durations:
            p95_index = min(len(durations) - 1, max(0, math.ceil(len(durations) * 0.95) - 1))
            average = sum(durations) / len(durations)
            p95 = durations[p95_index]
            maximum = durations[-1]
        else:
            average = p95 = maximum = 0.0
        return {
            "samples": len(samples),
            "errors": errors,
            "average_ms": average,
            "p95_ms": p95,
            "max_ms": maximum,
            "slow_commands": sum(item["duration_ms"] >= 1000 for item in samples),
        }

    def recent_rate_limits(self, limit: int = 10) -> list[dict]:
        return list(self._rate_limit_events)[-max(1, min(int(limit), 50)):]

    def _schedule_owner_alert(self, alert_type: str, title: str, description: str, *, color: int, cooldown: int) -> None:
        """Queue one owner-webhook alert while suppressing repeated alert storms."""
        if not settings.owner_log_webhook_url:
            return
        now = time.monotonic()
        if now - self._last_owner_alert_at.get(alert_type, 0.0) < cooldown:
            return
        self._last_owner_alert_at[alert_type] = now
        try:
            task = asyncio.create_task(self._send_owner_alert(title, description, color=color), name=f"owner-alert-{alert_type}")
        except RuntimeError:
            log.warning("Could not schedule owner alert %s because no event loop is running", alert_type)
            return
        self._owner_alert_tasks.add(task)
        task.add_done_callback(self._owner_alert_tasks.discard)

    async def _send_owner_alert(self, title: str, description: str, *, color: int) -> None:
        try:
            webhook = discord.Webhook.from_url(settings.owner_log_webhook_url, client=self)
            embed = panel(title, description, color=color)
            embed.timestamp = discord.utils.utcnow()
            embed.set_footer(text="Zyven Owner Alert")
            await webhook.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            # Alert delivery must never affect command execution or shutdown.
            log.exception("Owner alert delivery failed for %s", title)

    def _maybe_alert_rate_limit(self, event: dict) -> None:
        recent = [item for item in self._rate_limit_events if time.time() - item["timestamp"] <= 60]
        if len(recent) < 1:
            return
        self._schedule_owner_alert(
            "rate_limit",
            "Discord Global Rate Limit Alert",
            f"Discord returned a global HTTP 429 for `{event['source']}`.\n"
            f"**Retry after:** `{event['retry_after']:.2f}s`\n"
            f"**Events in the last 60s:** `{len(recent)}`\n"
            f"**Command dispatch backoff:** `{self.global_rate_limit_remaining():.2f}s`",
            color=0xFEE75C,
            cooldown=settings.owner_alert_rate_limit_cooldown_seconds,
        )

    def _maybe_alert_latency(self) -> None:
        snapshot = self.performance_snapshot()
        if snapshot["samples"] < settings.owner_alert_p95_min_samples or snapshot["p95_ms"] < settings.owner_alert_p95_ms:
            return
        self._schedule_owner_alert(
            "latency",
            "Bot Performance Alert",
            f"Rolling command P95 latency exceeded the configured threshold.\n"
            f"**P95:** `{snapshot['p95_ms']:.2f}ms`\n"
            f"**Threshold:** `{settings.owner_alert_p95_ms}ms`\n"
            f"**Average:** `{snapshot['average_ms']:.2f}ms`\n"
            f"**Samples:** `{snapshot['samples']}`\n"
            f"**Errors:** `{snapshot['errors']}`\n"
            f"**Slow commands:** `{snapshot['slow_commands']}`",
            color=0xED4245,
            cooldown=settings.owner_alert_p95_cooldown_seconds,
        )

    async def _check_global_rate_limit(self, interaction: discord.Interaction) -> bool:
        """Stop new slash commands while Discord reports a global 429 backoff."""
        remaining = self.global_rate_limit_remaining()
        if remaining <= 0:
            return True
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    view=component_card(panel(
                        "Discord Rate Limit Active",
                        f"Discord is temporarily limiting Zyven. Try again in **{remaining:.1f} seconds**.",
                        color=0xFEE75C,
                    )),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return False

    async def owner_guard(self, user_id: int, guild_id: int | None = None):
        """Check owner restrictions, using cached policy but denying unknown policy."""
        if settings.is_owner(user_id):
            return None
        key = (int(user_id), int(guild_id or 0))
        now = time.monotonic()
        cached = self._owner_guard_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if self.db.temporarily_unavailable:
            if cached:
                return cached[1]
            # An uncached security decision must never fail open. Independent
            # non-security reads have their own outage-safe fallbacks, while
            # command dispatch waits for this policy to become available.
            if now - self._last_database_fallback_log > 60:
                log.warning("Database unavailable; blocking commands with uncached security policy")
                self._last_database_fallback_log = now
            return {"kind": "error", "reason": "Database security policy is temporarily unavailable."}
        # Never cancel an in-flight database operation just to meet a short
        # command latency budget. Cancellation can leave a pooled connection
        # inside its transaction context and poison later database requests.
        task = self._owner_guard_pending.get(key)
        if task is None:
            checks = [("user", self.db.get_global_ban(int(user_id)))]
            if guild_id:
                checks.append(("guild", self.db.get_server_blacklist(int(guild_id))))
            task = asyncio.ensure_future(asyncio.gather(*(query for _, query in checks)))
            self._owner_guard_pending[key] = task
            task.add_done_callback(lambda done, cache_key=key, query_checks=checks: self._finish_owner_guard_lookup(cache_key, query_checks, done))

        done, _ = await asyncio.wait((task,), timeout=0.5)
        if not done:
            return cached[1] if cached else {"kind": "error", "reason": "Database security policy is temporarily unavailable."}
        self._finish_owner_guard_lookup(key, None, task)
        cached = self._owner_guard_cache.get(key)
        return cached[1] if cached and cached[0] > time.monotonic() else None

    def _finish_owner_guard_lookup(self, key, checks, task):
        """Cache a completed owner-policy lookup without cancelling its DB work."""
        if self._owner_guard_pending.get(key) is task:
            self._owner_guard_pending.pop(key, None)
        if task.cancelled():
            return
        try:
            entries = task.result()
        except Exception as error:
            self.db.mark_unavailable()
            if time.monotonic() - self._last_database_fallback_log > 60:
                log.warning("Owner security lookup failed (%s); blocking uncached commands during a storage outage", type(error).__name__)
                self._last_database_fallback_log = time.monotonic()
            self._owner_guard_cache[key] = (time.monotonic() + 10.0, {"kind": "error", "reason": "Database security policy is temporarily unavailable."})
            return
        self.db.mark_available()
        if checks is None:
            # The completed callback may have already populated this cache.
            return
        result = None
        for (kind, _), entry in zip(checks, entries):
            if entry:
                result = {"kind": kind, **entry}
                break
        self._owner_guard_cache[key] = (time.monotonic() + 10.0, result)
        if len(self._owner_guard_cache) > 10000:
            now = time.monotonic()
            self._owner_guard_cache = {item: value for item, value in self._owner_guard_cache.items() if value[0] > now}

    async def on_message(self, message: discord.Message):
        # Run non-audit-log Antinuke checks explicitly in the main message
        # dispatcher, including webhook messages, before command processing.
        security_cog = self.get_cog("Security")
        if security_cog is not None:
            await security_cog.inspect_mention_security(message)
        if message.author.bot:
            return
        if self.user and message.content.strip() in {f"<@{self.user.id}>", f"<@!{self.user.id}>"}:
            guard = await self.owner_guard(message.author.id, message.guild.id if message.guild else None)
            if guard:
                if guard.get("kind") == "error":
                    await self._reply_database_unavailable(message)
                    return
                owner_cog = self.get_cog("Owner")
                if owner_cog:
                    await owner_cog.audit_restricted_attempt(message.author, message.guild, message.channel, "bot mention / welcome menu")
                return
            prefix = await current_guild_prefix(self, message.guild.id if message.guild else None)
            embed = panel(
                f"{icon('tick')} Hey, I'm Zyven",
                f"**Server Prefix:** `{prefix}`\n"
                f"**Get Started:** Run `{prefix}help` or mention me with a command.\n"
                "Use only a valid command after my mention to run it.",
            )
            view = MentionWelcomeView(self)
            await message.reply(view=component_card(embed, view), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        await self.process_commands(message)

    async def process_commands(self, message: discord.Message):
        """Process prefix commands with local dedupe and cooldown state."""
        if message.author.bot:
            return
        ctx = await self.get_context(message)
        custom_role_cog = self.get_cog("CustomRole")
        if ctx.guild and custom_role_cog and await custom_role_cog.should_override_command(ctx):
            # Guild custom role commands may shadow a built-in command group
            # (e.g. `gstaff @member`) while preserving its own subcommands.
            return
        if ctx.command is None:
            return
        global_remaining = self.global_rate_limit_remaining()
        if global_remaining > 0:
            now = time.monotonic()
            if now - self._last_global_rate_limit_notice >= 10:
                self._last_global_rate_limit_notice = now
                try:
                    await message.reply(
                        view=component_card(panel(
                            "Discord Rate Limit Active",
                            f"Discord is temporarily limiting Zyven. Try again in **{global_remaining:.1f} seconds**.",
                            color=0xFEE75C,
                        )),
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        delete_after=5,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return
        # Claim each gateway message locally so repeated dispatches in this
        # process cannot run twice. Avoid a database round trip here: it made
        # every prefix command wait on the database and marked the whole bot
        # unavailable during transient pool failures.
        now = time.monotonic()
        async with self._local_command_claim_lock:
            if message.id in self._local_command_claims:
                log.warning("Suppressed repeated gateway dispatch: command=%s message_id=%s", ctx.command.qualified_name, message.id)
                return
            self._local_command_claims[message.id] = now
            if len(self._local_command_claims) > 4096:
                self._local_command_claims = {
                    claim_id: claimed_at
                    for claim_id, claimed_at in self._local_command_claims.items()
                    if now - claimed_at < 300
                }
        command_name = self.prefix_command_name(ctx)
        remaining, notify = self._claim_local_cooldown(message.author.id, command_name)
        if remaining > 0:
            if notify:
                await message.reply(
                    view=component_card(panel("Command Cooldown", f"Wait **{remaining:.1f} seconds** before using `{ctx.prefix or settings.prefix}{command_name}` again.", color=0xFEE75C)),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                    delete_after=3,
                )
            return
        guard = await self.owner_guard(message.author.id, message.guild.id if message.guild else None)
        if guard:
            owner_cog = self.get_cog("Owner")
            if owner_cog and guard.get("kind") != "error":
                await owner_cog.audit_restricted_attempt(message.author, message.guild, message.channel, ctx.command.qualified_name)
            if not message.guild or guard.get("kind") in {"user", "error"}:
                text = "Your bot access is globally restricted." if guard.get("kind") == "user" else guard.get("reason", "Bot security check failed.")
                if guard.get("kind") == "error":
                    await self._reply_database_unavailable(message)
                else:
                    await message.reply(view=component_card(notice_panel(text, title="Command Access Restricted")), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        if ctx.guild and await self._is_ignored_prefix_command(ctx):
            return
        started = time.perf_counter()
        status = "ok"
        try:
            await self.invoke(ctx)
        except Exception:
            status = "error"
            raise
        finally:
            self.performance_sample(command_name, "prefix", (time.perf_counter() - started) * 1000, status)

    async def _reply_database_unavailable(self, message: discord.Message):
        text = (
            "The local database is temporarily unavailable. Commands that need database reads or writes may fail; "
            "cached settings and independent commands can still work. Try again shortly."
        )
        try:
            await message.reply(
                view=component_card(notice_panel(text, title="Database Unavailable")),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("Could not deliver database unavailable notice for command %s", message.id)

    async def invoke(self, ctx: commands.Context):
        """Run a prefix command with the invoker's role permits applied."""
        token = None
        if ctx.guild is not None and ctx.command is not None and isinstance(ctx.author, discord.Member):
            config = await permit_utils.load_config(self, ctx.guild.id)
            token = permit_utils.activate(ctx.guild.id, ctx.author.id, permit_utils.member_grants(config, ctx.author, (ctx.command.root_parent or ctx.command).name))
        try:
            await super().invoke(ctx)
        finally:
            permit_utils.deactivate(token)

    def _owner_immunity_applies(self, guild: discord.Guild, user_id: int) -> bool:
        return user_id != guild.owner_id and not settings.is_owner(user_id)

    @staticmethod
    def _owner_immunity_text(blocked: list[discord.Member]) -> str:
        names = ", ".join(member.mention for member in blocked[:5])
        return f"{names} {'has' if len(blocked) == 1 else 'have'} an owner role and cannot be targeted by this command."

    async def _permit_before_invoke(self, ctx: commands.Context):
        """Block moderation commands aimed at members that hold a permit owner role."""
        if ctx.guild is None or ctx.command is None:
            return
        if not permit_utils.is_immune_command(ctx.command.qualified_name) or not self._owner_immunity_applies(ctx.guild, ctx.author.id):
            return
        config = await permit_utils.load_config(self, ctx.guild.id)
        if not config.owner_roles:
            return
        ids = permit_utils.target_ids([*ctx.args, *ctx.kwargs.values()])
        blocked = permit_utils.immune_targets(config, ctx.guild, ctx.author.id, ids)
        if not blocked:
            return
        ctx._suppress_command_error_response = True
        try:
            await ctx.send(
                view=component_card(notice_panel(self._owner_immunity_text(blocked), title="Owner Immunity")),
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=5,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        raise commands.CheckFailure("Target holds a permit owner role.")

    async def _permit_interaction_check(self, interaction: discord.Interaction) -> bool:
        """Tree-wide check for slash commands: apply role permits and owner immunity."""
        user = interaction.user
        if interaction.guild is None or not isinstance(user, discord.Member):
            return True
        config = await permit_utils.load_config(self, interaction.guild.id)
        # Task-local: the value lives only for this interaction's command task.
        name, ids = permit_utils.slash_command_path(interaction.data or {})
        permit_utils.activate(interaction.guild.id, user.id, permit_utils.member_grants(config, user, name.split(" ")[0]))
        if not config.owner_roles or not self._owner_immunity_applies(interaction.guild, user.id):
            return True
        if not permit_utils.is_immune_command(name):
            return True
        blocked = permit_utils.immune_targets(config, interaction.guild, user.id, ids)
        if not blocked:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                view=component_card(notice_panel(self._owner_immunity_text(blocked), title="Owner Immunity")),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return False

    async def _is_ignored_prefix_command(self, ctx: commands.Context) -> bool:
        """Apply server ignore lists before dispatching a recognized prefix command."""
        data = await self._load_ignore_settings(ctx.guild.id)
        if data is None:
            # Ignore policy is a security decision; an uncached failure must
            # block dispatch rather than silently treating the policy as empty.
            log.warning("Could not load ignore settings for guild %s", ctx.guild.id)
            return True
        security = data.get("security", {}) or {}
        config = security.get("ignore", {}) or {}
        try:
            bypass_users = {int(value) for value in config.get("bypass_users", [])}
            extraowners = {int(value) for value in security.get("extraowners", [])}
            ignored_users = {int(value) for value in config.get("users", [])}
            ignored_channels = {int(value) for value in config.get("channels", [])}
        except (TypeError, ValueError):
            bypass_users, extraowners, ignored_users, ignored_channels = set(), set(), set(), set()
        trusted = {ctx.guild.owner_id, *self.owner_ids, *bypass_users, *extraowners}
        root = ctx.command.root_parent or ctx.command
        if ctx.author.id in trusted or root.name.casefold() in {"ignore", "extraowner", "extraowners"}:
            return False
        if ctx.author.id in ignored_users or ctx.channel.id in ignored_channels:
            return True
        ignored_commands = {str(value).casefold() for value in config.get("commands", [])}
        command_names = {ctx.command.qualified_name.casefold(), root.name.casefold()}
        return bool(command_names.intersection(ignored_commands))

    async def _load_ignore_settings(self, guild_id: int) -> dict | None:
        """Load ignore policy, returning None only when no safe policy is known."""
        now = time.monotonic()
        cached = self._ignore_config_cache.get(guild_id)
        if cached and now - cached[0] < 5:
            return cached[1]
        if self.db.temporarily_unavailable:
            if cached and now - cached[0] < 300:
                log.warning("Using cached ignore settings for guild %s during a database outage", guild_id)
                return cached[1]
            return None
        try:
            data = await await_without_cancelling(self.db.get_guild(guild_id), timeout=1.0)
        except Exception as error:
            self.db.mark_unavailable()
            log.warning("Could not load ignore settings for guild %s (%s)", guild_id, type(error).__name__)
            if cached and now - cached[0] < 300:
                log.warning("Using cached ignore settings for guild %s after a database error", guild_id)
                return cached[1]
            return None
        if self.db.temporarily_unavailable:
            if cached and now - cached[0] < 300:
                return cached[1]
            return None
        self._ignore_config_cache[guild_id] = (time.monotonic(), data)
        return data

    async def _check_ignored_app_command(self, interaction: discord.Interaction) -> bool:
        """Apply the same ignore rules to slash commands and hybrid commands."""
        guard = await self.owner_guard(interaction.user.id, interaction.guild_id)
        if guard:
            if guard.get("kind") == "error":
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        view=component_card(notice_panel("The local database is temporarily unavailable, so this security check could not complete. Try again shortly.", title="Database Unavailable")),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return False
            owner_cog = self.get_cog("Owner")
            if owner_cog:
                await owner_cog.audit_restricted_attempt(interaction.user, interaction.guild, interaction.channel, interaction.command.qualified_name if interaction.command else "unknown")
            text = "Your bot access is globally restricted." if guard.get("kind") == "user" else ("This server is blocked from using the bot." if guard.get("kind") == "guild" else guard.get("reason", "Bot security check failed."))
            if not interaction.response.is_done():
                await interaction.response.send_message(view=component_card(notice_panel(text, title="Command Access Restricted")), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return False
        if interaction.guild is None or interaction.command is None:
            return True
        if settings.is_owner(interaction.user.id):
            return True
        command = interaction.command
        root = command
        while getattr(root, "parent", None) is not None:
            root = root.parent
        if root.name.casefold() in {"ignore", "extraowner", "extraowners"}:
            return True
        data = await self._load_ignore_settings(interaction.guild.id)
        if data is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    view=component_card(notice_panel("The local database is temporarily unavailable, so this security check could not complete. Try again shortly.", title="Database Unavailable")),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return False
        security = data.get("security", {}) or {}
        config = security.get("ignore", {}) or {}
        try:
            bypass_users = {int(value) for value in config.get("bypass_users", [])}
            extraowners = {int(value) for value in security.get("extraowners", [])}
            ignored_users = {int(value) for value in config.get("users", [])}
            ignored_channels = {int(value) for value in config.get("channels", [])}
        except (TypeError, ValueError):
            bypass_users, extraowners, ignored_users, ignored_channels = set(), set(), set(), set()
        if interaction.user.id in {interaction.guild.owner_id, *self.owner_ids, *bypass_users, *extraowners}:
            return True
        ignored_commands = {str(value).casefold() for value in config.get("commands", [])}
        command_names = {command.qualified_name.casefold(), root.name.casefold()}
        reason = None
        if interaction.user.id in ignored_users:
            reason = "You are ignored from using commands in this server."
        elif interaction.channel_id in ignored_channels:
            reason = "Commands are disabled in this channel."
        elif command_names.intersection(ignored_commands):
            reason = f"`/{command.qualified_name}` is ignored in this server."
        if reason:
            await interaction.response.send_message(view=component_card(notice_panel(reason, title="Command Ignored")), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return False
        return True

    async def on_guild_join(self, guild: discord.Guild):
        """Thank the person who invited Zyven in DMs and in the new server."""
        try:
            blocked = await await_without_cancelling(self.db.get_server_blacklist(guild.id), timeout=2)
        except Exception:
            log.exception("Could not verify blacklist status on guild join (%s); leaving safely", guild.id)
            await guild.leave()
            return
        if blocked:
            log.warning("Blocked server %s attempted to add the bot; leaving", guild.id)
            await guild.leave()
            return
        prefix = await current_guild_prefix(self, guild.id)
        description = (
            f"Thanks for adding Zyven to **{discord.utils.escape_markdown(guild.name)}**!\n\n"
            f"Run `{prefix}help` to explore the commands and modules. Configure Antinuke and moderation from the Security and Moderation panels."
        )
        # Use the bot application's synced tick emoji for consistent branding.
        embed = panel(f"{icon('tick')} Thank you for choosing Zyven!", description)
        destination = guild.system_channel
        me = guild.me
        if destination is None or me is None or not destination.permissions_for(me).send_messages:
            destination = next(
                (
                    channel for channel in guild.text_channels
                    if me and channel.permissions_for(me).send_messages
                ),
                None,
            )

        async def send_server_welcome():
            if destination is None:
                return
            try:
                await destination.send(
                    view=component_card(embed, MentionWelcomeView(self)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not post the Zyven welcome in guild %s", guild.id)

        async def send_owner_welcome():
            owner = guild.owner
            if owner is None:
                return
            try:
                await owner.send(
                    view=component_card(embed, MentionWelcomeView(self)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.info("Could not DM the owner of guild %s", guild.id)

        # Deliver both immediately and concurrently; a slow/blocked DM must
        # never hold up the welcome in the server (or vice versa).
        await asyncio.gather(send_server_welcome(), send_owner_welcome())
        task = self.loop.create_task(self._send_inviter_welcome(guild, embed), name=f"zyven-inviter-welcome-{guild.id}")
        self._inviter_welcome_tasks.add(task)
        task.add_done_callback(self._inviter_welcome_tasks.discard)

    async def _send_inviter_welcome(self, guild: discord.Guild, embed: discord.Embed):
        """DM the actual inviter when audit logs identify someone other than the owner."""
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                if getattr(entry.target, "id", None) == (self.user.id if self.user else None):
                    inviter = entry.user
                    if inviter and inviter.id != getattr(guild.owner, "id", None):
                        await inviter.send(
                            view=component_card(embed, MentionWelcomeView(self)),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    return
        except (discord.Forbidden, discord.HTTPException):
            log.info("Could not DM/identify Zyven's inviter in guild %s", guild.id)

    def _finish_application_metric(self, interaction: discord.Interaction, status: str) -> None:
        started = self._command_started_at.pop(getattr(interaction, "id", 0), None)
        if started is None:
            return
        started_at, command_name, kind = started
        self.performance_sample(command_name, kind, (time.perf_counter() - started_at) * 1000, status)

    async def on_app_command_completion(self, interaction: discord.Interaction, command: discord.app_commands.Command):
        self._finish_application_metric(interaction, "ok")

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        self._finish_application_metric(interaction, "error")
        retry_after = self._record_global_rate_limit(error, "application command")
        if interaction.response.is_done():
            return
        if retry_after:
            message = f"Discord is temporarily rate limiting Zyven. Please try again in **{retry_after:.1f} seconds**."
            title = "Discord Rate Limit"
        elif isinstance(error, discord.app_commands.MissingPermissions):
            permissions = ", ".join(name.replace("_", " ").title() for name in error.missing_permissions)
            message = f"You need **{permissions}** permission(s) to use this command."
        elif isinstance(error, discord.app_commands.BotMissingPermissions):
            message = "Zyven is missing the bot permissions required for that command. Check its server and channel permissions."
        else:
            original = getattr(error, "original", error)
            if isinstance(original, discord.Forbidden):
                message = "Zyven cannot complete that action. Check its permissions and role position above the target."
            elif isinstance(original, discord.NotFound):
                message = "That member, role, or message is no longer available. Refresh and try again."
            elif isinstance(original, discord.HTTPException):
                message = "Discord could not complete that action. Please try again in a moment."
            else:
                message = "The command could not be completed. The error has been recorded in the bot logs."
            log.error("Application command failed: %s", error, exc_info=(type(original), original, original.__traceback__))
        title = title if retry_after else ("Access Denied" if isinstance(error, discord.app_commands.MissingPermissions) else "Command Failed")
        status_icon = icon("error") if title == "Access Denied" else icon("warning")
        embed = panel(f"{status_icon} {title}", message, color=0xED4245)
        if interaction.is_expired():
            log.info("Skipped application-command error response for expired interaction %s", interaction.id)
            return
        try:
            await send_card(interaction, embed, ephemeral=True)
            if isinstance(error, discord.app_commands.MissingPermissions):
                schedule_access_denied_delete(interaction)
        except discord.NotFound as exc:
            if exc.code == 10062:
                log.info("Discord interaction %s expired before command-error delivery", interaction.id)
                return
            log.warning("Could not deliver command error response for interaction %s", interaction.id)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not deliver command error response for interaction %s", interaction.id)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Give prefix command errors the same clean CV2 feedback as slash commands."""
        if getattr(ctx, "_suppress_command_error_response", False):
            return
        retry_after = self._record_global_rate_limit(error, "prefix command")
        if retry_after:
            try:
                await ctx.send(
                    view=component_card(panel(
                        "Discord Rate Limit",
                        f"Discord is temporarily rate limiting Zyven. Please try again in **{retry_after:.1f} seconds**.",
                        color=0xFEE75C,
                    )),
                    reference=None,
                    mention_author=False,
                    delete_after=5,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        command = ctx.command
        root = command.root_parent or command if command else None
        if command and command.hidden and isinstance(error, commands.CheckFailure):
            return
        if root and root.name in {"ownerhelp", "noprefix"} and not settings.is_owner(ctx.author.id):
            return
        if isinstance(error, commands.CommandNotFound):
            # Ignore ordinary chat that happens to start with the configured prefix.
            return
        elif isinstance(error, commands.MissingPermissions):
            permissions = ", ".join(name.replace("_", " ").title() for name in error.missing_permissions)
            title = f"{icon('error')} Access Denied"
            message = f"You need **{permissions}** permission(s) to use this command."
        elif isinstance(error, commands.BotMissingPermissions):
            title = f"{icon('warning')} Bot Permission Required"
            message = "Zyven is missing the permissions needed for this command. Check its server permissions and role position."
        elif isinstance(error, commands.MissingRequiredArgument):
            command_name = command.qualified_name if command else "command"
            command_prefix = ctx.prefix or settings.prefix
            await ctx.send(
                view=component_card(panel("", f"Command usage: `{command_prefix}help {command_name}`")),
                reference=None,
                mention_author=False,
                delete_after=3,
            )
            return
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument, commands.UserInputError)):
            if command:
                command_prefix = ctx.prefix or settings.prefix
                await ctx.send(
                    view=component_card(panel("", f"Command usage: `{command_prefix}help {command.qualified_name}`")),
                    reference=None,
                    mention_author=False,
                    delete_after=3,
                )
            return
        elif isinstance(error, commands.NoPrivateMessage):
            title = f"{icon('warning')} Server Only"
            message = "This command can only be used in a server."
        elif isinstance(error, commands.CommandOnCooldown):
            title = f"{icon('warning')} Command Cooldown"
            message = f"Try again in **{error.retry_after:.1f} seconds**."
        elif isinstance(error, commands.DisabledCommand):
            title = f"{icon('warning')} Command Unavailable"
            message = "This command is currently disabled."
        elif type(error).__name__ == "LeaderboardFeatureDisabled":
            feature = getattr(error, "feature", "messages")
            enable_command = "invitelb" if feature == "invites" else "msglb" if feature == "messages" else "voicelb"
            await ctx.send(
                view=component_card(panel(
                    "Leaderboard Disabled",
                    f"Enable this feature first with `{settings.prefix}{enable_command} on`.",
                    color=0xFEE75C,
                ), accent_colour=0xFFFF00),
                reference=None,
                mention_author=False,
            )
            return
        else:
            original = getattr(error, "original", error)
            log.error("Prefix command failed: %s", error, exc_info=(type(original), original, original.__traceback__))
            title = f"{icon('error')} Command Failed"
            message = "The command could not be completed. The error was recorded in the bot logs."
        try:
            await ctx.send(
                view=component_card(panel(title, message, color=0xED4245)),
                reference=None,
                mention_author=False,
                delete_after=5 if isinstance(error, commands.MissingPermissions) else 3 if isinstance(error, commands.CommandOnCooldown) else None,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Could not deliver prefix command error response for message %s", getattr(ctx.message, "id", "unknown"))

    def _startup_color(self, value: str, code: str) -> str:
        if not getattr(self, "_startup_color_enabled", False):
            return value
        return f"\033[{code}m{value}\033[0m"

    async def _show_startup_dashboard(self) -> None:
        """Initialize one compact, in-place startup dashboard for host consoles."""
        import sys

        self._startup_is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._startup_color_enabled = os.getenv("TERM", "xterm").lower() != "dumb" and not os.getenv("NO_COLOR")
        # Keep the entire startup animation in one fixed block. Clearing once
        # also removes noisy package-install lines printed by the host before
        # Python starts this process.
        self._startup_dashboard_lines = 17
        self._startup_progress = 0
        self._startup_spinner_index = 0
        self._startup_statuses = {
            "Database (SQLite + MongoDB)": ("pending", "Waiting"),
            "Lavalink music node": ("pending", "Waiting"),
            "Command modules and events": ("pending", "Waiting"),
            "Discord application commands": ("pending", "Waiting"),
            "Application emoji assets": ("pending", "Waiting for gateway"),
            "Zyven Development": ("pending", "Waiting for gateway"),
        }
        if self._startup_color_enabled:
            print("\033[2J\033[H", end="", flush=True)
        await self._render_startup_dashboard()
        await self._startup_stage("Process bootstrap", 5, "Starting Zyven worker")

    async def _render_startup_dashboard(self) -> None:
        """Redraw the same branded checklist and single progress bar in place."""
        cyan, purple, dim = "1;96", "1;95", "2;37"
        width = 34
        percent = max(0, min(100, int(getattr(self, "_startup_progress", 0))))
        filled = round(width * percent / 100)
        bar = "█" * filled + "░" * (width - filled)
        spinner = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")[self._startup_spinner_index % 10]
        rows = [
            self._startup_color("╔══════════════════════════════════════════════════════════════╗", cyan),
            self._startup_color("║                                                              ║", cyan),
            self._startup_color("║                 Z Y V E N   D E V E L O P M E N T            ║", purple),
            self._startup_color("║               MULTIPURPOSE DISCORD BOT                       ║", cyan),
            self._startup_color("╚══════════════════════════════════════════════════════════════╝", cyan),
            self._startup_color("  ⚙  INITIALIZING SYSTEMS…", "1;97"),
            self._startup_color(f"  [{bar}] {percent:>3}%", "1;96"),
            self._startup_color("  Discord HTTP rate-limit retries are managed automatically", dim),
            "",
        ]
        for label, (state, detail) in self._startup_statuses.items():
            if state == "done":
                marker, color = "✓", "1;92"
            elif state == "warning":
                marker, color = "!", "1;93"
            elif state == "loading":
                marker, color = spinner, "1;96"
            else:
                marker, color = "·", "2;37"
            rows.append(self._startup_color(f"  {marker} {label:<31} {detail}", color))
        # Cursor-home plus erase-to-end replaces the same dashboard block; it
        # never prints another progress panel for each subsystem.
        if self._startup_color_enabled:
            print("\033[H" + "\n".join(f"{row}\033[K" for row in rows), flush=True)
        else:
            # Plain logs remain useful on terminals without ANSI support.
            print("\n".join(rows), flush=True)

    async def _startup_stage(self, label: str, percent: int, detail: str = "", *, state: str = "loading") -> None:
        """Update one checklist row and the shared overall progress bar."""
        percent = max(0, min(100, int(percent)))
        self._startup_progress = max(getattr(self, "_startup_progress", 0), percent)
        if label == "Process bootstrap":
            self._startup_statuses["Command modules and events"] = ("loading", detail)
        elif label in self._startup_statuses:
            self._startup_statuses[label] = (state, detail or ("Loading" if state == "loading" else "Ready"))
        self._startup_spinner_index = getattr(self, "_startup_spinner_index", 0) + 1
        await self._render_startup_dashboard()

    async def setup_hook(self):
        # Hosting panels commonly request shutdown with SIGTERM. Route it
        # through close() so pending MongoDB mirror writes can flush first.
        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM,
                lambda: asyncio.create_task(self.close(), name="zyven-graceful-shutdown"),
            )
        except (NotImplementedError, RuntimeError, AttributeError):
            pass
        await self._show_startup_dashboard()
        await self._startup_stage("Database (SQLite + MongoDB)", 12, "Opening local store and starting mirror")
        await self.db.connect()
        self._db_ping_task = asyncio.create_task(self._database_health_loop(), name="zyven-database-health")
        await self._startup_stage("Database (SQLite + MongoDB)", 24, "SQLite ready; MongoDB sync is in background", state="done")
        lavalink_host = settings.lavalink_host.strip()
        lavalink_auth = settings.lavalink_auth.strip()
        placeholders = {"your-lavalink-host.example", "your-lavalink-password"}
        if lavalink_host.lower() in placeholders or lavalink_auth.lower() in placeholders:
            log.warning("Lavalink still has example placeholder settings; set LAVALINK_HOST, LAVALINK_PORT, LAVALINK_AUTH, and LAVALINK_SECURE in the host panel. Music is disabled until then.")
        elif lavalink_host and lavalink_auth:
            scheme = "https" if settings.lavalink_secure else "http"
            node = wavelink.Node(
                uri=f"{scheme}://{lavalink_host}:{settings.lavalink_port}",
                password=lavalink_auth,
                identifier=settings.lavalink_node_name,
            )
            try:
                await wavelink.Pool.connect(nodes=[node], client=self)
                log.info("Connected to Lavalink node %s", settings.lavalink_node_name)
                await self._startup_stage("Lavalink music node", 34, f"Connected to {settings.lavalink_node_name}", state="done")
            except Exception:
                log.exception("Could not connect to Lavalink node %s", settings.lavalink_node_name)
                await self._startup_stage("Lavalink music node", 34, "Music node unavailable; other bot features continue", state="warning")
        else:
            log.warning("Lavalink is not configured; set LAVALINK_HOST and LAVALINK_AUTH in the host panel. Music is disabled until then.")
            await self._startup_stage("Lavalink music node", 34, "Music is not configured; remaining features continue", state="warning")
        extensions = ("cogs.configuration", "cogs.security", "cogs.moderation", "cogs.moderation_extra", "cogs.jail", "cogs.automod", "cogs.antibot", "cogs.members", "cogs.voice", "cogs.leaderboard", "cogs.information", "cogs.utility", "cogs.fun", "cogs.music", "cogs.giveaways", "cogs.afk", "cogs.autoresponder", "cogs.autoreact", "cogs.customrole", "cogs.reactionroles", "cogs.tickets", "cogs.leveling", "cogs.permit", "cogs.helpdesk", "cogs.owner", "cogs.eventlogs")
        await self._startup_stage("Command modules and events", 40, f"Loading {len(extensions)} feature modules")
        for extension in extensions:
            await self.load_extension(extension)
        for command in self.tree.walk_commands():
            # walk_commands() includes app-command Groups, which are containers
            # and do not support add_check(); checks belong on executable leaves.
            if isinstance(command, (app_commands.Command, app_commands.ContextMenu)):
                command.add_check(self._check_global_rate_limit)
                command.add_check(self._check_app_command_cooldown)
                command.add_check(self._check_ignored_app_command)
        info_commands = ("serverinfo", "userinfo", "avatar", "roleinfo", "stats")
        loaded_info_commands = [name for name in info_commands if self.get_command(name)]
        log.info("Loaded info prefix commands: %s", ", ".join(loaded_info_commands) or "none")
        command_count = sum(1 for _ in self.tree.walk_commands())
        await self._startup_stage("Command modules and events", 65, f"Loaded {len(extensions)} modules and {command_count} application commands", state="done")
        await self._startup_stage("Discord application commands", 70, "Syncing slash commands")
        if settings.sync_guild_id:
            guild = discord.Object(id=settings.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                log.info("Synced application commands to development guild %s", settings.sync_guild_id)
                await self._startup_stage("Discord application commands", 82, "Synced to development server", state="done")
            except discord.HTTPException as error:
                log.warning(
                    "Guild application-command sync failed for %s (HTTP %s); trying global sync",
                    settings.sync_guild_id,
                    error.status,
                )
                try:
                    await self.tree.sync()
                    log.info("Synced application commands globally after guild sync failure")
                    await self._startup_stage("Discord application commands", 82, "Development sync failed; global sync succeeded", state="warning")
                except discord.HTTPException as global_error:
                    log.error(
                        "Application-command sync failed (HTTP %s); bot will continue with prefix commands",
                        global_error.status,
                    )
                    await self._startup_stage("Discord application commands", 82, "Sync failed; prefix commands remain available", state="warning")
        else:
            try:
                await self.tree.sync()
                log.info("Synced application commands globally")
                await self._startup_stage("Discord application commands", 82, "Synced globally", state="done")
            except discord.HTTPException as error:
                log.error(
                    "Global application-command sync failed (HTTP %s); bot will continue with prefix commands",
                    error.status,
                )
                await self._startup_stage("Discord application commands", 82, "Sync failed; prefix commands remain available", state="warning")
        if settings.backup_webhook_url:
            if self._backup_task is None:
                self._backup_task = asyncio.create_task(self._source_backup_loop(), name="zyven-source-backups")
        if not settings.mongo_uri:
            log.warning("MONGODB_URI is not configured; bot will run on local SQLite without remote sync")
        await self._startup_stage("Emoji assets and gateway", 90, "Modules ready; waiting for Discord gateway")

    async def _source_backup_loop(self):
        while not self.is_closed():
            try:
                filename, size = await send_source_backup(settings.backup_webhook_url)
                log.info("Uploaded source backup %s (%s bytes)", filename, size)
            except asyncio.CancelledError:
                raise
            except discord.HTTPException as error:
                log.warning(
                    "Source backup upload failed (Discord HTTP %s, code %s)",
                    getattr(error, "status", "unknown"),
                    getattr(error, "code", "unknown"),
                )
            except Exception as error:
                log.warning("Source backup upload failed (%s)", type(error).__name__)
            try:
                await asyncio.sleep(settings.backup_interval_hours * 3600)
            except asyncio.CancelledError:
                raise

    async def _database_health_loop(self):
        while not self.is_closed():
            try:
                await asyncio.sleep(5 if self.db.temporarily_unavailable else 15)
                started = time.perf_counter()
                await await_without_cancelling(self.db.store.ping(), timeout=3.0)
                self.db.last_ping_ms = (time.perf_counter() - started) * 1000
                self.db.last_ping_at = time.time()
                self.db.mark_available()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.db.mark_unavailable()
                now = time.monotonic()
                if now - self._last_database_health_warning > 60:
                    # Avoid formatting asyncio's chained CancelledError / TimeoutError
                    # traceback here; Python 3.13 can fail while rendering it and
                    # stall the gateway event loop.
                    log.warning("Database health check failed; retrying shortly (%s)", type(error).__name__)
                    self._last_database_health_warning = now

    async def on_ready(self):
        if not self._emoji_sync_started:
            await self._startup_stage("Application emoji assets", 94, "Syncing custom loading and UI emojis")
            self._emoji_sync_started = True
            await sync_application_emojis(self)
            await self._startup_stage("Application emoji assets", 97, "Emoji sync finished", state="done")
        # Resolve the ticket cog's source emoji assets only after the shared
        # Developer Portal sync has populated the application emoji markup.
        from cogs.tickets import refresh_ticket_emojis
        refresh_ticket_emojis()
        if self._presence_task is None or self._presence_task.done():
            self._presence_task = asyncio.create_task(self._rotate_presence(), name="zyven-presence-rotation")
        log.info("Connected as %s in %s server(s)", self.user, len(self.guilds))
        if not getattr(self, "_startup_ready_reported", False):
            self._startup_ready_reported = True
            await self._startup_stage("Zyven Development", 100, f"Online as {self.user} • ready in {len(self.guilds)} server(s)", state="done")

    async def close(self):
        if self._shutdown_started:
            await super().close()
            return
        self._shutdown_started = True
        if self._presence_task and not self._presence_task.done():
            self._presence_task.cancel()
            try:
                await self._presence_task
            except asyncio.CancelledError:
                pass
        if self._db_ping_task and not self._db_ping_task.done():
            self._db_ping_task.cancel()
            try:
                await self._db_ping_task
            except asyncio.CancelledError:
                pass
        if self._backup_task and not self._backup_task.done():
            self._backup_task.cancel()
            try:
                await self._backup_task
            except asyncio.CancelledError:
                pass
        for task in tuple(self._inviter_welcome_tasks):
            if not task.done():
                task.cancel()
        if self._inviter_welcome_tasks:
            await asyncio.gather(*self._inviter_welcome_tasks, return_exceptions=True)
        self._inviter_welcome_tasks.clear()
        for task in tuple(self._owner_alert_tasks):
            if not task.done():
                task.cancel()
        if self._owner_alert_tasks:
            await asyncio.gather(*self._owner_alert_tasks, return_exceptions=True)
        self._owner_alert_tasks.clear()
        await self.db.close()
        await super().close()

    async def _rotate_presence(self):
        """Rotate the bot's profile activity without resetting on reconnects."""
        index = 0
        while not self.is_closed():
            shard_count = self.shard_count or len(getattr(self, "shards", {})) or 1
            activities = (
                f"{settings.prefix}help | Security in your Server",
                f"/{shard_count} Shards",
                f"Guarding {len(self.guilds)} servers",
            )
            try:
                await self.change_presence(
                    status=discord.Status.idle,
                    activity=discord.Game(name=activities[index % len(activities)]),
                )
                index += 1
                await asyncio.sleep(25)
            except asyncio.CancelledError:
                raise
            except discord.HTTPException as error:
                log.warning("Presence update failed (HTTP %s); retrying on next rotation", error.status)
                await asyncio.sleep(25)


bot = ZyvenBot()
