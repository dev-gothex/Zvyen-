"""Isolated callback smoke runner for owner diagnostics.

This process never logs in to Discord or connects to the configured database.
Callbacks receive synthetic context objects and all I/O boundaries are blocked.
"""
from __future__ import annotations

import argparse
import asyncio
import builtins
import importlib
import inspect
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import traceback
import types
import typing
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


class SandboxBlocked(RuntimeError):
    pass


class FakeValue:
    id = 1
    name = "selftest"
    display_name = "Self Test Member"
    mention = "<@1>"
    content = "selftest"
    url = "https://example.invalid/selftest"
    filename = "selftest.txt"
    position = 1
    created_at = None
    guild_permissions = types.SimpleNamespace(**{name: True for name in (
        "administrator", "manage_guild", "manage_roles", "manage_channels",
        "manage_messages", "moderate_members", "ban_members", "kick_members",
    )})
    bot = False
    roles: list = []
    members: list = []
    channels: list = []
    emojis: list = []
    stickers: list = []
    voice = None
    category = None
    reference = None
    attachments: list = []
    embeds: list = []
    components: list = []
    status = discord.Status.online
    color = discord.Color.blurple()
    colour = color
    icon = None
    avatar = None
    banner = None
    default_role = None
    owner_id = 1
    premium_tier = 0
    verification_level = discord.VerificationLevel.none
    explicit_content_filter = discord.ContentFilter.disabled
    mfa_level = discord.MFALevel.disabled
    preferred_locale = discord.Locale.american_english

    def __init__(self, **values):
        self.__dict__.update(values)

    def __bool__(self):
        return False

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __getitem__(self, key):
        return self

    def get(self, key, default=None):
        return default

    def items(self):
        return ().__iter__()

    def values(self):
        return ().__iter__()

    def __int__(self):
        return 1

    def __str__(self):
        return "selftest"

    def __getattr__(self, name):
        if name.startswith("is_") or name.startswith("has_"):
            return lambda *args, **kwargs: False
        if name.startswith("get_"):
            return lambda *args, **kwargs: None
        if name in {"history", "audit_logs", "bans", "fetch_bans", "members", "roles", "channels"}:
            async def empty_iterator(*args, **kwargs):
                if False:
                    yield None
            return empty_iterator

        async def blocked_or_stub(*args, **kwargs):
            return FakeValue()
        return blocked_or_stub


class FakeMember(FakeValue):
    async def add_roles(self, *roles, **kwargs):
        return None

    async def remove_roles(self, *roles, **kwargs):
        return None

    async def edit(self, **kwargs):
        return self

    async def send(self, *args, **kwargs):
        return FakeValue()

    async def ban(self, **kwargs):
        return None

    async def kick(self, **kwargs):
        return None

    async def timeout(self, *args, **kwargs):
        return None


class FakeChannel(FakeValue):
    async def send(self, *args, **kwargs):
        return FakeMessage()

    async def purge(self, *args, **kwargs):
        return []


class FakeMessage(FakeValue):
    async def delete(self, *args, **kwargs):
        return None

    async def edit(self, *args, **kwargs):
        return self

    async def reply(self, *args, **kwargs):
        return self


class FakeDatabase:
    """Returns empty read fixtures and records writes without applying them."""
    def __init__(self):
        self.blocked_writes: list[str] = []
        self.db = FakeValue()

    def __getattr__(self, name):
        async def fake_db_call(*args, **kwargs):
            if any(word in name.casefold() for word in ("set_", "add_", "remove_", "delete_", "update_", "create_", "reset_", "insert_", "increment_", "clear_", "save_", "upsert_")):
                self.blocked_writes.append(name)
                return False
            if name.startswith(("list_", "fetch_all", "search_")):
                return []
            if name.startswith(("count_", "get_count")):
                return 0
            return FakeValue()
        return fake_db_call


