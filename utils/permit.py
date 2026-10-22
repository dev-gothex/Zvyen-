"""Role based command permits and owner-role immunity.

``?permit @role`` lets a server owner unlock *bot commands* (Mute, Ban, Purge,
...) for a role without giving that role the matching Discord permission. Grants
are stored per guild in ``guild["permit"]``::

    {"roles": {"<role id>": ["ban", "mute", ...]}, "owner_roles": [<role id>, ...]}

How a grant is applied
----------------------
Most Zyven commands gate themselves on ``member.guild_permissions.<name>`` (or on
``interaction.permissions`` for slash commands).  Instead of rewriting every
gate, the bot publishes the Discord permissions that the invoked command's permit
keys unlock in a ``ContextVar`` for the duration of that command (see ``ZyvenBot.invoke`` and the command tree check in
``core/bot.py``).  While that variable is set, ``Member.guild_permissions`` and
``Interaction.permissions`` return the real permissions *plus* the permitted
ones - but only for the invoking member, only inside that command's task, and
never ``administrator``.  Discord itself never sees the extra permissions, and
role hierarchy checks inside the commands still apply.

Owner roles
-----------
Members holding an owner role cannot be the target of moderation style commands
(see ``IMMUNE_COMMANDS``) unless the invoker is the server owner or a bot owner.
"""

from __future__ import annotations

import contextvars
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

import discord

log = logging.getLogger("zyven.permit")

# (permit key, label shown in the UI, root command names it unlocks,
#  Discord permissions those commands check for).  Roots are the canonical
# prefix command / slash command names; ``administrator`` is never unlocked.
PERMIT_COMMANDS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("mute", "Mute", ("mute", "timeout"), ("moderate_members",)),
    ("unmute", "Unmute", ("unmute", "untimeout", "unmuteall"), ("moderate_members",)),
    ("kick", "Kick", ("kick",), ("kick_members",)),
    ("ban", "Ban", ("ban",), ("ban_members",)),
    ("unban", "Unban", ("unban", "unbanall"), ("ban_members",)),
    ("nick", "Nick", ("nick", "nickname"), ("manage_nicknames",)),
    ("purge", "Purge", ("purge", "pb"), ("manage_messages",)),
    ("warn", "Warn", ("warn", "warnings", "unwarn", "clearwarnings", "clearwarns"), ("moderate_members",)),
    ("role", "Role", ("role", "removerole", "rrole"), ("manage_roles",)),
    ("giveaway", "Giveaway", ("gstart", "gconfig", "gwystaff", "gembed", "glist"), ("manage_guild",)),
    ("voice", "Voice", ("voice", "vcban", "vckick", "vcunmute", "vcpull"),
     ("mute_members", "deafen_members", "move_members", "manage_channels")),
)
PERMIT_PERMISSIONS: tuple[tuple[str, str], ...] = tuple((key, label) for key, label, _, _ in PERMIT_COMMANDS)
PERMIT_KEYS: frozenset[str] = frozenset(key for key, _ in PERMIT_PERMISSIONS)
PERMIT_LABELS: dict[str, str] = dict(PERMIT_PERMISSIONS)
_KEY_ROOTS = {key: frozenset(roots) for key, _, roots, _ in PERMIT_COMMANDS}
_KEY_DISCORD = {key: frozenset(perms) for key, _, _, perms in PERMIT_COMMANDS}
DISCORD_PERMISSION_NAMES: frozenset[str] = frozenset(p for perms in _KEY_DISCORD.values() for p in perms)

# Commands that act on a member and are blocked against owner-role members.
# Names are ``Command.qualified_name`` (prefix) / the slash command path.
IMMUNE_COMMANDS: frozenset[str] = frozenset({
    "ban", "kick", "mute", "timeout", "nick", "nickname", "warn", "transfer",
    "jail", "jail add",
    "role", "role add", "role remove", "removerole",
    "vcban", "vckick", "vcpull",
    "voice mute", "voice deafen", "voice kick", "voice move", "voice pull",
    "voicekick", "voicedeny",
})


@dataclass(frozen=True)
class PermitConfig:
    roles: dict[int, frozenset[str]] = field(default_factory=dict)
    owner_roles: frozenset[int] = frozenset()

    def grants_for_command(self, role_ids: Iterable[int], root_command: str | None) -> frozenset[str]:
        """Discord permissions unlocked for ``root_command`` by the given roles."""
        root = (root_command or "").casefold()
        granted: set[str] = set()
        for role_id in role_ids:
            for key in self.roles.get(role_id, ()):
                if root in _KEY_ROOTS.get(key, ()):
                    granted |= _KEY_DISCORD[key]
        return frozenset(granted)


