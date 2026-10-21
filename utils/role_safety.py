"""Shared safety checks for roles assigned automatically or through setup commands."""

import discord


DANGEROUS_ROLE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "manage_messages",
    "ban_members",
    "kick_members",
    "moderate_members",
    "mention_everyone",
    "manage_nicknames",
    "manage_emojis_and_stickers",
    "manage_events",
    "manage_threads",
)


def dangerous_permissions(role: discord.Role) -> list[str]:
    return [name for name in DANGEROUS_ROLE_PERMISSIONS if getattr(role.permissions, name, False)]