class FakeBot(FakeValue):
    def __init__(self):
        super().__init__(db=FakeDatabase(), user=FakeMember(id=999, name="Zyven"), guilds=[])
        self._fake_cogs = {}
        self._fake_commands = {}
        self.latency = 0.0
        self.guilds = []
        self.session = FakeValue()
        self.loop = _FakeLoop()
        self.tree = FakeValue()

    def get_cog(self, name):
        return self._fake_cogs.get(name)

    def get_command(self, name):
        return self._fake_commands.get(name)

    def get_guild(self, guild_id):
        return FAKE_GUILD

    def get_channel(self, channel_id):
        return FAKE_CHANNEL

    def get_user(self, user_id):
        return FAKE_MEMBER


FAKE_ROLE = FakeValue(id=2, name="SelfTest Role", mention="<@&2>")
FAKE_CHANNEL = FakeChannel(id=3, name="self-test", mention="<#3>")
FAKE_MEMBER = FakeMember(
    id=1, name="SelfTest", display_name="SelfTest", mention="<@1>",
    roles=[FAKE_ROLE], guild_permissions=FakeValue(administrator=True, manage_guild=True,
        manage_roles=True, manage_channels=True, manage_messages=True, moderate_members=True,
        ban_members=True, kick_members=True),
)
FAKE_GUILD = FakeValue(
    id=4, name="Isolated Command Sandbox", owner_id=1,
    roles=[FAKE_ROLE], members=[FAKE_MEMBER], channels=[FAKE_CHANNEL],
    me=FakeMember(id=999, name="Zyven", roles=[FAKE_ROLE]),
    default_role=FakeValue(id=4, name="@everyone"),
)
FAKE_MEMBER.guild = FAKE_GUILD
FAKE_CHANNEL.guild = FAKE_GUILD


class FakeContext:
    def __init__(self, bot, command):
        self.bot = bot
        self.guild = FAKE_GUILD
        self.author = FAKE_MEMBER
        self.user = FAKE_MEMBER
        self.channel = FAKE_CHANNEL
        self.me = FAKE_GUILD.me
        self.message = FakeMessage(
            id=10, author=FAKE_MEMBER, guild=FAKE_GUILD, channel=FAKE_CHANNEL,
            content=f"?{command.qualified_name}", reference=None,
        )
        self.prefix = "?"
        self.clean_prefix = "?"
        self.command = command
        self.valid = True
        self.sent_messages = []
        self.interaction = None

    async def send(self, *args, **kwargs):
        message = FakeMessage()
        self.sent_messages.append((args, kwargs))
        return message

    async def reply(self, *args, **kwargs):
        return await self.send(*args, **kwargs)

    async def defer(self, *args, **kwargs):
        return None

    def typing(self):
        return _AsyncNullContext()


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


class _FakeLoop:
    def create_task(self, coroutine, *args, **kwargs):
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return FakeValue()


class FakeInteractionResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._done = True
        self.interaction.sent_messages.append((args, kwargs))
        return FakeMessage()

    async def defer(self, *args, **kwargs):
        self._done = True

    async def edit_message(self, *args, **kwargs):
        self._done = True
        self.interaction.sent_messages.append((args, kwargs))


class FakeInteraction:
    def __init__(self, bot, command):
        self.client = bot
        self.user = FAKE_MEMBER
        self.guild = FAKE_GUILD
        self.channel = FAKE_CHANNEL
        self.guild_id = FAKE_GUILD.id
        self.channel_id = FAKE_CHANNEL.id
        self.command = command
        self.data = {}
        self.namespace = FakeValue()
        self.sent_messages = []
        self.response = FakeInteractionResponse(self)
        self.followup = FakeValue()

    async def edit_original_response(self, *args, **kwargs):
        return FakeMessage()

    async def original_response(self):
        return FakeMessage()


def _unwrap_type(annotation):
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        choices = [item for item in typing.get_args(annotation) if item is not type(None)]
        return choices[0] if choices else str
    return origin or annotation


