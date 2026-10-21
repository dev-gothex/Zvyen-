from discord.ext import commands

from core.settings import settings


def owner_or_guild_permissions(**required):
    """Allow configured owners to use a command only when Zyven is Administrator."""
    async def predicate(ctx: commands.Context) -> bool:
        guild = ctx.guild
        bot_member = guild.me if guild is not None else None
        if settings.is_owner(getattr(ctx.author, "id", None)) and bot_member is not None:
            if bot_member.guild_permissions.administrator:
                return True
        permissions = getattr(ctx.author, "guild_permissions", None)
        missing = [name for name, value in required.items() if getattr(permissions, name, False) != value]
        if missing:
            raise commands.MissingPermissions(missing)
        return True

    return commands.check(predicate)