def parse_config(guild_doc: dict | None) -> PermitConfig:
    """Build a ``PermitConfig`` from a guild document, ignoring malformed data."""
    raw = (guild_doc or {}).get("permit") or {}
    if not isinstance(raw, dict):
        return PermitConfig()
    roles: dict[int, frozenset[str]] = {}
    raw_roles = raw.get("roles") or {}
    if isinstance(raw_roles, dict):
        for role_id, keys in raw_roles.items():
            try:
                rid = int(role_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(keys, (list, tuple, set, frozenset)):
                continue
            valid = frozenset(str(key) for key in keys if str(key) in PERMIT_KEYS)
            if valid:
                roles[rid] = valid
    owners: set[int] = set()
    for role_id in raw.get("owner_roles") or []:
        try:
            owners.add(int(role_id))
        except (TypeError, ValueError):
            continue
    return PermitConfig(roles=roles, owner_roles=frozenset(owners))


def dump_config(config: PermitConfig) -> dict:
    """Serialise a config for storage (string keys, sorted lists)."""
    return {
        "roles": {str(role_id): sorted(keys) for role_id, keys in sorted(config.roles.items()) if keys},
        "owner_roles": sorted(config.owner_roles),
    }


async def load_config(bot, guild_id: int) -> PermitConfig:
    try:
        return parse_config(await bot.db.get_guild(guild_id))
    except Exception:
        log.exception("Could not load permit settings for guild %s", guild_id)
        return PermitConfig()


def member_grants(config: PermitConfig, member: discord.Member, root_command: str | None) -> frozenset[str]:
    return config.grants_for_command((role.id for role in getattr(member, "roles", ())), root_command)


def has_owner_role(config: PermitConfig, member) -> bool:
    return bool(config.owner_roles) and any(role.id in config.owner_roles for role in getattr(member, "roles", ()))


# --------------------------------------------------------------------------- #
# Applying grants for the duration of one command
# --------------------------------------------------------------------------- #

# (guild id, user id, granted permission names) of the member running a command.
_active: contextvars.ContextVar[tuple[int, int, frozenset[str]] | None] = contextvars.ContextVar(
    "zyven_permit_grants", default=None
)


def activate(guild_id: int, user_id: int, grants: frozenset[str]):
    """Publish ``grants`` for the current task. Returns a reset token (or None)."""
    if not grants:
        return None
    return _active.set((guild_id, user_id, grants))


def deactivate(token) -> None:
    if token is not None:
        try:
            _active.reset(token)
        except ValueError:
            # Reset from a different context; the value dies with its task anyway.
            pass


def _merge(base: discord.Permissions, grants: frozenset[str]) -> discord.Permissions:
    if base.administrator:
        return base
    extra = discord.Permissions(**{name: True for name in grants if name in DISCORD_PERMISSION_NAMES})
    return discord.Permissions(base.value | extra.value)


def install_permission_patch() -> bool:
    """Make ``Member.guild_permissions`` / ``Interaction.permissions`` permit-aware.

    Safe to call more than once. Returns True when the patch is (already) active.
    """
    if getattr(discord.Member, "_zyven_permit_patched", False):
        return True
    member_fget = getattr(discord.Member.guild_permissions, "fget", None)
    interaction_fget = getattr(discord.Interaction.permissions, "fget", None)
    if member_fget is None or interaction_fget is None:
        log.error("discord.py internals changed; permit grants will not apply to commands")
        return False

    def guild_permissions(self):
        base = member_fget(self)
        state = _active.get()
        if state is None or state[1] != self.id or state[0] != self.guild.id:
            return base
        return _merge(base, state[2])

    def interaction_permissions(self):
        base = interaction_fget(self)
        state = _active.get()
        if state is None or self.guild_id is None or state[0] != self.guild_id or state[1] != getattr(self.user, "id", None):
            return base
        return _merge(base, state[2])

    discord.Member.guild_permissions = property(guild_permissions, doc=discord.Member.guild_permissions.__doc__)
    discord.Interaction.permissions = property(interaction_permissions, doc=discord.Interaction.permissions.__doc__)
    discord.Member._zyven_permit_patched = True
    return True


# --------------------------------------------------------------------------- #
# Owner-role immunity
# --------------------------------------------------------------------------- #

_REF_RE = re.compile(r"^\s*(?:<@!?(\d{15,22})>|(\d{15,22}))\s*$")


def is_immune_command(qualified_name: str | None) -> bool:
    return bool(qualified_name) and qualified_name.casefold() in IMMUNE_COMMANDS


def target_ids(values: Iterable) -> set[int]:
    """Member/user ids referenced by parsed command arguments.

    Strings only count when the whole string is a mention or an id (the form
    used by commands such as ``ban <id>``), so free-text reasons are ignored.
    """
    ids: set[int] = set()
    for value in values:
        if isinstance(value, (discord.Member, discord.User)):
            ids.add(value.id)
        elif isinstance(value, str):
            match = _REF_RE.match(value)
            if match:
                ids.add(int(match.group(1) or match.group(2)))
    return ids


def slash_command_path(data: dict) -> tuple[str, set[int]]:
    """Qualified name and referenced user ids from raw application-command data."""
    names = [str(data.get("name", ""))]
    ids: set[int] = set()
    options = data.get("options") or []
    while options:
        next_options = []
        for option in options:
            kind = option.get("type")
            if kind in (1, 2):  # subcommand / subcommand group
                names.append(str(option.get("name", "")))
                next_options = option.get("options") or []
            elif kind in (6, 9):  # user / mentionable
                try:
                    ids.add(int(option.get("value")))
                except (TypeError, ValueError):
                    pass
        options = next_options
    return " ".join(name for name in names if name), ids


def immune_targets(config: PermitConfig, guild: discord.Guild, invoker_id: int, ids: Iterable[int]) -> list[discord.Member]:
    """Owner-role members among ``ids`` (excluding the invoker themselves)."""
    found = []
    for user_id in ids:
        if user_id == invoker_id:
            continue
        member = guild.get_member(user_id)
        if member is not None and has_owner_role(config, member):
            found.append(member)
    return found