def _sample_value(annotation, parameter_name: str):
    origin = typing.get_origin(annotation)
    if origin is not None and "Greedy" in str(origin):
        return []
    target = _unwrap_type(annotation)
    try:
        if target is bool:
            return True
        if target is int:
            return 1
        if target is float:
            return 1.0
        if target is str:
            return "selftest"
        if target is discord.Member:
            return FAKE_MEMBER
        if target is discord.User:
            return FAKE_MEMBER
        if target is discord.Role:
            return FAKE_ROLE
        if target in (discord.Guild,):
            return FAKE_GUILD
        if target in (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread, discord.ForumChannel):
            return FAKE_CHANNEL
        if target is discord.Attachment:
            return FakeValue(url="https://example.invalid/selftest.txt", filename="selftest.txt")
        if target is discord.Object:
            return discord.Object(id=1)
        if inspect.isclass(target) and issubclass(target, (str, int, float)):
            return target("selftest") if issubclass(target, str) else target(1)
        if inspect.isclass(target) and issubclass(target, __import__("enum").Enum):
            return next(iter(target))
    except (TypeError, StopIteration):
        pass
    return "selftest"


def _commands_for_cog(cog_class):
    found = {}
    for command in getattr(cog_class, "__cog_commands__", ()):
        name = getattr(command, "qualified_name", getattr(command, "name", "unknown"))
        found[name.casefold()] = command
    return list(found.values())


def _walk_app_commands(command):
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            yield from _walk_app_commands(child)
    else:
        yield command


async def _run_with_io_blocked(callback, *args, **kwargs):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_getaddrinfo = socket.getaddrinfo
    original_create_connection = socket.create_connection
    original_open = builtins.open
    original_io_open = pathlib.Path.open
    original_write_text = pathlib.Path.write_text
    original_write_bytes = pathlib.Path.write_bytes
    original_os_open = os.open
    original_remove = os.remove
    original_unlink = os.unlink
    original_popen = subprocess.Popen

    def deny_network(*_args, **_kwargs):
        raise SandboxBlocked("network access blocked in self-test")

    def safe_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise SandboxBlocked("file writes blocked in self-test")
        return original_open(file, mode, *args, **kwargs)

    def deny_process(*_args, **_kwargs):
        raise SandboxBlocked("child processes blocked in self-test")

    def deny_file_write(*_args, **_kwargs):
        raise SandboxBlocked("file writes/deletes blocked in self-test")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.socket.sendto = deny_network
    socket.create_connection = deny_network
    socket.getaddrinfo = deny_network
    builtins.open = safe_open
    subprocess.Popen = deny_process
    pathlib.Path.open = safe_open
    pathlib.Path.write_text = deny_file_write
    pathlib.Path.write_bytes = deny_file_write
    os.open = deny_file_write
    os.remove = deny_file_write
    os.unlink = deny_file_write
    try:
        result = callback(*args, **kwargs)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=4.0)
        return result
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.socket.sendto = original_sendto
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        builtins.open = original_open
        subprocess.Popen = original_popen
        pathlib.Path.open = original_io_open
        pathlib.Path.write_text = original_write_text
        pathlib.Path.write_bytes = original_write_bytes
        os.open = original_os_open
        os.remove = original_remove
        os.unlink = original_unlink


async def _test_command(command, cog_class, fake_bot, *, kind="prefix"):
    name = getattr(command, "qualified_name", getattr(command, "name", "unknown"))
    if name.casefold() in {"ownerscan", "fullscan"}:
        return {"command": name, "status": "SKIPPED", "detail": "recursive diagnostic command"}
    try:
        write_count_before = len(fake_bot.db.blocked_writes)
        callback = command.callback
        if not callable(callback):
            return {"command": name, "status": "CALLBACK_EXCEPTION", "detail": "registered command callback is not callable"}
        cog = cog_class(fake_bot)
        ctx = FakeContext(fake_bot, command) if kind == "prefix" else FakeInteraction(fake_bot, command)
        signature = inspect.signature(callback)
        parameters = list(signature.parameters.values())
        if parameters and parameters[0].name == "self":
            parameters = parameters[1:]
        if parameters and parameters[0].name in {"ctx", "context", "interaction"}:
            parameters = parameters[1:]
        try:
            hints = typing.get_type_hints(callback)
        except Exception:
            hints = {}
        positional = []
        keyword = {}
        for parameter in parameters:
            if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if parameter.default is not inspect.Parameter.empty:
                continue
            annotation = hints.get(parameter.name, parameter.annotation)
            sample = _sample_value(annotation, parameter.name)
            if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
                keyword[parameter.name] = sample
            else:
                positional.append(sample)
        await _run_with_io_blocked(callback, cog, ctx, *positional, **keyword)
        blocked = len(fake_bot.db.blocked_writes) - write_count_before
        return {
            "command": name,
            "status": "SIMULATED",
            "detail": f"callback returned; {len(ctx.sent_messages)} response(s), {blocked} database write(s) blocked",
        }
    except asyncio.TimeoutError:
        return {"command": name, "status": "SIMULATION_TIMEOUT", "detail": "callback exceeded 4 seconds in sandbox"}
    except SandboxBlocked as exc:
        return {"command": name, "status": "BLOCKED_IO", "detail": str(exc)}
    except Exception as exc:
        return {
            "command": name,
            "status": "CALLBACK_EXCEPTION",
            "detail": f"{type(exc).__name__}: {str(exc)[:1000]}\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))[-5000:]}",
        }


async def run(root: Path):
    sys.path.insert(0, str(root))
    results = []
    from core.settings import settings
    FAKE_MEMBER.id = settings.owner_id
    FAKE_MEMBER.mention = f"<@{settings.owner_id}>"
    FAKE_GUILD.owner_id = settings.owner_id
    fake_bot = FakeBot()
    fake_bot.user.id = settings.application_id
    try:
        import discord.ext.commands as command_framework
        discovered = []
        for path in sorted((root / "cogs").glob("*.py")):
            if path.name == "__init__.py":
                continue
            module_name = f"cogs.{path.stem}"
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                results.append({"command": module_name, "status": "IMPORT_ERROR", "detail": f"{type(exc).__name__}: {exc}"})
                continue
            classes = [value for value in vars(module).values()
                       if inspect.isclass(value) and value is not command_framework.Cog
                       and issubclass(value, command_framework.Cog)]
            for cog_class in classes:
                try:
                    commands_to_test = _commands_for_cog(cog_class)
                except Exception as exc:
                    results.append({"command": module_name, "status": "DISCOVERY_ERROR", "detail": f"{type(exc).__name__}: {exc}"})
                    continue
                for command in commands_to_test:
                    discovered.append((command, cog_class, "prefix"))
                for app_command in getattr(cog_class, "__cog_app_commands__", ()):
                    for app_leaf in _walk_app_commands(app_command):
                        if callable(getattr(app_leaf, "callback", None)):
                            discovered.append((app_leaf, cog_class, "application"))
        total = len(discovered)
        print(f"TOTAL|{total}", flush=True)
        for index, (command, cog_class, kind) in enumerate(discovered, 1):
            results.append(await _test_command(command, cog_class, fake_bot, kind=kind))
            name = getattr(command, "qualified_name", getattr(command, "name", "unknown"))
            results[-1]["kind"] = kind
            print(f"PROGRESS|{index}|{total}|{kind}:{name}", flush=True)
    finally:
        # callbacks never start a Discord client or a real database connection.
        pass
    return results


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    results = await run(root)
    output = {
        "python": sys.version,
        "command_count": len(results),
        "results": results,
    }
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
